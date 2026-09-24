from __future__ import annotations

import hashlib
import json

import pytest
from playwright.sync_api import expect

from ui_automation.support.chat import answer_event, assert_real_ai_result
from ui_automation.support.chat_flow import (
    last_assistant_message,
    prepare_chat,
    start_turn_from_ui,
    wait_for_completed_ui,
)
from ui_automation.support.database import conversation_database_snapshot


pytestmark = [pytest.mark.ui_automation, pytest.mark.chat, pytest.mark.destructive]


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def test_real_ai_chat_persists_usage_trace_and_audit(admin_page, live_api, ui_config, artifact_case, real_world):
    settings = live_api.get_json("/settings", save_as="state/chat-settings.json")
    assert settings.get("agent") != "heuristic", "positive chat must use a configured real AI"
    prepare_chat(admin_page, ui_config, real_world)
    question = "資料 SHERPA-LIVE-ALPHA-927 を実際に検索し、検証用税率、端数処理、承認者を、根拠となるファイル名とともに答えてください。"
    started = start_turn_from_ui(admin_page, ui_config, question)
    cid = int(started["conversation_id"])
    tid = str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    artifact_case.write_json(
        "state/chat-turn-start.json",
        {key: value for key, value in started.items() if not key.startswith("_")},
    )
    artifact_case.screenshot(admin_page, 10, "chat-real-question-sent")

    events = live_api.collect_sse(f"/chat/turns/{tid}/stream?cursor=0")
    answer = answer_event(events)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
    artifact_case.screenshot(admin_page, 20, "chat-real-answer-complete")

    conversation = live_api.get_json(f"/conversations/{cid}", save_as="state/conversation-api.json")
    assistant = last_assistant_message(conversation)
    assert int(answer["conversation_id"]) == cid
    assert answer["message"]["id"] == assistant["id"]
    assert "12.5" in str(assistant.get("content") or assistant.get("answer"))
    assert "tax-policy.md" in str(assistant.get("answer") or "")
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

    database = conversation_database_snapshot(ui_config.database_url, cid, artifact_case, turn_id=tid)
    assert [row["role"] for row in database["messages"]][-2:] == ["user", "assistant"]
    assert database["messages"][-2]["content"] == question
    artifact_case.attest_control_state(
        control_key="input",
        state="normal",
        assertion="入力した実質問全文が同じconversationのuser messageとしてPostgresへ永続化された",
    )
    assert any(row.get("action") == "chat.turn" and row.get("outcome") == "success" for row in database["audit"])
    artifact_case.attest_control_state(
        control_key="send",
        state="normal",
        assertion="送信した実AI turnがSSE完了しPostgres監査へsuccessとして記録された",
    )
    audit = live_api.get_json(f"/admin/audit?resource_id=conv:{cid}&limit=20", save_as="state/chat-audit-api.json")
    assert any(row.get("action") == "chat.turn" for row in audit.get("rows", []))
    usage_details = admin_page.locator("#messages .msg:not(.user) .usage-meta:not(.usage-sub-meta)").last
    usage_summary = usage_details.locator("summary")
    usage_body = usage_details.locator(".usage-detail")
    persisted_usage = (assistant.get("answer") or {}).get("usage") or {}
    input_tokens = int(persisted_usage.get("input_tokens") or 0)
    output_tokens = int(persisted_usage.get("output_tokens") or 0)
    assert input_tokens + output_tokens > 0, "real assistant message persisted no non-zero usage"
    expect(usage_details).to_be_visible()
    artifact_case.arm_unkeyed_control(
        usage_summary,
        control_key="@unkeyed:web/chat/render.js:185:summary",
    )
    usage_summary.click()
    expect(usage_details).to_have_attribute("open", "")
    expect(usage_body).to_be_visible()
    expect(usage_body).to_contain_text(f"入力トークン: {input_tokens:,}")
    expect(usage_body).to_contain_text(f"出力トークン: {output_tokens:,}")
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/chat/render.js:185:summary",
        state="normal",
        assertion="展開したusage内訳の入力出力token数が同じ実assistant messageの保存値と一致した",
    )
    artifact_case.screenshot(admin_page, 22, "chat-main-provider-usage-details-open-and-correlated")

    artifact_case.arm_unkeyed_control(
        usage_summary,
        control_key="@unkeyed:web/chat/render.js:185:summary",
    )
    usage_summary.click()
    expect(usage_details).not_to_have_attribute("open", "")
    expect(usage_body).to_be_hidden()
    assert admin_page.locator("#messages .msg.user .usage-meta").count() == 0
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/chat/render.js:185:summary",
        state="abnormal",
        assertion="内訳を閉じると実質問側へusageを漏らさず、別messageの数値を表示中として残さなかった",
    )
    artifact_case.screenshot(admin_page, 24, "chat-main-provider-usage-details-closed-without-cross-turn-leak")
    expect(admin_page.locator("#messages .sources").last).to_contain_text("tax-policy.md")

    citations = (assistant.get("answer") or {}).get("data", {}).get("citations") or []
    assert citations, "real QA answer has no citation data, so the citation UI cannot be exercised"
    citation_toggle = admin_page.locator("#messages .msg:not(.user) [data-cites]").last
    expect(citation_toggle).to_be_visible()
    citation_body = citation_toggle.locator("xpath=..").locator(".cites-body")
    expect(citation_toggle).to_have_attribute("aria-expanded", "false")
    expect(citation_body).to_be_hidden()
    citation_toggle.click()
    expect(citation_toggle).to_have_attribute("aria-expanded", "true")
    expect(citation_body).to_be_visible()
    expect(citation_body.locator(".cite")).to_have_count(len(citations))
    expect(citation_body).to_contain_text("tax-policy.md")
    artifact_case.attest_control_state(
        control_key="@selector:[data-cites]",
        state="normal",
        assertion="実回答のcitation操作でAPI由来の件数とtax-policy原本名を展開表示した",
    )
    citation_toggle.click()
    expect(citation_toggle).to_have_attribute("aria-expanded", "false")
    expect(citation_body).to_be_hidden()

    admin_page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=ui_config.base_url,
    )
    user_copy = admin_page.locator("#messages .msg.user [data-copy]").last
    expect(user_copy).to_be_visible()
    user_copy.click()
    expect(admin_page.locator("#toast")).to_contain_text("コピーしました")
    clipboard_text = admin_page.evaluate("() => navigator.clipboard.readText()")
    assert clipboard_text == question, "message copy control changed or truncated the real submitted question"
    artifact_case.attest_control_state(
        control_key="@selector:[data-copy]",
        state="normal",
        assertion="表示中の実質問全文とclipboardへコピーされた内容が完全一致した",
    )

    answer_export = admin_page.locator("#messages .msg:not(.user) [data-export]").last
    expect(answer_export).to_be_visible()
    with admin_page.expect_download(timeout=ui_config.timeout_ms) as answer_export_info:
        answer_export.click()
    exported_answer = artifact_case.case_dir / "state" / "chat-single-real-answer-export.md"
    answer_export_info.value.save_as(str(exported_answer))
    exported_answer_text = exported_answer.read_text(encoding="utf-8")
    assert "12.5" in exported_answer_text and "tax-policy.md" in exported_answer_text
    artifact_case.attest_control_state(
        control_key="@selector:[data-export]",
        state="normal",
        assertion="実回答exportにfixture固有税率と根拠ファイル名がともに含まれた",
    )
    artifact_case.write_json(
        "state/chat-message-actions.json",
        {
            "citation_count": len(citations),
            "citation_toggled_open_and_closed": True,
            "copied_user_question_exactly": True,
            "single_answer_export_sha256": hashlib.sha256(exported_answer.read_bytes()).hexdigest(),
        },
    )
    artifact_case.screenshot(admin_page, 30, "chat-usage-sources-and-audit-correlated")

    admin_page.reload()
    admin_page.wait_for_load_state("domcontentloaded")
    conversation_link = admin_page.locator(f'#convlist [data-open="{cid}"]')
    expect(conversation_link).to_be_visible()
    conversation_link.click()
    expect(admin_page.locator("#messages")).to_contain_text("12.5")
    expect(admin_page.locator("#flow .fstep.done")).not_to_have_count(0)
    trace_turn = admin_page.locator("#flow details.fturn").last
    trace_button = admin_page.locator("#messages .msg:not(.user) [data-showtrace]").last
    expect(trace_turn).to_be_visible()
    expect(trace_button).to_be_visible()
    trace_turn.evaluate("element => { element.open = false; }")
    assert trace_turn.evaluate("element => element.open") is False
    trace_button.click()
    expect(trace_turn).to_have_attribute("open", "")
    expect(trace_turn.locator(".fstep.done")).not_to_have_count(0)
    artifact_case.attest_control_state(
        control_key="@selector:[data-showtrace]",
        state="normal",
        assertion="保存済み回答のtrace操作で同じturnのdone実行nodeを展開表示した",
    )
    artifact_case.screenshot(admin_page, 40, "chat-history-and-trace-restored-after-reload")


def test_real_impact_filter_and_reference_graph_controls(admin_page, live_api, ui_config, artifact_case, real_world):
    settings = live_api.get_json("/settings", save_as="state/impact-control-settings.json")
    assert settings.get("agent") != "heuristic", "impact controls must be driven by a real AI turn"
    prepare_chat(admin_page, ui_config, real_world)
    question = (
        "TAXCALC の税率を変更した場合、NIGHTLY と関連プログラムへの影響範囲は？実グラフと実資料を使い、確実と要確認を区別してください。"
    )
    started = start_turn_from_ui(admin_page, ui_config, question)
    cid, tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    events = live_api.collect_sse(
        f"/chat/turns/{tid}/stream?cursor=0",
        save_as="network/impact-controls-sse.jsonl",
    )
    answer_event(events)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
    conversation = live_api.get_json(
        f"/conversations/{cid}",
        save_as="state/impact-controls-conversation.json",
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

    answer = assistant.get("answer") or {}
    assert answer.get("lens") == "impact", f"impact request was routed to {answer.get('lens')!r}"
    items = (answer.get("data") or {}).get("items") or []
    assert items, "real impact turn produced no structured graph items; data-toggle/data-rg controls must not be marked covered"
    graph_items = [item for item in items if len(item.get("trace") or []) >= 2]
    assert graph_items, "real impact items have no multi-node trace for the reference-graph UI"
    assistant_card = admin_page.locator("#messages .msg:not(.user)").last
    rows = assistant_card.locator(".ilist").first.locator(":scope > li")
    expect(rows).to_have_count(len(items))

    detail_index = next(
        index
        for index, item in enumerate(items)
        if (item.get("trace") or []) or any(evidence.get("doc") for evidence in item.get("evidence") or [])
    )
    detail_item = items[detail_index]
    detail_row = rows.nth(detail_index)
    detail_toggle = detail_row.locator("[data-toggle]")
    detail_body = detail_row.locator(".ixdetail")
    expected_trace = [str(name).strip() for name in detail_item.get("trace") or []]
    expected_evidence_docs = [
        str(evidence.get("doc") or "").strip() for evidence in detail_item.get("evidence") or [] if str(evidence.get("doc") or "").strip()
    ]
    expect(detail_toggle).to_be_visible()
    expect(detail_toggle).to_have_attribute("aria-expanded", "false")
    expect(detail_body).to_be_hidden()
    detail_toggle.click()
    expect(detail_toggle).to_have_attribute("aria-expanded", "true")
    expect(detail_body).to_be_visible()
    assert detail_row.evaluate("element => element.classList.contains('open')") is True
    if expected_trace:
        assert detail_body.locator(".ix-route .chip").all_inner_texts() == expected_trace
        expect(detail_body.locator(".chain")).to_have_text(" ← ".join(expected_trace))
    rendered_detail = detail_body.inner_text()
    assert all(document in rendered_detail for document in expected_evidence_docs), {
        "expected_evidence_docs": expected_evidence_docs,
        "rendered_detail": rendered_detail,
    }
    artifact_case.attest_control_state(
        control_key="@selector:[data-toggle]",
        state="normal",
        assertion="展開した実impact行のtrace順序と根拠fileが同じAPI構造化itemと一致した",
    )
    artifact_case.screenshot(admin_page, 10, "chat-impact-selected-detail-open-and-api-correlated")

    detail_toggle.click()
    expect(detail_toggle).to_have_attribute("aria-expanded", "false")
    expect(detail_body).to_be_hidden()
    assert detail_row.evaluate("element => element.classList.contains('open')") is False
    assert rows.evaluate_all("elements => elements.filter(element => element.classList.contains('open')).length") == 0
    artifact_case.attest_control_state(
        control_key="@selector:[data-toggle]",
        state="abnormal",
        assertion="選択したimpact詳細を閉じた後に別の根拠行を開いた状態として残さなかった",
    )
    artifact_case.screenshot(admin_page, 20, "chat-impact-selected-detail-closed-without-row-leak")

    graph_toggle = assistant_card.locator("[data-rg]")
    expect(graph_toggle).to_be_visible()
    graph_payload = json.loads(graph_toggle.get_attribute("data-rg") or "null")
    assert isinstance(graph_payload, dict) and len(graph_payload.get("nodes") or []) >= 2
    graph_body = graph_toggle.locator("xpath=..").locator(".refgraph-body")
    expect(graph_body).to_be_hidden()
    graph_toggle.click()
    expect(graph_body).to_be_visible()
    expect(graph_body.locator(".rg-canvas canvas").first).to_be_visible(timeout=ui_config.timeout_ms)
    graph_state = graph_body.evaluate(
        """el => ({
          nodeCount: el._cy ? el._cy.nodes().length : 0,
          edgeCount: el._cy ? el._cy.edges().length : 0,
          canvasCount: el.querySelectorAll('.rg-canvas canvas').length,
          width: el.getBoundingClientRect().width,
          height: el.getBoundingClientRect().height
        })"""
    )
    assert graph_state["nodeCount"] == len(graph_payload["nodes"]), graph_state
    assert graph_state["edgeCount"] == len(graph_payload["edges"]), graph_state
    assert graph_state["canvasCount"] > 0 and graph_state["width"] > 0 and graph_state["height"] > 0, graph_state
    artifact_case.attest_control_state(
        control_key="@selector:[data-rg]",
        state="normal",
        assertion="実回答の参照graphを開きpayloadと同数のnodeとedgeをcanvasへ描画した",
    )
    graph_toggle.click()
    expect(graph_body).to_be_hidden()
    graph_toggle.click()
    expect(graph_body).to_be_visible()
    reopened_graph_state = graph_body.evaluate(
        "el => ({nodeCount: el._cy ? el._cy.nodes().length : 0, canvasCount: el.querySelectorAll('canvas').length})"
    )
    assert reopened_graph_state["nodeCount"] == graph_state["nodeCount"]
    assert reopened_graph_state["canvasCount"] == graph_state["canvasCount"]
    artifact_case.write_json(
        "state/impact-dynamic-controls.json",
        {
            "provider": ((answer.get("usage") or {}).get("provider")),
            "item_count": len(items),
            "reference_graph": graph_state,
        },
    )
    artifact_case.screenshot(admin_page, 30, "chat-impact-detail-and-reference-graph-operated")


def test_ext23_verified_and_reference_sources(admin_page, live_api, ui_config, artifact_case, real_world):
    settings = live_api.get_json("/settings", save_as="state/chat-settings.json")
    assert settings.get("agent") != "heuristic", "positive chat must use a configured real AI"
    prepare_chat(admin_page, ui_config, real_world)
    question = "SHERPA-LIVE-ALPHA-927 の税率の正本を精読し、NIGHTLY手順は参考として確認してください。根拠と参考を混同せず回答してください。"
    started = start_turn_from_ui(admin_page, ui_config, question)
    cid, tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    events = live_api.collect_sse(f"/chat/turns/{tid}/stream?cursor=0")
    answer_event(events)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
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
    sources_verified = (assistant.get("answer") or {}).get("sources_verified")
    assert isinstance(sources_verified, list) and sources_verified, "ext23 produced no verified-source evidence"
    sources = (assistant.get("answer") or {}).get("sources") or []
    verified_ids = {str(item) for item in sources_verified}
    source_ids = {str(item.get("doc_id")) for item in sources}
    assert any("tax-policy.md" in item for item in verified_ids)
    assert any("nightly-runbook.md" in item for item in source_ids - verified_ids)
    expect(admin_page.locator("#messages")).to_contain_text("根拠（精読済み）")
    expect(admin_page.locator("#messages")).to_contain_text("参考（ヒットのみ）")
    verified_link = admin_page.locator(
        "#messages .sources-verified a[data-dl], "
        "#messages [data-source-kind='verified'] a[data-dl], "
        "#messages .verified-sources a[data-dl]",
        has_text="tax-policy.md",
    ).last
    expect(verified_link).to_be_visible()
    href = verified_link.get_attribute("href")
    assert href and "/documents/download" in href, "ext23 verified source has no original-document download route"
    artifact_case.screenshot(admin_page, 10, "chat-ext23-evidence-and-reference-separated")
    with admin_page.expect_download(timeout=ui_config.timeout_ms) as download_info:
        with admin_page.expect_response(
            lambda response: response.request.method == "GET" and response.url.endswith(href),
            timeout=ui_config.timeout_ms,
        ) as response_info:
            verified_link.click()
    response = response_info.value
    assert response.status == 200, response.text()
    response_bytes = response.body()
    expected_bytes = (ui_config.world_path / "specs/tax-policy.md").read_bytes()
    assert response_bytes == expected_bytes, "verified-source original link returned a different World document"
    downloaded = artifact_case.case_dir / "state" / "ext23-verified-original-download.md"
    download_info.value.save_as(str(downloaded))
    assert downloaded.read_bytes() == expected_bytes
    artifact_case.attest_control_state(
        control_key="@selector:[data-dl]",
        state="normal",
        assertion="根拠原本のdownload内容がfixture tax-policy原本byte列と完全一致した",
    )
    artifact_case.write_json(
        "state/ext23-verified-original-link.json",
        {
            "doc_id": "specs/tax-policy.md",
            "http_status": response.status,
            "download_suggested_filename": download_info.value.suggested_filename,
            "response_sha256": hashlib.sha256(response_bytes).hexdigest(),
            "source_sha256": hashlib.sha256(expected_bytes).hexdigest(),
            "download_sha256": hashlib.sha256(downloaded.read_bytes()).hexdigest(),
        },
    )
    artifact_case.screenshot(admin_page, 20, "chat-ext23-verified-original-opened-and-matched")


@pytest.mark.provider_profile_real
def test_real_provider_profile_uses_selected_service(admin_page, live_api, ui_config, artifact_case, real_world):
    contract = ui_config.expected_environment()
    expected_agent = str((contract.get("expected") or {}).get("agent") or "").strip().lower()
    assert expected_agent, "provider profile did not declare its expected real agent"
    settings = live_api.get_json("/settings", save_as="state/provider-profile-settings.json")
    actual_agent = str(settings.get("agent") or "").strip().lower()
    assert actual_agent == expected_agent, {
        "expected_agent": expected_agent,
        "actual_agent": actual_agent,
        "profile": ui_config.profile,
    }
    assert actual_agent != "heuristic", "provider profile selected the non-AI implementation"

    prepare_chat(admin_page, ui_config, real_world)
    started = start_turn_from_ui(
        admin_page,
        ui_config,
        "SHERPA-LIVE-REFERENCE-314 を実資料から調べ、根拠ファイルと運用時刻を答えてください。",
    )
    cid, tid = int(started["conversation_id"]), str(started["turn_id"])
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    artifact_case.screenshot(admin_page, 10, f"chat-{expected_agent}-question-sent")
    events = live_api.collect_sse(f"/chat/turns/{tid}/stream?cursor=0")
    answer_event(events)
    wait_for_completed_ui(admin_page, ui_config.timeout_ms)
    conversation = live_api.get_json(f"/conversations/{cid}", save_as="state/provider-conversation.json")
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
    usage = (assistant.get("answer") or {}).get("usage") or {}
    used_provider = str(usage.get("provider") or "").strip().lower()
    assert used_provider == expected_agent, {
        "expected_agent": expected_agent,
        "usage_provider": used_provider,
        "usage_model": usage.get("model"),
    }
    answer = assistant.get("answer") or {}
    answer_text = str(answer.get("headline") or assistant.get("content") or "")
    citations = (answer.get("data") or {}).get("citations") or []
    citation_docs = {str(item.get("doc_id") or "") for item in citations if isinstance(item, dict)}
    scope = answer.get("scope") or {}
    assert "02:15" in answer_text, "selected real provider omitted the deterministic operations time"
    assert citations and any(doc in citation_docs for doc in {"office/nightly-operations.pptx", "media/text-evidence.pdf"}), {
        "reason": "selected real provider did not cite an ingested source containing 02:15",
        "citation_docs": sorted(citation_docs),
    }
    assert scope.get("world") == real_world
    assert scope.get("source") == "all" and not (scope.get("scope_paths") or []), (
        "unscoped provider profile did not persist the full-World scope contract"
    )
    artifact_case.write_json(
        "state/provider-profile-summary.json",
        {
            "profile": ui_config.profile,
            "configured_agent": actual_agent,
            "usage_provider": used_provider,
            "usage_model": usage.get("model"),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "grounded_time_seen": True,
            "citation_docs": sorted(citation_docs),
            "scope": scope,
        },
    )
    artifact_case.screenshot(admin_page, 20, f"chat-{expected_agent}-real-usage-correlated")
