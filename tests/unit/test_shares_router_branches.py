"""sherpa/routers/shares.py の分岐カバレッジ（フェーズ7 S6・14%→引き上げ）。

TestClient を経由せず、ルートハンドラ関数を直接呼ぶ（DB 非依存＝store/auth.deps を monkeypatch）。
`_current_user`/`_client_ip_hash`/`_synthetic_admin` は `sherpa.deps` からの直接束縛（`from
sherpa.deps import ...`）のため、router モジュール側の名前（`shares_routes._current_user` 等）を
monkeypatch する（facade 経由ではない通常 import＝tests/unit/phase5-agents-split-notes.md と同じ事情）。
`store`/`auth` は `from sherpa import auth, store`（モジュール import）のため `store.xxx` への
monkeypatch がそのまま効く。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from sherpa.routers import shares as shares_routes


def _req(cookies=None, headers=None, host="127.0.0.1"):
    return SimpleNamespace(
        cookies=cookies or {},
        headers=headers or {},
        client=SimpleNamespace(host=host) if host else None,
    )


def _user(uid="u1"):
    return {"uid": uid, "email": None, "display_name": uid, "role": "user", "status": "active",
            "must_change_password": False}


@pytest.fixture(autouse=True)
def _default_current_user(monkeypatch):
    monkeypatch.setattr(shares_routes, "_current_user", lambda request, **kw: _user())
    yield


# ===== GET /users/suggest =====

def test_users_suggest_empty_query_returns_empty_without_store_call(monkeypatch):
    calls = []
    monkeypatch.setattr(shares_routes.store, "suggest_users", lambda *a, **kw: calls.append((a, kw)) or [])
    out = shares_routes.users_suggest(_req(), q="   ")
    assert out == {"users": []}
    assert calls == []          # 空クエリは store を叩かない


def test_users_suggest_forwards_query_and_excludes_self(monkeypatch):
    seen = {}

    def _fake_suggest(q, exclude_uid, limit=10):
        seen["args"] = (q, exclude_uid, limit)
        return [{"uid": "u2", "display_name": "U2"}]

    monkeypatch.setattr(shares_routes.store, "suggest_users", _fake_suggest)
    out = shares_routes.users_suggest(_req(), q=" tar ")
    assert out == {"users": [{"uid": "u2", "display_name": "U2"}]}
    assert seen["args"] == ("tar", "u1", 10)   # router 側で strip() 済みの q が渡る


# ===== POST /conversations/{cid}/shares =====

def _share_req(invitee=("u2",), expires_at=None, sanitize=False):
    return shares_routes.ShareCreateReq(invitee_user_ids=list(invitee), expires_at=expires_at, sanitize=sanitize)


def test_share_create_not_owner_denies_403(monkeypatch):
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: False)
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(), _req())
    assert ei.value.status_code == 403
    assert audit_calls[0][0][1] == "share.created" and audit_calls[0][1]["reason"] == "not_owner"


def test_share_create_sanitize_missing_source_returns_404(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read", lambda uid, cid: None)
    monkeypatch.setattr(shares_routes.store, "create_sanitized_snapshot", lambda uid, cid: None)
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(sanitize=True), _req())
    assert ei.value.status_code == 404


def test_share_create_sanitize_success_shares_snapshot_not_source(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read", lambda uid, cid: None)
    monkeypatch.setattr(shares_routes.store, "create_sanitized_snapshot", lambda uid, cid: 999)
    monkeypatch.setattr(shares_routes.store, "get_user",
                        lambda uid: {"uid": uid, "status": "active"})
    create_calls = []
    monkeypatch.setattr(shares_routes.store, "create_share",
                        lambda cid, owner, th, exp, invitees, **kw: create_calls.append(cid) or 5)
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    out = shares_routes.conversation_share_create(1, _share_req(sanitize=True), _req())
    assert out["ok"] is True and out["share_id"] == 5
    assert create_calls == [999]                                # snapshot cid が共有対象（source ではない）
    actions = [c[0][1] for c in audit_calls]
    assert "share.sanitized_snapshot" in actions and "share.created" in actions
    created_detail = next(c[1]["detail"] for c in audit_calls if c[0][1] == "share.created")
    assert created_detail["sanitized"] is True and created_detail["source_conversation_id"] == 1


def test_share_create_personal_workspace_flag_denies_409(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read",
                        lambda uid, cid: {"conversation": {"contains_personal_workspace": True}})
    monkeypatch.setattr(shares_routes.store, "conversation_has_personal_message", lambda cid: False)
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(), _req())
    assert ei.value.status_code == 409
    assert audit_calls[0][1]["reason"] == "contains_personal_workspace"


def test_share_create_personal_message_flag_denies_409_even_if_conv_flag_false(monkeypatch):
    """多層防御: 会話フラグは false でも messages.personal が1件あれば拒否する。"""
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read",
                        lambda uid, cid: {"conversation": {"contains_personal_workspace": False}})
    monkeypatch.setattr(shares_routes.store, "conversation_has_personal_message", lambda cid: True)
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: None)
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(), _req())
    assert ei.value.status_code == 409


def test_share_create_empty_invitees_422(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read",
                        lambda uid, cid: {"conversation": {"contains_personal_workspace": False}})
    monkeypatch.setattr(shares_routes.store, "conversation_has_personal_message", lambda cid: False)
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(invitee=()), _req())
    assert ei.value.status_code == 422


def test_share_create_invalid_invitee_422(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read",
                        lambda uid, cid: {"conversation": {"contains_personal_workspace": False}})
    monkeypatch.setattr(shares_routes.store, "conversation_has_personal_message", lambda cid: False)
    monkeypatch.setattr(shares_routes.store, "get_user", lambda uid: None)   # 存在しない招待先
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(invitee=("ghost",)), _req())
    assert ei.value.status_code == 422
    assert audit_calls[0][1]["reason"] == "invitee_invalid"


def test_share_create_invitee_disabled_status_422(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read",
                        lambda uid, cid: {"conversation": {"contains_personal_workspace": False}})
    monkeypatch.setattr(shares_routes.store, "conversation_has_personal_message", lambda cid: False)
    monkeypatch.setattr(shares_routes.store, "get_user", lambda uid: {"uid": uid, "status": "disabled"})
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: None)
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(invitee=("disabled_user",)), _req())
    assert ei.value.status_code == 422


def test_share_create_success_normalizes_naive_expires_to_utc(monkeypatch):
    from datetime import datetime, timezone

    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read",
                        lambda uid, cid: {"conversation": {"contains_personal_workspace": False}})
    monkeypatch.setattr(shares_routes.store, "conversation_has_personal_message", lambda cid: False)
    monkeypatch.setattr(shares_routes.store, "get_user", lambda uid: {"uid": uid, "status": "active"})
    seen = {}

    def _fake_create_share(cid, owner, th, expires, invitees, **kw):
        seen["expires"] = expires
        return 7

    monkeypatch.setattr(shares_routes.store, "create_share", _fake_create_share)
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: None)
    naive = datetime(2030, 1, 1, 9, 0, 0)   # tzinfo なし
    out = shares_routes.conversation_share_create(1, _share_req(expires_at=naive), _req())
    assert out["ok"] is True and out["url"].startswith("/share/conversations/")
    assert seen["expires"].tzinfo == timezone.utc


def test_share_create_audit_failure_revokes_and_500(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "owns_conversation", lambda uid, cid: True)
    monkeypatch.setattr(shares_routes.store, "get_conversation_for_read",
                        lambda uid, cid: {"conversation": {"contains_personal_workspace": False}})
    monkeypatch.setattr(shares_routes.store, "conversation_has_personal_message", lambda cid: False)
    monkeypatch.setattr(shares_routes.store, "get_user", lambda uid: {"uid": uid, "status": "active"})
    monkeypatch.setattr(shares_routes.store, "create_share", lambda *a, **kw: 42)
    revoke_calls = []
    monkeypatch.setattr(shares_routes.store, "revoke_share", lambda sid, owner: revoke_calls.append((sid, owner)))

    def _boom(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(shares_routes.store, "audit", _boom)
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_create(1, _share_req(), _req())
    assert ei.value.status_code == 500
    assert revoke_calls == [(42, "u1")]      # fail-closed: 作成済み共有を取消す


# ===== GET /share/conversations/{token} =====

def test_share_click_no_cookie_redirects_to_login(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: False)
    out = shares_routes.share_click("tok", _req())
    assert out.status_code == 302 and "/ui/login.html" in out.headers["location"]


def test_share_click_invalid_session_redirects_to_login(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: False)
    monkeypatch.setattr(shares_routes.store, "session_user", lambda th: None)
    out = shares_routes.share_click("tok", _req(cookies={"sherpa_session": "badtok"}))
    assert out.status_code == 302 and "/ui/login.html" in out.headers["location"]


def test_share_click_auth_disabled_uses_synthetic_admin(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: True)
    monkeypatch.setattr(shares_routes, "_synthetic_admin", lambda: _user("admin"))
    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token", lambda th: None)
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: None)
    with pytest.raises(HTTPException) as ei:
        shares_routes.share_click("tok", _req())          # cookie 無くても synthetic admin で進む
    assert ei.value.status_code == 403


def test_share_click_missing_or_inactive_share_403(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: True)
    monkeypatch.setattr(shares_routes, "_synthetic_admin", lambda: _user("admin"))
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))

    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token", lambda th: None)
    with pytest.raises(HTTPException):
        shares_routes.share_click("tok", _req())
    assert audit_calls[-1][1]["reason"] == "invalid_token"

    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token",
                        lambda th: {"id": 1, "active": False, "revoked_at": "2026-01-01"})
    with pytest.raises(HTTPException):
        shares_routes.share_click("tok", _req())
    assert audit_calls[-1][1]["reason"] == "revoked"

    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token",
                        lambda th: {"id": 1, "active": False, "revoked_at": None})
    with pytest.raises(HTTPException):
        shares_routes.share_click("tok", _req())
    assert audit_calls[-1][1]["reason"] == "expired"


def test_share_click_not_invited_403(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: True)
    monkeypatch.setattr(shares_routes, "_synthetic_admin", lambda: _user("admin"))
    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token",
                        lambda th: {"id": 1, "active": True, "conversation_id": 10})
    monkeypatch.setattr(shares_routes.store, "is_invited", lambda sid, uid: False)
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    with pytest.raises(HTTPException) as ei:
        shares_routes.share_click("tok", _req())
    assert ei.value.status_code == 403 and audit_calls[-1][1]["reason"] == "not_invited"


def test_share_click_source_gone_403(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: True)
    monkeypatch.setattr(shares_routes, "_synthetic_admin", lambda: _user("admin"))
    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token",
                        lambda th: {"id": 1, "active": True, "conversation_id": 10})
    monkeypatch.setattr(shares_routes.store, "is_invited", lambda sid, uid: True)

    def _boom(sid, uid):
        raise ValueError("共有元の会話が見つかりません")

    monkeypatch.setattr(shares_routes.store, "accept_share", _boom)
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    with pytest.raises(HTTPException) as ei:
        shares_routes.share_click("tok", _req())
    assert ei.value.status_code == 403 and audit_calls[-1][1]["reason"] == "source_gone"


def test_share_click_success_redirects_to_chat(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: True)
    monkeypatch.setattr(shares_routes, "_synthetic_admin", lambda: _user("admin"))
    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token",
                        lambda th: {"id": 1, "active": True, "conversation_id": 10})
    monkeypatch.setattr(shares_routes.store, "is_invited", lambda sid, uid: True)
    monkeypatch.setattr(shares_routes.store, "accept_share", lambda sid, uid: 55)
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: None)
    out = shares_routes.share_click("tok", _req())
    assert out.status_code == 302 and "/ui/chat.html?conversation_id=55" in out.headers["location"]


def test_share_click_accept_success_but_audit_failure_500(monkeypatch):
    monkeypatch.setattr(shares_routes.auth, "auth_disabled", lambda: True)
    monkeypatch.setattr(shares_routes, "_synthetic_admin", lambda: _user("admin"))
    monkeypatch.setattr(shares_routes.store, "resolve_share_by_token",
                        lambda th: {"id": 1, "active": True, "conversation_id": 10})
    monkeypatch.setattr(shares_routes.store, "is_invited", lambda sid, uid: True)
    monkeypatch.setattr(shares_routes.store, "accept_share", lambda sid, uid: 55)

    def _boom(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(shares_routes.store, "audit", _boom)
    with pytest.raises(HTTPException) as ei:
        shares_routes.share_click("tok", _req())
    assert ei.value.status_code == 500


# ===== POST /conversation-shares/{share_id}/revoke =====

def test_conversation_share_revoke_not_owner_or_already_revoked_403(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "revoke_share", lambda sid, uid: False)
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_revoke(5, _req())
    assert ei.value.status_code == 403
    assert audit_calls[0][1]["reason"] == "not_owner_or_already_revoked"


def test_conversation_share_revoke_success(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "revoke_share", lambda sid, uid: True)
    audit_calls = []
    monkeypatch.setattr(shares_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    out = shares_routes.conversation_share_revoke(5, _req())
    assert out == {"ok": True, "share_id": 5}
    assert audit_calls[0][0][1] == "share.revoked" and audit_calls[0][1]["outcome"] == "success"


def test_conversation_share_revoke_audit_failure_500(monkeypatch):
    monkeypatch.setattr(shares_routes.store, "revoke_share", lambda sid, uid: True)

    def _boom(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(shares_routes.store, "audit", _boom)
    with pytest.raises(HTTPException) as ei:
        shares_routes.conversation_share_revoke(5, _req())
    assert ei.value.status_code == 500
