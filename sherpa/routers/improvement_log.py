"""管理者向けの改善ログ（`GET /admin/improvement-log/export`）。集計ロジックは `sherpa/improvement_log.py`。`sherpa.api` を import しない。"""
from __future__ import annotations

import csv
import io
import json
import logging
import tempfile
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import StreamingResponse

from sherpa import improvement_log, store
from sherpa.csv_safe import csv_safe as _csv_safe
from sherpa.deps import _current_user, _require_admin

_log = logging.getLogger("sherpa")

improvement_log_router = APIRouter()

def _export_filename(fmt: str) -> str:
    return f"sherpa-improvement-log-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.{fmt}"


@improvement_log_router.get("/admin/improvement-log/export", tags=["管理者:改善ログ"])
def admin_improvement_log_export(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    format: str = Query("csv", pattern="^(csv|jsonl)$"),
):
    """改善ログを CSV/JSONL でエクスポートする（管理者のみ）。1行＝1ターン（assistant メッセージ）。
    個人情報由来のターンと sanitized share の複製は除く。行数の上限に達した場合は `X-Truncated: true` ヘッダ（JSONL は末尾に `{"truncated": true}` 行も追加）で示す。エクスポート実行は監査に記録し、記録できなければエクスポートを返さない（500）。
    """
    u = _current_user(request)
    _require_admin(u)
    time_from = datetime.now(timezone.utc) - timedelta(days=days)
    # ① ページごとに読んで整形し、一時ファイル（大きければディスクへ退避）へ書く ② 監査に記録 ③ 一時ファイルを返す。
    spool = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    try:
        scan = improvement_log.ExportScan(time_from=time_from, output_cap=improvement_log.EXPORT_MAX_ROWS)
        result_count = _write_export(spool, scan, format)
        truncated = scan.truncated

        try:
            store.audit(u["uid"], "admin.improvement_log_exported", "improvement_log", None,
                        detail={"days": days, "format": format, "result_count": result_count,
                                "truncated": truncated},
                        outcome="success", severity="critical")
        except Exception:
            _log.critical("audit write failed for admin.improvement_log_exported – fail-closed")
            raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")
        size = spool.tell()
    except BaseException:
        spool.close()
        raise

    filename = _export_filename(format)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"', "Content-Length": str(size)}
    if truncated:
        headers["X-Truncated"] = "true"

    def _chunks():
        try:
            spool.seek(0)
            while chunk := spool.read(1024 * 1024):
                yield chunk
        finally:
            spool.close()

    media_type = ("application/x-ndjson; charset=utf-8" if format == "jsonl"
                  else "text/csv; charset=utf-8")
    return StreamingResponse(_chunks(), media_type=media_type, headers=headers)


def _write_export(dest, scan: improvement_log.ExportScan, fmt: str) -> int:
    """`scan` の行を `fmt`（csv/jsonl）で `dest`（バイト列の書き込み先）へページごとに流し、書いた行数を返す。JSONL は打ち切られたとき末尾に `{"truncated": true}` 行を足す。"""
    buf = io.StringIO()
    writer = (csv.DictWriter(buf, fieldnames=list(improvement_log.EXPORT_FIELDS))
              if fmt == "csv" else None)

    def _flush() -> None:
        dest.write(buf.getvalue().encode("utf-8"))
        buf.seek(0)
        buf.truncate()

    if writer is not None:
        writer.writeheader()
    count = 0
    for batch in scan.batches():
        feedback_map = store.get_feedback_by_message_ids([m["id"] for m in batch])
        for m in batch:
            r = improvement_log.build_export_row(m, feedback=feedback_map.get(m["id"]))
            if writer is not None:
                writer.writerow({
                    k: _csv_safe(json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list))
                                else v)
                    for k, v in r.items()
                })
            else:
                buf.write(json.dumps(r, ensure_ascii=False, default=str) + "\n")
            count += 1
        _flush()
    if fmt == "jsonl" and scan.truncated:
        buf.write(json.dumps({"truncated": True}, ensure_ascii=False) + "\n")
    _flush()
    return count
