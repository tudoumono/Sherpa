"""アーカイブ取り込み（zip/tar）内の Office/PDF の決定的MD化（検収の是正・2026-10-01）。

`sherpa.ingest.office_md` の原本ツリー走査（変換・drift 判定・軽量再生成）が展開先
（`worlds.archives_dir`）を合流させることを固定する。固定するのは3点:
  1. zip の中の docx が MD/rag.md/document.json/evidence.json に変換され、台帳一覧・grep で見える。
  2. アーカイブの更新で中の docx の派生物が作り直される／削除で消える。**かつ**内容が不変のまま
     軽量再生成（`rag_sidecars_missing`/`refresh_document_ir`/`refresh_evidence_ir`）を呼んでも、
     展開先由来の派生物を「原本が消えた」と誤認して削除しない（孤児掃除の誤爆防止）。
  3. `/ext/v1/doc` で中のファイルが展開した写しとして取れる。

世界の隔離は `tests/unit/test_agentic_search.py::_isolate_world_kb` と同じ手法
（`SHERPA_KB_DIR`/`SHERPA_DERIVED_DIR` を tmp へ向け、`store.get_world` を None 固定して
registry 解決をバイパスする・DB 不要）。docx は `tests/unit/test_office_md.py` と同じ手法
（`word/document.xml` だけの最小 zip・python-docx 等の外部ライブラリ不要）で組む。
"""
from __future__ import annotations

import io
import zipfile
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from sherpa import corpus_docs, ext_api, grep_tool, worlds
from sherpa.ingest import archive_extract, office_md

_DOCX_XML_TMPL = (
    '<?xml version="1.0"?>\n'
    '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">\n'
    ' <w:body><w:p><w:r><w:t>{text}</w:t></w:r></w:p></w:body>\n'
    '</w:document>'
)


def _docx_bytes(text: str) -> bytes:
    """最小 docx（`word/document.xml` のみ）をメモリ上で組む（`test_office_md.py` と同じ手法）。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as z:
        z.writestr("word/document.xml", _DOCX_XML_TMPL.format(text=text))
    return buf.getvalue()


def _isolate_world_kb(monkeypatch, tmp_path) -> Path:
    """`sherpa.worlds.world_dir`/`archives_dir` を tmp 配下へ隔離する（DB 不要）。"""
    from sherpa import store

    kb = tmp_path / "kb"
    kb.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("SHERPA_KB_DIR", str(kb))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.delenv("SHERPA_USE_FIXTURES", raising=False)
    for env in ("SHERPA_MCP_WORLD", "SHERPA_MCP_WORLD_ROOT"):
        monkeypatch.delenv(env, raising=False)
    monkeypatch.setattr(store, "get_world", lambda world_id, **kw: None)
    return kb


def test_zip_docx_converts_to_md_rag_ir_and_is_listed_and_searchable(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcoffice1"
    wd = kb / world
    (wd / "docs").mkdir(parents=True)
    zpath = wd / "docs" / "design.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("screen/doc1.docx", _docx_bytes("Needle12345"))

    root = worlds.world_dir(world)
    archive_extract.sync_world_archives(world, root)
    dmd = worlds.derived_md_dir(world)
    rep = office_md.build_derived(root, dmd, world=world)
    assert rep.get("error") is None
    assert rep["converted"] == 1 and rep["failed"] == 0
    assert rep["document_ir_generated"] == 1 and rep["document_ir_failed"] == 0
    assert rep["evidence_ir_generated"] == 1 and rep["evidence_ir_failed"] == 0
    assert rep["rag_generated"] == 1 and rep["rag_failed"] == 0

    rel = "docs/design.zip/screen/doc1.docx"
    assert (dmd / (rel + ".md")).is_file()
    assert (worlds.derived_rag_dir(world) / (rel + ".rag.md")).is_file()
    assert (worlds.derived_ir_dir(world) / (rel + ".document.json")).is_file()
    assert (worlds.derived_ir_dir(world) / (rel + ".evidence.json")).is_file()

    # 台帳一覧（= ES 索引の材料・`es_index.index_world` と同じ `corpus_docs.world_documents`）に出る。
    rows = {r["name"]: r for r in corpus_docs.iter_world_documents(world, include_rag=True)}
    assert rows[rel]["doctype"] == "Word"
    assert rows[rel]["state"] == "ready"

    # grep（検索）で見つかる。
    hits = grep_tool.grep_search("Needle12345", world=world)
    assert any(h["doc_id"] == rel for h in hits)


def test_archive_update_rebuilds_delete_removes_and_lightweight_refresh_preserves(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcoffice2"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "a.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("doc1.docx", _docx_bytes("version1"))

    root = worlds.world_dir(world)
    archive_extract.sync_world_archives(world, root)
    dmd = worlds.derived_md_dir(world)
    office_md.build_derived(root, dmd, world=world)
    rel = "a.zip/doc1.docx"
    md_path = dmd / (rel + ".md")
    ev_path = worlds.derived_ir_dir(world) / (rel + ".evidence.json")
    assert md_path.read_text(encoding="utf-8").strip() == "version1"
    assert ev_path.is_file()

    # 孤児掃除の誤爆防止: 内容不変のまま軽量再生成（`world=` 配線）を呼んでも、展開先由来の
    # 派生物を「原本が消えた」と誤認して削除しない（検収で指摘された実害・直すまでは実際に消えていた）。
    assert office_md.rag_sidecars_missing(root, dmd, world=world) is False
    doc_result = office_md.refresh_document_ir(root, dmd, write_document_ir_sig_marker=False, world=world)
    assert doc_result.get("document_ir_failed", 0) == 0
    ev_result = office_md.refresh_evidence_ir(root, dmd, world=world)
    assert ev_result.get("evidence_ir_failed", 0) == 0 and ev_result.get("rag_failed", 0) == 0
    assert md_path.is_file() and ev_path.is_file()      # 誤って消されていない

    # 更新（内容変化）→ 再展開＋再変換で中身が置き換わる。
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("doc1.docx", _docx_bytes("version2"))
    archive_extract.sync_world_archives(world, root)
    rep2 = office_md.build_derived(root, dmd, world=world)
    assert rep2["converted"] == 1
    assert md_path.read_text(encoding="utf-8").strip() == "version2"

    # 削除（アーカイブごと消える）→ 派生物も消える（鏡）。
    zpath.unlink()
    archive_extract.sync_world_archives(world, root)
    office_md.build_derived(root, dmd, world=world)
    assert not md_path.is_file()
    assert not ev_path.is_file()


def test_ext_doc_returns_extracted_copy_of_archive_inner_file(monkeypatch, tmp_path):
    kb = _isolate_world_kb(monkeypatch, tmp_path)
    world = "arcextdoc1"
    wd = kb / world
    wd.mkdir(parents=True)
    zpath = wd / "b.zip"
    with zipfile.ZipFile(zpath, "w") as zf:
        zf.writestr("x/y.txt", "payload-content\n")

    root = worlds.world_dir(world)
    archive_extract.sync_world_archives(world, root)

    # `_enforce_world_scope`/`require_api_key` は DB（APIキー/スコープ）に依存する別契約のため、
    # ここではこの endpoint 固有のアーカイブ fallback ロジックだけを対象に、それ以外を無害化する
    # （`tests/api` 全体は別ゲートの対象・ここでは router を直接最小 app へ積んで DB 非依存にする）。
    monkeypatch.setattr(ext_api, "_enforce_world_scope", lambda *a, **k: None)
    app = FastAPI()
    app.include_router(ext_api.router)
    app.dependency_overrides[ext_api.require_api_key] = lambda: {"id": "test"}
    client = TestClient(app, raise_server_exceptions=True)

    r = client.get("/ext/v1/doc", params={"world": world, "path": "b.zip/x/y.txt"})
    assert r.status_code == 200
    assert r.content == b"payload-content\n"

    # アーカイブ自身（原本）は従来どおり原本ツリーから配信される。
    r2 = client.get("/ext/v1/doc", params={"world": world, "path": "b.zip"})
    assert r2.status_code in (404, 415)   # zip 自体は配信対象の doctype ではない（未対応種別・既存契約）
