"""`CobolAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S）。"""
from __future__ import annotations

from sherpa.ingest.analyzers.cobol import CobolAnalyzer

A = CobolAnalyzer()


def test_extensions_match_static_analysis_cobol_ext():
    from sherpa.ingest.static_analysis import COBOL_EXT
    assert A.extensions == frozenset(COBOL_EXT)
    assert A.name == "cobol"


def test_collect_defs_extracts_program_id_as_module():
    text = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       PROCEDURE DIVISION.\n"
    )
    res = A.collect_defs(text, "案件A/ORDER-MAIN.cbl")
    assert res.primary is not None
    assert res.primary.label == "Module" and res.primary.name == "ORDER-MAIN"
    assert res.children == []


def test_collect_defs_ignores_program_id_in_comment_line():
    text = "      * PROGRAM-ID. FAKE.\n       PROCEDURE DIVISION.\n"
    res = A.collect_defs(text, "x.cbl")
    assert res.primary is None                    # コメント行の PROGRAM-ID は拾わない


def test_collect_defs_returns_no_primary_without_program_id():
    text = "       PROCEDURE DIVISION.\n           DISPLAY 'HELLO'.\n"
    res = A.collect_defs(text, "x.cbl")
    assert res.primary is None


def test_extract_refs_finds_copy_and_call_after_program_id():
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       PROCEDURE DIVISION.\n"
        "           COPY SHARED-CPY.\n"
        "           CALL 'ORDER-SUB'.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert kinds == {("COPIES", "Copybook", "SHARED-CPY"), ("INVOKES", "Module", "ORDER-SUB")}
    assert all(r.line >= 1 for r in res.refs)
    assert res.dropped == []


def test_extract_refs_ignores_copy_call_before_program_id():
    """PROGRAM-ID 行より前の COPY/CALL は拾わない（after_id ゲート）。"""
    text = (
        "           COPY BEFORE-ID.\n"
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "           COPY SHARED-CPY.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    names = {r.name for r in res.refs}
    assert names == {"SHARED-CPY"}


def test_extract_refs_ignores_comment_lines():
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "      * COPY SHOULD-NOT-APPEAR.\n"
        "           COPY REAL-CPY.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    names = {r.name for r in res.refs}
    assert names == {"REAL-CPY"}


def test_extract_refs_flags_dynamic_call_as_dropped_not_resolved():
    """`CALL` の対象がリテラルでない（識別子＝動的呼び出し）場合は解決せず `dropped` に記録する。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "           CALL WS-PROGRAM-NAME.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == []                               # 動的呼び出し先はノード/エッジを作らない
    assert len(res.dropped) == 1
    d = res.dropped[0]
    assert d.reason == "dynamic_call" and d.line == 2 and "WS-PROGRAM-NAME" in d.snippet


def test_extract_refs_does_not_double_count_literal_call_as_dynamic():
    """リテラル CALL は動的呼び出しとして二重に dropped へ入らない。"""
    text = "       PROGRAM-ID. ORDER-MAIN.\n           CALL 'ORDER-SUB'.\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {r.name for r in res.refs} == {"ORDER-SUB"}
    assert res.dropped == []


def test_extract_refs_finds_dynamic_call_even_when_literal_call_shares_the_line():
    """同一行に literal CALL と動的 CALL が並んでいても、両方を文単位で検出する
    （`CALL 'STATIC'. CALL WS-TARGET.` → STATIC 参照＋dynamic_call 1件）。"""
    text = "       PROGRAM-ID. ORDER-MAIN.\n           CALL 'STATIC'. CALL WS-TARGET.\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {r.name for r in res.refs} == {"STATIC"}
    assert len(res.dropped) == 1 and res.dropped[0].reason == "dynamic_call"
    assert "WS-TARGET" in res.dropped[0].snippet


def test_extract_refs_ignores_call_keyword_inside_string_literal():
    """`DISPLAY 'CALL X'.` のように文字列リテラルの中に `CALL` という語があるだけでは、
    動的呼び出しとして誤検知しない。"""
    text = "       PROGRAM-ID. ORDER-MAIN.\n           DISPLAY 'CALL X'.\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == [] and res.dropped == []


def test_extract_refs_finds_dynamic_call_after_literal_call_end_call_without_period():
    """ピリオドを挟まず `END-CALL` で区切られた literal CALL と動的 CALL が連続していても、
    両方を検出する（`CALL 'STATIC' END-CALL CALL WS-TARGET END-CALL.` → STATIC 参照＋
    dynamic_call 1件）。"""
    text = ("       PROGRAM-ID. ORDER-MAIN.\n"
           "           CALL 'STATIC' END-CALL CALL WS-TARGET END-CALL.\n")
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {r.name for r in res.refs} == {"STATIC"}
    assert len(res.dropped) == 1 and res.dropped[0].reason == "dynamic_call"
    assert "WS-TARGET" in res.dropped[0].snippet


def test_extract_refs_ignores_dynamic_call_beyond_column_72():
    """動的 CALL の検知は固定形式 COBOL の採番/識別領域（73桁以降）を見ない
    （論理行の正規化そのものは CODE-2「静的解析の深化」の対象・ここは誤検知防止の最小対応）。"""
    beyond = " " * 72 + "CALL COLUMN73PLUS."          # "CALL ..." は73桁目（index 72）から始まる
    text = "       PROGRAM-ID. ORDER-MAIN.\n" + beyond + "\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == [] and res.dropped == []


def test_extract_refs_finds_dynamic_call_within_column_72():
    """72桁目までに収まる動的 CALL は従来どおり検出する（73桁以降を無視する対応が実コードの
    検知範囲まで狭めていないことの確認）。"""
    within = (" " * 60 + "CALL X.").ljust(70)          # "CALL X" は72桁目より前で終わる
    assert len(within) <= 72
    text = "       PROGRAM-ID. ORDER-MAIN.\n" + within + "\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == []
    assert len(res.dropped) == 1 and res.dropped[0].reason == "dynamic_call"


def test_extract_refs_detects_dynamic_call_beyond_column_72_when_source_format_free():
    """`>>SOURCE FORMAT FREE` 指示文があるファイルは自由形式＝73桁以降も切り詰めない
    （固定／自由形式は入力から判定する・docs/proposals/2026-08-29-コード解析層のコンポーネント化.md §2.2）。"""
    beyond = " " * 72 + "CALL COLUMN73PLUS."
    text = ">>SOURCE FORMAT FREE\n       PROGRAM-ID. ORDER-MAIN.\n" + beyond + "\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == []
    assert len(res.dropped) == 1 and res.dropped[0].reason == "dynamic_call"
    assert "COLUMN73PLUS" in res.dropped[0].snippet


def test_extract_refs_recognizes_source_free_short_directive_variant():
    """`>>SOURCE FREE`（`FORMAT` を省略した短縮形）も自由形式として扱う。"""
    beyond = " " * 72 + "CALL COLUMN73PLUS."
    text = ">>SOURCE FREE\n       PROGRAM-ID. ORDER-MAIN.\n" + beyond + "\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert len(res.dropped) == 1 and "COLUMN73PLUS" in res.dropped[0].snippet


def test_extract_refs_finds_nested_dynamic_call_inside_literal_call_exception_clause():
    """literal CALL の `ON EXCEPTION` 節に動的 CALL がネストしても取りこぼさない
    （`CALL 'STATIC' ON EXCEPTION CALL WS-RECOVERY END-CALL END-CALL.` → STATIC 参照＋
    dynamic_call 1件・断片内の CALL 出現ごとに個別判定する）。"""
    text = ("       PROGRAM-ID. ORDER-MAIN.\n"
           "           CALL 'STATIC' ON EXCEPTION CALL WS-RECOVERY END-CALL END-CALL.\n")
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {r.name for r in res.refs} == {"STATIC"}
    assert len(res.dropped) == 1 and res.dropped[0].reason == "dynamic_call"
    assert "WS-RECOVERY" in res.dropped[0].snippet


def test_extract_refs_end_call_boundary_uses_cobol_identifier_charset():
    """`END-CALL` の語境界判定は COBOL 識別子文字（`A-Z0-9#@$-`）に合わせる——
    `END-CALL$TARGET` のような識別子中の部分文字列を誤って区切らない。"""
    text = ("       PROGRAM-ID. ORDER-MAIN.\n"
           "           CALL END-CALL$TARGET.\n")
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert len(res.dropped) == 1 and res.dropped[0].reason == "dynamic_call"
    assert "END-CALL$TARGET" in res.dropped[0].snippet


def test_extract_refs_does_not_misdetect_call_inside_identifier_as_dynamic_call():
    """`CALL` の前方境界は COBOL 識別子文字に合わせる——`MOVE WS-CALL TO RESULT.` のように
    識別子（`WS-CALL`）の一部としての "CALL" を独立した語と誤認識し、続く `TO` を動的呼び出し先
    と誤検知しない。"""
    text = ("       PROGRAM-ID. ORDER-MAIN.\n"
           "           MOVE WS-CALL TO RESULT.\n")
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == [] and res.dropped == []


def test_extract_refs_still_detects_dynamic_call_when_preceded_by_boundary_char():
    """識別子境界の是正後も、真の動的 CALL（直前が空白等の非識別子文字）は従来どおり検出する。"""
    text = "       PROGRAM-ID. ORDER-MAIN.\n           CALL WS-PROGRAM-NAME.\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == []
    assert len(res.dropped) == 1 and res.dropped[0].reason == "dynamic_call"
    assert "WS-PROGRAM-NAME" in res.dropped[0].snippet


def test_source_format_directive_uses_first_occurrence_not_any_occurrence():
    """自由形式判定はファイル単位の近似——ファイル中で**最初に現れる**指示文（FREE/FIXED）で
    決める。途中で切り替わっても最初の指示のまま（対応は CODE-2 の対象・正典 §5）。"""
    from sherpa.ingest.static_analysis import _is_free_format

    assert _is_free_format(">>SOURCE FORMAT FIXED\n>>SOURCE FORMAT FREE\n") is False
    assert _is_free_format(">>SOURCE FORMAT FREE\n>>SOURCE FORMAT FIXED\n") is True
    assert _is_free_format("no directive here") is False


def test_call_copy_do_not_match_inside_cobol_identifiers():
    """語境界是正（2026-09-05）: `WS-CALL 'X'`／`WS-COPY ITEM` のような COBOL 識別子の末尾に
    偶然 COPY/CALL が現れる行を偽参照として拾わない（`_DYNAMIC_CALL` と同じ前方境界規則）。
    正当な文（行頭・空白後）は従来どおり拾う。"""
    from sherpa.ingest.analyzers.cobol import CobolAnalyzer
    a = CobolAnalyzer()
    text = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. GUARD1.\n"
        "       PROCEDURE DIVISION.\n"
        "           MOVE 'A' TO WS-CALL 'FAKE1'.\n"
        "           MOVE WS-COPY FAKE2 TO X.\n"
        "           CALL 'REAL1'.\n"
        "           COPY REALCPY.\n"
    )
    refs = a.extract_refs(text, "GUARD1.cbl").refs
    names = {(r.edge_type, r.name) for r in refs}
    assert ("INVOKES", "REAL1") in names
    assert ("COPIES", "REALCPY") in names
    assert not any(n in ("FAKE1", "FAKE2") for _e, n in names)
