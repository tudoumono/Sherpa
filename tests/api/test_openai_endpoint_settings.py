"""GET /settings の接続先表示（`openai_endpoint_kind`・`openai_base_url_host`）と、
POST /settings/test（codex 分岐）・admin 専用 POST /admin/settings/openai-endpoint-test の契約。

接続先の判定そのものは `sherpa/llm.py::openai_base_url` / `openai_endpoint_kind` の担当。
要 Postgres。DB 不可は SKIP。
"""
from __future__ import annotations

import shutil
import subprocess
import time
import urllib.error

import pytest
from fastapi.testclient import TestClient

from _test_users import register_test_uid
from sherpa import auth, keys as keys_mod, llm, model_catalog, store
from sherpa.api import app
from sherpa.ingest import graph_extract

_FIXED_LABEL = "(不正な保存値)"
_AZURE_CATALOG = {"codex": {"codex": {"allowed": ["my-deployment"], "default": "my-deployment"}}}


def _sfx() -> str:
    return str(time.time_ns())[-13:]


def _login_new(role: str) -> TestClient:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    sfx = _sfx()
    uid, pw = f"oaiep{role[0]}{sfx}", f"pw-{sfx}"
    store.upsert_user(uid, email=f"{uid}@openaiendpoint.local", display_name=uid,
                      password_hash=auth.hash_password(pw), role=role, status="active")
    register_test_uid(uid)
    c = TestClient(app, raise_server_exceptions=False)
    r = c.post("/auth/login", json={"username": uid, "password": pw})
    assert r.status_code == 200, r.text
    return c


@pytest.fixture
def user_client() -> TestClient:
    return _login_new("user")


@pytest.fixture
def admin_client() -> TestClient:
    return _login_new("admin")


def _patch_endpoint(monkeypatch, kind, base_url):
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: kind, raising=False)
    if callable(base_url):
        monkeypatch.setattr(llm, "openai_base_url", base_url, raising=False)
    else:
        monkeypatch.setattr(llm, "openai_base_url", lambda system_settings=None: base_url, raising=False)


def _patch_sys_settings(monkeypatch, value):
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: value)


def _forbid(msg):
    def _boom(*a, **k):
        raise AssertionError(msg)
    return _boom


def _stub_codex_cli(monkeypatch, *, login=None):
    """codex CLI を見つかる状態にする。`login` は `codex login status` の (rc, stdout, stderr)。
    None なら `codex login status` が呼ばれたら失敗。"""
    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    if login is None:
        monkeypatch.setattr(subprocess, "run", _forbid("codex login status を呼ぶべきではない"))
    else:
        rc, out, err = login
        monkeypatch.setattr(
            subprocess, "run",
            lambda *a, **k: type("R", (), {"returncode": rc, "stdout": out, "stderr": err})())


# ===== GET /settings の表示 =====

def test_llm_endpoint_helpers_exist_and_are_called_directly():
    """`_openai_endpoint_kind`/`_openai_base_url_host`（system.py）は `llm` の関数を直接呼ぶ
    （欠落を隠す防御を挟まない）。"""
    assert callable(getattr(llm, "openai_endpoint_kind", None))
    assert callable(getattr(llm, "openai_base_url", None))

    from sherpa.routers import system as system_router
    assert system_router._openai_endpoint_kind() in ("openai", "azure", "custom")
    assert isinstance(system_router._openai_base_url_host(), str)


def test_default_env_reports_openai_kind_via_real_get_settings(user_client):
    r = user_client.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_endpoint_kind"] == "openai"
    assert "openai_base_url_host" in body


@pytest.mark.parametrize("kind, base_url, expected_host, absent", [
    # ホスト名のみを返す（パスは含めない）
    ("azure", "https://my-resource.openai.azure.com/openai/v1", "my-resource.openai.azure.com", ["openai/v1"]),
    ("custom", "https://gateway.internal.example.com/v1", "gateway.internal.example.com", []),
    # 表示前の再検証で不合格（クエリ付き・バックスラッシュ混入の旧保存値）なら固定文字列
    ("azure", "https://my-resource.openai.azure.com/openai/v1/?api-version=2024-10-21", _FIXED_LABEL,
     ["api-version"]),
    ("azure", "https://host.example\\internal\\secret", _FIXED_LABEL, ["internal", "secret"]),
])
def test_get_settings_reflects_endpoint_kind_and_host_only(
        monkeypatch, user_client, kind, base_url, expected_host, absent):
    _patch_endpoint(monkeypatch, kind, base_url)
    r = user_client.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_endpoint_kind"] == kind
    assert body["openai_base_url_host"] == expected_host
    for s in absent:
        assert s not in body["openai_base_url_host"]


def test_endpoint_helper_exception_falls_back_safely(monkeypatch, user_client):
    """`llm.openai_base_url()` が例外を投げても GET /settings は 500 にならず空文字へ倒れる。"""
    def _boom(system_settings=None):
        raise RuntimeError("boom")

    _patch_endpoint(monkeypatch, "azure", _boom)
    r = user_client.get("/settings")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["openai_endpoint_kind"] == "azure"
    assert body["openai_base_url_host"] == ""


# ===== POST /settings/test の codex 分岐（_select_provider と同じ判定を共有する） =====

def test_settings_test_codex_default_endpoint_still_checks_login_status(monkeypatch, user_client):
    _stub_codex_cli(monkeypatch, login=(0, "logged in", ""))
    r = user_client.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert r.json()["detail"] == "接続OK"


def test_settings_test_codex_azure_missing_key_reports_reason_without_login_status(monkeypatch, user_client):
    _stub_codex_cli(monkeypatch)
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    r = user_client.post("/settings/test", json={"provider": "codex", "codex_model": "my-deployment"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "キー" in body["detail"]


def _azure_codex_probe_setup(monkeypatch, complete_json):
    """Azure 構成の codex 接続テスト用: login status は呼ばせず、実接続（complete_json）だけ差し替える。
    入力中キーも personal_api_keys_allowed のゲートを通り、モデル名はカタログから解決される。"""
    _stub_codex_cli(monkeypatch)
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(graph_extract, "complete_json", complete_json)
    _patch_sys_settings(monkeypatch, {"personal_api_keys_allowed": True, "model_catalog": _AZURE_CATALOG})


def test_settings_test_codex_azure_fully_configured_skips_login_status(monkeypatch, user_client):
    _azure_codex_probe_setup(monkeypatch, lambda system, user, cfg, **kw: '{"ok":true}')
    r = user_client.post("/settings/test", json={"provider": "codex", "openai_api_key": "sk-azure-key"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True
    assert "codex login" in r.json()["detail"]


def test_settings_test_codex_azure_real_connection_failure_is_not_reported_ok(monkeypatch, user_client):
    """形式確認を通っても実接続が 401 なら ok=False で、失敗理由にキーが混入しない。"""
    def _fake_complete_json(system, user, cfg, **kw):
        raise urllib.error.HTTPError("https://myres.openai.azure.com/openai/v1/chat/completions",
                                     401, "Unauthorized", {}, None)

    _azure_codex_probe_setup(monkeypatch, _fake_complete_json)
    r = user_client.post("/settings/test", json={"provider": "codex", "openai_api_key": "sk-azure-bad-key"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "401" in body["detail"]
    assert "sk-azure-bad-key" not in body["detail"]


class _FakeOllamaTags:
    """Ollama の /api/tags の応答（外部境界）。組み込み既定の Codex モデル名を取得済みにする。"""
    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def read(self):
        import json as _json
        name = model_catalog.resolve_model("codex", "codex", None, system_settings={})
        return _json.dumps({"models": [{"name": f"{name}:latest"}]}).encode()


def _corrupted_kind(system_settings=None):
    raise ValueError("接続先設定（openai_endpoint_kind）の保存値が不正です（文字列ではありません）")


@pytest.mark.parametrize("kind_fn", [
    lambda system_settings=None: "azure",   # Azure 判定の対象外
    _corrupted_kind,                          # 型破損で kind 解決が ValueError でも Ollama 分岐が先に確定する
], ids=["azure-env", "corrupted-kind"])
def test_settings_test_codex_ollama_construct_ignores_endpoint_kind(monkeypatch, user_client, kind_fn):
    """Codex(Ollama) 構成は codex login ではなく Ollama に届くか・モデルがあるかを見る。"""
    assert user_client.put("/settings", json={"codex_model_provider": "ollama"}).status_code == 200
    _stub_codex_cli(monkeypatch, login=(1, "", "not logged in"))
    monkeypatch.setattr(llm, "urlopen_no_redirect", lambda *a, **k: _FakeOllamaTags())
    monkeypatch.setattr(llm, "openai_endpoint_kind", kind_fn, raising=False)

    r = user_client.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["detail"].startswith("接続OK")


def test_settings_test_codex_ollama_sandbox_disabled_reports_fail_closed(monkeypatch, user_client):
    """`SHERPA_CODEX_SANDBOX=0` のとき `codex login status` を見る前に fail-closed で ok=False。"""
    assert user_client.put("/settings", json={"codex_model_provider": "ollama"}).status_code == 200
    login_calls = []
    _stub_codex_cli(monkeypatch)
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    monkeypatch.setattr(
        subprocess, "run",
        lambda *a, **k: (login_calls.append(a) or
                         type("R", (), {"returncode": 0, "stdout": "logged in", "stderr": ""})()))

    r = user_client.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "サンドボックス" in body["detail"]
    assert login_calls == []


def test_settings_test_codex_azure_branch_shares_one_system_settings_snapshot(monkeypatch, user_client):
    """入口で取得した `sys_s` を1回だけ読み、キー・モデル解決の両方へ同一オブジェクトで渡す。
    本文に openai_api_key を含めない＝`resolve_api_key` が実際に実行される経路を通す。"""
    _stub_codex_cli(monkeypatch)
    monkeypatch.setattr(llm, "openai_endpoint_kind", lambda system_settings=None: "azure", raising=False)
    monkeypatch.setattr(graph_extract, "complete_json", lambda system, user, cfg, **kw: '{"ok":true}')

    read_calls = []
    sentinel = {"personal_api_keys_allowed": True, "cloud_provider": "openai",
                "openai_api_key": "sk-central-azure", "model_catalog": _AZURE_CATALOG}

    def _spy_get_system_settings():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy_get_system_settings)

    seen_by_name: dict[str, list] = {}

    def _spy(name, real):
        def _wrapped(*a, **kw):
            seen_by_name.setdefault(name, []).append(kw.get("system_settings"))
            return real(*a, **kw)
        return _wrapped

    monkeypatch.setattr(keys_mod, "resolve_api_key", _spy("resolve_api_key", keys_mod.resolve_api_key))
    monkeypatch.setattr(model_catalog, "resolve_model", _spy("resolve_model", model_catalog.resolve_model))

    r = user_client.post("/settings/test", json={"provider": "codex"})
    assert r.status_code == 200, r.text
    assert r.json()["ok"] is True

    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    missing = {"resolve_api_key", "resolve_model"} - set(seen_by_name)
    assert not missing, f"呼ばれなかったヘルパー: {missing}"
    for name, snaps in seen_by_name.items():
        assert all(snap is sentinel for snap in snaps), \
            f"{name} が settings_test と異なる system_settings オブジェクトを受け取った"


def test_settings_test_openai_ignores_endpoint_override_from_general_user(monkeypatch, user_client):
    """一般ユーザーが本文に接続先 override（`openai_base_url` 等）を含めても無視され、
    保存済みの接続先で probe される（任意宛先へ中央キーを送れる SSRF の再現）。"""
    captured = {}

    def _fake_complete_json(system, user, cfg, **kw):
        captured["cfg"] = cfg
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    _patch_sys_settings(monkeypatch, {"cloud_provider": "openai", "openai_api_key": "sk-central-real"})

    r = user_client.post("/settings/test", json={
        "provider": "openai",
        "openai_endpoint_kind": "custom",
        "openai_base_url": "https://evil.example.com/v1",
        "openai_auth_header": "api-key",
    })
    assert r.status_code == 200, r.text

    override = captured["cfg"].get("openai_endpoint_override")
    resolved_url = llm.openai_url("chat/completions", system_settings=override)
    assert resolved_url == "https://api.openai.com/v1/chat/completions", \
        f"一般ユーザーが指定した接続先が使われてしまっている: {resolved_url}"


# ===== admin 専用 POST /admin/settings/openai-endpoint-test =====

_EP_TEST = "/admin/settings/openai-endpoint-test"


def test_admin_openai_endpoint_test_requires_admin(user_client):
    r = user_client.post(_EP_TEST, json={
        "provider": "openai", "openai_endpoint_kind": "custom",
        "openai_base_url": "https://evil.example.com/v1"})
    assert r.status_code == 403, r.text


def test_admin_openai_endpoint_test_rejects_invalid_base_url_before_probing(monkeypatch, admin_client):
    """http:// や userinfo 付きは 422 で、`complete_json` を呼ばない（通信前の検証）。"""
    monkeypatch.setattr(graph_extract, "complete_json", _forbid("不正な入力なのに complete_json が呼ばれた"))
    for base_url in ("http://evil.example.com/v1", "https://user:pass@evil.example.com/v1"):
        r = admin_client.post(_EP_TEST, json={
            "provider": "openai", "openai_endpoint_kind": "custom", "openai_base_url": base_url})
        assert r.status_code == 422, r.text


def test_admin_openai_endpoint_test_rejects_non_openai_kind_without_base_url(admin_client):
    r = admin_client.post(_EP_TEST, json={"provider": "openai", "openai_endpoint_kind": "azure"})
    assert r.status_code == 422, r.text


def test_admin_openai_endpoint_test_uses_central_key_and_model_not_personal(monkeypatch, admin_client):
    """個人キー許可が有効でも常に中央キー・中央カタログ既定で試す（`user_settings` は常に None）。"""
    captured = {}

    def _fake_complete_json(system, user, cfg, **kw):
        captured["cfg"] = cfg
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    _patch_sys_settings(monkeypatch, {
        "cloud_provider": "openai", "personal_api_keys_allowed": True,
        "openai_api_key": "sk-central-real",
        "model_catalog": {"openai": {"chat": {"allowed": ["gpt-central-deploy"],
                                              "default": "gpt-central-deploy"}}},
    })
    seen_user_settings = []
    real_resolve_api_key = keys_mod.resolve_api_key
    real_resolve_model = model_catalog.resolve_model

    def _spy_resolve_api_key(provider, user_settings, **kw):
        seen_user_settings.append(user_settings)
        return real_resolve_api_key(provider, user_settings, **kw)

    def _spy_resolve_model(provider, usage, user_settings, **kw):
        seen_user_settings.append(user_settings)
        return real_resolve_model(provider, usage, user_settings, **kw)

    monkeypatch.setattr(keys_mod, "resolve_api_key", _spy_resolve_api_key)
    monkeypatch.setattr(model_catalog, "resolve_model", _spy_resolve_model)

    r = admin_client.post(_EP_TEST, json={"provider": "openai"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is True
    assert body["model"] == "gpt-central-deploy"
    assert captured["cfg"]["key"] == "sk-central-real"
    assert seen_user_settings, "resolve_api_key/resolve_model が呼ばれなかった"
    assert all(us is None for us in seen_user_settings), \
        f"個人設定（user_settings）が参照されている: {seen_user_settings}"


def test_admin_openai_endpoint_test_input_key_overrides_saved_central_key(monkeypatch, admin_client):
    captured = {}

    def _fake_complete_json(system, user, cfg, **kw):
        captured["cfg"] = cfg
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    _patch_sys_settings(monkeypatch, {"cloud_provider": "openai", "openai_api_key": "sk-old-saved-key"})

    r = admin_client.post(_EP_TEST, json={"provider": "openai", "openai_api_key": "sk-input-in-progress"})
    assert r.status_code == 200, r.text
    assert captured["cfg"]["key"] == "sk-input-in-progress"


_AZURE_BODY = {"provider": "codex", "openai_endpoint_kind": "azure",
               "openai_base_url": "https://myres.openai.azure.com/openai/v1",
               "openai_api_key": "sk-azure-central-key"}


def test_admin_openai_endpoint_test_codex_branch_shares_pending_snapshot(monkeypatch, admin_client):
    """provider=codex も入力中の接続先 override を同じ pending スナップショットで Azure 判定へ渡し、
    モデルは中央カタログ既定で解決する。"""
    monkeypatch.setattr(graph_extract, "complete_json", lambda system, user, cfg, **kw: '{"ok":true}')
    _patch_sys_settings(monkeypatch, {
        "cloud_provider": "openai", "personal_api_keys_allowed": True,
        "model_catalog": {"codex": {"codex": {"allowed": ["my-azure-deployment"],
                                              "default": "my-azure-deployment"}}}})

    r = admin_client.post(_EP_TEST, json=_AZURE_BODY)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "codex"
    assert body["model"] == "my-azure-deployment"
    assert body["ok"] is True


def test_admin_openai_endpoint_test_codex_ignores_codex_model_field(monkeypatch, admin_client):
    """`codex_model` は無視されカタログ既定（gpt-5.5）で解決され、Azure 判定がブロックして probe に到達しない。"""
    called = []

    def _fake_complete_json(system, user, cfg, **kw):
        called.append(cfg)
        return '{"ok":true}'

    monkeypatch.setattr(graph_extract, "complete_json", _fake_complete_json)
    _patch_sys_settings(monkeypatch, {"cloud_provider": "openai", "personal_api_keys_allowed": True})

    r = admin_client.post(_EP_TEST, json={**_AZURE_BODY, "codex_model": "arbitrary-uncataloged-name"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["provider"] == "codex"
    assert body["model"] == "gpt-5.5"
    assert body["ok"] is False
    assert "デプロイ名" in body["detail"]
    assert called == []


def test_admin_openai_endpoint_test_reports_missing_central_key(monkeypatch, admin_client):
    _patch_sys_settings(monkeypatch, {"cloud_provider": "openai"})   # 共有 DB の状態に依存させない
    r = admin_client.post(_EP_TEST, json={"provider": "openai"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "管理者が" in body["detail"]


def test_admin_openai_endpoint_test_rejects_invalid_cloud_provider_without_probing(monkeypatch, admin_client):
    """`cloud_provider` が非空の不正値なら、寛容なキー解決で既定 openai 扱いのキーを実送信しない。"""
    _patch_sys_settings(monkeypatch, {"cloud_provider": "not-a-real-provider", "openai_api_key": "sk-central"})
    boom = _forbid("不正な cloud_provider なのに実送信してしまった")
    monkeypatch.setattr(graph_extract, "complete_json", boom)
    monkeypatch.setattr(graph_extract, "_probe", boom)

    r = admin_client.post(_EP_TEST, json={"provider": "openai"})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["ok"] is False
    assert "cloud_provider" in body["detail"]


def _assert_deny_audit_for_invalid_base_url(row: dict) -> None:
    assert row["outcome"] == "deny"
    assert row["reason"] == "invalid_base_url"
    assert row["severity"] == "warning"
    assert row["detail"]["host"] == _FIXED_LABEL


_BAD_INHERITED_BASE_URLS = (
    # kind=azure（接続先の整合検査まで進む）: 文字列だが不正・非文字列・falsy な非文字列
    [("azure", "https://host.example\\internal\\secret")]
    + [("azure", v) for v in (["https://host.example"], {"nested": "value"}, 12345, {}, [], 0, False)]
    # kind=openai／未設定でも型検査が本家既定への早期 return より先に効く
    + [(k, v) for k in ("openai", None) for v in ({}, [], 0, False)]
)


@pytest.mark.parametrize("saved_kind, bad_value", _BAD_INHERITED_BASE_URLS)
def test_admin_openai_endpoint_test_rejects_invalid_inherited_saved_base_url(
        monkeypatch, admin_client, saved_kind, bad_value):
    """`openai_base_url` を省略した接続テストが継承する保存済み値が不正（バックスラッシュ混入・非文字列・
    falsy な非文字列）でも、使用直前の再検証で 422＋deny 監査（固定文字列 host）となり、probe は呼ばれない。
    監査行は直前の最新 id と比べて新規に書かれたことを確認する（古い行で緑にしない）。"""
    saved = {"openai_base_url": bad_value}
    if saved_kind is not None:
        saved["openai_endpoint_kind"] = saved_kind
    _patch_sys_settings(monkeypatch, saved)
    monkeypatch.setattr(graph_extract, "complete_json", _forbid("不正な保存値なのに complete_json が呼ばれた"))

    before_rows = store.list_audit(action="openai_endpoint.tested", limit=1)
    before_id = before_rows[0]["id"] if before_rows else None

    r = admin_client.post(_EP_TEST, json={"provider": "openai"})
    assert r.status_code == 422, f"kind={saved_kind!r} base={bad_value!r}: {r.text}"

    rows = store.list_audit(action="openai_endpoint.tested", limit=5)
    assert rows, "接続テストの監査行が記録されていない（fail-closed で probe 前に記録する契約）"
    assert rows[0]["id"] != before_id, "新しい監査行が書かれていない（stale row）"
    assert "internal" not in rows[0]["detail"]["host"] and "secret" not in rows[0]["detail"]["host"]
    _assert_deny_audit_for_invalid_base_url(rows[0])


@pytest.mark.parametrize("bad_value", [{}, [], 0, False, ["https://host.example"]])
def test_admin_settings_view_does_not_crash_on_falsy_non_string_saved_base_url(
        monkeypatch, admin_client, bad_value):
    """非文字列の保存値でも `GET /admin/settings` は 500 にならず、表示は固定文字列へ倒れる。"""
    _patch_sys_settings(monkeypatch, {"openai_endpoint_kind": "azure", "openai_base_url": bad_value})
    r = admin_client.get("/admin/settings")
    assert r.status_code == 200, f"{bad_value!r}: {r.text}"
    body = r.json()
    assert body["openai_endpoint"]["effective"]["base_url"] == _FIXED_LABEL
    assert body["openai_endpoint"]["configured"]["base_url"] == bad_value
