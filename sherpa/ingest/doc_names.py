"""1 文書の入れ直しで、目録と名前の台帳のその文書の行を同じトランザクションで入れ替える。
名前の台帳の行は、作り直したグラフのノード・線から全体の取り込みと同じ関数（`name_rows.build_rows`）で作り、その文書の分に絞る。
設計: docs/design/scope.md「期の中の参照の張り方と名前の台帳」
"""
from __future__ import annotations

from typing import Callable

from .. import store
from ..store import replace_rows
from ..store.replace_rows import ReplaceRow
from . import name_rows

LEDGER_COLUMNS = ("layer", "scope_path", "doctype", "branch", "original_path", "md_path", "status",
                  "importance", "importance_reason", "importance_source")


def capture(build: Callable, out: list | None) -> Callable:
    """グラフを組む関数 `build` を、組んだ `(ノード, 線)` を `out` に残す関数にする（`out` が None なら `build` のまま）。"""
    if out is None:
        return build

    def _build():
        result = build()
        out[:] = [result[0], result[1]]
        return result
    return _build


def rows_for_doc(rel: str, doc: dict | None, nodes: list, edges: list) -> list:
    """文書 `rel` の名前の台帳の行。文書が無ければ空。"""
    if doc is None:
        return []
    return [r for r in name_rows.build_rows([doc], nodes, edges) if r["rel_path"] == rel]


def swap(world: str, rel: str, new: dict | None, graph: list, *, row: ReplaceRow) -> None:
    """目録の `rel` の行（`new`・None なら消す）と名前の台帳の `rel` の行を、今見せる番号の中で同じトランザクションで入れ替える。
    どちらも変わらなければ書かない。`graph` は作り直したグラフの `[ノード, 線]`。
    """
    names = rows_for_doc(rel, new, *graph)
    old = store.get_document(world, rel)
    same = (old is None and new is None) or (old is not None and new is not None
                                             and all(old.get(k) == new.get(k) for k in LEDGER_COLUMNS))
    if same and store.document_names_match(world, rel, names):
        replace_rows.settle_item(row, world, "ledger")
        return
    store.replace_document(world, rel, new, names=names, row=row)
