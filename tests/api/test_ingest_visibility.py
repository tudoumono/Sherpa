from __future__ import annotations

import contextlib

import psycopg
import pytest

from sherpa import corpus_docs, store
from sherpa.routers import worlds as api
from sherpa.store import db as store_db
from sherpa.store.replace_rows import RUN_NS
from tests._replace_row import row_for


def _state(*, status="auto_published", progress=None, warnings=None, blocked=None):
    return {
        "graph_nodes": 4, "graph_edges": 3, "es_chunks": 6,
        "es_state": "ok", "es_error": None, "es_index_kept": None,
        "flag_counts": {"always_shown": [], "unparsed_syntax": 0, "unparsed_oversize": 0,
                        "mention_unreadable": 0, "md_failed_many": 0},
        "es_notice_stage": None, "last_run_id": 10, "last_run_status": status,
        "last_run_warnings": warnings or [], "last_run_interrupted": False,
        "last_run_blocked": blocked or [], "last_run_flags_total": 0,
        "last_run_flags_truncated": False, "failed_files": None,
        "partial_extraction_suspected": None, "stage_summary": None,
        "running_progress": progress, "last_completed_at": "2026-10-24T00:50:00+00:00",
        "prior_failure": None,
    }


def test_summary_separates_completed_counts_and_active_operation_contract():
    report = {**corpus_docs.empty_scan_report(), "scanned": 8, "indexed": 6, "office_failed": 2}
    row = {"last_scan_report": report, "last_scan_report_at": "2026-10-24T01:00:00+00:00",
           "last_synced_at": "2026-10-24T00:50:00+00:00"}
    progress = {"stage": "es_index", "stage_label": "internal", "done": 3, "total": 8,
                "updated_at": "2026-10-24T01:01:00+00:00", "step": "embed_vectors",
                "step_label": "internal"}

    summary = api._ingest_summary("w", row, _state(status="extracting", progress=progress))

    assert summary["last_completed_at"] == "2026-10-24T00:50:00+00:00"
    assert summary["last_completed_counts"] == {
        "as_of": "2026-10-24T01:00:00+00:00", "scanned": 8, "indexed": 6, "office_failed": 2}
    assert summary["ingest_active"] is True
    assert all(not state["allowed"] and "取り込み中" in state["reason"]
               for state in summary["actions"].values())
    assert summary["running_progress"]["stage_label"] == "検索できるように登録中"
    assert summary["running_progress"]["step_label"] == "意味でも探せるように登録しています"


def test_summary_returns_plain_previous_failure_reason_and_enabled_actions():
    row = {"last_scan_report": corpus_docs.empty_scan_report(),
           "last_scan_report_at": None, "last_synced_at": None}
    state = _state(status="failed", warnings=["graph_reflect_failed:MemoryError"])

    summary = api._ingest_summary("w", row, state)

    assert summary["last_failure_message"] == (
        "前回の取り込みは失敗しました（資料どうしの関係を書き込めませんでした）")
    assert summary["ingest_active"] is False
    assert all(action == {"allowed": True, "reason": None} for action in summary["actions"].values())


def test_previous_low_disk_stop_reports_free_space_in_plain_language():
    message = api._last_failure_message("failed", ["low_disk_stop:9.8"], [])

    assert message == "前回の取り込みは、ディスクの空きが少ないため停止しました（空き 9.8 GB）"



_PREFIX = "qtest-vis-"
_FAILED_FLAGS = {"flags": [{"doc": None, "action": "warn", "reason": "graph_reflect_failed:MemoryError"}]}
_RETRYING_MESSAGE = "前回の取り込みは失敗しました（資料どうしの関係を書き込めませんでした）"


@pytest.fixture
def world(tmp_path):
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"infra down: {e}")
    wid = _PREFIX + "w"
    _clean()
    store.upsert_world(wid, str(tmp_path), label=wid)
    yield wid
    _clean()


def _clean() -> None:
    with store_db._connect() as c:
        c.execute("DELETE FROM ingest_replacements WHERE world LIKE %s", (_PREFIX + "%",))
        c.execute("DELETE FROM ingest_runs WHERE version LIKE %s", (_PREFIX + "%",))
        c.execute("DELETE FROM worlds WHERE world_id LIKE %s", (_PREFIX + "%",))


def _summary(wid: str) -> dict:
    return api._ingest_summary(wid, store.get_world(wid), api._run_state(wid))


def _finish_ok(wid: str) -> dict:
    run = store.start_ingest_run(wid)
    return store.finish_ingest_run(run["id"], status="auto_published",
                                   published_snapshot={"nodes": 1, "edges": 0})


def _finish_failed(wid: str) -> None:
    run = store.start_ingest_run(wid)
    store.finish_ingest_run(run["id"], status="failed", extraction_snapshot=_FAILED_FLAGS)


def test_completed_time_stays_at_last_success_when_a_new_run_starts_and_bumps_last_synced_at(world):
    done = _finish_ok(world)
    store.start_ingest_run(world)
    with store_db._connect() as c:
        c.execute("UPDATE worlds SET last_synced_at=now() + interval '1 hour' WHERE world_id=%s", (world,))

    summary = _summary(world)

    assert summary["ingest_active"] is True
    assert summary["last_completed_at"] == str(done["published_at"])
    assert summary["last_completed_at"] != str(store.get_world(world)["last_synced_at"])


def test_failed_run_is_not_counted_as_completed(world):
    done = _finish_ok(world)
    _finish_failed(world)

    assert _summary(world)["last_completed_at"] == str(done["published_at"])


def test_previous_failure_stays_visible_while_retrying_and_disappears_when_the_retry_succeeds(world):
    _finish_failed(world)
    retry = store.start_ingest_run(world)

    running = _summary(world)
    assert running["ingest_active"] is True
    assert running["last_failure_message"] == _RETRYING_MESSAGE
    assert running["last_failure_retrying"] is True

    store.finish_ingest_run(retry["id"], status="auto_published", published_snapshot={"nodes": 1, "edges": 0})
    finished = _summary(world)
    assert finished["last_failure_message"] is None
    assert finished["last_failure_retrying"] is False


def test_failure_of_the_current_run_replaces_the_previous_one_and_is_not_marked_retrying(world):
    _finish_failed(world)
    retry = store.start_ingest_run(world)
    store.finish_ingest_run(retry["id"], status="failed",
                            extraction_snapshot={"flags": [{"doc": None, "action": "warn",
                                                            "reason": "es_index_failed:bulk_failed"}]})

    summary = _summary(world)

    assert summary["last_failure_message"] == "前回の取り込みは失敗しました（検索できる状態に更新できませんでした）"
    assert summary["last_failure_retrying"] is False


def test_no_failure_message_while_running_after_a_success(world):
    _finish_ok(world)
    store.start_ingest_run(world)

    summary = _summary(world)

    assert summary["last_failure_message"] is None
    assert summary["last_failure_retrying"] is False


def test_ingest_runs_list_carries_failure_message_from_flags(monkeypatch, auth_disabled):
    from fastapi.testclient import TestClient
    from sherpa.api import app
    runs = [{"id": 1, "version": "w", "status": "failed",
             "extraction_snapshot": {"flags": [{"doc": None, "action": "warn", "reason": "low_disk_stop:9.8"}]}},
            {"id": 2, "version": "w", "status": "published", "extraction_snapshot": {}}]
    monkeypatch.setattr(store, "list_ingest_runs", lambda w: runs)
    body = TestClient(app).get("/ingest/runs", params={"world": "w"}).json()
    assert "ディスクの空きが少ない" in body["runs"][0]["failure_message"]
    assert body["runs"][1]["failure_message"] is None


_WHOLE = "取り込みの途中で止まったため、一部の検索が古い内容のままです"
_DOC = "取り込みの途中で止まったため、一部の資料が古いままです"


@contextlib.contextmanager
def _holding(lock_id: int):
    """別の接続で回の lock を持つ（動いている worker の代わり）。"""
    conn = psycopg.connect(store_db._dsn(), autocommit=True)
    try:
        conn.execute("SELECT pg_advisory_lock(%s, %s)", (RUN_NS, lock_id))
        yield
    finally:
        conn.close()


def _dead_extracting(wid: str) -> int:
    run = store.start_ingest_run(wid)
    with store_db._connect() as c:
        c.execute("UPDATE ingest_runs SET status='extracting', claimed_by='dead-worker', op='refresh', "
                  "fingerprint='fp' WHERE id=%s", (run["id"],))
    return run["id"]


def test_no_row_means_nothing_unfinished(world):
    _finish_ok(world)

    assert store.unfinished_state(world) is None
    assert _summary(world)["last_failure_message"] is None


def test_row_whose_lock_is_held_is_running_not_stopped(world):
    row_for(world, lock_id=910001, held=False)

    with _holding(910001):
        assert store.unfinished_state(world) is None
        assert _summary(world)["last_failure_message"] is None


def test_whole_row_without_lock_is_reported_as_stopped_with_how_to_continue(world):
    _finish_ok(world)
    row_for(world, held=False)

    summary = _summary(world)

    assert store.unfinished_state(world) == "whole"
    assert _WHOLE in summary["last_failure_message"]
    assert "「更新」を押すと続きから入れ替えます" in summary["last_failure_message"]
    assert summary["ingest_active"] is False


def test_doc_row_without_lock_is_reported_as_stopped_for_documents(world):
    _finish_ok(world)
    row_for(world, "a.md", held=False)

    summary = _summary(world)

    assert store.unfinished_state(world) == "doc"
    assert _DOC in summary["last_failure_message"]


_GUIDE = "途中で止まった取り込みを先に「更新」で続けてください"


def test_whole_row_left_by_a_stop_blocks_regeneration_and_reconvert_but_not_update(world):
    _finish_ok(world)
    row_for(world, held=False)

    actions = _summary(world)["actions"]

    for blocked in ("rag_regenerate_rules", "reconvert"):
        assert actions[blocked] == {"allowed": False, "reason": _GUIDE}
    assert actions["refresh"]["allowed"] is True and actions["delete"]["allowed"] is True


def test_doc_row_left_by_a_stop_blocks_regeneration_and_other_docs_but_not_the_doc_itself(world):
    from fastapi import HTTPException

    from sherpa.routers.ingest_messages import refuse_while_unfinished
    _finish_ok(world)
    row_for(world, "a.xlsx", held=False)

    actions = _summary(world)["actions"]

    assert actions["rag_regenerate_rules"] == {"allowed": False, "reason": _GUIDE}
    assert actions["reconvert"]["allowed"] is True and actions["refresh"]["allowed"] is True
    refuse_while_unfinished(world, "reconvert", "a.xlsx")
    for op, rel in (("reconvert", "b.xlsx"), ("rag_regenerate_rules", None)):
        with pytest.raises(HTTPException) as e:
            refuse_while_unfinished(world, op, rel)
        assert e.value.status_code == 409 and _GUIDE in e.value.detail


def test_nothing_is_blocked_while_the_row_is_held_by_a_running_worker(world):
    from sherpa.routers.ingest_messages import refuse_while_unfinished
    _finish_ok(world)
    row_for(world, held=False, lock_id=910002)

    with _holding(910002):
        assert all(a["allowed"] for a in _summary(world)["actions"].values())
        refuse_while_unfinished(world, "rag_regenerate_rules")


def test_cleanup_row_is_not_reported(world):
    row_for(world, kind="cleanup", held=False)

    assert store.unfinished_state(world) is None


def test_dead_extracting_run_is_closed_when_the_screen_reads_and_actions_are_not_blocked(world):
    run_id = _dead_extracting(world)

    summary = _summary(world)

    assert summary["last_run_status"] == "failed" and summary["last_run_interrupted"] is True
    assert summary["ingest_active"] is False
    assert all(a["allowed"] for a in summary["actions"].values())
    assert store.get_latest_run_summary(world)["id"] == run_id


def test_running_run_with_its_lock_is_not_closed_by_a_read(world):
    run_id = _dead_extracting(world)

    with _holding(run_id):
        summary = _summary(world)

    assert summary["last_run_status"] == "extracting" and summary["ingest_active"] is True


def test_refresh_does_not_join_a_dead_run(world):
    dead = _dead_extracting(world)

    kind, row = store.enqueue_ingest_run(world, "refresh", {}, "fp", "v1")

    assert kind == "created" and row["id"] != dead
    assert store.get_latest_run_summary(world)["id"] == row["id"]
    with store_db._connect() as c:
        assert c.execute("SELECT status FROM ingest_runs WHERE id=%s", (dead,)).fetchone()["status"] == "failed"


def test_refresh_still_joins_a_run_whose_lock_is_held(world):
    live = _dead_extracting(world)

    with _holding(live):
        kind, row = store.enqueue_ingest_run(world, "refresh", {}, "fp", "v1")

    assert kind == "joined" and row["id"] == live
