"""PUT /settings の agent allowlist（FastAPI TestClient・要 Postgres）。

- allowlist 外・チャットで閉じた頭脳（heuristic/gemini/bedrock/openai/ollama）は 422。
DB 不可は graceful SKIP（test_health_api.py の流儀）。
"""
from __future__ import annotations

import time

import pytest

from _test_users import register_test_uid

IMPORT_ERROR: Exception | None = None
try:
    from fastapi.testclient import TestClient

    from sherpa import auth, store
    from sherpa.api import app
except Exception as e:  # pragma: no cover
    IMPORT_ERROR = e
    TestClient = None  # type: ignore[assignment]


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    if IMPORT_ERROR is not None:
        pytest.skip(f"infra down: {IMPORT_ERROR}")
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")


def _mk_user(uid: str, password: str) -> None:
    store.upsert_user(uid, email=f"{uid}@agent-allowlist.local", display_name=uid.upper(),
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)   # テストユーザー残骸防止（tests/_test_users.py）


def _login(uid: str, password: str) -> "TestClient":
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, f"login failed: {r.status_code} {r.text}"
    return c


def test_settings_rejects_invalid_agent():
    """allowlist 外の agent は 422（chat.turn 監査 detail に任意文字列が入るのを防ぐ）。"""
    if not _try_init():
        pytest.skip("infra down")
    sfx = _sfx()
    uid, pw = f"agtinv{sfx}", f"agtinv-pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    r = c.put("/settings", json={"agent": "not-a-real-agent"})
    assert r.status_code == 422, r.text
    assert "codex" in r.json().get("detail", "")


