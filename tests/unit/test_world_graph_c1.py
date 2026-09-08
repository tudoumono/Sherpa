"""`world_graph.build_world()` 経由の C アナライザ統合テスト（アナライザ拡張 S6・
docs/proposals/2026-09-05-アナライザ拡張.md §4(a)/§9 S6/§12）。

`fixtures/corpus/c1` を実際に `build_world()` へ通し、`#include` の2段解決（相対パス完全一致→
拡張子込み basename 最近傍・§12）・関数呼び出しの単純名解決（`.c` 定義側の children を優先し
`.h` プロトタイプへ誤接続しない・§9 S6）・`.c`/`.h` 同居時の拡張子込み primary 名による
disambiguation（RV2-1）・関数ポインタ経由呼び出し/マクロ呼び出しの Dropped 化を固定する。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "c1"
WORLD_ID = "c1_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]): n for n in nodes}


def _edge_tuples(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


def test_c_and_h_primaries_are_distinct_extension_included_modules():
    """RV2-1: 拡張子込みファイル名により `.c`/`.h`（同ディレクトリ同居）が別ノードになる。"""
    nodes, _edges, _flags = _build()
    by_key = _node_keys(nodes)
    assert ("Module", "thing.c", "gen1/pair/thing.c") in by_key
    assert ("Module", "thing.h", "gen1/pair/thing.h") in by_key
    assert by_key[("Module", "thing.c", "gen1/pair/thing.c")]["cid"] != \
        by_key[("Module", "thing.h", "gen1/pair/thing.h")]["cid"]


def test_function_children_are_qualified_with_filename():
    nodes, _edges, _flags = _build()
    by_key = _node_keys(nodes)
    assert by_key[("Module", "util_add", "gen1/src/util.c")]["cid"] == \
        "module:c1_test:gen1/src/util.c#util.c.util_add"
    assert by_key[("Module", "util_add", "gen1/include/util.h")]["cid"] == \
        "module:c1_test:gen1/include/util.h#util.h.util_add"


def test_include_without_path_separator_resolves_to_sole_basename_candidate():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    main_c = ("Module", "main.c", "gen1/src/main.c")
    util_h = ("Module", "util.h", "gen1/include/util.h")
    assert ("INVOKES", main_c, util_h, "include") in tuples


def test_include_with_relative_path_resolves_via_exact_path_match():
    """`#include "../inc/log.h"`（§12 1段目＝相対パス完全一致）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    main_c = ("Module", "main.c", "gen1/src/main.c")
    log_h = ("Module", "log.h", "gen1/inc/log.h")
    assert ("INVOKES", main_c, log_h, "include") in tuples


def test_windows_style_include_separator_resolves_like_forward_slash():
    """`#include "..\\inc\\log.h"`（Windows 区切り）も `../inc/log.h` と同じ扱いで
    `gen1/inc/log.h` へ解決する（analyzer 入口で `\\`→`/` 正規化してから basename・
    `include_path` を組み立てる・§4(a)/§12）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    caller_win_c = ("Module", "caller_win.c", "gen1/win/caller_win.c")
    log_h = ("Module", "log.h", "gen1/inc/log.h")
    assert ("INVOKES", caller_win_c, log_h, "include") in tuples


def test_system_include_is_not_a_reference():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    assert not any(t[3] == "include" and "stdio" in t[2][1] for t in tuples)


def test_duplicate_basename_header_resolves_to_nearer_directory_not_farther_one():
    """`dupA/dup.h`（同ディレクトリ＝距離0）が選ばれ、`dupB/dup.h`（遠い）は選ばれない（RV2-1 の
    受け入れ例と同型の disambiguation 検証）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    caller_c = ("Module", "caller.c", "gen1/dupA/caller.c")
    dup_h_near = ("Module", "dup.h", "gen1/dupA/dup.h")
    dup_h_far = ("Module", "dup.h", "gen1/dupB/dup.h")
    assert ("INVOKES", caller_c, dup_h_near, "include") in tuples
    assert not any(t[0] == "INVOKES" and t[1] == caller_c and t[2] == dup_h_far for t in tuples)


def test_function_call_resolves_to_definition_child_not_prototype_child():
    """`via=call`＝`util_add(1, 2)` は util.c の定義（children）に解決する——同一 top_scope 内で
    util.c（main.c と同ディレクトリ＝距離0）が util.h（別ディレクトリ＝距離2）より近いため
    プロトタイプ側へ誤接続しない（S9 S6 の受け入れ条件）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    main_c = ("Module", "main.c", "gen1/src/main.c")
    util_add_def = ("Module", "util_add", "gen1/src/util.c")
    util_add_proto = ("Module", "util_add", "gen1/include/util.h")
    assert ("INVOKES", main_c, util_add_def, "call") in tuples
    assert not any(t[0] == "INVOKES" and t[1] == main_c and t[2] == util_add_proto and t[3] == "call"
                   for t in tuples)


def test_dynamic_call_via_function_pointer_is_dropped():
    _nodes, _edges, flags = _build()
    dynamic = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == "c_dynamic_call"]
    assert len(dynamic) == 1 and dynamic[0]["from"] == "gen1/src/main.c"


def test_macro_call_is_dropped():
    _nodes, _edges, flags = _build()
    macro = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == "c_macro_call"]
    assert len(macro) == 1 and macro[0]["from"] == "gen1/src/main.c"


def test_function_pointer_variable_declaration_produces_no_dropped_entry():
    """`int (*cb)(int);`（宣言）自体は検出限界として黙って見逃す（誤って dynamic_call にしない）。"""
    _nodes, _edges, flags = _build()
    dynamic = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == "c_dynamic_call"]
    assert all(f["line"] != 7 for f in dynamic)                # 宣言行（`int (*cb)(int);`）ではない


def test_c_source_external_prototype_is_not_a_fake_child_that_swallows_call_resolution():
    """`.c` の `extern int target(int);`（外部プロトタイプ）は children にしない——children にすると
    自ファイルの偽 child が `simple_name_defs` に登録され、同一ファイル距離0で誤って自己解決してしまう。
    `target(1)` は本当の定義（`gen1/ext/target.c`）へ解決する。"""
    nodes, edges, _flags = _build()
    by_key = _node_keys(nodes)
    tuples = _edge_tuples(nodes, edges)
    assert ("Module", "target", "gen1/ext/caller_ext.c") not in by_key
    caller_ext_c = ("Module", "caller_ext.c", "gen1/ext/caller_ext.c")
    target_def = ("Module", "target", "gen1/ext/target.c")
    assert ("INVOKES", caller_ext_c, target_def, "call") in tuples


def test_equidistant_declaration_and_definition_resolves_to_the_definition():
    """`pref.h`（宣言）と `pref.c`（定義）が呼び出し元と同一ディレクトリ（距離0＝等距離）でも、
    定義側を優先する——等距離だからといって `ambiguous` に落とさない（§9・定義優先の中核ケース）。"""
    nodes, edges, flags = _build()
    tuples = _edge_tuples(nodes, edges)
    main2_c = ("Module", "main2.c", "gen1/eqdist/main2.c")
    pref_def = ("Module", "pref_add", "gen1/eqdist/pref.c")
    pref_decl = ("Module", "pref_add", "gen1/eqdist/pref.h")
    assert ("INVOKES", main2_c, pref_def, "call") in tuples
    assert not any(t[0] == "INVOKES" and t[1] == main2_c and t[2] == pref_decl and t[3] == "call"
                   for t in tuples)
    assert not any(f.get("from") == "gen1/eqdist/main2.c" and f.get("name") == "pref_add" for f in flags)


def test_equidistant_definitions_in_two_files_are_ambiguous_not_unresolved():
    """定義（`{` 終端）候補が2ファイルに等距離で存在する場合は `ambiguous` を申告する——2段目の
    判定が1段目の `unresolved` に化けない（是正対象のバグ）。"""
    _nodes, edges, flags = _build()
    tuples = _edge_tuples(_nodes, edges)
    caller_ambig = ("Module", "caller_ambig.c", "gen1/ambig_call/caller_ambig.c")
    assert not any(t[0] == "INVOKES" and t[1] == caller_ambig and t[3] == "call" for t in tuples)
    ambiguous = [f for f in flags if f.get("reason") == "ambiguous"
                and f.get("from") == "gen1/ambig_call/caller_ambig.c" and f.get("name") == "foo_ambig"]
    assert len(ambiguous) == 1


def test_declaration_only_call_resolves_to_the_header_child_when_no_definition_exists():
    """実装が world 内に無い（宣言だけの `.h`）場合は、その宣言 child へ繋がる（定義優先の
    フォールバック側）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    caller_only = ("Module", "caller_only.c", "gen1/decl_only/caller_only.c")
    only_decl = ("Module", "only_decl", "gen1/decl_only/only.h")
    assert ("INVOKES", caller_only, only_decl, "call") in tuples


def test_uppercase_h_extension_header_children_are_indexed_like_lowercase():
    """`.H`（大文字）も `.h` と同じ扱いでヘッダの children を作る（拡張子の大文字小文字は
    区別しない）。"""
    _nodes, _edges, flags = _build()
    by_key = _node_keys(_nodes)
    assert ("Module", "upper_proto", "gen1/anycase/UTIL2.H") in by_key
    assert not any(f.get("reason") == "unknown_label" and "UTIL2.H" in f.get("from", "") for f in flags)
