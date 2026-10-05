"""`scripts/claude-hooks/deny-secrets.sh`（Claude Code PreToolUse フック）の単体テスト。

機密を端末へ出す形は拒否（exit 2）し、件数・接頭辞・長さの確認などの正当な操作は許可（exit 0）する。
"""
from __future__ import annotations

import json
import pathlib
import subprocess

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
HOOK = ROOT / "scripts" / "claude-hooks" / "deny-secrets.sh"


def _run(command: str) -> subprocess.CompletedProcess:
    return subprocess.run(["bash", str(HOOK)], input=json.dumps({"tool_input": {"command": command}}),
                          cwd=ROOT, capture_output=True, text=True, timeout=30)


DENY = [
    "cat .env", "printenv", "echo $DATABASE_PASSWORD", "source .env", "env", "cat .env*",
    'echo "$(cat .env)"', "timeout 5 cat .env", "nl .env", 'python3 -c "print(open(\'.env\').read())"',
    "sed -n '1p' .env",   # -i の無い sed は中身の表示
    "cut -d= -f2- .env", "cut -c1-200 .env",   # フィールド抽出や広い範囲は中身の表示
    'echo "${OPENAI_API_KEY:0:200}"',   # 接頭辞確認の範囲（8 文字）を超える部分展開
    "echo ${OPENAI_API_KEY:8:8}${OPENAI_API_KEY:16:8}",   # offset 付きは連結して全体を出せる
    "cp .env /dev/stdout", "mv .env /dev/tty", "cp .env /proc/self/fd/1",
    "cat <.env", "echo $(<.env)", "tr -d x <.env",
    "ls $(cat .env)", "wc -c $(cat .env)", "cut -c1-8 $(cat .env)", "grep -c x $(cat .env)",   # allowlist でも置換で中身が出る形
    "grep KEY <.env", "grep KEY .env*",
    'sed -i s/a/b/ "$(cat .env)"',
]
ALLOW = [
    "ls -la", "wc -l .env", "grep -c ERROR .env", "grep -ic KEY .env", "cat .env.example",
    "env FOO=bar python3 script.py", "cp .env.example .env", "sed -i s/a/b/ .env", "sed -i s/a/b/ notes.txt",
    'echo "${OPENAI_API_KEY:0:4}"', 'echo "${OPENAI_API_KEY:0:8}"', "echo ${#OPENAI_API_KEY}", "cut -c1-4 .env",
    'make azure-smoke ARGS="--env-file azure.env --yes"', "cat foo.envelope",   # 別名の env ファイルは .env 系ではない
    "cp .env .env.bak", "cp .env /tmp/x", "ls $(pwd)", "grep -r KEY .",
]


@pytest.mark.parametrize("command", DENY)
def test_denies_printing_secrets(command):
    assert _run(command).returncode == 2, command


@pytest.mark.parametrize("command", ALLOW)
def test_allows_legitimate_operations(command):
    r = _run(command)
    assert r.returncode == 0, command
    assert "SyntaxWarning" not in (r.stderr or "")


def test_denial_names_the_env_file():
    assert ".env" in _run("cat .env").stderr
