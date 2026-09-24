"""`routers.worlds._ingest_summary` の単体テスト。

`last_run_warnings` は blocked flag の `reason` のみ（`doc` は含まれない）。対象ファイルを取り込み
画面へ届けるには `last_run_blocked`（`doc`/`reason` 併記）を別途通す必要がある——不可読コードによる
全体停止（`unreadable_code_file`）でも対象ファイルが分かるようにする（§7）。

`_ingest_summary` は呼び出し元が取得済みの world 行（`row`）を受け取り、
`corpus_docs.scan_report`／`graph_view`／`es_index.count` を一切呼ばない（フォルダを歩かない・
graph/ES へ live 照会しない）。`store.get_latest_run_summary`/`get_latest_published_run_summary`/
`get_latest_es_run_summary`（いずれも狭い SELECT）だけを stub する。
"""
from __future__ import annotations

from sherpa import store
from sherpa.routers import worlds as worlds_router

_BASE_REP = {"scanned": 1, "indexed": 0, "by_doctype": {}, "office_md": 0,
            "skipped_office": 0, "office_failed": 0, "skipped_other": 0, "skipped_ext": {},
            "analyzer_declined": 0, "analyzer_declined_as_document": 0, "unreadable": 1}


def _row():
    return {"last_scan_report": dict(_BASE_REP), "last_scan_report_at": None}


def _stub_common(monkeypatch, run):
    monkeypatch.setattr(store, "get_latest_run_summary",
                        lambda wid: {"status": run["status"],
                                    "extraction_snapshot": run["extraction_snapshot"],
                                    "created_at": None} if run else None)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)


def test_ingest_summary_surfaces_doc_for_unreadable_code_file_blocked_flag(monkeypatch):
    """不可読コードによる全体停止の対象ファイルが `last_run_blocked` に doc 付きで通る。"""
    run = {"status": "failed", "extraction_snapshot": {
        "flags": [{"doc": "broken.cbl", "reason": "unreadable_code_file", "action": "blocked"}]}}
    _stub_common(monkeypatch, run)

    summary = worlds_router._ingest_summary("w", _row())

    assert summary["last_run_blocked"] == [{"doc": "broken.cbl", "reason": "unreadable_code_file"}]
    assert summary["last_run_warnings"] == ["unreadable_code_file"]


def test_ingest_summary_blocked_excludes_flags_without_doc(monkeypatch):
    """`doc` が無い blocked flag（例: `world_unresolved`）は `last_run_blocked` に載せない
    （対象ファイルを特定できないものを混ぜない）。`last_run_warnings` には引き続き reason が残る。"""
    run = {"status": "failed", "extraction_snapshot": {
        "flags": [{"doc": None, "reason": "world_unresolved", "action": "blocked"}]}}
    _stub_common(monkeypatch, run)

    summary = worlds_router._ingest_summary("w", _row())

    assert summary["last_run_blocked"] == []
    assert summary["last_run_warnings"] == ["world_unresolved"]


def test_ingest_summary_blocked_empty_when_no_run(monkeypatch):
    """直近の ingest run が無ければ `last_run_blocked`/`last_run_warnings` とも空。"""
    _stub_common(monkeypatch, None)

    summary = worlds_router._ingest_summary("w", _row())

    assert summary["last_run_blocked"] == []
    assert summary["last_run_warnings"] == []
