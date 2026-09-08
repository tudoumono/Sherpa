"""`world_graph.build_world()` 経由の SQL/DDL・EXEC SQL 統合テスト（アナライザ拡張 S2・
docs/proposals/2026-09-05-アナライザ拡張.md §4(a)/(d)/(g)）。

`fixtures/corpus/sql1`（DDL 3表＋うち1つ引用識別子＋制約行混在＋CREATE VIEW/ALTER TABLE＋
コメント/文字列内の CREATE TABLE 罠＋EXEC SQL を含む COBOL）を実際に `build_world()` へ通し、
`SqlDdlAnalyzer` の複数 `CREATE TABLE`（A10・`DefResult.extras`）ノード化と、COBOL 側
`EXEC SQL`→`Table`/`ACCESSES(via=exec_sql)` の解決を固定する。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "sql1"
WORLD_ID = "sql1_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]) for n in nodes}


def _cid_of(nodes, label, name, path):
    return next(n["cid"] for n in nodes if n["label"] == label and n["name"] == name and n["path"] == path)


# --- primary/extras（A10）が3表すべてノード化・索引登録されること ---

def test_all_three_create_table_become_table_nodes_primary_and_extras():
    nodes, _edges, _flags = _build()
    keys = _node_keys(nodes)
    assert ("Table", "ORDERS", "schema.sql") in keys           # primary（最初の CREATE TABLE）
    assert ("Table", "ORDER_LINES", "schema.sql") in keys       # extras[0]（引用識別子）
    assert ("Table", "CUSTOMERS", "schema.sql") in keys         # extras[1]


def test_quoted_identifier_table_preserves_case_as_written():
    """`CREATE TABLE "ORDER_LINES" (...)` は引用識別子——大文字小文字を区別してそのまま使う。
    ここでは大文字で書いたため EXEC SQL 側（非引用＝正規化後も大文字）と一致して解決する。"""
    nodes, _edges, _flags = _build()
    assert ("Table", "ORDER_LINES", "schema.sql") in _node_keys(nodes)


# --- Table -CONTAINS-> DataItem（列定義。制約行は列にしない）---

def test_table_contains_column_children_excluding_constraint_lines():
    nodes, edges, _flags = _build()
    orders_cid = _cid_of(nodes, "Table", "ORDERS", "schema.sql")
    children = {n["name"] for e in edges if e["type"] == "CONTAINS" and e["src"] == orders_cid
                for n in nodes if n["cid"] == e["dst"]}
    assert children == {"ID", "CUSTOMER_ID", "MEMO"}             # CONSTRAINT 行は含まれない

    order_lines_cid = _cid_of(nodes, "Table", "ORDER_LINES", "schema.sql")
    ol_children = {n["name"] for e in edges if e["type"] == "CONTAINS" and e["src"] == order_lines_cid
                   for n in nodes if n["cid"] == e["dst"]}
    assert ol_children == {"ORDER_ID", "LINE_NO", "QTY"}         # PRIMARY KEY 行は含まれない

    customers_cid = _cid_of(nodes, "Table", "CUSTOMERS", "schema.sql")
    cust_children = {n["name"] for e in edges if e["type"] == "CONTAINS" and e["src"] == customers_cid
                     for n in nodes if n["cid"] == e["dst"]}
    assert cust_children == {"ID", "NAME"}                       # KEY 行は含まれない


# --- CREATE VIEW/ALTER TABLE/コメント・文字列内の罠は Table を作らない ---

def test_view_and_alter_and_commented_or_stringed_fake_tables_produce_no_extra_nodes():
    nodes, _edges, _flags = _build()
    table_names = {n["name"] for n in nodes if n["label"] == "Table"}
    assert table_names == {"ORDERS", "ORDER_LINES", "CUSTOMERS"}
    assert not any("FAKE" in n["name"] for n in nodes)


def test_create_view_and_alter_table_are_reported_as_ddl_unsupported_dropped():
    _nodes, _edges, flags = _build()
    ddl_unsupported = [f for f in flags if f.get("why") == "ddl_unsupported" and f.get("from") == "schema.sql"]
    assert len(ddl_unsupported) == 2


# --- COBOL EXEC SQL → Table/ACCESSES(via=exec_sql) ---

def test_module_accesses_all_three_tables_via_exec_sql():
    nodes, edges, _flags = _build()
    billing_cid = _cid_of(nodes, "Module", "BILLING", "billing.cbl")
    accesses = {(n["name"], e.get("via")) for e in edges if e["type"] == "ACCESSES" and e["src"] == billing_cid
                for n in nodes if n["cid"] == e["dst"]}
    assert accesses == {("ORDERS", "exec_sql"), ("ORDER_LINES", "exec_sql"), ("CUSTOMERS", "exec_sql")}


def test_select_into_host_variable_does_not_create_a_spurious_reference():
    """`SELECT ... INTO :host FROM ORDERS` はホスト変数側を参照候補にしない
    （`ACCESSES` の宛先はすべて実在する `Table` ノードのみ）。"""
    nodes, edges, _flags = _build()
    billing_cid = _cid_of(nodes, "Module", "BILLING", "billing.cbl")
    table_cids = {n["cid"] for n in nodes if n["label"] == "Table"}
    for e in edges:
        if e["type"] == "ACCESSES" and e["src"] == billing_cid:
            assert e["dst"] in table_cids


# --- DDL に無いテーブル（SHIPPING_INFO）は unresolved flag のみ（ノード化しない）---

def test_table_absent_from_ddl_yields_unresolved_flag_and_no_node():
    nodes, _edges, flags = _build()
    assert not any(n["label"] == "Table" and n["name"] == "SHIPPING_INFO" for n in nodes)
    unresolved = [f for f in flags if f.get("reason") == "unresolved" and f.get("name") == "SHIPPING_INFO"]
    assert len(unresolved) == 1


# --- DECLARE CURSOR（動的 SQL）は Dropped のみ・テーブル抽出はしない ---

def test_declare_cursor_block_is_dropped_as_exec_sql_dynamic():
    _nodes, _edges, flags = _build()
    dynamic = [f for f in flags if f.get("why") == "exec_sql_dynamic"]
    assert len(dynamic) == 1
