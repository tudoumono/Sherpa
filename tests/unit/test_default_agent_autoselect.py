"""既定の頭脳の自動選択と Codex CLI 不在時の正直な「未接続」。

契約:
- 環境変数 `SHERPA_AGENT` は読まない。個人設定が未選択のときは自動選択する。
- 自動選択は「codex CLI がある」→codex、無ければ simple（認証の有無は見ない）。Codex の接続先は
  `codex_model_provider()` が決める（認証＝解決済みの中央 OpenAI キーまたは `~/.codex/auth.json`
  （`CODEX_HOME` を尊重）があれば openai、無ければ ollama）。
- `_select_provider` は agent=codex で CLI が無いとき `_UnwiredProvider` を返す（ごまかさない）。
"""
from __future__ import annotations

import shutil

import pytest

from sherpa import agent_constructs as AC
from sherpa import providers

pytestmark = pytest.mark.unit


@pytest.fixture
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    # 実機の `~/.codex/auth.json` を読み込んでしまわないよう、既定では auth.json の無い一時
    # ディレクトリへ隔離する（CLAUDE.md: 実 auth.json は読むだけ・変更しない＝テストは専用の
    # 一時ディレクトリを使う）。auth.json の存在を試したいテストは自分でここへ書き込む。
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home_empty"))


def _which(mapping: dict[str, str | None]):
    return lambda name, *a, **k: mapping.get(name)


def test_auto_default_prefers_codex_when_cli_present(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    assert AC.default_agent() == "codex"


def test_auto_default_falls_to_openai_when_no_cli_but_key(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    assert AC.default_agent() == "simple"


def test_auto_default_falls_to_ollama_when_nothing(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({}))
    assert AC.default_agent() == "simple"


def test_auto_default_selects_codex_when_cli_present_but_no_auth_at_all(_clean_env, monkeypatch):
    """CLI があれば認証が無くても codex を選ぶ（接続先はローカル LLM になる）。"""
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {})
    assert AC.default_agent() == "codex"
    assert AC.construct_id({}, system_settings={}) == "codex_ollama"


def test_unselected_user_follows_admin_default_provider(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    for value in ("openai", "ollama"):
        sys_s = {"codex_default_model_provider": value}
        assert AC.codex_model_provider({}, system_settings=sys_s) == value
        assert AC.construct_id({}, system_settings=sys_s) == f"codex_{value}"


def test_auto_provider_is_openai_when_key_or_auth_json_exists(_clean_env, monkeypatch, tmp_path):
    assert AC.codex_model_provider({}, system_settings={"openai_api_key": "sk-x"}) == "openai"
    codex_home = tmp_path / "codex_home_with_auth"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert AC.codex_model_provider({}, system_settings={}) == "openai"


def test_own_choice_beats_admin_default(_clean_env):
    sys_s = {"codex_default_model_provider": "ollama"}
    assert AC.codex_model_provider({"codex_model_provider": "openai"}, system_settings=sys_s) == "openai"


def test_invalid_admin_default_raises(_clean_env):
    for bad in ("anthropic", 3):
        with pytest.raises(AC.InvalidCodexModelProviderError):
            AC.codex_model_provider({}, system_settings={"codex_default_model_provider": bad})
    assert AC.construct_id({"agent": "codex"}, system_settings={"codex_default_model_provider": "x"}) == "codex_invalid"


def test_sherpa_agent_env_is_ignored(_clean_env, monkeypatch):
    """環境変数 `SHERPA_AGENT` は読まない（個人設定が未選択のときは常に自動選択）。"""
    monkeypatch.setattr(shutil, "which", _which({}))
    for value in ("codex", "bogus-agent-name"):
        monkeypatch.setenv("SHERPA_AGENT", value)
        assert AC.default_agent() == "simple"
    p = providers._select_provider({})
    assert not isinstance(p, providers._UnwiredProvider)


def test_select_provider_codex_without_cli_is_unwired(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({}))
    p = providers._select_provider({"agent": "codex"})
    assert isinstance(p, providers._UnwiredProvider)
    assert "Codex CLI" in p.howto and "tools/codex" in p.howto
    assert "接続されていません" in p._plain_text()


def test_unwired_provider_run_includes_scope_in_env(_clean_env, monkeypatch):
    """honest failure（`_UnwiredProvider`）の env にも通常レスポンス同様 scope を含める
    （欠落させると会話再表示時に UI が「全体」と解釈し、再試行で検索範囲が World 全体へ
    広がる回帰の防止）。"""
    from sherpa.providers.base import Ctx
    from sherpa.providers import _UnwiredProvider

    p = _UnwiredProvider("Codex", "案内文")
    narrow_scope = {"world": "v1", "scope_paths": ["4期/"], "source": "scope"}
    ctx = Ctx(message="質問", world="v1", knowledge=True, scope_meta=narrow_scope,
             route=lambda m: {"lens": "qa", "reason": "t", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
             make_sources=lambda docs: [{"doc_id": d} for d in docs])
    result = next(e for e in p.run(ctx) if e.get("type") == "_result")
    # qa 相当の honest failure は layer_applied=True を含む scope 契約を保持する。
    assert result["env"]["scope"] == {**narrow_scope, "layer_applied": True}


def test_unwired_provider_preserves_requested_layer_value():
    """layer_applied だけでなく、要求された layer の値自体も欠落させない
    （UI が「探す対象」の直前選択を再表示できるように）。"""
    from sherpa.providers.base import Ctx
    from sherpa.providers import _UnwiredProvider

    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    p = _UnwiredProvider("Codex", "案内文")
    ctx = Ctx(message="質問", world="v1", knowledge=True, scope_meta=scope_meta,
             route=lambda m: {"lens": "qa", "reason": "t", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
             make_sources=lambda docs: [{"doc_id": d} for d in docs])
    result = next(e for e in p.run(ctx) if e.get("type") == "_result")
    assert result["env"]["scope"]["layer"] == "code"
    assert result["env"]["scope"]["layer_applied"] is True


def test_select_provider_codex_with_cli_builds_codex_provider(_clean_env, monkeypatch):
    from sherpa import agents as facade

    class _FakeCodex:
        def __init__(self, *a, **k):
            self.args = a
    monkeypatch.setattr(shutil, "which", _which({"codex": "/usr/bin/codex"}))
    monkeypatch.setattr(facade, "CodexProvider", _FakeCodex)
    p = providers._select_provider({"agent": "codex", "codex_model_provider": "openai"})
    assert isinstance(p, _FakeCodex)
