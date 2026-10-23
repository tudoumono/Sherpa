"""docx の浮動図形の遮蔽（位置の決まった図形どうしの covers・本文に重なる図形の overlaps）。

正典: docs/proposals/2026-10-22-Office観測の共通の扱い.md §1.3・§3.1。docx は文字の位置が組版で決まるため、遮蔽を判定できるのは
ページ／余白基準の絶対位置の図形どうしだけ。図形が本文の文字に重なる場合は covers を作らず、overlaps に occlusion=undetermined を付けて本文は hidden にしない。
"""
from __future__ import annotations

import pathlib
import zipfile

import pytest

import _image_builders
from sherpa.ingest import evidence_ir, evidence_spike, observation_vocab as vocab

_NS = (
    'xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main" '
    'xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing" '
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture" '
    'xmlns:wps="http://schemas.microsoft.com/office/word/2010/wordprocessingShape" '
    'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"'
)
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"
_SOLID = '<a:solidFill><a:srgbClr val="FF0000"/></a:solidFill>'
_SOLID_ALPHA_50 = '<a:solidFill><a:srgbClr val="FF0000"><a:alpha val="50000"/></a:srgbClr></a:solidFill>'


def _position(axis: str, relative_from: str, offset: int) -> str:
    return f'<wp:position{axis} relativeFrom="{relative_from}"><wp:posOffset>{offset}</wp:posOffset></wp:position{axis}>'


def _anchor(
    object_id: int, name: str, inner: str, *, x: int = 0, y: int = 0, cx: int = 1000000, cy: int = 1000000,
    height: int = 1, frame_h: str = "page", frame_v: str = "page", behind: bool = False, wrap: str = "<wp:wrapNone/>",
) -> str:
    return (
        f'<w:r><w:drawing><wp:anchor behindDoc="{1 if behind else 0}" relativeHeight="{height}">'
        f"{_position('H', frame_h, x)}{_position('V', frame_v, y)}"
        f'<wp:extent cx="{cx}" cy="{cy}"/>{wrap}<wp:docPr id="{object_id}" name="{name}"/>'
        f"<a:graphic><a:graphicData>{inner}</a:graphicData></a:graphic></wp:anchor></w:drawing></w:r>"
    )


def _shape(fill_xml: str, text: str = "") -> str:
    txbx = f"<wps:txbx><w:txbxContent><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:txbxContent></wps:txbx>" if text else ""
    geometry = "" if "Geom" in fill_xml else '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'   # 形状の指定が無い図形は描画されない
    return f"<wps:wsp><wps:spPr>{geometry}{fill_xml}</wps:spPr>{txbx}</wps:wsp>"


def _picture() -> str:
    return '<pic:pic><pic:blipFill><a:blip r:embed="rId5"/></pic:blipFill></pic:pic>'


def _docx(tmp_path: pathlib.Path, body: str, *, media: bytes | None = None) -> pathlib.Path:
    entries: dict[str, bytes | str] = {
        "word/document.xml": f'<?xml version="1.0"?><w:document {_NS}><w:body>{body}</w:body></w:document>',
    }
    if media is not None:
        entries["word/media/image1.png"] = media
        entries["word/_rels/document.xml.rels"] = (
            f'<Relationships xmlns="{_RELS_NS}">'
            f'<Relationship Id="rId5" Type="{_IMAGE_REL}" Target="media/image1.png"/></Relationships>')
    path = tmp_path / "floating.docx"
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def _figure(ir: evidence_ir.EvidenceIR, name: str) -> evidence_ir.EvidenceElement:
    return next(el for el in ir.elements if el.extension.get("name") == name)


def _covers(ir: evidence_ir.EvidenceIR) -> list[evidence_ir.EvidenceRelation]:
    return [r for r in ir.relations if r.type == vocab.RELATION_COVERS]


def _figure_overlaps(ir: evidence_ir.EvidenceIR) -> list[evidence_ir.EvidenceRelation]:
    """図形どうしの重なり（本文の段落への重なりは除く）。"""
    paragraphs = {el.element_id for el in ir.elements if el.type == "paragraph"}
    return [r for r in ir.relations if r.type == "overlaps" and r.target_id not in paragraphs]


def _body(*figures: str) -> str:
    """図形が 1 段落にアンカーされた本文。先頭に Word が組版したページ境界の印を持つ（組版済みの文書として同じページを確かめられる）。"""
    return "<w:p><w:r><w:lastRenderedPageBreak/><w:t>本文の段落</w:t></w:r>" + "".join(figures) + "</w:p>"


# 前面の塗りの区分ごと: (前面の内容, 期待する区分, 背面が hidden になるか)
_FRONTS = {
    "opaque": (lambda: _shape(_SOLID), vocab.FILL_OPAQUE, True),
    "translucent": (lambda: _shape(_SOLID_ALPHA_50), vocab.FILL_TRANSLUCENT, False),
    "transparent": (lambda: _shape("<a:noFill/>"), vocab.FILL_TRANSPARENT, False),
    "none_with_text": (lambda: _shape("<a:noFill/>", "廃止"), vocab.FILL_NONE_WITH_TEXT, False),
    "unknown": (lambda: _shape('<a:gradFill><a:gsLst/></a:gradFill>'), vocab.FILL_UNKNOWN, False),
}


@pytest.mark.parametrize("case", sorted(_FRONTS))
def test_absolutely_positioned_figures_get_covers_by_fill_kind(tmp_path, case):
    make_front, fill, hides = _FRONTS[case]
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1)
    front = _anchor(2, "前面の図形", make_front(), height=2)
    ir = evidence_spike.extract(_docx(tmp_path, _body(back, front)))
    assert evidence_ir.validation_errors(ir) == []

    back_element, front_element = _figure(ir, "背面の図形"), _figure(ir, "前面の図形")
    (cover,) = _covers(ir)
    assert (cover.source_id, cover.target_id) == (front_element.element_id, back_element.element_id)
    assert cover.extension["fill"] == fill
    assert cover.extension["occlusion"] == vocab.occlusion_of(fill)
    assert cover.extension["cover_ratio"] == pytest.approx(1.0)
    assert (back_element.visibility == "hidden") is hides
    assert any(r.type == "overlaps" and {r.source_id, r.target_id} == {front_element.element_id, back_element.element_id}
               for r in ir.relations)


def test_absolutely_positioned_picture_uses_the_image_alpha(tmp_path):
    opaque = _docx(tmp_path, _body(
        _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1),
        _anchor(2, "前面の画像", _picture(), height=2)), media=_image_builders.opaque_png())
    ir = evidence_spike.extract(opaque)
    assert _covers(ir)[0].extension["occlusion"] == vocab.OCCLUSION_CONFIRMED
    assert _figure(ir, "背面の図形").visibility == "hidden"

    transparent = _docx(tmp_path, _body(
        _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1),
        _anchor(2, "前面の画像", _picture(), height=2)), media=_image_builders.transparent_png())
    ir = evidence_spike.extract(transparent)
    (cover,) = _covers(ir)
    assert cover.extension["fill"] == vocab.FILL_TRANSPARENT_PNG
    assert cover.extension["occlusion"] == vocab.OCCLUSION_UNDETERMINED
    assert _figure(ir, "背面の図形").visibility == "visible"


def test_cover_below_the_ratio_threshold_keeps_only_the_overlap(tmp_path):
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1)
    front = _anchor(2, "前面の図形", _shape(_SOLID), x=500000, height=2)       # 背面の半分だけ重なる
    ir = evidence_spike.extract(_docx(tmp_path, _body(back, front)))
    assert _covers(ir) == []
    assert _figure(ir, "背面の図形").visibility == "visible"
    assert any(r.type == "overlaps" for r in ir.relations)


def test_figures_in_different_position_frames_are_undetermined_not_covered(tmp_path):
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1, frame_h="page")
    front = _anchor(2, "前面の図形", _shape(_SOLID), height=2, frame_h="margin")
    ir = evidence_spike.extract(_docx(tmp_path, _body(back, front)))
    assert _covers(ir) == []
    assert _figure(ir, "背面の図形").visibility == "visible"
    (overlap,) = _figure_overlaps(ir)
    assert overlap.extension["occlusion"] == vocab.OCCLUSION_UNDETERMINED


def test_figures_positioned_against_the_paragraph_are_undetermined_not_covered(tmp_path):
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1, frame_h="column", frame_v="paragraph")
    front = _anchor(2, "前面の図形", _shape(_SOLID), height=2, frame_h="column", frame_v="paragraph")
    ir = evidence_spike.extract(_docx(tmp_path, _body(back, front)))
    assert _covers(ir) == []
    assert _figure(ir, "背面の図形").visibility == "visible"
    (overlap,) = _figure_overlaps(ir)
    assert overlap.extension["occlusion"] == vocab.OCCLUSION_UNDETERMINED


def test_figure_over_body_text_is_undetermined_and_never_hides_the_text(tmp_path):
    figure = _anchor(1, "廃止スタンプ", _shape(_SOLID, "廃止"), height=1)
    ir = evidence_spike.extract(_docx(tmp_path, _body(figure)))
    assert evidence_ir.validation_errors(ir) == []
    paragraph = next(el for el in ir.elements if el.type == "paragraph")
    stamp = _figure(ir, "廃止スタンプ")
    (overlap,) = [r for r in ir.relations if r.type == "overlaps"]
    assert (overlap.source_id, overlap.target_id) == (stamp.element_id, paragraph.element_id)
    assert overlap.extension["occlusion"] == vocab.OCCLUSION_UNDETERMINED
    assert overlap.extension["basis"] == vocab.BASIS_OOXML_ANCHOR
    assert _covers(ir) == []
    assert paragraph.visibility == "visible" and not paragraph.extension.get("visibility_reason")


def test_figure_behind_the_text_does_not_cover_it(tmp_path):
    figure = _anchor(1, "背面の透かし", _shape(_SOLID), height=1, behind=True)
    ir = evidence_spike.extract(_docx(tmp_path, _body(figure)))
    (overlap,) = [r for r in ir.relations if r.type == "overlaps"]
    assert overlap.extension["occlusion"] == vocab.OCCLUSION_NOT_OCCLUDING
    assert next(el for el in ir.elements if el.type == "paragraph").visibility == "visible"


def test_figure_that_text_wraps_around_has_no_text_overlap(tmp_path):
    figure = _anchor(1, "回り込みの図形", _shape(_SOLID), wrap="<wp:wrapSquare/>")
    ir = evidence_spike.extract(_docx(tmp_path, _body(figure)))
    assert [r for r in ir.relations if r.type == "overlaps"] == []
    assert _covers(ir) == []
