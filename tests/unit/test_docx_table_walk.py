"""`ooxml_arm._docx_table_walk` の単体テスト（DB不要・`<w:tbl>` 要素を直接構築）。

次の契約を検証する:
- 開始位置が Word 実仕様の表最大列数（`_DOCX_MAX_TABLE_COLUMNS`=63）を超えるセルは、63列へ座標を
  丸めず**表として出さない**（`flags` に `"docx_column_overflow_dropped"`）——複数セルを同じ丸め後
  座標へ衝突させて値/row_span 加算を壊さないため。開始位置は範囲内だが `column_span` が63列を
  超えて伸びるセルは、63列で止まるよう `column_span` だけをクランプする
  （`flags` に `"docx_column_span_clamped"`・値そのものは失わない）。
- `w:vMerge` 継続セルが本来持たないはずの可視本文を持つ不整形 OOXML でも、その本文を起点セルへ
  改行連結して残す（silent-drop 防止）。`flags` に `"docx_vmerge_text_merged"` を積む。
"""
from __future__ import annotations

from xml.etree import ElementTree as ET

from sherpa.ingest.arms import ooxml_arm

_W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def _tbl(xml: str):
    wrapped = f'<w:tbl xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">{xml}</w:tbl>'
    return ET.fromstring(wrapped)


def test_normal_table_has_no_flags():
    tbl = _tbl(
        "<w:tr><w:tc><w:p><w:r><w:t>a</w:t></w:r></w:p></w:tc>"
        "<w:tc><w:p><w:r><w:t>b</w:t></w:r></w:p></w:tc></w:tr>")
    cells, nested, cell_paras, flags = ooxml_arm._docx_table_walk(tbl)
    assert [c.text for c in cells] == ["a", "b"]
    assert flags == []


def test_huge_gridspan_clamps_column_span_when_start_in_range():
    """開始位置（列1）は範囲内なので落とさず、`column_span` だけ63列で止まるようクランプする。"""
    tbl = _tbl(
        '<w:tr><w:tc><w:tcPr><w:gridSpan w:val="999999999"/></w:tcPr>'
        "<w:p><w:r><w:t>見出し</w:t></w:r></w:p></w:tc></w:tr>")
    cells, _nested, _cell_paras, flags = ooxml_arm._docx_table_walk(tbl)
    assert len(cells) == 1
    assert cells[0].column == 1
    assert cells[0].column_span == ooxml_arm._DOCX_MAX_TABLE_COLUMNS
    assert "docx_column_span_clamped" in flags
    assert "docx_column_overflow_dropped" not in flags


def test_huge_gridbefore_drops_cell_instead_of_colliding_at_max_column():
    """開始位置自体が63列を超えるセルは、63列へ座標を丸めずに落とす（他セルとの座標衝突を避ける）。"""
    tbl = _tbl(
        '<w:tr><w:trPr><w:gridBefore w:val="999999999"/></w:trPr>'
        "<w:tc><w:p><w:r><w:t>後方セル</w:t></w:r></w:p></w:tc></w:tr>")
    cells, _nested, _cell_paras, flags = ooxml_arm._docx_table_walk(tbl)
    assert cells == []
    assert "docx_column_overflow_dropped" in flags
    assert "docx_column_span_clamped" not in flags


def test_64_cells_column_63_survives_column_64_dropped():
    """64個の1セル幅 `<w:tc>` を並べると、63列目までは全セルが残り、64列目だけが落ちて flags が立つ
    （座標衝突で64列目が63列目の値を上書きすることはない）。"""
    tcs = "".join(f"<w:tc><w:p><w:r><w:t>V{i}</w:t></w:r></w:p></w:tc>" for i in range(1, 65))
    tbl = _tbl(f"<w:tr>{tcs}</w:tr>")
    cells, _nested, _cell_paras, flags = ooxml_arm._docx_table_walk(tbl)
    by_col = {c.column: c.text for c in cells}
    assert by_col.get(63) == "V63"
    assert 64 not in by_col
    assert len(cells) == 63
    assert "docx_column_overflow_dropped" in flags


def test_vmerge_continuation_visible_text_merged_into_anchor():
    tbl = _tbl(
        '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
        "<w:p><w:r><w:t>起点</w:t></w:r></w:p></w:tc></w:tr>"
        '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr>'
        "<w:p><w:r><w:t>継続セルの可視本文</w:t></w:r></w:p></w:tc></w:tr>")
    cells, _nested, _cell_paras, flags = ooxml_arm._docx_table_walk(tbl)
    assert len(cells) == 1                                  # 継続セル自体は要素を作らない（規約どおり）
    assert cells[0].row_span == 2
    assert cells[0].text == "起点\n継続セルの可視本文"        # silent-drop せず改行連結
    assert "docx_vmerge_text_merged" in flags


def test_vmerge_continuation_without_text_produces_no_flag():
    tbl = _tbl(
        '<w:tr><w:tc><w:tcPr><w:vMerge w:val="restart"/></w:tcPr>'
        "<w:p><w:r><w:t>起点</w:t></w:r></w:p></w:tc></w:tr>"
        '<w:tr><w:tc><w:tcPr><w:vMerge/></w:tcPr><w:p/></w:tc></w:tr>')
    cells, _nested, _cell_paras, flags = ooxml_arm._docx_table_walk(tbl)
    assert len(cells) == 1
    assert cells[0].row_span == 2
    assert cells[0].text == "起点"
    assert flags == []
