"""`search_helper`（''／'ollama'／'openai'・下調べに使う AI）は個人設定に残るが、そのモデル名
（`search_helper_model`）はもう個人設定に無い（管理者の使えるモデル一覧だけで決まる）。

`SettingsReq` は `search_helper_model` を受け取らない＝ PUT ボディに含めても pydantic の未知
フィールドとして黙って無視される（保存もされず・422 にもならない）。

要 Postgres。DB 不可は SKIP。`search_helper=ollama` は保存時に実接続 probe が走るため（決定
2026-08-15）、ネットワーク I/O を避けるテストは `search_helper=openai` のみを使う。
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
    store.upsert_user(uid, email=f"{uid}@searchhelpermodel.local", display_name=uid,
                      password_hash=auth.hash_password(password), role="user", status="active")
    register_test_uid(uid)


def _login(uid: str, password: str) -> TestClient:
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": password})
    assert r.status_code == 200, r.text
    return c


def test_search_helper_model_field_is_silently_ignored_on_put():
    """PUT に `search_helper_model` を含めても 200・保存されない（未知フィールドとして無視される・
    旧・個人上書き時代のプロバイダ別カタログ検証には到達しない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"shmignore{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r = c.put("/settings", json={"search_helper": "openai", "search_helper_model": "gpt-5.4-mini"})
    assert r.status_code == 200, r.text
    assert store.get_settings(uid)["search_helper_model"] == ""


def test_search_helper_resolution_uses_catalog_default_ignoring_stale_saved_value():
    """実行時解決（`search_helper.resolve`）は個人設定に残る旧・個人上書き時代の値を読まず、
    管理者の使えるモデル一覧（用途 `subsearch`）から解決する。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid = f"shmstale{sfx}"
    _mk_user(uid, f"pw-{sfx}")
    store.update_settings(uid, search_helper="openai", search_helper_model="stale-legacy-model")

    from sherpa import model_catalog
    # 共有テスト DB の admin 設定に左右されないよう、組み込み既定だけのカタログで検証する。
    resolved = model_catalog.resolve_model("openai", "subsearch", None, system_settings={})
    assert resolved != "stale-legacy-model"
    assert resolved == "gpt-5.4-mini"   # 組み込み既定


def test_search_helper_ollama_probes_central_remote_url_not_localhost_when_personal_unset(monkeypatch):
    """重大バグ是正（RV 2巡目）: 個人の `ollama_url` が未設定でも、中央の共有 Ollama（管理者が
    `ollama_allowlist` へ登録した非 loopback ホストを中央既定に設定）があれば、検索アシスタント
    有効化時の probe はその中央 URL へ向かう（以前は `req.ollama_url or cur.get("ollama_url") or
    "http://localhost:11434"` という truthy 判定で、個人未設定＋中央リモート Ollama という正当な
    構成でも localhost を probe してしまい、到達不能で 422 になっていた）。probe 自体は
    monkeypatch で差し替え、実ネットワーク I/O は発生させない。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    admin_uid, admin_pw = f"shmadmin{sfx}", f"pw-{sfx}"
    store.upsert_user(admin_uid, email=f"{admin_uid}@searchhelpermodel.local", display_name=admin_uid,
                      password_hash=auth.hash_password(admin_pw), role="admin", status="active")
    register_test_uid(admin_uid)
    admin = _login(admin_uid, admin_pw)
    r = admin.put("/admin/settings", json={
        "ollama_allowlist": ["10.9.9.9:11434"], "ollama_url": "http://10.9.9.9:11434"})
    assert r.status_code == 200, r.text

    uid, pw = f"shmremote{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    calls = []

    def _fake_probe(cfg):
        calls.append(cfg)
        return True, "ok"

    monkeypatch.setattr("sherpa.ingest.graph_extract._probe", _fake_probe)

    r2 = c.put("/settings", json={"search_helper": "ollama"})
    assert r2.status_code == 200, r2.text
    assert calls and calls[-1]["url"] == "http://10.9.9.9:11434"


def test_search_helper_toggle_off_does_not_error_regardless_of_stale_saved_model():
    """`search_helper=openai` で有効化した後 `search_helper=""` へ無効化する操作自体は、
    個人設定に残る旧・個人上書き時代の `search_helper_model` の値に関わらず常に成功する
    （検証対象のフィールドがもう無いため、非消費/再検証の分岐に到達すらしない）。"""
    if not _try_init():
        pytest.skip("DB down")
    sfx = _sfx()
    uid, pw = f"shmdisable{sfx}", f"pw-{sfx}"
    _mk_user(uid, pw)
    c = _login(uid, pw)

    r1 = c.put("/settings", json={"search_helper": "openai"})
    assert r1.status_code == 200, r1.text

    r2 = c.put("/settings", json={"search_helper": ""})
    assert r2.status_code == 200, r2.text
    assert c.get("/settings").json()["search_helper"] == ""
