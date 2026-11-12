"""取り込みの台帳の書き込みの段。取り込みの台帳（`documents`）と名前の台帳（`name_ledger`）を同じ時点で入れ替える。
設計: docs/design/data.md「KB（取り込み・範囲・同一性）」・docs/design/scope.md「期の中の参照の張り方と名前の台帳」
"""
from __future__ import annotations

import logging

from .. import store
from . import name_rows, substage

_log = logging.getLogger("sherpa.ingest.convert")


def write_ledger_step(world: str, rows: list, nodes: list, *, row, edges=()) -> int:
    """台帳の入れ替え（`store.replace_documents`）と名前の台帳の入れ替えを、小さな段（台帳を書く）として進み具合に出す。
    グラフの書き込みが失敗した取り込みはここへ来ない（台帳と名前の台帳はグラフと食い違わない）。
    """
    names = name_rows.build_rows(rows, nodes, edges)   # 先に組み立て、両方の台帳を同じトランザクションで入れ替える
    with substage.step("ledger_write", "台帳を書いています", total=len(rows)) as st:
        written = store.replace_documents(world, rows, names=names, row=row)
        st.tick(written)
        return written


def stage_ledger_step(world: str, rows: list, nodes: list, *, row, edges=()) -> int:
    """目録と名前の台帳の行を、新しい番号の下書きに入れる（見せている目録はそのまま・グラフの入れ替えより前）。名前の台帳の行はここでグラフのノードから組み立てる。
    下書きを入れ始める前に、見せている番号とこの回の番号以外の古い番号の行を消し終える（1 つの資料フォルダの行は多くても 2 回分）。
    """
    names = name_rows.build_rows(rows, nodes, edges)
    store.purge_stale_ledger(world, row=row, keep_draft=True)
    with substage.step("ledger_write", "台帳の下書きを書いています", total=len(rows)) as st:
        written = store.stage_ledger(world, rows, names, row=row)
        st.tick(written)
        return written


def switch_ledger_step(world: str, count: int, *, row) -> int:
    """下書きの番号を今見せる番号に切り替え（グラフの入れ替えの後）、切り替え前の古い番号の行を別のトランザクションで小分けに消す。`count` は下書きの行数で、そのまま返す。
    消し残しは次の下書きの前に消すので、ここでの失敗は取り込みを止めずログに残す。
    """
    store.switch_ledger(world, row=row)
    try:
        store.purge_stale_ledger(world, row=row, keep_draft=False)
    except Exception:
        _log.warning("古い番号の目録の行を消し切れませんでした（次の取り込みの前に消します）: world=%s", world, exc_info=True)
    return count
