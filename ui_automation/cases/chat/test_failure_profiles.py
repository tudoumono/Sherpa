from __future__ import annotations

import hashlib
import os
import re
import time
from pathlib import Path

import pytest

from ui_automation.support.chat_flow import prepare_chat, start_turn_from_ui
from ui_automation.support.database import conversation_database_snapshot
from ui_automation.support.ui import login_without_trace


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def _database_checkpoint(dsn: str) -> dict[str, int]:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for failure-path database evidence") from exc
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        conversation_id = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM conversations").fetchone()[0])
        audit_id = int(connection.execute("SELECT COALESCE(MAX(id),0) FROM audit_log").fetchone()[0])
    return {"conversation_id": conversation_id, "audit_id": audit_id}


def _expire_browser_session(dsn: str, token: str) -> int:
    try:
        import psycopg
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for real session expiry") from exc
    token_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()
    with psycopg.connect(dsn, connect_timeout=5) as connection:
        row = connection.execute(
            "UPDATE auth_sessions SET expires_at=now()-interval '1 second' WHERE token_hash=%s AND revoked_at IS NULL RETURNING id",
            (token_hash,),
        ).fetchone()
        connection.commit()
    assert row, "the authenticated browser session was absent from isolated Postgres"
    return int(row[0])


def _database_rows_after(dsn: str, checkpoint: dict[str, int]) -> dict:
    try:
        import psycopg
        from psycopg.rows import dict_row
    except ModuleNotFoundError as exc:
        raise AssertionError("psycopg is required for failure-path database evidence") from exc
    with psycopg.connect(dsn, row_factory=dict_row, connect_timeout=5) as connection:
        conversations = connection.execute(
            "SELECT id,user_id,title,created_at FROM conversations WHERE id>%s ORDER BY id",
            (checkpoint["conversation_id"],),
        ).fetchall()
        audits = connection.execute(
            "SELECT id,actor_user_id,action,resource_type,resource_id,outcome,severity,detail FROM audit_log WHERE id>%s ORDER BY id",
            (checkpoint["audit_id"],),
        ).fetchall()
    return {
        "conversations": [dict(row) for row in conversations],
        "audit": [dict(row) for row in audits],
    }


def _service_log_checkpoint(ui_config) -> tuple[Path, dict[str, int]]:
    assert ui_config.expected_env_path is not None
    root = ui_config.expected_env_path.parent.parent / "services"
    return root, {str(path): path.stat().st_size for path in root.glob("app*.log") if path.is_file()}


def _service_log_since(checkpoint: tuple[Path, dict[str, int]]) -> str:
    root, offsets = checkpoint
    chunks: list[str] = []
    for path in root.glob("app*.log"):
        if not path.is_file():
            continue
        offset = offsets.get(str(path), 0)
        chunks.append(path.read_bytes()[offset:].decode("utf-8", errors="replace"))
    return "\n".join(chunks)


def _wait_for_slot_release(api, turn_id: str, timeout_seconds: float) -> dict:
    deadline = time.monotonic() + timeout_seconds
    latest: dict = {}
    while time.monotonic() < deadline:
        latest = api.get_json("/chat/turns/running")
        if all(str(row.get("turn_id")) != turn_id for row in latest.get("turns") or []):
            return latest
        time.sleep(0.2)
    raise AssertionError(f"failed turn {turn_id} retained its real execution slot: {latest}")


def _structured_database_error(message: dict) -> bool:
    answer = message.get("answer") or {}
    return answer.get("error") is True or answer.get("status") == "error" or bool(answer.get("error_code"))


def _exercise_provider_failure(
    *,
    admin_page,
    live_api,
    ui_config,
    artifact_case,
    real_world: str,
    expected_agent: str,
    log_pattern: re.Pattern,
    evidence_name: str,
) -> None:
    settings = live_api.get_json("/settings", save_as=f"state/{evidence_name}-settings.json")
    assert settings.get("agent") == expected_agent, f"failure profile selected {settings.get('agent')!r}, expected {expected_agent!r}"
    if expected_agent == "openai":
        assert settings.get("openai_key_set") is True, "provider-timeout profile has no real OpenAI credential"
        timeout = float(os.environ.get("SHERPA_LLM_TIMEOUT", "60"))
        assert 0 < timeout <= 0.1, "provider-timeout profile must use a positive sub-100ms real network timeout"
    log_checkpoint = _service_log_checkpoint(ui_config)
    prepare_chat(admin_page, ui_config, real_world)
    knowledge = admin_page.locator("#kbtoggle")
    if knowledge.get_attribute("aria-pressed") == "true" and knowledge.get_attribute("aria-disabled") != "true" and knowledge.is_enabled():
        knowledge.click()
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        "実AI接続を1回だけ実行し、この要求に短く回答してください。",
    )
    conversation_id = int(started["conversation_id"])
    turn_id = str(started["turn_id"])
    artifact_case.add_cleanup(
        f"delete failed provider conversation {conversation_id}",
        lambda: _cleanup_conversation(live_api, conversation_id),
    )
    artifact_case.screenshot(admin_page, 10, f"{evidence_name}-real-provider-turn-accepted")
    events = live_api.collect_sse(f"/chat/turns/{turn_id}/stream?cursor=0", save_as="network/sse.jsonl")
    running = _wait_for_slot_release(live_api, turn_id, max(ui_config.timeout_ms / 1000, 10))
    conversation = live_api.get_json(
        f"/conversations/{conversation_id}",
        save_as=f"state/{evidence_name}-conversation-api.json",
    )
    database = conversation_database_snapshot(
        ui_config.database_url,
        conversation_id,
        artifact_case,
        turn_id=turn_id,
    )
    assistants = [row for row in database["messages"] if row.get("role") == "assistant"]
    error_events = [row for row in events if row.get("type") == "error"]
    answer_events = [row for row in events if row.get("type") == "answer"]
    database_error = bool(assistants) and _structured_database_error(assistants[-1])
    audit_error = any(row.get("action") == "chat.turn" and row.get("outcome") == "error" for row in database["audit"])
    ui_status = admin_page.locator("#rt").inner_text().strip()
    ui_error = ui_status == "エラー"
    new_logs = _service_log_since(log_checkpoint)
    log_error = bool(log_pattern.search(new_logs))
    api_assistants = [row for row in conversation.get("messages") or [] if row.get("role") == "assistant"]
    api_error = bool(api_assistants) and _structured_database_error(api_assistants[-1])
    checks = {
        "sse_has_error_event": bool(error_events),
        "sse_has_no_success_answer": not answer_events,
        "ui_has_structured_error_state": ui_error,
        "conversation_api_has_structured_error": api_error,
        "postgres_has_structured_error": database_error,
        "audit_outcome_is_error": audit_error,
        "turn_slot_released": all(str(row.get("turn_id")) != turn_id for row in running.get("turns") or []),
        "service_log_has_failure_record": log_error,
    }
    artifact_case.write_json(
        f"state/{evidence_name}-correlation.json",
        {
            "turn_id_sha256": hashlib.sha256(turn_id.encode()).hexdigest(),
            "conversation_id_sha256": hashlib.sha256(str(conversation_id).encode()).hexdigest(),
            "event_types": [str(row.get("type") or "") for row in events],
            "ui_status": ui_status,
            "log_slice_sha256": hashlib.sha256(new_logs.encode()).hexdigest(),
            "checks": checks,
        },
    )
    failed = sorted(key for key, value in checks.items() if not value)
    assert not failed, "provider failure was presented or persisted as a successful answer: " + ", ".join(failed)
    artifact_case.attest_control_state(
        control_key="send",
        state="abnormal",
        assertion="実provider障害turnがSSE・UI・DB・auditでerrorとなり成功回答を残さずslotを解放した",
    )
    artifact_case.screenshot(admin_page, 20, f"{evidence_name}-error-visible-and-turn-slot-released")


@pytest.mark.auth_expiry_real
@pytest.mark.destructive
def test_expired_session_rejects_turn_and_releases_slot(
    admin_page,
    live_api,
    ui_config,
    artifact_case,
    admin_credentials,
    isolated_stack,
    real_world,
):
    contract = ui_config.expected_environment()
    assert (contract.get("expected") or {}).get("failure_mode") == "session_expiry"
    prepare_chat(admin_page, ui_config, real_world)
    checkpoint = _database_checkpoint(ui_config.database_url)
    cookie = next(
        (row for row in admin_page.context.cookies(ui_config.base_url) if row.get("name") == "sherpa_session"),
        None,
    )
    assert cookie and cookie.get("value"), "authenticated browser has no real session cookie"
    artifact_case.register_secret(cookie["value"])
    session_row_id = _expire_browser_session(ui_config.database_url, cookie["value"])
    admin_page.locator("#input").fill("失効済みセッションからは、このメッセージを実行しないでください。")
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/chat/turns"),
        timeout=ui_config.timeout_ms,
    ) as response_info:
        admin_page.locator("#send").click()
    response = response_info.value
    assert response.status == 401, response.text()
    admin_page.wait_for_timeout(300)
    artifact_case.screenshot(admin_page, 10, "chat-expired-session-rejected-before-turn-start")
    rows = _database_rows_after(ui_config.database_url, checkpoint)

    artifact_case.stop_trace(save=True)
    login_without_trace(
        admin_page,
        ui_config.base_url,
        admin_credentials,
        "/ui/chat.html",
        ui_config.timeout_ms,
        artifact_case,
    )
    running = live_api.get_json("/chat/turns/running", save_as="state/session-expiry-running-after-relogin.json")
    checks = {
        "request_rejected_401": response.status == 401,
        "no_conversation_created": not rows["conversations"],
        "no_chat_success_audit": not any(row.get("action") == "chat.turn" and row.get("outcome") == "success" for row in rows["audit"]),
        "no_sse_turn_created": not rows["conversations"],
        "turn_slot_released": running.get("turns") == [],
    }
    artifact_case.write_json(
        "state/session-expiry-correlation.json",
        {
            "session_row_id_sha256": hashlib.sha256(str(session_row_id).encode()).hexdigest(),
            "status": response.status,
            "database": rows,
            "checks": checks,
        },
    )
    assert all(checks.values()), checks
    artifact_case.attest_control_state(
        control_key="send",
        state="abnormal",
        assertion="失効済み実sessionからの送信を401拒否しconversationもrunning turnも作成しなかった",
    )
    artifact_case.screenshot(admin_page, 20, "chat-session-relogin-confirms-no-running-turn")


@pytest.mark.provider_timeout_real
@pytest.mark.destructive
def test_real_provider_timeout_is_error_and_releases_slot(admin_page, live_api, ui_config, artifact_case, isolated_stack, real_world):
    contract = ui_config.expected_environment()
    assert (contract.get("expected") or {}).get("failure_mode") == "provider_timeout"
    _exercise_provider_failure(
        admin_page=admin_page,
        live_api=live_api,
        ui_config=ui_config,
        artifact_case=artifact_case,
        real_world=real_world,
        expected_agent="openai",
        log_pattern=re.compile(r"openai.*(?:timeout|timed out)|(?:timeout|timed out).*openai", re.I),
        evidence_name="provider-timeout",
    )


@pytest.mark.codex_cli_failure_real
@pytest.mark.destructive
def test_real_codex_cli_nonzero_is_error_and_releases_slot(admin_page, live_api, ui_config, artifact_case, isolated_stack, real_world):
    contract = ui_config.expected_environment()
    assert (contract.get("expected") or {}).get("failure_mode") == "codex_cli_nonzero"
    _exercise_provider_failure(
        admin_page=admin_page,
        live_api=live_api,
        ui_config=ui_config,
        artifact_case=artifact_case,
        real_world=real_world,
        expected_agent="codex",
        log_pattern=re.compile(r"codex silent failure:.*returncode=(?:-[0-9]+|[1-9][0-9]*)", re.I),
        evidence_name="codex-cli-nonzero",
    )
