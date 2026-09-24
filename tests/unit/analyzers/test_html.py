"""`HtmlTemplateAnalyzer` の単体テスト（アナライザ拡張 波3 レーン A・
docs/proposals/2026-09-05-アナライザ拡張.md §13）。"""
from __future__ import annotations

import time

from sherpa.ingest.analyzers.html import HtmlTemplateAnalyzer

A = HtmlTemplateAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".html", ".htm", ".xhtml"})
    assert A.name == "html"
    assert A.doctype == "html"


def test_fallback_to_text_kind_when_declined_is_declared():
    assert A.fallback_to_text_kind_when_declined is True


# --- content-aware accepts()（コーディネータ裁定・HTML の分類） ---

def test_accepts_html_with_form():
    assert A.accepts("x.html", "<html><body><form></form></body></html>") is True


def test_accepts_html_with_script_src():
    assert A.accepts("x.html", '<script src="app.js"></script>') is True


def test_accepts_html_with_link_stylesheet():
    assert A.accepts("x.html", '<link rel="stylesheet" href="a.css">') is True


def test_accepts_html_with_jsp_tag():
    assert A.accepts("x.html", '<jsp:include page="a.jsp"/>') is True


def test_accepts_html_with_jsf_h_tag():
    assert A.accepts("x.html", '<h:outputText value="hi"/>') is True


def test_accepts_html_with_thymeleaf_attr():
    assert A.accepts("x.html", '<div th:if="loggedIn">Hi</div>') is True


def test_accepts_html_with_el_expression():
    assert A.accepts("x.html", '<span>${user.name}</span>') is True


def test_accepts_html_with_jsp_scriptlet():
    assert A.accepts("x.html", '<% int x = 1; %>') is True


def test_accepts_html_with_action_link():
    assert A.accepts("x.html", '<a href="/login.action">Login</a>') is True


def test_declines_plain_html_without_any_marker():
    text = '<html><body><p>ただの本文です。フォームもスクリプトもありません。</p></body></html>'
    assert A.accepts("x.html", text) is False


def test_accepts_ignores_marker_inside_comment():
    """`accepts()` はコメントを除去してから判定する——コメントアウトされた過去のフォーム/
    スクリプトを目印として誤検出しない。"""
    text = '<!-- <form></form> -->\n<p>ただの本文です。</p>'
    assert A.accepts("x.html", text) is False


def test_collect_defs_primary_is_extension_included_filename():
    res = A.collect_defs("<html></html>", "static/index.html")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "index.html"
    assert res.children == []


def test_script_src_and_link_href_are_include():
    res = A.extract_refs('<script src="app.js"></script>\n<link href="style.css">\n', "index.html")
    names = {(r.name, r.extra["via"]) for r in res.refs}
    assert ("app.js", "include") in names
    assert ("style.css", "include") in names


def test_form_action_bare_name_is_action_key():
    res = A.extract_refs('<form action="login" method="post"></form>\n', "index.html")
    assert [(r.edge_type, r.kind, r.name, r.extra["via"]) for r in res.refs] == [
        ("ACCESSES", "Config", "login", "config_key")]
    assert res.refs[0].extra["key_kind"] == "action"


def test_url_key_absolute_path_without_extension():
    res = A.extract_refs('<a href="/orders/list">Orders</a>\n', "index.html")
    assert res.refs[0].name == "/orders/list"
    assert res.refs[0].extra["key_kind"] == "url"


def test_href_to_static_asset_with_extension_is_not_a_config_key():
    res = A.extract_refs('<a href="/static/logo.png">logo</a>\n', "index.html")
    assert res.refs == []


def test_html_comment_contents_are_not_scanned():
    res = A.extract_refs('<!-- <form action="commented"></form> -->\n', "index.html")
    assert res.refs == []


def test_no_dropped_entries_when_no_include_or_external_ref_or_base():
    """`<script>` に `src` が無ければ include 候補が無く、`<base>`/外部参照も無いので
    `dropped` は空（JSP のスクリプトレットのような専用構文は無い）。"""
    res = A.extract_refs('<script>var x = 1;</script>\n', "index.html")
    assert res.dropped == []


# --- 外部参照スキーム／`/` 始まりの scope 相対化／`<base href>` ---

def test_external_script_src_is_dropped_not_included():
    res = A.extract_refs('<script src="https://cdn.example.com/lib.js"></script>\n', "index.html")
    assert res.refs == []
    assert [(d.reason, d.line) for d in res.dropped] == [("web_external_ref", 1)]


def test_absolute_include_is_converted_to_referrer_relative_path():
    res = A.extract_refs('<link href="/static/style.css">\n', "gen1/WEB-INF/view/page.html")
    assert res.refs[0].name == "style.css"
    assert res.refs[0].extra["include_path"] == "../../static/style.css"


def test_base_href_is_dropped_and_relative_include_after_it_is_suppressed():
    text = '<base href="/app/">\n<script src="app.js"></script>\n'
    res = A.extract_refs(text, "index.html")
    assert any(d.reason == "web_base_href" for d in res.dropped)
    assert any(d.reason == "web_relative_under_base" for d in res.dropped)
    assert res.refs == []


# --- URL 正規化（`?`/`#` 以降を判定/basename取得の前に除去） ---

def test_url_key_strips_query_and_fragment():
    res = A.extract_refs('<a href="/orders/list?page=2#tab">Orders</a>\n', "index.html")
    assert res.refs[0].name == "/orders/list"


def test_include_basename_strips_query_string():
    res = A.extract_refs('<script src="app.js?v=1"></script>\n', "index.html")
    assert res.refs[0].name == "app.js"
    assert res.refs[0].extra["include_path"] == "app.js"


# --- 開始タグの字句解析（属性順不同・引用符省略・大文字タグ名・script本文除外） ---

def test_uppercase_tag_and_unquoted_attribute_value():
    res = A.extract_refs('<SCRIPT SRC=app.js></SCRIPT>\n', "index.html")
    assert res.refs[0].name == "app.js"


def test_unquoted_form_action_attribute():
    res = A.extract_refs('<form action=login></form>\n', "index.html")
    assert res.refs[0].name == "login"


def test_script_body_href_is_not_scanned():
    text = '<script>var x = \'<a href="/admin/delete">\';</script>\n'
    res = A.extract_refs(text, "index.html")
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
    res = A.extract_refs(text, "index.html")
    assert {r.name for r in res.refs} == {"/real-1", "/real-2", "/real-3"}


# --- コメント除去の二次時間対策（線形スキャナ） ---

def test_unclosed_html_comment_sanitization_is_linear_time():
    text = "<!--" * 20000
    start = time.perf_counter()
    A.extract_refs(text, "index.html")
    assert time.perf_counter() - start < 1.0
