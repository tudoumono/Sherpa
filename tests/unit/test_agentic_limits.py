"""利用統計「打ち切りの内訳」（`InvestigationState.limits`）の単体テスト。

内部制限（1件あたりのツール結果バイト予算・累計予算・会話履歴の文脈整理・清書入力の打ち切り・
検索系ツールの件数打ち切り）が実際に発動した回数を数えるだけの計測——制限そのものの値・挙動は
一切変えない契約をここで固定する。LLM は stub（コスト0・`test_agentic_search.py` と同じ流儀）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa import agentic_search as A  # noqa: E402
from sherpa import investigation_state  # noqa: E402
from sherpa import store  # noqa: E402


def _final_events(events: list) -> list:
    return [ev for ev in events if "final" in ev]


# ===== _record_run_tool_limits（run_tool 直後の分類・純関数） =====

def test_record_run_tool_limits_counts_search_truncated_for_listed_tools():
    state = investigation_state.InvestigationState(question="q", scope={})
    for name in ("ripgrep_search", "es_search", "glob_search", "graph_neighbors",
                "list_docs", "doc_outline"):
        A._record_run_tool_limits(state, name, {"truncated": True})
    assert state.limits["search_truncated"] == 6
    # 件数上限の truncated はバイト予算の切り詰めではない＝tool_result_clipped は増えない。
    assert state.limits["tool_result_clipped"] == 0


def test_record_run_tool_limits_ignores_truncated_for_unlisted_tool():
    state = investigation_state.InvestigationState(question="q", scope={})
    A._record_run_tool_limits(state, "read_around", {"truncated": True, "text": "x"})
    # read_around は search_truncated 対象外（byte clip 側の text_truncated で数える）。
    assert state.limits["search_truncated"] == 0


def test_record_run_tool_limits_counts_tool_result_clipped_for_byte_budget_tools():
    state = investigation_state.InvestigationState(question="q", scope={})
    A._record_run_tool_limits(state, "read_around", {"text_truncated": True})
    A._record_run_tool_limits(state, "read_doc", {"text_truncated": True})
    A._record_run_tool_limits(state, "pdf_pages", {"text_truncated": True})
    A._record_run_tool_limits(state, "graph_neighbors", {"truncated": True})      # 件数上限＝対象外
    A._record_run_tool_limits(state, "compare_documents", {"truncated": True})    # 件数上限＝対象外
    assert state.limits["tool_result_clipped"] == 3


def test_record_run_tool_limits_noop_on_error_result_and_non_dict():
    state = investigation_state.InvestigationState(question="q", scope={})
    A._record_run_tool_limits(state, "ripgrep_search", {"error": "x"})
    A._record_run_tool_limits(state, "ripgrep_search", None)
    assert state.limits["search_truncated"] == 0
    assert state.limits["tool_result_clipped"] == 0


# ===== InvestigationState.limits ヘルパー =====

def test_investigation_state_bump_and_mark_limit():
    state = investigation_state.InvestigationState(question="q", scope={})
    assert state.limits == {
        "tool_result_clipped": 0, "total_budget_hit": False, "context_compactions": 0,
        "synthesis_truncated": False, "search_truncated": 0, "auto_continues": 0,
    }
    state.bump_limit("auto_continues")
    state.bump_limit("auto_continues", 2)
    state.mark_limit("total_budget_hit")
    assert state.limits["auto_continues"] == 3
    assert state.limits["total_budget_hit"] is True


# ===== openai_style ループ経由（final payload の limits） =====

def test_openai_style_search_truncated_propagates_to_final_limits(monkeypatch):
    """探す系ツールが `truncated: true` を1回返したターンは `limits["search_truncated"] == 1`。"""
    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": [{"doc_id": "a.md", "span": [1, 1], "text": "x"}], "truncated": True},
               {"a.md"}, [{"doc_id": "a.md", "span": [1, 1], "quote": "x"}], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
           {"choices": [{"message": {"content": "回答"}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    finals = _final_events(events)
    assert finals and finals[-1]["limits"]["search_truncated"] == 1
    assert finals[-1]["limits"]["tool_result_clipped"] == 0


def test_openai_style_tool_result_clipped_propagates_to_final_limits(monkeypatch):
    """read_around が `text_truncated: true` を返した回数がそのまま `tool_result_clipped` に載る。"""
    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"doc_id": "a.md", "text": "x" * 10, "text_truncated": True, "start_line": 1, "end_line": 1},
               {"a.md"}, [], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "read_around", "arguments": '{"doc_id":"a.md"}'}}]}}]},
           {"choices": [{"message": {"content": "回答"}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    finals = _final_events(events)
    assert finals and finals[-1]["limits"]["tool_result_clipped"] == 1


def test_openai_style_no_limits_key_when_nothing_triggered():
    """何も制限に当たらないターンは `limits` キー自体を作らない（旧行=0件として集計する契約の裏）。"""
    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": []}, set(), [], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]},
           {"choices": [{"message": {"content": "回答"}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    finals = _final_events(events)
    assert finals and "limits" not in finals[-1]


def test_openai_style_total_budget_hit_sets_limits(monkeypatch):
    """1 run 累計のツール結果バイト予算超過で打ち切られたターンは `limits["total_budget_hit"]` が真。
    （`test_openai_style_budget_snapshotted_once_settings_change_mid_run_has_no_effect` と同じ縮退手法
    ＝小さい予算 settings で確実に超過させる。）"""
    monkeypatch.setattr(store, "get_system_settings", lambda **kw: {"agentic_budget_total": 4096})

    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": [{"doc_id": "x.md", "line": 1, "text": "x" * 5000}]}, set(), [], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
           {"choices": [{"message": {"content": "final answer (should not be reached)"}}]}]
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "消費税率は?", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    finals = _final_events(events)
    assert finals and finals[-1]["limits"]["total_budget_hit"] is True
    assert finals[-1]["stop_reason"] == "budget_exceeded"


# ===== build_synthesis_digest の synthesis_truncated =====

def test_build_synthesis_digest_reports_truncated_when_over_budget():
    citations = [{"doc_id": f"d{i}.md", "span": [1, 1], "quote": "x" * 200} for i in range(50)]
    meta = [{"doc_id": c["doc_id"], "span": c["span"]} for c in citations]
    digest, ev_map, truncated = A.build_synthesis_digest(citations, meta, max_bytes=512)
    assert truncated is True
    assert len(ev_map) < len(citations)


def test_build_synthesis_digest_not_truncated_when_all_fit():
    citations = [{"doc_id": "d.md", "span": [1, 1], "quote": "x"}]
    meta = [{"doc_id": "d.md", "span": [1, 1]}]
    digest, ev_map, truncated = A.build_synthesis_digest(citations, meta)
    assert truncated is False


def test_count_limit_truncated_on_outline_is_not_a_byte_clip():
    """doc_outline の件数上限 `truncated` は 1 件バイト予算の切り詰めではない＝tool_result_clipped を増やさない。"""
    from sherpa import agentic_search as A, investigation_state as I
    st = I.InvestigationState(question="q", scope={})
    A._record_run_tool_limits(st, "doc_outline", {"headings": [], "truncated": True})
    assert st.limits["tool_result_clipped"] == 0
    A._record_run_tool_limits(st, "read_around", {"text": "x", "text_truncated": True})
    assert st.limits["tool_result_clipped"] == 1


def test_resynthesis_digest_truncation_marks_limit(monkeypatch):
    """引用検証後の再合成入力が予算で省略されたら synthesis_truncated を立てる。"""
    from sherpa import agentic_search as A
    monkeypatch.setattr(A, "build_synthesis_digest", lambda *a, **k: ("d", {}, True))
    limits = {"synthesis_truncated": False}
    A._committed_evidence_digest([{"doc_id": "a.md", "span": [1, 1], "quote": "q"}],
                                 evidence_meta=[{"doc_id": "a.md", "span": [1, 1]}], limits=limits)
    assert limits["synthesis_truncated"] is True


def test_codex_provider_without_cli_does_not_reference_unbound_counter(monkeypatch):
    """Codex CLI が無い（起動しない）経路でも run() が UnboundLocalError にならない。"""
    import shutil
    from sherpa.providers.codex import provider as P
    monkeypatch.setattr(shutil, "which", lambda name: None)
    src = open(P.__file__, encoding="utf-8").read()
    # 初期化が起動条件（shutil.which）より前にあることを固定する（起動しない経路の参照安全）。
    assert src.index("_auto_continue_count = 0") < src.index('if shutil.which("codex") and ws_authoring')


def test_openai_style_parallel_tool_calls_are_counted(monkeypatch):
    """1 応答に 2 本のツール呼び出し（既定の並列経路）でも search_truncated が両方数えられる。"""
    def fake_run_tool(name, args, world, scope_paths, **kw):
        return ({"hits": [{"doc_id": "a.md", "span": [1, 1], "text": "x"}], "truncated": True},
               {"a.md"}, [{"doc_id": "a.md", "span": [1, 1], "quote": "x"}], [])

    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}},
               {"id": "c2", "function": {"name": "es_search", "arguments": '{"query":"y"}'}}]}}]},
           {"choices": [{"message": {"content": "回答"}}]}]
    monkeypatch.setattr(A, "SHERPA_TOOL_PARALLEL", 3)
    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    finals = _final_events(events)
    assert finals and finals[-1]["limits"]["search_truncated"] == 2


def test_reader_tool_byte_clip_is_counted():
    """読取系ツール（file_head/pdf_pages 等）のバイト上限クリップは `truncated` で申告される＝数える。"""
    from sherpa import investigation_state as I
    st = I.InvestigationState(question="q", scope={})
    A._record_run_tool_limits(st, "file_head", {"text": "x", "truncated": True, "byte_clipped": True})
    A._record_run_tool_limits(st, "pdf_pages", {"pages": [], "truncated": True, "byte_clipped": True})
    A._record_run_tool_limits(st, "xlsx_range", {"rows": [], "truncated": True})   # 行数上限＝数えない
    assert st.limits["tool_result_clipped"] == 2


def test_synthesis_digest_marks_truncated_when_list_paths_omitted(monkeypatch):
    """一覧のパスが予算で未提示になったら清書入力の打ち切りとして True を返す。"""
    monkeypatch.setattr(A, "_synthesis_list_paths", lambda matched, budget: ("a.md", 4))
    meta = [{"doc_id": None, "matched_doc_ids": ["a.md", "b.md", "c.md", "d.md", "e.md"],
             "list_meta": {"count": 5, "shown": 5}}]
    _digest, _ev, truncated = A.build_synthesis_digest([], meta)
    assert truncated is True


def test_reader_finisher_marks_byte_clip_distinct_from_row_cap():
    """`_finish_reader_result` がバイト予算で切ったときだけ `byte_clipped` が立つ（行数上限だけでは立たない）。"""
    long_rows = [{"row": i, "cells": ["x" * 50]} for i in range(40)]
    out = A._finish_reader_result("xlsx_range", {"rows": long_rows, "truncated": True}, "a.xlsx", 400)
    assert out.get("byte_clipped") is True
    small = A._finish_reader_result("xlsx_range", {"rows": long_rows[:2], "truncated": True}, "a.xlsx", 100000)
    assert not small.get("byte_clipped")


def test_docx_paragraph_shrink_marks_byte_clip():
    """docx の段落削減（表の救済ではない経路）でも byte_clipped が立つ。"""
    paras = [{"index": i, "text": "x" * 100} for i in range(20)]
    out = A._finish_docx_paragraphs_result({"paragraphs": paras, "tables": []}, "a.docx", 1024)
    assert out.get("truncated") is True and out.get("byte_clipped") is True


def test_compare_documents_within_budget_is_not_clipped():
    """予算内の diff は先引きで切られない（byte_clipped も立たない）。"""
    diff_text = "a" * 1024
    result = {"status": "ok", "diff": diff_text, "left": {"doc_id": "a.md"}, "right": {"doc_id": "b.md"}}
    from sherpa import agentic_search as A2
    clipped = A2._clip_utf8_bytes(diff_text, 1024)
    assert clipped == diff_text      # 予算ちょうどは切らない前提の確認
