"""`CobolAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S）。"""
from __future__ import annotations

import pathlib

from sherpa.ingest.analyzers.cobol import CobolAnalyzer

A = CobolAnalyzer()
ROOT = pathlib.Path(__file__).resolve().parents[3]


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


def test_extract_refs_ignores_copy_keyword_inside_string_literal():
    """`DISPLAY 'COPY FAKECPY'.` のように文字列リテラルの中に `COPY` という語があるだけでは、
    COPIES 参照として誤検知しない（`_CALL` 側の既存テスト`test_extract_refs_ignores_call_keyword_
    inside_string_literal` と対になる COPY 版・rv-s2-mention #5）。"""
    text = "       PROGRAM-ID. ORDER-MAIN.\n           DISPLAY 'COPY FAKECPY'.\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == [] and res.dropped == []


def test_extract_refs_ignores_copy_after_inline_comment_marker():
    """`*>` 以降はコメント——行全体が `*>` コメントの `*> COPY X`（`_is_comment` が拾う既存動作）に
    加え、`MOVE 1 TO Y. *> COPY FAKE` のように実コードへ続けて書かれた行末コメント中の COPY も
    拾わない（`_is_comment` は行**全体**がコメント行かどうかしか見ないため、行末コメントは
    従来対象外だった＝rv-s2-mention #5 で新規に塞いだ穴）。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "      *> COPY X\n"
        "           MOVE 1 TO Y. *> COPY FAKE\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == [] and res.dropped == []


def test_extract_refs_accepts_double_quoted_call_literal():
    """`CALL "REALPGM"`（二重引用符）も単一引用符と同様に INVOKES として受理する（rv-s2-mention #5）。"""
    text = "       PROGRAM-ID. ORDER-MAIN.\n           CALL \"REALPGM\".\n"
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert kinds == {("INVOKES", "Module", "REALPGM")}
    assert res.dropped == []


def test_source_format_directive_accepts_is_keyword():
    """`>>SOURCE FORMAT IS FREE`（`IS` 付き表記）も自由形式として扱う（S1・本番で `IS` 抜けにしか
    マッチしない既知の穴を埋める）。"""
    from sherpa.ingest.static_analysis import _is_free_format

    assert _is_free_format(">>SOURCE FORMAT IS FREE\n") is True
    assert _is_free_format(">>SOURCE FORMAT IS FIXED\n") is False


def test_extract_refs_handles_sequence_numbered_fixed_format_sample():
    """S1: 採番付き固定形式サンプル（fixtures/corpus/cobol-seq/SEQPGM.cbl）——
    1〜6桁連番・7桁 indicator のコメント／継続行・73桁以降のゴミを固定テスト化する。"""
    text = (ROOT / "fixtures" / "corpus" / "cobol-seq" / "SEQPGM.cbl").read_text(encoding="utf-8")

    d = A.collect_defs(text, "cobol-seq/SEQPGM.cbl")
    assert d.primary is not None
    assert d.primary.label == "Module" and d.primary.name == "SEQPGM"

    r = A.extract_refs(text, "cobol-seq/SEQPGM.cbl")
    refs_by_kind = {(x.edge_type, x.kind, x.name): x for x in r.refs}
    assert ("COPIES", "Copybook", "REAL-SEQCPY") in refs_by_kind
    assert refs_by_kind[("COPIES", "Copybook", "REAL-SEQCPY")].line == 5   # 継続なし＝原本行そのまま
    # 73桁以降（識別領域）のゴミ「CALLZZZ」は拾わない（同じ行に COPY と CALL らしき文字列が
    # 同居していても偽の INVOKES が増えない）。
    assert not any(k[0] == "INVOKES" and k[2] not in ("LONGCALLPART1PART2",) for k in refs_by_kind)
    # コメント行（7桁目 `*`）中の `COPY FAKE-SEQCPY` は拾わない。
    assert not any(name == "FAKE-SEQCPY" for _e, _k, name in refs_by_kind)

    call_ref = refs_by_kind[("INVOKES", "Module", "LONGCALLPART1PART2")]
    assert call_ref.line == 6                          # 継続行結合後も来歴 line は先頭の物理行
    assert call_ref.extra.get("via") == "call"          # RV1 是正: COBOL の CALL にも via=call

    assert len(r.dropped) == 1
    assert r.dropped[0].reason == "dynamic_call" and r.dropped[0].line == 8
    assert "WS-DYNAMIC-TARGET" in r.dropped[0].snippet


def test_continuation_non_literal_trims_padding_and_joins_directly():
    """非リテラル継続: 直前行の末尾空白（72桁までの空白埋め）と継続行の先頭空白を除去し、
    最後の非空白文字同士を直結する（`prev.rstrip() + cont.lstrip()`）。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       COPY VERY-LONG-COPY" + " " * 45 + "\n"
        "      -    BOOK.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert kinds == {("COPIES", "Copybook", "VERY-LONG-COPYBOOK")}


def test_continuation_literal_single_quote_preserves_column_position():
    """単一引用符のリテラル継続: 直前行で未閉鎖の `'` を検知し、継続行先頭の同種引用符を
    除去して連結する（`CALL 'LONG` / `-    'SUB'.` → `LONGSUB`）。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       CALL 'LONG\n"
        "      -    'SUB'.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {r.name for r in res.refs} == {"LONGSUB"}
    assert res.dropped == []


def test_continuation_literal_double_quote_preserves_column_position():
    """二重引用符のリテラル継続も単一引用符と同様に扱う。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       CALL \"LONG\n"
        "      -    \"SUB\".\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {r.name for r in res.refs} == {"LONGSUB"}
    assert res.dropped == []


def test_continuation_spans_three_or_more_physical_lines():
    """3行以上にまたがる継続でも正しく結合する（途中の継続行がまだ引用符を閉じない場合も
    未閉鎖状態を維持する）。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       CALL 'LO\n"
        "      -    NG\n"
        "      -    'SUB'.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {r.name for r in res.refs} == {"LONGSUB"}


def test_continuation_skips_blank_line_between_statement_and_continuation():
    """7〜72桁が空白だけの物理行を挟んでも継続結合され、来歴 line は開始行のまま。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       COPY PART\n"
        "\n"
        "      -    -B.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    refs_by_name = {r.name: r for r in res.refs}
    assert set(refs_by_name) == {"PART-B"}
    assert refs_by_name["PART-B"].line == 2


def test_continuation_skips_star_comment_line_between_statement_and_continuation():
    """コメント行（7桁目 `*`）を挟んでも継続結合される。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       COPY PART\n"
        "      * ignored comment\n"
        "      -    -B.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    refs_by_name = {r.name: r for r in res.refs}
    assert set(refs_by_name) == {"PART-B"}
    assert refs_by_name["PART-B"].line == 2


def test_continuation_skips_slash_comment_line_between_statement_and_continuation():
    """コメント行（7桁目 `/`）を挟んでも継続結合される。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "       COPY PART\n"
        "      /ignored\n"
        "      -    -B.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    refs_by_name = {r.name: r for r in res.refs}
    assert set(refs_by_name) == {"PART-B"}
    assert refs_by_name["PART-B"].line == 2


def test_source_format_directive_ignores_directive_inside_comment_line():
    """コメント行中の指示文は見ない——ファイル全体 regex 検索ではなく物理行を順に見て、
    `_is_comment` でない行に現れた最初の指示文だけを採用する。"""
    from sherpa.ingest.static_analysis import _is_free_format

    text = "      * >>SOURCE FORMAT FIXED\n>>SOURCE FORMAT FREE\n"
    assert _is_free_format(text) is True


def test_extract_refs_drops_debug_line_call_when_debugging_mode_not_declared():
    """7桁目 `D`（デバッグ行）は `WITH DEBUGGING MODE` の宣言が無ければ解析しない
    （黙って落とさない・CALL は INVOKES にならない）。`debug_line` の記録は `collect_defs`
    （Pass1）側だけの責務——`extract_refs`（Pass2）は同じ行を二重に `dropped` へ積まない。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "      D    CALL 'DEBUGSUB'.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert res.refs == []
    assert not any(d.reason == "debug_line" for d in res.dropped)


def test_extract_refs_processes_debug_line_call_when_debugging_mode_declared():
    """`WITH DEBUGGING MODE` の宣言があるファイルではデバッグ行を通常行として解析する。"""
    text = (
        "       SOURCE-COMPUTER. IBM WITH DEBUGGING MODE.\n"
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "      D    CALL 'DEBUGSUB'.\n"
    )
    res = A.extract_refs(text, "ORDER-MAIN.cbl")
    assert {(r.edge_type, r.name) for r in res.refs} == {("INVOKES", "DEBUGSUB")}
    assert not any(d.reason == "debug_line" for d in res.dropped)


def test_collect_defs_records_debug_line_as_dropped_without_debugging_mode_declared():
    """`collect_defs` 側でもデバッグ行は `DefResult.dropped` に記録される
    （PROGRAM-ID 自体には影響しない）。"""
    text = (
        "       PROGRAM-ID. ORDER-MAIN.\n"
        "      D    DISPLAY 'X'.\n"
    )
    res = A.collect_defs(text, "ORDER-MAIN.cbl")
    assert res.primary is not None and res.primary.name == "ORDER-MAIN"
    assert any(d.reason == "debug_line" for d in res.dropped)


def test_copy_continuation_across_intervening_debug_line_without_declaration():
    """`COPY PART` の直後に宣言なし D 行、さらにその次に継続行 (`-B.`) が続く場合、継続行は
    D 行を素通りして `COPY PART` へ結合される（D 行が誤って継続の起点/対象になり、`COPY PART`
    自体の結合先を奪って丸ごと消えてしまわない）。"""
    text = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. PARTJOIN.\n"
        "       PROCEDURE DIVISION.\n"
        "           COPY PART\n"
        "      D    DISPLAY 'X'.\n"
        "      -    -B.\n"
    )
    res = A.extract_refs(text, "PARTJOIN.cbl")
    assert {(r.edge_type, r.name) for r in res.refs} == {("COPIES", "PART-B")}
    assert not any(d.reason == "debug_line" for d in res.dropped)


def test_call_continuation_across_intervening_debug_line_without_declaration():
    """`CALL 'LONG` の直後に宣言なし D 行、さらにその次に継続行 (`'SUB'.`) が続く場合も同様に、
    継続行は D 行を素通りして `CALL 'LONG` の文字列リテラルへ結合される（INVOKES が消えない）。"""
    text = (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. CALLJOIN.\n"
        "       PROCEDURE DIVISION.\n"
        "           CALL 'LONG\n"
        "      D    DISPLAY 'X'.\n"
        "      -    'SUB'.\n"
    )
    res = A.extract_refs(text, "CALLJOIN.cbl")
    assert {(r.edge_type, r.name) for r in res.refs} == {("INVOKES", "LONGSUB")}
    assert not any(d.reason == "debug_line" for d in res.dropped)


def test_collect_and_extract_refs_handle_code_starting_at_column_one():
    """連番領域なし・`>>SOURCE FORMAT` 指示も無い固定形式ファイル（1桁目からコードが始まる
    ダンプ/抜粋等）でも、1〜6桁を連番領域として誤って切り落とさず PROGRAM-ID/COPY/CALL を
    拾う（S1 論理行正規化の後方是正・実コーパスにこの形は普通にある）。"""
    text = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. TAXCALC.\n"
        "PROCEDURE DIVISION.\n"
        "COPY REALCPY.\n"
        "CALL 'REALPGM'.\n"
    )
    d = A.collect_defs(text, "TAXCALC.cbl")
    assert d.primary is not None
    assert d.primary.label == "Module" and d.primary.name == "TAXCALC"

    r = A.extract_refs(text, "TAXCALC.cbl")
    kinds = {(x.edge_type, x.kind, x.name) for x in r.refs}
    assert kinds == {("COPIES", "Copybook", "REALCPY"), ("INVOKES", "Module", "REALPGM")}
    assert r.dropped == []


def test_column1_style_trigger_anywhere_in_file_applies_to_the_whole_file():
    """様式はファイル単位で1つに決める（行ごとの個別判定はしない）——ファイル中の1行にでも
    列1始まりの判定材料（見出し／レベル項目）があれば、他の行が採番付き固定形式のような
    見た目（1〜6桁が数字）でも、ファイル全体が列1始まりとして扱われる（列の切り落としも
    継続結合もせず、生の行のまま構文マッチする）。行3・4は DIVISION 見出し／PROGRAM-ID／
    レベル項目のいずれでもない文にする——それらは固定列の証拠として優先されるため、
    この判定材料自体を含めると意図した列1始まり判定を確認できない。"""
    text = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. MIXEDPGM.\n"
        "000300     DISPLAY 'NOTE'.\n"
        "000400     COPY SEQCPY.\n"
        "CALL 'NOSEQPGM'.\n"
    )
    d = A.collect_defs(text, "MIXEDPGM.cbl")
    assert d.primary is not None and d.primary.name == "MIXEDPGM"

    r = A.extract_refs(text, "MIXEDPGM.cbl")
    kinds = {(x.edge_type, x.kind, x.name) for x in r.refs}
    assert kinds == {("COPIES", "Copybook", "SEQCPY"), ("INVOKES", "Module", "NOSEQPGM")}


def test_column_one_comment_line_copy_is_not_matched():
    """列1始まりファイルでも、1桁目 `*` はコメント行として扱う（COPY を拾わない）——列1始まりでは
    7桁目は見ない規則の下でも、行頭 `*` によるコメント判定自体は変わらない。"""
    text = (
        "PROCEDURE DIVISION.\n"
        "PROGRAM-ID. GUARD2.\n"
        "* COPY SHOULD-NOT-APPEAR.\n"
        "COPY REAL-CPY.\n"
    )
    r = A.extract_refs(text, "GUARD2.cbl")
    names = {x.name for x in r.refs}
    assert names == {"REAL-CPY"}


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


def test_alphanumeric_sequence_area_is_treated_as_valid_and_continuation_still_joins():
    """固定形式の1〜6桁連番領域は任意文字を許す（IBM仕様どおり）——`A00010` 型の英数連番も
    連番として扱い、列位置どおりに8〜72桁を実コード領域として切り出す。連番が数字専用で
    なくても、7桁目 `-` の継続行結合は不変（`CALL 'LONGCALLPART1` / `-    'PART2'.` が
    1つの論理行 `LONGCALLPART1PART2` に結合される）。"""
    text = (
        "A00010 IDENTIFICATION DIVISION.\n"
        "A00020 PROGRAM-ID. ALNUMSEQ.\n"
        "A00030 PROCEDURE DIVISION.\n"
        "A00040     CALL 'LONGCALLPART1\n"
        "A00050-    'PART2'.\n"
    )
    d = A.collect_defs(text, "ALNUMSEQ.cbl")
    assert d.primary is not None and d.primary.name == "ALNUMSEQ"

    r = A.extract_refs(text, "ALNUMSEQ.cbl")
    assert {(x.edge_type, x.name) for x in r.refs} == {("INVOKES", "LONGCALLPART1PART2")}
    assert r.dropped == []


def test_column1_style_does_not_join_continuation_lines():
    """列1始まり（column-1 style）ファイルでは継続結合をしない——`COPY PART` の次行に
    `-B.` があっても、連番付き固定形式の継続行のようには結合されず、独立した2つの論理行の
    ままになる（様式の適用範囲＝列1始まりファイルには継続行という概念自体が無い）。"""
    from sherpa.ingest.static_analysis import _normalize_logical_lines

    text = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. X.\n"
        "PROCEDURE DIVISION.\n"
        "COPY PART\n"
        "-B.\n"
    )
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert debug_dropped == []
    logical_texts = [t for t, _ln, _segs in entries]
    assert logical_texts == [
        "IDENTIFICATION DIVISION.", "PROGRAM-ID. X.", "PROCEDURE DIVISION.",
        "COPY PART", "-B.",
    ]


def test_has_debugging_mode_ignores_occurrence_inside_comment_line():
    """コメント行に `WITH DEBUGGING MODE` という文言があるだけでは宣言と誤認しない
    （`_has_debugging_mode` は正規化済みの論理行を見るため、宣言の有無は D 行が
    `debug_line` として落ちるかどうかで確認する）。"""
    from sherpa.ingest.static_analysis import _normalize_logical_lines

    text = (
        "      * NOTE: WITH DEBUGGING MODE is just an example in this comment.\n"
        "      D    DISPLAY 'X'.\n"
    )
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert any(ln == 2 for ln, _snippet in debug_dropped)
    assert not any(ln == 2 for _t, ln, _segs in entries)


def test_has_debugging_mode_ignores_occurrence_inside_string_literal():
    """`DISPLAY 'WITH DEBUGGING MODE'.` のように文字列リテラルの中に文言があるだけでは
    宣言と誤認しない。"""
    from sherpa.ingest.static_analysis import _normalize_logical_lines

    text = (
        "       DISPLAY 'WITH DEBUGGING MODE'.\n"
        "      D    DISPLAY 'X'.\n"
    )
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert any(ln == 2 for ln, _snippet in debug_dropped)
    assert not any(ln == 2 for _t, ln, _segs in entries)


def test_fixed_columns_comment_uses_column7_regardless_of_alphanumeric_sequence_area():
    """固定列と確定したファイルでは、1〜6桁の連番領域が数字専用でなくても（英数字混在の
    連番でも）7桁目 `*` を無条件にコメントとして扱う（`_is_comment_fixed_columns`）。
    `_is_seq_area`（数字/空白限定）に依存した従来の `_is_comment` は英数字連番を連番領域
    として認めず、`A00030*    COPY FAKE.` のようなコメント行をコードとして解析し
    COPY FAKE を誤って拾っていた。"""
    text = (
        "A00010 IDENTIFICATION DIVISION.\n"
        "A00020 PROGRAM-ID. GUARD3.\n"
        "A00025 PROCEDURE DIVISION.\n"
        "A00030*    COPY FAKE.\n"
        "A00040     COPY REAL.\n"
    )
    r = A.extract_refs(text, "GUARD3.cbl")
    names = {x.name for x in r.refs}
    assert names == {"REAL"}


def test_detect_column1_style_prefers_fixed_evidence_over_column1_pattern_collision():
    """`01 ABC PROGRAM-ID. P.` のような行は `_LEVEL_ITEM_COLUMN1`（列1証拠）にも偶然
    一致しうるが、8桁目以降（`line[7:72]`）に `PROGRAM-ID` が始まる＝固定列の証拠を
    優先して見るため、真に固定列のファイルを誤って列1始まりと判定しない（列1始まりと
    誤判定すると、デバッグ行判定自体が行われず 7桁目 `D` の行がそのままコードになる）。"""
    text = (
        "01 ABC PROGRAM-ID. P.\n"
        "A00020 PROCEDURE DIVISION.\n"
        "A00030D    DISPLAY 'X'.\n"
    )
    d = A.collect_defs(text, "P.cbl")
    assert d.primary is not None
    assert d.primary.label == "Module" and d.primary.name == "P"
    assert any(dr.reason == "debug_line" for dr in d.dropped)


def test_detect_column1_style_recognizes_indented_division_header_as_fixed_evidence():
    """固定列の証拠判定は `line[7:72]` をそのまま `.match()` するのではなく `.lstrip()` して
    見る——DIVISION 見出しが8桁目ちょうどでなく、8〜11桁目のいずれかから始まる（Area A/B内で
    さらに字下げされている）場合でも固定列の証拠として認識する。認識できないと固定列と
    確定できず、デバッグ行（7桁目 `D`）の判定自体が行われない。"""
    text = (
        "01 ABC     IDENTIFICATION DIVISION.\n"
        "A00020 PROGRAM-ID. IDT1.\n"
        "A00030 PROCEDURE DIVISION.\n"
        "A00040D    DISPLAY 'X'.\n"
    )
    d = A.collect_defs(text, "IDT1.cbl")
    assert d.primary is not None
    assert d.primary.label == "Module" and d.primary.name == "IDT1"
    assert any(dr.reason == "debug_line" for dr in d.dropped)


def test_detect_column1_style_ignores_fixed_evidence_when_column7_is_not_valid_indicator():
    """列1始まりの文（`DISPLAY 01 UPON CONSOLE.`）は8桁目以降にレベル項目に似た断片
    （`01 UPON...`）を偶然含みうるが、7桁目（`Y`＝`DISPLAY` の末尾）が固定列の有効な
    indicator（空白・`D`/`d`・`-`・`*`・`/`）でなければ固定列の証拠として採用しない
    （RV 再現: 採用してしまうと列1始まりファイルが誤って固定列と判定され、PROGRAM-ID を
    含む全行の先頭7桁が連番領域として誤って切り落とされる）。"""
    text = (
        "IDENTIFICATION DIVISION.\n"
        "PROGRAM-ID. GUARD4.\n"
        "PROCEDURE DIVISION.\n"
        "DISPLAY 01 UPON CONSOLE.\n"
    )
    d = A.collect_defs(text, "GUARD4.cbl")
    assert d.primary is not None
    assert d.primary.label == "Module" and d.primary.name == "GUARD4"


def test_has_debugging_mode_ignores_identification_area_column_73_plus():
    """73桁以降（識別領域）だけに文言があっても宣言と誤認しない——固定列では各物理行の
    8〜72桁（`line[7:72]`＝コード領域）だけを対象にする。"""
    from sherpa.ingest.static_analysis import _normalize_logical_lines

    text = (
        " " * 72 + "WITH DEBUGGING MODE\n"
        "      D    DISPLAY 'X'.\n"
    )
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert any(ln == 2 for ln, _snippet in debug_dropped)
    assert not any(ln == 2 for _t, ln, _segs in entries)


def test_has_debugging_mode_ignores_occurrence_inside_multiline_literal():
    """文字列リテラルが継続行で閉じる場合でも、リテラルの中身は宣言と誤認しない——
    `_normalize_logical_lines` が継続行を1つの論理行へ結合済みのため、結合後の
    論理行に通常の `_strip_quoted`（単一行内で閉じるリテラルの除去）を掛けるだけで足りる
    （行をまたぐ引用符状態を別途持ち越す必要はない）。"""
    from sherpa.ingest.static_analysis import _normalize_logical_lines

    text = (
        "       DISPLAY 'OPEN LITERAL STARTS HERE\n"
        "      -    WITH DEBUGGING MODE'.\n"
        "      D    DISPLAY 'X'.\n"
    )
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert any(ln == 3 for ln, _snippet in debug_dropped)
    assert not any(ln == 3 for _t, ln, _segs in entries)


def test_has_debugging_mode_ignores_occurrence_inside_continuation_marker_quoted_literal():
    """継続行の先頭が「継続マーカーの引用符」で始まる場合（`CALL 'LONG` / `-    'SUB'.` と
    同じ継続規則）でも、結合後の論理行が正しく1つの閉じたリテラルになり、その中の文言を
    宣言と誤認しない（RV 再現: マーカー引用符の除去漏れがあると `WITH DEBUGGING MODE` が
    リテラルの外に取り残されて誤検知しうる）。"""
    from sherpa.ingest.static_analysis import _normalize_logical_lines

    text = (
        "       DISPLAY 'OPEN\n"
        "      -    'WITH DEBUGGING MODE'.\n"
        "      D    DISPLAY 'X'.\n"
    )
    entries, debug_dropped = _normalize_logical_lines(text, free_format=False)
    assert any(ln == 3 for ln, _snippet in debug_dropped)
    assert not any(ln == 3 for _t, ln, _segs in entries)


# --- EXEC SQL（アナライザ拡張 §4(d)）---

def _pgm(body: str) -> str:
    """`PROGRAM-ID` の直後に `body`（PROCEDURE DIVISION 相当）を続けた固定形式ソースを組み立てる。"""
    return (
        "       IDENTIFICATION DIVISION.\n"
        "       PROGRAM-ID. SQLDEMO.\n"
        "       PROCEDURE DIVISION.\n"
    ) + body


def test_exec_sql_select_into_host_var_from_table_yields_accesses_from_only():
    """`SELECT ... INTO :host-var FROM tbl` は `INTO` を対象にせず、`FROM` のテーブルだけを拾う
    （ホスト変数は参照候補にしない・§4(d)）。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT COL1, COL2\n"
        "                 INTO :WS-COL1, :WS-COL2\n"
        "                 FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs]
    assert kinds == [("ACCESSES", "Table", "ORDERS", {"via": "exec_sql"})]
    assert res.dropped == []


def test_exec_sql_insert_into_yields_accesses_ignoring_column_list():
    text = _pgm(
        "           EXEC SQL\n"
        "               INSERT INTO ORDER_LINES (ORDER_ID, QTY)\n"
        "               VALUES (:WS-ORDER-ID, :WS-QTY)\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs]
    assert kinds == [("ACCESSES", "Table", "ORDER_LINES", {"via": "exec_sql"})]


def test_exec_sql_update_lowercase_table_name_is_normalized_uppercase():
    text = _pgm(
        "           EXEC SQL\n"
        "               UPDATE customers\n"
        "               SET STATUS = 'X'\n"
        "               WHERE ID = :WS-ID\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs]
    assert kinds == [("ACCESSES", "Table", "CUSTOMERS", {"via": "exec_sql"})]


def test_exec_sql_delete_from_yields_accesses():
    text = _pgm(
        "           EXEC SQL\n"
        "               DELETE FROM ORDERS\n"
        "               WHERE ID = :WS-ID\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs]
    assert kinds == [("ACCESSES", "Table", "ORDERS", {"via": "exec_sql"})]


def test_exec_sql_join_yields_accesses_for_both_tables_discarding_aliases():
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT O.ID, C.NAME\n"
        "                 FROM ORDERS O\n"
        "                 JOIN CUSTOMERS C\n"
        "                   ON O.CUST_ID = C.ID\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    names = [r.name for r in res.refs]
    assert names == ["ORDERS", "CUSTOMERS"]
    assert all(r.extra == {"via": "exec_sql"} for r in res.refs)


def test_exec_sql_declare_cursor_is_dropped_as_dynamic_without_accesses():
    """`DECLARE ... CURSOR`（動的 SQL 扱い・§4(d)）はテーブル抽出をせず、ブロック全体を
    `Dropped("exec_sql_dynamic", ...)` として申告する。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               DECLARE CUR1 CURSOR FOR\n"
        "               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert res.refs == []
    assert [d.reason for d in res.dropped] == ["exec_sql_dynamic"]


def test_exec_sql_execute_immediate_is_dropped_as_dynamic():
    text = _pgm(
        "           EXEC SQL\n"
        "               EXECUTE IMMEDIATE :WS-DYNAMIC-SQL\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert res.refs == []
    assert [d.reason for d in res.dropped] == ["exec_sql_dynamic"]


def test_exec_sql_schema_qualified_table_name_drops_schema():
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT * FROM BILLING.ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]


def test_exec_sql_comma_separated_tables_in_from_are_all_returned():
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT * FROM ORDERS, CUSTOMERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS", "CUSTOMERS"]


def test_exec_sql_single_line_block_is_supported():
    text = _pgm(
        "           EXEC SQL SELECT * FROM ORDERS END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]


def test_exec_sql_hash_in_table_name_does_not_start_a_line_comment():
    """DB2 では `#` が識別子文字（`T#1`）——`EXEC SQL` は既定で `#` 行コメントを無効のまま
    サニタイズするため、同一物理行の `END-EXEC` を正しく終端として認識できる。"""
    text = _pgm(
        "           EXEC SQL SELECT * FROM T#1 END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["T#1"]
    assert res.dropped == []


def test_exec_sql_hash_in_host_variable_does_not_swallow_rest_of_line():
    """ホスト変数名に `#` を含む場合（`:WS#X`）も `#` を行コメント開始と誤認しない——
    以前の実装では `#` 以降が空白化され同じ物理行の `FROM ORDERS` まで消えていた。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT COL1\n"
        "                 INTO :WS#X\n"
        "                 FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs]
    assert kinds == [("ACCESSES", "Table", "ORDERS", {"via": "exec_sql"})]
    assert res.dropped == []


def test_exec_sql_unterminated_block_without_end_exec_is_dropped_as_dynamic():
    """`END-EXEC` の無いまま EOF に達したブロックは解釈せず記録するだけ（黙って消さない）。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT * FROM ORDERS\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert res.refs == []
    assert [d.reason for d in res.dropped] == ["exec_sql_dynamic"]


def test_call_and_copy_still_work_alongside_exec_sql_blocks():
    """EXEC SQL ブロックの追加が既存の COPY/CALL 抽出を壊さないことを確認する。"""
    text = _pgm(
        "           COPY SHARED-CPY.\n"
        "           EXEC SQL\n"
        "               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n"
        "           CALL 'ORDER-SUB'.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert ("COPIES", "Copybook", "SHARED-CPY") in kinds
    assert ("INVOKES", "Module", "ORDER-SUB") in kinds


# --- EXEC SQL 本文の文字列/コメント内キーワードは句として拾わない ---

def test_exec_sql_string_literal_containing_clause_keyword_is_not_matched():
    text = _pgm(
        "           EXEC SQL\n"
        "               UPDATE T SET X = 'FROM U'\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["T"]


def test_exec_sql_line_and_block_comments_are_ignored():
    text = _pgm(
        "           EXEC SQL\n"
        "               -- FROM FAKE_IN_LINE_COMMENT\n"
        "               SELECT * FROM ORDERS /* FROM FAKE_IN_BLOCK_COMMENT */\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]


# --- EXEC SQL 境界判定: 開始検索は COBOL 引用文字列を除外・終了は実際の END-EXEC だけ ---

def test_exec_sql_start_inside_display_string_literal_is_not_matched():
    """`DISPLAY 'EXEC SQL ... END-EXEC'` のような文字列リテラル中の疑似 EXEC SQL は
    ブロック開始と誤認しない（§4(d)）。"""
    text = _pgm(
        "           DISPLAY 'EXEC SQL FAKE END-EXEC'.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert res.refs == []
    assert res.dropped == []


def test_exec_sql_end_exec_inside_string_literal_does_not_terminate_the_block():
    """ブロック本文の SQL 文字列リテラル中に `END-EXEC` という文字列があっても、それで
    ブロックを終端しない——実際の `END-EXEC` まで読み進めて `FROM` のテーブルを拾う。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT 'END-EXEC' AS X FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]
    assert res.dropped == []


def test_exec_sql_end_exec_inside_line_comment_does_not_terminate_the_block():
    """`-- END-EXEC` のような行コメント中の `END-EXEC` もブロック終端とみなさない。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               -- END-EXEC\n"
        "               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]


def test_exec_sql_line_comment_does_not_swallow_continuation_joined_physical_line():
    """`-- END-EXEC` の行が COBOL の継続行マーカー（7桁目 `-`）で次の物理行
    （`SELECT * FROM ORDERS`）と1つの論理行へ結合される場合でも、`--` 行コメントは
    連結前の物理行境界で止まり、後続の物理行の内容までコメント化して参照を消さない
    （RV 再現: 継続結合された論理行を1つの断片として扱うと `--` が論理行末まで伸びて
    `ORDERS` が消えていた）。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               -- END-EXEC\n"
        "      -           SELECT * FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]
    assert res.dropped == []


def test_exec_sql_end_exec_split_across_continuation_is_still_one_token():
    """`END-` と継続行で分割された `EXEC` は継続結合で1つの文字列になるため、`--` の物理行
    境界と無関係に連続したトークン（`END-EXEC`）のまま認識してブロックを終端する。"""
    text = _pgm(
        "           EXEC SQL SELECT * FROM ORDERS END\n"
        "      -    -EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]
    assert res.dropped == []


# --- 動的 SQL 判定（DECLARE CURSOR/EXECUTE IMMEDIATE）はサニタイズ後に行う ---

def test_exec_sql_declare_cursor_keyword_inside_comment_does_not_trigger_dynamic_drop():
    """`-- DECLARE C CURSOR` のようなコメント中の語で動的 SQL と誤判定しない
    （判定はコメント・文字列を除去したサニタイズ済み本文に対して行う）。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               -- DECLARE C CURSOR\n"
        "               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]
    assert res.dropped == []


# --- EXEC SQL ... END-EXEC の前後（同一物理行）は通常の COPY/CALL/次の EXEC SQL 処理へ回す ---

def test_call_after_end_exec_on_the_same_physical_line_is_still_processed():
    text = _pgm(
        "           EXEC SQL SELECT * FROM T END-EXEC. CALL 'Q'.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert ("ACCESSES", "Table", "T") in kinds
    assert ("INVOKES", "Module", "Q") in kinds


def test_call_before_exec_sql_on_the_same_physical_line_is_still_processed():
    text = _pgm(
        "           CALL 'Q'. EXEC SQL SELECT * FROM T END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    kinds = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert ("INVOKES", "Module", "Q") in kinds
    assert ("ACCESSES", "Table", "T") in kinds


def test_two_exec_sql_blocks_on_the_same_physical_line_are_both_processed():
    """固定形式の実コード領域（8〜72桁）に収まる形で、同一物理行に2つの `EXEC SQL ... END-EXEC`
    ブロックが並ぶ場合でも両方処理する。"""
    text = _pgm(
        "       EXEC SQL SELECT*FROM T1 END-EXEC.EXEC SQL SELECT*FROM T2 END-EXEC\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["T1", "T2"]


# --- EXEC SQL 側の引用識別子（DDL 側＝analyzers/sql.py と同じ規則）---

def test_exec_sql_quoted_identifier_is_preserved_case_sensitively():
    text = _pgm(
        '           EXEC SQL\n'
        '               SELECT * FROM "orders"\n'
        '           END-EXEC.\n'
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["orders"]


def test_exec_sql_backtick_and_bracket_quoted_identifiers_are_preserved():
    text = _pgm(
        "           EXEC SQL\n"
        "               SELECT * FROM `Orders`\n"
        "           END-EXEC.\n"
        "           EXEC SQL\n"
        "               SELECT * FROM [Customers]\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["Orders", "Customers"]


# --- MERGE INTO も ACCESSES(via=exec_sql) の対象にする ---

def test_exec_sql_merge_into_yields_accesses():
    text = _pgm(
        "           EXEC SQL\n"
        "               MERGE INTO ORDERS USING SRC ON (ORDERS.ID = SRC.ID)\n"
        "               WHEN MATCHED THEN UPDATE SET X = 1\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]


# --- EXEC SQL は共通 SQL スキャナ（_sql_scan）の CTE/`#` 行コメント除外も継承する ---

def test_exec_sql_with_clause_cte_name_is_excluded_from_accesses():
    """`WITH cte AS (...)` で宣言された CTE 名は物理テーブルではないため `ACCESSES` にしない
    （DDL 側／MyBatis 側と共通の `_sql_scan.table_refs` の規則を EXEC SQL も継承する）。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               WITH RECENT AS (SELECT * FROM ORDERS)\n"
        "               SELECT * FROM RECENT\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["ORDERS"]


def test_exec_sql_hash_is_not_treated_as_a_line_comment():
    """DB2/COBOL では `#` が識別子文字（`T#1` 等）のため、`_sql_scan` の `#` 行コメント規則は
    EXEC SQL 本文では既定で無効のまま——MySQL 方言のような行コメントとしては読まない
    （MyBatis（MySQL 方言）だけが `hash_line_comments=True` で有効にする）。"""
    text = _pgm(
        "           EXEC SQL\n"
        "               # FROM FAKE_NOT_A_COMMENT_HERE\n"
        "               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "sqldemo.cbl")
    assert [r.name for r in res.refs] == ["FAKE_NOT_A_COMMENT_HERE", "ORDERS"]


# --- EXEC CICS XCTL/LINK（アナライザ拡張 §4(d)・S5b）---

def test_exec_cics_xctl_literal_program_yields_invokes_via_cics_xctl():
    text = _pgm(
        "           EXEC CICS\n"
        "               XCTL PROGRAM('MENU01')\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    kinds = [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs]
    assert kinds == [("INVOKES", "Module", "MENU01", {"via": "cics_xctl"})]
    assert res.dropped == []


def test_exec_cics_link_literal_program_yields_invokes_via_cics_link():
    """`COMMAREA(...)` 等の付随引数があっても `PROGRAM(...)` の引数だけを読む。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               LINK PROGRAM('SUBR01') COMMAREA(WS-AREA)\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    kinds = [(r.edge_type, r.kind, r.name, r.extra) for r in res.refs]
    assert kinds == [("INVOKES", "Module", "SUBR01", {"via": "cics_link"})]
    assert res.dropped == []


def test_exec_cics_double_quoted_program_literal_is_accepted():
    text = _pgm(
        '           EXEC CICS\n'
        '               XCTL PROGRAM("MENU01")\n'
        '           END-EXEC.\n'
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert [(r.name, r.extra) for r in res.refs] == [("MENU01", {"via": "cics_xctl"})]


def test_exec_cics_dynamic_identifier_program_is_dropped_without_invokes():
    """`PROGRAM(WS-NEXT)`（識別子＝動的）は静的に解決できないため `Dropped("cics_dynamic", ...)`
    として申告し、参照候補は作らない。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               XCTL PROGRAM(WS-NEXT)\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert res.refs == []
    assert [(d.reason, d.line) for d in res.dropped] == [("cics_dynamic", 5)]


def test_exec_cics_non_xctl_link_command_is_dropped_as_cics_other():
    """`XCTL`/`LINK` 以外の CICS コマンド（`SEND` 等）は参照を作らず、ブロック単位で
    `Dropped("cics_other", line, <コマンド名>)` を1件だけ申告する。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               SEND MAP('M1')\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert res.refs == []
    assert [(d.reason, d.line, d.snippet) for d in res.dropped] == [("cics_other", 4, "SEND")]


def test_exec_cics_other_command_drops_only_one_entry_per_block():
    """`RECEIVE`/`WRITE` 等の複数語が同一ブロックに含まれても、ブロック単位で1件だけ申告する
    （大量申告を避ける）。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               RECEIVE INTO(WS-AREA) LENGTH(WS-LEN)\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert len(res.dropped) == 1
    assert res.dropped[0].reason == "cics_other"


def test_exec_cics_literal_split_across_continuation_line_is_assembled():
    """継続行（7桁目 `-`）でリテラルが分割されても（COBOL の標準継続規則）、結合後の1つの
    リテラルとして読む。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               LINK PROGRAM('SUB\n"
        "      -    'R02')\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert [(r.name, r.extra) for r in res.refs] == [("SUBR02", {"via": "cics_link"})]


def test_exec_cics_and_exec_sql_blocks_coexist_in_the_same_file():
    """EXEC SQL/EXEC CICS の両方が同じファイルに現れても、それぞれ正しい種別で抽出される
    （同じ位置カーソル走査・同じ境界スキャナを種別で分岐して使う）。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               XCTL PROGRAM('MENU01')\n"
        "           END-EXEC.\n"
        "           EXEC SQL\n"
        "               SELECT * FROM ORDERS\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    kinds = {(r.edge_type, r.kind, r.name, r.extra.get("via")) for r in res.refs}
    assert kinds == {
        ("INVOKES", "Module", "MENU01", "cics_xctl"),
        ("ACCESSES", "Table", "ORDERS", "exec_sql"),
    }


def test_exec_cics_start_inside_display_string_literal_is_not_matched():
    """`DISPLAY 'EXEC CICS FAKE END-EXEC'` のような文字列リテラル中の偽陽性語を拾わない
    （`EXEC SQL` と同じ `_blank_cobol_strings` 前処理）。"""
    text = _pgm(
        "           DISPLAY 'EXEC CICS FAKE END-EXEC'.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert res.refs == []
    assert res.dropped == []


def test_exec_cics_unterminated_block_without_end_exec_is_dropped_as_cics_dynamic():
    """`END-EXEC` の無いまま EOF に達した未終端 `EXEC CICS` ブロックは
    `Dropped("cics_dynamic", ...)` として申告する（`EXEC SQL` 側の `exec_sql_dynamic` と対の挙動）。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               XCTL PROGRAM('MENU01')\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert res.refs == []
    assert [d.reason for d in res.dropped] == ["cics_dynamic"]


def test_exec_cics_program_keyword_inside_quoted_arg_value_is_not_matched():
    """他の引数の引用文字列値の中に `PROGRAM(...)` という文字列が偶然含まれても、引用符の外に
    ある本物の `PROGRAM(...)` だけを引数として読む（`_blank_cobol_strings` 前処理・§4(d) RV是正）。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               XCTL CHANNEL(\"PROGRAM('FAKE')\") PROGRAM('REAL')\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert [(r.name, r.extra) for r in res.refs] == [("REAL", {"via": "cics_xctl"})]
    assert res.dropped == []


def test_exec_cics_end_exec_inside_double_quoted_string_is_not_terminator():
    """`CHANNEL("END-EXEC")` のような二重引用符文字列中の `END-EXEC` でブロックを終端しない
    （CICS は単一・二重引用符の両方（エスケープ含む）を保護する境界スキャナ・§4(d) RV是正・
    SQL 側の既存挙動＝単一引用符のみ保護は不変）。"""
    text = _pgm(
        "           EXEC CICS\n"
        "               XCTL CHANNEL(\"END-EXEC\") PROGRAM(\"TARGET\")\n"
        "           END-EXEC.\n"
        "           GOBACK.\n"
    )
    res = A.extract_refs(text, "online1.cbl")
    assert [(r.name, r.extra) for r in res.refs] == [("TARGET", {"via": "cics_xctl"})]
    assert res.dropped == []
