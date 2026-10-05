"""グラフのクローズド語彙（ラベル・エッジ型の許容集合）。

`world_neo4j.load_world` が Cypher へ直接埋め込む allowlist としても使う。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

NODE_LABELS = {"Module", "Copybook", "Batch", "DataItem", "Table", "Document", "Config"}
EDGE_TYPES = {"COPIES", "CONTAINS", "INVOKES", "ACCESSES", "DOCUMENTS"}
