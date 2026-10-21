"""ES 索引の作り直し（エイリアスの付け替え）・停止・ベクトルの無いチャンクの実 ES 検証。

ES 未起動なら SKIP（既存の ES 統合テストと同じ流儀）。文書・埋め込みの送り先は差し替えるが（外部境界）、
ES への操作・SQLite の埋め込みキャッシュ・停止要求の判定は本物を通す。
"""
from __future__ import annotations

import time

import pytest

from sherpa import es_index as es
from sherpa.ingest import background

_EC = {"provider": "fake", "model": "m", "dim": 3}


def _vec(text: str) -> list:
    return [float(len(text) % 7 + 1), 0.5, 0.25]


@pytest.fixture
def env(monkeypatch, tmp_path):
    if not es.available():
        pytest.skip("ES 未起動")
    world = f"alias-rebuild-{tmp_path.name[-8:]}".lower()
    state = {"texts": {}, "embed_calls": [], "embed_fail_calls": set(), "embed_all_fail": False}

    def docs(*a, **k):
        return [{"name": n, "md_path": None, "top_scope": "t", "branch": "office"} for n in state["texts"]]

    monkeypatch.setattr(es.corpus_docs, "world_documents", docs)
    monkeypatch.setattr(es.doc_text, "read_world_doc_text", lambda w, d: state["texts"][d["name"]])
    monkeypatch.setattr(es.worlds, "semantic_dir", lambda w: tmp_path / "semantic")
    monkeypatch.setattr(es.embeddings, "cfg", lambda settings=None, **kw: _EC)
    monkeypatch.setattr(es.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)

    def fake_embed(texts, ec, **kw):
        state["embed_calls"].append(list(texts))
        if state["embed_all_fail"] or len(state["embed_calls"]) in state["embed_fail_calls"]:
            return None
        return [_vec(t) for t in texts]

    monkeypatch.setattr(es.embeddings, "embed", fake_embed)
    monkeypatch.setattr(es, "_EMBED_FLUSH_CHUNKS", 3)
    es._embed_failed_at.clear()
    es.delete_world(world)
    yield world, state
    es._building.pop(world, None)
    es.delete_world(world)


def _physicals(world):
    return [n for n in es._concrete_indices(world) if n != es._index(world)]


def _hits(world, word):
    return sorted({h["doc_id"] for h in es.search(world, word, k=50, vector=False)[0]})


def _count_where(world, query):
    return es._req("POST", f"/{es._index(world)}/_count", {"query": query})["count"]


def _stop_when(monkeypatch, flag):
    monkeypatch.setattr(background, "stop_requested", lambda: flag["on"])


def test_rebuild_keeps_searching_the_old_index_until_the_alias_switch(env, monkeypatch):
    world, st = env
    st["texts"] = {"a.md": "alpha01 本文", "b.md": "alpha01 二つ目"}
    assert es.index_world(world, content_sig="s1")["indexed"] == 2
    old = es._alias_targets(world)
    assert len(old) == 1 and _hits(world, "alpha01") == ["a.md", "b.md"]

    st["texts"] = {"c.md": "beta02 新しい", "d.md": "beta02 もう一つ"}
    seen = []
    orig = es._StreamingBulkSender.send_group

    def spy(self, ids, bodies, vec_by_idx):
        seen.append((_hits(world, "alpha01"), _hits(world, "beta02"), len(_physicals(world))))
        return orig(self, ids, bodies, vec_by_idx)

    monkeypatch.setattr(es._StreamingBulkSender, "send_group", spy)
    es.index_world(world, content_sig="s2")
    assert seen == [(["a.md", "b.md"], [], 2)]                   # 作り直しの間は古い索引が答える（新旧 2 つの物理索引が並ぶ）
    assert _hits(world, "alpha01") == [] and _hits(world, "beta02") == ["c.md", "d.md"]
    new = es._alias_targets(world)
    assert len(new) == 1 and new != old and _physicals(world) == new  # 古い物理索引は消える
    assert es._index_meta(world)["content_sig"] == "s2"


def test_stop_in_pass1_leaves_alias_old_index_and_markers_untouched(env, monkeypatch):
    world, st = env
    st["texts"] = {f"o{i}.md": f"old03 n{i}" for i in range(2)}
    es.index_world(world, content_sig="s1")
    old = es._alias_targets(world)
    st["texts"] = {f"d{i}.md": f"new04 t{i}" for i in range(10)}
    flag = {"on": False}
    _stop_when(monkeypatch, flag)
    base = es.embeddings.embed

    def embed_then_stop(texts, ec, **kw):
        flag["on"] = len(st["embed_calls"]) >= 1               # 2 つ目のまとまりの前で止まる
        return base(texts, ec, **kw)

    monkeypatch.setattr(es.embeddings, "embed", embed_then_stop)
    with pytest.raises(background.StopRequested):
        es.index_world(world, content_sig="s2")
    assert es._alias_targets(world) == old and _physicals(world) == old
    assert _hits(world, "old03") == ["o0.md", "o1.md"]
    assert es._index_meta(world)["content_sig"] == "s1"          # 確定していない
    assert es._building == {}


def test_stop_in_pass2_discards_the_building_index(env, monkeypatch):
    world, st = env
    st["texts"] = {"o.md": "old05"}
    es.index_world(world, content_sig="s1")
    old = es._alias_targets(world)
    st["texts"] = {f"d{i}.md": f"new06 t{i}" for i in range(10)}
    flag = {"on": False}
    _stop_when(monkeypatch, flag)
    orig_ensure = es.ensure_index

    def ensure_then_flag(*a, **k):                              # Pass1 が終わり、新しい物理索引を作った直後に停止要求
        ok = orig_ensure(*a, **k)
        flag["on"] = True
        return ok

    monkeypatch.setattr(es, "ensure_index", ensure_then_flag)
    with pytest.raises(background.StopRequested):
        es.index_world(world, content_sig="s2")
    assert es._alias_targets(world) == old and _physicals(world) == old   # 作りかけは消え、古い索引は無傷
    assert _hits(world, "old05") == ["o.md"] and es._index_meta(world)["content_sig"] == "s1"


def test_low_space_drops_the_old_index_first_and_search_is_empty_until_the_switch(env, monkeypatch, caplog):
    world, st = env
    st["texts"] = {"a.md": "gamma11 本文"}
    es.index_world(world, content_sig="s1")
    old = es._alias_targets(world)
    assert es._store_bytes(old) > 0 and es._es_free_bytes() > 0
    monkeypatch.setattr(es, "_es_free_bytes", lambda: 1)       # ES の空きが古い索引の 2 倍に満たない
    st["texts"] = {"c.md": "delta12 新しい"}
    seen = []
    orig = es._StreamingBulkSender.send_group

    def spy(self, ids, bodies, vec_by_idx):
        seen.append((es.search(world, "gamma11", k=5), es.search_knn_only(world, "gamma11", k=5)[0], _physicals(world)))
        return orig(self, ids, bodies, vec_by_idx)

    monkeypatch.setattr(es._StreamingBulkSender, "send_group", spy)
    with caplog.at_level("WARNING", logger="sherpa"):
        r = es.index_world(world, content_sig="s2")
    assert r["indexed"] == 1 and "error" not in r
    (bm25, knn, physicals), = seen
    assert bm25[0] == [] and knn == [] and old[0] not in physicals   # 古い索引は先に消え、検索は空（例外にしない）
    assert _hits(world, "delta12") == ["c.md"] and es._index_meta(world)["content_sig"] == "s2"
    assert any("空きが足りない" in m for m in caplog.messages)


def test_stop_after_low_space_drop_leaves_no_index_and_the_next_update_rebuilds(env, monkeypatch):
    world, st = env
    st["texts"] = {"o.md": "old13"}
    es.index_world(world, content_sig="s1")
    monkeypatch.setattr(es, "_es_free_bytes", lambda: 1)
    st["texts"] = {f"d{i}.md": f"new14 t{i}" for i in range(10)}
    flag = {"on": False}
    _stop_when(monkeypatch, flag)
    orig_ensure = es.ensure_index

    def ensure_then_flag(*a, **k):
        ok = orig_ensure(*a, **k)
        flag["on"] = True
        return ok

    monkeypatch.setattr(es, "ensure_index", ensure_then_flag)
    with pytest.raises(background.StopRequested):
        es.index_world(world, content_sig="s2")
    assert es._concrete_indices(world) == [] and es._alias_targets(world) == []
    assert es.search(world, "old13", k=5)[0] == [] and es.needs_reindex(world, "s2")
    monkeypatch.setattr(es, "ensure_index", orig_ensure)
    flag["on"] = False
    assert es.index_world(world, content_sig="s2")["indexed"] == 10
    assert len(_hits(world, "new14")) == 10 and not es.needs_reindex(world, "s2")


def test_bulk_progress_log_shows_es_free_space_and_warns_once_when_low(env, monkeypatch, caplog):
    world, st = env
    st["texts"] = {f"d{i}.md": f"free15 t{i}" for i in range(10)}
    monkeypatch.setattr(es, "_LOW_FREE_WARN_BYTES", 1 << 62)    # 実の空きが必ず下回る
    with caplog.at_level("INFO"):
        assert es.index_world(world, content_sig="s1")["indexed"] == 10
    progress = [m for m in caplog.messages if m.startswith("取り込みの進み具合: 全文索引に書き込んでいます ") and "件（" in m]
    assert progress and all("ES の空き" in m for m in progress)
    assert sum("5GB を切りました" in m for m in caplog.messages) == 1


def _compact_small(monkeypatch, settle=0, segments=True):
    monkeypatch.setattr(es, "_ES_COMPACT_MIN_CHUNKS", 1)
    monkeypatch.setattr(es, "_ES_COMPACT_SETTLE_SEC", settle)
    monkeypatch.setattr(es, "_ES_COMPACT_POLL_SEC", 0.05)
    if segments:                                                # まとまりごとに flush してセグメントを増やす（畳む相手を作る）
        orig = es._StreamingBulkSender.send_group

        def send(self, ids, bodies, vec_by_idx):
            ok = orig(self, ids, bodies, vec_by_idx)
            es._req("POST", f"/{self.idx}/_flush")
            return ok

        monkeypatch.setattr(es, "_ES_BULK_BATCH_MAX_DOCS", 1)
        monkeypatch.setattr(es._StreamingBulkSender, "send_group", send)


def test_compaction_at_the_end_keeps_every_chunk(env, monkeypatch, caplog):
    world, st = env
    st["texts"] = {f"d{i}.md": f"compact16 t{i}" for i in range(10)}
    _compact_small(monkeypatch)
    calls, merges = [], []
    orig, orig_req = es._compact_building, es._req
    monkeypatch.setattr(es, "_compact_building", lambda w, idx: (calls.append(idx), orig(w, idx)))

    def req(method, path, body=None, **kw):
        if "/_forcemerge" in path:
            merges.append(path)
        return orig_req(method, path, body, **kw)

    monkeypatch.setattr(es, "_req", req)
    with caplog.at_level("INFO"):
        r = es.index_world(world, content_sig="s1")
    assert r["indexed"] == 10 and "error" not in r and r["vectors"] is True
    assert calls == es._alias_targets(world) and len(merges) == 1  # 付け替える索引を、終わりに 1 回だけ畳む
    assert sum("検索の索引を詰めています を始めます" in m for m in caplog.messages) == 1
    assert len(_hits(world, "compact16")) == 10
    assert _count_where(world, {"exists": {"field": "embedding"}}) == 10
    assert not [x for x in caplog.records if x.levelname == "WARNING" and "詰め" in x.getMessage()]


@pytest.mark.parametrize("broken", ["forcemerge_error", "task_never_ends"])
def test_compaction_failure_or_timeout_warns_once_and_the_ingest_succeeds(env, monkeypatch, caplog, broken):
    world, st = env
    st["texts"] = {f"d{i}.md": f"compact17 t{i}" for i in range(4)}
    _compact_small(monkeypatch)
    monkeypatch.setattr(es, "_ES_COMPACT_TIMEOUT_SEC", 0.2)
    orig = es._req

    def req(method, path, body=None, **kw):
        if broken == "forcemerge_error" and "/_forcemerge" in path:
            raise OSError("boom")
        if broken == "task_never_ends" and path.startswith("/_tasks/"):
            return {"completed": False}
        return orig(method, path, body, **kw)

    monkeypatch.setattr(es, "_req", req)
    with caplog.at_level("WARNING", logger="sherpa"):
        r = es.index_world(world, content_sig="s1")
    assert r["indexed"] == 4 and "error" not in r
    assert len(_hits(world, "compact17")) == 4 and es._index_meta(world)["content_sig"] == "s1"
    assert len([x for x in caplog.records if x.levelname == "WARNING" and "詰め" in x.getMessage()]) == 1


def test_stop_while_waiting_to_compact_discards_the_building_index(env, monkeypatch):
    world, st = env
    st["texts"] = {"o.md": "old18"}
    es.index_world(world, content_sig="s1")
    old = es._alias_targets(world)
    st["texts"] = {f"d{i}.md": f"new18 t{i}" for i in range(10)}
    _compact_small(monkeypatch, settle=30, segments=False)
    flag = {"on": False}
    _stop_when(monkeypatch, flag)
    orig_req = es._req

    def req(method, path, body=None, **kw):
        out = orig_req(method, path, body, **kw)
        if path.endswith("/_flush"):
            flag["on"] = True                                   # 詰める前の待ちの間に停止要求
        return out

    monkeypatch.setattr(es, "_req", req)
    t0 = time.monotonic()
    with pytest.raises(background.StopRequested):
        es.index_world(world, content_sig="s2")
    assert time.monotonic() - t0 < 10                           # 待ち（30 秒）の途中で止まる
    assert es._alias_targets(world) == old and _physicals(world) == old
    assert _hits(world, "old18") == ["o.md"] and es._index_meta(world)["content_sig"] == "s1"


def test_resume_after_stop_does_not_ask_the_destination_for_cached_vectors(env, monkeypatch):
    world, st = env
    st["texts"] = {f"d{i}.md": f"resume07 t{i}" for i in range(10)}
    flag = {"on": False}
    _stop_when(monkeypatch, flag)
    base = es.embeddings.embed

    def embed_then_stop(texts, ec, **kw):
        out = base(texts, ec, **kw)
        flag["on"] = len(st["embed_calls"]) >= 2
        return out

    monkeypatch.setattr(es.embeddings, "embed", embed_then_stop)
    with pytest.raises(background.StopRequested):
        es.index_world(world, content_sig="s1")
    sent_first = [t for call in st["embed_calls"] for t in call]
    assert 0 < len(sent_first) < 10
    flag["on"] = False
    monkeypatch.setattr(es.embeddings, "embed", base)
    st["embed_calls"].clear()
    r = es.index_world(world, content_sig="s1")
    sent_second = [t for call in st["embed_calls"] for t in call]
    assert not set(sent_first) & set(sent_second)                # キャッシュ済みの分は問い合わせない
    assert len(sent_first) + len(sent_second) == 10
    assert r["reused"] == len(sent_first) and r["vectors"] is True


def test_failed_group_is_indexed_without_vector_and_found_by_fulltext(env):
    world, st = env
    st["texts"] = {f"d{i}.md": f"partial08 uniq{i}x" for i in range(10)}
    st["embed_fail_calls"] = {2}                                # 2 つ目のまとまり（3 件）だけ失敗
    r = es.index_world(world, content_sig="s1")
    assert r.get("error") is None and r["vectors_missing"] == 3 and "embed_degraded" not in r
    assert _count_where(world, {"match_all": {}}) == 10
    assert _count_where(world, {"term": {"vector_status": "missing"}}) == 3
    assert _count_where(world, {"exists": {"field": "embedding"}}) == 7
    assert _count_where(world, {"bool": {"must": [{"exists": {"field": "embedding"}}],
                                         "must_not": [{"term": {"vector_status": "missing"}}]}}) == 7
    missing_docs = es._req("POST", f"/{es._index(world)}/_search",
                           {"size": 10, "query": {"term": {"vector_status": "missing"}}})["hits"]["hits"]
    for h in missing_docs:
        assert h["_source"]["doc_id"] in _hits(world, h["_source"]["text"].split()[-1])   # 全文検索で見つかる
    assert es.needs_reindex(world, "s1") is False                # missing は全再構築の理由にしない


def test_fill_missing_vectors_fills_once_without_duplicates(env):
    world, st = env
    st["texts"] = {f"d{i}.md": f"fill09 uniq{i}x" for i in range(10)}
    st["embed_fail_calls"] = {2}
    es.index_world(world, content_sig="s1")
    texts_before = {h["_id"]: h["_source"]["text"] for h in es._scroll_hits(world, {"match_all": {}}, ["text"])}
    st["embed_fail_calls"] = set()
    st["embed_calls"].clear()
    es._embed_failed_at.clear()
    r = es.fill_missing_vectors(world)
    assert r == {"available": True, "filled": 3, "missing": 0}
    assert _count_where(world, {"match_all": {}}) == 10          # 二重に入らない
    assert _count_where(world, {"exists": {"field": "embedding"}}) == 10
    assert _count_where(world, {"term": {"vector_status": "missing"}}) == 0
    texts_after = {h["_id"]: h["_source"]["text"] for h in es._scroll_hits(world, {"match_all": {}}, ["text"])}
    assert texts_after == texts_before                           # 本文は触らない
    st["embed_calls"].clear()
    assert es.fill_missing_vectors(world) == {"available": True, "filled": 0, "missing": 0}   # 2 回目は何もしない
    assert st["embed_calls"] == []
    assert _count_where(world, {"match_all": {}}) == 10


def test_fill_missing_vectors_leaves_unobtained_and_respects_cooldown_and_stop(env, monkeypatch):
    world, st = env
    st["texts"] = {f"d{i}.md": f"keep10 uniq{i}x" for i in range(4)}
    st["embed_all_fail"] = True
    r = es.index_world(world, content_sig="s1")
    assert r["vectors_missing"] == 4 and r["embed_degraded"] is True
    st["embed_calls"].clear()
    assert es.fill_missing_vectors(world)["skipped"] == "embed_recently_failed"   # 失敗の直後は叩き直さない
    assert st["embed_calls"] == []
    es._embed_failed_at.clear()
    assert es.fill_missing_vectors(world) == {"available": True, "filled": 0, "missing": 4}   # 取れなければ missing のまま
    st["embed_all_fail"] = False
    es._embed_failed_at.clear()
    flag = {"on": True}
    _stop_when(monkeypatch, flag)
    with pytest.raises(background.StopRequested):
        es.fill_missing_vectors(world)
    assert _count_where(world, {"term": {"vector_status": "missing"}}) == 4
    flag["on"] = False
    assert es.fill_missing_vectors(world)["missing"] == 0


def test_migration_from_a_real_index_named_like_the_alias(env):
    world, st = env
    alias = es._index(world)
    es._req("PUT", "/" + alias, es._mapping(3, "standard", {"content_sig": "s1", "mapping_version": "10", "doc_count": 1}))
    es._req("PUT", f"/{alias}/_doc/x?refresh=true", {"doc_id": "legacy.md", "text": "legacy11 本文", "ext": ".md"})
    es._req("PUT", f"/{alias}-g1", {})                           # 前回落ちた作りかけ
    assert es._index_kind(world) == "index" and es.needs_reindex(world, "s1") is True
    assert _hits(world, "legacy11") == ["legacy.md"]             # 作り直しが終わるまで旧索引で検索できる
    st["texts"] = {"n.md": "migrated12 本文"}
    r = es.index_world(world, content_sig="s1")
    assert r["indexed"] == 1
    assert es._index_kind(world) == "alias"
    assert es._concrete_indices(world) == es._alias_targets(world)   # 旧索引と作りかけは消え、物理索引だけが残る
    assert _hits(world, "migrated12") == ["n.md"] and _hits(world, "legacy11") == []
    assert es.needs_reindex(world, "s1") is False                # 移行は 1 回きり


def test_delete_world_removes_alias_and_every_physical_index(env):
    world, st = env
    st["texts"] = {"a.md": "del13 本文"}
    es.index_world(world, content_sig="s1")
    es._req("PUT", f"/{es._index(world)}-g2", {})
    assert es.delete_world(world) is True
    assert es._concrete_indices(world) == [] and es._alias_targets(world) == []
    assert es.count(world) is None


def test_reconcile_keeps_generation_indices_of_a_registered_world(monkeypatch):
    """世代付きの物理索引は、エイリアス名（登録 world の索引名）で所有者を判定して残す。ほかの索引は消さない（ES は共有しうるので一覧と削除は差し替える）。"""
    alias = es._index("registered")
    stray = "sherpa-kb-orphan-0123456789-g5"
    monkeypatch.setattr(es, "list_kb_indices", lambda: [alias + "-g1700000000000", alias, stray])
    sent = []
    monkeypatch.setattr(es, "_req", lambda method, path, body=None, **kw: sent.append((method, path)) or {})
    assert es.reconcile(["registered"]) == [stray]
    assert sent == [("DELETE", "/" + stray)]
