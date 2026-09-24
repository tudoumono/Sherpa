from __future__ import annotations

import pytest
from playwright.sync_api import expect

from ui_automation.support.ui import unique_id


pytestmark = [pytest.mark.ui_automation, pytest.mark.settings, pytest.mark.destructive]


_PERSONAL_SETTING_KEYS = (
    "agent",
    "codex_model_provider",
    "codex_reasoning",
    "codex_model",
    "extract_provider",
    "sub_profile",
    "search_helper",
    "search_helper_model",
    "sub_planner",
    "intent_model",
    "graph_provider",
    "intent_provider",
    "embed_provider",
    "openai_model",
    "gemini_model",
    "ollama_url",
    "ollama_model",
    "bedrock_model",
    "system_prompt",
    "codex_web_search",
)


def _restore_payload(settings: dict) -> dict:
    return {key: settings.get(key) for key in _PERSONAL_SETTING_KEYS if key in settings}


def _next_enabled_value(select) -> str:
    values = select.evaluate(
        """element => Array.from(element.options)
          .filter(option => !option.disabled && !option.hidden)
          .map(option => option.value)"""
    )
    assert values, f"select has no enabled option: {select}"
    current = select.input_value()
    target = next((value for value in values if value != current), current)
    select.select_option(target)
    return str(target)


def test_personal_agent_helper_and_function_provider_controls_persist(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/settings", save_as="state/personal-provider-controls-before.json")
    artifact_case.add_cleanup(
        "restore personal provider controls",
        lambda: live_api.put_json("/settings", _restore_payload(before)),
    )

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    expect(admin_page.locator("#agent option")).not_to_have_count(0)
    construct_id = _next_enabled_value(admin_page.locator("#agent"))
    search_helper = admin_page.locator("#search_helper")
    search_helper.select_option("ollama")
    expect(admin_page.locator("#search_helper_model")).to_be_visible()
    helper_model = str(before.get("ollama_model") or "qwen2.5")
    admin_page.locator("#search_helper_model").fill(helper_model)

    expect(admin_page.locator("#subagent-card")).to_be_visible()
    sub_profile = admin_page.locator("#sub_profile")
    sub_values = sub_profile.locator("option").evaluate_all("options => options.map(option => option.value).filter(Boolean)")
    assert sub_values, "full stack exposes no real subagent profile choice"
    sub_profile.select_option(sub_values[0])
    planner = admin_page.locator("#sub_planner")
    target_planner = not planner.is_checked()
    planner.set_checked(target_planner)

    selected = {
        "extract_provider": _next_enabled_value(admin_page.locator("#extract_provider")),
        "graph_provider": _next_enabled_value(admin_page.locator("#graph_provider")),
        "intent_provider": _next_enabled_value(admin_page.locator("#intent_provider")),
        "embed_provider": _next_enabled_value(admin_page.locator("#embed_provider")),
    }
    intent_model = f"ui-intent-{unique_id('model')[-8:]}"
    admin_page.locator("#intent_model").fill(intent_model)
    admin_page.once("dialog", lambda dialog: dialog.accept())
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/settings"),
        timeout=ui_config.timeout_ms,
    ) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200, save_info.value.text()
    saved = save_info.value.json()
    assert saved.get("construct_id") == construct_id
    assert saved.get("search_helper") == "ollama"
    assert saved.get("search_helper_model") == helper_model
    assert saved.get("sub_profile") == sub_values[0]
    assert saved.get("sub_planner") == ("auto" if target_planner else "")
    assert saved.get("intent_model") == intent_model
    for key, value in selected.items():
        assert saved.get(key) == value

    admin_page.reload()
    expect(admin_page.locator("#agent")).to_have_value(construct_id)
    expect(admin_page.locator("#search_helper")).to_have_value("ollama")
    expect(admin_page.locator("#search_helper_model")).to_have_value(helper_model)
    expect(admin_page.locator("#sub_profile")).to_have_value(sub_values[0])
    expect(admin_page.locator("#intent_model")).to_have_value(intent_model)
    for control_key, assertion in (
        ("agent", "選択した主agentが実設定APIと再読込後の選択値で一致した"),
        ("search_helper", "選択した検索補助providerが実設定APIと再読込後も保持された"),
        ("search_helper_model", "入力した検索補助modelが実設定APIと再読込後も保持された"),
        ("sub_profile", "選択した実subagent profileが実設定APIと再読込後も保持された"),
        ("sub_planner", "変更したplanner選択が実設定APIのautoまたは無効値と一致した"),
        ("extract_provider", "選択した抽出providerが実設定APIの保存値と一致した"),
        ("graph_provider", "選択したgraph providerが実設定APIの保存値と一致した"),
        ("intent_provider", "選択したintent providerが実設定APIの保存値と一致した"),
        ("embed_provider", "選択したembedding providerが実設定APIの保存値と一致した"),
        ("intent_model", "入力したintent modelが実設定APIと再読込後も保持された"),
    ):
        artifact_case.attest_control_state(control_key=control_key, state="normal", assertion=assertion)
    artifact_case.screenshot(admin_page, 10, "settings-agent-helper-and-function-providers-persisted")


def test_personal_cloud_key_and_bedrock_model_controls_use_real_providers(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    admin_before = live_api.get_json("/admin/settings", save_as="state/personal-cloud-controls-admin-before.json")
    cloud_before = admin_before.get("cloud") or {}
    artifact_case.add_cleanup(
        "restore central cloud selection after personal control probes",
        lambda: live_api.put_json(
            "/admin/settings",
            {
                "cloud_provider": cloud_before.get("provider"),
                "personal_api_keys_allowed": bool(cloud_before.get("personal_api_keys_allowed")),
            },
        ),
    )

    key_fields = {
        # Credential-like prefixes are assembled at runtime so an assertion
        # traceback cannot copy a scanner-matching canary from source text.
        # The completed value is registered before it enters the browser.
        "openai": ("#okey", "#omodel", "".join(("s", "k-", "ui-invalid-"))),
        "gemini": ("#gkey", "#gmodel", "".join(("AI", "za", "-ui-invalid-"))),
        "bedrock": ("#bkey", "#bmodel", "bedrock-ui-invalid-"),
    }
    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    artifact_case.stop_trace(save=False)
    try:
        for provider, (key_selector, model_selector, prefix) in key_fields.items():
            live_api.put_json(
                "/admin/settings",
                {"cloud_provider": provider, "personal_api_keys_allowed": True},
            )
            admin_page.reload()
            key_input = admin_page.locator(key_selector)
            expect(key_input).to_be_visible()
            invalid_key = prefix + unique_id(provider)
            artifact_case.register_secret(invalid_key)
            key_input.fill(invalid_key)
            model_control = admin_page.locator(model_selector)
            expect(model_control).to_be_visible()
            if provider == "bedrock":
                assert model_control.input_value(), "Bedrock model select has no real model"
            else:
                current_model = model_control.input_value()
                assert current_model, f"{provider} model is empty"
                model_control.fill(current_model)
            with admin_page.expect_response(
                lambda response: response.request.method == "POST" and response.url.endswith("/settings/test"),
                timeout=ui_config.timeout_ms,
            ) as invalid_info:
                admin_page.locator(f'[data-test="{provider}"]').click()
            invalid_payload = invalid_info.value.json()
            assert invalid_payload.get("provider") == provider
            assert invalid_payload.get("ok") is False, f"{provider} accepted a deliberately invalid personal credential"
            artifact_case.attest_control_state(
                control_key=key_selector.removeprefix("#"),
                state="abnormal",
                assertion=f"故意に無効な個人{provider}資格情報が実接続成功として扱われなかった",
            )
            key_input.fill("")

        with admin_page.expect_response(
            lambda response: response.request.method == "GET" and response.url.endswith("/settings/bedrock-models"),
            timeout=ui_config.timeout_ms,
        ) as models_info:
            admin_page.locator("#bmodel-fetch").click()
        assert models_info.value.status == 200, models_info.value.text()
        models = models_info.value.json().get("models") or []
        assert models, "real Bedrock account returned no available model"
        expect(admin_page.locator("#bmodel-fetch-res")).to_contain_text("✓")
        artifact_case.attest_control_state(
            control_key="bmodel-fetch",
            state="normal",
            assertion="実Bedrock accountのmodel一覧が非空で取得され画面へ成功表示された",
        )

        verified_model = str(models[0].get("id") or "")
        assert verified_model
        admin_page.locator("#bmodel").select_option(verified_model)
        admin_page.locator("#bmodel-manual").fill(verified_model)
        with admin_page.expect_response(
            lambda response: response.request.method == "POST" and response.url.endswith("/settings/bedrock-models/verify"),
            timeout=ui_config.timeout_ms,
        ) as verify_info:
            admin_page.locator("#bmodel-verify").click()
        verified = verify_info.value.json()
        assert verify_info.value.status == 200 and verified.get("ok") is True, verified
        artifact_case.attest_control_state(
            control_key="bmodel",
            state="normal",
            assertion="実Bedrock一覧から選択したmodelがprovider検証成功の対象になった",
        )
        artifact_case.attest_control_state(
            control_key="bmodel-manual",
            state="normal",
            assertion="手入力した実Bedrock model IDがprovider検証で成功した",
        )
        artifact_case.attest_control_state(
            control_key="bmodel-verify",
            state="normal",
            assertion="Bedrock model検証操作が実providerから200かつ成功を返した",
        )
        admin_page.locator("#bmodel-manual").fill("")
    finally:
        for key_selector, _, _ in key_fields.values():
            if admin_page.locator(key_selector).count():
                admin_page.locator(key_selector).evaluate("element => { element.value = ''; }")
        if admin_page.locator("#bmodel-manual").count():
            admin_page.locator("#bmodel-manual").evaluate("element => { element.value = ''; }")
        artifact_case.start_trace(admin_page.context)
    expect(admin_page.locator("#bmodel")).to_have_value(verified_model)
    artifact_case.screenshot(admin_page, 10, "settings-real-bedrock-model-list-and-verification-complete")


def test_personal_ollama_and_codex_controls_persist_and_reload(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/settings", save_as="state/personal-local-codex-controls-before.json")
    artifact_case.add_cleanup(
        "restore Ollama and Codex personal settings",
        lambda: live_api.put_json("/settings", _restore_payload(before)),
    )
    ollama_url = str(before.get("ollama_url") or "http://127.0.0.1:11434")
    ollama_model = str(before.get("ollama_model") or "qwen2.5")
    codex_model = str(before.get("codex_model") or "gpt-5.5")

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    admin_page.locator("#ourl").fill(ollama_url)
    admin_page.locator("#omodel2").fill(ollama_model)
    admin_page.locator("#cmodel").fill(codex_model)
    current_reason = admin_page.locator("#reason").input_value()
    reasons = ["low", "medium", "high", "xhigh"]
    target_reason = next(value for value in reasons if value != current_reason)
    admin_page.locator("#reason").select_option(target_reason)
    expect(admin_page.locator("#cwebsearch-row")).to_be_visible()
    web_search = admin_page.locator("#cwebsearch")
    target_web = not web_search.is_checked()
    web_search.set_checked(target_web)

    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/settings"),
        timeout=ui_config.timeout_ms,
    ) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200, save_info.value.text()
    saved = save_info.value.json()
    assert saved.get("ollama_url") == ollama_url
    assert saved.get("ollama_model") == ollama_model
    assert saved.get("codex_model") == codex_model
    assert saved.get("codex_reasoning") == target_reason
    assert saved.get("codex_web_search") is target_web

    admin_page.reload()
    expect(admin_page.locator("#ourl")).to_have_value(ollama_url)
    expect(admin_page.locator("#omodel2")).to_have_value(ollama_model)
    expect(admin_page.locator("#cmodel")).to_have_value(codex_model)
    expect(admin_page.locator("#reason")).to_have_value(target_reason)
    expect(admin_page.locator("#cwebsearch")).to_be_checked(checked=target_web)
    for control_key, assertion in (
        ("ourl", "入力したOllama endpointが実設定APIと再読込後も一致した"),
        ("omodel2", "入力したOllama modelが実設定APIと再読込後も一致した"),
        ("cmodel", "入力したCodex modelが実設定APIと再読込後も一致した"),
        ("reason", "選択したCodex reasoning値が実設定APIと再読込後も一致した"),
        ("cwebsearch", "変更したCodex web検索設定が実設定APIと再読込後も一致した"),
    ):
        artifact_case.attest_control_state(control_key=control_key, state="normal", assertion=assertion)
    artifact_case.screenshot(admin_page, 10, "settings-ollama-and-codex-controls-persisted")


def test_personal_system_prompt_default_control_persists(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    before = live_api.get_json("/settings", save_as="state/personal-system-default-before.json")
    artifact_case.add_cleanup(
        "restore personal system prompt after default control",
        lambda: live_api.put_json("/settings", _restore_payload(before)),
    )

    admin_page.goto(ui_config.base_url + "/ui/settings.html")
    admin_page.locator("#sysprompt").fill(unique_id("temporary-policy"))
    admin_page.locator("#sysdefault").click()
    default_prompt = admin_page.locator("#sysprompt").input_value()
    assert "憶測で回答しない" in default_prompt
    with admin_page.expect_response(
        lambda response: response.request.method == "PUT" and response.url.endswith("/settings"),
        timeout=ui_config.timeout_ms,
    ) as save_info:
        admin_page.locator("#save").click()
    assert save_info.value.status == 200, save_info.value.text()
    assert save_info.value.json().get("system_prompt") == default_prompt
    admin_page.reload()
    expect(admin_page.locator("#sysprompt")).to_have_value(default_prompt)
    artifact_case.attest_control_state(
        control_key="sysdefault",
        state="normal",
        assertion="既定化操作で生成されたsystem promptが保存後の再読込でも保持された",
    )
    artifact_case.attest_control_state(
        control_key="save",
        state="normal",
        assertion="既定system promptの保存操作が200となり再読込後も同じ値を表示した",
    )
    artifact_case.screenshot(admin_page, 10, "settings-system-prompt-default-persisted")
