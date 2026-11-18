"""データの流れ（ファイル受け渡し）を実 Neo4j に載せて、影響たどりの第 2 段が候補を別枠で返す（要 Neo4j・`make up`）。

`fixtures/dataflow`（架空: SALESCALC が `PROD.SALES.CALC` を書き REPORTGEN が読む）の `build_world` の出力をそのまま `load_world` し、
`Dataset` 節点と `FLOWS_TO` 辺の保存・`flow_depth=0` の不変・候補の代表経路を確かめる。
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest
from _world_registry import register_test_world

from sherpa import graph_tools
from sherpa.ingest import world_graph, world_neo4j

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD = "dataflow_real_test"          # 使い捨て（テスト後に削除）


@pytest.fixture
def graph():
    nodes, edges, _flags = world_graph.build_world(ROOT / "fixtures" / "dataflow", WORLD)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)
    return {n["name"] + "@" + str(n["top_scope"]): n["cid"] for n in nodes}


@pytest.fixture
def session(graph):
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


def test_dataset_nodes_and_flow_edges_are_stored(session):
    rows = session.run(
        "MATCH (d:Dataset {world_id:$w}) RETURN d.name AS name, d.top_scope AS top, d.path AS path, d.value AS value, "
        "d.sources AS sources ORDER BY d.top_scope", w=WORLD).data()
    assert [(r["name"], r["top"], r["path"], r["value"]) for r in rows] == [
        ("PROD.SALES.CALC", "gen1", "gen1/NIGHTLY1.jcl", "gdg=true disp=NEW"),
        ("PROD.SALES.CALC", "gen2", "gen2/NIGHTLY2.jcl", None)]
    assert "NIGHTLY1.jcl" in rows[0]["sources"]
    vias = session.run("MATCH ()-[r:FLOWS_TO {world_id:$w}]->() RETURN r.via AS via, count(r) AS n ORDER BY via", w=WORLD).data()
    assert {r["via"]: r["n"] for r in vias} == {"read_file": 2, "use": 1, "write_file": 1}


@pytest.mark.parametrize("start", ["RATE-CPY@gen1", "TAX-RATE@gen1"])
def test_reader_program_is_a_flow_candidate_and_structure_is_untouched(session, graph, start):
    cid = graph[start]
    plain = world_neo4j.world_impact(session, [cid], WORLD, depth=5, detail=True)
    with_flow = world_neo4j.world_impact(session, [cid], WORLD, depth=5, detail=True, flow_depth=3)
    structure = [i for i in with_flow if i["origin"] == "structure"]
    flow = {i["name"]: i for i in with_flow if i["origin"] == "flow"}
    assert [(i["canonical_id"], i["trace_cids"]) for i in structure] == [(i["canonical_id"], i["trace_cids"]) for i in plain]
    assert set(flow) == {"PROD.SALES.CALC", "REPORTGEN", "NIGHTLY2"}
    assert [e["type"] for e in flow["REPORTGEN"]["edges"]][:2] == ["FLOWS_TO", "FLOWS_TO"]
    assert [e["via"] for e in flow["REPORTGEN"]["edges"]][:2] == ["read_file", "write_file"]


def test_run_world_impact_and_graph_impact_return_candidates_apart(session, graph):
    res = world_neo4j.run_world_impact(session, "RATE-CPY", WORLD, flow_depth=3)
    assert {i["name"] for i in res["items"]} == {"SALESCALC", "NIGHTLY1"}
    assert {i["name"] for i in res["candidates"]} == {"PROD.SALES.CALC", "REPORTGEN", "NIGHTLY2"}
    assert res["coverage"]["flow"] == {"requested": 3, "truncated": False}
    gi = graph_tools._impact(session, WORLD, None, False, False, None, canonical_id=graph["RATE-CPY@gen1"], depth=5, limit=50,
                             evidence_limit=3, flow_depth=3)
    assert {c["name"] for c in gi["flow_candidates"]} == {"PROD.SALES.CALC", "REPORTGEN", "NIGHTLY2"}
    assert gi["summary"]["flow_total"] == 3 and gi["coverage"]["complete"] is True
    assert "REPORTGEN" not in {i["name"] for i in gi["impact"]}


@pytest.fixture
def scoped_graph(tmp_path):
    def program(name, files, mode):
        return (f"       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {name}.\n"
                "       ENVIRONMENT DIVISION.\n       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n"
                + "".join(f"           SELECT {f} ASSIGN TO UT-S-{dd}.\n" for f, dd in files)
                + "       DATA DIVISION.\n       FILE SECTION.\n"
                + "".join(f"       FD {f}.\n       01 {f}-REC PIC X(80).\n" for f, _dd in files)
                + f"       PROCEDURE DIVISION.\n           OPEN {mode} "
                + " ".join(f for f, _dd in files) + ".\n           STOP RUN.\n")

    files = {
        "g/z/WRITER.cbl": program("WRITER", [("OUT-F", "OUTDD"), ("ALT-F", "ALTDD")], "OUTPUT"),
        "g/z/READER.cbl": program("READER", [("IN-F", "INDD")], "INPUT"),
        "g/z/OTHER.cbl": program("OTHER", [("IN-F", "INDD")], "INPUT"),
        "g/a/J0.jcl": "//J0 JOB\n//S EXEC PGM=READER\n//INDD DD DSN=DEMO.SHARED,DISP=SHR\n",
        "g/z/J1.jcl": "//J1 JOB\n//S EXEC PGM=WRITER\n//OUTDD DD DSN=DEMO.SHARED,DISP=NEW\n"
                      "//ALTDD DD DSN=DEMO.OUTSIDE,DISP=NEW\n"
                      "//R EXEC PGM=READER\n//INDD DD DSN=DEMO.SHARED,DISP=SHR\n",
        "g/zz/J2.jcl": "//J2 JOB\n//S EXEC PGM=OTHER\n//INDD DD DSN=DEMO.OUTSIDE,DISP=SHR\n",
    }
    for rel, content in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    world = WORLD + "_scope"
    nodes, edges, _flags = world_graph.build_world(tmp_path, world)
    for edge in edges:
        if edge["type"] == "FLOWS_TO":
            edge["sources"].append({"doc_id": "g/z/.env", "file": "g/z/.env", "line": 1})
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, world, env["uri"], env["user"], env["pw"])
    register_test_world(world)
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        with drv.session() as s:
            yield s, world, next(n["cid"] for n in nodes if n["name"] == "WRITER")
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=world)
    finally:
        drv.close()


def test_shared_dataset_uses_the_selected_subfolder_membership(scoped_graph):
    session, world, cid = scoped_graph
    plain = world_neo4j.world_impact(session, [cid], world, ["g/z"], depth=5, detail=True)
    result = world_neo4j.world_impact(session, [cid], world, ["g/z"], depth=5, detail=True, flow_depth=3)
    flow = {i["name"]: i for i in result if i["origin"] == "flow"}
    assert "READER" in flow and flow["DEMO.SHARED"]["path"] == "g/z/J1.jcl"
    assert [{k: v for k, v in i.items() if k != "origin"} for i in result if i["origin"] == "structure"] == plain
    gi = graph_tools._impact(session, world, ["g/z"], False, False, None, canonical_id=cid,
                             depth=5, limit=50, evidence_limit=10, flow_depth=3)
    assert "READER" in {i["name"] for i in gi["flow_candidates"]}
    for candidates in ([i for i in result if i["origin"] == "flow"], gi["flow_candidates"]):
        serialized = json.dumps(candidates)
        assert "g/a/" not in serialized and "g/zz/" not in serialized and ".env" not in serialized


def test_in_scope_endpoints_do_not_admit_an_outside_dd_binding(scoped_graph):
    session, world, cid = scoped_graph
    result = world_neo4j.run_world_impact(session, "WRITER", world, ["g/z"], flow_depth=3)
    assert "DEMO.OUTSIDE" in {i["name"] for i in result["candidates"]}
    assert "OTHER" not in {i["name"] for i in result["candidates"]}
    assert "g/zz/" not in json.dumps(result["candidates"])
    assert ".env" not in json.dumps(result["candidates"])
    info = {}
    world_neo4j.world_impact(session, [cid], world, ["g/z"], info=info, flow_depth=2)
    assert info["flow_depth_truncated"] is False


def test_neighbors_limit_shared_datasets_and_dd_evidence_to_the_selected_subfolder(scoped_graph, monkeypatch):
    from sherpa import lens_service
    session, world, cid = scoped_graph
    related = lens_service.neo4j_related(session, [cid], world, ["g/z"], depth=2, sources_limit=10)
    by_name = {r["name"]: r for r in related}
    assert "OTHER" not in by_name
    assert {"DEMO.SHARED", "DEMO.OUTSIDE", "READER"} <= set(by_name)
    serialized = json.dumps(related)
    assert "g/a/" not in serialized and "g/zz/" not in serialized and ".env" not in serialized
    monkeypatch.setattr(lens_service, "_RELATED_REL", lens_service._RELATED_REL.replace("|FLOWS_TO", ""))
    structure = lens_service.neo4j_related(session, [cid], world, ["g/z"], depth=2, sources_limit=10)
    assert by_name["J1"] == next(r for r in structure if r["name"] == "J1")
