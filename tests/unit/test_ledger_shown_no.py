"""目録（`documents`）と名前の台帳（`name_ledger`）の読み取りは、資料フォルダの「今見せる番号」の行だけを相手にする（実 Postgres）。
別の番号の行（切り替え前の下書き・消し残した古い番号）が並んでいても、一覧・件数・実在・名前の引きに混ざらない。
"""
from __future__ import annotations

import uuid

import pytest

from sherpa import store
from sherpa.store import db, name_ledger

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path):
    wid = "shownno_" + uuid.uuid4().hex[:8]
    store.upsert_world(wid, str(tmp_path / "w"))
    yield wid
    with db._connect() as c:
        c.execute("DELETE FROM documents WHERE version=%s", (wid,))
        c.execute("DELETE FROM name_ledger WHERE world=%s", (wid,))
        c.execute("DELETE FROM name_ledger_built WHERE world=%s", (wid,))
    store.delete_world_row(wid)


def _doc(c, world, name, no):
    c.execute("INSERT INTO documents (kb_id, version, name, layer, doctype, status, ingest_no) "
              "VALUES (%s,%s,%s,'version','markdown','ok',%s)", (db._KB_ID, world, name, no))


def _name(c, world, name, rel, no):
    c.execute("INSERT INTO name_ledger (world, name, kind, source, rel_path, ingest_no) VALUES (%s,%s,'',%s,%s,%s)",
              (world, name, "filename", rel, no))


def _seed(world, shown):
    with db._connect() as c:
        _doc(c, world, "now.md", shown)
        _doc(c, world, "draft.md", shown + 1)
        _doc(c, world, "old.md", shown - 1)
        _name(c, world, "NOW", "now.md", shown)
        _name(c, world, "DRAFT", "draft.md", shown + 1)
        _name(c, world, "OLD", "old.md", shown - 1)
        c.execute("INSERT INTO name_ledger_built (world, ingest_no) VALUES (%s,%s)", (world, shown))
        c.execute("UPDATE worlds SET shown_ingest_no=%s WHERE kb_id=%s AND world_id=%s", (shown, db._KB_ID, world))


def test_document_reads_see_only_the_shown_number(world):
    _seed(world, 5)
    assert store.list_document_names(world) == ["now.md"]
    assert [r["name"] for r in store.list_documents(world)] == ["now.md"]
    assert store.count_documents(world) == 1
    assert [r["name"] for r in store.list_documents_page(world, limit=10, offset=0)] == ["now.md"]
    total, rows = store.count_and_list_documents(world, limit=None)
    assert (total, [r["name"] for r in rows]) == (1, ["now.md"])
    assert store.document_exists(world, "now.md") and not store.document_exists(world, "draft.md")
    assert store.get_document(world, "old.md") is None
    assert store.ledger_stamp(world)[0] == 1
    assert [r["name"] for r in store.read_ledger_for_listing(world)["rows"]] == ["now.md"]


def test_name_ledger_reads_see_only_the_shown_number(world):
    _seed(world, 5)
    assert [r["rel_path"] for r in name_ledger.read_rows(world)] == ["now.md"]
    assert name_ledger.lookup(world, "NOW") and not name_ledger.lookup(world, "DRAFT")
    assert not name_ledger.lookup_shape(world, "OL?")
    assert name_ledger.like_prefix(world, "N", 2) and not name_ledger.like_prefix(world, "D", 4)
    assert name_ledger.is_built(world)
    with db._connect() as c:
        c.execute("UPDATE worlds SET shown_ingest_no=7 WHERE kb_id=%s AND world_id=%s", (db._KB_ID, world))
    assert not name_ledger.is_built(world) and name_ledger.built_at(world) is None


def test_file_state_delta_writes_to_the_shown_number(world):
    _seed(world, 5)
    w = store.get_world(world)
    expected = {k: w[k] for k in ("root_path", "last_sig", "created_at", "updated_at", "last_synced_at")}
    expected["ledger_stamp"] = store.ledger_stamp(world)
    ok = store.apply_file_state_delta(
        world, add_rows=[{"name": "added.md"}], remove_names=["now.md"], expected=expected,
        add_names=[{"name": "ADDED", "kind": "", "source": "filename", "rel_path": "added.md"}])
    assert ok
    assert store.list_document_names(world) == ["added.md"]
    assert [r["rel_path"] for r in name_ledger.read_rows(world)] == ["added.md"]
    with db._connect() as c:
        left = c.execute("SELECT name FROM documents WHERE version=%s AND ingest_no<>5 ORDER BY name", (world,)).fetchall()
    assert [r["name"] for r in left] == ["draft.md", "old.md"]
