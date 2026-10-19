"""重い 1 件の変換の間だけ、循環 GC の発動を減らす。

関数の実行中だけ世代 0 のしきい値を上げ、抜けるときに元へ戻す（入れ子でも安全）。
守ること: しきい値の変更はプロセス全体に効くため、必ず `finally` で元へ戻す。GC を止めない（しきい値を上げるだけ）。
"""
from __future__ import annotations

import functools
import gc
from collections.abc import Callable
from typing import TypeVar

_GEN0_THRESHOLD = 200_000
_GEN1_THRESHOLD = 20
_GEN2_THRESHOLD = 20

F = TypeVar("F", bound=Callable)


def relaxed_gc(func: F) -> F:
    """`func` の実行中だけ GC のしきい値を上げる。"""

    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        previous = gc.get_threshold()
        if previous[0] >= _GEN0_THRESHOLD:
            return func(*args, **kwargs)
        gc.set_threshold(_GEN0_THRESHOLD, _GEN1_THRESHOLD, _GEN2_THRESHOLD)
        try:
            return func(*args, **kwargs)
        finally:
            gc.set_threshold(*previous)

    return wrapper  # type: ignore[return-value]
