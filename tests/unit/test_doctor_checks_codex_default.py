"""doctor が Codex の接続先を実効値で見ることの確認。"""
from scripts import doctor_checks
from sherpa import agent_constructs


def test_resolve_ollama_usages_counts_unselected_codex_users_by_effective_provider(monkeypatch, tmp_path):
    """接続先を選んでいない Codex の利用者・個人設定を保存していない利用者も、実効が Ollama なら点検の対象に入る。"""
    from sherpa import required_tools
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "empty"))
    monkeypatch.setattr(agent_constructs.shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)
    monkeypatch.setattr(required_tools, "codex_cli_missing", lambda: False)
    sys_s = {"research_default_provider": "openai", "openai_api_key": "sk-x",
             "codex_default_model_provider": "ollama"}

    def _codex(usages):
        return [u for u in usages if "Codex(Ollama) 実行モデル" in u["purposes"]]

    assert _codex(doctor_checks._resolve_ollama_usages(sys_s, [{"agent": "codex", "codex_model_provider": ""}]))
    assert _codex(doctor_checks._resolve_ollama_usages(sys_s, [{"agent": None, "codex_model_provider": None}]))
    assert not _codex(doctor_checks._resolve_ollama_usages({**sys_s, "codex_default_model_provider": "openai"},
                                                             [{"agent": None, "codex_model_provider": None}]))
