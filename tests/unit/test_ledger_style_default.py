"""調査の方式の判定: 標準モード（Codex(OpenAI)・Codex(Ollama)）で出力スキーマ v2 なら C-check（plan_main）、それ以外は A（json）。"""
from sherpa.providers.codex import plan_ledger


def _style(**kw):
    return plan_ledger.effective_style.real(**kw)


def test_standard_openai_with_schema_v2_is_c_check():
    assert _style(plain=False, schema_level=2) == "plan_main"


def test_plain_and_old_schema_keep_the_json_path():
    assert _style(plain=True, schema_level=2) == "json"
    assert _style(plain=False, schema_level=1) == "json"
    assert _style(plain=False, schema_level=0) == "json"


def test_the_style_has_no_environment_switch(monkeypatch):
    monkeypatch.setenv("SHERPA_LEDGER_STYLE", "json")
    assert _style(plain=False, schema_level=2) == "plan_main"
