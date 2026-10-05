"""辺の根拠 `sources` の保存と返却・旧 era の拒否（ANA-14 S7・要 Neo4j・`make up`）。

使い捨ての資料フォルダ ID で `build_world`→`load_world` を通し、影響・近傍の辺に `via`・`rule`・`sources` が返ること、
辺の本数・影響の件数が参照の数で増えないこと、保存形式の版が旧いグラフを読み取りが拒否することを確かめる。
"""
from __future__ import annotations

import os

import pytest

from sherpa import impact_service, lens_service
from sherpa.ingest import world_graph, world_neo4j
from _world_registry import register_test_world

WORLD = "s7_sources_test"          # 使い捨て（テスト後に削除）


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


_CALLS = "".join("  helper();\n" for _ in range(5))
_FILES = {"g/main.c": "int main(void) {\n" + _CALLS + "}\n",
          "g/helper.c": "int helper(void) {\n  return 1;\n}\n",
          "g/設計書.md": "# 概要\nhelper を呼び出す。\n\n## 詳細\n手順の途中で再び helper を使う。\n"}


def _main_edge(r):
    (it,) = [i for i in r["items"] if i["name"] == "main"]
    (e,) = [e for e in it["evidence"] if e["type"] == "INVOKES"]
    return e


def test_impact_edges_carry_via_rule_and_the_first_sources(session, tmp_path):
    _load_files(tmp_path, _FILES)
    e = _main_edge(impact_service.run_impact(session, "helper", WORLD))
    assert (e["via"], e["rule"], e["line"]) == ("call", "nearest_name", 2)
    assert [s["line"] for s in e["sources"]] == [2, 3, 4]                       # 既定は先頭 3 件
    assert e["sources_overflow_count"] == 2
    assert all(s["from_def"] == {"file": "g/main.c", "key": "main.c.main"} for s in e["sources"])
    e = _main_edge(impact_service.run_impact(session, "helper", WORLD, evidence_limit=10))
    assert [s["line"] for s in e["sources"]] == [2, 3, 4, 5, 6] and e["sources_overflow_count"] == 0
    e = _main_edge(impact_service.run_impact(session, "helper", WORLD, evidence_limit=0))
    assert e["sources"] == [] and e["sources_overflow_count"] == 5


def test_edge_and_impact_counts_do_not_grow_with_the_number_of_references(session, tmp_path):
    _n, edges, _f = _load_files(tmp_path, _FILES)
    triples = {(e["type"], e["src"], e["dst"]) for e in edges}
    stored = session.run("MATCH (a:Entity {world_id:$w})-[r]->() RETURN count(r) AS c", w=WORLD).single()["c"]
    assert stored == len(edges) == len(triples)
    r = world_neo4j.run_world_impact(session, "helper", WORLD)
    assert sorted(i["name"] for i in r["items"]) == ["helper.c", "main", "main.c"]
    assert all(len(i["trace"]) == len(i["evidence"]) + 1 for i in r["items"])      # 代表経路は最短 1 本のまま


def test_the_old_edge_properties_are_not_written(session, tmp_path):
    _load_files(tmp_path, _FILES)
    c = session.run("MATCH ()-[r]->() WHERE r.world_id=$w AND (r.source IS NOT NULL OR r.evidence IS NOT NULL "
                    "OR r.rule IS NOT NULL) RETURN count(r) AS c", w=WORLD).single()["c"]
    assert c == 0


def test_neighbors_return_via_rule_and_where_the_document_mentions_the_code(session, tmp_path):
    _load_files(tmp_path, _FILES)
    cards = lens_service.neighbor_cards_graph_only(WORLD, "helper")
    (doc,) = [c for c in cards if c["name"] == "g/設計書.md"]
    (e,) = doc["evidence"]["edges"]
    assert (e["type"], e["via"], e["from"], e["to"]) == ("DOCUMENTS", "mention", "g/設計書.md", "helper")
    assert [s["locator"] for s in e["sources"]] == ["概要・本文 2 行目", "詳細・本文 5 行目"]
    (caller,) = [c for c in cards if c["name"] == "main" and c["distance"] == 1]
    (e,) = caller["evidence"]["edges"]
    assert e["via"] == "call" and e["rule"] == "nearest_name" and len(e["sources"]) == 5 and e["sources_overflow_count"] == 0


def test_a_graph_with_an_older_storage_version_is_refused_until_rebuilt(session, tmp_path):
    nodes, edges, _f = _load_files(tmp_path, _FILES)
    env = world_neo4j._env()
    session.run("MATCH (m:SherpaMeta {world_id:$w}) SET m.schema_era='old-storage'", w=WORLD)
    assert world_neo4j.check_graph_counts(WORLD, env["uri"], env["user"], env["pw"]) == "era_mismatch"
    with pytest.raises(world_neo4j.GraphSchemaEraError):
        world_neo4j.run_world_impact(session, "helper", WORLD)
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])      # 今すぐ更新での作り直し
    assert world_neo4j.check_graph_counts(WORLD, env["uri"], env["user"], env["pw"]) is None
    assert world_neo4j.run_world_impact(session, "helper", WORLD)["items"]
