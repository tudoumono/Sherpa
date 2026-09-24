"""既定の頭脳の自動選択と Codex CLI 不在時の正直な「未接続」（閉域実機の是正・2026-08-18）。

不具合: `DEFAULT_CONSTRUCT_ID=codex_openai` のため、設定を忘れると既定＝Codex になり、Codex CLI が無い
閉域ホストでは provider.py の `shutil.which("codex")` 分岐が外れて決定的回答（定型文）だけが返っていた。

契約:
- `SHERPA_AGENT` が明示され、選択肢にある値ならそれを使う。選択肢に無い値（未知の名前・有効化して
  いない追加頭脳）は、警告ログを1回だけ出したうえで自動選択（下記）へフォールバックする
  （固定の頭脳へ黙って倒さない）。
- 未指定なら「codex CLI があり、かつ使える認証がある」→codex（CLI の有無だけでは、オフラインキット
  同梱の CLI がキーも認証も無いホストで必ず選ばれてしまうため）。使える認証＝解決済みの中央 OpenAI
  キー（`sherpa.keys.resolve_api_key`）または `~/.codex/auth.json`（`CODEX_HOME` を尊重）の存在。
  条件を満たさなければ次点: 中央 OpenAI キー（実キー）あり→openai／どちらも無し→ollama。
- `_select_provider` は agent=codex で CLI が無いとき `_UnwiredProvider` を返す（ごまかさない）。
"""
from __future__ import annotations

import logging
import shutil

import pytest

from sherpa import agent_constructs as AC
from sherpa import providers

pytestmark = pytest.mark.unit


@pytest.fixture
def _clean_env(monkeypatch, tmp_path):
    monkeypatch.delenv("SHERPA_AGENT", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv(AC.EXTRA_AGENTS_ENV, raising=False)
    # 実機の `~/.codex/auth.json` を読み込んでしまわないよう、既定では auth.json の無い一時
    # ディレクトリへ隔離する（CLAUDE.md: 実 auth.json は読むだけ・変更しない＝テストは専用の
    # 一時ディレクトリを使う）。auth.json の存在を試したいテストは自分でここへ書き込む。
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "codex_home_empty"))
    # 未知値の警告はプロセス内1回だけに絞ってある（`default_agent` 参照）。他テストが先に同じ
    # 不正値を使っていても本テストが独立して警告の有無を検証できるよう、毎回リセットする。
    monkeypatch.setattr(AC, "_warned_unknown_agent", set())


def _which(mapping: dict[str, str | None]):
    return lambda name, *a, **k: mapping.get(name)


def test_auto_default_prefers_codex_when_cli_present(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    assert AC.default_agent() == "codex"


def test_auto_default_falls_to_openai_when_no_cli_but_key(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    assert AC.default_agent() == "openai"


def test_auto_default_falls_to_ollama_when_nothing(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({}))
    assert AC.default_agent() == "ollama"


# ===== RV HIGH（2026-08-18 Codex RV 2巡目 指摘2）: CLI の有無だけでなく「使える認証」も条件にする =====

def test_auto_default_skips_codex_when_cli_present_but_no_auth_at_all(_clean_env, monkeypatch):
    """オフラインキットが Codex CLI を同梱するようになったため、CLI はあるがキーも auth.json も無い
    ホスト（＝OPENAI_API_KEY 未設定で `codex login` もしていない）では codex を選ばず、次点（この
    ケースは実キーも無いので ollama）へ落ちること。"""
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    assert AC.default_agent() == "ollama"


def test_auto_default_rejects_placeholder_key_as_codex_auth(_clean_env, monkeypatch):
    """`sk-REPLACE_ME`（`.env.example` のプレースホルダ）は「使える認証」に数えない
    （auth.json も無ければ codex を選ばない）。"""
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-REPLACE_ME"})
    assert AC.default_agent() == "ollama"


def test_auto_default_selects_codex_via_auth_json_without_any_api_key(_clean_env, monkeypatch, tmp_path):
    """`codex login`（サブスクリプション方式）で `~/.codex/auth.json` があれば、OPENAI_API_KEY が
    無くても codex を選ぶこと（auth_mode は問わない・中身は見ない＝存在だけを見る）。`CODEX_HOME` を
    尊重すること（実 `~/.codex/auth.json` には一切触れない）。"""
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    codex_home = tmp_path / "codex_home_with_auth"
    codex_home.mkdir()
    (codex_home / "auth.json").write_text('{"tokens": {}}', encoding="utf-8")
    monkeypatch.setenv("CODEX_HOME", str(codex_home))
    assert AC.default_agent() == "codex"


def test_explicit_env_wins_over_autoselect(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setenv("SHERPA_AGENT", "codex")
    assert AC.default_agent() == "codex"        # 明示は尊重（CLI の有無で黙って変えない）


# ===== 未知/選べない SHERPA_AGENT は固定の頭脳へ黙って倒さず、自動選択へフォールバックする =====

def test_unknown_explicit_agent_falls_back_to_auto_select_codex(_clean_env, monkeypatch):
    """typo 等で選べない値でも、固定 `DEFAULT_AGENT`（codex）に無条件で倒れるのではなく、その時点の
    環境から自動選択する（この場合は CLI・認証ともに揃っているので codex が選ばれる＝固定値と
    結果が同じでも「経路」が違うことを次のケースで確認する）。"""
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    monkeypatch.setenv("SHERPA_AGENT", "opneai")            # typo（openai のつもり）
    assert AC.default_agent() == "codex"


def test_unknown_explicit_agent_falls_back_to_auto_select_openai(_clean_env, monkeypatch):
    """自動選択の結果が固定 `DEFAULT_AGENT`（codex）と異なるケース＝本当に自動選択経路を
    通っていることの証拠（CLI 無し・実キー有りなら openai が選ばれる）。"""
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    monkeypatch.setenv("SHERPA_AGENT", "bogus-agent-name")
    assert AC.default_agent() == "openai"


def test_unknown_explicit_agent_falls_back_to_auto_select_ollama(_clean_env, monkeypatch):
    """CLI 無し・実キーも無ければ、自動選択の最終フォールバックである ollama になる。"""
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setenv("SHERPA_AGENT", "bogus-agent-name")
    assert AC.default_agent() == "ollama"


def test_known_but_disabled_extra_agent_falls_back_to_auto_not_fixed_default(_clean_env, monkeypatch):
    """`gemini` は実在の頭脳名だが `SHERPA_EXTRA_AGENTS` で有効化していなければ選べない。
    この「既知だが選べない」ケースも、typo と同じく自動選択へフォールバックする
    （固定 `DEFAULT_AGENT`＝codex へは倒れない）。"""
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setenv("SHERPA_AGENT", "gemini")
    assert AC.default_agent() == "ollama"


def test_unknown_explicit_agent_logs_warning_with_the_bad_value(_clean_env, monkeypatch, caplog):
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setenv("SHERPA_AGENT", "bogus-agent-name")
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        AC.default_agent()
    assert any("bogus-agent-name" in r.getMessage() for r in caplog.records)


def test_unknown_explicit_agent_warns_only_once_per_value(_clean_env, monkeypatch, caplog):
    """同じ不正値で `default_agent()` を繰り返し呼んでも警告はプロセス内1回だけ（毎リクエストの
    警告洪水を防ぐ）。フォールバック挙動（自動選択の結果を返す）自体は毎回変わらない。"""
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setenv("SHERPA_AGENT", "bogus-agent-name")
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        first = AC.default_agent()
        second = AC.default_agent()
    assert first == second == "ollama"
    warnings = [r for r in caplog.records if "bogus-agent-name" in r.getMessage()]
    assert len(warnings) == 1


def test_explicit_agent_is_case_insensitive(_clean_env, monkeypatch):
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    monkeypatch.setenv("SHERPA_AGENT", "CoDeX")
    assert AC.default_agent() == "codex"


def test_explicit_agent_strips_surrounding_whitespace(_clean_env, monkeypatch, caplog):
    monkeypatch.setattr(shutil, "which", _which({}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    monkeypatch.setenv("SHERPA_AGENT", "  openai  ")
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        assert AC.default_agent() == "openai"
    assert not any("SHERPA_AGENT" in r.getMessage() for r in caplog.records), \
        "前後の空白を取り除けば有効な値のはずなのに、未知値として警告している"


def test_select_provider_unknown_explicit_sherpa_agent_is_unwired_not_auto_selected(_clean_env, monkeypatch):
    """`_select_provider`（実行の唯一の入口）は `default_agent()` と異なり、`SHERPA_AGENT` の
    非空の不正値を黙って自動選択へ倒さない＝honest failure（`_UnwiredProvider`）を返す
    （黙ったプロバイダ切替の是正）。"""
    monkeypatch.setattr(shutil, "which", _which({"codex": "/opt/tools/codex/bin/codex"}))
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {"openai_api_key": "sk-x"})
    monkeypatch.setenv("SHERPA_AGENT", "bogus-agent-name")
    p = providers._select_provider({})
    assert isinstance(p, providers._UnwiredProvider)
    assert "bogus-agent-name" in p.howto


def test_select_provider_invalid_cloud_provider_is_unwired_not_downgraded(_clean_env, monkeypatch):
    """A7（クラウドプロバイダ排他選択）: 保存済み `agent=bedrock` で `cloud_provider`（A7 の選択
    設定）が非空の不正値（env 誤記・旧データ等）のとき、`_select_provider` は黙って ollama へ
    倒さず honest failure（`_UnwiredProvider`）を返す（黙ったプロバイダ切替の是正）。"""
    monkeypatch.setenv(AC.EXTRA_AGENTS_ENV, "bedrock")
    monkeypatch.setattr("sherpa.store.get_system_settings",
                        lambda: {"cloud_provider": "not-a-real-provider", "bedrock_api_key": "dummy"})
    p = providers._select_provider({"agent": "bedrock"})
    assert isinstance(p, providers._UnwiredProvider)
    assert "not-a-real-provider" in p.howto


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


def test_disabled_provider_run_includes_scope_in_env(monkeypatch):
    """`_DisabledProvider` も `_UnwiredProvider` と同じ honest failure 経路＝scope を含める。"""
    from sherpa.providers.base import Ctx
    from sherpa.providers import _DisabledProvider

    p = _DisabledProvider("gemini")
    narrow_scope = {"world": "v1", "scope_paths": ["4期/"], "source": "scope"}
    ctx = Ctx(message="質問", world="v1", knowledge=True, scope_meta=narrow_scope,
             route=lambda m: {"lens": "qa", "reason": "t", "input": m},
             dispatch=lambda l, i: {"summary": {"total": 0}, "data": {}, "sources": []},
             make_sources=lambda docs: [{"doc_id": d} for d in docs])
    result = next(e for e in p.run(ctx) if e.get("type") == "_result")
    assert result["env"]["scope"] == {**narrow_scope, "layer_applied": True}


def test_unwired_and_disabled_provider_preserve_requested_layer_value():
    """layer_applied だけでなく、要求された layer の値自体も欠落させない
    （UI が「探す対象」の直前選択を再表示できるように）。"""
    from sherpa.providers.base import Ctx
    from sherpa.providers import _DisabledProvider, _UnwiredProvider

    scope_meta = {"world": "v1", "scope_paths": [], "source": "all", "layer": "code"}
    for p in (_UnwiredProvider("Codex", "案内文"), _DisabledProvider("gemini")):
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
    p = providers._select_provider({"agent": "codex"})
    assert isinstance(p, _FakeCodex)
