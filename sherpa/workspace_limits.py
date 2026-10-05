"""個人ファイル（workspace）の上限と保持日数。値は管理画面（system_settings）が唯一の正で、未設定・不正・DB 不達は既定へ倒す。
設計: docs/design/settings.md「個人ファイル」
"""
from __future__ import annotations

# system_settings のキーと既定・許容範囲（`routers/system_extras.py` の検証と揃える）。
MAX_BYTES_KEY = "workspace_max_bytes"
TTL_DAYS_KEY = "workspace_ttl_days"
MAX_BYTES_DEFAULT = 10 * 1024 * 1024
MAX_BYTES_MIN = 1024 * 1024
MAX_BYTES_MAX = 1024 * 1024 * 1024
TTL_DAYS_DEFAULT = 90
TTL_DAYS_MIN = 0
TTL_DAYS_MAX = 3650


def _setting_int(key: str, default: int, lo: int, hi: int) -> int:
    try:
        from sherpa import store
        raw = (store.get_system_settings() or {}).get(key)
    except Exception:
        return default
    if isinstance(raw, bool) or not isinstance(raw, int):
        return default
    return raw if lo <= raw <= hi else default


def max_bytes() -> int:
    """個人ファイル 1 件のアップロード上限（バイト）。"""
    return _setting_int(MAX_BYTES_KEY, MAX_BYTES_DEFAULT, MAX_BYTES_MIN, MAX_BYTES_MAX)


def ttl_days() -> int:
    """個人ファイル・成果物の保持日数。0 は無期限。"""
    return _setting_int(TTL_DAYS_KEY, TTL_DAYS_DEFAULT, TTL_DAYS_MIN, TTL_DAYS_MAX)
