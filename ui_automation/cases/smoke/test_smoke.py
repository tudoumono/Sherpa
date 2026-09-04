from __future__ import annotations

import json
import os

import pytest
from PIL import Image
from playwright.sync_api import expect

from ui_automation.support.artifacts import redact, safe_url
from ui_automation.support.ui import runtime_password


pytestmark = [pytest.mark.ui_automation, pytest.mark.smoke]


def test_live_stack_health_and_ui_shell(admin_page, live_api, ui_config, artifact_case):
    userinfo_url = "".join(("https://", "local-user", ":", "local-pass", "@example.invalid/path?token=value#fragment"))
    sanitized_url = safe_url(userinfo_url)
    assert sanitized_url == "https://example.invalid/path?<redacted>"
    assert "local-user" not in sanitized_url and "local-pass" not in sanitized_url
    redaction_probe = redact(
        {
            "input_tokens": 17,
            "output_tokens": 9,
            "token": "runtime-secret-token",
            "access_token": "runtime-secret-access-token",
        }
    )
    assert redaction_probe == {
        "input_tokens": 17,
        "output_tokens": 9,
        "token": "<redacted>",
        "access_token": "<redacted>",
    }
    screenshot_canary = os.environ.get("SHERPA_UI_SECRET_CANARY") or runtime_password()
    artifact_case.register_secret(screenshot_canary)
    unknown_pattern_canary = "Bearer " + "uiAutomationPatternCanaryA1b2C3d4E5"
    admin_page.evaluate(
        """values => {
          const known = document.createElement('div');
          known.id = 'ui-automation-known-secret-mask-canary';
          known.textContent = values.known;
          document.body.appendChild(known);

          const unknown = document.createElement('input');
          unknown.id = 'ui-automation-pattern-mask-canary';
          unknown.value = values.pattern;
          unknown.title = values.pattern;
          document.body.appendChild(unknown);

          const canvas = document.createElement('canvas');
          canvas.id = 'ui-automation-canvas-secret-canary';
          canvas.width = 360;
          canvas.height = 60;
          canvas.style.cssText = 'display:block;width:360px;height:60px';
          canvas.getContext('2d').fillText(values.known, 10, 30);
          document.body.appendChild(canvas);

          const frame = document.createElement('iframe');
          frame.id = 'ui-automation-frame-secret-canary';
          frame.style.cssText = 'display:block;width:320px;height:80px;border:0';
          frame.srcdoc = `<p>${values.pattern}</p>`;
          document.body.appendChild(frame);

          const image = document.createElement('img');
          image.id = 'ui-automation-image-surface-canary';
          image.width = 24;
          image.height = 24;
          image.style.cssText = 'display:block;width:24px;height:24px';
          image.src = 'data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///ywAAAAAAQABAAACAUwAOw==';
          document.body.appendChild(image);

          const shadowHost = document.createElement('div');
          shadowHost.id = 'ui-automation-shadow-secret-canary';
          shadowHost.attachShadow({mode: 'open'}).innerHTML = `<span style="opacity:.42 !important">${values.known}</span>`;
          document.body.appendChild(shadowHost);

          const nestedShadowHost = document.createElement('div');
          nestedShadowHost.id = 'ui-automation-nested-shadow-secret-canary';
          const outerRoot = nestedShadowHost.attachShadow({mode: 'open'});
          const innerHost = document.createElement('div');
          outerRoot.appendChild(innerHost);
          innerHost.attachShadow({mode: 'open'}).innerHTML = `
            <style>span { display:block; width:96px; height:28px; background:rgb(1,254,137); }</style>
            <span id="pixel-canary">${values.known}</span>`;
          document.body.appendChild(nestedShadowHost);

          const closedShadowHost = document.createElement('div');
          closedShadowHost.id = 'ui-automation-closed-shadow-secret-canary';
          closedShadowHost.style.cssText = 'display:block;width:160px;height:24px';
          closedShadowHost.attachShadow({mode: 'closed'}).innerHTML = `<span>${values.known}</span>`;
          document.body.appendChild(closedShadowHost);
        }""",
        {"known": screenshot_canary, "pattern": unknown_pattern_canary},
    )
    expect(admin_page.locator("#ui-automation-canvas-secret-canary")).to_be_visible()
    expect(admin_page.locator("#ui-automation-frame-secret-canary")).to_be_visible()
    expect(admin_page.locator("#ui-automation-image-surface-canary")).to_be_visible()
    expect(admin_page.locator("#ui-automation-nested-shadow-secret-canary")).to_be_visible()
    expect(admin_page.locator("#ui-automation-closed-shadow-secret-canary")).to_be_visible()
    expect(admin_page.locator("#ui-automation-closed-shadow-secret-canary")).to_have_attribute("data-sherpa-ui-closed-shadow-host", "1")
    pixel_canary_box = admin_page.evaluate(
        """() => {
          const outer = document.querySelector('#ui-automation-nested-shadow-secret-canary').shadowRoot;
          const inner = outer.querySelector('div').shadowRoot;
          const rect = inner.querySelector('#pixel-canary').getBoundingClientRect();
          return {x: rect.x + scrollX, y: rect.y + scrollY, width: rect.width, height: rect.height};
        }"""
    )
    security_screenshot = artifact_case.screenshot(admin_page, 5, "security-known-and-unknown-pattern-secrets-masked")
    mask_event = artifact_case.screenshot_mask_events[-1]
    attestation = artifact_case.screenshot_attestations[-1]
    assert mask_event["visible_secret_groups_masked"] >= 1
    assert mask_event["secret_pattern_groups_masked"] >= 1
    assert attestation["known_match_count"] >= 1
    assert attestation["pattern_match_count"] >= 1
    assert attestation["input_value_match_count"] >= 1
    assert attestation["attribute_match_count"] >= 1
    assert attestation["opaque_pixel_surface_count"] >= 2
    assert attestation["shadow_root_count"] >= 1
    assert attestation["closed_shadow_host_count"] >= 1
    assert attestation["open_shadow_masked_element_count"] >= 2
    assert attestation["masked_element_count"] >= attestation["detected_element_count"]
    assert attestation["unmasked_visible_element_count"] == 0
    assert attestation["inline_mask_failure_count"] == 0
    serialized_attestation = json.dumps(attestation)
    known_value_absent = screenshot_canary not in serialized_attestation
    pattern_value_absent = unknown_pattern_canary not in serialized_attestation
    assert known_value_absent is True, "known canary value entered security evidence"
    assert pattern_value_absent is True, "pattern canary value entered security evidence"
    restored_styles = admin_page.evaluate(
        """() => {
          const direct = document.querySelector('#ui-automation-shadow-secret-canary').shadowRoot.querySelector('span');
          const outer = document.querySelector('#ui-automation-nested-shadow-secret-canary').shadowRoot;
          const nested = outer.querySelector('div').shadowRoot.querySelector('#pixel-canary');
          const light = document.querySelector('#ui-automation-known-secret-mask-canary');
          return {
            directOpacity: direct.style.getPropertyValue('opacity'),
            directPriority: direct.style.getPropertyPriority('opacity'),
            nestedHasStyle: nested.hasAttribute('style'),
            lightHasStyle: light.hasAttribute('style'),
          };
        }"""
    )
    assert restored_styles == {
        "directOpacity": "0.42",
        "directPriority": "important",
        "nestedHasStyle": False,
        "lightHasStyle": False,
    }
    marker_rgb = (1, 254, 137)
    with Image.open(security_screenshot) as screenshot_image:
        rgb_image = screenshot_image.convert("RGB")
        left = int(pixel_canary_box["x"])
        top = int(pixel_canary_box["y"])
        right = left + int(pixel_canary_box["width"])
        bottom = top + int(pixel_canary_box["height"])
        assert 0 <= left < right <= rgb_image.width and 0 <= top < bottom <= rgb_image.height
        pixel_bytes = rgb_image.crop((left, top, right, bottom)).tobytes()
        marker_bytes = bytes(marker_rgb)
        assert not any(pixel_bytes[offset : offset + 3] == marker_bytes for offset in range(0, len(pixel_bytes), 3)), (
            "open ShadowRoot pixel canary remained visible in the stored screenshot"
        )
    admin_page.locator(
        "#ui-automation-known-secret-mask-canary, #ui-automation-pattern-mask-canary, "
        "#ui-automation-canvas-secret-canary, #ui-automation-frame-secret-canary, "
        "#ui-automation-image-surface-canary, #ui-automation-shadow-secret-canary"
        ", #ui-automation-nested-shadow-secret-canary, #ui-automation-closed-shadow-secret-canary"
    ).evaluate_all("elements => elements.forEach(element => element.remove())")

    health = live_api.get_json("/healthz", save_as="state/healthz.json")
    summary = live_api.get_json("/health/summary", save_as="state/health-summary.json")
    assert health == {"ok": True}
    assert summary.get("status") == "ok", f"required stores are not healthy: {summary}"

    admin_page.goto(admin_page.url.split("/ui/", 1)[0] + "/ui/home.html")
    expect(admin_page.locator("sherpa-topbar")).to_be_visible()
    expect(admin_page.locator("#sherpa-nav")).to_contain_text("チャット")
    expect(admin_page.locator("#healthdot")).to_have_class("healthdot ok")
    artifact_case.screenshot(admin_page, 10, "smoke-home-real-stack-ready")
    expect(admin_page.locator("#healthdot")).to_have_attribute("href", "status.html")
    health_authorization = artifact_case.arm_control_authorization(admin_page, control_key="healthdot")
    assert health_authorization["status"] == 200 and health_authorization["role"] == "admin"
    admin_page.locator("#healthdot").click()
    admin_page.wait_for_url("**/ui/status.html", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#status-pill")).to_have_text("正常")
    artifact_case.attest_control_state(
        control_key="healthdot",
        state="normal",
        assertion="正常な実service集約indicatorからstatus画面へ遷移し正常pillを表示した",
    )
    artifact_case.screenshot(admin_page, 20, "smoke-health-indicator-opened-real-status")
