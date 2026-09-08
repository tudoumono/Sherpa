"""`sherpa.investigation_state.InvestigationState` の単体テスト（純粋な Python・LLM 不使用・コスト0）。

C（調査結果集約と並列実行の改善方針・§「質問ごとの調査状態をアプリが管理する」）: 1質問1調査状態が
(1) ev_id の永続性（追加・重複・並べ替えで変わらない）、(2) gaps の機械生成（モデルの散文を事実として
取り込まない）、(3) render の予算打ち切りと注記、を満たすことを固定する。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

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


def test_read_evidence_capped_at_800_chars():
    s = _state()
    long_text = "\n".join(f"{i}: {'x' * 50}" for i in range(1, 40))
    assert len(long_text) > 800
    s.add_tool_result("read_around", {"doc_id": "a.md", "line": 20}, {"doc_id": "a.md", "text": long_text},
                      [], None)
    ev = next(e for e in s.evidence if e.kind == "read")
    assert len(ev.text) <= 800


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
