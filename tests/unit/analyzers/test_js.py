"""`JsAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A・入力ソース片 → 参照・Dropped の表）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.js import JsAnalyzer

A = JsAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".js", ".mjs"})
    assert A.name == "js"
    assert A.doctype == "js"
    assert ".ts" not in A.extensions


def test_accepts_all_js_files_without_content_inspection():
    assert JsAnalyzer.accepts is Analyzer.accepts


def test_collect_defs_primary_is_extension_included_filename_no_children():
    res = A.collect_defs("console.log('hi');", "static/app.js")
    assert res.primary is not None and res.primary.label == "Module" and res.primary.name == "app.js"
    assert res.children == []


def Inc(name, path):
    return ("INVOKES", "Module", name, {"via": "include", "include_path": path})


def Key(name, key_kind="url"):
    return ("ACCESSES", "Config", name, {"via": "config_key", "key_kind": key_kind})


EXT = "web_external_ref"
DYN = "js_dynamic_url"
# (入力, path, 参照[(edge, kind, name, extra 部分|None)], Dropped[(reason, line|None)]|None)
CASES = {
    # import/require/importScripts（INVOKES via=include）
    "import_from_relative_with_extension": ('import { x } from "./util.js";\n', "app.js", [Inc("util.js", "./util.js")], None),
    "import_without_extension_gets_js": ('import x from "./util";\n', "app.js", [Inc("util.js", "./util.js")], None),
    "bare_package_import_not_local": ('import React from "react";\n', "app.js", [], None),
    "require_relative": ('const x = require("./util");\n', "app.js", [("INVOKES", "Module", "util.js", None)], None),
    "import_scripts_relative": ('importScripts("helpers.js");\n', "app.js", [("INVOKES", "Module", "helpers.js", None)], None),
    "import_scripts_absolute_url_excluded": ('importScripts("https://cdn.example.com/lib.js");\n', "app.js", [], [(EXT, None)]),
    "import_scripts_data_uri_dropped": ('importScripts("data:text/javascript,void(0)");\n', "app.js", [], [(EXT, None)]),
    "absolute_import_scripts_relative_to_referrer": ('importScripts("/static/helpers.js");\n', "gen1/static/worker.js",
                                                     [Inc("helpers.js", "helpers.js")], None),
    # URL 文字列リテラル（ACCESSES via=config_key）
    "fetch_static_path": ('fetch("/api/orders");\n', "app.js", [Key("/api/orders")], None),
    "axios_get_static_path": ('axios.get("/api/orders");\n', "app.js", [Key("/api/orders")], None),
    "ajax_url_key": ('$.ajax({ url: "/api/orders", method: "GET" });\n', "app.js", [Key("/api/orders")], None),
    "xhr_open_static_path": ('xhr.open("GET", "/api/orders");\n', "app.js", [Key("/api/orders")], None),
    "location_href_static_path": ('location.href = "/orders/list";\n', "app.js", [Key("/orders/list")], None),
    "form_action_dot_action_is_struts_key": ('form.action = "login.action";\n', "app.js", [Key("login", "action")], None),
    "fetch_url_strips_query_and_fragment": ('fetch("/api/orders?q=1#x");\n', "app.js", [Key("/api/orders")], None),
    "form_action_with_query_strips": ('form.action = "login.action?next=/";\n', "app.js", [Key("login", "action")], None),
    "protocol_relative_url_not_a_key": ('fetch("//cdn.example.com/x");\n', "app.js", [], None),
    # 動的連結は参照にせず Dropped（match span 単位で除外・同一行の静的呼び出しは残る）
    "dynamic_concatenation_dropped": ('fetch("/api/" + id);\n', "app.js", [], [(DYN, 1)]),
    "static_on_same_line_as_dynamic_still_ref": ('fetch("/ok"); fetch("/api/" + id);\n', "app.js", [Key("/ok")], [(DYN, 1)]),
    "interleaved_static_dynamic_static_dynamic": (
        'fetch("/a");\nfetch("/b/" + x);\nfetch("/c");\nfetch("/d/" + y);\n', "app.js", [Key("/a"), Key("/c")],
        [(DYN, 2), (DYN, 4)]),
    # コメント
    "line_comment_not_scanned": ('// fetch("/api/orders");\n', "app.js", [], None),
    "block_comment_not_scanned": ('/* fetch("/api/orders"); */\n', "app.js", [], None),
    "url_in_string_survives_comment_removal": ('// note\nfetch("/api/orders");\n', "app.js", [Key("/api/orders")], None),
    # 文字列の中のコードの断片は読まない（Dropped で申告）。API の引数の位置の文字列は読む
    "code_in_description_string_not_scanned": (
        'var msg = "import x from \'./b.js\'; fetch(\'/api/z\')";\n', "app.js", [], [("js_string_code", 1)]),
    "code_in_template_string_not_scanned": ('var t = `require("./b.js")`;\n', "app.js", [], [("js_string_code", 1)]),
    "api_arguments_kept_beside_description_string": (
        'var msg = "import x from \'./b.js\'; fetch(\'/api/z\')";\nconst b = require("./b.js");\n'
        'import("./c.js");\nfetch("/api/z");\n', "app.js",
        [Inc("c.js", "./c.js"), Inc("b.js", "./b.js"), Key("/api/z")], [("js_string_code", 1)]),
    "dynamic_import_with_comment_before_arg": ('import(/* chunk */ "./c.js");\n', "app.js", [Inc("c.js", "./c.js")], None),
    "template_expression_code_is_scanned": (
        'var t = `x ${require("./b.js")} ${`n ${fetch("/api/q")}`} import z from "./no.js"`;\n', "app.js",
        [Inc("b.js", "./b.js"), Key("/api/q")], [("js_string_code", 1)]),
    "api_argument_static_template_is_read": ('fetch(`/api/orders`);\n', "app.js", [Key("/api/orders")], None),
    "api_argument_static_template_import": ('import(`./b.js`);\n', "app.js", [Inc("b.js", "./b.js")], None),
    "api_argument_dynamic_template_reported": ('fetch(`/api/${id}`);\n', "app.js", [], [(DYN, 1)]),
    "xhr_open_variable_method_second_arg_is_url": ('xhr.open(method, "/api/orders");\n', "app.js", [Key("/api/orders")], None),
    # 関数名と括弧の間の空白・改行、複数行の import/export … from
    "require_space_before_paren": ('const x = require ("./util");\n', "app.js", [("INVOKES", "Module", "util.js", None)], None),
    "import_scripts_space_before_paren": ('importScripts ("helpers.js");\n', "app.js", [("INVOKES", "Module", "helpers.js", None)], None),
    "fetch_newline_before_paren": ('fetch\n("/api/orders");\n', "app.js", [Key("/api/orders")], None),
    "axios_spaced_call": ('axios . get ("/api/orders");\n', "app.js", [Key("/api/orders")], None),
    "multiline_named_import": ('import {x,\n  y} from "./b.js";\n', "app.js", [Inc("b.js", "./b.js")], None),
    "multiline_export_from": ('export {\n  a,\n  b\n} from "./b.js";\n', "app.js", [Inc("b.js", "./b.js")], None),
    "multiline_import_does_not_swallow_next_statement": (
        'import {x,\n  y} from "react";\nconst s = "./not-a-dep.js";\n', "app.js", [], None),
    # minified
    "min_js_filename_dropped": ('fetch("/api/orders");', "app.min.js", [], [("js_minified", 1)]),
    "long_single_line_dropped": ("var x=1;" * 1000, "bundle.js", [], [("js_minified", 1)]),
}


@pytest.mark.parametrize("text,path,refs,dropped", CASES.values(), ids=CASES)
def test_extract_refs(text, path, refs, dropped):
    res = A.extract_refs(text, path)
    assert [(r.edge_type, r.kind, r.name) for r in res.refs] == [e[:3] for e in refs]
    for r, e in zip(res.refs, refs):
        assert e[3] is None or all(r.extra.get(k) == v for k, v in e[3].items())
    expected = dropped or []  # None は「申告なし」の期待
    assert [(d.reason, d.line if line is not None else None) for d, (_r, line) in zip(res.dropped, expected)] == expected
    assert len(res.dropped) == len(expected)
