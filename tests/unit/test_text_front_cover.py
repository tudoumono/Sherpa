"""文字を持つ前面の図形も、文字の有無にかかわらず塗りを読んで遮蔽を判定する（pptx・xlsx・docx）。

前面の塗りが opaque なら confirmed で背面は hidden、translucent は not_occluding、unknown は undetermined。
none_with_text は「塗りなし（または完全透過）で文字がある」ときだけ。文字から業務上の廃止は決めない。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest
from test_docx_floating_cover import _anchor, _body, _covers, _docx, _figure, _shape
from test_occlusion_fill_kinds import (
    _BACK_TEXT,
    _GRADIENT,
    _RECT,
    _SOLID,
    _SOLID_ALPHA_0,
    _SOLID_ALPHA_50,
    _pptx,
    _pptx_back,
    _xfrm,
    _xlsx_shape,
    _xlsx_with_drawing,
)

from sherpa.ingest import evidence_ir, evidence_render, evidence_spike, observation_vocab as vocab, office_md

_FRONT_TEXT = "廃止"
_CONFIRMED_LINE = "覆われていると判定"

# 前面の塗り XML → (区分, 遮蔽の確からしさ, 覆われてはいない理由の平文)
_FILLS = {
    "opaque": (_SOLID, vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED, ""),
    "translucent": (_SOLID_ALPHA_50, vocab.FILL_TRANSLUCENT, vocab.OCCLUSION_NOT_OCCLUDING, "半透明"),
    "no_fill": ("<a:noFill/>", vocab.FILL_NONE_WITH_TEXT, vocab.OCCLUSION_NOT_OCCLUDING, "塗りなしで文字だけ"),
    "fully_transparent": (_SOLID_ALPHA_0, vocab.FILL_NONE_WITH_TEXT, vocab.OCCLUSION_NOT_OCCLUDING, "塗りなしで文字だけ"),
    "gradient": (_GRADIENT, vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED, ""),
}


def _pptx_front_with_text(fill_xml: str) -> str:
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="前面の図形"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm()}{_RECT}{fill_xml}</p:spPr>"
        f"<p:txBody><a:p><a:r><a:t>{_FRONT_TEXT}</a:t></a:r></a:p></p:txBody></p:sp>"
    )


def _assert_notes(markdown: str, occlusion: str, label: str) -> None:
    if occlusion == vocab.OCCLUSION_CONFIRMED:
        assert _CONFIRMED_LINE in markdown
        return
    assert _CONFIRMED_LINE not in markdown
    if occlusion == vocab.OCCLUSION_NOT_OCCLUDING:
        assert f"覆われてはいない：{label}" in markdown
    else:
        assert "覆われているかは未確定" in markdown


@pytest.mark.parametrize("case", sorted(_FILLS))
def test_pptx_front_shape_with_text_is_judged_by_its_fill(tmp_path, case):
    fill_xml, fill, occlusion, label = _FILLS[case]
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_with_text(fill_xml))
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    confirmed = occlusion == vocab.OCCLUSION_CONFIRMED
    backs = [el for el in ir.elements if el.type == "shape" and el.value == _BACK_TEXT]
    assert backs
    for back in backs:
        assert (back.visibility == "hidden") is confirmed
        assert (back.extension.get("visibility_reason") == "occluded") is confirmed
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS]
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (fill, occlusion)
    if fill == vocab.FILL_NONE_WITH_TEXT:
        assert backs[0].extension["covered_by_text"]["text"] == _FRONT_TEXT
    else:
        assert all("covered_by_text" not in back.extension for back in backs)

    rag = evidence_render.render(ir, source_name=p.name).markdown
    _assert_notes(rag, occlusion, label)
    human = office_md.to_markdown(p)
    assert human is not None and _BACK_TEXT in human
    _assert_notes(human, occlusion, label)
    assert (office_md._HIDDEN_MARKER in human) is confirmed        # 人用 Markdown の［隠し候補］も同じ判定


def test_pptx_text_front_without_fill_matches_the_old_none_with_text_result(tmp_path):
    """文字だけの前面（塗りの指定が無い）は、従来どおり none_with_text。"""
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_with_text(""))
    ir = evidence_spike.extract(p)
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS]
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (
        vocab.FILL_NONE_WITH_TEXT, vocab.OCCLUSION_NOT_OCCLUDING)


def test_pptx_hidden_text_front_does_not_cover(tmp_path):
    """自身が別の不透明な図形に隠れている前面の文字は、背面を覆わない。"""
    hider = (
        '<p:sp><p:nvSpPr><p:cNvPr id="4" name="さらに前面"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm()}{_RECT}{_SOLID}</p:spPr></p:sp>")
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_with_text(_SOLID) + hider)
    ir = evidence_spike.extract(p)
    back = next(el for el in ir.elements if el.type == "shape" and el.value == _BACK_TEXT
                and el.extension.get("origin") != "document-ir-v2-adapter")
    assert back.visibility == "hidden"
    covers_to_back = [r for r in ir.relations if r.type == vocab.RELATION_COVERS and r.target_id == back.element_id]
    assert {r.source_id for r in covers_to_back} == {_figure_id(ir, "さらに前面")}


def _figure_id(ir: evidence_ir.EvidenceIR, name: str) -> str:
    return next(el.element_id for el in ir.elements
                if el.extension.get("name") == name and el.extension.get("origin") != "document-ir-v2-adapter")


@pytest.mark.parametrize("case", sorted(_FILLS))
def test_xlsx_front_shape_with_text_is_judged_by_its_fill(tmp_path, case):
    fill_xml, fill, occlusion, _label = _FILLS[case]
    p = _xlsx_with_drawing(tmp_path, _xlsx_shape(fill_xml, text=_FRONT_TEXT))
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
    confirmed = occlusion == vocab.OCCLUSION_CONFIRMED
    assert (cell.visibility == "hidden") is confirmed
    (cover,) = [r for r in ir.relations if r.type == vocab.RELATION_COVERS and r.target_id == cell.element_id]
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (fill, occlusion)
    _assert_notes(evidence_render.render(ir, source_name=p.name).markdown, occlusion, _label)
    human = office_md.to_markdown(p)
    assert human is not None
    _assert_notes(human, occlusion, _label)


@pytest.mark.parametrize("case", sorted(_FILLS))
def test_docx_front_figure_with_text_is_judged_by_its_fill(tmp_path, case):
    fill_xml, fill, occlusion, _label = _FILLS[case]
    back = _anchor(1, "背面の図形", _shape(_SOLID, "背面の文字"), height=1)
    front = _anchor(2, "前面の図形", _shape(fill_xml, _FRONT_TEXT), height=2)
    ir = evidence_spike.extract(_docx(tmp_path, _body(back, front)))
    assert evidence_ir.validation_errors(ir) == []
    (cover,) = _covers(ir)
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (fill, occlusion)
    assert (_figure(ir, "背面の図形").visibility == "hidden") is (occlusion == vocab.OCCLUSION_CONFIRMED)
