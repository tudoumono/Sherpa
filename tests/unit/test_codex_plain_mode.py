"""素の Codex モード（`codex_mode=plain`・docs/proposals/2026-09-24-素のCodexモード.md）の
統合検証。既存 `tests/unit/test_codex_no_presearch.py` と同じ「偽 codex 実行ファイルを PATH に
差し込む」流儀（実 codex は一切呼ばない）に、`tests/unit/test_codex_ledger_gate.py` の
「偽 codex が自分の `CODEX_HOME`（config.toml）を読んで検証結果をファイルへ書く」手法を組み合わせる
——AGENTS.md／config.toml は run_dir/codex_home の後始末で消えるため、外側の pytest からではなく
偽 codex プロセス自身が実行中に読んでログへ控える。

plain のターンで無効になるべきもの（出力スキーマ・multi_agent・台帳・investigate 誘導・MCP の
grep/読取ツール一式）が実際に効かないこと、資料の場所（変換済みテキスト）を案内する短いプロンプルに
差し替わること、平文の回答がそのまま headline になることを1ターンでまとめて確認する。
"""
from __future__ import annotations

import os
import stat
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import agents as A  # noqa: E402

_FAKE_CODEX_PLAIN_PY = r'''#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import tomllib

argv_log = pathlib.Path(r"__ARGV_LOG__")
agents_md_log = pathlib.Path(r"__AGENTS_MD_LOG__")
toolset_log = pathlib.Path(r"__TOOLSET_LOG__")

args = sys.argv[1:]
argv_log.write_text(repr(args), encoding="utf-8")

run_dir = pathlib.Path(args[args.index("-C") + 1])
agents_md = run_dir / "AGENTS.md"
agents_md_log.write_text(
    agents_md.read_text(encoding="utf-8") if agents_md.exists() else "<missing>", encoding="utf-8")

config_path = pathlib.Path(os.environ["CODEX_HOME"]) / "config.toml"
if config_path.exists():
    config = tomllib.loads(config_path.read_text())
    toolset = config.get("mcp_servers", {}).get("sherpa", {}).get("env", {}).get("SHERPA_MCP_TOOLSET", "")
else:
    toolset = "<no-config>"
toolset_log.write_text(toolset, encoding="utf-8")

print(json.dumps({"type": "thread.started", "thread_id": "SID-PLAIN"}))
print(json.dumps({"type": "item.completed",
                  "item": {"id": "m0", "type": "agent_message", "text": "Codex の素の回答です。"}}))
print(json.dumps({"type": "turn.completed", "usage": {
    "input_tokens": 10, "cached_input_tokens": 0, "output_tokens": 5, "reasoning_output_tokens": 0}}))
'''


def _write_fake_codex(bin_dir: Path, argv_log: Path, agents_md_log: Path, toolset_log: Path) -> None:
    script = bin_dir / "codex"
    script.write_text(
        _FAKE_CODEX_PLAIN_PY.replace("__ARGV_LOG__", str(argv_log))
                            .replace("__AGENTS_MD_LOG__", str(agents_md_log))
                            .replace("__TOOLSET_LOG__", str(toolset_log)))
    mode = script.stat().st_mode
    script.chmod(mode | stat.S_IEXEC | stat.S_IXGRP | stat.S_IXOTH)


def _result_env(events: list) -> dict:
    results = [e for e in events if isinstance(e, dict) and e.get("type") == "_result"]
    assert len(results) == 1, f"_result が1件でない: {events!r}"
    return results[0]["env"]


def test_plain_mode_turn_skips_schema_and_multi_agent_and_uses_minimal_prompt(tmp_path, monkeypatch):
    """`codex_mode=plain` のターンは (a) argv に出力スキーマの指定と features.multi_agent=true が
    無い、(b) MCP サーバへの環境に SHERPA_MCP_TOOLSET=plain がある、(c) AGENTS.md に台帳・worker・
    investigate 誘導の段落が無い、(d) プロンプトに変換済みテキストの場所があり ripgrep_search の語が
    無い、(e) 平文の回答がそのまま headline になる。`SHERPA_CODEX_OUTPUT_SCHEMA` は明示的に
    セットしない——env の既定（2＝有効）に勝つことを確かめるため。"""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    argv_log = tmp_path / "argv.log"
    agents_md_log = tmp_path / "agents_md.log"
    toolset_log = tmp_path / "toolset.log"
    _write_fake_codex(bin_dir, argv_log, agents_md_log, toolset_log)

    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '')}")
    monkeypatch.setenv("SHERPA_USERS_DIR", str(tmp_path / "users"))
    monkeypatch.setenv("SHERPA_CODEX_REASONING", "medium")   # 基準値（クイックの standard なら low へ下がる）

    ctx = A.Ctx(
        message="消費税率の仕様を教えて",
        world="v1",
        route=lambda msg: {"lens": "qa", "input": msg, "reason": "test", "confident": True},
        dispatch=lambda lens_, inp: {
            "lens": lens_, "headline": "dispatch-headline", "summary": {"total": 0},
            "data": {}, "sources": []},
        knowledge=True, uid="plain-mode-u1", make_sources=lambda docs: [],
        scope_meta={"depth_profile": "quick"},   # plain では調べる深さが効かない（推論は基準値のまま）
    )
    prov = A.CodexProvider(system_settings={"codex_mode": "plain"})
    env = _result_env(list(prov.run(ctx)))

    # (e) 平文の回答がそのまま headline になる（非スキーマ経路 _pick_codex_headline）。
    assert env["headline"] == "Codex の素の回答です。"
    assert env.get("activity", {}).get("settings", {}).get("mode") == "plain"
    assert env["activity"]["settings"]["schema_level"] == 0
    assert env["activity"]["settings"]["multi_agent"] is False

    calls = [eval(line) for line in argv_log.read_text().splitlines() if line.strip()]
    assert len(calls) == 1, "自動継続などで2回以上 codex exec が呼ばれている"
    argv = calls[0]

    # (a) 出力スキーマの指定・multi_agent 有効化が argv に無い。
    assert "--output-schema" not in argv
    assert "features.multi_agent=true" not in argv
    assert "features.multi_agent=false" in argv
    assert "model_reasoning_effort=medium" in argv   # クイックでも 1 段下げない

    # (d) プロンプト（argv 末尾）に変換済みテキストの場所の案内があり、MCP ツール一覧の代表格
    # （ripgrep_search）の語が無い。
    prompt = argv[-1]
    assert "変換済みテキスト" in prompt
    assert "ripgrep_search" not in prompt
    assert "COBOL/JCL は" not in prompt   # 旧い配置（world 直下の md/・src/）の案内で場所を二重に示さない
    assert ".rag.md" in prompt and "rg -E sjis" in prompt   # 正本の変換テキストと Shift_JIS のソースの読み方
    assert "設計書と実装（ソース）の両方で確かめ" in prompt   # 両面で確かめ、食い違いは並べて書く

    # (b) MCP サーバへの環境に SHERPA_MCP_TOOLSET=plain がある（config.toml 経由・サンドボックス既定 ON）。
    assert toolset_log.read_text(encoding="utf-8") == "plain"

    # (c) AGENTS.md に台帳・worker・investigate 誘導の段落が無い（最小形）。
    agents_md = agents_md_log.read_text(encoding="utf-8")
    assert agents_md != "<missing>"
    assert "台帳" not in agents_md
    assert "worker" not in agents_md
    assert "investigate-" not in agents_md

    # 直読の準備（秘匿ファイル列挙）に失敗したら、素の Codex は資料を読む手段が無い＝Codex を起動せず正直に失敗する。
    from sherpa.providers.codex import provider as provider_mod

    def _enum_fails(*a, **k):
        raise RuntimeError("sensitive_enum_failed:test")

    monkeypatch.setattr(provider_mod, "_enumerate_sensitive", _enum_fails)
    env2 = _result_env(list(A.CodexProvider(system_settings={"codex_mode": "plain"}).run(ctx)))
    assert env2["agentic_failure"] == "error"
    assert "読み取りの準備ができませんでした" in env2["headline"]
    assert len([ln for ln in argv_log.read_text().splitlines() if ln.strip()]) == 1   # 2 回目は起動していない


def test_plain_headline_skips_follow_up_reply_and_tool_home_is_outside_deliverables(tmp_path):
    """実環境の 0.14.5 で起きた 2 つ: 回答の後に届いた通知への短い返事を回答として拾った／LibreOffice の
    プロファイル（HOME 配下）が成果物として登録された。"""
    from sherpa.providers.codex import provider as provider_mod
    from sherpa.providers.codex import sandbox as sandbox_mod
    answer = "区分は 1〜7 です。\n\n参照した資料:\n- a/b.doc"
    follow_up = "追加通知の内容は結論と整合していました。先ほどの回答内容は変更ありません。"
    assert provider_mod._pick_codex_headline([answer, follow_up], prefer_marker="参照した資料") == answer
    assert provider_mod._pick_codex_headline([answer, follow_up]) == follow_up   # standard は従来どおり

    run, tmp = tmp_path / "run", tmp_path / "run" / ".tmp"
    tmp.mkdir(parents=True)
    env = sandbox_mod._codex_clean_env(tmp_path / "home", run, tmp)
    assert Path(env["HOME"]).is_relative_to(tmp)   # 成果物の走査から外れる .tmp/ の下
