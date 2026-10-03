"""`lane` スキル（`.claude/skills/lane/`）単体テスト。"""
from __future__ import annotations

import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable
LANE_PROMPT = ROOT / ".claude" / "skills" / "lane" / "scripts" / "lane_prompt.py"
LANE_STATUS = ROOT / ".claude" / "skills" / "lane" / "scripts" / "lane_status.sh"

# 公開 export には .claude が含まれない＝スキルの実体があるときだけ検査する。
pytestmark = pytest.mark.skipif(not LANE_PROMPT.is_file(), reason="公開 export に .claude は含まれない")

_BASE_ARGS = ["--lane", "sample", "--branch", "harness/sample", "--goal", "サンプル目的",
              "--files", "対象ファイルの説明", "--accept", "受け入れ条件", "--avoid", "やらないこと"]


def _run_prompt(args: list[str]) -> subprocess.CompletedProcess:
    return subprocess.run([PY, str(LANE_PROMPT), *args], cwd=ROOT, capture_output=True, text=True, timeout=30)


def test_prompt_is_deterministic_and_has_required_sections():
    r1, r2 = _run_prompt(_BASE_ARGS), _run_prompt(_BASE_ARGS)
    assert r1.returncode == 0 and r2.returncode == 0, r1.stderr
    assert r1.stdout == r2.stdout, "同じ引数で出力が変わった（決定的出力の契約違反）"
    for needle in ("git merge-base main HEAD", "git rev-parse main", "git reset --hard main", "harness/sample",
                   "報告形式", "変更ファイル一覧", "pytest の要約行", "終了コード", "SMOKE", "worktree のパスとコミット SHA",
                   "迷った点", "Co-Authored-By: Claude Fable 5.1 <noreply@anthropic.com>", "モックは外部境界だけ"):
        assert needle in r1.stdout, needle


def test_missing_required_arg_is_argparse_error():
    r = _run_prompt([a for a in _BASE_ARGS if a not in ("--goal", "サンプル目的")])
    assert r.returncode != 0 and "--goal" in r.stderr


def _git(repo, *args):
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("x", encoding="utf-8")
    _git(repo, "add", "a.txt")
    _git(repo, "commit", "-q", "-m", "init")
    return repo


def _status(repo):
    return subprocess.run(["bash", str(LANE_STATUS)], cwd=repo, capture_output=True, text=True, timeout=30)


def test_lane_status_emits_one_line_per_worktree_and_stays_read_only(tmp_path):
    assert subprocess.run(["bash", "-n", str(LANE_STATUS)], capture_output=True, text=True, timeout=10).returncode == 0
    repo = _init_repo(tmp_path)
    wt = tmp_path / "wt-sample"
    _git(repo, "worktree", "add", "-q", "-b", "topic/sample", str(wt), "main")
    r = _status(repo)
    assert r.returncode == 0, r.stderr
    assert "topic/sample" in r.stdout and str(wt) in r.stdout
    assert str(repo) + " |" not in r.stdout   # main 本体は表に出ない
    # 衝突なしレーン（ゲート待ち）で merge-tree が呼ばれても、本体のオブジェクトストアへ書き込まない
    (wt / "b.txt").write_text("y", encoding="utf-8")
    _git(wt, "add", "b.txt")
    _git(wt, "commit", "-q", "-m", "topic change")

    def loose() -> int:
        out = subprocess.run(["git", "count-objects", "-v"], cwd=repo, capture_output=True, text=True, check=True).stdout
        return int(next(ln for ln in out.splitlines() if ln.startswith("count:")).split(":")[1])
    before = loose()
    r = _status(repo)
    assert r.returncode == 0 and "ゲート待ち" in r.stdout, r.stdout
    assert loose() == before, "merge-tree の write-tree が本体オブジェクトストアを汚した"
