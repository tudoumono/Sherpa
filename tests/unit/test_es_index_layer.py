"""ES 検索の探す対象（層）フィルタ（`es_index.search`/`search_knn_only(layer=...)`・
docs/proposals/2026-08-29-調べ方ブロック.md §3.4）の単体テスト。

実 ES 不要（`_req`/`available`/`embeddings` をモックし、送信クエリボディの `filter` 節だけを検証する）。
"""
from __future__ import annotations

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa import es_index

# `classify_document` 確定値（`branch=="source"`＝code）で絞る（ext membership ではない・
# `sherpa.layer.es_filter` 参照・grep/agentic と同じ判定に揃える・§7 裁定10）。
_CODE_TERMS = {"term": {"branch": "source"}}
_DOCS_MUST_NOT = {"bool": {"must_not": {"term": {"branch": "source"}}}}


def _capture_req(monkeypatch):
    captured: dict = {}

    def fake_req(method, path, body=None, **kw):
        captured["body"] = body
        return {"hits": {"hits": []}}

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_req", fake_req)
    return captured


def _bm25_bool(body: dict) -> dict:
    """`search()` の BM25 クエリは I2（重要度ブースト）で `function_score` に包まれた
    （`es_index._importance_boost_query` 参照）——テストが見たい `bool`（`must`/`filter`）節は
    その内側の `query` にある。"""
    return body["query"]["function_score"]["query"]["bool"]


# ===== search()（BM25・vector=False で埋め込みを一切呼ばない経路）=====

def test_search_layer_omitted_or_both_adds_no_ext_filter(monkeypatch):
    captured = _capture_req(monkeypatch)
    es_index.search("v1", "q", k=5, vector=False)
    flt = _bm25_bool(captured["body"])["filter"]
    assert flt == []   # scope_paths も layer も無指定なら filter は空のまま（既存挙動と完全同一）
    captured2 = _capture_req(monkeypatch)
    es_index.search("v1", "q", k=5, vector=False, layer="both")
    assert _bm25_bool(captured2["body"])["filter"] == []


def test_search_layer_code_adds_terms_membership_filter(monkeypatch):
    captured = _capture_req(monkeypatch)
    es_index.search("v1", "q", k=5, vector=False, layer="code")
    flt = _bm25_bool(captured["body"])["filter"]
    assert _CODE_TERMS in flt


def test_search_layer_docs_adds_must_not_filter(monkeypatch):
    captured = _capture_req(monkeypatch)
    es_index.search("v1", "q", k=5, vector=False, layer="docs")
    flt = _bm25_bool(captured["body"])["filter"]
    assert _DOCS_MUST_NOT in flt


def test_search_scope_and_layer_filters_combine_with_and_semantics(monkeypatch):
    """範囲×層の組み合わせ: 両方指定すると `filter` 配列に両方の節が並ぶ（ES の filter は AND）。"""
    captured = _capture_req(monkeypatch)
    es_index.search("v1", "q", k=5, vector=False, scope_paths=["設計"], layer="code")
    flt = _bm25_bool(captured["body"])["filter"]
    assert {"terms": {"scopes": ["設計"]}} in flt
    assert _CODE_TERMS in flt
    assert len(flt) == 2


def test_search_scope_only_no_layer_leaves_ext_filter_absent(monkeypatch):
    captured = _capture_req(monkeypatch)
    es_index.search("v1", "q", k=5, vector=False, scope_paths=["設計"])
    flt = _bm25_bool(captured["body"])["filter"]
    assert flt == [{"terms": {"scopes": ["設計"]}}]


def test_search_layer_only_no_scope_leaves_scope_filter_absent(monkeypatch):
    captured = _capture_req(monkeypatch)
    es_index.search("v1", "q", k=5, vector=False, layer="docs")
    flt = _bm25_bool(captured["body"])["filter"]
    assert flt == [_DOCS_MUST_NOT]


# ===== search_knn_only()（純 kNN・E2a）=====

def _stub_embeddings(monkeypatch):
    from sherpa import embeddings
    ec = {"provider": "openai", "model": "text-embedding-3-small", "dim": 3}
    monkeypatch.setattr(embeddings, "cfg", lambda settings=None, **kw: ec)
    monkeypatch.setattr(es_index, "_index_meta", lambda world: {
        "embed_provider": "openai", "embed_model": "text-embedding-3-small", "dim": 3})
    monkeypatch.setattr(embeddings, "embed", lambda texts, c, **kw: [[0.1, 0.2, 0.3]])


def test_search_knn_only_layer_code_adds_terms_filter(monkeypatch):
    captured = _capture_req(monkeypatch)
    _stub_embeddings(monkeypatch)
    es_index.search_knn_only("v1", "q", k=5, layer="code")
    flt = captured["body"]["knn"]["filter"]
    assert _CODE_TERMS in flt


def test_search_knn_only_layer_omitted_matches_current_behavior(monkeypatch):
    captured = _capture_req(monkeypatch)
    _stub_embeddings(monkeypatch)
    es_index.search_knn_only("v1", "q", k=5)
    assert captured["body"]["knn"]["filter"] == []
