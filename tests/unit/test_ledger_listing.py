"""一覧の道具（list_docs／glob_search／folder_tree）が、最後の取り込みの台帳から資料フォルダを歩かずに答えること。

資料フォルダがネットワーク共有にあると、道具の呼び出しごとの走査が遅くて時間切れになる。
取り込み直後の同じ資料で、台帳から出した結果と歩いた結果が（`ledger_at` を除いて）一致することを固定する。
台帳の書き込みは取り込みと同じ `worker._ledger_rows`、DB は実物（モックは資料フォルダの場所の解決と走査の計数だけ）。
"""
from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest

from sherpa import doc_ledger, store, tool_dispatch, worlds
from sherpa.ingest import worker

pytestmark = pytest.mark.unit

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/design/notes.txt": "plain notes\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen1/src/PROG2.cbl": "       PROGRAM-ID. PROG2.\n",
    "gen1/src/lib/Util.java": "class Util {}\n",
    "gen1/common/COPY1.cpy": "       01 REC-A PIC X.\n",
    "gen2/design/spec_a.md": "# spec A v2\n",
    "gen2/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "loose.md": "# loose\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "ledgerlist_" + uuid.uuid4().hex[:8]
    wd = tmp_path / "world"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    yield wid
    store.replace_documents(wid, [])
    with store._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version=%s", (wid,))


def _ingest(wid, *, flags=None, complete=True):
    """取り込み 1 回分（run を始め、台帳を書き、run を終える。本番と同じ順）。`complete=False` は台帳だけで run が無い状態。"""
    run = store.start_ingest_run(wid) if complete else None
    store.replace_documents(wid, worker._ledger_rows(wid))
    if run is not None:
        store.finish_ingest_run(run["id"], status="auto_published", extraction_snapshot={"flags": flags or []},
                                published_snapshot={"nodes": 1, "edges": 1})


def _ledger_time(wid):
    """台帳の行の最大更新時刻（`ledger_at` の出どころ）。"""
    with store._connect() as c:
        return c.execute("SELECT max(updated_at) AS at FROM documents WHERE kb_id=%s AND version=%s",
                         (store._KB_ID, wid)).fetchone()["at"]


def _failed_run(wid, *, flags, replace_ledger):
    """失敗した取り込み。`replace_ledger=True` は台帳を置き換えてから失敗、False は台帳に触れる前に失敗。"""
    run = store.start_ingest_run(wid)
    if replace_ledger:
        store.replace_documents(wid, worker._ledger_rows(wid))
    store.finish_ingest_run(run["id"], status="failed", extraction_snapshot={"flags": flags})


def _count_walks(monkeypatch):
    calls = {"n": 0}
    real = os.scandir

    def _counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    monkeypatch.setattr(os, "scandir", _counting)
    return calls


_CALLS = [
    ("list_docs", {}, None, None),
    ("list_docs", {"limit": 3}, None, None),
    ("list_docs", {"limit": 3, "offset": 4}, None, None),
    ("list_docs", {"path_prefix": "gen1/src"}, None, None),
    ("list_docs", {"name_pattern": "PROG"}, None, None),
    ("list_docs", {"doctype": "cobol"}, None, None),
    ("list_docs", {"state": "unreadable"}, None, None),
    ("list_docs", {"state": "ready"}, None, None),
    ("list_docs", {}, ["gen1/src", "gen2"], None),
    ("list_docs", {}, None, "docs"),
    ("list_docs", {}, None, "code"),
    ("list_docs", {"path_prefix": "gen1"}, ["gen1/design"], "docs"),
    ("glob_search", {"pattern": "**/*.cbl"}, None, None),
    ("glob_search", {"pattern": "gen1/**"}, None, None),
    ("glob_search", {"pattern": "**/*"}, ["gen2"], None),
    ("glob_search", {"pattern": "**/*"}, None, "code"),
    ("folder_tree", {}, None, None),
    ("folder_tree", {"depth": 1}, None, None),
    ("folder_tree", {"path_prefix": "gen1", "depth": 2}, None, None),
    ("folder_tree", {}, ["gen1/src"], None),
    ("folder_tree", {}, None, "docs"),
]


def _run_all(wid):
    return [tool_dispatch.run_tool(name, args, wid, sp, layer=layer)[0] for name, args, sp, layer in _CALLS]


def test_listing_from_ledger_matches_walk_and_does_not_walk(world, monkeypatch):
    # 台帳に載る状態の差（読み取れない文書）も含めて比べる
    _ingest(world, flags=[{"doc": "gen1/src/PROG2.cbl", "action": "blocked", "reason": "unreadable_code_file"}])

    walks = _count_walks(monkeypatch)
    from_ledger = _run_all(world)
    assert walks["n"] == 0, "台帳がある資料フォルダで一覧の道具が資料フォルダを歩いた"
    assert all(r.get("ledger_at") for r in from_ledger)

    store.replace_documents(world, [])          # 台帳を消すと、同じ資料を歩いて答える
    from_walk = _run_all(world)
    assert walks["n"] > 0
    assert not any("ledger_at" in r for r in from_walk)

    for (name, args, sp, layer), a, b in zip(_CALLS, from_ledger, from_walk):
        a = {k: v for k, v in a.items() if k != "ledger_at"}
        assert a == b, (name, args, sp, layer)
    # 比較が空振りでないこと（読み取れない文書が 1 件、両方に出ている）
    idx = _CALLS.index(("list_docs", {"state": "unreadable"}, None, None))
    assert from_walk[idx]["count"] == 1 and from_ledger[idx]["count"] == 1


def test_blocked_flag_of_last_run_marks_unreadable_in_ledger_listing(world):
    _ingest(world, flags=[{"doc": "gen1/src/PROG2.cbl", "action": "blocked", "reason": "unreadable_code_file"}])
    res = tool_dispatch.run_tool("list_docs", {"state": "unreadable"}, world, None)[0]
    assert [d["rel_path"] for d in res["docs"]] == ["gen1/src/PROG2.cbl"]
    assert res["ledger_at"]


def test_ledger_at_is_time_the_ledger_rows_were_written(world):
    _ingest(world)
    res = tool_dispatch.run_tool("glob_search", {"pattern": "**/*.cbl"}, world, None)[0]
    assert res["ledger_at"] == _ledger_time(world).isoformat()


_BLOCK_PROG1 = [{"doc": "gen1/src/PROG1.cbl", "action": "blocked", "reason": "unreadable_code_file"}]
_BLOCK_PROG2 = [{"doc": "gen1/src/PROG2.cbl", "action": "blocked", "reason": "unreadable_code_file"}]


def _unreadable_paths(wid):
    res = tool_dispatch.run_tool("list_docs", {"state": "unreadable"}, wid, None)[0]
    return [d["rel_path"] for d in res["docs"]], res["ledger_at"]


def test_failed_run_that_replaced_the_ledger_is_the_ledger_time_and_its_blocked(world):
    _ingest(world)
    after_success = _ledger_time(world)
    _failed_run(world, flags=_BLOCK_PROG2, replace_ledger=True)     # 台帳を置き換えてから失敗した run
    replaced = _ledger_time(world)
    assert replaced > after_success
    paths, at = _unreadable_paths(world)
    assert at == replaced.isoformat()
    assert paths == ["gen1/src/PROG2.cbl"]


def test_failed_run_before_ledger_write_does_not_move_ledger_time_or_add_blocked(world):
    _ingest(world, flags=_BLOCK_PROG2)
    written = _ledger_time(world)
    _failed_run(world, flags=_BLOCK_PROG1, replace_ledger=False)    # 台帳に触れる前に失敗した新しい run
    paths, at = _unreadable_paths(world)
    assert at == written.isoformat()
    assert paths == ["gen1/src/PROG2.cbl"]                         # 台帳を置き換えた run の blocked だけ


def test_running_run_that_replaced_the_ledger_does_not_borrow_older_blocked(world):
    _ingest(world, flags=_BLOCK_PROG1)
    run = store.start_ingest_run(world)                              # 台帳を置き換えて ES の段にいる run（flags は未確定）
    store.replace_documents(world, worker._ledger_rows(world))
    res = tool_dispatch.run_tool("list_docs", {}, world, None, layer="code")[0]
    states = {d["rel_path"]: d["state"] for d in res["docs"]}
    assert states["gen1/src/PROG1.cbl"] == "unknown"                # 前の run の blocked は使わず、確認できない扱い
    assert res["ledger_at"] == _ledger_time(world).isoformat()
    store.finish_ingest_run(run["id"], status="failed", extraction_snapshot={"flags": []})


def test_ledger_without_a_run_is_listed_with_unknown_source_state(world, monkeypatch):
    _ingest(world, complete=False)
    walks = _count_walks(monkeypatch)
    res = tool_dispatch.run_tool("list_docs", {}, world, None, layer="code")[0]
    assert walks["n"] == 0 and res["ledger_at"]
    assert {d["state"] for d in res["docs"]} == {"unknown"}


def test_running_ingest_does_not_hide_the_last_finished_ledger(world, monkeypatch):
    _ingest(world)
    store.start_ingest_run(world)                # 取り込み中（extracting）の run
    walks = _count_walks(monkeypatch)
    res = tool_dispatch.run_tool("list_docs", {}, world, None)[0]
    assert walks["n"] == 0
    assert res["count"] == len(_FILES) and res["ledger_at"]


def test_world_without_ledger_walks_as_before(world, monkeypatch):
    walks = _count_walks(monkeypatch)
    res = tool_dispatch.run_tool("list_docs", {}, world, None)[0]
    assert walks["n"] > 0
    assert res["count"] == len(_FILES) and "ledger_at" not in res


def test_missing_doctype_and_branch_are_filled_from_name(world, monkeypatch):
    _ingest(world)
    rows = store.list_documents(world)
    for r in rows:
        if r["name"] in ("gen1/src/PROG1.cbl", "gen1/design/spec_a.md"):
            r["doctype"] = None
            r["branch"] = None
    store.replace_documents(world, rows)
    walks = _count_walks(monkeypatch)
    got = {d["rel_path"]: d["doctype"] for d in tool_dispatch.run_tool("list_docs", {}, world, None)[0]["docs"]}
    assert walks["n"] == 0
    assert got["gen1/src/PROG1.cbl"] == "cobol"
    assert got["gen1/design/spec_a.md"] == "設計書"
    code = tool_dispatch.run_tool("glob_search", {"pattern": "**/PROG1.cbl"}, world, None, layer="code")[0]
    assert code["paths"] == ["gen1/src/PROG1.cbl", "gen2/src/PROG1.cbl"]


def test_row_not_completable_from_name_falls_back_to_walk(world, monkeypatch):
    _ingest(world)
    rows = store.list_documents(world)
    for r in rows:
        if r["name"] == "loose.md":
            r["name"] = "unknown_kind.zzzqqq"      # 名前から種別が決まらない
            r["doctype"] = None
            r["branch"] = None
    store.replace_documents(world, rows)
    walks = _count_walks(monkeypatch)
    res = tool_dispatch.run_tool("list_docs", {}, world, None)[0]
    assert walks["n"] > 0 and "ledger_at" not in res


def test_listing_documents_returns_rows_sorted_by_name(world):
    _ingest(world)
    names = [r["name"] for r in doc_ledger.listing_documents(world).rows]
    assert names == sorted(_FILES)
    assert {Path(n).name for n in names} == {Path(n).name for n in _FILES}


def test_unreadable_row_missing_doctype_and_branch_is_filled_and_not_listed_as_docs(world):
    _ingest(world)
    rows = store.list_documents(world)
    for r in rows:
        if r["name"] == "gen1/src/PROG2.cbl":
            r["status"], r["doctype"], r["branch"] = "unreadable", None, None
    store.replace_documents(world, rows)
    from_ledger = _run_all(world)
    docs = tool_dispatch.run_tool("list_docs", {}, world, None, layer="docs")[0]
    assert "gen1/src/PROG2.cbl" not in {d["rel_path"] for d in docs["docs"]}
    assert docs["ledger_at"]
    code = tool_dispatch.run_tool("list_docs", {"state": "unreadable"}, world, None, layer="code")[0]
    assert [d["rel_path"] for d in code["docs"]] == ["gen1/src/PROG2.cbl"]
    assert all(r.get("ledger_at") for r in from_ledger)


def test_unreadable_row_not_completable_from_name_falls_back_to_walk(world, monkeypatch):
    _ingest(world)
    rows = store.list_documents(world)
    for r in rows:
        if r["name"] == "loose.md":
            r["name"], r["status"], r["doctype"], r["branch"] = "unknown_kind.zzzqqq", "unreadable", None, None
    store.replace_documents(world, rows)
    walks = _count_walks(monkeypatch)
    res = tool_dispatch.run_tool("list_docs", {}, world, None)[0]
    assert walks["n"] > 0 and "ledger_at" not in res


def test_ledger_is_not_read_while_world_is_being_swapped(world, monkeypatch):
    _ingest(world)
    monkeypatch.setattr(doc_ledger, "SWAP_LOCK_WAIT_MS", 200)
    reads = {"n": 0}
    real = store.read_ledger_for_listing

    def _spy(w):
        reads["n"] += 1
        return real(w)

    monkeypatch.setattr(store, "read_ledger_for_listing", _spy)
    walks = _count_walks(monkeypatch)
    with store.world_swap_lock(world):                  # rebind／削除の最中
        res = tool_dispatch.run_tool("list_docs", {}, world, None)[0]
    assert reads["n"] == 0
    assert walks["n"] > 0 and "ledger_at" not in res and res["count"] == len(_FILES)
    assert tool_dispatch.run_tool("list_docs", {}, world, None)[0]["ledger_at"]   # 終われば台帳から出す
    assert reads["n"] == 1


def test_listing_reads_only_the_columns_the_tools_use(world):
    _ingest(world)
    snap = store.read_ledger_for_listing(world)
    assert len(snap["rows"]) == len(_FILES)
    assert all(set(r) == {"name", "doctype", "branch", "status"} for r in snap["rows"])
    assert snap["ledger_at"] == _ledger_time(world)
    assert set(snap["run"]) == {"status", "flags"}
