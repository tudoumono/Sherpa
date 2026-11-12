"""X-Request-Id の解決・応答ヘッダの OpenAPI 宣言・アプリログへの request_id 束縛。
設計: docs/design/external-api.md「共通の約束」
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import re
import secrets

from fastapi import Header

# ==== X-Request-Id（応答ヘッダの共通契約・OpenAPI にも宣言する）====

_REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_MAX_LEN = 200
# fullmatch 専用（`$` は末尾の改行の直前にもマッチし、末尾 LF 付きのヘッダ注入を通してしまうため `^/$` アンカーは使わない）
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_REQUEST_ID_OPENAPI_HEADER = {
    "X-Request-Id": {"schema": {"type": "string"}, "description": "リクエスト追跡ID"}}
# 422 は自動バリデーション（HTTPValidationError 形）とハンドラ内のドメインエラー（`{"detail": "文字列"}`）の両方があり得る。
# `responses=` で 422 を上書きすると既定 content が消えるため、X-Request-Id ヘッダを足すときはこの content も明示的に引き継ぐ
_VALIDATION_ERROR_CONTENT = {
    "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}}


def _validation_error_response(extra_description: str) -> dict:
    return {"description": f"入力値が不正です（自動バリデーション、または {extra_description}）",
            "content": dict(_VALIDATION_ERROR_CONTENT), "headers": dict(_REQUEST_ID_OPENAPI_HEADER)}


# 入力側の X-Request-Id を OpenAPI に明示する Header() 宣言（解決/検証は `ExtRequestMiddleware` が routing より前に行う）。
# 長さ制約は付けない（付けると不正な X-Request-Id を送っただけでリクエスト全体が 422 になる）
_XRequestIdIn = Header(
    default=None, alias="X-Request-Id",
    description="呼び出し元が指定するリクエスト追跡ID（省略時は採番される・不正な値は無視して採番）")

# アプリログへ request_id を束縛する ContextVar（`ExtRequestMiddleware` が要求ごとに set/reset する。監査 DB とは別系統）
_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("ext_request_id", default=None)


class _RequestIdLogFilter(logging.Filter):
    """`_request_id_ctx` の現在値を `record.request_id` として付与する（属性が既に在れば上書きしない＝冪等）。

    handler へ付与する（logger へは付与しない）。logger のフィルタは子ロガーへ継承されないが、handler のフィルタは届いた全レコードに効くため。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id_ctx.get() or "-"
        return True


_request_id_log_filter = _RequestIdLogFilter()


@contextlib.contextmanager
def _logging_module_lock():
    """`logging` 内部の直列化 lock を、Python バージョン差（3.13 で `_acquireLock()`/`_releaseLock()` が撤去）を吸収した context manager として貸す。"""
    acquire = getattr(logging, "_acquireLock", None)
    release = getattr(logging, "_releaseLock", None)
    if acquire is not None and release is not None:
        acquire()
        try:
            yield
        finally:
            release()
    else:
        # Python 3.13+: `logging._lock`（RLock 本体）を直接使う
        with logging._lock:
            yield


def _attach_request_id_filter() -> None:
    """`_request_id_log_filter` を既知の受信点（handler 単位）へ冪等に付与する。

    対象は root logger のその時点の handlers・`"sherpa"` と配下で作成済みの全 logger の handlers・`logging.lastResort`
    （同じ handler は重複排除して1回ずつ付与）。import 時に加えて `lifespan` の起動処理でも呼ぶ（`log_setup.configure_logging()` より後）。
    `loggerDict` はライブな dict のため、`_logging_module_lock()` を借りて root の handlers と loggerDict のスナップショットだけを取る。
    """
    with _logging_module_lock():
        targets: list[logging.Handler] = list(logging.getLogger().handlers)
        logger_snapshot = list(logging.Logger.manager.loggerDict.items())
    for name, obj in logger_snapshot:
        if isinstance(obj, logging.Logger) and (name == "sherpa" or name.startswith("sherpa.")):
            targets.extend(obj.handlers)
    if logging.lastResort is not None:
        targets.append(logging.lastResort)
    seen: set[int] = set()
    for h in targets:
        hid = id(h)
        if hid in seen:
            continue
        seen.add(hid)
        if _request_id_log_filter not in h.filters:
            h.addFilter(_request_id_log_filter)


_attach_request_id_filter()


def _resolve_request_id(raw: str | None) -> str:
    """X-Request-Id ヘッダ値を検証して採用する（不正/無指定なら自前で採番）。"""
    if raw and len(raw) <= _REQUEST_ID_MAX_LEN and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return secrets.token_hex(16)
