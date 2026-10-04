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
    "import_scripts_absolute_url_excluded": ('importScripts("https://cdn.example.com/lib.js");\n', "app.js", [], None),
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
    if dropped is not None:
        assert [(d.reason, d.line if line is not None else None) for d, (_r, line) in zip(res.dropped, dropped)] == dropped
        assert len(res.dropped) == len(dropped)
