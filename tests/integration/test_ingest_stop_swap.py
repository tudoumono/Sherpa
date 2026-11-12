"""付け替えと 1 文書の入れ直しの途中で止まった場面の確認（本物の worker を子プロセスで動かす・要 Postgres＋Neo4j＋ES）。土台は `test_ingest_stop_matrix` と共用する。"""
from __future__ import annotations

import pathlib
import shutil
import tempfile
import time

import openpyxl
import pytest
from _ingest_crash import run_worker_once, start_worker_once
from test_ingest_stop_matrix import (  # noqa: F401  (registered_world は fixture)
    W, _enqueue_refresh, _rows, _run, _settle_orphan, _wait_stopped, registered_world)

from sherpa import store, worlds
from sherpa.ingest import background

OK = ("auto_published", "auto_published_with_flags")
REL = "sample.xlsx"
WHOLE_MESSAGE = "取り込みの途中で止まったため、一部の検索が古い内容のままです"
DOC_MESSAGE = "取り込みの途中で止まったため、一部の資料が古いままです"


def _summary() -> dict:
    from sherpa.routers import worlds as api
    return api._ingest_summary(W, store.get_world(W), api._run_state(W))


# ---- 付け替え ---------------------------------------------------------------------------------------

@pytest.fixture
def new_root():
    root = tempfile.mkdtemp()
    wb = openpyxl.Workbook()
    wb.active["A1"], wb.active["B1"] = "付け替え先", "別の内容"
    wb.save(pathlib.Path(root) / "moved.xlsx")
    yield str(pathlib.Path(root).resolve())
    shutil.rmtree(root, ignore_errors=True)


def _backup() -> pathlib.Path:
    der = worlds.derived_dir(W)
    return der.with_name("." + der.name + ".rebind-bak")


def _stop_rebind(root: str, **kill) -> None:
    background.submit(W, "rebind", "{}", {"path": root, "label": None})
    assert run_worker_once(**kill).returncode == -9
    _wait_stopped()


def _resume_by_update(left: dict, root: str) -> None:
    """「更新」で続けると、新 root の取り込みが入れ替えきられ、退避が片付き、行が消える。"""
    _settle_orphan()
    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK
    assert _rows() == [] and not _backup().exists()
    assert store.get_world(W)["root_path"] == root and store.get_world(W)["last_sig"]
    assert worlds.derived_md_dir(W).is_dir() and any(store.list_documents(W))


@pytest.mark.parametrize("stop", ["after_set_aside", "mid_new_root"])
def test_kill_inside_rebind_is_reported_and_update_finishes_it(registered_world, new_root, stop):
    """付け替えの退避の直後・新 root の取り込みの途中で止まると「途中で止まった」と出て、「更新」が退避を片付けて入れ替えきる。"""
    kill = {"kill_on_scan_of": new_root} if stop == "after_set_aside" else {"kill_on_http_send": "/_bulk"}
    _stop_rebind(new_root, **kill)

    left = _rows()
    assert len(left) == 1 and left[0]["doc"] == "" and left[0]["old_things"]["derived_backup"] == [str(_backup())]
    assert _backup().is_dir() and (stop == "mid_new_root" or not worlds.derived_dir(W).exists())
    assert WHOLE_MESSAGE in _summary()["last_failure_message"] and _summary()["ingest_active"] is False
    _resume_by_update(left[0], new_root)


# ---- 1 文書の入れ直し -------------------------------------------------------------------------------

def _submit_reconvert() -> int:
    run_id, _ = background.submit(W, "reconvert", '{"rel": "%s"}' % REL, {"rel": REL, "uid": None})
    return run_id


def _mark_failed() -> None:
    store.replace_failed_docs(W, {REL: "source_parse_failed"})


def _doc_stop_state() -> dict:
    left = _rows()
    assert len(left) == 1 and left[0]["doc"] == REL and left[0]["source_state"] == "fixed"
    assert store.unfinished_state(W) == "doc"
    assert DOC_MESSAGE in _summary()["last_failure_message"]
    return left[0]


def _wait_blocked_by_pg_trigger(proc, table: str, event: str) -> None:
    """文書 `REL` の行にだけ掛かる trigger の中で、親が持つ advisory lock を子が待つまで待つ。"""
    from sherpa.store import db
    deadline = time.monotonic() + 180
    while True:
        with db._connect() as c:
            waiting = c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND NOT granted "
                                "AND classid=7102 AND objid=9").fetchone()
        if waiting:
            return
        assert proc.poll() is None, proc.communicate()[1][-2000:]
        assert time.monotonic() < deadline, f"{table} の {event} に達しませんでした"
        time.sleep(0.2)


def _stop_reconvert_at_pg(table: str, event: str, rel_column: str) -> None:
    import psycopg

    from sherpa.store import db
    holder = psycopg.connect(db._dsn(), autocommit=True)
    holder.execute("SELECT pg_advisory_lock(7102, 9)")
    with db._connect() as c:
        c.execute("CREATE OR REPLACE FUNCTION stop_swap_wait() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                  "PERFORM pg_advisory_lock(7102, 9); PERFORM pg_advisory_unlock(7102, 9); RETURN OLD; END $$")
        c.execute(f"CREATE TRIGGER stop_swap_trigger BEFORE {event} ON {table} FOR EACH ROW "
                  f"WHEN (OLD.{rel_column} = '{REL}') EXECUTE FUNCTION stop_swap_wait()")
    proc = None
    try:
        _submit_reconvert()
        proc = start_worker_once()
        _wait_blocked_by_pg_trigger(proc, table, event)
        proc.kill()
        proc.wait(timeout=30)
    finally:
        if proc is not None and proc.poll() is None:
            proc.kill()
            proc.wait(timeout=30)
        holder.execute("SELECT pg_advisory_unlock(7102, 9)")
        holder.close()
        with db._connect() as c:
            c.execute(f"DROP TRIGGER IF EXISTS stop_swap_trigger ON {table}")
            c.execute("DROP FUNCTION IF EXISTS stop_swap_wait()")


def _stop_reconvert_in_graph() -> None:
    """積んだ再変換を子で実行し、グラフの切り替えが :SherpaMeta の lock を待つ間に SIGKILL する。"""
    from _world_setup import driver
    from test_ingest_stop_matrix import _wait_neo4j_blocked
    drv = driver()
    session = drv.session()
    tx = session.begin_transaction()
    proc = None
    try:
        tx.run("MATCH (m:SherpaMeta {world_id:$w}) SET m.stop_hold = true", w=W).consume()
        _submit_reconvert()
        proc = start_worker_once()
        _wait_neo4j_blocked(drv, proc, "グラフの切り替え")
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


def _stop_reconvert(stop: str) -> None:
    if stop == "before_publish":
        _submit_reconvert()
        assert run_worker_once(kill_on_rename="|.reconvert-bak").returncode == -9
    elif stop == "mid_publish":
        _submit_reconvert()
        assert run_worker_once(kill_on_rename=f"{REL}.md|{REL}.md.reconvert-bak").returncode == -9
    elif stop == "in_graph":
        _stop_reconvert_in_graph()
    elif stop == "after_graph":
        _submit_reconvert()
        assert run_worker_once(kill_on_http_send="HTTP/1.1").returncode == -9
    else:
        _stop_reconvert_at_pg("world_failed_docs", "DELETE", "rel")
    deadline = time.monotonic() + 30
    while store.unfinished_state(W) != "doc" and time.monotonic() < deadline:
        time.sleep(0.2)


@pytest.mark.parametrize("stop", ["before_publish", "mid_publish", "in_graph", "after_graph", "after_es"])
def test_kill_inside_a_single_document_redo_is_reported_and_redoing_it_finishes(registered_world, stop):
    """1 文書の入れ直しが途中で止まると「途中で止まった」と出て、規則版の再生成は押せず、その文書のやり直しは押せる。やり直すと入れ替えきって行が消え、失敗の一覧から外れる。"""
    _mark_failed()
    _stop_reconvert(stop)

    left = _doc_stop_state()
    actions = _summary()["actions"]
    assert actions["rag_regenerate_rules"]["allowed"] is False and actions["reconvert"]["allowed"] is True
    assert actions["refresh"]["allowed"] is True
    assert left["items"]["ir"] == ("swapped" if stop in ("before_publish", "mid_publish") else "cleaned")
    assert (left["items"]["es"] == "cleaned") == (stop == "after_es")
    _settle_orphan()
    if stop == "mid_publish":   # 失敗の一覧に無くても、止まった入れ直しの行が残る文書はやり直しを受け付ける
        store.clear_failed_docs(W)

    run_id = _submit_reconvert()
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK
    assert _rows() == [] and store.unfinished_state(W) is None
    assert store.list_failed_docs(W) == []


def test_update_with_the_same_signature_replaces_everything_over_a_stopped_document_redo(registered_world):
    """1 文書の入れ直しが止まった資料フォルダで、署名が同じ「更新」は変更なしで済ませず、全体を入れ替えてから行を消す。"""
    _mark_failed()
    _stop_reconvert("after_es")
    _doc_stop_state()
    before_sig = store.get_world(W)["last_sig"]
    _settle_orphan()

    run_id = _enqueue_refresh(W)
    child = run_worker_once()
    assert child.returncode == 0, child.stderr[-2000:]
    assert _run(run_id)["status"] in OK
    assert _rows() == [] and store.unfinished_state(W) is None
    assert store.get_world(W)["last_sig"] == before_sig


def test_regeneration_and_other_docs_are_refused_while_a_whole_row_is_left(registered_world):
    """止まった取り込みの行が残る資料フォルダでは、規則版の再生成も再変換も受け付けず案内する。「更新」は受け付ける。"""
    from fastapi import HTTPException

    from sherpa.routers.ingest_messages import UNFINISHED_ACTION_REASON, refuse_while_unfinished
    root = store.get_world(W)["root_path"]
    wb = openpyxl.Workbook()
    wb.active["A1"] = "x"
    wb.save(pathlib.Path(root) / "extra_whole.xlsx")
    _enqueue_refresh(W)
    assert run_worker_once(kill_on_http_send="/_bulk").returncode == -9
    _wait_stopped()

    actions = _summary()["actions"]
    for blocked in ("rag_regenerate_rules", "reconvert"):
        assert actions[blocked] == {"allowed": False, "reason": UNFINISHED_ACTION_REASON}
    assert actions["refresh"]["allowed"] is True
    for op in ("rag_regenerate_rules", "reconvert"):
        with pytest.raises(HTTPException) as e:
            refuse_while_unfinished(W, op, REL)
        assert e.value.status_code == 409
