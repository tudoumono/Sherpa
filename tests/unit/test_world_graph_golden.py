"""`world_graph.build_world` の出力の回帰試験（部品化の前後でグラフが変わらないことの網）。

`fixtures/corpus/` 直下の資料フォルダごとに、正規化したグラフの SHA-256 と件数を期待値
（`tests/unit/goldens/world_graph/<名前>.json`）と突き合わせる。期待値の作り直しは `scripts/graph_golden.py --write`。
"""
from __future__ import annotations

import pytest

import _graph_golden as gg


def test_every_corpus_folder_has_a_golden():
    names = gg.corpus_names()
    missing = [n for n in names if not gg.golden_path(n).is_file()]
    stale = sorted(p.name[:-5] for p in gg.GOLDEN_DIR.glob("*.json") if p.name[:-5] not in names)
    assert not missing and not stale, f"期待値が無い: {missing} / 資料フォルダが無い: {stale}（scripts/graph_golden.py --write）"


@pytest.mark.parametrize("name", gg.corpus_names())
def test_world_graph_matches_golden(name):
    expected = gg.read_golden(name)
    graph = gg.build_graph(name)
    got = {"sha256": gg.digest(graph), "counts": gg.counts(graph)}
    if got["sha256"] == expected["sha256"] and got["counts"] == expected["counts"]:
        return
    lines = gg.diff_counts(expected["counts"], got["counts"])
    old = gg.read_graph(name)
    if old is not None:
        lines += gg.diff_graphs(old, graph)
    pytest.fail(f"{name}: グラフが期待値と違う（sha256 {expected['sha256'][:12]} -> {got['sha256'][:12]}）\n" + "\n".join(lines))
