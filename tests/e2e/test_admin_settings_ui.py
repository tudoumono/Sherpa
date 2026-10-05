"""システム管理画面（admin-settings.html）の e2e。

- 保存は触った項目だけを PUT /admin/settings に載せる（未操作・元に戻した項目は送らない）。
- タブ単位の「既定に戻す」は対象キーだけを null（または明示 false）で送り、他タブの未保存編集を消さない。
- 書込専用のキー欄は値を画面に出さず、削除は専用操作だけで行う。
- 非 admin はアクセス拒否・ナビに「システム管理」が出ない。
- 外部連携キー発行モーダル（管理画面・個人設定共通）は応答待ちの間閉じられず、曖昧な失敗は回復導線へ回す。
"""
from __future__ import annotations

import copy
import json
import re

import pytest

import mock_api
from mock_api import SYSTEM_SETTINGS_VIEW, USER_MEMBER, install_api_mocks
from _admin_page import goto_admin_settings

EMBED_SEL = "select.mc-default[data-provider='openai'][data-usage='embed']"
OPENAI_CHAT_SEL = "select.mc-default[data-provider='openai'][data-usage='chat']"
OLLAMA_CHAT_SEL = "select.mc-default[data-provider='ollama'][data-usage='chat']"
ARM_PDF = "#arms-list input[data-arm='pdf_text']"
AZURE_URL = "https://myres.openai.azure.com/openai/v1"
CHANGED = re.compile(r"\bcfg-changed\b")


def expect(*args):
    from playwright.sync_api import expect as _expect
    return _expect(*args)


def open_tab(page, key):
    """既定表示（"provider"）以外の要素は、操作前にそのタブを開く（可視でないと操作できない）。"""
    page.locator(f'.tab-btn[data-tab="{key}"]').click()


def open_advanced(page, tab_panel_id):
    page.locator(f'#{tab_panel_id} details.adv summary').click()


def _view():
    return json.loads(json.dumps(SYSTEM_SETTINGS_VIEW))


def _azure_view():
    view = _view()
    view["openai_endpoint"]["configured"] = {
        "kind": "azure", "base_url": AZURE_URL, "auth_header": "bearer", "api_version": None}
    view["openai_endpoint"]["effective"] = {
        "kind": "azure", "base_url": AZURE_URL, "auth_header": "bearer", "api_version": ""}
    return view


def _azure_kind_only_view():
    view = _view()
    view["openai_endpoint"]["configured"]["kind"] = "azure"
    view["openai_endpoint"]["effective"]["kind"] = "azure"
    return view


def _admin(page, web_base_url, tab=None, adv=False, url="/admin-settings.html", wait_loaded=True, **kw):
    records = install_api_mocks(page, **kw)
    if url == "/admin-settings.html" and wait_loaded:
        goto_admin_settings(page, web_base_url)
    else:
        page.goto(f"{web_base_url}{url}")
    if tab:
        open_tab(page, tab)
        if adv:
            open_advanced(page, f"tabpanel-{tab}")
    return records


def _save_ok(page):
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")


def _last_put(records):
    return records["admin_settings_put"][-1]


def _dialogs(page, accept):
    messages = []
    page.on("dialog", lambda d: (messages.append(d.message), d.accept() if accept else d.dismiss()))
    return messages


def _hold_first_put(page):
    held = {}

    def hold(route):
        if route.request.method != "PUT" or "route" in held:
            route.fallback()
            return
        held["route"] = route
    page.route("**/admin/settings", hold)
    return held


# ===== 画面の入口・タブ・権限 =====

def test_chat_examples_hint_matches_empty_save_is_hidden_contract(page, web_base_url):
    """空欄のまま保存すると質問例は「非表示」になる実契約と案内文言が一致する。未設定の初回描画は
    基準値と一致し、プロバイダタブに未保存の丸印が付かない。"""
    _admin(page, web_base_url)

    expect(page.locator("#chat-examples-card")).to_contain_text(
        "空欄のまま保存すると質問例は表示されません。組み込みの既定（4例）に戻すには、"
        "下の「未設定に戻す」を使ってください。")
    expect(page.locator("#tab-dot-provider")).to_be_hidden()


def test_tab_selection_persists_across_reload_via_url_hash(page, web_base_url):
    _admin(page, web_base_url, tab="ingest")
    expect(page).to_have_url(re.compile(re.escape("#ingest") + r"$"))

    page.reload()
    expect(page.locator('.tab-btn[data-tab="ingest"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#tabpanel-ingest")).to_be_visible()
    expect(page.locator("#tabpanel-provider")).to_be_hidden()

    page.goto(f"{web_base_url}/admin-settings.html#no-such-tab")
    expect(page.locator('.tab-btn[data-tab="provider"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#tabpanel-provider")).to_be_visible()


def test_denied_for_non_admin(page, web_base_url):
    _admin(page, web_base_url, user=USER_MEMBER, wait_loaded=False)   # 管理者以外は設定を読み込まない

    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#save-bar")).to_be_hidden()


@pytest.mark.parametrize("kw,admin_nav_count", [({}, None), ({"user": USER_MEMBER}, 0)],
                         ids=["admin", "member"])
def test_nav_system_admin_only_for_admin(page, web_base_url, kw, admin_nav_count):
    install_api_mocks(page, **kw)
    page.goto(f"{web_base_url}/home.html")

    nav = page.locator("#sherpa-nav")
    expect(nav.get_by_text("個人設定", exact=True)).to_be_visible()      # 全員
    if admin_nav_count is None:
        expect(nav.get_by_text("システム管理", exact=True)).to_be_visible()
    else:
        expect(nav.get_by_text("システム管理", exact=True)).to_have_count(admin_nav_count)


def test_embed_tab_bar_renders_real_tabs_with_lazy_iframes(page, web_base_url):
    """ユーザー管理・利用統計・監査ログ・システム状態は旧リンクでなく本物のタブ（role="tab"）で、
    選ぶまで iframe は src を持たない。"""
    _admin(page, web_base_url)

    expect(page.locator('#admin-tabs a.tab-link')).to_have_count(0)
    embed_btns = page.locator(
        '#admin-tabs .tab-btn[data-tab="users"], #admin-tabs .tab-btn[data-tab="usage-page"], '
        '#admin-tabs .tab-btn[data-tab="audit"], #admin-tabs .tab-btn[data-tab="status"]')
    expect(embed_btns).to_have_count(4)
    for tab_key in ("users", "usage-page", "audit", "status"):
        btn = page.locator(f'#admin-tabs .tab-btn[data-tab="{tab_key}"]')
        expect(btn).to_have_attribute("role", "tab")
        assert btn.get_attribute("href") is None
        expect(btn).to_have_attribute("aria-selected", "false")
    for frame_id in ("embed-frame-users", "embed-frame-usage-page",
                     "embed-frame-audit", "embed-frame-status"):
        assert page.locator(f"#{frame_id}").get_attribute("src") is None


def test_embed_tab_click_stays_on_page_and_loads_only_selected_iframe(page, web_base_url):
    """埋め込みタブは URL のハッシュだけが変わり、未保存の変更があっても確認ダイアログは出ない。
    選んだタブの iframe だけ `?embed=1` 付きの src を持つ。"""
    _admin(page, web_base_url, tab="ingest")
    page.locator(ARM_PDF).uncheck()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()

    page.locator('.tab-btn[data-tab="status"]').click()   # ダイアログ未登録でも出ない想定

    expect(page).to_have_url(f"{web_base_url}/admin-settings.html#status")
    expect(page.locator("#tabpanel-status")).to_be_visible()
    expect(page.locator("#tabpanel-status iframe.embed-frame")).to_be_visible()
    expect(page.locator('.tab-btn[data-tab="status"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#embed-frame-status")).to_have_attribute("src", "status.html?embed=1")
    for frame_id in ("embed-frame-users", "embed-frame-usage-page", "embed-frame-audit"):
        assert page.locator(f"#{frame_id}").get_attribute("src") is None

    for btn, frame, src in (
            ('.tab-btn[data-tab="users"]', "#embed-frame-users", "admin-users.html?embed=1"),
            ('.tab-btn[data-tab="usage-page"]', "#embed-frame-usage-page", "usage.html?embed=1"),
            ('.tab-btn[data-tab="audit"]', "#embed-frame-audit", "audit.html?embed=1")):
        page.locator(btn).click()
        expect(page.locator(frame)).to_have_attribute("src", src)


@pytest.mark.parametrize("path,embedded", [("/admin-users.html", False), ("/admin-users.html?embed=1", True)],
                         ids=["standalone", "embed"])
def test_admin_users_embed_param_hides_own_nav(page, web_base_url, path, embedded):
    install_api_mocks(page)
    page.goto(f"{web_base_url}{path}")

    if embedded:
        expect(page.locator("html")).to_have_class("embedded")
        expect(page.locator("sherpa-topbar")).to_be_hidden()
        expect(page.locator("#user-tbody tr").first).to_be_visible()
    else:
        expect(page.locator("sherpa-topbar")).to_be_visible()
        expect(page.locator("#sherpa-nav")).to_be_visible()
        assert page.evaluate("document.documentElement.classList.contains('embedded')") is False


# ===== 保存の差分送信（触った項目だけ・元に戻せば送らない） =====

@pytest.mark.parametrize("key_set", [False, True], ids=["no-key", "key-set"])
def test_save_without_touching_anything_sends_empty_body(page, web_base_url, key_set):
    """何も触らずに保存すると body は空（含めると後で既定が変わっても固定値に追従できなくなる）。"""
    view = _view()
    view["cloud"]["openai_key_set"] = key_set
    records = _admin(page, web_base_url, system_settings=view)

    _save_ok(page)
    put = _last_put(records)
    for key in ("arms_enabled", "legacy_backend", "rag_llm_render", "vlm", "cloud_provider",
                "openai_api_key", "research_default_provider", "openai_endpoint_kind",
                "openai_base_url", "openai_auth_header", "openai_api_version"):
        assert key not in put
    assert put == {}


def test_ingest_tab_renders_arms_and_saves_touched_arms(page, web_base_url):
    records = _admin(page, web_base_url, tab="ingest")

    arms = page.locator("#arms-list input[type=checkbox]")
    expect(arms).to_have_count(3)
    expect(page.locator("#arms-list")).to_contain_text("Office 文書から直接読み取り")
    expect(page.locator("#arms-list")).to_contain_text("PDF の文字を抽出")
    expect(page.locator("#arms-list")).to_contain_text("画像・スキャン文書を AI が見て読み取り")
    expect(page.locator("#arms-list input[data-arm='vision']")).to_be_enabled()
    expect(page.locator("#arms-status")).to_contain_text("既定")
    expect(page.locator("#prices-body")).to_have_count(0)   # コスト単価表・為替は撤去済み
    expect(page.locator("#usd-jpy")).to_have_count(0)

    page.locator(ARM_PDF).uncheck()
    _save_ok(page)
    put = _last_put(records)
    assert put["arms_enabled"] == ["ooxml"]
    assert "token_prices" not in put and "usd_jpy" not in put
    expect(page.locator("#arms-status")).to_contain_text("固定中")


@pytest.mark.parametrize("selector,put_key", [(ARM_PDF, "arms_enabled"),
                                              ("#rag-llm-render", "rag_llm_render")],
                         ids=["arms", "rag-llm-render"])
def test_ingest_toggle_reverted_hides_dot_and_omits_key(page, web_base_url, selector, put_key):
    """ダーティ判定は render() 時点の基準値との差で行う。元へ戻すと丸印も PUT 対象からも外れる。"""
    records = _admin(page, web_base_url, tab="ingest")

    box = page.locator(selector)
    box.uncheck()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()
    box.check()
    expect(page.locator("#tab-dot-ingest")).to_be_hidden()

    _save_ok(page)
    assert put_key not in _last_put(records)


def test_rag_llm_render_card_plain_language_cost_notice_and_toggle(page, web_base_url):
    """検索用文書の整形カードは専門用語を出さず利用料を明示し、オフにすると "off" を送る。"""
    records = _admin(page, web_base_url, tab="ingest")

    card = page.locator("#rag-llm-render-card")
    expect(card).to_be_visible()
    expect(card).to_contain_text("利用料が発生します")
    expect(card).not_to_contain_text("LLM")
    expect(card).not_to_contain_text("rag.md")
    expect(page.locator("#rag-llm-render")).to_be_checked()
    expect(page.locator("#rag-llm-render-status")).to_contain_text("既定に従っています")

    page.locator("#rag-llm-render").uncheck()
    _save_ok(page)
    assert _last_put(records)["rag_llm_render"] == "off"


def _hl_ingest(v):
    v["arms"]["configured"] = ["ooxml"]
    v["arms"]["enabled"] = ["ooxml"]


def _hl_rag(v):
    v["rag_llm_render"] = {"configured": "off", "effective": False, "default": True,
                           "options": ["on", "off"]}


def _hl_budget(v):
    v["agentic_budget"]["per_result"] = {"configured": 100_000, "effective": 100_000, "default": 262144}


def _hl_codex(v):
    v["codex_worker_model"] = {"configured": "gpt-5.6-sol-mini", "effective": "gpt-5.6-sol-mini",
                               "default": "gpt-5.6-sol"}


@pytest.mark.parametrize("tab,patch,selector,cls", [
    ("ingest", _hl_ingest, "#arms-list", "cfg-changed"),
    ("ingest", _hl_rag, "#rag-llm-render-card", CHANGED),
    ("research", _hl_budget, "#agentic-budget-per-result", CHANGED),
    ("research", _hl_codex, "#codex-worker-model", CHANGED),
], ids=["arms", "rag-llm-render", "agentic-budget", "codex-worker-model"])
def test_highlight_only_items_that_differ_from_default(page, web_base_url, tab, patch, selector, cls):
    view = _view()
    patch(view)
    _admin(page, web_base_url, tab=tab, system_settings=view)

    if selector == "#rag-llm-render-card":
        expect(page.locator("#rag-llm-render")).not_to_be_checked()
        expect(page.locator("#rag-llm-render-status")).to_contain_text("固定中")
    expect(page.locator(selector)).to_have_class(cls)


# ===== 取り込みタブの詳細（旧形式変換・視覚読み取り） =====

_LO_AVAIL = {"available": True, "version": "LibreOffice 7.5"}
_LO_MISSING = {"available": False, "version": None}
_OC_VERSIONS = {"word": "16.0", "excel": "16.0", "powerpoint": "16.0"}


def _legacy_view(legacy):
    return {**SYSTEM_SETTINGS_VIEW, "legacy_backend": legacy}


def test_legacy_backend_radio_and_missing_notice(page, web_base_url):
    """既定は soffice 未検出・Office 連携ワーカー未設定。選べない選択肢には案内が出る。"""
    _admin(page, web_base_url, tab="ingest", adv=True)

    expect(page.locator("#legacy-block")).to_be_visible()
    expect(page.locator("#legacy-radios input[type=radio]")).to_have_count(3)
    expect(page.locator("#legacy-radios input[data-legacy='none']")).to_be_checked()
    expect(page.locator("#legacy-radios input[data-legacy='libreoffice']")).to_be_disabled()
    expect(page.locator("#legacy-radios input[data-legacy='office_com']")).to_be_disabled()
    expect(page.locator("#legacy-radios")).to_contain_text("使わない（既定）")
    expect(page.locator("#legacy-radios")).to_contain_text("Office 連携")
    expect(page.locator("#legacy-status")).to_contain_text("既定に従っています")
    expect(page.locator("#legacy-lo-missing")).to_be_visible()
    expect(page.locator("#legacy-lo-missing")).to_contain_text("LibreOffice が見つかりません")
    expect(page.locator("#legacy-radios input[data-legacy='libreoffice'] ~ span")).to_contain_text(
        "LibreOffice が入っていません")
    expect(page.locator("#required-tools-body tr[data-tool='libreoffice']")).to_contain_text("入っていません")
    expect(page.locator("#required-tools-body tr[data-tool='libreoffice']")).to_contain_text("apt-get install")
    expect(page.locator("#required-tools-body tr[data-tool='chromium']")).to_contain_text("入っています")
    expect(page.locator("#legacy-oc-missing")).to_be_visible()
    expect(page.locator("#legacy-oc-missing")).to_contain_text("SHERPA_OFFICE_COM_URL")


_LEGACY_BASE = {"configured": None, "effective": "none", "default": "none"}
_OPTS3 = ["none", "libreoffice", "office_com"]


@pytest.mark.parametrize("legacy,radio,missing,text,selectable", [
    ({**_LEGACY_BASE, "options": _OPTS3, "libreoffice": _LO_MISSING,
      "office_com": {"configured_url": True, "available": True, "versions": _OC_VERSIONS}},
     "office_com", "#legacy-oc-missing", "Word 16.0", True),
    ({**_LEGACY_BASE, "options": _OPTS3, "libreoffice": _LO_MISSING,
      "office_com": {"configured_url": False, "mode": "direct", "powershell": True,
                     "available": True, "versions": _OC_VERSIONS}},
     "office_com", "#legacy-oc-missing", "このパソコンの Office を直接使用", True),
    ({**_LEGACY_BASE, "options": _OPTS3, "libreoffice": _LO_MISSING,
      "office_com": {"configured_url": False, "mode": "direct", "powershell": True,
                     "available": False, "versions": None}},
     "office_com", None, None, False),
    ({**_LEGACY_BASE, "options": ["none", "libreoffice"], "libreoffice": _LO_AVAIL},
     "libreoffice", "#legacy-lo-missing", None, True),
], ids=["office-com-reachable", "office-com-direct", "office-com-direct-without-office", "libreoffice"])
def test_legacy_backend_selectable_only_when_detected(page, web_base_url, legacy, radio, missing, text,
                                                      selectable):
    records = _admin(page, web_base_url, tab="ingest", adv=True, system_settings=_legacy_view(legacy))

    box = page.locator(f"#legacy-radios input[data-legacy='{radio}']")
    if not selectable:
        expect(box).to_be_disabled()
        return
    expect(page.locator(missing)).to_be_hidden()
    if text:
        expect(page.locator("#legacy-radios")).to_contain_text(text)
    expect(box).to_be_enabled()
    box.check()
    _save_ok(page)
    assert _last_put(records)["legacy_backend"] == radio
    expect(box).to_be_checked()
    expect(page.locator("#legacy-status")).to_contain_text("固定中")


def test_legacy_backend_default_marker_follows_env_default(page, web_base_url):
    """「（既定）」マーカーは view.legacy_backend.default に追従し、明示 none（configured）は
    未設定と区別されて「固定中」と表示される。"""
    view = _legacy_view({"configured": "none", "effective": "none", "default": "libreoffice",
                         "options": ["none", "libreoffice"], "libreoffice": _LO_AVAIL})
    _admin(page, web_base_url, tab="ingest", adv=True, system_settings=view)

    expect(page.locator("#legacy-radios")).to_contain_text("LibreOffice で変換（既定）")
    expect(page.locator("#legacy-radios")).not_to_contain_text("使わない（既定）")
    expect(page.locator("#legacy-radios input[data-legacy='none']")).to_be_checked()
    expect(page.locator("#legacy-status")).to_contain_text("固定中")


def test_vlm_renders_and_saves(page, web_base_url):
    records = _admin(page, web_base_url, tab="ingest", adv=True)

    expect(page.locator("#vlm-block")).to_be_visible()
    expect(page.locator("#vlm-provider")).to_have_value("ollama")
    expect(page.locator("#vlm-model")).to_have_value("qwen2.5vl")
    expect(page.locator("#vlm-cloud-allowed")).not_to_be_checked()
    expect(page.locator("#vlm-block")).to_contain_text("画像が外部の AI")
    expect(page.locator("#vlm-status")).to_contain_text("既定")

    page.locator("#vlm-provider").select_option("openai")
    expect(page.locator("#vlm-key-missing")).to_be_visible()
    expect(page.locator("#vlm-key-missing")).to_contain_text("OPENAI_API_KEY")
    page.locator("#vlm-cloud-allowed").check()
    page.locator("#vlm-model").fill("gpt-4o")
    _save_ok(page)
    assert _last_put(records)["vlm"] == {"provider": "openai", "model": "gpt-4o", "cloud_allowed": True}
    expect(page.locator("#vlm-status")).to_contain_text("固定中")


# ===== タブ単位の「既定に戻す」 =====

@pytest.mark.parametrize("tab,patch,expected,exact,absent,field_after", [
    ("ingest", None,
     {"arms_enabled": None, "legacy_backend": None, "vlm": None, "rag_llm_render": None}, True, (), None),
    ("models", None, {"model_catalog": None}, False, (), None),
    ("extkeys",
     lambda v: v["ext_keys"].update(
         user_api_keys_allowed=True, self_issued_active_count=0,
         research_default_provider={"configured": "openai", "effective": "openai", "default": "ollama"}),
     {"user_api_keys_allowed": False, "user_api_keys_daily_quota_default": None,
      "research_default_provider": None}, False, ("usage_chat_provider",),
     ("#ext-research-default-provider", "ollama")),
], ids=["ingest", "models", "extkeys"])
def test_tab_reset_sends_only_that_tabs_keys(page, web_base_url, tab, patch, expected, exact, absent,
                                             field_after):
    """タブのリセットは対象キーだけを送る（ingest は4キー・external は実効既定と同値の明示 false）。"""
    view = _view()
    if patch:
        patch(view)
    records = _admin(page, web_base_url, tab=tab, system_settings=view)

    page.locator(f'[data-reset-tab="{tab}"]').click()
    expect(page.locator(f"#tab-reset-res-{tab}")).to_contain_text("既定に戻しました")
    put = _last_put(records)
    if exact:
        assert put == expected
    for key, value in expected.items():
        assert put[key] == value and type(put[key]) is type(value)
    for key in absent:
        assert key not in put
    if field_after:
        expect(page.locator(field_after[0])).to_have_value(field_after[1])


@pytest.mark.parametrize("confirm", [True, False], ids=["accepted", "rejected"])
def test_provider_tab_reset_confirms_when_personal_keys_in_use(page, web_base_url, confirm):
    """個人キー保有者がいる状態のリセットは確認を経る。個人キー許可は null でなく明示 false を送る
    （バックエンドの一括削除は厳密な false でだけ発火する）。棄却すると PUT 自体を送らない。"""
    view = _view()
    view["cloud"]["personal_api_keys_allowed"] = True
    view["cloud"]["personal_keys_in_use_count"] = 3
    records = _admin(page, web_base_url, system_settings=view)

    if confirm:
        page.once("dialog", lambda d: d.accept())
    page.locator('[data-reset-tab="provider"]').click()   # 未登録＝既定で自動棄却
    if confirm:
        expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
        put = _last_put(records)
        assert put["personal_api_keys_allowed"] is False
        assert put["cloud_provider"] is None
    else:
        expect(page.locator("#tab-reset-res-provider")).not_to_contain_text("既定に戻しました")
        assert records["admin_settings_put"] == []


def test_extkeys_tab_reset_confirms_when_self_issued_keys_active(page, web_base_url):
    view = _view()
    view["ext_keys"]["user_api_keys_allowed"] = True
    view["ext_keys"]["self_issued_active_count"] = 2
    records = _admin(page, web_base_url, tab="extkeys", system_settings=view)

    page.locator('[data-reset-tab="extkeys"]').click()   # 未登録＝既定で自動棄却
    expect(page.locator("#tab-reset-res-extkeys")).not_to_contain_text("既定に戻しました")
    assert records["admin_settings_put"] == []

    page.once("dialog", lambda d: d.accept())
    page.locator('[data-reset-tab="extkeys"]').click()
    expect(page.locator("#tab-reset-res-extkeys")).to_contain_text("既定に戻しました")
    assert _last_put(records)["user_api_keys_allowed"] is False


def test_research_tab_reset_nulls_all_keys_and_keeps_provider_draft(page, web_base_url):
    """調査・回答タブのリセットは12キー全てを null で送り、プロバイダタブの未保存キー入力は残す。"""
    view = _view()
    view["depth_profile"]["grep_max_hits"] = {"configured": 20, "effective": 20, "default": 30}
    view["agentic_budget"]["per_result"] = {"configured": 100_000, "effective": 100_000, "default": 262144}
    records = _admin(page, web_base_url, system_settings=view)
    page.locator('#cloud-key').fill('sk-test-unsaved')
    open_tab(page, 'research')
    expect(page.locator("#depth-base-grep-max-hits")).to_have_value("20")
    expect(page.locator("#agentic-budget-per-result")).to_have_value(str(round(100_000 / 1024)))
    page.locator('#depth-base-codex-reasoning').select_option('high')

    page.locator('[data-reset-tab="research"]').click()
    expect(page.locator('#tab-reset-res-research')).to_contain_text('既定に戻しました')
    body = records['admin_settings_put'][-1]
    assert len(body) == 12 and all(value is None for value in body.values())
    assert set(body) == {
        'depth_base_grep_max_hits', 'depth_base_qa_max_hits',
        'depth_base_read_window', 'depth_base_impact_depth', 'depth_base_troubleshoot_depth',
        'depth_base_codex_reasoning', 'embed_parallel',
        'max_review_rounds', 'codex_worker_model', 'codex_session_retention_days',
        'agentic_budget_per_result', 'codex_mode',
    }
    assert 'agentic_budget_total' not in body
    assert 'openai_api_key' not in body and 'cloud_provider' not in body
    expect(page.locator('#depth-base-grep-max-hits')).to_have_value('')
    expect(page.locator("#agentic-budget-per-result")).to_have_value("")
    expect(page.locator('#tab-dot-research')).to_be_hidden()
    open_tab(page, 'provider')
    expect(page.locator('#cloud-key')).to_have_value('sk-test-unsaved')
    expect(page.locator('#tab-dot-provider')).to_be_visible()


def test_tab_reset_preserves_other_tab_unsaved_draft_and_dirty_dot(page, web_base_url):
    """調査タブのリセットで、取り込みタブの未保存編集・丸印とプロバイダタブの書込専用キー入力は残る。"""
    records = _admin(page, web_base_url)
    page.locator("#cloud-key").fill("sk-unsaved-draft")
    open_tab(page, "ingest")
    page.locator(ARM_PDF).uncheck()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()

    open_tab(page, "research")
    page.locator('[data-reset-tab="research"]').click()
    expect(page.locator("#tab-reset-res-research")).to_contain_text("既定に戻しました")

    open_tab(page, "ingest")
    expect(page.locator(ARM_PDF)).not_to_be_checked()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()
    open_tab(page, "provider")
    expect(page.locator("#cloud-key")).to_have_value("sk-unsaved-draft")

    _save_ok(page)
    put = _last_put(records)
    assert put["arms_enabled"] == ["ooxml"]
    assert put["openai_api_key"] == "sk-unsaved-draft"


def test_provider_reset_preserves_research_draft(page, web_base_url):
    records = _admin(page, web_base_url, url="/admin-settings.html#research")
    page.locator('#depth-base-grep-max-hits').fill('20')
    page.locator('#depth-base-read-window').fill('50')
    open_tab(page, 'provider')
    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator('#tab-reset-res-provider')).to_contain_text('既定に戻しました')
    assert not any(key.startswith('depth_base_') for key in _last_put(records))
    open_tab(page, 'research')
    expect(page.locator('#depth-base-grep-max-hits')).to_have_value('20')
    expect(page.locator('#depth-base-read-window')).to_have_value('50')
    expect(page.locator('#tab-dot-research')).to_be_visible()
    _save_ok(page)
    assert _last_put(records) == {'depth_base_grep_max_hits': 20, 'depth_base_read_window': 50}


def test_ingest_reset_preserves_research_budget_draft(page, web_base_url):
    records = _admin(page, web_base_url, tab="research")
    page.locator('#agentic-budget-per-result').fill('512')
    expect(page.locator('#tab-dot-research')).to_be_visible()
    expect(page.locator('#tab-dot-ingest')).to_be_hidden()
    open_tab(page, 'ingest')
    page.locator('[data-reset-tab="ingest"]').click()
    expect(page.locator('#tab-reset-res-ingest')).to_contain_text('既定に戻しました')
    assert 'agentic_budget_per_result' not in _last_put(records)
    open_tab(page, 'research')
    expect(page.locator('#agentic-budget-per-result')).to_have_value('512')
    expect(page.locator('#tab-dot-research')).to_be_visible()
    _save_ok(page)
    assert _last_put(records)['agentic_budget_per_result'] == 512 * 1024


# ===== クラウド AI プロバイダの中央設定 =====

def test_cloud_provider_renders_defaults(page, web_base_url):
    """既定は OpenAI・キー未設定・個人キー許可 OFF・接続先は本家で詳細欄は隠れる。"""
    _admin(page, web_base_url)

    expect(page.locator("input[data-cloud-provider='openai']")).to_be_checked()
    expect(page.locator("input[data-cloud-provider]")).to_have_count(1)
    expect(page.locator("#cloud-key-label")).to_contain_text("OpenAI")
    expect(page.locator("#cloud-key")).to_have_value("")
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "未設定")
    expect(page.locator("#cloud-key-clear")).to_be_disabled()
    expect(page.locator("#personal-keys-allowed")).not_to_be_checked()
    expect(page.locator("#cloud-status")).to_contain_text("OpenAI")
    expect(page.locator("#cloud-status")).to_contain_text("中央設定のみ")
    expect(page.locator("#cloud-ollama-url")).to_have_value("http://localhost:11434")
    expect(page.locator("#cloud-retired-warn")).to_be_hidden()
    note = page.locator("#personal-keys-allowed").locator("xpath=ancestor::label").locator(".arm-d")
    expect(note).to_contain_text("個人キーは保存されません")
    expect(note).to_contain_text("削除されます")
    expect(page.locator("input[data-openai-endpoint-kind='openai']")).to_be_checked()
    expect(page.locator("#openai-endpoint-fields")).to_be_hidden()


def test_cloud_provider_save_sends_only_touched_fields(page, web_base_url):
    records = _admin(page, web_base_url)

    page.locator("#cloud-key").fill("openai-secret-key")
    page.locator("#personal-keys-allowed").check()
    _save_ok(page)
    put = _last_put(records)
    assert put["openai_api_key"] == "openai-secret-key"
    assert put["personal_api_keys_allowed"] is True
    assert "arms_enabled" not in put and "legacy_backend" not in put
    assert "cloud_provider" not in put   # ラジオに触れていない保存で選択を作らない


@pytest.mark.parametrize("raw", [None, "not-a-real-provider"], ids=["unset", "invalid-saved"])
def test_cloud_provider_explicit_click_on_default_still_saves_raw_value(page, web_base_url, raw):
    """既定の openai を明示クリックして保存すると、値が変わらなくても cloud_provider を送る
    （生値が未設定・不正で丸め表示されている場合も、選び直しが保存対象から漏れない）。"""
    view = _view()
    if raw:
        view["cloud"]["provider"] = "openai"
        view["cloud"]["provider_raw"] = raw
    records = _admin(page, web_base_url, system_settings=view)

    page.locator("input[data-cloud-provider='openai']").click()
    page.locator("#cloud-key").fill("openai-secret-key")
    _save_ok(page)
    put = _last_put(records)
    assert put["cloud_provider"] == "openai"
    assert put["openai_api_key"] == "openai-secret-key"


@pytest.mark.parametrize("in_use,accept,saved", [(3, False, False), (2, True, True), (0, False, True)],
                         ids=["cancel-aborts", "confirmed", "no-keys-no-dialog"])
def test_personal_keys_off_save_confirms_with_count(page, web_base_url, in_use, accept, saved):
    """個人キー許可を ON→OFF で保存すると、保有者数を示す確認が出る（0 件なら出さない）。
    キャンセルすると保存全体を中断する。"""
    view = _view()
    view["cloud"]["personal_api_keys_allowed"] = True
    view["cloud"]["personal_keys_in_use_count"] = in_use
    records = install_api_mocks(page, system_settings=view)
    dialogs = _dialogs(page, accept)
    goto_admin_settings(page, web_base_url)

    expect(page.locator("#personal-keys-allowed")).to_be_checked()
    page.locator("#personal-keys-allowed").uncheck()
    page.locator("#save").click()

    if saved:
        expect(page.locator("#msg")).to_contain_text("保存しました")
        assert records["admin_settings_put"][-1]["personal_api_keys_allowed"] is False
        assert bool(dialogs) == (in_use > 0)
    else:
        assert dialogs and "3 人" in dialogs[0] and "削除されます" in dialogs[0]
        expect(page.locator("#msg")).to_contain_text("保存を取り消しました")
        assert records["admin_settings_put"] == []


def test_cloud_key_shown_as_placeholder_and_cleared_only_by_dedicated_action(page, web_base_url):
    """設定済みキーは値を返さずプレースホルダで示す。空欄保存では変更されず、削除は確認を経る専用操作だけ
    （棄却すると何も送らず、成功後はボタンが再び disabled になる）。"""
    view = _view()
    view["cloud"]["openai_key_set"] = True
    records = _admin(page, web_base_url, system_settings=view)

    expect(page.locator("#cloud-key")).to_have_value("")
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "設定済み（変更する場合のみ入力）")
    expect(page.locator("#cloud-key-clear")).to_be_enabled()
    _save_ok(page)
    assert "openai_api_key" not in _last_put(records)

    page.locator("#cloud-key-clear").click()   # 未登録＝既定で自動棄却
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")
    assert len(records["admin_settings_put"]) == 1
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "設定済み（変更する場合のみ入力）")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しました")
    assert _last_put(records) == {"openai_api_key": ""}
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "未設定")
    expect(page.locator("#cloud-key-clear")).to_be_disabled()


def test_cloud_key_clear_preserves_unsaved_edits_dot_and_updates_vlm_warning(page, web_base_url):
    """キー削除は変わったキー欄の表示とキー有無に依存する案内（視覚読み取りの警告）だけを更新し、
    同タブの他の未保存編集・未保存丸印を無言で巻き戻さない。"""
    view = _view()
    view["cloud"]["openai_key_set"] = True
    view["cloud"]["personal_api_keys_allowed"] = False
    view["vlm"]["openai_key_present"] = True
    view["vlm"]["effective"]["provider"] = "openai"
    records = _admin(page, web_base_url, system_settings=view)
    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")
    expect(page.locator("#vlm-key-missing")).to_be_hidden()
    open_tab(page, "provider")

    page.locator("#personal-keys-allowed").check()
    expect(page.locator("#tab-dot-provider")).to_be_visible()
    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しました")

    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "未設定")
    expect(page.locator("#personal-keys-allowed")).to_be_checked()
    expect(page.locator("#tab-dot-provider")).to_be_visible()
    assert _last_put(records) == {"openai_api_key": ""}   # 無関係な項目を巻き込まない

    open_tab(page, "ingest")
    expect(page.locator("#vlm-key-missing")).to_be_visible()
    expect(page.locator("#vlm-key-missing")).to_contain_text("OPENAI_API_KEY")


def _start_pending_key_clear(page, web_base_url):
    view = _view()
    view["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=view)
    held = _hold_first_put(page)
    goto_admin_settings(page, web_base_url)
    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()   # 最初の PUT（削除）は保留のまま
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しています")
    return view, held


def test_cloud_key_typed_after_clear_started_is_not_wiped_by_stale_response(page, web_base_url):
    """削除待ち中に同じ欄へ新しいキーを入力し始めると、後から届く削除応答は入力を消さず、
    入力時点で「削除しています...」の残留表示も消える。"""
    view, held = _start_pending_key_clear(page, web_base_url)

    page.locator("#cloud-key").fill("sk-typed-after-clear-started")
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")

    cleared = json.loads(json.dumps(view))
    cleared["cloud"]["openai_key_set"] = False
    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps(cleared, ensure_ascii=False))
    page.wait_for_timeout(200)

    expect(page.locator("#cloud-key")).to_have_value("sk-typed-after-clear-started")
    expect(page.locator("#cloud-key-clear-res")).not_to_contain_text("削除しました")


def test_cloud_key_clear_pending_message_cleared_after_save(page, web_base_url):
    _start_pending_key_clear(page, web_base_url)

    page.locator("#cloud-key").fill("sk-saved-after-clear-started")
    _save_ok(page)
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")


def test_cloud_key_clear_pending_message_cleared_by_other_tab_reset(page, web_base_url):
    _start_pending_key_clear(page, web_base_url)

    open_tab(page, "models")
    page.locator('[data-reset-tab="models"]').click()
    expect(page.locator("#tab-reset-res-models")).to_contain_text("既定に戻しました")
    open_tab(page, "provider")
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")


# ===== OpenAI 互換 API の接続先と埋め込みデプロイ名 =====

def test_openai_endpoint_switch_to_azure_reveals_fields_and_saves(page, web_base_url):
    records = _admin(page, web_base_url)

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    expect(page.locator("#openai-endpoint-fields")).to_be_visible()
    open_advanced(page, "tabpanel-provider")
    page.locator("#openai-endpoint-base-url").fill(AZURE_URL)
    page.locator("#openai-endpoint-auth-header").select_option("api-key")
    page.locator("#openai-endpoint-api-version").fill("2026-05-01-preview")
    _save_ok(page)
    put = _last_put(records)
    assert put["openai_endpoint_kind"] == "azure"
    assert put["openai_base_url"] == AZURE_URL
    assert put["openai_auth_header"] == "api-key"
    assert put["openai_api_version"] == "2026-05-01-preview"


def test_openai_endpoint_switch_back_to_openai_preserves_detail_fields(page, web_base_url):
    """本家へ戻して保存しても base URL 等の詳細欄は null 化せず送らない（kind のみ送る）。"""
    records = _admin(page, web_base_url, system_settings=_azure_view())

    expect(page.locator("input[data-openai-endpoint-kind='azure']")).to_be_checked()
    expect(page.locator("#openai-endpoint-base-url")).to_have_value(AZURE_URL)
    page.locator("input[data-openai-endpoint-kind='openai']").check()
    expect(page.locator("#openai-endpoint-fields")).to_be_hidden()
    _save_ok(page)
    put = _last_put(records)
    assert put["openai_endpoint_kind"] == "openai"
    for key in ("openai_base_url", "openai_auth_header", "openai_api_version"):
        assert key not in put


@pytest.mark.parametrize("selector,op,value,put_key", [
    ("#openai-endpoint-auth-header", "select", "api-key", "openai_auth_header"),
    ("#openai-endpoint-api-version", "fill", "2026-06-01-preview", "openai_api_version"),
], ids=["auth-header", "api-version"])
def test_openai_endpoint_detail_field_alone_change_is_saved(page, web_base_url, selector, op, value, put_key):
    """「詳細」折りたたみの中の項目だけを変えても保存される（監視は値そのもので判定する）。"""
    records = _admin(page, web_base_url, tab="provider", adv=True, system_settings=_azure_view())

    field = page.locator(selector)
    field.select_option(value) if op == "select" else field.fill(value)
    _save_ok(page)
    put = _last_put(records)
    assert put["openai_endpoint_kind"] == "azure"
    assert put[put_key] == value
    assert put["openai_base_url"] == AZURE_URL


def test_openai_endpoint_embed_deployment_updates_model_catalog(page, web_base_url):
    """埋め込みのデプロイ名欄は model_catalog（openai/embed）へ直接反映し、接続先ラジオには触れない。"""
    records = _admin(page, web_base_url, system_settings=_azure_kind_only_view())

    expect(page.locator("#openai-endpoint-fields")).to_be_visible()
    page.locator("#openai-endpoint-embed-deployment").fill("my-embed-deployment")
    _save_ok(page)
    put = _last_put(records)
    assert "openai_endpoint_kind" not in put
    cell = put["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "my-embed-deployment"
    assert "my-embed-deployment" in cell["allowed"]


def test_embed_deployment_catalog_tab_change_syncs_to_provider_field(page, web_base_url):
    records = _admin(page, web_base_url, tab="models", system_settings=_azure_kind_only_view())

    page.locator(EMBED_SEL).select_option("text-embedding-3-large")
    open_tab(page, "provider")
    expect(page.locator("#openai-endpoint-embed-deployment")).to_have_value("text-embedding-3-large")

    _save_ok(page)
    put = _last_put(records)
    assert put["model_catalog"]["openai"]["embed"]["default"] == "text-embedding-3-large"
    assert "openai_endpoint_kind" not in put


@pytest.mark.parametrize("catalog_first", [True, False], ids=["field-last", "catalog-last"])
def test_embed_deployment_last_touched_field_wins_on_save(page, web_base_url, catalog_first):
    """カタログの既定と埋め込みデプロイ名欄の両方を編集したら、後から触った方が保存される。"""
    records = _admin(page, web_base_url, system_settings=_azure_kind_only_view())

    def edit_catalog():
        open_tab(page, "models")
        page.locator(EMBED_SEL).select_option("text-embedding-3-large")

    def edit_field(value):
        open_tab(page, "provider")
        page.locator("#openai-endpoint-embed-deployment").fill(value)

    if catalog_first:
        edit_catalog()
        edit_field("later-typed-deployment")
        want = "later-typed-deployment"
    else:
        edit_field("earlier-typed-deployment")
        edit_catalog()
        want = "text-embedding-3-large"
    _save_ok(page)
    assert _last_put(records)["model_catalog"]["openai"]["embed"]["default"] == want


def test_embed_deployment_typing_commits_only_final_value(page, web_base_url):
    """逐次入力の途中値（'d'・'de'…）は allowed に入れず、確定（change）した最終値だけを反映する。"""
    records = _admin(page, web_base_url, system_settings=_azure_kind_only_view())

    field = page.locator("#openai-endpoint-embed-deployment")
    field.fill("")
    field.press_sequentially("deploy-x")
    page.locator("#openai-endpoint-base-url").click()   # blur＝確定
    _save_ok(page)
    cell = _last_put(records)["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "deploy-x"
    assert cell["allowed"].count("deploy-x") == 1
    for partial in ("d", "de", "dep", "depl", "deplo", "deploy", "deploy-"):
        assert partial not in cell["allowed"]


def test_embed_deployment_single_change_event_lights_dot_immediately(page, web_base_url):
    """確定（change）したその1イベントだけで未保存丸印が付く。Tab はフォーカス移動のみで別イベントを
    起こさないため、状態更新が dirty 判定より先に済んでいることを厳密に確かめられる。"""
    _admin(page, web_base_url, system_settings=_azure_kind_only_view())

    expect(page.locator("#tab-dot-provider")).to_be_hidden()
    field = page.locator("#openai-endpoint-embed-deployment")
    field.fill("one-shot-deployment")
    field.press("Tab")
    expect(page.locator("#tab-dot-provider")).to_be_visible()


@pytest.mark.parametrize("reset_tab,other_draft", [
    ("provider", lambda page: (open_tab(page, "models"),
                               page.locator(OPENAI_CHAT_SEL).select_option("gpt-5.4-mini"))),
    ("models", lambda page: page.locator("#openai-endpoint-embed-deployment").fill("unsaved-provider-draft")),
], ids=["provider-reset", "models-reset"])
def test_tab_reset_does_not_leak_other_tabs_unsaved_catalog_draft(page, web_base_url, reset_tab, other_draft):
    """他タブの未保存の model_catalog 編集は、リセットの PUT に同送されない（null のまま）。"""
    records = _admin(page, web_base_url, system_settings=_azure_kind_only_view())

    other_draft(page)
    open_tab(page, reset_tab)
    page.locator(f'[data-reset-tab="{reset_tab}"]').click()

    expect(page.locator(f"#tab-reset-res-{reset_tab}")).to_contain_text("既定に戻しました")
    assert _last_put(records)["model_catalog"] is None


def test_provider_reset_clears_embed_and_preserves_other_saved_cells(page, web_base_url):
    view = _view()
    view["model_catalog"]["configured"] = {"openai": {
        "chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.4-mini"},
        "embed": {"allowed": ["text-embedding-3-small", "custom-embed-deployment"],
                  "default": "custom-embed-deployment"}}}
    view["model_catalog"]["effective"]["openai"]["chat"]["default"] = "gpt-5.4-mini"
    view["model_catalog"]["effective"]["openai"]["embed"] = {
        "allowed": ["text-embedding-3-small", "custom-embed-deployment"],
        "default": "custom-embed-deployment"}
    records = _admin(page, web_base_url, system_settings=view)
    expect(page.locator("#openai-endpoint-embed-deployment")).to_have_value("custom-embed-deployment")

    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
    assert _last_put(records)["model_catalog"] == {
        "openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.4-mini"}}}
    expect(page.locator("#openai-endpoint-embed-deployment")).to_have_value("text-embedding-3-small")


def _ollama_chat_view():
    view = _view()
    view["model_catalog"]["effective"]["ollama"]["chat"] = {
        "allowed": ["qwen2.5", "llama3.1"], "default": "qwen2.5"}
    return view


def test_normal_save_after_reset_does_not_refix_cleared_or_untouched_cells(page, web_base_url):
    """リセットで未設定へ戻した embed や触っていないセルは、後の通常保存で既定値として明示固定されない。"""
    view = _ollama_chat_view()
    embed = {"allowed": ["text-embedding-3-small", "custom-embed-deployment"],
             "default": "custom-embed-deployment"}
    view["model_catalog"]["configured"] = {"openai": {"embed": embed}}
    view["model_catalog"]["effective"]["openai"]["embed"] = embed
    records = _admin(page, web_base_url, system_settings=view)

    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
    assert _last_put(records)["model_catalog"] is None

    open_tab(page, "models")
    page.locator(OLLAMA_CHAT_SEL).select_option("llama3.1")
    _save_ok(page)
    put = _last_put(records)["model_catalog"]
    assert put == {"ollama": {"chat": {"allowed": ["qwen2.5", "llama3.1"], "default": "llama3.1"}}}
    assert "openai" not in put


@pytest.mark.parametrize("saved_default,edit_openai", [("gpt-5.4-mini", True), ("gpt-5.5", False)],
                         ids=["reverted-to-builtin-omitted", "untouched-at-builtin-preserved"])
def test_models_save_provenance_of_cells_equal_to_builtin_default(page, web_base_url, saved_default,
                                                                  edit_openai):
    """組み込み既定と同値へ選び直したセルは送らない（既定への追従に戻る）一方、同値で明示保存済みの
    未編集セルは落とさない（将来の既定変更に管理者操作なしで追従してしまわない）。"""
    view = _ollama_chat_view()
    view["model_catalog"]["configured"] = {"openai": {"chat": {
        "allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": saved_default}}}
    view["model_catalog"]["effective"]["openai"]["chat"]["default"] = saved_default
    records = _admin(page, web_base_url, tab="models", system_settings=view)

    if edit_openai:
        page.locator(OPENAI_CHAT_SEL).select_option("gpt-5.5")
    page.locator(OLLAMA_CHAT_SEL).select_option("llama3.1")
    _save_ok(page)
    put = _last_put(records)["model_catalog"]
    assert put["ollama"]["chat"]["default"] == "llama3.1"
    if edit_openai:
        assert "openai" not in put
    else:
        assert put["openai"]["chat"]["default"] == "gpt-5.5"


def test_openai_endpoint_test_button_sends_input_values_without_saving(page, web_base_url):
    records = _admin(page, web_base_url)

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    page.locator("#openai-endpoint-base-url").fill(AZURE_URL)
    page.locator("#openai-endpoint-test").click()

    expect(page.locator("#openai-endpoint-test-res")).to_contain_text("接続OK")
    body = records["admin_openai_endpoint_test"][-1]
    assert body["provider"] == "openai"
    assert body["openai_endpoint_kind"] == "azure"
    assert body["openai_base_url"] == AZURE_URL
    assert records["settings_test"] == []
    assert records["admin_settings_put"] == []


@pytest.mark.parametrize("trigger,result", [("#save", "#msg"),
                                            ("#openai-endpoint-test", "#openai-endpoint-test-res")],
                         ids=["save", "connection-test"])
def test_openai_endpoint_azure_without_base_url_shows_error(page, web_base_url, trigger, result):
    """Azure を選んだまま接続先 URL が空だと、保存も接続テストも同じクロス検証で拒否される。"""
    records = _admin(page, web_base_url)

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    page.locator(trigger).click()

    expect(page.locator(result)).to_contain_text("接続先 URL")
    if trigger == "#openai-endpoint-test":
        body = records["admin_openai_endpoint_test"][-1]
        assert body["openai_endpoint_kind"] == "azure"
        assert not body.get("openai_base_url")
        assert records["admin_settings_put"] == []


@pytest.mark.parametrize("own", [False, True], ids=["module-constant", "caller-dict"])
def test_openai_endpoint_save_does_not_mutate_source_system_settings(page, web_base_url, own):
    """PUT ハンドラの状態更新は元の dict（共有定数・呼び出し元所有）を書き換えない。"""
    source = _view() if own else SYSTEM_SETTINGS_VIEW
    before = copy.deepcopy(source)
    assert before["openai_endpoint"]["configured"]["kind"] is None
    install_api_mocks(page, **({"system_settings": source} if own else {}))
    goto_admin_settings(page, web_base_url)

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    page.locator("#openai-endpoint-base-url").fill(
        "https://caller-dict-mutation-check.openai.azure.com" if own
        else "https://mutation-check.openai.azure.com")
    _save_ok(page)
    assert source == before, (
        "PUT ハンドラが呼び出し元所有の system_settings dict を直接書き換えた（deep-copy 漏れ）" if own
        else "PUT ハンドラがモジュール定数 SYSTEM_SETTINGS_VIEW を直接書き換えた（deep-copy 漏れの再発）")


# ===== 調査・回答タブ =====

def test_research_tab_groups_settings_and_saves_search_base(page, web_base_url):
    records = _admin(page, web_base_url, url="/admin-settings.html#research")
    expect(page.locator('#tabpanel-provider [id^="depth-base-"]')).to_have_count(0)
    expect(page.locator('#max-review-rounds')).to_have_value('')
    expect(page.locator('#max-review-rounds-hint')).to_contain_text('現在の適用値: 7 回。既定: 7 回。')
    expect(page.locator('#search-investigation-card #depth-base-grep-max-hits')).to_be_visible()
    expect(page.locator('#search-investigation-card #depth-base-read-window')).to_be_visible()
    expect(page.locator('#codex-investigation-card #depth-base-codex-reasoning')).to_be_visible()
    expect(page.locator('#depth-base-max-turns')).to_have_count(0)
    expect(page.locator('#search-investigation-card #depth-base-impact-depth')).to_have_count(1)
    expect(page.locator('#agentic-max-tools-per-turn')).to_have_count(0)
    expect(page.locator('#research-api-heading')).not_to_contain_text('API 専用')
    page.locator('#depth-base-grep-max-hits').fill('6')
    expect(page.locator('#tab-dot-research')).to_be_visible()
    expect(page.locator('#tab-dot-provider')).to_be_hidden()
    _save_ok(page)
    assert _last_put(records) == {'depth_base_grep_max_hits': 6}
    page.reload()
    expect(page.locator('#depth-base-grep-max-hits')).to_have_value('6')


def test_codex_worker_model_default_placeholder_save_and_clear(page, web_base_url):
    records = _admin(page, web_base_url, url="/admin-settings.html#research")
    field = page.locator('#codex-worker-model-card #codex-worker-model')
    expect(field).to_have_value('')
    expect(field).to_have_attribute('placeholder', '既定: gpt-5.6-sol')
    expect(page.locator('#codex-worker-model-hint')).to_contain_text('未設定です（実際に適用される値: gpt-5.6-sol。')
    field.fill('gpt-5.6-sol-mini')
    expect(page.locator('#tab-dot-research')).to_be_visible()
    _save_ok(page)
    assert _last_put(records) == {'codex_worker_model': 'gpt-5.6-sol-mini'}
    page.reload()
    expect(page.locator('#codex-worker-model')).to_have_value('gpt-5.6-sol-mini')
    expect(page.locator('#codex-worker-model-hint')).to_contain_text('固定中')

    page.locator('#codex-worker-model').fill('')
    _save_ok(page)
    assert _last_put(records) == {'codex_worker_model': None}


def test_embed_parallel_saves_rejects_out_of_range_and_clears(page, web_base_url):
    records = _admin(page, web_base_url, url="/admin-settings.html#research")
    page.locator('#embed-parallel').fill('8')
    expect(page.locator('#tab-dot-research')).to_be_visible()
    _save_ok(page)
    assert _last_put(records) == {'embed_parallel': 8}
    page.reload()
    expect(page.locator('#embed-parallel')).to_have_value('8')
    expect(page.locator('#embed-parallel-hint')).to_contain_text('固定中')

    page.locator('#embed-parallel').fill('17')
    page.locator('#save').click()
    expect(page.locator('#msg')).to_contain_text('1〜16')
    assert len(records['admin_settings_put']) == 1
    page.locator('#embed-parallel').fill('2')
    _save_ok(page)
    page.locator('#embed-parallel').fill('')
    page.locator('#save').click()
    expect(page.locator('#embed-parallel-hint')).to_contain_text('未設定')
    assert _last_put(records) == {'embed_parallel': None}


def test_agentic_budget_card_unset_then_kb_to_bytes_save(page, web_base_url):
    """入力は KB 単位で、1024 倍した bytes で PUT する（触っていない項目は送らない）。"""
    records = _admin(page, web_base_url, tab="research")
    expect(page.locator("#agentic-budget-per-result")).to_have_value("")
    expect(page.locator("#agentic-budget-per-result-hint")).to_contain_text("未設定です")

    page.locator("#agentic-budget-per-result").fill("500")
    _save_ok(page)
    body = _last_put(records)
    assert body.get("agentic_budget_per_result") == 500 * 1024
    assert set(body) == {"agentic_budget_per_result"}


def test_agentic_budget_card_configured_shown_in_kb_and_cleared_with_null(page, web_base_url):
    view = _view()
    view["agentic_budget"]["per_result"] = {"configured": 256000, "effective": 256000, "default": 262144}
    records = _admin(page, web_base_url, tab="research", system_settings=view)
    expect(page.locator("#agentic-budget-per-result")).to_have_value("250")
    expect(page.locator("#agentic-budget-per-result-hint")).to_contain_text("で固定中です")

    page.locator("#agentic-budget-per-result").fill("")
    _save_ok(page)
    assert _last_put(records).get("agentic_budget_per_result") is None


def test_depth_profile_card_unset_then_saves_changed_fields_only(page, web_base_url):
    """未設定は全欄が空・Codex 推論レベルは空選択肢（環境設定の既定に従う）。変えた項目だけ送る。"""
    records = _admin(page, web_base_url, tab="research")
    expect(page.locator("#depth-base-grep-max-hits")).to_have_value("")
    expect(page.locator("#depth-base-grep-max-hits-hint")).to_contain_text("未設定です")
    expect(page.locator("#depth-base-codex-reasoning")).to_have_value("")
    expect(page.locator("#depth-base-codex-reasoning-hint")).to_contain_text("未設定です")

    page.locator("#depth-base-grep-max-hits").fill("30")
    page.locator("#depth-base-codex-reasoning").select_option("xhigh")
    _save_ok(page)
    body = _last_put(records)
    assert body.get("depth_base_grep_max_hits") == 30
    assert body.get("depth_base_codex_reasoning") == "xhigh"
    assert "depth_base_qa_max_hits" not in body


def test_depth_profile_card_configured_values_and_clearing_send_null(page, web_base_url):
    """保存済みの値は生値で表示される。空欄へ戻す・Codex 推論を空選択肢へ選び直すと、その項目だけ null。"""
    view = _view()
    view["depth_profile"]["grep_max_hits"] = {"configured": 20, "effective": 20, "default": 30}
    view["depth_profile"]["read_window"] = {"configured": 80, "effective": 80, "default": 40}
    view["depth_profile"]["codex_reasoning"] = {
        "configured": "high", "effective": "high", "default": "low",
        "options": ["minimal", "low", "medium", "high", "xhigh"]}
    records = _admin(page, web_base_url, tab="research", system_settings=view)
    expect(page.locator("#depth-base-grep-max-hits")).to_have_value("20")
    expect(page.locator("#depth-base-grep-max-hits-hint")).to_contain_text("この値で固定中")
    expect(page.locator("#depth-base-codex-reasoning")).to_have_value("high")
    expect(page.locator("#depth-base-codex-reasoning-hint")).to_contain_text("この値で固定中")
    expect(page.locator("#depth-base-read-window")).to_have_value("80")

    page.locator("#depth-base-read-window").fill("")
    page.locator("#depth-base-codex-reasoning").select_option("")
    _save_ok(page)
    body = _last_put(records)
    assert body.get("depth_base_read_window") is None
    assert body.get("depth_base_codex_reasoning") is None
    assert "depth_base_qa_max_hits" not in body
    assert "depth_base_grep_max_hits" not in body


@pytest.mark.parametrize("tab,selector,value,message", [
    ("research", "#depth-base-grep-max-hits", "1001", "資料検索のヒット件数上限は1〜1000の整数で指定してください"),
    ("research", "#depth-base-troubleshoot-depth", "0", "原因調査でたどる段数は1〜16の整数で指定してください"),
    ("research", "#agentic-budget-per-result", "8193",
     "ツール結果1件あたりの上限は1〜8192（KB）の整数で指定してください"),
    ("research", "#agentic-budget-per-result", "0",
     "ツール結果1件あたりの上限は1〜8192（KB）の整数で指定してください"),
    (None, "#chat-max-turns-global", "65", "同時に実行できる質問の数（全員の合計）は1〜64の整数で指定してください"),
], ids=["depth-hits", "depth-troubleshoot-zero", "budget-over", "budget-zero", "chat-turns-global"])
def test_out_of_range_numeric_input_rejected_client_side(page, web_base_url, tab, selector, value, message):
    """範囲外の値は日本語エラーを出して PUT 自体を送らない（422 の配列表示が読めなくなるのを防ぐ）。"""
    records = _admin(page, web_base_url, tab=tab)

    page.locator(selector).fill(value)
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text(message)
    assert records["admin_settings_put"] == []


def test_chat_max_turns_card_unset_then_saves_changed_field_only(page, web_base_url):
    records = _admin(page, web_base_url)
    expect(page.locator("#chat-max-turns-per-user")).to_have_value("")
    expect(page.locator("#chat-max-turns-per-user-hint")).to_contain_text("未設定です")
    expect(page.locator("#chat-max-turns-global")).to_have_value("")
    expect(page.locator("#chat-max-turns-global-hint")).to_contain_text("未設定です")

    page.locator("#chat-max-turns-per-user").fill("5")
    _save_ok(page)
    body = _last_put(records)
    assert body.get("chat_max_turns_per_user") == 5
    assert "chat_max_turns_global" not in body


def test_chat_max_turns_card_configured_clear_and_tab_reset(page, web_base_url):
    view = _view()
    view["chat_max_turns"]["per_user"] = {"configured": 5, "effective": 5, "default": 2}
    view["chat_max_turns"]["global"] = {"configured": 30, "effective": 30, "default": 8}
    records = _admin(page, web_base_url, system_settings=view)
    expect(page.locator("#chat-max-turns-per-user")).to_have_value("5")
    expect(page.locator("#chat-max-turns-per-user-hint")).to_contain_text("この値で固定中")
    expect(page.locator("#chat-max-turns-global")).to_have_value("30")

    page.locator("#chat-max-turns-global").fill("")
    _save_ok(page)
    assert _last_put(records).get("chat_max_turns_global") is None

    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
    expect(page.locator("#chat-max-turns-per-user")).to_have_value("")
    body = _last_put(records)
    assert body.get("chat_max_turns_per_user") is None
    assert body.get("chat_max_turns_global") is None


# ===== 外部連携タブ（利用者キー許可・簡易回答 AI） =====

def test_user_api_keys_toggle_off_confirms_with_count_and_cancel_aborts(page, web_base_url):
    view = _view()
    view["ext_keys"]["user_api_keys_allowed"] = True
    view["ext_keys"]["self_issued_active_count"] = 4
    records = install_api_mocks(page, system_settings=view)
    dialogs = _dialogs(page, accept=False)
    goto_admin_settings(page, web_base_url)
    open_tab(page, "extkeys")

    expect(page.locator("#ext-keys-user-allowed")).to_be_checked()
    page.locator("#ext-keys-user-allowed").uncheck()
    page.locator("#save").click()

    assert dialogs and "4 件" in dialogs[0] and "失効" in dialogs[0]
    expect(page.locator("#msg")).to_contain_text("保存を取り消しました")
    assert records["admin_settings_put"] == []


def test_user_api_keys_toggle_on_and_quota_default_save(page, web_base_url):
    view = _view()
    view["ext_keys"]["daily_quota_default"] = {"configured": 50, "effective": 50}
    records = _admin(page, web_base_url, tab="extkeys", system_settings=view)

    expect(page.locator("#ext-keys-user-allowed")).not_to_be_checked()
    expect(page.locator("#ext-keys-user-quota-default")).to_have_value("50")
    expect(page.locator("#ext-keys-user-quota-default-hint")).to_contain_text("この値で固定中")
    page.locator("#ext-keys-user-allowed").check()
    page.locator("#ext-keys-user-quota-default").fill("30")
    _save_ok(page)
    put = _last_put(records)
    assert put["user_api_keys_allowed"] is True
    assert put["user_api_keys_daily_quota_default"] == 30


def test_research_default_provider_renders_saves_and_dirty_by_value(page, web_base_url):
    """簡易回答に使う AI は render 時の値との差で dirty 判定する（戻すと丸印が消える）。"""
    view = _view()
    view["cloud"]["openai_key_set"] = True   # 保存時 preflight を通す
    records = _admin(page, web_base_url, tab="extkeys", system_settings=view)

    sel = page.locator("#ext-research-default-provider")
    expect(sel).to_have_value("ollama")
    hint = page.locator("#ext-research-default-provider-hint")
    expect(hint).to_contain_text("未設定")
    expect(hint).to_contain_text("ローカル（Ollama）")

    sel.select_option("openai")
    expect(page.locator("#tab-dot-extkeys")).to_be_visible()
    sel.select_option("ollama")
    expect(page.locator("#tab-dot-extkeys")).to_be_hidden()

    sel.select_option("openai")
    _save_ok(page)
    assert _last_put(records)["research_default_provider"] == "openai"
    expect(hint).to_contain_text("固定中")


def test_research_default_provider_invalid_saved_value_shown_not_rounded(page, web_base_url):
    """破損した保存値は黙って Ollama に丸めず、そのまま示して注意を出す。この状態の保存では何も送らず、
    選び直せば通常どおり保存でき、警告は消える。"""
    view = _view()
    view["ext_keys"]["research_default_provider"] = {
        "configured": "gemini", "effective": "(不正な保存値)", "default": "ollama"}
    records = _admin(page, web_base_url, tab="extkeys", system_settings=view)

    sel = page.locator("#ext-research-default-provider")
    expect(sel).to_have_value("__invalid__")
    expect(sel.locator("option:checked")).to_have_text("(不正な保存値)")
    expect(page.locator("#ext-research-default-provider-invalid")).to_be_visible()
    expect(page.locator("#ext-research-default-provider-invalid")).to_contain_text("正しくありません")

    _save_ok(page)
    assert "research_default_provider" not in _last_put(records)

    sel.select_option("ollama")
    _save_ok(page)
    assert _last_put(records)["research_default_provider"] == "ollama"
    expect(page.locator("#ext-research-default-provider-invalid")).to_be_hidden()


def test_put_rejected_by_research_provider_preflight_does_not_revoke_self_issued_keys(page, web_base_url):
    """中央 OpenAI キー未設定のまま簡易回答 AI を OpenAI にすると保存全体が 422 で拒否され、同じ PUT の
    「利用者のキー発行を許可」OFF による自己発行キーの失効も実行されない（部分的な副作用が残らない）。"""
    view = _view()
    view["ext_keys"]["user_api_keys_allowed"] = True
    view["ext_keys"]["self_issued_active_count"] = 1
    _admin(page, web_base_url, system_settings=view)
    page.evaluate("""() => fetch('/ext/v1/keys', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label: 'atomic-test-self-key'}),
    })""")
    page.reload()   # 一覧はページ読み込み時に1回だけ取得される
    open_tab(page, "extkeys")
    expect(page.locator("#ext-keys-list")).to_contain_text("atomic-test-self-key")
    expect(page.locator("#ext-keys-list")).to_contain_text("有効")

    page.locator("#ext-keys-user-allowed").uncheck()
    page.locator("#ext-research-default-provider").select_option("openai")
    page.once("dialog", lambda d: d.accept())
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("OpenAI にできません")
    expect(page.locator("#msg")).not_to_contain_text("保存しました")

    page.reload()   # 失敗時は一覧を再取得しないため、権威ある状態を取り直す
    open_tab(page, "extkeys")
    expect(page.locator("#ext-keys-user-allowed")).to_be_checked()
    expect(page.locator("#ext-keys-list")).to_contain_text("atomic-test-self-key")
    expect(page.locator("#ext-keys-list")).to_contain_text("有効")


# ===== 外部連携キー（管理画面・個人設定共通の発行モーダル） =====

def _ek_key_row(**overrides):
    row = {"id": 1, "key_prefix": "sk-ext-mock", "label": "既存キー", "created_by": "admin",
           "revoked_by": None, "allowed_worlds": None, "daily_quota": None, "owner_uid": None,
           "created_at": "2026-08-01T00:00:00+00:00", "revoked_at": None, "last_used_at": None,
           "expires_at": None, "call_count": 0}
    row.update(overrides)
    return row


def _ek_created(key, prefix, label, key_id=1):
    return json.dumps({"ok": True, "id": key_id, "key": key, "key_prefix": prefix, "label": label,
                       "created_at": "2026-08-25T00:00:00+00:00", "allowed_worlds": None,
                       "expires_at": None, "daily_quota": None})


class _Ek:
    """管理画面（admin）と個人設定（self）は同じ発行モーダルを持つ。差は API パスと記録キーだけ。"""

    def __init__(self, kind):
        self.admin = kind == "admin"
        self.path = "/ext/v1/admin/keys" if self.admin else "/ext/v1/keys"
        self.glob = "**/ext/v1/admin/keys" if self.admin else "**/ext/v1/keys"
        self.recover_glob = "**/ext/v1/admin/keys/recover" if self.admin else "**/ext/v1/keys/recover"
        self.create_rec = "ext_key_admin_create" if self.admin else "ext_key_self_create"
        self.put_rec = "admin_settings_put" if self.admin else "settings_put"
        self.held_key = "sk-ext-heldresp" if self.admin else "sk-ext-heldresp2"

    def start(self, page, web_base_url, route=None, recover=False):
        if self.admin:
            records = install_api_mocks(page)
        else:
            records = install_api_mocks(
                page, settings={**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True})
        if route:
            page.route(self.glob, route)
            if recover:
                page.route(self.recover_glob, route)
        if self.admin:
            goto_admin_settings(page, web_base_url)
            open_tab(page, "extkeys")
        else:
            page.goto(f"{web_base_url}/settings.html")
        return records

    def issue(self, page, label):
        page.locator("#ext-key-issue-open").click()
        page.locator("#ek-label").fill(label)
        page.locator("#ek-modal-submit").click()


@pytest.fixture(params=["admin", "self"])
def ek(request):
    return _Ek(request.param)


def _hold_post():
    pending = {}

    def hold(route):
        if route.request.method != "POST":
            route.fallback()
            return
        pending["route"] = route
    return pending, hold


def test_ext_key_issue_shows_plain_key_once(page, web_base_url, ek):
    """発行直後だけプレーンキーを表示し、閉じた後は DOM に残さない。一覧には反映される。"""
    records = ek.start(page, web_base_url)

    page.locator("#ext-key-issue-open").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")
    label = "新しい連携キー" if ek.admin else "私の連携キー"
    page.locator("#ek-label").fill(label)
    page.locator("#ek-quota").fill("100")
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-reveal")).to_be_visible()
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    expect(page.locator("#ek-issue-form")).to_be_hidden()
    assert records[ek.create_rec][-1]["label"] == label
    assert records[ek.create_rec][-1]["daily_quota"] == 100

    page.locator("#ek-modal-close").click()
    expect(page.locator("#ext-keys-list")).to_contain_text(label)
    expect(page.locator("#ek-reveal-key")).to_have_text("")


def test_ext_key_modal_cannot_close_while_issuing(page, web_base_url, ek):
    """応答待ちの間は ✕・キャンセルでも閉じられない（見せる前に閉じて有効キーだけ残る事故を防ぐ）。"""
    pending, hold = _hold_post()
    ek.start(page, web_base_url, route=hold)

    ek.issue(page, "保留中キー")
    expect(page.locator("#ek-modal-submit")).to_be_disabled()

    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")
    page.locator("#ek-modal-cancel").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")

    pending["route"].fulfill(status=200, content_type="application/json",
                             body=_ek_created(ek.held_key, "sk-ext-hel", "保留中キー", 99))
    expect(page.locator("#ek-reveal-key")).to_contain_text(ek.held_key)
    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).not_to_have_class("overlay open")
    expect(page.locator("#ek-reveal-key")).to_have_text("")


def test_ext_key_concurrent_close_reissue_survives_slow_list_refresh(page, web_base_url, ek):
    """1本目発行直後の一覧再取得が遅れる間に2本目が先に新しい一覧を描画したら、後から届く古い世代の
    一覧応答で巻き戻さない（世代番号ガード）。"""
    get_calls = {"n": 0}
    held = {}

    def handler(route):
        if route.request.method == "GET":
            get_calls["n"] += 1
            if get_calls["n"] == 2:
                held["route"] = route   # 1本目発行成功直後の一覧取得だけ保留する
                return
        route.fallback()
    records = ek.start(page, web_base_url, route=handler)

    ek.issue(page, "1本目")
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    page.locator("#ek-modal-close").click()
    ek.issue(page, "2本目")
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    expect(page.locator("#ext-keys-list")).to_contain_text("2本目")
    expect(page.locator("#ext-keys-list")).to_contain_text("1本目")

    assert "route" in held
    held["route"].fulfill(status=200, content_type="application/json", body=json.dumps(
        {"keys": [_ek_key_row(id=1, key_prefix="sk-ext-mock0001", label="1本目")]}))
    page.wait_for_timeout(50)
    expect(page.locator("#ext-keys-list")).to_contain_text("2本目")
    expect(page.locator("#ext-keys-list")).to_contain_text("1本目")
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    expect(page.locator("#ek-modal-submit")).to_be_hidden()
    expect(page.locator("#ek-issue-err")).to_have_text("")
    assert len(records[ek.create_rec]) == 2


_RECOVER_NOT_FOUND = {"found": False, "id": None, "revoked_at": None}
_ISSUE_ERR_FORMS = [
    # kind, POST の振る舞い, 回復応答, 仮想時間の進め方, 期待表示, 出てはいけない表示, ラベル
    ("admin", "hang", {"found": True, "id": 55, "revoked_at": "2026-08-25T00:00:00+00:00"},
     [31000], "失効しました", None, "孤児化候補"),
    ("self", "hang", {"found": True, "id": 66, "revoked_at": "2026-08-25T00:00:00+00:00"},
     [31000], "失効しました", None, "孤児化候補"),
    ("admin", "hang", _RECOVER_NOT_FOUND, [31000, 2000, 2000], "失敗した可能性", "失効しました",
     "届いていない候補"),
    ("self", "hang", _RECOVER_NOT_FOUND, [31000, 2000, 2000], "失敗した可能性", "失効しました",
     "届いていない候補"),
    ("admin", "hang", {"found": "yes"}, [31000, 2000, 2000], "確認できませんでした", "失効しました",
     "型崩れ候補"),
    ("self", "hang", {"found": "yes"}, [31000, 2000, 2000], "確認できませんでした", "失効しました",
     "型崩れ候補"),
    ("admin", "html502", _RECOVER_NOT_FOUND, [2000, 2000], "失敗した可能性", "エラー (502)", "502候補"),
    ("admin", "abort", _RECOVER_NOT_FOUND, [2000, 2000], "失敗した可能性", None, "通信断候補"),
]


@pytest.mark.parametrize("kind,post_mode,recover_body,clock_steps,shown,not_shown,label", _ISSUE_ERR_FORMS,
                         ids=["admin-recovered", "self-recovered", "admin-no-match", "self-no-match",
                              "admin-malformed", "self-malformed", "admin-502-html", "admin-abort"])
def test_ext_key_issue_ambiguous_failure_goes_through_recovery(page, web_base_url, kind, post_mode,
                                                               recover_body, clock_steps, shown, not_shown,
                                                               label):
    """発行 POST が応答しない・本文が JSON でない 5xx・通信断はどれも「曖昧な失敗」として回復専用
    エンドポイントへ client_op_id を渡して照合する。失効を確認できたときだけその旨を出し、確認できない・
    型崩れ・該当なしは成功に見せず失敗として確定し、いずれもボタンが復帰する。"""
    ek = _Ek(kind)
    captured = {}

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith(ek.path):
            if post_mode == "hang":
                captured["body"] = json.loads(route.request.post_data)
                return
            if post_mode == "html502":
                route.fulfill(status=502, content_type="text/html",
                              body="<html><body>Bad Gateway</body></html>")
                return
            route.abort("connectionreset")
            return
        if route.request.method == "POST" and url.endswith(ek.path + "/recover"):
            if recover_body.get("found") is True:
                req = json.loads(route.request.post_data)
                if req.get("client_op_id") != captured.get("body", {}).get("client_op_id"):
                    route.fallback()
                    return
            route.fulfill(status=200, content_type="application/json", body=json.dumps(recover_body))
            return
        route.fallback()

    ek.start(page, web_base_url, route=handler, recover=True)
    page.clock.install()
    ek.issue(page, label)
    if post_mode == "hang":
        expect(page.locator("#ek-modal-submit")).to_be_disabled()
    for step in clock_steps:
        page.clock.fast_forward(step)

    expect(page.locator("#ek-issue-err")).to_contain_text(shown)
    if not_shown:
        expect(page.locator("#ek-issue-err")).not_to_contain_text(not_shown)
    expect(page.locator("#ek-modal-submit")).to_be_enabled()
    if recover_body.get("found") is True:
        page.locator("#ek-modal-close").click()   # issuing の永久ロックが解消され、閉じる操作も効く
        expect(page.locator("#ek-overlay")).not_to_have_class("overlay open")


def test_ext_key_issue_body_stall_after_headers_treated_as_ambiguous(page, web_base_url):
    """ヘッダは届くが本文の読み取りだけが詰まる場合も、締切が効いて曖昧な結果として回復導線へ入る。"""
    ek = _Ek("admin")
    ek.start(page, web_base_url)
    page.evaluate("""() => {
      const orig = Response.prototype.json;
      Response.prototype.json = function () {
        if (this.url && this.url.includes('/ext/v1/admin/keys') && !this.url.includes('recover')) {
          return new Promise(() => {});
        }
        return orig.call(this);
      };
    }""")
    page.clock.install()

    ek.issue(page, "stall候補")
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    page.clock.fast_forward(31000)
    expect(page.locator("#ek-issue-err")).to_contain_text("失効しました")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_ext_key_issue_valid_json_4xx_is_confirmed_error_not_ambiguous(page, web_base_url):
    """妥当な JSON を持つ非 2xx は確定的なエラーとしてそのまま表示し、回復導線には入らない。"""
    def handler(route):
        if route.request.method == "POST" and route.request.url.endswith("/ext/v1/admin/keys"):
            route.fulfill(status=422, content_type="application/json",
                          body=json.dumps({"detail": "許可されていない world です"}))
            return
        route.fallback()
    _Ek("admin").start(page, web_base_url, route=handler)

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("422候補")
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-issue-err")).to_have_text("許可されていない world です")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_ext_key_modal_direct_reentry_blocked_during_issuing(page, web_base_url):
    """issuing 中に openExtKeyModal()/submitExtKeyIssue() を直接呼んでも二重の POST は飛ばない
    （内部ガードはボタンの見た目に依存しない）。"""
    post_count = {"n": 0}
    pending = {}

    def handler(route):
        if route.request.method == "POST" and route.request.url.endswith("/ext/v1/admin/keys"):
            post_count["n"] += 1
            pending["route"] = route
            return
        route.fallback()
    ek = _Ek("admin")
    ek.start(page, web_base_url, route=handler)

    ek.issue(page, "再入テスト")
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    assert post_count["n"] == 1

    page.evaluate("openExtKeyModal()")
    page.evaluate("submitExtKeyIssue()")
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    assert post_count["n"] == 1, "issuing 中の直接再入で2本目の POST が飛んだ"

    assert "route" in pending
    pending["route"].fulfill(status=200, content_type="application/json",
                             body=_ek_created("sk-ext-mockreentry", "sk-ext-mockre", "再入テスト"))
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mockreentry")
    assert post_count["n"] == 1


def test_ext_key_copy_failure_shows_error_not_success(page, web_base_url):
    ek = _Ek("admin")
    ek.start(page, web_base_url)
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: () => Promise.reject(new Error('denied')) }, configurable: true });
      document.execCommand = () => false;
    }""")

    ek.issue(page, "コピー失敗テスト")
    expect(page.locator("#ek-reveal")).to_be_visible()
    page.locator("#ek-copy").click()
    expect(page.locator("#ek-copy-res")).to_have_text("✗ コピーできませんでした（選択してコピーしてください）")


def test_ext_key_list_renders_columns(page, web_base_url):
    """一覧（ラベル・prefix・world スコープ・呼び出し数・状態）が描画される。"""
    ek_calls = {"list": 0}

    def handler(route):
        if route.request.method == "GET" and route.request.url.endswith("/ext/v1/admin/keys"):
            ek_calls["list"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"keys": [
                _ek_key_row(id=1, label="Dify連携", key_prefix="sk-ext-abc1", allowed_worlds=["test"],
                            call_count=42, last_used_at="2026-08-20T09:00:00+00:00"),
                _ek_key_row(id=2, label="失効済みキー", key_prefix="sk-ext-old1",
                            revoked_at="2026-08-10T00:00:00+00:00", revoked_by="admin"),
            ]}))
            return
        route.fallback()
    _Ek("admin").start(page, web_base_url, route=handler)

    for text in ("Dify連携", "sk-ext-abc1", "test", "42", "失効済みキー", "失効済み"):
        expect(page.locator("#ext-keys-list")).to_contain_text(text)
    assert ek_calls["list"] >= 1


@pytest.mark.parametrize("kind,key_id,label,revoke_btn", [
    ("admin", 7, "削除対象", "[data-ek-revoke='7']"), ("self", 3, "自分のキー", "[data-ek-revoke='3']")])
def test_ext_key_revoke_asks_confirm_and_calls_delete(page, web_base_url, kind, key_id, label, revoke_btn):
    ek = _Ek(kind)

    def handler(route):
        if route.request.method == "GET" and route.request.url.endswith(ek.path):
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"keys": [
                _ek_key_row(id=key_id, label=label, key_prefix="sk-ext-mine",
                            created_by="u1", owner_uid="u1", call_count=1)]}))
            return
        route.fallback()
    dialogs = _dialogs(page, accept=True)
    records = ek.start(page, web_base_url, route=handler)

    expect(page.locator("#ext-keys-list")).to_contain_text(label)
    page.locator(revoke_btn).click()

    assert dialogs and label in dialogs[0]
    assert records["ext_key_admin_revoke" if ek.admin else "ext_key_self_revoke"] == [key_id]


def test_ext_key_modal_inert_blocks_background_keyboard_interaction(page, web_base_url, ek):
    """モーダル中は背後（.wrap）が inert になり、Tab でも背後へフォーカスが移らない（aria-modal 宣言つき）。"""
    ek.start(page, web_base_url)

    expect(page.locator("#ek-overlay .modal[role='dialog']")).to_have_attribute("aria-modal", "true")
    page.locator("#ext-key-issue-open").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")
    expect(page.locator(".wrap")).to_have_attribute("inert", "")

    for _ in range(15):
        page.keyboard.press("Tab")
    focused_in_modal = page.evaluate(
        "document.activeElement && !!document.activeElement.closest('#ek-overlay')")
    assert focused_in_modal, "Tab移動でフォーカスがモーダルの外（inert な背後）へ出た"

    page.locator("#ek-modal-close").click()
    expect(page.locator(".wrap")).not_to_have_attribute("inert", "")


def test_ctrl_s_during_key_modal_does_not_save_and_is_default_prevented(page, web_base_url, ek):
    """キー発行モーダルが開いている間（入力中・応答待ち・キー表示中）は Ctrl+S で PUT しない
    （既定動作は実際に preventDefault される）。閉じれば通常どおり保存できる。"""
    pending, hold = _hold_post()
    records = ek.start(page, web_base_url)

    page.locator("#ext-key-issue-open").click()
    default_prevented = page.evaluate("""() => {
      const ev = new KeyboardEvent('keydown', {
        key: 's', ctrlKey: true, cancelable: true, bubbles: true });
      document.dispatchEvent(ev);
      return ev.defaultPrevented;
    }""")
    assert default_prevented is True
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records[ek.put_rec]) == 0

    page.route(ek.glob, hold)
    page.locator("#ek-label").fill("ctrls候補")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records[ek.put_rec]) == 0

    pending["route"].fulfill(status=200, content_type="application/json",
                             body=_ek_created("sk-ext-mockctrls", "sk-ext-mockct", "ctrls候補"))
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mockctrls")
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records[ek.put_rec]) == 0

    page.locator("#ek-modal-close").click()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records[ek.put_rec]) == 1


def test_ext_key_focus_moves_to_copy_on_success_and_back_to_opener_on_close(page, web_base_url, ek):
    ek.start(page, web_base_url)

    open_btn = page.locator("#ext-key-issue-open")
    ek.issue(page, "フォーカステスト")
    expect(page.locator("#ek-copy")).to_be_focused()

    page.locator("#ek-modal-close").click()
    expect(open_btn).to_be_focused()


def test_ext_key_expires_date_is_inclusive_and_min_blocks_past(page, web_base_url, ek):
    """日付は選択日を含めて有効（翌日0時 JST に失効）へ変換して送る。min 属性は当日。手入力の過去日は
    送信前のクライアント検査が POST 自体を止める。日付は実行日からの相対で導出する。"""
    from datetime import datetime, timedelta, timezone

    records = ek.start(page, web_base_url)

    # ブラウザは JST 固定（conftest）。Python 側もホスト TZ に関わらず UTC+9 で「今日」を求める。
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    future = today + timedelta(days=7)
    past = today - timedelta(days=1)

    page.locator("#ext-key-issue-open").click()
    assert page.locator("#ek-expires").get_attribute("min") == today.isoformat()

    page.locator("#ek-label").fill("期限つきキー")
    page.locator("#ek-expires").fill(past.isoformat())
    before_creates = len(records[ek.create_rec])
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-issue-err")).to_contain_text("今日以降")
    assert len(records[ek.create_rec]) == before_creates

    page.locator("#ek-expires").fill(future.isoformat())
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    assert records[ek.create_rec][-1]["expires_at"] == f"{future.isoformat()}T15:00:00.000Z"


@pytest.mark.parametrize("prefix,is_self", [("/ext/v1/admin/keys", False), ("/ext/v1/keys", True)],
                         ids=["admin", "self"])
def test_ext_key_mock_case_insensitive_full_contract(page, web_base_url, prefix, is_self):
    """モックの client_op_id 大小文字非区別契約: 大文字で発行→応答は小文字の正準形、大小文字違いの再発行は
    409、異なる表記での回復照会は一致する。"""
    _Ek("self" if is_self else "admin").start(page, web_base_url)

    result = page.evaluate("""async (prefix) => {
      const post = (body) => fetch(prefix, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      }).then(async (r) => ({ status: r.status, body: await r.json() }));

      const opId = crypto.randomUUID();
      const created = await post({ label: 'case-full', client_op_id: opId.toUpperCase() });
      const reissue = await post({ label: 'case-full-2', client_op_id: opId.toLowerCase() });
      const recovered = await fetch(prefix + '/recover', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ client_op_id: opId.toUpperCase() }),
      }).then((r) => r.json());

      return { opId, created, reissue, recovered };
    }""", prefix)

    assert result["created"]["status"] == 200, result["created"]
    assert result["created"]["body"]["client_op_id"] == result["opId"].lower()
    assert result["reissue"]["status"] == 409, result["reissue"]
    assert result["recovered"]["found"] is True
    assert result["recovered"]["id"] == result["created"]["body"]["id"]


# ===== モックの契約（実サーバと同じ検証を返すことの固定） =====

def test_mock_openai_endpoint_pending_inherits_saved_base_url_only_when_present():
    """kind だけの body は、保存済み base_url があれば引き継いでクロス検証を通り、無ければ拒否される。"""
    configured = {"kind": "azure", "base_url": "https://res.openai.azure.com",
                  "auth_header": "bearer", "api_version": None}
    pending = mock_api._mock_openai_endpoint_pending(configured, {"openai_endpoint_kind": "azure"})
    assert pending["openai_base_url"] == "https://res.openai.azure.com"
    kind = mock_api._mock_infer_openai_endpoint_kind(
        pending["openai_endpoint_kind"], pending["openai_base_url"] or "")
    assert mock_api._mock_validate_openai_endpoint_cross(kind, pending["openai_base_url"] or "") is None

    configured = {"kind": None, "base_url": None, "auth_header": None, "api_version": None}
    pending = mock_api._mock_openai_endpoint_pending(configured, {"openai_endpoint_kind": "azure"})
    kind = mock_api._mock_infer_openai_endpoint_kind(
        pending["openai_endpoint_kind"], pending["openai_base_url"] or "")
    assert mock_api._mock_validate_openai_endpoint_cross(kind, pending["openai_base_url"] or "") is not None


def test_mock_validate_depth_base_matches_real_api_ranges_and_error_shape():
    """負値・0・上限+1は拒否、境界値と null は受理。整数項目のエラーは実 API の pydantic detail と同形
    （リスト・loc/type/ctx）、語彙違反は文字列。codex 推論は strip().lower() で正規化して受理する。"""
    v = mock_api._mock_validate_depth_base
    for body in ({"depth_base_grep_max_hits": -1}, {"depth_base_grep_max_hits": 0},
                 {"depth_base_grep_max_hits": 1001}, {"depth_base_read_window": 9},
                 {"depth_base_read_window": 401}, {"depth_base_troubleshoot_depth": 17}):
        assert v(body) is not None, body
    for body in ({"depth_base_grep_max_hits": 1}, {"depth_base_grep_max_hits": 1000},
                 {"depth_base_read_window": 10}, {"depth_base_read_window": 400},
                 {"depth_base_grep_max_hits": None}):
        assert v(body) is None, body

    err = v({"depth_base_grep_max_hits": 0})
    assert isinstance(err, list) and len(err) == 1
    assert err[0]["type"] == "greater_than_equal"
    assert err[0]["loc"] == ["body", "depth_base_grep_max_hits"]
    assert err[0]["ctx"] == {"ge": 1}
    err_hi = v({"depth_base_grep_max_hits": 5000})
    assert err_hi[0]["type"] == "less_than_equal" and err_hi[0]["ctx"] == {"le": 1000}
    assert v({"depth_base_grep_max_hits": "twelve"})[0]["type"] == "int_type"
    assert isinstance(v({"depth_base_codex_reasoning": "very-high"}), str)

    for ok in ("xhigh", "High", " HIGH ", None):
        assert v({"depth_base_codex_reasoning": ok}) is None, ok
    assert v({"depth_base_codex_reasoning": 123}) is not None


def test_mock_validate_chat_max_turns_rejects_out_of_range_and_accepts_boundary_and_null():
    v = mock_api._mock_validate_chat_max_turns
    for body in ({"chat_max_turns_per_user": -1}, {"chat_max_turns_per_user": 0},
                 {"chat_max_turns_per_user": 17}, {"chat_max_turns_global": 65}):
        assert v(body) is not None, body
    for body in ({"chat_max_turns_per_user": 1}, {"chat_max_turns_per_user": 16},
                 {"chat_max_turns_global": 1}, {"chat_max_turns_global": 64},
                 {"chat_max_turns_per_user": None}):
        assert v(body) is None, body


def test_mock_admin_put_normalizes_codex_reasoning_and_returns_422_for_out_of_range(page, web_base_url):
    """UI の select は語彙しか選べず検証も止めるため、fetch で直接 PUT して mock の防御を固定する:
    " HIGH " は正規化後 "high" で保存され、範囲外の depth_base_* は 422 で state を変えない。"""
    records = _admin(page, web_base_url, tab="research")

    status = page.evaluate("""
        async () => {
          const res = await fetch('/admin/settings', {
            method: 'PUT', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ depth_base_codex_reasoning: ' HIGH ' }),
          });
          return res.status;
        }
    """)
    assert status == 200
    assert _last_put(records).get("depth_base_codex_reasoning") == " HIGH "   # 送信値は生のまま記録
    status = page.evaluate("""
        async () => {
          const res = await fetch('/admin/settings', {
            method: 'PUT', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ depth_base_grep_max_hits: 0 }),
          });
          return res.status;
        }
    """)
    assert status == 422
    page.reload()
    expect(page.locator("#depth-base-codex-reasoning")).to_have_value("high")
    expect(page.locator("#depth-base-grep-max-hits")).to_have_value("")
