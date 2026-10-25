"""ES の BM25 が本文に加えて見出し系の欄（labels・region_context）も引くことを、実 ES で確かめる結合テスト。

見出し・シート名だけに語がある文書が当たり、当たった欄が示されること、本文に直接一致する文書が見出しだけの一致より上に来ることを固定する。
ES に到達できなければ SKIP。
"""
from __future__ import annotations

import json

import pytest

from sherpa import es_index as es

WORLD = "es-fields-check"

_LONG_BODY = "この処理では受注入力の内容を検証し、結果を集計して出力する。" + "補足の説明が続く。" * 30
_DOCS = [
    {"_id": "body", "doc_id": "a/本文に一致.md", "line": 1, "text": _LONG_BODY},
    {"_id": "heading", "doc_id": "b/見出しだけ.md", "line": 1, "text": "合計金額を計算する。",
     "labels": ["受注入力"], "region_context": "受注入力 画面"},
    {"_id": "sheet", "doc_id": "c/シート名だけ.md", "line": 1, "text": "別の内容の説明。", "region_context": "受注入力"},
    {"_id": "none", "doc_id": "d/無関係.md", "line": 1, "text": "在庫の棚卸を行う。"},
]


@pytest.fixture
def fields_world():
    if not es.available():
        pytest.skip("ES 未起動")
    from _world_registry import register_test_world
    register_test_world(WORLD)
    es.delete_world(WORLD)
    es._req("PUT", f"/{es._index(WORLD)}", es._mapping(None, "kuromoji"))
    lines = []
    for d in _DOCS:
        lines.append(json.dumps({"index": {"_id": d["_id"]}}))
        lines.append(json.dumps({k: v for k, v in d.items() if k != "_id"}, ensure_ascii=False))
    es._req("POST", f"/{es._index(WORLD)}/_bulk?refresh=true", "\n".join(lines) + "\n", ndjson=True)
    yield WORLD
    es.delete_world(WORLD)


def test_heading_only_documents_match_and_show_which_field(fields_world):
    hits, reason = es.search(fields_world, "受注入力", k=10, vector=False)
    assert reason is None
    by_doc = {h["doc_id"]: h for h in hits}
    assert "d/無関係.md" not in by_doc
    assert by_doc["b/見出しだけ.md"]["matched_fields"] == ["labels", "region_context"]
    assert by_doc["c/シート名だけ.md"]["matched_fields"] == ["region_context"]
    assert by_doc["a/本文に一致.md"]["matched_fields"] == ["text"]


def test_body_match_ranks_above_heading_only_match(fields_world):
    hits, _ = es.search(fields_world, "受注入力", k=10, vector=False)
    order = [h["doc_id"] for h in hits]
    assert order.index("a/本文に一致.md") < order.index("b/見出しだけ.md")
    assert order.index("a/本文に一致.md") < order.index("c/シート名だけ.md")


def test_recheck_marks_heading_match_for_hits_outside_bm25_top(fields_world):
    hits = [{"doc_id": "b/見出しだけ.md", "keyword_match": False, "_es_id": "heading"},
            {"doc_id": "d/無関係.md", "keyword_match": False, "_es_id": "none"}]
    out = es._mark_keyword_matches(fields_world, "受注入力", hits)
    assert out[0]["keyword_match"] is True and "text" not in out[0]["matched_fields"]
    assert out[1]["keyword_match"] is False and "matched_fields" not in out[1]
