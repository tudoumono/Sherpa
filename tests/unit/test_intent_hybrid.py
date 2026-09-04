"""hybrid intent オーケストレーション（chat_service._build_router）結合テスト。

intent_llm.classify を差し替えて Tier1→2→3 の分岐＋per-turn memoize を検証（ネットワーク/DB 不要）。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import chat_service as C  # noqa: E402
from sherpa import intent_llm  # noqa: E402

_CONFLICT = "税率を変えたら夜間バッチが落ちる？"     # 影響(変え)＋障害(落ち)＝heuristic 曖昧


def _patch(fn):
    """`fn` は `(message, settings, **kw)` を受ける（S1・2026-07-15-LLMオーケストレーション実装計画.md §3:
    `chat_service._route` が `intent_llm.classify(message, settings, user_id=user_id, world=world)` と
    キーワード引数付きで呼ぶため、`intent_llm.classify` を丸ごと差し替える fake は `**kw` で受け止める）。"""
    orig = intent_llm.classify
    intent_llm.classify = fn
    return orig


def test_confident_skips_llm():
    calls = []
    orig = _patch(lambda m, s, **kw: calls.append(m) or {"lens": "impact", "confident": True})
    try:
        d = C._build_router([], "v1", {}, can_ask=True)("夜間バッチが ABEND。原因は？")   # 強 cue 単独→確定
        assert d["lens"] == "troubleshoot" and calls == []                                # LLM を呼ばない
    finally:
        intent_llm.classify = orig


def test_ambiguous_uses_llm_when_confident():
    orig = _patch(lambda m, s, **kw: {"lens": "impact", "confident": True})
    try:
        d = C._build_router([], "v1", {"openai_api_key": "k"}, can_ask=True)(_CONFLICT)
        assert d["lens"] == "impact" and d["confident"] is True and "AI判定" in d["reason"]
    finally:
        intent_llm.classify = orig


def test_ambiguous_clarify_when_llm_unsure_and_can_ask():
    orig = _patch(lambda m, s, **kw: None)                       # LLM 未接続/不明
    try:
        d = C._build_router([], "v1", {}, can_ask=True)(_CONFLICT)
        assert d["lens"] == "clarify" and d["question"]["type"] == "question"
    finally:
        intent_llm.classify = orig


def test_ambiguous_qa_fallback_when_cannot_ask():
    orig = _patch(lambda m, s, **kw: None)
    try:
        d = C._build_router([], "v1", {}, can_ask=False)(_CONFLICT)   # 非対話＝clarify 不可
        assert d["lens"] == "qa" and d["confident"] is True
    finally:
        intent_llm.classify = orig


def test_llm_low_confidence_goes_to_clarify():
    orig = _patch(lambda m, s, **kw: {"lens": "impact", "confident": False})   # LLM も自信なし
    try:
        d = C._build_router([], "v1", {}, can_ask=True)(_CONFLICT)
        assert d["lens"] == "clarify"
    finally:
        intent_llm.classify = orig


def test_per_turn_memoize_single_classify():
    calls = []
    orig = _patch(lambda m, s, **kw: calls.append(m) or None)
    try:
        r = C._build_router([], "v1", {}, can_ask=False)
        r(_CONFLICT)
        r(_CONFLICT)                                        # 同ターンの再 route（_GenProvider）でも
        assert len(calls) == 1                              # LLM 分類は1回だけ（二重実行しない）
    finally:
        intent_llm.classify = orig


# ---- High-2（2026-07-07-フィードバック一括.md F2-2）: 「確認してから進めて」の決定的ガード ----
# ルーター層で provider/dispatch 到達前に確認カード（question）を出す。プロンプト遵守任せ（agents 側 F2）では
# 「必ず」を保証できないため。回答の再送（確認ID 付き）では発動しない＝再質問ループ防止。
_CONFIRM = "税率の一覧を Excel にまとめて。確認してから進めて。"


def test_confirm_first_emits_question_before_provider():
    # (a) トリガー句＋確認ID なし → provider に到達せず clarify question が出る。LLM 分類は呼ばれない。
    calls = []
    orig = _patch(lambda m, s, **kw: calls.append(m) or {"lens": "author", "confident": True})
    try:
        d = C._build_router([], "v1", {}, can_ask=True)(_CONFIRM)
        assert d["lens"] == "clarify" and d["question"]["type"] == "question"
        assert d["question"]["allow_free_text"] is True
        assert not d["question"]["interaction_id"].startswith("ask-")   # lens 選択 clarify とは別採番（VOCAB-1: lens-→ask-）
        assert calls == []                                               # ガードが先＝LLM 分類に到達しない
    finally:
        intent_llm.classify = orig


def test_confirm_first_not_triggered_when_confirm_id_present():
    # (b) トリガー句が残っていても確認ID 付き（回答の再送）なら発動しない＝ループしない。
    orig = _patch(lambda m, s, **kw: {"lens": "author", "confident": True})
    try:
        msg = ("選択: 対象範囲（どの資料/システムか）\n確認ID: confirm-abcd\n"
               "元の依頼: 税率の一覧を Excel にまとめて。確認してから進めて。")
        d = C._build_router([], "v1", {}, can_ask=True)(msg)
        assert d["lens"] != "clarify"
    finally:
        intent_llm.classify = orig


def test_confirm_first_ignored_when_cannot_ask():
    # 非対話（can_ask=False）は質問できないので通常ルーティングへ委ねる（clarify にしない）。
    orig = _patch(lambda m, s, **kw: {"lens": "author", "confident": True})
    try:
        d = C._build_router([], "v1", {}, can_ask=False)(_CONFIRM)
        assert d["lens"] != "clarify"
    finally:
        intent_llm.classify = orig


def test_confirm_first_no_trigger_unaffected():
    # (c) トリガー句が無ければ従来どおり（clarify にはしない）。
    orig = _patch(lambda m, s, **kw: {"lens": "author", "confident": True})
    try:
        d = C._build_router([], "v1", {}, can_ask=True)("税率の一覧を Excel にまとめて。")
        assert d["lens"] != "clarify"
    finally:
        intent_llm.classify = orig


def test_confirm_first_question_carries_resolved_lens_layer_scope_rv1_3():
    """RV1 #3/RV2 #1: 確認カードの payload に、確認が出た時点で解決済みだった調べ方・探す対象・
    範囲・lens_source・lens_block を保持する（スラッシュ接頭辞由来の explicit_lens も含む・
    回答再送の1回限りの戻し先）。"""
    scope_meta = {"world": "w1", "scope_paths": ["4期/設計"], "source": "explicit",
                 "layer": "docs", "lens_source": "slash", "lens_block": "qa"}
    d = C._build_router([], "v1", {}, can_ask=True, explicit_lens="impact",
                        scope_meta=scope_meta)(_CONFIRM)
    assert d["lens"] == "clarify"
    q = d["question"]
    assert q["lens"] == "impact"
    assert q["layer"] == "docs"
    assert q["scope_paths"] == ["4期/設計"]
    assert q["lens_source"] == "slash"
    assert q["lens_block"] == "qa"


def test_confirm_first_question_lens_none_when_auto():
    d = C._build_router([], "v1", {}, can_ask=True)(_CONFIRM)
    assert d["question"]["lens"] is None
    assert d["question"]["scope_paths"] == []
    assert d["question"]["lens_source"] is None
    assert d["question"]["lens_block"] is None


def test_confirm_first_slash_resend_restores_lens_source_and_block_rv2_1():
    """RV2 #1: /影響 ...確認してから進めて → 確認カードの payload（lens_source=slash・
    lens_block=継続設定）→ フロントが復元した「先頭にスラッシュを戻した再送」を通すと、
    _resolve_lens/_resolve_scope が同じ lens_source="slash"・lens_block を再現する
    （新しい復元経路を使わず、既存のスラッシュ解決をそのまま再利用する）。"""
    from sherpa import chat_service as CS

    # 1ターン目: ブロックは「qa」を継続選択中に /影響 ... を送る。
    explicit_lens, lens_source, lens_block, message = CS._resolve_lens(
        "qa", "/影響 税率表を確認してから進めて。")
    assert (explicit_lens, lens_source, lens_block) == ("impact", "slash", "qa")
    scope_meta = CS._resolve_scope(message, "w1", [], lens_source=lens_source, lens_block=lens_block)
    d = C._build_router([], "w1", {}, can_ask=True, explicit_lens=explicit_lens,
                        scope_meta=scope_meta)(message)
    assert d["lens"] == "clarify"
    q = d["question"]
    assert q["lens"] == "impact" and q["lens_source"] == "slash" and q["lens_block"] == "qa"

    # web/chat.js の data-ask-submit ハンドラと同じ組み立て: lens_source=="slash" なら
    # 再送本文の先頭へ元の接頭辞を復元し、override の lens には lens_block を渡す。
    resend_message_raw = (f"/影響 確認事項: {d['question']['prompt']}\n"
                          f"確認ID: {q['interaction_id']}\n選択: 対象範囲（どの資料/システムか）\n"
                          f"元の依頼: {q['original_message']}")
    explicit2, lens_source2, lens_block2, stripped2 = CS._resolve_lens(q["lens_block"], resend_message_raw)
    assert explicit2 == "impact" and lens_source2 == "slash" and lens_block2 == "qa"
    scope_meta2 = CS._resolve_scope(stripped2, "w1", [], lens_source=lens_source2, lens_block=lens_block2)
    assert scope_meta2["lens_source"] == "slash"
    assert scope_meta2["lens_block"] == "qa"   # ブロックの継続設定は変わらない（1回限りの明示のまま）
