"""管理画面の取り込みタブの OCR スイッチ（`web/admin-settings-ocr.js`）。"""
from __future__ import annotations

from test_admin_settings_ui import _admin, _last_put, _save_ok, _view, expect


def test_ocr_switch_defaults_to_off_and_saves_only_when_touched(page, web_base_url):
    records = _admin(page, web_base_url, tab="ingest")

    card = page.locator("#ocr-card")
    expect(card).to_be_visible()
    expect(card).to_contain_text("止めても、これまでに読み取った文字は残ります")
    expect(page.locator("#ocr-enabled")).not_to_be_checked()
    expect(page.locator("#ocr-enabled-status")).to_contain_text("既定に従っています")
    expect(page.locator("#tab-dot-ingest")).to_be_hidden()

    page.locator("#ocr-enabled").check()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()
    _save_ok(page)
    assert _last_put(records)["ocr_enabled"] is True
    expect(page.locator("#msg")).to_contain_text("今すぐ更新")


def test_ocr_switch_is_locked_while_environment_variable_decides(page, web_base_url):
    view = _view()
    view["ocr"] = {"configured": True, "effective": False, "default": False, "env_override": False}
    _admin(page, web_base_url, tab="ingest", system_settings=view)

    expect(page.locator("#ocr-enabled")).not_to_be_checked()
    expect(page.locator("#ocr-enabled")).to_be_disabled()
    expect(page.locator("#ocr-enabled-status")).to_contain_text("環境変数")
