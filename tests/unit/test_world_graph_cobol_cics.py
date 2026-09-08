"""`world_graph.build_world()` 経由の EXEC CICS XCTL/LINK の統合テスト（アナライザ拡張 S5b・
docs/proposals/2026-09-05-アナライザ拡張.md §4(d)/§9 S5b）。

`fixtures/corpus/cobol-cics` を実際に `build_world()` へ通し、`Module(ONLINE1) -INVOKES(via=
cics_xctl)-> Module(MENU01)`・`via=cics_link` → SUBR01/SUBR02・存在しない参照先の `unresolved`
flag（line/via 付き）・`cics_dynamic`/`cics_other` の dropped 申告を固定する。

`cics_xctl`/`cics_link` は `analyzers/_base.KNOWN_VIA`/`VIA_PRIORITY` に登録済み。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "cobol-cics"
WORLD_ID = "cobol_cics_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]): n for n in nodes}


def _edge_tuples(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


def test_module_nodes_present():
    nodes, _edges, _flags = _build()
    by_key = _node_keys(nodes)
    assert ("Module", "ONLINE1", "ONLINE1.cbl") in by_key
    assert ("Module", "MENU01", "MENU01.cbl") in by_key
    assert ("Module", "SUBR01", "SUBR01.cbl") in by_key
    assert ("Module", "SUBR02", "SUBR02.cbl") in by_key


def test_xctl_and_link_yield_invokes_with_expected_via():
    """`XCTL PROGRAM('MENU01')` → `via=cics_xctl`、`LINK PROGRAM('SUBR01')`/継続行分割の
    `LINK PROGRAM('SUB' + '-  'R02')` → `via=cics_link`（実体は `SUBR02`）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    online1 = ("Module", "ONLINE1", "ONLINE1.cbl")
    menu01 = ("Module", "MENU01", "MENU01.cbl")
    subr01 = ("Module", "SUBR01", "SUBR01.cbl")
    subr02 = ("Module", "SUBR02", "SUBR02.cbl")

    assert ("INVOKES", online1, menu01, "cics_xctl") in tuples
    assert ("INVOKES", online1, subr01, "cics_link") in tuples
    assert ("INVOKES", online1, subr02, "cics_link") in tuples


def test_missing_link_target_is_unresolved_flag_with_line_and_via():
    """`LINK PROGRAM('NOPE')`（world 内に存在しない参照先）は誤った先を推測せず、
    `unresolved` flag（`line`/`via` 付き）に倒れる。"""
    nodes, _edges, flags = _build()
    assert not any(n["name"] == "NOPE" for n in nodes)
    matches = [fl for fl in flags
               if fl["reason"] == "unresolved" and fl.get("kind") == "Module" and fl.get("name") == "NOPE"]
    assert len(matches) == 1
    assert matches[0]["via"] == "cics_link"
    assert isinstance(matches[0]["line"], int)


def test_dynamic_and_other_cics_commands_are_dropped_syntax_flags():
    """`XCTL PROGRAM(WS-NEXT)`（動的）→ `cics_dynamic`・`SEND MAP('M1')`（XCTL/LINK 以外）→
    `cics_other` が `dropped_syntax` flag として記録される（黙って落とさない）。"""
    _nodes, _edges, flags = _build()
    whys = {fl["why"] for fl in flags if fl["reason"] == "dropped_syntax" and fl.get("analyzer") == "cobol"}
    assert "cics_dynamic" in whys
    assert "cics_other" in whys
