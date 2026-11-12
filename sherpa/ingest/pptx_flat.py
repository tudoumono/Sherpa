"""PPTX のスライドの図形をたどれなかったときの、平らな文字での取り込み。

設計: docs/design/simple.md「取り込み」
"""
from __future__ import annotations

import logging
from collections.abc import Callable
from pathlib import Path

_log = logging.getLogger(__name__)

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
FLAT_NOTE = "※ このスライドは図形の取り消し線・重なりを読み取れていません（文字だけを取り出しています）。"
NOTES_KEY = "pptx_flat_slides"


def flat_texts(root) -> list[str]:
    """スライドの全文字を図形の区別なく文書順に拾う。"""
    return [t.text.strip() for t in root.iter(f"{_A}t") if t.text and t.text.strip()]


def slide_texts(walk: Callable[[], list[str]], root, figures: dict | None, figure_line: Callable[[list[str]], str],
                path: Path, index: int) -> list[str]:
    """図形をたどる `walk` が失敗したら、注記つきで平らな文字を返す（図の文字はスライド末尾へまとめる）。"""
    try:
        return walk()
    except Exception as e:
        _log.warning("PPTX の図形をたどれず平らな文字で取り込みました: %s スライド %d（%s）", path.name, index, e.__class__.__name__)
        figure_blocks = [figure_line(lines) for blocks in (figures or {}).values() for lines in blocks]
        return [FLAT_NOTE, *flat_texts(root), *figure_blocks]


def count_flat_slides(md: str | None) -> int:
    """人用 Markdown にある平らに読んだスライドの数。"""
    return md.count(FLAT_NOTE) if md else 0


def provenance_note(md: str | None) -> list[str]:
    """来歴の notes に足す行（平らに読んだスライドが無ければ空）。"""
    n = count_flat_slides(md)
    return [f"{NOTES_KEY}={n}"] if n else []
