"""評価セット S（合成コーパス・影響範囲 40 問）を、実 Neo4j 上の BFS 影響たどり（`world_impact`）で採点する（要 Neo4j・`make up`）。

コーパスはテスト時に生成する（`scripts/bench/gen_corpus.py --seed 7`・決定的）。起点は項目名を `resolve_world_entity` で引き、
影響先は `world_impact`、設計書は `world_related_documents`（`graph_impact` の `related_documents` と同じ経路）から集めて
gold と突き合わせる。しきい値は Recall 1.000・Precision 1.000。Neo4j 無しの閉包側は `scripts/bench/eval_gate.py`（`make gate-merge`）。
"""
from __future__ import annotations

import os

import pytest
from _world_registry import register_test_world

from scripts.bench import eval_gate
from sherpa.ingest import world_neo4j

WORLD = "impact_eval_s"          # 使い捨て（テスト後に削除）
EVAL_DEPTH = 30                  # S の gold の最長の鎖は 13 段（既定の IMPACT_MAX_DEPTH=10 を超える）


@pytest.fixture(scope="module")
def eval_set(tmp_path_factory):
    corpus = eval_gate.generate(tmp_path_factory.mktemp("eval_s") / "corpus", "S")
    return eval_gate.load_questions(corpus), eval_gate.Graph.build(corpus)


@pytest.fixture(scope="module")
def session(eval_set):
    from neo4j import GraphDatabase
    _, graph = eval_set
    env = world_neo4j._env()
    drv = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        world_neo4j.load_world(graph.nodes, graph.edges, WORLD, env["uri"], env["user"], env["pw"])
        register_test_world(WORLD)
        with drv.session() as s:
            yield s
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD)
    finally:
        drv.close()


def _bfs_answer(session, q: dict, depth: int) -> tuple[set[str], dict]:
    cpy, item = q["start"]["copybook"], q["start"]["item"]
    starts = world_neo4j.resolve_world_entity(session, item, WORLD)
    assert [s["path"] for s in starts] == [cpy], f"起点 {item} が一意に引けない: {starts}"
    cid = starts[0]["canonical_id"]
    info: dict = {}
    items = world_neo4j.world_impact(session, [cid], WORLD, depth=depth, info=info, detail=True)
    assert info.get("overload") is None
    docs = world_neo4j.world_related_documents(session, [cid] + [i["canonical_id"] for i in items], WORLD)
    got = {i["path"] for i in items} | {d["path"] for d in docs}
    return got - {cpy}, info


def test_bfs_impact_matches_gold_on_all_questions(session, eval_set):
    """深さの天井（EVAL_DEPTH）を gold の最長の鎖より深く取れば、40 問すべてが gold と一致する。"""
    questions, _ = eval_set
    answers, truncated = {}, []
    for q in questions:
        answers[q["id"]], info = _bfs_answer(session, q, EVAL_DEPTH)
        if info.get("depth_truncated") is not False:
            truncated.append(q["id"])
    scores = eval_gate.score_all(questions, answers)
    report = eval_gate.format_report(scores, "S/neo4j-bfs")
    assert all(s.exact for s in scores), report
    assert eval_gate.summary(scores) == (1.0, 1.0), report
    assert not truncated, f"深さの先に影響先が残る問い: {truncated}"


def test_default_depth_never_adds_extras_and_declares_every_shortfall(session, eval_set):
    """既定の深さ（IMPACT_MAX_DEPTH）では最長の鎖に届かない問いがありうる。余計は出さず、漏れた問いは必ず depth_truncated を立てる。"""
    questions, _ = eval_set
    for q in questions:
        got, info = _bfs_answer(session, q, world_neo4j.IMPACT_MAX_DEPTH)
        s = eval_gate.score_question(q, got)
        assert not s.extra, eval_gate.format_report([s], "S/neo4j-bfs/default")
        if s.missing:
            assert info.get("depth_truncated") is True, eval_gate.format_report([s], "S/neo4j-bfs/default")
