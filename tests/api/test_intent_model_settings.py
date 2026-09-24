"""`intent_model`／`intent_provider`／`extract_provider` は個人設定に無い（管理者の使えるモデル
一覧・選択中のクラウドプロバイダだけで決まる）。

`SettingsReq` はこれらのフィールドを受け取らない＝ PUT ボディに含めても pydantic の未知フィールド
として黙って無視される（保存もされず・422 にもならない）。旧・個人上書き時代の provider 文脈別
検証（grandfather・セレクタ変更時の再検証等）はフィールド自体が無いため到達しない。

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
    store.upsert_user(uid, email=f"{uid}@intentmodel.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_unset_default_is_empty():
    """個人設定に `intent_model` の入力欄は無い＝ GET /settings の応答にも含まれない
    （ストア層の既定は未設定=None のまま・migration しない）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"imunset{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.get("/settings")
    assert r.status_code == 200, r.text
    assert "intent_model" not in r.json()
    assert store.get_settings(uid)["intent_model"] is None


def test_intent_model_and_intent_provider_fields_are_silently_ignored_on_put():
    """PUT に `intent_model`／`intent_provider`／`extract_provider` を含めても 200・保存されない
    （未知フィールドとして無視される・旧・自由入力時代のカタログ検証には到達しない）。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"imignore{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json={
        "intent_provider": "gemini", "extract_provider": "gemini", "intent_model": "gpt-4o-mini"})
    assert r.status_code == 200, r.text

    body = store.get_settings(uid)
    assert body["intent_model"] is None
    assert body["intent_provider"] == ""
    assert body["extract_provider"] == "auto"


def test_intent_model_invalid_value_does_not_422():
    """旧・個人上書き時代の形式検証は、フィールド自体が受理されない以上もう到達しない。"""
    _try_init()
    sfx = _sfx()
    uid, pw = f"iminvalid{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json={"intent_model": "bad model"})
    assert r.status_code == 200, r.text
    assert store.get_settings(uid)["intent_model"] is None


def test_per_user_isolation_of_stale_saved_value():
    """ストア層（migration しない・不活性のまま残置）は利用者ごとに独立して保持される。"""
    _try_init()
    sfx = _sfx()
    u1, p1 = f"imiso1{sfx}", f"pw1-{sfx}"
    u2, p2 = f"imiso2{sfx}", f"pw2-{sfx}"
    _mk_user(u1, p1)
    _mk_user(u2, p2)
    store.update_settings(u1, intent_provider="openai", intent_model="gpt-4o-mini")
    store.update_settings(u2, intent_provider="gemini", intent_model="gemini-2.5-flash")

    assert store.get_settings(u1)["intent_model"] == "gpt-4o-mini"
    assert store.get_settings(u2)["intent_model"] == "gemini-2.5-flash"


def test_intent_resolution_uses_catalog_default_ignoring_stale_saved_value():
    """実行時解決（`intent_llm._cfg`）は個人設定に残る旧・個人上書き時代の値を読まず、管理者の
    使えるモデル一覧（用途 `intent`）から解決する。"""
    _try_init()
    sfx = _sfx()
    uid = f"imstale{sfx}"
    _mk_user(uid, f"pw-{sfx}")
    store.update_settings(uid, intent_provider="openai", intent_model="stale-legacy-model")

    from sherpa import model_catalog
    # 共有テスト DB の admin 設定に左右されないよう、組み込み既定だけのカタログで検証する。
    resolved = model_catalog.resolve_model("openai", "intent", None, system_settings={})
    assert resolved != "stale-legacy-model"
    assert resolved == "gpt-4o-mini"   # 組み込み既定
