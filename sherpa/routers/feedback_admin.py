"""管理者向けの回答への評価（`GET /admin/feedback/summary`・`GET /admin/feedback/items`）。集計・一覧の組み立ては `sherpa/improvement_log.py`。`sherpa.api` を import しない。
設計: docs/design/usage.md「回答への評価（管理者の画面）」
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request

from sherpa import improvement_log, store
from sherpa.deps import _current_user, _require_admin
from sherpa.schemas import AdminFeedbackItemsResponse, AdminFeedbackSummaryResponse

_log = logging.getLogger("sherpa")

feedback_admin_router = APIRouter()


@feedback_admin_router.get("/admin/feedback/summary", tags=["管理者:回答への評価"],
                           response_model=AdminFeedbackSummaryResponse)
def admin_feedback_summary(request: Request, days: int = Query(30, ge=1, le=365)):
    """期間内の評価の集計（管理者のみ）。母集団は改善ログの書き出しと同じ。"""
    _require_admin(_current_user(request))
    return improvement_log.summarize_feedback(time_from=datetime.now(timezone.utc) - timedelta(days=days))


@feedback_admin_router.get("/admin/feedback/items", tags=["管理者:回答への評価"],
                           response_model=AdminFeedbackItemsResponse)
def admin_feedback_items(
    request: Request,
    days: int = Query(30, ge=1, le=365),
    rating: str = Query("all", pattern="^(all|up|down)$"),
    tag: str | None = Query(None),
    limit: int = Query(50, ge=1, le=200),
    before: int | None = Query(None, ge=1),
):
    """期間内の評価を新しい順に返す（管理者のみ）。閲覧のたびに監査へ残し（件数と絞り込み条件だけ）、記録できなければ返さない（500）。"""
    u = _current_user(request)
    _require_admin(u)
    if tag is not None and tag not in store.MESSAGE_FEEDBACK_TAGS:
        raise HTTPException(422, "タグが不正です")
    items, next_before = improvement_log.fetch_feedback_items(
        time_from=datetime.now(timezone.utc) - timedelta(days=days),
        rating=None if rating == "all" else rating, tag=tag, before_id=before, limit=limit)
    try:
        store.audit(u["uid"], "admin.feedback_viewed", "feedback", None,
                    detail={"days": days, "rating": rating, "tag": tag, "limit": limit,
                            "result_count": len(items)},
                    outcome="success", severity="info")
    except Exception:
        _log.critical("audit write failed for admin.feedback_viewed – fail-closed")
        raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")
    return {"items": items, "next_before": next_before}
