"""取り込みワーカーが起動した子プロセス（MD 化の先読み・soffice など）の控えと、親が消えたときの後始末。

ワーカーが SIGKILL・OOM・停電で突然消えると、子は親 pid=1 のまま残る。その対策は 2 段:
① 先読みの子は自分で親の生存を見張り、親が変わったら自分で終わる（`guard_parent`・Linux/macOS 共通）
② 子ごとに `RUN_DIR/ingest_children/<pid>` へ控え（持ち主の pid・起動時刻・種別）を置き、次のワーカー起動と `make stop` が
   持ち主が居ない控えの子を片付ける（scripts/run-common.sh の `sherpa_reap_ingest_children`・同じ書式を読む）。
片付けは「持ち主が死んでいる」かつ「pid の起動時刻が控えと一致する」ときだけ（pid の再利用で無関係なプロセスを殺さない）。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

import logging
import os
import subprocess
import threading
import time
from pathlib import Path

_log = logging.getLogger("sherpa")

WATCH_SEC = 1.0


def registry_dir() -> Path | None:
    """控えの置き場。env `SHERPA_INGEST_CHILD_DIR` が優先。テスト中（`SHERPA_TEST_DB_ISOLATED`）は env で明示したときだけ。"""
    explicit = os.environ.get("SHERPA_INGEST_CHILD_DIR", "").strip()
    if explicit:
        return Path(explicit)
    if os.environ.get("SHERPA_TEST_DB_ISOLATED"):
        return None
    from .. import log_setup
    return log_setup.run_dir() / "ingest_children"


def start_token(pid: int) -> str:
    """pid の起動時刻（`ps -o lstart=`）。取れなければ空文字。シェル側の照合と同じ書式にそろえる。"""
    try:
        out = subprocess.run(["ps", "-o", "lstart=", "-p", str(pid)], capture_output=True, text=True,
                             timeout=5, env={**os.environ, "LC_ALL": "C"}).stdout
    except (OSError, subprocess.SubprocessError):
        return ""
    return out.strip()


def register(pid: int, *, mode: str = "pid", owner: int | None = None) -> None:
    """子 `pid` の控えを置く。`mode`＝"pid"（その pid だけ）／"group"（`start_new_session` で起動した子＝プロセスグループごと）。best-effort。"""
    try:
        base = registry_dir()
        token = start_token(pid)
        if base is None or not token:
            return
        base.mkdir(parents=True, exist_ok=True)
        tmp = base / f".{pid}.tmp"
        tmp.write_text(f"pid={pid}\nowner={owner if owner is not None else os.getpid()}\nmode={mode}\nlstart={token}\n",
                       encoding="utf-8")
        os.replace(tmp, base / str(pid))
    except Exception:
        _log.debug("子プロセスの控えの記録に失敗しました（best-effort）", exc_info=True)


def unregister(pid: int) -> None:
    try:
        base = registry_dir()
        if base is not None:
            (base / str(pid)).unlink(missing_ok=True)
    except Exception:
        _log.debug("子プロセスの控えの削除に失敗しました（best-effort）", exc_info=True)


def guard_parent(parent_pid: int) -> None:
    """先読みの子の初期化。控えを置き、親（`parent_pid`）が変わったら（＝親が消えたら）自分で終わる見張りを始める。

    Linux の prctl(PR_SET_PDEATHSIG) は macOS に無く、親の「スレッド」の終了でも発火するため使わない。
    """
    register(os.getpid(), mode="pid", owner=parent_pid)

    def _watch() -> None:
        while True:
            time.sleep(WATCH_SEC)
            if os.getppid() != parent_pid:
                os._exit(1)

    threading.Thread(target=_watch, daemon=True, name="sherpa-parent-guard").start()
