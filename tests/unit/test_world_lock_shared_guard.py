"""`store.db.world_lock_shared`（PART-4・研究実行と rebind の TOCTOU 対策）の配線テスト（DB 不要）。

`test_world_lock_unlock_guard.py`（`world_lock` の unlock best-effort）と同じ手法（偽 psycopg 接続）
で、実際に発行される SQL・connect kwargs・`lock_timeout` の値を検証する。共有/排他の相互排他という
実際の PostgreSQL ロック意味論そのものは `tests/integration/test_world_lock_shared_semantics.py`
（実 Postgres・複数コネクション）で検証する。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class _FakeConn:
    """`pg_advisory_lock_shared` は成功・unlock で例外を投げ・close は記録する偽接続。
    渡された connect kwargs（`options` 等）も記録する。
    """

    def __init__(self):
        self.closed = False
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append((sql, params))
        if "pg_advisory_unlock_shared" in sql:
            raise RuntimeError("unlock-secondary (PG 断・元例外を隠したら NG)")
        return self

    def close(self):
        self.closed = True


def _patch(monkeypatch):
    from sherpa.store import db
    fake = _FakeConn()
    seen_kwargs: dict = {}

    def fake_connect(dsn, **kw):
        seen_kwargs.update(kw)
        return fake

    monkeypatch.setattr(db, "_ensure", lambda: None)
    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    return db, fake, seen_kwargs


def test_world_lock_shared_issues_shared_lock_and_unlock_sql(monkeypatch):
    db, fake, _ = _patch(monkeypatch)
    with db.world_lock_shared("w1"):
        pass
    sqls = [s for s, _ in fake.executed]
    assert any("pg_advisory_lock_shared" in s for s in sqls)
    assert any("pg_advisory_unlock_shared" in s for s in sqls)
    assert fake.closed is True


def test_world_lock_shared_and_world_lock_use_the_same_key(monkeypatch):
    """`world_lock`（排他）と `world_lock_shared`（共有）が同じキー導出を使う——別キーだと
    そもそも相互排他が成立しない（`_world_lock_key` を共用している契約の固定）。"""
    from sherpa.store import db
    assert db._world_lock_key("same-world") == db._world_lock_key("same-world")
    # `world_lock`/`world_lock_shared` が実際に渡す params の1番目（key）が一致することも確認する。
    db2, fake2, _ = _patch(monkeypatch)
    with db2.world_lock("w-key-check"):
        pass
    excl_key = next(p[0] for s, p in fake2.executed if s == "SELECT pg_advisory_lock(%s)")
    fake2.executed.clear()
    with db2.world_lock_shared("w-key-check"):
        pass
    shared_key = next(p[0] for s, p in fake2.executed if s == "SELECT pg_advisory_lock_shared(%s)")
    assert excl_key == shared_key


def test_world_lock_shared_timeout_ms_sets_lock_timeout_via_set_after_connect(monkeypatch):
    """`lock_timeout` は接続 `options` ではなく、接続確立**後**の `SET` で発行する——接続確立に
    かかった時間ぶんを差し引いた残りだけをロック待ちへ渡すため（RV MED#2: 接続待ちとロック待ちに
    同じ残時間を渡すと合算で超過しうる不具合の是正）。ここでは接続前後で `time.monotonic()` が
    250ms 進んだことにし、`timeout_ms=1500` から差し引いた `1250ms` が使われることを固定する。
    """
    db, fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 100.25])
    monkeypatch.setattr(db.time, "monotonic", lambda: next(times))
    with db.world_lock_shared("w1", timeout_ms=1500):
        pass
    assert "options" not in seen_kwargs
    sqls = [s for s, _ in fake.executed]
    assert any(s == "SET lock_timeout = '1250ms'" for s in sqls)


def test_world_lock_shared_lock_timeout_clamped_to_minimum_when_connect_elapsed_exceeds_budget(monkeypatch):
    """接続確立だけで `timeout_ms` を使い切って（または超えて）いても、`lock_timeout` に 0 を
    渡さない——Postgres では `lock_timeout=0` は「無効化＝無制限待ち」を意味し、デッドライン超過を
    防ぐはずの値がむしろ無制限待ちへ反転してしまう。残り無しは最小 1ms へクランプし、ほぼ即座に
    `LockNotAvailable` を送出させる。
    """
    db, fake, _ = _patch(monkeypatch)
    times = iter([100.0, 102.0])   # timeout_ms=1500 を超える 2000ms が接続だけで経過
    monkeypatch.setattr(db.time, "monotonic", lambda: next(times))
    with db.world_lock_shared("w1", timeout_ms=1500):
        pass
    sqls = [s for s, _ in fake.executed]
    assert any(s == "SET lock_timeout = '1ms'" for s in sqls)


def test_world_lock_shared_without_timeout_ms_has_no_lock_timeout(monkeypatch):
    db, fake, seen_kwargs = _patch(monkeypatch)
    with db.world_lock_shared("w1"):
        pass
    assert "options" not in seen_kwargs
    sqls = [s for s, _ in fake.executed]
    assert not any("lock_timeout" in s for s in sqls)


def test_world_lock_shared_connect_timeout_rounds_up_and_clamps_to_minimum_one(monkeypatch):
    """psycopg 3.3.4 は `connect_timeout` を整数秒でしか扱わず小数部を切り捨てる——1未満の値
    （例 0.3）をそのまま渡すと 0 に切り捨てられ、libpq は「無制限」（実測で Linux の TCP 再送
    タイムアウト約130秒相当）として扱ってしまう。整数秒へ切り上げ・最小1秒でクランプしてから渡す。
    """
    db, fake, seen_kwargs = _patch(monkeypatch)
    with db.world_lock_shared("w1", connect_timeout=0.3):
        pass
    assert seen_kwargs.get("connect_timeout") == 1


def test_world_lock_shared_unlock_failure_does_not_mask_body_exception(monkeypatch):
    db, fake, _ = _patch(monkeypatch)
    with pytest.raises(ValueError, match="body-primary"):
        with db.world_lock_shared("w1"):
            raise ValueError("body-primary")
    assert fake.closed is True


def test_world_lock_shared_unlock_failure_swallowed_on_clean_exit(monkeypatch):
    db, fake, _ = _patch(monkeypatch)
    with db.world_lock_shared("w1"):
        pass
    assert fake.closed is True
