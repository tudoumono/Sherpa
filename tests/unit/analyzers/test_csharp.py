"""`CSharpAnalyzer` の単体テスト（アナライザ拡張 §4(a)/§9 S7・A1＝本体のみ・FW なし）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.csharp import CSharpAnalyzer

A = CSharpAnalyzer()
NS = "namespace Acme.Order;\n\n"


def test_extensions_and_name():
    assert A.extensions == frozenset({".cs"})
    assert A.name == "csharp"
    assert A.doctype == "csharp"


def test_accepts_all_cs_files_without_content_inspection():
    assert CSharpAnalyzer.accepts is Analyzer.accepts


# ---- primary（public 型・qualified name）・children・Dropped ----
PRIMARY_CASES = {
    "file_scoped_namespace": (NS + "public class OrderService\n{\n}\n", "Order/OrderService.cs",
                              "OrderService", "Acme.Order.OrderService"),
    "block_scoped_namespace": ("namespace Acme.Order\n{\n    public class OrderService\n    {\n    }\n}\n",
                               "Order/OrderService.cs", "OrderService", "Acme.Order.OrderService"),
    "without_namespace_no_prefix": ("public class Standalone\n{\n}\n", "Standalone.cs", "Standalone", "Standalone"),
    "falls_back_to_first_type": ("internal class Helper\n{\n}\n", "Helper.cs", "Helper", None),
    "interface": ("public interface IFoo\n{\n}\n", "IFoo.cs", "IFoo", None),
    "struct": ("public struct Point\n{\n}\n", "Point.cs", "Point", None),
    "enum": ("public enum Color\n{\n}\n", "Color.cs", "Color", None),
    "record": ("public record Money(int V)\n{\n}\n", "Money.cs", "Money", None),
    "record_struct": ("public record struct Point(int X, int Y)\n{\n}\n", "Point.cs", "Point", None),   # 2語目が名前にならない
    "record_class": ("public record class Customer(string Name)\n{\n}\n", "Customer.cs", "Customer", None),
}


@pytest.mark.parametrize("text,path,name,cid_key", PRIMARY_CASES.values(), ids=PRIMARY_CASES)
def test_collect_defs_primary(text, path, name, cid_key):
    p = A.collect_defs(text, path).primary
    assert p is not None and p.label == "Module" and p.name == name
    assert cid_key is None or p.cid_key == cid_key


def test_block_scoped_namespace_class_has_no_children_or_dropped():
    res = A.collect_defs("namespace Acme.Order\n{\n    public class OrderService\n    {\n    }\n}\n", "O.cs")
    assert res.children == [] and res.dropped == []


def test_non_public_sibling_becomes_child_module():
    res = A.collect_defs(NS + "public class OrderService\n{\n}\n\nclass InternalHelper\n{\n}\n", "Order/OrderService.cs")
    assert res.primary.name == "OrderService"
    assert [c.name for c in res.children] == ["InternalHelper"]


def test_nested_type_is_dropped_not_a_child():
    res = A.collect_defs("public class Outer\n{\n    class Inner\n    {\n    }\n}\n", "Outer.cs")
    assert res.children == []
    assert [d.reason for d in res.dropped] == ["nested_type"]


def test_partial_class_is_reported_as_dropped_but_still_has_primary():
    res = A.collect_defs(NS + "public partial class Big\n{\n    void A() {}\n}\n", "Order/Big.Part1.cs")
    assert res.primary is not None and res.primary.name == "Big"
    assert [d.reason for d in res.dropped] == ["cs_partial"]


# ---- 参照（継承/実装は全部 via=extends・宣言型・new・using alias）----
def X(name, via, qualified=None):
    return (name, via, qualified)


def _cls(body, head="public class OrderService"):
    return f"{NS}{head}\n{{\n{body}}}\n"


REFS_CASES = {
    "base_list_all_entries_extends": (
        NS + "public class OrderService : BaseService, IOrderService\n{\n}\n",
        [X("BaseService", "extends"), X("IOrderService", "extends")]),
    "generic_constraint_where_alone_not_base_list": ("public class A<T>\n    where T : B\n{\n}\n", []),
    "enum_underlying_type_not_extends": ("public enum Color : int\n{\n}\n", []),
    "base_list_before_where_extracted": ("public class A : B\n    where T : C\n{\n}\n", [X("B", "extends")]),
    "field_declaration_type": (_cls("    private IOrderRepo _repo;\n"), [X("IOrderRepo", "field_type")]),
    "var_declared_field_excluded": (_cls("    var bar = 1;\n", "public class Foo"), []),
    "new_expression_call": (
        _cls("    public OrderService()\n    {\n        var repo = new OrderRepo();\n    }\n"), [X("OrderRepo", "call")]),
    "using_directive_not_a_reference": (NS + "using Acme.Data;\n\npublic class OrderService\n{\n}\n", []),
    "using_alias_new_resolves_to_qualified": (
        NS + "using Alias = Other.Real;\n\npublic class OrderService\n{\n"
        "    public OrderService()\n    {\n        var x = new Alias();\n    }\n}\n", [X("Other.Real", "call", True)]),
    "using_alias_base_list_resolves_to_qualified": (
        NS + "using Alias = Other.Real;\n\npublic class OrderService : Alias\n{\n}\n", [X("Other.Real", "extends", True)]),
    "plain_using_import_not_alias": (
        NS + "using Acme.Data;\n\npublic class OrderService\n{\n"
        "    public OrderService()\n    {\n        var x = new Data();\n    }\n}\n", [X("Data", "call", None)]),
    "inject_attribute_not_promoted": (_cls("    [Inject]\n    private IOrderRepo _repo;\n"), [X("IOrderRepo", "field_type")]),
    "generic_type_argument_keeps_full_qualification": (
        _cls("    B.Box<C.Dep> field;\n"), [X("B.Box", "field_type", True), X("C.Dep", "field_type", True)]),
    "directly_qualified_tokens_in_base_field_new": (
        "class Caller : A.Base\n{\n    B.Dep field;\n    void M()\n    {\n        new C.Target();\n    }\n}\n",
        [X("A.Base", "extends", True), X("C.Target", "call", True), X("B.Dep", "field_type", True)]),
    "base_list_in_comment_ignored": (NS + "// public class Fake : Ghost\npublic class Real\n{\n}\n", []),
}


@pytest.mark.parametrize("text,refs", REFS_CASES.values(), ids=REFS_CASES)
def test_extract_refs(text, refs):
    got = [(r.name, r.extra["via"], r.extra.get("qualified")) for r in A.extract_refs(text, "Order/OrderService.cs").refs]
    assert got == refs
