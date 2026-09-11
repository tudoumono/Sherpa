"""`lane` スキル（`.claude/skills/lane/`）単体テスト。

並列レーン開発の定型委譲文を組み立てる `lane_prompt.py`（`docs/20-開発ハーネス.md` §2・§5・§8を
実装）と、収束表を出す `lane_status.sh` を検査する。実 Agent・実 git worktree の大規模操作は
呼ばない（`lane_status.sh` の検査は一時 git リポジトリに worktree を1つ作るだけ）。
"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable
LANE_PROMPT = ROOT / ".claude" / "skills" / "lane" / "scripts" / "lane_prompt.py"
LANE_STATUS = ROOT / ".claude" / "skills" / "lane" / "scripts" / "lane_status.sh"

# 公開 export には .claude が含まれない＝スキルの実体があるときだけ検査する（test_rv_skill.py と同じ扱い）。
pytestmark = pytest.mark.skipif(not LANE_PROMPT.is_file(), reason="公開 export に .claude は含まれない")

_BASE_ARGS = [
    "--lane", "sample",
    "--branch", "harness/sample",
    "--goal", "サンプル目的",
    "--files", "対象ファイルの説明",
    "--accept", "受け入れ条件",
    "--avoid", "やらないこと",
]


def _run_prompt(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(LANE_PROMPT), *args], cwd=ROOT,
                           capture_output=True, text=True, timeout=30)


# ===== (a) 同じ引数なら同一出力（決定的） =====

def test_same_args_produce_identical_output_twice():
    r1 = _run_prompt(_BASE_ARGS)
    r2 = _run_prompt(_BASE_ARGS)
    assert r1.returncode == 0, r1.stderr
    assert r2.returncode == 0, r2.stderr
    assert r1.stdout == r2.stdout, "同じ引数で出力が変わった（決定的出力の契約違反）"


# ===== (b) 必須節: 基点確認・報告形式・SMOKE・Co-Authored-By =====

def test_output_contains_required_sections():
    r = _run_prompt(_BASE_ARGS)
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "git merge-base main HEAD" in out, "基点確認の手順が無い"
    assert "git rev-parse main" in out, "基点確認の手順が無い"
    assert "git reset --hard main" in out, "基点確認の不一致時の手順が無い"
    assert "harness/sample" in out, "作業ブランチ名が入っていない"
    assert "報告形式" in out
    assert "変更ファイル一覧" in out
    assert "pytest の要約行" in out
    assert "終了コード" in out
    assert "SMOKE" in out, "スモーク/サンプル実行の数値表記ルールが無い"
    assert "worktree のパスとコミット SHA" in out
    assert "迷った点" in out
    assert "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>" in out
    assert "モックは外部境界だけ" in out, "テスト規範（docs/20 §6）への参照が無い"


# ===== (c) 必須引数の欠落は argparse エラー =====

def test_missing_required_arg_is_argparse_error():
    args = [a for a in _BASE_ARGS if a not in ("--goal", "サンプル目的")]
    r = _run_prompt(args)
    assert r.returncode != 0, r.stdout
    assert "--goal" in r.stderr


# ===== (d) lane_status.sh: 構文チェック + 一時 git リポで worktree 1つ分の行が出る =====

def test_lane_status_sh_syntax_is_valid():
    r = subprocess.run(["bash", "-n", str(LANE_STATUS)], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr


def test_lane_status_sh_emits_one_line_for_a_worktree(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", "-b", "main"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.email", "t@example.com"], cwd=repo, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=repo, check=True)
    (repo / "a.txt").write_text("x", encoding="utf-8")
    subprocess.run(["git", "add", "a.txt"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)

    wt = tmp_path / "wt-sample"
    subprocess.run(["git", "worktree", "add", "-b", "topic/sample", str(wt), "main"],
                    cwd=repo, check=True, capture_output=True, text=True)

    r = subprocess.run(["bash", str(LANE_STATUS)], cwd=repo, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "topic/sample" in r.stdout, r.stdout
    assert str(wt) in r.stdout
    # main 本体（repo 自身）は表に出ない。
    assert str(repo) + " |" not in r.stdout
