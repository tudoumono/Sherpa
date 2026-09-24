"""NOTIFY-1: `GET /notifications` の認可・admin 限定イベントの絞り込みを実 DB で固定する。

`sherpa/notifications.py` の各通知源の組み立てロジック自体は tests/unit/test_notifications.py が
DB 無しで pin する——ここでは「実際に registered world + ingest_runs 行を1本作り、role に応じて
中身が変わる」ことだけを実 TestClient で確認する（要 Postgres・DB 不可は SKIP）。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from _common import _login, _sfx, _try_init
from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


def _mk_user(uid: str, password: str, role: str = "user") -> None:
    store.upsert_user(uid, email=f"{uid}@notif.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)


def test_notifications_requires_login():
    if not _try_init():
        pytest.skip("DB down")
    anon = TestClient(app, raise_server_exceptions=False)
    assert anon.get("/notifications").status_code == 401


def test_notifications_ingest_run_visible_to_everyone_llm_render_admin_only():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    wid = f"notiftest{sfx}"
    admin_uid, admin_pw = f"notifad{sfx}", f"NotifAd{sfx}"
    user_uid, user_pw = f"notifus{sfx}", f"NotifUs{sfx}"
    _mk_user(admin_uid, admin_pw, role="admin")
    _mk_user(user_uid, user_pw, role="user")
    store.upsert_world(wid, f"/tmp/notif-{sfx}", label="通知テスト用資料")
    store.add_ingest_run(wid, status="auto_published", extraction_snapshot={})
    store.add_usage_event(kind="rag_render", provider="openai", model="gpt-5.5", world=wid)
    try:
        admin = _login(admin_uid, admin_pw)
        user = _login(user_uid, user_pw)

        r_user = user.get("/notifications")
        assert r_user.status_code == 200, r_user.text
        user_items = [it for it in r_user.json()["notifications"] if it["world"] == wid]
        assert [it["kind"] for it in user_items] == ["ingest_run"]   # llm_render は admin 限定
        assert user_items[0]["status"] == "done"
        assert user_items[0]["admin_only"] is False

        r_admin = admin.get("/notifications")
        assert r_admin.status_code == 200, r_admin.text
        admin_items = [it for it in r_admin.json()["notifications"] if it["world"] == wid]
        kinds = {it["kind"] for it in admin_items}
        assert kinds == {"ingest_run", "llm_render"}
        render = next(it for it in admin_items if it["kind"] == "llm_render")
        assert render["admin_only"] is True
    finally:
        store.delete_world_row(wid)


def test_notifications_ingest_run_failure_is_visible_to_all():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    wid = f"notiffail{sfx}"
    uid, pw = f"notiffu{sfx}", f"NotifFu{sfx}"
    _mk_user(uid, pw, role="user")
    store.upsert_world(wid, f"/tmp/notif-fail-{sfx}", label="失敗テスト用資料")
    store.add_ingest_run(wid, status="failed", extraction_snapshot={})
    try:
        c = _login(uid, pw)
        r = c.get("/notifications")
        assert r.status_code == 200, r.text
        items = [it for it in r.json()["notifications"] if it["world"] == wid]
        assert len(items) == 1
        assert items[0]["status"] == "failed"
        assert "失敗" in items[0]["message"]
    finally:
        store.delete_world_row(wid)
