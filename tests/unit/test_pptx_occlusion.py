"""pptx 幾何オクルージョン（A5・§5.5 の簡略版）の単体テスト。

python-pptx に依存せず、手組みの最小 pptx（zipfile で XML を直接書く）で検証する。
EMU 単位: 1 inch = 914400 EMU。テキスト shape は幅 4000000 x 高さ 2000000 EMU を基準に置く。

正典: docs/proposals/2026-07-07-MD化多アーム統合.md A5 ／ docs/11-Office変換.md §5.5。
2026-07-12 ユーザー決定＝簡略版（人手確認UIなし・MD本文へマーカー行を自動出力するだけ）。
"""
from __future__ import annotations

import pathlib
import tempfile
import zipfile

from sherpa.ingest import office_md

_NS = (
    'xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
    'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"'
)

# 基準テキスト shape: (0,0) - (4000000, 2000000) EMU
_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY = 0, 0, 4000000, 2000000


def _zip(path, entries: dict):
    with zipfile.ZipFile(path, "w") as z:
        for name, data in entries.items():
            z.writestr(name, data)


def _sp_text(text: str, x=_TEXT_X0, y=_TEXT_Y0, cx=_TEXT_CX, cy=_TEXT_CY, rot: str | None = None,
             with_xfrm: bool = True) -> str:
    """テキストを持つ p:sp（幾何判定対象のテキスト shape）。"""
    rot_attr = f' rot="{rot}"' if rot is not None else ""
    xfrm = (f'<a:xfrm{rot_attr}><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
            if with_xfrm else "")
    return f"""<p:sp>
  <p:spPr>{xfrm}</p:spPr>
  <p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody>
 </p:sp>"""


def _sp_solid_fill(x, y, cx, cy, rot: str | None = None, no_fill: bool = False) -> str:
    """テキストを持たない塗り潰し図形（occluder 候補）。"""
    rot_attr = f' rot="{rot}"' if rot is not None else ""
    fill = "<a:noFill/>" if no_fill else "<a:solidFill><a:srgbClr val=\"FFFFFF\"/></a:solidFill>"
    xfrm = f'<a:xfrm{rot_attr}><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
    return f"""<p:sp>
  <p:spPr>{xfrm}{fill}</p:spPr>
 </p:sp>"""


def _sp_text_occluder(text: str, x, y, cx, cy) -> str:
    """テキストを持つ前面図形（コンテンツはコンテンツを隠さない扱いの検証用）。"""
    xfrm = f'<a:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
    return f"""<p:sp>
  <p:spPr>{xfrm}</p:spPr>
  <p:txBody><a:p><a:r><a:t>{text}</a:t></a:r></a:p></p:txBody>
 </p:sp>"""


def _pic(x, y, cx, cy, rot: str | None = None) -> str:
    rot_attr = f' rot="{rot}"' if rot is not None else ""
    xfrm = f'<a:xfrm{rot_attr}><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></a:xfrm>'
    return f"""<p:pic>
  <p:spPr>{xfrm}</p:spPr>
 </p:pic>"""


def _slide_xml(shapes: str) -> str:
    return f"""<?xml version="1.0"?>
<p:sld {_NS}>
 <p:cSld><p:spTree>
  {shapes}
 </p:spTree></p:cSld>
</p:sld>"""


def _md_for_slide(shapes: str) -> str:
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "a.pptx"
    _zip(p, {"ppt/slides/slide1.xml": _slide_xml(shapes)})
    md = office_md.to_markdown(p)
    assert md is not None
    return md


def test_full_coverage_marks_hidden_and_keeps_text():
    """完全被覆（solidFill 矩形がテキストの真上・100%）→ マーカー付与＋テキストは出力に残る。"""
    text = _sp_text("隠れたテキストA")
    occluder = _sp_solid_fill(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY)  # 同一bbox=完全被覆・後続=前面
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER in md
    assert "隠れたテキストA" in md


def test_partial_coverage_no_marker():
    """部分被覆（50%）→ マーカーなし。"""
    text = _sp_text("半分だけ隠れたB")
    # x を半分ずらして重なりを50%にする
    occluder = _sp_solid_fill(_TEXT_X0 + _TEXT_CX // 2, _TEXT_Y0, _TEXT_CX, _TEXT_CY)
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER not in md
    assert "半分だけ隠れたB" in md


def test_text_occluder_does_not_hide():
    """前面図形がテキストを持つ → マーカーなし（コンテンツはコンテンツを隠さない扱い）。"""
    text = _sp_text("見えるはずのC")
    occluder = _sp_text_occluder("前面のテキスト", _TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY)
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER not in md
    assert "見えるはずのC" in md
    assert "前面のテキスト" in md


def test_nofill_occluder_does_not_hide():
    """前面図形が noFill → マーカーなし。"""
    text = _sp_text("隠れないD")
    occluder = _sp_solid_fill(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY, no_fill=True)
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER not in md
    assert "隠れないD" in md


def test_rotated_text_shape_no_marker():
    """テキスト shape 側に rot 付き → 幾何判定に参加しない＝マーカーなし。"""
    text = _sp_text("回転E", rot="600000")
    occluder = _sp_solid_fill(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY)
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER not in md
    assert "回転E" in md


def test_rotated_occluder_no_marker():
    """occluder 側に rot 付き → 幾何判定に参加しない＝マーカーなし。"""
    text = _sp_text("回転occluderF")
    occluder = _sp_solid_fill(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY, rot="600000")
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER not in md
    assert "回転occluderF" in md


def test_background_solid_fill_no_marker():
    """背面の solidFill（z順が逆＝occluder がテキストより前に出現）→ マーカーなし。"""
    occluder = _sp_solid_fill(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY)
    text = _sp_text("背面被覆なしG")
    md = _md_for_slide(occluder + text)   # occluder が先＝背面
    assert office_md._HIDDEN_MARKER not in md
    assert "背面被覆なしG" in md


def test_missing_xfrm_text_extracted_no_marker():
    """xfrm 欠落のテキスト shape → テキストは抽出される・マーカーなし。"""
    text = _sp_text("xfrmなしH", with_xfrm=False)
    occluder = _sp_solid_fill(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY)
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER not in md
    assert "xfrmなしH" in md


def test_broken_xml_falls_back_to_flat_extraction():
    """壊れたXML → 例外にせず現行と同じフラット抽出にフォールバック（テキストは失われない）。"""
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "a.pptx"
    broken = f"""<?xml version="1.0"?>
<p:sld {_NS}>
 <p:cSld><p:spTree><a:t>壊れていても拾えるテキストI</a:t></p:spTree></p:cSld>
</p:sld>"""
    _zip(p, {"ppt/slides/slide1.xml": broken})
    md = office_md.to_markdown(p)
    assert md is not None
    assert "壊れていても拾えるテキストI" in md
    assert office_md._HIDDEN_MARKER not in md


def test_pic_occluder_hides_text():
    """前面図形が p:pic（画像）→ 完全被覆ならマーカー付与（仕様3(b)）。"""
    text = _sp_text("画像に隠れるK")
    occluder = _pic(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY)
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER in md
    assert "画像に隠れるK" in md


def test_rotated_pic_occluder_no_marker():
    """p:pic に rot 付き → 幾何判定に参加しない＝マーカーなし。"""
    text = _sp_text("画像回転で隠れないL")
    occluder = _pic(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY, rot="600000")
    md = _md_for_slide(text + occluder)
    assert office_md._HIDDEN_MARKER not in md
    assert "画像回転で隠れないL" in md


def test_group_shape_text_extracted_without_geometry_check():
    """p:grpSp 内はテキストのみ再帰抽出（幾何判定はしない＝occluder候補にも被occlude対象にもならない）。"""
    inner_text = _sp_text("グループ内本文M")
    group = f"""<p:grpSp>
  <p:grpSpPr><a:xfrm><a:off x="0" y="0"/><a:ext cx="1000000" cy="1000000"/>
   <a:chOff x="0" y="0"/><a:chExt cx="1000000" cy="1000000"/></a:xfrm></p:grpSpPr>
  {inner_text}
 </p:grpSp>"""
    occluder = _sp_solid_fill(_TEXT_X0, _TEXT_Y0, _TEXT_CX, _TEXT_CY)
    md = _md_for_slide(group + occluder)
    assert "グループ内本文M" in md
    assert office_md._HIDDEN_MARKER not in md


def test_normal_slide_matches_current_flat_output():
    """通常スライド（隠しなし）→ 出力が現行実装（フラット抽出）と完全一致する回帰確認。"""
    # 既存 test_office_md.py の _PPTX_SLIDE と同型（spTree 直下に裸の a:t を置く非現実的な最小形）
    d = tempfile.mkdtemp()
    p = pathlib.Path(d) / "a.pptx"
    slide = f"""<?xml version="1.0"?>
<p:sld {_NS}>
 <p:cSld><p:spTree><a:t>スライド本文XYZ</a:t></p:spTree></p:cSld>
</p:sld>"""
    _zip(p, {"ppt/slides/slide1.xml": slide})
    md = office_md.to_markdown(p)
    assert md is not None
    assert "## スライド 1" in md
    assert "スライド本文XYZ" in md
    assert office_md._HIDDEN_MARKER not in md

    # 通常の shape 構造（隠しなし）でも現行と同じ出力形式であることを確認
    text_only = _sp_text("通常のテキストJ")
    md2 = _md_for_slide(text_only)
    assert md2 == "## スライド 1\n\n通常のテキストJ"
