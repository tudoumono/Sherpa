"""`scripts/stop.sh`: 取り込み中は処理中の 1 件の完了を待ち、FORCE=1 は待たない。

本物のアプリの代わりに、SIGTERM を受けても「解放の印」が出るまで終わらない擬似プロセスを立て、
取り込みの処理状況ファイル（アプリ本体が RUN_DIR に書くもの）を置いて stop.sh を動かす。
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

_REPO = Path(__file__).resolve().parents[2]

_FAKE_APP = r'''
import os, signal, sys, time
release = sys.argv[1]
state = sys.argv[2]
open(state, "w", encoding="utf-8").write(f"pid={os.getpid()}\nrun\tw\t{int(time.time())}\tsub/dir/big.xls\n")
signal.signal(signal.SIGTERM, lambda *a: None)      # SIGTERM を受けても処理中の 1 件が終わるまで続ける
while not os.path.exists(release):
    time.sleep(0.1)
os.unlink(state)
'''


def _setup(tmp_path: Path):
    root = tmp_path / "app"
    (root / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/stop.sh", "scripts/run-common.sh", "scripts/lib/install_state.sh"):
        shutil.copy(_REPO / rel, root / rel)
    run = root / "data" / "run"
    run.mkdir(parents=True)
    release = tmp_path / "release"
    state = run / "ingest_active"
    fake = tmp_path / "fake_uvicorn_app.py"
    fake.write_text(_FAKE_APP, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(fake), str(release), str(state)], start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()     # 終了したらすぐ回収する（ゾンビは kill -0 に生存と答える）
    (run / "api.pid").write_text(str(proc.pid), encoding="utf-8")
    deadline = time.monotonic() + 10
    while not state.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    env = {**os.environ, "RUN_DIR": str(run), "KEEP_STORES": "1", "APP_PROC_NEEDLE": "fake_uvicorn_app",
           "SHERPA_ENV_FILE": str(tmp_path / "e.env"), "SHERPA_SYSTEMD_RUN_DIR": str(tmp_path / "no-systemd")}
    (tmp_path / "e.env").write_text("", encoding="utf-8")
    return root, proc, release, env


def test_stop_waits_for_current_ingest_file_then_exits(tmp_path):
    root, proc, release, env = _setup(tmp_path)
    try:
        stop = subprocess.Popen(["bash", "scripts/stop.sh"], cwd=root, env={**env, "SHERPA_STOP_WAIT": "60"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(3)
        assert stop.poll() is None and proc.poll() is None        # 取り込み中は待っている（10 秒の猶予で殺さない）
        time.sleep(9)
        assert stop.poll() is None and proc.poll() is None
        release.write_text("", encoding="utf-8")                  # 処理中の 1 件が終わった
        out, err = stop.communicate(timeout=30)
        assert stop.returncode == 0, err
        assert "取り込み中の 1 件の完了を待っています（sub/dir/big.xls・経過" in out
        assert "強制終了" not in err
        proc.wait(timeout=10)
    finally:
        release.write_text("", encoding="utf-8")
        proc.kill() if proc.poll() is None else None


def test_stop_force_does_not_wait_for_ingest(tmp_path):
    root, proc, release, env = _setup(tmp_path)
    try:
        t0 = time.monotonic()
        r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env={**env, "FORCE": "1", "SHERPA_STOP_WAIT": "600"},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert time.monotonic() - t0 < 30
        assert "次の起動で自動的に続きから再開します" in r.stderr
        proc.wait(timeout=10)
        assert proc.returncode != 0                               # SIGKILL
    finally:
        proc.kill() if proc.poll() is None else None


_TERM_APP = r'''
import os, signal, sys, time
marker = sys.argv[1]
def on_term(*a):
    open(marker, "w").write("term")
    sys.exit(0)
signal.signal(signal.SIGTERM, on_term)
while True:
    time.sleep(0.1)
'''


def _old_app(tmp_path: Path):
    """終了の合図で終わる、印（graceful_stop_ready）を持たない擬似アプリ＝古い版のふり。"""
    root = tmp_path / "app"
    (root / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/stop.sh", "scripts/run-common.sh", "scripts/lib/install_state.sh"):
        shutil.copy(_REPO / rel, root / rel)
    run = root / "data" / "run"
    run.mkdir(parents=True)
    term = tmp_path / "termed"
    fake = tmp_path / "fake_uvicorn_old.py"
    fake.write_text(_TERM_APP, encoding="utf-8")
    proc = subprocess.Popen([sys.executable, str(fake), str(term)], start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    (run / "api.pid").write_text(str(proc.pid), encoding="utf-8")
    time.sleep(0.5)
    (tmp_path / "e.env").write_text("", encoding="utf-8")
    env = {**os.environ, "RUN_DIR": str(run), "KEEP_STORES": "1", "APP_PROC_NEEDLE": "fake_uvicorn_old",
           "SHERPA_ENV_FILE": str(tmp_path / "e.env"), "SHERPA_SYSTEMD_RUN_DIR": str(tmp_path / "no-systemd"),
           "SHERPA_STOP_WAIT": "120"}
    return root, proc, run / "convert.log", term, env


def _log_line(msg: str, age_sec: int) -> str:
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - age_sec))
    return f"{ts},123 INFO sherpa.ingest.convert: {msg}\n"


def test_old_app_stop_waits_until_a_file_has_just_started(tmp_path):
    root, proc, log, term, env = _old_app(tmp_path)
    try:
        log.write_text(_log_line("MD化を開始します: sub/big.xls", 60), encoding="utf-8")
        stop = subprocess.Popen(["bash", "scripts/stop.sh"], cwd=root, env=env,
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(4)
        assert stop.poll() is None and not term.exists()          # 1 件の途中＝合図をまだ送らない
        with log.open("a", encoding="utf-8") as f:                # 前の 1 件が終わり、次の 1 件が始まった
            f.write(_log_line("MD化が完了しました: sub/big.xls", 0))
            f.write(_log_line("MD化を開始します: sub/next.xls", 0))
        out, err = stop.communicate(timeout=30)
        assert stop.returncode == 0, err
        assert term.exists()
        assert "古い版のため、処理中の 1 件が終わるのを待っています（sub/big.xls・経過" in out
    finally:
        proc.kill() if proc.poll() is None else None


@pytest.mark.parametrize("last", [("MD化を開始します: a.xls", 1), ("MD化が完了しました: a.xls", 300), None])
def test_old_app_stop_sends_signal_at_once_when_not_mid_file(tmp_path, last):
    root, proc, log, term, env = _old_app(tmp_path)
    try:
        if last:
            log.write_text(_log_line(*last), encoding="utf-8")
        t0 = time.monotonic()
        r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert term.exists() and time.monotonic() - t0 < 8
    finally:
        proc.kill() if proc.poll() is None else None


def test_old_app_force_does_not_wait_and_new_app_is_not_treated_as_old(tmp_path):
    root, proc, log, term, env = _old_app(tmp_path)
    try:
        log.write_text(_log_line("MD化を開始します: sub/big.xls", 60), encoding="utf-8")
        t0 = time.monotonic()
        r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env={**env, "FORCE": "1"},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and term.exists() and time.monotonic() - t0 < 8
    finally:
        proc.kill() if proc.poll() is None else None


def test_new_app_marker_skips_the_old_version_wait(tmp_path):
    root, proc, log, term, env = _old_app(tmp_path)
    try:
        log.write_text(_log_line("MD化を開始します: sub/big.xls", 60), encoding="utf-8")
        (root / "data" / "run" / "graceful_stop_ready").write_text(f"pid={proc.pid}\n", encoding="utf-8")
        t0 = time.monotonic()
        r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and term.exists() and time.monotonic() - t0 < 8
        assert "古い版" not in r.stdout
    finally:
        proc.kill() if proc.poll() is None else None


def test_stop_reports_leftover_child_processes(tmp_path):
    """アプリが終わった後も残った子（soffice 相当）を、停止前に控えた一覧から知らせる（止めはしない）。"""
    root, proc, log, term, env = _old_app(tmp_path)
    child_py = tmp_path / "leftover_child.py"
    child_py.write_text("import time\ntime.sleep(60)\n", encoding="utf-8")
    parent = tmp_path / "fake_uvicorn_old.py"
    # アプリ本体を、子を起こしてから合図を待つ形に差し替える
    parent.write_text(_TERM_APP.replace("while True:", f"import subprocess\nsubprocess.Popen([sys.executable, r'{child_py}'], start_new_session=True)\nwhile True:"), encoding="utf-8")
    proc.kill()
    proc = subprocess.Popen([sys.executable, str(parent), str(term)], start_new_session=True)
    threading.Thread(target=proc.wait, daemon=True).start()
    (root / "data" / "run" / "api.pid").write_text(str(proc.pid), encoding="utf-8")
    time.sleep(1.5)
    try:
        r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env=env, capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert "アプリの停止後も残っています" in r.stderr and "leftover_child.py" in r.stderr
    finally:
        subprocess.run(["pkill", "-f", "leftover_child.py"])


# ---- 取り込みワーカー（Web アプリとは別プロセス） ----

_FAKE_WORKER = r'''
import os, signal, sys, time
release = sys.argv[1]
state = sys.argv[2]
open(state, "w", encoding="utf-8").write(f"pid={os.getpid()}\nrun\tw\t{int(time.time())}\tsub/dir/big.xls\n")
signal.signal(signal.SIGTERM, lambda *a: None)      # SIGTERM を受けても処理中の 1 件が終わるまで続ける
while not os.path.exists(release):
    time.sleep(0.1)
os.unlink(state)
'''


def _app_and_worker(tmp_path: Path):
    """すぐ止められる擬似アプリ（印つき）と、処理中の 1 件が終わるまで止まらない擬似ワーカー。"""
    root, app, log, term, env = _old_app(tmp_path)
    run = root / "data" / "run"
    (run / "graceful_stop_ready").write_text(f"pid={app.pid}\n", encoding="utf-8")
    release = tmp_path / "worker-release"
    state = run / "ingest_worker_active"
    fake = tmp_path / "fake_ingest_runner.py"
    fake.write_text(_FAKE_WORKER, encoding="utf-8")
    worker = subprocess.Popen([sys.executable, str(fake), str(release), str(state)], start_new_session=True)
    threading.Thread(target=worker.wait, daemon=True).start()
    (run / "ingest-worker.pid").write_text(str(worker.pid), encoding="utf-8")
    deadline = time.monotonic() + 10
    while not state.exists() and time.monotonic() < deadline:
        time.sleep(0.05)
    return root, app, worker, term, release, {**env, "WORKER_PROC_NEEDLE": "fake_ingest_runner"}


def test_stop_waits_for_the_worker_but_not_for_the_web_app(tmp_path):
    root, app, worker, term, release, env = _app_and_worker(tmp_path)
    try:
        stop = subprocess.Popen(["bash", "scripts/stop.sh"], cwd=root, env={**env, "SHERPA_STOP_WAIT": "60"},
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        time.sleep(14)
        assert stop.poll() is None and worker.poll() is None      # ワーカーは処理中の 1 件が終わるまで待たれる
        assert term.exists()                                      # Web アプリは待たずに止まっている
        release.write_text("", encoding="utf-8")                  # 処理中の 1 件が終わった
        out, err = stop.communicate(timeout=30)
        assert stop.returncode == 0, err
        assert "取り込み中の 1 件の完了を待っています（sub/dir/big.xls・経過" in out
        assert "強制終了" not in err
        worker.wait(timeout=10)
    finally:
        release.write_text("", encoding="utf-8")
        for p in (app, worker):
            p.kill() if p.poll() is None else None


def test_stop_only_app_leaves_the_worker_running(tmp_path):
    """make restart-web: Web アプリだけ止め、取り込みワーカー（処理中）には触れない。"""
    root, app, worker, term, release, env = _app_and_worker(tmp_path)
    try:
        r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env={**env, "STOP_ONLY": "app"},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0, r.stderr
        assert term.exists() and "取り込みワーカーを停止" not in r.stdout
        assert worker.poll() is None
        assert (root / "data" / "run" / "ingest-worker.pid").exists()
    finally:
        release.write_text("", encoding="utf-8")
        for p in (app, worker):
            p.kill() if p.poll() is None else None


def test_stop_only_worker_force_does_not_wait_and_leaves_the_app(tmp_path):
    root, app, worker, term, release, env = _app_and_worker(tmp_path)
    try:
        t0 = time.monotonic()
        r = subprocess.run(["bash", "scripts/stop.sh"], cwd=root, env={**env, "STOP_ONLY": "worker", "FORCE": "1"},
                           capture_output=True, text=True, timeout=60)
        assert r.returncode == 0 and time.monotonic() - t0 < 30
        assert "次の起動で自動的に続きから再開します" in r.stderr
        worker.wait(timeout=10)
        assert worker.returncode != 0                             # SIGKILL
        assert not term.exists() and app.poll() is None           # アプリは止めていない
    finally:
        for p in (app, worker):
            p.kill() if p.poll() is None else None


_FAKE_WORKER = r'''
import sys
from pathlib import Path
root = Path(sys.argv[1])
count = root / "runs"
n = int(count.read_text()) + 1 if count.exists() else 1
count.write_text(str(n))
if n == 1:
    (root / "VERSION").write_text("0.18.2\n")   # 1 件目の処理中に、別の版のファイルが上書きで置かれた
'''


def test_watcher_stops_relaunching_when_files_are_replaced_by_another_version(tmp_path):
    """見張りは、起動後にファイルが別の版へ置き換えられたら、次のワーカーを起動せずに終わる（版の混ざったコードで動かさない）。"""
    root = tmp_path / "app"
    (root / "scripts" / "lib").mkdir(parents=True)
    for rel in ("scripts/run-ingest-worker.sh", "scripts/run-common.sh", "scripts/lib/install_state.sh"):
        shutil.copy(_REPO / rel, root / rel)
    (root / "VERSION").write_text("0.19.0\n", encoding="utf-8")
    (root / "data" / "run").mkdir(parents=True)
    fake = tmp_path / "fake_worker.py"
    fake.write_text(_FAKE_WORKER, encoding="utf-8")
    # `python -m sherpa.ingest.ingest_worker` の代わりに、1 件処理して終わる擬似ワーカーを PYTHON_BIN として動かす
    shim = tmp_path / "pyshim"
    shim.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "{root}"\n', encoding="utf-8")
    shim.chmod(0o755)
    env = {**os.environ, "RUN_DIR": str(root / "data" / "run"), "PYTHON_BIN": str(shim),
           "SHERPA_ENV_FILE": str(tmp_path / "e.env"), "SHERPA_INGEST_NICE": "0"}
    (tmp_path / "e.env").write_text("", encoding="utf-8")
    r = subprocess.run(["bash", str(root / "scripts" / "run-ingest-worker.sh"), "serve"], cwd=root, env=env,
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stderr
    assert (root / "runs").read_text() == "1"          # 2 件目のワーカーは起動していない
    assert "別の版" in r.stderr
