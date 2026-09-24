"""`store.usage_events.add_usage_event` の connect_timeout/statement_timeout 配線テスト
（DB 不要・RV8）。

`tests/unit/test_store_worlds_get_world_timeout.py` と同じ手法（偽 psycopg 接続）で検証する。
`sherpa.metering.record()`（PART-4 の `finally` 節・§8.3）が「記録は試みるが無期限にはブロック
しない」ための固定予算をここへ転送する配線（`add_usage_event`/`metering.record` docstring 参照）。
"""
from __future__ import annotations

import pytest

from sherpa.store import usage_events as store_usage_events

pytestmark = pytest.mark.unit


class _FakeConn:
    def __init__(self):
        self.closed = False
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return self

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def _patch(monkeypatch):
    fake = _FakeConn()
    seen_kwargs: dict = {}

    def fake_connect(**kw):
        seen_kwargs.update(kw)
        return fake

    monkeypatch.setattr(store_usage_events, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_usage_events, "_connect", fake_connect)
    return fake, seen_kwargs


def _add(**kw):
    store_usage_events.add_usage_event(kind="research", provider="ollama", model="qwen2.5", **kw)


def test_add_usage_event_without_timeout_args_passes_no_extra_kwargs(monkeypatch):
    fake, seen_kwargs = _patch(monkeypatch)
    _add()
    assert seen_kwargs == {}
    assert not any("statement_timeout" in s for s in fake.executed)


def test_add_usage_event_connect_timeout_rounds_up_and_clamps_to_minimum_one(monkeypatch):
    _fake, seen_kwargs = _patch(monkeypatch)
    _add(connect_timeout=0.3)
    assert seen_kwargs.get("connect_timeout") == 1


def test_add_usage_event_statement_timeout_ms_sets_via_set_after_connect(monkeypatch):
    fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 100.25])
    monkeypatch.setattr(store_usage_events.time, "monotonic", lambda: next(times))
    _add(statement_timeout_ms=1500)
    assert "options" not in seen_kwargs
    assert any(s == "SET LOCAL statement_timeout = '1250ms'" for s in fake.executed)


def test_add_usage_event_statement_timeout_ms_clamped_to_minimum_one_when_connect_elapsed_exceeds_budget(
        monkeypatch):
    fake, _seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 102.0])
    monkeypatch.setattr(store_usage_events.time, "monotonic", lambda: next(times))
    _add(statement_timeout_ms=1500)
    assert any(s == "SET LOCAL statement_timeout = '1ms'" for s in fake.executed)


def test_add_usage_event_connect_timeout_deducts_time_consumed_by_ensure(monkeypatch):
    """RV10 是正の固定: `_ensure()`（未初期化時は内部で `init_schema()` を起動しうる）の消費分も
    同じ予算から差し引く（`store.worlds.get_world`/`store.db.init_schema` と同型の是正）。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 103.0])
    monkeypatch.setattr(store_usage_events.time, "monotonic", lambda: next(times))
    _add(connect_timeout=5)
    assert seen_kwargs.get("connect_timeout") == 2


def test_add_usage_event_statement_timeout_ms_deducts_ensure_elapsed_too(monkeypatch):
    """RV10 是正の固定: `statement_timeout_ms` の残り計算も `_ensure()` 呼び出し前からの累積
    経過時間（`_ensure()` 分 + 接続確立分）を差し引く。"""
    fake, _seen = _patch(monkeypatch)
    times = iter([100.0, 101.0, 101.25])
    monkeypatch.setattr(store_usage_events.time, "monotonic", lambda: next(times))
    _add(connect_timeout=5, statement_timeout_ms=2000)
    assert any(s == "SET LOCAL statement_timeout = '750ms'" for s in fake.executed)


def test_add_usage_event_raises_without_connecting_when_budget_exhausted_by_ensure(monkeypatch):
    """RV11 是正の固定: `_ensure()` だけで予算を使い切っていたら、最低1秒へクランプして新規接続を
    開始することはせず、接続自体を試みずに `TimeoutError` を送出する（`metering.record()` の
    自前チェックとは独立に、本関数自身でも防ぐ多層防御）。"""
    connect_calls = {"n": 0}

    def fake_connect(**kw):
        connect_calls["n"] += 1
        raise AssertionError("budget exhausted after _ensure() のはずが接続を試みた")

    monkeypatch.setattr(store_usage_events, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_usage_events, "_connect", fake_connect)
    times = iter([100.0, 106.0])
    monkeypatch.setattr(store_usage_events.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError):
        _add(connect_timeout=5)
    assert connect_calls["n"] == 0
