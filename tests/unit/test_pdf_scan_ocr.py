"""文字の層が無いスキャン PDF: OCR が有効なら失敗にせず人向け MD に知らせを出し、無効なら従来どおり変換失敗のまま。

設計: docs/design/rag.md「人向け MD と RAG 正本の作り分け（マージの実際）」
"""
from __future__ import annotations

import json
import logging
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from pathlib import Path  # noqa: E402

import pytest  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

from sherpa.ingest import office_md  # noqa: E402

_EXPECTED_MD = (
    "（文字の層が無いスキャン PDF のため、ここには本文がありません。本文は画像の読み取り（OCR）で検索できます"
    "（読み取りが終わるまでは検索に出ません）。内容は原本で確認してください）\n\n"
    "## ページ 1\n\n（画像だけのページです）"
)


@pytest.fixture(autouse=True)
def _arms(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_ARMS", "ooxml,pdf_text")
    monkeypatch.setenv("SHERPA_MD_WORKERS", "1")
    monkeypatch.delenv(office_md._OCR_ENABLED_ENV, raising=False)


@pytest.fixture
def world(tmp_path):
    """文字の層が無い 1 ページのスキャン PDF を 1 つ置いた原本フォルダと、派生物の出力先 `(原本, md/)`。"""
    src = tmp_path / "src"
    src.mkdir()
    image = Image.new("1", (600, 400), 1)
    draw = ImageDraw.Draw(image)
    draw.rectangle((40, 40, 300, 200), outline=0, width=4)
    draw.text((60, 80), "SCAN TEXT", fill=0)
    image.save(src / "scan.pdf", "PDF")
    return src, tmp_path / "out" / "md"


def _meta(md_dir: Path) -> dict:
    return json.loads((md_dir / "scan.pdf.md.meta.json").read_text(encoding="utf-8"))


def _manifest(md_dir: Path) -> dict:
    return json.loads((md_dir.parent / "ir" / "scan.pdf.derived.json").read_text(encoding="utf-8"))


def test_scan_pdf_with_ocr_is_converted_with_notice(world):
    src, md_dir = world
    rep = office_md.build_derived(src, md_dir)

    assert rep["failed"] == 0 and rep["conversion_failures"] == [] and rep["converted"] == 1
    assert (md_dir / "scan.pdf.md").read_text(encoding="utf-8") == _EXPECTED_MD
    meta = _meta(md_dir)
    assert meta["arm"] == "pdf_text" and meta["method"] == "pdf_scan_ocr"
    assert "text_layer=none" in meta["notes"] and "ocr_inputs=1" in meta["notes"]
    assert office_md.failure_notice_reason(meta) is None
    assert _manifest(md_dir)["asset_versions"]["human_md"] == office_md._current_pdf_human_md_sig()
    assert office_md.rag_sidecars_missing(src, md_dir) is False


def test_scan_pdf_without_ocr_stays_failed_and_does_not_loop(world, monkeypatch):
    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "0")
    src, md_dir = world
    rep = office_md.build_derived(src, md_dir)

    assert rep["failed"] == 1 and rep["converted"] == 0
    assert rep["conversion_failures"] == [{"doc": "scan.pdf", "reason": "conversion_failed"}]
    assert not (md_dir / "scan.pdf.md").exists()
    assert _manifest(md_dir)["asset_versions"]["human_md"] == office_md._current_pdf_human_md_sig()
    assert office_md.rag_sidecars_missing(src, md_dir) is False
    assert office_md.rag_sidecars_missing(src, md_dir) is False


def test_scan_pdf_failed_before_this_version_is_rebuilt_once(world):
    """失敗として記録された旧状態（`.md` 無し・人向け MD の版の記録なし）は一度だけ作り直しの対象になり、作り直し後は繰り返さない。"""
    src, md_dir = world
    office_md.build_derived(src, md_dir)
    (md_dir / "scan.pdf.md").unlink()
    (md_dir / "scan.pdf.md.meta.json").unlink()
    manifest_path = md_dir.parent / "ir" / "scan.pdf.derived.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest.pop("asset_versions")
    manifest["sidecars"] = [s for s in manifest["sidecars"] if not s.startswith(".md")]
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False), encoding="utf-8")

    assert office_md.rag_sidecars_missing(src, md_dir) is True
    rep = office_md.build_derived(src, md_dir)
    assert rep["failed"] == 0 and rep["converted"] == 1
    assert office_md.rag_sidecars_missing(src, md_dir) is False


def test_second_build_restores_from_cache_and_ocr_off_misses_it(world, monkeypatch, caplog):
    src, md_dir = world
    office_md.build_derived(src, md_dir)
    first = (md_dir / "scan.pdf.md").read_bytes()

    with caplog.at_level(logging.INFO, logger=office_md._log.name):
        rep = office_md.build_derived(src, md_dir)
    assert rep["converted"] == 1 and rep["failed"] == 0
    assert any("キャッシュ復元" in r.getMessage() for r in caplog.records)
    assert (md_dir / "scan.pdf.md").read_bytes() == first

    monkeypatch.setenv(office_md._OCR_ENABLED_ENV, "0")
    caplog.clear()
    with caplog.at_level(logging.INFO, logger=office_md._log.name):
        rep = office_md.build_derived(src, md_dir)
    assert rep["failed"] == 1 and rep["converted"] == 0
    assert not any("キャッシュ復元" in r.getMessage() for r in caplog.records)
    assert not (md_dir / "scan.pdf.md").exists()

