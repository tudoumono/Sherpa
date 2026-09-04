from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from ui_automation.runner.artifacts import write_private_text_atomic
from ui_automation.support.chat_flow import prepare_chat
from ui_automation.support.ui import unique_id


pytestmark = [pytest.mark.ui_automation, pytest.mark.chat, pytest.mark.destructive]


def test_chat_panel_font_brain_scope_and_personal_controls(admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack):
    settings_before = live_api.get_json(
        "/settings",
        save_as="state/chat-quick-model-settings-before.json",
    )
    constructs = settings_before.get("constructs_available") or []
    testable = [item for item in constructs if str(item.get("agent") or "") in {"openai", "gemini", "ollama", "bedrock"}]
    assert testable, "no real provider with a UI connection probe is available"
    preferred = next(
        (item for provider in ("openai", "ollama", "gemini", "bedrock") for item in testable if item.get("agent") == provider),
        testable[0],
    )
    provider = str(preferred["agent"])
    construct_id = str(preferred["id"])
    original_agent = settings_before.get("agent")
    original_codex_model_provider = settings_before.get("codex_model_provider") or None
    artifact_case.add_cleanup(
        "restore chat quick-switch provider",
        lambda: live_api.put_json(
            "/settings",
            {
                "agent": original_agent,
                "codex_model_provider": original_codex_model_provider,
            },
        ),
    )

    prepare_chat(admin_page, ui_config, real_world)
    app = admin_page.locator(".app")
    expect(admin_page.locator("#version")).to_have_value(real_world)
    artifact_case.attest_control_state(
        control_key="version",
        state="normal",
        assertion="実World選択controlが登録済みWorld IDを保持してchat入力を利用可能にした",
    )
    knowledge = admin_page.locator("#kbtoggle")
    if knowledge.is_enabled():
        knowledge.click()
        expect(knowledge).to_have_attribute("aria-pressed", "false")
        knowledge.click()
        expect(knowledge).to_have_attribute("aria-pressed", "true")
        artifact_case.attest_control_state(
            control_key="kbtoggle",
            state="normal",
            assertion="資料参照toggleを実操作でOFFからONへ戻し選択状態をaria属性へ反映した",
        )

    conversations_before = live_api.get_json("/conversations")
    example = admin_page.locator("#messages [data-ex]").first
    expect(example).to_be_visible()
    example_text = example.locator(".exq").inner_text()
    example.click()
    expect(admin_page.locator("#input")).to_have_value(example_text)
    expect(admin_page.locator("#input")).to_be_focused()
    selection = admin_page.locator("#input").evaluate("el => ({start: el.selectionStart, end: el.selectionEnd, length: el.value.length})")
    assert selection == {"start": 0, "end": len(example_text), "length": len(example_text)}, selection
    assert live_api.get_json("/conversations") == conversations_before, (
        "clicking a question example submitted a turn instead of only filling the editable input"
    )
    artifact_case.attest_control_state(
        control_key="@selector:[data-ex]",
        state="normal",
        assertion="質問例の実操作が文面だけを入力欄へ設定しconversationを作成しなかった",
    )
    admin_page.locator("#input").fill("")

    admin_page.locator("#sideclose").click()
    expect(app).to_have_class(re.compile(r"\blzero\b"))
    artifact_case.attest_control_state(
        control_key="sideclose",
        state="normal",
        assertion="左panelを閉じる操作がappのlzero表示状態へ即時反映された",
    )
    expect(admin_page.locator("#sideopen")).to_be_visible()
    admin_page.locator("#sideopen").click()
    expect(app).not_to_have_class(re.compile(r"\blzero\b"))
    artifact_case.attest_control_state(
        control_key="sideopen",
        state="normal",
        assertion="左panel再表示操作がlzero状態を解除して主要領域を復元した",
    )

    admin_page.locator("#rightclose").click()
    expect(app).to_have_class(re.compile(r"\brzero\b"))
    artifact_case.attest_control_state(
        control_key="rightclose",
        state="normal",
        assertion="右panelを閉じる操作がappのrzero表示状態へ即時反映された",
    )
    expect(admin_page.locator("#rightopen")).to_be_visible()
    admin_page.locator("#rightopen").click()
    expect(app).not_to_have_class(re.compile(r"\brzero\b"))
    artifact_case.attest_control_state(
        control_key="rightopen",
        state="normal",
        assertion="右panel再表示操作がrzero状態を解除して実行trace領域を復元した",
    )

    admin_page.locator("#fontbtn").click()
    expect(admin_page.locator("#fontmenu")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="fontbtn",
        state="normal",
        assertion="文字サイズbutton操作で選択menuが可視状態になった",
    )
    admin_page.locator('#fontmenu [data-fs="大"]').click()
    assert admin_page.evaluate("getComputedStyle(document.documentElement).getPropertyValue('--chatfont').trim()") == "16.5px"
    artifact_case.attest_control_state(
        control_key="@selector:[data-fs]",
        state="normal",
        assertion="動的文字サイズ項目の選択がchat font CSS値16.5pxへ反映された",
    )

    admin_page.locator("#brainbadge").click()
    expect(admin_page.locator("#brainmenu")).to_be_visible()
    expect(admin_page.locator("#brainmenu [data-exec]")).not_to_have_count(0)
    artifact_case.attest_control_state(
        control_key="brainbadge",
        state="normal",
        assertion="実provider情報を取得して利用可能な実行構成menuを表示した",
    )
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/settings"),
        timeout=ui_config.timeout_ms,
    ) as provider_switch_info:
        admin_page.locator(f'#brainmenu [data-exec="{construct_id}"]').click()
    assert provider_switch_info.value.status == 200, provider_switch_info.value.text()
    switched = live_api.get_json("/settings")
    assert switched.get("agent") == provider
    artifact_case.attest_control_state(
        control_key="@selector:[data-exec]",
        state="normal",
        assertion="動的実行構成の選択を実settings APIへ保存しprovider値が一致した",
    )
    model_input = admin_page.locator("#bm-modelinput")
    model_save = admin_page.locator("#bm-modelsave")
    model_test = admin_page.locator("#bm-modeltest")
    expect(model_input).to_be_visible()
    expect(model_save).to_be_visible()
    expect(model_test).to_be_visible()
    model = str(settings_before.get(f"{provider}_model") or "").strip()
    assert model, f"real {provider} provider has no configured model to test"
    model_input.fill(model)
    expect(model_input).to_have_value(model)
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/settings"),
        timeout=ui_config.timeout_ms,
    ) as model_save_info:
        model_save.click()
    assert model_save_info.value.status == 200, model_save_info.value.text()
    saved = live_api.get_json(
        "/settings",
        save_as="state/chat-quick-model-settings-saved.json",
    )
    assert saved.get("agent") == provider
    assert saved.get(f"{provider}_model") == model
    artifact_case.attest_control_state(
        control_key="bm-modelinput",
        state="normal",
        assertion="quick menuへ入力した実provider modelがsettings API保存値と一致した",
    )
    assert saved.get(f"{provider}_model") == model
    artifact_case.attest_control_state(
        control_key="bm-modelsave",
        state="normal",
        assertion="model保存buttonの実PUT後にprovider別model設定が永続化された",
    )
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/settings/test"),
        timeout=ui_config.timeout_ms,
    ) as model_test_info:
        model_test.click()
    probe = model_test_info.value.json()
    artifact_case.write_json(
        "state/chat-quick-model-real-provider-probe.json",
        {
            "provider": probe.get("provider"),
            "model": probe.get("model"),
            "ok": probe.get("ok"),
            "detail": probe.get("detail"),
        },
    )
    assert model_test_info.value.status == 200 and probe.get("ok") is True, probe
    assert probe.get("provider") == provider and probe.get("model") == model
    expect(admin_page.locator("#bm-tres")).to_contain_text("接続OK")
    artifact_case.attest_control_state(
        control_key="bm-modeltest",
        state="normal",
        assertion="保存modelの実provider probeがproviderとmodel一致かつ接続OKを返した",
    )
    artifact_case.screenshot(admin_page, 5, f"chat-quick-model-{provider}-real-connection-ok")

    invalid_model = unique_id("ui-missing-model")
    model_input.fill(invalid_model)
    expect(model_input).to_have_value(invalid_model)
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/settings/test"),
        timeout=ui_config.timeout_ms,
    ) as invalid_model_info:
        model_test.click()
    invalid_probe = invalid_model_info.value.json()
    assert invalid_probe.get("ok") is not True, invalid_probe
    expect(admin_page.locator("#bm-tres")).not_to_contain_text("接続OK")
    artifact_case.attest_control_state(
        control_key="bm-modelinput",
        state="abnormal",
        assertion="存在しないmodel名を実provider probeへ渡して接続成功にならないことを確認した",
    )
    assert invalid_probe.get("ok") is not True
    artifact_case.attest_control_state(
        control_key="bm-modeltest",
        state="abnormal",
        assertion="存在しないmodelへの実接続probeがok trueを返さず成功表示もしなかった",
    )
    artifact_case.screenshot(admin_page, 7, f"chat-quick-model-{provider}-missing-model-rejected")
    model_input.fill(model)
    admin_page.locator("#messages").click(position={"x": 5, "y": 5})
    expect(admin_page.locator("#brainmenu")).to_be_hidden()

    expect(admin_page.locator("#scopebtn")).to_be_visible()
    admin_page.locator("#scopebtn").click()
    expect(admin_page.locator("#scopepanel")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="scopebtn",
        state="normal",
        assertion="検索scope button操作で実Worldのscope選択panelを表示した",
    )
    scope_rows = admin_page.locator("#scopepanel [data-scope]")
    expect(scope_rows).not_to_have_count(0)
    selected_scope = scope_rows.nth(1) if scope_rows.count() > 1 else scope_rows.first
    selected_scope.click()
    expect(selected_scope).to_have_class(re.compile(r"\bon\b"))
    artifact_case.attest_control_state(
        control_key="@selector:[data-scope]",
        state="normal",
        assertion="動的scope項目の実操作が選択行のon状態とscope labelへ反映された",
    )
    if scope_rows.count() > 1:
        expect(admin_page.locator("#scopelabel")).not_to_have_text("全体")
    admin_page.locator("#messages").click(position={"x": 5, "y": 5})
    expect(admin_page.locator("#scopepanel")).to_be_hidden()

    personal = admin_page.locator("#personaltoggle")
    before_personal = personal.get_attribute("aria-pressed") == "true"
    personal.click()
    expect(personal).to_have_attribute("aria-pressed", "false" if before_personal else "true")
    artifact_case.attest_control_state(
        control_key="personaltoggle",
        state="normal",
        assertion="個人領域toggle操作がaria pressed状態を反転して画面へ反映した",
    )
    personal.click()
    expect(personal).to_have_attribute("aria-pressed", "true" if before_personal else "false")
    artifact_case.screenshot(admin_page, 10, "chat-panels-font-brain-scope-and-personal-controls")

    admin_page.locator("#brainbadge").click()
    expect(admin_page.locator("#brainmenu")).to_be_visible()
    config_link = admin_page.locator("#brainmenu [data-cfg]")
    expect(config_link).to_be_visible()
    config_authorization = artifact_case.arm_control_authorization(
        admin_page,
        control_key="@selector:[data-cfg]",
    )
    assert config_authorization["status"] == 200 and config_authorization["role"] == "admin"
    config_link.click()
    admin_page.wait_for_url("**/ui/settings.html", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#sysprompt")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="@selector:[data-cfg]",
        state="normal",
        assertion="quick menuの設定導線が実settings画面へ遷移しsystem prompt欄を表示した",
    )
    artifact_case.screenshot(admin_page, 20, "chat-brain-detail-settings-navigation-complete")


def test_chat_upload_button_and_file_input_store_real_workspace_file(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    admin_page.goto(ui_config.base_url + "/ui/chat.html")
    expect(admin_page.locator("#input")).to_be_visible()
    source = artifact_case.case_dir / "state" / f"{unique_id('chat-upload')}.txt"
    write_private_text_atomic(source, "SHERPA-CHAT-UPLOAD-CONTROL-731 real workspace evidence\n")
    file_input = admin_page.locator("#chat-file-input")
    expect(file_input).to_be_attached()
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/workspace/files"),
        timeout=ui_config.timeout_ms,
    ) as upload_info:
        with admin_page.expect_file_chooser() as chooser_info:
            admin_page.locator("#chat-upload-btn").click()
        chooser_info.value.set_files(str(source))
    assert upload_info.value.status == 200, upload_info.value.text()
    expect(admin_page.locator("#chat-upload-status")).to_contain_text(source.name)

    listing = live_api.get_json("/workspace/files", save_as="state/chat-upload-workspace-list.json")
    uploaded = next(
        (item for item in listing.get("files") or [] if item.get("rel_path") == source.name),
        None,
    )
    assert uploaded, "chat upload did not create a real workspace record"
    artifact_case.attest_control_state(
        control_key="chat-upload-btn",
        state="normal",
        assertion="upload button操作で実file chooserを開きworkspace recordを作成した",
    )
    assert uploaded.get("rel_path") == source.name
    artifact_case.attest_control_state(
        control_key="chat-file-input",
        state="normal",
        assertion="file inputへ指定した実file名がworkspace APIの保存recordと一致した",
    )
    file_id = int(uploaded["id"])
    artifact_case.add_cleanup(
        f"delete chat-uploaded workspace file {file_id}",
        lambda: live_api.request("DELETE", f"/workspace/files/{file_id}", expected={200, 404}),
    )
    artifact_case.screenshot(admin_page, 10, "chat-file-uploaded-to-real-workspace")
