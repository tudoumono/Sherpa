"""廃止/隠しは既定で除外、include_deprecated で参照可（AT-5・要 Neo4j・鏡モデル）。

S3（2026-09-04-グラフのソース正典化.md §4・K9-K11）: status=deprecated/hidden_candidate を持つ
ノードの唯一の供給源だった L 意味層（`_ensure_concept` の status 引数）は撤去済み——骨格
（Pass1/Pass2・COBOL/JCL/copybook）は常に status=active のノードしか作らない。AT-5 の
フィルタ契約自体（Cypher の `$incl OR coalesce(status,'active')='active'`）は変えていないため、
Neo4j 上で直接 status を書き換えて契約を固定する（`test_world_impact_a.py::
test_nonimpact_edges_are_not_traversed` と同じ手法＝直接 Neo4j 操作でグラフ側だけの契約を確認）。
"""
from __future__ import annotations

from _world_setup import TEST_WORLD_ID, driver, ensure_v1

V = TEST_WORLD_ID   # 旧固定 'v1' から移行（2026-07-03 インシデント対応 HIGH#2・_world_setup.py 参照）
DEPRECATED_NODE = "BILLGEN"
HIDDEN_NODE = "SALESUP"


def _run(include_deprecated):
    from sherpa.impact_service import run_impact
    ensure_v1()
    drv = driver()
    try:
        with drv.session() as s:
            s.run("MATCH (n:Entity {world_id:$w, name:$name}) SET n.status='deprecated'",
                 w=V, name=DEPRECATED_NODE)
            s.run("MATCH (n:Entity {world_id:$w, name:$name}) SET n.status='hidden_candidate'",
                 w=V, name=HIDDEN_NODE)
            return run_impact(s, "TAX-RATE", V, include_deprecated=include_deprecated)
    finally:
        drv.close()


def test_deprecated_excluded_by_default():
    names = {i["name"] for i in _run(False)["items"]}
    assert DEPRECATED_NODE not in names and HIDDEN_NODE not in names
    assert "TAXCALC" in names                    # active な直接の系譜は壊れない
    # NIGHTLY は BILLGEN/SALESUP 経由でしか TAX-RATE に届かない＝経路上に非 active を含む＝除外される
    assert "NIGHTLY" not in names


def test_included_with_flag_and_labeled():
    items = {i["name"]: i for i in _run(True)["items"]}
    assert DEPRECATED_NODE in items and HIDDEN_NODE in items
    assert items[DEPRECATED_NODE]["status"] == "deprecated"
    assert items[HIDDEN_NODE]["status"] == "hidden_candidate"
