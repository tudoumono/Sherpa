"""止まった取り込みの再開で、入れ替え済みの項目を飛ばす部品。
行が「入れ替えた」以降で、作ったもの自身の番号と設定の値が行と一致する項目（`done`）は、作り直さず古いものの掃除だけをして「古いものを消した」にする。
設計: docs/design/operations.md「止まった取り込みの見え方」
"""
from __future__ import annotations

import logging
import shutil
from pathlib import Path

from .. import json_io, worlds
from ..store import replace_rows
from ..store.replace_rows import ITEM_CLEANED, ITEM_SWAPPED, ReplaceRow
from . import derived_numbers

_log = logging.getLogger("sherpa")

LAYERS = ("ir", "rag", "md")


class OldNotCleared(RuntimeError):
    """前の回が残した古いものを消し終えられなかった（行には「失敗」を残す）。"""


def _own_numbers(world: str, item: str) -> tuple | None:
    """作ったもの自身に書かれた `(取り込みの番号, 設定の値)`。無い・読めなければ None。"""
    if item == "archives":
        m = json_io.read_json(worlds.archive_manifest_path(world), default=None)
        return (m.get("ingest_no"), m.get("settings_sig")) if isinstance(m, dict) else None
    if item in LAYERS:
        rec = derived_numbers.read(derived_numbers.layer_dir(worlds.derived_md_dir(world), item))
        return (rec.get("ingest_no"), rec.get("settings_sig")) if rec else None
    if item == "graph":
        from . import world_neo4j_write
        meta = world_neo4j_write.read_swap_state(world)
        return (meta["ingest_no"], meta["settings_sig"]) if meta else None
    if item == "es":
        from .. import es_index
        meta = es_index._index_meta(world) or {}
        return (int(meta.get("ingest_no") or 0), meta.get("settings_sig")) if meta else None
    if item == "ledger":
        return (replace_rows.shown_ingest_no(world), None)
    return None


def done(row: ReplaceRow | None, item: str) -> bool:
    """行が `item` を「入れ替えた」以降にしていて、かつ作ったもの自身の番号と設定の値が行と一致するか。"""
    if row is None or row._offline is not None:
        return False
    cur = replace_rows.read_row(row.id)
    if cur is None or cur.items.get(item) not in (ITEM_SWAPPED, ITEM_CLEANED):
        return False
    try:
        own = _own_numbers(cur.world, item)
    except Exception:
        _log.warning("作ったものの番号を読めませんでした（作り直します）: world=%s item=%s", cur.world, item, exc_info=True)
        return False
    want_sig = None if item == "ledger" else replace_rows.settings_sig(cur, item)
    return own is not None and own[0] == cur.ingest_no and (item == "ledger" or own[1] == want_sig)


def _tidy(world: str, item: str) -> bool:
    """`item` の古いもの（前の回が残した旧世代・旧索引・退避先）を消す。公開中のものには触れない。消せたら True。"""
    if item == "graph":
        from . import world_neo4j_write
        world_neo4j_write.drop_inactive(world)
    elif item == "es":
        from .. import es_publish
        return es_publish.drop_stale_physicals(world)
    elif item in LAYERS:
        from . import derived_swap
        target = derived_numbers.layer_dir(worlds.derived_md_dir(world), item)
        derived_swap._recover_interrupted_swap(target)   # 改名 2 回の間で止まったときの唯一の旧世代を、消す前に公開中へ戻す
        try:
            shutil.rmtree(target.with_name(target.name + derived_swap._RETIRED_SUFFIX))
        except FileNotFoundError:   # 無いのは消し済み。それ以外の失敗は伝える（掃除済みとして控えを消さない）
            pass
    return True


def skip(row: ReplaceRow | None, item: str) -> bool:
    """`item` が入れ替え済みなら、古いものを消して「古いものを消した」にして True を返す。消せなければ False（その項目は作り直す）。"""
    if not done(row, item):
        return False
    if item == "ledger":
        return _tidy_ledger(row)
    try:
        if not _tidy(row.world, item):
            return False
    except Exception:
        _log.warning("古いものを消せませんでした（作り直します）: world=%s item=%s", row.world, item, exc_info=True)
        return False
    replace_rows.after_old_removed(row, row.world, item)
    return True


def _tidy_ledger(row: ReplaceRow) -> bool:
    """切り替え済みの目録の、見せている番号以外の古い番号の行を消し切る。消し切れなければ項目を「失敗」にして行を残す（飛ばしてよいので True）。"""
    from .. import store
    try:
        store.purge_stale_ledger(row.world, row=row, keep_draft=False)
    except Exception:
        _log.warning("古い番号の目録の行を消せませんでした（行を残します）: world=%s", row.world, exc_info=True)
        replace_rows.mark_failed(row, row.world, "ledger", "old_ledger_not_cleared")
        return True
    replace_rows.after_old_removed(row, row.world, "ledger")
    return True


def note_derived(row: ReplaceRow | None, rep: dict) -> None:
    """派生を作った結果の、作れなかった文書を行に控える（再開が派生を飛ばしても失敗の一覧を戻せるように）。"""
    if row is not None and not rep.get("resumed") and not rep.get("error"):
        from .failed_docs_derive import failed_doc_reasons
        replace_rows.note_failed_docs(row, row.world, failed_doc_reasons(rep))


def derived_if_done(row: ReplaceRow | None, world: str) -> dict | None:
    """派生の 3 層とも入れ替え済みで、その番号の失敗の一覧を行に控えてあれば、作り直さずに済ませた結果を返す。
    戻せなければ None で作り直す（作り直す前に古い控えを外す。公開がふつうに終わったときだけ `note_derived` が控え直す）。
    """
    if row is None or row._offline is not None:
        return None
    cur = replace_rows.read_row(row.id)
    saved = (cur.settings if cur else {}).get(replace_rows.FAILED_DOCS_KEY)
    if isinstance(saved, dict) and all(skip(row, layer) for layer in LAYERS):
        return {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}, "resumed": True,
                "rag_failures": [{"doc": rel, "reason": why} for rel, why in saved.items()]}
    if saved is not None:
        replace_rows.forget_failed_docs(row, world)
    return None


def loaded_graph(row: ReplaceRow | None) -> tuple | None:
    """グラフが入れ替え済みなら、切り替えを飛ばして公開中のメタの件数 `(ノード, 辺)` を返す。違えば None。"""
    if not skip(row, "graph"):
        return None
    from . import world_neo4j_write
    meta = world_neo4j_write.read_swap_state(row.world) or {}
    return int(meta.get("node_count") or 0), int(meta.get("edge_count") or 0)


def indexed_es(row: ReplaceRow | None) -> dict | None:
    """ES が入れ替え済みなら、作り直さずに済ませた結果を返す（途中からは始めない）。違えば None。"""
    if not skip(row, "es"):
        return None
    from .. import es_index
    n = es_index.count(row.world)
    return {"available": True, "error": None, "chunks": n, "indexed": n, "resumed": True}


def clear_old(row: ReplaceRow) -> None:
    """原本か設定が変わった再開で、前の回が残した古いものを、新しい番号に置き換える前に消す。消し終えられなければ項目を「失敗」にして `OldNotCleared`。"""
    for item in sorted(set(row.old_things) & {"graph", "es", *LAYERS}):
        try:
            ok = _tidy(row.world, item)
        except Exception:
            _log.warning("古いものを消せませんでした: world=%s item=%s", row.world, item, exc_info=True)
            ok = False
        if not ok:
            replace_rows.mark_failed(row, row.world, item, "old_things_not_cleared")
            raise OldNotCleared(f"古いものを消し終えられませんでした: world={row.world} item={item}")
        replace_rows.forget_old(row, row.world, item)
