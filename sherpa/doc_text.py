"""world 文書の本文テキスト取得。派生MD があればそれを、無ければ world 配下のソース原本を読む（読めなければ None）。"""
from __future__ import annotations

from pathlib import Path

from . import corpus_docs, worlds


def read_world_doc_text(world: str, d: dict) -> str | None:
    """文書 dict `d` の本文（派生MD 優先・無ければ原本）。"""
    p = Path(d["md_path"]) if d.get("md_path") else None
    if p is None:
        wd = worlds.world_dir(world)
        p = (Path(wd) / d["name"]) if wd else None
    if not p or not p.is_file():
        return None
    try:
        return corpus_docs.read_full_text_and_raw(p)[0]
    except OSError:
        return None
