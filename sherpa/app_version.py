"""アプリの版（利用統計 `activity.app_version`・利用統計の刷新 提案書 §3.1）。

リポ直下 `VERSION` の内容＋取れた場合だけ git の短い SHA を `"+<SHA>"` で付ける
（例: `"0.11.4+e87a6750"`）。git が無い/失敗/タイムアウト（2秒）した環境は VERSION のみ返す。
`VERSION` 自体が読めない環境は `None`（プレースホルダ値で埋めない＝呼び出し側はキー自体を
置かない契約）。起動後1回だけ計算しプロセス内にキャッシュする（毎ターン `git rev-parse` を
起動しない）。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT_TIMEOUT_S = 2.0

# `_cached is None` を「未計算」の目印に使えない（結果そのものが None になり得るため・
# `_computed` で計算済みかどうかを別に持つ）。
_cached: str | None = None
_computed: bool = False


def _read_version() -> str | None:
    try:
        return (_REPO_ROOT / "VERSION").read_text(encoding="utf-8").strip()
    except OSError:
        return None


def _short_sha() -> str | None:
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--short=8", "HEAD"],
            cwd=_REPO_ROOT, capture_output=True, text=True, timeout=_GIT_TIMEOUT_S)
    except (OSError, subprocess.SubprocessError):
        return None
    if proc.returncode != 0:
        return None
    sha = proc.stdout.strip()
    return sha or None


def current() -> str | None:
    """VERSION＋取れた場合だけ `"+<短いSHA>"`。`VERSION` が読めなければ `None`（推定で埋めない）。
    プロセス内で1回だけ計算する。"""
    global _cached, _computed
    if not _computed:
        version = _read_version()
        if version is not None:
            sha = _short_sha()
            _cached = f"{version}+{sha}" if sha else version
        else:
            _cached = None
        _computed = True
    return _cached
