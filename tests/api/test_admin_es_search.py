"""管理向け ES 検索 API（live ES 不要・temp world + stub・TestClient 経由）。

seam 整理（refactoring-plan フェーズ1）で素関数 `admin_es_search()` は撤去し、ルート
`_admin_es_search_endpoint`（`GET /admin/es/search`）に一本化した。認可（admin 必須）を含めて
検証するため TestClient 経由で叩く（`auth_disabled` fixture で合成 admin にする）。
"""
from __future__ import annotations

import pathlib
import tempfile

from fastapi.testclient import TestClient

from sherpa.api import app

W = "admin_tmp"


def test_admin_es_search_filters_scope_and_hides_paths(auth_disabled):
    from sherpa import es_index, worlds
    old_world_dir, old_search = worlds.world_dir, es_index.search
    calls = []
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "docs").mkdir()
        (root / "other").mkdir()
        (root / "docs" / "a.md").write_text("hit", encoding="utf-8")
        (root / "other" / "b.md").write_text("hit", encoding="utf-8")
        worlds.world_dir = lambda w: root if w == W else old_world_dir(w)

        def fake_search(world, query, scope_paths=None, k=20, settings=None, vector=True):
            calls.append({"world": world, "query": query, "scope_paths": scope_paths, "k": k, "vector": vector})
            # RV2（FBK-1・2026-09-01）: `es_index.search()` は (hits, degrade_reason) を返す。
            return [
                {"doc_id": "docs/a.md", "line": 7, "text": "ES hit", "score": 2.5, "ext": ".md", "path": "/tmp/secret"},
                {"doc_id": "other/b.md", "line": 1, "text": "範囲外", "score": 1.0, "ext": ".md"},
                {"doc_id": "missing.md", "line": 1, "text": "古い索引", "score": 9.0, "ext": ".md"},
            ], None

        es_index.search = fake_search
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/admin/es/search",
                      params={"world": W, "query": "hit", "scope_paths": ["docs"], "k": 20})
            assert r.status_code == 200, r.text
            body = r.json()
            assert body["world"] == W                  # 応答キーは world（version へ戻る回帰を検知）
            assert body["scope_paths"] == ["docs"]
            assert calls == [{"world": W, "query": "hit", "scope_paths": ["docs"], "k": 20, "vector": False}]
            assert body["hits"] == [{"doc_id": "docs/a.md", "line": 7, "snippet": "ES hit", "score": 2.5, "ext": ".md"}]
            assert "path" not in body["hits"][0] and "text" not in body["hits"][0]
        finally:
            worlds.world_dir, es_index.search = old_world_dir, old_search


def test_admin_es_search_passes_through_provenance(auth_disabled):
    """S2: ヒットに索引済みの由来（extraction_method/confidence/has_conflicts）を通す（あれば）。"""
    from sherpa import es_index, worlds
    old_world_dir, old_search = worlds.world_dir, es_index.search
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "docs").mkdir()
        (root / "docs" / "a.pdf").write_text("x", encoding="utf-8")
        (root / "docs" / "b.cbl").write_text("x", encoding="utf-8")
        worlds.world_dir = lambda w: root if w == W else old_world_dir(w)

        def fake_search(world, query, scope_paths=None, k=20, settings=None, vector=True):
            return [
                {"doc_id": "docs/a.pdf", "line": 3, "text": "OCR hit", "score": 2.1, "ext": ".pdf",
                 "extraction_method": "ocr", "confidence": 0.4, "has_conflicts": True},
                {"doc_id": "docs/b.cbl", "line": 1, "text": "source", "score": 1.0, "ext": ".cbl"},
            ], None

        es_index.search = fake_search
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/admin/es/search", params={"world": W, "query": "hit", "k": 20})
            assert r.status_code == 200, r.text
            hits = {h["doc_id"]: h for h in r.json()["hits"]}
            assert hits["docs/a.pdf"]["extraction_method"] == "ocr"
            assert hits["docs/a.pdf"]["confidence"] == 0.4
            assert hits["docs/a.pdf"]["has_conflicts"] is True
            # 来歴を持たないチャンク（ソース枝）はキーを付けない＝後方互換。
            assert "extraction_method" not in hits["docs/b.cbl"]
            assert "has_conflicts" not in hits["docs/b.cbl"]
        finally:
            worlds.world_dir, es_index.search = old_world_dir, old_search


def test_admin_es_search_tolerates_hit_without_line(auth_disabled):
    """rag_chunks 由来の ES ヒット（line キー無し）でも 200 で返り、line は null になる
    （`h.get("line")` の防御的取得・表示側 `web/ingest.js` の `h.line == null` 分岐で空欄表示）。"""
    from sherpa import es_index, worlds
    old_world_dir, old_search = worlds.world_dir, es_index.search
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "docs").mkdir()
        (root / "docs" / "a.docx").write_text("x", encoding="utf-8")
        worlds.world_dir = lambda w: root if w == W else old_world_dir(w)

        def fake_search(world, query, scope_paths=None, k=20, settings=None, vector=True):
            return [{"doc_id": "docs/a.docx", "text": "rag_chunks 由来", "score": 1.5, "ext": ".docx",
                     "chunk_id": "rc1"}], None

        es_index.search = fake_search
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/admin/es/search", params={"world": W, "query": "hit", "k": 20})
            assert r.status_code == 200, r.text
            hits = r.json()["hits"]
            assert hits == [{"doc_id": "docs/a.docx", "line": None, "snippet": "rag_chunks 由来",
                             "score": 1.5, "ext": ".docx"}]
        finally:
            worlds.world_dir, es_index.search = old_world_dir, old_search


def test_admin_es_search_rejects_unknown_scope(auth_disabled):
    from sherpa import es_index, worlds
    old_world_dir, old_search = worlds.world_dir, es_index.search
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "docs").mkdir()
        (root / "docs" / "a.md").write_text("hit", encoding="utf-8")
        worlds.world_dir = lambda w: root if w == W else old_world_dir(w)
        es_index.search = lambda *a, **kw: ([], None)
        try:
            c = TestClient(app, raise_server_exceptions=False)
            r = c.get("/admin/es/search",
                      params={"world": W, "query": "hit", "scope_paths": ["nope"], "k": 20})
            assert r.status_code == 422, r.text
        finally:
            worlds.world_dir, es_index.search = old_world_dir, old_search
