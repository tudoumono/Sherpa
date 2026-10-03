"""S2（開発ハーネス・RV 台帳＋`.claude/skills/rv/`）単体テスト。"""
from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable
RV_PROMPT = ROOT / ".claude" / "skills" / "rv" / "scripts" / "rv_prompt.py"
RV_CODEX = ROOT / ".claude" / "skills" / "rv" / "scripts" / "rv_codex.sh"
BASH = shutil.which("bash") or "/bin/bash"

# 公開 export には .claude が含まれない＝スキルの実体があるときだけ検査する（test_docs_gates の CLAUDE.md と同じ扱い）。
pytestmark = pytest.mark.skipif(not RV_PROMPT.is_file(), reason="公開 export に .claude は含まれない")


def _run_prompt(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(RV_PROMPT), *args], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)


_INTENT = ["--intent", "テスト用の意図要約"]


def test_prompt_with_ledger_includes_full_text_and_numbered_citation_rule(tmp_path):
    ledger = tmp_path / "ledger.md"
    ledger.write_text("| # | 巡 | 指摘の要約 | 分類 | 理由 | 是正コミット |\n|---|---|---|---|---|---|\n"
                      "| 1 | 1 | サンプル指摘テキスト | 採用 | サンプル理由 | abc1234 |\n", encoding="utf-8")
    r = _run_prompt(["--target", "HEAD", "--ledger", str(ledger), "--round", "2", *_INTENT])
    assert r.returncode == 0, r.stderr
    assert "サンプル指摘テキスト" in r.stdout and "サンプル理由" in r.stdout   # 台帳の全文を同梱
    assert "台帳 #" in r.stdout and "新しい事実" in r.stdout   # 番号付き再指摘・新事実なしの再指摘禁止


def test_prompt_default_output_contract():
    r = _run_prompt(["--target", "HEAD", *_INTENT])
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "台帳" not in out and "メインのチェックアウトは読まない" not in out   # 渡していなければ節が無い
    assert "[高/中/低] ファイル:行 — 症状 — 再現 — 最小修正" in out
    assert all(w in out for w in ("クローズ可", "要修正", "指摘なし", "完成形からの逸脱", "スコープ外の変更"))
    assert "`git show --diff-merges=first-parent HEAD`" in out and "`git diff HEAD`" not in out   # 単一対象は show
    args = ["--target", "HEAD", "--round", "3", *_INTENT, "--proposal", "some/proposal.md", "--focus", "確認してほしい点"]
    assert _run_prompt(args).stdout == _run_prompt(args).stdout   # 同じ引数なら同一出力


def test_prompt_range_target_and_worktree_guidance():
    out = _run_prompt(["--target", "abc123..def456", *_INTENT]).stdout
    assert "`git diff abc123..def456`" in out and "`git show abc123..def456`" not in out
    rel = "tmp/worktrees/rv-x"
    out = _run_prompt(["--target", "a1..b2", "--intent", "x", "--worktree", rel]).stdout
    absp = str((ROOT / rel).resolve())   # worktree は絶対パスに解決され、範囲なら diff を使う
    assert f"git -C {absp} diff a1..b2" in out and f"git -C {rel} " not in out and "メインのチェックアウトは読まない" in out


def test_intent_omitted_is_argparse_error():
    r = _run_prompt(["--target", "HEAD"])
    assert r.returncode != 0 and "--intent" in r.stderr


def test_rv_codex_sh_exits_2_when_codex_not_on_path_and_syntax_is_valid(tmp_path):
    r = subprocess.run([BASH, str(RV_CODEX), "-C", str(tmp_path), "-n", "rvtest", "ダミープロンプト"],
                       cwd=ROOT, capture_output=True, text=True, timeout=15, env={"PATH": "", "HOME": str(tmp_path)})
    assert r.returncode == 2, r.stdout + r.stderr
    assert "codex" in r.stderr.lower() and "adversarial-reviewer" in r.stderr
    assert subprocess.run([BASH, "-n", str(RV_CODEX)], capture_output=True, text=True, timeout=10).returncode == 0


# ===== rv_codex.sh: 前回実行の last.txt が残ったまま今回が失敗すると、古い結果を今回の結果として
# 誤読させてしまう（stale last.txt）。1 回目は成功して last.txt に書き、2 回目は何も書かず失敗する
# 偽 codex で再現する（実 codex は呼ばない）。 =====

_FAKE_CODEX_TWO_RUN = """#!/usr/bin/env bash
STATE="$FAKE_CODEX_STATE"
COUNT=$(( $(cat "$STATE" 2>/dev/null || echo 0) + 1 ))
echo "$COUNT" > "$STATE"

OUT=""
while [ "$#" -gt 0 ]; do
  if [ "$1" = "-o" ]; then
    OUT="$2"
  fi
  shift
done

if [ "$COUNT" -eq 1 ]; then
  echo "FIRST_RUN_RESULT" > "$OUT"
  exit 0
else
  exit 1
fi
"""


def test_rv_codex_sh_does_not_leak_stale_last_txt_on_second_failure(tmp_path):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    fake_codex = bin_dir / "codex"
    fake_codex.write_text(_FAKE_CODEX_TWO_RUN, encoding="utf-8")
    fake_codex.chmod(0o755)

    home = tmp_path / "home"
    home.mkdir()
    worktree = tmp_path / "wt"
    worktree.mkdir()
    state_file = tmp_path / "count"

    env = {
        "PATH": f"{bin_dir}{os.pathsep}/usr/bin:/bin",
        "HOME": str(home),
        "FAKE_CODEX_STATE": str(state_file),
    }

    r1 = subprocess.run(
        [BASH, str(RV_CODEX), "-C", str(worktree), "-n", "rvstale", "ダミープロンプト"],
        cwd=ROOT, capture_output=True, text=True, timeout=15, env=env,
    )
    assert r1.returncode == 0, r1.stdout + r1.stderr
    assert "FIRST_RUN_RESULT" in r1.stdout

    r2 = subprocess.run(
        [BASH, str(RV_CODEX), "-C", str(worktree), "-n", "rvstale", "ダミープロンプト"],
        cwd=ROOT, capture_output=True, text=True, timeout=15, env=env,
    )
    assert r2.returncode != 0, r2.stdout + r2.stderr
    assert "FIRST_RUN_RESULT" not in r2.stdout, "2回目失敗時に1回目の古い結果が出力に漏れている"
    assert "レビュー結果なし" in r2.stderr
