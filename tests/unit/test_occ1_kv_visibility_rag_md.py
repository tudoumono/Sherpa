"""L4a（可視性・廃止表現の表示側・OCC-1のKV直列化）の実ファイル往復での受け入れ条件を検証する。

正典: `docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md` §2.3（key-value 表示形）・
§8.1（rag.md を正本にする・アンカー方式）。

`tests/unit/test_deprecation_marker_acceptance.py`（L6′）は構造（`EvidenceElement.visibility`／
`.lifecycle`／`.extension`）だけを見る受け入れハーネスで、生成された自然文・rag.md の文字列表現には
依存しないことを明言している（L4′ が直列化書式を key-value 化している最中だったため）。本ファイルは
その続き＝**直列化そのもの**を実ファイル（`office_md.build_derived()` の実往復）で固定する。
`tests/unit/test_evidence_render_kv.py` の合成要素（`EvidenceElement`を直接組み立てる）テストとは
独立に、実際の xlsx/docx/pptx から抽出した構造がここまで期待どおりに rag.md へ出ることを見る。
"""
from __future__ import annotations

import os
import re
import shutil
import tempfile
from pathlib import Path

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import pytest

from sherpa.ingest import office_md

_ROOT = Path(__file__).resolve().parents[2]
_DEP_INPUTS = _ROOT / "fixtures" / "eval" / "deprecation_markers" / "inputs"

# `visibility_reason`等の生enum語彙（rag.mdに出てはいけない）。
_RAW_ENUM_TOKENS = (
    "occluded_by_picture", "occluded_by_shape", "hidden_sheet", "very_hidden",
    "hidden_row", "hidden_column", "hidden_run", "hidden_slide_inherited", "hidden_slide",
    "off_slide", "occluded",
)


@pytest.fixture(scope="module")
def _derived_dir():
    """DEP-*フィクスチャを実際にbuild_derived()へ通し、生成されたderivedディレクトリを返す。"""
    for name in ("DEP-XLSX-MARKERS.xlsx", "DEP-DOCX-MARKERS.docx", "DEP-PPTX-MARKERS.pptx"):
        if not (_DEP_INPUTS / name).is_file():
            pytest.skip(f"fixture が無い環境: {_DEP_INPUTS / name}")
    d = tempfile.mkdtemp()
    wd = Path(d) / "world"
    wd.mkdir()
    for name in ("DEP-XLSX-MARKERS.xlsx", "DEP-DOCX-MARKERS.docx", "DEP-PPTX-MARKERS.pptx"):
        shutil.copy(_DEP_INPUTS / name, wd / name)
    dmd = Path(d) / "derived"
    rep = office_md.build_derived(wd, dmd)
    assert rep["rag_failed"] == 0, rep
    return dmd


def _rag_md(dmd: Path, rel: str) -> str:
    """`dmd` は md 層。`.rag.md` は rag 層（`dmd` の兄弟）に物理配置される（§8.1 三階層）。"""
    return (dmd.parent / "rag" / f"{rel}.rag.md").read_text(encoding="utf-8")


# ---- xlsx ----

def test_xlsx_strike_cell_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-XLSX-MARKERS.xlsx")
    assert "取り消し線: 「あり」" in md


def test_xlsx_occluded_by_picture_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-XLSX-MARKERS.xlsx")
    assert "可視性: 「画像に覆われている」" in md


def test_xlsx_hidden_row_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-XLSX-MARKERS.xlsx")
    assert "可視性: 「行が非表示」" in md


def test_xlsx_hidden_column_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-XLSX-MARKERS.xlsx")
    assert "可視性: 「列が非表示」" in md


# ---- docx ----

def test_docx_strike_kv_line_in_rag_md(_derived_dir):
    """単/二重取り消し線どちらも同じ`取り消し線: 「あり」`（幾何的事実だけを区別なく残す設計）。"""
    md = _rag_md(_derived_dir, "DEP-DOCX-MARKERS.docx")
    assert md.count("取り消し線: 「あり」") == 2


def test_docx_hidden_run_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-DOCX-MARKERS.docx")
    assert "可視性: 「非表示文字」" in md


# ---- pptx ----

def test_pptx_occluded_shape_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-PPTX-MARKERS.pptx")
    assert "可視性: 「図形に覆われている」" in md


def test_pptx_covered_by_text_kv_line_matches_front_text(_derived_dir):
    """前面テキスト「廃止」との重なりがそのまま`重なり:`行に出る（意味の断定はしない）。"""
    md = _rag_md(_derived_dir, "DEP-PPTX-MARKERS.pptx")
    assert "重なり: 「廃止」" in md


def test_pptx_off_slide_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-PPTX-MARKERS.pptx")
    assert "可視性: 「スライド範囲外」" in md


def test_pptx_hidden_slide_inherited_kv_line_in_rag_md(_derived_dir):
    md = _rag_md(_derived_dir, "DEP-PPTX-MARKERS.pptx")
    assert "可視性: 「非表示スライド内の要素」" in md


# ---- 生enumが一切出ないこと（3ファイル共通の受け入れ条件）----

@pytest.mark.parametrize("rel", ["DEP-XLSX-MARKERS.xlsx", "DEP-DOCX-MARKERS.docx", "DEP-PPTX-MARKERS.pptx"])
def test_no_raw_visibility_reason_enum_leaks_into_rag_md(_derived_dir, rel):
    md = _rag_md(_derived_dir, rel)
    for token in _RAW_ENUM_TOKENS:
        assert token not in md, f"{rel} に生enum {token!r} が漏れている"


# ---- rag.mdのアンカーがcitation完全性を保ったまま出ること（D1との整合）----

@pytest.mark.parametrize("rel", ["DEP-XLSX-MARKERS.xlsx", "DEP-DOCX-MARKERS.docx", "DEP-PPTX-MARKERS.pptx"])
def test_rag_md_has_anchors_and_jsonl_chunk_ids_match_1to1(_derived_dir, rel):
    import json as _json
    md = _rag_md(_derived_dir, rel)
    chunks = [_json.loads(line) for line in
              (_derived_dir.parent / "rag" / f"{rel}.rag_chunks.jsonl")
              .read_text(encoding="utf-8").splitlines()]
    anchor_ids = re.findall(r"^<!-- chunk:(\S+) -->$", md, flags=re.MULTILINE)
    assert set(anchor_ids) == {c["chunk_id"] for c in chunks}
    assert len(anchor_ids) == len(set(anchor_ids))


def test_hidden_sheet_visibility_propagates_to_all_records_of_that_sheet():
    """非表示シート（hidden_sheet/very_hidden）は sheet 要素がコンテナ型でレコード化されないため、
    伝播が無いと rag.md のどこにも現れない——「廃止した旧版シートを非表示にする」運用が検索で
    見分けられなくなる（検収是正）。ES が読むアンカー本文へ、そのシートの**全レコード**に付き
    （チャンクは断片単独で自己完結する契約）、可視シートには付かないことを固定する。"""
    import re
    from sherpa.ingest import evidence_spike, evidence_render
    ir = evidence_spike.extract("fixtures/eval/deprecation_markers/inputs/DEP-XLSX-MARKERS.xlsx")
    r = evidence_render.render(ir, source_name="DEP-XLSX-MARKERS.xlsx")
    parts = re.split(r"<!-- chunk:(rag-chunk:[0-9a-f]+) -->", r.markdown)
    bodies = dict(zip(parts[1::2], parts[2::2]))
    for c in r.chunks:
        sheet = ((c.get("region_context") or {}).get("sheet")
                 or (c.get("document_context") or {}).get("sheet"))
        body = bodies[c["chunk_id"]]
        if sheet == "旧版":
            assert "シートの可視性: 「非表示のシートにあります」" in body
        elif sheet == "内部退避":
            assert "完全非表示のシートにあります" in body
        elif sheet == "対象":
            assert "シートの可視性" not in body
    # 生の enum（hidden_sheet/very_hidden）は出さない
    assert "hidden_sheet" not in r.markdown and "very_hidden" not in r.markdown
