"""グラフの**クローズド語彙**（05-グラフ語彙.md §1-§2・2026-09-04-グラフのソース正典化.md §4 K13）。

鏡モデルでは同一性＝パス（`world_graph` がパス修飾 canonical_id を作る）。旧 `@版` の
`canonical_id`/`Node`/`Edge`/`impact`/`name_of` は撤去（world_graph/world_neo4j が置換）。
ここに残すのは label/edge の許容集合だけ（`world_neo4j.load_world` の Cypher 直埋め allowlist）。

K13（確定・復活させない）: 意味層フル抽出・REALIZES 橋の撤去に伴い、供給源を失った概念ラベル/エッジ
（Parameter/BusinessRule/Function/Screen/Report/Standard/Incident・USES/REFERENCES/IMPLEMENTED_BY/
PRODUCED_BY/CONFORMS_TO/RELATES_TO/REALIZES）を刈った。Table/ACCESSES は producer が現存しないが、
将来の SQL/FILE 静的解析の受け皿として語彙に残す（着工していない計画枠・残置コストゼロ）。
"""
from __future__ import annotations

# ONTOLOGY §1 / §2 のクローズド語彙（K13 で確定）
NODE_LABELS = {"Module", "Copybook", "Batch", "DataItem", "Table", "Document"}
EDGE_TYPES = {"COPIES", "CONTAINS", "INVOKES", "ACCESSES", "DOCUMENTS"}
