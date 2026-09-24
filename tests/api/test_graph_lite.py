"""②graph 軽量化: /graph の段階読み込み（limit/truncated）＋ ETag（If-None-Match→304）。

グラフは world グラフ由来（`build_effective_world`・Neo4j 不要）＝fixtures/corpus/v1 を使う。
"""
from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from sherpa.api import app

V = "v1"


@pytest.fixture(autouse=True)
def _compat_mode(monkeypatch):
    """ログイン不要の互換モード（合成 admin）で直接叩く。"""
    monkeypatch.setenv("SHERPA_AUTH_DISABLED", "1")


def _client():
    return TestClient(app)


def test_full_graph_all_and_shape():
    c = _client()
    r = c.get("/graph", params={"world": V, "limit": 0})   # limit=0＝全件（すべて表示）
    assert r.status_code == 200
    g = r.json()
    n = g["total_nodes"]
    assert n >= 2, "fixtures/corpus/v1 は複数ノードを持つ前提"
    assert g["truncated"] is False
    assert len(g["nodes"]) == n
    assert "signature" not in g                            # 署名は本体に漏らさない（ETag が担う）
    assert r.headers.get("etag")                           # ETag ヘッダが付く
    # counts は常に全体を表す
    ids = {x["id"] for x in g["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])


def test_limit_truncates_to_top_degree():
    c = _client()
    full = c.get("/graph", params={"world": V, "limit": 0}).json()
    n = full["total_nodes"]
    r = c.get("/graph", params={"world": V, "limit": 1})
    assert r.status_code == 200
    g = r.json()
    assert g["truncated"] is True
    assert len(g["nodes"]) == 1
    assert g["total_nodes"] == n and g["total_edges"] == full["total_edges"]
    ids = {x["id"] for x in g["nodes"]}
    assert all(e["source"] in ids and e["target"] in ids for e in g["edges"])


def test_response_and_etag_are_deterministic():
    c = _client()
    a = c.get("/graph", params={"world": V, "limit": 1})
    b = c.get("/graph", params={"world": V, "limit": 1})
    assert a.json()["nodes"] == b.json()["nodes"]          # 同一グラフ→同一応答
    assert a.headers["etag"] == b.headers["etag"]          # 同一 ETag


def test_etag_varies_by_limit():
    c = _client()
    e_all = c.get("/graph", params={"world": V, "limit": 0}).headers["etag"]
    e_one = c.get("/graph", params={"world": V, "limit": 1}).headers["etag"]
    assert e_all != e_one                                  # 表現（表示範囲）が違えば ETag も違う


def test_if_none_match_returns_304():
    c = _client()
    r1 = c.get("/graph", params={"world": V, "limit": 1})
    etag = r1.headers["etag"]
    r2 = c.get("/graph", params={"world": V, "limit": 1}, headers={"If-None-Match": etag})
    assert r2.status_code == 304
    assert r2.headers.get("etag") == etag
    assert not r2.content                                  # 304 は本体を返さない（再転送しない）


def test_if_none_match_mismatch_returns_200():
    c = _client()
    r = c.get("/graph", params={"world": V, "limit": 1},
              headers={"If-None-Match": '"g.stale.1"'})
    assert r.status_code == 200 and r.json()["nodes"]


def test_default_limit_applies_without_param():
    """limit 未指定でも env 既定（SHERPA_GRAPH_NODE_LIMIT）が効く（総数は total_nodes で分かる）。"""
    c = _client()
    r = c.get("/graph", params={"world": V})               # limit 省略＝サーバ既定
    assert r.status_code == 200
    g = r.json()
    assert "total_nodes" in g and "truncated" in g


def test_graph_view_failure_returns_503_not_silent(monkeypatch):
    """GRA-1是正#3: `graph_view`（world 世代プローブ＝キャッシュの鍵読み）が例外を出したら、
    握り潰して縮退させず明示的に 503 にする（silent degradation なしの家風）。"""
    from sherpa.routers import graph as graph_router

    def _boom(world, limit=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(graph_router, "graph_view", _boom)
    c = _client()
    r = c.get("/graph", params={"world": V, "limit": 1})
    assert r.status_code == 503


def test_graph_ask_status_summary_failure_returns_503_with_graph_message(monkeypatch):
    """GRA-1是正RV2#1: `graph_ask` の前段（`_knowledge_status_summary`→`graph_view`）が
    ValueError 以外の例外を出しても、生の 500 ではなくログ付き 503 にする。文言は AI/Neo4j の
    障害用ではなく `/graph` と同じ `_GRAPH_UNAVAILABLE_MESSAGE`（グラフ状態の取得に失敗）を使い、
    利用者が AI 障害と取り違えないようにする。"""
    from sherpa.routers import graph as graph_router

    def _boom(wid, scope_paths=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(graph_router, "_knowledge_status_summary", _boom)
    c = _client()
    r = c.post("/graph/ask", json={"question": "消費税率について", "world": V})
    assert r.status_code == 503
    assert r.json()["detail"] == graph_router._GRAPH_UNAVAILABLE_MESSAGE
