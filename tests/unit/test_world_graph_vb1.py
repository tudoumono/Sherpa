"""`world_graph.build_world()` 経由の VB アナライザ統合テスト（アナライザ拡張 波3 レーン C・
docs/proposals/2026-09-05-アナライザ拡張.md §9・ユーザー裁定 2026-09-06）。

`VbAnalyzer` は `registry._ANALYZERS` に統合登録済み（波3 統合）のため、素の registry のまま
`fixtures/corpus/vb1` を `build_world()` へ通す。

`net/`（VB.NET・継承の単純名解決）と `vb6/`（VB6/VBA・単純名2段目解決＋修飾呼び出し＋SQL文字列）は
別トップフォルダ＝別世代（鏡モデル・世代跨ぎの構造リンクなし）として設計する。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "vb1"
WORLD_ID = "vb1_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]): n for n in nodes}


def _edge_tuples(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


# --- VB.NET（net/）: Namespace/Class・Inherits（単純名・primary 同士）---

def test_order_service_and_base_service_nodes_are_uppercase_normalized():
    nodes, _edges, _flags = _build()
    by_key = _node_keys(nodes)
    assert ("Module", "ORDERSERVICE", "net/Order/OrderService.vb") in by_key
    assert ("Module", "BASESERVICE", "net/Core/BaseService.vb") in by_key


def test_order_service_inherits_base_service_via_extends():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    order_service = ("Module", "ORDERSERVICE", "net/Order/OrderService.vb")
    base_service = ("Module", "BASESERVICE", "net/Core/BaseService.vb")
    assert ("INVOKES", order_service, base_service, "extends") in tuples


def test_order_service_sub_calls_sibling_sub_within_same_class():
    """`Process` が同一クラス内の `LogStart` を括弧呼び出しで解決する（VB.NET でも children の
    単純名2段目解決が働く・自己ファイル内でも除外しない）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    process = ("Module", "LOGSTART", "net/Order/OrderService.vb")
    assert any(t[0] == "INVOKES" and t[2] == process and t[3] == "call" for t in tuples)


# --- VB6/VBA（vb6/）: 単純名2段目解決・修飾呼び出し・SQL 文字列・late-bound Dropped ---

def test_modmain_bare_call_resolves_to_moddata_loadorders_definition_child():
    """`Call LoadOrders`（括弧なし・単純名）→ world_graph の2段目（simple_name_defs）解決で
    `modData.bas` の `LoadOrders` 定義 child へ繋がる（§9・C と同じ仕組み）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    modmain = ("Module", "MODMAIN", "vb6/modMain.bas")
    loadorders = ("Module", "LOADORDERS", "vb6/modData.bas")
    assert ("INVOKES", modmain, loadorders, "call") in tuples


def test_modmain_bare_call_without_call_keyword_resolves_within_same_file():
    """`LogMessage "starting"`（`Call` キーワード無し・括弧無し）も同じ機構で解決する。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    modmain = ("Module", "MODMAIN", "vb6/modMain.bas")
    logmessage = ("Module", "LOGMESSAGE", "vb6/modMain.bas")
    assert ("INVOKES", modmain, logmessage, "call") in tuples


def test_frmmain_qualified_call_resolves_to_moddata_loadorders():
    """`Call modData.LoadOrders(1)`（修飾・`.` 込み）は完全修飾名の cid_key 完全一致で解決する。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    frmmain = ("Module", "FRMMAIN", "vb6/frmMain.frm")
    loadorders = ("Module", "LOADORDERS", "vb6/modData.bas")
    assert ("INVOKES", frmmain, loadorders, "call") in tuples


def test_moddata_sql_string_concatenation_accesses_orders_table():
    """`"SELECT * " & _` 継続で連結された SQL 文字列から `ORDERS` テーブルへ `ACCESSES` を張る
    （`via="vba_sql"` は `KNOWN_VIA` 既知値・波3 統合で追加済みのため `unknown_via` flag は立たない）。
    """
    nodes, edges, flags = _build()
    tuples = _edge_tuples(nodes, edges)
    moddata = ("Module", "MODDATA", "vb6/modData.bas")
    orders = ("Table", "ORDERS", "vb6/schema.sql")
    assert ("ACCESSES", moddata, orders, "vba_sql") in tuples
    unknown_via = [f for f in flags if f.get("reason") == "unknown_via" and f.get("via") == "vba_sql"
                  and f.get("from") == "vb6/modData.bas"]
    assert unknown_via == []


def test_createobject_calls_are_dropped_as_vb_late_bound_with_progid_snippet():
    _nodes, _edges, flags = _build()
    late_bound = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("why") == "vb_late_bound"]
    snippets = {(f["from"], f["snippet"]) for f in late_bound}
    assert ("vb6/modMain.bas", "ADODB.Connection") in snippets
    assert ("vb6/modData.bas", "ADODB.Recordset") in snippets


# --- 言語間の名前一致（net/ 世代内の VB↔C#・裁定2026-09-06）: 表記が一致するものだけ許容する ---

def test_new_api_call_resolves_to_csharp_all_caps_class_in_same_generation():
    """VB は識別子を大文字化するため、C# の全大文字クラス名（`API`）とは表記が一致すれば
    同世代内の最近傍解決で繋がる（言語ドメインで索引を分けない・既存契約の範囲内として許容）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    apiclient = ("Module", "APICLIENT", "net/Api/ApiClient.vb")
    api = ("Module", "API", "net/Api/Api.cs")
    assert ("INVOKES", apiclient, api, "call") in tuples


def test_qualified_name_case_mismatch_does_not_cross_resolve_to_csharp():
    """`Acme.Order.OrderService`（VB 側は完全修飾名を大文字化した cid_key で保持）は、元の大小文字を
    保つ C# 側の cid_key とは文字列として一致しないため、VB 自身の同名クラスへ解決される
    （C# の同名ノードへは繋がらない・言語ドメインの索引を混ぜない安全側の帰結）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    apiclient = ("Module", "APICLIENT", "net/Api/ApiClient.vb")
    vb_order_service = ("Module", "ORDERSERVICE", "net/Order/OrderService.vb")
    cs_order_service = ("Module", "OrderService", "net/Api/OrderService.cs")
    assert ("INVOKES", apiclient, vb_order_service, "call") in tuples
    assert not any(t[0] == "INVOKES" and t[2] == cs_order_service for t in tuples)
