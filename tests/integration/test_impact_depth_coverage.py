"""影響たどりの深さの上限で先が残るか（`coverage.depth.truncated`・要 Neo4j・`make up`）。

連鎖 C→B→A（逆向きに A の影響先）を深さ 2 でたどる合成グラフに、深さ 3 の先 D を条件違いで足す。
判定が実際の影響クエリ（`world_impact`）の結果と矛盾しないこと（`truncated:false` なら深さ+1 でも同じ結果）も確かめる。
"""
from __future__ import annotations

import os

import pytest

from sherpa.ingest import world_neo4j
from _world_registry import register_test_world

WORLD = "depth_cov_test_a"          # 使い捨て（テスト後に削除）
OTHER_WORLD = "depth_cov_test_b"
A = f"module:{WORLD}:in/a.cbl#A"


def _node(world, name, path, status="active"):
    return {"cid": f"module:{world}:{path}#{name}", "name": name, "label": "Module",
            "top_scope": "in", "path": path, "status": status}


def _edge(src, dst, etype="INVOKES"):
    return {"src": src, "dst": dst, "type": etype, "doc": "x.md", "line": 1, "status": "active"}


def _cid(name, path):
    return f"module:{WORLD}:{path}#{name}"


def _chain():
    nodes = [_node(WORLD, "A", "in/a.cbl"), _node(WORLD, "B", "in/b.cbl"), _node(WORLD, "C", "in/c.cbl")]
    edges = [_edge(_cid("B", "in/b.cbl"), A), _edge(_cid("C", "in/c.cbl"), _cid("B", "in/b.cbl"))]
    return nodes, edges


def _with_d(path="in/d.cbl", status="active", etype="INVOKES", edge_status="active"):
    nodes, edges = _chain()
    nodes.append(_node(WORLD, "D", path, status))
    e = _edge(_cid("D", path), _cid("C", "in/c.cbl"), etype)
    e["status"] = edge_status
    edges.append(e)
    return nodes, edges


def _load(nodes, edges):
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)


@pytest.fixture
def session():
    from neo4j import GraphDatabase
    drv = GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")))
    try:
        with drv.session() as s:
            yield s
            s.run("MATCH (n) WHERE n.world_id IN $ws AND (n:Entity OR n:SherpaMeta) DETACH DELETE n",
                  ws=[WORLD, OTHER_WORLD])
    finally:
        drv.close()


def _names(session, depth, **kw):
    return {i["name"] for i in world_neo4j.world_impact(session, [A], WORLD, depth=depth, **kw)}


def _coverage(session, depth=2, **kw):
    return world_neo4j.run_world_impact(session, "A", WORLD, depth=depth, **kw)["coverage"]


def test_chain_within_depth_is_complete(session):
    _load(*_chain())
    cov = _coverage(session)
    assert cov == {"complete": True, "limits": [], "omitted": 0, "depth": {"requested": 2, "truncated": False}}
    assert _names(session, 3) == _names(session, 2) == {"B", "C"}


def test_remaining_edge_beyond_depth_is_reported(session):
    _load(*_with_d())
    cov = _coverage(session)
    assert cov == {"complete": False, "limits": [{"kind": "depth", "stage": "impact"}], "omitted": None,
                   "depth": {"requested": 2, "truncated": True}}
    assert _names(session, 3) == {"B", "C", "D"} != _names(session, 2)


def test_beyond_depth_but_out_of_scope_is_not_a_remainder(session):
    _load(*_with_d(path="out/d.cbl"))
    cov = _coverage(session, scope_prefixes=["in"])
    assert cov["depth"]["truncated"] is False and cov["complete"] is True
    assert _names(session, 3, scope_prefixes=["in"]) == _names(session, 2, scope_prefixes=["in"])


def test_deprecated_remainder_follows_include_deprecated(session):
    _load(*_with_d(status="deprecated"))
    assert _coverage(session)["depth"]["truncated"] is False
    assert _names(session, 3) == _names(session, 2)
    cov = _coverage(session, include_deprecated=True)
    assert cov["depth"]["truncated"] is True and cov["limits"] == [{"kind": "depth", "stage": "impact"}]
    assert _names(session, 3, include_deprecated=True) != _names(session, 2, include_deprecated=True)


def test_remainder_in_another_world_is_not_a_remainder(session):
    _load(*_chain())
    env = world_neo4j._env()
    other = [_node(OTHER_WORLD, "D", "in/d.cbl")]
    world_neo4j.load_world(other, [], OTHER_WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(OTHER_WORLD)
    session.run("MATCH (d:Entity {canonical_id:$d}), (c:Entity {canonical_id:$c}) "
                "MERGE (d)-[r:INVOKES]->(c) SET r.world_id=$w, r.status='active'",
                d=f"module:{OTHER_WORLD}:in/d.cbl#D", c=_cid("C", "in/c.cbl"), w=WORLD)
    assert _coverage(session)["depth"]["truncated"] is False
    assert _names(session, 3) == _names(session, 2)


def test_mention_only_remainder_is_not_a_remainder(session):
    _load(*_with_d(etype="DOCUMENTS"))
    assert _coverage(session)["depth"]["truncated"] is False
    assert _names(session, 3) == _names(session, 2)


def test_cycle_back_to_reached_node_is_not_a_remainder(session):
    nodes, edges = _chain()
    edges.append(_edge(A, _cid("B", "in/b.cbl")))  # A→B（B→A と 2 周）
    _load(nodes, edges)
    assert _coverage(session)["depth"]["truncated"] is False
    assert _names(session, 3) == _names(session, 2)
