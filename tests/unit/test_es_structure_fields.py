"""ES の構造の欄（`identifier_values`／`record_identity_values`／`labels`／`region_context`）と
`es_search` の `exact`（識別子の完全一致）の単体テスト。実 ES 不要（`_req` だけ差し替える）。
"""
from __future__ import annotations

import json

import pytest

from sherpa import documents, es_index
from sherpa import tool_dispatch as TD
from sherpa.ingest import text_kind

_STRUCT_KEYS = ("identifier_values", "record_identity_values", "labels", "region_context")


def _mention(value, role):
    return {"value": value, "normalized_value": value.casefold(), "role": role, "role_basis": "t",
            "field_label": "項目ID", "evidence_id": "e", "locator": {}, "text_span": {"start": 0, "end": len(value)}}


_RECORD = {
    "chunk_id": "rc1", "source_rel_path": "a.xlsx",
    "body": "項目ID: A01\n表示名: 現行顧客区分\n状態: 有効\n表示名のExcel原値: 現行顧客区分\n備考: memo",
    "record_keys": [{"label": "項目ID", "value": "A01", "evidence_id": "e"}],
    "identifier_mentions": [_mention("A01", "record_identity"), _mention("B02", "reference")],
    "record_identity_values_exact": ["A01"],
    "region_context": {"title": "対象項目", "sheet": "項目定義", "cell_range": "A1:C6", "mode": "header_record",
                       "section_path": ["第1章"]},
    "section_path": ["シート「項目定義」"],
}


def _write(tmp_path, rows, chunks):
    jsonl, md = tmp_path / "a.xlsx.rag_chunks.jsonl", tmp_path / "a.xlsx.rag.md"
    jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    md.write_text("".join(f"<!-- chunk:{cid} -->\n{body}\n\n" for cid, body in chunks), encoding="utf-8")
    return jsonl, md


# ===== mapping =====

def test_mapping_has_structure_fields():
    props = es_index._mapping(None, "kuromoji")["mappings"]["properties"]
    assert props["identifier_values"] == {"type": "keyword"}
    assert props["record_identity_values"] == {"type": "keyword"}
    assert props["labels"] == {"type": "text", "analyzer": "kuromoji"}
    assert props["region_context"] == {"type": "text", "analyzer": "kuromoji"}
    assert props["section_path"] == {"type": "keyword"}
    assert props["text"] == {"type": "text", "analyzer": "kuromoji"}        # 本文の欄は変えない


def test_mapping_version_is_bumped_so_old_index_is_rebuilt(monkeypatch):
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    meta = {"content_sig": "c1", "arms_sig": "sig-A", "analyzer_config_sig": "acfg-A", "source_excluded": True}
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {**meta, "mapping_version": "9"})
    assert es_index.needs_reindex("w", "c1") is True
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {**meta, "mapping_version": es_index.ES_MAPPING_VERSION})
    assert es_index.needs_reindex("w", "c1") is False


# ===== チャンク → 欄 =====

def test_iter_rag_chunk_entries_maps_structure_fields(tmp_path):
    jsonl, md = _write(tmp_path, [_RECORD], [("rc1", "本文")])
    anchors, reason = es_index._load_rag_md_anchors(md)
    assert reason is None
    (_id, body, text, _skip), = es_index._iter_rag_chunk_entries(jsonl, "a.xlsx", anchors, {"doc_id": "a.xlsx"})
    assert text == "本文" and body["text"] == "本文"
    assert body["identifier_values"] == ["A01", "B02"]
    assert body["record_identity_values"] == ["A01"]                         # 参照の B02 はレコードの定義ではない
    assert body["labels"] == ["現行顧客区分", "有効"]                          # 識別子・Excel の補助行・日本語の無い値は入れない
    assert body["region_context"] == "対象項目 項目定義 第1章"
    assert body["section_path"] == ["シート「項目定義」"]


def test_iter_rag_chunk_entries_omits_structure_fields_when_absent(tmp_path):
    row = {"chunk_id": "rc1", "source_rel_path": "a.xlsx", "body": "記載値: 備考"}
    jsonl, md = _write(tmp_path, [row], [("rc1", "本文")])
    anchors, _ = es_index._load_rag_md_anchors(md)
    (_id, body, _text, _skip), = es_index._iter_rag_chunk_entries(jsonl, "a.xlsx", anchors, {"doc_id": "a.xlsx"})
    assert not any(k in body for k in _STRUCT_KEYS)


def test_labels_only_for_keyed_records_and_are_bounded():
    no_key = {"body": "隣接項目: 参照値01"}
    assert es_index._chunk_structure_meta(no_key) == {}
    body = "\n".join(f"項目{i}: 値あ{i}" for i in range(100))
    labels = es_index._chunk_labels({"record_keys": [{"label": "k", "value": "v"}], "body": body}, [])
    assert len(labels) == es_index._LABELS_MAX_PER_CHUNK


def test_independent_tables_on_same_rows_do_not_share_values():
    """同じ物理行に並ぶ別の表のレコードは、それぞれのチャンクの値だけを欄に持つ（行で結ばない）。"""
    left = {**_RECORD, "chunk_id": "l", "body": "項目ID: A01\n表示名: 左の表",
            "identifier_mentions": [_mention("A01", "record_identity")], "record_identity_values_exact": ["A01"]}
    right = {**_RECORD, "chunk_id": "r", "body": "画面ID: S01\n画面名: 右の表",
             "identifier_mentions": [_mention("S01", "record_identity")], "record_identity_values_exact": ["S01"]}
    lm, rm = es_index._chunk_structure_meta(left), es_index._chunk_structure_meta(right)
    assert lm["record_identity_values"] == ["A01"] and rm["record_identity_values"] == ["S01"]
    assert lm["labels"] == ["左の表"] and rm["labels"] == ["右の表"]


# ===== 秘匿名のファイルは索引に入れない =====

def test_sensitive_named_office_file_is_never_indexed_with_structure_fields(monkeypatch, tmp_path):
    from sherpa import corpus_docs, worlds
    from sherpa.ingest import office_md
    src, md_dir, rag_dir = tmp_path / "src", tmp_path / "md", tmp_path / "rag"
    for d in (src, md_dir, rag_dir):
        d.mkdir()
    secret_name = ".env.xlsx"
    assert text_kind.is_sensitive_doc_id(secret_name)
    for name in ("a.xlsx", secret_name):
        (src / name).write_bytes(b"")
        row = {**_RECORD, "source_rel_path": name,
               "identifier_mentions": [_mention("SECRET-" + name, "record_identity")],
               "record_identity_values_exact": ["SECRET-" + name]}
        (rag_dir / f"{name}.rag_chunks.jsonl").write_text(json.dumps(row, ensure_ascii=False) + "\n", encoding="utf-8")
        (rag_dir / f"{name}.rag.md").write_text("<!-- chunk:rc1 -->\n本文\n", encoding="utf-8")
    monkeypatch.setattr(worlds, "world_dir", lambda w: src)
    monkeypatch.setattr(worlds, "derived_md_dir", lambda w: md_dir)
    monkeypatch.setattr(worlds, "derived_rag_dir", lambda w: rag_dir)
    monkeypatch.setattr(office_md, "convertible_exts", lambda: {".xlsx"})
    monkeypatch.setattr(corpus_docs, "_office_convertible", lambda: {".xlsx"})
    sent: list = []

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            sent.append(body)
        return {}

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_req", fake_req)
    monkeypatch.setattr(es_index.importance, "resolve_for_world", lambda *a, **k: {})
    out = es_index.index_world("w")
    assert out["indexed"] == 1 and out["chunks"] == 1
    bodies = [json.loads(ln) for p in sent for i, ln in enumerate(p.strip().split("\n")) if i % 2 == 1]
    assert [b["doc_id"] for b in bodies] == ["a.xlsx"]
    assert bodies[0]["identifier_values"] == ["SECRET-a.xlsx"]
    assert all("SECRET-.env" not in json.dumps(b, ensure_ascii=False) for b in bodies)


# ===== exact =====

def test_normalize_exact():
    n = es_index.normalize_exact
    assert n("A01") == ["A01"]
    assert n(["A01", " A01 ", "", "B02", 3, None]) == ["A01", "B02"]
    assert n(None) == [] and n(5) == [] and n("   ") == []
    assert n("x" * (es_index._EXACT_VALUE_MAX_CHARS + 1)) == []
    assert len(n([f"v{i}" for i in range(100)])) == es_index._EXACT_MAX_VALUES


def _capture(monkeypatch):
    captured: dict = {}

    def fake_req(method, path, body=None, **kw):
        captured["body"] = body
        return {"hits": {"hits": []}}

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_req", fake_req)
    return captured


_EXACT_CLAUSE = {"bool": {"should": [{"terms": {"identifier_values": ["A01", "B02"]}},
                                     {"terms": {"record_identity_values": ["A01", "B02"]}}],
                          "minimum_should_match": 1}}


def test_search_exact_builds_terms_filter_without_text_match(monkeypatch):
    captured = _capture(monkeypatch)
    hits, reason = es_index.search_exact("w", ["A01", "B02"], scope_paths=["設計"], k=5, layer="code")
    assert hits == [] and reason is None
    q = captured["body"]["query"]["function_score"]["query"]["bool"]
    assert _EXACT_CLAUSE in q["filter"] and {"terms": {"scopes": ["設計"]}} in q["filter"]
    assert "must" not in q and '{"match"' not in json.dumps(captured["body"])
    assert q["should"] == [{"terms": {"record_identity_values": ["A01", "B02"]}}]   # 定義しているレコードを先に
    assert captured["body"]["size"] == 5


def test_search_exact_empty_values_is_no_query(monkeypatch):
    captured = _capture(monkeypatch)
    assert es_index.search_exact("w", []) == ([], None) and "body" not in captured


def test_search_with_exact_and_query_is_and(monkeypatch):
    captured = _capture(monkeypatch)
    es_index.search("w", "表示名", vector=False, exact=["A01", "B02"])
    q = captured["body"]["query"]["function_score"]["query"]["bool"]
    assert q["must"] == [es_index._keyword_clause("表示名")]                  # 本文（＋見出し系の欄）で順位付け
    assert q["filter"] == [_EXACT_CLAUSE]                                     # exact で絞る


def test_search_without_exact_keeps_previous_query(monkeypatch):
    captured = _capture(monkeypatch)
    es_index.search("w", "表示名", vector=False)
    assert captured["body"]["query"]["function_score"]["query"]["bool"]["filter"] == []


def test_knn_only_applies_exact_filter(monkeypatch):
    captured = _capture(monkeypatch)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: {"provider": "p", "model": "m", "dim": 2})
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "embed_provider": "p", "embed_model": "m", "dim": 2, "embed_algo": es_index.embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": es_index.embeddings.ES_VECTOR_QUANT})
    monkeypatch.setattr(es_index.embeddings, "embed", lambda texts, ec, **kw: [[0.1, 0.2]])
    es_index.search_knn_only("w", "q", exact=["A01", "B02"])
    assert captured["body"]["knn"]["filter"] == [_EXACT_CLAUSE]


# ===== es_search ツール =====

def _run(monkeypatch, args, calls):
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {"a.xlsx"})
    monkeypatch.setattr(documents, "existing_rels", lambda world, rels, **kw: set(rels) & ({"a.xlsx"}))
    hit = {"doc_id": "a.xlsx", "line": None, "text": "項目ID: A01", "ext": ".xlsx", "keyword_match": True}
    monkeypatch.setattr(es_index, "search_exact", lambda *a, **kw: (calls.append(("exact", a, kw)) or ([hit], None)))
    monkeypatch.setattr(es_index, "search", lambda *a, **kw: (calls.append(("search", a, kw)) or ([hit], None)))
    monkeypatch.setattr(es_index, "search_knn_only", lambda *a, **kw: (calls.append(("knn", a, kw)) or ([hit], None)))
    return TD.run_tool("es_search", args, "v1", None)[0]


def test_es_search_exact_only_skips_text_search(monkeypatch):
    calls: list = []
    res = _run(monkeypatch, {"exact": "A01"}, calls)
    assert [c[0] for c in calls] == ["exact"] and calls[0][1][1] == ["A01"]
    assert res["hits"] and res["mode_used"] == "keyword" and "degrade_reason" not in res


def test_es_search_exact_with_query_filters_then_ranks_by_text(monkeypatch):
    calls: list = []
    _run(monkeypatch, {"query": "表示名", "exact": ["A01", "B02"], "mode": "keyword"}, calls)
    assert calls[0][0] == "search" and calls[0][2]["exact"] == ["A01", "B02"] and calls[0][2]["vector"] is False
    calls.clear()
    _run(monkeypatch, {"query": "表示名", "exact": "A01"}, calls)
    assert calls[0][0] == "search" and calls[0][2]["exact"] == ["A01"]
    calls.clear()
    _run(monkeypatch, {"query": "表示名", "exact": "A01", "mode": "vector"}, calls)
    assert calls[0][0] == "knn" and calls[0][2]["exact"] == ["A01"]


def test_es_search_without_exact_passes_no_exact(monkeypatch):
    calls: list = []
    _run(monkeypatch, {"query": "x"}, calls)
    assert "exact" not in calls[0][2]
    calls.clear()
    _run(monkeypatch, {"exact": "   "}, calls)                              # 空白だけの exact は無指定と同じ
    assert [c[0] for c in calls] == ["search"] and "exact" not in calls[0][2]


def test_es_search_exact_hits_keep_withheld_and_not_current_filters(monkeypatch):
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {"a.xlsx", ".env.xlsx"})
    monkeypatch.setattr(documents, "existing_rels", lambda world, rels, **kw: set(rels) & ({"a.xlsx", ".env.xlsx"}))
    hits = [{"doc_id": ".env.xlsx", "text": "x", "keyword_match": True},
            {"doc_id": "gone.xlsx", "text": "x", "keyword_match": True},
            {"doc_id": "a.xlsx", "text": "x", "keyword_match": True}]
    monkeypatch.setattr(es_index, "search_exact", lambda *a, **kw: (hits, None))
    res = TD.run_tool("es_search", {"exact": "A01"}, "v1", None)[0]
    assert [h["doc_id"] for h in res["hits"]] == ["a.xlsx"]
    assert res["excluded_hits"] == {"not_current": 1, "withheld": 1}


def test_es_search_description_and_schema_mention_exact():
    from sherpa import agentic_search as A
    assert "exact" in A._PARAMS_ES_SEARCH["properties"] and A._PARAMS_ES_SEARCH["required"] == []
    assert "項目ID・画面ID・帳票ID のような鍵付きの問いは exact" in A._DESC_ES
