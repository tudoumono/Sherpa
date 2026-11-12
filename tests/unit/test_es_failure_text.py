"""全文検索の反映の失敗が、画面に出す平文と「直前の索引が残る」判定になる。"""
from __future__ import annotations

import pytest

from sherpa.routers import worlds

_OK = {"available": True, "chunks": 5, "error": None}


@pytest.mark.parametrize("err, kept, words", [
    ("insufficient_space", True, "ディスクの空きが足りない"),
    ("analyzer_unavailable", False, "kuromoji"),
    ("alias_swap_unknown", True, "切り替えが終わったか確かめられません"),
])
def test_new_es_failures_have_plain_text_and_index_kept(err, kept, words):
    state, text, is_kept = worlds._es_reflect_state([{"available": True, "error": err}, _OK], None)
    assert state == "failed" and words in text and is_kept is kept


def test_insufficient_space_shows_how_much_to_free():
    latest = {"available": True, "error": "insufficient_space", "shortfall_bytes": 3 * 1024 ** 3}
    _, text, _ = worlds._es_reflect_state([latest, _OK], None)
    assert "あと約 3.0 GB 空けてください" in text


def test_worker_summary_keeps_shortfall_bytes():
    from sherpa.ingest import worker
    out = worker._es_summary({"available": True, "error": "insufficient_space", "shortfall_bytes": 7})
    assert out["shortfall_bytes"] == 7
