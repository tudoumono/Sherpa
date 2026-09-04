"""マニュアル一本化（M-A・docs/proposals/2026-07-08-マニュアル一本化.md）:
web の「使い方」が正本 docs/manual/*.md をその場でレンダリングするための読み取り専用配信。

- `/ui/manual-src/<name>.md` と `/ui/manual-src/manifest.json` は配信される。
- README.md・images/ 配下・拡張子違いは対象外（404）。
- ディレクトリ外への `..` トラバーサルは 404。
- manifest.json は旧アンカー互換に必要な id（start/chat/register/graph/workspace/settings/sysadmin）を含む。
"""
from __future__ import annotations

import json

import pytest
from fastapi.testclient import TestClient

from sherpa.api import app


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """ログインせず直接叩く前提（compat モード）。静的配信は auth 非依存だが念のため揃える。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def test_manual_src_manifest_served():
    c = TestClient(app)
    r = c.get("/ui/manual-src/manifest.json")
    assert r.status_code == 200
    data = json.loads(r.text)
    ids = {ch["id"] for ch in data["chapters"]}
    for required in ("start", "chat", "register", "graph", "workspace", "settings", "sysadmin"):
        assert required in ids, f"missing compat id: {required}"
    # 書き込み系は静的マウントでは提供されない（GET 専用）。
    assert c.put("/ui/manual-src/manifest.json").status_code in (404, 405)


def test_manual_src_md_served():
    c = TestClient(app)
    r = c.get("/ui/manual-src/00-製品概要.md")
    assert r.status_code == 200
    assert "製品概要" in r.text


def test_manual_src_readme_and_images_excluded():
    c = TestClient(app)
    assert c.get("/ui/manual-src/README.md").status_code == 404
    assert c.get("/ui/manual-src/images/10-chat-overview.png").status_code == 404
    assert c.get("/ui/manual-src/offline-kit.md").status_code == 200  # .md はトップレベルなら配信対象


def test_manual_src_traversal_blocked():
    c = TestClient(app)
    for path in (
        "/ui/manual-src/../../sherpa/api.py",
        "/ui/manual-src/%2e%2e%2f%2e%2e%2fsherpa/api.py",
        "/ui/manual-src/../../../etc/passwd",
    ):
        r = c.get(path)
        assert r.status_code == 404, f"{path} -> {r.status_code}"
        assert "FastAPI" not in r.text and "root:" not in r.text


def test_manual_src_missing_file_is_404():
    c = TestClient(app)
    assert c.get("/ui/manual-src/does-not-exist.md").status_code == 404


def test_manual_src_all_manifest_files_resolve():
    """manifest が指す全ファイルが実際に配信できる（章追加時の取りこぼし検出）。"""
    c = TestClient(app)
    manifest = json.loads(c.get("/ui/manual-src/manifest.json").text)
    for ch in manifest["chapters"]:
        r = c.get(f"/ui/manual-src/{ch['file']}")
        assert r.status_code == 200, f"{ch['id']} -> {ch['file']} -> {r.status_code}"
