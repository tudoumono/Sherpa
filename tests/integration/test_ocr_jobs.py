from __future__ import annotations

import uuid

import pytest
from psycopg.types.json import Json

from sherpa import store
from sherpa.ingest import observation_render
from sherpa.store import ocr_jobs
from tests._replace_row import offline_row


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


def _enqueue(**fields):
    ocr_jobs._ensure()
    with ocr_jobs._connect() as connection:
        return ocr_jobs._insert_job(connection, ocr_jobs._job_values(**fields), 0, 3)


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
        first = _enqueue(**common)
        duplicate = _enqueue(**common)
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
        cache_first = ocr_jobs.put_cached_result_for_lease(
            leased_again["id"], leased_again["lease_token"], world, "sha256:" + "1" * 64, "sha256:" + "e" * 64,
            {"schema": "ocr-engine-lines-v1", "observations": [{"text": "first"}]},
        )
        cache_second = ocr_jobs.put_cached_result_for_lease(
            leased_again["id"], leased_again["lease_token"], world, "sha256:" + "1" * 64, "sha256:" + "e" * 64,
            {"schema": "ocr-engine-lines-v1", "observations": [{"text": "second"}]},
        )
        assert cache_first["result_hash"] == cache_second["result_hash"]
        assert cache_second["result_payload"]["observations"][0]["text"] == "first"
        completed = ocr_jobs.complete_job(
            leased_again["id"], leased_again["lease_token"], observation_set_hash="sha256:" + "f" * 64,
            result_payload={"schema_version": "ai-observation-set-v1alpha2"},
        )
        assert completed["status"] == "succeeded"

        summary = ocr_jobs.status_summary(world, generation)
        assert summary["counts"]["succeeded"] == 1
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_identical_content_at_different_paths_keeps_one_job_per_path():
    _init_or_skip()
    world = "test-ocr-copies-" + uuid.uuid4().hex
    common = {
        "world": world,
        "canonical_generation_id": "a" * 64,
        "source_content_hash": "sha256:" + "b" * 64,
        "route_manifest_hash": "sha256:" + "c" * 64,
        "route_input": _route("route-1"),
        "engine_profile_hash": "sha256:" + "e" * 64,
    }
    try:
        first = _enqueue(source_rel_path="a/design.xlsx", **common)
        copy = _enqueue(source_rel_path="b/design.xlsx", **common)
        again = _enqueue(source_rel_path="a/design.xlsx", **common)

        assert copy["id"] != first["id"] and copy["source_rel_path"] == "b/design.xlsx"
        assert again["id"] == first["id"]
        assert ocr_jobs.status_summary(world, common["canonical_generation_id"])["counts"]["queued"] == 2
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def _legacy_unique_constraints(connection) -> list[str]:
    return [row["conname"] for row in connection.execute(
        "SELECT con.conname FROM pg_constraint con WHERE con.conrelid = 'ocr_jobs'::regclass AND con.contype = 'u' "
        "AND (SELECT array_agg(att.attname::text ORDER BY att.attname) FROM unnest(con.conkey) AS key(attnum) "
        "JOIN pg_attribute att ON att.attrelid = con.conrelid AND att.attnum = key.attnum) "
        "= ARRAY['canonical_generation_id','engine_profile_hash','route_input_id','world']"
    ).fetchall()]


def test_schema_migrates_the_legacy_unique_key_and_is_idempotent():
    _init_or_skip()
    world = "test-ocr-migrate-" + uuid.uuid4().hex
    common = {
        "world": world,
        "canonical_generation_id": "a" * 64,
        "source_content_hash": "sha256:" + "b" * 64,
        "route_manifest_hash": "sha256:" + "c" * 64,
        "route_input": _route("route-1"),
        "engine_profile_hash": "sha256:" + "e" * 64,
    }
    try:
        with ocr_jobs._connect() as connection:
            connection.execute("ALTER TABLE ocr_jobs ADD UNIQUE (world, canonical_generation_id, route_input_id, engine_profile_hash)")
            assert len(_legacy_unique_constraints(connection)) == 1     # 旧い環境と同じ自動名の制約
        _enqueue(source_rel_path="a/design.xlsx", **common)

        with ocr_jobs._connect() as connection:
            for _ in range(2):
                for statement in (s for s in store.db._SCHEMA if "ocr_jobs" in s):
                    connection.execute(statement)

        with ocr_jobs._connect() as connection:
            assert _legacy_unique_constraints(connection) == []
            assert connection.execute(
                "SELECT 1 FROM pg_indexes WHERE tablename='ocr_jobs' AND indexname='ocr_jobs_dispatch_key'"
            ).fetchone() is not None
        copy = _enqueue(source_rel_path="b/design.xlsx", **common)
        assert copy["source_rel_path"] == "b/design.xlsx"
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


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
        assert (completed["manifests_processed"], completed["selected_count"], completed["jobs_enqueued"]) == (1, 2, 2)
    finally:
        removed = ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))
        assert removed["refresh_runs"] == 1


def test_refresh_run_pause_repeated_does_not_consume_attempts_and_keeps_cursor():
    """切による停止を何度繰り返しても再試行回数を使い切らず、cursor を保ったまま残りを再開できる。"""
    _init_or_skip()
    world = "test-ocr-pause-" + uuid.uuid4().hex
    generation, profile = "a" * 64, "sha256:" + "e" * 64
    try:
        queued = ocr_jobs.enqueue_refresh_run(world, generation, profile)
        for _ in range(queued["max_attempts"] + 2):
            leased = ocr_jobs.lease_refresh_run("worker-a", lease_seconds=60, world=world)
            assert leased is not None and leased["id"] == queued["id"]
            assert ocr_jobs.update_refresh_run_progress(
                leased["id"], leased["lease_token"], cursor_rel_path="sub/a.xlsx.ocr_route.json",
                selected_delta=1, excluded_delta=0, failed_binding_delta=0, jobs_delta=1, lease_seconds=60,
            ) is True
            paused = ocr_jobs.pause_refresh_run(
                leased["id"], leased["lease_token"], error_code="ocr_stopping", error_detail="switched off")
            assert paused["status"] == "queued" and paused["attempts"] == 0
            assert paused["cursor_rel_path"] == "sub/a.xlsx.ocr_route.json" and paused["finished_at"] is None
        resumed = ocr_jobs.lease_refresh_run("worker-a", lease_seconds=60, world=world)
        assert resumed["cursor_rel_path"] == "sub/a.xlsx.ocr_route.json"
        assert ocr_jobs.complete_refresh_run(resumed["id"], resumed["lease_token"])["status"] == "completed"
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_refresh_run_pause_after_lease_expired_still_returns_the_attempt():
    """lease の期限が切れた直後の停止でも再試行回数を戻す（繰り返しても failed にならない）。"""
    _init_or_skip()
    world = "test-ocr-pause-expired-" + uuid.uuid4().hex
    generation, profile = "a" * 64, "sha256:" + "e" * 64
    try:
        queued = ocr_jobs.enqueue_refresh_run(world, generation, profile)
        for _ in range(queued["max_attempts"] + 2):
            leased = ocr_jobs.lease_refresh_run("worker-a", lease_seconds=60, world=world)
            assert leased is not None
            with ocr_jobs._connect() as connection:
                connection.execute(
                    "UPDATE ocr_refresh_runs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (leased["id"],))
            paused = ocr_jobs.pause_refresh_run(
                leased["id"], leased["lease_token"], error_code="ocr_stopping", error_detail="switched off")
            assert paused["status"] == "queued" and paused["attempts"] == 0
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_succeeded_results_stream_and_snapshot_mark_use_real_postgres():
    _init_or_skip()
    world = "test-ocr-publish-" + uuid.uuid4().hex
    generation = "a" * 64
    try:
        for index, source_rel_path in enumerate(("a/large.pdf", "b/image.png"), start=1):
            _enqueue(
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

        rows = list(ocr_jobs.iter_succeeded_results(
            world, generation, observation_render._relative_source_path, batch_size=1,
        ))
        assert [row["source_rel_path"] for row in rows] == ["a/large.pdf", "b/image.png"]
        snapshot = ocr_jobs.succeeded_results_snapshot(world, generation)
        assert snapshot["row_count"] == 2
        assert ocr_jobs.mark_snapshot_artifacts_published(world, generation, snapshot) == 2
        assert all(row["artifact_published"] for row in ocr_jobs.iter_succeeded_results(
            world, generation, observation_render._relative_source_path))
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_publication_ignores_work_that_cannot_progress_from_persisted_state():
    _init_or_skip()
    world = "test-ocr-terminal-" + uuid.uuid4().hex
    generation = "a" * 64
    profile = "sha256:" + "e" * 64
    try:
        succeeded = _enqueue(
            world=world, source_rel_path="done.png", canonical_generation_id=generation,
            source_content_hash="sha256:" + "b" * 64, route_manifest_hash="sha256:" + "c" * 64,
            route_input=_route("done"), engine_profile_hash=profile,
        )
        dead = _enqueue(
            world=world, source_rel_path="dead.png", canonical_generation_id=generation,
            source_content_hash="sha256:" + "b" * 64, route_manifest_hash="sha256:" + "c" * 64,
            route_input=_route("dead"), engine_profile_hash=profile,
        )
        _enqueue(
            world=world, source_rel_path="old.png", canonical_generation_id=generation,
            source_content_hash="sha256:" + "b" * 64, route_manifest_hash="sha256:" + "c" * 64,
            route_input=_route("old"), engine_profile_hash="sha256:" + "d" * 64,
        )
        with ocr_jobs._connect() as connection:
            connection.execute(
                "UPDATE ocr_jobs SET status='succeeded', result_observation_set_hash=%s, result_payload=%s "
                "WHERE id=%s",
                ("sha256:" + "f" * 64, Json({"schema_version": "ai-observation-set-v1alpha2"}), succeeded["id"]),
            )
            connection.execute(
                "UPDATE ocr_jobs SET status='leased', attempts=max_attempts, lease_owner='gone', "
                "lease_token='expired', lease_expires_at=now()-interval '1 minute' WHERE id=%s",
                (dead["id"],),
            )

        state = ocr_jobs.generation_state(world, generation, engine_profile_hash=profile)
        assert state == {
            "terminal": True, "pending_jobs": 0, "pending_refresh_runs": 0, "unpublished_jobs": 1,
        }
        assert ocr_jobs.generation_ready_for_publication(world, generation, engine_profile_hash=profile)
        assert world in {
            row["world"] for row in ocr_jobs.list_unpublished_generations(engine_profile_hash=profile)
        }
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_publication_waits_while_queued_or_leased_work_can_progress():
    _init_or_skip()
    world = "test-ocr-progressable-" + uuid.uuid4().hex
    generation = "a" * 64
    profile = "sha256:" + "e" * 64
    try:
        succeeded = _enqueue(
            world=world, source_rel_path="done.png", canonical_generation_id=generation,
            source_content_hash="sha256:" + "b" * 64, route_manifest_hash="sha256:" + "c" * 64,
            route_input=_route("done"), engine_profile_hash=profile,
        )
        pending = _enqueue(
            world=world, source_rel_path="pending.png", canonical_generation_id=generation,
            source_content_hash="sha256:" + "b" * 64, route_manifest_hash="sha256:" + "c" * 64,
            route_input=_route("pending"), engine_profile_hash=profile,
        )
        with ocr_jobs._connect() as connection:
            connection.execute(
                "UPDATE ocr_jobs SET status='succeeded', result_observation_set_hash=%s, result_payload=%s "
                "WHERE id=%s",
                ("sha256:" + "f" * 64, Json({"schema_version": "ai-observation-set-v1alpha2"}), succeeded["id"]),
            )

        assert not ocr_jobs.generation_ready_for_publication(world, generation, engine_profile_hash=profile)
        with ocr_jobs._connect() as connection:
            connection.execute(
                "UPDATE ocr_jobs SET status='leased', attempts=max_attempts, lease_owner='live', "
                "lease_token='live', lease_expires_at=now()+interval '1 minute' WHERE id=%s",
                (pending["id"],),
            )
        assert not ocr_jobs.generation_ready_for_publication(world, generation, engine_profile_hash=profile)
        with ocr_jobs._connect() as connection:
            connection.execute(
                "UPDATE ocr_jobs SET attempts=1, lease_expires_at=now()-interval '1 minute' WHERE id=%s",
                (pending["id"],),
            )
        assert not ocr_jobs.generation_ready_for_publication(world, generation, engine_profile_hash=profile)
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def _world_rows(world: str) -> int:
    with ocr_jobs._connect() as c:
        return sum(c.execute(f"SELECT count(*) AS n FROM {t} WHERE world=%s", (world,)).fetchone()["n"]
                   for t in ("ocr_jobs", "ocr_result_cache", "ocr_refresh_runs"))


def _enqueue_one(world: str) -> None:
    _enqueue(
        world=world, source_rel_path="sub/design.xlsx", canonical_generation_id="a" * 64,
        source_content_hash="sha256:" + "b" * 64, route_manifest_hash="sha256:" + "c" * 64,
        route_input=_route("route-1"), engine_profile_hash="sha256:" + "e" * 64,
    )
    leased = ocr_jobs.lease_next("worker", lease_seconds=60, world=world)
    ocr_jobs.put_cached_result_for_lease(
        leased["id"], leased["lease_token"], world, "sha256:" + "1" * 64, "sha256:" + "e" * 64,
        {"schema": "ocr-engine-lines-v1", "observations": [{"text": "secret"}]},
    )


def test_world_wipe_removes_ocr_rows_and_failure_keeps_them_for_retry(monkeypatch, tmp_path):
    """資料フォルダの削除（wipe）で OCR の本文（job/cache）も消える。消去に失敗したら例外で止まり行は残る。"""
    from sherpa.ingest import wipe

    _init_or_skip()
    world = "test-ocr-wipe-" + uuid.uuid4().hex
    monkeypatch.setenv("SHERPA_OBSERVATION_DIR", str(tmp_path / "obs"))
    obs = tmp_path / "obs" / world
    obs.mkdir(parents=True)
    (obs / ".ai_observations.jsonl").write_text("secret", encoding="utf-8")
    try:
        _enqueue_one(world)
        import shutil
        real_rmtree = shutil.rmtree

        def _rm_boom(*_a, **_k):
            raise OSError("rm fault")

        monkeypatch.setattr(shutil, "rmtree", _rm_boom)       # 観測の削除が失敗したら OCR の行は残る（再試行できる）
        with pytest.raises(OSError, match="rm fault"):
            wipe.wipe_locked(world, reflect=False, row=offline_row(world, kind="delete"))
        monkeypatch.setattr(shutil, "rmtree", real_rmtree)
        assert _world_rows(world)
        assert obs.exists()

        real_purge = ocr_jobs.purge_world

        def _boom(_world, *, row):
            raise RuntimeError("pg fault")

        monkeypatch.setattr(ocr_jobs, "purge_world", _boom)
        with pytest.raises(RuntimeError, match="pg fault"):
            wipe.wipe_locked(world, reflect=False, row=offline_row(world, kind="delete"))
        assert _world_rows(world)

        monkeypatch.setattr(ocr_jobs, "purge_world", real_purge)
        obs.mkdir(exist_ok=True)
        wipe.wipe_locked(world, reflect=False, row=offline_row(world, kind="delete"))
        assert not _world_rows(world)
        assert not obs.exists()
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_refresh_enqueue_after_world_purge_leaves_no_job():
    """削除（purge_world）の後に refresh が job を積もうとしても、run 行が無いので何も残らない。"""
    from types import SimpleNamespace

    _init_or_skip()
    world = "test-ocr-race-" + uuid.uuid4().hex
    gen, profile = "a" * 64, "sha256:" + "e" * 64
    manifest = SimpleNamespace(
        source_rel_path="a.xlsx", source_content_hash="sha256:" + "b" * 64,
        route_manifest_hash="sha256:" + "c" * 64, decisions=[],
    )
    try:
        ocr_jobs.enqueue_refresh_run(world, gen, profile)
        run = ocr_jobs.lease_refresh_run("w", lease_seconds=60, world=world)
        with ocr_jobs._connect() as c:
            c.execute("UPDATE ocr_refresh_runs SET lease_expires_at=now()-interval '1 second' WHERE id=%s", (run["id"],))
        assert ocr_jobs.enqueue_manifest_jobs(                                    # 期限切れの lease では積まない
            world, manifest, canonical_generation_id=gen, engine_profile_hash=profile,
            refresh_run=(run["id"], run["lease_token"])) is None
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))
        assert ocr_jobs.enqueue_manifest_jobs(
            world, manifest, canonical_generation_id=gen, engine_profile_hash=profile,
            refresh_run=(run["id"], run["lease_token"])) is None
        assert not _world_rows(world)
    finally:
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_world_wipe_refuses_overlapping_observation_dir_before_deleting_anything(monkeypatch, tmp_path):
    """観測の置き場が登録 root（読み取り専用の原本）と重なる設定（別の綴り＝symlink 別名・祖先を含む）では、
    削除は何も消さずに止まり、原本も OCR の行も残る。"""
    from sherpa.ingest import wipe

    _init_or_skip()
    world = "ocroverlap" + uuid.uuid4().hex[:8]
    obs_base = tmp_path / "obs"
    obs_base.mkdir()
    target = obs_base / world                         # 観測の置き場 {base}/{world}
    target.mkdir()
    (target / "original.txt").write_text("source", encoding="utf-8")
    alias = tmp_path / "alias"
    alias.symlink_to(target)                          # 同じ実体の別の綴り（大文字小文字違いの FS と同じ同一性の問題）
    parent_alias = tmp_path / "parent-alias"
    parent_alias.symlink_to(tmp_path)                 # 観測の置き場の祖先を指す登録 root
    monkeypatch.setenv("SHERPA_OBSERVATION_DIR", str(obs_base))
    try:
        _enqueue_one(world)
        for root in (alias, parent_alias):
            store.upsert_world(world, str(root))
            with pytest.raises(ValueError):
                wipe.wipe_locked(world, reflect=False, row=offline_row(world, kind="delete"))
            assert (target / "original.txt").read_text(encoding="utf-8") == "source"
            assert _world_rows(world)                 # 検証は何かを消す前＝OCR の行も残る
        not_dir = obs_base / (world + "f")             # 通常のファイルは対象にできない（何も消さずに拒否）
        not_dir.write_text("x", encoding="utf-8")
        store.upsert_world(world + "f", str(tmp_path / "elsewhere"))
        with pytest.raises(ValueError):
            wipe.wipe_locked(world + "f", reflect=False, row=offline_row(world + "f", kind="delete"))
        assert not_dir.exists()
    finally:
        store.delete_world_row(world)
        store.delete_world_row(world + "f")
        ocr_jobs.purge_world(world, row=offline_row(world, kind="delete"))


def test_ocr_catchup_marker_ignores_runs_before_the_latest_register():
    _init_or_skip()
    from sherpa.store import ingest as ingest_store

    world = f"ocrcatchup-{uuid.uuid4().hex[:8]}"
    obs = "gen-a/obs-1"

    def _run(op, payload, status="failed"):
        _, row = ingest_store.enqueue_ingest_run(world, op, payload, f"fp-{uuid.uuid4().hex}", "test",
                                                 keys=(world,))
        with ingest_store._connect() as c:
            c.execute("UPDATE ingest_runs SET status='extracting' WHERE id=%s", (row["id"],))
        ingest_store.finish_ingest_run(row["id"], status=status)

    try:
        _run("register", {}, "auto_published")
        _run("refresh", {"ocr_catchup": obs})
        assert ingest_store.has_ocr_catchup_run(world, obs)
        _run("rebind", {})                                   # 失敗した参照先の変更は区切りにしない
        assert ingest_store.has_ocr_catchup_run(world, obs)
        _run("register", {}, "auto_published")
        assert not ingest_store.has_ocr_catchup_run(world, obs)
        _run("refresh", {"ocr_catchup": obs})
        assert ingest_store.has_ocr_catchup_run(world, obs)
    finally:
        with store._connect() as c:
            c.execute("DELETE FROM ingest_runs WHERE version=%s", (world,))
