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
