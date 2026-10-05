"""GNU のコマンド（timeout・flock・setsid・sha256sum・getent）が無い環境（macOS の標準構成）の再現。"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import tarfile
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
PY = sys.executable
TOOLS = ROOT / "scripts" / "lib" / "portable_tools.py"
GNU_ONLY = {"timeout", "flock", "setsid", "sha256sum", "getent"}


@pytest.fixture(scope="module")
def nognu_path(tmp_path_factory) -> str:
    """GNU_ONLY を除いた /usr/bin・/bin の写し（symlink）だけを PATH にする。"""
    bindir = tmp_path_factory.mktemp("nognu")
    for d in ("/usr/bin", "/bin"):
        for p in pathlib.Path(d).glob("*"):
            if p.name not in GNU_ONLY and not (bindir / p.name).exists():
                (bindir / p.name).symlink_to(p)
    return str(bindir)


def _bash(script: str, path: str, timeout: float = 60, **kw) -> subprocess.CompletedProcess:
    env = {**os.environ, "PATH": path}
    return subprocess.run(["bash", "-c", script], cwd=ROOT, env=env, capture_output=True,
                          text=True, timeout=timeout, **kw)


def test_portable_tools_match_gnu_formats(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n")
    out = subprocess.run([PY, str(TOOLS), "sha256", "--basename", str(f)],
                         capture_output=True, text=True, check=True).stdout
    assert out == f"{hashlib.sha256(b'hello\n').hexdigest()}  a.txt\n"
    tar = tmp_path / "x.tar"
    with tarfile.open(tar, "w") as tf:
        tf.addfile(tarfile.TarInfo("sherpa-v1/README"))
    subprocess.run([PY, str(TOOLS), "tar-append", str(tar), "sherpa-v1", str(f)], check=True)
    with tarfile.open(tar) as tf:
        assert tf.getnames() == ["sherpa-v1/README", "sherpa-v1/a.txt"]
    r = subprocess.run([PY, str(TOOLS), "run-limited", "1", "sleep", "5"])
    assert r.returncode == 124


def test_gate_lock_slot_and_group_timeout_without_gnu_tools(nognu_path, tmp_path):
    """flock・setsid・timeout 抜きで、レーン別ロックの排他・スロット上限 2・タイムアウト・ プロセスグループの後始末が従来どおりになる。"""
    lock = tmp_path / "named.lock"
    slow = tmp_path / "test_slow.py"
    slow.write_text("import time\ndef test_slow():\n    time.sleep(30)\n")
    base = (f"set -u; cd {ROOT}; . scripts/lib/gate_common.sh; PY={PY}\n"
            f"GATE_LANE_SLOT_LOCKS=({tmp_path}/s1.lock {tmp_path}/s2.lock)\n"
            f"GATE_ADMISSION_LOCKFILE={tmp_path}/adm.lock; GATE_LANE_POLL_SECONDS=1\n")
    # 1 本目のレーン別ロックを持つ側（親が消えるまで保持）
    holder = subprocess.Popen(
        ["bash", "-c", base + f"gate_acquire_named_lock {lock} || exit 1; echo got; sleep 20"],
        cwd=ROOT, env={**os.environ, "PATH": nognu_path}, stdout=subprocess.PIPE, text=True)
    try:
        assert holder.stdout.readline().strip() == "got"
        # 同名ロックは取れない（別プロセスの nowait 取得は失敗＝待たされる）
        r = _bash(base + f"_gate_lock_take {lock} nowait 200", nognu_path)
        assert r.returncode == 1
        # スロット 2 本を順に確保でき（1・2 番目）、埋まった後は 2 本とも取れない
        r = _bash(base + "gate_acquire_lane_slot; echo first-$GATE_SLOT_INDEX\n"
                  f"(gate_acquire_lane_slot; echo second-$GATE_SLOT_INDEX > {tmp_path}/second; sleep 5) "
                  ">/dev/null 2>&1 &\n"
                  f"for _ in 1 2 3 4 5 6 7 8 9 10; do [ -s {tmp_path}/second ] && break; sleep 0.5; done\n"
                  f"cat {tmp_path}/second\n"
                  f"_gate_lock_take {tmp_path}/s1.lock nowait 205 && echo s1-free || echo s1-busy\n"
                  f"_gate_lock_take {tmp_path}/s2.lock nowait 206 && echo s2-free || echo s2-busy\n"
                  "kill $! 2>/dev/null", nognu_path)
        assert "first-1" in r.stdout and "second-2" in r.stdout, r.stdout + r.stderr
        assert "s1-busy" in r.stdout and "s2-busy" in r.stdout, r.stdout
    finally:
        holder.kill()
        holder.wait()
    # 時間制限: 1 秒で打ち切られ（124）、テスト子のプロセスグループが残らない
    t0 = time.time()
    r = _bash(base + f"GROUP_TIMEOUT=1s; GATE_LOG_FILE={tmp_path}/g.log\n"
              f"gate_run_group slow {slow}; echo rc=$?", nognu_path)
    assert "rc=124" in r.stdout, r.stdout + r.stderr
    assert time.time() - t0 < 20


def test_prod_check_endpoint_checks_without_getent_and_timeout(nognu_path, tmp_path):
    """getent・timeout が無くても、名前解決・TCP 疎通・接続先の判定を黙って抜かさない。"""
    root = tmp_path / "app"
    (root / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/check-production.sh", "scripts/run-common.sh", "scripts/lib/portable_tools.py"):
        shutil.copy(ROOT / rel, root / rel)
    (root / "scripts" / "check-ports.sh").write_text("#!/bin/sh\nexit 0\n")
    (root / "scripts" / "check-ports.sh").chmod(0o755)
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(1)
    port = srv.getsockname()[1]

    def run(p: int) -> str:
        (root / "scripts" / "check_production_openai_probe.py").write_text(
            f"print('NO_MARKER'); print('ENV_CANDIDATE_OK'); print('custom'); print('https')\n"
            f"print('localhost'); print('{p}')\n")
        env_file = tmp_path / "env"
        env_file.write_text("SHERPA_ENV=production\n")
        env = {**os.environ, "PATH": nognu_path, "SHERPA_ENV_FILE": str(env_file), "PYTHON_BIN": PY}
        r = subprocess.run(["bash", str(root / "scripts" / "check-production.sh")], cwd=root, env=env,
                           capture_output=True, text=True, timeout=60)
        return r.stdout + r.stderr

    try:
        out = run(port)
        assert "接続先の検査モード: env 候補" in out, out
        assert "host resolves: localhost" in out, out
        assert f"reachable: localhost:{port}" in out, out
        assert "確認できませんでした" not in out, out
    finally:
        srv.close()
    out = run(port)   # 閉じた後は接続できない＝警告（黙って OK にしない）
    assert f"localhost:{port} に TCP 接続できません" in out, out


def test_run_group_reaps_grandchild_that_ignores_sigint(tmp_path):
    """時間切れで孫まで止まり（孤児を残さない）、SIGINT を無視する子も TERM/KILL で回収されて 124 になる。"""
    pidfile = tmp_path / "grandchild.pid"
    child = tmp_path / "child.py"
    child.write_text(
        "import signal, subprocess, sys, time\n"
        "signal.signal(signal.SIGINT, signal.SIG_IGN)\n"
        f"p = subprocess.Popen(['sleep', '300'])\nopen({str(pidfile)!r}, 'w').write(str(p.pid))\n"
        "time.sleep(300)\n")
    r = subprocess.run([PY, str(TOOLS), "run-group", "1s", PY, str(child)], capture_output=True,
                       timeout=60, env={**os.environ, "SHERPA_GATE_KILL_GRACE": "1"})
    assert r.returncode == 124
    pid = int(pidfile.read_text())
    time.sleep(0.5)
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_check_ports_resolves_names_without_getent(nognu_path):
    """getent が無くても名前解決を検査する（解決できない名前は黙って通さず非 0）。"""
    script = (f"set -u; ROOT={ROOT}; PYTHON_BIN={PY}\n"
              f". <(sed -n '/^resolve_host() {{/,/^}}/p' {ROOT}/scripts/check-ports.sh)\n"
              "resolve_host localhost && echo ok-localhost\n"
              "resolve_host no-such-host.invalid || echo ng-invalid\n")
    r = _bash(script, nognu_path)
    assert "ok-localhost" in r.stdout and "ng-invalid" in r.stdout, r.stdout + r.stderr
