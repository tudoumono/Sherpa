"""`store.ingest.get_latest_run_summary` の connect_timeout/statement_timeout 配線テスト（DB 不要）。

RV2是正#a3: `corpus_docs.last_run_flags` は `world_lock_shared`（`doc_ledger.public_documents_page`
経由の共有ロック）保持中に呼ばれうるため、`source_doc_ids`（world の全文書名を持つ重い JSONB 配列）
を読む `list_ingest_runs` の代わりにこの狭い SELECT を使うよう切り替えた。`tests/unit/
test_store_ingest_list_runs_timeout.py`（`list_ingest_runs` の同種テスト）と同じ手法（偽 psycopg
接続）で、connect kwargs・発行される SQL・`source_doc_ids` を選択しないことを直接固定する。
"""
from __future__ import annotations

import pytest

from sherpa.store import ingest as store_ingest

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
        seen_kwargs.update(kw)
        return fake

    monkeypatch.setattr(store_ingest, "_ensure", lambda **kw: None)
    monkeypatch.setattr(store_ingest, "_connect", fake_connect)
    return fake, seen_kwargs


def test_get_latest_run_summary_select_excludes_source_doc_ids(monkeypatch):
    """発行される SQL に `source_doc_ids` が含まれない（world 全文書名の重い JSONB 配列を
    共有ロック区間へ持ち込まない・RV2是正#a3 の核）。"""
    fake, _seen_kwargs = _patch(monkeypatch)
    store_ingest.get_latest_run_summary("w1")
    assert len(fake.executed) == 1
    assert "source_doc_ids" not in fake.executed[0]
    assert "SELECT id, status, extraction_snapshot, progress, created_at" in fake.executed[0]


def test_get_latest_run_summary_without_timeout_args_passes_no_extra_kwargs(monkeypatch):
    """省略時（既定 None）は connect_timeout を一切渡さず、SET LOCAL statement_timeout も発行しない
    ——既存呼び出し元（`routers/worlds.py::_ingest_summary`）は無変更。"""
    fake, seen_kwargs = _patch(monkeypatch)
    store_ingest.get_latest_run_summary("w1")
    assert seen_kwargs == {}
    assert not any("statement_timeout" in s for s in fake.executed)


def test_get_latest_run_summary_connect_timeout_rounds_up_and_clamps_to_minimum_one(monkeypatch):
    """psycopg は `connect_timeout` を整数秒でしか扱わない——整数秒へ切り上げ・最小1秒でクランプ
    してから渡す（`list_ingest_runs`/`get_world_status_row` と同じ理由）。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    store_ingest.get_latest_run_summary("w1", connect_timeout=0.3)
    assert seen_kwargs.get("connect_timeout") == 1


def test_get_latest_run_summary_statement_timeout_ms_sets_via_set_after_connect_and_deducts_elapsed(
        monkeypatch):
    """`statement_timeout_ms` は接続確立**後**に `SET LOCAL statement_timeout = '...'` で発行し、
    接続確立に要した経過時間ぶんを差し引く（`corpus_docs.last_run_flags` が同じ残り時間を
    `connect_timeout`/`statement_timeout_ms` へ渡す前提と同じ・二重消費を避ける）。"""
    fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 100.25])
    monkeypatch.setattr(store_ingest.time, "monotonic", lambda: next(times))
    store_ingest.get_latest_run_summary("w1", statement_timeout_ms=1500)
    assert "options" not in seen_kwargs
    assert any(s == "SET LOCAL statement_timeout = '1250ms'" for s in fake.executed)


def test_get_latest_run_summary_statement_timeout_ms_clamped_to_minimum_one_when_elapsed_exceeds_budget(
        monkeypatch):
    """接続確立だけで `statement_timeout_ms` を使い切って（または超えて）いても、
    `statement_timeout` に 0 を渡さない（Postgres では 0 は無制限待ちを意味する）——最小 1ms へ
    クランプし、ほぼ即座に打ち切らせる。"""
    fake, _seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 102.0])   # statement_timeout_ms=1500 を超える 2000ms が接続だけで経過
    monkeypatch.setattr(store_ingest.time, "monotonic", lambda: next(times))
    store_ingest.get_latest_run_summary("w1", statement_timeout_ms=1500)
    assert any(s == "SET LOCAL statement_timeout = '1ms'" for s in fake.executed)


def test_get_latest_run_summary_connect_timeout_deducts_time_consumed_by_ensure(monkeypatch):
    """`_ensure()` の消費分も同じ予算から差し引く（`list_ingest_runs`/`get_world_status_row` と
    同じ理由）——計測開始を `_ensure()` 呼び出し**前**に置き、`_ensure()` 自体の所要（3秒）を
    差し引いた残り（5-3=2秒）だけが渡ることを固定する。"""
    _fake, seen_kwargs = _patch(monkeypatch)
    times = iter([100.0, 103.0])
    monkeypatch.setattr(store_ingest.time, "monotonic", lambda: next(times))
    store_ingest.get_latest_run_summary("w1", connect_timeout=5)
    assert seen_kwargs.get("connect_timeout") == 2


def test_get_latest_run_summary_raises_without_connecting_when_budget_exhausted_by_ensure(monkeypatch):
    """`_ensure()` だけで予算を使い切っていたら、接続自体を試みずに `TimeoutError` を送出する
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
        store_ingest.get_latest_run_summary("w1", connect_timeout=5)
    assert connect_calls["n"] == 0
