"""Office→決定的MD 変換（office_md）の単体テスト（OOXML 直パース・DB不要）。

docx/pptx は最小 OOXML を zip で組み、xlsx は openpyxl で実ファイルを作る。PDF/旧形式/壊れファイルは
None（未対応）であることも確認する。
"""
from __future__ import annotations

import pathlib
import shutil
import zipfile
from types import SimpleNamespace

import openpyxl
import pytest
from pypdf import PdfWriter

from sherpa import json_io
from sherpa.ingest import office_md

_DOCX_XML = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:p><w:pPr><w:pStyle w:val="Heading1"/></w:pPr><w:r><w:t>タイトル見出し</w:t></w:r></w:p>
  <w:p><w:r><w:t>本文テキストABC</w:t></w:r></w:p>
  <w:tbl><w:tr><w:tc><w:p><w:r><w:t>セル1</w:t></w:r></w:p></w:tc>
   <w:tc><w:p><w:r><w:t>セル2</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
 </w:body>
</w:document>"""

_DOCX_MERGED_NESTED_XML = """<?xml version="1.0"?>
<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
 <w:body>
  <w:tbl>
   <w:tr>
    <w:tc><w:tcPr><w:gridSpan w:val="2"/></w:tcPr><w:p><w:r><w:t>見出し結合</w:t></w:r></w:p></w:tc>
   </w:tr>
   <w:tr>
    <w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr><w:p><w:r><w:t>縦結合</w:t></w:r></w:p></w:tc>
    <w:tc><w:p><w:r><w:t>値2</w:t></w:r></w:p></w:tc>
   </w:tr>
   <w:tr>
    <w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc>
    <w:tc><w:p><w:r><w:t>値3</w:t></w:r></w:p></w:tc>
   </w:tr>
  </w:tbl>
  <w:tbl>
   <w:tr>
    <w:tc>
     <w:p><w:r><w:t>外側セル</w:t></w:r></w:p>
     <w:tbl><w:tr><w:tc><w:p><w:r><w:t>ネスト値</w:t></w:r></w:p></w:tc></w:tr></w:tbl>
    </w:tc>
   </w:tr>
  </w:tbl>
 </w:body>
</w:document>"""

_PPTX_SLIDE = """<?xml version="1.0"?>
<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
       xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
 <p:cSld><p:spTree><a:t>スライド本文XYZ</a:t></p:spTree></p:cSld>
</p:sld>"""

_EMPTY_DOCX_XML = ('<?xml version="1.0"?>'
                   '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                   "<w:body></w:body></w:document>")
_SHEET = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
          "<sheetData>{cells}</sheetData></worksheet>")


def _zip(path, entries: dict):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)


def _blank_pdf(path: pathlib.Path) -> None:
    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    with path.open("wb") as stream:
        writer.write(stream)


def _xlsx(path: pathlib.Path, **cells) -> pathlib.Path:
    wb = openpyxl.Workbook()
    for addr, value in cells.items():
        wb.active[addr] = value
    wb.save(path)
    return path


def _dirs(tmp_path):
    src = tmp_path / "src"
    src.mkdir()
    return src, tmp_path / "derived"


@pytest.fixture
def pdf_stub(monkeypatch):
    """PDF バックエンド/ページ抽出を差し替える（`pdf_stub(backend, pages)`・後始末は monkeypatch）。"""
    def apply(backend, pages=None):
        monkeypatch.setattr(office_md, "_pdf_backend", lambda: backend)
        if pages is not None:
            monkeypatch.setattr(office_md, "_pdf_pages", lambda p: pages)
    return apply


def _notice_meta(der, rel, reason):
    assert (der / f"{rel}.md").is_file()                                   # notice が発行される（消えない）
    meta = json_io.read_json(der / f"{rel}.md.meta.json", default=None)
    assert meta is not None and meta["arm"] == "evidence_notice"
    assert f"reason_code={reason}" in meta["notes"]


# ---- to_markdown ----

def test_docx_to_md_heading_body_table(tmp_path):
    p = tmp_path / "a.docx"
    _zip(p, {"word/document.xml": _DOCX_XML})
    md = office_md.to_markdown(p)
    assert md is not None
    assert "# タイトル見出し" in md and "本文テキストABC" in md and "| セル1 | セル2 |" in md


def test_docx_to_md_merged_cells_and_nested_table(tmp_path):
    """row_span/column_span を値の継続セルへ複製し、ネスト表はパイプ表の直後に小見出し付きで続ける。"""
    p = tmp_path / "merged.docx"
    _zip(p, {"word/document.xml": _DOCX_MERGED_NESTED_XML})
    md = office_md.to_markdown(p)
    assert md is not None
    assert "| 見出し結合 | 見出し結合 |" in md           # gridSpan=2
    assert "| 縦結合 | 値2 |" in md and "| 縦結合 | 値3 |" in md     # vMerge 継続セルへ起点の値を複製
    assert "外側セル" in md and "ネスト値" in md
    assert "#### ネスト表（1行1列）" in md


def test_pptx_to_md_slide_text(tmp_path):
    p = tmp_path / "a.pptx"
    _zip(p, {"ppt/slides/slide1.xml": _PPTX_SLIDE})
    md = office_md.to_markdown(p)
    assert md is not None and "## スライド 1" in md and "スライド本文XYZ" in md


def test_xlsx_to_md_values(tmp_path):
    """シート丸ごと1枚ではなく、`regions()` が検出した表候補ごとに `### {セル範囲}` 小見出し＋パイプ表を出す。"""
    p = tmp_path / "a.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "シート1"
    ws["A1"], ws["B1"] = "項目", "値"
    ws["A2"], ws["B2"] = "売上", "消費税率10%"
    wb.save(p)
    md = office_md.to_markdown(p)
    assert md is not None
    assert "## シート「シート1」" in md and "### A1:B2" in md
    assert "| 項目 | 値 |" in md and "| 売上 | 消費税率10% |" in md


def test_xlsx_to_md_merged_cell_and_multiple_regions(tmp_path):
    """結合セルは値を継続セルへ複製（R5）。癒着していない複数の表候補は独立した `### {セル範囲}` になる。"""
    p = tmp_path / "b.xlsx"
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "台帳"
    ws["A1"] = "見出し結合"
    ws.merge_cells("A1:B1")
    ws["A2"], ws["B2"] = "行", "値"
    ws["D1"], ws["E1"] = "甲", "乙"                    # 列 C を空けて別の連結成分にする
    ws["D2"], ws["E2"] = "丙", "丁"
    wb.save(p)
    md = office_md.to_markdown(p)
    assert md is not None and "## シート「台帳」" in md
    assert "### A1:B2" in md and "### D1:E2" in md
    assert "| 見出し結合 | 見出し結合 |" in md and "| 行 | 値 |" in md
    assert "| 甲 | 乙 |" in md and "| 丙 | 丁 |" in md
    assert md.index("A1:B2") < md.index("D1:E2")       # 出現順は regions() の (min_row, min_col) 順


def test_unsupported_and_broken_return_none(tmp_path):
    pdf = tmp_path / "a.pdf"
    pdf.write_bytes(b"%PDF-1.4 dummy")
    assert office_md.to_markdown(pdf) is None
    bad = tmp_path / "b.docx"
    bad.write_bytes(b"not a zip")
    assert office_md.to_markdown(bad) is None


# ---- PDF（テキスト層）: バックエンド未導入でも配線・整形をスタブで検証 ----

def test_pdf_normalize_deterministic():
    n = office_md._normalize_pdf_text
    assert n("a  \n\n\n b \r\nc") == "a\n\n b\nc"     # 行末空白除去・連続空行→1・CRLF正規化
    assert n("   ") == "" and n("") == ""


def test_pdf_md_assembly_stubbed(pdf_stub):
    """PDF は H2 の対象外のまま据え置く。バイト完全一致で固定する。"""
    pdf_stub("pypdf", ["ページ1の本文  ", "", "  二枚目\nの本文 "])      # 2枚目は空
    assert office_md.to_markdown(pathlib.Path("x.pdf")) == "## ページ 1\n\nページ1の本文\n\n## ページ 3\n\n二枚目\nの本文"


def test_pdf_all_empty_returns_none_stubbed(pdf_stub):
    pdf_stub("pypdf", ["", "   ", "\f"])                         # スキャン画像/暗号化＝本文ゼロ
    assert office_md.to_markdown(pathlib.Path("x.pdf")) is None


def test_convertible_exts_tracks_backend(pdf_stub):
    pdf_stub(None)
    assert ".pdf" not in office_md.convertible_exts() and not office_md.pdf_available()
    pdf_stub("pypdf")
    assert ".pdf" in office_md.convertible_exts() and office_md.pdf_available()


def test_build_derived_pdf_buckets(tmp_path, pdf_stub):
    src, der = _dirs(tmp_path)
    _blank_pdf(src / "doc.pdf")
    pdf_stub(None)
    rep = office_md.build_derived(src, der)
    assert rep["unsupported"] == 1 and rep["converted"] == 0
    pdf_stub("pypdf", ["税率10%の説明"])
    rep = office_md.build_derived(src, der)
    assert rep["converted"] == 1 and rep["unsupported"] == 0
    assert "税率10%" in (der / "doc.pdf.md").read_text(encoding="utf-8")


def test_build_derived_reports_actual_candidate_progress(tmp_path, pdf_stub):
    """未対応・破損を含め、候補文書を1件処理するごとに実数を報告する。"""
    src, der = _dirs(tmp_path)
    _blank_pdf(src / "unsupported.pdf")
    (src / "broken.docx").write_bytes(b"not a zip")
    pdf_stub(None)
    observed: list[tuple[int, int]] = []
    office_md.build_derived(src, der, progress=lambda processed, total: observed.append((processed, total)))
    assert observed[0] == (0, 2) and observed[-1] == (2, 2)
    assert [processed for processed, _total in observed] == [0, 1, 2]


# ---- 秘匿名の原本は派生物を作らない／drift を恒常化させない ----

def test_sensitive_docx_name_has_no_derived_and_never_flags_drift_or_missing(tmp_path):
    """秘匿名（`id_rsa.docx`）は変換ループが派生 `.md`・マニフェストを一切作らない。これを欠落/drift と誤検知すると
    sync ごとに world 全体の全再構築が反復する——2回連続の build 後も欠落・drift と判定されない。
    非秘匿の `normal.docx` は従来どおり変換・判定対象。"""
    src, der = _dirs(tmp_path)
    _zip(src / "id_rsa.docx", {"word/document.xml": _DOCX_XML})
    _zip(src / "normal.docx", {"word/document.xml": _DOCX_XML})
    rep = office_md.build_derived(src, der)
    assert rep["converted"] == 1                       # normal.docx のみ
    assert not (der / "id_rsa.docx.md").exists() and (der / "normal.docx.md").exists()
    assert "タイトル見出し" in (der / "normal.docx.md").read_text(encoding="utf-8")
    assert not (der.parent / "ir" / "id_rsa.docx.derived.json").exists()
    assert (der.parent / "ir" / "normal.docx.derived.json").exists()
    for _ in range(2):
        assert office_md.rag_sidecars_missing(src, der) is False
        assert office_md.human_md_sig_drift(src, der) is False
        office_md.build_derived(src, der)


def test_refresh_evidence_ir_skips_sensitive_original_name(tmp_path, pdf_stub):
    """秘匿名 PDF は `.md` 欠落だけでは対象外にならない経路があるため、明示除外しないと平文で `.evidence.json`/
    `.rag.md` へ書き出される。非秘匿の `note.pdf` は従来どおり生成される。"""
    src, der = _dirs(tmp_path)
    der.mkdir()
    _blank_pdf(src / "id_rsa.pdf")
    _blank_pdf(src / "note.pdf")
    pdf_stub("pypdf", ["秘密の本文テキスト"])
    rep = office_md.refresh_evidence_ir(src, der)
    assert rep["evidence_ir_failed"] == 0 and rep["rag_failed"] == 0
    assert rep["evidence_ir_generated"] == 1 and rep["rag_generated"] == 1     # note.pdf のみ
    for p in ("ir/id_rsa.pdf.evidence.json", "rag/id_rsa.pdf.rag.md", "ir/id_rsa.pdf.derived.json"):
        assert not (der.parent / p).exists()
    assert (der.parent / "ir" / "note.pdf.evidence.json").exists() and (der.parent / "rag" / "note.pdf.rag.md").exists()


def test_refresh_rag_skips_sensitive_original_with_stale_evidence_json(tmp_path):
    """秘匿名導入前に生成され残存する `.evidence.json`/`.rag.md`/`.rag_chunks.jsonl` は、`refresh_rag` が再生成せず
    cleanup で削除する。非秘匿の `normal.docx` は従来どおり再生成される。"""
    src, der = _dirs(tmp_path)
    _zip(src / "id_rsa.docx", {"word/document.xml": _DOCX_XML})
    _zip(src / "normal.docx", {"word/document.xml": _DOCX_XML})
    office_md.build_derived(src, der)
    ir_dir, rag_dir = der.parent / "ir", der.parent / "rag"
    assert not (ir_dir / "id_rsa.docx.evidence.json").exists()
    shutil.copyfile(ir_dir / "normal.docx.evidence.json", ir_dir / "id_rsa.docx.evidence.json")
    shutil.copyfile(rag_dir / "normal.docx.rag.md", rag_dir / "id_rsa.docx.rag.md")
    shutil.copyfile(rag_dir / "normal.docx.rag_chunks.jsonl", rag_dir / "id_rsa.docx.rag_chunks.jsonl")

    rep = office_md.refresh_rag(src, der)
    assert rep["rag_failed"] == 0 and rep["rag_generated"] == 1                # normal.docx のみ
    assert not (rag_dir / "id_rsa.docx.rag.md").exists() and not (rag_dir / "id_rsa.docx.rag_chunks.jsonl").exists()
    assert (rag_dir / "normal.docx.rag.md").exists()


def test_refresh_human_md_skips_sensitive_original_name(tmp_path):
    """除外しないと、秘匿本文が IR 経由で `{rel}.md` へ平文で書き出される。"""
    src, der = _dirs(tmp_path)
    der.mkdir()
    _xlsx(src / "id_rsa.xlsx", A1="秘密の値")
    _xlsx(src / "a.xlsx", A1="通常の値")
    rep = office_md.refresh_human_md(src, der)
    assert rep["human_md_generated"] == 1 and rep["human_md_failed"] == 0      # a.xlsx のみ
    assert not (der / "id_rsa.xlsx.md").exists() and not (der.parent / "ir" / "id_rsa.xlsx.derived.json").exists()
    assert (der / "a.xlsx.md").exists()


def test_refresh_document_ir_skips_sensitive_original_name(tmp_path):
    """rename 等で derived 側に旧 `.md` が残っていても、秘匿名は document_ir の軽量再生成対象から除外する。"""
    src, der = _dirs(tmp_path)
    der.mkdir()
    _xlsx(src / "id_rsa.xlsx", A1="秘密の値")
    _xlsx(src / "a.xlsx", A1="通常の値")
    (der / "id_rsa.xlsx.md").write_text("stale leftover", encoding="utf-8")
    (der / "a.xlsx.md").write_text("placeholder", encoding="utf-8")
    rep = office_md.refresh_document_ir(src, der)
    assert rep["document_ir_failed"] == 0 and rep["document_ir_generated"] == 1    # a.xlsx のみ
    assert not (der.parent / "ir" / "id_rsa.xlsx.document.json").exists()
    assert (der.parent / "ir" / "a.xlsx.document.json").exists()


# ---- 部分抽出の検知（ING-1）----

@pytest.mark.parametrize("size,md,expected", [
    ("big", "少しだけ", "flag"),             # 原本が大きいのに MD が極端に小さい
    ("small", "", "none"),                  # 小さい原本は MD も小さくて正常
    ("big", "本文" * 10000, "none"),         # 大きい原本でも MD が十分な量なら疑わない
])
def test_check_partial_extraction_size_ratio(tmp_path, size, md, expected):
    rp = tmp_path / "x.docx"
    n = office_md._PARTIAL_SIZE_MIN_SOURCE_BYTES + 1 if size == "big" else 100
    rp.write_bytes(b"x" * n)
    out: list[dict] = []
    office_md._check_partial_extraction(rp, md, "x.docx", None, out)
    assert out == ([{"doc": "x.docx", "basis": "size_ratio", "source_bytes": n, "md_bytes": len(md.encode("utf-8"))}]
                   if expected == "flag" else [])


def test_check_partial_extraction_truncated_sheet_does_not_hide_other_sheet_suspicion(tmp_path):
    """1シートの自己申告打切り（`truncated`）は `size_ratio` 判定だけを省略し、別シートの
    `partial_extraction_suspected` 走査は継続する。"""
    rp = tmp_path / "big.xlsx"
    rp.write_bytes(b"x" * (office_md._PARTIAL_SIZE_MIN_SOURCE_BYTES + 1))
    document = SimpleNamespace(elements=[
        SimpleNamespace(type="sheet", source_map={"sheet": "A", "truncated": True}),
        SimpleNamespace(type="sheet", source_map={"sheet": "B", "partial_extraction_suspected": True,
                                                   "declared_rows": 100, "extracted_rows": 1}),
    ])
    out: list[dict] = []
    office_md._check_partial_extraction(rp, "少しだけ", "big.xlsx", document, out)
    assert out == [{"doc": "big.xlsx", "basis": "xlsx_row_ratio", "declared_rows": 100, "extracted_rows": 1}]


# ---- 変換の失敗・fail-closed ----

def test_build_derived_broken_docx_notice_is_listed_and_does_not_block_ir_sig(tmp_path):
    """document-ir 構築に失敗した docx は `document_ir_failed` へ計上され、失敗の知らせで公開される。
    知らせの文書の失敗は失敗の一覧で管理し、`.document_ir_sig` は刻む（次の更新が全件の作り直しにならない）。"""
    src, der = _dirs(tmp_path)
    (src / "broken.docx").write_bytes(b"not a zip")
    rep = office_md.build_derived(src, der)
    assert rep["failed"] == 1 and rep["document_ir_failed"] >= 1
    assert any(f["doc"] == "broken.docx" and f["reason"].startswith("document_ir_failed:")
               for f in rep["document_ir_failures"])
    _notice_meta(der, "broken.docx", "source_parse_failed")
    assert office_md.document_ir_sig_drift(der) is False


def test_build_derived_docx_without_body_element_publishes_failed_notice(tmp_path):
    """`<w:body>` の無い docx（IR は例外無しで None）は、Evidence 側の再抽出の成否に依存せず IR 失敗理由から
    failed notice を発行する（さもないと `{rel}.md` が書かれず文書が台帳・grep から消える）。"""
    src, der = _dirs(tmp_path)
    _zip(src / "nobody.docx", {"word/document.xml": '<?xml version="1.0"?><w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"></w:document>'})
    rep = office_md.build_derived(src, der)
    assert rep["document_ir_failed"] >= 1
    assert any(f["doc"] == "nobody.docx" and f["reason"] == "document_ir_failed:malformed_structure"
               for f in rep["document_ir_failures"])
    assert rep["published_notice_count"] == 1
    _notice_meta(der, "nobody.docx", "source_parse_failed")
    assert (der.parent / "ir" / "nobody.docx.evidence.json").is_file()
    assert (der.parent / "rag" / "nobody.docx.rag.md").is_file()


# 入口ガード（MEM-1/MEM-2）: 上限超過は変換を試みず failed notice。上限未満の他ファイルは巻き込まれない。
def _guard_size(monkeypatch, src, tmp_path):
    p = _xlsx(src / "big.xlsx", A1="値")
    monkeypatch.setattr(office_md, "_OFFICE_FILE_CAP_BYTES", p.stat().st_size - 1)
    return "big.xlsx", "size_exceeded"


def _guard_cells(monkeypatch, src, tmp_path):
    _xlsx(src / "wide.xlsx", A1="値", J20="値")                      # dimension は A1:J20（200セル）
    monkeypatch.setattr(office_md, "_XLSX_CELL_CAP", 100)
    return "wide.xlsx", "cell_count_exceeded"


def _guard_uncompressed(monkeypatch, src, tmp_path):
    padded = _DOCX_XML.replace("本文テキストABC", "本文テキストABC" + "パディング" * 2000)
    _zip(src / "huge.docx", {"word/document.xml": padded})
    _zip(src / "normal.docx", {"word/document.xml": _DOCX_XML})
    cap = (office_md._office_uncompressed_total_bytes(src / "normal.docx")
           + office_md._office_uncompressed_total_bytes(src / "huge.docx")) // 2
    monkeypatch.setattr(office_md, "_OFFICE_UNCOMPRESSED_CAP_BYTES", cap)
    return "huge.docx", "uncompressed_size_exceeded"


def _guard_cells_no_dimension(monkeypatch, src, tmp_path):
    """`<dimension>` 欠落は「見積不能」であって「安全」ではない——`<c ` の実数をストリーミングで数えて検出する。"""
    cells = "".join(f'<c r="A{i + 1}" t="inlineStr"><is><t>v</t></is></c>' for i in range(50))
    _zip(src / "no_dim_wide.xlsx", {"xl/worksheets/sheet1.xml": _SHEET.format(cells=cells)})
    assert office_md._xlsx_estimated_cell_count(src / "no_dim_wide.xlsx") is None
    monkeypatch.setattr(office_md, "_XLSX_CELL_CAP", 10)
    return "no_dim_wide.xlsx", "cell_count_exceeded"


def _legacy_materialized(monkeypatch, src, tmp_path, name, ext, write):
    """旧形式（原本は小さい）を前段変換した materialized ファイルにも同じガードを適用する。"""
    from sherpa.ingest.arms import legacy_convert
    (src / name).write_bytes(b"legacy-binary-not-a-real-doc")
    mdir = tmp_path / "materialized"
    mdir.mkdir()
    materialized = mdir / f"old{ext}"
    write(materialized)
    monkeypatch.setattr(legacy_convert, "legacy_exts", lambda: {pathlib.Path(name).suffix})
    monkeypatch.setattr(legacy_convert, "ensure_ooxml", lambda s, rel, cache_root: (materialized, []))
    return materialized


def _guard_legacy_uncompressed(monkeypatch, src, tmp_path):
    m = _legacy_materialized(monkeypatch, src, tmp_path, "old.doc", ".docx",
                             lambda p: _zip(p, {"word/document.xml": _DOCX_XML}))
    monkeypatch.setattr(office_md, "_OFFICE_UNCOMPRESSED_CAP_BYTES", office_md._office_uncompressed_total_bytes(m) - 1)
    return "old.doc", "uncompressed_size_exceeded"


def _guard_legacy_cells(monkeypatch, src, tmp_path):
    _legacy_materialized(monkeypatch, src, tmp_path, "old.xls", ".xlsx", lambda p: _xlsx(p, A1="値", J20="値"))
    monkeypatch.setattr(office_md, "_XLSX_CELL_CAP", 100)
    return "old.xls", "cell_count_exceeded"


GUARDS = {"file_size": (_guard_size, True), "xlsx_cell_count": (_guard_cells, True),
          "docx_uncompressed_size": (_guard_uncompressed, True),
          "xlsx_cell_count_without_dimension": (_guard_cells_no_dimension, False),
          "legacy_materialized_uncompressed_size": (_guard_legacy_uncompressed, False),
          "legacy_materialized_cell_count": (_guard_legacy_cells, False)}


@pytest.mark.parametrize("guard,with_normal", GUARDS.values(), ids=GUARDS)
def test_build_derived_input_guard_skips_conversion_with_failed_notice(tmp_path, monkeypatch, guard, with_normal):
    src, der = _dirs(tmp_path)
    if with_normal:
        _zip(src / "normal.docx", {"word/document.xml": _DOCX_XML})          # 上限未満＝通常どおり変換される
    doc, reason = guard(monkeypatch, src, tmp_path)
    rep = office_md.build_derived(src, der)
    assert rep["failed"] == 1
    assert {"doc": doc, "reason": reason} in rep["conversion_failures"]
    _notice_meta(der, doc, reason)
    if with_normal:
        assert rep["converted"] == 1 and (der / "normal.docx.md").is_file()


def test_build_derived_xlsx_within_cell_cap_converts_normally(tmp_path, monkeypatch):
    src, der = _dirs(tmp_path)
    _xlsx(src / "ok.xlsx", A1="値", B2="値2")                       # dimension は A1:B2（4セル）
    monkeypatch.setattr(office_md, "_XLSX_CELL_CAP", 4)             # ちょうど境界＝超過ではない
    rep = office_md.build_derived(src, der)
    assert rep["converted"] == 1 and rep["failed"] == 0 and (der / "ok.xlsx.md").is_file()


def test_xlsx_cell_count_helpers(tmp_path):
    """`<dimension>` の無い原本は見積不能＝None（fail-open）。実数カウントは cap 超過が確定した時点で打ち切る。"""
    p = tmp_path / "no_dim.xlsx"
    _zip(p, {"xl/worksheets/sheet1.xml": _SHEET.format(cells='<row r="1"><c r="A1" t="inlineStr"><is><t>値</t></is></c></row>')})
    assert office_md._xlsx_estimated_cell_count(p) is None
    many = tmp_path / "many.xlsx"
    cells = "".join(f'<c r="A{i + 1}" t="inlineStr"><is><t>v</t></is></c>' for i in range(100))
    _zip(many, {"xl/worksheets/sheet1.xml": _SHEET.format(cells=cells)})
    result = office_md._xlsx_actual_cell_count(many, cap=5)
    assert result is not None and result > 5


def test_build_derived_conv_cache_hit_does_not_bypass_size_guard(tmp_path, monkeypatch):
    """入口ガードは CONV-CACHE の照合より前に評価する。署名不変のまま上限だけ引き下げられても、キャッシュ復元で迂回しない。"""
    src, der = _dirs(tmp_path)
    _xlsx(src / "wide.xlsx", A1="値", J20="値")
    assert office_md.build_derived(src, der)["converted"] == 1
    assert (office_md._conv_cache_root_for(der) / "wide.xlsx.key.json").is_file()
    monkeypatch.setattr(office_md, "_XLSX_CELL_CAP", 100)
    rep2 = office_md.build_derived(src, der)
    assert rep2["converted"] == 0 and rep2["failed"] == 1
    assert {"doc": "wide.xlsx", "reason": "cell_count_exceeded"} in rep2["conversion_failures"]


def test_conv_cache_skips_store_when_evidence_write_fails_then_recovers(tmp_path, monkeypatch):
    """Evidence 一時書込の失敗（`evidence_ir_failed`）を含む回はキャッシュへ保存しない。書込要因が解消した次回は
    フル実変換が再試行され回復する（保存されていたら失敗 delta が焼き付く）。"""
    from sherpa.ingest import evidence_ir
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="x")
    orig_write = evidence_ir.write_json_atomic
    should_fail = {"v": True}

    def flaky_write(path, data):
        if should_fail["v"]:
            raise OSError("simulated evidence.json write failure")
        return orig_write(path, data)
    monkeypatch.setattr(evidence_ir, "write_json_atomic", flaky_write)

    rep1 = office_md.build_derived(src, der)
    assert rep1["evidence_ir_failed"] == 1 and rep1["converted"] == 1      # md 自体は Evidence 書込失敗と独立に成功
    key_file = office_md._conv_cache_root_for(der) / "a.xlsx.key.json"
    assert not key_file.exists()
    should_fail["v"] = False
    rep2 = office_md.build_derived(src, der)
    assert rep2["evidence_ir_failed"] == 0 and rep2["converted"] == 1
    assert key_file.is_file()


def test_conv_cache_lookup_rejects_entry_with_failed_delta(tmp_path):
    """rep_delta に失敗カウンタが残る古いキャッシュ実体は復元しない（防御的二重チェック）。"""
    cache_root = tmp_path / "_conv_cache"
    (cache_root / "a.xlsx.d" / "md").mkdir(parents=True)
    (cache_root / "a.xlsx.d" / "md" / "a.xlsx.md").write_text("dummy", encoding="utf-8")
    json_io.write_json_atomic(cache_root / "a.xlsx.key.json", {
        "key": "k1",
        "rep_delta": {"document_ir_generated": 1, "document_ir_failed": 0, "evidence_ir_generated": 0,
                      "evidence_ir_failed": 1, "rag_generated": 0, "rag_failed": 0}})
    assert office_md._conv_cache_lookup(cache_root, "a.xlsx", "k1") is None


# ---- drift マーカー ----

def test_xlsx_extractor_version_bump_triggers_document_ir_and_human_md_drift(tmp_path, monkeypatch):
    """抽出器版の更新は document-ir（→evidence/rag）と human_md の両方の drift を発火する（split-brain 防止）。"""
    from sherpa.ingest.arms import ooxml_arm
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="値")
    assert office_md.build_derived(src, der)["converted"] == 1
    assert office_md.document_ir_sig_drift(der) is False and office_md.human_md_sig_drift(src, der) is False
    monkeypatch.setattr(ooxml_arm, "XLSX_EXTRACTOR_VERSION", "xlsx-ooxml-vX-test-bump")
    assert office_md.document_ir_sig_drift(der) is True and office_md.human_md_sig_drift(src, der) is True


def test_arms_sig_drift_marker(tmp_path, pdf_stub):
    """PDF バックエンドの導入/除去を `.arms_sig` で検知し、署名同一でも作り直す（document-ir 版は含まない）。"""
    src, der = _dirs(tmp_path)
    _blank_pdf(src / "doc.pdf")
    pdf_stub(None)
    office_md.build_derived(src, der)
    marker = der / office_md._ARMS_SIG_MARKER
    assert marker.read_text(encoding="utf-8") == "arms=ooxml,pdf_text;pdf=none;legacy=none;vlm=none"
    assert office_md.arms_sig_drift(der) is False
    pdf_stub("pypdf", ["税率10%の説明"])
    assert office_md.arms_sig_drift(der) is True
    office_md.build_derived(src, der)
    assert marker.read_text(encoding="utf-8") == "arms=ooxml,pdf_text;pdf=pypdf;legacy=none;vlm=none"
    assert office_md.arms_sig_drift(der) is False
    assert "税率10%" in (der / "doc.pdf.md").read_text(encoding="utf-8")


def test_arms_sig_drift_on_old_format_marker(tmp_path, pdf_stub):
    """tesseract 撤去前の旧フォーマット（`;ocr=` 成分入り）の marker は drift=True で1回だけ全再ビルドされる。"""
    der = tmp_path / "derived"
    der.mkdir()
    pdf_stub(None)
    (der / office_md._ARMS_SIG_MARKER).write_text(
        "arms=ooxml,pdf_text;pdf=none;legacy=none;md=none;ocr=none;vlm=none", encoding="utf-8")
    assert office_md.arms_sig_drift(der) is True
    office_md._write_arms_sig_marker(der)
    assert office_md.arms_sig_drift(der) is False


# ---- sidecar 欠落検知（`rag_sidecars_missing`）: 生成時マニフェスト（`{rel}.derived.json`）の記録を照合する ----

def test_empty_ooxml_has_no_md_but_manifest_and_refresh_regenerate_instead_of_delete(tmp_path):
    """本文が空の docx は legacy `.md` を持たないが Evidence/RAG は生成される。マニフェストは `.md` を含まず欠落扱いに
    ならず、`refresh_evidence_ir()` も削除ではなく再生成する（`seen` の基準が `.md` の有無に依存しない）。"""
    src, der = _dirs(tmp_path)
    _zip(src / "empty.docx", {"word/document.xml": _EMPTY_DOCX_XML})
    rep = office_md.build_derived(src, der)
    assert rep["evidence_ir_failed"] == 0 and rep["rag_failed"] == 0
    assert not (der / "empty.docx.md").is_file()
    assert (der.parent / "ir" / "empty.docx.evidence.json").is_file()
    manifest = json_io.read_json(der.parent / "ir" / "empty.docx.derived.json", default=None)
    assert manifest is not None and ".md" not in manifest["sidecars"]
    assert office_md.rag_sidecars_missing(src, der) is False
    for _ in range(2):                                                    # 2回目も安定して再生成
        rep2 = office_md.refresh_evidence_ir(src, der)
        assert rep2["evidence_ir_failed"] == 0 and rep2["rag_failed"] == 0
        assert rep2["evidence_ir_generated"] == 1 and rep2["rag_generated"] == 1
        for p in ("ir/empty.docx.evidence.json", "rag/empty.docx.rag.md", "rag/empty.docx.rag_chunks.jsonl"):
            assert (der.parent / p).is_file()


def test_rag_sidecars_missing_disabled_arm_extension_is_not_falsely_flagged(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_ARMS", "pdf_text")                     # ooxml を含まない構成
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="内容")
    rep = office_md.build_derived(src, der)
    assert rep["converted"] == 0 and rep["unsupported"] == 1
    manifest = json_io.read_json(der.parent / "ir" / "a.xlsx.derived.json", default=None)
    assert manifest == {"schema": office_md._DERIVED_MANIFEST_SCHEMA_VERSION, "sidecars": []}
    assert office_md.rag_sidecars_missing(src, der) is False


FIVE_SIDECARS = {".md", ".md.meta.json", ".evidence.json", ".rag.md", ".rag_chunks.jsonl"}


def test_rag_sidecars_missing_legacy_backend_off_notice_tracked(tmp_path, monkeypatch):
    """legacy backend 不在でも `.doc` は `legacy_backend_unavailable` の通知として5点が揃って書かれ、1つでも外部要因で消えれば欠落検知する。"""
    from sherpa.ingest.arms import legacy_convert
    monkeypatch.setattr(legacy_convert, "legacy_exts", lambda: set())
    src, der = _dirs(tmp_path)
    (src / "old.doc").write_bytes(b"legacy binary stub")
    assert office_md.build_derived(src, der)["published_notice_count"] == 1
    assert (der / "old.doc.md").is_file()
    manifest = json_io.read_json(der.parent / "ir" / "old.doc.derived.json", default=None)
    assert manifest is not None and set(manifest["sidecars"]) == FIVE_SIDECARS
    assert office_md.rag_sidecars_missing(src, der) is False
    (der.parent / "rag" / "old.doc.rag_chunks.jsonl").unlink()
    assert office_md.rag_sidecars_missing(src, der) is True


def _png(path, color):
    from PIL import Image
    Image.new("RGB", (4, 4), color=color).save(path)


def test_rag_sidecars_missing_raster_image_tracked(tmp_path):
    """単体 PNG/JPEG は Evidence/RAG が生成され（raster 経路）、5点が記録され `.evidence.json` の外部削除を検知できる。"""
    src, der = _dirs(tmp_path)
    _png(src / "scan.png", "red")
    rep = office_md.build_derived(src, der)
    assert rep["converted"] == 1 and rep["failed"] == 0
    manifest = json_io.read_json(der.parent / "ir" / "scan.png.derived.json", default=None)
    assert manifest is not None and set(manifest["sidecars"]) == FIVE_SIDECARS
    assert office_md.rag_sidecars_missing(src, der) is False
    (der.parent / "ir" / "scan.png.evidence.json").unlink()
    assert office_md.rag_sidecars_missing(src, der) is True


def test_rag_sidecars_missing_normal_pdf_md_meta_deletion_detected(tmp_path, pdf_stub):
    """テキスト層のある通常 PDF の `.md`/`.md.meta.json` だけが消えても（`.evidence.json` は残っていても）検知する。"""
    src, der = _dirs(tmp_path)
    _blank_pdf(src / "note.pdf")
    pdf_stub("pypdf", ["本文テキスト"])
    assert office_md.build_derived(src, der)["converted"] == 1
    assert (der / "note.pdf.md").is_file()
    assert office_md.rag_sidecars_missing(src, der) is False
    (der / "note.pdf.md").unlink()
    (der / "note.pdf.md.meta.json").unlink()
    assert office_md.rag_sidecars_missing(src, der) is True


def test_rag_sidecars_missing_asset_directory_deletion_detected(tmp_path):
    """`{rel}.assets/` の個々のファイル削除・ディレクトリ全体の削除のどちらも検知する。"""
    src, der = _dirs(tmp_path)
    _png(src / "scan.png", "blue")
    office_md.build_derived(src, der)
    manifest = json_io.read_json(der.parent / "ir" / "scan.png.derived.json", default=None)
    assert manifest is not None and manifest.get("assets")
    assert office_md.rag_sidecars_missing(src, der) is False
    asset_files = list((der.parent / "rag" / "scan.png.assets").iterdir())
    assert len(asset_files) == 1
    asset_files[0].unlink()
    assert office_md.rag_sidecars_missing(src, der) is True
    office_md.build_derived(src, der)                                       # 健全な状態へ作り直す
    assert office_md.rag_sidecars_missing(src, der) is False
    shutil.rmtree(der.parent / "rag" / "scan.png.assets")
    assert office_md.rag_sidecars_missing(src, der) is True


def test_write_derived_sidecar_manifest_returns_false_on_asset_iterdir_failure(tmp_path, monkeypatch):
    """`{rel}.assets/` の `iterdir()` 失敗は例外を伝播させず False を返す（iterdir は try 節の中）。"""
    src, der = _dirs(tmp_path)
    _png(src / "scan.png", "green")
    office_md.build_derived(src, der)
    original_iterdir = pathlib.Path.iterdir

    def _boom_iterdir(self):
        if self.name == "scan.png.assets":
            raise OSError("simulated iterdir failure")
        return original_iterdir(self)
    monkeypatch.setattr(pathlib.Path, "iterdir", _boom_iterdir)
    assert office_md._write_derived_sidecar_manifest(der, der.parent / "rag", der.parent / "ir", "scan.png") is False


# ---- human_md の選択的再生成 ----

def test_human_md_sig_drift_and_refresh_touch_only_the_md_asset(tmp_path, monkeypatch):
    """`asset_versions.human_md` の食い違いは `refresh_human_md` が `{rel}.md` だけを選択的に再生成する
    （evidence/rag/document_ir とそれらの sig は無変更・マニフェスト schema は v1 のまま全再構築は誘発しない）。"""
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="値")
    assert office_md.build_derived(src, der)["converted"] == 1
    manifest = json_io.read_json(der.parent / "ir" / "a.xlsx.derived.json", default=None)
    assert manifest["schema"] == office_md._DERIVED_MANIFEST_SCHEMA_VERSION == "derived-sidecar-manifest-v1"
    assert manifest["asset_versions"]["human_md"] == office_md._current_human_md_sig()
    assert office_md.human_md_sig_drift(src, der) is False

    before_evidence = (der.parent / "ir" / "a.xlsx.evidence.json").read_bytes()
    before_sigs = [(der / m).read_text(encoding="utf-8")
                   for m in (office_md._DOCUMENT_IR_SIG_MARKER, office_md._EVIDENCE_IR_SIG_MARKER)]
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "bumped-human-md-version")
    assert office_md.human_md_sig_drift(src, der) is True
    rep2 = office_md.refresh_human_md(src, der)
    assert rep2["human_md_generated"] == 1 and rep2["human_md_failed"] == 0
    assert office_md.human_md_sig_drift(src, der) is False

    manifest2 = json_io.read_json(der.parent / "ir" / "a.xlsx.derived.json", default=None)
    assert manifest2["asset_versions"]["human_md"] == "bumped-human-md-version"
    assert manifest2["schema"] == "derived-sidecar-manifest-v1"
    assert (der.parent / "ir" / "a.xlsx.evidence.json").read_bytes() == before_evidence
    assert [(der / m).read_text(encoding="utf-8")
            for m in (office_md._DOCUMENT_IR_SIG_MARKER, office_md._EVIDENCE_IR_SIG_MARKER)] == before_sigs
    assert office_md.document_ir_sig_drift(der) is False and office_md.evidence_ir_sig_drift(der) is False
    assert office_md.rag_sig_drift(der) is False


def test_refresh_human_md_generates_md_for_previously_md_less_rel(tmp_path):
    """空 xlsx（旧世代＝`.md`/`asset_versions` を持たないマニフェスト）も `.md` の有無で絞らないため移行対象から漏れない。"""
    src, der = _dirs(tmp_path)
    wb = openpyxl.Workbook()
    wb.save(src / "empty.xlsx")                                           # 値を書かない＝空シート
    der.mkdir()
    (der.parent / "ir").mkdir()
    json_io.write_json_atomic(der.parent / "ir" / "empty.xlsx.derived.json",
                              {"schema": office_md._DERIVED_MANIFEST_SCHEMA_VERSION, "sidecars": []})
    assert not (der / "empty.xlsx.md").is_file()
    assert office_md.human_md_sig_drift(src, der) is True
    rep = office_md.refresh_human_md(src, der)
    assert rep["human_md_generated"] == 1 and rep["human_md_failed"] == 0
    assert "値のあるセルが見つかりませんでした" in (der / "empty.xlsx.md").read_text(encoding="utf-8")
    assert office_md.human_md_sig_drift(src, der) is False


def test_human_md_sig_drift_and_refresh_are_noop_when_ooxml_arm_disabled(tmp_path, monkeypatch):
    """`ooxml` アーム無効の間は docx/xlsx を評価対象にせず、迂回して人間向け MD を新規生成しない。"""
    from sherpa.ingest import arms as _arms
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="x")
    der.mkdir()
    monkeypatch.setattr(_arms, "enabled_arm_names", lambda: ["pdf_text"])
    assert office_md.human_md_sig_drift(src, der) is False
    assert office_md.refresh_human_md(src, der) == {"human_md_generated": 0, "human_md_failed": 0, "human_md_failures": []}
    assert not (der / "a.xlsx.md").is_file() and not (der.parent / "ir" / "a.xlsx.derived.json").exists()
    monkeypatch.setattr(_arms, "enabled_arm_names", lambda: ["ooxml", "pdf_text"])
    assert office_md.human_md_sig_drift(src, der) is True
    rep = office_md.refresh_human_md(src, der)
    assert rep["human_md_generated"] == 1 and rep["human_md_failed"] == 0


def test_human_md_partial_failure_keeps_es_meta_pending_until_fixed(monkeypatch, tmp_path):
    """1 rel でも human_md 再生成に失敗している間は ES meta の human_md 版を確定させず pending センチネルを返し続け
    （fail-closed）、直り、かつ ES の bulk 成功を確認できた（`confirm_human_md_es_sig`）次回だけ現行版へ進む。"""
    from sherpa import es_index
    from sherpa import worlds as worlds_mod
    from sherpa.ingest.arms import ooxml_arm
    wd = tmp_path / "world"
    wd.mkdir()
    dmd = tmp_path / "derived"
    _xlsx(wd / "a.xlsx", A1="x")
    assert office_md.build_derived(wd, dmd)["evidence_ir_failed"] == 0
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: dmd)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vNEW")

    should_fail = {"v": True}
    real_build_xlsx_ir = ooxml_arm._build_xlsx_ir

    def _flaky_build(p):
        if should_fail["v"]:
            raise RuntimeError("simulated ir build failure")
        return real_build_xlsx_ir(p)
    monkeypatch.setattr(ooxml_arm, "_build_xlsx_ir", _flaky_build)

    assert office_md.refresh_human_md(wd, dmd)["human_md_failed"] == 1
    assert office_md.human_md_sig_drift(wd, dmd) is True
    assert es_index._human_md_config_sig("w") == es_index._HUMAN_MD_PENDING_SENTINEL
    should_fail["v"] = False
    assert office_md.refresh_human_md(wd, dmd)["human_md_failed"] == 0
    assert office_md.human_md_sig_drift(wd, dmd) is False
    assert es_index._human_md_config_sig("w") == es_index._HUMAN_MD_PENDING_SENTINEL   # ES bulk 成功が未確認の間は pending
    assert office_md.confirm_human_md_es_sig(wd, dmd) is True
    assert es_index._human_md_config_sig("w") == "human-md-vNEW"


# ---- 公開（staging→target）の fail-closed ----

def test_build_derived_blocks_publish_on_unhandled_exception(tmp_path, monkeypatch):
    """想定外の例外で終わった rel が1件でもあると `unhandled_failed` へ計上され公開しない（derived 自体が作られない）。"""
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="x")

    def _boom(*args, **kwargs):
        raise RuntimeError("simulated unhandled crash")
    monkeypatch.setattr(office_md, "_convert_with_arms", _boom)
    rep = office_md.build_derived(src, der)
    assert rep["unhandled_failed"] == 1 and rep["error"] == "derived_incomplete:unhandled_failed=1"
    assert not der.is_dir()
    assert rep["unhandled_failures"] == [{"doc": "a.xlsx", "reason": "unhandled_exception:RuntimeError"}]


def test_build_derived_blocks_publish_on_manifest_write_failure(tmp_path, monkeypatch):
    """`{rel}.derived.json` の書込失敗も `unhandled_failed` へ計上され公開しない（書けなかったマニフェストを対象外と取り違えない）。"""
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="x")
    original = json_io.write_text_atomic

    def _boom(path, *args, **kwargs):
        if str(path).endswith(".derived.json"):
            raise OSError("simulated manifest write failure")
        return original(path, *args, **kwargs)
    monkeypatch.setattr(office_md.json_io, "write_text_atomic", _boom)
    rep = office_md.build_derived(src, der)
    assert rep["unhandled_failed"] == 1 and rep["error"] == "derived_incomplete:unhandled_failed=1"
    assert not der.is_dir()
    assert rep["unhandled_failures"] == [{"doc": "a.xlsx", "reason": "manifest_write_failed"}]


def test_publish_failure_after_retire_rolls_back_old_derived_content(tmp_path, monkeypatch):
    """後半 rename（staging→target）が失敗しても、retired→target へ即時ロールバックして旧内容のまま残す。"""
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="old")
    assert not office_md.build_derived(src, der).get("error")
    old_md = (der / "a.xlsx.md").read_text(encoding="utf-8")
    assert "old" in old_md
    _xlsx(src / "a.xlsx", A1="new")
    original_rename = pathlib.Path.rename

    def _boom_rename(self, target):
        if self.name.endswith(office_md._STAGING_SUFFIX):
            raise OSError("simulated staging->target rename failure")
        return original_rename(self, target)
    monkeypatch.setattr(pathlib.Path, "rename", _boom_rename)

    rep2 = office_md.build_derived(src, der)
    assert rep2["error"].startswith("derived_publish_failed:")
    assert der.is_dir() and (der / "a.xlsx.md").read_text(encoding="utf-8") == old_md
    assert not der.with_name(der.name + office_md._STAGING_SUFFIX).exists()
    assert not der.with_name(der.name + office_md._RETIRED_SUFFIX).exists()


def test_double_rename_failure_preserves_retired_generation(tmp_path, monkeypatch):
    """ロールバックも次回の `_recover_interrupted_swap` の復旧も失敗する二重障害でも retired を消さない（消すと派生物が
    全消失する）。障害が解消すれば次の build で retired から復旧できる。"""
    src, der = _dirs(tmp_path)
    _xlsx(src / "a.xlsx", A1="old")
    assert not office_md.build_derived(src, der).get("error")
    old_md = (der / "a.xlsx.md").read_text(encoding="utf-8")
    _xlsx(src / "a.xlsx", A1="new")
    original_rename = pathlib.Path.rename
    retired = der.with_name(der.name + office_md._RETIRED_SUFFIX)

    def _boom_rename(self, target):
        if self.name.endswith(office_md._STAGING_SUFFIX) or self.name.endswith(office_md._RETIRED_SUFFIX):
            raise OSError("simulated rename failure")
        return original_rename(self, target)
    monkeypatch.setattr(pathlib.Path, "rename", _boom_rename)

    assert office_md.build_derived(src, der)["error"].startswith("derived_publish_failed:")
    assert retired.is_dir() and (retired / "a.xlsx.md").read_text(encoding="utf-8") == old_md
    # 次回も同じ障害: 復旧 rename も失敗＝setup error で打ち切り、retired を無条件削除する `_publish_staging` へ進まない。
    assert office_md.build_derived(src, der)["error"].startswith("derived_setup_failed:")
    assert retired.is_dir() and (retired / "a.xlsx.md").read_text(encoding="utf-8") == old_md
    assert not der.is_dir()
    monkeypatch.setattr(pathlib.Path, "rename", original_rename)             # 障害解消
    assert not office_md.build_derived(src, der).get("error")
    assert "new" in (der / "a.xlsx.md").read_text(encoding="utf-8")
    assert not retired.exists()
