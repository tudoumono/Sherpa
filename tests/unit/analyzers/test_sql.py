"""`SqlDdlAnalyzer` の単体テスト（アナライザ拡張 §4(a)/(g)＝A1・A10）。"""
from __future__ import annotations

from sherpa.ingest.analyzers._base import Analyzer
from sherpa.ingest.analyzers.sql import SqlDdlAnalyzer

A = SqlDdlAnalyzer()


def test_extensions_and_name():
    assert A.extensions == frozenset({".sql"})
    assert A.name == "sql"
    assert A.doctype == "sql"


def test_accepts_all_sql_files_without_content_inspection():
    """§6: `.sql` は拡張子内を全件受理する——`accepts()` は既定のままオーバーライドしていない。"""
    assert SqlDdlAnalyzer.accepts is Analyzer.accepts


# --- 単一 CREATE TABLE（primary のみ）---

def test_single_create_table_becomes_table_primary_with_column_children():
    text = (
        "CREATE TABLE orders (\n"
        "    id INT PRIMARY KEY,\n"
        "    customer_id INT NOT NULL,\n"
        "    CONSTRAINT fk_customer FOREIGN KEY (customer_id) REFERENCES customers(id)\n"
        ");\n"
    )
    res = A.collect_defs(text, "orders.sql")
    assert res.primary is not None
    assert res.primary.label == "Table" and res.primary.name == "ORDERS"     # 非引用＝大文字正規化
    names = [(c.label, c.name, c.cid_key) for c in res.children]
    assert names == [("DataItem", "ID", "ORDERS.ID"), ("DataItem", "CUSTOMER_ID", "ORDERS.CUSTOMER_ID")]
    assert res.extras == [] and res.dropped == []


def test_quoted_identifier_table_and_columns_preserve_case():
    text = 'CREATE TABLE "OrderLines" (\n    "LineNo" INT,\n    qty INT\n);\n'
    res = A.collect_defs(text, "order_lines.sql")
    assert res.primary.name == "OrderLines"                       # 引用識別子はそのまま（大文字小文字区別）
    names = [(c.name, c.cid_key) for c in res.children]
    assert names == [("LineNo", "OrderLines.LineNo"), ("QTY", "OrderLines.QTY")]


def test_schema_qualified_name_drops_schema_into_extra():
    text = "CREATE TABLE billing.orders (id INT);\n"
    res = A.collect_defs(text, "orders.sql")
    assert res.primary.name == "ORDERS"
    assert res.primary.extra == {"schema": "BILLING"}


def test_if_not_exists_is_accepted():
    text = "CREATE TABLE IF NOT EXISTS orders (id INT);\n"
    res = A.collect_defs(text, "orders.sql")
    assert res.primary is not None and res.primary.name == "ORDERS"


# --- 複数 CREATE TABLE（A10・extras）---

def test_multiple_create_table_first_is_primary_rest_are_extras():
    text = (
        "CREATE TABLE orders (id INT);\n"
        "CREATE TABLE order_lines (order_id INT);\n"
        "CREATE TABLE customers (id INT);\n"
    )
    res = A.collect_defs(text, "schema.sql")
    assert res.primary.name == "ORDERS"
    assert [g.primary.name for g in res.extras] == ["ORDER_LINES", "CUSTOMERS"]
    assert [c.name for c in res.extras[0].children] == ["ORDER_ID"]
    assert [c.name for c in res.extras[1].children] == ["ID"]


# --- 制約行は列にしない ---

def test_constraint_lines_are_not_columns():
    text = (
        "CREATE TABLE t (\n"
        "    id INT,\n"
        "    name VARCHAR(50),\n"
        "    PRIMARY KEY (id),\n"
        "    FOREIGN KEY (id) REFERENCES other(id),\n"
        "    CONSTRAINT uq UNIQUE (name),\n"
        "    UNIQUE (name),\n"
        "    INDEX idx_name (name),\n"
        "    KEY idx_name2 (name),\n"
        "    CHECK (id > 0)\n"
        ");\n"
    )
    res = A.collect_defs(text, "t.sql")
    assert [c.name for c in res.children] == ["ID", "NAME"]


# --- 未対応 DDL/DML → Dropped（定義・参照は作らない）---

def test_create_view_yields_no_primary_and_ddl_unsupported_dropped():
    text = "CREATE VIEW active_orders AS SELECT * FROM orders WHERE status = 'NEW';\n"
    res = A.collect_defs(text, "view.sql")
    assert res.primary is None
    assert [d.reason for d in res.dropped] == ["ddl_unsupported"]


def test_create_or_replace_procedure_function_trigger_yield_ddl_unsupported():
    text = (
        "CREATE OR REPLACE PROCEDURE p1() BEGIN END;\n"
        "CREATE FUNCTION f1() RETURNS INT BEGIN END;\n"
        "CREATE TRIGGER tr1 BEFORE INSERT ON t FOR EACH ROW BEGIN END;\n"
    )
    res = A.collect_defs(text, "proc.sql")
    assert res.primary is None
    assert [d.reason for d in res.dropped] == ["ddl_unsupported"] * 3


def test_alter_table_yields_ddl_unsupported_dropped():
    text = "ALTER TABLE orders ADD COLUMN notes VARCHAR(255);\n"
    res = A.collect_defs(text, "alter.sql")
    assert res.primary is None
    assert [d.reason for d in res.dropped] == ["ddl_unsupported"]


def test_dml_only_file_yields_dml_only_dropped():
    text = "INSERT INTO orders (id) VALUES (1);\nSELECT * FROM orders;\n"
    res = A.collect_defs(text, "dml.sql")
    assert res.primary is None
    assert [d.reason for d in res.dropped] == ["dml_only"]


def test_empty_or_irrelevant_file_yields_no_defs_and_no_dropped():
    res = A.collect_defs("-- just a comment\n", "empty.sql")
    assert res.primary is None and res.dropped == []


# --- コメント/文字列リテラル内は無視する ---

def test_create_table_inside_line_comment_is_ignored():
    text = "-- CREATE TABLE FAKE (x INT);\nCREATE TABLE real_table (id INT);\n"
    res = A.collect_defs(text, "t.sql")
    assert res.primary.name == "REAL_TABLE"
    assert res.extras == []


def test_create_table_inside_block_comment_is_ignored():
    text = "/* CREATE TABLE FAKE (x INT); */\nCREATE TABLE real_table (id INT);\n"
    res = A.collect_defs(text, "t.sql")
    assert res.primary.name == "REAL_TABLE"
    assert res.extras == []


def test_create_table_inside_string_literal_is_ignored():
    text = (
        "CREATE TABLE orders (\n"
        "    id INT,\n"
        "    memo VARCHAR(255) DEFAULT 'trap: CREATE TABLE FAKE (y INT)'\n"
        ");\n"
    )
    res = A.collect_defs(text, "t.sql")
    assert res.primary.name == "ORDERS"
    assert [c.name for c in res.children] == ["ID", "MEMO"]
    assert res.extras == []


def test_string_literal_escaped_quote_does_not_end_string_early():
    text = (
        "CREATE TABLE orders (\n"
        "    id INT,\n"
        "    memo VARCHAR(255) DEFAULT 'it''s a trap: CREATE TABLE FAKE (y INT)'\n"
        ");\n"
    )
    res = A.collect_defs(text, "t.sql")
    assert res.primary.name == "ORDERS"
    assert [c.name for c in res.children] == ["ID", "MEMO"]


# --- 引用識別子の内側の `--`/`/* */` はコメントと誤解釈しない ---

def test_double_quoted_identifier_containing_comment_marker_is_not_truncated():
    """`"a--b"` の `--` は行コメントの開始と誤認しない——`)` まで読み切って Table にする。"""
    text = 'CREATE TABLE "a--b" (id INT);\n'
    res = A.collect_defs(text, "t.sql")
    assert res.primary is not None
    assert res.primary.name == "a--b"
    assert res.dropped == []


def test_bracket_quoted_identifier_containing_comment_marker_is_not_truncated():
    """`[a--b]` も同様——角括弧識別子の中身はコメントと誤解釈しない。"""
    text = "CREATE TABLE [a--b] (id INT);\n"
    res = A.collect_defs(text, "t.sql")
    assert res.primary is not None
    assert res.primary.name == "a--b"
    assert res.dropped == []


# --- extract_refs は常に空（DDL は他ファイルを参照しない）---

def test_extract_refs_is_always_empty():
    text = "CREATE TABLE orders (id INT);\nALTER TABLE orders ADD COLUMN x INT;\n"
    res = A.extract_refs(text, "t.sql")
    assert res.refs == [] and res.dropped == []


# --- schema 除去後の同名 Table は cid が衝突するため2件目以降を Dropped にする ---

def test_schema_qualified_same_table_name_collision_only_first_is_defined():
    text = (
        "CREATE TABLE a.orders (id INT);\n"
        "CREATE TABLE b.orders (id INT, extra INT);\n"
    )
    res = A.collect_defs(text, "schema.sql")
    assert res.primary.name == "ORDERS" and res.primary.extra == {"schema": "A"}
    assert res.extras == []
    assert [d.reason for d in res.dropped] == ["table_name_collision"]
    assert res.dropped[0].snippet == "b.orders"


# --- CREATE TABLE 方言（GLOBAL/LOCAL TEMPORARY・CTAS）---

def test_global_temporary_table_is_accepted():
    text = "CREATE GLOBAL TEMPORARY TABLE staging (id INT);\n"
    res = A.collect_defs(text, "t.sql")
    assert res.primary is not None and res.primary.name == "STAGING"
    assert [c.name for c in res.children] == ["ID"]
    assert res.dropped == []


def test_local_temporary_table_is_accepted():
    text = "CREATE LOCAL TEMPORARY TABLE staging (id INT);\n"
    res = A.collect_defs(text, "t.sql")
    assert res.primary is not None and res.primary.name == "STAGING"


def test_create_table_as_select_yields_table_with_no_columns():
    text = "CREATE TABLE recent_orders AS SELECT * FROM orders WHERE status = 'NEW';\n"
    res = A.collect_defs(text, "t.sql")
    assert res.primary is not None
    assert res.primary.name == "RECENT_ORDERS" and res.children == []
    assert res.dropped == []


# --- CREATE TABLE/ALTER TABLE 以外の CREATE 方言は必ず ddl_unsupported にする ---

def test_unrecognized_create_dialect_is_reported_as_ddl_unsupported_not_silently_dropped():
    text = "CREATE INDEX idx_orders_id ON orders (id);\nCREATE SEQUENCE seq1;\n"
    res = A.collect_defs(text, "t.sql")
    assert res.primary is None
    assert [d.reason for d in res.dropped] == ["ddl_unsupported", "ddl_unsupported"]
