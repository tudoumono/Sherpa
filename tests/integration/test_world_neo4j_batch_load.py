"""グラフ保存世代の小分け書き込み統合テスト（要 Neo4j・`make up`）。

小さい設定でも結果が同じこと、途中失敗では旧世代だけが読まれ、次回に作りかけが消えること、
世代印のない旧形式から最初の1回で移行できることを固定する。
"""
from __future__ import annotations

import copy
import os
import pathlib

import pytest
from tests._replace_row import offline_row

ROOT = pathlib.Path(__file__).resolve().parents[2]
os.chdir(ROOT)

FIXTURE = ROOT / "fixtures" / "corpus" / "ana-s3"       # 50件設定で同一ラベルが複数txに分かれる実 fixture
WORLD_A = "graphmem_batch_equiv_a"                       # 使い捨て（テスト後に削除）
WORLD_B = "graphmem_batch_equiv_b"
WORLD_FAIL = "graphmem_batch_midfail"
WORLD_LEGACY = "graphmem_legacy_migration"
WORLD_DOWNGRADE = "graphmem_downgrade_reset"
WORLD_DOWNGRADE_OTHER = "graphmem_downgrade_other"


def _driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")),
    )


def _cleanup(drv, world):
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=world)
        s.run("MATCH (m:SherpaMeta {world_id:$w}) DELETE m", w=world)


def _retarget(nodes, edges, world):
    """fixture 由来の nodes/edges を使い捨て world_id へ付け替える（cid/src/dst の埋め込み world も置換）。"""
    ns = copy.deepcopy(nodes)
    es = copy.deepcopy(edges)
    for n in ns:
        n["cid"] = n["cid"].replace(":wtmp:", f":{world}:")
        n["world_id"] = world
    for e in es:
        e["src"] = e["src"].replace(":wtmp:", f":{world}:")
        e["dst"] = e["dst"].replace(":wtmp:", f":{world}:")
    return ns, es


def _dump(drv, world):
    """現行世代のノード/エッジ属性を、保存専用属性を除いて正規化する。"""
    with drv.session() as s:
        nodes = s.run(
            "MATCH (m:SherpaMeta {world_id:$w}) "
            "MATCH (n:Entity {world_id:$w, graph_generation:m.active_generation}) "
            "RETURN labels(n) AS labels, properties(n) AS props", w=world).data()
        edges = s.run(
            "MATCH (m:SherpaMeta {world_id:$w}) "
            "MATCH (a:Entity {world_id:$w, graph_generation:m.active_generation})-[r]->"
            "(b:Entity {world_id:$w, graph_generation:m.active_generation}) "
            "WHERE r.graph_generation=m.active_generation "
            "RETURN a.canonical_id AS a, type(r) AS type, b.canonical_id AS b, properties(r) AS props",
            w=world).data()

    def norm(v):
        if isinstance(v, str):
            return v.replace(f":{world}:", ":w:").replace(world, "w")
        if isinstance(v, list):
            return [norm(x) for x in v]
        if isinstance(v, dict):
            return {k: norm(x) for k, x in v.items() if k not in ("storage_id", "graph_generation")}
        return v

    nrows = sorted([norm(r) for r in nodes], key=lambda r: r["props"]["canonical_id"])
    erows = sorted([norm(r) for r in edges], key=lambda r: (r["a"], r["type"], r["b"]))
    return nrows, erows


def test_batch_rows_do_not_change_the_final_graph():
    """バッチ行数=50とデフォルトで、現行世代の全ノード/エッジ属性が一致する。"""
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world

    nodes, edges, flags = world_graph.build_world(FIXTURE, "wtmp")
    assert sum(n["label"] == "Module" for n in nodes) > 50

    env = world_neo4j._env()
    drv = _driver()
    orig_batch_rows = world_neo4j._NEO4J_BATCH_ROWS
    try:
        na, ea = _retarget(nodes, edges, WORLD_A)
        world_neo4j.load_world(na, ea, WORLD_A, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_A))
        register_test_world(WORLD_A)

        nb, eb = _retarget(nodes, edges, WORLD_B)
        world_neo4j._NEO4J_BATCH_ROWS = 50
        try:
            world_neo4j.load_world(nb, eb, WORLD_B, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_B))
        finally:
            world_neo4j._NEO4J_BATCH_ROWS = orig_batch_rows
        register_test_world(WORLD_B)

        nodes_a, edges_a = _dump(drv, WORLD_A)
        nodes_b, edges_b = _dump(drv, WORLD_B)
        assert nodes_a == nodes_b
        assert edges_a == edges_b
    finally:
        _cleanup(drv, WORLD_A)
        _cleanup(drv, WORLD_B)
        drv.close()


def test_mid_batch_failure_keeps_old_visible_and_next_load_removes_residue():
    """新世代の途中失敗では旧世代だけが読まれ、次の成功時に作りかけと旧世代が消える。"""
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world

    nodes, edges, flags = world_graph.build_world(FIXTURE, "wtmp")
    old_nodes, old_edges = _retarget(nodes, edges, WORLD_FAIL)
    new_nodes, new_edges = copy.deepcopy(old_nodes), copy.deepcopy(old_edges)
    target = new_nodes[0]["cid"]
    old_nodes[0]["name"] = "OLD_VISIBLE"
    new_nodes[0]["name"] = "NEW_VISIBLE"

    orig_batch_rows = world_neo4j._NEO4J_BATCH_ROWS
    orig_row_fn = world_neo4j._node_row
    calls = {"n": 0}

    def boom(n, *args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 51:
            raise RuntimeError("injected mid-batch failure (GRAPH-MEM test)")
        return orig_row_fn(n, *args, **kwargs)

    env = world_neo4j._env()
    drv = _driver()
    register_test_world(WORLD_FAIL)                        # 失敗しても session teardown が後始末する
    try:
        world_neo4j.load_world(old_nodes, old_edges, WORLD_FAIL, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_FAIL))
        old_generation = None
        with drv.session() as s:
            old_generation = world_neo4j.read_generation(s, WORLD_FAIL)
        world_neo4j._NEO4J_BATCH_ROWS = 50
        world_neo4j._node_row = boom
        with pytest.raises(RuntimeError, match="injected mid-batch failure"):
            world_neo4j.load_world(new_nodes, new_edges, WORLD_FAIL, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_FAIL))
        with drv.session() as s:
            assert world_neo4j.read_generation(s, WORLD_FAIL) == old_generation
            visible = world_neo4j.get_world_entities(s, [target], WORLD_FAIL)
            assert visible[0]["name"] == "OLD_VISIBLE"
            assert world_neo4j.resolve_world_entity(s, "NEW_VISIBLE", WORLD_FAIL) == []
            assert world_neo4j.list_world_candidates(s, "NEW_VISIBLE", WORLD_FAIL) == []
            assert world_neo4j.list_world_candidates(s, "OLD_VISIBLE", WORLD_FAIL)
            generations = s.run(
                "MATCH (n:Entity {world_id:$w}) RETURN n.graph_generation AS g, count(n) AS c",
                w=WORLD_FAIL).data()
            assert len(generations) == 2
            assert sorted(r["c"] for r in generations)[0] == 50

        world_neo4j._node_row = orig_row_fn
        world_neo4j.load_world(new_nodes, new_edges, WORLD_FAIL, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_FAIL))
        with drv.session() as s:
            visible = world_neo4j.get_world_entities(s, [target], WORLD_FAIL)
            assert visible[0]["name"] == "NEW_VISIBLE"
            assert world_neo4j.list_world_candidates(s, "OLD_VISIBLE", WORLD_FAIL) == []
            generations = s.run(
                "MATCH (n:Entity {world_id:$w}) RETURN DISTINCT n.graph_generation AS g",
                w=WORLD_FAIL).data()
            assert len(generations) == 1 and generations[0]["g"] is not None
    finally:
        world_neo4j._NEO4J_BATCH_ROWS = orig_batch_rows
        world_neo4j._node_row = orig_row_fn
        _cleanup(drv, WORLD_FAIL)
        drv.close()


def test_first_load_migrates_generationless_legacy_graph():
    """世代印と保存専用IDのない旧形式は最初の切替まで読め、1回の load で新形式へ移る。"""
    from sherpa.ingest import world_neo4j
    from _world_registry import register_test_world

    drv = _driver()
    env = world_neo4j._env()
    register_test_world(WORLD_LEGACY)
    legacy_cid = f"module:{WORLD_LEGACY}:g/old.cbl#OLD"
    new_nodes = [{"cid": f"module:{WORLD_LEGACY}:g/new.cbl#NEW", "label": "Module", "name": "NEW",
                  "world_id": WORLD_LEGACY, "path": "g/new.cbl", "top_scope": "g"}]
    try:
        _cleanup(drv, WORLD_LEGACY)
        with drv.session() as s:
            s.run("DROP CONSTRAINT entity_storage_id IF EXISTS")
            s.run("CREATE CONSTRAINT canon IF NOT EXISTS FOR (n:Entity) REQUIRE n.canonical_id IS UNIQUE")
            s.run("CREATE (n:Entity:Module {canonical_id:$cid, world_id:$w, name:'OLD', path:'g/old.cbl'})",
                  cid=legacy_cid, w=WORLD_LEGACY)
            s.run("CREATE (m:SherpaMeta {world_id:$w, schema_era:$era, node_count:1, edge_count:0, generation:'legacy'})",
                  w=WORLD_LEGACY, era=world_neo4j.GRAPH_SCHEMA_ERA)
            assert world_neo4j.resolve_world_entity(s, "OLD", WORLD_LEGACY)[0]["canonical_id"] == legacy_cid

        world_neo4j.load_world(new_nodes, [], WORLD_LEGACY, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_LEGACY))
        with drv.session() as s:
            assert world_neo4j.resolve_world_entity(s, "OLD", WORLD_LEGACY) == []
            assert world_neo4j.resolve_world_entity(s, "NEW", WORLD_LEGACY)
            row = s.run(
                "MATCH (m:SherpaMeta {world_id:$w}) MATCH (n:Entity {world_id:$w}) "
                "RETURN m.active_generation AS active, n.graph_generation AS generation, n.storage_id AS storage_id",
                w=WORLD_LEGACY).single()
            assert row["active"] == row["generation"] and row["storage_id"]
    finally:
        _cleanup(drv, WORLD_LEGACY)
        drv.close()


def test_downgrade_reset_clears_two_generations_so_legacy_unique_constraint_can_be_made():
    """新旧 2 世代と世代の印が残った状態から流すと、Entity も世代の印も残らず、旧版の一意制約が作れる。"""
    from sherpa.ingest import world_neo4j
    from _world_registry import register_test_world

    drv = _driver()
    env = world_neo4j._env()
    register_test_world(WORLD_DOWNGRADE)
    register_test_world(WORLD_DOWNGRADE_OTHER)
    nodes = [{"cid": f"module:{WORLD_DOWNGRADE}:g/a.cbl#A", "label": "Module", "name": "A",
              "world_id": WORLD_DOWNGRADE, "path": "g/a.cbl", "top_scope": "g"}]
    other_nodes = [{"cid": f"module:{WORLD_DOWNGRADE_OTHER}:g/b.cbl#B", "label": "Module", "name": "B",
                    "world_id": WORLD_DOWNGRADE_OTHER, "path": "g/b.cbl", "top_scope": "g"}]
    orig_batch_rows = world_neo4j._NEO4J_BATCH_ROWS
    try:
        _cleanup(drv, WORLD_DOWNGRADE)
        _cleanup(drv, WORLD_DOWNGRADE_OTHER)
        world_neo4j.load_world(nodes, [], WORLD_DOWNGRADE, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_DOWNGRADE))
        world_neo4j.load_world(other_nodes, [], WORLD_DOWNGRADE_OTHER, env["uri"], env["user"], env["pw"], row=offline_row(WORLD_DOWNGRADE_OTHER))
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) "
                  "CREATE (:Entity:Module {storage_id:'residue-1', canonical_id:n.canonical_id, world_id:$w, "
                  "name:'RESIDUE', graph_generation:'failed-generation'})", w=WORLD_DOWNGRADE)
            assert s.run("MATCH (n:Entity {world_id:$w}) RETURN count(DISTINCT n.graph_generation) AS c",
                         w=WORLD_DOWNGRADE).single()["c"] == 2

        world_neo4j._NEO4J_BATCH_ROWS = 1
        done = world_neo4j.reset_graph_for_downgrade(
            env["uri"], env["user"], env["pw"], worlds=[WORLD_DOWNGRADE])
        assert done == {"entities": 2, "metas": 1}

        with drv.session() as s:
            assert s.run("MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c", w=WORLD_DOWNGRADE).single()["c"] == 0
            assert s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN count(m) AS c", w=WORLD_DOWNGRADE).single()["c"] == 0
            assert s.run("MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c",
                         w=WORLD_DOWNGRADE_OTHER).single()["c"] == 1
            s.run("CREATE CONSTRAINT canon IF NOT EXISTS FOR (n:Entity) REQUIRE n.canonical_id IS UNIQUE")
    finally:
        world_neo4j._NEO4J_BATCH_ROWS = orig_batch_rows
        with drv.session() as s:
            s.run("DROP CONSTRAINT canon IF EXISTS")
        _cleanup(drv, WORLD_DOWNGRADE)
        _cleanup(drv, WORLD_DOWNGRADE_OTHER)
        drv.close()
