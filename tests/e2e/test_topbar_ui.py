"""全ページ共通トップバー（nav.js）の e2e。チャット以外の代表ページ（home.html）で検証する。"""
from __future__ import annotations

import json
import re

from mock_api import USER_ADMIN, auth_me_response, install_api_mocks
from playwright.sync_api import expect


def _open(page, web_base_url, path="home.html", **mock_kw):
    records = install_api_mocks(page, **mock_kw)
    page.goto(f"{web_base_url}/{path}")
    return records


def test_topbar_dropdown_logout_from_non_chat_page(page, web_base_url):
    """ユーザー表示からドロップダウンを開き、ログアウトすると /auth/logout を叩いて login.html へ遷移する。
    Escape・外側クリックで閉じる。"""
    records = _open(page, web_base_url)

    expect(page.locator("#topbar-user")).to_be_visible()
    page.locator("#topbar-user").click()
    menu = page.locator("#usermenu")
    expect(menu).to_be_visible()
    expect(menu).to_contain_text("管理者")
    expect(page.locator("#um-logout")).to_be_visible()
    expect(page.locator("#um-changepw")).to_be_visible()
    expect(page.locator("#um-note")).to_be_hidden()

    page.keyboard.press("Escape")
    expect(menu).to_be_hidden()
    page.locator("#topbar-user").click()
    expect(menu).to_be_visible()
    page.locator("body").click(position={"x": 5, "y": 400})   # メニュー外側をクリック
    expect(menu).to_be_hidden()

    page.locator("#topbar-user").click()
    page.on("dialog", lambda d: d.accept())   # confirm('ログアウトしますか？')
    page.locator("#um-logout").click()
    page.wait_for_url("**/login.html**", timeout=5000)
    assert records["auth_logout"] == [True]
    assert "login.html" in page.url


def test_topbar_dropdown_hides_logout_in_compat_mode(page, web_base_url):
    """互換モード（auth_disabled:true）ではログアウト/パスワード変更を隠し「認証は無効です」注記を出す。"""
    _open(page, web_base_url, user={**USER_ADMIN, "auth_disabled": True})

    page.locator("#topbar-user").click()
    expect(page.locator("#usermenu")).to_be_visible()
    expect(page.locator("#um-logout")).to_be_hidden()
    expect(page.locator("#um-changepw")).to_be_hidden()
    expect(page.locator("#um-note")).to_be_visible()
    expect(page.locator("#um-note")).to_contain_text("認証は無効です")


def test_topbar_shows_running_turn_badge_and_links_to_conversation(page, web_base_url):
    """実行中ターンがあるとバッジが出て該当会話へ遷移できる。無ければ出さない（既定=空一覧）。"""
    _open(page, web_base_url)
    expect(page.locator("#healthdot")).to_be_visible()
    expect(page.locator("#turnnotice")).to_be_hidden()

    page.unroute_all()
    install_api_mocks(page)
    page.route("**/chat/turns/running", lambda route: route.fulfill(
        content_type="application/json",
        body=json.dumps({"turns": [{"turn_id": "t1", "conversation_id": 42,
                                    "started_at": "2026-07-03T09:00:00+00:00"}]})))
    page.goto(f"{web_base_url}/home.html")

    notice = page.locator("#turnnotice")
    expect(notice).to_be_visible()
    expect(notice).to_contain_text("回答作成中")
    expect(notice).to_have_attribute("href", "chat.html?conv=42")


def _set_tab_hidden(page, hidden: bool) -> None:
    page.evaluate(
        "(h) => { Object.defineProperty(document, 'hidden', { value: h, configurable: true }); "
        "document.dispatchEvent(new Event('visibilitychange')); }",
        hidden,
    )


def test_healthdot_polling_pauses_when_hidden_and_resumes_once_visible(page, web_base_url):
    """状態ドットのポーリング（/health/summary）は非表示タブでは止まり、可視化に戻った瞬間に 1 回即時実行して
    定期ポーリングを再開する。起動直後の呼び出し回数は固定せず、安定後の基準値からの増分だけを見る。"""
    install_api_mocks(page)
    calls = {"n": 0}

    def handle_health_summary(route):
        calls["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "ok"}))

    page.route("**/health/summary", handle_health_summary)
    # clock は goto の前に install する（読込直後の setInterval を仮想化するため）
    page.clock.install()
    page.goto(f"{web_base_url}/home.html")
    expect(page.locator("#healthdot")).to_be_visible()
    page.wait_for_timeout(200)
    base = calls["n"]

    _set_tab_hidden(page, True)
    page.clock.fast_forward(60000)   # HEALTH_POLL_MS（45秒）を超えても非表示中は呼ばれない
    page.wait_for_timeout(200)
    assert calls["n"] == base

    _set_tab_hidden(page, False)
    page.wait_for_timeout(200)
    assert calls["n"] == base + 1   # 可視化に戻った瞬間の即時1回

    page.clock.fast_forward(46000)   # 再開した定期ポーリングが動く
    page.wait_for_timeout(200)
    assert calls["n"] == base + 2


def test_topbar_nav_collapses_overflow_into_more_menu(page, web_base_url):
    """横幅に収まらないタブは右端の「その他 ▾」に畳む。<a> は移動するだけ（複製しない）＝href／現在ページの強調はそのまま。
    広げれば元のリストへ戻る。畳まれたタブはメニューから普通のリンクとして辿れる。"""
    install_api_mocks(page)                       # 既定=admin＝タブ 8 本（一般 5＋管理 3）
    page.set_viewport_size({"width": 800, "height": 720})
    page.goto(f"{web_base_url}/graph.html")

    more = page.locator("#navmore")
    menu = page.locator("#navmenu")
    expect(more).to_be_visible()
    expect(more).to_have_attribute("aria-expanded", "false")
    expect(menu).to_be_hidden()
    expect(page.locator('#navmenu a[href="admin-settings.html"]')).to_have_count(1)
    expect(page.locator('#navlist a[href="admin-settings.html"]')).to_have_count(0)
    expect(page.locator('.nav a[href="admin-settings.html"]')).to_have_count(1)
    expect(page.locator('#navlist a[href="home.html"]')).to_be_visible()   # 800px なら先頭は残る
    expect(page.locator('#navmenu a[href="graph.html"]')).to_have_count(1)
    expect(more).to_have_class(re.compile(r"\bon\b"))   # 現在ページが畳まれている間はボタン側を強調
    assert page.evaluate(
        "() => { const l = document.getElementById('navlist'); return l.scrollWidth <= l.clientWidth; }"
    )

    more.click()
    expect(menu).to_be_visible()
    expect(more).to_have_attribute("aria-expanded", "true")
    graph_item = page.locator('#navmenu a[href="graph.html"]')
    expect(graph_item).to_be_visible()
    expect(graph_item).to_have_class(re.compile(r"\bon\b"))
    box = menu.bounding_box()
    assert box["x"] >= 0 and box["x"] + box["width"] <= 800   # 画面内に収まる

    page.keyboard.press("Escape")                 # Esc で閉じてボタンへフォーカスを戻す
    expect(menu).to_be_hidden()
    expect(more).to_be_focused()
    expect(more).to_have_attribute("aria-expanded", "false")

    more.click()
    expect(menu).to_be_visible()
    page.locator("body").click(position={"x": 5, "y": 400})   # 外側クリックで閉じる
    expect(menu).to_be_hidden()

    # 極端に狭くても「その他 ▾」自体は切れず、全タブがメニュー側へ移る
    page.set_viewport_size({"width": 320, "height": 720})
    expect(page.locator("#navlist a")).to_have_count(0)
    expect(page.locator("#navmenu a")).to_have_count(8)
    assert page.evaluate(
        "() => { const l = document.getElementById('navlist').getBoundingClientRect();"
        " const b = document.getElementById('navmore').getBoundingClientRect();"
        " return b.left >= l.left - 0.5 && b.right <= l.right + 0.5; }"
    )

    page.set_viewport_size({"width": 1366, "height": 900})   # 広げると全部リストへ戻る
    expect(more).to_be_hidden()
    expect(page.locator("#navmenu a")).to_have_count(0)
    graph_tab = page.locator('#navlist a[href="graph.html"]')
    expect(graph_tab).to_be_visible()
    expect(graph_tab).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator('#navlist a[href="admin-settings.html"]')).to_be_visible()

    page.set_viewport_size({"width": 800, "height": 720})   # 畳まれたタブはメニューから普通のリンクとして辿れる
    more.click()
    item = page.locator('#navmenu a[href="admin-settings.html"]')
    expect(item).to_be_visible()
    item.click()
    page.wait_for_url("**/admin-settings.html**", timeout=5000)


def test_topbar_late_links_keep_order_when_already_folded(page, web_base_url):
    """/auth/me が遅れて届いたとき（初期タブが畳まれた後）も、後から足すタブは末尾に並ぶ（回帰）。
    応答を保留し、初期タブが #navmenu へ移ったのを確認してから返す。"""
    install_api_mocks(page)
    held: list = []
    page.route("**/auth/me", lambda route: held.append(route))   # 後掛けの route が優先＝応答を保留
    page.set_viewport_size({"width": 480, "height": 720})
    page.goto(f"{web_base_url}/home.html")
    expect(page.locator('#navmenu a[href="settings.html"]')).to_have_count(1)   # 初期タブが先に畳まれた
    expect(page.locator('.nav a[href="workspace.html"]')).to_have_count(0)      # 認証後のタブはまだ無い
    assert held
    for r in held:
        r.fulfill(status=200, content_type="application/json", body=json.dumps(auth_me_response(USER_ADMIN)))
    expect(page.locator('#navmenu a[href="admin-settings.html"]')).to_have_count(1)
    hrefs = page.evaluate(
        "() => Array.from(document.querySelectorAll('#navlist a, #navmenu a')).map(a => a.getAttribute('href'))"
    )
    assert hrefs == [
        "home.html", "chat.html", "manual.html", "settings.html",
        "workspace.html", "ingest.html", "graph.html", "admin-settings.html",
    ]
