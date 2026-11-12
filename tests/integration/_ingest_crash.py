"""取り込みの途中で起きる失敗を、本物の worker を子プロセスで動かして再現する土台。

製品の関数は差し替えない。子プロセスの `sys.addaudithook` で、指定した接尾辞のファイルを書き込みで開く `open` だけを `OSError` にする。
また、指定した資料フォルダの走査（`os.scandir`／`os.listdir`）や、指定した文字列を含む HTTP 送信（ES の `/_bulk` など）、
指定した名前の改名（`os.rename`）に達した瞬間に自分を SIGKILL する。
"""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

ROOT = pathlib.Path(__file__).resolve().parents[2]

_CHILD = r"""
import os, signal, sys

suffix = os.environ["CRASH_FAIL_OPEN_SUFFIX"]
remaining = [int(os.environ["CRASH_FAIL_OPEN_TIMES"])]
kill_scan = os.environ.get("CRASH_KILL_SCAN_DIR", "")
kill_send = os.environ.get("CRASH_KILL_HTTP_SEND", "").encode()
armed_by = os.environ.get("CRASH_KILL_HTTP_AFTER", "").encode()
armed = [not armed_by]
kill_rename = os.environ.get("CRASH_KILL_RENAME", "")   # "改名元の末尾|改名先の末尾"（両方が一致した改名の直前に止まる）
kill_rmtree = os.environ.get("CRASH_KILL_RMTREE", "")   # この文字列で終わるパスの shutil.rmtree の直前に止まる
contains = os.environ.get("CRASH_FAIL_OPEN_CONTAINS", "")
write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
touch_file = os.environ.get("CRASH_TOUCH_FILE", "")   # 原本を途中で変える（更新時刻を進める）ファイル
touch_on = os.environ.get("CRASH_TOUCH_ON", "")   # "http"＝最初の HTTP 送信で／"open:文字列"＝その文字列を含むパスの最初の書き込みで
touched = [False]


def _touch():
    if touch_file and not touched[0]:
        touched[0] = True
        st = os.stat(touch_file)
        os.utime(touch_file, (st.st_atime, st.st_mtime + 100))


def _hook(event, args):
    if touch_on == "http" and event == "http.client.send":
        _touch()
    if kill_scan and event in ("os.scandir", "os.listdir") and str(args[0] or "").startswith(kill_scan):
        os.kill(os.getpid(), signal.SIGKILL)
    if event == "http.client.send" and isinstance(args[1], (bytes, bytearray)):
        if kill_send and armed[0] and kill_send in args[1]:
            os.kill(os.getpid(), signal.SIGKILL)
        if armed_by and armed_by in args[1]:
            armed[0] = True
    if kill_rmtree and event == "shutil.rmtree" and str(args[0]).endswith(kill_rmtree):
        os.kill(os.getpid(), signal.SIGKILL)
    if kill_rename and event == "os.rename":
        src_end, dst_end = kill_rename.split("|")
        if str(args[0]).endswith(src_end) and str(args[1]).endswith(dst_end):
            os.kill(os.getpid(), signal.SIGKILL)
    if event != "open":
        return
    path, _mode, flags = args
    if touch_on.startswith("open:") and isinstance(path, str) and touch_on[5:] in path and flags & write_flags:
        _touch()
    hit = isinstance(path, str) and ((suffix and path.endswith(suffix)) or (contains and contains in path))
    if hit and flags & write_flags and remaining[0] > 0:
        remaining[0] -= 1
        raise OSError(5, "injected write failure", path)


sys.addaudithook(_hook)
"""

_PROCESS_ONE = """
from sherpa import app_version
from sherpa.ingest import ingest_worker

ingest_worker.process_one("crash-child", app_version.current())
"""


def _child_env(fail_open_suffix: str, fail_times: int, kill_on_scan_of: str, kill_on_http_send: str,
               kill_after_http_send: str = "", extra_env: dict | None = None, kill_on_rename: str = "",
               fail_open_containing: str = "", kill_on_rmtree: str = "", touch_file: str = "", touch_on: str = "") -> dict:
    return {**os.environ, "CRASH_TOUCH_FILE": touch_file, "CRASH_TOUCH_ON": touch_on, "CRASH_FAIL_OPEN_SUFFIX": fail_open_suffix, "CRASH_KILL_RENAME": kill_on_rename,
            "CRASH_KILL_RMTREE": kill_on_rmtree,
            "CRASH_FAIL_OPEN_CONTAINS": fail_open_containing,
            "CRASH_FAIL_OPEN_TIMES": str(fail_times),
            "CRASH_KILL_SCAN_DIR": kill_on_scan_of, "CRASH_KILL_HTTP_SEND": kill_on_http_send,
            "CRASH_KILL_HTTP_AFTER": kill_after_http_send, **(extra_env or {}),
            "PYTHONPATH": os.pathsep.join([str(ROOT), os.environ.get("PYTHONPATH", "")])}


def start_worker_once() -> subprocess.Popen:
    """積み済みの依頼を 1 件処理する子プロセスを起動して待たずに返す（DB の文で止まっている間に、親が SIGKILL するため）。"""
    return subprocess.Popen([sys.executable, "-c", _CHILD + _PROCESS_ONE], cwd=ROOT, env=_child_env("", 0, "", ""),
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def run_worker_once(*, fail_open_suffix: str = "", fail_times: int = 1000, timeout: int = 300,
                    kill_on_scan_of: str = "", kill_on_http_send: str = "", kill_after_http_send: str = "",
                    extra_env: dict | None = None, kill_on_rename: str = "",
                    fail_open_containing: str = "", kill_on_rmtree: str = "", touch_file: str = "", touch_on: str = "",
                    code: str = _PROCESS_ONE) -> subprocess.CompletedProcess:
    """積み済みの依頼を 1 件だけ子プロセスで処理する。`fail_open_suffix` で終わるファイルの書き込みだけが、最初の `fail_times` 回失敗する。
    `kill_on_scan_of`＝その資料フォルダを走査し始めた瞬間に、`kill_on_http_send`＝その文字列を含む HTTP 送信の瞬間に SIGKILL される。
    `kill_after_http_send` を足すと、その文字列を含む送信を見た後の `kill_on_http_send` にだけ反応する。`extra_env` は子の環境変数に足す。
    `kill_on_rename`＝`"改名元の末尾|改名先の末尾"` に一致する改名の直前に SIGKILL される。`fail_open_containing`＝その文字列を含むパスの書き込みも失敗させる（`fail_times` の枠は `fail_open_suffix` と共有）。
    `touch_file`・`touch_on`＝`touch_on`（`"http"`＝最初の HTTP 送信、`"open:文字列"`＝その文字列を含むパスの最初の書き込み）に達した瞬間に、`touch_file` の更新時刻を進める（原本を途中で変える）。
    `kill_on_rmtree`＝その文字列で終わるパスの `shutil.rmtree` の直前に SIGKILL される。`code` を渡すと、依頼を拾う代わりにその Python を子で実行する（孤児の掃除の「1 つ消す」を直接呼ぶ）。"""
    env = _child_env(fail_open_suffix, fail_times, kill_on_scan_of, kill_on_http_send, kill_after_http_send, extra_env,
                     kill_on_rename, fail_open_containing, kill_on_rmtree, touch_file, touch_on)
    return subprocess.run([sys.executable, "-c", _CHILD + code], cwd=ROOT, env=env, timeout=timeout,
                          capture_output=True, text=True, check=False)
