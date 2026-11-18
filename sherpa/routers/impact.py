"""範囲セレクタ用の `GET /scopes`。
`sherpa.api` を import しない。
設計: docs/design/scope.md「リンクの解決：構造エッジ・対応エッジ・言及エッジ」
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request, Response

from sherpa import scope as scope_mod
from sherpa import scope_infer
from sherpa.deps import SCOPE_WALK_TIMEOUT_MESSAGE, _WORLD_PATTERN, _current_user, _resolve_world
from sherpa.schemas import ScopesResponse

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
impact_router = APIRouter()


def _etag_matches(header: str | None, etag: str) -> bool:
    """`If-None-Match` に `etag` が含まれるか（`*`・弱い比較の `W/` 接頭辞を許す）。"""
    if not header:
        return False
    return any(t.strip() in ("*", etag, "W/" + etag) for t in header.split(","))


@impact_router.get("/scopes", tags=["範囲"], response_model=ScopesResponse)
def scopes(request: Request, response: Response, world: str | None = Query(None, pattern=_WORLD_PATTERN)):
    """範囲セレクタ用のツリー。world のフォルダ prefix（件数つき）を返す。取り込みの台帳から出し、台帳が無いときだけ資料フォルダを（期限つきで）歩く。
    台帳の変更印から作る ETag を付け、`If-None-Match` が一致すれば本文を作らず 304 を返す（`Cache-Control: private, no-cache`＝毎回確かめる）。"""
    _current_user(request)  # ログイン必須（auth 有効時）。
    w = _resolve_world(world)
    etag = scope_mod.scope_tree_etag(w)  # 守ること: 本文より先に取る（本文が新しい分には、次回の再取得で済む）
    if etag and _etag_matches(request.headers.get("if-none-match"), etag):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "private, no-cache"})
    try:
        tree = scope_mod.scope_tree(w)
    except scope_infer.ScopeWalkDeadlineExceeded:
        raise HTTPException(503, SCOPE_WALK_TIMEOUT_MESSAGE) from None
    if etag:
        response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "private, no-cache"
    return tree
