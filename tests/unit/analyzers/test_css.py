"""`CssAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A）。"""
from __future__ import annotations

import pytest

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
    assert res.primary is not None and res.primary.label == "Module" and res.primary.name == "style.css"
    assert res.children == []


# (入力, path, [(name, include_path)], Dropped[(reason, line|None)])
CASES = {
    "import_url_double_quotes": ('@import url("base.css");\n', "style.css", [("base.css", "base.css")], []),
    "import_url_without_quotes": ("@import url(base.css);\n", "style.css", [("base.css", "base.css")], []),
    "import_bare_string": ('@import "base.css";\n', "style.css", [("base.css", "base.css")], []),
    "import_single_quotes": ("@import url('base.css');\n", "style.css", [("base.css", "base.css")], []),
    "relative_path_preserved": ('@import url("../common/base.css");\n', "sub/style.css",
                                [("base.css", "../common/base.css")], []),
    "windows_separator_normalized": (r'@import url("common\base.css");' + "\n", "style.css",
                                     [("base.css", "common/base.css")], []),
    "absolute_import_relative_to_referrer": ('@import url("/static/base.css");\n', "gen1/static/style.css",
                                             [("base.css", "base.css")], []),
    "query_string_stripped": ('@import url("base.css?v=1");\n', "style.css", [("base.css", "base.css")], []),
    "selectors_and_property_values_not_extracted": ('.icon { background: url("icon.png"); }\n', "style.css", [], []),
    "block_comment_not_scanned": ('/* @import url("commented.css"); */\n', "style.css", [], []),
    "external_import_dropped": ('@import url("https://cdn.example.com/base.css");\n', "style.css", [],
                                [("web_external_ref", 1)]),
    "protocol_relative_import_dropped": ('@import url("//cdn.example.com/base.css");\n', "style.css", [],
                                         [("web_external_ref", None)]),
}


@pytest.mark.parametrize("text,path,refs,dropped", CASES.values(), ids=CASES)
def test_extract_refs(text, path, refs, dropped):
    res = A.extract_refs(text, path)
    assert [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs] == [
        ("INVOKES", "Module", n, {"via": "include", "include_path": p}) for n, p in refs]
    assert len(res.dropped) == len(dropped)
    for d, (reason, line) in zip(res.dropped, dropped):
        assert d.reason == reason and (line is None or d.line == line)
