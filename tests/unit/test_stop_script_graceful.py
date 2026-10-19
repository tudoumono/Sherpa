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
