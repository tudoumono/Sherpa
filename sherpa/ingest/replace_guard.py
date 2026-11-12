"""入れ替えの行の受け取りと、行を消してよいかの最後の確かめ。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

from contextlib import contextmanager

from .. import store
from ..store import replace_rows
from ..store import settings as store_settings
from ..store.replace_rows import DropToken, ReplaceRow
from . import source_guard, swap_resume
from .swap_resume import derived_if_done, done, indexed_es, loaded_graph, note_derived, skip   # noqa: F401  再開で入れ替え済みの項目を飛ばす入口


def checked(row: ReplaceRow | None) -> ReplaceRow | None:
    """入れ替えに入る前に原本と設定を確かめる処理（`source_guard.verify`）を付けた行を返す（行が無ければ None）。"""
    return row if row is None or row._offline is not None else replace_rows.attach_check(row, source_guard.verify)


def settings_pinned():
    """with の間、`get_system_settings` の値を始めの 1 つに固定する（取り込み 1 回の作りかけが途中で変わった設定を混ぜない）。"""
    return store_settings.pinned_system_settings()


def row_for_run(world: str, run_id: int | None) -> ReplaceRow | None:
    """この回が拾った行を DB から読む。回の id が無い、または行が無ければ None。"""
    if run_id is None:
        return None
    return checked(replace_rows.read_row_for_run(world, run_id))


def fix_sources(row: ReplaceRow | None, sig: str, manifest: dict, *, targets: tuple = replace_rows.ITEMS_ALL) -> ReplaceRow | None:
    """走査で確かめた原本と、ものごとの設定の値を行に確定し、確定した後の行を返す（行が無ければ None）。`store.world_lock` の中で呼ぶ。以後の処理には返した行を渡す。"""
    if row is None:
        return None
    settings, renumber = _settings_of(row), False
    cur = None if row._offline is not None or row.doc else replace_rows.read_row(row.id)
    if cur is not None:
        _check_own(row, cur)
    if cur is not None and cur.kind == "build" and cur.source_state == "fixed":
        if _same_fix(cur, sig, settings):
            replace_rows.keep_fixed(cur, targets)   # 止まった回の続き: 同じ番号のまま、入れ替え済みの項目は飛ばす
            return _confirmed(row)
        swap_resume.clear_old(cur)                  # 原本か設定が変わった: 古いものを消し終えてから新しい番号に置き換える
        renumber = True
    replace_rows.fix_sources(row, sig, manifest, settings, targets=targets, renumber=renumber)
    return _confirmed(row)


def _same_fix(cur: ReplaceRow, sig: str, settings: dict | None) -> bool:
    """確定済みの行が、今の原本の署名と設定の値のままか（走査しない回〈規則版・書き換え〉の行は続きにしない）。"""
    if cur.source_sig != sig or cur.settings.get(source_guard.SOURCE_MODE_KEY) == source_guard.SOURCE_LAST:
        return False
    return all((settings or {}).get(k) == v for k, v in cur.settings.items() if not k.startswith("_"))


def _confirmed(row: ReplaceRow) -> ReplaceRow:
    """確定した後の行を読み直して、確かめの処理を付け直す。試験の手元の行はそのまま返す。読み直した行の lock の鍵が元の行と違えば `RowLockLost`。"""
    if row._offline is not None:
        return row
    cur = replace_rows.read_row(row.id)
    _check_own(row, cur)
    return checked(cur)


def _check_own(row: ReplaceRow, cur: ReplaceRow | None) -> None:
    """読み直した行が、受け取った行と同じ lock の鍵で、その lock を今も誰かが持っているか。違えば `RowLockLost`（別の回が取り直した鍵で書き込まない）。"""
    if cur is None or (cur.lock_ns, cur.lock_id) != (row.lock_ns, row.lock_id) or not replace_rows.lock_held(cur):
        raise replace_rows.RowLockLost(f"行の lock が自分のものではありません: world={row.world} row={row.id}")


def rewrite_settings(world: str) -> dict:
    """LLM での書き換えの行に確定する設定の値（ものごとの値と書き換えの接続先・モデル。走査せず前回取り込んだ署名と比べる印を付ける）。"""
    return {**source_guard.current_settings(world), source_guard.SOURCE_MODE_KEY: source_guard.SOURCE_LAST}


def _settings_of(row: ReplaceRow, *, last: bool = False) -> dict | None:
    """行に確定する設定の値。試験の手元の行は DB や設定を読まない。`last` は走査せず前回取り込んだ署名と比べる回の印を付ける。"""
    if row._offline is not None:
        return None
    return {**source_guard.current_settings(row.world), **({source_guard.SOURCE_MODE_KEY: source_guard.SOURCE_LAST} if last else {})}


def fix_sources_from_last(row: ReplaceRow | None, *, targets: tuple) -> ReplaceRow | None:
    """原本を走査しない回が、前回取り込んだ署名と一覧を行に確定し、確定した後の行を返す（行が無ければ None）。`store.world_lock` の中で呼ぶ。以後の処理には返した行を渡す。"""
    if row is None:
        return None
    replace_rows.fix_sources_from_last(row, _settings_of(row, last=True), targets=targets)
    return _confirmed(row)


def nothing_pending(row: ReplaceRow | None) -> bool:
    """変更なしの更新で済ませてよい行か。行が無い、または原本を集めている最中のまま（何も入れ替えていない）で、止まった 1 文書の行も残っていない。"""
    return row is None or (row.source_state == "collecting" and not replace_rows.doc_rows_left(row))


def left_by_stop(row: ReplaceRow | None, run_id: int, rel: str) -> bool:
    """止まった前の回が残した、その文書 `rel` の 1 文書の行を受け継いでいるか（この回が作った行ではないか）。"""
    return row is not None and row.doc == rel and not replace_rows.created_by_run(row, run_id)


def drop_own(row: ReplaceRow | None) -> None:
    """回を持たない呼び出し（LLM での書き換え・孤児の掃除）が、終わったときに自分の行を消す。`failed` の項目がある `build` の行は残す。"""
    if row is None:
        return
    drop = final_check(row) if row.kind == "build" else replace_rows.make_drop(row)
    if drop is not None:
        replace_rows.drop_own(drop)


def own_row(world: str, *, kind: str = "build", run_id: int | None = None):
    """回を拾わない呼び出しが、with の間だけ自分の lock と行を持つ。"""
    return replace_rows.own_row(world, kind=kind, run_id=run_id)


@contextmanager
def held_row(world: str, run_id: int | None, *, kind: str = "build", doc: str = ""):
    """この回が拾った行を渡す。回を拾っていない呼び出し（回の id が無い・行が無い）は、with の間だけ自分の lock と行（`doc` があればその 1 文書の行）を持つ。
    `kind="delete"` を求められたら、拾った行も消す仕事（`delete`）に変えてから渡す。拾った行の lock を今は誰も持っていなければ `RowLockLost`（失った lock を取り直さない）。
    """
    row = row_for_run(world, run_id)
    if row is not None:
        if not replace_rows.lock_held(row):
            raise replace_rows.RowLockLost(f"行の lock が自分のものではありません: world={world} row={row.id}")
        yield checked(replace_rows.become_delete(row) if kind == "delete" else row)
        return
    with replace_rows.own_row(world, kind=kind, run_id=run_id, doc=doc) as own:
        yield checked(own)


@contextmanager
def row_or_scratch(world: str, row: ReplaceRow | None):
    """回の行があればそれを、無ければ with の間だけ自分の行を渡す。自分の行は正常に抜けたときだけ消す（例外で抜ければ残して「途中で止まった」を出す）。"""
    if row is not None:
        yield row
        return
    with replace_rows.own_row(world) as own:
        own = checked(own)
        yield own
        drop_own(own)


def final_check(row: ReplaceRow | None, *, replaces_all: bool = True) -> DropToken | None:
    """行を消してよいなら `DropToken`、残すなら None。全項目が「古いものを消した」になっていない行（「失敗」を含む）・`build` 以外の行は残す。
    `replaces_all=False`（規則版の再生成など原本を集め直さない回）は、確定済みの全体の行を残す（1 文書の行は、その文書を入れ直した回の成功で消える）。
    確定した原本と設定が今も同じかを、消す直前にもう一度確かめる（変わっていれば `SourceChanged`）。
    """
    if row is None:
        return None
    cur = replace_rows.read_row(row.id)
    if cur is None or cur.kind != "build":
        return None
    if any(v != replace_rows.ITEM_CLEANED for v in cur.items.values()):   # 全項目が「古いものを消した」になるまで残す
        return None
    if not replaces_all and cur.source_state == "fixed" and not cur.doc:
        return None
    if row._check is not None and cur.source_state == "fixed":
        replace_rows.check_or_note(row, cur, None)   # 消す直前に原本と設定をもう一度確かめる（変わっていれば理由を行に書いて `SourceChanged`）
    return replace_rows.make_drop(row)


def refused_check(row: ReplaceRow | None, run_id: int) -> DropToken | None:
    """何も入れ替えないうちに断った回の行を消してよいなら `DropToken`、残すなら None。
    この回が作った `build` の行で、原本がまだ確定せず（`collecting`）、入れ替えたものも古いものの控えも無いときだけ消す。受け継いだ行は消さない。
    """
    if row is None:
        return None
    cur = replace_rows.read_row(row.id)
    if cur is None or cur.kind != "build" or cur.source_state != "collecting" or cur.items or cur.old_things:
        return None
    return replace_rows.make_drop(row) if replace_rows.created_by_run(row, run_id) else None


def finish_run(world: str, run_id: int, *, status: str, replaces_all: bool = True, nothing_swapped: bool = False,
               **fields) -> dict:
    """回の記録を閉じる。成功（`auto_published*`）で行を消してよければ、行も同じトランザクションで消す。
    `nothing_swapped`＝何も入れ替えないうちに断った失敗は、その回が作った行に限り、同じトランザクションで行も消す（断った回は記録を残さない）。
    """
    drop = None
    if status.startswith("auto_published"):
        drop = final_check(row_for_run(world, run_id), replaces_all=replaces_all)
    elif status == "failed" and nothing_swapped:
        drop = refused_check(row_for_run(world, run_id), run_id)
    if drop is None:
        return store.finish_ingest_run(run_id, status=status, **fields)
    return store.finish_ingest_run_and_drop_row(run_id, drop=drop, status=status, **fields)
