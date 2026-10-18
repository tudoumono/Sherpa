"""PDF のチャンクに文書の題名・文書番号を添える実害の再現テスト。

文書番号と題名が 1 ページ目にしか無く、値が別のページにある PDF では、値のチャンクから文書を引けなかった。
原本から確かめられる題名・文書番号だけを、本文の外に添えることを確かめる（確かめられないものは添えない）。
設計: docs/design/rag.md「チャンク化の考え方」
"""
from __future__ import annotations

from pathlib import Path

from sherpa.ingest import evidence_ir, evidence_render, evidence_spike

_BODY = "alpha beta gamma delta epsilon zeta eta theta iota kappa lambda"


def _pdf(path: Path, pages: list[list[tuple[float, float, float, str]]], title: str | None = None) -> Path:
    """pages[i] = [(x, y, 文字サイズ, 文字列)]。title は文書情報の Title。"""
    from pypdf import PdfWriter
    from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject

    writer = PdfWriter()
    font = writer._add_object(DictionaryObject({
        NameObject("/Type"): NameObject("/Font"),
        NameObject("/Subtype"): NameObject("/Type1"),
        NameObject("/BaseFont"): NameObject("/Helvetica"),
    }))
    for items in pages:
        page = writer.add_blank_page(width=420, height=600)
        page[NameObject("/Resources")] = DictionaryObject(
            {NameObject("/Font"): DictionaryObject({NameObject("/F1"): font})})
        stream = DecodedStreamObject()
        stream.set_data("".join(
            f"BT /F1 {size} Tf {x} {y} Td ({text}) Tj ET\n" for x, y, size, text in items).encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    if title is not None:
        writer.add_metadata({"/Title": title})
    with path.open("wb") as f:
        writer.write(f)
    return path


def _body(start_y: float, count: int = 3) -> list[tuple[float, float, float, str]]:
    return [(40, start_y - i * 14, 10, _BODY) for i in range(count)]


def _render(path: Path):
    ir = evidence_spike.extract(path)
    assert evidence_ir.validation_errors(ir) == []
    rendered = evidence_render.render(ir, source_name=path.name)
    sections = {}
    parts = rendered.markdown.split("<!-- chunk:")
    for part in parts[1:]:
        chunk_id, _, text = part.partition(" -->")
        sections[chunk_id] = text
    return rendered, sections


def _page(chunk: dict) -> int:
    return chunk["citations"][0]["locator"]["page"]


def test_title_and_number_from_page_one_reach_the_value_on_a_later_page(tmp_path):
    first = [(40, 540, 18, "Packing Maintenance Procedure"), (40, 515, 10, "Doc No.: PR-53787"), *_body(480)]
    second = [(40, 540, 10, "The replacement cycle of the packing is 14512 hours."), *_body(520)]
    rendered, sections = _render(_pdf(tmp_path / "a.pdf", [first, second], title="Packing Maintenance Procedure"))
    value = next(c for c in rendered.chunks if "14512" in c["body"])
    assert _page(value) == 2
    text = sections[value["chunk_id"]]
    assert "文書: 「Packing Maintenance Procedure」" in text
    assert "文書番号: 「PR-53787」" in text
    assert "PR-53787" not in value["body"] and "Packing" not in value["body"]  # 本文は原本の文字のまま


def test_nothing_is_added_when_the_title_cannot_be_confirmed_in_the_original(tmp_path):
    """文書情報の Title が 1 ページ目に無く、目立つ見出しも無ければ、題名を補わない。"""
    first = [*_body(540, 6)]
    second = [*_body(540, 3)]
    _, sections = _render(_pdf(tmp_path / "b.pdf", [first, second], title="Microsoft Word - draft.docx"))
    assert not any("文書: " in text for text in sections.values())
    _, sections = _render(_pdf(tmp_path / "c.pdf", [first, second], title="Quarterly Summary"))
    assert not any("文書: " in text for text in sections.values())


def test_a_section_heading_style_line_is_not_taken_as_the_document_title(tmp_path):
    """1 ページ目の最大の行が、ほかのページにも同じ大きさで出る見出しなら題名にしない（章の見出しを全ページへ広げない）。"""
    first = [(40, 540, 14, "Scope of work"), *_body(515)]
    second = [(40, 540, 14, "Inspection items"), *_body(515)]
    _, sections = _render(_pdf(tmp_path / "d.pdf", [first, second]))
    assert not any("文書: " in text for text in sections.values())


def test_conflicting_document_numbers_are_not_added(tmp_path):
    first = [(40, 540, 18, "Supply Agreement"), (40, 515, 10, "Doc No.: CT-10001"),
             (40, 500, 10, "Doc No.: CT-10002"), *_body(470)]
    second = [*_body(540)]
    rendered, sections = _render(_pdf(tmp_path / "e.pdf", [first, second]))
    later = [sections[c["chunk_id"]] for c in rendered.chunks if _page(c) == 2]
    assert later and all("文書: 「Supply Agreement」" in text for text in later)
    assert not any("文書番号: " in text for text in sections.values())
