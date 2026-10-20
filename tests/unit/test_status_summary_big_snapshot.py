"""状態の API（`GET /worlds/{wid}/status`）が、文書ごとの注意（flags）が数万件ある取り込みの記録を Python へ丸ごと読まないこと。
flags 全件を読んでいた頃の導出（`tests/_run_status_view.py`）と同じ応答を返し、読み込むデータが大きく減る。覚えは 5 秒で切れる。
"""
from __future__ import annotations

import tracemalloc
import uuid

import pytest
from _run_status_view import status_view

from sherpa import corpus_docs, store
from sherpa.routers import worlds as worlds_router

pytestmark = pytest.mark.unit

_N_FLAGS = 30_000


def _big_flags() -> list[dict]:
    flags: list[dict] = []
    for i in range(_N_FLAGS):
        flags.append({"doc": f"dir/{i}.md", "action": "publish", "reason": "ok", "detail": "x" * 40})
    flags[10:10] = [{"doc": "a.md", "action": "blocked", "reason": "graph_reflect_failed:Neo4jError"},
                    {"doc": None, "action": "warn", "reason": "es_index_failed:bulk_failed"}]
    for i in range(300):                                           # 先頭 200 件の外にある注意
        flags.append({"doc": f"s{i}.cbl", "action": "publish", "reason": "source_not_registered", "from": f"s{i % 250}.cbl"})
        flags.append({"doc": f"b{i}.cbl", "action": "publish", "reason": "dropped_syntax", "why": "size_exceeded",
                      "from": f"b{i % 7}.cbl"})
        flags.append({"doc": f"m{i}.md", "action": "publish", "reason": "unreadable_mention_doc"})
    flags.append({"doc": None, "action": "warn", "reason": "office_md:derived_partial:rag_failed=12",
                  "count": 12, "many": True})
    flags.append({"doc": None, "action": "warn", "reason": "office_md:derived_partial:rag_failed=3",
                  "count": 3, "many": False})
    flags.append({"doc": None, "action": "warn", "reason": "plugin_failed"})
    flags.append({"doc": None, "action": "blocked", "reason": "rebind_failed_rolled_back"})
    flags.append("not-a-dict")
    return flags


@pytest.fixture
def big_world():
    wid = "bigsnap_" + uuid.uuid4().hex[:8]
    store.add_ingest_run(wid, status="auto_published", extraction_snapshot={"flags": [{"reason": "old"}]},
                         published_snapshot={"nodes": 1, "edges": 1})
    store.add_ingest_run(
        wid, status="auto_published_with_flags",
        extraction_snapshot={"flags": _big_flags(), "interrupted": False,
                             "office_md": {"converted": 5, "failed": 1, "unsupported": 0},
                             "es": {"available": True, "error": None, "chunks": 7, "vectors_missing": 2},
                             "counts": {"scanned": 48000}},
        published_snapshot={"nodes": 123, "edges": 456, "extra": ["x" * 100] * 50})
    store.replace_failed_docs(wid, {f"f/{i:05d}.pdf": ("source_parse_failed" if i % 3 else "unknown_x")
                                    for i in range(3000)})
    yield wid
    store.clear_failed_docs(wid)
    with store._connect() as c:
        c.execute("DELETE FROM ingest_runs WHERE version=%s", (wid,))


def _row():
    return {"last_scan_report": dict(corpus_docs.empty_scan_report()), "last_scan_report_at": None}


def _reference(monkeypatch):
    """flags 全件を読んで Python で導出していた頃の経路。"""
    monkeypatch.setattr(store, "get_latest_run_status",
                        lambda wid, limit: status_view(store.get_latest_run_summary(wid), limit))


def test_status_response_is_unchanged_with_tens_of_thousands_of_flags(big_world, monkeypatch):
    new = worlds_router._ingest_summary(big_world, _row())
    _reference(monkeypatch)
    old = worlds_router._ingest_summary(big_world, _row())
    assert new == old
    assert new["last_run_flags_total"] > _N_FLAGS and new["last_run_flags_truncated"] is True
    assert {"code": "graph_source_unparsed", "count": 250} in new["ingest_notices"]
    assert {"code": "md_docs_failed_many", "count": 12} in new["ingest_notices"]
    assert "plugin_failed" in new["last_run_warnings"] and "rebind_failed_rolled_back" in new["last_run_warnings"]
    assert new["failed_files"]["total"] == 3000 and len(new["failed_files"]["items"]) == 200
    assert new["failed_files"]["by_reason"] == {"source_parse_failed": 2000, "other": 1000}
    assert (new["graph_nodes"], new["graph_edges"]) == (123, 456)


def test_status_reads_far_less_data_into_python(big_world, monkeypatch, capsys):
    def peak():
        tracemalloc.start()
        try:
            worlds_router._ingest_summary(big_world, _row())
            return tracemalloc.get_traced_memory()[1]
        finally:
            tracemalloc.stop()
    new = peak()
    _reference(monkeypatch)
    old = peak()
    with capsys.disabled():
        print(f"\n[status peak] before={old / 1e6:.1f}MB after={new / 1e6:.1f}MB")
    assert new * 5 < old


def test_run_state_is_remembered_only_for_a_few_seconds(monkeypatch):
    clock = {"t": 1000.0}
    calls = []
    monkeypatch.setattr(worlds_router.time, "monotonic", lambda: clock["t"])
    monkeypatch.setattr(worlds_router, "_RUN_STATE_TTL_SEC", 5.0)
    monkeypatch.setattr(worlds_router, "_run_state", lambda wid: calls.append(wid) or {"n": len(calls)})
    assert worlds_router._run_state_cached("w") == {"n": 1}
    clock["t"] += 4.9
    assert worlds_router._run_state_cached("w") == {"n": 1}
    clock["t"] += 0.2
    assert worlds_router._run_state_cached("w") == {"n": 2}
    worlds_router._run_state_forget("w")
    assert worlds_router._run_state_cached("w") == {"n": 3}
