"""ベクトルの容量削減（`_source` からベクトルを外す・埋め込みの次元を設定で選ぶ）の単体テスト（実 ES 不要）。
外部境界 `_req`／`_send_bulk`／`embeddings.embed` だけ差し替える。
"""
from __future__ import annotations

import json

import pytest

from sherpa import embeddings, es_index


def test_mapping_excludes_embedding_from_source_only_when_vectors_enabled():
    assert es_index._mapping(512, "standard")["mappings"]["_source"] == {"excludes": ["embedding"]}
    assert "_source" not in es_index._mapping(None, "standard")["mappings"]


def test_mapping_flushes_and_merges_often_only_when_vectors_enabled():
    with_vec = es_index._mapping(512, "standard")["settings"]["index"]
    assert with_vec["translog.flush_threshold_size"] == "256mb"
    assert with_vec["merge.policy.segments_per_tier"] == with_vec["merge.policy.max_merge_at_once"] == 4
    assert es_index._mapping(None, "standard")["settings"]["index"] == {"refresh_interval": "-1"}


@pytest.mark.parametrize("raw,model,expected", [
    (None, None, 1536), (None, "text-embedding-3-large", 1536), (None, "my-azure-deploy", 1536),
    (1024, None, 1024), (64, None, 64), (63, None, 1536), (4097, None, 1536),
    (2048, None, 1536), (2048, "text-embedding-3-large", 2048), (4096, "text-embedding-3-large", 3072),
    (True, None, 1536), ("512", None, 1536), (1.5, None, 1536),
])
def test_effective_embed_dim(raw, model, expected):
    settings = {} if raw is None else {"embed_dim": raw}
    assert embeddings.effective_embed_dim(settings, model) == expected


def test_embed_dim_changes_cfg_dim_cache_key_and_forces_reindex(monkeypatch):
    from sherpa import store
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    monkeypatch.setattr(store, "get_system_settings", lambda: {"personal_api_keys_allowed": True, "embed_dim": 512})
    ec512 = embeddings.cfg({"extract_provider": "openai", "openai_api_key": "sk-test"})
    monkeypatch.setattr(store, "get_system_settings", lambda: {"personal_api_keys_allowed": True})
    ec1536 = embeddings.cfg({"extract_provider": "openai", "openai_api_key": "sk-test"})
    assert (ec512["dim"], ec1536["dim"]) == (512, 1536)
    assert es_index._chunk_key(ec512, "本文") != es_index._chunk_key(ec1536, "本文")  # 違う次元のキャッシュは使い回さない

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_index_kind", lambda w: "alias")
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 40)
    meta = {"content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION, "arms_sig": "sig-A",
            "analyzer_config_sig": "acfg-A", "source_excluded": True, "embed_provider": "openai",
            "embed_model": ec1536["model"], "dim": 1536, "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}
    monkeypatch.setattr(es_index, "_index_meta", lambda w: meta)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec1536)
    assert es_index.needs_reindex("w", "c1") is False
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec512)
    assert es_index.needs_reindex("w", "c1") is True


def test_replace_document_rollback_restores_vectors_from_cache_without_api(monkeypatch, tmp_path):
    """`_source` にベクトルが無くても、巻き戻しは埋め込みキャッシュからベクトルを戻す（API は呼ばない）。キャッシュに無い分は missing で入れる。"""
    src = tmp_path / "a.md"
    src.write_text("新しい\n", encoding="utf-8")
    ec = {"provider": "openai", "model": "m", "dim": 3}
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    es_index._embed_cache_write_batch("w", {es_index._chunk_key(ec, "古い1"): [0.1, 0.2, 0.3]})  # 「古い2」はキャッシュに無い
    meta = {"content_sig": "c1", "embed_provider": "openai", "embed_model": "m", "dim": 3,
            "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: meta)
    monkeypatch.setattr(es_index, "count", lambda w: 2)
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: {})
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec)
    monkeypatch.setattr(es_index.corpus_docs, "iter_world_documents", lambda w, **kw: [
        {"name": "a.md", "md_path": None, "top_scope": "t", "branch": "office"}])
    monkeypatch.setattr(es_index.importance, "resolve_for_world", lambda *a, **k: {})
    monkeypatch.setattr(es_index.worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: tmp_path / "rag")
    monkeypatch.setattr(es_index, "_iter_doc_chunk_records", lambda *a, **k: (
        iter([("n1", {"doc_id": "a.md", "text": "新しい"}, "新しい", False)]), None))
    monkeypatch.setattr(es_index, "_embed_cached", lambda world, texts, ec_: ([[1.0, 0.0, 0.0]] * len(texts), 0, len(texts)))
    old = [{"_id": "o1", "_source": {"doc_id": "a.md", "text": "古い1"}},
           {"_id": "o2", "_source": {"doc_id": "a.md", "text": "古い2"}}]
    monkeypatch.setattr(es_index, "_scroll_hits", lambda w, q, s: old if s is True else [{"_id": "o1"}, {"_id": "o2"}])
    sent: list = []

    def send(world, payloads):
        sent.append(payloads)
        return "bulk_failed" if len(sent) == 1 else None

    def no_api(*a, **k):
        raise AssertionError("巻き戻しで埋め込み API を呼んではいけない")

    monkeypatch.setattr(es_index, "_send_bulk", send)
    monkeypatch.setattr(es_index.embeddings, "embed", no_api)
    r = es_index.replace_document("w", "a.md", src, content_sig="c1")
    assert r == {"available": True, "error": "bulk_failed"}
    restore = [json.loads(ln) for p in sent[1] for i, ln in enumerate(p.strip().split("\n")) if i % 2 == 1]
    by_text = {b["text"]: b for b in restore if b["text"].startswith("古い")}
    assert by_text["古い1"]["embedding"] == [42, 85, 127] and "vector_status" not in by_text["古い1"]
    assert "embedding" not in by_text["古い2"] and by_text["古い2"]["vector_status"] == "missing"


def test_validate_embed_dim_accepts_only_range():
    from fastapi import HTTPException
    from sherpa.routers import system_extras as se
    assert se._validate_embed_dim(None) is None
    assert se._validate_embed_dim(64) == 64 and se._validate_embed_dim(4096) == 4096
    for bad in (0, 63, 4097):
        with pytest.raises(HTTPException) as ei:
            se._validate_embed_dim(bad)
        assert ei.value.status_code == 422
