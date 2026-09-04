"""セッション cookie の Secure 属性（2026-08-17・閉域網の平文 HTTP 公開）。

以前は SHERPA_ENV=production なら常に Secure が付き、平文 http://<IP> ではログインできなかった。
契約: 未指定なら「その要求が HTTPS で来たときだけ Secure」。SHERPA_COOKIE_SECURE=1/0 で強制できる。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app

pytestmark = pytest.mark.api


def _mk_user():
    try:
        store.init_schema()
    except Exception as e:  # noqa: BLE001
        pytest.skip(f"DB down: {e}")
    sfx = str(int(time.time() * 1000))[-8:]
    uid, pw = f"cs{sfx}", f"pw-cs{sfx}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name="Cookie Test",
                      password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid)
    return uid, pw


def _login_cookie(base_url: str, uid: str, pw: str) -> str:
    c = TestClient(app, base_url=base_url, raise_server_exceptions=True)
    r = c.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    sc = r.headers.get("set-cookie", "")
    assert "sherpa_session=" in sc, sc
    return sc.lower()


def test_http_login_gets_non_secure_cookie_even_in_production(monkeypatch):
    """平文 HTTP で来たログインには Secure を付けない（＝閉域 LAN の HTTP 公開でログインできる）。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.delenv("SHERPA_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SHERPA_AUTH_DISABLED", raising=False)
    uid, pw = _mk_user()
    assert "secure" not in _login_cookie("http://lan-host", uid, pw)


def test_https_login_gets_secure_cookie(monkeypatch):
    """HTTPS（Caddy 経由等）で来たログインには従来どおり Secure を付ける。"""
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.delenv("SHERPA_COOKIE_SECURE", raising=False)
    monkeypatch.delenv("SHERPA_AUTH_DISABLED", raising=False)
    uid, pw = _mk_user()
    assert "secure" in _login_cookie("https://lan-host", uid, pw)


@pytest.mark.parametrize("forced,scheme,expect", [("1", "http", True), ("0", "https", False)])
def test_env_override_wins(monkeypatch, forced, scheme, expect):
    monkeypatch.setenv("SHERPA_ENV", "production")
    monkeypatch.setenv("SHERPA_COOKIE_SECURE", forced)
    monkeypatch.delenv("SHERPA_AUTH_DISABLED", raising=False)
    uid, pw = _mk_user()
    assert ("secure" in _login_cookie(f"{scheme}://lan-host", uid, pw)) is expect
