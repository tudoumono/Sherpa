"""sherpa/routers/auth.py の分岐カバレッジ（フェーズ7 S6・19%→引き上げ）。

TestClient を経由せず、ルートハンドラ関数を直接呼ぶ（DB 非依存＝store/ratelimit を monkeypatch）。
`_current_user`/`_ensure_initial_admin`/`_validate_new_password` は `sherpa.deps` からの直接束縛
（`from sherpa.deps import ...`）のため、router モジュール側の名前（`auth_routes._current_user` 等）を
monkeypatch する（facade 経由ではない通常 import＝phase5-agents-split-notes.md と同じ事情）。
`store`/`ratelimit`/`auth` は `from sherpa import auth, ratelimit, store`（モジュール import）のため
`store.xxx`/`ratelimit.xxx` への monkeypatch がそのまま効く。ratelimit は実装（プロセス内メモリ）を
そのまま使い、`_reset_for_tests()` で各テスト間の汚染を防ぐ。
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest
from fastapi import HTTPException, Response

from sherpa import auth as auth_lib
from sherpa import ratelimit
from sherpa.routers import auth as auth_routes


def _req(cookies=None, headers=None, host="127.0.0.1", scheme="http"):
    return SimpleNamespace(
        cookies=cookies or {},
        headers=headers or {},
        client=SimpleNamespace(host=host) if host else None,
        url=SimpleNamespace(scheme=scheme),
    )


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    ratelimit._reset_for_tests()
    # 初期 admin bootstrap は本テストの対象外（test_auth_defaults.py が別途担当）＝no-op にして
    # login 分岐テストから隔離する。
    monkeypatch.setattr(auth_routes, "_ensure_initial_admin", lambda **kw: None)
    yield
    ratelimit._reset_for_tests()


# ===== POST /auth/login =====

def test_login_lockout_blocks_before_password_check(monkeypatch):
    """ロックアウトはパスワード照合より前段でブロックする（正しいパスワードでも問答無用で拒否）。"""
    audit_calls = []
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    guard_calls = []
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid", lambda uid: guard_calls.append(uid))
    for _ in range(ratelimit.LOGIN_FAIL_THRESHOLD):
        ratelimit.record_login_failure("lockeduser")

    req = auth_routes.LoginReq(username="lockeduser", password="whatever")
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_login(req, _req(), Response())
    assert ei.value.status_code == 429
    assert "Retry-After" in ei.value.headers
    assert guard_calls == []                      # 照合ロジックへ到達しない
    assert audit_calls[-1][0][1] == "auth.login_failed" and audit_calls[-1][1]["reason"] == "rate_limited"


def test_login_user_not_found_401(monkeypatch):
    audit_calls = []
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid", lambda uid: None)
    req = auth_routes.LoginReq(username="ghost", password="whatever")
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_login(req, _req(), Response())
    assert ei.value.status_code == 401
    assert audit_calls[-1][1]["reason"] == "user_not_found"


def test_login_missing_password_hash_treated_as_not_found(monkeypatch):
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "status": "active", "password_hash": None})
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_login(auth_routes.LoginReq(username="u", password="x"), _req(), Response())
    assert ei.value.status_code == 401


def test_login_disabled_user_401(monkeypatch):
    audit_calls = []
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "status": "disabled", "password_hash": ph})
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_login(auth_routes.LoginReq(username="u", password="Correct123!"), _req(), Response())
    assert ei.value.status_code == 401
    assert audit_calls[-1][1]["reason"] == "user_disabled"


def test_login_bad_password_401(monkeypatch):
    audit_calls = []
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "status": "active", "password_hash": ph})
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_login(auth_routes.LoginReq(username="u", password="Wrong123!"), _req(), Response())
    assert ei.value.status_code == 401
    assert audit_calls[-1][1]["reason"] == "bad_credentials"


def test_login_race_lockout_after_password_check_429(monkeypatch):
    """監査台帳 MED-2: 照合成功直後のロックアウト再確認（別リクエストの失敗で成立した場合）。"""
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "status": "active", "password_hash": ph})
    calls = {"n": 0}

    def _check(uid):
        calls["n"] += 1
        return None if calls["n"] == 1 else 30.0

    monkeypatch.setattr(auth_routes.ratelimit, "check_login_lockout", _check)
    success_calls = []
    monkeypatch.setattr(auth_routes.ratelimit, "record_login_success", lambda uid: success_calls.append(uid))
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_login(auth_routes.LoginReq(username="u", password="Correct123!"), _req(), Response())
    assert ei.value.status_code == 429
    assert success_calls == []                     # ロック中はセッション発行に進まない


def test_login_success_must_change_password_sets_next_and_cookie(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "status": "active", "password_hash": ph,
                                     "must_change_password": True})
    session_calls = []
    monkeypatch.setattr(auth_routes.store, "create_session",
                        lambda uid, th, exp: session_calls.append((uid, th, exp)))
    monkeypatch.setattr(auth_routes.store, "set_last_login", lambda uid: None)
    resp = Response()
    out = auth_routes.auth_login(auth_routes.LoginReq(username="u1", password="Correct123!"), _req(), resp)
    assert out == {"ok": True, "uid": "u1", "must_change_password": True,
                   "next": "/ui/change-password.html"}
    assert session_calls and session_calls[0][0] == "u1"
    assert resp.headers.get("set-cookie")           # cookie が発行される


def test_login_success_without_must_change_password_next_is_none(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "status": "active", "password_hash": ph,
                                     "must_change_password": False})
    monkeypatch.setattr(auth_routes.store, "create_session", lambda *a, **kw: None)
    monkeypatch.setattr(auth_routes.store, "set_last_login", lambda uid: None)
    out = auth_routes.auth_login(auth_routes.LoginReq(username="u2", password="Correct123!"), _req(), Response())
    assert out["must_change_password"] is False and out["next"] is None


def test_login_success_audit_failure_revokes_session_fail_closed(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "status": "active", "password_hash": ph,
                                     "must_change_password": False})
    monkeypatch.setattr(auth_routes.store, "create_session", lambda *a, **kw: None)
    monkeypatch.setattr(auth_routes.store, "set_last_login", lambda uid: None)
    revoked = []
    monkeypatch.setattr(auth_routes.store, "revoke_session", lambda th: revoked.append(th))

    def _boom(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(auth_routes.store, "audit", _boom)
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_login(auth_routes.LoginReq(username="u3", password="Correct123!"), _req(), Response())
    assert ei.value.status_code == 500
    assert revoked                                  # fail-closed: セッションを取り消す


# ===== GET /auth/me =====

def test_auth_me_reflects_auth_disabled_flag(monkeypatch):
    monkeypatch.setattr(auth_routes, "_current_user",
                        lambda request, **kw: {"uid": "u1", "email": "u1@x", "display_name": "U1",
                                                "role": "user", "must_change_password": False})
    monkeypatch.setattr(auth_lib, "auth_disabled", lambda: True)
    out = auth_routes.auth_me(_req())
    assert out == {"uid": "u1", "email": "u1@x", "display_name": "U1", "role": "user",
                   "must_change_password": False, "auth_disabled": True}


# ===== POST /auth/logout =====

def test_logout_auth_disabled_clears_cookie_and_skips_revoke(monkeypatch):
    monkeypatch.setattr(auth_lib, "auth_disabled", lambda: True)
    revoke_calls = []
    monkeypatch.setattr(auth_routes.store, "revoke_session", lambda th: revoke_calls.append(th))
    resp = Response()
    out = auth_routes.auth_logout(_req(cookies={"sherpa_session": "tok"}), resp)
    assert out == {"ok": True}
    assert revoke_calls == []
    assert resp.headers.get("set-cookie")           # delete_cookie も Set-Cookie を出す


def test_logout_no_token_skips_revoke(monkeypatch):
    monkeypatch.setattr(auth_lib, "auth_disabled", lambda: False)
    revoke_calls = []
    monkeypatch.setattr(auth_routes.store, "revoke_session", lambda th: revoke_calls.append(th))
    out = auth_routes.auth_logout(_req(), Response())
    assert out == {"ok": True}
    assert revoke_calls == []


def test_logout_with_token_revokes_and_audits(monkeypatch):
    monkeypatch.setattr(auth_lib, "auth_disabled", lambda: False)
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "u1"})
    revoke_calls, audit_calls = [], []
    monkeypatch.setattr(auth_routes.store, "revoke_session", lambda th: revoke_calls.append(th))
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    out = auth_routes.auth_logout(_req(cookies={"sherpa_session": "tok"}), Response())
    assert out == {"ok": True}
    assert revoke_calls == [auth_lib.token_hash("tok")]
    assert audit_calls[0][0][1] == "auth.logout"


def test_logout_audit_failure_is_best_effort_not_fail_closed(monkeypatch):
    """logout の監査失敗は warning ログのみ（login/change-password と異なり fail-closed にしない）。"""
    monkeypatch.setattr(auth_lib, "auth_disabled", lambda: False)
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "u1"})
    monkeypatch.setattr(auth_routes.store, "revoke_session", lambda th: None)

    def _boom(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(auth_routes.store, "audit", _boom)
    out = auth_routes.auth_logout(_req(cookies={"sherpa_session": "tok"}), Response())
    assert out == {"ok": True}                      # 例外は伝播しない（best-effort）


# ===== POST /auth/change-password =====

def _pw_req(current="Correct123!", new="BrandNew123!", confirm="BrandNew123!"):
    return auth_routes.PasswordChangeReq(current_password=current, new_password=new, confirm_password=confirm)


def test_change_password_no_db_user_401(monkeypatch):
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "ghost"})
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid", lambda uid: None)
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_change_password(_pw_req(), _req())
    assert ei.value.status_code == 401


def test_change_password_bad_current_password_401(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "u1"})
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "role": "user", "status": "active", "password_hash": ph})
    audit_calls = []
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_change_password(_pw_req(current="Wrong!"), _req())
    assert ei.value.status_code == 401
    assert audit_calls[-1][1]["reason"] == "bad_current_password"


def test_change_password_weak_new_password_422(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "u1"})
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "role": "user", "status": "active", "password_hash": ph})
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: None)
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_change_password(_pw_req(new="short", confirm="short"), _req())
    assert ei.value.status_code == 422


def test_change_password_success_initial_marks_critical(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "u1"})
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "role": "user", "status": "active",
                                     "password_hash": ph, "must_change_password": True})
    upsert_calls = []
    monkeypatch.setattr(auth_routes.store, "upsert_user",
                        lambda uid, **kw: upsert_calls.append((uid, kw)))
    audit_calls = []
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    out = auth_routes.auth_change_password(_pw_req(), _req())
    assert out == {"ok": True, "uid": "u1", "must_change_password": False}
    assert upsert_calls[0][1]["must_change_password"] is False
    assert audit_calls[0][0][1] == "auth.initial_password_changed"
    assert audit_calls[0][1]["severity"] == "critical"


def test_change_password_success_not_initial_marks_info(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "u1"})
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "role": "user", "status": "active",
                                     "password_hash": ph, "must_change_password": False})
    monkeypatch.setattr(auth_routes.store, "upsert_user", lambda uid, **kw: None)
    audit_calls = []
    monkeypatch.setattr(auth_routes.store, "audit", lambda *a, **kw: audit_calls.append((a, kw)))
    out = auth_routes.auth_change_password(_pw_req(), _req())
    assert out["must_change_password"] is False
    assert audit_calls[0][0][1] == "auth.password_changed"
    assert audit_calls[0][1]["severity"] == "info"


def test_change_password_audit_failure_500(monkeypatch):
    ph = auth_lib.hash_password("Correct123!")
    monkeypatch.setattr(auth_routes, "_current_user", lambda request, **kw: {"uid": "u1"})
    monkeypatch.setattr(auth_routes.store, "get_user_by_uid",
                        lambda uid: {"uid": uid, "role": "user", "status": "active",
                                     "password_hash": ph, "must_change_password": False})
    monkeypatch.setattr(auth_routes.store, "upsert_user", lambda uid, **kw: None)

    def _boom(*a, **kw):
        raise RuntimeError("audit down")

    monkeypatch.setattr(auth_routes.store, "audit", _boom)
    with pytest.raises(HTTPException) as ei:
        auth_routes.auth_change_password(_pw_req(), _req())
    assert ei.value.status_code == 500
