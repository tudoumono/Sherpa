"""`scripts/ab_search.py`（rag.md vs 人間向けMD 検索比較 CLI）の単体テスト。

ES は実サービスを使わず全面モックする（`_es_req`/`_create_index`/`_delete_index`/`_bulk_index`/
`_search` を差し替え）。実変換（`office_md.build_derived`）も対象外——チャンク分割（A=見出し単位・
B=アンカー単位）・一時 index 名の隔離（`abtest_` プレフィクス・A/B で別名）・異常終了でも一時 index が
必ず削除されることの3点を固定する。

`scripts/` はパッケージ化されていないが、`tests/conftest.py` がリポジトリルートを sys.path に
載せるため、暗黙の名前空間パッケージとして `import scripts.ab_search` できる
（`tests/unit/test_graph_extract_ab_script.py` と同じ手法）。
"""
from __future__ import annotations

import urllib.error
from pathlib import Path

import pytest

import scripts.ab_search as cli
from sherpa.ingest import office_md


# ===== A: 人間向けMD 見出し単位チャンク（_chunk_human_md） =====

def test_chunk_human_md_splits_on_h2_and_h3():
    text = "## シート「対象」\n\n### A1:C4\n\n| 項目ID |\n\n## シート「旧版」\n\n本文\n"
    chunks = cli._chunk_human_md(text)
    assert [c["heading"] for c in chunks] == ["シート「対象」", "A1:C4", "シート「旧版」"]
    assert "| 項目ID |" in chunks[1]["text"]
    assert "本文" in chunks[2]["text"]


def test_chunk_human_md_ignores_h1_and_h4():
    """`#`（単独）や `####` 以下は境界にしない（`##`/`###` のみが境界＝仕様どおり）。

    `# タイトル`（境界にならない見出し）は最初の `##` より前の見出し無し本文として1チャンクになる。
    """
    text = "# タイトル\n\n## 見出し\n\n#### 深い見出し\n本文\n"
    chunks = cli._chunk_human_md(text)
    assert len(chunks) == 2
    assert chunks[0]["heading"] is None
    assert chunks[0]["text"] == "# タイトル"
    assert chunks[1]["heading"] == "見出し"
    assert "#### 深い見出し" in chunks[1]["text"]     # `####` は境界にならず同じチャンクに含まれる


def test_chunk_human_md_leading_text_without_heading():
    text = "見出しの無い前置き\n\n## 見出し\n本文\n"
    chunks = cli._chunk_human_md(text)
    assert chunks[0]["heading"] is None
    assert chunks[0]["text"] == "見出しの無い前置き"
    assert chunks[1]["heading"] == "見出し"


def test_chunk_human_md_empty_text_returns_no_chunks():
    assert cli._chunk_human_md("") == []
    assert cli._chunk_human_md("   \n\n  ") == []


# ===== B: rag.md アンカー単位チャンク（_chunk_rag_md） =====

def test_chunk_rag_md_splits_on_anchors():
    text = ("# AI検索用文書\n\n"
            "<!-- chunk:rag-chunk:aaa -->\n本文1\n\n"
            "<!-- chunk:rag-chunk:bbb -->\n本文2\n")
    chunks = cli._chunk_rag_md(text)
    assert [c["chunk_id"] for c in chunks] == ["rag-chunk:aaa", "rag-chunk:bbb"]
    assert chunks[0]["text"] == "本文1"
    assert chunks[1]["text"] == "本文2"


def test_chunk_rag_md_no_anchor_returns_empty():
    assert cli._chunk_rag_md("見出しだけでアンカーが無いrag.md\n") == []


def test_chunk_rag_md_skips_empty_body():
    text = "<!-- chunk:rag-chunk:aaa -->\n\n\n<!-- chunk:rag-chunk:bbb -->\n本文\n"
    chunks = cli._chunk_rag_md(text)
    assert [c["chunk_id"] for c in chunks] == ["rag-chunk:bbb"]  # 空本文のチャンクは含めない


# ===== 表示名の連番ステージングディレクトリ除去（_doc_display_name） =====

def test_doc_display_name_strips_numeric_staging_prefix():
    assert cli._doc_display_name("000/DEP-XLSX-MARKERS.xlsx") == "DEP-XLSX-MARKERS.xlsx"
    assert cli._doc_display_name("001/subdir/file.docx") == "subdir/file.docx"


def test_doc_display_name_passthrough_without_prefix():
    assert cli._doc_display_name("plain.xlsx") == "plain.xlsx"


# ===== _collect_chunks（人間向けMD＋rag.mdの実ファイルからA/Bを組み立てる） =====

def test_collect_chunks_reads_both_layers(tmp_path):
    md_dir = tmp_path / "derived" / "md"
    md_dir.mkdir(parents=True)
    (md_dir / "doc.xlsx.md").write_text("## 見出し\n本文A\n", encoding="utf-8")
    rag_dir = office_md._sibling_layer_dir(md_dir, "rag")
    rag_dir.mkdir(parents=True)
    (rag_dir / "doc.xlsx.rag.md").write_text(
        "<!-- chunk:rag-chunk:1 -->\n本文B1\n\n<!-- chunk:rag-chunk:2 -->\n本文B2\n", encoding="utf-8")
    chunks_a, chunks_b, doc_count = cli._collect_chunks(md_dir, rag_dir)
    assert doc_count == 1
    assert len(chunks_a) == 1 and chunks_a[0]["doc"] == "doc.xlsx"
    assert len(chunks_b) == 2 and {c["chunk_id"] for c in chunks_b} == {"rag-chunk:1", "rag-chunk:2"}


def test_collect_chunks_document_without_rag_md_contributes_zero_to_b(tmp_path):
    """PDF/旧形式等 rag.md 非対応の文書は B へ0件を計上するだけで処理が止まらない。"""
    md_dir = tmp_path / "derived" / "md"
    md_dir.mkdir(parents=True)
    (md_dir / "legacy.pdf.md").write_text("## 見出し\n本文\n", encoding="utf-8")
    rag_dir = office_md._sibling_layer_dir(md_dir, "rag")
    chunks_a, chunks_b, doc_count = cli._collect_chunks(md_dir, rag_dir)
    assert doc_count == 1
    assert len(chunks_a) == 1
    assert chunks_b == []


# ===== 一時 index 作成（kuromoji→standard フォールバック・_create_index） =====

class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: bytes = b"analyzer [kuromoji] not found"):
        import io
        super().__init__("http://es/index", code, "err", {}, io.BytesIO(body))


def test_create_index_uses_kuromoji_when_available(monkeypatch):
    calls = []

    def fake_req(method, path, body=None, ndjson=False, timeout=30):
        calls.append((method, path, body))
        return {}

    monkeypatch.setattr(cli, "_es_req", fake_req)
    analyzer = cli._create_index("abtest_a_xxxx", None)
    assert analyzer == "kuromoji"
    assert calls[0][2]["mappings"]["properties"]["text"]["analyzer"] == "kuromoji"


def test_create_index_falls_back_to_standard_on_kuromoji_400(monkeypatch):
    attempts = []

    def fake_req(method, path, body=None, ndjson=False, timeout=30):
        analyzer = body["mappings"]["properties"]["text"]["analyzer"]
        attempts.append(analyzer)
        if analyzer == "kuromoji":
            raise _FakeHTTPError(400)
        return {}

    monkeypatch.setattr(cli, "_es_req", fake_req)
    analyzer = cli._create_index("abtest_b_xxxx", 1536)
    assert analyzer == "standard"
    assert attempts == ["kuromoji", "standard"]


def test_create_index_includes_embedding_field_only_with_dim(monkeypatch):
    bodies = []
    monkeypatch.setattr(cli, "_es_req", lambda method, path, body=None, ndjson=False, timeout=30: bodies.append(body) or {})
    cli._create_index("abtest_a_xxxx", None)
    assert "embedding" not in bodies[0]["mappings"]["properties"]
    bodies.clear()
    cli._create_index("abtest_a_yyyy", 768)
    assert bodies[0]["mappings"]["properties"]["embedding"] == {
        "type": "dense_vector", "dims": 768, "index": True, "similarity": "cosine"}


def test_create_index_raises_when_both_analyzers_fail(monkeypatch):
    monkeypatch.setattr(cli, "_es_req",
                        lambda *a, **k: (_ for _ in ()).throw(_FakeHTTPError(400)))
    with pytest.raises(RuntimeError):
        cli._create_index("abtest_a_xxxx", None)


# ===== _delete_index（best-effort・失敗を握りつぶす） =====

def test_delete_index_swallows_errors(monkeypatch):
    monkeypatch.setattr(cli, "_es_req", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom")))
    cli._delete_index("abtest_a_xxxx")     # 例外を送出しなければ良い


# ===== _bulk_index / _search（ESモック） =====

def test_bulk_index_noop_when_no_docs(monkeypatch):
    calls = []
    monkeypatch.setattr(cli, "_es_req", lambda *a, **k: calls.append(a) or {})
    cli._bulk_index("abtest_a_xxxx", [], None)
    assert calls == []


def test_bulk_index_sends_ndjson_and_vectors(monkeypatch):
    calls = []

    def fake_req(method, path, body=None, ndjson=False, timeout=30):
        calls.append((method, path, ndjson, body))
        return {}

    monkeypatch.setattr(cli, "_es_req", fake_req)
    docs = [{"doc": "d.xlsx", "heading": None, "chunk_id": "c1", "text": "本文"}]
    cli._bulk_index("abtest_b_xxxx", docs, [[0.1, 0.2]])
    bulk_call = [c for c in calls if c[1] == "/abtest_b_xxxx/_bulk"][0]
    assert bulk_call[2] is True                       # ndjson=True
    assert '"embedding": [0.1, 0.2]' in bulk_call[3] or "0.1" in bulk_call[3]
    refresh_call = [c for c in calls if c[1] == "/abtest_b_xxxx/_refresh"]
    assert refresh_call


def test_search_returns_hits_from_es_response(monkeypatch):
    fake_response = {"hits": {"hits": [
        {"_score": 5.2, "_source": {"doc": "d.xlsx", "chunk_id": "c1", "text": "本文"}},
    ]}}
    monkeypatch.setattr(cli, "_es_req", lambda *a, **k: fake_response)
    hits = cli._search("abtest_a_xxxx", "q", 3, None)
    assert hits == [{"score": 5.2, "doc": "d.xlsx", "heading": None, "chunk_id": "c1", "text": "本文"}]


def test_search_handles_es_exception_gracefully():
    def raising_req(*a, **k):
        raise RuntimeError("es down")
    import scripts.ab_search as cli_mod
    orig = cli_mod._es_req
    cli_mod._es_req = raising_req
    try:
        hits = cli_mod._search("abtest_a_xxxx", "q", 3, None)
    finally:
        cli_mod._es_req = orig
    assert hits == []


# ===== main(): 一時 index 名の隔離＋異常終了でも必ず削除される（受け入れ条件） =====

def _write_fake_derived(_wd, derived) -> dict:
    """`office_md.build_derived` の差し替え。実変換はせず、人間向けMD＋rag.md を直接書く。"""
    derived = Path(derived)
    derived.mkdir(parents=True, exist_ok=True)
    (derived / "doc.xlsx.md").write_text("## 見出し1\n本文A\n", encoding="utf-8")
    rag_dir = office_md._sibling_layer_dir(derived, "rag")
    rag_dir.mkdir(parents=True, exist_ok=True)
    (rag_dir / "doc.xlsx.rag.md").write_text(
        "<!-- chunk:rag-chunk:1 -->\n本文B1\n\n<!-- chunk:rag-chunk:2 -->\n本文B2\n", encoding="utf-8")
    return {}


def _patch_main_es(monkeypatch, *, created: list, deleted: list, bulk_side_effect=None):
    monkeypatch.setattr(cli, "_es_available", lambda: True)
    monkeypatch.setattr(cli.office_md, "build_derived", _write_fake_derived)
    monkeypatch.setattr(cli, "_create_index", lambda name, dim: created.append(name) or "kuromoji")
    monkeypatch.setattr(cli, "_delete_index", lambda name: deleted.append(name))
    monkeypatch.setattr(cli, "_search", lambda *a, **k: [])

    def fake_bulk(name, docs, vectors):
        if bulk_side_effect is not None:
            bulk_side_effect(name, docs, vectors)

    monkeypatch.setattr(cli, "_bulk_index", fake_bulk)


def test_main_creates_two_isolated_abtest_indices(monkeypatch, tmp_path, capsys):
    src = tmp_path / "input.xlsx"
    src.write_bytes(b"dummy")
    created, deleted = [], []
    _patch_main_es(monkeypatch, created=created, deleted=deleted)
    monkeypatch.setattr("sys.argv", ["ab_search.py", str(src), "-q", "テスト"])
    cli.main()
    assert len(created) == 2
    assert created[0] != created[1]
    assert all(n.startswith(cli._INDEX_PREFIX) for n in created)
    assert created[0].startswith(cli._INDEX_PREFIX + "a_")
    assert created[1].startswith(cli._INDEX_PREFIX + "b_")
    # 同じ実行内でA/B 2本の suffix は一致する（1回のランで対にした一時 index という設計）。
    assert created[0][len(cli._INDEX_PREFIX) + 2:] == created[1][len(cli._INDEX_PREFIX) + 2:]


def test_main_deletes_temp_indices_even_on_mid_run_exception(monkeypatch, tmp_path):
    """`_bulk_index` が例外を送出しても、作成した一時 index は finally で必ず削除される（受け入れ条件）。"""
    src = tmp_path / "input.xlsx"
    src.write_bytes(b"dummy")
    created, deleted = [], []

    def boom(name, docs, vectors):
        raise RuntimeError("bulk index failed")

    _patch_main_es(monkeypatch, created=created, deleted=deleted, bulk_side_effect=boom)
    monkeypatch.setattr("sys.argv", ["ab_search.py", str(src), "-q", "テスト"])
    with pytest.raises(RuntimeError, match="bulk index failed"):
        cli.main()
    assert len(created) == 2
    assert sorted(deleted) == sorted(created)          # 作成した2本とも削除呼び出しに含まれる


def test_main_deletes_temp_indices_on_normal_completion(monkeypatch, tmp_path):
    src = tmp_path / "input.xlsx"
    src.write_bytes(b"dummy")
    created, deleted = [], []
    _patch_main_es(monkeypatch, created=created, deleted=deleted)
    monkeypatch.setattr("sys.argv", ["ab_search.py", str(src), "-q", "テスト"])
    cli.main()
    assert sorted(deleted) == sorted(created)


def test_main_exits_cleanly_when_es_unavailable(monkeypatch, tmp_path):
    src = tmp_path / "input.xlsx"
    src.write_bytes(b"dummy")
    monkeypatch.setattr(cli, "_es_available", lambda: False)
    monkeypatch.setattr("sys.argv", ["ab_search.py", str(src), "-q", "テスト"])
    with pytest.raises(SystemExit) as exc_info:
        cli.main()
    assert "Elasticsearch" in str(exc_info.value.code)


def test_main_exits_cleanly_when_input_missing(monkeypatch, tmp_path):
    missing = tmp_path / "does_not_exist.xlsx"
    monkeypatch.setattr(cli, "_es_available", lambda: True)
    monkeypatch.setattr("sys.argv", ["ab_search.py", str(missing), "-q", "テスト"])
    with pytest.raises(SystemExit):
        cli.main()
