"""未解決の申告の保存と返却・COPY の循環での影響の探索（ANA-14 S1b・要 Neo4j・`make up`）。

使い捨ての資料フォルダ ID で `build_world`→`load_world`→`run_world_impact`／`neighbor_cards_graph_only` を通し、
ノードの `unresolved` が影響・近傍の結果へ `{available, items, omitted}` で返ることを確かめる。
"""
from __future__ import annotations

import os

import pytest

from sherpa import lens_service
from sherpa.ingest import world_graph, world_neo4j
from _world_registry import register_test_world

WORLD = "s1b_unres_test"          # 使い捨て（テスト後に削除）


@pytest.fixture
def session():
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")))
    try:
        with drv.session() as s:
            yield s
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD)
    finally:
        drv.close()


def _load_files(tmp_path, files: dict):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    nodes, edges, flags = world_graph.build_world(tmp_path, WORLD)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)
    return nodes, edges, flags


_AMBIGUOUS = {"g/x/Helper.java": "class Helper {}\n", "g/y/Helper.java": "class Helper {}\n",
              "g/Main.java": "class Main {\n void m() { new Helper(); }\n}\n"}


def test_impact_returns_the_ambiguous_reference_with_path_line_and_reason(session, tmp_path):
    _load_files(tmp_path, _AMBIGUOUS)
    r = world_neo4j.run_world_impact(session, "Helper", WORLD)
    assert r["unresolved"] == {"available": True, "omitted": 0, "items": [
        {"path": "g/Main.java", "line": 2, "reason": "ambiguous", "kind": "Module", "name": "Helper",
         "via": "call", "from_def": {"file": "g/Main.java", "key": None}, "candidates": 2}]}


def test_impact_unresolved_respects_scope_prefixes(session, tmp_path):
    _load_files(tmp_path, _AMBIGUOUS)
    r = world_neo4j.run_world_impact(session, "Helper", WORLD, scope_prefixes=["g/x"])
    assert r["unresolved"]["items"] == [] and r["unresolved"]["available"] is True


def test_graph_without_stored_unresolved_reports_available_false(session, tmp_path):
    _load_files(tmp_path, _AMBIGUOUS)
    session.run("MATCH (m:SherpaMeta {world_id:$w}) REMOVE m.unresolved_stored", w=WORLD)
    r = world_neo4j.run_world_impact(session, "Helper", WORLD)
    assert r["unresolved"] == {"available": False, "items": [], "omitted": 0}


def test_per_file_overflow_is_returned_as_omitted(session, tmp_path):
    body = "".join(" new Zed();\n" for _ in range(60))
    _load_files(tmp_path, {"g/Many.java": "class Many {\n void m() {\n" + body + " }\n}\n"})
    u = world_neo4j.run_world_impact(session, "Zed", WORLD)["unresolved"]
    assert len(u["items"]) == 50 and u["omitted"] == 10


def test_neighbors_return_unresolved_for_the_anchor_name(session, tmp_path):
    _load_files(tmp_path, _AMBIGUOUS)
    cards = lens_service.neighbor_cards_graph_only(WORLD, "Helper")
    assert cards.unresolved["available"] is True
    assert [(i["path"], i["line"], i["reason"]) for i in cards.unresolved["items"]] == [("g/Main.java", 2, "ambiguous")]


_CYC_A = "       01 CYCA-REC.\n           COPY CYCB.\n"
_CYC_B = "       01 CYCB-REC.\n           COPY CYCA.\n"
_CYC_C = "       01 CYCC-REC.\n           COPY CYCA.\n"


def test_impact_over_a_copy_cycle_terminates_without_duplicates(session, tmp_path):
    """A→B→A の COPY の循環（と循環の外から入る C）。影響の探索は止まり、同じ影響先を重複して返さない。"""
    _n, _e, flags = _load_files(tmp_path, {"c/CYCA.cpy": _CYC_A, "c/CYCB.cpy": _CYC_B, "c/CYCC.cpy": _CYC_C})
    assert [f["paths"] for f in flags if f["reason"] == "copy_cycle"] == [["c/CYCA.cpy", "c/CYCB.cpy"]]
    r = world_neo4j.run_world_impact(session, "CYCA", WORLD)
    names = [i["name"] for i in r["items"]]
    assert len(names) == len(set(names))
    assert {"CYCB", "CYCC"} <= set(names)


_CASE = {"g/x/Helper.java": "class Helper {}\n", "g/y/Helper.java": "class Helper {}\n",
         "g/Main.java": "class Main {\n void m() { new Helper(); }\n}\n"}


def test_impact_matches_the_name_exactly_like_its_anchor_resolution(session, tmp_path):
    _load_files(tmp_path, _CASE)
    assert world_neo4j.run_world_impact(session, "helper", WORLD)["unresolved"]["items"] == []


def test_neighbors_match_the_name_case_insensitively(session, tmp_path):
    _load_files(tmp_path, _CASE)
    cards = lens_service.neighbor_cards_graph_only(WORLD, "helper")
    assert [i["name"] for i in cards.unresolved["items"]] == ["Helper"]
