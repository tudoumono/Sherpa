"""`store.ingest.list_ingest_runs` の connect_timeout/statement_timeout 配線テスト（DB 不要）。

`tests/unit/test_store_worlds_get_world_timeout.py`（`get_world` の同種テスト）と同じ手法
（偽 psycopg 接続）で、実際に渡される connect kwargs・発行される SQL を検証する。
`corpus_docs.last_run_flags`（`doc_ledger.documents_for` の list_docs ツール打切り契約）が
残り時間ベースで渡す先——0以下への接続前打切り・`_ensure()`/接続確立の経過時間控除・
`statement_timeout=0`（Postgres では無制限の意味）を避ける最小1msクランプを直接固定する。
"""
from __future__ import annotations

import pytest

from sherpa.store import ingest as store_ingest

pytestmark = pytest.mark.unit


class _FakeConn:
    """`fetchall` は空リストを返す・実行された SQL を記録する偽接続。"""

    def __init__(self):
        self.closed = False
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return self

    def fetchall(self):
        return []

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

    monkeypatch.setattr(store_ingest, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_ingest, "_connect", fake_connect)
    return fake, seen_kwargs


def test_list_ingest_runs_without_timeout_args_passes_no_extra_kwargs(monkeypatch):
    """省略時（既定 None）は connect_timeout を一切渡さず、SET LOCAL statement_timeout も発行しない
    ——既存呼び出し元は無変更。"""
    fake, seen_kwargs = _patch(monkeypatch)
    store_ingest.list_ingest_runs("w1", limit=1)
    assert seen_kwargs == {}
    assert not any("statement_timeout" in s for s in fake.executed)


def test_list_ingest_runs_connect_timeout_rounds_up_and_clamps_to_minimum_one(monkeypatch):
    """psycopg は `connect_timeout` を整数秒でしか扱わない——整数秒へ切り上げ・最小1秒でクランプ
    してから渡す（`store.worlds.get_world` と同じ理由）。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    store_ingest.list_ingest_runs("w1", connect_timeout=0.3)
    assert seen_kwargs.get("connect_timeout") == 1


def test_list_ingest_runs_statement_timeout_ms_sets_via_set_after_connect_and_deducts_elapsed(
        monkeypatch):
    """`statement_timeout_ms` は接続確立**後**に `SET LOCAL statement_timeout = '...'` で発行し、
    接続確立に要した経過時間ぶんを差し引く——`connect_timeout`/`statement_timeout_ms` に同じ
    残り時間を渡す呼び出し元（`corpus_docs.last_run_flags`）が、接続分と統計分で二重に消費
    しないようにする。接続前後で `time.monotonic()` が250ms進んだことにし、
    `statement_timeout_ms=1500` から差し引いた `1250ms` が使われることを固定する。"""
    fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 100.25])
    monkeypatch.setattr(store_ingest.time, "monotonic", lambda: next(times))
    store_ingest.list_ingest_runs("w1", statement_timeout_ms=1500)
    assert "options" not in seen_kwargs
    assert any(s == "SET LOCAL statement_timeout = '1250ms'" for s in fake.executed)


def test_list_ingest_runs_statement_timeout_ms_clamped_to_minimum_one_when_elapsed_exceeds_budget(
        monkeypatch):
    """接続確立だけで `statement_timeout_ms` を使い切って（または超えて）いても、
    `statement_timeout` に 0 を渡さない——Postgres では `statement_timeout=0` は「無制限待ち」を
    意味し、デッドライン超過を防ぐはずの値がむしろ無制限待ちへ反転してしまう。残り無しは
    最小 1ms へクランプし、ほぼ即座に打ち切らせる。"""
    fake, _seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 102.0])   # statement_timeout_ms=1500 を超える 2000ms が接続だけで経過
    monkeypatch.setattr(store_ingest.time, "monotonic", lambda: next(times))
    store_ingest.list_ingest_runs("w1", statement_timeout_ms=1500)
    assert any(s == "SET LOCAL statement_timeout = '1ms'" for s in fake.executed)


def test_list_ingest_runs_connect_timeout_deducts_time_consumed_by_ensure(monkeypatch):
    """`_ensure()`（未初期化時は内部で `init_schema()` の advisory lock 待ち・DDL 実行を起動しうる）
    の消費分も同じ予算から差し引く——差し引かないと、コールドスタート時に `_ensure()` が予算を
    使い切った後も本関数自身の接続へ満額の `connect_timeout` が再付与され、実時間が最大約2倍まで
    伸びうる。計測開始（`budget_started`）を `_ensure()` 呼び出しの**前**に置くことで、`_ensure()`
    自体の所要時間（ここでは3秒）を差し引いた残り（5-3=2秒）だけが渡ることを固定する。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 103.0])
    monkeypatch.setattr(store_ingest.time, "monotonic", lambda: next(times))
    store_ingest.list_ingest_runs("w1", connect_timeout=5)
    assert seen_kwargs.get("connect_timeout") == 2


def test_list_ingest_runs_raises_without_connecting_when_budget_exhausted_by_ensure(monkeypatch):
    """`_ensure()` だけで予算を使い切っていたら、`max(1, math.ceil(remaining))` で最低1秒へ
    クランプして新規接続を開始することはせず、接続自体を試みずに `TimeoutError` を送出する
    （クランプにより、既に期限切れでも `_connect()` を呼んでしまう抜け穴を塞ぐ）。"""
    connect_calls = {"n": 0}

    def fake_connect(**kw):
        connect_calls["n"] += 1
        raise AssertionError("budget exhausted after _ensure() のはずが接続を試みた")

    monkeypatch.setattr(store_ingest, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_ingest, "_connect", fake_connect)
    times = iter([100.0, 106.0])   # _ensure() だけで connect_timeout=5 を超える6秒が経過したことにする
    monkeypatch.setattr(store_ingest.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError):
        store_ingest.list_ingest_runs("w1", connect_timeout=5)
    assert connect_calls["n"] == 0
