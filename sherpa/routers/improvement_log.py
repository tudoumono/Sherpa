"""管理者:改善ログ。`GET /admin/improvement-log/export` のみ。

集計ロジック（ページング・個人情報除外・1ターン→エクスポート1行の変換・honest_failure 判定）は
`sherpa/improvement_log.py` に置く。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import csv
import io
import json
import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response

from sherpa import improvement_log, store
from sherpa.deps import _current_user, _require_admin

_log = logging.getLogger("sherpa")

improvement_log_router = APIRouter()

# 先頭がこれらの文字（半角/全角の = + - @・タブ・CR・LF）のセルは、スプレッドシートアプリが
# 数式として解釈しうる（CSV インジェクション・OWASP WSTG 準拠）。先頭の半角空白は無視して判定する
# （空白の後に = 等が来ても検知する）。先頭に `'`（テキスト強制の慣用記法）を前置して無害化する。
_CSV_FORMULA_TRIGGER_PREFIXES = (
    "=", "+", "-", "@", "\t", "\r", "\n",
    "＝", "＋", "－", "＠",   # 全角 = + - @
)


def _csv_safe(value):
    if not isinstance(value, str):
        return value
    if value.lstrip(" ").startswith(_CSV_FORMULA_TRIGGER_PREFIXES):
        return "'" + value
    return value


def _export_filename(fmt: str) -> str:
    return f"sherpa-improvement-log-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.{fmt}"


@improvement_log_router.get("/admin/improvement-log/export", tags=["管理者:改善ログ"])
def admin_improvement_log_export(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    format: str = Query("csv", pattern="^(csv|jsonl)$"),
):
    """改善ログを CSV/JSONL でエクスポートする（admin のみ）。1行＝1ターン（assistant メッセージ）。

    個人情報由来のターン（質問・回答のいずれか）と sanitized share の複製は除外する
    （`improvement_log.fetch_export_rows` 参照）。上限（`improvement_log.EXPORT_MAX_ROWS`）に
    到達した場合は `X-Truncated: true` ヘッダ（JSONL は末尾に `{"truncated": true}` 行も追加）で
    明示する。エクスポート実行自体を監査に記録する（fail-closed・`/admin/audit/export` と同じ流儀）。
    """
    u = _current_user(request)
    _require_admin(u)
    time_from = datetime.now(timezone.utc) - timedelta(days=days)
    msgs, truncated = improvement_log.fetch_export_rows(
        time_from=time_from, output_cap=improvement_log.EXPORT_MAX_ROWS)
    feedback_map = store.get_feedback_by_message_ids([m["id"] for m in msgs])
    rows = [improvement_log.build_export_row(m, feedback=feedback_map.get(m["id"])) for m in msgs]

    try:
        store.audit(u["uid"], "admin.improvement_log_exported", "improvement_log", None,
                    detail={"days": days, "format": format, "result_count": len(rows),
                            "truncated": truncated},
                    outcome="success", severity="critical")
    except Exception:
        _log.critical("audit write failed for admin.improvement_log_exported – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")

    filename = _export_filename(format)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if truncated:
        headers["X-Truncated"] = "true"
    if format == "jsonl":
        lines = [json.dumps(r, ensure_ascii=False, default=str) for r in rows]
        if truncated:
            lines.append(json.dumps({"truncated": True}, ensure_ascii=False))
        body = "\n".join(lines)
        if body:
            body += "\n"
        return Response(content=body, media_type="application/x-ndjson; charset=utf-8", headers=headers)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=list(improvement_log.EXPORT_FIELDS))
    writer.writeheader()
    for r in rows:
        row = {
            k: _csv_safe(json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list))
                        else v)
            for k, v in r.items()
        }
        writer.writerow(row)
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8", headers=headers)
