"""OCR の入切の解決。環境変数 `SHERPA_OCR_ENABLED=0` のときだけ切に固定し、それ以外（未設定・空・`1` など）は全体設定 `ocr_enabled`（既定は切）を読む。

取り込み・API・OCR ワーカーのどこから呼んでも同じ値になる。設定を読めなかったときは例外を伝える（切として扱わない）。
入切（`enabled`）は OCR の対象づくり・予約・ワーカーの仕事取りだけを決める。変換の出力と PDF の変換キャッシュの鍵は
環境変数だけで決まる `convert_mode_on` を使い、全体設定の入切では変えない（変えると全資料の再変換になる）。
設計: docs/design/rag.md「OCR（非同期・隔離ワーカー）」
"""
from __future__ import annotations

import os
from typing import Any

ENV_NAME = "SHERPA_OCR_ENABLED"
SETTING_KEY = "ocr_enabled"
DEFAULT_ENABLED = False
_TRUE = frozenset({"1", "true", "yes", "on"})


def env_override() -> bool | None:
    """環境変数で切に固定されていれば False。未設定・空・入の値（`1` など）は None（全体設定に従う）。
    `1` を固定として扱わないのは、`.env.example` から複写された `SHERPA_OCR_ENABLED=1` が既存の環境で画面の設定を覆わないようにするため。
    """
    raw = os.environ.get(ENV_NAME)
    if raw is None or not raw.strip():
        return None
    return False if raw.strip().lower() not in _TRUE else None


def convert_mode_on() -> bool:
    """変換の出力と PDF の変換キャッシュの鍵に使う OCR の形。環境変数だけで決まる（未設定は on・`0` などは off）。全体設定は見ない。"""
    override = env_override()
    return True if override is None else override


def enabled(settings: dict[str, Any] | None = None) -> bool:
    """OCR が入か。環境変数での切の固定 > 全体設定 > 既定（切）。`settings` を渡すと DB を読まずにその値を使う。"""
    override = env_override()
    if override is not None:
        return override
    if settings is None:
        from .. import store
        settings = store.get_system_settings()
    return settings.get(SETTING_KEY) is True


def lease_next(*args, **kwargs):
    """切のあいだは仕事を取らない。入なら `ocr_jobs.lease_next`。"""
    if not enabled():
        return None
    from ..store import ocr_jobs
    return ocr_jobs.lease_next(*args, **kwargs)


def lease_refresh_run(*args, **kwargs):
    """切のあいだは再展開の仕事を取らない。入なら `ocr_jobs.lease_refresh_run`。"""
    if not enabled():
        return None
    from ..store import ocr_jobs
    return ocr_jobs.lease_refresh_run(*args, **kwargs)


def admin_view(sysset: dict[str, Any]) -> dict[str, Any]:
    """管理画面（GET /admin/settings）の `ocr`。"""
    return {"configured": sysset.get(SETTING_KEY), "effective": enabled(sysset),
            "default": DEFAULT_ENABLED, "env_override": env_override()}


def routes_wanted(derived) -> bool:
    """資料 1 件の差し替えで OCR の対象づくりをするか。切のときは、作らなかった資料を入れ直したときに拾い直せるよう、ルート版の確定の印を消す。"""
    if enabled():
        return True
    from pathlib import Path
    from . import ocr_router
    try:
        (Path(derived) / ocr_router.OCR_ROUTE_SIG_MARKER).unlink(missing_ok=True)
    except OSError:
        pass
    return False
