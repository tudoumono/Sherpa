"""影響たどり（`world_impact`）が段ごとに広げる BFS であることの単体テスト。

実 Neo4j は使わず、メモリ上のグラフを問い合わせの中身（起点の確認・1 段の展開・深さの先の確認・世代プローブ）で
出し分ける session スタブで検証する。実 Neo4j との一致は `tests/integration/test_impact_bfs_real.py`。
"""
from __future__ import annotations

import pytest
from test_world_neo4j_overload import _FakeResult, _timeout_error

from sherpa.ingest import world_neo4j as wn


class _GraphSession:
    """`edges`＝(src, dst, type, doc, line)。src が dst に依存する（影響は dst → src の向きに広がる）。"""

    def __init__(self, nodes: dict, edges: list, fail_on_expansion: dict | None = None):
        self.nodes, self.edges = nodes, edges
        self.fail_on_expansion = fail_on_expansion or {}
        self.frontiers: list[list[str]] = []
        self.probes = 0
        self.queries: list[str] = []

    def _ok(self, cid, p):
        n = self.nodes[cid]
        if p["prefixes"] and not any(n["path"] == x or n["path"].startswith(x + "/") for x in p["prefixes"]):
            return False
        return p["incl"] or n.get("status", "active") == "active"

    def run(self, query, **p):
        text = query.text if hasattr(query, "text") else query
        self.queries.append(text)
        if "SherpaMeta" in text:
            return _FakeResult([{"c": 1, "era": wn.GRAPH_SCHEMA_ERA}])
        if "s.canonical_id IN $starts" in text:
            return _FakeResult([{"cid": c, "name": self.nodes[c]["name"], "path": self.nodes[c]["path"]}
                                for c in p["starts"] if c in self.nodes and self._ok(c, p)])
        rows = []
        out = "[e:FLOWS_TO]" in text           # 流れの段: frontier から出る FLOWS_TO。構造の段: frontier へ入る構造の辺
        for src, dst, typ, doc, line in self.edges:
            if out != (typ == "FLOWS_TO"):
                continue
            near, far = (src, dst) if out else (dst, src)
            if near in p["frontier"] and self._ok(far, p):
                m = self.nodes[far]
                rows.append({"cid": far, "name": m["name"], "label": m.get("label", "Module"), "status": "active",
                             "dpath": m["path"], "top": "t", "analyzer": None, "to_cid": near,
                             "edge": {"type": typ, "doc": doc, "line": line}})
        if "$seen" in text:
            self.probes += 1
            return _FakeResult([{"x": 1}] if any(r["cid"] not in p["seen"] for r in rows) else [])
        self.frontiers.append(list(p["frontier"]))
        hook = self.fail_on_expansion.get(len(self.frontiers))
        if isinstance(hook, BaseException):
            raise hook
        return _FakeResult(rows)


def _nodes(*cids, folder="t"):
    return {c: {"name": c.upper(), "path": f"{folder}/{c}.cbl"} for c in cids}


def _chain(n):
    """c0 ← c1 ← … ← c(n-1)（c(k+1) が c(k) に依存する）。"""
    cids = [f"c{i}" for i in range(n)]
    return _nodes(*cids), [(cids[i + 1], cids[i], "INVOKES", f"t/d{i}.md", i + 1) for i in range(n - 1)]


def test_one_query_per_level_and_trace_is_the_chain():
    nodes, edges = _chain(5)
    s = _GraphSession(nodes, edges)
    info: dict = {}
    items = wn.world_impact(s, ["c0"], "w", depth=10, info=info, detail=True)
    assert s.frontiers == [["c0"], ["c1"], ["c2"], ["c3"], ["c4"]]      # 段数分（最後の 1 本は空の確認）
    by = {i["canonical_id"]: i for i in items}
    assert set(by) == {"c1", "c2", "c3", "c4"}
    assert by["c4"]["trace_cids"] == ["c4", "c3", "c2", "c1", "c0"]
    assert by["c4"]["trace"] == ["C4", "C3", "C2", "C1", "C0"]
    assert [e["line"] for e in by["c4"]["edges"]] == [4, 3, 2, 1]
    assert info["depth_truncated"] is False and s.probes == 0


def test_fan_in_graph_visits_each_node_once_and_traces_are_shortest():
    width, layers = 4, 6
    ids = {(k, j): f"l{k}n{j}" for k in range(layers) for j in range(width)}
    nodes = _nodes(*ids.values())
    edges = [(ids[(k + 1, a)], ids[(k, b)], "COPIES", "t/d.md", 1)
             for k in range(layers - 1) for a in range(width) for b in range(width)]
    s = _GraphSession(nodes, edges)
    items = wn.world_impact(s, [ids[(0, 0)]], "w", depth=10, info={}, detail=True)
    seen_in_frontiers = [c for f in s.frontiers for c in f]
    assert len(seen_in_frontiers) == len(set(seen_in_frontiers))         # どのノードも frontier に 1 回だけ
    assert len(items) == (layers - 1) * width                            # 層 0 は起点以外届かない
    by = {i["canonical_id"]: i for i in items}
    for (k, j), cid in ids.items():
        if k == 0:
            continue
        assert len(by[cid]["trace_cids"]) == k + 1                       # 層 k のノードは k 段で届く最短


def test_shortest_route_wins_over_a_longer_one():
    nodes = _nodes("s", "a", "b", "x")
    edges = [("a", "s", "INVOKES", "t/d.md", 1), ("b", "a", "INVOKES", "t/d.md", 2),
             ("x", "b", "COPIES", "t/d.md", 3), ("x", "s", "ACCESSES", "t/d.md", 9)]
    items = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", depth=5, detail=True)
    x = next(i for i in items if i["canonical_id"] == "x")
    assert x["trace_cids"] == ["x", "s"] and x["edges"][0]["type"] == "ACCESSES"


def test_out_of_scope_node_is_neither_reached_nor_a_waypoint():
    nodes = {"s": {"name": "S", "path": "in/s.cbl"}, "mid": {"name": "MID", "path": "out/mid.cbl"},
             "far": {"name": "FAR", "path": "in/far.cbl"}, "ok": {"name": "OK", "path": "in/ok.cbl"}}
    edges = [("mid", "s", "INVOKES", "in/d.md", 1), ("far", "mid", "INVOKES", "in/d.md", 2),
             ("ok", "s", "INVOKES", "in/d.md", 3)]
    s = _GraphSession(nodes, edges)
    items = wn.world_impact(s, ["s"], "w", scope_prefixes=["in"], depth=5, detail=True)
    assert [i["canonical_id"] for i in items] == ["ok"]


def test_inactive_nodes_are_skipped_unless_deprecated_is_included():
    nodes = {"s": {"name": "S", "path": "t/s"}, "old": {"name": "OLD", "path": "t/o", "status": "deprecated"},
             "new": {"name": "NEW", "path": "t/n"}}
    edges = [("old", "s", "INVOKES", "t/d.md", 1), ("new", "old", "INVOKES", "t/d.md", 2)]
    assert wn.world_impact(_GraphSession(nodes, edges), ["s"], "w") == []
    got = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", include_deprecated=True)
    assert {i["name"] for i in got} == {"OLD", "NEW"}


@pytest.mark.parametrize("depth,expected", [(1, True), (3, True), (4, False), (10, False)])
def test_depth_truncated_asks_one_step_beyond_the_requested_depth(depth, expected):
    nodes, edges = _chain(5)
    s = _GraphSession(nodes, edges)
    info: dict = {}
    wn.world_impact(s, ["c0"], "w", depth=depth, info=info)
    assert info["depth_truncated"] is expected
    assert s.probes == (1 if depth <= 4 else 0)                         # 問うのは深さを使い切って frontier が残るときの 1 回
    assert len(s.frontiers) == min(depth, 5)


def test_cycle_back_to_the_start_is_beyond_depth_one_but_not_beyond_depth_two():
    nodes = _nodes("s", "a")
    edges = [("a", "s", "INVOKES", "t/d.md", 1), ("s", "a", "INVOKES", "t/d.md", 2)]
    info: dict = {}
    items = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", depth=1, info=info)
    assert [i["name"] for i in items] == ["A"] and info["depth_truncated"] is True
    info = {}
    items = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", depth=2, info=info)
    assert {i["name"] for i in items} == {"A", "S"} and info["depth_truncated"] is False


def test_cycle_back_to_the_start_counts_the_start_with_its_cycle_route():
    nodes = _nodes("s", "a")
    edges = [("a", "s", "INVOKES", "t/d.md", 1), ("s", "a", "INVOKES", "t/d.md", 2)]
    items = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", depth=3, detail=True)
    by = {i["canonical_id"]: i for i in items}
    assert by["s"]["trace_cids"] == ["s", "a", "s"] and by["a"]["trace_cids"] == ["a", "s"]


def test_starts_reaching_each_other_are_affected():
    nodes = _nodes("s1", "s2")
    edges = [("s1", "s2", "INVOKES", "t/d.md", 1)]
    items = wn.world_impact(_GraphSession(nodes, edges), ["s1", "s2"], "w", detail=True)
    assert [(i["canonical_id"], i["trace_cids"]) for i in items] == [("s1", ["s1", "s2"])]


def _overload_case(failure):
    nodes, edges = _chain(5)
    if failure == "cap":
        # 1 段目は c1 の 1 行だけ・2 段目は c2 と extra の 2 行で天井（行数の上限 1）
        edges = edges + [("extra", "c1", "INVOKES", "t/d.md", 7)]
        nodes = {**nodes, "extra": {"name": "EXTRA", "path": "t/extra.cbl"}}
        return nodes, edges, None
    return nodes, edges, {2: _timeout_error()}


@pytest.mark.parametrize("failure,reason,kind", [("timeout", "timeout", "timeout"), ("cap", "too_many_rows", "row_cap")])
def test_overload_in_a_level_returns_what_was_reached_and_reports_it(monkeypatch, failure, reason, kind):
    monkeypatch.setattr(wn, "_NEO4J_MAX_ROWS", 1)
    nodes, edges, hook = _overload_case(failure)
    info: dict = {}
    items = wn.world_impact(_GraphSession(nodes, edges, hook), ["c0"], "w", depth=10, info=info)
    assert [i["name"] for i in items] == ["C1"]
    assert info["overload"].reason == reason
    assert info["depth_truncated"] is None and info["depth_check_limit"] == kind
    with pytest.raises(wn.GraphQueryOverloadError) as ei:     # info が無い呼び出しは従来どおり送出する
        wn.world_impact(_GraphSession(nodes, edges, hook), ["c0"], "w", depth=10)
    assert ei.value.reason == reason


def test_row_cap_on_a_chunk_splits_the_frontier_before_giving_up(monkeypatch):
    monkeypatch.setattr(wn, "_NEO4J_MAX_ROWS", 4)
    starts = [f"s{i}" for i in range(4)]
    nodes = _nodes(*starts, *[f"a{i}" for i in range(8)])
    edges = [(f"a{2 * i + j}", f"s{i}", "INVOKES", "t/d.md", 1) for i in range(4) for j in range(2)]
    s = _GraphSession(nodes, edges)
    info: dict = {}
    items = wn.world_impact(s, starts, "w", depth=1, info=info)
    assert len(items) == 8 and "overload" not in info
    assert len(s.frontiers) > 1 and {c for f in s.frontiers for c in f} == set(starts)


def test_single_node_over_the_row_cap_is_reported(monkeypatch):
    monkeypatch.setattr(wn, "_NEO4J_MAX_ROWS", 2)
    nodes = _nodes("s", "a", "b", "c")
    edges = [(x, "s", "INVOKES", "t/d.md", 1) for x in "abc"]
    info: dict = {}
    items = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", info=info)
    assert items == [] and info["overload"].reason == "too_many_rows"


def test_depth_probe_overload_is_unknown_but_keeps_the_items():
    nodes, edges = _chain(5)

    class _ProbeOver(_GraphSession):
        def run(self, query, **p):
            if "$seen" in (query.text if hasattr(query, "text") else query):
                raise _timeout_error()
            return super().run(query, **p)

    info: dict = {}
    items = wn.world_impact(_ProbeOver(nodes, edges), ["c0"], "w", depth=2, info=info)
    assert len(items) == 2 and "overload" not in info
    assert info["depth_truncated"] is None and info["depth_check_limit"] == "timeout"


def test_run_world_impact_still_raises_on_overload(monkeypatch):
    nodes, edges = _chain(5)
    s = _GraphSession(nodes, edges, fail_on_expansion={2: _timeout_error()})
    monkeypatch.setattr(wn, "resolve_world_entity", lambda *a, **k: [{"canonical_id": "c0"}])
    with pytest.raises(wn.GraphQueryOverloadError):
        wn.run_world_impact(s, "C0", "w")


# ---- 第 2 段（データの流れの候補）----

def _flow_graph():
    """s ← a（INVOKES）、a -FLOWS_TO-> d -FLOWS_TO-> r、job -INVOKES-> r。d は Dataset（a が書き r が読む）。"""
    nodes = {**_nodes("s", "a", "r", "job"), "d": {"name": "D", "path": "t/job.jcl", "label": "Dataset"}}
    edges = [("a", "s", "INVOKES", "t/j.jcl", 1), ("a", "d", "FLOWS_TO", "t/a.cbl", 5),
             ("d", "r", "FLOWS_TO", "t/r.cbl", 7), ("job", "r", "INVOKES", "t/job.jcl", 2)]
    return nodes, edges


def test_flow_candidates_are_reached_after_structure_and_kept_separate():
    nodes, edges = _flow_graph()
    s = _GraphSession(nodes, edges)
    info: dict = {}
    items = wn.world_impact(s, ["s"], "w", depth=5, info=info, detail=True, flow_depth=3)
    by = {i["canonical_id"]: i for i in items}
    assert {c: i["origin"] for c, i in by.items()} == {"a": "structure", "d": "flow", "r": "flow", "job": "flow"}
    assert by["job"]["trace_cids"] == ["job", "r", "d", "a", "s"]
    assert [e["type"] for e in by["job"]["edges"]] == ["INVOKES", "FLOWS_TO", "FLOWS_TO", "INVOKES"]
    assert by["a"]["trace_cids"] == ["a", "s"]
    assert info["flow_depth_truncated"] is False and info["depth_truncated"] is False


def test_flow_depth_zero_returns_exactly_what_the_structure_stage_returns():
    nodes, edges = _flow_graph()
    base = _GraphSession(nodes, edges)
    base_info: dict = {}
    base_items = wn.world_impact(base, ["s"], "w", depth=5, info=base_info, detail=True)
    s = _GraphSession(nodes, edges)
    info: dict = {}
    items = wn.world_impact(s, ["s"], "w", depth=5, info=info, detail=True, flow_depth=0)
    assert items == base_items and info == base_info
    assert [i["canonical_id"] for i in items] == ["a"] and all("origin" not in i for i in items)
    assert not any("FLOWS_TO" in q for q in s.queries) and s.frontiers == base.frontiers


def test_a_node_reached_by_structure_is_not_a_flow_candidate():
    nodes, edges = _flow_graph()
    edges = edges + [("r", "s", "COPIES", "t/r.cbl", 3)]       # r は構造でも届く
    items = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", depth=5, detail=True, flow_depth=3)
    by = {i["canonical_id"]: i for i in items}
    assert by["r"]["origin"] == "structure" and by["r"]["trace_cids"] == ["r", "s"]
    assert {c for c, i in by.items() if i["origin"] == "flow"} == {"d"}      # job は r を経由して構造でも届く


@pytest.mark.parametrize("flow_depth,reached,truncated", [(1, {"d"}, True), (2, {"d", "r"}, True), (3, {"d", "r", "job"}, False)])
def test_flow_depth_limits_the_second_stage_and_reports_what_is_left(flow_depth, reached, truncated):
    nodes, edges = _flow_graph()
    info: dict = {}
    items = wn.world_impact(_GraphSession(nodes, edges), ["s"], "w", depth=5, info=info, detail=True, flow_depth=flow_depth)
    assert {i["canonical_id"] for i in items if i["origin"] == "flow"} == reached
    assert info["flow_depth_truncated"] is truncated


def test_structure_overload_skips_the_flow_stage_and_marks_it_unknown(monkeypatch):
    monkeypatch.setattr(wn, "_NEO4J_MAX_ROWS", 1)
    nodes, edges = _chain(5)
    nodes = {**nodes, "d": {"name": "D", "path": "t/d", "label": "Dataset"}}
    edges = edges + [("c1", "d", "FLOWS_TO", "t/x.cbl", 9)]
    s = _GraphSession(nodes, edges, fail_on_expansion={2: _timeout_error()})
    info: dict = {}
    items = wn.world_impact(s, ["c0"], "w", depth=10, info=info, flow_depth=3)
    assert [i["name"] for i in items] == ["C1"] and info["overload"].reason == "timeout"
    assert info["flow_depth_truncated"] is None and info["flow_depth_check_limit"] == "timeout"
    assert not any("FLOWS_TO" in q for q in s.queries)


def test_flow_stage_overload_keeps_the_structure_result_and_is_reported(monkeypatch):
    nodes, edges = _flow_graph()
    s = _GraphSession(nodes, edges, fail_on_expansion={3: _timeout_error()})   # 3 回目の展開（流れの 1 段目）で時間切れ
    info: dict = {}
    items = wn.world_impact(s, ["s"], "w", depth=5, info=info, detail=True, flow_depth=3)
    assert [i["canonical_id"] for i in items] == ["a"] and "overload" not in info
    assert info["flow_overload"].reason == "timeout"
    assert info["flow_depth_truncated"] is None and info["flow_depth_check_limit"] == "timeout"
    with pytest.raises(wn.GraphQueryOverloadError):      # info が無い呼び出しは握り潰さず送出する
        wn.world_impact(_GraphSession(nodes, edges, fail_on_expansion={3: _timeout_error()}), ["s"], "w", depth=5, flow_depth=3)


def test_run_world_impact_returns_candidates_apart_from_items(monkeypatch):
    nodes, edges = _flow_graph()
    monkeypatch.setattr(wn, "resolve_world_entity", lambda *a, **k: [{"canonical_id": "s"}])
    monkeypatch.setattr(wn, "read_unresolved", lambda *a, **k: {"available": True, "items": [], "omitted": 0})
    monkeypatch.setattr(wn, "read_plugin_failures", lambda *a, **k: [])
    monkeypatch.setattr(wn, "read_unparsed_sources", lambda *a, **k: None)
    res = wn.run_world_impact(_GraphSession(nodes, edges), "S", "w", flow_depth=2)
    assert [i["name"] for i in res["items"]] == ["A"]
    assert sorted(i["name"] for i in res["candidates"]) == ["D", "R"]
    assert all("origin" not in i for i in res["items"] + res["candidates"])
    assert res["coverage"]["flow"] == {"requested": 2, "truncated": True}
    assert {"kind": "depth", "stage": "flow"} in res["coverage"]["limits"]
    plain = wn.run_world_impact(_GraphSession(nodes, edges), "S", "w")
    assert "candidates" not in plain and "flow" not in plain["coverage"]
