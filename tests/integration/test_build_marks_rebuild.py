"""ビルドの定義を読む場所の印を変えた後の作り直し: グラフ・取り込みの台帳・名前の台帳が同じ番号にそろい、途中で止めても食い違わない
（本物の worker を子プロセスで動かす・要 Postgres＋Neo4j＋ES）。製品の関数は差し替えない。
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile
import time

import pytest
from _ingest_crash import run_worker_once, start_worker_once
from _world_setup import driver

from sherpa import build_marks, store, worlds
from sherpa.ingest import background
from sherpa.store import db, name_ledger, replace_rows

W = "test_build_marks_rebuild"
MAKEFILE = "app/src/Makefile"
FILES = {
    "app/job/night.jcl": "//NIGHT JOB\n//STEP1 EXEC PGM=CALC\n",
    MAKEFILE: "calc: main.o util.o\n\tcc -o calc main.o util.o\n",
    "app/src/main.c": "int main(void){return 0;}\n",
    "app/src/util.c": "int util(void){return 0;}\n",
}


def _refresh() -> int:
    run_id = background.submit(W, "refresh", "{}")[0]
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    run = next(r for r in store.list_ingest_runs(W) if r["id"] == run_id)
    assert run["status"] in ("auto_published", "auto_published_with_flags")
    return run_id


def _graph() -> dict:
    drv = driver()
    try:
        with drv.session() as s:
            meta = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.ingest_no AS no, m.build_marks_sig AS sig", w=W).single()
            gen = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.active_generation AS g", w=W).single()["g"]
            exe = s.run("MATCH (n:Entity {world_id:$w, label:'Executable'}) WHERE n.graph_generation=$g RETURN count(n) AS c",
                        w=W, g=gen).single()["c"]
            exe = exe or s.run("MATCH (n:Executable {world_id:$w}) WHERE n.graph_generation=$g RETURN count(n) AS c", w=W, g=gen).single()["c"]
    finally:
        drv.close()
    return {"no": meta["no"], "sig": meta["sig"] or "", "executables": exe}


def _ledgers() -> dict:
    with db._connect() as c:
        docs = c.execute("SELECT DISTINCT ingest_no FROM documents WHERE kb_id=%s AND version=%s", (db._KB_ID, W)).fetchall()
        names = c.execute("SELECT DISTINCT ingest_no FROM name_ledger WHERE world=%s", (W,)).fetchall()
    return {"docs": sorted(r["ingest_no"] for r in docs), "names": sorted(r["ingest_no"] for r in names),
            "build_rows": sorted(r["rel_path"] for r in name_ledger.read_rows(W) if r["source"] == "build")}


def _rows() -> list:
    with db._connect() as c:
        return c.execute("SELECT id, kind, ingest_no, items FROM ingest_replacements WHERE kb_id=%s AND world=%s",
                         (db._KB_ID, W)).fetchall()


@pytest.fixture
def world():
    root = tempfile.mkdtemp()
    for rel, body in FILES.items():
        p = pathlib.Path(root) / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")
    try:
        worlds.register(W, str(pathlib.Path(root).resolve()))
        yield W
    finally:
        try:
            worlds.delete(W)
        except Exception:
            pass
        store.delete_world_row(W)
        build_marks.save_world("test", W, {})
        drv = driver()
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=W)
        drv.close()
        shutil.rmtree(root, ignore_errors=True)


def test_mark_change_rebuilds_graph_and_both_ledgers_to_the_same_number(world):
    """置き方で読んだ実行名が、「読む場所にしない」で消える。グラフ・目録・名前の台帳は同じ番号にそろい、行は残らない。"""
    _refresh()
    before = _graph()
    assert before["executables"] == 1 and before["sig"]
    assert _ledgers()["build_rows"] == ["app/src/main.c", "app/src/util.c"]

    build_marks.save_world("test", W, {MAKEFILE: "skip"})
    _refresh()
    after = _graph()
    led = _ledgers()
    assert after["executables"] == 0 and after["sig"] == ""
    assert led["build_rows"] == []
    assert after["no"] == led["docs"][-1] and led["names"] in ([], led["docs"])
    assert _rows() == []


def test_unchanged_marks_do_not_rebuild(world):
    """印を変えても読むファイルが変わらないなら、作り直さない（番号が進まない）。"""
    _refresh()
    before = _graph()
    build_marks.save_world("test", W, {"app/job": "skip"})
    _refresh()
    assert _graph()["no"] == before["no"]


def test_kill_just_before_the_graph_switch_then_resume_keeps_both_ledgers_consistent(world):
    """印の変更後の作り直しで、グラフの切り替えの直前に止める → 旧グラフと旧台帳が見え続け、再開で同じ番号にそろう。"""
    _refresh()
    build_marks.save_world("test", W, {MAKEFILE: "skip"})
    old_ledgers = _ledgers()
    old_graph = _graph()
    old_shown = replace_rows.shown_ingest_no(W)
    drv = driver()
    session = drv.session()
    tx = session.begin_transaction()
    proc = None
    try:
        tx.run("MATCH (m:SherpaMeta {world_id:$w}) SET m.stop_hold = true", w=W).consume()
        background.submit(W, "refresh", "{}")
        proc = start_worker_once()
        deadline = time.monotonic() + 120
        while True:
            with drv.session() as s:
                blocked = s.run("SHOW TRANSACTIONS YIELD status WHERE status STARTS WITH 'Blocked' RETURN count(*) AS c").single()["c"]
            if blocked:
                break
            assert proc.poll() is None, proc.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "グラフの切り替えに達しませんでした"
            time.sleep(0.2)
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        tx.rollback()
        tx.close()
        session.close()
        drv.close()

    now = _ledgers()
    assert replace_rows.shown_ingest_no(W) == old_shown   # 見せている番号は変わらない（下書きが 1 つ増えるだけ）
    assert set(old_ledgers["docs"]) <= set(now["docs"]) and len(now["docs"]) <= 2
    assert now["build_rows"] == old_ledgers["build_rows"]
    assert _graph()["no"] == old_graph["no"]
    assert len(_rows()) == 1
    store.downgrade_orphaned_extracting_runs(W)
    _refresh()
    after = _graph()
    led = _ledgers()
    assert _rows() == [] and after["executables"] == 0 and led["build_rows"] == []
    assert after["no"] == led["docs"][-1]


def test_mark_change_together_with_a_count_mismatch_still_rebuilds_both_ledgers(world):
    """印の変更に件数の不一致が重なっても、グラフだけに新しい印が当たらず、名前の台帳も同じ番号で作り直される。"""
    _refresh()
    assert _ledgers()["build_rows"]
    build_marks.save_world("test", W, {MAKEFILE: "skip"})
    drv = driver()
    try:
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) WHERE n.path = 'app/src/util.c' DETACH DELETE n", w=W)
    finally:
        drv.close()
    _refresh()
    led = _ledgers()
    after = _graph()
    assert led["build_rows"] == [] and after["executables"] == 0 and after["sig"] == ""
    assert after["no"] == led["docs"][-1]


def test_mark_change_with_missing_graph_meta_still_rebuilds_both_ledgers(world):
    """グラフのメタが欠けたまま印が変わっても、台帳と名前の台帳は作り直され、新しい印の値はグラフと台帳が一緒に持つ。"""
    _refresh()
    assert _ledgers()["build_rows"]
    build_marks.save_world("test", W, {MAKEFILE: "skip"})
    drv = driver()
    try:
        with drv.session() as s:
            s.run("MATCH (m:SherpaMeta {world_id:$w}) DETACH DELETE m", w=W)
    finally:
        drv.close()
    _refresh()
    led = _ledgers()
    after = _graph()
    assert led["build_rows"] == [] and after["executables"] == 0
    assert after["no"] == led["docs"][-1]


def test_repeated_mark_changes_keep_at_most_two_ledger_numbers(world):
    """印だけを変える更新を繰り返しても、目録と名前の台帳の行は多くても 2 回分（古い番号は消える）。"""
    _refresh()
    for marks in ({MAKEFILE: "skip"}, {}, {MAKEFILE: "skip"}):
        build_marks.save_world("test", W, marks)
        _refresh()
        led = _ledgers()
        assert len(led["docs"]) <= 2 and len(led["names"]) <= 2
    assert _rows() == []
    assert _graph()["no"] == led["docs"][-1]
