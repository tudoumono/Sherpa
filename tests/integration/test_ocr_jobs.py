from __future__ import annotations

import uuid

import pytest

from sherpa import store
from sherpa.store import ocr_jobs


def _init_or_skip():
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")


def _route(route_input_id: str) -> dict:
    return {
        "route_input_id": route_input_id,
        "target_evidence_id": "picture-1",
        "input_kind": "asset",
        "status": "selected",
        "reason_code": "evidence_raster_asset",
        "priority": 100,
        "asset_sha256": "sha256:" + "d" * 64,
        "asset_rel_path": "asset.png",
        "media_type": "image/png",
    }


def test_ocr_job_lease_token_idempotence_retry_and_world_cache():
    _init_or_skip()
    world = "test-ocr-" + uuid.uuid4().hex
    generation = "a" * 64
    common = {
        "world": world,
        "source_rel_path": "sub/design.xlsx",
        "canonical_generation_id": generation,
        "source_content_hash": "sha256:" + "b" * 64,
        "route_manifest_hash": "sha256:" + "c" * 64,
        "route_input": _route("route-1"),
        "engine_profile_hash": "sha256:" + "e" * 64,
    }
    try:
        first = ocr_jobs.enqueue_job(**common)
        duplicate = ocr_jobs.enqueue_job(**common)
        assert duplicate["id"] == first["id"]

        leased = ocr_jobs.lease_next("worker-a", lease_seconds=60, world=world)
        assert leased["status"] == "leased" and leased["attempts"] == 1
        assert ocr_jobs.complete_job(
            leased["id"], "wrong-token", observation_set_hash="sha256:" + "f" * 64, result_payload={},
        ) is None

        retried = ocr_jobs.fail_job(
            leased["id"], leased["lease_token"], error_code="timeout", retryable=True, retry_delay_seconds=0,
        )
        assert retried["status"] == "queued"
        leased_again = ocr_jobs.lease_next("worker-b", lease_seconds=60, world=world)
        assert leased_again["attempts"] == 2 and leased_again["lease_token"] != leased["lease_token"]
        completed = ocr_jobs.complete_job(
            leased_again["id"], leased_again["lease_token"], observation_set_hash="sha256:" + "f" * 64,
            result_payload={"schema_version": "ai-observation-set-v1alpha2"},
        )
        assert completed["status"] == "succeeded"

        cache_first = ocr_jobs.put_cached_result(
            world, "sha256:" + "1" * 64, "sha256:" + "e" * 64,
            {"schema": "ocr-engine-lines-v1", "observations": [{"text": "first"}]},
        )
        cache_second = ocr_jobs.put_cached_result(
            world, "sha256:" + "1" * 64, "sha256:" + "e" * 64,
            {"schema": "ocr-engine-lines-v1", "observations": [{"text": "second"}]},
        )
        assert cache_first["result_hash"] == cache_second["result_hash"]
        assert cache_second["result_payload"]["observations"][0]["text"] == "first"

        summary = ocr_jobs.status_summary(world, generation)
        assert summary["counts"]["succeeded"] == 1
    finally:
        ocr_jobs.purge_world(world)


def test_ocr_refresh_run_lease_progress_and_world_purge():
    _init_or_skip()
    world = "test-ocr-refresh-" + uuid.uuid4().hex
    generation = "a" * 64
    profile = "sha256:" + "e" * 64
    try:
        queued = ocr_jobs.enqueue_refresh_run(world, generation, profile)
        duplicate = ocr_jobs.enqueue_refresh_run(world, generation, profile)
        assert duplicate["id"] == queued["id"]

        leased = ocr_jobs.lease_refresh_run("worker-a", lease_seconds=60, world=world)
        assert leased["id"] == queued["id"] and leased["status"] == "leased"
        assert ocr_jobs.update_refresh_run_progress(
            leased["id"], leased["lease_token"], cursor_rel_path="sub/a.xlsx.ocr_route.json",
            selected_delta=2, excluded_delta=1, failed_binding_delta=0, jobs_delta=2, lease_seconds=60,
        ) is True
        completed = ocr_jobs.complete_refresh_run(leased["id"], leased["lease_token"])
        assert completed["status"] == "completed"
        summary = ocr_jobs.refresh_run_summary(world, generation)
        assert summary["manifests"] == 1 and summary["selected"] == 2 and summary["jobs"] == 2
    finally:
        removed = ocr_jobs.purge_world(world)
        assert removed["refresh_runs"] == 1


def test_succeeded_results_stream_and_snapshot_mark_use_real_postgres():
    _init_or_skip()
    world = "test-ocr-publish-" + uuid.uuid4().hex
    generation = "a" * 64
    try:
        for index, source_rel_path in enumerate(("a/large.pdf", "b/image.png"), start=1):
            ocr_jobs.enqueue_job(
                world=world,
                source_rel_path=source_rel_path,
                canonical_generation_id=generation,
                source_content_hash="sha256:" + "b" * 64,
                route_manifest_hash="sha256:" + "c" * 64,
                route_input=_route(f"route-{index}"),
                engine_profile_hash="sha256:" + "e" * 64,
            )
            leased = ocr_jobs.lease_next(f"worker-{index}", lease_seconds=60, world=world)
            assert leased is not None
            completed = ocr_jobs.complete_job(
                leased["id"], leased["lease_token"],
                observation_set_hash="sha256:" + str(index) * 64,
                result_payload={"schema_version": "ai-observation-set-v1alpha2", "index": index},
            )
            assert completed is not None

        rows = list(ocr_jobs.iter_succeeded_results(world, generation, batch_size=1))
        assert [row["source_rel_path"] for row in rows] == ["a/large.pdf", "b/image.png"]
        snapshot = ocr_jobs.succeeded_results_snapshot(world, generation)
        assert snapshot["row_count"] == 2
        assert ocr_jobs.mark_snapshot_artifacts_published(world, generation, snapshot) == 2
        assert all(row["artifact_published"] for row in ocr_jobs.list_succeeded_results(world, generation))
    finally:
        ocr_jobs.purge_world(world)
