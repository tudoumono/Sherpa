"""1 文書の入れ直しで、目録とその文書の名前の台帳の行が同じトランザクションで入れ替わることを実 DB で確かめる（名前を引く道具は MCP の `name_lookup`）。"""
from __future__ import annotations

import json
import uuid

import pytest

from sherpa import mcp_server as M
from sherpa import store
from sherpa.ingest import doc_names, name_rows, worker
from sherpa.store import db, name_ledger
from tests._replace_row import offline_row

pytestmark = pytest.mark.integration

A, B = "prog/a.cbl", "prog/b.cbl"


def _node(name, path):
    return {"analyzer": "cobol", "label": "Module", "name": name, "path": path}


@pytest.fixture
def world(tmp_path, monkeypatch):
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")
    wid = f"test-docname-{uuid.uuid4().hex[:10]}"
    for rel in (A, B):
        (tmp_path / rel).parent.mkdir(exist_ok=True)
        (tmp_path / rel).write_text("       IDENTIFICATION DIVISION.\n", encoding="utf-8")
    store.upsert_world(wid, str(tmp_path))
    rows = worker._ledger_rows(wid)
    for r in rows:
        if r["name"] == A:
            r["status"] = "stale"
    nodes = [_node("OLDNAME", A), _node("OTHER", B)]
    store.replace_documents(wid, rows, names=name_rows.build_rows(rows, nodes), row=offline_row(wid, ingest_no=1))
    monkeypatch.setenv("SHERPA_MCP_WORLD", wid)
    monkeypatch.delenv("SHERPA_MCP_SCOPE", raising=False)
    M._seen_tool_calls.clear()
    yield wid, tmp_path
    store.replace_documents(wid, [], row=offline_row(wid))
    store.delete_world_row(wid)
    M._seen_tool_calls.clear()


def _lookup(name):
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "name_lookup", "arguments": {"name": name}}})
    return json.loads(resp["result"]["content"][0]["text"])


def _found(name):
    return [m["rel_path"] for m in _lookup(name).get("matches", [])]


def _fix(wid, root, graph, number=2):
    worker._fix_ledger_row(wid, root, root / A, A, "sig", graph, row=offline_row(wid, ingest_no=number))


def _shown(wid):
    with db._connect() as c:
        return c.execute("SELECT shown_ingest_no FROM worlds WHERE world_id=%s", (wid,)).fetchone()["shown_ingest_no"]


def _pair(wid):
    doc = store.get_document(wid, A)
    return (doc["status"] if doc else None), sorted(r["name"] for r in name_ledger.read_rows(wid) if r["rel_path"] == A)


def _block_inserts_of(name):
    with db._connect() as c:
        c.execute("CREATE OR REPLACE FUNCTION docname_block() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN "
                  f"IF NEW.name = '{name}' THEN RAISE EXCEPTION 'blocked'; END IF; RETURN NEW; END $$")
        c.execute("CREATE TRIGGER docname_block_trigger BEFORE INSERT ON name_ledger FOR EACH ROW EXECUTE FUNCTION docname_block()")


def _unblock():
    with db._connect() as c:
        c.execute("DROP TRIGGER IF EXISTS docname_block_trigger ON name_ledger")
        c.execute("DROP FUNCTION IF EXISTS docname_block()")


def test_changed_declared_name_is_found_under_the_new_name_only(world):
    wid, root = world
    assert _found("OLDNAME") == [A]
    shown = _shown(wid)

    _fix(wid, root, [[_node("NEWNAME", A), _node("OTHER", B)], []])

    assert _found("NEWNAME") == [A] and _found("OLDNAME") == []
    assert _found("OTHER") == [B]
    assert _shown(wid) == shown
    assert _pair(wid)[0] == "indexed"


def test_a_failed_name_write_leaves_the_document_row_as_it_was(world):
    wid, root = world
    _block_inserts_of("NEWNAME")
    try:
        with pytest.raises(Exception):
            _fix(wid, root, [[_node("NEWNAME", A)], []])
    finally:
        _unblock()
    assert _pair(wid) == ("stale", ["A", "OLDNAME"]) and _found("OLDNAME") == [A]


def test_a_removed_document_loses_both_rows(world):
    wid, _root = world

    doc_names.swap(wid, A, None, [[_node("OTHER", B)], []], row=offline_row(wid, ingest_no=2))

    assert store.get_document(wid, A) is None and _found("OLDNAME") == []
    assert store.get_document(wid, B) is not None and _found("OTHER") == [B]


def test_redo_after_a_stop_never_splits_the_two_ledgers_and_a_second_run_changes_nothing(world):
    wid, root = world
    graph = [[_node("NEWNAME", A), _node("OTHER", B)], []]
    _block_inserts_of("NEWNAME")
    try:
        with pytest.raises(Exception):
            _fix(wid, root, graph)
        assert _pair(wid) == ("stale", ["A", "OLDNAME"])          # 止まった場面: 目録も名前の台帳も元のまま
    finally:
        _unblock()

    _fix(wid, root, graph)                                   # 再開
    assert _pair(wid) == ("indexed", ["A", "NEWNAME"])
    built = name_ledger.built_at(wid)

    _fix(wid, root, graph, number=3)                         # もう一度流しても何も変わらない
    assert _pair(wid) == ("indexed", ["A", "NEWNAME"]) and name_ledger.built_at(wid) == built
