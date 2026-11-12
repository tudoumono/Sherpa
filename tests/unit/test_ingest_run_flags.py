"""取り込みの結果（`worker.run` の flags）に、派生物・アーカイブ・OCR の準備・孤児の掃除の失敗が載る。

周辺の差し替えは `test_worker_sig_discipline.py` の fixture をそのまま使う。
"""
from __future__ import annotations

from sherpa import reconcile
from sherpa.ingest import archive_extract, worker
from tests.unit.test_worker_sig_discipline import _stub_pipeline  # noqa: F401


def test_run_reports_archive_ocr_observation_and_cleanup_failures(monkeypatch):
    monkeypatch.setattr(worker.worlds, "world_dir", lambda w: None)
    monkeypatch.setattr(archive_extract, "sync_world_archives",
                        lambda *a, **k: {"a.zip": {"status": "ok", "last_sync_error": "read_failed"}})
    monkeypatch.setattr(worker, "_build_derived", lambda world, **kw: {
        "converted": 0, "failed": 0, "unsupported": 0, "by_ext": {},
        "ocr_routes_error": "OSError", "ocr_enqueue_error": "enqueue_failed",
        "observation_failures": [{"doc": "x.xlsx", "reason": "observation_merge_failed"}]})
    monkeypatch.setattr(reconcile, "reconcile_derivatives",
                        lambda reflect=True: {"es": [], "neo4j": [], "derived": [], "documents": [],
                                              "failed": ["derived:orphana"]})

    res = worker.run("w")

    reasons = {f["reason"] for f in res["flags"]}
    assert {"archive_sync_failed", "office_md:ocr_routes_failed:OSError", "office_md:ocr_enqueue_failed",
            "office_md:observation_issues:observation_merge_failed",
            "reconcile_failed:derived:orphana"} <= reasons
    assert res["status"] == "auto_published_with_flags"


def test_run_without_failures_has_no_warning_flags():
    res = worker.run("w")
    assert res["flags"] == [] and res["status"] == "auto_published"
