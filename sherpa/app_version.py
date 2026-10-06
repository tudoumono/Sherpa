"""アプリの版（利用統計 `activity.app_version`）。

リポ直下 `VERSION` の内容＋取れた場合だけ git の短い SHA を `"+<SHA>"` で付ける（例 `"0.11.4+e87a6750"`）。
`VERSION` に既に `+<コミット>` があれば（書き出し・パッケージ由来）そのまま使う。
`VERSION` が読めなければ `None`。起動後1回だけ計算してキャッシュする。
"""
from __future__ import annotations

import subprocess
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_GIT_TIMEOUT_S = 2.0

# 結果が None でも計算済みと区別するため `_computed` を別に持つ
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
    """VERSION＋取れた場合だけ `"+<短いSHA>"`。`VERSION` が読めなければ `None`。"""
    global _cached, _computed
    if not _computed:
        version = _read_version()
        if version is not None:
            sha = None if "+" in version else _short_sha()
            _cached = f"{version}+{sha}" if sha else version
        else:
            _cached = None
        _computed = True
    return _cached
