"""原本のパスの検証と解決（資料フォルダ root 相対の POSIX パス → 原本 Path）。symlink と root の外への脱出を拒否する。"""
from __future__ import annotations

import errno
import os
import stat as stat_mod
from pathlib import Path


_ABSENT_ERRNOS = (errno.ENOENT, errno.ENOTDIR, errno.ENAMETOOLONG)  # 「そこに無い」ことを表す errno（権限・I/O 障害は含めない）


def _lstat_kind(p, strict: bool = False) -> str | None:
    """`os.lstat()` ベースで種別を返す（`"dir"`/`"file"`/`"symlink"`/`None`＝不在扱い）。

    `Path.is_dir()` 等は `OSError` を握るため使わない。`OSError` は種別不明＝`None` にする。
    `strict=True` は、無いこと（`_ABSENT_ERRNOS`）と読めないこと（権限・I/O 障害）を区別し、後者を re-raise する。
    """
    try:
        st = os.lstat(p)
    except OSError as e:
        if strict and e.errno not in _ABSENT_ERRNOS:
            raise
        return None
    if stat_mod.S_ISLNK(st.st_mode):
        return "symlink"
    if stat_mod.S_ISDIR(st.st_mode):
        return "dir"
    if stat_mod.S_ISREG(st.st_mode):
        return "file"
    return None


def valid_rel_parts(rel: str) -> tuple[str, ...] | None:
    """`rel`（資料フォルダ root 相対 POSIX パス）の文字列検証だけを行う（FS アクセス無し）。

    絶対パス・`\\`・NUL・空/`.`/`..` 要素はすべて拒否し、通れば `"/"` 区切りのセグメント列を返す。
    `resolve_path` と `ext_api._doc_path_segments`（原本 DL 配信）が、正規化の真実源としてこの 1 関数だけを共有する。
    """
    if not rel or rel.startswith("/") or "\\" in rel or "\x00" in rel:
        return None
    parts = tuple(rel.split("/"))
    if any(p in ("", ".", "..") for p in parts):
        return None
    return parts


def resolve_path(world_dir, rel: str, strict: bool = False):
    """`rel`（資料フォルダ root 相対 POSIX）→ 原本 Path（パス基準・無ければ None）。`strict=True` は読めない（権限・I/O 障害）ときを None にせず `OSError` を送出する。

    ① `rel` を `valid_rel_parts` で検証する（FS アクセスより前）。
    ② root から `rel` の各階層へ直接 `os.lstat` して降りる（資料フォルダ全体は走査しない）。途中経路に symlink があれば拒否する。
    ③ 解決後パスが root 配下に収まることを再確認する（脱出防止）。
    """
    parts = valid_rel_parts(rel)
    if parts is None:
        return None
    root = Path(world_dir)
    if _lstat_kind(root, strict) != "dir":
        return None
    cur = root
    for i, part in enumerate(parts):
        cur = cur / part
        kind = _lstat_kind(cur, strict)
        if i == len(parts) - 1:
            if kind != "file":
                return None
        elif kind != "dir":
            return None
    try:
        rp = cur.resolve()
        rootr = root.resolve()
    except OSError:
        if strict:
            raise
        return None
    if not rp.is_relative_to(rootr):
        return None
    return rp
