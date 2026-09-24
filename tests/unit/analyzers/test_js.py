"""`JsAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A・docs/proposals/2026-09-05-アナライザ拡張.md
§13）。"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.js import JsAnalyzer

A = JsAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".js", ".mjs"})
    assert A.name == "js"
    assert A.doctype == "js"


def test_ts_extension_is_not_claimed():
    assert ".ts" not in A.extensions


def test_accepts_all_js_files_without_content_inspection():
    assert JsAnalyzer.accepts is Analyzer.accepts


def test_collect_defs_primary_is_extension_included_filename_no_children():
    res = A.collect_defs("console.log('hi');", "static/app.js")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "app.js"
    assert res.children == []


# --- import/require/importScripts（INVOKES via=include） ---

def test_import_from_relative_path_with_extension():
    res = A.extract_refs('import { x } from "./util.js";\n', "app.js")
    assert [(r.name, r.extra) for r in res.refs] == [
        ("util.js", {"via": "include", "include_path": "./util.js"})]


def test_import_without_extension_gets_js_appended():
    res = A.extract_refs('import x from "./util";\n', "app.js")
    assert res.refs[0].name == "util.js"
    assert res.refs[0].extra["include_path"] == "./util.js"


def test_bare_package_import_is_not_a_local_include():
    res = A.extract_refs('import React from "react";\n', "app.js")
    assert res.refs == []


def test_require_relative_path():
    res = A.extract_refs('const x = require("./util");\n', "app.js")
    assert res.refs[0].name == "util.js"


def test_import_scripts_relative_path():
    res = A.extract_refs('importScripts("helpers.js");\n', "app.js")
    assert res.refs[0].name == "helpers.js"


def test_import_scripts_absolute_url_is_excluded():
    res = A.extract_refs('importScripts("https://cdn.example.com/lib.js");\n', "app.js")
    assert res.refs == []


# --- URL 文字列リテラル（ACCESSES via=config_key） ---

def test_fetch_static_path_is_config_key():
    res = A.extract_refs('fetch("/api/orders");\n', "app.js")
    assert [(r.edge_type, r.kind, r.name, r.extra["via"]) for r in res.refs] == [
        ("ACCESSES", "Config", "/api/orders", "config_key")]
    assert res.refs[0].extra["key_kind"] == "url"


def test_axios_get_static_path_is_config_key():
    res = A.extract_refs('axios.get("/api/orders");\n', "app.js")
    assert res.refs[0].name == "/api/orders"


def test_ajax_url_key_is_config_key():
    res = A.extract_refs('$.ajax({ url: "/api/orders", method: "GET" });\n', "app.js")
    assert res.refs[0].name == "/api/orders"


def test_xhr_open_static_path_is_config_key():
    res = A.extract_refs('xhr.open("GET", "/api/orders");\n', "app.js")
    assert res.refs[0].name == "/api/orders"


def test_location_href_static_path_is_config_key():
    res = A.extract_refs('location.href = "/orders/list";\n', "app.js")
    assert res.refs[0].name == "/orders/list"


def test_form_action_dot_action_extension_is_struts_key():
    res = A.extract_refs('form.action = "login.action";\n', "app.js")
    assert res.refs[0].name == "login"
    assert res.refs[0].extra["key_kind"] == "action"


def test_dynamic_url_concatenation_is_dropped_not_a_ref():
    res = A.extract_refs('fetch("/api/" + id);\n', "app.js")
    assert res.refs == []
    assert [(d.reason, d.line) for d in res.dropped] == [("js_dynamic_url", 1)]


def test_static_call_on_same_line_as_dynamic_concatenation_still_becomes_a_ref():
    """動的連結の除外は match span 単位——同じ行の他の静的呼び出しまで巻き込まない
    （行単位の除外だと `/ok` のような同一行の静的参照まで一緒に消えてしまう）。"""
    res = A.extract_refs('fetch("/ok"); fetch("/api/" + id);\n', "app.js")
    assert [r.name for r in res.refs] == ["/ok"]
    assert [(d.reason, d.line) for d in res.dropped] == [("js_dynamic_url", 1)]


def test_multiple_dynamic_and_static_calls_interleaved_are_matched_correctly():
    """動的連結／静的呼び出しの単調ポインタ突合は、複数件が入り交じる順序
    （静的→動的→静的→動的）でも取りこぼし・誤除外なく判定する（二次時間対策の副作用で
    判定順序に依存するバグを作っていないことの固定）。"""
    text = (
        'fetch("/a");\n'
        'fetch("/b/" + x);\n'
        'fetch("/c");\n'
        'fetch("/d/" + y);\n'
    )
    res = A.extract_refs(text, "app.js")
    assert [r.name for r in res.refs] == ["/a", "/c"]
    assert [(d.reason, d.line) for d in res.dropped] == [
        ("js_dynamic_url", 2), ("js_dynamic_url", 4)]


# --- 外部参照スキーム（include 対象外・URL キーにもしない） ---

def test_import_scripts_data_uri_is_dropped():
    res = A.extract_refs('importScripts("data:text/javascript,void(0)");\n', "app.js")
    assert res.refs == []
    assert any(d.reason == "web_external_ref" for d in res.dropped)


def test_protocol_relative_url_in_fetch_is_not_a_config_key():
    """`//cdn.example.com/x` は `/` で始まるが外部参照（protocol-relative URL）——URL キーにしない。"""
    res = A.extract_refs('fetch("//cdn.example.com/x");\n', "app.js")
    assert res.refs == []


def test_absolute_import_scripts_path_is_converted_to_referrer_relative_path():
    res = A.extract_refs('importScripts("/static/helpers.js");\n', "gen1/static/worker.js")
    assert res.refs[0].name == "helpers.js"
    assert res.refs[0].extra["include_path"] == "helpers.js"


# --- URL 正規化（`?`/`#` 以降を判定/basename取得の前に除去） ---

def test_fetch_url_strips_query_and_fragment():
    res = A.extract_refs('fetch("/api/orders?q=1#x");\n', "app.js")
    assert res.refs[0].name == "/api/orders"


def test_form_action_with_query_string_still_strips_correctly():
    res = A.extract_refs('form.action = "login.action?next=/";\n', "app.js")
    assert res.refs[0].name == "login"


# --- コメント（内容は読み飛ばす・文字列内容は保持） ---

def test_line_comment_contents_are_not_scanned():
    res = A.extract_refs('// fetch("/api/orders");\n', "app.js")
    assert res.refs == []


def test_block_comment_contents_are_not_scanned():
    res = A.extract_refs('/* fetch("/api/orders"); */\n', "app.js")
    assert res.refs == []


def test_url_inside_string_survives_comment_sanitization():
    """コメント除去は文字列の中身を壊さない（`_sanitize_comments_only` は文字列を温存する）。"""
    res = A.extract_refs('// note\nfetch("/api/orders");\n', "app.js")
    assert res.refs[0].name == "/api/orders"


# --- minified（Dropped・参照抽出なし） ---

def test_min_js_filename_is_dropped_as_minified():
    res = A.extract_refs('fetch("/api/orders");', "app.min.js")
    assert res.refs == []
    assert [(d.reason, d.line) for d in res.dropped] == [("js_minified", 1)]


def test_long_single_line_is_dropped_as_minified():
    long_line = "var x=1;" * 1000
    res = A.extract_refs(long_line, "bundle.js")
    assert res.refs == []
    assert [(d.reason, d.line) for d in res.dropped] == [("js_minified", 1)]
