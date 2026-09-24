from __future__ import annotations

import hashlib
import uuid

import pytest
from psycopg.types.json import Json

from sherpa import store
from sherpa.ingest import ai_observation, evidence_ir, observation_render
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


def test_succeeded_results_are_read_via_sort_key_then_batched_id_fetch(monkeypatch):
    """軽量read（id・source_rel_path・result_observation_set_hashのみ）→呼出側sort_keyで
    Python側ソート→batch_size件ずつid=ANY(%s)で本体取得、という新方式を検証する。軽量readの
    行順・id=ANYの戻り行順のどちらも並び替え済みでない前提で、最終的な出力は呼出側sort_key
    （ここではidentity）が決める順に一致することを確かめる。"""
    rows = [
        {"id": 1, "source_rel_path": "a.pdf", "result_observation_set_hash": "sha256:" + "a" * 64},
        {"id": 2, "source_rel_path": "a.pdf", "result_observation_set_hash": "sha256:" + "b" * 64},
        {"id": 3, "source_rel_path": "b.pdf", "result_observation_set_hash": "sha256:" + "c" * 64},
    ]
    light_connection = _Connection([_Cursor(all_rows=[rows[2], rows[0], rows[1]])])
    batch1_connection = _Connection([_Cursor(all_rows=[rows[1], rows[0]])])
    batch2_connection = _Connection([_Cursor(all_rows=[rows[2]])])
    connections = [light_connection, batch1_connection, batch2_connection]
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connections.pop(0))

    assert list(ocr_jobs.iter_succeeded_results("world", "a" * 64, str, batch_size=2)) == rows

    assert not connections
    assert "SELECT id, source_rel_path, result_observation_set_hash FROM" in light_connection.calls[0][0]
    assert "id = ANY" in batch1_connection.calls[0][0]
    assert batch1_connection.calls[0][1][-1] == [1, 2]
    assert batch2_connection.calls[0][1][-1] == [3]


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


# ---- iter_succeeded_results: 呼出側sort_keyが決める並び順がobservation_renderの検査と
#      一致することの実PostgreSQL再現（区切り文字の表記ゆれ・PurePosixPath正規化・DBの
#      デフォルト照合順序、いずれもOCR workerが
#      ``ValueError: observation records must be ordered by source_rel_path``で落ちる実環境障害）----

_ORDER_SOURCE_CONTENT_HASH = "sha256:" + "1" * 64


def _try_init_real_postgres() -> None:
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")


def _insert_succeeded_row(
    *, world: str, source_rel_path: str, canonical_generation_id: str, route_input_id: str,
) -> None:
    """`enqueue_job`（`_relative_path`が`\\`→`/`を正規化する）を経由せず、DBへ直接succeeded行を
    作る。正規化済みの経路からは`\\`を含む値を作れないため、既存の未正規化行を模すにはこれが要る。"""
    observation_set_hash = "sha256:" + hashlib.sha256(
        (source_rel_path + route_input_id).encode("utf-8")
    ).hexdigest()
    with ocr_jobs._connect() as connection:
        connection.execute(
            "INSERT INTO ocr_jobs (world, source_rel_path, canonical_generation_id, source_content_hash, "
            "route_manifest_hash, route_input_id, route_input, engine_profile_hash, status, "
            "result_observation_set_hash, result_payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,'succeeded',%s,%s)",
            (
                world, source_rel_path, canonical_generation_id, "sha256:" + "b" * 64, "sha256:" + "c" * 64,
                route_input_id, Json({"route_input_id": route_input_id, "status": "selected"}),
                "sha256:" + "e" * 64, observation_set_hash, Json({}),
            ),
        )


def _observation_set_for(seed: str, *, canonical_generation_id: str) -> ai_observation.AIObservationSet:
    observation_set = ai_observation.AIObservationSet(
        schema_version=ai_observation.AI_OBSERVATION_SCHEMA_VERSION,
        source_content_hash=_ORDER_SOURCE_CONTENT_HASH, canonical_generation_id=canonical_generation_id,
        provider="test", model="test-model", model_revision=None, execution_mode="local",
        prompt_schema_version="v1", preprocessing_profile="none", engine_profile_hash="sha256:" + "e" * 64,
        response_hash="sha256:" + hashlib.sha256(seed.encode("utf-8")).hexdigest(),
        inputs=[], observations=[], observation_set_hash="",
    )
    observation_set.observation_set_hash = ai_observation.content_hash(observation_set)
    return observation_set


def _publish_rows(rows: list[dict], *, generation: str, tmp_path) -> dict:
    """`iter_succeeded_results`の行をobservation_renderの公開処理へそのまま流す。"""
    ir = evidence_ir.EvidenceIR(
        schema_version=evidence_ir.EVIDENCE_IR_SCHEMA_VERSION,
        parser_profile=evidence_ir.EVIDENCE_PARSER_PROFILE,
        source=evidence_ir.EvidenceSource(file_type="image/png", content_hash=_ORDER_SOURCE_CONTENT_HASH),
    )
    records = (
        observation_render.ObservationRecord(
            source_rel_path=str(row["source_rel_path"]), ir=ir,
            observation_set=_observation_set_for(str(row["id"]), canonical_generation_id=generation),
        )
        for row in rows
    )
    return observation_render.publish_snapshot_stream(
        tmp_path, canonical_generation_id=generation, records=records, canonical_is_current=lambda: True,
    )


def test_iter_succeeded_results_orders_by_normalized_separator_across_batches(tmp_path):
    """呼出側sort_key（``observation_render._relative_source_path``）が決める順は、DBの生の並び
    （置換もPurePosixPath正規化もしない）とは一致しない——`\\`を含むpath（区切り文字の表記ゆれ）
    に加え、連続する`/`・`.`セグメント・末尾`/`（``PurePosixPath``正規化）を含む組でも同様。
    batch_size=1でページ境界をまたいでも行の抜け・重複が無いことも確かめる。"""
    _try_init_real_postgres()
    world = "test-ocr-order-" + uuid.uuid4().hex
    generation = "a" * 64
    try:
        raw_paths = ("a0.png", "n/trail/", "a\\c.png", "n//z", "a\\b.png", "n/0", "n/./b")
        for index, source_rel_path in enumerate(raw_paths):
            _insert_succeeded_row(
                world=world, source_rel_path=source_rel_path, canonical_generation_id=generation,
                route_input_id=f"route-{index}",
            )

        rows = list(ocr_jobs.iter_succeeded_results(
            world, generation, observation_render._relative_source_path, batch_size=1,
        ))
        assert [str(row["source_rel_path"]) for row in rows] == [
            "a\\b.png", "a\\c.png", "a0.png", "n/0", "n/./b", "n/trail/", "n//z",
        ]

        result = _publish_rows(rows, generation=generation, tmp_path=tmp_path)
        assert result["status"] == "published"
        assert result["artifact_count"] == 7
    finally:
        ocr_jobs.purge_world(world)


def test_iter_succeeded_results_orders_by_c_collation_not_locale_collation(tmp_path):
    """DBのデフォルト（ロケール依存）照合順序は、大文字/小文字混在でPythonのcodepoint順と
    食い違いうる（例: 'B...' と 'a...'）。ソートはPython側（呼出側sort_key）だけで決まるため、
    DBの照合順序に依存せずobservation_renderの検査と一致することを確かめる。"""
    _try_init_real_postgres()
    world = "test-ocr-order-collate-" + uuid.uuid4().hex
    generation = "a" * 64
    try:
        for index, source_rel_path in enumerate(("alpha.png", "Bravo.png")):
            _insert_succeeded_row(
                world=world, source_rel_path=source_rel_path, canonical_generation_id=generation,
                route_input_id=f"route-{index}",
            )

        rows = list(ocr_jobs.iter_succeeded_results(
            world, generation, observation_render._relative_source_path,
        ))
        assert [str(row["source_rel_path"]) for row in rows] == ["Bravo.png", "alpha.png"]

        result = _publish_rows(rows, generation=generation, tmp_path=tmp_path)
        assert result["status"] == "published"
        assert result["artifact_count"] == 2
    finally:
        ocr_jobs.purge_world(world)
