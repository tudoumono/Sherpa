"""読み取り部品: エンジン分離検索＋RRF 融合。keyword（ES BM25）／vector（ES 純 kNN）／graph（Neo4j 影響たどり）を分離し、融合は RRF 固定。
共有 KB のみ（個人 workspace・personal テーブルは参照しない）。`sherpa.ext_api` から呼ぶ。
`sherpa.api`・`sherpa.agents`・`sherpa.chat_service`・`sherpa.chat_router`・`sherpa.grep_tool` を import しない（プロセス分離可能な境界）。
設計: docs/design/rag.md「融合検索（RRF）」
"""
from __future__ import annotations

from contextlib import contextmanager

from ... import documents, es_index, graph_coverage, rag_parent_return
from ... import scope as scope_mod
from ...impact_service import IMPACT_MAX_DEPTH, run_impact
from ...ingest import text_kind

ENGINES = ("keyword", "vector", "graph")
DEFAULT_ENGINES = ("keyword", "vector")  # 既定は ES 1 往復系のみ（graph は明示 opt-in）
RRF_K = 60  # RRF 定数（固定）
_SNIPPET_LEN = 240  # es_index の fragment_size と揃える
_JUDGEMENT_FACTOR = {"sure": 1.0, "review": 0.6, "presumed": 0.3}  # graph の RRF 寄与係数
_MAX_PATHS_PER_HIT = 5  # 同一 key に複数 graph item が落ちた時の paths 上限
_JUDGE_RANK = {"sure": 0, "review": 1, "presumed": 2}  # judgement の優劣（小さいほど良い）

DEGRADE_REASONS = frozenset({
    "es_unavailable", "es_query_failed", "es_query_rejected", "embedding_not_configured",
    "embedding_cloud_unavailable",  # A7 で明示選択したクラウドの埋め込みが解決できない
    "hybrid_query_failed",  # hybrid 自体が失敗し BM25 は成功（hits は空でない）
    "vector_feature_mismatch", "query_embed_failed",
    "neo4j_unavailable", "graph_query_failed",
    "graph_reingest_required",  # 旧世代グラフ（GraphSchemaEraError）
})


class DegradeReason(str):
    """エンジンの失敗理由（閉じた `DEGRADE_REASONS` の値そのもの）。`detail` に打ち切りの種類（`graph_coverage` の `kind`）を持つ。"""

    detail: str | None

    def __new__(cls, reason: str, detail: str | None = None):
        obj = super().__new__(cls, reason)
        obj.detail = detail
        return obj


class GraphHits(list):
    """`_graph_hits` が返すヒットの `list`。融合前の件数と、`run_impact` の `coverage` を持つ。"""

    total: int = 0                  # `k` で切る前の件数（同じ key を統合した後）
    structural_count: int = 0       # うち構造の辺をたどって見つかった件数
    presumed_count: int = 0         # うち構造の結果が無いときの推定の件数
    run_coverage: dict | None = None  # `run_impact` 結果の `coverage`（深さ・推定 grep の打ち切り）
    all_hits: list = []             # `k` で切る前の全ヒット（実在フィルタの後に数え直すため）


def search(world: str, query: str, engines=None, k: int = 10,
           scope_paths=None, weights=None, settings: dict | None = None, depth: int = IMPACT_MAX_DEPTH,
           root=None, strict: bool = False, layer=None, include_presumed: bool = True,
           evidence_limit: int | None = None) -> dict:
    """公開エントリ。返値 `{hits, engines_used, degraded: [{engine, reason, detail?}], coverage}`。
    - engines: ENGINES の部分集合（None→DEFAULT_ENGINES）。順序・重複は正規化する。
    - depth: graph エンジンの影響たどりの深さ（`run_impact` へ渡す。keyword／vector は無視）。
    - include_presumed: 真（既定）なら、構造の結果が無いときに grep 由来の推定を graph の結果へ足す。偽なら足さない。
    - evidence_limit: graph の各経路の辺ごとに返す根拠（`sources`）の最大件数（省略時 3・0〜10）。切った分は辺の `sources_omitted`。経路の数には効かない。
    - coverage: 成功したエンジンだけに `{complete, requested_k, returned, omitted, limits, ...}`、融合に `fused`。
      失敗したエンジンは `coverage` を持たず、`degraded[].detail`（`timeout`・`row_cap` など）で理由を伝える。
      keyword／vector は ES の上位 `k` 件で、`k` を超える一致の件数が ES から返らないため `omitted` は null。
    - layer（`"docs"|"code"|"both"`・既定 both）: keyword／vector にのみ適用する。graph は言及エッジが木を跨ぐため非適用。
    - root: 呼び出し側が解決済みの資料フォルダ root を渡すと、実在フィルタ（`documents.world_rel_set`）が再解決しない。
    - strict: `root` 指定時のみ有効。`safe_files` の OSError を re-raise する（呼び出し側が 503 にする）。
    - scope_paths は `scope_mod.normalize_scope_paths` で正規化（全エンジン共通のフォルダフィルタ）。
    - 資料フォルダ・範囲の妥当性検証は呼び出し側（ext_api）の責務。
    - 各エンジンは (hits, reason|None) を返す。reason ありなら degraded に積み、hits は空扱い。engines_used は degrade しなかったエンジン。
    - 実在フィルタ: doc_id 付きヒットのうち現資料フォルダに実在しないものを落とす（doc_id 無しは素通り）。
      パスの存在しか見ないため、rebind 後の再索引が未完了だと同じ相対パスの古いヒットが通りうる。
    """
    sel = [e for e in ENGINES if e in set(engines or DEFAULT_ENGINES)]  # 正規化（決定的順序）
    sp = scope_mod.normalize_scope_paths(scope_paths)
    w = {e: float((weights or {}).get(e, 1.0)) for e in sel}
    valid = (documents.world_rel_set(world, root=root, strict=strict) if root is not None
             else documents.world_rel_set(world))
    per_engine, degraded, coverage = {}, [], {}
    runners = {"keyword": _search_keyword, "vector": _search_vector, "graph": _search_graph}
    for e in sel:
        # graph だけ depth（と、既定と違うときの include_presumed）を渡し、keyword／vector は layer を渡す
        if e == "graph":
            extra = {} if include_presumed else {"include_presumed": False}
            if evidence_limit is not None:
                extra["evidence_limit"] = evidence_limit
            hits, reason = runners[e](world, query, sp, k, depth, **extra)
        else:
            hits, reason = runners[e](world, query, sp, k, settings, layer)
        if reason:
            entry = {"engine": e, "reason": str(reason)}
            if getattr(reason, "detail", None):
                entry["detail"] = reason.detail
            degraded.append(entry)
        else:
            graph_info = hits if isinstance(hits, GraphHits) else None
            if graph_info is not None:
                # 実在フィルタの後の件数で数える（今の資料に無いヒットは省略件数・由来の件数に含めない）
                hits = _filter_existing(graph_info.all_hits or list(hits), valid)
                hits = _dedupe_by_key(hits)
                graph_info.total = len(hits)
                graph_info.structural_count = sum(1 for h in hits if "structure" in h.get("graph_origin", []))
                graph_info.presumed_count = sum(1 for h in hits if "presumed" in h.get("graph_origin", []))
            else:
                hits = _filter_existing(hits, valid)
            per_engine[e] = _dedupe_by_key(hits)[:k]  # エンジン内は doc キーで先勝ち dedupe
            coverage[e] = _engine_coverage(e, k, len(per_engine[e]), graph_info, depth)
    fused = fuse_rrf(per_engine, w, k)
    distinct = len({h["key"] for hs in per_engine.values() for h in hs})
    coverage["fused"] = {"requested_k": k, "returned": len(fused), "omitted_by_cut": max(0, distinct - len(fused))}
    return {"hits": fused, "engines_used": sorted(per_engine.keys(), key=ENGINES.index),
            "degraded": degraded, "coverage": coverage}


def _engine_coverage(engine: str, k: int, returned: int, graph_info: "GraphHits | None", depth: int) -> dict:
    """成功したエンジン 1 つの `coverage`。外部 API の `limits[].kind` は結果が返った上での打ち切り（`depth`・`result_cap`・`doc_search_truncated`）だけ。"""
    out: dict = {"complete": True, "requested_k": k, "returned": returned, "omitted": None, "limits": []}
    if engine != "graph" or graph_info is None:
        return out
    run = graph_info.run_coverage or {}
    limits = [{"kind": lim["kind"], **({"count": lim["count"]} if isinstance(lim.get("count"), int) else {})}
              for lim in run.get("limits", [])
              if lim["kind"] in (graph_coverage.KIND_DEPTH, graph_coverage.KIND_DOC_SEARCH_TRUNCATED,
                                   graph_coverage.KIND_TIMEOUT, graph_coverage.KIND_ROW_CAP,
                                   graph_coverage.KIND_GRAPH_UNAVAILABLE, graph_coverage.KIND_RESULT_CAP,
                                   graph_coverage.KIND_PLUGIN_FAILED, graph_coverage.KIND_SOURCE_UNPARSED)]
    cut = max(0, graph_info.total - k)
    if cut and {"kind": graph_coverage.KIND_RESULT_CAP} not in limits:
        limits.append({"kind": graph_coverage.KIND_RESULT_CAP})
    out.update(complete=not limits, omitted=cut, limits=limits)
    if run.get("depth"):
        out["depth"] = dict(run["depth"])
    out["structural_count"] = graph_info.structural_count
    out["presumed_count"] = graph_info.presumed_count
    return out


def _filter_existing(hits: list, valid: set) -> list:
    """`doc_id` を持つヒットのうち現資料フォルダに実在しないものを落とす（doc_id 無しは素通り）。"""
    return [h for h in hits if not h.get("doc_id") or h["doc_id"] in valid]


def _dedupe_by_key(hits: list) -> list:
    """`hit["key"]` で先勝ち dedupe（エンジン内の重複排除）。"""
    seen: set = set()
    out = []
    for h in hits:
        if h["key"] in seen:
            continue
        seen.add(h["key"])
        out.append(h)
    return out


# ==== 各エンジン（内部関数・全て (hits, reason|None) を返す）====
# 共通ヒット中間形: {key, doc_id, path, line, snippet, engine_score, judgement, paths}
# - key: doc_id があれば doc_id（=rel_path）、無ければ f"graph:{label}:{name}"
# - engine_score: エンジン固有スコア（参考値・融合には使わない）

def _search_keyword(world, query, sp, k, settings, layer=None):
    """ES BM25（kuromoji）。`es_index.search(vector=False)` を使う。BM25 自体の失敗は reason として呼び出し元の degraded へ伝える。"""
    if not es_index.available():
        return [], "es_unavailable"
    hits, reason = es_index.search(world, query, scope_paths=sp, k=k, settings=settings, vector=False,
                                   layer=layer)
    hits = _exclude_sensitive(hits)
    hits = rag_parent_return.apply_to_hits(world, hits)
    return _parse_hits(hits), reason


def _search_vector(world, query, sp, k, settings, layer=None):
    """ES 純 kNN（BM25 を混ぜない・`es_index.search_knn_only`）。"""
    hits, reason = es_index.search_knn_only(world, query, scope_paths=sp, k=k, settings=settings,
                                            layer=layer)
    hits = _exclude_sensitive(hits)
    hits = rag_parent_return.apply_to_hits(world, hits)
    return _parse_hits(hits), reason


def _exclude_sensitive(hits) -> list:
    """秘匿名（`text_kind.is_sensitive_doc_id`）を `rag_parent_return.apply_to_hits` の前で落とす（親返しが秘匿本文を読む前に除く）。"""
    return [h for h in hits if not text_kind.is_sensitive_doc_id(h["doc_id"])]


def _parse_hits(hits) -> list[dict]:
    """ES ヒット list → 共通ヒット中間形の list。秘匿名はここでも除外する（`_exclude_sensitive` を経由し損ねても本文を出さない）。"""
    return [_es_hit(h) for h in hits if not text_kind.is_sensitive_doc_id(h["doc_id"])]


def _es_hit(h) -> dict:
    out = {"key": h["doc_id"], "doc_id": h["doc_id"], "path": h["doc_id"],
           "line": h.get("line"), "snippet": (h.get("text") or "")[:_SNIPPET_LEN],
           "engine_score": h.get("score"), "judgement": None, "paths": None}
    # 親返しで拡張できた場合だけ `tier`／`text` を加算する（`snippet`＝240 字クリップは不変）
    if h.get("tier"):
        out["tier"] = h["tier"]
        out["text"] = h.get("text")
    return out


@contextmanager
def _neo4j_session():
    """fused_search 専用の Neo4j セッション（`sherpa.api.neo4j_session` は循環のため import しない）。接続情報は `world_neo4j._env()` と同じ。"""
    from neo4j import GraphDatabase

    from ...ingest.world_neo4j import _env
    e = _env()
    drv = GraphDatabase.driver(e["uri"], auth=(e["user"], e["pw"]),
                               notifications_min_severity="OFF")
    try:
        with drv.session() as s:
            yield s
    finally:
        drv.close()


def _search_graph(world, query, sp, k, depth=IMPACT_MAX_DEPTH, include_presumed=True, evidence_limit=None):
    """語→ノード照合→近傍展開→文書＋経路。`run_impact`（構造たどり＋presumed フォールバック）をそのまま使い、読むだけ。depth は影響たどりの深さ。
    失敗理由は `DegradeReason`（`detail`: 過負荷なら `timeout`／`row_cap`・接続不可なら `graph_unavailable`・旧世代なら `graph_reingest_required`）。
    """
    from neo4j.exceptions import AuthError, ServiceUnavailable

    from ...ingest.world_neo4j import GraphQueryOverloadError, GraphSchemaEraError
    kw = {} if include_presumed else {"include_presumed": False}
    if evidence_limit is not None:
        kw["evidence_limit"] = evidence_limit
    try:
        with _neo4j_session() as s:
            result = run_impact(s, query, world, scope_prefixes=(sp or None), depth=depth, **kw)
    except (ServiceUnavailable, AuthError, OSError):
        return [], DegradeReason("neo4j_unavailable", graph_coverage.KIND_GRAPH_UNAVAILABLE)
    except GraphSchemaEraError:
        # 旧世代グラフは generic な理由に丸めず、再取り込み案内用の閉じた理由を返す
        return [], DegradeReason("graph_reingest_required", graph_coverage.KIND_GRAPH_REINGEST_REQUIRED)
    except GraphQueryOverloadError as e:
        return [], DegradeReason("graph_query_failed", graph_coverage.kind_of_overload(e.reason))
    except Exception:
        return [], "graph_query_failed"
    return _graph_hits(result, k), None


# ==== graph の run_impact 結果 → ヒット形のマッピング ====

def _ext_edge(e: dict) -> dict:
    """経路の辺（`world_neo4j.edge_view` の形）→ 外部 API の辺。切った根拠の件数 `sources_overflow_count` は `sources_omitted` の名前で返す。"""
    out = {k: v for k, v in e.items() if k != "sources_overflow_count"}
    if "sources" in e:
        out["sources_omitted"] = int(e.get("sources_overflow_count") or 0)
    return out


def _graph_item_hit(item: dict) -> dict:
    """items[]（構造的な影響・全件同格）を共通ヒット形へ。"""
    name = item.get("name")
    label = item.get("label")
    category = item.get("category") or label
    path = item.get("path")
    evidence = [_ext_edge(e) for e in (item.get("evidence") or [])]
    doc_id = path or (evidence[0].get("doc") if evidence else None)
    key = doc_id if doc_id else f"graph:{label}:{name}"
    trace = item.get("trace") or []
    snippet = f"{category}「{name}」"
    if trace:
        snippet += " 経路: " + " → ".join(trace)
    return {"key": key, "doc_id": doc_id, "path": path, "line": None,
            "snippet": snippet[:_SNIPPET_LEN], "engine_score": None, "judgement": None,
            "graph_origin": ["structure"],
            "paths": [{"nodes": trace or [], "edges": evidence or [], "judgement": None}]}


def _presumed_item_hit(item: dict) -> dict:
    """presumed[]（確実が 0 件の時の資料からの推定）を共通ヒット形へ。"""
    name = item.get("name")
    label = item.get("label")
    category = item.get("category") or label
    path = item.get("path")
    evidence = item.get("evidence") or []
    ev0 = evidence[0] if evidence else {}
    doc_id = path or ev0.get("doc")
    key = doc_id if doc_id else f"graph:{label}:{name}"
    quote = ev0.get("quote") or ""
    snippet = f"{category}「{name}」（判定: 推定）資料根拠: {quote}"
    return {"key": key, "doc_id": doc_id, "path": path, "line": None,
            "snippet": snippet[:_SNIPPET_LEN], "engine_score": None, "judgement": "presumed",
            "graph_origin": ["presumed"],
            "paths": [{"nodes": [name], "edges": [{"type": "PRESUMED", "doc": ev0.get("doc"),
                                                    "line": ev0.get("line")}],
                      "judgement": "presumed"}]}


def _union_origin(a, b) -> list:
    """由来（`structure`／`presumed`）の和集合（固定順）。"""
    got = set(a or []) | set(b or [])
    return [o for o in ("structure", "presumed") if o in got]


def _merge_graph_item(merged: dict, order: list, h: dict) -> None:
    """同一 key に複数 item が落ちる場合: 先勝ちで 1 ヒットに統合し、後続の paths を追記（上限 5 経路・切った本数は `paths_omitted`）。judgement は最良（sure > review > presumed）。"""
    key = h["key"]
    if key not in merged:
        merged[key] = h
        order.append(key)
        return
    m = merged[key]
    combined = (m["paths"] or []) + (h.get("paths") or [])
    if len(combined) > _MAX_PATHS_PER_HIT:
        m["paths_omitted"] = int(m.get("paths_omitted") or 0) + len(combined) - _MAX_PATHS_PER_HIT
    m["paths"] = combined[:_MAX_PATHS_PER_HIT]
    m["graph_origin"] = _union_origin(m.get("graph_origin"), h.get("graph_origin"))
    if _JUDGE_RANK.get(h.get("judgement"), 9) < _JUDGE_RANK.get(m.get("judgement"), 9):
        m["judgement"] = h["judgement"]


def _graph_hits(result: dict, k: int) -> list:
    """`run_impact` 結果（items[] は sure→review 順・presumed[] はその後ろ）を共通ヒット形へ変換し、同一 key を統合して `[:k]` に切る。"""
    merged: dict = {}
    order: list = []
    for item in result.get("items") or []:
        _merge_graph_item(merged, order, _graph_item_hit(item))
    for item in result.get("presumed") or []:
        _merge_graph_item(merged, order, _presumed_item_hit(item))
    out = GraphHits(merged[key] for key in order)
    out.total = len(out)
    out.structural_count = sum(1 for h in out if "structure" in h["graph_origin"])
    out.presumed_count = sum(1 for h in out if "presumed" in h["graph_origin"])
    out.run_coverage = result.get("coverage")
    out.all_hits = list(out)
    del out[k:]
    return out


# ==== RRF 融合 ====

def fuse_rrf(per_engine: dict, weights: dict, k: int) -> list:
    """RRF（Reciprocal Rank Fusion）。決定的・スコア尺度非依存。
    score(hit) = Σ_e weights[e] * jf(hit) / (RRF_K + rank_e)
      - rank_e: エンジン e 内の 1-based 順位（dedupe 後）
      - jf: graph のみ `_JUDGEMENT_FACTOR[judgement]`（keyword／vector は 1.0）
    key で統合し、sources={engine: rank} を記録。snippet／line は keyword > vector > graph の順で採用。paths は graph 由来のみ。
    並びは (-score, key) で決定的。返値は上位 k 件。
    """
    merged: dict = {}
    for e in ENGINES:  # 固定順で走査＝snippet 優先順を担保
        for rank, h in enumerate(per_engine.get(e, []), start=1):
            jf = _JUDGEMENT_FACTOR.get(h.get("judgement"), 1.0) if e == "graph" else 1.0
            contrib = weights.get(e, 1.0) * jf / (RRF_K + rank)
            m = merged.setdefault(h["key"], {"doc_id": h["doc_id"], "path": h["path"],
                                             "line": None, "snippet": "", "score": 0.0,
                                             "sources": {}, "paths": None, "judgement": None})
            m["score"] += contrib
            m["sources"][e] = rank
            if not m["snippet"]:
                m["snippet"], m["line"] = h["snippet"], h.get("line")
            if e == "graph":
                m["paths"] = (m["paths"] or []) + (h.get("paths") or [])
                if h.get("paths_omitted"):
                    m["paths_omitted"] = h["paths_omitted"]
                m["judgement"] = m["judgement"] or h.get("judgement")
                if h.get("graph_origin"):
                    m["graph_origin"] = _union_origin(m.get("graph_origin"), h["graph_origin"])
    out = sorted(merged.items(), key=lambda kv: (-kv[1]["score"], kv[0]))
    return [v for _, v in out[:k]]
