"""COBOL コピーブック アナライザ。ファイル自体を主体定義（`Copybook`）、レベル項目を子定義（`DataItem`）として返す。

レベルスタックで修飾名（`GROUP.ITEM`）を作り同名衝突を避ける。66/88 は項目にせずスタックも動かさない。FILLER は項目にしないが、同じレベル以上をスタックから閉じる境界として扱い（名前の無いグループとして修飾名に入れない）、後続の項目が直前の別グループの配下にならないようにする。
コピーブックの中の `COPY` は `Copybook` 参照（COPIES）として返す（入れ子の COPY。`CALL` など他の参照は持たない）。自分自身の `COPY` は辺にせず `Dropped("copy_self_reference")` で申告する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

from pathlib import PurePosixPath

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (COPYBOOK_EXT, _COPY, _ITEM, _VALUE, _is_comment,
                               _blank_pseudo_text, _is_free_format, _normalize_logical_lines,
                               _strip_inline_comment, _strip_quoted)
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult


class CopybookAnalyzer(Analyzer):
    """コピーブック自身 → `Copybook`。レベル項目 → `DataItem`（`Copybook -CONTAINS-> DataItem`）。"""

    name = "copybook"
    extensions = frozenset(COPYBOOK_EXT)
    doctype = "copybook"
    version = 2

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
            if m.group(1) in ("66", "88"):
                continue
            level, item = int(m.group(1)), _norm(m.group(2))
            while stack and stack[-1][0] >= level:
                stack.pop()
            if item == "FILLER":
                stack.append((level, None))  # 名前の無いグループ＝境界（修飾名には入れない）
                continue
            qualified = ".".join([s[1] for s in stack if s[1]] + [item])
            stack.append((level, item))
            mv = _VALUE.search(logical)
            children.append(DefItem(label="DataItem", name=item, cid_key=qualified,
                                     value=mv.group(1) if mv else None, line=i,
                                     extra={"qualified": qualified}))
        dropped = [Dropped("debug_line", ln, snippet) for ln, snippet in debug_dropped]
        return DefResult(primary=DefItem(label="Copybook", name=cb), children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        refs: list = []
        dropped: list = []
        self_name = _norm(PurePosixPath(rel_path).stem)
        entries, _debug = _normalize_logical_lines(text, _is_free_format(text))
        for logical, i, _segs in entries:
            if _is_comment(logical):
                continue
            for cb in _COPY.findall(_strip_quoted(_blank_pseudo_text(_strip_inline_comment(logical)))):
                if _norm(cb) == self_name:
                    dropped.append(Dropped("copy_self_reference", i, cb))
                    continue
                refs.append(RefCandidate("COPIES", "Copybook", _norm(cb), i))
        return RefResult(refs=refs, dropped=dropped)
