"""取り込み run 1 件の「変わったか」だけを返す軽い確認: `GET /ingest/runs/mark`（管理者のみ・読み取りだけ）。
画面が履歴（`GET /ingest/runs`）を読み直すかを決めるために使う。`sherpa.api` を import しない。
設計: docs/design/operations.md「止まった取り込みの見え方」
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request

from sherpa import store
from sherpa.deps import _current_user, _require_admin
from sherpa.grep_tool import valid_world

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
router = APIRouter()


@router.get("/ingest/runs/mark", tags=["資料フォルダ(World)管理"])
def ingest_run_mark(request: Request, world: str | None = Query(None), run_id: int | None = Query(None)):
    """取り込み run 1 件の `id` と `status` だけ。run が無ければ `run=null`。"""
    _require_admin(_current_user(request))
    if world is None or run_id is None or not valid_world(world):
        raise HTTPException(422, "world と run_id が要ります")
    return {"run": store.get_run_mark(world, run_id)}
