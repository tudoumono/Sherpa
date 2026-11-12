"""名前の台帳（`name_ledger`）の書き込み・入れ替え・削除と、道具 `name_lookup`（実 Postgres・取り込みと同じ関数で行を作る）。

架空のソース: ア＝中に書かれた名前（末尾 0）とファイル名（末尾 1）がずれる／イ＝中の名前もファイル名も末尾 1 で、実行される名前（末尾 0）がどこにも無い。
モックは資料フォルダの場所の解決だけ。グラフ（Neo4j）は使わない（`world_graph.build_world` の結果のノードを台帳へ渡す）。
"""
from __future__ import annotations

import uuid

import pytest

from sherpa import store, tool_dispatch, worlds
from sherpa.ingest import ledger_step, name_rows, world_graph, worker
from sherpa.store import name_ledger
from tests._replace_row import offline_row

pytestmark = pytest.mark.unit

_FILES = {
    "gen1/src/ZZAPROG1.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. ZZAPROG0.\n       PROCEDURE DIVISION.\n           STOP RUN.\n",
    "gen1/src/ZZBPROG1.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. ZZBPROG1.\n       PROCEDURE DIVISION.\n           STOP RUN.\n",
    "gen2/src/ZZAPROG1.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. ZZAPROG0.\n       PROCEDURE DIVISION.\n           STOP RUN.\n",
    "gen1/design/spec.md": "# ZZAPROG0 の仕様\n",
    "gen1/src/.env": "ZZSECRET1=x\n",
}


@pytest.fixture
def world(monkeypatch, tmp_path):
    wid = "nameledger_" + uuid.uuid4().hex[:8]
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
    store.replace_documents(wid, [], row=offline_row(wid))


def _ingest(world: str) -> list:
    rows = worker._ledger_rows(world)
    nodes, _edges, _flags = world_graph.build_world(worlds.world_dir(world), world)
    ledger_step.write_ledger_step(world, rows, nodes, row=offline_row(world))
    return rows


def _lookup(world, name, *, scope=None, layer=None, **kw):
    return tool_dispatch.run_tool("name_lookup", {"name": name, **kw}, world, scope, layer=layer)[0]


def test_declared_name_finds_file_with_other_name(world):
    _ingest(world)
    res = _lookup(world, "zzaprog0")
    got = {(m["source"], m["rel_path"]) for m in res["matches"]}
    assert got == {("declared", "gen1/src/ZZAPROG1.cbl"), ("declared", "gen2/src/ZZAPROG1.cbl")}
    assert "candidates" not in res or res["candidates"] == []
    assert all(m["kind"] == "Module" for m in res["matches"])


def test_filename_matches_by_stem(world):
    _ingest(world)
    res = _lookup(world, "ZZAPROG1")
    assert {(m["source"], m["rel_path"]) for m in res["matches"]} == {
        ("filename", "gen1/src/ZZAPROG1.cbl"), ("filename", "gen2/src/ZZAPROG1.cbl")}


def test_near_name_is_unconfirmed_candidate(world):
    _ingest(world)
    res = _lookup(world, "ZZBPROG0")
    assert res["matches"] == [] and res["unconfirmed"] is True
    assert {(c["name"], c["rel_path"], c["source"]) for c in res["candidates"]} == {
        ("ZZBPROG1", "gen1/src/ZZBPROG1.cbl", "declared"), ("ZZBPROG1", "gen1/src/ZZBPROG1.cbl", "filename")}
    assert all(c["unconfirmed"] is True for c in res["candidates"])
    assert "確認していない候補です" in res["notice"]


def test_near_name_needs_same_digit_count_and_other_text_same(world):
    _ingest(world)
    assert _lookup(world, "ZZBPROG10")["candidates"] == []     # 桁数が違う
    assert _lookup(world, "ZZCPROG0")["candidates"] == []      # 数字以外が違う
    assert _lookup(world, "ZZBPROG")["candidates"] == []       # 末尾が数字でない
    res = _lookup(world, "ZZNOTHING9")
    assert res["matches"] == [] and "ripgrep_search" in res["notice"]


def test_candidates_are_capped(world):
    name_ledger.replace_rows(world, [{"name": f"CAPPQ{i:03d}", "kind": "Module", "source": "filename", "rel_path": f"x/CAPPQ{i:03d}.cbl"}
                                     for i in range(1, 30)], row=offline_row(world))
    res = _lookup(world, "CAPPQ000")
    assert len(res["candidates"]) == 20 and res["candidates_total"] == 29


def test_scope_layer_and_secret_filters(world):
    _ingest(world)
    scoped = _lookup(world, "ZZAPROG0", scope=["gen1"])
    assert [m["rel_path"] for m in scoped["matches"]] == ["gen1/src/ZZAPROG1.cbl"]
    assert _lookup(world, "ZZAPROG0", layer="docs")["matches"] == []
    assert len(_lookup(world, "ZZAPROG0", layer="code")["matches"]) == 2
    assert _lookup(world, "ZZAPROG0", kind="Table")["matches"] == []
    assert len(_lookup(world, "ZZAPROG0", kind="module")["matches"]) == 2
    name_ledger.replace_rows(world, [{"name": "ZZSECRET1", "kind": "", "source": "filename", "rel_path": "gen1/src/.env"}], row=offline_row(world))
    assert _lookup(world, "ZZSECRET1")["matches"] == []
    assert not any(r["rel_path"].endswith(".env") for r in name_rows.build_rows(
        [{"name": "gen1/src/.env", "branch": "source"}], []))


def test_scope_filters_candidates_too(world):
    _ingest(world)
    res = _lookup(world, "ZZBPROG0", scope=["gen2"])
    assert res["candidates"] == [] and "unconfirmed" not in res


def test_ingest_replaces_and_delete_clears(world, monkeypatch, tmp_path):
    _ingest(world)
    assert name_ledger.is_built(world)
    (worlds.world_dir(world) / "gen2/src/ZZAPROG1.cbl").unlink()
    _ingest(world)
    assert [m["rel_path"] for m in _lookup(world, "ZZAPROG0")["matches"]] == ["gen1/src/ZZAPROG1.cbl"]
    store.replace_documents(world, [], row=offline_row(world))
    assert not name_ledger.is_built(world)
    res = _lookup(world, "ZZAPROG0")
    assert res["degrade_reason"] == "name_ledger_empty"


def test_lookup_works_without_graph(world, monkeypatch):
    _ingest(world)
    from sherpa.ingest import world_neo4j
    monkeypatch.setattr(world_neo4j, "_env", lambda: (_ for _ in ()).throw(AssertionError("グラフを読まない")))
    assert len(_lookup(world, "ZZAPROG0")["matches"]) == 2


def test_name_is_required(world):
    assert "error" in _lookup(world, "  ")


def test_near_helpers():
    assert name_rows.is_near("ABC0", "ABC1") and not name_rows.is_near("ABC0", "ABC0")
    assert not name_rows.is_near("ABC0", "ABD1") and not name_rows.is_near("ABC", "ABC1")
    assert name_rows.near_key("123") is None
    assert name_rows.normalize_name(" ｚｚａ０ ") == "ZZA0"


def test_ledger_with_no_names_is_built_not_missing(world):
    """名前が 1 つも無い資料フォルダでも、取り込みで作った台帳は「作った」と分かる（「台帳なし」にしない）。"""
    store.replace_documents(world, [{"name": "a.md", "branch": "office", "doctype": "markdown"}], names=[], row=offline_row(world))
    assert name_ledger.is_built(world)
    assert "degrade_reason" not in _lookup(world, "ZZAPROG0")


def test_documents_and_names_are_replaced_in_one_transaction(world):
    """名前の台帳の書き込みが失敗したら、取り込みの台帳も前のまま（片方だけ新しくならない）。"""
    _ingest(world)
    before = [r["name"] for r in store.list_documents(world)]
    try:
        store.replace_documents(world, [{"name": "new.md"}], names=[{"name": "X"}], row=offline_row(world))   # 行に source が無く失敗する
    except Exception:
        pass
    assert [r["name"] for r in store.list_documents(world)] == before
    assert name_ledger.is_built(world)



def test_name_rules_match_with_via_rule_and_no_candidates(world, tmp_path):
    """決まりで置き換えた名前の一致は `via_rule` 付きで返り、近い名前の候補は出ない。範囲の絞り込みは通常の一致と同じ。"""
    store.upsert_world(world, str(tmp_path / "world"))
    _ingest(world)
    try:
        assert _lookup(world, "ZZBPROG0")["candidates"]          # 決まり無しなら確認していない候補になる
        store.set_resolve_settings(world, {"copy_paths": [], "path_aliases": {}, "name_rules": [{"from": "*0", "to": "*1"}]})
        res = _lookup(world, "ZZBPROG0")
        assert "candidates" not in res and "unconfirmed" not in res and "notice" not in res
        assert {(m["source"], m["rel_path"]) for m in res["matches"]} == {
            ("declared", "gen1/src/ZZBPROG1.cbl"), ("filename", "gen1/src/ZZBPROG1.cbl")}
        assert all(m["via_rule"] == {"from": "*0", "to": "*1", "name": "ZZBPROG1"} for m in res["matches"])
        assert _lookup(world, "ZZBPROG0", scope=["gen2"])["matches"] == []
        assert all("via_rule" not in m for m in _lookup(world, "ZZAPROG0")["matches"])    # 確認済みの一致には付けない
    finally:
        store.delete_world_row(world)


def test_name_rule_with_question_mark_returns_all_matches_and_escapes_like(world, tmp_path):
    """`?` を含む決まりは拡張子の有無を問わず一致した行を全部返す。名前の `_`・`%` は 1 文字の代わりにならない。"""
    store.upsert_world(world, str(tmp_path / "world"))
    _ingest(world)
    name_ledger.replace_rows(world, [
        {"name": "ZZCPROG01.C", "kind": "Module", "source": "declared", "rel_path": "gen1/c/ZZCPROG01.c"},
        {"name": "ZZCPROG02", "kind": "Module", "source": "filename", "rel_path": "gen1/c/ZZCPROG02.c"},
        {"name": "ZZCPROG0_", "kind": "Module", "source": "declared", "rel_path": "gen1/c/under.c"},
        {"name": "ZZCPROG0%", "kind": "Module", "source": "declared", "rel_path": "gen1/c/percent.c"},
        {"name": "ZZCPROG011", "kind": "Module", "source": "declared", "rel_path": "gen1/c/long.c"},
    ], row=offline_row(world))
    try:
        store.set_resolve_settings(world, {"copy_paths": [], "path_aliases": {}, "name_rules": [{"from": "*00", "to": "*0?"}]})
        res = _lookup(world, "ZZCPROG00")
        assert {m["rel_path"] for m in res["matches"]} == {
            "gen1/c/ZZCPROG01.c", "gen1/c/ZZCPROG02.c", "gen1/c/under.c", "gen1/c/percent.c"}
        assert all(m["via_rule"]["from"] == "*00" and m["via_rule"]["name"] == "ZZCPROG0?" for m in res["matches"])
        assert "candidates" not in res
        store.set_resolve_settings(world, {"copy_paths": [], "path_aliases": {}, "name_rules": [{"from": "*00", "to": "*0_"}]})
        assert {m["rel_path"] for m in _lookup(world, "ZZCPROG00")["matches"]} == {"gen1/c/under.c"}
        store.set_resolve_settings(world, {"copy_paths": [], "path_aliases": {}, "name_rules": [{"from": "*00", "to": "*0%"}]})
        assert {m["rel_path"] for m in _lookup(world, "ZZCPROG00")["matches"]} == {"gen1/c/percent.c"}
    finally:
        store.delete_world_row(world)


def test_name_lookup_stops_at_the_first_rule_with_matches(world, tmp_path):
    """グラフと同じく、一致が出た最初の決まりで止め、後ろの決まりの一致は返さない。"""
    store.upsert_world(world, str(tmp_path / "world"))
    name_ledger.replace_rows(world, [
        {"name": "ZZAX1", "kind": "Module", "source": "filename", "rel_path": "gen1/src/ZZAX1.cbl"},
        {"name": "ZZBZZAX1", "kind": "Module", "source": "filename", "rel_path": "gen1/src/ZZBZZAX1.cbl"},
    ], row=offline_row(world))
    try:
        store.set_resolve_settings(world, {"copy_paths": [], "path_aliases": {}, "name_rules": [
            {"from": "ZZA*0", "to": "ZZA*1"}, {"from": "*0", "to": "ZZB*1"}]})
        assert [m["rel_path"] for m in _lookup(world, "ZZAX0")["matches"]] == ["gen1/src/ZZAX1.cbl"]
    finally:
        store.delete_world_row(world)
