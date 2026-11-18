"""admin 専用 POST /admin/settings/ollama-test（管理画面の Ollama 接続テスト）の契約。

別のパソコンの Ollama を「利用者が選べる接続先」に登録しないまま、入力中の値で試せること。
通信は `llm.urlopen_no_redirect`（外部境界）だけを差し替える。要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import io
import json
import time
import urllib.error

import pytest
from _test_users import register_test_uid
from fastapi.testclient import TestClient

from sherpa import auth, llm, model_catalog, store
from sherpa.api import app

_URL = "/admin/settings/ollama-test"
_LAN = "http://10.9.9.21:11434"


def _login_new(role: str) -> TestClient:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    sfx = str(time.time_ns())[-13:]
    uid, pw = f"olt{role[0]}{sfx}", f"pw-{sfx}"
    store.upsert_user(uid, email=f"{uid}@ollamatest.local", display_name=uid,
                      password_hash=auth.hash_password(pw), role=role, status="active")
    register_test_uid(uid)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def admin_client() -> TestClient:
    return _login_new("admin")


def _patch_sys_settings(monkeypatch, value):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: value)


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def _stub_tags(monkeypatch, *, names=None, error=None, show=None):
    """`show` は /api/show の応答（モデル名 → 応答の辞書、または例外）。無ければ空の辞書を返す。"""
    seen = []

    def _open(req, timeout=None):
        seen.append(req if isinstance(req, str) else req.full_url)
        if error is not None:
            raise error
        if str(seen[-1]).endswith("/api/show"):
            r = (show or {}).get(json.loads(req.data)["model"], {})
            if isinstance(r, Exception):
                raise r
            return _Resp(json.dumps(r).encode())
        return _Resp(json.dumps({"models": [{"name": n} for n in names or []]}).encode())

    monkeypatch.setattr(llm, "urlopen_no_redirect", _open)
    return seen


def _max_audit_id() -> int:
    with store._connect() as c:
        return c.execute("SELECT COALESCE(MAX(id), 0) AS m FROM audit_log").fetchone()["m"]


def test_ollama_test_requires_admin(monkeypatch):
    seen = _stub_tags(monkeypatch, names=[])
    r = _login_new("user").post(_URL, json={"ollama_url": _LAN})
    assert r.status_code == 403, r.text
    assert seen == []


def test_ollama_test_reaches_lan_url_without_allowlist_and_lists_missing_models(monkeypatch, admin_client):
    """許可一覧が空でも入力中の別のパソコンの URL を試せる。保存はせず、監査は host:port だけ。"""
    _patch_sys_settings(monkeypatch, {
        "ollama_allowlist": [],
        "model_catalog": {"ollama": {"chat": {"allowed": ["qwen2.5", "gemma3:4b"], "default": "qwen2.5"},
                                     "subsearch": {"allowed": ["gemma3:4b"], "default": "gemma3:4b"}},
                          "codex": {"codex": {"allowed": ["gemma3:4b"], "default": "gemma3:4b"}}}})
    seen = _stub_tags(monkeypatch, names=["qwen2.5:latest", "nomic-embed-text:latest"])
    before = _max_audit_id()

    r = admin_client.post(_URL, json={"ollama_url": _LAN})

    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True and body["host"] == "10.9.9.21:11434"
    assert body["models"] == ["nomic-embed-text:latest", "qwen2.5:latest"]
    assert body["missing"] == ["gemma3:4b"]
    assert seen == [f"{_LAN}/api/tags"]
    with store._connect() as c:
        rows = c.execute("SELECT detail FROM audit_log WHERE action = 'ollama_endpoint.tested' AND id > %s",
                         (before,)).fetchall()
    assert [dict(x)["detail"] for x in rows] == [{"host": "10.9.9.21:11434"}]


def test_ollama_test_rejects_malformed_url_before_probing(monkeypatch, admin_client):
    seen = _stub_tags(monkeypatch, names=[])
    for bad in ("ftp://10.9.9.21:11434", "http://u:p@10.9.9.21:11434", "http://10.9.9.21:11434/api"):
        r = admin_client.post(_URL, json={"ollama_url": bad})
        assert r.status_code == 422, (bad, r.text)
    assert seen == []


def test_ollama_test_refused_connection_points_to_ollama_host_setting(monkeypatch, admin_client):
    _patch_sys_settings(monkeypatch, {})
    _stub_tags(monkeypatch, error=urllib.error.URLError(ConnectionRefusedError(61, "refused")))

    body = admin_client.post(_URL, json={"ollama_url": _LAN}).json()

    assert body["ok"] is False
    assert "OLLAMA_HOST=0.0.0.0" in body["detail"]


def test_ollama_test_aborts_when_audit_fails(monkeypatch, admin_client):
    _patch_sys_settings(monkeypatch, {})
    seen = _stub_tags(monkeypatch, names=[])

    def _boom(*a, **k):
        raise RuntimeError("audit down")

    monkeypatch.setattr(store, "audit", _boom)
    r = admin_client.post(_URL, json={"ollama_url": _LAN})
    assert r.status_code == 500, r.text
    assert seen == []


def test_ollama_test_connects_without_proxy(admin_client, monkeypatch):
    _patch_sys_settings(monkeypatch, {})
    flags = []

    def _open(req, timeout=None):
        flags.append(llm._no_proxy_ctx.get())
        return _Resp(json.dumps({"models": []}).encode())

    monkeypatch.setattr(llm, "urlopen_no_redirect", _open)
    r = admin_client.post(_URL, json={"ollama_url": _LAN})
    assert r.status_code == 200, r.text
    assert flags == [True]


def _catalog_with_subsearch(*names):
    return {"ollama_allowlist": [],
            "model_catalog": {"ollama": {"subsearch": {"allowed": list(names), "default": names[0]}},
                              "codex": {"codex": {"allowed": ["absent-model"], "default": "absent-model"}}}}


def test_ollama_test_lists_fetched_model_without_tools(monkeypatch, admin_client):
    _patch_sys_settings(monkeypatch, _catalog_with_subsearch("gemma3:12b", "qwen2.5"))
    seen = _stub_tags(monkeypatch, names=["gemma3:12b", "qwen2.5:latest"],
                      show={"gemma3:12b": {"capabilities": ["completion"]},
                            "qwen2.5": {"capabilities": ["completion", "tools"]}})

    body = admin_client.post(_URL, json={"ollama_url": _LAN}).json()

    assert body["ok"] is True and body["no_tools"] == ["gemma3:12b"]
    assert seen[0] == f"{_LAN}/api/tags" and seen[1:] == [f"{_LAN}/api/show"] * 2


def test_ollama_test_without_capabilities_field_says_nothing(monkeypatch, admin_client):
    _patch_sys_settings(monkeypatch, _catalog_with_subsearch("gemma3:12b"))
    _stub_tags(monkeypatch, names=["gemma3:12b"], show={"gemma3:12b": {"details": {}}})

    body = admin_client.post(_URL, json={"ollama_url": _LAN}).json()

    assert body["ok"] is True and body["no_tools"] == []


def test_ollama_test_show_failure_keeps_test_successful(monkeypatch, admin_client):
    _patch_sys_settings(monkeypatch, _catalog_with_subsearch("gemma3:12b"))
    _stub_tags(monkeypatch, names=["gemma3:12b"],
               show={"gemma3:12b": urllib.error.URLError(ConnectionRefusedError(61, "refused"))})

    body = admin_client.post(_URL, json={"ollama_url": _LAN}).json()

    assert body["ok"] is True and body["no_tools"] == [] and body["detail"] == "接続OK"


def test_ollama_test_does_not_show_models_that_are_not_fetched(monkeypatch, admin_client):
    _patch_sys_settings(monkeypatch, _catalog_with_subsearch("gemma3:12b"))
    seen = _stub_tags(monkeypatch, names=["qwen2.5:latest"])

    body = admin_client.post(_URL, json={"ollama_url": _LAN}).json()

    assert body["no_tools"] == [] and "gemma3:12b" in body["missing"]
    assert seen == [f"{_LAN}/api/tags"]


def test_ollama_test_always_checks_default_model_beyond_the_show_limit(monkeypatch, admin_client):
    names = [f"m{i:02d}" for i in range(10)] + ["zz-default"]
    ss = {"ollama_allowlist": [],
          "model_catalog": {"ollama": {"subsearch": {"allowed": names, "default": "zz-default"}},
                            "codex": {"codex": {"allowed": ["absent-model"], "default": "absent-model"}}}}
    _patch_sys_settings(monkeypatch, ss)
    _stub_tags(monkeypatch, names=names, show={"zz-default": {"capabilities": ["completion"]}})

    body = admin_client.post(_URL, json={"ollama_url": _LAN}).json()

    assert body["ok"] is True and body["no_tools"] == ["zz-default"]


def test_ollama_test_checks_builtin_default_when_catalog_default_is_empty(monkeypatch, admin_client):
    builtin = model_catalog.hardcoded_fallback("ollama", "subsearch")
    names = [f"a{i}" for i in range(9)] + [builtin]
    ss = {"ollama_allowlist": [],
          "model_catalog": {"ollama": {"subsearch": {"allowed": names, "default": ""}},
                            "codex": {"codex": {"allowed": ["absent-model"], "default": "absent-model"}}}}
    _patch_sys_settings(monkeypatch, ss)
    _stub_tags(monkeypatch, names=names[:-1] + [f"{builtin}:latest"], show={builtin: {"capabilities": ["completion"]}})

    body = admin_client.post(_URL, json={"ollama_url": _LAN}).json()

    assert body["ok"] is True and body["no_tools"] == [builtin]
