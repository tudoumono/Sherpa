"""ES チャンクへの抽出来歴メタ搬送（A4）の単体テスト（実 ES 不要・モック契約テスト）。

`office_md` が書く来歴サイドカー `{md}.meta.json` を es_index が読み、チャンクに `extraction_method`／
`confidence`／`has_conflicts` を付与する。検索スコアには反映しない（表示のみ）。ソース文書（サイドカー無し）
は従来どおり（後方互換＝キーを付けない）。

RV是正1（2026-07-08・Med+Low）: アーム構成（例 OCR 有効/無効）や ES マッピング版が変わっても `content_sig`
（ソースファイルの stat のみ）は変化しないため、`needs_reindex` が見逃していた。`arms_sig`/`mapping_version`
を index の `_meta` に刻み、drift を検知して次回 sync で確実に張り直す。

常時、Office/PDF の rag_chunks.jsonl（Evidence IR 由来のレコード単位チャンク）を索引ソースに使う
経路（chunk_id/locator の搬送・rag_chunks 欠落時の40行チャンクへの縮退）も検証する（TOGGLE-RM・
2026-09-03 でグローバルな系統切替トグル `SHERPA_SEARCH_RAG_ES` は撤去済み）。
"""
from __future__ import annotations

import json

import _fresh_import as FI   # noqa: E402   # import-time 固定 env 定数の実プロセス検証
from sherpa import es_index


# ---- _provenance_meta（純関数）----

def test_provenance_meta_from_merged_sidecar(tmp_path):
    md = tmp_path / "a.docx.md"; md.write_text("x", encoding="utf-8")
    (tmp_path / "a.docx.md.meta.json").write_text(json.dumps({
        "arm": "ooxml", "method": "ooxml", "confidence": 1.0, "notes": [],
        "merge": "deterministic-v1",
        "conflicts": [{"type": "numeric_only_in_secondary", "value": "9"}],
        "arms": [{"name": "ooxml"}, {"name": "markitdown"}],
    }), encoding="utf-8")
    assert es_index._provenance_meta({"md_path": str(md)}) == {
        "extraction_method": "ooxml", "confidence": 1.0, "has_conflicts": True}


def test_provenance_meta_no_conflicts_key_omits_has_conflicts(tmp_path):
    """マージが走っていない（conflicts キー無し）文書は has_conflicts を付けない（無ければ省略）。"""
    md = tmp_path / "scan.png.md"; md.write_text("x", encoding="utf-8")
    (tmp_path / "scan.png.md.meta.json").write_text(json.dumps({
        "arm": "ocr", "method": "ocr", "confidence": 0.4, "notes": ["numeric_verified=false"]}),
        encoding="utf-8")
    assert es_index._provenance_meta({"md_path": str(md)}) == {
        "extraction_method": "ocr", "confidence": 0.4}


def test_provenance_meta_empty_conflicts_is_false(tmp_path):
    md = tmp_path / "b.docx.md"; md.write_text("x", encoding="utf-8")
    (tmp_path / "b.docx.md.meta.json").write_text(json.dumps({
        "method": "ooxml", "confidence": 1.0, "conflicts": []}), encoding="utf-8")
    out = es_index._provenance_meta({"md_path": str(md)})
    assert out["has_conflicts"] is False


def test_provenance_meta_source_doc_and_missing_sidecar_are_empty(tmp_path):
    assert es_index._provenance_meta({"md_path": None}) == {}     # ソース文書（md_path 無し）
    assert es_index._provenance_meta({}) == {}
    md = tmp_path / "c.md"; md.write_text("x", encoding="utf-8")   # サイドカー欠落＝後方互換
    assert es_index._provenance_meta({"md_path": str(md)}) == {}


def test_mapping_has_extraction_fields():
    props = es_index._mapping(None, "kuromoji")["mappings"]["properties"]
    assert props["extraction_method"]["type"] == "keyword"
    assert props["confidence"]["type"] == "float"
    assert props["has_conflicts"]["type"] == "boolean"


# ---- _parse_hits の来歴 passthrough（S2: 検索ヒットの由来表示）----

def test_parse_hits_passes_through_provenance():
    """_source に索引済みの extraction_method/confidence/has_conflicts があればヒットへ通す。"""
    res = {"hits": {"hits": [{"_score": 2.5, "_source": {
        "doc_id": "a.docx", "line": 3, "text": "本文", "ext": ".docx",
        "extraction_method": "ocr", "confidence": 0.4, "has_conflicts": True}}]}}
    hit = es_index._parse_hits(res)[0]
    assert hit["extraction_method"] == "ocr"
    assert hit["confidence"] == 0.4
    assert hit["has_conflicts"] is True


def test_parse_hits_omits_provenance_when_absent():
    """来歴を持たないチャンク（ソース文書など）はキーを付けない＝後方互換。"""
    res = {"hits": {"hits": [{"_score": 1.0, "_source": {
        "doc_id": "a.cbl", "line": 1, "text": "行", "ext": ".cbl"}}]}}
    hit = es_index._parse_hits(res)[0]
    assert hit == {"doc_id": "a.cbl", "line": 1, "text": "行", "score": 1.0, "ext": ".cbl"}


# ---- index_world の契約（チャンク body へ搬送・後方互換）----

def test_index_world_carries_provenance_to_chunks(monkeypatch, tmp_path):
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約（TOGGLE-RM 後も内部シームとして残置）
    md = tmp_path / "a.docx.md"; md.write_text("行1\n行2", encoding="utf-8")
    (tmp_path / "a.docx.md.meta.json").write_text(json.dumps({
        "method": "markitdown", "confidence": 0.6,
        "conflicts": [{"type": "numeric_only_in_secondary", "value": "9"}]}), encoding="utf-8")
    docs = [{"name": "a.docx", "md_path": str(md), "top_scope": "t"},
            {"name": "b.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text",
                        lambda w, d: (md.read_text(encoding="utf-8") if d["md_path"] else "ソース本文1\nソース本文2"))
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r["indexed"] == 2 and r["chunks"] == 2
    # ndjson: 偶数行=action / 奇数行=body。
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    a_chunks = [b for b in bodies if b["doc_id"] == "a.docx"]
    b_chunks = [b for b in bodies if b["doc_id"] == "b.md"]
    assert a_chunks and all(
        c["extraction_method"] == "markitdown" and c["confidence"] == 0.6 and c["has_conflicts"] is True
        for c in a_chunks)
    # ソース文書はサイドカー無し＝従来どおり（来歴キーを付けない・後方互換）。
    assert b_chunks and all(
        "extraction_method" not in c and "confidence" not in c and "has_conflicts" not in c
        for c in b_chunks)


def test_index_world_carries_importance_meta_to_chunks(monkeypatch, tmp_path):
    """I2（2026-09-05）: `_重要度.txt` で解決された importance/importance_reason が対象文書の
    全チャンクへ passthrough される。解決結果に無い文書はキー自体を持たない（§2 truth table）。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約
    docs = [{"name": "a.md", "md_path": None, "top_scope": "t"},
            {"name": "b.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文1\n本文2")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index.worlds, "world_dir", lambda w: tmp_path)
    res = es_index.importance.Resolution(value="高", reason="契約書", config_path="_重要度.txt", rule_line=1)
    monkeypatch.setattr(es_index.importance, "resolve_for_world", lambda w, root=None: {"a.md": res})
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r["indexed"] == 2
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    a_chunks = [b for b in bodies if b["doc_id"] == "a.md"]
    b_chunks = [b for b in bodies if b["doc_id"] == "b.md"]
    assert a_chunks and all(c["importance"] == "高" and c["importance_reason"] == "契約書" for c in a_chunks)
    assert b_chunks and all("importance" not in c and "importance_reason" not in c for c in b_chunks)


def test_index_world_no_control_file_omits_importance_key_entirely(monkeypatch, tmp_path):
    """`_重要度.txt` の無い world（`resolve_for_world` が空 dict）は、全チャンクとも `importance`
    キー自体を持たない（受け入れ条件の直接固定・チャンク本体レベル）。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    docs = [{"name": "a.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index.worlds, "world_dir", lambda w: tmp_path)
    monkeypatch.setattr(es_index.importance, "resolve_for_world", lambda w, root=None: {})
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    es_index.index_world("w")
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    assert bodies and all("importance" not in b and "importance_reason" not in b for b in bodies)


# ---- _importance_boost_query / _rerank_knn_by_importance（純関数・I2）----

def test_importance_boost_query_wraps_in_function_score_with_high_low_only():
    wrapped = es_index._importance_boost_query({"bool": {"must": [{"match": {"text": "q"}}], "filter": []}})
    fs = wrapped["function_score"]
    assert fs["query"] == {"bool": {"must": [{"match": {"text": "q"}}], "filter": []}}
    assert fs["score_mode"] == "first" and fs["boost_mode"] == "multiply"
    filters = [f["filter"]["term"]["importance"] for f in fs["functions"]]
    assert set(filters) == {"高", "低"}   # 「中」/未設定は function 自体を持たない＝一致しようがない


def test_rerank_knn_by_importance_promotes_high_and_demotes_low(monkeypatch):
    monkeypatch.setattr(es_index, "_ES_IMPORTANCE_BOOST_HIGH", 2.0)
    monkeypatch.setattr(es_index, "_ES_IMPORTANCE_BOOST_LOW", 0.5)
    hits = [{"doc_id": "a", "score": 1.0, "importance": "低"},
            {"doc_id": "b", "score": 1.0},                          # 未設定＝等倍
            {"doc_id": "c", "score": 1.0, "importance": "高"}]
    out = es_index._rerank_knn_by_importance(hits)
    assert [h["doc_id"] for h in out] == ["c", "b", "a"]   # 高(2.0)>未設定(1.0)>低(0.5)
    assert out[0]["score"] == 2.0 and out[2]["score"] == 0.5


def test_rerank_knn_by_importance_is_noop_without_any_importance_field():
    """`importance` フィールドを誰も持たない（`_重要度.txt` の無い world）なら、スコア・順序とも
    完全不変（全ヒットの乗数が一律1.0・安定ソートで元の順序を保つ）。ES は常にスコア降順で返す
    ため、入力は既にその順（降順）で与える。"""
    hits = [{"doc_id": "a", "score": 3.0}, {"doc_id": "b", "score": 2.0}, {"doc_id": "c", "score": 1.0}]
    original_scores = [h["score"] for h in hits]
    out = es_index._rerank_knn_by_importance(list(hits))
    assert [h["doc_id"] for h in out] == ["a", "b", "c"]   # ES の返り順（スコア降順）をそのまま保つ
    assert [h["score"] for h in out] == original_scores


def test_index_world_pass2_progress_resets_to_zero_and_advances_for_excluded_docs(monkeypatch, tmp_path):
    """RV是正（rv-periphery #3・2026-09-05）: Pass2 開始時に `progress(0, total_docs)` を明示的に
    1回呼ぶ（Pass1 完走時の高い done 値のまま止まって見えないように）。対象外文書（`state`=
    "unreadable"＝`chunk_iter` が None）も `docs_done`（total_docs へ収束する目盛り）を1件分
    進める——旧実装は実際に索引した文書数（`n_docs`）だけを done として使っており、対象外文書は
    数えていなかった。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    docs = [{"name": "skip.md", "md_path": None, "top_scope": "t", "state": "unreadable"},
           {"name": "a.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文1\n本文2")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_req", lambda method, path, body=None, **kw: {})
    calls = []
    r = es_index.index_world("w", progress=lambda done, total: calls.append((done, total)))
    assert r["indexed"] == 1                          # 対象外文書は「索引した」数には数えない（不変）
    assert calls[0] == (0, 2)                          # Pass2 開始の明示 0/total
    assert (1, 2) in calls                              # 対象外文書1件を見終えた時点で1件分進む
    assert calls[-1] == (2, 2)                          # 最終呼び出しは必ず done==total


def test_index_world_skips_embedding_for_branch_source(monkeypatch, tmp_path):
    """branch=="source"（登録コード＋軽量テキスト枠の汎用コード・`ingest.text_kind`）のチャンクは
    埋め込み対象から除外する——embed API 呼び出しから除外され、bulk body に `embedding` フィールドを
    持たない（BM25 のみでヒットする）。branch=="office" のチャンクは従来どおり埋め込みが付く。

    EMBED-3（doc単位ストリーミング化）: `index_world()` は Pass1 で埋め込みキャッシュへ書き込み、
    Pass2 でそこから読み直して bulk body へ付与する（2パス・disk 経由）——`_embed_cached` を直接
    差し替えて戻り値をそのまま bulk へ流し込む旧テスト流儀は、この2パス構成とは噛み合わない
    （Pass2 は `_embed_cached` を呼ばず実キャッシュを読むため）。より深い境界（`embeddings.embed`＝
    実 API 呼び出しそのもの）を差し替え、実キャッシュ機構ごと検証する。"""
    docs = [{"name": "a.cbl", "md_path": None, "top_scope": "t", "branch": "source"},
            {"name": "b.md", "md_path": None, "top_scope": "t", "branch": "office"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text",
                        lambda w, d: "コード本文" if d["branch"] == "source" else "資料本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index.worlds, "derived_dir", lambda w: tmp_path / w)   # 埋め込みキャッシュを tmp へ
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "p", "model": "m", "dim": 3})
    embed_calls = []

    def fake_embed(texts, ec, **kw):
        embed_calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert embed_calls == [["資料本文"]]           # source 側のテキストは埋め込み呼び出しに一切渡さない
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    src_chunks = [b for b in bodies if b["doc_id"] == "a.cbl"]
    doc_chunks = [b for b in bodies if b["doc_id"] == "b.md"]
    assert src_chunks and all("embedding" not in c for c in src_chunks)
    assert doc_chunks and all(c.get("embedding") == [0.1, 0.2, 0.3] for c in doc_chunks)
    assert r["vectors"] is True                    # office 側だけでもベクトルは付いた


def test_index_world_skips_embedding_for_light_text_document_label_too(monkeypatch, tmp_path):
    """軽量テキスト枠の資料側（`text_kind.DOCUMENT_DOCTYPE_LABEL`・csv/tsv/log/rtf
    等・branch=="office"）も embed 対象から除外する——除外条件が branch=="source" だけだと、
    「ベクトル・グラフ・LLM は一切通さない・取り込みコスト増ゼロ」というユーザー裁定と矛盾する。
    通常の `.md`/`.txt`（doctype が
    text_kind ラベルでない）は従来どおり embed 対象のまま。

    EMBED-3: 実キャッシュ機構（Pass1書き込み→Pass2読み直し）ごと検証する（上のテストと同じ理由で
    `embeddings.embed` を差し替える）。"""
    from sherpa.ingest import text_kind
    docs = [{"name": "data.csv", "md_path": None, "top_scope": "t", "branch": "office",
            "doctype": text_kind.DOCUMENT_DOCTYPE_LABEL},
            {"name": "note.md", "md_path": None, "top_scope": "t", "branch": "office", "doctype": "設計書"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text",
                        lambda w, d: "csv本文" if d["name"] == "data.csv" else "設計書本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index.worlds, "derived_dir", lambda w: tmp_path / w)   # 埋め込みキャッシュを tmp へ
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "p", "model": "m", "dim": 3})
    embed_calls = []

    def fake_embed(texts, ec, **kw):
        embed_calls.append(list(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    es_index.index_world("w")
    assert embed_calls == [["設計書本文"]]          # csv側のテキストは埋め込み呼び出しに一切渡さない
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    csv_chunks = [b for b in bodies if b["doc_id"] == "data.csv"]
    md_chunks = [b for b in bodies if b["doc_id"] == "note.md"]
    assert csv_chunks and all("embedding" not in c for c in csv_chunks)
    assert md_chunks and all(c.get("embedding") == [0.1, 0.2, 0.3] for c in md_chunks)


def test_index_world_excludes_stage2_light_text_docs_entirely(monkeypatch):
    """軽量テキスト枠の**第2段**（未知拡張子・拡張子なしの内容推定）文書は
    ES 索引の対象外——第1段（拡張子マップで判定できる）は通常どおり索引する。「検索可能集合＝
    引用可能集合」の契約を守るため（第2段は read_around/引用検証/`/ext/v1/doc` が元々拒否する・
    `text_kind.py` モジュールdocstring参照）。"""
    from sherpa.ingest import text_kind
    docs = [
        # 第1段（.py は CODE_EXT にある）＝索引対象のまま。
        {"name": "app.py", "md_path": None, "top_scope": "t", "branch": "source",
         "doctype": text_kind.CODE_DOCTYPE_LABEL},
        # 第2段（拡張子なし・extが空文字列で classify_ext が None を返す）＝索引対象外。
        {"name": "README", "md_path": None, "top_scope": "t", "branch": "office",
         "doctype": text_kind.DOCUMENT_DOCTYPE_LABEL},
    ]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    read_calls = []

    def fake_read(w, d):
        read_calls.append(d["name"])
        return "本文"

    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", fake_read)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert read_calls == ["app.py"]                   # README（第2段）は本文すら読まない
    assert r["indexed"] == 1 and r["chunks"] == 1
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    assert {b["doc_id"] for b in bodies} == {"app.py"}


def test_pure_code_world_unchanged_sync_does_not_loop_reindex_when_embeddings_configured(monkeypatch):
    """埋め込み設定済み＋全チャンク branch=="source"（純コード world）でも、
    `index_world()` 後の emeta に埋め込み素性（provider/model/dim）が書かれ、内容不変な
    次回 `needs_reindex()` は False（unchanged sync が no-op になる）。書かれないと
    want（`ec` 由来）vs have（`None`）が恒久的に不一致になり、無変更 world でも毎 sync で
    無限に full reindex が走り続けていた（実測）。"""
    world = "w"
    content_sig = "c1"
    docs = [{"name": "a.cbl", "md_path": None, "top_scope": "t", "branch": "source"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "コード本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    ec = {"provider": "openai", "model": "text-embedding-3-small", "dim": 1536}
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)

    def fake_embed_cached(world, texts, ec_):
        assert texts == []                            # 純コード world＝embed 対象0件
        return (None, 0, 0)

    monkeypatch.setattr(es_index, "_embed_cached", fake_embed_cached)
    store: dict = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        store.clear()
        store.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    # content_sig は全バッチ成功後の後書き（`_confirm_content_sig`）。簡易ストアへ往復させる。
    monkeypatch.setattr(es_index, "_confirm_content_sig",
                        lambda w, sig: store.__setitem__("content_sig", sig) if sig else None)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})

    r = es_index.index_world(world, content_sig=content_sig)
    assert r.get("error") is None
    assert store.get("embed_provider") == "openai" and store.get("dim") == 1536

    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(store))
    monkeypatch.setattr(es_index, "count", lambda w: 1)
    assert es_index.needs_reindex(world, content_sig) is False   # 変化無し＝unchanged syncはno-op


def test_true_embed_failure_does_not_record_features_and_retries(monkeypatch):
    """H-2 の逆側の固定: 真の埋め込み失敗（embed 対象あり・vectors 取得失敗）では emeta に
    埋め込み素性を**書かず**、`needs_reindex()` が True のまま＝次回 sync で再試行される。
    素性を書いてしまうと失敗が「この構成で最新」と誤記され、再索引が静かに止まる。"""
    world = "w"
    content_sig = "c1"
    docs = [{"name": "設計.md", "md_path": None, "top_scope": "t", "branch": "office"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "資料本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    ec = {"provider": "openai", "model": "text-embedding-3-small", "dim": 1536}
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: ec)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)

    def fake_embed_cached(world, texts, ec_):
        assert texts, "embed 対象があるケースを検証する"
        return (None, 0, 0)                           # 取得失敗（vectors なし）

    monkeypatch.setattr(es_index, "_embed_cached", fake_embed_cached)
    store: dict = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        store.clear()
        store.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})

    es_index.index_world(world, content_sig=content_sig)
    assert "embed_provider" not in store              # 失敗を「最新」と誤記しない

    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(store))
    monkeypatch.setattr(es_index, "count", lambda w: 1)
    assert es_index.needs_reindex(world, content_sig) is True   # 次回 sync で再試行


# ---- EMBED-3: doc単位ストリーミング化の契約（メモリ有界性・一様degrade・再開性） ----

def _setup_embed3_world(monkeypatch, tmp_path, n_docs: int, flush_chunks: int):
    """`n_docs` 件（各1文書=1チャンク・本文は distinct）を用意し、埋め込み設定済み・ES 呼び出しは
    no-op でモックする（EMBED-3 の契約テスト群の共通土台）。"""
    monkeypatch.setattr(es_index, "_EMBED_FLUSH_CHUNKS", flush_chunks)
    docs = [{"name": f"d{i}.md", "md_path": None, "top_scope": "t", "branch": "office"} for i in range(n_docs)]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: f"本文{d['name']}")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index.worlds, "derived_dir", lambda w: tmp_path / w)   # 実キャッシュを tmp へ
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "p", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)


def test_index_world_embed_calls_bounded_by_flush_size_not_world_total(monkeypatch, tmp_path):
    """メモリ有界性の代理指標（EMBED-3）: `embeddings.embed()` は1回の呼び出しで
    `_EMBED_FLUSH_CHUNKS` 件を超えるテキストを受け取らない——world 全体のチャンク数がそれより
    多くても、一度に保持されるテキスト/ベクトル量は world サイズ比例ではなくフラッシュ単位に
    有界（旧実装は world 全体の texts/vectors を1回で `_embed_cached` へ渡していた＝この境界が
    無かった）。"""
    _setup_embed3_world(monkeypatch, tmp_path, n_docs=23, flush_chunks=5)
    call_sizes = []

    def fake_embed(texts, ec, **kw):
        call_sizes.append(len(texts))
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})
    r = es_index.index_world("w")
    assert r.get("error") is None
    assert call_sizes and max(call_sizes) <= 5          # 一度に embed へ渡すのは常にフラッシュ単位以下
    assert sum(call_sizes) == 23                          # 23件の distinct 本文は全部 embed された（取りこぼし無し）


def test_index_world_embed_partial_failure_degrades_uniformly_no_doc_mixing(monkeypatch, tmp_path):
    """embed が world の一部（後続フラッシュバッチ＝後半の doc 群）で失敗した場合、既に成功した
    バッチの doc だけがベクトル付きになる非一様な索引を作らない——world 全体を BM25 のみへ縮退する
    （EMBED-3・2パス構成: Pass1 が world 全体の embed 成否を確定してから Pass2 が bulk を組む
    ため、doc の処理順序によって前半だけベクトル付きになる早期実行は起きない）。"""
    _setup_embed3_world(monkeypatch, tmp_path, n_docs=8, flush_chunks=3)
    call_n = {"n": 0}

    def fake_embed(texts, ec, **kw):
        call_n["n"] += 1
        if call_n["n"] == 2:               # 2番目のフラッシュバッチ（後半の doc 群）だけ失敗させる
            return None
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    captured: dict = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured.setdefault("bulk", []).append(body)
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r.get("error") is None                        # クラウド未選択の実失敗は graceful に BM25-only へ降格
    assert r["vectors"] is False
    bodies = [json.loads(ln) for payload in captured.get("bulk", [])
              for i, ln in enumerate(payload.strip().split("\n")) if i % 2 == 1]
    assert len(bodies) == 8                               # 8文書は全て索引される（消えない）
    assert all("embedding" not in b for b in bodies)      # 前半バッチ（成功済み）の doc も含め誰もベクトルを持たない


def test_index_world_resumes_embed_cache_across_failed_and_retried_runs(monkeypatch, tmp_path):
    """embed が一部失敗した回でも、成功したフラッシュバッチ分はキャッシュへ残る——次回の
    `index_world()` 実行はそのぶん再 embed を省ける（EMBED-2 由来の再開性が2パス構成でも保たれる）。"""
    _setup_embed3_world(monkeypatch, tmp_path, n_docs=8, flush_chunks=3)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})
    embed_texts_seen: list = []
    fail_second = {"on": True}

    def fake_embed(texts, ec, **kw):
        embed_texts_seen.append(list(texts))
        if fail_second["on"] and len(embed_texts_seen) == 2:
            return None
        return [[0.1, 0.2, 0.3] for _ in texts]

    monkeypatch.setattr(es_index.embeddings, "embed", fake_embed)
    r1 = es_index.index_world("w")
    assert r1["vectors"] is False                          # 1回目: 一部失敗→BM25 縮退

    fail_second["on"] = False                              # 2回目: 今度は全部成功させる
    embed_texts_seen.clear()
    r2 = es_index.index_world("w")
    assert r2["vectors"] is True
    total_texts_2nd_run = sum(len(t) for t in embed_texts_seen)
    assert total_texts_2nd_run < 8    # 1回目の最初のフラッシュバッチ分はキャッシュヒットで再 embed されない


def _boom_get_system_settings():
    raise AssertionError("SHERPA_DISABLE_EMBED 有効時に system_settings を読んではいけない")


def test_index_world_skips_system_settings_read_when_disable_embed_active(monkeypatch):
    """RV3（FBK-1・境界回帰#2・2026-09-01）: `SHERPA_DISABLE_EMBED=1` の間は `index_world()` が
    `store.get_system_settings()` を一切読まない——`embeddings.cfg()`/`cloud_selected_but_
    unavailable()` は kill-switch を最優先で見て system_settings に触れず即座に返す契約だが、
    以前は呼び出し元（`index_world`）が両者へ渡すスナップショットを無条件に読んでおり、
    kill-switch が有効でも設定 DB 障害でこの読み取り自体が例外を出していた。"""
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    monkeypatch.setattr("sherpa.store.get_system_settings", _boom_get_system_settings)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: [])
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})
    r = es_index.index_world("w")
    assert r.get("error") is None


def test_search_skips_system_settings_read_when_disable_embed_active(monkeypatch):
    """RV3（境界回帰#2）: `search()`（`vector=True`）も kill-switch 有効時は system_settings を
    読まない（BM25 のみへ確実に degrade する）。"""
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    monkeypatch.setattr("sherpa.store.get_system_settings", _boom_get_system_settings)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": []}})
    hits, reason = es_index.search("w", "query")
    assert reason is None
    assert hits == []


def test_search_knn_only_skips_system_settings_read_when_disable_embed_active(monkeypatch):
    """RV3（境界回帰#2）: `search_knn_only()` も kill-switch 有効時は system_settings を読まず、
    既存の `embedding_not_configured` 早期 return（`cloud_selected_but_unavailable()` も
    system_settings 無しで偽を返す）に落ちる。"""
    monkeypatch.setenv("SHERPA_DISABLE_EMBED", "1")
    monkeypatch.setattr("sherpa.store.get_system_settings", _boom_get_system_settings)
    monkeypatch.setattr(es_index, "available", lambda: True)
    hits, reason = es_index.search_knn_only("w", "query")
    assert hits == [] and reason == "embedding_not_configured"


def test_index_world_fails_before_delete_when_cloud_selected_but_unavailable(monkeypatch):
    """RV1（FBK-1・境界回帰#3・2026-09-01）: A7 で明示選択したクラウドの埋め込みが解決できない
    （`embeddings.cfg()` が None・`cloud_selected_but_unavailable()` が真）場合、`index_world()`
    は既存索引を削除する前に打ち切る——`delete_world` が一度も呼ばれないこと・`error` に
    `embedding_cloud_unavailable` が返ることの両方を固定する（通常の埋め込み未設定＝BM25 のみは
    従来どおり graceful に進む・上の `test_index_world_carries_provenance_to_chunks` 系が回帰確認）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: True)
    r = es_index.index_world("w")
    assert r == {"available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}
    assert delete_calls == []


def test_index_world_fails_before_delete_when_real_embed_call_fails(monkeypatch):
    """RV2（FBK-1・境界回帰#1・2026-09-01）: `cfg()` は解決できた（選択済みクラウドの鍵は有効）が、
    実際の embed API 呼び出し（`_embed_cached`）が実通信で失敗した場合も、`index_world()` は
    既存索引を削除する前に打ち切る——RV1 の保護は `cfg() is None` のときしか働かなかった
    （`cfg()` 成功後に delete_world 済みのまま embed が失敗すると、BM25-only 索引を「成功」として
    書き戻していた）。文書列挙（`corpus_docs.world_documents`）は delete より前に完了しており、
    `_embed_cached` が呼ばれた時点で実際に texts が存在することを固定する。"""
    docs = [{"name": "a.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文1\n本文2")
    monkeypatch.setattr(es_index, "available", lambda: True)
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "openai", "model": "m", "dim": 1536})
    embed_cached_calls = []

    def _fake_embed_cached(world, texts, ec):
        embed_cached_calls.append(texts)
        return None, 0, 0   # 実通信失敗（`need and not new`）＝BM25 のみへ降格するはずの生の戻り値

    monkeypatch.setattr(es_index, "_embed_cached", _fake_embed_cached)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: True)
    r = es_index.index_world("w")
    assert r == {"available": True, "indexed": 0, "chunks": 0, "error": "embedding_cloud_unavailable"}
    assert delete_calls == []
    assert embed_cached_calls and embed_cached_calls[0], "texts が空のまま _embed_cached が呼ばれた（列挙が先に終わっていない）"


def test_index_world_still_graceful_when_embed_fails_and_cloud_never_selected(monkeypatch):
    """RV2: クラウドを一度も選んでいない構成（`cloud_selected_but_unavailable()` が偽）では、
    embed 実通信失敗も従来どおり BM25-only で索引を作り直す（graceful・delete_world は呼ばれる）。"""
    docs = [{"name": "a.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文1\n本文2")
    monkeypatch.setattr(es_index, "available", lambda: True)
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "ollama", "model": "m", "dim": 768})
    monkeypatch.setattr(es_index, "_embed_cached", lambda world, texts, ec: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    r = es_index.index_world("w")
    assert r.get("error") is None
    assert delete_calls == ["w"]


def test_search_knn_only_distinguishes_cloud_unavailable_from_not_configured(monkeypatch):
    """RV1（FBK-1・境界回帰#3）: `search_knn_only()` は埋め込み未解決の理由を区別する——
    A7 で明示選択したクラウドが解決できない場合は `embedding_cloud_unavailable`、クラウドを
    一度も選んでいない通常の未設定は従来どおり `embedding_not_configured`。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: True)
    hits, reason = es_index.search_knn_only("w", "query")
    assert hits == [] and reason == "embedding_cloud_unavailable"
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)
    hits, reason = es_index.search_knn_only("w", "query")
    assert hits == [] and reason == "embedding_not_configured"


def _spy_system_settings_snapshot(monkeypatch, sentinel):
    """`store.get_system_settings()` を1回だけ読ませ、以後の呼び出しは全て同じ `sentinel` を
    返す（RV2・境界回帰#3・2026-09-01: `cfg()`/`cloud_selected_but_unavailable()` が同じ
    snapshot を受け取ることの確認に使う）。

    `SHERPA_DISABLE_EMBED`（他テストファイル・例えば `test_agentic_search.py` が import 時に
    `os.environ.setdefault` で立てプロセス全体に残る kill-switch）を明示的に外す——RV3 の
    kill-switch 早期 return（`_embed_system_settings_snapshot`）が有効なままだと、ここで
    読ませたいはずの system_settings が一度も読まれず `read_calls == []` になってしまう。
    """
    monkeypatch.delenv("SHERPA_DISABLE_EMBED", raising=False)
    read_calls = []

    def _spy():
        read_calls.append(1)
        return sentinel

    monkeypatch.setattr("sherpa.store.get_system_settings", _spy)
    return read_calls


def test_search_knn_only_shares_one_system_settings_snapshot(monkeypatch):
    """RV2（境界回帰#3）: `search_knn_only()` は `store.get_system_settings()` を1回だけ読み、
    `embeddings.cfg()` と `cloud_selected_but_unavailable()` の両方へ同じオブジェクトを渡す
    （別々に読むと、その間の admin 更新で判定が食い違いうる）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    sentinel = {"cloud_provider": "openai"}
    read_calls = _spy_system_settings_snapshot(monkeypatch, sentinel)
    seen = []

    def _fake_cfg(settings=None, *, system_settings=None):
        seen.append(("cfg", system_settings))
        return None

    def _fake_unavailable(system_settings=None):
        seen.append(("unavailable", system_settings))
        return True

    monkeypatch.setattr(es_index.embeddings, "cfg", _fake_cfg)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", _fake_unavailable)
    hits, reason = es_index.search_knn_only("w", "query")
    assert reason == "embedding_cloud_unavailable"
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    assert seen and all(snap is sentinel for _, snap in seen), \
        "cfg()/cloud_selected_but_unavailable() が異なる system_settings を受け取った"


def test_index_world_shares_one_system_settings_snapshot(monkeypatch):
    """RV2（境界回帰#3）: `index_world()` も同じスナップショットを `cfg()`（1回目の判定）と
    `cloud_selected_but_unavailable()`（1回目・embed 実失敗時の2回目）の両方へ渡す。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    sentinel = {"cloud_provider": "openai"}
    read_calls = _spy_system_settings_snapshot(monkeypatch, sentinel)
    seen = []

    def _fake_cfg(settings=None, *, system_settings=None):
        seen.append(("cfg", system_settings))
        return None

    def _fake_unavailable(system_settings=None):
        seen.append(("unavailable", system_settings))
        return True

    monkeypatch.setattr(es_index.embeddings, "cfg", _fake_cfg)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", _fake_unavailable)
    r = es_index.index_world("w")
    assert r["error"] == "embedding_cloud_unavailable"
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
def test_search_hybrid_failure_with_bm25_success_is_hybrid_query_failed(monkeypatch):
    """RV3（FBK-1・境界回帰#1・2026-09-01）: hybrid クエリ自体が失敗（次元不一致/未ベクトル索引等）
    しても BM25 が成功すれば hits は空にならない——この場合の reason は `es_query_failed`
    （hits が空になる BM25 自体の失敗）ではなく `hybrid_query_failed`（BM25 は返っている）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index.embeddings, "cfg",
                        lambda settings=None, **kw: {"provider": "openai", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "embed", lambda texts, ec, **kw: [[0.1, 0.2, 0.3]])
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", lambda *a, **k: False)

    def _fake_req(method, path, body=None, **kw):
        if method == "GET" and path.endswith("/_mapping"):
            return {"idx": {"mappings": {"_meta": {"embed_provider": "openai", "embed_model": "m", "dim": 3}}}}
        if method == "POST" and path.endswith("/_search"):
            # RV是正（rv-i2-importance #4）: hybrid（vector=True・knn 併用）は `bool.should` に
            # match/knn の2節を並べる形（`es_index.search` 参照）——BM25-only 本文（`bool.must`
            # 1節のみ）と区別する検出条件を新しい形に合わせる。
            bool_q = (body or {}).get("query", {}).get("function_score", {}).get("query", {}).get("bool", {})
            if "should" in bool_q:
                raise RuntimeError("hybrid query failed (dimension mismatch etc.)")
            return {"hits": {"hits": [{"_source": {"doc_id": "a.md", "line": 1, "text": "hit"}, "_score": 1.0}]}}
        return {}

    monkeypatch.setattr(es_index, "_req", _fake_req)
    hits, reason = es_index.search("w", "query")
    assert reason == "hybrid_query_failed"
    assert hits and hits[0]["doc_id"] == "a.md"   # BM25 の hits はそのまま返る（空にならない）


def test_degrade_vocabulary_includes_hybrid_query_failed():
    """RV3: `hybrid_query_failed` は `search_service.DEGRADE_REASONS` にも登録済み
    （増やすときは両方直す契約・`es_index.search()` docstring 参照）。"""
    from sherpa import search_service
    assert "hybrid_query_failed" in search_service.DEGRADE_REASONS


def test_search_shares_one_system_settings_snapshot(monkeypatch):
    """RV2（境界回帰#3）: `search()`（`vector=True`）も `cfg()` と `cloud_selected_but_
    unavailable()` を同じスナップショットで呼ぶ。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {"hits": {"hits": []}})
    sentinel = {"cloud_provider": "openai"}
    read_calls = _spy_system_settings_snapshot(monkeypatch, sentinel)
    seen = []

    def _fake_cfg(settings=None, *, system_settings=None):
        seen.append(("cfg", system_settings))
        return None

    def _fake_unavailable(system_settings=None):
        seen.append(("unavailable", system_settings))
        return True

    monkeypatch.setattr(es_index.embeddings, "cfg", _fake_cfg)
    monkeypatch.setattr(es_index.embeddings, "cloud_selected_but_unavailable", _fake_unavailable)
    hits, reason = es_index.search("w", "query")
    assert reason == "embedding_cloud_unavailable"
    assert read_calls == [1], f"store.get_system_settings() が {len(read_calls)} 回呼ばれた（期待は1回）"
    assert seen and all(snap is sentinel for _, snap in seen), \
        "cfg()/cloud_selected_but_unavailable() が異なる system_settings を受け取った"


def test_index_world_excludes_unreadable_documents_even_if_reread_succeeds(monkeypatch, tmp_path):
    """`state="unreadable"` の文書は分類（`corpus_docs` 側の判定）を唯一のゲートにする——
    索引用の再読が（たまたま）成功しても索引しない。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約（TOGGLE-RM 後も内部シームとして残置）
    docs = [{"name": "bad.cbl", "md_path": None, "top_scope": "t", "state": "unreadable"},
            {"name": "ok.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文")   # 再読は成功する設定
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r["indexed"] == 1                              # unreadable は数えない
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    assert {b["doc_id"] for b in bodies} == {"ok.md"}      # bad.cbl は索引されない


def test_index_world_records_mapping_version_and_arms_sig(monkeypatch, tmp_path):
    """index_world は _meta に mapping_version と arms_sig（アーム構成署名）を刻む（RV Med/Low）。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約（TOGGLE-RM 後も内部シームとして残置）
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-xyz")
    captured = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        captured.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    es_index.index_world("w")
    assert captured["mapping_version"] == es_index.ES_MAPPING_VERSION
    assert captured["arms_sig"] == "sig-xyz"


# ---- アナライザ構成署名（analyzer_config_sig）も World 署名と同様に ES 設定署名の材料 ----

def test_index_world_records_analyzer_config_sig(monkeypatch):
    """index_world は _meta にコード解析アナライザの有効構成署名（`_analyzer_config_sig()`）を刻む
    ——新規アナライザ追加・CODE-1b の有効/無効・並び替えのいずれかで台帳/Neo4j/ES の `branch` が
    旧構成のまま残らないよう、通常の署名不一致→reindex 経路に乗せる。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約（TOGGLE-RM 後も内部シームとして残置）
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-xyz")
    captured = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        captured.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    es_index.index_world("w")
    assert captured["analyzer_config_sig"] == "acfg-xyz"


def test_needs_reindex_reacts_to_analyzer_config_drift(monkeypatch):
    """アナライザ構成が変わった（新規アナライザ追加・有効順変更）＝ content_sig 不変でも
    analyzer_config_sig 不一致で reindex 要。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "legacy", "arms_sig": "sig-A", "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is False           # 全一致＝不要
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-B")   # アナライザ構成が変わった
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_old_index_without_analyzer_config_sig_field_forces_one_time_reindex(monkeypatch):
    """本 RV 以前の索引（analyzer_config_sig フィールド自体が無い）も1回だけ reindex される
    （メタに無い＝None と現在値の不一致で自然に収束）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "legacy", "arms_sig": "sig-A"})   # 旧索引（フィールド無し）
    assert es_index.needs_reindex("w", "c1") is True


def test_analyzer_config_sig_survives_json_round_trip():
    """`_analyzer_config_sig()` は `config_signature()`（タプル）を `repr()` で文字列化して返す
    ——ES `_meta` は JSON 往復（保存時 dump／読み出し時 load）を経るため、タプルのまま保存すると
    tuple→list になり素の比較が常に不一致になる（対照実験として実際にタプルを往復させて示す）。
    文字列（`repr()` 済み）は JSON 往復を経ても値が保持されることを、実 `analyzer_registry`（mock
    しない・実際の cobol/copybook/jcl 構成）で確認する。"""
    raw_tuple = es_index.analyzer_registry.config_signature()
    round_tripped_tuple = json.loads(json.dumps(raw_tuple))
    assert round_tripped_tuple != raw_tuple    # 対照実験: 生のタプルは JSON 往復で型が変わり不一致になる

    sig = es_index._analyzer_config_sig()
    assert isinstance(sig, str)
    round_tripped_sig = json.loads(json.dumps(sig))
    assert round_tripped_sig == sig            # 文字列は JSON 往復後も同じ値のまま
    assert round_tripped_sig == es_index._analyzer_config_sig()   # 再計算した値とも一致する


def test_stale_mapping_v4_index_triggers_reindex_once_then_stays_stable(monkeypatch):
    """mapping v4（`analyzer_config_sig` フィールド自体が無い旧世代の索引）から始めても、初回の
    sync 相当の呼び出しで1回だけ reindex され、以降はソース内容・アナライザ構成のいずれも変わら
    なければ reindex されない（RV11 是正の移行シナリオを実 `_analyzer_config_sig()` で一気通貫
    固定する・`_index_meta`/`ensure_index` を連動させた簡易ストアで write→read を再現する）。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約（TOGGLE-RM 後も内部シームとして残置）
    world = "w"
    content_sig = "c1"   # ソースファイル自体は最初から最後まで不変のまま
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    # `_analyzer_config_sig` は実関数のまま（mock しない・実際の repr 値で往復させる）。

    store = {"content_sig": content_sig, "mapping_version": "4", "arms_sig": "sig-A",
             "search_chunk_mode": "legacy"}   # 旧索引（v4・analyzer_config_sig フィールド自体が無い）
    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(store))
    # `content_sig` は全バッチ成功後に `_confirm_content_sig` が Put Mapping で後書きする
    # （途中でプロセスが落ちても中途半端な索引が居座らないため）。簡易ストアにもその往復を再現する。
    monkeypatch.setattr(es_index, "_confirm_content_sig",
                        lambda w, sig: store.__setitem__("content_sig", sig) if sig else None)
    ensure_calls = {"n": 0}

    def fake_ensure_index(w, dim=None, emeta=None):
        ensure_calls["n"] += 1
        store.clear()
        store.update(emeta or {})
        return True
    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)

    assert es_index.needs_reindex(world, content_sig) is True    # 初回: mapping v4 ズレで reindex 要
    es_index.index_world(world, content_sig=content_sig)
    assert ensure_calls["n"] == 1

    assert es_index.needs_reindex(world, content_sig) is False   # 2回目: 全一致＝不要（次回は張り直さない）
    assert ensure_calls["n"] == 1                                 # sync 側は needs_reindex=False なら
    # index_world を呼ばない契約——ここでは呼んでいないこと自体（1のまま）で確認する。


# ---- _arms_config_sig（fail-safe）----

def test_arms_config_sig_failsafe_on_error(monkeypatch):
    """office_md._current_arms_sig() が例外を投げても None（fail-safe・needs_reindex を巻き込まない）。"""
    from sherpa.ingest import office_md

    def _boom():
        raise RuntimeError("構成読み取り失敗")

    monkeypatch.setattr(office_md, "_current_arms_sig", _boom)
    assert es_index._arms_config_sig() is None


# ---- _human_md_config_sig（RAG_ES の ON/OFF に関わらず常に評価・pending はセンチネル）----

def test_human_md_config_sig_evaluated_even_when_rag_es_enabled(monkeypatch):
    """RAG_ES 有効時でも `{rel}.md` の版を無条件に無視しない——`rag_chunks` が無効/劣化した
    文書は legacy 縮退で `{rel}.md` を読むため（`index_world` の `rag_result is None`
    フォールバック参照）。world "w" は未登録＝pending の評価対象が無く現行版を返す。"""
    from sherpa.ingest import office_md

    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vX")
    assert es_index._human_md_config_sig("w") == "human-md-vX"


def test_human_md_config_sig_returns_value_when_not_pending(monkeypatch):
    """pending（render/ES いずれの drift）が無い world では `office_md._current_human_md_sig()`
    の値を返す（world "w" は未登録＝`world_dir` が None を返すため、pending チェックは素通りする）。"""
    from sherpa.ingest import office_md

    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vX")
    assert es_index._human_md_config_sig("w") == "human-md-vX"


def test_human_md_config_sig_failsafe_on_error(monkeypatch):
    """office_md._current_human_md_sig() が例外を投げても None（fail-safe・needs_reindex を巻き込まない・
    pending センチネルとは区別する）。"""
    from sherpa.ingest import office_md

    def _boom():
        raise RuntimeError("構成読み取り失敗")

    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    monkeypatch.setattr(office_md, "_current_human_md_sig", _boom)
    assert es_index._human_md_config_sig("w") is None


def test_human_md_config_sig_fail_closed_while_world_has_pending_human_md_drift(monkeypatch, tmp_path):
    """world がまだ human_md の再生成待ち（`human_md_sig_drift` True）の間は、レンダラ版が
    現行のままでも pending センチネルを返し続ける（fail-closed）。部分失敗のまま ES の meta へ
    現行版を確定させてしまうと、後で当該 rel の再生成が成功しても reindex の契機を失うため。
    pending は `None` ではなく明示のセンチネルにする（meta のフィールド欠落と区別するため）。
    render 側の drift が消えても、ES 側のマーカー（`human_md_es_sig_drift`）が別途 True の間は
    まだ pending のまま——両方揃って初めて現行版へ進む。
    （`Path.exists` の広域 monkeypatch は pytest 自身を壊しうるため使わず、実ディレクトリ
    `tmp_path` を使う）。"""
    from sherpa import worlds as worlds_mod
    from sherpa.ingest import office_md

    wd = tmp_path / "world"; wd.mkdir()
    dmd = tmp_path / "derived"; dmd.mkdir()
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: dmd)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vX")
    monkeypatch.setattr(office_md, "human_md_sig_drift", lambda wd, dmd: True)
    monkeypatch.setattr(office_md, "human_md_es_sig_drift", lambda dmd: True)
    assert es_index._human_md_config_sig("w") == es_index._HUMAN_MD_PENDING_SENTINEL

    monkeypatch.setattr(office_md, "human_md_sig_drift", lambda wd, dmd: False)
    # render 側は直ったが ES 側のマーカーはまだ True＝依然 pending。
    assert es_index._human_md_config_sig("w") == es_index._HUMAN_MD_PENDING_SENTINEL

    monkeypatch.setattr(office_md, "human_md_es_sig_drift", lambda dmd: False)
    assert es_index._human_md_config_sig("w") == "human-md-vX"


def test_needs_reindex_reacts_to_human_md_drift_regardless_of_rag_es_setting(monkeypatch):
    """human_md 版のズレは RAG_ES の ON/OFF に関わらず reindex 要（RAG_ES=ON でも
    `rag_chunks` 無効時は legacy `{rel}.md` へ縮退するため）。"""
    from sherpa.ingest import office_md

    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: None)   # この寸法も孤立させる
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vNEW")

    # legacy（RAG_ES OFF）: 索引済みメタは旧版のまま＝ズレを検知して reindex 要。
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "legacy", "arms_sig": "sig-A", "human_md_sig": "human-md-vOLD"})
    assert es_index.needs_reindex("w", "c1") is True

    # rag（RAG_ES 有効）でも human_md のレンダラ版だけを変えれば同様に reindex 要
    # （RAG_ES=ON でも rag_chunks 無効時の legacy 縮退経路に human_md 版が影響しうるため
    # 無条件には無視できない）。
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "rag")
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "rag", "arms_sig": "sig-A", "human_md_sig": "human-md-vOLD"})
    assert es_index.needs_reindex("w", "c1") is True

    # 一致していれば（RAG_ES の設定に関わらず）reindex 不要。
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "rag", "arms_sig": "sig-A", "human_md_sig": "human-md-vNEW"})
    assert es_index.needs_reindex("w", "c1") is False


# ---- needs_reindex（RV Med/Low: アーム構成/マッピング版ズレも drift 対象）----

def test_needs_reindex_reacts_to_arms_config_drift(monkeypatch):
    """アーム構成が変わった（例: OCR 無効化）＝ content_sig 不変でも arms_sig 不一致で reindex 要。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")   # アナライザ構成は不変に固定
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")   # 索引ソース方針は不変に固定
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "legacy", "arms_sig": "sig-A",
        "analyzer_config_sig": "acfg-A"})   # chunk_lines は既定時は書かない
    assert es_index.needs_reindex("w", "c1") is False           # 全一致＝不要
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-B")   # アーム構成が変わった
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_reacts_to_mapping_version_drift(monkeypatch):
    """ES マッピング版が古い（このデプロイでチャンクメタ項目を追加）＝ reindex 要（1回きりの移行）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": "1", "arms_sig": "sig-A"})   # 旧版（"2" 追加前）
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_old_index_without_arms_sig_field_forces_one_time_reindex(monkeypatch):
    """本 RV 以前に作られた索引（arms_sig/mapping_version フィールド自体が無い）も1回だけ reindex される。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {"content_sig": "c1"})   # 旧索引（フィールド無し）
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_reacts_to_mapping_version_bump_from_2_to_3(monkeypatch):
    """rag_chunks 接続によるマッピング版 2→3 の移行を具体的な旧値で固定する。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": "2", "arms_sig": "sig-A"})
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_reacts_to_mapping_version_bump_from_3_to_4(monkeypatch):
    """重要度機能のスキーマ版導入に伴うマッピング版 3→4 の移行を具体的な旧値で固定する
    （既存データ移行の代替＝world 署名だけでなく ES 側も版不一致で再索引される）。

    `search_chunk_mode`/`human_md_sig`/`analyzer_config_sig` も固定し、それらの次元のズレが
    偶然 True を生んで mapping_version 単独のズレを検知できていないことを見逃さない
    （固定なしだと、他の次元がたまたま不一致になっても同じ assert が通ってしまい、
    テストの意図（3→4 のズレ検知）を検証できていない）。
    """
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": "3", "search_chunk_mode": "legacy",
        "arms_sig": "sig-A", "human_md_sig": None, "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is True
    # 対照実験: mapping_version だけを現行値へ戻すと、他の全次元は変わらず一致のままなので不要になる
    # （＝上の True が本当に mapping_version 単独のズレで生じていたことの確認）。
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION, "search_chunk_mode": "legacy",
        "arms_sig": "sig-A", "human_md_sig": None, "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is False


def test_needs_reindex_reacts_to_mapping_version_bump_from_5_to_6(monkeypatch):
    """B1: 隣接キー（previous_chunk_id 等）追加によるマッピング版 5→6 の移行を具体的な旧値で
    固定する（1回だけ再索引され、現行版へ収束すれば不要に戻る）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": "5", "search_chunk_mode": "legacy",
        "arms_sig": "sig-A", "human_md_sig": None, "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is True
    # 対照実験: mapping_version だけを現行値へ戻すと不要になる（1回で収束する契約の確認）。
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION, "search_chunk_mode": "legacy",
        "arms_sig": "sig-A", "human_md_sig": None, "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is False


def test_needs_reindex_reacts_to_mapping_version_bump_from_6_to_7(monkeypatch):
    """I2（2026-09-05）: `importance`/`importance_reason` フィールド追加によるマッピング版 6→7 の
    移行を具体的な旧値で固定する（1回だけ再索引され、現行版へ収束すれば不要に戻る）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": "6", "search_chunk_mode": "legacy",
        "arms_sig": "sig-A", "human_md_sig": None, "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is True
    # 対照実験: mapping_version だけを現行値へ戻すと不要になる（1回で収束する契約の確認）。
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION, "search_chunk_mode": "legacy",
        "arms_sig": "sig-A", "human_md_sig": None, "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is False


# ---- needs_reindex: search_chunk_mode の反転検知（High是正・旧世代 legacy 索引からの
# 一度きりの移行を取りこぼさない安全弁。TOGGLE-RM・2026-09-03 でグローバル切替トグルは撤去済みだが、
# `rag_es_enabled()` は内部シームとして残るため直接差し替えて検証する）----

def _pin_needs_reindex_signals(monkeypatch, *, arms_sig="sig-A"):
    """content_sig/mapping_version/arms_sig/human_md_sig/analyzer_config_sig を固定し、
    search_chunk_mode 系のズレだけを孤立させる。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: arms_sig)
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")


def test_needs_reindex_reacts_to_search_chunk_mode_flip_off_to_on(monkeypatch):
    """legacy で索引した後 rag へ切り替えると、次回 sync が索引ソース方針のズレを検知する
    （TOGGLE-RM 後は env での切替はできないが、`rag_es_enabled` 自体は内部シームとして残る
    ＝旧世代 legacy 索引からの一度きりの移行安全弁を直接差し替えて検証する）。"""
    _pin_needs_reindex_signals(monkeypatch)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "arms_sig": "sig-A", "analyzer_config_sig": "acfg-A",
        "search_chunk_mode": "legacy"})   # legacy で索引済み（chunk_lines は既定時省略）
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    assert es_index.needs_reindex("w", "c1") is False          # 現在も legacy＝一致・不要
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_reacts_to_search_chunk_mode_flip_on_to_off(monkeypatch):
    """rag で索引した後 legacy へ戻しても（逆方向も）同様にズレを検知する。"""
    _pin_needs_reindex_signals(monkeypatch)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "arms_sig": "sig-A", "analyzer_config_sig": "acfg-A",
        "search_chunk_mode": "rag"})   # rag で索引済み（chunk_lines は既定時省略）
    assert es_index.needs_reindex("w", "c1") is False          # 現在も rag（既定）＝一致・不要
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_old_index_without_search_chunk_mode_field_forces_one_time_reindex(monkeypatch):
    """本 RV 以前の索引（search_chunk_mode フィールド自体が無い）も1回だけ reindex される
    （メタに無い＝None と現在値の不一致で自然に収束＝新しい特別扱いは不要）。"""
    _pin_needs_reindex_signals(monkeypatch)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION, "arms_sig": "sig-A",
        "analyzer_config_sig": "acfg-A"})
    assert es_index.needs_reindex("w", "c1") is True


def test_confirm_human_md_meta_updates_field_and_converges_needs_reindex(monkeypatch, tmp_path):
    """`confirm_human_md_meta` は既存の `_meta`（`content_sig` 等）を保持したまま
    `human_md_sig` フィールドだけを現行署名へ書き直す。書き直す前は meta の human_md_sig
    （`ensure_index` が bulk 実行前に書いた `None`）と現行版が不一致のため `needs_reindex` は
    True（マーカーだけ確定して meta を放置すると収束しない）。書き直した後は一致して False
    になる——マーカー確定後の次回 sync が unchanged（再索引なし）で収束することの確認。"""
    from sherpa import worlds as worlds_mod
    from sherpa.ingest import office_md

    wd = tmp_path / "world"; wd.mkdir()
    dmd = tmp_path / "derived"; dmd.mkdir()
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: dmd)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vX")
    monkeypatch.setattr(office_md, "human_md_sig_drift", lambda wd, dmd: False)
    monkeypatch.setattr(office_md, "human_md_es_sig_drift", lambda dmd: False)   # マーカーは既に確定済み
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")

    meta = {"content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
            "search_chunk_mode": "legacy", "arms_sig": "sig-A", "human_md_sig": None,
            "analyzer_config_sig": "acfg-A", "world_id": "w"}
    monkeypatch.setattr(es_index, "_index_meta", lambda w: dict(meta))

    assert es_index.needs_reindex("w", "c1") is True     # human_md_sig=None ≠ "human-md-vX"（収束前）

    put_calls: list[tuple] = []

    def _fake_req(method, path, body=None, **kw):
        if method == "PUT" and path.endswith("/_mapping"):
            put_calls.append((path, body))
            meta.update(body["_meta"])
        return {}
    monkeypatch.setattr(es_index, "_req", _fake_req)

    assert es_index.confirm_human_md_meta("w") is True
    assert len(put_calls) == 1
    assert put_calls[0][1]["_meta"]["human_md_sig"] == "human-md-vX"
    assert put_calls[0][1]["_meta"]["content_sig"] == "c1"     # 既存フィールドは保持（丸ごと置換しない）

    assert es_index.needs_reindex("w", "c1") is False    # 収束した（次回 sync は unchanged）


def test_confirm_human_md_meta_refuses_while_still_pending(monkeypatch, tmp_path):
    """render 側/ES マーカーのどちらかがまだ pending の間は `confirm_human_md_meta` 自体が
    False を返し、meta を書き換えない（呼び出し元の前提が崩れている防御）。"""
    from sherpa import worlds as worlds_mod
    from sherpa.ingest import office_md

    wd = tmp_path / "world"; wd.mkdir()
    dmd = tmp_path / "derived"; dmd.mkdir()
    monkeypatch.setattr(worlds_mod, "world_dir", lambda w: wd)
    monkeypatch.setattr(worlds_mod, "derived_md_dir", lambda w: dmd)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    monkeypatch.setattr(office_md, "_current_human_md_sig", lambda: "human-md-vX")
    monkeypatch.setattr(office_md, "human_md_sig_drift", lambda wd, dmd: True)   # まだ pending
    put_calls: list[tuple] = []
    monkeypatch.setattr(es_index, "_req", lambda *a, **kw: put_calls.append(a) or {})
    assert es_index.confirm_human_md_meta("w") is False
    assert put_calls == []


def test_index_world_records_search_chunk_mode(monkeypatch):
    """index_world は現在の索引ソース方針（rag/legacy）を _meta に刻む（フラグ反転検知の書き込み側）。"""
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, include_rag=False: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        captured.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    es_index.index_world("w")
    assert captured["search_chunk_mode"] == "legacy"
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: True)
    es_index.index_world("w")
    assert captured["search_chunk_mode"] == "rag"


# ---- rag_chunks 接続（ES 索引ソースの切替）----

def test_rag_es_enabled_always_true_no_env_toggle():
    """TOGGLE-RM（2026-09-03）: グローバルな系統切替トグルは撤去済み・常時 True（env に一切
    左右されない）。"""
    assert es_index.rag_es_enabled() is True


def test_mapping_has_rag_chunk_fields():
    props = es_index._mapping(None, "kuromoji")["mappings"]["properties"]
    assert props["chunk_id"]["type"] == "keyword"
    # locator は形式ごとに shape が変わる（Excel: sheet/cell_range、PPTX: slide/bbox 等）ため
    # dynamic mapping の型衝突を避けて enabled:false（_source には残るが索引/絞り込み対象にしない）。
    assert props["locator"] == {"type": "object", "enabled": False}


def test_mapping_has_context_neighbor_fields():
    """B1: 隣接キー（v6）は全て keyword（section_path は配列だが ES keyword は配列値をそのまま
    受け付けるため型は同じ）。"""
    props = es_index._mapping(None, "kuromoji")["mappings"]["properties"]
    for key in ("previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path"):
        assert props[key]["type"] == "keyword"


# ---- _rag_chunk_source_exts（Med是正: sidecar の帰属検証・拡張子ゲート）----

def test_rag_chunk_source_exts_limited_to_office_pdf():
    exts = es_index._rag_chunk_source_exts()
    assert ".docx" in exts and ".pdf" in exts and ".xlsx" in exts and ".pptx" in exts
    # ソース/テキスト文書は対象外＝同名 sidecar があっても拾わない（stale/別文書の取り違え防止）。
    assert ".cbl" not in exts and ".jcl" not in exts and ".txt" not in exts and ".md" not in exts


# ---- _rag_chunk_es_id（High是正: chunk_id 衝突の無警告上書き防止）----

def test_rag_chunk_es_id_is_namespaced_by_doc_id():
    """同じ chunk_id でも doc_id が違えば別の ES _id になる（複製文書/stale sidecar の無警告上書き防止）。
    同一入力からは常に同じ _id（決定的）。"""
    id_a = es_index._rag_chunk_es_id("a.docx", "dup-chunk-id")
    id_b = es_index._rag_chunk_es_id("b.docx", "dup-chunk-id")
    assert id_a != id_b
    assert id_a == es_index._rag_chunk_es_id("a.docx", "dup-chunk-id")


# ---- _safe_rag_chunks_path（Low是正: symlink 拒否・derived root 配下の確認）----

def test_safe_rag_chunks_path_absent_is_silent(tmp_path):
    """存在しない（旧 world の未再sync等）は通常の縮退であり報告対象ではない。"""
    derived = tmp_path / "derived"; derived.mkdir()
    path, reason = es_index._safe_rag_chunks_path(derived, "a.docx")
    assert path is None and reason is None


def test_safe_rag_chunks_path_accepts_regular_file(tmp_path):
    derived = tmp_path / "derived"; derived.mkdir()
    p = derived / "a.docx.rag_chunks.jsonl"; p.write_text("x", encoding="utf-8")
    path, reason = es_index._safe_rag_chunks_path(derived, "a.docx")
    assert path == p.resolve() and reason is None


def test_safe_rag_chunks_path_rejects_symlink(tmp_path):
    """symlink は resolve 前に拒否する（resolve 後は is_symlink() が常に False になるため先に見る）。"""
    derived = tmp_path / "derived"; derived.mkdir()
    outside = tmp_path / "outside.jsonl"
    outside.write_text(json.dumps({"chunk_id": "c1",
                                   "source_rel_path": "a.docx"}) + "\n", encoding="utf-8")
    (derived / "a.docx.rag_chunks.jsonl").symlink_to(outside)
    path, reason = es_index._safe_rag_chunks_path(derived, "a.docx")
    assert path is None and reason == "symlink_rejected"


# ---- _safe_rag_md_path（D1: rag.md正本の安全な読み取りパス・_safe_rag_chunks_pathと同型）----

def test_safe_rag_md_path_absent_is_silent(tmp_path):
    derived = tmp_path / "derived"; derived.mkdir()
    path, reason = es_index._safe_rag_md_path(derived, "a.docx")
    assert path is None and reason is None


def test_safe_rag_md_path_accepts_regular_file(tmp_path):
    derived = tmp_path / "derived"; derived.mkdir()
    p = derived / "a.docx.rag.md"; p.write_text("x", encoding="utf-8")
    path, reason = es_index._safe_rag_md_path(derived, "a.docx")
    assert path == p.resolve() and reason is None


def test_safe_rag_md_path_rejects_symlink(tmp_path):
    derived = tmp_path / "derived"; derived.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("<!-- chunk:c1 -->\nx\n", encoding="utf-8")
    (derived / "a.docx.rag.md").symlink_to(outside)
    path, reason = es_index._safe_rag_md_path(derived, "a.docx")
    assert path is None and reason == "symlink_rejected"


# ---- _parse_rag_md_chunks（D1: rag.mdのアンカー分割・純関数）----

def test_parse_rag_md_chunks_splits_between_anchors():
    md = "見出し\n\n<!-- chunk:c1 -->\n本文1\n\n<!-- chunk:c2 -->\n本文2\n"
    bodies, reason = es_index._parse_rag_md_chunks(md)
    assert reason is None
    assert bodies == {"c1": "本文1", "c2": "本文2"}


def test_parse_rag_md_chunks_no_anchors_is_legacy_format():
    """アンカーが1つも無い（D1以前の旧形式rag.md）は呼び出し側が安全に縮退できる理由を返す。"""
    bodies, reason = es_index._parse_rag_md_chunks("旧形式の本文だけ\n")
    assert bodies == {} and reason == "rag_md_no_anchors"


def test_parse_rag_md_chunks_duplicate_anchor_invalidates():
    md = "<!-- chunk:c1 -->\nA\n<!-- chunk:c1 -->\nB\n"
    bodies, reason = es_index._parse_rag_md_chunks(md)
    assert bodies == {} and reason == "rag_md_duplicate_anchor"


# ---- _chunk_locator（純関数・代表 locator の選出）----

def test_chunk_locator_takes_first_citation():
    chunk = {"citations": [{"locator": {"sheet": "S1", "cell_range": "A1"}},
                            {"locator": {"sheet": "S1", "cell_range": "A2"}}]}
    assert es_index._chunk_locator(chunk) == {"sheet": "S1", "cell_range": "A1"}


def test_chunk_locator_absent_when_no_usable_citation():
    assert es_index._chunk_locator({}) is None
    assert es_index._chunk_locator({"citations": []}) is None
    assert es_index._chunk_locator({"citations": [{"evidence_id": "e1"}]}) is None   # locator キー無し


# ---- _validate_rag_chunks（rag_chunks.jsonl + rag.md → ES bulk 用の (ids, bodies, texts, reason)）----
# D1（`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md`§8.1）: 索引本文は jsonl の
# `search_text` ではなく rag.md のアンカー間本文から取る。

_ROW1 = {"chunk_id": "rc1", "source_rel_path": "a.docx",
         "citations": [{"locator": {"sheet": "S1"}}]}


def _write_jsonl(path, *rows):
    path.write_text("".join(json.dumps(r) + "\n" for r in rows), encoding="utf-8")


def _write_rag_md(path, *chunks):
    """`chunks`＝`(chunk_id, body)` のタプル列からD1アンカー付きrag.mdを組み立てる。"""
    lines = []
    for chunk_id, body in chunks:
        lines.append(f"<!-- chunk:{chunk_id} -->")
        lines.append(body)
        lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


def test_validate_rag_chunks_builds_entries_without_line(tmp_path):
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, _ROW1)
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(
        p, md, "a.docx", {"doc_id": "a.docx", "ext": ".docx"})
    assert reason is None
    assert ids == [es_index._rag_chunk_es_id("a.docx", "rc1")] and texts == ["本文"]
    assert bodies == [{"doc_id": "a.docx", "ext": ".docx", "chunk_id": "rc1", "text": "本文",
                       "locator": {"sheet": "S1"}}]
    assert "line" not in bodies[0]


def test_validate_rag_chunks_omits_locator_when_absent(tmp_path):
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "a.docx"})
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx"})
    assert reason is None and "locator" not in bodies[0]


# ---- _chunk_context_meta（B1: 隣接キー・純関数）----

def test_chunk_context_meta_present_fields():
    chunk = {"previous_chunk_id": "a", "next_chunk_id": "b", "parent_id": "p",
             "logical_record_id": "l", "section_path": ["見出し1", "見出し2"]}
    assert es_index._chunk_context_meta(chunk) == chunk


def test_chunk_context_meta_absent_is_empty():
    assert es_index._chunk_context_meta({}) == {}


def test_chunk_context_meta_ignores_wrong_types():
    """型不正（生成側の不具合等）は locator と同じ流儀でキーを立てないだけ（無効化しない）。"""
    chunk = {"previous_chunk_id": 123, "parent_id": "", "section_path": "not-a-list"}
    assert es_index._chunk_context_meta(chunk) == {}


def test_chunk_context_meta_rejects_non_string_section_path_items():
    assert es_index._chunk_context_meta({"section_path": ["ok", 5]}) == {}
    assert es_index._chunk_context_meta({"section_path": []}) == {}   # 空配列も立てない


# ---- _validate_rag_chunks: B1 隣接キーの搬送（欠落は縮退させない）----

def test_validate_rag_chunks_includes_context_meta_when_present(tmp_path):
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "a.docx",
                     "previous_chunk_id": "rc0", "next_chunk_id": "rc2", "parent_id": "region1",
                     "logical_record_id": "lr1", "section_path": ["見出し1"]})
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx"})
    assert reason is None
    assert bodies[0]["previous_chunk_id"] == "rc0"
    assert bodies[0]["next_chunk_id"] == "rc2"
    assert bodies[0]["parent_id"] == "region1"
    assert bodies[0]["logical_record_id"] == "lr1"
    assert bodies[0]["section_path"] == ["見出し1"]


def test_validate_rag_chunks_omits_context_meta_when_absent_does_not_degrade(tmp_path):
    """B1 受け入れ条件: 隣接キーが無い rag_chunks でも縮退せずに索引できる（`reason is None`）。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "a.docx"})
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx"})
    assert reason is None
    for key in ("previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path"):
        assert key not in bodies[0]


def test_validate_rag_chunks_missing_field_invalidates_whole_file(tmp_path):
    """Med是正（部分破損の黙認防止）: 必須フィールド欠落は1行だけ捨てず、ファイル全体を無効にする。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"source_rel_path": "a.docx"})   # chunk_id 無し
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "missing_chunk_id" and ids == [] and bodies == [] and texts == []


def test_validate_rag_chunks_missing_anchor_invalidates_whole_file(tmp_path):
    """D1 受け入れ条件（1:1検証の破れ・アンカー欠落）: jsonl の chunk_id に対応するアンカーが
    rag.md に無ければファイル全体を無効にする。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "a.docx"})
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc-other", "本文"))          # rc1 のアンカーが無い
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "rag_md_anchor_missing"


def test_validate_rag_chunks_surplus_anchor_invalidates_whole_file(tmp_path):
    """D1 受け入れ条件（1:1検証の破れ・アンカー余剰）: rag.md に jsonl のどの chunk_id とも
    対応しないアンカーが余っていればファイル全体を無効にする。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "a.docx"})
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"), ("rc-surplus", "余剰"))
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "rag_md_anchor_surplus"


def test_validate_rag_chunks_duplicate_anchor_invalidates_whole_file(tmp_path):
    """D1 受け入れ条件（1:1検証の破れ・アンカー重複）: rag.md 内で同じ chunk_id のアンカーが
    複数あればファイル全体を無効にする。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "a.docx"})
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文A"), ("rc1", "本文B"))
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "rag_md_duplicate_anchor"


def test_validate_rag_chunks_legacy_rag_md_without_anchors_degrades_safely(tmp_path):
    """D1 受け入れ条件（旧形式の安全な縮退）: 旧形式（v1alpha8以下・アンカー無し）の rag.md を
    新コードが読むと、jsonl の中身（旧`search_text`があってもなくても）に関わらず安全に無効化される。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "search_text": "旧形式の本文", "source_rel_path": "a.docx"})
    md = tmp_path / "a.docx.rag.md"
    md.write_text("# AI検索用文書\n\n旧形式のrag.md本文（アンカー無し）\n", encoding="utf-8")
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "rag_md_no_anchors"


def test_validate_rag_chunks_invalid_json_line_invalidates_whole_file(tmp_path):
    """1行目は正常でも、2行目が壊れていれば1行目も含めファイル全体を無効にする（部分採用しない）。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    p.write_text(json.dumps(_ROW1) + "\nnot json\n", encoding="utf-8")
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "invalid_json" and ids == [] and bodies == []


def test_validate_rag_chunks_row_not_object_invalidates_whole_file(tmp_path):
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, ["not", "a", "dict"])
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "row_not_object"


def test_validate_rag_chunks_missing_jsonl_file_is_stat_or_read_failed(tmp_path):
    """jsonl 自体が無い場合は _safe_rag_chunks_path 側で弾く前提の関数なので、直接呼ぶと
    stat_failed/read_failed（rag.md 側は正常に揃っている前提でjsonl側の欠落だけを見る）。"""
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(
        tmp_path / "missing.rag_chunks.jsonl", md, "a.docx", {})
    assert reason in {"stat_failed", "read_failed"} and ids == []


def test_validate_rag_chunks_missing_md_path_is_rag_md_missing(tmp_path):
    """D1: jsonl はあるのに対になる rag.md が無い不整合な派生状態は `rag_md_missing` で縮退する。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, _ROW1)
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, None, "a.docx", {})
    assert reason == "rag_md_missing" and ids == []


def test_validate_rag_chunks_rejects_source_rel_path_mismatch(tmp_path):
    """Med是正（sidecar の帰属検証）: source_rel_path が呼び出し元の rel と食い違う
    （stale/別文書の sidecar 取り違え）とファイル全体を無効にする。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "b.docx"})   # 別文書
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    ids, bodies, texts, reason = es_index._validate_rag_chunks(p, md, "a.docx", {"doc_id": "a.docx"})
    assert reason == "source_rel_path_mismatch" and ids == [] and bodies == [] and texts == []


def test_validate_rag_chunks_duplicate_chunk_id_invalidates_whole_file(tmp_path):
    """Med是正（部分破損の黙認防止）: 同一ファイル内の chunk_id 重複はファイル全体を無効にする。"""
    row = {"chunk_id": "dup", "source_rel_path": "a.docx"}
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, row, row)
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("dup", "本文"))
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "duplicate_chunk_id"


def test_validate_rag_chunks_file_too_large_invalidates(monkeypatch, tmp_path):
    """Med是正（無制限メモリ）: jsonl のファイルサイズ上限超過は無効化する（明示定数
    `_RAG_CHUNKS_FILE_CAP_BYTES`・rag.md と共通）。rag.md 側の判定（md.stat が先に走る）を
    通り抜けたうえで jsonl 側だけが超過する構成にし、`file_too_large`（`rag_md_too_large`ではない）
    分岐を単独で確認する。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, _ROW1)
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    cap = md.stat().st_size + 1          # rag.mdはこのcapを超えない・jsonlは超える構成にする
    assert p.stat().st_size > cap
    monkeypatch.setattr(es_index, "_RAG_CHUNKS_FILE_CAP_BYTES", cap)
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "file_too_large"


def test_validate_rag_md_too_large_invalidates(monkeypatch, tmp_path):
    """D1: rag.md 側のサイズ上限は jsonl と同じ `_RAG_CHUNKS_FILE_CAP_BYTES` を流用する
    （対になるサイドカーでサイズの桁が大きく異ならない前提・新しい env は増やさない）。"""
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, _ROW1)
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))
    monkeypatch.setattr(es_index, "_RAG_CHUNKS_FILE_CAP_BYTES", 4)
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "rag_md_too_large"


def test_validate_rag_chunks_too_many_rows_invalidates(monkeypatch, tmp_path):
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, *[{"chunk_id": f"rc{i}", "source_rel_path": "a.docx"} for i in range(5)])
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, *[(f"rc{i}", "x") for i in range(5)])
    monkeypatch.setattr(es_index, "_RAG_CHUNKS_MAX_ROWS", 2)
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "too_many_rows"


def test_validate_rag_chunks_search_text_too_long_invalidates(monkeypatch, tmp_path):
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, {"chunk_id": "rc1", "source_rel_path": "a.docx"})
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "x" * 100))
    monkeypatch.setattr(es_index, "_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS", 10)
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason == "search_text_too_long"


def test_validate_rag_chunks_does_not_slurp_jsonl_via_read_text(monkeypatch, tmp_path):
    """Med是正（無制限メモリ）: jsonl 本体は `Path.read_text()` を使わず逐次 `open()` で読むことを
    固定する（rag.md 側は D1 でアンカー分割のため意図的に一括読み込みする＝対象外）。"""
    from pathlib import Path as PathCls
    p = tmp_path / "a.docx.rag_chunks.jsonl"
    _write_jsonl(p, _ROW1)
    md = tmp_path / "a.docx.rag.md"
    _write_rag_md(md, ("rc1", "本文"))

    original_read_text = PathCls.read_text

    def _guarded(self, *a, **kw):
        if self == p:
            raise AssertionError("jsonl側でread_textはもう呼ばれない実装であるべき")
        return original_read_text(self, *a, **kw)

    monkeypatch.setattr(PathCls, "read_text", _guarded)
    _, _, _, reason = es_index._validate_rag_chunks(p, md, "a.docx", {})
    assert reason is None


# ---- index_world: ON/OFF の索引ソース選択（本体の契約）----

def test_index_world_off_calls_world_documents_without_include_rag_kwarg(monkeypatch):
    """legacy モード（`rag_es_enabled` を直接差し替えて模擬）: world_documents は従来どおり
    位置引数のみで呼ぶ（include_rag 未使用）。derived_md_dir も呼ばれない＝legacy は追加の
    I/O も新フィールドも一切発生しない完全一致。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    calls = []
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: (calls.append(w) or []))

    def _boom(w):
        raise AssertionError("OFF では derived_md_dir を呼んではいけない")

    monkeypatch.setattr(es_index.worlds, "derived_md_dir", _boom)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    r = es_index.index_world("w")
    assert calls == ["w"] and r["indexed"] == 0


def test_index_world_rag_chunks_preferred_legacy_fallback_source_unchanged(monkeypatch, tmp_path):
    """ON: rag_chunks を持つ Office はレコード単位チャンクを索引し、legacy md は使わない。
    rag_chunks の無い Office は40行チャンクへ縮退（消えない）。ソース文書は拡張子ゲートで対象外
    （同名の sidecar が万一存在しても無視＝Med是正）。全て正常系なので rag_degraded は 0。
    索引本文は D1 のとおり rag.md のアンカー間本文から取る（jsonl はもう search_text を持たない）。"""
    derived = tmp_path / "derived" / "md"
    derived.mkdir(parents=True)
    (derived / "a.docx.rag_chunks.jsonl").write_text(json.dumps({
        "chunk_id": "rc1", "source_rel_path": "a.docx",
        "citations": [{"locator": {"sheet": "Sheet1", "cell_range": "B12"}}],
    }) + "\n", encoding="utf-8")
    _write_rag_md(derived / "a.docx.rag.md", ("rc1", "Excel の B12 セルは 1000 円"))
    # ソース文書に同名 sidecar が万一存在しても、拡張子ゲート（Office/PDF のみ）で読まれない。
    (derived / "c.cbl.rag_chunks.jsonl").write_text(json.dumps({
        "chunk_id": "should-not-be-used", "source_rel_path": "c.cbl",
    }) + "\n", encoding="utf-8")
    _write_rag_md(derived / "c.cbl.rag.md", ("should-not-be-used", "混入注意"))
    docs = [
        {"name": "a.docx", "md_path": str(derived / "a.docx.md"), "top_scope": "t"},
        {"name": "b.xlsx", "md_path": str(derived / "b.xlsx.md"), "top_scope": "t"},   # rag_chunks 無し
        {"name": "c.cbl", "md_path": None, "top_scope": "t"},                          # ソース文書
    ]
    calls = []

    def fake_world_documents(w, include_rag=False):
        calls.append(include_rag)
        return docs

    def fake_doc_text(w, d):
        return {"a.docx": "legacy body（使われないはず）",
                "b.xlsx": "legacy only body\n",
                "c.cbl": "ソース本文1\nソース本文2"}[d["name"]]

    monkeypatch.setattr(es_index.corpus_docs, "world_documents", fake_world_documents)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: derived)
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: derived)  # §8.1 三階層（テストfixtureは同一dirを共用）
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", fake_doc_text)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)

    r = es_index.index_world("w")
    assert r["indexed"] == 3
    assert r["rag_degraded"] == 0 and "rag_degraded_docs" not in r   # 全て正常系＝0件は省略ではなくキー自体は出す
    assert calls == [True]   # include_rag=True で列挙（既存の opt-in を活用）
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    a_chunks = [b for b in bodies if b["doc_id"] == "a.docx"]
    b_chunks = [b for b in bodies if b["doc_id"] == "b.xlsx"]
    c_chunks = [b for b in bodies if b["doc_id"] == "c.cbl"]
    assert len(a_chunks) == 1
    assert a_chunks[0]["chunk_id"] == "rc1"
    assert a_chunks[0]["text"] == "Excel の B12 セルは 1000 円"
    assert a_chunks[0]["locator"] == {"sheet": "Sheet1", "cell_range": "B12"}
    assert "line" not in a_chunks[0]
    assert len(b_chunks) == 1 and "chunk_id" not in b_chunks[0] and b_chunks[0]["line"] == 1
    assert "legacy only body" in b_chunks[0]["text"]
    # ソース文書は拡張子ゲートで rag_chunks を一切見ない＝混入 sidecar の内容が絶対に紛れ込まない。
    assert c_chunks and all("chunk_id" not in c for c in c_chunks)
    assert not any("混入注意" in c.get("text", "") for c in bodies)


def test_index_world_reports_rag_degraded_on_corruption_and_falls_back(monkeypatch, tmp_path):
    """ON: rag_chunks が壊れている文書は legacy 40行チャンクへ縮退し、rag_degraded に計数される
    （Med是正: 部分破損の黙認防止・戻り値で報告）。rag.md 自体は正常（アンカー付き）だが、jsonl 側が
    壊れている＝jsonl 内部の検証（`invalid_json`）まで到達することを確認する。"""
    derived = tmp_path / "derived" / "md"
    derived.mkdir(parents=True)
    (derived / "a.docx.rag_chunks.jsonl").write_text("not json\n", encoding="utf-8")
    _write_rag_md(derived / "a.docx.rag.md", ("rc1", "本文"))
    docs = [{"name": "a.docx", "md_path": str(derived / "a.docx.md"), "top_scope": "t"}]

    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, include_rag=False: docs)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: derived)
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: derived)  # §8.1 三階層（テストfixtureは同一dirを共用）
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "legacy fallback body\n")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)

    r = es_index.index_world("w")
    assert r["indexed"] == 1                              # 文書自体は索引から消えない
    assert r["rag_degraded"] == 1
    assert r["rag_degraded_docs"] == [{"doc": "a.docx", "reason": "invalid_json"}]
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    assert bodies[0]["text"] == "legacy fallback body"
    assert "chunk_id" not in bodies[0] and bodies[0]["line"] == 1


def test_index_world_reports_rag_degraded_when_rag_md_missing(monkeypatch, tmp_path):
    """D1: jsonl はあるのに対になる rag.md が無い（不整合な派生状態）場合も legacy へ縮退し、
    `rag_md_missing` として報告する。"""
    derived = tmp_path / "derived" / "md"
    derived.mkdir(parents=True)
    (derived / "a.docx.rag_chunks.jsonl").write_text(json.dumps({
        "chunk_id": "rc1", "source_rel_path": "a.docx"}) + "\n", encoding="utf-8")
    # rag.md を書かない＝不整合な派生状態を再現する。
    docs = [{"name": "a.docx", "md_path": str(derived / "a.docx.md"), "top_scope": "t"}]

    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, include_rag=False: docs)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: derived)
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: derived)  # §8.1 三階層（テストfixtureは同一dirを共用）
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "legacy fallback body\n")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})

    r = es_index.index_world("w")
    assert r["indexed"] == 1
    assert r["rag_degraded"] == 1
    assert r["rag_degraded_docs"] == [{"doc": "a.docx", "reason": "rag_md_missing"}]


def test_index_world_legacy_rag_md_without_anchors_degrades_safely(monkeypatch, tmp_path):
    """D1 受け入れ条件（旧形式の安全な縮退）: 版bump前（v1alpha8以下）に生成された、アンカー無しの
    旧形式 rag.md を新コードが読んでも、legacy 40行チャンクへ静かに縮退するだけでクラッシュしない。"""
    derived = tmp_path / "derived" / "md"
    derived.mkdir(parents=True)
    (derived / "a.docx.rag_chunks.jsonl").write_text(json.dumps({
        "chunk_id": "rc1", "search_text": "旧形式の本文", "source_rel_path": "a.docx"}) + "\n",
        encoding="utf-8")
    (derived / "a.docx.rag.md").write_text(
        "# AI検索用文書\n\n旧形式のrag.md本文（アンカー無し）\n", encoding="utf-8")
    docs = [{"name": "a.docx", "md_path": str(derived / "a.docx.md"), "top_scope": "t"}]

    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, include_rag=False: docs)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: derived)
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: derived)  # §8.1 三階層（テストfixtureは同一dirを共用）
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "legacy fallback body\n")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            captured["bulk"] = body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)

    r = es_index.index_world("w")
    assert r["indexed"] == 1
    assert r["rag_degraded"] == 1
    assert r["rag_degraded_docs"] == [{"doc": "a.docx", "reason": "rag_md_no_anchors"}]
    bodies = [json.loads(ln) for i, ln in enumerate(captured["bulk"].strip().split("\n")) if i % 2 == 1]
    assert bodies[0]["text"] == "legacy fallback body"   # jsonlの旧search_textは一切使われない
    assert "chunk_id" not in bodies[0]


def test_index_world_off_return_shape_has_no_rag_report_keys(monkeypatch):
    """legacy モード（`rag_es_enabled` を直接差し替えて模擬）: rag_degraded/rag_degraded_docs
    キー自体を返さない＝戻り値の形も完全不変。"""
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    r = es_index.index_world("w")
    assert "rag_degraded" not in r and "rag_degraded_docs" not in r


# ---- _parse_hits: rag_chunks 由来メタの passthrough ----

def test_parse_hits_passes_through_rag_chunk_meta():
    res = {"hits": {"hits": [{"_score": 3.0, "_source": {
        "doc_id": "a.docx", "text": "本文", "ext": ".docx",
        "chunk_id": "rc1", "locator": {"sheet": "S1", "cell_range": "B12"}}}]}}
    hit = es_index._parse_hits(res)[0]
    assert hit["chunk_id"] == "rc1"
    assert hit["locator"] == {"sheet": "S1", "cell_range": "B12"}
    assert hit["line"] is None   # rag チャンクは line を持たない（下流の呼び出し元は h.get("line") で耐性）


def test_parse_hits_passes_through_context_neighbor_meta():
    """B1: 隣接キーもヒットへ passthrough する（`parent_id` は `agentic_search` の親返し L4c が読む。
    文脈拡張として辿って連結する B2 は撤去済み）。"""
    res = {"hits": {"hits": [{"_score": 1.0, "_source": {
        "doc_id": "a.docx", "text": "本文", "ext": ".docx", "chunk_id": "rc2",
        "previous_chunk_id": "rc1", "next_chunk_id": "rc3", "parent_id": "region1",
        "logical_record_id": "lr1", "section_path": ["見出し1"]}}]}}
    hit = es_index._parse_hits(res)[0]
    assert hit["previous_chunk_id"] == "rc1"
    assert hit["next_chunk_id"] == "rc3"
    assert hit["parent_id"] == "region1"
    assert hit["logical_record_id"] == "lr1"
    assert hit["section_path"] == ["見出し1"]


def test_parse_hits_omits_context_neighbor_meta_when_absent():
    """legacy 40行チャンク（隣接キー無し）はキー自体を付けない＝後方互換。"""
    res = {"hits": {"hits": [{"_score": 1.0, "_source": {
        "doc_id": "a.cbl", "line": 1, "text": "行", "ext": ".cbl"}}]}}
    hit = es_index._parse_hits(res)[0]
    for key in ("previous_chunk_id", "next_chunk_id", "parent_id", "logical_record_id", "section_path"):
        assert key not in hit


# ---- SHERPA_ES_CHUNK_LINES / needs_reindex 連動 ----
# import 時に一度だけ確定する定数は実プロセスを新規に起こして検証する（`_fresh_import`）。

def test_chunk_lines_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.es_index", "_CHUNK_LINES",
                                env={"SHERPA_ES_CHUNK_LINES": None}) == 40


def test_chunk_lines_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.es_index", "_CHUNK_LINES",
                                env={"SHERPA_ES_CHUNK_LINES": "80"}) == 80


def test_chunk_lines_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("5", "401", "abc"):
        assert FI.fresh_import_attr("sherpa.es_index", "_CHUNK_LINES",
                                    env={"SHERPA_ES_CHUNK_LINES": bad}) == 40, bad


def test_chunk_lines_env_change_after_import_has_no_effect(monkeypatch):
    before = es_index._CHUNK_LINES
    monkeypatch.setenv("SHERPA_ES_CHUNK_LINES", "300")
    assert es_index._CHUNK_LINES == before == 40


def test_index_world_omits_chunk_lines_at_default(monkeypatch):
    """既定粒度（40）のときは emeta に `chunk_lines` を書かない＝索引 meta を最小限に保つ。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約（TOGGLE-RM 後も内部シームとして残置）
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 40)   # 既定と同値に固定
    captured = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        captured.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    es_index.index_world("w")
    assert "chunk_lines" not in captured


def test_index_world_records_chunk_lines_when_non_default(monkeypatch):
    """既定と異なる粒度のときだけ emeta に `chunk_lines` を刻む（drift 検知の書き込み側）。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)   # legacy チャンク経路の契約（TOGGLE-RM 後も内部シームとして残置）
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 80)
    captured = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        captured.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    es_index.index_world("w")
    assert captured["chunk_lines"] == 80


def test_needs_reindex_reacts_to_chunk_lines_drift(monkeypatch):
    """`SHERPA_ES_CHUNK_LINES` を変えた（既存索引は旧粒度のまま）＝ content_sig 不変でも
    chunk_lines 不一致で reindex 要。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "legacy", "arms_sig": "sig-A", "analyzer_config_sig": "acfg-A",
        "chunk_lines": 40})
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 40)
    assert es_index.needs_reindex("w", "c1") is False           # 全一致＝不要
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 80)            # チャンク粒度を変えた
    assert es_index.needs_reindex("w", "c1") is True


def test_needs_reindex_missing_chunk_lines_field_does_not_force_reindex_at_default(monkeypatch):
    """旧索引（`chunk_lines` フィールド自体が無い）は、現在の粒度が既定 40 のままなら reindex
    しない（欠落は `None` ではなく旧既定 40 として扱う）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 40)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "legacy", "arms_sig": "sig-A",
        "analyzer_config_sig": "acfg-A"})   # 旧索引（フィールド無し）
    assert es_index.needs_reindex("w", "c1") is False


def test_needs_reindex_missing_chunk_lines_field_still_reacts_when_current_changed(monkeypatch):
    """旧索引（フィールド無し＝旧既定40相当）でも、現在の粒度が既定と異なれば reindex 要
    （欠落を無条件で「常に一致」扱いにしていないことの確認）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "count", lambda w: 5)
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_arms_config_sig", lambda: "sig-A")
    monkeypatch.setattr(es_index, "_human_md_config_sig", lambda world: None)   # H2: この寸法は孤立させる
    monkeypatch.setattr(es_index, "_analyzer_config_sig", lambda: "acfg-A")
    monkeypatch.setattr(es_index, "_search_chunk_mode", lambda: "legacy")
    monkeypatch.setattr(es_index, "_CHUNK_LINES", 80)   # 現在は既定と異なる
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "content_sig": "c1", "mapping_version": es_index.ES_MAPPING_VERSION,
        "search_chunk_mode": "legacy", "arms_sig": "sig-A",
        "analyzer_config_sig": "acfg-A"})   # 旧索引（フィールド無し）
    assert es_index.needs_reindex("w", "c1") is True


# ---- SHERPA_ES_HYBRID_WEIGHT（BM25/kNN 配分） ----

def test_hybrid_weight_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.es_index", "_HYBRID_WEIGHT",
                                env={"SHERPA_ES_HYBRID_WEIGHT": None}) == 0.5


def test_hybrid_weight_fresh_import_env_valid_value():
    assert FI.fresh_import_attr("sherpa.es_index", "_HYBRID_WEIGHT",
                                env={"SHERPA_ES_HYBRID_WEIGHT": "0.8"}) == 0.8
    assert FI.fresh_import_attr("sherpa.es_index", "_HYBRID_WEIGHT",
                                env={"SHERPA_ES_HYBRID_WEIGHT": "0"}) == 0.0   # 境界値
    assert FI.fresh_import_attr("sherpa.es_index", "_HYBRID_WEIGHT",
                                env={"SHERPA_ES_HYBRID_WEIGHT": "1"}) == 1.0   # 境界値


def test_hybrid_weight_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("-0.1", "1.1", "abc"):
        assert FI.fresh_import_attr("sherpa.es_index", "_HYBRID_WEIGHT",
                                    env={"SHERPA_ES_HYBRID_WEIGHT": bad}) == 0.5, bad


def test_hybrid_weight_env_change_after_import_has_no_effect(monkeypatch):
    """他5定数と同様 import 時に一度だけ確定する定数＝同一プロセス内で env を後から変えても
    効かない。"""
    before = es_index._HYBRID_WEIGHT
    monkeypatch.setenv("SHERPA_ES_HYBRID_WEIGHT", "0.9")
    assert es_index._HYBRID_WEIGHT == before == 0.5


def test_search_hybrid_query_omits_boost_at_default_weight_for_byte_identical_body(monkeypatch):
    """既定配分（w=0.5）では boost キー自体を書かない（match 節が生の `{"match": {"text": q}}`
    のまま・knn に "boost" が無い＝無指定と同じ本文になる）。

    RV是正（rv-i2-importance #4・2026-09）: hybrid（vector=True）は match/knn を同一 `bool.should`
    に並べ、その bool 全体を `function_score`（重要度ブースト）で1回だけ包む形に変わった
    （`es_index.search` docstring 参照・旧実装は top-level `query`+top-level `knn` の別々の節で、
    function_score は `query` 側だけに掛かっていた＝合成スコアの一部にしか重要度が反映されない
    実害があった）。"""
    monkeypatch.setattr(es_index, "_HYBRID_WEIGHT", 0.5)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "embed_provider": "openai", "embed_model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: {
        "provider": "openai", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "embed", lambda qs, ec, world=None: [[0.1, 0.2, 0.3]])
    captured = {}

    def fake_req(method, path, body=None, **kw):
        captured["body"] = body
        return {"hits": {"hits": []}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    es_index.search("w", "query")
    body = captured["body"]
    assert "knn" not in body   # top-level knn パラメータはもう使わない（query 節の中身へ移した）
    bool_q = body["query"]["function_score"]["query"]["bool"]
    assert bool_q["should"][0] == {"match": {"text": "query"}}
    assert "boost" not in bool_q["should"][1]["knn"]


def test_search_hybrid_query_weight_skews_boost_toward_keyword(monkeypatch):
    """weight=0.8（`_HYBRID_WEIGHT` を直接差し替え。import-time 定数化のため env 経由では
    効かない＝`_HYBRID_WEIGHT` の parse 自体は上の fresh-import テストが検証済み）だと
    keyword 側 boost が vector 側より大きくなる（配分が効いている）。"""
    monkeypatch.setattr(es_index, "_HYBRID_WEIGHT", 0.8)
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {
        "embed_provider": "openai", "embed_model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: {
        "provider": "openai", "model": "m", "dim": 3})
    monkeypatch.setattr(es_index.embeddings, "embed", lambda qs, ec, world=None: [[0.1, 0.2, 0.3]])
    captured = {}

    def fake_req(method, path, body=None, **kw):
        captured["body"] = body
        return {"hits": {"hits": []}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    es_index.search("w", "query")
    body = captured["body"]
    bool_q = body["query"]["function_score"]["query"]["bool"]
    match_boost = bool_q["should"][0]["match"]["text"]["boost"]
    knn_boost = bool_q["should"][1]["knn"]["boost"]
    assert match_boost > knn_boost
    assert round(match_boost + knn_boost, 6) == 2.0


# ---- SHERPA_GREP_MAX_HITS と ES 側 size 上限（_ES_SEARCH_K_MAX）の連動 ----
# `.env.example` の `SHERPA_GREP_MAX_HITS` は grep 向けの既定値（30）をコメントアウトで示している
# だけだが、bootstrap で丸ごと `.env` にコピーする運用や利用者が値だけ有効化する運用でも、
# ES 側の既定上限（50）を後退させてはいけない＝`_ES_SEARCH_K_MAX` は 50 未満には下がらない。

def test_es_search_k_max_fresh_import_env_unset_is_default():
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_SEARCH_K_MAX",
                                env={"SHERPA_GREP_MAX_HITS": None}) == 50


def test_es_search_k_max_fresh_import_env_below_50_does_not_lower_ceiling():
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_SEARCH_K_MAX",
                                env={"SHERPA_GREP_MAX_HITS": "30"}) == 50
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_SEARCH_K_MAX",
                                env={"SHERPA_GREP_MAX_HITS": "1"}) == 50


def test_es_search_k_max_fresh_import_env_above_50_raises_ceiling():
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_SEARCH_K_MAX",
                                env={"SHERPA_GREP_MAX_HITS": "100"}) == 100


def test_es_search_k_max_fresh_import_env_invalid_falls_back_to_default():
    for bad in ("0", "1001", "abc"):
        assert FI.fresh_import_attr("sherpa.es_index", "_ES_SEARCH_K_MAX",
                                    env={"SHERPA_GREP_MAX_HITS": bad}) == 50, bad


def test_es_search_k_max_env_change_after_import_has_no_effect(monkeypatch):
    before = es_index._ES_SEARCH_K_MAX
    monkeypatch.setenv("SHERPA_GREP_MAX_HITS", "999")
    assert es_index._ES_SEARCH_K_MAX == before == 50


def _es_size_capture_script() -> str:
    return (
        "import json\n"
        "import sherpa.es_index as es_index\n"
        "captured = {}\n"
        "def fake_req(method, path, body=None, **kw):\n"
        "    captured['body'] = body\n"
        "    return {'hits': {'hits': []}}\n"
        "es_index._req = fake_req\n"
        "es_index.available = lambda: True\n"
        "es_index.embeddings.cfg = lambda settings=None, **kw: None\n"   # kNN 無効化＝BM25 body を見る
        "es_index.search('w', 'query', k=999)\n"
        "print(json.dumps(captured['body']['size']))\n"
    )


def test_es_search_size_reaches_100_when_env_set_to_100():
    """`SHERPA_GREP_MAX_HITS=100` のとき `search()` が ES へ送る size も 100 まで通る。"""
    out = FI.run_script(_es_size_capture_script(), env={"SHERPA_GREP_MAX_HITS": "100"})
    assert json.loads(out.splitlines()[-1]) == 100


def test_es_search_size_stays_50_at_default():
    out = FI.run_script(_es_size_capture_script(), env={"SHERPA_GREP_MAX_HITS": None})
    assert json.loads(out.splitlines()[-1]) == 50


def test_es_search_size_stays_50_when_env_set_to_30():
    """`.env.example` の該当行はコメント配布（既定はコード側）だが、`SHERPA_GREP_MAX_HITS` が
    grep 向け既定値（30）に明示設定されていても、ES の既定上限（50）は後退しない
    （keyword/vector 経路の非対称を防ぐ）。"""
    out = FI.run_script(_es_size_capture_script(), env={"SHERPA_GREP_MAX_HITS": "30"})
    assert json.loads(out.splitlines()[-1]) == 50


# ---- k_ceiling で `_ES_SEARCH_K_MAX`（既定 50）の再クランプを迂回する ----
# `agentic_search.run_tool` の es_search 分岐が、調べる深さ（depth_profile）で計算した実効値
# （既定構成でも「最大」は 30×2=60 に達し、50 の床を超えうる）を渡すための経路。

def _es_size_capture_script_with_k_ceiling(k: int, k_ceiling) -> str:
    return (
        "import json\n"
        "import sherpa.es_index as es_index\n"
        "captured = {}\n"
        "def fake_req(method, path, body=None, **kw):\n"
        "    captured['body'] = body\n"
        "    return {'hits': {'hits': []}}\n"
        "es_index._req = fake_req\n"
        "es_index.available = lambda: True\n"
        "es_index.embeddings.cfg = lambda settings=None, **kw: None\n"
        f"es_index.search('w', 'query', k={k}, k_ceiling={k_ceiling!r})\n"
        "print(json.dumps(captured['body']['size']))\n"
    )


def test_es_search_k_ceiling_bypasses_es_search_k_max_default_floor():
    """要求67・90 は既定（k_ceiling 省略）だと ES 側の既定上限50へ潰れる。`k_ceiling=1000`
    （`agentic_search.MAX_HITS_ABS_MAX`・grep と共通の絶対上限）を渡すとそのまま通る。"""
    for requested in (45, 67, 90):
        out = FI.run_script(
            _es_size_capture_script_with_k_ceiling(requested, 1000), env={"SHERPA_GREP_MAX_HITS": None})
        assert json.loads(out.splitlines()[-1]) == requested, requested


def test_es_search_k_ceiling_still_clamps_above_itself():
    """`k_ceiling` 自体は「無制限」ではなく、それを超える要求はそこでクランプされる
    （倍率適用後に一度だけ適用する絶対上限という契約）。"""
    out = FI.run_script(
        _es_size_capture_script_with_k_ceiling(2000, 1000), env={"SHERPA_GREP_MAX_HITS": None})
    assert json.loads(out.splitlines()[-1]) == 1000


def test_es_search_k_ceiling_omitted_keeps_existing_50_floor_behavior():
    """`k_ceiling` 省略（既存呼び出し元）は従来どおり `_ES_SEARCH_K_MAX`（既定50）が効く
    （`test_es_search_size_stays_50_at_default` と同じ契約・回帰無し）。"""
    out = FI.run_script(_es_size_capture_script(), env={"SHERPA_GREP_MAX_HITS": None})
    assert json.loads(out.splitlines()[-1]) == 50


# ---- bulk バッチ化（2026-09-02・本レーン）: `_bulk_batches` の境界 ----

def test_bulk_batches_splits_by_doc_count(monkeypatch):
    """`_bulk_batches` は件数境界（`_ES_BULK_BATCH_MAX_DOCS`）でバッチを分割する。"""
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 2)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", 10 * 1024 * 1024)   # バイト境界には掛からない
    ids = [f"id{i}" for i in range(5)]
    bodies = [{"doc_id": "d", "text": f"t{i}"} for i in range(5)]
    batches = es_index._bulk_batches(ids, bodies, {})
    counts = [len(b.strip().split("\n")) // 2 for b in batches]
    assert counts == [2, 2, 1]                        # 5件 = 2+2+1
    seen_ids = []
    for b in batches:
        lines = b.strip().split("\n")
        seen_ids.extend(json.loads(lines[i])["index"]["_id"] for i in range(0, len(lines), 2))
    assert seen_ids == ids                            # 全チャンクがどこかに1回だけ現れる（順序も保持）


def test_bulk_batches_splits_by_byte_size(monkeypatch):
    """`_bulk_batches` はバイト境界（`_ES_BULK_BATCH_MAX_BYTES`）でも分割する——チャンクサイズが
    ばらつく（特に embedding 付きチャンク）ため、件数だけでは1バッチの実バイト量を有界にできない。"""
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1000)   # 件数境界には掛からない
    ids = [f"id{i}" for i in range(4)]
    bodies = [{"doc_id": "d", "text": "x" * 80} for _ in range(4)]
    action = json.dumps({"index": {"_id": ids[0]}})
    doc = json.dumps(bodies[0], ensure_ascii=False)
    pair_bytes = len(action.encode("utf-8")) + len(doc.encode("utf-8")) + 2
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", pair_bytes * 2)   # ちょうど2件分
    batches = es_index._bulk_batches(ids, bodies, {})
    counts = [len(b.strip().split("\n")) // 2 for b in batches]
    assert counts == [2, 2]


def test_bulk_batches_oversized_single_chunk_gets_own_batch(monkeypatch):
    """1チャンク単体が `_ES_BULK_BATCH_MAX_BYTES` を超えても、そのチャンクだけの単独バッチとして
    送る（レコードを分割できないため）——現行バッチが空でない場合のみ閾値判定するので、
    単独チャンクは必ず1バッチに収まり無限ループにならない。"""
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1000)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", 50)   # 極端に小さい
    ids = ["a", "b"]
    bodies = [{"doc_id": "d", "text": "x" * 500}, {"doc_id": "d", "text": "y" * 500}]
    batches = es_index._bulk_batches(ids, bodies, {})
    assert len(batches) == 2
    for b in batches:
        assert len(b.strip().split("\n")) == 2         # action + doc のみ（単独チャンク）


def test_bulk_batches_applies_embedding_from_vec_by_idx(monkeypatch):
    """`vec_by_idx`（元 index 位置→ベクトル）を各バッチのボディへ正しく反映する（欠落は付けない）。"""
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1000)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_BYTES", 10 * 1024 * 1024)
    ids = ["a", "b"]
    bodies = [{"doc_id": "d", "text": "t1"}, {"doc_id": "d", "text": "t2"}]
    batches = es_index._bulk_batches(ids, bodies, {0: [0.1, 0.2]})
    docs = [json.loads(ln) for b in batches for i, ln in enumerate(b.strip().split("\n")) if i % 2 == 1]
    assert docs[0].get("embedding") == [0.1, 0.2]
    assert "embedding" not in docs[1]


# ---- index_world: バッチ送信・refresh は最後のバッチだけ・途中失敗は wipe（案a）----

def _setup_index_world_multi_chunk_docs(monkeypatch, n: int):
    """`n` 文書（各1チャンクの legacy 本文）を用意し、bulk 送信以外を graceful にモックする。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    docs = [{"name": f"d{i}.md", "md_path": None, "top_scope": "t"} for i in range(n)]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文1行のみ")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_ES_BULK_BATCH_MAX_DOCS", 1)   # 1文書=1チャンクなので n バッチに割れる


def test_index_world_sends_multiple_bulk_batches_refresh_only_last(monkeypatch):
    """bulk 送信はバッチ境界で複数リクエストに分かれ、`refresh=true` は最後のバッチだけに付く
    （毎バッチ refresh すると著しく遅い）。"""
    _setup_index_world_multi_chunk_docs(monkeypatch, 3)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    calls = []

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            calls.append((path, body))
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r["indexed"] == 3 and r["chunks"] == 3 and r.get("error") is None
    assert len(calls) == 3
    assert [p.endswith("?refresh=true") for p, _ in calls] == [False, False, True]
    for _, body in calls:
        assert len(body.strip().split("\n")) == 2       # 各バッチは1チャンク分（action+doc）だけ


def test_index_world_partial_batch_exception_wipes_index_and_returns_bulk_failed(monkeypatch):
    """案a（全部か無しか・2026-09-02裁定）: 複数バッチに分かれた bulk の途中バッチが例外で
    失敗したら、`delete_world()` で world を空へ戻し `bulk_failed` を返す——先に成功した
    バッチ分だけが index に残る「検索したのに出てこない」というサイレントな取りこぼしを防ぐ。
    3バッチ目は送られない（打ち切り）。"""
    _setup_index_world_multi_chunk_docs(monkeypatch, 3)
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    call_n = {"n": 0}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            call_n["n"] += 1
            if call_n["n"] == 2:
                raise RuntimeError("network failure mid-batch")
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r == {"available": True, "indexed": 0, "chunks": 0, "error": "bulk_failed"}
    assert call_n["n"] == 2
    # delete_world: クリーン再索引の delete（bulk 前・毎回発生）＋ 部分失敗を検知した後の wipe。
    assert delete_calls == ["w", "w"]


def test_index_world_partial_batch_item_errors_wipes_index_and_returns_bulk_errors(monkeypatch):
    """途中バッチが item-level エラー（HTTP200 でも `res["errors"]` が真）で失敗した場合も
    同様に world を空へ戻し `bulk_errors` を返す。"""
    _setup_index_world_multi_chunk_docs(monkeypatch, 3)
    delete_calls = []
    monkeypatch.setattr(es_index, "delete_world", lambda w: delete_calls.append(w) or True)
    call_n = {"n": 0}

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            call_n["n"] += 1
            if call_n["n"] == 2:
                return {"errors": True}
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r == {"available": True, "indexed": 0, "chunks": 0, "error": "bulk_errors"}
    assert call_n["n"] == 2
    assert delete_calls == ["w", "w"]


def test_index_world_single_batch_default_thresholds_still_refreshes(monkeypatch):
    """既定の閾値では少数チャンクの world は従来どおり1バッチで送られ、そのバッチに
    `refresh=true` が付く（回帰防止・バッチ化前の単発 bulk と外形が変わらないこと）。"""
    monkeypatch.setattr(es_index, "rag_es_enabled", lambda: False)
    docs = [{"name": "a.md", "md_path": None, "top_scope": "t"},
            {"name": "b.md", "md_path": None, "top_scope": "t"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    calls = []

    def fake_req(method, path, body=None, **kw):
        if isinstance(path, str) and "_bulk" in path:
            calls.append(path)
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    r = es_index.index_world("w")
    assert r.get("error") is None
    assert calls == [f"/{es_index._index('w')}/_bulk?refresh=true"]


# ---- 量的超過（`_RAG_CHUNKS_MAX_ROWS` 等）は env 可変・上限到達は rag_degraded_docs へ ----

def test_index_world_too_many_rows_is_env_configurable_and_reports_rag_degraded(monkeypatch, tmp_path):
    """量的超過（`too_many_rows`）は env（`_RAG_CHUNKS_MAX_ROWS` を通じて）で上限を変えられ、
    当たったらファイル全体を legacy 40行チャンクへ縮退しつつ `rag_degraded_docs` へ理由付きで
    載る——先頭N行だけを黙って採用する部分採用はしない（破損との区別が付かなくなるため）。"""
    derived = tmp_path / "derived" / "md"
    derived.mkdir(parents=True)
    rows = [{"chunk_id": f"rc{i}", "source_rel_path": "a.docx"} for i in range(5)]
    (derived / "a.docx.rag_chunks.jsonl").write_text(
        "\n".join(json.dumps(r) for r in rows) + "\n", encoding="utf-8")
    _write_rag_md(derived / "a.docx.rag.md", *[(f"rc{i}", f"本文{i}") for i in range(5)])
    docs = [{"name": "a.docx", "md_path": str(derived / "a.docx.md"), "top_scope": "t"}]

    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, include_rag=False: docs)
    monkeypatch.setattr(es_index.worlds, "derived_md_dir", lambda w: derived)
    monkeypatch.setattr(es_index.worlds, "derived_rag_dir", lambda w: derived)  # §8.1 三階層（テストfixtureは同一dirを共用）
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "legacy fallback\n")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: {})
    monkeypatch.setattr(es_index, "_RAG_CHUNKS_MAX_ROWS", 2)   # env 相当の値を下げて5行のファイルを拒否させる

    r = es_index.index_world("w")
    assert r["indexed"] == 1                              # 文書自体は legacy へ縮退して残る（消えない）
    assert r["rag_degraded"] == 1
    assert r["rag_degraded_docs"] == [{"doc": "a.docx", "reason": "too_many_rows"}]


def test_rag_chunks_max_rows_default_loosened_and_env_configurable():
    """既定は旧 20000 から大幅に緩和（world 全体の bulk 資源はバッチ分割が別途守るため、1文書
    単位のこの上限を過度に絞る理由が無くなった）。env でも変更できる（`_env_int` と同じセマンティクス）。"""
    assert FI.fresh_import_attr("sherpa.es_index", "_RAG_CHUNKS_MAX_ROWS",
                                env={"SHERPA_ES_RAG_CHUNKS_MAX_ROWS": None}) == 200000
    assert FI.fresh_import_attr("sherpa.es_index", "_RAG_CHUNKS_MAX_ROWS",
                                env={"SHERPA_ES_RAG_CHUNKS_MAX_ROWS": "500"}) == 500


def test_rag_chunks_file_cap_bytes_env_configurable():
    assert FI.fresh_import_attr("sherpa.es_index", "_RAG_CHUNKS_FILE_CAP_BYTES",
                                env={"SHERPA_ES_RAG_CHUNKS_FILE_CAP_BYTES": None}) == 32 * 1024 * 1024
    assert FI.fresh_import_attr("sherpa.es_index", "_RAG_CHUNKS_FILE_CAP_BYTES",
                                env={"SHERPA_ES_RAG_CHUNKS_FILE_CAP_BYTES": "2048"}) == 2048


def test_rag_chunk_search_text_max_chars_env_configurable():
    assert FI.fresh_import_attr("sherpa.es_index", "_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS",
                                env={"SHERPA_ES_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS": None}) == 20000
    assert FI.fresh_import_attr("sherpa.es_index", "_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS",
                                env={"SHERPA_ES_RAG_CHUNK_SEARCH_TEXT_MAX_CHARS": "500"}) == 500


def test_es_bulk_batch_max_docs_env_configurable():
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_BULK_BATCH_MAX_DOCS",
                                env={"SHERPA_ES_BULK_BATCH_MAX_DOCS": None}) == 2000
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_BULK_BATCH_MAX_DOCS",
                                env={"SHERPA_ES_BULK_BATCH_MAX_DOCS": "10"}) == 10


def test_es_bulk_batch_max_bytes_env_configurable():
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_BULK_BATCH_MAX_BYTES",
                                env={"SHERPA_ES_BULK_BATCH_MAX_BYTES": None}) == 8 * 1024 * 1024
    assert FI.fresh_import_attr("sherpa.es_index", "_ES_BULK_BATCH_MAX_BYTES",
                                env={"SHERPA_ES_BULK_BATCH_MAX_BYTES": "131072"}) == 131072


# ---- bulk 途中失敗の wipe は fail-closed（検収是正） ----

def test_wipe_after_bulk_failure_drops_content_sig_when_delete_fails(monkeypatch):
    """wipe に失敗したら `_meta.content_sig` を落とす。

    `ensure_index` は bulk の**前**に content_sig を書くため、wipe が失敗すると
    「一部だけ入った索引＋有効な content_sig」が残り `needs_reindex` が False を返す
    ＝中途半端な索引が居座る（サイレントな取りこぼし）。これを塞ぐ。
    """
    monkeypatch.setattr(es_index, "delete_world", lambda w: False)      # wipe 失敗
    monkeypatch.setattr(es_index, "_index_meta",
                        lambda w: {"content_sig": "c1", "mapping_version": "6", "world_id": w})
    sent = {}

    def fake_req(method, path, body=None, ndjson=False):
        sent["method"], sent["path"], sent["body"] = method, path, body
        return {}

    monkeypatch.setattr(es_index, "_req", fake_req)
    es_index._wipe_after_bulk_failure("w")
    assert sent["method"] == "PUT" and sent["path"].endswith("/_mapping")
    assert "content_sig" not in sent["body"]["_meta"]        # 落ちている
    assert sent["body"]["_meta"]["world_id"] == "w"          # 他のフィールドは保持


def test_wipe_after_bulk_failure_does_nothing_more_when_delete_succeeds(monkeypatch):
    """wipe が成功したら索引ごと消えているので meta 書き換えは不要（余計な PUT を出さない）。"""
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    called = []
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: called.append(a) or {})
    es_index._wipe_after_bulk_failure("w")
    assert called == []


def test_wipe_after_bulk_failure_survives_meta_write_failure(monkeypatch):
    """wipe も meta 無効化も失敗しても例外を投げない（ES 自体が落ちている＝次回 sync に委ねる）。"""
    monkeypatch.setattr(es_index, "delete_world", lambda w: False)
    monkeypatch.setattr(es_index, "_index_meta", lambda w: {"content_sig": "c1"})

    def boom(*a, **k):
        raise RuntimeError("es down")

    monkeypatch.setattr(es_index, "_req", boom)
    es_index._wipe_after_bulk_failure("w")                    # 例外を漏らさない


def test_content_sig_written_only_after_all_batches_succeed(monkeypatch):
    """`content_sig` は全バッチ成功後にだけ書く。

    先に書くと、途中でプロセスが落ちた（OOM/kill＝ES エラーではないので wipe が走らない）とき
    「一部だけ入った索引＋有効な content_sig」が残り、`needs_reindex` が False を返して
    中途半端な索引が恒久的に居座る。バッチ化で索引中の時間窓が伸びたぶん無視できない。
    """
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: [])
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    emeta_at_create = {}

    def fake_ensure_index(w, dim=None, emeta=None):
        emeta_at_create.update(emeta or {})
        return True

    monkeypatch.setattr(es_index, "ensure_index", fake_ensure_index)
    confirmed = []
    monkeypatch.setattr(es_index, "_confirm_content_sig", lambda w, sig: confirmed.append(sig))
    es_index.index_world("w", content_sig="c1")
    assert "content_sig" not in emeta_at_create      # 索引作成時点では書かない
    assert confirmed == ["c1"]                        # 全バッチ成功後に確定


def test_content_sig_not_confirmed_when_a_batch_fails(monkeypatch):
    """途中バッチが失敗したら content_sig は確定しない（wipe が失敗しても次回 sync が張り直す）。"""
    docs = [{"name": "a.cbl", "md_path": None, "top_scope": "t", "branch": "source"}]
    monkeypatch.setattr(es_index.corpus_docs, "world_documents", lambda w, **kw: docs)
    monkeypatch.setattr(es_index.doc_text, "read_world_doc_text", lambda w, d: "本文")
    monkeypatch.setattr(es_index, "available", lambda: True)
    monkeypatch.setattr(es_index, "delete_world", lambda w: True)
    monkeypatch.setattr(es_index, "_embed_cached", lambda *a, **k: (None, 0, 0))
    monkeypatch.setattr(es_index.embeddings, "cfg", lambda settings=None, **kw: None)
    monkeypatch.setattr(es_index, "ensure_index", lambda w, dim=None, emeta=None: True)
    confirmed = []
    monkeypatch.setattr(es_index, "_confirm_content_sig", lambda w, sig: confirmed.append(sig))
    monkeypatch.setattr(es_index, "_wipe_after_bulk_failure", lambda w: None)

    def boom(method, path, body=None, ndjson=False):
        raise RuntimeError("bulk down")

    monkeypatch.setattr(es_index, "_req", boom)
    res = es_index.index_world("w", content_sig="c1")
    assert res.get("error") == "bulk_failed"
    assert confirmed == []                            # 確定していない＝次回 sync が張り直す


# ---- L4c 親返し（`agentic_search._resolve_parent_return` の P2 が使う土台）----

def test_rag_md_anchor_chunk_id_matches_and_rejects():
    """`rag_md_anchor_chunk_id` は `_parse_rag_md_chunks`（全文一括版）と同じアンカー形式
    （`<!-- chunk:{chunk_id} -->`）を行単位で判定する。"""
    assert es_index.rag_md_anchor_chunk_id("<!-- chunk:rag-chunk:abc123 -->") == "rag-chunk:abc123"
    assert es_index.rag_md_anchor_chunk_id("本文の行") is None
    assert es_index.rag_md_anchor_chunk_id("<!-- chunk:c1 --> 余分な文字") is None   # 完全一致のみ
    assert es_index.rag_md_anchor_chunk_id("") is None


def test_chunk_ids_for_parent_returns_matching_chunk_ids(monkeypatch):
    """`chunk_ids_for_parent` は doc_id＋parent_id の terms フィルタで ES を問い合わせ、
    ヒットの chunk_id だけを取り出す。"""
    monkeypatch.setattr(es_index, "available", lambda: True)
    captured = {}

    def fake_req(method, path, body=None, **kw):
        captured["method"] = method
        captured["path"] = path
        captured["body"] = body
        return {"hits": {"hits": [
            {"_source": {"chunk_id": "c1"}}, {"_source": {"chunk_id": "c2"}},
            {"_source": {}},                              # chunk_id 欠落は無視
        ]}}

    monkeypatch.setattr(es_index, "_req", fake_req)
    out = es_index.chunk_ids_for_parent("w", "a.docx", ["p1"])
    assert out == ["c1", "c2"]
    assert captured["method"] == "POST" and captured["path"].endswith("/_search")
    q = captured["body"]["query"]["bool"]["filter"]
    assert {"term": {"doc_id": "a.docx"}} in q
    assert {"terms": {"parent_id": ["p1"]}} in q


def test_chunk_ids_for_parent_empty_when_unavailable_or_no_parent_ids(monkeypatch):
    """ES 不達、または `parent_ids` が空/不正値のみのときは空リスト（best-effort・クエリしない）。"""
    monkeypatch.setattr(es_index, "available", lambda: False)
    calls = []
    monkeypatch.setattr(es_index, "_req", lambda *a, **k: calls.append(1) or {})
    assert es_index.chunk_ids_for_parent("w", "a.docx", ["p1"]) == []
    assert calls == []

    monkeypatch.setattr(es_index, "available", lambda: True)
    assert es_index.chunk_ids_for_parent("w", "a.docx", []) == []
    assert es_index.chunk_ids_for_parent("w", "a.docx", [None, "", 123]) == []
    assert calls == []


def test_chunk_ids_for_parent_best_effort_on_query_failure(monkeypatch):
    """ES クエリが例外を投げても空リスト（呼び出し元＝親返しの P2 は chunk tier へ縮退できる）。"""
    monkeypatch.setattr(es_index, "available", lambda: True)

    def boom(*a, **k):
        raise RuntimeError("es down")

    monkeypatch.setattr(es_index, "_req", boom)
    assert es_index.chunk_ids_for_parent("w", "a.docx", ["p1"]) == []
