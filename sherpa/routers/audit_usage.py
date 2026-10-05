"""管理者向けの監査ログ（`GET /admin/audit`・`GET /admin/audit/verify`・`GET /admin/audit/export`）と利用統計（`GET /admin/usage/stats`）のエンドポイント。
`sherpa.api` を import しない。
設計: docs/design/usage.md「管理画面が読む `GET /admin/usage/stats`」
"""
from __future__ import annotations

import csv
import io
import json
import logging
import math
import tempfile
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi.responses import StreamingResponse

from sherpa import app_version, store, usage_export
from sherpa.deps import _current_user, _require_admin
from sherpa.schemas import (
    AdminAuditListResponse,
    AdminUsageQualityRunAck,
    AdminUsageQualityRunReq,
    AdminUsageStatsResponse,
)
from sherpa.store.usage import usage_export_aux_calls, usage_export_turns

_log = logging.getLogger("sherpa")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
audit_usage_router = APIRouter()


@audit_usage_router.get("/admin/audit", tags=["管理者:監査ログ"], response_model=AdminAuditListResponse)
def admin_audit_list(
    request: Request,
    actor: str | None = Query(None),
    action: str | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    outcome: str | None = Query(None),
    severity: str | None = Query(None),
    time_from: str | None = Query(None),  # ISO 8601 文字列。
    time_to: str | None = Query(None),
    request_id: str | None = Query(None),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
):
    """監査ログを閲覧する（管理者のみ）。actor/action/resource 等で絞り込める。閲覧自体が `admin.audit_viewed` として記録される（記録できなければ応答しない）。"""
    u = _current_user(request)
    _require_admin(u)

    rows = store.list_audit(
        actor=actor, action=action, resource_type=resource_type,
        resource_id=resource_id, outcome=outcome, severity=severity,
        time_from=time_from, time_to=time_to, request_id=request_id,
        limit=limit, offset=offset,
    )
    filters = {k: v for k, v in {
        "actor": actor, "action": action, "resource_type": resource_type,
        "resource_id": resource_id, "outcome": outcome, "severity": severity,
        "time_from": time_from, "time_to": time_to, "request_id": request_id,
        "limit": limit, "offset": offset,
    }.items() if v is not None and v != 0}

    # 閲覧自体を監査する（fail-closed: 書けなければ応答しない）。
    try:
        store.audit(u["uid"], "admin.audit_viewed", "audit_log", None,
                    detail={"filters": filters, "result_count": len(rows)},
                    outcome="success", severity="critical")
    except Exception:
        _log.critical("audit write failed for admin.audit_viewed – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")

    return {"rows": rows, "count": len(rows), "offset": offset, "limit": limit}


@audit_usage_router.get("/admin/audit/verify", tags=["管理者:監査ログ"])
def admin_audit_verify(request: Request):
    """監査ログの hash-chain の整合性を検証する（管理者のみ）。`ok=false` のとき `broken_at` にズレた行 id を返す。検証自体も監査に残る。"""
    u = _current_user(request)
    _require_admin(u)
    result = store.verify_audit_chain()
    try:
        store.audit(u["uid"], "admin.audit_verified", "audit_log", None,
                    detail=result, outcome="success" if result.get("ok") else "failure",
                    severity="critical")
    except Exception:
        _log.critical("audit write failed for admin.audit_verified – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")
    return result


_AUDIT_EXPORT_FIELDS = [
    "id", "created_at", "actor_user_id", "action", "resource_type", "resource_id",
    "outcome", "reason", "severity", "request_id", "session_id", "ip_hash",
    "user_agent", "detail", "before_state", "after_state",
]


def _audit_export_rows(**filters) -> list[dict]:
    """監査ログをエクスポート用にページング取得する。"""
    rows: list[dict] = []
    offset = 0
    page = 500
    max_rows = min(int(filters.pop("max_rows", 50000) or 50000), 50000)
    while len(rows) < max_rows:
        batch = store.list_audit(limit=min(page, max_rows - len(rows)), offset=offset, **filters)
        if not batch:
            break
        rows.extend(batch)
        if len(batch) < page:
            break
        offset += page
    return rows


def _audit_export_clean(row: dict) -> dict:
    clean = {}
    for k in _AUDIT_EXPORT_FIELDS:
        v = row.get(k)
        if k in ("detail", "before_state", "after_state"):
            v = store._redact(v) if v is not None else None
        if isinstance(v, datetime):
            v = v.astimezone(timezone.utc).isoformat()
        clean[k] = v
    return clean


def _audit_export_filename(fmt: str) -> str:
    return f"sherpa-audit-{datetime.now(timezone.utc).strftime('%Y%m%d-%H%M%S')}.{fmt}"


# `include_chat_content=1` のときだけ本文を結合する。保存済みの `audit_log.detail`（append-only・hash-chain 対象）は変更せず、エクスポート出力にだけ足す。
_CHAT_CONTENT_DELETED_PLACEHOLDER = "（削除済み）"
_CHAT_CONTENT_PERSONAL_PLACEHOLDER = "（個人ファイル参照ターン・本文はエクスポート対象外）"


def _chat_content_for_export(msg: dict | None, *, turn_personal: bool = False) -> str | None:
    """メッセージ1件をエクスポート用の文字列に落とす。削除済み・個人参照ターンはプレースホルダにする。
    所属会話が soft-delete 済み（`store.get_messages_by_ids` の `conv_deleted`）なら存在しない id と同じ扱いにする。個人判定は message の flag と呼出元が渡す `turn_personal`（chat.turn.detail.personal）の OR で行い、片側だけ立っていても両側をプレースホルダにする。
    """
    if msg is None or msg.get("conv_deleted"):
        return _CHAT_CONTENT_DELETED_PLACEHOLDER
    if turn_personal or msg.get("personal"):
        # 個人 workspace 参照ターンの本文は admin エクスポートにも平文で出さない。
        return _CHAT_CONTENT_PERSONAL_PLACEHOLDER
    return msg.get("content")


def _join_chat_content(rows: list[dict]) -> None:
    """`chat.turn` 行の detail に messages 台帳の本文（user prompt / assistant 回答）を結合する。対象行全体の message_id を一括収集して 1 回で取得する（`store.get_messages_by_ids`）。`detail` は redaction 済みの dict を受け取り、キーを追加するだけで既存キーには触れない。"""
    ids: set[int] = set()
    for r in rows:
        if r.get("action") != "chat.turn":
            continue
        d = r.get("detail") or {}
        if d.get("message_id_user"):
            ids.add(d["message_id_user"])
        if d.get("message_id_assistant"):
            ids.add(d["message_id_assistant"])
    if not ids:
        return
    msgs = store.get_messages_by_ids(list(ids))
    for r in rows:
        if r.get("action") != "chat.turn":
            continue
        d = dict(r.get("detail") or {})
        turn_personal = bool(d.get("personal"))
        uid_msg, aid_msg = d.get("message_id_user"), d.get("message_id_assistant")
        d["user_prompt"] = _chat_content_for_export(
            msgs.get(uid_msg), turn_personal=turn_personal) if uid_msg else None
        d["assistant_answer"] = _chat_content_for_export(
            msgs.get(aid_msg), turn_personal=turn_personal) if aid_msg else None
        r["detail"] = store._redact(d)  # 追加後にもう一度 redaction する（多層防御）。


@audit_usage_router.get("/admin/audit/export", tags=["管理者:監査ログ"])
def admin_audit_export(
    request: Request,
    format: str = Query("csv", pattern="^(csv|jsonl)$"),
    actor: str | None = Query(None),
    action: str | None = Query(None),
    resource_type: str | None = Query(None),
    resource_id: str | None = Query(None),
    outcome: str | None = Query(None),
    severity: str | None = Query(None),
    time_from: str | None = Query(None),
    time_to: str | None = Query(None),
    request_id: str | None = Query(None),
    include_chat_content: bool = Query(False),
):
    """監査ログを CSV / JSONL でエクスポートする（管理者のみ）。
    `include_chat_content=1` を指定したときだけ、`chat.turn` 行にユーザーのプロンプトと AI の回答（headline）を結合する（未指定なら本文を含まない）。個人参照ターンはプレースホルダになる。
    """
    u = _current_user(request)
    _require_admin(u)
    filters = {
        "actor": actor, "action": action, "resource_type": resource_type,
        "resource_id": resource_id, "outcome": outcome, "severity": severity,
        "time_from": time_from, "time_to": time_to, "request_id": request_id,
    }
    filters = {k: v for k, v in filters.items() if v is not None}
    rows = [_audit_export_clean(r) for r in _audit_export_rows(**filters)]
    if include_chat_content:
        _join_chat_content(rows)

    try:
        store.audit(u["uid"], "admin.audit_exported", "audit_log", None,
                    detail={"format": format, "filters": filters, "result_count": len(rows),
                            "include_chat_content": include_chat_content},
                    outcome="success", severity="critical")
    except Exception:
        _log.critical("audit write failed for admin.audit_exported – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")

    filename = _audit_export_filename(format)
    headers = {"Content-Disposition": f'attachment; filename="{filename}"'}
    if format == "jsonl":
        body = "\n".join(json.dumps(r, ensure_ascii=False, default=str) for r in rows)
        if body:
            body += "\n"
        return Response(content=body, media_type="application/x-ndjson; charset=utf-8",
                        headers=headers)

    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=_AUDIT_EXPORT_FIELDS)
    writer.writeheader()
    for r in rows:
        row = {
            k: json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
            for k, v in r.items()
        }
        writer.writerow(row)
    return Response(content=buf.getvalue(), media_type="text/csv; charset=utf-8",
                    headers=headers)


@audit_usage_router.get("/admin/usage/stats", tags=["管理者:利用統計"], response_model=AdminUsageStatsResponse)
def admin_usage_stats(request: Request, days: int = Query(30, ge=1, le=365),
                      time_from: str | None = Query(None, alias="from"),
                      time_to: str | None = Query(None, alias="to")):
    """利用統計を返す（管理者のみ）。メッセージ本文・会話タイトルは含めず、件数・日時・種別だけを集計する。閲覧自体が `admin.usage_viewed` として記録される（記録できなければ応答しない）。
    期間は `days`（JST 暦日・既定30日）か `from`/`to`（ISO 8601・オフセット必須・半開区間 `[from, to)`・両方必須・最大365日）のどちらか。`days` を明示指定したうえで `from`/`to` も渡すと 422。応答の `period` には実際に使った境界が入る。
    """
    u = _current_user(request)
    _require_admin(u)
    if (time_from is not None or time_to is not None) and "days" in request.query_params:
        raise HTTPException(422, "days と from/to は同時に指定できません")
    try:
        result = store.usage_stats(days=days, time_from=time_from, time_to=time_to)
    except store.UsagePeriodError as e:
        raise HTTPException(422, str(e)) from None
    detail = {"days": days} if time_from is None and time_to is None else {"from": time_from, "to": time_to}
    try:
        store.audit(u["uid"], "admin.usage_viewed", "usage", None, detail=detail,
                    outcome="success", severity="info")
    except Exception:
        _log.critical("audit write failed for admin.usage_viewed – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")
    return result


@audit_usage_router.get("/admin/usage/export", tags=["管理者:利用統計"])
def admin_usage_export(request: Request, days: int = Query(30, ge=1, le=365),
                       time_from: str | None = Query(None, alias="from"),
                       time_to: str | None = Query(None, alias="to")):
    """利用明細エクスポート（ZIP・管理者のみ）。画面の期間の回答明細を 1 つの ZIP にまとめて返す。質問・回答の本文・会話タイトル・参照した資料・ツールの引数は含めない（会話番号・回答番号で DB や `make trace` と突き合わせる調査用）。
    期間の指定は `GET /admin/usage/stats` と同じ。エクスポート自体が `admin.usage_exported`（detail は期間だけ）として記録される（記録できなければ応答しない）。
    """
    u = _current_user(request)
    _require_admin(u)
    if (time_from is not None or time_to is not None) and "days" in request.query_params:
        raise HTTPException(422, "days と from/to は同時に指定できません")
    try:
        summary = store.usage_stats(days=days, time_from=time_from, time_to=time_to)
        turn_rows = usage_export_turns(days=days, time_from=time_from, time_to=time_to)
        aux_rows = usage_export_aux_calls(days=days, time_from=time_from, time_to=time_to)
    except store.UsagePeriodError as e:
        raise HTTPException(422, str(e)) from None

    # ZIP は大きくなりうるため、一時ファイル（大きければディスクへ退避）に書いて少しずつ返す。
    buf = tempfile.SpooledTemporaryFile(max_size=10 * 1024 * 1024)
    try:
        usage_export.build_export_zip(
            buf, summary=summary, turn_rows=turn_rows, aux_rows=aux_rows,
            retrieved_at=datetime.now(timezone.utc), app_ver=app_version.current())
        detail = {"days": days} if time_from is None and time_to is None else {"from": time_from, "to": time_to}
        try:
            store.audit(u["uid"], "admin.usage_exported", "usage", None, detail=detail,
                        outcome="success", severity="info")
        except Exception:
            _log.critical("audit write failed for admin.usage_exported – fail-closed")
            raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）") from None
    except BaseException:
        buf.close()
        raise

    def _chunks():
        try:
            buf.seek(0)
            while chunk := buf.read(1024 * 1024):
                yield chunk
        finally:
            buf.close()

    filename = f"usage-detail-{summary['period']['start']}-{summary['period']['end']}.zip"
    return StreamingResponse(_chunks(), media_type="application/zip",
                             headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@audit_usage_router.post("/admin/usage/quality-runs", tags=["管理者:利用統計"],
                         response_model=AdminUsageQualityRunAck)
def admin_usage_quality_run_create(request: Request, body: AdminUsageQualityRunReq):
    """品質採点（1巡 vs 3巡等の正解付き比較）の結果を、集計済みカウントだけで受け取る。質問文・回答本文のフィールドは無く受け付けない。"""
    u = _current_user(request)
    _require_admin(u)
    # `cost_usd` の有限性チェックはアプリコードで行い、pydantic の制約違反にはしない（制約違反にすると 422 ハンドラが inf/nan をエラー本文へ埋め込み、JSON 化で 500 になるため・schemas.py 参照）。
    if body.cost_usd is not None and not (math.isfinite(body.cost_usd) and body.cost_usd >= 0):
        raise HTTPException(422, "cost_usd は有限の非負数のみ指定できます")
    # 登録（INSERT）と監査ログは同一トランザクション（`record_depth_quality_run` 内）で書く（監査だけ失敗して INSERT が残らないようにする）。
    try:
        inserted = store.record_depth_quality_run(
            body.rounds,
            {"correct": body.correct, "wrong_assertion": body.wrong_assertion, "missing": body.missing,
             "regressed": body.regressed, "unrated": body.unrated},
            condition=body.condition, executed_from=body.executed_from, executed_to=body.executed_to,
            cost_usd=body.cost_usd, run_id=body.run_id, audit_actor=u["uid"])
    except ValueError as e:  # `UsagePeriodError` を含む（実行期間・condition の規則違反）。
        raise HTTPException(422, str(e)) from None
    except Exception:
        _log.critical("write failed for admin.usage_quality_run_recorded (insert+audit) – fail-closed")
        raise HTTPException(500, "記録に失敗しました（fail-closed）")
    return AdminUsageQualityRunAck(ok=True, inserted=inserted)
