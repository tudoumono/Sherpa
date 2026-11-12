"""ソースの中の SQL（`.pc` の EXEC SQL・文字列の SQL）が、定義の無い表を含めて Neo4j のグラフと名前の台帳まで届く（要 Postgres＋Neo4j＋ES）。"""
from __future__ import annotations

import pathlib
import shutil
import tempfile

import pytest
from _ingest_crash import run_worker_once
from _world_setup import driver

from sherpa import store, worlds
from sherpa.ingest import background

W = "test_embedded_sql_graph"
PC = ('int load(void){\n  EXEC SQL SELECT NAME INTO :n FROM CUSTOMER WHERE ID = :i;\n'
      '  const char *q = "SELECT * FROM ORDERS o";\n  return 0;\n}\n')


@pytest.fixture
def world():
    root = tempfile.mkdtemp()
    p = pathlib.Path(root) / "sysA" / "src" / "load.pc"
    p.parent.mkdir(parents=True)
    p.write_text(PC, encoding="utf-8")
    try:
        worlds.register(W, str(pathlib.Path(root).resolve()))
        yield W
    finally:
        try:
            worlds.delete(W)
        except Exception:
            pass
        store.delete_world_row(W)
        drv = driver()
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=W)
        drv.close()
        shutil.rmtree(root, ignore_errors=True)


def test_pc_sql_reaches_the_graph(world):
    run_id = background.submit(W, "refresh", "{}")[0]
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert next(r for r in store.list_ingest_runs(W) if r["id"] == run_id)["status"] in ("auto_published", "auto_published_with_flags")
    drv = driver()
    try:
        with drv.session() as s:
            rows = s.run("MATCH (a:Entity {world_id:$w})-[r:ACCESSES]->(t:Table) RETURN a.path AS src, t.name AS tbl, t.path AS tpath, "
                         "t.value AS value, r.via AS via ORDER BY tbl", w=W).data()
    finally:
        drv.close()
    assert [(r["src"], r["tbl"], r["tpath"], r["via"]) for r in rows] == [
        ("sysA/src/load.pc", "CUSTOMER", "sysA", "exec_sql"), ("sysA/src/load.pc", "ORDERS", "sysA", "string_sql")]
    assert all(r["value"] == "undefined" for r in rows)


def test_undefined_table_is_found_in_a_sub_folder_scope_with_a_real_source_path(world):
    from sherpa.ingest import world_neo4j
    run_id = background.submit(W, "refresh", "{}")[0]
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert next(r for r in store.list_ingest_runs(W) if r["id"] == run_id)["status"] in ("auto_published", "auto_published_with_flags")
    drv = driver()
    try:
        with drv.session() as s:
            inside = world_neo4j.resolve_world_entity(s, "CUSTOMER", W, scope_prefixes=["sysA/src"])
            outside = world_neo4j.resolve_world_entity(s, "CUSTOMER", W, scope_prefixes=["sysA/other"])
            listed = world_neo4j.get_world_entities(s, [r["canonical_id"] for r in inside], W, scope_prefixes=["sysA/src"])
    finally:
        drv.close()
    assert [r["path"] for r in inside] == ["sysA/src/load.pc"] and outside == []
    assert [r["path"] for r in listed] == ["sysA/src/load.pc"]
