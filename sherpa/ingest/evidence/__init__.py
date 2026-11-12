"""XLSX/DOCX/PPTX/PDF を同じ Evidence IR 契約へ通す部品。

現行 Document IR を共通 core へ写し、Document IR が落としている DrawingML/PDF の object inventory を原本 package から補う。
画像の内容・overlay の業務状態・図の意味は推測せず、存在・位置・hash・幾何関係と未解釈状態だけを残す。
外から使う名前はここに出したものだけ。
設計: docs/design/rag.md「取り込みのアーム」
"""
from __future__ import annotations

from .api import extract, extract_assets
from .base import DOCX_ADAPTER_VERSION, PDF_ADAPTER_VERSION, PPTX_ADAPTER_VERSION, XLSX_ADAPTER_VERSION

__all__ = [
    "DOCX_ADAPTER_VERSION",
    "PDF_ADAPTER_VERSION",
    "PPTX_ADAPTER_VERSION",
    "XLSX_ADAPTER_VERSION",
    "extract",
    "extract_assets",
]
