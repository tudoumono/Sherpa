"""埋め込み（ベクトル）生成（内部 Elasticsearch の kNN 用）。OpenAI（Azure OpenAI を含む）／Ollama に REST（urllib）で送る。
未設定なら None＝ベクトル無効（ES は BM25 のみで動く）。cosine 前提で正規化は ES 側に任せる。
設計: docs/design/rag.md「埋め込み」
"""
from __future__ import annotations

import concurrent.futures
import contextvars
import json
import logging
import math
import os
import re
import socket
import time
import urllib.error

from . import llm

_BATCH = 50
_ITEM_MAX_UTF8_BYTES = 8_000
_BATCH_MAX_UTF8_BYTES = 240_000
_TIMEOUT = 60
_SAFE_REMOTE_ERROR_FIELD = re.compile(r"[A-Za-z0-9_.-]{1,80}")
# 並列度（システム設定 `embed_parallel`）の既定・範囲。env フォールバックは持たない
EMBED_PARALLEL_DEFAULT = 4
EMBED_PARALLEL_MIN = 1
EMBED_PARALLEL_MAX = 16
# 429/5xx と一時的な通信エラーだけ再送する（429 以外の 4xx は再送しない）
_RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})
_RETRY_MAX_RETRIES = 5  # 最初の送信を含めない再送回数
_RETRY_BACKOFF_SCHEDULE = (1, 2, 4, 8, 16)
_RETRY_MAX_TOTAL_SECONDS = 300
_RETRY_AFTER_MAX_SECONDS = 60
# 検索本文契約: provider に依存しない同じ window／pooling を適用する。cache／index の互換性は algorithm ID で管理する
EMBEDDING_INPUT_ALGORITHM_ID = "utf8-window-mean-l2-v2"
# プロバイダ → (埋め込みモデル, 次元)
_MODELS = {"openai": ("text-embedding-3-small", 1536),
           "ollama": ("nomic-embed-text", 768)}
# 埋め込みの接続先（システム設定 `embed_provider`）。"auto"＝回答用に選んだクラウドに従う／"ollama"＝中央 Ollama を使う
EMBED_PROVIDERS = ("auto", "ollama")
EMBED_PROVIDER_DEFAULT = "auto"
# Ollama の埋め込みモデル名（タグ除く）→ 次元。表に無いモデルは `ollama_embed_dim` が初回に実測する（ES の次元と合わせるため推測しない）
_OLLAMA_KNOWN_DIMS = {"nomic-embed-text": 768, "bge-m3": 1024, "mxbai-embed-large": 1024,
                      "bge-large": 1024, "all-minilm": 384, "embeddinggemma": 768}
_OLLAMA_DIM_PROBE_TIMEOUT = 15
_OLLAMA_DIM_FAIL_TTL = 60.0
_ollama_dim_cache: dict = {}  # (url, model) -> 実測次元
_ollama_dim_failed: dict = {}  # (url, model) -> 失敗時刻
# 専用ログ（sherpa.embed）
_log = logging.getLogger("sherpa.embed")


def effective_embed_parallel(system_settings: dict | None) -> int:
    """埋め込み HTTP 送信の並列度。システム設定 `embed_parallel`（1〜16）を使い、未設定・範囲外・非整数は既定 4。"""
    configured = system_settings.get("embed_parallel") if isinstance(system_settings, dict) else None
    if isinstance(configured, bool) or not isinstance(configured, int):
        return EMBED_PARALLEL_DEFAULT
    if configured < EMBED_PARALLEL_MIN or configured > EMBED_PARALLEL_MAX:
        return EMBED_PARALLEL_DEFAULT
    return configured


def effective_embed_provider(system_settings: dict | None) -> str:
    """埋め込みの接続先（`embed_provider`）。未設定・不正値は "auto"。"""
    value = system_settings.get("embed_provider") if isinstance(system_settings, dict) else None
    return value if value in EMBED_PROVIDERS else EMBED_PROVIDER_DEFAULT


def ollama_embed_dim(url: str, model: str) -> int | None:
    """Ollama 埋め込みモデルの次元。既知表 → 実測（1 件を埋め込んで応答の長さを採る・成功のみキャッシュ）の順。解決できなければ None。"""
    known = _OLLAMA_KNOWN_DIMS.get(model.split(":", 1)[0])
    if known:
        return known
    key = (url, model)
    if key in _ollama_dim_cache:
        return _ollama_dim_cache[key]
    failed_at = _ollama_dim_failed.get(key)
    if failed_at is not None and time.monotonic() - failed_at < _OLLAMA_DIM_FAIL_TTL:
        return None
    try:
        with llm.no_proxy_requests():
            r = llm.post_json(llm.ollama_url(url, "/api/embed"), llm.JSON_HEADERS,
                              {"model": model, "input": ["dimension probe"]}, _OLLAMA_DIM_PROBE_TIMEOUT)
        vecs = r.get("embeddings") if isinstance(r, dict) else None
        dim = len(vecs[0]) if isinstance(vecs, list) and vecs and isinstance(vecs[0], list) else 0
    except Exception as exc:
        _log.warning("ollama embed dimension probe failed: model=%s error_type=%s", model, type(exc).__name__)
        dim = 0
    if dim <= 0:
        _ollama_dim_failed[key] = time.monotonic()
        return None
    _ollama_dim_cache[key] = dim
    return dim


def cfg(settings: dict | None = None, *, system_settings: dict | None = None) -> dict | None:
    """埋め込み設定 `{provider, key/url, model, dim}`（無ければ None）。選択中のクラウドプロバイダで自動解決する（個人設定のプロバイダは読まない）。
    クラウドを明示選択していて解決できない場合は Ollama へ倒さず None。`SHERPA_DISABLE_EMBED` で無効化できる。
    `system_settings` を渡すと、その読み取り済みスナップショットを使う（`cloud_selected_but_unavailable()` と同じ値で判定するため）。
    """
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return None
    # 同じ system_settings スナップショットを `select_provider()` と送信時の接続先解決で使う。
    # 読取失敗を `{}` に縮退させない（未選択と読取不能は別状態）。例外はそのまま呼び出し元へ伝える
    from . import store
    sys_s = system_settings if system_settings is not None else store.get_system_settings()
    parallel = effective_embed_parallel(sys_s)

    def O(key):
        m, d = _MODELS["openai"]
        # Azure OpenAI は `model` にデプロイ名を送る。上書き元は `model_catalog`（openai/embed）。次元は変えない
        from . import model_catalog
        model = model_catalog.resolve_model("openai", "embed", None, system_settings=sys_s) or m
        return {"provider": "openai", "key": key, "model": model, "dim": d, "parallel": parallel,
                "system_settings": sys_s}  # `_embed_batch` の接続先解決へ引き継ぐ

    def L(url):
        m, d = _MODELS["ollama"]
        from . import model_catalog
        model = model_catalog.resolve_model("ollama", "embed", None, system_settings=sys_s) or m
        return {"provider": "ollama", "url": url, "model": model, "dim": d, "parallel": parallel}

    # 管理者が埋め込みを「ローカル（Ollama）」にしているときは、クラウド選択に関わらず中央 Ollama を使う。
    # 解決できなければ None（クラウドへは倒さない）
    if effective_embed_provider(sys_s) == "ollama":
        from . import keys as _k, model_catalog
        url = _k.resolve_ollama_url(None, system_settings=sys_s)
        model = (model_catalog.resolve_model("ollama", "embed", None, system_settings=sys_s)
                 or _MODELS["ollama"][0])
        dim = ollama_embed_dim(url, model)
        if dim is None:
            return None
        return {"provider": "ollama", "url": url, "model": model, "dim": dim, "parallel": parallel}

    # `cloud_provider` が不正値のときは、黙って既定キーで送信しない（fail-closed）。None は「埋め込み未設定」として扱われる
    from . import keys as _keys
    try:
        return llm.select_provider(settings, openai=O, ollama=L, system_settings=sys_s,
                                   strict=True)
    except _keys.InvalidCloudProviderConfigError:
        _log.warning("embeddings.cfg: cloud_provider の値が不正（廃止済みの保存値を含む）なため埋め込みを無効化しました。設定画面で選び直してください")
        return None


def cloud_selected_but_unavailable(system_settings: dict | None = None) -> bool:
    """`cfg()` が None の理由が「クラウドを選んでいない」でなく「明示選択したクラウドが解決できない」ことを示すか。
    後者のとき `es_index.py` は再索引を失敗させ（既存索引を BM25 のみで上書きしない）、検索は明示エラーを返す。
    `SHERPA_DISABLE_EMBED` が有効な間は常に偽。
    """
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return False
    from . import keys as _keys, store
    sys_s = system_settings if system_settings is not None else store.get_system_settings()
    if _keys.retired_cloud_provider(sys_s) is not None:
        return True  # 廃止済みプロバイダの保存値＝管理者が選び直すまで未接続（BM25 へ黙って降格しない）
    if effective_embed_provider(sys_s) == "ollama":
        return True  # ローカルを明示選択済み＝`cfg()` が None なら Ollama／モデルの障害（BM25 へ黙って降格しない）
    return _keys.cloud_provider_explicitly_selected(sys_s)


def _safe_failure_fields(exc: Exception) -> tuple[int | None, str, str | None]:
    """API 失敗から本文・key・header・message を除いた分類だけを返す。"""
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


def _sleep(seconds: float) -> None:
    """`time.sleep` のモジュール関数越し呼び出し（テストが差し替えられるように）。"""
    time.sleep(seconds)


def _retryable_http_status(exc: Exception) -> bool:
    """429/5xx のみ再送対象。"""
    return isinstance(exc, urllib.error.HTTPError) and exc.code in _RETRY_STATUSES


def _retryable_transport_error(exc: Exception) -> bool:
    """一時的な通信エラー（非 HTTPError の `URLError`／timeout／接続エラー）。"""
    if isinstance(exc, urllib.error.HTTPError):
        return False
    return isinstance(exc, (urllib.error.URLError, socket.timeout, ConnectionError))


def _is_retryable(exc: Exception) -> bool:
    return _retryable_http_status(exc) or _retryable_transport_error(exc)


def _retry_after_seconds(exc: Exception) -> int | None:
    """`Retry-After` ヘッダ（整数秒のみ・上限 `_RETRY_AFTER_MAX_SECONDS`）。無い・不正なら None（指数バックオフへ）。"""
    if not isinstance(exc, urllib.error.HTTPError) or exc.headers is None:
        return None
    raw = exc.headers.get("Retry-After")
    if raw is None:
        return None
    try:
        seconds = int(str(raw).strip())
    except (TypeError, ValueError):
        return None
    if seconds < 0:
        return None
    return min(seconds, _RETRY_AFTER_MAX_SECONDS)


def _utf8_windows(text: str, *, max_bytes: int = _ITEM_MAX_UTF8_BYTES) -> list[str]:
    """UTF-8 文字境界を壊さず、連結すると原文へ戻る決定的な byte 上限 window。"""
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
    """元入力 index 付き window を API の件数・総 UTF-8 byte 上限内にまとめる。"""
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


def is_valid_vector(vector: object, dimension: int) -> bool:
    """dense vector として安全な、有限・非 zero の数値 list だけを受理する。"""
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


def _embed_batch_once(texts: list, c: dict, timeout: int = _TIMEOUT) -> list | None:
    """1 回分の HTTP 送信（再送なし）。ネットワーク／HTTP 例外はそのまま送出する（`_embed_batch` が分類する）。応答の形が壊れていれば None。"""
    from . import metering
    if c["provider"] == "openai":
        # `cfg()` が渡した snapshot で接続先を解決する。送信は OpenAI 専用の送信前ガード付き `llm.openai_post_json`
        _sys_s = c.get("system_settings")
        r = llm.openai_post_json(llm.openai_url("embeddings", system_settings=_sys_s),
                          llm.openai_headers(c["key"], system_settings=_sys_s),
                          {"model": c["model"], "input": texts, "dimensions": c["dim"]}, timeout)
        metering.acc_add(metering.usage_from_openai_embed(r))
        # ここから先は応答の解析。壊れた応答は例外にせず None（再送ループに二重計上させない）
        return _parse_openai_embeddings(r, len(texts))
    if c["provider"] == "ollama":
        # Ollama の文書・質問は環境の HTTP(S)_PROXY へ渡さない
        with llm.no_proxy_requests():
            r = llm.post_json(llm.ollama_url(c["url"], "/api/embed"), llm.JSON_HEADERS,
                              {"model": c["model"], "input": texts}, timeout)
        metering.acc_add(metering.usage_from_ollama_embed(r))
        return r.get("embeddings") if isinstance(r, dict) else None


def _parse_openai_embeddings(r, n: int) -> list | None:
    """OpenAI 応答からベクトル列を取り出す（壊れた応答は None）。各 vector の入力位置 `index` で順序を復元する。
    全件 index 無しは受信順を維持し、一部だけ欠ける応答は拒否する。
    """
    try:
        data = r.get("data", [])
        if not isinstance(data, list) or not all(isinstance(item, dict) for item in data):
            return None
        indices = [item.get("index") for item in data]
        if all(isinstance(index, int) and not isinstance(index, bool) for index in indices):
            if sorted(indices) != list(range(n)):
                return None
            data = sorted(data, key=lambda item: item["index"])
        elif any(index is not None for index in indices):
            return None
        return [d["embedding"] for d in data]
    except (TypeError, KeyError, AttributeError):
        return None


def _embed_batch(texts: list, c: dict) -> list | None:
    """1 バッチ分の送信＋429/5xx・一時的通信エラーの再送（`Retry-After` 優先・無ければ指数バックオフ・最大 `_RETRY_MAX_RETRIES` 回・合計 `_RETRY_MAX_TOTAL_SECONDS` 秒で打ち切り None）。"""
    from . import metering
    start = time.monotonic()
    retries = 0
    while True:
        # 合計上限は HTTP の応答待ちも含めて守る（再送時は残り時間を timeout の上限にする）
        timeout = _TIMEOUT
        if retries:
            remaining = _RETRY_MAX_TOTAL_SECONDS - (time.monotonic() - start)
            if remaining < 1:
                return None  # 1 秒未満では送らない
            timeout = min(_TIMEOUT, int(remaining))
        try:
            return _embed_batch_once(texts, c, timeout)
        except Exception as exc:
            # 失敗した試行も物理送信として数える（トークンは不明なので `acc_add(None)`）。送信前ガードの拒否は送っていないので数えない
            if not isinstance(exc, llm.PreflightRejected):
                metering.acc_add(None)
            if not _is_retryable(exc) or retries >= _RETRY_MAX_RETRIES:
                _log_embed_failure(c, exc)
                return None
            wait = _retry_after_seconds(exc)
            if wait is None:
                wait = _RETRY_BACKOFF_SCHEDULE[min(retries, len(_RETRY_BACKOFF_SCHEDULE) - 1)]
            if time.monotonic() - start + wait > _RETRY_MAX_TOTAL_SECONDS:
                _log_embed_failure(c, exc)
                return None
            retries += 1
            status, error_type, _error_code = _safe_failure_fields(exc)
            _log.warning(
                "embedding request retry: provider=%s attempt=%s status=%s error_type=%s wait=%.1fs",
                c.get("provider"), retries, status, error_type, wait,
            )
            _sleep(wait)


def _embed_batch_worker(batch: list, c: dict) -> tuple[list | None, dict | None, int]:
    """スレッドプールのワーカー本体。`metering` の積み上げはスレッド専有のため、ワーカー自身で 1 バッチ分を閉じ、`(vectors, tokens, calls)` で主スレッドへ返す。"""
    from . import metering
    metering.acc_begin()
    vecs = _embed_batch(batch, c)
    tokens, calls = metering.acc_end()
    return vecs, tokens, calls


def _embed_batches_parallel(batches: list[tuple[list, list]], c: dict, parallel: int) -> list[list] | None:
    """有界スレッドプールで複数バッチを並列送信する（`origins` による順序復元は呼び出し元）。
    一部でも失敗したら None。早期打ち切り時も、走り始めたバッチは完了を待つ（未着手だけ cancel）。
    ワーカーは `contextvars.copy_context().run()` で起動する（`llm.no_proxy_requests()` を伝播させるため）。
    """
    from . import metering
    results: list[list | None] = [None] * len(batches)
    failed = False
    merged: set = set()
    future_to_index: dict = {}

    def _merge(future) -> tuple:
        """完了した future の計測を 1 回だけ主スレッドへ合算し、戻り値を返す。"""
        vecs, tokens, calls = future.result()
        if future not in merged:
            merged.add(future)
            if calls:
                metering.acc_merge(tokens, calls)
        return vecs, tokens, calls

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(parallel, len(batches)))
    try:
        for index, (_origins, batch) in enumerate(batches):
            ctx = contextvars.copy_context()
            future = executor.submit(ctx.run, _embed_batch_worker, batch, c)
            future_to_index[future] = index
        for future in concurrent.futures.as_completed(future_to_index):
            index = future_to_index[future]
            try:
                vecs, _tokens, _calls = _merge(future)
            except concurrent.futures.CancelledError:
                failed = True  # 結果の無いバッチ＝失敗
                break
            batch = batches[index][1]
            if not isinstance(vecs, list) or len(vecs) != len(batch):
                failed = True
                # 以降の完了は待たない（`finally` の `shutdown` が未着手を cancel する。`shutdown()` は 1 回だけ呼ぶ）
                break
            results[index] = vecs
    finally:
        executor.shutdown(wait=True, cancel_futures=True)
        # 早期打ち切り後に完了した実行中バッチの計測も記録に載せる
        for future in future_to_index:
            if future.done() and not future.cancelled():
                try:
                    _merge(future)
                except Exception:
                    pass
    return None if failed else results


def embed(texts: list, c: dict, *, user_id: str | None = None, world: str | None = None) -> list | None:
    """テキスト群 → ベクトル群（順序対応）。一部でも失敗したら None（ベクトル無効＝BM25 へ）。
    バッチループ全体を `metering.acc_begin()`／`acc_end()` で囲み、`kind='embed'` で 1 呼び出し 1 行記録する。
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
            batches = list(_window_batches(texts))
        except (TypeError, UnicodeError, ValueError):
            return None
        parallel = c.get("parallel")
        if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
            parallel = 1
        if len(batches) >= 2 and parallel > 1:
            results = _embed_batches_parallel(batches, c, parallel)
            if results is None:
                return None
            for (origins, _batch), vecs in zip(batches, results):
                for origin, vector in zip(origins, vecs):
                    if not is_valid_vector(vector, dimension):
                        return None
                    grouped[origin].append(vector)
        else:
            for origins, batch in batches:
                vecs = _embed_batch(batch, c)
                if not isinstance(vecs, list) or len(vecs) != len(batch):
                    return None
                for origin, vector in zip(origins, vecs):
                    if not is_valid_vector(vector, dimension):
                        return None
                    grouped[origin].append(vector)
        out: list = []
        for vectors in grouped:
            if not vectors:
                return None
            if len(vectors) == 1:  # 短文は v1 と byte-identical な vector を維持
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
