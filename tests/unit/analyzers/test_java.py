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


# --- URL キー定義側（波3 統合・Spring MVC マッピング注釈→ `Config` children） ---

def _config_children(res):
    return [(c.label, c.name, c.cid_key, c.extra.get("key_kind")) for c in res.children]


def test_collect_defs_class_and_method_mapping_annotations_join_into_url_config_child():
    text = (
        "package com.acme;\n"
        "\n"
        "@RestController\n"
        "@RequestMapping(\"/orders\")\n"
        "public class OrderController {\n"
        "\n"
        "    @GetMapping(\"/list\")\n"
        "    public String list() {\n"
        "        return \"orders\";\n"
        "    }\n"
        "}\n"
    )
    res = A.collect_defs(text, "com/acme/OrderController.java")
    assert _config_children(res) == [("Config", "/orders/list", "key:url:/orders/list", "url")]


def test_collect_defs_method_mapping_without_class_prefix_uses_bare_path():
    text = (
        "public class HealthController {\n"
        "    @GetMapping(\"/health\")\n"
        "    public String health() { return \"ok\"; }\n"
        "}\n"
    )
    res = A.collect_defs(text, "HealthController.java")
    assert _config_children(res) == [("Config", "/health", "key:url:/health", "url")]


def test_collect_defs_mapping_value_attribute_form_is_supported():
    text = (
        "@RequestMapping(\"/orders\")\n"
        "public class OrderController {\n"
        "    @PostMapping(value = \"/create\")\n"
        "    public void create() {}\n"
        "}\n"
    )
    res = A.collect_defs(text, "OrderController.java")
    assert _config_children(res) == [("Config", "/orders/create", "key:url:/orders/create", "url")]


def test_collect_defs_mapping_path_attribute_form_is_supported():
    text = (
        "@RequestMapping(\"/orders\")\n"
        "public class OrderController {\n"
        "    @PutMapping(path = \"/update\")\n"
        "    public void update() {}\n"
        "}\n"
    )
    res = A.collect_defs(text, "OrderController.java")
    assert _config_children(res) == [("Config", "/orders/update", "key:url:/orders/update", "url")]


def test_collect_defs_mapping_array_form_yields_one_child_per_path():
    text = (
        "@RequestMapping(\"/orders\")\n"
        "public class OrderController {\n"
        "    @GetMapping({\"/list\", \"/all\"})\n"
        "    public String list() { return \"orders\"; }\n"
        "}\n"
    )
    res = A.collect_defs(text, "OrderController.java")
    assert _config_children(res) == [
        ("Config", "/orders/list", "key:url:/orders/list", "url"),
        ("Config", "/orders/all", "key:url:/orders/all", "url"),
    ]


def test_collect_defs_class_level_mapping_array_is_cross_joined_with_method_path():
    """クラスレベル `@RequestMapping` が配列（複数 prefix）の場合、各 prefix と
    メソッドレベルパスの直積を1本ずつ返す（`{"/v1","/v2"}`×`/x` → `/v1/x`・`/v2/x`）。"""
    text = (
        "@RequestMapping({\"/v1\", \"/v2\"})\n"
        "public class OrderController {\n"
        "    @GetMapping(\"/x\")\n"
        "    public String x() { return \"x\"; }\n"
        "}\n"
    )
    res = A.collect_defs(text, "OrderController.java")
    assert _config_children(res) == [
        ("Config", "/v1/x", "key:url:/v1/x", "url"),
        ("Config", "/v2/x", "key:url:/v2/x", "url"),
    ]


def test_collect_defs_delete_and_patch_mapping_are_also_recognized():
    text = (
        "@RequestMapping(\"/orders\")\n"
        "public class OrderController {\n"
        "    @DeleteMapping(\"/remove\")\n"
        "    public void remove() {}\n"
        "\n"
        "    @PatchMapping(\"/patch\")\n"
        "    public void patch() {}\n"
        "}\n"
    )
    res = A.collect_defs(text, "OrderController.java")
    assert _config_children(res) == [
        ("Config", "/orders/remove", "key:url:/orders/remove", "url"),
        ("Config", "/orders/patch", "key:url:/orders/patch", "url"),
    ]


def test_collect_defs_class_without_method_mapping_yields_no_url_config_child():
    """クラスレベル `@RequestMapping` だけでメソッドレベルのマッピング注釈が無ければ
    `Config` children は作らない（本スライスのスコープ＝メソッドレベルとの連結のみ）。"""
    text = (
        "@RequestMapping(\"/orders\")\n"
        "public class OrderController {\n"
        "    public void helper() {}\n"
        "}\n"
    )
    res = A.collect_defs(text, "OrderController.java")
    assert _config_children(res) == []


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


# --- S3'（A7 案B）: 設定キー参照（`@Value`/`getProperty`/`getString`）---

def _config_refs(res):
    return [(r.edge_type, r.kind, r.name, r.extra.get("via")) for r in res.refs
            if r.extra.get("via") == "config_key"]


def test_extract_refs_value_annotation_yields_config_key_reference():
    text = 'public class A {\n    @Value("${tax.rate}")\n    private String rate;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "tax.rate", "config_key")]


def test_extract_refs_value_annotation_default_part_is_discarded():
    text = 'public class A {\n    @Value("${tax.rate:0.1}")\n    private String rate;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "tax.rate", "config_key")]


def test_extract_refs_get_property_with_and_without_receiver():
    text = (
        "public class A {\n"
        "    void m() {\n"
        '        System.getProperty("db.url");\n'
        '        env.getProperty("db.user");\n'
        '        getProperty("db.pass");\n'
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    keys = {r.name for r in res.refs if r.extra.get("via") == "config_key"}
    assert keys == {"db.url", "db.user", "db.pass"}


def test_extract_refs_get_string_yields_config_key_reference():
    text = 'public class A {\n    void m() { bundle.getString("app.title"); }\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "app.title", "config_key")]


def test_extract_refs_configuration_properties_prefix_is_dropped_not_connected():
    text = '@ConfigurationProperties(prefix="myapp")\npublic class A {\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_prefix", "myapp")]


def test_extract_refs_same_config_key_appearing_twice_yields_one_reference_at_first_line():
    text = (
        "public class A {\n"
        '    @Value("${tax.rate}")\n'
        "    private String a;\n"
        '    @Value("${tax.rate}")\n'
        "    private String b;\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    matches = [r for r in res.refs if r.extra.get("via") == "config_key"]
    assert len(matches) == 1
    assert matches[0].line == 2


def test_extract_refs_config_key_inside_comment_is_not_extracted():
    text = (
        "public class A {\n"
        '    // @Value("${should.not.match}")\n'
        "    void m() {}\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []


def test_extract_refs_value_annotation_with_multiple_placeholders_yields_all_keys():
    """`@Value("${a}-${b}")` のように1つの文字列に複数の `${key}` が現れる場合は分解できる限り
    全部抽出する（黙って1個も返さない、という退化を避ける）。"""
    text = 'public class A {\n    @Value("${host}:${port}")\n    private String addr;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert sorted(name for _edge_type, _kind, name, _via in _config_refs(res)) == ["host", "port"]


def test_extract_refs_value_annotation_spel_expression_is_dropped_not_silently_lost():
    text = 'public class A {\n    @Value("#{someBean.someProperty}")\n    private String v;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_spel", "someBean.someProperty")]


def test_extract_refs_get_property_with_nonliteral_argument_is_dropped_not_silently_lost():
    text = (
        "public class A {\n"
        "    void m() {\n"
        "        System.getProperty(KEY_CONST);\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_nonliteral", "KEY_CONST")]


def test_extract_refs_get_string_with_nonliteral_argument_is_dropped_not_silently_lost():
    text = "public class A {\n    void m() { bundle.getString(var); }\n}\n"
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_nonliteral", "var")]


def test_extract_refs_get_property_method_declaration_is_not_flagged_or_extracted():
    """`String getProperty(String key) { ... }` のようなメソッド**宣言**は呼び出しではないため、
    `config_nonliteral` を誤って申告しない。呼び出し `getProperty(KEY)` は
    引き続き申告される。"""
    text = (
        "public class A {\n"
        "    String getProperty(String key) { return key; }\n"
        "    void m() {\n"
        "        getProperty(KEY);\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_nonliteral", "KEY")]


def test_extract_refs_get_property_multi_param_declaration_is_not_flagged():
    """複数引数の宣言（`String getProperty(String key, String fallback)`）は宣言と判定される
    ——単一引数限定の判定では見逃していた形。"""
    text = (
        "public class A {\n"
        "    String getProperty(String key, String fallback) { return key; }\n"
        "    void m() {\n"
        "        getProperty(KEY);\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_nonliteral", "KEY")]


def test_extract_refs_get_property_annotated_param_declaration_is_not_flagged():
    """注釈付き引数（`@Nonnull String key`）の宣言も、引数の形を問わず宣言と判定される。"""
    text = "public class A {\n    String getProperty(@Nonnull String key) { return key; }\n}\n"
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert res.dropped == []


def test_extract_refs_get_property_varargs_declaration_is_not_flagged():
    """varargs（`String... keys`）の宣言も宣言と判定される。"""
    text = "public class A {\n    String getProperty(String... keys) { return keys[0]; }\n}\n"
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert res.dropped == []


def test_extract_refs_get_string_receiver_call_with_nonliteral_arg_still_flagged():
    """レシーバ付き呼び出し（`cfg.getString(name)`）は宣言ではあり得ないため、引き続き
    `config_nonliteral` を申告する。"""
    text = "public class A {\n    void m() { cfg.getString(name); }\n}\n"
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_nonliteral", "name")]


def test_extract_refs_config_key_inside_text_block_is_not_extracted():
    """JAVA text block（三連続の二重引用符）の本文は空白化する——本文中に書かれた
    `getProperty("...")` のような記述を実在の参照として拾わない。"""
    text = (
        'public class A {\n'
        '    String doc = """\n'
        '        example: getProperty("should.not.match")\n'
        '        """;\n'
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []


def test_extract_refs_config_key_does_not_interfere_with_declared_type_refs():
    """既存の宣言型参照（extends/implements/field_type/inject/call）は挙動不変。"""
    text = (
        "public class A extends Base {\n"
        "    private Engine engine;\n"
        '    @Value("${tax.rate}")\n'
        "    private String rate;\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    non_config = {(r.name, r.extra.get("via")) for r in res.refs if r.extra.get("via") != "config_key"}
    assert non_config == {("Base", "extends"), ("Engine", "field_type")}


def test_return_get_property_is_a_call_not_a_declaration():
    """`return getProperty(...)` は `return` が戻り値型に見えるが呼び出し＝リテラルは参照・定数は申告。"""
    src = (
        "class A {\n"
        "    String m() {\n"
        "        return getProperty(\"app.key\");\n"
        "    }\n"
        "    String n() {\n"
        "        return getProperty(KEY);\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(src, "A.java")
    keys = [r.name for r in res.refs if r.extra.get("via") == "config_key"]
    assert keys == ["app.key"]
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_nonliteral", "KEY")]


# --- S3' 追補: `getBean`/`@Qualifier`/`@Named`/`@Resource(name=...)` ---

def test_extract_refs_get_bean_yields_config_key_reference():
    text = 'public class A {\n    Object m() { return getBean("orderService"); }\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "orderService", "config_key")]


def test_extract_refs_get_bean_with_receiver_and_nonliteral_argument_is_dropped():
    text = (
        "public class A {\n"
        "    void m() {\n"
        '        ctx.getBean("orderService");\n'
        "        ctx.getBean(SERVICE_NAME);\n"
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    assert {r.name for r in res.refs if r.extra.get("via") == "config_key"} == {"orderService"}
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_nonliteral", "SERVICE_NAME")]


def test_extract_refs_qualifier_annotation_yields_config_key_reference():
    text = 'public class A {\n    @Qualifier("orderService")\n    private String hint;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "orderService", "config_key")]


def test_extract_refs_named_annotation_yields_config_key_reference():
    text = 'public class A {\n    @Named("orderService")\n    private String hint;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "orderService", "config_key")]


def test_extract_refs_qualifier_annotation_value_attribute_form_yields_config_key_reference():
    """`@Qualifier(value="k")`（`value=` 属性名付き形）も `@Qualifier("k")` と同じ扱い。"""
    text = 'public class A {\n    @Qualifier(value = "orderService")\n    private String hint;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "orderService", "config_key")]


def test_extract_refs_named_annotation_value_attribute_form_yields_config_key_reference():
    """`@Named(value="k")`（`value=` 属性名付き形）も `@Named("k")` と同じ扱い。"""
    text = 'public class A {\n    @Named(value = "orderService")\n    private String hint;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "orderService", "config_key")]


def test_extract_refs_resource_name_attribute_yields_config_key_reference():
    text = 'public class A {\n    @Resource(name = "mailer")\n    private String mailer;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "mailer", "config_key")]


def test_extract_refs_resource_without_name_attribute_is_not_a_config_key_reference():
    """`@Resource` 単独（`name=` 無し）は既存の DI 注釈による `via=inject` 格上げ専用のまま
    ——本抽出（設定キー参照）とは別軸で、キーを持たないため対象外。"""
    text = 'public class A {\n    @Resource\n    private Engine engine;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert {r.name: r.extra.get("via") for r in res.refs} == {"Engine": "inject"}


def test_extract_refs_autowired_alone_has_no_config_key_reference():
    """`@Autowired` は文字列引数を持たない注釈であり設定キー抽出の対象外
    （既存の `via=inject` 格上げのみ・本抽出とは別軸）。"""
    text = 'public class A {\n    @Autowired\n    private Engine engine;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []


def test_extract_refs_qualifier_with_nonliteral_argument_is_silently_not_extracted():
    """`@Qualifier`/`@Named`/`@Resource(name=...)` は非リテラル形を検知する構文を持たない——
    `@Value` の非リテラル値と同様、文字列リテラル形に一致しなければ黙って対象外にするだけ
    （`config_nonliteral` は申告しない・既存の @Value の扱いに揃える設計判断）。"""
    text = 'public class A {\n    @Qualifier(BeanNames.ORDER_SERVICE)\n    private String hint;\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [d.reason for d in res.dropped] == []


# --- Config キーの種別（`extra["key_kind"]`・名前空間分離＝裁定2026-09-06） ---

def test_extract_refs_value_annotation_key_kind_is_property():
    text = 'public class A {\n    @Value("${tax.rate}")\n    private String rate;\n}\n'
    res = A.extract_refs(text, "A.java")
    keys = {(r.name, r.extra.get("key_kind")) for r in res.refs if r.extra.get("via") == "config_key"}
    assert keys == {("tax.rate", "property")}


def test_extract_refs_get_property_and_get_string_key_kind_is_property():
    text = (
        'public class A {\n'
        '    void m() {\n'
        '        System.getProperty("db.url");\n'
        '        bundle.getString("app.title");\n'
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    keys = {(r.name, r.extra.get("key_kind")) for r in res.refs if r.extra.get("via") == "config_key"}
    assert keys == {("db.url", "property"), ("app.title", "property")}


def test_extract_refs_get_bean_and_qualifier_and_named_and_resource_key_kind_is_bean():
    text = (
        "public class A {\n"
        '    @Qualifier("qualifierHint")\n'
        "    private String a;\n"
        '    @Named("namedHint")\n'
        "    private String b;\n"
        '    @Resource(name = "mailer")\n'
        "    private String c;\n"
        "    Object m() {\n"
        '        return getBean("orderService");\n'
        "    }\n"
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    keys = {(r.name, r.extra.get("key_kind")) for r in res.refs if r.extra.get("via") == "config_key"}
    assert keys == {
        ("qualifierHint", "bean"), ("namedHint", "bean"), ("mailer", "bean"), ("orderService", "bean"),
    }


# --- 呼び出し候補が通常の文字列/char リテラル内にある記述は走査しない ---

def test_extract_refs_get_bean_call_inside_a_string_literal_is_not_extracted():
    """`getBean(...)` という記述が通常の文字列リテラルの**中身**に書かれているだけの場合
    （実際の呼び出しではない）、参照にも `Dropped` にもしない（黙って除外するだけ・申告なし）。"""
    text = 'public class A {\n    String example = "getBean(k)";\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [d.reason for d in res.dropped] == []


def test_extract_refs_get_property_and_get_string_call_inside_a_string_literal_is_not_extracted():
    text = (
        "public class A {\n"
        '    String a = "getProperty(k)";\n'
        '    String b = "getString(k)";\n'
        "}\n"
    )
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == []
    assert [d.reason for d in res.dropped] == []


def test_extract_refs_get_bean_call_outside_a_string_literal_is_still_extracted_on_the_same_line():
    """文字列リテラル内の記述を除外しても、同じ行の**実際の呼び出し**まで巻き込まない
    （文字列リテラル内だけを個別に位置チェックしている・行単位の一括除外ではない）。"""
    text = 'public class A {\n    Object m() { log("getBean(k)"); return getBean("orderService"); }\n}\n'
    res = A.extract_refs(text, "A.java")
    assert _config_refs(res) == [("ACCESSES", "Config", "orderService", "config_key")]
