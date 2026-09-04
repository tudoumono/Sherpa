"""外部連携 API キーの Webhook 通知（PART-6・docs/proposals/2026-09-05-Webhook通知.md）のテスト。

対象: キー発行/一覧 API の `webhook_url` 検証（422）・secret は発行応答でのみ1度だけ返り一覧には
出ない・`system_settings.webhook_allowlist` の管理者設定（GET/PUT /admin/settings）。要 Postgres
（DB 不可は既存流儀どおり skip）。実 HTTP 送信（`sherpa.webhooks._deliver`）はここでは検証しない
（`tests/unit/test_webhooks.py` の担当）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app

client = TestClient(app, raise_server_exceptions=True)


def _sfx() -> str:
    return str(int(time.time() * 1000))[-8:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_admin(sfx: str) -> tuple[str, str]:
    uid = f"whka{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role="admin", status="active")
    register_test_uid(uid)
    return uid, pw


def _mk_user(sfx: str) -> tuple[str, str]:
    uid = f"whku{sfx}"
    pw = f"pw-{uid}"
    store.upsert_user(uid, email=f"{uid}@ex.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(pw), role="user", status="active")
    register_test_uid(uid)
    return uid, pw


def _login(uid: str, pw: str) -> None:
    r = client.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, f"login failed: {r.text}"


def _logout() -> None:
    client.post("/auth/logout")


def test_key_create_with_loopback_webhook_returns_secret_once():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys",
                    json={"label": f"wh-{sfx}", "webhook_url": "http://127.0.0.1:9999/hook"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["webhook_url"] == "http://127.0.0.1:9999/hook"
    assert isinstance(body["webhook_secret"], str) and len(body["webhook_secret"]) > 10

    listed = client.get("/ext/v1/admin/keys").json()["keys"]
    row = next(x for x in listed if x["id"] == body["id"])
    assert row["webhook"] is True
    assert row["webhook_host"] == "127.0.0.1:9999"
    assert "webhook_secret" not in row     # 一覧には絶対に平文 secret を出さない
    assert "webhook_url" not in row        # フル URL（path/query 含む）も一覧には出さない契約
    _logout()


def test_key_create_without_webhook_has_null_secret_and_false_flag():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys", json={"label": f"nowh-{sfx}"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["webhook_url"] is None
    assert body["webhook_secret"] is None

    listed = client.get("/ext/v1/admin/keys").json()["keys"]
    row = next(x for x in listed if x["id"] == body["id"])
    assert row["webhook"] is False
    assert row["webhook_host"] is None
    _logout()


def test_key_create_rejects_unallowlisted_non_loopback_webhook():
    """非 loopback・admin allowlist 未登録の宛先は 422（fail-closed・W3）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys",
                    json={"label": f"whbad-{sfx}", "webhook_url": "https://example.invalid/hook"})
    assert r.status_code == 422, r.text
    _logout()


def test_key_create_accepts_allowlisted_webhook_destination():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    try:
        r = client.put("/admin/settings",
                       json={"webhook_allowlist": ["example-webhook-allow.invalid:443"]})
        assert r.status_code == 200, r.text
        r = client.post("/ext/v1/admin/keys",
                        json={"label": f"whok-{sfx}",
                              "webhook_url": "https://example-webhook-allow.invalid/hooks/sherpa"})
        assert r.status_code == 200, r.text
        assert r.json()["webhook_secret"]
    finally:
        client.put("/admin/settings", json={"webhook_allowlist": None})
        _logout()


def test_admin_settings_webhook_allowlist_round_trip():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    try:
        got = client.get("/admin/settings").json()
        assert "webhook_allowlist" in got
        assert got["webhook_allowlist"]["configured"] is None
        assert got["webhook_allowlist"]["effective"] == []

        r = client.put("/admin/settings", json={"webhook_allowlist": ["10.0.0.5:8080"]})
        assert r.status_code == 200, r.text
        got2 = client.get("/admin/settings").json()
        assert got2["webhook_allowlist"]["configured"] == ["10.0.0.5:8080"]
        assert got2["webhook_allowlist"]["effective"] == ["10.0.0.5:8080"]

        # 未設定へ戻す（null）。
        r = client.put("/admin/settings", json={"webhook_allowlist": None})
        assert r.status_code == 200, r.text
        got3 = client.get("/admin/settings").json()
        assert got3["webhook_allowlist"]["configured"] is None
    finally:
        client.put("/admin/settings", json={"webhook_allowlist": None})
        _logout()


def test_admin_settings_webhook_allowlist_rejects_junk():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.put("/admin/settings", json={"webhook_allowlist": ["http://evil.example/x"]})
    assert r.status_code == 422, r.text
    r = client.put("/admin/settings", json={"webhook_allowlist": ["user@evil.example:80"]})
    assert r.status_code == 422, r.text
    _logout()


def test_self_key_create_with_loopback_webhook():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    assert client.put("/admin/settings", json={"user_api_keys_allowed": True}).status_code == 200
    _logout()
    try:
        uid, pw = _mk_user(sfx)
        _login(uid, pw)
        r = client.post("/ext/v1/keys",
                        json={"label": f"selfwh-{sfx}", "webhook_url": "http://127.0.0.1:9999/hook"})
        assert r.status_code == 200, r.text
        body = r.json()
        assert body["webhook_secret"]
        listed = client.get("/ext/v1/keys").json()["keys"]
        row = next(x for x in listed if x["id"] == body["id"])
        assert row["webhook"] is True
        _logout()
    finally:
        _login(adm_uid, adm_pw)
        client.put("/admin/settings", json={"user_api_keys_allowed": None})
        _logout()


def test_key_create_rejects_malformed_webhook_url():
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    r = client.post("/ext/v1/admin/keys",
                    json={"label": f"whmal-{sfx}", "webhook_url": "ftp://127.0.0.1/hook"})
    assert r.status_code == 422, r.text
    _logout()


def test_ext_openapi_subset_excludes_admin_and_self_key_routes():
    """Dify 向け openapi サブセットには `/ext/v1/admin/*`・`/ext/v1/keys*` が出ない
    （既存の線引き・`sherpa/ext_api.py::_ext_openapi_subset` docstring 参照）＝webhook_url/
    webhook_secret を含むスキーマもこのサブセットには出ない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    adm_uid, adm_pw = _mk_admin(sfx)
    _login(adm_uid, adm_pw)
    issued = client.post("/ext/v1/admin/keys", json={"label": f"openapi-{sfx}"})
    assert issued.status_code == 200, issued.text
    api_key = issued.json()["key"]
    _logout()
    r = client.get("/ext/v1/openapi.json", headers={"X-API-Key": api_key})
    assert r.status_code == 200, r.text
    doc = r.json()
    for p in doc.get("paths", {}):
        assert not p.startswith("/ext/v1/admin")
        assert not p.startswith("/ext/v1/keys")
    schema_names = " ".join(doc.get("components", {}).get("schemas", {}).keys())
    assert "ExtKeyCreatedResponse" not in schema_names
