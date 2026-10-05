"""個人設定画面（settings.html）の e2e。

- 実行構成（agent）は選び直して値が変わったときだけ送る（無関係な保存で書き換えない）。
- 保存済みの鍵は画面に戻さない・保存と接続テストは未保存の入力キーも扱う。
- 個人キー・外部連携カード・接続先の注記は管理者設定に従って出し分ける。
"""
from __future__ import annotations

import json

import pytest

import mock_api
from mock_api import install_api_mocks


def expect(*args):
    from playwright.sync_api import expect as _expect
    return _expect(*args)


def _settings(page, web_base_url, **kw):
    records = install_api_mocks(page, **kw)
    page.goto(f"{web_base_url}/settings.html")
    return records


def _save_ok(page):
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")


def _put_without_agent(records):
    put = records["settings_put"][-1]
    assert "agent" not in put
    assert "codex_model_provider" not in put
    return put


def test_save_and_connection_test_do_not_echo_saved_keys(page, web_base_url):
    records = _settings(page, web_base_url)

    expect(page.locator("#agent")).to_have_value("simple")   # 3構成: 既定の構成id
    expect(page.locator("#okey")).to_have_value("")
    expect(page.locator("#okey")).to_have_attribute("placeholder", "設定済み（変更する時だけ入力）")

    # 実行構成は選び直した時だけ送る＝既定の simple から codex_ollama へ実際に変える。
    page.locator("#agent").select_option("codex_ollama")
    page.locator("#okey").fill("sk-test")
    _save_ok(page)
    put = records["settings_put"][-1]
    assert put["agent"] == "codex"
    assert put["codex_model_provider"] == "ollama"
    assert put["openai_api_key"] == "sk-test"

    expect(page.locator("#okey")).to_have_value("")
    page.locator("[data-test='openai']").click()
    expect(page.locator("#t-openai")).to_contain_text("接続OK")
    assert records["settings_test"][-1]["provider"] == "openai"


@pytest.mark.parametrize("agent,provider", [("codex_ollama", "ollama"), ("codex_openai", None)])
def test_agent_change_sends_agent_and_codex_model_provider_together(page, web_base_url, agent, provider):
    """Codex のモデル・思考の深さは管理者の既定に一本化されており、このページは実行構成の選択だけを送る。"""
    records = _settings(page, web_base_url)

    page.locator("#agent").select_option(agent)
    _save_ok(page)
    put = records["settings_put"][-1]
    assert put["agent"] == "codex"
    if provider:
        assert put["codex_model_provider"] == provider


@pytest.mark.parametrize("case", ["untouched", "reselect-same", "out-of-list"])
def test_agent_unchanged_save_omits_agent_fields(page, web_base_url, case):
    """触らない・同じ値へ選び直す・一覧外の保存値のまま、のいずれでも agent／codex_model_provider は
    送らない（無関係な保存で実行構成が書き換わる事故、黙って別の頭脳へ移行する事故を防ぐ）。"""
    kw = {}
    if case == "out-of-list":
        kw["settings"] = {**mock_api.SETTINGS_RESP, "agent": "heuristic", "construct_id": "heuristic",
                          "codex_model_provider": ""}
    records = _settings(page, web_base_url, **kw)

    if case == "untouched":
        page.locator("#okey").fill("sk-untouched")
    elif case == "reselect-same":
        expect(page.locator("#agent")).to_have_value("simple")
        page.locator("#agent").select_option("simple")
    else:
        expect(page.locator("#agent")).to_have_value("heuristic")
        expect(page.locator("#agent option[value='heuristic']")).to_contain_text("現在の設定（一覧外）")
        expect(page.locator("#agent-hint")).to_contain_text("選べない設定")
    _save_ok(page)

    put = _put_without_agent(records)
    if case == "untouched":
        assert put["openai_api_key"] == "sk-untouched"
        assert "system_prompt" not in put and "search_helper" not in put


def _fail_first_put(page, mode):
    count = {"n": 0}

    def handler(route):
        if route.request.method != "PUT":
            route.fallback()
            return
        count["n"] += 1
        if count["n"] > 1:
            route.fallback()
        elif mode == "abort":
            route.abort()
        else:
            status, detail = (422, "invalid") if mode == "4xx" else (500, "boom")
            route.fulfill(status=status, content_type="application/json", body=json.dumps({"detail": detail}))
    page.route("**/settings", handler)


def test_agent_put_4xx_keeps_old_baseline_and_omits_fields_after_revert(page, web_base_url):
    """PUT が 4xx（未適用）で失敗しても基準値は動かない。元の値へ選び直して保存すると両方省略される。"""
    records = install_api_mocks(page)
    _fail_first_put(page, "4xx")
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("invalid")
    assert records["settings_put"] == []   # モック側の記録はフォールバック経路でしか積まれない

    page.locator("#agent").select_option("simple")
    _save_ok(page)
    _put_without_agent(records)


@pytest.mark.parametrize("mode", ["5xx", "abort"])
def test_agent_put_5xx_or_network_error_resends_on_retry_then_omits_after_success(page, web_base_url, mode):
    """応答からコミットされたか分からない失敗（5xx・通信例外）のあとは、選択を変えずに保存し直しても
    agent・codex_model_provider の両方を送り、その保存が成功すれば以後は再び省略する。"""
    records = install_api_mocks(page)
    _fail_first_put(page, mode)
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()
    expect(page.locator("#msg .danger")).to_be_visible()

    _save_ok(page)
    put = records["settings_put"][-1]
    assert put["agent"] == "codex"
    assert put["codex_model_provider"] == "ollama"

    _save_ok(page)
    _put_without_agent(records)


def test_agent_survives_reload_failure_and_resends_on_revert(page, web_base_url):
    """PUT 成功→直後の自動 load()（GET）失敗のあとに元の値へ選び直して保存すると agent は再送される
    （基準値は PUT 成功の時点で送信済みの値へ進んでおり GET の成否に依存しない）。"""
    records = install_api_mocks(page)
    get_count = {"n": 0}

    def fail_second_settings_get(route):
        if route.request.method != "GET":
            route.fallback()
            return
        get_count["n"] += 1
        if get_count["n"] == 2:   # 1回目=初期 load()・2回目=保存後の自動 load() だけ失敗させる
            route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
            return
        route.fallback()

    page.route("**/settings", fail_second_settings_get)
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("再読込に失敗しました")
    assert records["settings_put"][-1]["agent"] == "codex"

    page.locator("#agent").select_option("simple")
    _save_ok(page)
    assert records["settings_put"][-1]["agent"] == "simple"


def test_save_bar_stays_visible_when_scrolled_and_ctrl_s_saves(page, web_base_url):
    records = _settings(page, web_base_url)

    save = page.locator("#save")
    page.mouse.wheel(0, 100000)   # ページ最下部までスクロール
    expect(save).to_be_in_viewport()   # 追加スクロールなしで見えている＝fixed が効いている
    save.click()
    expect(page.locator("#msg")).to_contain_text("保存しました")

    page.locator("#okey").fill("sk-ctrl-s")
    page.keyboard.press("Control+s")
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["openai_api_key"] == "sk-ctrl-s"


def test_codex_connection_test_sends_unsaved_openai_key(page, web_base_url):
    """Codex の接続テストは保存前（入力中）の「OpenAI」欄のキーも使い、モデルは管理者のカタログ既定に従う。"""
    records = _settings(page, web_base_url)

    page.locator("#agent").select_option("codex_openai")
    page.locator("#okey").fill("sk-unsaved-azure-key")
    page.locator("[data-test='codex']").click()

    expect(page.locator("#t-codex")).to_contain_text("接続OK")
    assert records["settings_test"][-1] == {"provider": "codex", "openai_api_key": "sk-unsaved-azure-key"}


def test_save_waits_for_reload_before_showing_success(page, web_base_url):
    """保存後の2回目の GET /settings（save() 内の自動 load()）を保留し、解放前は成功メッセージが
    出ておらず保存ボタンも無効のままで、解放後に出て再び有効になる（fire-and-forget の load() では
    メッセージの出現だけでは見分けられない）。"""
    records = install_api_mocks(page)
    held = {}
    get_count = {"n": 0}

    def hold_second_settings_get(route):
        if route.request.method != "GET":
            route.fallback()
            return
        get_count["n"] += 1
        if get_count["n"] == 1:
            route.fallback()   # 初回 GET は通常どおり応答させる
            return
        held["route"] = route

    page.route("**/settings", hold_second_settings_get)   # goto より前に登録して初回から経由させる
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#okey").fill("sk-held-get")
    page.locator("#save").click()

    expect(page.locator("#save")).to_be_disabled()
    assert len(records["settings_put"]) == 1, "PUT 自体は保留の影響を受けず完了しているはず"
    expect(page.locator("#msg")).not_to_contain_text("保存しました")

    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps(mock_api.SETTINGS_RESP, ensure_ascii=False))
    expect(page.locator("#msg")).to_contain_text("保存しました")
    expect(page.locator("#save")).not_to_be_disabled()


@pytest.mark.parametrize("allowed", [False, True])
def test_personal_keys_allowed_toggles_key_inputs_and_note(page, web_base_url, allowed):
    """個人キー不許可（管理者設定）ではキー欄が隠れ「キーは管理者が設定します」の注記が出る。許可なら欄が見える。"""
    _settings(page, web_base_url, settings={**mock_api.SETTINGS_RESP, "personal_api_keys_allowed": allowed})

    if allowed:
        expect(page.locator("#okey-row")).to_be_visible()
        expect(page.locator("#okey-disabled-note")).to_be_hidden()
    else:
        expect(page.locator("#okey-row")).to_be_hidden()
        expect(page.locator("#okey-disabled-note")).to_be_visible()


def test_ext_keys_card_hidden_when_disabled(page, web_base_url):
    _settings(page, web_base_url, settings=mock_api.SETTINGS_RESP)   # 既定は user_api_keys_allowed=False

    expect(page.locator("#ext-keys-card")).to_be_hidden()


def test_ext_keys_card_shown_when_allowed(page, web_base_url):
    _settings(page, web_base_url, settings={**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True})

    expect(page.locator("#ext-keys-card")).to_be_visible()


def test_openai_endpoint_note_hidden_when_connected_to_openai(page, web_base_url):
    _settings(page, web_base_url, settings=mock_api.SETTINGS_RESP)

    expect(page.locator("#openai-endpoint-note")).to_be_hidden()


def test_openai_endpoint_note_shows_azure_host_read_only(page, web_base_url):
    """接続先の種類とホスト名だけを読み取り専用で示す（パス・キーは出さない）。"""
    _settings(page, web_base_url, settings={**mock_api.SETTINGS_RESP, "openai_endpoint_kind": "azure",
                                            "openai_base_url_host": "myres.openai.azure.com"})

    note = page.locator("#openai-endpoint-note")
    expect(note).to_be_visible()
    expect(note).to_contain_text("Azure OpenAI")
    expect(note).to_contain_text("myres.openai.azure.com")
    expect(note).to_contain_text("デプロイ名")


def test_recompute_construct_id_rejects_falsy_non_string_codex_model_provider():
    """mock の `_recompute_construct_id` は codex_model_provider が文字列以外の falsy（False/0/{}/[]）
    のとき「未設定」に丸めず、実サーバと同じ "codex_invalid" を返す。None・空文字は既定の openai。"""
    for bad in (False, 0, {}, []):
        resp = {"agent": "codex", "codex_model_provider": bad, "constructs_available": []}
        assert mock_api._recompute_construct_id(resp) == "codex_invalid", bad
    for ok in (None, ""):
        resp = {"agent": "codex", "codex_model_provider": ok, "constructs_available": []}
        assert mock_api._recompute_construct_id(resp) == "codex_openai", ok


def test_settings_put_rejects_falsy_non_string_codex_model_provider(page, web_base_url):
    """PUT /settings の codex_model_provider は False/0/{}/[] を truthiness だけで見ると allowlist を
    すり抜ける——実サーバは型検証で 422 にする（mock が同じ結果になることを固定する）。"""
    _settings(page, web_base_url)
    for bad in (False, 0, {}, []):
        status = page.evaluate(
            """(body) => fetch('/settings', {method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)}).then((r) => r.status)""",
            {"codex_model_provider": bad},
        )
        assert status == 422, f"{bad!r} が拒否されなかった"
