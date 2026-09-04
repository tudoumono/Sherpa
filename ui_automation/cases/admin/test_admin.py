from __future__ import annotations

import csv
import io
import json
import re
from datetime import date, timedelta

import pytest
from playwright.sync_api import expect

from ui_automation.runner.artifacts import write_private_text_atomic
from ui_automation.support.chat import assert_real_ai_result
from ui_automation.support.database import real_ai_turn_checkpoint
from ui_automation.support.ui import runtime_password, unique_id


pytestmark = [pytest.mark.ui_automation, pytest.mark.admin, pytest.mark.destructive]


def _delete_announcement_if_present(api, announcement_id: int) -> None:
    api.request(
        "DELETE",
        f"/admin/announcements/{announcement_id}",
        expected={200, 404},
    )


def _revoke_external_key_if_present(api, key_id: int) -> None:
    api.request("DELETE", f"/ext/v1/admin/keys/{key_id}", expected={200, 404})


def test_admin_status_audit_usage_and_user_management(
    admin_page,
    live_api,
    ui_config,
    artifact_case,
    isolated_stack,
    real_world,
):
    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#main-content")).to_be_visible()
    # UI-TABS2（2026-09-04）: 旧・リンクタブ（<a class="tab-link">）は本物のタブ（.tab-btn/role="tab"・
    # iframe 埋め込み）へ置き換わった。
    for label in ("ユーザー管理", "利用統計", "監査ログ", "システム状態"):
        expect(admin_page.locator("#admin-tabs .tab-btn", has_text=label)).to_be_visible()
    artifact_case.screenshot(admin_page, 10, "admin-system-management-menu")

    admin_page.goto(ui_config.base_url + "/ui/status.html")
    expect(admin_page.locator("#status-pill")).to_have_text("正常")
    expect(admin_page.locator("#health-tbody tr")).not_to_have_count(0)
    health = live_api.get_json("/admin/health", save_as="state/admin-health.json")
    assert health.get("status") == "ok"
    required = {"postgres", "neo4j", "elasticsearch"}
    observed = {str(component.get("id")) for component in health.get("components", []) if component.get("ok")}
    assert required <= observed, {"required": sorted(required), "observed": sorted(observed)}
    artifact_case.screenshot(admin_page, 20, "admin-real-services-health")

    uid = unique_id("ui-admin-case")
    initial = runtime_password()
    artifact_case.register_secret(initial)
    admin_page.goto(ui_config.base_url + "/ui/admin-users.html")
    artifact_case.stop_trace(save=False)
    admin_page.locator("#nu-uid").fill(uid)
    admin_page.locator("#nu-name").fill("UI Admin Case Member")
    admin_page.locator("#nu-role").select_option("user")
    admin_page.locator("#nu-pw").fill(initial)
    admin_page.locator("#nu-submit").click()
    row = admin_page.locator("#user-tbody tr", has_text=uid)
    expect(row).to_be_visible()
    created_users = live_api.get_json("/admin/users")
    created_user = next(item for item in created_users.get("users") or [] if item.get("uid") == uid)
    assert created_user.get("display_name") == "UI Admin Case Member"
    assert created_user.get("role") == "user"
    for control_key, assertion in (
        ("nu-uid", "入力した一意なuser IDの行が実user一覧へ表示された"),
        ("nu-name", "入力した表示名が作成済み実user APIの対象recordと一致した"),
        ("nu-role", "選択したuser roleが作成済み実user APIの対象recordと一致した"),
        ("nu-submit", "新規user作成操作後に対象userの実一覧行を確認した"),
    ):
        artifact_case.attest_control_state(control_key=control_key, state="normal", assertion=assertion)
    artifact_case.start_trace(admin_page.context)
    artifact_case.add_cleanup(
        f"disable user {uid}",
        lambda: live_api.patch_json(f"/admin/users/{uid}", {"status": "disabled"}),
    )
    artifact_case.screenshot(admin_page, 30, "admin-user-created-through-real-ui")

    admin_page.goto(ui_config.base_url + "/ui/audit.html")
    admin_page.locator("#f-action").fill("user.created")
    admin_page.locator("#search-btn").click()
    expect(admin_page.locator("#audit-tbody")).to_contain_text(uid)
    audit_row = admin_page.locator("#audit-tbody tr.audit-row").first
    expect(audit_row.locator("[data-detail-toggle]")).to_be_visible()
    artifact_case.arm_control(audit_row, control_key="@selector:.audit-row")
    audit_row.click(position={"x": 4, "y": 4})
    expect(admin_page.locator("#audit-tbody tr.detail-row")).to_have_count(1)
    artifact_case.attest_control_state(
        control_key="@selector:.audit-row",
        state="normal",
        assertion="選択した実audit行を直接操作し同じrecordの詳細行だけを展開した",
    )
    audit_row.locator("[data-detail-toggle]").click()
    expect(admin_page.locator("#audit-tbody tr.detail-row")).to_have_count(0)
    artifact_case.attest_control_state(
        control_key="@selector:[data-detail-toggle]",
        state="normal",
        assertion="選択した実audit行の詳細だけを折り畳み別recordの表示を変更しなかった",
    )
    audit = live_api.get_json("/admin/audit?action=user.created&limit=20", save_as="state/admin-audit.json")
    assert any(row.get("resource_id") == f"user:{uid}" for row in audit.get("rows", []))
    artifact_case.attest_control_state(
        control_key="f-action",
        state="normal",
        assertion="user.created条件で対象userの実audit recordだけを取得した",
    )
    artifact_case.attest_control_state(
        control_key="search-btn",
        state="normal",
        assertion="audit検索操作の画面結果と実audit API結果が対象userで一致した",
    )
    artifact_case.screenshot(admin_page, 40, "admin-audit-user-created-recorded")

    settings = live_api.get_json("/settings", save_as="state/admin-usage-ai-settings.json")
    assert settings.get("agent") != "heuristic", "usage chart evidence requires a configured real AI"
    usage_ai_checkpoint = real_ai_turn_checkpoint(ui_config.database_url, ui_config.expected_env_path)
    generated_turn = live_api.post_json(
        "/chat/turns",
        {
            "message": "実AI利用量グラフの検証用に、SHERPA-LIVE-ALPHA-927 の税率を一文で答えてください。",
            "world": real_world,
            "knowledge": True,
            "personal": False,
        },
        save_as="state/admin-usage-real-chat-turn-start.json",
    )
    usage_conversation_id = int(generated_turn.get("conversation_id") or 0)
    usage_turn_id = str(generated_turn.get("turn_id") or "")
    assert usage_conversation_id > 0
    assert usage_turn_id
    artifact_case.add_cleanup(
        f"delete usage-chart conversation {usage_conversation_id}",
        lambda: live_api.delete_json(f"/conversations/{usage_conversation_id}"),
    )
    usage_sse = live_api.collect_sse(f"/chat/turns/{usage_turn_id}/stream?cursor=0")
    assert any(event.get("type") == "answer" for event in usage_sse), "real usage turn emitted no final answer event"
    generated_conversation = live_api.get_json(
        f"/conversations/{usage_conversation_id}",
        save_as="state/admin-usage-real-chat-conversation.json",
    )
    generated_assistants = [message for message in generated_conversation.get("messages") or [] if message.get("role") == "assistant"]
    assert generated_assistants
    generated_assistant = generated_assistants[-1]
    generated_answer = generated_assistant.get("answer") or {}
    assert_real_ai_result(
        settings,
        usage_sse,
        generated_assistant,
        require_tool=True,
        evidence=artifact_case,
        turn_id=usage_turn_id,
        conversation_id=usage_conversation_id,
        database_url=ui_config.database_url,
        checkpoint=usage_ai_checkpoint,
        operation="admin-usage-chart-real-ai",
    )
    generated_usage = generated_answer.get("usage") or {}
    generated_provider = str(generated_usage.get("provider") or "")
    generated_answer_text = json.dumps(generated_answer, ensure_ascii=False)
    assert "12.5" in generated_answer_text and "tax-policy" in generated_answer_text
    assert generated_provider and generated_provider != "heuristic"
    assert int(generated_usage.get("input_tokens") or 0) + int(generated_usage.get("output_tokens") or 0) > 0

    admin_page.goto(ui_config.base_url + "/ui/usage.html")
    expect(admin_page.locator("#t-active")).not_to_have_text("—")
    for days in (7, 90, 30):
        with admin_page.expect_response(
            lambda response, selected=days: response.request.method == "GET" and f"/admin/usage/stats?days={selected}" in response.url,
            timeout=ui_config.timeout_ms,
        ) as period_info:
            admin_page.locator(f'[data-days="{days}"]').click()
        assert period_info.value.status == 200
        expect(admin_page.locator(f'[data-days="{days}"]')).to_have_class(re.compile(r"(?:^|\s)on(?:\s|$)"))
        artifact_case.attest_control_state(
            control_key="@selector:[data-days]",
            state="normal",
            assertion=f"{days}日集計の実API応答が200となり選択periodだけがactive表示になった",
        )
    usage = live_api.get_json("/admin/usage/stats?days=30", save_as="state/admin-usage.json")
    assert isinstance(usage.get("totals"), dict)
    artifact_case.screenshot(admin_page, 50, "admin-real-usage-dashboard")

    provider_rows = list(usage.get("providers") or [])
    provider_order = ["heuristic", "codex", "openai", "gemini", "bedrock", "ollama", "unknown"]
    sorted_providers = sorted(
        provider_rows,
        key=lambda row: provider_order.index(str(row.get("provider"))) if str(row.get("provider")) in provider_order else 999,
    )
    provider_index = next(index for index, row in enumerate(sorted_providers) if str(row.get("provider")) == generated_provider)
    provider_row = sorted_providers[provider_index]
    provider_labels = {
        "heuristic": "簡易（AIなし）",
        "codex": "Codex",
        "openai": "OpenAI API",
        "gemini": "Gemini",
        "bedrock": "AWS Bedrock (Claude)",
        "ollama": "ローカルLLM (Ollama)",
        "unknown": "不明",
    }
    provider_hit = admin_page.locator('#chart-provider-svg rect[tabindex="0"]').nth(provider_index)
    provider_tip = admin_page.locator("#chart-provider-tip")
    expect(provider_hit).to_be_visible()
    artifact_case.arm_unkeyed_control(
        provider_hit,
        control_key="@unkeyed:web/usage.js:396:rect",
    )
    provider_hit.click()
    expect(provider_tip).to_be_visible()
    expect(provider_tip).to_contain_text(f"{int(provider_row.get('turns') or 0)}件")
    expect(provider_tip).to_contain_text(provider_labels[generated_provider])
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/usage.js:396:rect",
        state="normal",
        assertion="実AI turnから集計したprovider barへfocusしAPIと一致するprovider名と件数をtooltip表示した",
    )
    artifact_case.screenshot(admin_page, 52, "admin-usage-provider-bar-tooltip-matches-real-turn")

    artifact_case.arm_unkeyed_control(
        provider_hit,
        control_key="@unkeyed:web/usage.js:396:rect",
    )
    provider_hit.press("Tab")
    expect(provider_tip).to_be_hidden()
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/usage.js:396:rect",
        state="abnormal",
        assertion="provider barからfocusを外すと直前の実集計tooltipを隠し古い値を表示中として残さなかった",
    )
    artifact_case.screenshot(admin_page, 54, "admin-usage-provider-tooltip-hidden-after-focus-leaves")

    daily_rows = {str(row.get("date")): int(row.get("turns") or 0) for row in usage.get("daily") or []}
    period_start = date.fromisoformat(str((usage.get("period") or {}).get("start")))
    period_end = date.fromisoformat(str((usage.get("period") or {}).get("end")))
    period_dates = []
    current_date = period_start
    while current_date <= period_end:
        period_dates.append(current_date.isoformat())
        current_date += timedelta(days=1)
    active_date = next(day for day in reversed(period_dates) if daily_rows.get(day, 0) > 0)
    active_index = period_dates.index(active_date)
    active_hit = admin_page.locator("#chart-tn-svg .hit").nth(active_index)
    turn_tip = admin_page.locator("#chart-tn-tip")
    artifact_case.arm_control(active_hit, control_key="@selector:.hit")
    active_hit.click()
    expect(turn_tip).to_be_visible()
    expect(turn_tip).to_contain_text(active_date)
    expect(turn_tip).to_contain_text(f"{daily_rows[active_date]}件")
    artifact_case.attest_control_state(
        control_key="@selector:.hit",
        state="normal",
        assertion="日別turn chartの実活動日へfocusしusage APIと一致する日付とturn件数をtooltip表示した",
    )
    artifact_case.screenshot(admin_page, 56, "admin-usage-daily-turn-hit-tooltip-matches-api")

    zero_date = next((day for day in period_dates if daily_rows.get(day, 0) == 0), None)
    assert zero_date is not None, "isolated 30-day usage series unexpectedly has no zero day"
    zero_hit = admin_page.locator("#chart-tn-svg .hit").nth(period_dates.index(zero_date))
    artifact_case.arm_control(zero_hit, control_key="@selector:.hit")
    zero_hit.click()
    expect(turn_tip).to_be_visible()
    expect(turn_tip).to_contain_text(zero_date)
    expect(turn_tip).to_contain_text("0件")
    assert f"{daily_rows[active_date]}件" not in turn_tip.inner_text() or daily_rows[active_date] == 0
    artifact_case.attest_control_state(
        control_key="@selector:.hit",
        state="abnormal",
        assertion="活動のない別日へfocusしたtooltipを0件へ更新し直前の実活動日件数を誤表示しなかった",
    )
    artifact_case.screenshot(admin_page, 58, "admin-usage-zero-day-hit-does-not-show-stale-count")

    heatmap_rows = {
        (int(row.get("weekday") or 0), int(row.get("hour") or 0)): int(row.get("count") or 0) for row in usage.get("heatmap") or []
    }
    assert heatmap_rows, "real chat turn did not produce a heatmap cell"
    active_cell_key = max(heatmap_rows, key=heatmap_rows.get)
    active_cell = admin_page.locator("#heatmap-svg .cell").nth(active_cell_key[0] * 24 + active_cell_key[1])
    heatmap_tip = admin_page.locator("#heatmap-tip")
    day_labels = ["日", "月", "火", "水", "木", "金", "土"]
    artifact_case.arm_control(active_cell, control_key="@selector:.cell")
    active_cell.click()
    expect(heatmap_tip).to_be_visible()
    expect(heatmap_tip).to_contain_text(f"{heatmap_rows[active_cell_key]}件")
    expect(heatmap_tip).to_contain_text(f"{day_labels[active_cell_key[0]]}曜 {active_cell_key[1]}時台")
    artifact_case.attest_control_state(
        control_key="@selector:.cell",
        state="normal",
        assertion="実message時刻のheatmap cellへfocusしusage APIと一致する曜日時台と件数をtooltip表示した",
    )
    artifact_case.screenshot(admin_page, 60, "admin-usage-active-heatmap-cell-tooltip-matches-api")

    zero_cell_key = next((divmod(index, 24) for index in range(7 * 24) if divmod(index, 24) not in heatmap_rows), None)
    assert zero_cell_key is not None, "isolated heatmap unexpectedly has no zero cell"
    zero_cell = admin_page.locator("#heatmap-svg .cell").nth(zero_cell_key[0] * 24 + zero_cell_key[1])
    artifact_case.arm_control(zero_cell, control_key="@selector:.cell")
    zero_cell.click()
    expect(heatmap_tip).to_be_visible()
    expect(heatmap_tip).to_contain_text("0件")
    expect(heatmap_tip).to_contain_text(f"{day_labels[zero_cell_key[0]]}曜 {zero_cell_key[1]}時台")
    artifact_case.attest_control_state(
        control_key="@selector:.cell",
        state="abnormal",
        assertion="利用のないheatmap cellへfocusしたtooltipを0件へ更新し別時台の実件数を残さなかった",
    )
    artifact_case.screenshot(admin_page, 62, "admin-usage-empty-heatmap-cell-does-not-show-stale-count")

    chart_info = admin_page.locator(".info-dot").first
    expected_info = str(chart_info.get_attribute("title") or "")
    assert "JST" in expected_info and "曜日" in expected_info
    artifact_case.arm_control(chart_info, control_key="@selector:.info-dot")
    chart_info.click()
    expect(chart_info).to_be_focused()
    assert chart_info.get_attribute("title") == expected_info
    assert admin_page.locator(".chart-tooltip:visible").count() == 0
    artifact_case.attest_control_state(
        control_key="@selector:.info-dot",
        state="normal",
        assertion="usage説明iconへkeyboard focusでき、対象heatmapのJST曜日集計説明だけをtitleとして保持した",
    )
    artifact_case.screenshot(admin_page, 64, "admin-usage-info-focus-has-correct-chart-description")

    artifact_case.arm_control(chart_info, control_key="@selector:.info-dot")
    chart_info.press("Tab")
    expect(chart_info).not_to_be_focused()
    assert admin_page.locator(".chart-tooltip:visible").count() == 0
    artifact_case.attest_control_state(
        control_key="@selector:.info-dot",
        state="abnormal",
        assertion="説明iconからfocusを外しても別chartの件数tooltipを誤って表示せず説明対象を混同しなかった",
    )
    artifact_case.screenshot(admin_page, 66, "admin-usage-info-blur-does-not-show-unrelated-tooltip")

    usage_users = list(usage.get("users") or [])
    assert usage_users, "real usage API returned no sortable user row"
    sort_header = admin_page.locator('th.sortable[data-sort="turns"]')
    artifact_case.arm_control(sort_header, control_key="@selector:.sortable")
    sort_header.click()
    expect(sort_header.locator(".arrow")).to_have_text("▲")
    actual_ascending = admin_page.locator("#usage-tbody tr.u-row").evaluate_all(
        "rows => rows.map(row => row.dataset.uid)",
    )
    expected_ascending = [str(row.get("uid")) for row in sorted(usage_users, key=lambda row: int(row.get("turns") or 0))]
    assert actual_ascending == expected_ascending
    artifact_case.attest_control_state(
        control_key="@selector:.sortable",
        state="normal",
        assertion="turn数header操作で実usage APIのuser行を件数昇順へ並べ替えarrow表示とも一致した",
    )
    artifact_case.screenshot(admin_page, 68, "admin-usage-user-table-sorted-turns-ascending")

    artifact_case.arm_control(sort_header, control_key="@selector:.sortable")
    sort_header.click()
    expect(sort_header.locator(".arrow")).to_have_text("▼")
    actual_descending = admin_page.locator("#usage-tbody tr.u-row").evaluate_all(
        "rows => rows.map(row => row.dataset.uid)",
    )
    expected_descending = [str(row.get("uid")) for row in sorted(usage_users, key=lambda row: int(row.get("turns") or 0), reverse=True)]
    assert actual_descending == expected_descending
    assert set(actual_descending) == {str(row.get("uid")) for row in usage_users}
    artifact_case.attest_control_state(
        control_key="@selector:.sortable",
        state="abnormal",
        assertion="再sortで実user集合を欠落や空行に置換せず件数降順へ戻して別userを混入しなかった",
    )
    artifact_case.screenshot(admin_page, 70, "admin-usage-user-table-sort-preserves-real-user-set")

    current_user = live_api.get_json("/auth/me", save_as="state/admin-usage-current-user.json")
    current_uid = str(current_user.get("uid") or "")
    expected_user_usage = next(row for row in usage_users if str(row.get("uid")) == current_uid)
    usage_row = admin_page.locator(f'#usage-tbody tr.u-row[data-uid="{current_uid}"]')
    usage_detail = usage_row.locator("xpath=following-sibling::tr[1]")
    artifact_case.arm_control(usage_row, control_key="@selector:.u-row")
    usage_row.click()
    expect(usage_row).to_have_class(re.compile(r"(?:^|\s)u-open(?:\s|$)"))
    expect(usage_detail).to_be_visible()
    expect(usage_detail.locator(".worldtag")).to_have_text(real_world)
    expect(usage_detail.locator(".g", has_text="ログイン回数").locator("b")).to_have_text(f"{int(expected_user_usage.get('logins') or 0)}")
    artifact_case.attest_control_state(
        control_key="@selector:.u-row",
        state="normal",
        assertion="選択した実admin user行だけを展開しAPI由来の利用worldとlogin内訳を表示した",
    )
    artifact_case.screenshot(admin_page, 72, "admin-usage-selected-real-user-detail-expanded")

    artifact_case.arm_control(usage_row, control_key="@selector:.u-row")
    usage_row.click()
    expect(usage_row).not_to_have_class(re.compile(r"(?:^|\s)u-open(?:\s|$)"))
    expect(usage_detail).to_be_hidden()
    assert admin_page.locator("#usage-tbody tr.u-row.u-open").count() == 0
    assert {str(row.get("uid")) for row in usage_users} == set(actual_descending)
    artifact_case.attest_control_state(
        control_key="@selector:.u-row",
        state="abnormal",
        assertion="再操作で選択user内訳だけを閉じ他user行を展開せず実APIのuser集合も変えなかった",
    )
    artifact_case.screenshot(admin_page, 74, "admin-usage-user-detail-closed-without-cross-user-leak")


def test_announcement_create_edit_publish_and_delete_lifecycle(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    title = unique_id("ui-announcement")
    body = "UI実サービス試験のお知らせ本文です。"
    admin_page.goto(ui_config.base_url + "/ui/home.html")
    expect(admin_page.locator("#pf-open")).to_be_visible()
    admin_page.locator("#pf-open").click()
    admin_page.locator("#pf-title").fill("この入力は投稿せず取り消します")
    admin_page.locator("#pf-cancel").click()
    expect(admin_page.locator("#pf-title")).to_have_count(0)
    expect(admin_page.locator("#pf-open")).to_be_focused()
    artifact_case.attest_control_state(
        control_key="pf-cancel",
        state="normal",
        assertion="取消操作後に投稿formが閉じて投稿開始buttonへfocusが戻った",
    )
    admin_page.locator("#pf-open").click()
    expect(admin_page.locator("#pf-title")).to_have_value("")
    artifact_case.attest_control_state(
        control_key="pf-open",
        state="normal",
        assertion="投稿formを再度開いた時に取消済みtitleが残らず空欄だった",
    )
    admin_page.locator("#pf-title").fill(title)
    admin_page.locator("#pf-body").fill(body)
    admin_page.locator("#pf-cat").select_option("maintenance")
    admin_page.locator("#pf-pinned").check()
    publish_value = "2020-01-02T03:04"
    expire_value = "2099-12-30T23:59"
    admin_page.locator("#pf-publish").fill(publish_value)
    admin_page.locator("#pf-expire").fill(expire_value)
    expected_publish = admin_page.locator("#pf-publish").evaluate("element => new Date(element.value).toISOString()")
    expected_expire = admin_page.locator("#pf-expire").evaluate("element => new Date(element.value).toISOString()")
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/admin/announcements"),
        timeout=ui_config.timeout_ms,
    ) as create_info:
        admin_page.locator("#pf-submit").click()
    created = create_info.value.json()
    assert create_info.value.status == 200 and created.get("ok") is True, created
    assert created["announcement"].get("body") == body
    assert created["announcement"].get("category") == "maintenance"
    assert admin_page.evaluate(
        "([actual, expected]) => new Date(actual).toISOString() === expected",
        [created["announcement"].get("publish_at"), expected_publish],
    )
    assert admin_page.evaluate(
        "([actual, expected]) => new Date(actual).toISOString() === expected",
        [created["announcement"].get("expire_at"), expected_expire],
    )
    announcement_id = int(created["announcement"]["id"])
    artifact_case.add_cleanup(
        f"delete announcement {announcement_id}",
        lambda: _delete_announcement_if_present(live_api, announcement_id),
    )
    row = admin_page.locator(f'[data-item="{announcement_id}"]')
    expect(row).to_contain_text(title)
    expect(row.locator(".pin")).to_be_visible()
    for control_key, assertion in (
        ("pf-title", "入力したtitleが作成済み実お知らせ行へ表示された"),
        ("pf-body", "入力した本文が作成済み実お知らせAPIの本文と一致した"),
        ("pf-cat", "選択したmaintenance分類が作成済み実お知らせAPIと一致した"),
        ("pf-publish", "入力した公開開始日時がAPI応答のISO時刻と一致した"),
        ("pf-expire", "入力した公開終了日時がAPI応答のISO時刻と一致した"),
        ("pf-pinned", "pin指定した実お知らせ行へpin表示が描画された"),
        ("pf-submit", "投稿操作が成功し対象IDの実お知らせ行を確認した"),
    ):
        artifact_case.attest_control_state(control_key=control_key, state="normal", assertion=assertion)
    artifact_case.screenshot(admin_page, 10, "admin-announcement-created-and-pinned")

    row.locator("[data-edit]").click()
    admin_page.locator(f"#ef-body-{announcement_id}").fill("この変更はキャンセルされる必要があります。")
    row.locator("[data-cancel]").click()
    expect(row).to_contain_text(body)
    expect(row).not_to_contain_text("この変更はキャンセルされる必要があります。")
    artifact_case.attest_control_state(
        control_key="@selector:[data-cancel]",
        state="abnormal",
        assertion="取消した編集本文が実お知らせ行へ保存も表示もされなかった",
    )

    row.locator("[data-edit]").click()
    expect(row.locator("[data-cancel]")).to_be_visible()
    row.locator("[data-cancel]").click()
    expect(row.locator("[data-cancel]")).to_have_count(0)
    expect(row).to_contain_text(body)
    artifact_case.attest_control_state(
        control_key="@selector:[data-cancel]",
        state="normal",
        assertion="未変更の実お知らせ編集を取消して編集formだけを閉じ元本文を維持した",
    )

    row.locator("[data-edit]").click()
    edited_title = title + " 編集済み"
    edited_body = body + " 変更内容を実画面で保存しました。"
    edited_publish = "2021-02-03T04:05"
    edited_expire = "2098-11-29T22:58"
    admin_page.locator(f"#ef-title-{announcement_id}").fill(edited_title)
    admin_page.locator(f"#ef-body-{announcement_id}").fill(edited_body)
    admin_page.locator(f"#ef-cat-{announcement_id}").select_option("release")
    admin_page.locator(f"#ef-publish-{announcement_id}").fill(edited_publish)
    admin_page.locator(f"#ef-expire-{announcement_id}").fill(edited_expire)
    admin_page.locator(f"#ef-pinned-{announcement_id}").uncheck()
    with admin_page.expect_response(
        lambda response: response.request.method == "PATCH" and response.url.endswith(f"/admin/announcements/{announcement_id}"),
        timeout=ui_config.timeout_ms,
    ) as edit_info:
        admin_page.locator(f'[data-save="{announcement_id}"]').click()
    assert edit_info.value.status == 200
    edited_announcement = edit_info.value.json()["announcement"]
    assert edited_announcement["title"] == edited_title
    assert edited_announcement["category"] == "release"
    assert edited_announcement["pinned"] is False
    assert admin_page.evaluate(
        "([actual, expected]) => new Date(actual).toISOString() === new Date(expected).toISOString()",
        [edited_announcement["publish_at"], edited_publish],
    )
    assert admin_page.evaluate(
        "([actual, expected]) => new Date(actual).toISOString() === new Date(expected).toISOString()",
        [edited_announcement["expire_at"], edited_expire],
    )
    expect(row).to_contain_text("変更内容を実画面で保存しました")
    expect(row).to_contain_text(edited_title)
    for control_key, assertion in (
        ("@id-prefix:ef-title-", "動的title編集値が同じ実お知らせIDのAPI保存値へ反映された"),
        ("@id-prefix:ef-cat-", "動的category選択値releaseが同じ実お知らせIDへ保存された"),
        ("@id-prefix:ef-publish-", "動的公開開始時刻が同じ実お知らせのISO時刻へ保存された"),
        ("@id-prefix:ef-expire-", "動的公開終了時刻が同じ実お知らせのISO時刻へ保存された"),
        ("@id-prefix:ef-pinned-", "動的pin解除が同じ実お知らせのfalse保存値へ反映された"),
    ):
        artifact_case.attest_control_state(control_key=control_key, state="normal", assertion=assertion)
    artifact_case.attest_control_state(
        control_key="@selector:[data-edit]",
        state="normal",
        assertion="選択した実お知らせの編集内容だけが保存後の行へ表示された",
    )
    artifact_case.attest_control_state(
        control_key="@selector:[data-save]",
        state="normal",
        assertion="編集保存APIが200となり変更本文が対象行へ反映された",
    )
    artifact_case.screenshot(admin_page, 20, "admin-announcement-edited")

    with admin_page.expect_response(
        lambda response: response.request.method == "PATCH" and response.url.endswith(f"/admin/announcements/{announcement_id}"),
        timeout=ui_config.timeout_ms,
    ) as unpublish_info:
        row.locator("[data-toggle-pub]").click()
    assert unpublish_info.value.json()["announcement"]["published"] is False
    expect(row).to_contain_text("非公開")
    artifact_case.attest_control_state(
        control_key="@selector:[data-toggle-pub]",
        state="normal",
        assertion="公開切替APIと対象行の表示がともに非公開へ変わった",
    )
    artifact_case.screenshot(admin_page, 30, "admin-announcement-unpublished")

    admin_page.once("dialog", lambda dialog: dialog.accept())
    with admin_page.expect_response(
        lambda response: response.request.method == "DELETE" and response.url.endswith(f"/admin/announcements/{announcement_id}"),
        timeout=ui_config.timeout_ms,
    ) as delete_info:
        row.locator("[data-delete]").click()
    assert delete_info.value.status == 200
    expect(admin_page.locator(f'[data-item="{announcement_id}"]')).to_have_count(0)
    listed = live_api.get_json("/announcements?include_unpublished=true&limit=100")
    assert all(int(item["id"]) != announcement_id for item in listed.get("announcements", []))
    artifact_case.attest_control_state(
        control_key="@selector:[data-delete]",
        state="normal",
        assertion="削除API成功後に対象お知らせが画面一覧と実API一覧の両方から消えた",
    )
    artifact_case.screenshot(admin_page, 40, "admin-announcement-deleted")


def test_audit_chain_verify_and_ui_export(admin_page, live_api, ui_config, artifact_case):
    verified = live_api.get_json("/admin/audit/verify", save_as="state/audit-chain-verify.json")
    assert verified.get("ok") is True, verified
    admin_page.goto(ui_config.base_url + "/ui/audit.html")
    expect(admin_page.locator("#audit-tbody tr")).not_to_have_count(0)
    for index, (button_id, suffix) in enumerate((("#export-csv", "csv"), ("#export-jsonl", "jsonl")), 1):
        with admin_page.expect_download(timeout=ui_config.timeout_ms) as download_info:
            admin_page.locator(button_id).click()
        download = download_info.value
        target = artifact_case.case_dir / "state" / f"audit-export.{suffix}"
        raw_path = download.path()
        assert raw_path is not None and raw_path.stat().st_size > 0, f"audit {suffix} export is empty"
        if suffix == "csv":
            rows = list(csv.DictReader(io.StringIO(raw_path.read_text(encoding="utf-8"))))
            assert rows and "action" in rows[0]
            output = io.StringIO()
            writer = csv.DictWriter(output, fieldnames=list(rows[0]))
            writer.writeheader()
            writer.writerows(artifact_case.redact(rows))
            write_private_text_atomic(target, output.getvalue())
        else:
            rows = [json.loads(line) for line in raw_path.read_text(encoding="utf-8").splitlines() if line]
            assert rows
            artifact_case.write_jsonl("state/audit-export.jsonl", rows)
        expect(admin_page.locator("#export-status")).to_contain_text("エクスポートしました")
        artifact_case.attest_control_state(
            control_key=button_id.removeprefix("#"),
            state="normal",
            assertion=f"{suffix}形式の実audit exportが非空で解析可能となり完了表示を確認した",
        )
        artifact_case.screenshot(admin_page, index * 10, f"admin-audit-{suffix}-export-complete")
    audit = live_api.get_json(
        "/admin/audit?action=admin.audit_exported&limit=10",
        save_as="state/audit-export-events.json",
    )
    assert len(audit.get("rows", [])) >= 2, audit


def test_external_api_key_create_use_list_and_revoke(admin_page, live_api, artifact_case, isolated_stack):
    label = unique_id("ui-ext-key")
    created = live_api.post_json("/ext/v1/admin/keys", {"label": label})
    plain_key = str(created.get("key") or "")
    artifact_case.register_secret(plain_key)
    assert created.get("ok") is True, "external key creation did not report success"
    assert created.get("id"), "external key creation returned no identifier"
    assert plain_key, "external key creation returned no one-time credential"
    key_id = int(created["id"])
    artifact_case.add_cleanup(
        f"revoke external API key {key_id}",
        lambda: _revoke_external_key_if_present(live_api, key_id),
    )

    openapi = live_api.request(
        "GET",
        "/ext/v1/openapi.json",
        expected=200,
        headers={"X-API-Key": plain_key},
    ).json()
    assert openapi.get("info", {}).get("title") == "Sherpa External API"
    assert "/ext/v1/search" in openapi.get("paths", {})
    keys = live_api.get_json("/ext/v1/admin/keys", save_as="state/external-api-key-list.json")
    row = next((item for item in keys.get("keys", []) if int(item["id"]) == key_id), None)
    assert row and row.get("label") == label and row.get("revoked_at") is None
    assert "key" not in row, "external key listing exposed the plaintext key"
    artifact_case.screenshot(admin_page, 10, "admin-external-api-key-active-without-plaintext-listing")

    revoked = live_api.request("DELETE", f"/ext/v1/admin/keys/{key_id}", expected=200).json()
    assert revoked.get("ok") is True and revoked.get("revoked_at")
    denied = live_api.request(
        "GET",
        "/ext/v1/openapi.json",
        expected=401,
        headers={"X-API-Key": plain_key},
    )
    assert denied.status == 401
    after = live_api.get_json("/ext/v1/admin/keys")
    revoked_row = next(item for item in after.get("keys", []) if int(item["id"]) == key_id)
    assert revoked_row.get("revoked_at")
    artifact_case.write_json(
        "state/external-api-key-lifecycle.json",
        {
            "id": key_id,
            "label": label,
            "prefix": revoked_row.get("key_prefix"),
            "used_successfully": True,
            "rejected_after_revoke": True,
        },
    )
    artifact_case.screenshot(admin_page, 20, "admin-external-api-key-revoked-and-rejected")
