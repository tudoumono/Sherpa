"""チャットターンのバックグラウンド実行（覗き窓方式・docs/proposals/2026-07-03-チャット背景実行.md）の e2e。

送信→画面遷移→戻る、というシナリオそのものを確認する。トップバーのバッジ単体の確認は
test_topbar_ui.py（全ページ共通の見え方）、購読ゼロ完走・cursor replay 等のサーバ挙動は
tests/api/test_chat_turns.py が担当する。
"""
from __future__ import annotations

import json

from mock_api import IMPACT_ANSWER, install_api_mocks


def test_chat_background_turn_survives_navigation_and_resumes_on_return(page, web_base_url):
    """送信直後に別ページへ移動してもターンは止まらず（GET /chat/turns/{turn_id}/stream への
    最初の購読を保留＝切断を模す）、トップバーに「実行中」バッジが出る。会話に戻ると
    実行中ターンを自動検知して再購読し（2回目の GET）、続きから回答が表示される。"""
    from playwright.sync_api import expect

    stream_calls = {"n": 0}

    def handle_turn_stream(route):
        stream_calls["n"] += 1
        if stream_calls["n"] == 1:
            return   # fulfill しない＝保留（送信直後の「実行中」を作る・別ページへの遷移で自然に破棄される）
        body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in [
            {"type": "node", "id": "understand", "kind": "think", "status": "done",
             "label": "質問を理解", "detail": "内容を把握しました"},
            {"type": "answer", "conversation_id": 101, "message": {"answer": IMPACT_ANSWER}},
        ])
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)

    def handle_conversation(route):
        # 実行中＝まだ assistant メッセージは保存されていない状態（user 発言のみ）。
        route.fulfill(content_type="application/json", body=json.dumps({
            "conversation": {"id": 101, "title": "消費税率の相談", "origin": "own",
                             "contains_personal_workspace": False},
            "messages": [{"id": 1, "role": "user", "content": "消費税率を変えたい。影響は？",
                         "created_at": "2026-07-03T09:00:00+00:00"}],
        }))

    def handle_running(route):
        # 簡略化のため常に「実行中」を返す（会話に戻った直後の再購読が完走イベントで終わる想定）。
        route.fulfill(content_type="application/json", body=json.dumps(
            {"turns": [{"turn_id": "turn-101", "conversation_id": 101,
                       "started_at": "2026-07-03T09:00:00+00:00"}]}))

    install_api_mocks(page)
    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/conversations/101", handle_conversation)
    page.route("**/chat/turns/running", handle_running)

    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    send_btn = page.locator("#send")
    expect(send_btn).to_have_text("■")   # 実行中（サーバはまだ応答を返していない＝保留中）

    # 別ページへ遷移（unsubscribeTurn のみ＝サーバ側の停止要求は送らない・ターンは続く）。
    page.goto(f"{web_base_url}/home.html")
    expect(page.locator("#turnnotice")).to_be_visible()
    expect(page.locator("#turnnotice")).to_contain_text("回答作成中")

    # トップバーのバッジから該当会話へ戻る。
    page.locator("#turnnotice").click()
    page.wait_for_url("**/chat.html?conv=101")

    # resumeRunningTurn が自動で再購読（2回目の GET）→ 続きから回答が表示される。
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator("#messages")).to_contain_text("TAXCALC")
    expect(page.locator("#rt")).to_contain_text("完了")
    assert stream_calls["n"] == 2, f"再購読が期待どおりに行われていない: {stream_calls['n']}"


def test_resume_running_turn_ignores_stale_response_when_conversation_changed(page, web_base_url):
    """MEDIUM（Codex RV）: `resumeRunningTurn(cid, ...)` の `GET /chat/turns/running` 応答待ちの間に
    画面の会話（`_cid`）が別の値へ変わっていたら、届いた応答は古い（stale）とみなして購読を張らない
    （resumeRunningTurn 冒頭の世代チェック＝現在の会話IDとの一致確認）。修正前は「会話Aの遅延応答が
    会話Bの画面に誤って購読を張る」という stale response バグがあった。

    注意（テスト設計）: `GET /chat/turns/running` は nav.js（トップバーの「実行中」バッジ・全ページ
    共通ポーリング）と chat.js の resumeRunningTurn の**両方**が呼ぶため、ネットワーク越しに
    「openConversation(A) の応答だけを狙って遅延させる」手法は呼び出し順に依存して不安定になりやすい
    （実際に検証したところ、どちらの呼び出しが先に飛ぶかがタイミングに左右され、意図した1本を確実に
    補足できなかった）。EventSource が実際に張られたかどうかの生ネットワーク記録
    （records["turn_stream_urls"]）も、SSE 接続はエラー/再試行が絡むと非同期に状態が変わるため
    「一定時間後にチェック」という形では不安定だった。そこで resumeRunningTurn を直接呼び出し、
    その**同じ await チェーンの中で**（別途 sleep して後から見るのではなく）`_turnId`/`_es` の状態を
    読み取ることで、世代チェックの分岐そのものを決定的に検証する（openConversation からの実際の
    呼び出し経路や、subscribeTurn が実際に正しい URL で EventSource を開くことは他の e2e
    （test_chat_background_turn_survives_navigation_and_resumes_on_return 等）で確認済み）。
    """
    def handle_running(route):
        route.fulfill(content_type="application/json", body=json.dumps(
            {"turns": [{"turn_id": "t-a", "conversation_id": 101, "started_at": "2026-07-03T09:00:00+00:00"}]}))

    install_api_mocks(page)
    page.route("**/chat/turns/running", handle_running)
    page.goto(f"{web_base_url}/chat.html")

    # 会話101向けの応答が届いた時点では、画面はすでに別の会話（999）を見ている状況を作る。
    # e2e はテスト専用 seam（window.__sherpaChatTest・フェーズ6 S1）経由でのみ内部状態に触れる
    # （chat.js の module 化後も同じ形で触れられるようにするため・bare identifier の直接代入はしない）。
    page.evaluate("window.__sherpaChatTest.cid = 999; window.__sherpaChatTest.turnId = null; window.__sherpaChatTest.es = null")
    mismatched = page.evaluate(
        "(async () => { await window.__sherpaChatTest.resumeRunningTurn(101, []); "
        "return {turnId: window.__sherpaChatTest.turnId, es: !!window.__sherpaChatTest.es}; })()")
    assert mismatched == {"turnId": None, "es": False}, (
        "世代不一致（_cid が別会話）なのに購読を張ろうとした（世代チェック漏れ・"
        f"stale response バグの再発）: {mismatched}")

    # 対照実験: _cid が一致していれば正しく処理が進む（世代チェック自体が誤検知していないことの確認）。
    page.evaluate("window.__sherpaChatTest.cid = 101")
    matched = page.evaluate(
        "(async () => { await window.__sherpaChatTest.resumeRunningTurn(101, []); "
        "return {turnId: window.__sherpaChatTest.turnId, es: !!window.__sherpaChatTest.es}; })()")
    assert matched == {"turnId": "t-a", "es": True}, f"世代一致なのに処理が進んでいない: {matched}"


def test_resume_running_turn_does_not_hijack_new_send_while_start_post_pending(page, web_base_url):
    """`S.sending=true`（開始 POST 応答待ち中）のときに見つかった実行中ターンへ割り込んで
    再購読しない。割り込むと `subscribeTurn` が turnGen を進めてしまい、利用者が同じ会話で
    新しく送った開始 POST の応答が届いた時点でそれが世代不一致で破棄され（`send()` の turnGen
    契約）、しかも `S.sending` を解除する経路が無くなって送信不能のまま固着する事故になる。"""
    def handle_running(route):
        route.fulfill(content_type="application/json", body=json.dumps(
            {"turns": [{"turn_id": "turn-old", "conversation_id": 101,
                       "started_at": "2026-07-03T09:00:00+00:00"}]}))

    install_api_mocks(page)
    page.route("**/chat/turns/running", handle_running)
    page.goto(f"{web_base_url}/chat.html")

    page.evaluate(
        "window.__sherpaChatTest.cid = 101; window.__sherpaChatTest.turnId = null; "
        "window.__sherpaChatTest.es = null; window.__sherpaChatTest.sending = true")
    result = page.evaluate(
        "(async () => { await window.__sherpaChatTest.resumeRunningTurn(101, []); "
        "return {turnId: window.__sherpaChatTest.turnId, es: !!window.__sherpaChatTest.es}; })()")
    assert result == {"turnId": None, "es": False}, (
        f"S.sending 中でも実行中ターンへ割り込んで再購読した（新しい送信を巻き込む事故）: {result}")

    # 対照実験: S.sending が false なら通常どおり再購読される（ガード自体が誤検知していない確認）。
    page.evaluate("window.__sherpaChatTest.sending = false")
    resumed = page.evaluate(
        "(async () => { await window.__sherpaChatTest.resumeRunningTurn(101, []); "
        "return {turnId: window.__sherpaChatTest.turnId, es: !!window.__sherpaChatTest.es}; })()")
    assert resumed == {"turnId": "turn-old", "es": True}, f"S.sending=false なのに再購読されない: {resumed}"


def test_resume_running_turn_does_not_override_existing_subscription_with_older_turn(page, web_base_url):
    """`GET /chat/turns/running` の応答待ちの間に別経路（利用者自身の送信の完了）で既に別の
    turnId へ購読が確立していた場合（応答順の逆転）、ここで見つかった（おそらく旧い）実行中
    ターンへ張り替えない（turnId 照合）——張り替えると新しい送信の購読を巻き戻して孤児化させる。"""
    def handle_running(route):
        route.fulfill(content_type="application/json", body=json.dumps(
            {"turns": [{"turn_id": "turn-old", "conversation_id": 101,
                       "started_at": "2026-07-03T09:00:00+00:00"}]}))

    install_api_mocks(page)
    page.route("**/chat/turns/running", handle_running)
    page.goto(f"{web_base_url}/chat.html")

    # 既に別の（新しい）turnId へ購読済みの状態を模す。
    page.evaluate(
        "window.__sherpaChatTest.cid = 101; window.__sherpaChatTest.turnId = 'turn-new'; "
        "window.__sherpaChatTest.es = {}; window.__sherpaChatTest.sending = false")
    result = page.evaluate(
        "(async () => { await window.__sherpaChatTest.resumeRunningTurn(101, []); "
        "return {turnId: window.__sherpaChatTest.turnId}; })()")
    assert result == {"turnId": "turn-new"}, f"既存購読（別 turnId）を旧ターンへ張り替えてしまった: {result}"


def test_resume_running_turn_duplicate_response_for_same_turn_id_is_a_noop(page, web_base_url):
    """同一 turnId への遅延/重複した `GET /chat/turns/running` 応答（ポーリングの二重発火・
    応答が遅れて後から届く等）で、既に購読中の同じターンへ何度も「再開」処理を走らせない——
    素通りさせると `.thinking` プレースホルダーや右ペインのターン積み上げ（`startFlow`）を
    毎回二重に作ってしまい、`subscribeTurn` も無駄に既存の EventSource を閉じて張り直す
    （受信中のストリームを一瞬でも切る実害がある）。"""
    stream_calls = {"n": 0}

    def handle_stream(route):
        stream_calls["n"] += 1
        return   # fulfill しない＝保留のまま（実行中ターンを模す・再購読の回数だけ数える）

    def handle_running(route):
        route.fulfill(content_type="application/json", body=json.dumps(
            {"turns": [{"turn_id": "turn-dup", "conversation_id": 101,
                       "started_at": "2026-07-03T09:00:00+00:00"}]}))

    install_api_mocks(page)
    page.route("**/chat/turns/turn-dup/stream?**", handle_stream)
    page.route("**/chat/turns/running", handle_running)
    page.goto(f"{web_base_url}/chat.html")

    page.evaluate(
        "window.__sherpaChatTest.cid = 101; window.__sherpaChatTest.turnId = null; "
        "window.__sherpaChatTest.es = null; window.__sherpaChatTest.sending = false")
    first = page.evaluate(
        "(async () => { await window.__sherpaChatTest.resumeRunningTurn(101, []); "
        "return {turnId: window.__sherpaChatTest.turnId, es: !!window.__sherpaChatTest.es}; })()")
    assert first == {"turnId": "turn-dup", "es": True}, f"1回目の再購読が失敗している: {first}"
    # EventSource のコンストラクタ呼び出しが page.evaluate() の Promise 解決として観測できる時点と、
    # ブラウザが実際にネットワーク要求を発行し Playwright の route がそれを捕捉する時点は非同期
    # （別マクロタスク）にずれるため、素通しの即時 assert ではなく届くまで短間隔でポーリングする。
    _wait_until(lambda: stream_calls["n"] >= 1, page=page, message="1回目の GET stream が届かなかった")
    assert stream_calls["n"] == 1, f"1回目で GET stream が期待どおり1回発行されていない: {stream_calls['n']}"

    thinking_n = page.locator(".thinking").count()
    fturn_n = page.locator("#flow details.fturn").count()

    # 同じ turnId への重複応答（2回目の呼び出し）は no-op になり、GET stream も再発行されない。
    second = page.evaluate(
        "(async () => { await window.__sherpaChatTest.resumeRunningTurn(101, []); "
        "return {turnId: window.__sherpaChatTest.turnId, es: !!window.__sherpaChatTest.es}; })()")
    assert second == {"turnId": "turn-dup", "es": True}
    # no-op であること（何も起きないこと）は「待てば真になる」条件では確認できないため、
    # 誤って再購読していれば十分観測できる猶予だけ待ってから件数を確定させる。
    page.wait_for_timeout(150)
    assert stream_calls["n"] == 1, (
        f"重複応答で EventSource が不要に閉じられ再購読された（GET stream が2回発行された）: {stream_calls['n']}")
    assert page.locator(".thinking").count() == thinking_n, "重複応答で .thinking プレースホルダーが二重に追加された"
    assert page.locator("#flow details.fturn").count() == fturn_n, "重複応答で右ペインのターンが二重に追加された"


def _wait_until(predicate, timeout_ms=5000, interval_ms=20, page=None, message="条件が満たされなかった"):
    """`predicate()` が真になるまで `page.wait_for_timeout` で短間隔ポーリングする（EventSource の
    ネットワーク要求発行が JS の Promise 解決と非同期にずれるタイミングを確定的に待つ）。"""
    import time
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        assert time.monotonic() < deadline, message
        page.wait_for_timeout(interval_ms)
