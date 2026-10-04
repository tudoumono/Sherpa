"""`./sherpactl`（運用の入口）: 一覧・未知の道具・logs -h・make 別名の配線。"""
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CTL = str(ROOT / "sherpactl")

pytestmark = pytest.mark.unit


def _run(*args):
    return subprocess.run([CTL, *args], capture_output=True, text=True, cwd=ROOT, timeout=60)


def test_help_lists_tools():
    for a in ([], ["-h"], ["help"]):
        r = _run(*a)
        assert r.returncode == 0
        for tool in ("logs", "trace", "diag", "doctor"):
            assert f"./sherpactl {tool}" in r.stdout


def test_unknown_tool_fails_with_list():
    r = _run("nosuchtool")
    assert r.returncode != 0
    assert "./sherpactl logs" in r.stderr


def test_logs_help_prints_usage():
    r = _run("logs", "-h")
    assert r.returncode == 0
    assert "使い方" in r.stdout


def test_logs_dash_f_unsupported_and_trace_rejects_bad_conv():
    assert _run("logs", "-f").returncode != 0
    assert _run("trace", "12;x").returncode != 0


def test_make_aliases_call_sherpactl():
    out = subprocess.run(["make", "-n", "logs", "convert"], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert "./sherpactl logs" in out.stdout
    out = subprocess.run(["make", "-n", "trace", "CONV=1,2", "MASK=1"], capture_output=True, text=True, cwd=ROOT, timeout=60)
    assert "./sherpactl trace" in out.stdout
