"""未完了・打ち切りの申告（`coverage`）の単体テスト（Neo4j 不要・fake session とスタブ）。

「関連なし」と「調べきれなかった」を区別できること（時間切れ・行数の天井・文書探索の打ち切り・カード数の上限・
深さの上限・`k` の切り捨て）を、原因調査のレンズ・`graph_neighbors`・影響レンズ・融合検索で固定する。
深さの先の判定そのものは `tests/integration/test_impact_depth_coverage.py`（実 Neo4j）。
"""
from __future__ import annotations

import neo4j as neo4j_mod
import pytest
from neo4j import Query
from neo4j.exceptions import Neo4jError, ServiceUnavailable

from sherpa import documents, graph_coverage, impact_service, lens_service as ls, mcp_server as M
from sherpa import tool_dispatch as TD
from sherpa.ingest import world_neo4j
from sherpa.parts.read import fused_search as ss


# ---- 段階ごとに時間切れ・行数の天井を起こす fake session --------------------------------------------

class _Result:
    def __init__(self, rows):
        self._rows = rows

    def __iter__(self):
        return iter(_Rec(r) for r in self._rows)

    def consume(self):
        pass

    def data(self):
        return [dict(r) for r in self._rows]


class _Rec:
    def __init__(self, d):
        self._d = d

    def data(self):
        return dict(self._d)


def _timeout():
    return Neo4jError._hydrate_neo4j(
        code="Neo.ClientError.Transaction.TransactionTimedOutClientConfiguration", message="timed out")


def _related(i):
    return {"cid": f"module:w1:a#N{i}", "name": f"N{i}", "label": "Module", "status": "active",
            "path_names": ["ROOT", f"N{i}"], "edges": [{"type": "INVOKES", "doc": "a.md"}], "dist": 1}


class _StageSession:
    """起点の解決（anchor）・近傍の取得（neighbors）を `"timeout"` か行のリストで差し替える。`Query` でない実行（世代プローブ）は未投入を返す。"""

    def __init__(self, anchor, neighbors):
        self.behave = {"anchor": anchor, "neighbors": neighbors}

    def run(self, query, **params):
        if not isinstance(query, Query):
            return _Result([{"c": 0, "era": None}])
        if "unresolved_names" in str(query):                      # 未解決の申告の読み取り（この単体テストの対象外）
            return _Result([])
        stage = "anchor" if "RETURN DISTINCT n.canonical_id" in str(query) else "neighbors"
        b = self.behave[stage]
        if b == "timeout":
            raise _timeout()
        return _Result(b)


_ANCHOR_OK = [{"cid": "module:w1:a#TAXCALC", "name": "TAXCALC"}]


@pytest.fixture(autouse=True)
def _no_grep(monkeypatch):
    monkeypatch.setattr(ls, "grep_search", lambda *a, **k: [])


def _patch_driver(monkeypatch, session):
    class _Sess:
        def __enter__(self):
            return session

        def __exit__(self, *a):
            return False

    class _Driver:
        def session(self):
            return _Sess()

        def close(self):
            pass

    monkeypatch.setattr(neo4j_mod.GraphDatabase, "driver", lambda uri, auth: _Driver())
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})


# ---- run_troubleshoot（API・画面）---------------------------------------------------------------

@pytest.mark.parametrize("session, stage", [
    (_StageSession("timeout", []), "anchor"),
    (_StageSession(_ANCHOR_OK, "timeout"), "neighbors"),
])
def test_troubleshoot_timeout_is_not_a_plain_empty_result(session, stage):
    res = ls.run_troubleshoot(session, "TAXCALC の ABEND", "w1")
    assert res["candidates"] == []
    assert res["coverage"] == {"complete": False, "limits": [{"kind": "timeout", "stage": stage}], "omitted": None}
    assert len(res["notes"]) == 1 and "調べきれていません" in res["notes"][0]
    assert not any(w in res["notes"][0] for w in ("timeout", "row_cap", "cap", "Neo4j"))  # 内部語彙を出さない


def test_troubleshoot_row_cap_partial_is_marked_partial(monkeypatch):
    monkeypatch.setattr(ls, "_NEO4J_MAX_ROWS", 2)
    res = ls.run_troubleshoot(_StageSession(_ANCHOR_OK, [_related(i) for i in range(3)]), "TAXCALC", "w1")
    assert len(res["candidates"]) == 2  # 上限までの部分結果
    assert res["coverage"]["limits"] == [{"kind": "row_cap", "stage": "neighbors"}]
    assert res["coverage"]["complete"] is False
    assert "一部しか調べられていません" in res["notes"][0]


def test_troubleshoot_doc_search_truncation_is_reported_in_coverage_and_notes(monkeypatch):
    def grep(term, world, scope_paths=None, truncated_docs=None, **kw):
        truncated_docs.append("大きい資料.md")
        return []

    monkeypatch.setattr(ls, "grep_search", grep)
    res = ls.run_troubleshoot(_StageSession(_ANCHOR_OK, [_related(1)]), "TAXCALC", "w1")
    assert res["coverage"]["limits"] == [{"kind": "doc_search_truncated", "stage": "docs"}]
    assert res["notes"] == [ls._truncated_search_note(["大きい資料.md"])]


# ---- graph_neighbors（Codex の道具）---------------------------------------------------------------

def _neighbors(monkeypatch, session):
    _patch_driver(monkeypatch, session)
    res, *_ = TD.run_tool("graph_neighbors", {"name": "TAXCALC"}, "w1", None)
    return res


@pytest.mark.parametrize("session, stage", [
    (_StageSession("timeout", []), "anchor"),
    (_StageSession(_ANCHOR_OK, "timeout"), "neighbors"),
])
def test_graph_neighbors_timeout_is_not_reported_as_no_neighbors(monkeypatch, session, stage):
    res = _neighbors(monkeypatch, session)
    assert res["neighbors"] == []
    assert res["coverage"] == {"complete": False, "limits": [{"kind": "timeout", "stage": stage}], "omitted": None}
    assert res["truncated"] is True
    assert M._coverage_outcome("graph_neighbors", res, False) == "limit"  # 台帳に「確認済みの 0 件」と記録しない


@pytest.mark.parametrize("session, stage", [
    (_StageSession("timeout", []), "anchor"),
    (_StageSession(_ANCHOR_OK, "timeout"), "neighbors"),
])
def test_graph_neighbors_partial_fetch_has_no_unresolved_field(monkeypatch, session, stage):
    """起点の解決・近傍の取得が打ち切られたときは `unresolved` を付けない（未解決の問い合わせも流さない）。"""
    asked: list = []
    orig = session.run
    session.run = lambda query, **kw: asked.append(str(query)) or orig(query, **kw)
    res = _neighbors(monkeypatch, session)
    assert res == {"neighbors": [], "truncated": True,
                   "coverage": {"complete": False, "limits": [{"kind": "timeout", "stage": stage}], "omitted": None}}
    assert not any("unresolved_names" in q for q in asked)


def test_graph_neighbors_row_cap_is_partial(monkeypatch):
    monkeypatch.setattr(ls, "_NEO4J_MAX_ROWS", 2)
    res = _neighbors(monkeypatch, _StageSession(_ANCHOR_OK, [_related(i) for i in range(3)]))
    assert res["truncated"] is True
    assert res["coverage"]["limits"] == [{"kind": "row_cap", "stage": "neighbors"}]


def test_graph_neighbors_doc_search_truncation_is_not_a_complete_zero(monkeypatch):
    monkeypatch.setattr(ls, "grep_search",
                        lambda t, w, scope_paths=None, truncated_docs=None, **kw: truncated_docs.append("x.md") or [])
    res = _neighbors(monkeypatch, _StageSession(_ANCHOR_OK, [_related(1)]))
    assert res["coverage"]["limits"] == [{"kind": "doc_search_truncated", "stage": "docs"}]
    assert "truncated" not in res  # 近傍そのものは切れていない（カードの根拠 grep だけが先頭部分）
    assert M._coverage_outcome("graph_neighbors", res, False) == "limit"


def test_graph_neighbors_card_cap_reports_omitted_count(monkeypatch):
    from sherpa.parts.read import tools as RT
    cards = [{"name": f"N{i}", "label": "Module", "category": "ソース", "role": "実装", "distance": 1,
              "path": [], "source": "graph", "evidence": {"edges": [], "grep": []}}
             for i in range(RT._GRAPH_CARDS_MAX + 1)]
    monkeypatch.setattr(ls, "neighbor_cards", lambda world, term, sp=None: list(cards))
    res, *_ = TD.run_tool("graph_neighbors", {"name": "TAXCALC"}, "w1", None)
    assert res["count"] == RT._GRAPH_CARDS_MAX + 1 and res["truncated"] is True
    assert res["coverage"] == {"complete": False, "limits": [{"kind": "card_cap", "stage": "cards"}], "omitted": 1}


def test_graph_neighbors_unavailable_and_reingest_are_named_by_the_same_kinds(monkeypatch):
    class _Down:
        def session(self):
            raise ServiceUnavailable("down")

        def close(self):
            pass

    monkeypatch.setattr(neo4j_mod.GraphDatabase, "driver", lambda uri, auth: _Down())
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    res, *_ = TD.run_tool("graph_neighbors", {"name": "TAXCALC"}, "w1", None)
    assert res["error_code"] == graph_coverage.KIND_GRAPH_UNAVAILABLE
    assert res["coverage"]["limits"] == [{"kind": "graph_unavailable"}] and res["coverage"]["complete"] is False
    # 旧世代は失敗応答のまま、理由は同じ kind の名前（`coverage` には載せない）
    from sherpa.parts.read import tools as RT
    assert RT.GRAPH_REINGEST_ERROR_CODE == graph_coverage.KIND_GRAPH_REINGEST_REQUIRED


def test_complete_empty_neighbors_is_still_a_confirmed_zero(monkeypatch):
    res = _neighbors(monkeypatch, _StageSession([], []))
    assert res["coverage"] == {"complete": True, "limits": [], "omitted": 0}
    assert M._coverage_outcome("graph_neighbors", res, False) == "no_hits"


# ---- 影響レンズ（run_impact）-----------------------------------------------------------------------

def _impact_result(truncated, items=()):
    cov = graph_coverage.Coverage()
    if truncated:
        cov.add(graph_coverage.KIND_DEPTH, graph_coverage.STAGE_IMPACT)
    return {"type": "impact", "world_id": "w", "scope_prefixes": [], "start": "A", "include_deprecated": False,
            "starts": [], "items": list(items),
            "coverage": cov.as_dict(depth={"requested": 4, "truncated": truncated})}


def test_run_impact_notes_depth_stop_and_presumed_toggle(monkeypatch):
    monkeypatch.setattr(world_neo4j, "run_world_impact", lambda *a, **k: _impact_result(True, [{"name": "B"}]))
    res = impact_service.run_impact(object(), "A", "w", depth=4)
    assert res["coverage"]["depth"] == {"requested": 4, "truncated": True}
    assert "4段" in res["notes"][0] and "presumed" not in res

    monkeypatch.setattr(world_neo4j, "run_world_impact", lambda *a, **k: _impact_result(False))
    called = []
    monkeypatch.setattr(impact_service, "presumed_impact", lambda *a, **k: called.append(1) or [])
    assert "notes" not in impact_service.run_impact(object(), "A", "w", include_presumed=False)
    assert called == []  # `include_presumed=False` は推定の grep をしない
    assert impact_service.run_impact(object(), "A", "w")["presumed"] == [] and called == [1]


# ---- 融合検索（外部 API の coverage・graph_origin・degraded.detail）------------------------------

def _graph_result(n_structure=0, n_presumed=0, truncated=False):
    items = [{"name": f"S{i}", "label": "Module", "category": "ソース", "path": f"s{i}.cbl",
              "trace": [], "evidence": []} for i in range(n_structure)]
    presumed = [{"name": f"P{i}", "label": "Module", "category": "ソース", "path": f"p{i}.md",
                 "evidence": [{"doc": f"p{i}.md", "line": 1, "quote": "q"}]} for i in range(n_presumed)]
    cov = graph_coverage.Coverage()
    if truncated:
        cov.add(graph_coverage.KIND_DEPTH, graph_coverage.STAGE_IMPACT)
    return {"items": items, "presumed": presumed,
            "coverage": cov.as_dict(depth={"requested": 10, "truncated": truncated})}


def _kw(doc):
    return {"key": doc, "doc_id": doc, "path": doc, "line": 1, "snippet": "s", "engine_score": 1.0,
            "judgement": None, "paths": None}


class _AllDocs:
    def __contains__(self, item):
        return True


def _search(monkeypatch, graph_result, k, keyword_docs=(), **kw):
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kwa: _AllDocs())
    monkeypatch.setattr(ss, "_search_keyword",
                        lambda w, q, sp, kk, s, layer=None: ([_kw(d) for d in keyword_docs], None))
    monkeypatch.setattr(ss, "_search_graph",
                        lambda w, q, sp, kk, d=10, **ex: (ss._graph_hits(graph_result, kk), None))
    return ss.search("w", "q", engines=["keyword", "graph"], k=k, **kw)


def test_coverage_reports_k_cut_depth_and_origin_counts(monkeypatch):
    res = _search(monkeypatch, _graph_result(n_structure=5, truncated=True), k=2, keyword_docs=["k0.md"])
    assert res["coverage"] == {
        "keyword": {"complete": True, "requested_k": 2, "returned": 1, "omitted": None, "limits": []},
        "graph": {"complete": False, "requested_k": 2, "returned": 2, "omitted": 3,
                  "limits": [{"kind": "depth"}, {"kind": "result_cap"}],
                  "depth": {"requested": 10, "truncated": True}, "structural_count": 5, "presumed_count": 0},
        "fused": {"requested_k": 2, "returned": 2, "omitted_by_cut": 1},
    }


def test_graph_origin_structure_presumed_and_keyword_integration(monkeypatch):
    res = _search(monkeypatch, _graph_result(n_structure=1), k=10, keyword_docs=["s0.cbl", "k.md"])
    by = {h["doc_id"]: h for h in res["hits"]}
    assert set(by["s0.cbl"]["sources"]) == {"keyword", "graph"} and by["s0.cbl"]["graph_origin"] == ["structure"]
    assert "graph_origin" not in by["k.md"]  # keyword だけのヒットには付かない

    res = _search(monkeypatch, _graph_result(n_presumed=1), k=10, keyword_docs=["p0.md"])
    h = res["hits"][0]
    assert set(h["sources"]) == {"keyword", "graph"} and h["graph_origin"] == ["presumed"]
    assert res["coverage"]["graph"]["presumed_count"] == 1 and res["coverage"]["graph"]["structural_count"] == 0
    assert not any(set(h.get("graph_origin", [])) == {"structure", "presumed"} for h in res["hits"])


@pytest.mark.parametrize("exc, detail", [
    (world_neo4j.GraphQueryOverloadError("timeout", world="w"), "timeout"),
    (world_neo4j.GraphQueryOverloadError("too_many_rows", world="w", rows=10000), "row_cap"),
])
def test_overload_is_a_degraded_detail_not_a_coverage(monkeypatch, exc, detail):
    from contextlib import contextmanager

    @contextmanager
    def _session():
        yield object()

    def _boom(*a, **k):
        raise exc

    monkeypatch.setattr(ss, "_neo4j_session", _session)
    monkeypatch.setattr(ss, "run_impact", _boom)
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _AllDocs())
    res = ss.search("w", "q", engines=["graph"], k=3)
    assert res["degraded"] == [{"engine": "graph", "reason": "graph_query_failed", "detail": detail}]
    assert "graph" not in res["coverage"] and res["engines_used"] == []


def test_include_presumed_false_reaches_run_impact_only_when_changed(monkeypatch):
    from contextlib import contextmanager

    @contextmanager
    def _session():
        yield object()

    seen = []
    monkeypatch.setattr(ss, "_neo4j_session", _session)
    monkeypatch.setattr(ss, "run_impact", lambda s, q, w, scope_prefixes=None, depth=10, **kw: seen.append(kw) or
                        {"items": [], "presumed": []})
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: _AllDocs())
    ss.search("w", "q", engines=["graph"])
    ss.search("w", "q", engines=["graph"], include_presumed=False)
    assert seen == [{}, {"include_presumed": False}]


# ---- RV 1 巡目の指摘 -------------------------------------------------------------------------------

def test_presumed_failure_is_an_incomplete_coverage_not_a_clean_zero(monkeypatch):
    monkeypatch.setattr(world_neo4j, "run_world_impact", lambda *a, **k: _impact_result(False))

    def _boom(*a, **k):
        raise RuntimeError("bug")

    monkeypatch.setattr(impact_service, "presumed_impact", _boom)
    res = impact_service.run_impact(object(), "A", "w")
    assert res["presumed"] == [] and res["coverage"]["complete"] is False
    assert res["coverage"]["limits"] == [{"kind": "graph_unavailable", "stage": "presumed"}]
    assert any("資料から関連を探す" in n for n in res["notes"])


def test_graph_neighbors_final_clip_keeps_coverage_and_error_code(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "700")
    nb = [{"name": f"N{i}", "path": ["x" * 40], "edges": []} for i in range(30)]
    full = {"neighbors": nb, "coverage": {"complete": True, "limits": [], "omitted": 0}, "error_code": "graph_unavailable"}
    out, clipped = M._clip_tool_result(full, name="graph_neighbors")
    assert clipped and out["error_code"] == "graph_unavailable" and "text" not in out
    kept = len(out["neighbors"])
    assert 0 < kept < 30 and out["truncated"] is True and out["count"] == 30
    assert out["coverage"] == {"complete": False, "limits": [{"kind": "card_cap", "stage": "cards"}],
                               "omitted": 30 - kept}


def test_depth_check_overload_keeps_main_result_and_marks_unknown(monkeypatch):
    items = [{"name": "B", "label": "Module", "category": "ソース", "status": "active", "analyzer": None,
              "top_scope": "in", "path": "in/b", "trace": [], "evidence": []}]
    monkeypatch.setattr(world_neo4j, "resolve_world_entity", lambda *a, **k: [{"canonical_id": "c"}])

    def _wi(session, starts, world, scope, depth, incl, info=None):
        info["depth_truncated"] = None
        info["depth_check_limit"] = "timeout"
        return items

    monkeypatch.setattr(world_neo4j, "world_impact", _wi)
    monkeypatch.setattr(world_neo4j, "read_unresolved", lambda *a, **k: {"available": False, "items": [], "omitted": 0})
    monkeypatch.setattr(world_neo4j.worlds, "world_dir", lambda w: None)
    res = world_neo4j.run_world_impact(object(), "A", "w", depth=3)
    assert res["items"] == items
    assert res["coverage"] == {"complete": False, "limits": [{"kind": "timeout", "stage": "impact"}],
                               "omitted": None, "depth": {"requested": 3, "truncated": None}}


def test_world_impact_converts_depth_check_overload_to_unknown(monkeypatch):
    monkeypatch.setattr(world_neo4j, "_run_read_capped", lambda *a, **k: [{"cid": "x", "name": "B", "label": "Module",
        "status": "active", "dpath": "p", "top": "t", "analyzer": None, "path_names": ["B"], "edges": []}])
    monkeypatch.setattr(world_neo4j, "check_schema_era", lambda *a, **k: None)
    monkeypatch.setattr(world_neo4j, "_attach_importance", lambda *a, **k: None)

    def _over(*a, **k):
        raise world_neo4j.GraphQueryOverloadError("timeout", world="w")

    monkeypatch.setattr(world_neo4j, "_impact_depth_truncated", _over)
    info: dict = {}
    items = world_neo4j.world_impact(object(), ["c"], "w", info=info)
    assert len(items) == 1 and info == {"depth_truncated": None, "depth_check_limit": "timeout"}


# ---- RV 2 巡目の指摘 -------------------------------------------------------------------------------

def test_presumed_cap_is_reported_as_result_cap(monkeypatch):
    monkeypatch.setattr(world_neo4j, "run_world_impact", lambda *a, **k: _impact_result(False))

    def _presumed(session, term, world, scope_prefixes=None, truncated_docs=None):
        return impact_service.PresumedItems([{"name": "P"}], capped=True)

    monkeypatch.setattr(impact_service, "presumed_impact", _presumed)
    res = impact_service.run_impact(object(), "A", "w")
    assert res["coverage"]["complete"] is False and res["coverage"]["omitted"] is None
    assert res["coverage"]["limits"] == [{"kind": "result_cap", "stage": "presumed"}]


def test_presumed_impact_sets_capped_only_when_more_candidates_remain(monkeypatch):
    from sherpa import grep_tool
    nodes = [{"name": f"NODE{i}", "label": "Module", "cid": f"c{i}", "path": f"p{i}", "top": "t"} for i in range(3)]
    monkeypatch.setattr(world_neo4j, "_run_read_capped", lambda *a, **k: nodes)
    monkeypatch.setattr(grep_tool, "grep_search", lambda *a, **k: [{"doc_id": "t/d.md", "line": 1,
                                                                    "text": "NODE0 NODE1 NODE2"}])
    out = impact_service.presumed_impact(object(), "x", "w", max_items=2)
    assert len(out) == 2 and out.capped is True
    out = impact_service.presumed_impact(object(), "x", "w", max_items=3)
    assert len(out) == 3 and out.capped is False


@pytest.mark.parametrize("budget, expect_error", [("300", False), ("45", True)])
def test_graph_neighbors_clip_below_required_fields_keeps_minimal_or_errors(monkeypatch, budget, expect_error):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", budget)
    full = {"neighbors": [{"name": "N", "path": ["x" * 400], "edges": []}],
            "coverage": {"complete": True, "limits": [], "omitted": 0}, "error_code": "graph_unavailable"}
    out, clipped = M._clip_tool_result(full, name="graph_neighbors")
    assert clipped
    if expect_error:
        assert out == {"error": "tool_result_budget_too_small"}
        assert M._coverage_outcome("graph_neighbors", out, True) != "hit"
    else:
        assert out["neighbors"] == [] and out["error_code"] == "graph_unavailable"
        assert out["coverage"]["complete"] is False and out["coverage"]["limits"][0]["kind"] == "card_cap"
        assert M._coverage_outcome("graph_neighbors", out, False) == "error"


def test_stale_graph_hits_are_not_counted_in_omitted_or_origin_counts(monkeypatch):
    monkeypatch.setattr(documents, "world_rel_set", lambda world=None, **kw: {"s0.cbl", "s2.cbl", "p0.md"})
    monkeypatch.setattr(ss, "_search_graph",
                        lambda w, q, sp, k, d=10, **ex: (ss._graph_hits(_graph_result(n_structure=3, n_presumed=1), k), None))
    res = ss.search("w", "q", engines=["graph"], k=2)
    g = res["coverage"]["graph"]
    # s1.cbl は今の資料に無い: 残りは s0・s2・p0 の 3 件（構造 2・推定 1）→ k=2 で 1 件省略
    assert (g["structural_count"], g["presumed_count"], g["omitted"], g["returned"]) == (2, 1, 1, 2)


# ---- RV 3 巡目の指摘 -------------------------------------------------------------------------------

def test_final_clip_minimal_form_keeps_upstream_limits_and_unknown_totals(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "330")
    full = {"neighbors": [{"name": "N", "path": ["x" * 400], "edges": []}], "truncated": True, "count": None,
            "coverage": {"complete": False, "limits": [{"kind": "row_cap", "stage": "neighbors"}], "omitted": None}}
    out, _ = M._clip_tool_result(full, name="graph_neighbors")
    assert out["neighbors"] == [] and out["count"] is None
    assert out["coverage"] == {"complete": False, "omitted": None, "limits": [
        {"kind": "row_cap", "stage": "neighbors"}, {"kind": "card_cap", "stage": "cards"}]}


def test_card_cap_after_partial_fetch_leaves_total_unknown(monkeypatch):
    from sherpa.parts.read import tools as RT
    cov = graph_coverage.Coverage()
    cov.add(graph_coverage.KIND_ROW_CAP, graph_coverage.STAGE_NEIGHBORS)
    cards = [{"name": f"N{i}", "label": "Module", "category": "ソース", "role": "実装", "distance": 1,
              "path": [], "source": "graph", "evidence": {"edges": [], "grep": []}}
             for i in range(RT._GRAPH_CARDS_MAX + 1)]
    monkeypatch.setattr(ls, "neighbor_cards", lambda world, term, sp=None: ls._Cards(cards, cov))
    res, *_ = TD.run_tool("graph_neighbors", {"name": "TAXCALC"}, "w1", None)
    assert res["count"] is None and res["truncated"] is True
    assert res["coverage"]["omitted"] is None
    assert [x["kind"] for x in res["coverage"]["limits"]] == ["row_cap", "card_cap"]
