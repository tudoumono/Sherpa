"""`evidence_render.py` の key-value 直列化（`〈ヘッダ〉: 「値」`）の文字列契約。

テンプレート箇所は決定的な文字列組み立てなので、IR のバリデーションを経由しない直接呼び出しで確かめる。
group repack・chunk リンク・identifier overflow は `office_md.build_derived()` の実ファイル往復で確かめる。
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import zipfile
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import openpyxl
import pytest

from sherpa import json_io
from sherpa.ingest import context_ir as CIR
from sherpa.ingest import evidence_ir as IR
from sherpa.ingest import evidence_render as R
from sherpa.ingest import office_md

_HASH = "sha256:" + "0" * 64


def _loc(**kw) -> IR.Locator:
    kw.setdefault("part", "xl/worksheets/sheet1.xml")
    return IR.Locator(**kw)


def _cell(eid: str, value, *, row: int, column: int, sheet: str = "明細", **extension) -> IR.EvidenceElement:
    return IR.EvidenceElement(
        element_id=eid, type="cell", parent_id=None, order=row * 100 + column, value=value,
        locator=_loc(sheet=sheet, extension={"row": row, "column": column}),
        coverage_id=f"cov:{eid}", extension=extension)


def _element(eid, etype, value, *, visibility="visible", lifecycle="active", **extension):
    return IR.EvidenceElement(
        element_id=eid, type=etype, parent_id=None, order=0, value=value,
        locator=IR.Locator(part="word/document.xml"), coverage_id=f"cov:{eid}",
        visibility=visibility, lifecycle=lifecycle, extension=extension)


def _region(**kw) -> CIR.ContextRegion:
    defaults = dict(region_id="r1", table_id="t1", sheet="明細", start_column=1, end_column=3,
                    start_row=1, end_row=1, title=None, header_row=1, header_paths=(),
                    mode="grid", confidence=1.0)
    defaults.update(kw)
    return CIR.ContextRegion(**defaults)


def _ctx(source_name="API詳細設計_顧客照会.xlsx", **kw) -> CIR.ContextIR:
    kw.setdefault("document_titles", {})
    return CIR.ContextIR(
        schema_version=CIR.CONTEXT_IR_SCHEMA_VERSION, analyzer_version="test",
        source_hash=_HASH, source_name=source_name, **kw)


def _ir(file_type="xlsx", elements=None) -> IR.EvidenceIR:
    return IR.EvidenceIR(
        schema_version=IR.EVIDENCE_IR_SCHEMA_VERSION, parser_profile="test",
        source=IR.EvidenceSource(file_type=file_type, content_hash=_HASH),
        elements=elements or [])


def _piece(element, *, doc="doc.docx", path=("見出し",), relations=(), native=True, others=()):
    elements = {e.element_id: e for e in (element, *others)}
    return R._element_piece(element, elements, list(relations), doc, list(path),
                            include_native_metadata=native)


def _non_table(long_text="あ" * 1500, **kw):
    element = IR.EvidenceElement(
        element_id="e1", type=kw.pop("etype", "paragraph"), parent_id=None, order=0, value=long_text,
        locator=IR.Locator(part="word/document.xml"), coverage_id="c1", **kw)
    return R._non_table_records(_ir(file_type="docx", elements=[element]), _ctx(source_name="doc.docx"), {}, set())


# ---- _field_piece ----

def test_field_piece_single_and_multi_span():
    cell = _cell("e1", "2026-09-01", row=2, column=1)
    single = R._field_piece(cell, "納期", 0, 1, 0, 10, "2026-09-01", [])
    assert single["semantic"] == single["markdown"] == "納期: 「2026-09-01」"
    multi = R._field_piece(_cell("e1", "長文本体", row=2, column=1), "備考", 0, 2, 0, 4, "長文本体", [])
    assert multi["semantic"] == "備考（1/2）:\n長文本体"


@pytest.mark.parametrize("note,expected", [
    ("continues", "項目: 「値」（同じ縦結合ラベル内の前後記載と連続）"),
    ("separate", "項目: 「値」（隣接行と左ラベルが異なる別項目）"),
])
def test_field_piece_layout_note(note, expected):
    cell = _cell("e1", "値", row=2, column=1)
    assert R._field_piece(cell, "項目", 0, 1, 0, 1, "値", [{"type": note}])["semantic"] == expected


def test_field_piece_order_layout_before_excel_note():
    cell = _cell("e1", "45900", row=2, column=1, raw_value="45900", number_format="yyyy/mm/dd",
                 display_status="rendered", display_value="2025/09/10")
    sem = R._field_piece(cell, "期限", 0, 1, 0, 5, "45900", [{"type": "continues"}])["semantic"]
    assert sem.startswith("期限: 「45900」")
    assert sem.index("（同じ") < sem.index("Excel原値")


# ---- _context_summary_record / _context_prefix ----

def test_context_summary_record_three_labels():
    region = _region(start_row=1, end_row=1, header_row=3)
    cells = [_cell("h1", "No", row=1, column=1), _cell("h2", "業務名", row=3, column=1),
             _cell("h3", "備考欄", row=2, column=1)]
    record = R._context_summary_record(_ir(elements=cells), _ctx(), region, cells, None, {})
    texts = record["semantic_text"].splitlines()[1:]
    assert "領域見出し: 「No」" in texts
    assert "列見出し: 「業務名」" in texts
    assert "領域情報: 「備考欄」" in texts


def test_context_prefix_levels():
    region, ctx = _region(), _ctx()
    base = "出所: 原本「API詳細設計_顧客照会.xlsx」 / シート「明細」"
    assert R._context_prefix(ctx, region, None) == base

    def record(*keys):
        return CIR.ContextRecord(record_id="cr1", region_id="r1", row=2, keys=keys, identifiers=(),
                                 identifier_mentions=(), confidence=1.0)
    k1 = CIR.RecordKey(label="No", value="1", evidence_id="e1")
    k2 = CIR.RecordKey(label="区分 > 明細番号", value="A-1", evidence_id="e2")
    assert R._context_prefix(ctx, region, record(k1)) == base + " / No「1」"
    assert R._context_prefix(ctx, region, record(k1, k2)) == base + " / No「1」、明細番号「A-1」"
    sectioned = _region(title="明細領域", section_path=("第1章", "明細"))
    assert R._context_prefix(ctx, sectioned, None, common_header=("共通区分",)) == (
        base + " / 節「第1章」 / 節「明細」 / 領域「明細領域」 / 区分「共通区分」")


# ---- 非表要素の長文分割（出所前置は維持・状態/asset は先頭 chunk のみ） ----

def test_non_table_long_text_split_keeps_prefix():
    records = _non_table()
    assert len(records) == 2
    assert records[0]["semantic_text"].startswith("原本「doc.docx」の文書「doc」にあるparagraph内容（1/2）:\n")
    assert records[1]["semantic_text"].startswith("原本「doc.docx」の文書「doc」にあるparagraph内容（2/2）:\n")
    assert "は次のとおりである" not in records[0]["semantic_text"]


def test_non_table_long_text_split_keeps_kv_tail_on_first_chunk():
    records = _non_table(visibility="hidden", lifecycle="deleted")
    assert len(records) == 2
    assert records[0]["semantic_text"].endswith("可視性: 「hidden」 / 状態: 「deleted」")
    assert "可視性" not in records[1]["semantic_text"]


def test_non_table_long_text_split_keeps_shape_fill_asset_on_first_chunk():
    records = _non_table(etype="shape", extension={
        "name": "元図形",
        "assets": [{"asset_role": "shape_fill", "asset_sha256": "abc123", "media_part": "word/media/image1.png"}]})
    assert len(records) == 2
    assert records[0]["semantic_text"].endswith(
        "この要素「元図形」には内容未解釈の画像塗りassetが1件存在し、各assetの原本bytesと参照先を保持している。")
    assert records[0]["markdown_text"].endswith(
        "![元図形の画像塗りasset 1/1（内容未解釈）](doc.docx.assets/abc123.png)\n\n画像塗りasset 1/1のSHA-256: abc123")
    assert "画像塗りasset" not in records[1]["semantic_text"]
    assert "画像塗りasset" not in records[1]["markdown_text"]
    assert "![" not in records[1]["markdown_text"]


# ---- _element_piece ----

def test_element_piece_paragraph_kv():
    element = _element("e1", "paragraph", "対象システム: BETA契約管理システム")
    piece = _piece(element, doc="業務フロー補足_契約.docx", path=["文書「業務フロー補足_契約」"])
    assert piece["semantic"] == piece["markdown"] == (
        "出所: 原本「業務フロー補足_契約.docx」 / 文書「業務フロー補足_契約」\n"
        "本文: 「対象システム: BETA契約管理システム」")
    assert "がある。" not in piece["semantic"] and "である。" not in piece["semantic"]


@pytest.mark.parametrize("etype,value,doc,path,native,expected", [
    ("paragraph", "値X", "x.docx", ["文書「x」"], False, "出所: 原本「x.docx」 / 文書「x」\nparagraphの文字列: 「値X」"),
    ("notes", "発表者向け補足メモ", "deck.pptx", ["スライド1"], True,
     "出所: 原本「deck.pptx」 / スライド1\n発表者ノート: 「発表者向け補足メモ」"),
    ("shape", "元図形テキスト", "doc.docx", ["見出し"], True, "出所: 原本「doc.docx」 / 見出し\n図形: 「元図形テキスト」"),
    ("paragraph", "値Y", "doc.docx", ["文書「doc」", "節「第1章」"], True,
     "出所: 原本「doc.docx」 / 文書「doc」 / 節「第1章」\n本文: 「値Y」"),
])
def test_element_piece_type_label_and_source_line(etype, value, doc, path, native, expected):
    assert _piece(_element("e1", etype, value), doc=doc, path=path, native=native)["semantic"] == expected


def _connected_pair(**source_ext):
    target = _element("t1", "shape", "対象図形")
    source = _element("e1", "shape", "元図形", **source_ext)
    relation = IR.EvidenceRelation(relation_id="rel1", type="connects_to", source_id="e1",
                                    target_id="t1", evidence_ids=[], confidence=1.0)
    return source, target, relation


def test_element_piece_relation_text_on_separate_line():
    source, target, relation = _connected_pair()
    piece = _piece(source, relations=[relation], others=[target])
    assert piece["semantic"] == (
        "出所: 原本「doc.docx」 / 見出し\n図形: 「元図形」\nこの要素は対象「対象図形」へ接続している。")


def test_element_piece_relation_target_name_is_not_cut():
    long_name = "長い図形名" * 50                          # 250 字（160 字で切っていた長さを超える）
    target = _element("t1", "shape", long_name)
    source = _element("e1", "shape", "元図形")
    relation = IR.EvidenceRelation(relation_id="rel1", type="connects_to", source_id="e1",
                                   target_id="t1", evidence_ids=[], confidence=1.0)
    sem = _piece(source, relations=[relation], others=[target])["semantic"]
    assert f"この要素は対象「{long_name}」へ接続している" in sem
    huge = "あ" * (R._RELATION_TARGET_NAME_MAX + 500)          # チャンクの上限を壊さない長さに収め、省いた字数を明記する
    target2 = _element("t2", "shape", huge)
    relation2 = IR.EvidenceRelation(relation_id="rel2", type="connects_to", source_id="e1",
                                    target_id="t2", evidence_ids=[], confidence=1.0)
    sem2 = _piece(source, relations=[relation2], others=[target2])["semantic"]
    assert "以下 500 字は対象の要素の本文に全文あり" in sem2 and huge not in sem2
    target3 = _element("t3", "shape", None, name="い" * (R._RELATION_TARGET_NAME_MAX + 7))
    relation3 = IR.EvidenceRelation(relation_id="rel3", type="connects_to", source_id="e1",
                                    target_id="t3", evidence_ids=[], confidence=1.0)
    sem3 = _piece(source, relations=[relation3], others=[target3])["semantic"]
    assert "以下 7 字を省略" in sem3


def test_element_piece_order_relations_before_excel_note_before_state():
    source, target, relation = _connected_pair(
        visibility="hidden", raw_value="1", number_format="General", display_status="unsupported")
    sem = _piece(source, relations=[relation], others=[target])["semantic"]
    assert sem.index("接続している") < sem.index("Excel表示状態") < sem.index("可視性")


def test_element_piece_value_not_altered_or_summarized():
    original = "改行を含む\n値「かぎ括弧」付き"
    piece = _piece(_element("e1", "shape", original), doc="x.pptx", path=["スライド1"])
    assert piece["exact"] == original
    assert f"「{original}」" in piece["semantic"]


def test_element_piece_visibility_state_kv():
    for kw, tail in [({"visibility": "hidden"}, "可視性: 「hidden」"),
                     ({"lifecycle": "deleted"}, "状態: 「deleted」"),
                     ({"visibility": "hidden", "lifecycle": "deleted"}, "可視性: 「hidden」 / 状態: 「deleted」")]:
        assert _piece(_element("e1", "paragraph", "本文", **kw))["semantic"].endswith(tail)


def test_element_piece_shape_fill_asset_description_is_independent_line():
    element = _element("e1", "shape", "元図形", visibility="hidden", lifecycle="deleted", name="元図形",
                       assets=[{"asset_role": "shape_fill", "asset_sha256": "abc123",
                                "media_part": "word/media/image1.png"}])
    sem = _piece(element)["semantic"]
    tail = sem[sem.index("可視性: 「hidden」 / 状態: 「deleted」"):]
    assert "\nこの要素「元図形」には内容未解釈の画像塗りassetが1件存在し、" in tail
    assert " この要素「元図形」には" not in tail


# ---- _excel_value_note ----

def test_excel_value_note_all_six_fields():
    note = R._excel_value_note({
        "raw_value": "45900", "number_format": "yyyy/mm/dd", "display_status": "rendered",
        "display_value": "2025/09/10", "formula": "=A1+1", "display_reason": "date_serial"})
    assert note == (
        "\nExcel原値: 「45900」\nExcel書式: 「yyyy/mm/dd」\nExcel表示状態: rendered"
        "\nExcel表示値: 「2025/09/10」\nExcel数式: 「=A1+1」\nExcel表示理由: date_serial")


def test_excel_value_note_display_value_material_even_if_equal_raw():
    note = R._excel_value_note({
        "raw_value": "10", "number_format": "General", "display_status": "rendered",
        "display_value": "10", "formula": "=SUM(A1:A2)", "display_reason": None})
    assert "Excel表示値: 「10」" in note


def test_excel_value_note_on_non_table_element():
    element = _element("e1", "formula", "10", raw_value="10", number_format="General",
                       display_status="rendered", display_value="10", formula="=SUM(A1:A2)")
    sem = _piece(element, doc="a.xlsx", path=["シート「明細」"])["semantic"]
    assert "Excel表示値: 「10」" in sem and "Excel数式: 「=SUM(A1:A2)」" in sem


# ---- スコープ外の叙述文・coverage notice ----

def test_out_of_scope_sentences_unchanged():
    picture = _element("e1", "picture", None, name="ロゴ")
    assert _piece(picture, path=["見出しA"])["semantic"] == (
        "原本「doc.docx」の見出しAに画像「ロゴ」が存在する。画像内容は未解釈である。")
    ir = _ir(file_type="pptx")
    ir.coverage.append(IR.CoverageItem(
        coverage_id="cov1", scope="shape", detected_kind="chart",
        locator=IR.Locator(part="ppt/slides/slide1.xml", slide=1), status="unsupported",
        content_basis="structured", reason_code="unsupported_shape", parser_id="p", detail={}))
    assert R._coverage_notice_records(ir, "deck.pptx")[0]["semantic_text"] == (
        "原本「deck.pptx」には、chartとして検知した内容があるが、変換結果は未対応である。"
        "理由コードはunsupported_shape、原本位置は"
        '{"part":"ppt/slides/slide1.xml","slide":1}である。内容を抽出済みとは扱わない。')


@pytest.mark.parametrize("file_type,name,reason,detail,expected", [
    ("xlsx", "big.xlsx", "cell_count_exceeded", {"measured_cells": 6203000, "cap_cells": 2000000},
     "セル数が多すぎるため取り込み対象外である（上限2000000セル・このファイル約6203000セル）。"),
    ("docx", "huge.docx", "uncompressed_size_exceeded",
     {"measured_bytes": 900 * 1024 * 1024, "cap_bytes": 500 * 1024 * 1024},
     "展開後サイズが大きすぎるため取り込み対象外である（上限500MiB・このファイル約900MiB）。"),
])
def test_coverage_notice_embeds_measured_values(file_type, name, reason, detail, expected):
    ir = _ir(file_type=file_type)
    ir.coverage.append(IR.CoverageItem(
        coverage_id="cov1", scope="document", detected_kind="legacy_office_binary",
        locator=IR.Locator(part="source-file", object_id="legacy-office-source"), status="failed",
        content_basis="binary_only", reason_code=reason, parser_id="p", detail=detail))
    assert expected in R._coverage_notice_records(ir, name)[0]["semantic_text"]


def test_ai_observation_disclaimer_unchanged():
    from sherpa.ingest import ai_observation

    observation_set = ai_observation.AIObservationSet(
        schema_version="test", source_content_hash=_HASH,
        canonical_generation_id="gen1", provider="openai", model="gpt-test",
        model_revision=None, execution_mode="batch", prompt_schema_version="v1",
        preprocessing_profile="p1", engine_profile_hash="h1", response_hash="r1",
        inputs=[], observations=[], observation_set_hash="obsset1")
    md = R._markdown(_ir(), "a.xlsx", [], {}, observation_set)
    assert "採用AI観測Set: obsset1" in md
    assert "AI観測生成元: openai/gpt-test（batch）" in md
    assert "AI観測は原本確定値ではない。" in md


# ---- 実ファイルの往復（build_derived） ----

def _zip(path: Path, entries: dict):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)


def _dirs() -> tuple[Path, Path]:
    d = tempfile.mkdtemp()
    src = Path(d) / "src"
    src.mkdir()
    return src, Path(d) / "derived"


def _pdf_with(path: Path, page_size, content: str, resources_font=True):
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    page = writer.add_blank_page(width=page_size[0], height=page_size[1])
    font = DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    })
    font_ref = writer._add_object(font)
    page[NameObject("/Resources")] = DictionaryObject(
        {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font_ref})})
    stream = DecodedStreamObject()
    stream.set_data(content.encode("latin-1"))
    page[NameObject("/Contents")] = writer._add_object(stream)
    with path.open("wb") as f:
        writer.write(f)


def _build_derived_pypdf(src: Path, der: Path) -> dict:
    orig = office_md._pdf_backend, office_md._pdf_pages
    try:
        office_md._pdf_backend = lambda: "pypdf"
        office_md._pdf_pages = lambda p: ["ダミー本文"]
        return office_md.build_derived(src, der)
    finally:
        office_md._pdf_backend, office_md._pdf_pages = orig


def _rag_md(der: Path, rel: str) -> str:
    return (der.parent / "rag" / f"{rel}.rag.md").read_text(encoding="utf-8")


def _read_chunks(der: Path, rel: str) -> list[dict]:
    return [json.loads(line) for line in json_io.read_text_maybe_gzip(
        der.parent / "rag" / f"{rel}.rag_chunks.jsonl").splitlines()]


def test_render_shared_path_across_docx_pptx_xlsx_and_pdf_tables():
    src, der = _dirs()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明細"
    ws["A1"], ws["B1"] = "No", "内容"
    ws["A2"], ws["B2"] = 1, "サンプル内容"
    wb.save(src / "a.xlsx")
    _zip(src / "doc.docx", {"word/document.xml": (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body><w:tbl>"
        "<w:tr><w:tc><w:p><w:r><w:t>項目</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>値</w:t></w:r></w:p></w:tc></w:tr>"
        "<w:tr><w:tc><w:p><w:r><w:t>納期</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>2026-09-01</w:t></w:r></w:p></w:tc></w:tr>"
        "</w:tbl></w:body></w:document>")})
    _zip(src / "deck.pptx", {"ppt/slides/slide1.xml": (
        '<?xml version="1.0"?>'
        '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"'
        ' xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
        "<p:cSld><p:spTree><p:graphicFrame>"
        '<p:nvGraphicFramePr><p:cNvPr id="2" name="Table 1"/></p:nvGraphicFramePr>'
        '<p:xfrm><a:off x="0" y="0"/><a:ext cx="1000" cy="1000"/></p:xfrm>'
        '<a:graphic><a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">'
        '<a:tbl><a:tr h="370840">'
        "<a:tc><a:txBody><a:p><a:r><a:t>項目</a:t></a:r></a:p></a:txBody></a:tc>"
        "<a:tc><a:txBody><a:p><a:r><a:t>値</a:t></a:r></a:p></a:txBody></a:tc>"
        '</a:tr><a:tr h="370840">'
        "<a:tc><a:txBody><a:p><a:r><a:t>納期</a:t></a:r></a:p></a:txBody></a:tc>"
        "<a:tc><a:txBody><a:p><a:r><a:t>2026-09-01</a:t></a:r></a:p></a:txBody></a:tc>"
        "</a:tr></a:tbl></a:graphicData></a:graphic></p:graphicFrame></p:spTree></p:cSld></p:sld>")})
    # pdf は縦横の罫線 grid ＋ セルごとに独立した BT/ET の文字列で表にする
    grid = "\n".join([f"{x} 100 m {x} 200 l S" for x in (50, 250, 450)]
                     + [f"50 {y} m 450 {y} l S" for y in (100, 150, 200)])
    cells = [(60, 175, "LABEL"), (260, 175, "VALUE"), (60, 125, "DUEDATE"), (260, 125, "20260901")]
    text = "\n".join(f"BT /F1 10 Tf 1 0 0 1 {x} {y} Tm ({t}) Tj ET" for x, y, t in cells)
    _pdf_with(src / "table.pdf", (500, 300), f"{grid}\n{text}\n")

    rep = _build_derived_pypdf(src, der)
    assert rep["evidence_ir_failed"] == 0 and rep["rag_failed"] == 0
    mds = {n: _rag_md(der, n) for n in ("a.xlsx", "doc.docx", "deck.pptx", "table.pdf")}
    for md in mds.values():
        assert "出所: 原本「" in md
        assert "は「" not in md and "である。" not in md.split("## ", 1)[-1]
    assert "領域見出し: 「No」" in mds["a.xlsx"] and "No: 「1」" in mds["a.xlsx"]
    for n in ("doc.docx", "deck.pptx"):
        assert "領域見出し: 「項目」" in mds[n] and "項目: 「納期」" in mds[n] and "値: 「2026-09-01」" in mds[n]
    pdf_md = mds["table.pdf"]
    assert "領域見出し: 「LABEL」" in pdf_md and "領域見出し: 「VALUE」" in pdf_md
    assert "LABEL: 「DUEDATE」" in pdf_md and "VALUE: 「20260901」" in pdf_md


def test_docx_non_table_paragraph_is_key_value_end_to_end():
    src, der = _dirs()
    _zip(src / "業務フロー補足_契約.docx", {"word/document.xml": (
        '<?xml version="1.0"?>'
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
        "<w:body>"
        '<w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>業務フロー補足_契約</w:t></w:r></w:p>'
        "<w:p><w:r><w:t>対象システム: BETA契約管理システム</w:t></w:r></w:p>"
        "</w:body></w:document>")})
    rep = office_md.build_derived(src, der)
    assert rep["evidence_ir_failed"] == 0 and rep["rag_failed"] == 0
    md = _rag_md(der, "業務フロー補足_契約.docx")
    assert "出所: 原本「業務フロー補足_契約.docx」 / 文書「業務フロー補足_契約」" in md
    assert "本文: 「対象システム: BETA契約管理システム」" in md
    assert "がある。" not in md and "である。" not in md
    bodies = "\n".join(c["body"] for c in _read_chunks(der, "業務フロー補足_契約.docx"))
    assert "対象システム: BETA契約管理システム" in bodies


def test_pdf_non_table_long_text_split_via_real_pdf():
    src, der = _dirs()
    long_text = "A" * 1500
    _pdf_with(src / "note.pdf", (2000, 200), f"BT /F1 12 Tf 10 100 Td ({long_text}) Tj ET\n")
    rep = _build_derived_pypdf(src, der)
    assert rep["evidence_ir_failed"] == 0 and rep["rag_failed"] == 0
    md = _rag_md(der, "note.pdf")
    assert "原本「note.pdf」のページ1にあるpositioned_text内容（1/2）:\n" + long_text[:1200] in md
    assert "原本「note.pdf」のページ1にあるpositioned_text内容（2/2）:\n" + long_text[1200:] in md
    assert "は次のとおりである" not in md


def _build_wide_xlsx(ncols: int) -> Path:
    src, der = _dirs()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明細"
    for c in range(1, ncols + 1):
        ws.cell(row=1, column=c, value=f"項目{c:03d}")
        ws.cell(row=2, column=c, value=f"値{c:03d}の内容テキストはグループ分割の閾値を試すために十分に長くしてある")
    wb.save(src / "big.xlsx")
    office_md.build_derived(src, der)
    return der


@pytest.fixture(scope="module")
def wide_der() -> Path:
    return _build_wide_xlsx(60)


def test_group_repack_at_max_group_chars_boundary_no_data_loss(wide_der):
    chunks = _read_chunks(wide_der, "big.xlsx")
    table_chunks = [c for c in chunks if c["content_type"] == "table_record"]
    assert len({c["logical_record_id"] for c in table_chunks}) == 1
    assert table_chunks[0]["field_group_count"] > 1
    table_chunks.sort(key=lambda c: c["field_group_index"])
    body = "".join(c["body"] for c in table_chunks)
    assert all(f"値{c:03d}の内容" in body for c in range(1, 61))

    ir = IR.read_json_file(wide_der.parent / "ir" / "big.xlsx.evidence.json")
    cell_ids = {el.element_id for el in ir.elements if el.type == "cell" and R._value_text(el.value)}
    cited_ids = {citation["evidence_id"] for chunk in chunks for citation in chunk["citations"]}
    assert cell_ids <= cited_ids


def test_chunk_links_consistent_after_repack(wide_der):
    chunks = _read_chunks(wide_der, "big.xlsx")
    by_id = {c["chunk_id"]: c for c in chunks}
    for index, chunk in enumerate(chunks):
        assert chunk["previous_chunk_id"] == (chunks[index - 1]["chunk_id"] if index > 0 else None)
        assert chunk["next_chunk_id"] == (chunks[index + 1]["chunk_id"] if index + 1 < len(chunks) else None)
    table_chunks = [c for c in chunks if c["content_type"] == "table_record"]
    for chunk in table_chunks:
        expected = {c["chunk_id"] for c in table_chunks
                    if c["logical_record_id"] == chunk["logical_record_id"]} - {chunk["chunk_id"]}
        assert set(chunk["sibling_chunk_ids"]) == expected
        assert expected <= set(by_id)


def test_identifier_mentions_overflow_caps_at_limit_with_exact_value_set():
    """見出し `項NNN`・値 `ANNN` の 149 列で 1 group 内の識別子上限 128 を超えさせる。"""
    src, der = _dirs()
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "明細"
    ws.cell(row=1, column=1, value="No")
    ws.cell(row=2, column=1, value=1)
    for c in range(2, 151):
        ws.cell(row=1, column=c, value=f"項{c:03d}")
        ws.cell(row=2, column=c, value=f"A{c:03d}")
    wb.save(src / "ids.xlsx")
    office_md.build_derived(src, der)

    chunks = _read_chunks(der, "ids.xlsx")
    data_chunks = sorted((c for c in chunks if c["content_type"] != "context_summary"),
                         key=lambda c: c["field_group_index"])
    assert len(data_chunks) == 2
    group1, group2 = data_chunks
    assert group1["field_group_count"] == 2 and group2["field_group_count"] == 2
    kept1 = {m["value"] for m in group1["identifier_mentions"]}
    assert kept1 == {f"A{c:03d}" for c in range(2, 130)}
    assert group1["identifier_mention_overflow_count"] == 6
    assert group1["identifier_mention_count"] == len(kept1) + 6
    for c in range(130, 136):
        assert f"A{c:03d}" in group1["body"]
    kept2 = {m["value"] for m in group2["identifier_mentions"]}
    assert kept2 == {f"A{c:03d}" for c in range(136, 151)}
    assert group2["identifier_mention_overflow_count"] == 0
    assert kept1.isdisjoint(kept2)


def test_chunk_versions_are_v1alpha10():
    chunks = _read_chunks(_build_wide_xlsx(4), "big.xlsx")
    assert chunks
    for chunk in chunks:
        assert chunk["renderer_version"] == "evidence-rag-renderer-v1alpha11"
        assert chunk["chunker_version"] == "evidence-rag-chunker-v1alpha10"
    assert R.RAG_RENDERER_VERSION == "evidence-rag-renderer-v1alpha11"
    assert R.RAG_CHUNKER_VERSION == "evidence-rag-chunker-v1alpha10"


def test_markdown_anchors_precede_each_chunk_and_match_chunk_ids_1to1():
    der = _build_wide_xlsx(2)
    chunks = _read_chunks(der, "big.xlsx")
    md = _rag_md(der, "big.xlsx")
    anchor_ids = re.findall(r"^<!-- chunk:(\S+) -->$", md, flags=re.MULTILINE)
    assert set(anchor_ids) == {c["chunk_id"] for c in chunks}
    assert len(anchor_ids) == len(set(anchor_ids))
    for chunk in chunks:
        assert md.count(f"<!-- chunk:{chunk['chunk_id']} -->") == 1


def test_render_validation_errors_catch_anchor_chunk_mismatch():
    ir = _ir(file_type="docx")
    ir.elements.append(_element("e1", "paragraph", "本文A"))
    result = R.RenderedEvidence(
        markdown="<!-- chunk:wrong-id -->\n本文A\n",
        chunks=[{"chunk_id": "rag-chunk:actual", "citations": [{"evidence_id": "e1"}],
                 "logical_record_id": "l1", "field_group_index": 1, "field_group_count": 1,
                 "sibling_chunk_ids": [], "source_rel_path": "doc.docx", "evidence_tier": "canonical",
                 "coverage_statuses": [], "has_unresolved_coverage": False, "needs_optional_vision": False}],
        coverage_summary={})
    assert "rag_md_anchor_mismatch" in R.validation_errors(ir, result)


# ---- 可視性・廃止表現の KV 直列化 ----

@pytest.mark.parametrize("ext,expected", [
    ({"visibility_reason": "occluded_by_picture"}, ["可視性: 「画像に覆われている」"]),
    ({"visibility_reason": "occluded_by_shape"}, ["可視性: 「図形に覆われている」"]),
    ({"visibility_reason": "hidden_sheet"}, ["可視性: 「シートが非表示」"]),
    ({"visibility_reason": "very_hidden"}, ["可視性: 「シートが完全非表示」"]),
    ({"visibility_reason": "hidden_row"}, ["可視性: 「行が非表示」"]),
    ({"visibility_reason": "hidden_column"}, ["可視性: 「列が非表示」"]),
    ({"visibility_reason": "hidden_run"}, ["可視性: 「非表示文字」"]),
    ({"visibility_reason": "hidden_slide"}, ["可視性: 「スライドが非表示」"]),
    ({"visibility_reason": "hidden_slide_inherited"}, ["可視性: 「非表示スライド内の要素」"]),
    ({"visibility_reason": "off_slide"}, ["可視性: 「スライド範囲外」"]),
    ({"visibility_reason": "occluded"}, ["可視性: 「図形に覆われている」"]),
    ({"visibility_reason": "strike"}, ["取り消し線: 「あり」"]),
    ({"occluded_by": {"kind": "shape", "text": "廃止"}}, ["重なり: 「廃止」"]),
    ({"covered_by_text": {"element_id": "shape:2", "text": "廃止"}}, ["重なり: 「廃止」"]),
    ({"occluded_by": {"kind": "picture", "z_index": 3}}, []),
    ({"floating_anchors": [{"behind_doc": True, "text": "透かし画像"}, {"behind_doc": False, "name": "図形1"}]},
     ["背面図形: 「透かし画像」", "前面図形: 「図形1」"]),
    ({"floating_anchors": [{"behind_doc": True}]}, []),
    ({"visibility_reason": "some_future_reason"}, []),
    ({}, []),
])
def test_occlusion_kv_lines(ext, expected):
    assert R._occlusion_kv_lines(ext) == expected


@pytest.mark.parametrize("value,label,ext,expected", [
    ("使用中", "状態", {"visibility_reason": "occluded_by_picture",
                       "occluded_by": {"kind": "picture", "element_id": "shape:1", "z_order": 2}},
     "可視性: 「画像に覆われている」"),
    ("廃止予定", "状態", {"visibility_reason": "strike"}, "取り消し線: 「あり」"),
    ("非表示行", "状態", {"visibility_reason": "hidden_row"}, "可視性: 「行が非表示」"),
    ("X02", "内部コード", {"visibility_reason": "hidden_column"}, "可視性: 「列が非表示」"),
])
def test_field_piece_occlusion_kv(value, label, ext, expected):
    piece = R._field_piece(_cell("e1", value, row=3, column=2, **ext), label, 0, 1, 0, len(value), value, [])
    assert expected in piece["semantic"] and expected in piece["exact"]
    assert "occluded_by_picture" not in piece["semantic"]


def test_field_piece_occlusion_kv_only_on_first_span():
    cell = _cell("e1", "長文", row=2, column=2, visibility_reason="strike")
    p0 = R._field_piece(cell, "備考", 0, 2, 0, 2, "長文A", [])
    p1 = R._field_piece(cell, "備考", 1, 2, 2, 4, "長文B", [])
    assert "取り消し線" in p0["semantic"] and "取り消し線" not in p1["semantic"]


@pytest.mark.parametrize("etype,value,ext,doc,path,expected", [
    ("hidden_text", "内部メモ", {"visibility": "hidden", "visibility_reason": "hidden_run"},
     "doc.docx", "見出し", "可視性: 「非表示文字」"),
    ("strike_text", "旧料金プラン", {"visibility_reason": "strike"}, "doc.docx", "見出し", "取り消し線: 「あり」"),
    ("shape", "旧料金体系: 月額1000円", {"covered_by_text": {"element_id": "shape:2", "text": "廃止"}},
     "deck.pptx", "スライド2", "重なり: 「廃止」"),
    ("shape", "旧仕様メモ", {"visibility": "hidden", "visibility_reason": "off_slide"},
     "deck.pptx", "スライド3", "可視性: 「スライド範囲外」"),
    ("paragraph", "対象システム", {"floating_anchors": [{"behind_doc": True, "name": "透かし画像"}]},
     "doc.docx", "見出し", "背面図形: 「透かし画像」"),
])
def test_element_piece_occlusion_kv(etype, value, ext, doc, path, expected):
    assert expected in _piece(_element("e1", etype, value, **ext), doc=doc, path=[path])["semantic"]


def test_element_piece_mapped_reason_replaces_raw_visibility_and_no_native_suppresses():
    element = _element("e1", "hidden_text", "内部メモ", visibility="hidden", visibility_reason="hidden_run")
    assert "「hidden」" not in _piece(element)["semantic"]
    covered = _element("e1", "shape", "旧料金体系", covered_by_text={"element_id": "shape:2", "text": "廃止"})
    assert "重なり" not in _piece(covered, doc="deck.pptx", path=["スライド2"], native=False)["semantic"]
