"""期のある資料フォルダ（fixtures/corpus/period-proj）の参照の張り方と、エッジ・ノードの属性。

期の読み方の表の各行（同じ期・親の連鎖で確認済み・名前の順で条件つき・揺れで条件つき・親が決まらずつながない・期の外の共通の部品）を 1 つずつ確かめる。
"""
from __future__ import annotations

import pytest

import _graph_golden as gg
from sherpa import graph_tools
from sherpa.ingest import world_neo4j
from sherpa.ingest.graph.periods_select import merge_confirm

P = "注文管理/改修"


@pytest.fixture(scope="module")
def graph():
    return gg.build_graph("period-proj")


def _edge(graph, src_suffix: str, dst_suffix: str):
    hits = [e for e in graph["edges"] if e["src"].endswith(src_suffix) and e["dst"].endswith(dst_suffix)]
    assert len(hits) == 1, (src_suffix, dst_suffix, hits)
    return hits[0]


def test_same_period_is_confirmed(graph):
    e = _edge(graph, f"{P}/3期/src/app/Order.java#Order", f"{P}/3期/src/app/Slot.java#Slot")
    assert e["confirm"] == "confirmed" and "assumption" not in e


def test_parent_by_change_list_is_confirmed(graph):
    e = _edge(graph, f"{P}/3期/src/app/Order.java#Order", f"{P}/2期/lib/Rate.java#Rate")
    assert e["confirm"] == "confirmed" and "assumption" not in e


def test_parent_by_name_order_is_conditional(graph):
    e = _edge(graph, f"{P}/2期/ソース/app/Settle.java#Settle", f"{P}/1期/lib/Util.java#Util")
    assert e["confirm"] == "conditional" and e["assumption"] == ["parent_by_order"]


def test_variant_folder_name_is_conditional(graph):
    e = _edge(graph, f"{P}/3期/src/app/Report.java#Report", f"{P}/2期/ソース/app/Fee.java#Fee")
    assert e["confirm"] == "conditional" and e["assumption"] == ["variance"]
    both = _edge(graph, f"{P}/2期/ソース/app/Settle.java#Settle", f"{P}/1期/src/app/Ledger.java#Ledger")
    assert both["assumption"] == ["parent_by_order", "variance"]


def test_nearest_period_wins_over_older_same_name(graph):
    dsts = [e["dst"] for e in graph["edges"] if e["src"].endswith("3期/src/app/Report.java#Report")]
    assert dsts and all("/1期/" not in d for d in dsts)
    assert not [f for f in graph["flags"] if f["reason"] == "ambiguous"]


def test_undetermined_parent_does_not_link_and_is_flagged(graph):
    assert not [e for e in graph["edges"] if "帳票/" in e["src"]]
    flag = next(f for f in graph["flags"] if f["reason"] == "parent_period_undetermined")
    assert flag["from"] == "帳票/改修/初版/app/Print.java" and flag["name"] == "Fmt"
    assert flag["candidate_paths"] == ["帳票/改修/改良版/app/Fmt.java"]


def test_common_part_outside_periods_is_confirmed(graph):
    e = _edge(graph, f"{P}/3期/src/app/Order.java#Order", "注文管理/本番資料/app/Money.java#Money")
    assert e["confirm"] == "confirmed"


def test_nodes_carry_period_only_inside_periods(graph):
    by_path = {n["path"]: n for n in graph["nodes"]}
    n = by_path[f"{P}/2期/ソース/app/Settle.java"]
    assert (n["period"], n["period_place"]) == ("2期", P)
    assert "period" not in by_path["注文管理/本番資料/app/Money.java"]


def test_edges_without_period_have_no_confirm():
    g = gg.build_graph("java1")
    assert not [e for e in g["edges"] if "confirm" in e or "assumption" in e]
    assert not [n for n in g["nodes"] if "period" in n]


def test_merging_candidates_conditional_wins():
    assert merge_confirm([{"_confirm": "confirmed", "_assumption": []},
                          {"_confirm": "conditional", "_assumption": ["variance"]}]) == {
        "confirm": "conditional", "assumption": ["variance"]}
    assert merge_confirm([{"_confirm": "confirmed", "_assumption": []}]) == {"confirm": "confirmed"}
    assert merge_confirm([{}]) == {}


def test_neo4j_rows_and_edge_view_keep_confirm():
    edge = {"type": "INVOKES", "src": "a", "dst": "b", "confirm": "conditional", "assumption": ["variance"]}
    row = world_neo4j._edge_row(edge, "g")
    assert (row["confirm"], row["assumption"]) == ("conditional", ["variance"])
    node = world_neo4j._node_row({"cid": "c", "name": "n", "period": "2期", "period_place": P}, "g", "w")
    assert (node["period"], node["period_place"]) == ("2期", P)
    view = world_neo4j.edge_view({"type": "INVOKES", "from": "a", "to": "b", "confirm": "conditional",
                                  "assumption": ["variance"]})
    assert view["confirm"] == "conditional" and view["assumption"] == ["variance"]
    assert "confirm" not in world_neo4j.edge_view({"type": "INVOKES", "from": "a", "to": "b"})


def test_impact_item_reports_conditional_route():
    raw = [{"type": "INVOKES", "doc": "x", "line": 1, "confirm": "confirmed"},
           {"type": "INVOKES", "doc": "x", "line": 2, "confirm": "conditional", "assumption": ["parent_by_order"]}]
    it = {"canonical_id": "c", "label": "Module", "name": "n", "path": "p", "trace": ["a", "b", "c"],
          "trace_cids": ["a", "b", "c"], "edges": raw}
    full = graph_tools._impact_item(it, None, 3)
    assert full["confirm"] == "conditional" and full["assumption"] == ["parent_by_order"]
    assert [r.get("confirm") for r in full["route"]] == ["confirmed", "conditional"]
    assert graph_tools._impact_item(it, None, 0)["confirm"] == "conditional"
    plain = graph_tools._impact_item({**it, "edges": [{"type": "INVOKES", "doc": "x", "line": 1}]}, None, 0)
    assert "confirm" not in plain


def _config_key_world(tmp_path, monkeypatch):
    from test_world_graph_analyzer_expansion_common import _config, _config_ref, _FakeAnalyzer, _key_child, _module

    from sherpa.ingest import world_graph
    from sherpa.ingest.analyzers import registry
    base = "帳票/改修"
    defs = {f"{base}/初版/app/App.fk": _module("App"),
            f"{base}/改良版/app/db.fk": _config("db.fk", [_key_child("db.url")])}
    refs = {f"{base}/初版/app/App.fk": _config_ref("db.url")}
    wd = tmp_path / "world"
    for rel in defs:
        (wd / rel).parent.mkdir(parents=True, exist_ok=True)
        (wd / rel).write_text("x", encoding="utf-8")
    (wd / base / "改良版" / "変更一覧.csv").write_text("改良版 の変更一覧（基準: 初版）\n追加,app/db.fk\n", encoding="utf-8")
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))
    return world_graph.build_world(wd, "w")


def test_config_key_reference_with_undetermined_parent_is_flagged_with_candidates(tmp_path, monkeypatch):
    nodes, edges, flags = _config_key_world(tmp_path, monkeypatch)
    assert not [e for e in edges if e["type"] == "ACCESSES"]
    flag = next(f for f in flags if f["reason"] == "parent_period_undetermined")
    assert flag["from"] == "帳票/改修/初版/app/App.fk" and flag["name"] == "db.url" and flag["via"] == "config_key"
    assert flag["candidate_paths"] == ["帳票/改修/改良版/app/db.fk"]
    app = next(n for n in nodes if n["path"] == "帳票/改修/初版/app/App.fk")
    assert [u["reason"] for u in app["unresolved"]] == ["parent_period_undetermined"]
