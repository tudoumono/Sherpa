"""入れ替えの行（`ingest_replacements`）と取り込みの番号（`ingest_no_seq`）の窓口。
資料フォルダ（または 1 文書）ごとに、入れ替えが終わるまで残る 1 行。回を拾った worker が、動いている間だけ DB の lock（`RUN_NS`, 回の id）を持つ。
設計: docs/design/data.md「PostgreSQL（主な表と役割）」
"""
from __future__ import annotations

import hashlib
import json
from contextlib import contextmanager
from dataclasses import dataclass, field, replace
from typing import Self

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Json

from .db import _KB_ID, _connect, _connect_bounded, _dsn, _ensure, _ensure_unless_bounded

RUN_NS = 0x52554E31   # 回ごとの lock の第 1 引数。`db.py` の他の 2 引数形の lock と重ならない固定値
REWRITE_NS = 0x52575431   # LLM での書き換えの行の lock（回を持たない）
CLEANUP_NS = 0x434C4E31   # 登録の無い孤児の掃除の行の lock
OWN_NS = 0x4F574E31       # 回を持たない呼び出しが自分で持つ行の lock
ES_CLEANUP_PREFIX = "es:"   # ES の孤児の掃除の行の鍵の接頭辞（`valid_world` が許さない `:` を含むので資料フォルダ名と衝突しない）
ITEMS_ALL = ("archives", "ir", "rag", "md", "graph", "ledger", "es")
ITEMS_DOC = ("ir", "rag", "md", "graph", "ledger", "es")   # 1 文書の入れ直しが入れ替えるもの
ITEM_PENDING, ITEM_SWAPPED, ITEM_CLEANED, ITEM_FAILED = "pending", "swapped", "cleaned", "failed"

_COLS = ("id, world, doc, kind, ingest_no, lock_ns, lock_id, source_state, source_sig, settings, items, "
         "old_things, rebuild_reason")


class ReplaceAborted(BaseException):
    """入れ替えを続けてはいけない（広い `except Exception` に吸われないよう `BaseException` から派生する）。受けるのは回の最外周だけ。"""


class SourceChanged(ReplaceAborted):
    """確定した原本・設定が変わった（自動では再開しない）。"""


class RowLockLost(ReplaceAborted):
    """行の lock の鍵が自分のものでなくなった（自動で再開してよい）。"""


_MINT = object()


@dataclass(frozen=True)
class ReplaceRow:
    """DB から読んだ行。このモジュールの外では作れない（作ると `TypeError`）。"""
    id: int
    world: str
    doc: str
    kind: str
    ingest_no: int
    lock_ns: int | None
    lock_id: int | None
    source_state: str
    source_sig: str | None
    settings: dict
    items: dict
    old_things: dict
    rebuild_reason: str | None
    _mint: object = field(default=None, repr=False, compare=False)
    _offline: list | None = field(default=None, repr=False, compare=False)   # 試験の行だけが持つ。DB に書かず入れ替えた項目をここへ記録する
    _check: object = field(default=None, repr=False, compare=False)   # 入れ替えに入る前に原本と設定を確かめる処理 `(row, item)`。`attach_check` で付ける
    _checked: set = field(default_factory=set, repr=False, compare=False)   # 確かめ済みの項目（確かめは項目ごとに最初の 1 回だけ）

    def __post_init__(self):
        if self._mint is not _MINT:
            raise TypeError("ReplaceRow は replace_rows の中でしか作れません（行は DB から読んで受け取る）")


@dataclass(frozen=True)
class DropToken:
    """行を消してよいと確かめた印。`replace_guard.final_check` の戻り値としてしか手に入らない。"""
    id: int
    lock_ns: int
    lock_id: int
    _mint: object = field(default=None, repr=False, compare=False)

    def __post_init__(self):
        if self._mint is not _MINT:
            raise TypeError("DropToken は replace_rows の中でしか作れません")


def _row_from(rec: dict) -> ReplaceRow:
    return ReplaceRow(**{k: rec[k] for k in (
        "id", "world", "doc", "kind", "ingest_no", "lock_ns", "lock_id", "source_state", "source_sig",
        "rebuild_reason")}, settings=rec["settings"] or {}, items=rec["items"] or {},
        old_things=rec["old_things"] or {}, _mint=_MINT)


def shown_cond(conn, world: str, col: str = "ingest_no") -> tuple[str, tuple]:
    """目録・名前の台帳を読む文に足す条件 `(" AND col=%s", (今見せる番号,))`。読みはどれも、この番号の行だけを相手にする。
    登録の無い資料フォルダ（`worlds` に行が無い孤児）は番号で絞らない。
    トランザクションを始める前の接続なら、番号とその番号の行を同じ REPEATABLE READ のスナップショットで読む（切り替えと古い番号の削除が間に入っても行が欠けない）。
    """
    if conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE:
        conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ READ ONLY")
    rec = conn.execute("SELECT shown_ingest_no FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world)).fetchone()
    return (f" AND {col}=%s", (rec["shown_ingest_no"],)) if rec else ("", ())


def next_ingest_no(conn=None) -> int:
    """取り込みの番号を 1 つ振る（`ingest_no_seq` から。番号 0 は番号が付く前に作ったものに予約）。"""
    _ensure()
    if conn is not None:
        return conn.execute("SELECT nextval('ingest_no_seq') AS n").fetchone()["n"]
    with _connect() as c:
        return c.execute("SELECT nextval('ingest_no_seq') AS n").fetchone()["n"]


def read_row(row_id: int) -> ReplaceRow | None:
    _ensure()
    with _connect() as c:
        rec = c.execute(f"SELECT {_COLS} FROM ingest_replacements WHERE id=%s", (row_id,)).fetchone()
    return _row_from(rec) if rec else None


def read_row_for_run(world: str, run_id: int, *, lock_ns: int = RUN_NS) -> ReplaceRow | None:
    """この回（`lock_ns`, `run_id`）の lock を持つ、この資料フォルダの行（全体の行も 1 文書の行も）。無ければ None。"""
    _ensure()
    with _connect() as c:
        rec = c.execute(
            f"SELECT {_COLS} FROM ingest_replacements "
            "WHERE kb_id=%s AND world=%s AND lock_ns=%s AND lock_id=%s",
            (_KB_ID, world, lock_ns, run_id)).fetchone()
    return _row_from(rec) if rec else None


def claim_row(conn, world: str, run_id: int, op: str | None, *, lock_ns: int = RUN_NS, doc: str = "") -> None:
    """回を拾うトランザクションの中で、資料フォルダ全体の行（`doc` があればその 1 文書の行）を作る。行があれば番号と `old_things` を受け継ぎ、lock の鍵だけ更新する。
    消す操作（全体の行だけ）は受け継いだ行を `delete`・`not_used` に置き換え、登録の無い孤児の掃除（`cleanup`）の行は `build` に置き換える。
    """
    deleting = op == "delete" and not doc
    cur = conn.execute(
        "SELECT id, kind FROM ingest_replacements WHERE kb_id=%s AND world=%s AND doc=%s FOR UPDATE",
        (_KB_ID, world, doc)).fetchone()
    if cur is None:
        conn.execute(
            "INSERT INTO ingest_replacements (kb_id, world, doc, kind, ingest_no, lock_ns, lock_id, source_state) "
            "VALUES (%s,%s,%s,%s,nextval('ingest_no_seq'),%s,%s,%s)",
            (_KB_ID, world, doc, "delete" if deleting else "build", lock_ns, run_id,
             "not_used" if deleting else "collecting"))
    elif deleting:
        conn.execute(
            "UPDATE ingest_replacements SET kind='delete', source_state='not_used', source_sig=NULL, "
            "source_list=NULL, items='{}', lock_ns=%s, lock_id=%s, updated_at=now() WHERE id=%s",
            (lock_ns, run_id, cur["id"]))
    else:
        conn.execute(
            "UPDATE ingest_replacements SET kind=CASE WHEN kind='cleanup' THEN 'build' ELSE kind END, "
            "lock_ns=%s, lock_id=%s, updated_at=now() WHERE id=%s", (lock_ns, run_id, cur["id"]))


@dataclass
class ClaimedRun:
    """拾った回 1 件と、その回の lock を持つ専用の接続。接続を閉じれば lock が外れる。"""
    run: dict
    conn: psycopg.Connection

    def close(self) -> None:
        try:
            self.conn.close()
        except Exception:
            pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc) -> None:
        self.close()


def open_claim_connection() -> psycopg.Connection:
    """回の lock を持つ専用の接続（プールは使わない）。"""
    _ensure()
    return psycopg.connect(_dsn(), autocommit=True, row_factory=dict_row)


def fix_sources(row: ReplaceRow, sig: str, manifest: dict, settings: dict | None = None, *,
                targets: tuple = ITEMS_ALL, renumber: bool = False) -> None:
    """走査で確かめた原本の署名と一覧を行に確定し、この回が入れ替えるもの（`targets`）を「まだ」にする。`store.world_lock` の中で呼ぶ。
    同じ資料フォルダに残る 1 文書の行は、同じトランザクションでこの行にまとめる（`old_things` は引き継ぐ）。
    `renumber` は確定済みの行を新しい取り込みの番号に置き換える（原本か設定が変わった再開。理由は消す）。
    """
    items = {t: ITEM_PENDING for t in targets}
    with _connect() as c:
        cur = c.execute("SELECT kind FROM ingest_replacements WHERE id=%s AND lock_ns=%s AND lock_id=%s FOR UPDATE",
                        (row.id, row.lock_ns, row.lock_id)).fetchone()
        if cur is None:
            raise RowLockLost(f"行の lock が自分のものではありません: world={row.world} row={row.id}")
        if cur["kind"] != "build":
            return
        if row.doc:   # 1 文書の行は、ほかの行をまとめずにその行だけ確定する
            c.execute("UPDATE ingest_replacements SET source_state='fixed', source_sig=%s, source_list=%s, settings=%s, "
                      "items=%s, updated_at=now() WHERE id=%s", (sig, Json(manifest), Json(settings or {}), Json(items), row.id))
            return
        docs = c.execute("SELECT id, old_things FROM ingest_replacements "
                         "WHERE kb_id=%s AND world=%s AND doc<>'' FOR UPDATE", (_KB_ID, row.world)).fetchall()
        carried: dict = {}
        for d in docs:
            carried.update(d["old_things"] or {})
        if docs:
            c.execute("DELETE FROM ingest_replacements WHERE id = ANY(%s)", ([d["id"] for d in docs],))
        c.execute(
            "UPDATE ingest_replacements SET source_state='fixed', source_sig=%s, source_list=%s, settings=%s, "
            "items=%s, old_things=%s::jsonb || old_things, "
            "ingest_no=CASE WHEN %s THEN nextval('ingest_no_seq') ELSE ingest_no END, "
            "rebuild_reason=CASE WHEN %s THEN NULL ELSE rebuild_reason END, updated_at=now() WHERE id=%s",
            (sig, Json(manifest), Json(settings or {}), Json(items), Json(carried), renumber, renumber, row.id))


def keep_fixed(row: ReplaceRow, targets: tuple) -> None:
    """再開: 確定した原本・設定・番号・入れ替えの状態をそのままにして、この回の対象のうち行に無い項目だけ「まだ」で足す。`store.world_lock` の中で呼ぶ。"""
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE ingest_replacements r SET items=%s::jsonb || items, updated_at=now() "
            "WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s", (Json({t: ITEM_PENDING for t in targets}), row.id, row.lock_ns, row.lock_id)).rowcount
    if n == 0:
        raise RowLockLost(f"行の lock が自分のものではありません: world={row.world} row={row.id}")


# 回の lock（`RUN_NS`, 回の id）を、どこかの接続が今持っているか。lock を取らずに `pg_locks` を読むだけ。
_LOCK_HELD = ("EXISTS (SELECT 1 FROM pg_locks l WHERE l.locktype='advisory' AND l.granted AND l.objsubid=2 "
              "AND l.database=(SELECT oid FROM pg_database WHERE datname=current_database()) "
              "AND l.classid::bigint=r.lock_ns::bigint AND l.objid::bigint=r.lock_id::bigint)")


def unfinished_docs(world: str, *, connect_timeout: float | None = None,
                    statement_timeout_ms: int | None = None) -> list[str]:
    """入れ替えの途中で止まっている行の `doc`（全体の行は `""`）。行があり、その lock を誰も持っていなければ止まっている。登録の無い孤児の掃除（`cleanup`）の行は見ない。
    止まったと見えた行は別の文で、行の存在と lock の不在をもう一度確かめる（回の終わりの直前・lock の取り直しの直後の誤判定を塞ぐ）。lock は取らない。
    """
    _ensure_unless_bounded(connect_timeout, statement_timeout_ms)
    with _connect_bounded(connect_timeout, statement_timeout_ms) as c:
        cand = c.execute(
            "SELECT r.id FROM ingest_replacements r WHERE r.kb_id=%s AND r.world=%s AND r.kind<>'cleanup' "
            f"AND NOT {_LOCK_HELD}", (_KB_ID, world)).fetchall()
        if not cand:
            return []
        return [r["doc"] for r in c.execute(
            "SELECT r.doc FROM ingest_replacements r WHERE r.id = ANY(%s) "
            f"AND NOT {_LOCK_HELD}", ([r["id"] for r in cand],)).fetchall()]


def unfinished_state(world: str, *, connect_timeout: float | None = None,
                     statement_timeout_ms: int | None = None) -> str | None:
    """入れ替えの途中で止まっているか（全体なら `"whole"`・1 文書だけなら `"doc"`・止まっていなければ None）。見方は `unfinished_docs`。
    設計: docs/design/operations.md「止まった取り込みの見え方」
    """
    docs = unfinished_docs(world, connect_timeout=connect_timeout, statement_timeout_ms=statement_timeout_ms)
    return None if not docs else "whole" if "" in docs else "doc"


def shown_ingest_no(world: str) -> int:
    """目録が今見せている取り込みの番号（`worlds.shown_ingest_no`）。登録が無ければ 0。"""
    _ensure()
    with _connect() as c:
        rec = c.execute("SELECT shown_ingest_no FROM worlds WHERE kb_id=%s AND world_id=%s", (_KB_ID, world)).fetchone()
    return int(rec["shown_ingest_no"] or 0) if rec else 0


def doc_rows_left(row: ReplaceRow) -> bool:
    """この資料フォルダに 1 文書の行が残っているか（止まった 1 文書の入れ直しが入れ替えきっていない）。"""
    if row._offline is not None:
        return False
    _ensure()
    with _connect() as c:
        return c.execute("SELECT 1 FROM ingest_replacements WHERE kb_id=%s AND world=%s AND doc<>'' LIMIT 1",
                         (_KB_ID, row.world)).fetchone() is not None


def lock_held(row: ReplaceRow) -> bool:
    """この行の lock の鍵を、今どこかの接続が持っているか（持っていない行は、回が終わった後に残った行）。"""
    if row._offline is not None:
        return True
    _ensure()
    with _connect() as c:
        return c.execute(f"SELECT 1 FROM ingest_replacements r WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD}",
                         (row.id, row.lock_ns, row.lock_id)).fetchone() is not None


def created_at(row: ReplaceRow):
    """この行を作った時刻。手元の行・行が無ければ None。"""
    if row._offline is not None:
        return None
    _ensure()
    with _connect() as c:
        rec = c.execute("SELECT created_at FROM ingest_replacements WHERE id=%s", (row.id,)).fetchone()
    return rec["created_at"] if rec else None


def created_by_run(row: ReplaceRow, run_id: int) -> bool:
    """この行が、この回を拾うときに作られたものか（前の回から受け継いだ行ではないか）。"""
    _ensure()
    with _connect() as c:
        return c.execute(
            "SELECT 1 FROM ingest_replacements r JOIN ingest_runs g ON g.id=%s "
            "WHERE r.id=%s AND r.created_at >= g.created_at", (run_id, row.id)).fetchone() is not None


def fix_sources_from_last(row: ReplaceRow, settings: dict | None = None, *, targets: tuple = ITEMS_ALL) -> None:
    """原本を走査しない回（規則版への再生成など）が、前回取り込んだ署名と一覧（`worlds.last_sig`・`last_manifest`）を行に確定する。`store.world_lock` の中で呼ぶ。
    原本がすでに確定している行（前の回から受け継いだ止まった行）は上書きしない。
    """
    if row._offline is not None:
        return
    cur = read_row(row.id)
    if cur is None or cur.source_state != "collecting":
        return
    with _connect() as c:
        w = c.execute("SELECT last_sig, last_manifest FROM worlds WHERE kb_id=%s AND world_id=%s",
                      (_KB_ID, row.world)).fetchone() or {}
    fix_sources(row, w.get("last_sig") or "", w.get("last_manifest") or {}, settings, targets=targets)


@contextmanager
def _held(lock_ns: int, lock_id: int | None = None):
    """専用の接続で lock を取って持つ（`lock_id` が無ければ行の番号の列から振る）。抜けると接続を閉じて lock が外れる（行は消さない）。"""
    conn = open_claim_connection()
    try:
        if lock_id is None:
            lock_id = conn.execute(
                "SELECT nextval(pg_get_serial_sequence('ingest_replacements', 'id')) AS n").fetchone()["n"]
        conn.execute("SELECT pg_advisory_lock(%s, %s)", (lock_ns, lock_id))
        yield conn, lock_id
    finally:
        conn.close()


@contextmanager
def own_row(world: str, *, kind: str = "build", run_id: int | None = None, doc: str = ""):
    """回を拾わない呼び出しが、with の間だけ自分の lock と資料フォルダ全体の行（`doc` があればその 1 文書の行）を持つ。`run_id` があればその回の lock（`RUN_NS`）にして、回を拾った worker と同じ読み方（`read_row_for_run`）で見える。
    行があれば番号を受け継ぐ（`claim_row` と同じ）。抜けても行は消さない（消すのは終わりの記録か `drop_own`）。
    """
    ns = RUN_NS if run_id is not None else OWN_NS
    with _held(ns, run_id) as (conn, lock_id):
        with conn.transaction():
            claim_row(conn, world, lock_id, "delete" if kind == "delete" else None, lock_ns=ns, doc=doc)
        yield read_row_for_run(world, lock_id, lock_ns=ns)


@contextmanager
def begin_rewrite(world: str, settings):
    """LLM での書き換えの回が、with の間だけ自分の lock と行を持つ（`ingest_runs` には出さない）。同じ資料フォルダに行が 1 つでも残っていれば、受け継がずに None を渡す（書き換えを見送る）。
    新しい番号の行に、前回取り込んだ署名と一覧・書き換えの設定を同じトランザクションで確定する。項目は `rag`・`graph`・`es`。`settings` は設定の値の dict か、行を作るときだけ呼ぶ引数なしの関数。
    """
    with _held(REWRITE_NS) as (conn, lock_id):
        with conn.transaction():
            rec = None
            if conn.execute("SELECT 1 FROM ingest_replacements WHERE kb_id=%s AND world=%s LIMIT 1",
                            (_KB_ID, world)).fetchone() is None:
                w = conn.execute("SELECT last_sig, last_manifest FROM worlds WHERE kb_id=%s AND world_id=%s",
                                 (_KB_ID, world)).fetchone() or {}
                rec = conn.execute(
                    "INSERT INTO ingest_replacements (kb_id, world, doc, kind, ingest_no, lock_ns, lock_id, "
                    "source_state, source_sig, source_list, settings, items) "
                    "VALUES (%s,%s,'','build',nextval('ingest_no_seq'),%s,%s,'fixed',%s,%s,%s,%s) "
                    "ON CONFLICT (kb_id, world, doc) DO NOTHING RETURNING id",
                    (_KB_ID, world, REWRITE_NS, lock_id, w.get("last_sig") or "", Json(w.get("last_manifest") or {}),
                     Json(settings() if callable(settings) else settings), Json({t: ITEM_PENDING for t in ("rag", "graph", "es")}))).fetchone()
        yield read_row(rec["id"]) if rec else None


@contextmanager
def begin_cleanup(world: str):
    """登録の無い孤児の掃除が、with の間だけ自分の lock と行（`kind='cleanup'`）を持つ。他が掃除中ならその資料フォルダは飛ばして None を渡す。
    掃除の途中で止まって lock が外れた掃除の行は引き継ぐ。
    """
    with _held(CLEANUP_NS) as (conn, lock_id):
        with conn.transaction():
            rec = conn.execute(
                "INSERT INTO ingest_replacements (kb_id, world, doc, kind, ingest_no, lock_ns, lock_id) "
                "VALUES (%s,%s,'','cleanup',nextval('ingest_no_seq'),%s,%s) "
                "ON CONFLICT (kb_id, world, doc) DO NOTHING RETURNING id",
                (_KB_ID, world, CLEANUP_NS, lock_id)).fetchone()
            if rec is None:
                rec = conn.execute(
                    "UPDATE ingest_replacements r SET lock_ns=%s, lock_id=%s, updated_at=now() "
                    "WHERE r.kb_id=%s AND r.world=%s AND r.doc='' AND r.kind='cleanup' "
                    f"AND NOT {_LOCK_HELD} RETURNING r.id", (CLEANUP_NS, lock_id, _KB_ID, world)).fetchone()
        yield read_row(rec["id"]) if rec else None


def become_delete(row: ReplaceRow) -> ReplaceRow:
    """行を消す仕事（`kind='delete'`・原本は使わない）に変える。古いものの控え（`old_things`）は残す。行の lock を今も持っていなければ `RowLockLost`。"""
    if row.kind == "delete":
        return row
    if row._offline is not None:
        return replace(row, kind="delete", source_state="not_used")
    _ensure()
    with _connect() as c:
        rec = c.execute(
            "UPDATE ingest_replacements r SET kind='delete', source_state='not_used', source_sig=NULL, source_list=NULL, "
            f"items='{{}}', updated_at=now() WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD} RETURNING r.id",
            (row.id, row.lock_ns, row.lock_id)).fetchone()
    if rec is None:
        raise RowLockLost(f"行の lock が自分のものではありません: world={row.world} row={row.id}")
    return read_row(rec["id"])


def stale_cleanup_worlds() -> list[str]:
    """登録の無い孤児の掃除（`kind='cleanup'`）の行のうち、lock を持つ接続がいなくなったものの world（次の掃除が続きから消す）。"""
    _ensure()
    with _connect() as c:
        rows = c.execute(
            "SELECT r.world FROM ingest_replacements r WHERE r.kb_id=%s AND r.doc='' AND r.kind='cleanup' "
            f"AND NOT {_LOCK_HELD} ORDER BY r.world", (_KB_ID,)).fetchall()
    return [r["world"] for r in rows]


def drop_own(drop: DropToken) -> None:
    """自分の lock の行を消す（回の記録と一緒に消す `drop_in` と違い、回を持たない呼び出し用）。他に持ち替えられていれば何もしない。"""
    _ensure()
    with _connect() as c:
        c.execute("DELETE FROM ingest_replacements WHERE id=%s AND lock_ns=%s AND lock_id=%s",
                  (drop.id, drop.lock_ns, drop.lock_id))


def make_drop(row: ReplaceRow) -> DropToken:
    return DropToken(row.id, row.lock_ns, row.lock_id, _MINT)


def drop_in(conn, drop: DropToken) -> None:
    """回の記録を閉じる同じトランザクションで、自分の lock の行を消す。自分のものでなければ例外にして、記録ごと取り消す。"""
    n = conn.execute("DELETE FROM ingest_replacements WHERE id=%s AND lock_ns=%s AND lock_id=%s",
                     (drop.id, drop.lock_ns, drop.lock_id)).rowcount
    if n == 0:
        raise RowLockLost(f"行の lock が自分のものではありません: row={drop.id}")


def drop_world_rows(conn, world: str) -> None:
    """資料フォルダの行を、1 文書の行も含めて全部消す（資料フォルダの削除と同じトランザクション）。"""
    conn.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world=%s", (_KB_ID, world))


def attach_check(row: ReplaceRow, check) -> ReplaceRow:
    """入れ替えに入る前の確かめ `check(row, item)` を付けた行を返す（`SourceChanged` を投げうる）。確かめは `before_swap`・`before_swap_in` が項目ごとに最初の 1 回だけ呼ぶ。"""
    return replace(row, _check=check)


def gate(row: ReplaceRow, item: str) -> None:
    """項目 `item` を最初に入れ替える前に、付いている確かめを 1 回呼ぶ。原本か設定が変わっていたら行に理由を書いて `SourceChanged` を投げる。"""
    if row._check is None or row._offline is not None or item in row._checked:
        return
    check_or_note(row, row, item)
    row._checked.add(item)


def check_or_note(row: ReplaceRow, cur: ReplaceRow, item: str | None) -> None:
    """付いている確かめを呼ぶ。原本か設定が変わっていたら、行に理由（`rebuild_reason`）を書いてから `SourceChanged` を投げる。"""
    try:
        row._check(cur, item)
    except SourceChanged as e:
        _ensure()
        with _connect() as c:
            c.execute("UPDATE ingest_replacements SET rebuild_reason=%s, updated_at=now() WHERE id=%s", (str(e), row.id))
        raise


def read_source_list(row: ReplaceRow) -> dict | None:
    """行に確定した原本の一覧（`rel -> [mtime_ns, ctime_ns, size]`）。確定前・行が無ければ None。"""
    if row._offline is not None:
        return None
    _ensure()
    with _connect() as c:
        rec = c.execute("SELECT source_list FROM ingest_replacements WHERE id=%s", (row.id,)).fetchone()
    return rec["source_list"] if rec else None


def before_swap_in(conn, row: ReplaceRow, world: str, item: str) -> None:
    """PostgreSQL の中のもの（目録・名前の台帳）を入れ替える接続 `conn` のトランザクションの先頭で呼ぶ。
    ①別の資料フォルダの行なら `ValueError` ②行の lock の鍵が自分のものでない、またはその lock を今持っている接続が無ければ `RowLockLost` ③`item` を「古いものを消した」にする。
    ③は入れ替えと同じトランザクションなので、入れ替えが取り消されれば一緒に取り消される。
    """
    if row.world != world:
        raise ValueError(f"別の資料フォルダの行です: row={row.world} 対象={world}")
    if row._offline is not None:
        row._offline.append(item)
        return
    gate(row, item)
    n = conn.execute(
        "UPDATE ingest_replacements r SET items = items || jsonb_build_object(%s::text, %s::text), updated_at=now() "
        f"WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD}", (item, ITEM_CLEANED, row.id, row.lock_ns, row.lock_id)).rowcount
    if n == 0:
        raise RowLockLost(f"行の lock が自分のものではありません: world={world} row={row.id}")


def check_held_in(conn, row: ReplaceRow, world: str) -> None:
    """書き込みの接続 `conn` のトランザクションの先頭で呼び、行を持っていることだけ確かめる（行は書かない）。
    ①別の資料フォルダの行なら `ValueError` ②行の lock の鍵が自分のものでない、またはその lock を今持っている接続が無ければ `RowLockLost`。
    """
    if row.world != world:
        raise ValueError(f"別の資料フォルダの行です: row={row.world} 対象={world}")
    if row._offline is not None:
        return
    held = conn.execute(f"SELECT 1 FROM ingest_replacements r WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD}",
                        (row.id, row.lock_ns, row.lock_id)).fetchone()
    if held is None:
        raise RowLockLost(f"行の lock が自分のものではありません: world={world} row={row.id}")


def before_swap(row: ReplaceRow, world: str, item: str, *, old=()) -> None:
    """PostgreSQL の外のもの（グラフ・ES）を入れ替える直前に、古いものの名前と「入れ替えた」を先に行へ書く。
    ①別の資料フォルダの行なら `ValueError` ②行の lock の鍵が自分のものでない、またはその lock を今持っている接続が無ければ `RowLockLost` ③`item` を「入れ替えた」にし、古いものの名前を `old_things` に控える。
    入れ替えの前に書くので、入れ替えの途中で止まっても古いものの名前が残る。古いものを消し終えたら `after_old_removed` を呼ぶ。
    """
    if row.world != world:
        raise ValueError(f"別の資料フォルダの行です: row={row.world} 対象={world}")
    if row._offline is not None:
        row._offline.append(item)
        return
    gate(row, item)
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE ingest_replacements r SET items = items || jsonb_build_object(%s::text, %s::text), "
            "old_things = old_things || jsonb_build_object(%s::text, %s::jsonb), updated_at=now() "
            f"WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD}",
            (item, ITEM_SWAPPED, item, Json(list(old)), row.id, row.lock_ns, row.lock_id)).rowcount
    if n == 0:
        raise RowLockLost(f"行の lock が自分のものではありません: world={world} row={row.id}")


def after_old_removed(row: ReplaceRow, world: str, item: str) -> None:
    """`before_swap` で控えた古いものを消し終えたので、`item` を「古いものを消した」にして控えを外す。"""
    if row.world != world:
        raise ValueError(f"別の資料フォルダの行です: row={row.world} 対象={world}")
    if row._offline is not None:
        return
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE ingest_replacements r SET items = items || jsonb_build_object(%s::text, %s::text), "
            f"old_things = old_things - %s::text, updated_at=now() WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD}",
            (item, ITEM_CLEANED, item, row.id, row.lock_ns, row.lock_id)).rowcount
    if n == 0:
        raise RowLockLost(f"行の lock が自分のものではありません: world={world} row={row.id}")


def settle_item(row: ReplaceRow, world: str, item: str) -> None:
    """この回は入れ替えるものが無いと決めた `item` を、「古いものを消した」にする（入れ替えの前の確かめもしない）。
    ①別の資料フォルダの行なら `ValueError` ②行の lock の鍵が自分のものでない、またはその lock を今持っている接続が無ければ `RowLockLost`。
    """
    _write_held(row, world, "items = items || jsonb_build_object(%s::text, %s::text)", (item, ITEM_CLEANED))


def settle_pending(row: ReplaceRow, world: str) -> None:
    """その場で書き換える回（規則版・LLM での書き換え）が成功で終わるとき、入れ替える必要が無かった項目（まだ「まだ」のもの）を「古いものを消した」にする。"""
    _write_held(row, world, "items = COALESCE((SELECT jsonb_object_agg(k, CASE WHEN v = 'pending' THEN 'cleaned' ELSE v END) "
                            "FROM jsonb_each_text(r.items) AS t(k, v)), '{}'::jsonb)", ())


def mark_failed(row: ReplaceRow, world: str, item: str, reason: str) -> None:
    """`item` を「失敗」にして理由を行に残す。「失敗」の項目がある行は回の終わりに消えず、「途中で止まった」と出る。
    ①別の資料フォルダの行なら `ValueError` ②行の lock の鍵が自分のものでない、またはその lock を今持っている接続が無ければ `RowLockLost`。
    """
    if row.world != world:
        raise ValueError(f"別の資料フォルダの行です: row={row.world} 対象={world}")
    if row._offline is not None:
        row._offline.append(f"failed:{item}")
        return
    _ensure()
    with _connect() as c:
        n = c.execute(
            "UPDATE ingest_replacements r SET items = items || jsonb_build_object(%s::text, %s::text), "
            f"rebuild_reason=%s, updated_at=now() WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD}",
            (item, ITEM_FAILED, reason, row.id, row.lock_ns, row.lock_id)).rowcount
    if n == 0:
        raise RowLockLost(f"行の lock が自分のものではありません: world={world} row={row.id}")


def note_old(row: ReplaceRow, world: str, key: str, old) -> None:
    """入れ替えの前に、消すべき古いものの名前を行の `old_things[key]` へ控える（`items` は変えない）。
    ①別の資料フォルダの行なら `ValueError` ②行の lock の鍵が自分のものでない、またはその lock を今持っている接続が無ければ `RowLockLost`。
    """
    _write_held(row, world, "old_things = old_things || jsonb_build_object(%s::text, %s::jsonb)", (key, Json(list(old))))


FAILED_DOCS_KEY = "_failed_docs"   # 行の `settings` に、この番号の派生で作れなかった文書 `{rel: 理由}` を控えるキー（再開が派生を飛ばしても失敗の一覧を戻せる）


def note_failed_docs(row: ReplaceRow, world: str, reasons: dict) -> None:
    """この番号の派生で作れなかった文書を行に控える。"""
    _write_held(row, world, "settings = settings || jsonb_build_object(%s::text, %s::jsonb)", (FAILED_DOCS_KEY, Json(reasons)))


def forget_failed_docs(row: ReplaceRow, world: str) -> None:
    """`note_failed_docs` で控えた失敗の一覧を外す（派生を作り直す前に、古い控えを残さない）。"""
    _write_held(row, world, "settings = settings - %s::text", (FAILED_DOCS_KEY,))


def forget_old(row: ReplaceRow, world: str, key: str) -> None:
    """`note_old` で控えた古いものを消し終えたので、控えを外す。"""
    _write_held(row, world, "old_things = old_things - %s::text", (key,))


def _write_held(row: ReplaceRow, world: str, set_sql: str, params: tuple) -> None:
    if row.world != world:
        raise ValueError(f"別の資料フォルダの行です: row={row.world} 対象={world}")
    if row._offline is not None:
        return
    _ensure()
    with _connect() as c:
        n = c.execute(f"UPDATE ingest_replacements r SET {set_sql}, updated_at=now() "
                      f"WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD}",
                      (*params, row.id, row.lock_ns, row.lock_id)).rowcount
    if n == 0:
        raise RowLockLost(f"行の lock が自分のものではありません: world={world} row={row.id}")


def renumber(row: ReplaceRow) -> ReplaceRow:
    """付け替えを旧 root へ戻すとき、1 トランザクションで番号を新しい値にし、確定した原本の署名と一覧・入れ替えの状態を消して原本を集め直す状態（`collecting`）へ戻す。古いものの控えは残す。
    行の lock を今も持っていなければ `RowLockLost`。新しい行を返す。
    """
    if row._offline is not None:
        return replace(row, ingest_no=row.ingest_no + 1, source_state="collecting", source_sig=None, items={})
    _ensure()
    with _connect() as c:
        rec = c.execute(
            "UPDATE ingest_replacements r SET ingest_no=nextval('ingest_no_seq'), source_state='collecting', "
            "source_sig=NULL, source_list=NULL, settings='{}', items='{}', rebuild_reason=NULL, updated_at=now() "
            f"WHERE r.id=%s AND r.lock_ns=%s AND r.lock_id=%s AND {_LOCK_HELD} RETURNING r.id",
            (row.id, row.lock_ns, row.lock_id)).fetchone()
    if rec is None:
        raise RowLockLost(f"行の lock が自分のものではありません: world={row.world} row={row.id}")
    return read_row(rec["id"])


def settings_sig(row: ReplaceRow, item: str) -> str:
    """行の `settings[item]` を決まった順に並べたハッシュ（作ったものに番号と一緒に書く「設定の値」）。"""
    return hashlib.sha256(json.dumps(row.settings.get(item) or {}, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def row_for_test(**fields) -> ReplaceRow:
    """試験の土台（`tests/_replace_row.py`）専用。本番のコードは使わない。"""
    base = {"id": 0, "world": "w", "doc": "", "kind": "build", "ingest_no": 1, "lock_ns": RUN_NS, "lock_id": 0,
            "source_state": "collecting", "source_sig": None, "settings": {}, "items": {}, "old_things": {},
            "rebuild_reason": None}
    return ReplaceRow(**{**base, **fields}, _mint=_MINT)


def offline_row_for_test(**fields) -> ReplaceRow:
    """DB に行を作らない試験用の行（`tests/_replace_row.py` 専用）。`before_swap_in` は DB を見ず、入れ替えた項目を `_offline` に記録するだけ。"""
    return row_for_test(**fields, _offline=[])
