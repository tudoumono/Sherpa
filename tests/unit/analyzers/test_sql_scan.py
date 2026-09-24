"""`_sql_scan` の単体テスト（アナライザ拡張 §9 S4'）。

`analyzers/sql.py`（DDL の `_sanitize`）と `analyzers/cobol.py`（EXEC SQL の `_sanitize_sql_span`/
`_extract_sql_table_names`）で重複していた SQL 字句処理を統合した共通スキャナの規則を固定する。
「cobol 互換入力」の節は `test_cobol.py` の EXEC SQL テストからテーブル抽出に関係する本文だけを
取り出し、同じテーブル名が得られることを固定する（S5b 完了後に cobol.py をこのスキャナへ差し替える
前提）。
"""
from __future__ import annotations

from sherpa.ingest.analyzers import _sql_scan


def _names(text: str) -> list:
    return [name for name, _offset in _sql_scan.table_refs(_sql_scan.sanitize(text))]


# --- sanitize: コメント ---

def test_sanitize_blanks_line_comment_to_end_of_physical_line():
    text = "SELECT * FROM ORDERS -- trailing comment\nWHERE 1=1"
    out = _sql_scan.sanitize(text)
    assert len(out) == len(text)
    assert "trailing comment" not in out
    assert out.endswith("\nWHERE 1=1")


def test_sanitize_blanks_block_comment_preserving_length_and_embedded_newline():
    text = "SELECT * /* multi\nline */ FROM ORDERS"
    out = _sql_scan.sanitize(text)
    assert len(out) == len(text)
    assert "multi" not in out and "line" not in out
    assert "\n" in out                                  # 改行は保持（`line` 番号の対応を保つ）


def test_sanitize_blanks_single_quoted_string_with_escaped_quote():
    text = "UPDATE T SET X = 'it''s FROM fake' WHERE 1=1"
    out = _sql_scan.sanitize(text)
    assert len(out) == len(text)
    assert "FROM fake" not in out
    assert "FROM" not in out                            # 文字列内の FROM は句として拾わない


def test_sanitize_preserves_double_quoted_identifier_with_dashdash_untouched():
    text = 'SELECT * FROM "orders--not-a-comment"'
    out = _sql_scan.sanitize(text)
    assert '"orders--not-a-comment"' in out             # 引用識別子内の -- はコメントと誤解釈しない


def test_sanitize_preserves_backtick_and_bracket_quoted_identifiers():
    text = "SELECT * FROM `Orders`, [Customers]"
    out = _sql_scan.sanitize(text)
    assert "`Orders`" in out and "[Customers]" in out


def test_sanitize_dash_comment_stops_at_given_boundary_not_only_at_newline():
    """`boundaries` を渡すと、改行が無い連結断片でも `--` は次の境界オフセットで止まる
    （cobol.py の継続結合断片で必要になる契約・COBOL 側からの差し替えを見越した引数）。"""
    text = "-- END-EXEC SELECT * FROM ORDERS"            # 改行なし・1断片へ連結された想定
    boundary = text.index("SELECT")
    out = _sql_scan.sanitize(text, boundaries=(boundary,))
    assert "SELECT * FROM ORDERS" in out
    assert "END-EXEC" not in out


def test_sanitize_dash_comment_without_boundaries_runs_to_end_of_text_when_no_newline():
    text = "-- END-EXEC SELECT * FROM ORDERS"
    out = _sql_scan.sanitize(text)
    assert out.strip() == ""


# --- unquote_or_norm_ident ---

def test_unquote_or_norm_ident_upcases_unquoted_and_preserves_quoted_case():
    assert _sql_scan.unquote_or_norm_ident("orders") == "ORDERS"
    assert _sql_scan.unquote_or_norm_ident('"orders"') == "orders"
    assert _sql_scan.unquote_or_norm_ident("`Orders`") == "Orders"
    assert _sql_scan.unquote_or_norm_ident("[Customers]") == "Customers"


# --- table_refs: 各句 ---

def test_table_refs_from_clause():
    assert _names("SELECT * FROM ORDERS") == ["ORDERS"]


def test_table_refs_join_clause_both_sides_discarding_aliases():
    assert _names("SELECT * FROM ORDERS O JOIN CUSTOMERS C ON O.ID = C.ID") == ["ORDERS", "CUSTOMERS"]


def test_table_refs_insert_into_ignores_column_list():
    assert _names("INSERT INTO ORDER_LINES (ORDER_ID, QTY) VALUES (1, 2)") == ["ORDER_LINES"]


def test_table_refs_update_clause_normalizes_lowercase_to_uppercase():
    assert _names("UPDATE customers SET STATUS = 'X' WHERE ID = 1") == ["CUSTOMERS"]


def test_table_refs_delete_from_clause():
    assert _names("DELETE FROM ORDERS WHERE ID = 1") == ["ORDERS"]


def test_table_refs_merge_into_clause():
    text = "MERGE INTO ORDERS USING SRC ON (ORDERS.ID = SRC.ID) WHEN MATCHED THEN UPDATE SET X = 1"
    assert _names(text) == ["ORDERS"]


def test_table_refs_comma_separated_tables_are_all_returned():
    assert _names("SELECT * FROM ORDERS, CUSTOMERS") == ["ORDERS", "CUSTOMERS"]


def test_table_refs_schema_qualified_table_name_drops_schema():
    assert _names("SELECT * FROM BILLING.ORDERS") == ["ORDERS"]


def test_table_refs_host_variable_and_subquery_lead_chars_are_excluded():
    assert _names("SELECT * FROM :HOST-TABLE") == []
    assert _names("SELECT * FROM (SELECT 1) X") == []


def test_table_refs_mybatis_dynamic_placeholder_is_excluded():
    """MyBatis の動的プレースホルダ（`#{...}`/`${...}`）をテーブル名候補として読み始めない。"""
    assert _names("SELECT * FROM #{tbl}") == []
    assert _names("SELECT * FROM ${tbl}") == []


def test_table_refs_quoted_identifiers_preserved_case_sensitively():
    assert _names('SELECT * FROM "orders"') == ["orders"]
    assert _names("SELECT * FROM `Orders`") == ["Orders"]
    assert _names("SELECT * FROM [Customers]") == ["Customers"]


def test_table_refs_offset_points_at_the_identifier_position():
    text = "SELECT * FROM ORDERS"
    [(name, offset)] = _sql_scan.table_refs(_sql_scan.sanitize(text))
    assert name == "ORDERS"
    assert text[offset:offset + len("ORDERS")] == "ORDERS"


def test_table_refs_base_offset_is_added_to_returned_offsets():
    text = "FROM ORDERS"
    [(name, offset)] = _sql_scan.table_refs(_sql_scan.sanitize(text), base_offset=100)
    assert name == "ORDERS"
    assert offset == 100 + text.index("ORDERS")


# --- cobol.py 互換入力（`test_cobol.py` の EXEC SQL テストと同じテーブル名になることを固定・
#     S5b 完了後に cobol.py の `_extract_sql_table_names` をこのスキャナへ差し替える前提）---

def test_cobol_compat_select_into_host_var_from_table_yields_from_only():
    text = "SELECT COL1, COL2 INTO :WS-COL1, :WS-COL2 FROM ORDERS"
    assert _names(text) == ["ORDERS"]


def test_cobol_compat_insert_into_ignoring_column_list():
    text = "INSERT INTO ORDER_LINES (ORDER_ID, QTY) VALUES (:WS-ORDER-ID, :WS-QTY)"
    assert _names(text) == ["ORDER_LINES"]


def test_cobol_compat_update_lowercase_table_name_is_normalized_uppercase():
    text = "UPDATE customers SET STATUS = 'X' WHERE ID = :WS-ID"
    assert _names(text) == ["CUSTOMERS"]


def test_cobol_compat_join_yields_both_tables_discarding_aliases():
    text = "SELECT O.ID, C.NAME FROM ORDERS O JOIN CUSTOMERS C ON O.CUST_ID = C.ID"
    assert _names(text) == ["ORDERS", "CUSTOMERS"]


def test_cobol_compat_schema_qualified_table_name_drops_schema():
    assert _names("SELECT * FROM BILLING.ORDERS") == ["ORDERS"]


def test_cobol_compat_comma_separated_tables_are_all_returned():
    assert _names("SELECT * FROM ORDERS, CUSTOMERS") == ["ORDERS", "CUSTOMERS"]


def test_cobol_compat_string_literal_containing_clause_keyword_is_not_matched():
    assert _names("UPDATE T SET X = 'FROM U'") == ["T"]


def test_cobol_compat_line_and_block_comments_are_ignored():
    text = "-- FROM FAKE_IN_LINE_COMMENT\nSELECT * FROM ORDERS /* FROM FAKE_IN_BLOCK_COMMENT */"
    assert _names(text) == ["ORDERS"]


def test_cobol_compat_end_exec_inside_string_literal_does_not_confuse_table_extraction():
    assert _names("SELECT 'END-EXEC' AS X FROM ORDERS") == ["ORDERS"]


def test_cobol_compat_merge_into_yields_accesses():
    text = "MERGE INTO ORDERS USING SRC ON (ORDERS.ID = SRC.ID) WHEN MATCHED THEN UPDATE SET X = 1"
    assert _names(text) == ["ORDERS"]


def test_cobol_compat_quoted_identifier_is_preserved_case_sensitively():
    assert _names('SELECT * FROM "orders"') == ["orders"]


def test_cobol_compat_backtick_and_bracket_quoted_identifiers_are_preserved():
    assert _names("SELECT * FROM `Orders`") == ["Orders"]
    assert _names("SELECT * FROM [Customers]") == ["Customers"]


# --- table_refs: 引用識別子の内部を句キーワードと誤認しない・`""` エスケープ ---

def test_table_refs_does_not_match_clause_keyword_inside_quoted_identifier():
    """引用識別子の内部に偶然含まれる `FROM` を句キーワードと誤認しない——外側の本物の
    `FROM real` だけを拾う。"""
    assert _names('SELECT "x FROM fake" FROM real') == ["REAL"]


def test_ident_token_handles_double_quote_escape_inside_identifier():
    """`"a""b"` は1つの識別子トークン（`""` は `"` のエスケープ）——`a"b` になる。"""
    assert _names('FROM "a""b"') == ['a"b']


# --- table_refs: CTE 名（`WITH ... AS (`）は FROM/JOIN 候補から除外する ---

def test_table_refs_excludes_unqualified_cte_name_from_from_clause():
    assert _names("WITH x AS (SELECT * FROM a) SELECT * FROM x") == ["A"]


def test_table_refs_excludes_cte_name_with_column_list():
    assert _names("WITH x (c1, c2) AS (SELECT * FROM a) SELECT * FROM x") == ["A"]


def test_table_refs_multiple_ctes_only_real_tables_remain():
    text = "WITH x AS (SELECT * FROM a), y AS (SELECT * FROM b) SELECT * FROM x, y, real_table"
    assert _names(text) == ["A", "B", "REAL_TABLE"]


def test_table_refs_with_recursive_excludes_unqualified_cte_name():
    """`WITH` 直後の省略可能な `RECURSIVE` を読み飛ばしてから CTE 宣言を読む。"""
    assert _names("WITH RECURSIVE x AS (SELECT * FROM a) SELECT * FROM x") == ["A"]


def test_table_refs_with_recursive_multiple_ctes_with_column_list():
    text = ("WITH RECURSIVE x (c1, c2) AS (SELECT * FROM a), y AS (SELECT * FROM b) "
            "SELECT * FROM x, y")
    assert _names(text) == ["A", "B"]


def test_table_refs_qualified_reference_to_cte_name_is_not_excluded():
    """CTE 名との比較は未修飾の `FROM x`/`JOIN x` にだけ適用する——`schema.x` のように
    schema 修飾された参照は同名の別テーブルとみなし除外しない。"""
    assert _names("WITH x AS (SELECT * FROM a) SELECT * FROM schema.x") == ["A", "X"]


# --- table_refs: CTE 名のスコープは SQL 文単位（`;` 区切り）——別の文へは漏れない ---

def test_cte_name_scope_is_limited_to_its_own_statement_not_leaked_to_next_statement():
    """1文目で宣言した CTE 名は1文目の中でだけ除外に効く——2文目で同名が実テーブルとして
    使われていれば普通に拾う（複文タグ・複数 EXEC SQL 文の混在での誤除外を防ぐ）。"""
    text = "WITH recent AS (SELECT * FROM source) SELECT * FROM recent; SELECT * FROM recent;"
    assert _names(text) == ["SOURCE", "RECENT"]


def test_quoted_cte_name_is_excluded_using_same_normalization_as_unquote_or_norm_ident():
    """引用 CTE 名（`WITH "recent" AS (...)`）も `unquote_or_norm_ident()` と同じ規則
    （引用識別子は大文字化しない）で比較して除外できる。"""
    text = 'WITH "recent" AS (SELECT * FROM source) SELECT * FROM "recent"'
    assert _names(text) == ["SOURCE"]


# --- table_refs: 識別子直後の `@`（Oracle の DB link）は参照候補から除外する ---

def test_table_refs_excludes_oracle_db_link_target():
    assert _names("FROM schema.table@dblink") == []


def test_table_refs_db_link_does_not_block_subsequent_comma_separated_table():
    assert _names("FROM schema.table@dblink, real_table") == ["REAL_TABLE"]


# --- sanitize: MySQL の `#` 行コメントは既定で無効（DB2/COBOL は `#` が識別子文字） ---

def test_sanitize_hash_is_identifier_char_by_default_not_a_line_comment():
    """既定（`hash_line_comments=False`）では `#` は行コメントを開始しない——DB2/COBOL の
    `T#1` のような識別子を1トークンのまま読む（DDL/EXEC SQL の呼び出しはこの既定のまま）。"""
    assert _names("SELECT * FROM T#1") == ["T#1"]


def test_sanitize_hash_line_comment_is_blanked_when_enabled():
    """`hash_line_comments=True`（MyBatis/MySQL 方言）を明示したときだけ `#` 行コメントを
    空白化する（`#{` は MyBatis プレースホルダのため除外）。"""
    out = _sql_scan.sanitize("# FROM fake\nSELECT * FROM real", hash_line_comments=True)
    names = [name for name, _offset in _sql_scan.table_refs(out)]
    assert names == ["REAL"]


def test_sanitize_hash_brace_placeholder_is_not_treated_as_comment_even_when_enabled():
    """`#{...}`（MyBatis の動的プレースホルダ）は `hash_line_comments=True` でも行コメントとして
    扱わない——後続の `_STOP_LEAD_CHARS` がそのままテーブル名候補として読み始めないよう打ち切る。"""
    out = _sql_scan.sanitize("SELECT * FROM #{tbl}", hash_line_comments=True)
    names = [name for name, _offset in _sql_scan.table_refs(out)]
    assert names == []


# --- table_refs: `JOIN LATERAL (subquery)` の LATERAL を除外する ---

def test_table_refs_excludes_lateral_keyword_after_join():
    text = "SELECT * FROM ORDERS O JOIN LATERAL (SELECT 1) L ON TRUE"
    assert _names(text) == ["ORDERS"]
