"""全文検索の失敗を、空・部分結果・「語を含まない」・「ベクトル設定の不一致」に化けさせない単体テスト。
モックは外部境界（ES の `_req`・埋め込みの `embeddings`）だけ。
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from sherpa import documents, embeddings, es_index
from sherpa.parts.read import fused_search

_EC = {"provider": "ollama", "url": "http://127.0.0.1:11434", "model": "m", "dim": 3}
_META = {"embed_provider": "ollama", "embed_model": "m", "dim": 3,
         "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}


def _hit(cid, doc):
    return {"_id": cid, "_score": 1.0, "_source": {"doc_id": doc, "line": 1, "text": "本文", "chunk_id": cid}}


@pytest.fixture
def env(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_index_meta_read", lambda w, idx=None: (es_index._index_meta(w), False))  # 読み取りの失敗種別は差し替えた `_index_meta` の値から
    monkeypatch.setattr(es_index, "_index_meta", lambda w, idx=None: dict(_META))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: dict(_EC))
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: {})
    monkeypatch.setattr(es_index, "_embed_query", lambda q, ec, world: ([[0.1, 0.2, 0.3]], 1))
    monkeypatch.setattr(es_index.embeddings, "vector_to_es_bytes", lambda v: [1, 2, 3])


@pytest.mark.parametrize("partial", [{"timed_out": True}, {"_shards": {"total": 3, "failed": 1}}])
def test_timed_out_or_partial_response_is_a_failure_not_an_empty_result(env, monkeypatch, partial):
    """時間切れ・一部の分割の失敗で返った応答を、通常の（空・部分の）結果にしない。"""
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": [_hit("c1", "a.md")]}, **partial})
    assert es_index.search("w", "質問", vector=False) == ([], "es_query_failed")
    assert es_index.search_exact("w", ["X1"]) == ([], "es_query_failed")


def test_fused_search_does_not_report_a_partial_es_response_as_complete(env, monkeypatch):
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"timed_out": True, "hits": {"hits": [_hit("c1", "a.md")]}})
    monkeypatch.setattr(documents, "existing_rels", lambda world, rels, **kw: set(rels))
    res = fused_search.search("w", "質問", engines=["keyword"], k=5)
    assert res["degraded"] == [{"engine": "keyword", "reason": "es_query_failed"}]
    assert "keyword" not in res["coverage"] and res["engines_used"] == [] and res["hits"] == []


def _hybrid_req(follow_up):
    """1 回目＝BM25（c1）、2 回目＝kNN（c2 が kNN だけで出る）、3 回目＝語の一致の追加照会（`follow_up`）。"""
    calls = []

    def req(method, path, body=None, **kw):
        calls.append(body)
        if len(calls) == 1:
            return {"hits": {"hits": [_hit("c1", "a.md")]}}
        if len(calls) == 2:
            return {"hits": {"hits": [_hit("c2", "b.md")]}}
        return follow_up()

    return req


def test_failed_keyword_follow_up_leaves_keyword_match_unknown_not_false(env, monkeypatch):
    def boom():
        raise OSError("down")

    monkeypatch.setattr(es_index, "_req", _hybrid_req(boom))
    hits, reason = es_index.search("w", "質問")
    by = {h["doc_id"]: h for h in hits}
    assert reason is None and by["a.md"]["keyword_match"] is True
    assert by["b.md"]["keyword_match"] is None                      # 「語を含まない」と断定しない


def test_confirmed_non_match_is_still_false(env, monkeypatch):
    monkeypatch.setattr(es_index, "_req", _hybrid_req(lambda: {"hits": {"hits": []}}))
    by = {h["doc_id"]: h for h in es_index.search("w", "質問")[0]}
    assert by["b.md"]["keyword_match"] is False


def test_unreadable_index_meta_is_not_reported_as_vector_mismatch(env, monkeypatch):
    """索引の素性を読めなかったときは、設定の不一致（再索引待ち）ではなく ES の失敗として返す。"""
    monkeypatch.setattr(es_index, "_index_meta_read", lambda w, idx=None: (es_index._index_meta(w), False))  # 読み取りの失敗種別は差し替えた `_index_meta` の値から
    monkeypatch.setattr(es_index, "_index_meta", lambda w, idx=None: None)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": []}})
    assert es_index.search("w", "質問") == ([], "es_query_failed")
    assert es_index.search_knn_only("w", "質問") == ([], "es_query_failed")
    monkeypatch.setattr(es_index, "_index_meta_read", lambda w, idx=None: (es_index._index_meta(w), False))  # 読み取りの失敗種別は差し替えた `_index_meta` の値から
    monkeypatch.setattr(es_index, "_index_meta", lambda w, idx=None: {})      # 読めて素性が空＝本当の不一致
    assert es_index.search_knn_only("w", "質問") == ([], "vector_feature_mismatch")


# ---- MCP は PostgreSQL へ行かない ----

def test_explicit_mode_that_cannot_run_returns_an_empty_result_with_the_reason(monkeypatch):
    """mode を明示した es_search は、埋め込みが使えなくても語の一致へ自動で切り替えない。省略した既定の hybrid だけが縮退する。"""
    from sherpa.parts.read import tools
    monkeypatch.setattr(documents, "existing_rels", lambda world, rels, **kw: set(rels))
    bm25 = [{"doc_id": "a.md", "line": 1, "text": "x", "ext": ".md"}]
    called = []
    monkeypatch.setattr(es_index, "search", lambda *a, **kw: called.append("search") or (bm25, "embedding_not_configured"))
    monkeypatch.setattr(es_index, "search_knn_only", lambda *a, **kw: called.append("knn") or ([], "embedding_not_configured"))

    def run(args):
        return tools.run_tool("es_search", {"query": "x", **args}, "w", None)[0]

    for mode in ("hybrid", "vector"):
        v = run({"mode": mode})
        assert v["hits"] == [] and v["mode_used"] == mode and v["degrade_reason"] == "embedding_not_configured"
    assert "search" in called and called.count("knn") == 1
    v = run({})                                                           # 省略（既定）は BM25 に縮退し、理由と方式を返す
    assert v["mode_used"] == "keyword" and v["degrade_reason"] == "embedding_not_configured" and v["hits"]
