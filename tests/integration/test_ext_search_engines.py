"""外部連携 API E2a〜b（`sherpa/search_service.py` のエンジン分離検索）の統合テスト。

要 ES/Neo4j 実接続（不達なら SKIP・tests/integration の既存流儀）。RRF融合の計算式・graph
マッピングの純粋ロジックは `tests/unit/test_search_service.py` で検証済み。ここでは実 ES/Neo4j
経路（`es_index.search`/`search_knn_only`/Neo4j 影響たどり）が search_service 経由で正しく
配線されていること、および既存 `es_index.search()` を1文字も壊していないことを検証する。
"""
from __future__ import annotations

import os
import time

import pytest
from _world_setup import TEST_WORLD_ID

from sherpa import es_index, search_service


def _es_or_skip():
    if not es_index.available():
        pytest.skip("ES 未起動")


def _neo4j_reachable() -> bool:
    import socket
    try:
        from urllib.parse import urlparse
        uri = os.environ.get("NEO4J_URI", "bolt://localhost:7687")
        host = urlparse(uri).hostname or "localhost"
        port = urlparse(uri).port or 7687
        with socket.create_connection((host, port), timeout=2):
            return True
    except OSError:
        return False


def _neo4j_or_skip():
    if not _neo4j_reachable():
        pytest.skip("Neo4j 未起動")


def _index_v1():
    """TEST_WORLD_ID（fixtures/corpus/v1 のエイリアス）を ES に索引する（既存 test_es_index.py と同一手順）。"""
    from _world_registry import register_test_world
    register_test_world(TEST_WORLD_ID)
    r = es_index.index_world(TEST_WORLD_ID)
    assert r["available"] and r["indexed"] > 0 and r["chunks"] > 0
    return r


# ===== E2a: keyword/vector エンジン（ES）=====

def test_keyword_engine_es():
    _es_or_skip()
    _index_v1()
    hits, reason = search_service._search_keyword(TEST_WORLD_ID, "消費税率", [], 5, None)
    assert reason is None
    assert hits and all(h["doc_id"] and h["snippet"] for h in hits)


def test_knn_only_no_bm25_leak(monkeypatch):
    """search_knn_only は純 kNN（BM25 の bool/match 節を含まない）。既存 hybrid search() とはボディが違う。"""
    _es_or_skip()
    _index_v1()
    from sherpa import embeddings
    orig_cfg, orig_embed, orig_req = embeddings.cfg, embeddings.embed, es_index._req
    embeddings.cfg = lambda settings=None, **kw: {"provider": "stub", "key": "x", "model": "stub", "dim": 8}
    embeddings.embed = lambda texts, c, **kw: [[0.1] * 8 for _ in texts]
    bodies = []

    def _cap(method, path, body=None, **kw):
        if isinstance(path, str) and path.endswith("/_search"):
            bodies.append(body)
        return orig_req(method, path, body, **kw)

    es_index._req = _cap
    try:
        es_index.index_world(TEST_WORLD_ID, settings={})   # dense_vector 付きで再索引（_meta 一致させる）
        hits, reason = es_index.search_knn_only(TEST_WORLD_ID, "消費税", k=3, settings={})
        assert reason is None and hits
        assert bodies, "kNN クエリが発行されていない"
        body = bodies[-1]
        assert "knn" in body and "query" not in body   # 純 kNN（bool/match の BM25 節が無い）
    finally:
        embeddings.cfg, embeddings.embed, es_index._req = orig_cfg, orig_embed, orig_req
        es_index.index_world(TEST_WORLD_ID, settings={})   # BM25 索引へ戻す（他テストへの影響防止）


def test_existing_search_unchanged():
    """既存 `es_index.search()`（ハイブリッド固定）の挙動が search_knn_only 追加後も不変。
    RV2（FBK-1・2026-09-01）: 返値は (hits, degrade_reason) タプルへ統一——通常の索引済み world
    では degrade しないため reason は None のまま（`search_knn_only`/`search_service` と同型）。"""
    _es_or_skip()
    _index_v1()
    hits, reason = es_index.search(TEST_WORLD_ID, "消費税率", k=5)
    assert reason is None
    assert hits and all("doc_id" in h and "score" in h for h in hits)


def test_scope_filter_all_engines():
    _es_or_skip()
    _index_v1()
    hits, reason = search_service._search_keyword(TEST_WORLD_ID, "バッチ", ["4期/03_開発"], 10, None)
    assert reason is None
    assert all(h["doc_id"].startswith("4期/03_開発") for h in hits)


# ===== E2b: graph エンジン（Neo4j・run_impact 委譲）=====

def test_graph_engine_impact():
    _neo4j_or_skip()
    from _world_setup import ensure_v1
    ensure_v1()
    hits, reason = search_service._search_graph(TEST_WORLD_ID, "消費税率", None, 10, None)
    assert reason is None
    assert hits, "影響たどりの結果が空（fixture の起点語が変わっていないか確認）"
    assert all(h["key"] for h in hits)
    assert any(h.get("judgement") in ("sure", "review", "presumed") for h in hits)


# ===== 公開エントリ: search() の統合（keyword+vector+graph 併用）=====

def test_public_search_combines_engines():
    """keyword(ES BM25)＋graph(Neo4j) の併用（vector は SHERPA_DISABLE_EMBED=1 の既定テスト環境では
    embedding_not_configured で degrade するため、埋め込み配線は test_knn_only_no_bm25_leak 側で検証済み）。
    """
    _es_or_skip()
    _neo4j_or_skip()
    _index_v1()
    from _world_setup import ensure_v1
    ensure_v1()
    res = search_service.search(TEST_WORLD_ID, "消費税率", engines=["keyword", "graph"], k=10)
    assert set(res["engines_used"]) == {"keyword", "graph"}
    assert res["degraded"] == []
    assert res["hits"]


# ===== 契約テスト: 個人 workspace は検索対象に出ない（CLAUDE.md 契約）=====

def test_search_never_returns_workspace():
    """個人 workspace に置いたファイルは ES/Neo4j 経由の search_service の hits に一切出ない
    （共有 KB のみが検索対象・CLAUDE.md 契約）。"""
    _es_or_skip()
    _index_v1()

    users_dir = __import__("pathlib").Path(os.environ.get("SHERPA_USERS_DIR", "data/users"))
    uid = f"extsearch_ws_{int(time.time() * 1000) % 100000}"
    marker = "エクストサーチワークスペース一意マーカーZZZ12345"
    ws_dir = users_dir / uid / "workspace" / "files"
    ws_dir.mkdir(parents=True, exist_ok=True)
    (ws_dir / "secret.txt").write_text(marker, encoding="utf-8")
    try:
        hits, reason = search_service._search_keyword(TEST_WORLD_ID, marker, [], 10, None)
        assert reason is None
        assert hits == []
    finally:
        import shutil
        shutil.rmtree(users_dir / uid, ignore_errors=True)
