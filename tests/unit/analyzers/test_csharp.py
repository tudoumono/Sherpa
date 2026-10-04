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
    "using_alias_new_keeps_the_alias_for_the_resolver": (
        NS + "using Alias = Other.Real;\n\npublic class OrderService\n{\n"
        "    public OrderService()\n    {\n        var x = new Alias();\n    }\n}\n", [X("Alias", "call", None)]),
    "using_alias_base_list_keeps_the_alias_for_the_resolver": (
        NS + "using Alias = Other.Real;\n\npublic class OrderService : Alias\n{\n}\n", [X("Alias", "extends", None)]),
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


# ---- Tree-sitter の木で読むようになった形 ----
def test_multiline_signatures_nested_generics_and_type_parameters():
    text = ("public class Svc<TItem>\n{\n"
            "    public Svc(\n        IRepo repo,\n        Logger<Svc<Dep>> log) { }\n"
            "    public TItem Pick<TOut>(TItem a, TOut b, Other c) { return new TOut(); }\n"
            "    private Dictionary<string, List<Leaf>> map;\n"
            "    private (First a, Second b) pair;\n}\n")
    got = [(r.name, r.line) for r in A.extract_refs(text, "Svc.cs").refs]
    assert got == [("IRepo", 4), ("Logger", 5), ("Svc", 5), ("Dep", 5), ("Other", 6), ("Leaf", 7), ("First", 8), ("Second", 8)]


def test_same_line_type_bodies_are_read():
    refs = A.extract_refs("namespace One { class A1 { Foo f; } }\n", "A1.cs").refs
    assert [(r.name, r.line) for r in refs] == [("Foo", 1)]


def test_preprocessor_branch_around_a_class_header_reads_both_base_lists():
    text = "#if NET6\npublic class A : B1\n#else\npublic class A : B2\n#endif\n{\n    private Dep d;\n}\n"
    res = A.extract_refs(text, "A.cs")
    assert [(r.name, r.extra["via"]) for r in res.refs] == [("B1", "extends"), ("B2", "extends"), ("Dep", "field_type")]
    assert any(d.reason == "syntax_error" for d in A.collect_defs(text, "A.cs").dropped)


def test_syntax_error_is_reported_and_the_rest_of_the_file_is_still_read():
    text = ("public class Broken : Base\n{\n    private Dep2 d2;\n    public void M( { }\n}\n"
            "class AfterBroken : Base3\n{\n    private Dep3 d3;\n}\n")
    defs = A.collect_defs(text, "Broken.cs")
    assert [d.reason for d in defs.dropped] == ["syntax_error"] and defs.dropped[0].line == 4
    assert defs.primary.name == "Broken" and [c.name for c in defs.children] == ["AfterBroken"]
    refs = A.extract_refs(text, "Broken.cs").refs
    assert [(r.name, r.source_symbol_id) for r in refs if r.name in ("Dep2", "Dep3")] == [
        ("Dep2", None), ("Dep3", ("Broken.cs", "AfterBroken"))]


def test_return_types_property_event_indexer_and_primary_constructor_types_are_read():
    text = ("public record Money(Currency C, int V);\n"
            "public class Svc<T>\n{\n"
            "    public Result Run(Req r) { return null; }\n"
            "    public T Echo(T t) { return t; }\n"
            "    public Task<Item> LoadAsync() { return null; }\n"
            "    public static explicit operator Wrapped(Svc<T> s) => null;\n"
            "    public event Handler<Ev> Changed;\n"
            "    public Foo this[Bar idx] { get { return null; } }\n"
            "    public Mapped Mapping { get; }\n"
            "    void Body() { Local x = null; }\n}\n")
    got = [(r.name, r.line) for r in A.extract_refs(text, "Svc.cs").refs]
    assert got == [("Currency", 1), ("Result", 4), ("Req", 4), ("Item", 6), ("Wrapped", 7), ("Svc", 7), ("Handler", 8),
                   ("Ev", 8), ("Foo", 9), ("Bar", 9), ("Mapped", 10)]


def test_nested_type_arguments_in_base_list_new_and_casts_are_read():
    text = ("public class C : I<A<B>>\n{\n    void M(object o)\n    {\n        var f = new Factory<X<Y>>();\n"
            "        var c = (Cast<Z>)o;\n    }\n}\n")
    got = [(r.name, r.extra["via"]) for r in A.extract_refs(text, "C.cs").refs]
    assert got == [("I", "extends"), ("A", "field_type"), ("B", "field_type"), ("Factory", "call"),
                   ("X", "field_type"), ("Y", "field_type"), ("Cast", "field_type"), ("Z", "field_type")]


def test_typeof_in_an_attribute_belongs_to_the_type_it_decorates():
    text = ("namespace N;\npublic class First\n{\n}\n"
            "[Uses(typeof(Dep))]\nclass Second\n{\n}\n")
    refs = A.extract_refs(text, "T.cs")
    assert [(r.name, r.source_symbol_id) for r in refs.refs] == [("Dep", ("T.cs", "N.Second"))]
