"""`world_graph.build_world()` 経由の画面テンプレート（JSP/JS/CSS）統合テスト（アナライザ拡張
波3 レーン A・docs/proposals/2026-09-05-アナライザ拡張.md §13）。

`jsp.py`/`html.py`/`js.py`/`css.py` は `registry._ANALYZERS` に統合登録済み（波3 統合）のため、
素の registry のまま `fixtures/corpus/web1` を `build_world()` へ通す。

固定する内容: `<%@ include %>`/`<script src>`/`<link href>` の2段解決（相対パス完全一致→
拡張子込み basename 最近傍）・`<jsp:useBean class="...">` の完全修飾名解決（Java 側の primary へ）・
JS の `fetch("/api/orders")` が未定義キーで unresolved になること・`"/api/" + id` の動的連結が
`Dropped("js_dynamic_url", ...)` になること・JSP スクリプトレットの `Dropped("jsp_scriptlet", ...)`。

Config キーは種別（`key_kind`）で名前空間を分ける（裁定2026-09-06）: `<s:form action="login">`
は `key_kind="action"` の参照になるが、`app.properties` の `login=` キーは `key_kind="property"`
（xml_config.py の Struts action 定義側は本レーンのスコープ外のため、この world には action 種別の
定義が無い）——別種別のため接続せず unresolved のまま。同世代の `LoginAction.java` に足した
`@Value("${login}")`（`key_kind="property"`）が代わりに `app.properties` の `login=` キーへ A9 で
解決し、同種別同士の全件接続が引き続き機能することを固定する。

`OrderController.java`（`@RequestMapping("/orders")` クラス＋`@GetMapping("/list")` メソッド）は
URL キー定義側（波3 統合・Java アナライザの新規抽出）の検証用に追加した——`login.jsp` の
`href="/orders/list"`（既存の JSP URL キー参照）と `app.js` の `fetch("/orders/list")`
（本作業で追加）の両方が、この定義へ A9（同一 top_scope 内の同名 `Config` キー全件）で解決する。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "web1"
WORLD_ID = "web1_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]): n for n in nodes}


def _edge_tuples(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


def test_login_jsp_includes_header_fragment_via_relative_path():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    login_jsp = ("Module", "login.jsp", "gen1/WEB-INF/jsp/login.jsp")
    header_jspf = ("Module", "header.jspf", "gen1/WEB-INF/jsp/common/header.jspf")
    assert ("INVOKES", login_jsp, header_jspf, "include") in tuples


def test_login_jsp_includes_app_js_via_relative_path():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    login_jsp = ("Module", "login.jsp", "gen1/WEB-INF/jsp/login.jsp")
    app_js = ("Module", "app.js", "gen1/static/app.js")
    assert ("INVOKES", login_jsp, app_js, "include") in tuples


def test_login_jsp_includes_style_css_via_relative_path():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    login_jsp = ("Module", "login.jsp", "gen1/WEB-INF/jsp/login.jsp")
    style_css = ("Module", "style.css", "gen1/static/style.css")
    assert ("INVOKES", login_jsp, style_css, "include") in tuples


def test_style_css_imports_base_css():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    style_css = ("Module", "style.css", "gen1/static/style.css")
    base_css = ("Module", "base.css", "gen1/static/base.css")
    assert ("INVOKES", style_css, base_css, "include") in tuples


def test_struts_form_action_key_kind_does_not_cross_match_a_property_key():
    """`<s:form action="login">` は `key_kind="action"` の参照になる。`app.properties` の
    `login=` キーは `key_kind="property"`（properties.py の既定）——Config キーの名前空間分離
    （裁定2026-09-06）により種別が違うキーへは接続しない（`struts.xml` の action 定義側は
    xml_config.py の担当・本レーンのスコープ外のため、この world に action 種別の定義は無い）。"""
    _nodes, _edges, flags = _build()
    unresolved = [f for f in flags if f.get("reason") == "unresolved"
                 and f.get("from") == "gen1/WEB-INF/jsp/login.jsp"
                 and f.get("kind") == "Config" and f.get("name") == "login"]
    assert len(unresolved) == 1


def test_java_value_annotation_resolves_to_properties_config_key_via_a9():
    """`LoginAction.java` の `@Value("${login}")`（`key_kind="property"`）は `app.properties` の
    `login=` キー（同じく `key_kind="property"`）へ A9 で解決する——名前空間分離後も同種別同士の
    全件接続は引き続き機能する。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    login_action = ("Module", "LoginAction", "gen1/com/acme/LoginAction.java")
    login_key = ("Config", "login", "gen1/app.properties")
    assert ("ACCESSES", login_action, login_key, "config_key") in tuples


def test_use_bean_class_resolves_qualified_name_to_java_module():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    login_jsp = ("Module", "login.jsp", "gen1/WEB-INF/jsp/login.jsp")
    login_action = ("Module", "LoginAction", "gen1/com/acme/LoginAction.java")
    assert ("INVOKES", login_jsp, login_action, "bean_class") in tuples


def test_app_js_fetch_config_key_is_unresolved_without_a_matching_definition():
    """`fetch("/api/orders")` は世界内に同名 `Config` キーが無い＝unresolved flag のみ（ノード化しない）。"""
    _nodes, _edges, flags = _build()
    unresolved = [f for f in flags if f.get("reason") == "unresolved" and f.get("from") == "gen1/static/app.js"
                 and f.get("kind") == "Config" and f.get("name") == "/api/orders"]
    assert len(unresolved) == 1


def test_app_js_dynamic_url_concatenation_is_dropped():
    _nodes, _edges, flags = _build()
    dynamic = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == "js_dynamic_url"]
    assert len(dynamic) == 1 and dynamic[0]["from"] == "gen1/static/app.js"


def test_login_jsp_scriptlet_is_dropped():
    _nodes, _edges, flags = _build()
    scriptlet = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == "jsp_scriptlet"]
    assert len(scriptlet) == 1 and scriptlet[0]["from"] == "gen1/WEB-INF/jsp/login.jsp"


def test_login_jsp_url_key_orders_list_resolves_to_order_controller():
    """`href="/orders/list"`（URL キー）は `OrderController.java` の `@RequestMapping("/orders")`+
    `@GetMapping("/list")`（URL キー定義側・波3 統合）へ A9 で解決する。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    login_jsp = ("Module", "login.jsp", "gen1/WEB-INF/jsp/login.jsp")
    orders_list_key = ("Config", "/orders/list", "gen1/com/acme/OrderController.java")
    assert ("ACCESSES", login_jsp, orders_list_key, "config_key") in tuples


def test_app_js_fetch_orders_list_resolves_to_order_controller():
    """`fetch("/orders/list")`（`app.js`・本作業で追加）も同じ `Config(key:/orders/list)` へ
    A9（同一 top_scope 内の同名キー全件）で解決する。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    app_js = ("Module", "app.js", "gen1/static/app.js")
    orders_list_key = ("Config", "/orders/list", "gen1/com/acme/OrderController.java")
    assert ("ACCESSES", app_js, orders_list_key, "config_key") in tuples
