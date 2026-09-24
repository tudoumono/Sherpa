"""認証ゲート網羅テスト（auth-gates・Task A）。

新たにゲートを追加したエンドポイントが:
  - 既定のログイン必須モードで未セッションに 401 を返す
  - 既定のログイン必須モードでログイン済みユーザーに 200 を返す
  - /fs/list は admin 必須（non-admin → 403、admin → 200）
  - SHERPA_AUTH_DISABLED=1（compat モード）はクッキーなしで 200 を返す

要 Postgres。DB 不可は graceful SKIP（_try_init が False を返す）。
"""
from __future__ import annotations

import time

import pytest

from _test_users import register_test_uid

# 既定（conftest）の実認証モードで import。compat モードのテストだけ一時切替する。
IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import auth, store
    from sherpa.api import app
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]
    auth = None  # type: ignore[assignment]
    store = None  # type: ignore[assignment]
    app = None  # type: ignore[assignment]


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    """DB を初期化できるか確認。失敗した場合は SKIP 判定用に False を返す。"""
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")   # 不可なら可視の skip（silent-green 根絶）


def _client(*, raise_server_exceptions: bool = True) -> TestClient:
    return TestClient(app, raise_server_exceptions=raise_server_exceptions)


def _mk_user(uid: str, password: str, *, role: str = "user") -> None:
    store.upsert_user(
        uid,
        email=f"{uid}@gates.local",
        display_name=uid.upper(),
        password_hash=auth.hash_password(password),
        role=role,
        status="active",
    )
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py・2026-07）


def _login(uid: str, password: str) -> TestClient:
    c = _client(raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed for {uid}: {r.status_code} {r.text}"
    return c


# ログイン必須ルート（一般ユーザーで 200 が返れば OK）
_LOGIN_REQUIRED_ROUTES = [
    ("GET",  "/scopes",               {}),
    ("GET",  "/documents",            {}),
    ("GET",  "/world-options",        {}),
    # POST routes — body が空でも 422 ではなく認証が先に刺さることを確認
    ("POST", "/troubleshoot/run",     {"json": {"symptom": "test", "world": "v1"}}),
    ("POST", "/qa/run",               {"json": {"question": "test", "world": "v1"}}),
]

# admin 必須ルート
_ADMIN_REQUIRED_ROUTES = [
    ("GET",  "/fs/list",              {}),
]


def test_ungated_routes_return_401_without_session():
    """auth 有効時: セッションなしで新規ゲート対象ルートが 401 を返す。"""
    if not _try_init():
        pytest.skip("infra down")
    c = _client(raise_server_exceptions=False)
    for method, path, kwargs in _LOGIN_REQUIRED_ROUTES + _ADMIN_REQUIRED_ROUTES:
        r = getattr(c, method.lower())(path, **kwargs)
        assert r.status_code == 401, (
            f"expected 401 without session for {method} {path}, got {r.status_code}: {r.text}"
        )


def test_login_required_routes_return_200_with_user_session():
    """auth 有効時: ログイン済み一般ユーザーでログイン必須ルートが 401/403 以外を返す。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"gateuser{sfx}", f"gateuser-pw-{sfx}"
    _mk_user(uid, pw, role="user")
    c = _login(uid, pw)
    for method, path, kwargs in _LOGIN_REQUIRED_ROUTES:
        r = getattr(c, method.lower())(path, **kwargs)
        assert r.status_code not in (401, 403), (
            f"expected non-40x for {method} {path} with user session, got {r.status_code}: {r.text}"
        )


def test_fs_list_requires_admin_403_for_user():
    """auth 有効時: /fs/list は一般ユーザーで 403。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"fsusr{sfx}", f"fsusr-pw-{sfx}"
    _mk_user(uid, pw, role="user")
    c = _login(uid, pw)
    r = c.get("/fs/list")
    assert r.status_code == 403, f"expected 403 for non-admin on /fs/list, got {r.status_code}: {r.text}"


def test_fs_list_200_for_admin():
    """auth 有効時: /fs/list は admin で 200 を返す。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"fsadm{sfx}", f"fsadm-pw-{sfx}"
    _mk_user(uid, pw, role="admin")
    c = _login(uid, pw)
    r = c.get("/fs/list")
    assert r.status_code == 200, f"expected 200 for admin on /fs/list, got {r.status_code}: {r.text}"
    data = r.json()
    assert "entries" in data


def test_compat_mode_passes_all_gated_routes_without_login(auth_disabled):
    """SHERPA_AUTH_DISABLED=1 (compat モード): クッキーなしで新規ゲート対象ルートが 401/403 を返さない。"""
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    c = _client(raise_server_exceptions=False)
    for method, path, kwargs in _LOGIN_REQUIRED_ROUTES + _ADMIN_REQUIRED_ROUTES:
        r = getattr(c, method.lower())(path, **kwargs)
        assert r.status_code not in (401, 403), (
            f"compat mode: expected no auth block for {method} {path}, got {r.status_code}: {r.text}"
        )
