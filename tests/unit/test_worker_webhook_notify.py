"""PART-6: 取り込み run の terminal 化から `webhooks.notify_run_terminal` が呼ばれる配線テスト。

`docs/proposals/2026-09-05-Webhook通知.md` の発火点契約（`worker._record` の確定パス＋
`_sync_impl._finalize_if_unused`）を、DB/ネットワーク不要のスタブ実行で検証する
（`tests/unit/test_ingest_worker_flags.py` と同じ構成のスタブ流儀）。実際の HTTP 送信・
宛先検証・リトライは `tests/unit/test_webhooks.py` の担当——ここでは「正しい引数で
呼ばれるか」だけを見る。
"""
from __future__ import annotations

import contextlib

import pytest

from sherpa import corpus_docs, store, webhooks
from sherpa.ingest import world_neo4j, worker


@pytest.fixture(autouse=True)
def _stub_pipeline(monkeypatch):
    """`worker.run`/`worker.sync` を DB/Neo4j 無しで駆動できるよう周辺を差し替える（happy path 既定・
    `test_ingest_worker_flags.py::_stub_pipeline` と同型）。"""
    monkeypatch.setattr(worker, "world_state", lambda world, progress=None: ("sig", {"a": [1, 2, 3]}))
    monkeypatch.setattr(worker, "build_world_graph", lambda world: ([], [], []))
    monkeypatch.setattr(worker, "_build_derived",
                        lambda world, **_kw: {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}})
    monkeypatch.setattr(worker, "_ledger_rows", lambda world, *, sig: [])
    monkeypatch.setattr(worker, "world_signature", lambda world: "sig")
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "load_world", lambda nodes, edges, world, uri, user, pw: (0, 0))

    @contextlib.contextmanager
    def _noop_lock(world_id):
        yield
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "replace_documents", lambda world, rows: 0)
    monkeypatch.setattr(store, "set_world_sig",
                        lambda world, sig, manifest=None, doc_count=None, scan_report=None: None)
    monkeypatch.setattr(store, "downgrade_orphaned_extracting_runs", lambda world=None: [])
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, progress: None)
    monkeypatch.setattr(corpus_docs, "scan_report", lambda world: {})
    monkeypatch.setattr(store, "set_scan_report", lambda world, report: None)
    monkeypatch.setattr("sherpa.es_index.index_world",
                        lambda world, content_sig=None, **kw: {"available": None})
    monkeypatch.setattr("sherpa.reconcile.reconcile_derivatives", lambda reflect=True: None)

    def _fake_start_ingest_run(world, **kw):
        return {"id": 1, "version": world, "layer": "version", "status": "extracting", "created_at": None}
    monkeypatch.setattr(store, "start_ingest_run", _fake_start_ingest_run)

    def _fake_finish_ingest_run(run_id, **kw):
        return {"id": run_id, **kw}
    monkeypatch.setattr(store, "finish_ingest_run", _fake_finish_ingest_run)

    def _fake_finish_and_confirm(run_id, world, *, status, extraction_snapshot=None,
                                 published_snapshot=None, source_doc_ids=None,
                                 sig=None, manifest=None, doc_count=None, scan_report=None):
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _fake_finish_and_confirm)

    notified = []
    monkeypatch.setattr(webhooks, "notify_run_terminal",
                        lambda world, run_id, op, status, **kw: notified.append(
                            {"world": world, "run_id": run_id, "op": op, "status": status, **kw}))
    return notified


def test_run_success_notifies_with_sync_op_and_completed_status(_stub_pipeline):
    res = worker.run("w")
    assert res["status"] in ("auto_published", "auto_published_with_flags")
    assert len(_stub_pipeline) == 1
    call = _stub_pipeline[0]
    assert call["world"] == "w"
    assert call["op"] == "sync"          # 既定 op
    assert call["status"] == res["status"]
    assert call["doc_count"] == 0        # `_ledger_rows` を空にスタブしている


def test_rerun_notifies_with_rerun_op(_stub_pipeline):
    worker.rerun("w")
    assert len(_stub_pipeline) == 1
    assert _stub_pipeline[0]["op"] == "rerun"


def test_explicit_op_overrides_default(_stub_pipeline):
    worker.run("w", op="refresh")
    assert _stub_pipeline[0]["op"] == "refresh"


def test_world_unresolved_notifies_failed_status(_stub_pipeline, monkeypatch):
    """`world_state` が `sig=None`（未解決）を返すと mutation 前に即 failed で終了する経路
    （`_run_locked` 冒頭）でも通知される。"""
    monkeypatch.setattr(worker, "world_state", lambda world, progress=None: (None, None))
    res = worker.run("w2")
    assert res["status"] == "failed"
    assert len(_stub_pipeline) == 1
    assert _stub_pipeline[0]["status"] == "failed"
    assert _stub_pipeline[0]["world"] == "w2"


def test_sync_content_changed_notifies_with_explicit_op(_stub_pipeline, monkeypatch):
    """RV是正#7: `_sync_impl` の「内容が変わった」経路（`prev != sig`・`_run_locked` の
    sidecar欠落分岐を経由せず末尾で `run()` へ委譲する分岐）は `op` を渡し忘れていたため、
    呼び出し元が refresh/rerun 等で `op` を指定していても Webhook payload の `op` が常に既定の
    "sync" に固定されてしまっていた——再発防止。"""
    monkeypatch.setattr(store, "get_world", lambda world: None)   # prev=None ≠ sig="sig"（内容変化扱い）
    result = worker.sync("w5", op="refresh")
    assert result["changed"] is True
    assert len(_stub_pipeline) == 1
    assert _stub_pipeline[0]["op"] == "refresh"


def test_sync_unchanged_world_unresolved_notifies_via_finalize_if_unused(_stub_pipeline, monkeypatch):
    """`_sync_impl` の `_run_locked` を経由しない終了点（world 未解決）でも `run_id` があれば
    通知される（`_finalize_if_unused` 経由）。"""
    monkeypatch.setattr(worker, "world_state", lambda world, progress=None: (None, None))
    result = worker.sync("w3", run_id=99)
    assert result["status"] == "unavailable"
    assert len(_stub_pipeline) == 1
    assert _stub_pipeline[0]["run_id"] == 99
    assert _stub_pipeline[0]["status"] == "failed"
    assert _stub_pipeline[0]["op"] == "sync"


def test_notify_failure_does_not_break_ingest(_stub_pipeline, monkeypatch):
    """`webhooks.notify_run_terminal` が例外を投げても取り込み自体の結果には影響しない
    （best-effort・`_record` 側の try/except）。"""
    def _boom(*a, **kw):
        raise RuntimeError("webhook down")
    monkeypatch.setattr(webhooks, "notify_run_terminal", _boom)
    res = worker.run("w4")
    assert res["status"] in ("auto_published", "auto_published_with_flags")
