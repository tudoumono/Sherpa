from __future__ import annotations

import hashlib
import time
from pathlib import Path

from .artifacts import CaseEvidence, redact


def real_ai_turn_checkpoint(dsn: str, expected_env_path: Path | None) -> dict:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for real AI turn correlation"
    assert expected_env_path is not None and expected_env_path.is_file(), (
        "SHERPA_UI_EXPECTED_ENV_JSON is required for real AI service-log correlation"
    )
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for real AI turn correlation") from exc

    with psycopg.connect(dsn, connect_timeout=5) as connection:
        message_id = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM messages").fetchone()[0])
        audit_id = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM audit_log").fetchone()[0])
        usage_event_id = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM usage_events").fetchone()[0])

    service_root = expected_env_path.parent.parent / "services"
    assert service_root.is_dir(), f"runner service-log directory is absent: {service_root}"
    log_offsets = {str(path): path.stat().st_size for path in sorted(service_root.glob("app*.log")) if path.is_file()}
    assert log_offsets, "runner produced no application log before the real AI turn"
    return {
        "message_id": message_id,
        "audit_id": audit_id,
        "usage_event_id": usage_event_id,
        "service_root": str(service_root),
        "service_log_offsets": log_offsets,
    }


def correlate_real_ai_turn_database(
    dsn: str,
    *,
    conversation_id: int,
    assistant_message_id: int,
    checkpoint: dict,
    reported_usage: dict,
    reported_trace: list[dict],
    reported_message: dict,
) -> dict:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for real AI turn correlation"
    for key in ("message_id", "audit_id", "usage_event_id"):
        assert isinstance(checkpoint.get(key), int), f"real AI checkpoint lacks integer {key}"
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for real AI turn correlation") from exc

    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        message_rows = connection.execute(
            "SELECT id,conversation_id,role,content,lens,route,trace,answer,personal,created_at FROM messages "
            "WHERE conversation_id=%s AND id>%s ORDER BY id",
            (conversation_id, checkpoint["message_id"]),
        ).fetchall()
        audit_rows = connection.execute(
            "SELECT id,action,resource_type,resource_id,outcome,detail,created_at "
            "FROM audit_log WHERE id>%s AND action='chat.turn' AND resource_id=%s ORDER BY id",
            (checkpoint["audit_id"], f"conv:{conversation_id}"),
        ).fetchall()
        usage_rows = connection.execute(
            "SELECT id,kind,provider,model,input_tokens,cached_input_tokens,output_tokens,"
            "reasoning_output_tokens,calls,user_id,world,ts FROM usage_events "
            "WHERE id>%s ORDER BY id",
            (checkpoint["usage_event_id"],),
        ).fetchall()

    normalized_message_rows = [dict(row) for row in message_rows]
    user_rows = [row for row in normalized_message_rows if row.get("role") == "user"]
    assistant_rows = [row for row in normalized_message_rows if row.get("role") == "assistant"]
    assert len(user_rows) == 1 and len(assistant_rows) == 1, (
        "expected exactly one new user/assistant message pair for the real AI turn, "
        f"got roles={[row.get('role') for row in normalized_message_rows]}"
    )
    assert [row.get("role") for row in normalized_message_rows] == ["user", "assistant"], (
        "real AI turn messages were duplicated or persisted out of order"
    )
    user_row = user_rows[0]
    assistant_row = assistant_rows[0]
    assert int(user_row["conversation_id"]) == int(conversation_id)
    assert int(assistant_row["conversation_id"]) == int(conversation_id)
    assert int(assistant_row["id"]) == int(assistant_message_id), "conversation API assistant message does not match the new Postgres row"
    assert int(reported_message.get("id") or 0) == int(assistant_message_id), (
        "reported conversation API message id differs from the requested assistant message"
    )
    assert str(reported_message.get("role") or "") == "assistant", "reported conversation API row is not an assistant message"
    for key in ("content", "lens", "route", "trace", "answer"):
        assert assistant_row.get(key) == reported_message.get(key), f"Postgres assistant {key} differs from the conversation API"
    stored_content = str(assistant_row.get("content") or "")
    stored_answer = assistant_row.get("answer") or {}
    assert stored_content and stored_content == str(stored_answer.get("headline") or ""), (
        "Postgres assistant content does not exactly match its final answer headline"
    )
    stored_trace = assistant_row.get("trace") or []
    assert isinstance(stored_trace, list) and stored_trace, "Postgres assistant message has no structured execution trace"
    assert stored_trace == reported_trace, "Postgres execution trace differs from the conversation API trace"
    stored_usage = (assistant_row.get("answer") or {}).get("usage")
    assert isinstance(stored_usage, dict), "Postgres assistant answer has no persisted usage"
    reported_provider = str(reported_usage.get("provider") or "").strip().lower()
    reported_model = str(reported_usage.get("model") or "").strip()
    stored_provider = str(stored_usage.get("provider") or "").strip().lower()
    stored_model = str(stored_usage.get("model") or "").strip()
    assert stored_provider == reported_provider and stored_model == reported_model, (
        "Postgres usage provider/model differs from the conversation API"
    )
    for key in ("input_tokens", "output_tokens"):
        assert int(stored_usage.get(key) or 0) == int(reported_usage.get(key) or 0), (
            f"Postgres usage {key} differs from the conversation API"
        )
    assert int(stored_usage.get("input_tokens") or 0) + int(stored_usage.get("output_tokens") or 0) > 0, (
        "Postgres persisted zero token usage for a successful real AI turn"
    )

    assert len(audit_rows) == 1, f"expected exactly one new chat.turn audit row for the real AI turn, got {len(audit_rows)}"
    audit_row = dict(audit_rows[0])
    assert audit_row.get("outcome") == "success", "real AI chat.turn audit was not successful"
    audit_detail = audit_row.get("detail") or {}
    assert int(audit_detail.get("message_id_user") or 0) == int(user_row["id"]), (
        "chat.turn audit does not reference the unique persisted user message"
    )
    assert int(audit_detail.get("message_id_assistant") or 0) == int(assistant_message_id), (
        "chat.turn audit does not reference the persisted assistant message"
    )
    assert str(audit_detail.get("provider") or "").strip().lower() == reported_provider, (
        "chat.turn audit provider differs from persisted provider usage"
    )

    normalized_usage_rows = [dict(row) for row in usage_rows]
    chat_usage_rows = [row for row in normalized_usage_rows if row.get("kind") == "chat"]
    assert not chat_usage_rows, (
        "main chat usage was unexpectedly duplicated into usage_events instead of remaining canonical in messages.answer.usage"
    )
    return {
        "main_chat_usage_storage": "messages.answer.usage",
        "user_message_id": int(user_row["id"]),
        "assistant_message_id": int(assistant_row["id"]),
        "conversation_id": int(assistant_row["conversation_id"]),
        "assistant_content_sha256": hashlib.sha256(stored_content.encode()).hexdigest(),
        "assistant_content_exact_match": True,
        "provider": stored_provider,
        "model": stored_model,
        "input_tokens": int(stored_usage.get("input_tokens") or 0),
        "output_tokens": int(stored_usage.get("output_tokens") or 0),
        "audit_id": int(audit_row["id"]),
        "audit_action": audit_row["action"],
        "audit_outcome": audit_row["outcome"],
        "audit_provider": str(audit_detail.get("provider") or "").strip().lower(),
        "trace_node_count": len(stored_trace),
        # Internal hand-off for the caller's independent degradation scan.  The
        # caller removes this field before writing the compact correlation
        # summary; the full trace is already captured by conversation evidence.
        "_stored_trace": stored_trace,
        "usage_events_chat_row_count": 0,
        "auxiliary_usage_events": [
            {
                "id": int(row["id"]),
                "kind": row["kind"],
                "provider": row["provider"],
                "model": row["model"],
                "input_tokens": row["input_tokens"],
                "cached_input_tokens": row["cached_input_tokens"],
                "output_tokens": row["output_tokens"],
                "reasoning_output_tokens": row["reasoning_output_tokens"],
                "calls": row["calls"],
                "world": row["world"],
            }
            for row in normalized_usage_rows
        ],
    }


def database_utc_now(dsn: str):
    """Return the database clock so cross-process ingestion evidence shares one time base."""
    assert dsn, "SHERPA_UI_DATABASE_URL is required for database timestamp evidence"
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for database timestamp evidence") from exc
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        row = connection.execute("SELECT clock_timestamp()").fetchone()
    assert row and row[0] is not None, "Postgres did not return its current timestamp"
    return row[0]


def conversation_database_snapshot(
    dsn: str,
    conversation_id: int,
    evidence: CaseEvidence,
    *,
    turn_id: str | None = None,
) -> dict:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for independent database evidence"
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for database evidence") from exc

    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        conversation = connection.execute(
            "SELECT id,user_id,version,title,codex_session_id,created_at,updated_at FROM conversations WHERE id=%s",
            (conversation_id,),
        ).fetchone()
        assert conversation, f"conversation {conversation_id} is absent from Postgres"
        messages = connection.execute(
            "SELECT id,role,content,lens,route,trace,answer,personal,created_at FROM messages WHERE conversation_id=%s ORDER BY id",
            (conversation_id,),
        ).fetchall()
        audit_rows = connection.execute(
            "SELECT id,actor_user_id,action,resource_type,resource_id,outcome,severity,detail,created_at "
            "FROM audit_log WHERE resource_id=%s ORDER BY id",
            (f"conv:{conversation_id}",),
        ).fetchall()

    conversation = dict(conversation)
    session_id = conversation.pop("codex_session_id", None)
    conversation["codex_session"] = {
        "set": bool(session_id),
        "sha256": hashlib.sha256(session_id.encode()).hexdigest() if session_id else None,
    }
    snapshot = {
        "conversation": conversation,
        "messages": [dict(row) for row in messages],
        "audit": [dict(row) for row in audit_rows],
    }
    evidence.write_json("state/db-summary.json", redact(snapshot))
    assistant_rows = [row for row in messages if row.get("role") == "assistant"]
    turn_audits = [row for row in audit_rows if row.get("action") == "chat.turn"]
    evidence.record_database_correlation(
        conversation_id=conversation_id,
        turn_id=turn_id,
        source="conversation_database_snapshot",
        assistant_message_id=int(assistant_rows[-1]["id"]) if assistant_rows else None,
        audit_id=int(turn_audits[-1]["id"]) if turn_audits else None,
    )
    return snapshot


def conversation_session_id(dsn: str, conversation_id: int) -> str | None:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for session continuity evidence"
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for session continuity evidence") from exc
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        row = connection.execute(
            "SELECT codex_session_id FROM conversations WHERE id=%s",
            (conversation_id,),
        ).fetchone()
    assert row, f"conversation {conversation_id} is absent from Postgres"
    return row[0]


def set_nonexistent_conversation_session(dsn: str, conversation_id: int, session_id: str) -> None:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for session failure injection"
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for session failure injection") from exc
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        changed = connection.execute(
            "UPDATE conversations SET codex_session_id=%s WHERE id=%s",
            (session_id, conversation_id),
        ).rowcount
        connection.commit()
    assert changed == 1, f"conversation {conversation_id} session row was not updated"


def usage_event_checkpoint(dsn: str, kind: str) -> int:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for provider usage correlation"
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for provider usage correlation") from exc
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        row = connection.execute(
            "SELECT COALESCE(MAX(id),0) FROM usage_events WHERE kind=%s",
            (kind,),
        ).fetchone()
    return int(row[0])


def usage_events_after(dsn: str, kind: str, checkpoint: int, *, world: str | None) -> list[dict]:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for provider usage correlation"
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for provider usage correlation") from exc
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        rows = connection.execute(
            "SELECT id,ts,kind,provider,model,input_tokens,output_tokens,calls,world FROM usage_events WHERE kind=%s AND id>%s ORDER BY id",
            (kind, checkpoint),
        ).fetchall()
    return [dict(row) for row in rows if world is None or row["world"] == world]


def usage_event_after(dsn: str, kind: str, checkpoint: int, *, world: str | None) -> dict:
    matching = usage_events_after(dsn, kind, checkpoint, world=world)
    assert len(matching) == 1, f"expected one new {kind} usage event for world={world!r}, got {len(matching)}"
    return matching[0]


def wait_for_ingestion_database_snapshot(
    dsn: str,
    world_id: str,
    evidence: CaseEvidence,
    *,
    timeout_seconds: float,
) -> dict:
    assert dsn, "SHERPA_UI_DATABASE_URL is required for ingestion database evidence"
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for ingestion database evidence") from exc

    deadline = time.monotonic() + timeout_seconds
    snapshot: dict = {}
    while time.monotonic() < deadline:
        with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
            ingest_runs = connection.execute(
                "SELECT id,status,source_doc_ids,extraction_snapshot,published_snapshot,created_at "
                "FROM ingest_runs WHERE version=%s ORDER BY id DESC LIMIT 5",
                (world_id,),
            ).fetchall()
            refresh_runs = connection.execute(
                "SELECT id,status,attempts,manifests_processed,selected_count,excluded_count,"
                "failed_binding_count,jobs_enqueued,error_code,created_at,finished_at "
                "FROM ocr_refresh_runs WHERE world=%s ORDER BY id",
                (world_id,),
            ).fetchall()
            jobs = connection.execute(
                "SELECT id,source_rel_path,status,attempts,result_observation_set_hash,result_payload,"
                "cache_hit,observation_count,artifact_published,error_code,created_at,finished_at "
                "FROM ocr_jobs WHERE world=%s ORDER BY id",
                (world_id,),
            ).fetchall()
            workers = connection.execute(
                "SELECT engine_profile_hash,available,model_hashes_valid,status,last_seen_at "
                "FROM ocr_worker_heartbeats ORDER BY last_seen_at DESC",
            ).fetchall()
            vlm_usage = connection.execute(
                "SELECT id,ts,provider,model,input_tokens,cached_input_tokens,output_tokens,"
                "reasoning_output_tokens,calls FROM usage_events WHERE kind='vlm' "
                "ORDER BY id DESC LIMIT 20",
            ).fetchall()
        snapshot = {
            "ingest_runs": [dict(row) for row in ingest_runs],
            "ocr_refresh_runs": [dict(row) for row in refresh_runs],
            "ocr_jobs": [dict(row) for row in jobs],
            "ocr_workers": [dict(row) for row in workers],
            "vlm_usage_events": [dict(row) for row in vlm_usage],
        }
        terminal_failure = [row for row in refresh_runs if row["status"] in {"failed", "cancelled"}]
        terminal_failure += [row for row in jobs if row["status"] in {"failed", "cancelled", "stale"}]
        if terminal_failure:
            evidence.write_json("state/ingestion-db-summary.json", redact(snapshot))
            raise AssertionError(f"OCR execution reached a failed terminal state: {terminal_failure}")
        refresh_complete = bool(refresh_runs) and all(row["status"] == "completed" for row in refresh_runs)
        jobs_complete = bool(jobs) and all(row["status"] == "succeeded" for row in jobs)
        artifacts_published = jobs_complete and all(row["artifact_published"] for row in jobs)
        worker_ready = any(row["available"] and row["model_hashes_valid"] for row in workers)
        if refresh_complete and jobs_complete and artifacts_published and worker_ready:
            evidence.write_json("state/ingestion-db-summary.json", redact(snapshot))
            return snapshot
        time.sleep(1)
    evidence.write_json("state/ingestion-db-summary.json", redact(snapshot))
    raise AssertionError(f"OCR execution did not finish within {timeout_seconds:.0f}s: {snapshot}")
