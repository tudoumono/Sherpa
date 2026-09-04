"""`arms/raster.py`（PDF→ページ画像のラスタ化ヘルパ）の単体テスト（DB不要）。

tesseract 直の `ocr` アーム撤去（2026-07-08・視覚読み取りは vision に一本化）に伴い、
`arms/ocr_arm.py` にあったラスタ化処理をこの共有モジュールへ移設した（唯一の呼び出し元は
`vision_arm`）。env 名は互換のため据え置き: `SHERPA_OCR_MAX_PAGES`・`SHERPA_OCR_MAX_PIXELS`。

pypdfium2 の到達性は偽モジュールで検証する。
"""
from __future__ import annotations

import sys
import types

from PIL import Image

from sherpa.ingest.arms import raster


def _fake_pdfium(monkeypatch):
    fake = types.ModuleType("pypdfium2")
    monkeypatch.setitem(sys.modules, "pypdfium2", fake)


def test_pdf_rasterize_available_false_without_pdfium(monkeypatch):
    monkeypatch.setitem(sys.modules, "pypdfium2", None)
    assert raster.pdf_rasterize_available() is False


def test_pdf_rasterize_available_true_with_pdfium(monkeypatch):
    _fake_pdfium(monkeypatch)
    assert raster.pdf_rasterize_available() is True


# ---- ラスタ化のピクセル上限クランプ（RV Med #5b・旧 ocr_arm から移設）----

def test_rasterize_pixel_cap_for_huge_page(monkeypatch):
    """巨大 MediaBox のページはラスタ化前に縮小倍率をクランプする（メモリ暴走防止）。"""
    monkeypatch.delenv("SHERPA_OCR_MAX_PIXELS", raising=False)        # 既定 4000px
    calls: list[float] = []

    class _HugePage:
        def get_size(self):
            return 20000.0, 10000.0

        def render(self, *, scale):
            calls.append(scale)
            return _bitmap()

    raster._rasterize_page(_HugePage())
    assert calls, "render が scale 付きで呼ばれること"
    longest_px = max(20000.0 * calls[0], 10000.0 * calls[0])
    assert longest_px <= 4000 + 1e-6                                  # 最長辺が上限にクランプされている


def test_rasterize_no_cap_for_normal_page(monkeypatch):
    """通常サイズのページは既定 dpi 相当の倍率のまま（クランプされない）。"""
    monkeypatch.delenv("SHERPA_OCR_MAX_PIXELS", raising=False)
    calls: list[float] = []

    class _NormalPage:
        def get_size(self):
            return 612.0, 792.0

        def render(self, *, scale):
            calls.append(scale)
            return _bitmap()

    raster._rasterize_page(_NormalPage())
    expected_zoom = raster._RASTERIZE_DPI / 72.0
    assert abs(calls[0] - expected_zoom) < 1e-9


def test_rasterize_pixel_cap_env_override(monkeypatch):
    """SHERPA_OCR_MAX_PIXELS でクランプしきい値を変更できる。"""
    monkeypatch.setenv("SHERPA_OCR_MAX_PIXELS", "1000")
    calls: list[float] = []

    class _Page:
        def get_size(self):
            return 612.0, 792.0

        def render(self, *, scale):
            calls.append(scale)
            return _bitmap()

    raster._rasterize_page(_Page())
    longest_px = max(612.0 * calls[0], 792.0 * calls[0])
    assert longest_px <= 1000 + 1e-6


# ---- ページ数上限（env SHERPA_OCR_MAX_PAGES）----

def test_max_pages_default_and_env_override(monkeypatch):
    monkeypatch.delenv("SHERPA_OCR_MAX_PAGES", raising=False)
    assert raster._max_pages() == 20                                  # 既定
    monkeypatch.setenv("SHERPA_OCR_MAX_PAGES", "3")
    assert raster._max_pages() == 3
    monkeypatch.setenv("SHERPA_OCR_MAX_PAGES", "not-a-number")
    assert raster._max_pages() == 20                                  # 不正値は既定へフォールバック


def _bitmap():
    image = Image.new("RGB", (2, 2), "white")
    return types.SimpleNamespace(to_pil=lambda: image, close=lambda: None)
