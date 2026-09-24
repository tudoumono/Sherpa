"""`XmlConfigAnalyzer` の単体テスト（アナライザ拡張 S3・A2/A7/A8＝Spring/MyBatis/Struts の XML 設定）。"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.xml_config import XmlConfigAnalyzer

A = XmlConfigAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".xml"})
    assert A.name == "xml_config"
    assert A.doctype == "xml_config"


def test_accepts_all_xml_files_without_content_inspection():
    """§6: 設定 XML アナライザは拡張子内を全件受理する——`accepts()` は既定のまま
    オーバーライドしていない（未確定ファイルは primary なし＋Dropped で通す）。"""
    assert XmlConfigAnalyzer.accepts is Analyzer.accepts


# --- Spring（Bean 定義）---

def test_spring_beans_root_becomes_config_primary():
    text = "<beans><bean class=\"com.acme.Foo\"/></beans>\n"
    res = A.collect_defs(text, "spring/applicationContext.xml")
    assert res.primary is not None
    assert res.primary.label == "Config" and res.primary.name == "applicationContext.xml"
    assert res.children == [] and res.dropped == []


def test_spring_bean_class_becomes_invokes_module_candidate():
    text = "<beans>\n  <bean class=\"com.acme.Foo\"/>\n</beans>\n"
    res = A.extract_refs(text, "spring/applicationContext.xml")
    assert len(res.refs) == 1
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.acme.Foo")
    assert ref.extra == {"via": "bean_class", "qualified": True}
    assert ref.reverse is False
    assert ref.line == 2


def test_spring_duplicate_bean_class_yields_one_candidate_per_occurrence():
    """同じ class を指す複数 `<bean>` はそれぞれ候補として返す——重複排除は共通層の
    §4(f) エッジ集約が担う（アナライザは黙って落とさない）。"""
    text = "<beans>\n  <bean class=\"com.acme.Foo\"/>\n  <bean class=\"com.acme.Foo\"/>\n</beans>\n"
    res = A.extract_refs(text, "spring/applicationContext.xml")
    assert len(res.refs) == 2
    assert [r.line for r in res.refs] == [2, 3]


def test_spring_bean_inside_comment_does_not_confuse_the_real_beans_line_number():
    """コメント内の同名タグ（`<bean class="wrong.X"/>`）に惑わされず、実要素の行番号が正しく
    取れる——1パスの `expat` は要素ツリーもコメントも自身で正しく判定するため、生テキストを
    別途検索して行番号を後付けする経路が持っていた「コメント内の出現に前へ進んでしまう」不具合
    （旧 `_TagCursor`）が構造的に起こらない。"""
    text = (
        "<beans>\n"
        "  <!-- <bean class=\"wrong.X\"/> -->\n"
        "  <bean class=\"com.acme.Real\"/>\n"
        "</beans>\n"
    )
    res = A.extract_refs(text, "spring/applicationContext.xml")
    assert len(res.refs) == 1
    ref = res.refs[0]
    assert ref.name == "com.acme.Real"
    assert ref.line == 3                     # コメント（2行目）ではなく実要素の行


def test_mybatis_cdata_containing_fake_tag_text_does_not_shift_the_next_statement_line():
    """CDATA 本文中に `<select>` に似たテキストが含まれていても、後続の実要素の行番号がずれない
    （旧 `_TagCursor` は生テキスト検索のため CDATA 内の偽出現に前進してしまい得た）。行の正しさは
    2件目の `ACCESSES(via=mapper_sql)` の line で確認する（1件目は CDATA に SQL 句が無く無関係）。"""
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <select id=\"first\"><![CDATA[ fake <select> looks like a tag ]]></select>\n"
        "  <select id=\"second\" resultType=\"com.acme.mybatis.Order\">SELECT * FROM second_table</select>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert len(table_refs) == 1
    assert table_refs[0].name == "SECOND_TABLE"
    assert table_refs[0].line == 3


def test_mybatis_large_mapper_with_many_select_statements_gets_distinct_increasing_lines():
    """多数（8,000件）の `<select>` を持つ Mapper XML でも各要素が正しい行に対応する——改行を
    毎回先頭から数え直す経路（旧 `_TagCursor`）を持たないため、要素数に対して二次的にならない
    （時間の目安ではなく、行番号の正しさ・単調増加で計算量の構造を担保する）。"""
    n = 8000
    lines = ["<mapper namespace=\"com.acme.mybatis.OrderMapper\">"]
    lines += [f'  <select id="s{i}">SELECT {i} FROM T{i}</select>' for i in range(n)]
    lines.append("</mapper>")
    text = "\n".join(lines) + "\n"

    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert len(table_refs) == n
    assert [r.name for r in table_refs] == [f"T{i}" for i in range(n)]
    got_lines = [r.line for r in table_refs]
    assert got_lines == sorted(got_lines) and len(set(got_lines)) == n   # 単調増加＝正しく1件ずつ対応


def test_spring_namespaced_beans_root_is_recognized_by_local_name():
    """namespace URI が付いていてもローカル名（`beans`）だけで判定する。"""
    text = ('<beans xmlns="http://www.springframework.org/schema/beans">'
            '<bean class="com.acme.Foo"/></beans>')
    res = A.collect_defs(text, "applicationContext.xml")
    assert res.primary is not None and res.primary.label == "Config"
    refs = A.extract_refs(text, "applicationContext.xml").refs
    assert refs[0].name == "com.acme.Foo"


# --- MyBatis（Mapper XML）---

def test_mybatis_mapper_namespace_becomes_reverse_invokes_candidate():
    text = "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n</mapper>\n"
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    ns_refs = [r for r in res.refs if r.extra.get("via") == "mapper_namespace"]
    assert len(ns_refs) == 1
    ref = ns_refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.acme.mybatis.OrderMapper")
    assert ref.extra == {"via": "mapper_namespace", "qualified": True}
    assert ref.reverse is True


def test_mybatis_result_type_and_parameter_type_become_normal_invokes_candidates():
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <select id=\"selectOrder\" resultType=\"com.acme.mybatis.Order\" parameterType=\"int\">\n"
        "    SELECT * FROM orders WHERE id = #{id}\n"
        "  </select>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    type_refs = [r for r in res.refs if r.extra.get("via") == "mapper_type"]
    assert len(type_refs) == 1                              # parameterType="int" は無視（別名・§4(c)）
    ref = type_refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.acme.mybatis.Order")
    assert ref.extra == {"via": "mapper_type", "qualified": True}
    assert ref.reverse is False


def test_mybatis_type_alias_values_without_dot_are_reported_as_dropped_mapper_type_alias():
    """`int`/`string`/`map` 等の別名（非空だが完全修飾名でない値）は推測接続せず、
    `Dropped("mapper_type_alias", ...)` として申告する（黙って捨てない・§4(c)）。"""
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <select id=\"selectAll\" resultType=\"map\" parameterType=\"string\">SELECT 1</select>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    assert [r for r in res.refs if r.extra.get("via") == "mapper_type"] == []
    alias_dropped = [d for d in res.dropped if d.reason == "mapper_type_alias"]
    assert {d.snippet for d in alias_dropped} == {"map", "string"}
    assert all(d.line == 2 for d in alias_dropped)


def test_mybatis_sql_body_yields_accesses_table_not_dropped_mapper_sql():
    """本文の SQL から `Table` へ `ACCESSES(via=mapper_sql)` を抽出する（S4'）——旧
    `Dropped("mapper_sql", ...)`＝文数だけの申告は本抽出に置き換わり撤去済み。"""
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <select id=\"selectOrder\" resultType=\"com.acme.mybatis.Order\">SELECT 1</select>\n"
        "  <insert id=\"insertOrder\">INSERT INTO orders VALUES (1)</insert>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    assert [d for d in res.dropped if d.reason == "mapper_sql"] == []
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert len(table_refs) == 1
    ref = table_refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("ACCESSES", "Table", "ORDERS")
    assert ref.line == 3                                  # <insert> の本文の行


def test_mybatis_include_refid_is_reported_as_dropped_mapper_include():
    """`<include refid="...">` は実体展開せず `Dropped("mapper_include", ...)` として申告する。"""
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <sql id=\"cols\">o.id, o.status</sql>\n"
        "  <select id=\"selectOrder\"><include refid=\"cols\"/> FROM orders o</select>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    include_dropped = [d for d in res.dropped if d.reason == "mapper_include"]
    assert len(include_dropped) == 1 and include_dropped[0].snippet == "cols"
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert [r.name for r in table_refs] == ["ORDERS"]


def test_mybatis_dynamic_sql_child_tags_are_not_expanded_but_text_is_concatenated():
    """`<if>`/`<where>` 等は展開（条件評価）しないが、その配下のテキストノードは連結して読む。"""
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <select id=\"selectOrder\">\n"
        "    SELECT * FROM orders\n"
        "    <where>\n"
        "      <if test=\"id != null\">AND id = #{id}</if>\n"
        "    </where>\n"
        "  </select>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert [r.name for r in table_refs] == ["ORDERS"]


def test_mybatis_dynamic_placeholder_table_name_is_excluded_not_misread():
    """`${tableName}` のような動的テーブル名プレースホルダをテーブル候補として読み始めない。"""
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <select id=\"dyn\">SELECT * FROM ${tableName}</select>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    assert [r for r in res.refs if r.extra.get("via") == "mapper_sql"] == []


def test_mybatis_hash_line_comment_in_sql_body_is_ignored():
    """MyBatis（MySQL 方言）は `#` 行コメントも有効——DDL/EXEC SQL の DB2/COBOL 方言（`#` が
    識別子文字）とは切り分けて `hash_line_comments=True` で呼ぶ（`#{...}` は動的プレースホルダの
    ため除外のまま）。"""
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <select id=\"selectOrder\">\n"
        "    # FROM fake\n"
        "    SELECT * FROM orders\n"
        "  </select>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert [r.name for r in table_refs] == ["ORDERS"]


def test_mybatis_placeholder_in_values_list_is_not_captured_as_table_name():
    text = (
        "<mapper namespace=\"com.acme.mybatis.OrderMapper\">\n"
        "  <insert id=\"insertOrder\">INSERT INTO orders (id, status) VALUES (#{id}, #{status})</insert>\n"
        "</mapper>\n"
    )
    res = A.extract_refs(text, "mybatis/OrderMapper.xml")
    table_refs = [r for r in res.refs if r.extra.get("via") == "mapper_sql"]
    assert [r.name for r in table_refs] == ["ORDERS"]


def test_mybatis_root_becomes_config_primary():
    text = "<mapper namespace=\"com.acme.mybatis.OrderMapper\"></mapper>"
    res = A.collect_defs(text, "mybatis/OrderMapper.xml")
    assert res.primary is not None
    assert res.primary.label == "Config" and res.primary.name == "OrderMapper.xml"


# --- Struts ---

def test_struts_action_class_becomes_invokes_module_candidate():
    text = (
        "<struts>\n"
        "  <package name=\"default\">\n"
        "    <action name=\"order\" class=\"com.acme.struts.OrderAction\"/>\n"
        "  </package>\n"
        "</struts>\n"
    )
    res = A.extract_refs(text, "struts/struts.xml")
    assert len(res.refs) == 1
    ref = res.refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("INVOKES", "Module", "com.acme.struts.OrderAction")
    assert ref.extra == {"via": "action_class", "qualified": True}


def test_struts_root_becomes_config_primary():
    res = A.collect_defs("<struts></struts>", "struts/struts.xml")
    assert res.primary is not None
    assert res.primary.label == "Config" and res.primary.name == "struts.xml"


# --- 設定でない XML（全件受理・§6）---

def test_non_config_root_yields_no_primary_and_xml_not_config_dropped():
    text = "<project><modelVersion>4.0.0</modelVersion></project>"
    res = A.collect_defs(text, "nonconfig/pom.xml")
    assert res.primary is None and res.children == []
    assert len(res.dropped) == 1
    assert res.dropped[0].reason == "xml_not_config"
    assert res.dropped[0].line == 1
    assert res.dropped[0].snippet == "project"


def test_non_config_xml_extract_refs_returns_nothing():
    text = "<web-app><display-name>Demo</display-name></web-app>"
    res = A.extract_refs(text, "nonconfig/web.xml")
    assert res.refs == [] and res.dropped == []


# --- 壊れた XML ---

def test_malformed_xml_yields_no_primary_and_xml_parse_error_dropped():
    text = "<beans>\n  <bean class=\"com.acme.a.Foo\">\n</beans>\n"     # bean が閉じていない
    res = A.collect_defs(text, "broken/broken.xml")
    assert res.primary is None and res.children == []
    assert len(res.dropped) == 1
    assert res.dropped[0].reason == "xml_parse_error"


def test_malformed_xml_extract_refs_returns_nothing():
    text = "<beans>\n  <bean class=\"com.acme.a.Foo\">\n</beans>\n"
    res = A.extract_refs(text, "broken/broken.xml")
    assert res.refs == [] and res.dropped == []


# --- 外部実体（XXE）は展開しない ---

def test_external_entity_reference_is_rejected_not_resolved():
    """DTD の外部実体参照は展開せず（`SetParamEntityParsing`＋`ExternalEntityRefHandler` が拒否）、
    パースエラーとして `Dropped("xml_parse_error", ...)` に落とす——外部ファイルの内容が
    ノードの値/名前に紛れ込まない（旧 `ElementTree.iterparse` と同水準の安全性）。"""
    text = (
        '<?xml version="1.0"?>'
        '<!DOCTYPE beans [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        '<beans><bean class="&xxe;"/></beans>'
    )
    res = A.collect_defs(text, "xxe/attempt.xml")
    assert res.primary is None
    assert len(res.dropped) == 1 and res.dropped[0].reason == "xml_parse_error"


# --- キー単位 Config children（S3'・A7 案B）---

def test_spring_bean_id_and_name_become_config_children():
    text = (
        "<beans>\n"
        '  <bean id="orderService" name="orderSvc, orderServiceAlias" class="com.acme.Foo">\n'
        '    <property name="timeout" value="30"/>\n'
        "  </bean>\n"
        "</beans>\n"
    )
    res = A.collect_defs(text, "spring/applicationContext.xml")
    by_name = {c.name: c for c in res.children}
    assert set(by_name) == {"orderService", "orderSvc", "orderServiceAlias", "orderService.timeout"}
    assert by_name["orderService"].cid_key == "key:bean:orderService"
    assert by_name["orderService"].extra == {"config_value": "com.acme.Foo", "key_kind": "bean"}
    assert by_name["orderService.timeout"].extra == {"config_value": "30", "key_kind": "property"}
    assert res.dropped == []


def test_spring_property_outside_any_named_bean_is_not_a_child():
    """id/name の無い匿名 bean 配下の `<property>` は接頭辞になる識別子が無いため children にしない。"""
    text = (
        "<beans>\n"
        '  <bean class="com.acme.Foo">\n'
        '    <property name="timeout" value="30"/>\n'
        "  </bean>\n"
        "</beans>\n"
    )
    res = A.collect_defs(text, "spring/applicationContext.xml")
    assert res.children == []


def test_spring_property_placeholder_location_is_reported_as_dropped():
    text = (
        '<beans xmlns:context="http://www.springframework.org/schema/context">\n'
        '  <context:property-placeholder location="classpath:app.properties"/>\n'
        "</beans>\n"
    )
    res = A.collect_defs(text, "spring/applicationContext.xml")
    assert res.children == []
    assert [(d.reason, d.snippet) for d in res.dropped] == [
        ("config_placeholder_location", "classpath:app.properties")]


def test_spring_alias_becomes_config_child_keyed_by_alias_name():
    text = '<beans>\n  <alias name="orderService" alias="orderServiceAlias"/>\n</beans>\n'
    res = A.collect_defs(text, "spring/applicationContext.xml")
    assert len(res.children) == 1
    child = res.children[0]
    assert child.name == "orderServiceAlias"
    assert child.extra == {"config_value": "orderService", "key_kind": "bean"}


def test_spring_duplicate_config_key_is_reported_as_dropped_and_first_wins():
    text = (
        "<beans>\n"
        '  <bean id="svc" class="com.acme.A"/>\n'
        '  <bean id="svc" class="com.acme.B"/>\n'
        "</beans>\n"
    )
    res = A.collect_defs(text, "spring/applicationContext.xml")
    assert [c.extra["config_value"] for c in res.children if c.name == "svc"] == ["com.acme.A"]
    assert [(d.reason, d.snippet) for d in res.dropped] == [("config_duplicate_key", "svc")]


def test_duplicate_bare_key_across_different_key_kinds_is_not_treated_as_duplicate():
    """`seen` は `(key_kind, 裸キー)` の組で判定する——同じ裸キー `login` でも
    `key_kind` が違えば（`constant`＝property と `action`）別名前空間なので、どちらも
    `config_duplicate_key` として落とされず両方 children に残る。"""
    text = (
        "<struts>\n"
        '  <constant name="login" value="loginPage"/>\n'
        '  <package name="default">\n'
        '    <action name="login" class="com.acme.struts.LoginAction"/>\n'
        "  </package>\n"
        "</struts>\n"
    )
    res = A.collect_defs(text, "struts/struts.xml")
    by_kind = {(c.name, c.extra["key_kind"]) for c in res.children}
    assert by_kind == {("login", "property"), ("login", "action")}
    assert res.dropped == []
    cid_keys = {c.cid_key for c in res.children}              # cid も key_kind で分離される
    assert cid_keys == {"key:property:login", "key:action:login"}


def test_mybatis_statement_and_result_map_ids_become_config_children_keyed_by_namespace():
    text = (
        '<mapper namespace="com.acme.mybatis.OrderMapper">\n'
        '  <resultMap id="OrderResult"/>\n'
        '  <select id="selectOrder">SELECT 1</select>\n'
        "</mapper>\n"
    )
    res = A.collect_defs(text, "mybatis/OrderMapper.xml")
    names = {c.name for c in res.children}
    assert names == {"com.acme.mybatis.OrderMapper.OrderResult",
                      "com.acme.mybatis.OrderMapper.selectOrder"}
    assert {c.extra["key_kind"] for c in res.children} == {"mapper"}


def test_mybatis_without_namespace_yields_no_config_children():
    text = '<mapper>\n  <select id="selectOrder">SELECT 1</select>\n</mapper>\n'
    res = A.collect_defs(text, "mybatis/OrderMapper.xml")
    assert res.children == []


def test_struts_action_and_constant_become_config_children():
    text = (
        "<struts>\n"
        '  <constant name="struts.i18n.encoding" value="UTF-8"/>\n'
        '  <package name="default">\n'
        '    <action name="order" class="com.acme.struts.OrderAction"/>\n'
        "  </package>\n"
        "</struts>\n"
    )
    res = A.collect_defs(text, "struts/struts.xml")
    by_name = {c.name: c.extra["config_value"] for c in res.children}
    assert by_name == {"order": "com.acme.struts.OrderAction", "struts.i18n.encoding": "UTF-8"}
    by_kind = {c.name: c.extra["key_kind"] for c in res.children}
    assert by_kind == {"order": "action", "struts.i18n.encoding": "property"}


def test_struts_package_name_is_not_a_config_child():
    text = '<struts>\n  <package name="default"/>\n</struts>\n'
    res = A.collect_defs(text, "struts/struts.xml")
    assert res.children == []


# --- bean 参照（`ref` 属性形・children にはせず ACCESSES(via=config_key, key_kind="bean")） ---

def test_property_ref_attribute_is_not_a_child_but_a_config_key_reference():
    text = (
        "<beans>\n"
        '  <bean id="orderService" class="com.acme.Foo">\n'
        '    <property name="repo" ref="orderRepository"/>\n'
        "  </bean>\n"
        "</beans>\n"
    )
    def_res = A.collect_defs(text, "spring/applicationContext.xml")
    assert {c.name for c in def_res.children} == {"orderService"}    # `repo` は children にしない
    ref_res = A.extract_refs(text, "spring/applicationContext.xml")
    config_refs = [r for r in ref_res.refs if r.extra.get("via") == "config_key"]
    assert len(config_refs) == 1
    ref = config_refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("ACCESSES", "Config", "orderRepository")
    assert ref.extra == {"via": "config_key", "key_kind": "bean"}
    assert ref.line == 3


def test_constructor_arg_ref_attribute_becomes_a_config_key_reference():
    text = (
        "<beans>\n"
        '  <bean id="orderService" class="com.acme.Foo">\n'
        '    <constructor-arg ref="orderRepository"/>\n'
        "  </bean>\n"
        "</beans>\n"
    )
    def_res = A.collect_defs(text, "spring/applicationContext.xml")
    assert {c.name for c in def_res.children} == {"orderService"}
    ref_res = A.extract_refs(text, "spring/applicationContext.xml")
    config_refs = [r for r in ref_res.refs if r.extra.get("via") == "config_key"]
    assert len(config_refs) == 1
    ref = config_refs[0]
    assert (ref.edge_type, ref.kind, ref.name) == ("ACCESSES", "Config", "orderRepository")
    assert ref.extra == {"via": "config_key", "key_kind": "bean"}


# --- Spring Batch（アナライザ拡張 波3 レーン B）---

_BATCH_XMLNS = 'xmlns:batch="http://www.springframework.org/schema/batch"'


def test_batch_job_and_step_become_config_children_keyed_by_job_id():
    text = (
        f"<beans {_BATCH_XMLNS}>\n"
        '  <batch:job id="nightlyJob">\n'
        '    <batch:step id="loadStep"/>\n'
        "  </batch:job>\n"
        "</beans>\n"
    )
    res = A.collect_defs(text, "batch-context.xml")
    by_name = {c.name: c for c in res.children}
    assert set(by_name) == {"nightlyJob", "nightlyJob.loadStep"}
    assert by_name["nightlyJob"].extra["key_kind"] == "bean"
    assert by_name["nightlyJob.loadStep"].extra["key_kind"] == "bean"


def test_batch_step_outside_any_job_becomes_bare_config_child():
    """job の外（`<beans>` 直下）の `<batch:step>` は裸キーとして登録する（RV 是正——旧実装は
    黙って無視していた）。"""
    text = f'<beans {_BATCH_XMLNS}>\n  <batch:step id="orphanStep"/>\n</beans>\n'
    res = A.collect_defs(text, "batch-context.xml")
    assert len(res.children) == 1
    assert res.children[0].name == "orphanStep"
    assert res.children[0].extra["key_kind"] == "bean"


def test_batch_tasklet_chunk_and_job_listener_refs_become_config_key_references():
    text = (
        f"<beans {_BATCH_XMLNS}>\n"
        '  <batch:job id="nightlyJob">\n'
        '    <batch:step id="loadStep">\n'
        '      <batch:tasklet ref="loadTasklet"/>\n'
        "    </batch:step>\n"
        '    <batch:step id="payrollStep">\n'
        '      <batch:chunk reader="payrollReader" processor="payrollProcessor" writer="payrollWriter"/>\n'
        "    </batch:step>\n"
        '    <batch:job-listener ref="auditListener"/>\n'
        "  </batch:job>\n"
        "</beans>\n"
    )
    res = A.extract_refs(text, "batch-context.xml")
    config_refs = {r.name for r in res.refs if r.extra.get("via") == "config_key"}
    assert config_refs == {"loadTasklet", "payrollReader", "payrollProcessor", "payrollWriter",
                            "auditListener"}
    assert all(r.extra == {"via": "config_key", "key_kind": "bean"}
              for r in res.refs if r.extra.get("via") == "config_key")


# --- Spring Batch の namespace URI 判定（§4(c) RV 是正）---

def test_unnamespaced_job_element_is_not_treated_as_spring_batch():
    """`batch:`/`b:` プレフィックスも default namespace も無い `<job>` は Spring Batch 扱いにしない
    （ローカル名だけの誤判定を防ぐ）。"""
    text = '<beans><job id="notBatch"/></beans>\n'
    res = A.collect_defs(text, "batch-context.xml")
    assert res.children == []


def test_job_with_alternate_prefix_bound_to_batch_namespace_is_recognized():
    """prefix の綴りは `batch:` でなくても、URI が Spring Batch のものなら認識する。"""
    text = (
        '<beans xmlns:b="http://www.springframework.org/schema/batch">\n'
        '  <b:job id="nightlyJob"/>\n'
        "</beans>\n"
    )
    res = A.collect_defs(text, "batch-context.xml")
    assert {c.name for c in res.children} == {"nightlyJob"}


def test_job_with_default_namespace_override_is_recognized():
    """要素だけに `xmlns="…/batch"`（default namespace の上書き）を付けた形でも認識する。"""
    text = (
        "<beans>\n"
        '  <job xmlns="http://www.springframework.org/schema/batch" id="nightlyJob"/>\n'
        "</beans>\n"
    )
    res = A.collect_defs(text, "batch-context.xml")
    assert {c.name for c in res.children} == {"nightlyJob"}
