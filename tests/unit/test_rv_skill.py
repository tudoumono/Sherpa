"""S2（開発ハーネス・RV 台帳＋`.claude/skills/rv/`）単体テスト。

`docs/20-開発ハーネス.md` §3（敵対レビュー）・§4（RV 台帳）を実装した `rv_prompt.py`（定型
プロンプトの決定的な組み立て・台帳同梱時の番号付き再指摘の定型文・出力形式の固定）と `rv_codex.sh`
（`codex` CLI が無い環境での fail-closed）を検査する。実 codex CLI は呼ばない。
"""
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


# ===== (a) 台帳を渡すと全文と番号付き再指摘の定型文が含まれる =====

def test_ledger_full_text_and_numbered_citation_instruction_included(tmp_path):
    ledger = tmp_path / "ledger.md"
    ledger.write_text(
        "| # | 巡 | 指摘の要約 | 分類 | 理由 | 是正コミット |\n"
        "|---|---|---|---|---|---|\n"
        "| 1 | 1 | サンプル指摘テキスト | 採用 | サンプル理由 | abc1234 |\n",
        encoding="utf-8",
    )
    r = _run_prompt(["--target", "HEAD", "--ledger", str(ledger), "--round", "2",
                      "--intent", "テスト用の意図要約"])
    assert r.returncode == 0, r.stderr
    assert "サンプル指摘テキスト" in r.stdout, "台帳の全文が同梱されていない"
    assert "サンプル理由" in r.stdout
    assert "台帳 #" in r.stdout, "番号付き再指摘の定型文が無い"
    assert "新しい事実" in r.stdout, "『新しい事実がない限り再指摘しない』の定型文が無い"


# ===== (b) 台帳を渡さないと台帳節が無い =====

def test_no_ledger_means_no_ledger_section():
    r = _run_prompt(["--target", "HEAD", "--intent", "テスト用の意図要約"])
    assert r.returncode == 0, r.stderr
    assert "台帳" not in r.stdout, "台帳を渡していないのに台帳への言及が出力されている"


# ===== (c) 出力形式の行と末尾の可否・指摘なし表記 =====

def test_output_format_line_and_verdict_and_no_findings_wording_present():
    r = _run_prompt(["--target", "HEAD", "--intent", "テスト用の意図要約"])
    assert r.returncode == 0, r.stderr
    assert "[高/中/低] ファイル:行 — 症状 — 再現 — 最小修正" in r.stdout
    assert "クローズ可" in r.stdout
    assert "要修正" in r.stdout
    assert "指摘なし" in r.stdout


# ===== (d) 同じ引数なら同一出力（決定的） =====

def test_same_args_produce_identical_output_twice():
    args = ["--target", "HEAD", "--round", "3", "--intent", "テスト用の意図要約",
            "--proposal", "some/proposal.md", "--focus", "確認してほしい点"]
    r1 = _run_prompt(args)
    r2 = _run_prompt(args)
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert r1.stdout == r2.stdout, "同じ引数で出力が変わった（決定的出力の契約違反）"


# ===== (e) --target が単一 SHA/HEAD なら git show、範囲（".." を含む）なら git diff を案内する =====

def test_single_target_guides_git_show_not_git_diff():
    r = _run_prompt(["--target", "HEAD", "--intent", "テスト用の意図要約"])
    assert r.returncode == 0, r.stderr
    assert "`git show --diff-merges=first-parent HEAD`" in r.stdout
    assert "`git diff HEAD`" not in r.stdout


def test_range_target_guides_git_diff_not_git_show():
    r = _run_prompt(["--target", "abc123..def456", "--intent", "テスト用の意図要約"])
    assert r.returncode == 0, r.stderr
    assert "`git diff abc123..def456`" in r.stdout
    assert "`git show abc123..def456`" not in r.stdout


# ===== (f) --intent は必須（省略すると argparse エラー） =====

def test_intent_omitted_is_argparse_error():
    r = _run_prompt(["--target", "HEAD"])
    assert r.returncode != 0, r.stdout
    assert "--intent" in r.stderr


# ===== (g) 意図の要約に「完成形からの逸脱」も指摘対象という文言が入る =====

def test_intent_section_mentions_deviation_from_finished_shape():
    r = _run_prompt(["--target", "HEAD", "--intent", "テスト用の意図要約"])
    assert r.returncode == 0, r.stderr
    assert "完成形からの逸脱" in r.stdout
    assert "スコープ外の変更" in r.stdout


# ===== (h) --worktree 指定時は対象節に worktree での確認方法が出る =====

def test_worktree_option_adds_isolated_worktree_guidance():
    r = _run_prompt(["--target", "HEAD", "--intent", "テスト用の意図要約",
                      "--worktree", "tmp/worktrees/rv-sample"])
    assert r.returncode == 0, r.stderr
    assert "tmp/worktrees/rv-sample" in r.stdout
    assert "メインのチェックアウトは読まない" in r.stdout


def test_no_worktree_option_means_no_worktree_guidance():
    r = _run_prompt(["--target", "HEAD", "--intent", "テスト用の意図要約"])
    assert r.returncode == 0, r.stderr
    assert "メインのチェックアウトは読まない" not in r.stdout


# ===== rv_codex.sh: codex 不在時は exit 2（実 codex は呼ばない） =====

def test_rv_codex_sh_exits_2_when_codex_not_on_path(tmp_path):
    env = {"PATH": "", "HOME": str(tmp_path)}
    r = subprocess.run(
        [BASH, str(RV_CODEX), "-C", str(tmp_path), "-n", "rvtest", "ダミープロンプト"],
        cwd=ROOT, capture_output=True, text=True, timeout=15, env=env,
    )
    assert r.returncode == 2, r.stdout + r.stderr
    assert "codex" in r.stderr.lower(), r.stderr
    assert "adversarial-reviewer" in r.stderr, r.stderr


def test_rv_codex_sh_syntax_is_valid():
    r = subprocess.run([BASH, "-n", str(RV_CODEX)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


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


def test_single_target_uses_first_parent_diff_for_merge_commits():
    """単一対象の案内はマージコミットでも差分が空にならない `git show --diff-merges=first-parent`。"""
    out = _run_prompt(["--target", "abc1234", "--intent", "x"]).stdout
    assert "git show --diff-merges=first-parent abc1234" in out


def test_worktree_guidance_follows_diff_command_and_is_absolute(tmp_path):
    """worktree 案内は範囲なら diff・単一なら show を使い、パスは絶対パスに解決される。"""
    rel = "tmp/worktrees/rv-x"
    out = _run_prompt(["--target", "a1..b2", "--intent", "x", "--worktree", rel]).stdout
    absp = str((ROOT / rel).resolve())
    assert f"git -C {absp} diff a1..b2" in out
    assert f"git -C {rel} " not in out

