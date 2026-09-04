"""tests/api 共通ヘルパ（DB 初期化・uid サフィックス・ログイン処理の3本を抽出・TEST-2 棚卸し）。

多数の test_*.py が同一の `_try_init`/`_sfx`/`_login` をファイルごとに再定義していた重複を
1本化する（`tests/api/_authz_probe.py` と同じ抽出方針＝ロジックは移動のみで挙動は変更しない）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from sherpa import store
from sherpa.api import app


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c
