"""目録の入れ替えは、下書きを COPY で入れ、短いトランザクションで見せる番号だけを切り替える（実 Postgres）。
往復が行数に比例しないこと、切り替えの前は古い番号の目録が見え続けること、同じ名前が番号違いで並べられることを固定する。
"""
from __future__ import annotations

import uuid

import psycopg
import pytest

from sherpa import store
from sherpa.store import db, ledger_stage, name_ledger
from tests._replace_row import offline_row

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path):
    wid = "stage_" + uuid.uuid4().hex[:8]
    store.upsert_world(wid, str(tmp_path / "w"))
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    store.delete_world_row(wid)


def _rows(n, tag="a"):
    return [{"name": f"{tag}/f{i:04d}.md", "doctype": "markdown", "branch": "office", "status": "ok"} for i in range(n)]


def _names(n):
    return [{"name": f"N{i:04d}", "kind": "", "source": "filename", "rel_path": f"a/f{i:04d}.md"} for i in range(n)]


def test_statements_do_not_grow_with_row_count(world, monkeypatch):
    counts = []
    orig_exec, orig_many = psycopg.Cursor.execute, psycopg.Cursor.executemany

    def run(n):
        calls = []
        monkeypatch.setattr(psycopg.Cursor, "execute", lambda self, *a, **k: calls.append(1) or orig_exec(self, *a, **k))
        monkeypatch.setattr(psycopg.Cursor, "executemany", lambda self, *a, **k: calls.append(len(a[1])) or orig_many(self, *a, **k))
        store.replace_documents(world, _rows(n), names=_names(n), row=offline_row(world, ingest_no=n))
        monkeypatch.setattr(psycopg.Cursor, "execute", orig_exec)
        monkeypatch.setattr(psycopg.Cursor, "executemany", orig_many)
        counts.append(len(calls))

    run(20)
    run(400)
    assert counts[1] == counts[0] and counts[1] < 25


def test_old_ledger_stays_visible_until_the_switch(world):
    store.replace_documents(world, _rows(3, "old"), names=_names(3), row=offline_row(world, ingest_no=1))
    ledger_stage.stage_ledger(world, _rows(5, "new"), _names(5), row=offline_row(world, ingest_no=2))
    assert store.list_document_names(world) == [f"old/f{i:04d}.md" for i in range(3)]
    assert len(name_ledger.read_rows(world)) == 3
    ledger_stage.switch_ledger(world, row=offline_row(world, ingest_no=2))
    assert store.count_documents(world) == 5 and len(name_ledger.read_rows(world)) == 5


def test_same_name_can_be_staged_under_a_new_number(world):
    store.replace_documents(world, _rows(3), names=_names(3), row=offline_row(world, ingest_no=1))
    ledger_stage.stage_ledger(world, _rows(3), _names(3), row=offline_row(world, ingest_no=2))
    ledger_stage.stage_ledger(world, _rows(4), _names(4), row=offline_row(world, ingest_no=2))   # 止まった下書きは捨てて入れ直す
    with db._connect() as c:
        assert c.execute("SELECT count(*) AS n FROM documents WHERE version=%s", (world,)).fetchone()["n"] == 3 + 4
    assert store.count_documents(world) == 3


def test_switch_without_a_draft_is_refused_and_keeps_the_old_ledger(world):
    store.replace_documents(world, _rows(3), names=_names(3), row=offline_row(world, ingest_no=1))
    with pytest.raises(RuntimeError):
        ledger_stage.switch_ledger(world, row=offline_row(world, ingest_no=9))
    assert store.count_documents(world) == 3


def test_failed_draft_leaves_the_shown_ledger_untouched(world):
    store.replace_documents(world, _rows(3), names=_names(3), row=offline_row(world, ingest_no=1))
    with pytest.raises(Exception):
        ledger_stage.stage_ledger(world, _rows(5), [{"name": "X"}], row=offline_row(world, ingest_no=2))
    assert store.count_documents(world) == 3 and len(name_ledger.read_rows(world)) == 3
    with db._connect() as c:
        assert c.execute("SELECT count(*) AS n FROM documents WHERE version=%s AND ingest_no=2", (world,)).fetchone()["n"] == 0


def _numbers(world):
    with db._connect() as c:
        d = c.execute("SELECT DISTINCT ingest_no FROM documents WHERE version=%s ORDER BY 1", (world,)).fetchall()
        n = c.execute("SELECT DISTINCT ingest_no FROM name_ledger WHERE world=%s ORDER BY 1", (world,)).fetchall()
        b = c.execute("SELECT ingest_no FROM name_ledger_built WHERE world=%s ORDER BY 1", (world,)).fetchall()
    return [r["ingest_no"] for r in d], [r["ingest_no"] for r in n], [r["ingest_no"] for r in b]


def test_purge_removes_other_numbers_in_batches_and_keeps_shown_and_draft(world, monkeypatch):
    monkeypatch.setattr(ledger_stage, "PURGE_BATCH", 7)
    for no in (1, 2, 3):
        ledger_stage.stage_ledger(world, _rows(20), _names(20), row=offline_row(world, ingest_no=no))
    ledger_stage.switch_ledger(world, row=offline_row(world, ingest_no=2))
    ledger_stage.stage_ledger(world, _rows(20), _names(20), row=offline_row(world, ingest_no=4))
    assert store.purge_stale_ledger(world, row=offline_row(world, ingest_no=4), keep_draft=True) == 40
    assert _numbers(world) == ([2, 4], [2, 4], [2, 4])
    assert store.count_documents(world) == 20
    assert store.purge_stale_ledger(world, row=offline_row(world, ingest_no=4), keep_draft=False) == 20
    assert _numbers(world) == ([2], [2], [2])
    assert store.purge_stale_ledger(world, row=offline_row(world, ingest_no=4), keep_draft=False) == 0


def test_purge_stopped_halfway_is_continued_by_the_next_call(world, monkeypatch):
    monkeypatch.setattr(ledger_stage, "PURGE_BATCH", 5)
    store.replace_documents(world, _rows(12), names=_names(12), row=offline_row(world, ingest_no=1))
    store.replace_documents(world, _rows(12), names=_names(12), row=offline_row(world, ingest_no=2))
    orig, calls = db._connect, []

    class _Stop(Exception):
        pass

    def flaky(*a, **k):
        calls.append(1)
        if len(calls) == 3:
            raise _Stop()
        return orig(*a, **k)

    monkeypatch.setattr(ledger_stage, "_connect", flaky)
    with pytest.raises(_Stop):
        store.purge_stale_ledger(world, row=offline_row(world, ingest_no=2), keep_draft=False)
    monkeypatch.setattr(ledger_stage, "_connect", orig)
    assert store.count_documents(world) == 12
    store.purge_stale_ledger(world, row=offline_row(world, ingest_no=2), keep_draft=False)
    assert _numbers(world) == ([2], [2], [2])


def test_reads_do_not_lose_rows_when_the_switch_and_purge_commit_between_the_number_and_the_rows(world, monkeypatch):
    """見せる番号を取った後、行を取る前に、切り替えと古い番号の削除が確定しても、読み取りは空にも欠けにもならない。"""
    from sherpa.store import replace_rows
    store.replace_documents(world, _rows(3, "old"), names=_names(3), row=offline_row(world, ingest_no=1))
    ledger_stage.stage_ledger(world, _rows(5, "new"), _names(5), row=offline_row(world, ingest_no=2))
    real = replace_rows.shown_cond
    fired = []

    def racing(conn, w, col="ingest_no"):
        out = real(conn, w, col)
        if not fired:
            fired.append(1)
            ledger_stage.switch_ledger(w, row=offline_row(w, ingest_no=2))
            store.purge_stale_ledger(w, row=offline_row(w, ingest_no=2), keep_draft=False)
        return out

    monkeypatch.setattr(replace_rows, "shown_cond", racing)
    assert store.list_document_names(world) == [f"old/f{i:04d}.md" for i in range(3)]
    fired.clear()
    assert len(name_ledger.read_rows(world)) == 5   # 前の読み取りで切り替え済み＝新しい番号
