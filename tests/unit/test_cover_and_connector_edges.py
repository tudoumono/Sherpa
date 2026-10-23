"""覆い（covers）とコネクタ（connects_to）の端の扱い。

① 原本の非表示の理由（行・列）を遮蔽の理由で上書きしない。
② Word の浮動図形は、Word が組版したページ境界で同じページと確かめられる組だけ遮蔽を判定する（確かめられなければ未確定）。
③ 片端だけ解決できるコネクタの関係を残し、「接続先は不明」と書く。
④ 画像の中身を判定できない pic を不透明と断定しない（未確定）。
"""
from __future__ import annotations

import os
import pathlib
import zipfile

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest
from test_docx_floating_cover import _anchor, _body, _covers, _docx, _figure, _figure_overlaps, _shape, _SOLID
from test_observation_base import _by_object_id, _connector, _pptx, _sp_text
from test_occlusion_fill_kinds import (
    _A_NS,
    _PNG,
    _XDR_NS,
    _assert_pptx_result,
    _pptx_back,
    _pptx_front_picture,
    _SOLID as _SOLID_FILL,
    _xlsx_picture,
    _xlsx_shape,
    _xlsx_with_drawing,
)

from sherpa.ingest import evidence_ir, evidence_render, evidence_spike, observation_vocab as vocab, office_md

# ---- ① 非表示の行・列を覆いで上書きしない ----


@pytest.mark.parametrize("flag,reason", [("hidden_row", "hidden_row"), ("hidden_column", "hidden_column")])
@pytest.mark.parametrize("drawing", [_xlsx_shape(_SOLID_FILL), _xlsx_picture()])
def test_hidden_row_or_column_reason_survives_an_opaque_cover(tmp_path, flag, reason, drawing):
    p = _xlsx_with_drawing(tmp_path, drawing, media=_PNG if "pic" in drawing else None, **{flag: True})
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
    assert cell.visibility == "hidden"
    assert cell.extension["visibility_reason"] == reason                  # 遮蔽の理由に置き換わらない
    assert cell.extension["occluded_by"]["element_id"]                   # 覆いは occluded_by と covers に残る
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS and r.target_id == cell.element_id]
    assert cover.extension["occlusion"] == vocab.OCCLUSION_CONFIRMED
    assert cell.extension["state_basis"] == {"visibility": vocab.STATE_BASIS_OOXML_DIRECT}


def test_cover_reason_still_applies_to_a_visible_cell_and_overwrites_only_cover_reasons(tmp_path):
    ir = evidence_spike.extract(_xlsx_with_drawing(tmp_path, _xlsx_shape(_SOLID_FILL)))
    cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
    assert cell.extension["visibility_reason"] == "occluded_by_shape"
    assert cell.extension["state_basis"] == {"visibility": vocab.STATE_BASIS_GEOMETRY}


# ---- ② Word の別ページの図形 ----

_PAGE_BREAK = '<w:p><w:r><w:br w:type="page"/></w:r></w:p>'
_RENDERED_BREAK = '<w:p><w:r><w:lastRenderedPageBreak/><w:t>次のページ</w:t></w:r></w:p>'


def _paragraph(*figures: str) -> str:
    return "<w:p><w:r><w:t>段落</w:t></w:r>" + "".join(figures) + "</w:p>"


def _pair() -> tuple[str, str]:
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1)
    front = _anchor(2, "前面の図形", _shape(_SOLID), height=2)
    return back, front


def _assert_no_cover(ir: evidence_ir.EvidenceIR) -> None:
    assert evidence_ir.validation_errors(ir) == []
    assert _covers(ir) == []
    assert _figure(ir, "背面の図形").visibility == "visible"
    (overlap,) = _figure_overlaps(ir)
    assert overlap.extension["occlusion"] == vocab.OCCLUSION_UNDETERMINED


def test_figures_across_an_explicit_page_break_are_not_covered(tmp_path):
    back, front = _pair()
    body = _paragraph(back) + _PAGE_BREAK + _paragraph(front)
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, body)))


def test_page_break_inside_the_paragraph_between_the_anchors_separates_them(tmp_path):
    back, front = _pair()
    body = "<w:p>" + back + '<w:r><w:br w:type="page"/></w:r>' + front + "</w:p>"
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, body)))


@pytest.mark.parametrize("separator", [
    '<w:p><w:pPr><w:sectPr/></w:pPr></w:p>',                       # セクション区切り
    '<w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:r><w:t>x</w:t></w:r></w:p>',
])
def test_section_break_and_page_break_before_separate_the_figures(tmp_path, separator):
    back, front = _pair()
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, _paragraph(back) + separator + _paragraph(front))))


def test_figures_in_different_paragraphs_of_an_unrendered_document_are_not_confirmed_on_one_page(tmp_path):
    back, front = _pair()
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, _paragraph(back) + _paragraph(front))))


def test_figures_anchored_in_the_same_paragraph_of_an_unrendered_document_are_not_confirmed_on_one_page(tmp_path):
    """組版の記録が無い文書では、長い段落がページをまたぐことがあるので、同じ段落でも同じページと断定しない。"""
    back, front = _pair()
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, _paragraph(back, front))))


def test_same_paragraph_figures_are_separated_by_a_rendered_page_boundary_between_the_anchors(tmp_path):
    back, front = _pair()
    split = "<w:p>" + back + "<w:r><w:lastRenderedPageBreak/></w:r>" + front + "</w:p>"
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, split)))
    before = "<w:p><w:r><w:lastRenderedPageBreak/></w:r>" + back + front + "</w:p>"      # 境界は 2 つのアンカーの前
    ir = evidence_spike.extract(_docx(tmp_path, before))
    (cover,) = _covers(ir)
    assert cover.extension["occlusion"] == vocab.OCCLUSION_CONFIRMED
    assert _figure(ir, "背面の図形").visibility == "hidden"


def test_word_rendered_page_boundaries_confirm_a_shared_page_across_paragraphs(tmp_path):
    back, front = _pair()
    shared = _paragraph(back) + _paragraph(front) + _RENDERED_BREAK
    ir = evidence_spike.extract(_docx(tmp_path, shared))
    (cover,) = _covers(ir)
    assert cover.extension["occlusion"] == vocab.OCCLUSION_CONFIRMED
    assert _figure(ir, "背面の図形").visibility == "hidden"

    apart = _paragraph(back) + _RENDERED_BREAK + _paragraph(front)
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, apart)))


_NESTED_BREAKS = {
    "sdt_page_break_before": (
        '<w:sdt><w:sdtContent><w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:r><w:t>x</w:t></w:r></w:p></w:sdtContent></w:sdt>'),
    "sdt_section_break": '<w:sdt><w:sdtContent><w:p><w:pPr><w:sectPr/></w:pPr></w:p></w:sdtContent></w:sdt>',
    "sdt_page_break_run": '<w:sdt><w:sdtContent><w:p><w:r><w:br w:type="page"/></w:r></w:p></w:sdtContent></w:sdt>',
    "table_cell_page_break_before": (
        '<w:tbl><w:tr><w:tc><w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:r><w:t>セル</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'),
    "table_cell_rendered_boundary": (
        '<w:tbl><w:tr><w:tc><w:p><w:r><w:lastRenderedPageBreak/><w:t>セル</w:t></w:r></w:p></w:tc></w:tr></w:tbl>'),
    "custom_xml_page_break": (
        '<w:customXml w:element="e"><w:p><w:r><w:br w:type="page"/></w:r></w:p></w:customXml>'),
    "smart_tag_page_break_before": (
        '<w:smartTag w:element="e"><w:p><w:pPr><w:pageBreakBefore/></w:pPr><w:r><w:t>y</w:t></w:r></w:p></w:smartTag>'),
}


@pytest.mark.parametrize("name", sorted(_NESTED_BREAKS))
def test_page_breaks_nested_in_content_controls_and_table_cells_separate_the_figures(tmp_path, name):
    """組版済みの文書で、入れ子の中の改ページ・セクション区切りも、図形の前後を別のページとして数える。"""
    back, front = _pair()
    body = "<w:p><w:r><w:lastRenderedPageBreak/></w:r>" + back + "</w:p>" + _NESTED_BREAKS[name] + _paragraph(front)
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, body)))


def test_figures_inside_a_content_control_without_a_break_share_the_page(tmp_path):
    back, front = _pair()
    body = ("<w:p><w:r><w:lastRenderedPageBreak/></w:r></w:p><w:sdt><w:sdtContent>"
            + _paragraph(back) + _paragraph(front) + "</w:sdtContent></w:sdt>")
    ir = evidence_spike.extract(_docx(tmp_path, body))
    (cover,) = _covers(ir)
    assert cover.extension["occlusion"] == vocab.OCCLUSION_CONFIRMED


def test_page_break_before_applies_before_the_paragraph_and_section_break_after_it(tmp_path):
    back, front = _pair()
    rendered = "<w:p><w:r><w:lastRenderedPageBreak/></w:r></w:p>"
    before = rendered + '<w:p><w:pPr><w:pageBreakBefore/></w:pPr>' + back + "</w:p>" + _paragraph(front)
    ir = evidence_spike.extract(_docx(tmp_path, before))                   # 段落の前で区切るので、背面の図形は新しいページの側
    assert [c.extension["occlusion"] for c in _covers(ir)] == [vocab.OCCLUSION_CONFIRMED]
    after = rendered + '<w:p><w:pPr><w:sectPr/></w:pPr>' + back + "</w:p>" + _paragraph(front)
    _assert_no_cover(evidence_spike.extract(_docx(tmp_path, after)))       # 段落の後で区切るので、前面の図形は次のページ


# ---- ③ 片端だけ解決できるコネクタ ----

_UNKNOWN_START = "接続元: 不明（始点の図形を原本から特定できない）"
_UNKNOWN_END = "接続先: 不明（終点の図形を原本から特定できない）"


def _connect_relations(ir: evidence_ir.EvidenceIR) -> list[evidence_ir.EvidenceRelation]:
    return [r for r in ir.relations if r.type == "connects_to"]


def _pptx_partial(tmp_path, *, start, end):
    shapes = (
        _sp_text("始点の文字", shape_id=2, name="始点", x=0, y=0, cx=1000000, cy=500000)
        + _sp_text("終点の文字", shape_id=3, name="終点", x=2000000, y=500000, cx=1000000, cy=500000)
        + _connector(shape_id=4, name="線", start=start, end=end))
    return _pptx(tmp_path, shapes, name="partial.pptx")


def test_pptx_connector_with_only_the_start_resolved_keeps_one_relation_and_says_the_end_is_unknown(tmp_path):
    p = _pptx_partial(tmp_path, start=2, end=99)
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    (relation,) = _connect_relations(ir)
    connector = _by_object_id(ir, 4)
    assert (relation.source_id, relation.target_id) == (_by_object_id(ir, 2).element_id, connector.element_id)
    assert relation.extension["ends"] == vocab.ENDS_SOURCE_ONLY
    assert relation.extension["unresolved_object_id"] == 99
    assert relation.extension["connector_element_id"] == connector.element_id

    md = evidence_render.render(ir, source_name=p.name).markdown
    assert _UNKNOWN_END in md
    assert "接続: 始点（図形 ID 2・スライド1） → 不明の図形" in md
    assert "接続元: 不明" not in md
    human = office_md.to_markdown(p)
    assert "接続先は不明です" in human and "不明の図形を接続しています" in human


def test_pptx_connector_with_only_the_end_resolved_keeps_one_relation_and_says_the_start_is_unknown(tmp_path):
    p = _pptx_partial(tmp_path, start=98, end=3)
    ir = evidence_spike.extract(p)
    (relation,) = _connect_relations(ir)
    connector = _by_object_id(ir, 4)
    assert (relation.source_id, relation.target_id) == (connector.element_id, _by_object_id(ir, 3).element_id)
    assert relation.extension["ends"] == vocab.ENDS_TARGET_ONLY
    assert relation.extension["unresolved_object_id"] == 98
    md = evidence_render.render(ir, source_name=p.name).markdown
    assert _UNKNOWN_START in md and "接続: 不明の図形 → 終点（図形 ID 3・スライド1）" in md
    assert "接続元は不明です" in office_md.to_markdown(p)


def test_pptx_connector_with_neither_end_resolved_makes_no_relation(tmp_path):
    ir = evidence_spike.extract(_pptx_partial(tmp_path, start=98, end=99))
    assert _connect_relations(ir) == []


def test_pptx_connector_with_both_ends_is_unchanged(tmp_path):
    ir = evidence_spike.extract(_pptx_partial(tmp_path, start=2, end=3))
    (relation,) = _connect_relations(ir)
    assert relation.extension["ends"] == vocab.ENDS_BOTH and "unresolved_object_id" not in relation.extension
    assert (relation.source_id, relation.target_id) == (_by_object_id(ir, 2).element_id, _by_object_id(ir, 3).element_id)


def test_partial_connector_is_not_drawn_as_a_mermaid_edge(tmp_path):
    p = _pptx_partial(tmp_path, start=2, end=99)
    markdown = evidence_render.render(evidence_spike.extract(p), source_name=p.name).markdown
    diagram = markdown.split("```mermaid", 1)[1].split("```", 1)[0]
    assert "-->" not in diagram and "未接続: 線" in diagram              # 辺は引かず、未接続のコネクタとして書く


def _xlsx_connector(start: int, end: int) -> str:
    return (
        '<xdr:cxnSp><xdr:nvCxnSpPr><xdr:cNvPr id="2" name="線"/><xdr:cNvCxnSpPr>'
        f'<a:stCxn id="{start}" idx="0"/><a:endCxn id="{end}" idx="0"/></xdr:cNvCxnSpPr></xdr:nvCxnSpPr>'
        "<xdr:spPr/></xdr:cxnSp>")


def test_xlsx_connector_with_only_one_end_resolved_keeps_one_relation(tmp_path):
    p = _xlsx_with_drawing(tmp_path, _xlsx_shape("<a:noFill/>", text="始点") + _xlsx_connector(1, 77))
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    (relation,) = _connect_relations(ir)
    assert relation.extension["ends"] == vocab.ENDS_SOURCE_ONLY and relation.extension["unresolved_object_id"] == 77
    assert _UNKNOWN_END in evidence_render.render(ir, source_name=p.name).markdown

    ir = evidence_spike.extract(_xlsx_with_drawing(tmp_path, _xlsx_shape("<a:noFill/>", text="終点") + _xlsx_connector(66, 1)))
    (relation,) = _connect_relations(ir)
    assert relation.extension["ends"] == vocab.ENDS_TARGET_ONLY and relation.extension["unresolved_object_id"] == 66


# ---- ④ 画像の中身を判定できない pic ----

def test_picture_without_a_blip_is_unknown_and_never_hides(tmp_path):
    from test_occlusion_fill_kinds import _pptx as pptx_with_media
    p = pptx_with_media(tmp_path, _pptx_back() + _pptx_front_picture(""))
    _assert_pptx_result(p, vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED)


def test_xlsx_picture_without_a_blip_is_unknown_and_never_hides(tmp_path):
    ir = evidence_spike.extract(_xlsx_with_drawing(tmp_path, _xlsx_picture("")))
    cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
    assert cell.visibility == "visible"
    (cover,) = [r for r in _covers_any(ir) if r.target_id == cell.element_id]
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED)


def _covers_any(ir: evidence_ir.EvidenceIR) -> list[evidence_ir.EvidenceRelation]:
    return [r for r in ir.relations if r.type == vocab.RELATION_COVERS]
