"""取込元パスの変換（P1b-2）。Windows/NAS パス → WSL `/mnt` パス（純関数）。

大量取り込みは Windows ホストのパスを登録し WSL `/mnt` 経由でスキャンする（UC-A2）。
ここは**変換だけ**（実在確認・走査は呼び出し側で `safe_files` を使う＝shell に渡さない）。
不正（相対・空・`..`・制御文字・ドライブ無し）は **None** を返す（fail-closed）。
特定テーマの名前は持たない。
"""
from __future__ import annotations

import re

_DRIVE = re.compile(r"^([A-Za-z]):[\\/](.*)$")          # 例: D:\x\y / D:/x/y
_UNC = re.compile(r"^[\\/]{2}([^/\\]+)[\\/](.+)$")      # 例: \\nas\share\x（host に区切り不可・両区切り対応）


def _clean_parts(rest: str) -> list | None:
    """区切り正規化＋成分検証。`..`/空成分（連続区切り除く）/制御文字を拒否。"""
    parts = [p for p in rest.replace("\\", "/").split("/")]
    out = []
    for p in parts:
        if p == "":                                 # 連続/末尾の区切りは無視
            continue
        if p == "." or p == "..":                   # トラバーサル拒否
            return None
        if any(ord(c) < 32 for c in p):             # 制御文字拒否
            return None
        out.append(p)
    return out


def windows_to_wsl_path(win_path: str) -> str | None:
    """`D:\\取込\\5期` → `/mnt/d/取込/5期`、`\\\\nas\\share\\x` → `/mnt/nas/share/x`。

    不正（空・相対・`D:relative`（区切り無し）・`..`成分・制御文字）は **None**。
    """
    if not win_path or not isinstance(win_path, str):
        return None
    s = win_path.strip()
    m = _DRIVE.match(s)
    if m:
        drive, rest = m.group(1).lower(), m.group(2)
        parts = _clean_parts(rest)
        if parts is None:
            return None
        return "/".join(["/mnt", drive, *parts])
    u = _UNC.match(s)
    if u:
        host, rest = u.group(1), u.group(2)
        if any(ord(c) < 32 for c in host) or host in (".", ".."):
            return None
        parts = _clean_parts(rest)
        if parts is None or not parts:
            return None
        return "/".join(["/mnt", host, *parts])
    return None                                     # 相対・ドライブ無し・形式不明は拒否
