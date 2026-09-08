"""全ページ共通トップバー（nav.js の #topbar-user ドロップダウン）の e2e。

UIフィードバック（2026-07-03）: ログアウトがチャットページ内にしかなかった問題への対応。
`home.html`（チャット以外の代表ページ）で検証し、「全ページで動く」ことを間接的に示す。
"""
from __future__ import annotations

from mock_api import USER_ADMIN, install_api_mocks


def test_topbar_dropdown_logout_from_non_chat_page(page, web_base_url):
    """トップバーのユーザー表示（右上）からドロップダウンを開き、ログアウトすると
    /auth/logout を叩いて login.html へ遷移する。chat.html 以外のページでも動く。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/home.html")

    expect(page.locator("#topbar-user")).to_be_visible()
    page.locator("#topbar-user").click()

    menu = page.locator("#usermenu")
    expect(menu).to_be_visible()
    expect(menu).to_contain_text("管理者")
    expect(page.locator("#um-logout")).to_be_visible()
    expect(page.locator("#um-changepw")).to_be_visible()
    expect(page.locator("#um-note")).to_be_hidden()

    page.on("dialog", lambda d: d.accept())   # confirm('ログアウトしますか？')
    page.locator("#um-logout").click()
    page.wait_for_url("**/login.html**", timeout=5000)

    assert records["auth_logout"] == [True]
    assert "login.html" in page.url


def test_topbar_dropdown_closes_on_escape_and_outside_click(page, web_base_url):
    """Escape・外側クリックでドロップダウンが閉じる（キーボード操作可）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/home.html")

    page.locator("#topbar-user").click()
    expect(page.locator("#usermenu")).to_be_visible()
    page.keyboard.press("Escape")
    expect(page.locator("#usermenu")).to_be_hidden()

    page.locator("#topbar-user").click()
    expect(page.locator("#usermenu")).to_be_visible()
    page.locator("body").click(position={"x": 5, "y": 400})   # メニュー外側をクリック
    expect(page.locator("#usermenu")).to_be_hidden()


def test_topbar_dropdown_hides_logout_in_compat_mode(page, web_base_url):
    """互換モード（認証OFF・/auth/me が auth_disabled:true を返す）では
    ログアウト/パスワード変更を隠し、「認証は無効です」注記を出す。"""
    from playwright.sync_api import expect

    install_api_mocks(page, user={**USER_ADMIN, "auth_disabled": True})
    page.goto(f"{web_base_url}/home.html")

    page.locator("#topbar-user").click()
    expect(page.locator("#usermenu")).to_be_visible()
    expect(page.locator("#um-logout")).to_be_hidden()
    expect(page.locator("#um-changepw")).to_be_hidden()
    expect(page.locator("#um-note")).to_be_visible()
    expect(page.locator("#um-note")).to_contain_text("認証は無効です")


def test_topbar_shows_running_turn_badge_and_links_to_conversation(page, web_base_url):
    """背景実行チャットターン（覗き窓方式・docs/proposals/2026-07-03-チャット背景実行.md §4）:
    実行中ターンがあるとトップバーに「⏳ 回答作成中」バッジが出て、クリックで該当会話
    （chat.html?conv=）へ遷移できる。chat.html 以外（home.html）でも出ること＝全ページ共通の確認。"""
    import json

    from playwright.sync_api import expect

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


def test_topbar_hides_running_turn_badge_when_none(page, web_base_url):
    """実行中ターンが無ければバッジは表示しない（install_api_mocks の既定 = 空一覧）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/home.html")

    expect(page.locator("#turnnotice")).to_be_hidden()


def _set_tab_hidden(page, hidden: bool) -> None:
    """Page Visibility API を Playwright から模擬する（`document.hidden` は読取専用の
    getter のため、own-property で上書きしてから `visibilitychange` を発火させる）。"""
    page.evaluate(
        "(h) => { Object.defineProperty(document, 'hidden', { value: h, configurable: true }); "
        "document.dispatchEvent(new Event('visibilitychange')); }",
        hidden,
    )


def test_healthdot_polling_pauses_when_hidden_and_resumes_once_visible(page, web_base_url):
    """性能是正②（Sherpa.visibilityInterval・性能台帳 QW4）: nav.js の状態ドットポーリング
    （/health/summary）は非表示タブでは止まり、可視化に戻った瞬間に1回即時実行してから
    定期ポーリングを再開する。

    nav.js は `/auth/me` 成功後にも `pollHealth()` を追加で1回呼ぶため（役割判明直後の
    「クリックで詳細」反映）、起動直後の呼び出し回数は実装詳細として固定しない——安定するまで
    実時間待ちしてから基準値を取り、以後は基準値からの増分だけを検証する。`page.clock.fast_forward`
    はページ内の仮想時刻を進めるだけでモック応答の到達までは保証しないため、各ステップ後にも
    `page.wait_for_timeout` を挟む。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    calls = {"n": 0}

    def handle_health_summary(route):
        calls["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"status": "ok"}))

    page.route("**/health/summary", handle_health_summary)
    # clock は goto の前に install する（ページ読込直後に張られる setInterval 自体を仮想化
    # するため）。goto の後に install すると、その setInterval は実タイマーのまま残り、
    # 「hidden 中は呼ばれない」assert が単に短時間しか待っていないだけの空振りになりうる。
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
    """横幅に収まらないタブは右端の「その他 ▾」に畳む（2026-09-07）。切れて見えなくなるタブを作らない。
    <a> は移動するだけ（複製しない）＝href／現在ページの強調はそのまま。広げれば元のリストへ戻る。"""
    import re

    from playwright.sync_api import expect

    install_api_mocks(page)                       # 既定=admin＝タブ 8 本（一般 5＋管理 3）
    page.set_viewport_size({"width": 800, "height": 720})
    page.goto(f"{web_base_url}/graph.html")

    more = page.locator("#navmore")
    menu = page.locator("#navmenu")
    expect(more).to_be_visible()
    expect(more).to_have_attribute("aria-expanded", "false")
    expect(menu).to_be_hidden()
    # 末尾側のタブがメニューへ移り、リストには残らない（要素は 1 つのまま）。先頭タブは必ずリストに残る。
    expect(page.locator('#navmenu a[href="admin-settings.html"]')).to_have_count(1)
    expect(page.locator('#navlist a[href="admin-settings.html"]')).to_have_count(0)
    expect(page.locator('.nav a[href="admin-settings.html"]')).to_have_count(1)
    expect(page.locator('#navlist a[href="home.html"]')).to_be_visible()   # 800px なら先頭は残る
    # 現在ページ（ナレッジグラフ）が畳まれている間はボタン側を強調する
    expect(page.locator('#navmenu a[href="graph.html"]')).to_have_count(1)
    expect(more).to_have_class(re.compile(r"\bon\b"))
    # リストに残ったタブは切れていない（内容幅が表示幅に収まる）
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

    # 極端に狭くても「その他 ▾」自体は切れず、全タブがメニュー側へ移る（ボタンが切れると畳んだタブへ到達できない）
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


def test_topbar_more_menu_link_navigates(page, web_base_url):
    """畳まれたタブはメニューから普通のリンクとして辿れる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.set_viewport_size({"width": 800, "height": 720})
    page.goto(f"{web_base_url}/home.html")
    page.locator("#navmore").click()
    item = page.locator('#navmenu a[href="admin-settings.html"]')
    expect(item).to_be_visible()
    item.click()
    page.wait_for_url("**/admin-settings.html**", timeout=5000)


def test_topbar_late_links_keep_order_when_already_folded(page, web_base_url):
    """/auth/me が遅れて届いたとき（初期タブが畳まれた後）も、後から足すタブは末尾に並ぶ。
    応答を保留し、初期タブが #navmenu へ移ったのを確認してから返す＝順序の回帰条件を決定的に踏む。"""
    import json

    from mock_api import auth_me_response
    from playwright.sync_api import expect

    install_api_mocks(page)
    held: list = []
    page.route("**/auth/me", lambda route: held.append(route))   # 後掛けの route が優先＝応答を保留
    page.set_viewport_size({"width": 480, "height": 720})
    page.goto(f"{web_base_url}/home.html")
    expect(page.locator('#navmenu a[href="settings.html"]')).to_have_count(1)   # 初期タブが先に畳まれた
    expect(page.locator('.nav a[href="workspace.html"]')).to_have_count(0)      # 認証後のタブはまだ無い
    assert held   # nav.js（＋ページ側スクリプト）の /auth/me がここで待っている
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
