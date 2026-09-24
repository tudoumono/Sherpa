"""STOP-1: 調査予算到達（`evidence_packet.stop_reason` が turns_exhausted/budget_exceeded/
tools_per_turn_exceeded）の回答に、本文とは別要素で「途中までの結果」注記を出す e2e。

回答ヘッダ（調べる深さ・所要時間表示・SC-6c）や `.ftrace-stopreason`（思考の流れパネルの終了理由・
EXT-4）とは別の描画対象（`answerHTML` が組む回答本文直下の `.budget-note`）を確認するため、
両者と衝突しない専用ファイルに分離する。
"""
from __future__ import annotations

import json

from mock_api import (
    IMPACT_ANSWER, V2_BLOCKED_ANSWER, V2_BUDGET_ANSWER, V2_NOSUB_ANSWER, V2_REFUSAL_ANSWER,
    V2_TOOLS_LIMIT_ANSWER, install_api_mocks,
)


def _send_and_get_answer_locator(page, web_base_url, answer):
    events = [{"type": "trace_meta", "trace_version": 2},
             {"type": "answer", "conversation_id": 101,
              "message": {"answer": answer, "trace": []}}]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    return page.locator("#messages")


def test_budget_note_shown_when_turns_exhausted(page, web_base_url):
    from playwright.sync_api import expect

    messages = _send_and_get_answer_locator(page, web_base_url, V2_BUDGET_ANSWER)
    expect(messages).to_contain_text("影響範囲分析")
    note = page.locator(".budget-note").last
    expect(note).to_be_visible()
    expect(note).to_contain_text("調査の上限に達したため、途中までの結果で答えています")
    expect(note).to_contain_text("続きを調べて")


def test_budget_note_shown_when_tools_per_turn_exceeded(page, web_base_url):
    """実環境の実害シナリオ（`SHERPA_AGENTIC_MAX_TOOLS_PER_TURN` 到達）そのものの再現。"""
    from playwright.sync_api import expect

    messages = _send_and_get_answer_locator(page, web_base_url, V2_TOOLS_LIMIT_ANSWER)
    expect(messages).to_contain_text("影響範囲分析")
    expect(page.locator(".budget-note").last).to_be_visible()


def test_budget_note_not_shown_for_natural_completion(page, web_base_url):
    """通常完了（`no_tool_calls`）では注記を出さない。"""
    from playwright.sync_api import expect

    messages = _send_and_get_answer_locator(page, web_base_url, V2_NOSUB_ANSWER)
    expect(messages).to_contain_text("影響範囲分析")
    expect(page.locator(".budget-note")).to_have_count(0)


def test_budget_note_not_shown_for_answer_without_evidence_packet(page, web_base_url):
    """非 agentic（Evidence Packet 自体が無い）回答では例外にならず注記も出ない。"""
    from playwright.sync_api import expect

    messages = _send_and_get_answer_locator(page, web_base_url, IMPACT_ANSWER)
    expect(messages).to_contain_text("影響範囲分析")
    expect(page.locator(".budget-note")).to_have_count(0)


def test_budget_note_not_shown_for_other_incomplete_stop_reasons(page, web_base_url):
    """根拠不足で中断（`evaluation_blocked`）・回答拒否（`refusal`）は予算到達とは別カテゴリ——
    「範囲を絞る／続きを調べて」という案内は当てはまらないため注記の対象外にする。"""
    from playwright.sync_api import expect

    for answer in (V2_BLOCKED_ANSWER, V2_REFUSAL_ANSWER):
        messages = _send_and_get_answer_locator(page, web_base_url, answer)
        expect(messages).to_contain_text("影響範囲分析")
        expect(page.locator(".budget-note")).to_have_count(0)


# ===== STOP-1: 保存会話の再表示（ライブ SSE を経由しない）でも同じ DOM 配置 =====
# `install_api_mocks` はライブ用の単一 catch-all route（`**/*`）を登録するため、特定の会話取得
# だけを上書きしたい場合は、その後に**より限定的な** `page.route` を追加登録すればよい
# （Playwright は最後に登録した route から順に試す・`test_message_feedback_ui.py` と同じ手法）。

_HISTORY_CONV_ID = 9991


def _install_history_conversation(page, answer):
    install_api_mocks(page)

    def handle_get(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "conversation": {"id": _HISTORY_CONV_ID, "title": "予算到達の履歴", "origin": "own",
                             "version": "v1", "read_only": False, "contains_personal_workspace": False},
            "messages": [
                {"role": "user", "content": "消費税率を変えたい。影響は？",
                 "created_at": "2026-09-01T09:00:00+00:00"},
                {"role": "assistant", "answer": answer, "trace": [],
                 "created_at": "2026-09-01T09:00:20+00:00"},
            ]}))
    page.route(f"**/conversations/{_HISTORY_CONV_ID}", handle_get)


def test_budget_note_dom_placement_on_reopened_history(page, web_base_url):
    """ライブ SSE を経由せず保存会話を開き直したときも、`.budget-note` が `.headline` の直後の
    兄弟要素として `.a-body` 内に出ること（実サーバの保存経路とは独立に、履歴復元経路＝
    `history.js`→同じ `answerHTML` でも再現されることの固定）。"""
    from playwright.sync_api import expect

    _install_history_conversation(page, V2_BUDGET_ANSWER)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate(f"window.__sherpaChatTest.openConversation({_HISTORY_CONV_ID})")

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    placed = page.locator(".a-body > .headline + .budget-note")
    expect(placed).to_have_count(1)
    expect(placed).to_contain_text("調査の上限に達したため、途中までの結果で答えています")
