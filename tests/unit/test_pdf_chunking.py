"""PDF のチャンク単位の実害の再現テスト。

PDF の 1 行が 1 チャンクだったため、値と項目・単位が別チャンクに分かれ、短い断片が検索の上位を占めていた。
同じページの読み順で連続する行を 300〜400 字にまとめ、見出し・ページ・出典を保つことを確かめる。
設計: docs/design/rag.md「チャンク化の考え方」
"""
from __future__ import annotations

import re
from pathlib import Path

from sherpa.ingest import evidence_ir, evidence_render, evidence_spike, office_md, pdf_chunking

_LINE = 14.0


def _pdf(path: Path, pages: list[list[tuple[float, float, float, str]]]) -> Path:
    """pages[i] = [(x, y, 文字サイズ, 文字列)]。ページごとに明示的に改ページする最小の PDF。"""
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
    with path.open("wb") as f:
        writer.write(f)
    return path


def _lines(count: int, *, start_y: float = 500.0, prefix: str = "line") -> list[tuple[float, float, float, str]]:
    return [(40, start_y - i * _LINE, 10, f"{prefix}{i:02d} " + "alpha beta gamma delta epsilon zeta eta")
            for i in range(count)]


def _render(path: Path):
    ir = evidence_spike.extract(path)
    assert evidence_ir.validation_errors(ir) == []
    return ir, evidence_render.render(ir, source_name=path.name)


def _text_chunks(rendered) -> list[dict]:
    return [chunk for chunk in rendered.chunks if chunk["content_type"] == "positioned_text"]


def _squash(text: str) -> str:
    return re.sub(r"\s", "", text)


def test_wrapped_lines_become_one_chunk_with_a_citation_per_line(tmp_path):
    """4 行の段落は、行ごとの 4 チャンクではなく 1 チャンク。出典は行ごとの位置を読み順で残す。"""
    ir, rendered = _render(_pdf(tmp_path / "a.pdf", [_lines(4)]))
    chunks = _text_chunks(rendered)
    assert len(chunks) == 1
    assert chunks[0]["body"].splitlines() == [element.value for element in sorted(
        (e for e in ir.elements if e.type == "positioned_text"), key=lambda e: e.order)]
    lines = [c for c in chunks[0]["citations"] if str(c["locator"].get("object_id", "")).startswith("text:")]
    boxes = [citation["locator"]["bbox"] for citation in lines]
    assert len(boxes) == 4
    assert [box[3] for box in boxes] == sorted((box[3] for box in boxes), reverse=True)
    assert {citation["locator"]["page"] for citation in chunks[0]["citations"]} == {1}


def test_value_and_unit_on_adjacent_lines_share_a_chunk(tmp_path):
    items = [
        (40, 500, 10, "The replacement cycle of the packing is"),
        (40, 486, 10, "14512"),
        (40, 472, 10, "hours."),
    ]
    _, rendered = _render(_pdf(tmp_path / "v.pdf", [items]))
    bodies = [chunk["body"] for chunk in _text_chunks(rendered)]
    assert any("cycle" in body and "14512" in body and "hours" in body for body in bodies)


def test_chunks_never_cross_a_physical_page(tmp_path):
    _, rendered = _render(_pdf(tmp_path / "p.pdf", [_lines(3, prefix="first"), _lines(3, prefix="second")]))
    chunks = _text_chunks(rendered)
    assert len(chunks) == 2
    for chunk in chunks:
        assert len({citation["locator"]["page"] for citation in chunk["citations"]}) == 1
    assert [chunk["document_context"]["page"] for chunk in chunks] == [1, 2]


def test_long_text_is_cut_under_the_cap_without_losing_or_rewriting_characters(tmp_path):
    items = _lines(24)
    ir, rendered = _render(_pdf(tmp_path / "l.pdf", [items]))
    chunks = _text_chunks(rendered)
    assert len(chunks) >= 3
    assert all(len(chunk["body"]) <= pdf_chunking.PDF_CHUNK_MAX_CHARS for chunk in chunks)
    original = "".join(_squash(e.value) for e in ir.elements if e.type == "positioned_text")
    assert sorted(_squash("".join(chunk["body"] for chunk in chunks))) == sorted(original)
    ids = {citation["evidence_id"] for chunk in chunks for citation in chunk["citations"]}
    assert {e.element_id for e in ir.elements if e.type == "positioned_text"} <= ids


def test_heading_starts_a_chunk_and_later_chunks_of_the_section_name_it(tmp_path):
    items = [(40, 540, 16, "1 Scope")] + _lines(24, start_y=510)
    _, rendered = _render(_pdf(tmp_path / "h.pdf", [items]))
    chunks = _text_chunks(rendered)
    assert chunks[0]["body"].startswith("1 Scope\n")
    assert len(chunks) >= 3
    markdown = rendered.markdown
    for chunk in chunks[1:]:
        block = markdown.split(f"<!-- chunk:{chunk['chunk_id']} -->")[1].split("<!-- chunk:")[0]
        assert "見出し: 「1 Scope」" in block
        assert "1 Scope" not in chunk["body"]
    assert "見出し:" not in markdown.split(f"<!-- chunk:{chunks[0]['chunk_id']} -->")[1].split("<!-- chunk:")[0]


def test_new_heading_cuts_the_chunk_even_when_short(tmp_path):
    items = ([(40, 540, 16, "1 First")] + _lines(2, start_y=510, prefix="a")
             + [(40, 440, 16, "2 Second")] + _lines(2, start_y=410, prefix="b"))
    _, rendered = _render(_pdf(tmp_path / "s.pdf", [items]))
    bodies = [chunk["body"] for chunk in _text_chunks(rendered)]
    assert len(bodies) == 2
    assert bodies[0].startswith("1 First") and "2 Second" not in bodies[0]
    assert bodies[1].startswith("2 Second")


def test_header_and_footer_of_a_page_share_one_chunk_apart_from_the_body(tmp_path):
    items = [(40, 585, 8, "Running title")] + _lines(3) + [(200, 12, 8, "7")]
    _, rendered = _render(_pdf(tmp_path / "m.pdf", [items]))
    bodies = sorted(chunk["body"] for chunk in _text_chunks(rendered))
    assert len(bodies) == 2
    assert "Running title\n7" in bodies
    assert not any("Running title" in body and "line00" in body for body in bodies)


def _element(order: int, text: str, *, page: int = 1, object_id: str = "", top: float = 100.0,
             row_lines: int | None = None) -> evidence_ir.EvidenceElement:
    extension: dict = {"font_size": 10.0}
    if row_lines:
        extension["row_lines"] = row_lines
    return evidence_ir.EvidenceElement(
        element_id=f"e{order}", type="positioned_text", parent_id=None, order=order, value=text,
        locator=evidence_ir.Locator(
            part=f"page:{page}/contents", page=page, object_id=object_id or f"text:{order}",
            bbox=[40, top - 10, 300, top]),
        coverage_id="c", extension=extension)


def test_row_lines_blocks_keep_their_rows_and_only_same_table_rows_are_packed():
    first = _element(3000, "r1a\nr1b", object_id="text-rows:1:1", top=300)
    second = _element(3001, "r2a\nr2b", object_id="text-rows:1:2", top=280)
    other = _element(3002, "x\ny", object_id="text-rows:2:1", top=200)
    for item in (first, second, other):
        item.extension["row_lines"] = 2
    groups = pdf_chunking.plan([first, second, other], {1: [0, 0, 420, 600]}, max_span_chars=1200)
    assert [[e.element_id for e in group.elements] for group in groups] == [["e3000", "e3001"], ["e3002"]]
    assert groups[0].texts == ["r1a\nr1b", "r2a\nr2b"]


def test_row_lines_block_longer_than_the_span_cap_is_left_to_the_existing_split():
    big = _element(3000, "row\n" * 400, object_id="text-rows:1:1")
    big.extension["row_lines"] = 400
    assert pdf_chunking.plan([big], {1: [0, 0, 420, 600]}, max_span_chars=1200) == []


def test_chunker_version_change_makes_derived_files_stale(tmp_path):
    current = office_md._current_rag_sig()
    assert f"chunker={evidence_render.RAG_CHUNKER_VERSION}" in current
    stale = current.replace(f"chunker={evidence_render.RAG_CHUNKER_VERSION}", "chunker=evidence-rag-chunker-v1alpha11")
    (tmp_path / office_md._RAG_SIG_MARKER).write_text(stale, encoding="utf-8")
    assert office_md.rag_sig_drift(tmp_path) is True
    (tmp_path / office_md._RAG_SIG_MARKER).write_text(current, encoding="utf-8")
    assert office_md.rag_sig_drift(tmp_path) is False
