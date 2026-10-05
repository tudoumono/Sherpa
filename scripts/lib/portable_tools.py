#!/usr/bin/env python3
"""GNU のコマンド（timeout・flock・setsid・sha256sum・getent・tar --transform）が無い環境
（macOS の標準構成）向けの代替。標準ライブラリだけで動く。シェル側は「GNU のコマンドがあればそれ、
無ければこの補助」と使い分ける。

サブコマンド:
  sha256 [--basename] FILE...  sha256sum と同じ形式（"<hash>  <name>"）で出す（--basename は名前をファイル名だけにする）
  tar-append TAR PREFIX FILE...  既存 tar へ FILE を PREFIX/<basename> の名前で追記する
  resolve HOST                 名前解決できれば 0（数値 IP はそのまま通る）
  tcp HOST PORT [SEC]          SEC 秒（既定 3）以内に TCP 接続できれば 0
  run-limited SEC CMD...       CMD を SEC 秒で打ち切る（超過は終了コード 124・GNU timeout と同じ）
  run-group LIMIT CMD...       専用のプロセスグループで CMD を LIMIT（例 45m）まで実行し、超過時は
                               グループへ SIGINT→TERM→KILL を送り 124 を返す（setsid ＋ timeout -s INT の代わり）
  hold-lock FILE wait|nowait WATCH_PID
                               FILE の排他ロックを取り、取れたら "LOCKED"（nowait で取れなければ
                               "BUSY"）を標準出力へ出して保持し続ける。WATCH_PID が消えるか SIGTERM で解放
"""
from __future__ import annotations

import fcntl
import hashlib
import os
import signal
import socket
import subprocess
import sys
import tarfile
import time


def _parse_seconds(text: str) -> float:
    unit = {"s": 1, "m": 60, "h": 3600, "d": 86400}
    if text and text[-1] in unit:
        return float(text[:-1]) * unit[text[-1]]
    return float(text)


def _sha256(args: list[str]) -> int:
    basename = bool(args) and args[0] == "--basename"
    for name in args[1:] if basename else args:
        h = hashlib.sha256()
        with open(name, "rb") as f:
            for chunk in iter(lambda: f.read(1 << 20), b""):
                h.update(chunk)
        print(f"{h.hexdigest()}  {os.path.basename(name) if basename else name}")
    return 0


def _tar_append(args: list[str]) -> int:
    tar_path, prefix, files = args[0], args[1].strip("/"), args[2:]
    with tarfile.open(tar_path, "a") as tf:
        for name in files:
            tf.add(name, arcname=f"{prefix}/{os.path.basename(name)}", recursive=False)
    return 0


def _resolve(args: list[str]) -> int:
    try:
        socket.getaddrinfo(args[0], None)
    except OSError:
        return 1
    return 0


def _tcp(args: list[str]) -> int:
    sec = float(args[2]) if len(args) > 2 else 3.0
    try:
        with socket.create_connection((args[0], int(args[1])), timeout=sec):
            return 0
    except OSError:
        return 1


def _run_limited(args: list[str]) -> int:
    limit, cmd = float(args[0]), args[1:]
    child = subprocess.Popen(cmd)
    try:
        return _exit_code(child.wait(timeout=limit))
    except subprocess.TimeoutExpired:
        child.terminate()
        try:
            child.wait(timeout=5)
        except subprocess.TimeoutExpired:
            child.kill()
            child.wait()
        return 124


def _exit_code(rc: int) -> int:
    return 128 - rc if rc < 0 else rc


def _wait_for(child: subprocess.Popen, sec: float) -> bool:
    try:
        child.wait(timeout=sec)
        return True
    except subprocess.TimeoutExpired:
        return False


def _group_alive(pgid: int) -> bool:
    try:
        os.killpg(pgid, 0)
    except OSError:
        return False
    return True


def _run_group(args: list[str]) -> int:
    limit, cmd = _parse_seconds(args[0]), args[1:]
    grace = float(os.environ.get("SHERPA_GATE_KILL_GRACE", "10"))
    try:
        os.setsid()   # 呼び出し元が kill -s SIG -<このPID> で送れるよう、自身をグループの先頭にする
    except OSError:
        pass
    state: dict[str, int] = {}

    def to_child_group(sig: int, *_: object) -> None:
        if "pgid" in state:
            try:
                os.killpg(state["pgid"], sig)
            except OSError:
                pass

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, to_child_group)
    # テスト子は専用のセッション・グループで起動する（孫まで含めて後始末するため）。
    child = subprocess.Popen(cmd, start_new_session=True)
    state["pgid"] = child.pid
    if _wait_for(child, limit):
        return _exit_code(child.returncode)
    # 時間切れ: INT（pytest が後始末できる）→ 猶予後に TERM → KILL の順でグループごと止める。
    to_child_group(signal.SIGINT)
    if not _wait_for(child, grace):
        to_child_group(signal.SIGTERM)
        if not _wait_for(child, 5):
            to_child_group(signal.SIGKILL)
            child.wait()
    if _group_alive(child.pid):   # 直接の子が終わっても孫が残っていれば回収する
        to_child_group(signal.SIGTERM)
        deadline = time.time() + 3
        while _group_alive(child.pid) and time.time() < deadline:
            time.sleep(0.1)
        to_child_group(signal.SIGKILL)
    return 124


def _hold_lock(args: list[str]) -> int:
    path, mode, watch = args[0], args[1], int(args[2])
    fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    while True:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            break
        except OSError:
            if mode == "nowait":
                print("BUSY", flush=True)
                return 1
            if not _alive(watch):
                return 1
            time.sleep(0.2)
    print("LOCKED", flush=True)
    while _alive(watch):
        time.sleep(0.1)
    return 0


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


COMMANDS = {
    "sha256": _sha256, "tar-append": _tar_append, "resolve": _resolve, "tcp": _tcp,
    "run-limited": _run_limited, "run-group": _run_group, "hold-lock": _hold_lock,
}


def main(argv: list[str]) -> int:
    if len(argv) < 2 or argv[1] not in COMMANDS:
        print(__doc__, file=sys.stderr)
        return 2
    return COMMANDS[argv[1]](argv[2:])


if __name__ == "__main__":
    sys.exit(main(sys.argv))
