"""`CopybookAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S）。"""
from __future__ import annotations

from sherpa.ingest.analyzers.copybook import CopybookAnalyzer

A = CopybookAnalyzer()


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


def test_extract_refs_is_always_empty():
    """コピーブック自身は他ファイルを参照しない（COPY される側）。"""
    text = "       01 SHARED-CPY.\n           05 SHARED-AMT PIC 9(5) VALUE 100.\n"
    res = A.extract_refs(text, "SHARED-CPY.cpy")
    assert res.refs == [] and res.dropped == []
