"""world 文書の本文テキスト取得（派生MD 優先・無ければソース原本）。

抽出（graph_extract）と全文索引（es_index）が同じ責務を別々に実装していたのを集約（rv-full (2)）。
派生MD（Office→決定的MD 等）があればそれを、無ければ world 配下のソース原本を読む。読めなければ None。
"""
from __future__ import annotations

from pathlib import Path

from . import worlds


def read_world_doc_text(world: str, d: dict) -> str | None:
    """文書 dict `d`（`md_path`/`name` を持つ台帳/走査行）の本文。派生MD 優先・無ければ world 配下の原本。"""
    p = Path(d["md_path"]) if d.get("md_path") else None
    if p is None:
        wd = worlds.world_dir(world)
        p = (Path(wd) / d["name"]) if wd else None
    if not p or not p.is_file():
        return None
    try:
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
