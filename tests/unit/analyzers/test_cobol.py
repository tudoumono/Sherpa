"""`CobolAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S）。

入力ソース片 → (参照, Dropped の理由) の表で確かめる。
"""
from __future__ import annotations

import pathlib

import pytest

from sherpa.ingest.analyzers.cobol import CobolAnalyzer
from sherpa.ingest.static_analysis import _is_free_format, _normalize_logical_lines

A = CobolAnalyzer()
ROOT = pathlib.Path(__file__).resolve().parents[3]

P = "       PROGRAM-ID. ORDER-MAIN.\n"
BEYOND = " " * 72 + "CALL COLUMN73PLUS."          # "CALL ..." は73桁目から始まる


def CP(name):
    return ("COPIES", "Copybook", name, None)


def CL(name):
    return ("INVOKES", "Module", name, "call")


def TB(name):
    return ("ACCESSES", "Table", name, "exec_sql")


def XC(name, via):
    return ("INVOKES", "Module", name, via)


def _pgm(body: str) -> str:
    return (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. SQLDEMO.\n"
        "       PROCEDURE DIVISION.\n"
    ) + body


def _run(text):
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    return [(r.edge_type, r.kind, r.name, r.extra.get("via")) for r in res.refs], res.dropped


def test_extensions_match_static_analysis_cobol_ext():
    from sherpa.ingest.static_analysis import COBOL_EXT
    assert A.extensions == frozenset(COBOL_EXT)
    assert A.name == "cobol"


# ---- collect_defs ----

def test_collect_defs_extracts_program_id_as_module():
    res = A.collect_defs(
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. ORDER-MAIN.\n       PROCEDURE DIVISION.\n",
        "案件A/ORDER-MAIN.cbl")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "ORDER-MAIN"
    assert res.children == []


@pytest.mark.parametrize("text", [
    "      * PROGRAM-ID. FAKE.\n       PROCEDURE DIVISION.\n",     # コメント行の PROGRAM-ID は拾わない
    "       PROCEDURE DIVISION.\n           DISPLAY 'HELLO'.\n",
])
def test_collect_defs_no_primary(text):
    assert A.collect_defs(text, "x.cbl").primary is None


# ---- COPY/CALL（固定形式・継続・コメント・語境界・デバッグ行・列1始まり）----
# (入力, 参照, Dropped[(reason, snippet に含まれる語)])
COPY_CALL_CASES = {
    "copy_call_after_program_id": (
        P + "       PROCEDURE DIVISION.\n           COPY SHARED-CPY.\n           CALL 'ORDER-SUB'.\n",
        [CP("SHARED-CPY"), CL("ORDER-SUB")], []),
    "before_program_id_ignored": (
        "           COPY BEFORE-ID.\n" + P + "           COPY SHARED-CPY.\n", [CP("SHARED-CPY")], []),
    "comment_line_ignored": (
        P + "      * COPY SHOULD-NOT-APPEAR.\n           COPY REAL-CPY.\n", [CP("REAL-CPY")], []),
    "dynamic_call_dropped": (
        P + "           CALL WS-PROGRAM-NAME.\n", [], [("dynamic_call", "CALL WS-PROGRAM-NAME")]),
    "literal_call_not_dynamic": (P + "           CALL 'ORDER-SUB'.\n", [CL("ORDER-SUB")], []),
    "literal_and_dynamic_same_line": (
        P + "           CALL 'STATIC'. CALL WS-TARGET.\n", [CL("STATIC")], [("dynamic_call", "CALL WS-TARGET")]),
    "call_in_string_literal": (P + "           DISPLAY 'CALL X'.\n", [], []),
    "end_call_without_period": (
        P + "           CALL 'STATIC' END-CALL CALL WS-TARGET END-CALL.\n",
        [CL("STATIC")], [("dynamic_call", "CALL WS-TARGET")]),
    "nested_dynamic_in_exception_clause": (
        P + "           CALL 'STATIC' ON EXCEPTION CALL WS-RECOVERY END-CALL END-CALL.\n",
        [CL("STATIC")], [("dynamic_call", "CALL 'STATIC' ON EXCEPTION CALL WS-RECOVERY")]),
    "end_call_boundary_is_cobol_identifier": (
        P + "           CALL END-CALL$TARGET.\n", [], [("dynamic_call", "CALL END-CALL$TARGET")]),
    "call_inside_identifier_not_dynamic": (P + "           MOVE WS-CALL TO RESULT.\n", [], []),
    "dynamic_beyond_col72_ignored": (P + BEYOND + "\n", [], []),
    "dynamic_within_col72": (P + (" " * 60 + "CALL X.").ljust(70) + "\n", [], [("dynamic_call", "CALL X")]),
    "free_format_directive_no_truncation": (
        ">>SOURCE FORMAT FREE\n" + P + BEYOND + "\n", [], [("dynamic_call", "CALL COLUMN73PLUS")]),
    "free_format_short_directive": (
        ">>SOURCE FREE\n" + P + BEYOND + "\n", [], [("dynamic_call", "CALL COLUMN73PLUS")]),
    "copy_in_string_literal": (P + "           DISPLAY 'COPY FAKECPY'.\n", [], []),
    "copy_after_inline_comment": (
        P + "      *> COPY X\n           MOVE 1 TO Y. *> COPY FAKE\n", [], []),
    "double_quoted_call": (P + '           CALL "REALPGM".\n', [CL("REALPGM")], []),
    # 継続行
    "continuation_non_literal": (
        P + "       COPY VERY-LONG-COPY" + " " * 45 + "\n      -    BOOK.\n", [CP("VERY-LONG-COPYBOOK")], []),
    "continuation_single_quote_literal": (
        P + "       CALL 'LONG\n      -    'SUB'.\n", [CL("LONGSUB")], []),
    "continuation_double_quote_literal": (
        P + '       CALL "LONG\n      -    "SUB".\n', [CL("LONGSUB")], []),
    "continuation_three_lines": (
        P + "       CALL 'LO\n      -    NG\n      -    'SUB'.\n", [CL("LONGSUB")], []),
    "continuation_over_blank_line": (P + "       COPY PART\n\n      -    -B.\n", [CP("PART-B")], []),
    "continuation_over_star_comment": (
        P + "       COPY PART\n      * ignored comment\n      -    -B.\n", [CP("PART-B")], []),
    "continuation_over_slash_comment": (
        P + "       COPY PART\n      /ignored\n      -    -B.\n", [CP("PART-B")], []),
    # デバッグ行（7桁目 D）
    "debug_line_without_declaration_dropped": (
        P + "      D    CALL 'DEBUGSUB'.\n", [], []),
    "debug_line_with_declaration_processed": (
        "       SOURCE-COMPUTER. IBM WITH DEBUGGING MODE.\n" + P + "      D    CALL 'DEBUGSUB'.\n",
        [CL("DEBUGSUB")], []),
    "copy_continuation_across_debug_line": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. PARTJOIN.\n       PROCEDURE DIVISION.\n"
        "           COPY PART\n      D    DISPLAY 'X'.\n      -    -B.\n", [CP("PART-B")], []),
    "call_continuation_across_debug_line": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. CALLJOIN.\n       PROCEDURE DIVISION.\n"
        "           CALL 'LONG\n      D    DISPLAY 'X'.\n      -    'SUB'.\n", [CL("LONGSUB")], []),
    # 列1始まり／英数連番
    "column1_file": (
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. TAXCALC.\nPROCEDURE DIVISION.\n"
        "COPY REALCPY.\nCALL 'REALPGM'.\n", [CP("REALCPY"), CL("REALPGM")], []),
    "column1_trigger_anywhere_applies_to_whole_file": (
        "IDENTIFICATION DIVISION.\nPROGRAM-ID. MIXEDPGM.\n"
        "000300     DISPLAY 'NOTE'.\n000400     COPY SEQCPY.\nCALL 'NOSEQPGM'.\n",
        [CP("SEQCPY"), CL("NOSEQPGM")], None),
    "column1_comment_star": (
        "PROCEDURE DIVISION.\nPROGRAM-ID. GUARD2.\n* COPY SHOULD-NOT-APPEAR.\nCOPY REAL-CPY.\n",
        [CP("REAL-CPY")], None),
    "identifier_suffix_call_copy_not_matched": (
        "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. GUARD1.\n       PROCEDURE DIVISION.\n"
        "           MOVE 'A' TO WS-CALL 'FAKE1'.\n           MOVE WS-COPY FAKE2 TO X.\n"
        "           CALL 'REAL1'.\n           COPY REALCPY.\n", [CL("REAL1"), CP("REALCPY")], None),
    "alnum_sequence_area_with_continuation": (
        "A00010 IDENTIFICATION DIVISION.\nA00020 PROGRAM-ID. ALNUMSEQ.\nA00030 PROCEDURE DIVISION.\n"
        "A00040     CALL 'LONGCALLPART1\nA00050-    'PART2'.\n", [CL("LONGCALLPART1PART2")], []),
    "alnum_sequence_area_column7_comment": (
        "A00010 IDENTIFICATION DIVISION.\nA00020 PROGRAM-ID. GUARD3.\nA00025 PROCEDURE DIVISION.\n"
        "A00030*    COPY FAKE.\nA00040     COPY REAL.\n", [CP("REAL")], None),
}


@pytest.mark.parametrize("text,refs,dropped", COPY_CALL_CASES.values(), ids=COPY_CALL_CASES)
def test_extract_refs_copy_call(text, refs, dropped):
    got_refs, got_dropped = _run(text)
    assert got_refs == refs
    if dropped is not None:
        assert len(got_dropped) == len(dropped)
        for d, (reason, needle) in zip(got_dropped, dropped):
            assert d.reason == reason and needle == d.snippet


def test_dynamic_call_dropped_records_line():
    d = _run(P + "           CALL WS-PROGRAM-NAME.\n")[1][0]
    assert d.reason == "dynamic_call" and d.line == 2


@pytest.mark.parametrize("noise", ["\n", "      * ignored comment\n", "      /ignored\n"])
def test_continuation_across_noise_keeps_start_line(noise):
    res = A.extract_refs(P + "       COPY PART\n" + noise + "      -    -B.\n", "ORDER-MAIN.cbl")
    assert [(r.name, r.line) for r in res.refs] == [("PART-B", 2)]


def test_collect_defs_records_debug_line_as_dropped():
    """デバッグ行の `debug_line` は collect_defs（Pass1）側だけが記録し、extract_refs は二重に積まない。"""
    res = A.collect_defs(P + "      D    DISPLAY 'X'.\n", "ORDER-MAIN.cbl")
    assert res.primary is not None and res.primary.name == "ORDER-MAIN"
    assert any(d.reason == "debug_line" for d in res.dropped)
    assert not any(d.reason == "debug_line" for d in A.extract_refs(P + "      D    DISPLAY 'X'.\n", "x.cbl").dropped)


# ---- 固定／自由形式の判定・論理行の正規化 ----

@pytest.mark.parametrize("text,free", [
    (">>SOURCE FORMAT FIXED\n>>SOURCE FORMAT FREE\n", False),    # 最初の指示文が勝つ
    (">>SOURCE FORMAT FREE\n>>SOURCE FORMAT FIXED\n", True),
    ("no directive here", False),
    (">>SOURCE FORMAT IS FREE\n", True),
    (">>SOURCE FORMAT IS FIXED\n", False),
    ("      * >>SOURCE FORMAT FIXED\n>>SOURCE FORMAT FREE\n", True),   # コメント行中の指示文は見ない
])
def test_is_free_format(text, free):
    assert _is_free_format(text) is free


def test_extract_refs_handles_sequence_numbered_fixed_format_sample():
    """採番付き固定形式サンプル: 7桁 indicator のコメント／継続行・73桁以降のゴミ。"""
    text = (ROOT / "fixtures" / "corpus" / "cobol-seq" / "SEQPGM.cbl").read_text(encoding="utf-8")

    d = A.collect_defs(text, "cobol-seq/SEQPGM.cbl")
    assert d.primary is not None
    assert d.primary.label == "Module" and d.primary.name == "SEQPGM"

    r = A.extract_refs(text, "cobol-seq/SEQPGM.cbl")
    refs_by_kind = {(x.edge_type, x.kind, x.name): x for x in r.refs}
    assert refs_by_kind[("COPIES", "Copybook", "REAL-SEQCPY")].line == 5
    assert not any(k[0] == "INVOKES" and k[2] not in ("LONGCALLPART1PART2",) for k in refs_by_kind)
    assert not any(name == "FAKE-SEQCPY" for _e, _k, name in refs_by_kind)

    call_ref = refs_by_kind[("INVOKES", "Module", "LONGCALLPART1PART2")]
    assert call_ref.line == 6                          # 継続結合後も来歴 line は先頭の物理行
    assert call_ref.extra.get("via") == "call"

    assert len(r.dropped) == 1
    assert r.dropped[0].reason == "dynamic_call" and r.dropped[0].line == 8
    assert "WS-DYNAMIC-TARGET" in r.dropped[0].snippet


def test_column1_style_does_not_join_continuation_lines():
    text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. X.\nPROCEDURE DIVISION.\nCOPY PART\n-B.\n"
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert debug_dropped == []
    assert [t for t, _ln, _segs in entries] == [
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. X.", "PROCEDURE DIVISION.", "COPY PART", "-B."]


@pytest.mark.parametrize("text,debug_line", [
    ("      * NOTE: WITH DEBUGGING MODE is just an example in this comment.\n      D    DISPLAY 'X'.\n", 2),
    ("       DISPLAY 'WITH DEBUGGING MODE'.\n      D    DISPLAY 'X'.\n", 2),
    (" " * 72 + "WITH DEBUGGING MODE\n      D    DISPLAY 'X'.\n", 2),                      # 73桁以降
    ("       DISPLAY 'OPEN LITERAL STARTS HERE\n      -    WITH DEBUGGING MODE'.\n      D    DISPLAY 'X'.\n", 3),
    ("       DISPLAY 'OPEN\n      -    'WITH DEBUGGING MODE'.\n      D    DISPLAY 'X'.\n", 3),
], ids=["comment", "string_literal", "column73", "multiline_literal", "continuation_marker_literal"])
def test_debugging_mode_declaration_not_faked_by_comment_or_literal(text, debug_line):
    """コメント・文字列リテラル・識別領域中の `WITH DEBUGGING MODE` は宣言ではない（D 行は落ちる）。"""
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert any(ln == debug_line for ln, _snippet in debug_dropped)
    assert not any(ln == debug_line for _t, ln, _segs in entries)


@pytest.mark.parametrize("text,name", [
    ("01 ABC PROGRAM-ID. P.\nA00020 PROCEDURE DIVISION.\nA00030D    DISPLAY 'X'.\n", "P"),
    ("01 ABC     IDENTIFICATION DIVISION.\nA00020 PROGRAM-ID. IDT1.\nA00030 PROCEDURE DIVISION.\n"
     "A00040D    DISPLAY 'X'.\n", "IDT1"),
], ids=["fixed_evidence_beats_column1_pattern", "indented_division_header_is_fixed_evidence"])
def test_detect_column1_style_prefers_fixed_evidence(text, name):
    d = A.collect_defs(text, f"{name}.cbl")
    assert d.primary is not None and d.primary.label == "Module" and d.primary.name == name
    assert any(dr.reason == "debug_line" for dr in d.dropped)


def test_detect_column1_style_ignores_fixed_evidence_when_column7_is_not_valid_indicator():
    """列1始まりの `DISPLAY 01 UPON CONSOLE.` を固定列と誤判定すると PROGRAM-ID が先頭7桁ごと切り落とされる。"""
    text = "IDENTIFICATION DIVISION.\nPROGRAM-ID. GUARD4.\nPROCEDURE DIVISION.\nDISPLAY 01 UPON CONSOLE.\n"
    d = A.collect_defs(text, "GUARD4.cbl")
    assert d.primary is not None and d.primary.label == "Module" and d.primary.name == "GUARD4"


def test_collect_defs_column1_program_id():
    d = A.collect_defs("IDENTIFICATION DIVISION.\nPROGRAM-ID. TAXCALC.\nPROCEDURE DIVISION.\n", "TAXCALC.cbl")
    assert d.primary is not None and d.primary.label == "Module" and d.primary.name == "TAXCALC"
    d = A.collect_defs("A00010 IDENTIFICATION DIVISION.\nA00020 PROGRAM-ID. ALNUMSEQ.\n", "ALNUMSEQ.cbl")
    assert d.primary is not None and d.primary.name == "ALNUMSEQ"


# ---- EXEC SQL / EXEC CICS ----
# (本文, 参照（出現順）, Dropped の reason 列)
SQL_CASES = {
    "select_into_host_var_from_table": (
        "           EXEC SQL\n               SELECT COL1, COL2\n                 INTO :WS-COL1, :WS-COL2\n"
        "                 FROM ORDERS\n           END-EXEC.\n           GOBACK.\n", [TB("ORDERS")], []),
    "insert_into_ignores_column_list": (
        "           EXEC SQL\n               INSERT INTO ORDER_LINES (ORDER_ID, QTY)\n"
        "               VALUES (:WS-ORDER-ID, :WS-QTY)\n           END-EXEC.\n", [TB("ORDER_LINES")], []),
    "update_lowercase_normalized_upper": (
        "           EXEC SQL\n               UPDATE customers\n               SET STATUS = 'X'\n"
        "               WHERE ID = :WS-ID\n           END-EXEC.\n", [TB("CUSTOMERS")], []),
    "delete_from": (
        "           EXEC SQL\n               DELETE FROM ORDERS\n               WHERE ID = :WS-ID\n"
        "           END-EXEC.\n", [TB("ORDERS")], []),
    "join_both_tables_aliases_discarded": (
        "           EXEC SQL\n               SELECT O.ID, C.NAME\n                 FROM ORDERS O\n"
        "                 JOIN CUSTOMERS C\n                   ON O.CUST_ID = C.ID\n           END-EXEC.\n",
        [TB("ORDERS"), TB("CUSTOMERS")], []),
    "declare_cursor_dropped_dynamic": (
        "           EXEC SQL\n               DECLARE CUR1 CURSOR FOR\n               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n", [], ["exec_sql_dynamic"]),
    "execute_immediate_dropped_dynamic": (
        "           EXEC SQL\n               EXECUTE IMMEDIATE :WS-DYNAMIC-SQL\n           END-EXEC.\n",
        [], ["exec_sql_dynamic"]),
    "schema_qualified_drops_schema": (
        "           EXEC SQL\n               SELECT * FROM BILLING.ORDERS\n           END-EXEC.\n", [TB("ORDERS")], None),
    "comma_separated_from": (
        "           EXEC SQL\n               SELECT * FROM ORDERS, CUSTOMERS\n           END-EXEC.\n",
        [TB("ORDERS"), TB("CUSTOMERS")], None),
    "single_line_block": ("           EXEC SQL SELECT * FROM ORDERS END-EXEC.\n", [TB("ORDERS")], None),
    "hash_in_table_name_not_comment": ("           EXEC SQL SELECT * FROM T#1 END-EXEC.\n", [TB("T#1")], []),
    "hash_in_host_variable_not_comment": (
        "           EXEC SQL\n               SELECT COL1\n                 INTO :WS#X\n"
        "                 FROM ORDERS\n           END-EXEC.\n", [TB("ORDERS")], []),
    "unterminated_block_dropped": (
        "           EXEC SQL\n               SELECT * FROM ORDERS\n", [], ["exec_sql_dynamic"]),
    "string_literal_with_clause_keyword": (
        "           EXEC SQL\n               UPDATE T SET X = 'FROM U'\n           END-EXEC.\n", [TB("T")], None),
    "line_and_block_comments_ignored": (
        "           EXEC SQL\n               -- FROM FAKE_IN_LINE_COMMENT\n"
        "               SELECT * FROM ORDERS /* FROM FAKE_IN_BLOCK_COMMENT */\n           END-EXEC.\n",
        [TB("ORDERS")], None),
    "start_inside_display_literal_ignored": ("           DISPLAY 'EXEC SQL FAKE END-EXEC'.\n", [], []),
    "end_exec_in_sql_string_not_terminator": (
        "           EXEC SQL\n               SELECT 'END-EXEC' AS X FROM ORDERS\n           END-EXEC.\n",
        [TB("ORDERS")], []),
    "end_exec_in_line_comment_not_terminator": (
        "           EXEC SQL\n               -- END-EXEC\n               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n", [TB("ORDERS")], None),
    "line_comment_does_not_swallow_continuation_joined_line": (
        "           EXEC SQL\n               -- END-EXEC\n      -           SELECT * FROM ORDERS\n"
        "           END-EXEC.\n", [TB("ORDERS")], []),
    "end_exec_split_across_continuation": (
        "           EXEC SQL SELECT * FROM ORDERS END\n      -    -EXEC.\n", [TB("ORDERS")], []),
    "declare_cursor_in_comment_not_dynamic": (
        "           EXEC SQL\n               -- DECLARE C CURSOR\n               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n", [TB("ORDERS")], []),
    "call_after_end_exec_same_line": (
        "           EXEC SQL SELECT * FROM T END-EXEC. CALL 'Q'.\n", [TB("T"), CL("Q")], None),
    "call_before_exec_sql_same_line": (
        "           CALL 'Q'. EXEC SQL SELECT * FROM T END-EXEC.\n", [CL("Q"), TB("T")], None),
    "two_blocks_same_line": (
        "       EXEC SQL SELECT*FROM T1 END-EXEC.EXEC SQL SELECT*FROM T2 END-EXEC\n",
        [TB("T1"), TB("T2")], None),
    "double_quoted_identifier_case_preserved": (
        '           EXEC SQL\n               SELECT * FROM "orders"\n           END-EXEC.\n', [TB("orders")], None),
    "backtick_and_bracket_identifiers": (
        "           EXEC SQL\n               SELECT * FROM `Orders`\n           END-EXEC.\n"
        "           EXEC SQL\n               SELECT * FROM [Customers]\n           END-EXEC.\n",
        [TB("Orders"), TB("Customers")], None),
    "merge_into": (
        "           EXEC SQL\n               MERGE INTO ORDERS USING SRC ON (ORDERS.ID = SRC.ID)\n"
        "               WHEN MATCHED THEN UPDATE SET X = 1\n           END-EXEC.\n", [TB("ORDERS")], None),
    "cte_name_excluded": (
        "           EXEC SQL\n               WITH RECENT AS (SELECT * FROM ORDERS)\n               SELECT * FROM RECENT\n"
        "           END-EXEC.\n", [TB("ORDERS")], None),
    "hash_line_comment_not_applied": (
        "           EXEC SQL\n               # FROM FAKE_NOT_A_COMMENT_HERE\n               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n", [TB("FAKE_NOT_A_COMMENT_HERE"), TB("ORDERS")], None),
    "copy_call_alongside_exec_sql": (
        "           COPY SHARED-CPY.\n           EXEC SQL\n               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n           CALL 'ORDER-SUB'.\n",
        [CP("SHARED-CPY"), TB("ORDERS"), CL("ORDER-SUB")], None),
    # EXEC CICS
    "cics_xctl_literal": (
        "           EXEC CICS\n               XCTL PROGRAM('MENU01')\n           END-EXEC.\n",
        [XC("MENU01", "cics_xctl")], []),
    "cics_link_ignores_commarea": (
        "           EXEC CICS\n               LINK PROGRAM('SUBR01') COMMAREA(WS-AREA)\n           END-EXEC.\n",
        [XC("SUBR01", "cics_link")], []),
    "cics_double_quoted_literal": (
        '           EXEC CICS\n               XCTL PROGRAM("MENU01")\n           END-EXEC.\n',
        [XC("MENU01", "cics_xctl")], None),
    "cics_dynamic_program_dropped": (
        "           EXEC CICS\n               XCTL PROGRAM(WS-NEXT)\n           END-EXEC.\n", [], ["cics_dynamic"]),
    "cics_other_command_one_drop_per_block": (
        "           EXEC CICS\n               RECEIVE INTO(WS-AREA) LENGTH(WS-LEN)\n           END-EXEC.\n",
        [], ["cics_other"]),
    "cics_literal_split_across_continuation": (
        "           EXEC CICS\n               LINK PROGRAM('SUB\n      -    'R02')\n           END-EXEC.\n",
        [XC("SUBR02", "cics_link")], None),
    "cics_and_sql_coexist": (
        "           EXEC CICS\n               XCTL PROGRAM('MENU01')\n           END-EXEC.\n"
        "           EXEC SQL\n               SELECT * FROM ORDERS\n           END-EXEC.\n",
        [XC("MENU01", "cics_xctl"), TB("ORDERS")], None),
    "cics_start_inside_display_literal_ignored": ("           DISPLAY 'EXEC CICS FAKE END-EXEC'.\n", [], []),
    "cics_unterminated_dropped": (
        "           EXEC CICS\n               XCTL PROGRAM('MENU01')\n", [], ["cics_dynamic"]),
    "cics_program_keyword_inside_quoted_arg_ignored": (
        "           EXEC CICS\n               XCTL CHANNEL(\"PROGRAM('FAKE')\") PROGRAM('REAL')\n           END-EXEC.\n",
        [XC("REAL", "cics_xctl")], []),
    "cics_end_exec_inside_double_quoted_string_not_terminator": (
        "           EXEC CICS\n               XCTL CHANNEL(\"END-EXEC\") PROGRAM(\"TARGET\")\n           END-EXEC.\n",
        [XC("TARGET", "cics_xctl")], []),
}


@pytest.mark.parametrize("body,refs,dropped", SQL_CASES.values(), ids=SQL_CASES)
def test_extract_refs_exec_blocks(body, refs, dropped):
    res = A.extract_refs(_pgm(body), "sqldemo.cbl")
    assert [(r.edge_type, r.kind, r.name, r.extra.get("via")) for r in res.refs] == refs
    if dropped is not None:
        assert [d.reason for d in res.dropped] == dropped


def test_exec_cics_dropped_lines_and_snippets():
    res = A.extract_refs(_pgm(
        "           EXEC CICS\n               XCTL PROGRAM(WS-NEXT)\n           END-EXEC.\n"), "online1.cbl")
    assert [(d.reason, d.line) for d in res.dropped] == [("cics_dynamic", 5)]
    res = A.extract_refs(_pgm(
        "           EXEC CICS\n               SEND MAP('M1')\n           END-EXEC.\n"), "online1.cbl")
    assert [(d.reason, d.line, d.snippet) for d in res.dropped] == [("cics_other", 4, "SEND")]
