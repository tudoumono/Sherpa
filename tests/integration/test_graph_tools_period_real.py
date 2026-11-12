"""期のある資料フォルダ（fixtures/corpus/period-proj）を実 Neo4j に取り込み、道具の結果に確かさ（`confirm`・`assumption`）が出ることを確かめる（要 Neo4j・`make up`）。"""
from __future__ import annotations

import pathlib

import pytest

from sherpa import tool_dispatch
from sherpa.ingest import world_graph, world_neo4j
from _world_registry import register_test_world
from tests._replace_row import offline_row

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "corpus" / "period-proj"
WORLD = "period_proj_gt_test"      # 使い捨て（テスト後に削除）
P = "注文管理/改修"


def _cid(path: str, name: str) -> str:
    return f"module:{WORLD}:{path}#{name}"


@pytest.fixture(scope="module")
def loaded():
    from neo4j import GraphDatabase
    nodes, edges, _flags = world_graph.build_world(FIXTURE, WORLD)
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"], row=offline_row(WORLD))
    register_test_world(WORLD)
    drv = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        yield drv
    finally:
        with drv.session() as s:
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD)
        drv.close()


def _run(name, args):
    return tool_dispatch.run_tool(name, args, WORLD, None)[0]


def _impact(path, name, **args):
    return {i["name"]: i for i in _run("graph_impact", {"canonical_id": _cid(path, name), **args})["impact"]}


def test_impact_marks_conditional_and_confirmed_routes(loaded):
    util = _impact(f"{P}/1期/lib/Util.java", "Util")["Settle"]
    assert util["confirm"] == "conditional" and util["assumption"] == ["parent_by_order"]
    assert util["route"][0]["confirm"] == "conditional"
    assert util["evidence"][0]["assumption"] == ["parent_by_order"]
    ledger = _impact(f"{P}/1期/src/app/Ledger.java", "Ledger")["Settle"]
    assert ledger["assumption"] == ["parent_by_order", "variance"]
    slot = _impact(f"{P}/3期/src/app/Slot.java", "Slot")["Order"]
    assert slot["confirm"] == "confirmed" and "assumption" not in slot


def test_light_list_form_keeps_confirm(loaded):
    item = _impact(f"{P}/2期/ソース/app/Fee.java", "Fee", evidence_limit=0)["Report"]
    assert item["confirm"] == "conditional" and item["assumption"] == ["variance"]


def test_resolve_lists_period_of_candidates(loaded):
    res = _run("graph_resolve", {"name": "Fee"})
    assert {c["path"]: c["period"] for c in res["candidates"]} == {
        f"{P}/1期/src/app/Fee.java": "1期", f"{P}/2期/ソース/app/Fee.java": "2期"}
