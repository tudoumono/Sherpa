"""優先・除外 2 巡目の是正の再現: 原因の調べ（近傍）・未解決の参照・`graph_resolve` の上限。名前は架空。"""
from __future__ import annotations

import json

import pytest

from sherpa import lens_service as L
from sherpa import path_rules as PR

WORLD = "prrv2"


@pytest.fixture(autouse=True)
def _rules(monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "ZZold", "kind": "exclude", "outside_scope": False}]}))


def _src(doc, **kw):
    return {"via": "call", "doc_id": doc, "file": doc, "line": 1, **kw}


def test_related_hides_excluded_nodes_edges_and_nested_sources(monkeypatch):
    """1: 経路の全ノードに除外の条件を掛け、辺の根拠は資料のパス単位で組み直す。"""
    cap = {}
    rows = [
        {"cid": "c1", "name": "keep", "label": "Module", "status": "active", "path_names": ["a", "keep"], "dist": 1,
         "edges": [{"type": "INVOKES", "from": "a", "to": "keep", "doc": "ZZnew/a.cbl", "line": 1, "via": "call",
                    "sources_overflow": 4,
                    "sources": json.dumps([_src("ZZnew/a.cbl", evidence_text="OK"),
                                           _src("ZZnew/b.cbl", evidence_text="SECRETTEXT", from_def={"file": "ZZold/d.cbl"}),
                                           _src("ZZold/c.cbl", evidence_text="SECRETTEXT2")]),
                    }]},
        {"cid": "c2", "name": "via-old-doc", "label": "Module", "status": "active", "path_names": ["a", "x"], "dist": 1,
         "edges": [{"type": "INVOKES", "from": "a", "to": "x", "doc": "ZZold/e.cbl", "line": 1, "via": "call",
                    "sources": None, "sources_overflow": 0}]},
    ]

    def fake(session, cy, **kw):
        cap["cy"], cap["kw"] = cy, kw
        return rows
    monkeypatch.setattr(L, "_run_capped", fake)
    monkeypatch.setattr(L, "check_schema_era", lambda *a, **k: None)
    monkeypatch.setattr(L, "read_plugin_failures", lambda *a, **k: None)
    monkeypatch.setattr(L, "read_unparsed_sources", lambda *a, **k: None)
    out = L.neo4j_related(None, ["c0"], WORLD, None)
    assert cap["kw"]["x_exact"] == ["ZZold"] and cap["kw"]["x_pre"] == ["ZZold/"]
    assert "all(n IN nodes(p) WHERE n.world_id=$world AND (CASE WHEN n.membership_paths" in cap["cy"]   # 経路の全ノード（所属ごと）
    assert [c["name"] for c in out] == ["keep"]
    assert "SECRET" not in json.dumps(out, ensure_ascii=False)
    assert [s["doc_id"] for s in out[0]["edges"][0]["sources"]] == ["ZZnew/a.cbl"]
    assert out[0]["edges"][0]["sources_overflow_count"] == 0                  # 保存済みの超過の数は捨てる（除外の根拠の数を残さない）
    # 起点の選びにも掛かる
    seen = []
    monkeypatch.setattr(L, "_run_capped", lambda session, cy, **kw: seen.append((cy, kw)) or [])
    L.resolve_anchor(None, "x", WORLD, None)
    assert "$x_exact" in seen[0][0] and seen[0][1]["x_pre"] == ["ZZold/"]
    # 設定が無ければ問い合わせは従来どおり（除外の述語を足さない）
    seen.clear()
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))
    L.resolve_anchor(None, "x", WORLD, None)
    assert "x_exact" not in seen[0][0] and "x_exact" not in seen[0][1]


def test_unresolved_omitted_does_not_reveal_excluded_entries():
    """2: 除外の項目だけの一覧と省略件数で、除外の存在を示さない。"""
    rules = PR.rules_for(WORLD)
    only_hidden = {"available": True, "omitted": 7, "items": [{"path": "ZZold/a.cbl", "name": "S"}]}
    out = PR.scrub_unresolved(only_hidden, rules)
    assert out["items"] == [] and out["omitted"] is None
    assert PR.scrub_unresolved({"available": True, "omitted": 0, "items": []}, rules)["omitted"] == 0
    assert PR.scrub_unresolved(only_hidden, PR.EMPTY) is only_hidden


def test_graph_resolve_applies_exclusion_before_the_limit(monkeypatch):
    """3: 先頭が除外で埋まっていても、見える候補が limit 件まで返る（除外は LIMIT の前）。"""
    from sherpa import graph_tools
    from sherpa.ingest import world_neo4j
    seen = {}

    def fake(session, cy, **kw):
        seen["cy"], seen["kw"] = cy, kw
        # Cypher が除外を LIMIT の前に掛けている前提で、見える行だけを limit+1 件返す
        return [{"cid": f"c{i}", "label": "Module", "name": f"n{i}", "path": f"ZZnew/{i}.cbl", "qname": None,
                 "status": "active"} for i in range(kw["lim"])]
    monkeypatch.setattr(world_neo4j, "_run_read_capped", fake)
    monkeypatch.setattr(world_neo4j, "check_schema_era", lambda *a, **k: None)
    res = graph_tools._resolve(None, WORLD, None, False, name="n", path="", kind="", limit=20)
    assert len(res["candidates"]) == 20 and res["count"] is None            # 上限超過を正しく申告
    cy = seen["cy"]
    assert "$x_exact" in cy and cy.index("$x_exact") < cy.index("LIMIT $lim")
    assert seen["kw"]["x_exact"] == ["ZZold"]
