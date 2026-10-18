"""影響たどり（段ごとに広げる BFS）が実 Neo4j で期待どおりの集合・最短の代表経路・深さの申告を返す（要 Neo4j・`make up`）。

合流（層をまたぐ全結合）・循環・範囲外・deprecated を含む合成グラフを、メモリ上の BFS（正解）と突き合わせる。
"""
from __future__ import annotations

import os
from collections import deque

import pytest
from _world_registry import register_test_world

from sherpa.ingest import world_neo4j

WORLD = "impact_bfs_test_a"          # 使い捨て（テスト後に削除）
LAYERS, WIDTH = 6, 4


def _cid(name, path):
    return f"module:{WORLD}:{path}#{name}"


def _graph(extra_nodes=(), extra_edges=()):
    """層 0 の n0 が起点。層 k+1 の各ノードは層 k の全ノードに依存する（合流の多い全結合）。"""
    nodes, edges = [], []
    for k in range(LAYERS):
        for j in range(WIDTH):
            path = f"in/l{k}/n{j}.cbl"
            nodes.append({"cid": _cid(f"L{k}N{j}", path), "name": f"L{k}N{j}", "label": "Module",
                          "top_scope": "in", "path": path, "status": "active"})
    for k in range(LAYERS - 1):
        for a in range(WIDTH):
            for b in range(WIDTH):
                edges.append({"src": _cid(f"L{k + 1}N{a}", f"in/l{k + 1}/n{a}.cbl"),
                              "dst": _cid(f"L{k}N{b}", f"in/l{k}/n{b}.cbl"),
                              "type": "INVOKES", "doc": "in/d.md", "line": k + 1, "status": "active"})
    # 起点へ戻る循環
    edges.append({"src": _cid("L0N0", "in/l0/n0.cbl"), "dst": _cid("L1N0", "in/l1/n0.cbl"),
                  "type": "COPIES", "doc": "in/d.md", "line": 99, "status": "active"})
    return nodes + list(extra_nodes), edges + list(extra_edges)


START = _cid("L0N0", "in/l0/n0.cbl")


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
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD)
    finally:
        drv.close()


def _reference(edges, start, depth, keep=lambda cid: True):
    """メモリ上の BFS。start から逆向きに depth 段までの {cid: 最短の長さ}（start 自身は循環で戻るときだけ入る）。"""
    rev: dict[str, list[str]] = {}
    for e in edges:
        rev.setdefault(e["dst"], []).append(e["src"])
    dist = {start: 0}
    out: dict[str, int] = {}
    q = deque([start])
    while q:
        x = q.popleft()
        if dist[x] >= depth:
            continue
        for y in rev.get(x, ()):
            if not keep(y):
                continue
            if y not in dist:
                dist[y] = dist[x] + 1
                q.append(y)
                out[y] = dist[y]
            elif y == start and y not in out:
                out[y] = dist[x] + 1
    return out


@pytest.mark.parametrize("depth", [1, 2, 3, 5, 10])
def test_set_and_shortest_route_match_a_reference_bfs(session, depth):
    nodes, edges = _graph()
    _load(nodes, edges)
    info: dict = {}
    items = world_neo4j.world_impact(session, [START], WORLD, depth=depth, info=info, detail=True)
    ref = _reference(edges, START, depth)
    assert {i["canonical_id"]: len(i["trace_cids"]) - 1 for i in items} == ref
    deeper = _reference(edges, START, depth + 1)
    assert info["depth_truncated"] is (set(deeper) != set(ref))
    for i in items:                                  # 代表経路は実在する辺をたどって起点へ着く
        assert i["trace_cids"][0] == i["canonical_id"] and i["trace_cids"][-1] == START
        assert len(i["edges"]) == len(i["trace_cids"]) - 1


def test_out_of_scope_and_deprecated_are_not_waypoints(session):
    nodes, edges = _graph()
    for n in nodes:
        if n["name"] in ("L2N1", "L2N2"):
            n["status"] = "deprecated"
        if n["name"] in ("L1N3",):
            n["path"] = "out/l1/n3.cbl"
    _load(nodes, edges)
    by_cid = {n["cid"]: n for n in nodes}
    def keep(cid):
        return by_cid[cid]["status"] == "active" and by_cid[cid]["path"].startswith("in/")
    items = world_neo4j.world_impact(session, [START], WORLD, scope_prefixes=["in"], depth=10, detail=True)
    assert {i["canonical_id"]: len(i["trace_cids"]) - 1 for i in items} == _reference(edges, START, 10, keep)
    everything = world_neo4j.world_impact(session, [START], WORLD, depth=10, include_deprecated=True)
    assert len(everything) > len(items)
