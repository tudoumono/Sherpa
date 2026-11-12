"""取り込み設定（管理画面 GET・PUT /admin/settings の `rag_llm_render`・`ocr`）の応答スキーマ。`sherpa.schemas` から再公開する。"""
from __future__ import annotations

from pydantic import BaseModel


class RagLlmRenderInfo(BaseModel):
    """rag.md の LLM 成形トグル。"""
    configured: str | None
    effective: bool
    default: bool
    options: list[str]


class OcrInfo(BaseModel):
    """OCR の入切。`configured` は全体設定の生値（未設定=null＝既定の切）、`env_override` は環境変数 SHERPA_OCR_ENABLED=0 で切に固定されているとき false（それ以外は null＝設定に従う）。"""
    configured: bool | None
    effective: bool
    default: bool
    env_override: bool | None
