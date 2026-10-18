"""`world_impact(flow_depth=0)` の戻りが、データの流れの段を足す前と同一であることのスナップショット比較。

`fixtures/corpus/v1` の構造の辺（`COPIES`/`CONTAINS`/`INVOKES`/`ACCESSES`）だけを Neo4j の代わりのセッションに載せ、
全ノードを起点にした影響たどりの結果（影響先・代表経路・辺）を `goldens/world_impact_v1_flow0.json` と比べる。golden は流れの段を足す前の
`world_neo4j.world_impact` で作ったもの。意図して構造のたどりを変えたときだけ、次で更新して差分を目視確認する:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/unit'); import test_world_impact_flow0_snapshot as t; t._write_golden()"
"""
from __future__ import annotations

import json
import pathlib

import pytest
from test_world_graph_dataflow import _WorldSession

from sherpa.ingest import world_graph
from sherpa.ingest import world_neo4j as WN

ROOT = pathlib.Path(__file__).resolve().parents[2]
GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "world_impact_v1_flow0.json"
STRUCTURE = frozenset(WN._IMPACT_REL.split("|"))


def _snapshot(**impact_kwargs) -> dict:
    nodes, edges, _flags = world_graph.build_world(ROOT / "fixtures" / "corpus" / "v1", "v1_flow0_snapshot")
    session = _WorldSession(nodes, [e for e in edges if e["type"] in STRUCTURE])
    out = {}
    for cid in sorted(session.nodes):
        info: dict = {}
        items = WN.world_impact(session, [cid], "w", depth=10, info=info, detail=True, **impact_kwargs)
        out[cid] = {"depth_truncated": info["depth_truncated"],
                    "items": [{"cid": i["canonical_id"], "name": i["name"], "label": i["label"], "trace_cids": i["trace_cids"],
                               "edges": [(e["type"], e["doc"], e["line"]) for e in i["edges"]]} for i in items]}
    return out


def _write_golden() -> None:
    GOLDEN.write_text(json.dumps(_snapshot(), indent=1, ensure_ascii=False, sort_keys=True) + "\n", encoding="utf-8")


@pytest.fixture(scope="module")
def golden():
    return json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_flow_depth_zero_equals_the_golden_made_before_the_flow_stage(golden):
    assert json.loads(json.dumps(_snapshot(flow_depth=0), sort_keys=True)) == golden


def test_default_arguments_are_the_flow_depth_zero_path(golden):
    assert json.loads(json.dumps(_snapshot(), sort_keys=True)) == golden


def test_golden_is_meaningful(golden):
    assert len(golden) >= 20 and sum(len(v["items"]) for v in golden.values()) >= 20
