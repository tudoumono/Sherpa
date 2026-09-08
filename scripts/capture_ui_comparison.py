"""UI 改善の比較用画面を、既存 API モックと Chromium で撮影する（実サービス不要）。"""
from __future__ import annotations

import argparse
import functools
import json
import sys
import threading
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests" / "e2e"))

from mock_api import install_api_mocks  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

PAGES = [
    "home", "chat", "settings", "admin-settings", "admin-users", "usage", "audit", "status",
    "ingest", "workspace", "graph", "manual", "login", "change-password",
]
VIEWPORTS = [(1280, 900), (1440, 900), (1920, 1080), (640, 450)]


class QuietHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--web-root", type=Path, default=ROOT / "web")
    args = parser.parse_args()
    args.out.mkdir(parents=True, exist_ok=True)
    server = ThreadingHTTPServer(
        ("127.0.0.1", 0), functools.partial(QuietHandler, directory=str(args.web_root)))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    rows = []
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch()
            for theme in ("light", "dark"):
                for width, height in VIEWPORTS:
                    context = browser.new_context(
                        viewport={"width": width, "height": height}, locale="ja-JP",
                        timezone_id="Asia/Tokyo", reduced_motion="reduce")
                    context.add_init_script(
                        f"localStorage.setItem('sherpa-theme', {json.dumps(theme)});")
                    for name in PAGES:
                        page = context.new_page()
                        errors = []
                        page.on("pageerror", lambda e: errors.append(str(e)))
                        install_api_mocks(page, auth_status=401 if name == "login" else 200)
                        page.goto(f"http://127.0.0.1:{server.server_port}/{name}.html")
                        if name == "chat":
                            page.locator("#input").fill("消費税率を変えたい。影響は？")
                            page.locator("#send").click()
                            page.locator("#messages .sources").wait_for()
                        page.evaluate("document.fonts.ready")
                        page.wait_for_timeout(250)
                        if errors:
                            raise RuntimeError(f"{name}: {' / '.join(errors)}")
                        filename = f"{name}-{theme}-{width}.png"
                        page.screenshot(path=str(args.out / filename), full_page=True)
                        metrics = page.evaluate("""() => ({
                            font: getComputedStyle(document.body).fontFamily,
                            size: getComputedStyle(document.body).fontSize,
                            overflow: document.documentElement.scrollWidth > innerWidth,
                            url: location.pathname,
                        })""")
                        cdp = context.new_cdp_session(page)
                        cdp.send("DOM.enable")
                        cdp.send("CSS.enable")
                        document = cdp.send("DOM.getDocument")
                        node = cdp.send("DOM.querySelector", {
                            "nodeId": document["root"]["nodeId"], "selector": "h1, .login-title, .headline, .t, .graphlegend"})
                        metrics["rendered_fonts"] = cdp.send("CSS.getPlatformFontsForNode", {
                            "nodeId": node["nodeId"]})["fonts"]
                        cdp.detach()
                        rows.append({"page": name, "theme": theme, "viewport": f"{width}x{height}",
                                     "state": "mock answer" if name == "chat" else "initial",
                                     "image": filename, **metrics})
                        page.close()
                    context.close()
                    print(f"captured {theme} {width}x{height}", flush=True)
            browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    (args.out / "results.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2) + "\n")
    print(f"{len(rows)} screenshots: {args.out}")


if __name__ == "__main__":
    main()
