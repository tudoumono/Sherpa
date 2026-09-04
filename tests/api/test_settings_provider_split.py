"""`graph_provider`/`intent_provider`/`embed_provider`/`extract_provider` は個人設定に無い
（管理者の使えるモデル一覧・選択中のクラウドプロバイダだけで決まる）。

`SettingsReq` はこれらのフィールドを受け取らない＝ PUT ボディに含めても pydantic の未知フィールド
として黙って無視される（保存もされず・422 にもならない）。

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
    store.upsert_user(uid, email=f"{uid}@providersplit.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_defaults_empty_for_fresh_user():
    """個人設定にこれらのフィールドの入力欄は無い＝ GET /settings の応答にも含まれない
    （ストア層の既定は空文字のまま・migration しない）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"psdef{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert "graph_provider" not in body and "intent_provider" not in body and "embed_provider" not in body
    s = store.get_settings(uid)
    assert s["graph_provider"] == "" and s["intent_provider"] == "" and s["embed_provider"] == ""


def test_provider_fields_are_silently_ignored_on_put():
    """PUT にこれらのフィールドを含めても 200・保存されない（未知フィールドとして無視される）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"psignore{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json={"graph_provider": "ollama", "intent_provider": "openai",
                                 "embed_provider": "gemini", "extract_provider": "anything-goes"})
    assert r.status_code == 200, r.text

    s = store.get_settings(uid)
    assert s["graph_provider"] == ""
    assert s["intent_provider"] == ""
    assert s["embed_provider"] == ""
    assert s["extract_provider"] == "auto"


def test_provider_resolution_ignores_stale_saved_value_and_fails_loud_on_selected_cloud():
    """実行時解決（`llm.select_provider`）は個人設定に残る旧・個人上書き時代の値
    （`graph_provider="gemini"`）を読まず、常に auto 解決（選択中のクラウドプロバイダ・A7）へ
    進む。openai を明示選択中（`cloud_provider="openai"`）だが鍵が無い環境では、`graph_provider`
    を読んでいれば明示選択のまま None（llm_unavailable）になるはずだが、実際にも auto 解決は
    Ollama へ黙って倒れず None になる（FBK-1・fail-loud・2026-09-01）。"""
    _try_init()
    sfx = _sfx()
    uid = f"psstale{sfx}"
    _mk_user(uid, f"pw-{sfx}")
    store.update_settings(uid, graph_provider="gemini")
    s = store.get_settings(uid)

    from sherpa import llm
    called = []
    result = llm.select_provider(
        s, openai=lambda k: called.append("openai") or {"provider": "openai"},
        gemini=lambda k: called.append("gemini") or {"provider": "gemini"},
        ollama=lambda u: called.append("ollama") or {"provider": "ollama", "url": u},
        system_settings={"cloud_provider": "openai"})
    assert called == []
    assert result is None
