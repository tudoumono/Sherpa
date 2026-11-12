"""小さな xlsx でも、巨大な結合範囲・dimension の過小申告・接頭辞つきセルタグで、セル数の上限をすり抜けない。"""
from __future__ import annotations

import zipfile

from sherpa.ingest import office_md
from tests._replace_row import offline_row

_NS = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


def _write(path, sheet_xml):
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet_xml)


def _build(tmp_path, monkeypatch, name, sheet_xml, cap=1_000):
    src, der = tmp_path / "src", tmp_path / "derived"
    src.mkdir()
    _write(src / name, sheet_xml)
    monkeypatch.setattr(office_md, "_XLSX_CELL_CAP", cap)
    return office_md.build_derived(src, der, row=offline_row("w")), der


def test_huge_merge_range_with_tiny_dimension_is_rejected(tmp_path, monkeypatch):
    xml = (f'<worksheet xmlns="{_NS}"><dimension ref="A1:A1"/><sheetData><row r="1"><c r="A1"/></row></sheetData>'
           '<mergeCells count="1"><mergeCell ref="A1:CV1000"/></mergeCells></worksheet>')
    rep, _ = _build(tmp_path, monkeypatch, "merged.xlsx", xml)
    assert rep["failed"] == 1
    assert {"doc": "merged.xlsx", "reason": "cell_count_exceeded"} in rep["conversion_failures"]


def test_understated_dimension_and_prefixed_cell_tags_are_counted(tmp_path, monkeypatch):
    cells = "".join(f'<x:c\nr="A{i + 1}"/>' for i in range(1_100))
    xml = (f'<x:worksheet xmlns:x="{_NS}"><x:dimension ref="A1"/><x:sheetData>{cells}</x:sheetData></x:worksheet>')
    rep, _ = _build(tmp_path, monkeypatch, "lie.xlsx", xml)
    assert {"doc": "lie.xlsx", "reason": "cell_count_exceeded"} in rep["conversion_failures"]


def test_single_quoted_merge_ref_is_counted(tmp_path, monkeypatch):
    xml = (f"<worksheet xmlns=\"{_NS}\"><dimension ref=\"A1:A1\"/><sheetData><row r=\"1\"><c r=\"A1\"/></row></sheetData>"
           "<mergeCells count=\"1\"><mergeCell ref='A1:CV1000'/></mergeCells></worksheet>")
    rep, _ = _build(tmp_path, monkeypatch, "single.xlsx", xml)
    assert {"doc": "single.xlsx", "reason": "cell_count_exceeded"} in rep["conversion_failures"]


def test_uncompressed_size_is_judged_before_reading_sheets(tmp_path, monkeypatch):
    from sherpa.ingest import xlsx_guard
    p = tmp_path / "big.xlsx"
    _write(p, f'<worksheet xmlns="{_NS}"><sheetData/></worksheet>')
    monkeypatch.setattr(office_md, "_OFFICE_UNCOMPRESSED_CAP_BYTES", 10)
    monkeypatch.setattr(xlsx_guard, "cell_load", lambda *a, **k: (_ for _ in ()).throw(AssertionError("シートを読んだ")))
    verdict = office_md._precheck_target(p, ".xlsx", {".xlsx": object()})
    assert verdict[:2] == ("guard", "uncompressed_size_exceeded")


def test_unclosed_merge_tags_do_not_stall_the_cell_count(tmp_path):
    import time
    from sherpa.ingest import xlsx_guard
    p = tmp_path / "unclosed.xlsx"
    _write(p, f'<worksheet xmlns="{_NS}"><mergeCells>' + "<mergeCell ref=x " * 20_000 + "</mergeCells></worksheet>")
    started = time.monotonic()
    xlsx_guard.cell_load(p, 1_000)
    assert time.monotonic() - started < 5


def test_merge_refs_with_long_whitespace_absolute_refs_or_children_are_counted(tmp_path):
    from sherpa.ingest import xlsx_guard
    for i, tag in enumerate(('<mergeCell' + " " * 1100 + 'ref="A1:CV1000"/>',
                             '<mergeCell ref="$A$1:$CV$1000"/>',
                             '<mergeCell ref="A1:CV1000"><x/></mergeCell>')):
        p = tmp_path / f"m{i}.xlsx"
        _write(p, f'<worksheet xmlns="{_NS}"><mergeCells>{tag}</mergeCells></worksheet>')
        assert xlsx_guard.cell_load(p, 1_000) > 1_000, tag[:40]


def test_broken_orphan_sheet_deep_nesting_and_hyperlink_ranges_do_not_slip_past_the_cap(tmp_path):
    from sherpa.ingest import xlsx_guard
    cells = "<c/>" * 1_100
    p = tmp_path / "orphan.xlsx"
    with zipfile.ZipFile(p, "w") as z:
        z.writestr("xl/worksheets/aa_orphan.xml", '<worksheet><broken')
        z.writestr("xl/worksheets/sheet1.xml", f'<worksheet xmlns="{_NS}"><sheetData>{cells}</sheetData></worksheet>')
    assert xlsx_guard.cell_load(p, 1_000) > 1_000
    deep = tmp_path / "deep.xlsx"
    _write(deep, "<worksheet>" + "<a>" * 10_000)
    assert xlsx_guard.cell_load(deep, 1_000) > 1_000
    link = tmp_path / "link.xlsx"
    _write(link, f'<worksheet xmlns="{_NS}"><hyperlinks><hyperlink ref="A1:CV1000" location="S!A1"/></hyperlinks></worksheet>')
    assert xlsx_guard.cell_load(link, 1_000) > 1_000
