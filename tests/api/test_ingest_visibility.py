from __future__ import annotations

import pytest

from sherpa import corpus_docs, store
from sherpa.routers import worlds as api
from sherpa.store import db as store_db


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
