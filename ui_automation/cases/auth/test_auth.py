from __future__ import annotations

import pytest
from playwright.sync_api import expect

from ui_automation.support.live_api import LiveApi
from ui_automation.support.ui import login_without_trace, runtime_password, unique_id


pytestmark = [pytest.mark.ui_automation, pytest.mark.auth]


@pytest.mark.environment
def test_anonymous_redirect_and_auth_disabled_profile(page, ui_config, artifact_case):
    assert ui_config.expected_auth_disabled is not None, "SHERPA_UI_EXPECT_AUTH_DISABLED must declare the active authentication profile"
    page.context.clear_cookies()
    artifact_case.start_trace(page.context)
    artifact_case.begin_auth_bootstrap()
    try:
        response = page.goto(ui_config.base_url + "/ui/admin-settings.html")
        assert response is not None
        client = LiveApi(ui_config.base_url, page.context, artifact_case)
        if ui_config.expected_auth_disabled:
            assert "/ui/login.html" not in page.url, "auth-disabled profile redirected an anonymous browser"
            me = client.get_json("/auth/me", save_as="state/auth-disabled-user.json")
            assert me.get("auth_disabled") is True and me.get("role") == "admin", me
            expect(page.locator("#main-content")).to_be_visible()
            expect(page.locator("#access-denied")).to_be_hidden()
            artifact_case.screenshot(page, 10, "auth-disabled-anonymous-admin-surface-available")
        else:
            page.wait_for_url("**/ui/login.html?next=**", timeout=ui_config.timeout_ms)
            assert "admin-settings.html" in page.url, "login redirect lost the protected destination"
            unauthenticated = client.request("GET", "/auth/me", expected=401)
            assert unauthenticated.status == 401
            artifact_case.screenshot(page, 10, "auth-enabled-anonymous-protected-page-redirected")
        page.wait_for_timeout(100)
    finally:
        artifact_case.end_auth_bootstrap()


def test_login_session_logout(page, ui_config, artifact_case, admin_credentials):
    assert ui_config.expected_auth_disabled is not True, "login case requires the auth-enabled environment profile"
    page.goto(ui_config.base_url + "/ui/login.html")
    expect(page.locator("#form")).to_be_visible()
    artifact_case.screenshot(page, 10, "auth-login-form-empty")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#form")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 2
    artifact_case.screenshot(page, 15, "auth-login-form-narrow-viewport")
    page.set_viewport_size({"width": 1440, "height": 1000})

    invalid_password = runtime_password()
    artifact_case.register_secret(invalid_password)
    page.locator("#username").fill(unique_id("missing-login"))
    page.locator("#password").fill(invalid_password)
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/auth/login"),
        timeout=ui_config.timeout_ms,
    ) as rejected_login_info:
        page.locator("#submit").click()
    assert rejected_login_info.value.status == 401, rejected_login_info.value.text()
    expect(page.locator("#err")).to_contain_text("正しくありません")
    assert page.url.endswith("/ui/login.html"), "invalid credentials escaped the login surface"
    artifact_case.attest_control_state(
        control_key="username",
        state="abnormal",
        assertion="存在しないユーザー名を実ログインAPIが401で拒否しログイン画面を維持した",
    )
    assert rejected_login_info.value.status == 401
    artifact_case.attest_control_state(
        control_key="password",
        state="abnormal",
        assertion="不正パスワードを実ログインAPIが401で拒否し認証済み画面へ遷移しなかった",
    )
    expect(page.locator("#form")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="submit",
        state="abnormal",
        assertion="不正資格情報の送信後もログインformが表示されセッションを開始しなかった",
    )

    page.locator("#username").fill(admin_credentials.username)
    page.locator("#password").fill(admin_credentials.active_password)
    artifact_case.arm_control_authorization(page, control_key="submit")
    page.locator("#submit").click()
    page.wait_for_url("**/ui/change-password.html**", timeout=ui_config.timeout_ms)
    expect(page.locator("#form")).to_be_visible()
    assert page.url.split("?", 1)[0].endswith("/ui/change-password.html")
    artifact_case.attest_control_state(
        control_key="username",
        state="normal",
        assertion="正しい管理者ユーザー名を含む資格情報で初回パスワード変更画面へ遷移した",
    )
    expect(page.locator("#form")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="password",
        state="normal",
        assertion="正しい管理者パスワードで実セッションが成立し初回変更formを表示した",
    )
    assert page.url.split("?", 1)[0].endswith("/ui/change-password.html")
    artifact_case.attest_control_state(
        control_key="submit",
        state="normal",
        assertion="正しい資格情報の送信が成功し保護された初回変更画面へ遷移した",
    )
    artifact_case.screenshot(page, 20, "auth-fresh-admin-forced-password-change-form")
    page.set_viewport_size({"width": 390, "height": 844})
    expect(page.locator("#form")).to_be_visible()
    assert page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 2
    artifact_case.screenshot(page, 22, "auth-admin-password-change-narrow-viewport")
    page.set_viewport_size({"width": 1440, "height": 1000})

    page.locator("#current-password").fill(admin_credentials.active_password)
    page.locator("#new-password").fill(admin_credentials.changed_password)
    page.locator("#confirm-password").fill(admin_credentials.changed_password)
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/auth/change-password"),
        timeout=ui_config.timeout_ms,
    ) as change_info:
        page.locator("#submit").click()
    change_response = change_info.value
    assert change_response.status == 200, "fresh admin password change was rejected"
    page.wait_for_url("**/ui/chat.html**", timeout=ui_config.timeout_ms)
    admin_credentials.active_password = admin_credentials.changed_password
    admin_credentials.initial_change_completed = True
    artifact_case.start_trace(page.context)
    me = LiveApi(ui_config.base_url, page.context, artifact_case).get_json("/auth/me", save_as="state/authenticated-user.json")
    assert me["uid"] == ui_config.admin_user and me["role"] == "admin"
    assert me["auth_disabled"] is False
    artifact_case.write_json(
        "state/initial-admin-password-change.json",
        {
            "changed_in_this_case": True,
            "change_response_status": change_response.status,
            "completed_in_session": admin_credentials.initial_change_completed,
        },
    )
    artifact_case.screenshot(page, 30, "auth-admin-session-established-after-password-change")

    page.locator("#topbar-user").click()
    expect(page.locator("#usermenu")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="topbar-user",
        state="normal",
        assertion="認証済み管理者のtopbar操作で実ユーザーメニューを表示した",
    )
    change_password_link = page.locator("#um-changepw")
    expect(change_password_link).to_be_visible()
    expect(change_password_link).to_have_attribute("href", "change-password.html?next=%2Fui%2Fchat.html")
    change_password_authorization = artifact_case.arm_control_authorization(page, control_key="um-changepw")
    assert change_password_authorization["status"] == 200 and change_password_authorization["role"] == "admin"
    change_password_link.click()
    page.wait_for_url("**/ui/change-password.html?next=**", timeout=ui_config.timeout_ms)
    expect(page.locator("#form")).to_be_visible()
    assert "%2Fui%2Fchat.html" in page.url
    artifact_case.attest_control_state(
        control_key="um-changepw",
        state="normal",
        assertion="変更導線が安全なchat return先を保持して実パスワード変更formへ遷移した",
    )
    artifact_case.screenshot(page, 35, "auth-user-menu-change-password-link-preserves-return-target")
    page.goto(ui_config.base_url + "/ui/chat.html")
    expect(page.locator("#topbar-user")).to_be_visible()
    page.locator("#topbar-user").click()
    expect(page.locator("#usermenu")).to_be_visible()
    page.on("dialog", lambda dialog: dialog.accept())
    artifact_case.arm_control_authorization(page, control_key="um-logout")
    page.locator("#um-logout").click()
    page.wait_for_url("**/ui/login.html**", timeout=ui_config.timeout_ms)
    unauth = LiveApi(ui_config.base_url, page.context, artifact_case).request("GET", "/auth/me", expected=401)
    assert unauth.status == 401
    artifact_case.attest_control_state(
        control_key="um-logout",
        state="normal",
        assertion="ログアウト操作後に実セッションが失効しauth meが401を返した",
    )
    artifact_case.screenshot(page, 40, "auth-session-revoked-login-restored")

    artifact_case.stop_trace(save=True)
    changed_now = login_without_trace(
        page,
        ui_config.base_url,
        admin_credentials,
        "/ui/chat.html",
        ui_config.timeout_ms,
        artifact_case,
    )
    assert not changed_now, "changed admin password unexpectedly required another forced change"
    artifact_case.start_trace(page.context)
    relogged = LiveApi(ui_config.base_url, page.context, artifact_case).get_json(
        "/auth/me", save_as="state/admin-relogin-with-changed-password.json"
    )
    assert relogged["uid"] == ui_config.admin_user and not relogged["must_change_password"]
    artifact_case.screenshot(page, 50, "auth-admin-relogin-with-changed-password-succeeds")


@pytest.mark.admin
@pytest.mark.destructive
def test_new_user_password_change_and_role_boundary(browser, admin_page, live_api, ui_config, artifact_case, isolated_stack):
    assert ui_config.expected_auth_disabled is not True, "password-change case requires auth enabled"
    uid = unique_id("ui-member")
    initial = runtime_password()
    changed = runtime_password()
    artifact_case.register_secret(initial)
    artifact_case.register_secret(changed)

    artifact_case.stop_trace(save=False)
    created = live_api.post_json(
        "/admin/users",
        {"uid": uid, "display_name": "UI Automation Member", "role": "user", "password": initial},
        save_as="state/member-created.json",
    )
    assert created.get("ok") is True
    artifact_case.add_cleanup(
        f"disable user {uid}",
        lambda: live_api.patch_json(f"/admin/users/{uid}", {"status": "disabled"}),
    )

    member_context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="ja-JP")
    member = member_context.new_page()
    artifact_case.attach_page(member)
    try:
        member.goto(ui_config.base_url + "/ui/login.html")
        member.locator("#username").fill(uid)
        member.locator("#password").fill(initial)
        artifact_case.arm_control_authorization(member, control_key="submit")
        member.locator("#submit").click()
        member.wait_for_url("**/ui/change-password.html**", timeout=ui_config.timeout_ms)
        expect(member.locator("#form")).to_be_visible()
        artifact_case.screenshot(member, 3, "auth-member-password-change-desktop-viewport")
        member.set_viewport_size({"width": 390, "height": 844})
        assert member.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth") <= 2
        artifact_case.screenshot(member, 5, "auth-member-password-change-narrow-viewport")
        member.set_viewport_size({"width": 1366, "height": 900})

        rejected_current = runtime_password()
        candidate_password = runtime_password()
        mismatched_confirmation = runtime_password()
        invalid_new_password = "invalid password"
        for secret in (rejected_current, candidate_password, mismatched_confirmation, invalid_new_password):
            artifact_case.register_secret(secret)

        member.locator("#current-password").fill(rejected_current)
        member.locator("#new-password").fill(candidate_password)
        member.locator("#confirm-password").fill(candidate_password)
        with member.expect_response(
            lambda response: response.request.method == "POST" and response.url.endswith("/auth/change-password"),
            timeout=ui_config.timeout_ms,
        ) as rejected_current_info:
            member.locator("#submit").click()
        assert rejected_current_info.value.status == 401, rejected_current_info.value.text()
        expect(member.locator("#err")).to_contain_text("現在のパスワードが正しくありません")
        artifact_case.attest_control_state(
            control_key="current-password",
            state="abnormal",
            assertion="誤った現在パスワードを実変更APIが401で拒否し変更formを維持した",
        )
        assert rejected_current_info.value.status == 401
        artifact_case.attest_control_state(
            control_key="submit",
            state="abnormal",
            assertion="誤った現在パスワードでの変更送信が401となり成功遷移しなかった",
        )

        member.locator("#current-password").fill(initial)
        member.locator("#new-password").fill(candidate_password)
        member.locator("#confirm-password").fill(mismatched_confirmation)
        member.locator("#submit").click()
        expect(member.locator("#err")).to_contain_text("確認入力が一致しません")
        artifact_case.attest_control_state(
            control_key="confirm-password",
            state="abnormal",
            assertion="一致しない確認パスワードをclient検証が拒否し実変更を送信しなかった",
        )

        member.locator("#current-password").fill(initial)
        member.locator("#new-password").fill(invalid_new_password)
        member.locator("#confirm-password").fill(invalid_new_password)
        member.locator("#submit").click()
        expect(member.locator("#err")).to_contain_text("半角英数字・記号のみ")
        artifact_case.attest_control_state(
            control_key="new-password",
            state="abnormal",
            assertion="空白を含む不正な新パスワードをclient検証が拒否し保存しなかった",
        )

        member.locator("#current-password").fill(initial)
        member.locator("#new-password").fill(changed)
        member.locator("#confirm-password").fill(changed)
        member.locator("#submit").click()
        member.wait_for_url("**/ui/chat.html**", timeout=ui_config.timeout_ms)
        assert member.url.split("?", 1)[0].endswith("/ui/chat.html")
        artifact_case.attest_control_state(
            control_key="current-password",
            state="normal",
            assertion="正しい現在パスワードを使った変更が成功してchat画面へ遷移した",
        )
        assert member.url.split("?", 1)[0].endswith("/ui/chat.html")
        artifact_case.attest_control_state(
            control_key="new-password",
            state="normal",
            assertion="強い新パスワードを実ユーザーへ保存してchat画面へ遷移した",
        )
        assert member.url.split("?", 1)[0].endswith("/ui/chat.html")
        artifact_case.attest_control_state(
            control_key="confirm-password",
            state="normal",
            assertion="一致する確認入力を受理して実パスワード変更を完了した",
        )
        expect(member.locator("#topbar-user")).to_be_visible()
        artifact_case.attest_control_state(
            control_key="submit",
            state="normal",
            assertion="パスワード変更送信後に認証済みchat画面のtopbarを表示した",
        )
        artifact_case.start_trace(member_context)
        artifact_case.screenshot(member, 10, "auth-member-password-changed")

        member.goto(ui_config.base_url + "/ui/admin-users.html")
        expect(member.locator("#access-denied")).to_be_visible()
        expect(member.locator("#main-content")).to_be_hidden()
        artifact_case.screenshot(member, 20, "auth-member-admin-access-denied")

        artifact_case.stop_trace(save=True)
        member_context.clear_cookies()
        member.goto(ui_config.base_url + "/ui/login.html")
        member.locator("#username").fill(uid)
        member.locator("#password").fill(changed)
        artifact_case.arm_control_authorization(member, control_key="submit")
        member.locator("#submit").click()
        member.wait_for_url("**/ui/chat.html**", timeout=ui_config.timeout_ms)
        me = LiveApi(ui_config.base_url, member_context, artifact_case).get_json("/auth/me", save_as="state/member-relogin.json")
        assert me["uid"] == uid and me["role"] == "user" and not me["must_change_password"]
        artifact_case.screenshot(member, 30, "auth-member-relogin-with-new-password")

        member_surfaces = (
            ("home.html", "home"),
            ("chat.html", "chat"),
            ("graph.html", "graph"),
            ("manual.html", "manual"),
            ("settings.html", "settings"),
            ("workspace.html", "workspace"),
        )
        for index, (filename, semantic_name) in enumerate(member_surfaces, 4):
            member.goto(ui_config.base_url + "/ui/" + filename)
            expect(member.locator("sherpa-topbar")).to_be_visible()
            if member.locator("#access-denied").count():
                expect(member.locator("#access-denied")).to_be_hidden()
            artifact_case.screenshot(
                member,
                index * 10,
                f"auth-member-{semantic_name}-surface-authorized",
            )

        member.set_viewport_size({"width": 390, "height": 844})
        for index, (filename, semantic_name) in enumerate(member_surfaces, 11):
            member.goto(ui_config.base_url + "/ui/" + filename)
            expect(member.locator("sherpa-topbar")).to_be_visible()
            if member.locator("#access-denied").count():
                expect(member.locator("#access-denied")).to_be_hidden()
            overflow = member.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")
            assert overflow <= 2, f"user {filename} overflows narrow viewport by {overflow}px"
            artifact_case.screenshot(
                member,
                index * 10,
                f"auth-member-{semantic_name}-surface-narrow-authorized",
            )
        member.set_viewport_size({"width": 1366, "height": 900})

        member.goto(ui_config.base_url + "/docs")
        expect(member.locator(".swagger-ui")).to_be_visible(timeout=ui_config.timeout_ms)
        artifact_case.screenshot(member, 170, "auth-member-swagger-surface-authorized")
    finally:
        artifact_case.stop_trace(save=True)
        member_context.close()
