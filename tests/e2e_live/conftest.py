from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import quote

import pytest

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover - optional dependency.
    PlaywrightError = Exception
    PlaywrightTimeoutError = TimeoutError
    sync_playwright = None


def _get(url: str, timeout: float = 2.0):
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    return urllib.request.urlopen(req, timeout=timeout)


@pytest.fixture(scope="session")
def live_base_url() -> str:
    """起動済み FastAPI の URL。

    既定は dev server の http://127.0.0.1:8000。未起動なら live E2E は skip。
    """
    base = os.environ.get("SHERPA_E2E_LIVE_BASE_URL", "http://127.0.0.1:8000").rstrip("/")
    try:
        with _get(base + "/healthz") as r:
            if r.status != 200:
                pytest.skip(f"live server is not healthy: {base}/healthz -> {r.status}")
    except Exception as exc:
        pytest.skip(f"live server is not running at {base}: {exc}")
    return base


@pytest.fixture(scope="session")
def live_auth_enabled(live_base_url: str):
    """本物ログインが有効な環境だけで live 認証 E2E を走らせる。"""
    try:
        with _get(live_base_url + "/auth/me") as r:
            if r.status == 200:
                pytest.skip("auth appears disabled; unset SHERPA_AUTH_DISABLED for e2e_live auth flows")
    except urllib.error.HTTPError as exc:
        if exc.code == 401:
            return
        pytest.skip(f"/auth/me returned unexpected status: {exc.code}")


@pytest.fixture(scope="session")
def admin_password() -> str:
    pw = os.environ.get("SHERPA_E2E_ADMIN_PASSWORD") or os.environ.get("SHERPA_ADMIN_PASSWORD")
    if not pw:
        pytest.skip("set SHERPA_ADMIN_PASSWORD or SHERPA_E2E_ADMIN_PASSWORD for e2e_live")
    return pw


@pytest.fixture(scope="session")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright is not installed; run `pip install -r requirements.txt`")
    with sync_playwright() as p:
        try:
            br = p.chromium.launch()
        except PlaywrightError as exc:
            pytest.skip(f"Chromium for Playwright is not installed: {exc}")
        try:
            yield br
        finally:
            br.close()


@pytest.fixture
def page(browser):
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        accept_downloads=True,
    )
    pg = context.new_page()
    page_errors: list[str] = []
    pg.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        yield pg
    finally:
        context.close()
    assert not page_errors, "\n".join(page_errors)


@pytest.fixture
def login_as(page, live_base_url):
    def _login(username: str, password: str, next_path: str = "/ui/chat.html", *, allow_skip: bool = False):
        page.goto(f"{live_base_url}/ui/login.html?next={quote(next_path, safe='')}")
        page.locator("#username").fill(username)
        page.locator("#password").fill(password)
        page.locator("#submit").click()
        expected = next_path.split("?", 1)[0]
        try:
            page.wait_for_url(f"**{expected}**", timeout=7000)
        except PlaywrightTimeoutError:
            err = ""
            if page.locator("#err").count():
                try:
                    err = page.locator("#err").inner_text(timeout=1000)
                except PlaywrightTimeoutError:
                    err = ""
            msg = f"login failed for {username!r}; check live credentials. {err}".strip()
            if allow_skip:
                pytest.skip(msg)
            raise AssertionError(msg)
        return page

    return _login


@pytest.fixture
def admin_login(login_as, admin_password, live_auth_enabled):
    def _admin_login(next_path: str = "/ui/chat.html"):
        return login_as("admin", admin_password, next_path, allow_skip=True)

    return _admin_login


def put_json(page, url: str, payload: dict):
    return page.request.put(
        url,
        headers={"Content-Type": "application/json"},
        data=json.dumps(payload, ensure_ascii=False),
    )


@pytest.fixture
def api_put_json(page):
    def _put(url: str, payload: dict):
        return put_json(page, url, payload)

    return _put
