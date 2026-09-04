"""ログイン壁（login wall）の e2e テスト。

nav.js が /auth/me の 401 レスポンスを受けたとき login.html へリダイレクトし、
互換モード（200）や login.html 自体ではリダイレクトしないことを検証する。
"""
from __future__ import annotations

import json

from mock_api import install_api_mocks


def _mock_auth(page, status: int):
    """全ページで /auth/me を指定ステータスで返すルートを設定する。"""
    def handler(route):
        if route.request.url.endswith("/auth/me"):
            body = json.dumps(
                {"uid": "admin", "display_name": "管理者", "role": "admin"}
                if status == 200 else {"detail": "認証が必要です"}
            )
            route.fulfill(status=status, content_type="application/json", body=body)
        else:
            # その他（CSS, JS 等）は静的ファイルサーバに委ねる。
            route.continue_()

    page.route("**/*", handler)


def test_unauth_page_redirects_to_login(page, web_base_url):
    """/auth/me が 401 のとき、保護ページ（chat.html）が login.html へリダイレクトされる。"""
    _mock_auth(page, 401)
    page.goto(f"{web_base_url}/chat.html")
    # nav.js が 401 を検知して location.href = '/ui/login.html?next=...' へ変更する。
    # 静的サーバは /ui/ プレフィックスを持たないので login.html に落ち着く。
    page.wait_for_url("**/login.html**", timeout=5000)
    assert "login.html" in page.url


def test_unauth_redirect_includes_safe_next(page, web_base_url):
    """/auth/me が 401 のとき、next= は同一オリジン /ui/ パスのみ含まれる。"""
    _mock_auth(page, 401)
    page.goto(f"{web_base_url}/chat.html")
    page.wait_for_url("**/login.html**", timeout=5000)
    # next= が付いていてもパーセントエンコードされた同一オリジンパスのみ。
    # open-redirect にならないこと（http:// や // で始まらない）を確認。
    params = page.url.split("?", 1)[1] if "?" in page.url else ""
    if params:
        from urllib.parse import parse_qs, unquote
        qs = parse_qs(params)
        if "next" in qs:
            next_val = unquote(qs["next"][0])
            assert next_val.startswith("/ui/"), f"next は /ui/ で始まる必要がある: {next_val!r}"
            assert "://" not in next_val, f"next にスキームが含まれている: {next_val!r}"


def test_compat_mode_no_redirect(page, web_base_url):
    """/auth/me が 200（互換モード合成 admin）のとき、リダイレクトしない。"""
    _mock_auth(page, 200)
    page.goto(f"{web_base_url}/chat.html")
    # 300ms 待って URL が変わらないことを確認（login.html へ飛ばない）。
    import time
    time.sleep(0.3)
    assert "login.html" not in page.url, f"互換モードで誤リダイレクト: {page.url}"


def test_login_html_does_not_redirect_loop(page, web_base_url):
    """/auth/me が 401 でも login.html 自体はリダイレクトしない（ループ防止）。

    login.html は nav.js を読み込まないので、そもそも /auth/me を呼ばない。
    この確認は DOM の存在確認（ログインフォームが表示されること）で行う。
    """
    _mock_auth(page, 401)
    page.goto(f"{web_base_url}/login.html")
    # リダイレクトされず login.html のままでフォームが見えること。
    import time
    time.sleep(0.3)
    assert "login.html" in page.url, f"login.html から意図せずリダイレクト: {page.url}"
    assert page.locator("#form").count() == 1, "ログインフォームが存在しない"


def test_login_form_posts_credentials_and_uses_safe_next(page, web_base_url):
    """ログイン成功時は /auth/login に資格情報を送り、安全な next へ遷移する。"""
    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/login.html?next=%2Fui%2Fgraph.html%3Ffrom%3Dlogin")

    page.locator("#username").fill("admin")
    page.locator("#password").fill("secret")
    page.locator("#submit").click()

    page.wait_for_url("**/ui/graph.html?from=login", timeout=5000)
    assert records["auth_login"][-1] == {"username": "admin", "password": "secret"}


def test_login_form_shows_generic_error_for_invalid_credentials(page, web_base_url):
    """ログイン失敗時はユーザー存在情報を漏らさない汎用エラーを表示する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, login_status=401)
    page.goto(f"{web_base_url}/login.html")

    page.locator("#username").fill("unknown")
    page.locator("#password").fill("bad")
    page.locator("#submit").click()

    expect(page.locator("#err")).to_contain_text("ユーザー名またはパスワードが正しくありません")
    assert "login.html" in page.url
    assert records["auth_login"][-1] == {"username": "unknown", "password": "bad"}


def test_login_form_rejects_external_next(page, web_base_url):
    """外部 URL の next は採用せず、既定のチャット画面へ遷移する。"""
    install_api_mocks(page)
    page.goto(f"{web_base_url}/login.html?next=https%3A%2F%2Fevil.example%2Fsteal")

    page.locator("#username").fill("admin")
    page.locator("#password").fill("secret")
    page.locator("#submit").click()

    page.wait_for_url("**/ui/chat.html", timeout=5000)
