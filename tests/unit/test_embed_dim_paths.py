"""Ollama 埋め込みの次元の実測を「取り込み」と「検索」で使い分ける契約の単体テスト。

- 検索の経路（`probe=False`）はネットワークへ出ず、保存済みの実測値か索引のメタの次元だけを使う。
- `cfg()` は接続先が Ollama に決まれば、次元を測れなくても `dim=None` で返す（None は埋め込みを使わない設定だけ）。
- 取り込みで次元を測れなかったとき、ベクトルのある既存索引・埋め込みキャッシュを BM25 のために捨てない。
- 次元が未実測でも、索引の鮮度の比較は「違う」と言わない。
モックは外部境界（`llm.post_json`・ES の `_req`）だけに置く。
"""
from __future__ import annotations

import logging
import urllib.error

import pytest

from sherpa import embeddings, es_index, llm
from tests._replace_row import offline_row

_CUSTOM = {"model_catalog": {"ollama": {"embed": {"allowed": ["custom-embed"], "default": "custom-embed"}}}}


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    for k in ("OPENAI_API_KEY", "OLLAMA_URL"):
        monkeypatch.delenv(k, raising=False)
    embeddings._ollama_dim_cache.clear()
    embeddings._ollama_dim_failed.clear()
    embeddings._ollama_legacy_urls.clear()
    embeddings._float_only_urls.clear()
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: dict(_CUSTOM))


def _no_network(monkeypatch):
    calls = []

    def boom(*a, **k):
        calls.append(1)
        raise OSError("unreachable")

    monkeypatch.setattr(llm, "post_json", boom)
    return calls


def test_cfg_without_probe_uses_index_dim_and_never_touches_network(monkeypatch):
    calls = _no_network(monkeypatch)
    ec = embeddings.cfg(None, system_settings=_CUSTOM, probe=False, index_dim=384)
    assert ec["provider"] == "ollama" and ec["dim"] == 384
    ec = embeddings.cfg(None, system_settings=_CUSTOM, probe=False)  # 索引の次元も保存済みの実測も無い
    assert ec["provider"] == "ollama" and ec["model"] == "custom-embed" and ec["dim"] is None
    assert calls == []


def test_cfg_returns_target_with_unknown_dim_when_probe_fails(monkeypatch):
    _no_network(monkeypatch)
    ec = embeddings.cfg(None, system_settings=_CUSTOM)
    assert (ec["provider"], ec["url"], ec["model"], ec["dim"]) == ("ollama", "http://localhost:11434", "custom-embed", None)
    assert embeddings.embed(["x"], ec) is None  # 次元の無い設定では送らない


def _v1_only_server(monkeypatch, make_embedding):
    sent = []

    def fake_post(url, headers, body, timeout=90):
        sent.append(url)
        if url.endswith("/api/embed"):
            raise _http_error(404)
        return {"data": [{"index": 0, "embedding": make_embedding()}], "usage": {"prompt_tokens": 1}}

    monkeypatch.setattr(llm, "post_json", fake_post)
    return sent


@pytest.mark.parametrize("encode", [False, True])
def test_dim_probe_follows_the_normal_send_path_when_only_v1_embeddings_exists(monkeypatch, encode):
    import base64
    from array import array
    values = [0.5, -0.25, 1.0, 2.0, 0.125]
    make = (lambda: base64.b64encode(embeddings.vector_to_bytes(array("f", values))).decode()) if encode else (lambda: values)
    sent = _v1_only_server(monkeypatch, make)
    ec = embeddings.cfg(None, system_settings=_CUSTOM)
    assert ec["dim"] == 5
    assert not any(u.endswith("/api/embed") for u in sent)


def test_cfg_without_probe_prefers_saved_measurement(monkeypatch):
    calls = _no_network(monkeypatch)
    embeddings._ollama_dim_cache[("http://localhost:11434", "custom-embed")] = 512
    assert embeddings.cfg(None, system_settings=_CUSTOM, probe=False, index_dim=384)["dim"] == 512
    assert calls == []


def _search_env(monkeypatch, *, dim):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.path_rules, "rules_for", lambda w: es_index.path_rules.EMPTY)
    meta = {"embed_provider": "ollama", "embed_model": "custom-embed", "dim": dim,
            "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}

    def fake_req(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/_mapping"):
            return {"idx": {"mappings": {"_meta": meta}}}
        return {"hits": {"hits": [{"_id": "a", "_source": {"doc_id": "a.md", "line": 1, "text": "hit"}, "_score": 1.0}]}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    monkeypatch.setattr(es_index.embeddings, "embed", lambda texts, ec, **kw: [[0.1] * ec["dim"]])
    monkeypatch.setattr(es_index.embeddings, "vector_to_es_bytes", lambda v: b"x")


@pytest.mark.parametrize("call", [
    lambda: es_index.search("w", "query"),
    lambda: es_index.search_knn_only("w", "query"),
], ids=["search", "search_knn_only"])
def test_search_does_not_probe_ollama_dimension(monkeypatch, call):
    calls = _no_network(monkeypatch)
    _search_env(monkeypatch, dim=384)
    hits, reason = call()
    assert calls == []
    assert reason is None and hits  # 索引のメタの次元で kNN まで進む


# ---- 取り込み: 測れないだけで既存のベクトルを捨てない ----

def _ingest_env(monkeypatch, *, mapping):
    """`index_world` を ES 無しで走らせる。`cfg()` は実物（次元の実測は失敗する）。`mapping` は索引メタの GET への応答（例外も可）。"""
    from test_es_index_meta import _doc, _index_env

    real_cfg = embeddings.cfg
    bulk = _index_env(monkeypatch, [_doc("a.md", branch="office")], read=lambda w, d: "本文")
    monkeypatch.setattr(es_index.embeddings, "cfg", real_cfg)
    deleted = []
    monkeypatch.setattr(es_index, "_delete_embed_cache", lambda w: deleted.append(w))

    def req(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/_mapping"):
            if isinstance(mapping, Exception):
                raise mapping
            return mapping
        return bulk.req(method, path, body, **kw)

    monkeypatch.setattr(es_index, "_req", req)
    return bulk, deleted


def _http_error(code):
    return urllib.error.HTTPError("http://es/idx/_mapping", code, "x", {}, None)


def test_index_world_keeps_existing_vectors_when_dimension_probe_fails(monkeypatch, caplog):
    _no_network(monkeypatch)
    meta = {"embed_provider": "ollama", "embed_model": "custom-embed", "dim": 384}
    bulk, deleted = _ingest_env(monkeypatch, mapping={"idx": {"mappings": {"_meta": meta}}})
    with caplog.at_level(logging.INFO, logger="sherpa.embed"):
        r = es_index.index_world("w", row=offline_row("w"))
    assert r["error"] == "embedding_cloud_unavailable"
    assert deleted == [] and bulk.payloads == []
    line = next(m for m in caplog.messages if "embed 接続先" in m)
    assert "provider=ollama" in line and "host=localhost:11434" in line and "model=custom-embed" in line and "dim=None" in line


@pytest.mark.parametrize("mapping", [
    _http_error(404),
    {"idx": {"mappings": {"_meta": {"world_id": "w"}}}},
], ids=["index_missing", "index_without_vectors"])
def test_index_world_still_indexes_bm25_when_no_vectors_to_lose(monkeypatch, caplog, mapping):
    _no_network(monkeypatch)
    bulk, deleted = _ingest_env(monkeypatch, mapping=mapping)
    with caplog.at_level(logging.INFO, logger="sherpa.embed"):
        assert es_index.index_world("w", row=offline_row("w")).get("error") is None
    assert len(bulk.bodies) == 1 and deleted == []  # 接続先は決まっているので埋め込みキャッシュは消さない
    lines = [m for m in caplog.messages if "embed 接続先" in m]
    assert len(lines) == 1 and "host=localhost:11434" in lines[0] and "model=custom-embed" in lines[0] and "dim=None" in lines[0]


def test_index_world_aborts_when_existing_index_unreadable(monkeypatch):
    _no_network(monkeypatch)
    bulk, deleted = _ingest_env(monkeypatch, mapping=_http_error(503))
    assert es_index.index_world("w", row=offline_row("w"))["error"] == "embedding_cloud_unavailable"
    assert deleted == [] and bulk.payloads == []


# ---- 中央の Ollama の接続先を設定した自動の経路 ----

_CENTRAL = {**_CUSTOM, "ollama_url": "http://localhost:11434"}


def test_index_world_indexes_bm25_on_new_world_even_with_central_ollama_url(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: dict(_CENTRAL))
    bulk, deleted = _ingest_env(monkeypatch, mapping=_http_error(404))
    assert es_index.index_world("w", row=offline_row("w")).get("error") is None
    assert len(bulk.bodies) == 1 and deleted == []


def test_index_world_keeps_existing_vectors_for_known_model_when_probe_fails(monkeypatch):
    _no_network(monkeypatch)
    known = {"model_catalog": {"ollama": {"embed": {"allowed": ["bge-m3"], "default": "bge-m3"}}}}
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: dict(known))
    meta = {"embed_provider": "ollama", "embed_model": "bge-m3", "dim": 1024}
    bulk, deleted = _ingest_env(monkeypatch, mapping={"idx": {"mappings": {"_meta": meta}}})
    assert es_index.index_world("w", row=offline_row("w"))["error"] == "embedding_cloud_unavailable"
    assert deleted == [] and bulk.payloads == []


def _search_meta_env(monkeypatch, mapping):
    _no_network(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: dict(_CENTRAL))
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.path_rules, "rules_for", lambda w: es_index.path_rules.EMPTY)

    def fake_req(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/_mapping"):
            if isinstance(mapping, Exception):
                raise mapping
            return mapping
        return {"hits": {"hits": [{"_id": "a", "_source": {"doc_id": "a.md", "line": 1, "text": "hit"}, "_score": 1.0}]}}

    monkeypatch.setattr(es_index, "_req", fake_req)


_SEARCH_CALLS = [lambda: es_index.search("w", "query"), lambda: es_index.search_knn_only("w", "query")]


@pytest.mark.parametrize("call", _SEARCH_CALLS, ids=["search", "search_knn_only"])
@pytest.mark.parametrize("failure", [_http_error(503), OSError("down")], ids=["http_503", "unreachable"])
def test_search_reports_index_failure_not_embedding_when_meta_unreadable(monkeypatch, call, failure):
    _search_meta_env(monkeypatch, failure)
    assert call() == ([], "es_query_failed")


@pytest.mark.parametrize("call", _SEARCH_CALLS, ids=["search", "search_knn_only"])
@pytest.mark.parametrize("mapping", [_http_error(404), {"idx": {"mappings": {"_meta": {"world_id": "w"}}}}],
                         ids=["index_missing", "meta_without_dim"])
def test_search_keeps_embedding_reason_when_index_missing_or_has_no_dim(monkeypatch, call, mapping):
    _search_meta_env(monkeypatch, mapping)
    assert call()[1] == "embedding_cloud_unavailable"


def test_search_reads_index_meta_once(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setattr("sherpa.store.get_system_settings", lambda **kw: dict(_CENTRAL))
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.path_rules, "rules_for", lambda w: es_index.path_rules.EMPTY)
    gets = []

    def fake_req(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/_mapping"):
            gets.append(path)
            raise _http_error(503)
        return {"hits": {"hits": []}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    assert es_index.search("w", "query") == ([], "es_query_failed")
    assert len(gets) == 1


# ---- 索引の鮮度: 次元が測れないだけでは「違う」と言わない ----

def _meta(provider="ollama", model="custom-embed", dim=384):
    return {"embed_provider": provider, "embed_model": model, "dim": dim,
            "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}


@pytest.mark.parametrize("meta, expected", [
    (_meta(), True),                                  # 同じモデルのベクトルのある索引
    ({"world_id": "w"}, True),                        # ベクトルの無い索引（次元を測れない間は BM25 のまま）
    (_meta(model="other-embed"), False),              # モデルが違うことは次元が無くても分かる
    (_meta(provider="openai"), False),
], ids=["same_model", "bm25_only_index", "other_model", "other_provider"])
def test_unknown_dim_does_not_claim_index_is_stale(meta, expected):
    from sherpa import es_publish
    ec = {"provider": "ollama", "url": "http://localhost:11434", "model": "custom-embed", "dim": None}
    assert es_publish.embed_feature_matches(ec, meta) is expected


def test_known_dim_difference_still_means_stale():
    from sherpa import es_publish
    ec = {"provider": "ollama", "url": "http://localhost:11434", "model": "custom-embed", "dim": 512}
    assert es_publish.embed_feature_matches(ec, _meta(dim=384)) is False
    assert es_publish.embed_feature_matches(ec, _meta(dim=512)) is True
    assert es_publish.embed_feature_matches(None, _meta()) is False


def test_needs_reindex_is_not_prompted_by_unmeasurable_dim(monkeypatch):
    _no_network(monkeypatch)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 3)
    monkeypatch.setattr(es_index, "_index_kind", lambda w: "alias")
    full = {**_meta(), "content_sig": "s", "source_excluded": True, "mapping_version": es_index.ES_MAPPING_VERSION,
            "arms_sig": es_index._arms_config_sig(), "human_md_sig": es_index._human_md_config_sig("w"),
            "analyzer_config_sig": es_index._analyzer_config_sig(), "doc_count": 3}
    monkeypatch.setattr(es_index, "_index_meta", lambda w, idx=None: dict(full))
    assert es_index.needs_reindex("w", "s", {}) is False


def test_replace_document_stops_when_vector_index_and_dim_unmeasurable(monkeypatch, tmp_path):
    _no_network(monkeypatch)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 3)
    monkeypatch.setattr(es_index, "_index_meta", lambda w, idx=None: {**_meta(), "content_sig": "s"})
    row = offline_row("w")
    r = es_index.replace_document("w", "a.md", tmp_path / "a.md", content_sig="s", row=row)
    assert r == {"available": True, "error": "embedding_dim_unknown"}
    assert row._offline == []   # 何も書かないうちは行に触れない


def test_resolve_ollama_usages_counts_embed_even_when_dim_is_unmeasurable(monkeypatch):
    """次元を測れない Ollama の埋め込みも「使っている」として点検の対象に入れる（モデルの有無の判定が動く）。"""
    import scripts.doctor_checks as doctor_checks
    from test_doctor_checks import _agent, _ollama
    _ollama(monkeypatch, embed={"provider": "ollama", "url": "http://localhost:11434", "model": "nomic-embed-text", "dim": None},
            simple=("openai", "m"), catalog=True)
    _agent(monkeypatch, "openai")
    usages = doctor_checks._resolve_ollama_usages({}, [])
    assert {"url": "http://localhost:11434", "model": "nomic-embed-text", "purposes": ["埋め込み（ベクトル検索）"]} in usages


def test_update_target_keeps_bm25_index_updatable_when_dim_is_unmeasurable():
    from sherpa import es_publish
    ec = {"provider": "ollama", "url": "http://localhost:11434", "model": "custom-embed", "dim": None}
    assert es_publish.update_embed_target(ec, {"world_id": "w"}) == (None, None)
    assert es_publish.update_embed_target(ec, _meta()) == (ec, "embedding_dim_unknown")


@pytest.mark.parametrize("body", [{"embeddings": [1]}, {"embeddings": [[0.1, 0.2], [0.3, 0.4]]}, {"embeddings": [[10**1000, 0.1]]}], ids=["not_a_vector", "wrong_count", "huge_int"])
def test_probe_of_a_malformed_response_is_unmeasured_not_an_exception(monkeypatch, body):
    monkeypatch.setattr(embeddings, "_ollama_dim_cache", {})
    monkeypatch.setattr(embeddings, "_ollama_dim_failed", {})
    monkeypatch.setattr(embeddings, "_ollama_legacy_urls", set())

    def _post(url, *a, **k):
        if url.endswith("/v1/embeddings"):
            raise urllib.error.HTTPError(url, 404, "not found", {}, None)
        return body

    monkeypatch.setattr(llm, "post_json", _post)
    assert embeddings.ollama_embed_dim("http://localhost:11434", "custom-embed") is None
