"""COBOL コピーブック アナライザ。ファイル自体を主体定義（`Copybook`）、レベル項目を子定義（`DataItem`）として返す。

レベルスタックで修飾名（`GROUP.ITEM`）を作り同名衝突を避ける（FILLER/66/88 は対象外）。他ファイルは参照しない（`extract_refs` は空）。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (COPYBOOK_EXT, _ITEM, _VALUE, _is_comment,
                               _is_free_format, _normalize_logical_lines)
from ._base import Analyzer, DefItem, DefResult, Dropped, RefResult


class CopybookAnalyzer(Analyzer):
    """コピーブック自身 → `Copybook`。レベル項目 → `DataItem`（`Copybook -CONTAINS-> DataItem`）。"""

    name = "copybook"
    extensions = frozenset(COPYBOOK_EXT)
    doctype = "copybook"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        cb = _norm(PurePosixPath(rel_path).stem)
        children: list = []
        stack: list = []  # (level:int, name) ＝レベルスタック
        free_format = _is_free_format(text)
        # `_ITEM` は行頭アンカーのため、正規化済みの論理行（連番除去済み）に適用する（自由形式には適用しない）。
        entries, debug_dropped = _normalize_logical_lines(text, free_format)
        for logical, i, _segs in entries:
            if _is_comment(logical):
                continue
            m = _ITEM.match(logical)
            if not m:
                continue
            if m.group(1) in ("66", "88") or _norm(m.group(2)) == "FILLER":
                continue
            level, item = int(m.group(1)), _norm(m.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            qualified = ".".join([s[1] for s in stack] + [item])
            stack.append((level, item))
            mv = _VALUE.search(logical)
            children.append(DefItem(label="DataItem", name=item, cid_key=qualified,
                                     value=mv.group(1) if mv else None, line=i,
                                     extra={"qualified": qualified}))
        dropped = [Dropped("debug_line", ln, snippet) for ln, snippet in debug_dropped]
        return DefResult(primary=DefItem(label="Copybook", name=cb), children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()
