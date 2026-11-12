"""行を書く入口は、行の鍵の lock を今も持つ接続があるときだけ書ける（lock の接続が切れた回は書き続けない・要 Postgres）。"""
from __future__ import annotations

import time
import uuid

import pytest

from sherpa import store
from sherpa.store import db, replace_rows


@pytest.fixture
def world(tmp_path):
    wid = f"row_lock_{uuid.uuid4().hex[:8]}"
    store.upsert_world(wid, str(tmp_path))
    yield wid
    store.delete_world_row(wid)


def _terminate_lock_holder(row: replace_rows.ReplaceRow) -> None:
    with db._connect() as c:
        pids = [r["pid"] for r in c.execute(
            "SELECT pid FROM pg_locks WHERE locktype='advisory' AND objsubid=2 AND classid::bigint=%s AND objid::bigint=%s",
            (row.lock_ns, row.lock_id)).fetchall()]
        assert pids
        for pid in pids:
            c.execute("SELECT pg_terminate_backend(%s)", (pid,))
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        with db._connect() as c:
            if not c.execute("SELECT 1 FROM pg_locks WHERE locktype='advisory' AND objsubid=2 AND classid::bigint=%s "
                             "AND objid::bigint=%s", (row.lock_ns, row.lock_id)).fetchone():
                return
        time.sleep(0.1)
    raise AssertionError("lock が外れない")


def test_every_row_write_stops_once_the_lock_connection_is_gone(world):
    """行を取った回の lock の接続を切ると、鍵が行のままでも、行を書く 4 つの入口は `RowLockLost` で止まり、行は書き換わらない。"""
    with replace_rows.own_row(world) as row:
        replace_rows.before_swap(row, world, "graph", old=["g1"])   # lock がある間は書ける
        _terminate_lock_holder(row)

        with pytest.raises(replace_rows.RowLockLost):
            replace_rows.before_swap(row, world, "es", old=["e1"])
        with pytest.raises(replace_rows.RowLockLost):
            replace_rows.after_old_removed(row, world, "graph")
        with pytest.raises(replace_rows.RowLockLost):
            replace_rows.mark_failed(row, world, "es", "reason")
        with db._connect() as c, pytest.raises(replace_rows.RowLockLost):
            replace_rows.before_swap_in(c, row, world, "ledger")

        cur = replace_rows.read_row(row.id)
        assert cur.items == {"graph": "swapped"} and cur.old_things == {"graph": ["g1"]}
