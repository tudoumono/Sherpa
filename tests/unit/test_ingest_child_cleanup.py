"""取り込みワーカーが SIGKILL で消えても、先読みの子・soffice が残らない（実プロセスで確かめる）。"""
from __future__ import annotations

import os
import signal
import subprocess
import sys
import time
from pathlib import Path

_REPO = Path(__file__).resolve().parents[2]

_FAKE_WORKER = r'''
import os, sys, time
from sherpa.ingest import md_prefetch
jobs = [md_prefetch.Job(rel=f"a{i}.pdf", path=sys.argv[1], arm_name="pdf_text", ext=".pdf") for i in range(2)]
pf = md_prefetch.Prefetcher(jobs, workers=2)
pf.take("a0.pdf", sys.argv[1])
pids = sorted(pf._pool._processes)
print(" ".join(map(str, pids)), flush=True)
time.sleep(300)
'''


def _alive(pid: int) -> bool:
    out = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True).stdout.strip()
    return bool(out) and not out.startswith("Z")


def _wait_gone(pids, limit: float) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if not any(_alive(p) for p in pids):
            return True
        time.sleep(0.2)
    return False


def test_prefetch_children_exit_when_parent_is_sigkilled(tmp_path):
    env = {**os.environ, "SHERPA_INGEST_CHILD_DIR": str(tmp_path / "children"), "PYTHONPATH": str(_REPO)}
    src = tmp_path / "x.pdf"
    src.write_bytes(b"%PDF-1.4\n")
    parent = subprocess.Popen([sys.executable, "-c", _FAKE_WORKER, str(src)], cwd=_REPO, env=env,
                              stdout=subprocess.PIPE, text=True)
    kids: list[int] = []
    try:
        kids = [int(x) for x in parent.stdout.readline().split()]
        assert kids and all(_alive(p) for p in kids)
        os.kill(parent.pid, signal.SIGKILL)
        parent.wait()
        assert _wait_gone(kids, 20), "親が消えても先読みの子が残っている"
    finally:
        for p in kids:
            if _alive(p):
                os.kill(p, signal.SIGKILL)
        if parent.poll() is None:
            parent.kill()


def _reap(run_dir: Path) -> str:
    out = subprocess.run(
        ["bash", "-c", f'. "{_REPO}/scripts/run-common.sh"; sherpa_reap_ingest_children'],
        env={**os.environ, "RUN_DIR": str(run_dir)}, capture_output=True, text=True, check=True).stdout
    return out.strip()


def _dead_pid() -> int:
    p = subprocess.Popen(["true"])
    p.wait()
    return p.pid


def test_reap_kills_orphan_group_but_not_a_reused_pid(tmp_path):
    from sherpa.ingest import child_registry

    run = tmp_path / "run"
    os.environ["SHERPA_INGEST_CHILD_DIR"] = str(run / "ingest_children")
    orphan = subprocess.Popen(["sleep", "300"], start_new_session=True)
    bystander = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        child_registry.register(orphan.pid, mode="group", owner=_dead_pid())
        child_registry.register(bystander.pid, mode="group", owner=_dead_pid())
        f = run / "ingest_children" / str(bystander.pid)
        f.write_text(f.read_text().replace("lstart=", "lstart=Thu Jan  1 00:00:00 1970 #"), encoding="utf-8")   # pid が別物に再利用された状態
        assert _reap(run) == "1"
        assert orphan.wait(timeout=10) == -signal.SIGKILL
        assert bystander.poll() is None
        assert not list((run / "ingest_children").iterdir())
    finally:
        os.environ.pop("SHERPA_INGEST_CHILD_DIR", None)
        for p in (orphan, bystander):
            if p.poll() is None:
                p.kill()
                p.wait()


def test_reap_leaves_children_of_a_living_owner(tmp_path):
    from sherpa.ingest import child_registry

    run = tmp_path / "run"
    os.environ["SHERPA_INGEST_CHILD_DIR"] = str(run / "ingest_children")
    kid = subprocess.Popen(["sleep", "300"], start_new_session=True)
    try:
        child_registry.register(kid.pid, mode="group", owner=os.getpid())
        assert _reap(run) == "0"
        assert kid.poll() is None
    finally:
        os.environ.pop("SHERPA_INGEST_CHILD_DIR", None)
        kid.kill()
        kid.wait()
