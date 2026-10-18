"""調査の方式の既定が C-check（plan_main）であること。A（json）・B（plan_gate）は指定したときだけ使う。"""
from sherpa.providers.codex import plan_ledger


def test_default_style_is_c_check_when_unset(monkeypatch):
    monkeypatch.delenv("SHERPA_LEDGER_STYLE", raising=False)
    assert plan_ledger.ledger_style() == "plan_main"
    assert plan_ledger.effective_style(plain=False, ollama=False, schema_level=2) == "plan_main"


def test_unknown_value_falls_back_to_c_check(monkeypatch):
    monkeypatch.setenv("SHERPA_LEDGER_STYLE", "bogus")
    assert plan_ledger.ledger_style() == "plan_main"


def test_a_and_b_are_used_only_when_chosen(monkeypatch):
    for style in ("json", "plan_gate", "plan_main"):
        monkeypatch.setenv("SHERPA_LEDGER_STYLE", style)
        assert plan_ledger.ledger_style() == style


def test_plain_ollama_and_old_schema_keep_the_json_path(monkeypatch):
    monkeypatch.delenv("SHERPA_LEDGER_STYLE", raising=False)
    assert plan_ledger.effective_style(plain=True, ollama=False, schema_level=2) == "json"
    assert plan_ledger.effective_style(plain=False, ollama=True, schema_level=2) == "json"
    assert plan_ledger.effective_style(plain=False, ollama=False, schema_level=1) == "json"
