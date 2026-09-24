"""L3（可視性・廃止表現の全形式展開・OCC-1）の受け入れ条件を検証する構造ベースのハーネス。

正典: `docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md` §2（提案A・受け入れ条件は §2.3）。
設計思想: 「意味の断定はしない」共通思想（ここでは値・visibility・
lifecycle・extension の構造だけを見る。生成された自然文・rag.md の文字列表現には一切依存しない
——L4′ が直列化書式を key-value 化している最中で、文字列に依存すると無関係に壊れるため）。

判定対象: `sherpa.ingest.evidence_spike.extract()` が返す `EvidenceElement.visibility`／
`.lifecycle`／`.extension`（`visibility_reason`・`covered_by_text`・`occluded_by` 等のキー）。

L3 は本レーンの作業時点で他レーンにより並行実装が進行中だった。実際に `evidence_spike.py` を都度
実行して確認したところ、一部の表現（xlsx の非表示シート・画像による覆い／docx の隠し文字・変更履歴
削除／pptx の覆い・スライド外配置・非表示スライド・covered_by_text）は既に構造的に確認できたため
素の assert にした。取り消し線（xlsx の `font.strike`／docx の `w:strike`・`w:dstrike`）と xlsx の
非表示行/列は当初 visibility/lifecycle/extension に反映されていなかったため `pytest.mark.xfail` にして
いたが、L3 の実装完了（`evidence_spike._adapt_document_ir` が xlsx の `cell` 要素へ非表示行/列・
取り消し線を反映・docx の `strike:N` へ `visibility_reason="strike"` を付与）に伴いマーカーを外した。

フィクスチャ: `fixtures/eval/deprecation_markers/inputs/DEP-*`
（`fixtures/eval/deprecation_markers/generate_fixtures.py` で決定的に再生成可能）に加え、
実データ（`fixtures/eval/excel_ja/inputs/JPX-011.xlsx`＝廃止スタンプ画像でセルを覆う実例）も使う。
"""
from __future__ import annotations

from pathlib import Path

import pytest

from sherpa.ingest import evidence_spike

_ROOT = Path(__file__).resolve().parents[2]
_DEP_INPUTS = _ROOT / "fixtures" / "eval" / "deprecation_markers" / "inputs"
_XLSX_JPX011 = _ROOT / "fixtures" / "eval" / "excel_ja" / "inputs" / "JPX-011.xlsx"


def _elements(path: Path):
    if not path.is_file():
        pytest.skip(f"fixture が無い環境: {path}")
    return evidence_spike.extract(path).elements


def _cell(elements, sheet: str, cell_range: str):
    for el in elements:
        if el.type == "cell" and el.locator.sheet == sheet and el.locator.cell_range == cell_range:
            return el
    raise AssertionError(f"cell not found: {sheet}!{cell_range}")


def _shape(elements, slide: int, value_contains: str):
    for el in elements:
        if el.type == "shape" and el.locator.slide == slide and value_contains in (el.value or ""):
            return el
    raise AssertionError(f"shape not found: slide={slide} containing {value_contains!r}")


def _structurally_marked(el) -> bool:
    """要素が「通常の可視・現行本文」から構造的に区別されているか（値・生成文字列は見ない）。

    L3 が最終的にどのキー／reason 文字列を選ぶかは未確定なので、特定の reason 値には固定しない
    （生成された文字列に依存しないこと、という受け入れ条件どおり）。
    """
    if el.visibility != "visible":
        return True
    if el.lifecycle != "active":
        return True
    if el.extension.get("visibility_reason"):
        return True
    if el.extension.get("covered_by_text"):
        return True
    if el.extension.get("occluded_by"):
        return True
    return False


# ---- xlsx ----

def test_xlsx_hidden_sheet_is_marked():
    """非表示シート（`state="hidden"`）は sheet 要素の visibility="hidden" として出る。"""
    elements = _elements(_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx")
    sheet = next(el for el in elements if el.type == "sheet" and el.locator.sheet == "旧版")
    assert sheet.visibility == "hidden"
    assert sheet.extension.get("visibility_reason") == "hidden_sheet"


def test_xlsx_very_hidden_sheet_is_marked():
    """完全非表示シート（`state="veryHidden"`）は reason="very_hidden" で区別される。"""
    elements = _elements(_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx")
    sheet = next(el for el in elements if el.type == "sheet" and el.locator.sheet == "内部退避")
    assert sheet.visibility == "hidden"
    assert sheet.extension.get("visibility_reason") == "very_hidden"


def test_xlsx_cell_covered_by_image_is_marked():
    """不透明画像（`廃止スタンプ.png` 相当）でセルを覆うと、覆われたセル要素が hidden になる。"""
    elements = _elements(_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx")
    cell = _cell(elements, "対象", "B3")
    assert cell.visibility == "hidden"
    assert cell.extension.get("visibility_reason") == "occluded_by_picture"


def test_xlsx_cell_covered_by_image_real_fixture():
    """実データ（JPX-011.xlsx・廃止スタンプ画像でセルを覆う実例）でも同じことを確認する。"""
    elements = _elements(_XLSX_JPX011)
    cell = _cell(elements, "帳票項目", "B2")
    assert cell.visibility == "hidden"
    assert cell.extension.get("visibility_reason") == "occluded_by_picture"


def test_xlsx_strikethrough_cell_is_marked():
    elements = _elements(_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx")
    cell = _cell(elements, "対象", "B2")
    assert _structurally_marked(cell)


def test_xlsx_hidden_row_is_marked():
    elements = _elements(_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx")
    cell = _cell(elements, "対象", "B4")
    assert _structurally_marked(cell)


def test_xlsx_hidden_column_is_marked():
    elements = _elements(_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx")
    cell = _cell(elements, "対象", "C2")
    assert _structurally_marked(cell)


# ---- docx ----

def test_docx_hidden_run_is_marked():
    """隠し文字（`w:vanish`）は type=hidden_text・visibility=hidden・reason=hidden_run になる。"""
    elements = _elements(_DEP_INPUTS / "DEP-DOCX-MARKERS.docx")
    hidden = next(el for el in elements if el.type == "hidden_text")
    assert hidden.visibility == "hidden"
    assert hidden.extension.get("visibility_reason") == "hidden_run"


def test_docx_deleted_run_is_marked():
    """変更履歴削除（`w:del`）は type=deleted_text・lifecycle=deleted になる。"""
    elements = _elements(_DEP_INPUTS / "DEP-DOCX-MARKERS.docx")
    deleted = next(el for el in elements if el.type == "deleted_text")
    assert deleted.lifecycle == "deleted"


def test_docx_strike_run_is_marked():
    elements = _elements(_DEP_INPUTS / "DEP-DOCX-MARKERS.docx")
    strikes = [el for el in elements if el.type == "strike_text"]
    assert strikes, "strike_text 要素が見つからない"
    assert all(_structurally_marked(el) for el in strikes)


# ---- pptx ----

def test_pptx_occluded_shape_is_marked():
    """無地塗りの前面シェイプに覆われたテキストシェイプは visibility=hidden・reason=occluded になる。"""
    elements = _elements(_DEP_INPUTS / "DEP-PPTX-MARKERS.pptx")
    shape = _shape(elements, 1, "旧機能C")
    assert shape.visibility == "hidden"
    assert shape.extension.get("visibility_reason") == "occluded"


def test_pptx_covered_by_text_is_marked():
    """前面のテキストシェイプに重なられた（取り消し線的表現）背面シェイプは
    extension["covered_by_text"] を持つ（visibilityはvisibleのまま＝意味の断定はしない設計）。"""
    elements = _elements(_DEP_INPUTS / "DEP-PPTX-MARKERS.pptx")
    shape = _shape(elements, 2, "旧料金体系")
    assert shape.extension.get("covered_by_text") is not None


def test_pptx_off_slide_shape_is_marked():
    """スライド範囲外に配置されたシェイプは visibility=hidden・reason=off_slide になる。"""
    elements = _elements(_DEP_INPUTS / "DEP-PPTX-MARKERS.pptx")
    shape = _shape(elements, 3, "旧仕様メモ")
    assert shape.visibility == "hidden"
    assert shape.extension.get("visibility_reason") == "off_slide"


def test_pptx_hidden_slide_is_marked():
    """非表示スライド（`p:sld/@show="0"`）は slide 要素が hidden になり、含まれるシェイプへ伝播する。"""
    elements = _elements(_DEP_INPUTS / "DEP-PPTX-MARKERS.pptx")
    slide = next(el for el in elements if el.type == "slide" and el.locator.slide == 4)
    assert slide.visibility == "hidden"
    assert slide.extension.get("visibility_reason") == "hidden_slide"
    shape = _shape(elements, 4, "旧業務フロー")
    assert shape.visibility == "hidden"
