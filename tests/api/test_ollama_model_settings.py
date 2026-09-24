"""`ollama_model` は個人設定に無い（管理者の使えるモデル一覧だけで選ぶ）。

`SettingsReq` はこのフィールドを受け取らない＝ PUT ボディに含めても pydantic の未知フィールドとして
黙って無視される（保存もされず・422 にもならない）。旧・個人上書き時代の形式検証
（`_MODEL_NAME_RE`）はフィールド自体が無いため到達しない。

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
    store.upsert_user(uid, email=f"{uid}@ollamamodel.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_ollama_model_field_is_silently_ignored_on_put():
    """PUT に `ollama_model` を含めても 200・保存されない（未知フィールドとして無視される）。
    GET /settings の応答にもこのフィールドは含まれない（削除済みフィールドは応答からも返さない）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"omignore{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    before = store.get_settings(uid)["ollama_model"]

    r = c.put("/settings", json={"ollama_model": "qwen2.5"})
    assert r.status_code == 200, r.text

    assert store.get_settings(uid)["ollama_model"] == before
    assert "ollama_model" not in r.json()


def test_ollama_model_invalid_or_oversized_value_does_not_422():
    """旧・個人上書き時代の形式検証（英数字と `. _ : / -`・128文字以内）は、フィールド自体が
    受理されない以上もう到達しない＝不正な値を送っても 422 にならず、単に無視される。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"ombadval{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)
    before = store.get_settings(uid)["ollama_model"]

    r = c.put("/settings", json={"ollama_model": "bad model; rm -rf /"})
    assert r.status_code == 200, r.text
    r = c.put("/settings", json={"ollama_model": "x" * (2 * 1024 * 1024)})   # 2MB 級
    assert r.status_code == 200, r.text

    assert store.get_settings(uid)["ollama_model"] == before


def test_ollama_chat_resolves_via_catalog_default_regardless_of_stale_saved_value():
    """DB に個人上書き時代の値が残っていても（migration しない・不活性のまま残置）、実行時解決は
    それを読まず管理者のカタログ既定（`ollama`/`chat`）を使う。"""
    _try_init()
    sfx = _sfx()
    uid = f"omstale{sfx}"
    _mk_user(uid, f"pw-{sfx}")
    # ストア層は列自体を保つ（migration しない）＝旧・個人上書き時代の値を直接書き込める。
    store.update_settings(uid, ollama_model="stale-legacy-model")

    from sherpa import model_catalog
    # 共有テスト DB の admin 設定に左右されないよう、組み込み既定だけのカタログで検証する。
    resolved = model_catalog.resolve_model("ollama", "chat", None, system_settings={})
    assert resolved != "stale-legacy-model"
    assert resolved == "qwen2.5"   # 組み込み既定
