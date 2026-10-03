"""symlink 差し替え（TOCTOU）耐性を持つファイル/ディレクトリ open の共通ヘルパ。stdlib のみに依存する葉ノード。
`agentic_search.py` と `ext_api.py` が、検証を通った後の実ファイル open に使う。
"""
from __future__ import annotations

import os
from pathlib import Path


def open_file_nofollow_walk(anchor: Path, rel_parts: tuple) -> int:
    """信頼済みディレクトリ `anchor` を起点に、`rel_parts` を dir_fd 相対で1段ずつ `O_NOFOLLOW` で開き、最終ファイルの fd を返す。

    中間ディレクトリは `O_DIRECTORY|O_NOFOLLOW`、最終要素は `O_RDONLY|O_NOFOLLOW|O_NONBLOCK`（FIFO で塞がらない）。
    どの段が symlink でも `OSError`（`ELOOP`）で拒否する（fail-closed）。`anchor` 自身も `/` から1段ずつ辿り、
    lexical 正規化は `os.path.abspath()` のみ（`resolve()` は使わない）。呼び出し元は `rel_parts` を resolve 前の lexical パスから組む。
    呼び出し元は `OSError` を読み取り失敗として処理する。
    """
    if not rel_parts:
        raise ValueError("rel_parts が空です")
    # anchor に `..` を含むと検証対象と open 対象が食い違いうるため fail-closed で拒否する
    if ".." in Path(anchor).parts:
        raise OSError(f"anchor に '..' 要素が含まれています（fail-closed・FIX-V）: {anchor}")
    anchor_abs = Path(os.path.abspath(anchor))  # 純 lexical 正規化（FS アクセスなし・resolve は使わない）
    fd = os.open(os.sep, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        for part in anchor_abs.parts[1:]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        for part in rel_parts[:-1]:
            next_fd = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = next_fd
        file_fd = os.open(rel_parts[-1], os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=fd)
        return file_fd
    finally:
        os.close(fd)
