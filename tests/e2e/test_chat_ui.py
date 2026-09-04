from __future__ import annotations

import re

import pytest

import mock_api
from mock_api import IMPACT_ANSWER, PLAN_ANSWER, PLAN_TRACE, install_api_mocks


def test_chat_streams_answer_with_explicit_scope(page, web_base_url):
    """背景実行（覗き窓方式）: 送信は POST /chat/turns（JSON body）で開始し、
    GET /chat/turns/{turn_id}/stream を購読する。範囲パネルは折りたたみツリー（既定=トップ階層のみ）
    のため、深い階層のフォルダを選ぶには祖先のトグル（▸）を順に開く必要がある。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#messages")).to_contain_text("気になること")
    page.locator("#kbtoggle").click()
    expect(page.locator("#scopesel")).to_be_visible()

    page.locator("#scopebtn").click()
    page.locator("#scopepanel [data-toggle='4期']").click()
    page.locator("#scopepanel [data-toggle='4期/02_設計']").click()
    page.locator("#scopepanel [data-scope='4期/02_設計/01_基本設計']").click()
    expect(page.locator("#scopelabel")).to_have_text("01_基本設計")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#flow")).to_contain_text("グラフ検索")
    expect(page.locator("#flow")).to_contain_text("MCP graph_neighbors")
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator("#messages")).to_contain_text("TAXCALC")
    expect(page.locator("#messages")).to_contain_text("出典")
    expect(page.locator("#rt")).to_contain_text("完了")

    assert records["turn_starts"], "POST /chat/turns が呼ばれていない"
    body = records["turn_starts"][-1]
    assert body["knowledge"] is True
    assert body["personal"] is False
    assert body["scope_paths"] == ["4期/02_設計/01_基本設計"]
    assert records["turn_stream_urls"], "GET /chat/turns/{turn_id}/stream が呼ばれていない"


def test_scope_panel_tree_collapses_to_top_level_by_default(page, web_base_url):
    """折りたたみツリー化（実環境指摘 2026-09-02）: 既定はトップ階層（depth 0）のみ表示し、
    子を持つ行のトグル（▸）をクリックすると直下の子だけが段階的に現れる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#scopebtn").click()

    expect(page.locator("#scopepanel [data-scope='4期']")).to_be_visible()
    expect(page.locator("#scopepanel [data-scope='4期/02_設計']")).to_have_count(0)
    expect(page.locator("#scopepanel [data-scope='4期/02_設計/01_基本設計']")).to_have_count(0)

    page.locator("#scopepanel [data-toggle='4期']").click()
    expect(page.locator("#scopepanel [data-scope='4期/02_設計']")).to_be_visible()
    expect(page.locator("#scopepanel [data-scope='4期/03_開発']")).to_be_visible()
    expect(page.locator("#scopepanel [data-scope='4期/02_設計/01_基本設計']")).to_have_count(0)   # 孫はまだ


def test_scope_panel_toggle_click_does_not_change_selection(page, web_base_url):
    """▸（開閉）のクリックと行本体（選択）のクリックは別クリック領域（要件3）——
    トグルを押しても選択状態・チップ表示は変わらない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#scopebtn").click()

    page.locator("#scopepanel [data-toggle='4期']").click()
    expect(page.locator("#scopepanel [data-scope='4期']")).not_to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#scopelabel")).to_have_text("全体")


def test_scope_panel_restores_deep_selection_visible_on_reopen(page, web_base_url):
    """選択済みフォルダの祖先は自動展開される（要件2）——深い範囲を選んだ会話を開き直しても
    選択行が折りたたみに隠れず見える（GET /conversations/117 の scope.source=="explicit"）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=117")
    expect(page.locator("#messages")).to_contain_text("TAXCALC")
    expect(page.locator("#scopelabel")).to_have_text("01_ソース")

    page.locator("#scopebtn").click()
    target = page.locator("#scopepanel [data-scope='4期/03_開発/01_ソース']")
    expect(target).to_be_visible()
    expect(target).to_have_class(re.compile(r"\bon\b"))


def test_scope_panel_filter_narrows_and_selects_then_reverts_to_tree(page, web_base_url):
    """絞り込み入力（要件4）: ラベル部分一致（大小文字無視）したフォルダだけを祖先パス付きの
    平坦表示にし、クリックで選択できる。空にすればツリー表示（折りたたみ済み）へ戻る。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#scopebtn").click()

    page.locator("#scopefilter").fill("ソース")
    target = page.locator("#scopepanel [data-scope='4期/03_開発/01_ソース']")
    expect(target).to_be_visible()
    expect(target).to_contain_text("03_開発")   # 祖先パスが前置きされる
    expect(page.locator("#scopepanel [data-scope='4期/02_設計']")).to_have_count(0)   # 不一致は出ない
    target.click()
    expect(page.locator("#scopelabel")).to_have_text("01_ソース")

    page.locator("#scopefilter").fill("")
    expect(page.locator("#scopepanel [data-scope='4期']")).to_be_visible()   # ツリー表示へ戻る
    expect(page.locator("#scopepanel [data-scope='4期/03_開発/01_ソース']")).to_be_visible()   # 選択済みの祖先は自動展開


def test_chat_source_download_completes_via_shared_blob_helper(page, web_base_url):
    """UI フィードバック3（2026-07-03・原本DL「フリーズ」修正）の回帰確認: 出典リンククリックで
    実際にファイルダウンロードが完了する（共通ヘルパ Sherpa.downloadBlob への一本化後も壊れていない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("出典")
    with page.expect_download() as dl_info:
        page.locator("[data-dl]").first.click()
    download = dl_info.value
    assert download.suggested_filename == "税計算仕様書.md"
    assert records["doc_downloads"], "/documents/download が呼ばれていない"


_AUTHOR_ANSWER = {
    "lens": "author",
    "headline": "消費税率の一覧をExcelにまとめました。",
    "route": {"path": ["文書を検索", "資料を作成"]},
    "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
    "data": {"citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                            "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                "download_url": "/documents/download?world=w1&rel=x"}],
    "created_files": [{"name": "消費税率一覧.xlsx", "download_url": "/workspace/files/501/download"}],
}


def test_created_files_card_displays_with_working_download_link(page, web_base_url):
    """P1-c（Codex 強化計画 Phase1）: lens=author の回答に created_files があると
    「📎 作成したファイル」カードが表示され、DL リンク（href=download_url）から実際に DL が完了する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, stream_events=[
        {"type": "node", "id": "codex", "kind": "think", "status": "done",
         "label": "Codex が調べる", "detail": "調べて回答をまとめました"},
        {"type": "answer", "conversation_id": 101, "message": {"answer": _AUTHOR_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率の一覧をExcelにまとめて")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("資料を作成")           # LENS_LABEL チップ（P1-a）
    expect(page.locator("#messages")).to_contain_text("作成したファイル")
    link = page.locator(".created-files a[data-dl]").first
    expect(link).to_have_text("消費税率一覧.xlsx")
    assert link.get_attribute("href") == "/workspace/files/501/download"
    expect(page.locator(".created-files-link")).to_have_text("マイワークスペースで開く")

    with page.expect_download() as dl_info:
        link.click()
    download = dl_info.value
    assert download.suggested_filename == "消費税率一覧.xlsx", \
        f"保存名がカードのファイル名と一致しない: {download.suggested_filename}"
    assert records["workspace_downloads"] == ["/workspace/files/501/download"], \
        "/workspace/files/{id}/download が呼ばれていない"


def test_created_files_card_absent_when_no_created_files(page, web_base_url):
    """回帰: created_files が無い通常の回答ではカードが出ない（qa 等への誤爆が無い）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator(".created-files")).to_have_count(0)


def test_created_files_card_displays_on_history_load(page, web_base_url):
    """P1-c: 履歴ロード（answer JSONB 保存経由・mock 会話106）でも「作成したファイル」カードが表示される。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(106)")

    expect(page.locator("#messages")).to_contain_text("作成したファイル")
    link = page.locator(".created-files a[data-dl]").last
    expect(link).to_have_text("消費税率一覧.xlsx")
    assert link.get_attribute("href") == "/workspace/files/501/download"


def test_chat_stop_button_stops_streaming_and_restores_ui(page, web_base_url):
    """UI フィードバック1（2026-07-03・背景実行移行後も踏襲）: 送信中は送信ボタンが「■ 停止」に
    切り替わり、クリックで POST /chat/turns/{turn_id}/stop を叩く。サーバが {"type":"stopped"} を
    返すと「（停止しました）」を表示し、ボタンは通常の送信状態に戻る。

    GET /chat/turns/{turn_id}/stream を明示的に**保留**（fulfill しない）ことで、実際のサーバの
    ブロッキング処理中と同じ「まだ応答が来ていない」状態を再現する（mock_api の既定 SSE モックは
    全イベントを一括配信するため、素の応答では途中状態を作れない＝
    参照: [[feedback_e2e_sse_mock_timing]]。stop 側から**保留中の GET を後から fulfill**
    することで、タイマー等に頼らず決定的にテストする）。POST /chat/turns（開始）自体は
    install_api_mocks の既定モック（turn_id="turn-101" を即返す）に任せる。
    """
    import json
    import re

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route   # fulfill せず保留＝送信中の状態を作る

    def handle_stop(route):
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in [
                {"type": "node", "id": "understand", "kind": "think", "status": "active",
                 "label": "質問を理解", "detail": "確認しています"},
                {"type": "stopped", "conversation_id": 101},
            ])
            stream_route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)
        route.fulfill(content_type="application/json", body=json.dumps({"ok": True}))

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    send_btn = page.locator("#send")
    expect(send_btn).to_have_class(re.compile(r"\bstopping\b"))   # 送信中＝停止ボタンに切替
    expect(send_btn).to_have_text("■")

    send_btn.click()   # 同じボタンをもう一度クリック＝停止

    expect(page.locator(".stopped-note")).to_contain_text("停止しました")
    expect(send_btn).not_to_have_class(re.compile(r"\bstopping\b"))   # 通常の送信状態に復帰
    expect(send_btn).to_have_text("↑")
    expect(page.locator("#rt")).to_contain_text("停止しました")


def test_chat_enter_key_double_submit_during_pending_start_is_rejected(page, web_base_url):
    """開始 POST（`POST /chat/turns`）の応答待ち中は `send()` の再入を拒否する（`S.sending`）。
    `$('input')` の Enter キー押下は `sendOrStop()`（`S.es` ガード込み）ではなく `send()` を直接
    呼ぶため、応答が届く前（`S.es` はまだ null）に連続で Enter を押すと、以前は開始 POST が2本
    飛び1本目のターンが孤児化した（購読されず・停止もできない）。開始 POST を保留したまま
    2回 Enter を押しても POST は1本のまま（=Aのターンだけが開始）で、保留を解いた後は
    そのAのターン（turn_id）に正しく購読されることを固定する。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    starts: list = []

    def handle_turn_start(route):
        starts.append(route)   # 応答は保留＝「まだ届いていない」状態を作る

    page.route("**/chat/turns", handle_turn_start)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#input").press("Enter")
    _wait_until(lambda: len(starts) >= 1, page=page, message="1回目の開始 POST が届かなかった")

    # 応答が届いていない間にもう一度 Enter を押す（バグ再現条件: S.es はまだ null）。
    page.locator("#input").fill("2回目の質問（届いてはいけない）")
    page.locator("#input").press("Enter")
    page.wait_for_timeout(150)   # 誤って2本目が飛ぶ場合に反映される猶予
    assert len(starts) == 1, f"開始 POST が複数回飛んでいる（二重送信ガードが効いていない）: {len(starts)}"

    # 保留していた（Aの）開始 POST の応答を返し、そのターンへ正しく購読されることを確認する。
    starts[0].fulfill(content_type="application/json",
                      body=json.dumps({"turn_id": "turn-A", "conversation_id": 101}))
    expect(page.locator("#rt")).to_contain_text("リアルタイム")


def test_chat_new_conversation_during_pending_start_clears_sending_guard(page, web_base_url):
    """開始 POST（`POST /chat/turns`）の応答待ち中（`S.sending=true`）に「新しいチャット」へ
    移動した場合、`S.sending` は `unsubscribeTurn()` 側で必ず解除される。解除しないと、旧 POST が
    応答するまで（応答しなければ恒久的に）新しい画面で送信できない無言拒否になる。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    starts: list = []

    page.route("**/chat/turns", lambda route: starts.append(route))   # 応答は保留のまま
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#input").press("Enter")
    _wait_until(lambda: len(starts) >= 1, page=page, message="1回目の開始 POST が届かなかった")

    # 旧 POST の応答が届かないまま「新しいチャット」へ移動する。
    page.locator("#newbtn").click()
    expect(page.locator("#messages")).to_contain_text("ようこそ")

    # 新しい画面で送信できる（S.sending が解除されている＝無言拒否されない）ことを確認する。
    page.locator("#input").fill("新しい会話での質問")
    page.locator("#input").press("Enter")
    _wait_until(lambda: len(starts) >= 2, page=page,
                message="新しい画面からの開始 POST が送れていない（S.sending が解除されていない）")


def test_chat_send_start_post_timeout_resets_sending_and_allows_resend(page, web_base_url):
    """開始 POST（`POST /chat/turns`）が既定の締切（30秒・`Sherpa.api` の `timeoutMs`）を超えて
    応答しない場合、`S.sending` を解除しエラー表示する（締切が無いと `S.sending` が永久に
    解除されず送信不能になっていた）。締切超過後は再送できる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}

    page.route("**/chat/turns", lambda route: pending.__setitem__("route", route))   # 応答は保留のまま
    page.goto(f"{web_base_url}/chat.html")
    page.clock.install()

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#input").press("Enter")
    _wait_until(lambda: "route" in pending, page=page, message="開始 POST が届かなかった")

    page.clock.fast_forward(31000)   # 30秒の締切を超過させる
    expect(page.locator(".thinking")).to_contain_text("タイムアウト")
    expect(page.locator("#send")).to_be_enabled()

    # 締切超過後は再送できる（S.sending が解除されている）。
    pending.clear()
    page.locator("#input").fill("再送の質問")
    page.locator("#input").press("Enter")
    _wait_until(lambda: "route" in pending, page=page,
                message="締切超過後の再送 POST が送れていない（S.sending が解除されていない）")


def test_chat_stale_start_post_completion_does_not_reset_sending_for_newer_generation(page, web_base_url):
    """A（旧会話・応答保留中）→ 会話遷移 → B（新しい会話・応答保留中）→ A の遅延応答到着、という
    順序でも、A の遅延応答は B のための `S.sending` を解除しない（世代照合を `S.sending` の解除
    より先に行う）。解除してしまうと Enter キー（disabled ボタンを経由しない）で C が送信され、
    B がまだ応答待ちのまま C と衝突して B が孤児化する（購読されない）事故になっていた。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    starts: list = []
    b_stream_hits: list = []

    page.route("**/chat/turns", lambda route: starts.append(route))   # 応答は保留のまま
    # Bの turn_id（"turn-B"）宛の stream GET だけを個別に捕捉する——「#rt がリアルタイムになった」
    # だけでは B が実際に購読された証拠として弱い（無関係な理由で同じ文言になる余地を排除しない）
    # ため、B 固有の GET が実際に届いたことで購読を直接確認する。
    def handle_b_stream(route):
        b_stream_hits.append(route.request)
        route.fallback()   # install_api_mocks 側の既定 SSE ハンドラへ委譲する（本テストは捕捉だけ行う）

    page.route("**/chat/turns/turn-B/stream?**", handle_b_stream)
    page.goto(f"{web_base_url}/chat.html")

    # A: 応答保留のまま送信。
    page.locator("#input").fill("Aの質問")
    page.locator("#input").press("Enter")
    _wait_until(lambda: len(starts) >= 1, page=page, message="Aの開始POSTが届かなかった")

    # 会話を切り替える（Aへの関心を手放す＝世代が進む）。
    page.locator("#newbtn").click()
    expect(page.locator("#messages")).to_contain_text("ようこそ")

    # B: 新しい画面で送信。応答保留のまま。
    page.locator("#input").fill("Bの質問")
    page.locator("#input").press("Enter")
    _wait_until(lambda: len(starts) >= 2, page=page, message="Bの開始POSTが届かなかった")

    # Aの遅延応答がここで届く（stale）。
    starts[0].fulfill(content_type="application/json",
                      body=json.dumps({"turn_id": "turn-A", "conversation_id": 101}))
    page.wait_for_timeout(150)   # Aの応答処理が（誤って）S.sending を解除する場合の猶予

    # C: Bがまだ応答待ちのまま Enter を押しても送信されない（S.sending がまだ立っている）はず。
    page.locator("#input").fill("Cの質問（届いてはいけない）")
    page.locator("#input").press("Enter")
    page.wait_for_timeout(150)
    assert len(starts) == 2, f"Bの応答待ち中にCが送信されている（S.sending が誤って解除された）: {len(starts)}"

    # Bの応答を返し、B自身の stream GET が実際に届く（＝正しく購読される・孤児化していない）ことを
    # 確認する。
    starts[1].fulfill(content_type="application/json",
                      body=json.dumps({"turn_id": "turn-B", "conversation_id": 102}))
    _wait_until(lambda: len(b_stream_hits) >= 1, page=page,
                message="Bのターン（turn-B）へ購読（GET stream）されていない＝孤児化している")


def _wait_until(predicate, timeout_ms=5000, interval_ms=20, page=None, message="条件が満たされなかった"):
    """`predicate()` が真になるまで `page.wait_for_timeout` で短間隔ポーリングする（この
    ファイル内の停止フロー系テストで、Python 側ハンドラが route を保留し終えるタイミングを
    確定的に待つために使う共通ヘルパ）。"""
    import time
    deadline = time.monotonic() + timeout_ms / 1000
    while not predicate():
        assert time.monotonic() < deadline, message
        page.wait_for_timeout(interval_ms)


def _settle(page):
    """保留していた route を解放した直後、その Promise 継続（fetch 応答→JSON 解析→後続処理）が
    実際に最後まで走り切るのを待ってから戻る。JS のイベントループは、その時点までにキュー
    済みの Promise 継続（マイクロタスク）を必ず先に消化してから次のマクロタスクへ進む仕様を
    利用し、ページ内で本物の fetch 往復（既定モックが応答する無害な GET）を実際に待つことで、
    『まだ再開処理が終わっていないのに assert が通ってしまう』空振りを防ぐ（何も起きない
    ことを検証する no-op 系のテストで、解放直後に assert するだけでは決定的とは言えないため）。"""
    page.evaluate("async () => { try { await fetch('/chat/turns/running'); } catch (e) {} }")


def test_chat_stop_before_first_response_shows_stopped_not_connection_error(page, web_base_url):
    """最初の応答が届く前に「■ 停止」を押した場合の表示契約: SSE の接続断（onerror）が停止 POST
    の応答より先に届く競合が起き得るが、その場合も「接続エラー。もう一度お試しください。」ではなく
    「（停止しました）」を表示する（本テストは停止 POST の応答を保留し、onerror が先に発火した
    ことを確認してから応答を返すことで、この順序を確定的に再現する）。一方、停止していない別
    ターンで起きた本物の接続断は引き続き「接続エラー」のまま変わらない（対照ケースとして同じ
    テスト内で検証する）。
    """
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route   # fulfill せず保留＝まだ最初の応答が届いていない状態を作る

    def handle_stop(route):
        held["stop_route"] = route   # 応答も保留＝onerror が先に発火したことを確認してから返す
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.abort()   # stopped イベント配送前に接続断（onerror が先に発火する競合の再現）

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    send_btn = page.locator("#send")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")

    expect(send_btn).to_have_text("■")
    send_btn.click()   # 最初の応答（node 等）が来るより前に停止

    # onerror が先に発火した（＝停止 POST の応答より前に接続断が検知された）ことを、暫定表示を
    # 見て確定させてから、停止 POST の応答（成功）を返す。
    expect(page.locator(".thinking")).to_contain_text("停止しました")
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": True}))
    _settle(page)   # 成功応答の Promise 継続（acknowledged 判定・以降の no-op 分岐）を待ってから確認する

    expect(page.locator(".thinking")).to_contain_text("停止しました")
    expect(page.locator(".thinking")).not_to_contain_text("接続エラー")
    expect(page.locator("#rt")).to_contain_text("停止しました")
    expect(send_btn).to_have_text("↑")

    # 対照ケース: 停止操作をしていない別ターンで接続が切れた場合は、引き続き「接続エラー」の
    # まま（上の「停止起因は停止しました表示」に読み替える処理が、停止していないケースにまで
    # 誤って及んでいないこと・前ターンの停止状態を次のターンへ持ち越していないことの確認）。
    page.locator("#input").fill("2件目の質問です。")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="2件目の GET /chat/turns/*/stream が届かなかった")

    stream_route2 = pending.pop("route")
    stream_route2.abort()   # 停止操作なしの、純粋な接続断

    thinking2 = page.locator(".thinking").last
    expect(thinking2).to_contain_text("接続エラー")
    expect(thinking2).not_to_contain_text("停止しました")
    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(send_btn).to_have_text("↑")


def test_chat_stop_failure_after_new_chat_does_not_clobber_new_screen(page, web_base_url):
    """停止 POST が保留中に onerror が先着（暫定「停止しました」）した後、「新しいチャット」で
    別画面へ遷移した場合、旧ターンの遅延応答（失敗）が後から返っても遷移先の #rt・思考枠を
    上書きしてはならない（会話遷移＝ history.js の unsubscribeTurn 経由で世代を無効化する）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route

    def handle_stop(route):
        held["stop_route"] = route
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.abort()

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    send_btn = page.locator("#send")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")

    send_btn.click()
    _wait_until(lambda: "stop_route" in held, page=page, message="停止 POST が届かなかった")
    expect(page.locator(".thinking")).to_contain_text("停止しました")   # onerror 先着（S.es は既に null）

    # 「新しいチャット」へ遷移する（停止 POST の応答はまだ保留中のまま）。
    page.locator("#newbtn").click()
    expect(page.locator("#messages")).to_contain_text("ようこそ")   # 新しい画面（welcome）に切り替わった
    expect(page.locator("#rt")).to_contain_text("待機中")

    # ここで旧ターンの停止 POST の遅延応答（失敗）を返す。会話遷移で世代は無効化済みのはずで、
    # 新しい画面の #rt・思考枠に触れてはならない。
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    _settle(page)

    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(page.locator("#rt")).not_to_contain_text("接続エラー")
    expect(page.locator(".thinking")).to_have_count(0)   # welcome 画面に思考枠が紛れ込んだりしない


def test_chat_stop_correction_still_works_when_open_conversation_fetch_fails(page, web_base_url):
    """openConversation() は会話取得（GET /conversations/{id}）が成功するまで画面遷移を確定
    しない（世代の無効化を含む）。取得が失敗すれば今の画面のままなので、暫定「停止しました」
    表示は停止 POST の結果確定で引き続き正しく訂正されなければならない。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route

    def handle_stop(route):
        held["stop_route"] = route
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.abort()

    def handle_open_fail(route):
        route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.route("**/conversations/999", handle_open_fail)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")

    page.locator("#send").click()   # 停止（POST は保留のまま）
    _wait_until(lambda: "stop_route" in held, page=page, message="停止 POST が届かなかった")
    expect(page.locator(".thinking")).to_contain_text("停止しました")   # onerror 先着（S.es は既に null）

    # 会話取得に失敗する openConversation() を呼ぶ（画面はまだ遷移していない想定）。
    page.evaluate("(id) => window.__sherpaChatTest.openConversation(id).catch(() => {})", 999)
    expect(page.locator(".thinking")).to_contain_text("停止しました")   # 失敗＝今の画面のまま

    # 停止 POST 自体は失敗だったと判明する。取得失敗で世代は無効化されていないはずなので、
    # このターン自身の訂正がそのまま効くはず。
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    _settle(page)

    expect(page.locator(".thinking")).to_contain_text("接続エラー。もう一度お試しください。")
    expect(page.locator("#rt")).to_contain_text("接続エラー。もう一度お試しください。")


def test_chat_stop_post_failure_after_onerror_corrects_rt_and_thinking_to_connection_error(page, web_base_url):
    """onerror が停止 POST の応答より先に発火して「停止しました」を暫定表示した後、停止 POST
    自体が失敗（停止起因ではない本物の接続断だった）と判明した場合は、#rt と思考枠の両方を
    「接続エラー。もう一度お試しください。」へ訂正する。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route

    def handle_stop(route):
        held["stop_route"] = route
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.abort()

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    send_btn = page.locator("#send")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")

    send_btn.click()

    expect(page.locator(".thinking")).to_contain_text("停止しました")   # onerror 先着の暫定表示
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))

    expect(page.locator(".thinking")).to_contain_text("接続エラー。もう一度お試しください。")
    expect(page.locator("#rt")).to_contain_text("接続エラー。もう一度お試しください。")


def test_chat_stop_failure_after_next_turn_started_does_not_clobber_next_turn_ui(page, web_base_url):
    """前ターンの停止 POST が失敗したという遅延応答が、次のターンが既に開始済み（開始 POST
    発行後・購読確立前）のタイミングで返っても、次のターンの表示（送信ボタン・#rt・aria-busy・
    思考枠）を上書きしない。次のターン自体は、その後も普通に進行できる。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route

    def handle_stop(route):
        held["stop_route"] = route
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.abort()

    turn_starts_seen = {"n": 0}

    def handle_turn_start(route):
        turn_starts_seen["n"] += 1
        if turn_starts_seen["n"] == 1:
            route.fallback()   # 1件目（1ターン目の開始）は install_api_mocks の既定応答へ委譲する
            return
        held["turn_start_route"] = route   # 2件目（2ターン目の開始）だけ保留する

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.route("**/chat/turns", handle_turn_start)
    page.goto(f"{web_base_url}/chat.html")

    send_btn = page.locator("#send")

    # 1件目: 送信→最初の応答が来る前に停止（onerror が先着・停止 POST は保留のまま）。
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="1件目の GET /chat/turns/*/stream が届かなかった")

    send_btn.click()
    expect(page.locator(".thinking")).to_contain_text("停止しました")   # onerror 先着＝1ターン目は決着済み
    expect(send_btn).to_have_text("↑")

    # 2件目: 開始 POST を保留したまま送信する（世代は開始 POST 発行前に進む＝購読確立前でも
    # 1ターン目の遅延応答から守られているはずの状態を作る）。
    page.locator("#input").fill("2件目の質問です。")
    page.locator("#send").click()
    _wait_until(lambda: "turn_start_route" in held, page=page, message="2件目の開始 POST が届かなかった")

    expect(send_btn).to_have_text("■")
    expect(page.locator("#messages")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#rt")).to_contain_text("リアルタイム")
    thinking2 = page.locator(".thinking").last

    # ここで 1ターン目の停止 POST の遅延応答（失敗）を返す。2ターン目はまだ開始 POST の
    # 応答待ち（購読前）＝世代が変わっている以上、この応答は 2ターン目の表示に触れてはならない。
    # _settle() でこの遅延応答の Promise 継続が実際に走り切るのを待ってから確認する
    # （解放直後にすぐ assert するだけでは、まだ継続処理が終わっていないだけで偶然通る恐れがある）。
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    _settle(page)

    expect(send_btn).to_have_text("■")
    expect(page.locator("#messages")).to_have_attribute("aria-busy", "true")
    expect(page.locator("#rt")).to_contain_text("リアルタイム")
    expect(thinking2).not_to_contain_text("接続エラー")
    expect(thinking2).not_to_contain_text("停止しました")

    # 2ターン目自身は、これ以降も普通に進行できる（開始 POST を解放→自身の接続断で
    # 独立して「接続エラー」になることを確認）。
    held.pop("turn_start_route").fulfill(
        content_type="application/json",
        body=json.dumps({"turn_id": "turn-101", "conversation_id": 101}),
    )
    _wait_until(lambda: "route" in pending, page=page, message="2件目の GET /chat/turns/*/stream が届かなかった")
    pending.pop("route").abort()

    expect(thinking2).to_contain_text("接続エラー")
    expect(page.locator("#rt")).to_contain_text("待機中")
    expect(send_btn).to_have_text("↑")


def test_chat_stop_delayed_failure_from_older_turn_does_not_erase_newer_turns_pending_correction(
    page, web_base_url,
):
    """世代不一致で no-op になる分岐は、共有状態（onerror が先着した事実・思考枠への参照）に
    一切触れてはならない。旧ターンの停止応答を保留したまま次のターンを送信してすぐに停止し、
    次のターンの onerror が先着（暫定「停止しました」表示）した後で旧ターンの遅延応答（失敗）
    が返っても、次のターンの暫定表示への参照を消してはならない。次のターン自身の停止 POST が
    最終的に失敗と判明したとき、#rt・思考枠の両方が正しく「接続エラー」へ訂正されることまで
    確認する。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held_stops: list = []   # 到着順（[0]=1件目=旧ターン、[1]=2件目=新ターン）。応答は両方保留する。

    def handle_turn_stream(route):
        pending["route"] = route

    def handle_stop(route):
        held_stops.append(route)
        stream_route = pending.pop("route", None)
        if stream_route is not None:
            stream_route.abort()   # 該当ターンの onerror を先着させる

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    send_btn = page.locator("#send")

    # 旧ターン: 送信→停止（onerror 先着・停止 POST は保留のまま）。
    page.locator("#input").fill("1件目の質問です。")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="1件目の GET /chat/turns/*/stream が届かなかった")
    send_btn.click()
    _wait_until(lambda: len(held_stops) == 1, page=page, message="1件目の停止 POST が届かなかった")
    expect(page.locator(".thinking")).to_contain_text("停止しました")
    expect(send_btn).to_have_text("↑")   # onerror が S.es を閉じた＝次のターンを送信できる

    # 新ターン: 送信→最初の応答が来る前に停止（この onerror も先着・停止 POST は保留のまま）。
    page.locator("#input").fill("2件目の質問です。")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="2件目の GET /chat/turns/*/stream が届かなかった")
    send_btn.click()
    _wait_until(lambda: len(held_stops) == 2, page=page, message="2件目の停止 POST が届かなかった")

    thinking2 = page.locator(".thinking").last
    expect(thinking2).to_contain_text("停止しました")
    expect(page.locator("#rt")).to_contain_text("停止しました")

    # 旧ターンの遅延応答（失敗）を返す。世代が変わっている以上 no-op のはずで、
    # 新ターンが onerror 先着で保持している参照を消してはならない。
    held_stops[0].fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    _settle(page)

    expect(thinking2).to_contain_text("停止しました")   # 新ターンの暫定表示のまま（参照が生きている）
    expect(page.locator("#rt")).to_contain_text("停止しました")

    # 新ターン自身の停止 POST が失敗と判明する。#rt・思考枠の両方が「接続エラー」へ訂正される。
    held_stops[1].fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    _settle(page)

    expect(thinking2).to_contain_text("接続エラー。もう一度お試しください。")
    expect(page.locator("#rt")).to_contain_text("接続エラー。もう一度お試しください。")


def test_chat_stop_failure_after_partial_answer_removed_thinking_still_corrects_rt(page, web_base_url):
    """部分回答（answer_delta）が既に届いて思考枠が消えた後に停止し、onerror が停止 POST の
    応答より先に発火（暫定「停止しました」）、その後停止 POST 自体が失敗と判明した場合。
    「onerror が先着したか」の判定は思考枠の DOM 接続状態と混同してはならず、思考枠が既に
    無くても #rt は必ず「接続エラー。もう一度お試しください。」へ訂正される。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route

    def handle_stop(route):
        held["stop_route"] = route   # 応答は保留（先に停止 POST を確定させ、後から接続を切る）

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    send_btn = page.locator("#send")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")

    send_btn.click()   # 停止（POST は保留のまま＝ stopState は 'pending' で固定される）
    _wait_until(lambda: "stop_route" in held, page=page, message="停止 POST が届かなかった")

    # 停止要求が確定する前に、部分回答が届く（ありうる正常な競合）。render.js の
    # ensureAnswerCard() が思考枠（.thinking の祖先）を DOM から取り除く。
    delta_event = {"type": "answer_delta", "text": "回答の一部です"}
    stream_route = pending.pop("route")
    stream_route.fulfill(
        status=200, headers={"Content-Type": "text/event-stream"},
        body=f'data: {json.dumps(delta_event, ensure_ascii=False)}\n\n',
    )
    expect(page.locator(".thinking")).to_have_count(0)   # 思考枠は既に消えている

    # 応答本体を全て配送し終えた接続はここで自然に終端し、EventSource が onerror を発火する
    # （stopState は既に 'pending' なので暫定的に「停止しました」表示になる）。
    expect(page.locator("#rt")).to_contain_text("停止しました")

    # 停止 POST 自体は失敗だったと判明する。思考枠は既に無いが、#rt は必ず訂正される。
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    _settle(page)

    expect(page.locator("#rt")).to_contain_text("接続エラー。もう一度お試しください。")
    expect(page.locator(".thinking")).to_have_count(0)   # 思考枠が新たに作られたりはしない


def test_chat_stop_failure_after_answer_already_landed_is_ignored(page, web_base_url):
    """同一ターン（世代は変わらない）で、停止 POST の応答より先に本物の回答（answer 終端
    イベント）が届いて完了した場合、その後に届く停止 POST の失敗応答はもう関係ない
    （#rt が「完了」から巻き戻らない）。turnConcluded ガードの直接的な回帰確認。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}
    held: dict = {}

    def handle_turn_stream(route):
        pending["route"] = route

    def handle_stop(route):
        held["stop_route"] = route

    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.route("**/chat/turns/*/stop", handle_stop)
    page.goto(f"{web_base_url}/chat.html")

    send_btn = page.locator("#send")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    _wait_until(lambda: "route" in pending, page=page, message="GET /chat/turns/*/stream が届かなかった")

    send_btn.click()   # 停止（POST は保留のまま）
    _wait_until(lambda: "stop_route" in held, page=page, message="停止 POST が届かなかった")

    # 停止が確定する前に、ターンが正常に完走して本物の回答が届く。
    answer_event = {"type": "answer", "conversation_id": 101, "message": {"answer": mock_api.IMPACT_ANSWER}}
    stream_route = pending.pop("route")
    stream_route.fulfill(
        status=200, headers={"Content-Type": "text/event-stream"},
        body=f'data: {json.dumps(answer_event, ensure_ascii=False)}\n\n',
    )
    expect(page.locator("#rt")).to_contain_text("完了")
    expect(send_btn).to_have_text("↑")

    # 停止 POST の遅延応答（失敗）が今ごろ返っても、このターンは終端イベント（answer）で
    # 既に決着済み＝ no-op のはずで、「完了」表示を巻き戻してはならない。
    held.pop("stop_route").fulfill(content_type="application/json", body=json.dumps({"ok": False}))
    _settle(page)

    expect(page.locator("#rt")).to_contain_text("完了")
    expect(page.locator("#rt")).not_to_contain_text("待機中")
    expect(page.locator("#rt")).not_to_contain_text("接続エラー")


def test_chat_qa_citations_collapsed_by_default_and_toggle(page, web_base_url):
    """UI フィードバック2（2026-07-03）: QA 回答の引用（該当箇所）カードは既定で折りたたみ・
    件数だけ見える見出しをクリックすると開閉する。"""
    import json

    from playwright.sync_api import expect

    qa_answer = {
        "lens": "qa", "headline": "端数処理は切り捨てです。",
        "route": {"path": ["資料"]},
        "summary": {"total": 2},
        "data": {"citations": [
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md", "span": [10, 12], "quote": "端数は切り捨てとする。"},
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md", "span": [20, 21], "quote": "1円未満切り捨て。"},
        ]},
        "sources": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                     "download_url": "/documents/download?world=w1&rel=x"}],
    }

    def handle_turn_stream(route):
        body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in [
            {"type": "answer", "conversation_id": 101, "message": {"answer": qa_answer}},
        ])
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)

    install_api_mocks(page)
    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("端数処理は？")
    page.locator("#send").click()

    cites_h = page.locator(".cites-h")
    cites_body = page.locator(".cites-body")
    expect(cites_h).to_contain_text("該当箇所 (2)")
    expect(cites_body).to_be_hidden()                       # 既定は折りたたみ（[hidden] で非表示）

    cites_h.click()
    expect(cites_body).to_be_visible()
    expect(cites_body).to_contain_text("端数は切り捨てとする。")
    expect(cites_h).to_have_attribute("aria-expanded", "true")

    cites_h.click()                                          # もう一度クリックで再び折りたたむ
    expect(cites_body).to_be_hidden()
    expect(cites_h).to_have_attribute("aria-expanded", "false")


def test_chat_prefills_question_from_graph_bridge(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.add_init_script("localStorage.setItem('sherpa-ask', '消費税率を変えたい。影響は？')")
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#input")).to_have_value("消費税率を変えたい。影響は？")
    assert page.evaluate("localStorage.getItem('sherpa-ask')") is None


def test_chat_turn_stack_restores_all_turns_and_button_expands_scrolls(page, web_base_url):
    """UIフィードバック（2026-07-03）: 会話ロードで右ペインに**全ターン**が時系列で積み上げ表示され、
    最新ターンだけ展開・他は畳まれる。trace の無いターンは見出しに「（記録なし）」。
    回答カードの「この回答の思考の流れ」ボタンは、右ペインの該当ターンを展開する（別表示への
    切替ではなく既存の積み上げ表示に統合）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#convlist")).to_contain_text("消費税率の相談")
    # 行クリックの誤爆修正（2026-07・S4 Part2）後は行全体クリックで開けることを他テストで固定済み
    # （test_chat_conversation_row_click_opens_not_renames）なので、ここは素直に行全体をクリックする。
    page.locator("[data-open='101']").click()

    # mock は3ターン（記録なし→trace有→trace有＝最新）。全ターンのヘッダが時系列で並ぶ。
    turns = page.locator(".fturn")
    expect(turns).to_have_count(3)
    expect(turns.nth(0)).to_contain_text("消費税率を変えたい")
    expect(turns.nth(0)).to_contain_text("（記録なし）")
    expect(turns.nth(1)).to_contain_text("対象範囲はどこまでですか")
    expect(turns.nth(2)).to_contain_text("影響はどこまで及びますか")
    expect(page.locator("#rt")).to_contain_text("過去の記録")

    # 最新（3番目）だけ展開。trace 有りだが最新でない2番目は畳まれている。
    expect(turns.nth(2)).to_have_js_property("open", True)
    expect(turns.nth(1)).to_have_js_property("open", False)

    # 回答カードの専用ボタン（trace 有りの2ターン分）。1つ目（最新でない方＝畳まれている）を
    # クリックすると、そのターン（#fturn-1）が展開される。
    trace_btns = page.locator("[data-showtrace]")
    expect(trace_btns).to_have_count(2)
    trace_btns.first.click()
    expect(page.locator("#fturn-1")).to_have_js_property("open", True)


def test_chat_turn_stack_includes_user_only_turn_without_answer(page, web_base_url):
    """RV再検証 HIGH#1: clarify/停止等で assistant 応答が無い user だけのターンも積み上げから
    消えない（trace:null＝「（記録なし）」として1件目に残る）。mock 会話102参照。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(102)")

    turns = page.locator(".fturn")
    expect(turns).to_have_count(2)
    expect(turns.nth(0)).to_contain_text("それ、直して")
    expect(turns.nth(0)).to_contain_text("（記録なし）")
    expect(turns.nth(1)).to_contain_text("消費税率の変更点を直して")
    expect(turns.nth(1)).to_have_js_property("open", True)


def test_chat_turn_stack_renders_for_own_conversation_with_no_trace_at_all(page, web_base_url):
    """RV再検証 HIGH#2: 自分の会話は全ターン trace 無しでも積み上げる（placeholder のままにしない）。
    受領共有だけが placeholder のまま（別テストで確認）。mock 会話103参照。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(103)")

    turns = page.locator(".fturn")
    expect(turns).to_have_count(1)
    expect(turns.nth(0)).to_contain_text("こんにちは")
    expect(turns.nth(0)).to_contain_text("（記録なし）")
    expect(page.locator("#flow")).not_to_contain_text("質問すると、考えた流れがここに流れます")


def test_chat_received_share_keeps_flow_pane_as_placeholder(page, web_base_url):
    """受領共有（route/trace を返さない既存 posture）は、trace が無い旨の積み上げも出さず、
    右ペインは既定のプレースホルダのまま（既存 posture の維持を明示的に固定）。mock 会話104参照。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(104)")

    expect(page.locator(".fturn")).to_have_count(0)
    expect(page.locator("#flow")).to_contain_text("質問すると、考えた流れがここに流れます")


def test_chat_history_restores_answered_and_operable_question_cards(page, web_base_url):
    """S1（ask_user-improvements.md）: 保存された確認カード（answer.question）を履歴から復元する。
    1つ目（回答済み＝それ以降の user に同じ確認ID）は選択内容つきで disabled、2つ目（未回答＝最新）は
    操作可能のまま。mock 会話107参照。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(107)")

    cards = page.locator(".askcard")
    expect(cards).to_have_count(2)

    # 1つ目: 回答済み → answered クラス・disabled・選択内容（影響範囲）が見える。
    answered = cards.nth(0)
    expect(answered).to_have_class(re.compile(r"\banswered\b"))
    expect(answered).to_contain_text("回答済み")
    expect(answered).to_contain_text("影響範囲")
    expect(answered.locator("[data-ask-submit]")).to_be_disabled()
    # 選択済みオプション（label=影響範囲）がチェック状態で復元される。
    assert answered.locator("[data-qopt][data-label='影響範囲']").is_checked()

    # 2つ目: 未回答の最新 → 操作可能（disabled でない・answered クラスなし）。
    operable = cards.nth(1)
    expect(operable).not_to_have_class(re.compile(r"\banswered\b"))
    expect(operable.locator("[data-ask-submit]")).to_be_enabled()
    expect(operable).not_to_contain_text("回答済み")


def test_chat_sanitized_share_clarify_shows_placeholder_not_blank(page, web_base_url):
    """RV Med（Codex 2026-07-07）: sanitized 共有側の確認カード（answer={"lens":"clarify"}・question
    無し）は受領共有でも空白/崩れ表示にならず、既存のプレースホルダ「（確認のやり取り）」を出す。
    mock 会話108参照（受領共有 origin・store 側で clarify が最小形に縮退した想定）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(108)")

    expect(page.locator(".askcard")).to_have_count(0)   # 対話カードは出さない（read-only 共有）
    expect(page.locator("#messages")).to_contain_text("（確認のやり取り）")


def test_chat_history_load_renders_markdown_and_escapes_xss(page, web_base_url):
    """UIフィードバック（2026-07-03・AI回答のMarkdown表示）: 履歴ロードでも太字/インラインコード/
    箇条書き/コードブロックがMD整形され、XSS注入（<img onerror>・[link](javascript:)）は
    エスケープされたまま実行されずに残る（mock 会話105参照）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    # RV LOW（2026-07-03）: 「実行されない」ことを直接検証する sentinel（onerror が発火すれば 1/2 になる）。
    page.evaluate("window.__xss = 0")
    page.evaluate("window.__sherpaChatTest.openConversation(105)")

    headline = page.locator(".headline").last
    expect(headline.locator("strong")).to_have_count(2)          # **結論**・**6件**
    expect(headline.locator("code", has_text="list_docs")).to_have_count(1)
    expect(headline.locator("ul li")).to_have_count(2)
    expect(headline.locator("pre.md-code")).to_have_count(2)     # 正常フェンス＋脱出試行フェンス
    expect(headline.locator("pre.md-code").first).to_contain_text("path_prefix=4期更改")

    # XSS: 生タグ/リンク構文がエスケープされた「見えるだけの文字列」として残り、実要素化しない。
    expect(headline).to_contain_text("<img src=x onerror=alert(1)>")
    expect(headline).to_contain_text("[link](javascript:alert(1))")
    # code fence 内の </pre> 脱出試行も「見えるだけ」でフェンス内に残る（RV LOW）。
    expect(headline.locator("pre.md-code").last).to_contain_text("</code></pre><img src=x onerror=window.__xss=1>")
    # esc() 済み実体の再解釈が起きない: ソース中の「&lt;img ...&gt;」は文字どおり見え（タグ化も
    # 二重デコードもされない）、「&amp;amp;」は「&amp;」と見える。
    expect(headline).to_contain_text("&lt;img src=x onerror=window.__xss=2&gt;")
    expect(headline).to_contain_text("&amp;amp;")
    expect(headline.locator("img")).to_have_count(0)
    expect(headline.locator("a")).to_have_count(0)
    assert page.evaluate("window.__xss") == 0, "XSS ペイロードが実行された（onerror 発火）"


def test_chat_answer_renders_markdown_after_stream_completes(page, web_base_url):
    """UIフィードバック（2026-07-03）: ストリーミング完了（answer 確定）後、生Markdownが
    整形されて表示される（実例: **結論**・`list_docs`・**6件**）。"""
    from playwright.sync_api import expect

    md_answer = {**IMPACT_ANSWER, "headline": "**結論**: `list_docs` で確認した結果、**6件**でした。"}
    install_api_mocks(page, stream_events=[
        {"type": "answer", "conversation_id": 101, "message": {"answer": md_answer}},
    ])
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("4期更改資料はどのくらいある？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    headline = page.locator(".headline").last
    expect(headline.locator("strong")).to_have_count(2)
    expect(headline.locator("code")).to_have_count(1)
    expect(headline).to_contain_text("list_docs")
    expect(headline).not_to_contain_text("**")


def test_chat_copy_button_preserves_raw_markdown_not_rendered_html(page, web_base_url):
    """UIフィードバック（2026-07-03）: 書き出し（コピー）は表示用のMD整形を経由せず、生テキストの
    ままクリップボードへ渡る（**/`` 等の記法が失われない）。"""
    from playwright.sync_api import expect

    context = page.context
    try:
        context.grant_permissions(["clipboard-read", "clipboard-write"])
    except Exception:
        pytest.skip("このブラウザ/環境ではクリップボード権限を付与できない")

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(105)")

    page.locator(".headline").last.locator("xpath=ancestor::div[contains(@class,'a-body')]")\
        .locator("[data-copy]").click()
    expect(page.locator("#toast")).to_contain_text("コピーしました")
    copied = page.evaluate("navigator.clipboard.readText()")
    assert "**結論**" in copied and "`list_docs`" in copied and "**6件**" in copied, copied


def test_chat_conversation_row_click_opens_not_renames(page, web_base_url):
    """S4 Part2: 短いタイトルの会話行を行全体クリックしても、改名ダイアログではなく会話が開くこと。

    2026-07 修正前は行クリック（バウンディングボックス中心）が hover で現れる改名ボタン（✎）と
    幾何学的に重なり、開くつもりで誤って改名ダイアログ（prompt()）が起動していた。
    """
    from playwright.sync_api import expect

    install_api_mocks(page)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#convlist")).to_contain_text("消費税率の相談")
    page.locator("[data-open='101']").click()   # 行全体クリック（既定＝バウンディングボックス中心）

    expect(page.locator("#conv-title")).to_have_text("消費税率の相談")   # 会話が開いた
    assert dialogs == [], f"行クリックで改名ダイアログが誤って開いた（クリック誤爆の再発）: {dialogs}"


def test_chat_conversation_row_click_opens_at_minimum_sidebar_width(page, web_base_url):
    """RV5 MEDIUM: 左ペインを最小幅（200px・ドラッグリサイズの下限）まで狭めても、行全体クリックの
    幾何中心が改名ボタン（✎）等と重ならず、会話が正しく開くこと（elementFromPoint で実測して固定）。

    2026-07 の 22px ボタン化（既定サイドバー幅 264px 向け）だけでは Lmin=200px（行内容幅 約176px）
    で再び中心がボタン域に届いてしまっていた＝コンテナクエリで狭幅時は rename/share を隠す対策を追加。
    """
    from playwright.sync_api import expect

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

    # 狭幅では改名(✎)・共有(🔗) ボタンが隠れ、行の幾何中心が実際にタイトル側に当たることを確認する。
    cx, cy = box["x"] + box["width"] / 2, box["y"] + box["height"] / 2
    hit = page.evaluate(f"document.elementFromPoint({cx},{cy})?.className || ''")
    assert "cact" not in hit, f"狭幅でも行中心がボタンに当たっている（誤爆再発）: {hit!r}"

    row.click()   # 行全体クリック（既定＝バウンディングボックス中心）
    expect(page.locator("#conv-title")).to_have_text("消費税率の相談")
    assert dialogs == [], f"狭幅サイドバーで行クリックが改名ダイアログを誤って開いた: {dialogs}"


def test_chat_conversation_list_shows_local_time_not_raw_utc(page, web_base_url):
    """S3: サーバは timezone 付き UTC（`+00:00`）を返す。表示は端末ロケール（実質 JST）に変換されること
    （素朴な文字列 slice だと UTC の時刻をそのまま見せてしまい 9 時間ズレる）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    # mock の updated_at は "2026-07-01T09:00:00+00:00"（UTC）＝ JST では 18:00（同日・+9h）。
    row = page.locator("[data-open='101']")
    expect(row).to_contain_text("2026-07-01 18:00")
    expect(row).not_to_contain_text("09:00")


def test_chat_can_share_current_conversation(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    page.locator("#sharebtn").click()
    expect(page.locator("#share-overlay")).to_be_visible()
    page.locator("#share-invitees").fill("sato tanaka")
    page.locator("#share-days").select_option("3")
    page.locator("#share-submit").click()

    expect(page.locator("#share-result")).to_be_visible()
    expect(page.locator("#share-url-val")).to_contain_text("/share/conversations/share-token-101")
    assert records["share_create"][-1]["invitee_user_ids"] == ["sato", "tanaka"]
    assert "expires_at" in records["share_create"][-1]


def test_chat_share_dialog_autocomplete_pick_and_chip_and_free_text_combine(page, web_base_url):
    """バッチ2・5番（2026-07-03）: 入力中にドロップダウン候補（デバウンス200ms）→クリックで確定し
    チップ化。既存のカンマ区切り手入力（チップ化しない自由入力）と組み合わせても両方送られる
    （後方互換）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    page.locator("#sharebtn").click()
    expect(page.locator("#share-overlay")).to_be_visible()

    invitees = page.locator("#share-invitees")
    invitees.fill("tana")
    suggest = page.locator("#share-invitee-suggest")
    expect(suggest).to_be_visible()
    expect(suggest).to_contain_text("田中 花子")
    assert records["users_suggest"][-1] == "tana"

    page.locator("[data-pick-invitee]", has_text="田中 花子").first.click()
    expect(suggest).to_be_hidden()
    chips = page.locator("#share-invitee-chips .invitee-chip")
    expect(chips).to_have_count(1)
    expect(chips.first).to_contain_text("田中 花子")
    expect(invitees).to_have_value("")   # 検索クエリだった分は入力欄から取り除かれる

    # 自由入力（カンマ区切り・チップ化しない従来どおりの手入力）も併用できる。
    invitees.fill("freeuser1, freeuser2")
    page.locator("#share-submit").click()

    expect(page.locator("#share-result")).to_be_visible()
    sent = set(records["share_create"][-1]["invitee_user_ids"])
    assert sent == {"tanaka", "freeuser1", "freeuser2"}


def test_chat_share_dialog_autocomplete_enter_confirms_highlighted_and_chip_removable(page, web_base_url):
    """Enter でハイライト中の候補を確定（チップ化）でき、チップの✕クリックで取り消せる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    page.locator("#sharebtn").click()
    invitees = page.locator("#share-invitees")
    invitees.fill("yamada")
    suggest = page.locator("#share-invitee-suggest")
    expect(suggest).to_contain_text("山田 太郎")

    invitees.press("ArrowDown")   # 1件目をハイライト
    invitees.press("Enter")

    chips = page.locator("#share-invitee-chips .invitee-chip")
    expect(chips).to_have_count(1)
    expect(chips.first).to_contain_text("山田 太郎")
    expect(suggest).to_be_hidden()

    chips.first.locator("button").click()   # ✕ で取り消し
    expect(chips).to_have_count(0)


def test_chat_sends_personal_workspace_toggle_to_stream(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#personaltoggle").click()
    page.locator("#input").fill("個人メモも見て影響を確認して")
    page.locator("#send").click()

    expect(page.locator("#rt")).to_contain_text("完了")
    assert records["turn_starts"][-1]["personal"] is True


def test_chat_uploads_file_to_personal_workspace(page, web_base_url, tmp_path):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    upload = tmp_path / "chat-note.md"
    upload.write_text("個人メモです\n", encoding="utf-8")
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#chat-file-input").set_input_files(str(upload))

    expect(page.locator("#chat-upload-status")).to_contain_text("chat-note.md")
    expect(page.locator("#chat-upload-status")).to_contain_text("個人ワークスペース")
    assert records["workspace_uploads"][-1]["filename"] == "chat-note.md"


def test_chat_flow_shows_query_chip_and_detail_history(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    # A3: ツール detail の「クエリ」がチップとして分離描画される。
    expect(page.locator("#flow .fchip")).to_contain_text("消費税率")

    # A2: 同一 id の think ステップが detail 変化を履歴として蓄積し、直近を表示。
    expect(page.locator("#flow")).to_contain_text("再検索: 税率 改定")
    hist = page.locator("#flow .fhist")
    expect(hist).to_be_visible()
    expect(hist).to_contain_text("履歴 2")

    # クリックで過去 detail を開閉（<button> ＝ Enter/Space も同経路・aria-expanded で状態管理）。
    expect(hist).to_have_attribute("aria-expanded", "false")
    hist.click()
    expect(hist).to_have_attribute("aria-expanded", "true")
    expect(page.locator("#flow .fhist-list")).to_contain_text("検索: 消費税率")
    hist.click()
    expect(hist).to_have_attribute("aria-expanded", "false")


def test_chat_impact_item_shows_analyzer_provenance(page, web_base_url):
    """影響結果の各項目に担当アナライザ名を出す（§7 裁定2の受入条件＝影響分析の根拠表示で参照
    できるようにする）。コード分（TAXCALC・analyzer=cobol）だけに出て、請求機能（analyzer キー無し）
    には出ない。内部名 `cobol` はそのまま出さず表示ラベル `COBOL` にする。
    ソース正典化（K12）で「確実/要確認」判定・フィルタチップ・`data-sure` は機構ごと撤去済み＝
    行は IMPACT_ANSWER の並び順（`.ilist li` の nth）で一意に絞る。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    rows = page.locator("#messages .ilist li")
    expect(rows).to_have_count(2)

    taxcalc_row = rows.nth(0)
    expect(taxcalc_row).to_contain_text("TAXCALC")
    expect(taxcalc_row).to_contain_text("解析: COBOL")

    billing_row = rows.nth(1)
    expect(billing_row).to_contain_text("請求機能")
    expect(billing_row).not_to_contain_text("解析:")


def test_world_selector_persists_last_choice(page, web_base_url):
    """複数フォルダ運用の磨き（2026-07-10）: 資料フォルダの選択を端末ローカル（localStorage）に記憶し、
    次回チャットを開いた時に復元する（毎回ソート先頭に戻らない）。削除済み保存値は先頭へフォールバック。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/world-options", lambda route: route.fulfill(
        content_type="application/json",
        body='{"worlds": ["w1", "w2"], "labels": {"w1": "販売管理", "w2": "在庫管理"}}'))
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator(".verselect")).to_be_visible()       # 複数フォルダならセレクタが出る
    expect(page.locator("#version")).to_have_value("w1")     # 初回は先頭
    page.locator("#version").select_option("w2")

    page.goto(f"{web_base_url}/chat.html")                   # 再訪＝前回選択が復元される
    expect(page.locator("#version")).to_have_value("w2")

    # 保存値の資料フォルダが削除された場合は先頭へフォールバック（存在しない値を強要しない）
    page.route("**/world-options", lambda route: route.fulfill(
        content_type="application/json",
        body='{"worlds": ["w1"], "labels": {"w1": "販売管理"}}'))
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#version")).to_have_value("w1")


def test_world_selector_conversation_beats_saved_choice(page, web_base_url):
    """deep-link（?conv=）で開いた会話の world は、端末保存の前回選択より優先される
    （選択肢の読込順に依らず、保存値が会話の world を上書きしない・RV 指摘の回帰ガード）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/world-options", lambda route: route.fulfill(
        content_type="application/json",
        body='{"worlds": ["w1", "w2"], "labels": {"w1": "販売管理", "w2": "在庫管理"}}'))
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("localStorage.setItem('sherpa-world', 'w2')")   # 前回選択＝w2 を模す

    page.goto(f"{web_base_url}/chat.html?conv=102")               # 会話102の直近回答は world=w1
    expect(page.locator("#messages")).to_contain_text("TAXCALC")  # 会話が開けている
    expect(page.locator("#version")).to_have_value("w1")          # 保存値(w2)でなく会話の world


# ===== リファクタリング計画フェーズ6 S1: 分割前の未カバー面スモーク pin =====
# S2〜S8 の分割（common.js 分離・module 化・chat/*.js 切り出し）で壊れやすいが、
# これまで e2e で1本も触れていなかった面を先に固定する（危険地雷2「既存静的ゲートが非再帰 glob」
# と対になる「新規ゲート」側の担保）。

def test_export_menu_opens_with_expected_items(page, web_base_url):
    """書き出しメニュー（#exportbtn）の開閉と、項目（Markdown/テキスト/JSON/PDF）の存在を固定する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#exportmenu")).to_be_hidden()
    page.locator("#exportbtn").click()
    expect(page.locator("#exportmenu")).to_be_visible()
    items = page.locator("#exportmenu [data-exp]")
    expect(items).to_have_count(4)
    expect(items).to_have_text(["Markdown", "テキスト", "JSON", "PDF（印刷）"])


def test_font_menu_opens_and_applies_selected_size(page, web_base_url):
    """文字サイズメニュー（#fontbtn）の開閉と適用（`--chatfont` カスタムプロパティの変化を1点確認）を固定する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#fontmenu")).to_be_hidden()
    page.locator("#fontbtn").click()
    expect(page.locator("#fontmenu")).to_be_visible()
    page.locator("#fontmenu [data-fs='大']").click()
    expect(page.locator("#fontmenu")).to_be_hidden()   # 選択後は自動で閉じる
    applied = page.evaluate("document.documentElement.style.getPropertyValue('--chatfont')")
    assert applied == "16.5px", f"文字サイズ「大」が適用されていない: {applied}"


def test_brain_menu_opens_and_shows_model_note_for_llm_provider(page, web_base_url):
    """頭脳メニュー（#brainbadge）の開閉と、実行構成の切替を固定する。

    4構成（2026-08-15・`sherpa/agent_constructs.py`）: メニューの項目はサーバが返す
    `constructs_available` から描画され、`data-exec` は構成id（agent 名ではない）。
    Codex(OpenAI) と Codex(Ollama) は同じ agent=codex で別項目として並ぶ。

    モデル名は個人設定に無い（管理者の使えるモデル一覧・選択中のクラウドプロバイダだけで決まる）。
    openai/gemini/ollama/codex はモデル欄自体を出さず案内文だけ（保存ボタンも無い＝ PUT しても
    孤立フィールドとして無視されるだけの偽の「保存しました」を出さない）。唯一の例外は Bedrock
    （実在確認済みモデルの専用機構が個人設定側にあり、自由入力 `<input>` のまま）。
    """
    from playwright.sync_api import expect

    # A7（クラウドプロバイダ排他選択）: openai と bedrock を同じセッションで両方保存できる
    # 実サーバの状態は無い（選択中でないクラウド系 agent の保存は 422）ため、本テストは
    # cloud_provider=bedrock に揃え、「モデル欄を持たない構成」の代表を openai ではなく
    # ollama（A7 対象外・showModelNote の判定は openai と同じ扱い）で確認する。cloud_provider=bedrock
    # なら実サーバの一覧も openai_only（provider≠openai）・gemini（provider≠gemini）を除いた
    # 4件（ollama_only/codex_openai/codex_ollama/bedrock）になる＝一覧漏れの偽緑を作らないよう
    # constructs_available・agent・construct_id もその状態に揃える。
    _bedrock_constructs = [c for c in mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS["constructs_available"]
                          if c["id"] not in ("openai_only", "gemini")]
    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "cloud_provider": "bedrock",
               "constructs_available": _bedrock_constructs, "agent": "ollama",
               "construct_id": "ollama_only"}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("#brainmenu")).to_be_hidden()
    page.locator("#brainbadge").click()
    expect(page.locator("#brainmenu")).to_be_visible()
    # cloud_provider=bedrock の実サーバ相当＝ollama_only/codex_openai/codex_ollama/bedrock の4件
    # （openai_only・gemini は A7 で一覧から外れる）。
    expect(page.locator("#brainmenu [data-exec]")).to_have_count(4)

    page.locator("#brainmenu [data-exec='ollama_only']").click()

    # 実際に切り替わること（＝PUT /settings が飛ぶこと）まで見る。属性名と読み出し側の
    # 食い違いで「クリックしても何も起きない」状態を作らないための固定（2026-08-15 実害）。
    assert records["settings_put"][-1]["agent"] == "ollama"
    assert records["settings_put"][-1]["codex_model_provider"] is None

    # ollama はモデル欄自体を持たない（保存できないフィールドの偽の入力欄を出さない・openai と同型）。
    expect(page.locator("#bm-modelinput")).to_have_count(0)
    expect(page.locator("#bm-modelsave")).to_have_count(0)
    expect(page.locator(".bm-model")).to_contain_text("管理画面")
    # 接続テストはモデル欄が無くても引き続き提供される（キー疎通の確認は依然有効）。
    expect(page.locator("#bm-modeltest")).to_be_visible()

    # Bedrock はモデルカタログの対象外（実在確認済みモデルの専用機構が別にある）＝自由入力の
    # <input> のまま（唯一モデル欄と保存ボタンが残る）。メニューは開いたまま（クリックでは閉じない）
    # なので #brainbadge を再クリックしない（クリックすると開閉トグルで閉じてしまう）。
    page.locator("#brainmenu [data-exec='bedrock']").click()
    bedrock_field = page.locator("#bm-modelinput")
    expect(bedrock_field).to_be_visible()
    assert page.evaluate("document.getElementById('bm-modelinput').tagName") == "INPUT"
    assert bedrock_field.get_attribute("aria-label") == "モデル名"
    # Bedrock（唯一の個人設定モデル欄）は引き続き保存できる。
    bedrock_field.fill("jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    page.locator("#bm-modelsave").click()
    assert records["settings_put"][-1] == {"bedrock_model": "jp.anthropic.claude-haiku-4-5-20251001-v1:0"}

    # Codex 2構成は同じ agent=codex で、codex_model_provider だけが違う（メニューは開いたまま）。
    page.locator("#brainmenu [data-exec='codex_ollama']").click()
    expect(page.locator("#brainmenu [data-exec='codex_ollama']")).to_have_class(re.compile(r"\bon\b"))
    assert records["settings_put"][-1]["agent"] == "codex"
    assert records["settings_put"][-1]["codex_model_provider"] == "ollama"


def test_chat_html_static_assets_load_without_failure(page, web_base_url):
    """module 分割後の 404/構文全死の検知器（危険地雷10）: chat.html ロード時に同一オリジンの
    静的取得で requestfailed も 4xx/5xx 応答も発生しないことを固定する。favicon.ico はページが
    参照していないブラウザ既定の自動取得のため対象外にする。"""
    from urllib.parse import urlparse

    install_api_mocks(page)
    origin_host = urlparse(web_base_url).hostname
    failures = []

    def _on_request_failed(req):
        failures.append(f"requestfailed: {req.url} ({req.failure})")

    def _on_response(resp):
        if urlparse(resp.url).hostname != origin_host or urlparse(resp.url).path == "/favicon.ico":
            return
        if resp.status >= 400:
            failures.append(f"status {resp.status}: {resp.url}")

    page.on("requestfailed", _on_request_failed)
    page.on("response", _on_response)

    page.goto(f"{web_base_url}/chat.html")
    page.wait_for_load_state("networkidle")
    assert not failures, "\n".join(failures)
    # RV LOW（2026-07-14 フェーズ6）: エントリ module のロード成功マーカー（受け入れ基準の明示化）。
    # module graph は1ファイルの失敗で原子的に全死し pageerror に乗らないことがあるため、
    # エントリ末尾で公開される e2e seam の存在＝グラフ全体の評価完了を直接 assert する。
    assert page.evaluate("!!window.__sherpaChatTest"), (
        "window.__sherpaChatTest が無い＝chat.js module グラフの評価が失敗している"
        "（import 解決失敗・構文エラー等）"
    )


# ===================================================================================
# S4-e（複数プロファイル並用＋自動選択・UI 表示・E2E・§6.3）
# ===================================================================================

def test_chat_sub_planner_plan_and_usage_subs_render_additively(page, web_base_url):
    """S4-e: sub_planner ON 相当のライブ応答（計画ノード＝id="plan"・sub:{profile_id}: 名前空間化
    ノードを含む PLAN_TRACE 相当のイベント列）で、右ペインに計画/サブの各ノード label が流れ、
    回答カードに `usage_subs`（プロファイル別内訳）が additive 表示される（既存 usage_sub の
    折りたたみ流儀＝<details>+summary を踏襲・既定は折りたたみ）。"""
    from playwright.sync_api import expect

    events = list(PLAN_TRACE) + [
        {"type": "answer", "conversation_id": 101, "message": {"answer": PLAN_ANSWER}},
    ]
    install_api_mocks(page, stream_events=events)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#flow")).to_contain_text("進め方を計画")
    expect(page.locator("#flow")).to_contain_text("資料を検索（語句そのまま）")
    expect(page.locator("#flow")).to_contain_text("関係グラフを照会")
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")

    sub_meta = page.locator(".usage-sub-meta")
    expect(sub_meta).to_have_count(1)
    expect(sub_meta.locator("summary")).to_contain_text("下調べの使用量（2件）")
    expect(sub_meta.locator(".usage-detail")).to_be_hidden()   # 既定は折りたたみ

    sub_meta.locator("summary").click()
    expect(sub_meta.locator(".usage-detail")).to_be_visible()
    expect(sub_meta).to_contain_text("researcher: 入力 1,234 / 出力 567 トークン")
    expect(sub_meta).to_contain_text("reviewer: 入力 89 / 出力 45 トークン")


def test_chat_default_flow_has_no_plan_node_or_sub_usage_ui(page, web_base_url):
    """S4-e 回帰固定（S4 由来 UI の non-emission）: sub_planner 未設定の従来フロー（既定
    stream_events・usage_sub/usage_subs 無し）は計画ノード・プロファイル別使用量表示のいずれも
    出ないことを固定する。※本テストが主張するのは「S4 の additive 表示が既存フローに滲まない」
    ことだけ（OFF 時の byte-identical そのものは Python 側のピン＝tests/unit/test_sub_planner.py と
    test_sub_hybrid.py の S3 挙動不変ピンが担保する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    expect(page.locator("#flow")).not_to_contain_text("進め方を計画")
    expect(page.locator(".usage-sub-meta")).to_have_count(0)


def test_chat_history_restores_plan_node_and_sub_namespaced_trace(page, web_base_url):
    """S4-e: 保存済みターン（計画ノード＝id="plan"・sub:{profile_id}: 名前空間化ノードを含む
    PLAN_TRACE・usage_subs 付き PLAN_ANSWER）を、renderTurnStack/_renderTraceSteps が無改修で
    復元できることの固定（trace 描画は id を見ず label/detail/kind のみで組む＝プロファイル間の
    id 名前空間化は元々描画に影響しない）。mock 会話109参照。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(109)")

    turns = page.locator(".fturn")
    expect(turns).to_have_count(1)
    turn = turns.first
    expect(turn).to_have_js_property("open", True)
    expect(turn).to_contain_text("進め方を計画")
    expect(turn).to_contain_text("意図を特定")
    expect(turn).to_contain_text("資料を検索（語句そのまま）")
    expect(turn).to_contain_text("関係グラフを照会")

    sub_meta = page.locator(".usage-sub-meta")
    expect(sub_meta).to_have_count(1)
    sub_meta.locator("summary").click()
    expect(sub_meta).to_contain_text("researcher: 入力 1,234 / 出力 567 トークン")
    expect(sub_meta).to_contain_text("reviewer: 入力 89 / 出力 45 トークン")


def test_codex_construct_locks_knowledge_toggle_on(page, web_base_url):
    """Codex 構成は資料参照ON固定（決定 2026-08-15）。

    Codex CLI は read-only 実行でも自分で grep できるため「参照オフのつもりで KB を覗く」状態を
    作らない。トグルは見えるが押しても外れず、送信 body も knowledge=true になる
    （サーバ側の強制は `tests/unit/test_agent_constructs.py` が固定する）。
    """
    import json

    from playwright.sync_api import expect

    records = install_api_mocks(page)
    # /config が Codex を返す＝Codex構成でチャットを開いた状態
    page.route("**/config", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({"agent": "codex", "label": "Codex", "model": "gpt-5.5"})))
    page.goto(f"{web_base_url}/chat.html")

    kb = page.locator("#kbtoggle")
    expect(kb).to_have_attribute("aria-pressed", "true")     # 既定オフではなくON
    expect(kb).to_have_attribute("aria-disabled", "true")
    expect(kb).to_contain_text("オン")

    # aria-disabled のため Playwright の通常クリックは弾かれる（＝利用者も押せない）。
    # 「万一クリックが届いても外れない」ことまで見るため force で発火させる。
    kb.click(force=True)
    expect(kb).to_have_attribute("aria-pressed", "true")
    expect(kb).to_contain_text("オン")

    page.locator("#input").fill("税率の影響は？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("影響範囲分析")
    assert records["turn_starts"][-1]["knowledge"] is True   # 送信 body も ON


def test_chat_welcome_examples_are_concrete_and_load_only_into_input(page, web_base_url):
    """ウェルカム画面の質問例チップは、編集前提のテンプレ文（「【質問内容に置き換えて】」等）を
    含まない、そのまま送信して意味が通る文言・順序で固定されている（web/chat/state.js の
    DEFAULT_EXAMPLES と一致＝回帰ピン）。管理者設定 `chat_examples` が未設定（`GET /settings` の
    `chat_examples` が None・既定の mock 応答＝`mock_api.SETTINGS_RESP`）のときは、フロントの
    組み込み既定（この4例）がそのまま使われる契約も兼ねて確認する。アイコンは送信を示す記号では
    なく、クリックの実際の挙動どおり入力欄に読み込むだけで自動送信しない
    （POST /chat/turns は起きない）。4チップすべてで確認する。"""
    from playwright.sync_api import expect

    expected_examples = [
        "消費税率を変更すると、影響がありそうな箇所を教えてください。",
        "夜間バッチが異常終了しました。原因の候補を教えてください。",
        "消費税の端数処理の仕様を教えてください。",
        "登録されている資料の内容を要約した概要資料を作ってください。",
    ]

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

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
    """管理者設定 `chat_examples`（`GET /settings` の同名フィールド）が配列で返ると、組み込み既定
    ではなくその内容へ差し替わる（`web/chat/menus.js::loadConfig` の後追い反映・
    `web/chat/render.js::refreshWelcomeExamples`）。件数・文言・クリック時の入力欄反映まで確認する。"""
    from playwright.sync_api import expect

    custom_examples = ["在庫の締め処理はどうなっていますか？", "月次バッチの流れを教えてください。"]
    settings = {**mock_api.SETTINGS_RESP, "chat_examples": custom_examples}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/chat.html")

    chips = page.locator(".example")
    expect(chips).to_have_count(len(custom_examples))
    expect(chips.locator(".exq")).to_have_text(custom_examples)

    chips.nth(0).click()
    expect(page.locator("#input")).to_have_value(custom_examples[0])


def test_chat_welcome_examples_hidden_when_admin_disabled(page, web_base_url):
    """管理者設定 `chat_examples` が空配列（`enabled=false`、または明示的に空の `items`）で返ると、
    質問例ブロック自体が表示されない（組み込み既定へのフォールバックはしない＝非表示の意図を尊重する）。
    ウェルカム画面の他の案内（ようこそ見出し）は影響を受けない。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "chat_examples": []}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator(".example")).to_have_count(0)
    expect(page.locator(".headline")).to_contain_text("ようこそ Sherpa へ")


_EV0_ANSWER = {
    "lens": "qa",
    "headline": "確認しました。",
    "route": {"path": ["文書を検索"]},
    "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
    "data": {"citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                            "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [
        {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
         "download_url": "/documents/download?world=w1&rel=x"},
        {"doc_id": "<script>alert(1)</script>.md",
         "download_url": "/documents/download?world=w1&rel=y"},
    ],
    "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
}


def test_chat_sources_split_into_grounded_and_reference_when_verified(page, web_base_url):
    """EV-0（拡張設計 §4.4）: answer.sources_verified があると出典フッターが「根拠（精読済み）」/
    「参考（ヒットのみ）」の2区分になる（除外はしない＝両方の doc が引き続きリンクとして残る）。
    doc_id に HTML 特殊文字を含めても render.js の esc() 経由でエスケープされ、生の `<script>` 等が
    DOM に紛れ込まないことも併せて確認する。"""
    from playwright.sync_api import expect

    install_api_mocks(page, stream_events=[
        {"type": "answer", "conversation_id": 101, "message": {"answer": _EV0_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率は?")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("根拠（精読済み）")
    expect(page.locator("#messages")).to_contain_text("参考（ヒットのみ）")
    expect(page.locator("#messages")).to_contain_text("税計算仕様書.md")
    expect(page.locator("#messages")).to_contain_text("<script>alert(1)</script>.md")
    sources_html = page.locator(".sources").first.inner_html()
    assert "<script>alert(1)</script>" not in sources_html, "doc_id がエスケープされず生の script タグとして描画されている"
    assert "&lt;script&gt;" in sources_html


def test_chat_sources_stay_single_list_without_sources_verified(page, web_base_url):
    """`answer.sources_verified` を持たない回答（impact/troubleshoot 等）は従来どおり単一リストの
    出典表示のまま（byte-identical の根拠・EV-0 の区分見出しは出ない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    expect(page.locator("#messages")).to_contain_text("出典")
    assert "根拠（精読済み）" not in page.locator(".sources").first.inner_text()
    assert "参考（ヒットのみ）" not in page.locator(".sources").first.inner_text()
    assert records["turn_starts"], "POST /chat/turns が呼ばれていない"


_EV0_EXPORT_ANSWER = {
    "lens": "qa",
    "headline": "確認しました。",
    "route": {"path": ["文書を検索"]},
    "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
    "data": {"citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                            "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [
        {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
         "download_url": "/documents/download?world=w1&rel=x"},
        {"doc_id": "参考資料.md", "download_url": "/documents/download?world=w1&rel=y"},
    ],
    "sources_verified": ["4期/02_設計/01_基本設計/税計算仕様書.md"],
}


def test_export_menu_txt_and_md_split_grounded_and_reference_sources(page, web_base_url, tmp_path):
    """EXT-2/EV-0（拡張設計 §4.4）: 会話の書き出し（menus.js::_answerLines）も画面表示と同じ根拠/参考
    2区分になる（`sources_verified` にある doc_id は「根拠」、無い doc_id は「参考」・両方書き出す＝
    除外はしない）。TXT/Markdown 双方の書式を固定する。"""
    install_api_mocks(page, stream_events=[
        {"type": "answer", "conversation_id": 110, "message": {"answer": _EV0_EXPORT_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率は?")
    page.locator("#send").click()
    page.wait_for_selector("#messages .sources")

    page.locator("#exportbtn").click()
    with page.expect_download() as dl_info:
        page.locator("#exportmenu [data-exp='txt']").click()
    txt_path = tmp_path / "export.txt"
    dl_info.value.save_as(txt_path)
    txt = txt_path.read_text(encoding="utf-8")
    assert "根拠: 4期/02_設計/01_基本設計/税計算仕様書.md" in txt
    assert "参考: 参考資料.md" in txt
    assert "出典: " not in txt   # 2区分のときは単一「出典:」見出しを出さない

    page.locator("#exportbtn").click()
    with page.expect_download() as dl_info:
        page.locator("#exportmenu [data-exp='md']").click()
    md_path = tmp_path / "export.md"
    dl_info.value.save_as(md_path)
    md = md_path.read_text(encoding="utf-8")
    assert "**根拠:** 4期/02_設計/01_基本設計/税計算仕様書.md" in md
    assert "**参考:** 参考資料.md" in md


# ===== SC-6b: 調べ方ブロック（右ペイン下部の固定フッター・入力欄の要約チップ）=====
# docs/proposals/2026-08-29-調べ方ブロック.md §2・§7。範囲/参照するトグルの既存 e2e（#kbtoggle・
# #scopesel 系・#personaltoggle）はブロックへ移設しただけで挙動は変えていないため、上記の既存テスト
# （test_chat_streams_answer_with_explicit_scope・test_chat_sends_personal_workspace_toggle_to_stream 等）
# は無改修のまま新レイアウトでも通る前提（ID を変えていない・ブロックは既定オープン）。ここでは
# 新設した行（調べ方・探す対象）・チップ・開閉・復元・スラッシュ・SC-6d の再検索案内を追加で固定する。

def test_inquiry_block_lens_segment_sets_body_lens_and_grays_layer(page, web_base_url):
    """調べ方を「影響」に明示すると body.lens に反映され、探す対象（層フィルタ）は非適用として
    グレーアウト＋注記が出る（§2.5・§3.1）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#messages")).to_contain_text("気になること")
    page.locator("#kbtoggle").click()

    page.locator("#lens-seg [data-lens='impact']").click()
    expect(page.locator("#lens-seg [data-lens='impact']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#layer-seg [data-layer='docs']")).to_be_disabled()
    expect(page.locator("#layer-note")).to_be_visible()
    expect(page.locator("#inquiry-chip-label")).to_contain_text("影響")

    page.locator("#input").fill("消費税率を変えたい")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body.get("lens") == "impact"
    assert "layer" not in body   # 探す対象は既定（両方）のまま＝§4.2 裁定3「既定は省略」


def test_inquiry_block_layer_segment_sets_body_layer(page, web_base_url):
    """探す対象を「コードのみ」にすると body.layer に反映される（調べ方は既定＝送らない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()

    page.locator("#layer-seg [data-layer='code']").click()
    expect(page.locator("#layer-seg [data-layer='code']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("コードのみ")

    page.locator("#input").fill("TAXCALC の仕様は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body.get("layer") == "code"
    assert "lens" not in body


def test_inquiry_chip_opens_closed_right_pane_and_scrolls(page, web_base_url):
    """右ペインが閉じていても、入力欄の要約チップから調べ方ブロックへ必ず到達できる（§2.4）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#rightclose").click()
    expect(page.locator(".app")).to_have_class(re.compile(r"\brzero\b"))

    page.locator("#inquiry-chip").click()
    expect(page.locator(".app")).not_to_have_class(re.compile(r"\brzero\b"))
    expect(page.locator("#inquiry-body")).to_be_visible()


def test_inquiry_block_open_state_persists_per_conversation(page, web_base_url):
    """調べ方ブロックの開閉は会話ごとに localStorage で覚える（§2.1・§8 裁定12）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=101")
    expect(page.locator("#messages")).to_contain_text("消費税率")
    expect(page.locator("#inquiry-body")).to_be_visible()   # 記録が無い＝初回訪問は開く既定

    page.locator("#inquiry-head").click()                   # 畳む
    expect(page.locator("#inquiry-body")).to_be_hidden()

    page.goto(f"{web_base_url}/chat.html?conv=112")          # 別会話へ（開閉は会話ごと＝独立）
    expect(page.locator("#messages")).to_contain_text("消費税率")

    page.goto(f"{web_base_url}/chat.html?conv=101")          # 101 を開き直す
    expect(page.locator("#messages")).to_contain_text("消費税率")
    expect(page.locator("#inquiry-body")).to_be_hidden()     # 前回の閉状態を保つ


def test_inquiry_restore_explicit_lens_and_layer_from_history(page, web_base_url):
    """`lens_source=="explicit"` の直近回答を開き直すと、調べ方（影響）と探す対象（資料のみ）が
    両方とも復元される（§4.2・§4.3）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=112")
    expect(page.locator("#messages")).to_contain_text("消費税率")
    expect(page.locator("#lens-seg [data-lens='impact']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#layer-seg [data-layer='docs']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("影響")


def test_inquiry_restore_auto_lens_resets_but_layer_persists(page, web_base_url):
    """調べ方が自動判定だった回答を開き直すと調べ方は「自動」に戻るが、探す対象は復元される
    （明示のときだけ調べ方を復元する・§4.3 裁定4）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=113")
    expect(page.locator("#messages")).to_contain_text("消費税")
    expect(page.locator("#lens-seg [data-lens='auto']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#layer-seg [data-layer='code']")).to_have_class(re.compile(r"\bon\b"))


# ===== SC-6c: 調べる深さ（標準/深く/最大・調べ方ブロック §3.2）=====

_DEPTH_DEEP_ANSWER = {
    "lens": "qa", "headline": "該当箇所が1件見つかりました。",
    "route": {"path": ["文書を検索"]}, "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
             "depth_profile": "deep"},
    "duration_ms": 252000,   # 4分12秒（依頼の例示どおり）
    "data": {"citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                            "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                "download_url": "/documents/download?world=w1&rel=x"}],
}


def test_inquiry_depth_profile_deep_send_reflects_body_and_header(page, web_base_url):
    """調べる深さを「深く」にして送信すると `body.depth_profile` に反映され、回答ヘッダに
    「調べる深さ: 深く・所要 N分N秒」が表示される（§3.2・LOG-1a の duration_ms を流用）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, stream_events=[
        {"type": "answer", "conversation_id": 101, "message": {"answer": _DEPTH_DEEP_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()

    page.locator("#depth-seg [data-depth='deep']").click()
    expect(page.locator("#depth-seg [data-depth='deep']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("深く")

    page.locator("#input").fill("消費税率とは？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body.get("depth_profile") == "deep"
    expect(page.locator("#messages")).to_contain_text("調べる深さ: 深く・所要 4分12秒")


def test_inquiry_restore_depth_profile_from_history(page, web_base_url):
    """直近回答の `scope.depth_profile` を無条件に復元する（範囲/探す対象と同じ・調べ方の明示指定
    とは違い、調べる深さは自動判定という概念が無いため常に復元する・§4.3）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=115")
    expect(page.locator("#messages")).to_contain_text("消費税率")
    expect(page.locator("#depth-seg [data-depth='deep']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("深く")


def test_inquiry_new_conversation_resets_depth_profile_to_standard(page, web_base_url):
    """新規会話は常に「標準」（既存会話で深く/最大にしていても引き継がない・依頼の設計）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=115")
    expect(page.locator("#depth-seg [data-depth='deep']")).to_have_class(re.compile(r"\bon\b"))

    page.locator("#newbtn").click()
    expect(page.locator("#depth-seg [data-depth='standard']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("標準")


# ===== SC-6e: 検索経路トグル（grep/全文・ベクトル(ES)/グラフ・調べ方ブロック §3.6）=====

_TOOLS_GRAPH_ONLY_ANSWER = {
    "lens": "qa", "headline": "原因候補は関係グラフから見つかりました。",
    "route": {"path": ["関係グラフを照会"]}, "summary": {"total": 1},
    "scope": {"world": "w1", "scope_paths": [], "source": "all", "layer": "both",
             "tools": {"grep": False, "fulltext": False, "graph": True}},
    "data": {"citations": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                            "quote": "消費税率は10%", "span": [3, 3]}]},
    "sources": [{"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
                "download_url": "/documents/download?world=w1&rel=x"}],
}


def test_inquiry_tools_details_collapsed_by_default(page, web_base_url):
    """「詳細」は既定閉（初回訪問・折りたたみの中身は見えない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#tools-details-body")).to_be_hidden()
    expect(page.locator("#tools-details-head")).to_have_attribute("aria-expanded", "false")
    # 既定は全ON＝要約チップに何も付記しない。
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")


def test_inquiry_tools_graph_only_send_reflects_body_and_hides_grep_fulltext_nodes(page, web_base_url):
    """詳細を開いて grep・全文をOFFにすると、要約チップに反映され、送信 body にも反映される。
    思考の流れには（ツールが提示されないため）grep/全文のノードが出ない。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, stream_events=[
        {"type": "node", "id": "tool-graph", "kind": "tool", "status": "done",
         "label": "関係グラフをたどる", "detail": "「TAX-RATE」の関連部品"},
        {"type": "answer", "conversation_id": 101, "message": {"answer": _TOOLS_GRAPH_ONLY_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()

    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-details-body")).to_be_visible()
    page.locator("#tools-seg [data-tool='grep']").click()
    page.locator("#tools-seg [data-tool='fulltext']").click()
    expect(page.locator("#tools-seg [data-tool='grep']")).not_to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='fulltext']")).not_to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='graph']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("使う検索: グラフのみ")

    page.locator("#input").fill("夜間バッチが異常終了しました。原因は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body.get("tools") == {"grep": False, "fulltext": False, "graph": True}
    expect(page.locator("#flow")).to_contain_text("関係グラフをたどる")
    expect(page.locator("#flow")).not_to_contain_text("資料を検索（語句そのまま）")
    expect(page.locator("#flow")).not_to_contain_text("資料を検索（全文/日本語）")


def test_inquiry_tools_last_one_cannot_be_turned_off(page, web_base_url):
    """残り1つの検索経路はクリックしても外せない（disabled・理由のツールチップ付き）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#tools-details-head").click()

    page.locator("#tools-seg [data-tool='grep']").click()
    page.locator("#tools-seg [data-tool='fulltext']").click()
    graph_btn = page.locator("#tools-seg [data-tool='graph']")
    expect(graph_btn).to_be_disabled()
    expect(graph_btn).to_have_attribute("title", re.compile("OFFにできません"))
    # 残りON状態は変わらない（依然「グラフのみ」）。
    expect(page.locator("#inquiry-chip-label")).to_contain_text("使う検索: グラフのみ")


def test_inquiry_tools_unavailable_chip_hidden(page, web_base_url):
    """SC-6e: グラフが実接続で不達（`GET /chat/tools-availability` が graph=false）なら、
    グラフのチップ自体を表示しない（実効検索経路0を選べる状態を作らない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, tools_availability={"grep": True, "fulltext": True, "graph": False})
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-seg [data-tool='graph']")).to_be_hidden()
    expect(page.locator("#tools-seg [data-tool='grep']")).to_be_visible()
    expect(page.locator("#tools-seg [data-tool='fulltext']")).to_be_visible()


def test_inquiry_tools_last_one_gating_ignores_unavailable_tool(page, web_base_url):
    """「最後の1つ」判定は available ∩ requested で行う——不達のグラフが希望ON のまま
    残っていても、grep/全文のうち最後に残った1つが正しく disabled になる
    （不達チップをカウントに含めて「まだ2つ残っている」と誤判定しない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, tools_availability={"grep": True, "fulltext": True, "graph": False})
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#tools-details-head").click()
    page.locator("#tools-seg [data-tool='grep']").click()
    fulltext_btn = page.locator("#tools-seg [data-tool='fulltext']")
    expect(fulltext_btn).to_be_disabled()
    expect(fulltext_btn).to_have_attribute("title", re.compile("OFFにできません"))


def test_inquiry_tools_unavailable_axis_omitted_from_send_avoids_422(page, web_base_url):
    """SC-6e: グラフが不達のまま grep だけを変更して送信しても、送信 body に `graph:true` が
    含まれず 422 にならない（不達チップは隠れて触れないため、グラフの内部値は既定 true の
    ままだが、`web/chat/inquiry.js::toolsForSend` が送信直前にそのキーを省略する）。モックは
    実サーバ（`_validate_tools_availability`）と同じ「明示ONかつ不達」判定で 422 を返すため、
    このテストは是正前なら実際に失敗する（graph:true を送ってしまい 422 表示になる）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, tools_availability={"grep": True, "fulltext": True, "graph": False})
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-seg [data-tool='graph']")).to_be_hidden()

    page.locator("#tools-seg [data-tool='grep']").click()   # グラフには触れず grep だけを変更

    page.locator("#input").fill("消費税率は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")
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
    """SC-6e: 「OFFにした検索を戻す」ボタン（SC-6d 連携）は検索経路トグルを全ONへ戻して直前の
    質問を再送する——グラフが不達のままでも、この操作は利用者の明示 ON なので送信 body から
    省略せず、実サーバの `_validate_tools_availability` と同じ判定で422になる（黙って OFF の
    まま実行しない）。是正前は真偽値だけで「既定ON」と区別できず省略され、422にならず黙って
    OFFのまま実行されていた。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, tools_availability={"grep": True, "fulltext": True, "graph": False},
                                stream_events=[
        {"type": "answer", "conversation_id": 101, "message": {"answer": _TOOLS_RETRY_HINT_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#input").fill("消費税率とは？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("OFF にした検索を戻す")

    page.locator(".retry-hint-btn", has_text="OFF にした検索を戻す").click()

    body = records["turn_starts"][-1]
    assert body.get("tools") == {"grep": True, "fulltext": True, "graph": True}
    expect(page.locator("#messages")).to_contain_text("現在利用できません")   # 黙って完了しない


def test_inquiry_tools_toggle_off_then_on_sends_explicit_despite_availability_drift(page, web_base_url):
    """SC-6e: グラフを OFF→ON と操作した後に送信すると、ページ読込時に取得したクライアント側の
    可用性キャッシュとは無関係に、常に明示 ON をそのまま送る——`GET /chat/tools-availability`
    取得後（ページ読込時点）は到達可能だったが、実際の送信時点（サーバの実接続チェック）では
    不達へ変わっていた場合でも、黙って OFF 実行にはせず 422 で気づける（クライアントは
    「操作したか」だけを保持し、可用性の当否は毎回サーバへ委ねる）。"""
    import json as _json

    from playwright.sync_api import expect

    install_api_mocks(page, tools_availability={"grep": True, "fulltext": True, "graph": True})
    turn_bodies = []

    def handle_turn_start(route):
        body = _json.loads(route.request.post_data or "{}")
        turn_bodies.append(body)
        if (body.get("tools") or {}).get("graph") is True:
            # ページ読込後に接続が不達へ変わった状況を模す（実サーバの受付時422と同型）。
            route.fulfill(status=422, content_type="application/json",
                          body=_json.dumps({"detail": "検索経路 graph は現在利用できません"}))
            return
        route.fulfill(status=200, content_type="application/json",
                      body=_json.dumps({"turn_id": "turn-101", "conversation_id": 101}))

    page.route("**/chat/turns", handle_turn_start)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#tools-details-head").click()

    graph_btn = page.locator("#tools-seg [data-tool='graph']")
    graph_btn.click()   # OFF
    expect(graph_btn).not_to_have_class(re.compile(r"\bon\b"))
    graph_btn.click()   # ON へ戻す（明示操作のまま・値は既定と同じ true）
    expect(graph_btn).to_have_class(re.compile(r"\bon\b"))

    page.locator("#input").fill("消費税率とは？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("現在利用できません")   # 黙って完了しない

    assert turn_bodies[-1].get("tools", {}).get("graph") is True


def test_inquiry_tools_last_one_rapid_clicks_stay_on(page, web_base_url):
    """SC-6e: 「最後の1つ」を連打しても外れない（disabled のためクリックが素通りしない・
    実効検索経路0を作れないことを連打耐性としても固定する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#tools-details-head").click()
    page.locator("#tools-seg [data-tool='grep']").click()
    page.locator("#tools-seg [data-tool='fulltext']").click()
    graph_btn = page.locator("#tools-seg [data-tool='graph']")
    expect(graph_btn).to_be_disabled()
    # disabled 要素への force クリックは実 DOM の click イベントを起こさない（ブラウザの既定動作）
    # ため、何度実行しても素通りせず状態は変わらない。
    for _ in range(5):
        graph_btn.click(force=True, timeout=1000)
    expect(graph_btn).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("使う検索: グラフのみ")


def test_inquiry_tools_chip_keyboard_activation_toggles(page, web_base_url):
    """SC-6e: チップはネイティブ `<button>` なのでキーボード（Enter/Space）でも操作できる
    （クリックだけの実装になっていないことを固定する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#tools-details-head").click()
    grep_btn = page.locator("#tools-seg [data-tool='grep']")
    grep_btn.focus()
    page.keyboard.press("Enter")
    expect(grep_btn).not_to_have_class(re.compile(r"\bon\b"))
    grep_btn.focus()
    page.keyboard.press(" ")
    expect(grep_btn).to_have_class(re.compile(r"\bon\b"))


def test_inquiry_restore_tools_missing_key_defaults_to_all_on(page, web_base_url):
    """SC-6e: `scope.tools` キー自体が無い旧会話（SC-6e 導入前に保存された回答）を
    開いても例外にならず、全ON（既定）として復元する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=115")   # SC-6c の会話フィクスチャ（tools キー無し）
    expect(page.locator("#messages")).to_contain_text("消費税率")
    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-seg [data-tool='grep']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='fulltext']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='graph']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")


def test_inquiry_restore_tools_from_history(page, web_base_url):
    """直近回答の `scope.tools` を無条件に復元する（範囲/探す対象/調べる深さと同じ・§4.3）。
    SC-6e: 復元しただけ（チップを一切操作しない）で追質問を送っても、復元した非既定値
    （grep OFF）がそのまま送信 body に反映される——「既定ONは省略」の規則により、復元直後は
    全軸「未操作」のままだと誤って省略され、無操作の追質問で黙って全ONへ戻ってしまう
    （復元値が非既定なら3軸とも明示状態にする・`inquiry.js::toolsExplicitForRestore` 参照）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=116")
    expect(page.locator("#messages")).to_contain_text("消費税率")
    # 復元後も「詳細」自体は既定閉のまま（会話ごとに永続しない）。
    expect(page.locator("#tools-details-body")).to_be_hidden()
    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-seg [data-tool='grep']")).not_to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='fulltext']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='graph']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).to_contain_text("使う検索")

    # チップには一切触れず、追質問だけ送る。
    page.locator("#input").fill("影響範囲を教えて")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")
    body = records["turn_starts"][-1]
    assert body.get("tools") == {"grep": False, "fulltext": True, "graph": True}


def test_inquiry_new_conversation_resets_tools_to_all_on(page, web_base_url):
    """新規会話は常に全ON（既存会話で絞っていても引き継がない・依頼の設計）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html?conv=116")
    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-seg [data-tool='grep']")).not_to_have_class(re.compile(r"\bon\b"))

    page.locator("#newbtn").click()
    expect(page.locator("#tools-details-body")).to_be_hidden()   # 既定閉にも戻る
    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-seg [data-tool='grep']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='fulltext']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='graph']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")


def test_new_conversation_during_pending_world_options_ignores_stale_conv_followup(page, web_base_url):
    """会話ロード（`pendingConvWorld` セット）の直後・`/world-options` の解決より前に「新規会話」へ
    切替えても、遅れて届く `/world-options` 応答が旧会話の検索経路トグル（絞った状態）を新規会話へ
    誤って再適用しない——`newConversation()`（history.js）が
    `S.pendingConvWorld`/`S.currentScopeMeta` を null にし、chat.js の後追いブロック自体を
    素通りさせる。

    `/world-options` の応答を明示的に解放するまで保留する
    （`test_web_search_pending_conv_world_followup_restores_web_search` と同型の順序制御・
    `time.sleep` によるタイミング頼みでは dispatcher 自体も止まり順序を保証しない）。
    """
    from playwright.sync_api import expect

    held = {}

    def _hold_world_options(route):
        held["route"] = route   # ここでは fulfill しない＝応答を明示的に保留する

    records = install_api_mocks(page)
    page.route("**/world-options", _hold_world_options)
    page.goto(f"{web_base_url}/chat.html?conv=116")   # 検索経路を絞った会話（grep OFF）

    # /world-options は保留したまま＝会話本文の表示は /conversations/116 だけで先に進んだ証拠
    # （pendingConvWorld 経路に確実に入っている）。
    expect(page.locator("#messages")).to_contain_text("消費税率")

    # ここで新規会話へ切替える（/world-options がまだ解決していない）。
    page.locator("#newbtn").click()
    expect(page.locator("#conv-title")).to_have_text("新しい会話")

    # ここで初めて /world-options を解放する——修正前は、保留中に残った pendingConvWorld 後追い
    # ブロックが旧会話（conv=116）の grep:false をこの新規会話へ誤って適用してしまっていた。
    held["route"].fulfill(content_type="application/json",
                          body='{"worlds": ["w1"], "labels": {"w1": "4期"}}')

    page.locator("#tools-details-head").click()
    expect(page.locator("#tools-seg [data-tool='grep']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='fulltext']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#tools-seg [data-tool='graph']")).to_have_class(re.compile(r"\bon\b"))
    expect(page.locator("#inquiry-chip-label")).not_to_contain_text("使う検索")

    page.locator("#input").fill("消費税率とは")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")
    body = records["turn_starts"][-1]
    assert "tools" not in body, "全軸未操作＝既定ONは body.tools キー自体を省略するはず"


def test_slash_prefix_message_sent_verbatim_without_body_lens(page, web_base_url):
    """メッセージ先頭のスラッシュ指定は1回限りの明示（サーバ側で解釈）——クライアントは本文を
    加工せず、ブロックの選択状態（自動）も変えない（§3.1・裁定11）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()

    page.locator("#input").fill("/影響 消費税率を変えたい")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body["message"] == "/影響 消費税率を変えたい"
    assert "lens" not in body
    expect(page.locator("#lens-seg [data-lens='auto']")).to_have_class(re.compile(r"\bon\b"))


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


def test_retry_hint_button_broadens_scope_and_resends(page, web_base_url):
    """出典0件時の再検索案内（SC-6d・§5）: ボタンを押すと絞られていた範囲を広げ、直前の質問を
    そのまま再送する。RV1 #7: 選択しなかった軸（探す対象）は現在のブロック設定ではなく
    元回答（`_RETRY_HINT_ANSWER.scope.layer == "docs"`）を基準に据え置く。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, stream_events=[
        {"type": "answer", "conversation_id": 101, "message": {"answer": _RETRY_HINT_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#input").fill("消費税率とは？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("範囲を全体に広げる")
    expect(page.locator("#messages")).to_contain_text("コードも含めて探す")

    page.locator(".retry-hint-btn", has_text="範囲を全体に広げる").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body.get("scope_paths") == []
    assert body.get("layer") == "docs"   # 選択しなかった軸は元回答の値を維持（RV1 #7）
    assert body.get("message") == "消費税率とは？"


_DEPTH_RETRY_HINT_ANSWER = {
    "lens": "qa", "headline": "該当する記述は見つかりませんでした（確証なし）。検索語を変えて試してください。",
    "route": {"path": ["文書を検索"]}, "summary": {"total": 0},
    "scope": {"world": "w1", "scope_paths": ["4期/02_設計"], "source": "explicit",
             "layer": "docs", "layer_applied": True, "depth_profile": "standard"},
    "data": {"citations": []}, "sources": [],
    "retry_hints": [
        {"kind": "depth", "label": "調べる深さを上げて探す（今は標準）", "action": {"depth_profile": "max"}},
    ],
}


def test_retry_hint_button_raises_depth_profile_and_resends(page, web_base_url):
    """出典0件時の再検索案内（SC-6d・§5）: 調べる深さの案内ボタンを押すと `depth_profile=max` を
    付けて直前の質問をそのまま再送する。選択しなかった軸（範囲・探す対象）は
    `test_retry_hint_button_broadens_scope_and_resends` と同じく元回答の値を維持する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, stream_events=[
        {"type": "answer", "conversation_id": 101, "message": {"answer": _DEPTH_RETRY_HINT_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#input").fill("消費税率とは？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("調べる深さを上げて探す")

    page.locator(".retry-hint-btn", has_text="調べる深さを上げて探す").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body.get("depth_profile") == "max"
    assert body.get("scope_paths") == ["4期/02_設計"]   # 未選択の範囲は元回答の値を維持
    assert body.get("layer") == "docs"                  # 未選択の探す対象も元回答の値を維持
    assert body.get("message") == "消費税率とは？"


# Codex CLI タイムアウトの継続注記（2026-09-02 実環境観測）: 進行中の宣言文がそのまま headline に
# 残ったターンに「続きを調べる」ボタンを出す。SC-6d の retry_hints/`.retry-hint-btn` 機構をそのまま
# 使うが、kind="resume" は他の kind と異なり「範囲等を広げて直前の質問を再送」ではなく固定文言
# 「続きを調べて」をそのまま送るだけ（`web/chat.js` の kind="resume" 専用分岐）。
_CODEX_TIMEOUT_ANSWER = {
    "lens": "qa", "headline": "次に資料を確認します。",
    "route": {"path": ["文書を検索"]}, "summary": {"total": 0},
    "scope": {"world": "w1", "scope_paths": [], "source": "explicit", "layer": "both", "layer_applied": True},
    "data": {"citations": ["doc1"]}, "sources": ["doc1"],
    "codex_timed_out": True,
    "retry_hints": [{"kind": "resume", "label": "続きを調べる", "action": {"message": "続きを調べて"}}],
}


def test_codex_timeout_note_and_continue_button_resends_fixed_message(page, web_base_url):
    """注記（本文とは別要素）とボタンが表示され、クリックで固定文言「続きを調べて」を送信する
    （範囲・探す対象等は変えない・resume はサーバ側の codex_session_id 継続に委ねる）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, stream_events=[
        {"type": "answer", "conversation_id": 101, "message": {"answer": _CODEX_TIMEOUT_ANSWER}},
    ])
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#input").fill("消費税率とは？")
    page.locator("#send").click()
    expect(page.locator("#messages")).to_contain_text("次に資料を確認します。")
    note = page.locator(".budget-note").last
    expect(note).to_be_visible()
    expect(note).to_contain_text("調査の時間上限に達したため途中までの結果です")

    page.locator(".retry-hint-btn", has_text="続きを調べる").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    body = records["turn_starts"][-1]
    assert body.get("message") == "続きを調べて"
    assert not body.get("scope_paths")   # 範囲は変えない（元回答は全体のまま）
    assert body.get("layer") is None     # 探す対象も変えない（既定=送らない）


def test_inquiry_chip_narrow_viewport_prioritizes_right_pane_over_left(page, web_base_url):
    """RV1 #5: 狭幅でチップから明示的に開くと、既存のレイアウト計算は左ペインを優先的に縮めて
    右ペインを確保する（右トラックを 0 に戻さない）。

    幅は 950px（`updateLayout()` の既定 L=264/R=300/CMIN=460 が Lmin=200・Rmin=280 まで縮めて
    ちょうど収まる境界）を使う——900px だと `preferRight` を使わない**初回ロード時の既定レイアウト**
    自体が両ペインを最小まで縮めてもなお 50px 足りず右ペインを 0 へ畳んでしまい、`#rightclose`
    （閉じるボタン）がクリック前から非表示/操作不能になって前提が崩れる（RV2 是正）。
    """
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.set_viewport_size({"width": 950, "height": 700})
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#rightclose").click()
    expect(page.locator(".app")).to_have_class(re.compile(r"\brzero\b"))

    page.locator("#inquiry-chip").click()
    expect(page.locator(".app")).not_to_have_class(re.compile(r"\brzero\b"))
    expect(page.locator("#inquiry-body")).to_be_visible()
    right_track = page.evaluate(
        "getComputedStyle(document.documentElement).getPropertyValue('--tR')").strip()
    assert right_track not in ("", "0px")


def test_confirm_first_resend_restores_slash_lens_via_question_payload(page, web_base_url):
    """RV1 #3/RV2 #1: 「確認してから進めて」を伴うスラッシュ指定は確認カードの payload に
    lens_source="slash"・lens_block（ブロックの継続設定）を持つ。回答の再送は既存のスラッシュ
    解決経路をそのまま使うため、本文の先頭へ元の接頭辞（/影響 ）を復元して送り、body.lens には
    実効レンズ（impact）ではなくブロックの継続設定（lens_block="qa"）を渡す（サーバ側の
    _resolve_lens がスラッシュを優先解決し、ブロック状態は変えない・1回限りの契約を保つ）。"""
    import json

    from playwright.sync_api import expect

    calls = {"n": 0}

    def handle_turn_stream(route):
        calls["n"] += 1
        if calls["n"] == 1:
            events = [
                {"type": "question", "conversation_id": 101, "interaction_id": "confirm-abcd",
                 "mode": "single",
                 "prompt": "確認してから進めるよう指定されています。何を確認してから進めますか？",
                 "options": [{"id": "scope", "label": "対象範囲（どの資料/システムか）",
                             "description": "どのフォルダ・資料・システムを対象にするか"}],
                 "allow_free_text": True, "original_message": "税率表を確認してから進めて。",
                 "lens": "impact", "layer": None, "scope_paths": [],
                 "lens_source": "slash", "lens_block": "qa"},
            ]
        else:
            events = [{"type": "answer", "conversation_id": 101, "message": {"answer": IMPACT_ANSWER}}]
        body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)

    records = install_api_mocks(page)
    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()

    page.locator("#input").fill("/影響 税率表を確認してから進めて。")
    page.locator("#send").click()
    expect(page.locator(".askcard")).to_be_visible()

    page.locator("[data-qopt]").first.check()
    page.locator("[data-ask-submit]").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    assert len(records["turn_starts"]) == 2
    resend = records["turn_starts"][1]
    # ブロックの継続設定（qa）が ChatReq.lens として送られ、実効レンズ（impact）は本文の
    # スラッシュ接頭辞から再解決される（RV2 #1）。
    assert resend.get("lens") == "qa"
    assert resend.get("message", "").startswith("/影響 ")
    # ブロック自体は「自動」のまま（1回限りの明示・S.lens は変わらない）。
    expect(page.locator("#lens-seg [data-lens='auto']")).to_have_class(re.compile(r"\bon\b"))


def test_confirm_first_resend_restores_tools_from_question_payload(page, web_base_url):
    """SC-6e: 確認カードの payload に検索経路トグル（グラフのみ）を持たせると、回答の
    再送 body にもその値がそのまま渡る（欠落=全ONへ復元される事故を防ぐ）。"""
    import json

    from playwright.sync_api import expect

    calls = {"n": 0}

    def handle_turn_stream(route):
        calls["n"] += 1
        if calls["n"] == 1:
            events = [
                {"type": "question", "conversation_id": 101, "interaction_id": "confirm-tools1",
                 "mode": "single",
                 "prompt": "確認してから進めるよう指定されています。何を確認してから進めますか？",
                 "options": [{"id": "scope", "label": "対象範囲（どの資料/システムか）",
                             "description": "どのフォルダ・資料・システムを対象にするか"}],
                 "allow_free_text": True, "original_message": "原因を確認してから進めて。",
                 "lens": "qa", "layer": None, "scope_paths": [],
                 "lens_source": "explicit", "lens_block": None,
                 "tools": {"grep": False, "fulltext": False, "graph": True}},
            ]
        else:
            events = [{"type": "answer", "conversation_id": 101, "message": {"answer": IMPACT_ANSWER}}]
        body = "".join(f"data: {json.dumps(e, ensure_ascii=False)}\n\n" for e in events)
        route.fulfill(status=200, headers={"Content-Type": "text/event-stream"}, body=body)

    records = install_api_mocks(page)
    page.route("**/chat/turns/*/stream?**", handle_turn_stream)
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#kbtoggle").click()

    page.locator("#input").fill("原因を確認してから進めて。")
    page.locator("#send").click()
    expect(page.locator(".askcard")).to_be_visible()

    page.locator("[data-qopt]").first.check()
    page.locator("[data-ask-submit]").click()
    expect(page.locator("#rt")).to_contain_text("完了")

    assert len(records["turn_starts"]) == 2
    resend = records["turn_starts"][1]
    assert resend.get("tools") == {"grep": False, "fulltext": False, "graph": True}


# ===== WEB-1（docs/notes/2026-08-29-デプロイ後バックログ.md）: Web 検索の二段階化 =====
# 表示条件＝管理者許可（GET /settings の web_search_available）かつ現在の頭脳が Codex（OpenAI 直結・
# openai_endpoint_kind=="openai"）。条件を満たさないときは「参照する」行ごと非表示（#websearchtoggle）。
# body の web_search・会話復元（scope.web_search）は作成のみ（デプロイ後バックログ WEB-1 の e2e 範囲）。

_WEB_SEARCH_ELIGIBLE_SETTINGS = {
    **mock_api.SETTINGS_RESP,
    "agent": "codex", "codex_model_provider": "openai", "construct_id": "codex_openai",
    "web_search_available": True, "openai_endpoint_kind": "openai",
}


def test_web_search_row_hidden_by_default(page, web_base_url):
    """既定モック（頭脳=OpenAI・管理者許可も既定 false）では Web 検索行は出ない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#websearchtoggle")).to_be_hidden()


def test_web_search_row_visible_when_admin_allowed_and_codex_openai(page, web_base_url):
    """管理者許可＋頭脳が Codex（OpenAI 直結）の両方が揃うと行が表示される。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#websearchtoggle")).to_be_visible()


def test_web_search_row_hidden_when_azure_endpoint(page, web_base_url):
    """頭脳が Codex でも、接続先が Azure（OpenAI 直結でない）なら行は出ない。"""
    from playwright.sync_api import expect

    settings = {**_WEB_SEARCH_ELIGIBLE_SETTINGS, "openai_endpoint_kind": "azure"}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#websearchtoggle")).to_be_hidden()


def test_web_search_row_hidden_for_codex_ollama_construct_even_if_admin_allowed(page, web_base_url):
    """WEB-1: 頭脳が Codex(Ollama)（construct_id=codex_ollama）のときは、管理者許可・
    openai_endpoint_kind が "openai" のままでも行は出ない（Codex は OpenAI の web_search＝ホスト型
    検索インデックスに Ollama 経由では接続できないため、表示条件は agent でなく construct_id で
    codex_openai だけに絞る）。"""
    from playwright.sync_api import expect

    settings = {**_WEB_SEARCH_ELIGIBLE_SETTINGS,
               "codex_model_provider": "ollama", "construct_id": "codex_ollama"}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#websearchtoggle")).to_be_hidden()


def test_web_search_row_hidden_when_admin_not_allowed(page, web_base_url):
    """頭脳が Codex（OpenAI 直結）でも、管理者が許可していなければ行は出ない。"""
    from playwright.sync_api import expect

    settings = {**_WEB_SEARCH_ELIGIBLE_SETTINGS, "web_search_available": False}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#websearchtoggle")).to_be_hidden()


def test_web_search_toggle_sends_body_web_search_true_only_when_on(page, web_base_url):
    """既定 OFF（body に web_search を載せない）→ ON にすると body.web_search が true。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#websearchtoggle")).to_be_visible()

    page.locator("#input").fill("消費税の最新情報は？")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")
    assert "web_search" not in records["turn_starts"][-1], "既定 OFF なのに web_search が送られている"

    page.locator("#websearchtoggle").click()
    expect(page.locator("#websearchtoggle")).to_have_class(re.compile(r"\bon\b"))
    page.locator("#input").fill("もう一つ教えてください")
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了")
    assert records["turn_starts"][-1]["web_search"] is True


def test_web_search_restore_from_conversation_history(page, web_base_url):
    """会話を開き直すと直近回答の Web 検索希望（scope.web_search）が復元される
    （Codex＋管理者許可の構成のときだけ行自体が見える）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    page.goto(f"{web_base_url}/chat.html?conv=114")
    expect(page.locator("#messages")).to_contain_text("消費税")
    expect(page.locator("#websearchtoggle")).to_be_visible()
    expect(page.locator("#websearchtoggle")).to_have_class(re.compile(r"\bon\b"))


def test_web_search_pending_conv_world_followup_restores_web_search(page, web_base_url):
    """WEB-1: 会話取得が `/world-options` の解決より先に完了する順序（`chat.js` の
    `pendingConvWorld` 後追い経路）を明示的に強制しても、Web 検索希望（`scope.web_search`）が
    正しく復元される。`applyConversationScope` は世界一覧が未読込のうちは `sameDir` が偽になり
    一旦 `S.webSearch=false` へ倒すため、`pendingConvWorld` の後追いブロックが `S.scope`/`S.lens`/
    `S.layer` と同様に `S.webSearch` も再適用しないと、復元した ON が世界一覧読込後に元へ
    戻らないまま失われる（`test_world_selector_conversation_beats_saved_choice` と同型の順序）。

    `/world-options` の応答を明示的に解放するまで保留し（`time.sleep` によるタイミング頼みでは
    dispatcher 自体も止まり順序を保証しない）、会話本文の表示（＝`/conversations/114` が確実に
    先へ進んだこと）を確認してから解放する——この後解放して初めて `pendingConvWorld` 後追い
    ブロックが走る、という順序をテスト側が完全に制御する。"""
    from playwright.sync_api import expect

    held = {}

    def _hold_world_options(route):
        held["route"] = route   # ここでは fulfill しない＝応答を明示的に保留する

    install_api_mocks(page, settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    page.route("**/world-options", _hold_world_options)
    page.goto(f"{web_base_url}/chat.html?conv=114")

    # /world-options は保留したまま＝会話本文がここで表示されるのは /conversations/114 の応答
    # だけで先に進んだ証拠（pendingConvWorld 経路に確実に入っている）。
    expect(page.locator("#messages")).to_contain_text("消費税")

    # ここで初めて /world-options を解放する（pendingConvWorld の後追いブロックが走る）。
    held["route"].fulfill(content_type="application/json",
                          body='{"worlds": ["w1"], "labels": {"w1": "4期"}}')

    expect(page.locator("#websearchtoggle")).to_have_class(re.compile(r"\bon\b"))


def test_web_search_new_conversation_always_starts_off(page, web_base_url):
    """新規会話は Web 検索が常に OFF（前の会話や以前の選択を引き継がない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=_WEB_SEARCH_ELIGIBLE_SETTINGS)
    page.goto(f"{web_base_url}/chat.html?conv=114")
    expect(page.locator("#websearchtoggle")).to_have_class(re.compile(r"\bon\b"))

    page.locator("#newbtn").click()
    expect(page.locator("#websearchtoggle")).to_be_visible()
    expect(page.locator("#websearchtoggle")).not_to_have_class(re.compile(r"\bon\b"))
