"""台帳に名前から種類を補えない行があっても、一覧の道具が資料フォルダを歩かずに答えること。

補えない行は、歩いたときと同じ扱いで一覧に入れる。
- 読み取れなかった行（status=unreadable・doctype／branch 空）: 歩いたときと同じ `doctype=None`／`branch=None`／`state=unreadable`（資料の層）。
- それ以外で種類が決まらない行: `state=unknown`・`doctype=None`・`branch=None`（種類が分からない）。層の絞り込みでは資料・コードの両方に残す。
DB は実物。差し替えるのは資料フォルダの場所の解決と、歩く関数 `documents_for` が呼ばれないことの確認だけ。
"""
from __future__ import annotations

import uuid

import pytest

from sherpa import doc_ledger, store, tool_dispatch, worlds
from sherpa.ingest import worker

pytestmark = pytest.mark.unit

_FILES = {
    "gen1/design/spec_a.md": "# spec A\n",
    "gen1/src/PROG1.cbl": "       PROGRAM-ID. PROG1.\n",
    "gen1/src/mystery.zzzqqq": "x\n",
    "gen1/src/broken.zzzqqq": "y\n",
}
_UNKNOWN = "gen1/src/mystery.zzzqqq"
_BROKEN = "gen1/src/broken.zzzqqq"


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "ledgerunres_" + uuid.uuid4().hex[:8]
    wd = tmp_path / "world"
    for rel, text in _FILES.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    der = tmp_path / "derived"
    der.mkdir()
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: der)
    run = store.start_ingest_run(wid)
    store.replace_documents(wid, worker._ledger_rows(wid))
    store.finish_ingest_run(run["id"], status="auto_published", extraction_snapshot={"flags": []},
                            published_snapshot={"nodes": 1, "edges": 1})
    rows = store.list_documents(wid)
    for r in rows:
        if r["name"] == _UNKNOWN:
            r["doctype"], r["branch"], r["status"] = None, None, "indexed"
        elif r["name"] == _BROKEN:
            r["doctype"], r["branch"], r["status"] = None, None, "unreadable"
    store.replace_documents(wid, rows)
    yield wid
    store.replace_documents(wid, [])
    with store._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version=%s", (wid,))


@pytest.fixture
def no_walk(monkeypatch):
    def _boom(*a, **kw):
        raise AssertionError("台帳に補えない行があるだけで資料フォルダを歩いた")

    monkeypatch.setattr(doc_ledger, "documents_for", _boom)


def _by_name(wid):
    return {r["name"]: r for r in doc_ledger.listing_documents(wid).rows}


def test_unresolved_rows_are_listed_without_walking(world, no_walk):
    listing = doc_ledger.listing_documents(world)
    assert listing.ledger_at
    rows = {r["name"]: r for r in listing.rows}
    assert set(rows) == set(_FILES)
    assert rows["gen1/src/PROG1.cbl"]["doctype"] == "cobol" and rows["gen1/src/PROG1.cbl"]["branch"] == "source"
    assert rows["gen1/design/spec_a.md"]["doctype"] == "設計書"


def test_unreadable_unresolved_row_looks_like_walked_row(world, no_walk):
    r = _by_name(world)[_BROKEN]
    assert (r["doctype"], r["branch"], r["state"]) == (None, None, "unreadable")


def test_other_unresolved_row_is_marked_unknown_kind(world, no_walk):
    r = _by_name(world)[_UNKNOWN]
    assert (r["doctype"], r["branch"], r["state"]) == (None, None, "unknown")


def test_list_docs_layer_filter_keeps_unresolved_rows_where_decided(world, no_walk):
    def names(layer, args=None):
        res = tool_dispatch.run_tool("list_docs", args or {}, world, None, layer=layer)[0]
        assert res["ledger_at"]
        return {d["rel_path"] for d in res["docs"]}

    assert _UNKNOWN in names("docs") and _UNKNOWN in names("code")      # 層が決められない行は黙って消さない
    assert _BROKEN in names("docs") and _BROKEN not in names("code")    # 歩いたときと同じ（資料の層）
    assert "gen1/src/PROG1.cbl" in names("code") and "gen1/src/PROG1.cbl" not in names("docs")
    assert names("both") == set(_FILES)
    assert names("both", {"state": "unknown"}) == {_UNKNOWN}
    assert names("both", {"state": "unreadable"}) == {_BROKEN}


def test_glob_and_folder_tree_do_not_walk_with_unresolved_rows(world, no_walk):
    glob = tool_dispatch.run_tool("glob_search", {"pattern": "**/*.zzzqqq"}, world, None, layer="code")[0]
    assert glob["paths"] == [_UNKNOWN] and glob["ledger_at"]
    tree = tool_dispatch.run_tool("folder_tree", {}, world, None, layer="code")[0]
    assert tree["ledger_at"]
    assert {f["path"]: f["direct_files"] for f in tree["folders"]}["gen1/src"] == 2     # PROG1.cbl と未確定の 1 件


def test_unreadable_unresolved_row_matches_walked_row(world):
    # 歩くと「読み取れない」になるファイル（文字コードを判別できない）と、台帳から出した行が同じ形になる
    wd = worlds.world_dir(world)
    (wd / "gen1/src/garbled.cbl").write_bytes(bytes((i * 37) % 128 + 128 for i in range(4000)))
    walked = {r["name"]: r for r in doc_ledger.documents_for(world)}["gen1/src/garbled.cbl"]
    assert (walked["doctype"], walked["branch"], walked["state"]) == (None, None, "unreadable")
    r = _by_name(world)[_BROKEN]
    assert (r["doctype"], r["branch"], r["state"]) == (walked["doctype"], walked["branch"], walked["state"])


def test_kind_unknown_count_and_notice_in_all_three_tools(world, no_walk):
    lo = tool_dispatch.run_tool("list_docs", {}, world, None, layer="code")[0]
    assert lo["kind_unknown"] == 1 and lo["kind_unknown_dropped"] == 0 and "1 件" in lo["kind_unknown_notice"]
    dt = tool_dispatch.run_tool("list_docs", {"doctype": "cobol"}, world, None)[0]
    assert dt["count"] == 1 and dt["kind_unknown"] == 0 and dt["kind_unknown_dropped"] == 1   # 落とした分も知らせる
    gl = tool_dispatch.run_tool("glob_search", {"pattern": "**/*.zzzqqq"}, world, None, layer="docs")[0]
    assert gl["kind_unknown"] == 1 and "確定しない" in gl["kind_unknown_notice"]
    ft = tool_dispatch.run_tool("folder_tree", {}, world, None, layer="code")[0]
    assert ft["kind_unknown"] == 1 and ft["kind_unknown_notice"]


def test_no_kind_unknown_keys_when_none_unknown(world, no_walk):
    for name, args in (("list_docs", {"path_prefix": "gen1/design"}), ("glob_search", {"pattern": "**/*.md"}),
                       ("folder_tree", {"path_prefix": "gen1/design"})):
        res = tool_dispatch.run_tool(name, args, world, None)[0]
        assert not [k for k in res if k.startswith("kind_unknown")], name


def test_row_missing_only_doctype_is_counted_as_unknown_kind(world, no_walk):
    # branch は台帳にあり doctype だけ欠けて名前から補えない行: 層は台帳の branch で決まり、種類の絞り込みの注意には数える
    rows = store.list_documents(world)
    for r in rows:
        if r["name"] == _UNKNOWN:
            r["branch"] = "source"
    store.replace_documents(world, rows)
    r = _by_name(world)[_UNKNOWN]
    assert (r["doctype"], r["branch"], r["state"]) == (None, "source", "unknown")
    code = tool_dispatch.run_tool("list_docs", {}, world, None, layer="code")[0]
    assert _UNKNOWN in {d["rel_path"] for d in code["docs"]} and code["kind_unknown"] == 1
    docs = tool_dispatch.run_tool("list_docs", {}, world, None, layer="docs")[0]
    assert _UNKNOWN not in {d["rel_path"] for d in docs["docs"]}
    dt = tool_dispatch.run_tool("list_docs", {"doctype": "cobol"}, world, None)[0]
    assert dt["kind_unknown_dropped"] == 1 and dt["kind_unknown_notice"]


@pytest.mark.parametrize("doctype,branch", [(None, "source"), ("cobol", None)])
def test_unreadable_row_with_one_kind_value_left_looks_like_walked_row(world, no_walk, doctype, branch):
    # 名前から補えず読み取れない行は、台帳に片側の種類の値が残っていても歩いたときと同じ形（種類なし・資料の層）
    rows = store.list_documents(world)
    for r in rows:
        if r["name"] == _BROKEN:
            r["doctype"], r["branch"] = doctype, branch
    store.replace_documents(world, rows)
    r = _by_name(world)[_BROKEN]
    assert (r["doctype"], r["branch"], r["state"]) == (None, None, "unreadable")
    docs = tool_dispatch.run_tool("list_docs", {}, world, None, layer="docs")[0]
    assert _BROKEN in {d["rel_path"] for d in docs["docs"]}


def test_blocked_unknown_row_becomes_walked_unreadable_shape(world, no_walk):
    # 取り込みの記録で読み取れなかった（blocked）とされた未確定の行も、歩いたときと同じ形になる
    rows = store.list_documents(world)
    for r in rows:
        if r["name"] == _UNKNOWN:
            r["branch"] = "source"
    store.replace_documents(world, rows)
    run = store.start_ingest_run(world)
    store.finish_ingest_run(run["id"], status="auto_published", extraction_snapshot={
        "flags": [{"doc": _UNKNOWN, "action": "blocked", "reason": "unreadable_code_file"}]},
        published_snapshot={"nodes": 1, "edges": 1})
    store.replace_documents(world, store.list_documents(world))   # この run が台帳を置き換えたことにする
    r = _by_name(world)[_UNKNOWN]
    assert (r["doctype"], r["branch"], r["state"]) == (None, None, "unreadable")
