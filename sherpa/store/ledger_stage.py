"""目録（`documents`）と名前の台帳（`name_ledger`）の番号つきの下書きと、見せる番号の切り替え。
下書き（`stage_ledger`）は新しい番号の行を COPY で入れ、見せている行には触らない。切り替え（`switch_ledger`）は `worlds.shown_ingest_no` を変えるだけの短いトランザクション。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

from . import replace_rows
from .db import _KB_ID, _connect, _ensure

_DOC_COLS = ("kb_id, version, name, layer, scope_path, doctype, branch, original_path, md_path, status, "
             "importance, importance_reason, importance_source, ingest_no")
_NAME_COLS = "world, name, kind, source, rel_path, ingest_no"


def _doc_tuple(world: str, r: dict, no: int) -> tuple:
    return (_KB_ID, world, r.get("name"), r.get("layer") or "version", r.get("scope_path"), r.get("doctype"),
            r.get("branch"), r.get("original_path"), r.get("md_path"), r.get("status"),
            r.get("importance"), r.get("importance_reason"), r.get("importance_source"), no)


def stage_ledger(world: str, rows: list, names: list | None, *, row) -> int:
    """`row.ingest_no` の番号で、目録の行と名前の台帳の行（`names` が None なら目録だけ）を下書きに入れる（1 トランザクション・COPY）。
    行を持っていなければ（別の資料フォルダの行・鍵を失った行）何も書かずに例外にする。同じ番号の行が残っていれば（途中で止まった下書き・切り替え済みの再実行）同じトランザクションで先に捨てる。
    """
    _ensure()
    no = row.ingest_no
    with _connect() as c:
        replace_rows.check_held_in(c, row, world)
        c.execute("DELETE FROM documents WHERE kb_id=%s AND version=%s AND ingest_no=%s", (_KB_ID, world, no))
        c.execute("DELETE FROM name_ledger WHERE world=%s AND ingest_no=%s", (world, no))
        c.execute("DELETE FROM name_ledger_built WHERE world=%s AND ingest_no=%s", (world, no))
        with c.cursor() as cur:
            with cur.copy(f"COPY documents ({_DOC_COLS}) FROM STDIN") as cp:
                for r in rows:
                    cp.write_row(_doc_tuple(world, r, no))
            if names is not None:
                with cur.copy(f"COPY name_ledger ({_NAME_COLS}) FROM STDIN") as cp:
                    for r in names:
                        cp.write_row((world, r["name"], r.get("kind") or "", r["source"], r["rel_path"], no))
        if names is not None:
            c.execute("INSERT INTO name_ledger_built (world, ingest_no) VALUES (%s,%s)", (world, no))
    return len(rows)


def switch_ledger(world: str, *, row) -> None:
    """下書きの番号 `row.ingest_no` を、資料フォルダの今見せる番号にする（短い 1 トランザクション）。目録と名前の台帳は同じ番号で切り替わる。
    ① 行を確かめて項目「ledger」を更新（`before_swap_in`） ② 下書きがあることを確かめる ③ `worlds.shown_ingest_no` を変える。
    """
    _ensure()
    no = row.ingest_no
    with _connect() as c:
        replace_rows.before_swap_in(c, row, world, "ledger")
        staged = c.execute(
            "SELECT EXISTS (SELECT 1 FROM documents WHERE kb_id=%s AND version=%s AND ingest_no=%s) "
            "OR EXISTS (SELECT 1 FROM name_ledger_built WHERE world=%s AND ingest_no=%s) AS ok",
            (_KB_ID, world, no, world, no)).fetchone()["ok"]
        if not staged:
            raise RuntimeError(f"下書きが無いので切り替えられません: world={world} ingest_no={no}")
        c.execute("UPDATE worlds SET shown_ingest_no=%s WHERE kb_id=%s AND world_id=%s", (no, _KB_ID, world))


PURGE_BATCH = 5000


def purge_stale_ledger(world: str, *, row, keep_draft: bool) -> int:
    """今見せる番号（と、`keep_draft` なら `row.ingest_no` の下書き）以外の番号の目録・名前の台帳の行を、`PURGE_BATCH` 件ずつ別のトランザクションで消す。
    切り替えのトランザクションとは別に呼ぶ。途中で止まっても残りは次の呼び出しが続きから消す。消した目録の行数を返す。
    """
    _ensure()
    with _connect() as c:
        rec = c.execute("SELECT shown_ingest_no FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world)).fetchone()
    if rec is None:
        return 0
    keep = [rec["shown_ingest_no"], *([row.ingest_no] if keep_draft else [])]
    gone = 0
    while True:
        with _connect() as c:
            n = c.execute(
                "DELETE FROM documents WHERE id IN (SELECT id FROM documents WHERE kb_id=%s AND version=%s "
                "AND ingest_no <> ALL(%s) LIMIT %s)", (_KB_ID, world, keep, PURGE_BATCH)).rowcount
        gone += n
        if n < PURGE_BATCH:
            break
    while True:
        with _connect() as c:
            n = c.execute(
                "DELETE FROM name_ledger WHERE ctid IN (SELECT ctid FROM name_ledger WHERE world=%s "
                "AND ingest_no <> ALL(%s) LIMIT %s)", (world, keep, PURGE_BATCH)).rowcount
        if n < PURGE_BATCH:
            break
    with _connect() as c:
        c.execute("DELETE FROM name_ledger_built WHERE world=%s AND ingest_no <> ALL(%s)", (world, keep))
    return gone
