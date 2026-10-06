"""回答の形（本文・注記・完了状態の分離・`sherpa/answer_shape.py`）の契約。DB 不要。"""
from __future__ import annotations

import pytest

from sherpa import answer_shape, chat_service, stop_kind
from sherpa.providers.codex.continuation import _pick_codex_headline
from sherpa.providers.codex.structured import split_review_preamble
from sherpa.store.shares import _safe_share_answer

_DECISION = {"lens": "qa", "reason": "test"}


def test_finalize_keeps_codex_body_and_puts_no_sources_into_notices():
    body = "原因は設定値の不一致です。\n\n参照した資料: なし"
    env = {"headline": body, "summary": {"total": 0}, "data": {"citations": []}, "sources": [],
           "codex_multi_agent": True, "scope": {"scope_paths": [], "layer": "both", "layer_applied": True,
                                                "depth_profile": "max"}}
    out = chat_service._finalize(env, _DECISION, "質問")
    assert out["body"] == body and out["answer_schema"] == 2
    assert [n["kind"] for n in out["notices"]] == ["no_sources"]
    assert out["headline"] == out["notices"][0]["text"] + "\n\n" + body
    assert out["completion"] == "complete" and out["investigation_summary"]["items"] == []


@pytest.mark.parametrize("env", [
    {"limits": {"wall_clock_hit": True}},
    {"limits": {"ledger_incomplete": True}},
    {"limits": {"wall_clock_hit": True}, "codex_multi_agent": True},
    {"codex_stopped_early": True},
])
def test_timeout_and_unfinished_ledger_never_resolve_to_completed(env):
    env = {"headline": "回収済みの回答です。", "data": {}, **env}
    out = chat_service._finalize(env, _DECISION, "質問")
    assert out["completion"] == "partial"
    assert out["stop_kind"] in ("timeout", "codex_partial") and out["stop_kind"] != "completed"
    assert out["body"] == "回収済みの回答です。"


def test_old_rows_read_headline_as_body_and_share_keeps_new_fields():
    old = {"lens": "qa", "headline": "旧形式の回答です。", "sources": []}
    assert answer_shape.body_of(old) == "旧形式の回答です。" and answer_shape.notices_of(old) == []
    shared = _safe_share_answer(old)
    assert shared["headline"] == "旧形式の回答です。" and "body" not in shared and "notices" not in shared

    new = answer_shape.seal({"lens": "qa", "headline": "本文です。", "sources": [], "completion": "partial"})
    answer_shape.add_notice(new, "wall_clock", "時間の上限に達しました。")
    new["notices"].append({"kind": 1, "text": "壊れた要素"})
    shared = _safe_share_answer(new)
    assert shared["body"] == "本文です。" and shared["completion"] == "partial" and shared["answer_schema"] == 2
    assert shared["notices"] == [{"kind": "wall_clock", "text": "時間の上限に達しました。"}]
    assert shared["headline"] == "時間の上限に達しました。\n\n本文です。"
    assert shared["investigation_summary"] == {"v": 1, "items": []}


def test_trimmed_preamble_and_trailing_progress_are_recorded_not_lost():
    dropped: list = []
    kept = _pick_codex_headline(["結論は設定の不一致です。次に詳細を確認します。"], dropped=dropped)
    assert kept == "結論は設定の不一致です。"
    assert dropped == [{"kind": "trailing_progress", "text": "次に詳細を確認します。"}]
    body, preamble = split_review_preamble("点検して前回の回答を答え直します。\n\n本文です。")
    assert body == "本文です。" and preamble.startswith("点検")
    assert stop_kind.derive_completion({}) == "complete"


def test_investigation_summary_lists_limits_unconfirmed_and_broken_items_in_plain_words():
    """打ち切り・確認できた／できなかった項目（51 件目以降は「ほか N 件」）・壊れた項目・調べた量が、利用者向けの文で並ぶ。"""
    unconfirmed = [{"item": f"項目{i}", "reason": "検索が失敗し、確認できませんでした"} for i in range(53)]
    env = {"headline": "回答です。", "data": {},
           "limits": {"search_truncated": 2, "total_budget_hit": True, "wall_clock_hit": True, "duplicate_tool_call": 0},
           "investigation": {"counts": {"source_confirmed": 3, "unverified": 53}, "unconfirmed_items": unconfirmed,
                             "invalid": ["x1", "x2"], "effort": {"searches": 7, "docs_read": 4}}}
    items = answer_shape.seal(env)["investigation_summary"]["items"]
    texts = [(i["label"], i["text"]) for i in items]
    assert ("打ち切り", "検索の結果が多く、途中で打ち切った検索が 2 回ありました（残りは確認していません）。") in texts
    assert sum(1 for label, _t in texts if label == "打ち切り") == 3     # 0 回の重複拒否は出さない
    assert ("確認できた項目", "3 件（ソースで確認 3 件）") in texts
    assert sum(1 for label, _t in texts if label == "確認できなかった項目") == 51   # 50 件 + 「ほか 3 件」
    assert ("確認できなかった項目", "ほか 3 件") in texts
    assert any(label == "壊れていた項目" and "2 件" in t and "x1" in t for label, t in texts)
    assert ("調べた量", "検索 7 回・読んだ資料 4 件（確認できた範囲の数です）") in texts
    # 何度封印しても同じ（事実から作り直す）・自前で詰めた中身は上書きしない。
    assert answer_shape.seal(env)["investigation_summary"]["items"] == items
    own = {"headline": "x", "limits": {"wall_clock_hit": True},
           "investigation_summary": {"v": 1, "items": [{"label": "自前", "text": "詰めた中身"}]}}
    assert answer_shape.seal(own)["investigation_summary"]["items"] == [{"label": "自前", "text": "詰めた中身"}]


def test_summary_hides_sensitive_names_shows_broken_overflow_and_share_keeps_counts_only():
    """秘匿名の項目は件数に落とし、壊れた項目は総数と「ほか N 件」を示す。共有には項目名を載せず件数だけ・確認できなかった資料は秘匿名を除いて通す。"""
    env = {"headline": "回答", "limits": {},
           "sources_unverified": [{"path": "仕様/a.md", "reason": "今回の範囲の外です"},
                                  {"path": "鍵/id.pem", "reason": "今回の範囲の外です"}],
           "sources_unverified_hidden": 1,
           "investigation": {"unconfirmed_items": [{"item": "鍵/id.pem", "reason": "x"},
                                                   {"item": "田中さんの件", "reason": "確認できませんでした"}],
                             "invalid": [f"b{i}" for i in range(50)], "invalid_total": 73}}
    out = answer_shape.seal(env)
    texts = [i["text"] for i in out["investigation_summary"]["items"]]
    assert "id.pem" not in str(texts) and "名前を表示できない項目 1 件" in texts
    assert any(t.startswith("73 件") and "ほか 23 件" in t for t in texts)
    shared = _safe_share_answer(out)
    s_items = shared["investigation_summary"]["items"]
    assert "田中さん" not in str(s_items) and "b1" not in str(s_items)
    assert {"label": "壊れていた項目", "text": "73 件（項目名は共有では表示しません）"} in s_items
    assert {"label": "確認できなかった項目", "text": "2 件（項目名は共有では表示しません）"} in s_items
    assert shared["sources_unverified"] == [{"path": "仕様/a.md", "reason": "今回の範囲の外です"}]
    assert shared["sources_unverified_hidden"] == 2
