"""PDF→ページ画像のラスタ化ヘルパ（`vision`＝VLM 視覚読み取りが使う共有ユーティリティ）。

**tesseract の `ocr` アーム撤去（2026-07-08・視覚読み取りは vision（VLM）に一本化）**に伴い、
`arms/ocr_arm.py` にあったラスタ化処理（PDFium でページを画像化・ページ数上限・ピクセル上限
クランプ）をこの共有モジュールへ移設した。PyMuPDF/fitz はライセンス境界を単純にするため採用しない。
唯一の呼び出し元は現在 `vision_arm`（テキスト層ゼロの
PDF をページ画像化して VLM に渡す経路）。env 名は互換のため据え置く（改名しない）:
`SHERPA_OCR_MAX_PAGES`（ページ数上限）・`SHERPA_OCR_MAX_PIXELS`（ラスタ化後の最長辺ピクセル上限）。

決定的（同一入力・同一環境で同一出力）。ネットワーク I/O・LLM 呼び出しは行わない。
"""
from __future__ import annotations

import os

_DEFAULT_MAX_PAGES = 20               # PDF のラスタ化対象ページ上限（暴走防止）
_DEFAULT_MAX_PIXEL_SIDE = 4000        # ラスタ化後の最長辺の上限（px・巨大 MediaBox でのメモリ暴走防止・RV Med #5b）
_RASTERIZE_DPI = 200                  # PDF→画像の解像度（固定＝決定的・ただし最長辺は上限でクランプされうる）


def pdf_rasterize_available() -> bool:
    """PDF をページ画像へラスタライズできるか（pypdfium2 の到達性）。"""
    try:
        import pypdfium2  # noqa: F401
        return True
    except Exception:
        return False


def _max_pages() -> int:
    """ラスタ化対象ページ上限（env `SHERPA_OCR_MAX_PAGES`・不正/未設定は既定 20）。"""
    raw = os.environ.get("SHERPA_OCR_MAX_PAGES")
    if not raw:
        return _DEFAULT_MAX_PAGES
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_MAX_PAGES
    return v if v > 0 else _DEFAULT_MAX_PAGES


def _max_pixel_side() -> int:
    """ラスタ化後の最長辺の上限（px・env `SHERPA_OCR_MAX_PIXELS`・不正/未設定は既定 4000・RV Med #5b）。"""
    raw = os.environ.get("SHERPA_OCR_MAX_PIXELS")
    if not raw:
        return _DEFAULT_MAX_PIXEL_SIDE
    try:
        v = int(raw)
    except ValueError:
        return _DEFAULT_MAX_PIXEL_SIDE
    return v if v > 0 else _DEFAULT_MAX_PIXEL_SIDE


def _rasterize_page(page, dpi: int = _RASTERIZE_DPI):
    """ページを固定 dpi でラスタライズし、最長辺が `_max_pixel_side()` を超えたら縮小倍率をクランプする
    （巨大 MediaBox でのメモリ暴走防止・RV Med #5b）。決定的（同一入力・同一環境で同一出力）。
    """
    width, height = page.get_size()
    zoom = dpi / 72.0                                        # PDFium も PDF ポイント（72dpi）基準
    longest = max(width, height) * zoom
    cap = _max_pixel_side()
    if longest > cap:
        zoom *= cap / longest                                # 縮小倍率をクランプ（アスペクト比維持）
    bitmap = page.render(scale=zoom)
    try:
        # PDFium の bitmap を閉じた後も使える独立した PIL 画像を返す。
        return bitmap.to_pil().copy()
    finally:
        bitmap.close()
