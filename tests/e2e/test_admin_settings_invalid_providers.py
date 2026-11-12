"""保存されている埋め込み・視覚読み取りの接続先が不正なときだけ、選び直しの案内を出す。"""
from __future__ import annotations

import json

from _admin_page import goto_admin_settings
from mock_api import SYSTEM_SETTINGS_VIEW, install_api_mocks


def expect(*args):
    from playwright.sync_api import expect as _expect
    return _expect(*args)


def test_invalid_saved_providers_show_reselect_warning(page, web_base_url):
    view = json.loads(json.dumps(SYSTEM_SETTINGS_VIEW))
    view["embed_provider"]["invalid"] = True
    view["vlm"]["invalid"] = True
    install_api_mocks(page, system_settings=view)
    goto_admin_settings(page, web_base_url)
    warn = "保存されている接続先の設定が正しくありません。接続先を選び直してください。"
    expect(page.locator("#embed-provider-invalid")).to_have_text(warn)
    expect(page.locator("#vlm-provider-invalid")).to_have_text(warn)


def test_valid_saved_providers_show_no_reselect_warning(page, web_base_url):
    install_api_mocks(page)
    goto_admin_settings(page, web_base_url)
    expect(page.locator("#embed-provider-invalid")).to_be_hidden()
    expect(page.locator("#vlm-provider-invalid")).to_be_hidden()
