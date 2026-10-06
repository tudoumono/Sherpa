from __future__ import annotations

import json
import re
import time

import pytest

import mock_api
from mock_api import IMPACT_ANSWER, PLAN_ANSWER, PLAN_TRACE, install_api_mocks

expect = pytest.importorskip("playwright.sync_api").expect

ON = re.compile(r"\bon\b")
DOC = "4期/02_設計/01_基本設計/税計算仕様書.md"
Q_IMPACT = "消費税率を変えたい。影響は？"
SSE = {"Content-Type": "text/event-stream"}
CONN_ERR = "接続エラー。もう一度お試しください。"


def _open(page, base, path="/chat.html", **mocks):
    records = install_api_mocks(page, **mocks)
    page.goto(f"{base}{path}")
    return records


def _send(page, text, key=None):
    page.locator("#input").fill(text)
    if key:
        page.locator("#input").press(key)
    else:
        page.locator("#send").click()


def _ask(page, text=Q_IMPACT):
    _send(page, text)
    expect(page.locator("#rt")).to_contain_text("完了")


def _open_conv(page, n):
    page.evaluate(f"window.__sherpaChatTest.openConversation({n})")


def _answer_events(answer, conv=101):
    return [{"type": "answer", "conversation_id": conv, "message": {"answer": answer}}]


def _sse_body(events):
    return "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)


def _fulfill_json(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body, ensure_ascii=False))


def _wait_until(predicate, timeout_ms=5000, interval_ms=20, page=None, message="条件が満たされなかった"):
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        assert time.monotonic() < deadline, message
        page.wait_for_timeout(interval_ms)


def _settle(page):
    """保留 route の解放後、その Promise 継続が走り切るのを本物の fetch 往復で待つ。"""
    page.evaluate("async () => { try { await fetch('/chat/turns/running'); } catch (e) {} }")


def _wait_stream(page, pending, message="GET /chat/turns/*/stream が届かなかった"):
    _wait_until(lambda: "route" in pending, page=page, message=message)


def _wait_stops(page, stops, n, message="停止 POST が届かなかった"):
    _wait_until(lambda: len(stops) >= n, page=page, message=message)


def _hold_stop_flow(page, abort_on_stop=True):
    """stream GET と stop POST を保留する。abort_on_stop なら停止 POST 到着時に stream を切る（onerror 先着）。"""
    pending: dict = {}
    stops: list = []

    def on_stream(route):
        pending["route"] = route

    def on_stop(route):
        stops.append(route)
        stream_route = pending.pop("route", None) if abort_on_stop else None
        if stream_route is not None:
            stream_route.abort()

    page.route("**/chat/turns/*/stream?**", on_stream)
    page.route("**/chat/turns/*/stop", on_stop)
    return pending, stops


def test_chat_streams_answer_with_explicit_scope(page, web_base_url):
    records = _open(page, web_base_url)
    expect(page.locator("#messages")).to_contain_text("気になること")
    expect(page.locator("#scopesel")).to_be_visible()

    page.locator("#scopebtn").click()
    page.locator("#scopepanel [data-toggle='4期']").click()
    page.locator("#scopepanel [data-toggle='4期/02_設計']").click()
    page.locator("#scopepanel [data-scope='4期/02_設計/01_基本設計']").click()
    expect(page.locator("#scopelabel")).to_have_text("01_基本設計")

    _ask(page)
    expect(page.locator("#flow")).to_contain_text("グラフ検索")
    expect(page.locator("#flow")).to_contain_text("MCP graph_neighbors")
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator("#messages")).to_contain_text("TAXCALC")
    expect(page.locator("#messages")).to_contain_text("出典")
    body = records["turn_starts"][-1]
    assert body["knowledge"] is True
    assert body["personal"] is False
    assert body["scope_paths"] == ["4期/02_設計/01_基本設計"]
    assert records["turn_stream_urls"], "GET /chat/turns/{turn_id}/stream が呼ばれていない"

    rows = page.locator("#messages .ilist li")
    expect(rows).to_have_count(2)
    expect(rows.nth(0)).to_contain_text("TAXCALC")
    expect(rows.nth(0)).to_contain_text("解析: COBOL")
    expect(rows.nth(1)).to_contain_text("請求機能")
    expect(rows.nth(1)).not_to_contain_text("解析:")

    expect(page.locator("#flow .fchip")).to_contain_text("消費税率")
    expect(page.locator("#flow")).to_contain_text("再検索: 税率 改定")
    hist = page.locator("#flow .fhist")
    expect(hist).to_be_visible()
    expect(hist).to_contain_text("履歴 2")
    expect(hist).to_have_attribute("aria-expanded", "false")
    hist.click()
    expect(hist).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#flow .fhist-list")).to_contain_text("検索: 消費税率")
    hist.click()
    expect(hist).to_have_attribute("aria-expanded", "false")

    expect(page.locator(".created-files")).to_have_count(0)
    expect(page.locator("#flow")).not_to_contain_text("進め方を計画")
    expect(page.locator(".usage-sub-meta")).to_have_count(0)
    sources_text = page.locator(".sources").first.inner_text()
    assert "根拠（精読済み）" not in sources_text
    assert "参考（ヒットのみ）" not in sources_text

    with page.expect_download() as dl_info:
        page.locator("[data-dl]").first.click()
    assert dl_info.value.suggested_filename == "税計算仕様書.md"
    assert records["doc_downloads"], "/documents/download が呼ばれていない"

    page.locator("#inquiry-head").click()
    page.locator("#personaltoggle").click()
    _ask(page, "個人メモも見て影響を確認して")
    assert records["turn_starts"][-1]["personal"] is True


def test_impact_detail_explains_why_connected_in_plain_words(page, web_base_url):
    """影響の詳細を開くと「なぜつながっているか」が平文で出る（参照元の文書と行が複数・内部の値と確からしさの語は出ない）。"""
    _open(page, web_base_url)
    _ask(page)
    row = page.locator("#messages .ilist li").nth(0)
    row.locator(".top").click()
    why = row.locator(".why")
    expect(why).to_contain_text("なぜつながっているか")
    expect(why).to_contain_text("TAXCALC は TAX-RATE を COPY で取り込んでいます")
    expect(why).to_contain_text("参照元: 4期/03_開発/01_ソース/TAXCALC.cbl〔行 12〕 / 4期/03_開発/01_ソース/TAXCALC.cbl〔行 40〕 ほか 2 件")
    text = page.locator("#messages").inner_text()
    for word in ("確実", "要確認", "推定", "nearest_name", "via", "rule"):
        assert word not in text, word


def test_presumed_only_impact_uses_plain_words_without_grades(page, web_base_url):
    """構造の影響が無く資料から見つけた関連だけのとき、見出しと別枠に確からしさの語（確実・推定・要確認）が出ない。"""
    answer = {**IMPACT_ANSWER, "headline": "「税率」に構造的な依存は見つかりませんでしたが、資料から見つけた関連が 1件あります: TAXCALC など。",
              "data": {"items": [], "presumed": [{"category": "ソース", "name": "TAXCALC", "evidence": [{"doc": "d.md", "quote": "税率"}]}]}}
    _open(page, web_base_url, stream_events=_answer_events(answer))
    _ask(page)
    expect(page.locator("#messages")).to_contain_text("資料から見つけた関連")
    text = page.locator("#messages").inner_text()
    for word in ("確実", "要確認", "推定"):
        assert word not in text, word


def test_scope_panel_tree_toggle_and_filter(page, web_base_url):
    _open(page, web_base_url)
    page.locator("#scopebtn").click()

    expect(page.locator("#scopepanel [data-scope='4期']")).to_be_visible()
    expect(page.locator("#scopepanel [data-scope='4期/02_設計']")).to_have_count(0)
    expect(page.locator("#scopepanel [data-scope='4期/02_設計/01_基本設計']")).to_have_count(0)

    page.locator("#scopepanel [data-toggle='4期']").click()
    expect(page.locator("#scopepanel [data-scope='4期/02_設計']")).to_be_visible()
    expect(page.locator("#scopepanel [data-scope='4期/03_開発']")).to_be_visible()
    expect(page.locator("#scopepanel [data-scope='4期/02_設計/01_基本設計']")).to_have_count(0)
    expect(page.locator("#scopepanel [data-scope='4期']")).not_to_have_class(ON)
    expect(page.locator("#scopelabel")).to_have_text("全体")

    page.locator("#scopefilter").fill("ソース")
    target = page.locator("#scopepanel [data-scope='4期/03_開発/01_ソース']")
    expect(target).to_be_visible()
    expect(target).to_contain_text("03_開発")
    expect(page.locator("#scopepanel [data-scope='4期/02_設計']")).to_have_count(0)
    target.click()
    expect(page.locator("#scopelabel")).to_have_text("01_ソース")

    page.locator("#scopefilter").fill("")
    expect(page.locator("#scopepanel [data-scope='4期']")).to_be_visible()
    expect(target).to_be_visible()


def test_scope_panel_restores_deep_selection_visible_on_reopen(page, web_base_url):
    _open(page, web_base_url, "/chat.html?conv=117")
    expect(page.locator("#messages")).to_contain_text("TAXCALC")
    expect(page.locator("#scopelabel")).to_have_text("01_ソース")

    page.locator("#scopebtn").click()
    target = page.locator("#scopepanel [data-scope='4期/03_開発/01_ソース']")
    expect(target).to_be_visible()
    expect(target).to_have_class(ON)


_AUTHOR_ANSWER = {
    "lens": "author",
    "headline": "消費税率の一覧をExcelにまとめました。",
    "route": {"path": ["文書を検索", "資料を作成"]},
    "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
    "data": {"citations": [{"doc_id": DOC, "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [{"doc_id": DOC, "download_url": "/documents/download?world=w1&rel=x"}],
    "created_files": [{"name": "消費税率一覧.xlsx", "download_url": "/workspace/files/501/download"}],
}


def test_created_files_card_displays_with_working_download_link(page, web_base_url):
    records = _open(page, web_base_url, stream_events=[
        {"type": "node", "id": "codex", "kind": "think", "status": "done",
         "label": "Codex が調べる", "detail": "調べて回答をまとめました"},
        *_answer_events(_AUTHOR_ANSWER),
    ])
    _send(page, "消費税率の一覧をExcelにまとめて")

    expect(page.locator("#messages")).to_contain_text("資料を作成")
    expect(page.locator("#messages")).to_contain_text("作成したファイル")
    link = page.locator(".created-files a[data-dl]").first
    expect(link).to_have_text("消費税率一覧.xlsx")
    assert link.get_attribute("href") == "/workspace/files/501/download"
    expect(page.locator(".created-files-link")).to_have_text("マイワークスペースで開く")

    with page.expect_download() as dl_info:
        link.click()
    assert dl_info.value.suggested_filename == "消費税率一覧.xlsx", \
        f"保存名がカードのファイル名と一致しない: {dl_info.value.suggested_filename}"
    assert records["workspace_downloads"] == ["/workspace/files/501/download"], \
        "/workspace/files/{id}/download が呼ばれていない"

    _open_conv(page, 106)
    expect(page.locator("#messages")).to_contain_text("作成したファイル")
    history_link = page.locator(".created-files a[data-dl]").last
    expect(history_link).to_have_text("消費税率一覧.xlsx")
    assert history_link.get_attribute("href") == "/workspace/files/501/download"


def test_chat_stop_button_stops_streaming_and_restores_ui(page, web_base_url):
    install_api_mocks(page)
    pending: dict = {}

    def handle_stop(route):
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.fulfill(status=200, headers=SSE, body=_sse_body([
                {"type": "node", "id": "understand", "kind": "think", "status": "active",
                 "label": "質問を理解", "detail": "確認しています"},
                {"type": "stopped", "conversation_id": 101},
            ]))
        _fulfill_json(route, {"ok": True})

    page.route("**/chat/turns/*/stream?**", lambda route: pending.__setitem__("route", route))
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    _send(page, Q_IMPACT)
    send_btn = page.locator("#send")
    expect(send_btn).to_have_class(re.compile(r"\bstopping\b"))
    expect(send_btn).to_have_text("■")

    send_btn.click()
    expect(page.locator(".stopped-note")).to_contain_text("停止しました")
    expect(send_btn).not_to_have_class(re.compile(r"\bstopping\b"))
    expect(send_btn).to_have_text("↑")
    expect(page.locator("#rt")).to_contain_text("停止しました")


def test_chat_enter_key_double_submit_during_pending_start_is_rejected(page, web_base_url):
    install_api_mocks(page)
    starts: list = []
    page.route("**/chat/turns", lambda route: starts.append(route))
    page.goto(f"{web_base_url}/chat.html")

    _send(page, Q_IMPACT, key="Enter")
    _wait_until(lambda: len(starts) >= 1, page=page, message="1回目の開始 POST が届かなかった")

    _send(page, "2回目の質問（届いてはいけない）", key="Enter")
    page.wait_for_timeout(150)
    assert len(starts) == 1, f"開始 POST が複数回飛んでいる（二重送信ガードが効いていない）: {len(starts)}"

    _fulfill_json(starts[0], {"turn_id": "turn-A", "conversation_id": 101})
    expect(page.locator("#rt")).to_contain_text("リアルタイム")


def test_chat_send_start_post_timeout_resets_sending_and_allows_resend(page, web_base_url):
    install_api_mocks(page)
    pending: dict = {}
    page.route("**/chat/turns", lambda route: pending.__setitem__("route", route))
    page.goto(f"{web_base_url}/chat.html")
    page.clock.install()

    _send(page, Q_IMPACT, key="Enter")
    _wait_until(lambda: "route" in pending, page=page, message="開始 POST が届かなかった")

    page.clock.fast_forward(31000)
    expect(page.locator(".thinking")).to_contain_text("タイムアウト")
    expect(page.locator("#send")).to_be_enabled()

    pending.clear()
    _send(page, "再送の質問", key="Enter")
    _wait_until(lambda: "route" in pending, page=page,
                message="締切超過後の再送 POST が送れていない（S.sending が解除されていない）")


def test_chat_stale_start_post_completion_does_not_reset_sending_for_newer_generation(page, web_base_url):
    install_api_mocks(page)
    starts: list = []
    b_stream_hits: list = []

    def handle_b_stream(route):
        b_stream_hits.append(route.request)
        route.fallback()

    page.route("**/chat/turns", lambda route: starts.append(route))
    page.route("**/chat/turns/turn-B/stream?**", handle_b_stream)
    page.goto(f"{web_base_url}/chat.html")

    _send(page, "Aの質問", key="Enter")
    _wait_until(lambda: len(starts) >= 1, page=page, message="Aの開始POSTが届かなかった")

    page.locator("#newbtn").click()
    expect(page.locator("#messages")).to_contain_text("ようこそ")
    _send(page, "Bの質問", key="Enter")
    _wait_until(lambda: len(starts) >= 2, page=page,
                message="新しい画面からの開始 POST が送れていない（S.sending が解除されていない）")

    _fulfill_json(starts[0], {"turn_id": "turn-A", "conversation_id": 101})
    page.wait_for_timeout(150)

    _send(page, "Cの質問（届いてはいけない）", key="Enter")
    page.wait_for_timeout(150)
    assert len(starts) == 2, f"Bの応答待ち中にCが送信されている（S.sending が誤って解除された）: {len(starts)}"

    _fulfill_json(starts[1], {"turn_id": "turn-B", "conversation_id": 102})
    _wait_until(lambda: len(b_stream_hits) >= 1, page=page,
                message="Bのターン（turn-B）へ購読（GET stream）されていない＝孤児化している")


def test_chat_stop_before_first_response_shows_stopped_not_connection_error(page, web_base_url):
    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_stop(route):
        held["stop_route"] = route
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.abort()

    page.route("**/chat/turns/*/stream?**", lambda route: pending.__setitem__("route", route))
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")
    send_btn = page.locator("#send")

    _send(page, Q_IMPACT)
    _wait_stream(page, pending)
    expect(send_btn).to_have_text("■")
    send_btn.click()

    expect(page.locator(".thinking")).to_contain_text("停止しました")
    _fulfill_json(held.pop("stop_route"), {"ok": True})
    _settle(page)

    expect(page.locator(".thinking")).to_contain_text("停止しました")
    expect(page.locator(".thinking")).not_to_contain_text("接続エラー")
    expect(page.locator("#rt")).to_contain_text("停止しました")
    expect(send_btn).to_have_text("↑")

    _send(page, "2件目の質問です。")
    _wait_stream(page, pending, "2件目の GET /chat/turns/*/stream が届かなかった")
    pending.pop("route").abort()

    thinking2 = page.locator(".thinking").last
    expect(thinking2).to_contain_text("接続エラー")
    expect(thinking2).not_to_contain_text("停止しました")
    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(send_btn).to_have_text("↑")


def test_chat_stop_failure_after_new_chat_does_not_clobber_new_screen(page, web_base_url):
    install_api_mocks(page)
    pending, stops = _hold_stop_flow(page)
    page.goto(f"{web_base_url}/chat.html")

    _send(page, Q_IMPACT)
    _wait_stream(page, pending)
    page.locator("#send").click()
    _wait_stops(page, stops, 1)
    expect(page.locator(".thinking")).to_contain_text("停止しました")

    page.locator("#newbtn").click()
    expect(page.locator("#messages")).to_contain_text("ようこそ")
    expect(page.locator("#rt")).to_contain_text("待機中")

    _fulfill_json(stops[0], {"ok": False})
    _settle(page)

    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(page.locator("#rt")).not_to_contain_text("接続エラー")
    expect(page.locator(".thinking")).to_have_count(0)


def test_chat_stop_correction_still_works_when_open_conversation_fetch_fails(page, web_base_url):
    install_api_mocks(page)
    pending, stops = _hold_stop_flow(page)
    page.route("**/conversations/999", lambda route: _fulfill_json(route, {"detail": "boom"}, status=500))
    page.goto(f"{web_base_url}/chat.html")

    _send(page, Q_IMPACT)
    _wait_stream(page, pending)
    page.locator("#send").click()
    _wait_stops(page, stops, 1)
    expect(page.locator(".thinking")).to_contain_text("停止しました")

    page.evaluate("(id) => window.__sherpaChatTest.openConversation(id).catch(() => {})", 999)
    expect(page.locator(".thinking")).to_contain_text("停止しました")

    _fulfill_json(stops[0], {"ok": False})
    _settle(page)

    expect(page.locator(".thinking")).to_contain_text(CONN_ERR)
    expect(page.locator("#rt")).to_contain_text(CONN_ERR)


def test_chat_stop_failure_after_next_turn_started_does_not_clobber_next_turn_ui(page, web_base_url):
    install_api_mocks(page)
    pending, stops = _hold_stop_flow(page)
    held: dict = {}
    starts_seen = {"n": 0}

    def handle_turn_start(route):
        starts_seen["n"] += 1
        if starts_seen["n"] == 1:
            route.fallback()
            return
        held["turn_start_route"] = route

    page.route("**/chat/turns", handle_turn_start)
    page.goto(f"{web_base_url}/chat.html")
    send_btn = page.locator("#send")

    _send(page, Q_IMPACT)
    _wait_stream(page, pending, "1件目の GET /chat/turns/*/stream が届かなかった")
    send_btn.click()
    expect(page.locator(".thinking")).to_contain_text("停止しました")
    expect(send_btn).to_have_text("↑")

    _send(page, "2件目の質問です。")
    _wait_until(lambda: "turn_start_route" in held, page=page, message="2件目の開始 POST が届かなかった")
    expect(send_btn).to_have_text("■")
    expect(page.locator("#messages")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#rt")).to_contain_text("リアルタイム")
    thinking2 = page.locator(".thinking").last

    _fulfill_json(stops[0], {"ok": False})
    _settle(page)

    expect(send_btn).to_have_text("■")
    expect(page.locator("#messages")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#rt")).to_contain_text("リアルタイム")
    expect(thinking2).not_to_contain_text("接続エラー")
    expect(thinking2).not_to_contain_text("停止しました")

    _fulfill_json(held.pop("turn_start_route"), {"turn_id": "turn-101", "conversation_id": 101})
    _wait_stream(page, pending, "2件目の GET /chat/turns/*/stream が届かなかった")
    pending.pop("route").abort()

    expect(thinking2).to_contain_text("接続エラー")
    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(send_btn).to_have_text("↑")


def test_chat_stop_delayed_failure_from_older_turn_does_not_erase_newer_turns_pending_correction(
    page, web_base_url,
):
    install_api_mocks(page)
    pending, stops = _hold_stop_flow(page)
    page.goto(f"{web_base_url}/chat.html")
    send_btn = page.locator("#send")

    _send(page, "1件目の質問です。")
    _wait_stream(page, pending, "1件目の GET /chat/turns/*/stream が届かなかった")
    send_btn.click()
    _wait_stops(page, stops, 1, "1件目の停止 POST が届かなかった")
    expect(page.locator(".thinking")).to_contain_text("停止しました")
    expect(send_btn).to_have_text("↑")

    _send(page, "2件目の質問です。")
    _wait_stream(page, pending, "2件目の GET /chat/turns/*/stream が届かなかった")
    send_btn.click()
    _wait_stops(page, stops, 2, "2件目の停止 POST が届かなかった")
    thinking2 = page.locator(".thinking").last
    expect(thinking2).to_contain_text("停止しました")
    expect(page.locator("#rt")).to_contain_text("停止しました")

    _fulfill_json(stops[0], {"ok": False})
    _settle(page)
    expect(thinking2).to_contain_text("停止しました")
    expect(page.locator("#rt")).to_contain_text("停止しました")

    _fulfill_json(stops[1], {"ok": False})
    _settle(page)
    expect(thinking2).to_contain_text(CONN_ERR)
    expect(page.locator("#rt")).to_contain_text(CONN_ERR)


def test_chat_stop_failure_after_partial_answer_removed_thinking_still_corrects_rt(page, web_base_url):
    install_api_mocks(page)
    pending, stops = _hold_stop_flow(page, abort_on_stop=False)
    page.goto(f"{web_base_url}/chat.html")

    _send(page, Q_IMPACT)
    _wait_stream(page, pending)
    page.locator("#send").click()
    _wait_stops(page, stops, 1)

    pending.pop("route").fulfill(status=200, headers=SSE, body=_sse_body([{"type": "answer_delta", "text": "回答の一部です"}]))
    expect(page.locator(".thinking")).to_have_count(0)
    expect(page.locator("#rt")).to_contain_text("停止しました")

    _fulfill_json(stops[0], {"ok": False})
    _settle(page)
    expect(page.locator("#rt")).to_contain_text(CONN_ERR)
    expect(page.locator(".thinking")).to_have_count(0)


def test_chat_stop_failure_after_answer_already_landed_is_ignored(page, web_base_url):
    install_api_mocks(page)
    pending, stops = _hold_stop_flow(page, abort_on_stop=False)
    page.goto(f"{web_base_url}/chat.html")
    send_btn = page.locator("#send")

    _send(page, Q_IMPACT)
    _wait_stream(page, pending)
    send_btn.click()
    _wait_stops(page, stops, 1)

    pending.pop("route").fulfill(status=200, headers=SSE, body=_sse_body(_answer_events(IMPACT_ANSWER)))
    expect(page.locator("#rt")).to_contain_text("完了")
    expect(send_btn).to_have_text("↑")

    _fulfill_json(stops[0], {"ok": False})
    _settle(page)
    expect(page.locator("#rt")).to_contain_text("完了")
    expect(page.locator("#rt")).not_to_contain_text("待機中")
    expect(page.locator("#rt")).not_to_contain_text("接続エラー")


def test_chat_qa_citations_collapsed_by_default_and_toggle(page, web_base_url):
    qa_answer = {
        "lens": "qa", "headline": "端数処理は切り捨てです。",
        "route": {"path": ["資料"]},
        "summary": {"total": 2},
        "data": {"citations": [
            {"doc_id": DOC, "span": [10, 12], "quote": "端数は切り捨てとする。"},
            {"doc_id": DOC, "span": [20, 21], "quote": "1円未満切り捨て。"},
        ]},
        "sources": [{"doc_id": DOC, "download_url": "/documents/download?world=w1&rel=x"}],
    }
    _open(page, web_base_url, stream_events=_answer_events(qa_answer))
    _send(page, "端数処理は？")

    cites_h = page.locator(".cites-h")
    cites_body = page.locator(".cites-body")
    expect(cites_h).to_contain_text("該当箇所 (2)")
    expect(cites_body).to_be_hidden()

    cites_h.click()
    expect(cites_body).to_be_visible()
    expect(cites_body).to_contain_text("端数は切り捨てとする。")
    expect(cites_h).to_have_attribute("aria-expanded", "true")

    cites_h.click()
    expect(cites_body).to_be_hidden()
    expect(cites_h).to_have_attribute("aria-expanded", "false")


def test_chat_prefills_question_from_graph_bridge(page, web_base_url):
    install_api_mocks(page)
    page.add_init_script("localStorage.setItem('sherpa-ask', '消費税率を変えたい。影響は？')")
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#input")).to_have_value(Q_IMPACT)
    assert page.evaluate("localStorage.getItem('sherpa-ask')") is None


def test_chat_row_click_opens_conversation_with_turn_stack_and_local_time(page, web_base_url):
    _open(page, web_base_url)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))

    row = page.locator("[data-open='101']")
    expect(page.locator("#convlist")).to_contain_text("消費税率の相談")
    expect(row).to_contain_text("2026-07-01 18:00")
    expect(row).not_to_contain_text("09:00")
    row.click()

    expect(page.locator("#conv-title")).to_have_text("消費税率の相談")
    assert dialogs == [], f"行クリックで改名ダイアログが誤って開いた（クリック誤爆の再発）: {dialogs}"
    expect(page).to_have_url(re.compile(r"[?&]conv=101(&|$)"))

    turns = page.locator(".fturn")
    expect(turns).to_have_count(3)
    expect(turns.nth(0)).to_contain_text("消費税率を変えたい")
    expect(turns.nth(0)).to_contain_text("（記録なし）")
    expect(turns.nth(1)).to_contain_text("対象範囲はどこまでですか")
    expect(turns.nth(2)).to_contain_text("影響はどこまで及びますか")
    expect(page.locator("#rt")).to_contain_text("過去の記録")
    expect(turns.nth(2)).to_have_js_property("open", True)
    expect(turns.nth(1)).to_have_js_property("open", False)

    trace_btns = page.locator("[data-showtrace]")
    expect(trace_btns).to_have_count(2)
    trace_btns.first.click()
    expect(page.locator("#fturn-1")).to_have_js_property("open", True)

    page.locator("#newbtn").click()
    expect(page).not_to_have_url(re.compile(r"[?&]conv="))


def test_chat_history_conversation_variants(page, web_base_url):
    _open(page, web_base_url)
    turns = page.locator(".fturn")

    _open_conv(page, 102)
    expect(turns).to_have_count(2)
    expect(turns.nth(0)).to_contain_text("それ、直して")
    expect(turns.nth(0)).to_contain_text("（記録なし）")
    expect(turns.nth(1)).to_contain_text("消費税率の変更点を直して")
    expect(turns.nth(1)).to_have_js_property("open", True)

    _open_conv(page, 103)
    expect(turns).to_have_count(1)
    expect(turns.nth(0)).to_contain_text("こんにちは")
    expect(turns.nth(0)).to_contain_text("（記録なし）")
    expect(page.locator("#flow")).not_to_contain_text("質問すると、考えた流れがここに流れます")

    _open_conv(page, 104)
    expect(turns).to_have_count(0)
    expect(page.locator("#flow")).to_contain_text("質問すると、考えた流れがここに流れます")

    _open_conv(page, 107)
    cards = page.locator(".askcard")
    expect(cards).to_have_count(2)
    answered = cards.nth(0)
    expect(answered).to_have_class(re.compile(r"\banswered\b"))
    expect(answered).to_contain_text("回答済み")
    expect(answered).to_contain_text("影響範囲")
    expect(answered.locator("[data-ask-submit]")).to_be_disabled()
    assert answered.locator("[data-qopt][data-label='影響範囲']").is_checked()
    operable = cards.nth(1)
    expect(operable).not_to_have_class(re.compile(r"\banswered\b"))
    expect(operable.locator("[data-ask-submit]")).to_be_enabled()
    expect(operable).not_to_contain_text("回答済み")

    _open_conv(page, 108)
    expect(page.locator(".askcard")).to_have_count(0)
    expect(page.locator("#messages")).to_contain_text("（確認のやり取り）")


def test_chat_history_load_renders_markdown_and_escapes_xss(page, web_base_url):
    _open(page, web_base_url)
    page.evaluate("window.__xss = 0")
    _open_conv(page, 105)

    headline = page.locator(".headline").last
    expect(headline.locator("strong")).to_have_count(2)
    expect(headline.locator("code", has_text="list_docs")).to_have_count(1)
    expect(headline.locator("ul li")).to_have_count(2)
    expect(headline.locator("pre.md-code")).to_have_count(2)
    expect(headline.locator("pre.md-code").first).to_contain_text("path_prefix=4期更改")

    expect(headline).to_contain_text("<img src=x onerror=alert(1)>")
    expect(headline).to_contain_text("[link](javascript:alert(1))")
    expect(headline.locator("pre.md-code").last).to_contain_text("</code></pre><img src=x onerror=window.__xss=1>")
    expect(headline).to_contain_text("&lt;img src=x onerror=window.__xss=2&gt;")
    expect(headline).to_contain_text("&amp;amp;")
    expect(headline.locator("img")).to_have_count(0)
    expect(headline.locator("a")).to_have_count(0)
    assert page.evaluate("window.__xss") == 0, "XSS ペイロードが実行された（onerror 発火）"


def test_chat_answer_renders_markdown_after_stream_completes(page, web_base_url):
    md_answer = {**IMPACT_ANSWER, "headline": "**結論**: `list_docs` で確認した結果、**6件**でした。"}
    _open(page, web_base_url, stream_events=_answer_events(md_answer))
    _ask(page, "4期更改資料はどのくらいある？")

    headline = page.locator(".headline").last
    expect(headline.locator("strong")).to_have_count(2)
    expect(headline.locator("code")).to_have_count(1)
    expect(headline).to_contain_text("list_docs")
    expect(headline).not_to_contain_text("**")


def test_chat_copy_button_preserves_raw_markdown_not_rendered_html(page, web_base_url):
    try:
        page.context.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        pytest.skip("このブラウザ/環境ではクリップボード権限を付与できない")
    _open(page, web_base_url)
    _open_conv(page, 105)

    page.locator(".headline").last.locator("xpath=ancestor::div[contains(@class,'a-body')]")\
        .locator("[data-copy]").click()
    expect(page.locator("#toast")).to_contain_text("コピーしました")
    copied = page.evaluate("navigator.clipboard.readText()")
    assert "**結論**" in copied and "`list_docs`" in copied and "**6件**" in copied, copied


def test_chat_unopenable_conv_param_is_cleared(page, web_base_url):
    install_api_mocks(page)
    page.route("**/conversations/999", lambda r: _fulfill_json(r, {"detail": "会話が見つかりません"}, status=404))
    page.goto(f"{web_base_url}/chat.html?conv=999")

    expect(page).not_to_have_url(re.compile(r"[?&]conv="))
    expect(page.locator("#convlist")).to_contain_text("消費税率の相談")


def test_chat_conversation_row_click_opens_at_minimum_sidebar_width(page, web_base_url):
    install_api_mocks(page)
    page.add_init_script("localStorage.setItem('sherpa-cols', JSON.stringify({L:200,R:300}))")
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#convlist")).to_contain_text("消費税率の相談")
    row = page.locator("[data-open='101']")
    box = row.bounding_box()
    assert box["width"] < 200, f"サイドバー最小幅の再現に失敗（行幅 {box['width']}px）"
    row.hover()

    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    hit = page.evaluate(f"document.elementFromPoint({cx},{cy})?.className || ''")
    assert "cact" not in hit, f"狭幅でも行中心がボタンに当たっている（誤爆再発）: {hit!r}"

    row.click()
    expect(page.locator("#conv-title")).to_have_text("消費税率の相談")
    assert dialogs == [], f"狭幅サイドバーで行クリックが改名ダイアログを誤って開いた: {dialogs}"


def test_chat_share_dialog_create_and_extend(page, web_base_url):
    records = _open(page, web_base_url)
    _ask(page)

    page.locator("#sharebtn").click()
    expect(page.locator("#share-overlay")).to_be_visible()
    expect(page.locator("#share-days")).to_have_value("30")
    expect(page.locator("#share-days option")).not_to_contain_text(["無期限"])
    page.locator("[data-share-extend='77']").click()
    expect(page.locator("#toast")).to_contain_text("30日後")
    assert records["share_extend"] == [("/conversation-shares/77/extend", {"days": 30})]

    page.locator("#share-invitees").fill("sato tanaka")
    page.locator("#share-days").select_option("3")
    page.locator("#share-submit").click()
    expect(page.locator("#share-result")).to_be_visible()
    expect(page.locator("#share-url-val")).to_contain_text("/share/conversations/share-token-101")
    assert records["share_create"][-1]["invitee_user_ids"] == ["sato", "tanaka"]
    assert "expires_at" in records["share_create"][-1]


def test_chat_share_dialog_autocomplete_chips_and_free_text_combine(page, web_base_url):
    records = _open(page, web_base_url)
    _ask(page)

    page.locator("#sharebtn").click()
    expect(page.locator("#share-overlay")).to_be_visible()
    invitees = page.locator("#share-invitees")
    suggest = page.locator("#share-invitee-suggest")
    chips = page.locator("#share-invitee-chips .invitee-chip")

    invitees.fill("tana")
    expect(suggest).to_be_visible()
    expect(suggest).to_contain_text("田中 花子")
    assert records["users_suggest"][-1] == "tana"
    page.locator("[data-pick-invitee]", has_text="田中 花子").first.click()
    expect(suggest).to_be_hidden()
    expect(chips).to_have_count(1)
    expect(chips.first).to_contain_text("田中 花子")
    expect(invitees).to_have_value("")

    invitees.fill("yamada")
    expect(suggest).to_contain_text("山田 太郎")
    invitees.press("ArrowDown")
    invitees.press("Enter")
    expect(chips).to_have_count(2)
    expect(chips.nth(1)).to_contain_text("山田 太郎")
    expect(suggest).to_be_hidden()
    chips.nth(1).locator("button").click()
    expect(chips).to_have_count(1)

    invitees.fill("freeuser1, freeuser2")
    page.locator("#share-submit").click()
    expect(page.locator("#share-result")).to_be_visible()
    assert set(records["share_create"][-1]["invitee_user_ids"]) == {"tanaka", "freeuser1", "freeuser2"}


def test_chat_uploads_file_to_personal_workspace(page, web_base_url, tmp_path):
    records = _open(page, web_base_url)
    upload = tmp_path / "chat-note.md"
    upload.write_text("個人メモです\n", encoding="utf-8")

    page.locator("#chat-file-input").set_input_files(str(upload))
    expect(page.locator("#chat-upload-status")).to_contain_text("chat-note.md")
    expect(page.locator("#chat-upload-status")).to_contain_text("個人ワークスペース")
    assert records["workspace_uploads"][-1]["filename"] == "chat-note.md"


def test_world_selector_persists_last_choice(page, web_base_url):
    install_api_mocks(page)
    two = {"worlds": ["w1", "w2"], "labels": {"w1": "販売管理", "w2": "在庫管理"}}
    page.route("**/world-options", lambda route: _fulfill_json(route, two))
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator(".verselect")).to_be_visible()
    expect(page.locator("#version")).to_have_value("w1")
    page.locator("#version").select_option("w2")

    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#version")).to_have_value("w2")

    page.route("**/world-options", lambda route: _fulfill_json(route, {"worlds": ["w1"], "labels": {"w1": "販売管理"}}))
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#version")).to_have_value("w1")


def test_world_selector_conversation_beats_saved_choice(page, web_base_url):
    install_api_mocks(page)
    two = {"worlds": ["w1", "w2"], "labels": {"w1": "販売管理", "w2": "在庫管理"}}
    page.route("**/world-options", lambda route: _fulfill_json(route, two))
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("localStorage.setItem('sherpa-world', 'w2')")

    page.goto(f"{web_base_url}/chat.html?conv=102")
    expect(page.locator("#messages")).to_contain_text("TAXCALC")
    expect(page.locator("#version")).to_have_value("w1")


def test_export_and_font_menus(page, web_base_url):
    _open(page, web_base_url)

    expect(page.locator("#exportmenu")).to_be_hidden()
    page.locator("#exportbtn").click()
    expect(page.locator("#exportmenu")).to_be_visible()
    items = page.locator("#exportmenu [data-exp]")
    expect(items).to_have_count(4)
    expect(items).to_have_text(["Markdown", "テキスト", "JSON", "PDF（印刷）"])

    expect(page.locator("#fontmenu")).to_be_hidden()
    page.locator("#fontbtn").click()
    expect(page.locator("#fontmenu")).to_be_visible()
    page.locator("#fontmenu [data-fs='大']").click()
    expect(page.locator("#fontmenu")).to_be_hidden()
    expect(page.locator("#messages")).to_have_css("font-size", "18px")


_CODEX_SETTINGS = {**mock_api.SETTINGS_RESP, "agent": "codex", "construct_id": "codex_openai"}
_UNLOCKED_KB_SETTINGS = {**mock_api.SETTINGS_RESP, "agent": "ollama", "construct_id": "ollama"}


def _open_brain_menu(page):
    page.locator("#brainbadge").click()
    expect(page.locator("#brainmenu")).to_be_visible()


def _pick_brain(page, exec_id):
    _open_brain_menu(page)
    page.locator(f"#brainmenu [data-exec='{exec_id}']").click()


def test_brain_menu_lists_three_constructs_and_switching_puts_settings(page, web_base_url):
    records = _open(page, web_base_url)

    expect(page.locator("#brainmenu")).to_be_hidden()
    _open_brain_menu(page)
    expect(page.locator("#brainmenu [data-exec]")).to_have_count(3)
    expect(page.locator("#brainmenu [data-exec='simple']")).to_contain_text("簡易")
    expect(page.locator(".bm-model")).to_contain_text("簡易回答に使う AI")
    expect(page.locator("#bm-modeltest")).to_have_count(0)

    page.locator("#brainmenu [data-exec='codex_ollama']").click()
    assert records["settings_put"][-1]["agent"] == "codex"
    assert records["settings_put"][-1]["codex_model_provider"] == "ollama"
    expect(page.locator("#bm-modelinput")).to_have_count(0)
    expect(page.locator("#bm-modeltest")).to_have_count(0)
    expect(page.locator(".bm-model")).to_contain_text("管理画面")


def test_simple_mode_hides_inquiry_rows_and_omits_them_from_send(page, web_base_url):
    records = _open(page, web_base_url)

    for sel in ("#lens-row", "#depth-row", "#tools-details", "#websearchtoggle"):
        expect(page.locator(sel)).to_be_hidden()
    expect(page.locator("#simple-note")).to_be_visible()
    expect(page.locator("#simple-note")).to_contain_text("Codex 調査へ")
    expect(page.locator("#kbtoggle")).to_have_attribute("aria-disabled", "true")
    expect(page.locator("#kbtoggle")).to_have_attribute("aria-pressed", "true")

    _pick_brain(page, "codex_openai")
    for sel in ("#lens-row", "#depth-row", "#tools-details"):
        expect(page.locator(sel)).to_be_visible()
    expect(page.locator("#simple-note")).to_be_hidden()

    page.locator("#depth-seg [data-depth='deep']").click()
    page.locator("#lens-seg [data-lens='qa']").click()
    _pick_brain(page, "simple")
    expect(page.locator("#depth-row")).to_be_hidden()
    expect(page.locator("#simple-note")).to_be_visible()

    _ask(page, "消費税率の定義を教えて")
    body = records["turn_starts"][-1]
    assert "depth_profile" not in body and "lens" not in body and "tools" not in body

    if page.locator("#brainmenu").is_hidden():
        _open_brain_menu(page)
    page.locator("#brainmenu [data-exec='codex_openai']").click()
    _ask(page, "もう一度")
    body = records["turn_starts"][-1]
    assert body.get("depth_profile") == "deep" and body.get("lens") == "qa"


def test_chat_html_static_assets_load_without_failure(page, web_base_url):
    from urllib.parse import urlparse

    install_api_mocks(page)
    origin_host = urlparse(web_base_url).hostname
    failures = []

    def _on_response(resp):
        if urlparse(resp.url).hostname != origin_host or urlparse(resp.url).path == "/favicon.ico":
            return
        if resp.status >= 400:
            failures.append(f"status {resp.status}: {resp.url}")

    page.on("requestfailed", lambda req: failures.append(f"requestfailed: {req.url} ({req.failure})"))
    page.on("response", _on_response)
    page.goto(f"{web_base_url}/chat.html")
    page.wait_for_load_state("networkidle")
    assert not failures, "\n".join(failures)
    assert page.evaluate("!!window.__sherpaChatTest"), (
        "window.__sherpaChatTest が無い＝chat.js module グラフの評価が失敗している"
        "（import 解決失敗・構文エラー等）"
    )


def test_chat_sub_planner_plan_and_usage_subs_render_live_and_from_history(page, web_base_url):
    _open(page, web_base_url,
          stream_events=[*PLAN_TRACE, *_answer_events(PLAN_ANSWER)])
    _send(page, Q_IMPACT)

    expect(page.locator("#flow")).to_contain_text("進め方を計画")
    expect(page.locator("#flow")).to_contain_text("資料を検索（語句そのまま）")
    expect(page.locator("#flow")).to_contain_text("関係グラフを照会")
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")

    sub_meta = page.locator(".usage-sub-meta")
    expect(sub_meta).to_have_count(1)
    expect(sub_meta.locator("summary")).to_contain_text("下調べの使用量（2件）")
    expect(sub_meta.locator(".usage-detail")).to_be_hidden()
    sub_meta.locator("summary").click()
    expect(sub_meta.locator(".usage-detail")).to_be_visible()
    expect(sub_meta).to_contain_text("researcher: 入力 1,234 / 出力 567 トークン")
    expect(sub_meta).to_contain_text("reviewer: 入力 89 / 出力 45 トークン")

    _open_conv(page, 109)
    turns = page.locator(".fturn")
    expect(turns).to_have_count(1)
    expect(turns.first).to_have_js_property("open", True)
    for label in ("進め方を計画", "意図を特定", "資料を検索（語句そのまま）", "関係グラフを照会"):
        expect(turns.first).to_contain_text(label)
    expect(sub_meta).to_have_count(1)
    sub_meta.locator("summary").click()
    expect(sub_meta).to_contain_text("researcher: 入力 1,234 / 出力 567 トークン")
    expect(sub_meta).to_contain_text("reviewer: 入力 89 / 出力 45 トークン")


def test_codex_construct_locks_knowledge_toggle_on(page, web_base_url):
    records = install_api_mocks(page)
    page.route("**/config", lambda route: _fulfill_json(route, {"agent": "codex", "label": "Codex", "model": "gpt-5.5"}))
    page.goto(f"{web_base_url}/chat.html")

    kb = page.locator("#kbtoggle")
    expect(kb).to_have_attribute("aria-pressed", "true")
    expect(kb).to_have_attribute("aria-disabled", "true")
    expect(kb).to_contain_text("オン")

    kb.click(force=True)
    expect(kb).to_have_attribute("aria-pressed", "true")
    expect(kb).to_contain_text("オン")

    _send(page, "税率の影響は？")
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    assert records["turn_starts"][-1]["knowledge"] is True


def test_chat_welcome_examples_are_concrete_and_load_only_into_input(page, web_base_url):
    expected_examples = [
        "消費税率を変更すると、影響がありそうな箇所を教えてください。",
        "夜間バッチが異常終了しました。原因の候補を教えてください。",
        "消費税の端数処理の仕様を教えてください。",
        "登録されている資料の内容を要約した概要資料を作ってください。",
    ]
    records = _open(page, web_base_url)

    chips = page.locator(".example")
    expect(chips).to_have_count(4)
    texts = chips.locator(".exq").all_inner_texts()
    assert texts == expected_examples, f"質問例の文言または順序がずれている: {texts!r}"
    icons = set(chips.locator(".exarrow").all_inner_texts())
    assert icons == {"✎"}, f"アイコンが送信を連想させる記号（例: ↵）のままになっている: {icons}"

    for i, expected_text in enumerate(expected_examples):
        chips.nth(i).click()
        expect(page.locator("#input")).to_have_value(expected_text)
        assert records["turn_starts"] == [], f"チップ{i}のクリックだけで送信されている（読み込みのみのはず）"
        expect(page.locator(".msg.user")).to_have_count(0)


def test_chat_welcome_examples_use_admin_configured_content_when_set(page, web_base_url):
    custom_examples = ["在庫の締め処理はどうなっていますか？", "月次バッチの流れを教えてください。"]
    _open(page, web_base_url, settings={**mock_api.SETTINGS_RESP, "chat_examples": custom_examples})

    chips = page.locator(".example")
    expect(chips).to_have_count(len(custom_examples))
    expect(chips.locator(".exq")).to_have_text(custom_examples)
    chips.nth(0).click()
    expect(page.locator("#input")).to_have_value(custom_examples[0])


def test_chat_welcome_examples_hidden_when_admin_disabled(page, web_base_url):
    _open(page, web_base_url, settings={**mock_api.SETTINGS_RESP, "chat_examples": []})

    expect(page.locator(".example")).to_have_count(0)
    expect(page.locator(".headline")).to_contain_text("ようこそ Sherpa へ")


_EV0_ANSWER = {
    "lens": "qa",
    "headline": "確認しました。",
    "route": {"path": ["文書を検索"]},
    "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
    "data": {"citations": [{"doc_id": DOC, "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [
        {"doc_id": DOC, "download_url": "/documents/download?world=w1&rel=x"},
        {"doc_id": "<script>alert(1)</script>.md", "download_url": "/documents/download?world=w1&rel=y"},
        {"doc_id": "参考資料.md", "download_url": "/documents/download?world=w1&rel=z"},
    ],
    "sources_verified": [DOC],
}


def test_chat_sources_split_grounded_and_reference_and_export_follows(page, web_base_url, tmp_path):
    _open(page, web_base_url, stream_events=_answer_events(_EV0_ANSWER, conv=110))
    _send(page, "消費税率は?")

    expect(page.locator("#messages")).to_contain_text("根拠（精読済み）")
    expect(page.locator("#messages")).to_contain_text("参考（ヒットのみ）")
    expect(page.locator("#messages")).to_contain_text("税計算仕様書.md")
    expect(page.locator("#messages")).to_contain_text("<script>alert(1)</script>.md")
    sources_html = page.locator(".sources").first.inner_html()
    assert "<script>alert(1)</script>" not in sources_html, "doc_id がエスケープされず生の script タグとして描画されている"
    assert "&lt;script&gt;" in sources_html

    page.locator("#exportbtn").click()
    with page.expect_download() as dl_info:
        page.locator("#exportmenu [data-exp='txt']").click()
    txt_path = tmp_path / "export.txt"
    dl_info.value.save_as(txt_path)
    txt = txt_path.read_text(encoding="utf-8")
    assert "根拠: 4期/02_設計/01_基本設計/税計算仕様書.md" in txt
    assert "参考: 参考資料.md" in txt
    assert "出典: " not in txt

    page.locator("#exportbtn").click()
    with page.expect_download() as dl_info:
        page.locator("#exportmenu [data-exp='md']").click()
    md_path = tmp_path / "export.md"
    dl_info.value.save_as(md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "**根拠:** 4期/02_設計/01_基本設計/税計算仕様書.md" in md
    assert "**参考:** 参考資料.md" in md


def test_inquiry_block_lens_segment_sets_body_lens_and_grays_layer(page, web_base_url):
    records = _open(page, web_base_url, settings=_CODEX_SETTINGS)
    expect(page.locator("#messages")).to_contain_text("気になること")

    page.locator("#lens-seg [data-lens='impact']").click()
    expect(page.locator("#lens-seg [data-lens='impact']")).to_have_class(ON)
    expect(page.locator("#layer-seg [data-layer='docs']")).to_be_disabled()
    expect(page.locator("#layer-note")).to_be_visible()
    expect(page.locator("#inquiry-chip-label")).to_contain_text("影響")

    _ask(page, "消費税率を変えたい")
    body = records["turn_starts"][-1]
    assert body.get("lens") == "impact"
    assert "layer" not in body


def test_inquiry_block_layer_segment_sets_body_layer(page, web_base_url):
    records = _open(page, web_base_url)

    page.locator("#layer-seg [data-layer='code']").click()
    expect(page.locator("#layer-seg [data-layer='code']")).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).to_contain_text("コードのみ")

    _ask(page, "TAXCALC の仕様は？")
    body = records["turn_starts"][-1]
    assert body.get("layer") == "code"
    assert "lens" not in body


@pytest.mark.parametrize("width", [None, 950])
def test_inquiry_chip_opens_closed_right_pane_and_scrolls(page, web_base_url, width):
    install_api_mocks(page)
    if width:
        page.set_viewport_size({"width": width, "height": 700})
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#rightclose").click()
    expect(page.locator(".app")).to_have_class(re.compile(r"\brzero\b"))

    page.locator("#inquiry-chip").click()
    expect(page.locator(".app")).not_to_have_class(re.compile(r"\brzero\b"))
    expect(page.locator("#inquiry-body")).to_be_visible()
    if width:
        right_track = page.evaluate(
            "getComputedStyle(document.documentElement).getPropertyValue('--tR')").strip()
        assert right_track not in ("", "0px")


def test_inquiry_block_closes_on_send(page, web_base_url):
    _open(page, web_base_url)
    expect(page.locator("#inquiry-body")).to_be_visible()
    expect(page.locator("#inquiry-head")).to_have_attribute("aria-expanded", "true")

    _ask(page, "TAXCALC の仕様は？")
    expect(page.locator("#inquiry-body")).to_be_hidden()
    expect(page.locator("#inquiry-head")).to_have_attribute("aria-expanded", "false")

    page.locator("#inquiry-head").click()
    expect(page.locator("#inquiry-body")).to_be_visible()

    page.goto(f"{web_base_url}/chat.html?conv=101")
    expect(page.locator("#messages")).to_contain_text("消費税率")
    expect(page.locator("#inquiry-body")).to_be_visible()


@pytest.mark.parametrize("path, settings, lens, layer, chip", [
    ("/chat.html?conv=112", _CODEX_SETTINGS, "impact", "docs", "影響"),
    ("/chat.html?conv=113", None, "auto", "code", None),
])
def test_inquiry_restore_lens_and_layer_from_history(page, web_base_url, path, settings, lens, layer, chip):
    _open(page, web_base_url, path, settings=settings)
    expect(page.locator("#messages")).to_contain_text("消費税")
    expect(page.locator(f"#lens-seg [data-lens='{lens}']")).to_have_class(ON)
    expect(page.locator(f"#layer-seg [data-layer='{layer}']")).to_have_class(ON)
    if chip:
        expect(page.locator("#inquiry-chip-label")).to_contain_text(chip)


_DEPTH_DEEP_ANSWER = {
    "lens": "qa", "headline": "該当箇所が1件見つかりました。",
    "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
             "depth_profile": "deep"},
    "duration_ms": 252000,
    "data": {"citations": [{"doc_id": DOC, "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [{"doc_id": DOC, "download_url": "/documents/download?world=w1&rel=x"}],
}


@pytest.mark.parametrize("depth, label, header", [
    ("deep", "深く", "調べる深さ: 深く・所要 4分12秒"),
    ("quick", "クイック", "調べる深さ: クイック・所要 4分12秒"),
])
def test_inquiry_depth_profile_send_reflects_body_and_header(page, web_base_url, depth, label, header):
    answer = {**_DEPTH_DEEP_ANSWER, "scope": {**_DEPTH_DEEP_ANSWER["scope"], "depth_profile": depth}}
    records = _open(page, web_base_url, settings=_CODEX_SETTINGS, stream_events=_answer_events(answer))

    page.locator(f"#depth-seg [data-depth='{depth}']").click()
    expect(page.locator(f"#depth-seg [data-depth='{depth}']")).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).to_contain_text(label)

    _ask(page, "消費税率とは？")
    assert records["turn_starts"][-1].get("depth_profile") == depth
    expect(page.locator("#messages")).to_contain_text(header)


def test_inquiry_restore_depth_and_default_tools_then_new_conversation_resets_depth(page, web_base_url):
    _open(page, web_base_url, "/chat.html?conv=115", settings=_CODEX_SETTINGS)
    expect(page.locator("#messages")).to_contain_text("消費税率")
    expect(page.locator("#depth-seg [data-depth='deep']")).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).to_contain_text("深く")

    page.locator("#tools-details-head").click()
    for tool in ("grep", "fulltext", "graph"):
        expect(page.locator(f"#tools-seg [data-tool='{tool}']")).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")

    page.locator("#newbtn").click()
    expect(page.locator("#depth-seg [data-depth='standard']")).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).to_contain_text("標準")


_TOOLS_GRAPH_ONLY_ANSWER = {
    "lens": "qa", "headline": "原因候補は関係グラフから見つかりました。",
    "route": {"path": ["関係グラフを照会"]}, "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
             "tools": {"grep": False, "fulltext": False, "graph": True}},
    "data": {"citations": [{"doc_id": DOC, "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [{"doc_id": DOC, "download_url": "/documents/download?world=w1&rel=x"}],
}

_TOOL_OFF = "OFFにできません"


def _tool(page, name):
    return page.locator(f"#tools-seg [data-tool='{name}']")


def test_inquiry_tools_graph_only_send_reflects_body_and_hides_grep_fulltext_nodes(page, web_base_url):
    records = _open(page, web_base_url, settings=_CODEX_SETTINGS, stream_events=[
        {"type": "node", "id": "tool-graph", "kind": "tool", "status": "done",
         "label": "関係グラフをたどる", "detail": "「TAX-RATE」の関連部品"},
        *_answer_events(_TOOLS_GRAPH_ONLY_ANSWER),
    ])
    expect(page.locator("#tools-details-body")).to_be_hidden()
    expect(page.locator("#tools-details-head")).to_have_attribute("aria-expanded", "false")
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")

    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-details-body")).to_be_visible()
    _tool(page, "grep").click()
    _tool(page, "fulltext").click()
    expect(_tool(page, "grep")).not_to_have_class(ON)
    expect(_tool(page, "fulltext")).not_to_have_class(ON)
    expect(_tool(page, "graph")).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).to_contain_text("使う検索: グラフのみ")

    _ask(page, "夜間バッチが異常終了しました。原因は？")
    assert records["turn_starts"][-1].get("tools") == {"grep": False, "fulltext": False, "graph": True}
    expect(page.locator("#flow")).to_contain_text("関係グラフをたどる")
    expect(page.locator("#flow")).not_to_contain_text("資料を検索（語句そのまま）")
    expect(page.locator("#flow")).not_to_contain_text("資料を検索（全文/日本語）")


def test_inquiry_tools_last_one_cannot_be_turned_off(page, web_base_url):
    _open(page, web_base_url, settings=_CODEX_SETTINGS)
    page.locator("#tools-details-head").click()

    grep_btn = _tool(page, "grep")
    grep_btn.focus()
    page.keyboard.press("Enter")
    expect(grep_btn).not_to_have_class(ON)
    grep_btn.focus()
    page.keyboard.press(" ")
    expect(grep_btn).to_have_class(ON)

    grep_btn.click()
    _tool(page, "fulltext").click()
    graph_btn = _tool(page, "graph")
    expect(graph_btn).to_be_disabled()
    expect(graph_btn).to_have_attribute("title", re.compile(_TOOL_OFF))
    for _ in range(5):
        graph_btn.click(force=True, timeout=1000)
    expect(graph_btn).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).to_contain_text("使う検索: グラフのみ")


def test_inquiry_tools_unavailable_graph_is_hidden_and_omitted_from_send(page, web_base_url):
    records = _open(page, web_base_url, settings=_CODEX_SETTINGS,
                    tools_availability={"grep": True, "fulltext": True, "graph": False})
    page.locator("#tools-details-head").click()
    expect(_tool(page, "graph")).to_be_hidden()
    expect(_tool(page, "grep")).to_be_visible()
    expect(_tool(page, "fulltext")).to_be_visible()

    _tool(page, "grep").click()
    fulltext_btn = _tool(page, "fulltext")
    expect(fulltext_btn).to_be_disabled()
    expect(fulltext_btn).to_have_attribute("title", re.compile(_TOOL_OFF))

    _ask(page, "消費税率は？")
    expect(page.locator("#messages")).not_to_contain_text("送信に失敗しました")
    expect(page.locator("#messages")).not_to_contain_text("現在利用できません")
    body = records["turn_starts"][-1]
    assert "graph" not in body["tools"], "不達かつ未操作の graph はキー自体を省略するはず"
    assert body["tools"].get("grep") is False


_TOOLS_RETRY_HINT_ANSWER = {
    "lens": "qa", "headline": "該当する記述は見つかりませんでした（確証なし）。検索語を変えて試してください。",
    "route": {"path": ["文書を検索"]}, "summary": {"total": 0},
    "scope": {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
             "tools": {"grep": True, "fulltext": True, "graph": False}},
    "data": {"citations": []}, "sources": [],
    "retry_hints": [
        {"kind": "tools", "label": "OFF にした検索を戻す",
         "action": {"tools": {"grep": True, "fulltext": True, "graph": True}}},
    ],
}


def test_retry_hint_tools_button_resends_explicit_on_even_if_still_unavailable(page, web_base_url):
    records = _open(page, web_base_url, settings=_CODEX_SETTINGS,
                    tools_availability={"grep": True, "fulltext": True, "graph": False},
                    stream_events=_answer_events(_TOOLS_RETRY_HINT_ANSWER))
    _send(page, "消費税率とは？")
    expect(page.locator("#messages")).to_contain_text("OFF にした検索を戻す")

    page.locator(".retry-hint-btn", has_text="OFF にした検索を戻す").click()
    assert records["turn_starts"][-1].get("tools") == {"grep": True, "fulltext": True, "graph": True}
    expect(page.locator("#messages")).to_contain_text("現在利用できません")


def test_inquiry_tools_toggle_off_then_on_sends_explicit_despite_availability_drift(page, web_base_url):
    install_api_mocks(page, settings=_CODEX_SETTINGS,
                      tools_availability={"grep": True, "fulltext": True, "graph": True})
    turn_bodies = []

    def handle_turn_start(route):
        body = json.loads(route.request.post_data or "{}")
        turn_bodies.append(body)
        if (body.get("tools") or {}).get("graph") is True:
            _fulfill_json(route, {"detail": "検索経路 graph は現在利用できません"}, status=422)
            return
        _fulfill_json(route, {"turn_id": "turn-101", "conversation_id": 101})

    page.route("**/chat/turns", handle_turn_start)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#tools-details-head").click()

    graph_btn = _tool(page, "graph")
    graph_btn.click()
    expect(graph_btn).not_to_have_class(ON)
    graph_btn.click()
    expect(graph_btn).to_have_class(ON)

    _send(page, "消費税率とは？")
    expect(page.locator("#messages")).to_contain_text("現在利用できません")
    assert turn_bodies[-1].get("tools", {}).get("graph") is True


def test_inquiry_restore_tools_from_history_then_new_conversation_resets(page, web_base_url):
    records = _open(page, web_base_url, "/chat.html?conv=116", settings=_CODEX_SETTINGS)
    expect(page.locator("#messages")).to_contain_text("消費税率")
    expect(page.locator("#tools-details-body")).to_be_hidden()
    page.locator("#tools-details-head").click()
    expect(_tool(page, "grep")).not_to_have_class(ON)
    expect(_tool(page, "fulltext")).to_have_class(ON)
    expect(_tool(page, "graph")).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).to_contain_text("使う検索")

    _ask(page, "影響範囲を教えて")
    assert records["turn_starts"][-1].get("tools") == {"grep": False, "fulltext": True, "graph": True}

    page.locator("#newbtn").click()
    expect(page.locator("#tools-details-body")).to_be_hidden()
    page.locator("#tools-details-head").click()
    for tool in ("grep", "fulltext", "graph"):
        expect(_tool(page, tool)).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")


def test_new_conversation_during_pending_world_options_ignores_stale_conv_followup(page, web_base_url):
    held = {}
    records = install_api_mocks(page, settings=_CODEX_SETTINGS)
    page.route("**/world-options", lambda route: held.__setitem__("route", route))
    page.goto(f"{web_base_url}/chat.html?conv=116")
    expect(page.locator("#messages")).to_contain_text("消費税率")

    page.locator("#newbtn").click()
    expect(page.locator("#conv-title")).to_have_text("新しい会話")
    _fulfill_json(held["route"], {"worlds": ["w1"], "labels": {"w1": "4期"}})

    page.locator("#tools-details-head").click()
    for tool in ("grep", "fulltext", "graph"):
        expect(_tool(page, tool)).to_have_class(ON)
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")

    _ask(page, "消費税率とは")
    assert "tools" not in records["turn_starts"][-1], "全軸未操作＝既定ONは body.tools キー自体を省略するはず"


def test_slash_prefix_message_sent_verbatim_without_body_lens(page, web_base_url):
    records = _open(page, web_base_url)

    _ask(page, "/影響 消費税率を変えたい")
    body = records["turn_starts"][-1]
    assert body["message"] == "/影響 消費税率を変えたい"
    assert "lens" not in body
    expect(page.locator("#lens-seg [data-lens='auto']")).to_have_class(ON)


_RETRY_HINT_ANSWER = {
    "lens": "qa", "headline": "該当する記述は見つかりませんでした（確証なし）。検索語を変えて試してください。",
    "route": {"path": ["文書を検索"]}, "summary": {"total": 0},
    "scope": {"world": "w1", "scope_paths": ["4期/02_設計"], "source": "explicit",
             "layer": "docs", "layer_applied": True},
    "data": {"citations": []}, "sources": [],
    "retry_hints": [
        {"kind": "scope", "label": "範囲を全体に広げる", "action": {"scope_paths": []}},
        {"kind": "layer", "label": "コードも含めて探す（今は資料のみ）", "action": {"layer": "both"}},
    ],
}

_DEPTH_RETRY_HINT_ANSWER = {
    **_RETRY_HINT_ANSWER,
    "scope": {**_RETRY_HINT_ANSWER["scope"], "depth_profile": "standard"},
    "retry_hints": [
        {"kind": "depth", "label": "調べる深さを上げて探す（今は標準）", "action": {"depth_profile": "max"}},
    ],
}


def test_retry_hint_button_broadens_scope_and_resends(page, web_base_url):
    records = _open(page, web_base_url, stream_events=_answer_events(_RETRY_HINT_ANSWER))
    _send(page, "消費税率とは？")
    expect(page.locator("#messages")).to_contain_text("範囲を全体に広げる")
    expect(page.locator("#messages")).to_contain_text("コードも含めて探す")

    page.locator(".retry-hint-btn", has_text="範囲を全体に広げる").click()
    expect(page.locator("#rt")).to_contain_text("完了")
    body = records["turn_starts"][-1]
    assert body.get("scope_paths") == []
    assert body.get("layer") == "docs"
    assert body.get("message") == "消費税率とは？"


def test_retry_hint_button_raises_depth_profile_and_resends(page, web_base_url):
    records = _open(page, web_base_url, settings=_CODEX_SETTINGS,
                    stream_events=_answer_events(_DEPTH_RETRY_HINT_ANSWER))
    _send(page, "消費税率とは？")
    expect(page.locator("#messages")).to_contain_text("調べる深さを上げて探す")

    page.locator(".retry-hint-btn", has_text="調べる深さを上げて探す").click()
    expect(page.locator("#rt")).to_contain_text("完了")
    body = records["turn_starts"][-1]
    assert body.get("depth_profile") == "max"
    assert body.get("scope_paths") == ["4期/02_設計"]
    assert body.get("layer") == "docs"
    assert body.get("message") == "消費税率とは？"


_TIMEOUT_NOTICE = "（時間の上限に達したため、ここまでの結果で打ち切りました）"


def _resume_answer(flag):
    answer = {
        "lens": "qa", "headline": "次に資料を確認します。",
        "route": {"path": ["文書を検索"]}, "summary": {"total": 0},
        "scope": {"world": "w1", "scope_paths": [], "source": "explicit", "layer": "both", "layer_applied": True},
        "data": {"citations": ["doc1"]}, "sources": ["doc1"],
        "retry_hints": [{"kind": "resume", "label": "続きを調べる", "action": {"message": "続きを調べて"}}],
    }
    text = _TIMEOUT_NOTICE if flag == "notices" else (
        "AI が途中経過を伝えたまま調査を終えたため、途中までの結果です。「続きを調べる」を押すと続きから調べられます。")
    answer.update(body="次に資料を確認します。", completion="partial", answer_schema=2,
                  notices=[{"kind": "wall_clock" if flag == "notices" else flag, "text": text}],
                  headline=text + "\n\n次に資料を確認します。")
    if flag == "stopped_early":
        answer["codex_stopped_early"] = True
    return answer


@pytest.mark.parametrize("flag, note_selector, note_text", [
    ("notices", ".answer-notice", "時間の上限に達したため"),
    ("stopped_early", ".answer-notice", "AI が途中経過を伝えたまま調査を終えたため、途中までの結果です"),
])
def test_codex_incomplete_note_and_continue_button_resends_fixed_message(
        page, web_base_url, flag, note_selector, note_text):
    records = _open(page, web_base_url, stream_events=_answer_events(_resume_answer(flag)))
    _send(page, "消費税率とは？")
    expect(page.locator("#messages")).to_contain_text("次に資料を確認します。")
    note = page.locator(note_selector).last
    expect(note).to_be_visible()
    expect(note).to_contain_text(note_text)
    if True:
        # 注記は本文の上の別要素で、本文の欄には混ざらない。
        expect(page.locator(".a-body > .answer-notices + .headline")).to_have_count(1)
        expect(page.locator(".headline").last).not_to_contain_text("時間の上限")

    page.locator(".retry-hint-btn", has_text="続きを調べる").click()
    expect(page.locator("#rt")).to_contain_text("途中までの回答")
    body = records["turn_starts"][-1]
    assert body.get("message") == "続きを調べて"
    assert not body.get("scope_paths")
    assert body.get("layer") is None


@pytest.mark.parametrize("answer, expect_notice", [
    ({"lens": "qa", "headline": "旧形式の回答です。", "sources": [], "summary": {"total": 0}}, False),
    ({"lens": "qa", "headline": "注記の文。\n\n新形式の回答です。", "body": "新形式の回答です。", "answer_schema": 2,
      "completion": "partial", "notices": [{"kind": "stopped", "text": "注記の文。"}],
      "sources": [], "summary": {"total": 0}}, True),
])
def test_old_and_new_rows_render_and_export_with_notices(page, web_base_url, tmp_path, answer, expect_notice):
    _open(page, web_base_url, stream_events=_answer_events(answer, conv=120))
    _send(page, "質問です")
    expect(page.locator("#messages")).to_contain_text("回答です。")
    assert page.locator(".answer-notice").count() == (1 if expect_notice else 0)
    # 書き出し（メニューの実装 exportMessages）へ保存行と同じ形を渡す。
    with page.expect_download() as dl_info:
        page.evaluate(
            "async (msgs) => { const m = await import('/chat/menus.js'); m.exportMessages('t', msgs, 'txt'); }",
            [{"role": "user", "content": "質問です"}, {"role": "assistant", "answer": answer}])
    txt_path = tmp_path / "export.txt"
    dl_info.value.save_as(txt_path)
    txt = txt_path.read_text(encoding="utf-8")
    assert answer.get("body", answer["headline"]) in txt
    assert ("※ 注記の文。" in txt and "途中までの回答" in txt) == expect_notice
    assert txt.count("旧形式の回答です。" if not expect_notice else "注記の文。") == 1


def _confirm_first_resend(page, base, question, text, settings=_CODEX_SETTINGS):
    """1回目の stream は確認カード（question）、2回目は回答。カードを選んで送信し、records を返す。"""
    calls = {"n": 0}

    def handle_turn_stream(route):
        calls["n"] += 1
        events = [question] if calls["n"] == 1 else _answer_events(IMPACT_ANSWER)
        route.fulfill(status=200, headers=SSE, body=_sse_body(events))

    records = install_api_mocks(page, settings=settings)
    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.goto(f"{base}/chat.html")
    _send(page, text)
    expect(page.locator(".askcard")).to_be_visible()
    page.locator("[data-qopt]").first.check()
    page.locator("[data-ask-submit]").click()
    expect(page.locator("#rt")).to_contain_text("完了")
    assert len(records["turn_starts"]) == 2
    return records


def _confirm_question(extra):
    return {
        "type": "question", "conversation_id": 101, "interaction_id": "confirm-abcd", "mode": "single",
        "prompt": "確認してから進めるよう指定されています。何を確認してから進めますか？",
        "options": [{"id": "scope", "label": "対象範囲（どの資料/システムか）",
                     "description": "どのフォルダ・資料・システムを対象にするか"}],
        "allow_free_text": True, "layer": None, "scope_paths": [], **extra,
    }


def test_confirm_first_resend_restores_slash_lens_via_question_payload(page, web_base_url):
    question = _confirm_question({"original_message": "税率表を確認してから進めて。", "lens": "impact",
                                  "lens_source": "slash", "lens_block": "qa"})
    records = _confirm_first_resend(page, web_base_url, question, "/影響 税率表を確認してから進めて。")
    resend = records["turn_starts"][1]
    assert resend.get("lens") == "qa"
    assert resend.get("message", "").startswith("/影響 ")
    expect(page.locator("#lens-seg [data-lens='auto']")).to_have_class(ON)


def test_confirm_first_resend_restores_tools_from_question_payload(page, web_base_url):
    question = _confirm_question({"original_message": "原因を確認してから進めて。", "lens": "qa",
                                  "lens_source": "explicit", "lens_block": None,
                                  "tools": {"grep": False, "fulltext": False, "graph": True}})
    records = _confirm_first_resend(page, web_base_url, question, "原因を確認してから進めて。")
    assert records["turn_starts"][1].get("tools") == {"grep": False, "fulltext": False, "graph": True}


_WEB_SEARCH_ELIGIBLE_SETTINGS = {
    **mock_api.SETTINGS_RESP,
    "agent": "codex", "codex_model_provider": "openai", "construct_id": "codex_openai",
    "web_search_available": True, "openai_endpoint_kind": "openai",
}


@pytest.mark.parametrize("override, visible", [
    (None, False),
    ({}, True),
    ({"openai_endpoint_kind": "azure"}, False),
    ({"codex_model_provider": "ollama", "construct_id": "codex_ollama"}, False),
    ({"web_search_available": False}, False),
])
def test_web_search_row_visibility(page, web_base_url, override, visible):
    settings = None if override is None else {**_WEB_SEARCH_ELIGIBLE_SETTINGS, **override}
    _open(page, web_base_url, settings=settings)
    if visible:
        expect(page.locator("#websearchtoggle")).to_be_visible()
    else:
        expect(page.locator("#websearchtoggle")).to_be_hidden()


def test_web_search_toggle_sends_body_web_search_true_only_when_on(page, web_base_url):
    records = _open(page, web_base_url, settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    expect(page.locator("#websearchtoggle")).to_be_visible()

    _ask(page, "消費税の最新情報は？")
    assert "web_search" not in records["turn_starts"][-1], "既定 OFF なのに web_search が送られている"

    page.locator("#inquiry-head").click()
    expect(page.locator("#websearchtoggle")).to_be_visible()
    page.locator("#websearchtoggle").click()
    expect(page.locator("#websearchtoggle")).to_have_class(ON)
    _ask(page, "もう一つ教えてください")
    assert records["turn_starts"][-1]["web_search"] is True


def test_web_search_restore_from_history_then_new_conversation_starts_off(page, web_base_url):
    _open(page, web_base_url, "/chat.html?conv=114", settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    expect(page.locator("#messages")).to_contain_text("消費税")
    expect(page.locator("#websearchtoggle")).to_be_visible()
    expect(page.locator("#websearchtoggle")).to_have_class(ON)

    page.locator("#newbtn").click()
    expect(page.locator("#websearchtoggle")).to_be_visible()
    expect(page.locator("#websearchtoggle")).not_to_have_class(ON)


def test_web_search_pending_conv_world_followup_restores_web_search(page, web_base_url):
    held = {}
    install_api_mocks(page, settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    page.route("**/world-options", lambda route: held.__setitem__("route", route))
    page.goto(f"{web_base_url}/chat.html?conv=114")
    expect(page.locator("#messages")).to_contain_text("消費税")

    _fulfill_json(held["route"], {"worlds": ["w1"], "labels": {"w1": "4期"}})
    expect(page.locator("#websearchtoggle")).to_have_class(ON)


def _md(page, web_base_url, text):
    page.goto(f"{web_base_url}/login.html")
    return page.evaluate("(t) => Sherpa.mdLite(t)", text)


_MDLITE_EXACT = [
    ("* 一\n  - 一の子\n  1. 番号の子\n+ 二",
     "<ul><li>一<ul><li>一の子</li></ul><ol><li>番号の子</li></ol></li><li>二</li></ul>"),
    ("> 引用 **強調**\n> 続き\n\n---\n\n#### 見出し4",
     "<blockquote><p>引用 <strong>強調</strong><br>続き</p></blockquote><hr><p><strong>見出し4</strong></p>"),
    ("SELECT * FROM T WHERE a * b > 0", "<p>SELECT * FROM T WHERE a * b &gt; 0</p>"),
    ("*.cbl と *.cpy を対象", "<p>*.cbl と *.cpy を対象</p>"),
    ("| 条件 | 意味 |\n|---|---|\n| A \\| B | A または B |",
     "<table class=\"md-table\"><thead><tr><th>条件</th><th>意味</th></tr></thead>"
     "<tbody><tr><td>A | B</td><td>A または B</td></tr></tbody></table>"),
    ("| コード | 意味 |\n|---|---|\n| `a|b` | aまたはb |",
     "<table class=\"md-table\"><thead><tr><th>コード</th><th>意味</th></tr></thead>"
     "<tbody><tr><td><code>a|b</code></td><td>aまたはb</td></tr></tbody></table>"),
    ("残高 >= 0 の場合\n>= 0 なら継続", "<p>残高 &gt;= 0 の場合<br>&gt;= 0 なら継続</p>"),
    ("``` sql\nSELECT 1\n```\n\n次の段落 **太字**",
     '<pre class="md-code"><code>SELECT 1</code></pre><p>次の段落 <strong>太字</strong></p>'),
    ("1. 実行:\n   ```sql\n   SELECT 1\n   ```\n2. 完了",
     '<ol><li>実行:<pre class="md-code"><code>SELECT 1</code></pre></li><li>完了</li></ol>'),
    ("1. 手順A\n\n2. 手順B\n\n3. 手順C", "<ol><li>手順A</li><li>手順B</li><li>手順C</li></ol>"),
    ("1. 手順A\n   詳細A\n2. 手順B", "<ol><li>手順A<br>詳細A</li><li>手順B</li></ol>"),
    ("3. 三\n4. 四", '<ol start="3"><li>三</li><li>四</li></ol>'),
    ("[wiki](https://ja.wikipedia.org/wiki/COBOL_(言語)) を参照",
     '<p><a href="https://ja.wikipedia.org/wiki/COBOL_(言語)" '
     'target="_blank" rel="noopener noreferrer">wiki</a> を参照</p>'),
    ("| a | b |\n|---|---|\n| 1 | 2\n| 3 | 4 |",
     "<table class=\"md-table\"><thead><tr><th>a</th><th>b</th></tr></thead>"
     "<tbody><tr><td>1</td><td>2</td></tr><tr><td>3</td><td>4</td></tr></tbody></table>"),
    ("項目 | 件数\n--- | ---\nA | 1",
     "<table class=\"md-table\"><thead><tr><th>項目</th><th>件数</th></tr></thead>"
     "<tbody><tr><td>A</td><td>1</td></tr></tbody></table>"),
    ("**`MOVE`** 文は転記", "<p><strong><code>MOVE</code></strong> 文は転記</p>"),
    ("**COUNT(*)** は **NULL** を数えない", "<p><strong>COUNT(*)</strong> は <strong>NULL</strong> を数えない</p>"),
    ("[`README.md`](https://x/README.md)",
     '<p><a href="https://x/README.md" target="_blank" '
     'rel="noopener noreferrer"><code>README.md</code></a></p>'),
    ("**COUNT(*)** と **COUNT(*)**", "<p><strong>COUNT(*)</strong> と <strong>COUNT(*)</strong></p>"),
    ("1. 実行:\n   ```\n   cmd\n   ```\n   出力を確認する\n2. 次へ",
     '<ol><li>実行:<pre class="md-code"><code>cmd</code></pre><div>出力を確認する</div></li>'
     '<li>次へ</li></ol>'),
    ("````\n```\nx\n```\n````", '<pre class="md-code"><code>```\nx\n```</code></pre>'),
    ("- a\n", "<ul><li>a</li></ul>"),
    ("テキスト\n- a\n- b\n", "<p>テキスト</p><ul><li>a</li><li>b</li></ul>"),
    ("- 項目\n\n  > 引用文", "<ul><li>項目</li></ul><blockquote><p>引用文</p></blockquote>"),
    ("1. 親\n   1. 子\n      ```\n      SELECT 1\n      ```",
     '<ol><li>親<ol><li>子<pre class="md-code"><code>SELECT 1</code></pre></li></ol></li></ol>'),
]


def test_mdlite_renders_expected_html(page, web_base_url):
    page.goto(f"{web_base_url}/login.html")
    for text, expected in _MDLITE_EXACT:
        html = page.evaluate("(t) => Sherpa.mdLite(t)", text)
        assert html == expected, text


def test_mdlite_tables_links_and_escaping(page, web_base_url):
    html = _md(page, web_base_url, "| 項目 | 件数 |\n|---|--:|\n| A | 1 |\n| B | 2 |")
    assert html.startswith('<table class="md-table"><thead><tr><th>項目</th><th class="md-al-right">件数</th></tr></thead>')
    assert '<tr><td>B</td><td class="md-al-right">2</td></tr>' in html

    html = _md(page, web_base_url,
               "[資料](https://example.com/a?x=1&y=2) [危険](javascript:alert(1)) [d](data:text/html,x)")
    assert '<a href="https://example.com/a?x=1&amp;y=2" target="_blank" rel="noopener noreferrer">資料</a>' in html
    assert "<a" in html and html.count("<a ") == 1
    assert "[危険](javascript:alert(1))" in html

    html = _md(page, web_base_url, "| <b>x</b> |\n|---|\n| <img src=x onerror=window.__xss=1> |\n\n> <script>1</script>")
    assert "<b>" not in html and "<img" not in html and "<script>" not in html
    assert "&lt;img src=x onerror=window.__xss=1&gt;" in html

    html = _md(page, web_base_url, "| a | b |\n|---|---|\n| 1 | 2 |\n| `x | 3 |\n| 4 | 5 |")
    assert html.count("<tr>") == 4
    assert "<td>`x</td><td>3</td>" in html

    html = _md(page, web_base_url, "| a | b |\n|---|---|\n| 1 | 2 | 3 |\n| 4 | 5 |")
    assert "<td>2 | 3</td>" in html
    assert "<td>4</td><td>5</td>" in html
    assert "<p>" not in html

    html = _md(page, web_base_url, "1. 手順\n\n   | A | B |\n   |---|---|\n   | 1 | 2 |")
    assert html.startswith("<ol><li>手順</li></ol><table")


def test_inquiry_restore_only_touched_axis_is_explicit(page, web_base_url):
    records = _open(page, web_base_url, "/chat.html?conv=118", settings=_CODEX_SETTINGS,
                    tools_availability={"grep": True, "fulltext": True, "graph": False})
    expect(page.locator("#messages")).to_contain_text("消費税率")

    _ask(page, "影響範囲を教えて")
    expect(page.locator("#messages")).not_to_contain_text("現在利用できません")
    body = records["turn_starts"][-1]
    assert "graph" not in body["tools"], "触っていない graph は明示扱いにしない（不達なら省略）"
    assert body["tools"].get("grep") is False
    assert body["tools_explicit"] == ["grep"]


def test_confirm_first_resend_does_not_persist_override_as_explicit(page, web_base_url):
    question = _confirm_question({"original_message": "原因を確認してから進めて。", "lens": "qa",
                                  "lens_source": "explicit", "lens_block": None,
                                  "tools": {"grep": True, "fulltext": True, "graph": True}})
    records = _confirm_first_resend(page, web_base_url, question, "原因を確認してから進めて。")
    resend = records["turn_starts"][1]
    assert resend.get("tools") == {"grep": True, "fulltext": True, "graph": True}
    assert "tools_explicit" not in resend, "チップを触っていないのに明示状態として保存してはいけない"


_TROUBLE_DEGRADED_ANSWER = {
    "lens": "troubleshoot",
    "headline": "関係のつながりをたどる検索が使えないため、資料とソースを直接調べて回答します。\n\n夜間バッチの停止は TAXCALC の異常終了が原因の可能性があります。",
    "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all", "layer": "both"},
    "data": {"type": "qa", "citations": [
        {"doc_id": "4期/03_開発/01_ソース/TAXCALC.cbl", "span": [10, 12], "quote": "ABEND-CODE 0C7"}]},
    "sources": [],
}


def test_troubleshoot_without_candidates_falls_back_to_citation_view(page, web_base_url):
    _open(page, web_base_url, stream_events=_answer_events(_TROUBLE_DEGRADED_ANSWER))
    _send(page, "夜間バッチが止まる")
    expect(page.locator("#messages")).to_contain_text("資料とソースを直接調べて回答します")
    expect(page.locator("#messages")).to_contain_text("該当箇所 (1)")


def test_export_includes_citations_when_troubleshoot_degraded_to_qa_shape(page, web_base_url, tmp_path):
    _open(page, web_base_url, "/chat.html?conv=119")
    page.wait_for_selector("#messages .cites")

    page.locator("#exportbtn").click()
    with page.expect_download() as dl_info:
        page.locator("#exportmenu [data-exp='txt']").click()
    txt_path = tmp_path / "export_degraded.txt"
    dl_info.value.save_as(txt_path)
    assert "4期/03_開発/01_ソース/TAXCALC.cbl（行10-12）: ABEND-CODE 0C7" in txt_path.read_text(encoding="utf-8")


def test_new_conversation_resets_knowledge_toggle_to_on_after_chat_lens_conversation(page, web_base_url):
    records = install_api_mocks(page, settings=_UNLOCKED_KB_SETTINGS)
    page.route("**/conversations/777", lambda route: _fulfill_json(route, {
        "conversation": {"id": 777, "title": "雑談", "origin": "own", "version": "w1",
                         "read_only": False, "contains_personal_workspace": False},
        "messages": [
            {"role": "user", "content": "こんにちは", "created_at": "2026-09-01T00:00:00+00:00"},
            {"role": "assistant", "content": "こんにちは！", "answer": {"lens": "chat"},
             "trace": None, "created_at": "2026-09-01T00:00:05+00:00"},
        ],
    }))
    page.goto(f"{web_base_url}/chat.html")

    page.evaluate("(id) => window.__sherpaChatTest.openConversation(id)", 777)
    expect(page.locator("#kbtoggle")).to_have_attribute("aria-pressed", "false")

    page.locator("#newbtn").click()
    expect(page.locator("#kbtoggle")).to_have_attribute("aria-pressed", "true")

    _send(page, Q_IMPACT)
    assert records["turn_starts"], "POST /chat/turns が呼ばれていない"
    assert records["turn_starts"][-1]["knowledge"] is True


@pytest.mark.parametrize("world_options_fail, pressed", [(False, "false"), (True, "true")])
def test_knowledge_toggle_follows_world_options_state(page, web_base_url, world_options_fail, pressed):
    if world_options_fail:
        records = install_api_mocks(page)
        page.route("**/world-options", lambda route: route.abort())
    else:
        records = install_api_mocks(page, settings=_UNLOCKED_KB_SETTINGS,
                                   world_options={"worlds": [], "labels": {}})
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#kbtoggle")).to_have_attribute("aria-pressed", pressed)
    if not world_options_fail:
        _send(page, "こんにちは")
        assert records["turn_starts"], "POST /chat/turns が呼ばれていない"
        assert records["turn_starts"][-1]["knowledge"] is False

    page.locator("#newbtn").click()
    expect(page.locator("#kbtoggle")).to_have_attribute("aria-pressed", pressed)
    _send(page, "こんにちは")
    assert records["turn_starts"], "POST /chat/turns が呼ばれていない"
    assert records["turn_starts"][-1]["knowledge"] is (pressed == "true")


def _usage(provider, model, inp, cached, out, local):
    return {"provider": provider, "model": model, "input_tokens": inp, "cached_input_tokens": cached,
            "output_tokens": out, "reasoning_output_tokens": 0, "is_local": local}


_BREAKDOWN_PARENT = {"input_tokens": 800, "cached_input_tokens": 200, "output_tokens": 100,
                     "reasoning_output_tokens": 0}


@pytest.mark.parametrize("usage, summary, summary_without, shown, hidden", [
    (_usage("openai", "gpt-5.5", 12000, 9000, 2000, "cloud"),
     "🪙 3k in（+9k cache） / 2k out", None,
     ["入力トークン: 12,000", "実入力 3,000・キャッシュ 9,000"], []),
    (_usage("ollama", "qwen2.5", 5000, None, 800, "local"),
     "🪙 5k in / 800 out", "cache", ["入力トークン: 5,000"], ["キャッシュ"]),
    (_usage("openai", "gpt-5.5", 400, 0, 60, "cloud"),
     "🪙 400 in / 60 out", "cache", [], []),
    ({**_usage("codex", "qwen2.5", 1100, 250, 160, "local"), "codex_usage_breakdown": {
        "parent": _BREAKDOWN_PARENT,
        "children": {"input_tokens": 300, "cached_input_tokens": 50, "output_tokens": 60,
                     "reasoning_output_tokens": 0},
        "children_found": 2, "children_missing": 1}},
     None, None,
     ["本体: 入力 800", "うちキャッシュ 200", "出力 100", "下調べ役 3 体: 入力 300",
      "うちキャッシュ 50", "1 体は記録なし"], []),
    ({**_usage("codex", "qwen2.5", 800, 200, 100, "local"), "codex_usage_breakdown": {
        "parent": _BREAKDOWN_PARENT,
        "children": {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
                     "reasoning_output_tokens": 0},
        "children_found": 0, "children_missing": 2}},
     None, None, ["下調べ役 2 体: 入力 0 / 出力 0", "2 体は記録なし"], []),
])
def test_chat_usage_meta_cache_and_breakdown(page, web_base_url, usage, summary, summary_without, shown, hidden):
    _open(page, web_base_url, stream_events=_answer_events({**IMPACT_ANSWER, "usage": usage}))
    _ask(page, "消費税率とは？")

    usage_meta = page.locator(".usage-meta")
    expect(usage_meta).to_have_count(1)
    expect(usage_meta.locator(".usage-detail")).to_be_hidden()
    if summary:
        expect(usage_meta.locator("summary")).to_contain_text(summary)
    if summary_without:
        expect(usage_meta.locator("summary")).not_to_contain_text(summary_without)

    usage_meta.locator("summary").click()
    expect(usage_meta.locator(".usage-detail")).to_be_visible()
    for text in shown:
        expect(usage_meta).to_contain_text(text)
    for text in hidden:
        expect(usage_meta).not_to_contain_text(text)


def test_chat_ime_composition_enter_does_not_send(page, web_base_url):
    records = _open(page, web_base_url)
    expect(page.locator("#messages")).to_contain_text("気になること")

    page.locator("#input").fill("へんかんちゅう")
    page.locator("#input").dispatch_event("keydown", {"key": "Enter", "keyCode": 229, "isComposing": True})
    page.wait_for_timeout(300)
    assert not records["turn_starts"], "変換確定の Enter で送信された"

    page.locator("#input").press("Enter")
    expect(page.locator("#messages")).to_contain_text("へんかんちゅう")
    assert records["turn_starts"]


_MANY_CANDIDATES_ANSWER = {
    "lens": "troubleshoot", "headline": "原因候補です。", "body": "原因候補です。", "answer_schema": 2,
    "summary": {"total": 10},
    "data": {"candidates": [{"name": f"候補{i}", "role": "原因"} for i in range(10)]},
    "sources": [{"doc_id": "a/b.md", "download_url": "/documents/download?world=w&rel=a%2Fb.md"},
                {"doc_id": "c/d.md"}],
    "personal_sources": [{"doc_id": "個人メモ.txt", "quote": "あ" * 250}],
}


def test_trouble_candidates_show_more_count_unlinked_source_and_clipped_personal_quote(page, web_base_url):
    _open(page, web_base_url, stream_events=_answer_events(_MANY_CANDIDATES_ANSWER))
    _send(page, "夜間バッチが止まる")
    expect(page.locator(".cand")).to_have_count(10)   # 折りたたみの中も含めて全件ある
    more = page.locator("details.cand-more")
    expect(more.locator("summary")).to_contain_text("ほか 2 件")
    expect(more.locator(".cand")).to_have_count(2)
    # リンクの無い出典は href を空にせず、ダウンロードできないと分かる。
    expect(page.locator(".sources a[data-dl]")).to_have_count(1)
    expect(page.locator(".sources")).to_contain_text("c/d.md（ダウンロードできません）")
    snippet = page.locator(".personal-sources .src-snippet").inner_text()
    assert snippet == "あ" * 200 + "…"


def test_export_includes_budget_stop_and_personal_sources_but_not_when_shared(page, web_base_url, tmp_path):
    _open(page, web_base_url, stream_events=_answer_events(_MANY_CANDIDATES_ANSWER))
    answer = {**_MANY_CANDIDATES_ANSWER, "lens": "qa",
              "data": {"evidence_packet": {"stop_reason": "turns_exhausted"}}}
    msgs = [{"role": "user", "content": "質問"}, {"role": "assistant", "answer": answer}]
    texts = []
    for shared in (False, True):
        with page.expect_download() as dl_info:
            page.evaluate(
                "async ([msgs, shared]) => { const m = await import('/chat/menus.js'); m.exportMessages('t', msgs, 'txt', shared); }",
                [msgs, shared])
        path = tmp_path / f"export{int(shared)}.txt"
        dl_info.value.save_as(path)
        texts.append(path.read_text(encoding="utf-8"))
    own, shared_txt = texts
    assert "調査の上限に達したため、途中までの結果で答えています" in own and "調査の上限に達したため" in shared_txt
    assert "個人メモ.txt" in own and "個人メモ.txt" not in shared_txt
