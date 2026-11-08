"""除外と複数の資料に属する Dataset・影響たどりの行数の天井（実 Neo4j）。名前は架空。"""
from __future__ import annotations

import json
import os

import pytest

from sherpa import lens_service
from sherpa import path_rules as PR
from sherpa.ingest import world_neo4j
from _world_registry import register_test_world

WORLD = "prrules_neo4j_rv4"


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


def _cid(path, name, kind="module"):
    return f"{kind}:{WORLD}:{path}#{name}"


def _load(nodes, edges):
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)


def _mod(name, path):
    return {"cid": _cid(path, name), "label": "Module", "name": name, "top_scope": path.split("/")[0], "path": path,
            "status": "active"}


def _edge(a, b, doc):
    return {"type": "INVOKES", "src": a["cid"], "dst": b["cid"], "doc": doc, "line": 1, "status": "active"}


def test_dataset_with_a_visible_membership_stays_searchable(session, monkeypatch):
    """複数の資料に属する Dataset は、見える所属が 1 件でもあれば探せる（全部が除外のときだけ外す）。"""
    both = {"cid": f"dataset:{WORLD}:both", "label": "Dataset", "name": "DS_BOTH", "top_scope": "ZA", "path": None,
            "status": "active", "membership_paths": ["ZAold/a.jcl", "ZBnew/b.jcl"]}
    gone = {"cid": f"dataset:{WORLD}:gone", "label": "Dataset", "name": "DS_GONE", "top_scope": "ZA", "path": None,
            "status": "active", "membership_paths": ["ZAold/a.jcl", "ZAold/c.jcl"]}
    _load([both, gone], [])
    _exclude(monkeypatch, "ZAold")
    names = [c["name"] for c in world_neo4j.list_world_candidates(session, "DS_", WORLD, None)]
    assert names == ["DS_BOTH"]
    got = [c["path"] for c in world_neo4j.list_world_candidates(session, "DS_BOTH", WORLD, None)]
    assert got == ["ZBnew/b.jcl"]                                           # 代表のパスは除外でない所属
    anchors = lens_service.resolve_anchor(session, "DS_BOTH を調べる", WORLD, None)
    assert [n for _c, n in anchors] == ["DS_BOTH"]                          # 起点としても探せる


def test_impact_returns_visible_targets_even_when_the_excluded_branch_hits_the_row_cap(session, monkeypatch):
    """除外側の枝だけで緊急の上限に達しても、見える影響先を返す（除外は上限の前）。"""
    root = _mod("Core", "ZZnew/Core.java")
    vis = _mod("Visible", "ZZnew/Visible.java")
    hidden = [_mod(f"Hid{i:03d}", f"ZAold/Hid{i:03d}.java") for i in range(40)]
    nodes = [root, vis, *hidden]
    edges = [_edge(vis, root, "ZZnew/Visible.java")] + [_edge(h, root, h["path"]) for h in hidden]
    _load(nodes, edges)
    base = world_neo4j.run_world_impact(session, "Core", WORLD)
    assert len(base["items"]) == 41                                          # 設定なしは従来どおり
    monkeypatch.setattr(world_neo4j, "_NEO4J_MAX_ROWS", 20)                  # 除外側の枝だけで緊急の上限（20 行）を超える
    _exclude(monkeypatch, "ZAold")
    got = world_neo4j.run_world_impact(session, "Core", WORLD)
    assert [i["name"] for i in got["items"]] == ["Visible"]


def test_graph_impact_start_for_a_dataset_with_a_visible_membership(session, monkeypatch):
    """graph_resolve で見える複数所属の Dataset は、graph_impact でも起点になる（代表のパスは除外でない所属）。"""
    from sherpa import graph_tools
    both = {"cid": f"dataset:{WORLD}:both2", "label": "Dataset", "name": "DS_BOTH2", "top_scope": "ZA", "path": None,
            "status": "active", "membership_paths": ["ZAold/a.jcl", "ZBnew/b.jcl"]}
    _load([both], [])
    _exclude(monkeypatch, "ZAold")
    starts = world_neo4j.get_world_entities(session, [both["cid"]], WORLD)
    assert [s["path"] for s in starts] == ["ZBnew/b.jcl"]
    res = graph_tools.run("graph_impact", {"canonical_id": both["cid"]}, WORLD, None)
    assert res.get("start", {}).get("path") == "ZBnew/b.jcl" and "error" not in res


def test_neighbors_keep_an_aggregated_edge_with_a_visible_source(session, monkeypatch):
    """同じ辺の複数の資料を集約した辺は、代表の資料が除外でも、見える根拠が残れば近傍に出る（代表は見える根拠から選び直す）。"""
    main, keep, gone = _mod("Main", "ZZnew/Main.java"), _mod("Keep", "ZZnew/Keep.java"), _mod("Gone", "ZZnew/Gone.java")

    def src(doc, line):
        return {"via": "call", "doc_id": doc, "file": doc, "line": line, "rule": "same_package", "evidence_text": f"T:{doc}"}

    e_keep = {**_edge(main, keep, "ZAold/a.java"), "line": 3,
              "sources": [src("ZAold/a.java", 3), src("ZZnew/b.java", 9)]}
    e_gone = {**_edge(main, gone, "ZAold/a.java"), "sources": [src("ZAold/a.java", 4)]}
    _load([main, keep, gone], [e_keep, e_gone])
    base = {c["name"] for c in lens_service.neighbor_cards_graph_only(WORLD, "Main")}
    assert {"Keep", "Gone"} <= base                                          # 設定なしは従来どおり
    _exclude(monkeypatch, "ZAold")
    cards = lens_service.neighbor_cards_graph_only(WORLD, "Main")
    names = {c["name"] for c in cards}
    assert "Keep" in names and "Gone" not in names
    edge = next(c for c in cards if c["name"] == "Keep")["evidence"]["edges"][0]
    assert edge["doc"] == "ZZnew/b.java" and edge["line"] == 9
    assert [s["doc_id"] for s in edge["sources"]] == ["ZZnew/b.java"] and "ZAold" not in json.dumps(cards, ensure_ascii=False)


# ---- 上限・曖昧さの判定の前に除外が入る場所（Cypher） ----

def test_related_documents_limit_counts_only_visible_documents(session, monkeypatch):
    tgt = _mod("Tgt", "ZZnew/Tgt.java")
    docs = [{"cid": f"document:{WORLD}:ZAold/d{i}.md", "label": "Document", "name": f"d{i}.md", "top_scope": "ZAold",
             "path": f"ZAold/d{i}.md", "status": "active"} for i in range(5)]
    vis = {"cid": f"document:{WORLD}:ZZnew/dv.md", "label": "Document", "name": "dv.md", "top_scope": "ZZnew",
           "path": "ZZnew/dv.md", "status": "active"}
    edges = [{"type": "DOCUMENTS", "src": d["cid"], "dst": tgt["cid"], "doc": d["path"], "line": 0, "status": "active",
              "via": "mention"} for d in [*docs, vis]]
    _load([tgt, *docs, vis], edges)
    assert len(world_neo4j.world_related_documents(session, [tgt["cid"]], WORLD, None, limit=2)) == 2   # 設定なしは従来どおり
    _exclude(monkeypatch, "ZAold")
    got = world_neo4j.world_related_documents(session, [tgt["cid"]], WORLD, None, limit=2)
    assert [d["path"] for d in got] == ["ZZnew/dv.md"]                       # 除外が先頭の 2 件を占めても、見える資料が返る


def test_same_name_in_an_excluded_place_is_not_ambiguous(session, monkeypatch):
    from sherpa import impact_service
    a, b = _mod("Same", "ZAold/Same.java"), _mod("Same", "ZZnew/Same.java")
    _load([a, b], [])
    assert len(world_neo4j.resolve_world_entity(session, "Same", WORLD)) == 2
    _exclude(monkeypatch, "ZAold")
    assert [r["path"] for r in world_neo4j.resolve_world_entity(session, "Same", WORLD)] == ["ZZnew/Same.java"]
    # 推定の影響: 除外側の同名が「曖昧」の判定に数えられない（max_items の前）
    monkeypatch.setattr("sherpa.grep_tool.grep_search", lambda *a, **k: [
        {"doc_id": "ZYother/doc.md", "line": 1, "text": "Same を呼び出す処理"}])
    got = impact_service.presumed_impact(session, "x", WORLD)
    assert [i["path"] for i in got] == ["ZZnew/Same.java"]
    monkeypatch.delenv(PR.ENV_KEY)
    assert impact_service.presumed_impact(session, "x", WORLD) == []        # 設定なしは従来どおり（曖昧で繋がない）


def test_impact_does_not_walk_edges_whose_only_evidence_is_excluded(session, monkeypatch):
    """根拠が除外の資料だけにある辺は、graph_impact でも辿らない（見える根拠が残る辺は辿る）。"""
    core, hid, vis = _mod("Core", "ZZnew/Core.java"), _mod("OnlyHidden", "ZZnew/OnlyHidden.java"), _mod("HasVisible", "ZZnew/HasVisible.java")

    def src(doc, line):
        return {"via": "call", "doc_id": doc, "file": doc, "line": line, "rule": "same_package"}
    e_hid = {**_edge(hid, core, "ZAold/a.java"), "sources": [src("ZAold/a.java", 1)]}
    e_vis = {**_edge(vis, core, "ZAold/a.java"), "sources": [src("ZAold/a.java", 1), src("ZZnew/b.java", 2)]}
    _load([core, hid, vis], [e_hid, e_vis])
    base = {i["name"] for i in world_neo4j.run_world_impact(session, "Core", WORLD)["items"]}
    assert base == {"OnlyHidden", "HasVisible"}                                # 設定なしは従来どおり
    _exclude(monkeypatch, "ZAold")
    got = {i["name"] for i in world_neo4j.run_world_impact(session, "Core", WORLD)["items"]}
    assert got == {"HasVisible"}


def test_resolve_world_entity_for_a_dataset_with_a_visible_membership(session, monkeypatch):
    """複数の資料に属する Dataset は、代表パスが除外側でも、見える所属だけの範囲でも、起点として解決できる（実行は Neo4j が空いているとき）。"""
    ds = {"cid": f"dataset:{WORLD}:rwe", "label": "Dataset", "name": "DS_RWE", "top_scope": "ZB", "path": None,
          "status": "active", "membership_paths": ["ZAold/a.jcl", "ZBnew/b.jcl"]}
    _load([ds], [])
    _exclude(monkeypatch, "ZAold")
    got = world_neo4j.resolve_world_entity(session, "DS_RWE", WORLD)
    assert [r["path"] for r in got] == ["ZBnew/b.jcl"]
    got = world_neo4j.resolve_world_entity(session, "DS_RWE", WORLD, scope_prefixes=["ZBnew"])
    assert [r["path"] for r in got] == ["ZBnew/b.jcl"]


def test_dataset_memberships_must_satisfy_scope_and_visibility_together(session, monkeypatch):
    """複数の資料に属する Dataset は、範囲の中と除外でないを別々の所属で満たしても候補に入れない（同じ所属が両方を満たすものだけ）。"""
    split = [{"cid": f"dataset:{WORLD}:split{i}", "label": "Dataset", "name": f"DS_SPLIT{i}", "top_scope": "ZA", "path": None,
              "status": "active", "membership_paths": ["ZAold/a.jcl", "ZBnew/b.jcl"]} for i in range(6)]
    ok = {"cid": f"dataset:{WORLD}:ok", "label": "Dataset", "name": "DS_SPLITOK", "top_scope": "ZC", "path": None,
          "status": "active", "membership_paths": ["ZAold/c.jcl", "ZCok/c.jcl"]}
    _load([*split, ok], [])
    _exclude(monkeypatch, "ZAold")
    got = world_neo4j.list_world_candidates(session, "DS_SPLIT", WORLD, ["ZAold", "ZCok"], limit=3)
    assert [c["name"] for c in got] == ["DS_SPLITOK"] and [c["path"] for c in got] == ["ZCok/c.jcl"]
    assert world_neo4j.resolve_world_entity(session, "DS_SPLIT0", WORLD, scope_prefixes=["ZAold"]) == []
    assert world_neo4j.get_world_entities(session, [split[0]["cid"]], WORLD, ["ZAold"]) == []
