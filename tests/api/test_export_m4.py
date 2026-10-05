"""出力 受け入れ（要 Neo4j）: 根拠DL(R6・パス基準)。AT-2/AT-3（鏡モデル）。"""
from __future__ import annotations

import pytest
from _world_setup import BILLGEN, SPEC, TAXCPY, TEST_WORLD_ID, ensure_v1
from fastapi.testclient import TestClient

from sherpa.api import app
from sherpa.documents import resolve

client = TestClient(app)
V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """このファイルはログインせず直接叩く前提（compat モード）。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def test_doc_resolve():
    """DL はパス基準（rel_path）。実在ソース→解決可。存在しない設計書 rel→None。トラバーサルは None。"""
    assert resolve(BILLGEN, V) is not None
    assert resolve(TAXCPY, V) is not None
    assert resolve("4期/02_設計/01_基本設計/未作成_NOEXIST.md", V) is None   # 実在しない設計書
    assert resolve("../etc/passwd", V) is None


def test_download():
    """根拠DL（R6・AT-2・パス基準）: 実在ソースは DL 可、実在しない設計書は 404。"""
    ensure_v1()
    ok = client.get("/documents/download", params={"world": V, "rel": BILLGEN})
    assert ok.status_code == 200
    ng = client.get("/documents/download",
                    params={"world": V, "rel": "4期/02_設計/01_基本設計/未作成_NOEXIST.md"})
    assert ng.status_code == 404
