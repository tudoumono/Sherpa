"""前面の塗りの区分（opaque・translucent・transparent・none_with_text・transparent_png・unknown）による遮蔽の判定。

正典: docs/proposals/2026-10-22-Office観測の共通の扱い.md §1.4・§3.1。背面が hidden になるのは occlusion=confirmed のときだけで、
重なり（overlaps）と covers（fill・occlusion つき）はどの区分でも残る。実環境の資料は使わず、手組みの最小パッケージだけを使う。
"""
from __future__ import annotations

import pathlib
import zipfile

import openpyxl
import pytest
from openpyxl.drawing.image import Image as XLImage
from openpyxl.drawing.spreadsheet_drawing import AnchorMarker, TwoCellAnchor

import _image_builders
from sherpa.ingest import evidence_ir, evidence_spike, image_alpha, observation_vocab as vocab, office_md
from sherpa.ingest.arms import ooxml_arm

_A_NS = "http://schemas.openxmlformats.org/drawingml/2006/main"
_XDR_NS = "http://schemas.openxmlformats.org/drawingml/2006/spreadsheetDrawing"
_R_NS = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
_PPTX_NS = (
    f'xmlns:a="{_A_NS}" xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main" xmlns:r="{_R_NS}"'
)
_PNG = _image_builders.opaque_png()
_RELS_NS = "http://schemas.openxmlformats.org/package/2006/relationships"
_IMAGE_REL = "http://schemas.openxmlformats.org/officeDocument/2006/relationships/image"

_RECT = '<a:prstGeom prst="rect"><a:avLst/></a:prstGeom>'   # 形状の指定が無い図形は描画されないので、図形には矩形を書く
_SOLID = '<a:solidFill><a:srgbClr val="FF0000"/></a:solidFill>'
_SOLID_ALPHA_50 = '<a:solidFill><a:srgbClr val="FF0000"><a:alpha val="50000"/></a:srgbClr></a:solidFill>'
_SOLID_ALPHA_0 = '<a:solidFill><a:srgbClr val="FF0000"><a:alpha val="0"/></a:srgbClr></a:solidFill>'
_GRADIENT = '<a:gradFill><a:gsLst><a:gs pos="0"><a:srgbClr val="FF0000"/></a:gs></a:gsLst></a:gradFill>'

# 前面の塗り XML と、期待する (区分, 遮蔽の確からしさ)
_SHAPE_FILLS = {
    "opaque": (_SOLID, vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED),
    "translucent": (_SOLID_ALPHA_50, vocab.FILL_TRANSLUCENT, vocab.OCCLUSION_NOT_OCCLUDING),
    "transparent_alpha0": (_SOLID_ALPHA_0, vocab.FILL_TRANSPARENT, vocab.OCCLUSION_NOT_OCCLUDING),
    "transparent_nofill": ("<a:noFill/>", vocab.FILL_TRANSPARENT, vocab.OCCLUSION_NOT_OCCLUDING),
    "unknown_gradient": (_GRADIENT, vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED),
}
# 前面の画像のバイト列と期待する区分
_PICTURES = {
    "opaque_png": (_image_builders.opaque_png(), vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED),
    "opaque_jpeg": (_image_builders.jpeg(), vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED),
    "opaque_rgba_png": (_image_builders.opaque_rgba_png(), vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED),
    "transparent_png": (_image_builders.transparent_png(), vocab.FILL_TRANSPARENT_PNG, vocab.OCCLUSION_UNDETERMINED),
    "transparent_png_trns": (
        _image_builders.png(color_type=2, trns=True), vocab.FILL_TRANSPARENT_PNG, vocab.OCCLUSION_UNDETERMINED),
    "oversized_png": (_image_builders.opaque_rgba_png(), vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED),
    "unreadable": (b"not an image", vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED),
}


def _limit_pixels_for_oversized(monkeypatch, case: str) -> None:
    """上限を超える画像の例は、上限を小さくして作る（巨大な画像を作らない）。"""
    if case == "oversized_png":
        monkeypatch.setattr(image_alpha, "MAX_PIXELS", 10)


def _write_zip(path: pathlib.Path, entries: dict[str, bytes | str]) -> pathlib.Path:
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)
    return path


def _covers(ir: evidence_ir.EvidenceIR) -> list[evidence_ir.EvidenceRelation]:
    return [r for r in ir.relations if r.type == vocab.RELATION_COVERS]


# ---- pptx ----

_BACK_TEXT = "背面の文字"


def _xfrm() -> str:
    return '<a:xfrm><a:off x="0" y="0"/><a:ext cx="4000000" cy="2000000"/></a:xfrm>'


def _pptx_back() -> str:
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="2" name="背面の図形"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm()}</p:spPr><p:txBody><a:p><a:r><a:t>{_BACK_TEXT}</a:t></a:r></a:p></p:txBody></p:sp>"
    )


def _pptx_front_shape(fill_xml: str, *, style: str = "") -> str:
    return (
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="前面の図形"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm()}{_RECT}{fill_xml}</p:spPr>{style}</p:sp>"
    )


def _pptx_front_picture(blip: str) -> str:
    return (
        '<p:pic><p:nvPicPr><p:cNvPr id="3" name="前面の画像"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>'
        f"<p:blipFill>{blip}</p:blipFill><p:spPr>{_xfrm()}</p:spPr></p:pic>"
    )


def _pptx(tmp_path: pathlib.Path, shapes: str, *, media: bytes | None = None, external: bool = False) -> pathlib.Path:
    entries: dict[str, bytes | str] = {
        "ppt/slides/slide1.xml": (
            f'<?xml version="1.0"?><p:sld {_PPTX_NS}><p:cSld><p:spTree>{shapes}</p:spTree></p:cSld></p:sld>'),
    }
    if media is not None:
        entries["ppt/media/image1.png"] = media
        entries["ppt/slides/_rels/slide1.xml.rels"] = (
            f'<Relationships xmlns="{_RELS_NS}"><Relationship Id="rId2" Type="{_IMAGE_REL}" Target="../media/image1.png"/>'
            "</Relationships>")
    if external:
        entries["ppt/slides/_rels/slide1.xml.rels"] = (
            f'<Relationships xmlns="{_RELS_NS}"><Relationship Id="rId2" Type="{_IMAGE_REL}" '
            'Target="https://example.invalid/stamp.png" TargetMode="External"/></Relationships>')
    return _write_zip(tmp_path / "front.pptx", entries)


def _assert_pptx_result(p: pathlib.Path, fill: str, occlusion: str) -> None:
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    back_elements = [el for el in ir.elements if el.type == "shape" and el.value == _BACK_TEXT]
    assert back_elements                       # 原本の図形と DocumentIR 由来の両方
    confirmed = occlusion == vocab.OCCLUSION_CONFIRMED
    for back in back_elements:
        assert (back.visibility == "hidden") is confirmed
        assert (back.extension.get("visibility_reason") == "occluded") is confirmed
    (cover,) = _covers(ir)
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (fill, occlusion)
    assert any(r.type == "overlaps" for r in ir.relations)
    md = office_md.to_markdown(p)
    assert md is not None and _BACK_TEXT in md
    assert (office_md._HIDDEN_MARKER in md) is confirmed          # 人用 Markdown の［隠し候補］は confirmed だけ


@pytest.mark.parametrize("case", sorted(_SHAPE_FILLS))
def test_pptx_shape_fill_kinds(tmp_path, case):
    fill_xml, fill, occlusion = _SHAPE_FILLS[case]
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_shape(fill_xml))
    _assert_pptx_result(p, fill, occlusion)


def test_pptx_theme_fill_reference_is_unknown(tmp_path):
    style = '<p:style><a:fillRef idx="1"><a:schemeClr val="accent1"/></a:fillRef></p:style>'
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_shape("", style=style))
    _assert_pptx_result(p, vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED)


@pytest.mark.parametrize("case", sorted(_PICTURES))
def test_pptx_picture_fill_kinds(tmp_path, monkeypatch, case):
    data, fill, occlusion = _PICTURES[case]
    _limit_pixels_for_oversized(monkeypatch, case)
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_picture('<a:blip r:embed="rId2"/>'), media=data)
    _assert_pptx_result(p, fill, occlusion)


def test_pptx_picture_with_alpha_mod_fix_is_translucent(tmp_path):
    blip = '<a:blip r:embed="rId2"><a:alphaModFix amt="40000"/></a:blip>'
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_picture(blip), media=_PNG)
    _assert_pptx_result(p, vocab.FILL_TRANSLUCENT, vocab.OCCLUSION_NOT_OCCLUDING)


def test_pptx_external_picture_is_unknown(tmp_path):
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_picture('<a:blip r:link="rId2"/>'), external=True)
    _assert_pptx_result(p, vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED)


def test_pptx_text_in_front_with_no_fill_is_none_with_text(tmp_path):
    front = (
        '<p:sp><p:nvSpPr><p:cNvPr id="3" name="前面の文字"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>'
        f"<p:spPr>{_xfrm()}<a:noFill/></p:spPr><p:txBody><a:p><a:r><a:t>廃止</a:t></a:r></a:p></p:txBody></p:sp>"
    )
    p = _pptx(tmp_path, _pptx_back() + front)
    ir = evidence_spike.extract(p)
    back = next(el for el in ir.elements if el.type == "shape" and el.value == _BACK_TEXT)
    assert back.visibility == "visible"
    (cover,) = _covers(ir)
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (
        vocab.FILL_NONE_WITH_TEXT, vocab.OCCLUSION_NOT_OCCLUDING)
    assert back.extension["covered_by_text"]["text"] == "廃止"
    md = office_md.to_markdown(p)
    assert md is not None and office_md._HIDDEN_MARKER not in md


def test_pptx_document_ir_keeps_every_front_with_its_fill(tmp_path):
    p = _pptx(tmp_path, _pptx_back() + _pptx_front_shape(_SOLID_ALPHA_50))
    document = ooxml_arm._build_pptx_ir(p)
    shape = next(el for el in document.elements if el.type == "shape")
    assert shape.visibility == "visible" and "occluded_by" not in shape.source_map
    assert shape.source_map["fronts"] == [
        {"z_index": 1, "shape_id": 3, "fill": vocab.FILL_TRANSLUCENT, "covers_bbox": True}]


# ---- xlsx ----

def _xlsx_with_drawing(
    tmp_path: pathlib.Path, drawing_body: str, *, media: bytes | None = None,
    hidden_row: bool = False, hidden_column: bool = False,
) -> pathlib.Path:
    """A1 に値のあるシートの A1 全体に重なる drawing を持つ xlsx。画像は openpyxl で配線してから中身を差し替える。"""
    seed = tmp_path / "seed.png"
    seed.write_bytes(_PNG)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws["A1"] = "背面の値"
    if hidden_row:
        ws.row_dimensions[1].hidden = True
    if hidden_column:
        ws.column_dimensions["A"].hidden = True
    img = XLImage(str(seed))
    img.anchor = TwoCellAnchor(
        editAs="oneCell", _from=AnchorMarker(col=0, colOff=0, row=0, rowOff=0),
        to=AnchorMarker(col=1, colOff=0, row=1, rowOff=0))
    ws.add_image(img)
    path = tmp_path / "front.xlsx"
    wb.save(path)
    with zipfile.ZipFile(path) as z:
        entries = {name: z.read(name) for name in z.namelist()}
    drawing = next(n for n in entries if n.startswith("xl/drawings/drawing") and n.endswith(".xml"))
    media_name = next(n for n in entries if n.startswith("xl/media/"))
    entries[drawing] = (
        f'<?xml version="1.0"?><xdr:wsDr xmlns:xdr="{_XDR_NS}" xmlns:a="{_A_NS}" xmlns:r="{_R_NS}">'
        '<xdr:twoCellAnchor editAs="oneCell"><xdr:from><xdr:col>0</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>0</xdr:row>'
        "<xdr:rowOff>0</xdr:rowOff></xdr:from><xdr:to><xdr:col>1</xdr:col><xdr:colOff>0</xdr:colOff><xdr:row>1</xdr:row>"
        f"<xdr:rowOff>0</xdr:rowOff></xdr:to>{drawing_body}<xdr:clientData/></xdr:twoCellAnchor></xdr:wsDr>"
    ).encode()
    if media is not None:
        entries[media_name] = media
    return _write_zip(path, entries)


def _xlsx_shape(fill_xml: str, *, text: str = "", style: str = "") -> str:
    body = f"<xdr:txBody><a:bodyPr/><a:p><a:r><a:t>{text}</a:t></a:r></a:p></xdr:txBody>" if text else ""
    return (
        '<xdr:sp><xdr:nvSpPr><xdr:cNvPr id="1" name="前面の図形"/><xdr:cNvSpPr/></xdr:nvSpPr>'
        f"<xdr:spPr>{_RECT}{fill_xml}</xdr:spPr>{style}{body}</xdr:sp>"
    )


def _xlsx_picture(blip: str = '<a:blip r:embed="rId1"/>') -> str:
    return (
        '<xdr:pic><xdr:nvPicPr><xdr:cNvPr id="1" name="前面の画像"/><xdr:cNvPicPr/></xdr:nvPicPr>'
        f"<xdr:blipFill>{blip}</xdr:blipFill><xdr:spPr/></xdr:pic>"
    )


def _assert_xlsx_result(p: pathlib.Path, fill: str, occlusion: str) -> None:
    ir = evidence_spike.extract(p)
    assert evidence_ir.validation_errors(ir) == []
    cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
    confirmed = occlusion == vocab.OCCLUSION_CONFIRMED
    assert (cell.visibility == "hidden") is confirmed
    assert (cell.extension.get("visibility_reason") in {"occluded_by_shape", "occluded_by_picture"}) is confirmed
    (cover,) = [r for r in _covers(ir) if r.target_id == cell.element_id]
    assert (cover.extension["fill"], cover.extension["occlusion"]) == (fill, occlusion)
    assert any(r.type == "overlaps" and r.source_id == cover.source_id for r in ir.relations)


@pytest.mark.parametrize("case", sorted(_SHAPE_FILLS))
def test_xlsx_shape_fill_kinds(tmp_path, case):
    fill_xml, fill, occlusion = _SHAPE_FILLS[case]
    _assert_xlsx_result(_xlsx_with_drawing(tmp_path, _xlsx_shape(fill_xml)), fill, occlusion)


def test_xlsx_filled_shape_with_text_still_occludes(tmp_path):
    p = _xlsx_with_drawing(tmp_path, _xlsx_shape(_SOLID, text="廃止"))
    _assert_xlsx_result(p, vocab.FILL_OPAQUE, vocab.OCCLUSION_CONFIRMED)


def test_xlsx_unfilled_shape_with_text_is_none_with_text(tmp_path):
    p = _xlsx_with_drawing(tmp_path, _xlsx_shape("<a:noFill/>", text="廃止"))
    _assert_xlsx_result(p, vocab.FILL_NONE_WITH_TEXT, vocab.OCCLUSION_NOT_OCCLUDING)
    ir = evidence_spike.extract(p)
    cell = next(el for el in ir.elements if el.type == "cell" and el.locator.cell_range == "A1")
    assert cell.extension["covered_by_text"]["text"] == "廃止"


def test_xlsx_theme_fill_reference_is_unknown(tmp_path):
    shape = _xlsx_shape("", style='<xdr:style><a:fillRef idx="1"/></xdr:style>')
    _assert_xlsx_result(_xlsx_with_drawing(tmp_path, shape), vocab.FILL_UNKNOWN, vocab.OCCLUSION_UNDETERMINED)


@pytest.mark.parametrize("case", sorted(_PICTURES))
def test_xlsx_picture_fill_kinds(tmp_path, monkeypatch, case):
    data, fill, occlusion = _PICTURES[case]
    _limit_pixels_for_oversized(monkeypatch, case)
    _assert_xlsx_result(_xlsx_with_drawing(tmp_path, _xlsx_picture(), media=data), fill, occlusion)


def test_xlsx_picture_with_alpha_mod_fix_is_translucent(tmp_path):
    blip = '<a:blip r:embed="rId1"><a:alphaModFix amt="40000"/></a:blip>'
    p = _xlsx_with_drawing(tmp_path, _xlsx_picture(blip))
    _assert_xlsx_result(p, vocab.FILL_TRANSLUCENT, vocab.OCCLUSION_NOT_OCCLUDING)


# ---- 画像の透過の判定（純関数） ----

_PNG_CASES = [
    (_image_builders.png(color_type=2), vocab.FILL_OPAQUE),
    (_image_builders.png(color_type=0), vocab.FILL_OPAQUE),
    (_image_builders.png(color_type=3), vocab.FILL_OPAQUE),
    (_image_builders.jpeg(), vocab.FILL_OPAQUE),
    (_image_builders.png(color_type=3, trns=True), vocab.FILL_TRANSPARENT_PNG),
    (_image_builders.png(color_type=2, trns=True), vocab.FILL_TRANSPARENT_PNG),
    (b"", vocab.FILL_UNKNOWN),
    (None, vocab.FILL_UNKNOWN),
    (b"GIF89a....", vocab.FILL_UNKNOWN),
    (b"\x89PNG\r\n\x1a\n", vocab.FILL_UNKNOWN),
    (_image_builders.png(color_type=2)[:30], vocab.FILL_UNKNOWN),
    (_image_builders.png(color_type=2)[:40], vocab.FILL_UNKNOWN),
    (_image_builders.png(color_type=6)[:-20], vocab.FILL_UNKNOWN),           # 画素データが途中で切れている
    (_image_builders.png(color_type=6, interlace=1), vocab.FILL_UNKNOWN),    # インターレースは画素を読まない
]


@pytest.mark.parametrize("data,expected", _PNG_CASES)
def test_image_fill_kind(data, expected):
    assert image_alpha.image_fill_kind(data) == expected


@pytest.mark.parametrize("filter_type", range(5))
@pytest.mark.parametrize("color_type,bit_depth", [(6, 8), (4, 8), (6, 16), (4, 16)])
def test_alpha_png_is_opaque_only_when_every_pixel_is_opaque(color_type, bit_depth, filter_type):
    """アルファ付きの PNG は、全画素のアルファが最大なら opaque、1 画素でも透過があれば transparent_png（どのフィルタでも同じ）。"""
    opaque = _image_builders.png(color_type=color_type, bit_depth=bit_depth, filter_type=filter_type)
    assert image_alpha.image_fill_kind(opaque) == vocab.FILL_OPAQUE
    for transparent_pixels in (1, 30, 64):                       # 先頭の画素・途中の行・全画素
        data = _image_builders.png(
            color_type=color_type, bit_depth=bit_depth, filter_type=filter_type, transparent_pixels=transparent_pixels)
        assert image_alpha.image_fill_kind(data) == vocab.FILL_TRANSPARENT_PNG


@pytest.mark.parametrize("filter_type", range(5))
def test_png_with_only_the_last_pixel_transparent_is_transparent(filter_type):
    data = _image_builders.png(color_type=6, filter_type=filter_type, transparent_last=1)
    assert image_alpha.image_fill_kind(data) == vocab.FILL_TRANSPARENT_PNG


def test_real_stamp_asset_has_transparent_pixels():
    asset = pathlib.Path(__file__).resolve().parents[2] / "fixtures/eval/excel_ja/assets/廃止スタンプ.png"
    assert image_alpha.image_fill_kind(asset.read_bytes()) == vocab.FILL_TRANSPARENT_PNG


@pytest.mark.parametrize("limit", ["MAX_PIXELS", "MAX_RAW_BYTES"])
def test_image_over_the_read_limit_is_unknown(monkeypatch, limit):
    data = _image_builders.opaque_rgba_png()
    assert image_alpha.image_fill_kind(data) == vocab.FILL_OPAQUE
    monkeypatch.setattr(image_alpha, limit, 10)
    assert image_alpha.image_fill_kind(data) == vocab.FILL_UNKNOWN


def test_image_with_a_corrupt_pixel_stream_is_unknown():
    data = bytearray(_image_builders.opaque_rgba_png())
    start = data.index(b"IDAT") + 4
    data[start:start + 8] = b"\x00" * 8
    assert image_alpha.image_fill_kind(bytes(data)) == vocab.FILL_UNKNOWN


def test_image_fill_kind_png_without_leading_header_is_unknown():
    data = b"\x89PNG\r\n\x1a\n" + _image_builders._chunk(b"IDAT", b"x")
    assert image_alpha.image_fill_kind(data) == vocab.FILL_UNKNOWN
