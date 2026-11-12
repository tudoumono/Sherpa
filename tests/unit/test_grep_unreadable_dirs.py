"""読めないフォルダのある範囲の検索は、探せなかったフォルダの数を申告する（通常の検索候補にしない）。"""
from __future__ import annotations

import os

import pytest

from sherpa import grep_tool as G
from sherpa import rg_search

pytestmark = pytest.mark.skipif(os.geteuid() == 0, reason="root は権限 0 のフォルダも開ける")


@pytest.fixture
def tree(tmp_path):
    (tmp_path / "ok").mkdir()
    (tmp_path / "ok" / "a.txt").write_text("NEEDLE\n", encoding="utf-8")
    locked = tmp_path / "locked"
    locked.mkdir()
    (locked / "b.txt").write_text("NEEDLE\n", encoding="utf-8")
    locked.chmod(0)
    try:
        yield tmp_path
    finally:
        locked.chmod(0o755)


@pytest.mark.parametrize("engine", ["python", "auto"])
def test_unreadable_dir_is_counted_in_stats(tree, engine):
    if engine == "auto" and rg_search.find_rg() is None:
        pytest.skip("rg が無い")
    stats: dict = {}
    hits = G.grep_search("NEEDLE", roots=[tree], stats=stats, engine=engine)
    assert [h["doc_id"] for h in hits] == ["ok/a.txt"]
    assert stats == {"unreadable_dirs": 1}


def test_rg_abnormal_exit_without_known_cause_falls_back_to_python(tree, monkeypatch):
    """rg が終了コード 2 で、読めなかった場所を特定できないときは、結果を採らず純 Python 走査でやり直す。"""
    if rg_search.find_rg() is None:
        pytest.skip("rg が無い")
    monkeypatch.setattr(rg_search, "_error_dirs", lambda err, cwd=None: [])
    stats: dict = {}
    hits = G.grep_search("NEEDLE", roots=[tree], stats=stats)
    assert [h["doc_id"] for h in hits] == ["ok/a.txt"]
    assert stats == {"unreadable_dirs": 1}
