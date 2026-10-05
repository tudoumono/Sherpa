"""原本読取ツール（`sherpa/doc_readers.py`）の契約テスト。

`doc_readers` は開いたバイナリ file object だけを受ける純関数（world/scope/秘匿判定と
path→fd の open は呼び出し元 `agentic_search` の責務）。DB/ES/Neo4j 不要。
"""
from __future__ import annotations

import os
import re
import struct
import tempfile
import time
import zipfile
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import docx
import openpyxl
import pptx
import pytest
from openpyxl import Workbook
from pptx.util import Inches
from pypdf import PdfWriter

from sherpa import doc_readers as DR
from sherpa.agentic_search import _redact
from sherpa.parts.read.tools import _doc_reader_text_locator
from sherpa.redact_keys import KeyBlockRedactor

BEGIN = "-----BEGIN RSA PRIVATE KEY-----"
END = "-----END RSA PRIVATE KEY-----"
ZIP_TOO_BIG = {"error": "大きすぎて開けません（展開サイズ）"}


@pytest.fixture()
def tmp() -> Path:
    return Path(tempfile.mkdtemp())


def _open(p: Path):
    return open(p, "rb")


# ===== 作成補助 =====

def _make_xlsx(path: Path, rows: int = 11, cols: int = 5) -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "Sheet1"
    for r in range(1, rows + 1):
        for c in range(1, cols + 1):
            ws.cell(row=r, column=c, value=f"r{r}c{c}")
    wb.save(path)


def _make_pptx(path: Path, n: int = 3) -> None:
    prs = pptx.Presentation()
    for i in range(n):
        prs.slides.add_slide(prs.slide_layouts[1]).shapes.title.text = f"slide {i}"
    prs.save(path)


def _make_pdf(path: Path, n: int = 7) -> None:
    w = PdfWriter()
    for _ in range(n):
        w.add_blank_page(width=200, height=200)
    with path.open("wb") as f:
        w.write(f)


def _xlsx_with_cells(path: Path, cells: dict[str, str], title: str = "Sheet1") -> None:
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = title
    for ref, v in cells.items():
        ws[ref] = v
    wb.save(path)


def _rewrite_sheet_dimension(src: Path, dst: Path, replacement: bytes) -> None:
    """sheet1.xml の <dimension> を差し替える（空なら削除）。"""
    with zipfile.ZipFile(src) as zin, zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as zout:
        for item in zin.infolist():
            data = zin.read(item.filename)
            if item.filename == "xl/worksheets/sheet1.xml":
                data = re.sub(rb"<dimension[^>]*/>", replacement, data)
            zout.writestr(item.filename, data)


def _find_cd(raw: bytearray, member: str, start: int = 0) -> int:
    name_b = member.encode()
    idx = start
    while True:
        idx = raw.find(b"PK\x01\x02", idx)
        assert idx != -1, f"central directory entry not found: {member}"
        name_len = struct.unpack_from("<H", raw, idx + 28)[0]
        if bytes(raw[idx + 46:idx + 46 + name_len]) == name_b:
            return idx
        idx += 4


def _spoof_cd(path: Path, member: str, fake_size: int, fake_crc: int | None = None) -> None:
    """central directory の宣言展開サイズ（と任意で CRC）だけを書き換える（実データは無改変）。"""
    raw = bytearray(path.read_bytes())
    idx = _find_cd(raw, member)
    if fake_crc is not None:
        struct.pack_into("<I", raw, idx + 16, fake_crc)
    struct.pack_into("<I", raw, idx + 24, fake_size)
    path.write_bytes(bytes(raw))


def _duplicate_cd_entry(path: Path, member: str, new_name: str) -> None:
    """`member` の central directory レコードを同じ header_offset のまま `new_name`（同長）で複製する。"""
    assert len(new_name) == len(member)
    raw = bytearray(path.read_bytes())
    start = _find_cd(raw, member)
    name_len, extra_len, comment_len = struct.unpack_from("<HHH", raw, start + 28)
    rec_len = 46 + name_len + extra_len + comment_len
    record = bytearray(raw[start:start + rec_len])
    record[46:46 + name_len] = new_name.encode()
    eocd = raw.rfind(b"PK\x05\x06")
    assert eocd != -1
    cd_size = struct.unpack_from("<I", raw, eocd + 12)[0]
    n_disk, n_total = struct.unpack_from("<HH", raw, eocd + 8)
    new_raw = raw[:start + rec_len] + record + raw[start + rec_len:]
    e = eocd + len(record)
    struct.pack_into("<H", new_raw, e + 8, n_disk + 1)
    struct.pack_into("<H", new_raw, e + 10, n_total + 1)
    struct.pack_into("<I", new_raw, e + 12, cd_size + len(record))
    path.write_bytes(bytes(new_raw))


def _set_encrypted_flag(path: Path, member: str) -> None:
    """central directory と local header 両方の暗号化ビットを立てる。"""
    raw = bytearray(path.read_bytes())
    cd = _find_cd(raw, member)
    struct.pack_into("<H", raw, cd + 8, struct.unpack_from("<H", raw, cd + 8)[0] | 1)
    name_b = member.encode()
    idx = 0
    while True:
        idx = raw.find(b"PK\x03\x04", idx)
        assert idx != -1, f"local header not found: {member}"
        name_len = struct.unpack_from("<H", raw, idx + 26)[0]
        if bytes(raw[idx + 30:idx + 30 + name_len]) == name_b:
            struct.pack_into("<H", raw, idx + 6, struct.unpack_from("<H", raw, idx + 6)[0] | 1)
            break
        idx += 4
    path.write_bytes(bytes(raw))


# ===== xlsx_sheets / xlsx_range =====

def test_xlsx_sheets_returns_name_and_size(tmp):
    p = tmp / "a.xlsx"
    _make_xlsx(p)
    assert DR.xlsx_sheets(_open(p)) == {"sheets": [{"name": "Sheet1", "max_row": 11, "max_col": 5}]}


def test_xlsx_range_default_and_explicit_a1(tmp):
    p = tmp / "a.xlsx"
    _make_xlsx(p)
    default = DR.xlsx_range(_open(p), "Sheet1")
    assert default["range"] == "A1:E11"
    assert default["rows"][0] == ["r1c1", "r1c2", "r1c3", "r1c4", "r1c5"]
    assert default["truncated"] is False
    assert DR.xlsx_range(_open(p), "Sheet1", "B3:D5") == {
        "sheet": "Sheet1", "range": "B3:D5",
        "rows": [["r3c2", "r3c3", "r3c4"], ["r4c2", "r4c3", "r4c4"], ["r5c2", "r5c3", "r5c4"]],
        "truncated": False}


def test_xlsx_range_truncates_when_exceeding_max_rows_cols(tmp):
    p = tmp / "a.xlsx"
    _make_xlsx(p, rows=10, cols=10)
    r = DR.xlsx_range(_open(p), "Sheet1", max_rows=3, max_cols=2)
    assert r["truncated"] is True and r["range"] == "A1:B3"
    assert len(r["rows"]) == 3 and all(len(row) == 2 for row in r["rows"])


def test_xlsx_range_max_rows_cols_are_clamped_even_if_caller_asks_for_more(tmp):
    p = tmp / "a.xlsx"
    _make_xlsx(p, rows=300, cols=60)
    r = DR.xlsx_range(_open(p), "Sheet1", max_rows=10_000, max_cols=10_000)
    assert len(r["rows"]) == 200 and all(len(row) == 50 for row in r["rows"])
    assert r["truncated"] is True


def test_xlsx_range_none_cell_becomes_empty_string(tmp):
    p = tmp / "a.xlsx"
    _xlsx_with_cells(p, {"A1": "x", "C1": "y"})
    assert DR.xlsx_range(_open(p), "Sheet1", "A1:C1")["rows"] == [["x", "", "y"]]


def test_xlsx_range_unknown_sheet_and_bad_range(tmp):
    p = tmp / "a.xlsx"
    _make_xlsx(p)
    assert DR.xlsx_range(_open(p), "NoSuchSheet") == {"error": "シートが見つかりません"}
    assert DR.xlsx_range(_open(p), "Sheet1", "not-a-range") == {"error": "range が不正です"}


def test_xlsx_range_rejects_row_numbers_beyond_excel_limit(tmp):
    p = tmp / "b.xlsx"
    _xlsx_with_cells(p, {"A1": "x"}, title="Sheet")
    r = DR.xlsx_range(_open(p), "Sheet", "A1000000000000:A1000000000000", clean=lambda x: x)
    assert "error" in r


def test_xlsx_range_without_clean_keeps_raw_cell_truncated_at_200(tmp):
    p = tmp / "a.xlsx"
    _xlsx_with_cells(p, {"A1": "x" * 300})
    assert DR.xlsx_range(_open(p), "Sheet1", "A1:A1")["rows"][0][0] == "x" * 200


@pytest.mark.parametrize("name, reader, error", [
    ("broken.xlsx", lambda f: DR.xlsx_sheets(f), "Excel を開けませんでした"),
    ("broken.docx", lambda f: DR.docx_paragraphs(f), "Word を開けませんでした"),
    ("broken.pptx", lambda f: DR.pptx_slides(f), "PowerPoint を開けませんでした"),
])
def test_broken_office_file_returns_error(tmp, name, reader, error):
    p = tmp / name
    p.write_bytes(b"not an office zip")
    assert reader(_open(p)) == {"error": error}


# ===== 伏せ字（状態付き鍵ブロック）は切り詰め・窓選択より先に原本の順序で掛かる =====

def test_xlsx_range_clean_is_applied_before_cell_truncation(tmp):
    """200 字へ切ってから伏せ字を掛けると、境界をまたぐ秘密鍵が検出をすり抜ける。"""
    p = tmp / "a.xlsx"
    secret = "-----BEGIN PRIVATE KEY-----\n" + "\n".join("A" * 60 for _ in range(5)) + "\n-----END PRIVATE KEY-----"
    assert len(secret) > 200
    _xlsx_with_cells(p, {"A1": secret})
    cell = DR.xlsx_range(_open(p), "Sheet1", "A1:A1", clean=_redact)["rows"][0][0]
    assert "[REDACTED]" in cell and "BEGIN PRIVATE KEY" not in cell and len(cell) <= 200


def test_xlsx_range_key_block_state_survives_cell_truncation(tmp):
    """A1（BEGIN が 200 字切りで欠ける長さ）→A2 鍵本文→A3 END で、A2 が漏れない。"""
    p = tmp / "key.xlsx"
    _xlsx_with_cells(p, {"A1": "x" * 190 + BEGIN, "A2": "A" * 300, "A3": END + " suffix"})
    r = DR.xlsx_range(_open(p), "Sheet1", "A1:A3", clean=_redact)
    a1, a2, a3 = (row[0] for row in r["rows"])
    assert "BEGIN" not in a1 and "[REDACTED]" in a1
    assert a2 == "[REDACTED]" and "AAAA" not in a2
    assert "END" not in a3 and "suffix" in a3


def test_xlsx_range_selected_subrange_still_tracks_key_block_from_sheet_head(tmp):
    """選択範囲より前の行（A1）で始まった鍵ブロックの本文（A2）が、範囲選択で漏れない。"""
    p = tmp / "key_subrange.xlsx"
    _xlsx_with_cells(p, {"A1": BEGIN, "A2": "A" * 100, "A3": END})
    r = DR.xlsx_range(_open(p), "Sheet1", "A2:A3", clean=_redact)
    assert r["range"] == "A2:A3"
    assert r["rows"][0][0] == "[REDACTED]"


def test_xlsx_range_state_scan_includes_columns_left_of_window(tmp):
    p = tmp / "cols.xlsx"
    wb = Workbook()
    wb.active.append([BEGIN, "MIIEowIBAAKCAQEAsecretbody", END])
    wb.save(p)
    r = DR.xlsx_range(_open(p), "Sheet", "B1:B1", clean=_redact)
    assert "secretbody" not in r["rows"][0][0] and r["range"] == "B1:B1"


def test_key_block_redactor_detects_begin_before_base_clean_mangles_it():
    """`secret:\\n-----BEGIN` のように kv パターンが BEGIN を飲み込む入力でも、BEGIN 検出は生文字列で先に行う。"""
    redactor = KeyBlockRedactor(_redact)
    out1 = redactor("secret: x\n-----BEGIN RSA PRIVATE KEY-----")
    assert "BEGIN" not in out1 and "[REDACTED]" in out1
    assert redactor("A" * 100) == "[REDACTED]"
    out3 = redactor("-----END RSA PRIVATE KEY----- tail")
    assert "END" not in out3 and "tail" in out3


# ===== zip 展開サイズ上限（central directory の宣言と実測の両方） =====

def test_xlsx_sheets_rejects_zip_with_huge_declared_uncompressed_entry(tmp, monkeypatch):
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_UNZIP_BYTES", str(10 * 1024 * 1024))
    p = tmp / "bomb.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", b"\x00" * (64 * 1024 * 1024))
    assert os.path.getsize(p) < 1024 * 1024
    assert DR.xlsx_sheets(_open(p)) == ZIP_TOO_BIG


def test_xlsx_sheets_rejects_zip_with_too_many_entries(tmp, monkeypatch):
    monkeypatch.setattr(DR, "_MAX_ZIP_ENTRIES", 5)
    p = tmp / "manyentries.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        for i in range(10):
            zf.writestr(f"part{i}.xml", b"x")
    assert DR.xlsx_sheets(_open(p)) == ZIP_TOO_BIG


@pytest.mark.parametrize("name, member, reader", [
    ("spoofed.xlsx", "xl/worksheets/sheet1.xml", lambda f: DR.xlsx_sheets(f)),
    ("spoofed.docx", "word/document.xml", lambda f: DR.docx_paragraphs(f)),
])
def test_rejects_zip_with_spoofed_small_declared_size(tmp, name, member, reader):
    """宣言サイズだけ小さく偽装した zip（実データ 2MB）は、実際に読み流して CRC 不一致で拒否する。"""
    p = tmp / name
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, b"x" * (2 * 1024 * 1024))
    _spoof_cd(p, member, 500)
    assert reader(_open(p)) == ZIP_TOO_BIG


def test_xlsx_sheets_rejects_zip_with_declared_size_and_crc_spoofed_to_match_truncated_prefix(tmp, monkeypatch):
    """宣言サイズと CRC を「先頭 549 バイト」に合わせて偽装しても、宣言値に依存しない実測で検出する。"""
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_UNZIP_BYTES", str(1 * 1024 * 1024))
    p = tmp / "bomb2.xlsx"
    content = b"<worksheet>" + b"x" * 526 + b"</worksheet>" + b" " * (16 * 1024 * 1024)
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("xl/worksheets/sheet1.xml", content)
    _spoof_cd(p, "xl/worksheets/sheet1.xml", 549, zipfile.crc32(content[:549]) & 0xFFFFFFFF)
    assert DR.xlsx_sheets(_open(p)) == ZIP_TOO_BIG


def test_xlsx_sheets_rejects_zip_with_duplicate_entries_pointing_to_same_local_header(tmp):
    """同じ local header を指す entry の重複（実データの増幅）は、伸長前に圧縮区間の重複で拒否する。"""
    p = tmp / "dup.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("a.xml", b"hello world" * 100)
    _duplicate_cd_entry(p, "a.xml", "b.xml")
    assert DR.xlsx_sheets(_open(p)) == ZIP_TOO_BIG


def test_xlsx_sheets_accepts_normal_multi_entry_zip_without_overlap(tmp):
    p = tmp / "normal.xlsx"
    _make_xlsx(p)
    assert "error" not in DR.xlsx_sheets(_open(p))


def test_xlsx_sheets_encrypted_entry_with_secret_name_does_not_leak_or_raise(tmp):
    """暗号化フラグ付き entry（名前に秘密）でも例外が伝播せず、名前が結果に出ない。"""
    p = tmp / "encrypted.xlsx"
    member = "api_key=SECRET"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr(member, b"dummy content")
    _set_encrypted_flag(p, member)
    result = DR.xlsx_sheets(_open(p))
    assert "SECRET" not in repr(result) and "error" in result


def test_precheck_office_wraps_unexpected_inspection_exception_into_generic_error(tmp, monkeypatch):
    p = tmp / "a.xlsx"
    with zipfile.ZipFile(p, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("x.xml", b"data")

    def _boom(f):
        raise RuntimeError("File 'api_key=SECRET' is encrypted, password required for extraction")

    monkeypatch.setattr(DR, "_zip_actual_size_error", _boom)
    result = DR.xlsx_sheets(_open(p))
    assert result == {"error": "Office ファイルを開けませんでした"}
    assert "SECRET" not in repr(result)


# ===== docx_paragraphs =====

def test_docx_paragraphs_returns_indexed_paragraphs_and_tables(tmp):
    p = tmp / "a.docx"
    d = docx.Document()
    for i in range(5):
        d.add_paragraph(f"para {i}")
    t = d.add_table(rows=2, cols=2)
    t.rows[0].cells[0].text = "h1"
    t.rows[0].cells[1].text = "h2"
    d.save(p)

    r = DR.docx_paragraphs(_open(p), start=0, count=3)
    assert r["total"] == 5
    assert r["paragraphs"] == [{"i": i, "style": "Normal", "text": f"para {i}"} for i in range(3)]
    assert r["tables"] == [{"i": 0, "row_start": 0, "total_rows": 2, "rows": [["h1", "h2"], ["", ""]]}]
    assert r["truncated"] is True
    r2 = DR.docx_paragraphs(_open(p), start=3, count=200)
    assert r2["paragraphs"][0]["i"] == 3 and r2["truncated"] is False


def test_docx_paragraphs_table_and_paragraph_limits(tmp):
    p = tmp / "big.docx"
    d = docx.Document()
    d.add_paragraph("only one")
    for _ in range(25):
        d.add_table(rows=1, cols=1)
    d.save(p)
    r = DR.docx_paragraphs(_open(p))
    assert len(r["tables"]) == 20 and r["truncated"] is True


def test_docx_paragraphs_tables_can_be_paged_with_table_start_and_row_start(tmp):
    p = tmp / "paged.docx"
    d = docx.Document()
    for ti in range(25):
        t = d.add_table(rows=60, cols=1)
        for ri in range(60):
            t.rows[ri].cells[0].text = f"T{ti}R{ri}"
    d.save(p)
    r = DR.docx_paragraphs(_open(p))
    assert r["total_tables"] == 25 and [t["i"] for t in r["tables"]] == list(range(20)) and r["truncated"]
    r2 = DR.docx_paragraphs(_open(p), table_start=20)
    assert [t["i"] for t in r2["tables"]] == [20, 21, 22, 23, 24]
    r3 = DR.docx_paragraphs(_open(p), table_start=3, table_row_start=50)
    assert r3["tables"][0]["i"] == 3 and r3["tables"][0]["rows"][0] == ["T3R50"] and r3["tables"][0]["total_rows"] == 60


def test_docx_paragraphs_applies_clean_to_paragraph_text(tmp):
    p = tmp / "a.docx"
    d = docx.Document()
    d.add_paragraph("token=SECRET123")
    d.save(p)
    assert "[REDACTED]" in DR.docx_paragraphs(_open(p), clean=_redact)["paragraphs"][0]["text"]


def test_docx_paragraphs_key_block_spans_paragraph_table_paragraph(tmp):
    """本文順（段落→表セル→段落）で状態を引き継ぐ。表セルの鍵本文が漏れず、read_evidence 用の合成 text にも出ない。"""
    p = tmp / "keyblock.docx"
    d = docx.Document()
    d.add_paragraph("prefix " + BEGIN)
    d.add_table(rows=1, cols=1).rows[0].cells[0].text = "A" * 200
    d.add_paragraph(END + " suffix")
    d.save(p)

    r = DR.docx_paragraphs(_open(p), clean=_redact)
    p0, p1 = r["paragraphs"][0]["text"], r["paragraphs"][1]["text"]
    assert "BEGIN" not in p0 and "[REDACTED]" in p0 and "prefix" in p0
    assert r["tables"][0]["rows"][0][0] == "[REDACTED]"
    assert "END" not in p1 and "suffix" in p1
    text, _locator = _doc_reader_text_locator("docx_paragraphs", r)
    assert "AAAA" not in text and "BEGIN" not in text and "END" not in text


def test_docx_paragraphs_vertically_merged_cell_end_is_not_double_supplied(tmp):
    """縦結合セル（END）が行数ぶん重複供給されて状態が誤って閉じ、直後の鍵本文（B2）が漏れてはならない。"""
    p = tmp / "merged.docx"
    d = docx.Document()
    d.add_paragraph("prefix " + BEGIN)
    t = d.add_table(rows=2, cols=2)
    t.cell(0, 0).merge(t.cell(1, 0)).text = END
    t.cell(0, 1).text = BEGIN
    t.cell(1, 1).text = "A" * 100
    d.save(p)
    rows = DR.docx_paragraphs(_open(p), clean=_redact)["tables"][0]["rows"]
    assert rows[1][1] == "[REDACTED]"


def test_docx_paragraphs_table_tail_row_state_reaches_following_paragraph(tmp):
    """表の最終行（出力窓の外）の BEGIN が、後続段落の鍵本文に効く。"""
    p = tmp / "tail.docx"
    d = docx.Document()
    d.add_paragraph("前置き")
    t = d.add_table(rows=51, cols=1)
    for ri in range(50):
        t.rows[ri].cells[0].text = f"r{ri}"
    t.rows[50].cells[0].text = BEGIN
    d.add_paragraph("MIIEowIBAAKCAQEAsecretbody\n" + END)
    d.save(p)
    r = DR.docx_paragraphs(_open(p), clean=_redact)
    assert all("secretbody" not in q["text"] for q in r["paragraphs"])


def test_docx_paragraphs_stops_scanning_after_output_window(tmp):
    p = tmp / "huge.docx"
    d = docx.Document()
    d.add_paragraph("先頭")
    t = d.add_table(rows=1200, cols=4)
    for ri in range(1200):
        t.rows[ri].cells[0].text = f"r{ri}"
    d.save(p)
    t0 = time.time()
    r = DR.docx_paragraphs(_open(p), start=0, count=5, clean=lambda x: x)
    assert r["tables"][0]["rows"][0][0] == "r0" and len(r["tables"][0]["rows"]) == 50
    assert time.time() - t0 < 8.0


# ===== pptx_slides =====

def test_pptx_slides_default_and_explicit_pages(tmp):
    p = tmp / "a.pptx"
    _make_pptx(p, n=3)
    r = DR.pptx_slides(_open(p), pages="1-2")
    assert r["total"] == 3 and [s["no"] for s in r["slides"]] == [1, 2]
    assert r["slides"][0]["texts"] == ["slide 0"] and r["truncated"] is False


def test_pptx_slides_page_spec_variants(tmp):
    p = tmp / "a.pptx"
    _make_pptx(p, n=5)
    assert [s["no"] for s in DR.pptx_slides(_open(p), pages="3")["slides"]] == [3]
    assert [s["no"] for s in DR.pptx_slides(_open(p), pages="1,3,5")["slides"]] == [1, 3, 5]
    assert [s["no"] for s in DR.pptx_slides(_open(p), pages="4,9")["slides"]] == [4]


def test_pptx_slides_selected_page_still_tracks_key_block_from_first_slide(tmp):
    p = tmp / "key.pptx"
    prs = pptx.Presentation()
    for text in (BEGIN, "A" * 100, END):
        prs.slides.add_slide(prs.slide_layouts[1]).shapes.title.text = text
    prs.save(p)
    r = DR.pptx_slides(_open(p), pages="2", clean=_redact)
    assert [s["no"] for s in r["slides"]] == [2]
    assert r["slides"][0]["texts"] == ["[REDACTED]"]


def test_pptx_slides_non_contiguous_pages_do_not_carry_key_state_across_gap(tmp):
    """1 枚目 BEGIN・2 枚目 END で閉じた鍵の後、離れた 60 枚目の通常本文は伏せない。"""
    prs = pptx.Presentation()
    for t in [BEGIN, "MIIEkey\n" + END] + ["normal"] * 58:
        s = prs.slides.add_slide(prs.slide_layouts[6])
        s.shapes.add_textbox(Inches(1), Inches(1), Inches(4), Inches(1)).text_frame.text = t
    p = tmp / "gap.pptx"
    prs.save(p)
    r = DR.pptx_slides(_open(p), pages="1,60", clean=_redact)
    assert "normal" in " ".join({sl["no"]: sl for sl in r["slides"]}[60]["texts"])


# ===== pdf_pages / ページ指定 =====

def test_pdf_pages_default_and_range(tmp):
    p = tmp / "a.pdf"
    _make_pdf(p, n=7)
    r = DR.pdf_pages(_open(p))
    assert r["total"] == 7 and [pg["no"] for pg in r["pages"]] == [1, 2, 3, 4, 5]
    assert r["truncated"] is False and all(pg["text"] == "" for pg in r["pages"])
    assert [pg["no"] for pg in DR.pdf_pages(_open(p), pages="2-4")["pages"]] == [2, 3, 4]


@pytest.mark.parametrize("spec, total, max_count, pages, truncated", [
    (None, 25, 10, list(range(1, 11)), True),
    ("", 3, 10, [1, 2, 3], False),
    ("5,1,3-4,3", 10, 10, [1, 3, 4, 5], False),
    ("1-1000000000000", 1, 10, [1], False),
    ("1-1000000000000", 1_000_000, 5, [1, 2, 3, 4, 5], True),
])
def test_parse_page_spec(spec, total, max_count, pages, truncated):
    assert DR._parse_page_spec(spec, total=total, max_count=max_count) == (pages, truncated)


def test_scan_pages_with_lookback_is_per_interval():
    got = DR._scan_pages_with_lookback([1, 300])
    assert got[0] == 1 and 300 in got and 150 not in got and len(got) == 1 + 51


# ===== file_head =====

def test_file_head_reads_within_cap_and_redacts_nothing_itself(tmp):
    p = tmp / "note.txt"
    p.write_text("hello\nworld\n", encoding="utf-8")
    assert DR.file_head(_open(p), max_bytes=1024) == {"size": 12, "text": "hello\nworld\n", "truncated": False}


@pytest.mark.parametrize("encoding, text, cap, expected", [
    ("utf-8", "x" * 100, 10, "x" * 10),
    ("utf-8", "日本語", 4, "日�"),
    ("cp932", "日本語", 3, "日�"),
])
def test_file_head_truncates_at_max_bytes(tmp, encoding, text, cap, expected):
    p = tmp / "note.txt"
    raw = text.encode(encoding)
    p.write_bytes(raw)
    r = DR.file_head(_open(p), max_bytes=cap)
    assert r["truncated"] is True and r["text"] == expected and r["size"] == len(raw)


@pytest.mark.parametrize("encoding", ["utf-8", "cp932"])
def test_file_head_applies_clean(tmp, encoding):
    p = tmp / "note.txt"
    p.write_bytes("架空の設定\npassword=hunter2".encode(encoding))
    r = DR.file_head(_open(p), clean=_redact)
    assert "架空の設定" in r["text"] and "[REDACTED]" in r["text"] and "hunter2" not in r["text"]


def test_file_head_read_os_error_carries_read_io_error_code(tmp):
    """open 後の read() が OSError で失敗したら固定理由コード read_io_failed が付く。"""
    p = tmp / "note.txt"
    p.write_text("hello\n", encoding="utf-8")
    f = _open(p)

    def boom_read(n):
        raise OSError("boom")

    f.read = boom_read
    assert DR.file_head(f) == {"error": "ファイルを開けませんでした", "error_code": "read_io_failed"}


# ===== サイズ・時間の上限 =====

def test_size_limit_blocks_before_opening(tmp, monkeypatch):
    p = tmp / "note.txt"
    p.write_text("x" * 100, encoding="utf-8")
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_BYTES", "10")
    assert DR.file_head(_open(p)) == {"error": "大きすぎて開けません（サイズ）"}
    assert DR.xlsx_sheets(_open(p)) == {"error": "大きすぎて開けません（サイズ）"}


def test_size_limit_invalid_env_falls_back_to_default(tmp, monkeypatch):
    p = tmp / "note.txt"
    p.write_text("hello", encoding="utf-8")
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_BYTES", "not-a-number")
    assert DR.file_head(_open(p)) == {"size": 5, "text": "hello", "truncated": False}


def test_xlsx_range_tail_window_does_not_scan_whole_sheet(tmp):
    p = tmp / "big.xlsx"
    wb = Workbook()
    ws = wb.active
    for i in range(60000):
        ws.append([f"r{i}", "x"])
    wb.save(p)
    t0 = time.time()
    r = DR.xlsx_range(_open(p), "Sheet", "A59990:B60000", clean=lambda x: x)
    assert len(r["rows"]) == 11 and r["rows"][0][0] == "r59989"
    assert time.time() - t0 < 8.0


def test_time_budget_returns_error_instead_of_hanging(tmp, monkeypatch):
    p = tmp / "slow.xlsx"
    wb = Workbook()
    for i in range(3000):
        wb.active.append([i, "x"])
    wb.save(p)
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_SECONDS", "0.0001")
    assert DR.xlsx_range(_open(p), "Sheet", "A2900:B3000", clean=lambda x: x) == DR._TIME_ERROR


# ===== <dimension> 記録が不正・無い・過小なブック =====

def test_xlsx_range_state_scan_ignores_wrong_dimension_record(tmp):
    """<dimension> が無くても、列窓の右の BEGIN が状態に効き、既定範囲も実寸になる。"""
    p = tmp / "dim.xlsx"
    _xlsx_with_cells(p, {"AD3": BEGIN, "B5": "MIIEpAIBAAKCAQEAsecretbody", "AD9": END, "A20": "tail"}, title="S")
    q = tmp / "nodim.xlsx"
    _rewrite_sheet_dimension(p, q, b"")
    r = DR.xlsx_range(_open(q), "S", "A1:B20", clean=_redact)
    assert all("secretbody" not in c for row in r["rows"] for c in row)
    assert DR.xlsx_sheets(_open(q))["sheets"][0]["max_row"] >= 20
    r2 = DR.xlsx_range(_open(q), "S", None, clean=_redact)
    assert r2["range"].endswith("20") and len(r2["rows"]) == 20


def test_xlsx_range_scans_beyond_recorded_dimension_for_key_state(tmp):
    p = tmp / "small_dim.xlsx"
    _xlsx_with_cells(p, {"AD3": BEGIN, "B5": "MIIEpAIBAAKCAQEAsecretbody", "AD9": END}, title="S")
    q = tmp / "small_dim2.xlsx"
    _rewrite_sheet_dimension(p, q, b'<dimension ref="A1:B20"/>')
    r = DR.xlsx_range(_open(q), "S", "A1:B20", clean=_redact)
    assert all("secretbody" not in c for row in r["rows"] for c in row)


def test_xlsx_sheets_and_default_range_ignore_undersized_dimension_record(tmp):
    p = tmp / "u.xlsx"
    _xlsx_with_cells(p, {"AD3": "x", "A20": "y"}, title="S")
    q = tmp / "u2.xlsx"
    _rewrite_sheet_dimension(p, q, b'<dimension ref="A1:B20"/>')
    assert DR.xlsx_sheets(_open(q))["sheets"][0]["max_col"] == 30
    assert DR.xlsx_range(_open(q), "S", None, clean=lambda x: x)["range"] == "A1:AD20"


def test_xlsx_sheets_dimension_calculation_respects_time_budget(tmp, monkeypatch):
    p = tmp / "d.xlsx"
    wb = Workbook()
    for i in range(2000):
        wb.active.append([i])
    wb.save(p)
    q = tmp / "d2.xlsx"
    _rewrite_sheet_dimension(p, q, b"")
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_SECONDS", "0.0001")
    r = DR.xlsx_sheets(_open(q))
    assert r["sheets"][0]["name"] == "Sheet" and r["sheets"][0]["dims_estimated"]
    assert "max_row" not in r["sheets"][0]       # 記録が無い＝不明（0 行と偽らない）


def test_xlsx_sheets_time_budget_is_shared_across_sheets_and_checked_every_row(tmp, monkeypatch):
    p = tmp / "wide.xlsx"
    wb = Workbook()
    for k in range(3):
        ws = wb.active if k == 0 else wb.create_sheet(f"S{k}")
        for _ in range(5):
            ws.append(["x"] * 300)
    wb.save(p)
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_SECONDS", "0.00001")
    r = DR.xlsx_sheets(_open(p))
    assert [sh["name"] for sh in r["sheets"]] == ["Sheet", "S1", "S2"]
    assert all(sh.get("dims_estimated") for sh in r["sheets"])


@pytest.mark.parametrize("prior_range_read", [False, True])
def test_xlsx_sheets_recorded_dims_survive_workbook_reset(tmp, monkeypatch, prior_range_read):
    """記録寸法は reset_dimensions の前に控える。キャッシュ命中の 2 回目／先行の明示範囲読みの後でも、予算超過時に記録値で縮退できる。"""
    p = tmp / "rec.xlsx"
    wb = Workbook()
    for _ in range(29):
        wb.active.append(["a", "b", "c"])
    wb.save(p)
    if prior_range_read:
        assert len(DR.xlsx_range(_open(p), "Sheet", "A1:C1", clean=lambda x: x)["rows"]) == 1
    else:
        assert DR.xlsx_sheets(_open(p))["sheets"][0]["max_row"] == 29
    monkeypatch.setenv("SHERPA_DOC_READ_MAX_SECONDS", "0.00001")
    r = DR.xlsx_sheets(_open(p))
    assert r["sheets"][0]["dims_estimated"] and r["sheets"][0]["max_row"] == 29


# ===== キャッシュ（fstat キー・LRU） =====

def test_cached_load_reuses_object_for_same_file_and_reloads_after_modified(tmp):
    p = tmp / "a.xlsx"
    _make_xlsx(p)
    calls = []

    def loader(f):
        calls.append(f)
        return object()

    obj1 = DR._cached_load(_open(p), loader)
    assert DR._cached_load(_open(p), loader) is obj1 and len(calls) == 1
    os.utime(p, (os.stat(p).st_atime, os.stat(p).st_mtime + 5))
    assert DR._cached_load(_open(p), loader) is not obj1 and len(calls) == 2


class _FakeLoaded:
    def __init__(self, f):
        self._f = f
        self.closed = False

    def close(self):
        self.closed = True


def test_cache_eviction_does_not_close_object_still_held_by_another_caller(tmp):
    DR._cache.clear()
    p_a = tmp / "a.xlsx"
    _make_xlsx(p_a)
    held = DR._cached_load(_open(p_a), _FakeLoaded)
    for i in range(9):                           # LRU（8 件）から A を追い出す
        p_b = tmp / f"b{i}.xlsx"
        _make_xlsx(p_b)
        DR._cached_load(_open(p_b), _FakeLoaded)
    assert held.closed is False


def test_cache_eviction_does_not_break_concurrent_workbook_read(tmp):
    DR._cache.clear()
    p_a = tmp / "a.xlsx"
    _make_xlsx(p_a, rows=3, cols=2)
    held = DR._cached_load(_open(p_a), DR._load_workbook)
    for i in range(9):
        p_b = tmp / f"b{i}.xlsx"
        _make_xlsx(p_b)
        DR._cached_load(_open(p_b), DR._load_workbook)
    rows = [list(row) for row in held["Sheet1"].iter_rows(values_only=True)]
    assert rows == [["r1c1", "r1c2"], ["r2c1", "r2c2"], ["r3c1", "r3c2"]]


def test_cache_skips_large_inputs(tmp, monkeypatch):
    monkeypatch.setattr(DR, "_CACHE_ENTRY_MAX_BYTES", 10)
    DR._cache.clear()
    p = tmp / "x.docx"
    docx.Document().save(p)
    DR.docx_paragraphs(_open(p))
    assert not DR._cache
