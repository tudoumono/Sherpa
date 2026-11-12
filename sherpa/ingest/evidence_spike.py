"""旧モジュール名。`sherpa.ingest.evidence` の部品から名前を出し直すだけ（呼び出し側を付け替えたら消す）。"""
from __future__ import annotations

from .evidence import (  # noqa: F401
    DOCX_ADAPTER_VERSION, PDF_ADAPTER_VERSION, PPTX_ADAPTER_VERSION, XLSX_ADAPTER_VERSION, extract, extract_assets,
)
from .evidence.adapt import _adapt_document_ir, _apply_cover_state  # noqa: F401
from .evidence.base import _element_id, _stable_id  # noqa: F401
from .evidence.builder import _Builder  # noqa: F401
from .evidence.drawing import _image_pixel_size, _occlusion_ratio  # noqa: F401
from .evidence.package import _image_relationship_records, _relationship_records, _relationships  # noqa: F401
from .evidence.pdf_layout import _PDFIUM_LOCK  # noqa: F401
from .evidence.pdf_xobject import _pdf_image_payload  # noqa: F401
from .evidence.pptx import _ppt_kind  # noqa: F401
