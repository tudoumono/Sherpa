"""PDF→ページ画像のラスタ化ヘルパ。`vision` アームが使う共有ユーティリティで、PDFium でページを画像化し、ページ数上限・ピクセル上限でクランプする。

env: `SHERPA_OCR_MAX_PAGES`（ページ数上限）。ラスタ化後の最長辺ピクセル上限は定数 `_MAX_PIXEL_SIDE`。決定的（同一入力・同一環境で同一出力）。ネットワーク I/O・LLM 呼び出しは行わない。
設計: docs/design/rag.md「アーム一覧」
"""
from __future__ import annotations

import os

_DEFAULT_MAX_PAGES = 20  # PDF のラスタ化対象ページ上限
_MAX_PIXEL_SIDE = 4000  # ラスタ化後の最長辺の上限（px・巨大 MediaBox でのメモリ暴走防止）
_RASTERIZE_DPI = 200  # PDF→画像の解像度（固定。最長辺は上限でクランプされうる）


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


def _rasterize_page(page, dpi: int = _RASTERIZE_DPI):
    """ページを固定 dpi でラスタライズし、最長辺が `_MAX_PIXEL_SIDE` を超えたら縮小倍率をクランプする。決定的。"""
    width, height = page.get_size()
    zoom = dpi / 72.0  # PDFium も PDF ポイント（72dpi）基準
    longest = max(width, height) * zoom
    cap = _MAX_PIXEL_SIDE
    if longest > cap:
        zoom *= cap / longest  # 縮小倍率をクランプ（アスペクト比維持）
    bitmap = page.render(scale=zoom)
    try:
        # PDFium の bitmap を閉じた後も使える独立した PIL 画像を返す。
        return bitmap.to_pil().copy()
    finally:
        bitmap.close()
