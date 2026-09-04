"""`worker.sync()` の LLM 成形バックグラウンド起動フック（L5・§8.6-4）を pin する。

`_sync_impl`（sync 本体）自体は monkeypatch で置き換え、`sync()` ラッパーが
`llm_render.schedule_background` を呼ぶかどうかの配線だけを検証する（sync 本体の分岐は
`tests/unit/test_worker_refresh_branches.py` が別途 pin 済み）。

`SHERPA_TEST_DB_ISOLATED` は `tests/conftest.py` がセッション全体へ常時セットする（テスト用 DB
分離）。`worker.sync()` はこのフラグが立っている間バックグラウンド起動そのものをしない
（本ファイルの docstring どおり、数百のテストから直接呼ばれる `sync()` に daemon thread を
無条件で仕込むと、実 DB 読み取りの追加や他テストとのファイル競合を引き起こすため）——
このフラグを明示的に外すテストだけが「本番相当（フラグ無し）」の配線を検証する。
"""
from __future__ import annotations

from sherpa.ingest import llm_render, worker


def test_sync_schedules_llm_render_background_when_not_isolated(monkeypatch):
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unchanged", "changed": False})
    calls = []
    monkeypatch.setattr(llm_render, "schedule_background", lambda world, fn: calls.append(world) or True)

    worker.sync("v1")
    assert calls == ["v1"]


def test_sync_does_not_schedule_when_test_db_isolated(monkeypatch):
    monkeypatch.setenv("SHERPA_TEST_DB_ISOLATED", "1")
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unchanged", "changed": False})
    calls = []
    monkeypatch.setattr(llm_render, "schedule_background", lambda world, fn: calls.append(world) or True)

    worker.sync("v1")
    assert calls == []


def test_sync_does_not_schedule_when_world_unavailable(monkeypatch):
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unavailable", "changed": False})
    calls = []
    monkeypatch.setattr(llm_render, "schedule_background", lambda world, fn: calls.append(world) or True)

    worker.sync("v1")
    assert calls == []


def test_sync_swallows_schedule_background_failure(monkeypatch):
    """背景起動そのものの失敗は sync() の戻り値・例外伝播に影響しない（best-effort）。"""
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unchanged", "changed": False})

    def _boom(world, fn):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_render, "schedule_background", _boom)
    res = worker.sync("v1")
    assert res == {"status": "unchanged", "changed": False}


def test_sync_passes_correct_work_fn(monkeypatch):
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unchanged", "changed": False})
    captured = {}

    def _capture(world, fn):
        captured["world"] = world
        captured["fn"] = fn
        return True

    monkeypatch.setattr(llm_render, "schedule_background", _capture)
    worker.sync("v1")
    assert captured["world"] == "v1"
    assert captured["fn"] is worker._llm_render_pass
