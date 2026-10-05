"""環境変数の整数読み取り（依存を持たない末端モジュール）。"""
from __future__ import annotations

import os


def env_int(name: str, default: int, lo: int, hi: int) -> int:
    """security-limit 系 env の整数解析。範囲 [lo, hi] 外・非整数は既定値へ戻す（負値がスライス上限として反転して上限が無効になるのを防ぐ）。
    既定値自体も [lo, hi] にクランプする。未設定も既定値。
    """
    default = max(lo, min(default, hi))
    raw = os.environ.get(name)
    if raw is None:
        return default
    try:
        v = int(raw)
    except ValueError:
        return default
    return v if lo <= v <= hi else default
