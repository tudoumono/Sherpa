"""`world_graph.build_world()` 経由の C# アナライザ統合テスト（アナライザ拡張 S7・
docs/proposals/2026-09-05-アナライザ拡張.md §4(a)/§9 S7）。

`fixtures/corpus/cs1` を実際に `build_world()` へ通し、継承/実装（`: Base, IFoo`）が全件
`via=extends` になること・宣言型（`via=field_type`）・`new`（`via=call`）・完全修飾名（package
＝namespace 込みの `cid_key`）・`partial class`（複数ファイル分割）の Dropped 化・非 primary 型
（internal sibling）にも namespace 込み `cid_key` を持たせ `using Alias = N.Child;` の完全修飾参照が
別 namespace の同名型と混同されないことを固定する。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "cs1"
WORLD_ID = "cs1_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]): n for n in nodes}


def _edge_tuples(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


def test_order_service_node_exists_with_namespace_qualified_source():
    """primary の cid 自体は表示名（`.name`）基準（Java と同じ規律・`world_graph.py` の
    `_cid(..., defres.primary.name)`）——qualified 名（`cid_key`＝`Acme.Order.OrderService`）は
    `qualified_defs` 側の追加索引としてのみ使われる（`test_csharp.py` で cid_key 自体を確認済み）。
    """
    nodes, _edges, _flags = _build()
    by_key = _node_keys(nodes)
    node = by_key[("Module", "OrderService", "Acme/Order/OrderService.cs")]
    assert node["cid"] == "module:cs1_test:Acme/Order/OrderService.cs#OrderService"


def test_base_list_entries_are_all_via_extends():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    order_service = ("Module", "OrderService", "Acme/Order/OrderService.cs")
    base_service = ("Module", "BaseService", "Acme/Core/BaseService.cs")
    iorder_service = ("Module", "IOrderService", "Acme/Order/IOrderService.cs")
    assert ("INVOKES", order_service, base_service, "extends") in tuples
    assert ("INVOKES", order_service, iorder_service, "extends") in tuples
    assert not any(t[0] == "INVOKES" and t[1] == order_service and t[3] == "implements" for t in tuples)


def test_field_declaration_type_is_field_type_reference():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    order_service = ("Module", "OrderService", "Acme/Order/OrderService.cs")
    iorder_repo = ("Module", "IOrderRepo", "Acme/Data/IOrderRepo.cs")
    assert ("INVOKES", order_service, iorder_repo, "field_type") in tuples


def test_new_expression_is_call_reference():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    order_service = ("Module", "OrderService", "Acme/Order/OrderService.cs")
    order_repo = ("Module", "OrderRepo", "Acme/Data/OrderRepo.cs")
    assert ("INVOKES", order_service, order_repo, "call") in tuples


def test_order_repo_extends_iorder_repo_across_directories_within_same_generation():
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    order_repo = ("Module", "OrderRepo", "Acme/Data/OrderRepo.cs")
    iorder_repo = ("Module", "IOrderRepo", "Acme/Data/IOrderRepo.cs")
    assert ("INVOKES", order_repo, iorder_repo, "extends") in tuples


def test_using_alias_field_reference_resolves_to_the_internal_sibling_in_its_own_namespace():
    """内部/非 public 型（children）にも namespace 込みの `cid_key` を設定する是正——別 namespace
    に同名の internal sibling（`Helper`）が複数存在しても、`using Alias = N.Child;` の完全修飾参照は
    `cid_key` の完全一致で指定 namespace（`Acme.Widgets`）の型だけに一意に繋がり、別 namespace
    （`Acme.Reports`）の同名型へは誤接続しない。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    widget_host = ("Module", "WidgetHost", "Acme/Widgets/WidgetHost.cs")
    widgets_helper = ("Module", "Helper", "Acme/Widgets/WidgetHost.cs")
    reports_helper = ("Module", "Helper", "Acme/Reports/ReportHost.cs")
    assert ("INVOKES", widget_host, widgets_helper, "field_type") in tuples
    assert not any(t[0] == "INVOKES" and t[1] == widget_host and t[2] == reports_helper
                   for t in tuples)


def test_generic_type_argument_keeps_full_qualification_and_resolves_to_internal_child():
    """ジェネリック型引数（`B.Box<C.Dep>` の `C.Dep`）は判定に末尾セグメント（`Dep`）を使うが、
    参照そのものは完全修飾トークンを保持したまま `_resolve_qualified` へ渡る——internal child
    （`Acme/Misc/CTypes.cs` の `Dep`・namespace `C` 込みの `cid_key=C.Dep`）へ一意に解決する。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    box_host = ("Module", "BoxHost", "Acme/Misc/BoxHost.cs")
    dep_child = ("Module", "Dep", "Acme/Misc/CTypes.cs")
    assert ("INVOKES", box_host, dep_child, "field_type") in tuples


def test_partial_class_split_across_two_files_is_dropped_in_both():
    """`partial class`（複数ファイル分割）は解決せず両ファイルでそれぞれ申告する（Dropped・
    ファイル自体の主体は通常どおり作る）。"""
    nodes, _edges, flags = _build()
    by_key = _node_keys(nodes)
    assert ("Module", "BatchJob", "Acme/Order/BatchJob.Part1.cs") in by_key
    assert ("Module", "BatchJob", "Acme/Order/BatchJob.Part2.cs") in by_key
    partial_flags = {(f["from"]) for f in flags
                     if f.get("reason") == "dropped_syntax" and f.get("why") == "cs_partial"}
    assert partial_flags == {"Acme/Order/BatchJob.Part1.cs", "Acme/Order/BatchJob.Part2.cs"}
