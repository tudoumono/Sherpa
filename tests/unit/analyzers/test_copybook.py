"""`CopybookAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S）。"""
from __future__ import annotations

import pathlib

from sherpa.ingest.analyzers.copybook import CopybookAnalyzer

A = CopybookAnalyzer()
ROOT = pathlib.Path(__file__).resolve().parents[3]


def test_extensions_match_static_analysis_copybook_ext():
    from sherpa.ingest.static_analysis import COPYBOOK_EXT
    assert A.extensions == frozenset(COPYBOOK_EXT)
    assert A.name == "copybook"


def test_collect_defs_primary_is_copybook_named_by_file_stem():
    text = "       01 SHARED-CPY.\n           05 SHARED-AMT   PIC 9(5) VALUE 100.\n"
    res = A.collect_defs(text, "案件A/00_共通/SHARED-CPY.cpy")
    assert res.primary is not None
    assert res.primary.label == "Copybook" and res.primary.name == "SHARED-CPY"


def test_collect_defs_builds_qualified_names_via_level_stack():
    """レベルスタックで修飾名（GROUP.ITEM）を作る。深い階層でも正しく戻る（pop）。

    `_VALUE` は数値リテラルのみ拾う（`static_analysis._VALUE` の契約）ので、値の検証は数値の
    VALUE 句で行う（英数字リテラルは対象外＝そのまま移植した既存挙動）。
    """
    text = (
        "       01 GROUP-A.\n"
        "           05 SUB-A.\n"
        "               10 ITEM-A       PIC 9       VALUE 5.\n"
        "           05 SUB-B            PIC X(2).\n"
    )
    res = A.collect_defs(text, "G.cpy")
    by_name = {c.name: c for c in res.children}
    assert by_name["ITEM-A"].cid_key == "GROUP-A.SUB-A.ITEM-A"
    assert by_name["ITEM-A"].value == "5"
    assert by_name["SUB-B"].cid_key == "GROUP-A.SUB-B"          # 深い階層から正しく戻って修飾
    assert by_name["SUB-B"].value is None


def test_collect_defs_skips_filler_and_level_66_88():
    """FILLER/66/88 は対象外。最上位の 01 レベル自体は他の項目と同様に子として登録される
    （レベル行かどうかしか見ない既存の正規表現契約＝そのまま移植した挙動）。"""
    text = (
        "       01 REC.\n"
        "           05 FILLER            PIC X(3).\n"
        "           05 REAL-ITEM         PIC 9(2) VALUE 10.\n"
        "           66 RENAME-ITEM       RENAMES REAL-ITEM.\n"
        "           88 REAL-FLAG         VALUE 'Y'.\n"
    )
    res = A.collect_defs(text, "R.cpy")
    names = {c.name for c in res.children}
    assert names == {"REC", "REAL-ITEM"}


def test_collect_defs_ignores_comment_lines():
    text = (
        "       01 REC.\n"
        "      * 05 COMMENTED-OUT PIC X.\n"
        "           05 REAL-ITEM  PIC X.\n"
    )
    res = A.collect_defs(text, "R.cpy")
    names = {c.name for c in res.children}
    assert names == {"REC", "REAL-ITEM"}


def test_collect_defs_handles_sequence_numbered_items():
    """S1: 採番付き固定形式サンプル（fixtures/corpus/cobol-seq/SEQCPY.cpy）——採番のある行でも
    現行 `_ITEM`（行頭アンカー）が誤って連番を「レベル番号」と読まないよう、正規化で 1〜6桁の
    連番領域を落としてから項目定義を拾う。FILLER/66/88 の除外・レベルスタック修飾・コメント行
    （7桁目 `*`）除外は採番ありでも不変。"""
    text = (ROOT / "fixtures" / "corpus" / "cobol-seq" / "SEQCPY.cpy").read_text(encoding="utf-8")
    res = A.collect_defs(text, "cobol-seq/SEQCPY.cpy")
    by_name = {c.name: c for c in res.children}
    assert set(by_name) == {"SEQ-REC", "SEQ-AMT", "SEQ-FILLER-CHK", "SEQ-SUB-ITEM"}
    assert by_name["SEQ-AMT"].cid_key == "SEQ-REC.SEQ-AMT"
    assert by_name["SEQ-AMT"].value == "100"
    assert by_name["SEQ-SUB-ITEM"].cid_key == "SEQ-REC.SEQ-FILLER-CHK.SEQ-SUB-ITEM"
    assert by_name["SEQ-REC"].line == 2                # 採番除去後も来歴 line は原本の物理行


def test_collect_defs_drops_debug_line_item_without_debugging_mode_declared():
    """7桁目 `D`（デバッグ行）は `WITH DEBUGGING MODE` の宣言が無ければ解析せず、
    `Dropped("debug_line", ...)` として記録する（黙って落とさない・項目は DataItem にならない）。"""
    text = (
        "       01 REC.\n"
        "      D    05 DEBUG-ITEM PIC X.\n"
    )
    res = A.collect_defs(text, "R.cpy")
    assert {c.name for c in res.children} == {"REC"}
    debug_dropped = [d for d in res.dropped if d.reason == "debug_line"]
    assert len(debug_dropped) == 1
    assert debug_dropped[0].line == 2 and "DEBUG-ITEM" in debug_dropped[0].snippet


def test_collect_defs_processes_debug_line_item_when_debugging_mode_declared():
    """`WITH DEBUGGING MODE` の宣言があるファイルではデバッグ行を通常行として解析する。"""
    text = (
        "       SOURCE-COMPUTER. IBM WITH DEBUGGING MODE.\n"
        "       01 REC.\n"
        "      D    05 DEBUG-ITEM PIC X.\n"
    )
    res = A.collect_defs(text, "R.cpy")
    assert {c.name for c in res.children} == {"REC", "DEBUG-ITEM"}
    assert not any(d.reason == "debug_line" for d in res.dropped)


def test_collect_defs_handles_items_starting_at_column_one():
    """連番領域なし・1桁目から項目定義が始まる固定形式ファイル（採番/字下げ無しのダンプ・抜粋等）
    でも、1〜6桁を連番領域として誤って切り落とさずレベル項目を拾う（S1 後方是正）。"""
    text = "01 REC.\n05 REAL-ITEM PIC X.\n"
    res = A.collect_defs(text, "R.cpy")
    names = {c.name for c in res.children}
    assert names == {"REC", "REAL-ITEM"}
    assert {c.name: c.cid_key for c in res.children}["REAL-ITEM"] == "REC.REAL-ITEM"


def test_extract_refs_is_always_empty():
    """コピーブック自身は他ファイルを参照しない（COPY される側）。"""
    text = "       01 SHARED-CPY.\n           05 SHARED-AMT PIC 9(5) VALUE 100.\n"
    res = A.extract_refs(text, "SHARED-CPY.cpy")
    assert res.refs == [] and res.dropped == []
