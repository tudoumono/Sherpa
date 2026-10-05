"""`JspAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A・入力ソース片 → 参照・Dropped の表）。"""
from __future__ import annotations

import time

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.jsp import JspAnalyzer

A = JspAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".jsp", ".jspx", ".jspf", ".tag", ".tagx"})
    assert A.name == "jsp"
    assert A.doctype == "jsp"


def test_accepts_all_jsp_files_without_content_inspection():
    assert JspAnalyzer.accepts is Analyzer.accepts


def test_collect_defs_primary_is_extension_included_filename():
    res = A.collect_defs("<html></html>", "WEB-INF/jsp/login.jsp")
    assert res.primary is not None and res.primary.label == "Module" and res.primary.name == "login.jsp"
    assert res.children == []


# 参照の期待値: (edge, kind, name, extra 部分)。Dropped は (reason, line|None)。
def Inc(name, path, kind="Module"):
    return ("INVOKES", kind, name, {"via": "include", "include_path": path})


def Key(name, key_kind):
    return ("ACCESSES", "Config", name, {"via": "config_key", "key_kind": key_kind})


EXT = "web_external_ref"
CASES = {
    # include 系（INVOKES via=include）
    "include_directive_file": ('<%@ include file="common/header.jspf" %>\n', "x.jsp",
                               [Inc("header.jspf", "common/header.jspf")], []),
    "jsp_include_page": ('<jsp:include page="../frag.jspf" />\n', "x.jsp", [Inc("frag.jspf", "../frag.jspf")], []),
    "c_import_url": ('<c:import url="/WEB-INF/frag.jspf" />\n', "x.jsp", [("INVOKES", "Module", "frag.jspf", {"via": "include"})], None),
    "script_src_and_link_href": ('<script src="../static/app.js"></script>\n<link href="../static/style.css">\n', "x.jsp",
                                 [("INVOKES", "Module", "app.js", {"via": "include"}),
                                  ("INVOKES", "Module", "style.css", {"via": "include"})], None),
    "windows_separator_normalized": (r'<%@ include file="common\header.jspf" %>' + "\n", "x.jsp",
                                     [Inc("header.jspf", "common/header.jspf")], None),
    "jspx_directive_include": ('<jsp:directive.include file="frag.jspf"/>\n', "x.jspx",
                               [("INVOKES", "Module", "frag.jspf", {"via": "include"})], None),
    "absolute_include_relative_to_referrer": ('<jsp:include page="/WEB-INF/shared/header.jspf" />\n',
                                              "gen1/WEB-INF/jsp/page.jsp", [Inc("header.jspf", "../shared/header.jspf")], None),
    "include_basename_strips_query": ('<script src="app.js?v=1"></script>\n', "x.jsp", [Inc("app.js", "app.js")], None),
    "uppercase_tag_unquoted_attribute": ('<SCRIPT SRC=app.js></SCRIPT>\n', "x.jsp", [("INVOKES", "Module", "app.js", None)], None),
    "attribute_order_independent_jsp_include": ('<jsp:include flush="true" page="frag.jspf"/>\n', "x.jsp",
                                                [("INVOKES", "Module", "frag.jspf", None)], None),
    "attribute_order_independent_c_import": ('<c:import var="x" url="frag.jspf"/>\n', "x.jsp",
                                             [("INVOKES", "Module", "frag.jspf", None)], None),
    # エッジ化しないもの
    "taglib_tagdir_no_reference": ('<%@ taglib tagdir="/WEB-INF/tags" %>\n<html></html>\n', "x.jsp", [], []),
    "page_import_is_hint_only": ('<%@ page import="java.util.List" %>\n', "x.jsp", [], None),
    "el_expression_not_processed": ("${loginBean.name}\n", "x.jsp", [], []),
    "jsp_comment_not_scanned": ('<%-- <%@ include file="commented.jspf" %> --%>\n', "x.jsp", [], None),
    "html_comment_not_scanned": ('<!-- <s:form action="commented"> -->\n', "x.jsp", [], None),
    "script_body_href_not_scanned": ("<script>var x = '<a href=\"/admin/delete\">';</script>\n", "x.jsp", [], None),
    # Struts action キー／URL キー（ACCESSES via=config_key）
    "struts_form_action_bare_name": ('<s:form action="login">\n</s:form>\n', "x.jsp", [Key("login", "action")], None),
    "href_dot_action_strips_suffix_and_slash": ('<a href="/login.action">Login</a>\n', "x.jsp", [Key("login", "action")], None),
    "action_extension_with_query": ('<a href="/login.action?next=/">Login</a>\n', "x.jsp", [Key("login", "action")], None),
    "unquoted_form_action": ("<form action=login></form>\n", "x.jsp", [("ACCESSES", "Config", "login", None)], None),
    "url_key_absolute_path": ('<a href="/orders/list">Orders</a>\n', "x.jsp", [Key("/orders/list", "url")], None),
    "url_key_strips_query": ('<a href="/orders/list?page=2">Orders</a>\n', "x.jsp", [Key("/orders/list", "url")], None),
    "url_key_strips_fragment": ('<a href="/orders/list#tab">Orders</a>\n', "x.jsp", [Key("/orders/list", "url")], None),
    "static_asset_href_not_config_key": ('<a href="/static/logo.png">logo</a>\n', "x.jsp", [], None),
    "hash_and_javascript_scheme_not_keys": ('<a href="#">top</a>\n<a href="javascript:void(0)">x</a>\n', "x.jsp", [], None),
    # jsp:useBean
    "use_bean_class_qualified": ('<jsp:useBean id="loginBean" class="com.acme.LoginAction" />\n', "x.jsp",
                                 [("INVOKES", "Module", "com.acme.LoginAction", {"via": "bean_class", "qualified": True})], None),
    # 外部参照スキーム（include 対象外・URL キーにもしない）
    "external_script_src_dropped": ('<script src="https://cdn.example.com/lib.js"></script>\n', "x.jsp", [], [(EXT, 1)]),
    "protocol_relative_link_dropped": ('<link href="//cdn.example.com/style.css">\n', "x.jsp", [], [(EXT, None)]),
    "data_uri_script_src_dropped": ('<script src="data:text/javascript,void(0)"></script>\n', "x.jsp", [], [(EXT, None)]),
    # <base href>: 対応せず Dropped・相対 include は basename 最近傍に落とさない
    "base_href_suppresses_relative_include": ('<base href="/app/">\n<script src="app.js"></script>\n', "x.jsp", [],
                                              [("web_base_href", None), ("web_relative_under_base", None)]),
    "absolute_include_unaffected_by_base_href": (
        '<base href="/app/">\n<jsp:include page="/WEB-INF/shared/header.jspf" />\n', "gen1/WEB-INF/jsp/page.jsp",
        [("INVOKES", "Module", "header.jspf", None)], None),
    # 埋め込みの中の文字列は読まない・属性値の中の式は空白に置き換える
    "tag_text_inside_scriptlet_string_not_scanned": ('<% String s = "<a href=\'/in-java\'>"; %>\n<a href="/real">x</a>\n', "x.jsp",
                                                    [Key("/real", "url")], None),
    "jsp_comment_may_contain_close_marker": ('<%-- a %> <a href="/in-comment"> --%>\n<a href="/real">x</a>\n', "x.jsp",
                                             [Key("/real", "url")], None),
    "expression_in_href_is_blanked": ('<a href="<%= ctx %>/x.action">x</a>\n', "x.jsp", [Key("x", "action")], None),
    # スクリプトレット
    "directive_and_expression_not_scriptlets": ('<%@ page import="a.b.C" %>\n<%= 1 + 1 %>\n', "x.jsp", [], []),
    "multiple_script_bodies_interleaved_with_real_tags": (
        '<a href="/real-1">one</a>\n<script>var x = \'<a href="/fake-1">\';</script>\n<a href="/real-2">two</a>\n'
        '<script>var y = \'<a href="/fake-2">\';</script>\n<a href="/real-3">three</a>\n', "x.jsp",
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


def test_scriptlet_is_dropped_once_at_first_line():
    res = A.extract_refs("<%\n  int x = 1;\n%>\n<%\n  int y = 2;\n%>\n", "x.jsp")
    scriptlets = [d for d in res.dropped if d.reason == "jsp_scriptlet"]
    assert len(scriptlets) == 1 and scriptlets[0].line == 1


def test_unclosed_html_comment_sanitization_is_linear_time():
    start = time.perf_counter()
    A.extract_refs("<!--" * 20000, "x.jsp")
    assert time.perf_counter() - start < 1.0


def test_broken_scriptlet_java_and_broken_template_are_reported_and_the_rest_is_read():
    res = A.extract_refs('<% int x = ; %>\n<a href="/ok">z</a>\n<% if (a) { %>\n<a href="/in-if">y</a>\n<%\n', "x.jsp")
    errors = [d.line for d in res.dropped if d.reason == "syntax_error"]
    assert 1 in errors and len(errors) >= 2
    assert [r.name for r in res.refs] == ["/ok", "/in-if"]


def test_valid_scriptlets_split_across_html_are_not_syntax_errors():
    text = '<% if (a) { %>\n<p>x</p>\n<% } else { %>\n<p>y</p>\n<% } %>\n<%! int n; %>\n<%= n %>\n'
    assert [d for d in A.extract_refs(text, "x.jsp").dropped if d.reason == "syntax_error"] == []
