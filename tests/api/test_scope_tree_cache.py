"""範囲の選択肢（`GET /scopes`）・範囲の検証が、資料フォルダごとに 1 回だけ台帳の全件を読んで使い回し、台帳が変わったら作り直すこと。
DB は実物（台帳の書き込みは取り込みと同じ `worker._ledger_rows`／`store.replace_documents`）。差し替えは資料フォルダの場所の解決と、全件読みの回数を数える薄い包みだけ。
"""
from __future__ import annotations

import threading
import time
import uuid

import pytest
from fastapi.testclient import TestClient

from sherpa import scope, store, worlds
from sherpa.api import app
from sherpa.ingest import worker

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen2/design/spec_a.md": "# spec A v2\n",
    "loose.md": "# loose\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path, auth_disabled):
    wid = "scopecache_" + uuid.uuid4().hex[:8]
    wd, arch, der = tmp_path / "world", tmp_path / "arch", tmp_path / "derived"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    arch.mkdir()
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    monkeypatch.setattr(worlds, "archives_dir", lambda w: arch)
    store.replace_documents(wid, worker._ledger_rows(wid))
    yield wid
    store.replace_documents(wid, [])
    scope._INDEX_CACHE.pop(wid, None)


@pytest.fixture
def full_reads(monkeypatch):
    """台帳の全件読み（`store.list_document_names`）の回数を数える。"""
    calls = {"n": 0, "delay": 0.0}
    real = store.list_document_names

    def counting(w):
        calls["n"] += 1
        if calls["delay"]:
            time.sleep(calls["delay"])
        return real(w)
    monkeypatch.setattr(store, "list_document_names", counting)
    return calls


def _row(name: str) -> dict:
    return {"name": name, "doctype": "document", "branch": "doc", "status": "ready"}


def _paths(tree: dict) -> list:
    return [s["path"] for s in tree["scopes"]]


def test_second_call_does_not_read_the_whole_ledger(world, full_reads):
    c = TestClient(app, raise_server_exceptions=False)
    first = c.get("/scopes", params={"world": world})
    second = c.get("/scopes", params={"world": world})
    assert first.status_code == second.status_code == 200
    assert first.json() == second.json()
    assert full_reads["n"] == 1
    assert {"gen1", "gen1/src", "gen2/design"} <= set(_paths(first.json()))


def test_replacing_the_ledger_rebuilds_the_tree(world, full_reads):
    assert "extra" not in _paths(scope.scope_tree(world))
    store.replace_document(world, "extra/new/memo.md", _row("extra/new/memo.md"))
    assert {"extra", "extra/new"} <= set(_paths(scope.scope_tree(world)))
    store.replace_document(world, "extra/new/memo.md", None)
    assert "extra" not in _paths(scope.scope_tree(world))
    rows = worker._ledger_rows(world)
    store.replace_documents(world, [r for r in rows if not r["name"].startswith("gen2/")])
    tree = scope.scope_tree(world)
    assert "gen2" not in _paths(tree) and "gen1" in _paths(tree)
    assert scope.valid_scope_paths(world, ["gen2"]) is False
    assert full_reads["n"] == 4


def test_concurrent_first_calls_read_the_ledger_once(world, full_reads):
    full_reads["delay"] = 0.3
    results, errors = [], []
    barrier = threading.Barrier(6)

    def go():
        try:
            barrier.wait(timeout=5)
            results.append(scope.scope_tree(world))
        except Exception as e:  # pragma: no cover
            errors.append(e)

    threads = [threading.Thread(target=go) for _ in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=20)
    assert not errors
    assert len(results) == 6 and all(r == results[0] for r in results)
    assert full_reads["n"] == 1


def test_etag_gives_304_until_the_ledger_changes(world, full_reads):
    c = TestClient(app, raise_server_exceptions=False)
    r1 = c.get("/scopes", params={"world": world})
    etag = r1.headers["etag"]
    assert r1.headers["cache-control"] == "private, no-cache"
    r2 = c.get("/scopes", params={"world": world}, headers={"If-None-Match": etag})
    assert r2.status_code == 304 and r2.content == b"" and r2.headers["etag"] == etag
    assert full_reads["n"] == 1
    store.replace_document(world, "extra/memo.md", _row("extra/memo.md"))
    r3 = c.get("/scopes", params={"world": world}, headers={"If-None-Match": etag})
    assert r3.status_code == 200 and r3.headers["etag"] != etag
    assert "extra" in _paths(r3.json())
    r4 = c.get("/scopes", params={"world": world}, headers={"If-None-Match": r3.headers["etag"]})
    assert r4.status_code == 304


def test_valid_scope_paths_uses_the_cache_and_keeps_its_answers(world, full_reads):
    sels = (None, [], ["gen1"], ["gen1/src"], ["gen1", "gen2/design"], ["gen1/nothing"], ["gen1/src/PROG1.cbl"])
    expected = [True, True, True, True, True, False, False]
    assert [scope.valid_scope_paths(world, sp) for sp in sels] == expected
    assert [scope.valid_scope_paths(world, sp, root=worlds.world_dir(world), strict=True) for sp in sels] == expected
    assert scope.known_scope_prefixes(world) == {"gen1", "gen1/design", "gen1/src", "gen2", "gen2/design"}
    assert full_reads["n"] == 1


def test_returned_values_do_not_change_the_cache(world):
    tree = scope.scope_tree(world)
    tree["scopes"].clear()
    known = scope.known_scope_prefixes(world)
    known.clear()
    assert scope.scope_tree(world)["scopes"] and scope.known_scope_prefixes(world)


def test_empty_ledger_is_not_cached(world):
    store.replace_documents(world, [])
    assert scope.scope_tree_etag(world) is None
    walked = scope.scope_tree(world)                      # 台帳が無い＝歩いて答える
    assert "gen1" in _paths(walked) and world not in scope._INDEX_CACHE


def test_stamp_failing_inside_the_lock_falls_back_instead_of_failing(world, monkeypatch):
    real = store.ledger_stamp
    calls = {"n": 0}

    def flaky(w):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("db hiccup")
        return real(w)
    monkeypatch.setattr(store, "ledger_stamp", flaky)
    scope._INDEX_CACHE.pop(world, None)
    tree = scope.scope_tree(world)                        # 2 回目の印で失敗しても 500 にせず従来どおり答える
    assert "gen1" in _paths(tree) and world not in scope._INDEX_CACHE
