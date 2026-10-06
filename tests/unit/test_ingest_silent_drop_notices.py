"""取り込みで黙って落とした・粗くしたものを、件数つきで状態（`/worlds/{id}/status`）と来歴に出す。

上限・退避の値は変えず、落とした事実を数えて残すだけ（外部境界＝ES の `_req`・埋め込み・VLM だけ差し替える）。
"""
from __future__ import annotations

import os
import sys
import types
import urllib.error

import pytest

from sherpa import corpus_docs, es_index, graph_coverage, store, worlds
from sherpa.ingest import office_md, text_kind, world_graph
from sherpa.ingest.arms import ArmResult, vision_arm
from sherpa.routers import worlds as worlds_router

pytestmark = pytest.mark.usefixtures("upstream_only_registry")


def _row(rep=None):
    base = corpus_docs.empty_scan_report()
    return {"last_scan_report": {**base, **(rep or {})}, "last_scan_report_at": None}


def _stub_runs(monkeypatch, snapshot=None, es_snapshot=None):
    monkeypatch.setattr(store, "get_recent_es_attempts", lambda wid, limit=200: [])
    monkeypatch.setattr(store, "get_latest_run_summary",
                        lambda wid: {"status": "auto_published_with_flags",
                                     "extraction_snapshot": snapshot or {}, "created_at": None})
    monkeypatch.setattr(store, "get_latest_published_run_summary", lambda wid: None)
    monkeypatch.setattr(store, "get_latest_es_run_summary",
                        lambda wid: {"extraction_snapshot": {"es": es_snapshot}} if es_snapshot else None)


def test_unreadable_folder_and_symlink_are_counted_not_silently_skipped(monkeypatch, tmp_path):
    """権限で開けないフォルダとシンボリックリンクは、`scan_report` の件数と状態の通知に出る（無かったことにしない）。"""
    wd = tmp_path / "world"
    (wd / "open").mkdir(parents=True)
    (wd / "open" / "a.txt").write_text("本文", encoding="utf-8")
    locked = wd / "locked"
    locked.mkdir()
    (locked / "b.txt").write_text("見えない", encoding="utf-8")
    (wd / "link.txt").symlink_to(wd / "open" / "a.txt")
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: tmp_path / "derived")
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: tmp_path / "derived")
    monkeypatch.setattr(worlds, "observation_current_dir", lambda w: None)
    os.chmod(locked, 0)
    try:
        rep = corpus_docs.scan_report("w")
    finally:
        os.chmod(locked, 0o755)

    assert rep["scanned"] == 1
    assert rep["walk_skipped"] == {"symlink": 1, "unreadable_dir": 1}
    _stub_runs(monkeypatch)
    summary = worlds_router._ingest_summary("w", _row({"walk_skipped": rep["walk_skipped"]}))
    assert {"code": "walk_unreadable_dir", "count": 1} in summary["ingest_notices"]
    assert {"code": "walk_symlink", "count": 1} in summary["ingest_notices"]


def _fake_pdfium(monkeypatch, pages: int):
    class _Page:
        def close(self):
            pass

    class _Doc:
        def __init__(self, path):
            pass

        def __len__(self):
            return pages

        def __getitem__(self, i):
            return _Page()

        def close(self):
            pass

    class _Img:
        def save(self, path, format=None):
            open(path, "wb").write(b"png")

        def close(self):
            pass

    mod = types.ModuleType("pypdfium2")
    mod.PdfDocument = _Doc
    monkeypatch.setitem(sys.modules, "pypdfium2", mod)
    from sherpa.ingest.arms import raster
    monkeypatch.setattr(raster, "_rasterize_page", lambda page: _Img())


def test_scanned_pdf_unread_pages_are_recorded_in_provenance(monkeypatch, tmp_path):
    """21 ページ以上のスキャン PDF: 上限で読まなかったページ・読み取りに失敗したページ・時間予算で打ち切ったページを来歴に残し、本文は変えない。"""
    _fake_pdfium(monkeypatch, pages=25)
    calls = []

    def fake_vlm(image_path, cfg, timeout):
        calls.append(image_path.name)
        return None if image_path.name == "page_3.png" else "本文"

    monkeypatch.setattr(vision_arm, "_vlm_read", fake_vlm)
    arm = vision_arm.VisionArm()
    text, pages_done, notes = arm._read_pdf(tmp_path / "scan.pdf", {"provider": "ollama", "model": "m"})

    assert pages_done == 20 and len(calls) == 20
    assert "## ページ 3" not in text and "## ページ 4" in text   # 本文は読めたページだけ（従来どおり）
    md = tmp_path / "scan.pdf.md"
    office_md._write_provenance(md, "vision", ArmResult(md="x", method="vision", confidence=0.4, notes=notes))
    assert corpus_docs.provenance_summary(md)["pdf_pages"] == {"total": 25, "over_limit": 5, "unread": 1}

    monkeypatch.setattr(vision_arm, "_vlm_timeout_sec", lambda: -1.0)   # 予算が最初から尽きている
    _text, done, notes = arm._read_pdf(tmp_path / "scan.pdf", {"provider": "ollama", "model": "m"})
    assert done == 0 and "pdf_pages_budget_cut=20" in notes

    # 全ページが空・失敗でも変換失敗は変えず（md=None）、読めなかったページ数を失敗の記録へ載せる
    monkeypatch.setattr(vision_arm, "_vlm_timeout_sec", lambda: 60.0)
    monkeypatch.setattr(vision_arm, "_vlm_read", lambda *a, **k: None)
    monkeypatch.setattr(vision_arm, "resolve_vlm", lambda: {"provider": "ollama", "model": "m"})
    res = arm.convert(tmp_path / "scan.pdf")
    assert res.md is None
    from sherpa.ingest import worker
    entry = {"doc": "scan.pdf", "reason": "conversion_failed", "pdf_pages": corpus_docs._pdf_pages_summary(res.notes)}
    md2 = tmp_path / "fig.docx.md"   # サイドカー作成前に集計した図の切り詰めは、書くときに失われない
    office_md._write_provenance(md2, "ooxml", ArmResult(md="x", method="ooxml", confidence=1.0),
                                extra_meta={"metafile_truncated": {"lines_dropped": 3}})
    assert corpus_docs.provenance_summary(md2)["metafile_truncated"] == {"lines_dropped": 3}
    md3 = tmp_path / "other.docx.md"   # 保留はファイルごとの入れ物だけ＝モジュールに残らず、別の書き込みへ漏れない
    office_md._write_provenance(md3, "ooxml", ArmResult(md="x", method="ooxml", confidence=1.0))
    assert "metafile_truncated" not in (corpus_docs.provenance_summary(md3) or {})
    assert not hasattr(office_md, "_DEFERRED_PROVENANCE")
    item = worker._failed_files_summary({"conversion_failures": [entry]})["items"][0]
    assert item["pdf_pages"] == {"total": 25, "over_limit": 5, "unread": 20}


def test_source_not_in_graph_is_reported_in_coverage_and_status(monkeypatch, tmp_path):
    """大きすぎてグラフに入れなかったソースは、グラフの coverage（`source_unparsed`）と状態の通知に出る（「関係が無い」と区別する）。"""
    wd = tmp_path / "world"
    wd.mkdir()
    (wd / "big.cbl").write_text("       IDENTIFICATION DIVISION.\n" * 20, encoding="utf-8")
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(text_kind, "MAX_BYTES", 64)
    _nodes, _edges, flags = world_graph.build_world(wd, "w")
    flags = flags + [{"reason": "dropped_syntax", "from": "part.js", "why": "js_dynamic_url"},   # 一部の省略は数えない
                     {"reason": "dropped_syntax", "from": "part.java", "why": "syntax_error"}]     # 主体を作った上での構文エラーも数えない

    info = world_graph.unparsed_sources_from_flags(flags)
    assert info == {"syntax": 0, "size_exceeded": 1}
    assert world_graph.unparsed_sources_from_flags(
        [{"reason": "source_not_registered", "from": "x.xml"}]) == {"syntax": 1, "size_exceeded": 0}
    cov = graph_coverage.Coverage()
    graph_coverage.attach_plugin_failures(cov, lambda: [], graph_coverage.STAGE_IMPACT,
                                          read_unparsed=lambda: info)
    assert cov.as_dict()["limits"] == [{"kind": "source_unparsed", "stage": "impact", "count": 1}]
    from sherpa.ext_api import ExtLimit
    assert ExtLimit(**{"kind": "source_unparsed", "count": 1}).count == 1
    assert not cov.as_dict()["complete"]
    legacy = graph_coverage.Coverage()   # 保存の無い旧グラフ（None）は申告しない
    graph_coverage.attach_plugin_failures(legacy, lambda: [], None, read_unparsed=lambda: None)
    assert legacy.as_dict()["complete"]

    _stub_runs(monkeypatch, snapshot={"flags": flags})   # action の無い flag も状態から落ちない
    summary = worlds_router._ingest_summary("w", _row())
    assert {"code": "graph_source_oversize", "count": 1} in summary["ingest_notices"]


def _es_env(monkeypatch, tmp_path, docs, read):
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", read)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index.worlds, "derived_dir", lambda w: tmp_path / w)
    puts = []

    def req(method, path, body=None, **kw):
        if method == "PUT":
            puts.append(body["mappings"]["properties"]["text"]["analyzer"])
            if puts[-1] == "kuromoji":
                raise urllib.error.HTTPError(path, 400, "unknown analyzer", {}, None)
        return {}

    monkeypatch.setattr(es_index, "_req", req)
    return puts


def test_es_analyzer_fallback_is_reported(monkeypatch, tmp_path):
    """日本語アナライザを作れず標準で索引を作り直したことが、索引の結果と状態の通知に出る。"""
    docs = [{"name": "a.md", "md_path": None, "top_scope": "t"}]
    puts = _es_env(monkeypatch, tmp_path, docs, lambda w, d: "本文")
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    res = es_index.index_world("w")

    assert puts == ["kuromoji", "standard"]
    assert res["analyzer_fallback"] is True
    _stub_runs(monkeypatch, es_snapshot={"available": True, "error": None, "chunks": 1, "analyzer_fallback": True})
    summary = worlds_router._ingest_summary("w", _row())
    assert {"code": "es_analyzer_fallback", "count": 1} in summary["ingest_notices"]


def test_es_degraded_docs_and_bm25_only_are_recorded_without_extra_embedding_calls(monkeypatch, tmp_path):
    """本文を読めず外した文書・埋め込み失敗の BM25 のみ降格は結果に残る。通知のために埋め込みを呼び直さない。"""
    from sherpa.ingest import worker
    docs = [{"name": "ok.md", "md_path": None, "top_scope": "t", "branch": "office"},
            {"name": "gone.md", "md_path": None, "top_scope": "t", "branch": "office"}]
    _es_env(monkeypatch, tmp_path, docs, lambda w, d: None if d["name"] == "gone.md" else "本文")
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "p", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    embed_calls = []

    def failing_embed(texts, ec, **kw):
        embed_calls.append(len(texts))
        return None

    monkeypatch.setattr(es_index.embeddings, "embed", failing_embed)
    res = es_index.index_world("w")

    assert res["rag_degraded_by_reason"] == {"text_read_failed": 1}
    assert res["embed_degraded"] is True and res["vectors"] is False
    assert embed_calls == [1]                                  # 索引に入る 1 チャンクの 1 回だけ（通知で増えない）
    summary = worker._es_summary(res)
    assert summary["rag_degraded_by_reason"] == {"text_read_failed": 1} and summary["embed_degraded"] is True
    _stub_runs(monkeypatch, es_snapshot=summary)
    codes = {n["code"]: n["count"] for n in worlds_router._ingest_summary("w", _row())["ingest_notices"]}
    assert codes == {"es_text_read_failed": 1, "es_embed_failed_bm25_only": 1}
    _stub_runs(monkeypatch, es_snapshot={"available": True, "error": None, "rag_degraded_by_reason": {"empty_text": 2}})
    assert {"code": "es_empty_text", "count": 2} in worlds_router._ingest_summary("w", _row())["ingest_notices"]
