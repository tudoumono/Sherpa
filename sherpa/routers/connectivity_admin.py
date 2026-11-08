"""管理者の「つながりの確認」（`POST /admin/connectivity/check`）。確認の中身は `sherpa/connectivity.py`。`sherpa.api` を import しない。
設計: docs/design/operations.md「つながりの確認」
"""
from __future__ import annotations

import concurrent.futures
import logging
import threading

from fastapi import APIRouter, HTTPException, Request

from sherpa import connectivity, store
from sherpa.deps import _current_user, _require_admin
from sherpa.ingest import connectivity_relay
from sherpa.schemas import ConnectivityCheckRequest, ConnectivityCheckResponse

_log = logging.getLogger("sherpa")

connectivity_admin_router = APIRouter()

_running = threading.Lock()


@connectivity_admin_router.post("/admin/connectivity/check", tags=["管理者:つながりの確認"],
                                response_model=ConnectivityCheckResponse)
def admin_connectivity_check(req: ConnectivityCheckRequest, request: Request):
    """経路ごとに決まった確認を流す（管理者のみ・同時に 1 本・有料を含む実行は監査に残す）。"""
    u = _current_user(request)
    _require_admin(u)
    if not _running.acquire(blocking=False):
        raise HTTPException(409, "つながりの確認が実行中です。終わってからもう一度お試しください")
    try:
        if req.include_paid:
            try:
                store.audit(u["uid"], "admin.connectivity_check_paid", "connectivity", None,
                            detail={"include_paid": True}, outcome="success", severity="info")
            except Exception:
                _log.critical("audit write failed for admin.connectivity_check_paid – fail-closed")
                raise HTTPException(500, "監査ログの記録に失敗しました（fail-closed）")
        with concurrent.futures.ThreadPoolExecutor(max_workers=3) as pool:  # 4 つの経路を同時に流す
            child = pool.submit(connectivity.run_mcp_child, req.include_paid)
            ingest = pool.submit(connectivity_relay.request_and_wait, req.include_paid)
            codex = pool.submit(connectivity.run_checks, req.include_paid, path="codex", uid=u["uid"])
            api_rows = connectivity.run_checks(req.include_paid)
            mcp_rows, ingest_rows, codex_rows = child.result(), ingest.result(), codex.result()
        return {"paths": [
            {"path": "api", "label": "画面と回答の受付", "results": api_rows},
            {"path": "mcp", "label": "調べものの道具（Codex から呼ぶ）", "results": mcp_rows},
            {"path": "ingest", "label": "取り込みの係", "results": ingest_rows},
            {"path": "codex", "label": "Codex（回答を作る AI の実行）", "results": codex_rows},
        ]}
    finally:
        _running.release()
