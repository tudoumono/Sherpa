"""LLM での書き換えの失敗が `failed` の回として残ることの確認（要 Postgres＋Neo4j＋ES）。LLM の呼び出しだけを差し替え、ES に届かない状態は本物の宛先で作る。土台は `test_ingest_stop_matrix` と共用する。"""
from __future__ import annotations

import pytest
from test_ingest_stop_matrix import W, _enqueue_refresh, _rows, registered_world  # noqa: F401  (registered_world は fixture)

from sherpa import store
from sherpa.ingest import background, ingest_worker, llm_render, worker
from sherpa.store.replace_rows import RowLockLost


class _Result:
    def __init__(self, changed_rels):
        self.changed_rels = changed_rels


class _Beat:
    def set_state(self, *a, **kw):
        pass


def _llm_runs() -> list:
    return [r for r in store.list_ingest_runs(W) if (r.get("extraction_snapshot") or {}).get("op") == "llm_rewrite"]


@pytest.fixture(autouse=True)
def _no_old_runs():
    with store.db._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version=%s", (W,))
    yield


def _enable_llm(mp, run_world_pass) -> None:
    mp.setattr(llm_render, "prepare", lambda world, settings=None: {"provider": "p", "model": "m"})
    mp.setattr(llm_render, "run_world_pass", run_world_pass)


def test_failed_reindex_keeps_the_row_records_a_failed_run_and_later_passes_are_skipped(registered_world):
    """書き換えた rag.md が ES に載らなかった回は、行を残し、op=llm_rewrite の failed の回を 1 件残す。行が残る間は次の後追いは見送る。"""
    with pytest.MonkeyPatch.context() as mp:
        _enable_llm(mp, lambda world, *, cfg, row, should_stop=None: _Result(["sample.xlsx"]))
        mp.setenv("ES_URL", "http://127.0.0.1:9")
        worker.llm_render_pass(W)

        left = _rows()
        assert len(left) == 1 and left[0]["items"]["es"] == "failed"
        assert store.unfinished_state(W) == "whole"
        failed = _llm_runs()
        assert len(failed) == 1 and failed[0]["status"] == "failed"

        _enable_llm(mp, lambda *a, **kw: pytest.fail("行が残っている間は書き換えない"))
        worker.llm_render_pass(W)
        assert [r["id"] for r in _rows()] == [left[0]["id"]] and len(_llm_runs()) == 1


@pytest.mark.parametrize("exc", [RowLockLost("lost"), RuntimeError("boom")])
def test_followup_that_ends_with_an_exception_records_a_failed_run_and_keeps_the_row(registered_world, exc):
    """後追いが入れ替えの確かめの中止でも想定外の例外でも終わったら、failed の回を残し、行は残る。"""
    def _boom(world, *, cfg, row, should_stop=None):
        raise exc

    with pytest.MonkeyPatch.context() as mp:
        _enable_llm(mp, _boom)
        llm_render.request_followup(W)
        ingest_worker._run_followups(None, _Beat())

    assert len(_rows()) == 1 and store.unfinished_state(W) == "whole"
    failed = _llm_runs()
    assert len(failed) == 1 and failed[0]["status"] == "failed"
    assert failed[0]["extraction_snapshot"]["flags"][0]["reason"] == f"llm_rewrite_failed:{exc.__class__.__name__}"


def test_rewrite_in_progress_shows_no_run_and_does_not_block_a_new_request(registered_world):
    """書き換えの最中は ingest_runs に実行中の回が出ず、別の依頼を積んでも 409 にならない。"""
    seen = {}

    def _pass(world, *, cfg, row, should_stop=None):
        seen["runs"] = [r["status"] for r in store.list_ingest_runs(W) if r["status"] in ("queued", "extracting")]
        seen["run_id"], _ = background.submit(W, "refresh", "{}")
        return _Result([])

    with pytest.MonkeyPatch.context() as mp:
        _enable_llm(mp, _pass)
        worker.llm_render_pass(W)

    assert seen["runs"] == [] and seen["run_id"]
    assert _llm_runs() == [] and _rows() == []


def _boom_pass(world, *, cfg, row, should_stop=None):
    raise RuntimeError("boom")


def _run_failing_followup(pass_fn) -> None:
    with pytest.MonkeyPatch.context() as mp:
        _enable_llm(mp, pass_fn)
        llm_render.request_followup(W)
        ingest_worker._run_followups(None, _Beat())


def test_failed_rewrite_alone_is_the_latest_status(registered_world):
    """書き換えの失敗が最新なら、状態を取るときも失敗がそのまま出る（これまでの表示は変わらない）。"""
    _run_failing_followup(_boom_pass)

    failed = _llm_runs()[0]
    latest = store.get_latest_run_status(W, 10)
    assert latest["id"] == failed["id"] and latest["status"] == "failed"
    assert store.get_latest_run_summary(W)["id"] == failed["id"]


def test_failed_rewrite_does_not_hide_a_request_accepted_during_it(registered_world):
    """書き換えの最中に受け付けた「更新」は、書き換えが失敗した後も、動いている間も終わった後も状態に出る。失敗の履歴は残る。"""
    seen = {}

    def _pass(world, *, cfg, row, should_stop=None):
        seen["run_id"] = store.start_ingest_run(W)["id"]
        raise RuntimeError("boom")

    _run_failing_followup(_pass)

    assert len(_llm_runs()) == 1 and _llm_runs()[0]["id"] > seen["run_id"]
    latest = store.get_latest_run_status(W, 10)
    assert latest["id"] == seen["run_id"] and latest["status"] == "extracting"
    assert store.get_latest_run_progress(W)["status"] == "extracting"
    assert store.finish_ingest_run(seen["run_id"], status="auto_published") is not None
    assert store.get_latest_run_status(W, 10)["status"] == "auto_published"
    assert store.get_latest_run_summary(W)["id"] == seen["run_id"]
    assert store.get_last_finished_run_brief(W)["status"] == "auto_published"
