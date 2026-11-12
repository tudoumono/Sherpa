"""失った lock を、回を拾っていない呼び出しと同じに扱って取り直さない（古い worker の書き込みをもう一度許さない）。要 Postgres。"""
from __future__ import annotations

import pytest
from _replace_row import row_for

from sherpa import store
from sherpa.ingest import replace_guard
from sherpa.store.replace_rows import RowLockLost

W = "test_lock_lost"


@pytest.fixture
def run_id():
    rid = store.start_ingest_run(W)["id"]
    yield rid
    store.fail_close_if_extracting(rid, reason="cleanup", interrupted=True)


def test_held_row_does_not_retake_the_lock_of_a_run_closed_as_dead(run_id):
    """拾った回の行があるのに、その lock を誰も持っていなければ（切れて failed で閉じられた回）、取り直さず RowLockLost にする。"""
    row_for(W, "", lock_id=run_id, held=False)
    assert store.fail_close_if_extracting(run_id, reason="lock_lost", interrupted=True) is True

    with pytest.raises(RowLockLost):
        with replace_guard.held_row(W, run_id):
            pytest.fail("失った lock で入れ替えに進んではいけない")


def test_held_row_without_a_claimed_row_still_takes_its_own_lock():
    """回を拾っていない呼び出し（行が無い）は、今までどおり自分の lock と行を持つ。"""
    with replace_guard.held_row(W, 987654) as row:
        assert row is not None and row.lock_id == 987654
    store.delete_world_row(W)


def test_fix_sources_does_not_write_through_a_key_taken_over_by_another_run():
    """確定済みの行の lock の鍵を別の回が書き換えていたら、同じ原本・設定の分岐でも、読み直した鍵で書かず RowLockLost にする。"""
    from sherpa.ingest import source_scan
    from sherpa.store import db
    rid = store.start_ingest_run(W)["id"]
    row = replace_guard.checked(row_for(W, "", lock_id=rid))
    sig, manifest = source_scan.world_state(W) if store.get_world(W) else ("s", {})
    row = replace_guard.fix_sources(row, sig, manifest)
    with db._connect() as c:
        c.execute("UPDATE ingest_replacements SET lock_id=lock_id+1000000 WHERE id=%s", (row.id,))
    with pytest.raises(RowLockLost):
        replace_guard.fix_sources(row, sig, manifest)
