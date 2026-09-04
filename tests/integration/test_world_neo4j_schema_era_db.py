"""グラフのスキーマ世代ゲート（rv-s3-removal・Codex RV HIGH）の統合テスト（要 Neo4j・`make up`）。

背景・裁定は `sherpa/ingest/world_neo4j.py`（`GRAPH_SCHEMA_ERA`/`check_schema_era`/
`GraphSchemaEraError`）の docstring 参照。fake session 版の境界確認は
`tests/unit/test_world_neo4j_schema_era.py`。

固定する契約:
  - `load_world()` が world ごとに現行 `GRAPH_SCHEMA_ERA` を `:SherpaMeta{world_id}.schema_era`
    として保存し、読み取れる。
  - 保存済み era を故意に旧値へ書き換えると、`world_impact`/`resolve_world_entity`
    （`run_world_impact` 経由）・`lens_service.neo4j_related`（`run_troubleshoot` 経由）・
    `graph_admin.graph_search` のいずれも `GraphSchemaEraError` を raise する（fail-loud）。
  - 未投入 world（`:Entity` が0件）は era 未保存でもゲートが発動しない（既存の空応答のまま）。
"""
from __future__ import annotations

import os
import pathlib

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[2]
os.chdir(ROOT)

MIRROR = ROOT / "fixtures" / "mirror"
WORLD = "schema_era_test_a"          # 使い捨て（テスト後に削除）
WORLD_EMPTY = "schema_era_test_never_loaded"   # 一度もロードしない（未投入 world）


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
    assert flags == []
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)


def _cleanup(drv, world):
    with drv.session() as s:
        s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=world)


def test_load_world_stamps_readable_schema_era():
    """`load_world()` 後、`:SherpaMeta{world_id}.schema_era` が現行 `GRAPH_SCHEMA_ERA` として読める。"""
    from sherpa.ingest import world_neo4j
    _load()
    drv = _driver()
    try:
        with drv.session() as s:
            row = s.run("MATCH (m:SherpaMeta {world_id:$w}) RETURN m.schema_era AS era", w=WORLD).single()
        assert row is not None
        assert row["era"] == world_neo4j.GRAPH_SCHEMA_ERA
    finally:
        _cleanup(drv, WORLD)
        drv.close()


def test_stale_schema_era_fails_loud_across_all_gated_read_entrypoints():
    """era を故意に旧値へ書き換えると、影響/troubleshoot/管理グラフ検索のいずれも
    `GraphSchemaEraError` で fail-loud になる（旧世代の実データを黙って読まない）。"""
    from sherpa import graph_admin, lens_service
    from sherpa.ingest import world_neo4j
    _load()
    drv = _driver()
    try:
        with drv.session() as s:
            s.run("MATCH (m:SherpaMeta {world_id:$w}) SET m.schema_era=$old", w=WORLD, old="old-era-stamp")

            with pytest.raises(world_neo4j.GraphSchemaEraError) as ei:
                world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD)
            assert ei.value.world == WORLD and ei.value.stored_era == "old-era-stamp"
            assert ei.value.lens == "impact"

            with pytest.raises(world_neo4j.GraphSchemaEraError) as ei2:
                lens_service.run_troubleshoot(s, "ORDER-MAIN で ABEND", WORLD)
            assert ei2.value.lens == "troubleshoot"

            with pytest.raises(world_neo4j.GraphSchemaEraError):
                graph_admin.graph_search(s, WORLD, field="category", value="01_ソース", op="prefix")
    finally:
        _cleanup(drv, WORLD)
        drv.close()


def test_missing_schema_era_stamp_also_fails_loud():
    """`:SherpaMeta` スタンプ自体が無い（現行コード以前に作られた旧世代グラフ）場合も同様に
    fail-loud——era 未保存を「一致」とみなして素通ししない。"""
    from sherpa.ingest import world_neo4j
    _load()
    drv = _driver()
    try:
        with drv.session() as s:
            s.run("MATCH (m:SherpaMeta {world_id:$w}) DETACH DELETE m", w=WORLD)   # スタンプごと消す
            with pytest.raises(world_neo4j.GraphSchemaEraError) as ei:
                world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD)
            assert ei.value.stored_era is None
    finally:
        _cleanup(drv, WORLD)
        drv.close()


def test_never_loaded_world_is_unaffected_by_the_gate():
    """一度もロードしていない world（`:Entity` が0件）は era 未保存でもゲートが発動しない——
    既存の「未投入 world は空応答」契約を変えない。"""
    from sherpa.ingest import world_neo4j
    drv = _driver()
    try:
        with drv.session() as s:
            result = world_neo4j.run_world_impact(s, "SHARED-AMT", WORLD_EMPTY)
            assert result["items"] == []                  # raise せず従来どおり空
    finally:
        _cleanup(drv, WORLD_EMPTY)
        drv.close()
