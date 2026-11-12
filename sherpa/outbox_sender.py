"""配送待ち（`store.outbox`）の送り手。期限の来た行を種類ごとの処理に渡し、結果に応じて完了・再送の予約・諦めを記録する。
API のプロセスの背景スレッド（`start()`）が常時引き継ぎ、取り込みの worker は終了前に `run_due()` を呼んで手元の分を送る。
処理が例外を投げたら再送（間隔は種類ごとの `delays`）、`GiveUp` を投げたら即座に諦める。再送の上限を超えたら理由を残して諦める。
設計: docs/design/external-api.md「配送待ち」
"""
from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass

from sherpa.store import outbox

_log = logging.getLogger(__name__)

_TICK_SECONDS = 2.0
_PURGE_EVERY_TICKS = 1800
_BATCH = 20


class GiveUp(Exception):
    """再送しても直らない失敗（宛先が無い・許可されていない等）。理由を `str(e)` で残して諦める。"""


@dataclass(frozen=True)
class Kind:
    """`deliver` は成功で戻り、一時的な失敗は例外、恒久的な失敗は `GiveUp`。`delays` の秒数だけ空けて再送し、尽きたら諦める（試行は最大 `len(delays)+1` 回）。`on_gave_up` は諦めを記録した後に呼ぶ。"""
    deliver: Callable[[dict], None]
    delays: tuple[float, ...]
    on_gave_up: Callable[[dict, str], None] | None = None


def _give_up(kind: Kind, row: dict, reason: str) -> None:
    if not outbox.mark_gave_up(row, reason=reason):
        return  # 貸出を失っている（別の送り手が引き継いだ）
    if kind.on_gave_up is not None:
        try:
            kind.on_gave_up(row, reason)
        except Exception:
            _log.warning("配送待ちの諦めの記録に失敗しました: id=%s kind=%s", row["id"], row["kind"], exc_info=True)


def _handle(kind: Kind, row: dict) -> None:
    try:
        kind.deliver(row)
    except GiveUp as e:
        _give_up(kind, row, str(e) or "give_up")
        return
    except Exception as e:
        reason = e.__class__.__name__
        if row["attempts"] > len(kind.delays):
            _give_up(kind, row, reason)
        else:
            outbox.mark_retry(row, delay_seconds=kind.delays[row["attempts"] - 1], error=reason)
        return
    outbox.mark_done(row)


def run_due(kinds: dict[str, Kind], *, deadline: float | None = None) -> int:
    """期限の来た行を、空になるか `deadline`（`time.monotonic()` 基準）まで処理する。処理した件数を返す。"""
    handled = 0
    while deadline is None or time.monotonic() < deadline:
        rows = outbox.claim_due(tuple(kinds), _BATCH)
        if not rows:
            break
        for i, row in enumerate(rows):
            if deadline is not None and time.monotonic() >= deadline:
                outbox.release(rows[i:])  # 借りたまま塞がない
                return handled
            _handle(kinds[row["kind"]], row)
            handled += 1
    return handled


_thread: threading.Thread | None = None
_stop = threading.Event()


def _loop(kinds: dict[str, Kind]) -> None:
    ticks = 0
    while not _stop.is_set():
        try:
            run_due(kinds)
            ticks += 1
            if ticks % _PURGE_EVERY_TICKS == 0:
                outbox.purge_finished()
        except Exception:
            _log.warning("配送待ちの送り手の1周期に失敗しました（DB 不達の可能性）", exc_info=True)
        _stop.wait(_TICK_SECONDS)


def start(kinds: dict[str, Kind]) -> None:
    """送り手の背景スレッドを起こす（起動済みなら何もしない）。"""
    global _thread
    if _thread is not None and _thread.is_alive():
        return
    _stop.clear()
    _thread = threading.Thread(target=_loop, args=(kinds,), daemon=True, name="outbox-sender")
    _thread.start()


def stop(timeout: float = 5.0) -> None:
    _stop.set()
    t = _thread
    if t is not None:
        t.join(timeout=timeout)
