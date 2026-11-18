"""`graph_neighbors` の絞り込み（`path_prefix`・`kind`・起点が多すぎるときの『広すぎる』応答）の単体テスト（Neo4j 不要・スタブ）。

実 Neo4j での再現（同名のノードを上限より多く置く）は `tests/integration/test_graph_narrow_real.py`。
"""
from __future__ import annotations

import pytest

from sherpa import graph_coverage, lens_service as ls, mcp_server as M
from sherpa import tool_dispatch as TD


def test_narrow_required_is_a_known_coverage_kind():
    cov = graph_coverage.Coverage()
    cov.add(graph_coverage.KIND_NARROW_REQUIRED, graph_coverage.STAGE_ANCHOR)
    assert cov.as_dict()["limits"] == [{"kind": "narrow_required", "stage": "anchor"}]


def test_anchor_max_env_is_clamped(monkeypatch):
    monkeypatch.delenv("SHERPA_GRAPH_ANCHOR_MAX", raising=False)
    assert ls.graph_anchor_max() == 50
    monkeypatch.setenv("SHERPA_GRAPH_ANCHOR_MAX", "120")
    assert ls.graph_anchor_max() == 120
    monkeypatch.setenv("SHERPA_GRAPH_ANCHOR_MAX", "0")
    assert ls.graph_anchor_max() == 50


def test_anchor_queries_judge_scope_by_membership_paths():
    class _S:
        def __init__(self):
            self.queries = []

        def run(self, query, **params):
            self.queries.append(str(query))
            return iter(())

    s = _S()
    ls.resolve_anchor(s, "Foo", "w1", ["y"])
    ls._resolve_anchor_by_name(s, "Foo", "w1", ["y"])
    ls._anchor_breadth(s, "w1", "n.name = $term", ["y"], None, 50, ls.Coverage(), term="Foo")
    assert len(s.queries) >= 3 and all("n.membership_paths" in q for q in s.queries)


def _record_calls(monkeypatch, cards=()):
    calls = []

    def fake(world, term, sp=None, kind=None):
        calls.append({"term": term, "sp": sp, "kind": kind})
        return ls._Cards(list(cards), ls.Coverage())

    monkeypatch.setattr(ls, "neighbor_cards", fake)
    return calls


def test_graph_neighbors_passes_kind_and_narrowed_scope(monkeypatch):
    calls = _record_calls(monkeypatch)
    TD.run_tool("graph_neighbors", {"name": "X", "kind": "Module", "path_prefix": "a/b"}, "w1", ["a"])
    TD.run_tool("graph_neighbors", {"name": "X"}, "w1", ["a"])
    assert calls == [{"term": "X", "sp": ["a/b"], "kind": "Module"}, {"term": "X", "sp": ["a"], "kind": None}]


def test_graph_neighbors_path_prefix_outside_scope_is_rejected_without_querying(monkeypatch):
    calls = _record_calls(monkeypatch)
    res, *_ = TD.run_tool("graph_neighbors", {"name": "X", "path_prefix": "b"}, "w1", ["a"])
    assert res["error"] == "graph_invalid_args" and "範囲の外" in res["message"] and calls == []
    res, *_ = TD.run_tool("graph_neighbors", {"name": "X", "path_prefix": "../etc"}, "w1", None)
    assert res["error"] == "graph_invalid_args" and calls == []


def test_graph_neighbors_unknown_kind_is_rejected(monkeypatch):
    calls = _record_calls(monkeypatch)
    res, *_ = TD.run_tool("graph_neighbors", {"name": "X", "kind": "Nope"}, "w1", None)
    assert res["error"] == "graph_invalid_args" and "kind" in res["message"] and calls == []


def test_too_broad_cards_become_a_narrowing_request(monkeypatch):
    cov = ls.Coverage()
    cov.add(graph_coverage.KIND_NARROW_REQUIRED, graph_coverage.STAGE_ANCHOR)
    cards = ls._Cards([], cov)
    cards.too_broad = {"anchors": 80, "limit": 50, "by_folder": {"a": 55, "b": 25}, "by_kind": {"Module": 60, "Copybook": 20}}
    monkeypatch.setattr(ls, "neighbor_cards", lambda world, term, sp=None: cards)
    res, *_ = TD.run_tool("graph_neighbors", {"name": "X"}, "w1", None)
    assert res["neighbors"] == [] and res["too_broad"]["anchors"] == 80
    assert res["too_broad"]["by_folder"] == {"a": 55, "b": 25} and "path_prefix" in res["too_broad"]["message"]
    assert res["coverage"]["complete"] is False
    assert res["coverage"]["limits"] == [{"kind": "narrow_required", "stage": "anchor"}]
    assert M._coverage_outcome("graph_neighbors", res, False) == "limit"   # 空でも「近傍なし」にしない


@pytest.mark.parametrize("name", ["graph_resolve", "graph_impact"])
def test_graph_resolve_and_impact_reject_path_prefix_outside_scope(name):
    args = {"name": "X", "path_prefix": "b"} if name == "graph_resolve" else {"canonical_id": "c", "path_prefix": "b"}
    res, *_ = TD.run_tool(name, args, "w1", ["a"])
    assert res["error"] == "graph_invalid_args" and "範囲の外" in res["message"]


def test_resolve_anchor_matches_names_in_cypher_and_reads_cap_plus_one():
    class _S:
        def __init__(self):
            self.calls = []

        def run(self, query, **params):
            self.calls.append((str(query), params))
            return iter(())

    s = _S()
    ls.resolve_anchor(s, "Foo が落ちる", "w1", cap=50)
    query, params = s.calls[0]
    assert "CONTAINS toLower(n.name)" in query and "coalesce(n.status,'active')" in query and "LIMIT $lim" in query
    assert params["text"] == "Foo が落ちる" and params["lim"] == 51 and params["incl"] is False
    ls.resolve_anchor(s, "Foo", "w1", include_deprecated=True)
    assert "LIMIT" not in s.calls[1][0] and s.calls[1][1]["incl"] is True
