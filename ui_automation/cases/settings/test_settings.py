from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from ui_automation.support.ui import unique_id


pytestmark = [pytest.mark.ui_automation, pytest.mark.settings]


@pytest.mark.destructive
def test_personal_settings_persist_through_api_and_reload(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/settings", save_as="state/settings-before.json")
    original_prompt = str(before.get("system_prompt") or "")
    prompt = f"UI実サービス試験 {unique_id('policy')}。資料にない事実は断定しない。"
    artifact_case.add_cleanup(
        "restore personal system prompt",
        lambda: live_api.put_json("/settings", {"system_prompt": original_prompt}),
    )

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    expect(admin_page.locator("#sysprompt")).to_be_visible()
    admin_page.locator("#sysprompt").fill(prompt)
    with admin_page.expect_response(lambda response: response.request.method == "PUT" and response.url.endswith("/settings")) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200
    expect(admin_page.locator("#msg")).to_contain_text("保存しました")
    artifact_case.screenshot(admin_page, 10, "settings-personal-policy-saved")

    after = live_api.get_json("/settings", save_as="state/settings-after.json")
    assert after.get("system_prompt") == prompt
    admin_page.reload()
    expect(admin_page.locator("#sysprompt")).to_have_value(prompt)
    artifact_case.attest_control_state(
        control_key="sysprompt",
        state="normal",
        assertion="入力した個人system promptが実設定APIと再読込後の欄で一致した",
    )
    artifact_case.attest_control_state(
        control_key="save",
        state="normal",
        assertion="個人設定の保存操作後にsystem promptが実APIと再読込後も保持された",
    )
    artifact_case.screenshot(admin_page, 20, "settings-personal-policy-restored-after-reload")


def test_u3_settings_toc_and_collapse(admin_page, ui_config, artifact_case):
    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    artifact_case.screenshot(admin_page, 10, "settings-u3-section-toc-current-state")
    toc = admin_page.locator('.sec-toc[aria-label="このページの目次"]')
    expect(toc).to_be_visible()
    swagger_item = toc.locator(".sec-toc-item", has_text="Swagger 仕様書")
    expect(swagger_item).to_be_visible()
    card = admin_page.locator('.sec-card[data-section-key="swagger"]')
    expect(card).to_be_attached()
    fragment = swagger_item.get_attribute("href") or swagger_item.get_attribute("data-target") or ""
    assert fragment.startswith("#") and len(fragment) > 1, (
        "U3 settings TOC item has no fragment target, so navigation effect cannot be verified"
    )
    before = admin_page.evaluate(
        """() => {
          const card = document.querySelector('.sec-card[data-section-key="swagger"]');
          const rect = card.getBoundingClientRect();
          return {hash: location.hash, scrollY: window.scrollY, cardTop: rect.top, viewportHeight: innerHeight};
        }"""
    )
    swagger_item.click()
    admin_page.wait_for_function("fragment => location.hash === fragment", fragment, timeout=ui_config.timeout_ms)
    expect(card).to_be_visible()
    expect(swagger_item).to_be_focused()
    active_state = swagger_item.evaluate(
        """el => ({
          ariaCurrent: el.getAttribute('aria-current') || '',
          activeClass: el.classList.contains('active') || el.classList.contains('on')
        })"""
    )
    assert active_state["ariaCurrent"] in {"page", "location", "true"} or active_state["activeClass"], (
        f"clicked U3 TOC item did not expose an active state: {active_state}"
    )
    after = admin_page.evaluate(
        """() => {
          const card = document.querySelector('.sec-card[data-section-key="swagger"]');
          const rect = card.getBoundingClientRect();
          return {hash: location.hash, scrollY: window.scrollY, cardTop: rect.top, viewportHeight: innerHeight};
        }"""
    )
    assert after["hash"] == fragment
    assert abs(after["cardTop"] - before["cardTop"]) > 1 or abs(after["scrollY"] - before["scrollY"]) > 1, (
        f"U3 TOC click did not scroll to its target: before={before} after={after}"
    )
    assert -2 <= after["cardTop"] <= after["viewportHeight"] * 0.5, (
        f"U3 TOC target was not brought into the visible navigation region: {after}"
    )
    toggle = card.locator(".sec-toggle")
    expect(toggle).to_have_attribute("aria-expanded", "true")
    toggle.click()
    expect(toggle).to_have_attribute("aria-expanded", "false")
    expect(card).to_have_class(re.compile(r"\bsec-collapsed\b"))
    artifact_case.screenshot(admin_page, 20, "settings-u3-swagger-section-collapsed")
    admin_page.reload()
    persisted_card = admin_page.locator('.sec-card[data-section-key="swagger"]')
    expect(persisted_card).to_have_class(re.compile(r"\bsec-collapsed\b"))
    expect(persisted_card.locator(".sec-toggle")).to_have_attribute("aria-expanded", "false")
    assert admin_page.evaluate("() => location.hash") == fragment
    persisted_item = admin_page.locator('.sec-toc[aria-label="このページの目次"] .sec-toc-item', has_text="Swagger 仕様書")
    persisted_active = persisted_item.evaluate(
        "el => el.getAttribute('aria-current') || (el.classList.contains('active') || el.classList.contains('on'))"
    )
    assert persisted_active, "U3 TOC active state was lost after reload"
    persisted_card.locator(".sec-toggle").click()
    expect(persisted_card.locator(".sec-toggle")).to_have_attribute("aria-expanded", "true")
    expect(persisted_card).not_to_have_class(re.compile(r"\bsec-collapsed\b"))
    artifact_case.screenshot(admin_page, 30, "settings-u3-swagger-section-reexpanded-after-reload")


@pytest.mark.chat
def test_selected_ai_provider_real_connection_from_settings_ui(admin_page, live_api, ui_config, artifact_case):
    settings = live_api.get_json("/settings", save_as="state/ai-connection-settings.json")
    provider = str(settings.get("agent") or "").strip().lower()
    assert provider in {"codex", "openai", "ollama", "gemini", "bedrock"}, f"selected agent has no real connection-test UI: {provider!r}"
    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    button = admin_page.locator(f'[data-test="{provider}"]')
    result = admin_page.locator(f"#t-{provider}")
    expect(button).to_be_visible()
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/settings/test"),
        timeout=ui_config.timeout_ms,
    ) as probe_info:
        button.click()
    response = probe_info.value
    assert response.status == 200, response.text()
    payload = response.json()
    assert payload.get("provider") == provider, payload
    assert payload.get("ok") is True, f"real {provider} connection test failed: {payload.get('detail') or 'no detail'}"
    expect(result).to_contain_text("接続OK", timeout=ui_config.timeout_ms)
    artifact_case.attest_control_state(
        control_key="@selector:[data-test]",
        state="normal",
        assertion=f"選択中の実{provider} providerが成功応答を返し画面へ接続OKを表示した",
    )
    artifact_case.write_json(
        "state/ai-connection-result.json",
        {"provider": provider, "model": payload.get("model"), "ok": payload.get("ok")},
    )
    artifact_case.screenshot(admin_page, 10, f"settings-{provider}-real-connection-ok")


@pytest.mark.destructive
def test_admin_central_ai_selection_and_personal_key_policy_persist_after_reload(
    admin_page, live_api, ui_config, artifact_case, isolated_stack
):
    before = live_api.get_json("/admin/settings", save_as="state/admin-ai-settings-before.json")
    cloud = before.get("cloud") or {}
    original_provider = str(cloud.get("provider") or "openai")
    original_personal = bool(cloud.get("personal_api_keys_allowed"))
    providers = [str(value) for value in cloud.get("providers") or []]
    assert set(providers) >= {"openai", "gemini", "bedrock"}
    selected = next(value for value in providers if value != original_provider)
    artifact_case.add_cleanup(
        "restore central AI and personal key policy",
        lambda: live_api.put_json(
            "/admin/settings",
            {
                "cloud_provider": original_provider,
                "personal_api_keys_allowed": original_personal,
            },
        ),
    )

    admin_page.goto(ui_config.base_url + "/ui/admin-settings.html")
    provider_radio = admin_page.locator(f'[data-cloud-provider="{selected}"]')
    expect(provider_radio).to_be_visible()
    provider_radio.check()
    personal = admin_page.locator("#personal-keys-allowed")
    if personal.is_checked():
        personal.uncheck()
        personal.check()
    else:
        personal.check()
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/admin/settings"),
        timeout=ui_config.timeout_ms,
    ) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200, save_info.value.text()
    expect(admin_page.locator("#msg")).to_contain_text("保存しました")
    saved = save_info.value.json()
    assert (saved.get("cloud") or {}).get("provider") == selected
    assert (saved.get("cloud") or {}).get("personal_api_keys_allowed") is True
    artifact_case.attest_control_state(
        control_key="@selector:[data-cloud-provider]",
        state="normal",
        assertion="選択した中央cloud providerが実管理設定APIの保存値と一致した",
    )
    artifact_case.attest_control_state(
        control_key="personal-keys-allowed",
        state="normal",
        assertion="個人API key許可の選択値が実管理設定APIへtrueで保存された",
    )
    artifact_case.attest_control_state(
        control_key="save",
        state="normal",
        assertion="中央管理設定の保存操作が200となりproviderと個人key許可を反映した",
    )
    artifact_case.screenshot(admin_page, 10, "settings-central-ai-and-personal-key-policy-saved")

    admin_page.reload()
    expect(admin_page.locator(f'[data-cloud-provider="{selected}"]')).to_be_checked()
    expect(admin_page.locator("#personal-keys-allowed")).to_be_checked()
    persisted = live_api.get_json(
        "/admin/settings",
        save_as="state/admin-ai-settings-after-reload.json",
    )
    assert (persisted.get("cloud") or {}).get("provider") == selected
    assert (persisted.get("cloud") or {}).get("personal_api_keys_allowed") is True
    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    expect(admin_page.locator("#okey-row")).to_be_visible()
    expect(admin_page.locator("#gkey-row")).to_be_visible()
    expect(admin_page.locator("#bkey-row")).to_be_visible()
    artifact_case.screenshot(admin_page, 20, "settings-personal-provider-key-inputs-enabled-after-central-policy")
