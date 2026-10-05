"""範囲セレクタ用の `GET /scopes`。
`sherpa.api` を import しない。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from fastapi import APIRouter, Query, Request

from sherpa import scope as scope_mod
from sherpa.deps import _WORLD_PATTERN, _current_user, _resolve_world
from sherpa.schemas import ScopesResponse

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
impact_router = APIRouter()


@impact_router.get("/scopes", tags=["範囲"], response_model=ScopesResponse)
def scopes(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN)):
    """範囲セレクタ用のツリー。world のフォルダ prefix（件数つき）を返す。"""
    _current_user(request)  # ログイン必須（auth 有効時）。
    return scope_mod.scope_tree(_resolve_world(world))
