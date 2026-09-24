"""グラフの**クローズド語彙**（05-グラフ語彙.md §1-§2・2026-09-04-グラフのソース正典化.md §4 K13）。

鏡モデルでは同一性＝パス（`world_graph` がパス修飾 canonical_id を作る）。旧 `@版` の
`canonical_id`/`Node`/`Edge`/`impact`/`name_of` は撤去（world_graph/world_neo4j が置換）。
ここに残すのは label/edge の許容集合だけ（`world_neo4j.load_world` の Cypher 直埋め allowlist）。

K13（確定・復活させない）: 意味層フル抽出・REALIZES 橋の撤去に伴い、供給源を失った概念ラベル/エッジ
（Parameter/BusinessRule/Function/Screen/Report/Standard/Incident・USES/REFERENCES/IMPLEMENTED_BY/
PRODUCED_BY/CONFORMS_TO/RELATES_TO/REALIZES）を刈った。Table/ACCESSES は当初 producer ゼロの計画枠
だったが、アナライザ拡張 S2（`SqlDdlAnalyzer`＝DDL の `CREATE TABLE`・COBOL `EXEC SQL`→
`ACCESSES(via=exec_sql)`）で producer が付いた（docs/proposals/2026-09-05-アナライザ拡張.md §4(a)/(d)/(g)）。

A6（アナライザ拡張・2026-09-05・K13 確定後の唯一の追加）: `Config`（設定ファイル・.properties/YAML/
XML 設定の受け皿）を1種のみ追加。エッジ型は増やさない——`INVOKES`/`ACCESSES` を `via` で細分する
（docs/05-グラフ語彙.md §1/§2・`analyzers/_base.py::KNOWN_VIA`）。
"""
from __future__ import annotations

# ONTOLOGY §1 / §2 のクローズド語彙（K13 確定＋A6 で Config を追加）
NODE_LABELS = {"Module", "Copybook", "Batch", "DataItem", "Table", "Document", "Config"}
EDGE_TYPES = {"COPIES", "CONTAINS", "INVOKES", "ACCESSES", "DOCUMENTS"}
