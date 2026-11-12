"""名前の台帳の差分更新（ファイルの状態の更新）を実 DB で動かす。消えたファイルの名前は消え、足したファイルの名前は入り、ページ送りの世代の印が変わる。"""
from __future__ import annotations

import uuid

import pytest

from sherpa import store
from sherpa.store import name_ledger
from sherpa.store.db import _connect
from tests._replace_row import offline_row

pytestmark = pytest.mark.integration


def _row(name, rel):
    return {"name": name, "kind": "Module", "source": "filename", "rel_path": rel}


@pytest.fixture
def world():
    try:
        store.init_schema()
    except Exception as exc:
        pytest.skip(f"infra down: {exc}")
    wid = f"test-nldelta-{uuid.uuid4().hex[:12]}"
    store.upsert_world(wid, "/nonexistent/" + wid)
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    store.delete_world_row(wid)


def _apply(world, remove, add):
    with _connect() as c:
        shown = c.execute("SELECT shown_ingest_no FROM worlds WHERE world_id=%s", (world,)).fetchone()
        name_ledger.apply_delta_in(c, world, remove, add, shown=shown["shown_ingest_no"] if shown else 0)


def test_delta_swaps_rows_and_moves_the_generation_mark_only_when_rows_change(world):
    name_ledger.replace_rows(world, [_row("ALPHA", "g/ALPHA.cbl"), _row("BETA", "g/BETA.cbl")], row=offline_row(world))
    before = name_ledger.built_at(world)

    _apply(world, ["g/ALPHA.cbl"], [_row("GAMMA", "g/GAMMA.cbl")])
    assert name_ledger.lookup(world, "ALPHA") == []
    assert [r["rel_path"] for r in name_ledger.lookup(world, "GAMMA")] == ["g/GAMMA.cbl"]
    assert [r["rel_path"] for r in name_ledger.lookup(world, "BETA")] == ["g/BETA.cbl"]
    changed = name_ledger.built_at(world)
    assert changed != before

    _apply(world, ["g/NOTHING.cbl"], [])
    assert name_ledger.built_at(world) == changed


def test_delta_writes_nothing_for_a_folder_without_a_ledger(world):
    _apply(world, [], [_row("ALPHA", "g/ALPHA.cbl")])
    assert not name_ledger.is_built(world)
    assert name_ledger.lookup(world, "ALPHA") == []
