"""ING-1（失敗一覧・各段要約）／ING-2（status キャッシュ化）の単体テスト。DB/ネットワーク不要。

対象:
  - `worker._failed_files_summary`/`_partial_extraction_summary`（`office_md.build_derived()` の
    `*_failures`/`partial_extraction_suspected` を1つの一覧＋理由別内訳へまとめる）。
  - `worker._run_locked` の成功パスが `corpus_docs.scan_report()` を実行し、既存の署名確定
    `store.set_world_sig(..., scan_report=...)` 呼び出し1本へ含める
    （sig/doc_count/scan_report を同一 UPDATE で一斉に確定する）。
  - `routers.worlds._ingest_summary(wid, row)` が **フォルダを歩かない**（`corpus_docs.scan_report`／
    `graph_view`／`es_index.count` を一切呼ばず、呼び出し元が渡した `row` と狭い run SELECT だけを
    読む）こと。
"""
from __future__ import annotations

import contextlib

import pytest

from sherpa import corpus_docs, es_index, reconcile, store
from sherpa.ingest import world_neo4j, worker
from sherpa.routers import worlds as worlds_router


# ===================================================================================
# _failed_files_summary / _partial_extraction_summary
# ===================================================================================

def test_failed_files_summary_aggregates_by_reason_and_stage():
    drep = {
        "document_ir_failures": [{"doc": "a.docx", "reason": "document_ir_failed:malformed_structure"}],
        "legacy_conversion_failures": [{"doc": "b.doc", "reason": "legacy_conversion_timeout"},
                                       {"doc": "c.doc", "reason": "legacy_conversion_failed"}],
        "rag_failures": [{"doc": "a.docx", "reason": "write_failed"}],
    }
    out = worker._failed_files_summary(drep)
    assert out["total"] == 4
    assert out["truncated"] is False
    assert len(out["items"]) == 4
    assert out["by_reason"] == {"malformed_structure": 1, "legacy_conversion_timeout": 1,
                                "legacy_conversion_failed": 1, "write_failed": 1}
    stages = {item["doc"]: item["stage"] for item in out["items"]}
    assert stages["a.docx"] in ("document_ir", "rag")   # a.docx appears twice（別段）
    assert {item["stage"] for item in out["items"] if item["doc"] == "b.doc"} == {"legacy_conversion"}


def test_failed_files_summary_truncates_at_limit():
    many = [{"doc": f"f{i}.docx", "reason": "write_failed"} for i in range(worker._FAILED_FILES_LIMIT + 5)]
    out = worker._failed_files_summary({"document_ir_failures": many})
    assert out["total"] == worker._FAILED_FILES_LIMIT + 5
    assert out["truncated"] is True
    assert len(out["items"]) == worker._FAILED_FILES_LIMIT
    assert out["by_reason"]["write_failed"] == worker._FAILED_FILES_LIMIT + 5   # 内訳は打切り前の全件


def test_failed_files_summary_ignores_malformed_entries():
    drep = {"document_ir_failures": [{"doc": None, "reason": "write_failed"}, {"doc": "x", "reason": None}]}
    out = worker._failed_files_summary(drep)
    assert out["total"] == 0
    assert out["items"] == []


def test_partial_extraction_summary_shape_and_truncation():
    items = [{"doc": f"f{i}.xlsx", "basis": "xlsx_row_ratio"} for i in range(worker._PARTIAL_EXTRACTION_LIMIT + 1)]
    out = worker._partial_extraction_summary({"partial_extraction_suspected": items})
    assert out["total"] == worker._PARTIAL_EXTRACTION_LIMIT + 1
    assert out["truncated"] is True
    assert len(out["items"]) == worker._PARTIAL_EXTRACTION_LIMIT


def test_partial_extraction_summary_empty_when_absent():
    out = worker._partial_extraction_summary({})
    assert out == {"items": [], "total": 0, "truncated": False}


# ===================================================================================
# `_run_locked` 成功パス: scan_report を署名確定 UPDATE 1本へ含める
# ===================================================================================

@pytest.fixture
def _stub_pipeline(monkeypatch):
    """`worker.run` を DB/Neo4j 無しで駆動できるよう周辺を差し替える（happy path）。

    `tests/unit/test_ingest_worker_flags.py` と同じ流儀。ここでは `corpus_docs.scan_report`／
    `store.set_world_sig` を実際に検証したいので**差し替えず呼び出しを記録するだけ**にする
    （他の DB/Neo4j/ES 呼び出しは従来どおりスタブして DB 不要を保つ）。
    """
    monkeypatch.setattr(worker, "world_state", lambda world, progress=None: ("sig", {"a": [1, 2, 3]}))
    monkeypatch.setattr(worker, "build_world_graph", lambda world: ([], [], []))
    monkeypatch.setattr(worker, "_build_derived",
                        lambda world, **_kw: {"converted": 1, "failed": 0, "unsupported": 0, "by_ext": {}})
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
    monkeypatch.setattr(store, "downgrade_orphaned_extracting_runs", lambda world=None: [])
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, progress: None)
    monkeypatch.setattr(store, "start_ingest_run",
                        lambda world, **kw: {"id": 1, "version": world, "status": "extracting"})
    monkeypatch.setattr(store, "finish_ingest_run", lambda run_id, **kw: {"id": run_id, **kw})

    calls = {"scan_report": [], "set_world_sig": []}
    monkeypatch.setattr(corpus_docs, "scan_report", lambda world: calls["scan_report"].append(world) or {"indexed": 5})

    # ING-3: 成功パスの sig/manifest/doc_count/scan_report 確定は run 完了と同一トランザクション
    # （`finish_ingest_run_and_confirm_world`）へ移った——`set_world_sig` 単体呼び出しはもう発生しない。
    # 記録先の名前（`calls["set_world_sig"]`）はテスト読者に馴染みのある呼称のまま残し、実体だけ
    # 新しい関数呼び出しから集める。
    def _fake_finish_and_confirm(run_id, world, *, status, extraction_snapshot=None,
                                 published_snapshot=None, source_doc_ids=None,
                                 sig=None, manifest=None, doc_count=None, scan_report=None):
        if sig is not None:
            calls["set_world_sig"].append({"world": world, "sig": sig, "doc_count": doc_count,
                                           "scan_report": scan_report})
        return {"id": run_id, "status": status}
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _fake_finish_and_confirm)
    return calls


def test_run_locked_success_folds_scan_report_into_sig_confirm(monkeypatch, _stub_pipeline):
    """成功パスは `scan_report()` を実行し、既存の署名確定 `set_world_sig` 呼び出し1本へ含める
    （別立ての保存呼び出しは無い＝同一 UPDATE で sig/doc_count/scan_report を一斉に確定する）。"""
    res = worker.run("w")
    assert res["status"] == "auto_published"
    assert _stub_pipeline["scan_report"] == ["w"]
    confirm_calls = [c for c in _stub_pipeline["set_world_sig"] if c["sig"] == "sig"]
    assert len(confirm_calls) == 1
    assert confirm_calls[0]["scan_report"] == {"indexed": 5}


def test_run_locked_failure_before_success_does_not_compute_scan_report(monkeypatch, _stub_pipeline):
    """world 未解決（sig=None）で即 failed の経路は scan_report の計算にすら触れない。"""
    monkeypatch.setattr(worker, "world_state", lambda world, progress=None: (None, None))
    res = worker.run("w")
    assert res["status"] == "failed"
    assert _stub_pipeline["scan_report"] == []
    assert _stub_pipeline["set_world_sig"] == []


def test_run_locked_scan_report_computation_failure_confirms_sig_without_report(monkeypatch, _stub_pipeline):
    """`corpus_docs.scan_report()` 自体が失敗しても、取り込み結果（成功）や sig 確定は変えない
    （best-effort・`scan_report=None` のまま `set_world_sig` を呼ぶ＝該当列は更新されず前回値が残る）。"""
    def _boom(world):
        raise RuntimeError("scan failed")
    monkeypatch.setattr(corpus_docs, "scan_report", _boom)
    res = worker.run("w")
    assert res["status"] == "auto_published"
    confirm_calls = [c for c in _stub_pipeline["set_world_sig"] if c["sig"] == "sig"]
    assert len(confirm_calls) == 1
    assert confirm_calls[0]["scan_report"] is None


def test_run_locked_wires_es_index_progress_to_ingest_run_progress(monkeypatch, _stub_pipeline):
    """`es_index.index_world` へ渡す `progress` コールバック（EMBED-3′・利用者報告 2026-09-04:
    最長段で done/total が数時間動かない問題への対処）が `_progress("es_index", done, total)` 経由で
    `store.update_ingest_run_progress` まで届く。`index_world` スタブが受け取った `progress` を実際に
    呼び出して配線を検証する（実 ES 不要）。開始時の `done=0/total=None` に続き、コールバックの
    2回（間引きの安全弁は `done==0`／`done==total` を必ず書く契約なのでどちらも通る）が記録される。
    """
    progress_calls = []

    def _fake_update_progress(run_id, progress):
        progress_calls.append(progress)
    monkeypatch.setattr(store, "update_ingest_run_progress", _fake_update_progress)

    def _fake_index_world(world, content_sig=None, progress=None, **kw):
        if progress is not None:
            progress(3, 10)
            progress(10, 10)
        return {"available": True, "indexed": 10, "chunks": 10}
    monkeypatch.setattr(es_index, "index_world", _fake_index_world)

    res = worker.run("w")
    assert res["status"] == "auto_published"
    es_stage_calls = [p for p in progress_calls if p.get("stage") == "es_index"]
    assert [(p["done"], p["total"]) for p in es_stage_calls] == [(0, None), (3, 10), (10, 10)]


def test_run_locked_es_progress_dedupes_consecutive_same_value(monkeypatch, _stub_pipeline):
    """RV是正（rv-periphery #3(c)・2026-09-05）: `_es_progress` は直前と全く同じ `done` を
    無条件に書かない——`done==0`/`done==total` は同値でも毎回書く特例だったため、例えば
    Pass2 が同じ値を2回連続で通知するケース（空 world の 0/0 等）で重複書込みしていた。"""
    progress_calls = []
    monkeypatch.setattr(store, "update_ingest_run_progress",
                        lambda run_id, progress: progress_calls.append(progress))

    def _fake_index_world(world, content_sig=None, progress=None, **kw):
        if progress is not None:
            progress(0, 10)
            progress(0, 10)     # 同値の2回目＝抑止される
            progress(10, 10)
            progress(10, 10)    # 同値の2回目＝抑止される
        return {"available": True, "indexed": 10, "chunks": 10}
    monkeypatch.setattr(es_index, "index_world", _fake_index_world)

    res = worker.run("w")
    assert res["status"] == "auto_published"
    es_stage_calls = [p for p in progress_calls if p.get("stage") == "es_index" and p.get("total") == 10]
    assert [(p["done"], p["total"]) for p in es_stage_calls] == [(0, 10), (10, 10)]


def test_run_locked_sig_confirm_write_failure_propagates(monkeypatch, _stub_pipeline):
    """run 完了と sig/doc_count/scan_report 確定は同一トランザクション
    （`finish_ingest_run_and_confirm_world`）——その書込自体が失敗すれば（PG断等）例外がそのまま
    伝播する（MED-1 と同じ契約：次回 sync が再試行する）。"""
    def _boom(run_id, world, *, status, extraction_snapshot=None, published_snapshot=None,
             source_doc_ids=None, sig=None, manifest=None, doc_count=None, scan_report=None):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", _boom)
    with pytest.raises(RuntimeError, match="db down"):
        worker.run("w")


# ===================================================================================
# `routers.worlds._ingest_summary(wid, row)`: フォルダを歩かない・live 照会しない
# ===================================================================================

def _must_not_walk(*a, **kw):
    raise AssertionError("_ingest_summary は corpus_docs.scan_report を呼んではいけない（ING-2）")


def _must_not_call(name):
    def _boom(*a, **kw):
        raise AssertionError(f"_ingest_summary は {name} を呼んではいけない（ING-2）")
    return _boom


@pytest.fixture(autouse=True)
def _stub_ingest_summary_deps(monkeypatch):
    # ING-3: confirm/disable の応答末尾だった graph_view 再構築は非同期化で撤去済み＝
    # `sherpa.routers.worlds` はもう `graph_view` を import していない（属性自体が無い）。
    # `_ingest_summary` がこれを呼ばない契約は、その import 不在自体で構造的に保証される。
    monkeypatch.setattr(es_index, "count", _must_not_call("es_index.count"))
    monkeypatch.setattr(store, "get_world", _must_not_call("store.get_world"))   # row は引数で渡す・再取得しない
    monkeypatch.setattr(corpus_docs, "scan_report", _must_not_walk)
    monkeypatch.setattr(store, "get_latest_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: None)


def test_ingest_summary_uses_cached_scan_report_without_walking(monkeypatch):
    cached = {"scanned": 9, "indexed": 9, "by_doctype": {}, "office_md": 0, "skipped_office": 0,
             "office_failed": 0, "skipped_other": 0, "skipped_ext": {}, "analyzer_declined": 0,
             "analyzer_declined_as_document": 0, "unreadable": 0}
    row = {"last_scan_report": cached, "last_scan_report_at": "2026-09-01T03:12:00+00:00"}
    s = worlds_router._ingest_summary("w", row)
    assert s["scanned"] == 9
    assert s["counts_as_of"] == "2026-09-01T03:12:00+00:00"


def test_ingest_summary_reports_uncounted_when_no_cache_without_walking(monkeypatch):
    row = {"last_scan_report": None, "last_scan_report_at": None}
    s = worlds_router._ingest_summary("w", row)
    assert s["counts_as_of"] is None
    assert s["scanned"] == 0
    assert s["indexed"] == 0


def test_ingest_summary_graph_and_es_counts_come_from_latest_published_run(monkeypatch):
    """graph_nodes/graph_edges は最新の**反映済み** run の published_snapshot から、
    es_chunks は ES 専用の最新反映 run（`get_latest_es_run_summary`）の
    extraction_snapshot.es から読む（live 照会しない・別クエリなので通常時は同じ run を指す）。"""
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: {
        "published_snapshot": {"nodes": 12, "edges": 7}})
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: {
        "extraction_snapshot": {"es": {"available": True, "error": None, "chunks": 34}}})
    row = {"last_scan_report": None, "last_scan_report_at": None}
    s = worlds_router._ingest_summary("w", row)
    assert s["graph_nodes"] == 12 and s["graph_edges"] == 7
    assert s["es_chunks"] == 34


def test_ingest_summary_no_published_run_yields_zero_graph_and_none_es(monkeypatch):
    row = {"last_scan_report": None, "last_scan_report_at": None}
    s = worlds_router._ingest_summary("w", row)
    assert s["graph_nodes"] == 0 and s["graph_edges"] == 0
    assert s["es_chunks"] is None


def test_ingest_summary_es_chunks_survive_a_newer_pg_replace_failed_run(monkeypatch):
    """台帳（PG）replace 失敗の run は Neo4j へは反映済み（`published_snapshot`/`published_at` が
    立つ＝graph 用クエリの最新反映 run になる）でも、ES 段には未到達のため
    `extraction_snapshot` に `es` キーを持たない。graph と ES を同じクエリから読むと、この
    run が「最新反映」を名乗って実際に ES へ触れたより古い run の件数を隠してしまう——
    別クエリ（`get_latest_es_run_summary`）に分離したことで、新しい run の登場後も
    古い有効な es.chunks がそのまま表示され続けることを固定する。"""
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: {
        # 新しい pg_replace 失敗 run: nodes/edges は新世代だが extraction_snapshot に es は無い。
        "published_snapshot": {"nodes": 20, "edges": 15},
        "extraction_snapshot": {"docs": 5, "nodes": 20, "edges": 15, "stage": "pg_replace"}})
    monkeypatch.setattr(store, "get_latest_es_run_summary", lambda wid: {
        # より古い、実際に ES まで到達した run。
        "extraction_snapshot": {"es": {"available": True, "error": None, "chunks": 34}}})
    row = {"last_scan_report": None, "last_scan_report_at": None}
    s = worlds_router._ingest_summary("w", row)
    assert s["graph_nodes"] == 20 and s["graph_edges"] == 15    # 新世代のグラフ件数
    assert s["es_chunks"] == 34                                  # 旧 ES の件数が隠されない


def test_ingest_summary_db_failure_propagates_not_zeroes(monkeypatch):
    """最新 run の狭い SELECT が失敗したら、summary を全ゼロへ縮退させず例外を伝播する
    （明示エラー）。"""
    def _boom(wid):
        raise RuntimeError("db down")
    monkeypatch.setattr(store, "get_latest_run_summary", _boom)
    row = {"last_scan_report": None, "last_scan_report_at": None}
    with pytest.raises(RuntimeError, match="db down"):
        worlds_router._ingest_summary("w", row)


def test_scan_dir_reports_progress_counts(tmp_path):
    """走査段の件数進捗（2026-09-04・実環境「走査段が無音」対応）: `_scan_dir(progress=...)` が
    `_SCAN_PROGRESS_INTERVAL` 件ごと＋最後に走査済み件数を単調増加で報告する。"""
    from sherpa.ingest import worker
    for i in range(7):
        (tmp_path / f"f{i}.txt").write_text("x", encoding="utf-8")
    calls = []
    monkey_interval = 3
    orig = worker._SCAN_PROGRESS_INTERVAL
    worker._SCAN_PROGRESS_INTERVAL = monkey_interval
    try:
        parts = worker._scan_dir(tmp_path, progress=calls.append)
    finally:
        worker._SCAN_PROGRESS_INTERVAL = orig
    assert len(parts) == 7
    assert calls == [3, 6, 7]
