"""`store.settings.get_system_settings` の connect_timeout/statement_timeout 配線テスト
（DB 不要・RV8）。

`tests/unit/test_store_worlds_get_world_timeout.py`（`get_world` の同種テスト）と全く同じ手法
（偽 psycopg 接続）で、実際に渡される connect kwargs・発行される SQL を検証する。PART-4
（`/ext/v1/research`）のリクエスト全体デッドラインが、この設定読み取り自体を無期限に
ブロックさせないための配線（`sherpa/store/settings.py::get_system_settings` docstring 参照）。
"""
from __future__ import annotations

import pytest

from sherpa.store import settings as store_settings

pytestmark = pytest.mark.unit


class _FakeConn:
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

    # キャッシュが命中すると DB へ到達しないため、各テストで必ずミスするよう毎回クリアする。
    monkeypatch.setattr(store_settings, "_system_settings_cache", None)
    monkeypatch.setattr(store_settings, "_system_settings_cache_ts", 0.0)
    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", fake_connect)
    return fake, seen_kwargs


def test_get_system_settings_without_timeout_args_passes_no_extra_kwargs(monkeypatch):
    """省略時（既定 None）は connect_timeout を一切渡さず、SET LOCAL statement_timeout も発行しない
    ——既存呼び出し元は無変更。"""
    fake, seen_kwargs = _patch(monkeypatch)
    store_settings.get_system_settings()
    assert seen_kwargs == {}
    assert not any("statement_timeout" in s for s in fake.executed)


def test_get_system_settings_connect_timeout_rounds_up_and_clamps_to_minimum_one(monkeypatch):
    _fake, seen_kwargs = _patch(monkeypatch)
    store_settings.get_system_settings(connect_timeout=0.3)
    assert seen_kwargs.get("connect_timeout") == 1


def test_get_system_settings_statement_timeout_ms_sets_via_set_after_connect(monkeypatch):
    """`statement_timeout_ms` は接続確立**後**に `SET LOCAL statement_timeout = '...'` で発行する
    （`store.worlds.get_world` と同じ理由・同じ方式）。接続前後で `time.monotonic()` が
    250ms 進んだことにし、`statement_timeout_ms=1500` から差し引いた `1250ms` が使われることを
    固定する。"""
    fake, seen_kwargs = _patch(monkeypatch)
    # 1回目はキャッシュ判定用（`_system_settings_cache` が None のため値自体は無関係）・
    # 2回目が接続直前・3回目が接続後の経過計測。
    times = iter([0.0, 100.0, 100.25])
    monkeypatch.setattr(store_settings.time, "monotonic", lambda: next(times))
    store_settings.get_system_settings(statement_timeout_ms=1500)
    assert "options" not in seen_kwargs
    assert any(s == "SET LOCAL statement_timeout = '1250ms'" for s in fake.executed)


def test_get_system_settings_connect_timeout_deducts_time_consumed_by_ensure(monkeypatch):
    """RV10 是正の固定: `_ensure()`（未初期化時は内部で `init_schema()` を起動しうる）の消費分も
    同じ予算から差し引く——差し引かないと、コールドスタート時に `_ensure()` が予算を使い切った
    後も本関数自身の接続へ満額の `connect_timeout` が再付与され、実時間が最大約2倍まで伸びうる
    （`store.worlds.get_world`/`store.db.init_schema` と同型の是正）。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    # 1回目=キャッシュ判定用・2回目=`_ensure()` 呼び出し直前・3回目=`_ensure()` 後（3秒経過）。
    times = iter([0.0, 100.0, 103.0])
    monkeypatch.setattr(store_settings.time, "monotonic", lambda: next(times))
    store_settings.get_system_settings(connect_timeout=5)
    assert seen_kwargs.get("connect_timeout") == 2


def test_get_system_settings_statement_timeout_ms_deducts_ensure_elapsed_too(monkeypatch):
    """RV10 是正の固定: `statement_timeout_ms` の残り計算も `_ensure()` 呼び出し前からの累積
    経過時間（`_ensure()` 分 + 接続確立分）を差し引く。"""
    fake, _seen = _patch(monkeypatch)
    times = iter([0.0, 100.0, 101.0, 101.25])   # _ensure()で1秒・接続確立でさらに250ms
    monkeypatch.setattr(store_settings.time, "monotonic", lambda: next(times))
    store_settings.get_system_settings(connect_timeout=5, statement_timeout_ms=2000)
    assert any(s == "SET LOCAL statement_timeout = '750ms'" for s in fake.executed)


def test_get_system_settings_cache_hit_skips_db_and_ignores_timeout_args(monkeypatch):
    """キャッシュ命中時は DB I/O が発生しないため、timeout 引数を渡しても無視される
    （渡すこと自体は無害・docstring の契約）。"""
    fake, seen_kwargs = _patch(monkeypatch)
    monkeypatch.setattr(store_settings, "_system_settings_cache", {"k": "v"})
    monkeypatch.setattr(store_settings, "_system_settings_cache_ts", store_settings.time.monotonic())
    result = store_settings.get_system_settings(connect_timeout=1.0, statement_timeout_ms=500)
    assert result == {"k": "v"}
    assert seen_kwargs == {}
    assert fake.executed == []


# ===== WEB-1: get_provider() の唯一の読取点は共有キャッシュに触れない fresh read
#      （DB 停止直後の fail-open 窓・並行ターンの TOCTOU） =====

def test_read_system_settings_fresh_ignores_concurrently_rewarmed_cache(monkeypatch):
    """回帰（TOCTOU）: A（このターン）が読もうとする直前に、並行ターン B が DB から読み直して
    共有キャッシュを再加熱していても（例: `web_search_allowed=True`）、`_read_system_settings_fresh()`
    はそのキャッシュを一切参照しないため、実際に DB へ触れに行く。その時点で DB が落ちていれば
    再加熱された値を拾わず正直に例外を送出する（fail-closed）——「invalidate してから
    `get_system_settings()` を呼ぶ」方式だと、invalidate 直後に B が再加熱した場合にその値を
    拾ってしまう TOCTOU があり、`providers.get_provider()` がこの関数を直接呼ぶ理由。"""
    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", _boom)

    # B（並行ターン）が直前に DB から読み直してキャッシュを再加熱した状態を模す
    # （内容は無関係——「参照されないこと」自体がこのテストの対象）。
    monkeypatch.setattr(store_settings, "_system_settings_cache", {"web_search_allowed": True})
    monkeypatch.setattr(store_settings, "_system_settings_cache_ts", store_settings.time.monotonic())

    with pytest.raises(RuntimeError):
        store_settings._read_system_settings_fresh()
    # 再加熱されたキャッシュ自体は書き換えていない（参照だけでなく更新もしない契約）。
    assert store_settings._system_settings_cache == {"web_search_allowed": True}


def test_get_provider_uses_fresh_read_so_db_outage_fails_closed_despite_warm_cache(monkeypatch):
    """`get_system_settings()` を素朴に呼べば TTL 内はキャッシュ済みの値を返し続ける
    （fail-open）。`providers.get_provider()` はこの1ターン唯一の読取点で
    `store._read_system_settings_fresh()` を直接呼ぶため、キャッシュが温まっていても DB 断が
    そのまま例外として伝播する（fail-closed・外部実行＝Codex 等へ踏み切らない）。

    `tests/unit/conftest.py::_hermetic_system_settings`（autouse）が既定で
    `sherpa.store._read_system_settings_fresh` を `lambda **kw: {}` へ据えているため、本テストは
    その既定を本物の関数へ明示的に戻してから検証する（同 fixture の docstring が案内する
    「本体内の明示的 monkeypatch が最後に効く」契約どおり）。"""
    from sherpa import store
    from sherpa.providers import get_provider

    def _boom(**kw):
        raise RuntimeError("db down")

    monkeypatch.setattr(store, "_read_system_settings_fresh", store_settings._read_system_settings_fresh)
    monkeypatch.setattr(store_settings, "_system_settings_cache", {"web_search_allowed": True})
    monkeypatch.setattr(store_settings, "_system_settings_cache_ts", store_settings.time.monotonic())
    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", _boom)

    # 素朴な呼び出しは温めたキャッシュをそのまま返す＝DB 断に気付かない（fail-open の現状・
    # `get_provider()` がこれを直接呼ばない理由）。キャッシュ命中は `_system_settings_cache_ts`
    # を更新しないため、この呼び出し自体はまだ DB に触れていない。
    assert store_settings.get_system_settings() == {"web_search_allowed": True}

    # `get_provider()` は `_read_system_settings_fresh()` を直接呼ぶため、キャッシュが温まって
    # いても必ず DB へ触れに行き、接続失敗をそのまま例外にする（stale な True を黙って使って
    # provider を組み立てない）。
    with pytest.raises(RuntimeError):
        get_provider({})


def test_get_system_settings_raises_without_connecting_when_budget_exhausted_by_ensure(monkeypatch):
    """RV11 是正の固定: `_ensure()` だけで予算を使い切っていたら、最低1秒へクランプして新規接続を
    開始することはせず、接続自体を試みずに `TimeoutError` を送出する。"""
    connect_calls = {"n": 0}

    def fake_connect(**kw):
        connect_calls["n"] += 1
        raise AssertionError("budget exhausted after _ensure() のはずが接続を試みた")

    monkeypatch.setattr(store_settings, "_system_settings_cache", None)
    monkeypatch.setattr(store_settings, "_system_settings_cache_ts", 0.0)
    monkeypatch.setattr(store_settings, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_settings, "_connect", fake_connect)
    # 1回目=キャッシュ判定用・2回目=`_ensure()` 呼び出し直前・3回目=`_ensure()` 後（6秒経過）。
    times = iter([0.0, 100.0, 106.0])
    monkeypatch.setattr(store_settings.time, "monotonic", lambda: next(times))
    with pytest.raises(TimeoutError):
        store_settings.get_system_settings(connect_timeout=5)
    assert connect_calls["n"] == 0
