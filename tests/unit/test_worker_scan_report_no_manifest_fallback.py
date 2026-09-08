"""`scan_report` 失敗時にフォールバックしない契約（波3 3巡目 RV・高#1）。

`corpus_docs.scan_report()` は best-effort（失敗しても run は正常終端・確定 doc_count/scan_report
は `None` のまま＝前回値を保持）。従来は例外後に `manifest_doctype_count()` を代わりに実行して
いたが、これは削除済み——scan_report が失敗したら doc_count も一緒に更新保留にする（scan_report
自身の「取り込み冒頭 manifest との一致チェック」の結果を、粗い代替集計で覆い隠さないため）。

DB/Neo4j/ES は全て monkeypatch で差し替え、外部サービス不要（`tests/unit` の慣行・
`tests/unit/test_ingest_worker_flags.py` と同じスタブ流儀）。
"""
from __future__ import annotations

import contextlib

import pytest

from sherpa import corpus_docs, es_index, reconcile, store
from sherpa.ingest import world_neo4j, worker


@pytest.fixture
def _stub_pipeline(monkeypatch):
    monkeypatch.setattr(worker, "world_state", lambda world, progress=None: ("sig", {"a": [1, 2, 3]}))
    monkeypatch.setattr(worker, "build_world_graph", lambda world: ([], [], []))
    monkeypatch.setattr(worker, "_build_derived",
                        lambda world, **_kw: {"converted": 0, "failed": 0, "unsupported": 0, "by_ext": {}})
    monkeypatch.setattr(worker, "_ledger_rows", lambda world, *, sig: [])
    monkeypatch.setattr(worker, "world_signature", lambda world: "sig")
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "load_world", lambda nodes, edges, world, uri, user, pw: (0, 0))
    monkeypatch.setattr(es_index, "index_world",
                        lambda world, content_sig=None, **kw: {"available": True, "indexed": 0, "chunks": 0})
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)

    @contextlib.contextmanager
    def _noop_lock(world_id):
        yield
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "replace_documents", lambda world, rows: 0)
    monkeypatch.setattr(store, "set_world_sig", lambda world, sig, manifest=None, doc_count=None, scan_report=None: None)
    monkeypatch.setattr(store, "set_scan_report", lambda world, report: None)
    monkeypatch.setattr(store, "downgrade_orphaned_extracting_runs", lambda world=None: [])
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, progress: None)
    monkeypatch.setattr(store, "start_ingest_run",
                        lambda world, **kw: {"id": 1, "version": world, "status": "extracting"})
    monkeypatch.setattr(store, "finish_ingest_run", lambda run_id, **kw: {"id": run_id, **kw})

    captured = {"finish_and_confirm": []}

    def _fake_finish_and_confirm(run_id, world, *, status, extraction_snapshot=None,
                                 published_snapshot=None, source_doc_ids=None,
                                 sig=None, manifest=None, doc_count=None, scan_report=None):
        captured["finish_and_confirm"].append(
            {"status": status, "sig": sig, "doc_count": doc_count, "scan_report": scan_report})
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _fake_finish_and_confirm)
    return captured


def test_scan_report_exception_confirms_none_doc_count_without_calling_manifest_fallback(
        monkeypatch, _stub_pipeline):
    """`scan_report` が例外を投げても run は成功終端（`auto_published`）のまま——`manifest_
    doctype_count()` は一切呼ばれず、確定される doc_count/scan_report はどちらも `None`
    （該当列は更新されず前回値を保持する）。"""
    def _boom(world, **kw):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(corpus_docs, "scan_report", _boom)

    def _must_not_be_called(*a, **kw):
        raise AssertionError("manifest_doctype_count はフォールバックとして呼ばれてはいけない")
    monkeypatch.setattr(corpus_docs, "manifest_doctype_count", _must_not_be_called)

    res = worker.run("w")

    assert res["status"] == "auto_published"
    confirm_calls = [c for c in _stub_pipeline["finish_and_confirm"] if c["sig"] == "sig"]
    assert len(confirm_calls) == 1
    assert confirm_calls[0]["doc_count"] is None
    assert confirm_calls[0]["scan_report"] is None


def test_scan_report_success_passes_manifest_rels_as_expected_rels(monkeypatch, _stub_pipeline):
    """成功パスは冒頭 manifest（`world_state` が返した rel 集合）を `expected_rels` として
    `scan_report()` へ渡す（世代混在チェックの材料・`corpus_docs.scan_report` docstring 参照）。"""
    received = {}

    def _fake_scan_report(world, *, expected_rels=None):
        received["expected_rels"] = expected_rels
        return {"document_count": 3}
    monkeypatch.setattr(corpus_docs, "scan_report", _fake_scan_report)

    res = worker.run("w")

    assert res["status"] == "auto_published"
    assert received["expected_rels"] == frozenset({"a"})   # world_state スタブの manifest={"a": [1,2,3]}
    confirm_calls = [c for c in _stub_pipeline["finish_and_confirm"] if c["sig"] == "sig"]
    assert confirm_calls[0]["doc_count"] == 3
