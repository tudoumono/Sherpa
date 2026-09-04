from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from ui_automation.support.ui import runtime_password, unique_id


pytestmark = [pytest.mark.ui_automation, pytest.mark.admin, pytest.mark.destructive]


def _configured(view: dict, key: str):
    block = view.get(key) or {}
    return block.get("configured") if isinstance(block, dict) else None


def test_admin_cloud_key_rejects_invalid_credential_without_persisting(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/admin/settings", save_as="state/admin-cloud-key-before.json")
    provider = str((before.get("cloud") or {}).get("provider") or "openai")
    invalid_key = f"ui-invalid-{unique_id(provider)}"
    artifact_case.register_secret(invalid_key)

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#cloud-key")).to_be_visible()
    artifact_case.stop_trace(save=False)
    try:
        admin_page.locator("#cloud-key").fill(invalid_key)
        with admin_page.expect_response(
            lambda response: response.request.method == "POST" and response.url.endswith("/settings/test"),
            timeout=ui_config.timeout_ms,
        ) as probe_info:
            admin_page.locator("#cloud-key-test").click()
        response = probe_info.value
        payload = response.json()
        assert response.status == 200, response.text()
        assert payload.get("provider") == provider
        assert payload.get("ok") is False, "an invalid real credential was reported as connected"
        artifact_case.attest_control_state(
            control_key="cloud-key",
            state="abnormal",
            assertion="故意に無効な中央cloud資格情報が実接続成功として扱われなかった",
        )
        artifact_case.attest_control_state(
            control_key="cloud-key-test",
            state="abnormal",
            assertion="無効資格情報の実接続probeがprovider一致の失敗結果を返した",
        )
    finally:
        if admin_page.locator("#cloud-key").count():
            admin_page.locator("#cloud-key").fill("")
        artifact_case.start_trace(admin_page.context)

    expect(admin_page.locator("#cloud-key-test-res")).to_contain_text("✗")
    after = live_api.get_json("/admin/settings", save_as="state/admin-cloud-key-after.json")
    assert (after.get("cloud") or {}).get(f"{provider}_key_set") == (before.get("cloud") or {}).get(f"{provider}_key_set")
    artifact_case.screenshot(admin_page, 10, "admin-invalid-cloud-key-rejected-and-cleared")


def test_admin_ingestion_default_reset_controls_restore_saved_values(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/admin/settings", save_as="state/admin-ingestion-reset-before.json")
    original_arms = _configured(before, "arms")
    original_legacy = _configured(before, "legacy_backend")
    artifact_case.add_cleanup(
        "restore ingestion settings after reset controls",
        lambda: live_api.put_json(
            "/admin/settings",
            {"arms_enabled": original_arms, "legacy_backend": original_legacy},
        ),
    )

    enabled = list((before.get("arms") or {}).get("enabled") or [])
    assert enabled, "real stack exposes no enabled ingestion arm"
    legacy = before.get("legacy_backend") or {}
    effective_legacy = str(legacy.get("effective") or "none")
    live_api.put_json(
        "/admin/settings",
        {"arms_enabled": enabled, "legacy_backend": effective_legacy},
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    arm_controls = admin_page.locator("#arms-list input[data-arm]")
    expect(arm_controls).not_to_have_count(0)
    enabled_arm_controls = admin_page.locator("#arms-list input[data-arm]:not(:disabled)")
    expect(enabled_arm_controls).not_to_have_count(0)
    first_arm = enabled_arm_controls.first
    arm_before = first_arm.is_checked()
    first_arm.set_checked(not arm_before)
    expect(first_arm).to_be_checked(checked=not arm_before)
    artifact_case.attest_control_state(
        control_key="@selector:[data-arm]",
        state="normal",
        assertion="利用可能な実取込armだけを反対の選択状態へ変更できた",
    )

    legacy_controls = admin_page.locator("#legacy-radios input[data-legacy]")
    expect(legacy_controls).not_to_have_count(0)
    enabled_legacy = admin_page.locator("#legacy-radios input[data-legacy]:not(:disabled)")
    expect(enabled_legacy).not_to_have_count(0)
    alternate_legacy = admin_page.locator("#legacy-radios input[data-legacy]:not(:disabled):not(:checked)")
    if alternate_legacy.count():
        target_legacy = alternate_legacy.first.get_attribute("data-legacy")
        assert target_legacy
        target_legacy_control = admin_page.locator(f'#legacy-radios input[data-legacy="{target_legacy}"]')
        target_legacy_control.check()
        expect(target_legacy_control).to_be_checked()
        artifact_case.attest_control_state(
            control_key="@selector:[data-legacy]",
            state="normal",
            assertion="利用可能な別legacy Office backendだけを選択状態へ変更できた",
        )
    else:
        expect(enabled_legacy.first).to_be_checked()

    for selector, configured_key in (
        ("#arms-reset", "arms"),
        ("#legacy-reset", "legacy_backend"),
    ):
        with admin_page.expect_response(
            lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
            timeout=ui_config.timeout_ms,
        ) as reset_info:
            admin_page.locator(selector).click()
        assert reset_info.value.status == 200, reset_info.value.text()
        reset_view = reset_info.value.json()
        assert _configured(reset_view, configured_key) is None
        artifact_case.attest_control_state(
            control_key=selector.removeprefix("#"),
            state="normal",
            assertion=f"{configured_key}のreset API成功後に保存上書き値が未設定へ戻った",
        )

    artifact_case.screenshot(admin_page, 10, "admin-ingestion-settings-reset-to-env-defaults")


def test_admin_vlm_controls_persist_then_reset_to_environment_default(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/admin/settings", save_as="state/admin-vlm-controls-before.json")
    original = _configured(before, "vlm")
    artifact_case.add_cleanup(
        "restore VLM settings after control inventory",
        lambda: live_api.put_json("/admin/settings", {"vlm": original}),
    )
    effective = (before.get("vlm") or {}).get("effective") or {}
    provider = str(effective.get("provider") or "ollama")
    alternate = "openai" if provider == "ollama" else "ollama"
    model = f"ui-control-vlm-{unique_id('model')[-8:]}"
    cloud_allowed = bool(effective.get("cloud_allowed"))

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#vlm-provider")).to_be_visible()
    admin_page.locator("#vlm-provider").select_option(alternate)
    admin_page.locator("#vlm-provider").select_option(provider)
    admin_page.locator("#vlm-model").fill(model)
    admin_page.locator("#vlm-cloud-allowed").set_checked(not cloud_allowed)
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
        timeout=ui_config.timeout_ms,
    ) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200, save_info.value.text()
    configured = (save_info.value.json().get("vlm") or {}).get("configured") or {}
    assert configured == {
        "provider": provider,
        "model": model,
        "cloud_allowed": not cloud_allowed,
    }
    for control_key, assertion in (
        ("vlm-provider", "選択したVLM providerが実管理設定APIの保存値と一致した"),
        ("vlm-model", "入力したVLM modelが実管理設定APIの保存値と一致した"),
        ("vlm-cloud-allowed", "変更したcloud送信許可が実管理設定APIの保存値と一致した"),
    ):
        artifact_case.attest_control_state(control_key=control_key, state="normal", assertion=assertion)

    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
        timeout=ui_config.timeout_ms,
    ) as reset_info:
        admin_page.locator("#vlm-reset").click()
    assert reset_info.value.status == 200, reset_info.value.text()
    assert _configured(reset_info.value.json(), "vlm") is None
    artifact_case.attest_control_state(
        control_key="vlm-reset",
        state="normal",
        assertion="VLM reset API成功後にVLM保存上書き値が未設定へ戻った",
    )
    artifact_case.screenshot(admin_page, 10, "admin-vlm-controls-saved-and-reset")


def test_admin_usage_and_subagent_profile_controls_persist(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/admin/settings", save_as="state/admin-subagent-controls-before.json")
    profiles = before.get("subagent_profiles")
    assert isinstance(profiles, dict), "full stack did not expose real subagent profiles"
    original_profiles = profiles.get("configured")
    original_metering = _configured(before, "usage_metering")
    artifact_case.add_cleanup(
        "restore usage and subagent profile settings",
        lambda: live_api.put_json(
            "/admin/settings",
            {
                "usage_metering": original_metering,
                "subagent_profiles": original_profiles,
            },
        ),
    )

    profile_id = unique_id("ui-worker")[:32]
    target_metering = not bool((before.get("usage_metering") or {}).get("effective"))
    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    expect(admin_page.locator("#usage-metering")).to_be_visible()
    admin_page.locator("#usage-metering").set_checked(target_metering)
    before_rows = admin_page.locator("#subagent-profiles-list .sap-row").count()
    admin_page.locator("#subagent-profile-add").click()
    expect(admin_page.locator("#subagent-profiles-list .sap-row")).to_have_count(before_rows + 1)
    row = admin_page.locator("#subagent-profiles-list .sap-row").last
    row.locator(".sap-id").fill(profile_id)
    row.locator(".sap-name").fill("UI control inventory worker")
    row.locator(".sap-description").fill("実サービスの資料検索を確認する隔離試験用worker")
    provider = row.locator(".sap-provider option").first.get_attribute("value")
    assert provider
    row.locator(".sap-provider").select_option(provider)
    row.locator(".sap-model").fill("")
    tool = row.locator("[data-sap-tool]").first
    expect(tool).to_be_visible()
    tool_name = str(tool.get_attribute("data-sap-tool") or "")
    assert tool_name
    tool.check()
    row.locator(".sap-min-citations").fill("1")
    enabled_control = row.locator(".sap-enabled-cb")
    expect(enabled_control).to_be_checked()
    enabled_control.uncheck()

    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
        timeout=ui_config.timeout_ms,
    ) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200, save_info.value.text()
    saved = save_info.value.json()
    assert (saved.get("usage_metering") or {}).get("effective") is target_metering
    configured_profiles = (saved.get("subagent_profiles") or {}).get("configured") or []
    saved_profile = next(item for item in configured_profiles if item.get("id") == profile_id)
    assert tool_name in (saved_profile.get("tools") or [])
    assert saved_profile.get("enabled") is False
    artifact_case.attest_control_state(
        control_key="usage-metering",
        state="normal",
        assertion="変更した利用量計測toggleが実管理設定APIのeffective値と一致した",
    )
    assert saved_profile.get("id") == profile_id
    artifact_case.attest_control_state(
        control_key="subagent-profile-add",
        state="normal",
        assertion="追加した実subagent profile IDが管理設定APIの保存一覧へ反映された",
    )
    assert tool_name in (saved_profile.get("tools") or [])
    artifact_case.attest_control_state(
        control_key="@selector:[data-sap-tool]",
        state="normal",
        assertion="選択した動的subagent toolが対象profileの保存tools一覧へ反映された",
    )
    artifact_case.attest_control_state(
        control_key="@selector:.sap-enabled-cb",
        state="normal",
        assertion="追加したsubagent profileの有効toggle解除が実管理設定APIへfalseで保存された",
    )
    artifact_case.screenshot(admin_page, 10, "admin-usage-and-subagent-profile-controls-saved")

    delete_control = row.locator(".sap-del")
    expect(delete_control).to_be_visible()
    delete_control.click()
    expect(admin_page.locator("#subagent-profiles-list .sap-row")).to_have_count(before_rows)
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
        timeout=ui_config.timeout_ms,
    ) as delete_save_info:
        admin_page.locator("#save").click()
    assert delete_save_info.value.status == 200, delete_save_info.value.text()
    remaining_profiles = (delete_save_info.value.json().get("subagent_profiles") or {}).get("configured") or []
    assert all(item.get("id") != profile_id for item in remaining_profiles)
    artifact_case.attest_control_state(
        control_key="@selector:.sap-del",
        state="normal",
        assertion="選択した動的subagent profile行だけを削除し実管理設定一覧からも除外した",
    )
    artifact_case.screenshot(admin_page, 20, "admin-subagent-profile-deleted-and-persisted")


def test_admin_user_edit_controls_update_role_status_and_password(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    uid = unique_id("ui-edit-member")
    initial_password = runtime_password()
    changed_password = runtime_password()
    artifact_case.register_secret(initial_password)
    artifact_case.register_secret(changed_password)
    created = live_api.post_json(
        "/admin/users",
        {
            "uid": uid,
            "display_name": "UI Edit Control Member",
            "role": "user",
            "password": initial_password,
        },
    )
    assert created.get("ok") is True
    artifact_case.add_cleanup(
        f"disable edited user {uid}",
        lambda: live_api.patch_json(f"/admin/users/{uid}", {"status": "disabled"}),
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-users.html")
    row = admin_page.locator("#user-tbody tr", has_text=uid)
    expect(row).to_be_visible()
    row.locator(f'[data-edit="{uid}"]').click()
    expect(admin_page.locator("#edit-overlay")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="@selector:[data-edit]",
        state="normal",
        assertion="選択した対象userのroleとstatus編集dialogだけが開いた",
    )

    modal_close = admin_page.locator("#edit-overlay .modal-close")
    admin_page.locator("#edit-role").select_option("admin")
    admin_page.locator("#edit-status").select_option("disabled")
    modal_close.click()
    expect(admin_page.locator("#edit-overlay")).to_be_hidden()
    close_discarded = live_api.get_json("/admin/users", save_as="state/admin-user-after-dirty-x-close.json")
    close_discarded_user = next(item for item in close_discarded.get("users") or [] if item.get("uid") == uid)
    assert close_discarded_user.get("role") == "user" and close_discarded_user.get("status") == "active"
    artifact_case.attest_control_state(
        control_key="@selector:.modal-close",
        state="abnormal",
        assertion="X closeで未保存のadmin化と無効化を実user更新成功として扱わず元値を維持した",
    )
    artifact_case.screenshot(admin_page, 1, "admin-user-dirty-edit-x-close-discarded")

    row.locator(f'[data-edit="{uid}"]').click()
    expect(admin_page.locator("#edit-overlay")).to_be_visible()
    modal_close.click()
    expect(admin_page.locator("#edit-overlay")).to_be_hidden()
    close_unchanged = live_api.get_json("/admin/users", save_as="state/admin-user-after-plain-x-close.json")
    close_unchanged_user = next(item for item in close_unchanged.get("users") or [] if item.get("uid") == uid)
    assert close_unchanged_user.get("role") == "user" and close_unchanged_user.get("status") == "active"
    artifact_case.attest_control_state(
        control_key="@selector:.modal-close",
        state="normal",
        assertion="X closeで実user値を変更せずuser編集dialogだけを閉じた",
    )
    artifact_case.screenshot(admin_page, 2, "admin-user-edit-x-close-without-change")

    row.locator(f'[data-edit="{uid}"]').click()
    expect(admin_page.locator("#edit-overlay")).to_be_visible()

    cancel = admin_page.locator("#edit-overlay .modal-actions > button.mini")
    artifact_case.arm_unkeyed_control(
        cancel,
        control_key="@unkeyed:web/admin-users.html:152:button",
    )
    cancel.click()
    expect(admin_page.locator("#edit-overlay")).to_be_hidden()
    unchanged = live_api.get_json("/admin/users", save_as="state/admin-user-after-plain-cancel.json")
    unchanged_user = next(item for item in unchanged.get("users") or [] if item.get("uid") == uid)
    assert unchanged_user.get("role") == "user" and unchanged_user.get("status") == "active"
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/admin-users.html:152:button",
        state="normal",
        assertion="取消buttonでdialogだけを閉じ、実userのroleとstatusを変更しなかった",
    )
    artifact_case.screenshot(admin_page, 3, "admin-user-edit-cancel-closed-without-change")

    row.locator(f'[data-edit="{uid}"]').click()
    expect(admin_page.locator("#edit-overlay")).to_be_visible()
    admin_page.locator("#edit-role").select_option("admin")
    admin_page.locator("#edit-status").select_option("disabled")
    artifact_case.arm_unkeyed_control(
        cancel,
        control_key="@unkeyed:web/admin-users.html:152:button",
    )
    cancel.click()
    expect(admin_page.locator("#edit-overlay")).to_be_hidden()
    discarded = live_api.get_json("/admin/users", save_as="state/admin-user-after-dirty-cancel.json")
    discarded_user = next(item for item in discarded.get("users") or [] if item.get("uid") == uid)
    assert discarded_user.get("role") == "user" and discarded_user.get("status") == "active"
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/admin-users.html:152:button",
        state="abnormal",
        assertion="未保存のadmin化と無効化を取消し、実user更新成功として扱わず元値を維持した",
    )
    artifact_case.screenshot(admin_page, 4, "admin-user-dirty-edit-cancel-discarded")

    row.locator(f'[data-edit="{uid}"]').click()
    expect(admin_page.locator("#edit-overlay")).to_be_visible()
    admin_page.locator("#edit-role").select_option("admin")
    admin_page.locator("#edit-status").select_option("active")
    artifact_case.stop_trace(save=False)
    try:
        admin_page.locator("#edit-pw").fill(changed_password)
        with admin_page.expect_response(
            lambda response: response.request.method == "PATCH" and response.url.endswith(f"/admin/users/{uid}"),
            timeout=ui_config.timeout_ms,
        ) as update_info:
            admin_page.locator("#edit-submit").click()
        assert update_info.value.status == 200, update_info.value.text()
    finally:
        if admin_page.locator("#edit-pw").count():
            admin_page.locator("#edit-pw").evaluate("element => { element.value = ''; }")
        artifact_case.start_trace(admin_page.context)
    expect(admin_page.locator("#edit-overlay")).to_be_hidden()
    expect(admin_page.locator("#user-tbody tr", has_text=uid)).to_contain_text("管理者")
    users = live_api.get_json("/admin/users", save_as="state/admin-users-after-edit.json")
    edited = next(item for item in users.get("users") or [] if item.get("uid") == uid)
    assert edited.get("role") == "admin" and edited.get("status") == "active"
    artifact_case.attest_control_state(
        control_key="edit-role",
        state="normal",
        assertion="選択したadmin roleが実user APIの対象userへ保存された",
    )
    artifact_case.attest_control_state(
        control_key="edit-status",
        state="normal",
        assertion="選択したactive状態が実user APIの対象userへ保存された",
    )
    artifact_case.attest_control_state(
        control_key="edit-submit",
        state="normal",
        assertion="user更新API成功後に対象userのroleとstatusが一覧とAPIで一致した",
    )
    artifact_case.screenshot(admin_page, 10, "admin-user-edit-controls-persisted")


def test_audit_filter_controls_apply_real_actor_action_outcome_severity_and_time(admin_page, live_api, ui_config, artifact_case):
    latest = live_api.get_json("/admin/audit?limit=1").get("rows") or []
    assert latest, "real audit store contains no row to filter"
    sample = latest[0]
    actor = str(sample.get("actor_user_id") or "")
    action = str(sample.get("action") or "")
    outcome = str(sample.get("outcome") or "")
    severity = str(sample.get("severity") or "")
    assert actor and action and outcome and severity
    timestamp = datetime.fromisoformat(str(sample["created_at"]).replace("Z", "+00:00"))
    local_time = timestamp.astimezone()
    time_from = (local_time - timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")
    time_to = (local_time + timedelta(minutes=1)).strftime("%Y-%m-%dT%H:%M")

    admin_page.goto(ui_config.base_url + "/ui/audit.html")
    detail_column = admin_page.locator('#colcfg input[data-col="detail"]')
    detail_column.check()
    expect(admin_page.locator("#audit-table")).not_to_have_attribute("data-hide", re.compile(r"(?:^|\s)detail(?:\s|$)"))
    artifact_case.attest_control_state(
        control_key="@selector:[data-col]",
        state="normal",
        assertion="detail列の選択操作後にaudit表のdetail非表示指定が解除された",
    )
    detail_column.uncheck()
    expect(admin_page.locator("#audit-table")).to_have_attribute("data-hide", re.compile(r"(?:^|\s)detail(?:\s|$)"))
    admin_page.locator("#f-actor").fill(actor)
    admin_page.locator("#f-action").fill(action)
    admin_page.locator("#f-outcome").select_option(outcome)
    admin_page.locator("#f-severity").select_option(severity)
    admin_page.locator("#f-from").fill(time_from)
    admin_page.locator("#f-to").fill(time_to)
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/admin/audit?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as search_info:
        admin_page.locator("#search-btn").click()
    assert search_info.value.status == 200
    expect(admin_page.locator("#audit-tbody")).to_contain_text(action)
    filtered = search_info.value.json().get("rows") or []
    assert filtered and any(int(row.get("id") or 0) == int(sample["id"]) for row in filtered)
    query = parse_qs(urlsplit(search_info.value.url).query)
    artifact_case.attest_control_state(
        control_key="@selector:[data-col]",
        state="abnormal",
        assertion="detail列を非表示にしても対象の実audit record自体は検索結果に残った",
    )
    assert all(str(row.get("actor_user_id") or "") == actor for row in filtered)
    artifact_case.attest_control_state(
        control_key="f-actor",
        state="normal",
        assertion="指定actorの実audit検索結果がすべて同じactor user IDと一致した",
    )
    artifact_case.attest_control_state(
        control_key="f-action",
        state="normal",
        assertion="指定したaction条件の実audit recordが検索結果へ表示された",
    )
    assert all(str(row.get("outcome") or "") == outcome for row in filtered)
    artifact_case.attest_control_state(
        control_key="f-outcome",
        state="normal",
        assertion="選択outcomeの実audit検索結果がすべて指定結果と一致した",
    )
    assert all(str(row.get("severity") or "") == severity for row in filtered)
    artifact_case.attest_control_state(
        control_key="f-severity",
        state="normal",
        assertion="選択severityの実audit検索結果がすべて指定重大度と一致した",
    )
    assert query.get("time_from") == [time_from + ":00"]
    artifact_case.attest_control_state(
        control_key="f-from",
        state="normal",
        assertion="開始日時入力が実audit APIのtime_from秒精度queryへ反映された",
    )
    assert query.get("time_to") == [time_to + ":59"]
    artifact_case.attest_control_state(
        control_key="f-to",
        state="normal",
        assertion="終了日時入力が実audit APIのtime_to秒精度queryへ反映された",
    )
    artifact_case.attest_control_state(
        control_key="search-btn",
        state="normal",
        assertion="全filter入力後の実audit検索が200となり対象actionを表示した",
    )
    artifact_case.screenshot(admin_page, 10, "admin-audit-all-filters-applied")


def test_audit_pagination_controls_cover_next_and_previous_pages(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    # Audit閲覧そのものも実監査eventになる。隔離DBで十分な実recordを作り、pager境界を検証する。
    for _ in range(105):
        live_api.get_json("/admin/audit?limit=1")

    admin_page.goto(ui_config.base_url + "/ui/audit.html")
    expect(admin_page.locator("#next-btn")).to_be_enabled()
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "offset=100" in response.url,
        timeout=ui_config.timeout_ms,
    ) as next_info:
        admin_page.locator("#next-btn").click()
    assert next_info.value.status == 200
    expect(admin_page.locator("#prev-btn")).to_be_enabled()
    artifact_case.attest_control_state(
        control_key="next-btn",
        state="normal",
        assertion="次page操作がoffset 100の実audit APIを取得し戻る操作を有効化した",
    )
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "offset=0" in response.url,
        timeout=ui_config.timeout_ms,
    ) as previous_info:
        admin_page.locator("#prev-btn").click()
    assert previous_info.value.status == 200
    expect(admin_page.locator("#prev-btn")).to_be_disabled()
    artifact_case.attest_control_state(
        control_key="prev-btn",
        state="normal",
        assertion="前page操作がoffset 0の実audit APIを取得し先頭pageへ戻った",
    )
    artifact_case.screenshot(admin_page, 10, "admin-audit-next-and-previous-pages")


def test_status_recheck_control_refreshes_all_real_service_health(admin_page, live_api, ui_config, artifact_case):
    admin_page.goto(ui_config.base_url + "/ui/status.html")
    expect(admin_page.locator("#health-tbody tr")).not_to_have_count(0)
    checked_before = admin_page.locator("#checked-at").text_content()
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and response.url.endswith("/admin/health?refresh=1"),
        timeout=ui_config.timeout_ms,
    ) as refresh_info:
        admin_page.locator("#recheck-btn").click()
    assert refresh_info.value.status == 200
    expect(admin_page.locator("#recheck-btn")).to_be_enabled()
    expect(admin_page.locator("#status-pill")).to_have_text("正常")
    checked_after = admin_page.locator("#checked-at").text_content()
    assert checked_after and checked_after != "最終チェック: —"
    assert checked_before or checked_after
    health = refresh_info.value.json()
    assert health.get("status") == "ok"
    assert {"postgres", "neo4j", "elasticsearch"} <= {str(item.get("id")) for item in health.get("components") or [] if item.get("ok")}
    artifact_case.attest_control_state(
        control_key="recheck-btn",
        state="normal",
        assertion="再確認操作で実health APIをrefreshし三つの必須store正常を確認した",
    )
    artifact_case.screenshot(admin_page, 10, "admin-status-real-services-rechecked")
