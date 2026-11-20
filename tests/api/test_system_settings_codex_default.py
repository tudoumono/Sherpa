"""管理者の全体設定 `codex_default_model_provider`（Codex 調査の既定の接続先）の保存と実効値。"""
from __future__ import annotations

import pytest
from test_system_settings import _ADMIN, _clean_system_settings, _put, admin, admin_pair

from sherpa import store


def test_codex_default_model_provider_effective_and_empty_reset(admin):
    r = _put(admin, {"codex_default_model_provider": " OpenAI "})
    assert r.status_code == 200, r.text
    info = r.json()["ext_keys"]["codex_default_model_provider"]
    assert (info["configured"], info["effective"]) == ("openai", "openai")
    assert store.get_system_settings()["codex_default_model_provider"] == "openai"
    assert _put(admin, {"chat_examples": {"items": ["z"]}}).status_code == 200   # 他項目の更新で消えない
    assert admin.get(_ADMIN).json()["ext_keys"]["codex_default_model_provider"]["configured"] == "openai"
    info = _put(admin, {"codex_default_model_provider": "ollama"}).json()["ext_keys"]["codex_default_model_provider"]
    assert (info["configured"], info["effective"]) == ("ollama", "ollama")
    r = _put(admin, {"codex_default_model_provider": ""})
    assert r.status_code == 200, r.text
    assert r.json()["ext_keys"]["codex_default_model_provider"]["configured"] is None


@pytest.mark.parametrize("bad", ["gemini", 3, ["openai"]])
def test_codex_default_model_provider_rejects_invalid(admin, bad):
    assert _put(admin, {"codex_default_model_provider": bad}).status_code == 422
