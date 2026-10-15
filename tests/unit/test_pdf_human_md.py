"""PDF の人向け MD（`{rel}.md`）を Evidence IR から組む契約: 表は Markdown の表・位置付き文字の無いページは従来のページ本文。

設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import shutil
from pathlib import Path

from sherpa.ingest import office_md

_ROOT = Path(__file__).resolve().parents[2]
_MERGED = _ROOT / "fixtures" / "pdf_export" / "PDFX-MERGED.pdf"


def _build_md(src: Path, tmp_path: Path, name: str) -> str:
    out = tmp_path / "out"
    rep = office_md.build_derived(src, out / "md")
    assert rep["failed"] == 0 and rep["converted"] >= 1
    return (out / "md" / (name + ".md")).read_text(encoding="utf-8")


def test_merged_cell_pdf_gets_markdown_table(tmp_path):
    """縦結合の表を持つ PDF は、見出し行つきの Markdown の表になり、結合の残りは空欄・表の前後の文字は上から下の順で並ぶ。"""
    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(_MERGED, src / "a.pdf")
    md = _build_md(src, tmp_path, "a.pdf")
    lines = md.split("\n")
    assert lines[0] == "## ページ 1"
    header = lines.index("| 区分 | 項目 ID | 表示名 | 桁数 |")
    assert lines[header + 1] == "| --- | --- | --- | --- |"
    assert lines[header + 2] == "| 基本情報 | ORDER_NO | 受注番号 | 10 |"
    assert lines[header + 3] == "|  | ORDER_DATE | 受注日 | 8 |"
    assert lines[header + 4] == "| 明細 | ITEM_CD | 品目コード | 6 |"
    assert lines[header + 5] == "|  | QTY | 数量 | 5 |"
    assert lines.index("受注連携項目定義（架空）") < header < lines.index("備考: 数量は 0 を許可しない。")


def _pdf_with_covered_page(path: Path) -> Path:
    """1 ページ目は普通の本文、2 ページ目は本文の上にページ全面の画像が重なる PDF（2 ページ目の本文は位置付き文字にならず、ページ全文だけが残る）。"""
    from pypdf import PdfWriter
    from pypdf.generic import (
        ArrayObject, DecodedStreamObject, DictionaryObject, NameObject, NumberObject,
    )

    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"), NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica")}))
    image = DecodedStreamObject()
    image.set_data(b"\x80")
    image.update({
        NameObject("/Type"): NameObject("/XObject"), NameObject("/Subtype"): NameObject("/Image"),
        NameObject("/Width"): NumberObject(1), NameObject("/Height"): NumberObject(1),
        NameObject("/ColorSpace"): NameObject("/DeviceGray"), NameObject("/BitsPerComponent"): NumberObject(8)})
    image_ref = writer._add_object(image)
    bodies = (b"BT /F1 12 Tf 20 200 Td (Visible line one) Tj ET\n",
              b"BT /F1 12 Tf 20 200 Td (Hidden line one) Tj ET\nq 300 0 0 300 0 0 cm /Im1 Do Q\n")
    for body in bodies:
        page = writer.add_blank_page(width=300, height=300)
        page[NameObject("/Resources")] = DictionaryObject({
            NameObject("/Font"): DictionaryObject({NameObject("/F1"): font}),
            NameObject("/XObject"): DictionaryObject({NameObject("/Im1"): image_ref}),
            NameObject("/ProcSet"): ArrayObject([NameObject("/PDF")])})
        content = DecodedStreamObject()
        content.set_data(body)
        page[NameObject("/Contents")] = writer._add_object(content)
    with path.open("wb") as f:
        writer.write(f)
    return path


def test_page_without_positioned_text_keeps_page_body(tmp_path):
    """位置付き文字の無いページ（本文が全面画像で覆われている）は、従来どおり pdfium のページ本文（整形済み）をそのまま出す。"""
    src = tmp_path / "src"
    src.mkdir()
    pdf = _pdf_with_covered_page(src / "b.pdf")
    before = office_md._pdf_md(pdf)
    assert before == "## ページ 1\n\nVisible line one\n\n## ページ 2\n\nHidden line one"
    assert _build_md(src, tmp_path, "b.pdf") == before


def test_existing_pdf_md_is_rebuilt_once_after_version_change(tmp_path):
    """人向け MD を作った版が記録されていない（旧形式の）PDF は全再構築の対象になり、作り直した後は収まる。"""
    import json

    src = tmp_path / "src"
    src.mkdir()
    shutil.copy(_MERGED, src / "a.pdf")
    out = tmp_path / "out"
    office_md.build_derived(src, out / "md")
    assert office_md.rag_sidecars_missing(src, out / "md") is False
    manifest_path = out / "ir" / "a.pdf.derived.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("asset_versions")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    assert office_md.rag_sidecars_missing(src, out / "md") is True
