"""ES へ渡すベクトルを 1 次元 1 バイトに縮める（`embeddings.vector_to_es_bytes`・`_meta.quant`）の単体テスト。
実 ES 不要（`_req`／`available`／`embeddings` を差し替える）。
"""
from __future__ import annotations

import json
import math
from array import array

import pytest

from sherpa import embeddings, es_index


def test_vector_to_es_bytes_is_deterministic_and_bounded():
    v = array("f", [0.013 * ((i * 37) % 101 - 50) for i in range(1536)])
    q = embeddings.vector_to_es_bytes(v)
    assert q == embeddings.vector_to_es_bytes(v) == embeddings.vector_to_es_bytes(v.tolist())
    assert len(q) == 1536 and all(isinstance(x, int) and -127 <= x <= 127 for x in q)
    assert max(abs(x) for x in q) == 127
    assert embeddings.vector_to_es_bytes([-x for x in v]) == [-x for x in q]   # 符号で対称
    assert embeddings.vector_to_es_bytes([x * 1000 for x in v]) == q            # 大きさに依らない
    fq = math.fsum(a * b for a, b in zip(v, q)) / (math.sqrt(math.fsum(a * a for a in v))
                                                    * math.sqrt(math.fsum(b * b for b in q)))
    assert fq > 0.999


@pytest.mark.parametrize("bad", [[0.0, 0.0, 0.0], [], [1.0, float("nan")], [float("inf"), 1.0],
                                 [float("-inf"), 1.0], ["a", 1.0], None])
def test_vector_to_es_bytes_rejects_zero_and_non_finite(bad):
    assert embeddings.vector_to_es_bytes(bad) is None


def test_mapping_is_byte_cosine_and_meta_records_quant():
    m = es_index._mapping(1536, "standard", {"quant": embeddings.ES_VECTOR_QUANT})
    assert m["mappings"]["properties"]["embedding"] == {
        "type": "dense_vector", "dims": 1536, "element_type": "byte", "index": True, "similarity": "cosine"}
    assert m["mappings"]["_source"] == {"excludes": ["embedding"]}
    assert m["mappings"]["_meta"]["quant"] == "byte_maxabs_v1"


def test_index_world_records_quant_and_rebuild_sends_the_same_bytes(monkeypatch, tmp_path):
    """1 回目（API の float を縮める）と作り直し（キャッシュの 1 バイトの値）で、ES へ送る中身がまったく同じ。"""
    captured: dict = {}
    sent: list = []
    embed_calls: list = []
    ec = {"provider": "p", "model": "m", "dim": 4}
    monkeypatch.setattr(es_index.corpus_docs, "world_documents",
                        lambda w, **kw: [{"name": "a.md", "rel": "a.md", "ext": ".md", "branch": "office"}])
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_drop_stale_physicals", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: captured.update(emeta or {}) or True)
    monkeypatch.setattr(es_index.worlds, "derived_dir", lambda w: tmp_path / w)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    monkeypatch.setattr(es_index.embeddings, "embed",
                        lambda texts, c, **kw: embed_calls.append(texts) or [[0.1, -0.2, 0.3, 0.0123456] for _ in texts])

    def fake_req(method, path, body=None, **kw):
        if path.endswith("_bulk") or "_bulk?" in path:
            lines = body.strip().split("\n")
            sent.extend(json.loads(ln) for i, ln in enumerate(lines) if i % 2 == 1)
            return {"errors": False, "items": []}
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    es_index.index_world("w")
    first = list(sent)
    assert captured["quant"] == embeddings.ES_VECTOR_QUANT
    assert first and all(b["embedding"] == [42, -85, 127, 5] for b in first) and len(embed_calls) == 1
    key = es_index._chunk_key(ec, "本文")
    assert es_index._embed_cache_db_path("w").name == "embed_cache.i8.sqlite3"
    assert es_index._embed_cache_lookup_batch("w", [key], 4)[key] == array("b", [42, -85, 127, 5])
    sent.clear()
    es_index.index_world("w")
    assert sent == first and len(embed_calls) == 1   # 作り直しは API を呼ばずキャッシュの値をそのまま送る


def test_vector_update_batches_sends_bytes_and_skips_unusable():
    payloads = es_index._vector_update_batches(["a", "b"], [array("f", [0.5, 1.0]), array("f", [0.0, 0.0])])
    lines = "".join(payloads).strip().split("\n")
    assert json.loads(lines[0]) == {"update": {"_id": "a"}}
    assert json.loads(lines[1]) == {"doc": {"embedding": [64, 127], "vector_status": "ok"}}
    assert len(lines) == 2


def test_bulk_batches_marks_unusable_vector_missing():
    batches = es_index._bulk_batches(["a"], [{"doc_id": "d", "text": "t"}], {0: [0.0, 0.0]})
    doc = json.loads(batches[0].strip().split("\n")[1])
    assert "embedding" not in doc and doc["vector_status"] == "missing"


def _search_env(monkeypatch, meta):
    bodies: list = []

    def fake_req(method, path, body=None, **kw):
        bodies.append(body)
        return {"hits": {"hits": []}}

    ec = {"provider": "openai", "model": "m", "dim": 3}
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_req", fake_req)
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: None)
    monkeypatch.setattr(embeddings, "cfg", lambda settings=None, **kw: ec)
    monkeypatch.setattr(es_index, "_index_meta", lambda world, idx=None: dict(meta))
    embed_calls: list = []
    monkeypatch.setattr(embeddings, "embed", lambda texts, c, **kw: embed_calls.append(texts) or [[0.1, 0.2, -0.3]])
    return bodies, embed_calls


_CURRENT = {"embed_provider": "openai", "embed_model": "m", "dim": 3,
            "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}


def test_query_vectors_are_bytes_in_both_search_paths(monkeypatch):
    bodies, _ = _search_env(monkeypatch, _CURRENT)
    assert es_index.search_knn_only("w", "q", k=5)[1] is None
    assert bodies[-1]["knn"]["query_vector"] == [42, 85, -127]
    es_index.search("w", "q", k=5)
    knn = bodies[-1]["query"]["function_score"]["query"]["knn"]
    assert knn["query_vector"] == [42, 85, -127]


def test_float_index_from_0195_is_not_queried_with_knn(monkeypatch):
    """0.19.5 の float の索引（`quant` が無い）には kNN を打たず、問いの埋め込みも呼ばない。"""
    old = {k: v for k, v in _CURRENT.items() if k != "quant"}
    bodies, embed_calls = _search_env(monkeypatch, old)
    assert es_index.search_knn_only("w", "q", k=5) == ([], "vector_feature_mismatch")
    assert es_index.search("w", "q", k=5)[1] == "vector_feature_mismatch"
    assert embed_calls == [] and all("knn" not in json.dumps(b) for b in bodies)


@pytest.mark.parametrize("over,expected", [
    ({}, False),
    ({"quant": None}, True),                                  # 0.19.5 の float の索引（`quant` が無い）
    ({"quant": None, "mapping_version": "12"}, True),
    ({"quant": "byte_other_v0"}, True),
])
def test_needs_reindex_on_quant_and_mapping_version(monkeypatch, over, expected):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index, "_index_kind", lambda w: "alias")
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda w: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(embeddings, "cfg", lambda settings=None, **kw: {"provider": "openai", "model": "m", "dim": 3})
    meta = {"content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION, "arms_sig": "sig-A",
            "analyzer_config_sig": "acfg-A", "source_excluded": True, **_CURRENT}
    meta.update(over)
    meta = {k: v for k, v in meta.items() if v is not None}
    monkeypatch.setattr(es_index, "_index_meta", lambda w, idx=None: dict(meta))
    assert es_index.needs_reindex("w", "c1") is expected


def test_fill_and_replace_skip_float_index(monkeypatch):
    """`quant` の無い索引には、足し直し・1 文書の入れ替えでも書かない（作り直しに任せる）。"""
    old = {k: v for k, v in _CURRENT.items() if k != "quant"}
    _search_env(monkeypatch, {**old, "content_sig": "c1"})
    monkeypatch.setattr(es_index, "count_missing_vectors", lambda w: 3)
    assert es_index.fill_missing_vectors("w")["skipped"] == "embed_config_changed"
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    assert es_index.replace_document("w", "a.md", "a.md", content_sig="c1")["error"] == "embed_config_changed"


# ===== 作り直しの途中の空きの安全弁 =====

def _bound_run():
    from sherpa.ingest import background
    background.bind_run(1)
    return background


def test_low_es_free_space_stops_index_world_and_discards_building(monkeypatch, tmp_path):
    from sherpa.ingest import background
    discarded: list = []
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    monkeypatch.setattr(es_index, "_es_free_bytes", lambda: 9 * 1024 ** 3)
    monkeypatch.setattr(es_index, "_discard_building", lambda w: discarded.append(w))
    monkeypatch.setattr(es_index, "_index_world", lambda *a, **k: es_index._space_point("w"))
    _bound_run()
    try:
        with pytest.raises(background.LowDiskStop) as ei:
            es_index.index_world("w")
    finally:
        background.unbind_run()
    assert isinstance(ei.value, background.StopRequested) and discarded == ["w"]
    assert str(ei.value) == "ディスクの空きが少ないため取り込みを止めました（空き 9.0 GB）"
    rec = background.stop_record(ei.value)
    assert rec["stop_reason"] == "low_disk" and rec["flags"][0]["reason"] == "low_disk_stop:9.0"


def test_low_cache_free_space_stops_and_enough_space_continues(monkeypatch, tmp_path):
    from sherpa.ingest import background
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    monkeypatch.setattr(es_index, "_es_free_bytes", lambda: 50 * 1024 ** 3)
    free = {"n": 50 * 1024 ** 3}
    monkeypatch.setattr(es_index.shutil, "disk_usage", lambda p: type("U", (), {"free": free["n"]})())
    es_index._space_point("w")                      # run の外では確かめない
    _bound_run()
    try:
        es_index._space_point("w")                  # 空きが足りていれば止めない
        free["n"] = 1024 ** 3
        with pytest.raises(background.LowDiskStop):
            es_index._space_point("w")
    finally:
        background.unbind_run()


def test_followup_scope_checks_free_space_without_a_run(monkeypatch, tmp_path):
    """取り込みワーカーの後追い（run の外）でも、`followup_scope` の間は空きを確かめて止める。"""
    from sherpa.ingest import background
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    monkeypatch.setattr(es_index, "_es_free_bytes", lambda: 1024 ** 3)
    es_index._space_point("w")
    with background.followup_scope():
        with pytest.raises(background.LowDiskStop):
            es_index._space_point("w")
    es_index._space_point("w")


def test_run_followups_ends_low_disk_stop_with_one_log_and_no_retry(monkeypatch, caplog):
    from sherpa.ingest import background, ingest_worker, llm_render, worker

    class _Beat:
        def set_state(self, *a, **k):
            pass

    calls: list = []

    def render(world, should_stop=None):
        calls.append((world, background.in_followup()))
        raise background.LowDiskStop(1024 ** 3)

    monkeypatch.setattr(llm_render, "take_followups", lambda: ["w"])
    monkeypatch.setattr(worker, "llm_render_pass", render)
    monkeypatch.setattr(ingest_worker.store, "count_queued_runs", lambda v: (0, 0))
    ingest_worker._run_followups(None, _Beat())
    assert calls == [("w", True)]
    assert sum("後追いをディスクの空き不足で止めました" in r.getMessage() for r in caplog.records) == 1


def test_empty_world_checks_space_before_dropping_old_index(monkeypatch, tmp_path):
    """ベクトルの対象が 1 件も無い資料フォルダでも、古い索引を消す前に空きを確かめて止める。"""
    from sherpa.ingest import background
    dropped: list = []
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_drop_stale_physicals", lambda w: True)
    monkeypatch.setattr(es_index.worlds, "derived_dir", lambda w: tmp_path / w)
    monkeypatch.setattr(es_index.worlds, "semantic_dir", lambda w: tmp_path / w / "semantic")
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_es_free_bytes", lambda: 1024 ** 3)
    monkeypatch.setattr(es_index, "_drop_old_for_space", lambda w: dropped.append(w) or True)
    _bound_run()
    try:
        with pytest.raises(background.LowDiskStop):
            es_index.index_world("w")
    finally:
        background.unbind_run()
    assert dropped == []


def test_resume_skips_run_stopped_for_low_disk(monkeypatch):
    from sherpa.ingest import resume
    started: list = []
    monkeypatch.setattr(resume.store, "list_worlds_with_interrupted_latest_run", lambda: [
        {"world_id": "a", "run_id": 1, "op": "sync", "stop_reason": "low_disk"},
        {"world_id": "b", "run_id": 2, "op": "sync", "stop_reason": None}])
    monkeypatch.setattr(resume, "_start_sync", lambda wid: started.append(wid) or True)
    monkeypatch.setattr(resume, "enabled", lambda: True)
    assert resume.resume_interrupted() == ["b"] and started == ["b"]
