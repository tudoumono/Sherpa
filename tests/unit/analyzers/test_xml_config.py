"""`XmlConfigAnalyzer` の単体テスト（Spring/MyBatis/Struts/Spring Batch の XML 設定）。

入力 XML → (定義・children / 参照 / Dropped の理由) の表で確かめる。
"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.xml_config import XmlConfigAnalyzer

A = XmlConfigAnalyzer()

SPRING = "spring/applicationContext.xml"
MAPPER = "mybatis/OrderMapper.xml"
BATCH = "batch-context.xml"
NS = '<mapper namespace="com.acme.mybatis.OrderMapper">\n'
BATCH_XMLNS = 'xmlns:batch="http://www.springframework.org/schema/batch"'


def test_extensions_and_name():
    assert A.extensions == frozenset({".xml"})
    assert A.name == "xml_config"
    assert A.doctype == "xml_config"


def test_accepts_all_xml_files_without_content_inspection():
    """設定 XML アナライザは拡張子内を全件受理する（`accepts()` を上書きしない）。"""
    assert XmlConfigAnalyzer.accepts is Analyzer.accepts


# ---- primary（設定ルート）----

@pytest.mark.parametrize("text,path,name", [
    ('<beans><bean class="com.acme.Foo"/></beans>\n', SPRING, "applicationContext.xml"),
    ('<beans xmlns="http://www.springframework.org/schema/beans"><bean class="com.acme.Foo"/></beans>',
     "applicationContext.xml", "applicationContext.xml"),          # namespace 付きでもローカル名で判定
    ('<mapper namespace="com.acme.mybatis.OrderMapper"></mapper>', MAPPER, "OrderMapper.xml"),
    ("<struts></struts>", "struts/struts.xml", "struts.xml"),
], ids=["spring", "spring_namespaced", "mybatis", "struts"])
def test_config_root_becomes_config_primary(text, path, name):
    res = A.collect_defs(text, path)
    assert res.primary is not None
    assert res.primary.label == "Config" and res.primary.name == name


# 設定でない XML・壊れた XML・外部実体（XXE）は primary なし＋Dropped（全件受理・§6）
@pytest.mark.parametrize("text,path,dropped", [
    ("<project><modelVersion>4.0.0</modelVersion></project>", "nonconfig/pom.xml",
     ("xml_not_config", 1, "project")),
    ('<beans>\n  <bean class="com.acme.a.Foo">\n</beans>\n', "broken/broken.xml",          # bean が閉じていない
     ("xml_parse_error", None, None)),
    ('<?xml version="1.0"?><!DOCTYPE beans [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
     '<beans><bean class="&xxe;"/></beans>', "xxe/attempt.xml", ("xml_parse_error", None, None)),
], ids=["not_config", "malformed", "external_entity_rejected"])
def test_unusable_xml_yields_no_primary_and_one_dropped(text, path, dropped):
    res = A.collect_defs(text, path)
    assert res.primary is None and res.children == []
    assert len(res.dropped) == 1
    d = res.dropped[0]
    reason, line, snippet = dropped
    assert d.reason == reason
    assert line is None or d.line == line
    assert snippet is None or d.snippet == snippet


@pytest.mark.parametrize("text,path", [
    ("<web-app><display-name>Demo</display-name></web-app>", "nonconfig/web.xml"),
    ('<beans>\n  <bean class="com.acme.a.Foo">\n</beans>\n', "broken/broken.xml"),
], ids=["not_config", "malformed"])
def test_unusable_xml_extract_refs_returns_nothing(text, path):
    res = A.extract_refs(text, path)
    assert res.refs == [] and res.dropped == []


# ---- extract_refs ----
# 参照の期待値: (edge, kind, name, extra, reverse, line)。extra/reverse/line は None なら検査しない。

def R(edge, kind, name, extra=None, reverse=None, line=None):
    return (edge, kind, name, extra, reverse, line)


def Q(name, line=None):
    """Spring/MyBatis の `config_key`（bean 参照）。"""
    return R("ACCESSES", "Config", name, {"via": "config_key", "key_kind": "bean"}, None, line)


def T(name, line=None):
    return R("ACCESSES", "Table", name, {"via": "mapper_sql"}, None, line)


def D(reason, line=None, snippet=None):
    return (reason, line, snippet)


def _beans(body):
    return f"<beans>\n{body}</beans>\n"


def _mapper(body):
    return NS + body + "</mapper>\n"


# (入力, path, via フィルタ（None=全件）, 参照, Dropped（None=検査しない）)
REFS_CASES = {
    "spring_bean_class": (
        _beans('  <bean class="com.acme.Foo"/>\n'), SPRING, None,
        [R("INVOKES", "Module", "com.acme.Foo", {"via": "bean_class", "qualified": True}, False, 2)], []),
    "spring_duplicate_bean_class_one_per_occurrence": (
        _beans('  <bean class="com.acme.Foo"/>\n  <bean class="com.acme.Foo"/>\n'), SPRING, None,
        [R("INVOKES", "Module", "com.acme.Foo", None, None, 2), R("INVOKES", "Module", "com.acme.Foo", None, None, 3)],
        None),
    "spring_bean_in_comment_does_not_shift_line": (
        _beans('  <!-- <bean class="wrong.X"/> -->\n  <bean class="com.acme.Real"/>\n'), SPRING, None,
        [R("INVOKES", "Module", "com.acme.Real", None, None, 3)], None),
    "mybatis_cdata_fake_tag_does_not_shift_line": (
        _mapper('  <select id="first"><![CDATA[ fake <select> looks like a tag ]]></select>\n'
                '  <select id="second" resultType="com.acme.mybatis.Order">SELECT * FROM second_table</select>\n'),
        MAPPER, "mapper_sql", [T("SECOND_TABLE", 3)], None),
    "mybatis_namespace_reverse_invokes": (
        _mapper(""), MAPPER, "mapper_namespace",
        [R("INVOKES", "Module", "com.acme.mybatis.OrderMapper", {"via": "mapper_namespace", "qualified": True}, True)],
        None),
    "mybatis_result_type_normal_invokes_parameter_type_alias_ignored": (
        _mapper('  <select id="selectOrder" resultType="com.acme.mybatis.Order" parameterType="int">\n'
                "    SELECT * FROM orders WHERE id = #{id}\n  </select>\n"),
        MAPPER, "mapper_type",
        [R("INVOKES", "Module", "com.acme.mybatis.Order", {"via": "mapper_type", "qualified": True}, False)], None),
    "mybatis_sql_body_yields_accesses_table": (
        _mapper('  <select id="selectOrder" resultType="com.acme.mybatis.Order">SELECT 1</select>\n'
                "  <insert id=\"insertOrder\">INSERT INTO orders VALUES (1)</insert>\n"),
        MAPPER, "mapper_sql", [T("ORDERS", 3)], None),
    "mybatis_include_refid_dropped": (
        _mapper('  <sql id="cols">o.id, o.status</sql>\n'
                '  <select id="selectOrder"><include refid="cols"/> FROM orders o</select>\n'),
        MAPPER, "mapper_sql", [T("ORDERS")], None),
    "mybatis_dynamic_tags_not_expanded_text_concatenated": (
        _mapper('  <select id="selectOrder">\n    SELECT * FROM orders\n    <where>\n'
                '      <if test="id != null">AND id = #{id}</if>\n    </where>\n  </select>\n'),
        MAPPER, "mapper_sql", [T("ORDERS")], None),
    "mybatis_dynamic_placeholder_table_excluded": (
        _mapper('  <select id="dyn">SELECT * FROM ${tableName}</select>\n'), MAPPER, "mapper_sql", [], None),
    "mybatis_hash_line_comment_ignored": (
        _mapper('  <select id="selectOrder">\n    # FROM fake\n    SELECT * FROM orders\n  </select>\n'),
        MAPPER, "mapper_sql", [T("ORDERS")], None),
    "mybatis_placeholder_in_values_not_table": (
        _mapper('  <insert id="insertOrder">INSERT INTO orders (id, status) VALUES (#{id}, #{status})</insert>\n'),
        MAPPER, "mapper_sql", [T("ORDERS")], None),
    "struts_action_class": (
        '<struts>\n  <package name="default">\n    <action name="order" class="com.acme.struts.OrderAction"/>\n'
        "  </package>\n</struts>\n", "struts/struts.xml", None,
        [R("INVOKES", "Module", "com.acme.struts.OrderAction", {"via": "action_class", "qualified": True})], None),
    # bean 参照（children にはせず ACCESSES(via=config_key, key_kind=bean)）
    "property_ref_attribute": (
        _beans('  <bean id="orderService" class="com.acme.Foo">\n    <property name="repo" ref="orderRepository"/>\n'
               "  </bean>\n"), SPRING, "config_key", [Q("orderRepository", 3)], None),
    "constructor_arg_ref_attribute": (
        _beans('  <bean id="orderService" class="com.acme.Foo">\n    <constructor-arg ref="orderRepository"/>\n'
               "  </bean>\n"), SPRING, "config_key", [Q("orderRepository")], None),
    "batch_tasklet_chunk_job_listener_refs": (
        f"<beans {BATCH_XMLNS}>\n  <batch:job id=\"nightlyJob\">\n    <batch:step id=\"loadStep\">\n"
        '      <batch:tasklet ref="loadTasklet"/>\n    </batch:step>\n    <batch:step id="payrollStep">\n'
        '      <batch:chunk reader="payrollReader" processor="payrollProcessor" writer="payrollWriter"/>\n'
        '    </batch:step>\n    <batch:job-listener ref="auditListener"/>\n  </batch:job>\n</beans>\n',
        BATCH, "config_key",
        [Q("loadTasklet"), Q("payrollReader"), Q("payrollProcessor"), Q("payrollWriter"), Q("auditListener")], None),
    # bean 定義継承（TERASOLUNA 実測の取りこぼし）
    "bean_parent_attribute": (
        _beans('  <bean id="AbstractCodeList" class="jp.example.fw.JdbcCodeList" abstract="true"/>\n'
               '  <bean id="CL_SAMPLE" parent="AbstractCodeList">\n'
               '    <property name="querySql" value="SELECT code FROM sample_code"/>\n  </bean>\n'),
        "spring/sample-codelist.xml", "config_key", [Q("AbstractCodeList", 3)], None),
    "bean_parent_in_comment_not_reference": (
        _beans('  <!-- Example:\n  <bean id="CL_SAMPLE" parent="AbstractCodeList"/>\n  -->\n'),
        "spring/sample-codelist.xml", None, [], None),
    # <import resource>（設定ファイル合成）
    "import_resource_by_path_suffix": (
        _beans('  <import resource="classpath:/META-INF/spring/sample-domain.xml"/>\n'
               '  <import resource="sample-env.xml"/>\n'),
        "spring/sample.xml", "include",
        [R("INVOKES", "Config", "META-INF/spring/sample-domain.xml", {"via": "include", "path_suffix": True}),
         R("INVOKES", "Config", "sample-env.xml", {"via": "include", "path_suffix": True})], []),
    "import_without_resource_dropped": (
        "<beans>\n  <import/>\n</beans>\n", "spring/sample.xml", None, [],
        [D("config_import_missing_resource", 2, "")]),
    "import_wildcard_dropped_not_guessed": (
        _beans('  <import resource="classpath*:META-INF/spring/**/*-codelist.xml"/>\n'),
        "spring/sample.xml", None, [],
        [D("config_import_wildcard", None, "classpath*:META-INF/spring/**/*-codelist.xml")]),
    # MyBatis の型別名は推測接続せず Dropped
    "mybatis_type_alias_dropped": (
        _mapper('  <select id="selectAll" resultType="map" parameterType="string">SELECT 1</select>\n'),
        MAPPER, "mapper_type", [], None),
}


def _refs_match(got, exp):
    e_edge, e_kind, e_name, e_extra, e_rev, e_line = exp
    return ((got.edge_type, got.kind, got.name) == (e_edge, e_kind, e_name)
            and (e_extra is None or got.extra == e_extra)
            and (e_rev is None or got.reverse is e_rev)
            and (e_line is None or got.line == e_line))


@pytest.mark.parametrize("text,path,via,refs,dropped", REFS_CASES.values(), ids=REFS_CASES)
def test_extract_refs(text, path, via, refs, dropped):
    res = A.extract_refs(text, path)
    got = [r for r in res.refs if via is None or r.extra.get("via") == via]
    assert len(got) == len(refs)
    for g, e in zip(got, refs):
        assert _refs_match(g, e), (g, e)
    if dropped is not None:
        assert len(res.dropped) == len(dropped)
        for d, (reason, line, snippet) in zip(res.dropped, dropped):
            assert d.reason == reason
            assert line is None or d.line == line
            assert snippet is None or d.snippet == snippet


def test_mybatis_type_alias_values_are_reported_as_dropped_mapper_type_alias():
    """`map`/`string` 等の別名（完全修飾でない値）は推測接続せず `mapper_type_alias` で申告する。"""
    res = A.extract_refs(
        _mapper('  <select id="selectAll" resultType="map" parameterType="string">SELECT 1</select>\n'), MAPPER)
    alias_dropped = [d for d in res.dropped if d.reason == "mapper_type_alias"]
    assert {d.snippet for d in alias_dropped} == {"map", "string"}
    assert all(d.line == 2 for d in alias_dropped)
    assert [d for d in res.dropped if d.reason == "mapper_sql"] == []        # 文数だけの旧申告は撤去済み


def test_mybatis_include_refid_is_reported_as_dropped_mapper_include():
    res = A.extract_refs(
        _mapper('  <sql id="cols">o.id, o.status</sql>\n'
                '  <select id="selectOrder"><include refid="cols"/> FROM orders o</select>\n'), MAPPER)
    include_dropped = [d for d in res.dropped if d.reason == "mapper_include"]
    assert len(include_dropped) == 1 and include_dropped[0].snippet == "cols"


def test_mybatis_large_mapper_gets_distinct_increasing_lines():
    """8,000 件の `<select>` でも各要素が正しい行に対応する（行番号の正しさ・単調増加で構造を担保）。"""
    n = 8000
    lines = ['<mapper namespace="com.acme.mybatis.OrderMapper">']
    lines += [f'  <select id="s{i}">SELECT {i} FROM T{i}</select>' for i in range(n)]
    lines.append("</mapper>")
    res = A.extract_refs("\n".join(lines) + "\n", MAPPER)
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert [r.name for r in table_refs] == [f"T{i}" for i in range(n)]
    got_lines = [r.line for r in table_refs]
    assert got_lines == sorted(got_lines) and len(set(got_lines)) == n


# ---- collect_defs: キー単位 Config children ----
# 期待値: {name: extra の部分集合}・Dropped は (reason, snippet) 列（None=検査しない）

STRUTS_SAME_KEY = (
    '<struts>\n  <constant name="login" value="loginPage"/>\n  <package name="default">\n'
    '    <action name="login" class="com.acme.struts.LoginAction"/>\n  </package>\n</struts>\n')

CHILDREN_CASES = {
    "spring_bean_id_name_property": (
        _beans('  <bean id="orderService" name="orderSvc, orderServiceAlias" class="com.acme.Foo">\n'
               '    <property name="timeout" value="30"/>\n  </bean>\n'), SPRING,
        {"orderService": {"config_value": "com.acme.Foo", "key_kind": "bean"},
         "orderSvc": None, "orderServiceAlias": None,
         "orderService.timeout": {"config_value": "30", "key_kind": "property"}}, []),
    "spring_anonymous_bean_property_not_child": (
        _beans('  <bean class="com.acme.Foo">\n    <property name="timeout" value="30"/>\n  </bean>\n'),
        SPRING, {}, None),
    "spring_property_placeholder_location_dropped": (
        '<beans xmlns:context="http://www.springframework.org/schema/context">\n'
        '  <context:property-placeholder location="classpath:app.properties"/>\n</beans>\n', SPRING, {},
        [("config_placeholder_location", "classpath:app.properties")]),
    "spring_alias": (
        _beans('  <alias name="orderService" alias="orderServiceAlias"/>\n'), SPRING,
        {"orderServiceAlias": {"config_value": "orderService", "key_kind": "bean"}}, None),
    "spring_duplicate_key_first_wins": (
        _beans('  <bean id="svc" class="com.acme.A"/>\n  <bean id="svc" class="com.acme.B"/>\n'), SPRING,
        {"svc": {"config_value": "com.acme.A"}}, [("config_duplicate_key", "svc")]),
    "struts_same_bare_key_different_kinds_not_duplicate": (
        STRUTS_SAME_KEY, "struts/struts.xml", {"login": None}, []),       # 個別に検証（下の専用テスト）
    "mybatis_statement_and_result_map_ids": (
        _mapper('  <resultMap id="OrderResult"/>\n  <select id="selectOrder">SELECT 1</select>\n'), MAPPER,
        {"com.acme.mybatis.OrderMapper.OrderResult": {"key_kind": "mapper"},
         "com.acme.mybatis.OrderMapper.selectOrder": {"key_kind": "mapper"}}, None),
    "mybatis_without_namespace_no_children": (
        '<mapper>\n  <select id="selectOrder">SELECT 1</select>\n</mapper>\n', MAPPER, {}, None),
    "struts_action_and_constant": (
        '<struts>\n  <constant name="struts.i18n.encoding" value="UTF-8"/>\n  <package name="default">\n'
        '    <action name="order" class="com.acme.struts.OrderAction"/>\n  </package>\n</struts>\n',
        "struts/struts.xml",
        {"order": {"config_value": "com.acme.struts.OrderAction", "key_kind": "action"},
         "struts.i18n.encoding": {"config_value": "UTF-8", "key_kind": "property"}}, None),
    "struts_package_name_not_child": ('<struts>\n  <package name="default"/>\n</struts>\n', "struts/struts.xml", {}, None),
    "property_ref_not_a_child": (
        _beans('  <bean id="orderService" class="com.acme.Foo">\n    <property name="repo" ref="orderRepository"/>\n'
               "  </bean>\n"), SPRING, {"orderService": None}, None),
    "constructor_arg_ref_not_a_child": (
        _beans('  <bean id="orderService" class="com.acme.Foo">\n    <constructor-arg ref="orderRepository"/>\n'
               "  </bean>\n"), SPRING, {"orderService": None}, None),
    "bean_parent_is_reference_only": (
        _beans('  <bean id="AbstractCodeList" class="jp.example.fw.JdbcCodeList" abstract="true"/>\n'
               '  <bean id="CL_SAMPLE" parent="AbstractCodeList">\n'
               '    <property name="querySql" value="SELECT code FROM sample_code"/>\n  </bean>\n'),
        "spring/sample-codelist.xml", {"AbstractCodeList": None, "CL_SAMPLE": None, "CL_SAMPLE.querySql": None}, None),
    # Spring Batch
    "batch_job_and_step_keyed_by_job_id": (
        f'<beans {BATCH_XMLNS}>\n  <batch:job id="nightlyJob">\n    <batch:step id="loadStep"/>\n  </batch:job>\n</beans>\n',
        BATCH, {"nightlyJob": {"key_kind": "bean"}, "nightlyJob.loadStep": {"key_kind": "bean"}}, None),
    "batch_step_outside_job_is_bare_child": (
        f'<beans {BATCH_XMLNS}>\n  <batch:step id="orphanStep"/>\n</beans>\n', BATCH,
        {"orphanStep": {"key_kind": "bean"}}, None),
    "batch_unnamespaced_job_not_batch": ('<beans><job id="notBatch"/></beans>\n', BATCH, {}, None),
    "batch_alternate_prefix_bound_to_batch_uri": (
        '<beans xmlns:b="http://www.springframework.org/schema/batch">\n  <b:job id="nightlyJob"/>\n</beans>\n',
        BATCH, {"nightlyJob": None}, None),
    "batch_default_namespace_override": (
        '<beans>\n  <job xmlns="http://www.springframework.org/schema/batch" id="nightlyJob"/>\n</beans>\n',
        BATCH, {"nightlyJob": None}, None),
}


@pytest.mark.parametrize("text,path,children,dropped", CHILDREN_CASES.values(), ids=CHILDREN_CASES)
def test_collect_defs_config_children(text, path, children, dropped):
    res = A.collect_defs(text, path)
    assert {c.name for c in res.children} == set(children)
    for c in res.children:
        want = children[c.name]
        assert want is None or all(c.extra.get(k) == v for k, v in want.items()), c
    if dropped is not None:
        assert [(d.reason, d.snippet) for d in res.dropped] == dropped


def test_duplicate_bare_key_across_key_kinds_keeps_both_with_distinct_cids():
    """`seen` は `(key_kind, 裸キー)` の組で判定する——同じ `login` でも property と action は別名前空間。"""
    res = A.collect_defs(STRUTS_SAME_KEY, "struts/struts.xml")
    assert {(c.name, c.extra["key_kind"]) for c in res.children} == {("login", "property"), ("login", "action")}
    assert {c.cid_key for c in res.children} == {"key:property:login", "key:action:login"}
    assert res.dropped == []


def test_spring_bean_id_cid_key():
    res = A.collect_defs(
        _beans('  <bean id="orderService" class="com.acme.Foo"/>\n'), SPRING)
    assert [c.cid_key for c in res.children] == ["key:bean:orderService"]
