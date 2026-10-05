"""Codex(OpenAI) 構成の接続先（Azure OpenAI 等）切替の単体テスト。

接続先は `system_settings`（DB）が唯一の真実源。`sysset` フィクスチャ（可変 dict）で
`sherpa.store.get_system_settings` を差し替え、各テストが辞書へ直接キーを足して制御する。
実 Codex CLI・実 Azure/OpenAI は呼ばない（config.toml の生成物と `_select_provider` の結果だけ検証）。
"""
from __future__ import annotations

import inspect
import shutil

import pytest

from sherpa.providers import _select_provider, _UnwiredProvider
from sherpa.providers.codex import sandbox
from sherpa.providers.codex.sandbox import (
    _openai_compat_base_url,
    _openai_compat_provider_lines,
    _write_codex_authoring_config,
    codex_multi_agent_enabled,
)

AZURE_URL = "https://myres.openai.azure.com/openai/v1"
OLLAMA_URL = "http://127.0.0.1:11434"


@pytest.fixture(autouse=True)
def sysset(monkeypatch):
    state = {"personal_api_keys_allowed": True}
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: dict(state))
    return state


@pytest.fixture(autouse=True)
def _codex_cli_present(monkeypatch):
    # `_select_provider` は先に `shutil.which("codex")` を見る＝開発機の有無に依らず「ある」に固定。
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)


def _azure(sysset, **extra) -> None:
    sysset["openai_endpoint_kind"] = "azure"
    sysset["openai_base_url"] = AZURE_URL
    sysset.update(extra)


def _custom(sysset, **extra) -> None:
    sysset["openai_endpoint_kind"] = "custom"
    sysset["openai_base_url"] = "https://example.com/v1"
    sysset.update(extra)


def _catalog(model: str) -> dict:
    return {"codex": {"codex": {"allowed": [model], "default": model}}}


def _config_text(tmp_path, **kw) -> str:
    ch = tmp_path / "ch"
    _write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None, **kw)
    return (ch / "config.toml").read_text(encoding="utf-8")


def _role_toml(tmp_path, role: str) -> str:
    return (tmp_path / "ch" / "agents" / f"{role}.toml").read_text(encoding="utf-8")


def _loads_toml(txt: str) -> None:
    try:
        import tomllib
    except ModuleNotFoundError:
        return
    tomllib.loads(txt)


# ===== 1. _openai_compat_provider_lines =====

def test_provider_lines_bearer_no_api_version():
    txt = "\n".join(_openai_compat_provider_lines(AZURE_URL, api_version=None, auth_header="bearer"))
    assert 'model_provider = "sherpa-openai-compat"' in txt
    assert "[model_providers.sherpa-openai-compat]" in txt
    assert f'base_url = "{AZURE_URL}"' in txt
    assert 'env_key = "OPENAI_API_KEY"' in txt
    assert 'wire_api = "responses"' in txt
    assert "query_params" not in txt
    assert "http_headers" not in txt


def test_provider_lines_with_api_version():
    txt = "\n".join(_openai_compat_provider_lines(
        AZURE_URL, api_version="2025-04-01-preview", auth_header="bearer"))
    assert 'query_params = { "api-version" = "2025-04-01-preview" }' in txt


def test_provider_lines_api_key_mode_uses_env_http_headers_not_literal():
    lines = _openai_compat_provider_lines(AZURE_URL, api_version=None, auth_header="api-key")
    assert 'env_http_headers = { "api-key" = "OPENAI_API_KEY" }' in "\n".join(lines)
    assert not any(ln.startswith("http_headers") for ln in lines)   # 静的ヘッダ（キー値 literal）を書かない


def test_provider_lines_toml_string_escaping():
    txt = "\n".join(_openai_compat_provider_lines(
        'https://evil".openai.azure.com/v1', api_version=None, auth_header="bearer"))
    assert '\\"' in txt
    _loads_toml('model = "x"\n' + txt)


# ===== 2. _write_codex_authoring_config =====

def test_default_endpoint_writes_no_model_provider_lines(tmp_path):
    txt = _config_text(tmp_path)
    assert "model_provider" not in txt
    assert "model_providers" not in txt
    assert "sherpa-openai-compat" not in txt


def test_azure_endpoint_writes_model_provider_lines(tmp_path, sysset):
    _azure(sysset)
    txt = _config_text(tmp_path)
    assert 'model_provider = "sherpa-openai-compat"' in txt
    assert "[model_providers.sherpa-openai-compat]" in txt
    assert f'base_url = "{AZURE_URL}"' in txt
    assert 'wire_api = "responses"' in txt
    _loads_toml(txt)


def test_azure_endpoint_with_api_version_and_api_key_header(tmp_path, sysset):
    _azure(sysset, openai_auth_header="api-key", openai_api_version="2024-10-21")
    txt = _config_text(tmp_path)
    assert 'query_params = { "api-version" = "2024-10-21" }' in txt
    assert 'env_http_headers = { "api-key" = "OPENAI_API_KEY" }' in txt
    assert "OPENAI_API_KEY" in txt
    for line in txt.splitlines():
        if line.strip().startswith(("query_params", "env_http_headers", "base_url", "env_key")):
            assert "sk-" not in line, f"キーらしき文字列が config に出ている: {line!r}"


def test_ollama_construct_ignores_azure_settings(tmp_path, sysset):
    _azure(sysset)
    txt = _config_text(tmp_path, ollama_base_url="http://127.0.0.1:11500/")
    assert 'model_provider = "sherpa-ollama"' in txt
    assert "sherpa-openai-compat" not in txt


# ===== multi_agent の有効判定（接続構成ごと） =====

def test_codex_multi_agent_enabled_for_default_azure_and_ollama(sysset):
    assert codex_multi_agent_enabled(ollama_base_url=None, system_settings=None) is True
    assert codex_multi_agent_enabled(ollama_base_url=OLLAMA_URL, system_settings=None) is True
    _azure(sysset)
    assert codex_multi_agent_enabled(ollama_base_url=None, system_settings=None) is True


def test_codex_multi_agent_disabled_for_ollama_when_sandbox_disabled(monkeypatch):
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    assert codex_multi_agent_enabled(ollama_base_url=OLLAMA_URL, system_settings=None) is False


def test_codex_multi_agent_custom_endpoint_requires_explicit_worker_model(sysset):
    # custom は worker の既定モデルがその接続先に在る保証が無い＝codex_worker_model 明示時のみ有効。
    _custom(sysset)
    assert codex_multi_agent_enabled(ollama_base_url=None, system_settings=None) is False
    _custom(sysset, codex_worker_model="my-custom-deployment")
    assert codex_multi_agent_enabled(ollama_base_url=None, system_settings=None) is True


# ===== role config（worker/evaluator） =====

def test_azure_multi_agent_role_configs_use_main_model_and_same_provider_as_parent(tmp_path, sysset):
    _azure(sysset)
    enabled = codex_multi_agent_enabled(ollama_base_url=None, system_settings=None)
    assert enabled is True
    txt = _config_text(tmp_path, multi_agent=enabled, orchestrator_model="my-deployment",
                       system_settings=dict(sysset))
    assert "[agents]" in txt
    assert "[agents.worker]" in txt and "[agents.evaluator]" in txt
    assert 'model_provider = "sherpa-openai-compat"' in txt
    for role in ("worker", "evaluator"):
        role_txt = _role_toml(tmp_path, role)
        assert 'model = "my-deployment"' in role_txt          # 本体と同じデプロイ名
        # 子 Codex が組み込み openai provider（本家）へ出ないよう親と同じ provider 行を書く。
        assert 'model_provider = "sherpa-openai-compat"' in role_txt
        assert "[model_providers.sherpa-openai-compat]" in role_txt
        assert f'base_url = "{AZURE_URL}"' in role_txt


def test_azure_multi_agent_worker_uses_configured_value_over_main_model(tmp_path, sysset):
    _azure(sysset, codex_worker_model="gpt-5.9-custom")
    enabled = codex_multi_agent_enabled(ollama_base_url=None, system_settings=None)
    assert _config_text(tmp_path, multi_agent=enabled, orchestrator_model="my-deployment",
                        system_settings=dict(sysset))
    assert 'model = "gpt-5.9-custom"' in _role_toml(tmp_path, "worker")


def test_default_endpoint_role_configs_have_developer_instructions_and_no_provider_lines(tmp_path):
    enabled = codex_multi_agent_enabled(ollama_base_url=None, system_settings=None)
    txt = _config_text(tmp_path, multi_agent=enabled, orchestrator_model="gpt-5.5")
    assert "[agents.worker]" in txt
    for role in ("worker", "evaluator"):
        role_txt = _role_toml(tmp_path, role)
        assert "developer_instructions = " in role_txt
        assert "model_provider" not in role_txt and "model_providers" not in role_txt


def test_ollama_multi_agent_role_configs_use_main_model_and_ollama_provider(tmp_path):
    enabled = codex_multi_agent_enabled(ollama_base_url=OLLAMA_URL, system_settings=None)
    assert enabled is True
    txt = _config_text(tmp_path, multi_agent=enabled, orchestrator_model="llama3.1:8b",
                       ollama_base_url=OLLAMA_URL)
    assert "[agents]" in txt
    assert "[agents.worker]" in txt and "[agents.evaluator]" in txt
    worker_toml = _role_toml(tmp_path, "worker")
    assert 'model = "llama3.1:8b"' in worker_toml
    assert 'model_provider = "sherpa-ollama"' in worker_toml
    assert "[model_providers.sherpa-ollama]" in worker_toml
    evaluator_toml = _role_toml(tmp_path, "evaluator")
    assert 'model = "llama3.1:8b"' in evaluator_toml
    assert 'model_provider = "sherpa-ollama"' in evaluator_toml


def test_ollama_multi_agent_worker_uses_configured_value_over_main_model(tmp_path, sysset):
    sysset["codex_worker_model"] = "gpt-5.9-custom"
    enabled = codex_multi_agent_enabled(ollama_base_url=OLLAMA_URL, system_settings=None)
    assert _config_text(tmp_path, multi_agent=enabled, orchestrator_model="llama3.1:8b",
                        ollama_base_url=OLLAMA_URL, system_settings=dict(sysset))
    assert 'model = "gpt-5.9-custom"' in _role_toml(tmp_path, "worker")


# ===== 多層防御: sandbox.py 側でも base URL を検証 =====

@pytest.mark.parametrize("bad_base_url", [
    "http://myres.openai.azure.com/openai/v1",
    {}, [], 0, False,   # falsy な非文字列が本家 OpenAI 既定 URL へ黙って縮退してはならない
])
def test_invalid_base_url_is_rejected_not_degraded_to_default(tmp_path, sysset, bad_base_url):
    _azure(sysset, openai_base_url=bad_base_url)
    with pytest.raises(ValueError):
        _openai_compat_base_url()
    with pytest.raises(ValueError):
        _write_codex_authoring_config(tmp_path / "ch", ["/kb"], "low", False, "test", None)


# ===== 3. web_search の強制 OFF =====

def test_web_search_disabled_value_forces_off_for_non_openai_endpoint():
    allowed = {"web_search_allowed": True}
    assert sandbox._web_search_disabled_value(True, system_settings=allowed) is None
    assert sandbox._web_search_disabled_value(True, "openai", allowed) is None
    assert sandbox._web_search_disabled_value(True, "azure", allowed) == "disabled"
    assert sandbox._web_search_disabled_value(True, "custom", allowed) == "disabled"
    assert sandbox._web_search_disabled_value(False, "azure", allowed) == "disabled"


def test_config_web_search_forced_off_when_azure_even_if_admin_and_user_allow(tmp_path, sysset):
    _azure(sysset)
    sysset["web_search_allowed"] = True
    assert 'web_search = "disabled"' in _config_text(tmp_path, web_search_enabled=True)


def test_web_search_endpoint_note_only_when_would_have_been_enabled():
    allowed = {"web_search_allowed": True}
    assert sandbox._web_search_endpoint_note(True, "openai", allowed) is None
    assert sandbox._web_search_endpoint_note(True, "azure") is None
    assert sandbox._web_search_endpoint_note(False, "azure", allowed) is None
    note = sandbox._web_search_endpoint_note(True, "azure", allowed)
    assert note and "Azure" in note


# ===== 4. _codex_clean_env =====

def test_codex_clean_env_injects_key_only_when_explicitly_passed(tmp_path, monkeypatch):
    from sherpa.providers.codex.sandbox import _codex_clean_env

    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak-by-default")
    assert "OPENAI_API_KEY" not in _codex_clean_env(tmp_path / "ch", tmp_path / "tmp")
    env_azure = _codex_clean_env(tmp_path / "ch2", tmp_path / "tmp2", openai_api_key="sk-azure-key-value")
    assert env_azure["OPENAI_API_KEY"] == "sk-azure-key-value"
    env_empty = _codex_clean_env(tmp_path / "ch3", tmp_path / "tmp3", openai_api_key="")
    assert "OPENAI_API_KEY" not in env_empty


# ===== 5. _select_provider =====

def test_select_provider_default_endpoint_unaffected():
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai"})
    assert p.__class__.__name__ == "CodexProvider"
    assert p._openai_api_key is None
    assert p._ollama_base_url is None


_AZ_KEY = {"openai_api_key": "sk-real-azure-key"}
_OLLAMA_CFG = {"agent": "codex", "codex_model_provider": "ollama", "ollama_url": "http://localhost:11434"}


@pytest.mark.parametrize("azure_extra, cfg_extra, env, howto_has", [
    # キー未設定
    ({}, {"codex_model": "my-azure-deployment"}, None, ["キー"]),
    # codex_model 未設定／既定値（"gpt-5.5"）のまま＝デプロイ名が要る
    ({}, dict(_AZ_KEY), None, ["デプロイ名"]),
    ({}, {**_AZ_KEY, "codex_model": "gpt-5.5"}, None, ["デプロイ名"]),
    # base URL が http かつ非ループバック
    ({"openai_base_url": "http://myres.openai.azure.com/openai/v1"},
     {**_AZ_KEY, "codex_model": "my-deployment"}, None, ["接続先"]),
    # サンドボックス無効（fail-closed）
    ({}, {**_AZ_KEY, "codex_model": "my-deployment"}, "0", ["サンドボックス"]),
    # cloud_provider の不正値はキーを渡さず honest failure
    ({"cloud_provider": "not-a-real-provider", "model_catalog": _catalog("my-deployment")},
     {"openai_api_key": "sk-should-not-leak"}, None, ["not-a-real-provider"]),
])
def test_select_provider_azure_unwired_cases(sysset, monkeypatch, azure_extra, cfg_extra, env, howto_has):
    _azure(sysset, **azure_extra)
    if env is not None:
        monkeypatch.setenv("SHERPA_CODEX_SANDBOX", env)
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai", **cfg_extra})
    assert isinstance(p, _UnwiredProvider)
    for s in howto_has:
        assert s in p.howto


@pytest.mark.parametrize("sandbox_env", [None, "1"])
def test_select_provider_azure_with_key_and_model_wires_codex_provider(sysset, monkeypatch, sandbox_env):
    # モデル名は個人設定でなく管理者のカタログ（codex/codex）から解決される。
    _azure(sysset, model_catalog=_catalog("my-deployment"))
    if sandbox_env is not None:
        monkeypatch.setenv("SHERPA_CODEX_SANDBOX", sandbox_env)
    p = _select_provider({"agent": "codex", "codex_model_provider": "openai", **_AZ_KEY})
    assert p.__class__.__name__ == "CodexProvider"
    assert p._openai_api_key == "sk-real-azure-key"
    assert p._ollama_base_url is None
    assert p.model == "my-deployment"


def test_select_provider_narrows_exception_catch_to_invalid_model_name_only():
    # Codex 構成分岐の except 節を InvalidModelNameError に狭く保つ不変条件（ソース検査）。
    src = inspect.getsource(_select_provider)
    tail = src[src.index("_facade.CodexProvider(None, codex_model"):][:300]
    assert "except model_catalog.InvalidModelNameError as e:" in tail
    assert "except Exception" not in tail
    assert "except ValueError" not in tail


def test_select_provider_ollama_construct_ignores_azure_settings(sysset):
    _azure(sysset)
    sysset["model_catalog"] = _catalog("gpt-oss:20b")
    p = _select_provider(_OLLAMA_CFG)
    assert p.__class__.__name__ == "CodexProvider"
    assert p._ollama_base_url == "http://localhost:11434"
    assert p._openai_api_key is None


def test_select_provider_ollama_construct_sandbox_disabled_is_fail_closed(monkeypatch):
    # サンドボックス無効では独自 model_provider を書けず Codex が OpenAI へ黙って繋がる＝止める。
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    p = _select_provider(_OLLAMA_CFG)
    assert isinstance(p, _UnwiredProvider)
    assert "サンドボックス" in p.howto
    assert "OpenAI" in p.howto


def test_select_provider_ollama_construct_sandbox_enabled_explicit_still_wires(monkeypatch, sysset):
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "1")
    sysset["model_catalog"] = _catalog("gpt-oss:20b")
    p = _select_provider(_OLLAMA_CFG)
    assert p.__class__.__name__ == "CodexProvider"
    assert p._ollama_base_url == "http://localhost:11434"


def test_select_provider_ollama_construct_with_default_openai_model_is_unwired():
    assert _select_provider(_OLLAMA_CFG).__class__.__name__ == "_UnwiredProvider"


def test_select_provider_invalid_codex_model_provider_is_unwired():
    p = _select_provider({"agent": "codex", "codex_model_provider": "anthropic"})
    assert isinstance(p, _UnwiredProvider)
    assert "anthropic" in p.howto
