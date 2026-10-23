"""DrawingML の図形・画像の塗りを、遮蔽の判定に使う区分へ分ける純関数（pptx・xlsx・docx で共通）。

区分は `observation_vocab` の `FILL_*`。覆う割合の閾値は呼び出し側が見る（ここは塗りだけを判定する）。
設計: docs/proposals/2026-10-22-Office観測の共通の扱い.md §1.4
"""
from __future__ import annotations

from collections.abc import Callable
from xml.etree import ElementTree as ET

from .observation_vocab import (
    FILL_NONE_WITH_TEXT,
    FILL_OPAQUE,
    FILL_TRANSLUCENT,
    FILL_TRANSPARENT,
    FILL_TRANSPARENT_PNG,
    FILL_UNKNOWN,
)

_A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"
_R = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"

_ALPHA_FULL = 100000
_NON_SOLID_FILLS = ("gradFill", "pattFill", "blipFill", "grpFill")
_ALPHA_TRANSFORMS = ("alphaMod", "alphaOff")


def shape_fill_kind(sp_pr: ET.Element | None, style: ET.Element | None, *, has_text: bool) -> str:
    """図形の塗りの区分。`sp_pr` は `spPr`、`style` は同じ図形の `style`（無ければ None）。

    solidFill は alpha で opaque・translucent・transparent に分ける。文字の有無では塗りを読み飛ばさない。
    noFill・完全透過・塗りの指定が無い図形は、文字があれば none_with_text、無ければ transparent。
    グラデーション・パターン・画像の塗り・テーマの塗り参照（style の fillRef）は unknown。
    """
    if sp_pr is not None:
        if sp_pr.find(f"{_A}noFill") is not None:
            return _unfilled(has_text)
        solid = sp_pr.find(f"{_A}solidFill")
        if solid is not None:
            kind = _solid_fill_kind(solid)
            return _unfilled(has_text) if kind == FILL_TRANSPARENT else kind
        if any(sp_pr.find(f"{_A}{name}") is not None for name in _NON_SOLID_FILLS):
            return FILL_UNKNOWN
    fill_ref = style.find(f"{_A}fillRef") if style is not None else None
    if fill_ref is not None and (fill_ref.get("idx") or "").strip() != "0":
        return FILL_UNKNOWN
    return _unfilled(has_text)


# 覆う範囲が外接矩形と一致する図形のプリセット形状（これ以外のプリセットとカスタム形状は、外接矩形では覆う範囲を言えない）。
BBOX_PRESET_GEOMETRIES = frozenset({"rect"})


def explicit_geometry_covers_bounding_box(sp_pr: ET.Element | None) -> bool | None:
    """`spPr` に直接書かれた形状が、外接矩形と同一視してよいか。形状の指定が無ければ None。

    カスタム形状（`a:custGeom`）と、`BBOX_PRESET_GEOMETRIES` 以外のプリセット形状（ドーナツ・楕円・矢印・吹き出しなど）は False。
    """
    if sp_pr is None:
        return None
    if sp_pr.find(f"{_A}custGeom") is not None:
        return False
    geometry = sp_pr.find(f"{_A}prstGeom")
    return None if geometry is None else geometry.get("prst") in BBOX_PRESET_GEOMETRIES


def covers_bounding_box(sp_pr: ET.Element | None, *, picture: bool = False) -> bool:
    """図形・画像が覆う範囲を外接矩形と同一視してよいか。`sp_pr` は `spPr`、`picture` は画像か。

    形状の指定が直接あればそれで決める。指定が無い画像は矩形と見なす（形状が無くても画像は矩形で描画される）。
    指定が無い図形は覆う範囲を言えないので False（形状が無い図形は塗りが描画されない。レイアウトから形状を継承する
    プレースホルダーは、呼び出し側が継承元を解決して決める）。任意の形状の描画の解析まではしない。
    """
    explicit = explicit_geometry_covers_bounding_box(sp_pr)
    return picture if explicit is None else explicit


def picture_fill_kind(node: ET.Element, image_kind_of: Callable[[str | None, str | None], str]) -> str:
    """画像（`pic`）の塗りの区分。`node` の下の最初の `a:blip` を見る。

    blip が無ければ画像の中身を判断する材料が無いので unknown。alphaModFix は translucent（0 は transparent）、
    clrChange（色を透明にする）は transparent_png。それ以外は埋め込み画像の透過の有無
    （`image_kind_of(embed の関係ID, link の関係ID)`）で決める。
    """
    blip = node.find(f".//{_A}blip")
    if blip is None:
        return FILL_UNKNOWN
    alpha_fix = blip.find(f"{_A}alphaModFix")
    if alpha_fix is not None:
        amount = _int(alpha_fix.get("amt"), default=_ALPHA_FULL)
        if amount <= 0:
            return FILL_TRANSPARENT
        if amount < _ALPHA_FULL:
            return FILL_TRANSLUCENT
    if blip.find(f"{_A}clrChange") is not None:
        return FILL_TRANSPARENT_PNG
    return image_kind_of(blip.get(f"{_R}embed"), blip.get(f"{_R}link"))


def _unfilled(has_text: bool) -> str:
    return FILL_NONE_WITH_TEXT if has_text else FILL_TRANSPARENT


def _solid_fill_kind(solid: ET.Element) -> str:
    color = next(iter(solid), None)
    if color is None:
        return FILL_OPAQUE
    alpha = color.find(f"{_A}alpha")
    if alpha is not None:
        value = _int(alpha.get("val"), default=_ALPHA_FULL)
        if value <= 0:
            return FILL_TRANSPARENT
        return FILL_TRANSLUCENT if value < _ALPHA_FULL else FILL_OPAQUE
    if any(color.find(f"{_A}{name}") is not None for name in _ALPHA_TRANSFORMS):
        return FILL_UNKNOWN
    return FILL_OPAQUE


def _int(raw: str | None, *, default: int) -> int:
    try:
        return int(raw) if raw is not None else default
    except ValueError:
        return default
