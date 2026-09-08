"""`world_graph.build_world()` 経由の Java FW / 設定ファイル統合テスト（アナライザ拡張 S3b/S3'・
docs/proposals/2026-09-05-アナライザ拡張.md §4(b)/(c)/(c)'）。

`fixtures/corpus/java-fw`（Spring/MyBatis/Struts の最小サンプル＋設定でない XML＋壊れた XML＋
properties/YAML＋`configkeys/`＝S3' のキー単位 children/config_key 参照サンプル）を実際に
`build_world()` へ通し、共通層（S3a で実装済み・無改修）が新設アナライザ（`XmlConfigAnalyzer`/
`PropertiesAnalyzer`/`YamlConfigAnalyzer`）の出力を正しく処理することを固定する。
"""
from __future__ import annotations

import pathlib

from sherpa import corpus_docs
from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "java-fw"
WORLD_ID = "java_fw_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]) for n in nodes}


def _edge_keys(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


def _edges_between(nodes, edges, etype, src_key, dst_key):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return [e for e in edges if e["type"] == etype
            and by_cid.get(e["src"]) == src_key and by_cid.get(e["dst"]) == dst_key]


# --- Config primary ノード（設定 XML／properties／YAML）---

def test_config_nodes_created_for_spring_mybatis_struts_properties_and_yaml():
    nodes, _edges, _flags = _build()
    keys = _node_keys(nodes)
    assert ("Config", "applicationContext.xml", "spring/applicationContext.xml") in keys
    assert ("Config", "OrderMapper.xml", "mybatis/OrderMapper.xml") in keys
    assert ("Config", "struts.xml", "struts/struts.xml") in keys
    assert ("Config", "app.properties", "properties/app.properties") in keys
    assert ("Config", "config.yml", "yamlcfg/config.yml") in keys


def test_non_config_xml_produces_no_config_node():
    nodes, _edges, _flags = _build()
    paths = {n["path"] for n in nodes}
    assert "nonconfig/pom.xml" not in paths
    assert "nonconfig/web.xml" not in paths


def test_broken_xml_produces_no_config_node():
    nodes, _edges, _flags = _build()
    assert "broken/broken.xml" not in {n["path"] for n in nodes}


# --- Spring（Bean 定義・完全修飾名の2段解決）---

def test_bean_class_resolves_to_qualified_package_and_does_not_connect_to_same_named_sibling():
    """同名・別 package（`com.acme.a.Foo`/`com.acme.b.Foo`）の2型が同一世代に存在するとき、
    `<bean class="com.acme.a.Foo">` は qualified 完全一致で a 側だけに解決する（RV2-4）。"""
    nodes, edges, flags = _build()
    ek = _edge_keys(nodes, edges)
    config = ("Config", "applicationContext.xml", "spring/applicationContext.xml")
    foo_a = ("Module", "Foo", "spring/com/acme/a/Foo.java")
    foo_b = ("Module", "Foo", "spring/com/acme/b/Foo.java")
    assert ("INVOKES", config, foo_a, "bean_class") in ek
    assert ("INVOKES", config, foo_b, "bean_class") not in ek
    assert not [f for f in flags if f.get("reason") in ("ambiguous", "qualified_fallback")
                and f.get("from") == "spring/applicationContext.xml"]


def test_duplicate_bean_class_pointing_at_the_same_module_collapses_to_one_edge():
    """§4(f): 同一 (src,type,dst) の複数候補（同じ class を指す2つの `<bean>`）は1本へ集約される。"""
    nodes, edges, _flags = _build()
    config = ("Config", "applicationContext.xml", "spring/applicationContext.xml")
    foo_a = ("Module", "Foo", "spring/com/acme/a/Foo.java")
    matched = _edges_between(nodes, edges, "INVOKES", config, foo_a)
    assert len(matched) == 1
    assert matched[0]["via"] == "bean_class"


# --- MyBatis（逆向きエッジ＋resultType/parameterType）---

def test_mapper_namespace_yields_reverse_edge_from_module_to_config():
    """A8: `<mapper namespace="...">` は Module→Config の向きで張られる
    （Mapper インターフェース側から見た依存として）。"""
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    mapper_module = ("Module", "OrderMapper", "mybatis/com/acme/mybatis/OrderMapper.java")
    mapper_config = ("Config", "OrderMapper.xml", "mybatis/OrderMapper.xml")
    assert ("INVOKES", mapper_module, mapper_config, "mapper_namespace") in ek


def test_mapper_result_type_yields_normal_edge_from_config_to_module():
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    mapper_config = ("Config", "OrderMapper.xml", "mybatis/OrderMapper.xml")
    order_module = ("Module", "Order", "mybatis/com/acme/mybatis/Order.java")
    assert ("INVOKES", mapper_config, order_module, "mapper_type") in ek


def test_mapper_include_refid_is_recorded_as_dropped_not_silently_lost():
    """`<include refid="...">` は実体展開せず `Dropped("mapper_include", ...)` として申告する
    （旧 `Dropped("mapper_sql", ...)`＝文数だけの申告は S4' の `Table` 抽出に置き換わり撤去済み）。"""
    _nodes, _edges, flags = _build()
    mapper_include = [f for f in flags if f.get("reason") == "dropped_syntax"
                       and f.get("why") == "mapper_include" and f.get("from") == "mybatis/OrderMapper.xml"]
    assert len(mapper_include) == 1
    assert mapper_include[0]["snippet"] == "cols"


# --- S4'（MyBatis SQL 本文 → Table・Config→Table の2段契約・波2持ち越し対応）---

def test_mapper_sql_body_yields_accesses_table_edges_to_ddl_defined_tables():
    """`fixtures/corpus/java-fw/mybatis/schema.sql`（同一 top_scope）が定義する `ORDERS`/
    `ORDER_LINES`/`"customers"` へ、Mapper XML の SQL 本文から `Config -ACCESSES(via=mapper_sql)->
    Table` が張られる。"""
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    mapper_config = ("Config", "OrderMapper.xml", "mybatis/OrderMapper.xml")
    orders = ("Table", "ORDERS", "mybatis/schema.sql")
    order_lines = ("Table", "ORDER_LINES", "mybatis/schema.sql")
    customers = ("Table", "customers", "mybatis/schema.sql")
    assert ("ACCESSES", mapper_config, orders, "mapper_sql") in ek
    assert ("ACCESSES", mapper_config, order_lines, "mapper_sql") in ek
    assert ("ACCESSES", mapper_config, customers, "mapper_sql") in ek


def test_mapper_sql_nonexistent_table_is_flagged_unresolved_not_silently_dropped():
    """DDL に無い `AUDIT_LOG` は `Table` ノードを作らず unresolved flag に落ちる
    （§4(g) 既定案・COBOL EXEC SQL と同じ契約）。"""
    _nodes, _edges, flags = _build()
    missing = [f for f in flags if f.get("reason") == "unresolved" and f.get("kind") == "Table"
               and f.get("name") == "AUDIT_LOG" and f.get("from") == "mybatis/OrderMapper.xml"]
    assert len(missing) == 1
    assert missing[0]["via"] == "mapper_sql"


def test_table_incoming_reaches_config_then_module_mapper_then_module_service():
    """A8＋S4': `Table(ORDERS)` 起点の incoming（影響 traversal が辿る `COPIES|CONTAINS|INVOKES|
    ACCESSES` と同じエッジ集合を1ホップずつ逆に辿る）が `Config(OrderMapper.xml)`（`mapper_sql`）→
    `Module(OrderMapper)`（`mapper_namespace`・reverse）→ `Module(OrderService)`（`field_type`）まで
    2ホップ超えて届く——Mapper XML の SQL から Table への直接エッジが無くても、Mapper 利用
    プログラムまで影響探索が到達する（波2持ち越し「Module→Config→Table の2段」契約）。"""
    nodes, edges, _flags = _build()
    by_key = {(n["label"], n["name"], n["path"]): n["cid"] for n in nodes}
    orders_cid = by_key[("Table", "ORDERS", "mybatis/schema.sql")]

    incoming: dict = {}
    for e in edges:
        incoming.setdefault(e["dst"], []).append(e["src"])

    seen = {orders_cid}
    frontier = [orders_cid]
    while frontier:
        nxt = []
        for cid in frontier:
            for src in incoming.get(cid, []):
                if src not in seen:
                    seen.add(src)
                    nxt.append(src)
        frontier = nxt

    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    reached = {by_cid[cid] for cid in seen if cid in by_cid}
    assert ("Config", "OrderMapper.xml", "mybatis/OrderMapper.xml") in reached
    assert ("Module", "OrderMapper", "mybatis/com/acme/mybatis/OrderMapper.java") in reached
    assert ("Module", "OrderService", "mybatis/com/acme/mybatis/OrderService.java") in reached


# --- Struts ---

def test_struts_action_class_yields_edge_from_config_to_module():
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    struts_config = ("Config", "struts.xml", "struts/struts.xml")
    action_module = ("Module", "OrderAction", "struts/com/acme/struts/OrderAction.java")
    assert ("INVOKES", struts_config, action_module, "action_class") in ek


# --- 設定でない XML／壊れた XML の可視化（黙って落とさない）---

def test_non_config_xml_is_flagged_dropped_syntax_xml_not_config():
    _nodes, _edges, flags = _build()
    pom = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("from") == "nonconfig/pom.xml"]
    assert len(pom) == 1
    assert pom[0]["why"] == "xml_not_config" and pom[0]["analyzer"] == "xml_config"
    assert pom[0]["snippet"] == "project"

    web = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("from") == "nonconfig/web.xml"]
    assert len(web) == 1 and web[0]["why"] == "xml_not_config" and web[0]["snippet"] == "web-app"


def test_broken_xml_is_flagged_dropped_syntax_xml_parse_error():
    _nodes, _edges, flags = _build()
    broken = [f for f in flags if f.get("reason") == "dropped_syntax" and f.get("from") == "broken/broken.xml"]
    assert len(broken) == 1
    assert broken[0]["why"] == "xml_parse_error" and broken[0]["analyzer"] == "xml_config"


# --- properties/YAML はキー単位の Config children を持つ（S3'・案B）---
# S3（案A）時点の fixtures（`properties/app.properties`・`yamlcfg/config.yml`）は children を
# 持たなかったが、S3' 実装後は自動的にキー children を持つようになる——ここではその children が
# `CONTAINS` エッジになり、かつ INVOKES（bean_class 等）は引き続き無関係のままであることを確認する。

def test_properties_and_yaml_config_nodes_have_key_children_via_contains_edges():
    nodes, edges, _flags = _build()
    props_cid = next(n["cid"] for n in nodes if n["path"] == "properties/app.properties")
    yaml_cid = next(n["cid"] for n in nodes if n["path"] == "yamlcfg/config.yml")
    props_children = {n["name"] for e in edges if e["type"] == "CONTAINS" and e["src"] == props_cid
                      for n in nodes if n["cid"] == e["dst"]}
    yaml_children = {n["name"] for e in edges if e["type"] == "CONTAINS" and e["src"] == yaml_cid
                     for n in nodes if n["cid"] == e["dst"]}
    assert props_children == {"db.url", "db.user"}
    assert yaml_children == {"server.port", "db.url"}
    # INVOKES（bean_class/mapper_type/action_class 等）はこれらのノードには依然として無関係。
    assert not [e for e in edges if e["type"] == "INVOKES"
                and (e["src"] in (props_cid, yaml_cid) or e["dst"] in (props_cid, yaml_cid))]


# --- S3'（キー単位 children・config_key 参照・A9 全件接続）: `fixtures/corpus/java-fw/configkeys/` ---

def test_config_key_children_use_config_label_and_bare_key_index_not_data_item():
    """RV2-2: children のラベルは親と同じ `Config`（`DataItem` にしない）。索引キーは裸のキー
    （ファイル修飾なし）——`Config(app.properties) -CONTAINS-> Config(db.url)` の形。"""
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    config = ("Config", "app.properties", "configkeys/env/dev/app.properties")
    key_child = ("Config", "db.url", "configkeys/env/dev/app.properties")
    assert ("CONTAINS", config, key_child, None) in ek


def test_properties_duplicate_key_and_continuation_line_are_handled():
    """重複キーは最初の行のみ採用（後続は無視）・継続行（行末 `\\`）は1論理行へ結合してから
    キーを取り出す。"""
    nodes, _edges, _flags = _build()
    names = [n["name"] for n in nodes if n["path"] == "configkeys/app.properties"
             and n["label"] == "Config"]
    assert names.count("tax.rate") == 1                  # 重複キーは1ノードだけ
    assert "long.note" in names                          # 継続行が1キーへ結合されている


def test_config_key_reference_from_java_resolves_to_both_properties_and_yaml_same_key():
    """`tax.rate` は `configkeys/app.properties` と `configkeys/config.yml` の両方に定義されており、
    `@Value("${tax.rate}")` はその両方へ（A9＝同一 top_scope 内の同名 `Config` キー全件）。"""
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    order_service = ("Module", "OrderService", "configkeys/OrderService.java")
    tax_props = ("Config", "tax.rate", "configkeys/app.properties")
    tax_yaml = ("Config", "tax.rate", "configkeys/config.yml")
    assert ("ACCESSES", order_service, tax_props, "config_key") in ek
    assert ("ACCESSES", order_service, tax_yaml, "config_key") in ek


def test_config_key_reference_connects_to_dev_and_prod_env_files_both():
    """A9: `System.getProperty("db.url")` は環境別（dev/prod）の同名キー**両方**へ接続する
    （通常の最近傍/ambiguous 判定を迂回する特例）。"""
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    order_service = ("Module", "OrderService", "configkeys/OrderService.java")
    dev_db_url = ("Config", "db.url", "configkeys/env/dev/app.properties")
    prod_db_url = ("Config", "db.url", "configkeys/env/prod/app.properties")
    assert ("ACCESSES", order_service, dev_db_url, "config_key") in ek
    assert ("ACCESSES", order_service, prod_db_url, "config_key") in ek


def test_config_key_reference_to_db_url_connects_to_exactly_three_files_with_distinct_cids():
    """A9 の全件接続は `db.url` を持つ top/env-dev/env-prod の**3本ちょうど**——`configkeys/`
    直下（top）・`env/dev/`・`env/prod/` それぞれの `app.properties` が同名キーを持つため。
    3本の宛先は `name` こそ同じ `db.url` だが、`rel_path`（ひいては cid・`"key:"` 接頭辞込みの
    名前空間分離により primary の cid とも衝突しない）で区別される別ノードであることを固定する。"""
    nodes, edges, _flags = _build()
    order_service_cid = next(n["cid"] for n in nodes if n["path"] == "configkeys/OrderService.java"
                              and n["label"] == "Module")
    matched = [e for e in edges if e["type"] == "ACCESSES" and e["src"] == order_service_cid
               and e.get("via") == "config_key"]
    by_cid = {n["cid"]: n for n in nodes}
    db_url_edges = [e for e in matched if by_cid[e["dst"]]["name"] == "db.url"]
    assert len(db_url_edges) == 3
    dst_paths = {by_cid[e["dst"]]["path"] for e in db_url_edges}
    assert dst_paths == {
        "configkeys/app.properties",
        "configkeys/env/dev/app.properties",
        "configkeys/env/prod/app.properties",
    }
    dst_cids = {e["dst"] for e in db_url_edges}
    assert len(dst_cids) == 3                            # rel_path で区別される別ノード（cid も別）


def test_config_key_reference_to_nonexistent_key_is_flagged_unresolved_not_silently_dropped():
    _nodes, _edges, flags = _build()
    missing = [f for f in flags if f.get("reason") == "unresolved"
               and f.get("kind") == "Config" and f.get("name") == "no.such.key"]
    assert len(missing) == 1
    assert missing[0]["from"] == "configkeys/OrderService.java"


# --- S3' 残課題（XML children・`spring/`）: bean id/name/property/alias・struts constant ---

def test_xml_bean_identity_becomes_config_child_via_contains():
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    config = ("Config", "applicationContext.xml", "spring/applicationContext.xml")
    order_service_key = ("Config", "orderService", "spring/applicationContext.xml")
    assert ("CONTAINS", config, order_service_key, None) in ek


def test_xml_bean_property_becomes_dotted_config_child():
    nodes, _edges, _flags = _build()
    names = [n["name"] for n in nodes if n["path"] == "spring/applicationContext.xml"
              and n["label"] == "Config"]
    assert "orderService.timeout" in names


def test_xml_alias_becomes_config_child_keyed_by_alias_name():
    nodes, _edges, _flags = _build()
    names = [n["name"] for n in nodes if n["path"] == "spring/applicationContext.xml"
              and n["label"] == "Config"]
    assert "orderServiceAlias" in names


def test_struts_constant_becomes_config_child():
    nodes, _edges, _flags = _build()
    names = [n["name"] for n in nodes if n["path"] == "struts/struts.xml" and n["label"] == "Config"]
    assert "struts.i18n.encoding" in names


def test_java_get_bean_or_qualifier_resolves_to_xml_bean_config_key():
    """新設の `getBean`/`@Qualifier` 設定キー抽出（S3' 追補）で、`spring/com/acme/a/OrderService.java`
    の `orderService` 参照が `applicationContext.xml` の bean children へ A9 で接続する。"""
    nodes, edges, _flags = _build()
    ek = _edge_keys(nodes, edges)
    order_service_module = ("Module", "OrderService", "spring/com/acme/a/OrderService.java")
    order_service_key = ("Config", "orderService", "spring/applicationContext.xml")
    assert ("ACCESSES", order_service_module, order_service_key, "config_key") in ek


def test_java_resource_name_reference_to_missing_key_is_unresolved():
    _nodes, _edges, flags = _build()
    missing = [f for f in flags if f.get("reason") == "unresolved" and f.get("kind") == "Config"
               and f.get("name") == "mailer" and f.get("from") == "spring/com/acme/a/OrderService.java"]
    assert len(missing) == 1
    assert missing[0]["via"] == "config_key"
    assert "line" in missing[0]


# --- 語彙・via の健全性（アナライザが語彙外を返していない・§8）---

def test_no_unknown_label_edge_type_or_via_flags():
    _nodes, _edges, flags = _build()
    reasons = {f["reason"] for f in flags}
    assert "unknown_label" not in reasons
    assert "unknown_edge_type" not in reasons
    assert "unknown_via" not in reasons


def test_analyzer_provenance_is_recorded_on_config_nodes():
    nodes, _edges, _flags = _build()
    cfg = next(n for n in nodes if n["path"] == "spring/applicationContext.xml")
    assert cfg["analyzer"] == "xml_config"


# --- 拡張子集合の単一の真実源（§6・§8）: .xml/.properties/.yml は登録アナライザ側の「コード」
#     判定になり、軽量テキスト枠（text_kind）経路には落ちない ---

def test_xml_properties_yaml_are_classified_as_registered_code_not_generic_text(monkeypatch):
    assert {".xml", ".properties", ".yaml", ".yml"} <= registry.registered_extensions()

    for rel, ext in (("f.xml", ".xml"), ("f.properties", ".properties"), ("f.yml", ".yml")):
        result = corpus_docs.classify_document(rel, ext, lambda: "", allow_content_sniff=False)
        assert result["kind"] == "code", (rel, result)
        assert result["had_code_candidates"] is True
