"""ナレッジグラフ エンドポイント（フェーズ3スライス5・純移動）。

`GET /graph`・`GET /graph/facets`・`GET /graph/search`・`POST /graph/ask`（＋専用モデル
`GraphAskReq`）を api.py から抽出する。`graph_ask` 専用の read-only ヘルパ
`_knowledge_status_summary` も物理的には worlds 系ハンドラ群の間（`_ingest_summary` と
`/worlds/{wid}/status` の間）に置かれていたが graph 専用なのでこのモジュールへ純移動する。
ロジックは変更しない（コード移動のみ）。

4ルートは定義順で連続（`/worlds/{wid}`（DELETE）の直後・`/ingest/rerun` の直前）のため router
は1本で足りる。ルート表 golden の定義順を保つため、api.py 側は削除したブロックの元位置に
`app.include_router(graph.router)` を1回だけ置く。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import logging
import os

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel, Field

from sherpa import doc_ledger, graph_admin, store
from sherpa import scope as scope_mod
from sherpa.deps import _WORLD_PATTERN, _WorldField, _current_user, _require_admin, _resolve_world, neo4j_session, validated_scope
from sherpa.ingest.world_neo4j import GRAPH_SCHEMA_ERA_USER_MESSAGE, GraphSchemaEraError
from sherpa.preview_service import graph_view
from sherpa.schemas import GraphAskResponse, GraphFacetsResponse, GraphResponse, GraphSearchResponse

# router に tags を持たせない: 各エンドポイントの `tags=["ナレッジグラフ"]` と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
router = APIRouter()

_log = logging.getLogger("sherpa")
_GRAPH_UNAVAILABLE_MESSAGE = "ナレッジグラフを読み込めませんでした。時間をおいてやり直してください"


class GraphAskReq(BaseModel):
    question: str
    world: str | None = _WorldField
    scope_paths: list[str] = Field(default_factory=list)


def _knowledge_status_summary(wid: str, scope_paths: list[str] | None = None) -> dict:
    """グラフ質問用の read-only ナレッジ状況サマリ。"""
    sp = scope_mod.normalize_scope_paths(scope_paths or [])
    docs = doc_ledger.public_documents(wid)
    if sp:
        docs = [d for d in docs if scope_mod.in_scope(d.get("name") or "", sp)]
    g = graph_view(wid)
    all_nodes = g.get("nodes") or []
    all_edges = g.get("edges") or []

    def _node_in_scope(n: dict) -> bool:
        if not sp:
            return True
        for key in ("path", "name", "top_scope"):
            v = n.get(key)
            if v and scope_mod.in_scope(str(v), sp):
                return True
        return False

    nodes = [n for n in all_nodes if _node_in_scope(n)]
    node_ids = {n.get("id") for n in nodes}
    edges = [e for e in all_edges if e.get("source") in node_ids and e.get("target") in node_ids]
    degree = {nid: 0 for nid in node_ids}
    for e in edges:
        degree[e.get("source")] = degree.get(e.get("source"), 0) + 1
        degree[e.get("target")] = degree.get(e.get("target"), 0) + 1
    isolated = [n for n in nodes if degree.get(n.get("id"), 0) == 0]

    node_paths = {str(n.get("path") or "") for n in nodes if n.get("path")}
    weak_docs = []
    for d in docs:
        name = d.get("name") or ""
        if not any(name == p or p.startswith(name + "/") or name.startswith(p + "/") for p in node_paths):
            weak_docs.append(d)

    try:
        runs = store.list_ingest_runs(wid, limit=10)
    except Exception:
        runs = []
    errors = [r for r in runs if r.get("status") == "failed"]

    return {
        "world": wid,
        "scope_paths": sp,
        "documents": len(docs),
        "graph_nodes": len(nodes),
        "graph_edges": len(edges),
        "isolated_node_count": len(isolated),
        "isolated_nodes": [
            {"name": n.get("name"), "type": n.get("type"), "path": n.get("path")}
            for n in isolated[:12]
        ],
        "weak_document_count": len(weak_docs),
        "weak_documents": [
            {"name": d.get("name"), "doctype": d.get("doctype"), "category": d.get("category")}
            for d in weak_docs[:12]
        ],
        "recent_ingest_errors": [
            {"id": r.get("id"), "status": r.get("status"), "created_at": str(r.get("created_at"))}
            for r in errors[:5]
        ],
    }


def _graph_node_limit() -> int:
    """初期グラフの主要ノード上限（次数上位・段階読み込み・②2026-07-08）。env で調整可・既定100。

    既定 100 の根拠: cose レイアウト（animate:true）＋ラベル描画は数百ノードで体感が落ちる。まず全体の
    骨格（次数の高い中心＝主要な繋がり）を軽く見せ、細部は検索/近傍展開/「すべて表示」で辿る設計。"""
    try:
        v = int(os.environ.get("SHERPA_GRAPH_NODE_LIMIT", "100"))
        return v if v > 0 else 100
    except ValueError:
        return 100


@router.get("/graph", tags=["ナレッジグラフ"], response_model=GraphResponse)
def graph_get(request: Request, response: Response,
              world: str | None = Query(None, pattern=_WORLD_PATTERN),
              limit: int | None = Query(None, ge=0, le=100000)):
    """ナレッジグラフの可視化用データ（nodes/edges・read-only）。

    既定は**主要ノードのみ**（次数上位・`limit` 未指定＝env `SHERPA_GRAPH_NODE_LIMIT`）。`limit=0` で
    全件（「すべて表示」）。内容署名の ETag を付け、`If-None-Match` 一致なら 304（再転送しない）。

    `graph_view` は world 世代のプローブ（`store.get_world_status_row`）を read-only キャッシュの
    鍵として読む（GRA-1）——ここが失敗（DB 不調等）した場合は握り潰さずログ付き 503 にする
    （silent degradation なしの家風どおり・旧キャッシュ流用や自動リトライはしない）。
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
    etag = f'"g.{sig}.{token}"'                        # 内容署名＋表示範囲＝表現ごとに一意（決定的）
    inm = request.headers.get("if-none-match")
    if inm and (inm.strip() == "*" or etag in [t.strip() for t in inm.split(",")]):
        return Response(status_code=304, headers={"ETag": etag, "Cache-Control": "no-cache"})
    response.headers["ETag"] = etag
    response.headers["Cache-Control"] = "no-cache"     # 毎回サーバへ再検証させる（304 で軽い）
    return data


@router.get("/graph/facets", tags=["ナレッジグラフ"], response_model=GraphFacetsResponse)
def graph_facets(request: Request):
    """管理グラフ検索の選択肢（閉じた label/relationship 語彙）。"""
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
    """関係種別/属性条件で Neo4j グラフを検索し、可視化と同じ nodes/edges 形で返す。"""
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
        # rv-s3-removal: 旧世代の実データがある world（fail-loud）。専門用語ゼロの平文で 503。
        _log.warning("graph/search がスキーマ世代不一致で失敗（fail-loud・world=%s・stored=%s）", w, e.stored_era)
        raise HTTPException(503, GRAPH_SCHEMA_ERA_USER_MESSAGE) from e


@router.post("/graph/ask", tags=["ナレッジグラフ"], response_model=GraphAskResponse,
            responses={503: {"description": _GRAPH_UNAVAILABLE_MESSAGE}})
def graph_ask(req: GraphAskReq, request: Request):
    """管理グラフへの自然言語質問。既存 graph_neighbors tool だけを使う read-only 経路。

    質問応答の前段（`_knowledge_status_summary` 経由の `graph_view`）が失敗したら、AI/Neo4j 側の
    障害と混同しない専用文言（`/graph` と同じ `_GRAPH_UNAVAILABLE_MESSAGE`）でログ付き 503 にする
    （GRA-1是正RV2#1）。
    """
    u = _current_user(request)
    _require_admin(u)
    w = _resolve_world(req.world)
    sp = validated_scope(w, req.scope_paths)
    try:
        summary = _knowledge_status_summary(w, sp)
    except ValueError as e:
        raise HTTPException(422, str(e))
    except Exception as e:
        _log.warning("グラフ状態サマリの取得に失敗しました wid=%s", w, exc_info=True)
        raise HTTPException(503, _GRAPH_UNAVAILABLE_MESSAGE) from e
    try:
        return graph_admin.ask_graph(req.question, w, scope_paths=sp,
                                     settings=store.get_settings(u["uid"]),
                                     status_summary=summary, user_id=u["uid"])
    except ValueError as e:
        raise HTTPException(422, str(e))
