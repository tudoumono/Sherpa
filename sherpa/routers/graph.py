"""ナレッジグラフの閲覧・検索エンドポイント（`GET /graph`・`GET /graph/facets`・`GET /graph/search`）。AI は使わない。
`sherpa.api` を import しない。
"""
from __future__ import annotations

import logging

from fastapi import APIRouter, HTTPException, Query, Request, Response
from sherpa import graph_admin
from sherpa.deps import _WORLD_PATTERN, _current_user, _require_admin, _resolve_world, neo4j_session, validated_scope
from sherpa.ingest.world_neo4j import GRAPH_SCHEMA_ERA_USER_MESSAGE, GraphSchemaEraError
from sherpa.preview_service import graph_view
from sherpa.schemas import GraphFacetsResponse, GraphResponse, GraphSearchResponse

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
router = APIRouter()

_log = logging.getLogger("sherpa")
_GRAPH_NODE_LIMIT = 100
_GRAPH_UNAVAILABLE_MESSAGE = "ナレッジグラフを読み込めませんでした。時間をおいてやり直してください"


def _graph_node_limit() -> int:
    """初期グラフの主要ノード上限（次数上位）。"""
    return _GRAPH_NODE_LIMIT


@router.get("/graph", tags=["ナレッジグラフ"], response_model=GraphResponse)
def graph_get(request: Request, response: Response,
              world: str | None = Query(None, pattern=_WORLD_PATTERN),
              limit: int | None = Query(None, ge=0, le=100000)):
    """ナレッジグラフの可視化用データ（nodes/edges・読み取り専用）を返す。
    既定は主要ノードのみ（次数上位・`limit` 未指定は 100 件）。`limit=0` で全件。内容署名の ETag を付け、`If-None-Match` が一致すれば 304 を返す。
    world 世代の確認に失敗した場合は、握り潰さずログ付きの 503 にする。
    """
    _require_admin(_current_user(request))
    wid = _resolve_world(world)
    eff_limit = _graph_node_limit() if limit is None else limit
    try:
        data = graph_view(wid, limit=eff_limit)
    except Exception as e:
        _log.warning("グラフ表示の構築に失敗しました wid=%s", wid, exc_info=True)
        raise HTTPException(503, _GRAPH_UNAVAILABLE_MESSAGE) from e
    sig = data.pop("signature", "")
    token = "all" if eff_limit <= 0 else str(eff_limit)
    etag = f'"g.{sig}.{token}"'  # 内容署名＋表示範囲で表現ごとに一意（決定的）。
    inm = request.headers.get("if-none-match")
    if inm and (inm.strip() == "*" or etag in [t.strip() for t in inm.split(",")]):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"  # 毎回サーバへ再検証させる（304 で軽い）。
    return data


@router.get("/graph/facets", tags=["ナレッジグラフ"], response_model=GraphFacetsResponse)
def graph_facets(request: Request):
    """グラフ検索の選択肢（label/relationship の閉じた語彙）を返す。"""
    _require_admin(_current_user(request))
    return graph_admin.facets()


@router.get("/graph/search", tags=["ナレッジグラフ"], response_model=GraphSearchResponse)
def graph_search(request: Request, world: str | None = Query(None, pattern=_WORLD_PATTERN),
                 relationship: list[str] = Query(default_factory=list),
                 field: str | None = Query(None),
                 value: str | None = Query(None),
                 op: str = Query("eq"),
                 include_deprecated: bool = False,
                 scope_paths: list[str] = Query(default_factory=list),
                 limit: int = Query(200, ge=1, le=1000)):
    """関係種別/属性条件でグラフを検索し、可視化と同じ nodes/edges 形で返す。"""
    _require_admin(_current_user(request))
    w = _resolve_world(world)
    sp = validated_scope(w, scope_paths)
    try:
        with neo4j_session() as s:
            return graph_admin.graph_search(
                s, w, relationship_types=relationship, field=field, value=value, op=op,
                scope_paths=sp, include_deprecated=include_deprecated, limit=limit)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except GraphSchemaEraError as e:
        # 旧世代の実データがある world は、専門用語を使わない平文で 503 にする。
        _log.warning("graph/search がスキーマ世代不一致で失敗（fail-loud・world=%s・stored=%s）", w, e.stored_era)
        raise HTTPException(503, GRAPH_SCHEMA_ERA_USER_MESSAGE) from e


