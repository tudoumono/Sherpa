"""`excel_display.extract_cell_metadata` の出力固定テスト（MEM-1・2重フルロード是正のゴールデン）。

`extract_cell_metadata` は元々 `wb_formula`（data_only=False）と `wb_values`（data_only=True）を
**同時に**フルロードしていた（ピークメモリ2倍）。その後 `wb_values` の 1 冊だけを読む形にしたが、出力が
byte-identical であることをこのテストで固定する。期待値（`_EXPECTED_JSON`）は**是正前のコード**を実際に実行して得た実測値
（`json.dumps(sort_keys=True)` の決定的直列化）であり、推測値ではない。

対象セル: formula（cache 有/無）・日付書式・素の整数・文字列・空セル（対象だが未使用の座標）。
"""
from __future__ import annotations

import json
import zipfile
from datetime import datetime
from pathlib import Path

import openpyxl

from sherpa.ingest import excel_display


def _build_fixture_xlsx(dst: Path) -> Path:
    """formula(cache有/無)・日付書式・整数・文字列・空セルを含む最小 xlsx を組み立てる。

    openpyxl が自ら書いた formula には cached value（`<v>`）が付かないため、A3 は
    自然に「cache無し」になる。A4 は保存後に xlsx（zip）を直接いじって `<f>` の直後に
    `<v>84</v>` を差し込み、「cache有り」を作る（Excel で開いて再保存したのと同じ形）。
    """
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    ws["A1"] = 42
    ws["A2"] = "hello"
    ws["A3"] = "=A1+1"          # formula・cache無し
    ws["A4"] = "=A1*2"          # formula・cache有り（後段でXML注入）
    ws["A5"] = datetime(2024, 1, 15)
    ws["A5"].number_format = "yyyy-mm-dd"
    # A6 は触らない（targets には含めるが未使用セル＝空セルの経路を確認する）
    wb.save(dst)

    with zipfile.ZipFile(dst) as zin:
        names = zin.namelist()
        sheet_name = next(n for n in names if n.startswith("xl/worksheets/sheet") and n.endswith(".xml"))
        data = {n: zin.read(n) for n in names}

    xml = data[sheet_name].decode("utf-8")
    idx = xml.index('<c r="A4"')
    end_idx = xml.index("</c>", idx) + len("</c>")
    cell_xml = xml[idx:end_idx]
    f_close = cell_xml.index("</f>") + len("</f>")
    new_cell_xml = cell_xml[:f_close] + "<v>84</v>" + cell_xml[f_close:]
    data[sheet_name] = (xml[:idx] + new_cell_xml + xml[end_idx:]).encode("utf-8")

    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for n in names:
            zout.writestr(n, data[n])
    return dst


# 是正前のコード（同時2重フルロード版）を実際に実行して得た実測出力（推測値ではない）。
_EXPECTED_JSON = (
    '{"Sheet1!A1":{"cached_value":null,"calculation_status":"not_formula","display_reason":null,'
    '"display_source":"raw_value","display_status":"rendered","display_value":"42","formula":null,'
    '"number_format":"General","raw_value":42,"style_id":0,"typed_value":{"type":"integer","value":42}},'
    '"Sheet1!A2":{"cached_value":null,"calculation_status":"not_formula","display_reason":null,'
    '"display_source":"raw_value","display_status":"rendered","display_value":"hello","formula":null,'
    '"number_format":"General","raw_value":"hello","style_id":0,"typed_value":{"type":"string","value":"hello"}},'
    '"Sheet1!A3":{"cached_value":null,"calculation_status":"cache_missing","display_reason":"formula_cache_missing",'
    '"display_source":"excel-display-linux-v1","display_status":"unsupported","display_value":null,'
    '"formula":"=A1+1","number_format":"General","raw_value":null,"style_id":0,'
    '"typed_value":{"type":"blank","value":null}},'
    '"Sheet1!A4":{"cached_value":84,"calculation_status":"cached","display_reason":null,'
    '"display_source":"raw_value","display_status":"rendered","display_value":"84","formula":"=A1*2",'
    '"number_format":"General","raw_value":84,"style_id":0,"typed_value":{"type":"integer","value":84}},'
    '"Sheet1!A5":{"cached_value":null,"calculation_status":"not_formula","display_reason":null,'
    '"display_source":"excel-display-linux-v1","display_status":"rendered","display_value":"2024-01-15",'
    '"formula":null,"number_format":"yyyy-mm-dd","raw_value":45306,"style_id":2,'
    '"typed_value":{"type":"datetime","value":"2024-01-15T00:00:00"}},'
    '"Sheet1!A6":{"cached_value":null,"calculation_status":"not_formula","display_reason":null,'
    '"display_source":"raw_value","display_status":"rendered","display_value":"","formula":null,'
    '"number_format":"General","raw_value":null,"style_id":0,"typed_value":{"type":"blank","value":null}}}'
)


def test_extract_cell_metadata_matches_pre_refactor_golden(tmp_path):
    """逐次化リファクタ後も `extract_cell_metadata` の出力が是正前と完全一致すること。"""
    fixture = _build_fixture_xlsx(tmp_path / "fixture.xlsx")
    targets = {"Sheet1": {"A1", "A2", "A3", "A4", "A5", "A6"}}
    metadata = excel_display.extract_cell_metadata(fixture, targets)
    keyed = {f"{sheet}!{coord}": v for (sheet, coord), v in metadata.items()}
    assert json.dumps(keyed, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == _EXPECTED_JSON


def test_extract_cell_metadata_skips_sheet_missing_from_either_workbook(tmp_path):
    """`targets` に無いシート名を渡しても例外にならず空で返る（シート欠落チェックの契約）。"""
    fixture = _build_fixture_xlsx(tmp_path / "fixture2.xlsx")
    metadata = excel_display.extract_cell_metadata(fixture, {"NoSuchSheet": {"A1"}})
    assert metadata == {}


def test_extract_cell_metadata_loads_one_workbook_when_no_formula_restore_needed(tmp_path, monkeypatch):
    """式の復元が要らないブックは、値ブック（data_only=True）の 1 冊だけを読む（式の無いセルの値は数式ブックと同じ）。"""
    fixture = _build_fixture_xlsx(tmp_path / "fixture3.xlsx")
    orig_load_workbook = openpyxl.load_workbook
    loads: list[dict] = []

    def wrapped_load_workbook(*args, **kwargs):
        loads.append(kwargs)
        return orig_load_workbook(*args, **kwargs)

    monkeypatch.setattr(openpyxl, "load_workbook", wrapped_load_workbook)
    metadata = excel_display.extract_cell_metadata(fixture, {"Sheet1": {"A1", "A3", "A4"}})
    assert [(kw.get("data_only"), kw.get("read_only")) for kw in loads] == [(True, False)]
    assert metadata[("Sheet1", "A1")]["raw_value"] == 42
    assert metadata[("Sheet1", "A4")]["formula"] == "=A1*2"
    assert metadata[("Sheet1", "A4")]["cached_value"] == 84


def test_extract_cell_metadata_restores_shared_formula_followers(tmp_path):
    """共有数式の従属セルは OOXML 直読だと式が空になる。数式ブックから復元した式が入ること。"""
    path = tmp_path / "shared.xlsx"
    wb = openpyxl.Workbook()
    wb.active.title = "Sheet1"
    for row in range(1, 4):
        wb.active.cell(row=row, column=1, value=row * 10)
    wb.save(path)
    with zipfile.ZipFile(path) as zin:
        data = {n: zin.read(n) for n in zin.namelist()}
    xml = data["xl/worksheets/sheet1.xml"].decode("utf-8")

    for row in range(1, 4):
        cell = (f'<c r="B{row}"><f t="shared" ref="B1:B3" si="0">A1*2</f><v>{row * 20}</v></c>' if row == 1
                else f'<c r="B{row}"><f t="shared" si="0"/><v>{row * 20}</v></c>')
        marker = f'<c r="A{row}"'
        start = xml.index(marker)
        end = xml.index("</c>", start) + len("</c>")
        xml = xml[:end] + cell + xml[end:]
    data["xl/worksheets/sheet1.xml"] = xml.encode("utf-8")
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, body in data.items():
            zout.writestr(name, body)

    metadata = excel_display.extract_cell_metadata(path, {"Sheet1": {"B1", "B2", "B3", "A2"}})
    assert metadata[("Sheet1", "B1")]["formula"] == "=A1*2"
    assert metadata[("Sheet1", "B2")]["formula"] == "=A2*2"
    assert metadata[("Sheet1", "B3")]["formula"] == "=A3*2"
    assert metadata[("Sheet1", "B3")]["cached_value"] == 60
    assert metadata[("Sheet1", "A2")]["formula"] is None
