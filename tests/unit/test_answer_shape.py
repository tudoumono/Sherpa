"""回答の形（本文・注記・完了状態の分離・`sherpa/answer_shape.py`）の契約。DB 不要。"""
from __future__ import annotations

import pytest

from sherpa import answer_shape, investigation_summary, chat_service, stop_kind
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
    """確認できた／できなかった項目（51 件目以降は「ほか N 件」）・壊れた項目が、利用者向けの文で並ぶ。道具の打ち切りと調べた量は回答に出さず、調査の記録に残す。"""
    unconfirmed = [{"item": f"項目{i}", "reason": "検索が失敗し、確認できませんでした"} for i in range(53)]
    env = {"headline": "回答です。", "data": {},
           "limits": {"search_truncated": 2, "total_budget_hit": True, "wall_clock_hit": True, "duplicate_tool_call": 0},
           "investigation": {"counts": {"source_confirmed": 3, "unverified": 53}, "unconfirmed_items": unconfirmed,
                             "invalid": ["x1", "x2"], "effort": {"searches": 7, "docs_read": 4}}}
    items = answer_shape.seal(env)["investigation_summary"]["items"]
    texts = [(i["label"], i["text"]) for i in items]
    assert not any(label in ("打ち切り", "調べた量") for label, _t in texts)
    # 読み取り量の上限は、打てる手を添えて「注意」として出す（時間の上限は回答の注記が受け持つ）。
    assert any(label == "注意" and "読み取り量の上限" in t and "深く" in t for label, t in texts)
    assert ("確認できた項目", "3 件（ソースで確認 3 件）") in texts
    assert sum(1 for label, _t in texts if label == "確認できなかった項目") == 51   # 50 件 + 「ほか 3 件」
    assert ("確認できなかった項目", "ほか 3 件") in texts
    assert any(label == "壊れていた項目" and "2 件" in t and "x1" in t for label, t in texts)
    record = [(i["label"], i["text"]) for i in investigation_summary.record_lines(env["limits"], env["investigation"]["effort"])]
    assert record == [("調べた量", "検索 7 回・読んだ資料 4 件（確認できた範囲の数です）")]
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


_NEW_SHAPE = {
    "referenced_docs": [{"path": "docs/a.md", "ranges": [[3, 9], [0, 1], "x"], "unopened": True},
                        {"path": "src/secrets/.env", "ranges": [[1, 2]]}, {"path": 5}],
    "referenced_docs_more": 2,
    "impact_list": {"v": 1, "traced": True, "reasons": ["深さの上限で打ち切りました"],
                    "rows": [{"name": "OrderBatch", "path": "src/OrderBatch.java", "role": "origin", "state": "used"},
                             {"name": "src/secrets/.env", "state": "candidate"}, {"name": "x", "state": "bogus"}]},
    "found_docs": [{"path": "docs/b.md"}, {"path": "src/secrets/.env"}], "found_docs_more": 4,
    "how": {"mode": "investigate", "doc_focus": True},
}


def test_new_shape_fields_are_shared_without_sensitive_names_and_survive_seal():
    shared = _safe_share_answer(answer_shape.seal({"lens": "qa", "headline": "本文", "sources": [], **_NEW_SHAPE}))
    assert shared["referenced_docs"] == [{"path": "docs/a.md", "ranges": [[3, 9]], "unopened": True}]
    assert shared["referenced_docs_hidden"] == 1 and shared["referenced_docs_more"] == 2
    il = shared["impact_list"]
    assert [r["name"] for r in il["rows"]] == ["OrderBatch"] and il["hidden"] == 1 and il["traced"] is True
    assert shared["found_docs"] == [{"path": "docs/b.md"}] and shared["found_docs_hidden"] == 1
    assert shared["found_docs_more"] == 4 and shared["how"] == {"mode": "investigate", "doc_focus": True}
    assert ".env" not in str(shared)
    leaky = {"impact_list": {"traced": True, "reasons": ["src/secrets/.env を解析できません", "深さの上限です"],
                             "rows": [{"name": "A", "state": "used", "reason": "id_rsa.pem を参照"}]}}
    il2 = _safe_share_answer({"lens": "qa", "headline": "h", **leaky})["impact_list"]
    assert il2["reasons"] == ["深さの上限です"] and il2["rows"][0]["reason"] == ""
    assert ".env" not in str(il2) and "id_rsa" not in str(il2)


def test_old_answers_get_no_new_fields_and_stay_unfaked():
    old = {"lens": "impact", "headline": "旧形式の回答です。", "sources": [], "data": {"items": []}}
    shared = _safe_share_answer(old)
    assert not {"referenced_docs", "impact_list", "found_docs", "how"} & set(shared) and shared["lens"] == "impact"
    assert answer_shape.impact_list_of(old) is None and answer_shape.referenced_docs_of(old) == []
    assert answer_shape.impact_list_of({"impact_list": {"traced": "yes"}}) is None


def test_received_share_read_path_hides_sensitive_names_in_new_fields():
    from sherpa.store.shares import _strip_shared_message
    out = _strip_shared_message({"role": "assistant", "answer": {"headline": "本文", **_NEW_SHAPE}})["answer"]
    assert [r["path"] for r in out["referenced_docs"]] == ["docs/a.md"] and out["referenced_docs_hidden"] == 1
    assert out["found_docs_hidden"] == 1 and ".env" not in str(out)


def test_plan_style_summary_leaves_unconfirmed_items_to_the_body_and_keeps_quality_notes():
    """計画書の方式（C-check）では、確認できなかった項目は本文の補足に出るので要約に重ねない。主張を推定に下げた知らせは残す。"""
    env = {"headline": "回答", "limits": {"claims_unmatched": 1, "search_truncated": 3},
           "investigation": {"ledger": "plan", "unconfirmed_items": [{"item": "対象A", "reason": "範囲外"}]}}
    texts = [(i["label"], i["text"]) for i in answer_shape.seal(env)["investigation_summary"]["items"]]
    assert not any(label == "確認できなかった項目" for label, _t in texts)
    assert ("打ち切り", "調べた記録と合わない主張があったため、「推定」に下げています。") in texts
    assert not any("検索の結果が多く" in t for _l, t in texts)


def test_search_cutoff_is_shown_with_what_to_do_only_for_exhaustive_requests():
    """検索の打ち切りは、全件・一覧・影響範囲の依頼のときだけ、打てる手と一緒に出す。普段の質問・読み取りの切り詰めは出さない。"""
    base = {"headline": "回答", "limits": {"search_truncated": 2, "tool_result_clipped": 5}}
    plain = answer_shape.seal({**base, "investigation": {"ledger": "plan"}})["investigation_summary"]["items"]
    assert plain == []
    wide = answer_shape.seal({**base, "investigation": {"ledger": "plan", "exhaustive": True}})["investigation_summary"]["items"]
    assert len(wide) == 1 and wide[0]["label"] == "注意"
    assert "資料フォルダを絞る" in wide[0]["text"] and "深く" in wide[0]["text"] and "切り詰め" not in wide[0]["text"]

