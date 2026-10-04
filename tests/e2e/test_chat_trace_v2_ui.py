"""trace_version=2（サブエージェント レーン・集約・実行の分担サマリ・検証バッジ・終了理由）と
trace_version=1（従来）の後方互換の e2e。ライブ配信と履歴復元が同じ階層描画になることを固定する。"""
from __future__ import annotations

import json
import time

import pytest

from mock_api import (
    IMPACT_ANSWER, V2_BLOCKED_ANSWER, V2_BUCKET_DISMANTLE_PRESERVES_ARRIVAL_ORDER_TRACE,
    V2_BUCKET_REPARENT_ORDER_A_TRACE,
    V2_BUCKET_REPARENT_ORDER_B_TRACE, V2_BUCKET_REPARENT_ORDER_C_TRACE, V2_BUDGET_ANSWER,
    V2_BUCKET_SURVIVES_SUBAGENT_LANE_INTERLEAVED_TRACE,
    V2_BUCKET_SURVIVING_FRAME_REPOSITIONS_ON_DETACH_TRACE,
    V2_CODEX_OLLAMA_ANSWER, V2_CONTENT_FILTERED_ANSWER, V2_LANE_ANSWER, V2_LANE_TRACE,
    V2_NOSUB_ANSWER, V2_NOSUB_TRACE, V2_NOUSAGE_ANSWER, V2_PARENT_ID_OUT_OF_ORDER_TRACE,
    V2_PARENT_ID_TRACE, V2_REFUSAL_ANSWER, V2_TOOLS_LIMIT_ANSWER, V2_TRUNCATED_ANSWER,
    V2_UNKNOWN_STOP_REASON_ANSWER, V2_UNKNOWN_STOP_REASON_TRACE, install_api_mocks,
)

expect = pytest.importorskip("playwright.sync_api").expect

Q = "消費税率を変えたい。影響は？"
DOC = "4期/02_設計/01_基本設計/税計算仕様書.md"
SSE = {"Content-Type": "text/event-stream"}
V2_META = {"type": "trace_meta", "trace_version": 2}
SLUGS = ("researcher", "search-helper-openai", "search-helper-ollama", "sub:researcher:1")


def _answer_event(answer, trace=None):
    message = {"answer": answer} if trace is None else {"answer": answer, "trace": trace}
    return {"type": "answer", "conversation_id": 101, "message": message}


def _run(page, base, events, text=Q):
    install_api_mocks(page, stream_events=events)
    page.goto(f"{base}/chat.html")
    page.locator("#input").fill(text)
    page.locator("#send").click()


def _run_trace(page, base, trace, answer=IMPACT_ANSWER):
    _run(page, base, [V2_META, *trace, _answer_event(answer, trace)])
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")


def _children(step):
    return step.locator("> .fbody").locator("> .fchildren").locator("> .fstep")


def _wait_until(predicate, timeout_ms=5000, interval_ms=20, page=None, message="条件が満たされなかった"):
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        assert time.monotonic() < deadline, message
        page.wait_for_timeout(interval_ms)


def _settle(page):
    page.evaluate("async () => { try { await fetch('/chat/turns/running'); } catch (e) {} }")


def _no_slug_leak(text):
    for slug in SLUGS:
        assert slug not in text, f"内部 slug {slug!r} が画面の可視テキストに出ている: {text!r}"


def test_v2_live_stream_shows_agent_lane_aggregation_and_summary(page, web_base_url):
    _run_trace(page, web_base_url, V2_LANE_TRACE, V2_LANE_ANSWER)

    lane = page.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane).to_contain_text("下調べ役")
    expect(lane).not_to_contain_text("researcher")
    expect(lane).to_contain_text("ローカル: qwen2.5")
    expect(lane.locator(".fagent-status")).to_contain_text("完了")
    expect(lane).to_contain_text("調査の回数 1")
    expect(lane).to_contain_text("調べる操作の回数 4")
    expect(lane).not_to_contain_text("候補")
    expect(page.locator("#flow")).to_contain_text("根拠を確定")
    expect(page.locator("#flow")).to_contain_text("2 件の根拠を機械検証済みとして確定しました")

    agg = lane.locator(".fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    expect(agg.locator(".fagg-body")).to_be_hidden()
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body")).to_be_visible()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).to_contain_text("消費税率")
    expect(agg).to_contain_text("TAX-RATE")

    summary = page.locator(".provider-summary")
    expect(summary).to_have_count(1)
    expect(summary).to_contain_text("ローカル AI")
    expect(summary).to_contain_text("クラウド AI")
    expect(summary).to_contain_text("回答の合成")
    expect(summary).to_contain_text("その他の処理 1 回")
    expect(page.locator(".ftrace-stopreason")).to_contain_text("自然終了")

    sub_meta = page.locator(".usage-sub-meta")
    expect(sub_meta).to_have_count(1)
    sub_meta.locator("summary").click()
    expect(sub_meta).to_contain_text("下調べ役: 入力 300 / 出力 40 トークン")
    expect(sub_meta).not_to_contain_text("search-helper")

    _no_slug_leak(page.locator("#flow").inner_text() + page.locator("#messages").inner_text())


@pytest.mark.parametrize("trace, grandchild", [
    (V2_PARENT_ID_TRACE, True),
    (V2_PARENT_ID_OUT_OF_ORDER_TRACE, False),
])
def test_v2_parent_id_nests_child_nodes_under_parent(page, web_base_url, trace, grandchild):
    _run_trace(page, web_base_url, trace)

    child = _children(page.locator("#flow .fstep", has_text="進め方を計画").first)
    expect(child).to_have_count(1)
    expect(child).to_contain_text("資料を検索（語句そのまま）")
    if grandchild:
        sub = _children(child)
        expect(sub).to_have_count(1)
        expect(sub).to_contain_text("検索結果を確認")


def test_v2_bucket_reparent_order_a_full_cleanup_before_aggregation(page, web_base_url):
    _run_trace(page, web_base_url, V2_BUCKET_REPARENT_ORDER_A_TRACE)

    child = _children(page.locator("#flow .fstep", has_text="進め方を計画").first)
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")
    expect(page.locator("#flow .fagg")).to_have_count(0)


def test_v2_bucket_reparent_order_b_partial_cleanup_then_later_aggregation(page, web_base_url):
    _run_trace(page, web_base_url, V2_BUCKET_REPARENT_ORDER_B_TRACE)

    child = _children(page.locator("#flow .fstep", has_text="進め方を計画").first)
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")
    agg = page.locator("#flow .fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).not_to_contain_text("A")


def test_v2_bucket_reparent_order_c_detach_from_existing_aggregation_frame(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("""async () => {
        const { TraceTreeV2 } = await import('/chat/render.js');
        const container = document.createElement('div');
        container.id = 'test-order-c-tree';
        document.body.appendChild(container);
        window.__testTree = new TraceTreeV2(container, { live: false });
    }""")

    def feed(event):
        page.evaluate("(e) => window.__testTree.addOrUpdate(e)", event)

    for e in V2_BUCKET_REPARENT_ORDER_C_TRACE[:4]:
        feed(e)

    root = page.locator("#test-order-c-tree")
    child = _children(root.locator(".fstep", has_text="進め方を計画").first)
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")
    expect(root.locator(".fagg")).to_have_count(0)
    expect(root.locator("> .fstep.tool")).to_have_count(2)

    feed(V2_BUCKET_REPARENT_ORDER_C_TRACE[4])

    agg = root.locator(".fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).not_to_contain_text("A")
    expect(child).to_have_count(1)


def test_v2_bucket_dismantle_preserves_arrival_order_of_interleaved_siblings(page, web_base_url):
    _run_trace(page, web_base_url, V2_BUCKET_DISMANTLE_PRESERVES_ARRIVAL_ORDER_TRACE)

    expect(page.locator("#flow .fagg")).to_have_count(0)
    top = page.locator("#flow details.fturn .fturn-body > .fstep")
    expect(top).to_have_count(4)
    for i, text in enumerate(("念のため確認", "B", "C", "進め方を計画")):
        expect(top.nth(i)).to_contain_text(text)
    child = _children(top.nth(3))
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")


def test_v2_bucket_surviving_frame_repositions_on_detach(page, web_base_url):
    _run_trace(page, web_base_url, V2_BUCKET_SURVIVING_FRAME_REPOSITIONS_ON_DETACH_TRACE)

    body = "#flow details.fturn .fturn-body"
    top = page.locator(f"{body} > .fstep, {body} > .fagg")
    expect(top).to_have_count(3)
    expect(top.nth(0)).to_contain_text("念のため確認")
    agg = top.nth(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    expect(top.nth(2)).to_contain_text("進め方を計画")
    agg.locator(".fagg-head").click()
    expect(agg.locator(".fagg-body .fstep")).to_have_count(3)
    expect(agg).not_to_contain_text("A")
    child = _children(top.nth(2))
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")


def test_v2_bucket_dismantle_repositions_around_subagent_lane(page, web_base_url):
    _run_trace(page, web_base_url, V2_BUCKET_SURVIVES_SUBAGENT_LANE_INTERLEAVED_TRACE)

    expect(page.locator("#flow .fagg")).to_have_count(0)
    body = "#flow details.fturn .fturn-body"
    top = page.locator(f"{body} > .fstep, {body} > .fagent")
    expect(top).to_have_count(4)
    for i, text in enumerate(("B", "下調べ役", "C", "進め方を計画")):
        expect(top.nth(i)).to_contain_text(text)
    child = _children(top.nth(3))
    expect(child).to_have_count(1)
    expect(child).to_contain_text("A")


def _usage_answer(is_local, stop_reason):
    return {**IMPACT_ANSWER, "trace_version": 2,
            "data": {**IMPACT_ANSWER["data"], "evidence_packet": {"stop_reason": stop_reason}},
            "usage": {"provider": "openai", "model": "gpt-5.5", "input_tokens": 300, "output_tokens": 40,
                      "cached_input_tokens": 0, "reasoning_output_tokens": 0, "is_local": is_local}}


@pytest.mark.parametrize("trace, answer, shown, absent", [
    (V2_NOSUB_TRACE, V2_NOSUB_ANSWER, ["すべてクラウド AI が担当しました"], []),
    ([], V2_CODEX_OLLAMA_ANSWER, ["すべてローカル AI が担当しました"], ["クラウド"]),
    ([], V2_NOUSAGE_ANSWER, ["担当不明"], ["すべてローカル", "すべてクラウド"]),
    ([], _usage_answer("constructor", "no_tool_calls"), ["担当不明"], ["function"]),
])
def test_v2_provider_summary_labels_locality_honestly(page, web_base_url, trace, answer, shown, absent):
    _run(page, web_base_url, [V2_META, *trace, _answer_event(answer, trace)])

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".fagent")).to_have_count(0)
    summary = page.locator(".provider-summary")
    expect(summary).to_have_count(1)
    for text in shown:
        expect(summary).to_contain_text(text)
    for text in absent:
        expect(summary).not_to_contain_text(text)


@pytest.mark.parametrize("answer, expected", [
    (V2_BLOCKED_ANSWER, "根拠不足で中断"),
    (V2_BUDGET_ANSWER, "調査の上限に到達"),
    (V2_TOOLS_LIMIT_ANSWER, "調べる操作の回数の上限に到達"),
    (V2_REFUSAL_ANSWER, "AI が回答を控えた"),
    (V2_TRUNCATED_ANSWER, "出力上限で途中終了"),
    (V2_CONTENT_FILTERED_ANSWER, "内容の制限で終了"),
    (_usage_answer("cloud", "constructor"), "終了理由を確認できませんでした"),
])
def test_v2_stop_reason_categories_map_to_plain_text(page, web_base_url, answer, expected):
    _run(page, web_base_url, [V2_META, _answer_event(answer, [])])
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".ftrace-stopreason")).to_contain_text(expected)


def test_v2_unknown_stop_reason_is_honest_not_a_guess(page, web_base_url):
    _run_trace(page, web_base_url, V2_UNKNOWN_STOP_REASON_TRACE, V2_UNKNOWN_STOP_REASON_ANSWER)

    expect(page.locator(".ftrace-stopreason")).to_contain_text("終了理由を確認できませんでした")
    lane = page.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane.locator(".fagent-status")).to_contain_text("完了")
    expect(lane.locator(".fagent-status.aborted")).to_have_count(0)
    expect(page.locator(".fagent-status.aborted")).to_have_count(0)


def test_v2_question_pause_shows_no_stop_reason_note(page, web_base_url):
    _run(page, web_base_url, [
        V2_META,
        {"type": "node", "id": "u1", "kind": "think", "label": "質問を理解",
         "detail": "内容を把握しました", "status": "done"},
        {"type": "question", "conversation_id": 101, "interaction_id": "q1", "mode": "single",
         "prompt": "対象範囲を選んでください", "options": [{"id": "a", "label": "全体"},
                                                   {"id": "b", "label": "一部"}],
         "allow_free_text": False},
    ])

    expect(page.locator(".askcard")).to_be_visible()
    expect(page.locator(".ftrace-stopreason")).to_have_count(0)


def test_v2_lane_status_becomes_aborted_when_connection_drops_mid_run(page, web_base_url):
    metrics = {"provider": "ollama", "model": "qwen2.5", "is_local": "local", "name": "下調べ役"}
    _run(page, web_base_url, [
        V2_META,
        {"type": "node", "id": "search-helper", "kind": "think", "label": "下調べ役に任せる",
         "detail": "qwen2.5 が資料を探して読みます", "status": "done", "agent_run_id": "sub:researcher:1",
         "metrics": metrics},
        {"type": "node", "id": "sub:researcher:1:grep-1", "kind": "tool", "label": "資料を検索（語句そのまま）",
         "detail": "「消費税率」", "status": "active", "agent_run_id": "sub:researcher:1",
         "metrics": metrics},
    ])

    lane = page.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane.locator(".fagent-status")).to_contain_text("中断")
    expect(page.locator(".ftrace-stopreason")).to_contain_text("エラー")


def test_v2_send_discards_stale_turn_start_response_after_conversation_switch(page, web_base_url):
    install_api_mocks(page)
    held: dict = {}
    page.route("**/chat/turns", lambda route: held.__setitem__("turn_start_route", route))
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill(Q)
    page.locator("#send").click()
    _wait_until(lambda: "turn_start_route" in held, page=page,
                message="開始 POST（POST /chat/turns）が届かなかった")

    page.locator("#newbtn").click()
    expect(page.locator("#messages")).to_contain_text("ようこそ")
    expect(page.locator("#rt")).to_contain_text("待機中")

    held.pop("turn_start_route").fulfill(
        content_type="application/json",
        body=json.dumps({"turn_id": "turn-stale", "conversation_id": 999}))
    _settle(page)

    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(page.locator("#messages")).to_contain_text("ようこそ")


def test_v2_stop_pending_then_post_failure_corrects_trace_stop_reason(page, web_base_url):
    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_stop(route):
        held["stop_route"] = route
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in [
                V2_META,
                {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
                 "detail": "内容を把握しました", "status": "active"},
            ])
            stream_route.fulfill(status=200, headers=SSE, body=body)

    page.route("**/chat/turns/*/stream?**", lambda route: pending.__setitem__("route", route))
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill(Q)
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")
    page.locator("#send").click()
    _wait_until(lambda: "stop_route" in held, page=page, message="停止 POST が届かなかった")

    expect(page.locator(".ftrace-stopreason")).to_contain_text("停止操作")
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    expect(page.locator(".ftrace-stopreason")).to_contain_text("エラー")
    expect(page.locator("#rt")).to_contain_text("接続エラー。もう一度お試しください。")


def test_v2_history_replay_has_same_hierarchy_as_live(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(111)")

    turn = page.locator(".fturn").first
    expect(turn).to_contain_text("進め方を計画")
    lane = turn.locator(".fagent")
    expect(lane).to_have_count(1)
    expect(lane).to_contain_text("下調べ役")
    expect(lane).to_contain_text("ローカル: qwen2.5")
    expect(lane.locator(".fagent-status")).to_contain_text("完了")
    agg = lane.locator(".fagg")
    expect(agg).to_have_count(1)
    expect(agg.locator(".fagg-head")).to_contain_text("資料を検索（語句そのまま）×3")
    expect(turn.locator(".ftrace-stopreason")).to_contain_text("自然終了")
    _no_slug_leak(turn.inner_text())


_V1_FLAT_NODES = [
    {"type": "node", "id": "understand", "kind": "think", "label": "質問を理解",
     "detail": "内容を把握しました", "status": "done"},
    {"type": "node", "id": "tool-graph", "kind": "tool", "label": "関係グラフを照会",
     "detail": "「消費税率」", "status": "done"},
]


@pytest.mark.parametrize("with_marker", [True, False])
def test_v1_conversations_render_flat_without_lanes_or_summary(page, web_base_url, with_marker):
    marker = [{"type": "trace_meta", "trace_version": 1}] if with_marker else []
    _run(page, web_base_url, [*marker, *_V1_FLAT_NODES, _answer_event(IMPACT_ANSWER)])

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator("#flow .fstep")).to_have_count(2)
    expect(page.locator(".fagent")).to_have_count(0)
    expect(page.locator(".fagg")).to_have_count(0)
    expect(page.locator(".provider-summary")).to_have_count(0)
    expect(page.locator(".ftrace-stopreason")).to_have_count(0)


def _qa_answer(evidence, citations=None, v2=True):
    answer = {
        "lens": "qa", "headline": "確認しました。",
        "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
        "scope": {"world": "w1", "scope_paths": [], "source": "all"},
        "data": {
            "citations": [{"doc_id": DOC, "quote": "消費税率は10%", "span": [3, 3]}] if citations is None else citations,
            "evidence_packet": {"evidence": [evidence]},
        },
        "sources": [{"doc_id": DOC, "download_url": "/documents/download?world=w1&rel=x"}],
        "sources_verified": [DOC],
    }
    if v2:
        answer["trace_version"] = 2
    return answer


def _doc_evidence(method):
    return {"evidence_id": "ev-1", "source_type": "document", "source_path": DOC,
            "source_span": [3, 3], "verification_method": method, "used": True}


def _ask_badge(page, base, answer, v2=True):
    _run(page, base, [*([V2_META] if v2 else []), _answer_event(answer, [] if v2 else None)], "消費税率は?")
    expect(page.locator("#messages")).to_contain_text("確認しました。")
    return page.locator(".verif-badge")


def test_v1_answer_never_shows_verification_badge_even_with_evidence_packet(page, web_base_url):
    badge = _ask_badge(page, web_base_url, _qa_answer(_doc_evidence("span_verified"), v2=False), v2=False)
    expect(badge).to_have_count(0)


def test_v2_verification_badge_shown_on_grounded_source(page, web_base_url):
    badge = _ask_badge(page, web_base_url, _qa_answer(_doc_evidence("span_verified")))
    expect(badge).to_have_count(1)
    expect(badge).to_contain_text("機械検証済み（該当箇所一致）")


@pytest.mark.parametrize("method", ["future_unknown_method", "constructor"])
def test_v2_verification_badge_unknown_method_is_neutral_not_verified(page, web_base_url, method):
    badge = _ask_badge(page, web_base_url, _qa_answer(_doc_evidence(method)))
    expect(badge).to_have_count(1)
    expect(badge).to_contain_text("検証方法不明")
    expect(badge).not_to_contain_text("機械検証済み")
    expect(badge).to_have_class("verif-badge unknown")


def test_v2_verification_badge_matches_via_matched_doc_ids(page, web_base_url):
    evidence = {"evidence_id": "ev-1", "source_type": "document", "source_path": None,
                "verification_method": "list_docs_verified", "used": True,
                "matched_doc_ids": [DOC], "list_meta": {"count": 1, "prefix": "4期/02_設計"}}
    badge = _ask_badge(page, web_base_url, _qa_answer(evidence, citations=[]))
    expect(badge).to_have_count(1)
    expect(badge).to_contain_text("機械検証済み（一覧確認）")


def test_thinking_placeholder_ticks_while_waiting_for_first_event(page, web_base_url):
    install_api_mocks(page)
    page.route("**/chat/turns/*/stream?**", lambda route: None)
    page.goto(f"{web_base_url}/chat.html")
    page.clock.install()

    page.locator("#input").fill(Q)
    page.locator("#send").click()

    expect(page.locator(".thinking")).to_contain_text("回答を準備しています")
    page.clock.fast_forward(3000)
    expect(page.locator(".thinking")).to_contain_text("AI が考えています（3秒）")
    page.clock.fast_forward(4000)
    expect(page.locator(".thinking")).to_contain_text("AI が考えています（7秒）")


def test_thinking_ticker_stops_after_answer_arrives(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.clock.install()

    page.locator("#input").fill(Q)
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".thinking")).to_have_count(0)
    page.clock.fast_forward(10000)
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")


def test_v2_lane_labels_and_badges_stay_single_line_in_narrow_pane(page, web_base_url):
    page.set_viewport_size({"width": 1366, "height": 900})
    _run(page, web_base_url, [V2_META, *V2_LANE_TRACE, _answer_event(V2_LANE_ANSWER, V2_LANE_TRACE)])

    expect(page.locator(".fagent")).to_have_count(1)
    pane_box = page.locator(".pane.right").bounding_box()
    assert pane_box is not None
    right_edge = pane_box["x"] + pane_box["width"]

    ONE_LINE_MAX_PX = 50
    labels = page.locator(".fagent .flabel")
    for i in range(labels.count()):
        box = labels.nth(i).bounding_box()
        assert box is not None and box["height"] <= ONE_LINE_MAX_PX, (
            f".flabel[{i}] が複数行に折り返している（1文字ずつ縦積みの再発）: {box}")

    name_box = page.locator(".fagent-name").first.bounding_box()
    assert name_box is not None and name_box["height"] <= ONE_LINE_MAX_PX, (
        f".fagent-name が複数行に折り返している: {name_box}")

    badges = page.locator(".provider-badge")
    for i in range(badges.count()):
        box = badges.nth(i).bounding_box()
        assert box is not None and (box["x"] + box["width"]) <= right_edge + 1, (
            f".provider-badge[{i}] がペイン右端からはみ出している: {box} (right_edge={right_edge})")
