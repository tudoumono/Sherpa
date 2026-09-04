"""`POST /worlds/{wid}/rag_regenerate_rules`（L5・§8.6-2「規則版で再生成」・ING-3 即受付）の API 層テスト。

401/403 は `test_authz_matrix.py`（POLICY 表に追加済み）が担保するため対象外。ここでは 404/503/202
の業務ロジックと、背景実行本体（`ingest_worker.regenerate_rag_rule_only`）の成否が受付 run（同一
run_id）へ正しく terminal 化されることを検証する（`test_world_extract_ingest_run.py` と同じ
hermetic な流儀・背景スレッドの完走は `background.is_running` のポーリングで待つ）。
"""
from __future__ import annotations

import time
from contextlib import contextmanager

import pytest
from fastapi.testclient import TestClient

from sherpa.ingest import background


@pytest.fixture
def client(auth_disabled):
    from sherpa.api import app
    return TestClient(app, raise_server_exceptions=False)


@contextmanager
def _fake_world_lock(wid, timeout_ms=None):
    yield


def _wait_idle(wid: str, timeout: float = 2.0) -> None:
    deadline = time.monotonic() + timeout
    while background.is_running(wid) and time.monotonic() < deadline:
        time.sleep(0.01)


def _common_stubs(monkeypatch, *, run_id: int, world_dir="/tmp/fake-world-dir") -> None:
    from sherpa import store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: world_dir)
    monkeypatch.setattr(store, "world_lock", _fake_world_lock)
    monkeypatch.setattr(store, "start_ingest_run", lambda *a, **k: {"id": run_id})
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda *a, **k: None)
    monkeypatch.setattr(store, "fail_close_if_extracting", lambda run_id, reason: False)


def test_unknown_world_returns_404(client, monkeypatch):
    from sherpa import store
    monkeypatch.setattr(store, "get_world", lambda wid: None)
    r = client.post("/worlds/w1/rag_regenerate_rules")
    assert r.status_code == 404, r.text


def test_unreachable_root_returns_503(client, monkeypatch):
    from sherpa import store, worlds
    monkeypatch.setattr(store, "get_world", lambda wid: {"world_id": wid})
    monkeypatch.setattr(worlds, "world_dir", lambda wid: None)
    r = client.post("/worlds/w1/rag_regenerate_rules")
    assert r.status_code == 503, r.text


def test_success_accepted_and_finalizes_run(client, monkeypatch):
    from sherpa import store
    from sherpa.ingest import worker as ingest_worker

    _common_stubs(monkeypatch, run_id=701)
    monkeypatch.setattr(ingest_worker, "regenerate_rag_rule_only",
                        lambda wid: {"status": "ok", "rag_generated": 5, "rag_failed": 0})
    recorded = []
    monkeypatch.setattr(store, "finish_ingest_run",
                        lambda run_id, **k: recorded.append({"run_id": run_id, **k}) or {"id": run_id})

    r = client.post("/worlds/w1/rag_regenerate_rules")
    assert r.status_code == 202, r.text
    body = r.json()
    assert body["ok"] is True and body["run_id"] == 701 and body["joined"] is False

    _wait_idle("w1")
    assert len(recorded) == 1
    assert recorded[0]["run_id"] == 701
    assert recorded[0]["status"] == "auto_published"
    assert recorded[0]["extraction_snapshot"]["docs"] == 5


def test_background_failure_status_finalizes_run_as_failed(client, monkeypatch):
    from sherpa import store
    from sherpa.ingest import worker as ingest_worker

    _common_stubs(monkeypatch, run_id=702)
    monkeypatch.setattr(ingest_worker, "regenerate_rag_rule_only",
                        lambda wid: {"status": "es_reindex_failed", "rag_generated": 2})
    recorded = []
    monkeypatch.setattr(store, "finish_ingest_run",
                        lambda run_id, **k: recorded.append({"run_id": run_id, **k}) or {"id": run_id})

    r = client.post("/worlds/w1/rag_regenerate_rules")
    assert r.status_code == 202, r.text

    _wait_idle("w1")
    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed"
    assert recorded[0]["extraction_snapshot"]["flags"] == [
        {"doc": None, "action": "blocked", "reason": "es_reindex_failed"}]


def test_background_unexpected_exception_finalizes_run_as_failed(client, monkeypatch):
    from sherpa import store
    from sherpa.ingest import worker as ingest_worker

    _common_stubs(monkeypatch, run_id=703)

    def _boom(wid):
        raise RuntimeError("boom")
    monkeypatch.setattr(ingest_worker, "regenerate_rag_rule_only", _boom)
    recorded = []
    monkeypatch.setattr(store, "finish_ingest_run",
                        lambda run_id, **k: recorded.append({"run_id": run_id, **k}) or {"id": run_id})

    r = client.post("/worlds/w1/rag_regenerate_rules")
    assert r.status_code == 202, r.text

    _wait_idle("w1")
    assert len(recorded) == 1
    assert recorded[0]["status"] == "failed"
    assert recorded[0]["extraction_snapshot"]["flags"][0]["reason"] == "unexpected_error:RuntimeError"
