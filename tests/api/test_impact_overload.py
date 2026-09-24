"""POST /impact/run の Neo4j 安全弁縮退（secRV 範囲外是正・2026-07-19）。

`run_impact`（ingest.world_neo4j 経由）が `GraphQueryOverloadError`（timeout／緊急天井）を送出した
場合、未処理の 500 ではなく 503（平文・専門用語ゼロ・docs/04-画面の原則.md §5/§6）へ縮退することを
固定する。実際に Neo4j を過負荷にはせず、`sherpa.routers.impact.run_impact` を monkeypatch して
例外を直接送出させる（fail-loud＝偽陰性防止の縮退経路だけを検証する・関数自体の安全弁ロジックは
tests/unit/test_world_neo4j_overload.py が担当）。

要 Neo4j（`_world_setup.ensure_v1`・`tests/api/test_impact_idor.py` と同じ流儀）。DB/Neo4j 不可は SKIP。
"""
from __future__ import annotations

import time

import pytest
from _test_users import register_test_uid
from _world_setup import TEST_WORLD_ID, ensure_v1
from fastapi.testclient import TestClient

from sherpa import api, auth, store
from sherpa.ingest.world_neo4j import GRAPH_OVERLOAD_USER_MESSAGE, GraphQueryOverloadError

V = TEST_WORLD_ID


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _ensure_world():
    try:
        ensure_v1()
    except Exception as e:
        pytest.skip(f"Neo4j down: {e}")


def _login() -> TestClient:
    sfx = _sfx()
    uid, pw = f"overload{sfx}", f"Overload{sfx}"
    store.upsert_user(uid, email=f"{uid}@overload.local", display_name=f"表示名-{uid}",
                      password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid)
    c = TestClient(api.app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return c


def test_impact_run_degrades_to_503_on_timeout_overload(monkeypatch):
    if not _try_init():
        pytest.skip("DB down")
    _ensure_world()
    c = _login()

    def _boom(*a, **kw):
        raise GraphQueryOverloadError("timeout", world=V)

    from sherpa.routers import impact as impact_router_mod
    monkeypatch.setattr(impact_router_mod, "run_impact", _boom)

    r = c.post("/impact/run", json={"start": "消費税率", "world": V})
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == GRAPH_OVERLOAD_USER_MESSAGE


def test_impact_run_degrades_to_503_on_row_cap_overload(monkeypatch):
    """天井到達（too_many_rows）も同じ 503・同じ平文へ縮退する（reason 違いで分岐しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    _ensure_world()
    c = _login()

    def _boom(*a, **kw):
        raise GraphQueryOverloadError("too_many_rows", world=V, rows=10000)

    from sherpa.routers import impact as impact_router_mod
    monkeypatch.setattr(impact_router_mod, "run_impact", _boom)

    r = c.post("/impact/run", json={"start": "消費税率", "world": V})
    assert r.status_code == 503, r.text
    assert r.json()["detail"] == GRAPH_OVERLOAD_USER_MESSAGE
