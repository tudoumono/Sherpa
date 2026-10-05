"""管理設定の画面を開いて、初回読込の描画が終わるまで待つ（描画が入力欄を空にするので、入力はその後に行う）。"""
from playwright.sync_api import expect


def goto_admin_settings(page, web_base_url, suffix=""):
    page.goto(f"{web_base_url}/admin-settings.html{suffix}")
    expect(page.locator("#cloud-key-label")).to_contain_text("API キー")
