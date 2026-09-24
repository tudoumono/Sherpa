"""非agentic troubleshoot への ES 統合（live ES/Neo4j 不要・stub）。"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")


def _patch(es_hits, base_candidates):
    from sherpa import chat_service, documents, es_index
    old_rt, old_search, old_rel = chat_service.run_troubleshoot, es_index.search, documents.world_rel_set

    def fake_rt(session, symptom, world, scope_paths=None, depth=None):
        return {"type": "troubleshoot", "world": world, "symptom": symptom,
                "anchors": ["NIGHTLY"], "candidates": list(base_candidates)}

    chat_service.run_troubleshoot = fake_rt
    # RV2（FBK-1・2026-09-01）: `es_index.search()` は (hits, degrade_reason) を返す。
    es_index.search = lambda world, q, scope_paths=None, k=8, vector=True, layer=None, **kw: (list(es_hits), None)
    documents.world_rel_set = lambda world, **kw: {"docs/ops.md", "docs/es.md", "docs/dup.md"}
    return chat_service, (old_rt, old_search, old_rel)


def _restore(saved):
    from sherpa import chat_service, documents, es_index
    chat_service.run_troubleshoot, es_index.search, documents.world_rel_set = saved


def test_dispatch_troubleshoot_adds_es_document_card_and_source():
    base = [{
        "name": "NIGHTLY", "label": "Batch", "category": "ジョブ", "role": "ジョブ",
        "distance": 1, "path": ["NIGHTLY", "TAXCALC"], "source": "graph",
        "evidence": {"edges": [{"doc": "docs/ops.md"}], "grep": []},
    }]
    es_hits = [
        {"doc_id": "docs/es.md", "line": 3, "text": "password=secret ABEND の対処", "score": 4.2, "ext": ".md"},
        {"doc_id": "missing.md", "line": 1, "text": "古い索引", "score": 9.9, "ext": ".md"},
    ]
    chat_service, saved = _patch(es_hits, base)
    try:
        env = chat_service._dispatch(None, "troubleshoot", "NIGHTLY ABEND", "w", None,
                                     {"world": "w", "scope_paths": ["docs"], "source": "explicit"})
        cards = env["data"]["candidates"]
        assert [c["source"] for c in cards] == ["graph", "es"]          # graph と ES を交互に統合
        es_card = cards[1]
        assert es_card["name"] == "docs/es.md" and es_card["role"] == "関連文書"
        assert "password=[REDACTED]" in es_card["evidence"]["grep"][0]["text"]
        assert "missing.md" not in {c["name"] for c in cards}           # stale doc は出さない
        srcs = {s["doc_id"] for s in env["sources"]}
        assert {"docs/ops.md", "docs/es.md"} <= srcs
    finally:
        _restore(saved)


def test_troubleshoot_es_duplicate_doc_span_is_deduped():
    base = [{
        "name": "既存grep", "label": "Document", "category": "文書", "role": "関連文書",
        "distance": None, "path": [], "source": "grep",
        "evidence": {"edges": [], "grep": [{"doc_id": "docs/dup.md", "line": 5, "span": [5, 5],
                                             "text": "grep", "match": "ABEND"}]},
    }]
    es_hits = [{"doc_id": "docs/dup.md", "line": 5, "text": "ES duplicate", "score": 2.0, "ext": ".md"}]
    chat_service, saved = _patch(es_hits, base)
    try:
        merged = chat_service._merge_troubleshoot_with_es(
            {"type": "troubleshoot", "world": "w", "symptom": "ABEND", "candidates": base},
            "w", "ABEND", ["docs"])
        assert len(merged["candidates"]) == 1
        assert merged["candidates"][0]["source"] == "grep"
    finally:
        _restore(saved)


def test_troubleshoot_facts_redacts_base_grep_secret():
    """LLM へ渡す facts では base(grep) 根拠本文の秘匿値も伏せる（ES だけでなく grep も・RV High）。"""
    from sherpa import agents
    env = {"data": {"candidates": [{
        "name": "NIGHTLY", "role": "ジョブ",
        "evidence": {"edges": [], "grep": [{"doc_id": "src/x.jcl", "line": 2, "span": [2, 2],
                                            "text": "password=topsecretvalue123 を使う", "match": "x"}]},
    }]}}
    facts = agents._facts("troubleshoot", env)
    assert "topsecretvalue123" not in facts and "password=[REDACTED]" in facts


def test_es_troubleshoot_cards_tolerate_es_hit_without_line():
    """rag_chunks 由来の ES ヒット（line キー無し）でもカード生成はクラッシュしない。
    span=[None, None] はクラッシュしないが意味の無い値になる既知の挙動で、chat 側の検索ロジック
    自体はこのスライスでは変更しない（h.get("line") の防御的取得のみで足りることの固定）。"""
    es_hits = [{"doc_id": "docs/es.md", "text": "rag_chunks 由来のヒット", "score": 4.0, "ext": ".md",
                "chunk_id": "rc1"}]
    chat_service, saved = _patch(es_hits, [])
    try:
        cards = chat_service._es_troubleshoot_cards("w", "ABEND", ["docs"])
        assert len(cards) == 1
        ev = cards[0]["evidence"]["grep"][0]
        assert ev["line"] is None and ev["span"] == [None, None]
    finally:
        _restore(saved)


def test_es_troubleshoot_card_merges_multiple_spans_of_same_doc():
    """同一 doc の複数 span は1カードの evidence.grep に集約（name dedupe で落とさない・RV Med）。"""
    es_hits = [
        {"doc_id": "docs/es.md", "line": 3, "text": "一つ目", "score": 4.0, "ext": ".md"},
        {"doc_id": "docs/es.md", "line": 9, "text": "二つ目", "score": 3.0, "ext": ".md"},
    ]
    chat_service, saved = _patch(es_hits, [])
    try:
        cards = chat_service._es_troubleshoot_cards("w", "ABEND", ["docs"])
        assert len(cards) == 1                                          # doc ごとに1カード
        spans = [g["span"] for g in cards[0]["evidence"]["grep"]]
        assert [3, 3] in spans and [9, 9] in spans                      # 別 span は両方残る
    finally:
        _restore(saved)
