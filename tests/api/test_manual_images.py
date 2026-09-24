"""使い方（manual.html）が参照する画面キャプチャの読み取り専用配信＋概観（cloud）廃止の回帰。

- `/ui/manual-images/<name>.png` は docs/manual/images/ を読み取り専用で配信する（api.py の専用マウント）。
- ディレクトリ外への `..` トラバーサルは 404（StaticFiles が遮断）。
- 廃止した「概観」（cloud.html）は直リンクで 404・ナビ（nav.js）にも出ない。グラフは従来どおり残る。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sherpa.api import app


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """ログインせず直接叩く前提（compat モード）。静的配信は auth 非依存だが念のため揃える。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def test_manual_image_served_read_only():
    c = TestClient(app)
    r = c.get("/ui/manual-images/10-chat-overview.png")
    assert r.status_code == 200
    assert r.headers.get("content-type", "").startswith("image/")
    assert len(r.content) > 0
    # 書き込み系は静的マウントでは提供されない（GET 専用）。
    assert c.put("/ui/manual-images/10-chat-overview.png").status_code in (404, 405)


def test_manual_images_traversal_blocked():
    """ディレクトリ外への `..` は配信しない（正規化されて別ルート無し→404、または StaticFiles が遮断）。"""
    c = TestClient(app)
    for path in (
        "/ui/manual-images/../../sherpa/api.py",
        "/ui/manual-images/%2e%2e%2f%2e%2e%2fsherpa/api.py",
        "/ui/manual-images/../../../etc/passwd",
    ):
        r = c.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"
        assert "FastAPI" not in r.text and "root:" not in r.text


def test_manual_missing_image_is_404():
    c = TestClient(app)
    assert c.get("/ui/manual-images/does-not-exist.png").status_code == 404


def test_cloud_page_removed_but_graph_remains():
    """「概観」（cloud）は機能ごと廃止＝直リンク 404。グラフ画面は従来どおり 200。"""
    c = TestClient(app)
    assert c.get("/ui/cloud.html").status_code == 404
    assert c.get("/ui/cloud.js").status_code == 404
    assert c.get("/ui/graph.html").status_code == 200
    assert c.get("/ui/manual.html").status_code == 200


def test_nav_has_no_overview_entry():
    """ナビ（nav.js）から「概観」/cloud.html の項目が消えていること（ソースを直接確認）。"""
    c = TestClient(app)
    nav = c.get("/ui/nav.js")
    assert nav.status_code == 200
    assert "cloud.html" not in nav.text
    assert "概観" not in nav.text
