from __future__ import annotations

import pytest

from sherpa.store import ocr_jobs


class _Cursor:
    def __init__(self, *, one=None, all_rows=None, rowcount=0):
        self.one = one
        self.all_rows = all_rows or []
        self.rowcount = rowcount

    def fetchone(self):
        return self.one

    def fetchall(self):
        return self.all_rows


class _Connection:
    def __init__(self, cursors):
        self.cursors = iter(cursors)
        self.calls = []

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return None

    def execute(self, sql, params=None):
        self.calls.append((sql, params))
        return next(self.cursors)


def test_job_validation_rejects_absolute_paths_before_database_access():
    with pytest.raises(ValueError, match="relative"):
        ocr_jobs.enqueue_job(
            world="world", source_rel_path="/customer/design.xlsx", canonical_generation_id="a" * 64,
            source_content_hash="sha256:" + "b" * 64, route_manifest_hash="sha256:" + "c" * 64,
            route_input={
                "route_input_id": "route", "input_kind": "asset", "status": "selected",
                "asset_rel_path": "asset.png",
            },
            engine_profile_hash="sha256:" + "d" * 64,
        )


def test_status_summary_exposes_world_api_aggregates(monkeypatch):
    connection = _Connection([
        _Cursor(all_rows=[{"status": "succeeded", "count": 4}, {"status": "failed", "count": 1}]),
        _Cursor(one={"updated_at": "now"}),
        _Cursor(one={
            "targets": 7, "processed": 4, "cached": 2, "empty": 1, "failed": 1, "pending": 2,
        }),
    ])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)
    summary = ocr_jobs.status_summary("world", "a" * 64)
    assert {key: summary[key] for key in ("targets", "processed", "cached", "empty", "failed", "pending")} == {
        "targets": 7, "processed": 4, "cached": 2, "empty": 1, "failed": 1, "pending": 2,
    }
    assert summary["counts"]["succeeded"] == 4
    assert summary["counts"]["queued"] == 0


def test_lease_sweeps_exhausted_expired_jobs_before_skip_locked_claim(monkeypatch):
    connection = _Connection([_Cursor(), _Cursor(one=None)])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)
    assert ocr_jobs.lease_next("worker", lease_seconds=60) is None
    assert "attempts>=max_attempts" in connection.calls[0][0]
    assert "FOR UPDATE SKIP LOCKED" in connection.calls[1][0]
    assert connection.calls[1][1][0] == "worker"


def test_worker_availability_comes_from_recent_worker_heartbeat(monkeypatch):
    connection = _Connection([_Cursor(all_rows=[{
        "worker_id": "ocr-1", "available": True, "unavailable_reason": None,
        "model_hashes_valid": True, "status": "idle", "metadata": {}, "last_seen_at": "now",
    }])])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)
    summary = ocr_jobs.worker_availability_summary("sha256:" + "a" * 64)
    assert summary["available"] is True
    assert summary["model_hashes_valid"] is True
    assert summary["worker_count"] == 1
    assert "FROM ocr_worker_heartbeats" in connection.calls[0][0]


def test_no_recent_heartbeat_reports_worker_not_seen(monkeypatch):
    connection = _Connection([_Cursor(all_rows=[])])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)
    summary = ocr_jobs.worker_availability_summary("sha256:" + "a" * 64)
    assert summary["available"] is False
    assert summary["unavailable_reason"] == "worker_not_seen"
    assert summary["model_hashes_valid"] is False


def test_generation_state_waits_for_jobs_and_refresh_scheduler(monkeypatch):
    connection = _Connection([_Cursor(one={
        "pending_jobs": 0, "unpublished_jobs": 4, "pending_refresh_runs": 1,
    })])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    state = ocr_jobs.generation_state("world", "a" * 64)

    assert state == {
        "terminal": False, "pending_jobs": 0, "pending_refresh_runs": 1, "unpublished_jobs": 4,
    }
    assert "ocr_refresh_runs" in connection.calls[0][0]


def test_enqueue_refresh_run_is_small_idempotent_scheduler_request(monkeypatch):
    inserted = {
        "id": 1, "world": "world", "canonical_generation_id": "a" * 64, "status": "queued",
    }
    connection = _Connection([_Cursor(one=inserted)])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    row = ocr_jobs.enqueue_refresh_run("world", "a" * 64, "sha256:" + "b" * 64)

    assert row == inserted
    assert "INSERT INTO ocr_refresh_runs" in connection.calls[0][0]
    assert "route_input" not in connection.calls[0][0]


def test_purge_world_deletes_all_ocr_text_in_one_connection(monkeypatch):
    connection = _Connection([
        _Cursor(rowcount=1),
        _Cursor(rowcount=2),
        _Cursor(rowcount=1),
    ])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    result = ocr_jobs.purge_world("world")

    assert result == {"jobs": 2, "cache_entries": 1, "refresh_runs": 1}
    assert all("RETURNING" not in sql for sql, _params in connection.calls)
    assert [call[0].split()[2] for call in connection.calls] == [
        "ocr_refresh_runs", "ocr_jobs", "ocr_result_cache",
    ]


def test_cancel_superseded_generation_covers_jobs_and_refresh_runs(monkeypatch):
    connection = _Connection([
        _Cursor(rowcount=1),
        _Cursor(rowcount=1),
    ])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    result = ocr_jobs.cancel_superseded_generations("world", "a" * 64)

    assert result == {"jobs_cancelled": 1, "refresh_runs_cancelled": 1}
    assert all("canonical_generation_id<>%s" in sql for sql, _params in connection.calls)
    assert all("RETURNING" not in sql for sql, _params in connection.calls)


@pytest.mark.parametrize(
    ("operation", "rowcounts", "expected"),
    [
        (lambda: ocr_jobs.cancel_generation("world", "a" * 64), [700_000], 700_000),
        (lambda: ocr_jobs.purge_generation("world", "a" * 64), [4, 700_000], {"jobs": 700_000, "refresh_runs": 4}),
        (lambda: ocr_jobs.purge_superseded_generations("world", "a" * 64), [3, 600_000], {
            "jobs": 600_000, "refresh_runs": 3,
        }),
        (lambda: ocr_jobs.requeue_failed("world", "a" * 64), [500_000], 500_000),
    ],
)
def test_bulk_lifecycle_operations_use_rowcount_without_materializing_ids(
    monkeypatch, operation, rowcounts, expected,
):
    connection = _Connection([_Cursor(rowcount=value) for value in rowcounts])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    assert operation() == expected
    assert all("RETURNING" not in sql for sql, _params in connection.calls)


def test_cache_commit_is_refused_when_job_lease_is_lost(monkeypatch):
    connection = _Connection([_Cursor(one=None)])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    row = ocr_jobs.put_cached_result_for_lease(
        1, "lost-token", "world", "sha256:" + "a" * 64, "sha256:" + "b" * 64,
        {"schema": "ocr-engine-lines-v1", "observations": []},
    )

    assert row is None
    assert len(connection.calls) == 1
    assert "lease_expires_at>now() FOR UPDATE" in connection.calls[0][0]


def test_succeeded_results_are_read_with_bounded_keyset_pages(monkeypatch):
    rows = [
        {"id": 1, "source_rel_path": "a.pdf", "result_observation_set_hash": "sha256:" + "a" * 64},
        {"id": 2, "source_rel_path": "a.pdf", "result_observation_set_hash": "sha256:" + "b" * 64},
        {"id": 3, "source_rel_path": "b.pdf", "result_observation_set_hash": "sha256:" + "c" * 64},
    ]
    connections = [
        _Connection([_Cursor(all_rows=rows[:2])]),
        _Connection([_Cursor(all_rows=rows[2:])]),
    ]
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connections.pop(0))

    assert list(ocr_jobs.iter_succeeded_results("world", "a" * 64, batch_size=2)) == rows

    assert not connections


def test_result_snapshot_and_guarded_mark_do_not_materialize_job_ids(monkeypatch):
    snapshot_connection = _Connection([_Cursor(one={
        "row_count": 500_000, "min_id": 1, "max_id": 700_000, "id_sum": 123_456_789,
    })])
    mark_connection = _Connection([_Cursor(one={"marked": 499_999})])
    connections = [snapshot_connection, mark_connection]
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connections.pop(0))

    snapshot = ocr_jobs.succeeded_results_snapshot("world", "a" * 64)
    marked = ocr_jobs.mark_snapshot_artifacts_published("world", "a" * 64, snapshot)

    assert snapshot == {
        "row_count": 500_000, "min_id": 1, "max_id": 700_000, "id_sum": 123_456_789,
    }
    assert marked == 499_999
    assert "sum(id)" in snapshot_connection.calls[0][0]
    assert "WITH current AS MATERIALIZED" in mark_connection.calls[0][0]
    assert "id = ANY" not in mark_connection.calls[0][0]
