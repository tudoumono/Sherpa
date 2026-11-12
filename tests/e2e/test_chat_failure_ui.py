"""チャット画面の失敗の見せ方（範囲の取得・停止の要求）の e2e。"""
from __future__ import annotations

import re
import time

from mock_api import install_api_mocks
from playwright.sync_api import expect

Q = "消費税率を変えたい。影響は？"


def _wait_until(predicate, page, timeout_ms=5000, message="条件が満たされなかった"):
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        assert time.monotonic() < deadline, message
        page.wait_for_timeout(20)


def test_chat_scope_load_failure_is_shown_in_scope_row(page, web_base_url):
    """参照範囲の取得に失敗したら範囲の欄を消さず、読み込めなかったことと全体を対象にすることを出す。"""
    install_api_mocks(page)
    page.route("**/scopes?**", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"detail":"down"}'))
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#scopelabel")).to_have_text("範囲を読み込めません")
    page.locator("#scopebtn").click()
    expect(page.locator("#scopepanel")).to_contain_text("参照範囲を読み込めませんでした")
    expect(page.locator("#scopepanel")).to_contain_text("全体を対象に調べます")   # 前の選択が無いときは全体で送る
    expect(page.locator("#scopepanel [data-scope-retry]")).to_be_visible()


def test_chat_stop_request_failure_keeps_turn_and_allows_retry(page, web_base_url):
    """停止の要求が失敗したら「待機中」に戻さず、停止を確認できていないことを出して、もう一度停止できる。"""
    install_api_mocks(page)
    pending: dict = {}
    stops: list = []

    def handle_stop(route):
        stops.append(route)
        route.fulfill(status=500, content_type="application/json", body='{"detail":"down"}')

    page.route("**/chat/turns/*/stream?**", lambda route: pending.__setitem__("route", route))
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill(Q)
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page, message="stream の GET が届かなかった")
    send_btn = page.locator("#send")
    send_btn.click()

    expect(page.locator("#rt")).to_contain_text("停止を確認できませんでした")
    expect(page.locator("#rt")).not_to_contain_text("待機中")
    expect(send_btn).to_have_class(re.compile(r"\bstopping\b"))
    expect(send_btn).to_be_enabled()
    send_btn.click()
    _wait_until(lambda: len(stops) >= 2, page, message="2回目の停止 POST が届かなかった")
