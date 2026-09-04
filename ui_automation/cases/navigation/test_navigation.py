from __future__ import annotations

import re
import time
from urllib.parse import urlsplit

import pytest
from playwright.sync_api import expect


pytestmark = [pytest.mark.ui_automation, pytest.mark.navigation]


def _assert_admin_session(page) -> dict:
    observation = page.evaluate(
        """
        async () => {
          const response = await fetch('/auth/me', {
            method: 'GET',
            credentials: 'same-origin',
            headers: {'Accept': 'application/json'},
          });
          const body = await response.json().catch(() => ({}));
          return {
            status: response.status,
            role: String(body.role || 'unknown'),
            auth_disabled: body.auth_disabled === true,
          };
        }
        """
    )
    assert observation["status"] == 200, observation
    assert observation["role"] == "admin", observation
    assert isinstance(observation["auth_disabled"], bool)
    return observation


def _arm_admin_control(page, artifact_case, control_key: str) -> None:
    authorization = artifact_case.arm_control_authorization(page, control_key=control_key)
    assert authorization["status"] == 200, authorization
    assert authorization["role"] == "admin", authorization


def _finish_internal_navigation(
    page,
    ui_config,
    artifact_case,
    *,
    destination: str,
    control_key: str,
    step: int,
    screenshot_name: str,
    assertion: str,
) -> None:
    page.wait_for_url(f"**/ui/{destination}", timeout=ui_config.timeout_ms)
    actual = urlsplit(page.url)
    expected = urlsplit("/ui/" + destination)
    assert actual.path == expected.path, page.url
    assert actual.fragment == expected.fragment, page.url
    expect(page.locator("sherpa-topbar")).to_be_visible()
    _assert_admin_session(page)
    artifact_case.attest_control_state(
        control_key=control_key,
        state="normal",
        assertion=assertion,
    )
    artifact_case.screenshot(page, step, screenshot_name)


@pytest.mark.destructive
def test_all_primary_navigation_links(admin_page, ui_config, artifact_case, live_api, real_world):
    destinations = [
        ("ホーム", "home.html"),
        ("チャット", "chat.html"),
        ("資料", "ingest.html"),
        ("ナレッジグラフ", "graph.html"),
        ("使い方", "manual.html"),
        ("個人設定", "settings.html"),
        ("マイワークスペース", "workspace.html"),
        ("システム管理", "admin-settings.html"),
    ]
    for index, (label, filename) in enumerate(destinations, 1):
        admin_page.goto(ui_config.base_url + "/ui/home.html")
        link = admin_page.locator("#sherpa-nav a", has_text=label)
        expect(link).to_be_visible()
        _arm_admin_control(admin_page, artifact_case, f"@href:{filename}")
        link.click()
        admin_page.wait_for_url(f"**/ui/{filename}**", timeout=ui_config.timeout_ms)
        expect(admin_page.locator("sherpa-topbar")).to_be_visible()
        artifact_case.screenshot(admin_page, index * 10, f"navigation-{filename.removesuffix('.html')}-loaded")

    admin_page.goto(ui_config.base_url + "/ui/ingest-new.html")
    admin_page.wait_for_url("**/ui/ingest.html**", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("sherpa-topbar")).to_be_visible()
    artifact_case.screenshot(admin_page, 90, "navigation-ingest-legacy-entry-redirected")

    admin_page.goto(ui_config.base_url + "/ui/home.html")
    theme_button = admin_page.locator("#themebtn")
    before_theme = admin_page.locator("html").get_attribute("data-theme")
    assert before_theme in {"light", "dark"}
    theme_button.click()
    expected_theme = "light" if before_theme == "dark" else "dark"
    expect(admin_page.locator("html")).to_have_attribute("data-theme", expected_theme)
    assert admin_page.evaluate("localStorage.getItem('sherpa-theme')") == expected_theme
    admin_page.reload()
    expect(admin_page.locator("html")).to_have_attribute("data-theme", expected_theme)
    artifact_case.attest_control_state(
        control_key="themebtn",
        state="normal",
        assertion="theme操作でlight darkを切り替えlocal storageから再読込後も同じ値を保持した",
    )

    admin_page.evaluate("localStorage.setItem('sherpa-theme', 'unsupported-theme')")
    admin_page.reload()
    expect(admin_page.locator("html")).to_have_attribute("data-theme", "unsupported-theme")
    theme_button = admin_page.locator("#themebtn")
    expect(theme_button).to_be_visible()
    theme_button.click()
    expect(admin_page.locator("html")).to_have_attribute("data-theme", "dark")
    assert admin_page.evaluate("localStorage.getItem('sherpa-theme')") == "dark"
    artifact_case.attest_control_state(
        control_key="themebtn",
        state="abnormal",
        assertion="未知theme保存値からの操作が不能にならず有効なdark値へ安全に復旧した",
    )
    artifact_case.screenshot(admin_page, 100, "navigation-theme-invalid-value-recovered-to-dark")

    admin_page.goto(ui_config.base_url + "/ui/home.html")
    home_chat_link = admin_page.locator('a.home-cta[href="chat.html"]')
    expect(home_chat_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@selector:.home-cta")
    home_chat_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="chat.html",
        control_key="@selector:.home-cta",
        step=110,
        screenshot_name="navigation-home-cta-chat-loaded",
        assertion="home画面の主要CTAを管理者として実クリックし認証済みchat画面へ遷移した",
    )

    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    ingest_manual_link = admin_page.locator('a[href="manual.html#register"]')
    expect(ingest_manual_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:manual.html")
    ingest_manual_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="manual.html#register",
        control_key="@href:manual.html",
        step=120,
        screenshot_name="navigation-ingest-manual-register-anchor-loaded",
        assertion="ingest画面の使い方linkを実クリックしmanualのregister章とadmin認証を確認した",
    )

    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    ingest_graph_link = admin_page.locator('a.help-link[href="graph.html"]')
    expect(ingest_graph_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:graph.html")
    ingest_graph_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="graph.html",
        control_key="@href:graph.html",
        step=130,
        screenshot_name="navigation-ingest-graph-loaded",
        assertion="ingest画面のgraph導線を実クリックし認証済みknowledge graph画面へ遷移した",
    )

    original_settings = live_api.get_json("/settings", save_as="state/navigation-settings-before.json")
    restore_settings = {
        "extract_provider": original_settings.get("extract_provider") or "auto",
        "graph_provider": original_settings.get("graph_provider") or "",
        "ollama_url": original_settings.get("ollama_url") or "",
        "ollama_model": original_settings.get("ollama_model") or "qwen2.5",
    }
    artifact_case.add_cleanup(
        "restore navigation graph provider settings",
        lambda: live_api.put_json("/settings", restore_settings),
    )
    forced_settings = live_api.put_json(
        "/settings",
        {
            "extract_provider": "ollama",
            "graph_provider": "ollama",
            "ollama_url": "http://127.0.0.1:1",
            "ollama_model": "ui-navigation-unreachable",
        },
        save_as="state/navigation-unreachable-ollama-settings.json",
    )
    assert forced_settings.get("graph_provider") == "ollama", forced_settings
    assert forced_settings.get("ollama_url") == "http://127.0.0.1:1", forced_settings
    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    extract_button = admin_page.locator(f'[data-extract="{real_world}"]')
    expect(extract_button).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@selector:[data-extract]")
    admin_page.once("dialog", lambda dialog: dialog.accept())
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith(f"/worlds/{real_world}/extract"),
        timeout=ui_config.timeout_ms,
    ) as failed_extract_info:
        extract_button.click()
    failed_extract = failed_extract_info.value
    assert failed_extract.status == 503, failed_extract.text()
    failed_detail = failed_extract.json().get("detail", "")
    assert "LLM" in failed_detail or "AI" in failed_detail, failed_detail
    ingest_settings_link = admin_page.locator('#listmsg a[href="settings.html"]')
    expect(ingest_settings_link).to_be_visible()
    artifact_case.attest_control_state(
        control_key="@selector:[data-extract]",
        state="abnormal",
        assertion="到達不能な実Ollamaへのgraph抽出が503となり成功表示せず設定導線を表示した",
    )
    artifact_case.screenshot(admin_page, 135, "navigation-ingest-real-llm-failure-settings-link-visible")
    _arm_admin_control(admin_page, artifact_case, "@href:settings.html")
    ingest_settings_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="settings.html",
        control_key="@href:settings.html",
        step=140,
        screenshot_name="navigation-ingest-ai-failure-settings-loaded",
        assertion="実LLM接続失敗で表示されたsettings導線を実クリックし認証済み設定画面へ遷移した",
    )

    legacy_page = admin_page.context.new_page()
    cdp = admin_page.context.new_cdp_session(legacy_page)
    try:
        cdp.send("Emulation.setScriptExecutionDisabled", {"value": True})
        legacy_page.goto(ui_config.base_url + "/ui/ingest-new.html")
        legacy_link = legacy_page.locator('a[href="ingest.html"]')
        expect(legacy_link).to_be_visible()
        cdp.send("Emulation.setScriptExecutionDisabled", {"value": False})
        artifact_case.attach_page(legacy_page)
        _arm_admin_control(legacy_page, artifact_case, "@href:ingest.html")
        legacy_link.click()
        _finish_internal_navigation(
            legacy_page,
            ui_config,
            artifact_case,
            destination="ingest.html",
            control_key="@href:ingest.html",
            step=150,
            screenshot_name="navigation-ingest-legacy-noscript-link-loaded",
            assertion="script無効時の旧ingest救済linkを実クリックし認証済み現行ingest画面へ遷移した",
        )
    finally:
        try:
            cdp.detach()
        finally:
            if not legacy_page.is_closed():
                legacy_page.close()

    admin_page.goto(ui_config.base_url + "/ui/graph.html")
    graph_manual_link = admin_page.locator('a[href="manual.html#graph"]')
    expect(graph_manual_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:manual.html")
    graph_manual_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="manual.html#graph",
        control_key="@href:manual.html",
        step=160,
        screenshot_name="navigation-graph-manual-anchor-loaded",
        assertion="graph画面の使い方linkを実クリックしmanualのgraph章とadmin認証を確認した",
    )

    admin_page.goto(ui_config.base_url + "/ui/graph.html")
    graph_ingest_link = admin_page.locator('a.help-link[href="ingest.html"]')
    expect(graph_ingest_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:ingest.html")
    graph_ingest_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="ingest.html",
        control_key="@href:ingest.html",
        step=170,
        screenshot_name="navigation-graph-ingest-loaded",
        assertion="graph画面の取込状況linkを実クリックし認証済みingest画面へ遷移した",
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    admin_manual_link = admin_page.locator('a[href="manual.html#sysadmin"]')
    expect(admin_manual_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:manual.html")
    admin_manual_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="manual.html#sysadmin",
        control_key="@href:manual.html",
        step=180,
        screenshot_name="navigation-admin-settings-manual-anchor-loaded",
        assertion="system管理画面の使い方linkを実クリックしmanualのsysadmin章と認証を確認した",
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    admin_users_link = admin_page.locator('a.tab-link[href="admin-users.html"]')
    expect(admin_users_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:admin-users.html")
    admin_users_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="admin-users.html",
        control_key="@href:admin-users.html",
        step=190,
        screenshot_name="navigation-admin-settings-users-loaded",
        assertion="system管理menuのuser管理を実クリックしadmin認証済みuser管理画面へ遷移した",
    )

    # 資料（ingest.html）へのタブは admin-settings から撤去済み（2026-09-04 裁定・ナビの
    # ADMIN_LINKS からのみ遷移する）。ここではトップナビの資料リンク経由で検証する。
    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    admin_ingest_link = admin_page.locator('nav a[href="ingest.html"]')
    expect(admin_ingest_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:ingest.html")
    admin_ingest_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="ingest.html",
        control_key="@href:ingest.html",
        step=200,
        screenshot_name="navigation-admin-settings-ingest-loaded",
        assertion="トップナビの資料リンクを実クリックしadmin認証済みingest画面へ遷移した",
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    admin_usage_link = admin_page.locator('a.tab-link[href="usage.html"]')
    expect(admin_usage_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:usage.html")
    admin_usage_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="usage.html",
        control_key="@href:usage.html",
        step=210,
        screenshot_name="navigation-admin-settings-usage-loaded",
        assertion="system管理menuのusageを実クリックしadmin認証済み利用量画面へ遷移した",
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    admin_audit_link = admin_page.locator('a.tab-link[href="audit.html"]')
    expect(admin_audit_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:audit.html")
    admin_audit_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="audit.html",
        control_key="@href:audit.html",
        step=220,
        screenshot_name="navigation-admin-settings-audit-loaded",
        assertion="system管理menuのauditを実クリックしadmin認証済み監査画面へ遷移した",
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    admin_status_link = admin_page.locator('a.tab-link[href="status.html"]')
    expect(admin_status_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:status.html")
    admin_status_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="status.html",
        control_key="@href:status.html",
        step=230,
        screenshot_name="navigation-admin-settings-status-loaded",
        assertion="system管理menuのstatusを実クリックしadmin認証済み状態画面へ遷移した",
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-users.html")
    users_admin_settings_link = admin_page.locator('a.crumb[href="admin-settings.html"]')
    expect(users_admin_settings_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:admin-settings.html")
    users_admin_settings_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="admin-settings.html",
        control_key="@href:admin-settings.html",
        step=240,
        screenshot_name="navigation-admin-users-settings-loaded",
        assertion="user管理のbreadcrumbを実クリックしadmin認証済みsystem管理画面へ戻った",
    )

    admin_page.goto(ui_config.base_url + "/ui/audit.html")
    audit_admin_settings_link = admin_page.locator('a.crumb[href="admin-settings.html"]')
    expect(audit_admin_settings_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:admin-settings.html")
    audit_admin_settings_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="admin-settings.html",
        control_key="@href:admin-settings.html",
        step=250,
        screenshot_name="navigation-audit-admin-settings-loaded",
        assertion="audit画面のbreadcrumbを実クリックしadmin認証済みsystem管理画面へ戻った",
    )

    admin_page.goto(ui_config.base_url + "/ui/usage.html")
    usage_admin_settings_link = admin_page.locator('a.crumb[href="admin-settings.html"]')
    expect(usage_admin_settings_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:admin-settings.html")
    usage_admin_settings_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="admin-settings.html",
        control_key="@href:admin-settings.html",
        step=260,
        screenshot_name="navigation-usage-admin-settings-loaded",
        assertion="usage画面のbreadcrumbを実クリックしadmin認証済みsystem管理画面へ戻った",
    )

    admin_page.goto(ui_config.base_url + "/ui/status.html")
    status_admin_settings_link = admin_page.locator('a.crumb[href="admin-settings.html"]')
    expect(status_admin_settings_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:admin-settings.html")
    status_admin_settings_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="admin-settings.html",
        control_key="@href:admin-settings.html",
        step=270,
        screenshot_name="navigation-status-admin-settings-loaded",
        assertion="status画面のbreadcrumbを実クリックしadmin認証済みsystem管理画面へ戻った",
    )

    admin_page.goto(ui_config.base_url + "/ui/manual.html")
    manual_chat_link = admin_page.locator('a.btn-ghost[href="chat.html"]')
    expect(manual_chat_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:chat.html")
    manual_chat_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="chat.html",
        control_key="@href:chat.html",
        step=280,
        screenshot_name="navigation-manual-chat-loaded",
        assertion="manual headerのchat導線を実クリックしadmin認証済みchat画面へ遷移した",
    )

    admin_page.goto(ui_config.base_url + "/ui/manual.html")
    manual_ingest_link = admin_page.locator('a.btn-ghost[href="ingest.html"]')
    expect(manual_ingest_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:ingest.html")
    manual_ingest_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="ingest.html",
        control_key="@href:ingest.html",
        step=290,
        screenshot_name="navigation-manual-ingest-loaded",
        assertion="manual headerの資料導線を実クリックしadmin認証済みingest画面へ遷移した",
    )


def test_narrow_viewport_primary_navigation(admin_page, ui_config, artifact_case):
    admin_page.set_viewport_size({"width": 390, "height": 844})
    for index, filename in enumerate(
        (
            "home.html",
            "chat.html",
            "ingest.html",
            "graph.html",
            "manual.html",
            "settings.html",
            "workspace.html",
            "admin-settings.html",
            "status.html",
        ),
        1,
    ):
        admin_page.goto(ui_config.base_url + "/ui/" + filename)
        expect(admin_page.locator("sherpa-topbar")).to_be_visible()
        overflow = admin_page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
        assert overflow <= 2, f"{filename} overflows narrow viewport by {overflow}px"
        artifact_case.screenshot(admin_page, index * 10, f"navigation-narrow-{filename.removesuffix('.html')}")


@pytest.mark.destructive
def test_help_manual_swagger_internal_links_and_keyboard(admin_page, ui_config, artifact_case, isolated_stack):
    admin_page.goto(ui_config.base_url + "/ui/workspace.html")
    workspace_manual_link = admin_page.locator('a[href="manual.html#workspace"]')
    expect(workspace_manual_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:manual.html")
    workspace_manual_link.click()
    _finish_internal_navigation(
        admin_page,
        ui_config,
        artifact_case,
        destination="manual.html#workspace",
        control_key="@href:manual.html",
        step=5,
        screenshot_name="help-workspace-manual-anchor-loaded",
        assertion="workspace画面の使い方linkを実クリックしmanualのworkspace章と認証を確認した",
    )

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    help_link = admin_page.locator('a.help-link[href="manual.html#settings"]')
    expect(help_link).to_be_visible()
    _arm_admin_control(admin_page, artifact_case, "@href:manual.html")
    help_link.click()
    admin_page.wait_for_url("**/ui/manual.html#settings", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#doc-title")).not_to_have_text("読み込み中...")
    expect(admin_page.locator("#manual-nav a.on")).to_be_visible()
    expect(admin_page.locator("#manual-doc")).not_to_be_empty()
    artifact_case.screenshot(admin_page, 10, "help-settings-manual-anchor-loaded")

    admin_page.locator("#doc-search").fill("共有")
    expect(admin_page.locator("#manual-filter-result")).to_contain_text("件のページが一致しました")
    expect(admin_page.locator("#manual-nav a")).not_to_have_count(0)
    artifact_case.attest_control_state(
        control_key="doc-search",
        state="normal",
        assertion="manual検索語に一致する実文書項目と一致件数を目次へ表示した",
    )
    manual_entry = admin_page.locator("#manual-nav a[data-doc]").first
    target_document = manual_entry.get_attribute("data-doc")
    assert target_document
    manual_entry.click()
    admin_page.wait_for_url(f"**/ui/manual.html#{target_document}", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#manual-nav a.on")).to_have_attribute("data-doc", target_document)
    expect(admin_page.locator("#manual-doc")).not_to_be_empty()
    artifact_case.attest_control_state(
        control_key="@selector:[data-doc]",
        state="normal",
        assertion="動的manual目次操作が選択anchorと一致する実Markdown本文を表示した",
    )
    artifact_case.screenshot(admin_page, 20, "help-manual-search-filtered")

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    ai_studio_link = admin_page.locator('a[href="https://aistudio.google.com/apikey"]')
    expect(ai_studio_link).to_have_count(1)
    expect(ai_studio_link).to_be_visible()
    expect(ai_studio_link).to_have_attribute("target", "_blank")
    rel_tokens = set((ai_studio_link.get_attribute("rel") or "").split())
    assert "noopener" in rel_tokens, rel_tokens
    _arm_admin_control(admin_page, artifact_case, "@href:https://aistudio.google.com/apikey")
    with admin_page.context.expect_event(
        "request",
        predicate=lambda request: request.is_navigation_request() and request.url.startswith("https://aistudio.google.com/apikey"),
        timeout=ui_config.timeout_ms,
    ) as ai_studio_request_info:
        with admin_page.expect_popup(timeout=ui_config.timeout_ms) as ai_studio_popup_info:
            ai_studio_link.click()
    ai_studio_request = ai_studio_request_info.value
    ai_studio_popup = ai_studio_popup_info.value
    try:
        requested = urlsplit(ai_studio_request.url)
        assert requested.scheme == "https", ai_studio_request.url
        assert requested.hostname == "aistudio.google.com", ai_studio_request.url
        assert requested.path.rstrip("/") == "/apikey", ai_studio_request.url
        assert ai_studio_request.frame == ai_studio_popup.main_frame
        _assert_admin_session(admin_page)
        artifact_case.attest_control_state(
            control_key="@href:https://aistudio.google.com/apikey",
            state="normal",
            assertion="settings外部linkを実クリックしnoopener popupがGoogle AI Studio宛へ要求した",
        )
        artifact_case.write_json(
            "state/google-ai-studio-popup.json",
            {
                "href": ai_studio_link.get_attribute("href"),
                "target": ai_studio_link.get_attribute("target"),
                "rel": sorted(rel_tokens),
                "popup_navigation_request": ai_studio_request.url,
                "communication_success_required": False,
            },
        )
        artifact_case.screenshot(admin_page, 22, "help-settings-google-ai-studio-popup-requested")
    finally:
        if not ai_studio_popup.is_closed():
            ai_studio_popup.close()

    admin_page.goto(ui_config.base_url + f"/ui/manual.html#{target_document}")
    expect(admin_page.locator("#doc-search")).to_be_visible()
    admin_page.locator("#doc-search").fill("SHERPA-MANUAL-NO-MATCH-927")
    expect(admin_page.locator("#manual-filter-result")).to_have_text("0件のページが一致しました")
    expect(admin_page.locator("#manual-nav a[data-doc]")).to_have_count(0)
    artifact_case.attest_control_state(
        control_key="doc-search",
        state="abnormal",
        assertion="該当しない検索語で0件を表示し偽のmanual文書linkを生成しなかった",
    )

    admin_page.context.grant_permissions(
        ["clipboard-read", "clipboard-write"],
        origin=ui_config.base_url,
    )
    admin_page.locator("#doc-search").fill("")
    operations_entry = admin_page.locator('#manual-nav a[data-doc="operations"]')
    expect(operations_entry).to_be_visible()
    operations_entry.click()
    admin_page.wait_for_url("**/ui/manual.html#operations", timeout=ui_config.timeout_ms)
    code_block = admin_page.locator("#manual-doc pre").first
    copy_button = code_block.locator(".manual-copy")
    expect(copy_button).to_be_visible()
    expected_code = code_block.evaluate("element => element.innerText.replace(/\\s*コピー$/, '')")
    assert expected_code.strip(), "rendered operations manual has no copyable code"
    copy_button.click()
    expect(copy_button).to_have_text("コピー済み")
    clipboard_code = admin_page.evaluate("() => navigator.clipboard.readText()")
    assert clipboard_code == expected_code, "manual copy control changed the rendered command text"
    artifact_case.attest_control_state(
        control_key="@selector:.manual-copy",
        state="normal",
        assertion="manual表示command全文とclipboardへcopyされた内容が完全一致した",
    )
    artifact_case.screenshot(admin_page, 25, "help-manual-real-command-copied-exactly")

    sanitized_link = admin_page.locator('#manual-doc a[href="#runbook"]').first
    expect(sanitized_link).to_be_visible()
    assert sanitized_link.get_attribute("target") is None
    artifact_case.arm_unkeyed_control(
        sanitized_link,
        control_key="@unkeyed:web/manual.js:119:a",
    )
    sanitized_link.click()
    admin_page.wait_for_url("**/ui/manual.html#runbook", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#doc-title")).to_have_text("運用 Runbook（障害対応・バックアップ・復旧）")
    expect(admin_page.locator('#manual-nav a[data-doc="runbook"]')).to_have_class(re.compile(r"(?:^|\s)on(?:\s|$)"))
    unsafe_links = admin_page.locator(
        '#manual-doc a[href^="javascript:"], #manual-doc a[href^="data:"], #manual-doc a[href^="mailto:"]',
    )
    expect(unsafe_links).to_have_count(0)
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/manual.js:119:a",
        state="normal",
        assertion="sanitization済み章間linkが許可済みrunbook anchorだけへ遷移し危険schemeを残さなかった",
    )
    artifact_case.screenshot(admin_page, 27, "help-manual-sanitized-internal-link-opened-runbook")

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    with admin_page.expect_popup() as popup_info:
        admin_page.locator('a[href="/docs"]').click()
    swagger = popup_info.value
    artifact_case.attach_page(swagger)
    swagger.wait_for_load_state("domcontentloaded")
    expect(swagger.locator(".swagger-ui")).to_be_visible(timeout=ui_config.timeout_ms)
    assert swagger.url.rstrip("/").endswith("/docs")
    artifact_case.screenshot(swagger, 30, "help-swagger-ui-real-openapi-loaded")
    swagger.close()

    internal_paths = {
        "/ui/home.html",
        "/ui/chat.html",
        "/ui/ingest.html",
        "/ui/graph.html",
        "/ui/manual.html",
        "/ui/settings.html",
        "/ui/workspace.html",
        "/ui/admin-settings.html",
        "/ui/admin-users.html",
        "/ui/usage.html",
        "/ui/audit.html",
        "/ui/status.html",
        "/docs",
    }
    statuses = {}
    for path in sorted(internal_paths):
        started = time.monotonic()
        response = admin_page.request.get(ui_config.base_url + path)
        elapsed = int((time.monotonic() - started) * 1000)
        statuses[path] = response.status
        artifact_case.record_api(
            method="GET",
            url=ui_config.base_url + path,
            status=response.status,
            elapsed_ms=elapsed,
        )
        assert 200 <= response.status < 400, f"internal navigation target is broken: {path} -> {response.status}"
    artifact_case.write_json("state/internal-link-status.json", statuses)

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    admin_page.locator("#topbar-user").click()
    expect(admin_page.locator("#usermenu")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="topbar-user",
        state="normal",
        assertion="設定画面の認証済みtopbar操作で実ユーザーメニューを表示した",
    )
    admin_page.keyboard.press("Escape")
    expect(admin_page.locator("#usermenu")).to_be_hidden()
    expect(admin_page.locator("#topbar-user")).to_be_focused()
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/settings"),
        timeout=ui_config.timeout_ms,
    ) as keyboard_save:
        admin_page.keyboard.press("Control+s")
    assert keyboard_save.value.status == 200
    expect(admin_page.locator("#msg")).to_contain_text("保存しました")
    artifact_case.screenshot(admin_page, 40, "keyboard-escape-focus-and-control-save-work")
