"""`scripts/git-hooks/post-checkout` の単体テスト。"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_DIR = ROOT / "scripts" / "git-hooks"


def _git_ok(args, cwd) -> str:
    r = subprocess.run(["git", *args], cwd=cwd, capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, f"git {args} failed: {r.stderr}"
    return r.stdout.strip()


@pytest.fixture
def central_repo(tmp_path):
    """`main` に2コミット持つスクラッチ central リポ。フックは本物（HOOK_DIR）を向ける。"""
    repo = tmp_path / "central"
    repo.mkdir()
    _git_ok(["init", "-q", "-b", "main"], repo)
    _git_ok(["config", "user.email", "test@example.com"], repo)
    _git_ok(["config", "user.name", "Test"], repo)
    _git_ok(["config", "core.hooksPath", str(HOOK_DIR)], repo)
    (repo / "a.txt").write_text("1\n")
    _git_ok(["add", "a.txt"], repo)
    _git_ok(["commit", "-q", "-m", "c1"], repo)
    commit1 = _git_ok(["rev-parse", "HEAD"], repo)
    (repo / "a.txt").write_text("2\n")
    _git_ok(["commit", "-q", "-am", "c2"], repo)
    return repo, commit1, _git_ok(["rev-parse", "HEAD"], repo)


def test_worktree_add_snaps_to_main_but_later_checkouts_do_not_fire(tmp_path, central_repo):
    repo, commit1, commit2 = central_repo
    _git_ok(["branch", "old-feature", commit1], repo)
    # フックの発火条件（`*/.claude/worktrees/agent-*`）に一致する worktree
    wt = tmp_path / ".claude" / "worktrees" / "agent-test"
    _git_ok(["worktree", "add", "--detach", str(wt), commit1], repo)
    assert _git_ok(["rev-parse", "HEAD"], wt) == commit2   # worktree 作成直後は main へ揃える
    _git_ok(["checkout", commit1], wt)
    assert _git_ok(["rev-parse", "HEAD"], wt) == commit1   # 通常の checkout では発火しない
    _git_ok(["checkout", "-b", "feature", commit1], wt)
    assert _git_ok(["rev-parse", "HEAD"], wt) == commit1   # checkout -b でも発火しない
    _git_ok(["checkout", "old-feature"], wt)
    assert _git_ok(["rev-parse", "old-feature"], repo) == commit1   # 祖先ブランチの ref を書き換えない
