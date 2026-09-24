"""R2a #3（2026-07-14 横断レビュー対応・Codex RV HIGH）: `llm.urlopen_no_redirect` が 3xx
（redirect）を追跡しないことの回帰テスト。

`assert_ollama_url_allowed` は接続開始時の宛先しか見ないため、応答が 3xx redirect を返すと
（既定の `urlopen` は自動追跡する）allowlist 外・任意パスへ誘導されうる。ここでは実ローカル
HTTP サーバ（`tests/unit/test_legacy_convert.py` と同じ手法・127.0.0.1 の空きポート）を立て、
`llm.urlopen_no_redirect` が redirect 先へ到達しないことを実際の HTTP 往復で確認する
（`tests/contract/test_ssrf_allowlist.py` の autouse network guard と分離するため、ここでは
専用ファイルに置く＝redirect を追跡「しない」ことそのものを検証したいので、実ネットワーク
（ローカルループバック）を塞ぐ fixture が無い場所で行う）。
"""
from __future__ import annotations

import http.server
import threading
import urllib.error
import urllib.request

import pytest

from sherpa import llm


class _RedirectHandler(http.server.BaseHTTPRequestHandler):
    """`/api/tags` は `/should-not-be-reached` への 302 を返す。redirect が追跡されれば
    `followed`（サーバインスタンスの属性）が True になる。"""

    def log_message(self, *a):    # テスト出力を汚さない
        pass

    def do_GET(self):
        if self.path == "/api/tags":
            self.send_response(302)
            self.send_header("Location", "/should-not-be-reached")
            self.end_headers()
            return
        if self.path == "/should-not-be-reached":
            self.server.followed = True
            body = b"{}"
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        self.send_response(404)
        self.end_headers()


def _start_redirect_server():
    srv = http.server.HTTPServer(("127.0.0.1", 0), _RedirectHandler)   # port 0＝空きポート
    srv.followed = False
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def test_urlopen_no_redirect_does_not_follow_redirect():
    """3xx はそのまま `urllib.error.HTTPError` として送出され、redirect 先へは到達しない。

    break-and-confirm: `llm.urlopen_no_redirect` の実装を素の `urllib.request.urlopen` に
    戻すと、本テストは `srv.followed is True`（redirect 先へ到達してしまう）で失敗する。
    """
    srv, base = _start_redirect_server()
    try:
        with pytest.raises(urllib.error.HTTPError) as exc:
            llm.urlopen_no_redirect(f"{base}/api/tags", timeout=2)
        assert exc.value.code == 302
        assert srv.followed is False, "redirect 先（allowlist の想定外パス）へ追跡してしまった"
    finally:
        srv.shutdown()


def test_urlopen_no_redirect_accepts_request_object_and_timeout_none():
    """`req` が `urllib.request.Request` でも動く（`post_json`/`_stream` と同じ呼び方）。
    `timeout=None` も既定の urllib タイムアウトへフォールバックしてエラーなく動く。"""
    srv, base = _start_redirect_server()
    try:
        req = urllib.request.Request(f"{base}/should-not-be-reached")
        with llm.urlopen_no_redirect(req, timeout=2) as r:
            assert r.status == 200
    finally:
        srv.shutdown()
