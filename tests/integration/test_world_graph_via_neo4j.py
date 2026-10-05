"""CODE-2（エッジ属性の透過）の統合テスト（要 Neo4j・`make up`）。

`RefCandidate.extra["via"]`（Java アナライザの `field_type`/`inject`/`call`/`extends`/`implements`）が
`world_neo4j.load_world` で実際に Neo4j のエッジ `via` プロパティへ載ることを固定する
（`tests/integration/test_world_graph_provenance_neo4j.py`＝L7 の `sources` と同じ手法・
使い捨て world_id・テスト後に削除）。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
os.chdir(ROOT)

WORLD = "code2_via_test"                      # 使い捨て（テスト後に削除）
FIXTURE = ROOT / "fixtures" / "corpus" / "java1"


def _driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")),
    )


def _cleanup(drv):
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=WORLD)


def test_via_lands_as_a_neo4j_edge_property():
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world

    nodes, edges, flags = world_graph.build_world(FIXTURE, WORLD)
    assert not any(f["reason"] == "unknown_via" for f in flags), flags

    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)

    drv = _driver()
    try:
        with drv.session() as s:
            rows = s.run(
                "MATCH (a:Entity {world_id:$w, name:'TaxCalculator'})"
                "-[r:INVOKES]->(b:Entity {world_id:$w, name:'AbstractCalculator'}) "
                "RETURN r.via AS via", w=WORLD,
            ).data()
            assert len(rows) == 1
            assert rows[0]["via"] == "extends"

            rows = s.run(
                "MATCH (a:Entity {world_id:$w, name:'InvoiceService'})"
                "-[r:INVOKES]->(b:Entity {world_id:$w, name:'TaxCalculator'}) "
                "RETURN r.via AS via", w=WORLD,
            ).data()
            assert {r["via"] for r in rows} == {"call"}
    finally:
        _cleanup(drv)
        drv.close()


def test_edge_without_via_has_null_property():
    """COBOL/JCL 等 `via` を積まないアナライザ由来のエッジは `via` が null のまま
    （属性追加は加算的＝既存エッジを汚さない）。"""
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world

    nodes, edges, flags = world_graph.build_world(FIXTURE, WORLD)
    assert flags == [] or all(f["reason"] != "unknown_via" for f in flags)

    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)

    drv = _driver()
    try:
        with drv.session() as s:
            rows = s.run(
                "MATCH ()-[r:CONTAINS {world_id:$w}]->() RETURN r.via AS via LIMIT 1", w=WORLD,
            ).data()
            assert len(rows) == 1 and rows[0]["via"] is None
    finally:
        _cleanup(drv)
        drv.close()


def test_sql_table_schema_and_qualified_name_survive_the_neo4j_round_trip():
    """SQL の Table ノードの `schema`・`qualified_name`（ANA-14 S5）が保存後に残る。schema を書かない表は null。"""
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world

    fixture = ROOT / "fixtures" / "corpus" / "ana-s5"
    nodes, edges, _flags = world_graph.build_world(fixture, WORLD)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)

    drv = _driver()
    try:
        with drv.session() as s:
            rows = s.run(
                "MATCH (t:Table {world_id:$w}) WHERE t.path IN ['g/sql/schemas.sql', 'g/sql/plain.sql'] "
                "RETURN t.canonical_id AS cid, t.name AS name, t.schema AS schema, "
                "t.qualified_name AS qn", w=WORLD,
            ).data()
        got = {r["cid"].split("#", 1)[1]: (r["name"], r["schema"], r["qn"]) for r in rows}
        assert got == {
            "CUSTOMER": ("CUSTOMER", "A", "A.CUSTOMER"),
            "B.CUSTOMER": ("CUSTOMER", "B", "B.CUSTOMER"),
            "X": ("X", None, None),
        }
    finally:
        _cleanup(drv)
        drv.close()
