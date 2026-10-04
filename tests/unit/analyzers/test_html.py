"""`HtmlTemplateAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A）。"""
from __future__ import annotations

import time

import pytest

from sherpa.ingest.analyzers.html import HtmlTemplateAnalyzer

A = HtmlTemplateAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".html", ".htm", ".xhtml"})
    assert A.name == "html"
    assert A.doctype == "html"


def test_fallback_to_text_kind_when_declined_is_declared():
    assert A.fallback_to_text_kind_when_declined is True


# ---- content-aware accepts()（マーカーがあれば受理・無ければ資料へ）----
@pytest.mark.parametrize("text,accepted", [
    ("<html><body><form></form></body></html>", True),
    ('<script src="app.js"></script>', True),
    ('<link rel="stylesheet" href="a.css">', True),
    ('<jsp:include page="a.jsp"/>', True),
    ('<h:outputText value="hi"/>', True),
    ('<div th:if="loggedIn">Hi</div>', True),
    ("<span>${user.name}</span>", True),
    ("<% int x = 1; %>", True),
    ('<a href="/login.action">Login</a>', True),
    ("<html><body><p>ただの本文です。フォームもスクリプトもありません。</p></body></html>", False),
    ("<!-- <form></form> -->\n<p>ただの本文です。</p>", False),             # コメント内の目印は誤検出しない
], ids=["form", "script_src", "link_stylesheet", "jsp_tag", "jsf_h_tag", "thymeleaf", "el", "scriptlet",
        "action_link", "plain_declined", "marker_in_comment_declined"])
def test_accepts(text, accepted):
    assert A.accepts("x.html", text) is accepted


def test_collect_defs_primary_is_extension_included_filename():
    res = A.collect_defs("<html></html>", "static/index.html")
    assert res.primary is not None and res.primary.label == "Module" and res.primary.name == "index.html"
    assert res.children == []


def Inc(name, path=None):
    return ("INVOKES", "Module", name, {"via": "include", **({"include_path": path} if path else {})})


def Key(name, key_kind):
    return ("ACCESSES", "Config", name, {"via": "config_key", "key_kind": key_kind})


# (入力, path, 参照[(edge, kind, name, extra 部分|None)], Dropped[(reason, line|None)]|None)
CASES = {
    "script_src_and_link_href": ('<script src="app.js"></script>\n<link href="style.css">\n', "index.html",
                                 [Inc("app.js"), Inc("style.css")], None),
    "form_action_bare_name": ('<form action="login" method="post"></form>\n', "index.html", [Key("login", "action")], None),
    "url_key_absolute_path": ('<a href="/orders/list">Orders</a>\n', "index.html", [Key("/orders/list", "url")], None),
    "static_asset_href_not_key": ('<a href="/static/logo.png">logo</a>\n', "index.html", [], None),
    "html_comment_not_scanned": ('<!-- <form action="commented"></form> -->\n', "index.html", [], None),
    "script_without_src_no_dropped": ("<script>var x = 1;</script>\n", "index.html", [], []),
    "external_script_src_dropped": ('<script src="https://cdn.example.com/lib.js"></script>\n', "index.html", [],
                                    [("web_external_ref", 1)]),
    "absolute_include_relative_to_referrer": ('<link href="/static/style.css">\n', "gen1/WEB-INF/view/page.html",
                                              [Inc("style.css", "../../static/style.css")], None),
    "base_href_suppresses_relative_include": ('<base href="/app/">\n<script src="app.js"></script>\n', "index.html", [],
                                              [("web_base_href", None), ("web_relative_under_base", None)]),
    "url_key_strips_query_and_fragment": ('<a href="/orders/list?page=2#tab">Orders</a>\n', "index.html",
                                          [Key("/orders/list", "url")], None),
    "include_basename_strips_query": ('<script src="app.js?v=1"></script>\n', "index.html", [Inc("app.js", "app.js")], None),
    "uppercase_tag_unquoted_attribute": ("<SCRIPT SRC=app.js></SCRIPT>\n", "index.html", [Inc("app.js")], None),
    "unquoted_form_action": ("<form action=login></form>\n", "index.html", [("ACCESSES", "Config", "login", None)], None),
    "script_body_href_not_scanned": ("<script>var x = '<a href=\"/admin/delete\">';</script>\n", "index.html", [], None),
    "multiple_script_bodies_interleaved": (
        '<a href="/real-1">one</a>\n<script>var x = \'<a href="/fake-1">\';</script>\n<a href="/real-2">two</a>\n'
        '<script>var y = \'<a href="/fake-2">\';</script>\n<a href="/real-3">three</a>\n', "index.html",
        [Key("/real-1", "url"), Key("/real-2", "url"), Key("/real-3", "url")], None),
}


@pytest.mark.parametrize("text,path,refs,dropped", CASES.values(), ids=CASES)
def test_extract_refs(text, path, refs, dropped):
    res = A.extract_refs(text, path)
    got = res.refs
    exp = refs
    assert [(r.edge_type, r.kind, r.name) for r in got] == [e[:3] for e in exp]
    for r, e in zip(got, exp):
        assert e[3] is None or all(r.extra.get(k) == v for k, v in e[3].items())
    if dropped is not None:
        assert len(res.dropped) == len(dropped)
        for d, (reason, line) in zip(res.dropped, dropped):
            assert d.reason == reason and (line is None or d.line == line)


def test_unclosed_html_comment_sanitization_is_linear_time():
    start = time.perf_counter()
    A.extract_refs("<!--" * 20000, "index.html")
    assert time.perf_counter() - start < 1.0
