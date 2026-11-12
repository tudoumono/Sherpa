"""チャットの実行記録（`chat_turn_runs`）の契約テスト（実 Postgres・down なら skip）。

プロセスが消えた状態は「記録を running のまま残す」ことで作り、起動時の回収・停止時の中断記録を直接呼んで確かめる。
"""
from __future__ import annotations

import os
import threading
import time
import uuid
from datetime import datetime, timezone

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest  # noqa: E402

from sherpa import chat_run_journal as J  # noqa: E402
from sherpa import chat_turns as CT  # noqa: E402
from sherpa import store  # noqa: E402
from sherpa.store import chat_runs  # noqa: E402


@pytest.fixture
def conv():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    uid = f"u-{uuid.uuid4().hex[:8]}"
    cid = store.create_conversation(user_id=uid, world="v1", title="journal")["id"]
    store.add_message(cid, "user", "調べて")
    return {"cid": cid, "uid": uid}


def _node(i: int, status: str = "done", detail: str = "d") -> dict:
    return {"type": "node", "id": f"n{i}", "kind": "tool", "label": f"道具{i}", "detail": detail,
            "status": status}


def _messages(cid: int) -> list[dict]:
    with store._connect() as c:
        return c.execute("SELECT role, content, answer, trace FROM messages WHERE conversation_id=%s ORDER BY id",
                         (cid,)).fetchall()


def _journal(conv) -> J.RunJournal:
    return J.RunJournal(turn_id=uuid.uuid4().hex, conversation_id=conv["cid"], user_id=conv["uid"])


# ===== 1. 実行の記録 =====

def test_run_row_is_running_at_start_and_final_state_at_finish(conv):
    j = _journal(conv)
    j.begin()
    assert chat_runs.get_run(j.turn_id)["status"] == "running"
    j.finish("completed")
    row = chat_runs.get_run(j.turn_id)
    assert row["status"] == "completed" and row["finished_at"] is not None and row["progress"] is None


def test_finish_does_not_overwrite_an_already_interrupted_run(conv):
    j = _journal(conv)
    j.begin()
    assert chat_runs.mark_interrupted(j.turn_id, "shutdown")
    j.finish("completed")
    assert chat_runs.get_run(j.turn_id)["status"] == "interrupted"


def test_start_turn_records_run_and_thread_end_state(conv):
    done = threading.Event()

    def factory(cid):
        def run(stop_event, emit):
            emit(_node(1))
            done.set()
        return run

    rec = CT.start_turn(uid=conv["uid"], conversation_factory=lambda: conv["cid"], run_fn_factory=factory)
    assert done.wait(5)
    rec.thread.join(5)
    assert chat_runs.get_run(rec.turn_id)["status"] == "completed"


def test_start_turn_records_failed_and_stopped(conv):
    def crash_factory(cid):
        def run(stop_event, emit):
            raise RuntimeError("boom")
        return run

    r1 = CT.start_turn(uid=conv["uid"], conversation_factory=lambda: conv["cid"], run_fn_factory=crash_factory)
    r1.thread.join(5)
    row = chat_runs.get_run(r1.turn_id)
    assert row["status"] == "failed" and row["end_reason"] == "RuntimeError"

    def stop_factory(cid):
        def run(stop_event, emit):
            stop_event.wait(5)
        return run

    r2 = CT.start_turn(uid=conv["uid"], conversation_factory=lambda: conv["cid"], run_fn_factory=stop_factory)
    CT.stop_turn(r2.turn_id, conv["uid"])
    r2.thread.join(5)
    assert chat_runs.get_run(r2.turn_id)["status"] == "stopped"


@pytest.mark.parametrize("status", ["failed", "stopped"])
def test_failed_or_stopped_turn_keeps_progress_seen_since_the_last_save(conv, status):
    """小分けの保存の間（3 秒以内）に失敗・停止で終わっても、それまでの調査の記録を残す。"""
    j = _journal(conv)
    j.begin()
    j.observe({"type": "node", **_node(1, detail="直前の記録")})
    j.finish(status, "x")
    row = chat_runs.get_run(j.turn_id)
    assert row["status"] == status
    assert "直前の記録" in str(row["progress"])


def test_failed_turn_keeps_progress_even_if_the_progress_save_fails(conv, monkeypatch):
    """途中経過だけの保存が失敗しても、失敗の状態と同じ書き込みで直前の記録を残す。"""
    j = _journal(conv)
    j.begin()
    monkeypatch.setattr(chat_runs, "save_progress", lambda **kw: (_ for _ in ()).throw(RuntimeError("db down")))
    j.observe({"type": "node", **_node(1, detail="保存に失敗した後の記録")})
    j.flush()
    j.finish("failed", "x")
    row = chat_runs.get_run(j.turn_id)
    assert row["status"] == "failed" and "保存に失敗した後の記録" in str(row["progress"])


def test_deleting_conversation_deletes_run_records(conv):
    j = _journal(conv)
    j.begin()
    assert store.delete_conversation(conv["cid"], user_id=conv["uid"])
    assert chat_runs.get_run(j.turn_id) is None


# ===== 2. 途中の保存 =====

def test_progress_is_saved_in_small_steps_while_running(conv, monkeypatch):
    monkeypatch.setattr(J, "FLUSH_INTERVAL_SECONDS", 0.0)
    j = _journal(conv)
    j.begin()
    j.observe(_node(1))
    j.observe(_node(2))
    nodes = chat_runs.get_run(j.turn_id)["progress"]["nodes"]
    assert [n["id"] for n in nodes] == ["n1", "n2"]
    j.observe(_node(1, detail="更新"))
    nodes = chat_runs.get_run(j.turn_id)["progress"]["nodes"]
    assert len(nodes) == 2 and nodes[0]["detail"] == "更新"


def test_progress_over_budget_drops_oldest_and_records_how_many(conv, monkeypatch):
    monkeypatch.setattr(J, "FLUSH_INTERVAL_SECONDS", 0.0)
    monkeypatch.setattr(J, "MAX_PROGRESS_BYTES", 2000)
    j = _journal(conv)
    j.begin()
    for i in range(30):
        j.observe(_node(i, detail="x" * 200))
    row = chat_runs.get_run(j.turn_id)
    ids = [n["id"] for n in row["progress"]["nodes"]]
    assert ids[-1] == "n29" and "n0" not in ids
    assert row["events_dropped"] == 30 - len(ids) > 0


def test_save_failure_does_not_stop_the_turn_and_is_recorded(conv, monkeypatch):
    monkeypatch.setattr(J, "FLUSH_INTERVAL_SECONDS", 0.0)
    j = _journal(conv)
    j.begin()
    real = chat_runs.save_progress

    def broken(**kw):
        raise OSError("disk full")

    monkeypatch.setattr(chat_runs, "save_progress", broken)
    j.observe(_node(1))   # 例外は外へ出ない
    assert j.save_failures == 1
    monkeypatch.setattr(chat_runs, "save_progress", real)
    j.observe(_node(2))
    row = chat_runs.get_run(j.turn_id)
    assert row["save_failures"] == 1 and len(row["progress"]["nodes"]) == 2


# ===== 3. 起動時の回収 =====

def test_recover_marks_running_as_interrupted_and_leaves_partial_trace_with_note(conv, monkeypatch):
    monkeypatch.setattr(J, "FLUSH_INTERVAL_SECONDS", 0.0)
    j = _journal(conv)
    j.begin()
    j.observe(_node(1))
    j.observe(_node(2, status="active"))
    # ここでプロセスが消えた: 終わりの記録を書かないまま、起動時の回収を呼ぶ
    assert j.turn_id in J.recover_unfinished()
    row = chat_runs.get_run(j.turn_id)
    assert row["status"] == "interrupted" and row["end_reason"] == "process_exit"
    last = _messages(conv["cid"])[-1]
    assert last["role"] == "assistant"
    assert J.INTERRUPTED_NOTICE in last["content"]
    assert last["answer"]["completion"] == "partial"
    assert {n["id"] for n in last["trace"]} >= {"n1", "n2"}
    # 二度目の起動では何も増えない
    assert j.turn_id not in J.recover_unfinished()
    assert len(_messages(conv["cid"])) == 2


def test_recover_without_progress_still_leaves_note_only(conv):
    j = _journal(conv)
    j.begin()
    J.recover_unfinished()
    last = _messages(conv["cid"])[-1]
    assert last["content"] == J.INTERRUPTED_NOTICE and not last["trace"]


def test_recover_does_not_add_note_when_answer_was_already_saved(conv):
    j = _journal(conv)
    j.begin()
    store.add_message(conv["cid"], "assistant", "回答済み")
    J.recover_unfinished()
    assert chat_runs.get_run(j.turn_id)["status"] == "interrupted"
    assert [m["role"] for m in _messages(conv["cid"])] == ["user", "assistant"]


# ===== 4. 通常の停止 =====

def test_shutdown_waits_for_a_turn_that_stops_and_interrupts_one_that_does_not(conv, monkeypatch):
    monkeypatch.setattr(J, "FLUSH_INTERVAL_SECONDS", 0.0)
    release = threading.Event()

    def obedient(cid):
        def run(stop_event, emit):
            emit(_node(1))
            stop_event.wait(10)
        return run

    def stuck(cid):
        def run(stop_event, emit):
            emit(_node(7, status="active"))
            release.wait(10)
        return run

    r1 = CT.start_turn(uid=conv["uid"], conversation_factory=lambda: conv["cid"], run_fn_factory=obedient)
    cid2 = store.create_conversation(user_id=conv["uid"], world="v1", title="j2")["id"]
    store.add_message(cid2, "user", "q")
    r2 = CT.start_turn(uid=conv["uid"], conversation_factory=lambda: cid2, run_fn_factory=stuck)
    time.sleep(0.2)
    try:
        interrupted = CT.shutdown_turns(wait_seconds=1.0)
    finally:
        release.set()
    assert interrupted == [r2.turn_id]
    assert chat_runs.get_run(r1.turn_id)["status"] == "stopped"
    assert chat_runs.get_run(r2.turn_id)["status"] == "interrupted"
    last = _messages(cid2)[-1]
    assert J.INTERRUPTED_NOTICE in last["content"] and {n["id"] for n in last["trace"]} == {"n7"}
    r2.thread.join(5)
    assert chat_runs.get_run(r2.turn_id)["status"] == "interrupted"   # 遅れて終わっても書き換えない


def test_shutdown_keeps_the_in_memory_progress_even_if_progress_saves_failed(conv, monkeypatch):
    """途中経過の保存が失敗していても、停止時の中断は手元の途中経過を会話と記録に残す。"""
    monkeypatch.setattr(J, "FLUSH_INTERVAL_SECONDS", 0.0)
    real_save = chat_runs.save_progress

    def save_fails_once_there_are_nodes(**kw):     # 開始の行は作れ、途中経過の保存だけが失敗する
        if kw.get("nodes"):
            raise RuntimeError("db down")
        return real_save(**kw)
    monkeypatch.setattr(chat_runs, "save_progress", save_fails_once_there_are_nodes)
    release = threading.Event()

    def stuck(cid):
        def run(stop_event, emit):
            emit(_node(9, status="active", detail="手元だけの記録"))
            release.wait(10)
        return run

    r = CT.start_turn(uid=conv["uid"], conversation_factory=lambda: conv["cid"], run_fn_factory=stuck)
    time.sleep(0.2)
    try:
        assert CT.shutdown_turns(wait_seconds=0.5) == [r.turn_id]
    finally:
        release.set()
    row = chat_runs.get_run(r.turn_id)
    assert row["status"] == "interrupted" and "手元だけの記録" in str(row["progress"])
    assert {n["id"] for n in _messages(conv["cid"])[-1]["trace"]} == {"n9"}
    r.thread.join(5)


# ===== 5. 完了ターンのバッファの合計上限 =====

def _done_record(uid: str, nbytes: int, completed_at: float) -> CT.TurnRecord:
    rec = CT.TurnRecord(turn_id=uuid.uuid4().hex, uid=uid, stop_event=threading.Event(), conversation_id=1)
    rec.buffer.append({"type": "node", "pad": "x" * nbytes})
    rec.buffer.mark_done()
    rec.buffer.completed_at = completed_at
    return rec


def test_completed_buffers_over_total_budget_are_dropped_oldest_first_but_running_kept(monkeypatch):
    monkeypatch.setattr(CT, "COMPLETED_MAX_BYTES", 1500)
    now = time.time()
    old = _done_record("u", 1000, now - 30)
    mid = _done_record("u", 1000, now - 20)
    new = _done_record("u", 1000, now - 10)
    running = CT.TurnRecord(turn_id=uuid.uuid4().hex, uid="u", stop_event=threading.Event(), conversation_id=2)
    running.buffer.append({"type": "node", "pad": "x" * 5000})
    with CT._REGISTRY_LOCK:
        CT._REGISTRY.clear()
        for r in (old, mid, new, running):
            CT._REGISTRY[r.turn_id] = r
        try:
            CT._sweep_expired_locked()
            kept = set(CT._REGISTRY)
        finally:
            CT._REGISTRY.clear()
    assert kept == {new.turn_id, running.turn_id}
