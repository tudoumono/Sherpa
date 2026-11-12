"""目録を全部読む所の上限（実 Postgres）。場所の前の部分で絞る読み取り・作り置きの上限・run 1 件の軽い確認。"""
from __future__ import annotations

import uuid

import pytest

from sherpa import preview_service as ps
from sherpa import store
from sherpa.store import db
from tests._replace_row import offline_row

pytestmark = pytest.mark.unit


@pytest.fixture
def world(tmp_path):
    wid = "readbound_" + uuid.uuid4().hex[:8]
    store.upsert_world(wid, str(tmp_path / "w"))
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    store.delete_world_row(wid)


def _rows(*names):
    return [{"name": n, "doctype": "markdown", "branch": "office", "status": "indexed"} for n in names]


def test_prefix_read_returns_only_the_subtree_but_the_whole_ledger_stamp(world):
    names = ["a/b/x.md", "a/b/y.md", "a/bc/z.md", "a/b", "a/c/w.md", "A/b/x.md", "a/b0/q.md", "top.md"]
    store.replace_documents(world, _rows(*names), row=offline_row(world))
    whole = store.read_ledger_for_listing(world)
    sub = store.read_ledger_for_listing(world, ["a/b"])
    assert sorted(r["name"] for r in sub["rows"]) == ["a/b", "a/b/x.md", "a/b/y.md"]
    assert (sub["total"], sub["ledger_at"], sub["run"]) == (len(names), whole["ledger_at"], whole["run"])
    two = store.read_ledger_for_listing(world, ["a/c", "top.md"])
    assert sorted(r["name"] for r in two["rows"]) == ["a/c/w.md", "top.md"]
    assert store.read_ledger_for_listing(world, ["nothing"])["rows"] == []
    assert store.read_ledger_for_listing(world, ["nothing"])["total"] == len(names)


def test_prefix_index_is_the_pattern_ops_index_on_the_shown_number_and_name():
    with db._connect() as c:
        d = c.execute("SELECT indexdef FROM pg_indexes WHERE indexname='doc_ver_no_name_prefix'").fetchone()["indexdef"]
    assert "(kb_id, version, ingest_no, name text_pattern_ops)" in d


def test_ledger_view_cache_is_bounded_by_worlds_and_rows(monkeypatch):
    ps._LEDGER_VIEW_CACHE.clear()
    monkeypatch.setattr(ps, "LEDGER_VIEW_CACHE_MAX_WORLDS", 2)
    monkeypatch.setattr(ps, "LEDGER_VIEW_CACHE_MAX_ROWS", 10)
    view = lambda n: ps._LedgerView([({}, None)] * n, frozenset())  # noqa: E731
    try:
        ps._cache_ledger_view("a", ("s",), view(3))
        ps._cache_ledger_view("b", ("s",), view(3))
        ps._cache_ledger_view("c", ("s",), view(3))
        assert list(ps._LEDGER_VIEW_CACHE) == ["b", "c"]
        ps._cache_ledger_view("d", ("s",), view(9))
        assert list(ps._LEDGER_VIEW_CACHE) == ["d"]
        ps._cache_ledger_view("e", ("s",), view(11))
        assert "e" not in ps._LEDGER_VIEW_CACHE
    finally:
        ps._LEDGER_VIEW_CACHE.clear()


def test_run_mark_returns_only_id_and_status_and_nothing_for_another_world(world):
    run = store.start_ingest_run(world)
    try:
        assert store.get_run_mark(world, run["id"]) == {"id": run["id"], "status": "extracting"}
        assert store.get_run_mark("other_" + world, run["id"]) is None
        assert store.get_run_mark(world, run["id"] + 10_000) is None
    finally:
        with db._connect() as c:
            c.execute("DELETE FROM ingest_runs WHERE version=%s", (world,))
