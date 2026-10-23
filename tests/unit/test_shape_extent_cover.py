"""前面の図形の形による遮蔽の判定（pptx・xlsx・docx）。

塗りの観測（fill=opaque）と、その形が背面を実際に覆うかは分ける。覆う範囲を外接矩形と同一視してよい形（矩形・画像）だけが
confirmed で、ドーナツ・楕円・矢印・吹き出しなどのプリセット形状とカスタム形状は undetermined（背面は hidden にしない・重なりは残す）。
"""
from __future__ import annotations

import os
import re

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest
from test_docx_floating_cover import _anchor, _body, _covers, _docx, _figure, _shape
from test_occlusion_fill_kinds import (
    _BACK_TEXT,
    _GRADIENT,
    _PNG,
    _SOLID,
    _SOLID_ALPHA_50,
    _pptx,
    _xlsx_with_drawing,
)

from sherpa.ingest import drawing_fill, evidence_ir, evidence_render, evidence_spike, observation_vocab as vocab, office_md

_CONFIRMED_LINE = "覆われていると判定"
_UNDETERMINED_LINE = "覆われているかは未確定"

_DONUT = '<a:prstGeom prst="donut"><a:avLst/></a:prstGeom>'
_ELLIPSE = '<a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>'
_RECT = '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'
_ROUND_RECT = '<a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>'
_ARROW = '<a:prstGeom prst="rightArrow"><a:avLst/></a:prstGeom>'
_CALLOUT = '<a:prstGeom prst="wedgeRectCallout"><a:avLst/></a:prstGeom>'
_CUSTOM = ('<a:custGeom><a:avLst/><a:gdLst/><a:ahLst/><a:cxnLst/><a:rect l="0" t="0" r="r" b="b"/><a:pathLst>'
           '<a:path w="10" h="10"><a:moveTo><a:pt x="0" y="0"/></a:moveTo><a:lnTo><a:pt x="10" y="10"/></a:lnTo>'
           "</a:path></a:pathLst></a:custGeom>")

# 形 → 外接矩形と同一視してよいか。形状の指定が無い図形は描画されない（LibreOffice で塗りが出ないことを確認済み）ので覆うと言えない。
_GEOMETRIES = {
    "none": ("", False),
    "rect": (_RECT, True),
    "donut": (_DONUT, False),
    "ellipse": (_ELLIPSE, False),
    "round_rect": (_ROUND_RECT, False),
    "right_arrow": (_ARROW, False),
    "callout": (_CALLOUT, False),
    "custom": (_CUSTOM, False),
}


def _xfrm_at(x: int, y: int, cx: int, cy: int) -> str:
    return f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'


def _pptx_hole_scene(geometry: str, fill_xml: str, *, text: str = "NOTE") -> str:
    """前面の 4,000,000 角の図形（輪の上に文字）の中央の穴に、小さい背面のテキストボックスを収めた slide の図形群。"""
    back = (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="背面の図形"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm_at(1500000, 1700000, 1000000, 600000)}</p:spPr>"
        f"<p:txBody><a:p><a:r><a:t>{_BACK_TEXT}</a:t></a:r></a:p></p:txBody></p:sp>")
    body = f"<p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody>" if text else ""
    front = (
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="前面の図形"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm_at(0, 0, 4000000, 4000000)}{geometry}{fill_xml}</p:spPr>{body}</p:sp>")
    return back + front


def _native_back(ir: evidence_ir.EvidenceIR) -> evidence_ir.EvidenceElement:
    return next(el for el in ir.elements if el.type == "shape" and el.value == _BACK_TEXT
                and el.extension.get("origin") != "document-ir-v2-adapter")


def _assert_cover(ir: evidence_ir.EvidenceIR, fill: str, occlusion: str) -> None:
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS]
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (fill, occlusion)
    assert cover.extension["cover_ratio"] == pytest.approx(1.0)


@pytest.mark.parametrize("with_text", [True, False])
@pytest.mark.parametrize("case", sorted(_GEOMETRIES))
def test_pptx_opaque_front_covers_only_when_its_shape_matches_the_bounding_box(tmp_path, case, with_text):
    geometry, exact = _GEOMETRIES[case]
    p = _pptx(tmp_path, _pptx_hole_scene(geometry, _SOLID, text="NOTE" if with_text else ""))
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    _assert_cover(ir, vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED if exact else vocab.OCCLUSION_UNDETERMINED)
    backs = [el for el in ir.elements if el.type == "shape" and el.value == _BACK_TEXT]
    assert len(backs) == 2
    for back in backs:
        assert (back.visibility == "hidden") is exact
        assert (back.extension.get("visibility_reason") == "occluded") is exact
    assert any(r.type == "overlaps" for r in ir.relations)                       # 重なりの観測は残る

    rag = evidence_render.render(ir, source_name=p.name).markdown
    human = office_md.to_markdown(p)
    assert human is not None and _BACK_TEXT in human
    for markdown in (rag, human):
        assert (_CONFIRMED_LINE in markdown) is exact
        assert (_UNDETERMINED_LINE in markdown) is (not exact)
    assert (office_md._HIDDEN_MARKER in human) is exact                          # 人用 Markdown の［隠し候補］も同じ判定
    assert (rag.count("可視性: 「図形に覆われている」") > 0) is exact


def test_pptx_donut_hole_reproduction_keeps_the_back_visible_and_records_the_shape(tmp_path):
    ir = evidence_spike.extract(_pptx(tmp_path, _pptx_hole_scene(_DONUT, _SOLID)))
    back = _native_back(ir)
    assert back.visibility == "visible" and "occluded_by" not in back.extension
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS]
    assert cover.extension["covers_bbox"] is False and cover.extension["fill"] == vocab.FILL_OPAQUE


@pytest.mark.parametrize("fill_xml,fill,occlusion", [
    (_SOLID_ALPHA_50, vocab.FILL_TRANSLUCENT, vocab.OCCLUSION_NOT_OCCLUDING),
    ("<a:noFill/>", vocab.FILL_NONE_WITH_TEXT, vocab.OCCLUSION_NOT_OCCLUDING),
    (_GRADIENT, vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED),
])
@pytest.mark.parametrize("geometry", [_RECT, _DONUT])
def test_pptx_fill_boundaries_do_not_depend_on_the_shape(tmp_path, geometry, fill_xml, fill, occlusion):
    ir = evidence_spike.extract(_pptx(tmp_path, _pptx_hole_scene(geometry, fill_xml)))
    _assert_cover(ir, fill, occlusion)
    assert _native_back(ir).visibility == "visible"
    cover = next(r for r in ir.relations if r.type == vocab.RELATION_COVERS)
    assert ("covers_bbox" in cover.extension) is (geometry == _DONUT)


def _pptx_picture_scene(tmp_path, geometry: str):
    back = _pptx_hole_scene("", "").split("<p:sp>", 2)[1]
    picture = (
        '<p:pic><p:nvPicPr><p:cNvPr id="3" name="前面の画像"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        '<p:blipFill><a:blip r:embed="rId2"/></p:blipFill>'
        f"<p:spPr>{_xfrm_at(0, 0, 4000000, 4000000)}{geometry}</p:spPr></p:pic>")
    return _pptx(tmp_path, "<p:sp>" + back + picture, media=_PNG)


def test_pptx_picture_with_a_rectangle_mask_is_confirmed_and_with_an_ellipse_mask_is_undetermined(tmp_path):
    ir = evidence_spike.extract(_pptx_picture_scene(tmp_path, _RECT))
    _assert_cover(ir, vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED)
    assert _native_back(ir).visibility == "hidden"
    ir = evidence_spike.extract(_pptx_picture_scene(tmp_path, _ELLIPSE))
    _assert_cover(ir, vocab.FILL_OPAQUE, vocab.OCCLUSION_UNDETERMINED)
    assert _native_back(ir).visibility == "visible"


def test_covers_bounding_box_rule():
    from xml.etree import ElementTree as ET

    def sp_pr(inner: str) -> ET.Element:
        return ET.fromstring(f'<spPr xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">{inner}</spPr>')

    assert not drawing_fill.covers_bounding_box(None)                    # 形状の指定が無い図形は覆う範囲を言えない
    assert not drawing_fill.covers_bounding_box(sp_pr(""))
    assert drawing_fill.covers_bounding_box(None, picture=True)           # 画像は形状の指定が無くても矩形で描画される
    assert drawing_fill.covers_bounding_box(sp_pr(""), picture=True)
    assert not drawing_fill.covers_bounding_box(sp_pr(_ELLIPSE), picture=True)
    assert drawing_fill.covers_bounding_box(sp_pr(_RECT))
    for geometry in (_DONUT, _ELLIPSE, _ROUND_RECT, _ARROW, _CALLOUT, _CUSTOM):
        assert not drawing_fill.covers_bounding_box(sp_pr(geometry))
    assert vocab.occlusion_of(vocab.FILL_OPAQUE, covers_bbox=False) == vocab.OCCLUSION_UNDETERMINED
    assert vocab.occlusion_of(vocab.FILL_TRANSLUCENT, covers_bbox=False) == vocab.OCCLUSION_NOT_OCCLUDING


# ---- xlsx ----

def _xlsx_front(geometry: str, fill_xml: str, text: str = "") -> str:
    body = f"<xdr:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></xdr:txBody>" if text else ""
    return (
        '<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="1" name="前面の図形"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        f"<xdr:spPr>{geometry}{fill_xml}</xdr:spPr>{body}</xdr:sp>")


@pytest.mark.parametrize("with_text", [True, False])
@pytest.mark.parametrize("case", sorted(_GEOMETRIES))
def test_xlsx_opaque_front_covers_only_when_its_shape_matches_the_bounding_box(tmp_path, case, with_text):
    geometry, exact = _GEOMETRIES[case]
    p = _xlsx_with_drawing(tmp_path, _xlsx_front(geometry, _SOLID, "NOTE" if with_text else ""))
    ir = evidence_spike.extract(p)
    cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
    assert (cell.visibility == "hidden") is exact
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS and r.target_id == cell.element_id]
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (
        vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED if exact else vocab.OCCLUSION_UNDETERMINED)
    assert any(r.type == "overlaps" and r.source_id == cover.source_id for r in ir.relations)
    for markdown in (evidence_render.render(ir, source_name=p.name).markdown, office_md.to_markdown(p)):
        assert (_CONFIRMED_LINE in markdown) is exact


def _front_chunk(markdown: str, marker: str) -> str:
    chunks = [c for c in re.split(r"<!-- chunk:[^>]*-->", markdown) if marker in c]
    assert len(chunks) == 1, chunks
    return chunks[0]


def test_xlsx_front_shape_without_text_names_itself_in_its_chunk(tmp_path):
    """文字の無い前面の図形のチャンクにも、図形の名前・図形 ID・位置と出所の行を書く（どの図形の重なりか分かる）。"""
    p = _xlsx_with_drawing(tmp_path, _xlsx_front(_RECT, _SOLID))
    markdown = evidence_render.render(evidence_spike.extract(p), source_name=p.name).markdown
    front = _front_chunk(markdown, "覆っている対象:")
    assert "出所: 原本「front.xlsx」 / シート「Sheet」" in front
    assert "この要素: 前面の図形（図形 ID 1・シート「Sheet」のセル範囲 A1:A1）" in front
    back = _front_chunk(markdown, "前面図形:")
    assert "前面図形: 前面の図形（図形 ID 1・シート「Sheet」のセル範囲 A1:A1）" in back


def test_xlsx_front_shape_with_text_keeps_its_chunk_as_is(tmp_path):
    p = _xlsx_with_drawing(tmp_path, _xlsx_front(_RECT, _SOLID, "NOTE"))
    markdown = evidence_render.render(evidence_spike.extract(p), source_name=p.name).markdown
    front = _front_chunk(markdown, "覆っている対象:")
    assert "図形: 「NOTE」" in front
    assert "この要素:" not in front


def test_xlsx_picture_mask_follows_the_same_rule(tmp_path):
    def picture(geometry: str) -> str:
        return (
            '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="前面の画像"/><xdr:cNvPicPr/></xdr:nvPicPr>'
            f'<xdr:blipFill><a:blip r:embed="rId1"/></xdr:blipFill><xdr:spPr>{geometry}</xdr:spPr></xdr:pic>')

    for geometry, exact in ((_RECT, True), (_ELLIPSE, False)):
        ir = evidence_spike.extract(_xlsx_with_drawing(tmp_path, picture(geometry), media=_PNG))
        cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
        assert (cell.visibility == "hidden") is exact


# ---- docx ----

@pytest.mark.parametrize("case", sorted(set(_GEOMETRIES) - {"none"}))
def test_docx_opaque_front_covers_only_when_its_shape_matches_the_bounding_box(tmp_path, case):
    geometry, exact = _GEOMETRIES[case]
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1)
    front = _anchor(2, "前面の図形", _shape(geometry + _SOLID, "NOTE"), height=2)
    ir = evidence_spike.extract(_docx(tmp_path, _body(back, front)))
    assert evidence_ir.validation_errors(ir) == []
    (cover,) = _covers(ir)
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (
        vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED if exact else vocab.OCCLUSION_UNDETERMINED)
    assert (_figure(ir, "背面の図形").visibility == "hidden") is exact


def test_docx_picture_mask_follows_the_same_rule(tmp_path):
    def picture(geometry: str) -> str:
        return ('<pic:pic><pic:blipFill><a:blip r:embed="rId5"/></pic:blipFill>'
                f"<pic:spPr>{geometry}</pic:spPr></pic:pic>")

    for geometry, exact in ((_RECT, True), (_ELLIPSE, False)):
        back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1)
        front = _anchor(2, "前面の画像", picture(geometry), height=2)
        ir = evidence_spike.extract(_docx(tmp_path, _body(back, front), media=_PNG))
        assert (_figure(ir, "背面の図形").visibility == "hidden") is exact


def test_docx_shape_without_geometry_is_undetermined(tmp_path):
    bare = "<wps:wsp><wps:spPr>" + _SOLID + "</wps:spPr></wps:wsp>"
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1)
    ir = evidence_spike.extract(_docx(tmp_path, _body(back, _anchor(2, "前面の図形", bare, height=2))))
    (cover,) = _covers(ir)
    assert cover.extension["occlusion"] == vocab.OCCLUSION_UNDETERMINED
    assert _figure(ir, "背面の図形").visibility == "visible"
