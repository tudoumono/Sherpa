from __future__ import annotations

import hashlib
import uuid

import pytest
from psycopg.types.json import Json

from sherpa import store
from sherpa.ingest import ai_observation, evidence_ir, observation_render
from sherpa.store import ocr_jobs
from tests._replace_row import offline_row


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
        ocr_jobs._job_values(
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
    connection = _Connection([_Cursor(rowcount=0), _Cursor(rowcount=0), _Cursor(one=inserted)])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    row = ocr_jobs.enqueue_refresh_run("world", "a" * 64, "sha256:" + "b" * 64)

    assert row == inserted
    assert "INSERT INTO ocr_refresh_runs" in connection.calls[2][0]
    assert "route_input" not in connection.calls[2][0]


def test_purge_world_deletes_all_ocr_text_in_one_connection(monkeypatch):
    connection = _Connection([
        _Cursor(rowcount=1),
        _Cursor(rowcount=2),
        _Cursor(rowcount=1),
    ])
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: None)
    monkeypatch.setattr(ocr_jobs, "_connect", lambda: connection)

    result = ocr_jobs.purge_world("world", row=offline_row("world", kind="delete"))

    assert result == {"jobs": 2, "cache_entries": 1, "refresh_runs": 1}
    assert all("RETURNING" not in sql for sql, _params in connection.calls)
    assert [call[0].split()[2] for call in connection.calls] == [
        "ocr_refresh_runs", "ocr_jobs", "ocr_result_cache",
    ]


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
    """`_job_values`（`_relative_path`が`\\`→`/`を正規化する）を経由せず、DBへ直接succeeded行を
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
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


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
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_unsupported_image_route_cancels_old_failed_job_but_not_leased():
    """読めない画像形式として対象外になったルートの過去の failed/queued job は cancelled に終端し、
    実行中（leased）の行は触らない。status_summary では failed ではなく cancelled に数える。"""
    from sherpa.ingest import ocr_router

    _try_init_real_postgres()
    world = "test-ocr-unsupported-" + uuid.uuid4().hex
    generation = "a" * 64
    profile = "sha256:" + "e" * 64
    try:
        def decision(route_id: str, status: str, reason: str) -> ocr_router.OCRRouteDecision:
            return ocr_router.OCRRouteDecision(
                route_input_id=route_id, target_evidence_id="e-" + route_id, input_kind="asset",
                status=status, reason_code=reason, priority=1,
                asset_sha256="sha256:" + "f" * 64, asset_rel_path="a.wmf", media_type="image/x-wmf",
            )

        def manifest(*decisions: ocr_router.OCRRouteDecision) -> ocr_router.OCRRouteManifest:
            return ocr_router.OCRRouteManifest(
                schema_version=ocr_router.OCR_ROUTE_SCHEMA_VERSION,
                router_profile=ocr_router.OCR_ROUTER_PROFILE,
                source_rel_path="doc/old.xls", source_content_hash="sha256:" + "b" * 64,
                decisions=list(decisions), route_manifest_hash="sha256:" + "c" * 64,
            )

        old = manifest(*(decision(f"r{i}", "selected", "evidence_raster_asset") for i in range(3)))
        ocr_jobs.enqueue_manifest_jobs(
            world, old, canonical_generation_id=generation, engine_profile_hash=profile,
        )
        with ocr_jobs._connect() as connection:
            connection.execute(
                "UPDATE ocr_jobs SET status='failed', error_code='engine_failure' "
                "WHERE world=%s AND route_input_id='r0'", (world,),
            )
            connection.execute(
                "UPDATE ocr_jobs SET status='leased', lease_token='t' "
                "WHERE world=%s AND route_input_id='r1'", (world,),
            )

        new = manifest(*(decision(f"r{i}", "excluded", "unsupported_image_format") for i in range(2)))
        assert ocr_jobs.enqueue_manifest_jobs(
            world, new, canonical_generation_id=generation, engine_profile_hash=profile,
        ) == []

        with ocr_jobs._connect() as connection:
            rows = {
                row["route_input_id"]: row for row in connection.execute(
                    "SELECT route_input_id, status, error_code FROM ocr_jobs WHERE world=%s", (world,),
                ).fetchall()
            }
        assert rows["r0"]["status"] == "cancelled" and rows["r0"]["error_code"] == "unsupported_image_format"
        assert rows["r1"]["status"] == "leased"
        assert rows["r2"]["status"] == "queued"
        summary = ocr_jobs.status_summary(world, generation)
        assert summary["failed"] == 0 and summary["counts"]["cancelled"] == 1
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_requeue_with_higher_route_priority_raises_existing_job_priority():
    """ルートを作り直して同じ入力の priority が上がったとき、積み済みの job の priority も上がり、
    下がる向きには動かない（ON CONFLICT GREATEST）。"""
    from sherpa.ingest import ocr_router

    _try_init_real_postgres()
    world = "test-ocr-priority-" + uuid.uuid4().hex
    generation = "a" * 64
    profile = "sha256:" + "e" * 64
    try:
        def manifest(priority: int) -> ocr_router.OCRRouteManifest:
            decision = ocr_router.OCRRouteDecision(
                route_input_id="page-route", target_evidence_id="e-page", input_kind="page_render",
                status="selected", reason_code="scan_page_render_fallback", priority=priority,
                media_type="image/png", page_render={**ocr_router.PAGE_RENDER_PROFILE, "page_1_based": 1},
            )
            return ocr_router.OCRRouteManifest(
                schema_version=ocr_router.OCR_ROUTE_SCHEMA_VERSION,
                router_profile=ocr_router.OCR_ROUTER_PROFILE,
                source_rel_path="doc/scan.pdf", source_content_hash="sha256:" + "b" * 64,
                decisions=[decision], route_manifest_hash="sha256:" + "c" * 64,
            )

        def stored_priority() -> int:
            with ocr_jobs._connect() as connection:
                return connection.execute(
                    "SELECT priority FROM ocr_jobs WHERE world=%s", (world,),
                ).fetchone()["priority"]

        ocr_jobs.enqueue_manifest_jobs(world, manifest(90), canonical_generation_id=generation, engine_profile_hash=profile)
        assert stored_priority() == 90
        ocr_jobs.enqueue_manifest_jobs(
            world, manifest(ocr_router.SCAN_PAGE_PRIORITY), canonical_generation_id=generation,
            engine_profile_hash=profile,
        )
        assert stored_priority() == ocr_router.SCAN_PAGE_PRIORITY
        ocr_jobs.enqueue_manifest_jobs(world, manifest(90), canonical_generation_id=generation, engine_profile_hash=profile)
        assert stored_priority() == ocr_router.SCAN_PAGE_PRIORITY
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def _insert_job_row(
    *, world: str, route_input_id: str, status: str, error_code: str | None = None, attempts: int = 0,
    max_attempts: int = 3,
) -> None:
    leased = status == "leased"
    with ocr_jobs._connect() as connection:
        connection.execute(
            "INSERT INTO ocr_jobs (world, source_rel_path, canonical_generation_id, source_content_hash, "
            "route_manifest_hash, route_input_id, route_input, engine_profile_hash, status, attempts, "
            "max_attempts, error_code, error_detail, lease_owner, lease_token, lease_expires_at, finished_at) "
            "VALUES (%s,'a.xlsx',%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,"
            "CASE WHEN %s THEN now() + interval '1 hour' END, CASE WHEN %s THEN now() END)",
            (
                world, "a" * 64, "sha256:" + "b" * 64, "sha256:" + "c" * 64, route_input_id,
                Json({"route_input_id": route_input_id, "status": "selected"}), "sha256:" + "e" * 64,
                status, attempts, max_attempts, error_code, "detail" if error_code else None,
                "worker" if leased else None, "token" if leased else None, leased, status == "failed",
            ),
        )


def _job_state(world: str) -> dict[str, dict]:
    with ocr_jobs._connect() as connection:
        return {
            str(row["route_input_id"]): row for row in connection.execute(
                "SELECT route_input_id, status, attempts, error_code, error_detail, lease_token, finished_at "
                "FROM ocr_jobs WHERE world=%s", (world,),
            ).fetchall()
        }


def test_requeue_failed_jobs_filters_by_reason_and_world_and_leaves_leased_alone():
    _try_init_real_postgres()
    world = "test-ocr-requeue-" + uuid.uuid4().hex
    other = "test-ocr-requeue-other-" + uuid.uuid4().hex
    try:
        _insert_job_row(world=world, route_input_id="oom", status="failed", error_code="oom", attempts=1)
        _insert_job_row(world=world, route_input_id="timeout", status="failed", error_code="timeout", attempts=3)
        _insert_job_row(world=world, route_input_id="engine", status="failed", error_code="engine_failure", attempts=3)
        _insert_job_row(world=world, route_input_id="leased", status="leased", attempts=2)
        _insert_job_row(world=world, route_input_id="queued", status="queued", attempts=1)
        _insert_job_row(world=world, route_input_id="done", status="succeeded", attempts=1)
        _insert_job_row(world=other, route_input_id="oom", status="failed", error_code="oom", attempts=1)

        assert ocr_jobs.count_failed_jobs(world=world) == {"engine_failure": 1, "oom": 1, "timeout": 1}
        assert ocr_jobs.count_failed_jobs(error_codes=["oom", "timeout"], world=world) == {"oom": 1, "timeout": 1}

        assert ocr_jobs.requeue_failed_jobs(error_codes=["oom", "timeout"], world=world) == 2

        state = _job_state(world)
        for route in ("oom", "timeout"):
            assert state[route]["status"] == "queued" and state[route]["attempts"] == 0
            assert state[route]["error_code"] is None and state[route]["error_detail"] is None
            assert state[route]["finished_at"] is None
        assert state["engine"]["status"] == "failed" and state["engine"]["attempts"] == 3
        assert state["leased"]["status"] == "leased" and state["leased"]["attempts"] == 2
        assert state["leased"]["lease_token"] == "token"
        assert state["queued"]["attempts"] == 1
        assert state["done"]["status"] == "succeeded"
        assert _job_state(other)["oom"]["status"] == "failed"

        assert ocr_jobs.requeue_failed_jobs(world=world) == 1
        assert _job_state(world)["engine"]["status"] == "queued"
        assert _job_state(other)["oom"]["status"] == "failed"
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))
        ocr_jobs.purge_world(other, row=offline_row(other, kind="delete"))


def test_requeued_job_is_leased_again_from_attempt_one():
    _try_init_real_postgres()
    world = "test-ocr-requeue-lease-" + uuid.uuid4().hex
    try:
        _insert_job_row(world=world, route_input_id="r", status="failed", error_code="timeout", attempts=3)
        assert ocr_jobs.lease_next("worker", world=world) is None
        assert ocr_jobs.requeue_failed_jobs(error_codes=["timeout"], world=world) == 1
        job = ocr_jobs.lease_next("worker", world=world)
        assert job is not None and job["attempts"] == 1
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_requeue_failed_jobs_rejects_empty_filters_before_database_access(monkeypatch):
    monkeypatch.setattr(ocr_jobs, "_ensure", lambda: pytest.fail("must not reach the database"))
    for bad in ([], [""], ["oom", "  "]):
        with pytest.raises(ValueError, match="error_codes"):
            ocr_jobs.requeue_failed_jobs(error_codes=bad)
        with pytest.raises(ValueError, match="error_codes"):
            ocr_jobs.count_failed_jobs(error_codes=bad)
    with pytest.raises(ValueError, match="world"):
        ocr_jobs.requeue_failed_jobs(world="  ")


# ---- engine profile が変わったとき（旧 profile の仕事が新 profile の仕事を塞がない・公開に混ざらない）----

_OLD_PROFILE = "sha256:" + "d" * 64
_NEW_PROFILE = "sha256:" + "f" * 64
_GENERATION = "a" * 64


def _insert_profiled_job(
    world: str, route: str, profile: str, *, status: str = "queued", priority: int = 0,
    lease: str | None = None, error_code: str | None = None,
) -> None:
    """`lease` は 'live'（期限内）か 'expired'。status='leased' のときだけ使う。"""
    with ocr_jobs._connect() as connection:
        connection.execute(
            "INSERT INTO ocr_jobs (world, source_rel_path, canonical_generation_id, source_content_hash, "
            "route_manifest_hash, route_input_id, route_input, engine_profile_hash, status, priority, attempts, "
            "error_code, lease_owner, lease_token, lease_expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,1,%s,%s,%s,"
            "CASE WHEN %s='live' THEN now() + interval '1 hour' WHEN %s='expired' THEN now() - interval '1 hour' END)",
            (
                world, "doc/a.png", _GENERATION, "sha256:" + "b" * 64, "sha256:" + "c" * 64, route,
                Json({"route_input_id": route, "status": "selected"}), profile, status, priority, error_code,
                "w" if lease else None, "t-" + route if lease else None, lease, lease,
            ),
        )


def _job_states(world: str) -> dict[str, tuple[str, str | None]]:
    with ocr_jobs._connect() as connection:
        rows = connection.execute(
            "SELECT route_input_id, status, error_code FROM ocr_jobs WHERE world=%s", (world,),
        ).fetchall()
    return {row["route_input_id"]: (row["status"], row["error_code"]) for row in rows}


def test_lease_next_skips_other_profiles_even_when_they_have_higher_priority():
    _try_init_real_postgres()
    world = "test-ocr-lease-profile-" + uuid.uuid4().hex
    try:
        for index in range(5):
            _insert_profiled_job(world, f"old-{index}", _OLD_PROFILE, priority=10)
        _insert_profiled_job(world, "new-0", _NEW_PROFILE, priority=0)

        leased = ocr_jobs.lease_next("worker", lease_seconds=60, world=world, engine_profile_hash=_NEW_PROFILE)

        assert leased is not None and leased["route_input_id"] == "new-0"
        assert ocr_jobs.lease_next("worker", lease_seconds=60, world=world, engine_profile_hash=_NEW_PROFILE) is None
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_old_profile_work_that_becomes_idle_later_is_retired_when_the_current_profile_polls():
    _try_init_real_postgres()
    world = "test-ocr-late-retire-" + uuid.uuid4().hex
    try:
        _insert_profiled_job(world, "old-expired-later", _OLD_PROFILE, status="leased", lease="expired")
        _insert_profiled_job(world, "old-requeued", _OLD_PROFILE)
        with ocr_jobs._connect() as connection:
            connection.execute(
                "INSERT INTO ocr_refresh_runs (world, canonical_generation_id, engine_profile_hash) "
                "VALUES (%s,%s,%s)", (world, "b" * 64, _OLD_PROFILE),
            )

        assert ocr_jobs.lease_next("worker", lease_seconds=60, world=world, engine_profile_hash=_NEW_PROFILE) is None

        stale = ("stale", "stale_profile")
        assert _job_states(world) == {"old-expired-later": stale, "old-requeued": stale}
        with ocr_jobs._connect() as connection:
            assert [r["status"] for r in connection.execute(
                "SELECT status FROM ocr_refresh_runs WHERE world=%s", (world,)).fetchall()] == ["cancelled"]
            connection.execute(
                "INSERT INTO ocr_refresh_runs (world, canonical_generation_id, engine_profile_hash) "
                "VALUES (%s,%s,%s)", (world, "c" * 64, _OLD_PROFILE),
            )

        assert ocr_jobs.lease_refresh_run("worker", lease_seconds=60, world=world, engine_profile_hash=_NEW_PROFILE) is None
        with ocr_jobs._connect() as connection:
            assert [r["status"] for r in connection.execute(
                "SELECT status FROM ocr_refresh_runs WHERE world=%s", (world,)).fetchall()] == ["cancelled", "cancelled"]
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_new_profile_refresh_retires_idle_work_of_old_profiles_without_counting_failures():
    _try_init_real_postgres()
    world = "test-ocr-retire-" + uuid.uuid4().hex
    other_world = "test-ocr-retire-other-" + uuid.uuid4().hex
    try:
        for index in range(3):
            _insert_profiled_job(world, f"old-queued-{index}", _OLD_PROFILE)
        _insert_profiled_job(world, "old-expired", _OLD_PROFILE, status="leased", lease="expired")
        _insert_profiled_job(world, "old-live", _OLD_PROFILE, status="leased", lease="live")
        _insert_profiled_job(world, "old-failed", _OLD_PROFILE, status="failed", error_code="timeout")
        _insert_profiled_job(world, "new-queued", _NEW_PROFILE)
        _insert_profiled_job(other_world, "other-old", _OLD_PROFILE)
        with ocr_jobs._connect() as connection:
            connection.execute(
                "INSERT INTO ocr_refresh_runs (world, canonical_generation_id, engine_profile_hash) "
                "VALUES (%s,%s,%s)", (world, "b" * 64, _OLD_PROFILE),
            )

        run = ocr_jobs.enqueue_refresh_run(world, _GENERATION, _NEW_PROFILE)

        stale = ("stale", "stale_profile")
        assert _job_states(world) == {
            "old-queued-0": stale, "old-queued-1": stale, "old-queued-2": stale, "old-expired": stale,
            "old-live": ("leased", None), "old-failed": ("failed", "timeout"), "new-queued": ("queued", None),
        }
        assert _job_states(other_world) == {"other-old": ("queued", None)}      # 他の資料フォルダには触れない
        assert run["status"] == "queued" and run["engine_profile_hash"] == _NEW_PROFILE
        with ocr_jobs._connect() as connection:
            old_run = connection.execute(
                "SELECT status, error_code FROM ocr_refresh_runs WHERE world=%s AND engine_profile_hash=%s",
                (world, _OLD_PROFILE),
            ).fetchone()
        assert (old_run["status"], old_run["error_code"]) == ("cancelled", "stale_profile")
        summary = ocr_jobs.status_summary(world)
        assert summary["failed"] == 1 and summary["counts"]["stale"] == 4 and summary["pending"] == 2
        leased = ocr_jobs.lease_next("worker", lease_seconds=60, world=world, engine_profile_hash=_NEW_PROFILE)
        assert leased is not None and leased["route_input_id"] == "new-queued"
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))
        ocr_jobs.purge_world(other_world, row=offline_row(other_world, kind="delete"))


def test_retire_other_profile_work_keeps_the_current_profile_and_reports_counts():
    _try_init_real_postgres()
    world = "test-ocr-retire-fn-" + uuid.uuid4().hex
    try:
        _insert_profiled_job(world, "old", _OLD_PROFILE)
        _insert_profiled_job(world, "new", _NEW_PROFILE)
        with ocr_jobs._connect() as connection:
            connection.execute(
                "INSERT INTO ocr_refresh_runs (world, canonical_generation_id, engine_profile_hash) "
                "VALUES (%s,%s,%s)", (world, _GENERATION, _OLD_PROFILE),
            )

        assert ocr_jobs.retire_other_profile_work(_NEW_PROFILE, world=world) == {"jobs": 1, "refresh_runs": 1}
        assert ocr_jobs.retire_other_profile_work(_NEW_PROFILE, world=world) == {"jobs": 0, "refresh_runs": 0}
        assert _job_states(world) == {"old": ("stale", "stale_profile"), "new": ("queued", None)}
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_publication_state_counts_only_the_current_profile():
    _try_init_real_postgres()
    world = "test-ocr-state-profile-" + uuid.uuid4().hex
    try:
        _insert_succeeded_row(
            world=world, source_rel_path="doc/a.png", canonical_generation_id=_GENERATION, route_input_id="r0",
        )   # 旧 profile（"e" * 64）の成功結果だけがある
        old_profile = "sha256:" + "e" * 64

        assert ocr_jobs.generation_state(world, _GENERATION, engine_profile_hash=old_profile)["unpublished_jobs"] == 1
        state = ocr_jobs.generation_state(world, _GENERATION, engine_profile_hash=_NEW_PROFILE)
        assert state["unpublished_jobs"] == 0 and state["terminal"] is True
        assert not ocr_jobs.generation_ready_for_publication(world, _GENERATION, engine_profile_hash=_NEW_PROFILE)
        assert ocr_jobs.succeeded_results_snapshot(
            world, _GENERATION, engine_profile_hash=_NEW_PROFILE)["row_count"] == 0
        assert list(ocr_jobs.iter_succeeded_results(
            world, _GENERATION, observation_render._relative_source_path, engine_profile_hash=_NEW_PROFILE)) == []
        assert world not in {
            item["world"] for item in ocr_jobs.list_unpublished_generations(
                limit=1000, engine_profile_hash=_NEW_PROFILE)
        }
        assert world in {
            item["world"] for item in ocr_jobs.list_unpublished_generations(
                limit=1000, engine_profile_hash=old_profile)
        }
        snapshot = ocr_jobs.succeeded_results_snapshot(world, _GENERATION, engine_profile_hash=_NEW_PROFILE)
        assert ocr_jobs.mark_snapshot_artifacts_published(
            world, _GENERATION, snapshot, engine_profile_hash=_NEW_PROFILE) == 0
        assert ocr_jobs.generation_state(world, _GENERATION, engine_profile_hash=old_profile)["unpublished_jobs"] == 1
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_retiring_other_profiles_locks_refresh_runs_before_jobs_like_purge_world():
    executed: list[str] = []

    class _Result:
        rowcount = 0

    class _Connection:
        def execute(self, sql, params=None):
            executed.append(sql)
            return _Result()

    ocr_jobs._retire_other_profile_work(_Connection(), _NEW_PROFILE, None)

    assert [sql.split()[1] for sql in executed] == ["ocr_refresh_runs", "ocr_jobs"]


def test_old_profile_work_is_found_through_the_active_profile_index():
    _try_init_real_postgres()
    with ocr_jobs._connect() as connection:
        names = {row["indexname"] for row in connection.execute(
            "SELECT indexname FROM pg_indexes WHERE tablename IN ('ocr_jobs','ocr_refresh_runs')").fetchall()}
        connection.execute("SET LOCAL enable_seqscan = off")
        plan = "\n".join(row["QUERY PLAN"] for row in connection.execute(
            "EXPLAIN SELECT id FROM ocr_jobs WHERE (engine_profile_hash<%s OR engine_profile_hash>%s) "
            "AND status IN ('queued','leased')", (_NEW_PROFILE, _NEW_PROFILE)).fetchall())
    assert {"ocr_jobs_active_profile", "ocr_refresh_runs_active_profile"} <= names
    assert "ocr_jobs_active_profile" in plan
