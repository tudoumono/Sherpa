"""影響分析結果の所有者照合（IDOR対策・監査台帳 2026-07-10-監査対応台帳.md #1）。

`_analyses` の格納エントリに owner_uid/created_at を持たせ、`GET /impact/{aid}` と
`GET /impact/{aid}/export.xlsx` は本人の実行分のみ返す（別ユーザー・期限切れはいずれも404
＝ID の存在自体を秘匿する。impact_export の既存コメントと同じ思想）。

要 Neo4j（`_world_setup.ensure_v1`）。DB/Neo4j 不可は SKIP。
"""
from __future__ import annotations

import time

import pytest
from _test_users import register_test_uid
from _world_setup import TEST_WORLD_ID, ensure_v1
from fastapi.testclient import TestClient

from sherpa import api, auth, store

V = TEST_WORLD_ID


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(uid: str, password: str) -> None:
    store.upsert_user(uid, email=f"{uid}@idor.local", display_name=f"表示名-{uid}",
                       password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(api.app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def _ensure_world():
    try:
        ensure_v1()
    except Exception as e:
        pytest.skip(f"Neo4j down: {e}")


def test_owner_can_get_own_analysis():
    if not _try_init():
        pytest.skip("DB down")
    _ensure_world()
    sfx = _sfx()
    uid, pw = f"idorA{sfx}", f"IdorA{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    aid = c.post("/impact/run", json={"start": "消費税率", "world": V}).json()["analysis_id"]

    r = c.get(f"/impact/{aid}")
    assert r.status_code == 200, r.text


def test_other_user_gets_404_on_get():
    if not _try_init():
        pytest.skip("DB down")
    _ensure_world()
    sfx = _sfx()
    uid_a, pw_a = f"idorB{sfx}", f"IdorB{sfx}"
    uid_b, pw_b = f"idorC{sfx}", f"IdorC{sfx}"
    _mk_user(uid_a, pw_a)
    _mk_user(uid_b, pw_b)
    c_a = _login(uid_a, pw_a)
    c_b = _login(uid_b, pw_b)
    aid = c_a.post("/impact/run", json={"start": "消費税率", "world": V}).json()["analysis_id"]

    r = c_b.get(f"/impact/{aid}")
    assert r.status_code == 404, r.text


def test_other_user_gets_404_on_export():
    if not _try_init():
        pytest.skip("DB down")
    _ensure_world()
    sfx = _sfx()
    uid_a, pw_a = f"idorD{sfx}", f"IdorD{sfx}"
    uid_b, pw_b = f"idorE{sfx}", f"IdorE{sfx}"
    _mk_user(uid_a, pw_a)
    _mk_user(uid_b, pw_b)
    c_a = _login(uid_a, pw_a)
    c_b = _login(uid_b, pw_b)
    aid = c_a.post("/impact/run", json={"start": "消費税率", "world": V}).json()["analysis_id"]

    r = c_b.get(f"/impact/{aid}/export.xlsx")
    assert r.status_code == 404, r.text


def test_expired_analysis_returns_404(monkeypatch):
    """TTL（`_ANALYSES_TTL_SECONDS`）超過分は本人でも404・辞書からも掃除される。"""
    if not _try_init():
        pytest.skip("DB down")
    _ensure_world()
    sfx = _sfx()
    uid, pw = f"idorF{sfx}", f"IdorF{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    aid = c.post("/impact/run", json={"start": "消費税率", "world": V}).json()["analysis_id"]

    # created_at を TTL 超過分だけ過去に書き換える（time.time() をmonkeypatchするより直接的で確実）。
    api._analyses[aid]["created_at"] -= (api._ANALYSES_TTL_SECONDS + 1)

    r = c.get(f"/impact/{aid}")
    assert r.status_code == 404, r.text
    assert aid not in api._analyses   # 取得時に掃除される
