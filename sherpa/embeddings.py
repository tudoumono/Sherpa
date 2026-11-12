"""埋め込み（ベクトル）生成（内部 Elasticsearch の kNN 用）。OpenAI（Azure OpenAI を含む）／Ollama に REST（urllib）で送る。
未設定なら None＝ベクトル無効（ES は BM25 のみで動く）。cosine 前提で正規化は ES 側に任せる。
設計: docs/design/rag.md「埋め込み」
"""
from __future__ import annotations

import base64
import binascii
import concurrent.futures
import contextvars
import dataclasses
import json
import logging
import math
import os
import re
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
from array import array

from . import embed_shrink, llm

_BATCH = 50
_ITEM_MAX_UTF8_BYTES = 8_000
_BATCH_MAX_UTF8_BYTES = 240_000
_TIMEOUT = 60
# 1 回の要求の待ち時間（システム設定 `embed_request_timeout_s`）と 1 回に送る件数の上限（`embed_batch_size`）の既定・範囲
EMBED_TIMEOUT_DEFAULT = _TIMEOUT
EMBED_TIMEOUT_MIN = 10
EMBED_TIMEOUT_MAX = 600
EMBED_BATCH_DEFAULT = _BATCH
EMBED_BATCH_MIN = 1
EMBED_BATCH_MAX = 200
_SAFE_REMOTE_ERROR_FIELD = re.compile(r"[A-Za-z0-9_.-]{1,80}")
# 並列度（システム設定 `embed_parallel`）の既定・範囲。env フォールバックは持たない
EMBED_PARALLEL_DEFAULT = 4
EMBED_PARALLEL_MIN = 1
EMBED_PARALLEL_MAX = 16
# OpenAI の埋め込みの次元（システム設定 `embed_dim`）の範囲。Ollama はモデルごとの固定次元でこの設定を使わない
EMBED_DIM_MIN = 64
EMBED_DIM_MAX = 4096
# OpenAI の埋め込みモデル名 → 既定の次元。表に無いモデル（Azure のデプロイ名など）は `_MODELS["openai"]` の次元
_OPENAI_MODEL_DIMS = {"text-embedding-3-small": 1536, "text-embedding-3-large": 3072}
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
           "ollama": ("bge-m3", 1024)}
# 埋め込みの接続先（システム設定 `embed_provider`）。"auto"＝回答用に選んだクラウドに従う／"ollama"＝中央 Ollama を使う
EMBED_PROVIDERS = ("auto", "ollama")
EMBED_PROVIDER_DEFAULT = "auto"
_OLLAMA_DIM_FAIL_TTL = 60.0
_ollama_dim_cache: dict = {}  # (url, model) -> 実測次元
_ollama_dim_failed: dict = {}  # (url, model) -> 失敗時刻
# 埋め込み API には `encoding_format: "base64"` を付けて送り、float32 リトルエンディアンのバイト列を `array('f')` に戻す。
# 次の 2 つはプロセスの間だけ持つ切り替えの記録（再起動で戻る）
_float_only_urls: set = set()  # base64 の指定に 400 を返し、指定なしでは通った接続先（送信先 URL）
_ollama_legacy_urls: set = set()  # `/v1/embeddings` が 404 で、`/api/embed` なら通った Ollama（ベース URL）
_switch_lock = threading.Lock()
# 専用ログ（sherpa.embed）
_log = logging.getLogger("sherpa.embed")


# API から受けたベクトルは float32 の `array('f')` にする。0.19.5 の float32 のキャッシュ（`embed_cache.f32.sqlite3`）の BLOB はリトルエンディアンの float32。
# ES へ送る値と埋め込みキャッシュ（`embed_cache.i8.sqlite3`）の値は `quantize_vector()` で 1 次元 1 バイトに縮める。
def to_vector(values) -> array:
    """float の list／`array('f')` を float32 の `array('f')` にする（float32 に丸める）。"""
    if isinstance(values, array) and values.typecode == "f":
        return values
    return array("f", values)


def vector_to_bytes(vector) -> bytes:
    """ベクトルを float32 リトルエンディアンのバイト列にする。"""
    vec = to_vector(vector)
    if sys.byteorder == "big":
        vec = array("f", vec)
        vec.byteswap()
    return vec.tobytes()


def vector_from_bytes(raw, dimension: int) -> array | None:
    """バイト列を `array('f')` に戻す。長さが `dimension`×4 でない・有限でない値を含むものは None（キャッシュ miss）。"""
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != dimension * 4 or dimension <= 0:
        return None
    vec = array("f")
    vec.frombytes(bytes(raw))
    if sys.byteorder == "big":
        vec.byteswap()
    try:
        return vec if math.isfinite(math.fsum(vec)) else None
    except (ValueError, OverflowError):
        return None


def vector_to_json(vector) -> list:
    """数の list（float32 に丸めた値・JSON 形式の埋め込みキャッシュへ書く）。list で来たものはそのまま通す。"""
    return vector.tolist() if isinstance(vector, array) else vector


# ES へ送るベクトル（索引に入れるもの・kNN の問い）と埋め込みキャッシュの値の縮め方の名前。索引の `_meta.quant` に刻み、違えば作り直す（`es_index.needs_reindex`）
ES_VECTOR_QUANT = "byte_maxabs_v1"


def vector_to_es_bytes(vector) -> list | None:
    """ES の byte の dense_vector へ渡す -127〜127 の整数の list。ベクトルごとに最大の絶対値で割って 127 倍し、四捨五入する（0.5 は 0 から遠い側）。
    float は float32 に丸めてから縮める（キャッシュの float32 から縮めても、API の値から縮めても同じ結果にする）。縮めた値をもう一度通しても変わらない。
    全部 0・有限でない値・数でない値を含むものは None。
    """
    if isinstance(vector, array) and vector.typecode == "b":
        return vector.tolist() if any(vector) else None
    try:
        vec = to_vector(vector)
        if not math.isfinite(math.fsum(map(abs, vec))):
            return None
        peak = max(map(abs, vec), default=0.0)
    except (TypeError, ValueError, OverflowError):
        return None
    if peak <= 0:
        return None
    s = 127.0 / peak
    return [int(x * s + 0.5) if x >= 0 else -int(0.5 - x * s) for x in vec]


def quantize_vector(vector) -> array | None:
    """`vector_to_es_bytes` で縮めた値を `array('b')`（1 次元 1 バイト）で返す。縮められないものは None。"""
    q = vector_to_es_bytes(vector)
    return array("b", q) if q is not None else None


def quantized_from_bytes(raw, dimension: int) -> array | None:
    """1 次元 1 バイトのバイト列を `array('b')` に戻す。長さが `dimension` でない・-128 を含む・全部 0 のものは None（キャッシュ miss）。"""
    if not isinstance(raw, (bytes, bytearray, memoryview)) or len(raw) != dimension or dimension <= 0:
        return None
    vec = array("b")
    vec.frombytes(bytes(raw))
    if not any(vec) or min(vec) < -127:
        return None
    return vec


def effective_embed_parallel(system_settings: dict | None) -> int:
    """埋め込み HTTP 送信の並列度。システム設定 `embed_parallel`（1〜16）を使い、未設定・範囲外・非整数は既定 4。"""
    configured = system_settings.get("embed_parallel") if isinstance(system_settings, dict) else None
    if isinstance(configured, bool) or not isinstance(configured, int):
        return EMBED_PARALLEL_DEFAULT
    if configured < EMBED_PARALLEL_MIN or configured > EMBED_PARALLEL_MAX:
        return EMBED_PARALLEL_DEFAULT
    return configured


def _effective_int(system_settings: dict | None, key: str, default: int, low: int, high: int) -> int:
    configured = system_settings.get(key) if isinstance(system_settings, dict) else None
    if isinstance(configured, bool) or not isinstance(configured, int) or not low <= configured <= high:
        return default
    return configured


def effective_embed_request_timeout(system_settings: dict | None) -> int:
    """1 回の埋め込み要求の待ち時間（秒）。システム設定 `embed_request_timeout_s`（10〜600）。未設定・範囲外・非整数は既定 60。"""
    return _effective_int(system_settings, "embed_request_timeout_s", EMBED_TIMEOUT_DEFAULT,
                          EMBED_TIMEOUT_MIN, EMBED_TIMEOUT_MAX)


def effective_embed_batch_size(system_settings: dict | None) -> int:
    """1 回の要求に載せる件数の上限。システム設定 `embed_batch_size`（1〜200）。未設定・範囲外・非整数は既定 50。"""
    return _effective_int(system_settings, "embed_batch_size", EMBED_BATCH_DEFAULT, EMBED_BATCH_MIN, EMBED_BATCH_MAX)


def request_timeout(c: dict | None) -> int:
    """接続設定 `c` の 1 回の待ち時間。設定が既定のままなら `c` は何も持たず `_TIMEOUT`。"""
    return (c or {}).get("timeout_s", _TIMEOUT)


def openai_model_max_dim(model: str | None) -> int:
    """OpenAI の埋め込みモデルが返せる最大の次元。モデル名から分からなければ `_MODELS["openai"]` の次元。"""
    return _OPENAI_MODEL_DIMS.get((model or "").strip(), _MODELS["openai"][1])


def openai_model_default_dim(model: str | None) -> int:
    """設定が空欄のときの次元。どのモデルでも `_MODELS["openai"]` の次元（1536・モデルの最大を超えない）。
    守ること: モデルの最大（`-large` は 3072）を既定にしない（空欄のまま版を上げただけで次元が変わり、全部の埋め込みが使えなくなる）。
    """
    return min(_MODELS["openai"][1], openai_model_max_dim(model))


def effective_embed_dim(system_settings: dict | None, model: str | None = None) -> int:
    """OpenAI の埋め込みの次元。システム設定 `embed_dim`（64〜4096）を使い、未設定・範囲外・非整数は既定（1536）。
    モデルの最大より大きい値は最大に丸める（`dimensions` はモデルの最大以下でないと API が失敗する）。
    """
    default = openai_model_default_dim(model)
    limit = openai_model_max_dim(model)
    configured = system_settings.get("embed_dim") if isinstance(system_settings, dict) else None
    if isinstance(configured, bool) or not isinstance(configured, int):
        return default
    if configured < EMBED_DIM_MIN or configured > EMBED_DIM_MAX:
        return default
    if configured > limit:
        _log.warning("embed_dim=%d is larger than the model maximum; using %d (model=%s)", configured, limit, model)
        return limit
    return configured


class InvalidEmbedProviderConfigError(ValueError):
    """保存済みの `embed_provider` が非空の不正値のとき、`effective_embed_provider(strict=True)` が送出する。"""


def invalid_embed_provider(system_settings: dict | None) -> str | None:
    """保存済みの `embed_provider` が非空の不正値ならその文字列、未設定（なし・空）と正しい値は None（管理画面の警告用）。"""
    value = system_settings.get("embed_provider") if isinstance(system_settings, dict) else None
    if value is None or (isinstance(value, str) and not value.strip()) or value in EMBED_PROVIDERS:
        return None
    return str(value)


def effective_embed_provider(system_settings: dict | None, *, strict: bool = False) -> str:
    """埋め込みの接続先（`embed_provider`）。未設定は "auto"。
    非空の不正値は `strict=True` のときだけ `InvalidEmbedProviderConfigError`。`strict=False`（既定）は表示・診断向けに "auto" で返す。
    実際に送る入口は `strict=True` で呼び、不正値では接続先を選ばない。
    """
    if strict and invalid_embed_provider(system_settings) is not None:
        raise InvalidEmbedProviderConfigError("embed_provider の保存値が不正です")
    value = system_settings.get("embed_provider") if isinstance(system_settings, dict) else None
    return value if value in EMBED_PROVIDERS else EMBED_PROVIDER_DEFAULT


def remember_ollama_dim(url: str, model: str, dim: int) -> None:
    """応答の長さから分かった Ollama の次元を保存し、測れなかった印を消す。"""
    _ollama_dim_cache[(url, model)] = dim
    _ollama_dim_failed.pop((url, model), None)


def ollama_embed_dim(url: str, model: str, *, probe: bool = True, index_dim: int | None = None,
                     timeout_s: int = _TIMEOUT) -> int | None:
    """Ollama 埋め込みモデルの次元。実測（通常の送信と同じ経路で 1 件を埋め込み、応答の長さを採る・成功のみキャッシュ）が唯一の正。
    実測できなければ None（モデル名からの推測も 768 の仮定もしない）。
    `probe=False`（検索の経路）はネットワークへ出ない。保存済みの実測値、無ければ `index_dim`（索引のメタの次元）だけを返す。
    測るときの待ち時間は `timeout_s`（1 回の要求の待ち時間と同じ設定）。
    """
    key = (url, model)
    if key in _ollama_dim_cache:
        return _ollama_dim_cache[key]
    if not probe:
        return index_dim
    failed_at = _ollama_dim_failed.get(key)
    if failed_at is not None and time.monotonic() - failed_at < _OLLAMA_DIM_FAIL_TTL:
        return None
    vecs, _failure = reach_with_failure(
        ["dimension probe"], {"provider": "ollama", "url": url, "model": model, "dim": None, "timeout_s": timeout_s},
        deadline_s=timeout_s)
    dim = len(vecs[0]) if vecs else 0
    if dim <= 0:
        _ollama_dim_failed[key] = time.monotonic()
        return None
    _ollama_dim_cache[key] = dim
    return dim


def _ollama_model(sys_s: dict) -> str:
    from . import model_catalog
    return model_catalog.resolve_model("ollama", "embed", None, system_settings=sys_s) or _MODELS["ollama"][0]


def _tuning(sys_s: dict) -> dict:
    """待ち時間と件数の上限のうち、既定と違うものだけ（既定のままなら接続設定は何も増えない）。"""
    t, b = effective_embed_request_timeout(sys_s), effective_embed_batch_size(sys_s)
    return {**({"timeout_s": t} if t != EMBED_TIMEOUT_DEFAULT else {}),
            **({"batch": b} if b != EMBED_BATCH_DEFAULT else {})}


def cfg(settings: dict | None = None, *, system_settings: dict | None = None,
        probe: bool = True, index_dim: int | None = None) -> dict | None:
    """埋め込み設定 `{provider, key/url, model, dim}`（無ければ None）。選択中のクラウドプロバイダで自動解決する（個人設定のプロバイダは読まない）。
    None は「埋め込みを使わない・設定が成立しない」だけ。クラウドを明示選択していて解決できない場合は Ollama へ倒さず None。`SHERPA_DISABLE_EMBED` で無効化できる。
    Ollama は接続先が決まれば、次元を測れなくても `dim=None` で返す（次元が要る呼び出し元が `dim` を確かめる）。
    `system_settings` を渡すと、その読み取り済みスナップショットを使う（`cloud_selected_but_unavailable()` と同じ値で判定するため）。
    `probe=False`（検索の経路）は Ollama の次元をネットワークで測らず、保存済みの実測値か `index_dim`（索引のメタの次元）を使う。無ければ `dim=None`。
    """
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return None
    # 同じ system_settings スナップショットを `select_provider()` と送信時の接続先解決で使う。
    # 読取失敗を `{}` に縮退させない（未選択と読取不能は別状態）。例外はそのまま呼び出し元へ伝える
    from . import store
    sys_s = system_settings if system_settings is not None else store.get_system_settings()
    parallel = effective_embed_parallel(sys_s)
    tuning = _tuning(sys_s)

    def O(key):
        m, _d = _MODELS["openai"]
        # Azure OpenAI は `model` にデプロイ名を送る。上書き元は `model_catalog`（openai/embed）
        from . import model_catalog
        model = model_catalog.resolve_model("openai", "embed", None, system_settings=sys_s) or m
        d = effective_embed_dim(sys_s, model)  # OpenAI だけ設定で次元を選べる（`dimensions` で送る）
        return {"provider": "openai", "key": key, "model": model, "dim": d, "parallel": parallel, **tuning,
                "system_settings": sys_s}  # `_embed_batch` の接続先解決へ引き継ぐ

    def L(url):
        model = _ollama_model(sys_s)
        timeout_s = effective_embed_request_timeout(sys_s)
        dim = ollama_embed_dim(url, model, probe=probe, index_dim=index_dim, timeout_s=timeout_s)  # 実測が正。測れなければ None（768 を仮定しない）
        return {"provider": "ollama", "url": url, "model": model, "dim": dim, "parallel": parallel, **tuning}

    # 管理者が埋め込みを「ローカル（Ollama）」にしているときは、クラウド選択に関わらず中央 Ollama を使う。
    # クラウドへは倒さない。保存値が不正なら接続先を選ばず None（`cloud_selected_but_unavailable` が真になる）
    try:
        embed_provider = effective_embed_provider(sys_s, strict=True)
    except InvalidEmbedProviderConfigError:
        _log.warning("embeddings.cfg: embed_provider の保存値が不正なため埋め込みを無効化しました。管理画面で接続先を選び直してください")
        return None
    if embed_provider == "ollama":
        from . import keys as _k
        return L(_k.resolve_ollama_url(None, system_settings=sys_s))

    # `cloud_provider` が不正値のときは、黙って既定キーで送信しない（fail-closed）。None は「埋め込み未設定」として扱われる
    from . import keys as _keys
    try:
        return llm.select_provider(settings, openai=O, ollama=L, system_settings=sys_s,
                                   strict=True)
    except _keys.InvalidCloudProviderConfigError:
        _log.warning("embeddings.cfg: cloud_provider の値が不正（廃止済みの保存値を含む）なため埋め込みを無効化しました。設定画面で選び直してください")
        return None


def cloud_selected_but_unavailable(system_settings: dict | None = None) -> bool:
    """`cfg()` が None の理由が「クラウドを選んでいない」でなく「Ollama でない設定が成立しない」（保存値の不正・廃止済み・明示選択したクラウドが解決できない）ことを示すか。
    後者のとき `es_index.py` は再索引を失敗させ（既存索引を BM25 のみで上書きしない）、検索は明示エラーを返す。
    `SHERPA_DISABLE_EMBED` が有効な間は常に偽。
    """
    if os.environ.get("SHERPA_DISABLE_EMBED"):
        return False
    from . import keys as _keys, store
    sys_s = system_settings if system_settings is not None else store.get_system_settings()
    if invalid_embed_provider(sys_s) is not None:
        return True  # 埋め込みの接続先の保存値が不正＝管理者が選び直すまで未接続（auto・OpenAI へ黙って倒さない）
    if _keys.retired_cloud_provider(sys_s) is not None:
        return True  # 廃止済みプロバイダの保存値＝管理者が選び直すまで未接続（BM25 へ黙って降格しない）
    return _keys.cloud_provider_explicitly_selected(sys_s)


def describe(ec: dict) -> str:
    """ログ用の 1 行（接続先・モデル・次元）。URL は host:port だけで、認証情報・パス・鍵は含めない。"""
    host = ""
    if ec.get("url"):
        try:
            p = urllib.parse.urlsplit(str(ec["url"]))
            host = f" host={p.hostname}" + (f":{p.port}" if p.port else "")
        except ValueError:
            host = " host=?"
    return f"provider={ec.get('provider')}{host} model={ec.get('model')} dim={ec.get('dim')}"


def log_target(ec: dict | None, world: str) -> dict | None:
    """取り込みの開始時に 1 回、埋め込みの接続先・モデル・次元を embed ログへ 1 行残す。`ec` はそのまま返す。"""
    logging.getLogger("sherpa.embed").info("embed 接続先 %s（world=%s）", describe(ec) if ec else "なし（BM25 のみ）", world)
    return ec


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


@dataclasses.dataclass(frozen=True)
class EmbedFailure:
    """埋め込みが失敗した理由（接続診断用）。状態コード・例外の型名・期限切れ・応答の検証だけを持ち、鍵や本文は入れない。
    kind: "http"｜"error"（通信などの例外）｜"timeout"（1 回の要求の待ち時間切れ）｜"deadline"｜"dimension"（次元不一致）｜"invalid"（応答の形が不正）
    """
    kind: str
    status: int | None = None
    error_type: str | None = None
    got_dim: int | None = None


# `embed_with_failure` が立てる失敗の受け皿（最初の 1 件だけ残す）。並列ワーカーへはコンテキストごと渡る
_failure_sink: contextvars.ContextVar[list | None] = contextvars.ContextVar("embed_failure_sink", default=None)


# 件数を減らして送り直せる送信が立てる、待ち時間切れの受け皿（立っていなければ待ち時間切れも今までどおり再送する）
_timeout_sink: contextvars.ContextVar[list | None] = contextvars.ContextVar("embed_timeout_sink", default=None)


def _note_failure(failure: EmbedFailure) -> None:
    sink = _failure_sink.get()
    if sink is not None and not sink:
        sink.append(failure)


def _log_embed_failure(c: dict, exc: Exception) -> None:
    """最後の失敗をログに残し、受け皿があれば同じ分類を渡す。守ること: `_safe_failure_fields` は応答の本文を読み切るので 1 回だけ呼ぶ。"""
    status, error_type, error_code = _safe_failure_fields(exc)
    _note_failure(EmbedFailure("http" if status is not None else ("timeout" if is_timeout(exc) else "error"),
                               status=status, error_type=error_type))
    _log.warning(
        "embedding request failed: provider=%s status=%s error_type=%s error_code=%s",
        c.get("provider"), status, error_type, error_code,
    )


def _sleep(seconds: float) -> None:
    """`time.sleep` のモジュール関数越し呼び出し（テストが差し替えられるように）。"""
    time.sleep(seconds)


def is_timeout(exc: BaseException) -> bool:
    """1 回の要求の待ち時間切れか（例外の型で決める）。`URLError` に包まれた待ち時間切れも含む。"""
    reason = exc.reason if isinstance(exc, urllib.error.URLError) else None
    return isinstance(exc, (TimeoutError, socket.timeout)) or isinstance(reason, (TimeoutError, socket.timeout))


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


def _window_batches(texts: list[str], limit: int = _BATCH):
    """元入力 index 付き window を API の件数（`limit`）・総 UTF-8 byte 上限内にまとめる。"""
    origins: list[int] = []
    batch: list[str] = []
    batch_bytes = 0
    for input_index, text in enumerate(texts):
        for window in _utf8_windows(text):
            window_bytes = len(window.encode("utf-8"))
            if batch and (len(batch) >= limit or batch_bytes + window_bytes > _BATCH_MAX_UTF8_BYTES):
                yield origins, batch
                origins, batch, batch_bytes = [], [], 0
            origins.append(input_index)
            batch.append(window)
            batch_bytes += window_bytes
    if batch:
        yield origins, batch


def is_valid_vector(vector: object, dimension: int) -> bool:
    """dense vector として安全な、有限・非 zero の数値 list（float32 の `array('f')` を含む）だけを受理する。"""
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0:
        return False
    if not (isinstance(vector, list) or (isinstance(vector, array) and vector.typecode == "f")) or len(vector) != dimension:
        return False
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return False
        try:
            selected = float(value)
        except OverflowError:
            return False
        if not math.isfinite(selected):
            return False
        values.append(selected)
    try:
        norm_squared = math.fsum(value * value for value in values)
    except (OverflowError, ValueError):
        return False
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


def _note_switch(targets: set, key: str, message: str, *args) -> None:
    """切り替えを記録し、そのプロセスで最初の 1 回だけログに 1 行出す。"""
    with _switch_lock:
        if key in targets:
            return
        targets.add(key)
    _log.warning(message, *args)


def _post_embeddings(post, url: str, headers: dict, body: dict, timeout: int, provider: str) -> dict:
    """`encoding_format: "base64"` を付けて送る。400 が返り、付けずに送り直すと通ったら、その送信先は以降プロセスの間付けずに送る。
    付けずに送っても失敗するときは切り替えず、その失敗をそのまま送出する。
    """
    from . import metering
    if url in _float_only_urls:
        return post(url, headers, body, timeout)
    try:
        return post(url, headers, {**body, "encoding_format": "base64"}, timeout)
    except urllib.error.HTTPError as exc:
        if exc.code != 400:
            raise
        exc.close()
    metering.acc_add(None)  # 400 で終わった試行も物理送信として数える
    r = post(url, headers, body, timeout)
    _note_switch(_float_only_urls, url,
                 "embedding endpoint rejected encoding_format=base64 (400); sending float from now on: provider=%s", provider)
    return r


def _embed_batch_once(texts: list, c: dict, timeout: int = _TIMEOUT) -> list | None:
    """1 回分の HTTP 送信（再送なし）。ネットワーク／HTTP 例外はそのまま送出する（`_embed_batch` が分類する）。応答の形が壊れていれば None。"""
    from . import metering
    if c["provider"] == "openai":
        # `cfg()` が渡した snapshot で接続先を解決する。送信は OpenAI 専用の送信前ガード付き `llm.openai_post_json`
        _sys_s = c.get("system_settings")
        r = _post_embeddings(llm.openai_post_json, llm.openai_url("embeddings", system_settings=_sys_s),
                             llm.openai_headers(c["key"], system_settings=_sys_s),
                             {"model": c["model"], "input": texts, "dimensions": c["dim"]}, timeout, "openai")
        metering.acc_add(metering.usage_from_openai_embed(r))
        # ここから先は応答の解析。壊れた応答は例外にせず None（再送ループに二重計上させない）
        return _parse_openai_embeddings(r, len(texts), c.get("dim"))
    if c["provider"] == "ollama":
        # Ollama の文書・質問は環境の HTTP(S)_PROXY へ渡さない
        base = c["url"]
        with llm.no_proxy_requests():
            if base not in _ollama_legacy_urls:
                try:
                    r = _post_embeddings(llm.post_json, llm.ollama_url(base, "/v1/embeddings"), llm.JSON_HEADERS,
                                         {"model": c["model"], "input": texts}, timeout, "ollama")
                except urllib.error.HTTPError as exc:
                    if exc.code != 404:
                        raise
                    exc.close()
                    metering.acc_add(None)  # 404 で終わった試行も物理送信として数える
                else:
                    metering.acc_add(metering.usage_from_openai_embed(r))
                    return _parse_openai_embeddings(r, len(texts), c.get("dim"))
            r = llm.post_json(llm.ollama_url(base, "/api/embed"), llm.JSON_HEADERS,
                              {"model": c["model"], "input": texts}, timeout)
        if base not in _ollama_legacy_urls:
            _note_switch(_ollama_legacy_urls, base,
                         "ollama has no /v1/embeddings (404); using /api/embed from now on")
        metering.acc_add(metering.usage_from_ollama_embed(r))
        return r.get("embeddings") if isinstance(r, dict) else None


def _decode_embedding(value, dimension):
    """応答の embedding を返す。文字列は base64 の float32 リトルエンディアンとして `array('f')` に戻す（壊れた base64・長さが次元×4 でない・有限でない値を含むものは None）。
    list など文字列以外はそのまま返す（検証は呼び出し元）。
    """
    if not isinstance(value, str):
        return value
    try:
        raw = base64.b64decode(value, validate=True)
    except (binascii.Error, ValueError):
        return None
    if dimension is None:  # 次元の実測・接続の確認: バイト長から求める
        if not raw or len(raw) % 4:
            return None
        dimension = len(raw) // 4
    return vector_from_bytes(raw, dimension)


def _parse_openai_embeddings(r, n: int, dimension: int | None = None) -> list | None:
    """OpenAI 応答からベクトル列を取り出す（壊れた応答は None）。各 vector の入力位置 `index` で順序を復元する。
    全件 index 無しは受信順を維持し、一部だけ欠ける応答は拒否する。embedding が base64 の文字列なら `_decode_embedding` で戻す。
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
        vectors = [_decode_embedding(d["embedding"], dimension) for d in data]
    except (TypeError, KeyError, AttributeError):
        return None
    return None if any(vector is None for vector in vectors) else vectors


def _embed_batch(texts: list, c: dict, deadline: float | None = None) -> list | None:
    """1 バッチ分の送信＋429/5xx・一時的通信エラーの再送（`Retry-After` 優先・無ければ指数バックオフ・最大 `_RETRY_MAX_RETRIES` 回・合計 `_RETRY_MAX_TOTAL_SECONDS` 秒で打ち切り None）。
    `deadline`（`time.monotonic()` の絶対時刻・省略可）を渡すと、合計の打ち切りをそれより早め、初回を含む各送信の応答待ちも残り時間で切る（検索の質問用）。省略時の動作は変えない。
    `_timeout_sink` が立っているときは、待ち時間切れを再送せずに印を足して None で返す（呼び出し側が件数を減らして送り直す）。
    """
    from . import metering
    start = time.monotonic()
    give_up_at = (start + _RETRY_MAX_TOTAL_SECONDS if deadline is None
                  else min(start + max(_RETRY_MAX_TOTAL_SECONDS, request_timeout(c)), deadline))   # 総期限つきでも最初の要求は設定の待ち時間をまるごと使える
    retries = 0
    while True:
        # 合計上限は HTTP の応答待ちも含めて守る（再送時は残り時間を timeout の上限にする）
        timeout = request_timeout(c)
        if retries or deadline is not None:
            remaining = give_up_at - time.monotonic()
            if remaining < 1:
                _note_failure(EmbedFailure("deadline"))
                return None  # 1 秒未満では送らない
            timeout = min(request_timeout(c), int(remaining))
        try:
            got = _embed_batch_once(texts, c, timeout)
            if deadline is not None and time.monotonic() > deadline:
                _note_failure(EmbedFailure("deadline"))
                return None  # 期限を過ぎて届いた応答は使わない（検索は全文検索で続ける）
            if got is None:
                _note_failure(EmbedFailure("invalid"))
            return got
        except Exception as exc:
            # 失敗した試行も物理送信として数える（トークンは不明なので `acc_add(None)`）。送信前ガードの拒否は送っていないので数えない
            if not isinstance(exc, llm.PreflightRejected):
                metering.acc_add(None)
            sink = _timeout_sink.get()
            if sink is not None and is_timeout(exc):
                sink.append(True)
                _log_embed_failure(c, exc)
                return None
            if not _is_retryable(exc) or retries >= _RETRY_MAX_RETRIES:
                _log_embed_failure(c, exc)
                return None
            wait = _retry_after_seconds(exc)
            if wait is None:
                wait = _RETRY_BACKOFF_SCHEDULE[min(retries, len(_RETRY_BACKOFF_SCHEDULE) - 1)]
            if time.monotonic() + wait > give_up_at:
                _log_embed_failure(c, exc)
                return None
            retries += 1
            status, error_type, _error_code = _safe_failure_fields(exc)
            _log.warning(
                "embedding request retry: provider=%s attempt=%s status=%s error_type=%s wait=%.1fs",
                c.get("provider"), retries, status, error_type, wait,
            )
            _sleep(wait)


def _send_batch(batch: list, c: dict, deadline: float | None) -> list | None:
    """1 バッチを送る。待ち時間切れなら、その回の中で件数を半分にして送り直す（`embed_shrink`）。"""
    def once(part, timed_out):
        token = _timeout_sink.set(timed_out)
        try:
            return _embed_batch(part, c) if deadline is None else _embed_batch(part, c, deadline)
        finally:
            _timeout_sink.reset(token)
    return embed_shrink.send_halving(batch, once, embed_shrink.state_of(c, c.get("batch", _BATCH)))


def _embed_batch_worker(batch: list, c: dict, deadline: float | None = None) -> tuple[list | None, dict | None, int, float]:
    """スレッドプールのワーカー本体。計測をスレッド内で閉じ、API 待ちの累積時間も主スレッドへ返す。"""
    from . import metering
    metering.acc_begin()
    started = time.monotonic()
    vecs = _send_batch(batch, c, deadline)
    elapsed_ms = (time.monotonic() - started) * 1000
    tokens, calls = metering.acc_end()
    return vecs, tokens, calls, elapsed_ms


def _embed_batches_parallel(batches: list[tuple[list, list]], c: dict, parallel: int,
                            metrics: dict | None = None, deadline: float | None = None) -> list[list] | None:
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
        vecs, tokens, calls, elapsed_ms = future.result()
        if future not in merged:
            merged.add(future)
            if metrics is not None:
                metrics["api_cumulative_ms"] = metrics.get("api_cumulative_ms", 0.0) + elapsed_ms
            if calls:
                metering.acc_merge(tokens, calls)
        return vecs, tokens, calls

    executor = concurrent.futures.ThreadPoolExecutor(max_workers=min(parallel, len(batches)))
    try:
        for index, (_origins, batch) in enumerate(batches):
            ctx = contextvars.copy_context()
            future = executor.submit(ctx.run, _embed_batch_worker, batch, c, *(() if deadline is None else (deadline,)))
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


def _note_bad_vector(vector, dimension: int) -> None:
    got = len(vector) if isinstance(vector, (list, tuple)) else None
    _note_failure(EmbedFailure("dimension" if got is not None and got != dimension else "invalid", got_dim=got))


def embed_with_failure(texts: list, c: dict, *, user_id: str | None = None, world: str | None = None,
                       deadline_s: float | None = None) -> tuple[list | None, EmbedFailure | None]:
    """`embed` と同じ送信を 1 回行い、`(ベクトル群, 失敗理由)` を返す（接続診断用）。成功時の失敗理由は None。"""
    sink: list = []
    token = _failure_sink.set(sink)
    try:
        vecs = embed(texts, c, user_id=user_id, world=world, deadline_s=deadline_s)
    finally:
        _failure_sink.reset(token)
    return vecs, (sink[0] if sink and vecs is None else None)


def reach_with_failure(texts: list, c: dict, *, deadline_s: float | None = None) -> tuple[list | None, EmbedFailure | None]:
    """設定の次元を使わず、通常の送信（`_embed_batch`）と同じ経路で 1 バッチ送り、`(応答のベクトル群, 失敗理由)` を返す。
    各ベクトルは自身の長さを次元とみなして有限・非ゼロを確かめる（次元の実測と、次元が未実測の Ollama への接続診断専用・索引や検索には使わない）。
    """
    sink: list = []
    token = _failure_sink.set(sink)
    try:
        vecs = _embed_batch(texts, c, None if deadline_s is None else time.monotonic() + deadline_s)
        if vecs is not None and not (len(vecs) == len(texts) and all(isinstance(v, (list, array)) and is_valid_vector(v, len(v)) for v in vecs)):
            _note_failure(EmbedFailure("invalid"))
            vecs = None
    finally:
        _failure_sink.reset(token)
    return vecs, (sink[0] if sink and vecs is None else None)


def embed(texts: list, c: dict, *, user_id: str | None = None, world: str | None = None,
          metrics: dict | None = None, deadline_s: float | None = None) -> list | None:
    """テキスト群 → ベクトル群（順序対応）。一部でも失敗したら None（ベクトル無効＝BM25 へ）。
    バッチループ全体を `metering.acc_begin()`／`acc_end()` で囲み、`kind='embed'` で 1 呼び出し 1 行記録する。
    `deadline_s`（省略可）: 呼び出し全体の総期限（秒）。通信の応答待ちと再送をこの残り時間で打ち切り、間に合わなければ None。取り込みは渡さない。
    """
    if not isinstance(c, dict) or not texts or not isinstance(texts, list) or any(not isinstance(text, str) for text in texts):
        return None
    dimension = c.get("dim")
    provider = c.get("provider")
    if not isinstance(dimension, int) or isinstance(dimension, bool) or dimension <= 0 or not isinstance(provider, str):
        return None
    from . import metering
    deadline = None if deadline_s is None else time.monotonic() + deadline_s
    metering.acc_begin()
    api_measure: dict = {"api_cumulative_ms": 0.0}
    try:
        grouped: list[list[list]] = [[] for _text in texts]
        try:
            batches = list(_window_batches(texts, c.get("batch", _BATCH)))
        except (TypeError, UnicodeError, ValueError):
            return None
        parallel = c.get("parallel")
        if not isinstance(parallel, int) or isinstance(parallel, bool) or parallel < 1:
            parallel = 1
        if len(batches) >= 2 and parallel > 1:
            results = (_embed_batches_parallel(batches, c, parallel, metrics=api_measure) if deadline is None
                       else _embed_batches_parallel(batches, c, parallel, metrics=api_measure, deadline=deadline))
            if results is None:
                return None
            for (origins, _batch), vecs in zip(batches, results):
                for origin, vector in zip(origins, vecs):
                    if not is_valid_vector(vector, dimension):
                        _note_bad_vector(vector, dimension)
                        return None
                    grouped[origin].append(vector)
        else:
            for origins, batch in batches:
                started = time.monotonic()
                vecs = _send_batch(batch, c, deadline)
                api_measure["api_cumulative_ms"] += (time.monotonic() - started) * 1000
                if not isinstance(vecs, list) or len(vecs) != len(batch):
                    _note_failure(EmbedFailure("invalid"))
                    return None
                for origin, vector in zip(origins, vecs):
                    if not is_valid_vector(vector, dimension):
                        _note_bad_vector(vector, dimension)
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
        if metrics is not None:
            metrics["api_calls"] = metrics.get("api_calls", 0) + n
            metrics["api_cumulative_ms"] = metrics.get("api_cumulative_ms", 0.0) + api_measure["api_cumulative_ms"]
            if n:
                if isinstance(tokens, dict) and isinstance(tokens.get("input_tokens"), int):
                    metrics["input_tokens"] = metrics.get("input_tokens", 0) + tokens["input_tokens"]
                else:
                    metrics["input_tokens_unavailable"] = True
        if n:
            metering.record("embed", c["provider"], c["model"], tokens,
                            user_id=user_id, world=world, calls=n)
