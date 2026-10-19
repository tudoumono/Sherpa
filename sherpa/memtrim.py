"""重い処理の後に、解放済みのメモリを OS へ返す（Linux の glibc のみ・それ以外は何もしない）。

Python は空いたメモリを OS へ返しにくく、glibc の malloc が持ったままにする分は `malloc_trim` で返せる。
連続して呼んでも無駄なので、前回から `MIN_INTERVAL_SEC` 秒以内の呼び出しは見送る。
"""
from __future__ import annotations

import ctypes
import gc
import sys
import threading
import time

MIN_INTERVAL_SEC = 30.0

_lock = threading.Lock()
_last = 0.0
_libc = None
_libc_loaded = False


def _malloc_trim() -> bool:
    global _libc, _libc_loaded
    if not sys.platform.startswith("linux"):
        return False
    if not _libc_loaded:
        _libc_loaded = True
        try:
            _libc = ctypes.CDLL("libc.so.6")
        except OSError:
            _libc = None
    if _libc is None or not hasattr(_libc, "malloc_trim"):
        return False
    try:
        _libc.malloc_trim(0)
        return True
    except Exception:
        return False


def release(*, force: bool = False) -> bool:
    """`gc.collect()` のあと `malloc_trim(0)` を呼ぶ。呼んだら True（macOS・間隔内の呼び出しは False）。"""
    global _last
    with _lock:
        now = time.monotonic()
        if not force and _last and now - _last < MIN_INTERVAL_SEC:
            return False
        _last = now
    gc.collect()
    return _malloc_trim()
