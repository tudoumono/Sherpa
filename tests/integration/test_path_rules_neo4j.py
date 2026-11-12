"""管理者の除外を、実 Neo4j の問い合わせで確かめる（要 Neo4j）。起点・経路の全ノード・未解決の参照・`graph_resolve` の上限・内訳。名前は架空。"""
from __future__ import annotations

import json
import os

import pytest

from sherpa import graph_tools, lens_service
from sherpa import path_rules as PR
from sherpa.ingest import world_graph, world_neo4j
from _world_registry import register_test_world
from tests._replace_row import offline_row

WORLD = "prrules_neo4j_test"          # 使い捨て（テスト後に削除）


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


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)
    monkeypatch.delenv(PR.ENV_KEY, raising=False)


def _exclude(monkeypatch, *paths):
    monkeypatch.setenv(PR.ENV_KEY, json.dumps(
        {WORLD: [{"path": p, "kind": "exclude", "outside_scope": False} for p in paths]}))


def _load(tmp_path, files: dict):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    nodes, edges, _ = world_graph.build_world(tmp_path, WORLD)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"], row=offline_row(WORLD))
    register_test_world(WORLD)


def _load_graph(nodes_spec, edges_spec):
    """手で組んだノード（名前・パス）と辺（INVOKES）を入れる。"""
    def cid(path, name):
        return f"module:{WORLD}:{path}#{name}"
    nodes = [{"cid": cid(p, n), "label": "Module", "name": n, "top_scope": p.split("/")[0], "path": p, "status": "active"}
             for n, p in nodes_spec]
    path_of = dict(nodes_spec)
    edges = [{"type": "INVOKES", "src": cid(path_of[a], a), "dst": cid(path_of[b], b), "doc": path_of[a], "line": 1,
              "status": "active"} for a, b in edges_spec]
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"], row=offline_row(WORLD))
    register_test_world(WORLD)


_CHAIN_NODES = [("Main", "ZZnew/Main.java"), ("Mid", "ZZnew/Mid.java"), ("Old", "ZZold/Old.java"), ("Far", "ZZnew/Far.java")]
_CHAIN_EDGES = [("Main", "Old"), ("Main", "Mid"), ("Old", "Far")]


def test_neighbors_skip_excluded_nodes_and_paths_through_them(session, tmp_path, monkeypatch):
    _load_graph(_CHAIN_NODES, _CHAIN_EDGES)
    base = {c["name"] for c in lens_service.neighbor_cards_graph_only(WORLD, "Main")}
    assert {"Old", "Mid", "Far"} <= base                                    # 設定なしは従来どおり
    _exclude(monkeypatch, "ZZold")
    names = {c["name"] for c in lens_service.neighbor_cards_graph_only(WORLD, "Main")}
    assert "Mid" in names and "Old" not in names and "Far" not in names   # 除外のノードと、そこを経由する先は出ない
    assert lens_service.neighbor_cards_graph_only(WORLD, "Old") == []      # 除外のノードは起点にもならない


def test_unresolved_is_excluded_before_the_return_cap(session, tmp_path, monkeypatch):
    files = {f"ZA/F{i:03d}.java": "class F%03d {\n void m() { new Helper(); }\n}\n" % i for i in range(60)}
    files.update({"ZB/Helper.java": "class Helper {}\n", "ZC/Helper.java": "class Helper {}\n",
                  "ZZ/Main.java": "class Main {\n void m() { new Helper(); }\n}\n"})
    _load(tmp_path, files)
    _exclude(monkeypatch, "ZA")
    got = world_neo4j.read_unresolved(session, WORLD, ["Helper"])
    assert [i["path"] for i in got["items"]] == ["ZZ/Main.java"]            # 除外が先頭 50 件を占めても、見える参照が返る
    assert "ZA/" not in json.dumps(got)


def test_graph_resolve_limit_counts_only_visible_candidates(session, tmp_path, monkeypatch):
    files = {f"ZA/Cand{i:02d}.java": "class Cand%02d {}\n" % i for i in range(25)}
    files["ZZ/CandVisible.java"] = "class CandVisible {}\n"
    _load(tmp_path, files)
    _exclude(monkeypatch, "ZA")
    res = graph_tools.run("graph_resolve", {"name": "Cand", "limit": 20}, WORLD, None)
    assert [c["name"] for c in res["candidates"]] == ["CandVisible"] and res["count"] == 1


def test_too_broad_breakdown_hides_excluded_folders(session, tmp_path, monkeypatch):
    files = {f"ZAold/M{i}.java": "class Same {}\n" for i in range(3)}
    files.update({f"ZZnew/M{i}.java": "class Same {}\n" for i in range(3)})
    _load(tmp_path, files)
    _exclude(monkeypatch, "ZAold")
    cov = lens_service.Coverage()
    b = lens_service._anchor_breadth(session, WORLD, "n.name = $term", None, None, 2, cov, term="Same")
    assert "ZAold" not in json.dumps(b, ensure_ascii=False) and b["anchors"] is not None
