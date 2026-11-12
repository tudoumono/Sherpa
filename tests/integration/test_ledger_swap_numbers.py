"""目録（`documents`）と名前の台帳（`name_ledger`）の入れ替えが、入れ替えの行を確かめ、取り込みの番号を書くこと（要 Postgres）。"""
from __future__ import annotations

import uuid

import pytest

from sherpa import store
from sherpa.store import db, replace_rows
from tests._replace_row import offline_row, row_for

_DOC = {"layer": "version", "status": "indexed"}


def _names(path: str) -> list:
    return [{"name": path.upper(), "kind": "Module", "source": "filename", "rel_path": path}]


@pytest.fixture
def world(tmp_path):
    wid = f"ledger_swap_{uuid.uuid4().hex[:8]}"
    store.upsert_world(wid, str(tmp_path))
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    store.delete_world_row(wid)


def _numbers(wid: str) -> dict:
    with db._connect() as c:
        docs = c.execute("SELECT name, ingest_no, doc_ingest_no FROM documents WHERE kb_id=%s AND version=%s ORDER BY name",
                         (db._KB_ID, wid)).fetchall()
        names = c.execute("SELECT DISTINCT ingest_no FROM name_ledger WHERE world=%s", (wid,)).fetchall()
        built = c.execute("SELECT ingest_no FROM name_ledger_built WHERE world=%s", (wid,)).fetchone()
        shown = c.execute("SELECT shown_ingest_no FROM worlds WHERE kb_id=%s AND world_id=%s",
                          (db._KB_ID, wid)).fetchone()
    return {"docs": {d["name"]: (d["ingest_no"], d["doc_ingest_no"]) for d in docs},
            "names": sorted(n["ingest_no"] for n in names), "built": built and built["ingest_no"],
            "shown": shown["shown_ingest_no"]}


def test_whole_swap_writes_the_row_number_everywhere_and_marks_the_item(world):
    row = row_for(world)
    store.replace_documents(world, [{"name": "a.txt", **_DOC}, {"name": "b.txt", **_DOC}],
                            names=_names("a.txt"), row=row)

    n = _numbers(world)
    assert n["docs"] == {"a.txt": (row.ingest_no, 0), "b.txt": (row.ingest_no, 0)}
    assert n["names"] == [row.ingest_no] and n["built"] == row.ingest_no and n["shown"] == row.ingest_no
    assert replace_rows.read_row(row.id).items["ledger"] == replace_rows.ITEM_CLEANED


def test_single_doc_swap_keeps_the_shown_number_and_other_rows(world):
    whole = row_for(world)
    store.replace_documents(world, [{"name": "a.txt", **_DOC}, {"name": "b.txt", **_DOC}],
                            names=_names("a.txt"), row=whole)
    one = row_for(world, "a.txt")

    store.replace_document(world, "a.txt", {"status": "partial", "layer": "version"}, row=one)

    n = _numbers(world)
    assert n["docs"] == {"a.txt": (whole.ingest_no, one.ingest_no), "b.txt": (whole.ingest_no, 0)}
    assert n["shown"] == whole.ingest_no and n["names"] == [whole.ingest_no] and n["built"] == whole.ingest_no
    assert store.get_document(world, "a.txt")["status"] == "partial"

    store.replace_document(world, "a.txt", None, row=one)
    assert set(_numbers(world)["docs"]) == {"b.txt"}


def test_swap_with_a_row_of_another_world_is_refused_and_writes_nothing(world):
    store.replace_documents(world, [{"name": "a.txt", **_DOC}], row=offline_row(world, ingest_no=7))
    other = row_for(world + "_x")
    try:
        with pytest.raises(ValueError):
            store.replace_documents(world, [{"name": "z.txt", **_DOC}], row=other)
    finally:
        store.delete_world_row(world + "_x")
    assert set(_numbers(world)["docs"]) == {"a.txt"}


def test_swap_with_a_row_that_lost_its_lock_is_refused_and_writes_nothing(world):
    stale = row_for(world)
    row_for(world)   # 同じ資料フォルダの行が作り直され、古い行の鍵は自分のものでなくなる
    store.replace_documents(world, [{"name": "a.txt", **_DOC}], row=offline_row(world, ingest_no=7))

    with pytest.raises(replace_rows.RowLockLost):
        store.replace_documents(world, [{"name": "z.txt", **_DOC}], names=_names("z.txt"), row=stale)
    with pytest.raises(replace_rows.RowLockLost):
        store.replace_document(world, "a.txt", None, row=stale)

    assert set(_numbers(world)["docs"]) == {"a.txt"}
