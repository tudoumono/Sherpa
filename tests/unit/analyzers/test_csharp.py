"""`CSharpAnalyzer` の単体テスト（アナライザ拡張 §4(a)/§9 S7・A1＝本体のみ・FW なし）。"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.csharp import CSharpAnalyzer

A = CSharpAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".cs"})
    assert A.name == "csharp"
    assert A.doctype == "csharp"


def test_accepts_all_cs_files_without_content_inspection():
    assert CSharpAnalyzer.accepts is Analyzer.accepts


# --- primary（public 型＝primary・qualified name）---

def test_collect_defs_extracts_public_class_as_primary_with_file_scoped_namespace():
    text = "namespace Acme.Order;\n\npublic class OrderService\n{\n}\n"
    res = A.collect_defs(text, "Order/OrderService.cs")
    assert res.primary.label == "Module" and res.primary.name == "OrderService"
    assert res.primary.cid_key == "Acme.Order.OrderService"


def test_collect_defs_extracts_public_class_with_block_scoped_namespace():
    text = "namespace Acme.Order\n{\n    public class OrderService\n    {\n    }\n}\n"
    res = A.collect_defs(text, "Order/OrderService.cs")
    assert res.primary.name == "OrderService"
    assert res.primary.cid_key == "Acme.Order.OrderService"
    assert res.children == [] and res.dropped == []


def test_collect_defs_without_namespace_has_no_qualified_prefix():
    res = A.collect_defs("public class Standalone\n{\n}\n", "Standalone.cs")
    assert res.primary.name == "Standalone"
    assert res.primary.cid_key == "Standalone"


def test_collect_defs_falls_back_to_first_type_when_no_public_type_present():
    res = A.collect_defs("internal class Helper\n{\n}\n", "Helper.cs")
    assert res.primary is not None and res.primary.name == "Helper"


def test_collect_defs_extracts_non_public_sibling_as_child_module():
    text = (
        "namespace Acme.Order;\n\n"
        "public class OrderService\n{\n}\n\n"
        "class InternalHelper\n{\n}\n"
    )
    res = A.collect_defs(text, "Order/OrderService.cs")
    assert res.primary.name == "OrderService"
    assert [c.name for c in res.children] == ["InternalHelper"]


def test_interface_struct_enum_record_are_all_recognized_type_kinds():
    for keyword, name in (("interface", "IFoo"), ("struct", "Point"), ("enum", "Color"),
                          ("record", "Money")):
        text = f"public {keyword} {name}\n{{\n}}\n"
        res = A.collect_defs(text, f"{name}.cs")
        assert res.primary.name == name


def test_record_struct_and_record_class_forms_are_recognized_with_correct_name():
    """`record` は `record`/`record struct`/`record class` の3形とも取れる——2語目のキーワードが
    名前として誤って捕まらないこと（型宣言正規表現の是正）。"""
    for text, filename, expected_name in (
        ("public record Money(int V)\n{\n}\n", "Money.cs", "Money"),
        ("public record struct Point(int X, int Y)\n{\n}\n", "Point.cs", "Point"),
        ("public record class Customer(string Name)\n{\n}\n", "Customer.cs", "Customer"),
    ):
        res = A.collect_defs(text, filename)
        assert res.primary is not None and res.primary.name == expected_name


def test_nested_type_is_dropped_not_a_child():
    text = "public class Outer\n{\n    class Inner\n    {\n    }\n}\n"
    res = A.collect_defs(text, "Outer.cs")
    assert res.children == []
    assert [d.reason for d in res.dropped] == ["nested_type"]


# --- partial class（複数ファイル分割は解決せず Dropped で申告）---

def test_partial_class_is_reported_as_dropped_but_still_has_primary():
    text = "namespace Acme.Order;\n\npublic partial class Big\n{\n    void A() {}\n}\n"
    res = A.collect_defs(text, "Order/Big.Part1.cs")
    assert res.primary is not None and res.primary.name == "Big"
    assert [d.reason for d in res.dropped] == ["cs_partial"]


# --- 参照（継承/実装は全部 via=extends・宣言型・new）---

def test_base_list_all_entries_become_via_extends():
    text = "namespace Acme.Order;\n\npublic class OrderService : BaseService, IOrderService\n{\n}\n"
    res = A.extract_refs(text, "Order/OrderService.cs")
    assert [(r.name, r.extra["via"]) for r in res.refs] == [
        ("BaseService", "extends"), ("IOrderService", "extends")]


# --- base list の対象範囲は `where` 手前まで・`enum` の `:` は underlying type であり base list ではない ---

def test_generic_constraint_where_clause_alone_is_not_a_base_list():
    text = "public class A<T>\n    where T : B\n{\n}\n"
    res = A.extract_refs(text, "A.cs")
    assert res.refs == []


def test_enum_underlying_type_colon_is_not_extends():
    text = "public enum Color : int\n{\n}\n"
    res = A.extract_refs(text, "Color.cs")
    assert res.refs == []


def test_base_list_before_where_clause_is_still_extracted():
    text = "public class A : B\n    where T : C\n{\n}\n"
    res = A.extract_refs(text, "A.cs")
    assert [(r.name, r.extra["via"]) for r in res.refs] == [("B", "extends")]


def test_field_declaration_type_is_field_type_reference():
    text = (
        "namespace Acme.Order;\n\n"
        "public class OrderService\n{\n"
        "    private IOrderRepo _repo;\n"
        "}\n"
    )
    res = A.extract_refs(text, "Order/OrderService.cs")
    assert [(r.name, r.extra["via"]) for r in res.refs] == [("IOrderRepo", "field_type")]


def test_var_declared_field_is_excluded():
    text = "namespace Acme.Order;\n\npublic class Foo\n{\n    var bar = 1;\n}\n"
    res = A.extract_refs(text, "Foo.cs")
    assert res.refs == []


def test_new_expression_is_call_reference():
    text = (
        "namespace Acme.Order;\n\n"
        "public class OrderService\n{\n"
        "    public OrderService()\n    {\n"
        "        var repo = new OrderRepo();\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "Order/OrderService.cs")
    call_refs = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [r.name for r in call_refs] == ["OrderRepo"]


def test_using_directive_is_not_a_reference():
    text = "namespace Acme.Order;\n\nusing Acme.Data;\n\npublic class OrderService\n{\n}\n"
    res = A.extract_refs(text, "Order/OrderService.cs")
    assert res.refs == []


# --- `using Alias = Namespace.Real;`（エイリアス形）は実体への qualified 参照に置換する ---

def test_using_alias_new_expression_resolves_to_the_aliased_qualified_type():
    text = (
        "namespace Acme.Order;\n\n"
        "using Alias = Other.Real;\n\n"
        "public class OrderService\n{\n"
        "    public OrderService()\n    {\n"
        "        var x = new Alias();\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "Order/OrderService.cs")
    call_refs = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [(r.name, r.extra.get("qualified")) for r in call_refs] == [("Other.Real", True)]


def test_using_alias_base_list_resolves_to_the_aliased_qualified_type():
    text = (
        "namespace Acme.Order;\n\n"
        "using Alias = Other.Real;\n\n"
        "public class OrderService : Alias\n{\n}\n"
    )
    res = A.extract_refs(text, "Order/OrderService.cs")
    assert [(r.name, r.extra["via"], r.extra.get("qualified")) for r in res.refs] == [
        ("Other.Real", "extends", True)]


def test_plain_using_import_is_not_treated_as_an_alias():
    text = (
        "namespace Acme.Order;\n\n"
        "using Acme.Data;\n\n"
        "public class OrderService\n{\n"
        "    public OrderService()\n    {\n"
        "        var x = new Data();\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "Order/OrderService.cs")
    call_refs = [r for r in res.refs if r.extra.get("via") == "call"]
    assert [(r.name, r.extra.get("qualified")) for r in call_refs] == [("Data", None)]


def test_inject_attribute_is_not_promoted_to_inject_via():
    """A2＝C# は `[Inject]` 格上げをしない（field_type のまま）。"""
    text = (
        "namespace Acme.Order;\n\n"
        "public class OrderService\n{\n"
        "    [Inject]\n"
        "    private IOrderRepo _repo;\n"
        "}\n"
    )
    res = A.extract_refs(text, "Order/OrderService.cs")
    assert [(r.name, r.extra["via"]) for r in res.refs] == [("IOrderRepo", "field_type")]


# --- base list/宣言型/`new` に直接書かれた完全修飾トークン（`.` を含む）は完全名を保持し qualified 参照になる ---

def test_generic_type_argument_keeps_full_qualification():
    """ジェネリック型引数（`B.Box<C.Dep>` の `C.Dep`）は末尾セグメント（`Dep`）で候補判定するが、
    参照そのものは完全修飾トークンを保持したまま `qualified=True` で返す——単純名（`Dep`）へ
    落とすと、別 namespace の同名型と混同され得る。"""
    text = (
        "namespace Acme.Order;\n\n"
        "public class OrderService\n{\n"
        "    B.Box<C.Dep> field;\n"
        "}\n"
    )
    res = A.extract_refs(text, "Order/OrderService.cs")
    assert {(r.name, r.extra["via"], r.extra.get("qualified")) for r in res.refs} == {
        ("B.Box", "field_type", True),
        ("C.Dep", "field_type", True),
    }


def test_directly_qualified_type_tokens_in_base_list_field_and_new_are_all_qualified():
    """alias を介さず直接 `A.Base`/`B.Dep`/`C.Target` のように完全修飾で書かれたトークンも
    単純名へ落とさず完全名を保持し `qualified=True` で返す（`_resolve_qualified` 経路）。"""
    text = (
        "class Caller : A.Base\n{\n"
        "    B.Dep field;\n"
        "    void M()\n    {\n"
        "        new C.Target();\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "Caller.cs")
    assert {(r.name, r.extra["via"], r.extra.get("qualified")) for r in res.refs} == {
        ("A.Base", "extends", True),
        ("B.Dep", "field_type", True),
        ("C.Target", "call", True),
    }


# --- コメント/文字列リテラル内は無視する ---

def test_base_list_inside_comment_is_ignored():
    text = "namespace Acme.Order;\n\n// public class Fake : Ghost\npublic class Real\n{\n}\n"
    res = A.extract_refs(text, "Real.cs")
    assert res.refs == []
