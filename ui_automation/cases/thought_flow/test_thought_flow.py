from __future__ import annotations

import pytest
from playwright.sync_api import expect

from ui_automation.support.chat import (
    answer_event,
    assert_answer_delta_correlation,
    assert_node_status_lifecycle,
    assert_persisted_trace_after_cap,
    assert_real_ai_result,
    assert_sse_cursor_replay,
    assert_trace_correlation,
)
from ui_automation.support.chat_flow import (
    last_assistant_message,
    prepare_chat,
    start_turn_from_ui,
    ui_trace_nodes,
    wait_for_completed_ui,
)
from ui_automation.support.database import conversation_database_snapshot


pytestmark = [pytest.mark.ui_automation, pytest.mark.thought_flow, pytest.mark.destructive]


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def test_sse_trace_matches_ui_api_and_database(admin_page, live_api, ui_config, artifact_case, real_world):
    settings = live_api.get_json("/settings", save_as="state/thought-flow-settings.json")
    assert settings.get("agent") != "heuristic", "structured positive flow requires a real AI"
    prepare_chat(admin_page, ui_config, real_world)
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        "SHERPA-LIVE-ALPHA-927 と TAXCALC と NIGHTLY の関係を実資料と実ツールで順に調べてください。",
    )
    cid, tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    artifact_case.screenshot(admin_page, 10, "thought-flow-question-accepted")
    events = live_api.collect_sse(f"/chat/turns/{tid}/stream?cursor=0")
    answer_event(events)
    replay_cursor = len(events) // 2
    replayed_events = live_api.collect_sse(
        f"/chat/turns/{tid}/stream?cursor={replay_cursor}",
        save_as="network/sse-cursor-replay.jsonl",
    )
    replay_correlation = assert_sse_cursor_replay(events, replayed_events, cursor=replay_cursor)
    artifact_case.write_json("state/sse-cursor-replay-correlation.json", replay_correlation)
    lifecycle = assert_node_status_lifecycle(events)
    artifact_case.write_json("state/sse-node-status-transitions.json", lifecycle)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
    nodes = ui_trace_nodes(admin_page)
    artifact_case.write_json("state/ui-trace.json", nodes)
    assert nodes and all(node["status"] == "done" for node in nodes)
    artifact_case.screenshot(admin_page, 20, "thought-flow-all-steps-complete")

    conversation = live_api.get_json(f"/conversations/{cid}", save_as="state/conversation-api.json")
    assistant = last_assistant_message(conversation)
    assert_real_ai_result(
        settings,
        events,
        assistant,
        require_tool=True,
        evidence=artifact_case,
        turn_id=tid,
        conversation_id=cid,
        database_url=ui_config.database_url,
        checkpoint=started["_real_ai_checkpoint"],
    )
    delta_correlation = assert_answer_delta_correlation(events, assistant)
    artifact_case.write_json(
        "state/answer-delta-correlation.json",
        {
            "conversation_id": cid,
            "turn_id": tid,
            "assistant_message_id": assistant["id"],
            **delta_correlation,
        },
    )
    cap_correlation = assert_persisted_trace_after_cap(events, assistant)
    artifact_case.write_json(
        "state/persisted-trace-cap-correlation.json",
        {
            "conversation_id": cid,
            "turn_id": tid,
            "assistant_message_id": assistant["id"],
            **cap_correlation,
        },
    )
    assert cap_correlation["cap_mode"] in {"none", "legacy-tail", "aggregate", "budget"}
    # A real provider decides how many execution nodes this turn needs.  A
    # normal 5--15 node turn must not be failed merely because it did not cross
    # the product's fixed 120-node cap.  ``assert_persisted_trace_after_cap``
    # still checks the exact uncapped representation and, whenever a real turn
    # does cross a limit, verifies every retained/aggregated node and omission
    # count against the full SSE stream.
    assert_trace_correlation(events, assistant, nodes)
    database = conversation_database_snapshot(ui_config.database_url, cid, artifact_case, turn_id=tid)
    db_assistant = [row for row in database["messages"] if row["role"] == "assistant"][-1]
    assert db_assistant["id"] == assistant["id"]
    assert db_assistant["trace"] == assistant["trace"]
    assert db_assistant["answer"] == assistant["answer"]
    artifact_case.screenshot(admin_page, 30, "thought-flow-sse-ui-api-db-correlated")


def test_running_turn_reload_resume_and_stop(admin_page, live_api, ui_config, artifact_case, real_world):
    settings = live_api.get_json("/settings", save_as="state/thought-flow-settings.json")
    assert settings.get("agent") != "heuristic", "resume/stop must exercise a real AI"
    prepare_chat(admin_page, ui_config, real_world)
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        "このWorldの全ファイルを実ツールで順番に確認し、参照関係と運用上の注意を詳細に整理してください。",
    )
    cid, tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    running = live_api.get_json("/chat/turns/running", save_as="state/running-before-reload.json")
    assert any(row.get("turn_id") == tid for row in running.get("turns", [])), "turn completed before resume could be tested"
    artifact_case.screenshot(admin_page, 10, "thought-flow-running-before-reload")

    admin_page.goto(ui_config.base_url + "/ui/home.html")
    running_notice = admin_page.locator("#turnnotice")
    expect(running_notice).to_be_visible(timeout=ui_config.timeout_ms)
    expect(running_notice).to_contain_text("回答作成中")
    expect(running_notice).to_have_attribute("href", f"chat.html?conv={cid}")
    artifact_case.screenshot(admin_page, 15, "thought-flow-global-running-turn-notice-visible")
    notice_authorization = artifact_case.arm_control_authorization(admin_page, control_key="turnnotice")
    assert notice_authorization["status"] == 200 and notice_authorization["role"] == "admin"
    running_notice.click()
    admin_page.wait_for_url(f"**/ui/chat.html?conv={cid}", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#send")).to_have_attribute("title", "停止")
    expect(admin_page.locator("#rt")).to_contain_text("リアルタイム")
    artifact_case.attest_control_state(
        control_key="turnnotice",
        state="normal",
        assertion="実行中noticeから同じconversationへ戻り実turnのSSE購読を再開した",
    )
    artifact_case.screenshot(admin_page, 20, "thought-flow-resubscribed-after-reload")
    admin_page.locator("#send").click()

    events = live_api.collect_sse(f"/chat/turns/{tid}/stream?cursor=0", save_as="network/stopped-sse.jsonl")
    stopped_events = [event for event in events if event.get("type") == "stopped"]
    assert len(stopped_events) == 1 and events[-1] == stopped_events[0], f"turn was not uniquely stopped: {events[-3:]}"
    assert int(stopped_events[0].get("conversation_id") or 0) == cid
    assert not any(event.get("type") in {"answer", "error"} for event in events), "stopped turn emitted an answer or error terminal"
    running_after_stop = live_api.get_json(
        "/chat/turns/running",
        save_as="state/running-after-stop.json",
    )
    assert running_after_stop.get("turns") == [], "stopped turn remained in /chat/turns/running"
    expect(admin_page.locator("#rt")).to_contain_text("停止")
    expect(admin_page.locator("#send")).to_have_attribute("title", "送信")
    conversation = live_api.get_json(f"/conversations/{cid}", save_as="state/stopped-conversation.json")
    messages = conversation.get("messages") or []
    assert messages and messages[-1]["role"] == "user", "stopped turn incorrectly persisted an assistant success"
    database = conversation_database_snapshot(ui_config.database_url, cid, artifact_case, turn_id=tid)
    assert any(row.get("action") == "chat.turn" and (row.get("detail") or {}).get("stopped") is True for row in database["audit"])
    artifact_case.screenshot(admin_page, 30, "thought-flow-stopped-without-assistant-success")
