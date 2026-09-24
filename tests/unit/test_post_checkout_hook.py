"""`scripts/git-hooks/post-checkout` の単体テスト。

Agent worktree（`.claude/worktrees/agent-*`）の基点を、新規 worktree 作成時（直前 HEAD が null SHA
になる `git worktree add`/`git clone`）だけローカル main へ揃える契約をスクラッチ git リポジトリで
固定する（CLAUDE.md「開発の型」節・docs/20-開発ハーネス.md）。通常の checkout・`checkout -b`・
祖先ブランチの checkout では発火しないことも併せて固定する。`core.hooksPath` を本物のフックへ向け、
git 自身に発火させて検証する（フックの引数を手で組み立てて疑似呼び出ししない）。
"""
from __future__ import annotations

import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK_DIR = ROOT / "scripts" / "git-hooks"


def _git(args, cwd) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        timeout=30,
    )


def _git_ok(args, cwd) -> str:
    r = _git(args, cwd)
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
    commit2 = _git_ok(["rev-parse", "HEAD"], repo)
    return repo, commit1, commit2


def _agent_worktree_path(tmp_path: pathlib.Path) -> pathlib.Path:
    # フックの発火条件（`case "$top" in */.claude/worktrees/agent-*)`）に一致させる。
    return tmp_path / ".claude" / "worktrees" / "agent-test"


def test_worktree_add_snaps_to_main(tmp_path, central_repo):
    repo, commit1, commit2 = central_repo
    wt = _agent_worktree_path(tmp_path)
    _git_ok(["worktree", "add", "--detach", str(wt), commit1], repo)
    assert _git_ok(["rev-parse", "HEAD"], wt) == commit2


def test_normal_checkout_does_not_fire(tmp_path, central_repo):
    repo, commit1, commit2 = central_repo
    wt = _agent_worktree_path(tmp_path)
    _git_ok(["worktree", "add", "--detach", str(wt), commit1], repo)
    assert _git_ok(["rev-parse", "HEAD"], wt) == commit2
    _git_ok(["checkout", commit1], wt)
    assert _git_ok(["rev-parse", "HEAD"], wt) == commit1


def test_checkout_b_does_not_fire(tmp_path, central_repo):
    repo, commit1, commit2 = central_repo
    wt = _agent_worktree_path(tmp_path)
    _git_ok(["worktree", "add", "--detach", str(wt), commit1], repo)
    _git_ok(["checkout", "-b", "feature", commit1], wt)
    assert _git_ok(["rev-parse", "HEAD"], wt) == commit1


def test_ancestor_branch_checkout_does_not_rewrite_ref(tmp_path, central_repo):
    repo, commit1, commit2 = central_repo
    _git_ok(["branch", "old-feature", commit1], repo)
    wt = _agent_worktree_path(tmp_path)
    _git_ok(["worktree", "add", "--detach", str(wt), commit1], repo)
    _git_ok(["checkout", "old-feature"], wt)
    assert _git_ok(["rev-parse", "old-feature"], repo) == commit1
