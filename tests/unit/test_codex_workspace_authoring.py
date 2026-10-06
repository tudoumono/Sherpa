"""Codex の個人書込（authoring）・サンドボックス設定・AGENTS.md・出典収集・サイドカーの契約。

実 codex CLI は起動しない（偽 codex スクリプト／ソース検査）。
"""
from __future__ import annotations

import dataclasses
import inspect
import json
import logging
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import threading
import time
import tomllib
from unittest.mock import patch

import pytest

from sherpa import agents as A
from sherpa import codex_agents_md
from sherpa import providers as providers_pkg
from sherpa.providers import base as BASE
from sherpa.providers.codex import process as PROC
from sherpa.providers.codex import sandbox as SB


# 互換モード＋fixtures はテスト実行中だけ有効化する（モジュールレベルの os.environ 直書きは
# 一括 collection 時にプロセス全体へ漏れ、後続テストの認証を無効化する）。
@pytest.fixture(autouse=True)
def _unit_compat_env(monkeypatch):
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


@pytest.fixture(autouse=True)
def _codex_cli_present(monkeypatch):
    """`_select_provider` の codex 分岐が見る `shutil.which("codex")` を「ある」に固定する。"""
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/codex" if name == "codex" else None)


def _cfg(tmp_path, kb_roots=("/kb",), mcp=True, name="ch", **kw) -> str:
    """config.toml を生成してその本文を返す（同一テスト内の複数生成は name で分ける）。"""
    ch = tmp_path / name
    SB._write_codex_authoring_config(ch, list(kb_roots), "low", mcp, "test", None, **kw)
    return (ch / "config.toml").read_text()


def _agents_md(tmp_path, name="authoring", **kw) -> str:
    d = tmp_path / name
    d.mkdir(exist_ok=True)
    codex_agents_md.write_agents_md(d, **kw)
    return (d / "AGENTS.md").read_text(encoding="utf-8")


def test_codex_sandbox_flag_default_on_and_off(monkeypatch):
    import importlib
    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    importlib.reload(A)
    assert A._codex_sandbox_enabled() is True
    monkeypatch.setenv("SHERPA_CODEX_SANDBOX", "0")
    importlib.reload(A)
    assert A._codex_sandbox_enabled() is False
    monkeypatch.delenv("SHERPA_CODEX_SANDBOX", raising=False)
    importlib.reload(A)


# ===== config.toml（permission profile） =====

def test_codex_profile_config_confines_reads(tmp_path):
    cfg = _cfg(tmp_path, ["/kb/abs/path"], mcp=False)
    assert 'default_permissions = "sherpa-authoring"' in cfg
    assert '":root" = "deny"' in cfg
    assert '"/kb/abs/path" = "read"' in cfg
    assert '"." = "write"' in cfg
    assert "enabled = false" in cfg
    tomllib.loads(cfg)


def test_codex_clean_env_has_no_secrets_and_passes_proxy_only_when_set(monkeypatch, tmp_path):
    for k in SB._CODEX_PASSTHROUGH_ENV:
        monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("NEO4J_PASSWORD", "should_not_leak")
    monkeypatch.setenv("OPENAI_API_KEY", "sk-should-not-leak")
    env = SB._codex_clean_env(tmp_path / "ch", tmp_path / "tmp")
    assert not any("NEO4J" in k or "PASSWORD" in k for k in env)
    assert "PATH" in env and "CODEX_HOME" in env
    assert "SHERPA_MCP_LEDGER_DIR" not in env
    assert "OPENAI_API_KEY" not in env
    assert not any(k in env for k in SB._CODEX_PASSTHROUGH_ENV)   # 未設定なのに透過しない

    monkeypatch.setenv("HTTPS_PROXY", "http://proxy.internal:8080")
    monkeypatch.setenv("no_proxy", "localhost,127.0.0.1")
    monkeypatch.setenv("SSL_CERT_FILE", "/etc/ssl/certs/corp.pem")
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "/etc/ssl/certs/corp.pem")
    monkeypatch.setenv("HTTP_PROXY", "")                    # 空文字は未設定扱い
    env = SB._codex_clean_env(tmp_path / "ch", tmp_path / "tmp")
    assert env["HTTPS_PROXY"] == "http://proxy.internal:8080"
    assert env["no_proxy"] == "localhost,127.0.0.1"
    assert env["SSL_CERT_FILE"] == env["NODE_EXTRA_CA_CERTS"] == "/etc/ssl/certs/corp.pem"
    assert "HTTP_PROXY" not in env and "ALL_PROXY" not in env
    assert "OPENAI_API_KEY" not in env and "NEO4J_PASSWORD" not in env


def test_venv_root_and_clean_env_path_prefix(monkeypatch, tmp_path):
    monkeypatch.setattr(sys, "prefix", "/fake/venv")
    monkeypatch.setattr(sys, "base_prefix", "/usr")
    assert SB._venv_root() == pathlib.Path("/fake/venv")
    monkeypatch.setattr(sys, "prefix", "/usr")
    assert SB._venv_root() is None

    monkeypatch.setattr(SB, "_venv_root", lambda: pathlib.Path("/fake/venv"))
    env = SB._codex_clean_env(tmp_path / "ch", tmp_path / "tmp")
    assert env["PATH"].startswith("/fake/venv/bin:")
    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    env2 = SB._codex_clean_env(tmp_path / "ch2", tmp_path / "tmp2")
    assert "/fake/venv" not in env2["PATH"]


# ===== MCP ツール結果予算・hits/window の env =====

def test_resolve_mcp_budget_ceiling_and_plain_dict():
    """1 件あたりの予算は天井 64KiB に潰れ、管理画面の小さい値はそのまま効く。"""
    from sherpa.providers.codex.usage import _CODEX_MCP_TOOL_BUDGET_CEILING_BYTES, _resolve_mcp_budget
    assert _CODEX_MCP_TOOL_BUDGET_CEILING_BYTES == 64 * 1024
    assert _resolve_mcp_budget({})["SHERPA_MCP_TOOL_BUDGET_BYTES"] == str(64 * 1024)
    huge = _resolve_mcp_budget({"agentic_budget_per_result": 8 * 1024 * 1024})
    assert huge["SHERPA_MCP_TOOL_BUDGET_BYTES"] == str(64 * 1024)
    small = _resolve_mcp_budget({"agentic_budget_per_result": 16 * 1024})
    assert small["SHERPA_MCP_TOOL_BUDGET_BYTES"] == str(16 * 1024)
    result = _resolve_mcp_budget({})
    assert isinstance(result, dict)
    assert set(result) == {"SHERPA_MCP_TOOL_BUDGET_BYTES", "SHERPA_MCP_TOOL_MAX_HITS", "SHERPA_MCP_TOOL_WINDOW_CAP"}


def test_resolve_mcp_budget_hits_and_window():
    """標準は env 既定、「深く」は ×1.5、管理画面の基準値編集が API 経路と同じ優先順位で効く。"""
    from sherpa import agentic_search
    from sherpa.providers.codex.usage import _resolve_mcp_budget
    env = _resolve_mcp_budget({})
    assert env["SHERPA_MCP_TOOL_MAX_HITS"] == str(agentic_search.MAX_HITS)
    assert env["SHERPA_MCP_TOOL_WINDOW_CAP"] == str(agentic_search.READ_WINDOW)
    deep = _resolve_mcp_budget({}, "deep")
    assert deep["SHERPA_MCP_TOOL_MAX_HITS"] == str(int(agentic_search.MAX_HITS * 1.5))
    assert deep["SHERPA_MCP_TOOL_WINDOW_CAP"] == str(int(agentic_search.READ_WINDOW * 1.5))
    admin = _resolve_mcp_budget({"depth_base_grep_max_hits": 50, "depth_base_read_window": 60})
    assert admin["SHERPA_MCP_TOOL_MAX_HITS"] == "50" and admin["SHERPA_MCP_TOOL_WINDOW_CAP"] == "60"


# ===== config.toml の各種項目 =====

def test_config_forwards_mcp_env_items(tmp_path):
    """extra_mcp_env・sidecar_path・layer は MCP サーバの env として書かれ、省略時や mcp=False では出ない。"""
    cfg = _cfg(tmp_path, extra_mcp_env={"SHERPA_MCP_TOOL_BUDGET_BYTES": "65536"}, name="a")
    assert "SHERPA_MCP_TOOL_BUDGET_BYTES" in cfg and "65536" in cfg
    assert "SHERPA_MCP_SIDECAR" not in cfg and "SHERPA_MCP_LAYER" not in _cfg(tmp_path, name="b")
    assert "/tmp/run-x/.mcp_sidecar.jsonl" in _cfg(tmp_path, sidecar_path="/tmp/run-x/.mcp_sidecar.jsonl", name="c")
    assert 'SHERPA_MCP_LAYER = "code"' in _cfg(tmp_path, layer="code", name="d")
    off = _cfg(tmp_path, mcp=False, sidecar_path="/tmp/run-x/.mcp_sidecar.jsonl", name="e")
    assert "mcp_servers" not in off and "SHERPA_MCP_SIDECAR" not in off


def test_config_mcp_creds_in_config_file_not_cmdline(tmp_path, monkeypatch):
    monkeypatch.setenv("NEO4J_PASSWORD", "creds_here")
    cfg = _cfg(tmp_path, ["/kb"], name="ch")
    assert "[mcp_servers.sherpa]" in cfg
    assert "creds_here" in cfg
    assert "PYTHONPATH" in cfg


def test_codex_home_and_config_perms_and_fail_closed_on_existing(tmp_path):
    """CODEX_HOME は 0700・config.toml は 0600。既存 config.toml があれば raise（古い config での起動を防ぐ）。"""
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, ["/kb"], "low", True, "t", None)
    assert stat.S_IMODE(ch.stat().st_mode) == 0o700
    assert stat.S_IMODE((ch / "config.toml").stat().st_mode) == 0o600
    with pytest.raises(FileExistsError):
        SB._write_codex_authoring_config(ch, ["/kb"], "low", True, "t", None)


@pytest.mark.parametrize("root,layer,mcp", [
    ("/kb/world-root", None, True), ("/kb/world-root", "both", True),
    ("/kb/world-root", "code", True), ("/kb/world-root", "docs", True),
    ("/kb/world-root", "code", False),
    ("/usr/share/sherpa-kb/some-world", "code", True),   # `:minimal` 配下でも deny されない
])
def test_config_keeps_kb_root_read_regardless_of_layer(tmp_path, root, layer, mcp):
    """層（探す対象）は Codex に強制しない: 層限定でも MCP 有無でも KB ルートは read のまま（層フィルタは MCP 側が担う）。"""
    cfg = _cfg(tmp_path, [root], mcp=mcp, **({"layer": layer} if layer else {}))
    assert f'"{root}" = "read"' in cfg
    assert f'"{root}" = "deny"' not in cfg
    assert ("[mcp_servers.sherpa]" in cfg) is mcp


def test_config_direct_read_roots_override_and_empty_denies(tmp_path, monkeypatch):
    sub = tmp_path / "scope" / "subtree"
    sub.mkdir(parents=True)
    secret = sub / ".env"
    secret.write_text("x")
    cfg = _cfg(tmp_path, ["/kb/should-not-appear"], mcp=False, name="o",
               direct_read_roots=[str(sub), "/derived/md/subtree"], sensitive_deny=[str(secret)])
    assert f'"{sub}" = "read"' in cfg and '"/derived/md/subtree" = "read"' in cfg
    assert '"/kb/should-not-appear" = "read"' not in cfg
    assert f'"{secret}" = "deny"' in cfg
    assert cfg.index(f'"{sub}" = "read"') < cfg.index(f'"{secret}" = "deny"')   # 秘匿 deny は read 行より後

    # direct_read_roots=[]（直読不許可・fail-closed）: KB root・派生 root・venv を read せず明示 deny
    kb, md, venv = tmp_path / "kb", tmp_path / "md", tmp_path / "venv"
    for d in (kb, md, venv):
        d.mkdir()
    monkeypatch.setattr(SB, "_venv_root", lambda: venv)
    cfg2 = _cfg(tmp_path, [str(kb)], mcp=False, name="e", direct_read_roots=[], deny_roots=[str(md)])
    assert f'"{kb}" = "read"' not in cfg2 and f'"{kb}" = "deny"' in cfg2
    assert f'"{md}" = "deny"' in cfg2
    assert f'"{venv}" = "read"' not in cfg2 and f'"{venv}" = "deny"' in cfg2
    # 不在の root への deny 行は書かない（起動失敗）
    cfg3 = _cfg(tmp_path, ["/kb"], mcp=False, name="e2", direct_read_roots=[])
    assert '"/kb" = "deny"' not in cfg3
    # 派生 root が KB root 配下: deny 済みフォルダ配下の deny 行は書かない
    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    kb2 = tmp_path / "kb2"
    (kb2 / "derived" / "md").mkdir(parents=True)
    other = tmp_path / "other"
    other.mkdir()
    cfg4 = _cfg(tmp_path, [str(kb2)], mcp=False, name="e3", direct_read_roots=[],
                deny_roots=[str(kb2 / "derived" / "md"), str(other)])
    assert f'"{kb2}" = "deny"' in cfg4 and f'"{other}" = "deny"' in cfg4
    assert f'"{kb2 / "derived" / "md"}"' not in cfg4


def test_config_adds_venv_read_when_running_in_venv(monkeypatch, tmp_path):
    monkeypatch.setattr(SB, "_venv_root", lambda: pathlib.Path("/fake/venv"))
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, ["/kb"], "low", False, "test", None)
    assert '"/fake/venv" = "read"' in (ch / "config.toml").read_text()
    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    ch2 = tmp_path / "ch2"
    SB._write_codex_authoring_config(ch2, ["/kb"], "low", False, "test", None)
    assert "/fake/venv" not in (ch2 / "config.toml").read_text()


def test_config_root_denied_by_scope_has_no_read_line(tmp_path, monkeypatch):
    """範囲が KB にだけある（派生 root は root ごと deny）とき、その root の read 行を書かない（同一キー重複で TOML が壊れる）。"""
    kb, md = tmp_path / "kb", tmp_path / "md"
    (kb / "A").mkdir(parents=True)
    (kb / "B").mkdir()
    md.mkdir()
    monkeypatch.setattr(SB, "_venv_root", lambda: None)
    deny = SB._scope_deny_entries([str(kb), str(md)], ["A"])
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, [str(kb)], "low", False, "test", None,
                                     direct_read_roots=[str(kb), str(md)], sensitive_deny=deny)
    cfg = (ch / "config.toml").read_text()
    tomllib.loads(cfg)
    assert f'"{md.resolve()}" = "deny"' in cfg and f'"{md.resolve()}" = "read"' not in cfg
    assert f'"{kb.resolve()}" = "read"' in cfg


# ===== multi_agent の config・AGENTS.md =====

def test_config_writes_agents_sections_when_multi_agent(tmp_path):
    off = _cfg(tmp_path, name="off")
    assert "[agents]" not in off and "[agents.worker]" not in off

    ch = tmp_path / "on"
    SB._write_codex_authoring_config(ch, ["/kb"], "low", True, "test", None,
                                    multi_agent=True, orchestrator_model="gpt-5.5")
    cfg = (ch / "config.toml").read_text()
    assert "[agents]" in cfg and "[agents.worker]" in cfg and "[agents.evaluator]" in cfg
    assert f'default_subagent_model = "{SB._CODEX_WORKER_MODEL_FALLBACK}"' in cfg
    assert "max_concurrent_threads_per_session" in cfg
    worker_path, evaluator_path = ch / "agents" / "worker.toml", ch / "agents" / "evaluator.toml"
    assert f'config_file = "{worker_path}"' in cfg and f'config_file = "{evaluator_path}"' in cfg
    assert worker_path.is_file() and evaluator_path.is_file()
    assert ch.resolve() in worker_path.resolve().parents   # 子 config は codex_home 配下（model-shell から不可視）
    worker_toml = worker_path.read_text()
    assert f'model = "{SB._CODEX_WORKER_MODEL_FALLBACK}"' in worker_toml
    assert 'model_reasoning_effort = "medium"' in worker_toml
    evaluator_toml = evaluator_path.read_text()
    assert 'model = "gpt-5.5"' in evaluator_toml and 'model_reasoning_effort = "low"' in evaluator_toml
    parsed = tomllib.loads(cfg)
    assert parsed["agents"]["default_subagent_model"] == SB._CODEX_WORKER_MODEL_FALLBACK
    assert parsed["agents"]["default_subagent_reasoning_effort"] == "medium"
    assert parsed["agents"]["worker"]["config_file"] == str(worker_path)
    assert parsed["agents"]["evaluator"]["config_file"] == str(evaluator_path)

    # orchestrator_model 省略: evaluator は worker と同じモデル
    ch2 = tmp_path / "no-orch"
    SB._write_codex_authoring_config(ch2, ["/kb"], "low", True, "test", None, multi_agent=True)
    assert f'model = "{SB._CODEX_WORKER_MODEL_FALLBACK}"' in (ch2 / "agents" / "evaluator.toml").read_text()


def test_codex_worker_model_configurable_via_system_settings(tmp_path):
    fb = SB._CODEX_WORKER_MODEL_FALLBACK
    assert SB._codex_worker_model(None) == fb and SB._codex_worker_model({}) == fb
    assert SB._codex_worker_model({"codex_worker_model": "  "}) == fb
    assert SB._codex_worker_model({"codex_worker_model": "gpt-5.9-custom"}) == "gpt-5.9-custom"
    ch = tmp_path / "ch"
    SB._write_codex_authoring_config(ch, ["/kb"], "low", True, "test", None, multi_agent=True,
                                    system_settings={"codex_worker_model": "gpt-5.9-custom"})
    assert 'default_subagent_model = "gpt-5.9-custom"' in (ch / "config.toml").read_text()
    assert 'model = "gpt-5.9-custom"' in (ch / "agents" / "worker.toml").read_text()


def test_agents_md_multi_agent_rounds_and_worker_usage(tmp_path):
    assert "spawn_agent(worker)" not in _agents_md(tmp_path, "off")
    std = _agents_md(tmp_path, "std", multi_agent=True, review_rounds=0)
    assert "spawn_agent(worker)" in std and "0 回＝evaluator は使わない" in std
    assert "spawn_agent(evaluator) は" not in std
    assert "見直しの回数は 2 回まで" in _agents_md(tmp_path, "deep", multi_agent=True, review_rounds=2)
    assert "0 回＝evaluator は使わない" not in _agents_md(tmp_path, "deep2", multi_agent=True, review_rounds=2)
    assert "見直しの回数は 7 回まで" in _agents_md(tmp_path, "max", multi_agent=True, review_rounds=7)
    # worker は観点ごとに必ず使う・同時起動数の上限・観点の分け方だけが委ねられる（クイックでも同じ）
    for rounds in (0, 2):
        txt = _agents_md(tmp_path, f"mand-{rounds}", multi_agent=True, review_rounds=rounds)
        assert "観点ごとに spawn_agent(worker) を必ず使う" in txt
        assert f"同時に {SB._CODEX_MAX_CONCURRENT_SUBAGENTS} 体まで起動してよい" in txt
        assert "worker には観点を1つだけ渡し" in txt
        assert "観点の分け方はあなた自身の" in txt and "判断でよい" in txt
        assert "何体をどう使うか（観点の分け方・worker の数）はあなた自身の判断でよい" not in txt
    assert "観点ごとに spawn_agent(worker) を必ず使う" in _agents_md(
        tmp_path, "docs-only", direct_read=False, multi_agent=True, review_rounds=2, layer="docs")


def test_agents_md_two_stage_coverage_and_escalation(tmp_path):
    txt = _agents_md(tmp_path, "cov")
    for kw in ("「ごと」", "「各」", "「それぞれ」", "「漏れなく」", "「全件」"):
        assert kw in txt
    assert "親子二段の網羅要求" in txt
    assert "まず親項目 X の集合を確定し、件数を明示する" in txt
    assert "欠けている親があれば具体的に明示する" in txt

    on = _agents_md(tmp_path, "esc-on", multi_agent=True, review_rounds=2, review_rounds_escalation=True)
    assert "もう 1 回だけ追加してよい" in on and "合計 3 回まで" in on
    for name, kw in (("esc-off", dict(review_rounds=2, review_rounds_escalation=False)),
                     ("esc-quick", dict(review_rounds=0, review_rounds_escalation=True))):
        assert "もう 1 回だけ追加してよい" not in _agents_md(tmp_path, name, multi_agent=True, **kw)


@pytest.mark.parametrize("rounds", [0, 2, 7])
def test_agents_md_source_confirmation_required_at_every_depth(tmp_path, rounds):
    txt = _agents_md(tmp_path, f"d{rounds}", multi_agent=True, review_rounds=rounds)
    assert "自分（本体）が" in txt
    assert "グラフ" in txt and "ripgrep" in txt and "直接読" in txt
    assert "根拠の**件数**では判定しない" in txt
    flat = txt.replace("\n  ", "")
    assert "設計書と実装（ソース）の" in flat and "『食い違い』と書く" in flat
    assert "実装に関する主張は必ず自分でソース" not in txt
    if rounds <= 0:
        assert "根拠として示された箇所（ファイル:行）を自分で開いて突き合わせる" in txt


def test_source_verification_paragraph_switches_wording_by_direct_read():
    direct = codex_agents_md._source_verification_paragraph(True)
    mcp_only = codex_agents_md._source_verification_paragraph(False)
    assert "直接開いて" in direct and "直接読んで確認する" in direct
    assert "MCP の読取ツール" in mcp_only and "直接" not in mcp_only
    for txt in (direct, mcp_only):
        assert "worker" in txt and "ファイル:行" in txt
        assert "根拠として示された箇所" in txt and "突き合わせる" in txt
        assert "全文を読み直す必要はなく" in txt
        assert "根拠（ファイル:行）が示されていない主張は事実と断定せず" in txt and "未確認" in txt
        assert "採らない" not in txt and "捨てる" not in txt
        assert "グラフ検索・全文検索（ES）が空・不調・未構築のときは、それを理由に回答を止めない" in txt


def test_agents_md_direct_read_false_avoids_direct_read_wording(tmp_path):
    txt = _agents_md(tmp_path, direct_read=False, multi_agent=True, review_rounds=2)
    for banned in ("直接開いて", "直接読んで確認する", "直接読む", "investigate-*"):
        assert banned not in txt
    assert "MCP の読取ツール" in txt
    assert "spawn_agent(worker)" in txt


@pytest.mark.parametrize("direct_read,layer", [
    (True, None), (True, "docs"), (True, "code"), (False, None), (False, "code"), (False, "both")])
def test_agents_md_requires_source_except_docs_only(tmp_path, direct_read, layer):
    txt = _agents_md(tmp_path, direct_read=direct_read, multi_agent=True, review_rounds=2, layer=layer)
    assert "ソースを確認していないため確定できません" not in txt
    assert "根拠の**件数**では判定しない" in txt
    assert "グラフ検索・全文検索（ES）が空・不調・未構築のときは、それを理由に回答を止めない" in txt


def test_agents_md_docs_only_special_case(tmp_path):
    """直読不可かつ資料のみ（layer=docs）だけは、ソース確認を要求せず確定不可の告知＋部分回答を指示する。"""
    txt = _agents_md(tmp_path, "d2", direct_read=False, multi_agent=True, review_rounds=2, layer="docs")
    assert "ソースを確認していないため確定できません" in txt
    assert "今回の探す対象は資料のみ" in txt and "部分回答" in txt
    assert "実装に関する主張は、自分（本体）が" not in txt
    assert "根拠の**件数**では判定しない" in txt
    std = _agents_md(tmp_path, "d3", direct_read=False, multi_agent=True, review_rounds=0, layer="docs")
    assert "ソースを確認していないため確定できません" in std
    assert "0 回＝evaluator は使わない" in std
    assert "必ず自分でソースを確認" not in std and "実装に関する主張は、自分（本体）が" not in std


_COMBOS = [(dr, layer, ma, rr) for dr in (True, False) for layer in (None, "docs", "code", "both")
           for ma in (False, True) for rr in ((0, 2) if ma else (0,))]


@pytest.mark.parametrize("direct_read,layer,multi_agent,review_rounds", _COMBOS)
def test_agents_md_discard_wording_removed_and_classification_present(
        tmp_path, direct_read, layer, multi_agent, review_rounds):
    """全組合せで「採らない」「捨てる」「だけを採る」「採否」が出ず、4 分類の語が出る。"""
    txt = _agents_md(tmp_path, direct_read=direct_read, layer=layer,
                     multi_agent=multi_agent, review_rounds=review_rounds)
    for banned in ("採らない", "捨てる", "だけを採る", "採否"):
        assert banned not in txt
    for term in ("ソース確認済み", "設計書のみ", "設計書とソースが不一致", "未確認"):
        assert term in txt


_LEDGER_REQUIRED = (
    "ledger_manifest_set", "ledger_item_put", "ledger_status", "ファイルを直接書かない", "`problems` を読み",
    "pending", "source_confirmed", "not_found_in_scope", "台帳に無い新しい主張", "最初からやり直さず")
_LEDGER_BANNED = ("items/<id>.json", "`manifest.json` は親だけが書く", "manifest.json を書く",
                  "一時ファイルに書いてから置換", "manifest.json が既に存在するか")


@pytest.mark.parametrize("direct_read,layer,multi_agent,review_rounds", _COMBOS)
def test_agents_md_ledger_paragraph_present_in_all_schema_v2_combos(
        tmp_path, direct_read, layer, multi_agent, review_rounds):
    txt = _agents_md(tmp_path, direct_read=direct_read, layer=layer, multi_agent=multi_agent,
                     review_rounds=review_rounds, output_schema=True, output_schema_v2=True)
    for term in _LEDGER_REQUIRED:
        assert term in txt, term
    for term in _LEDGER_BANNED:
        assert term not in txt, term


@pytest.mark.parametrize("output_schema,output_schema_v2", [(False, False), (False, True), (True, False)])
@pytest.mark.parametrize("multi_agent", [False, True])
def test_agents_md_ledger_paragraph_absent_without_schema_v2(tmp_path, output_schema, output_schema_v2, multi_agent):
    txt = _agents_md(tmp_path, output_schema=output_schema, output_schema_v2=output_schema_v2,
                     multi_agent=multi_agent, review_rounds=2 if multi_agent else 0)
    for banned in ("manifest.json", ".tmp/investigation/", "台帳に無い新しい主張",
                   "ledger_manifest_set", "ledger_item_put", "ledger_status"):
        assert banned not in txt


def test_agents_md_ledger_docs_only_population_wording(tmp_path):
    """docs_only は母集団・初期状態・列挙元をすべて設計書側に書き分け、ソース側の文言を残さない。
    それ以外は設計書と実装の両方から確定する。source_required は渡したときだけ必須文が出る。"""
    docs_only = _agents_md(tmp_path, "do", direct_read=False, layer="docs", output_schema=True, output_schema_v2=True)
    assert "設計書から確定" in docs_only
    for banned in ("母集団をソースから", "`pending` で `ledger_item_put`", "ソースから発見", "ソースから確定"):
        assert banned not in docs_only
    assert "設計書から発見した項目" in docs_only
    assert "`spec_only` で `ledger_item_put` に登録" in docs_only
    for direct_read, layer in ((True, None), (True, "docs"), (False, None), (False, "code")):
        txt = _agents_md(tmp_path, f"s-{direct_read}-{layer}", direct_read=direct_read, layer=layer,
                         output_schema=True, output_schema_v2=True)
        assert "母集団を設計書と実装（ソース）の" in txt
        assert "`pending` で `ledger_item_put` に登録" in txt
        assert "ソースから発見" in txt
    need = "required_checks` に `source` を必ず含める"
    assert need not in _agents_md(tmp_path, "nsr", direct_read=True, layer=None, output_schema=True, output_schema_v2=True)
    assert need in _agents_md(tmp_path, "sr", direct_read=True, layer=None, output_schema=True,
                              output_schema_v2=True, source_required=True)


def test_agents_md_ledger_claim_mapping_scope_and_zero_population(tmp_path):
    txt = _agents_md(tmp_path, "m", output_schema=True, output_schema_v2=True)
    # 証拠を持たない終端（not_found_in_scope/unreadable/unavailable）は unknown 主張へ対応づける
    assert "evidence` を持たない終端" in txt and "`unknown` にし" in txt and "`unavailable`→`insufficient`" in txt
    # 「claims は item に対応」「全 item 終端まで final を返さない」は台帳を作った依頼に限る
    assert "台帳を作った依頼では、最終回答" in txt
    assert "台帳を作らなかった依頼（作成系・単純な質問）ではこの対応関係は適用せず" in txt
    assert "台帳を作らなかった依頼は、この段落の残り" in txt
    for direct_read, layer in ((True, None), (False, "docs")):
        t = _agents_md(tmp_path, f"z-{direct_read}", direct_read=direct_read, layer=layer,
                       output_schema=True, output_schema_v2=True)
        label = f"direct_read={direct_read} layer={layer!r}"
        # 母集団ゼロ件の逃げ道
        assert "母集団がゼロ件のとき" in t and "scope-check" in t, label
        # 復元済みの台帳を初期化しない
        assert "まず親が `ledger_status`" in t, label
        assert "`manifest_invalid` が true かつ `items` が 0" in t, label
        assert "非終端の item から調査を再開" in t and "作り直しをしない" in t, label
        # 差し戻し後に見つけた新しい対象を未登録のまま放置しない
        assert "manifest に追加" in t and "未登録のまま放置しない" in t, label
        assert "見つかった辺は都度 manifest に追加する" in t and "item から調査を再開する" in t, label
        assert "item だけを進める" not in t, label


def test_agents_md_ledger_worker_note_and_mcp_off(tmp_path):
    txt = _agents_md(tmp_path, "w", multi_agent=True, review_rounds=2, output_schema=True, output_schema_v2=True)
    assert "割り当てられた item id" in txt
    assert "worker が使う台帳ツールは `ledger_item_put` だけ" in txt
    assert "`ledger_manifest_set` は親だけが呼ぶ" in txt
    assert "割り当てられた item id" not in _agents_md(tmp_path, "w2", multi_agent=True, review_rounds=2)
    # MCP 無効では台帳段落を出さない（「直接書かない」と「台帳を作る」が両立しない指示になる）
    no_mcp = _agents_md(tmp_path, "nm", output_schema=True, output_schema_v2=True, mcp=False)
    assert "ledger_item_put" not in no_mcp and "調査台帳" not in no_mcp
    assert "ledger_item_put" in _agents_md(tmp_path, "m2", output_schema=True, output_schema_v2=True, mcp=True)


# ===== 範囲（scope）・直読 root・秘匿列挙 =====

def test_scope_deny_entries_denies_siblings_off_the_scope_path(tmp_path):
    """範囲は「経路上にない兄弟の deny」で表す（親の deny が子の read に勝つため）。親子で選んだ scope は親だけが効く。"""
    kb = tmp_path / "kb"
    (kb / "A" / "sub" / "deep").mkdir(parents=True)
    (kb / "A" / "other").mkdir()
    (kb / "B").mkdir()
    (kb / "top.txt").write_text("x")
    (kb / "A" / "sub" / "s.txt").write_text("x")
    deny = SB._scope_deny_entries([str(kb)], ["A/sub"])
    assert set(deny) == {str(kb.resolve() / "B"), str(kb.resolve() / "top.txt"), str(kb.resolve() / "A" / "other")}

    kb2 = tmp_path / "kb2"
    (kb2 / "A" / "sub").mkdir(parents=True)
    (kb2 / "A" / "other").mkdir()
    (kb2 / "B").mkdir()
    assert SB._scope_deny_entries([str(kb2)], ["A", "A/sub"]) == [str(kb2.resolve() / "B")]


def test_scope_deny_entries_denies_root_when_scope_absent_and_skips_symlinks(tmp_path):
    """root 配下に無い scope は root ごと deny。`..`／symlink 脱出は無視。兄弟が symlink なら deny に書かない。"""
    kb = tmp_path / "kb"
    (kb / "A").mkdir(parents=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (kb / "escape").symlink_to(outside)
    (kb / "link_sibling").symlink_to(kb / "A")
    assert SB._scope_deny_entries([str(kb)], ["escape"]) == [str(kb.resolve())]
    assert SB._scope_deny_entries([str(kb)], ["../outside"]) == [str(kb.resolve())]
    assert SB._scope_deny_entries([str(kb)], ["A"]) == []
    assert SB._scope_deny_entries([str(kb)], None) == []


def test_scope_deny_entries_max_entries_raises(tmp_path):
    kb = tmp_path / "kb"
    (kb / "A").mkdir(parents=True)
    for i in range(5):
        (kb / f"f{i}.txt").write_text("x")
    with pytest.raises(RuntimeError, match="scope_enum_failed:max_entries_exceeded"):
        SB._scope_deny_entries([str(kb)], ["A"], max_entries=3)


def test_direct_read_roots_returns_base_roots(monkeypatch, tmp_path):
    from sherpa import worlds as W
    kb = tmp_path / "kb"
    kb.mkdir()
    md = tmp_path / "derived" / "md"
    md.mkdir(parents=True)
    monkeypatch.setattr(W, "_fixtures", lambda: False)
    monkeypatch.setattr(W, "world_dir", lambda w: kb)
    monkeypatch.setattr(W, "derived_md_dir", lambda w: md)
    monkeypatch.setattr(W, "derived_rag_dir", lambda w: tmp_path / "no-rag")
    roots = SB._direct_read_roots("test")
    assert str(kb.resolve()) in roots and str(md.resolve()) in roots
    monkeypatch.setattr(W, "derived_md_dir", lambda w: tmp_path / "no-md")
    assert SB._direct_read_roots("test") == [str(kb.resolve())]


def test_enumerate_sensitive_detects_known_patterns(tmp_path):
    """秘匿名（環境ファイル・鍵・認証情報・証明書）を検出する（判定は `text_kind.is_sensitive` に一本化）。"""
    root = tmp_path / "root"
    root.mkdir()
    (root / ".env").write_text("SECRET=1")
    (root / "id_rsa").write_text("---")
    (root / "sub").mkdir()
    (root / "sub" / "credentials.json").write_text("{}")
    (root / "sub" / "x.pem").write_text("---")
    (root / "normal.txt").write_text("ok")
    names = {pathlib.Path(h).name for h in SB._enumerate_sensitive([str(root)])}
    assert names == {".env", "id_rsa", "credentials.json", "x.pem"}


def test_enumerate_sensitive_symlink_denies_target_inside_roots_only(tmp_path):
    """秘匿名の symlink は symlink 自体を deny せず、実体が root 配下の通常ファイルなら実体を deny・root 外や dangling は書かない。"""
    root = tmp_path / "root"
    root.mkdir()
    inside = root / "notes.txt"
    inside.write_text("secret")
    (root / ".env").symlink_to(inside)
    outside = tmp_path / "real_secret.txt"
    outside.write_text("x")
    (root / "id_rsa").symlink_to(outside)
    (root / "credentials.json").symlink_to(tmp_path / "missing")
    assert SB._enumerate_sensitive([str(root)]) == [str(inside.resolve())]


def test_enumerate_sensitive_dedupes_overlapping_roots_and_recurses_venv_like(tmp_path):
    root = tmp_path / "A"
    (root / "sub").mkdir(parents=True)
    (root / "sub" / ".env").write_text("x")
    assert SB._enumerate_sensitive([str(root), str(root / "sub")]) == [str((root / "sub" / ".env").resolve())]
    venv = tmp_path / "venv"
    (venv / "bin").mkdir(parents=True)
    (venv / ".env").write_text("SECRET")
    (venv / "bin" / ".env").write_text("SECRET-deep")
    assert set(SB._enumerate_sensitive([str(venv)])) == {
        str((venv / ".env").resolve()), str((venv / "bin" / ".env").resolve())}


def test_enumerate_sensitive_does_not_follow_symlinked_dirs(tmp_path):
    root = tmp_path / "root"
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / ".env").write_text("SECRET")
    (root / "link").symlink_to(outside)
    assert SB._enumerate_sensitive([str(root)]) == []


@pytest.mark.parametrize("limit,names", [("max_hits", [f"{i}.pem" for i in range(5)]),
                                        ("max_files", [f"file{i}.txt" for i in range(5)])])
def test_enumerate_sensitive_limit_exceeded_raises(tmp_path, limit, names):
    """秘匿件数・走査ファイル数の上限超過は RuntimeError（fail-closed）。"""
    root = tmp_path / "root"
    root.mkdir()
    for n in names:
        (root / n).write_text("x")
    with pytest.raises(RuntimeError):
        SB._enumerate_sensitive([str(root)], **{limit: 2})


def test_enumerate_sensitive_permission_error_raises(monkeypatch, tmp_path):
    root = tmp_path / "root"
    root.mkdir()

    def _boom_walk(top, *a, **k):
        onerror = k.get("onerror")
        if onerror:
            onerror(PermissionError("no access (test)"))
        return iter(())

    monkeypatch.setattr(SB.os, "walk", _boom_walk)
    with pytest.raises(RuntimeError):
        SB._enumerate_sensitive([str(root)])


def test_prune_deny_paths_drops_symlink_missing_nested_and_outside(tmp_path):
    """deny 行の整形: symlink・不在・deny 済み配下・read root 外は落とし、重複は 1 つ、read root 自体の deny は残す。"""
    root = tmp_path / "kb"
    (root / "other").mkdir(parents=True)
    (root / "other" / "o.txt").write_text("x")
    (root / "a.txt").write_text("x")
    (root / "ln").symlink_to(root / "a.txt")
    outside = tmp_path / "outside"
    outside.mkdir()
    deny = [str(root / "other" / "o.txt"), str(root / "other"), str(root / "a.txt"), str(root / "a.txt"),
            str(root / "ln"), str(root / "missing"), str(outside), str(root)]
    assert SB._prune_deny_paths(deny, [str(root)]) == [str(root.resolve())]
    assert SB._prune_deny_paths(deny[:-1], [str(root)]) == [
        str((root / "a.txt").resolve()), str((root / "other").resolve())]


# ===== 偽 codex の共通部品 =====

_PY = "#!/usr/bin/env python3\n"
_DOC = "4期/02_設計/01_基本設計/税計算仕様書.md"
_DOC2 = "4期/01_標準/消費税法.md"


def _emit(obj) -> str:
    return f"print({json.dumps(json.dumps(obj))})\n"


def _msg(text: str, id: str = "1") -> str:
    return _emit({"type": "item.completed", "item": {"id": id, "type": "agent_message", "text": text}})


def _mcp_call(tool: str, args: dict, id: str = "t1", status: str = "completed", **extra) -> str:
    return _emit({"type": "item.completed", "item": {
        "id": id, "type": "mcp_tool_call", "tool": tool, "status": status, "arguments": args, **extra}})


def _install_codex(tmp_path, monkeypatch, body: str, *, sandbox: str | None = None):
    """偽 codex を PATH 先頭に置き、users dir を tmp に向け、出力スキーマを無効化（平文応答）する。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    exe = bin_dir / "codex"
    exe.write_text(body)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "0")
    if sandbox is not None:
        monkeypatch.setenv("SHERPA_CODEX_SANDBOX", sandbox)


def _ctx(uid: str, make_sources=None, lens: str = "qa", message: str = "消費税率について教えて", **kw):
    return A.Ctx(
        message=message, world="v1",
        route=lambda msg: {"lens": lens, "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline", "summary": {"total": 0}, "data": {}, "sources": []},
        knowledge=True, uid=uid, make_sources=make_sources, **kw)


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


def _run_fake(tmp_path, monkeypatch, body: str, uid: str, *, lens="qa", make_sources=None,
              sandbox=None, prov=None):
    """偽 codex で 1 ターン実行して (events, env) を返す。"""
    _install_codex(tmp_path, monkeypatch, body, sandbox=sandbox)
    events = list((prov or A.CodexProvider()).run(_ctx(uid, make_sources, lens)))
    return events, _result_env(events)


def _make_sources(docs):
    return [{"doc_id": d, "download_url": f"/documents/download?world=v1&rel={d}"} for d in docs]


_ARGV_LOG_CODEX = (
    _PY + "import pathlib, sys\n"
    "pathlib.Path(r'{log}').open('a', encoding='utf-8').write(repr(sys.argv[1:]) + chr(10))\n"
    + _msg("確認しました。", "m0")
    + _emit({"type": "turn.completed", "usage": {"input_tokens": 1, "cached_input_tokens": 0,
                                                 "output_tokens": 1, "reasoning_output_tokens": 0}}))


def _argv_calls(log) -> list:
    return [eval(line) for line in log.read_text().splitlines() if line.strip()]


# ===== _run_authoring の直読可否・配線 =====

def test_run_authoring_uses_direct_read_ok_for_prompt_and_disables_on_enum_failure(tmp_path, monkeypatch):
    """秘匿列挙が成功すれば prompt に直読許可・profile に KB root の read が入り、失敗（RuntimeError）なら
    direct_read_roots=[] で config が書かれ prompt に「直接読み取りは使えない」が入る（両者を食い違わせない）。"""
    log = tmp_path / "argv.log"
    body = (_PY + "import json, pathlib, sys\n"
            "_prompt = sys.stdin.read() if sys.argv[-1:] == ['-'] else (sys.argv[-1] if sys.argv[1:] else '')\n"
            f"pathlib.Path(r'{log}').open('a', encoding='utf-8').write(_prompt + chr(10) + '---' + chr(10))\n"
            + _msg("ok"))
    _install_codex(tmp_path, monkeypatch, body)
    orig = SB._write_codex_authoring_config
    captured: list = []

    def _capture(*args, **kwargs):
        captured.append(kwargs)
        return orig(*args, **kwargs)

    monkeypatch.setattr(SB, "_write_codex_authoring_config", _capture)
    list(A.CodexProvider().run(_ctx("direct-read-ok-u1")))
    assert captured[-1].get("direct_read_roots") not in (None, [])
    assert any("原本は直接読んでよい" in p for p in log.read_text(encoding="utf-8").split("---\n") if p.strip())

    log.write_text("")
    enum_calls: list = []

    def _enum_fails(*a, **k):
        enum_calls.append(a)
        raise RuntimeError("sensitive_enum_failed:boom")

    monkeypatch.setattr(SB, "_enumerate_sensitive", _enum_fails)
    list(A.CodexProvider().run(_ctx("direct-read-fail-u1")))
    assert len(enum_calls) == 1
    assert captured[-1].get("direct_read_roots") == []
    assert any("今回は原本の直接読み取りは使えない" in p for p in log.read_text(encoding="utf-8").split("---\n") if p.strip())


def test_authoring_symlink_rejected_fail_closed(tmp_path):
    """workspace/authoring・workspace 自体が symlink、不正 uid（パス注入）は fail-closed（None）で Codex を起動しない。"""
    ud = tmp_path / "users"
    (ud / "ok" / "workspace").mkdir(parents=True)
    assert SB._safe_workspace_authoring(ud, "ok") is not None
    evil = tmp_path / "evil"
    evil.mkdir()
    (ud / "bad" / "workspace").mkdir(parents=True)
    (ud / "bad" / "workspace" / "authoring").symlink_to(evil)
    assert SB._safe_workspace_authoring(ud, "bad") is None
    (ud / "bad2").mkdir()
    (ud / "bad2" / "workspace").symlink_to(evil)
    assert SB._safe_workspace_authoring(ud, "bad2") is None
    assert SB._safe_workspace_authoring(ud, "../etc") is None
    assert SB._safe_workspace_authoring(ud, "a/b") is None


def test_codex_sessions_home_symlink_rejected_fail_closed(tmp_path):
    """会話ごとの永続 CODEX_HOME（`.codex-sessions/{cid}`）も symlink・不正 cid を拒否する。"""
    ud = tmp_path / "users"
    (ud / "ok" / "workspace").mkdir(parents=True)
    home = SB._safe_codex_sessions_home(ud, "ok", 42)
    assert home is not None and home.is_dir()
    assert home == ud / "ok" / "workspace" / ".codex-sessions" / "42"
    assert SB._safe_codex_sessions_home(ud, "ok", 42) == home    # 毎ターン再利用
    evil = tmp_path / "evil"
    evil.mkdir()
    (ud / "bad1" / "workspace" / ".codex-sessions").mkdir(parents=True)
    (ud / "bad1" / "workspace" / ".codex-sessions" / "1").symlink_to(evil)
    assert SB._safe_codex_sessions_home(ud, "bad1", 1) is None
    (ud / "bad2" / "workspace").mkdir(parents=True)
    (ud / "bad2" / "workspace" / ".codex-sessions").symlink_to(evil)
    assert SB._safe_codex_sessions_home(ud, "bad2", 1) is None
    assert SB._safe_codex_sessions_home(ud, "ok", "../../etc") is None
    assert SB._safe_codex_sessions_home(ud, "ok", None) is None


# ===== 実行ごとの作業領域（run dir）とその後始末 =====

def test_safe_run_authoring_creates_distinct_dirs_and_fails_closed(tmp_path):
    ud = tmp_path / "users"
    (ud / "u1" / "workspace").mkdir(parents=True)
    r1, r2 = SB._safe_run_authoring(ud, "u1"), SB._safe_run_authoring(ud, "u1")
    assert r1 is not None and r2 is not None and r1 != r2
    for r in (r1, r2):
        assert r.is_dir() and r.name.startswith("run-")
        assert r.parent == ud / "u1" / "workspace" / "authoring"
        assert r.resolve().relative_to((ud / "u1" / "workspace").resolve()) == pathlib.Path("authoring") / r.name

    assert SB._safe_run_authoring(ud, "../etc") is None
    assert SB._safe_run_authoring(ud, "a/b") is None
    (ud / "bad" / "workspace").mkdir(parents=True)
    evil = tmp_path / "evil"
    evil.mkdir()
    (ud / "bad" / "workspace" / "authoring").symlink_to(evil)
    assert SB._safe_run_authoring(ud, "bad") is None
    assert not list(evil.iterdir())


def test_safe_run_authoring_sweeps_stale_run_dirs_but_keeps_fresh_ones(tmp_path):
    """24 時間より古い run-* だけ best-effort で掃除する。新しいもの・symlink（と指す先）は残す。"""
    ud = tmp_path / "users"
    authoring = ud / "u1" / "workspace" / "authoring"
    authoring.mkdir(parents=True)
    stale = authoring / "run-deadbeef0000"
    stale.mkdir()
    (stale / "leftover.txt").write_text("crashed run leftover", encoding="utf-8")
    old_time = time.time() - SB._RUN_DIR_TTL_SECONDS - 3600
    os.utime(stale, (old_time, old_time))
    fresh = authoring / "run-cafebabe0000"
    fresh.mkdir()
    link_target = tmp_path / "evil-link-target"
    link_target.mkdir()
    link = authoring / "run-symlinked00000"
    link.symlink_to(link_target)
    os.utime(link, (old_time, old_time), follow_symlinks=False)

    new_run = SB._safe_run_authoring(ud, "u1")

    assert new_run is not None and new_run.is_dir()
    assert not stale.exists()
    assert fresh.is_dir()
    assert link.is_symlink() and link_target.is_dir()


def test_safe_run_authoring_keeps_active_run_dir_even_when_mtime_is_stale(tmp_path):
    """稼働中として登録された run dir は mtime が期限切れでも掃除対象から外れ、解放後の次回スイープで掃除される。"""
    ud = tmp_path / "users"
    (ud / "u1" / "workspace").mkdir(parents=True)
    active = SB._safe_run_authoring(ud, "u1")
    assert active is not None
    old_time = time.time() - SB._RUN_DIR_TTL_SECONDS - 3600
    os.utime(active, (old_time, old_time))
    another = SB._safe_run_authoring(ud, "u1")
    assert another is not None and another != active
    assert active.is_dir()
    SB._release_active_run_dir(active)
    assert SB._safe_run_authoring(ud, "u1") is not None
    assert not active.exists()


def test_chmod_if_not_symlink_never_follows_symlinks(tmp_path):
    target = tmp_path / "target-file"
    target.write_text("x", encoding="utf-8")
    os.chmod(target, 0o644)
    link = tmp_path / "link-to-file"
    link.symlink_to(target)
    SB._chmod_if_not_symlink(str(link))
    assert stat.S_IMODE(os.stat(target).st_mode) == 0o644


def test_remove_dir_best_effort_does_not_chmod_symlink_targets(tmp_path):
    """後始末は symlink のリンク先（run dir 外）の権限を変えず、書込不可ディレクトリ配下でも run dir を削除できる。"""
    run_dir = tmp_path / "run-abc123456789"
    locked = run_dir / "locked"
    locked.mkdir(parents=True)
    evil_target = tmp_path / "external-target"
    evil_target.mkdir()
    os.chmod(evil_target, 0o755)
    (locked / "evil").symlink_to(evil_target)
    os.chmod(locked, 0o500)
    SB._remove_dir_best_effort(run_dir)
    assert stat.S_IMODE(os.stat(evil_target).st_mode) == 0o755
    assert not run_dir.exists()


def test_remove_dir_best_effort_does_not_chmod_hardlinked_files(tmp_path):
    """ファイルには chmod しない（run dir 外のファイルとのハードリンクで外側の権限を変えてしまうため）。"""
    run_dir = tmp_path / "run-hardlink0000001"
    locked = run_dir / "locked"
    locked.mkdir(parents=True)
    external = tmp_path / "external-shared-file.txt"
    external.write_text("shared content", encoding="utf-8")
    os.chmod(external, 0o644)
    os.link(str(external), str(locked / "hardlinked.txt"))
    os.chmod(locked, 0o500)
    SB._remove_dir_best_effort(run_dir)
    assert stat.S_IMODE(os.stat(external).st_mode) == 0o644
    assert not run_dir.exists()


def test_cleanup_stale_run_dirs_recovers_permission_restricted_leftover(tmp_path):
    ud = tmp_path / "users"
    authoring = ud / "u1" / "workspace" / "authoring"
    authoring.mkdir(parents=True)
    stale = authoring / "run-permlocked0000"
    locked_sub = stale / "locked"
    locked_sub.mkdir(parents=True)
    (locked_sub / "leftover.txt").write_text("crashed run leftover", encoding="utf-8")
    os.chmod(locked_sub, 0o500)
    old_time = time.time() - SB._RUN_DIR_TTL_SECONDS - 3600
    os.utime(stale, (old_time, old_time))
    assert SB._safe_run_authoring(ud, "u1") is not None
    assert not stale.exists()


def test_cleanup_stale_run_dirs_stat_failure_is_logged_without_leaking_path(monkeypatch, tmp_path, caplog):
    """staleness 判定の stat 失敗は掃除対象から外し、警告に絶対パス・例外文字列を出さず型と errno だけを記録する。"""
    authoring = tmp_path / "users" / "u1" / "workspace" / "authoring"
    authoring.mkdir(parents=True)
    target = authoring / "run-statfail0000"
    target.mkdir()
    orig_stat = pathlib.Path.stat
    calls: dict = {}

    def _boom_stat(self, *a, **kw):
        # 掃除対象フィルタ（is_symlink/is_dir は内部で stat を呼ぶ）ぶんは通し、mtime 取得（3 回目）だけ失敗させる
        if self.name == "run-statfail0000":
            calls[self.name] = calls.get(self.name, 0) + 1
            if calls[self.name] >= 3:
                raise OSError(13, "Permission denied", str(self))
        return orig_stat(self, *a, **kw)

    monkeypatch.setattr(pathlib.Path, "stat", _boom_stat)
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        SB._cleanup_stale_run_dirs(authoring)
    monkeypatch.undo()

    matched = [r for r in caplog.records if "stale codex run dir cleanup failed" in r.message]
    assert matched
    for r in matched:
        assert str(tmp_path) not in r.message
        assert "Permission denied" not in r.message
        assert "type=PermissionError" in r.message and "errno=13" in r.message
    assert target.exists()


def test_restore_removable_permissions_recovers_when_root_itself_is_mode_000(tmp_path):
    run_dir = tmp_path / "run-rootlocked0000"
    child = run_dir / "child"
    child.mkdir(parents=True)
    (child / "leftover.txt").write_text("crashed run leftover", encoding="utf-8")
    os.chmod(child, 0o000)
    os.chmod(run_dir, 0o000)
    SB._remove_dir_best_effort(run_dir)
    assert not run_dir.exists()


# ===== multi_agent フラグ・窓・停止監視 =====

@pytest.mark.parametrize("sandbox,expect_flag", [(None, True), ("0", False)])
def test_codex_run_argv_multi_agent_flag(tmp_path, monkeypatch, sandbox, expect_flag):
    """既定（OpenAI＋サンドボックス）は `features.multi_agent=true` と env["codex_multi_agent"]=True。
    サンドボックス無効（config 無し）は false を明示し、AGENTS.md にも役割段落を出さない。"""
    log = tmp_path / "argv.log"
    captured: dict = {}
    orig_write = codex_agents_md.write_agents_md

    def _spy(authoring, **kw):
        captured.update(kw)
        return orig_write(authoring, **kw)

    monkeypatch.setattr(codex_agents_md, "write_agents_md", _spy)
    _install_codex(tmp_path, monkeypatch, _ARGV_LOG_CODEX.replace("{log}", str(log)), sandbox=sandbox)
    events = list(A.CodexProvider().run(_ctx("multi-agent-argv-u1")))
    calls = _argv_calls(log)
    assert len(calls) == 1
    assert ("features.multi_agent=true" in calls[0]) is expect_flag
    assert ("features.multi_agent=false" in calls[0]) is (not expect_flag)
    assert captured.get("multi_agent") is expect_flag
    assert _result_env(events)["codex_multi_agent"] is expect_flag


@pytest.mark.parametrize("model", [None, "gpt-4.1"])
def test_codex_run_argv_does_not_pass_model_context_window(tmp_path, monkeypatch, caplog, model):
    """モデルの文脈窓は Codex CLI 任せ: argv に model_context_window を渡さず、codex.log 開始行は window=none。"""
    log = tmp_path / "argv.log"
    _install_codex(tmp_path, monkeypatch, _ARGV_LOG_CODEX.replace("{log}", str(log)))
    with caplog.at_level(logging.INFO, logger="sherpa.codex"):
        list((A.CodexProvider(model=model) if model else A.CodexProvider()).run(_ctx("window-none-u1")))
    calls = _argv_calls(log)
    assert len(calls) == 1
    assert not any(str(a).startswith("model_context_window=") for a in calls[0])
    start = [r.message for r in caplog.records if r.name == "sherpa.codex" and "start conv=" in r.message]
    assert start and "window_cli=none" in start[0] and "window_source=none" in start[0]


def test_spawn_stop_watcher_kills_promptly_and_exits_on_natural_finish():
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    try:
        ev = threading.Event()
        PROC._spawn_stop_watcher(proc, ev, threading.Lock(), {"done": False})
        time.sleep(0.2)
        assert proc.poll() is None
        t0 = time.time()
        ev.set()
        proc.wait(timeout=3)
        assert time.time() - t0 < 3
        assert proc.returncode not in (None, 0)
    finally:
        if proc.poll() is None:
            proc.kill()
    # 自然終了したら監視スレッドは自分から抜ける（無期限 wait だとスレッドが積み上がる）
    proc2 = subprocess.Popen(["true"], start_new_session=True)
    t = PROC._spawn_stop_watcher(proc2, threading.Event(), threading.Lock(), {"done": False})
    proc2.wait(timeout=3)
    t.join(timeout=2)
    assert not t.is_alive()


def test_ctx_fields_and_compat_mode_uid_admin():
    fields = {f.name: f for f in dataclasses.fields(A.Ctx)}
    assert fields["uid"].default == "admin"
    assert fields["stop_event"].default is None
    from sherpa import auth, chat_service
    assert auth.auth_disabled()
    assert "uid=user_id" in inspect.getsource(chat_service.stream_message)


def test_office_libs_importable():
    """python-docx / python-pptx / openpyxl が import できる（Feature A の依存）。未インストールなら skip。"""
    missing = []
    for mod, label in (("docx", "python-docx"), ("pptx", "python-pptx"), ("openpyxl", "openpyxl")):
        try:
            __import__(mod)
        except ImportError:
            missing.append(label)
    if missing:
        pytest.skip(f"Office ライブラリ未インストール: {missing}")


# ===== 個人ファイル（workspace）の grep・事実化・フラグ =====

def _personal_files(tmp_path, uid: str, files: dict) -> None:
    d = tmp_path / uid / "workspace" / "files"
    d.mkdir(parents=True)
    for name, text in files.items():
        (d / name).write_text(text, encoding="utf-8")


def test_personal_grep_hits_own_file_sensitive_skipped_and_code_extension(tmp_path):
    """本人の workspace/files/ のヒットを返す。秘匿名は検索可能な拡張子でも出さず、コード拡張子も層と無関係にヒットする。"""
    from sherpa.chat_service import _personal_grep_hits
    _personal_files(tmp_path, "u_own", {"mytax.txt": "TAX_RATE=0.10\nshohizei\n"})
    with patch("sherpa.store.live_workspace_rel_paths", return_value={"mytax.txt"}):
        hits = _personal_grep_hits("u_own", "TAX_RATE", str(tmp_path))
    assert hits and hits[0]["rel_path"] == "mytax.txt"
    assert "TAX_RATE" in hits[0]["text"] and hits[0]["source"] == "個人ファイル内ヒット"

    _personal_files(tmp_path, "u_sens", {"credentials.csv": "API_TOKEN,abc\n", "memo.csv": "API_TOKEN,memo\n"})
    with patch("sherpa.store.live_workspace_rel_paths", return_value={"credentials.csv", "memo.csv"}):
        hits = _personal_grep_hits("u_sens", "API_TOKEN", str(tmp_path))
    assert [h["rel_path"] for h in hits] == ["memo.csv"]

    _personal_files(tmp_path, "u_code", {"MYPROG.cbl": "TAX_RATE=0.10\n"})
    with patch("sherpa.store.live_workspace_rel_paths", return_value={"MYPROG.cbl"}):
        hits = _personal_grep_hits("u_code", "TAX_RATE", str(tmp_path))
    assert hits and hits[0]["rel_path"] == "MYPROG.cbl"


def test_personal_grep_hits_cross_user_isolation(tmp_path):
    """user_a のファイルは user_b の grep に出ない（越境不可）。"""
    from sherpa.chat_service import _personal_grep_hits
    _personal_files(tmp_path, "user_a_iso", {"secret_a.txt": "SUPER_SECRET_A"})
    (tmp_path / "user_b_iso" / "workspace" / "files").mkdir(parents=True)
    with patch("sherpa.store.live_workspace_rel_paths", return_value=set()):
        assert _personal_grep_hits("user_b_iso", "SUPER_SECRET_A", str(tmp_path)) == []


def test_personal_facts_and_citations_composition():
    from sherpa.chat_service import _personal_citations, _personal_facts
    hit = {"rel_path": "myfile.txt", "line": 3, "text": "TAX=10", "match": "TAX", "source": "個人ファイル内ヒット"}
    facts = _personal_facts([hit], "TAX")
    assert "個人ファイル内ヒット" in facts and "myfile.txt" in facts

    cites = _personal_citations([
        {"rel_path": "a.txt", "line": 1, "text": "foo", "match": "foo", "source": "個人ファイル内ヒット"},
        {"rel_path": "a.txt", "line": 5, "text": "foo2", "match": "foo", "source": "個人ファイル内ヒット"},
        {"rel_path": "b.md", "line": 2, "text": "bar", "match": "bar", "source": "個人ファイル内ヒット"}])
    assert len(cites) == 2
    assert all(c["source"] == "個人ファイル内ヒット" for c in cites)
    assert {c["doc_id"] for c in cites} == {"a.txt", "b.md"}


def test_personal_scope_invariants_by_source():
    """個人ヒットは ES/グラフに入れず（台帳基準）、層フィルタを受け取らず、personal=OFF では呼ばれない。"""
    from sherpa import chat_service
    grep_src = inspect.getsource(chat_service._personal_grep_hits)
    assert "es_index" not in grep_src and "world_graph" not in grep_src
    assert "live_workspace_rel_paths" in grep_src
    assert "layer" not in inspect.signature(chat_service._personal_grep_hits).parameters
    assert "layer" not in grep_src
    stream_src = inspect.getsource(chat_service.stream_message)
    assert "if personal:" in stream_src
    assert "personal_hits = []" in stream_src or "personal_hits: list[dict] = []" in stream_src


def test_personal_workspace_flag_wiring_by_source():
    """個人由来フラグ: stream_message が codex_wrote_files で _used_personal を立て、
    フラグはアシスタント保存（2 回目の add_message）より先に立てる。"""
    from sherpa import chat_service, store
    store_src = inspect.getsource(store.set_contains_personal_workspace)
    assert "contains_personal_workspace=TRUE" in store_src
    for fn in (chat_service.stream_message,):
        src = inspect.getsource(fn)
        assert "set_contains_personal_workspace" in src and "codex_wrote_files" in src
        idx_flag = src.find("set_contains_personal_workspace")
        idx_msg2 = src.find("add_message", src.find("add_message") + 1)
        assert idx_msg2 != -1 and idx_flag < idx_msg2, fn.__name__
    assert "_used_personal" in inspect.getsource(chat_service.stream_message)
    # ナレッジ参照オフの素の会話でも、個人ファイルの事実は回答 env の個人由来の印（`_personal_facts`）として運ばれる。
    class _Plain:
        label = "test"

        def _plain_text(self, message=""):
            return "本文"

    ctx = A.Ctx(message="こんにちは", world="w", route=lambda m: {"lens": "chat"},
                dispatch=lambda *a, **k: None, knowledge=False, personal_facts="個人ヒット")
    res = [e for e in BASE._plain_run(_Plain(), ctx) if e.get("type") == "_result"][0]
    assert res["env"]["_personal_facts"] == "個人ヒット" and res["env"]["headline"] == "本文"


# ===== web_search の管理者許可×ユーザー希望 =====

def test_web_search_admin_allowed_reads_system_settings(monkeypatch):
    assert A._web_search_admin_allowed({}) is False
    assert A._web_search_admin_allowed({"web_search_allowed": False}) is False
    assert A._web_search_admin_allowed({"web_search_allowed": True}) is True

    def _boom():
        raise RuntimeError("db down")

    monkeypatch.setattr("sherpa.store.get_system_settings", _boom)
    assert A._web_search_admin_allowed() is False       # DB 不達は安全側（env へフォールバックしない）


@pytest.mark.parametrize("admin,user,expect_disabled", [
    (False, False, True), (False, True, True), (True, False, True), (True, True, False)])
def test_web_search_gating_matrix(tmp_path, admin, user, expect_disabled):
    """管理者許可 AND ユーザー希望のときだけ有効。sandbox（config.toml）・fallback（-c 引数）・単一の真実源が一致する。"""
    sysset = {"web_search_allowed": admin}
    assert (SB._web_search_disabled_value(user, system_settings=sysset) == "disabled") is expect_disabled
    cfg = _cfg(tmp_path, mcp=False, web_search_enabled=user, system_settings=sysset)
    assert ('web_search = "disabled"' in cfg) is expect_disabled
    tomllib.loads(cfg)
    assert SB._web_search_c_args(user, sysset) == (["-c", 'web_search="disabled"'] if expect_disabled else [])


def test_web_search_defaults_and_ollama_construct(tmp_path):
    """省略時は常に disabled。Codex(Ollama) 構成は管理者許可＋ユーザー希望でも disabled のまま。"""
    assert 'web_search = "disabled"' in _cfg(tmp_path, mcp=False, system_settings={}, name="d")
    cfg = _cfg(tmp_path, mcp=False, name="o", web_search_enabled=True,
               ollama_base_url="http://localhost:11434", system_settings={"web_search_allowed": True})
    assert 'web_search = "disabled"' in cfg
    assert "[model_providers.sherpa-ollama]" in cfg


def test_codex_provider_web_search_field_and_select_provider_wiring():
    assert A.CodexProvider(web_search=True)._web_search is True
    assert A.CodexProvider()._web_search is False
    assert "codex_web_search" in inspect.getsource(providers_pkg._select_provider)


# ===== -o 最終メッセージ・AGENTS.md の書込 =====

def test_read_last_message_fallback(tmp_path):
    """無し/空は None、中身は前後空白を除いて返す。symlink は追従せず、メモリ保護の上限超過は読まない。"""
    assert PROC._read_last_message_fallback(tmp_path / "no-such-file.txt") is None
    empty = tmp_path / "empty.txt"
    empty.write_text("   \n\n  ", encoding="utf-8")
    assert PROC._read_last_message_fallback(empty) is None
    ok = tmp_path / "ok.txt"
    ok.write_text("  最終回答のテキストです。\n", encoding="utf-8")
    assert PROC._read_last_message_fallback(ok) == "最終回答のテキストです。"
    secret = tmp_path / "secret.txt"
    secret.write_text("SECRET DATA SHOULD NOT LEAK", encoding="utf-8")
    link = tmp_path / "last-message.txt"
    link.symlink_to(secret)
    assert PROC._read_last_message_fallback(link) is None
    big = tmp_path / "big.txt"
    big.write_text("x" * (PROC._LAST_MESSAGE_MAX_BYTES + 1), encoding="utf-8")
    assert PROC._read_last_message_fallback(big) is None


def test_agents_md_written_with_required_phrases_idempotent_and_replaces_symlink(tmp_path):
    d = tmp_path / "a"
    d.mkdir()
    codex_agents_md.write_agents_md(d)
    txt = (d / "AGENTS.md").read_text(encoding="utf-8")
    for phrase in ("KB", "authoring 直下", "推定", "全件", "省略しない", "list_docs", "path_prefix",
                   "どのフォルダを数えたか", "参照した資料"):
        assert phrase in txt, phrase
    for removed in ("推測しない", "出典の列挙は不要", "簡潔に回答", "憶測で回答"):
        assert removed not in txt, removed
    codex_agents_md.write_agents_md(d)
    assert (d / "AGENTS.md").read_text(encoding="utf-8") == txt   # 冪等

    # 既存 AGENTS.md が symlink: 指す先には書かず、AGENTS.md 自体を通常ファイルへ置き換える
    d2 = tmp_path / "b"
    d2.mkdir()
    outside = tmp_path / "outside-target.txt"
    outside.write_text("SHOULD NOT BE OVERWRITTEN", encoding="utf-8")
    (d2 / "AGENTS.md").symlink_to(outside)
    codex_agents_md.write_agents_md(d2)
    assert outside.read_text(encoding="utf-8") == "SHOULD NOT BE OVERWRITTEN"
    assert not (d2 / "AGENTS.md").is_symlink() and "KB" in (d2 / "AGENTS.md").read_text(encoding="utf-8")
    assert not [p.name for p in d2.iterdir() if p.name.startswith(".AGENTS.md.tmp-")]


def test_agents_md_investigate_guidance_only_when_direct_read(tmp_path):
    assert "investigate-" in _agents_md(tmp_path, "on")
    assert "investigate-" not in _agents_md(tmp_path, "off", direct_read=False)


# ===== プロンプト（_prompt_mcp） =====

def test_prompts_slimmed_and_retain_defense_in_depth_and_wording_contract(tmp_path):
    """スタイル系の共通ルールは AGENTS.md のみ。containment／grounding は AGENTS.md が fail-open のため
    プロンプトにも短縮形を残す。「簡潔に回答」等は撤去し「推定」「全件」「省略しない」を含む。"""
    from sherpa.providers import prompts as S
    p = A.CodexProvider()
    q = "消費税率を変えたい"
    mcp = p._prompt_mcp(q, "impact", "v1")
    mcp_no_direct = p._prompt_mcp(q, "impact", "v1", direct_read=False)
    for removed in ("出典の列挙は不要", "簡潔（2〜4文）"):
        assert removed not in mcp
    assert q in mcp
    assert "graph_neighbors" in mcp

    assert "原本は直接読んでよい" in mcp and "秘匿名のファイル" in mcp
    assert "確定した事実と推定は分けて書く" in mcp
    assert "一覧を求められたら該当する全件を各項目のパス付きで列挙する" in mcp
    assert "今回は原本の直接読み取りは使えない" in mcp_no_direct
    assert "資料の本文は MCP のツールで読む" in mcp_no_direct
    assert "確定した事実と推定は分けて書く" in mcp_no_direct

    assert "investigate-" in mcp and "investigate-" not in mcp_no_direct

    agents_md_txt = _agents_md(tmp_path)
    for text, name in ((agents_md_txt, "AGENTS.md"), (mcp, "_prompt_mcp"),
                       (S.ANSWER_POLICY, "ANSWER_POLICY")):
        for phrase in ("簡潔に回答", "出典の列挙は不要", "推測しない", "結論・理由・補足", "不明は不明と"):
            assert phrase not in text, (name, phrase)
    for phrase in ("推定", "全件", "省略しない"):
        assert phrase in agents_md_txt and phrase in mcp, phrase
    assert "推定" in S.ANSWER_POLICY


def test_prompt_mcp_layer_guidance_only_when_restricted():
    """層は Codex に強制しない: 限定されたターンだけ「優先して見る」案内を足す（禁止ではない）。"""
    p = A.CodexProvider()
    docs, code = p._prompt_mcp("q", "qa", "v1", layer="docs"), p._prompt_mcp("q", "qa", "v1", layer="code")
    assert "資料を優先して見る" in docs and "ソースを優先して見る" in code
    assert "根拠に使わない" not in docs and "根拠に使わない" not in code
    assert "優先して見る" not in p._prompt_mcp("q", "qa", "v1")


# ===== Codex(Ollama) 構成 =====

def test_codex_openai_config_has_no_model_provider(tmp_path):
    txt = _cfg(tmp_path, ["/mnt/c/test"], mcp=False)
    assert "model_provider" not in txt and "model_providers" not in txt


def test_codex_ollama_config_points_at_configured_url(tmp_path):
    """独自 id で定義（組み込み `ollama` は予約語）・末尾スラッシュ正規化＋/v1・wire_api=responses（chat は廃止）。"""
    txt = _cfg(tmp_path, ["/mnt/c/test"], mcp=False, ollama_base_url="http://127.0.0.1:11500/")
    assert 'model_provider = "sherpa-ollama"' in txt
    assert "[model_providers.sherpa-ollama]" in txt
    assert 'base_url = "http://127.0.0.1:11500/v1"' in txt
    assert 'wire_api = "responses"' in txt and 'wire_api = "chat"' not in txt


def test_codex_ollama_selection_and_blocked_destination(monkeypatch):
    from sherpa.providers import _select_provider, _UnwiredProvider
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda: {
        "model_catalog": {"codex": {"codex": {"allowed": ["gpt-oss:20b"], "default": "gpt-oss:20b"}}}})
    p = _select_provider({"agent": "codex", "codex_model_provider": "ollama", "ollama_url": "http://localhost:11434"})
    assert p._ollama_base_url == "http://localhost:11434"
    assert _select_provider({"agent": "codex", "codex_model_provider": "openai"})._ollama_base_url is None
    # 許可されていない接続先（loopback でも allowlist でもない）は起動せず未接続
    blocked = _select_provider({"agent": "codex", "codex_model_provider": "ollama",
                                "ollama_url": "http://198.51.100.7:11434"})
    assert isinstance(blocked, _UnwiredProvider)


# ===== 出典収集（参照した資料・MCP の読取引数） =====

def test_referenced_docs_block_promotes_verified_docs_to_sources(tmp_path, monkeypatch):
    """回答末尾の「参照した資料:」から台帳で実在確認できた 1 件だけを sources の先頭に足す（実在しない・秘匿名は捨てる）。"""
    answer = f"消費税率は10%です。\n\n参照した資料:\n- {_DOC}\n- 存在しない.md\n- .env\n"
    _, env = _run_fake(tmp_path, monkeypatch, _PY + _msg(answer), "citation-ok-u1", make_sources=_make_sources)
    assert env["codex_referenced_docs"] == {"listed": 3, "verified": 1}
    assert env["sources"][0]["doc_id"] == _DOC and "rel=" in env["sources"][0]["download_url"]
    assert not any(s["doc_id"] in ("存在しない.md", ".env") for s in env["sources"])
    assert "参照した資料" not in env["headline"] and "消費税率は10%です。" in env["headline"]
    assert env["sources_verified"] == [_DOC]


def test_referenced_docs_block_keeps_body_when_zero_verified(tmp_path, monkeypatch):
    answer = "消費税率は10%です。\n\n参照した資料:\n- 存在しない1.md\n- 存在しない2.md\n"
    _, env = _run_fake(tmp_path, monkeypatch, _PY + _msg(answer), "citation-zero-u1", make_sources=_make_sources)
    assert env["codex_referenced_docs"] == {"listed": 2, "verified": 0}
    assert env["body"].strip() == answer.strip()         # 本文は書き換えず、確認できなかった旨は注記に出る
    assert [n["kind"] for n in env["notices"]] == ["sources_unverified"] and "2 件" in env["notices"][0]["text"]
    assert not env.get("sources") and "sources_verified" not in env


@pytest.mark.parametrize("tool,extra,verified", [
    ("read_doc", {}, None),
    ("xlsx_range", {"sheet": "Sheet1"}, None),       # 原本読取ツールも read_doc と同じ経路で拾う
    ("xlsx_sheets", {}, []),                          # シート一覧だけでは「精読済み」に数えない
])
def test_mcp_read_args_collected_as_referenced_docs(tmp_path, monkeypatch, tool, extra, verified):
    body = _PY + _mcp_call(tool, {"doc_id": _DOC, **extra}) + _msg("消費税率は10%です。", "2")
    _, env = _run_fake(tmp_path, monkeypatch, body, f"citation-mcp-{tool}-u1", make_sources=_make_sources)
    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    assert env["sources"][0]["doc_id"] == _DOC
    assert env["headline"] == "消費税率は10%です。"
    if verified is not None:
        assert env.get("sources_verified") == verified


def test_failed_mcp_read_is_not_a_source(tmp_path, monkeypatch):
    """失敗した読取（status=failed／result.isError／進行中のみ）の引数は出典にも「根拠」にも載せない。"""
    body = (_PY + _mcp_call("read_doc", {"doc_id": _DOC}, "t1", status="failed")
            + _mcp_call("read_around", {"doc_id": _DOC, "line": 1}, "t2", result={"isError": True})
            + _emit({"type": "item.started", "item": {"id": "t3", "type": "mcp_tool_call", "tool": "doc_outline",
                                                      "status": "in_progress", "arguments": {"doc_id": _DOC}}})
            + _msg("消費税率は10%です。", "2"))
    _, env = _run_fake(tmp_path, monkeypatch, body, "citation-mcp-fail-u1", make_sources=_make_sources)
    assert env["codex_referenced_docs"] == {"listed": 0, "verified": 0}
    assert not env.get("sources") and "sources_verified" not in env


# ===== サイドカー（子エージェントの観測） =====

def _sidecar_snippet(entries: list, var: str = "CODEX_HOME") -> str:
    """サイドカーへ JSONL を書く偽 codex 断片。var=CODEX_HOME は codex_home 配下、
    SHERPA_MCP_SIDECAR は非サンドボックス経路（env が指すパスへ直接書く）。"""
    lines = ", ".join(json.dumps(json.dumps(e, ensure_ascii=False)) for e in entries)
    target = ("pathlib.Path(os.environ['CODEX_HOME']) / '.mcp_sidecar.jsonl'" if var == "CODEX_HOME"
              else "pathlib.Path(os.environ['SHERPA_MCP_SIDECAR'])")
    return f"import os, pathlib\n({target}).write_text(chr(10).join([{lines}]) + chr(10))\n"


@pytest.mark.parametrize("same_doc", [False, True])
def test_sidecar_read_doc_promoted_to_sources_verified_without_duplication(tmp_path, monkeypatch, same_doc):
    """子だけが読んだ doc_id は sources_verified に入り、親が観測した同じ doc_id と二重に数えない。"""
    child = _DOC if same_doc else _DOC2
    body = (_PY + _sidecar_snippet([{"kind": "read", "tool": "read_doc", "doc_id": child, "ts": 1.0}])
            + _mcp_call("read_doc", {"doc_id": _DOC}) + _msg("消費税率は10%です。", "2"))
    _, env = _run_fake(tmp_path, monkeypatch, body, f"sidecar-read-{same_doc}-u1", make_sources=_make_sources)
    expected = sorted({_DOC, child})
    assert env["codex_referenced_docs"] == {"listed": len(expected), "verified": len(expected)}
    assert sorted(env["sources_verified"]) == expected
    doc_ids = [s["doc_id"] for s in env["sources"]]
    assert all(doc_ids.count(d) == 1 for d in expected)


def test_sidecar_ask_user_becomes_confirmation_card_once(tmp_path, monkeypatch):
    """子だけが呼んだ ask_user は run 終了後にサイドカーから確認カードを一度だけ出し、回答（_result）は保存しない。"""
    question = {"type": "question", "interaction_id": "child-q1", "mode": "single",
                "prompt": "この資料で合っていますか", "allow_free_text": False,
                "options": [{"id": "yes", "label": "はい", "description": ""},
                            {"id": "no", "label": "いいえ", "description": ""}]}
    body = _PY + _sidecar_snippet([{"kind": "ask_user", "ts": 1.0, "question": question}]) + _msg(
        "確認した結果、影響はありません。")
    _install_codex(tmp_path, monkeypatch, body)
    events = list(A.CodexProvider().run(_ctx("sidecar-ask-u1", _make_sources)))
    questions = [e for e in events if isinstance(e, dict) and e.get("type") == "question"]
    assert len(questions) == 1
    assert questions[0]["interaction_id"] == "child-q1" and questions[0]["prompt"] == "この資料で合っていますか"
    assert [e for e in events if isinstance(e, dict) and e.get("type") == "_result"] == []


@pytest.mark.parametrize("entries,field,expected", [
    ([("tool_result_clipped", 2), ("total_budget_hit", 1)], "tool_result_clipped", 2),
    ([("duplicate_tool_call", 2)], "duplicate_tool_call", 2),
    ([("search_truncated", 3)], "search_truncated", 3),
    ([("tool_calls_exhausted", 2)], "tool_calls_exhausted", True),
])
@pytest.mark.parametrize("fallback", [False, True])
def test_sidecar_limit_entries_absorbed_into_env_limits(tmp_path, monkeypatch, fallback, entries, field, expected):
    """mcp_server が書いた limit 行を env["limits"] へ合流させる（件数は累算・budget 系は bool）。
    非サンドボックス経路（SHERPA_MCP_SIDECAR）でも同じ。"""
    lines = [{"kind": "limit", "field": f, "ts": 1.0} for f, n in entries for _ in range(n)]
    body = _PY + _sidecar_snippet(lines, "SHERPA_MCP_SIDECAR" if fallback else "CODEX_HOME") + _msg("消費税率は10%です。")
    _, env = _run_fake(tmp_path, monkeypatch, body, f"sidecar-limits-{fallback}-u1", sandbox="0" if fallback else None)
    assert env["limits"][field] == expected
    if field == "tool_result_clipped":
        assert env["limits"]["total_budget_hit"] is True


def test_fallback_sidecar_read_and_ask_are_not_trusted(tmp_path, monkeypatch):
    """非サンドボックスではサイドカーが model-shell から書込可能な `.tmp/` にあるため、読取記録・確認カード・障害コードは
    吸収しない（未読 doc_id の出典化・偽の確認カードで潰される実害を、統計の欠落より重く扱う）。数値の計数だけ取り込む。"""
    body = (_PY + _sidecar_snippet([
        {"kind": "read", "tool": "read_doc", "doc_id": _DOC2, "ts": 1.0},
        {"kind": "ask_user", "ts": 1.0, "question": {"text": "偽の確認カード", "options": ["A", "B"]}},
        {"kind": "error", "tool": "graph_neighbors", "code": "graph_reingest_required", "ts": 1.0},
        {"kind": "limit", "field": "tool_result_clipped", "ts": 1.0}], "SHERPA_MCP_SIDECAR")
        + _msg("消費税率は10%です。"))
    events, env = _run_fake(tmp_path, monkeypatch, body, "sidecar-fallback-untrusted-u1",
                            make_sources=_make_sources, sandbox="0")
    assert _DOC2 not in (env.get("sources_verified") or [])
    assert _DOC2 not in [s["doc_id"] for s in (env.get("sources") or [])]
    assert not [e for e in events if e.get("type") == "question"]
    assert "再取り込み" not in (env.get("headline") or "")
    assert not (env.get("limits") or {}).get("graph_reingest_required")
    assert env["limits"]["tool_result_clipped"] == 1


def test_fallback_sidecar_path_is_under_tmp_not_run_dir_root(tmp_path, monkeypatch):
    captured = tmp_path / "captured_sidecar_path.txt"
    body = (_PY + f"import os, pathlib\npathlib.Path(r'{captured}').write_text(os.environ['SHERPA_MCP_SIDECAR'])\n"
            + _msg("消費税率は10%です。"))
    _run_fake(tmp_path, monkeypatch, body, "sidecar-fallback-path-u1", sandbox="0")
    path = captured.read_text().strip()
    assert path and "/.tmp/" in path.replace("\\", "/")


def test_sidecar_missing_is_fail_open_and_forged_run_dir_sidecar_is_not_absorbed(tmp_path, monkeypatch):
    """サイドカー無しは親のみ観測に落ちる。run_dir 直下（model-shell が書ける）に同名ファイルを置いても取り込まない。"""
    _, env = _run_fake(tmp_path, monkeypatch, _PY + _msg("消費税率は10%です。"), "sidecar-missing-u1",
                       make_sources=_make_sources)
    assert env["headline"] == "消費税率は10%です。"
    assert not env.get("sources") and "sources_verified" not in env

    forged = json.dumps({"kind": "read", "tool": "read_doc", "doc_id": _DOC2, "ts": 1.0}, ensure_ascii=False)
    body = (_PY + f"import pathlib\npathlib.Path('.mcp_sidecar.jsonl').write_text({json.dumps(forged)} + '\\n')\n"
            + _msg("消費税率は10%です。"))
    _, env2 = _run_fake(tmp_path, monkeypatch, body, "sidecar-forged-run-dir-u1", make_sources=_make_sources)
    assert env2["headline"] == "消費税率は10%です。"
    assert not env2.get("sources") and "sources_verified" not in env2


def test_sidecar_path_not_within_codex_write_permitted_roots(tmp_path):
    """サイドカー（codex_home 配下）は書込許可ルート（cwd=run_dir）と包含関係を持たない。"""
    from sherpa.providers.codex.turn_consts import _MCP_SIDECAR_NAME
    run_dir = tmp_path / "users" / "u1" / "workspace" / "authoring" / "run-deadbeef"
    run_dir.mkdir(parents=True)
    codex_home = tmp_path / "users" / "u1" / "workspace" / ".codexhome-deadbeef"
    sidecar_path = codex_home / _MCP_SIDECAR_NAME
    SB._write_codex_authoring_config(codex_home, ["/kb"], "low", True, "test", None, sidecar_path=str(sidecar_path))
    cfg = (codex_home / "config.toml").read_text()
    assert '"." = "write"' in cfg and str(sidecar_path) in cfg
    with pytest.raises(ValueError):
        sidecar_path.resolve().relative_to(run_dir.resolve())
    with pytest.raises(ValueError):
        run_dir.resolve().relative_to(sidecar_path.parent.resolve())


def test_sidecar_corrupt_lines_skipped_and_not_registered_as_created_file(tmp_path, monkeypatch):
    """壊れた行（不正 JSON・非 dict）があっても正しい行は拾い（fail-open）、サイドカー自体は成果物登録の対象にならない。"""
    good = json.dumps({"kind": "read", "tool": "read_doc", "doc_id": _DOC2, "ts": 1.0}, ensure_ascii=False)
    body = (_PY + "import os, pathlib\n(pathlib.Path(os.environ['CODEX_HOME']) / '.mcp_sidecar.jsonl')"
            f".write_text('not-json\\n' + {json.dumps(good)} + '\\n' + '[]\\n')\n" + _msg("消費税率は10%です。"))
    _, env = _run_fake(tmp_path, monkeypatch, body, "sidecar-corrupt-u1", make_sources=_make_sources)
    assert env["codex_referenced_docs"] == {"listed": 1, "verified": 1}
    assert env["sources_verified"] == [_DOC2]
    assert not env.get("codex_wrote_files") and not env.get("created_files")


def test_read_mcp_sidecar_invalid_utf8_bytes_is_fail_open(tmp_path):
    from sherpa.providers.codex import usage as US
    path = tmp_path / ".mcp_sidecar.jsonl"
    path.write_bytes(b"\xff\n")
    reads, listed, ask, error_codes, limits = US._read_mcp_sidecar(path)
    assert reads == [] and listed == [] and ask is None and error_codes == []
    assert limits == {"tool_result_clipped": 0, "total_budget_hit": False, "duplicate_tool_call": 0,
                      "search_truncated": 0, "tool_calls_exhausted": False, "coverage_write_failed": 0}


# ===== 書込・失敗・分類 =====

def test_early_write_then_turn_failure_still_flags_codex_wrote_files(tmp_path, monkeypatch):
    """内部の前段階だけがファイルを書き、後段が turn.failed（agent_message 無し）で終わっても codex_wrote_files は立つ。"""
    body = _PY + "import pathlib\npathlib.Path('draft.md').write_text('前段の下書き')\n" + _emit(
        {"type": "turn.failed", "error": {"code": "boom"}})
    _, env = _run_fake(tmp_path, monkeypatch, body, "early-write-then-fail-u1", lens="author")
    assert env.get("codex_silent_failure") is True
    assert env.get("codex_wrote_files")


def test_context_window_exceeded_sets_budget_headline_and_stop_kind(tmp_path, monkeypatch, caplog):
    from sherpa import stop_kind
    body = _PY + _emit({"type": "turn.failed", "error": {
        "code": "boom", "codex_error_info": "context_window_exceeded",
        "message": "Error running remote compact task: Codex ran out of room"}})
    with caplog.at_level(logging.WARNING, logger="sherpa"):
        _, env = _run_fake(tmp_path, monkeypatch, body, "context-window-exceeded-u1")
    assert env.get("codex_silent_failure") is True
    assert env.get("codex_error_code") == "context_window_exceeded"
    assert "範囲（フォルダ）を絞る" in env["headline"]
    assert "認証が設定されていない" not in env["headline"]
    assert env.get("limits", {}).get("total_budget_hit") is True
    assert stop_kind.resolve(env) == "budget"
    assert any("codex turn failed" in r.message and "context_window_exceeded" in r.message for r in caplog.records)


def test_turn_failed_generic_code_keeps_legacy_headline(tmp_path, monkeypatch):
    from sherpa import stop_kind
    body = _PY + _emit({"type": "turn.failed", "error": {"code": "boom"}})
    _, env = _run_fake(tmp_path, monkeypatch, body, "turn-failed-generic-u1")
    assert env.get("codex_silent_failure") is True and env.get("codex_error_code") == "boom"
    assert "Codex に接続できませんでした" in env["headline"] and "codex.log" in env["headline"]
    assert not (env.get("limits") or {}).get("total_budget_hit")
    assert stop_kind.resolve(env) == "codex_silent"


def test_codex_log_end_line_has_no_stderr_or_message_content(tmp_path, monkeypatch, caplog):
    """codex.log の終了行には stderr も turn.failed の本文も載せない（固定語彙のコードと件数・所要時間だけ）。"""
    body = (_PY + "import sys\n"
            "sys.stderr.write('sk-abcdefghijklmnopqrstuvwx OPENAI_API_KEY=secretvalue123 設計/機密資料.xlsx\\n')\n"
            + _msg("消費税率は10%です。"))
    with caplog.at_level(logging.INFO, logger="sherpa.codex"):
        _run_fake(tmp_path, monkeypatch, body, "codex-log-noleak-u1")
    end = [r.message for r in caplog.records if r.name == "sherpa.codex" and "end conv=" in r.message]
    assert end
    for token in ("sk-abcdefghijklmnopqrstuvwx", "secretvalue123", "機密資料", "stderr"):
        assert token not in end[0]
    assert "error_code=" in end[0] and "elapsed=" in end[0]


def test_turn_failure_message_classified_without_being_stored():
    from sherpa.providers.codex import process as PR
    msg = ("Error running remote compact task: Codex ran out of room in the model's context window. "
           "設計/機密資料.xlsx")
    assert PR._classify_turn_failure(msg) == PR._CONTEXT_WINDOW_EXCEEDED_CODE
    assert PR._classify_turn_failure("some other failure") is None
    assert PR._classify_turn_failure(None) is None and PR._classify_turn_failure("") is None


def test_repeated_write_across_internal_stages_registers_final_version_once(tmp_path, monkeypatch):
    """同じファイルを内部の複数段階で書き直しても成果物登録は最終版 1 本だけ（要 Postgres・DB down は skip）。"""
    from sherpa import store
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    uid = f"unit-depth2s6-{int(time.time() * 1000) % 100000000}"
    store.upsert_user(uid, display_name="D2S6", password_hash="x", status="active")
    body = (_PY + "import pathlib\npathlib.Path('report.md').write_text('下書き')\n"
            "pathlib.Path('report.md').write_text('最終版')\n" + _msg("最終版を作成しました。"))
    _, env = _run_fake(tmp_path, monkeypatch, body, uid, lens="author")
    assert env.get("codex_wrote_files")
    created = env.get("created_files") or []
    assert len(created) == 1 and created[0]["name"] == "report.md"


def test_non_authoring_turn_does_not_keep_files_codex_created(tmp_path, monkeypatch):
    body = _PY + "import pathlib\npathlib.Path('registrymodifications.xcu').write_text('x')\n" + _msg("回答です。")
    _, env = _run_fake(tmp_path, monkeypatch, body, "non-author-files-u1")
    assert not env.get("codex_wrote_files") and not env.get("created_files")


def test_author_lens_keeps_its_own_reasoning_under_quick(tmp_path, monkeypatch):
    """作成系は専用の推論設定（_REASONING_AUTHOR）を持つ別軸＝クイックでも 1 段下げない。"""
    body = (_PY + "import sys, pathlib\npathlib.Path('argv.txt').write_text(' '.join(sys.argv))\n"
            + _msg("資料を作成しました。"))
    _install_codex(tmp_path, monkeypatch, body)
    ctx = _ctx("author-quick-u1")
    ctx = dataclasses.replace(
        ctx, scope_meta={**(ctx.scope_meta or {}), "depth_profile": "quick"},
        route=lambda msg: {"lens": "author", "input": msg, "reason": "test", "confident": True})
    list(A.CodexProvider().run(ctx))
    assert "model_reasoning_effort=medium" in next(tmp_path.rglob("argv.txt")).read_text()
