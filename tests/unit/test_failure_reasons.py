"""取り込み失敗の閉じた理由語彙（`sherpa.ingest.failure_reasons`・ING-1）の単体テスト。

`classify()`/`describe()` が実際にコード側が生成する生 `reason` 文字列（`office_md.py`／
`ooxml_arm.py` の `document_ir_failed:<detail>`／`legacy_convert` の失敗系列）を漏れなく
`REASON_CATALOG` の既知コードへ分類できること、未知の reason は `other` へ fail-open で
落ちることを固定する。DB/ネットワーク不要。
"""
from __future__ import annotations

from sherpa.ingest import failure_reasons as fr


def test_catalog_entries_all_have_label_and_advice():
    """`REASON_CATALOG` の全コードが label/advice を持つ（UI がそのまま表示できる契約）。"""
    for code, info in fr.REASON_CATALOG.items():
        assert isinstance(info.get("label"), str) and info["label"], code
        assert isinstance(info.get("advice"), str) and info["advice"], code


def test_partial_extraction_advice_is_non_empty_string():
    """「抽出不完全の疑い」は失敗カタログとは別枠だが同じく平文の advice を持つ。"""
    assert isinstance(fr.PARTIAL_EXTRACTION_LABEL, str) and fr.PARTIAL_EXTRACTION_LABEL
    assert isinstance(fr.PARTIAL_EXTRACTION_ADVICE, str) and fr.PARTIAL_EXTRACTION_ADVICE


# コード側（office_md.py／ooxml_arm.py／legacy_convert.py）が実際に生成する生 reason 文字列と、
# 期待される分類先（閉じた語彙のコード）。ここに無い形の reason が新たに追加されたら、このテストが
# 検知できるよう都度この表を更新する（vocabulary の網羅性を機械的に保つ）。
_KNOWN_RAW_REASONS = {
    # office_md._build_derived_into_staging（legacy 変換・ING-1 タイムアウト分離）
    "legacy_conversion_timeout": "legacy_conversion_timeout",
    "legacy_conversion_failed": "legacy_conversion_failed",
    # office_md._build_derived_into_staging（入口サイズガード・MEM-1・プレフィックス無し）
    "size_exceeded": "size_exceeded",
    # office_md._build_derived_into_staging（xlsx セル数/非圧縮サイズガード・MEM-2・プレフィックス無し）
    "cell_count_exceeded": "cell_count_exceeded",
    "uncompressed_size_exceeded": "uncompressed_size_exceeded",
    # ooxml_arm.OoxmlArm.convert（document_ir_failed:<detail>）
    "document_ir_failed:malformed_structure": "malformed_structure",
    "document_ir_failed:password_protected": "password_protected",
    "document_ir_failed:size_exceeded": "size_exceeded",
    "document_ir_failed:ValueError": "other",          # 未知の例外クラス名はそのまま other へ
    "document_ir_failed:KeyError": "other",
    # 各段の書込失敗（write_failed 系の別名）
    "write_failed": "write_failed",
    "manifest_write_failed": "write_failed",
    "fallback_write_failed": "write_failed",
    # 想定外例外系（render/build/fallback/unhandled）
    "fallback_failed:TypeError": "other",
    "render_failed:RuntimeError": "other",
    "build_failed:OSError": "other",
    "unhandled_os_error:OSError": "other",
    "unhandled_exception:RuntimeError": "other",
}


def test_classify_covers_all_known_raw_reasons():
    for raw, expected in _KNOWN_RAW_REASONS.items():
        assert fr.classify(raw) == expected, raw


def test_classify_unknown_and_empty_reason_is_other():
    assert fr.classify("something_never_seen_before") == "other"
    assert fr.classify(None) == "other"
    assert fr.classify("") == "other"


def test_describe_keeps_original_detail_for_other():
    """`other` に落ちた reason は元の生文字列を `detail` に残す（UI の内訳表示・原因追跡用）。"""
    d = fr.describe("document_ir_failed:SomeWeirdError")
    assert d["code"] == "other"
    assert d["detail"] == "document_ir_failed:SomeWeirdError"
    assert d["label"] == fr.REASON_CATALOG["other"]["label"]
    assert d["advice"] == fr.REASON_CATALOG["other"]["advice"]


def test_describe_known_reason_matches_catalog():
    d = fr.describe("legacy_conversion_timeout")
    assert d["code"] == "legacy_conversion_timeout"
    assert d["label"] == fr.REASON_CATALOG["legacy_conversion_timeout"]["label"]
    assert d["advice"] == fr.REASON_CATALOG["legacy_conversion_timeout"]["advice"]
