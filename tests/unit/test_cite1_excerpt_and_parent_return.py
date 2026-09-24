"""CITE-1（H3/SC-4 の生き残り核）の受け入れ条件テスト。

正典: `docs/proposals/2026-08-22-検索接続切替.md` §9（SC-4 追加要件）・
`docs/proposals/2026-08-28-人間向けMDの刷新.md` §7（H3）・
`docs/proposals/2026-09-02-RAG表現の全形式展開と文脈保持.md` §3.3/§3.4（非agentic への適用範囲拡張）。

- excerpts.py: rag チャンクの locator/chunk_id/span から人間向け MD の該当節へ引き直せること・
  対応不能時のフォールバック＋位置ヒントを固定する。
- rag_parent_return.py: 非agentic の親返し（P3/P2/chunk・tier 申告・サイズ操作）を固定する。
- lens_service/chat_service の実配線: rag の KV 文（「項目ID: 「ITEM01」」）が利用者向けには出ず、
  実表＋位置ヒントになることを実データ（DEP-XLSX-MARKERS）で固定する。
- agentic 経路（agentic_search.py）は本レーンで一切変更していない（import すらしない）ことも確認する。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
from pathlib import Path

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

from sherpa import citations, excerpts, rag_parent_return, worlds
from sherpa.ingest import office_md

_ROOT = Path(__file__).resolve().parents[2]
_DEP_INPUTS = _ROOT / "fixtures" / "eval" / "deprecation_markers" / "inputs"


@pytest.fixture()
def _xlsx_world(monkeypatch, tmp_path):
    """DEP-XLSX-MARKERS を実際に build_derived() へ通し、world id を返す（実ファイル往復）。"""
    if not (_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx").is_file():
        pytest.skip(f"fixture が無い環境: {_DEP_INPUTS / 'DEP-XLSX-MARKERS.xlsx'}")
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    wd = tmp_path / "world"
    wd.mkdir()
    shutil.copy(_DEP_INPUTS / "DEP-XLSX-MARKERS.xlsx", wd / "DEP-XLSX-MARKERS.xlsx")
    world = "cite1-xlsx"
    rep = office_md.build_derived(wd, worlds.derived_md_dir(world))
    assert rep["rag_failed"] == 0, rep
    return world


_DOC_ID = "DEP-XLSX-MARKERS.xlsx"


def _first_row_for_sheet(world: str, sheet: str) -> dict:
    """`sheet` の**表領域レコード**（`region_context` 持ち）を1件返す。同じシートでも strike/occlusion
    の metadata-only レコードや画像レコードは `region_context=None`（表の範囲を持たない）ため除外する。
    """
    rows = [json.loads(line) for line in
            (worlds.derived_rag_dir(world) / (_DOC_ID + ".rag_chunks.jsonl")).read_text(
                encoding="utf-8").splitlines() if line.strip()]
    for row in rows:
        if row.get("document_context", {}).get("sheet") == sheet and row.get("region_context"):
            return row
    raise AssertionError(f"no region row for sheet={sheet}")


# ---- excerpts.resolve_human_excerpt / display_quote（実データ）----

def test_resolve_human_excerpt_via_chunk_id_returns_pipe_table(_xlsx_world):
    row = _first_row_for_sheet(_xlsx_world, "対象")
    chunk_id = row["chunk_id"]
    locator = row["citations"][0]["locator"]
    section_path = row["section_path"]
    result = excerpts.resolve_human_excerpt(
        _xlsx_world, _DOC_ID, chunk_id=chunk_id, locator=locator, section_path=section_path)
    assert result is not None
    assert "| 項目ID | 状態 | 内部コード |" in result["text"]
    assert "ITEM01" in result["text"]
    assert result["hint"] == "シート「対象」A1:C4"


def test_resolve_human_excerpt_kv_line_never_appears(_xlsx_world):
    """rag の KV 文（「項目ID: 「ITEM01」」）が利用者向けの解決結果に出ないことを固定する。"""
    row = _first_row_for_sheet(_xlsx_world, "対象")
    result = excerpts.resolve_human_excerpt(
        _xlsx_world, _DOC_ID, chunk_id=row["chunk_id"],
        locator=row["citations"][0]["locator"], section_path=row["section_path"])
    assert result is not None
    assert "項目ID: 「ITEM01」" not in result["text"]
    assert "状態: 「廃止予定」" not in result["text"]


def test_display_quote_grep_span_resolves_same_table(_xlsx_world):
    """grep ヒット（span のみ・locator 無し）でも rag.md のアンカーを逆引きして同じ節へたどり着く。"""
    from sherpa.grep_tool import grep_search
    hits = grep_search("ITEM01", _xlsx_world)
    assert hits
    h = hits[0]
    assert "項目ID: 「ITEM01」" in h["text"]        # 前提: grep 自体は rag.md の KV 文をヒットさせている
    disp = excerpts.display_quote(_xlsx_world, h["doc_id"], h["text"], span=h["span"])
    assert disp["excerpt_source"] == "human_md"
    assert "| 項目ID | 状態 | 内部コード |" in disp["quote"]
    assert "項目ID: 「ITEM01」" not in disp["quote"]
    assert disp["locator_hint"] == "シート「対象」A1:C4"


def test_display_quote_falls_back_when_locator_unresolvable(_xlsx_world):
    """docx/pptx 相当（sheet を持たない locator）は rag 文へフォールバックし、hint は取れるだけ付く。"""
    disp = excerpts.display_quote(_xlsx_world, _DOC_ID, "FALLBACK TEXT", locator={"page": 3})
    assert disp == {"quote": "FALLBACK TEXT", "excerpt_source": "rag", "locator_hint": "p.3"}


def test_display_quote_falls_back_for_nonexistent_doc():
    disp = excerpts.display_quote("no-such-world", "missing.xlsx", "FALLBACK",
                                  locator={"sheet": "x", "cell_range": "A1"})
    assert disp["quote"] == "FALLBACK"
    assert disp["excerpt_source"] == "rag"
    assert disp["locator_hint"] == "シート「x」A1"


def test_display_quote_without_any_locator_or_span_is_pure_fallback():
    disp = excerpts.display_quote("w", "doc.xlsx", "FALLBACK")
    assert disp == {"quote": "FALLBACK", "excerpt_source": "rag", "locator_hint": None}


def test_resolve_human_excerpt_rejects_traversal_doc_id():
    assert excerpts.resolve_human_excerpt("w", "../evil.xlsx", chunk_id="x",
                                          locator={"sheet": "s", "cell_range": "A1"}) is None


# ---- citations.py 加算的フィールド ----

def test_with_display_excerpt_is_additive():
    base = citations.from_es_hit({"doc_id": "d.xlsx", "line": 3, "text": "ORIG", "ext": ".xlsx"}, "q")
    out = citations.with_display_excerpt(base, quote="NEW", excerpt_source="human_md",
                                         locator_hint="シート「x」A1", tier="full")
    assert out["quote"] == "NEW"
    assert out["excerpt_source"] == "human_md"
    assert out["locator_hint"] == "シート「x」A1"
    assert out["tier"] == "full"
    assert out["doc_id"] == base["doc_id"] and out["span"] == base["span"]   # 既存フィールドは維持
    assert "locator" not in out and "chunk_id" not in out                   # SC-3 の不変条件は継続


def test_with_display_excerpt_omits_absent_optional_keys():
    base = citations.from_grep_hit({"doc_id": "d.md", "span": [1, 2], "text": "x", "match": "q", "ext": ".md"})
    out = citations.with_display_excerpt(base, quote="x", excerpt_source="rag", locator_hint=None, tier=None)
    assert "locator_hint" not in out and "tier" not in out


def test_with_display_text_is_additive():
    base = citations.public_grep_hit({"doc_id": "d.md", "line": 1, "span": [1, 1], "text": "old", "match": "q"})
    out = citations.with_display_text(base, text="new", excerpt_source="human_md", locator_hint="p.1")
    assert out["text"] == "new" and out["excerpt_source"] == "human_md" and out["locator_hint"] == "p.1"


# ---- rag_parent_return.py（サイズ操作・tier 申告）----

def _write_rag_md_and_chunks(rag_root: Path, doc_id: str, records: list[tuple[str, str]]) -> None:
    """`records`: [(chunk_id, body_text), ...]。1レコード=1見出し「## S」+ アンカーで rag.md を組む。"""
    rag_root.mkdir(parents=True, exist_ok=True)
    lines = []
    for cid, body in records:
        lines.append(f"<!-- chunk:{cid} -->")
        lines.append(body)
        lines.append("")
    (rag_root / (doc_id + ".rag.md")).write_text("\n".join(lines), encoding="utf-8")


def test_resolve_parent_return_chunk_tier_when_budget_tiny(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    world = "cite1-pr-1"
    rag_root = worlds.derived_rag_dir(world)
    _write_rag_md_and_chunks(rag_root, "d.xlsx", [("c1", "A" * 50), ("c2", "B" * 50)])
    groups = {"d.xlsx": [{"chunk_id": "c1", "parent_id": None, "score": 1.0, "text": "A" * 50},
                         {"chunk_id": "c2", "parent_id": None, "score": 1.0, "text": "B" * 50}]}
    out = rag_parent_return.resolve_parent_return(world, groups, budget_for_rag=10)
    assert out == [{"doc_id": "d.xlsx", "tier": "chunk",
                    "text": ("A" * 50) + "\n\n" + ("B" * 50), "chunk_ids": ["c1", "c2"]}]


def test_resolve_parent_return_full_tier_when_budget_generous(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    world = "cite1-pr-2"
    rag_root = worlds.derived_rag_dir(world)
    _write_rag_md_and_chunks(rag_root, "d.xlsx", [("c1", "A" * 50), ("c2", "B" * 50)])
    groups = {"d.xlsx": [{"chunk_id": "c1", "parent_id": None, "score": 1.0, "text": "A" * 50},
                         {"chunk_id": "c2", "parent_id": None, "score": 1.0, "text": "B" * 50}]}
    out = rag_parent_return.resolve_parent_return(world, groups, budget_for_rag=1_000_000)
    assert out[0]["tier"] == "full"
    assert "A" * 50 in out[0]["text"] and "B" * 50 in out[0]["text"]


def test_resolve_parent_return_region_tier_between_chunk_and_full(monkeypatch, tmp_path):
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    world = "cite1-pr-3"
    rag_root = worlds.derived_rag_dir(world)
    # 3レコード・region は c1/c2（同じ parent_id）だけ、c3 は別 region（他ドキュメント文脈のノイズ）。
    _write_rag_md_and_chunks(rag_root, "d.xlsx", [
        ("c1", "A" * 100), ("c2", "B" * 100), ("c3", "C" * 5000)])
    groups = {"d.xlsx": [{"chunk_id": "c1", "parent_id": "region-1", "score": 1.0, "text": "A" * 100},
                         {"chunk_id": "c2", "parent_id": "region-1", "score": 1.0, "text": "B" * 100}]}
    # full（c1+c2+c3 全体）は入らないが、c1/c2 のヒット自身の chunk_id を対象にした region なら入る予算。
    out = rag_parent_return.resolve_parent_return(world, groups, budget_for_rag=400)
    assert out[0]["tier"] == "region"
    assert "A" * 100 in out[0]["text"] and "B" * 100 in out[0]["text"]
    assert "C" * 5000 not in out[0]["text"]


def test_resolve_parent_return_reports_tier_for_every_doc_never_drops():
    """doc は必ず1件返る（黙って消えない）——rag.md が存在しない doc は chunk のまま。"""
    groups = {"missing.xlsx": [{"chunk_id": "c1", "parent_id": None, "score": 1.0, "text": "X"}]}
    out = rag_parent_return.resolve_parent_return("no-such-world", groups, budget_for_rag=1_000_000)
    assert out == [{"doc_id": "missing.xlsx", "tier": "chunk", "text": "X", "chunk_ids": ["c1"]}]


def test_parent_return_enabled_always_true_no_env_toggle():
    """TOGGLE-RM（2026-09-03）: グローバルな系統切替トグルは撤去済み・常時 True（env に一切
    左右されない）。"""
    assert rag_parent_return.parent_return_enabled() is True


def test_apply_to_hits_disabled_returns_input_unchanged(monkeypatch):
    """`parent_return_enabled` は今も内部シームとして残るため、直接差し替えて False 分岐を
    引き続き検証する（env では OFF にできない）。"""
    monkeypatch.setattr(rag_parent_return, "parent_return_enabled", lambda: False)
    hits = [{"doc_id": "d.xlsx", "chunk_id": "c1", "text": "x", "score": 1.0}]
    assert rag_parent_return.apply_to_hits("w", hits) is hits


def test_apply_to_hits_passes_through_non_rag_hits():
    hits = [{"doc_id": "legacy.txt", "line": 3, "text": "no chunk_id here", "score": 1.0}]
    assert rag_parent_return.apply_to_hits("w", hits) == hits


def test_excerpt_budget_bytes_default_and_env_bounds(monkeypatch):
    monkeypatch.delenv("SHERPA_CHAT_ES_EXCERPT_BUDGET_BYTES", raising=False)
    assert rag_parent_return.excerpt_budget_bytes() == rag_parent_return.DEFAULT_BUDGET_BYTES
    monkeypatch.setenv("SHERPA_CHAT_ES_EXCERPT_BUDGET_BYTES", "not-a-number")
    assert rag_parent_return.excerpt_budget_bytes() == rag_parent_return.DEFAULT_BUDGET_BYTES
    monkeypatch.setenv("SHERPA_CHAT_ES_EXCERPT_BUDGET_BYTES", "999999999999")   # out of range
    assert rag_parent_return.excerpt_budget_bytes() == rag_parent_return.DEFAULT_BUDGET_BYTES
    monkeypatch.setenv("SHERPA_CHAT_ES_EXCERPT_BUDGET_BYTES", "65536")
    assert rag_parent_return.excerpt_budget_bytes() == 65536


# ---- 実配線: lens_service.run_qa / chat_service._es_citations ----

def test_run_qa_citations_show_table_not_kv_text(_xlsx_world):
    from sherpa import lens_service
    result = lens_service.run_qa("ITEM01", _xlsx_world)
    assert result["citations"], result
    quotes = [c["quote"] for c in result["citations"] if c["doc_id"] == _DOC_ID]
    assert quotes, result["citations"]
    assert any("| 項目ID | 状態 | 内部コード |" in q for q in quotes)
    assert not any("項目ID: 「ITEM01」" in q for q in quotes)
    assert any(c.get("excerpt_source") == "human_md" for c in result["citations"] if c["doc_id"] == _DOC_ID)


def test_es_citations_excerpt_swap_and_tier(monkeypatch, _xlsx_world):
    from sherpa import chat_service, documents, es_index
    row = _first_row_for_sheet(_xlsx_world, "対象")
    chunk_id = row["chunk_id"]
    locator = row["citations"][0]["locator"]
    section_path = row["section_path"]

    def fake_search(world, query, scope_paths=None, k=8, vector=True, layer=None, **kw):
        return ([{"doc_id": _DOC_ID, "line": None, "text": "項目ID: 「ITEM01」", "score": 3.0,
                  "ext": ".xlsx", "chunk_id": chunk_id, "locator": locator,
                  "section_path": section_path, "parent_id": row.get("parent_id")}], None)

    monkeypatch.setattr(es_index, "search", fake_search)
    monkeypatch.setattr(documents, "world_rel_set", lambda world, **kw: {_DOC_ID})
    cites = chat_service._es_citations(_xlsx_world, "ITEM01", None)
    assert len(cites) == 1
    c = cites[0]
    assert c["excerpt_source"] == "human_md"
    assert "| 項目ID | 状態 | 内部コード |" in c["quote"]
    assert "項目ID: 「ITEM01」" not in c["quote"]
    assert c["locator_hint"] == "シート「対象」A1:C4"
    assert "locator" not in c and "chunk_id" not in c   # SC-3 の不変条件は継続


# ---- agentic 経路は変更していないことの確認（byte-identical 契約）----

def test_excerpts_module_does_not_import_agentic_search():
    import sherpa.excerpts as m
    assert "agentic_search" not in m.__dict__
    assert not hasattr(m, "agentic_search")


def test_rag_parent_return_module_has_no_forbidden_imports():
    """`search_service.py` の分離境界（api/agents/chat_service/chat_router/grep_tool を import しない）
    を共有部品側も満たすことを確認する——`search_service.py` から import できる前提。"""
    import sherpa.rag_parent_return as m
    src_globals = set(m.__dict__.keys())
    for forbidden in ("api", "agents", "chat_service", "chat_router", "grep_tool", "agentic_search"):
        assert forbidden not in src_globals
