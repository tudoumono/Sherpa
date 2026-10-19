"""Web アプリと取り込みワーカーが RUN_DIR / DB に残す状態（stop.sh の待ち表示と、取り込み画面の版の照合が読む）。

- 取り込みワーカーの処理状況ファイル（`ingest_worker_active`）: 1 行目 `pid=<ワーカーの pid>`、以降 `run<TAB>資料フォルダ<TAB>開始(epoch秒)<TAB>処理中ファイル`。
- 停止の印（`graceful_stop_ready`）: 取り込みを持たない Web アプリが起動時に pid を書く。stop.sh が「すぐ止められる版」と見分ける。
- Web アプリの心拍（`ingest_workers` の role=web）。
設計: docs/design/operations.md「取り込みワーカーと版の照合」
"""
from __future__ import annotations

import logging
import os
import socket
import threading
from pathlib import Path

_log = logging.getLogger("sherpa")

BEAT_SEC = 10


def _state_path() -> Path | None:
    """処理状況の記録先。テスト中（`SHERPA_TEST_DB_ISOLATED`）は env で明示したときだけ書く。"""
    explicit = os.environ.get("SHERPA_INGEST_STATE_FILE", "").strip()
    if explicit:
        return Path(explicit)
    if os.environ.get("SHERPA_TEST_DB_ISOLATED"):
        return None
    from .. import log_setup
    return log_setup.run_dir() / "ingest_worker_active"


def write_active(world: str, since: float, rel: str | None) -> None:
    """ワーカーが今処理している run を記録する。best-effort。"""
    try:
        path = _state_path()
        if path is None:
            return
        from .. import json_io
        one = (rel or "").replace("\t", " ").replace("\n", " ").replace("\r", " ")
        json_io.write_text_atomic(path, f"pid={os.getpid()}\nrun\t{world}\t{int(since)}\t{one}\n")
    except Exception:
        _log.debug("取り込みの処理状況の記録に失敗しました（best-effort）", exc_info=True)


def clear_active() -> None:
    try:
        path = _state_path()
        if path is not None:
            path.unlink(missing_ok=True)
    except Exception:
        _log.debug("取り込みの処理状況の削除に失敗しました（best-effort）", exc_info=True)


def _ready_path() -> Path | None:
    p = _state_path()
    return p.with_name("graceful_stop_ready") if p is not None else None


def mark_web_ready() -> None:
    """取り込みを持たない（すぐ止められる）Web アプリの印を RUN_DIR に置く。best-effort。"""
    try:
        path = _ready_path()
        if path is not None:
            from .. import json_io
            json_io.write_text_atomic(path, f"pid={os.getpid()}\n")
    except Exception:
        _log.debug("停止対応の印の書き込みに失敗しました（best-effort）", exc_info=True)


def clear_web_ready() -> None:
    try:
        path = _ready_path()
        if path is not None:
            path.unlink(missing_ok=True)
    except Exception:
        _log.debug("停止対応の印の削除に失敗しました（best-effort）", exc_info=True)


def new_worker_id(role: str) -> str:
    import uuid
    return f"{role}:{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:6]}"


def start_web_heartbeat(stop: threading.Event) -> threading.Thread:
    """Web アプリの心拍を `BEAT_SEC` ごとに書く daemon thread を起動する（`stop` で止まり、最後に `stopping` を書く）。"""
    from .. import app_version
    from ..store import workers as store_workers

    worker_id = new_worker_id("web")
    version = app_version.current()

    def _write(state: str) -> None:
        try:
            store_workers.beat(worker_id, role="web", app_version=version, state=state, pid=os.getpid())
        except Exception:
            _log.debug("Web アプリの心拍の記録に失敗しました（best-effort）", exc_info=True)

    def _run() -> None:
        try:
            store_workers.purge_old()
        except Exception:
            _log.debug("古い心拍の掃除に失敗しました（best-effort）", exc_info=True)
        _write("idle")
        while not stop.wait(BEAT_SEC):
            _write("idle")
        _write("stopping")

    t = threading.Thread(target=_run, daemon=True, name="sherpa-web-heartbeat")
    t.start()
    return t
