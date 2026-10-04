"""認証・ユーザー管理・会話共有 API の契約（ログイン必須モード・要 Postgres）。"""
from __future__ import annotations

import time
from datetime import datetime, timedelta, timezone

import psycopg
import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, notifications, store
from sherpa.api import app

client = TestClient(app, raise_server_exceptions=True)


@pytest.fixture(autouse=True)
def _db():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _sfx():
    return str(int(time.time() * 1000))[-8:]


def _mk(uid: str, pw: str, role: str, display_name: str, **kw) -> tuple[str, str]:
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=display_name,
                      password_hash=auth.hash_password(pw), role=role, status=kw.pop("status", "active"), **kw)
    register_test_uid(uid)
    return uid, pw


def _mk_admin(sfx: str) -> tuple[str, str]:
    return _mk(f"adm{sfx}", f"pw-adm{sfx}", "admin", "Admin Test")


def _mk_user(sfx: str, role: str = "user") -> tuple[str, str]:
    return _mk(f"usr{sfx}", f"pw-usr{sfx}", role, "User Test")


def _cookies(uid: str, pw: str):
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return r.cookies


def _conv(owner_uid: str, title: str, *msgs: str) -> int:
    cid = store.create_conversation(user_id=owner_uid, world="v1", title=title)["id"]
    for i, m in enumerate(msgs):
        store.add_message(cid, "user" if i % 2 == 0 else "assistant", m)
    return cid


def _share(cookies, cid: int, invitee_uid: str, **extra):
    return client.post(f"/conversations/{cid}/shares",
                       json={"invitee_user_ids": [invitee_uid], **extra}, cookies=cookies)


def _received(uid: str) -> list[dict]:
    return [c for c in store.list_conversations(uid) if c["origin"] == "received_share"]


def _share_row(owner: str, cid: int, sid: int) -> dict:
    return [s for s in store.list_shares_for_conversation(owner, cid) if s["share_id"] == sid][0]


def _sql(q: str, *args):
    with psycopg.connect(store._dsn()) as c:
        c.execute(q, args)
        c.commit()


# ===== ログイン・パスワード =====

def test_login_and_me():
    uid, pw = _mk_admin(_sfx())
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    assert "sherpa_session" in r.cookies
    me = client.get("/auth/me", cookies=r.cookies)
    assert me.status_code == 200
    assert me.json()["uid"] == uid
    assert me.json()["role"] == "admin"
    assert client.post("/auth/logout", cookies=r.cookies).status_code == 200
    assert client.get("/auth/me", cookies=r.cookies).status_code == 401


def test_wrong_password_401():
    """パスワード誤りも存在しないユーザーも同じ汎用 401（ユーザー存在を漏らさない）。"""
    uid, _ = _mk_user(_sfx())
    r = client.post("/auth/login", json={"username": uid, "password": "wrong"})
    assert r.status_code == 401
    assert "パスワード" in r.json().get("detail", "")
    assert client.post("/auth/login", json={"username": "no_such_user_xyz", "password": "x"}).status_code == 401


def test_must_change_password_blocks_app_until_changed():
    sfx = _sfx()
    uid, pw = _mk(f"chg{sfx}", f"pw-chg{sfx}", "user", "Change Required", must_change_password=True)
    new_pw = f"BetterPass{sfx}"
    lr = client.post("/auth/login", json={"username": uid, "password": pw})
    assert lr.status_code == 200, lr.text
    assert lr.json()["must_change_password"] is True
    me = client.get("/auth/me", cookies=lr.cookies)
    assert me.status_code == 200
    assert me.json()["must_change_password"] is True
    assert client.get("/settings", cookies=lr.cookies).status_code == 403

    weak = client.post("/auth/change-password", cookies=lr.cookies,
                       json={"current_password": pw, "new_password": "password123",
                             "confirm_password": "password123"})
    assert weak.status_code == 422
    ok = client.post("/auth/change-password", cookies=lr.cookies,
                     json={"current_password": pw, "new_password": new_pw, "confirm_password": new_pw})
    assert ok.status_code == 200, ok.text
    assert ok.json()["must_change_password"] is False

    assert client.get("/settings", cookies=lr.cookies).status_code == 200
    row = store.get_user_by_uid(uid)
    assert row["must_change_password"] is False
    assert auth.verify_password(new_pw, row["password_hash"])


def test_admin_created_and_reset_passwords_force_change_on_next_login():
    """管理者が作成した初期パスワード・管理者によるリセットは、次回ログインで本人変更を強制する。"""
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    usr_uid, _ = _mk_user(sfx)
    assert store.get_user_by_uid(usr_uid)["must_change_password"] is False
    adm_cookies = _cookies(adm_uid, adm_pw)

    new_uid = f"pwinit{sfx}"
    cr = client.post("/admin/users", cookies=adm_cookies,
                     json={"uid": new_uid, "display_name": "PwInit", "role": "user", "password": "init-pass-1"})
    assert cr.status_code == 200, cr.text
    register_test_uid(new_uid)
    lr = client.post("/auth/login", json={"username": new_uid, "password": "init-pass-1"})
    assert lr.status_code == 200, lr.text
    assert lr.json()["must_change_password"] is True

    pr = client.patch(f"/admin/users/{usr_uid}", json={"password": f"reset-pass-{sfx}"}, cookies=adm_cookies)
    assert pr.status_code == 200, pr.text
    assert store.get_user_by_uid(usr_uid)["must_change_password"] is True
    lr = client.post("/auth/login", json={"username": usr_uid, "password": f"reset-pass-{sfx}"})
    assert lr.status_code == 200, lr.text
    assert lr.json()["must_change_password"] is True


# ===== ユーザー管理 =====

def test_admin_creates_user():
    """admin は /admin/users で一覧・作成できる。non-admin は 403。"""
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    usr_uid, usr_pw = _mk_user(sfx)
    adm_cookies = _cookies(adm_uid, adm_pw)

    lu = client.get("/admin/users", cookies=adm_cookies)
    assert lu.status_code == 200
    assert adm_uid in [u["uid"] for u in lu.json()["users"]]

    new_uid = f"new{sfx}"
    cr = client.post("/admin/users", cookies=adm_cookies,
                     json={"uid": new_uid, "display_name": "New", "role": "user", "password": "pass1234"})
    assert cr.status_code == 200, cr.text
    assert cr.json()["user"]["uid"] == new_uid
    register_test_uid(new_uid)

    assert client.get("/admin/users", cookies=_cookies(usr_uid, usr_pw)).status_code == 403


def test_admin_create_user_rejects_duplicate_uid_and_does_not_overwrite():
    """既存 uid（無効化済み含む）への「作成」は 409 で、既存ユーザーのパスワード/権限を上書きしない。"""
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    adm_cookies = _cookies(adm_uid, adm_pw)
    victim_uid, original_pw = f"victim{sfx}", f"orig-pw-{sfx}"
    _mk(victim_uid, original_pw, "user", "Victim", status="disabled")
    before = store.get_user_by_uid(victim_uid)

    cr = client.post("/admin/users", cookies=adm_cookies,
                     json={"uid": victim_uid, "display_name": "Attacker", "role": "admin",
                           "password": "attacker-pass-1"})
    assert cr.status_code == 409, cr.text
    assert "既に存在します" in cr.json()["detail"]

    after = store.get_user_by_uid(victim_uid)
    assert after["role"] == "user"
    assert after["status"] == "disabled"
    assert after["password_hash"] == before["password_hash"]
    assert auth.verify_password(original_pw, after["password_hash"])
    assert not auth.verify_password("attacker-pass-1", after["password_hash"])


def test_store_create_user_returns_none_on_existing_uid_without_upserting():
    uid, _ = _mk_user(_sfx())
    before = store.get_user_by_uid(uid)
    result = store.create_user(uid, display_name="Should Not Apply",
                               password_hash=auth.hash_password("should-not-apply"), role="admin")
    assert result is None
    after = store.get_user_by_uid(uid)
    for k in ("role", "password_hash", "display_name"):
        assert after[k] == before[k]


# ===== 共有 =====

def test_share_click_wrapper_read_append_denied_and_revoke():
    """共有作成→招待外は click 403→invitee が click→受領ラッパー→本文読取→append 403→取消後は unavailable。"""
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"o{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"i{sfx}")
    other_uid, other_pw = _mk_user(f"ot{sfx}")
    cid = _conv(owner_uid, "テスト会話", "テスト質問", "テスト回答")
    owner_cookies = _cookies(owner_uid, owner_pw)

    sr = _share(owner_cookies, cid, invitee_uid,
                expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
    assert sr.status_code == 200, sr.text
    share_url = sr.json()["url"]
    assert share_url.startswith("/share/conversations/")

    other = client.get(share_url, cookies=_cookies(other_uid, other_pw), follow_redirects=False)
    assert other.status_code == 403, other.status_code

    inv_cookies = _cookies(invitee_uid, invitee_pw)
    cr = client.get(share_url, cookies=inv_cookies, follow_redirects=False)
    assert cr.status_code in (200, 302), f"share click failed: {cr.status_code} {cr.text}"
    received = _received(invitee_uid)
    assert len(received) == 1, received
    wid = received[0]["id"]

    gr = client.get(f"/conversations/{wid}", cookies=inv_cookies)
    assert gr.status_code == 200, gr.text
    assert len(gr.json()["messages"]) == 2

    append_r = client.post("/chat/turns", cookies=inv_cookies,
                           json={"message": "追記", "world": "v1", "conversation_id": wid})
    assert append_r.status_code == 403, append_r.status_code

    assert client.post(f"/conversation-shares/{sr.json()['share_id']}/revoke",
                       cookies=owner_cookies).status_code == 200
    data = client.get(f"/conversations/{wid}", cookies=inv_cookies).json()
    assert data.get("share_status") == "unavailable" or data.get("messages") == []


def test_share_default_30d_survives_source_delete_and_soft_deleted_rejects_owner_ops():
    """expires_at 省略は作成+30日（明示 null は 422）。共有元を削除しても受領側は読め、
    soft-delete 後は owner の GET/PIN/rename が 404・共有作成が拒否される（PIN の deleted_at 漏れの回帰）。"""
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"ueo{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"uei{sfx}")
    stranger_uid, _ = _mk_user(f"ues{sfx}")
    cid = _conv(owner_uid, "無期限共有API", "質問", "回答")
    owner_cookies = _cookies(owner_uid, owner_pw)

    nr = _share(owner_cookies, cid, invitee_uid, expires_at=None)
    assert nr.status_code == 422, nr.text
    sr = _share(owner_cookies, cid, invitee_uid)
    assert sr.status_code == 200, sr.text
    lst = store.list_shares_for_conversation(owner_uid, cid)
    assert len(lst) == 1
    delta = lst[0]["expires_at"] - lst[0]["created_at"]
    assert timedelta(days=30) - timedelta(seconds=5) <= delta <= timedelta(days=30) + timedelta(seconds=5)

    inv_cookies = _cookies(invitee_uid, invitee_pw)
    cr = client.get(sr.json()["url"], cookies=inv_cookies, follow_redirects=False)
    assert cr.status_code in (200, 302), f"share click failed: {cr.status_code} {cr.text}"
    received = _received(invitee_uid)
    assert received and received[0]["share_status"] == "active"
    wid = received[0]["id"]

    dr = client.delete(f"/conversations/{cid}", cookies=owner_cookies)
    assert dr.status_code == 200, dr.text
    gr = client.get(f"/conversations/{wid}", cookies=inv_cookies)
    assert gr.status_code == 200, gr.text
    assert len(gr.json()["messages"]) == 2, "元会話削除後に受領側が読めなくなった"
    owner_list = client.get("/conversations", cookies=owner_cookies)
    assert owner_list.status_code == 200
    assert cid not in [c["id"] for c in owner_list.json()]

    assert client.get(f"/conversations/{cid}", cookies=owner_cookies).status_code == 404
    assert client.post(f"/conversations/{cid}/pin", json={"pinned": True},
                       cookies=owner_cookies).status_code == 404
    assert client.patch(f"/conversations/{cid}", json={"title": "改ざんタイトル"},
                        cookies=owner_cookies).status_code == 404
    sr2 = _share(owner_cookies, cid, stranger_uid)
    assert sr2.status_code in (403, 409), f"{sr2.status_code} {sr2.text}"


def _audit_insert_fails(monkeypatch, action: str):
    real = store._audit_insert

    def boom(c, actor, act, *a, **kw):
        if act == action:
            raise RuntimeError("audit down")
        return real(c, actor, act, *a, **kw)
    monkeypatch.setattr(store, "_audit_insert", boom)


def test_share_revoke_audit_failure_keeps_share_active(monkeypatch):
    """取消と監査は同一トランザクション: 監査が失敗したら 500 で取消も戻る。"""
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"rao{sfx}")
    invitee_uid, _ = _mk_user(f"rai{sfx}")
    cid = _conv(owner_uid, "取消監査", "q")
    cookies = _cookies(owner_uid, owner_pw)
    sid = _share(cookies, cid, invitee_uid).json()["share_id"]

    _audit_insert_fails(monkeypatch, "share.revoked")
    with TestClient(app, raise_server_exceptions=False) as c2:
        r = c2.post(f"/conversation-shares/{sid}/revoke", cookies=cookies)
    assert r.status_code == 500
    assert _share_row(owner_uid, cid, sid)["revoked_at"] is None


def test_share_receive_audit_failure_creates_no_wrapper(monkeypatch):
    """受領と監査は同一トランザクション: 監査が失敗したら 500 で受領ラッパーも作られない。"""
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"rbo{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"rbi{sfx}")
    cid = _conv(owner_uid, "受領監査", "q")
    url = _share(_cookies(owner_uid, owner_pw), cid, invitee_uid).json()["url"]
    icookies = _cookies(invitee_uid, invitee_pw)

    _audit_insert_fails(monkeypatch, "share.accepted")
    with TestClient(app, raise_server_exceptions=False) as c2:
        r = c2.get(url, cookies=icookies, follow_redirects=False)
    assert r.status_code == 500
    assert _received(invitee_uid) == []


# ===== 設定・監査 =====

def test_settings_isolated_per_user():
    sfx = _sfx()
    u1, p1 = _mk_user(f"s1{sfx}")
    u2, p2 = _mk_user(f"s2{sfx}")
    c1, c2 = _cookies(u1, p1), _cookies(u2, p2)
    assert client.put("/settings", json={"codex_model_provider": "ollama"}, cookies=c1).status_code == 200
    get2 = client.get("/settings", cookies=c2)
    assert get2.status_code == 200
    assert get2.json()["codex_model_provider"] != "ollama"


def test_audit_row_written_for_login_and_share():
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"au{sfx}")
    inv_uid, _ = _mk_user(f"av{sfx}")
    cookies = _cookies(owner_uid, owner_pw)
    cid = _conv(owner_uid, "監査テスト会話")
    sr = _share(cookies, cid, inv_uid, expires_at=(datetime.now(timezone.utc) + timedelta(days=7)).isoformat())
    assert sr.status_code == 200
    actions = [r["action"] for r in store.list_audit(actor=owner_uid, limit=10)]
    assert "auth.login" in actions, actions
    assert "share.created" in actions, actions


def test_admin_audit_endpoint():
    """GET /admin/audit: admin は閲覧可・non-admin は 403・閲覧自体が admin.audit_viewed として記録される。"""
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    usr_uid, usr_pw = _mk_user(sfx)
    r = client.get("/admin/audit?limit=10", cookies=_cookies(adm_uid, adm_pw))
    assert r.status_code == 200, r.text
    assert isinstance(r.json()["rows"], list)
    assert client.get("/admin/audit?limit=10", cookies=_cookies(usr_uid, usr_pw)).status_code == 403
    rows = store.list_audit(actor=adm_uid, action="admin.audit_viewed", limit=10)
    assert any(r["action"] == "admin.audit_viewed" for r in rows), rows


def test_auth_disabled_compat(auth_disabled):
    me = client.get("/auth/me")
    assert me.status_code == 200
    assert me.json()["uid"] == "admin"
    assert me.json()["role"] == "admin"


# ===== 期限前の通知・延長 =====

def _share_via_api(prefix: str):
    sfx = _sfx()
    owner_uid, owner_pw = _mk_user(f"{prefix}o{sfx}")
    invitee_uid, invitee_pw = _mk_user(f"{prefix}i{sfx}")
    cid = _conv(owner_uid, f"延長{sfx}", "q")
    ocookies = _cookies(owner_uid, owner_pw)
    r = _share(ocookies, cid, invitee_uid)
    assert r.status_code == 200, r.text
    return owner_uid, invitee_uid, invitee_pw, cid, r.json()["share_id"], r.json()["url"], ocookies


def _expiring(**kw) -> list[dict]:
    return [n for n in notifications.list_notifications(is_admin=False, **kw) if n["kind"] == "share_expiring"]


def test_share_expiry_notice_owner_only_within_7_days_incl_legacy_null():
    owner, invitee, _, cid, sid, _, _ = _share_via_api("na")
    _sql("UPDATE conversation_shares SET expires_at=now() + interval '8 days' WHERE id=%s", sid)
    assert _expiring(uid=owner) == []
    # 旧 NULL 行（作成 25 日前＝残り 5 日）→ 所有者にだけ出る
    _sql("UPDATE conversation_shares SET expires_at=NULL, created_at=now() - interval '25 days' WHERE id=%s", sid)
    got = _expiring(uid=owner)
    assert len(got) == 1 and "5日後" in got[0]["message"] and f"conv={cid}" in got[0]["link"]
    assert _expiring(uid=invitee) == []
    assert _expiring() == []


def test_share_extend_sets_now_plus_days_and_rejects_bad_requests():
    owner, invitee, invitee_pw, cid, sid, _, ocookies = _share_via_api("ne")
    ext = f"/conversation-shares/{sid}/extend"
    assert client.post(ext, json={"days": 31}, cookies=ocookies).status_code == 422
    assert client.post(ext, json={"days": 0}, cookies=ocookies).status_code == 422
    r = client.post(ext, json={"days": 10}, cookies=ocookies)
    assert r.status_code == 200, r.text
    delta = _share_row(owner, cid, sid)["expires_at"] - datetime.now(timezone.utc)
    assert timedelta(days=10) - timedelta(minutes=1) <= delta <= timedelta(days=10)
    # 非所有者・不在・取消済みは 404（存在を漏らさない）
    assert client.post(ext, json={}, cookies=_cookies(invitee, invitee_pw)).status_code == 404
    assert client.post("/conversation-shares/999999999/extend", json={}, cookies=ocookies).status_code == 404
    assert client.post(f"/conversation-shares/{sid}/revoke", cookies=ocookies).status_code == 200
    assert client.post(ext, json={}, cookies=ocookies).status_code == 404


def test_share_extend_audit_failure_keeps_expiry(monkeypatch):
    owner, _, _, cid, sid, _, ocookies = _share_via_api("nf")
    before = _share_row(owner, cid, sid)["expires_at"]
    _sql("UPDATE conversation_shares SET expires_at=now() + interval '2 days' WHERE id=%s", sid)
    _audit_insert_fails(monkeypatch, "share.extended")
    with TestClient(app, raise_server_exceptions=False) as c2:
        r = c2.post(f"/conversation-shares/{sid}/extend", json={"days": 30}, cookies=ocookies)
    assert r.status_code == 500
    after = _share_row(owner, cid, sid)["expires_at"]
    assert after - datetime.now(timezone.utc) < timedelta(days=3) and after < before


def test_recipient_regains_access_after_extending_expired_share():
    owner, invitee, invitee_pw, cid, sid, url, ocookies = _share_via_api("nr")
    icookies = _cookies(invitee, invitee_pw)
    assert client.get(url, cookies=icookies, follow_redirects=False).status_code in (200, 302)
    wid = _received(invitee)[0]["id"]
    _sql("UPDATE conversation_shares SET expires_at=now() - interval '1 day' WHERE id=%s", sid)
    assert store.get_conversation_for_read(invitee, wid)["share_status"] == "unavailable"
    assert client.post(f"/conversation-shares/{sid}/extend", json={}, cookies=ocookies).status_code == 200
    rec = [c for c in store.list_conversations(invitee) if c["id"] == wid][0]
    assert rec["share_status"] == "active" and rec["share_expires_at"] is not None
    assert len(store.get_conversation_for_read(invitee, wid)["messages"]) == 1
