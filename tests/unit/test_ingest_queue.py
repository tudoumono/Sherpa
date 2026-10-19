"""取り込みの依頼（`ingest_runs` の queued）と取り込みワーカー。

Web アプリは依頼を積むだけで、同じ版のワーカーが 1 件拾って実行する。ここでは実 DB の上で次を固定する:
- 依頼は queued で積まれ、ワーカーが 1 件拾って処理し、プロセスを終える
- 同じ操作の再要求は合流し、別の操作は衝突（409）。同時の受付でも 1 件しか積まれない
- 版の違う依頼はワーカーが拾わず、待機の状態（waiting_version）になる
- 持ち主の居ない extracting は中断へ格下げされ、起動時に中断した run の再開依頼が積まれる
実行する操作（Neo4j／ES に触る取り込み本体）だけを差し替える。
"""
from __future__ import annotations

import os
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from sherpa import app_version, store, webhooks
from sherpa.ingest import background, ingest_worker, jobs, resume
from sherpa.store import db as store_db
from sherpa.store import workers as store_workers

_REPO = Path(__file__).resolve().parents[2]
_PREFIX = "qtest-"


@pytest.fixture(autouse=True)
def qdb():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    _clean()
    yield
    _clean()
    ingest_worker._terminate.clear()


def _clean() -> None:
    with store_db._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version LIKE %s", (_PREFIX + "%",))
        c.execute("DELETE FROM ingest_workers WHERE worker_id LIKE %s", (_PREFIX + "%",))
        c.execute("DELETE FROM worlds WHERE world_id LIKE %s", (_PREFIX + "%",))


def _row(run_id: int) -> dict:
    with store_db._connect() as c:
        return c.execute("SELECT * FROM ingest_runs WHERE id=%s", (run_id,)).fetchone()


def _finish_op(world, payload, run_id):
    store.finish_ingest_run(run_id, status="auto_published")


def test_request_is_queued_then_worker_picks_one_and_runs_it(monkeypatch):
    ran = []

    def refresh(world, payload, run_id):
        ran.append((world, run_id))
        store.finish_ingest_run(run_id, status="auto_published")

    monkeypatch.setitem(jobs.OPS, "refresh", refresh)
    run_id, joined = background.submit("qtest-a", "refresh", "{}", {"x": 1})

    row = _row(run_id)
    assert joined is False and row["status"] == "queued" and row["op"] == "refresh"
    assert row["payload"] == {"x": 1} and row["app_version"] == app_version.current()
    assert ran == []                                           # 積んだだけでは何も実行しない

    version = app_version.current()
    assert ingest_worker.process_one("qtest-wk1", version) is True
    assert ran == [("qtest-a", run_id)]
    done = _row(run_id)
    assert done["status"] == "auto_published" and done["claimed_by"] == "qtest-wk1"
    assert done["extraction_snapshot"] is None or done["extraction_snapshot"].get("op") in (None, "refresh")
    assert ingest_worker.process_one("qtest-wk1", version) is False   # 拾えるものが無い


def test_worker_process_picks_one_request_and_exits():
    """本物のワーカープロセス: 1 件拾って処理し、自分から終了する（メモリを OS へ戻すため）。"""
    run_id, _ = background.submit("qtest-sub", "refresh", "{}")     # 未登録の資料フォルダ＝実行は失敗で終わる
    env = {**os.environ, "PYTHONPATH": str(_REPO), "SHERPA_INGEST_POLL_SEC": "1"}
    proc = subprocess.run([sys.executable, "-m", "sherpa.ingest.ingest_worker"], cwd=_REPO, env=env,
                          capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    row = _row(run_id)
    assert row["status"] == "failed" and str(row["claimed_by"]).startswith("ingest:")
    with store_db._connect() as c:
        beats = c.execute("SELECT role, app_version FROM ingest_workers WHERE worker_id=%s",
                          (row["claimed_by"],)).fetchall()
    assert beats == [{"role": "ingest", "app_version": app_version.current()}]


def test_same_op_joins_other_op_conflicts_and_finished_run_frees_the_world():
    first, joined = background.submit("qtest-b", "refresh", "{}")
    assert joined is False
    again, joined = background.submit("qtest-b", "refresh", "{}")
    assert again == first and joined is True

    with pytest.raises(background.ConflictError) as ei:
        background.submit("qtest-b", "rebind", '{"path":"/x"}', {"path": "/x"})
    assert ei.value.existing_op == "refresh" and ei.value.existing_run_id == first

    other, joined = background.submit("qtest-other", "refresh", "{}")      # 別の資料フォルダは独立
    assert joined is False and other != first

    store.finish_ingest_run(first, status="auto_published")
    after, joined = background.submit("qtest-b", "rebind", '{"path":"/x"}', {"path": "/x"})
    assert joined is False and after != first                              # 終わった run には合流しない


def test_extra_key_arbitrates_new_registrations_across_worlds():
    first, _ = background.submit("qtest-n1", "register", "fp1", extra_keys=("__qtest_new__",))
    with pytest.raises(background.ConflictError):
        background.submit("qtest-n2", "register", "fp2", extra_keys=("__qtest_new__",))
    again, joined = background.submit("qtest-n1", "register", "fp1", extra_keys=("__qtest_new__",))
    assert again == first and joined is True
    same_world, joined = background.submit("qtest-n1", "register", "fp1")   # 資料フォルダ単独のキーでも同じ run が見える
    assert same_world == first and joined is True


def test_concurrent_requests_queue_exactly_one_run():
    results: list = []

    def go():
        results.append(background.submit("qtest-c", "refresh", "{}"))

    threads = [threading.Thread(target=go) for _ in range(8)]
    [t.start() for t in threads]
    [t.join(timeout=20) for t in threads]
    assert len(results) == 8
    assert len({r[0] for r in results}) == 1 and sum(1 for r in results if not r[1]) == 1
    with store_db._connect() as c:
        n = c.execute("SELECT count(*) AS n FROM ingest_runs WHERE version='qtest-c'").fetchone()["n"]
    assert n == 1


def test_shutting_down_web_app_rejects_new_requests():
    background.stop_accepting()
    try:
        with pytest.raises(background.ShuttingDownError):
            background.submit("qtest-d", "refresh", "{}")
    finally:
        background.start_accepting()


def test_worker_does_not_pick_request_from_other_version_and_waits(monkeypatch):
    monkeypatch.setattr(app_version, "current", lambda: "9.9.9+aaaaaaaa")
    run_id, _ = background.submit("qtest-v", "refresh", "{}")
    monkeypatch.undo()
    assert _row(run_id)["app_version"] == "9.9.9+aaaaaaaa"

    mine = "9.9.9+bbbbbbbb"
    assert ingest_worker.process_one("qtest-wk2", mine) is False
    assert store.count_queued_runs(mine) == (0, 1)
    assert _row(run_id)["status"] == "queued"                  # 失敗にも実行中にもしない

    monkeypatch.setenv("SHERPA_INGEST_POLL_SEC", "1")
    beat = ingest_worker._Beat("qtest-wk2", mine)
    t = threading.Thread(target=ingest_worker._wait_and_process, args=("qtest-wk2", mine, beat))
    t.start()
    try:
        deadline = time.monotonic() + 15
        state = None
        while time.monotonic() < deadline and state != "waiting_version":
            time.sleep(0.2)
            with store_db._connect() as c:
                r = c.execute("SELECT state FROM ingest_workers WHERE worker_id='qtest-wk2'").fetchone()
            state = r["state"] if r else None
        assert state == "waiting_version"
    finally:
        ingest_worker._terminate.set()
        t.join(timeout=10)
    assert _row(run_id)["status"] == "queued"


def test_operation_that_crashes_without_finishing_is_failed_and_notified(monkeypatch):
    def boom(world, payload, run_id):
        raise RuntimeError("simulated crash")

    monkeypatch.setitem(jobs.OPS, "refresh", boom)
    notified = []
    monkeypatch.setattr(webhooks, "notify_run_terminal", lambda *a, **kw: notified.append(a))
    run_id, _ = background.submit("qtest-e", "refresh", "{}")

    assert ingest_worker.process_one("qtest-wk3", app_version.current()) is True
    row = _row(run_id)
    assert row["status"] == "failed"
    assert row["extraction_snapshot"]["flags"][0]["reason"] == "background_worker_exited_without_terminal_status"
    assert notified == [("qtest-e", run_id, "refresh", "failed")]


def test_stop_request_reaches_the_operation_only_while_it_runs(monkeypatch):
    seen = []

    def op(world, payload, run_id):
        seen.append(background.stop_requested())
        store.finish_ingest_run(run_id, status="failed")

    monkeypatch.setitem(jobs.OPS, "refresh", op)
    background.submit("qtest-f", "refresh", "{}")
    background.request_stop()
    try:
        assert background.stop_requested() is False            # 取り込みの run の外では効かない
        ingest_worker.process_one("qtest-wk4", app_version.current())
        assert seen == [True]
        assert background.stop_requested() is False
    finally:
        background.start_accepting()


def test_unknown_operation_name_fails_the_run_instead_of_hanging():
    run_id, _ = background.submit("qtest-g", "no_such_op", "{}")
    ingest_worker.process_one("qtest-wk5", app_version.current())
    assert _row(run_id)["status"] == "failed"


# ---- 起動時: 異常終了の後始末と中断した取り込みの再開 ----

def _extracting_run(world: str, claimed_by: str | None) -> int:
    run = store.start_ingest_run(world)
    with store_db._connect() as c:
        c.execute("UPDATE ingest_runs SET claimed_by=%s, op='refresh' WHERE id=%s", (claimed_by, run["id"]))
    return run["id"]


def test_orphan_of_dead_worker_is_downgraded_but_live_worker_is_left_alone():
    dead = _extracting_run("qtest-h1", "qtest-dead-worker")
    live_id = _extracting_run("qtest-h2", "qtest-live-worker")
    legacy = _extracting_run("qtest-h3", None)                   # 0.18.x のアプリがプロセス内で動かしていた run
    lock = store_workers.hold_liveness_lock("qtest-live-worker")   # 生きているワーカーは lock を持っている
    try:
        assert ingest_worker.recover_orphans() == [dead]
        assert _row(dead)["status"] == "failed" and _row(dead)["extraction_snapshot"]["interrupted"] is True
        assert _row(live_id)["status"] == "extracting"
        assert _row(legacy)["status"] == "extracting"            # ワーカーは自分が拾っていない行に触れない
        assert store.downgrade_orphaned_extracting_runs(legacy_only=True) == [legacy]   # Web アプリの起動時の整理
        assert _row(live_id)["status"] == "extracting"
    finally:
        lock.close()


def test_start_requests_resume_for_interrupted_world(tmp_path):
    wid = "qtest-r1"
    store.upsert_world(wid, str(tmp_path), label=wid)
    run = store.start_ingest_run(wid)
    store.note_ingest_run_op(run["id"], "refresh")
    store.downgrade_orphaned_extracting_runs(wid)                # 強制終了で中断へ格下げされた run

    assert resume.resume_interrupted() == [wid]
    with store_db._connect() as c:
        queued = c.execute("SELECT op, status, app_version FROM ingest_runs "
                           "WHERE version=%s AND status='queued'", (wid,)).fetchall()
    assert queued == [{"op": "refresh", "status": "queued", "app_version": app_version.current()}]
    assert resume.resume_interrupted() == []                     # 積み済みなら二重に積まない


def test_start_does_not_resume_interrupted_delete(tmp_path):
    wid = "qtest-r2"
    store.upsert_world(wid, str(tmp_path), label=wid)
    run = store.start_ingest_run(wid)
    store.note_ingest_run_op(run["id"], "delete")
    store.downgrade_orphaned_extracting_runs(wid)
    assert resume.resume_interrupted() == []


def test_queued_request_from_previous_version_is_restamped_so_the_new_worker_can_pick_it(monkeypatch):
    """Web アプリの版を入れ替えると、前の版が積んだままの依頼は同じ版のワーカーが拾えず資料フォルダを塞ぐ。起動時に今の版へ付け替える。"""
    monkeypatch.setattr(app_version, "current", lambda: "1.0.0+oldoldol")
    run_id, _ = background.submit("qtest-s", "refresh", "{}")
    monkeypatch.undo()
    now = app_version.current()
    assert ingest_worker.process_one("qtest-wk6", now) is False        # このままでは拾えない

    assert run_id in store.restamp_queued_runs(now)
    assert _row(run_id)["app_version"] == now
    monkeypatch.setitem(jobs.OPS, "refresh", _finish_op)
    assert ingest_worker.process_one("qtest-wk6", now) is True
    assert _row(run_id)["status"] == "auto_published"
