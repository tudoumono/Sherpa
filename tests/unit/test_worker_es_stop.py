"""ES 索引の区切りでの停止と、ベクトルの足し直しの取り込み側の扱い（`ingest/worker.py`）。"""
from __future__ import annotations

import pytest

from sherpa.ingest import background, worker


def test_holdback_passes_stop_request_through_without_recording_a_failure(monkeypatch):
    """停止要求は失敗ではない。失敗の記録（ingest_runs）を作らず、呼び出し元の中断の扱いへ通す。"""
    from sherpa import worlds
    monkeypatch.setattr(worlds, "world_dir", lambda w: None)

    def stop(*a, **k):
        raise background.StopRequested()

    monkeypatch.setattr(worker.es_index, "index_world", stop)
    recorded = []
    monkeypatch.setattr(worker, "_record_es_index_failure", lambda *a, **k: recorded.append(a))
    with pytest.raises(background.StopRequested):
        worker.index_world_with_human_md_holdback("w", content_sig="s")
    assert recorded == []


def test_holdback_still_records_other_exceptions_as_failure(monkeypatch):
    from sherpa import worlds
    monkeypatch.setattr(worlds, "world_dir", lambda w: None)

    def boom(*a, **k):
        raise RuntimeError("x")

    monkeypatch.setattr(worker.es_index, "index_world", boom)
    recorded = []
    monkeypatch.setattr(worker, "_record_es_index_failure", lambda *a, **k: recorded.append(a))
    assert worker.index_world_with_human_md_holdback("w", content_sig="s")["error"] == "RuntimeError"
    assert recorded


def test_sync_records_a_stop_in_the_unchanged_branch_as_interrupted(monkeypatch):
    """変更なしの分岐の ES の区切りで止まった同期は、失敗ではなく「中断」として記録して返す（起動時の自動再開の対象）。"""
    def stop(*a, **k):
        raise background.StopRequested()

    monkeypatch.setattr(worker, "_sync_impl", stop)
    finished = []
    monkeypatch.setattr(worker.store, "finish_ingest_run", lambda run_id, **kw: finished.append((run_id, kw)))
    res = worker.sync("w", run_id=7, op="sync")
    assert res["interrupted"] is True and res["status"] == "failed" and res["changed"] is False
    assert finished[0][0] == 7 and finished[0][1]["extraction_snapshot"]["interrupted"] is True


def test_with_fill_overlays_the_remaining_and_filled_counts():
    base = {"available": True, "chunks": 10, "vectors_missing": 5}
    out = worker._with_fill(base, {"filled": 5, "missing": 0}, "w")
    assert "vectors_missing" not in out and out["vectors_filled"] == 5      # 全部足せたので残りの印は消える
    out = worker._with_fill(base, {"filled": 2, "missing": 3}, "w")
    assert out["vectors_missing"] == 3 and out["vectors_filled"] == 2
    assert worker._with_fill(base, {"filled": 0, "missing": 0}, "w") is base   # 何も起きなければそのまま
    assert worker._with_fill(None, None, "w") is None


def test_fill_step_is_best_effort_but_lets_a_stop_through(monkeypatch):
    def boom(world):
        raise RuntimeError("es down")

    monkeypatch.setattr(worker.es_index, "fill_missing_vectors", boom)
    assert worker._fill_missing_vectors_step("w") is None

    def stop(world):
        raise background.StopRequested()

    monkeypatch.setattr(worker.es_index, "fill_missing_vectors", stop)
    with pytest.raises(background.StopRequested):
        worker._fill_missing_vectors_step("w")
