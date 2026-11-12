"""Codex ジョブ: Codex が結論を返さず終わったとき、ジョブを `completed` にしない（要 Postgres・偽 codex は外部境界）。"""
from __future__ import annotations

import pytest

from sherpa.store import codex_jobs as store_jobs
import test_ext_codex_jobs as base
from test_ext_codex_jobs import _db, _keyed, _result, _status, _submit_job  # noqa: F401  (autouse fixture を再利用)

_SILENT_CODEX = r'''#!/usr/bin/env python3
import json
import sys

args = sys.argv[1:]
if args[:2] == ["login", "status"]:
    print("Logged in")
    sys.exit(0)
sys.stdin.read()
sys.exit(1)
'''


@pytest.fixture
def silent_codex(tmp_path, monkeypatch):
    monkeypatch.setattr(base, "_FAKE_CODEX_PY", _SILENT_CODEX)
    gen = base.fake_codex.__wrapped__(tmp_path, monkeypatch) if hasattr(base.fake_codex, "__wrapped__") else None
    if gen is None:
        pytest.skip("fixture wrapper unavailable")
    next(gen)
    try:
        yield
    finally:
        next(gen, None)


def test_silent_codex_job_is_failed_not_completed(silent_codex):
    issued = _keyed("cj-silent", with_destination=False)
    job_id = _submit_job(issued["key"], "沈黙")
    job = base._dispatch_and_wait(job_id)
    assert job["status"] == "failed"
    assert job["completion"] == "failed" and job["stop_kind"] == "codex_silent"
    r = _result(job_id, issued["key"])
    assert r.status_code == 409
    assert r.json()["status"] == "failed" and r.json()["stop_kind"] == "codex_silent"
    assert _status(job_id, issued["key"]).json()["completion"] == "failed"


def test_partial_answer_is_saved_as_partial_and_exposed():
    issued = _keyed("cj-partial", with_destination=False)
    job_id = store_jobs.new_job_id()
    store_jobs.insert_job(job_id=job_id, key_id=issued["id"], world="v1", query="途中", scope_paths=[], depth="standard")
    assert job_id in [j["id"] for j in store_jobs.claim_queued_jobs(1)]
    store_jobs.mark_completed(job_id, answer="ここまで", sources=[], unconfirmed_items=[], elapsed_ms=5,
                              completion="partial", stop_kind="codex_partial")
    body = _result(job_id, issued["key"]).json()
    assert body["completion"] == "partial" and body["stop_kind"] == "codex_partial"
    st = _status(job_id, issued["key"]).json()
    assert st["status"] == "completed" and st["completion"] == "partial"
    from sherpa import webhooks
    assert webhooks.codex_job_payload(job_id, "completed", None, completion="partial",
                                      stop_kind="codex_partial")["completion"] == "partial"
