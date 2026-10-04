"""`CAnalyzer` の単体テスト（アナライザ拡張 §4(a)/§9 S6・A1）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.c import CAnalyzer

A = CAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".c", ".h"})
    assert A.name == "c"
    assert A.doctype == "c"


def test_accepts_all_c_files_without_content_inspection():
    assert CAnalyzer.accepts is Analyzer.accepts


# ---- primary（ファイル自体・拡張子込みファイル名）----

@pytest.mark.parametrize("text,path,name", [
    ("int f(void) {\n    return 0;\n}\n", "src/foo.c", "foo.c"),
    ("int f(void);\n", "src/foo.h", "foo.h"),                                # 宣言のみの .h も主体を持つ
    ("#ifndef FOO_H\n#define FOO_H\n#endif\n", "foo.h", "foo.h"),
])
def test_collect_defs_primary_is_extension_included_filename(text, path, name):
    res = A.collect_defs(text, path)
    assert res.primary is not None and res.primary.label == "Module" and res.primary.name == name


def test_c_and_h_same_stem_coexist_as_distinct_primaries():
    assert A.collect_defs("void thing_run(void) {\n}\n", "pair/thing.c").primary.name == "thing.c"
    assert A.collect_defs("void thing_run(void);\n", "pair/thing.h").primary.name == "thing.h"


# ---- children（関数定義／プロトタイプ・修飾名 `<ファイル名>.<関数名>`）----
# (入力, path, [(name, cid_key|None, c_kind|None)])
CHILD_CASES = {
    "definition": ("int util_add(int a, int b) {\n    return a + b;\n}\n", "util.c",
                   [("util_add", "util.c.util_add", "definition")]),
    "prototype_in_header": ("int util_add(int a, int b);\n", "util.h", [("util_add", "util.h.util_add", "declaration")]),
    "c_source_external_prototype_not_child": (
        "extern int target(int x);\n\nint f(void) {\n    return target(1);\n}\n", "caller.c", [("f", None, None)]),
    "multiple_functions": (
        "int f1(int a) {\n    return a;\n}\n\nvoid f2(void) {\n}\n\nstatic int f3(void) {\n    return 1;\n}\n",
        "multi.c", [("f1", None, None), ("f2", None, None), ("f3", None, None)]),
    "control_keywords_not_return_type": (
        "int f(int x) {\n    if (x > 0) {\n        return x;\n    }\n    return 0;\n}\n", "f.c", [("f", None, None)]),
    "macro_function_definition_not_child": (
        "#define MAX(a, b) ((a) > (b) ? (a) : (b))\n\nint f(void) {\n    return MAX(1, 2);\n}\n", "f.c",
        [("f", None, None)]),
    "signature_in_line_comment_ignored": (
        "// int fake(int a);\nint real(int a) {\n    return a;\n}\n", "real.c", [("real", None, None)]),
    "signature_in_block_comment_ignored": (
        "/* int fake(int a); */\nint real(int a) {\n    return a;\n}\n", "real.c", [("real", None, None)]),
    "allman_style_and_multiline_signature": (
        "static int\nmulti(int a,\n      int b)\n{\n    return a;\n}\n", "m.c", [("multi", "m.c.multi", "definition")]),
    "knr_definition_is_a_definition": (
        "int f(a, b)\n    int a;\n    int b;\n{\n    return a + b;\n}\n", "f.c", [("f", "f.c.f", "definition")]),
    "function_pointer_parameter_and_extern_c_block": (
        '#ifdef __cplusplus\nextern "C" {\n#endif\nvoid reg(void (*cb)(int));\n#ifdef __cplusplus\n}\n#endif\n',
        "reg.h", [("reg", "reg.h.reg", "declaration")]),
    "function_pointer_variable_and_typedef_not_child": ("int (*fp)(int);\ntypedef void cbfn(int);\n", "t.h", []),
    "uppercase_h_extension_is_header": ("void upper_proto(void);\n", "UTIL.H",
                                        [("upper_proto", "UTIL.H.upper_proto", "declaration")]),
}


@pytest.mark.parametrize("text,path,children", CHILD_CASES.values(), ids=CHILD_CASES)
def test_collect_defs_children(text, path, children):
    res = A.collect_defs(text, path)
    assert [(c.label, c.name) for c in res.children] == [("Module", n) for n, _k, _c in children]
    for c, (_n, cid, c_kind) in zip(res.children, children):
        assert cid is None or c.cid_key == cid
        assert c_kind is None or c.extra["c_kind"] == c_kind        # 定義を宣言より優先する材料


def test_empty_header_has_no_children():
    assert A.collect_defs("#ifndef FOO_H\n#define FOO_H\n#endif\n", "foo.h").children == []


# ---- extract_refs ----
# (入力, path, [(name, via, extra 部分)], Dropped の reason 列)
CTRL = ("int f(int x) {\n    if (x > 0) {\n        return sizeof(x);\n    }\n    while (x) {\n        x--;\n    }\n"
        "    for (;;) {\n        break;\n    }\n    switch (x) {\n        default: break;\n    }\n    return 0;\n}\n")
INC = {"via": "include"}
REFS_CASES = {
    "local_include": ('#include "util.h"\n', "main.c", [("util.h", {"via": "include", "include_path": "util.h"})], []),
    "local_include_relative_path": ('#include "../inc/log.h"\n', "src/main.c",
                                    [("log.h", {"via": "include", "include_path": "../inc/log.h"})], []),
    "windows_include_path_normalized": ('#include "..\\inc\\log.h"\n', "src/main.c",
                                        [("log.h", {"via": "include", "include_path": "../inc/log.h"})], []),
    "system_include_ignored": ("#include <stdio.h>\n", "main.c", [], []),
    "include_in_comment_ignored": ('// #include "fake.h"\n#include "real.h"\n', "main.c", [("real.h", INC)], []),
    "function_call": ("int main(void) {\n    return util_add(1, 2);\n}\n", "main.c", [("util_add", {"via": "call"})], []),
    "control_keywords_not_calls": (CTRL, "f.c", [], []),
    "definition_header_not_a_call": ("int util_add(int a, int b) {\n    return a + b;\n}\n", "util.c", [], []),
    "external_prototype_excluded_but_call_detected": (
        "extern int target(int x);\n\nint f(void) {\n    return target(1);\n}\n", "caller.c",
        [("target", {"via": "call"})], []),
    "function_pointer_dereference_dropped": ("int f(void) {\n    (*cb)(1);\n    return 0;\n}\n", "f.c", [],
                                             ["c_dynamic_call"]),
    "function_pointer_declaration_not_flagged": ("int (*fp)(int);\n\nint f(void) {\n    return 0;\n}\n", "f.c", [], []),
    "function_like_macro_call_dropped": (
        "#define MAX(a, b) ((a) > (b) ? (a) : (b))\n\nint f(int x, int y) {\n    return MAX(x, y);\n}\n", "f.c",
        [], ["c_macro_call"]),
    "knr_definition_header_is_not_a_call": (
        "int f(a, b)\n    int a;\n    int b;\n{\n    return a + b;\n}\n", "f.c", [], []),
    "member_and_subscript_calls_dropped": (
        "int f(void) {\n    s->cb(1);\n    tbl[0](2);\n    return 0;\n}\n", "f.c", [],
        ["c_dynamic_call", "c_dynamic_call"]),
    "call_in_preprocessor_condition_ignored": ("#if CHECK(1)\nint x;\n#endif\n", "f.c", [], []),
    "dynamic_call_via_return": ("int f(void) {\n    return (*fp)(1);\n}\n", "f.c", [], ["c_dynamic_call"]),
    "dynamic_call_via_assignment": ("int f(void) {\n    int x;\n    x = (*fp)(1);\n}\n", "f.c", [], ["c_dynamic_call"]),
}


@pytest.mark.parametrize("text,path,refs,dropped", REFS_CASES.values(), ids=REFS_CASES)
def test_extract_refs(text, path, refs, dropped):
    res = A.extract_refs(text, path)
    assert [r.name for r in res.refs] == [n for n, _x in refs]
    for r, (_n, extra) in zip(res.refs, refs):
        assert all(r.extra.get(k) == v for k, v in extra.items())
        assert (r.edge_type, r.kind) == ("INVOKES", "Module")
    assert [d.reason for d in res.dropped] == dropped


# ---- `.h` の C++ 専用構文は cxx_header として Dropped（拡張子の大文字小文字は区別しない）----

@pytest.mark.parametrize("path,dropped", [("Widget.h", ["cxx_header"]), ("Widget.H", ["cxx_header"]), ("Widget.c", [])])
def test_cxx_only_header_syntax_dropped_only_for_h_files(path, dropped):
    res = A.collect_defs("namespace N {\n    class Widget {};\n}\n", path)
    assert [d.reason for d in res.dropped] == dropped
    assert res.primary is not None and res.primary.name == path


# ---- Tree-sitter の木で読むようになった形 ----
def test_calls_inside_a_definition_belong_to_it_for_allman_and_kr_styles():
    text = "int f(a)\n    int a;\n{\n    return g(a);\n}\nstatic int\nh(int x)\n{\n    return k(x);\n}\n"
    got = [(r.name, r.source_symbol_id) for r in A.extract_refs(text, "d/x.c").refs]
    assert got == [("g", ("d/x.c", "x.c.f")), ("k", ("d/x.c", "x.c.h"))]


def test_extern_c_guard_is_not_a_syntax_error_and_prototypes_inside_are_children():
    text = '#ifdef __cplusplus\nextern "C" {\n#endif\nint proto(int x);\n#ifdef __cplusplus\n}\n#endif\n'
    res = A.collect_defs(text, "p.h")
    assert [c.name for c in res.children] == ["proto"] and res.dropped == []


def test_syntax_error_is_reported_and_the_rest_of_the_file_is_still_read():
    text = "int before(void) {\n    return one();\n}\n\nint bad(int a {\n    return 0;\n}\n\nint after(void) {\n    return two();\n}\n"
    defs = A.collect_defs(text, "e.c")
    assert [d.reason for d in defs.dropped] == ["syntax_error"]
    assert {"before", "after"} <= {c.name for c in defs.children}
    refs = A.extract_refs(text, "e.c").refs
    assert [(r.name, r.source_symbol_id) for r in refs] == [("one", ("e.c", "e.c.before")), ("two", ("e.c", "e.c.after"))]


def test_export_macros_before_a_declaration_do_not_hide_the_definition():
    text = ("#define API\ntypedef int myint;\nAPI int f(void);\nEXPORT_X void g(int a);\nAPI_Y myint h(void);\n"
            "API void k(void) {\n    return;\n}\n")
    res = A.collect_defs(text, "m.h")
    assert [c.name for c in res.children] == ["f", "g", "h", "k"] and res.dropped == []
    assert [c.line for c in res.children] == [3, 4, 5, 6]


def test_macro_noise_blanking_is_limited_to_the_declaration_head_after_the_define():
    # `#define` より前の同名の定義・識別子は消さない
    text = "int API(void) {\n    return 1;\n}\n#define API\nAPI void g(void);\n"
    assert [c.name for c in A.collect_defs(text, "m.h").children] == ["API", "g"]
    enum = "enum { API, B };\n#define API\nint x = API;\n"
    assert A.collect_defs(enum, "e.c").dropped == []
    cont = "#define M(x) \\\n    EXPORT int x(void);\\\n    ok\nint f(void);\n"
    assert [c.name for c in A.collect_defs(cont, "c.h").children] == ["f"]


def test_only_the_extern_c_idiom_suppresses_missing_endif():
    idiom = '#ifdef __cplusplus\nextern "C" {\n#endif\nint a(void);\n#ifdef __cplusplus\n}\n#endif\n'
    assert A.collect_defs(idiom, "i.h").dropped == []
    unclosed = "#ifdef FOO\nint a(void);\n"
    assert [d.snippet for d in A.collect_defs(unclosed, "u.h").dropped] == ["missing #endif"]


def test_cxx_qualified_method_is_not_a_c_function_and_reports_cxx_header():
    res = A.collect_defs("inline void Widget::run(int x) {\n    go();\n}\n", "w.h")
    assert res.children == [] and "cxx_header" in [d.reason for d in res.dropped]


def test_bare_macro_call_without_semicolon_does_not_swallow_the_next_declaration():
    text = "int z;\nDECLARE_X(Foo)\nint f(void);\nint g(void) {\n    return 0;\n}\n"
    res = A.collect_defs(text, "m.h")
    assert [(c.name, c.line) for c in res.children] == [("f", 3), ("g", 4)] and res.dropped == []
