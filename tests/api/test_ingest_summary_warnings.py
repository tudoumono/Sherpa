"""_ingest_summary の半壊可視化フィールド（監査#5・Phase 2-a）の API 層テスト。

worker が記録した warn flag が `last_run_status` / `last_run_warnings` として
summary（`/worlds/{wid}/status` 等の応答）に載る抽出ロジックを固定する。
UI（web/ingest.js summaryNote）はこのフィールド名/reason prefix に依存するため、
ここが回帰すると画面の警告が消える（RV Low の指摘対応）。store/graph/ES は
monkeypatch＝DB 不要。

`_ingest_summary(wid, row)` は呼び出し元が取得済みの world 行を受け取り、
`corpus_docs.scan_report`／`graph_view`／`es_index.count` を一切呼ばない。最新 run の要約は
`store.get_latest_run_summary`（狭い SELECT）から読み、DB 例外は握りつぶさず伝播させる
（全ゼロへ縮退して「集計できた」ように見せない・明示エラー）。
"""
from __future__ import annotations

from sherpa import api, store

_ROW = {"last_scan_report": None, "last_scan_report_at": None}


def test_warn_flags_surface_as_last_run_warnings(monkeypatch):
    """最終 run の warn/blocked reason が last_run_warnings に載る（publish 等は載らない）。

    blocked（failed run の graph_reflect_failed 等）も契約に含める＝抽出条件から
    warn/blocked のどちらが欠けても検出する（RV round2 指摘）。
    """
    run = {"status": "auto_published_with_flags",
           "extraction_snapshot": {"flags": [
               {"doc": None, "action": "warn", "reason": "es_index_failed:bulk_failed"},
               {"doc": None, "action": "blocked", "reason": "graph_reflect_failed:Neo4jError"},
               {"doc": None, "action": "warn", "reason": "reconcile_failed:RuntimeError"},
               {"doc": "a.md", "action": "publish", "reason": "ok"},
           ]}}
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: run)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)
    s = api._ingest_summary("w", _ROW)
    assert s["last_run_status"] == "auto_published_with_flags"
    assert s["last_run_warnings"] == ["es_index_failed:bulk_failed",
                                      "graph_reflect_failed:Neo4jError",
                                      "reconcile_failed:RuntimeError"]


def test_non_dict_snapshot_does_not_break_summary(monkeypatch):
    """extraction_snapshot が dict 以外（旧データ/手動投入）でも summary は落ちない（RV Low）。"""
    run = {"status": "auto_published", "extraction_snapshot": []}
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: run)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)
    s = api._ingest_summary("w", _ROW)
    assert s["last_run_status"] == "auto_published"
    assert s["last_run_warnings"] == []


def test_store_failure_propagates_as_explicit_error(monkeypatch):
    """run 取得の DB 失敗は summary を全ゼロへ縮退させず、そのまま例外を伝播させる
    （呼び出し元＝`world_status`/`_ingest_summary_after_mutation` が 503 へ変換する）。
    """
    def _boom(wid):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_latest_run_summary", _boom)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)
    try:
        api._ingest_summary("w", _ROW)
    except RuntimeError as e:
        assert str(e) == "db down"
    else:
        raise AssertionError("DB 失敗が握りつぶされて summary が返ってしまった")


def test_no_runs_yields_none_and_empty(monkeypatch):
    """run が1件も無い world（登録直後）は None/[]。"""
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)
    s = api._ingest_summary("w", _ROW)
    assert s["last_run_status"] is None
    assert s["last_run_warnings"] == []
