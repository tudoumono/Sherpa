"""`CAnalyzer` の単体テスト（アナライザ拡張 §4(a)/§9 S6・A1）。"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.c import CAnalyzer

A = CAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".c", ".h"})
    assert A.name == "c"
    assert A.doctype == "c"


def test_accepts_all_c_files_without_content_inspection():
    assert CAnalyzer.accepts is Analyzer.accepts


# --- primary（ファイル自体・拡張子込みファイル名）---

def test_collect_defs_primary_is_extension_included_filename_for_c_source():
    res = A.collect_defs("int f(void) {\n    return 0;\n}\n", "src/foo.c")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "foo.c"


def test_collect_defs_primary_is_extension_included_filename_for_header_only_file():
    """宣言のみの `.h` ファイルも主体を持つ（RV1 是正・関数を primary にしない設計）。"""
    res = A.collect_defs("int f(void);\n", "src/foo.h")
    assert res.primary is not None
    assert res.primary.name == "foo.h"


def test_empty_header_still_has_primary_with_no_children():
    res = A.collect_defs("#ifndef FOO_H\n#define FOO_H\n#endif\n", "foo.h")
    assert res.primary is not None and res.primary.name == "foo.h"
    assert res.children == []


# --- children（関数定義／プロトタイプ・修飾名 `<ファイル名>.<関数名>`）---

def test_function_definition_becomes_qualified_child():
    text = "int util_add(int a, int b) {\n    return a + b;\n}\n"
    res = A.collect_defs(text, "util.c")
    assert [(c.label, c.name, c.cid_key) for c in res.children] == [
        ("Module", "util_add", "util.c.util_add")]


def test_prototype_declaration_becomes_qualified_child():
    res = A.collect_defs("int util_add(int a, int b);\n", "util.h")
    assert [(c.label, c.name, c.cid_key) for c in res.children] == [
        ("Module", "util_add", "util.h.util_add")]


def test_function_definition_child_has_definition_c_kind():
    """`world_graph._resolve_nearest_keyed` が定義を宣言より優先するための材料（§9）。"""
    res = A.collect_defs("int util_add(int a, int b) {\n    return a + b;\n}\n", "util.c")
    assert res.children[0].extra["c_kind"] == "definition"


def test_prototype_declaration_child_has_declaration_c_kind():
    res = A.collect_defs("int util_add(int a, int b);\n", "util.h")
    assert res.children[0].extra["c_kind"] == "declaration"


def test_c_source_external_prototype_is_not_a_child():
    """`.c` の `;` 終端は外部プロトタイプ宣言であり、この自ファイルの定義ではない——children に
    しない（自ファイルの偽 child が呼び出し解決を吸ってしまうのを防ぐ）。"""
    text = "extern int target(int x);\n\nint f(void) {\n    return target(1);\n}\n"
    res = A.collect_defs(text, "caller.c")
    assert [c.name for c in res.children] == ["f"]


def test_c_source_external_prototype_is_still_excluded_from_call_detection():
    """children にはしないが、プロトタイプ自体の `name(` は呼び出しとしても誤検出しない。"""
    text = "extern int target(int x);\n\nint f(void) {\n    return target(1);\n}\n"
    res = A.extract_refs(text, "caller.c")
    call_refs = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in call_refs] == ["target"]


def test_multiple_functions_in_one_file_all_become_children():
    text = (
        "int f1(int a) {\n    return a;\n}\n\n"
        "void f2(void) {\n}\n\n"
        "static int f3(void) {\n    return 1;\n}\n"
    )
    res = A.collect_defs(text, "multi.c")
    assert [c.name for c in res.children] == ["f1", "f2", "f3"]


def test_c_and_h_same_stem_coexist_as_distinct_primaries():
    """`.c`/`.h` 同居（RV2-1・拡張子込み primary 名による同一 stem の disambiguation）。"""
    c_res = A.collect_defs("void thing_run(void) {\n}\n", "pair/thing.c")
    h_res = A.collect_defs("void thing_run(void);\n", "pair/thing.h")
    assert c_res.primary.name == "thing.c"
    assert h_res.primary.name == "thing.h"
    assert c_res.primary.name != h_res.primary.name


# --- 制御構文キーワード・マクロ関数定義は関数定義として誤認しない ---

def test_control_keywords_are_not_mistaken_for_function_return_type():
    text = (
        "int f(int x) {\n"
        "    if (x > 0) {\n"
        "        return x;\n"
        "    }\n"
        "    return 0;\n"
        "}\n"
    )
    res = A.collect_defs(text, "f.c")
    assert [c.name for c in res.children] == ["f"]


def test_macro_function_definition_is_not_a_child():
    text = "#define MAX(a, b) ((a) > (b) ? (a) : (b))\n\nint f(void) {\n    return MAX(1, 2);\n}\n"
    res = A.collect_defs(text, "f.c")
    assert [c.name for c in res.children] == ["f"]


# --- コメント/文字列リテラル内は無視する ---

def test_function_signature_inside_comment_is_ignored():
    text = "// int fake(int a);\nint real(int a) {\n    return a;\n}\n"
    res = A.collect_defs(text, "real.c")
    assert [c.name for c in res.children] == ["real"]


def test_function_signature_inside_block_comment_is_ignored():
    text = "/* int fake(int a); */\nint real(int a) {\n    return a;\n}\n"
    res = A.collect_defs(text, "real.c")
    assert [c.name for c in res.children] == ["real"]


# --- #include（ローカルのみ・`<...>` は無視）---

def test_extract_refs_local_include_without_path_separator():
    res = A.extract_refs('#include "util.h"\n', "main.c")
    assert len(res.refs) == 1
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "util.h")
    assert ref.extra == {"via": "include", "include_path": "util.h"}


def test_extract_refs_local_include_with_relative_path_keeps_original_string():
    res = A.extract_refs('#include "../inc/log.h"\n', "src/main.c")
    ref = res.refs[0]
    assert ref.name == "log.h"                                # basename のみ（拡張子込み）
    assert ref.extra["include_path"] == "../inc/log.h"         # パス解決は world_graph 側（§12）


def test_extract_refs_system_include_is_ignored():
    res = A.extract_refs("#include <stdio.h>\n", "main.c")
    assert res.refs == [] and res.dropped == []


def test_extract_refs_include_inside_comment_is_ignored():
    res = A.extract_refs('// #include "fake.h"\n#include "real.h"\n', "main.c")
    assert [r.name for r in res.refs] == ["real.h"]


# --- 関数呼び出し（`via=call`）---

def test_extract_refs_function_call_via_call():
    res = A.extract_refs("int main(void) {\n    return util_add(1, 2);\n}\n", "main.c")
    call_refs = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in call_refs] == ["util_add"]


def test_control_keywords_are_not_treated_as_calls():
    text = (
        "int f(int x) {\n"
        "    if (x > 0) {\n"
        "        return sizeof(x);\n"
        "    }\n"
        "    while (x) {\n"
        "        x--;\n"
        "    }\n"
        "    for (;;) {\n"
        "        break;\n"
        "    }\n"
        "    switch (x) {\n"
        "        default: break;\n"
        "    }\n"
        "    return 0;\n"
        "}\n"
    )
    res = A.extract_refs(text, "f.c")
    call_refs = [r for r in res.refs if r.extra.get("via") == "call"]
    assert call_refs == []


def test_function_definition_and_prototype_header_are_not_double_counted_as_calls():
    """自身の定義/プロトタイプの `name(` はそれ自体を呼び出しとして誤検出しない。"""
    res = A.extract_refs("int util_add(int a, int b) {\n    return a + b;\n}\n", "util.c")
    assert res.refs == []


# --- Dropped（関数ポインタ経由の呼び出し・マクロ関数呼び出し）---

def test_function_pointer_dereference_call_is_dropped_as_dynamic_call():
    res = A.extract_refs("int f(void) {\n    (*cb)(1);\n    return 0;\n}\n", "f.c")
    assert [d.reason for d in res.dropped] == ["c_dynamic_call"]
    assert res.refs == []


def test_function_pointer_variable_declaration_is_not_flagged_at_all():
    """`int (*fp)(int);` は宣言であり呼び出しではない——検出限界として黙って見逃す（docstring 参照）。"""
    res = A.extract_refs("int (*fp)(int);\n\nint f(void) {\n    return 0;\n}\n", "f.c")
    assert res.dropped == [] and res.refs == []


def test_call_to_function_like_macro_is_dropped_as_macro_call():
    text = (
        "#define MAX(a, b) ((a) > (b) ? (a) : (b))\n\n"
        "int f(int x, int y) {\n"
        "    return MAX(x, y);\n"
        "}\n"
    )
    res = A.extract_refs(text, "f.c")
    assert [d.reason for d in res.dropped] == ["c_macro_call"]
    assert res.refs == []


# --- K&R 形式（旧式）関数定義ヘッダは通常呼び出しから除外し Dropped で申告する ---

def test_knr_style_function_definition_header_is_dropped_not_a_call():
    text = (
        "int f(a, b)\n"
        "    int a;\n"
        "    int b;\n"
        "{\n"
        "    return a + b;\n"
        "}\n"
    )
    res = A.extract_refs(text, "f.c")
    call_refs = [r for r in res.refs if r.extra.get("via") == "call"]
    assert call_refs == []
    assert [d.reason for d in res.dropped] == ["c_knr_definition"]


# --- `return (*fp)(x)`/`x = (*fp)(1)` は宣言と誤認せず c_dynamic_call として申告する ---

def test_dynamic_call_via_return_statement_is_dropped_not_silently_ignored():
    """`return` は識別子1語のため単純な前置部ヒューリスティックでは宣言と誤認されていた——
    前置部が制御構文キーワードなら宣言とはみなさないよう是正する。"""
    res = A.extract_refs("int f(void) {\n    return (*fp)(1);\n}\n", "f.c")
    assert [d.reason for d in res.dropped] == ["c_dynamic_call"]
    assert res.refs == []


def test_dynamic_call_via_assignment_is_dropped():
    res = A.extract_refs("int f(void) {\n    int x;\n    x = (*fp)(1);\n}\n", "f.c")
    assert [d.reason for d in res.dropped] == ["c_dynamic_call"]
    assert res.refs == []


# --- Windows 区切りの include パス（`\`）は `/` に正規化してから basename/include_path を組み立てる ---

def test_windows_style_local_include_path_is_normalized_to_forward_slashes():
    res = A.extract_refs('#include "..\\inc\\log.h"\n', "src/main.c")
    ref = res.refs[0]
    assert ref.name == "log.h"
    assert ref.extra["include_path"] == "../inc/log.h"


# --- `.h` の C++ 専用構文は primary はそのまま・cxx_header として Dropped で申告する ---

def test_cxx_only_header_syntax_is_reported_as_dropped_cxx_header():
    text = "namespace N {\n    class Widget {};\n}\n"
    res = A.collect_defs(text, "Widget.h")
    assert [d.reason for d in res.dropped] == ["cxx_header"]
    assert res.primary is not None and res.primary.name == "Widget.h"


def test_cxx_header_detection_is_scoped_to_h_files_only():
    text = "namespace N {\n    class Widget {};\n}\n"
    res = A.collect_defs(text, "Widget.c")
    assert res.dropped == []


# --- 拡張子の大文字小文字は区別しない（`registry._ext` が小文字化して既にルーティングしている
# ため、本体側のヘッダ判定もそれに合わせる）---

def test_uppercase_h_extension_is_treated_as_header_for_children():
    res = A.collect_defs("void upper_proto(void);\n", "UTIL.H")
    assert [(c.label, c.name, c.cid_key) for c in res.children] == [
        ("Module", "upper_proto", "UTIL.H.upper_proto")]


def test_uppercase_h_extension_is_treated_as_header_for_cxx_detection():
    text = "namespace N {\n    class Widget {};\n}\n"
    res = A.collect_defs(text, "Widget.H")
    assert [d.reason for d in res.dropped] == ["cxx_header"]
