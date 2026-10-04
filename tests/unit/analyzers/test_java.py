"""`JavaAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S・CODE-1d）。

入力ソース片 → (参照, Dropped の理由) の表で確かめる。
"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers.java import JavaAnalyzer

A = JavaAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".java"})
    assert A.name == "java"
    assert A.doctype == "java"


# ---- collect_defs: 型定義 ----

def test_collect_defs_extracts_public_class_as_primary_module():
    res = A.collect_defs("public class TaxCalculator {\n    void calc() {}\n}\n", "TaxCalculator.java")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "TaxCalculator"
    assert res.children == [] and res.dropped == []


def test_collect_defs_uses_package_qualified_name_and_simple_display_name():
    res = A.collect_defs("package com.acme.tax;\n\npublic class TaxCalculator {\n}\n",
                         "com/acme/tax/TaxCalculator.java")
    assert res.primary.name == "TaxCalculator"                       # 表示名は単純名
    assert res.primary.cid_key == "com.acme.tax.TaxCalculator"       # cid_key はパッケージ修飾名
    assert res.primary.extra["qualified_name"] == "com.acme.tax.TaxCalculator"


def test_collect_defs_extracts_non_public_sibling_as_child_module():
    text = ("public class TaxCalculator {\n    RoundingHelper h;\n}\n\n"
            "class RoundingHelper {\n    int round(int v) { return v; }\n}\n")
    res = A.collect_defs(text, "TaxCalculator.java")
    assert res.primary.name == "TaxCalculator"
    assert [(c.label, c.name) for c in res.children] == [("Module", "RoundingHelper")]


def test_collect_defs_falls_back_to_first_type_when_no_public_type_present():
    """public 型が1つも無いファイルでも黙って消さず、最初の型宣言を primary に採る。"""
    res = A.collect_defs("class PackagePrivateOnly {\n}\n", "PackagePrivateOnly.java")
    assert res.primary is not None and res.primary.name == "PackagePrivateOnly"
    assert res.children == []


def test_collect_defs_flags_nested_inner_class_as_dropped_not_a_child():
    res = A.collect_defs("public class Outer {\n    class Inner {\n    }\n}\n", "Outer.java")
    assert res.primary.name == "Outer"
    assert res.children == []
    assert len(res.dropped) == 1 and res.dropped[0].reason == "nested_type"
    assert "Inner" in res.dropped[0].snippet


def test_collect_defs_returns_no_primary_when_file_has_no_type_declaration():
    res = A.collect_defs("package com.acme;\n", "package-info.java")
    assert res.primary is None and res.children == [] and res.dropped == []


def test_collect_defs_records_imports_on_primary_extra():
    text = ("package com.acme.billing;\n\nimport com.acme.tax.TaxCalculator;\nimport java.util.List;\n\n"
            "public class InvoiceService {\n}\n")
    res = A.collect_defs(text, "com/acme/billing/InvoiceService.java")
    assert res.primary.extra["imports"] == ["com.acme.tax.TaxCalculator", "java.util.List"]


# ---- collect_defs: URL キー（Spring MVC マッピング注釈 → Config children） ----

def U(path):
    return ("Config", path, f"key:url:{path}", "url")


URL_CASES = {
    "class_and_method_join": (
        '@RestController\n@RequestMapping("/orders")\npublic class OrderController {\n\n'
        '    @GetMapping("/list")\n    public String list() {\n        return "orders";\n    }\n}\n',
        [U("/orders/list")]),
    "method_without_class_prefix": (
        'public class HealthController {\n    @GetMapping("/health")\n    public String health() { return "ok"; }\n}\n',
        [U("/health")]),
    "value_attribute": (
        '@RequestMapping("/orders")\npublic class OrderController {\n'
        '    @PostMapping(value = "/create")\n    public void create() {}\n}\n', [U("/orders/create")]),
    "path_attribute": (
        '@RequestMapping("/orders")\npublic class OrderController {\n'
        '    @PutMapping(path = "/update")\n    public void update() {}\n}\n', [U("/orders/update")]),
    "array_one_child_per_path": (
        '@RequestMapping("/orders")\npublic class OrderController {\n'
        '    @GetMapping({"/list", "/all"})\n    public String list() { return "orders"; }\n}\n',
        [U("/orders/list"), U("/orders/all")]),
    "class_level_array_cross_joined": (
        '@RequestMapping({"/v1", "/v2"})\npublic class OrderController {\n'
        '    @GetMapping("/x")\n    public String x() { return "x"; }\n}\n', [U("/v1/x"), U("/v2/x")]),
    "delete_and_patch": (
        '@RequestMapping("/orders")\npublic class OrderController {\n'
        '    @DeleteMapping("/remove")\n    public void remove() {}\n\n'
        '    @PatchMapping("/patch")\n    public void patch() {}\n}\n',
        [U("/orders/remove"), U("/orders/patch")]),
    "class_mapping_only_yields_none": (
        '@RequestMapping("/orders")\npublic class OrderController {\n    public void helper() {}\n}\n', []),
}


@pytest.mark.parametrize("text,children", URL_CASES.values(), ids=URL_CASES)
def test_collect_defs_url_config_children(text, children):
    res = A.collect_defs(text, "OrderController.java")
    assert [(c.label, c.name, c.cid_key, c.extra.get("key_kind")) for c in res.children] == children


# ---- extract_refs ----

def R(name, via):
    return ("INVOKES", "Module", name, via, None)


def C(name, kind="property"):
    return ("ACCESSES", "Config", name, "config_key", kind)


def D(reason, snippet):
    return (reason, snippet)


def _body(s):
    return f"public class A {{\n{s}\n}}\n"


# 入力 → (参照（順不同）, Dropped[(reason, snippet)]。None は検査しない)
REFS_CASES = {
    "new_call": (_body("    void m() { Object x = new Helper(); }"), [R("Helper", "call")], None),
    "new_strips_generics": (_body("    void m() { Object x = new ArrayList<String>(); }"), [R("ArrayList", "call")], None),
    "new_strips_package": (_body("    void m() { Object x = new com.acme.tax.TaxCalculator(); }"),
                           [R("TaxCalculator", "call")], None),
    "static_call_uppercase_qualifier": (_body("    void m() { double r = TaxCalculator.staticRate(); }"),
                                        [R("TaxCalculator", "call")], None),
    "lowercase_qualifier_not_static_call": (_body("    void m() { calc.hashCode(); }"), [], None),
    "extends_implements": (
        "public class TaxCalculator extends AbstractCalculator implements Taxable, Comparable {\n}\n",
        [R("AbstractCalculator", "extends"), R("Taxable", "implements"), R("Comparable", "implements")], None),
    "new_extends_implements_via": (
        "public class A extends Base implements Iface {\n    void m() { new Helper(); }\n}\n",
        [R("Base", "extends"), R("Iface", "implements"), R("Helper", "call")], None),
    # コメント・文字列リテラル・text block 内は拾わない
    "line_comment_ignored": (_body("    // new FakeIgnored();"), [], None),
    "block_comment_ignored": (_body("    /* new FakeIgnored();\n       TaxCalculator.fake(); */"), [], None),
    "string_literal_ignored": (_body('    String s = "new FakeIgnored(); TaxCalculator.fake()";'), [], None),
    "text_block_ignored": (
        'public class A {\n    String doc = """\n        example: getProperty("should.not.match")\n'
        '        """;\n}\n', [], None),
    # 宣言型参照
    "field_type": (_body("    private Engine engine;"), [R("Engine", "field_type")], None),
    "jdk_common_type_field_ignored": (_body("    private String label;\n    private java.util.List raw;"), [], None),
    "local_variable_not_field_type": (
        _body("    void m() {\n        Engine engine = new Engine();\n    }"), [R("Engine", "call")], None),
    "constructor_and_method_params": (
        "public class A {\n    public A(Engine engine, int retries) {\n    }\n\n"
        "    public void process(TaxCalc calc, String note) {\n    }\n}\n",
        [R("Engine", "field_type"), R("TaxCalc", "field_type")], None),
    "generic_argument_one_level": (_body("    private List<TaxCalc> calcs;"), [R("TaxCalc", "field_type")], None),
    "nested_generic_not_extracted": (_body("    private Map<String, List<TaxCalc>> byKey;"), [], None),
    "autowired_is_inject": (_body("    @Autowired\n    private Engine engine;"), [R("Engine", "inject")], None),
    "inject_annotation": (_body("    @Inject\n    private Engine engine;"), [R("Engine", "inject")], None),
    "resource_annotation": (_body("    @Resource\n    private Engine engine;"), [R("Engine", "inject")], []),
    "inject_does_not_leak_to_next_field": (
        _body("    @Autowired\n    private Engine engine;\n    private TaxCalc calc;"),
        [R("Engine", "inject"), R("TaxCalc", "field_type")], None),
    # 設定キー
    "value_annotation": (_body('    @Value("${tax.rate}")\n    private String rate;'), [C("tax.rate")], None),
    "value_default_discarded": (_body('    @Value("${tax.rate:0.1}")\n    private String rate;'), [C("tax.rate")], None),
    "value_multiple_placeholders": (_body('    @Value("${host}:${port}")\n    private String addr;'),
                                    [C("host"), C("port")], None),
    "get_property_with_and_without_receiver": (
        _body('    void m() {\n        System.getProperty("db.url");\n        env.getProperty("db.user");\n'
              '        getProperty("db.pass");\n    }'), [C("db.url"), C("db.user"), C("db.pass"), R("System", "call")], None),
    "get_string": (_body('    void m() { bundle.getString("app.title"); }'), [C("app.title")], None),
    "configuration_properties_prefix_dropped": (
        '@ConfigurationProperties(prefix="myapp")\npublic class A {\n}\n', [], [D("config_prefix", "myapp")]),
    "config_key_in_comment_ignored": (_body('    // @Value("${should.not.match}")\n    void m() {}'), [], None),
    "value_spel_dropped": (
        _body('    @Value("#{someBean.someProperty}")\n    private String v;'), [],
        [D("config_spel", "someBean.someProperty")]),
    "get_property_nonliteral_dropped": (
        _body("    void m() {\n        System.getProperty(KEY_CONST);\n    }"), [R("System", "call")], [D("config_nonliteral", "KEY_CONST")]),
    "get_string_nonliteral_dropped": (
        _body("    void m() { bundle.getString(var); }"), [], [D("config_nonliteral", "var")]),
    "get_string_receiver_nonliteral_still_flagged": (
        _body("    void m() { cfg.getString(name); }"), [], [D("config_nonliteral", "name")]),
    # getProperty のメソッド宣言は呼び出しではない（宣言の引数形を問わない）
    "get_property_declaration_not_flagged": (
        _body("    String getProperty(String key) { return key; }\n    void m() {\n        getProperty(KEY);\n    }"),
        [], [D("config_nonliteral", "KEY")]),
    "get_property_multi_param_declaration_not_flagged": (
        _body("    String getProperty(String key, String fallback) { return key; }\n"
              "    void m() {\n        getProperty(KEY);\n    }"), [], [D("config_nonliteral", "KEY")]),
    "get_property_annotated_param_declaration_not_flagged": (
        _body("    String getProperty(@Nonnull String key) { return key; }"), [], []),
    "get_property_varargs_declaration_not_flagged": (
        _body("    String getProperty(String... keys) { return keys[0]; }"), [], []),
    "return_get_property_is_call": (
        "class A {\n    String m() {\n        return getProperty(\"app.key\");\n    }\n"
        "    String n() {\n        return getProperty(KEY);\n    }\n}\n",
        [C("app.key")], [D("config_nonliteral", "KEY")]),
    "config_key_does_not_disturb_declared_type_refs": (
        'public class A extends Base {\n    private Engine engine;\n    @Value("${tax.rate}")\n'
        "    private String rate;\n}\n",
        [R("Base", "extends"), R("Engine", "field_type"), C("tax.rate")], None),
    # getBean / @Qualifier / @Named / @Resource(name=)
    "get_bean": (_body('    Object m() { return getBean("orderService"); }'), [C("orderService", "bean")], None),
    "get_bean_receiver_and_nonliteral": (
        _body('    void m() {\n        ctx.getBean("orderService");\n        ctx.getBean(SERVICE_NAME);\n    }'),
        [C("orderService", "bean")], [D("config_nonliteral", "SERVICE_NAME")]),
    "qualifier": (_body('    @Qualifier("orderService")\n    private String hint;'), [C("orderService", "bean")], None),
    "named": (_body('    @Named("orderService")\n    private String hint;'), [C("orderService", "bean")], None),
    "qualifier_value_attribute": (_body('    @Qualifier(value = "orderService")\n    private String hint;'),
                                  [C("orderService", "bean")], None),
    "named_value_attribute": (_body('    @Named(value = "orderService")\n    private String hint;'),
                              [C("orderService", "bean")], None),
    "resource_name_attribute": (_body('    @Resource(name = "mailer")\n    private String mailer;'),
                                [C("mailer", "bean")], None),
    "autowired_alone_has_no_config_key": (_body("    @Autowired\n    private Engine engine;"),
                                          [R("Engine", "inject")], None),
    "qualifier_nonliteral_silently_not_extracted": (
        _body("    @Qualifier(BeanNames.ORDER_SERVICE)\n    private String hint;"), [], []),
    "key_kind_mix": (
        _body('    @Qualifier("qualifierHint")\n    private String a;\n    @Named("namedHint")\n    private String b;\n'
              '    @Resource(name = "mailer")\n    private String c;\n    Object m() {\n'
              '        return getBean("orderService");\n    }\n    void n() {\n'
              '        System.getProperty("db.url");\n        bundle.getString("app.title");\n    }'),
        [C("qualifierHint", "bean"), C("namedHint", "bean"), C("mailer", "bean"), C("orderService", "bean"),
         C("db.url"), C("app.title"), R("System", "call")], None),
    # 文字列リテラル内の呼び出し風記述は走査しない（Dropped にもしない）
    "get_bean_in_string_literal": (_body('    String example = "getBean(k)";'), [], []),
    "get_property_get_string_in_string_literal": (
        _body('    String a = "getProperty(k)";\n    String b = "getString(k)";'), [], []),
    "real_call_on_same_line_as_string_literal": (
        _body('    Object m() { log("getBean(k)"); return getBean("orderService"); }'),
        [C("orderService", "bean")], None),
}


@pytest.mark.parametrize("text,refs,dropped", REFS_CASES.values(), ids=REFS_CASES)
def test_extract_refs(text, refs, dropped):
    res = A.extract_refs(text, "A.java")
    got = [(r.edge_type, r.kind, r.name, r.extra.get("via"), r.extra.get("key_kind")) for r in res.refs]
    assert sorted(got, key=str) == sorted(refs, key=str)
    if dropped is not None:
        assert [(d.reason, d.snippet) for d in res.dropped] == dropped


def test_extract_refs_line_numbers_are_one_based_and_match_source():
    res = A.extract_refs("public class A {\n    void m() { new Helper(); }\n}\n", "A.java")
    assert res.refs and res.refs[0].line == 2


def test_extract_refs_same_config_key_appearing_twice_yields_one_reference_at_first_line():
    text = ('public class A {\n    @Value("${tax.rate}")\n    private String a;\n'
            '    @Value("${tax.rate}")\n    private String b;\n}\n')
    matches = [r for r in A.extract_refs(text, "A.java").refs if r.extra.get("via") == "config_key"]
    assert len(matches) == 1 and matches[0].line == 2
