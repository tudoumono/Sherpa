"""モデルのコンテキスト窓（BUDGET-2・`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md`
§3.4・2026-09-03 裁定）。

`agentic_search.py::resolve_tool_result_budgets` が使う「窓由来のツール結果バイト上限」の導出と、
モデル名→窓 tokens の4段解決（登録値(DB) > プロバイダAPI > コード同梱シード表 > 不明）を持つ。

**min() 方式**（呼び出し側の契約）: 実効予算 = min(BUDGET-1 の解決値, 窓由来の上限)。小窓モデルへ
切り替えると自動で縮む一方、窓が不明・大きい場合は BUDGET-1 の値から自動では増えない
（支出の自動拡大はしない・裁定）。

tokenizer API は使わない（非決定・コスト）。予約枠・安全係数・バイト換算率は根拠コメント付きの
固定定数（env 化しない・裁定）。プロバイダAPI（Ollama `/api/show`・Anthropic Models API）の照会は
失敗時に例外を投げず None を返す（呼び出し側は次の解決段へ fail-safe に倒す）。
"""
from __future__ import annotations

import json
import logging
import threading
import time
import urllib.request

_log = logging.getLogger("sherpa")

# ---- 窓由来バイト上限の導出（決定的な純関数） -----------------------------------------------

# 予約枠（tokens）: システムプロンプト（`agentic_search.SYSTEM` 相当・高々数千 token）＋履歴
# （上流でキャップ済みだが数千〜1万 token 程度になりうる）＋応答余白（ツールループ中間応答の上限
# `agentic_search._ANTHROPIC_MAX_TOKENS`=16000 が現行3方言中の最大値）をまとめて確保する固定値。
# モデルごとの実測はしない（tokenizer API を使わない裁定のため）——保守的にまとめて引く。
_WINDOW_RESERVED_TOKENS = 32000
# 安全係数: token数の見積り誤差（バイト換算率は概算であり実トークナイザと厳密には一致しない）・
# 1 run 内で複数回のツール呼び出しが累積する誤差を吸収するための掛け目。
_WINDOW_SAFETY_FACTOR = 0.5
# バイト換算率（保守的な固定レート・token→byte）。tokenizer API は使わない裁定のため概算に留める。
_WINDOW_BYTES_PER_TOKEN = 2
# 窓由来上限の下限（BUDGET-1 の1件あたり予算の下限と同じ 1024 バイト）。窓が極端に小さいモデルでも
# 検索そのものが機能不能（0バイト）にならないための床。
_WINDOW_DERIVED_MIN_BYTES = 1024


def derive_window_bytes(window_tokens: int) -> int:
    """モデルの実コンテキスト窓（tokens）から「窓由来のツール結果バイト上限」を導く純関数
    （決定的・同じ入力→同じ値・BUDGET-2 §3.4）。

    `available = max(0, window_tokens - _WINDOW_RESERVED_TOKENS)`
    `bytes = max(_WINDOW_DERIVED_MIN_BYTES, available * _WINDOW_SAFETY_FACTOR * _WINDOW_BYTES_PER_TOKEN)`

    負値・0 の `window_tokens` は「窓なし」として扱い、下限値を返す（呼び出し側は "unknown"（窓
    そのものが分からない）と "極小窓"（分かっているが小さい）を区別して渡す——本関数は後者のみ
    対象）。
    """
    wt = max(0, int(window_tokens))
    available = max(0, wt - _WINDOW_RESERVED_TOKENS)
    derived = int(available * _WINDOW_SAFETY_FACTOR * _WINDOW_BYTES_PER_TOKEN)
    return max(_WINDOW_DERIVED_MIN_BYTES, derived)


# ---- 管理画面の登録値（system_settings・段1: 登録値） -----------------------------------------

# system_settings のキー名（値は `{"provider:model": tokens, ...}` の dict）。BUDGET-1 の
# `agentic_budget_per_result`/`agentic_budget_total` と同じ「単一キーに JSONB dict」の流儀
# （`sherpa/store/settings.py` の一般キー/値テーブルにそのまま乗る・スキーマ変更不要）。
MODEL_WINDOWS_KEY = "model_context_windows"
# 登録値の妥当な上限（将来の巨大窓モデルにも余裕を持たせつつ、明らかな誤入力（桁間違い等）を弾く）。
_MAX_REGISTERABLE_TOKENS = 10_000_000


def window_key(provider: str, model: str) -> str:
    """`model_context_windows` の1件のキー（"provider:model"）。"""
    return f"{provider}:{model}"


def registered_window_tokens(provider: str, model: str, system_settings: dict | None) -> int | None:
    """管理画面の登録値（段1）。`system_settings` 省略時は `store.get_system_settings()` を呼ぶ
    （読めない/未設定は None＝次段へ fail-safe・`agentic_search.effective_tool_result_max_bytes` と
    同じ流儀）。"""
    sysset = system_settings
    if sysset is None:
        try:
            from . import store
            sysset = store.get_system_settings()
        except Exception:
            sysset = {}
    table = (sysset or {}).get(MODEL_WINDOWS_KEY)
    if not isinstance(table, dict):
        return None
    v = table.get(window_key(provider, model))
    if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
        return None
    return v


def validate_model_windows(value):
    """`model_context_windows`（管理画面「モデル窓」欄・追加/上書き/削除）の保存値検証。

    `None` は未設定へ戻す（登録値なし＝以後は API/シード/不明段へ）。形式: `{"provider:model": tokens}`。
    provider は `model_catalog.PROVIDERS` のいずれか、model は非空文字列、tokens は
    1〜`_MAX_REGISTERABLE_TOKENS` の整数。不正は `ValueError`（呼び出し側 `sherpa/routers/
    system_extras.py` が 422 へ変換・`model_catalog.validate_catalog` と同じ流儀）。
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise ValueError("model_context_windows はオブジェクトで指定してください")
    from . import model_catalog
    out: dict = {}
    for k, v in value.items():
        if not isinstance(k, str) or ":" not in k:
            raise ValueError(
                f"model_context_windows のキーは 'provider:model' 形式で指定してください: {k!r}")
        provider, _, model = k.partition(":")
        if provider not in model_catalog.PROVIDERS:
            raise ValueError(f"model_context_windows の未知のプロバイダです: {provider}"
                             f"（利用可能: {', '.join(model_catalog.PROVIDERS)}）")
        model = model.strip()
        if not model:
            raise ValueError(f"model_context_windows のモデル名が空です: {k!r}")
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0 or v > _MAX_REGISTERABLE_TOKENS:
            raise ValueError(
                f"model_context_windows[{k}] は1〜{_MAX_REGISTERABLE_TOKENS}の整数（tokens）で"
                "指定してください")
        out[window_key(provider, model)] = v
    return out


# ---- プロバイダAPI照会（段2） -----------------------------------------------------------------

# 照会結果のTTLキャッシュ（失敗＝None も含めてキャッシュする＝落ちているエンドポイントを毎回
# 叩き続けない・fail-safe）。10分＝管理者設定の短TTL(3秒)よりずっと長い——モデル窓は運用中に
# 頻繁には変わらない値のため（`sherpa/store/settings.py::_SYSTEM_SETTINGS_CACHE_TTL` と対比）。
_API_CACHE_TTL_S = 600.0
_api_cache: dict[str, tuple[float, int | None]] = {}
_api_cache_lock = threading.Lock()


def _cached_query(cache_key: str, query_fn) -> int | None:
    now = time.monotonic()
    with _api_cache_lock:
        hit = _api_cache.get(cache_key)
        if hit is not None and now - hit[0] < _API_CACHE_TTL_S:
            return hit[1]
    try:
        result = query_fn()
    except Exception as e:
        _log.info("model_windows: プロバイダAPI照会に失敗（次段へフォールバック）: %s", e)
        result = None
    with _api_cache_lock:
        _api_cache[cache_key] = (now, result)
    return result


def _ollama_chat_suffix() -> str:
    """chat パスの唯一の出所＝`llm.OLLAMA_CHAT_PATH`（SSRF 契約: Ollama REST パスのリテラルは
    チョークポイントの llm.py 以外に置かない・tests/contract/test_ssrf_allowlist.py が走査で固定）。
    遅延 import は本モジュールの「llm 非依存で import 可能」な軽さを保つため。"""
    from . import llm
    return llm.OLLAMA_CHAT_PATH


def derive_ollama_base_url(chat_endpoint: str) -> str | None:
    """`llm.ollama_url(base, "/api/chat")` で組み立てた完全な chat URL から `base` を復元する。

    `agentic_search.openai_style` は Ollama 呼び出し時、組み立て済みの chat URL（`endpoint` 引数）
    しか保持しない——`/api/show`（窓照会）を呼ぶには base が要るため、既知の suffix を取り除くだけの
    決定的な逆変換に留める（`llm.ollama_url` は常に `base.rstrip("/") + path` で構築する・同関数
    docstring 参照）。suffix が一致しない値（テストのダミー URL・将来の path 変更）は None を返し、
    呼び出し側はこの段を安全にスキップする（例外を投げない）。
    """
    if not isinstance(chat_endpoint, str) or not chat_endpoint.endswith(_ollama_chat_suffix()):
        return None
    base = chat_endpoint[: -len(_ollama_chat_suffix())]
    return base or None


def _extract_ollama_context_length(obj) -> int | None:
    """`/api/show` 応答から context 長を取り出す。トップレベル `context_length` があれば優先し、
    無ければ `model_info` 内の `<family>.context_length` 形（Ollama の実際の応答形・family は
    アーキテクチャごとに変わる＝キー名を総当りで探す）の最初の一致を使う。既知の形と一致しなければ
    None（呼び出し側は次段へ fail-safe）。"""
    if not isinstance(obj, dict):
        return None
    top = obj.get("context_length")
    if isinstance(top, int) and top > 0:
        return top
    info = obj.get("model_info")
    if isinstance(info, dict):
        for k, v in info.items():
            if isinstance(k, str) and k.endswith(".context_length") and isinstance(v, int) and v > 0:
                return v
    return None


def query_ollama_context_length(base_url: str, model: str, *,
                                system_settings: dict | None = None) -> int | None:
    """Ollama `/api/show` の `context_length` をライブ照会（段2）。

    接続先は `llm.ollama_url()`（SSRF allowlist の単一チョークポイント）を経由して組み立て、送信は
    `llm.urlopen_no_redirect`（redirect 非追跡の共有 opener）を使う——`llm.py` の既存規律を迂回しない。
    失敗（未許可ホスト・接続不可・タイムアウト・応答形不正 等）は例外を投げず None を返す。
    `_cached_query` により TTL 付きでキャッシュする（`base_url`+`model` がキー）。
    """
    def _do():
        from . import llm
        url = llm.ollama_url(base_url, "/api/show", system_settings=system_settings)
        body = json.dumps({"model": model}).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=llm.JSON_HEADERS)
        with llm.urlopen_no_redirect(req, timeout=5) as r:
            obj = json.loads(r.read().decode("utf-8", "replace"))
        return _extract_ollama_context_length(obj)
    return _cached_query(f"ollama:{base_url}:{model}", _do)


def query_anthropic_context_length(client, model: str) -> int | None:
    """Anthropic Models API（`GET /v1/models/{model}` 相当・SDK の `client.models.retrieve()`）の
    `max_input_tokens` をライブ照会（段2）。

    `client` は `.models.retrieve(model)` を持つ SDK クライアント（`anthropic.Anthropic`・直結の
    Anthropic API 用）を想定する。**本アプリの Claude 接続は現状すべて `anthropic.AnthropicBedrock`
    経由（`sherpa/providers/bedrock.py`）で、この SDK クラスは `.models` を持たない**（Bedrock は
    別の AWS API 体系でモデル情報を持ち、Anthropic の Models API はプロキシしない）——そのため
    この経路は現状常に None を返す（ネットワーク I/O は発生しない・`AttributeError` を握りつぶす
    だけの速い no-op）。直結 Anthropic プロバイダが将来追加された時にコード変更なしで有効化される
    ためのフォワード互換コード（BUDGET-2 §3.4「どのモデルの窓か」参照）。失敗は例外を投げず None。
    """
    def _do():
        m = getattr(client, "models", None)
        if m is None:
            return None
        info = m.retrieve(model)
        val = getattr(info, "max_input_tokens", None)
        return val if isinstance(val, int) and val > 0 else None
    return _cached_query(f"anthropic:{model}", _do)


# ---- コード同梱シード表（段3・OpenAI/Azure 主要モデル） --------------------------------------

# Azure OpenAI は同じ基盤モデルを配信するため同じ値を流用する（provider="openai" で共用・Azure の
# デプロイ名がモデル名と異なる場合はここで解決できず「不明」へ落ちる——その場合は管理画面の登録欄
# で上書きする）。値は各モデルの公表コンテキスト窓（出典: OpenAI 公式モデル一覧
# platform.openai.com/docs/models）。**知らない値はでっち上げない**（BUDGET-2 §3.4）——本アプリの
# モデルカタログ組み込み既定に含まれる "gpt-5.5"/"gpt-5.4-mini" 等は非公開/将来のモデル名のため
# 意図的に未収載（unknown → 管理画面の「窓が未登録です」申告が正しい挙動）。
_OPENAI_SEED_WINDOWS: dict[str, int] = {
    "gpt-4o": 128_000,
    "gpt-4o-mini": 128_000,
    "gpt-4.1": 1_000_000,
    "gpt-4.1-mini": 1_000_000,
    "gpt-4.1-nano": 1_000_000,
    "gpt-4-turbo": 128_000,
    "gpt-3.5-turbo": 16_385,
    "o1": 200_000,
    "o1-mini": 128_000,
    "o3": 200_000,
    "o3-mini": 200_000,
}


def seed_window_tokens(provider: str, model: str) -> int | None:
    """コード同梱シード表（段3）。OpenAI/Azure（provider="openai"）のみ対象（他プロバイダは
    シード対象外＝常に None・モジュール docstring 参照）。"""
    if provider != "openai":
        return None
    return _OPENAI_SEED_WINDOWS.get(model)


# ---- 4段解決 ------------------------------------------------------------------------------

def resolve_window_tokens(provider: str, model: str, *, system_settings: dict | None = None,
                          ollama_base_url: str | None = None, anthropic_client=None
                          ) -> tuple[int | None, str]:
    """モデル窓サイズ（tokens）の4段解決（BUDGET-2 §3.4）:
    登録値(DB) > プロバイダAPI（Ollama `/api/show`・Anthropic Models API） > シード表（OpenAI/Azure）
    > 不明。

    戻り値 `(tokens|None, source)`。`source` は `"registered"`/`"api"`/`"seed"`/`"unknown"`。
    `provider`/`model` が空なら即 `(None, "unknown")`。`ollama_base_url`（provider="ollama" の
    ときのみ意味を持つ）・`anthropic_client`（`.models.retrieve()` を持つクライアント）は段2の
    照会に使う——どちらも省略すればその段は単にスキップされ、次の段へ進む（fail-safe）。
    """
    if not provider or not model:
        return (None, "unknown")
    reg = registered_window_tokens(provider, model, system_settings)
    if reg is not None:
        return (reg, "registered")
    if provider == "ollama" and ollama_base_url:
        v = query_ollama_context_length(ollama_base_url, model, system_settings=system_settings)
        if v is not None:
            return (v, "api")
    if anthropic_client is not None:
        v = query_anthropic_context_length(anthropic_client, model)
        if v is not None:
            return (v, "api")
    seed = seed_window_tokens(provider, model)
    if seed is not None:
        return (seed, "seed")
    return (None, "unknown")
