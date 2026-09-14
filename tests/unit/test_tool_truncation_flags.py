"""精読/検索の本文が上限で切れたときに `text_truncated` を明示する契約の単体テスト
（v1 フィクスチャ・Neo4j 不要）。

- read_around: `_clip_utf8_bytes` で実際に短くなったときだけ `text_truncated: True`。
- grep/es ヒット（`text_for_llm`）: 文字数では切らない（ヒット全文を渡す・`text_truncated` は
  付けない）。citation の `quote` だけ表示用の固定 500 字上限。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
from sherpa import agentic_search as A   # noqa: E402


# ===== read_around: バイト上限での切り詰め =====

def test_read_around_sets_text_truncated_when_clipped_by_small_budget():
    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    h = res["hits"][0]
    r, docs, _, _ = A.run_tool(
        "read_around", {"doc_id": h["doc_id"], "line": h["line"], "window": 2}, "v1", None,
        tool_result_max_bytes=5)
    assert "error" not in r, r
    assert r.get("text_truncated") is True
    assert len(r["text"].encode("utf-8")) <= 5
    assert h["doc_id"] in docs


def test_read_around_omits_text_truncated_when_budget_sufficient():
    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "TAX-RATE"}, "v1", None)
    h = res["hits"][0]
    r, _, _, _ = A.run_tool(
        "read_around", {"doc_id": h["doc_id"], "line": h["line"], "window": 2}, "v1", None)
    assert "error" not in r, r
    assert "text_truncated" not in r


# ===== grep/es ヒット本文（text_for_llm）: 文字数で切らない =====

def test_ripgrep_hit_keeps_full_text_and_omits_text_truncated(monkeypatch):
    long_text = "あ" * 2000
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: [
        {"doc_id": "a.md", "line": 1, "span": [1, 1], "text": long_text, "ext": ".md"},
    ])
    res, _, cites, _ = A.run_tool("ripgrep_search", {"query": "x"}, "v1", None)
    h = res["hits"][0]
    assert "text_truncated" not in h
    assert h["text"] == long_text
    # citation の quote（出典カード表示用）だけ固定 500 字上限。
    assert len(cites[0]["quote"]) == 500


def test_ripgrep_hit_omits_text_truncated_for_short_text(monkeypatch):
    monkeypatch.setattr(A.grep_tool, "grep_search", lambda *a, **kw: [
        {"doc_id": "a.md", "line": 1, "span": [1, 1], "text": "短いヒット本文", "ext": ".md"},
    ])
    res, _, _, _ = A.run_tool("ripgrep_search", {"query": "x"}, "v1", None)
    h = res["hits"][0]
    assert "text_truncated" not in h


def test_es_search_legacy_hit_keeps_full_text(monkeypatch):
    """chunk_id 無し（rag_chunks 由来ではない・従来型）ES ヒットは grep と同じ hit_view 経路を通る。"""
    from sherpa import documents, es_index
    long_text = "x" * 600
    monkeypatch.setattr(es_index, "search", lambda world, q, scope_paths=None, k=20, layer=None, **kw: (
        [{"doc_id": "a.md", "line": 3, "text": long_text, "ext": ".md"}], None))
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {"a.md"})
    res, _, cites, _ = A.run_tool("es_search", {"query": "q"}, "v1", None)
    h = res["hits"][0]
    assert "text_truncated" not in h
    assert h["text"] == long_text
    assert len(cites[0]["quote"]) == 500


def _setup_parent_return_world(monkeypatch, tmp_path, world: str, hits: list, rag_files: dict) -> None:
    """親返しテスト共通セットアップ（`tests/unit/test_agentic_search.py::_setup_parent_return_world`
    と同じ手法・cross-file import はしない）: `es_index.search`/`documents.world_rel_set` をスタブし、
    `rag_files`（`{doc_id: rag.md 本文}`）を `worlds.derived_rag_dir(world)` 配下へ書く
    （空 dict なら rag.md 無し＝どの doc も P3/P2 へ展開できない）。"""
    from sherpa import documents
    der_rag = tmp_path / "rag"
    der_rag.mkdir(parents=True, exist_ok=True)
    for doc_id, content in rag_files.items():
        (der_rag / (doc_id + ".rag.md")).write_text(content, encoding="utf-8")
    monkeypatch.setattr(A.worlds, "derived_rag_dir", lambda w: der_rag)
    monkeypatch.setattr(A.worlds, "derived_md_dir", lambda w: tmp_path / "md")   # legacy 無し
    monkeypatch.setattr(A.es_index, "search",
                        lambda w, q, scope_paths=None, k=20, layer=None, **kw: (list(hits), None))
    monkeypatch.setattr(documents, "world_rel_set", lambda w, **kw: {h["doc_id"] for h in hits})


def test_es_search_parent_return_chunk_tier_keeps_full_chunk_text(monkeypatch, tmp_path):
    """rag_chunks 由来（chunk_id あり）ヒットが予算/rag.md 不在で "full"/"region" へ展開できず
    tier="chunk" のまま残ったときも、子チャンク本文は文字数で切らず全文を渡す（`text_truncated` なし）。"""
    world = "parent-return-chunk-truncated-world"
    long_text = "y" * 600
    hits = [{"doc_id": "b.docx", "text": long_text, "ext": ".docx",
            "chunk_id": "c1", "parent_id": "p1", "score": 1.0}]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {})   # rag.md 無し＝展開不能
    monkeypatch.setattr(A.es_index, "chunk_ids_for_parent", lambda w, doc_id, parent_ids, limit=5000: [])
    res, _, _, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    h = res["hits"][0]
    assert h["tier"] == "chunk"
    assert "text_truncated" not in h
    assert h["text"] == long_text


def test_es_search_parent_return_full_tier_omits_text_truncated(monkeypatch, tmp_path):
    """同じ長い子チャンク本文でも "full" へ展開できたら `text_truncated` は付かない
    （最終 text は rag.md 由来の別文字列に置き換わる）。"""
    world = "parent-return-full-omits-truncated-world"
    long_text = "z" * 600
    full_md = "<!-- chunk:cf1 -->\n" + "F" * 50 + "\n"   # 予算に余裕で収まる小さな全文
    hits = [{"doc_id": "full.docx", "text": long_text, "ext": ".docx",
            "chunk_id": "cf1", "parent_id": "pf", "score": 1.0}]
    _setup_parent_return_world(monkeypatch, tmp_path, world, hits, {"full.docx": full_md})
    res, _, _, _ = A.run_tool("es_search", {"query": "q"}, world, None)
    h = res["hits"][0]
    assert h["tier"] == "full"
    assert "text_truncated" not in h
