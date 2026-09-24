"""鏡モデル 手順a 受け入れ（統合・要 Neo4j）: world_graph を Neo4j ロード＋**範囲フィルタ付き**影響たどり。

前提: `make up`（Neo4j 起動）＋ neo4j ドライバ。代表 fixture `fixtures/mirror`（2世代・複製同名）を
**使い捨て world_id** でロードし、テスト後に削除する（v1 等の本番 world を汚さない）。

S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: 意味層フル抽出・REALIZES 橋は撤去済みのため、
起点語は業務 Parameter（`ORDER-LIMIT`）ではなく実コード（`SHARED-AMT` DataItem）を直接使う。
K12: 「確実/要確認」の2値判定は機構ごと撤去（全件同格）——`test_judgement_and_trace` は
`test_trace_and_evidence` に改称し judgement 分岐無しの経路/根拠のみを固定する。

固定する契約（MIRROR-MODEL §2-§3 / 再プラン手順5）:
  - world グラフが Neo4j にロードできる（パス同一性・スコープ メタデータ込み）。
  - 保存は**1世界に統合**（範囲なしの起点語『SHARED-AMT』は両世代に一致）。
  - **範囲フィルタ**（top_scope / 工程 prefix）で1世代を1世界として絞れる・範囲外起点は空。
  - 構造リンクは**世代をまたがない**（世代を固定した起点の影響は同世代で閉じる）。
  - **対応エッジ `CORRESPONDS_TO`／添付 `DOCUMENTS`／参考 `RELATES_TO` は影響たどりに乗らない**
    （世代横断の比較・根拠添付＝影響伝播ではない）。
"""
from __future__ import annotations

import os
import pathlib

ROOT = pathlib.Path(__file__).resolve().parents[2]
os.chdir(ROOT)

MIRROR = ROOT / "fixtures" / "mirror"
WORLD = "mirror_test_a"                       # 使い捨て（テスト後に削除）
SRC = "03_開発/01_ソース"
D4_AMT = f"dataitem:{WORLD}:4期更改/{SRC}/SHARED-CPY.cpy#SHARED-REC.SHARED-AMT"
M5_MAIN = f"module:{WORLD}:5期更改/{SRC}/ORDER-MAIN.cbl#ORDER-MAIN"
M4_MAIN = f"module:{WORLD}:4期更改/{SRC}/ORDER-MAIN.cbl#ORDER-MAIN"
CODE_LINEAGE = {"SHARED-CPY", "ORDER-MAIN", "ORDER-SUB"}


def _driver():
    from neo4j import GraphDatabase
    return GraphDatabase.driver(
        os.environ.get("NEO4J_URI", "bolt://localhost:7687"),
        auth=(os.environ.get("NEO4J_USER", "neo4j"), os.environ.get("NEO4J_PASSWORD", "sherpa_dev")),
    )


def _load():
    from sherpa.ingest import world_graph, world_neo4j
    from _world_registry import register_test_world
    nodes, edges, flags = world_graph.build_world(MIRROR, WORLD)
    assert flags == [], f"鏡 fixture は曖昧なく解決するはず: {flags}"
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)   # 自前 _cleanup のバックストップ（tests/_world_registry.py）


def _cleanup(drv):
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=WORLD)


def test_unified_world_and_scope_filter():
    """統合保存（範囲なし＝両世代）＋ 範囲フィルタ（top_scope/工程で1世代に絞れる・範囲外は空）。"""
    from sherpa.ingest import world_neo4j
    _load()
    drv = _driver()
    try:
        with drv.session() as s:
            # 範囲なし＝1世界に統合されているので起点語は両世代の DataItem に一致
            full = world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD)
            assert {"4期更改", "5期更改"} <= {i["top_scope"] for i in full["items"] if i["top_scope"]}

            # 4期に絞る＝4期だけ（コード系譜が出る・5期は出ない）
            r4 = world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD, scope_prefixes=["4期更改"])
            n4, t4 = {i["name"] for i in r4["items"]}, {i["top_scope"] for i in r4["items"] if i["top_scope"]}
            assert CODE_LINEAGE <= n4 and t4 == {"4期更改"}, (n4, t4)

            # 5期に絞る＝対称（同じ顔ぶれ・世代だけ違う＝複製が別世界として独立）
            r5 = world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD, scope_prefixes=["5期更改"])
            assert {i["top_scope"] for i in r5["items"] if i["top_scope"]} == {"5期更改"}

            # どの階層でも絞れる（工程 prefix）
            rdev = world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD, scope_prefixes=["4期更改/03_開発"])
            assert {i["name"] for i in rdev["items"]} == n4

            # 範囲外の起点（4期 DataItem cid を 5期スコープで）＝空
            assert world_neo4j.world_impact(s, [D4_AMT], WORLD, scope_prefixes=["5期更改"]) == []
    finally:
        _cleanup(drv)
        drv.close()


def test_trace_and_evidence():
    """コード系譜は構造エッジ（COPIES/CONTAINS）で到達（全件同格・K12）＋経路/根拠が付く。"""
    from sherpa.ingest import world_neo4j
    _load()
    drv = _driver()
    try:
        with drv.session() as s:
            r4 = world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD, scope_prefixes=["4期更改"])
            by = {i["name"]: i for i in r4["items"]}
            assert "judgement" not in by["ORDER-MAIN"] and "extraction_method" not in by["ORDER-MAIN"]
            assert by["ORDER-MAIN"]["category"] == "ソース"
            assert by["ORDER-MAIN"]["trace"] and by["ORDER-MAIN"]["evidence"]
            assert by["ORDER-MAIN"]["evidence"][0]["doc"].startswith("4期更改/")   # 根拠=rel_path
    finally:
        _cleanup(drv)
        drv.close()


def test_nonimpact_edges_are_not_traversed():
    """対応 `CORRESPONDS_TO`・添付 `DOCUMENTS`・参考 `RELATES_TO` は影響たどりに乗らない。

    5期ノード→4期ノードにこれら3種を足しても、範囲なし・4期 DataItem 起点の影響に 5期は出ない
    （いずれか1つでも `_IMPACT_REL` に誤って戻れば 5期が漏れて落ちる）。
    """
    from sherpa.ingest import world_neo4j
    _load()
    drv = _driver()
    try:
        with drv.session() as s:
            for etype in ("CORRESPONDS_TO", "DOCUMENTS", "RELATES_TO"):
                s.run(
                    f"MATCH (a:Entity {{canonical_id:$a}}),(b:Entity {{canonical_id:$b}}) "
                    f"MERGE (a)-[:`{etype}` {{world_id:$w}}]->(b)",
                    a=M5_MAIN, b=M4_MAIN, w=WORLD,
                )
            items = world_neo4j.world_impact(s, [D4_AMT], WORLD)        # 範囲なし・4期起点
            tops = {(i.get("path") or "").split("/", 1)[0] for i in items}
            assert "5期更改" not in tops, f"非影響エッジを辿って 5期を引き込んではいけない: {tops}"
            assert CODE_LINEAGE <= {i["name"] for i in items}      # 4期系譜は出る
    finally:
        _cleanup(drv)
        drv.close()
