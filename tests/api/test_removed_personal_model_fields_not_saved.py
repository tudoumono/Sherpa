"""個人設定に画面の無い項目（モデル名・Codex の推論・機能別プロバイダ・Web 検索の希望）は個人設定に無い
（管理者の使えるモデル一覧・管理画面の設定・チャットごとの希望だけで決まる）。

`SettingsReq` はこれらのフィールドを受け取らない＝ PUT ボディに含めても pydantic の未知フィールドとして
黙って無視される（保存もされず・422 にもならない）。`user_settings` の該当列は起動時の移行で
`DROP COLUMN IF EXISTS` され、撤去後も起動と個人設定の読み書きが壊れない。

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


_RETIRED_FIELDS = {
    "openai_model": "gpt-5.4-mini", "ollama_model": "qwen2.5", "codex_model": "gpt-5.4-mini",
    "codex_reasoning": "high", "codex_web_search": True, "extract_provider": "ollama",
    "intent_model": "gpt-4o-mini", "graph_provider": "ollama", "intent_provider": "openai",
    "embed_provider": "ollama", "search_helper": "ollama", "search_helper_model": "qwen2.5",
    "system_prompt": "独自の回答方針",
}


def test_retired_fields_are_silently_ignored_on_put():
    """PUT に撤去済みフィールドを含めても 200・保存されない（未知フィールドとして無視される）。
    GET /settings の応答にもこれらのフィールドは含まれない。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"rmf{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json=_RETIRED_FIELDS)
    assert r.status_code == 200, r.text
    saved, got = store.get_settings(uid), c.get("/settings").json()
    for field in _RETIRED_FIELDS:
        assert field not in r.json() and field not in saved and field not in got
