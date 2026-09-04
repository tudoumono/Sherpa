from __future__ import annotations

import re
import time

import pytest
from playwright.sync_api import expect

from ui_automation.support.chat import (
    answer_event,
    assert_node_status_lifecycle,
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
from ui_automation.support.control import restart_application
from ui_automation.support.database import (
    conversation_database_snapshot,
    real_ai_turn_checkpoint,
)


pytestmark = [pytest.mark.ui_automation, pytest.mark.thought_flow, pytest.mark.destructive]


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def _wait_until_running(api, turn_ids: set[str], timeout_seconds: float) -> list[dict]:
    deadline = time.monotonic() + timeout_seconds
    latest: list[dict] = []
    while time.monotonic() < deadline:
        latest = api.get_json("/chat/turns/running").get("turns") or []
        observed = {str(row.get("turn_id")) for row in latest}
        if turn_ids <= observed:
            return latest
        time.sleep(0.2)
    raise AssertionError(f"turns did not remain active long enough for the runtime check: {len(latest)}")


def _message_projection(message: dict) -> dict:
    return {
        "id": int(message["id"]),
        "role": str(message.get("role") or ""),
        "content": str(message.get("content") or ""),
        "trace": message.get("trace") or [],
        "answer": message.get("answer") or {},
    }


def test_confirmation_card_persists_and_resumes_real_turn(admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack):
    settings = live_api.get_json("/settings", save_as="state/confirmation-settings.json")
    prepare_chat(admin_page, ui_config, real_world)
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        (
            "確認してから進めてください。対象は SHERPA-LIVE-ALPHA-927 の税率、端数処理、"
            "NIGHTLY からの呼出関係です。実資料と実ツールで調査してください。"
        ),
    )
    cid, first_tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    expect(admin_page.locator("#rt")).to_contain_text("確認待ち", timeout=ui_config.timeout_ms)
    first_events = live_api.collect_sse(
        f"/chat/turns/{first_tid}/stream?cursor=0",
        save_as="network/confirmation-question-sse.jsonl",
    )
    questions = [event for event in first_events if event.get("type") == "question"]
    assert len(questions) == 1, "confirm-first turn did not emit exactly one structured question"
    assert not any(event.get("type") in {"answer", "error"} for event in first_events), (
        "confirmation turn incorrectly completed or errored before user input"
    )
    artifact_case.screenshot(admin_page, 10, "thought-confirmation-card-waiting-for-user")

    first_conversation = live_api.get_json(
        f"/conversations/{cid}",
        save_as="state/confirmation-before-answer.json",
    )
    conversation_database_snapshot(
        ui_config.database_url,
        cid,
        artifact_case,
        turn_id=first_tid,
    )
    first_assistant = last_assistant_message(first_conversation)
    question = (first_assistant.get("answer") or {}).get("question") or {}
    assert question.get("interaction_id") == questions[0].get("interaction_id")
    assert str(question.get("interaction_id") or "").startswith("confirm-")

    admin_page.goto(ui_config.base_url + f"/ui/chat.html?conv={cid}")
    restored = admin_page.locator("#messages .askcard").last
    expect(restored).to_be_visible()
    expect(restored).to_contain_text("何を確認してから進めますか")
    restored.locator("[data-qopt]").first.check()
    restored.locator("[data-qfree]").fill("根拠ファイル名と呼出関係を明記する")
    resume_checkpoint = real_ai_turn_checkpoint(ui_config.database_url, ui_config.expected_env_path)
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/chat/turns"),
        timeout=ui_config.timeout_ms,
    ) as resume_info:
        restored.locator("[data-ask-submit]").click()
    assert resume_info.value.status == 200, resume_info.value.text()
    resumed = resume_info.value.json()
    second_tid = str(resumed.get("turn_id") or "")
    assert int(resumed.get("conversation_id") or 0) == cid and second_tid != first_tid
    second_events = live_api.collect_sse(
        f"/chat/turns/{second_tid}/stream?cursor=0",
        save_as="network/confirmation-resumed-sse.jsonl",
    )
    answer_event(second_events)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
    conversation = live_api.get_json(
        f"/conversations/{cid}",
        save_as="state/confirmation-after-answer.json",
    )
    assistant = last_assistant_message(conversation)
    assert_real_ai_result(
        settings,
        second_events,
        assistant,
        require_tool=True,
        evidence=artifact_case,
        turn_id=second_tid,
        conversation_id=cid,
        database_url=ui_config.database_url,
        checkpoint=resume_checkpoint,
    )
    answer_text = str(assistant.get("answer") or assistant.get("content") or "")
    assert "12.5" in answer_text, "resumed real turn did not return the fixture fact"
    artifact_case.attest_control_state(
        control_key="@selector:[data-qopt]",
        state="normal",
        assertion="確認cardの選択肢が同じconversationの再開turnへ結び付き実回答を完了した",
    )
    assert "12.5" in answer_text
    artifact_case.attest_control_state(
        control_key="@selector:[data-qfree]",
        state="normal",
        assertion="確認cardの自由入力を含む再開turnがfixture固有事実を実回答した",
    )
    expect(admin_page.locator("#messages .askcard").first).to_have_class(re.compile(r"\banswered\b"))
    expect(admin_page.locator("#messages .askcard").first.locator("input").first).to_be_disabled()
    artifact_case.attest_control_state(
        control_key="@selector:[data-ask-submit]",
        state="normal",
        assertion="確認回答の送信後にcardをansweredかつ入力不可へ変更し同じturnを完了した",
    )
    conversation_database_snapshot(
        ui_config.database_url,
        cid,
        artifact_case,
        turn_id=second_tid,
    )
    artifact_case.screenshot(admin_page, 20, "thought-confirmation-answered-and-real-turn-complete")


def test_real_concurrent_turn_limit_has_no_rejected_conversation_side_effect(
    admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack
):
    before = live_api.get_json("/conversations")
    assert isinstance(before, list)
    prepare_chat(admin_page, ui_config, real_world)
    first = start_turn_from_ui(
        admin_page,
        ui_config,
        "World内の全原本を実ツールで順に確認し、税率と夜間処理の関係を詳細に整理してください。",
    )
    first_cid, first_tid = int(first["conversation_id"]), str(first["turn_id"])
    artifact_case.add_cleanup(
        f"delete conversation {first_cid}",
        lambda: _cleanup_conversation(live_api, first_cid),
    )
    second = live_api.post_json(
        "/chat/turns",
        {
            "message": "全資料を実ツールで確認して、TAXCALCに関係する証跡を詳細に列挙してください。",
            "world": real_world,
            "knowledge": True,
        },
    )
    second_cid, second_tid = int(second["conversation_id"]), str(second["turn_id"])
    artifact_case.add_cleanup(
        f"delete conversation {second_cid}",
        lambda: _cleanup_conversation(live_api, second_cid),
    )
    running = _wait_until_running(
        live_api,
        {first_tid, second_tid},
        max(ui_config.timeout_ms / 1000, 10),
    )
    artifact_case.write_json("state/concurrent-running.json", {"running_count": len(running)})

    rejected = live_api.request(
        "POST",
        "/chat/turns",
        {
            "message": "この要求は同時実行上限で拒否され、会話を作ってはいけません。",
            "world": real_world,
            "knowledge": True,
        },
        expected=429,
    )
    assert rejected.status == 429, "third real turn was not rejected at the per-user limit"
    after = live_api.get_json("/conversations")
    assert isinstance(after, list) and len(after) == len(before) + 2, "the rejected turn created a conversation side effect"
    for tid in (first_tid, second_tid):
        stopped = live_api.post_json(f"/chat/turns/{tid}/stop", {})
        assert stopped.get("ok") is True, "an admitted turn could not be stopped after limit check"
    first_events = live_api.collect_sse(
        f"/chat/turns/{first_tid}/stream?cursor=0",
        save_as="network/concurrent-first-sse.jsonl",
    )
    second_events = live_api.collect_sse(
        f"/chat/turns/{second_tid}/stream?cursor=0",
        save_as="network/concurrent-second-sse.jsonl",
    )
    assert first_events[-1].get("type") == "stopped"
    assert second_events[-1].get("type") == "stopped"
    running_after_stop = live_api.get_json(
        "/chat/turns/running",
        save_as="state/concurrent-running-after-stop.json",
    )
    assert running_after_stop.get("turns") == [], "stopped admitted turns remained in /chat/turns/running"
    conversation_database_snapshot(
        ui_config.database_url,
        first_cid,
        artifact_case,
        turn_id=first_tid,
    )
    conversation_database_snapshot(
        ui_config.database_url,
        second_cid,
        artifact_case,
        turn_id=second_tid,
    )
    expect(admin_page.locator("#rt")).to_contain_text("停止", timeout=ui_config.timeout_ms)
    artifact_case.screenshot(admin_page, 10, "thought-concurrent-limit-rejected-third-and-stopped-admitted")


def test_eventsource_disconnect_reloads_and_replays_same_real_turn(
    admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack
):
    settings = live_api.get_json("/settings", save_as="state/reconnect-settings.json")
    prepare_chat(admin_page, ui_config, real_world)
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        "World内の全資料を実ツールで順に調査し、税率・端数・TAXCALC・NIGHTLYの関係を詳細に説明してください。",
    )
    cid, tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    _wait_until_running(live_api, {tid}, max(ui_config.timeout_ms / 1000, 10))
    admin_page.wait_for_function(
        "() => window.__sherpaChatTest && window.__sherpaChatTest.es",
        timeout=ui_config.timeout_ms,
    )
    expect(admin_page.locator("#flow details.fturn .fstep").first).to_be_visible(timeout=ui_config.timeout_ms)
    pre_disconnect_nodes = ui_trace_nodes(admin_page)
    assert pre_disconnect_nodes, "disconnect occurred before the browser observed any execution event"
    artifact_case.allow_request_failure(
        method="GET",
        path=f"/chat/turns/{tid}/stream",
        resource_type="eventsource",
        failure="net::ERR_ABORTED",
        reason="this case intentionally closes the active real EventSource exactly once to verify replay",
    )
    admin_page.evaluate(
        """() => {
          const seam = window.__sherpaChatTest;
          if (!seam || !seam.es) throw new Error('active EventSource is absent');
          seam.es.close();
          seam.es = null;
        }"""
    )
    still_running = live_api.get_json(
        "/chat/turns/running",
        save_as="state/running-after-browser-disconnect.json",
    )
    assert any(str(row.get("turn_id")) == tid for row in still_running.get("turns") or []), (
        "closing the browser subscription stopped the server turn"
    )
    artifact_case.screenshot(admin_page, 10, "thought-eventsource-disconnected-turn-still-running")

    admin_page.goto(ui_config.base_url + f"/ui/chat.html?conv={cid}")
    expect(admin_page.locator("#send")).to_have_attribute("title", "停止", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#rt")).to_contain_text("リアルタイム", timeout=ui_config.timeout_ms)
    artifact_case.screenshot(admin_page, 20, "thought-eventsource-reconnected-by-conversation-reload")
    events = live_api.collect_sse(
        f"/chat/turns/{tid}/stream?cursor=0",
        save_as="network/reconnected-full-replay-sse.jsonl",
    )
    answer_event(events)
    assert events[-1].get("type") == "answer", "full replay did not end at the one terminal answer"
    replay_cursor = len(events) // 2
    replayed_events = live_api.collect_sse(
        f"/chat/turns/{tid}/stream?cursor={replay_cursor}",
        save_as="network/reconnected-cursor-suffix-sse.jsonl",
    )
    cursor_correlation = assert_sse_cursor_replay(events, replayed_events, cursor=replay_cursor)
    artifact_case.write_json("state/reconnected-cursor-suffix-correlation.json", cursor_correlation)
    lifecycle = assert_node_status_lifecycle(events)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
    conversation = live_api.get_json(
        f"/conversations/{cid}",
        save_as="state/reconnected-conversation.json",
    )
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
    assert int(events[-1].get("conversation_id") or 0) == cid
    ui_nodes = ui_trace_nodes(admin_page)
    assert_trace_correlation(events, assistant, ui_nodes)
    pre_disconnect_labels = [str(node.get("label") or "").strip() for node in pre_disconnect_nodes]
    replayed_labels = [str(node.get("label") or "").strip() for node in ui_nodes]
    assert pre_disconnect_labels == replayed_labels[: len(pre_disconnect_labels)], (
        "nodes observed before disconnect are not an ordered prefix of the completed replay; "
        "the reconnect omitted or reordered an already visible execution step"
    )

    database = conversation_database_snapshot(ui_config.database_url, cid, artifact_case, turn_id=tid)
    api_messages = [_message_projection(row) for row in conversation.get("messages") or []]
    db_messages = [_message_projection(row) for row in database.get("messages") or []]
    assert api_messages == db_messages, "reconnected conversation API messages differ from Postgres or changed order"
    message_ids = [row["id"] for row in api_messages]
    assert len(message_ids) == len(set(message_ids)), "reconnect persisted a duplicate message identifier"
    assert [row["role"] for row in api_messages] == ["user", "assistant"], (
        "single reconnected turn did not persist exactly one user and one assistant message"
    )

    rendered_messages = admin_page.locator("#messages .msg")
    expect(rendered_messages).to_have_count(len(api_messages))
    expect(admin_page.locator("#messages .thinking")).to_have_count(0)
    rendered_users = admin_page.locator("#messages .msg.user .bubble-user").all_inner_texts()
    assert rendered_users == [api_messages[0]["content"]], "reconnect duplicated, omitted, or reordered the user message"
    rendered_assistants = admin_page.locator("#messages .msg:not(.user)")
    expect(rendered_assistants.locator(".headline")).to_have_count(1)
    rendered_answers = rendered_assistants.evaluate_all("els => els.map(el => el._answer || null)")
    assert rendered_answers == [api_messages[1]["answer"]], (
        "reconnect rendered an assistant payload different from the API/Postgres message"
    )
    # The browser replays the complete SSE node set, whereas Postgres stores
    # the cap-policy representation. ``assert_trace_correlation`` above has
    # already compared each rendered node to the complete stream and the
    # persisted representation to that same source.  Requiring the UI count to
    # equal the DB count would reject a correctly capped trace.
    flow_turn_count = admin_page.locator("#flow > .fturn").count()
    expected_turn_count = sum(row["role"] == "user" for row in api_messages)
    artifact_case.write_json(
        "state/reconnect-replay-correlation.json",
        {
            "event_types": [str(event.get("type") or "") for event in events],
            "node_lifecycle": lifecycle,
            "api_message_ids": message_ids,
            "database_message_ids": [row["id"] for row in db_messages],
            "rendered_message_count": rendered_messages.count(),
            "rendered_trace_node_count": len(ui_nodes),
            "persisted_trace_node_count": len(assistant.get("trace") or []),
            "pre_disconnect_trace_node_count": len(pre_disconnect_nodes),
            "pre_disconnect_trace_labels": pre_disconnect_labels,
            "flow_turn_count": flow_turn_count,
            "expected_turn_count": expected_turn_count,
            "terminal_answer_count": sum(event.get("type") == "answer" for event in events),
        },
    )
    artifact_case.screenshot(admin_page, 30, "thought-replayed-turn-complete-with-correlated-answer")
    assert flow_turn_count == expected_turn_count, (
        f"reconnect duplicated or omitted a thought-flow turn container: rendered={flow_turn_count} persisted={expected_turn_count}"
    )

    streamed_detail_histories: dict[str, dict] = {}
    for event in events:
        if event.get("type") != "node" or not event.get("id"):
            continue
        node_id = str(event["id"])
        history = streamed_detail_histories.setdefault(
            node_id,
            {"id": node_id, "label": "", "details": []},
        )
        history["label"] = str(event.get("label") or "").strip()
        detail = str(event.get("detail") or "")
        if detail and (not history["details"] or detail != history["details"][-1]):
            history["details"].append(detail)
    expected_history_rows = [
        {
            "id": history["id"],
            "label": history["label"],
            "history": history["details"][:-1],
            "detail_count": len(history["details"]),
        }
        for history in streamed_detail_histories.values()
        if len(history["details"]) >= 2
    ]
    assert expected_history_rows, "real replay emitted no changed node detail with which to exercise .fhist"
    history_buttons = admin_page.locator("#flow details.fturn .fstep .fhist")
    expect(history_buttons).to_have_count(len(expected_history_rows))
    rendered_history_rows = history_buttons.evaluate_all(
        """buttons => buttons.map(button => {
          const step = button.closest('.fstep');
          return {
            label: (step.querySelector('.flabel') || {}).textContent.trim(),
            history: Array.from(step.querySelectorAll('.fhist-list .fhist-item'), item => item.textContent),
            indicator: (button.querySelector('.fhtxt') || {}).textContent.trim(),
          };
        })"""
    )
    assert [row["label"] for row in rendered_history_rows] == [row["label"] for row in expected_history_rows]
    assert [row["history"] for row in rendered_history_rows] == [row["history"] for row in expected_history_rows]
    assert [row["indicator"] for row in rendered_history_rows] == [f"履歴 {row['detail_count']}" for row in expected_history_rows]
    artifact_case.write_json(
        "state/reconnected-node-detail-history-correlation.json",
        {
            "turn_id": tid,
            "streamed": expected_history_rows,
            "rendered": rendered_history_rows,
            "exact_order_and_history_match": True,
        },
    )

    history_toggle = history_buttons.first
    history_list = history_toggle.locator("xpath=ancestor::*[contains(concat(' ', normalize-space(@class), ' '), ' fstep ')][1]").locator(
        ".fhist-list"
    )
    expect(history_toggle).to_have_attribute("aria-expanded", "false")
    expect(history_list).to_be_hidden()
    history_toggle.click()
    expect(history_toggle).to_have_attribute("aria-expanded", "true")
    expect(history_list).to_be_visible()
    assert history_list.locator(".fhist-item").all_inner_texts() == expected_history_rows[0]["history"]
    artifact_case.attest_control_state(
        control_key="@selector:.fhist",
        state="normal",
        assertion="展開した実nodeの過去detail列が同じturnの生SSE履歴と順序を含め完全一致した",
    )
    artifact_case.screenshot(admin_page, 40, "thought-node-detail-history-open-and-sse-correlated")

    history_toggle.click()
    expect(history_toggle).to_have_attribute("aria-expanded", "false")
    expect(history_list).to_be_hidden()
    assert admin_page.locator("#flow .fstep.hist-open").count() == 0
    artifact_case.attest_control_state(
        control_key="@selector:.fhist",
        state="abnormal",
        assertion="履歴を閉じた後は他nodeを含む全detail履歴を展開状態として残さなかった",
    )
    artifact_case.screenshot(admin_page, 50, "thought-node-detail-history-closed-without-cross-node-leak")

    trace_turn = admin_page.locator("#flow details.fturn").last
    turn_header = trace_turn.locator(":scope > summary.fturn-head")
    turn_body = trace_turn.locator(":scope > .fturn-body")
    expect(turn_header.locator(".fturn-q")).to_have_text(api_messages[0]["content"][:40])
    expect(trace_turn).to_have_attribute("open", "")
    turn_header.click()
    expect(trace_turn).not_to_have_attribute("open", "")
    expect(turn_body).to_be_hidden()
    assert admin_page.locator("#flow details.fturn[open]").count() == 0
    artifact_case.attest_control_state(
        control_key="@selector:.fturn-head",
        state="abnormal",
        assertion="選択turnを閉じると同じ会話内の別turnも含めtrace本文を開いた状態にしなかった",
    )
    artifact_case.screenshot(admin_page, 60, "thought-replayed-turn-header-closed-without-other-trace")

    turn_header.click()
    expect(trace_turn).to_have_attribute("open", "")
    expect(turn_body).to_be_visible()
    expect(turn_body.locator(":scope > .fstep")).to_have_count(len(ui_nodes))
    artifact_case.attest_control_state(
        control_key="@selector:.fturn-head",
        state="normal",
        assertion="選択turnを再展開すると同じ生SSEと相関済みの実行nodeだけを全件表示した",
    )
    artifact_case.screenshot(admin_page, 70, "thought-replayed-turn-header-open-with-correlated-trace")

    turn_header.click()
    expect(trace_turn).not_to_have_attribute("open", "")
    expect(turn_body).to_be_hidden()
    assert admin_page.locator("#flow details.fturn[open]").count() == 0
    artifact_case.attest_control_state(
        control_key="@selector:.fturn",
        state="abnormal",
        assertion="実turn containerを閉じた状態で別turnのtrace本文を表示対象として残さなかった",
    )
    artifact_case.screenshot(admin_page, 80, "thought-replayed-turn-container-closed")

    turn_header.click()
    expect(trace_turn).to_have_attribute("open", "")
    expect(turn_body.locator(":scope > .fstep")).to_have_count(len(ui_nodes))
    artifact_case.attest_control_state(
        control_key="@selector:.fturn",
        state="normal",
        assertion="実turn containerの再展開後も同じ質問と相関済みnode件数を保持した",
    )
    artifact_case.screenshot(admin_page, 90, "thought-replayed-turn-container-reopened")


def test_server_restart_does_not_mark_interrupted_turn_complete(admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack):
    prepare_chat(admin_page, ui_config, real_world)
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        "World内の全資料を実ツールで順に調査し、詳細な監査向け報告を作成してください。",
    )
    cid, tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    _wait_until_running(live_api, {tid}, max(ui_config.timeout_ms / 1000, 10))
    artifact_case.allow_request_failure(
        method="GET",
        path=f"/chat/turns/{tid}/stream",
        resource_type="eventsource",
        failure="net::ERR_INCOMPLETE_CHUNKED_ENCODING",
        reason="this case intentionally restarts the real application while exactly one EventSource is active",
    )
    artifact_case.screenshot(admin_page, 10, "thought-turn-running-before-real-server-restart")
    restart = restart_application(ui_config, artifact_case)

    running = live_api.get_json(
        "/chat/turns/running",
        save_as="state/running-after-server-restart.json",
    )
    assert running.get("turns") == [], "in-memory turn registry survived an application restart"
    old_stream = live_api.request("GET", f"/chat/turns/{tid}/stream?cursor=0", expected=404)
    assert old_stream.status == 404, "interrupted turn remained falsely resumable"
    conversation = live_api.get_json(
        f"/conversations/{cid}",
        save_as="state/interrupted-conversation.json",
    )
    messages = conversation.get("messages") or []
    database = conversation_database_snapshot(ui_config.database_url, cid, artifact_case, turn_id=tid)
    success_audits = [
        row
        for row in database["audit"]
        if int(row["id"]) > int(started["_real_ai_checkpoint"]["audit_id"])
        and row.get("action") == "chat.turn"
        and row.get("outcome") == "success"
    ]
    artifact_case.write_json(
        "state/interrupted-turn-contract.json",
        {
            "app_start_count": restart.get("app_start_count"),
            "running_after_restart": 0,
            "old_stream_status": old_stream.status,
            "last_persisted_role": messages[-1].get("role") if messages else None,
            "successful_chat_turn_audit_count": len(success_audits),
        },
    )
    assert [row.get("role") for row in messages] == ["user"], (
        "application restart incorrectly persisted an assistant success for the interrupted turn"
    )
    assert [row.get("role") for row in database["messages"]] == ["user"], "Postgres contains a non-user row for the interrupted turn"
    assert not success_audits, "application restart emitted a successful chat.turn audit for an interrupted turn"

    admin_page.goto(ui_config.base_url + f"/ui/chat.html?conv={cid}")
    expect(admin_page.locator("#send")).to_have_attribute("title", "送信")
    expect(admin_page.locator("#rt")).not_to_contain_text("完了")
    artifact_case.screenshot(admin_page, 20, "thought-restarted-server-shows-interrupted-not-complete")
