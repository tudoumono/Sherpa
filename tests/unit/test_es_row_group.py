"""表の行・本文のまとまり（ベクトルはチャンクごとでなく、まとまりに 1 本）の単体テスト。実 ES 不要。

設計: docs/design/rag.md「表の行・本文のまとまり」。
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


def _para(cid, kind="paragraph", parent=None, section=("節「1」",), page=None):
    return {"chunk_id": cid, "source_rel_path": "a.xlsx", "content_type": kind, "parent_id": parent,
            "section_path": list(section), "body": cid,
            "citations": [{"locator": {"part": "word/document.xml", "object_id": cid, **({"page": page} if page else {})}}]}


def _para_text(cid, n=20):
    return f"出所: 原本「a」 / 節「1」\n本文: 「{cid}{'あ' * n}」"


def test_body_chunks_of_one_section_get_one_vector_on_the_head(tmp_path):
    rows = [_para("h1", kind="heading"), _para("p1"), _para("p2")]
    anchors = {"h1": "## 節「1」\n\n出所: 原本「a」 / 節「1」\n見出し: 「1」", "p1": _para_text("p1"), "p2": _para_text("p2")}
    entries = _entries(tmp_path, rows, anchors)
    assert len(entries) == 3                                   # 全文検索の単位（1 チャンク 1 件）は変えない
    assert _heads(entries) == [("h1", 3)]
    _i, head, text, _skip = entries[0]
    assert text == head["vector_text"] and head["text"] == anchors["h1"]
    assert text.split("\n") == anchors["h1"].split("\n") + [f"本文: 「p1{'あ' * 20}」", f"本文: 「p2{'あ' * 20}」"]
    assert head["row_group"]["chunk_ids"] == ["h1", "p1", "p2"]
    assert head["row_group"]["locator"] == {"part": "word/document.xml", "object_id": "h1"}
    assert [t for _i, _b, t, skip in entries if skip] == [anchors["p1"], anchors["p2"]]
    assert all("vector_text" not in body and "row_group" not in body for _i, body, _t, skip in entries if skip)


def test_body_groups_break_at_section_region_kind_and_size(tmp_path):
    rows = [_para("a1"), _para("a2"), _para("b1", section=("節「2」",)), _para("b2", section=("節「2」",)),
            _para("c1", section=("節「2」",), parent="region-9"), _para("c2", section=("節「2」",), parent="region-9"),
            _para("img", kind="picture", section=("節「2」",), parent="region-9"),
            _para("d1", section=("節「2」",), parent="region-9"), _para("f1", kind="formula", section=("節「2」",), parent="region-9"),
            _para("f2", kind="formula", section=("節「2」",), parent="region-9")]
    entries = _entries(tmp_path, rows, {r["chunk_id"]: _para_text(r["chunk_id"]) for r in rows})
    assert _heads(entries) == [("a1", 2), ("b1", 2), ("c1", 2), ("img", 1), ("d1", 1), ("f1", 2)]

    n = 30
    rows = [_para(f"p{i}") for i in range(n)]
    entries = _entries(tmp_path, rows, {r["chunk_id"]: _para_text(r["chunk_id"], 200) for r in rows})
    heads = [(body, text) for _i, body, text, skip in entries if not skip]
    assert len(heads) > 1 and sum(b.get("row_group", {}).get("rows", 1) for b, _t in heads) == n
    assert all(len(t) <= es_index._BODY_GROUP_MAX_CHARS and len(t.encode("utf-8")) <= 8000 for b, t in heads if "row_group" in b)


def test_single_body_chunk_and_long_chunk_keep_their_own_text(tmp_path):
    long_text = _para_text("p2", 1200)
    rows = [_para("p1", section=("節「0」",)), _para("p2"), _para("p3")]
    entries = _entries(tmp_path, rows, {"p1": _para_text("p1"), "p2": long_text, "p3": _para_text("p3")})
    assert [(t, skip) for _i, _b, t, skip in entries] == [(_para_text("p1"), False), (long_text, False),
                                                       (_para_text("p3"), False)]
    assert all("vector_text" not in body for _i, body, _t, _s in entries)


def test_body_and_table_rows_are_not_mixed(tmp_path):
    rows = [_para("p1", parent="region-1", section=()), _para("p2", parent="region-1", section=()),
            _row("r1"), _row("r2"), _para("p3", parent="region-1", section=()), _para("p4", parent="region-1", section=())]
    entries = _entries(tmp_path, rows)
    assert _heads(entries) == [("p1", 2), ("r1", 2), ("p3", 2)]
    groups = {b["chunk_id"]: b["row_group"]["chunk_ids"] for _i, b, _t, skip in entries if not skip}
    assert groups == {"p1": ["p1", "p2"], "r1": ["r1", "r2"], "p3": ["p3", "p4"]}


def test_vector_hit_on_body_group_returns_group_text_and_range_keyword_hit_one_chunk(tmp_path):
    rows = [_para("p1"), _para("p2")]
    anchors = {"p1": _para_text("p1"), "p2": _para_text("p2")}
    entries = _entries(tmp_path, rows, anchors)
    res = {"hits": {"hits": [_es_hit(es_id, body) for es_id, body, _t, _s in entries]}}
    vec = es_index._parse_hits(res, vector=True)
    assert vec[0]["text"] == entries[0][1]["vector_text"] and vec[0]["chunk_id"] == "p1"
    assert vec[0]["row_group_chunk_ids"] == ["p1", "p2"]
    assert vec[0]["locator"] == {"part": "word/document.xml", "object_id": "p1"}
    kw = es_index._parse_hits(res)
    assert [h["text"] for h in kw] == [anchors["p1"], anchors["p2"]] and all("row_group_chunk_ids" not in h for h in kw)


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


def test_exact_hit_rows_are_reported_only_for_vector_hits_with_matching_rows():
    src = {**_HEAD_SRC, "row_group_identifier_values": ["A", "B"],
           "row_group_identifier_rows": [{"chunk_id": "r0", "values": ["A"]}, {"chunk_id": "r1", "values": ["B", "C"]}]}
    res = {"hits": {"hits": [_es_hit("h", src)]}}
    assert es_index._parse_hits(res, vector=True, exact=["C", "X"])[0]["row_group_exact_chunk_ids"] == ["r1"]
    assert es_index._parse_hits(res, vector=True, exact=["A", "B"])[0]["row_group_exact_chunk_ids"] == ["r0", "r1"]
    assert "row_group_exact_chunk_ids" not in es_index._parse_hits(res, vector=True, exact=["X"])[0]
    assert "row_group_exact_chunk_ids" not in es_index._parse_hits(res, vector=True)[0]
    assert "row_group_exact_chunk_ids" not in es_index._parse_hits(res, exact=["A"])[0]
    fused = es_index._fuse_hybrid({"hits": {"hits": []}}, res, 5, exact=["B"])
    assert fused[0]["row_group_exact_chunk_ids"] == ["r1"]


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
    assert props["row_group_head_chunk_id"] == {"type": "keyword"}
    assert props["row_group_identifier_values"] == {"type": "keyword"}
    assert props["row_group_identifier_rows"] == {"type": "object", "enabled": False}
    assert props["record_identity_values"] == {"type": "keyword"}


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
        "embed_algo": es_index.embeddings.EMBEDDING_INPUT_ALGORITHM_ID, "quant": es_index.embeddings.ES_VECTOR_QUANT})
    monkeypatch.setattr(es_index, "_req", fake_req)
    monkeypatch.setattr(es_index, "_embed_failed_at", {})

    def fake_fill(world, texts, ec, guard=None, stop_points=False):
        calls.append(list(texts))
        return [[0.1, 0.2] for _ in texts], 0, len(texts)

    monkeypatch.setattr(es_index, "_embed_fill", fake_fill)
    es_index.fill_missing_vectors("w")
    assert calls == [["本文 r0\n\n列: r1", "段落"]]


def _id_rows(n=3, with_ids=True):
    rows = []
    for i in range(n):
        row = _row(f"r{i}", body=f"品番: ID-{i}\n品名: 部品{i}", cell=f"A{i + 2}")
        if with_ids:
            row["identifier_mentions"] = [{"value": f"ID-{i}", "role": "record_identity"}]
        rows.append(row)
    return rows


def test_group_head_holds_member_identifiers_and_other_rows_point_to_head(tmp_path):
    entries = _entries(tmp_path, _id_rows())
    head, *others = [b for _i, b, _t, _s in entries]
    assert head["row_group_identifier_values"] == ["ID-0", "ID-1", "ID-2"]
    assert head["row_group_identifier_rows"] == [{"chunk_id": f"r{i}", "values": [f"ID-{i}"]} for i in range(3)]
    assert head["record_identity_values"] == ["ID-0"]                       # 他の行の識別子は混ぜない
    assert [b["row_group_head_chunk_id"] for b in others] == ["r0", "r0"]
    assert [b["record_identity_values"] for b in others] == [["ID-1"], ["ID-2"]]
    assert all("row_group_identifier_values" not in b and "row_group_identifier_rows" not in b for b in others)
    assert "row_group_head_chunk_id" not in head


def test_single_chunk_and_identifier_less_group_get_no_group_identifier_fields(tmp_path):
    single = [b for _i, b, _t, _s in _entries(tmp_path, _id_rows(1))]
    assert all(not k.startswith("row_group_") for k in single[0])
    plain = [b for _i, b, _t, _s in _entries(tmp_path, _id_rows(3, with_ids=False))]
    assert "row_group_identifier_values" not in plain[0] and "row_group_identifier_rows" not in plain[0]
    assert plain[1]["row_group_head_chunk_id"] == "r0"


def test_group_identifier_fields_do_not_change_embedding_text_or_cache_key(tmp_path):
    with_ids = _entries(tmp_path, _id_rows())
    without = _entries(tmp_path, _id_rows(with_ids=False))
    assert [(i, t, skip) for i, _b, t, skip in with_ids] == [(i, t, skip) for i, _b, t, skip in without]
    assert with_ids[0][1]["vector_text"] == without[0][1]["vector_text"] == with_ids[0][2]
    ec = {"provider": "p", "model": "m", "dim": 2}
    assert ([es_index._chunk_key(ec, b.get("vector_text") or b["text"]) for _i, b, _t, _s in with_ids]
            == [es_index._chunk_key(ec, b.get("vector_text") or b["text"]) for _i, b, _t, _s in without])


def _terms_match(clause, body):
    """`_exact_filter` の bool/should/terms を、ES の keyword（配列の要素のどれかが一致）と同じ意味で 1 件の body に当てる。"""
    return any(set(body.get(field) or ()) & set(values)
               for c in clause["bool"]["should"] for field, values in c["terms"].items())


def test_exact_with_vector_reaches_the_group_whichever_row_holds_the_id(tmp_path):
    """同じレコードがまとまりの先頭・中間・末尾のどこにあっても、ベクトルを持つ先頭（kNN の候補）が exact の絞り込みを通り、当たった行の chunk_id が残る。"""
    entries = _entries(tmp_path, _id_rows(5))
    embedded = [(es_id, body) for es_id, body, _t, skip in entries if not skip]    # ベクトルを持つのは先頭だけ
    assert [b["chunk_id"] for _i, b in embedded] == ["r0"]
    for pos in (0, 2, 4):
        exact = [f"ID-{pos}"]
        assert [b["chunk_id"] for _i, b in embedded if _terms_match(es_index._exact_filter(exact, row_group=True), b)] == ["r0"]
        if pos:
            assert not [b for _i, b in embedded if _terms_match(es_index._exact_filter(exact), b)]   # 絞り込みを広げないと落ちる
        hit = es_index._parse_hits({"hits": {"hits": [_es_hit(i, b) for i, b in embedded]}}, vector=True, exact=exact)[0]
        assert hit["chunk_id"] == "r0" and hit["row_group_exact_chunk_ids"] == [f"r{pos}"]
        assert hit["row_group_chunk_ids"] == [f"r{i}" for i in range(5)]


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
