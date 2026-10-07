"""回答への評価 API（`GET /admin/feedback/summary`・`/admin/feedback/items`）。管理者のみ・個人由来を除く・閲覧を監査に残す。要 Postgres。"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _mk_user(uid: str, password: str, role: str = "user") -> TestClient:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    store.upsert_user(uid, email=f"{uid}@fbadmin.local", display_name=uid,
                      password_hash=auth.hash_password(password), role=role, status="active")
    register_test_uid(uid)
    c = TestClient(app, raise_server_exceptions=False)
    assert c.post("/auth/login", json={"username": uid, "password": password}).status_code == 200
    return c


def _turn(owner: str, question: str, *, personal: bool = False) -> tuple[dict, dict]:
    conv = store.create_conversation(user_id=owner, world="v1")
    um = store.add_message(conv["id"], "user", question, personal=personal)
    am = store.add_message(conv["id"], "assistant", "回答", lens="qa", personal=personal,
                           answer={"lens": "qa", "headline": "回答", "sources": [],
                                   "usage": {"provider": "openai"}})
    store.audit(owner, "chat.turn", "conversation", f"conv:{conv['id']}",
                detail={"message_id_user": um["id"], "message_id_assistant": am["id"], "lens": "qa",
                        "personal": personal},
                outcome="success", severity="info")
    return conv, am


def test_feedback_admin_requires_admin():
    sfx = _sfx()
    c = _mk_user(f"fbnon{sfx}", f"FbNon{sfx}")
    assert c.get("/admin/feedback/summary").status_code == 403
    assert c.get("/admin/feedback/items").status_code == 403


def test_feedback_items_exclude_personal_and_are_audited():
    sfx = _sfx()
    admin = _mk_user(f"fbadm{sfx}", f"FbAdm{sfx}", role="admin")
    owner_uid = f"fbown{sfx}"
    owner = _mk_user(owner_uid, f"FbOwn{sfx}")
    shown_q, hidden_q = f"通常の質問-{sfx}", f"個人の質問-{sfx}"
    conv_a, am_a = _turn(owner_uid, shown_q)
    conv_b, am_b = _turn(owner_uid, hidden_q, personal=True)
    for conv, am in ((conv_a, am_a), (conv_b, am_b)):
        r = owner.post(f"/chat/{conv['id']}/messages/{am['id']}/feedback",
                       json={"rating": "down", "tags": ["slow"], "comment": f"<b>一言</b>{sfx}"})
        assert r.status_code == 200, r.text

    r = admin.get("/admin/feedback/items?days=1&limit=200")
    assert r.status_code == 200, r.text
    items = r.json()["items"]
    heads = {i["question_head"] for i in items}
    assert shown_q in heads and hidden_q not in heads
    mine = next(i for i in items if i["question_head"] == shown_q)
    assert mine["comment"] == f"<b>一言</b>{sfx}" and mine["tags"] == ["slow"]
    assert not ({"conversation_id", "content", "sources"} & set(mine))

    assert store.list_audit(actor=f"fbadm{sfx}", action="admin.feedback_viewed", limit=5)
    assert admin.get("/admin/feedback/summary?days=1").status_code == 200
