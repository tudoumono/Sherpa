"""外部 API 監査の書き込み失敗の数え上げ（プロセス内）。"""
from __future__ import annotations

import threading

_lock = threading.Lock()
_failures = 0


def record_failure() -> int:
    """失敗を 1 件数えて、その時点の累計を返す。"""
    global _failures
    with _lock:
        _failures += 1
        return _failures


def failure_count() -> int:
    with _lock:
        return _failures
