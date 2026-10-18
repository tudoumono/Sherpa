"""影響範囲の評価セット S（合成コーパス・40 問）を、静的グラフの逆向き閉包で採点して固定する（Neo4j 不要）。

`gen_corpus.py --seed 7`（300 プログラム・60 コピーブック・60 ジョブ・200 文書）をテスト時に生成する（生成物は
リポジトリに入れない・決定的）。gold は生成器が依存を作る側で書くので、解析器（`world_graph.build_world`）とは独立。
しきい値は Recall 1.000・Precision 1.000。落ちたときは問いごとに漏れ／余計を架空名で列挙する。
実 Neo4j の BFS 経路は `tests/integration/test_impact_bfs_eval.py`。読み方・退行の確かめ方は `scripts/bench/README.md`。
"""
from __future__ import annotations

import pytest

from scripts.bench import eval_gate

pytestmark = pytest.mark.contract


@pytest.fixture(scope="module")
def eval_set(tmp_path_factory):
    corpus = eval_gate.generate(tmp_path_factory.mktemp("eval_s") / "corpus", "S")
    return eval_gate.load_questions(corpus), eval_gate.Graph.build(corpus)


def _scores(questions, graph):
    return eval_gate.score_all(questions, {q["id"]: graph.answer(q) for q in questions})


def test_gold_is_meaningful(eval_set):
    questions, _ = eval_set
    assert len(questions) == 40
    g = [q["gold"] for q in questions]
    assert sum(1 for x in g if x["programs_indirect"]) >= 30
    assert sum(1 for x in g if x["jobs"]) >= 30
    assert sum(1 for x in g if x["docs"]) >= 30


def test_static_closure_matches_gold_exactly(eval_set):
    questions, graph = eval_set
    scores = _scores(questions, graph)
    report = eval_gate.format_report(scores, "S/static-closure")
    assert all(s.exact for s in scores), report
    assert eval_gate.summary(scores) == (1.0, 1.0), report


@pytest.mark.parametrize("dropped", ["INVOKES", "COPIES"])
def test_dropping_an_edge_kind_turns_the_score_red(eval_set, dropped):
    questions, graph = eval_set
    broken = eval_gate.Graph.of(graph.nodes, [e for e in graph.edges if e["type"] != dropped])
    scores = _scores(questions, broken)
    assert eval_gate.summary(scores)[0] < 1.0
    report = eval_gate.format_report(scores, "S/static-closure")
    assert "NG:" in report and "漏れ" in report


def test_extra_results_are_reported_as_extra(eval_set):
    questions, graph = eval_set
    q = questions[0]
    ghost = "架空システム/03_開発/01_ソース/PG99999.cbl"
    s = eval_gate.score_question(q, graph.answer(q) | {ghost})
    assert s.extra == [ghost] and s.missing == [] and s.precision < 1.0
    report = eval_gate.format_report([s], "S/static-closure")
    assert "余計 1 件" in report and "PG99999.cbl" in report
