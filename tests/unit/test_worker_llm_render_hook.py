"""`worker.sync()` の LLM 成形の後追い登録フック（L5・§8.6-4）を pin する。

`_sync_impl`（sync 本体）自体は monkeypatch で置き換え、`sync()` ラッパーが
`llm_render.request_followup` を呼ぶかどうかの配線だけを検証する（sync 本体の分岐は
`tests/unit/test_worker_refresh_branches.py` が別途 pin 済み）。実行は取り込みワーカーが run の後に行う
（`tests/unit/test_ingest_queue.py`・`tests/unit/test_llm_render.py`）。

`SHERPA_TEST_DB_ISOLATED` は `tests/conftest.py` がセッション全体へ常時セットする（テスト用 DB
分離）。`worker.sync()` はこのフラグが立っている間、後追いの登録をしない
（数百のテストから直接呼ばれる `sync()` が、LLM 成形の対象を無条件に溜めると、
他テストとの干渉を引き起こすため）——このフラグを明示的に外すテストだけが「本番相当（フラグ無し）」の配線を検証する。
"""
from __future__ import annotations

from sherpa.ingest import llm_render, worker


def test_sync_registers_llm_render_followup_when_not_isolated(monkeypatch):
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unchanged", "changed": False})
    calls = []
    monkeypatch.setattr(llm_render, "request_followup", lambda world: calls.append(world))

    worker.sync("v1")
    assert calls == ["v1"]


def test_sync_does_not_register_when_test_db_isolated(monkeypatch):
    monkeypatch.setenv("SHERPA_TEST_DB_ISOLATED", "1")
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unchanged", "changed": False})
    calls = []
    monkeypatch.setattr(llm_render, "request_followup", lambda world: calls.append(world))

    worker.sync("v1")
    assert calls == []


def test_sync_does_not_register_when_world_unavailable(monkeypatch):
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unavailable", "changed": False})
    calls = []
    monkeypatch.setattr(llm_render, "request_followup", lambda world: calls.append(world))

    worker.sync("v1")
    assert calls == []


def test_sync_swallows_followup_registration_failure(monkeypatch):
    """後追いの登録そのものの失敗は sync() の戻り値・例外伝播に影響しない（best-effort）。"""
    monkeypatch.delenv("SHERPA_TEST_DB_ISOLATED", raising=False)
    monkeypatch.setattr(worker, "_sync_impl", lambda world, **kw: {"status": "unchanged", "changed": False})

    def _boom(world):
        raise RuntimeError("boom")

    monkeypatch.setattr(llm_render, "request_followup", _boom)
    res = worker.sync("v1")
    assert res == {"status": "unchanged", "changed": False}
