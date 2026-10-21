"""表の行のまとまり（ベクトルは行ごとでなく、まとまりに 1 本）の単体テスト。実 ES 不要。

設計: docs/design/rag.md「表の行のまとまり」。
"""
from __future__ import annotations

import gzip
import json

import openpyxl

from sherpa import es_index
from sherpa.ingest import evidence_render, evidence_spike, excel_display


def _row(cid, kind="table_record", parent="region-1", body=None, cell="A1", sheet="一覧"):
    return {"chunk_id": cid, "source_rel_path": "a.xlsx", "content_type": kind, "parent_id": parent,
            "body": body if body is not None else f"列: {cid}",
            "citations": [{"locator": {"cell_range": cell}}],
            "region_context": {"sheet": sheet}}


def _entries(tmp_path, rows, anchors=None):
    jsonl, md = tmp_path / "a.xlsx.rag_chunks.jsonl", tmp_path / "a.xlsx.rag.md"
    jsonl.write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows), encoding="utf-8")
    md.write_text("".join(f"<!-- chunk:{r['chunk_id']} -->\n{(anchors or {}).get(r['chunk_id'], '本文 ' + r['chunk_id'])}\n\n"
                          for r in rows), encoding="utf-8")
    anchors, reason = es_index._load_rag_md_anchors(md)
    assert reason is None
    return list(es_index._iter_rag_chunk_entries(jsonl, "a.xlsx", anchors, {"doc_id": "a.xlsx"}))


def _heads(entries):
    return [(body["chunk_id"], body.get("row_group", {}).get("rows", 1)) for _i, body, _t, skip in entries if not skip]


def test_table_rows_get_one_vector_per_group_and_other_rows_none(tmp_path):
    n = es_index._ROW_GROUP_MAX_ROWS
    rows = [_row(f"r{i}", cell=f"A{i + 2}") for i in range(2 * n + 5)]
    entries = _entries(tmp_path, rows)
    assert len(entries) == 2 * n + 5                            # 全文検索の単位（1 行 1 件）は変えない
    assert _heads(entries) == [("r0", n), (f"r{n}", n), (f"r{2 * n}", 5)]
    _i, head, text, _skip = entries[0]
    assert text == head["vector_text"]
    assert text.split("\n") == ["一覧", "列: 列"] + [f"r{i}" for i in range(n)]   # 列名は 1 回だけ・各行は値だけ
    assert head["text"] == "本文 r0"                             # 行の本文は今のまま
    assert head["row_group"]["chunk_ids"] == [f"r{i}" for i in range(n)]
    assert head["row_group"]["locator"] == {"cell_range": f"A2:A{n + 1}", "sheet": "一覧"}
    assert all("vector_text" not in body and "row_group" not in body for _i, body, _t, skip in entries if skip)


def test_groups_break_at_region_kind_and_non_table_chunks(tmp_path):
    rows = [_row("a1"), _row("a2"), _row("b1", parent="region-2"), _row("b2", parent="region-2"),
            _row("c1", kind="coordinate_record", parent="region-2"),
            _row("p1", kind="paragraph", parent="region-2"), _row("b3", parent="region-2")]
    entries = _entries(tmp_path, rows)
    assert _heads(entries) == [("a1", 2), ("b1", 2), ("c1", 1), ("p1", 1), ("b3", 1)]


def test_wide_rows_still_group_max_rows_within_one_embedding_window(tmp_path):
    n = es_index._ROW_GROUP_MAX_ROWS
    wide = "\n".join(f"列{c:02d}: " + "あ" * 100 for c in range(40))
    rows = [_row(f"r{i}", body=wide + f"\n列{i}のExcel原値: {i}", cell=f"A{i + 2}") for i in range(n + 1)]
    entries = _entries(tmp_path, rows)
    assert _heads(entries) == [("r0", n), (f"r{n}", 1)]
    text = entries[0][2]
    assert len(text.encode("utf-8")) <= 8000
    assert text.split("\n")[1].startswith("列: 列00 | 列01")
    assert "Excel原値" not in text and "あ" * (es_index._ROW_GROUP_CELL_MAX_CHARS + 1) not in text


def test_row_values_follow_the_head_row_columns(tmp_path):
    rows = [_row("r0", body="品番: P-1\n品名: ねじ\n単価: 10"), _row("r1", body="品番: P-2\n単価: 20"),
            _row("r2", body="品番: P-3\n品名: ナット\n単価: 30\n備考: 廃番")]
    text = _entries(tmp_path, rows)[0][2]
    assert text.split("\n")[1:] == ["列: 品番 | 品名 | 単価", "P-1 | ねじ | 10", "P-2 |  | 20", "P-3 | ナット | 30 | 廃番"]


def test_coordinate_rows_use_cell_position_and_label_instead_of_column_names(tmp_path):
    rows = [_row("c1", kind="coordinate_record", body="記載値: 受入検査集計", cell="A1"),
            _row("c2", kind="coordinate_record", body="作成: 品質保証部", cell="B2"),
            _row("t1", body="品番: P-1")]
    entries = _entries(tmp_path, rows)
    assert _heads(entries) == [("c1", 2), ("t1", 1)]               # 表の行と同じ束に混ぜない
    assert entries[0][2].split("\n") == ["一覧", "A1 受入検査集計", "B2 作成: 品質保証部"]


def test_non_table_chunks_keep_their_own_text_and_vector(tmp_path):
    rows = [_row("p1", kind="paragraph"), _row("p2", kind="heading")]
    entries = _entries(tmp_path, rows)
    assert [(t, skip) for _i, _b, t, skip in entries] == [("本文 p1", False), ("本文 p2", False)]
    assert all("vector_text" not in body for _i, body, _t, _s in entries)


def test_rendered_excel_table_groups_rows_with_sheet_and_row_range(tmp_path):
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = "単価表"
    ws.append(["品番", "品名", "単価"])
    n = es_index._ROW_GROUP_MAX_ROWS
    for i in range(n + 5):
        ws.append([f"P-{1000 + i}", f"部品{i}", 100 + i])
    path = tmp_path / "price.xlsx"
    wb.save(path)
    evidence = evidence_spike.extract(path)
    excel_display.enrich_evidence(evidence, path)
    rendered = evidence_render.render(evidence, source_name=path.name)
    rag = tmp_path / "rag"
    rag.mkdir()
    jsonl = rag / "price.xlsx.rag_chunks.jsonl"
    jsonl.write_bytes(evidence_render.chunks_gzip_bytes(rendered.chunks))
    md = rag / "price.xlsx.rag.md"
    md.write_text(rendered.markdown, encoding="utf-8")
    anchors, _ = es_index._load_rag_md_anchors(md)
    seen, reason = es_index._rag_chunks_validate(jsonl, anchors, "price.xlsx")
    assert reason is None
    entries = list(es_index._iter_rag_chunk_entries(jsonl, "price.xlsx", anchors, {"doc_id": "price.xlsx"}))
    table = [(body, text, skip) for _i, body, text, skip in entries
             if body["chunk_id"] in {c["chunk_id"] for c in rendered.chunks if c["content_type"] == "table_record"}]
    assert len(table) == n + 5
    heads = [(body, text) for body, text, skip in table if not skip]
    assert [body["row_group"]["rows"] for body, _t in heads] == [n, 5]
    first, text = heads[0]
    assert first["row_group"]["locator"]["sheet"] == "単価表"
    assert first["row_group"]["locator"]["cell_range"] == f"A2:C{n + 1}"
    assert "P-1000" in text and f"P-{1000 + n - 1}" in text and f"P-{1000 + n}" not in text
    assert "単価" in text                                         # 列名がベクトルの文字に入る


def _es_hit(es_id, src):
    return {"_id": es_id, "_score": 1.0, "_source": src}


_HEAD_SRC = {"doc_id": "a.xlsx", "chunk_id": "r0", "text": "本文 r0", "locator": {"cell_range": "A2"},
             "vector_text": "本文 r0\n\n列: r1", "parent_id": "region-1",
             "row_group": {"chunk_ids": ["r0", "r1"], "rows": 2,
                           "locator": {"cell_range": "A2:C3", "sheet": "一覧"}}}


def test_vector_hit_returns_group_text_rows_and_range_but_keyword_hit_stays_one_row():
    res = {"hits": {"hits": [_es_hit("h", _HEAD_SRC)]}}
    vec = es_index._parse_hits(res, vector=True)[0]
    assert vec["text"] == "本文 r0\n\n列: r1" and vec["chunk_id"] == "r0"
    assert vec["locator"] == {"cell_range": "A2:C3", "sheet": "一覧"}
    assert vec["row_group_chunk_ids"] == ["r0", "r1"]
    kw = es_index._parse_hits(res)[0]
    assert kw["text"] == "本文 r0" and kw["locator"] == {"cell_range": "A2"} and "row_group_chunk_ids" not in kw


def test_hybrid_fusion_uses_group_only_for_hits_found_by_vector():
    other = {"doc_id": "b.docx", "chunk_id": "p1", "text": "段落"}
    bm25 = {"hits": {"hits": [_es_hit("p", other)]}}
    knn = {"hits": {"hits": [_es_hit("h", _HEAD_SRC)]}}
    hits = {h["chunk_id"]: h for h in es_index._fuse_hybrid(bm25, knn, 5)}
    assert hits["r0"]["text"] == "本文 r0\n\n列: r1" and hits["r0"]["locator"]["cell_range"] == "A2:C3"
    assert hits["p1"]["text"] == "段落"


def test_mapping_keeps_group_fields_out_of_search():
    props = es_index._mapping(1536, "kuromoji")["mappings"]["properties"]
    assert props["vector_text"] == {"type": "text", "index": False}
    assert props["row_group"] == {"type": "object", "enabled": False}


def test_fill_missing_vectors_embeds_group_text_for_group_head(monkeypatch):
    calls = []
    page = {"_scroll_id": "s", "hits": {"hits": [
        {"_id": "h", "_source": {"text": "本文 r0", "vector_text": "本文 r0\n\n列: r1"}},
        {"_id": "p", "_source": {"text": "段落"}}]}}

    def fake_req(method, path, body=None, ndjson=False, timeout=None):
        if "scroll=" in path:
            return page
        if path == "/_search/scroll" and method == "POST":
            return {"_scroll_id": "s", "hits": {"hits": []}}
        return {}

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count_missing_vectors", lambda w: 2)
    ec = {"provider": "openai", "model": "m", "dim": 2}
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda *a, **k: ec)
    monkeypatch.setattr(es_index, "_embed_system_settings_snapshot", lambda: None)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "embed_provider": "openai", "embed_model": "m", "dim": 2,
        "embed_algo": es_index.embeddings.EMBEDDING_INPUT_ALGORITHM_ID})
    monkeypatch.setattr(es_index, "_req", fake_req)
    monkeypatch.setattr(es_index, "_embed_failed_at", {})

    def fake_fill(world, texts, ec, guard=None, stop_points=False):
        calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts], 0, len(texts)

    monkeypatch.setattr(es_index, "_embed_fill", fake_fill)
    es_index.fill_missing_vectors("w")
    assert calls == [["本文 r0\n\n列: r1", "段落"]]


def test_rendered_chunks_round_trip_through_gzip(tmp_path):
    rows = [_row("r0"), _row("r1")]
    jsonl = tmp_path / "a.xlsx.rag_chunks.jsonl"
    jsonl.write_bytes(gzip.compress("".join(json.dumps(r) + "\n" for r in rows).encode("utf-8")))
    md = tmp_path / "a.xlsx.rag.md"
    md.write_text("<!-- chunk:r0 -->\nx\n\n<!-- chunk:r1 -->\ny\n", encoding="utf-8")
    anchors, _ = es_index._load_rag_md_anchors(md)
    entries = list(es_index._iter_rag_chunk_entries(jsonl, "a.xlsx", anchors, {}))
    assert [skip for *_x, skip in entries] == [False, True]


def test_cut_values_in_a_group_are_marked(tmp_path):
    """束の中で切ったセルの値は、原本の値と取り違えないよう末尾に「…」を付ける。"""
    long_value = "あ" * 40
    rows = [_row("r0", body=f"品番: P-1\n品名: {long_value}"), _row("r1", body="品番: P-2\n品名: ねじ")]
    lines = _entries(tmp_path, rows)[0][2].split("\n")
    assert lines[2] == f"P-1 | {'あ' * (es_index._ROW_GROUP_CELL_MAX_CHARS - 1)}…"
    assert lines[3] == "P-2 | ねじ"
    assert es_index._cut_bytes("い" * 200, 30).endswith("…") and len(es_index._cut_bytes("い" * 200, 30).encode()) <= 30
