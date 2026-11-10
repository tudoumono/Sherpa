"""つながりの確認の「取り込みの係」の経路。取り込みの埋め込みなどはワーカーのプロセスで動くので、確認もワーカーの中で流す。
やり取りは `log_setup.run_dir()`（ワーカーの状態ファイルと同じ置き場）の 2 つのファイル:
API が頼み（`connectivity_request.json`）を書き、ワーカーが心拍（`_Beat`）のたびに見て、別のスレッドで確認を流して結果（`connectivity_result_ingest.json`）を書く。
設計: docs/design/operations.md「つながりの確認」
"""
from __future__ import annotations

import json
import logging
import threading
import time
import uuid
from pathlib import Path

from . import worker_state

_log = logging.getLogger("sherpa")

REQUEST_TTL_S = 120          # 頼みから、この秒数を過ぎたものはワーカーが流さない
CHECK_LIMIT_S = 60.0         # ワーカーでの確認の上限の見込み（API が待つ時間の計算用）
WAIT_MARGIN_S = 10.0
POLL_S = 0.5

_now = time.time             # 試験で差し替える（時計の境界）


def _paths() -> tuple[Path, Path] | None:
    """頼みと結果のファイル。置き場は `worker_state._state_path` と同じ考え方（テスト中は env で明示したときだけ）。"""
    state = worker_state._state_path()
    if state is None:
        return None
    return state.with_name("connectivity_request.json"), state.with_name("connectivity_result_ingest.json")


def wait_limit_s() -> float:
    """API が結果を待つ最大の秒数（心拍の間隔＋確認の上限時間＋余裕）。"""
    return worker_state.BEAT_SEC + CHECK_LIMIT_S + WAIT_MARGIN_S


def _read(path: Path) -> dict | None:
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return d if isinstance(d, dict) else None


def write_request(include_paid: bool) -> str | None:
    """頼みを書いて識別子を返す。置き場が無い・書けないときは None。"""
    from .. import json_io
    paths = _paths()
    if paths is None:
        return None
    rid = uuid.uuid4().hex
    try:
        json_io.write_text_atomic(paths[0], json.dumps(
            {"id": rid, "include_paid": bool(include_paid), "at": _now()}))
    except OSError:
        return None
    return rid


def read_result(rid: str) -> list[dict] | None:
    """識別子が一致する結果の配列（まだ無ければ None）。"""
    paths = _paths()
    d = _read(paths[1]) if paths else None
    if d and d.get("id") == rid and isinstance(d.get("results"), list):
        return d["results"]
    return None


def request_and_wait(include_paid: bool, *, wait_s: float | None = None, sleep=time.sleep) -> list[dict]:
    """取り込みワーカーへ確認を頼み、結果を待って返す。ワーカーが動いていなければすぐ、返事が無ければ時間切れで「確かめきれなかった」。"""
    from .. import connectivity
    from . import worker_status

    def _incomplete(reason: str) -> list[dict]:
        return connectivity.incomplete_rows("ingest", f"確かめきれなかった（{reason}）")

    try:
        alive = bool(worker_status.live_workers())
    except Exception:
        return _incomplete("取り込みの係の状態を読めない")
    if not alive:
        return _incomplete("取り込みの係が動いていない")
    rid = write_request(include_paid)
    if rid is None:
        return _incomplete("取り込みの係と連絡する場所に書けない")
    limit = wait_limit_s() if wait_s is None else wait_s
    deadline = time.monotonic() + limit
    while True:
        rows = read_result(rid)
        if rows is not None:
            return rows
        if time.monotonic() >= deadline:
            return _incomplete("取り込みの係から返事が無い")
        sleep(POLL_S)


STOP_WAIT_S = 35.0           # ワーカーが終わるとき、流している確認を待つ上限


def _claim(request: Path) -> dict | None:
    """頼みのファイルを自分だけの名前へ移して取り（移せるのは 1 つのプロセスだけ）、中身を読んで消す。無ければ None。"""
    mine = request.with_name(f"{request.name}.claim-{uuid.uuid4().hex}")
    try:
        request.rename(mine)
    except OSError:
        return None
    try:
        return _read(mine)
    finally:
        try:
            mine.unlink()
        except OSError:
            pass


class Relay:
    """ワーカー側。心拍のたびに `poll()` で頼みを取り、新しい頼みだけを別のスレッドで流す（取った頼みは消えるので 2 度流さない）。"""

    def __init__(self) -> None:
        self._thread: threading.Thread | None = None

    def poll(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return  # 前の確認が終わるまで取らない（次の心拍でもう一度見る）
        paths = _paths()
        if paths is None or not paths[0].exists():
            return
        req = _claim(paths[0])
        if not req:
            return
        rid, at = req.get("id"), req.get("at")
        if not isinstance(rid, str) or not isinstance(at, (int, float)) or _now() - at > REQUEST_TTL_S:
            return
        self._thread = threading.Thread(target=self._run, args=(rid, bool(req.get("include_paid"))),
                                        daemon=True, name="sherpa-ingest-connectivity")
        self._thread.start()

    def wait(self, timeout: float = STOP_WAIT_S) -> None:
        """流している確認があれば、上限まで終わるのを待つ（ワーカーが終わる前に結果を書くため）。"""
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout)

    def _run(self, rid: str, include_paid: bool) -> None:
        from .. import connectivity, json_io, proxy_bypass
        try:
            proxy_bypass.apply()
            rows = connectivity.run_checks(include_paid, path="ingest")
        except Exception:
            _log.warning("つながりの確認（取り込みの係）に失敗しました", exc_info=True)
            rows = connectivity.incomplete_rows("ingest", "確かめきれなかった（取り込みの係で確認が失敗した）")
        paths = _paths()
        if paths is None:
            return
        try:
            json_io.write_text_atomic(paths[1], json.dumps(
                {"id": rid, "at": _now(), "results": rows}, ensure_ascii=False))
        except OSError:
            _log.warning("つながりの確認の結果を書けませんでした", exc_info=True)
