"""`JavaAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S・CODE-1d）。"""
from __future__ import annotations

from sherpa.ingest.analyzers.java import JavaAnalyzer

A = JavaAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".java"})
    assert A.name == "java"
    assert A.doctype == "java"


def test_collect_defs_extracts_public_class_as_primary_module():
    text = "public class TaxCalculator {\n    void calc() {}\n}\n"
    res = A.collect_defs(text, "TaxCalculator.java")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "TaxCalculator"
    assert res.children == [] and res.dropped == []


def test_collect_defs_uses_package_qualified_name_and_simple_display_name():
    text = "package com.acme.tax;\n\npublic class TaxCalculator {\n}\n"
    res = A.collect_defs(text, "com/acme/tax/TaxCalculator.java")
    assert res.primary.name == "TaxCalculator"                       # 表示名は単純名
    assert res.primary.cid_key == "com.acme.tax.TaxCalculator"       # cid_key はパッケージ修飾名
    assert res.primary.extra["qualified_name"] == "com.acme.tax.TaxCalculator"


def test_collect_defs_extracts_non_public_sibling_as_child_module():
    text = (
        "public class TaxCalculator {\n"
        "    RoundingHelper h;\n"
        "}\n"
        "\n"
        "class RoundingHelper {\n"
        "    int round(int v) { return v; }\n"
        "}\n"
    )
    res = A.collect_defs(text, "TaxCalculator.java")
    assert res.primary.name == "TaxCalculator"
    assert [c.label for c in res.children] == ["Module"]
    assert [c.name for c in res.children] == ["RoundingHelper"]


def test_collect_defs_falls_back_to_first_type_when_no_public_type_present():
    """public 型が1つも無いファイルでも黙って消さない——最初の型宣言を primary に採る
    （CODE-1d の実装判断・docs/proposals/2026-08-29-コード解析層のコンポーネント化.md の
    CODE-1d 節に報告）。"""
    text = "class PackagePrivateOnly {\n}\n"
    res = A.collect_defs(text, "PackagePrivateOnly.java")
    assert res.primary is not None
    assert res.primary.name == "PackagePrivateOnly"
    assert res.children == []


def test_collect_defs_flags_nested_inner_class_as_dropped_not_a_child():
    text = (
        "public class Outer {\n"
        "    class Inner {\n"
        "    }\n"
        "}\n"
    )
    res = A.collect_defs(text, "Outer.java")
    assert res.primary.name == "Outer"
    assert res.children == []                                       # Inner はノード化しない
    assert len(res.dropped) == 1 and res.dropped[0].reason == "nested_type"
    assert "Inner" in res.dropped[0].snippet


def test_collect_defs_returns_no_primary_when_file_has_no_type_declaration():
    text = "package com.acme;\n"
    res = A.collect_defs(text, "package-info.java")
    assert res.primary is None and res.children == [] and res.dropped == []


def test_collect_defs_records_imports_on_primary_extra():
    text = (
        "package com.acme.billing;\n"
        "\n"
        "import com.acme.tax.TaxCalculator;\n"
        "import java.util.List;\n"
        "\n"
        "public class InvoiceService {\n"
        "}\n"
    )
    res = A.collect_defs(text, "com/acme/billing/InvoiceService.java")
    assert res.primary.extra["imports"] == ["com.acme.tax.TaxCalculator", "java.util.List"]


def test_extract_refs_finds_new_call():
    text = "public class A {\n    void m() { Object x = new Helper(); }\n}\n"
    res = A.extract_refs(text, "A.java")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert ("INVOKES", "Module", "Helper") in kinds


def test_extract_refs_strips_generics_from_new_call():
    text = "public class A {\n    void m() { Object x = new ArrayList<String>(); }\n}\n"
    res = A.extract_refs(text, "A.java")
    assert {r.name for r in res.refs} == {"ArrayList"}


def test_extract_refs_strips_package_qualification_from_new_call():
    text = "public class A {\n    void m() { Object x = new com.acme.tax.TaxCalculator(); }\n}\n"
    res = A.extract_refs(text, "A.java")
    assert {r.name for r in res.refs} == {"TaxCalculator"}


def test_extract_refs_finds_static_call_by_uppercase_qualifier_convention():
    text = "public class A {\n    void m() { double r = TaxCalculator.staticRate(); } }\n"
    res = A.extract_refs(text, "A.java")
    assert {r.name for r in res.refs} == {"TaxCalculator"}


def test_extract_refs_does_not_treat_lowercase_qualifier_as_static_call():
    """変数（小文字始まり）の呼び出しは静的呼び出し候補にしない（命名慣習ヒューリスティック）。"""
    text = "public class A {\n    void m() { calc.hashCode(); } }\n"
    res = A.extract_refs(text, "A.java")
    assert res.refs == []


def test_extract_refs_finds_extends_and_implements():
    text = "public class TaxCalculator extends AbstractCalculator implements Taxable, Comparable {\n}\n"
    res = A.extract_refs(text, "TaxCalculator.java")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert ("INVOKES", "Module", "AbstractCalculator") in kinds
    assert ("INVOKES", "Module", "Taxable") in kinds
    assert ("INVOKES", "Module", "Comparable") in kinds


def test_extract_refs_ignores_call_like_syntax_in_line_comment():
    text = "public class A {\n    // new FakeIgnored();\n}\n"
    res = A.extract_refs(text, "A.java")
    assert res.refs == []


def test_extract_refs_ignores_call_like_syntax_in_block_comment():
    text = "public class A {\n    /* new FakeIgnored();\n       TaxCalculator.fake(); */\n}\n"
    res = A.extract_refs(text, "A.java")
    assert res.refs == []


def test_extract_refs_ignores_call_like_syntax_in_string_literal():
    text = 'public class A {\n    String s = "new FakeIgnored(); TaxCalculator.fake()";\n}\n'
    res = A.extract_refs(text, "A.java")
    assert res.refs == []


def test_extract_refs_line_numbers_are_one_based_and_match_source():
    text = "public class A {\n    void m() { new Helper(); }\n}\n"
    res = A.extract_refs(text, "A.java")
    assert res.refs and res.refs[0].line == 2


def test_extract_refs_new_call_and_extends_and_implements_carry_via_in_extra():
    """JAVA-2: 既存の抽出（new/静的呼び出し・extends・implements）は `extra["via"]` を持つように
    なる（docs/05 §2 一般化のエッジ属性・CODE-2）。既存の edge_type/kind/name の契約は変えない。"""
    text = "public class A extends Base implements Iface {\n    void m() { new Helper(); }\n}\n"
    res = A.extract_refs(text, "A.java")
    via_by_name = {r.name: r.extra.get("via") for r in res.refs}
    assert via_by_name["Base"] == "extends"
    assert via_by_name["Iface"] == "implements"
    assert via_by_name["Helper"] == "call"


# --- JAVA-2: 宣言型参照（フィールド/コンストラクタ引数/メソッド引数）の一般抽出 ---

def test_extract_refs_field_declaration_type_becomes_field_type_reference():
    text = "public class A {\n    private Engine engine;\n}\n"
    res = A.extract_refs(text, "A.java")
    kinds = {(r.edge_type, r.kind, r.name, r.extra.get("via")) for r in res.refs}
    assert ("INVOKES", "Module", "Engine", "field_type") in kinds


def test_extract_refs_jdk_common_type_field_is_not_extracted():
    text = "public class A {\n    private String label;\n    private java.util.List raw;\n}\n"
    res = A.extract_refs(text, "A.java")
    assert res.refs == []


def test_extract_refs_local_variable_inside_method_body_is_not_extracted():
    """フィールド/引数のみが対象——メソッド本体内のローカル変数（brace 深度2以上）は対象外。"""
    text = "public class A {\n    void m() {\n        Engine engine = new Engine();\n    }\n}\n"
    res = A.extract_refs(text, "A.java")
    # `new Engine()`（call）は既存どおり拾うが、ローカル変数宣言型としての field_type は増えない。
    kinds = [(r.name, r.extra.get("via")) for r in res.refs]
    assert kinds == [("Engine", "call")]


def test_extract_refs_constructor_and_method_parameter_types_become_field_type_references():
    text = (
        "public class A {\n"
        "    public A(Engine engine, int retries) {\n"
        "    }\n"
        "\n"
        "    public void process(TaxCalc calc, String note) {\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    kinds = {(r.name, r.extra.get("via")) for r in res.refs}
    assert ("Engine", "field_type") in kinds
    assert ("TaxCalc", "field_type") in kinds
    assert not any(n in ("retries", "note", "String", "int") for n, _via in kinds)


def test_extract_refs_generic_type_argument_is_extracted_one_level_deep():
    text = "public class A {\n    private List<TaxCalc> calcs;\n}\n"
    res = A.extract_refs(text, "A.java")
    kinds = {(r.name, r.extra.get("via")) for r in res.refs}
    # 外側の List は JDK 型なので候補にしない。型引数 TaxCalc だけを1段拾う。
    assert kinds == {("TaxCalc", "field_type")}


def test_extract_refs_nested_generic_argument_is_not_extracted_two_levels_deep():
    """ネストしたジェネリクスは1段目までしか拾わない（2段目は誤検出しない側の見逃し）。"""
    text = "public class A {\n    private Map<String, List<TaxCalc>> byKey;\n}\n"
    res = A.extract_refs(text, "A.java")
    assert res.refs == []


def test_extract_refs_autowired_field_is_upgraded_to_via_inject():
    text = "public class A {\n    @Autowired\n    private Engine engine;\n}\n"
    res = A.extract_refs(text, "A.java")
    kinds = {(r.name, r.extra.get("via")) for r in res.refs}
    assert kinds == {("Engine", "inject")}


def test_extract_refs_inject_and_resource_annotations_also_upgrade_to_via_inject():
    for anno in ("@Inject", "@Resource"):
        text = f"public class A {{\n    {anno}\n    private Engine engine;\n}}\n"
        res = A.extract_refs(text, "A.java")
        assert {(r.name, r.extra.get("via")) for r in res.refs} == {("Engine", "inject")}


def test_extract_refs_plain_field_without_annotation_stays_via_field_type():
    """フレームワーク非依存の核——アノテーションが無くても同じ宣言型は field_type で拾う
    （検出手段ではなく分類の改善のみがアノテーションの役割・裁定2026-09-03）。"""
    text = "public class A {\n    private Engine engine;\n}\n"
    res = A.extract_refs(text, "A.java")
    assert {(r.name, r.extra.get("via")) for r in res.refs} == {("Engine", "field_type")}


def test_extract_refs_inject_annotation_does_not_leak_to_the_next_unrelated_field():
    """「直前」の判定——注釈と対象フィールドの間に他の行を挟んだら pending は持ち越さない。"""
    text = (
        "public class A {\n"
        "    @Autowired\n"
        "    private Engine engine;\n"
        "    private TaxCalc calc;\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    via_by_name = {r.name: r.extra.get("via") for r in res.refs}
    assert via_by_name["Engine"] == "inject"
    assert via_by_name["TaxCalc"] == "field_type"
