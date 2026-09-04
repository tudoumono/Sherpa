from __future__ import annotations

import pathlib

from sherpa import auth


def _setenv(name: str, value: str | None):
    old = __import__("os").environ.get(name)
    if value is None:
        __import__("os").environ.pop(name, None)
    else:
        __import__("os").environ[name] = value
    return old


def _restore(name: str, old: str | None):
    if old is None:
        __import__("os").environ.pop(name, None)
    else:
        __import__("os").environ[name] = old


def test_auth_is_enabled_by_default():
    old_enabled = _setenv("SHERPA_AUTH_ENABLED", None)
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", None)
    try:
        assert auth.auth_enabled() is True
        assert auth.auth_disabled() is False
    finally:
        _restore("SHERPA_AUTH_ENABLED", old_enabled)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)


def test_auth_disabled_requires_explicit_env():
    old_enabled = _setenv("SHERPA_AUTH_ENABLED", None)
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", "1")
    try:
        assert auth.auth_enabled() is False
        assert auth.auth_disabled() is True
    finally:
        _restore("SHERPA_AUTH_ENABLED", old_enabled)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)


def test_auth_disabled_ignored_in_production():
    """TOGGLE-RM（2026-09-03）: 本番プロファイル（SHERPA_ENV=production、大小文字不問）では
    SHERPA_AUTH_DISABLED を無視し常に False を返す（誤設定で本番に合成 admin が残る事故を防ぐ）。"""
    old_env = _setenv("SHERPA_ENV", "production")
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", "1")
    try:
        assert auth.auth_disabled() is False
        assert auth.auth_enabled() is True
    finally:
        _restore("SHERPA_ENV", old_env)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)

    old_env = _setenv("SHERPA_ENV", "PRODUCTION")   # 大小文字不問
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", "true")
    try:
        assert auth.auth_disabled() is False
    finally:
        _restore("SHERPA_ENV", old_env)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)

    old_env = _setenv("SHERPA_ENV", "dev")   # 非本番では従来どおり有効
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", "1")
    try:
        assert auth.auth_disabled() is True
    finally:
        _restore("SHERPA_ENV", old_env)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)


def test_warn_auth_disabled_in_production_logs_once_and_does_not_raise(monkeypatch, caplog):
    """`api._warn_auth_disabled_in_production()` は本番＋SHERPA_AUTH_DISABLED 設定時だけ ERROR ログを
    1回残す（fail-closed で起動拒否はしない・auth_disabled() 自体が既に無視するため）。"""
    from sherpa import api
    import logging

    old_env = _setenv("SHERPA_ENV", "production")
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", "1")
    try:
        with caplog.at_level(logging.ERROR, logger="sherpa"):
            api._warn_auth_disabled_in_production()
        assert any("SHERPA_AUTH_DISABLED" in r.message for r in caplog.records)
    finally:
        _restore("SHERPA_ENV", old_env)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)

    caplog.clear()
    old_env = _setenv("SHERPA_ENV", "production")
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", None)
    try:
        with caplog.at_level(logging.ERROR, logger="sherpa"):
            api._warn_auth_disabled_in_production()
        assert not caplog.records   # 未設定なら無音
    finally:
        _restore("SHERPA_ENV", old_env)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)

    caplog.clear()
    old_env = _setenv("SHERPA_ENV", "dev")
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", "1")
    try:
        with caplog.at_level(logging.ERROR, logger="sherpa"):
            api._warn_auth_disabled_in_production()
        assert not caplog.records   # 非本番は無音（互換モードは正当に有効）
    finally:
        _restore("SHERPA_ENV", old_env)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)


def test_initial_admin_bootstrap_uses_default_password_and_requires_change():
    old_pw = _setenv("SHERPA_ADMIN_PASSWORD", None)
    old_disabled = _setenv("SHERPA_AUTH_DISABLED", None)

    from sherpa import api, deps

    row = {
        "uid": "admin",
        "email": "admin@sherpa.local",
        "display_name": "Administrator",
        "role": "admin",
        "status": "active",
        "must_change_password": True,
        "password_hash": auth.hash_password(auth.DEFAULT_ADMIN_PASSWORD),
    }
    calls = {"upsert": None, "audit": []}
    users = [None, row]

    old_get = api.store.get_user_by_uid
    old_upsert = api.store.upsert_user
    old_audit = api.store.audit
    # `_ensure_initial_admin` は sherpa/deps.py 定義（フェーズ3スライス8）。api.py 側の bare name
    # 代入（`api.ensure_workspace = ...`）は deps.py 内で解決される呼び出しには効かないため、
    # deps モジュール属性を直接差し替える（feature-implementer agent-memory 参照）。
    old_ensure = deps.ensure_workspace
    try:
        api.store.get_user_by_uid = lambda uid: users.pop(0) if users else row

        def fake_upsert_user(*args, **kwargs):
            calls["upsert"] = {"args": args, "kwargs": kwargs}
            return row

        api.store.upsert_user = fake_upsert_user
        api.store.audit = lambda *args, **kwargs: calls["audit"].append((args, kwargs))
        deps.ensure_workspace = lambda uid: pathlib.Path("/tmp") / uid

        got = api._ensure_initial_admin()

        assert got["uid"] == "admin"
        assert calls["upsert"]["kwargs"]["display_name"] == "Administrator"
        assert calls["upsert"]["kwargs"]["must_change_password"] is True
        assert auth.verify_password(auth.DEFAULT_ADMIN_PASSWORD, calls["upsert"]["kwargs"]["password_hash"])
        assert calls["audit"][0][0][1] == "admin.initial_created"
    finally:
        api.store.get_user_by_uid = old_get
        api.store.upsert_user = old_upsert
        api.store.audit = old_audit
        deps.ensure_workspace = old_ensure
        _restore("SHERPA_ADMIN_PASSWORD", old_pw)
        _restore("SHERPA_AUTH_DISABLED", old_disabled)


def test_password_validation_rejects_initial_and_obvious_weak_values():
    old_pw = _setenv("SHERPA_ADMIN_PASSWORD", None)
    from sherpa.api import _validate_new_password
    try:
        assert _validate_new_password("admin", "oldpass123", "short", "short")
        assert _validate_new_password("admin", "oldpass123", auth.DEFAULT_ADMIN_PASSWORD, auth.DEFAULT_ADMIN_PASSWORD)
        assert _validate_new_password("admin", "oldpass123", "my-admin-pass-123", "my-admin-pass-123")
        assert _validate_new_password("sato", "oldpass123", "satoStrong123", "satoStrong123")
        assert _validate_new_password("sato", "oldpass123", "BetterPass123", "Mismatch123")
        assert _validate_new_password("sato", "oldpass123", "Ａbcdef123", "Ａbcdef123")
        assert _validate_new_password("sato", "oldpass123", "Better Pass123", "Better Pass123")
        assert _validate_new_password("sato", "oldpass123", "BetterPass123", "BetterPass123") is None
    finally:
        _restore("SHERPA_ADMIN_PASSWORD", old_pw)
