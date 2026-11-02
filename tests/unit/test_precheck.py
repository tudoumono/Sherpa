"""導入前の事前確認（precheck.sh）: 読み取り専用・sudo を呼ばない・まとめの行と終了コード。

外へ出る通信は切る（推論の接続先は閉じたローカルポート、macOS のオンライン先は環境変数で省く）。
"""
from __future__ import annotations

import os
import re
import socket
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "precheck.sh"
SUMMARY = re.compile(r"=== 結果: 自分で用意するもの (\d+) 件・注意 (\d+) 件 ===")
PROXY_VARS = ("http_proxy", "https_proxy", "all_proxy", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY")


def _closed_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _run(shell: str, workdir: Path, path_prefix: Path | None = None) -> subprocess.CompletedProcess:
    env = {k: v for k, v in os.environ.items() if k not in PROXY_VARS}
    env["SHERPA_PRECHECK_SKIP_ONLINE"] = "1"
    if path_prefix is not None:
        env["PATH"] = f"{path_prefix}{os.pathsep}{env['PATH']}"
    return subprocess.run([shell, str(SCRIPT), "--dir", str(workdir), "--api-url", f"http://127.0.0.1:{_closed_port()}/"],
                          env=env, capture_output=True, text=True, timeout=120)


def _assert_summary_and_exit_code(r: subprocess.CompletedProcess) -> None:
    m = SUMMARY.search(r.stdout)
    assert m, r.stdout + r.stderr
    assert r.returncode == (1 if int(m.group(1)) else 0), r.stdout + r.stderr


def test_precheck_prints_summary_and_exit_code_without_writing_anything(tmp_path: Path):
    r = _run("bash", tmp_path)
    _assert_summary_and_exit_code(r)
    assert list(tmp_path.iterdir()) == []


def test_precheck_never_calls_sudo(tmp_path: Path):
    fake = tmp_path / "fakebin"
    fake.mkdir()
    mark = tmp_path / "sudo-called"
    sudo = fake / "sudo"
    sudo.write_text(f'#!/bin/sh\necho "$@" >> "{mark}"\nexit 1\n', encoding="utf-8")
    sudo.chmod(0o755)
    work = tmp_path / "work"
    work.mkdir()
    r = _run("bash", work, path_prefix=fake)
    _assert_summary_and_exit_code(r)
    assert not mark.exists()


@pytest.mark.skipif(sys.platform != "darwin" or not Path("/bin/bash").exists(), reason="macOS 標準の bash 3.2 での確認")
def test_precheck_runs_with_macos_system_bash(tmp_path: Path):
    r = _run("/bin/bash", tmp_path)
    _assert_summary_and_exit_code(r)
    assert list(tmp_path.iterdir()) == []

