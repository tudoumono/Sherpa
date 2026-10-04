"""`SqlDdlAnalyzer` の単体テスト（アナライザ拡張 §4(a)/(g)＝A1・A10）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.sql import SqlDdlAnalyzer

A = SqlDdlAnalyzer()
UNS = "ddl_unsupported"


def test_extensions_and_name():
    assert A.extensions == frozenset({".sql"})
    assert A.name == "sql"
    assert A.doctype == "sql"


def test_accepts_all_sql_files_without_content_inspection():
    assert SqlDdlAnalyzer.accepts is Analyzer.accepts


def DI(name, key):
    return ("DataItem", name, key)


# (入力, primary 名（None=primary なし）, primary.extra（None は検査しない）, 列 children（None は検査しない）,
#  extras の primary 名列（None は検査しない）, Dropped の reason 列（None は検査しない）)
CASES = {
    "single_create_table_with_columns": (
        "CREATE TABLE orders (\n    id INT PRIMARY KEY,\n    customer_id INT NOT NULL,\n"
        "    CONSTRAINT fk_customer FOREIGN KEY (customer_id) REFERENCES customers(id)\n);\n",
        "ORDERS", None, [DI("ID", "ORDERS.ID"), DI("CUSTOMER_ID", "ORDERS.CUSTOMER_ID")], [], []),
    "quoted_identifiers_preserve_case": (
        'CREATE TABLE "OrderLines" (\n    "LineNo" INT,\n    qty INT\n);\n', "OrderLines", None,
        [DI("LineNo", "OrderLines.LineNo"), DI("QTY", "OrderLines.QTY")], None, None),
    "schema_dropped_into_extra": ("CREATE TABLE billing.orders (id INT);\n", "ORDERS", {"schema": "BILLING", "qualified_name": "BILLING.ORDERS"}, None, None, None),
    "if_not_exists": ("CREATE TABLE IF NOT EXISTS orders (id INT);\n", "ORDERS", None, None, None, None),
    "multiple_create_table_rest_are_extras": (
        "CREATE TABLE orders (id INT);\nCREATE TABLE order_lines (order_id INT);\nCREATE TABLE customers (id INT);\n",
        "ORDERS", None, None, ["ORDER_LINES", "CUSTOMERS"], None),
    "constraint_lines_not_columns": (
        "CREATE TABLE t (\n    id INT,\n    name VARCHAR(50),\n    PRIMARY KEY (id),\n"
        "    FOREIGN KEY (id) REFERENCES other(id),\n    CONSTRAINT uq UNIQUE (name),\n    UNIQUE (name),\n"
        "    INDEX idx_name (name),\n    KEY idx_name2 (name),\n    CHECK (id > 0)\n);\n",
        "T", None, [DI("ID", "T.ID"), DI("NAME", "T.NAME")], None, None),
    # 未対応 DDL/DML → Dropped（定義・参照は作らない）
    "create_view": ("CREATE VIEW active_orders AS SELECT * FROM orders WHERE status = 'NEW';\n", None, None, None, None, [UNS]),
    "procedure_function_trigger": (
        "CREATE OR REPLACE PROCEDURE p1() BEGIN END;\nCREATE FUNCTION f1() RETURNS INT BEGIN END;\n"
        "CREATE TRIGGER tr1 BEFORE INSERT ON t FOR EACH ROW BEGIN END;\n", None, None, None, None, [UNS] * 3),
    "alter_table": ("ALTER TABLE orders ADD COLUMN notes VARCHAR(255);\n", None, None, None, None, [UNS]),
    "dml_only": ("INSERT INTO orders (id) VALUES (1);\nSELECT * FROM orders;\n", None, None, None, None, ["dml_only"]),
    "empty_or_irrelevant": ("-- just a comment\n", None, None, None, None, []),
    "unrecognized_create_dialect_reported": (
        "CREATE INDEX idx_orders_id ON orders (id);\nCREATE SEQUENCE seq1;\n", None, None, None, None, [UNS, UNS]),
    # コメント・文字列リテラル内は無視
    "create_table_in_line_comment": (
        "-- CREATE TABLE FAKE (x INT);\nCREATE TABLE real_table (id INT);\n", "REAL_TABLE", None, None, [], None),
    "create_table_in_block_comment": (
        "/* CREATE TABLE FAKE (x INT); */\nCREATE TABLE real_table (id INT);\n", "REAL_TABLE", None, None, [], None),
    "create_table_in_string_literal": (
        "CREATE TABLE orders (\n    id INT,\n    memo VARCHAR(255) DEFAULT 'trap: CREATE TABLE FAKE (y INT)'\n);\n",
        "ORDERS", None, [DI("ID", "ORDERS.ID"), DI("MEMO", "ORDERS.MEMO")], [], None),
    "escaped_quote_does_not_end_string_early": (
        "CREATE TABLE orders (\n    id INT,\n    memo VARCHAR(255) DEFAULT 'it''s a trap: CREATE TABLE FAKE (y INT)'\n);\n",
        "ORDERS", None, [DI("ID", "ORDERS.ID"), DI("MEMO", "ORDERS.MEMO")], None, None),
    # 引用識別子の内側の `--` はコメントと誤解釈しない
    "double_quoted_identifier_with_comment_marker": ('CREATE TABLE "a--b" (id INT);\n', "a--b", None, None, None, []),
    "bracket_quoted_identifier_with_comment_marker": ("CREATE TABLE [a--b] (id INT);\n", "a--b", None, None, None, []),
    # 同じ schema＋名前の重複は 2件目を Dropped
    "same_schema_name_collision": (
        "CREATE TABLE a.orders (id INT);\nCREATE TABLE a.orders (id INT, extra INT);\n",
        "ORDERS", {"schema": "A", "qualified_name": "A.ORDERS"}, [DI("ID", "ORDERS.ID")], [], ["table_name_collision"]),
    # 方言
    "global_temporary_table": ("CREATE GLOBAL TEMPORARY TABLE staging (id INT);\n", "STAGING", None, [DI("ID", "STAGING.ID")], None, []),
    "local_temporary_table": ("CREATE LOCAL TEMPORARY TABLE staging (id INT);\n", "STAGING", None, None, None, None),
    "create_table_as_select_has_no_columns": (
        "CREATE TABLE recent_orders AS SELECT * FROM orders WHERE status = 'NEW';\n", "RECENT_ORDERS", None, [], None, []),
}


@pytest.mark.parametrize("text,name,extra,children,extras,dropped", CASES.values(), ids=CASES)
def test_collect_defs(text, name, extra, children, extras, dropped):
    res = A.collect_defs(text, "t.sql")
    if name is None:
        assert res.primary is None
    else:
        assert res.primary is not None and res.primary.label == "Table" and res.primary.name == name
    assert extra is None or res.primary.extra == extra
    if children is not None:
        assert [(c.label, c.name, c.cid_key) for c in res.children] == children
    if extras is not None:
        assert [g.primary.name for g in res.extras] == extras
    if dropped is not None:
        assert [d.reason for d in res.dropped] == dropped


def test_extras_carry_their_own_columns():
    res = A.collect_defs("CREATE TABLE orders (id INT);\nCREATE TABLE order_lines (order_id INT);\n"
                         "CREATE TABLE customers (id INT);\n", "schema.sql")
    assert [[c.name for c in g.children] for g in res.extras] == [["ORDER_ID"], ["ID"]]


def test_same_name_in_another_schema_is_a_separate_table_with_its_own_key():
    res = A.collect_defs("CREATE TABLE a.orders (id INT);\nCREATE TABLE b.orders (id INT);\n", "schema.sql")
    assert res.dropped == []
    assert (res.primary.key, res.extras[0].primary.key) == ("ORDERS", "B.ORDERS")
    assert res.extras[0].children[0].cid_key == "B.ORDERS.ID"


def test_collision_snippet_names_the_second_table():
    res = A.collect_defs("CREATE TABLE a.orders (id INT);\nCREATE TABLE a.orders (id INT);\n", "schema.sql")
    assert res.dropped[0].snippet == "a.orders"


def test_extract_refs_is_always_empty():
    res = A.extract_refs("CREATE TABLE orders (id INT);\nALTER TABLE orders ADD COLUMN x INT;\n", "t.sql")
    assert res.refs == [] and res.dropped == []
