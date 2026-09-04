from __future__ import annotations

import functools
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

try:
    from playwright.sync_api import Error as PlaywrightError
    from playwright.sync_api import sync_playwright
except ModuleNotFoundError:  # pragma: no cover - exercised when optional dependency is absent.
    PlaywrightError = Exception
    sync_playwright = None


ROOT = Path(__file__).resolve().parents[2]
WEB = ROOT / "web"


class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


@pytest.fixture(scope="session")
def web_base_url():
    handler = functools.partial(_QuietStaticHandler, directory=str(WEB))
    server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture(scope="session")
def browser():
    if sync_playwright is None:
        pytest.skip("playwright is not installed; run `pip install playwright` and `python -m playwright install chromium`")
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
    # RV LOW（2026-07・S3 日時表示テスト）: 日時変換系のテストは実行環境の TZ に依存する
    # （`new Date(iso)` のローカル変換はブラウザの実効タイムゾーンに従うため）。実行機の TZ に
    # 関わらず決定的にするため JST を明示指定する（本番の実ユーザーも日本語ロケール・JST 前提）。
    context = browser.new_context(
        viewport={"width": 1366, "height": 900},
        accept_downloads=True,
        timezone_id="Asia/Tokyo",
        locale="ja-JP",
    )
    pg = context.new_page()
    page_errors: list[str] = []
    pg.on("pageerror", lambda exc: page_errors.append(str(exc)))
    try:
        yield pg
    finally:
        context.close()
    assert not page_errors, "\n".join(page_errors)
