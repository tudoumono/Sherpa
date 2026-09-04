"""`BedrockProvider`（リファクタリング計画 フェーズ5 S7・`sherpa/agents.py` から純移動）。

AWS Bedrock（Claude）の頭脳。`_GenProvider`（base.py）を継承し、`_agentic_loop`/`_stream`/`_complete`/
`probe` を実装する（他プロバイダと違い、接続テスト用の `probe` を持つ）。`sherpa/agents.py` が facade
として本モジュールから再エクスポートする。`_select_provider` は **S11 で
`sherpa/providers/__init__.py` へ移動済み**で、そこから `_bedrock_auth_available(...)`/
`BedrockProvider(...)` を facade 属性経由で実行時解決して呼ぶ（下の地雷2参照）。

移動に伴い相対 import の深さが1段増える（`sherpa/agents.py` → `sherpa/providers/bedrock.py`）ため
`from . import agentic_search` は `from .. import agentic_search` に変更した（挙動は不変・参照先
モジュールは変わらない）。`_log` は base.py（`getLogger("sherpa")`）から直接 import する（patch 対象
ではない・危険な継ぎ目リストに無い）。

**地雷1（`import anthropic` の属性解決・`tests/unit/test_bedrock_provider.py::
test_get_client_builds_anthropic_bedrock_not_mantle` が固定）**: 本モジュールは `import anthropic`
のみを行い、`_get_client` は `anthropic.AnthropicBedrock(...)` と**呼び出し時に属性解決**する
（`from anthropic import AnthropicBedrock` へは変更しない）。`monkeypatch.setattr(anthropic,
"AnthropicBedrock", _FakeSDKClient)` は `anthropic` という共有モジュールオブジェクトの属性を
書き換えるため、どのモジュールが `import anthropic` していても同じオブジェクトを指す
（`urllib.request` と同型の理由・S5 の教訓 #11 参照）。`probe` の `except anthropic.RateLimitError`
等の例外クラス参照も同様に属性解決のまま（元コードのフレーム不変）。`agents.py`（facade）側の
`import anthropic` は本 S7 で参照が無くなったため削除した（golden は `inspect.ismodule()` を
除外するため `anthropic` は元々対象外＝削除しても golden に影響しない）。

**地雷2（`_bedrock_auth_available`/`BedrockProvider` は「危険な継ぎ目」＝呼び出し側で facade
実行時解決が必要）**: S7 時点では呼び出し元 `_select_provider` が agents.py 側に残っていたため
モジュールグローバル解決で足りていたが、**S11 で `_select_provider` は
`sherpa/providers/__init__.py` へ移動済み**であり、そこでは計画書のシーム規則どおり
関数内 `from sherpa import agents as _facade` で `_facade._bedrock_auth_available`/
`_facade.BedrockProvider`（他の全 Provider クラスも同様）を**実行時解決している**。
この facade 経由参照は `tests/unit/test_agents_seams.py`（facade patch の介入 pin）と
`tests/unit/test_health.py`（`agents.BedrockProvider = Fake` 直接代入）が検知器のため、
「不要になった」と誤解して直接 import へ戻さないこと（RV LOW 2026-07-14 で本注記を最終状態に更新）。

`_anthropic_usage_raw` は本モジュールに実体を置く（`agents.py` は facade 経由の再エクスポート）。
"""
from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from pathlib import Path
from typing import Iterator

import anthropic          # AWS Bedrock（Claude）プロバイダ用 SDK（requirements: anthropic[bedrock]）

from .base import _CompletionState, _GenProvider, _can_ask, _log, _usage_meta


def _anthropic_usage_raw(u) -> dict:
    """Anthropic/Bedrock の usage（input_tokens は cache 分を除く）→ 生トークン（cached ⊆ input へ正規化）。

    Anthropic は `input_tokens` にキャッシュ読取/作成を含めないため、標準形（cached ⊆ input）に合わせて
    `input_tokens = 素の入力 + cache_read + cache_creation`、`cached_input_tokens = cache_read` とする
    （cache_creation は input 率で概算・cache_read は cached 率で概算）。SDK オブジェクト/dict の両対応。"""
    def _g(key):
        if isinstance(u, dict):
            return u.get(key)
        return getattr(u, key, None)
    base = _g("input_tokens") or 0
    read = _g("cache_read_input_tokens") or 0
    creation = _g("cache_creation_input_tokens") or 0
    return {"input_tokens": (base or 0) + (read or 0) + (creation or 0),
            "cached_input_tokens": read or 0,
            "output_tokens": _g("output_tokens") or 0,
            "reasoning_output_tokens": 0}


# ---- AWS Bedrock（Claude）----
# 認証は Bedrock コンソール発行の API キー（Bearer・長期/短期）が主。設定 `bedrock_api_key`（per-user）→
# 未設定なら SDK が env `AWS_BEARER_TOKEN_BEDROCK`／`ANTHROPIC_AWS_API_KEY` → SigV4(AWS_ACCESS_KEY_ID 等/プロファイル)
# の順に解決する（この解決順は SDK 任せ）。
# 経路は bedrock-runtime（`anthropic.AnthropicBedrock`）＋推論プロファイルID（2026-07 切替・実アカウントで
# Mantle 直接枠が無く 403 のため）。モデルIDは `jp.`/`global.` 等のプロファイル接頭辞＋バージョン付き。
_BEDROCK_MODEL = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
_BEDROCK_MAX_TOKENS = 64000           # ストリーミング応答の上限（回答生成用。SDK はこの大きさでも stream で安全）
_BEDROCK_ENV_KEYS = ("AWS_BEARER_TOKEN_BEDROCK", "ANTHROPIC_AWS_API_KEY")

# 選択可能なモデル（設定画面の <select> の既定表示＋ PUT /settings の allowlist 検証の一方）。
# (id, 表示ラベル) のタプル。既定は先頭＝ _BEDROCK_MODEL と一致させる。
# 静的な最小フォールバック（実アカウントで確認済みの Haiku 4.5 のみ）。2026-07 S6 以降は
# `GET /settings/bedrock-models`（`list_bedrock_inference_profiles` 参照）でアカウントが実際に
# 使える推論プロファイルを動的取得できる＝ID を推測でここに追加しない（400/403 の温床・実績あり）。
BEDROCK_MODEL_CHOICES = [
    ("jp.anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5（JP 推論プロファイル・既定）"),
    ("global.anthropic.claude-haiku-4-5-20251001-v1:0", "Claude Haiku 4.5（Global 推論プロファイル）"),
]

# 推論プロファイル ID の形式（PUT /settings が動的取得由来の ID も許可するための緩和・2026-07 S6）。
# 静的 BEDROCK_MODEL_CHOICES に無くても、この形式に一致すれば保存を許可する（自由文字列は依然通さない）。
BEDROCK_MODEL_ID_RE = re.compile(r"^(jp|global|apac|us|eu)\.anthropic\.[A-Za-z0-9.\-]+-v\d+:\d+$")


def _bedrock_region() -> str:
    """Bedrock の region は東京（ap-northeast-1）に固定する。

    ユーザーが誤ったリージョンを設定して 401/レイテンシ事故を踏んだ経緯があるため、設定値・env
    `AWS_REGION` は一切参照しない（2026-07 決定）。**変更する場合はここ1箇所だけでよい**。
    """
    return "ap-northeast-1"


def _bedrock_runtime_base_url() -> str:
    """bedrock-runtime の正準 URL（東京固定 region から機械的に導出）。

    `AnthropicBedrock` は `base_url` 省略時、まず env `ANTHROPIC_BEDROCK_BASE_URL` を読み、
    無ければこの同じ式で既定値を組み立てる（SDK 内部実装）。`.env` は起動スクリプトが
    全キーを export する構成のため、無関係な env に紛れ込んだ `ANTHROPIC_BEDROCK_BASE_URL`
    がこの既定値を静かに上書きしうる。`_get_client` がこの関数の戻り値を
    `base_url` として明示的に渡すことで、env による上書きを封じる。
    """
    return f"https://bedrock-runtime.{_bedrock_region()}.amazonaws.com"


def _bedrock_auth_available(api_key: str | None = None) -> bool:
    """Bedrock が使える認証が解決できそうか（渡された API キー、または SigV4 の静的手掛かり）。

    `api_key` は呼び元が `sherpa.keys.resolve_api_key("bedrock", ...)`（中央/個人・A7 排他込み）で
    解決してから渡す。**この関数はもう env の Bearer キー
    （`_BEDROCK_ENV_KEYS`）を読まない**＝env はシード専用という所有原則に合わせる。SigV4
    （`AWS_ACCESS_KEY_ID`/`AWS_PROFILE`/`~/.aws/credentials`）は Sherpa の設定ガバナンス対象外の
    インフラ管理チェーンのため引き続き手掛かりとして見る（呼び出し側が bedrock 未選択なら
    そもそもこの関数を呼ばない・A7 ゲートは呼び出し側の責務）。
    **ネットワークに一切出ない**判定に限定する（boto3 の get_credentials は IMDS へ外向き通信し
    得るため使わない＝health._ping_bedrock と同方針）。ここは事前ゲートにすぎず、実際の認証解決は
    SDK がリクエスト時に行う。
    """
    if api_key:
        return True
    if os.environ.get("AWS_ACCESS_KEY_ID") or os.environ.get("AWS_PROFILE"):
        return True
    return (Path.home() / ".aws" / "credentials").exists()


def _bedrock_error_detail(e: Exception) -> str:
    """anthropic 例外を短い理由文へ整形する（**未マスク・未切断のまま返す**＝秘密の伏せ字・長さ調整は
    呼び出し元の `_safe_bedrock_detail` が担う。呼び出し元はここを直接使わず `_safe_bedrock_detail`
    を経由すること）。既存プロバイダの整形に倣う（status_code/message）。"""
    sc = getattr(e, "status_code", None)
    msg = getattr(e, "message", None) or str(e)
    return (f"{sc}: {msg}" if sc else f"{type(e).__name__}: {msg}").strip()


_BEDROCK_LIST_TIMEOUT = 10.0   # control-plane 列挙は設定画面のボタン押下待ち＝短めに（S6）
_BEDROCK_PROFILE_REGION_LABEL = {"jp": "JP", "global": "Global", "apac": "APAC", "us": "US", "eu": "EU"}


def _resolve_bedrock_bearer_key(api_key: str | None) -> str | None:
    """列挙に使う Bearer キーを解決（呼び出し元が渡した解決済みキーのみ・env は読まない）。

    control-plane の列挙は Bearer ヘッダの素の HTTP 呼び出しで SDK を経由しない＝
    SigV4 署名は組み立てられない。SigV4 のみで運用している構成では列挙不可
    （フロントは既定の静的 `BEDROCK_MODEL_CHOICES` にフォールバックする）。
    `api_key` は既に `sherpa.keys.resolve_api_key("bedrock", ...)` で解決済み（呼び出し元の責務）
    のため、ここで env（`_BEDROCK_ENV_KEYS`）へフォールバックはしない（所有原則違反になる）。
    """
    return api_key


def _bedrock_profile_label(profile_id: str, name: str) -> str:
    prefix = profile_id.split(".", 1)[0]
    region = _BEDROCK_PROFILE_REGION_LABEL.get(prefix, prefix)
    return f"{name or profile_id}（{region} 推論プロファイル）"


def _is_anthropic_profile(profile_id: str) -> bool:
    parts = profile_id.split(".", 1)
    return len(parts) == 2 and parts[1].startswith("anthropic.")


_BEARER_RE = re.compile(r"Bearer\s+\S+", re.IGNORECASE)


def _redact_bedrock_secret(text: str, key: str | None) -> str:
    """万一 key 値や `Bearer <...>` パターンが文字列に混ざっていても伏せる（二重防御）。

    RV MEDIUM（2026-07-03）→ RV HIGH（2026-07-03再検証）: 上流/プロキシのエラー本文をそのまま
    返すのは危険（誤ってキーが echo される・プロキシのエラーページがリクエストヘッダを含める等）
    なので、`list_bedrock_inference_profiles` は原則この関数を経由しない固定文言のみを返すが、
    将来の変更で生テキストを混ぜてしまっても最後の砦になるよう用意する。
    `key`（呼出元が明示的に使った値・per-user 保存キー等）だけでなく、**サーバ側 env のキー値**
    （`_BEDROCK_ENV_KEYS`＝`AWS_BEARER_TOKEN_BEDROCK`/`ANTHROPIC_AWS_API_KEY`）も伏せる＝
    `BedrockProvider(api_key=None)` で env/SigV4 チェーンに委譲したケース（呼出元は「どのキーが
    実際に使われたか」を知らない）でも、SDK 例外メッセージに env キー値が混入していれば伏せられる。
    """
    for secret in (key, *(os.environ.get(k) for k in _BEDROCK_ENV_KEYS)):
        if secret:
            text = text.replace(secret, "[REDACTED]")
    return _BEARER_RE.sub("Bearer [REDACTED]", text)


_BEDROCK_DETAIL_MAX_LEN = 400   # _bedrock_error_detail 相当の理由文の表示上限


def _safe_bedrock_detail(e: Exception, *, key: str | None = None) -> str:
    """Bedrock（Anthropic SDK）例外 → 利用者向けの安全な理由文。**マスクしてから切断する順序を
    ここで保証する**（`_bedrock_error_detail` 自体は長さを調整しない未加工の文字列を返す契約・
    先に切断すると切断境界をまたいだ秘密の断片が未マスクのまま残るため）。`key`（呼び出しで明示的に
    使ったキー）が None でも、`_redact_bedrock_secret` が env キー（`AWS_BEARER_TOKEN_BEDROCK` 等・
    SDK が env/SigV4 チェーンへ委譲したケース）を必ず確認する。

    `BedrockProvider.probe` の全例外節・`graph_extract._error_detail` の Anthropic 委譲分岐・
    `sherpa/routers/system.py` の Bedrock 接続テスト/モデル検証は、失敗理由を外部へ返す前に
    必ずここを経由する（`_bedrock_error_detail`/`_redact_bedrock_secret` を個別に組み合わせない）。
    """
    text = _redact_bedrock_secret(_bedrock_error_detail(e), key)
    return text[:_BEDROCK_DETAIL_MAX_LEN]


def _bedrock_list_error_message(status: int | None) -> str:
    """HTTP status だけから固定の日本語文言を組み立てる（上流本文の生文字列は使わない・RV MEDIUM）。

    上流/プロキシのエラー本文には（誤ってヘッダが echo される等で）キーが混ざるリスクがあるため、
    メッセージの中身に一切取り込まない＝status コードのみを条件分岐の入力にする。
    """
    if status in (401, 403):
        return f"認証エラー（{status}）。API キー/権限を確認してください。"
    if status == 429:
        return "リクエストが多すぎます（429）。しばらく待って再試行してください。"
    if status is not None:
        return f"取得に失敗しました（HTTP {status}）。"
    return "接続できませんでした（ネットワークエラー）。"


def list_bedrock_inference_profiles(api_key: str | None) -> tuple[list[dict], str | None]:
    """アカウントが実際に使える Bedrock 推論プロファイルを control-plane から列挙し、ACTIVE な
    anthropic 系のみ `[{"id","label"}]` で返す。

    推論プロファイル ID を推測でハードコードするのは 400/403 の温床（実績あり）なので、
    ユーザー要望のモデル追加（Sonnet 4.6 / Opus 4.8 / Opus 4.6 等）はこの動的取得の結果に
    出てくれば選べる形にする（2026-07 S6）。失敗（キー無し/403/ネットワーク）は例外を投げず
    `([], "<短い日本語理由>")` を返す（呼び出し側の `GET /settings/bedrock-models` が 200 のまま
    返せるようにするため・設定画面の UX を壊さない）。エラー文言は HTTP status ベースの固定日本語
    文言のみ（上流のエラー本文は読んでも返さない・RV MEDIUM 2026-07-03: プロキシ等がリクエスト
    ヘッダを本文に echo するとキーが漏れ得るため）。
    """
    key = _resolve_bedrock_bearer_key(api_key)
    if not key:
        return [], "Bedrock の API キーが未設定です（設定でキーを保存するか、サーバ側 env を設定してください）"
    region = _bedrock_region()
    url = f"https://bedrock.{region}.amazonaws.com/inference-profiles"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=_BEDROCK_LIST_TIMEOUT) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # RV「バッチ2」1番（2026-07-03）: 実環境で「接続テストOK・モデル取得は失敗」の報告あり
        # （容疑: Bearer キーが runtime=InvokeModel 専用で control-plane=ListInferenceProfiles 権限が
        # 無い＝403）。原因調査のため status だけをログに残す（本文/キーは出さない・上の
        # _bedrock_list_error_message と同じ理由）。
        _log.warning("bedrock list_inference_profiles failed: HTTP %s", e.code)
        return [], _redact_bedrock_secret(_bedrock_list_error_message(e.code), key)
    except Exception as e:
        _log.warning("bedrock list_inference_profiles failed: %s", type(e).__name__)
        return [], _redact_bedrock_secret(_bedrock_list_error_message(None), key)

    profiles = data.get("inferenceProfileSummaries") or []
    out = []
    for p in profiles:
        pid = p.get("inferenceProfileId") or ""
        if p.get("status") != "ACTIVE" or not _is_anthropic_profile(pid):
            continue
        out.append({"id": pid, "label": _bedrock_profile_label(pid, p.get("inferenceProfileName") or "")})
    return out, None


def _bedrock_text(resp) -> str:
    """非ストリーミング応答 `response.content` のブロックから text だけを連結する（type=="text" のみ）。"""
    return "".join(getattr(b, "text", "") for b in (getattr(resp, "content", None) or [])
                   if getattr(b, "type", None) == "text").strip()


class BedrockProvider(_GenProvider):
    """AWS Bedrock 上の Claude（Anthropic SDK・bedrock-runtime クライアント／推論プロファイル）を頭脳に。

    OpenAIProvider と同じ立ち位置＝取得（Neo4j/grep）は本物＋事実を渡して**根拠つき回答をストリーミング**、
    qa/troubleshoot は反復ツール検索（`anthropic_style`）。**temperature/top_p/top_k/thinking は送らない**
    （400 になり得るため）・プレフィル無し・`max_tokens` 必須。クライアントは遅延生成。
    """
    label = "AWS Bedrock (Claude)"
    provider_id = "bedrock"
    # EV-0（拡張設計 §4.4）: Anthropic/Bedrock の自然完了理由（`stop_reason`）。
    _natural_completion_reasons = frozenset({"end_turn", "stop_sequence"})

    def __init__(self, region: str | None = None, model: str | None = None, api_key: str | None = None,
                 *, max_retries: int | None = None):
        super().__init__()
        # `region` は呼び出し側の互換のため受け取るが、常に無視する（`_bedrock_region` 参照）。
        self._region = _bedrock_region()
        self.model = model or _BEDROCK_MODEL
        self._api_key = api_key or None                  # None なら SDK の env/SigV4 チェーンに委譲
        # `max_retries`（省略可・既定は SDK 既定＝2）: 診断用途（`health._ai_check_bedrock` の
        # doctor 呼び出し）が「1回だけ実際に HTTP を送る」ことを保証したい場合に `0` を明示できる
        # ようにする（SDK 既定のリトライは実 HTTP 送信回数を「呼び出し回数」と食い違わせる）。
        # 省略時（`None`）は SDK 既定のまま＝既存の呼び出し元（chat/qa 等）の挙動は変えない。
        self._max_retries = max_retries
        self._client = None

    def _get_client(self):
        if self._client is None:                         # 遅延生成（構築は creds 未解決でも落ちない＝呼び出し時に判定）
            # `base_url` を明示する: 省略すると SDK が env
            # `ANTHROPIC_BEDROCK_BASE_URL` を読んで接続先を上書きできてしまう
            # （`_bedrock_runtime_base_url` docstring 参照）。ここで正準 URL を渡すことで
            # SDK 側の env フォールバックへ進ませず、Bearer 資格情報の送信先を東京固定にする。
            kwargs = {"api_key": self._api_key, "aws_region": self._region,
                      "base_url": _bedrock_runtime_base_url()}
            if self._max_retries is not None:
                kwargs["max_retries"] = self._max_retries
            self._client = anthropic.AnthropicBedrock(**kwargs)
        return self._client

    def _agentic_loop(self, ctx):
        from .. import agentic_search
        # SC-6e: ターン先頭の可用性 snapshot と希望の実効集合（要求∩可用）から SYSTEM を
        # 組み立てる（OpenAIProvider._agentic_loop と同じ理由）。
        _tools_pref = (ctx.scope_meta or {}).get("tools")
        _eff_tools = agentic_search.effective_tools_pref(_tools_pref, ctx.tools_availability)
        sys = (self.system_prompt + "\n\n" if self.system_prompt else "") + \
            agentic_search.system_prompt(_eff_tools)
        return agentic_search.anthropic_style(
            self._get_client(), self.model, sys, ctx.message, ctx.world,
            (ctx.scope_meta or {}).get("scope_paths"), stop_event=ctx.stop_event,
            can_ask=_can_ask(ctx.message), history=ctx.history or [],
            layer=(ctx.scope_meta or {}).get("layer"),
            tools_pref=_tools_pref, tools_availability=ctx.tools_availability)

    def _stream(self, prompt: str, completion: _CompletionState | None = None) -> Iterator[str]:
        """回答テキストをストリーミングで yield（system は Anthropic の system パラメータへ）。

        R1a: `self._history`（上流でキャップ済み・空なら従来と完全同一）を user メッセージの前に並べる。
        """
        client = self._get_client()
        kwargs = {"model": self.model, "max_tokens": _BEDROCK_MAX_TOKENS,
                  "messages": self._history + [{"role": "user", "content": prompt}]}
        if self.system_prompt:
            kwargs["system"] = self.system_prompt        # 本文テキストのみ送信（CLAUDE.md）
        with client.messages.stream(**kwargs) as stream:
            for text in stream.text_stream:
                if text:
                    yield text
            # F3: ストリーム完了後に最終メッセージの usage を拾う（cache_read/creation は input に畳み込む）。
            # EV-0（拡張設計 §4.4）: 同じ最終メッセージから完了理由（"end_turn"／
            # "stop_sequence"＝自然完了・"max_tokens"＝打ち切り 等）も拾う。`get_final_message()`
            # が例外を投げたら usage 同様どちらも取得しない——`completion.terminal_seen` を
            # **立てない**（fail-open で None のまま「取得できた」ことにしない・呼び出し元は
            # 未完了として扱う（取得失敗を「終端を観測できた」と偽装しない fail-closed 契約）。
            try:
                final = stream.get_final_message()
                self._last_usage = _usage_meta(self.provider_id, self.model,
                                               **_anthropic_usage_raw(getattr(final, "usage", None)))
                if completion is not None:
                    completion.terminal_seen = True
                    completion.reason = getattr(final, "stop_reason", None)
            except Exception:
                pass

    def _complete(self, prompt: str, max_tokens: int | None = None) -> str:
        """非ストリーミングの単発補完（`response.content` の text ブロックを連結して返す）。

        R1a: `self._history` を user メッセージの前に並べる（`_stream` と同じ・空なら従来と完全同一）。
        """
        client = self._get_client()
        kwargs = {"model": self.model, "max_tokens": max_tokens or _BEDROCK_MAX_TOKENS,
                  "messages": self._history + [{"role": "user", "content": prompt}]}
        if self.system_prompt:
            kwargs["system"] = self.system_prompt
        return _bedrock_text(client.messages.create(**kwargs))

    def _attribute(self, text: str, digest: str, ev_map: dict, call_budget=None) -> set:
        from .. import agentic_search
        return agentic_search.attribute_anthropic(
            self._get_client(), self.model, _BEDROCK_MAX_TOKENS, text, digest, ev_map,
            call_budget=call_budget)

    def probe(self, timeout: float | None = None, max_tokens: int = 16) -> tuple[bool, str]:
        """接続テスト用に最小リクエストを送る（既定 max_tokens=16）。`(ok, detail)` を返す。

        実際に送信される HTTP リクエストの回数は `max_retries`（コンストラクタ引数・省略可）に
        従う: 省略時（既定）は SDK 既定のリトライ（`AnthropicBedrock` 既定＝2・タイムアウトや
        5xx 等リトライ可能なエラーでは実 HTTP 送信が最大3回になりうる）のまま。「呼び出し1回＝
        実送信1回」を保証したい呼び出し元（`sherpa.health._ai_check_bedrock` 経由の doctor 診断
        ツール等）は、コンストラクタへ `max_retries=0` を明示する（`BedrockProvider.__init__`
        docstring 参照）。

        失敗理由は `_safe_bedrock_detail`（マスクしてから切断する安全境界・明示キー`self._api_key`と
        env キーの両方を伏せる）を経由する＝例外の種類を問わず一括で守る。
        `timeout`（省略時は SDK既定＝数分オーダー）: システム状態画面の「再チェック」（health.py）は
        短く（既定8s・`SHERPA_HEALTH_AI_TIMEOUT`）指定して1プローブが全体をブロックしないようにする
        （RV HIGH・2026-07-03）。`max_tokens`: モデルID の検証つき手動追加（バッチ2 1番・2026-07-03）は
        1（`POST /settings/bedrock-models/verify`）を指定してコスト/レイテンシを最小化する。
        """
        try:
            client = self._get_client()
            kwargs = {"model": self.model, "max_tokens": max_tokens,
                      "messages": [{"role": "user", "content": "ping"}]}
            if timeout is not None:
                kwargs["timeout"] = timeout
            client.messages.create(**kwargs)
            return True, ""
        except Exception as e:
            return False, _safe_bedrock_detail(e, key=self._api_key)
