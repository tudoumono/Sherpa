from __future__ import annotations

import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from ui_automation.support.chat import (
    answer_event,
    assert_node_status_lifecycle,
    assert_real_ai_result,
)
from ui_automation.support.chat_flow import (
    last_assistant_message,
    prepare_chat,
    start_turn_from_ui,
    wait_for_completed_ui,
)
from ui_automation.support.database import (
    conversation_database_snapshot,
    usage_event_checkpoint,
    usage_events_after,
)


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def _service_log_checkpoint(ui_config) -> dict[str, int]:
    assert ui_config.expected_env_path is not None
    root = ui_config.expected_env_path.parent.parent / "services"
    return {str(path): path.stat().st_size for path in root.glob("app*.log") if path.is_file()}


def _service_log_since(checkpoint: dict[str, int]) -> str:
    chunks = []
    for raw_path, offset in checkpoint.items():
        path = Path(raw_path)
        if path.is_file():
            chunks.append(path.read_bytes()[offset:].decode("utf-8", errors="replace"))
    return "\n".join(chunks)


@pytest.mark.subagent_planner_real
@pytest.mark.destructive
def test_real_openai_planner_delegates_to_real_subagent(admin_page, live_api, ui_config, artifact_case, isolated_stack, real_world):
    contract = ui_config.expected_environment()
    expected = contract.get("expected") or {}
    assert str(expected.get("agent") or "").lower() == "openai", "planner profile must use OpenAI as its real main agent"
    assert expected.get("subagents_enabled") in {True, "1", 1}, "planner profile did not declare real subagent execution enabled"
    admin_before = live_api.get_json("/admin/settings", save_as="state/planner-admin-settings-before.json")
    settings_before = live_api.get_json("/settings", save_as="state/planner-user-settings-before.json")
    assert str(settings_before.get("agent") or "").lower() == "openai"
    original_profiles = (admin_before.get("subagent_profiles") or {}).get("configured")
    artifact_case.add_cleanup(
        "restore subagent profiles",
        lambda: live_api.put_json("/admin/settings", {"subagent_profiles": original_profiles}),
    )
    artifact_case.add_cleanup(
        "restore planner preference",
        lambda: live_api.put_json(
            "/settings",
            {
                "sub_planner": settings_before.get("sub_planner") or "",
                "sub_profile": settings_before.get("sub_profile") or "",
            },
        ),
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#subagent-profiles-card")).to_be_visible()
    admin_page.locator("#subagent-profile-add").click()
    row = admin_page.locator("#subagent-profiles-list .sap-row").last
    expect(row).to_be_visible()
    row.locator(".sap-id").fill("ui-openai-researcher")
    row.locator(".sap-name").fill("UI実資料調査役")
    row.locator(".sap-provider").select_option("openai")
    row.locator(".sap-model").fill("gpt-4o-mini")
    row.locator(".sap-description").fill("税率、原本、COBOL、夜間処理を全文検索と精読で根拠付き調査する")
    for tool in ("list_docs", "ripgrep_search", "read_around", "es_search"):
        row.locator(f'[data-sap-tool="{tool}"]').check()
    row.locator(".sap-min-citations").fill("1")
    row.locator(".sap-max-turns").fill("4")
    row.locator(".sap-llm-timeout").fill("120")
    artifact_case.screenshot(admin_page, 10, "planner-admin-openai-subagent-profile-ready")
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
        timeout=ui_config.timeout_ms,
    ) as admin_save:
        admin_page.locator("#save").click()
    assert admin_save.value.status == 200, admin_save.value.text()
    saved_profiles = (admin_save.value.json().get("subagent_profiles") or {}).get("effective") or []
    assert any(
        profile.get("id") == "ui-openai-researcher" and profile.get("provider") == "openai" and profile.get("enabled") is True
        for profile in saved_profiles
    ), "admin UI did not persist the real OpenAI subagent profile"

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    expect(admin_page.locator("#subagent-card")).to_be_visible()
    expect(admin_page.locator('#sub_profile option[value="ui-openai-researcher"]')).to_have_count(1)
    admin_page.locator("#sub_profile").select_option("")
    admin_page.locator("#sub_planner").check()
    artifact_case.screenshot(admin_page, 20, "planner-user-auto-planning-enabled")
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/settings"),
        timeout=ui_config.timeout_ms,
    ) as user_save:
        admin_page.locator("#save").click()
    assert user_save.value.status == 200, user_save.value.text()
    saved_settings = user_save.value.json()
    assert saved_settings.get("sub_planner") == "auto"
    assert str(saved_settings.get("agent") or "").lower() == "openai"

    plan_checkpoint = usage_event_checkpoint(ui_config.database_url, "chat-plan")
    sub_checkpoint = usage_event_checkpoint(ui_config.database_url, "chat-sub")
    log_checkpoint = _service_log_checkpoint(ui_config)
    prepare_chat(admin_page, ui_config, real_world)
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        (
            "UI実資料調査役へ委譲し、SHERPA-LIVE-ALPHA-927、TAXCALC、NIGHTLYの関係を"
            "原本の全文検索と精読で調べ、税率と運用時刻を根拠ファイル付きで回答してください。"
        ),
    )
    conversation_id = int(started["conversation_id"])
    turn_id = str(started["turn_id"])
    artifact_case.add_cleanup(
        f"delete planner conversation {conversation_id}",
        lambda: _cleanup_conversation(live_api, conversation_id),
    )
    events = live_api.collect_sse(f"/chat/turns/{turn_id}/stream?cursor=0")
    answer_event(events)
    lifecycle = assert_node_status_lifecycle(events)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
    conversation = live_api.get_json(
        f"/conversations/{conversation_id}",
        save_as="state/planner-conversation.json",
    )
    assistant = last_assistant_message(conversation)
    assert_real_ai_result(
        saved_settings,
        events,
        assistant,
        require_tool=True,
        evidence=artifact_case,
        turn_id=turn_id,
        conversation_id=conversation_id,
        database_url=ui_config.database_url,
        checkpoint=started["_real_ai_checkpoint"],
        operation="chat-planner-main",
    )
    node_events = [event for event in events if event.get("type") == "node"]
    assert any(event.get("id") == "plan" for event in node_events), "real planner emitted no structured plan node"
    delegated = [event for event in node_events if str(event.get("id") or "").startswith("sub:ui-openai-researcher:")]
    assert delegated and any(event.get("kind") == "tool" for event in delegated), (
        "planner did not execute a real tool through the configured subagent"
    )
    answer = assistant.get("answer") or {}
    answer_text = str(answer.get("headline") or assistant.get("content") or "")
    citations = (answer.get("data") or {}).get("citations") or []
    citation_docs = {str(item.get("doc_id") or "") for item in citations if isinstance(item, dict)}
    scope = answer.get("scope") or {}
    assert "12.5" in answer_text and "02:15" in answer_text, "real planner/subagent result omitted deterministic tax or operations facts"
    assert citations and "specs/tax-policy.md" in citation_docs, "real planner/subagent result did not cite the authoritative tax policy"
    assert any(doc in citation_docs for doc in {"office/nightly-operations.pptx", "media/text-evidence.pdf"}), (
        "real planner/subagent result did not cite an ingested source containing 02:15"
    )
    assert scope.get("world") == real_world
    assert scope.get("source") == "all" and not (scope.get("scope_paths") or []), (
        "planner result did not persist the requested full-World scope"
    )
    usage_subs = answer.get("usage_subs") or ([answer["usage_sub"]] if answer.get("usage_sub") else [])
    assert usage_subs and any(
        row.get("profile") == "ui-openai-researcher"
        and row.get("provider") == "openai"
        and int(row.get("input_tokens") or 0) + int(row.get("output_tokens") or 0) > 0
        for row in usage_subs
    ), "real delegated usage is absent from the persisted answer"

    plan_usage = usage_events_after(ui_config.database_url, "chat-plan", plan_checkpoint, world=real_world)
    sub_usage = usage_events_after(ui_config.database_url, "chat-sub", sub_checkpoint, world=real_world)
    assert len(plan_usage) == 1 and plan_usage[0].get("provider") == "openai", plan_usage
    assert sub_usage and all(row.get("provider") == "openai" for row in sub_usage), sub_usage
    for row in plan_usage:
        artifact_case.record_usage_event(
            row,
            turn_id=f"{turn_id}:plan:{row['id']}",
            operation="chat-plan",
        )
    for row in sub_usage:
        artifact_case.record_usage_event(
            row,
            turn_id=f"{turn_id}:sub:{row['id']}",
            operation="chat-sub",
        )

    database = conversation_database_snapshot(ui_config.database_url, conversation_id, artifact_case)
    assert any(row.get("action") == "chat.turn" and row.get("outcome") == "success" for row in database["audit"])
    new_logs = _service_log_since(log_checkpoint)
    assert not re.search(r"sub_planner:.*縮退", new_logs), "the real planner silently degraded after the request was issued"
    artifact_case.write_json(
        "state/subagent-planner-correlation.json",
        {
            "turn_id": turn_id,
            "conversation_id": conversation_id,
            "lifecycle": lifecycle,
            "plan_node_count": sum(event.get("id") == "plan" for event in node_events),
            "delegated_node_count": len(delegated),
            "delegated_tool_count": sum(event.get("kind") == "tool" for event in delegated),
            "usage_subs": usage_subs,
            "chat_plan_usage_ids": [row["id"] for row in plan_usage],
            "chat_sub_usage_ids": [row["id"] for row in sub_usage],
            "grounded_tax_and_time_seen": True,
            "citation_docs": sorted(citation_docs),
            "scope": scope,
            "planner_degradation_log_match": False,
        },
    )
    sub_usage_details = admin_page.locator("#messages .msg:not(.user) .usage-sub-meta").last
    sub_usage_summary = sub_usage_details.locator("summary")
    sub_usage_body = sub_usage_details.locator(".usage-detail")
    expect(sub_usage_details).to_be_visible()
    artifact_case.arm_unkeyed_control(
        sub_usage_summary,
        control_key="@unkeyed:web/chat/render.js:212:summary",
    )
    sub_usage_summary.click()
    expect(sub_usage_details).to_have_attribute("open", "")
    expect(sub_usage_body).to_be_visible()
    expect(sub_usage_body).to_contain_text("ui-openai-researcher")
    for persisted in usage_subs:
        if persisted.get("profile") != "ui-openai-researcher":
            continue
        expect(sub_usage_body).to_contain_text(f"入力 {int(persisted.get('input_tokens') or 0):,}")
        expect(sub_usage_body).to_contain_text(f"出力 {int(persisted.get('output_tokens') or 0):,}")
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/chat/render.js:212:summary",
        state="normal",
        assertion="展開した下調べ役usageのprofileとtoken数が同じ実turnの保存済み委譲usageと一致した",
    )
    artifact_case.screenshot(admin_page, 25, "planner-subagent-usage-details-open-and-correlated")

    artifact_case.arm_unkeyed_control(
        sub_usage_summary,
        control_key="@unkeyed:web/chat/render.js:212:summary",
    )
    sub_usage_summary.click()
    expect(sub_usage_details).not_to_have_attribute("open", "")
    expect(sub_usage_body).to_be_hidden()
    assert admin_page.locator("#messages .msg.user .usage-sub-meta").count() == 0
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/chat/render.js:212:summary",
        state="abnormal",
        assertion="委譲usageを閉じた後に実質問側や別messageへ下調べ役の数値を表示中として残さなかった",
    )
    artifact_case.screenshot(admin_page, 27, "planner-subagent-usage-details-closed-without-cross-turn-leak")

    # The native summary also identifies its parent dynamic control.  Exercise
    # it separately from the source-line @unkeyed contract so both contracts
    # retain one trusted action and one explicit state assertion apiece.
    sub_usage_summary.click()
    expect(sub_usage_details).to_have_attribute("open", "")
    expect(sub_usage_body).to_be_visible()
    for persisted in usage_subs:
        if persisted.get("profile") != "ui-openai-researcher":
            continue
        expect(sub_usage_body).to_contain_text(f"入力 {int(persisted.get('input_tokens') or 0):,}")
        expect(sub_usage_body).to_contain_text(f"出力 {int(persisted.get('output_tokens') or 0):,}")
    artifact_case.attest_control_state(
        control_key="@selector:.usage-sub-meta",
        state="normal",
        assertion="下調べ役usage controlが同じ実turnの保存済みprofileとtoken数だけを展開表示した",
    )
    artifact_case.screenshot(admin_page, 28, "planner-subagent-usage-control-open-and-db-correlated")

    sub_usage_summary.click()
    expect(sub_usage_details).not_to_have_attribute("open", "")
    expect(sub_usage_body).to_be_hidden()
    assert admin_page.locator("#messages .msg:not(.user) .usage-sub-meta[open]").count() == 0
    assert admin_page.locator("#messages .msg.user .usage-sub-meta").count() == 0
    artifact_case.attest_control_state(
        control_key="@selector:.usage-sub-meta",
        state="abnormal",
        assertion="下調べ役usage controlを閉じた後に別messageや質問側へ委譲token表示を残さなかった",
    )
    artifact_case.screenshot(admin_page, 29, "planner-subagent-usage-control-closed-without-message-leak")
    artifact_case.screenshot(admin_page, 30, "planner-real-plan-subagent-tools-usage-and-audit-correlated")
