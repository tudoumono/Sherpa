"""入れ替えの行（`ingest_replacements`）を試験で用意する土台。本番のコードからは使わない。

- `offline_row(world, doc="")`: DB に書かず手元に作る。単体試験用（外部サービス不要）。
- `row_for(world, doc="")`: 実物の DB に行を作って読み直し、その鍵の lock を持つ。`tests/integration`・`tests/api` 用。
"""
from __future__ import annotations

import sys
import types

from sherpa.store import db, replace_rows
from sherpa.store.replace_rows import ReplaceRow


def offline_row(world: str, doc: str = "", **fields) -> ReplaceRow:
    return replace_rows.offline_row_for_test(world=world, doc=doc, **fields)


# `tests._replace_row` と `_replace_row` の 2 つの名前で読み込まれても、lock の接続を 1 本にする（別の接続どうしは同じ鍵で待ち合う）
_LOCK_CONN = sys.modules.setdefault("_sherpa_test_row_lock_conn", types.SimpleNamespace(conn=None))


def hold_lock(lock_id: int) -> None:
    """行の鍵（`RUN_NS`, `lock_id`）の advisory lock を、このプロセスの専用の接続で持つ（行を書く入口は lock を持つ接続があるかも確かめる）。"""
    if _LOCK_CONN.conn is None:
        _LOCK_CONN.conn = replace_rows.open_claim_connection()
    _LOCK_CONN.conn.execute("SELECT pg_advisory_lock(%s, %s)", (replace_rows.RUN_NS, lock_id))


def row_for(world: str, doc: str = "", *, kind: str = "build", lock_id: int = 0, held: bool = True) -> ReplaceRow:
    """行を 1 つ作り、`held` ならその鍵の lock をこのプロセスが持つ（`held=False` は、止まって lock が外れた行（鍵を指定しなければ -1）。同じ資料フォルダ・文書の行があれば置き換える）。"""
    if held:
        hold_lock(lock_id)
    elif lock_id == 0:
        lock_id = -1   # 誰も持たない鍵（他の試験が持つ鍵と重ならない）
    db._ensure()
    with db._connect() as c:
        c.execute("DELETE FROM ingest_replacements WHERE kb_id=%s AND world=%s AND doc=%s", (db._KB_ID, world, doc))
        rec = c.execute(
            "INSERT INTO ingest_replacements (kb_id, world, doc, kind, ingest_no, lock_ns, lock_id) "
            "VALUES (%s,%s,%s,%s,nextval('ingest_no_seq'),%s,%s) RETURNING id",
            (db._KB_ID, world, doc, kind, replace_rows.RUN_NS, lock_id)).fetchone()
    return replace_rows.read_row(rec["id"])


def use_offline_rows(monkeypatch) -> None:
    """DB に繋がらない単体試験で `worker.run` など行を自分で持つ経路を動かすために、行の入口を手元の行（`offline_row`）で返す。
    `replace_rows` の中の DB を読み書きする入口（`own_row`・`begin_rewrite`・`created_by_run`・`read_row_for_run`・`read_row`・`fix_sources`・`fix_sources_from_last`・`drop_own`）を差し替える。
    """
    from contextlib import contextmanager

    last = {"world": "w"}

    def _row(world=None, **fields):
        last["world"] = world or last["world"]
        return offline_row(last["world"], **fields)

    held = {}

    @contextmanager
    def own_row(world, *, kind="build", run_id=None, doc=""):
        row = held[(world, run_id)] = _row(world, kind=kind, lock_id=run_id or 0, doc=doc)
        try:
            yield row
        finally:
            held.pop((world, run_id), None)

    @contextmanager
    def begin_rewrite(world, settings):
        yield _row(world, source_state="fixed", settings=settings)

    monkeypatch.setattr(replace_rows, "own_row", own_row)
    monkeypatch.setattr(replace_rows, "begin_rewrite", begin_rewrite)
    monkeypatch.setattr(replace_rows, "created_by_run", lambda row, run_id: False)
    monkeypatch.setattr(replace_rows, "read_row_for_run", lambda world, run_id, **kw: held.get((world, run_id)))
    monkeypatch.setattr(replace_rows, "read_row", lambda row_id: _row())
    monkeypatch.setattr(replace_rows, "fix_sources", lambda *a, **kw: None)
    monkeypatch.setattr(replace_rows, "fix_sources_from_last", lambda *a, **kw: None)
    monkeypatch.setattr(replace_rows, "drop_own", lambda drop: None)
