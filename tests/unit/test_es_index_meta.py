"""ES チャンクの来歴メタ搬送・索引ソース選択・再索引判定（`needs_reindex`）・bulk バッチ化・
埋め込みキャッシュの単体テスト（実 ES 不要・外部境界 `_req` と embed API だけ差し替える）。
"""
from __future__ import annotations

import json
import urllib.error
from pathlib import Path

import pytest

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa import es_index
from sherpa.ingest import text_kind


class _Bulk:
    """`_req` の差し替え。bulk 送信だけを記録する。"""

    def __init__(self):
        self.payloads: list[str] = []
        self.paths: list[str] = []

    def req(self, method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            self.payloads.append(body)
            self.paths.append(path)
        return {}

    @property
    def bodies(self) -> list[dict]:
        return [json.loads(ln) for p in self.payloads
                for i, ln in enumerate(p.strip().split("\n")) if i % 2 == 1]

    def of(self, doc_id: str) -> list[dict]:
        return [b for b in self.bodies if b["doc_id"] == doc_id]


def _index_env(monkeypatch, docs=(), *, read=None, ec=None, tmp_path=None) -> _Bulk:
    """`index_world` を ES・embed 無しで走らせる共通土台。`ec` を渡すと埋め込み設定済み
    （実キャッシュ機構は `tmp_path` 配下を使い、`embeddings.embed` はテスト側で差し替える）。"""
    docs = list(docs)
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", read or (lambda w, d: "本文1\n本文2"))
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    if ec is None:
        monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
        monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    else:
        monkeypatch.setattr(es_index.worlds, "derived_dir", lambda w: tmp_path / w)
        monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec)
        monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    bulk = _Bulk()
    monkeypatch.setattr(es_index, "_req", bulk.req)
    return bulk


_EC = {"provider": "p", "model": "m", "dim": 3}


def _doc(name, **kw):
    return {"name": name, "md_path": None, "top_scope": "t", **kw}


def _write_meta(md, **meta):
    (md.parent / f"{md.name}.meta.json").write_text(json.dumps(meta), encoding="utf-8")


# ---- 来歴メタ（_provenance_meta / mapping / _parse_hits） ----

def test_provenance_meta_from_sidecar(tmp_path):
    def md_with(name, meta):
        md = tmp_path / name
        md.write_text("x", encoding="utf-8")
        if meta is not None:
            _write_meta(md, **meta)
        return {"md_path": str(md)}

    merged = md_with("a.docx.md", {
        "arm": "ooxml", "method": "ooxml", "confidence": 1.0, "notes": [], "merge": "deterministic-v1",
        "conflicts": [{"type": "numeric_only_in_secondary", "value": "9"}],
        "arms": [{"name": "ooxml"}, {"name": "markitdown"}]})
    assert es_index._provenance_meta(merged) == {
        "extraction_method": "ooxml", "confidence": 1.0, "has_conflicts": True}
    no_conflicts_key = md_with("scan.png.md", {
        "arm": "ocr", "method": "ocr", "confidence": 0.4, "notes": ["numeric_verified=false"]})
    assert es_index._provenance_meta(no_conflicts_key) == {"extraction_method": "ocr", "confidence": 0.4}
    empty_conflicts = md_with("b.docx.md", {"method": "ooxml", "confidence": 1.0, "conflicts": []})
    assert es_index._provenance_meta(empty_conflicts)["has_conflicts"] is False
    assert es_index._provenance_meta({"md_path": None}) == {}
    assert es_index._provenance_meta({}) == {}
    assert es_index._provenance_meta(md_with("c.md", None)) == {}


def test_mapping_has_extraction_rag_chunk_and_neighbor_fields():
    props = es_index._mapping(None, "kuromoji")["mappings"]["properties"]
    assert props["extraction_method"]["type"] == "keyword"
    assert props["confidence"]["type"] == "float"
    assert props["has_conflicts"]["type"] == "boolean"
    assert props["chunk_id"]["type"] == "keyword"
    assert props["locator"] == {"type": "object", "enabled": False}
    for key in ("previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path"):
        assert props[key]["type"] == "keyword"


_NEIGHBOR_KEYS = ("previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path")


def test_parse_hits_passes_through_meta_and_omits_when_absent():
    res = {"hits": {"hits": [{"_score": 2.5, "_source": {
        "doc_id": "a.docx", "line": 3, "text": "本文", "ext": ".docx",
        "extraction_method": "ocr", "confidence": 0.4, "has_conflicts": True}}]}}
    hit = es_index._parse_hits(res)[0]
    assert (hit["extraction_method"], hit["confidence"], hit["has_conflicts"]) == ("ocr", 0.4, True)

    plain = {"hits": {"hits": [{"_score": 1.0, "_source": {
        "doc_id": "a.cbl", "line": 1, "text": "行", "ext": ".cbl"}}]}}
    assert es_index._parse_hits(plain)[0] == {"doc_id": "a.cbl", "line": 1, "text": "行", "score": 1.0, "ext": ".cbl"}
    assert not any(k in es_index._parse_hits(plain)[0] for k in _NEIGHBOR_KEYS)

    rag = {"hits": {"hits": [{"_score": 3.0, "_source": {
        "doc_id": "a.docx", "text": "本文", "ext": ".docx", "chunk_id": "rc2",
        "locator": {"sheet": "S1", "cell_range": "B12"},
        "previous_chunk_id": "rc1", "next_chunk_id": "rc3", "parent_id": "region1",
        "logical_record_id": "lr1", "section_path": ["見出し1"]}}]}}
    hit = es_index._parse_hits(rag)[0]
    assert hit["chunk_id"] == "rc2" and hit["locator"] == {"sheet": "S1", "cell_range": "B12"}
    assert hit["line"] is None
    assert (hit["previous_chunk_id"], hit["next_chunk_id"], hit["parent_id"]) == ("rc1", "rc3", "region1")
    assert hit["logical_record_id"] == "lr1" and hit["section_path"] == ["見出し1"]


# ---- index_world: チャンク body への搬送 ----

def test_index_world_carries_provenance_to_chunks(monkeypatch, tmp_path):
    md = tmp_path / "a.docx.md"
    md.write_text("行1\n行2", encoding="utf-8")
    _write_meta(md, method="markitdown", confidence=0.6,
                conflicts=[{"type": "numeric_only_in_secondary", "value": "9"}])
    docs = [_doc("a.docx", md_path=str(md)), _doc("b.md")]
    bulk = _index_env(monkeypatch, docs, read=lambda w, d: (md.read_text(encoding="utf-8") if d["md_path"]
                                                           else "ソース本文1\nソース本文2"))
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: tmp_path)   # rag_chunks 無し＝legacy 縮退
    r = es_index.index_world("w")
    assert r["indexed"] == 2 and r["chunks"] == 2
    a_chunks, b_chunks = bulk.of("a.docx"), bulk.of("b.md")
    assert a_chunks and all(c["extraction_method"] == "markitdown" and c["confidence"] == 0.6
                            and c["has_conflicts"] is True for c in a_chunks)
    assert b_chunks and all("extraction_method" not in c and "confidence" not in c
                            and "has_conflicts" not in c for c in b_chunks)


@pytest.mark.parametrize("resolved", ["with_rule", "no_control_file"])
def test_index_world_carries_importance_meta_to_chunks(monkeypatch, tmp_path, resolved):
    bulk = _index_env(monkeypatch, [_doc("a.md"), _doc("b.md")])
    monkeypatch.setattr(es_index.worlds, "world_dir", lambda w: tmp_path)
    res = es_index.importance.Resolution(value="高", reason="契約書", config_path="_重要度.txt", rule_line=1)
    mapping = {"a.md": res} if resolved == "with_rule" else {}
    monkeypatch.setattr(es_index.importance, "resolve_for_world", lambda w, root=None: mapping)
    assert es_index.index_world("w")["indexed"] == 2
    for doc_id in ("a.md", "b.md"):
        chunks = bulk.of(doc_id)
        assert chunks
        if doc_id == "a.md" and mapping:
            assert all(c["importance"] == "高" and c["importance_reason"] == "契約書" for c in chunks)
        else:
            assert all("importance" not in c and "importance_reason" not in c for c in chunks)


def test_importance_boost_query_wraps_in_function_score_with_high_low_only():
    wrapped = es_index._importance_boost_query({"bool": {"must": [{"match": {"text": "q"}}], "filter": []}})
    fs = wrapped["function_score"]
    assert fs["query"] == {"bool": {"must": [{"match": {"text": "q"}}], "filter": []}}
    assert fs["score_mode"] == "first" and fs["boost_mode"] == "multiply"
    assert {f["filter"]["term"]["importance"] for f in fs["functions"]} == {"高", "低"}


def test_rerank_knn_by_importance(monkeypatch):
    monkeypatch.setattr(es_index, "_ES_IMPORTANCE_BOOST_HIGH", 2.0)
    monkeypatch.setattr(es_index, "_ES_IMPORTANCE_BOOST_LOW", 0.5)
    hits = [{"doc_id": "a", "score": 1.0, "importance": "低"}, {"doc_id": "b", "score": 1.0},
            {"doc_id": "c", "score": 1.0, "importance": "高"}]
    out = es_index._rerank_knn_by_importance(hits)
    assert [h["doc_id"] for h in out] == ["c", "b", "a"]
    assert out[0]["score"] == 2.0 and out[2]["score"] == 0.5
    # importance を誰も持たなければスコア・順序とも不変
    plain = [{"doc_id": "a", "score": 3.0}, {"doc_id": "b", "score": 2.0}, {"doc_id": "c", "score": 1.0}]
    out = es_index._rerank_knn_by_importance(list(plain))
    assert [h["doc_id"] for h in out] == ["a", "b", "c"] and [h["score"] for h in out] == [3.0, 2.0, 1.0]


def test_index_world_pass2_progress_resets_to_zero_and_advances_for_excluded_docs(monkeypatch):
    _index_env(monkeypatch, [_doc("skip.md", state="unreadable"), _doc("a.md")])
    calls = []
    r = es_index.index_world("w", progress=lambda done, total: calls.append((done, total)))
    assert r["indexed"] == 1
    assert calls[0] == (0, 2) and (1, 2) in calls and calls[-1] == (2, 2)


def test_index_world_excludes_unreadable_documents_even_if_reread_succeeds(monkeypatch):
    bulk = _index_env(monkeypatch, [_doc("bad.cbl", state="unreadable"), _doc("ok.md")],
                      read=lambda w, d: "本文")
    assert es_index.index_world("w")["indexed"] == 1
    assert {b["doc_id"] for b in bulk.bodies} == {"ok.md"}


@pytest.mark.parametrize("docs,texts,no_embed,embedded", [
    ([_doc("a.cbl", branch="source"), _doc("b.md", branch="office")],
     {"a.cbl": "コード本文", "b.md": "資料本文"}, "a.cbl", "b.md"),
    ([_doc("data.csv", branch="office", doctype=text_kind.DOCUMENT_DOCTYPE_LABEL), _doc("note.md", branch="office", doctype="設計書")],
     {"data.csv": "csv本文", "note.md": "設計書本文"}, "data.csv", "note.md"),
], ids=["branch_source", "light_text_document_label"])
def test_index_world_skips_embedding_for_source_and_light_text(monkeypatch, tmp_path, docs, texts, no_embed, embedded):
    bulk = _index_env(monkeypatch, docs, read=lambda w, d: texts[d["name"]], ec=_EC, tmp_path=tmp_path)
    embed_calls = []

    def fake_embed(texts_, ec, **kw):
        embed_calls.append(list(texts_))
        return [[0.1, 0.2, 0.3] for _ in texts_]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    r = es_index.index_world("w")
    assert embed_calls == [[texts[embedded]]]                   # 除外側のテキストは embed に一切渡さない
    assert bulk.of(no_embed) and all("embedding" not in c for c in bulk.of(no_embed))
    assert bulk.of(embedded) and all(c.get("embedding") == [0.1, 0.2, 0.3] for c in bulk.of(embedded))
    assert r["vectors"] is True


def test_index_world_includes_stage2_light_text_docs(monkeypatch):
    docs = [_doc("app.py", branch="source", doctype=text_kind.CODE_DOCTYPE_LABEL),
            _doc("README", branch="office", doctype=text_kind.DOCUMENT_DOCTYPE_LABEL)]
    read_calls = []

    def fake_read(w, d):
        read_calls.append(d["name"])
        return "本文"

    bulk = _index_env(monkeypatch, docs, read=fake_read)
    r = es_index.index_world("w")
    assert read_calls == ["app.py", "README"]
    assert r["indexed"] == 2 and r["chunks"] == 2
    assert {b["doc_id"] for b in bulk.bodies} == {"app.py", "README"}
    assert all("embedding" not in b for b in bulk.bodies)


@pytest.mark.parametrize("branch,text,expect_features", [
    ("source", "コード本文", True),     # 純コード world は embed 対象 0 件でも素性を書く
    ("office", "資料本文", False),      # 真の埋め込み失敗は素性を書かず次回 sync で再試行
], ids=["pure_code_world", "true_embed_failure"])
def test_embed_features_recorded_only_when_embedding_not_failed(monkeypatch, branch, text, expect_features):
    docs = [_doc("a.cbl" if branch == "source" else "設計.md", branch=branch)]
    _index_env(monkeypatch, docs, read=lambda w, d: text)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    ec = {"provider": "openai", "model": "text-embedding-3-small", "dim": 1536}
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)

    def fake_embed_cached(world, texts, ec_):
        assert (texts == []) is expect_features
        return (None, 0, 0)

    monkeypatch.setattr(es_index, "_embed_cached", fake_embed_cached)
    store: dict = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        store.clear()
        store.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    if expect_features:      # 失敗側は実 `_confirm_content_sig`（実件数が取れず content_sig を確定しない）を通す
        monkeypatch.setattr(es_index, "_confirm_content_sig",
                            lambda w, sig: store.__setitem__("content_sig", sig) if sig else None)
    r = es_index.index_world("w", content_sig="c1")
    assert r.get("error") is None
    assert ("embed_provider" in store) is expect_features
    if expect_features:
        assert store["embed_provider"] == "openai" and store["dim"] == 1536
    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(store))
    monkeypatch.setattr(es_index, "count", lambda w: 1)
    assert es_index.needs_reindex("w", "c1") is (not expect_features)


# ---- EMBED-3: doc 単位ストリーミング化（メモリ有界性・一様 degrade・再開性） ----

def _embed3_world(monkeypatch, tmp_path, n_docs: int, flush_chunks: int) -> _Bulk:
    monkeypatch.setattr(es_index, "_EMBED_FLUSH_CHUNKS", flush_chunks)
    docs = [_doc(f"d{i}.md", branch="office") for i in range(n_docs)]
    return _index_env(monkeypatch, docs, read=lambda w, d: f"本文{d['name']}", ec=_EC, tmp_path=tmp_path)


def test_index_world_embed_calls_bounded_by_flush_size_not_world_total(monkeypatch, tmp_path):
    _embed3_world(monkeypatch, tmp_path, n_docs=23, flush_chunks=5)
    call_sizes = []

    def fake_embed(texts, ec, **kw):
        call_sizes.append(len(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    assert es_index.index_world("w").get("error") is None
    assert call_sizes and max(call_sizes) <= 5
    assert sum(call_sizes) == 23


def test_index_world_embed_partial_failure_degrades_uniformly_no_doc_mixing(monkeypatch, tmp_path):
    bulk = _embed3_world(monkeypatch, tmp_path, n_docs=8, flush_chunks=3)
    call_n = {"n": 0}

    def fake_embed(texts, ec, **kw):
        call_n["n"] += 1
        return None if call_n["n"] == 2 else [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    r = es_index.index_world("w")
    assert r.get("error") is None and r["vectors"] is False
    assert len(bulk.bodies) == 8
    assert all("embedding" not in b for b in bulk.bodies)       # 成功済みバッチの doc も含め誰もベクトルを持たない


def test_index_world_resumes_embed_cache_across_failed_and_retried_runs(monkeypatch, tmp_path):
    _embed3_world(monkeypatch, tmp_path, n_docs=8, flush_chunks=3)
    seen: list = []
    fail_second = {"on": True}

    def fake_embed(texts, ec, **kw):
        seen.append(list(texts))
        if fail_second["on"] and len(seen) == 2:
            return None
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    assert es_index.index_world("w")["vectors"] is False
    fail_second["on"] = False
    seen.clear()
    assert es_index.index_world("w")["vectors"] is True
    assert sum(len(t) for t in seen) < 8                         # 1 回目に成功したバッチ分はキャッシュヒット


# ---- kill-switch（SHERPA_DISABLE_EMBED）・クラウド選択・system_settings スナップショット ----

def _boom_get_system_settings():
    raise AssertionError("SHERPA_DISABLE_EMBED 有効時に system_settings を読んではいけない")


def test_disable_embed_never_reads_system_settings(monkeypatch):
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    monkeypatch.setattr("sherpa.store.get_system_settings", _boom_get_system_settings)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: [])
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": []}})
    assert es_index.index_world("w").get("error") is None
    assert es_index.search("w", "query") == ([], "embedding_not_configured")
    assert es_index.search_knn_only("w", "query") == ([], "embedding_not_configured")


def test_index_world_fails_before_delete_when_cloud_selected_but_unavailable(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: True)
    assert es_index.index_world("w") == {
        "available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}
    assert delete_calls == []


def test_index_world_fails_before_delete_when_real_embed_call_fails(monkeypatch):
    _index_env(monkeypatch, [_doc("a.md")])
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "openai", "model": "m", "dim": 1536})
    embed_cached_calls = []

    def _fake_embed_cached(world, texts, ec):
        embed_cached_calls.append(texts)
        return None, 0, 0

    monkeypatch.setattr(es_index, "_embed_cached", _fake_embed_cached)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: True)
    r = es_index.index_world("w")
    assert r.pop("embed_elapsed_ms") >= 0
    assert r == {"available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}
    assert delete_calls == []
    assert embed_cached_calls and embed_cached_calls[0]


def test_index_world_still_graceful_when_embed_fails_and_cloud_never_selected(monkeypatch):
    _index_env(monkeypatch, [_doc("a.md")])
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "ollama", "model": "m", "dim": 768})
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    assert es_index.index_world("w").get("error") is None
    assert delete_calls == ["w"]


def test_search_knn_only_distinguishes_cloud_unavailable_from_not_configured(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: True)
    assert es_index.search_knn_only("w", "query") == ([], "embedding_cloud_unavailable")
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    assert es_index.search_knn_only("w", "query") == ([], "embedding_not_configured")


def test_search_knn_only_reacts_to_embed_algo_mismatch(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: {
        "provider": "openai", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "embed_provider": "openai", "embed_model": "m", "dim": 3,
        "embed_algo": es_index.embeddings.EMBEDDING_INPUT_ALGORITHM_ID})
    monkeypatch.setattr(es_index.embeddings, "embed", lambda qs, ec, world=None: [[0.1, 0.2, 0.3]])
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": []}})
    assert es_index.search_knn_only("w", "query")[1] is None
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "embed_provider": "openai", "embed_model": "m", "dim": 3, "embed_algo": "old-algo-v0"})
    assert es_index.search_knn_only("w", "query") == ([], "vector_feature_mismatch")


@pytest.mark.parametrize("call", [
    lambda: es_index.search_knn_only("w", "query")[1],
    lambda: es_index.search("w", "query")[1],
    lambda: es_index.index_world("w")["error"],
], ids=["search_knn_only", "search", "index_world"])
def test_one_system_settings_snapshot_shared_by_cfg_and_cloud_check(monkeypatch, call):
    """`store.get_system_settings()` は 1 回だけ読み、`cfg()` と `cloud_selected_but_unavailable()` へ
    同じオブジェクトを渡す（別々に読むと admin 更新で判定が食い違いうる）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": []}})
    sentinel = {"cloud_provider": "openai"}
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    read_calls = []

    def _spy():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy)
    seen = []

    def _fake_cfg(settings=None, *, system_settings=None):
        seen.append(system_settings)
        return None

    def _fake_unavailable(system_settings=None):
        seen.append(system_settings)
        return True

    monkeypatch.setattr(es_index.embeddings, "cfg", _fake_cfg)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", _fake_unavailable)
    assert call() == "embedding_cloud_unavailable"
    assert read_calls == [1]
    assert seen and all(s is sentinel for s in seen)


# ---- search: hybrid / 素性不一致 ----

_HYBRID_META = {"embed_provider": "openai", "embed_model": "m", "dim": 3}


def test_search_hybrid_failure_with_bm25_success_is_hybrid_query_failed(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: dict(
        provider="openai", model="m", dim=3))
    monkeypatch.setattr(es_index.embeddings, "embed", lambda texts, ec, **kw: [[0.1, 0.2, 0.3]])
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)

    def _fake_req(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/_mapping"):
            return {"idx": {"mappings": {"_meta": {
                **_HYBRID_META, "embed_algo": es_index.embeddings.EMBEDDING_INPUT_ALGORITHM_ID}}}}
        if method == "POST" and path.endswith("/_search"):
            bool_q = (body or {}).get("query", {}).get("function_score", {}).get("query", {}).get("bool", {})
            if "should" in bool_q:                      # hybrid（match/knn を bool.should に並べる形）
                raise RuntimeError("hybrid query failed (dimension mismatch etc.)")
            return {"hits": {"hits": [{"_source": {"doc_id": "a.md", "line": 1, "text": "hit"}, "_score": 1.0}]}}
        return {}

    monkeypatch.setattr(es_index, "_req", _fake_req)
    hits, reason = es_index.search("w", "query")
    assert reason == "hybrid_query_failed"
    assert hits and hits[0]["doc_id"] == "a.md"


def _mismatch_search_env(monkeypatch, *, meta, bm25_fails=False):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "openai", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    embed_calls = []

    def _embed(texts, ec, **kw):
        embed_calls.append(texts)
        raise AssertionError("query embedding must not be called on feature mismatch")
    monkeypatch.setattr(es_index.embeddings, "embed", _embed)

    def _fake_req(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/_mapping"):
            return {"idx": {"mappings": {"_meta": meta}}}
        if method == "POST" and path.endswith("/_search"):
            if bm25_fails:
                raise urllib.error.URLError("bm25 failed")
            return {"hits": {"hits": [{"_source": {"doc_id": "a.md", "line": 1, "text": "hit"}, "_score": 1.0}]}}
        return {}
    monkeypatch.setattr(es_index, "_req", _fake_req)
    return embed_calls


@pytest.mark.parametrize("meta", [
    {"embed_provider": "azure", "embed_model": "m", "dim": 3},
    {"embed_provider": "openai", "embed_model": "m2", "dim": 3},
    {"embed_provider": "openai", "embed_model": "m", "dim": 1536},
    {},
])
def test_search_vector_feature_mismatch_reports_reason_keeps_bm25_and_skips_embed(monkeypatch, meta):
    embed_calls = _mismatch_search_env(monkeypatch, meta=meta)
    hits, reason = es_index.search("w", "query")
    assert reason == "vector_feature_mismatch"
    assert hits and hits[0]["doc_id"] == "a.md"
    assert embed_calls == []


def test_search_vector_false_never_reports_feature_mismatch(monkeypatch):
    embed_calls = _mismatch_search_env(monkeypatch, meta={"embed_provider": "azure", "embed_model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda *a, **k: (_ for _ in ()).throw(AssertionError("cfg must not be called")))
    hits, reason = es_index.search("w", "query", vector=False)
    assert reason is None and hits and embed_calls == []


def test_search_embedding_unresolved_keeps_existing_reason_not_mismatch(monkeypatch):
    _mismatch_search_env(monkeypatch, meta={"embed_provider": "azure", "embed_model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: True)
    assert es_index.search("w", "query")[1] == "embedding_cloud_unavailable"
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    assert es_index.search("w", "query")[1] == "embedding_not_configured"


def test_search_feature_mismatch_with_bm25_failure_prefers_es_query_failed(monkeypatch):
    _mismatch_search_env(monkeypatch, meta={"embed_provider": "openai", "embed_model": "m", "dim": 8},
                         bm25_fails=True)
    assert es_index.search("w", "query") == ([], "es_query_failed")


def test_degrade_vocabulary_includes_new_reasons():
    from sherpa.parts.read import fused_search
    assert "vector_feature_mismatch" in fused_search.DEGRADE_REASONS
    assert "hybrid_query_failed" in fused_search.DEGRADE_REASONS


@pytest.mark.parametrize("weight,skewed", [(0.5, False), (0.8, True)])
def test_search_hybrid_query_boost_follows_weight(monkeypatch, weight, skewed):
    monkeypatch.setattr(es_index, "_HYBRID_WEIGHT", weight)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        **_HYBRID_META, "embed_algo": es_index.embeddings.EMBEDDING_INPUT_ALGORITHM_ID})
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: dict(
        provider="openai", model="m", dim=3))
    monkeypatch.setattr(es_index.embeddings, "embed", lambda qs, ec, world=None: [[0.1, 0.2, 0.3]])
    captured = {}

    def fake_req(method, path, body=None, **kw):
        captured["body"] = body
        return {"hits": {"hits": []}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    es_index.search("w", "query")
    body = captured["body"]
    assert "knn" not in body                                      # knn は bool.should の中へ移している
    should = body["query"]["function_score"]["query"]["bool"]["should"]
    if not skewed:                                                 # 既定配分は boost キー自体を書かない
        assert should[0] == {"match": {"text": {"query": "query", "_name": es_index._KEYWORD_QUERY_NAME}}}
        assert "boost" not in should[1]["knn"]
    else:
        match_boost, knn_boost = should[0]["match"]["text"]["boost"], should[1]["knn"]["boost"]
        assert match_boost > knn_boost and round(match_boost + knn_boost, 6) == 2.0


# ---- _meta の記録と再索引判定（needs_reindex） ----

@pytest.mark.parametrize("chunk_lines,expected", [(40, None), (80, 80)])
def test_index_world_records_signatures_in_meta(monkeypatch, chunk_lines, expected):
    _index_env(monkeypatch)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-xyz")
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-xyz")
    monkeypatch.setattr(es_index, "_CHUNK_LINES", chunk_lines)
    captured = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        captured.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    es_index.index_world("w")
    assert captured["mapping_version"] == es_index.ES_MAPPING_VERSION
    assert captured["arms_sig"] == "sig-xyz" and captured["analyzer_config_sig"] == "acfg-xyz"
    assert captured.get("chunk_lines") == expected                # 既定粒度は書かない


def _reindex_env(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 40)


def _full_meta(**over) -> dict:
    meta = {"content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
            "arms_sig": "sig-A", "analyzer_config_sig": "acfg-A"}
    meta.update(over)
    return {k: v for k, v in meta.items() if v is not _DROP}


_DROP = object()


@pytest.mark.parametrize("meta_over,patches,expected", [
    ({}, {}, False),
    ({}, {"_analyzer_config_sig": lambda: "acfg-B"}, True),
    ({"analyzer_config_sig": _DROP}, {}, True),
    ({}, {"_arms_config_sig": lambda: "sig-B"}, True),
    ({"arms_sig": _DROP, "mapping_version": _DROP, "analyzer_config_sig": _DROP}, {}, True),
    ({"mapping_version": "1"}, {}, True),
    ({"mapping_version": "2"}, {}, True),
    ({"mapping_version": "3"}, {}, True),
    ({"mapping_version": "5"}, {}, True),
    ({"mapping_version": "6"}, {}, True),
    ({"mapping_version": "7"}, {}, True),
    ({"chunk_lines": 40}, {}, False),
    ({"chunk_lines": 40}, {"_CHUNK_LINES": 80}, True),
    ({}, {"_CHUNK_LINES": 80}, True),          # chunk_lines 欠落は旧既定 40 として扱う
])
def test_needs_reindex_reacts_to_each_dimension(monkeypatch, meta_over, patches, expected):
    _reindex_env(monkeypatch)
    for name, value in patches.items():
        monkeypatch.setattr(es_index, name, value)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: _full_meta(**meta_over))
    assert es_index.needs_reindex("w", "c1") is expected


def test_needs_reindex_reacts_to_embed_algo_drift(monkeypatch):
    _reindex_env(monkeypatch)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: {
        "provider": "openai", "model": "m", "dim": 3})
    base = _full_meta(embed_provider="openai", embed_model="m", dim=3)
    for algo, expected in ((_DROP, True), (es_index.embeddings.EMBEDDING_INPUT_ALGORITHM_ID, False),
                           ("old-algo-v0", True)):
        meta = dict(base) if algo is _DROP else dict(base, embed_algo=algo)
        monkeypatch.setattr(es_index, "_index_meta", lambda w, m=meta: dict(m))
        assert es_index.needs_reindex("w", "c1") is expected


def test_needs_reindex_reacts_to_human_md_drift_regardless_of_rag_es_setting(monkeypatch):
    from sherpa.ingest import office_md
    # `_human_md_config_sig` は実関数を使うので `_reindex_env` は使わない
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: None)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vNEW")

    def meta_with(sig):
        return lambda w: {"content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
                          "arms_sig": "sig-A", "human_md_sig": sig}

    monkeypatch.setattr(es_index, "_index_meta", meta_with("human-md-vOLD"))
    assert es_index.needs_reindex("w", "c1") is True
    assert es_index.needs_reindex("w", "c1") is True
    monkeypatch.setattr(es_index, "_index_meta", meta_with("human-md-vNEW"))
    assert es_index.needs_reindex("w", "c1") is False


def test_analyzer_config_sig_survives_json_round_trip():
    raw_tuple = es_index.analyzer_registry.config_signature()
    assert json.loads(json.dumps(raw_tuple)) != raw_tuple        # 対照: 生のタプルは JSON 往復で型が変わる
    sig = es_index._analyzer_config_sig()
    assert isinstance(sig, str)
    assert json.loads(json.dumps(sig)) == sig == es_index._analyzer_config_sig()


def test_stale_mapping_v4_index_triggers_reindex_once_then_stays_stable(monkeypatch):
    _index_env(monkeypatch)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    store = {"content_sig": "c1", "mapping_version": "4", "arms_sig": "sig-A"}
    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(store))
    monkeypatch.setattr(es_index, "_confirm_content_sig",
                        lambda w, sig: store.__setitem__("content_sig", sig) if sig else None)
    ensure_calls = {"n": 0}

    def fake_ensure_index(w, dim=None, emeta=None):
        ensure_calls["n"] += 1
        store.clear()
        store.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    assert es_index.needs_reindex("w", "c1") is True
    es_index.index_world("w", content_sig="c1")
    assert ensure_calls["n"] == 1
    assert es_index.needs_reindex("w", "c1") is False


# ---- _arms_config_sig / _human_md_config_sig / confirm_human_md_meta ----

def test_arms_config_sig_failsafe_on_error(monkeypatch):
    from sherpa.ingest import office_md

    def _boom():
        raise RuntimeError("構成読み取り失敗")

    monkeypatch.setattr(office_md, "_current_arms_sig", _boom)
    assert es_index._arms_config_sig() is None


def _human_md_world(monkeypatch, tmp_path, *, render_drift, es_drift):
    from sherpa import worlds as worlds_mod
    from sherpa.ingest import office_md
    wd = tmp_path / "world"
    wd.mkdir(exist_ok=True)
    dmd = tmp_path / "derived"
    dmd.mkdir(exist_ok=True)
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: dmd)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vX")
    monkeypatch.setattr(office_md, "human_md_sig_drift", lambda wd_, dmd_, world=None: render_drift)
    monkeypatch.setattr(office_md, "human_md_es_sig_drift", lambda dmd_: es_drift)
    return office_md


def test_human_md_config_sig_value_and_failsafe(monkeypatch):
    from sherpa.ingest import office_md
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vX")
    assert es_index._human_md_config_sig("w") == "human-md-vX"          # 未登録 world は pending 評価の対象外
    assert es_index._human_md_config_sig("w") == "human-md-vX"

    def _boom():
        raise RuntimeError("構成読み取り失敗")

    monkeypatch.setattr(office_md, "_current_human_md_sig", _boom)
    assert es_index._human_md_config_sig("w") is None


def test_human_md_config_sig_fail_closed_while_world_has_pending_human_md_drift(monkeypatch, tmp_path):
    office_md = _human_md_world(monkeypatch, tmp_path, render_drift=True, es_drift=True)
    assert es_index._human_md_config_sig("w") == es_index._HUMAN_MD_PENDING_SENTINEL
    monkeypatch.setattr(office_md, "human_md_sig_drift", lambda wd, dmd, world=None: False)
    assert es_index._human_md_config_sig("w") == es_index._HUMAN_MD_PENDING_SENTINEL   # ES 側マーカーが残る間
    monkeypatch.setattr(office_md, "human_md_es_sig_drift", lambda dmd: False)
    assert es_index._human_md_config_sig("w") == "human-md-vX"


def test_confirm_human_md_meta_updates_field_and_converges_needs_reindex(monkeypatch, tmp_path):
    _human_md_world(monkeypatch, tmp_path, render_drift=False, es_drift=False)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    meta = _full_meta(human_md_sig=None, world_id="w")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(meta))
    assert es_index.needs_reindex("w", "c1") is True             # human_md_sig=None ≠ 現行（収束前）

    put_calls: list[tuple] = []

    def _fake_req(method, path, body=None, **kw):
        if method == "PUT" and path.endswith("/_mapping"):
            put_calls.append((path, body))
            meta.update(body["_meta"])
        return {}

    monkeypatch.setattr(es_index, "_req", _fake_req)
    assert es_index.confirm_human_md_meta("w") is True
    assert len(put_calls) == 1
    assert put_calls[0][1]["_meta"]["human_md_sig"] == "human-md-vX"
    assert put_calls[0][1]["_meta"]["content_sig"] == "c1"       # 既存フィールドは保持
    assert es_index.needs_reindex("w", "c1") is False


@pytest.mark.parametrize("render_drift,index_meta", [(True, {}), (False, None)], ids=["still_pending", "meta_get_failed"])
def test_confirm_human_md_meta_does_not_put(monkeypatch, tmp_path, render_drift, index_meta):
    """pending の間、および `_meta` の GET 失敗（None・Put Mapping は `_meta` を丸ごと置換するため
    `{}` 扱いで PUT すると既存フィールドを消す）の間は PUT しない。"""
    _human_md_world(monkeypatch, tmp_path, render_drift=render_drift, es_drift=False)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: index_meta)
    put_calls: list[tuple] = []
    monkeypatch.setattr(es_index, "_req", lambda *a, **kw: put_calls.append(a) or {})
    assert es_index.confirm_human_md_meta("w") is False
    assert put_calls == []


def test_confirm_content_sig_skips_put_when_meta_get_fails(monkeypatch):
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: None)
    put_calls: list[tuple] = []
    monkeypatch.setattr(es_index, "_req", lambda *a, **kw: put_calls.append(a) or {})
    es_index._confirm_content_sig("w", "sig-A")
    assert put_calls == []


# ---- bulk 途中失敗の wipe（fail-closed） ----

@pytest.mark.parametrize("meta", [{"content_sig": "c1", "mapping_version": "6", "world_id": "w"}, None],
                         ids=["meta_ok", "meta_get_failed"])
def test_wipe_after_bulk_failure_drops_content_sig_when_delete_fails(monkeypatch, meta):
    monkeypatch.setattr(es_index, "delete_world", lambda w: False)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: meta)
    sent = []
    monkeypatch.setattr(es_index, "_req", lambda method, path, body=None, **kw: sent.append((method, path, body)) or {})
    es_index._wipe_after_bulk_failure("w")
    assert len(sent) == 1 and sent[0][0] == "PUT" and sent[0][1].endswith("/_mapping")
    assert "content_sig" not in sent[0][2]["_meta"]
    if meta:
        assert sent[0][2]["_meta"]["world_id"] == "w"


def test_wipe_after_bulk_failure_edge_cases(monkeypatch):
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    called = []
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: called.append(a) or {})
    es_index._wipe_after_bulk_failure("w")
    assert called == []                                           # 成功したら余計な PUT を出さない

    monkeypatch.setattr(es_index, "delete_world", lambda w: False)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {"content_sig": "c1"})

    def boom(*a, **k):
        raise RuntimeError("es down")

    monkeypatch.setattr(es_index, "_req", boom)
    es_index._wipe_after_bulk_failure("w")                        # 例外を漏らさない


def test_content_sig_written_only_after_all_batches_succeed(monkeypatch):
    _index_env(monkeypatch)
    emeta_at_create = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        emeta_at_create.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    confirmed = []
    monkeypatch.setattr(es_index, "_confirm_content_sig", lambda w, sig: confirmed.append(sig))
    es_index.index_world("w", content_sig="c1")
    assert "content_sig" not in emeta_at_create
    assert confirmed == ["c1"]


def test_content_sig_not_confirmed_when_a_batch_fails(monkeypatch):
    _index_env(monkeypatch, [_doc("a.cbl", branch="source")], read=lambda w, d: "本文")
    confirmed = []
    monkeypatch.setattr(es_index, "_confirm_content_sig", lambda w, sig: confirmed.append(sig))
    monkeypatch.setattr(es_index, "_wipe_after_bulk_failure", lambda w: None)

    def boom(method, path, body=None, ndjson=False):
        raise RuntimeError("bulk down")

    monkeypatch.setattr(es_index, "_req", boom)
    assert es_index.index_world("w", content_sig="c1").get("error") == "bulk_failed"
    assert confirmed == []


# ---- rag_chunks / rag.md の安全な読み取りと検証 ----

def test_rag_chunk_source_exts_limited_to_office_pdf():
    exts = es_index._rag_chunk_source_exts()
    assert {".docx", ".pdf", ".xlsx", ".pptx"} <= set(exts)
    assert not {".cbl", ".jcl", ".txt", ".md"} & set(exts)         # ソース/テキストは同名 sidecar があっても拾わない


def test_rag_chunk_es_id_is_namespaced_by_doc_id():
    id_a = es_index._rag_chunk_es_id("a.docx", "dup-chunk-id")
    assert id_a != es_index._rag_chunk_es_id("b.docx", "dup-chunk-id")
    assert id_a == es_index._rag_chunk_es_id("a.docx", "dup-chunk-id")


@pytest.mark.parametrize("func,suffix,kind", [
    (es_index._safe_rag_chunks_path, ".rag_chunks.jsonl", "absent"),
    (es_index._safe_rag_chunks_path, ".rag_chunks.jsonl", "regular"),
    (es_index._safe_rag_chunks_path, ".rag_chunks.jsonl", "symlink"),
    (es_index._safe_rag_md_path, ".rag.md", "absent"),
    (es_index._safe_rag_md_path, ".rag.md", "regular"),
    (es_index._safe_rag_md_path, ".rag.md", "symlink"),
])
def test_safe_rag_sidecar_path(tmp_path, func, suffix, kind):
    derived = tmp_path / "derived"
    derived.mkdir()
    target = derived / f"a.docx{suffix}"
    if kind == "regular":
        target.write_text("x", encoding="utf-8")
    elif kind == "symlink":
        outside = tmp_path / "outside"
        outside.write_text("<!-- chunk:c1 -->\nx\n", encoding="utf-8")
        target.symlink_to(outside)
    path, reason = func(derived, "a.docx")
    if kind == "absent":
        assert path is None and reason is None                     # 旧 world の未再 sync は報告対象ではない
    elif kind == "regular":
        assert path == target.resolve() and reason is None
    else:
        assert path is None and reason == "symlink_rejected"


@pytest.mark.parametrize("md,expected_bodies,expected_reason", [
    ("見出し\n\n<!-- chunk:c1 -->\n本文1\n\n<!-- chunk:c2 -->\n本文2\n", {"c1": "本文1", "c2": "本文2"}, None),
    ("旧形式の本文だけ\n", {}, "rag_md_no_anchors"),
    ("<!-- chunk:c1 -->\nA\n<!-- chunk:c1 -->\nB\n", {}, "rag_md_duplicate_anchor"),
])
def test_parse_rag_md_chunks(md, expected_bodies, expected_reason):
    assert es_index._parse_rag_md_chunks(md) == (expected_bodies, expected_reason)


def test_chunk_locator_and_context_meta():
    first = {"sheet": "S1", "cell_range": "A1"}
    assert es_index._chunk_locator({"citations": [{"locator": first}, {"locator": {"sheet": "S1"}}]}) == first
    for chunk in ({}, {"citations": []}, {"citations": [{"evidence_id": "e1"}]}):
        assert es_index._chunk_locator(chunk) is None
    full = {"previous_chunk_id": "a", "next_chunk_id": "b", "parent_id": "p",
            "logical_record_id": "l", "section_path": ["見出し1", "見出し2"]}
    assert es_index._chunk_context_meta(full) == full
    assert es_index._chunk_context_meta({}) == {}
    # 型不正はキーを立てないだけ（無効化しない）
    assert es_index._chunk_context_meta({"previous_chunk_id": 123, "parent_id": "", "section_path": "x"}) == {}
    assert es_index._chunk_context_meta({"section_path": ["ok", 5]}) == {}
    assert es_index._chunk_context_meta({"section_path": []}) == {}


_ROW1 = {"chunk_id": "rc1", "source_rel_path": "a.docx", "citations": [{"locator": {"sheet": "S1"}}]}


def _write_jsonl(path, *rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _write_rag_md(path, *chunks):
    lines = []
    for chunk_id, body in chunks:
        lines += [f"<!-- chunk:{chunk_id} -->", body, ""]
    path.write_text("\n".join(lines), encoding="utf-8")


def _rag_pair(tmp_path, rows=(_ROW1,), chunks=(("rc1", "本文"),)):
    p, md = tmp_path / "a.docx.rag_chunks.jsonl", tmp_path / "a.docx.rag.md"
    _write_jsonl(p, *rows)
    _write_rag_md(md, *chunks)
    return p, md


def test_validate_rag_chunks_builds_entries_without_line(tmp_path):
    p, md = _rag_pair(tmp_path)
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx", "ext": ".docx"})
    assert reason is None
    assert ids == [es_index._rag_chunk_es_id("a.docx", "rc1")] and texts == ["本文"]
    assert bodies == [{"doc_id": "a.docx", "ext": ".docx", "chunk_id": "rc1", "text": "本文",
                       "locator": {"sheet": "S1"}}]


def test_validate_rag_chunks_omits_locator_and_context_meta_when_absent(tmp_path):
    p, md = _rag_pair(tmp_path, rows=({"chunk_id": "rc1", "source_rel_path": "a.docx"},))
    _, bodies, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx"})
    assert reason is None                                            # 隣接キーが無くても縮退しない
    assert "locator" not in bodies[0] and not any(k in bodies[0] for k in _NEIGHBOR_KEYS)


def test_validate_rag_chunks_includes_context_meta_when_present(tmp_path):
    p, md = _rag_pair(tmp_path, rows=({
        "chunk_id": "rc1", "source_rel_path": "a.docx", "previous_chunk_id": "rc0", "next_chunk_id": "rc2",
        "parent_id": "region1", "logical_record_id": "lr1", "section_path": ["見出し1"]},))
    _, bodies, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx"})
    assert reason is None
    assert (bodies[0]["previous_chunk_id"], bodies[0]["next_chunk_id"], bodies[0]["parent_id"]) == ("rc0", "rc2", "region1")
    assert bodies[0]["logical_record_id"] == "lr1" and bodies[0]["section_path"] == ["見出し1"]


_ROW_A = {"chunk_id": "rc1", "source_rel_path": "a.docx"}


@pytest.mark.parametrize("rows,chunks,reason", [
    ([{"source_rel_path": "a.docx"}], [("rc1", "本文")], "missing_chunk_id"),
    ([_ROW_A], [("rc-other", "本文")], "rag_md_anchor_missing"),
    ([_ROW_A], [("rc1", "本文"), ("rc-surplus", "余剰")], "rag_md_anchor_surplus"),
    ([_ROW_A], [("rc1", "本文A"), ("rc1", "本文B")], "rag_md_duplicate_anchor"),
    (["not", "a", "dict"], [("rc1", "本文")], "row_not_object"),
    ([{"chunk_id": "rc1", "source_rel_path": "b.docx"}], [("rc1", "本文")], "source_rel_path_mismatch"),
    ([{"chunk_id": "dup", "source_rel_path": "a.docx"}] * 2, [("dup", "本文")], "duplicate_chunk_id"),
])
def test_validate_rag_chunks_invalidates_whole_file(tmp_path, rows, chunks, reason):
    p, md = _rag_pair(tmp_path, rows=rows, chunks=chunks)
    ids, bodies, texts, got = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx"})
    assert got == reason and ids == [] and bodies == [] and texts == []


def test_validate_rag_chunks_other_invalid_states(tmp_path):
    p, md = _rag_pair(tmp_path)
    p.write_text(json.dumps(_ROW1) + "\nnot json\n", encoding="utf-8")        # 2 行目が壊れていれば 1 行目も採らない
    ids, bodies, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "invalid_json" and ids == [] and bodies == []

    p, md = _rag_pair(tmp_path, rows=({"chunk_id": "rc1", "search_text": "旧", "source_rel_path": "a.docx"},))
    md.write_text("# AI検索用文書\n\n旧形式のrag.md本文（アンカー無し）\n", encoding="utf-8")
    assert es_index._validate_rag_chunks(p, md, "a.docx", {})[3] == "rag_md_no_anchors"

    p, md = _rag_pair(tmp_path)
    ids, _, _, reason = es_index._validate_rag_chunks(tmp_path / "missing.rag_chunks.jsonl", md, "a.docx", {})
    assert reason in {"stat_failed", "read_failed"} and ids == []
    ids, _, _, reason = es_index._validate_rag_chunks(p, None, "a.docx", {})
    assert reason == "rag_md_missing" and ids == []


@pytest.mark.parametrize("attr,value,rows,chunks,reason", [
    ("_RAG_CHUNKS_MAX_ROWS", 2, [{"chunk_id": f"rc{i}", "source_rel_path": "a.docx"} for i in range(5)],
     [(f"rc{i}", "x") for i in range(5)], "too_many_rows"),
    ("_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS", 10, [_ROW_A], [("rc1", "x" * 100)], "search_text_too_long"),
    ("_RAG_CHUNKS_FILE_CAP_BYTES", 4, [_ROW1], [("rc1", "本文")], "rag_md_too_large"),
])
def test_validate_rag_chunks_limits(monkeypatch, tmp_path, attr, value, rows, chunks, reason):
    p, md = _rag_pair(tmp_path, rows=rows, chunks=chunks)
    monkeypatch.setattr(es_index, attr, value)
    assert es_index._validate_rag_chunks(p, md, "a.docx", {})[3] == reason


def test_validate_rag_chunks_jsonl_file_too_large(monkeypatch, tmp_path):
    p, md = _rag_pair(tmp_path)
    cap = md.stat().st_size + 1            # rag.md はこの上限を超えず jsonl だけ超える
    assert p.stat().st_size > cap
    monkeypatch.setattr(es_index, "_RAG_CHUNKS_FILE_CAP_BYTES", cap)
    assert es_index._validate_rag_chunks(p, md, "a.docx", {})[3] == "file_too_large"


def test_validate_rag_chunks_does_not_slurp_jsonl_via_read_text(monkeypatch, tmp_path):
    p, md = _rag_pair(tmp_path)
    original_read_text = Path.read_text

    def _guarded(self, *a, **kw):
        if self == p:
            raise AssertionError("jsonl 側で read_text は呼ばない（逐次 open で読む）")
        return original_read_text(self, *a, **kw)

    monkeypatch.setattr(Path, "read_text", _guarded)
    assert es_index._validate_rag_chunks(p, md, "a.docx", {})[3] is None


# ---- index_world: rag_chunks 優先・縮退・報告 ----

def _rag_world(monkeypatch, tmp_path, docs, *, read=lambda w, d: "legacy fallback body\n"):
    derived = tmp_path / "derived" / "md"
    derived.mkdir(parents=True)
    bulk = _index_env(monkeypatch, docs, read=read)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: derived)
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: derived)
    return derived, bulk


def test_index_world_rag_chunks_preferred_legacy_fallback_source_unchanged(monkeypatch, tmp_path):
    docs = [_doc("a.docx", md_path=str(tmp_path / "a.docx.md")), _doc("b.xlsx", md_path=str(tmp_path / "b.xlsx.md")),
            _doc("c.cbl")]
    texts = {"a.docx": "legacy body（使われないはず）", "b.xlsx": "legacy only body\n", "c.cbl": "ソース本文1\nソース本文2"}
    derived, bulk = _rag_world(monkeypatch, tmp_path, docs, read=lambda w, d: texts[d["name"]])
    (derived / "a.docx.rag_chunks.jsonl").write_text(json.dumps({
        "chunk_id": "rc1", "source_rel_path": "a.docx",
        "citations": [{"locator": {"sheet": "Sheet1", "cell_range": "B12"}}]}) + "\n", encoding="utf-8")
    _write_rag_md(derived / "a.docx.rag.md", ("rc1", "Excel の B12 セルは 1000 円"))
    # ソース文書に同名 sidecar があっても拡張子ゲートで読まない
    (derived / "c.cbl.rag_chunks.jsonl").write_text(json.dumps({
        "chunk_id": "should-not-be-used", "source_rel_path": "c.cbl"}) + "\n", encoding="utf-8")
    _write_rag_md(derived / "c.cbl.rag.md", ("should-not-be-used", "混入注意"))
    calls = []

    def fake_world_documents(w, include_rag=False):
        calls.append(include_rag)
        return docs

    monkeypatch.setattr(es_index.corpus_docs, "world_documents", fake_world_documents)
    r = es_index.index_world("w")
    assert r["indexed"] == 3
    assert r["rag_degraded"] == 0 and "rag_degraded_docs" not in r
    assert calls == [True]
    a_chunks, b_chunks, c_chunks = bulk.of("a.docx"), bulk.of("b.xlsx"), bulk.of("c.cbl")
    assert len(a_chunks) == 1 and a_chunks[0]["chunk_id"] == "rc1"
    assert a_chunks[0]["text"] == "Excel の B12 セルは 1000 円"
    assert a_chunks[0]["locator"] == {"sheet": "Sheet1", "cell_range": "B12"} and "line" not in a_chunks[0]
    assert len(b_chunks) == 1 and "chunk_id" not in b_chunks[0] and b_chunks[0]["line"] == 1
    assert "legacy only body" in b_chunks[0]["text"]
    assert c_chunks and all("chunk_id" not in c for c in c_chunks)
    assert not any("混入注意" in b.get("text", "") for b in bulk.bodies)


@pytest.mark.parametrize("jsonl,md_chunks,md_raw,max_rows,reason", [
    ("not json\n", [("rc1", "本文")], None, None, "invalid_json"),
    (json.dumps({"chunk_id": "rc1", "source_rel_path": "a.docx"}) + "\n", None, None, None, "rag_md_missing"),
    (json.dumps({"chunk_id": "rc1", "search_text": "旧形式の本文", "source_rel_path": "a.docx"}) + "\n", None,
     "# AI検索用文書\n\n旧形式のrag.md本文（アンカー無し）\n", None, "rag_md_no_anchors"),
    ("".join(json.dumps({"chunk_id": f"rc{i}", "source_rel_path": "a.docx"}) + "\n" for i in range(5)),
     [(f"rc{i}", f"本文{i}") for i in range(5)], None, 2, "too_many_rows"),
], ids=["invalid_json", "rag_md_missing", "legacy_no_anchors", "too_many_rows"])
def test_index_world_reports_rag_degraded_and_falls_back(monkeypatch, tmp_path, jsonl, md_chunks, md_raw, max_rows, reason):
    docs = [_doc("a.docx", md_path=str(tmp_path / "a.docx.md"))]
    derived, bulk = _rag_world(monkeypatch, tmp_path, docs)
    (derived / "a.docx.rag_chunks.jsonl").write_text(jsonl, encoding="utf-8")
    if md_chunks is not None:
        _write_rag_md(derived / "a.docx.rag.md", *md_chunks)
    if md_raw is not None:
        (derived / "a.docx.rag.md").write_text(md_raw, encoding="utf-8")
    if max_rows is not None:
        monkeypatch.setattr(es_index, "_RAG_CHUNKS_MAX_ROWS", max_rows)
    r = es_index.index_world("w")
    assert r["indexed"] == 1                                       # 文書自体は legacy へ縮退して残る
    assert r["rag_degraded"] == 1
    assert r["rag_degraded_docs"] == [{"doc": "a.docx", "reason": reason}]
    assert bulk.bodies[0]["text"] == "legacy fallback body"
    assert "chunk_id" not in bulk.bodies[0] and bulk.bodies[0]["line"] == 1


# ---- import-time 定数（実プロセスを新規に起こして検証） ----

def _import_all_constants_script() -> str:
    return (
        "import json\n"
        "import sherpa.es_index as m\n"
        "captured = {}\n"
        "def fake_req(method, path, body=None, **kw):\n"
        "    captured['body'] = body\n"
        "    return {'hits': {'hits': []}}\n"
        "m._req = fake_req\n"
        "m.available = lambda: True\n"
        "m.embeddings.cfg = lambda settings=None, **kw: None\n"
        "out = {\n"
        "    '_CHUNK_LINES': m._CHUNK_LINES,\n"
        "    '_HYBRID_WEIGHT': m._HYBRID_WEIGHT,\n"
        "    '_ES_SEARCH_K_MAX': m._ES_SEARCH_K_MAX,\n"
        "    '_RAG_CHUNKS_MAX_ROWS': m._RAG_CHUNKS_MAX_ROWS,\n"
        "    '_RAG_CHUNKS_FILE_CAP_BYTES': m._RAG_CHUNKS_FILE_CAP_BYTES,\n"
        "    '_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS': m._RAG_CHUNK_SEARCH_TEXT_MAX_CHARS,\n"
        "    '_ES_BULK_BATCH_MAX_DOCS': m._ES_BULK_BATCH_MAX_DOCS,\n"
        "    '_ES_BULK_BATCH_MAX_BYTES': m._ES_BULK_BATCH_MAX_BYTES,\n"
        "}\n"
        "m.search('w', 'query', k=999)\n"
        "out['size_default_k'] = captured['body']['size']\n"
        "for _req_k, _ceil in ((45, 1000), (67, 1000), (90, 1000), (2000, 1000)):\n"
        "    m.search('w', 'query', k=_req_k, k_ceiling=_ceil)\n"
        "    out[f'size_k{_req_k}_ceiling{_ceil}'] = captured['body']['size']\n"
        "print(json.dumps(out))\n"
    )


def test_env_constants_fresh_import_all_unset_is_default():
    out = json.loads(FI.run_script(_import_all_constants_script(), env={}).splitlines()[-1])
    assert out["_CHUNK_LINES"] == 40
    assert out["_HYBRID_WEIGHT"] == 0.5
    assert out["_ES_SEARCH_K_MAX"] == 50
    assert out["_RAG_CHUNKS_MAX_ROWS"] == 200000
    assert out["_RAG_CHUNKS_FILE_CAP_BYTES"] == 32 * 1024 * 1024
    assert out["_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS"] == 20000
    assert out["_ES_BULK_BATCH_MAX_DOCS"] == 2000
    assert out["_ES_BULK_BATCH_MAX_BYTES"] == 8 * 1024 * 1024
    assert out["size_default_k"] == 50                       # k=999 は既定の床へクランプ
    assert out["size_k45_ceiling1000"] == 45                  # k_ceiling があれば床を迂回する
    assert out["size_k67_ceiling1000"] == 67
    assert out["size_k90_ceiling1000"] == 90
    assert out["size_k2000_ceiling1000"] == 1000             # k_ceiling 自体でクランプ


# ---- bulk バッチ化 ----

def _pairs(batches) -> int:
    return [len(b.strip().split("\n")) // 2 for b in batches]


def test_bulk_batches_splits_by_doc_count(monkeypatch):
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 2)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", 10 * 1024 * 1024)
    ids = [f"id{i}" for i in range(5)]
    batches = es_index._bulk_batches(ids, [{"doc_id": "d", "text": f"t{i}"} for i in range(5)], {})
    assert _pairs(batches) == [2, 2, 1]
    seen_ids = []
    for b in batches:
        lines = b.strip().split("\n")
        seen_ids.extend(json.loads(lines[i])["index"]["_id"] for i in range(0, len(lines), 2))
    assert seen_ids == ids


def test_bulk_batches_splits_by_byte_size(monkeypatch):
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1000)
    ids = [f"id{i}" for i in range(4)]
    bodies = [{"doc_id": "d", "text": "x" * 80} for _ in range(4)]
    pair_bytes = (len(json.dumps({"index": {"_id": ids[0]}}).encode("utf-8"))
                  + len(json.dumps(bodies[0], ensure_ascii=False).encode("utf-8")) + 2)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", pair_bytes * 2)
    assert _pairs(es_index._bulk_batches(ids, bodies, {})) == [2, 2]


def test_bulk_batches_oversized_single_chunk_gets_own_batch(monkeypatch):
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1000)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", 50)
    batches = es_index._bulk_batches(["a", "b"], [{"doc_id": "d", "text": "x" * 500},
                                                   {"doc_id": "d", "text": "y" * 500}], {})
    assert _pairs(batches) == [1, 1]


def test_bulk_batches_applies_embedding_from_vec_by_idx(monkeypatch):
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1000)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", 10 * 1024 * 1024)
    batches = es_index._bulk_batches(["a", "b"], [{"doc_id": "d", "text": "t1"}, {"doc_id": "d", "text": "t2"}],
                                     {0: [0.1, 0.2]})
    docs = [json.loads(ln) for b in batches for i, ln in enumerate(b.strip().split("\n")) if i % 2 == 1]
    assert docs[0].get("embedding") == [0.1, 0.2] and "embedding" not in docs[1]


def _multi_chunk_world(monkeypatch, n: int):
    _index_env(monkeypatch, [_doc(f"d{i}.md") for i in range(n)], read=lambda w, d: "本文1行のみ")
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1)


def test_index_world_sends_multiple_bulk_batches_refresh_only_last(monkeypatch):
    _multi_chunk_world(monkeypatch, 3)
    calls = []

    def fake_req(method, path, body=None, **kw):
        if "_bulk" in str(path):
            calls.append((path, body))
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r["indexed"] == 3 and r["chunks"] == 3 and r.get("error") is None
    assert [p.endswith("?refresh=true") for p, _ in calls] == [False, False, True]
    assert all(len(body.strip().split("\n")) == 2 for _, body in calls)


@pytest.mark.parametrize("failure,error", [("raise", "bulk_failed"), ("item_errors", "bulk_errors")])
def test_index_world_partial_batch_failure_wipes_index(monkeypatch, failure, error):
    _multi_chunk_world(monkeypatch, 3)
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    call_n = {"n": 0}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            call_n["n"] += 1
            if call_n["n"] == 2:
                if failure == "raise":
                    raise RuntimeError("network failure mid-batch")
                return {"errors": True}
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r == {"available": True, "indexed": 0, "chunks": 0, "error": error, "rag_degraded": 0}
    assert call_n["n"] == 2                                       # 3 バッチ目は送らない
    assert delete_calls == ["w", "w"]                             # クリーン再索引の delete ＋ 失敗後の wipe


def test_index_world_empty_text_only_world_converges_without_no_chunks(monkeypatch, tmp_path):
    wd = tmp_path / "root"
    wd.mkdir()
    (wd / "a.md").write_text("   \n\t\n", encoding="utf-8")
    (wd / "b.md").write_text("", encoding="utf-8")
    monkeypatch.setattr(es_index.worlds, "world_dir", lambda w: wd)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: tmp_path / "derived" / "md")
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: tmp_path / "derived" / "rag")
    confirmed = []
    monkeypatch.setattr(es_index, "_confirm_content_sig", lambda w, sig: confirmed.append(sig))
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})
    for _ in range(2):
        r = es_index.index_world("w", content_sig="sig-x")
        assert r.get("error") is None
        assert r["indexed"] == 0 and r["chunks"] == 0
        assert r["rag_degraded"] == 2
        assert {d["reason"] for d in r["rag_degraded_docs"]} == {"empty_text"}
    assert confirmed == ["sig-x", "sig-x"]


def test_index_world_single_batch_default_thresholds_still_refreshes(monkeypatch):
    bulk = _index_env(monkeypatch, [_doc("a.md"), _doc("b.md")], read=lambda w, d: "本文")
    assert es_index.index_world("w").get("error") is None
    assert bulk.paths == [f"/{es_index._index('w')}/_bulk?refresh=true"]


# ---- 親返し ----

def test_rag_md_anchor_chunk_id_matches_and_rejects():
    assert es_index.rag_md_anchor_chunk_id("<!-- chunk:rag-chunk:abc123 -->") == "rag-chunk:abc123"
    assert es_index.rag_md_anchor_chunk_id("本文の行") is None
    assert es_index.rag_md_anchor_chunk_id("<!-- chunk:c1 --> 余分な文字") is None
    assert es_index.rag_md_anchor_chunk_id("") is None


def test_chunk_ids_for_parent(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        captured.update(method=method, path=path, body=body)
        return {"hits": {"hits": [{"_source": {"chunk_id": "c1"}}, {"_source": {"chunk_id": "c2"}},
                                  {"_source": {}}]}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    assert es_index.chunk_ids_for_parent("w", "a.docx", ["p1"]) == ["c1", "c2"]
    assert captured["method"] == "POST" and captured["path"].endswith("/_search")
    q = captured["body"]["query"]["bool"]["filter"]
    assert {"term": {"doc_id": "a.docx"}} in q and {"terms": {"parent_id": ["p1"]}} in q


def test_chunk_ids_for_parent_empty_when_unavailable_empty_ids_or_query_failure(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: False)
    calls = []
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: calls.append(1) or {})
    assert es_index.chunk_ids_for_parent("w", "a.docx", ["p1"]) == []
    monkeypatch.setattr(es_index, "available", lambda: True)
    assert es_index.chunk_ids_for_parent("w", "a.docx", []) == []
    assert es_index.chunk_ids_for_parent("w", "a.docx", [None, "", 123]) == []
    assert calls == []

    def boom(*a, **k):
        raise RuntimeError("es down")

    monkeypatch.setattr(es_index, "_req", boom)
    assert es_index.chunk_ids_for_parent("w", "a.docx", ["p1"]) == []


# ===== 埋め込みキャッシュ（SQLite）の剪定: 接続障害の fail-loud と容量回収 =====

def _seed_cache(monkeypatch, tmp_path, world: str, n: int) -> Path:
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    es_index._embed_cache_write_batch(world, {f"k{i:05d}": [0.5] * 1536 for i in range(n)})
    p = es_index._embed_cache_db_path(world)
    assert p.exists()
    return Path(p)


def test_prune_embed_cache_raises_when_existing_db_cannot_be_opened(monkeypatch, tmp_path):
    import sqlite3
    world = "prune-conn-fail"
    _seed_cache(monkeypatch, tmp_path, world, 3)
    real_connect = sqlite3.connect

    def broken_connect(*a, **k):
        raise sqlite3.OperationalError("unable to open database file")
    monkeypatch.setattr(sqlite3, "connect", broken_connect)
    with pytest.raises(OSError):
        es_index._prune_embed_cache(world, {"k00000"})
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    es_index._delete_embed_cache(world)                            # DB が無ければ何もしない
    es_index._prune_embed_cache(world, {"k00000"})


def test_prune_embed_cache_reclaims_disk_after_mass_delete(monkeypatch, tmp_path):
    p = _seed_cache(monkeypatch, tmp_path, "prune-vacuum", 1000)
    before = p.stat().st_size
    es_index._prune_embed_cache("prune-vacuum", {"k00000"})
    assert p.stat().st_size < before / 10
    assert es_index._embed_cache_lookup_batch("prune-vacuum", ["k00000", "k00001"], 1536).keys() == {"k00000"}


def test_prune_embed_cache_reclaims_free_pages_left_by_failed_vacuum(monkeypatch, tmp_path):
    import sqlite3
    world = "prune-vacuum-retry"
    p = _seed_cache(monkeypatch, tmp_path, world, 1000)
    before = p.stat().st_size
    real_connect = sqlite3.connect

    class _Conn:
        def __init__(self, inner):
            self._c = inner
        def execute(self, sql, *a):
            if sql.strip().upper() == "VACUUM":
                raise sqlite3.OperationalError("database or disk is full")
            return self._c.execute(sql, *a)
        def __getattr__(self, name):
            return getattr(self._c, name)
        def __enter__(self):
            return self._c.__enter__()
        def __exit__(self, *a):
            return self._c.__exit__(*a)
    monkeypatch.setattr(sqlite3, "connect", lambda *a, **k: _Conn(real_connect(*a, **k)))
    es_index._prune_embed_cache(world, {"k00000"})                 # VACUUM 失敗は警告のみ・sync を止めない
    monkeypatch.setattr(sqlite3, "connect", real_connect)
    assert es_index._embed_cache_lookup_batch(world, ["k00001"], 1536) == {}
    assert p.stat().st_size >= before * 0.9                        # 未回収のまま
    es_index._prune_embed_cache(world, {"k00000"})                 # 次回は削除 0 件でも回収する
    assert p.stat().st_size < before / 10


def test_embed_cache_write_recreates_corrupt_db_file(monkeypatch, tmp_path):
    world = "corrupt-db"
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    p = es_index._embed_cache_db_path(world)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(b"this is not a sqlite database, just junk bytes long enough to fail header check")
    assert es_index._embed_cache_lookup_batch(world, ["k1"], 4) == {}      # 読取側は miss 扱いで壊れた本体を消さない
    assert p.read_bytes().startswith(b"this is not")
    es_index._embed_cache_write_batch(world, {"k1": [0.1, 0.2, 0.3, 0.4]})
    assert es_index._embed_cache_lookup_batch(world, ["k1"], 4) == {"k1": [0.1, 0.2, 0.3, 0.4]}


def test_embed_cache_write_recreates_page_corrupt_db(monkeypatch, tmp_path):
    world = "corrupt-pages"
    p = _seed_cache(monkeypatch, tmp_path, world, 2000)
    raw = bytearray(p.read_bytes())
    for i in range(4096, len(raw)):
        raw[i] = 0xFF
    p.write_bytes(bytes(raw))
    for f in (p.with_name(p.name + "-wal"), p.with_name(p.name + "-shm")):
        f.unlink(missing_ok=True)
    es_index._embed_cache_write_batch(world, {"z1": [0.1] * 1536})
    assert es_index._embed_cache_lookup_batch(world, ["z1"], 1536) == {"z1": [0.1] * 1536}


def test_prune_embed_cache_raises_when_valid_keys_are_missing_from_db(monkeypatch, tmp_path):
    world = "prune-missing-keys"
    _seed_cache(monkeypatch, tmp_path, world, 5)
    with pytest.raises(OSError):
        es_index._prune_embed_cache(world, {"k00000", "k00001", "not-in-db"})
    es_index._prune_embed_cache(world, {"k00000", "k00001"})
    assert es_index._embed_cache_lookup_batch(world, ["k00002"], 1536) == {}
