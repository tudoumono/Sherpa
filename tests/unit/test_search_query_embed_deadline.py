"""検索の質問の埋め込みの総期限と、BM25 の欄（本文＋見出し系）の単体テスト。

対象: `embeddings.embed(deadline_s=)`／`es_index.search`／`_mark_keyword_matches`。
モックは外部境界（`llm.post_json`＝埋め込みの HTTP・ES の `_req`）と時計（`time.monotonic`・`embeddings._sleep`）だけ。
"""
from __future__ import annotations

import socket

import pytest

from sherpa import embeddings, es_index, llm

_OLLAMA = {"provider": "ollama", "url": "http://127.0.0.1:11434", "model": "m", "dim": 3}
_META = {"embed_provider": "ollama", "embed_model": "m", "dim": 3,
         "embed_algo": embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": embeddings.ES_VECTOR_QUANT}


class _Clock:
    """時計と HTTP の模擬。送信ごとに「応答待ちの timeout いっぱい」進めて通信の timeout を投げる。"""

    def __init__(self, monkeypatch):
        self.t = 0.0
        self.timeouts: list = []
        monkeypatch.setattr(embeddings.time, "monotonic", lambda: self.t)
        monkeypatch.setattr(embeddings, "_sleep", lambda s: setattr(self, "t", self.t + s))
        monkeypatch.setattr(llm, "post_json", self._post)

    def _post(self, url, headers, body, timeout):
        self.timeouts.append(timeout)
        self.t += timeout
        raise socket.timeout("timed out")


def _bm25_hit():
    return {"_id": "c1", "_score": 1.0, "_source": {"doc_id": "a.md", "line": 1, "text": "本文"}}


def _setup_search(monkeypatch, hits=None):
    monkeypatch.delenv("SHERPA_SEARCH_EMBED_TIMEOUT_SEC", raising=False)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(_META))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: dict(_OLLAMA))
    bodies: list = []

    def fake_req(method, path, body=None, **kw):
        bodies.append(body)
        return {"hits": {"hits": hits if hits is not None else [_bm25_hit()]}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    return bodies


def test_query_embed_past_deadline_returns_bm25_with_reason(monkeypatch):
    clock = _Clock(monkeypatch)
    bodies = _setup_search(monkeypatch)
    hits, reason = es_index.search("w", "質問")
    assert reason == "query_embed_failed"
    assert [h["doc_id"] for h in hits] == ["a.md"] and hits[0]["keyword_match"] is True
    assert clock.t <= es_index._SEARCH_EMBED_TIMEOUT_DEFAULT              # 10 秒を超えて待たない
    assert clock.timeouts and max(clock.timeouts) <= es_index._SEARCH_EMBED_TIMEOUT_DEFAULT   # 通信にも残り時間が渡る
    assert len(bodies) == 1 and "knn" not in str(bodies[0])               # BM25 だけを 1 回引く


def test_search_embed_deadline_is_configurable(monkeypatch):
    clock = _Clock(monkeypatch)
    _setup_search(monkeypatch)
    monkeypatch.setenv("SHERPA_SEARCH_EMBED_TIMEOUT_SEC", "3")
    assert es_index.search("w", "質問")[1] == "query_embed_failed"
    assert clock.t <= 3 and max(clock.timeouts) <= 3


@pytest.mark.parametrize("raw", ["", "abc", "0", "-5", "9999"])
def test_search_embed_deadline_invalid_env_falls_back_to_default(monkeypatch, raw):
    monkeypatch.setenv("SHERPA_SEARCH_EMBED_TIMEOUT_SEC", raw)
    assert es_index._search_embed_timeout_s() == es_index._SEARCH_EMBED_TIMEOUT_DEFAULT


def test_knn_only_also_stops_at_deadline(monkeypatch):
    clock = _Clock(monkeypatch)
    _setup_search(monkeypatch)
    assert es_index.search_knn_only("w", "質問") == ([], "query_embed_failed")
    assert clock.t <= es_index._SEARCH_EMBED_TIMEOUT_DEFAULT


def test_ingest_embedding_retry_settings_are_unchanged(monkeypatch):
    """期限を渡さない呼び出し（取り込み）は、従来どおり 1 回 60 秒・最大 5 回の再送・合計 300 秒。"""
    assert (embeddings._TIMEOUT, embeddings._RETRY_MAX_RETRIES, embeddings._RETRY_MAX_TOTAL_SECONDS) == (60, 5, 300)

    class _Retryable(_Clock):
        def _post(self, url, headers, body, timeout):
            self.timeouts.append(timeout)
            self.t += 1
            raise socket.timeout("timed out")

    clock = _Retryable(monkeypatch)
    assert embeddings._embed_batch(["a"], dict(_OLLAMA)) is None
    assert len(clock.timeouts) == 1 + embeddings._RETRY_MAX_RETRIES    # 初回＋再送 5 回
    assert set(clock.timeouts) == {embeddings._TIMEOUT}                 # 送信ごとの待ちは 60 秒のまま


def test_embed_without_deadline_waits_full_timeout(monkeypatch):
    clock = _Clock(monkeypatch)
    assert embeddings.embed(["a"], dict(_OLLAMA)) is None
    assert clock.timeouts[0] == embeddings._TIMEOUT


# ---- BM25 の欄 ----

def test_bm25_queries_body_and_heading_fields_with_body_heaviest(monkeypatch):
    bodies = _setup_search(monkeypatch)
    es_index.search("w", "項目名", vector=False)
    body = bodies[0]
    mm = body["query"]["function_score"]["query"]["bool"]["must"][0]["multi_match"]
    weights = {f.split("^")[0]: float(f.split("^")[1]) for f in mm["fields"]}
    assert set(weights) == {"text", "labels", "region_context"}
    assert weights["text"] > weights["labels"] > 0 and weights["text"] > weights["region_context"] > 0
    assert set(body["highlight"]["fields"]) == {"text", "labels", "region_context"}


def test_hit_reports_the_fields_the_words_matched():
    res = {"hits": {"hits": [
        {"_id": "1", "_score": 2.0, "_source": {"doc_id": "a.md", "text": "本文"}, "highlight": {"text": ["<em>本文</em>"]}},
        {"_id": "2", "_score": 1.0, "_source": {"doc_id": "b.md", "text": "別の本文"},
         "highlight": {"region_context": ["<em>受注</em>一覧"], "labels": ["<em>受注</em>"]}},
        {"_id": "3", "_score": 0.5, "_source": {"doc_id": "c.md", "text": "x"}},
    ]}}
    hits = es_index._parse_hits(res)
    assert hits[0]["matched_fields"] == ["text"]
    assert hits[1]["matched_fields"] == ["labels", "region_context"] and "text" not in hits[1]["matched_fields"]
    assert "matched_fields" not in hits[2]


def test_mark_keyword_matches_rechecks_same_fields_and_reports_them(monkeypatch):
    seen: list = []

    def fake_req(method, path, body=None, **kw):
        seen.append(body)
        return {"hits": {"hits": [{"_id": "k1", "highlight": {"region_context": ["<em>受注</em>"]}}]}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    hits = [{"doc_id": "a.md", "keyword_match": False, "_es_id": "k1"},
            {"doc_id": "b.md", "keyword_match": False, "_es_id": "k2"}]
    out = es_index._mark_keyword_matches("w", "受注", hits)
    assert out[0]["keyword_match"] is True and out[0]["matched_fields"] == ["region_context"]
    assert out[1]["keyword_match"] is False and "matched_fields" not in out[1]
    assert seen[0]["query"]["bool"]["must"] == [es_index._keyword_clause("受注")]
    assert set(seen[0]["highlight"]["fields"]) == {"text", "labels", "region_context"}


def test_query_embed_arriving_after_the_deadline_is_discarded(monkeypatch):
    clock = _Clock(monkeypatch)
    _setup_search(monkeypatch)

    def slow_success(url, headers, body, timeout):          # 応答が途切れ途切れに届き、期限の後に揃う
        clock.t += es_index._SEARCH_EMBED_TIMEOUT_DEFAULT + 1
        return {"embeddings": [[0.1, 0.2, 0.3]]}

    monkeypatch.setattr(llm, "post_json", slow_success)
    hits, reason = es_index.search("w", "質問")
    assert reason == "query_embed_failed"
    assert [h["doc_id"] for h in hits] == ["a.md"]
