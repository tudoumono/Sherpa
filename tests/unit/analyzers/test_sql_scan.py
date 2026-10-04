"""`_sql_scan` の単体テスト（DDL・COBOL EXEC SQL・MyBatis で共通の SQL 字句スキャナ）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers import _sql_scan


def _names(text: str) -> list:
    return [name for name, _offset in _sql_scan.table_refs(_sql_scan.sanitize(text))]


# ---- sanitize ----

def test_sanitize_blanks_line_comment_to_end_of_physical_line():
    text = "SELECT * FROM ORDERS -- trailing comment\nWHERE 1=1"
    out = _sql_scan.sanitize(text)
    assert len(out) == len(text)
    assert "trailing comment" not in out
    assert out.endswith("\nWHERE 1=1")


def test_sanitize_blanks_block_comment_preserving_length_and_newline():
    text = "SELECT * /* multi\nline */ FROM ORDERS"
    out = _sql_scan.sanitize(text)
    assert len(out) == len(text)
    assert "multi" not in out and "line" not in out
    assert "\n" in out                                  # `line` 番号の対応を保つ


def test_sanitize_blanks_single_quoted_string_with_escaped_quote():
    text = "UPDATE T SET X = 'it''s FROM fake' WHERE 1=1"
    out = _sql_scan.sanitize(text)
    assert len(out) == len(text)
    assert "FROM fake" not in out and "FROM" not in out


@pytest.mark.parametrize("text,kept", [
    ('SELECT * FROM "orders--not-a-comment"', ['"orders--not-a-comment"']),     # 引用識別子内の -- はコメントでない
    ("SELECT * FROM `Orders`, [Customers]", ["`Orders`", "[Customers]"]),
])
def test_sanitize_preserves_quoted_identifiers(text, kept):
    out = _sql_scan.sanitize(text)
    assert all(k in out for k in kept)


def test_sanitize_dash_comment_stops_at_given_boundary_not_only_at_newline():
    """`boundaries` を渡すと、改行が無い連結断片でも `--` は次の境界オフセットで止まる（COBOL 継続結合断片用）。"""
    text = "-- END-EXEC SELECT * FROM ORDERS"
    out = _sql_scan.sanitize(text, boundaries=(text.index("SELECT"),))
    assert "SELECT * FROM ORDERS" in out and "END-EXEC" not in out
    assert _sql_scan.sanitize(text).strip() == ""       # 境界なしなら改行が無い限り末尾まで


def test_sanitize_hash_is_identifier_char_by_default_not_a_line_comment():
    """既定（`hash_line_comments=False`）では `#` は行コメントを開始しない（DB2/COBOL の `T#1`）。"""
    assert _names("SELECT * FROM T#1") == ["T#1"]


@pytest.mark.parametrize("text,names", [
    ("# FROM fake\nSELECT * FROM real", ["REAL"]),
    ("SELECT * FROM #{tbl}", []),              # `#{...}` は MyBatis のプレースホルダ＝有効でもコメントにしない
])
def test_sanitize_hash_line_comment_only_when_enabled(text, names):
    out = _sql_scan.sanitize(text, hash_line_comments=True)
    assert [n for n, _o in _sql_scan.table_refs(out)] == names


# ---- unquote_or_norm_ident ----

def test_unquote_or_norm_ident_upcases_unquoted_and_preserves_quoted_case():
    assert _sql_scan.unquote_or_norm_ident("orders") == "ORDERS"
    assert _sql_scan.unquote_or_norm_ident('"orders"') == "orders"
    assert _sql_scan.unquote_or_norm_ident("`Orders`") == "Orders"
    assert _sql_scan.unquote_or_norm_ident("[Customers]") == "Customers"


# ---- table_refs（COBOL EXEC SQL 本文と同じ入力を含む）----
NAMES_CASES = {
    "from": ("SELECT * FROM ORDERS", ["ORDERS"]),
    "join_discarding_aliases": ("SELECT * FROM ORDERS O JOIN CUSTOMERS C ON O.ID = C.ID", ["ORDERS", "CUSTOMERS"]),
    "insert_into_ignores_column_list": ("INSERT INTO ORDER_LINES (ORDER_ID, QTY) VALUES (1, 2)", ["ORDER_LINES"]),
    "update_lowercase_normalized": ("UPDATE customers SET STATUS = 'X' WHERE ID = 1", ["CUSTOMERS"]),
    "delete_from": ("DELETE FROM ORDERS WHERE ID = 1", ["ORDERS"]),
    "merge_into": ("MERGE INTO ORDERS USING SRC ON (ORDERS.ID = SRC.ID) WHEN MATCHED THEN UPDATE SET X = 1", ["ORDERS"]),
    "comma_separated": ("SELECT * FROM ORDERS, CUSTOMERS", ["ORDERS", "CUSTOMERS"]),
    "schema_qualified_drops_schema": ("SELECT * FROM BILLING.ORDERS", ["ORDERS"]),
    "host_variable_excluded": ("SELECT * FROM :HOST-TABLE", []),
    "subquery_excluded": ("SELECT * FROM (SELECT 1) X", []),
    "mybatis_hash_placeholder_excluded": ("SELECT * FROM #{tbl}", []),
    "mybatis_dollar_placeholder_excluded": ("SELECT * FROM ${tbl}", []),
    "double_quoted_case_preserved": ('SELECT * FROM "orders"', ["orders"]),
    "backtick_case_preserved": ("SELECT * FROM `Orders`", ["Orders"]),
    "bracket_case_preserved": ("SELECT * FROM [Customers]", ["Customers"]),
    "select_into_host_vars": ("SELECT COL1, COL2 INTO :WS-COL1, :WS-COL2 FROM ORDERS", ["ORDERS"]),
    "insert_values_host_vars": ("INSERT INTO ORDER_LINES (ORDER_ID, QTY) VALUES (:WS-ORDER-ID, :WS-QTY)", ["ORDER_LINES"]),
    "update_host_var_where": ("UPDATE customers SET STATUS = 'X' WHERE ID = :WS-ID", ["CUSTOMERS"]),
    "join_with_qualified_columns": (
        "SELECT O.ID, C.NAME FROM ORDERS O JOIN CUSTOMERS C ON O.CUST_ID = C.ID", ["ORDERS", "CUSTOMERS"]),
    "string_literal_with_clause_keyword": ("UPDATE T SET X = 'FROM U'", ["T"]),
    "line_and_block_comments": (
        "-- FROM FAKE_IN_LINE_COMMENT\nSELECT * FROM ORDERS /* FROM FAKE_IN_BLOCK_COMMENT */", ["ORDERS"]),
    "end_exec_in_string_literal": ("SELECT 'END-EXEC' AS X FROM ORDERS", ["ORDERS"]),
    "clause_keyword_inside_quoted_identifier": ('SELECT "x FROM fake" FROM real', ["REAL"]),
    "double_quote_escape_in_identifier": ('FROM "a""b"', ['a"b']),
    # CTE 名は FROM/JOIN 候補から除外
    "cte_name_excluded": ("WITH x AS (SELECT * FROM a) SELECT * FROM x", ["A"]),
    "cte_with_column_list": ("WITH x (c1, c2) AS (SELECT * FROM a) SELECT * FROM x", ["A"]),
    "multiple_ctes": (
        "WITH x AS (SELECT * FROM a), y AS (SELECT * FROM b) SELECT * FROM x, y, real_table", ["A", "B", "REAL_TABLE"]),
    "with_recursive": ("WITH RECURSIVE x AS (SELECT * FROM a) SELECT * FROM x", ["A"]),
    "with_recursive_multiple_with_column_list": (
        "WITH RECURSIVE x (c1, c2) AS (SELECT * FROM a), y AS (SELECT * FROM b) SELECT * FROM x, y", ["A", "B"]),
    "schema_qualified_cte_name_not_excluded": ("WITH x AS (SELECT * FROM a) SELECT * FROM schema.x", ["A", "X"]),
    "cte_scope_limited_to_own_statement": (
        "WITH recent AS (SELECT * FROM source) SELECT * FROM recent; SELECT * FROM recent;", ["SOURCE", "RECENT"]),
    "quoted_cte_name_excluded_case_preserved": (
        'WITH "recent" AS (SELECT * FROM source) SELECT * FROM "recent"', ["SOURCE"]),
    # Oracle DB link・LATERAL
    "oracle_db_link_excluded": ("FROM schema.table@dblink", []),
    "db_link_does_not_block_next_table": ("FROM schema.table@dblink, real_table", ["REAL_TABLE"]),
    "lateral_after_join_excluded": ("SELECT * FROM ORDERS O JOIN LATERAL (SELECT 1) L ON TRUE", ["ORDERS"]),
}


@pytest.mark.parametrize("text,names", NAMES_CASES.values(), ids=NAMES_CASES)
def test_table_refs_names(text, names):
    assert _names(text) == names


def test_table_refs_offset_points_at_the_identifier_position():
    text = "SELECT * FROM ORDERS"
    [(name, offset)] = _sql_scan.table_refs(_sql_scan.sanitize(text))
    assert name == "ORDERS" and text[offset:offset + len("ORDERS")] == "ORDERS"


def test_table_refs_base_offset_is_added_to_returned_offsets():
    text = "FROM ORDERS"
    [(name, offset)] = _sql_scan.table_refs(_sql_scan.sanitize(text), base_offset=100)
    assert name == "ORDERS" and offset == 100 + text.index("ORDERS")
