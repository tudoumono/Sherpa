"""GRAPH-MEM（2026-09-04）の統合テスト（要 Neo4j・`make up`）。

`world_neo4j.load_world` の投入を UNWIND バッチへ分割しても（1）バッチ行数に関わらず最終的な
Neo4j 上のノード/エッジのプロパティ集合が同一であること、（2）バッチ送信の途中で例外が起きても
実 driver の managed transaction が本当にロールバックし、world が「半端な状態」を残さないことを
固定する。境界値/例外伝播の fake ベース確認は `tests/unit/test_world_neo4j_overload.py` 参照
（そちらは実 driver のロールバック挙動そのものは検証できないため、ここで実 Neo4j を使う）。
"""
from __future__ import annotations

import copy
import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
os.chdir(ROOT)

FIXTURE = ROOT / "fixtures" / "corpus" / "java1"        # 重複エッジ（同一 src/dst/type）を含む実 fixture
WORLD_A = "graphmem_batch_equiv_a"                       # 使い捨て（テスト後に削除）
WORLD_B = "graphmem_batch_equiv_b"
WORLD_FAIL = "graphmem_batch_midfail"


def _driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")),
    )


def _cleanup(drv, world):
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=world)


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
    """ノード/エッジのプロパティ集合を正規化して取り出す（cid の world 部分は共通トークンへ置換して比較可能にする）。"""
    with drv.session() as s:
        nodes = s.run(
            "MATCH (n:Entity {world_id:$w}) RETURN n.canonical_id AS cid, labels(n) AS labels, "
            "n.name AS name, n.top_scope AS top, n.value AS value, n.status AS status, "
            "n.sources AS sources", w=world).data()
        edges = s.run(
            "MATCH (a:Entity {world_id:$w})-[r]->(b:Entity {world_id:$w}) "
            "RETURN a.canonical_id AS a, type(r) AS t, b.canonical_id AS b, r.doc AS doc, "
            "r.line AS line, r.via AS via, r.sources AS sources", w=world).data()

    def norm_cid(v):
        return v.replace(f":{world}:", ":w:") if isinstance(v, str) else v

    nrows = sorted(
        [{k: norm_cid(v) for k, v in r.items()} for r in nodes],
        key=lambda r: r["cid"])
    erows = sorted(
        [{k: (norm_cid(v) if k in ("a", "b") else v) for k, v in r.items()} for r in edges],
        key=lambda r: (r["a"], r["t"], r["b"]))
    return nrows, erows


def test_batch_rows_do_not_change_the_final_graph():
    """バッチ行数=1（毎行 UNWIND）とデフォルト（少数バッチ）で、最終的な Neo4j 上のノード/エッジ
    プロパティ集合が完全一致する（重複エッジ＝同一 src/dst/type の上書き順序込み）。"""
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world

    nodes, edges, flags = world_graph.build_world(FIXTURE, "wtmp")
    assert len(nodes) >= 3 and len(edges) >= 3            # 複数バッチになる規模であることの前提

    env = world_neo4j._env()
    drv = _driver()
    orig_batch_rows = world_neo4j._NEO4J_BATCH_ROWS
    try:
        na, ea = _retarget(nodes, edges, WORLD_A)
        world_neo4j.load_world(na, ea, WORLD_A, env["uri"], env["user"], env["pw"])
        register_test_world(WORLD_A)

        nb, eb = _retarget(nodes, edges, WORLD_B)
        world_neo4j._NEO4J_BATCH_ROWS = 1                  # 意図的に最小バッチ（毎行 UNWIND）へ
        try:
            world_neo4j.load_world(nb, eb, WORLD_B, env["uri"], env["user"], env["pw"])
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


def test_mid_batch_failure_leaves_no_partial_graph():
    """バッチ送信の途中で例外が起きても、実 Neo4j の managed transaction がロールバックし、
    world は「空のまま」——旧グラフでも新グラフでもない半端な状態を作らない。これが GRAPH-MEM で
    複数 tx＋完了マーカー方式ではなく単一 write tx のバッチ送信を選んだ根拠そのもの
    （`world_neo4j.load_world` docstring 参照）。"""
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world

    nodes, edges, flags = world_graph.build_world(FIXTURE, "wtmp")
    ns, _es = _retarget(nodes, edges, WORLD_FAIL)
    assert len(ns) >= 3                                    # 3件目の投入前に落とせる規模であることの前提

    orig_batch_rows = world_neo4j._NEO4J_BATCH_ROWS
    orig_row_fn = world_neo4j._node_row
    calls = {"n": 0}

    def boom(n):
        calls["n"] += 1
        if calls["n"] == 3:
            raise RuntimeError("injected mid-batch failure (GRAPH-MEM test)")
        return orig_row_fn(n)

    world_neo4j._NEO4J_BATCH_ROWS = 1                      # 毎ノード1バッチ＝3件目で確実に落とす
    world_neo4j._node_row = boom
    env = world_neo4j._env()
    drv = _driver()
    register_test_world(WORLD_FAIL)                        # 失敗しても session teardown が後始末する
    try:
        with pytest.raises(RuntimeError, match="injected mid-batch failure"):
            world_neo4j.load_world(ns, [], WORLD_FAIL, env["uri"], env["user"], env["pw"])
        with drv.session() as s:
            count = s.run(
                "MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c", w=WORLD_FAIL).single()["c"]
        assert count == 0                                  # 部分反映が残っていない（ロールバック済み）
    finally:
        world_neo4j._NEO4J_BATCH_ROWS = orig_batch_rows
        world_neo4j._node_row = orig_row_fn
        _cleanup(drv, WORLD_FAIL)
        drv.close()
