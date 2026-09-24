"""`CssAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A・docs/proposals/2026-09-05-アナライザ拡張.md
§13）。"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.css import CssAnalyzer

A = CssAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".css"})
    assert A.name == "css"
    assert A.doctype == "css"


def test_accepts_all_css_files_without_content_inspection():
    assert CssAnalyzer.accepts is Analyzer.accepts


def test_collect_defs_primary_is_extension_included_filename_no_children():
    res = A.collect_defs("body { color: red; }", "static/style.css")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "style.css"
    assert res.children == []


def test_import_url_function_with_double_quotes():
    res = A.extract_refs('@import url("base.css");\n', "style.css")
    assert [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs] == [
        ("INVOKES", "Module", "base.css", {"via": "include", "include_path": "base.css"})]


def test_import_url_function_without_quotes():
    res = A.extract_refs('@import url(base.css);\n', "style.css")
    assert res.refs[0].name == "base.css"


def test_import_bare_string_without_url_function():
    res = A.extract_refs('@import "base.css";\n', "style.css")
    assert res.refs[0].name == "base.css"


def test_import_single_quotes():
    res = A.extract_refs("@import url('base.css');\n", "style.css")
    assert res.refs[0].name == "base.css"


def test_relative_path_include_path_is_preserved():
    res = A.extract_refs('@import url("../common/base.css");\n', "sub/style.css")
    assert res.refs[0].name == "base.css"
    assert res.refs[0].extra["include_path"] == "../common/base.css"


def test_windows_style_separator_is_normalized():
    res = A.extract_refs(r'@import url("common\base.css");' + "\n", "style.css")
    assert res.refs[0].extra["include_path"] == "common/base.css"


def test_selectors_and_property_values_are_not_extracted():
    """`@import` 以外（セレクタ・`background: url(...)` 等）は対象外。"""
    res = A.extract_refs('.icon { background: url("icon.png"); }\n', "style.css")
    assert res.refs == []


def test_block_comment_contents_are_not_scanned():
    res = A.extract_refs('/* @import url("commented.css"); */\n', "style.css")
    assert res.refs == []


# --- 外部参照スキーム（include 対象外） ---

def test_external_import_is_dropped_not_included():
    res = A.extract_refs('@import url("https://cdn.example.com/base.css");\n', "style.css")
    assert res.refs == []
    assert [(d.reason, d.line) for d in res.dropped] == [("web_external_ref", 1)]


def test_protocol_relative_import_is_dropped_not_included():
    res = A.extract_refs('@import url("//cdn.example.com/base.css");\n', "style.css")
    assert res.refs == []
    assert any(d.reason == "web_external_ref" for d in res.dropped)


# --- `/` 始まりの include の scope 相対化 ---

def test_absolute_import_is_converted_to_referrer_relative_path():
    res = A.extract_refs('@import url("/static/base.css");\n', "gen1/static/style.css")
    assert res.refs[0].name == "base.css"
    assert res.refs[0].extra["include_path"] == "base.css"


# --- URL 正規化（`?` 以降を basename取得の前に除去） ---

def test_import_basename_strips_query_string():
    res = A.extract_refs('@import url("base.css?v=1");\n', "style.css")
    assert res.refs[0].name == "base.css"
    assert res.refs[0].extra["include_path"] == "base.css"
