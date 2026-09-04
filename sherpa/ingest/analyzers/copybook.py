"""COBOL コピーブック アナライザ（docs/05-グラフ語彙.md §4 トラック S）。

ファイル自体を主体定義（`Copybook`）とし、レベル項目（01/05..）を子定義（`DataItem`）として返す。
COBOL レベルスタックで修飾名（`GROUP.ITEM`）を作り同名衝突を避ける（同一 copybook 内の
FILLER/66/88 は対象外）。コピーブック自身は他ファイルを参照しない（`extract_refs` は空）。
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import COPYBOOK_EXT, _ITEM, _VALUE, _is_comment
from ._base import Analyzer, DefItem, DefResult, RefResult


class CopybookAnalyzer(Analyzer):
    """コピーブック自身 → `Copybook`。レベル項目 → `DataItem`（`Copybook -CONTAINS-> DataItem`）。"""

    name = "copybook"
    extensions = frozenset(COPYBOOK_EXT)
    doctype = "copybook"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        cb = _norm(PurePosixPath(rel_path).stem)
        children: list = []
        stack: list = []                              # (level:int, name) ＝COBOL レベルスタックで修飾名
        for i, line in enumerate(text.splitlines(), 1):
            if _is_comment(line):
                continue
            m = _ITEM.match(line)
            if not m:
                continue
            if m.group(1) in ("66", "88") or _norm(m.group(2)) == "FILLER":
                continue
            level, item = int(m.group(1)), _norm(m.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            qualified = ".".join([s[1] for s in stack] + [item])   # GROUP.ITEM（同名衝突回避）
            stack.append((level, item))
            mv = _VALUE.search(line)
            children.append(DefItem(label="DataItem", name=item, cid_key=qualified,
                                     value=mv.group(1) if mv else None, line=i,
                                     extra={"qualified": qualified}))
        return DefResult(primary=DefItem(label="Copybook", name=cb), children=children)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()
