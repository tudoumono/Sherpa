"""Word の RAG 描画と人用 Markdown の外部レビュー指摘（本文とテキストボックスの統合・ヘッダー画像の注記・画像だけの文書）。

正典: docs/proposals/2026-10-22-Office観測の共通の扱い.md §1.1・§1.5・§1.6。
"""
from __future__ import annotations

import os
import pathlib
import zipfile

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa.ingest import evidence_ir as IR
from sherpa.ingest import evidence_render as R
from sherpa.ingest import evidence_spike, human_md, office_md
from sherpa.ingest.arms import ooxml_arm

_HASH = "sha256:" + "0" * 64
_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:v="urn:schemas-microsoft-com:vml" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture"'
)
_NOT_READ = "の内容は読み取っていません"


def _docx(path: pathlib.Path, body: str, header: str = "") -> pathlib.Path:
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("word/document.xml", f'<?xml version="1.0"?><w:document {_NS}><w:body>{body}</w:body></w:document>')
        if header:
            z.writestr("word/header1.xml", f'<?xml version="1.0"?><w:hdr {_NS}>{header}</w:hdr>')
    return path


def _picture(object_id: int, name: str) -> str:
    return (
        f'<w:r><w:drawing><wp:inline><wp:extent cx="100" cy="100"/><wp:docPr id="{object_id}" name="{name}"/>'
        "<a:graphic><a:graphicData><pic:pic><pic:nvPicPr><pic:cNvPr id=\"0\" name=\"x\"/></pic:nvPicPr>"
        "</pic:pic></a:graphicData></a:graphic></wp:inline></w:drawing></w:r>"
    )


# ---- ②: 本文とテキストボックスは別の原本要素として残す ----

def _el(element_id, element_type, value, *, parent=None, order=0, part="word/document.xml", object_id=None, ids=None):
    extension = {"document_ir_source_map": {"object_ids": ids}} if ids else {}
    return IR.EvidenceElement(
        element_id=element_id, type=element_type, parent_id=parent, order=order, value=value,
        locator=IR.Locator(part=part, object_id=object_id, extension=extension), coverage_id=f"cov:{element_id}")


def _aliases(elements):
    ir = IR.EvidenceIR(
        schema_version=IR.EVIDENCE_IR_SCHEMA_VERSION, parser_profile="test",
        source=IR.EvidenceSource(file_type="docx", content_hash=_HASH), elements=elements)
    return R._element_aliases(ir, {e.element_id: e for e in elements})


def test_paragraph_and_its_textbox_with_the_same_text_are_not_merged():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "共通の文字", order=1),
        _el("tb", "textbox", "共通の文字", parent="p1", order=2),
    ])
    assert suppressed == set() and aliases == {}


def test_two_textboxes_with_the_same_text_are_not_merged():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb1", "textbox", "共通の文字", parent="p1", order=2),
        _el("tb2", "textbox", "共通の文字", parent="p1", order=3),
    ])
    assert suppressed == set() and aliases == {}


def test_textboxes_with_the_same_text_in_different_parts_are_not_merged():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "共通の文字", parent="p1", order=2, ids=["5"]),
        _el("hdr", "floating_textbox", "共通の文字", order=3, part="word/header1.xml", object_id=5),
    ])
    assert suppressed == set() and aliases == {}


def test_alternate_representations_in_the_same_part_are_still_merged_into_one():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "共通の文字", parent="p1", order=2, ids=["3", "_x0000_s9"]),
        _el("fl", "floating_textbox", "共通の文字", order=3, object_id=3),
        _el("vml", "vml_shape", "共通の文字", order=4, object_id="_x0000_s9"),
    ])
    assert suppressed == {"tb", "fl"} and [a.element_id for a in aliases["vml"]] == ["tb", "fl"]


def test_textboxes_without_a_confirmed_identity_are_kept_apart():
    elements = [
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "共通の条件", parent="p1", order=2),                      # ID を持たない
        _el("fl", "floating_textbox", "共通の条件", order=3, object_id=3),
        _el("vml", "vml_shape", "共通の条件", order=4, object_id="_x0000_s9"),
    ]
    assert _aliases(elements) == ({}, set())
    elements[1] = _el("tb", "textbox", "共通の条件", parent="p1", order=2, ids=["99"])    # ID が一致しない
    assert _aliases(elements) == ({}, set())


def test_a_candidate_with_another_figures_id_is_not_mixed_in():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tbA", "textbox", "共通の条件", parent="p1", order=2, ids=["_x0000_s1"]),
        _el("vmlA", "vml_shape", "共通の条件", order=3, object_id="_x0000_s1"),
        _el("flB", "floating_textbox", "共通の条件", order=4, object_id=7),
    ])
    assert suppressed == {"tbA"} and [a.element_id for a in aliases["vmlA"]] == ["tbA"]


def _two_different_figures_docx(tmp_path):
    vml_a = ('<w:r><w:pict><v:shape id="_x0000_s1"><v:textbox><w:txbxContent><w:p><w:r><w:t>共通の条件</w:t></w:r></w:p>'
             "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>")
    drawing_b = ('<w:r><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="7" name="図形B"/>'
                 '<wp:extent cx="100" cy="100"/><w:txbxContent><w:p><w:r><w:rPr><w:strike/></w:rPr><w:t>共通の条件</w:t></w:r></w:p>'
                 "</w:txbxContent></wp:anchor></w:drawing></w:r>")
    body = f"<w:p><w:r><w:t>第一段落</w:t></w:r>{vml_a}</w:p><w:p><w:r><w:t>第二段落</w:t></w:r>{drawing_b}</w:p>"
    return _docx(tmp_path / "ab.docx", body)


def _chunk_evidence_ids(rendered):
    return [{c["evidence_id"] for c in chunk.get("citations", []) if c.get("evidence_id")} for chunk in rendered.chunks]


def test_two_different_figures_with_the_same_visible_text_keep_their_own_identity(tmp_path):
    p = _two_different_figures_docx(tmp_path)
    ir = evidence_spike.extract(p)
    vml_a = next(e for e in ir.elements if e.type == "vml_shape")
    floating_b = next(e for e in ir.elements if e.type == "floating_textbox")
    child_b = next(e for e in ir.elements if e.type == "textbox" and "取り消し線" in (e.value or ""))
    rendered = R.render(ir, source_name=p.name)
    by_id = _chunk_evidence_ids(rendered)
    chunk_a = next(ids for ids in by_id if vml_a.element_id in ids)
    assert floating_b.element_id not in chunk_a and child_b.element_id not in chunk_a   # A のチャンクに B の ID・位置が混ざらない
    # B（取り消し線つき）は 1 件にまとまり、括り付きの文字と B 自身の ID・位置を持つ
    b_chunks = [chunk for chunk, ids in zip(rendered.chunks, by_id, strict=True)
                if {child_b.element_id, floating_b.element_id} & ids]
    assert len(b_chunks) == 1
    assert "【取り消し線：共通の条件】" in b_chunks[0]["body"]
    assert {c["evidence_id"] for c in b_chunks[0]["citations"]} >= {child_b.element_id, floating_b.element_id}
    assert any(c["locator"].get("object_id") == 7 for c in b_chunks[0]["citations"])
    # 括りの無い同じ文字の B が、状態なしで残らない
    assert not any(floating_b.element_id in ids and child_b.element_id not in ids for ids in by_id)
    a_chunk = next(chunk for chunk, ids in zip(rendered.chunks, by_id, strict=True) if vml_a.element_id in ids)
    assert "取り消し線" not in a_chunk["body"]


def test_state_brackets_are_removed_only_for_the_comparison():
    strip = R._without_state_brackets
    assert strip("【取り消し線：旧】と新") == "旧と新"
    assert strip("【非表示：隠】【非表示・取り消し線：両方】") == "隠両方"
    assert strip("【取り消し線（続き）：後半】") == "後半"
    assert strip("通常の文字【注記】") == "通常の文字【注記】"


def test_plain_text_with_the_same_id_is_merged_and_a_stated_one_keeps_its_brackets_as_the_body():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "【取り消し線：共通の条件】", parent="p1", order=2, ids=["7"]),
        _el("fl", "floating_textbox", "共通の条件", order=3, object_id=7),
    ])
    assert suppressed == {"fl"} and [a.element_id for a in aliases["tb"]] == ["fl"]
    # ID が一致しなければ、括りを外して同じ文字でも束ねない
    assert _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "【取り消し線：共通の条件】", parent="p1", order=2, ids=["8"]),
        _el("fl", "floating_textbox", "共通の条件", order=3, object_id=7),
    ]) == ({}, set())


def test_multi_paragraph_stated_textbox_is_one_chunk_with_the_brackets_and_paragraph_breaks(tmp_path):
    box = ('<w:r><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="5" name="図形D"/>'
           '<wp:extent cx="100" cy="100"/><w:txbxContent>'
           "<w:p><w:r><w:t>前半の条件</w:t></w:r></w:p>"
           "<w:p><w:r><w:rPr><w:strike/></w:rPr><w:t>後半の条件</w:t></w:r></w:p>"
           "</w:txbxContent></wp:anchor></w:drawing></w:r>")
    p = _docx(tmp_path / "multi.docx", f"<w:p><w:r><w:t>本文</w:t></w:r>{box}</w:p>")
    ir = evidence_spike.extract(p)
    floating = next(e for e in ir.elements if e.type == "floating_textbox")
    child = next(e for e in ir.elements if e.type == "textbox")
    assert child.value == "前半の条件\n【取り消し線：後半の条件】" and floating.value == "前半の条件\n後半の条件"
    rendered = R.render(ir, source_name=p.name)
    by_id = _chunk_evidence_ids(rendered)
    chunks = [chunk for chunk, ids in zip(rendered.chunks, by_id, strict=True) if {child.element_id, floating.element_id} & ids]
    assert len(chunks) == 1
    assert "前半の条件\n【取り消し線：後半の条件】" in chunks[0]["body"]
    assert {c["evidence_id"] for c in chunks[0]["citations"]} >= {child.element_id, floating.element_id}
    assert any(c["locator"].get("object_id") == 5 for c in chunks[0]["citations"])


def test_comparison_ignores_brackets_line_breaks_and_whitespace_but_not_ids():
    assert R._textbox_comparison_text("前半\n【取り消し線：後半　の条件】") == "前半後半の条件"
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "前半\n【取り消し線：後半】", parent="p1", order=2, ids=["5"]),
        _el("fl", "floating_textbox", "前半 後半", order=3, object_id=5),
    ])
    assert suppressed == {"fl"} and [a.element_id for a in aliases["tb"]] == ["fl"]
    assert _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "前半\n【取り消し線：後半】", parent="p1", order=2, ids=["6"]),
        _el("fl", "floating_textbox", "前半後半", order=3, object_id=5),
    ]) == ({}, set())


def test_textbox_in_an_empty_paragraph_is_merged_by_id_and_keeps_its_state(tmp_path):
    box = ('<w:r><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="9" name="図形E"/>'
           '<wp:extent cx="100" cy="100"/><w:txbxContent>'
           "<w:p><w:r><w:t>有効な条件</w:t></w:r></w:p>"
           "<w:p><w:r><w:rPr><w:strike/></w:rPr><w:t>廃止した条件</w:t></w:r></w:p>"
           "</w:txbxContent></wp:anchor></w:drawing></w:r>")
    p = _docx(tmp_path / "empty_host.docx", f"<w:p><w:r><w:t>前</w:t></w:r></w:p><w:p>{box}</w:p>")
    ir = evidence_spike.extract(p)
    floating = next(e for e in ir.elements if e.type == "floating_textbox")
    child = next(e for e in ir.elements if e.type == "textbox")
    assert child.parent_id is None                                  # 本文の無い段落に置いたテキストボックス
    rendered = R.render(ir, source_name=p.name)
    by_id = _chunk_evidence_ids(rendered)
    chunks = [chunk for chunk, ids in zip(rendered.chunks, by_id, strict=True) if {child.element_id, floating.element_id} & ids]
    assert len(chunks) == 1
    assert "有効な条件\n【取り消し線：廃止した条件】" in chunks[0]["body"]
    assert {c["evidence_id"] for c in chunks[0]["citations"]} >= {child.element_id, floating.element_id}
    assert any(c["locator"].get("object_id") == 9 for c in chunks[0]["citations"])


def test_plain_textbox_in_an_empty_paragraph_is_one_chunk_and_keeps_the_anchor_fact(tmp_path):
    box = ('<w:r><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="4" name="図形F"/>'
           '<wp:extent cx="100" cy="100"/><w:txbxContent><w:p><w:r><w:t>通常の注記</w:t></w:r></w:p></w:txbxContent>'
           "</wp:anchor></w:drawing></w:r>")
    p = _docx(tmp_path / "plain_empty_host.docx", f"<w:p>{box}</w:p>")
    rendered = R.render(evidence_spike.extract(p), source_name=p.name)
    chunks = [c for c in rendered.markdown.split("<!-- chunk:")[1:] if "テキストボックス: 「通常の注記」" in c]
    assert len(chunks) == 1
    assert "前面図形: 「通常の注記」" in chunks[0]


def _group_docx(tmp_path):
    def box(figure: str, inner: str) -> str:
        return f"<v:{figure}><v:textbox><w:txbxContent><w:p>{inner}</w:p></w:txbxContent></v:textbox></v:{figure}>"

    strike = "<w:r><w:rPr><w:strike/></w:rPr><w:t>{}</w:t></w:r>"
    plain = "<w:r><w:t>{}</w:t></w:r>"
    rect = box("rect", strike.format("条件甲") + plain.format("条件乙")).replace(
        "<v:rect>", '<v:rect id="rectA" style="position:absolute;left:0;top:0;width:100pt;height:50pt">')
    shape = box("shape", plain.format("条件甲") + strike.format("条件乙")).replace(
        "<v:shape>", '<v:shape id="shapeB" style="position:absolute;left:300pt;top:0;width:100pt;height:50pt">')
    body = f'<w:p><w:r><w:t>本文</w:t></w:r><w:r><w:pict><v:group id="grp1">{rect}{shape}</v:group></w:pict></w:r></w:p>'
    return _docx(tmp_path / "group.docx", body)


def test_textbox_ids_come_from_the_figure_that_encloses_it_not_the_whole_group(tmp_path):
    p = _group_docx(tmp_path)
    ir = ooxml_arm._build_docx_ir(p)
    boxes = [e for e in ir.elements if e.type == "textbox"]
    assert [e.source_map["object_ids"] for e in boxes] == [["rectA"], ["shapeB"]]


def test_textboxes_in_a_group_keep_their_own_state_range_id_and_position(tmp_path):
    p = _group_docx(tmp_path)
    ir = evidence_spike.extract(p)
    vml = {e.locator.object_id: e for e in ir.elements if e.type == "vml_shape"}
    child_a = next(e for e in ir.elements if e.type == "textbox" and e.value == "【取り消し線：条件甲】条件乙")
    child_b = next(e for e in ir.elements if e.type == "textbox" and e.value == "条件甲【取り消し線：条件乙】")
    rendered = R.render(ir, source_name=p.name)
    by_id = _chunk_evidence_ids(rendered)
    for child, own, other in ((child_a, vml["rectA"], vml["shapeB"]), (child_b, vml["shapeB"], vml["rectA"])):
        chunk = next(c for c, ids in zip(rendered.chunks, by_id, strict=True) if child.element_id in ids)
        assert child.value in chunk["body"]
        evidence_ids = {c["evidence_id"] for c in chunk["citations"]}
        assert own.element_id in evidence_ids and other.element_id not in evidence_ids


def test_one_native_figure_claimed_by_two_document_ir_textboxes_is_not_merged():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb1", "textbox", "同じ文字", parent="p1", order=2, ids=["7"]),
        _el("tb2", "textbox", "同じ文字", parent="p1", order=3, ids=["7"]),
        _el("fl", "floating_textbox", "同じ文字", order=4, object_id=7),
    ])
    assert (aliases, suppressed) == ({}, set())


def test_paragraph_breaks_of_the_document_ir_side_are_kept_when_the_native_side_lost_them(tmp_path):
    box = ('<w:r><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="6" name="図形G"/>'
           '<wp:extent cx="100" cy="100"/><w:txbxContent><w:p><w:r><w:t>10</w:t></w:r></w:p>'
           "<w:p><w:r><w:t>20</w:t></w:r></w:p></w:txbxContent></wp:anchor></w:drawing></w:r>")
    p = _docx(tmp_path / "numbers.docx", f"<w:p><w:r><w:t>本文</w:t></w:r>{box}</w:p>")
    ir = evidence_spike.extract(p)
    floating = next(e for e in ir.elements if e.type == "floating_textbox")
    child = next(e for e in ir.elements if e.type == "textbox")
    assert floating.value == "10\n20" and child.value == "10\n20"
    rendered = R.render(ir, source_name=p.name)
    by_id = _chunk_evidence_ids(rendered)
    chunks = [c for c, ids in zip(rendered.chunks, by_id, strict=True) if {child.element_id, floating.element_id} & ids]
    assert len(chunks) == 1
    assert "10\n20" in chunks[0]["body"] and "1020" not in chunks[0]["body"]
    assert any(c["locator"].get("object_id") == 6 for c in chunks[0]["citations"])


def test_header_textbox_paragraphs_are_not_joined_in_the_rag_or_the_human_markdown(tmp_path):
    header_box = ('<w:p><w:r><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="2" name="ヘッダー枠"/>'
                  '<wp:extent cx="100" cy="100"/><w:txbxContent><w:p><w:r><w:t>10</w:t></w:r></w:p>'
                  "<w:p><w:r><w:t>20</w:t></w:r></w:p></w:txbxContent></wp:anchor></w:drawing></w:r></w:p>")
    p = _docx(tmp_path / "header-numbers.docx", "<w:p><w:r><w:t>本文</w:t></w:r></w:p>", header=header_box)
    ir = evidence_spike.extract(p)
    floating = next(e for e in ir.elements if e.type == "floating_textbox")
    assert floating.value == "10\n20"
    rendered = R.render(ir, source_name=p.name)
    assert "1020" not in rendered.markdown and all("1020" not in c["body"] for c in rendered.chunks)
    assert any("10\n20" in c["body"] for c in rendered.chunks)
    assert "1020" not in office_md.to_markdown(p)


def test_paragraph_separated_alias_texts_are_still_merged_by_the_same_object_id():
    aliases, suppressed = _aliases([
        _el("p1", "paragraph", "本文", order=1),
        _el("tb", "textbox", "10\n20", parent="p1", order=2, ids=["6"]),
        _el("fl", "floating_textbox", "10\n20", order=3, object_id=6),
    ])
    assert suppressed == {"tb"} and [a.element_id for a in aliases["fl"]] == ["tb"]


def _alternate_content_docx(tmp_path):
    txbx = "<w:txbxContent><w:p><w:r><w:t>同じ図形</w:t></w:r></w:p></w:txbxContent>"
    body = (
        "<w:p><w:r><w:t>前置き</w:t></w:r><w:r>"
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        '<mc:Choice><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="3" name="図形C"/>'
        f'<wp:extent cx="100" cy="100"/>{txbx}</wp:anchor></w:drawing></mc:Choice>'
        f'<mc:Fallback><w:pict><v:shape id="_x0000_s9"><v:textbox>{txbx}</v:textbox></v:shape></w:pict></mc:Fallback>'
        "</mc:AlternateContent></w:r></w:p>")
    return _docx(tmp_path / "alt.docx", body)


def test_choice_and_fallback_of_the_same_figure_are_one_chunk(tmp_path):
    p = _alternate_content_docx(tmp_path)
    rendered = R.render(evidence_spike.extract(p), source_name=p.name)
    chunks = [c for c in rendered.markdown.split("<!-- chunk:")[1:]
              if any(line.endswith(": 「同じ図形」") and not line.startswith(("前面図形", "背面図形")) for line in c.split("\n"))]
    assert len(chunks) == 1, chunks


def _two_figure_alternate_content_docx(tmp_path):
    strike = "<w:r><w:rPr><w:strike/></w:rPr><w:t>{}</w:t></w:r>"
    plain = "<w:r><w:t>{}</w:t></w:r>"

    def txbx(first: str, second: str) -> str:
        return f"<w:txbxContent><w:p>{first}{second}</w:p></w:txbxContent>"

    def drawing(object_id: int, left: int, content: str) -> str:
        return (f'<w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:positionH relativeFrom="page"><wp:posOffset>{left}</wp:posOffset></wp:positionH>'
                f'<wp:positionV relativeFrom="page"><wp:posOffset>0</wp:posOffset></wp:positionV><wp:docPr id="{object_id}" name="図形{object_id}"/>'
                f'<wp:extent cx="1000000" cy="500000"/>{content}</wp:anchor></w:drawing>')

    def vml(object_id: str, left: int, content: str) -> str:
        return (f'<w:pict><v:shape id="{object_id}" style="position:absolute;left:{left}pt;top:0;width:100pt;height:50pt">'
                f"<v:textbox>{content}</v:textbox></v:shape></w:pict>")

    left_text, right_text = txbx(strike.format("AAA"), plain.format("BBB")), txbx(plain.format("AAA"), strike.format("BBB"))
    body = (
        "<w:p><w:r><w:t>前置き</w:t></w:r><w:r>"
        '<mc:AlternateContent xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006">'
        f"<mc:Choice>{drawing(2, 0, left_text)}{drawing(3, 3000000, right_text)}</mc:Choice>"
        f"<mc:Fallback>{vml('fallbackB', 300, right_text)}{vml('fallbackA', 0, left_text)}</mc:Fallback>"
        "</mc:AlternateContent></w:r></w:p>")
    return _docx(tmp_path / "alt_two.docx", body)


def test_fallback_figures_are_not_paired_with_choice_figures_by_order_alone(tmp_path):
    p = _two_figure_alternate_content_docx(tmp_path)
    boxes = [e for e in ooxml_arm._build_docx_ir(p).elements if e.type == "textbox"]
    assert [e.source_map["object_ids"] for e in boxes] == [["2"], ["3"]]   # Fallback 側の図形の ID・位置を順番だけで足さない
    ir = evidence_spike.extract(p)
    left = next(e for e in ir.elements if e.type == "textbox" and e.value.startswith("【取り消し線：AAA】"))
    fallback_b = next(e for e in ir.elements if e.type == "vml_shape" and e.locator.object_id == "fallbackB")
    rendered = R.render(ir, source_name=p.name)
    chunk = next(c for c, ids in zip(rendered.chunks, _chunk_evidence_ids(rendered), strict=True) if left.element_id in ids)
    assert "【取り消し線：AAA】BBB" in chunk["body"]
    assert fallback_b.element_id not in {c["evidence_id"] for c in chunk["citations"]}


def test_body_textbox_and_header_textbox_with_the_same_text_both_stay_in_the_rag(tmp_path):
    body_box = ('<w:r><w:pict><v:shape id="_x0000_s1"><v:textbox><w:txbxContent><w:p><w:r><w:t>共通の文字</w:t></w:r></w:p>'
                "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>")
    header_box = ('<w:p><w:r><w:drawing><wp:anchor behindDoc="0" relativeHeight="1"><wp:docPr id="2" name="ヘッダー枠"/>'
                  '<wp:extent cx="100" cy="100"/><w:t>共通の文字</w:t></wp:anchor></w:drawing></w:r></w:p>')
    p = _docx(tmp_path / "parts.docx", f"<w:p><w:r><w:t>本文</w:t></w:r>{body_box}</w:p>", header=header_box)
    rendered = R.render(evidence_spike.extract(p), source_name=p.name)
    chunks = [c for c in rendered.markdown.split("<!-- chunk:")[1:] if "「共通の文字」" in c]
    assert any("vml_shape: 「共通の文字」" in c for c in chunks), chunks            # 本文のテキストボックス
    assert any("浮動テキストボックス: 「共通の文字」" in c for c in chunks), chunks   # ヘッダーのテキストボックス


def test_rag_keeps_both_the_body_paragraph_and_the_textbox_chunks(tmp_path):
    textbox = ('<w:r><w:pict><v:shape id="_x0000_s1"><v:textbox><w:txbxContent><w:p><w:r><w:t>共通の文字</w:t></w:r></w:p>'
               "</w:txbxContent></v:textbox></v:shape></w:pict></w:r>")
    p = _docx(tmp_path / "same.docx", f"<w:p><w:r><w:t>共通の文字</w:t></w:r>{textbox}</w:p>")
    ir = evidence_spike.extract(p)
    rendered = R.render(ir, source_name=p.name)
    chunks = [c for c in rendered.markdown.split("<!-- chunk:")[1:] if "「共通の文字」" in c]
    assert len(chunks) == 2, chunks
    assert any(c for c in chunks if "本文: 「共通の文字」" in c) and any(c for c in chunks if "本文: 「共通の文字」" not in c)
    md = office_md.to_markdown(p)
    assert md.count("共通の文字") == 2                                   # 人用 Markdown には 2 つ残る


# ---- ④: 本文の画像の注記にヘッダーの画像を混ぜない ----

def test_body_picture_note_does_not_carry_the_header_picture_with_the_same_object_id(tmp_path):
    p = _docx(
        tmp_path / "header.docx",
        f"<w:p><w:r><w:t>本文</w:t></w:r></w:p><w:p>{_picture(1, '本文の画像')}</w:p><w:p><w:r><w:t>末尾</w:t></w:r></w:p>",
        header=f"<w:p>{_picture(1, 'ヘッダーの画像')}</w:p>")
    md = office_md.to_markdown(p)
    assert md.count(_NOT_READ) == 1, md
    assert "本文の画像" in md and "ヘッダーの画像" not in md
    assert md.index("本文") < md.index(_NOT_READ) < md.index("末尾")


# ---- ⑤: 画像だけの文書にも通知を出す ----

def test_picture_only_docx_gets_the_human_md_and_the_unread_notice(tmp_path):
    p = _docx(tmp_path / "pic.docx", f"<w:p>{_picture(1, '社章')}</w:p>")
    md = office_md.to_markdown(p)
    assert md is not None
    assert "画像が1枚あります" in md and f"「社章」{_NOT_READ}" in md


def test_truly_empty_docx_still_has_no_human_md(tmp_path):
    p = _docx(tmp_path / "empty.docx", "<w:p/>")
    assert office_md.to_markdown(p) is None
    assert human_md.render_docx(ooxml_arm._build_docx_ir(p)) is None


def test_ingest_writes_the_human_md_for_a_picture_only_docx(tmp_path):
    src, derived = tmp_path / "src", tmp_path / "derived"
    src.mkdir()
    _docx(src / "pic.docx", f"<w:p>{_picture(1, '社章')}</w:p>")
    report = office_md.build_derived(src, derived)
    assert report["rag_failed"] == 0 and report["failed"] == 0, report
    written = (derived / "pic.docx.md").read_text(encoding="utf-8")
    assert _NOT_READ in written

