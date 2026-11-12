"""グラフの入れ替えが、入れ替えの行を確かめ、取り込みの番号と設定の値をメタに書くこと（要 Postgres・Neo4j）。"""
from __future__ import annotations

import uuid

import pytest
from tests._replace_row import row_for

from sherpa.ingest import world_neo4j
from sherpa.store import db, replace_rows

NODES = [{"label": "Module", "cid": "Module:{w}:a", "name": "A", "path": "a.cbl", "top": "t", "phase": "p", "cat": "c"}]


def _driver():
    from neo4j import GraphDatabase
    env = world_neo4j._env()
    return GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))


def _load(world, row, cid_suffix="a"):
    nodes = [{**NODES[0], "cid": f"Module:{world}:{cid_suffix}"}]
    env = world_neo4j._env()
    return world_neo4j.load_world(nodes, [], world, env["uri"], env["user"], env["pw"], row=row)


def _meta(drv, world):
    with drv.session() as s:
        return s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.ingest_no AS no, m.settings_sig AS sig, "
                     "m.active_generation AS gen", w=world).single()


def _generations(drv, world) -> set:
    with drv.session() as s:
        return {r["g"] for r in s.run("MATCH (n:Entity {world_id:$w}) RETURN DISTINCT n.graph_generation AS g", w=world)}


@pytest.fixture
def world():
    wid = f"graph_swap_{uuid.uuid4().hex[:8]}"
    drv = _driver()
    yield wid, drv
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
        s.run("MATCH (m:SherpaMeta {world_id:$w}) DELETE m", w=wid)
    drv.close()
    with db._connect() as c:
        c.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world=%s", (db._KB_ID, wid))


def test_swap_writes_the_row_number_and_settings_sig_and_marks_the_item(world):
    wid, drv = world
    row = row_for(wid)
    _load(wid, row)

    meta = _meta(drv, wid)
    assert meta["no"] == row.ingest_no and meta["sig"] == replace_rows.settings_sig(row, "graph")
    cur = replace_rows.read_row(row.id)
    assert cur.items["graph"] == replace_rows.ITEM_CLEANED and "graph" not in cur.old_things
    assert replace_rows.settings_sig(row, "graph")
    with drv.session() as s:
        assert world_neo4j.read_ingest_no(s, wid) == row.ingest_no
        assert world_neo4j.read_ingest_no(s, wid + "_none") == 0


def test_second_swap_replaces_the_number_and_leaves_one_generation(world):
    wid, drv = world
    first = row_for(wid)
    _load(wid, first)
    second = row_for(wid)
    _load(wid, second, "b")

    assert _meta(drv, wid)["no"] == second.ingest_no > first.ingest_no
    assert len(_generations(drv, wid)) == 1


def test_a_row_of_another_world_is_refused_and_the_old_generation_stays_active(world):
    wid, drv = world
    _load(wid, row_for(wid))
    before = _meta(drv, wid)
    with pytest.raises(ValueError):
        _load(wid, row_for(wid + "_other"), "b")
    assert _meta(drv, wid)["gen"] == before["gen"] and _meta(drv, wid)["no"] == before["no"]


def test_a_row_that_lost_its_lock_keeps_the_old_generation(world):
    wid, drv = world
    first = row_for(wid)
    _load(wid, first)
    before = _meta(drv, wid)
    lost = row_for(wid)
    with db._connect() as c:
        c.execute("UPDATE ingest_replacements SET lock_id=lock_id+1 WHERE id=%s", (lost.id,))

    with pytest.raises(replace_rows.RowLockLost):
        _load(wid, lost, "b")

    after = _meta(drv, wid)
    assert after["gen"] == before["gen"] and after["no"] == first.ingest_no
    assert replace_rows.read_row(lost.id).items == {}


def test_delete_world_marks_the_item_and_clears_the_graph(world):
    wid, drv = world
    _load(wid, row_for(wid))
    drow = row_for(wid, kind="delete")
    env = world_neo4j._env()
    world_neo4j.delete_world(wid, env["uri"], env["user"], env["pw"], row=drow)

    assert _meta(drv, wid) is None and _generations(drv, wid) == set()
    assert replace_rows.read_row(drow.id).items["graph"] == replace_rows.ITEM_CLEANED
