"""PDF の Evidence IR（位置付き文字・表の格子）の実害の再現テスト。

Word/Excel 由来の PDF で位置付き文字が取れず表が作られなかった・クリップ経路や細い塗り矩形の罫線を
表の線と誤る・縦結合が分からない、の 3 点を実物（または最小の手書き PDF）で確かめる。
設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import sys
from pathlib import Path

from sherpa.ingest import evidence_ir, evidence_spike

_ROOT = Path(__file__).resolve().parents[2]
_EXPORT = _ROOT / "fixtures" / "pdf_export"
_OJA_PDF_EASY = _ROOT / "fixtures" / "eval" / "office_ja" / "inputs" / "OJA-PDF-EASY.pdf"


def _pdf_with(path: Path, content: str) -> Path:
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=300, height=300)
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): writer._add_object(font)})})
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as f:
        writer.write(f)
    return path


def _text(x: float, y: float, value: str) -> str:
    return f"BT /F1 10 Tf {x} {y} Td ({value}) Tj ET\n"


def _elements(path: Path, element_type: str):
    ir = evidence_spike.extract(path)
    assert evidence_ir.validation_errors(ir) == []
    return [element for element in ir.elements if element.type == element_type]


def test_word_export_pdf_keeps_positioned_text_per_cell_position():
    """Word 由来 PDF でも位置付き文字が取れる（ページ全体の 1 塊に縮退しない）。"""
    groups = {e.value: e for e in _elements(_EXPORT / "OJA-DOCX-EASY.pdf", "positioned_text")}
    assert {"表示名", "ACCOUNT_NO", "口座番号"} <= set(groups)
    assert groups["ACCOUNT_NO"].locator.bbox[2] < groups["口座番号"].locator.bbox[0]
    assert abs(groups["ACCOUNT_NO"].locator.bbox[1] - groups["口座番号"].locator.bbox[1]) < 3
    assert groups["表示名"].locator.bbox[1] > groups["口座番号"].locator.bbox[1]
    assert all(e.extension["position_confidence"] == 1.0 for e in groups.values())


def test_vertically_merged_cell_gets_row_span_and_no_fake_columns():
    """縦結合の「区分」列は row_span=2 の 1 セルになり、他のセルは span 1・列数は 4。"""
    path = _EXPORT / "PDFX-MERGED.pdf"
    tables = _elements(path, "table")
    assert len(tables) == 1
    assert len(tables[0].extension["column_boundaries"]) == 5
    cells = {e.value: e for e in _elements(path, "cell")}
    for label in ("基本情報", "明細"):
        assert cells[label].locator.extension["row_span"] == 2
        assert cells[label].locator.extension["column_span"] == 1
    assert cells["ORDER_NO"].locator.extension["row_span"] == 1
    assert cells["受注番号"].locator.extension["column_span"] == 1
    assert cells["基本情報"].locator.bbox[1] < cells["ORDER_NO"].locator.bbox[1]


def test_clip_only_rectangle_is_not_a_table(tmp_path):
    """`re W n`（クリップ）だけの矩形は罫線ではない。"""
    path = _pdf_with(tmp_path / "clip.pdf", "50 100 200 100 re W n\n" + _text(60, 150, "CLIPPED"))
    assert _elements(path, "table") == []
    assert [e.value for e in _elements(path, "positioned_text")] == ["CLIPPED"]


def test_thin_filled_rect_rules_do_not_create_extra_columns(tmp_path):
    """細い塗り矩形で描いた罫線は 1 本の境界になり、幅の狭い偽の列を作らない。"""
    rules = "".join(f"{x} 100 1 100 re f\n" for x in (50, 130, 210, 249))
    rules += "".join(f"50 {y} 200 1 re f\n" for y in (100, 150, 199))
    path = _pdf_with(tmp_path / "thin.pdf", rules + _text(60, 170, "A1") + _text(140, 170, "B1")
                     + _text(60, 120, "A2") + _text(140, 120, "B2"))
    tables = _elements(path, "table")
    assert len(tables) == 1
    assert len(tables[0].extension["column_boundaries"]) == 4
    assert len(tables[0].extension["row_boundaries"]) == 3
    assert {e.value for e in _elements(path, "cell")} == {"A1", "B1", "A2", "B2"}


def test_falls_back_to_pypdf_when_pdfium_is_unavailable(monkeypatch):
    """pdfium が使えなくても pypdf 経路で位置付き文字・表が取れる。"""
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    cells = _elements(_OJA_PDF_EASY, "cell")
    assert len(cells) == 9
    assert all(e.value for e in cells)



def test_adjacent_values_across_vertical_rule_stay_in_separate_cells(tmp_path):
    """右詰めの値と隣の列の左詰めの値が近くても、縦罫線を跨いで 1 つのセルに混ざらない。"""
    rules = "".join(f"{x} 100 m {x} 200 l S\n" for x in (50, 100, 200))
    rules += "".join(f"50 {y} m 200 {y} l S\n" for y in (100, 150, 200))
    path = _pdf_with(tmp_path / "adjacent.pdf", rules + _text(55, 170, "NAME") + _text(105, 170, "VALUE")
                     + _text(81.3, 120, "123") + _text(101.5, 120, "ABC"))
    cells = {e.value: e.locator.extension for e in _elements(path, "cell")}
    assert set(cells) == {"NAME", "VALUE", "123", "ABC"}
    assert (cells["123"]["row"], cells["123"]["column"]) == (2, 1)
    assert (cells["ABC"]["row"], cells["ABC"]["column"]) == (2, 2)
