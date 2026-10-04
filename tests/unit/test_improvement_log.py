"""改善ログ集計（sherpa/improvement_log.py）単体テスト。DB/ネットワーク不要
（`fetch_export_rows` は `sherpa.store.list_export_messages` を monkeypatch で差し替える）。

- is_honest_failure: 検索レンズ限定。evidence_verification_failed/evaluation_blocked は無条件、
  それ以外は evidence_selected==0 かつ investigation_status が sufficient 以外のときだけ
  （budget_exceeded/turns_exhausted も同じ条件付き経路）。未完了系 stop_reason は含めない。
- trace_tool_stats: kind="tool" ノードのうち実際のツール呼び出しラベルだけを数える。
- build_export_row: evidence_packet 無しでも安全にフォールバック・stop_reason は閉じた語彙へ正規化。
- is_export_row_personal_tainted / fetch_export_rows: 個人情報行の除外・ページング・truncated 通知。
"""
from __future__ import annotations

import pytest

from sherpa import improvement_log as IL


# ===== is_honest_failure =====

@pytest.mark.parametrize("lens, stop_reason, evidence_selected, status, expected", [
    # evidence_verification_failed/evaluation_blocked は他の値に関わらず無条件
    ("qa", "evidence_verification_failed", 5, "sufficient", True),
    ("qa", "evaluation_blocked", 5, "sufficient", True),
    # evidence_selected==0 かつ investigation_status が sufficient 以外
    ("qa", "evaluation_sufficient", 0, "insufficient", True),
    ("qa", "evaluation_sufficient", 0, "sufficient", False),
    ("qa", "evaluation_sufficient", 2, "insufficient", False),
    # budget_exceeded/turns_exhausted は無条件ではなく条件付き経路のみ
    ("qa", "budget_exceeded", 3, "insufficient", False),
    ("qa", "turns_exhausted", 3, "insufficient", False),
    ("qa", "budget_exceeded", 0, "insufficient", True),
    ("qa", "turns_exhausted", 0, "insufficient", True),
    # 検索を試みていない雑談等は対象外
    ("chat", None, 0, None, False),
    (None, None, 0, None, False),
    # 未完了は別カテゴリ（条件を満たしても honest_failure に含めない）
    ("qa", "truncated", 0, "insufficient", False),
    ("qa", "content_filtered", 0, "insufficient", False),
    ("qa", "unknown", 0, "insufficient", False),
    ("qa", "refusal", 0, "insufficient", False),
    ("qa", "tools_per_turn_exceeded", 0, "insufficient", False),
])
def test_is_honest_failure(lens, stop_reason, evidence_selected, status, expected):
    assert IL.is_honest_failure(lens=lens, stop_reason=stop_reason, evidence_selected=evidence_selected,
                                investigation_status=status) is expected


# ===== trace_tool_stats =====

def _t(label, kind="tool", **extra):
    return {"kind": kind, "label": label, "detail": "x", "status": "done", **extra}


@pytest.mark.parametrize("trace, expected", [
    # 検索・精読・思考ノード（精読は files_read、think は数えない）
    ([_t("資料を検索（語句そのまま）"), _t("該当箇所を精読"), _t("該当箇所を精読"), _t("質問を理解", kind="think")],
     (3, 2, False)),
    ([_t("資料を検索（全文）")], (1, 0, False)),            # Codex 側のラベル差異
    ([_t("資料を検索（grep）")], (1, 0, False)),            # 履歴データの旧ラベルも数え続ける
    ([_t("文書を通読")], (1, 1, False)),
    ([_t(f"原本を読む（{k}）") for k in ("Excel", "Word", "PowerPoint", "PDF", "先頭")], (5, 5, False)),
    # 本文を読まない確認系は tool_calls のみ
    ([_t("原本のシート一覧を確認")], (1, 0, False)),
    ([_t("見出し構造を確認")], (1, 0, False)),
    ([_t("フォルダ構成を確認")], (1, 0, False)),
    ([_t("世代間の差分を比較")], (1, 0, False)),
    # kind="tool" でも実呼び出しでないマーカー
    ([_t("呼び出し予算の上限")], (0, 0, False)),
    # 欠落・不正
    (None, (0, 0, False)),
    ([], (0, 0, False)),
    ("not-a-list", (0, 0, False)),
    ([{"kind": "tool"}], (0, 0, False)),
    # v1: 畳まれた要約ノードは内訳不明＝加算せず truncated だけ立てる
    ([{"id": "trace-omitted", "kind": "think", "label": "（省略）", "detail": "…前半 40 件省略"},
      _t("該当箇所を精読")], (1, 1, True)),
    # v2: kind="tool" の集約は omitted_count を tool_calls に加算（files_read は内訳不明で加算しない）
    ([_t("（集約）", id="grp:1", metrics={"omitted_count": 5})], (5, 0, True)),
    # v2: tool 以外の集約は truncated のみ（過大集計を避ける）
    ([_t("（上限に到達）", kind="think", id="trace-budget-limit-reached", metrics={"omitted_count": 12})],
     (0, 0, True)),
])
def test_trace_tool_stats(trace, expected):
    assert IL.trace_tool_stats(trace) == expected


# ===== build_export_row =====

def _msg(answer, question="q", content="a", **extra):
    return {"id": 1, "conversation_id": 10, "created_at": None, "question": question,
            "content": content, "answer": answer, **extra}


def _msg_with_stop_reason(stop_reason):
    return _msg({"lens": "qa", "sources": [], "data": {"evidence_packet": {"stop_reason": stop_reason}}})


def test_build_export_row_plain_chat_without_evidence_packet_does_not_raise():
    msg = _msg({"lens": "chat"}, question="こんにちは", content="こんにちは！", trace=None,
               created_at="2026-08-28T00:00:00+00:00")
    row = IL.build_export_row(msg, feedback=None)
    assert row["stop_reason"] is None
    assert row["candidates_seen"] is None
    assert row["investigation_status"] is None
    assert row["sources"] == []
    assert row["tool_calls"] == 0
    assert row["honest_failure"] is False   # lens="chat" は検索レンズ対象外
    assert row["feedback"] is None


@pytest.mark.parametrize("text, truncated", [("あ" * 600, True), ("短い", False)])
def test_build_export_row_clips_question_and_answer_to_500_chars_with_flags(text, truncated):
    row = IL.build_export_row(_msg({}, question=text, content=text), feedback=None)
    assert row["question_head"] == text[:500]
    assert row["answer_head"] == text[:500]
    assert row["question_truncated"] is truncated
    assert row["answer_truncated"] is truncated


@pytest.mark.parametrize("stop_reason, expected", [
    ("future_unknown_reason", "unknown"),    # 閉じた語彙に無い値は下流集計が増殖しないよう正規化
    # 複合（plan 集約経路）: 全ステップ自然完了なら evaluation_sufficient
    ("researcher:evaluation_sufficient+reviewer:no_tool_calls", "evaluation_sufficient"),
    # 未完了ステップがあればその実値を代表に
    ("researcher:evaluation_sufficient+reviewer:truncated", "truncated"),
    # 未完了が無く honest 側があれば自然完了より優先
    ("researcher:evidence_verification_failed+reviewer:evaluation_sufficient", "evidence_verification_failed"),
    ("plan_completed", "evaluation_sufficient"),
    # 上限系は evaluation_sufficient へ丸めない（is_honest_failure の条件付き判定に委ねる）
    ("a:budget_exceeded+b:evaluation_sufficient", "budget_exceeded"),
    # 代表値は出現順に依存しない
    ("a:truncated+b:unknown", "truncated"),
    ("a:unknown+b:truncated", "truncated"),
    # 分解不能・未知のステップ reason は unknown
    ("researcher-evaluation_sufficient", "unknown"),
    ("researcher:unknown_step_reason", "unknown"),
    ("researcher:evaluation_sufficient+", "unknown"),
    # 型不正・空文字でも例外にならず unknown
    (123, "unknown"),
    (["evaluation_sufficient"], "unknown"),
    ({"x": 1}, "unknown"),
    ("", "unknown"),
])
def test_build_export_row_stop_reason_normalization(stop_reason, expected):
    assert IL.build_export_row(_msg_with_stop_reason(stop_reason), feedback=None)["stop_reason"] == expected


@pytest.mark.parametrize("answer, expected", [
    # route.reason はルーティング理由であり停止理由ではない
    ({"lens": "qa", "sources": [], "route": {"lens": "qa", "reason": "仕様問い合わせと判定"},
      "data": {"evidence_packet": {"evidence_selected": 1}}}, "unknown"),
    ({"lens": "qa", "sources": [], "data": {"evidence_packet": {"evidence_selected": 1}}}, "unknown"),
    ({"lens": "qa", "sources": [], "data": {"evidence_packet": {}}}, "unknown"),   # 空 dict は Packet 有り
    ({"lens": "qa", "sources": [], "data": {}}, None),                              # Packet 無し＝対象外
])
def test_build_export_row_stop_reason_when_packet_lacks_stop_reason(answer, expected):
    assert IL.build_export_row(_msg(answer), feedback=None)["stop_reason"] is expected


def test_build_export_row_empty_evidence_packet_dict_leaves_other_fields_none():
    row = IL.build_export_row(_msg({"lens": "qa", "sources": [], "data": {"evidence_packet": {}}}), feedback=None)
    assert row["evidence_selected"] is None
    assert row["investigation_status"] is None


def test_build_export_row_falls_back_to_route_lens_when_top_level_lens_missing():
    # トップレベル lens 欠落を理由に honest_failure が常に False になってはいけない。
    msg = _msg({"sources": [], "route": {"lens": "qa"},
                "data": {"evidence_packet": {"stop_reason": "evidence_verification_failed",
                                             "evidence_selected": 0}}})
    assert IL.build_export_row(msg, feedback=None)["honest_failure"] is True


def test_build_export_row_includes_feedback_when_present():
    fb = {"rating": "down", "tags": ["slow"], "comment": "遅い"}
    assert IL.build_export_row(_msg({"lens": "chat"}), feedback=fb)["feedback"] == fb


def test_build_export_row_lane_breakdown_prefers_usage_subs_over_usage_sub():
    msg = _msg({"lens": "chat", "usage_sub": {"profile": "p1"},
                "usage_subs": [{"profile": "p1"}, {"profile": "p2"}]})
    assert IL.build_export_row(msg, feedback=None)["lane_breakdown"] == [{"profile": "p1"}, {"profile": "p2"}]


# ===== is_export_row_personal_tainted =====

@pytest.mark.parametrize("row, expected", [
    ({"personal": True, "answer": {}, "question_personal": False, "question_answer": None}, True),
    # 回答側 personal=False でも質問側が個人情報由来なら除外（復旧 assistant 行の穴）
    ({"personal": False, "answer": {}, "question_personal": True, "question_answer": None}, True),
    ({"personal": False, "answer": {"codex_wrote_files": True}, "question_personal": False,
      "question_answer": None}, True),
    ({"personal": False, "answer": {}, "question_personal": False,
      "question_answer": {"personal_sources": [{"doc_id": "x"}]}}, True),
    ({"personal": False, "answer": {}, "question_personal": False, "question_answer": None}, False),
])
def test_is_export_row_personal_tainted(row, expected):
    assert IL.is_export_row_personal_tainted(row) is expected


# ===== fetch_export_rows =====

def _fake_list_export_messages(all_rows):
    """`store.list_export_messages` を模す（id 降順・cursor_id 未満・limit 件）。"""
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


@pytest.mark.parametrize("rows, page, cap, expected_ids, expected_truncated", [
    ([_clean_row(i) for i in range(1, 8)], 3, 100, [7, 6, 5, 4, 3, 2, 1], False),    # 複数ページ
    ([_clean_row(i) for i in range(1, 11)], None, 4, [10, 9, 8, 7], True),           # 上限到達＋残りあり
    ([_clean_row(i) for i in range(1, 5)], None, 4, [4, 3, 2, 1], False),            # 上限ちょうど
    ([], None, 100, [], False),
    # 上限直後の残りが個人情報の行だけなら truncated は立てない（probe も taint 判定を通す）
    ([_clean_row(i) for i in range(1, 5)] + [_personal_row(i) for i in range(5, 8)], None, 4,
     [4, 3, 2, 1], False),
    # 個人情報の行が連続していても、その先に非個人の行があれば truncated（ページをまたいで判定）
    ([_clean_row(i) for i in range(10, 14)] + [_personal_row(i) for i in range(5, 10)] + [_clean_row(4)], 3, 4,
     [13, 12, 11, 10], True),
])
def test_fetch_export_rows_paging_and_truncation(monkeypatch, rows, page, cap, expected_ids, expected_truncated):
    monkeypatch.setattr("sherpa.store.list_export_messages", _fake_list_export_messages(rows))
    if page is not None:
        monkeypatch.setattr(IL, "_EXPORT_PAGE", page)
    result, truncated = IL.fetch_export_rows(time_from=None, output_cap=cap)
    assert [r["id"] for r in result] == expected_ids
    assert truncated is expected_truncated
