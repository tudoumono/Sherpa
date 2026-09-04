"""埋め込み（ベクトル）生成＝内部 Elasticsearch kNN 用。

OpenAI / Gemini / Ollama に対応（REST/urllib・SDK 非依存）。**未設定なら None＝ベクトル無効**（ES は BM25 のみで動く）。
次元はプロバイダ既定（OpenAI/Gemini=1536・Ollama=768）。cosine 前提なので正規化は ES 側に任せる。

World固定RAG v2はこの汎用transportのうちOllama/OpenAIだけを厳密なprofileで利用する。
Gemini対応は旧呼出し元のために残すが、RAG v2 profileとしては受理されない。
"""
from __future__ import annotations

import json
import logging
import math
import os
import re
import urllib.error

from . import llm

_BATCH = 50
_ITEM_MAX_UTF8_BYTES = 8_000
_BATCH_MAX_UTF8_BYTES = 240_000
_TIMEOUT = 60
_SAFE_REMOTE_ERROR_FIELD = re.compile(r"[A-Za-z0-9_.-]{1,80}")
# World profileの検索本文契約。providerに依存しない同じwindow/poolingをOllama/OpenAIへ
# 適用し、cache/indexの互換性はprofileにも格納するalgorithm IDで管理する。
EMBEDDING_PREPROCESSING_PROFILE = "evidence-search-text-v1"
EMBEDDING_INPUT_ALGORITHM_ID = "utf8-window-mean-l2-v2"
# プロバイダ → (埋め込みモデル, 次元)。Gemini は gemini-embedding-001（text-embedding-004 は不可な環境あり）。
_MODELS = {"openai": ("text-embedding-3-small", 1536),
           "gemini": ("gemini-embedding-001", 1536),
           "ollama": ("nomic-embed-text", 768)}
# LOG-2（2026-09-03）: 専用ログ（sherpa.embed）へルーティングする（`sherpa/log_setup.py` の登録表参照）。
_log = logging.getLogger("sherpa.embed")


def cfg(settings: dict | None = None, *, system_settings: dict | None = None) -> dict | None:
    """埋め込み設定 `{provider, key/url, model, dim}`（無ければ None）。選択中のクラウドプロバイダ
    （A7）で自動解決する（個人設定のプロバイダ選択は読まない）。A7 を明示選択している場合、その
    プロバイダで解決できなければ Ollama へは倒さず None（クラウドを一度も選んでいない構成の
    ときだけ Ollama を試す・`llm.resolve_auto_provider`/`select_provider` 参照）。

    `SHERPA_DISABLE_EMBED` 環境変数で無効化（テストが実埋め込み API を叩かないための kill-switch）。

    プロバイダを変更すると `_chunk_key`/`_meta` 不一致→`needs_reindex` が次回 sync で索引を
    作り直す（既存機構・`es_index.py:328-352`）。`_MODELS` 次元表は無変更。

    `system_settings`（省略可）: RV2（FBK-1・2026-09-01）呼び出し側が既に読んだスナップショットを
    渡すと、それをそのまま使う（省略時だけ自分で読む）。`es_index.py` は本関数と
    `cloud_selected_but_unavailable()` を同じスナップショットで呼ぶ——別々に読むと、その間の
    admin 更新で「解決できた（旧鍵）が理由判定は不可用（新状態）」のような食い違いが起こりうる。
    """
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return None
    # key/model の解決に使うのと同じ system_settings スナップショットを `select_provider()` へ
    # 明示的に渡し、O() クロージャからも参照できるようにする（openai だけは送信時
    # （`_embed_batch`）にも同じ値を使って接続先を解決するため cfg に含めて持ち出す）。
    # RV1（FBK-1・2026-09-01）: 読取失敗を `{}` へ縮退させない——`{}` は「cloud_provider 未選択」に
    # 化け、`llm.select_provider` の auto 解決が Ollama fallback（`llm.resolve_auto_provider` 参照）
    # を復活させてしまう（読取不能と未選択は別状態）。`graph_extract.available`/`intent_llm._cfg`
    # と同じく、ここでも例外はそのまま呼び出し元へ伝播させる（`sherpa/ingest/worker.py` の
    # `es_index.index_world()` 呼び出しは既に broad except で拾い、削除より前に失敗させる）。
    from . import store
    sys_s = system_settings if system_settings is not None else store.get_system_settings()

    def G(key):
        m, d = _MODELS["gemini"]
        from . import model_catalog
        model = model_catalog.resolve_model("gemini", "embed", None, system_settings=sys_s) or m
        return {"provider": "gemini", "key": key, "model": model, "dim": d}

    def O(key):
        m, d = _MODELS["openai"]
        # Azure OpenAI は `model` にモデル名でなく**デプロイ名**を送る契約（`llm.py` docstring・
        # `_select_provider` の同種ガード参照）。埋め込みも例外ではなく、現場のデプロイ名が
        # `text-embedding-3-small` と違えば固定モデル名のままでは 404 になる。上書き元は
        # `model_catalog`（openai/embed・admin が管理画面で編集。env `OPENAI_EMBED_MODEL` は
        # 初回シード時にカタログへ一度だけ取り込む・以後は env を読まない＝ENV-1 の所有原則）。
        # 次元（1536）は変えない＝Azure 側もこの契約で作られたデプロイであることが前提（次元が違う
        # デプロイを使うと ES 側の kNN mapping と不一致になるが、これは admin の設定ミス側の責務）。
        from . import model_catalog
        model = model_catalog.resolve_model("openai", "embed", None, system_settings=sys_s) or m
        return {"provider": "openai", "key": key, "model": model, "dim": d,
                "system_settings": sys_s}   # `_embed_batch` の接続先解決へそのまま引き継ぐ

    def L(url):
        m, d = _MODELS["ollama"]
        from . import model_catalog
        model = model_catalog.resolve_model("ollama", "embed", None, system_settings=sys_s) or m
        return {"provider": "ollama", "url": url, "model": model, "dim": d}

    # `cloud_provider`（A7）が非空の不正値（env 誤記・旧データ等）のときは、黙って既定（openai）
    # へ倒れたキーで埋め込みを送信しない（fail-closed）。埋め込みは既存の graceful 契約
    # （「埋め込み未設定/失敗時は BM25 のみで索引・vector 検索は degrade」＝呼び出し元の docstring
    # 参照）と同じ扱いに寄せ、strict 判定は本関数の内側だけで完結させる（呼び出し元の変更は
    # 不要＝ es_index.py 側は今までどおり None を「埋め込み未設定」として扱う）。
    from . import keys as _keys
    try:
        return llm.select_provider(settings, openai=O, gemini=G, ollama=L, system_settings=sys_s,
                                   strict=True)
    except _keys.InvalidCloudProviderConfigError:
        _log.warning("embeddings.cfg: cloud_provider の値が不正なため埋め込みを無効化しました")
        return None


def cloud_selected_but_unavailable(system_settings: dict | None = None) -> bool:
    """`cfg()` が None を返した理由が「クラウドを一度も選んでいない（通常の埋め込み未設定＝
    BM25 のみで良い）」でなく「A7 で明示選択したクラウドが解決できない」ことを示すか。

    RV1（FBK-1・2026-09-01）: `es_index.py` はこれで両者を区別し、後者のときは再索引を失敗させる
    （既存索引を BM25-only で上書きしない）・検索は構造化された明示エラーを返す——`cfg() is None`
    だけでは「クラウド未選択」と「選択済みだが認証/接続不可」の区別が付かず、後者を通常の
    graceful degrade（BM25-only）に紛れ込ませてしまう。

    `SHERPA_DISABLE_EMBED`（テスト用 kill-switch）が有効な間は常に偽（意図した運用停止であって
    「選択済みだが使えない」障害ではないため）。cfg() 自体の呼び出しは行わない（呼び出し元が既に
    呼んだ結果と合わせて判定する軽量な追加チェックのみ）。
    """
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return False
    from . import keys as _keys, store
    sys_s = system_settings if system_settings is not None else store.get_system_settings()
    return _keys.cloud_provider_explicitly_selected(sys_s)


def _safe_failure_fields(exc: Exception) -> tuple[int | None, str, str | None]:
    """API失敗から本文・key・header・messageを除いた分類だけを返す。"""
    status = exc.code if isinstance(exc, urllib.error.HTTPError) else None
    error_type = type(exc).__name__
    error_code = None
    if isinstance(exc, urllib.error.HTTPError):
        try:
            payload = json.loads(exc.read())
            error = payload.get("error") if isinstance(payload, dict) else None
            if isinstance(error, dict):
                selected_type = error.get("type")
                selected_code = error.get("code")
                if isinstance(selected_type, str) and _SAFE_REMOTE_ERROR_FIELD.fullmatch(selected_type):
                    error_type = selected_type
                if isinstance(selected_code, str) and _SAFE_REMOTE_ERROR_FIELD.fullmatch(selected_code):
                    error_code = selected_code
        except (OSError, UnicodeError, TypeError, ValueError, json.JSONDecodeError):
            pass
    return status, error_type, error_code


def _log_embed_failure(c: dict, exc: Exception) -> None:
    status, error_type, error_code = _safe_failure_fields(exc)
    _log.warning(
        "embedding request failed: provider=%s status=%s error_type=%s error_code=%s",
        c.get("provider"), status, error_type, error_code,
    )


def _utf8_windows(text: str, *, max_bytes: int = _ITEM_MAX_UTF8_BYTES) -> list[str]:
    """UTF-8文字境界を壊さず、連結すると原文へ戻る決定的なbyte上限window。"""
    if max_bytes <= 0:
        raise ValueError("embedding item byte limit must be positive")
    if len(text.encode("utf-8")) <= max_bytes:
        return [text]
    windows: list[str] = []
    characters: list[str] = []
    byte_count = 0
    for character in text:
        character_bytes = len(character.encode("utf-8"))
        if characters and byte_count + character_bytes > max_bytes:
            windows.append("".join(characters))
            characters = []
            byte_count = 0
        characters.append(character)
        byte_count += character_bytes
    if characters:
        windows.append("".join(characters))
    return windows


def _window_batches(texts: list[str]):
    """元入力index付きwindowをAPIの件数・総UTF-8 byte上限内へまとめる。"""
    origins: list[int] = []
    batch: list[str] = []
    batch_bytes = 0
    for input_index, text in enumerate(texts):
        for window in _utf8_windows(text):
            window_bytes = len(window.encode("utf-8"))
            if batch and (len(batch) >= _BATCH or batch_bytes + window_bytes > _BATCH_MAX_UTF8_BYTES):
                yield origins, batch
                origins, batch, batch_bytes = [], [], 0
            origins.append(input_index)
            batch.append(window)
            batch_bytes += window_bytes
    if batch:
        yield origins, batch


def _provider_batches(texts: list[str], provider: str):
    """RAG v2 providersへ同じ決定的byte window契約を適用する。"""
    if provider in {"openai", "ollama"}:
        yield from _window_batches(texts)
        return
    for offset in range(0, len(texts), _BATCH):
        batch = texts[offset:offset + _BATCH]
        yield list(range(offset, offset + len(batch))), batch


def is_valid_vector(vector: object, dimension: int) -> bool:
    """dense vectorとして安全な、有限・非zeroの数値listだけを受理する。"""
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
        return False
    if not isinstance(vector, list) or len(vector) != dimension:
        return False
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        selected = float(value)
        if not math.isfinite(selected):
            return False
        values.append(selected)
    norm_squared = math.fsum(value * value for value in values)
    return math.isfinite(norm_squared) and norm_squared > 0


def _mean_l2(vectors: list[list], dimension: int) -> list[float] | None:
    if not vectors or any(not is_valid_vector(vector, dimension) for vector in vectors):
        return None
    try:
        mean = [math.fsum(float(vector[index]) for vector in vectors) / len(vectors) for index in range(dimension)]
        norm_squared = math.fsum(value * value for value in mean)
    except (OverflowError, ValueError):
        return None
    if not math.isfinite(norm_squared) or norm_squared <= 0:
        return None
    norm = math.sqrt(norm_squared)
    pooled = [value / norm for value in mean]
    return pooled if is_valid_vector(pooled, dimension) else None


def _embed_batch(texts: list, c: dict) -> list | None:
    from . import metering
    try:
        if c["provider"] == "openai":
            # `cfg()` が渡した snapshot（`c["system_settings"]`）で接続先を解決する（省略時 None は
            # `llm.py` が都度読み直す従来どおりの挙動）。送信は `llm.openai_post_json`（OpenAI 専用の
            # 送信直前ガード付き・`post_json` は Gemini/Ollama とも共用のため一律遮断しない）。
            _sys_s = c.get("system_settings")
            r = llm.openai_post_json(llm.openai_url("embeddings", system_settings=_sys_s),
                              llm.openai_headers(c["key"], system_settings=_sys_s),
                              {"model": c["model"], "input": texts, "dimensions": c["dim"]}, _TIMEOUT)
            metering.acc_add(metering.usage_from_openai_embed(r))
            data = r.get("data", [])
            if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
                return None
            # OpenAIの応答は各vectorに入力位置 ``index`` を持つ。HTTP応答順へ暗黙依存すると、
            # 順序が入れ替わった場合に別本文のvectorをchunkへ結び付けてしまうため、indexで復元する。
            # 古いmock/provider互換として全件index無しだけは受信順を維持し、一部だけ欠ける応答は拒否する。
            indices = [item.get("index") for item in data]
            if all(isinstance(index, int) and not isinstance(index, bool) for index in indices):
                if sorted(indices) != list(range(len(texts))):
                    return None
                data = sorted(data, key=lambda item: item["index"])
            elif any(index is not None for index in indices):
                return None
            return [d["embedding"] for d in data]
        if c["provider"] == "gemini":
            reqs = [{"model": f"models/{c['model']}", "content": {"parts": [{"text": t}]},
                     "outputDimensionality": c["dim"]} for t in texts]
            r = llm.post_json(llm.gemini_url(c["model"], "batchEmbedContents"),
                              llm.gemini_headers(c["key"]), {"requests": reqs}, _TIMEOUT)
            metering.acc_add(None)   # batchEmbedContents は usage フィールドを返さない＝報告不能マーカー
            return [e["values"] for e in r.get("embeddings", [])]
        if c["provider"] == "ollama":
            # ローカル/allowlist済みOllamaの文書・質問をambient HTTP(S)_PROXYへ渡さない。
            # OpenAI/Geminiは通常の企業proxy利用を維持する。
            with llm.no_proxy_requests():
                r = llm.post_json(llm.ollama_url(c["url"], "/api/embed"), llm.JSON_HEADERS,
                                  {"model": c["model"], "input": texts}, _TIMEOUT)
            metering.acc_add(metering.usage_from_ollama_embed(r))
            return r.get("embeddings")
        return None
    except Exception as exc:
        _log_embed_failure(c, exc)
        return None


def embed(texts: list, c: dict, *, user_id: str | None = None, world: str | None = None) -> list | None:
    """テキスト群 → ベクトル群（順序対応）。**一部でも失敗したら None**（ベクトル無効＝BM25 へ）。

    S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: バッチループ全体を
    `metering.acc_begin()`/`acc_end()` で囲み、`kind='embed'` で1回の呼び出しにつき1行記録する
    （calls＝HTTP が応答を返したバッチの数・None を返す場合でも記録する＝トークンは消費済み）。
    """
    if not isinstance(c, dict) or not texts or not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        return None
    dimension = c.get("dim")
    provider = c.get("provider")
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0 or not isinstance(provider, str):
        return None
    from . import metering
    metering.acc_begin()
    try:
        grouped: list[list[list]] = [[] for _text in texts]
        try:
            batches = _provider_batches(texts, provider)
            for origins, batch in batches:
                vecs = _embed_batch(batch, c)
                if not isinstance(vecs, list) or len(vecs) != len(batch):
                    return None
                for origin, vector in zip(origins, vecs):
                    if not is_valid_vector(vector, dimension):
                        return None
                    grouped[origin].append(vector)
        except (TypeError, UnicodeError, ValueError):
            return None
        out: list = []
        for vectors in grouped:
            if not vectors:
                return None
            if len(vectors) == 1:                    # 短文はv1とbyte-identicalなvectorを維持
                out.append(vectors[0])
                continue
            pooled = _mean_l2(vectors, dimension)
            if pooled is None:
                return None
            out.append(pooled)
        return out
    finally:
        tokens, n = metering.acc_end()
        if n:
            metering.record("embed", c["provider"], c["model"], tokens,
                            user_id=user_id, world=world, calls=n)
