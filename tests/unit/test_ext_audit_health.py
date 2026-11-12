"""外部 API の監査行を書けなかった件数が数えられ、管理者の監査ログ応答に出る。"""
from __future__ import annotations

import asyncio

from sherpa import ext_audit_health
from sherpa.ext import audit


def _failing_write(pending, status_code, duration_ms, method, path, request_id):
    raise RuntimeError("db down")


def test_failed_audit_write_is_counted(monkeypatch):
    monkeypatch.setattr(audit, "_write_pending_audit", _failing_write)
    writer = audit._AuditWriter(maxsize=10)
    monkeypatch.setattr(audit, "_audit_writer", writer)
    parked = []
    monkeypatch.setattr(audit.outbox, "add", lambda kind, payload, key_id=None: parked.append((kind, payload)))
    before = ext_audit_health.failure_count()
    pending = audit._init_audit_pending("ext_api.search", "ext_search", "ext:health-test")
    try:
        asyncio.run(audit._write_pending_audit_async(pending, 200, 1.0, "GET", "/x", "rid-health"))
    finally:
        writer.stop(drain_timeout=2)
    assert ext_audit_health.failure_count() == before + 1
    assert [k for k, _ in parked] == ["ext_audit"]   # 書けなかった行は捨てずに配送待ちへ残す


def test_admin_audit_response_carries_failure_count(monkeypatch):
    from sherpa.routers import audit_usage

    monkeypatch.setattr(audit_usage, "_current_user", lambda r: {"uid": "admin", "role": "admin"})
    monkeypatch.setattr(audit_usage, "_require_admin", lambda u: None)
    monkeypatch.setattr(audit_usage.store, "list_audit", lambda **kw: [])
    monkeypatch.setattr(audit_usage.store, "audit", lambda *a, **kw: None)
    monkeypatch.setattr(ext_audit_health, "failure_count", lambda: 3)
    body = audit_usage.admin_audit_list(
        request=None, actor=None, action=None, resource_type=None, resource_id=None, outcome=None,
        severity=None, time_from=None, time_to=None, request_id=None, limit=100, offset=0)
    assert body["ext_audit_write_failures"] == 3
