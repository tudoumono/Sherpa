"""`openai_model`／`gemini_model`／`codex_model`／`codex_reasoning` は個人設定に無い（管理者の
使えるモデル一覧・環境変数だけで決まる）。

`SettingsReq` はこれらのフィールドを受け取らない＝ PUT ボディに含めても pydantic の未知フィールド
として黙って無視される（保存もされず・422 にもならない）。`ollama_model`／`intent_model`／
`search_helper_model`／機能別プロバイダ（graph/intent/embed_provider・extract_provider）は
それぞれ専用のテストファイル（`test_ollama_model_settings.py` 等）で直接固定済み。このファイルは
専用ファイルの無かった残り4フィールドをまとめて固定する。

要 Postgres。DB 不可は SKIP（他の tests/api/test_*settings*.py と同じ流儀）。
"""
from __future__ import annotations

import time

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, store
from sherpa.api import app


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _mk_user(uid: str, password: str) -> None:
    store.upsert_user(uid, email=f"{uid}@removedmodelfields.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


@pytest.mark.parametrize("field,value,default", [
    ("openai_model", "gpt-5.4-mini", ""),
    ("gemini_model", "gemini-2.5-flash-lite", ""),
    ("codex_model", "gpt-5.4-mini", ""),
    ("codex_reasoning", "high", "low"),
])
def test_field_is_silently_ignored_on_put(field, value, default):
    """PUT にフィールドを含めても 200・保存されない（未知フィールドとして無視される）。
    GET /settings の応答にもこのフィールドは含まれない（削除済みフィールドは応答からも返さない）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"rmf{field[:4]}{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json={field: value})
    assert r.status_code == 200, r.text
    assert field not in r.json()

    assert store.get_settings(uid)[field] == default
    assert field not in c.get("/settings").json()
