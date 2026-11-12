"""システム設定の画面表示のうち、埋め込みの項目の形。"""
from __future__ import annotations

from pydantic import BaseModel


class EmbedDimInfo(BaseModel):
    """`embed_dim`（OpenAI の埋め込みの次元）。`default`=空欄のときの値（1536）、`model_max`=使っているモデルの最大、`min`/`max`=記入できる範囲、`applies`=偽なら Ollama で使われない。"""
    configured: int | None
    effective: int
    default: int
    model_max: int | None = None
    min: int
    max: int
    applies: bool


class EmbedRangeInfo(BaseModel):
    """`embed_request_timeout_s`・`embed_batch_size`。`min`/`max`=記入できる範囲。"""
    configured: int | None
    effective: int
    default: int
    min: int
    max: int


class EmbedProviderInfo(BaseModel):
    """`embed_provider`（埋め込みの接続先）。`ollama_model` は "ollama" のとき使う埋め込みモデル名。"""
    configured: str | None
    effective: str
    default: str
    options: list[str]
    ollama_model: str
