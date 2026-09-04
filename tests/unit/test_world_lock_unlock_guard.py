"""world_lock / workspace_file_lock の unlock が best-effort であること（RV HIGH round-3・R3・2026-07-14）。

PG 断で `pg_advisory_unlock` が例外を投げても、`with` 本体の**元例外を握り潰さない**こと
（session-level advisory lock は接続 close で解放されるため unlock 失敗は無害）。DB 不要＝psycopg.connect を
偽接続に差し替える。"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


class _FakeConn:
    """lock は成功・unlock で例外を投げ・close は記録する偽接続。"""

    def __init__(self):
        self.closed = False

    def execute(self, sql, params=None):
        if "pg_advisory_unlock" in sql:
            raise RuntimeError("unlock-secondary (PG 断・元例外を隠したら NG)")
        return self                                   # pg_advisory_lock は成功

    def close(self):
        self.closed = True


def _patch(monkeypatch):
    from sherpa.store import db
    fake = _FakeConn()
    monkeypatch.setattr(db, "_ensure", lambda: None)
    monkeypatch.setattr(db.psycopg, "connect", lambda *a, **k: fake)
    return db, fake


def test_world_lock_unlock_failure_does_not_mask_body_exception(monkeypatch):
    db, fake = _patch(monkeypatch)
    with pytest.raises(ValueError, match="body-primary"):    # unlock の RuntimeError ではなく本体の ValueError
        with db.world_lock("w1"):
            raise ValueError("body-primary")
    assert fake.closed is True                               # unlock 失敗でも接続は必ず close＝lock 解放


def test_world_lock_unlock_failure_swallowed_on_clean_exit(monkeypatch):
    db, fake = _patch(monkeypatch)
    with db.world_lock("w1"):                                # 本体は正常終了
        pass
    assert fake.closed is True                               # unlock 例外は握り潰され正常に抜ける


def test_workspace_file_lock_unlock_failure_does_not_mask_body_exception(monkeypatch):
    db, fake = _patch(monkeypatch)
    with pytest.raises(ValueError, match="body-primary"):
        with db.workspace_file_lock("u1", "a/b.txt"):
            raise ValueError("body-primary")
    assert fake.closed is True
