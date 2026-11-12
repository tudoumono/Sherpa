"""POST /ext/v1/search: エンジン分離検索＋RRF融合（`sherpa.parts.read.fused_search` へ委譲）。
設計: docs/design/external-api.md
"""
from __future__ import annotations

from typing import Literal

from fastapi import Depends, HTTPException, Request
from pydantic import BaseModel, Field, field_validator

from sherpa import path_rules, scope_infer
from sherpa import scope as scope_mod
from sherpa.ext.audit import _AuditScope
from sherpa.ext.auth import _enforce_world_scope, require_api_key
from sherpa.ext.common import _resolve_world_or_error, router
from sherpa.ext.request_id import _REQUEST_ID_OPENAPI_HEADER, _validation_error_response, _XRequestIdIn
from sherpa.parts.read import fused_search


class ExtSearchReq(BaseModel):
    world: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    engines: list[Literal["keyword", "vector", "graph"]] | None = None  # None→keyword+vector
    k: int = Field(default=10, ge=1, le=50)
    scope_paths: list[str] = Field(default_factory=list)  # フォルダ prefix
    # 探す対象。既定 both＝フィルタなし。keyword/vector にのみ適用（graph は言及エッジが資料とコードを繋ぐため非適用）
    layer: Literal["docs", "code", "both"] = "both"
    weights: dict[Literal["keyword", "vector", "graph"], float] | None = None
    # graph エンジンの影響たどりの深さ（keyword/vector は無視）。既定は `impact_service.IMPACT_MAX_DEPTH`。
    # 上限（le）は外部 API の契約値12を下回らないよう `max(12, IMPACT_MAX_DEPTH)`
    depth: int = Field(default=fused_search.IMPACT_MAX_DEPTH, ge=1,
                       le=max(12, fused_search.IMPACT_MAX_DEPTH))
    # graph エンジンで、構造の結果が無いときに資料の grep 由来の推定を足すか（既定 true＝従来どおり足す）。
    # false なら推定を足さない（`coverage.graph.presumed_count` は 0）。keyword/vector は無視。
    include_presumed: bool = True
    # graph エンジンの各経路の辺ごとに返す根拠（`paths[].edges[].sources`）の最大件数。経路の本数には効かない（切った本数は `paths_omitted`）。
    # 0 は根拠を返さず `sources_omitted` に件数だけ返す。keyword/vector は無視。
    evidence_limit: int = Field(default=3, ge=0, le=10)

    @field_validator("weights")
    @classmethod
    def _w_range(cls, v):
        if v is not None and any(not (0 < x <= 10) for x in v.values()):
            raise ValueError("weights は 0 < w <= 10")
        return v


class ExtFromDef(BaseModel):
    """参照元の定義。`key` は定義のキー（`null` はそのファイルの主体）。"""
    file: str
    key: str | None


class ExtEdgeSource(BaseModel):
    """辺（つながり）の根拠 1 件。参照元の文書・行・関係の種類・接続を決めた解決規則。言及の辺は `locator`（文書内の位置）を持つ。"""
    via: str | None = None
    doc_id: str
    file: str
    line: int
    rule: str | None = None
    from_def: ExtFromDef | None = None
    locator: str | None = None
    evidence_text: str | None = None


class ExtGraphEdge(BaseModel):
    """経路の辺 1 本。`via` は関係の種類・`rule` は代表の根拠の解決規則・`sources` は根拠（先頭 `evidence_limit` 件）・
    `sources_omitted` は返さなかった根拠の件数（保存の上限を超えた分を含む）。根拠のある辺だけが `rule`・`sources`・`sources_omitted` を持つ。"""
    type: str
    doc: str | None = None
    line: int | None = None
    via: str | None = None
    rule: str | None = None
    sources: list[ExtEdgeSource] | None = None
    sources_omitted: int | None = None


class ExtGraphPath(BaseModel):
    nodes: list[str]
    edges: list[ExtGraphEdge]


class ExtHit(BaseModel):
    doc_id: str | None
    path: str | None
    line: int | None = None
    snippet: str
    score: float
    sources: dict[str, int]
    paths: list[ExtGraphPath] | None = None
    # 経路を 5 本で切ったときだけ、切った本数（`paths` に載せなかった経路の数）。
    paths_omitted: int | None = None
    # graph で見つかったヒットだけ。`structure`＝構造の辺をたどって見つかった・`presumed`＝構造の結果が無いときの資料からの推定。
    # 確からしさの格付けではない（keyword/vector との統合は `sources` が表す）。
    graph_origin: list[Literal["structure", "presumed"]] | None = None


class ExtDegraded(BaseModel):
    engine: Literal["keyword", "vector", "graph"]
    reason: str
    # 失敗の詳細（`coverage.limits[].kind` と同じ語: `timeout`・`row_cap`・`graph_unavailable`・`graph_reingest_required`）。
    # `reason` の値は増やさない。詳細が無い失敗では出ない。
    detail: str | None = None


class ExtLimit(BaseModel):
    kind: Literal["timeout", "row_cap", "depth", "result_cap", "doc_search_truncated", "card_cap",
                  "graph_unavailable", "graph_reingest_required", "plugin_failed", "source_unparsed"]
    count: int | None = None  # `source_unparsed` のときだけ、グラフに入れなかったソースのファイル数


class ExtDepth(BaseModel):
    requested: int
    truncated: bool | None  # 深さの上限で止まり、先に辺が残るか（先の判定が時間内に終わらなかったときは null）


class ExtEngineCoverage(BaseModel):
    """成功したエンジン 1 つの調べ切れ具合。失敗したエンジンは持たない（`degraded` が伝える）。"""
    complete: bool  # `limits` が 1 つでもあれば false
    requested_k: int
    returned: int
    omitted: int | None = None  # そのエンジンが `k` で切って返さなかった件数（数えられないときは null）
    limits: list[ExtLimit]
    depth: ExtDepth | None = None  # graph だけ
    structural_count: int | None = None  # graph だけ（`k` で切る前の件数）
    presumed_count: int | None = None  # graph だけ


class ExtFusedCoverage(BaseModel):
    requested_k: int
    returned: int
    omitted_by_cut: int  # 融合した候補のうち、最終の `k` で切った件数（エンジン別の `omitted` とは足し合わせない）


class ExtCoverage(BaseModel):
    """使ったエンジンだけがキーになる。"""
    keyword: ExtEngineCoverage | None = None
    vector: ExtEngineCoverage | None = None
    graph: ExtEngineCoverage | None = None
    fused: ExtFusedCoverage | None = None


class ExtSearchRes(BaseModel):
    world: str
    query: str
    hits: list[ExtHit]
    engines_used: list[str]
    degraded: list[ExtDegraded]
    coverage: ExtCoverage | None = None


_SEARCH_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "資料フォルダ（world）が見つかりません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("不明な範囲（scope_paths）が指定された場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "資料フォルダの参照先を確認できませんでした（一時的な障害）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


# `response_model_exclude_unset`: 追加の省略可の項目（`coverage`・`graph_origin`・`degraded[].detail`）は、値が無いとき null で出さず欄ごと省く
# （既存のクライアントへ見える形を増やさない。既存の項目は常に値を持つので変わらない）。
@router.post("/search", response_model=ExtSearchRes, response_model_exclude_unset=True,
             responses=_SEARCH_RESPONSES)
def ext_search(req: ExtSearchReq, request: Request, key: dict = Depends(require_api_key),
               x_request_id: str | None = _XRequestIdIn):
    """RAG検索（エンジン分離＋RRF融合）。共有 KB のみ・個人 workspace は対象外。"""
    del x_request_id
    with _AuditScope(request, "ext_api.search", "ext_search") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200],
                             "engines": req.engines or list(fused_search.DEFAULT_ENGINES),
                             "k": req.k, "depth": req.depth, "layer": req.layer})
        if not req.include_presumed:
            audit.detail["include_presumed"] = False  # 既定と違う要求の欄だけ残す
        if req.evidence_limit != 3:
            audit.detail["evidence_limit"] = req.evidence_limit
        # scope 確認・world 解決・scope_paths 検証より前に正規化して積む（どの経路で失敗しても監査に world・prefix が残る）
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        _enforce_world_scope(request, key, req.world)  # scope 外は世界の存在有無を明かさず先に 403
        root = _resolve_world_or_error(req.world)  # strict 解決は1回だけ・以降これを使い回す
        try:
            # `valid_scope_paths(strict=True)` は OSError を re-raise しうるため、search() 本体と同じ try/except に含めて 503 にする
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True):
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
            extra = {} if req.include_presumed else {"include_presumed": False}
            if req.evidence_limit != 3:
                extra["evidence_limit"] = req.evidence_limit
            res = fused_search.search(req.world, req.query, engines=req.engines, k=req.k,
                                        scope_paths=req.scope_paths, weights=req.weights,
                                        depth=req.depth, root=root, strict=True, layer=req.layer, **extra)
        except scope_infer.ScopeWalkDeadlineExceeded:
            raise HTTPException(503, "資料フォルダの一覧を時間内に取得できませんでした。取り込みが終わってから、もう一度お試しください") from None
        except OSError as e:
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        audit.detail["result_count"] = len(res["hits"])
        audit.detail["degraded"] = [d["reason"] for d in res["degraded"]]
        complete = {e: c["complete"] for e, c in (res.get("coverage") or {}).items() if e != "fused"}
        if not all(complete.values()):
            # 打ち切りのあったエンジンがあるときだけ、エンジンごとの調べ切れ（`complete`）を残す（既定の要求の詳細は今と同じ）
            audit.detail["coverage_complete"] = complete
        return path_rules.filter_output({"world": req.world, "query": req.query, **res}, req.world)  # 外へ出る境界の最後の網（管理者の除外の資料を落とす）
