"""消す経路と、消し切る再開の確認（本物の worker を子プロセスで動かす・要 Postgres＋Neo4j＋ES）。土台は `test_ingest_stop_matrix` と共用する。"""
from __future__ import annotations

import shutil

import pytest
from _ingest_crash import run_worker_once
from _world_setup import driver
from test_ingest_stop_matrix import (  # noqa: F401  (registered_world は fixture)
    _STOP_LOCK, W, _enqueue_refresh, _rows, _run, _settle_orphan, _wait_stopped, registered_world)

from sherpa import store, worlds
from sherpa.ingest import background


# ---- 消す経路と、消し切る再開 -----------------------------------------------------------------------

def _submit_delete() -> int:
    run_id, _ = background.submit(W, "delete", "{}", {"uid": None})
    return run_id


def _graph_nodes(world: str = W) -> int:
    drv = driver()
    try:
        with drv.session() as s:
            return s.run("MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c", w=world).single()["c"]
    finally:
        drv.close()


def _stop_worker_at_documents_delete() -> None:
    """積んだ削除を子で実行し、目録の DELETE（グラフの削除の後）で止まっている間に SIGKILL する。"""
    import time

    import psycopg
    from _ingest_crash import start_worker_once
    from sherpa.store import db
    holder = psycopg.connect(db._dsn(), autocommit=True)
    holder.execute("SELECT pg_advisory_lock(%s, %s)", _STOP_LOCK)
    with db._connect() as c:
        c.execute("CREATE OR REPLACE FUNCTION stop_matrix_wait() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                  f"PERFORM pg_advisory_lock({_STOP_LOCK[0]}, {_STOP_LOCK[1]}); "
                  f"PERFORM pg_advisory_unlock({_STOP_LOCK[0]}, {_STOP_LOCK[1]}); RETURN NULL; END $$")
        c.execute("CREATE TRIGGER stop_matrix_documents_delete BEFORE DELETE ON documents "
                  "FOR EACH STATEMENT EXECUTE FUNCTION stop_matrix_wait()")
    proc = None
    try:
        _submit_delete()
        proc = start_worker_once()
        deadline = time.monotonic() + 120
        while True:
            with db._connect() as c:
                waiting = c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                                    "AND classid=%s AND objid=%s", _STOP_LOCK).fetchone()
            if waiting:
                break
            assert proc.poll() is None, proc.communicate()[1][-2000:]
            assert time.monotonic() < deadline, "目録の DELETE に達しませんでした"
            time.sleep(0.2)
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        holder.execute("SELECT pg_advisory_unlock(%s, %s)", _STOP_LOCK)
        holder.close()
        with db._connect() as c:
            c.execute("DROP TRIGGER IF EXISTS stop_matrix_documents_delete ON documents")
            c.execute("DROP FUNCTION IF EXISTS stop_matrix_wait()")


def _assert_world_is_gone() -> None:
    from sherpa import app_version, es_index
    assert store.get_world(W) is None
    assert _rows() == []
    assert not worlds.derived_dir(W).exists()
    assert _graph_nodes() == 0
    assert [i for i in es_index.list_kb_indices() if i.startswith(es_index._index(W))] == []
    assert store.count_queued_runs(app_version.current()) == (0, 0)


def _stop_delete(stop: str) -> None:
    from sherpa import es_index
    if stop == "after_graph":
        _stop_worker_at_documents_delete()
        return
    _submit_delete()
    kill = ({"kill_on_rmtree": str(worlds.derived_dir(W))} if stop == "after_ledger"
            else {"kill_on_http_send": f"DELETE /{es_index._index(W)}"})
    assert run_worker_once(**kill).returncode == -9


@pytest.mark.parametrize("stop", ["after_graph", "after_ledger", "after_derived"])
def test_kill_inside_delete_is_reported_and_startup_resume_finishes_it(registered_world, stop):
    """消去の途中（グラフの後・目録の後・派生の後）で止まると「途中で止まった」と出て、起動時の再開が消し切る。資料フォルダは戻らない。"""
    from sherpa.ingest import resume
    from sherpa.routers import worlds as api
    _stop_delete(stop)
    _wait_stopped()
    assert [r["kind"] for r in _rows()] == ["delete"]
    summary = api._ingest_summary(W, store.get_world(W), api._run_state(W))
    assert "取り込みの途中で止まったため" in summary["last_failure_message"]
    _settle_orphan()

    assert resume.resume_interrupted() == [W]
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    _assert_world_is_gone()


def test_update_over_a_stopped_delete_finishes_the_delete_instead_of_rebuilding(registered_world):
    """消去の途中で止まった資料フォルダで「更新」を押しても作り直さず、消去を最後まで実行する。"""
    _stop_delete("after_ledger")
    _wait_stopped()
    _settle_orphan()

    _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    _assert_world_is_gone()


def test_failed_delete_is_not_queued_again_by_worker_restarts(registered_world):
    """消去が失敗で終わった資料フォルダは、worker を何度起動し直しても消去を積み直さない。行は残り、「更新」が消し切る。"""
    import os

    from sherpa import app_version
    from sherpa.ingest import ingest_worker, resume
    locked = worlds.derived_dir(W) / "locked"
    locked.mkdir()
    (locked / "f").write_text("x", encoding="utf-8")
    os.chmod(locked, 0o555)
    try:
        run_id = _submit_delete()
        child = run_worker_once()
        assert child.returncode == 0, child.stderr[-2000:]
        run = _run(run_id)
        assert run["status"] == "failed" and not run["extraction_snapshot"].get("interrupted")
        assert [r["kind"] for r in _rows()] == ["delete"] and store.get_world(W) is not None
        for _ in range(2):
            ingest_worker.recover_orphans()
            assert resume.resume_interrupted() == []
            assert store.count_queued_runs(app_version.current()) == (0, 0)
    finally:
        os.chmod(locked, 0o755)

    _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    _assert_world_is_gone()


def test_delete_removes_every_row_of_the_world_including_single_document_rows(registered_world):
    """1 文書の行が残る資料フォルダを消すと、その資料フォルダの行は全部消える。"""
    from _replace_row import row_for
    row_for(W, "sub/a.xlsx")
    _submit_delete()
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert store.get_world(W) is None and _rows() == []


def test_failed_registration_compensation_keeps_world_and_delete_row_when_graph_delete_fails(monkeypatch, tmp_path):
    """登録の失敗の補償で Neo4j の削除が失敗すると、world の行と kind='delete' の行を残す。Neo4j が戻れば「更新」の消去が消し切る。"""
    name = "test_stop_matrix_reg"
    try:
        monkeypatch.setenv("NEO4J_URI", "bolt://127.0.0.1:1")
        with pytest.raises(Exception):
            worlds.register(name, str(tmp_path / "missing_root"))
        assert store.get_world(name) is not None
        assert [r["kind"] for r in _rows(name)] == ["delete"]
        monkeypatch.undo()

        assert worlds.delete(name) is True
        assert store.get_world(name) is None and _rows(name) == []
    finally:
        monkeypatch.undo()
        store.delete_world_row(name)


def test_orphan_cleanup_resumes_a_released_cleanup_row_from_where_it_stopped(registered_world):
    """孤児の掃除の「1 つ消す」を目録の後・派生の削除の直前で止めると、掃除の行が残り、次の reconcile が続きから消して行も消す。"""
    from sherpa import reconcile
    from sherpa.store import replace_rows
    from _replace_row import offline_row
    orphan = "test_stop_matrix_orphan2"
    store.replace_documents(orphan, [{"name": "a.txt", "layer": "version", "status": "indexed"}], row=offline_row(orphan))
    derived = worlds.derived_dir(orphan)
    (derived / "md").mkdir(parents=True)
    (derived / "md" / "a.md").write_text("x", encoding="utf-8")
    drv = driver()
    try:
        with drv.session() as s:
            s.run("CREATE (:Entity {world_id:$w, canonical_id:'x'})", w=orphan).consume()
        child = run_worker_once(kill_on_rmtree=str(derived), code=(
            "from sherpa import reconcile\n"
            f"reconcile.cleanup_world({orphan!r}, reflect=True, derived=True)\n"))
        assert child.returncode == -9, child.stderr[-2000:]
        assert derived.is_dir() and orphan not in store.list_document_worlds()
        left = _rows(orphan)
        assert [r["kind"] for r in left] == ["cleanup"]
        assert replace_rows.stale_cleanup_worlds() == [orphan]
        with drv.session() as s:
            assert s.run("MATCH (n:Entity {world_id:$w}) RETURN count(n) AS c", w=orphan).single()["c"] == 0

        out = reconcile.cleanup_world(orphan, reflect=True, derived=True)
        assert out is not None and out["failed"] == []
        assert not derived.exists() and _rows(orphan) == [] and replace_rows.stale_cleanup_worlds() == []
    finally:
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=orphan).consume()
        drv.close()
        shutil.rmtree(derived, ignore_errors=True)
        store.replace_documents(orphan, [], row=offline_row(orphan))
        from sherpa.store import db
        with db._connect() as c:
            c.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world=%s", (db._KB_ID, orphan))


def test_delete_with_unreachable_es_keeps_world_and_delete_row_until_es_returns(registered_world, monkeypatch):
    """ES に届かない間の削除は失敗にして、資料フォルダの行と delete の行を残す。ES が戻ってから消し切る。"""
    monkeypatch.setenv("ES_URL", "http://127.0.0.1:1")
    with pytest.raises(Exception):
        worlds.delete(W)
    assert store.get_world(W) is not None
    assert [r["kind"] for r in _rows()] == ["delete"]

    monkeypatch.undo()
    assert worlds.delete(W) is True
    assert store.get_world(W) is None and _rows() == []


def test_stopped_es_orphan_cleanup_is_resumed_as_es_not_as_a_world(registered_world):
    """ES の孤児の掃除が索引の削除で止まって残った行は、資料フォルダの掃除に拾われず（索引と行は残る）、次の ES の掃除が続きから消して行も消す。"""
    from _ingest_crash import run_worker_once
    from sherpa import es_index, reconcile
    from sherpa.store import replace_rows
    alias = "sherpa-kb-stop-matrix-orphan-0123456789"
    es_index._req("PUT", "/" + alias, {})
    list_only_ours = f"es_index.orphan_indices = lambda valid: [{alias!r}]\n"
    try:
        child = run_worker_once(kill_on_http_send=f"DELETE /{alias}", code=(
            "from sherpa import es_index, reconcile\n" + list_only_ours + "reconcile._reconcile_es(set(), [])\n"))
        assert child.returncode == -9, child.stderr[-2000:]
        assert alias in es_index.list_kb_indices()
        stale = replace_rows.stale_cleanup_worlds()
        assert len(stale) == 1

        valid = set(store.list_document_worlds()) | {W}
        failed: list = []
        reconcile._reconcile_worlds(valid, [reconcile._derived_parent()], False, failed)
        assert alias in es_index.list_kb_indices() and replace_rows.stale_cleanup_worlds() == stale

        with pytest.MonkeyPatch.context() as mp:
            mp.setattr(es_index, "orphan_indices", lambda valid: [alias])
            assert reconcile._reconcile_es(valid, failed) == [alias]
        assert alias not in es_index.list_kb_indices()
        assert replace_rows.stale_cleanup_worlds() == [] and failed == []
    finally:
        try:
            es_index._req("DELETE", "/" + alias)
        except Exception:
            pass
        from sherpa.store import db
        with db._connect() as c:
            c.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world LIKE 'es:%%'", (db._KB_ID,))
