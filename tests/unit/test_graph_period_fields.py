"""Neo4j に保存した期（`period`・`period_place`）が、候補・影響先・起点の取得と道具の戻りに欠けず届くことの単体テスト。

session スタブは、問い合わせが `AS period`／`AS period_place` を選んだ欄だけを行に載せる（選ばない問い合わせでは欄が落ちる）。
"""
from __future__ import annotations

from test_world_neo4j_overload import _FakeResult

from sherpa import graph_tools
from sherpa.ingest import world_neo4j as wn

_STORED = {"period": "2026-04", "period_place": "kaisei/2026-04"}


def _selected(text: str, base: dict, stored: dict) -> dict:
    row = dict(base)
    for k, v in stored.items():
        if f"AS {k}" in text:
            row[k] = v
    return row


class _Session:
    def __init__(self, stored: dict):
        self.stored = stored

    def run(self, query, **p):
        text = query.text if hasattr(query, "text") else query
        if "m.schema_era AS era" in text:
            return _FakeResult([{"c": 1, "era": wn.GRAPH_SCHEMA_ERA}])
        if "s.canonical_id IN $starts" in text:
            return _FakeResult([{"cid": "s", "name": "S", "path": "g/s.cbl"}])
        if "$frontier" in text or "frontier" in p:
            if p["frontier"] != ["s"]:
                return _FakeResult([])
            return _FakeResult([_selected(text, {
                "cid": "a", "name": "A", "label": "Module", "status": "active", "dpath": "g/a.cbl", "top": "g",
                "analyzer": None, "to_cid": "s", "edge": {"type": "INVOKES", "doc": "g/a.cbl", "line": 1}}, self.stored)])
        if "n.canonical_id IN $cids" in text:
            return _FakeResult([_selected(text, {"cid": "a", "label": "Module", "name": "A", "path": "g/a.cbl",
                                                 "qname": None}, self.stored)])
        return _FakeResult([_selected(text, {"cid": "a", "label": "Module", "name": "A", "path": "g/a.cbl", "qname": None,
                                             "status": "active"}, self.stored)])


def test_candidates_carry_period_and_place():
    out = wn.list_world_candidates(_Session(_STORED), "A", "w")
    assert out[0]["period"] == "2026-04" and out[0]["period_place"] == "kaisei/2026-04"


def test_entities_carry_period_and_place():
    out = wn.get_world_entities(_Session(_STORED), ["a"], "w")
    assert out[0]["period"] == "2026-04" and out[0]["period_place"] == "kaisei/2026-04"


def test_impact_items_carry_period_and_place():
    items = wn.world_impact(_Session(_STORED), ["s"], "w", depth=2, detail=True)
    assert items[0]["period"] == "2026-04" and items[0]["period_place"] == "kaisei/2026-04"


def test_node_outside_a_period_gets_no_period_fields():
    s = _Session({})
    assert "period" not in wn.get_world_entities(s, ["a"], "w")[0]
    assert "period" not in wn.list_world_candidates(s, "A", "w")[0]
    assert "period" not in wn.world_impact(s, ["s"], "w", depth=2, detail=True)[0]


def test_tool_replies_forward_period_and_place():
    item = wn.world_impact(_Session(_STORED), ["s"], "w", depth=2, detail=True)[0]
    for evidence_limit in (0, 3):
        out = graph_tools._impact_item(item, None, evidence_limit)
        assert out["period"] == "2026-04" and out["period_place"] == "kaisei/2026-04"
    view = graph_tools._node_view(wn.get_world_entities(_Session(_STORED), ["a"], "w")[0])
    assert view["period"] == "2026-04" and view["period_place"] == "kaisei/2026-04"
    assert "period" not in graph_tools._node_view({"canonical_id": "a", "label": "Module", "name": "A", "path": "p"})
