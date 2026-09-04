"""OCR補助観測worker用のPostgreSQL lease queueとWorld内結果cache。"""
from __future__ import annotations

import hashlib
import json
import re
import uuid
from dataclasses import asdict
from pathlib import PurePosixPath
from collections.abc import Iterator
from typing import Any

from psycopg.types.json import Json

from .db import _connect, _ensure


_SHA256_RE = re.compile(r"^(?:sha256:)?([0-9a-f]{64})$")
_GENERATION_RE = re.compile(r"^[0-9a-f]{64}$")


def _tagged_hash(value: str) -> str:
    match = _SHA256_RE.fullmatch(value.strip().lower())
    if match is None:
        raise ValueError("invalid sha256")
    return "sha256:" + match.group(1)


def _relative_path(value: str) -> str:
    path = PurePosixPath(value.replace("\\", "/"))
    if path.is_absolute() or not path.parts or any(part in {"", ".", ".."} for part in path.parts):
        raise ValueError("source path must be relative")
    return path.as_posix()


def _generation_id(value: str) -> str:
    generation = value.strip().lower()
    if _GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("invalid canonical generation id")
    return generation


def _world_id(value: str) -> str:
    world = value.strip()
    if not world:
        raise ValueError("world is required")
    return world


def _job_values(
    *,
    world: str,
    source_rel_path: str,
    canonical_generation_id: str,
    source_content_hash: str,
    route_manifest_hash: str,
    route_input: dict[str, Any],
    engine_profile_hash: str,
) -> tuple[str, str, str, str, str, str, dict[str, Any], str]:
    if not world.strip():
        raise ValueError("world is required")
    generation = canonical_generation_id.strip().lower()
    if _GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("invalid canonical generation id")
    if not isinstance(route_input, dict) or route_input.get("status") != "selected":
        raise ValueError("only selected OCR route inputs can be enqueued")
    route_input_id = str(route_input.get("route_input_id") or "")
    if not route_input_id:
        raise ValueError("route_input_id is required")
    if route_input.get("input_kind") == "asset":
        _relative_path(str(route_input.get("asset_rel_path") or ""))
    elif route_input.get("input_kind") == "page_render":
        render = route_input.get("page_render")
        if not isinstance(render, dict) or not isinstance(render.get("page_1_based"), int):
            raise ValueError("page_render contract is required")
    else:
        raise ValueError("invalid OCR input kind")
    return (
        world.strip(), _relative_path(source_rel_path), generation, _tagged_hash(source_content_hash),
        _tagged_hash(route_manifest_hash), route_input_id, route_input, _tagged_hash(engine_profile_hash),
    )


def enqueue_job(
    *,
    world: str,
    source_rel_path: str,
    canonical_generation_id: str,
    source_content_hash: str,
    route_manifest_hash: str,
    route_input: dict[str, Any],
    engine_profile_hash: str,
    priority: int = 0,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """同じgeneration/input/profileを1回だけenqueueする。成功済み行を再queueしない。"""
    if max_attempts <= 0 or priority < 0:
        raise ValueError("invalid OCR job limits")
    values = _job_values(
        world=world, source_rel_path=source_rel_path, canonical_generation_id=canonical_generation_id,
        source_content_hash=source_content_hash, route_manifest_hash=route_manifest_hash,
        route_input=route_input, engine_profile_hash=engine_profile_hash,
    )
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "INSERT INTO ocr_jobs (world, source_rel_path, canonical_generation_id, source_content_hash, "
            "route_manifest_hash, route_input_id, route_input, engine_profile_hash, priority, max_attempts) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (world, canonical_generation_id, route_input_id, engine_profile_hash) DO UPDATE SET "
            "priority=GREATEST(ocr_jobs.priority, EXCLUDED.priority), updated_at=now() "
            "RETURNING *",
            (*values[:6], Json(values[6]), values[7], priority, max_attempts),
        ).fetchone()


def enqueue_manifest_jobs(
    world: str,
    manifest: Any,
    *,
    canonical_generation_id: str,
    engine_profile_hash: str,
    max_attempts: int = 3,
) -> list[dict]:
    """OCRRouteManifestのselected入力だけを冪等enqueueする。"""
    jobs = []
    for decision in manifest.decisions:
        if decision.status != "selected":
            continue
        jobs.append(enqueue_job(
            world=world, source_rel_path=manifest.source_rel_path,
            canonical_generation_id=canonical_generation_id,
            source_content_hash=manifest.source_content_hash,
            route_manifest_hash=manifest.route_manifest_hash,
            route_input=asdict(decision), engine_profile_hash=engine_profile_hash,
            priority=decision.priority, max_attempts=max_attempts,
        ))
    return jobs


def enqueue_refresh_run(
    world: str,
    canonical_generation_id: str,
    engine_profile_hash: str,
    *,
    force: bool = False,
    max_attempts: int = 3,
) -> dict[str, Any]:
    """route manifest展開をworkerへ委譲する小さな永続runを冪等enqueueする。

    通常の重複呼出しは既存runをそのまま返す。明示的な``force``はterminal runだけを先頭から
    再queueし、処理中runのleaseやcursorは奪わない。
    """
    selected_world = _world_id(world)
    generation = _generation_id(canonical_generation_id)
    profile = _tagged_hash(engine_profile_hash)
    if max_attempts <= 0:
        raise ValueError("max_attempts must be positive")
    _ensure()
    with _connect() as connection:
        inserted = connection.execute(
            "INSERT INTO ocr_refresh_runs (world, canonical_generation_id, engine_profile_hash, max_attempts) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING *",
            (selected_world, generation, profile, max_attempts),
        ).fetchone()
        if inserted is not None:
            return inserted
        existing = connection.execute(
            "SELECT * FROM ocr_refresh_runs WHERE world=%s AND canonical_generation_id=%s "
            "AND engine_profile_hash=%s FOR UPDATE",
            (selected_world, generation, profile),
        ).fetchone()
        if existing is None:  # pragma: no cover - unique row disappeared inside one transaction
            raise RuntimeError("OCR refresh run disappeared")
        if not force or existing["status"] not in {"completed", "failed", "cancelled"}:
            return existing
        return connection.execute(
            "UPDATE ocr_refresh_runs SET status='queued', attempts=0, max_attempts=%s, cursor_rel_path=NULL, "
            "manifests_processed=0, selected_count=0, excluded_count=0, failed_binding_count=0, jobs_enqueued=0, "
            "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, error_code=NULL, error_detail=NULL, "
            "finished_at=NULL, updated_at=now() WHERE id=%s RETURNING *",
            (max_attempts, existing["id"]),
        ).fetchone()


def lease_refresh_run(
    worker_id: str,
    *,
    lease_seconds: int = 90,
    world: str | None = None,
) -> dict[str, Any] | None:
    """manifest展開runを1件leaseする。cursorは期限切れ回収時にも維持する。"""
    if not worker_id.strip() or lease_seconds <= 0:
        raise ValueError("worker_id and positive lease_seconds are required")
    token = uuid.uuid4().hex
    world_clause = " AND world=%s" if world is not None else ""
    params: list[Any] = []
    if world is not None:
        params.append(_world_id(world))
    params.extend([worker_id.strip(), token, lease_seconds])
    _ensure()
    with _connect() as connection:
        connection.execute(
            "UPDATE ocr_refresh_runs SET status='failed', error_code=COALESCE(error_code,'lease_expired'), "
            "error_detail=COALESCE(error_detail,'refresh lease expired after final attempt'), "
            "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, updated_at=now(), finished_at=now() "
            "WHERE status='leased' AND lease_expires_at<=now() AND attempts>=max_attempts"
        )
        return connection.execute(
            "WITH candidate AS (SELECT id FROM ocr_refresh_runs WHERE "
            "(status='queued' OR (status='leased' AND lease_expires_at<=now())) AND attempts<max_attempts" +
            world_clause + " "
            "ORDER BY updated_at, id FOR UPDATE SKIP LOCKED LIMIT 1) "
            "UPDATE ocr_refresh_runs AS run SET status='leased', attempts=run.attempts+1, lease_owner=%s, "
            "lease_token=%s, lease_expires_at=now() + (%s * interval '1 second'), updated_at=now() "
            "FROM candidate WHERE run.id=candidate.id RETURNING run.*",
            params,
        ).fetchone()


def renew_refresh_run(run_id: int, lease_token: str, *, lease_seconds: int = 90) -> bool:
    if lease_seconds <= 0 or not lease_token:
        raise ValueError("valid refresh lease is required")
    _ensure()
    with _connect() as connection:
        row = connection.execute(
            "UPDATE ocr_refresh_runs SET lease_expires_at=now() + (%s * interval '1 second'), updated_at=now() "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING id",
            (lease_seconds, run_id, lease_token),
        ).fetchone()
    return row is not None


def update_refresh_run_progress(
    run_id: int,
    lease_token: str,
    *,
    cursor_rel_path: str,
    selected_delta: int,
    excluded_delta: int,
    failed_binding_delta: int,
    jobs_delta: int,
    lease_seconds: int = 90,
) -> bool:
    """1 manifestのjob投入と同じ順序でcursor/counterを永続化し、同時にleaseを更新する。"""
    cursor = _relative_path(cursor_rel_path)
    deltas = (selected_delta, excluded_delta, failed_binding_delta, jobs_delta)
    if any(value < 0 for value in deltas) or lease_seconds <= 0 or not lease_token:
        raise ValueError("invalid OCR refresh progress")
    _ensure()
    with _connect() as connection:
        row = connection.execute(
            "UPDATE ocr_refresh_runs SET cursor_rel_path=%s, manifests_processed=manifests_processed+1, "
            "selected_count=selected_count+%s, excluded_count=excluded_count+%s, "
            "failed_binding_count=failed_binding_count+%s, jobs_enqueued=jobs_enqueued+%s, "
            "lease_expires_at=now() + (%s * interval '1 second'), updated_at=now() "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING id",
            (cursor, *deltas, lease_seconds, run_id, lease_token),
        ).fetchone()
    return row is not None


def complete_refresh_run(run_id: int, lease_token: str) -> dict[str, Any] | None:
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "UPDATE ocr_refresh_runs SET status='completed', lease_owner=NULL, lease_token=NULL, "
            "lease_expires_at=NULL, error_code=NULL, error_detail=NULL, updated_at=now(), finished_at=now() "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING *",
            (run_id, lease_token),
        ).fetchone()


def fail_refresh_run(
    run_id: int,
    lease_token: str,
    *,
    error_code: str,
    error_detail: str | None = None,
    retryable: bool,
) -> dict[str, Any] | None:
    if not error_code.strip():
        raise ValueError("refresh error_code is required")
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "UPDATE ocr_refresh_runs SET "
            "status=CASE WHEN %s AND attempts<max_attempts THEN 'queued' ELSE 'failed' END, "
            "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, error_code=%s, error_detail=%s, "
            "updated_at=now(), finished_at=CASE WHEN %s AND attempts<max_attempts THEN NULL ELSE now() END "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING *",
            (retryable, error_code.strip(), error_detail, retryable, run_id, lease_token),
        ).fetchone()


def mark_refresh_run_stale(run_id: int, lease_token: str) -> dict[str, Any] | None:
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "UPDATE ocr_refresh_runs SET status='cancelled', lease_owner=NULL, lease_token=NULL, "
            "lease_expires_at=NULL, error_code='stale_generation', "
            "error_detail='Canonical generation is no longer active', updated_at=now(), finished_at=now() "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING *",
            (run_id, lease_token),
        ).fetchone()


def lease_next(worker_id: str, *, lease_seconds: int = 900, world: str | None = None) -> dict | None:
    """優先度順に1件leaseする。期限切れleaseはattemptを増やして別workerが回収できる。"""
    if not worker_id.strip() or lease_seconds <= 0:
        raise ValueError("worker_id and positive lease_seconds are required")
    token = uuid.uuid4().hex
    world_clause = " AND world=%s" if world is not None else ""
    params: list[Any] = []
    if world is not None:
        params.append(world)
    params.extend([worker_id.strip(), token, lease_seconds])
    _ensure()
    with _connect() as connection:
        # 最終attemptのworkerがcrashしたjobは、期限切れ後もleasedのまま永久保留にしない。
        connection.execute(
            "UPDATE ocr_jobs SET status='failed', error_code=COALESCE(error_code,'lease_expired'), "
            "error_detail=COALESCE(error_detail,'lease expired after final attempt'), "
            "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, updated_at=now(), finished_at=now() "
            "WHERE status='leased' AND lease_expires_at<=now() AND attempts>=max_attempts"
        )
        return connection.execute(
            "WITH candidate AS ("
            " SELECT id FROM ocr_jobs WHERE ("
            "   (status='queued' AND available_at<=now()) OR "
            "   (status='leased' AND lease_expires_at<=now())"
            ") AND attempts < max_attempts" + world_clause +
            " ORDER BY priority DESC, available_at, id FOR UPDATE SKIP LOCKED LIMIT 1"
            ") UPDATE ocr_jobs AS job SET status='leased', attempts=job.attempts+1, "
            "lease_owner=%s, lease_token=%s, lease_expires_at=now() + (%s * interval '1 second'), "
            "updated_at=now() FROM candidate WHERE job.id=candidate.id RETURNING job.*",
            params,
        ).fetchone()


def renew_lease(job_id: int, lease_token: str, *, lease_seconds: int = 900) -> bool:
    if lease_seconds <= 0 or not lease_token:
        raise ValueError("valid lease is required")
    _ensure()
    with _connect() as connection:
        row = connection.execute(
            "UPDATE ocr_jobs SET lease_expires_at=now() + (%s * interval '1 second'), updated_at=now() "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING id",
            (lease_seconds, job_id, lease_token),
        ).fetchone()
    return row is not None


def complete_job(
    job_id: int,
    lease_token: str,
    *,
    observation_set_hash: str,
    result_payload: dict[str, Any],
    cache_hit: bool = False,
    observation_count: int | None = None,
) -> dict | None:
    """現在のlease token所有者だけが完了できる。"""
    result_hash = _tagged_hash(observation_set_hash)
    if observation_count is None:
        observations = result_payload.get("observations")
        observation_count = len(observations) if isinstance(observations, list) else 0
    if observation_count < 0:
        raise ValueError("observation_count must not be negative")
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "UPDATE ocr_jobs SET status='succeeded', result_observation_set_hash=%s, result_payload=%s, "
            "cache_hit=%s, observation_count=%s, "
            "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, error_code=NULL, error_detail=NULL, "
            "updated_at=now(), finished_at=now() "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING *",
            (result_hash, Json(result_payload), cache_hit, observation_count, job_id, lease_token),
        ).fetchone()


def fail_job(
    job_id: int,
    lease_token: str,
    *,
    error_code: str,
    error_detail: str | None = None,
    retryable: bool,
    retry_delay_seconds: int = 30,
) -> dict | None:
    """retryableかつ試行余地があればqueueへ戻し、それ以外はfailedで確定する。"""
    if not error_code.strip() or retry_delay_seconds < 0:
        raise ValueError("invalid OCR failure")
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "UPDATE ocr_jobs SET "
            "status=CASE WHEN %s AND attempts<max_attempts THEN 'queued' ELSE 'failed' END, "
            "available_at=CASE WHEN %s AND attempts<max_attempts "
            " THEN now() + (%s * interval '1 second') ELSE available_at END, "
            "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, error_code=%s, error_detail=%s, "
            "updated_at=now(), finished_at=CASE WHEN %s AND attempts<max_attempts THEN NULL ELSE now() END "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING *",
            (retryable, retryable, retry_delay_seconds, error_code.strip(), error_detail,
             retryable, job_id, lease_token),
        ).fetchone()


def mark_stale(job_id: int, lease_token: str, *, reason: str = "canonical_generation_changed") -> dict | None:
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "UPDATE ocr_jobs SET status='stale', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
            "error_code='stale_generation', error_detail=%s, updated_at=now(), finished_at=now() "
            "WHERE id=%s AND status='leased' AND lease_token=%s AND lease_expires_at>now() RETURNING *",
            (reason, job_id, lease_token),
        ).fetchone()


def cancel_generation(world: str, canonical_generation_id: str) -> int:
    generation = _generation_id(canonical_generation_id)
    _ensure()
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE ocr_jobs SET status='cancelled', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
            "updated_at=now(), finished_at=now() WHERE world=%s AND canonical_generation_id=%s "
            "AND status IN ('queued','leased')",
            (world, generation),
        )
        affected = int(cursor.rowcount)
    return affected


def cancel_superseded_generations(world: str, active_generation_id: str) -> dict[str, int]:
    """Canonical publish後に旧generationの実行中job/runを原子的にcancelする。"""
    selected_world = _world_id(world)
    active = _generation_id(active_generation_id)
    _ensure()
    with _connect() as connection:
        jobs = connection.execute(
            "UPDATE ocr_jobs SET status='cancelled', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
            "error_code='superseded_generation', error_detail='new Canonical generation became active', "
            "updated_at=now(), finished_at=now() WHERE world=%s AND canonical_generation_id<>%s "
            "AND status IN ('queued','leased')",
            (selected_world, active),
        )
        runs = connection.execute(
            "UPDATE ocr_refresh_runs SET status='cancelled', lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, "
            "error_code='superseded_generation', error_detail='new Canonical generation became active', "
            "updated_at=now(), finished_at=now() WHERE world=%s AND canonical_generation_id<>%s "
            "AND status IN ('queued','leased')",
            (selected_world, active),
        )
        jobs_cancelled = int(jobs.rowcount)
        runs_cancelled = int(runs.rowcount)
    return {"jobs_cancelled": jobs_cancelled, "refresh_runs_cancelled": runs_cancelled}


def purge_generation(world: str, canonical_generation_id: str) -> dict[str, int]:
    """指定generationのjob本文とrefresh進捗をtransactionで削除する。World cacheは共有のため残す。"""
    selected_world = _world_id(world)
    generation = _generation_id(canonical_generation_id)
    _ensure()
    with _connect() as connection:
        runs = connection.execute(
            "DELETE FROM ocr_refresh_runs WHERE world=%s AND canonical_generation_id=%s",
            (selected_world, generation),
        )
        jobs = connection.execute(
            "DELETE FROM ocr_jobs WHERE world=%s AND canonical_generation_id=%s",
            (selected_world, generation),
        )
        jobs_deleted = int(jobs.rowcount)
        runs_deleted = int(runs.rowcount)
    return {"jobs": jobs_deleted, "refresh_runs": runs_deleted}


def purge_superseded_generations(world: str, active_generation_id: str) -> dict[str, int]:
    """現Canonical以外のOCR job本文・runをtransactionで削除する。"""
    selected_world = _world_id(world)
    active = _generation_id(active_generation_id)
    _ensure()
    with _connect() as connection:
        runs = connection.execute(
            "DELETE FROM ocr_refresh_runs WHERE world=%s AND canonical_generation_id<>%s",
            (selected_world, active),
        )
        jobs = connection.execute(
            "DELETE FROM ocr_jobs WHERE world=%s AND canonical_generation_id<>%s",
            (selected_world, active),
        )
        jobs_deleted = int(jobs.rowcount)
        runs_deleted = int(runs.rowcount)
    return {"jobs": jobs_deleted, "refresh_runs": runs_deleted}


def purge_world(world: str) -> dict[str, int]:
    """World削除/rebind用。OCR本文を含むjob/cache/runを1 transactionで消去する。"""
    selected_world = _world_id(world)
    _ensure()
    with _connect() as connection:
        runs = connection.execute(
            "DELETE FROM ocr_refresh_runs WHERE world=%s", (selected_world,),
        )
        jobs = connection.execute(
            "DELETE FROM ocr_jobs WHERE world=%s", (selected_world,),
        )
        cache = connection.execute(
            "DELETE FROM ocr_result_cache WHERE world=%s", (selected_world,),
        )
        jobs_deleted = int(jobs.rowcount)
        cache_deleted = int(cache.rowcount)
        runs_deleted = int(runs.rowcount)
    return {
        "jobs": jobs_deleted,
        "cache_entries": cache_deleted,
        "refresh_runs": runs_deleted,
    }


def generation_state(world: str, canonical_generation_id: str) -> dict[str, int | bool]:
    """snapshot公開判定を1 DB snapshotで返す。refresh展開中はjobがゼロでもterminalではない。"""
    selected_world = _world_id(world)
    generation = _generation_id(canonical_generation_id)
    _ensure()
    with _connect() as connection:
        row = connection.execute(
            "SELECT "
            "(SELECT count(*) FROM ocr_jobs WHERE world=%s AND canonical_generation_id=%s "
            " AND status IN ('queued','leased')) AS pending_jobs, "
            "(SELECT count(*) FROM ocr_jobs WHERE world=%s AND canonical_generation_id=%s "
            " AND status='succeeded' AND artifact_published=false) AS unpublished_jobs, "
            "(SELECT count(*) FROM ocr_refresh_runs WHERE world=%s AND canonical_generation_id=%s "
            " AND status IN ('queued','leased')) AS pending_refresh_runs",
            (selected_world, generation, selected_world, generation, selected_world, generation),
        ).fetchone()
    pending_jobs = int(row["pending_jobs"])
    pending_runs = int(row["pending_refresh_runs"])
    unpublished = int(row["unpublished_jobs"])
    return {
        "terminal": pending_jobs == 0 and pending_runs == 0,
        "pending_jobs": pending_jobs,
        "pending_refresh_runs": pending_runs,
        "unpublished_jobs": unpublished,
    }


def generation_terminal(world: str, canonical_generation_id: str) -> bool:
    return bool(generation_state(world, canonical_generation_id)["terminal"])


def generation_ready_for_publication(world: str, canonical_generation_id: str) -> bool:
    state = generation_state(world, canonical_generation_id)
    return bool(state["terminal"] and state["unpublished_jobs"])


def status_summary(world: str, canonical_generation_id: str | None = None) -> dict[str, Any]:
    """World status APIへそのまま載せられる件数と最終更新を返す。"""
    where = "world=%s"
    params: list[Any] = [world]
    if canonical_generation_id is not None:
        generation = canonical_generation_id.strip().lower()
        if _GENERATION_RE.fullmatch(generation) is None:
            raise ValueError("invalid canonical generation id")
        where += " AND canonical_generation_id=%s"
        params.append(generation)
    _ensure()
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT status, count(*) AS count FROM ocr_jobs WHERE {where} GROUP BY status", params,
        ).fetchall()
        last = connection.execute(
            f"SELECT max(updated_at) AS updated_at FROM ocr_jobs WHERE {where}", params,
        ).fetchone()
        aggregates = connection.execute(
            f"SELECT count(*) AS targets, "
            "count(*) FILTER (WHERE status='succeeded') AS processed, "
            "count(*) FILTER (WHERE status='succeeded' AND cache_hit) AS cached, "
            "count(*) FILTER (WHERE status='succeeded' AND observation_count=0) AS empty, "
            "count(*) FILTER (WHERE status='failed') AS failed, "
            "count(*) FILTER (WHERE status IN ('queued','leased')) AS pending "
            f"FROM ocr_jobs WHERE {where}", params,
        ).fetchone()
    counts = {name: 0 for name in ("queued", "leased", "succeeded", "failed", "stale", "cancelled")}
    counts.update({str(row["status"]): int(row["count"]) for row in rows})
    summary = {name: int(aggregates[name]) for name in (
        "targets", "processed", "cached", "empty", "failed", "pending",
    )}
    return {**summary, "counts": counts, "total": sum(counts.values()), "updated_at": last["updated_at"] if last else None}


def refresh_run_summary(world: str, canonical_generation_id: str | None = None) -> dict[str, Any]:
    """API/運用表示用の永続refresh run集計。"""
    selected_world = _world_id(world)
    where = "world=%s"
    params: list[Any] = [selected_world]
    if canonical_generation_id is not None:
        where += " AND canonical_generation_id=%s"
        params.append(_generation_id(canonical_generation_id))
    _ensure()
    with _connect() as connection:
        rows = connection.execute(
            f"SELECT status, count(*) AS count FROM ocr_refresh_runs WHERE {where} GROUP BY status", params,
        ).fetchall()
        totals = connection.execute(
            f"SELECT COALESCE(sum(manifests_processed),0) AS manifests, "
            "COALESCE(sum(selected_count),0) AS selected, COALESCE(sum(excluded_count),0) AS excluded, "
            "COALESCE(sum(failed_binding_count),0) AS failed_binding, "
            "COALESCE(sum(jobs_enqueued),0) AS jobs, max(updated_at) AS updated_at "
            f"FROM ocr_refresh_runs WHERE {where}", params,
        ).fetchone()
    counts = {name: 0 for name in ("queued", "leased", "completed", "failed", "cancelled")}
    counts.update({str(row["status"]): int(row["count"]) for row in rows})
    return {
        "counts": counts,
        "total": sum(counts.values()),
        "pending": counts["queued"] + counts["leased"],
        **{name: int(totals[name]) for name in ("manifests", "selected", "excluded", "failed_binding", "jobs")},
        "updated_at": totals["updated_at"],
    }


def list_succeeded_results(world: str, canonical_generation_id: str) -> list[dict[str, Any]]:
    """別観測generation再構築用に、現Canonicalへbindした完了Setを決定順で返す。"""
    generation = canonical_generation_id.strip().lower()
    if _GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("invalid canonical generation id")
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "SELECT * FROM ocr_jobs WHERE world=%s AND canonical_generation_id=%s AND status='succeeded' "
            "AND result_payload IS NOT NULL ORDER BY source_rel_path, route_input_id, id",
            (world, generation),
        ).fetchall()


def iter_succeeded_results(
    world: str,
    canonical_generation_id: str,
    *,
    batch_size: int = 100,
) -> Iterator[dict[str, Any]]:
    """完了Setをkeyset paginationで有界に読む。

    ``fetchall``は各batch内だけであり、World全体を保持しない。Observation rendererが要求する
    ``source_rel_path, observation_set_hash``順をDB側で固定する。
    """
    selected_world = _world_id(world)
    generation = _generation_id(canonical_generation_id)
    if batch_size <= 0 or batch_size > 1000:
        raise ValueError("invalid OCR result batch size")
    _ensure()
    previous: tuple[str, str, int] | None = None
    while True:
        where_after = ""
        params: list[Any] = [selected_world, generation]
        if previous is not None:
            where_after = "AND (source_rel_path, result_observation_set_hash, id) > (%s,%s,%s) "
            params.extend(previous)
        params.append(batch_size)
        with _connect() as connection:
            rows = connection.execute(
                "SELECT * FROM ocr_jobs WHERE world=%s AND canonical_generation_id=%s AND status='succeeded' "
                "AND result_payload IS NOT NULL AND result_observation_set_hash IS NOT NULL "
                + where_after
                + "ORDER BY source_rel_path, result_observation_set_hash, id LIMIT %s",
                params,
            ).fetchall()
        if not rows:
            return
        for row in rows:
            yield row
        last = rows[-1]
        previous = (
            str(last["source_rel_path"]),
            str(last["result_observation_set_hash"]),
            int(last["id"]),
        )
        if len(rows) < batch_size:
            return


def succeeded_results_snapshot(world: str, canonical_generation_id: str) -> dict[str, int | None]:
    """完了Set集合の小さい不変性tokenを返す（result本文やID listを返さない）。"""
    selected_world = _world_id(world)
    generation = _generation_id(canonical_generation_id)
    _ensure()
    with _connect() as connection:
        row = connection.execute(
            "SELECT count(*) AS row_count, min(id) AS min_id, max(id) AS max_id, "
            "COALESCE(sum(id),0) AS id_sum FROM ocr_jobs "
            "WHERE world=%s AND canonical_generation_id=%s AND status='succeeded' "
            "AND result_payload IS NOT NULL AND result_observation_set_hash IS NOT NULL",
            (selected_world, generation),
        ).fetchone()
    return {
        "row_count": int(row["row_count"]),
        "min_id": int(row["min_id"]) if row["min_id"] is not None else None,
        "max_id": int(row["max_id"]) if row["max_id"] is not None else None,
        "id_sum": int(row["id_sum"]),
    }


def list_unpublished_generations(*, limit: int = 100) -> list[dict[str, Any]]:
    """worker再起動・artifact publish失敗後の自己修復候補を新しい順で返す。"""
    if limit <= 0 or limit > 1000:
        raise ValueError("invalid unpublished generation limit")
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "SELECT candidate.world, candidate.canonical_generation_id, max(candidate.updated_at) AS updated_at "
            "FROM ocr_jobs AS candidate WHERE candidate.status='succeeded' AND candidate.artifact_published=false "
            "AND NOT EXISTS (SELECT 1 FROM ocr_jobs AS pending WHERE pending.world=candidate.world "
            " AND pending.canonical_generation_id=candidate.canonical_generation_id "
            " AND pending.status IN ('queued','leased')) "
            "AND NOT EXISTS (SELECT 1 FROM ocr_refresh_runs AS refresh WHERE refresh.world=candidate.world "
            " AND refresh.canonical_generation_id=candidate.canonical_generation_id "
            " AND refresh.status IN ('queued','leased')) "
            "GROUP BY candidate.world, candidate.canonical_generation_id ORDER BY updated_at DESC LIMIT %s",
            (limit,),
        ).fetchall()


def mark_artifacts_published(job_ids: list[int]) -> int:
    """実際にsnapshotへ含めたjobだけを公開済みにする（並行完了jobの誤mark防止）。"""
    normalized = sorted({int(value) for value in job_ids if int(value) > 0})
    if not normalized:
        return 0
    _ensure()
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE ocr_jobs SET artifact_published=true, updated_at=now() "
            "WHERE id = ANY(%s) AND status='succeeded' AND artifact_published=false",
            (normalized,),
        )
        affected = int(cursor.rowcount)
    return affected


def mark_snapshot_artifacts_published(
    world: str,
    canonical_generation_id: str,
    snapshot: dict[str, int | None],
) -> int:
    """同じ完了Set集合のままなら、集合全体をID listなしで公開済みにする。

    集合照合とUPDATEは1 SQL statementのsnapshotで行う。並行して新しい成功jobが見えた場合は
    何もmarkせず、workerのself-repairに再公開を委ねる。
    """
    selected_world = _world_id(world)
    generation = _generation_id(canonical_generation_id)
    row_count = snapshot.get("row_count")
    min_id = snapshot.get("min_id")
    max_id = snapshot.get("max_id")
    id_sum = snapshot.get("id_sum")
    if (not isinstance(row_count, int) or isinstance(row_count, bool) or row_count < 0
            or (min_id is not None and (not isinstance(min_id, int) or isinstance(min_id, bool) or min_id <= 0))
            or (max_id is not None and (not isinstance(max_id, int) or isinstance(max_id, bool) or max_id <= 0))
            or not isinstance(id_sum, int) or isinstance(id_sum, bool) or id_sum < 0
            or (row_count == 0 and (min_id is not None or max_id is not None))
            or (row_count > 0 and (min_id is None or max_id is None))):
        raise ValueError("invalid succeeded OCR snapshot")
    _ensure()
    with _connect() as connection:
        row = connection.execute(
            "WITH current AS MATERIALIZED ("
            " SELECT count(*) AS row_count, min(id) AS min_id, max(id) AS max_id, COALESCE(sum(id),0) AS id_sum"
            " FROM ocr_jobs WHERE world=%s AND canonical_generation_id=%s AND status='succeeded'"
            " AND result_payload IS NOT NULL AND result_observation_set_hash IS NOT NULL"
            "), updated AS ("
            " UPDATE ocr_jobs AS job SET artifact_published=true, updated_at=now() FROM current"
            " WHERE job.world=%s AND job.canonical_generation_id=%s AND job.status='succeeded'"
            " AND job.result_payload IS NOT NULL AND job.result_observation_set_hash IS NOT NULL"
            " AND job.artifact_published=false AND current.row_count=%s"
            " AND current.min_id IS NOT DISTINCT FROM %s AND current.max_id IS NOT DISTINCT FROM %s"
            " AND current.id_sum=%s RETURNING job.id"
            ") SELECT count(*) AS marked FROM updated",
            (
                selected_world, generation, selected_world, generation,
                row_count, min_id, max_id, id_sum,
            ),
        ).fetchone()
    return int(row["marked"])


def requeue_failed(world: str, canonical_generation_id: str) -> int:
    """明示refresh用。同じactive Canonicalのterminal failureだけを再試行可能に戻す。"""
    generation = canonical_generation_id.strip().lower()
    if _GENERATION_RE.fullmatch(generation) is None:
        raise ValueError("invalid canonical generation id")
    _ensure()
    with _connect() as connection:
        cursor = connection.execute(
            "UPDATE ocr_jobs SET status='queued', attempts=0, available_at=now(), error_code=NULL, error_detail=NULL, "
            "lease_owner=NULL, lease_token=NULL, lease_expires_at=NULL, finished_at=NULL, updated_at=now() "
            "WHERE world=%s AND canonical_generation_id=%s AND status='failed'",
            (world, generation),
        )
        affected = int(cursor.rowcount)
    return affected


def record_worker_heartbeat(
    worker_id: str,
    *,
    engine_profile_hash: str,
    available: bool,
    unavailable_reason: str | None,
    model_hashes_valid: bool,
    status: str,
    metadata: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """隔離worker自身が依存/model availabilityを記録する。FastAPI側でPaddleをimportしない。"""
    if not worker_id.strip() or status not in {"starting", "idle", "processing", "unavailable", "stopping"}:
        raise ValueError("invalid OCR worker heartbeat")
    profile = _tagged_hash(engine_profile_hash)
    if available and (unavailable_reason is not None or not model_hashes_valid):
        raise ValueError("available worker must have valid models and no unavailable reason")
    _ensure()
    with _connect() as connection:
        return connection.execute(
            "INSERT INTO ocr_worker_heartbeats (worker_id, engine_profile_hash, available, unavailable_reason, "
            "model_hashes_valid, status, metadata) VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (worker_id) DO UPDATE SET engine_profile_hash=EXCLUDED.engine_profile_hash, "
            "available=EXCLUDED.available, unavailable_reason=EXCLUDED.unavailable_reason, "
            "model_hashes_valid=EXCLUDED.model_hashes_valid, status=EXCLUDED.status, metadata=EXCLUDED.metadata, "
            "last_seen_at=now() RETURNING *",
            (worker_id.strip(), profile, available, unavailable_reason, model_hashes_valid, status, Json(metadata or {})),
        ).fetchone()


def worker_availability_summary(engine_profile_hash: str, *, stale_seconds: int = 900) -> dict[str, Any]:
    """API用。直近heartbeatが無ければworker不在としてavailable=falseを返す。"""
    if stale_seconds <= 0:
        raise ValueError("stale_seconds must be positive")
    profile = _tagged_hash(engine_profile_hash)
    _ensure()
    with _connect() as connection:
        rows = connection.execute(
            "SELECT worker_id, available, unavailable_reason, model_hashes_valid, status, metadata, last_seen_at "
            "FROM ocr_worker_heartbeats WHERE engine_profile_hash=%s "
            "AND last_seen_at >= now() - (%s * interval '1 second') ORDER BY last_seen_at DESC",
            (profile, stale_seconds),
        ).fetchall()
    usable = [row for row in rows if row["available"] and row["model_hashes_valid"]]
    latest = rows[0] if rows else None
    return {
        "available": bool(usable),
        "unavailable_reason": None if usable else (
            latest.get("unavailable_reason") or "worker_unavailable" if latest else "worker_not_seen"
        ),
        "model_hashes_valid": bool(usable),
        "engine_profile_hash": profile,
        "worker_count": len(rows),
        "last_seen_at": latest.get("last_seen_at") if latest else None,
        "workers": rows,
    }


def _result_hash(payload: dict[str, Any]) -> str:
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return "sha256:" + hashlib.sha256(raw).hexdigest()


def get_cached_result(world: str, input_fingerprint: str, engine_profile_hash: str) -> dict | None:
    fingerprint, profile = _tagged_hash(input_fingerprint), _tagged_hash(engine_profile_hash)
    _ensure()
    with _connect() as connection:
        row = connection.execute(
            "UPDATE ocr_result_cache SET last_used_at=now() WHERE world=%s AND input_fingerprint=%s "
            "AND engine_profile_hash=%s RETURNING *",
            (world, fingerprint, profile),
        ).fetchone()
    return row


def put_cached_result_for_lease(
    job_id: int,
    lease_token: str,
    world: str,
    input_fingerprint: str,
    engine_profile_hash: str,
    result_payload: dict[str, Any],
) -> dict | None:
    """有効なjob leaseを同じtransactionでlockできた場合だけ推論cacheをcommitする。"""
    if not lease_token:
        raise ValueError("valid lease token is required")
    selected_world = _world_id(world)
    fingerprint, profile = _tagged_hash(input_fingerprint), _tagged_hash(engine_profile_hash)
    result_hash = _result_hash(result_payload)
    _ensure()
    with _connect() as connection:
        owned = connection.execute(
            "SELECT id FROM ocr_jobs WHERE id=%s AND world=%s AND status='leased' AND lease_token=%s "
            "AND lease_expires_at>now() FOR UPDATE",
            (job_id, selected_world, lease_token),
        ).fetchone()
        if owned is None:
            return None
        connection.execute(
            "INSERT INTO ocr_result_cache (world, input_fingerprint, engine_profile_hash, result_hash, result_payload) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (selected_world, fingerprint, profile, result_hash, Json(result_payload)),
        )
        return connection.execute(
            "UPDATE ocr_result_cache SET last_used_at=now() WHERE world=%s AND input_fingerprint=%s "
            "AND engine_profile_hash=%s RETURNING *",
            (selected_world, fingerprint, profile),
        ).fetchone()


def put_cached_result(
    world: str,
    input_fingerprint: str,
    engine_profile_hash: str,
    result_payload: dict[str, Any],
) -> dict:
    """並行推論時は先に保存された結果を権威とし、同一cache keyを上書きしない。"""
    fingerprint, profile = _tagged_hash(input_fingerprint), _tagged_hash(engine_profile_hash)
    result_hash = _result_hash(result_payload)
    _ensure()
    with _connect() as connection:
        connection.execute(
            "INSERT INTO ocr_result_cache (world, input_fingerprint, engine_profile_hash, result_hash, result_payload) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (world, fingerprint, profile, result_hash, Json(result_payload)),
        )
        row = connection.execute(
            "UPDATE ocr_result_cache SET last_used_at=now() WHERE world=%s AND input_fingerprint=%s "
            "AND engine_profile_hash=%s RETURNING *",
            (world, fingerprint, profile),
        ).fetchone()
    if row is None:  # pragma: no cover - INSERT直後のDB異常だけ
        raise RuntimeError("OCR cache row disappeared")
    return row
