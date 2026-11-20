"""管理画面「Codex 調査の既定の接続先」（自動／OpenAI／Ollama）の描画・保存・不正な保存値。"""
from __future__ import annotations

from test_admin_settings_ui import _admin, _last_put, _save_ok, _view, expect


def _view_with(configured, effective):
    view = _view()
    view["ext_keys"]["codex_default_model_provider"] = {
        "configured": configured, "effective": effective, "default": "auto"}
    return view


def test_codex_default_provider_renders_dirty_by_value_and_saves(page, web_base_url):
    records = _admin(page, web_base_url, tab="research", system_settings=_view())
    sel = page.locator("#ext-codex-default-provider")
    expect(sel).to_have_value("")
    expect(page.locator("#ext-codex-default-provider-hint")).to_contain_text("自動で決めています")

    sel.select_option("ollama")
    expect(page.locator("#tab-dot-research")).to_be_visible()
    sel.select_option("")
    expect(page.locator("#tab-dot-research")).to_be_hidden()

    sel.select_option("ollama")
    _save_ok(page)
    assert _last_put(records)["codex_default_model_provider"] == "ollama"


def test_codex_default_provider_auto_sends_null(page, web_base_url):
    records = _admin(page, web_base_url, tab="research", system_settings=_view_with("openai", "openai"))
    sel = page.locator("#ext-codex-default-provider")
    expect(sel).to_have_value("openai")
    expect(page.locator("#ext-codex-default-provider-hint")).to_contain_text("OpenAI で固定中")
    sel.select_option("")
    _save_ok(page)
    assert _last_put(records)["codex_default_model_provider"] is None


def test_codex_default_provider_invalid_saved_value_shown_not_rounded(page, web_base_url):
    records = _admin(page, web_base_url, tab="research", system_settings=_view_with("gemini", "(不正な保存値)"))
    sel = page.locator("#ext-codex-default-provider")
    expect(sel).to_have_value("__invalid__")
    expect(page.locator("#ext-codex-default-provider-invalid")).to_contain_text("正しくありません")
    _save_ok(page)
    assert "codex_default_model_provider" not in _last_put(records)
    sel.select_option("openai")
    _save_ok(page)
    assert _last_put(records)["codex_default_model_provider"] == "openai"
