"""`scripts/claude-hooks/deny-secrets.sh`（Claude Code PreToolUse フック）の単体テスト。

過去セッションで Azure API キーを端末に印字した実害の再発防止として、拒否/許可の境界を固定する
（CLAUDE.md「### セキュリティ」節・docs/20-開発ハーネス.md）。フックへ渡る JSON（`tool_input.command`）
を stdin から流し込み、拒否は exit 2、許可は exit 0 を検証する。
"""
from __future__ import annotations

import json
import pathlib
import subprocess

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "claude-hooks" / "deny-secrets.sh"


def _run(command: str) -> subprocess.CompletedProcess:
    payload = json.dumps({"tool_input": {"command": command}})
    return subprocess.run(
        ["bash", str(HOOK)],
        input=payload,
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=30,
    )


# ===== 拒否（機密の端末印字） =====

def test_denies_cat_env():
    r = _run("cat .env")
    assert r.returncode == 2
    assert ".env" in r.stderr


def test_denies_printenv():
    r = _run("printenv")
    assert r.returncode == 2


def test_denies_echo_secret_var():
    r = _run("echo $DATABASE_PASSWORD")
    assert r.returncode == 2


def test_denies_source_env():
    r = _run("source .env")
    assert r.returncode == 2


def test_denies_bare_env():
    r = _run("env")
    assert r.returncode == 2


# ===== 許可（誤検知にしない） =====

def test_allows_ls():
    r = _run("ls -la")
    assert r.returncode == 0


def test_allows_wc_on_env():
    r = _run("wc -l .env")
    assert r.returncode == 0


def test_allows_grep_count_only():
    r = _run("grep -c ERROR .env")
    assert r.returncode == 0


def test_allows_env_example():
    r = _run("cat .env.example")
    assert r.returncode == 0


def test_allows_env_with_args():
    r = _run("env FOO=bar python3 script.py")
    assert r.returncode == 0
