"""1 回の sync で、登録フォルダ本体の全体走査（`scope_infer.safe_files`）は 1 回だけ（MD 化前の工程が走査結果を使い回す）。

実害: 共有フォルダ（ネットワーク越し・数万ファイル）の取り込みで、sync の署名計算・run の冒頭・アーカイブの列挙・
MD 化の対象列挙が同じ木を 4 回歩き、「MD化を開始します」が出るまでが長かった。
DB/Neo4j/ES は差し替え、走査と MD 化は実物を通す（外部境界だけモック）。
"""
from __future__ import annotations

import contextlib
import zipfile
from pathlib import Path

from sherpa import corpus_docs, es_index, reconcile, scope_infer, store, worlds
from sherpa.ingest import scan_pdf_ocr_status, worker, world_neo4j

_DOCX_XML = (
    '<?xml version="1.0"?>\n'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
    ' <w:body><w:p><w:r><w:t>本文</w:t></w:r></w:p></w:body>\n'
    '</w:document>')


def _stub_external(monkeypatch, tmp_path):
    wd = tmp_path / "wd"
    (wd / "sub").mkdir(parents=True)
    with zipfile.ZipFile(wd / "sub" / "a.docx", "w") as z:
        z.writestr("word/document.xml", _DOCX_XML)
    (wd / "sub" / "b.txt").write_text("text", encoding="utf-8")
    (wd / "c.txt").write_text("text", encoding="utf-8")
    monkeypatch.setenv("SHERPA_OCR_ENABLED", "0")
    monkeypatch.setenv("SHERPA_MD_WORKERS", "1")
    monkeypatch.setattr(worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: tmp_path / "derived" / "md")
    monkeypatch.setattr(worlds, "derived_dir", lambda w: tmp_path / "derived")
    monkeypatch.setattr(worlds, "archives_dir", lambda w: tmp_path / "archives")
    monkeypatch.setattr(worlds, "archives_work_dir", lambda w: tmp_path / "archives_work")
    monkeypatch.setattr(worlds, "archive_manifest_path", lambda w: tmp_path / "archives.json")
    monkeypatch.setattr(store, "get_world", lambda w, **kw: None)

    @contextlib.contextmanager
    def _noop_lock(world_id):
        yield
    monkeypatch.setattr(store, "world_lock", _noop_lock)
    monkeypatch.setattr(store, "start_ingest_run", lambda w, **kw: {"id": 1, "status": "extracting"})
    monkeypatch.setattr(store, "update_ingest_run_progress", lambda run_id, progress: None)
    monkeypatch.setattr(store, "downgrade_orphaned_extracting_runs", lambda world=None: [])
    monkeypatch.setattr(store, "set_world_sig", lambda *a, **kw: None)
    monkeypatch.setattr(store, "set_scan_report", lambda world, report: None)
    monkeypatch.setattr(store, "replace_documents", lambda world, rows: 0)
    monkeypatch.setattr(store, "finish_ingest_run", lambda run_id, **kw: {"id": run_id, **kw})
    monkeypatch.setattr(store, "finish_ingest_run_and_confirm_world", lambda run_id, world, **kw: {"id": run_id})
    monkeypatch.setattr(corpus_docs, "scan_report", lambda world, expected_rels=None: {"document_count": 0})
    monkeypatch.setattr(scan_pdf_ocr_status, "settle", lambda *a, **kw: None)
    monkeypatch.setattr(worker, "build_world_graph", lambda world: ([], [], []))
    monkeypatch.setattr(worker, "_ledger_rows", lambda world, *, sig: [])
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(world_neo4j, "load_world", lambda *a, **kw: (0, 0))
    monkeypatch.setattr(es_index, "index_world", lambda world, content_sig=None, **kw: {"available": True})
    monkeypatch.setattr(worker.office_md, "confirm_human_md_es_sig", lambda *a, **kw: True)   # ES 反映の確定（変換より後の段）
    monkeypatch.setattr(es_index, "confirm_human_md_meta", lambda world: True)
    monkeypatch.setattr(reconcile, "reconcile_derivatives", lambda reflect=True: None)
    monkeypatch.setenv("SHERPA_TEST_DB_ISOLATED", "1")
    return wd


def _count_body_walks(monkeypatch, wd):
    """登録フォルダ本体を歩いた回数（ES 反映・グラフ・台帳は差し替え済み＝MD 化前の工程だけが数えられる）。"""
    calls = {"body": 0, "all": 0}
    real = scope_infer.safe_files

    def _counting(root, **kw):
        calls["all"] += 1
        if Path(root).resolve() == wd.resolve():
            calls["body"] += 1
        return real(root, **kw)

    monkeypatch.setattr(scope_infer, "safe_files", _counting)
    return calls


def test_sync_walks_world_body_once_before_conversion(monkeypatch, tmp_path):
    wd = _stub_external(monkeypatch, tmp_path)
    calls = _count_body_walks(monkeypatch, wd)

    res = worker.sync("w", reflect=True)

    assert res["status"] in ("auto_published", "auto_published_with_flags"), res
    assert calls["body"] == 1


def test_run_without_scan_still_walks_itself_once(monkeypatch, tmp_path):
    """sync を経由しない呼び出し（再変換・初回取り込みなど）は、今までどおり自分で走査する（それでも 1 回）。"""
    wd = _stub_external(monkeypatch, tmp_path)
    calls = _count_body_walks(monkeypatch, wd)

    res = worker.run("w")

    assert res["status"] in ("auto_published", "auto_published_with_flags"), res
    assert calls["body"] == 1


def test_walk_snapshot_is_not_used_when_a_directory_was_unreadable(tmp_path):
    """列挙で読めないフォルダがあった走査は、strict で歩く呼び出し元（アーカイブの列挙）には使わせない（「見えなかった」を「無かった」にしない）。"""
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")
    ok = scope_infer.WalkSnapshot.from_walk(tmp_path, [], {})
    bad = scope_infer.WalkSnapshot.from_walk(tmp_path, [], {"unreadable_dir": 1})

    assert ok.files_for(tmp_path, strict=True) == []
    assert bad.files_for(tmp_path, strict=True) is None
    assert bad.files_for(tmp_path, strict=False) == []
    assert ok.files_for(tmp_path / "other") is None


def test_walk_snapshot_matches_safe_files(tmp_path):
    """`_scan_walk` の列挙は `safe_files` の出力と同じ並び・同じ中身（署名と対象の並び順を変えない）。"""
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "z.txt").write_text("x", encoding="utf-8")
    (tmp_path / "a.txt").write_text("x", encoding="utf-8")

    scan = worker._scan_walk(tmp_path)

    assert scan.walk.files == list(scope_infer.safe_files(tmp_path))
    assert scan.parts == worker._scan_dir(tmp_path)
