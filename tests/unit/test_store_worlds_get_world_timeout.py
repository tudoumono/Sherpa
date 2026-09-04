"""`store.worlds.get_world` の connect_timeout/statement_timeout 配線テスト（DB 不要・RV6/RV7）。

`tests/unit/test_world_lock_shared_guard.py`（`world_lock_shared` の同種テスト）と同じ手法
（偽 psycopg 接続）で、実際に渡される connect kwargs・発行される SQL を検証する。PART-4
（`/ext/v1/research`）のリクエスト全体デッドラインが、この registry 読み取り自体を無期限に
ブロックさせないための配線（`sherpa/store/worlds.py::get_world` docstring 参照）。

RV7: `statement_timeout_ms` は接続 `options`（接続前に固定）ではなく、接続確立**後**に
`SET LOCAL statement_timeout = '{ms}ms'` で発行するよう変更した——`connect_timeout` と
`statement_timeout_ms` に呼び出し元が同じ残り時間 R を渡すため、接続オプションへ両方とも
接続**前**の値で焼き込むと、接続自体に R かかった上でさらに statement_timeout も R 残って
いるかのように振る舞い、実時間が最大で約 2R まで伸びうる（`store.db.world_lock_shared` の
RV5 是正と同型の不具合）。
"""
from __future__ import annotations

import pytest

from sherpa.store import worlds as store_worlds

pytestmark = pytest.mark.unit


class _FakeConn:
    """`fetchone` は None を返す・実行された SQL を記録する偽接続。"""

    def __init__(self):
        self.closed = False
        self.executed: list = []

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True
        return False


def _patch(monkeypatch):
    fake = _FakeConn()
    seen_kwargs: dict = {}

    def fake_connect(**kw):
        # `store.worlds.get_world` は `_connect(**connect_kwargs)` を呼ぶ——`_connect`（db.py）
        # 自身が DSN 解決を内包する `**kw` のみのシグネチャのため、ここでも位置引数は取らない。
        seen_kwargs.update(kw)
        return fake

    monkeypatch.setattr(store_worlds, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_worlds, "_connect", fake_connect)
    return fake, seen_kwargs


def test_get_world_without_timeout_args_passes_no_extra_kwargs(monkeypatch):
    """省略時（既定 None）は connect_timeout を一切渡さず、SET LOCAL statement_timeout も発行しない
    ——既存呼び出し元は無変更。"""
    fake, seen_kwargs = _patch(monkeypatch)
    store_worlds.get_world("w1")
    assert seen_kwargs == {}
    assert not any("statement_timeout" in s for s in fake.executed)


def test_get_world_connect_timeout_rounds_up_and_clamps_to_minimum_one(monkeypatch):
    """psycopg 3.3.4 は `connect_timeout` を整数秒でしか扱わず小数部を切り捨てる（`world_lock_shared`
    と同じ理由）——整数秒へ切り上げ・最小1秒でクランプしてから渡す。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    store_worlds.get_world("w1", connect_timeout=0.3)
    assert seen_kwargs.get("connect_timeout") == 1


def test_get_world_connect_timeout_ceils_fractional_value(monkeypatch):
    _fake, seen_kwargs = _patch(monkeypatch)
    store_worlds.get_world("w1", connect_timeout=2.1)
    assert seen_kwargs.get("connect_timeout") == 3


def test_get_world_statement_timeout_ms_sets_via_set_after_connect(monkeypatch):
    """`statement_timeout_ms` は接続確立**後**に `SET LOCAL statement_timeout = '...'` で発行する
    （接続オプションへは焼き込まない・`options` は使わない）。接続前後で `time.monotonic()` が
    250ms 進んだことにし、`statement_timeout_ms=1500` から差し引いた `1250ms` が使われることを
    固定する（RV7・二重消費の是正）。"""
    fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 100.25])
    monkeypatch.setattr(store_worlds.time, "monotonic", lambda: next(times))
    store_worlds.get_world("w1", statement_timeout_ms=1500)
    assert "options" not in seen_kwargs
    assert any(s == "SET LOCAL statement_timeout = '1250ms'" for s in fake.executed)


def test_get_world_statement_timeout_ms_clamped_to_minimum_one_when_connect_elapsed_exceeds_budget(
        monkeypatch):
    """接続確立だけで `statement_timeout_ms` を使い切って（または超えて）いても、
    `statement_timeout` に 0 を渡さない——Postgres では `statement_timeout=0` は「無効化＝
    無制限待ち」を意味し、デッドライン超過を防ぐはずの値がむしろ無制限待ちへ反転してしまう。
    残り無しは最小 1ms へクランプし、ほぼ即座に打ち切らせる。"""
    fake, _seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 102.0])   # statement_timeout_ms=1500 を超える 2000ms が接続だけで経過
    monkeypatch.setattr(store_worlds.time, "monotonic", lambda: next(times))
    store_worlds.get_world("w1", statement_timeout_ms=1500)
    assert any(s == "SET LOCAL statement_timeout = '1ms'" for s in fake.executed)


def test_get_world_statement_timeout_ms_zero_still_clamped_to_minimum_one(monkeypatch):
    fake, _seen_kwargs = _patch(monkeypatch)
    times = iter([0.0, 0.0])
    monkeypatch.setattr(store_worlds.time, "monotonic", lambda: next(times))
    store_worlds.get_world("w1", statement_timeout_ms=0)
    # 0 は falsy だが明示的に 0 を渡した場合でも None ではないため分岐へ入り、最小 1 へクランプされる。
    assert any(s == "SET LOCAL statement_timeout = '1ms'" for s in fake.executed)


def test_get_world_connect_timeout_deducts_time_consumed_by_ensure(monkeypatch):
    """RV10 是正の固定: `_ensure()`（未初期化時は内部で `init_schema()` の advisory lock 待ち・
    DDL 実行を起動しうる）の消費分も同じ予算から差し引く——差し引かないと、コールドスタート時に
    `_ensure()` が予算を使い切った後も本関数自身の接続へ満額の `connect_timeout` が再付与され、
    実時間が最大約2倍まで伸びうる（`store.db.init_schema` の二重消費是正と同型）。計測開始
    （`budget_started`）を `_ensure()` 呼び出しの**前**に置くことで、`_ensure()` 自体の所要時間
    （ここでは3秒）を差し引いた残り（5-3=2秒）だけが渡ることを固定する。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 103.0])
    monkeypatch.setattr(store_worlds.time, "monotonic", lambda: next(times))
    store_worlds.get_world("w1", connect_timeout=5)
    assert seen_kwargs.get("connect_timeout") == 2


def test_get_world_statement_timeout_ms_deducts_ensure_elapsed_too(monkeypatch):
    """RV10 是正の固定: `statement_timeout_ms` の残り計算も `_ensure()` 呼び出し前からの累積
    経過時間（`_ensure()` 分 + 接続確立分）を差し引く——接続確立の分だけを差し引いていた旧実装
    では、`_ensure()` が消費した分が二重に見逃されていた。"""
    fake, _seen = _patch(monkeypatch)
    times = iter([100.0, 101.0, 101.25])   # _ensure()で1秒・接続確立でさらに250ms
    monkeypatch.setattr(store_worlds.time, "monotonic", lambda: next(times))
    store_worlds.get_world("w1", connect_timeout=5, statement_timeout_ms=2000)
    assert any(s == "SET LOCAL statement_timeout = '750ms'" for s in fake.executed)


def test_get_world_raises_without_connecting_when_budget_exhausted_by_ensure(monkeypatch):
    """RV11 是正の固定: `_ensure()` だけで予算を使い切っていたら、`max(1, math.ceil(remaining))`
    で最低1秒へクランプして新規接続を開始することはせず、接続自体を試みずに `TimeoutError` を
    送出する（従来はクランプにより、既に期限切れでも `_connect()` を呼んでしまっていた）。"""
    connect_calls = {"n": 0}

    def fake_connect(**kw):
        connect_calls["n"] += 1
        raise AssertionError("budget exhausted after _ensure() のはずが接続を試みた")

    monkeypatch.setattr(store_worlds, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_worlds, "_connect", fake_connect)
    times = iter([100.0, 106.0])   # _ensure() だけで connect_timeout=5 を超える6秒が経過したことにする
    monkeypatch.setattr(store_worlds.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError):
        store_worlds.get_world("w1", connect_timeout=5)
    assert connect_calls["n"] == 0
