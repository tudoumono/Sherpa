"""`sherpa.investigation_state.InvestigationState` の単体テスト（純粋な Python・LLM 不使用・コスト0）。

C（調査結果集約と並列実行の改善方針・§「質問ごとの調査状態をアプリが管理する」）: 1質問1調査状態が
(1) ev_id の永続性（追加・重複・並べ替えで変わらない）、(2) gaps の機械生成（モデルの散文を事実として
取り込まない）、(3) render の予算打ち切りと注記、を満たすことを固定する。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import sherpa.investigation_state as IS  # noqa: E402
from sherpa.investigation_state import Evidence, InvestigationState, ToolCall  # noqa: E402


def _state() -> InvestigationState:
    return InvestigationState(question="TAX-RATEは?", scope={"world": "v1"})


# ===== ev_id の永続性 =====

def test_ev_id_assigned_in_insertion_order():
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "a"}, {"hits": [{"doc_id": "x.md"}]},
                      [{"doc_id": "x.md", "span": [1, 1], "quote": "A"}], None)
    s.add_tool_result("ripgrep_search", {"query": "b"}, {"hits": [{"doc_id": "y.md"}]},
                      [{"doc_id": "y.md", "span": [2, 2], "quote": "B"}], None)
    assert [e.ev_id for e in s.evidence] == ["ev-1", "ev-2"]
    assert s.evidence[0].doc_id == "x.md" and s.evidence[1].doc_id == "y.md"


def test_duplicate_same_doc_and_span_does_not_renumber_but_upgrades_verification():
    """同 doc/span の再取得は ev_id を変えず1件に吸収する——検証前（"unverified"）から
    確定値（"verified"）への昇格だけを反映する（重複排除で再採番しない契約）。"""
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "a"}, {"hits": [{"doc_id": "x.md"}]},
                      [{"doc_id": "x.md", "span": [1, 1], "quote": "A"}], None)
    assert len(s.evidence) == 1
    ev_id_before = s.evidence[0].ev_id
    assert s.evidence[0].verification == "unverified"
    s.add_tool_result("sub_loop", {}, {}, [{"doc_id": "x.md", "span": [1, 1], "quote": "A"}],
                      [{"doc_id": "x.md", "span": [1, 1], "verification_method": "span_verified"}])
    assert len(s.evidence) == 1   # 新規 ev-N を採番しない
    assert s.evidence[0].ev_id == ev_id_before
    assert s.evidence[0].verification == "verified"


def test_ev_id_stable_when_same_evidence_readded_in_different_order():
    """並べ替え（呼び出し元が違う順序で同じ根拠集合を渡す）でも、既存根拠の ev_id は変わらない。"""
    s = _state()
    s.add_tool_result("t1", {}, {}, [
        {"doc_id": "a.md", "span": [1, 1], "quote": "A"},
        {"doc_id": "b.md", "span": [2, 2], "quote": "B"},
    ], None)
    a_id = next(e.ev_id for e in s.evidence if e.doc_id == "a.md")
    b_id = next(e.ev_id for e in s.evidence if e.doc_id == "b.md")
    s.add_tool_result("t2", {}, {}, [   # 逆順で再度渡す（重複排除で吸収されるだけ）
        {"doc_id": "b.md", "span": [2, 2], "quote": "B"},
        {"doc_id": "a.md", "span": [1, 1], "quote": "A"},
    ], None)
    assert len(s.evidence) == 2
    assert next(e.ev_id for e in s.evidence if e.doc_id == "a.md") == a_id
    assert next(e.ev_id for e in s.evidence if e.doc_id == "b.md") == b_id


def test_distinct_span_on_same_doc_gets_separate_ev_id():
    s = _state()
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "A"}], None)
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [5, 5], "quote": "C"}], None)
    assert [e.ev_id for e in s.evidence] == ["ev-1", "ev-2"]
    assert {e.span for e in s.evidence} == {(1, 1), (5, 5)}


# ===== gaps の機械生成 =====

def test_gaps_zero_hit_is_mechanical():
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "税率"}, {"hits": []}, [], None)
    assert any("税率" in g and "0件" in g for g in s.gaps)


def test_gaps_truncated_read_around():
    s = _state()
    s.add_tool_result("read_around", {"doc_id": "x.md", "line": 120},
                      {"doc_id": "x.md", "text": "1: a", "text_truncated": True}, [], None)
    assert any("上限で切断" in g for g in s.gaps)


def test_gaps_index_unavailable_when_es_degrades_to_keyword_only():
    s = _state()
    s.add_tool_result("es_search", {"query": "x"},
                      {"hits": [], "degrade_reason": "es_unavailable"}, [], None)
    assert any("索引なし" in g for g in s.gaps)


def test_gaps_error_is_recorded():
    s = _state()
    s.add_tool_result("read_around", {"doc_id": "missing.md", "line": 1},
                      {"error": "doc not found"}, [], None)
    assert any("doc not found" in g for g in s.gaps)


def test_gaps_stay_empty_on_normal_success():
    """正常系（1件以上ヒット・エラー無し・切断無し）は gap を作らない——モデルの散文を事実として
    取り込まないのと対称に、機械判定に該当しない限り何も足さない。"""
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "x"}, {"hits": [{"doc_id": "a.md"}]},
                      [{"doc_id": "a.md", "span": [1, 1], "quote": "hit"}], None)
    assert s.gaps == []


# ===== render: 予算打ち切りと注記 =====

def test_render_omits_nothing_and_no_notice_when_everything_fits():
    s = _state()
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "short"}], None)
    out = s.render(max_bytes=4096)
    assert "省略" not in out
    assert "a.md" in out and "short" in out


def test_render_truncates_from_front_keeping_most_recent_with_notice():
    """予算超過時は**古い根拠から**落とす（`build_synthesis_digest` の「新しい方から打ち切る」とは
    逆）——直近の発見（不足軸を埋める新規根拠等）を優先して残す契約。"""
    s = _state()
    for i in range(20):
        s.add_tool_result(f"t{i}", {}, {}, [{"doc_id": f"{i}.md", "span": [1, 1], "quote": "x" * 50}], None)
    out = s.render(max_bytes=400)
    assert len(out.encode("utf-8")) <= 400
    assert "省略" in out
    assert "19.md" in out    # 直近（末尾）は残る
    assert "0.md" not in out   # 最も古いものから落ちる


def test_render_truncation_notice_never_exceeds_max_bytes():
    s = _state()
    for i in range(50):
        s.add_tool_result(f"t{i}", {}, {}, [{"doc_id": f"{i}.md", "span": [1, 1], "quote": "y" * 30}], None)
    for budget in (128, 256, 512, 1024):
        out = s.render(max_bytes=budget)
        assert len(out.encode("utf-8")) <= budget


def test_render_keep_recent_tools_trims_call_log_but_keeps_all_evidence():
    """`keep_recent_tools` は「呼び出し記録」セクションだけを削る（会話履歴に生のまま残っている
    直近分の二重記載を避ける）——根拠（evidence）は常に全件のまま失わない。"""
    s = _state()
    for i in range(5):
        s.add_tool_result("ripgrep_search", {"query": f"q{i}"}, {"hits": [{"doc_id": f"{i}.md"}]},
                          [{"doc_id": f"{i}.md", "span": [1, 1], "quote": f"quote{i}"}], None)
    out_all = s.render(max_bytes=8192, keep_recent_tools=0)
    out_trim = s.render(max_bytes=8192, keep_recent_tools=2)
    for i in range(5):
        assert f"quote{i}" in out_all and f"quote{i}" in out_trim
    assert out_all.count("ripgrep_search『q") == 5
    assert out_trim.count("ripgrep_search『q") == 3   # 直近2件は呼び出し記録から除かれる


def test_render_empty_state_returns_empty_string():
    assert _state().render(max_bytes=4096) == ""


# ===== kind="read"（精読・read_around/read_doc）=====

def test_read_around_evidence_recovers_span_from_line_numbered_text():
    s = _state()
    text = "\n".join(f"{i}: line{i}" for i in range(10, 21))
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 15}, {"doc_id": "a.md", "text": text}, [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert ev.doc_id == "a.md"
    assert ev.span == (10, 20)


def test_read_doc_evidence_uses_start_end_line_directly():
    s = _state()
    result = {"doc_id": "a.md", "start_line": 5, "end_line": 40, "text": "5: x\n...\n40: y"}
    s.add_tool_result("read_doc", {"doc_id": "a.md", "start_line": 5}, result, [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert ev.span == (5, 40)


def test_read_evidence_capped_at_synthesis_budget_quarter_bytes():
    """保存上限は清書ダイジェスト予算（env `SHERPA_AGENTIC_SYNTHESIS_BUDGET_BYTES`・既定 256KiB）の 1/4
    （`_READ_TEXT_CAP_BYTES`）——固定800字ではなく清書予算と同期した値であることを固定する。"""
    from sherpa import agentic_search as A
    assert IS._READ_TEXT_CAP_BYTES == A._SYNTHESIS_MAX_BYTES // 4
    assert IS._RENDER_READ_TEXT_CAP == 800   # 表示上限は保存上限に連動させない
    long_text = "あ" * (IS._READ_TEXT_CAP_BYTES // 3 + 1000)   # 保存上限を超える
    s = _state()
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 20}, {"doc_id": "a.md", "text": long_text},
                      [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert len(ev.text.encode("utf-8")) <= IS._READ_TEXT_CAP_BYTES


# ===== C6: 保存時切断（清書予算に合わせた保存上限を超えたときの text_truncated/gaps/注記） =====

def test_read_evidence_retains_trailing_special_case_after_cap_increase(monkeypatch):
    """900字超の精読本文の末尾に
    ある「特例税率0%」は、旧800字固定上限では保存時に切り落とされ清書へ渡らなかった——新しい
    保存上限（清書予算1/4）では全文がそのまま残り、`text_truncated` も立たない。"""
    monkeypatch.setattr(IS, "_READ_TEXT_CAP_BYTES", 6144)   # 予算の実効値に依存せず「800 字超でも残る」を検査
    tail = "特例税率0%が適用される場合がある"
    body = "税率の一般規定について説明する。" * 60 + tail
    assert len(body) > 900   # 旧800字上限なら tail が確実に切り落とされる長さ
    s = _state()
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 1},
                      {"doc_id": "a.md", "text": f"1: {body}"}, [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert tail in ev.text
    assert ev.text_truncated is False
    assert s.gaps == []

    from sherpa import agentic_search as A
    payload = A._read_evidence_payload(s)
    assert any(tail in (p.get("text") or "") for p in payload)
    digest, _, _ = A.build_synthesis_digest([], [], read_evidence=payload)
    assert tail in digest
    assert "保存時に切断" not in digest


def test_read_evidence_exceeding_save_cap_sets_truncated_flag_gap_and_digest_notice(monkeypatch):
    """保存上限（`_READ_TEXT_CAP_BYTES`）を実際に超える本文は (a) `Evidence.text_truncated`、
    (b) 専用の gap（"保存時に本文をN字で切断"）、(c) `build_synthesis_digest` の精読行末尾への
    注記、の3か所すべてに切断の事実が残る——黙って末尾を落とさない。"""
    monkeypatch.setattr(IS, "_READ_TEXT_CAP_BYTES", 6144)   # 保存上限を超える経路を小さな本文で再現
    monkeypatch.setattr(IS, "_RENDER_READ_TEXT_CAP", 800)
    huge = "あ" * 5000   # UTF-8で15000バイト・保存上限を大きく超える
    s = _state()
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 1},
                      {"doc_id": "a.md", "text": f"1: {huge}"}, [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert ev.text_truncated is True
    assert any("保存時に本文を" in g and "切断" in g and "a.md" in g for g in s.gaps)

    from sherpa import agentic_search as A
    payload = A._read_evidence_payload(s)
    assert any(p.get("text_truncated") for p in payload)
    digest, _, _ = A.build_synthesis_digest([], [], read_evidence=payload)
    assert "（末尾未保持）" in digest


def test_xlsx_range_exceeding_save_cap_sets_truncated_flag_with_locator_kept(monkeypatch):
    """S3b 原本読取ツール（span=None・`locator` で同一性を決める）でも保存時切断の扱いは
    read_around/read_doc と同じ——`locator` は失わない。"""
    monkeypatch.setattr(IS, "_READ_TEXT_CAP_BYTES", 6144)   # 保存上限を超える経路を小さな本文で再現
    monkeypatch.setattr(IS, "_RENDER_READ_TEXT_CAP", 800)
    huge = "あ" * 5000
    result = {"sheet": "Sheet1", "range": "A1:D20", "rows": [["a"]], "truncated": False,
             "doc_id": "a.xlsx", "locator": "Sheet1!A1:D20", "text": huge}
    s = _state()
    s.add_tool_result("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet1"}, result, [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert ev.text_truncated is True
    assert ev.locator == "Sheet1!A1:D20"


def test_read_evidence_dedupes_same_doc_and_range_to_one_entry():
    s = _state()
    text = "5: alpha\n6: beta"
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 5}, {"doc_id": "a.md", "text": text}, [], None)
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 5}, {"doc_id": "a.md", "text": text}, [], None)
    assert sum(1 for e in s.evidence if e.kind == "read") == 1


def test_read_evidence_appears_in_render_with_precise_label():
    s = _state()
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 5},
                      {"doc_id": "a.md", "text": "5: 適用除外あり"}, [], None)
    out = s.render(max_bytes=4096)
    assert "精読: a.md 行 5-5「5: 適用除外あり」" in out


# ===== RV#5: S3b 原本読取ツール6本も kind="read" として read_evidence に載る ==================
# `agentic_search.run_tool` が合成する結果の形（`doc_id`/`text`/`locator` を持つ）を模す
# （`_doc_reader_text_locator` 参照）。

def test_xlsx_range_result_becomes_one_read_evidence_entry():
    s = _state()
    result = {"sheet": "Sheet1", "range": "A1:D20", "rows": [["a", "b"]], "truncated": False,
             "doc_id": "a.xlsx", "locator": "Sheet1!A1:D20", "text": "1: a\tb"}
    s.add_tool_result("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet1"}, result, [], None)
    reads = [e for e in s.evidence if e.kind == "read"]
    assert len(reads) == 1
    assert reads[0].doc_id == "a.xlsx"
    assert "a" in reads[0].text


def test_file_head_result_becomes_read_evidence():
    s = _state()
    result = {"size": 5, "text": "hello", "truncated": False, "doc_id": "note.txt", "locator": "head"}
    s.add_tool_result("file_head", {"doc_id": "note.txt"}, result, [], None)
    reads = [e for e in s.evidence if e.kind == "read"]
    assert len(reads) == 1
    assert reads[0].doc_id == "note.txt"
    assert reads[0].text == "hello"


# RV2巡目#9: xlsx_range は span=None のため doc_id だけでは同一性が決まらない——別シートの
# 読み取りが locator（"Sheet1!..." 等）まで含めて区別されず1件に潰れていた（是正後は2件残る）。

def test_xlsx_range_different_sheets_same_doc_stay_as_two_entries():
    s = _state()
    r1 = {"sheet": "Sheet1", "range": "A1:B1", "rows": [["a", "b"]], "truncated": False,
         "doc_id": "a.xlsx", "locator": "Sheet1!A1:B1", "text": "1: a\tb"}
    r2 = {"sheet": "Sheet2", "range": "A1:B1", "rows": [["c", "d"]], "truncated": False,
         "doc_id": "a.xlsx", "locator": "Sheet2!A1:B1", "text": "1: c\td"}
    s.add_tool_result("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet1"}, r1, [], None)
    s.add_tool_result("xlsx_range", {"doc_id": "a.xlsx", "sheet": "Sheet2"}, r2, [], None)
    reads = [e for e in s.evidence if e.kind == "read" and e.doc_id == "a.xlsx"]
    assert len(reads) == 2
    texts = {e.text for e in reads}
    assert texts == {"1: a b", "1: c d"}
    # 清書引き継ぎ（read_evidence）にも locator が前置され、どちらのシートか区別できる。
    from sherpa import agentic_search
    payload = agentic_search._read_evidence_payload(s)
    payload_texts = {p["text"] for p in payload if p["doc_id"] == "a.xlsx"}
    assert payload_texts == {"Sheet1!A1:B1: 1: a b", "Sheet2!A1:B1: 1: c d"}


def test_docx_paragraphs_pptx_slides_pdf_pages_become_read_evidence_but_xlsx_sheets_does_not():
    """本文を返す読取ツールは精読 Evidence。シート一覧だけの xlsx_sheets は精読にしない（根拠の偽装防止）。"""
    s = _state()
    cases = [
        ("xlsx_sheets", {"sheets": [{"name": "Sheet1", "max_row": 1, "max_col": 1}],
                        "doc_id": "a.xlsx", "locator": "sheets", "text": "Sheet1: 1行×1列"}),
        ("docx_paragraphs", {"paragraphs": [{"i": 0, "style": "Normal", "text": "hi"}], "tables": [],
                            "truncated": False, "doc_id": "b.docx", "locator": "paragraphs[0-0]",
                            "text": "段落0: hi"}),
        ("pptx_slides", {"slides": [{"no": 1, "texts": ["t"], "tables": [], "notes": None}],
                        "truncated": False, "doc_id": "c.pptx", "locator": "slides[1]",
                        "text": "スライド1: t"}),
        ("pdf_pages", {"pages": [{"no": 1, "text": "p1"}], "truncated": False,
                      "doc_id": "d.pdf", "locator": "pages[1]", "text": "ページ1: p1"}),
    ]
    for name, result in cases:
        s.add_tool_result(name, {"doc_id": result["doc_id"]}, result, [], None)
    reads = {e.doc_id for e in s.evidence if e.kind == "read"}
    assert reads == {"b.docx", "c.pptx", "d.pdf"}


# ===== 構造的根拠（list_docs/graph_neighbors）=====

def test_structural_list_docs_evidence_is_mechanical_aggregate():
    s = _state()
    meta = [{"doc_id": None, "span": None, "verification_method": "list_docs_verified",
            "list_meta": {"count": 3, "shown": 1, "prefix": "4期", "pattern": ""},
            "matched_doc_ids": ["a.md"]}]
    s.add_tool_result("list_docs", {"path_prefix": "4期"}, {"count": 3, "docs": [{"rel_path": "a.md"}]},
                      [], meta)
    ev = next(e for e in s.evidence if e.kind == "list")
    assert ev.verification == "structural"
    assert "該当 3 件" in ev.text and "a.md" in ev.text


# ===== RV是正3（中）: glob_search/doc_outline/compare_documents の実質的結果を保存する =====

def test_glob_search_paths_are_saved_as_evidence():
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.md"},
                      {"count": 3, "paths": ["a.md", "b/c.md", "d.md"], "truncated": False}, [], None)
    ev = next(e for e in s.evidence if e.kind == "list" and "glob_search" in e.text)
    assert ev.verification == "structural"
    assert "該当 3 件" in ev.text
    for p in ("a.md", "b/c.md", "d.md"):
        assert p in ev.text


def test_glob_search_zero_results_creates_gap_not_empty_evidence():
    """0件は既存の gap 機構（"0件"）で表現し、空の集計 Evidence は作らない
    （list_docs/folder_tree の「0件も1Evidence」とは異なり、glob/outline/compare は文脈整理向けの
    追加保存のため、失う実質的内容が無い0件はgapsだけで十分）。"""
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.zzz"}, {"count": 0, "paths": [], "truncated": False}, [], None)
    assert not any(e.kind == "list" for e in s.evidence)
    assert any("0件" in g for g in s.gaps)


def test_doc_outline_headings_are_saved_as_evidence_keyed_by_doc_id():
    s = _state()
    s.add_tool_result("doc_outline", {"doc_id": "a.md"},
                      {"doc_id": "a.md", "total_lines": 50, "count": 2,
                       "headings": [{"line": 1, "level": 1, "title": "概要"},
                                   {"line": 10, "level": 2, "title": "税率の計算"}],
                       "truncated": False}, [], None)
    ev = next(e for e in s.evidence if e.kind == "outline")
    assert ev.doc_id == "a.md"
    assert ev.verification == "structural"
    assert "概要" in ev.text and "税率の計算" in ev.text


def test_doc_outline_same_doc_refetch_merges_not_duplicates():
    """doc_outline は doc_id で同一性判定（citation/read と同じ）——同じ doc の再取得は1件に統合。"""
    s = _state()
    headings = {"doc_id": "a.md", "count": 1, "headings": [{"line": 1, "level": 1, "title": "概要"}]}
    s.add_tool_result("doc_outline", {"doc_id": "a.md"}, headings, [], None)
    s.add_tool_result("doc_outline", {"doc_id": "a.md"}, headings, [], None)
    assert sum(1 for e in s.evidence if e.kind == "outline") == 1


def test_compare_documents_diff_excerpt_is_saved_not_just_count():
    s = _state()
    s.add_tool_result("compare_documents", {}, {
        "status": "comparable",
        "compare_conditions": {"left": {"doc_id": "a.md"}, "right": {"doc_id": "b.md"}},
        "diff": "--- a\n+++ b\n+新しい行\n-古い行\n 変化なし行",
    }, [], None)
    ev = next(e for e in s.evidence if e.kind == "compare")
    assert "差分 2 行" in ev.text
    assert "新しい行" in ev.text and "古い行" in ev.text   # 件数だけでなく実際の変更内容も残る


def test_glob_doc_outline_compare_evidence_are_redacted_and_capped_at_800_chars():
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.md"},
                      {"count": 1, "paths": ["config: api_key=sk-ABCDEFGHIJKLMNOP1234.md"],
                       "truncated": False}, [], None)
    ev = next(e for e in s.evidence if e.kind == "list")
    assert "sk-ABCDEFGHIJKLMNOP1234" not in ev.text
    assert len(ev.text) <= 800

    s2 = _state()
    many_headings = [{"line": i, "level": 1, "title": "見出し" * 50} for i in range(30)]
    s2.add_tool_result("doc_outline", {"doc_id": "a.md"},
                      {"doc_id": "a.md", "count": 30, "headings": many_headings, "truncated": True}, [], None)
    ev2 = next(e for e in s2.evidence if e.kind == "outline")
    assert len(ev2.text) <= 800


def test_glob_search_content_survives_after_context_compaction():
    """コーディネータ報告の再現: glob_search の結果は文脈整理で古いツール往復（生 JSON）が
    要約へ置換された後も、見つけたパスが render() の要約から読み取れる。"""
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.cbl"},
                      {"count": 2, "paths": ["src/BILLING.cbl", "src/TAXCALC.cbl"], "truncated": False}, [], None)
    # 生のツール結果（tool_log の会話履歴側）が置換された後を模す——state 自体は消えない。
    summary = s.render(max_bytes=4096, keep_recent_tools=0)
    assert "BILLING.cbl" in summary and "TAXCALC.cbl" in summary


# ===== 列挙区切りは空白を含める（後続の再 redact が区切りごと次の項目を飲み込まない） =====

def test_glob_search_kv_secret_in_one_path_does_not_swallow_next_path():
    """1件目のパスが `key=value` 形の秘密パターンにマッチしても、列挙の区切りに空白が無いと
    後続の再 redact（`_KV_SECRET_RE` の `\\S+` は空白でしか止まらない）が区切り記号ごと2件目
    まで飲み込んで消してしまう——区切りに空白を含めることで2件目を守る。"""
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.md"},
                      {"count": 2, "paths": ["config/api_key=secret.md", "keep.md"], "truncated": False},
                      [], None)
    ev = next(e for e in s.evidence if e.kind == "list")
    assert "keep.md" in ev.text


# ===== compare_documents: 差分全体を先に redact し、ヘッダー行は位置で除外する =====

def test_compare_documents_pem_key_beyond_excerpt_window_is_fully_redacted():
    """複数行にまたがる秘密鍵は、抜粋の10行制限をまたいで END 行が11行目以降に落ちても、
    行分割・抜粋の前に diff 全体へ redact 済みのため BEGIN 行や鍵本文が残らない。"""
    s = _state()
    body_lines = "\n".join(f"+BODYLINE{i:02d}" for i in range(9))
    diff = ("--- a\n+++ b\n"
           "+-----BEGIN RSA PRIVATE KEY-----\n"
           f"{body_lines}\n"
           "+-----END RSA PRIVATE KEY-----\n"
           "+keep-this-line-too")
    s.add_tool_result("compare_documents", {}, {
        "status": "comparable",
        "compare_conditions": {"left": {"doc_id": "a.md"}, "right": {"doc_id": "b.md"}},
        "diff": diff,
    }, [], None)
    ev = next(e for e in s.evidence if e.kind == "compare")
    assert "BEGIN RSA PRIVATE KEY" not in ev.text
    assert "BODYLINE00" not in ev.text
    assert "[REDACTED]" in ev.text
    assert "keep-this-line-too" in ev.text


def test_compare_documents_content_line_starting_with_plusplusplus_is_not_mistaken_for_header():
    """diff の本文（3行目以降）に "+++"/"---" で始まる変更行があっても、diff 自身のヘッダー
    （先頭2行だけ）と誤認して除外しない——ヘッダー除外は内容一致でなく位置で行う契約を固定する。"""
    s = _state()
    diff = "--- a\n+++ b\n+++valid content+++\n---also valid---"
    s.add_tool_result("compare_documents", {}, {
        "status": "comparable",
        "compare_conditions": {"left": {"doc_id": "a.md"}, "right": {"doc_id": "b.md"}},
        "diff": diff,
    }, [], None)
    ev = next(e for e in s.evidence if e.kind == "compare")
    assert "差分 2 行" in ev.text
    assert "valid content" in ev.text and "also valid" in ev.text


def test_dataclasses_are_plain_and_mutable_for_upsert():
    """`Evidence`/`ToolCall` はデータクラス（`add_tool_result` の内部実装が直接フィールドへ
    書き戻すため）。公開契約として ev_id/kind/doc_id/span/text/source_tool/verification/
    extra_quotes の各フィールドを持つ。"""
    ev = Evidence(ev_id="ev-1", kind="citation", doc_id="a.md", span=(1, 1), text="t",
                 source_tool="ripgrep_search", verification="unverified")
    assert ev.extra_quotes == []
    tc = ToolCall(name="ripgrep_search", args_summary="q", hits=0, truncated=False, error=None)
    assert tc.name == "ripgrep_search"


# ===== RV是正1（高）: render() の最終出力境界で doc_id/args_summary/error/gaps も redact する =====

def test_render_redacts_secret_in_doc_id():
    """citation の `doc_id` は格納時点では `_digest_clean` を通らない——render() の出力境界で
    初めて redact される（ハイブリッドではローカル下調べの結果がメイン（外部クラウド）へそのまま
    渡るため、doc_id に紛れ込んだ秘密も出力直前に必ず伏せる）。"""
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "x"}, {"hits": [{"doc_id": "a.md"}]},
                      [{"doc_id": "password=secret-value.md", "span": [1, 1], "quote": "本文"}], None)
    out = s.render(max_bytes=4096)
    assert "secret-value.md" not in out
    assert "[REDACTED]" in out


def test_render_redacts_secret_in_tool_args_summary():
    """`ToolCall.args_summary`（検索クエリ等）も render() の出力境界で redact される。"""
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "api_key=sk-ABCDEFGHIJKLMNOP1234"},
                      {"hits": [{"doc_id": "a.md"}]}, [], None)
    out = s.render(max_bytes=4096)
    assert "sk-ABCDEFGHIJKLMNOP1234" not in out
    assert "[REDACTED]" in out


def test_render_redacts_secret_in_tool_error():
    s = _state()
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 1},
                      {"error": "token=ABCDEFGHIJKLMNOPQRSTUVWXYZ0123 は無効です"}, [], None)
    out = s.render(max_bytes=4096)
    assert "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123" not in out
    assert "[REDACTED]" in out


def test_render_redacts_secret_in_gap_from_dropped_citations():
    """`providers/base.py::_ingest_sub_final_into_state` が `dropped_citations` から直接
    `state.gaps` へ足す文字列（`add_tool_result` を経由しない）も render() の出力境界で redact
    される——gaps は素の文字列として直接追記されることもある契約のため、`add_tool_result` 内で
    個別に clean するのではなく render() 側の一括 redact に守らせる。"""
    s = _state()
    s.gaps.append("secret-doc.md: 検証で除外（password=leaked-token-value）")
    out = s.render(max_bytes=4096)
    assert "leaked-token-value" not in out
    assert "[REDACTED]" in out


def test_render_redaction_never_pushes_output_over_max_bytes():
    """`_redact` は短い値を `"[REDACTED]"`（伸びうる）へ置換するため、redact **前**のバイト数で
    予算判定すると出力が redact 後に max_bytes を超えうる——render() は clean 済みの行だけを
    バイト数計算の対象にするため、この事故が起きないことを固定する。"""
    s = _state()
    for i in range(30):
        s.add_tool_result("ripgrep_search", {"query": f"pw={i}"}, {"hits": [{"doc_id": f"{i}.md"}]},
                          [{"doc_id": f"{i}.md", "span": [1, 1], "quote": f"secret={i}"}], None)
    for budget in (80, 120, 200, 400, 800):
        out = s.render(max_bytes=budget)
        assert len(out.encode("utf-8")) <= budget, (budget, out)


# ===== RV是正1・2巡目（高）: 上限で切ってから clean すると切断境界で秘密が断片化して残る =====

def test_args_summary_secret_spanning_truncation_boundary_is_still_redacted():
    """検索クエリが `_ARGS_SUMMARY_CAP`（120字）の境界をまたぐ秘密パターンを含む場合、先に切って
    から clean すると `"sk-ABCDE"` のような断片が `_SECRET_RE` にマッチせず残ってしまう——生値
    全体を先に clean してから上限で切る契約を固定する。"""
    s = _state()
    query = "x" * 112 + "sk-ABCDEFGHIJKLMNOP1234"   # 秘密の途中（120字目）で境界が来る
    s.add_tool_result("ripgrep_search", {"query": query}, {"hits": []}, [], None)
    assert "sk-ABCDE" not in s.tool_log[0].args_summary
    out = s.render(max_bytes=4096)
    assert "sk-ABCDE" not in out
    assert "[REDACTE" in out   # 秘密は redact 済み（置換後の "[REDACTED]" 自体が120字上限で
                               # 切れて "[REDACTE" どまりのことがあるが、それは安全な断片）


def test_tool_error_secret_spanning_truncation_boundary_is_still_redacted():
    """`_TOOL_ERROR_CAP`（80字）の境界をまたぐ PRIVATE KEY の END 行が上限外に落ちる場合でも、
    生値全体を先に clean するため BEGIN 直後の鍵本文が断片のまま残らない。"""
    s = _state()
    key_body = "MIIEvQIBADANBgkqhkiG9w0BAQEFAASCBKcwggSjAgEAAoIBAQDe" * 4
    long_error = f"failed: -----BEGIN RSA PRIVATE KEY-----\n{key_body}\n-----END RSA PRIVATE KEY-----"
    assert len(long_error) > 80
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 1}, {"error": long_error}, [], None)
    assert "BEGIN RSA PRIVATE KEY" not in s.tool_log[0].error
    out = s.render(max_bytes=4096)
    assert "BEGIN RSA PRIVATE KEY" not in out
    assert key_body[:40] not in out
    assert "[REDACTED]" in out


def test_gap_error_message_secret_spanning_truncation_boundary_is_still_redacted():
    """gaps へ積むエラーメッセージ（`_GAP_MESSAGE_CAP`=200字）も同様——境界をまたぐ秘密が残らない。"""
    s = _state()
    long_error = "x" * 190 + "sk-ABCDEFGHIJKLMNOP1234"   # 200字境界の直前から秘密が始まる
    s.add_tool_result("es_search", {"query": "q"}, {"error": long_error}, [], None)
    gap = next(g for g in s.gaps if "sk-" in g or "REDACTED" in g)
    assert "sk-ABCDE" not in gap
    out = s.render(max_bytes=4096)
    assert "sk-ABCDE" not in out


def test_empty_string_error_still_treated_as_error_present_for_hit_counting():
    """`error` が空文字列でも `"error"` キーが存在すれば hits は計算しない（既存契約・redaction
    修正で `has_error` 判定を空文字列の truthiness と切り離した副作用が無いことの回帰）。"""
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "q"}, {"error": "", "hits": [{"doc_id": "a.md"}]}, [], None)
    assert s.tool_log[0].hits is None
    assert s.tool_log[0].error == ""


# ===== RV是正2（中）: render() は根拠を gaps/呼び出し記録より優先して確保する =====

def test_render_prioritizes_recent_evidence_over_many_old_gaps():
    """多数の 0 件記録（gaps・呼び出し記録）の後に新規根拠を1件足しても、予算超過時に真っ先に
    消えるのは古い gaps/呼び出し記録の方——直近の新規根拠は残る（優先度: 根拠 ＞ gaps ＞
    呼び出し記録）。"""
    s = _state()
    for i in range(60):
        query = f"q{i}-" + "x" * 116   # 120字ちょうどの異なるクエリ
        s.add_tool_result("ripgrep_search", {"query": query}, {"hits": []}, [], None)
    assert len(s.gaps) == 60 and len(s.tool_log) == 60
    s.add_tool_result("ripgrep_search", {"query": "新規クエリ"}, {"hits": [{"doc_id": "new.md"}]},
                      [{"doc_id": "new.md", "span": [1, 1], "quote": "新しく見つかった根拠"}], None)
    # gaps+呼び出し記録の全件（60件×2）は数KB程度になる小さい予算を使い、根拠1件は必ず入る
    # 大きさに設定する——全件を保持するには足りないが、根拠1件+見出し程度には十分な予算。
    out = s.render(max_bytes=600)
    assert "新しく見つかった根拠" in out
    assert "ev-1: new.md" in out   # gaps 専用の呼び出しは Evidence を作らない＝ev_id は1件目のまま
    # 古い gaps/呼び出し記録は真っ先に間引かれる（60件全件は残らない）。
    assert out.count("『q") < 60


def test_render_evidence_survives_even_when_tail_sections_fully_dropped():
    """予算が極小で gaps/呼び出し記録セクションが丸ごと落ちても、根拠は（可能な限り）優先して残る。"""
    s = _state()
    for i in range(20):
        s.add_tool_result("ripgrep_search", {"query": f"query-number-{i:03d}-padding-text"},
                          {"hits": []}, [], None)
    s.add_tool_result("ripgrep_search", {"query": "latest"}, {"hits": [{"doc_id": "new.md"}]},
                      [{"doc_id": "new.md", "span": [1, 1], "quote": "最新の根拠"}], None)
    out = s.render(max_bytes=60)   # 根拠1行＋通知すら厳しい極小予算
    assert "【限界】" not in out
    assert "【呼び出し記録】" not in out


# ===== RV是正3（中）: render() は O(n²) にならない（行ごとのバイト数を1度だけ計算） =====

def test_render_formats_each_evidence_exactly_once_regardless_of_truncation():
    """`_fmt_evidence` の呼び出し回数は根拠件数と一致する——予算判定のために同じ行を何度も
    組み立て直さない（二次時間の作り込みを防ぐ回帰）。"""
    s = _state()
    n = 4000
    for i in range(n):
        s.add_tool_result(f"t{i}", {}, {}, [{"doc_id": f"{i}.md", "span": [1, 1], "quote": "x" * 20}], None)
    calls = {"count": 0}
    orig = InvestigationState._fmt_evidence

    def _counting_fmt(self, e):
        calls["count"] += 1
        return orig(self, e)

    InvestigationState._fmt_evidence = _counting_fmt
    try:
        out = s.render(max_bytes=32 * 1024)
    finally:
        InvestigationState._fmt_evidence = orig
    assert calls["count"] == n
    assert len(out.encode("utf-8")) <= 32 * 1024


def test_render_4000_evidence_completes_quickly():
    """4,000 件の根拠で `render()` が線形時間で終わることの目安（O(n²) だと数秒かかっていた）。
    タイミングのばらつきを吸収するため十分に緩い上限（1秒）を使う。"""
    import time

    s = _state()
    for i in range(4000):
        s.add_tool_result(f"t{i}", {}, {}, [{"doc_id": f"{i}.md", "span": [1, 1], "quote": "x" * 20}], None)
    started = time.monotonic()
    s.render(max_bytes=32 * 1024)
    assert time.monotonic() - started < 1.0


# ===== RV是正4（低）: citation/read の同一性は kind+doc_id+span（本文は鍵に含めない） =====

def test_same_doc_span_different_quote_merges_into_one_evidence():
    s = _state()
    s.add_tool_result("ripgrep_search", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "first"}], None)
    s.add_tool_result("ripgrep_search", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "second-longer"}], None)
    assert len(s.evidence) == 1
    assert s.evidence[0].ev_id == "ev-1"


def test_same_doc_span_merge_keeps_longer_text_as_primary():
    s = _state()
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "short"}], None)
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "a much longer quote here"}], None)
    assert s.evidence[0].text == "a much longer quote here"
    assert "short" in s.evidence[0].extra_quotes


def test_same_doc_span_merge_does_not_discard_the_shorter_quote():
    s = _state()
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "a much longer quote here"}], None)
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "short"}], None)
    # 2回目が短くても主本文は変わらず、短い方は extra_quotes へ退避される（事実を捨てない）。
    assert s.evidence[0].text == "a much longer quote here"
    assert "short" in s.evidence[0].extra_quotes


def test_same_doc_span_merge_verification_upgrades_but_never_downgrades():
    s = _state()
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "q"}], None)
    assert s.evidence[0].verification == "unverified"
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "q2"}],
                      [{"doc_id": "a.md", "span": [1, 1], "verification_method": "span_verified"}])
    assert s.evidence[0].verification == "verified"
    # 既に verified の後に unverified 相当（evidence_meta 無し）が来ても退行しない。
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "q3"}], None)
    assert s.evidence[0].verification == "verified"


def test_different_span_on_same_doc_still_gets_separate_evidence():
    """RV是正4は doc_id 単独ではなく kind+doc_id+span——span が違えば引き続き別エントリ。"""
    s = _state()
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [1, 1], "quote": "A"}], None)
    s.add_tool_result("t", {}, {}, [{"doc_id": "a.md", "span": [9, 9], "quote": "B"}], None)
    assert len(s.evidence) == 2


def test_list_docs_with_different_conditions_still_stay_separate_after_rv4():
    """list/graph/compare（doc_id/span が常に None）は引き続き text も鍵に含める——RV是正4で
    citation/read の同一性条件を緩めても、集計事実の異なる条件を1件に潰す回帰を起こさない。"""
    s = _state()
    meta_a = [{"doc_id": None, "span": None, "verification_method": "list_docs_verified",
              "list_meta": {"count": 1, "shown": 1, "prefix": "A", "pattern": ""}, "matched_doc_ids": ["a.md"]}]
    meta_b = [{"doc_id": None, "span": None, "verification_method": "list_docs_verified",
              "list_meta": {"count": 2, "shown": 1, "prefix": "B", "pattern": ""}, "matched_doc_ids": ["b.md"]}]
    s.add_tool_result("list_docs", {"path_prefix": "A"}, {"count": 1, "docs": [{"rel_path": "a.md"}]}, [], meta_a)
    s.add_tool_result("list_docs", {"path_prefix": "B"}, {"count": 2, "docs": [{"rel_path": "b.md"}]}, [], meta_b)
    assert len([e for e in s.evidence if e.kind == "list"]) == 2


# ===== RV 是正: list_docs のページ境界は限界ではない・同一 gap は 1 本・render の精読表示は短く =====

def test_list_docs_page_truncated_does_not_add_body_truncation_gap():
    s = _state()
    s.add_tool_result("list_docs", {"path_prefix": "4期"},
                      {"count": 300, "offset": 0, "docs": [{"rel_path": "a.md"}], "truncated": True,
                       "next_offset": 200}, None, [])
    assert not any("切断" in g for g in s.gaps)
    assert s.tool_log[-1].truncated is True       # 呼び出し記録の打ち切り印は残す


def test_args_summary_uses_doctype_and_state_filters():
    assert IS._args_summary({"doctype": "Excel"}, lambda x: x) == "Excel"
    assert IS._args_summary({"state": "unreadable"}, lambda x: x) == "unreadable"


def test_same_read_truncation_gap_is_recorded_once_across_reingest():
    s = _state()
    for _ in range(3):
        s.add_tool_result("read_doc", {"doc_id": "a.md"},
                          {"doc_id": "a.md", "text": "本文", "text_truncated": True}, [], None)
    assert s.gaps.count("read_doc doc a.md: 本文が上限で切断") == 1


def test_render_keeps_gaps_section_when_read_texts_are_long(monkeypatch):
    """既定の表示上限（固定 800 字）で、長い精読が並んでも【限界】が render から落ちない。"""
    monkeypatch.setattr(IS, "_READ_TEXT_CAP_BYTES", 6144)   # 保存上限の実効値（env）に依存しない
    s = _state()
    for i in range(8):
        s.add_tool_result("read_around", {"doc_id": f"d{i}.md", "line": 1},
                          {"doc_id": f"d{i}.md", "text": f"{i}: " + "あ" * 1500}, [], None)
    s.add_tool_result("ripgrep_search", {"query": "特例"}, {"hits": []}, [], None)
    out = s.render(max_bytes=32 * 1024)
    assert "0件" in out and "…" in out


# ===== RV 2巡目: 再取り込みは偽の 0件・切断 gap を作らない =====

def test_reingest_of_locator_read_does_not_add_zero_or_duplicate_truncation_gaps(monkeypatch):
    monkeypatch.setattr(IS, "_READ_TEXT_CAP_BYTES", 6144)   # 保存上限を超える経路を小さな本文で再現
    from sherpa import agentic_search as A
    from sherpa.providers.base import _ingest_sub_final_into_state
    child = _state()
    child.add_tool_result("xlsx_range", {"doc_id": "d1"},
                          {"doc_id": "d1", "locator": "Sheet1!A1:D20", "text": "い" * 3000}, [], None)
    assert any("保存時に本文を" in g for g in child.gaps)
    payload = A._read_evidence_payload(child)
    parent = _state()
    _ingest_sub_final_into_state(parent, {"read_evidence": payload, "gaps": list(child.gaps)})
    _ingest_sub_final_into_state(parent, {"read_evidence": payload, "gaps": list(child.gaps)})
    assert not any("0件" in g for g in parent.gaps)
    assert all(t.hits != 0 for t in parent.tool_log if t.name == "read_doc")   # 呼び出し記録も 0件にしない
    assert [g for g in parent.gaps if "切断" in g] == [g for g in child.gaps if "切断" in g]
    ev = next(e for e in parent.evidence if e.kind == "read")
    assert ev.text_truncated and ev.locator == "Sheet1!A1:D20" and not ev.text.startswith("Sheet1")


def test_graph_neighbors_truncation_gap_names_partial_neighbors_not_body():
    s = _state()
    s.add_tool_result("graph_neighbors", {"name": "TAXRATE"},
                      {"neighbors": [{"name": "A"}], "truncated": True, "count": 500}, [], None)
    assert any("近傍が上限で打ち切り" in g and "500" in g for g in s.gaps)
    assert not any("本文" in g for g in s.gaps)


def test_render_clips_read_extra_quotes_to_display_cap(monkeypatch):
    monkeypatch.setattr(IS, "_READ_TEXT_CAP_BYTES", 6144)   # 保存上限を超える経路を小さな本文で再現
    monkeypatch.setattr(IS, "_RENDER_READ_TEXT_CAP", 800)
    s = _state()
    s.add_tool_result("file_head", {"doc_id": "X"}, {"doc_id": "X", "text": "あ" * 2000, "locator": "head"}, [], None)
    s.add_tool_result("file_head", {"doc_id": "X"}, {"doc_id": "X", "text": "い" * 1500, "locator": "head"}, [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert ev.extra_quotes                      # 短い方は退避されている
    line = s._fmt_evidence(ev)
    assert len(line) < 2 * IS._RENDER_READ_TEXT_CAP + 200 and "い" * 801 not in line


def test_glob_and_outline_truncation_gap_is_a_list_cutoff_not_body():
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.xlsx"}, {"count": 350, "paths": ["a.xlsx"], "truncated": True}, [], None)
    s.add_tool_result("doc_outline", {"doc_id": "x.md"}, {"doc_id": "x.md", "count": 900, "headings": [], "truncated": True}, [], None)
    assert sum("一覧が上限で打ち切り" in g for g in s.gaps) == 2
    assert not any("本文" in g for g in s.gaps)


def test_doc_outline_file_truncated_only_is_not_reported_as_list_cutoff():
    s = _state()
    s.add_tool_result("doc_outline", {"doc_id": "big.md"},
                      {"doc_id": "big.md", "count": 7, "headings": [], "truncated": False, "file_truncated": True}, [], None)
    assert any("読み切れていない" in g and "過小" in g for g in s.gaps)
    assert not any("一覧が上限で打ち切り" in g for g in s.gaps)


def test_structural_fact_marks_omitted_paths_so_summary_cannot_claim_all():
    meta = [{"doc_id": None, "span": None, "verification_method": "list_docs_verified",
             "list_meta": {"count": 30, "shown": 30, "prefix": "4期", "pattern": ""},
             "matched_doc_ids": [f"4期/{i}.md" for i in range(30)]}]
    s = _state()
    s.add_tool_result("list_docs", {"path_prefix": "4期"}, {"count": 30, "docs": []}, [], meta)
    ev = next(e for e in s.evidence if e.kind == "list")
    assert "他 20 件のパスは未提示＝この一覧は全件として書かない" in ev.text


def test_read_evidence_payload_carries_glob_outline_compare_facts_and_reingests():
    from sherpa import agentic_search as A
    from sherpa.providers.base import _ingest_sub_final_into_state
    child = _state()
    child.add_tool_result("glob_search", {"pattern": "*.xlsx"},
                          {"count": 2, "paths": ["a.xlsx", "b.xlsx"], "truncated": False}, [], None)
    payload = A._read_evidence_payload(child)
    assert any(p.get("kind") == "list" and p.get("source_tool") == "glob_search" and "a.xlsx" in p["text"] for p in payload)
    digest, _, _ = A.build_synthesis_digest([], [], read_evidence=payload)
    assert "a.xlsx" in digest
    parent = _state()
    _ingest_sub_final_into_state(parent, {"read_evidence": payload})
    assert any(e.kind == "list" and e.source_tool == "glob_search" for e in parent.evidence)
    assert not any("0件" in g for g in parent.gaps)


def test_glob_outline_compare_facts_mark_omitted_items():
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.xlsx"},
                      {"count": 30, "paths": [f"p{i}.xlsx" for i in range(30)], "truncated": False}, [], None)
    s.add_tool_result("doc_outline", {"doc_id": "x.md"},
                      {"doc_id": "x.md", "count": 25, "headings": [{"title": f"h{i}", "line": i} for i in range(25)],
                       "truncated": False}, [], None)
    diff = "--- a\n+++ b\n" + "".join(f"+l{i}\n" for i in range(15))
    s.add_tool_result("compare_documents", {"left_doc_id": "a", "right_doc_id": "b"},
                      {"status": "comparable", "compare_conditions": {"left": {"doc_id": "a"}, "right": {"doc_id": "b"}},
                       "diff": diff}, [], None)
    texts = [e.text for e in s.evidence]
    assert any("他 10 件のパスは未提示＝この一覧は全件として書かない" in t for t in texts)
    assert any("他 5 件の見出しは未提示" in t for t in texts)
    assert any("他 5 行は未提示" in t for t in texts)


def test_omission_note_survives_structural_fact_cap():
    long = "4期/業務システム/販売管理/仕様書/画面設計/注文入力画面仕様書_第%02d版.xlsx"
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.xlsx"},
                      {"count": 30, "paths": [long % i for i in range(30)], "truncated": False}, [], None)
    ev = next(e for e in s.evidence if e.kind == "list")
    assert len(ev.text) <= IS._STRUCTURAL_FACT_CAP
    assert ev.text.endswith("この一覧は全件として書かない）")
    shown = sum(1 for i in range(30) if (long % i) in ev.text)
    assert shown < 30 and f"他 {30 - shown} 件のパスは未提示" in ev.text   # 途中で切れたパスは載せず件数は正確


def test_items_dropped_by_char_cap_are_counted_in_omission_note():
    long = "4期/業務システム/販売管理/仕様書/画面設計/注文入力画面仕様書_第%02d版_%s.xlsx"
    paths = [long % (i, "x" * 30) for i in range(20)]   # 20 件＝件数上限内だが 800 字に収まらない
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*.xlsx"}, {"count": 20, "paths": paths, "truncated": False}, [], None)
    ev = next(e for e in s.evidence if e.kind == "list")
    assert len(ev.text) <= IS._STRUCTURAL_FACT_CAP
    shown = sum(1 for p in paths if p in ev.text)
    assert shown < 20 and f"他 {20 - shown} 件のパスは未提示" in ev.text


def test_structural_fact_stays_within_cap_even_with_long_head():
    s = _state()
    s.add_tool_result("glob_search", {"pattern": "*" + "あ" * 1500 + "*.xlsx"},
                      {"count": 3, "paths": ["a.xlsx", "b.xlsx", "c.xlsx"], "truncated": False}, [], None)
    ev = next(e for e in s.evidence if e.kind == "list")
    assert len(ev.text) <= IS._STRUCTURAL_FACT_CAP and "未提示" in ev.text


def test_folder_tree_folders_truncated_is_a_cutoff_limit():
    s = _state()
    s.add_tool_result("folder_tree", {"path_prefix": "4期"},
                      {"count": 700, "folders": [], "folders_truncated": True}, [], None)
    assert any("フォルダ一覧が上限で打ち切り" in g and "700" in g for g in s.gaps)
    assert s.tool_log[-1].truncated is True


def test_xlsx_sheets_truncation_gap_is_a_list_cutoff_not_body():
    s = _state()
    s.add_tool_result("xlsx_sheets", {"doc_id": "x.xlsx"},
                      {"doc_id": "x.xlsx", "sheets": [{"name": "S1"}], "truncated": True}, [], None)
    assert any("シート一覧が上限で打ち切り" in g for g in s.gaps) and not any("本文" in g for g in s.gaps)


def test_compare_unsupported_leaves_a_limit_line():
    s = _state()
    s.add_tool_result("compare_documents", {"left_doc_id": "a", "right_doc_id": "b"},
                      {"status": "unsupported", "reason": "片方以上に RAG 正本が無い文書です"}, [], None)
    assert any("機械的な突合せができず未確認" in g and "RAG 正本" in g for g in s.gaps)


def test_search_hit_cap_is_a_population_cutoff_not_body():
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "税率"},
                      {"hits": [{"doc_id": f"d{i}.md", "line": 1, "text": "x"} for i in range(30)], "truncated": True}, [], None)
    assert any("検索ヒットが上限で打ち切り" in g for g in s.gaps) and not any("本文" in g for g in s.gaps)


def test_search_hit_cap_gap_survives_alongside_body_truncation():
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "税率"},
                      {"hits": [{"doc_id": "a.md", "line": 1, "text": "x"}], "truncated": True,
                       "truncated_docs": ["big.md"]}, [], None)
    assert any("検索ヒットが上限で打ち切り" in g for g in s.gaps) and any("本文が上限で切断" in g for g in s.gaps)


# ===== DEPTH-2 S1: 主張構造（`Claim`/`parse_claims`/`InvestigationState.set_claims`） =====

def test_parse_claims_accepts_confirmed_inferred_unknown_mix():
    raw = [
        {"id": "c1", "status": "confirmed", "text": "標準税率は10%。", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "inferred", "text": "経過措置が適用される可能性がある。",
         "evidence_refs": [], "reason": "類似の過去改正から推定", "reason_code": ""},
        {"id": "c3", "status": "unknown", "text": "適用開始日は資料からは確認できない。",
         "evidence_refs": [], "reason": "", "reason_code": "not_found_in_scope"},
    ]
    claims = IS.parse_claims(raw)
    assert claims is not None and len(claims) == 3
    assert [c.status for c in claims] == ["confirmed", "inferred", "unknown"]
    assert claims[2].reason_code == "not_found_in_scope"


def test_parse_claims_rejects_unknown_status_without_closed_reason_code():
    raw = [{"id": "c1", "status": "unknown", "text": "t", "evidence_refs": [],
            "reason": "", "reason_code": "not_a_real_code"}]
    assert IS.parse_claims(raw) is None


def test_parse_claims_rejects_reason_code_on_non_unknown_status():
    """理由コードは unknown 限定——confirmed/inferred に紛れ込ませたら不正として拒否する。"""
    raw = [{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": [],
            "reason": "", "reason_code": "budget"}]
    assert IS.parse_claims(raw) is None


def test_parse_claims_rejects_duplicate_ids_and_extra_keys():
    dup = [{"id": "c1", "status": "confirmed", "text": "a", "evidence_refs": [], "reason": "", "reason_code": ""},
           {"id": "c1", "status": "confirmed", "text": "b", "evidence_refs": [], "reason": "", "reason_code": ""}]
    assert IS.parse_claims(dup) is None
    extra = [{"id": "c1", "status": "confirmed", "text": "a", "evidence_refs": [], "reason": "",
              "reason_code": "", "unexpected": "x"}]
    assert IS.parse_claims(extra) is None


def test_parse_claims_rejects_non_list_and_malformed_items():
    assert IS.parse_claims({"claims": []}) is None          # 配列でない
    assert IS.parse_claims(["not a dict"]) is None
    assert IS.parse_claims([{"id": "c1", "status": "confirmed"}]) is None   # text 欠落


def _state_with_citation(doc_id="x.md", span=(1, 1), quote="A") -> InvestigationState:
    """confirmed の `evidence_refs` が指す実在の ev_id（"ev-1"）を1件持つ状態を作る。"""
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "a"}, {"hits": [{"doc_id": doc_id}]},
                      [{"doc_id": doc_id, "span": list(span), "quote": quote}], None)
    return s


def test_investigation_state_set_claims_stores_only_valid_parse():
    s = _state_with_citation()
    ok = s.set_claims([{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"],
                        "reason": "", "reason_code": ""}])
    assert ok is True and len(s.claims) == 1
    assert IS.claim_to_dict(s.claims[0]) == {
        "id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"],
        "reason": "", "reason_code": ""}


def test_investigation_state_set_claims_rejects_invalid_without_mutating():
    s = _state_with_citation()
    s.set_claims([{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"],
                  "reason": "", "reason_code": ""}])
    ok = s.set_claims([{"id": "bad", "status": "unknown", "text": "t", "evidence_refs": [],
                        "reason": "", "reason_code": "not_a_real_code"}])
    assert ok is False
    assert len(s.claims) == 1 and s.claims[0].id == "c1"   # 不正な再設定で既存の主張を失わない


def test_investigation_state_set_claims_empty_list_is_not_success():
    s = _state()
    assert s.set_claims([]) is False
    assert s.claims == []


# ===== RV C1（docs/rv/2026-09-17-DEPTH-2.md）: confirmed の evidence_refs は非空かつ実在必須 =====

def test_set_claims_rejects_confirmed_with_empty_evidence_refs():
    """confirmed が根拠参照を1件も挙げない主張は、裏付けの無い確定として通さない
    （end-to-end: `set_claims` が False を返し、呼び出し元は既存の根拠不足の固定文言経路へ落ちる）。"""
    s = _state_with_citation()
    ok = s.set_claims([{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": [],
                        "reason": "", "reason_code": ""}])
    assert ok is False
    assert s.claims == []


def test_set_claims_rejects_confirmed_with_nonexistent_evidence_id():
    """confirmed が実在しない ev_id（この調査に無い "ev-999"）を挙げる主張は不正として拒否する。"""
    s = _state_with_citation()
    ok = s.set_claims([{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-999"],
                        "reason": "", "reason_code": ""}])
    assert ok is False
    assert s.claims == []


def test_set_claims_rejects_whole_batch_if_any_confirmed_claim_is_ungrounded():
    """1件でも不正な confirmed があれば claims 配列全体を不正として扱う（部分採用しない）。"""
    s = _state_with_citation()
    ok = s.set_claims([
        {"id": "c1", "status": "confirmed", "text": "t1", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "confirmed", "text": "t2", "evidence_refs": ["ev-999"],
         "reason": "", "reason_code": ""},
    ])
    assert ok is False
    assert s.claims == []


def test_set_claims_accepts_confirmed_with_real_evidence_id():
    s = _state_with_citation()
    assert s.set_claims([{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"],
                          "reason": "", "reason_code": ""}]) is True


# ===== RV C4: inferred は空白のみでない reason 必須 =====

def test_parse_claims_rejects_inferred_with_empty_reason():
    raw = [{"id": "c1", "status": "inferred", "text": "t", "evidence_refs": [],
            "reason": "", "reason_code": ""}]
    assert IS.parse_claims(raw) is None


def test_parse_claims_rejects_inferred_with_whitespace_only_reason():
    raw = [{"id": "c1", "status": "inferred", "text": "t", "evidence_refs": [],
            "reason": "   ", "reason_code": ""}]
    assert IS.parse_claims(raw) is None


def test_parse_claims_accepts_inferred_with_real_reason():
    raw = [{"id": "c1", "status": "inferred", "text": "t", "evidence_refs": [],
            "reason": "類似事例からの推定", "reason_code": ""}]
    claims = IS.parse_claims(raw)
    assert claims is not None and claims[0].reason == "類似事例からの推定"


# ===== RV C2: claims の ev-N を Evidence Packet 側（combined_evidence_meta）へ変換する =====

def test_resolve_claim_evidence_ids_maps_across_differently_ordered_packet_meta():
    """再調査を挟むと、主張生成のダイジェスト（`state.evidence` の安定採番）と Evidence Packet
    （`combined_evidence_meta`・重複排除／再調査で並びが変わりうる別採番）の ev-N がずれる
    （RV C2 再現）——`resolve_claim_evidence_ids` は根拠の同一性（citation は doc_id＋span、
    構造的根拠は整形済みテキスト）で正しい Packet 側の ev-N へ書き換える。

    再現: 初回に引用 A（a.md）・構造根拠 B（list_docs）を得て、再調査で引用 C（c.md）を得る
    （state.evidence は挿入順で ev-1=A, ev-2=B, ev-3=C）。Evidence Packet 側の
    `combined_evidence_meta` は citation の重複排除・並び替えを経て別の順（ここでは C, A の後に
    構造 B）になる——書き換え前の ID をそのまま使うと ev-1 が A ではなく C を指してしまう
    （このテストは書き換え前提の `evidence_refs` をそのまま比較すれば失敗する＝赤の再現）。
    """
    s = _state()
    s.add_tool_result("ripgrep_search", {"query": "a"}, {"hits": [{"doc_id": "a.md"}]},
                      [{"doc_id": "a.md", "span": [1, 1], "quote": "A"}], None)
    b_meta = {"matched_doc_ids": ["b.md"], "list_meta": {"count": 1, "shown": 1}}
    s.add_tool_result("list_docs", {}, {}, [], [b_meta])
    s.add_tool_result("ripgrep_search", {"query": "c"}, {"hits": [{"doc_id": "c.md"}]},
                      [{"doc_id": "c.md", "span": [5, 5], "quote": "C"}], None)
    assert [e.ev_id for e in s.evidence] == ["ev-1", "ev-2", "ev-3"]   # A, B, C の挿入順

    ok = s.set_claims([
        {"id": "c1", "status": "confirmed", "text": "Aの主張", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "confirmed", "text": "Bの主張", "evidence_refs": ["ev-2"],
         "reason": "", "reason_code": ""},
        {"id": "c3", "status": "confirmed", "text": "Cの主張", "evidence_refs": ["ev-3"],
         "reason": "", "reason_code": ""},
    ])
    assert ok is True

    # Evidence Packet 側は別採番・別順（citation: C, A ／ structural: B）を模す。
    combined_evidence_meta = [
        {"doc_id": "c.md", "span": [5, 5], "verification_method": "span_verified"},   # packet ev-1
        {"doc_id": "a.md", "span": [1, 1], "verification_method": "span_verified"},   # packet ev-2
        b_meta,                                                                        # packet ev-3
    ]
    resolved = IS.resolve_claim_evidence_ids(s.claims, s.evidence, combined_evidence_meta)
    by_id = {c.id: c for c in resolved}
    assert by_id["c1"].evidence_refs == ["ev-2"]   # A -> packet の ev-2
    assert by_id["c3"].evidence_refs == ["ev-1"]   # C -> packet の ev-1
    assert by_id["c2"].evidence_refs == ["ev-3"]   # B -> packet の ev-3（構造的根拠はテキスト一致）


def test_resolve_claim_evidence_ids_drops_refs_without_packet_counterpart():
    """read（原本精読）等、Evidence Packet に現れない種別への参照は黙って落とす
    （存在しない ev-N を清書・共有へ残さない）。"""
    s = _state()
    s.add_tool_result("read_doc", {}, {"doc_id": "x.md", "text": "本文", "start_line": 1, "end_line": 2},
                      [], None)
    assert s.evidence[0].kind == "read" and s.evidence[0].ev_id == "ev-1"
    ok = s.set_claims([{"id": "c1", "status": "inferred", "text": "t", "evidence_refs": ["ev-1"],
                        "reason": "精読のみで確証はない", "reason_code": ""}])
    assert ok is True
    resolved = IS.resolve_claim_evidence_ids(s.claims, s.evidence, [])
    assert resolved[0].evidence_refs == []


def test_render_claims_formats_by_status():
    claims = IS.parse_claims([
        {"id": "c1", "status": "confirmed", "text": "標準税率は10%。", "evidence_refs": ["ev-1", "ev-2"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "inferred", "text": "経過措置が適用される可能性がある。",
         "evidence_refs": [], "reason": "類似の過去改正から推定", "reason_code": ""},
        {"id": "c3", "status": "unknown", "text": "適用開始日は資料からは確認できない。",
         "evidence_refs": [], "reason": "", "reason_code": "not_found_in_scope"},
    ])
    text = IS.render_claims(claims)
    assert "[c1] 確定: 標準税率は10%。（根拠: ev-1、ev-2）" in text
    assert "[c2] 推定: 経過措置が適用される可能性がある。（理由: 類似の過去改正から推定）" in text
    assert "[c3] 不明: 適用開始日は資料からは確認できない。（理由コード: not_found_in_scope）" in text
    assert IS.render_claims([]) == ""


# ===== DEPTH-2 S4b（docs/proposals/2026-09-17-深さの再定義とレビュー巡.md §2.2）:
# worker の一次判断——origin と evidence_refs の親 state への remap =====

def test_parse_claims_defaults_origin_to_synthesis_and_set_claims_accepts_worker_origin():
    raw = [{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": [],
            "reason": "", "reason_code": ""}]
    claims = IS.parse_claims(raw)
    assert claims[0].origin == "synthesis"   # 既定（`_claims_synthesis` 相当）
    claims_worker = IS.parse_claims(raw, origin="worker")
    assert claims_worker[0].origin == "worker"

    s = _state_with_citation()
    ok = s.set_claims([{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"],
                        "reason": "", "reason_code": ""}], origin="worker")
    assert ok is True and s.claims[0].origin == "worker"


def test_claim_to_dict_does_not_leak_origin():
    """`origin` は内部メタデータ——公開 envelope／共有の形（既存6キー）を変えない。"""
    s = _state_with_citation()
    s.set_claims([{"id": "c1", "status": "confirmed", "text": "t", "evidence_refs": ["ev-1"],
                  "reason": "", "reason_code": ""}], origin="worker")
    d = IS.claim_to_dict(s.claims[0])
    assert set(d.keys()) == {"id", "status", "text", "evidence_refs", "reason", "reason_code"}


def test_remap_claim_refs_to_evidence_maps_by_content_and_preserves_origin():
    """worker（`agentic_search.openai_style` の final_synthesis=False 経路）が自分のローカル
    `InvestigationState.evidence` 基準で組んだ主張を、親 `InvestigationState.evidence`（同じ根拠を
    別途取り込み済み）の ev_id へ内容一致（citation は doc_id＋span、structural はテキスト）で
    書き換える。"""
    worker_state = _state_with_citation(doc_id="a.md", span=(1, 1), quote="A")
    b_meta = {"matched_doc_ids": ["b.md"], "list_meta": {"count": 1, "shown": 1}}
    worker_state.add_tool_result("list_docs", {}, {}, [], [b_meta])
    assert [e.ev_id for e in worker_state.evidence] == ["ev-1", "ev-2"]
    ok = worker_state.set_claims([
        {"id": "c1", "status": "confirmed", "text": "Aの主張", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "confirmed", "text": "Bの主張", "evidence_refs": ["ev-2"],
         "reason": "", "reason_code": ""},
    ], origin="worker")
    assert ok is True

    # 親 state は既存の他根拠を1件持ってから、同じ根拠（a.md citation・b.md structural）を
    # 別順で取り込む（ev-N が worker 側とずれる状況を再現）。
    parent = _state()
    parent.add_tool_result("ripgrep_search", {"query": "z"}, {"hits": [{"doc_id": "z.md"}]},
                           [{"doc_id": "z.md", "span": [9, 9], "quote": "Z"}], None)
    parent.add_tool_result("list_docs", {}, {}, [], [b_meta])
    parent.add_tool_result("ripgrep_search", {"query": "a"}, {"hits": [{"doc_id": "a.md"}]},
                           [{"doc_id": "a.md", "span": [1, 1], "quote": "A"}], None)
    assert [e.ev_id for e in parent.evidence] == ["ev-1", "ev-2", "ev-3"]   # Z, B, A の挿入順

    remapped = IS.remap_claim_refs_to_evidence(worker_state.claims, worker_state.evidence, parent.evidence)
    by_id = {c.id: c for c in remapped}
    assert by_id["c1"].evidence_refs == ["ev-3"]   # A -> 親の ev-3
    assert by_id["c2"].evidence_refs == ["ev-2"]   # B -> 親の ev-2（structural はテキスト一致）
    assert by_id["c1"].origin == "worker" and by_id["c2"].origin == "worker"   # origin は保持


def test_remap_claim_refs_to_evidence_drops_refs_without_target_match():
    """親側に対応する根拠が無い参照（重複排除で消えた等）は黙って落とす
    （`resolve_claim_evidence_ids` と同じ契約）。"""
    worker_state = _state_with_citation(doc_id="a.md", span=(1, 1), quote="A")
    claims = IS.parse_claims([{"id": "c1", "status": "inferred", "text": "t", "evidence_refs": ["ev-1"],
                              "reason": "根拠は薄い", "reason_code": ""}], origin="worker")
    remapped = IS.remap_claim_refs_to_evidence(claims, worker_state.evidence, [])
    assert remapped[0].evidence_refs == []
    assert remapped[0].origin == "worker"


# ---- DEPTH-2 S5: evaluator の指摘（`Finding`）と主張の採否（§2.4）----

def _state_with_two_claims():
    st = _state_with_citation(doc_id="a.md", span=(1, 1), quote="A")
    assert st.set_claims([
        {"id": "c1", "status": "confirmed", "text": "標準税率は10%", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "inferred", "text": "経過措置あり", "evidence_refs": [],
         "reason": "根拠が薄い", "reason_code": ""},
    ], origin="worker") is True
    return st


def test_apply_findings_refutation_drops_claim_from_adoptable():
    """反証された主張はその巡の中で採用不可（不明・理由コード conflict）へ落ち、
    未完了回答に載る `adoptable_claims` から外れる。"""
    st = _state_with_two_claims()
    assert st.apply_findings([{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                               "refutes": True}], 1) is True
    by_id = {c.id: c for c in st.claims}
    assert by_id["c1"].status == "unknown" and by_id["c1"].reason_code == "conflict"
    assert [c.id for c in IS.adoptable_claims(st.claims)] == ["c2"]
    assert [f.round_no for f in st.findings] == [1]


def test_apply_findings_keeps_prior_finding_open_without_new_evidence():
    """再指摘が無いだけでは解決にしない——根拠が足されていない指摘は未解決のまま次巡の指示に残る。"""
    st = _state_with_two_claims()
    assert st.apply_findings([{"id": "f1", "claim_id": "c2", "text": "条件が未確認"}], 1) is True
    assert "条件が未確認" in IS.render_findings(st.findings)
    assert st.apply_findings([{"id": "f2", "claim_id": "c1", "text": "別の不足"}], 2) is True
    by_id = {f.id: f for f in st.findings}
    assert by_id["f1"].state == "open" and by_id["f2"].state == "open"
    rendered = IS.render_findings(st.findings)
    assert "条件が未確認" in rendered and "別の不足" in rendered


def test_apply_findings_resolves_only_with_added_evidence_and_confirmation():
    """解決へ遷移するのは「根拠が増えた／確定へ戻った」かつ「orchestrator が確認した
    （判定 sufficient または再指摘なし）」の両方が成立した指摘だけ。"""
    st = _state_with_two_claims()
    assert st.apply_findings([{"id": "f1", "claim_id": "c2", "text": "条件が未確認"}], 1) is True
    # 次巡で c2 に根拠が足され確定へ戻る（同じ id は再指摘されない）。
    assert st.set_claims([
        {"id": "c1", "status": "confirmed", "text": "標準税率は10%", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
        {"id": "c2", "status": "confirmed", "text": "経過措置あり", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""},
    ], origin="worker") is True
    assert st.apply_findings([], 2) is True
    assert {f.id: f.state for f in st.findings} == {"f1": "resolved"}
    assert IS.render_findings(st.findings) == ""


def test_apply_findings_reraised_finding_stays_open_even_with_new_evidence():
    """根拠が増えても、同じ指摘が再指摘されている間は未解決のまま（判定が sufficient でない限り）。"""
    st = _state_with_two_claims()
    assert st.apply_findings([{"id": "f1", "claim_id": "c2", "text": "条件が未確認"}], 1) is True
    assert st.set_claims([
        {"id": "c2", "status": "confirmed", "text": "経過措置あり", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""}], origin="worker") is True
    assert st.apply_findings([{"id": "f1", "claim_id": "c2", "text": "条件が未確認"}], 2,
                             verdict="insufficient") is True
    assert [f.state for f in st.findings] == ["open"]
    assert "条件が未確認" in IS.render_findings(st.findings)


def test_apply_findings_withdrawn_updates_existing_row_and_leaves_next_round():
    """撤回された指摘は旧 open 行を残さず（id で更新）、次巡の指示から落ちる。"""
    st = _state_with_two_claims()
    assert st.apply_findings([{"id": "f1", "claim_id": "c2", "text": "条件が未確認"}], 1) is True
    assert st.apply_findings([{"id": "f1", "claim_id": "c2", "text": "条件が未確認",
                               "state": "withdrawn"}], 2) is True
    assert [(f.id, f.state) for f in st.findings] == [("f1", "withdrawn")]
    assert IS.render_findings(st.findings) == ""


def test_set_claims_does_not_readopt_refuted_claim():
    """未解決の反証がある主張 ID は、後から確定として返し直されても採用不可のまま
    （`_claims_synthesis` の結果でも worker の再調査でも上書きされない）。"""
    st = _state_with_two_claims()
    assert st.apply_findings([{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                               "refutes": True}], 1) is True
    assert st.set_claims([
        {"id": "c1", "status": "confirmed", "text": "標準税率は10%", "evidence_refs": ["ev-1"],
         "reason": "", "reason_code": ""}]) is True
    assert st.claims[0].status == "unknown" and st.claims[0].reason_code == "conflict"
    assert IS.adoptable_claims(st.claims) == []


def test_apply_findings_rejects_malformed_without_changing_claims():
    """形が不正な指摘は何も変更しない（部分採用しない・`parse_claims` と同じ規律）。"""
    st = _state_with_two_claims()
    for bad in ("not-a-list", [{"claim_id": "c1"}], [{"id": "f1", "state": "done"}],
                [{"id": "f1", "refutes": "yes"}], [{"id": "f1"}, {"id": "f1"}]):
        assert st.apply_findings(bad, 1) is False
    assert [c.status for c in st.claims] == ["confirmed", "inferred"]
    assert st.findings == []


def test_apply_findings_none_is_no_findings_not_an_error():
    """`findings` キーが無い応答（`None`）は「指摘なし」＝成功（主張は変わらない）。"""
    st = _state_with_two_claims()
    assert st.apply_findings(None, 1) is True
    assert st.findings == []
    assert len(IS.adoptable_claims(st.claims)) == 2


def test_adoptable_claims_drops_confirmed_without_evidence_refs():
    """根拠 ID の公開採番への変換で `evidence_refs` を失った確定は、未完了回答（停止・失敗）へ
    載せない——裏付けを示せない確定を「確認できた範囲」として公開しない。"""
    claims = [IS.Claim(id="c1", status="confirmed", text="根拠を失った確定", evidence_refs=[]),
              IS.Claim(id="c2", status="confirmed", text="根拠のある確定", evidence_refs=["ev-1"]),
              IS.Claim(id="c3", status="inferred", text="推定", reason="根拠が薄い")]
    assert [c.id for c in IS.adoptable_claims(claims)] == ["c2", "c3"]


def test_render_findings_includes_the_finding_id():
    """未解決の指摘には指摘 ID を付けて渡す——ID の対応が無いと、査読が別の指摘へ同じ ID を
    返したときに既存行の更新が旧い反証を消す。"""
    st = _state_with_two_claims()
    assert st.apply_findings([{"id": "f1", "claim_id": "c1", "text": "別資料と矛盾",
                               "refutes": True}], 1) is True
    out = IS.render_findings(st.findings)
    assert "(f1)" in out and "[c1]" in out and "別資料と矛盾" in out


# ===== 通常の grep を「呼出関係」に数えない（`graph_fallback`）=====

def test_evidence_kinds_of_ripgrep_hit_is_source_only_without_graph_fallback():
    """`graph_fallback` 既定 False——ソースに対する ripgrep ヒットは `source` のみ（`callgraph` は
    グラフが使える環境の通常検索1件では満たされない）。"""
    ev = [Evidence(ev_id="ev-1", kind="citation", doc_id="src/PROG1.cbl", span=(1, 2), text="t",
                  source_tool="ripgrep_search", verification="verified")]
    assert IS.evidence_kinds_of(ev) == {"source"}
    assert IS.evidence_kinds_of(ev, graph_fallback=False) == {"source"}


def test_evidence_kinds_of_ripgrep_hit_upgrades_to_callgraph_with_graph_fallback():
    """`graph_fallback=True`（グラフが不達のターン）だけ ripgrep ヒットを呼出関係の代替として
    数える——`source` は引き続き両方に残る。"""
    ev = [Evidence(ev_id="ev-1", kind="citation", doc_id="src/PROG1.cbl", span=(1, 2), text="t",
                  source_tool="ripgrep_search", verification="verified")]
    assert IS.evidence_kinds_of(ev, graph_fallback=True) == {"source", "callgraph"}


def test_evidence_kinds_of_real_graph_query_is_always_callgraph():
    """真のグラフ照会（`graph_neighbors`/`find_paths`）は `graph_fallback` に関わらず常に
    `callgraph` として数える（代替ではなく本物の呼出関係）。"""
    ev = [Evidence(ev_id="ev-1", kind="graph", doc_id=None, span=None, text="t",
                  source_tool="graph_neighbors", verification="structural")]
    assert IS.evidence_kinds_of(ev, graph_fallback=False) == {"callgraph"}
    assert IS.evidence_kinds_of(ev, graph_fallback=True) == {"callgraph"}


def test_claim_evidence_kinds_propagates_graph_fallback():
    ev = [Evidence(ev_id="ev-1", kind="citation", doc_id="src/PROG1.cbl", span=(1, 2), text="t",
                  source_tool="ripgrep_search", verification="verified")]
    c = IS.Claim(id="c1", status="confirmed", text="t", evidence_refs=["ev-1"])
    assert IS.claim_evidence_kinds(c, ev) == {"source"}
    assert IS.claim_evidence_kinds(c, ev, graph_fallback=True) == {"source", "callgraph"}


def test_evidence_kinds_of_sub_loop_graph_card_is_callgraph_and_sub_loop_source_hit_only_with_fallback():
    """下調べ役経由の根拠は `source_tool="sub_loop"` に集約される——グラフカード（kind="graph"）は
    常に呼出関係、ソースへの検索ヒット（citation）はグラフ不達のターンだけ代替として数える。"""
    from sherpa.investigation_state import Evidence, evidence_kinds_of

    card = Evidence(ev_id="ev-1", kind="graph", doc_id=None, span=None, text="A → B",
                    source_tool="sub_loop", verification="structural")
    hit = Evidence(ev_id="ev-2", kind="citation", doc_id="src/TAXCALC.cbl", span=(1, 2),
                   text="PROGRAM-ID. TAXCALC.", source_tool="sub_loop", verification="verified")
    assert evidence_kinds_of([card]) == {"callgraph"}
    assert evidence_kinds_of([hit]) == {"source"}
    assert evidence_kinds_of([hit], graph_fallback=True) == {"source", "callgraph"}


def test_demote_reason_for_missing_kinds_is_shared_wording():
    """S1b: API（`providers/base.py::_demote_claim_for_missing_kinds`）と Codex
    （`providers/codex/provider.py::_apply_codex_evidence_gate`）の両方の最終ゲートが、
    確定を推定へ落とす理由文言をこの1関数から得る（文言の食い違いを防ぐ唯一の真実源）。"""
    assert (IS.demote_reason_for_missing_kinds(("spec_doc",))
           == "設計書を確認できていないため確定できません")
    assert (IS.demote_reason_for_missing_kinds(("source", "callgraph"))
           == "ソース・呼出関係を確認できていないため確定できません")


# ===== coverage_requested（網羅性の強化と、クイックを本当に速くする・変更C）=====
# `codex_agents_md.py`（AGENTS.md の検知語）・`providers/base.py`（査読プロンプトの
# `coverage_required`）・`chat_service.py`（クイック時の深さ案内）が同じ語彙を使う唯一の真実源。

def test_coverage_requested_true_for_each_keyword():
    assert IS.coverage_requested("区分ごとに起動方式を教えて") is True
    assert IS.coverage_requested("各画面の入力項目を教えて") is True
    assert IS.coverage_requested("それぞれの仕様を教えて") is True
    assert IS.coverage_requested("漏れなく調べて") is True
    assert IS.coverage_requested("一覧にして") is True
    assert IS.coverage_requested("すべての条件を教えて") is True
    assert IS.coverage_requested("全ての条件を教えて") is True
    assert IS.coverage_requested("全部の条件を教えて") is True
    assert IS.coverage_requested("網羅して答えて") is True
    assert IS.coverage_requested("全件出して") is True


def test_coverage_requested_false_for_ordinary_question():
    assert IS.coverage_requested("TAX-RATEは?") is False
    assert IS.coverage_requested("消費税率を変えたい") is False


def test_coverage_requested_false_for_empty_or_non_string():
    assert IS.coverage_requested("") is False
    assert IS.coverage_requested(None) is False


def test_coverage_keywords_is_the_single_source_for_agents_md():
    """`codex_agents_md.py` の検知語一覧はこの定数から作られる（語の定義は1か所）。"""
    from sherpa import codex_agents_md
    for kw in IS.COVERAGE_KEYWORDS:
        assert f"「{kw}」" in codex_agents_md.AGENTS_MD
