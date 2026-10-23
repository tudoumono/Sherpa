"""形状を直接指定していないプレースホルダーは、スライドレイアウト・スライドマスターから受け継いだ形状で遮蔽を判定する。

受け継いだ形が矩形なら confirmed、ドーナツなど矩形でなければ undetermined、継承元を解決できなければ undetermined
（背面は hidden にしない・重なりは残す。RAG と人用 Markdown の［隠し候補］・注記も同じ判定）。
"""
from __future__ import annotations

import os
import pathlib

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest
from test_occlusion_fill_kinds import _BACK_TEXT, _PPTX_NS, _RELS_NS, _SOLID, _write_zip
from test_shape_extent_cover import _DONUT, _ELLIPSE, _RECT, _pptx_hole_scene, _xfrm_at

from sherpa.ingest import evidence_ir, evidence_render, evidence_spike, observation_vocab as vocab, office_md

_LAYOUT_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout"
_MASTER_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster"


def _placeholder_shape(ph: str, geometry: str = "", *, shape_id: int = 3) -> str:
    """スライド側に不透明な塗りと位置・寸法を持ち、形状は `geometry`（既定は直接指定しない）の前面のプレースホルダー。"""
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="{shape_id}" name="前面の図形"/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr>'
        f"<p:spPr>{_xfrm_at(0, 0, 4000000, 4000000)}{geometry}{_SOLID}</p:spPr></p:sp>")


def _template_shape(ph: str, geometry: str) -> str:
    return (
        f'<p:sp><p:nvSpPr><p:cNvPr id="2" name="枠"/><p:cNvSpPr/><p:nvPr>{ph}</p:nvPr></p:nvSpPr>'
        f"<p:spPr>{geometry}</p:spPr></p:sp>")


def _part(root: str, shapes: str) -> str:
    return f'<?xml version="1.0"?><p:{root} {_PPTX_NS}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:{root}>'


def _rels(rel_type: str, target: str) -> str:
    return f'<Relationships xmlns="{_RELS_NS}"><Relationship Id="rId1" Type="{rel_type}" Target="{target}"/></Relationships>'


def _deck(tmp_path: pathlib.Path, front: str, *, layout: str | None = None, master: str | None = None) -> pathlib.Path:
    back = _pptx_hole_scene("", "").split("<p:sp>", 2)[1]
    entries: dict[str, str] = {
        "ppt/slides/slide1.xml": _part("sld", "<p:sp>" + back + front),
    }
    if layout is not None:
        entries["ppt/slides/_rels/slide1.xml.rels"] = _rels(_LAYOUT_REL, "../slideLayouts/slideLayout1.xml")
        entries["ppt/slideLayouts/slideLayout1.xml"] = _part("sldLayout", layout)
        entries["ppt/slideLayouts/_rels/slideLayout1.xml.rels"] = _rels(_MASTER_REL, "../slideMasters/slideMaster1.xml")
    if master is not None:
        entries["ppt/slideMasters/slideMaster1.xml"] = _part("sldMaster", master)
    return _write_zip(tmp_path / "deck.pptx", entries)


def _assert_judgement(p: pathlib.Path, confirmed: bool) -> None:
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS]
    assert cover.extension["fill"] == vocab.FILL_OPAQUE
    assert cover.extension["occlusion"] == (vocab.OCCLUSION_CONFIRMED if confirmed else vocab.OCCLUSION_UNDETERMINED)
    backs = [el for el in ir.elements if el.type == "shape" and el.value == _BACK_TEXT]
    assert backs and all((back.visibility == "hidden") is confirmed for back in backs)
    assert any(r.type == "overlaps" for r in ir.relations)
    rag = evidence_render.render(ir, source_name=p.name).markdown
    human = office_md.to_markdown(p)
    assert human is not None
    for markdown in (rag, human):
        assert ("覆われていると判定" in markdown) is confirmed
        assert ("覆われているかは未確定" in markdown) is (not confirmed)
    assert (office_md._HIDDEN_MARKER in human) is confirmed


_BODY = '<p:ph type="body" idx="1"/>'


def test_placeholder_inheriting_a_donut_from_the_layout_keeps_the_back_visible(tmp_path):
    p = _deck(tmp_path, _placeholder_shape(_BODY), layout=_template_shape(_BODY, _DONUT))
    _assert_judgement(p, confirmed=False)


def test_placeholder_inheriting_a_rectangle_from_the_layout_is_confirmed(tmp_path):
    p = _deck(tmp_path, _placeholder_shape(_BODY), layout=_template_shape(_BODY, _RECT))
    _assert_judgement(p, confirmed=True)


def test_local_geometry_wins_over_the_inherited_one(tmp_path):
    donut_front = _placeholder_shape(_BODY, _RECT)
    _assert_judgement(_deck(tmp_path, donut_front, layout=_template_shape(_BODY, _DONUT)), confirmed=True)
    _assert_judgement(_deck(tmp_path, _placeholder_shape(_BODY, _ELLIPSE), layout=_template_shape(_BODY, _RECT)), confirmed=False)


def test_geometry_missing_in_the_layout_is_taken_from_the_master(tmp_path):
    no_geometry_layout = _template_shape(_BODY, "")
    _assert_judgement(_deck(tmp_path, _placeholder_shape(_BODY), layout=no_geometry_layout,
                            master=_template_shape('<p:ph type="body" idx="1"/>', _DONUT)), confirmed=False)
    _assert_judgement(_deck(tmp_path, _placeholder_shape(_BODY), layout=no_geometry_layout,
                            master=_template_shape('<p:ph type="body" idx="1"/>', _RECT)), confirmed=True)


def test_placeholders_are_matched_by_idx_then_by_type(tmp_path):
    layout = _template_shape('<p:ph type="body" idx="1"/>', _DONUT) + _template_shape('<p:ph type="body" idx="2"/>', _RECT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body" idx="2"/>'), layout=layout), confirmed=True)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body" idx="1"/>'), layout=layout), confirmed=False)
    by_type = _template_shape('<p:ph type="title"/>', _DONUT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="title"/>'), layout=by_type), confirmed=False)


def test_omitted_idx_is_zero_when_matching_the_layout(tmp_path):
    layout = _template_shape('<p:ph type="body" idx="1"/>', _RECT) + _template_shape('<p:ph type="body" idx="0"/>', _DONUT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body"/>'), layout=layout), confirmed=False)


def test_duplicate_normalized_idx_is_undetermined(tmp_path):
    layout = _template_shape('<p:ph type="title" idx="0"/>', _RECT) + _template_shape('<p:ph type="ctrTitle" idx="00"/>', _DONUT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="ctrTitle"/>'), layout=layout), confirmed=False)
    layout = _template_shape('<p:ph type="body" idx="2"/>', "")
    master = _template_shape('<p:ph type="body" idx="2"/>', _RECT) + _template_shape('<p:ph type="body" idx="02"/>', _DONUT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body" idx="2"/>'), layout=layout, master=master),
                      confirmed=False)


def test_explicit_zero_idx_matches_an_omitted_idx(tmp_path):
    rect_zero = _template_shape('<p:ph type="body"/>', _RECT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body" idx="0"/>'), layout=rect_zero), confirmed=True)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body"/>'), layout=rect_zero), confirmed=True)


def test_center_title_and_subtitle_follow_the_master_title_and_body(tmp_path):
    layout = _template_shape('<p:ph type="ctrTitle"/>', "")
    master = _template_shape('<p:ph type="title"/>', _DONUT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="ctrTitle"/>'), layout=layout, master=master), confirmed=False)


def test_master_is_searched_with_the_type_of_the_layout_placeholder(tmp_path):
    slide_ph = '<p:ph idx="2"/>'
    layout = _template_shape('<p:ph type="title" idx="2"/>', "")
    body_rect = _template_shape('<p:ph type="body" idx="1"/>', _RECT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape(slide_ph), layout=layout,
                            master=_template_shape('<p:ph type="title"/>', _DONUT) + body_rect), confirmed=False)
    _assert_judgement(_deck(tmp_path, _placeholder_shape(slide_ph), layout=layout,
                            master=_template_shape('<p:ph type="title"/>', _RECT) + _template_shape('<p:ph type="body" idx="1"/>', _DONUT)),
                      confirmed=True)


def test_master_prefers_the_same_idx_and_does_not_guess_among_same_type_placeholders(tmp_path):
    layout = _template_shape('<p:ph type="body" idx="2"/>', "")
    rect_1 = _template_shape('<p:ph type="body" idx="1"/>', _RECT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body" idx="2"/>'), layout=layout,
                            master=rect_1 + _template_shape('<p:ph type="body" idx="2"/>', _DONUT)), confirmed=False)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body" idx="2"/>'), layout=layout,
                            master=rect_1 + _template_shape('<p:ph type="body" idx="3"/>', _DONUT)), confirmed=False)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph type="body" idx="2"/>'), layout=layout,
                            master=rect_1), confirmed=True)


def test_slide_placeholder_without_a_layout_match_does_not_fall_back_to_the_master(tmp_path):
    layout = _template_shape('<p:ph type="title" idx="3"/>', "")
    master = _template_shape('<p:ph type="body" idx="1"/>', _RECT)
    _assert_judgement(_deck(tmp_path, _placeholder_shape('<p:ph idx="2"/>'), layout=layout, master=master), confirmed=False)


@pytest.mark.parametrize("layout,master", [
    (None, None),                                                                    # 継承元の関係が無い
    (_template_shape('<p:ph type="body" idx="9"/>', _RECT), None),                    # 対応するプレースホルダーが無い
    (_template_shape(_BODY, ""), None),                                              # どこにも形状の指定が無い
])
def test_unresolved_inheritance_is_undetermined(tmp_path, layout, master):
    _assert_judgement(_deck(tmp_path, _placeholder_shape(_BODY), layout=layout, master=master), confirmed=False)


def test_a_plain_rectangle_without_placeholder_is_still_confirmed(tmp_path):
    front = (
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="前面の図形"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm_at(0, 0, 4000000, 4000000)}{_RECT}{_SOLID}</p:spPr></p:sp>")
    _assert_judgement(_deck(tmp_path, front, layout=_template_shape(_BODY, _DONUT)), confirmed=True)
