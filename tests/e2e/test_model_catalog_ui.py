"""モデルカタログの e2e。管理画面の「使えるモデル」表・Ollama 許可ホスト一覧と、個人設定の Ollama 接続先選択。"""
from __future__ import annotations

import mock_api
import pytest
from mock_api import SYSTEM_SETTINGS_VIEW, install_api_mocks
from _admin_page import goto_admin_settings
from playwright.sync_api import expect

CHAT_SEL = "select.mc-default[data-provider='openai'][data-usage='chat']"
OLLAMA_CHAT_SEL = "select.mc-default[data-provider='ollama'][data-usage='chat']"


def _open_models(page, web_base_url, **mock_kw):
    records = install_api_mocks(page, **mock_kw)
    goto_admin_settings(page, web_base_url)
    page.locator('.tab-btn[data-tab="models"]').click()
    return records


def _save(page):
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")


def _catalog(**overrides):
    return {**SYSTEM_SETTINGS_VIEW, "model_catalog": {**SYSTEM_SETTINGS_VIEW["model_catalog"], **overrides}}


def _effective(**providers):
    base = SYSTEM_SETTINGS_VIEW["model_catalog"]["effective"]
    return {**base, **{k: {**base[k], **v} for k, v in providers.items()}}


def _pinned_openai_chat_and_ollama_candidate():
    """openai/chat が組み込み既定と同値（gpt-5.5）で明示保存済み・ollama/chat に候補を1つ足した設定。"""
    return _catalog(
        configured={"openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"}}},
        effective=_effective(ollama={"chat": {"allowed": ["qwen2.5", "llama3.1"], "default": "qwen2.5"}}),
    )


# ===== 管理画面（admin-settings.html） =====

def test_admin_model_catalog_table_and_default_save(page, web_base_url):
    records = _open_models(page, web_base_url)   # 既定モックの cloud.provider は "openai"

    table = page.locator("#model-catalog-table")
    expect(table.locator("th")).to_have_count(4)   # 用途 + 3列（openai/ollama/codex）
    expect(table).to_contain_text("OpenAI")
    expect(table).to_contain_text("ローカル（Ollama）")
    expect(table).to_contain_text("Codex")
    expect(table).to_contain_text("チャット")
    sel = table.locator(CHAT_SEL)
    expect(sel).to_have_value("gpt-5.5")

    sel.select_option("gpt-5.4-mini")
    _save(page)
    put = records["admin_settings_put"][-1]
    assert put["model_catalog"]["openai"]["chat"]["default"] == "gpt-5.4-mini"
    assert "gpt-5.4-mini" in put["model_catalog"]["openai"]["chat"]["allowed"]


def test_admin_model_catalog_empty_default_shows_explicit_placeholder_not_first_option(page, web_base_url):
    """セルの既定が空（未設定）のとき、先頭の実モデル名が選択済みに見えてはならない（誤認の回帰）。
    明示的な「（未設定）」が選ばれていること。"""
    settings = _catalog(effective=_effective(openai={"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"],
                                                              "default": ""}}))
    _open_models(page, web_base_url, system_settings=settings)

    sel = page.locator(CHAT_SEL)
    expect(sel).to_have_value("")
    expect(sel.locator("option", has_text="（未設定）")).to_have_count(1)


def test_admin_model_catalog_cell_highlight_reflects_value_diff_not_configured_presence(page, web_base_url):
    """セルの強調は「configured にセルが存在する」ではなく「組み込み既定と値が異なる」で判定する。"""
    settings = _catalog(
        configured={"openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"}}},
    )
    _open_models(page, web_base_url, system_settings=settings)

    chat_cell = page.locator(f"td:has({CHAT_SEL})")
    expect(chat_cell).not_to_have_class("mc-changed")

    page.locator(CHAT_SEL).select_option("gpt-5.4-mini")
    expect(chat_cell).to_have_class("mc-changed")


def test_admin_model_catalog_edit_allowed_list_via_modal(page, web_base_url):
    """「一覧を編集」→ モーダルへ複数行入力 → 反映 → 保存で PUT body に載る。"""
    records = _open_models(page, web_base_url)

    page.locator("button.mc-edit[data-provider='ollama'][data-usage='chat']").click()
    overlay = page.locator("#mc-overlay")
    expect(overlay).to_have_class("overlay open")
    expect(page.locator("#mc-modal-title")).to_contain_text("チャット")

    page.locator("#mc-modal-textarea").fill("qwen2.5\nllama3.1")
    page.locator("#mc-modal-save").click()
    expect(overlay).not_to_have_class("overlay open")
    expect(page.locator(OLLAMA_CHAT_SEL).locator("option")).to_have_count(2)

    _save(page)
    put = records["admin_settings_put"][-1]
    assert sorted(put["model_catalog"]["ollama"]["chat"]["allowed"]) == ["llama3.1", "qwen2.5"]


def test_admin_model_catalog_reorder_only_is_sent_and_order_preserved(page, web_base_url):
    """`allowed` の並び順は契約。並べ替えだけ（集合・既定は不変）でも差分として送信され、ソートされずに保持される。"""
    records = _open_models(page, web_base_url)

    page.locator("button.mc-edit[data-provider='openai'][data-usage='chat']").click()
    ta = page.locator("#mc-modal-textarea")
    expect(ta).to_have_value("gpt-5.5\ngpt-5.4-mini")
    ta.fill("gpt-5.4-mini\ngpt-5.5")   # 既定 gpt-5.5 は変えない
    page.locator("#mc-modal-save").click()

    _save(page)
    cell = records["admin_settings_put"][-1]["model_catalog"]["openai"]["chat"]
    assert cell["allowed"] == ["gpt-5.4-mini", "gpt-5.5"]
    assert cell["default"] == "gpt-5.5"


@pytest.mark.parametrize("touch", ["change_and_revert", "modal_reflect_without_change"])
def test_admin_model_catalog_pin_preserved(page, web_base_url, touch):
    """組み込み既定と同値で明示保存済みのセルは、別候補へ変えて元の値へ戻しても・一覧編集モーダルを内容変更なしで
    『反映』しても、PUT body に残る。"""
    records = _open_models(page, web_base_url, system_settings=_pinned_openai_chat_and_ollama_candidate())

    if touch == "change_and_revert":
        sel = page.locator(CHAT_SEL)
        sel.select_option("gpt-5.4-mini")
        sel.select_option("gpt-5.5")
    else:
        page.locator("button.mc-edit[data-provider='openai'][data-usage='chat']").click()
        page.locator("#mc-modal-save").click()
    page.locator(OLLAMA_CHAT_SEL).select_option("llama3.1")

    _save(page)
    put = records["admin_settings_put"][-1]["model_catalog"]
    assert put["openai"]["chat"]["default"] == "gpt-5.5"   # pin は残る
    assert put["ollama"]["chat"]["default"] == "llama3.1"


def test_admin_model_catalog_untouched_not_sent(page, web_base_url):
    """表を一切触らずに保存すると model_catalog は PUT body に含まれない。"""
    records = _open_models(page, web_base_url)
    _save(page)
    assert "model_catalog" not in records["admin_settings_put"][-1]


def test_admin_ollama_allowlist_textarea_saves(page, web_base_url):
    records = install_api_mocks(page)
    goto_admin_settings(page, web_base_url)
    page.locator('#tabpanel-provider details.adv summary').click()   # 許可ホスト一覧は「詳細」の中

    page.locator("#cloud-ollama-allowlist").fill("10.0.0.5:11434\n10.0.0.6:11434")
    _save(page)
    put = records["admin_settings_put"][-1]
    assert sorted(put["ollama_allowlist"]) == ["10.0.0.5:11434", "10.0.0.6:11434"]


# ===== 個人設定（settings.html） =====

def _open_settings(page, web_base_url, ollama_url, allowed):
    settings = {**mock_api.SETTINGS_RESP, "ollama_url": ollama_url,
                "ollama_url_choice": {"allowed": allowed, "default": "http://localhost:11434"}}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")
    return records


def test_settings_ollama_url_select_from_allowed_hosts_and_saves(page, web_base_url):
    """許可ホスト一覧（完全 URL）から選ぶ。scheme 込みの完全 URL のまま保存される（HTTPS が HTTP に化けない）。"""
    records = _open_settings(page, web_base_url, "http://localhost:11434",
                             ["http://localhost:11434", "https://ollama.lan:8443"])

    ourl = page.locator("#ourl")
    values = ourl.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert values == ["", "http://localhost:11434", "https://ollama.lan:8443"]   # 先頭は「管理者の既定を使う」
    expect(ourl).to_have_value("http://localhost:11434")
    expect(page.locator("#ourl-warn")).to_be_hidden()

    ourl.select_option("https://ollama.lan:8443")
    _save(page)
    assert records["settings_put"][-1]["ollama_url"] == "https://ollama.lan:8443"


def test_settings_ollama_url_select_clear_to_admin_default_sends_empty_string(page, web_base_url):
    """「管理者の既定を使う」を選んで保存すると一覧外の既存値をクリアできる（null でなく空文字を送る）。"""
    records = _open_settings(page, web_base_url, "http://legacy-unlisted:11434", ["http://localhost:11434"])

    ourl = page.locator("#ourl")
    expect(ourl).to_have_value("http://legacy-unlisted:11434")
    expect(page.locator("#ourl-warn")).to_be_visible()

    ourl.select_option("")
    _save(page)
    assert records["settings_put"][-1]["ollama_url"] == ""
