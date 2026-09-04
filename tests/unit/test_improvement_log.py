"""改善ログ集計（sherpa/improvement_log.py）単体テスト。DB/ネットワーク不要（純関数のみ・
`fetch_export_rows` 系は `sherpa.store.list_export_messages` を monkeypatch で差し替える）。

- is_honest_failure: 検索レンズ限定・honest_failure 語彙（evidence_verification_failed/
  evaluation_blocked のみ無条件）・evidence_selected==0 かつ investigation_status が
  sufficient 以外の条件付き判定（budget_exceeded/turns_exhausted はこの条件付き経路でのみ
  honest_failure になる＝無条件ではない）。未完了（truncated/content_filtered/refusal/
  tools_per_turn_exceeded/unknown）は honest_failure に含めない。
- trace_tool_stats: kind="tool" ノードのうち実際のツール呼び出しラベルだけを数える
  （プロバイダ間のラベル文言差異込み）。v1/v2 いずれの trace 上限到達も truncated で示す。
- is_export_row_personal_tainted: 質問・回答いずれかの個人情報タグでも除外対象と判定する。
- fetch_export_rows: キーセット方式のページング・個人情報行の除外・出力上限到達時の
  taint 判定込みの truncated 通知。
- build_export_row: 非 agentic/plain 会話（evidence_packet 無し）でも例外にならず安全に
  フォールバック。question_head/answer_head は先頭500字＋*_truncated で切り詰めの有無を明示する。
- compact_summary: 集計（フィードバック件数・タグ分布・stop_reason 分布・honest_failure率・
  incomplete率・所要時間分布・👎質問/一言サンプルの件数上限）。
"""
from __future__ import annotations

import pytest

from sherpa import improvement_log as IL


# ===== is_honest_failure / is_incomplete =====

def test_honest_failure_true_for_evidence_verification_failed_unconditionally():
    """evidence_verification_failed/evaluation_blocked は evidence_selected/investigation_status
    の値に関わらず無条件で honest_failure（正典の第1項）。"""
    assert IL.is_honest_failure(lens="qa", stop_reason="evidence_verification_failed",
                                evidence_selected=5, investigation_status="sufficient") is True


def test_honest_failure_true_for_evaluation_blocked_unconditionally():
    assert IL.is_honest_failure(lens="qa", stop_reason="evaluation_blocked",
                                evidence_selected=5, investigation_status="sufficient") is True


def test_honest_failure_true_when_zero_evidence_and_investigation_not_sufficient():
    """正典の第2項: evidence_selected==0 かつ investigation_status が sufficient 以外。"""
    assert IL.is_honest_failure(lens="qa", stop_reason="evaluation_sufficient",
                                evidence_selected=0, investigation_status="insufficient") is True


def test_honest_failure_false_when_zero_evidence_but_investigation_sufficient():
    """evidence_selected==0 でも investigation_status が sufficient なら honest_failure ではない
    （投資対象の語彙が実際には sufficient/insufficient/conflicting/blocked であり、正典の
    「完了」は sufficient に対応させている・詳細はモジュール docstring 参照）。"""
    assert IL.is_honest_failure(lens="qa", stop_reason="evaluation_sufficient",
                                evidence_selected=0, investigation_status="sufficient") is False


def test_honest_failure_false_when_evidence_selected_present():
    assert IL.is_honest_failure(lens="qa", stop_reason="evaluation_sufficient",
                                evidence_selected=2, investigation_status="insufficient") is False


@pytest.mark.parametrize("stop_reason", ["budget_exceeded", "turns_exhausted"])
def test_honest_failure_budget_and_turns_exhausted_are_conditional_not_automatic(stop_reason):
    """正典は budget_exceeded/turns_exhausted を無条件の honest_failure に含めていない——
    evidence_selected が非0 なら False のまま（他の stop_reason と同じ条件付き経路）。"""
    assert IL.is_honest_failure(lens="qa", stop_reason=stop_reason,
                                evidence_selected=3, investigation_status="insufficient") is False


@pytest.mark.parametrize("stop_reason", ["budget_exceeded", "turns_exhausted"])
def test_honest_failure_budget_and_turns_exhausted_true_via_conditional_clause(stop_reason):
    """evidence_selected==0 かつ investigation_status が sufficient 以外なら、
    budget_exceeded/turns_exhausted も（他の stop_reason と同様に）honest_failure になる。"""
    assert IL.is_honest_failure(lens="qa", stop_reason=stop_reason,
                                evidence_selected=0, investigation_status="insufficient") is True


def test_honest_failure_false_for_non_knowledge_lens_even_with_zero_evidence():
    """雑談等（knowledge=False の素の会話）は検索を試みていないため honest_failure に含めない。"""
    assert IL.is_honest_failure(lens="chat", stop_reason=None, evidence_selected=0,
                                investigation_status=None) is False
    assert IL.is_honest_failure(lens=None, stop_reason=None, evidence_selected=0,
                                investigation_status=None) is False


@pytest.mark.parametrize("stop_reason", [
    "truncated", "content_filtered", "unknown", "refusal", "tools_per_turn_exceeded",
])
def test_honest_failure_false_for_incomplete_stop_reasons_even_with_zero_evidence(stop_reason):
    """出力上限/内容フィルタで途中終了・終了理由を確認できなかった（unknown）・AI が回答を控えた
    （refusal）・ツール呼び出し上限（tools_per_turn_exceeded）のいずれの stop_reason も、
    evidence_selected==0 かつ investigation_status が sufficient 以外という条件を満たしても
    honest_failure に含めない（未完了は honest_failure とは別カテゴリ）。"""
    assert IL.is_honest_failure(lens="qa", stop_reason=stop_reason, evidence_selected=0,
                                investigation_status="insufficient") is False


@pytest.mark.parametrize("stop_reason", [
    "truncated", "content_filtered", "unknown", "refusal", "tools_per_turn_exceeded",
])
def test_is_incomplete_true_for_incomplete_stop_reasons(stop_reason):
    assert IL.is_incomplete(stop_reason) is True


@pytest.mark.parametrize("stop_reason", [
    "evidence_verification_failed", "evaluation_blocked", "budget_exceeded", "turns_exhausted",
    "evaluation_sufficient", "no_tool_calls",
    None, "",
])
def test_is_incomplete_false_for_other_stop_reasons(stop_reason):
    assert IL.is_incomplete(stop_reason) is False


# ===== trace_tool_stats =====

def test_trace_tool_stats_counts_calls_and_files_read():
    trace = [
        {"id": "n1", "kind": "tool", "label": "資料を検索（語句そのまま）", "detail": "x", "status": "done"},
        {"id": "n2", "kind": "tool", "label": "該当箇所を精読", "detail": "y", "status": "done"},
        {"id": "n3", "kind": "tool", "label": "該当箇所を精読", "detail": "z", "status": "done"},
        {"id": "n4", "kind": "think", "label": "質問を理解", "detail": "", "status": "done"},
    ]
    tool_calls, files_read, truncated = IL.trace_tool_stats(trace)
    assert tool_calls == 3
    assert files_read == 2
    assert truncated is False


def test_trace_tool_stats_counts_codex_fulltext_search_label_variant():
    """Codex provider は agentic_search と異なるラベル文言（「資料を検索（全文）」）を使う。"""
    trace = [{"id": "n1", "kind": "tool", "label": "資料を検索（全文）", "detail": "x", "status": "done"}]
    tool_calls, files_read, truncated = IL.trace_tool_stats(trace)
    assert tool_calls == 1
    assert files_read == 0
    assert truncated is False


def test_trace_tool_stats_counts_read_doc_as_files_read():
    """TOOLREAD: read_doc（「文書を通読」）は read_around と同じく files_read に数える。"""
    trace = [{"id": "n1", "kind": "tool", "label": "文書を通読", "detail": "x", "status": "done"}]
    tool_calls, files_read, truncated = IL.trace_tool_stats(trace)
    assert tool_calls == 1
    assert files_read == 1
    assert truncated is False


def test_trace_tool_stats_counts_doc_outline_as_tool_call_but_not_files_read():
    """TOOLREAD: doc_outline（「見出し構造を確認」）は tool_calls には数えるが、本文を読んで
    いない（構造の確認のみ）ため files_read には数えない。"""
    trace = [{"id": "n1", "kind": "tool", "label": "見出し構造を確認", "detail": "x", "status": "done"}]
    tool_calls, files_read, truncated = IL.trace_tool_stats(trace)
    assert tool_calls == 1
    assert files_read == 0
    assert truncated is False


def test_trace_tool_stats_ignores_non_tool_call_labels():
    """予算上限マーカー等（kind="tool" だが実際のツール呼び出しではない）は数えない。"""
    trace = [{"id": "n1", "kind": "tool", "label": "呼び出し予算の上限", "detail": "", "status": "done"}]
    assert IL.trace_tool_stats(trace) == (0, 0, False)


def test_trace_tool_stats_handles_missing_or_malformed_trace():
    assert IL.trace_tool_stats(None) == (0, 0, False)
    assert IL.trace_tool_stats([]) == (0, 0, False)
    assert IL.trace_tool_stats("not-a-list") == (0, 0, False)
    assert IL.trace_tool_stats([{"kind": "tool"}]) == (0, 0, False)   # label 欠落


def test_trace_tool_stats_v1_omitted_marker_sets_truncated_without_counting():
    """v1（既定）の 120 ノード上限で畳まれた要約ノード（id="trace-omitted"）は、畳まれた中の
    種別内訳が分からないため tool_calls には加算せず、truncated=True だけ立てる。"""
    trace = [
        {"id": "trace-omitted", "kind": "think", "label": "（省略）", "detail": "…前半 40 件省略"},
        {"id": "n1", "kind": "tool", "label": "該当箇所を精読", "detail": "x", "status": "done"},
    ]
    tool_calls, files_read, truncated = IL.trace_tool_stats(trace)
    assert tool_calls == 1
    assert files_read == 1
    assert truncated is True


def test_trace_tool_stats_v2_tool_aggregate_adds_omitted_count_to_tool_calls():
    """v2（Execution Event v2）の集約ノードは畳んだ元ノードの kind を保つ。kind="tool" の集約は
    metrics.omitted_count を tool_calls に加算する（files_read への内訳は分からないため加算しない）。"""
    trace = [
        {"id": "grp:1", "kind": "tool", "label": "（集約）", "detail": "", "status": "done",
         "metrics": {"omitted_count": 5}},
    ]
    tool_calls, files_read, truncated = IL.trace_tool_stats(trace)
    assert tool_calls == 5
    assert files_read == 0
    assert truncated is True


def test_trace_tool_stats_v2_non_tool_aggregate_sets_truncated_without_counting():
    """kind が tool 以外の集約（例: budget_limit_reached マーカーは kind="think"）は truncated だけ
    立てて tool_calls には加算しない（畳まれた中身が tool とは限らないため過大集計を避ける）。"""
    trace = [
        {"id": "trace-budget-limit-reached", "kind": "think", "label": "（上限に到達）", "detail": "",
         "status": "done", "metrics": {"omitted_count": 12}},
    ]
    tool_calls, files_read, truncated = IL.trace_tool_stats(trace)
    assert tool_calls == 0
    assert files_read == 0
    assert truncated is True


# ===== build_export_row =====

def test_build_export_row_plain_chat_without_evidence_packet_does_not_raise():
    """非 agentic・plain 会話（evidence_packet 無し）でも空集計で落ちない。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": "2026-08-28T00:00:00+00:00",
          "question": "こんにちは", "content": "こんにちは！", "answer": {"lens": "chat"}, "trace": None}
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] is None
    assert row["candidates_seen"] is None
    assert row["investigation_status"] is None
    assert row["sources"] == []
    assert row["tool_calls"] == 0
    assert row["honest_failure"] is False   # lens="chat" は検索レンズ対象外
    assert row["feedback"] is None


def test_build_export_row_clips_question_and_answer_to_500_chars_with_flags():
    long_text = "あ" * 600
    msg = {"id": 1, "conversation_id": 10, "created_at": None,
          "question": long_text, "content": long_text, "answer": {}}
    row = IL.build_export_row(msg, feedback=None)
    assert len(row["question_head"]) == 500
    assert row["question_truncated"] is True
    assert len(row["answer_head"]) == 500
    assert row["answer_truncated"] is True


def test_build_export_row_short_question_and_answer_not_truncated():
    msg = {"id": 1, "conversation_id": 10, "created_at": None,
          "question": "短い質問", "content": "短い回答", "answer": {}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["question_head"] == "短い質問"
    assert row["question_truncated"] is False
    assert row["answer_head"] == "短い回答"
    assert row["answer_truncated"] is False


def test_build_export_row_normalizes_unknown_stop_reason_to_unknown():
    """閉じた語彙に無い stop_reason（未知の文字列）は "unknown" へ正規化する（そのまま通すと
    stop_reason_counts 等の下流集計が無制限に増殖する）。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "qa", "sources": [],
                     "data": {"evidence_packet": {"stop_reason": "future_unknown_reason"}}}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] == "unknown"


def _msg_with_stop_reason(stop_reason):
    return {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
           "answer": {"lens": "qa", "sources": [],
                      "data": {"evidence_packet": {"stop_reason": stop_reason}}}}


def test_build_export_row_composite_stop_reason_all_steps_natural_completion():
    """複数プロファイル並用（plan 集約経路）の複合 stop_reason で全ステップが自然完了なら
    "evaluation_sufficient" に正規化する（個々のステップの実値が揃っていなくても代表値は1つ）。"""
    row = IL.build_export_row(
        _msg_with_stop_reason("researcher:evaluation_sufficient+reviewer:no_tool_calls"),
        feedback=None)
    assert row["stop_reason"] == "evaluation_sufficient"


def test_build_export_row_composite_stop_reason_mixed_with_incomplete_step():
    """1ステップでも未完了側（truncated 等）があれば、複合値全体をその未完了側の実値へ
    代表させる（honest_failure にも自然完了にも数えない）。"""
    row = IL.build_export_row(
        _msg_with_stop_reason("researcher:evaluation_sufficient+reviewer:truncated"),
        feedback=None)
    assert row["stop_reason"] == "truncated"
    assert IL.is_incomplete(row["stop_reason"]) is True


def test_build_export_row_composite_stop_reason_prefers_honest_over_natural_when_no_incomplete():
    """未完了側のステップが無く honest 側（evidence_verification_failed 等）があれば、
    そちらを代表値にする（自然完了より優先）。"""
    row = IL.build_export_row(
        _msg_with_stop_reason("researcher:evidence_verification_failed+reviewer:evaluation_sufficient"),
        feedback=None)
    assert row["stop_reason"] == "evidence_verification_failed"


def test_build_export_row_plan_completed_token_normalizes_to_evaluation_sufficient():
    """集約対象ステップが無い場合の固定文言 "plan_completed" は自然完了
    （evaluation_sufficient 相当）として扱う。"""
    row = IL.build_export_row(_msg_with_stop_reason("plan_completed"), feedback=None)
    assert row["stop_reason"] == "evaluation_sufficient"


def test_build_export_row_composite_stop_reason_budget_exceeded_not_rounded_to_natural():
    """上限系（budget_exceeded/turns_exhausted）は honest 側・未完了側どちらのステップも無くても
    "evaluation_sufficient" へ丸めず実値をそのまま代表にする（is_honest_failure の条件付き
    判定に evidence_selected/investigation_status で評価させるため）。"""
    row = IL.build_export_row(
        _msg_with_stop_reason("a:budget_exceeded+b:evaluation_sufficient"), feedback=None)
    assert row["stop_reason"] == "budget_exceeded"


@pytest.mark.parametrize("stop_reason", [
    "a:truncated+b:unknown",
    "a:unknown+b:truncated",
])
def test_build_export_row_composite_stop_reason_representative_is_order_independent(stop_reason):
    """代表値の選定はステップの出現順に依存しない固定優先順位（未完了側内では
    content_filtered > truncated > refusal > tools_per_turn_exceeded > unknown）で決まる
    ——ステップの並びを入れ替えても同じ代表値になる。"""
    row = IL.build_export_row(_msg_with_stop_reason(stop_reason), feedback=None)
    assert row["stop_reason"] == "truncated"


@pytest.mark.parametrize("stop_reason", [
    "researcher-evaluation_sufficient",   # ":" が無い＝ "name:reason" 形式でない
    "researcher:unknown_step_reason",     # ステップの reason が既知語彙に無い
    "researcher:evaluation_sufficient+",  # 末尾の空ステップが分解不能
])
def test_build_export_row_undecomposable_composite_stop_reason_falls_back_to_unknown(stop_reason):
    """"name:reason(+name:reason)*" 形式に分解できない、またはステップの reason が既知語彙に
    無い場合は、従来どおり "unknown" にする（誤った代表値を推測しない）。"""
    row = IL.build_export_row(_msg_with_stop_reason(stop_reason), feedback=None)
    assert row["stop_reason"] == "unknown"


@pytest.mark.parametrize("bad_stop_reason", [123, ["evaluation_sufficient"], {"x": 1}, ""])
def test_build_export_row_normalizes_non_string_or_empty_stop_reason_to_unknown(bad_stop_reason):
    """stop_reason が非文字列（型不正なデータ）や空文字列でも例外にならず "unknown" になる。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "qa", "sources": [],
                     "data": {"evidence_packet": {"stop_reason": bad_stop_reason}}}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] == "unknown"


def test_build_export_row_does_not_use_route_reason_as_stop_reason():
    """Evidence Packet はあるが stop_reason だけ欠けている場合、answer.route.reason
    （ルーティング選択の理由であり停止理由ではない）を採用せず "unknown" にする。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "qa", "sources": [],
                     "route": {"lens": "qa", "reason": "仕様問い合わせと判定"},
                     "data": {"evidence_packet": {"evidence_selected": 1}}}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] == "unknown"


def test_build_export_row_falls_back_to_unknown_when_stop_reason_missing():
    """Evidence Packet はあるが stop_reason が無ければ "unknown" にする（対象外＝None とは区別する）。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "qa", "sources": [], "data": {"evidence_packet": {"evidence_selected": 1}}}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] == "unknown"


def test_build_export_row_empty_evidence_packet_dict_resolves_to_unknown_not_none():
    """Evidence Packet の値が**空 dict `{}`**（キー自体はある）の場合も、キーが無い場合
    （対象外＝None）とは区別して "unknown" にする（空 dict は `not packet` で判定すると
    「Packet 自体が無い」と誤って同一視されてしまう）。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "qa", "sources": [], "data": {"evidence_packet": {}}}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] == "unknown"
    assert row["evidence_selected"] is None
    assert row["investigation_status"] is None


def test_build_export_row_missing_evidence_packet_key_resolves_to_none_not_unknown():
    """`data` はあるが `evidence_packet` キー自体が無い（非 agentic 経路）場合は、従来どおり
    対象外（`None`）のまま——空 dict のケースと取り違えない。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "qa", "sources": [], "data": {}}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] is None


def test_build_export_row_falls_back_to_route_lens_when_top_level_lens_missing():
    """トップレベル answer.lens が欠落していても answer.route.lens で honest_failure 判定の
    検索レンズ gate を補う（欠落を理由に honest_failure が常に False になってはいけない）。"""
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"sources": [], "route": {"lens": "qa"},
                     "data": {"evidence_packet": {"stop_reason": "evidence_verification_failed",
                                                  "evidence_selected": 0}}}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["honest_failure"] is True


def test_build_export_row_includes_feedback_when_present():
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "chat"}}
    fb = {"rating": "down", "tags": ["slow"], "comment": "遅い"}
    row = IL.build_export_row(msg, feedback=fb)
    assert row["feedback"] == {"rating": "down", "tags": ["slow"], "comment": "遅い"}


def test_build_export_row_lane_breakdown_prefers_usage_subs_over_usage_sub():
    msg = {"id": 1, "conversation_id": 10, "created_at": None, "question": "q", "content": "a",
          "answer": {"lens": "chat", "usage_sub": {"profile": "p1"},
                     "usage_subs": [{"profile": "p1"}, {"profile": "p2"}]}}
    row = IL.build_export_row(msg, feedback=None)
    assert row["lane_breakdown"] == [{"profile": "p1"}, {"profile": "p2"}]


# ===== compact_summary =====

def _row(mid, *, question="質問", content="回答", stop_reason=None,
        evidence_selected=None, investigation_status=None, sources=None, lens="qa",
        duration_ms=None):
    answer = {"lens": lens, "sources": sources or []}
    if stop_reason is not None or evidence_selected is not None or investigation_status is not None:
        answer["data"] = {"evidence_packet": {"stop_reason": stop_reason,
                                              "evidence_selected": evidence_selected,
                                              "investigation_status": investigation_status}}
    if duration_ms is not None:
        answer["duration_ms"] = duration_ms
    return {"id": mid, "conversation_id": 1, "created_at": None, "question": question,
           "content": content, "answer": answer, "trace": None}


def test_compact_summary_aggregates_feedback_and_honest_failure_and_incomplete(monkeypatch):
    rows = [
        _row(1, stop_reason="evaluation_blocked", evidence_selected=0,
            investigation_status="insufficient", duration_ms=100),   # honest_failure（無条件語彙）
        _row(2, stop_reason="truncated", evidence_selected=0, investigation_status="insufficient",
            sources=[], duration_ms=200),  # incomplete のみ
        _row(3, stop_reason="evaluation_sufficient", evidence_selected=1,
            investigation_status="sufficient", sources=[{"doc_id": "d1"}], duration_ms=300),
    ]
    feedback = {
        1: {"rating": "down", "tags": ["wrong_evidence"], "comment": "違う"},
        3: {"rating": "up", "tags": [], "comment": None},
    }
    monkeypatch.setattr("sherpa.store.list_export_messages", lambda **kwargs: rows)
    monkeypatch.setattr("sherpa.store.get_feedback_by_message_ids", lambda ids: feedback)

    summary = IL.compact_summary(days=7)
    assert summary["turns_total"] == 3
    assert summary["feedback_up"] == 1
    assert summary["feedback_down"] == 1
    assert summary["feedback_tag_counts"] == {"wrong_evidence": 1}
    assert summary["honest_failure_count"] == 1
    assert summary["incomplete_count"] == 1
    assert summary["duration_ms"]["count"] == 3
    assert summary["flagged_questions_sample"] == ["質問"]
    assert summary["stop_reason_counts"] == {
        "evaluation_blocked": 1, "truncated": 1, "evaluation_sufficient": 1}


def test_compact_summary_includes_down_comment_samples(monkeypatch):
    rows = [_row(1, question="質問A"), _row(2, question="質問B")]
    feedback = {
        1: {"rating": "down", "tags": ["slow"], "comment": "遅かった"},
        2: {"rating": "down", "tags": ["slow"], "comment": "根拠が薄い"},
    }
    monkeypatch.setattr("sherpa.store.list_export_messages", lambda **kwargs: rows)
    monkeypatch.setattr("sherpa.store.get_feedback_by_message_ids", lambda ids: feedback)

    summary = IL.compact_summary(days=7)
    assert set(summary["flagged_comments_sample"]) == {"遅かった", "根拠が薄い"}


def test_compact_summary_stop_reason_counts_uses_none_bucket_when_missing(monkeypatch):
    rows = [_row(1)]   # stop_reason/evidence_selected/investigation_status 全て未指定
    monkeypatch.setattr("sherpa.store.list_export_messages", lambda **kwargs: rows)
    monkeypatch.setattr("sherpa.store.get_feedback_by_message_ids", lambda ids: {})

    summary = IL.compact_summary(days=7)
    assert summary["stop_reason_counts"] == {"none": 1}


def test_compact_summary_flagged_questions_and_comments_capped_at_20(monkeypatch):
    rows = [_row(i, question=f"質問{i}") for i in range(30)]
    feedback = {i: {"rating": "down", "tags": [], "comment": f"一言{i}"} for i in range(30)}
    monkeypatch.setattr("sherpa.store.list_export_messages", lambda **kwargs: rows)
    monkeypatch.setattr("sherpa.store.get_feedback_by_message_ids", lambda ids: feedback)

    summary = IL.compact_summary(days=30)
    assert len(summary["flagged_questions_sample"]) == 20
    assert len(summary["flagged_comments_sample"]) == 20


def test_compact_summary_empty_period_returns_none_rates(monkeypatch):
    monkeypatch.setattr("sherpa.store.list_export_messages", lambda **kwargs: [])
    monkeypatch.setattr("sherpa.store.get_feedback_by_message_ids", lambda ids: {})
    summary = IL.compact_summary(days=1)
    assert summary["turns_total"] == 0
    assert summary["honest_failure_rate"] is None
    assert summary["incomplete_rate"] is None
    assert summary["duration_ms"] == {"count": 0}
    assert summary["stop_reason_counts"] == {}
    assert summary["flagged_comments_sample"] == []


# ===== is_export_row_personal_tainted =====

def test_is_export_row_personal_tainted_checks_answer_side():
    assert IL.is_export_row_personal_tainted(
        {"personal": True, "answer": {}, "question_personal": False,
         "question_answer": None}) is True


def test_is_export_row_personal_tainted_checks_question_side():
    """回答側 personal=False でも、対応する質問側が個人情報由来なら除外対象と判定する
    （クラッシュ復旧の assistant 行が personal=false のまま保存される穴を塞ぐ）。"""
    assert IL.is_export_row_personal_tainted(
        {"personal": False, "answer": {}, "question_personal": True,
         "question_answer": None}) is True


def test_is_export_row_personal_tainted_checks_legacy_markers_on_both_sides():
    assert IL.is_export_row_personal_tainted(
        {"personal": False, "answer": {"codex_wrote_files": True}, "question_personal": False,
         "question_answer": None}) is True
    assert IL.is_export_row_personal_tainted(
        {"personal": False, "answer": {}, "question_personal": False,
         "question_answer": {"personal_sources": [{"doc_id": "x"}]}}) is True


def test_is_export_row_personal_tainted_false_when_clean():
    assert IL.is_export_row_personal_tainted(
        {"personal": False, "answer": {}, "question_personal": False,
         "question_answer": None}) is False


# ===== fetch_export_rows =====

def _fake_list_export_messages(all_rows):
    """`store.list_export_messages` の振る舞いを模す fake（id 降順・cursor_id 未満・limit 件）。"""
    def _fn(*, time_from, cursor_id, limit):
        pool = [r for r in all_rows if cursor_id is None or r["id"] < cursor_id]
        pool.sort(key=lambda r: r["id"], reverse=True)
        return pool[:limit]
    return _fn


def _clean_row(rid, question=None):
    return {"id": rid, "personal": False, "answer": {}, "question": question,
           "question_personal": False, "question_answer": None}


def _personal_row(rid):
    return {"id": rid, "personal": True, "answer": {}, "question": None,
           "question_personal": False, "question_answer": None}


def test_fetch_export_rows_excludes_personal_tainted_rows(monkeypatch):
    rows = [
        _clean_row(1, "q1"),
        {"id": 2, "personal": True, "answer": {}, "question": "q2",
         "question_personal": False, "question_answer": None},
        {"id": 3, "personal": False, "answer": {}, "question": "q3",
         "question_personal": True, "question_answer": None},
    ]
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages(rows))
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=100)
    assert [r["id"] for r in result] == [1]
    assert truncated is False


def test_fetch_export_rows_paginates_across_multiple_pages(monkeypatch):
    rows = [_clean_row(i) for i in range(1, 8)]
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages(rows))
    monkeypatch.setattr(IL, "_EXPORT_PAGE", 3)
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=100)
    assert [r["id"] for r in result] == [7, 6, 5, 4, 3, 2, 1]
    assert truncated is False


def test_fetch_export_rows_marks_truncated_when_output_cap_reached_with_more_remaining(monkeypatch):
    rows = [_clean_row(i) for i in range(1, 11)]
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages(rows))
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=4)
    assert [r["id"] for r in result] == [10, 9, 8, 7]
    assert truncated is True


def test_fetch_export_rows_not_truncated_when_cap_exactly_matches_available(monkeypatch):
    rows = [_clean_row(i) for i in range(1, 5)]
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages(rows))
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=4)
    assert len(result) == 4
    assert truncated is False


def test_fetch_export_rows_empty_dataset_returns_empty_not_truncated(monkeypatch):
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages([]))
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=100)
    assert result == []
    assert truncated is False


def test_fetch_export_rows_probe_ignores_remaining_rows_that_are_all_personal_tainted(monkeypatch):
    """出力上限に達した直後の残り候補が個人情報の行だけなら、truncated は立てない
    （probe も taint 判定を通す）。"""
    rows = [_clean_row(i) for i in range(1, 5)] + [_personal_row(i) for i in range(5, 8)]
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages(rows))
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=4)
    assert [r["id"] for r in result] == [4, 3, 2, 1]
    assert truncated is False


def test_fetch_export_rows_probe_finds_clean_row_beyond_personal_run(monkeypatch):
    """出力上限直後に個人情報の行が連続していても、その先に非個人の行が残っていれば
    truncated=True にする（probe がページをまたいで taint 判定を続ける）。"""
    rows = ([_clean_row(i) for i in range(10, 14)] + [_personal_row(i) for i in range(5, 10)]
           + [_clean_row(4)])
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages(rows))
    monkeypatch.setattr(IL, "_EXPORT_PAGE", 3)
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=4)
    assert [r["id"] for r in result] == [13, 12, 11, 10]
    assert truncated is True
