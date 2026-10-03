"""管理グラフ検索の単体テスト。Neo4j はスタブ。"""
from __future__ import annotations

from sherpa import graph_admin


class _Res:
    def __init__(self, rows):
        self._rows = rows

    def data(self):
        return self._rows


def _row(src="module:w:src", dst="copybook:w:dst", etype="COPIES"):
    base = {
        "source_id": src, "source_name": "TAXCALC", "source_label": "Module",
        "source_em": "static", "source_status": "active", "source_value": None,
        "source_top_scope": "4期", "source_phase": "03_開発", "source_category": "01_ソース",
        "source_path": "4期/03_開発/TAXCALC.cbl",
        "target_id": dst, "target_name": "TAX-CPY", "target_label": "Copybook",
        "target_em": "static", "target_status": "active", "target_value": None,
        "target_top_scope": "4期", "target_phase": "03_開発", "target_category": "01_ソース",
        "target_path": "4期/03_開発/TAX-CPY.cpy",
        "edge_type": etype, "edge_em": "static", "edge_status": "active",
    }
    return base


class _Session:
    def __init__(self):
        self.calls = []

    def run(self, q, **kw):
        self.calls.append((q, kw))
        if "MATCH (a:Entity)-[r:" in q:
            return _Res([_row(etype="COPIES")])
        return _Res([{**_row(src="module:w:taxcalc", dst=None, etype=None),
                      "target_id": None, "target_name": None, "target_label": None,
                      "edge_type": None}])


def test_graph_search_relationship_query_and_shape():
    s = _Session()
    g = graph_admin.graph_search(s, "w", relationship_types=["copies"], scope_paths=["4期"], limit=50)
    q, kw = s.calls[0]
    assert "MATCH (a:Entity)-[r:COPIES]->(b:Entity)" in q
    assert kw["world"] == "w" and kw["prefixes"] == ["4期"] and kw["limit"] == 50
    assert {n["name"] for n in g["nodes"]} == {"TAXCALC", "TAX-CPY"}
    assert g["edges"] == [{"source": "module:w:src", "target": "copybook:w:dst",
                           "type": "COPIES", "em": "static", "status": "active"}]


def test_graph_search_condition_uses_allowlisted_field():
    s = _Session()
    g = graph_admin.graph_search(s, "w", field="role", value="Module", op="eq")
    q, kw = s.calls[0]
    assert "MATCH (n:Entity {world_id:$world})" in q
    assert "[l IN labels(n) WHERE l<>'Entity'][0]" in q
    assert kw["cond_value"] == "Module"
    assert g["nodes"][0]["type"] == "DataItem" or g["nodes"][0]["type"] == "Module"


def test_graph_search_rejects_unknown_terms():
    s = _Session()
    try:
        graph_admin.graph_search(s, "w", relationship_types=["CALLS"])
        assert False, "CALLS は実語彙ではないので拒否する"
    except ValueError as e:
        assert "unknown relationship type" in str(e)
    try:
        graph_admin.graph_search(s, "w", field="free_cypher", value="x")
        assert False, "属性名は allowlist のみ"
    except ValueError as e:
        assert "unknown condition field" in str(e)

