"""埋め込みの要求が待ち時間に間に合わないとき、その回の中で 1 回に送る件数を半分にして続ける。
設計: docs/design/rag.md「埋め込みの待ち時間と件数」
"""
from __future__ import annotations

import threading


class BatchState:
    """その回（接続設定 1 つの寿命）で 1 回に送る件数の上限。下がる一方で、戻さない。"""

    def __init__(self, size: int):
        self.size = max(1, size)
        self._lock = threading.Lock()

    def lower(self, failed_size: int) -> None:
        with self._lock:
            self.size = max(1, min(self.size, failed_size // 2))


_create_lock = threading.Lock()


def state_of(c: dict, size: int) -> BatchState:
    """接続設定 `c` に回の状態を 1 つだけ持たせて返す（並列の送信が同じ状態を使う）。"""
    with _create_lock:
        st = c.get("shrink_state")
        if st is None:
            st = c["shrink_state"] = BatchState(size)
        return st


def send_halving(batch: list, send_once, state: BatchState) -> list | None:
    """`send_once(部分, timed_out)` で送る。`timed_out` は件数が 2 以上のときだけリストで渡り、待ち時間切れなら印が足される。
    切れたら上限を半分にして、同じ内容を小さく分けて送り直す。1 件で切れたら送り直さず、`send_once` の失敗（None）のまま返す。
    """
    out: list = []
    s = 0
    while s < len(batch):
        part = batch[s:s + state.size]   # 上限は途中で下がるので、進める幅は取った件数で決める
        s += len(part)
        timed_out: list | None = [] if len(part) > 1 else None
        got = send_once(part, timed_out)
        if got is None and timed_out:
            state.lower(len(part))
            got = send_halving(part, send_once, state)
        if got is None:
            return None
        out.extend(got)
    return out
