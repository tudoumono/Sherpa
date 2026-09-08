"""`JspAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A・docs/proposals/2026-09-05-アナライザ拡張.md
§13）。"""
from __future__ import annotations

import time

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
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "login.jsp"
    assert res.children == []


# --- include 系（INVOKES via=include） ---

def test_include_directive_file_attr():
    res = A.extract_refs('<%@ include file="common/header.jspf" %>\n', "x.jsp")
    assert [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs] == [
        ("INVOKES", "Module", "header.jspf",
         {"via": "include", "include_path": "common/header.jspf"})]


def test_jsp_include_page_attr():
    res = A.extract_refs('<jsp:include page="../frag.jspf" />\n', "x.jsp")
    assert res.refs[0].name == "frag.jspf"
    assert res.refs[0].extra == {"via": "include", "include_path": "../frag.jspf"}


def test_c_import_url_attr():
    res = A.extract_refs('<c:import url="/WEB-INF/frag.jspf" />\n', "x.jsp")
    assert res.refs[0].name == "frag.jspf"
    assert res.refs[0].extra["via"] == "include"


def test_script_src_and_link_href_are_include():
    res = A.extract_refs('<script src="../static/app.js"></script>\n<link href="../static/style.css">\n', "x.jsp")
    names = {(r.name, r.extra["via"]) for r in res.refs}
    assert ("app.js", "include") in names
    assert ("style.css", "include") in names


def test_windows_style_separator_is_normalized():
    res = A.extract_refs(r'<%@ include file="common\header.jspf" %>' + "\n", "x.jsp")
    assert res.refs[0].name == "header.jspf"
    assert res.refs[0].extra["include_path"] == "common/header.jspf"


# --- taglib tagdir（ディレクトリ指定・エッジ化しない） ---

def test_taglib_tagdir_produces_no_reference():
    res = A.extract_refs('<%@ taglib tagdir="/WEB-INF/tags" %>\n<html></html>\n', "x.jsp")
    assert res.refs == [] and res.dropped == []


def test_page_import_is_hint_only_no_edge():
    res = A.extract_refs('<%@ page import="java.util.List" %>\n', "x.jsp")
    assert res.refs == []


# --- Struts action キー／URL キー（ACCESSES via=config_key） ---

def test_struts_form_action_bare_name_is_action_key():
    res = A.extract_refs('<s:form action="login">\n</s:form>\n', "x.jsp")
    assert [(r.edge_type, r.kind, r.name, r.extra["via"]) for r in res.refs] == [
        ("ACCESSES", "Config", "login", "config_key")]
    assert res.refs[0].extra["key_kind"] == "action"


def test_href_with_dot_action_extension_strips_suffix_and_leading_slash():
    res = A.extract_refs('<a href="/login.action">Login</a>\n', "x.jsp")
    assert res.refs[0].name == "login"
    assert res.refs[0].extra["key_kind"] == "action"


def test_url_key_absolute_path_without_extension():
    res = A.extract_refs('<a href="/orders/list">Orders</a>\n', "x.jsp")
    assert res.refs[0].name == "/orders/list"
    assert res.refs[0].extra["key_kind"] == "url"


def test_url_key_strips_query_string():
    res = A.extract_refs('<a href="/orders/list?page=2">Orders</a>\n', "x.jsp")
    assert res.refs[0].name == "/orders/list"


def test_href_to_static_asset_with_extension_is_not_a_config_key():
    res = A.extract_refs('<a href="/static/logo.png">logo</a>\n', "x.jsp")
    assert res.refs == []


def test_href_hash_and_javascript_scheme_are_not_config_keys():
    res = A.extract_refs('<a href="#">top</a>\n<a href="javascript:void(0)">x</a>\n', "x.jsp")
    assert res.refs == []


# --- jsp:useBean（INVOKES via=bean_class qualified） ---

def test_use_bean_class_is_qualified_invokes():
    res = A.extract_refs('<jsp:useBean id="loginBean" class="com.acme.LoginAction" />\n', "x.jsp")
    assert [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs] == [
        ("INVOKES", "Module", "com.acme.LoginAction", {"via": "bean_class", "qualified": True})]


def test_use_bean_id_alone_is_not_processed():
    """`id` 属性・EL式 `${bean.prop}` はどちらも対象外（値スタックが曖昧なため・限界明記）。"""
    res = A.extract_refs('${loginBean.name}\n', "x.jsp")
    assert res.refs == [] and res.dropped == []


# --- コメント除去（JSP コメント／HTML コメント） ---

def test_jsp_comment_contents_are_not_scanned():
    res = A.extract_refs('<%-- <%@ include file="commented.jspf" %> --%>\n', "x.jsp")
    assert res.refs == []


def test_html_comment_contents_are_not_scanned():
    res = A.extract_refs('<!-- <s:form action="commented"> -->\n', "x.jsp")
    assert res.refs == []


# --- スクリプトレット（Dropped） ---

def test_scriptlet_is_dropped_once():
    text = "<%\n  int x = 1;\n%>\n<%\n  int y = 2;\n%>\n"
    res = A.extract_refs(text, "x.jsp")
    scriptlets = [d for d in res.dropped if d.reason == "jsp_scriptlet"]
    assert len(scriptlets) == 1
    assert scriptlets[0].line == 1


def test_directive_and_expression_tags_are_not_scriptlets():
    text = '<%@ page import="a.b.C" %>\n<%= 1 + 1 %>\n'
    res = A.extract_refs(text, "x.jsp")
    assert not any(d.reason == "jsp_scriptlet" for d in res.dropped)


# --- 外部参照スキーム（include 対象外・URL キーにもしない） ---

def test_external_script_src_is_dropped_not_included():
    res = A.extract_refs('<script src="https://cdn.example.com/lib.js"></script>\n', "x.jsp")
    assert res.refs == []
    assert [(d.reason, d.line) for d in res.dropped] == [("web_external_ref", 1)]


def test_protocol_relative_link_href_is_dropped_not_included():
    res = A.extract_refs('<link href="//cdn.example.com/style.css">\n', "x.jsp")
    assert res.refs == []
    assert any(d.reason == "web_external_ref" for d in res.dropped)


def test_data_uri_script_src_is_dropped():
    res = A.extract_refs('<script src="data:text/javascript,void(0)"></script>\n', "x.jsp")
    assert res.refs == []
    assert any(d.reason == "web_external_ref" for d in res.dropped)


# --- `/` 始まりの include の scope 相対化 ---

def test_absolute_include_is_converted_to_referrer_relative_path():
    res = A.extract_refs('<jsp:include page="/WEB-INF/shared/header.jspf" />\n',
                         "gen1/WEB-INF/jsp/page.jsp")
    assert res.refs[0].name == "header.jspf"
    assert res.refs[0].extra["include_path"] == "../shared/header.jspf"


# --- `<base href>`（対応せず Dropped・相対 include は basename 最近傍に落とさない） ---

def test_base_href_is_dropped_and_relative_include_after_it_is_suppressed():
    text = '<base href="/app/">\n<script src="app.js"></script>\n'
    res = A.extract_refs(text, "x.jsp")
    assert any(d.reason == "web_base_href" for d in res.dropped)
    assert any(d.reason == "web_relative_under_base" for d in res.dropped)
    assert res.refs == []


def test_absolute_include_is_unaffected_by_base_href():
    text = '<base href="/app/">\n<jsp:include page="/WEB-INF/shared/header.jspf" />\n'
    res = A.extract_refs(text, "gen1/WEB-INF/jsp/page.jsp")
    assert res.refs[0].name == "header.jspf"


# --- URL 正規化（`?`/`#` 以降を判定/basename取得の前に除去） ---

def test_action_extension_with_query_string_still_strips_correctly():
    res = A.extract_refs('<a href="/login.action?next=/">Login</a>\n', "x.jsp")
    assert res.refs[0].name == "login"


def test_url_key_strips_fragment():
    res = A.extract_refs('<a href="/orders/list#tab">Orders</a>\n', "x.jsp")
    assert res.refs[0].name == "/orders/list"


def test_include_basename_strips_query_string():
    res = A.extract_refs('<script src="app.js?v=1"></script>\n', "x.jsp")
    assert res.refs[0].name == "app.js"
    assert res.refs[0].extra["include_path"] == "app.js"


# --- 開始タグの字句解析（属性順不同・引用符省略・大文字タグ名・script本文除外） ---

def test_uppercase_tag_and_unquoted_attribute_value():
    res = A.extract_refs('<SCRIPT SRC=app.js></SCRIPT>\n', "x.jsp")
    assert res.refs[0].name == "app.js"


def test_unquoted_form_action_attribute():
    res = A.extract_refs('<form action=login></form>\n', "x.jsp")
    assert res.refs[0].name == "login"


def test_attribute_order_independence_for_jsp_include():
    res = A.extract_refs('<jsp:include flush="true" page="frag.jspf"/>\n', "x.jsp")
    assert res.refs[0].name == "frag.jspf"


def test_attribute_order_independence_for_c_import():
    res = A.extract_refs('<c:import var="x" url="frag.jspf"/>\n', "x.jsp")
    assert res.refs[0].name == "frag.jspf"


def test_script_body_href_is_not_scanned():
    text = '<script>var x = \'<a href="/admin/delete">\';</script>\n'
    res = A.extract_refs(text, "x.jsp")
    assert res.refs == []


def test_multiple_script_bodies_interleaved_with_real_tags_are_excluded_correctly():
    """タグ／除外 span の単調ポインタ突合は、複数の `<script>` ブロックが
    実タグと交互に現れても取りこぼし・誤除外なく判定する（二次時間対策の副作用で判定順序に
    依存するバグを作っていないことの固定）。"""
    text = (
        '<a href="/real-1">one</a>\n'
        '<script>var x = \'<a href="/fake-1">\';</script>\n'
        '<a href="/real-2">two</a>\n'
        '<script>var y = \'<a href="/fake-2">\';</script>\n'
        '<a href="/real-3">three</a>\n'
    )
    res = A.extract_refs(text, "x.jsp")
    assert {r.name for r in res.refs} == {"/real-1", "/real-2", "/real-3"}


def test_jspx_directive_include_is_processed():
    res = A.extract_refs('<jsp:directive.include file="frag.jspf"/>\n', "x.jspx")
    assert res.refs[0].name == "frag.jspf"
    assert res.refs[0].extra["via"] == "include"


# --- コメント除去の二次時間対策（線形スキャナ） ---

def test_unclosed_html_comment_sanitization_is_linear_time():
    text = "<!--" * 20000
    start = time.perf_counter()
    A.extract_refs(text, "x.jsp")
    assert time.perf_counter() - start < 1.0
