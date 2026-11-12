"""POST /ext/v1/convert: Office/PDF を決定的に Markdown へ変換する（stateless・KB へ書かない）。
設計: docs/design/external-api.md
"""
from __future__ import annotations

import asyncio
import os
import tempfile
from pathlib import Path

from fastapi import Depends, File, HTTPException, Request, UploadFile
from starlette.concurrency import run_in_threadpool

from sherpa.ext.audit import _AuditScope
from sherpa.ext.auth import require_api_key
from sherpa.ext.common import router
from sherpa.ext.request_id import _REQUEST_ID_OPENAPI_HEADER, _validation_error_response, _XRequestIdIn
from sherpa.ext.zipsafe import _zip_bomb_reason
from sherpa.ingest import office_md

_CONVERT_MAX_BYTES = int(os.environ.get("SHERPA_EXT_CONVERT_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MB
_CONVERT_CONCURRENCY = max(1, int(os.environ.get("SHERPA_EXT_CONVERT_CONCURRENCY", "2")))  # 変換の同時実行数
_CONVERT_MAX_WAITERS = 8  # 枠待ちの要求数の上限（待つ間も最大 _CONVERT_MAX_BYTES の一時ファイルを持つため）
_CONVERT_WAIT_S = 60  # 枠の取得を待つ時間の上限（秒）
_RETRY_AFTER_S = "30"


_ZIP_EXTS = {".docx", ".xlsx", ".pptx"}  # OOXML＝zip コンテナ
# convert が受理する拡張子（拡張子から method を決定的に導出）
_ALLOWED_EXT = office_md.CONVERTIBLE_EXT | office_md.PDF_EXT  # {.docx,.xlsx,.pptx} | {.pdf}


_CONVERT_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    413: {"description": "ファイルサイズが上限を超えています", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("この形式は変換できません／安全でないファイルです"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "変換が混み合っています（Retry-After 秒後に再試行してください）",
          "headers": {**_REQUEST_ID_OPENAPI_HEADER,
                      "Retry-After": {"schema": {"type": "string"}, "description": "再試行までの目安秒数"}}},
}


_slot: tuple[asyncio.AbstractEventLoop, asyncio.Semaphore] | None = None


def _convert_slot() -> asyncio.Semaphore:
    """変換専用の同時実行枠。枠が埋まっていれば拒否せず空くまで待つ。イベントループごとに作り直す。"""
    global _slot
    loop = asyncio.get_running_loop()
    if _slot is None or _slot[0] is not loop:
        _slot = (loop, asyncio.Semaphore(_CONVERT_CONCURRENCY))
    return _slot[1]


_inflight = 0  # 受付済み（受信中・枠待ち・変換中）の要求数


def _busy(audit: _AuditScope, reason: str) -> HTTPException:
    audit.business_outcome = "failed"
    audit.pending["reason"] = reason
    return HTTPException(503, "変換が混み合っています。しばらくして再試行してください",
                         headers={"Retry-After": _RETRY_AFTER_S})


async def _spool_upload(file: UploadFile, ext: str) -> tuple[Path, int]:
    """受信しながら一時ファイルへ直接書く（全体をメモリに持たない）。上限超過・失敗時は一時ファイルを消して送出する。"""
    fd, tmp_name = tempfile.mkstemp(suffix=ext)
    tmp = Path(tmp_name)
    try:
        total = 0
        with os.fdopen(fd, "wb") as f:
            while True:
                chunk = await file.read(65536)
                if not chunk:
                    break
                total += len(chunk)
                if total > _CONVERT_MAX_BYTES:
                    raise HTTPException(
                        413, f"ファイルサイズが上限（{_CONVERT_MAX_BYTES // 1024 // 1024}MB）を超えています")
                f.write(chunk)
        return tmp, total
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


async def _run_convert(audit: _AuditScope, file: UploadFile, filename: str, ext: str) -> dict:
    """受信→枠待ち（上限時間つき）→変換。一時ファイルはどの経路でも消す。"""
    tmp, size_bytes = await _spool_upload(file, ext)
    try:
        audit.detail["size_bytes"] = size_bytes

        method = "pdf_text" if ext == ".pdf" else "ooxml"  # 拡張子から決定的に導出

        def _convert() -> dict:
            """zip 爆弾検査・`to_markdown` を threadpool 内で実行する。"""
            if ext in _ZIP_EXTS:
                reason = _zip_bomb_reason(tmp, size_bytes)
                if reason is not None:
                    raise HTTPException(422, "安全でないファイルです（zip 検査に失敗）")

            if ext == ".pdf" and not office_md.pdf_available():
                audit.detail.update({"method": None, "ok": False})
                audit.business_outcome = "failed"
                return {"md": None, "method": None, "unsupported": True,
                        "reason": "pdf_backend_unavailable", "filename": filename,
                        "size_bytes": size_bytes}

            md = office_md.to_markdown(tmp)
            if md is None:
                audit.detail.update({"method": None, "ok": False})
                audit.business_outcome = "failed"
                return {"md": None, "method": None, "unsupported": True,
                        "reason": "conversion_failed", "filename": filename,
                        "size_bytes": size_bytes}

            audit.detail.update({"method": method, "ok": True})
            return {"md": md, "method": method, "unsupported": False,
                    "filename": filename, "size_bytes": size_bytes}

        slot = _convert_slot()
        try:
            await asyncio.wait_for(slot.acquire(), _CONVERT_WAIT_S)
        except asyncio.TimeoutError:
            raise _busy(audit, "convert_wait_timeout") from None
        try:
            return await run_in_threadpool(_convert)
        finally:
            slot.release()
    finally:
        tmp.unlink(missing_ok=True)


@router.post("/convert", responses=_CONVERT_RESPONSES)
async def ext_convert(request: Request, file: UploadFile = File(...),
                      key: dict = Depends(require_api_key),
                      x_request_id: str | None = _XRequestIdIn):
    """アップロードされた Office/PDF ファイルを決定的に Markdown へ変換する。

    stateless（KB・台帳・ES・Neo4j へ書き込まず、LLM を呼ばない）。ファイルは一時領域にのみ書き、必ず削除する。world を持たないため world スコープは対象外。
    変換は要求を受け付けるスレッドとは別の実行プールで行う。
    """
    del x_request_id  # 実際の解決は ExtRequestMiddleware（ここは OpenAPI 契約の宣言のみ）
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    with _AuditScope(request, "ext_api.convert", "ext_convert") as audit:
        audit.detail.update({"filename": filename, "ext": ext})
        if ext not in _ALLOWED_EXT:
            raise HTTPException(422, "この形式は変換できません（対応: .docx/.xlsx/.pptx/.pdf）")

        global _inflight
        if _inflight >= _CONVERT_CONCURRENCY + _CONVERT_MAX_WAITERS:
            raise _busy(audit, "convert_queue_full")
        _inflight += 1
        try:
            return await _run_convert(audit, file, filename, ext)
        finally:
            _inflight -= 1
