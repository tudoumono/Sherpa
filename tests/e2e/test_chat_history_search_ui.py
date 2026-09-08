"""チャット履歴の検索（H・docs/proposals/2026-09-07-履歴検索と下調べ並列化.md §1）の e2e。

左ペイン `#hist-search` のタイトル絞り込み（即時・クライアント側）・0件表示・Esc/×クリアと、
本文一致（`GET /conversations?q=` の応答・入力停止300ms後）による行の再表示＋抜粋表示を確認する。
mock は `mock_api.conversations_search_response`（`CONVERSATIONS_LIST` のタイトル一致に加え、
id=101 をタイトルに現れない語での本文一致として返す＝`CONVERSATIONS_MESSAGE_MATCH_SNIPPET`）。
history.js 側の一覧描画・履歴系の既存 e2e（test_chat_ui.py -k history）は本ファイルの対象外。
"""
from __future__ import annotations

from mock_api import install_api_mocks


def test_history_search_filters_by_title_immediately(page, web_base_url):
    """タイトルの部分一致は即時（クライアント側）で絞り込む。一致しない行は隠れる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")

    expect(page.locator("[data-open='101']")).to_be_visible()
    expect(page.locator("[data-open='202']")).to_be_visible()

    page.locator("#hist-search").fill("消費税")

    expect(page.locator("[data-open='101']")).to_be_visible()
    expect(page.locator("[data-open='202']")).to_be_hidden()
    expect(page.locator("#hist-search-clear")).to_be_visible()


def test_history_search_shows_no_hit_message(page, web_base_url):
    """タイトル・本文のどちらにも一致しなければ全行が隠れ、「見つかりません」を表示する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("[data-open='101']")).to_be_visible()

    page.locator("#hist-search").fill("存在しない語彙xyz")

    expect(page.locator("#hist-nohit")).to_be_visible()
    expect(page.locator("#hist-nohit")).to_have_text("見つかりません")
    expect(page.locator("[data-open='101']")).to_be_hidden()
    expect(page.locator("[data-open='202']")).to_be_hidden()


def test_history_search_escape_and_clear_button_restore_full_list(page, web_base_url):
    """Esc・×ボタンのどちらでも検索語が消え、絞り込み前の状態に戻る。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    search = page.locator("#hist-search")

    search.fill("消費税")
    expect(page.locator("[data-open='202']")).to_be_hidden()
    search.press("Escape")
    expect(search).to_have_value("")
    expect(page.locator("[data-open='202']")).to_be_visible()
    expect(page.locator("#hist-search-clear")).to_be_hidden()

    search.fill("存在しない語彙xyz")
    expect(page.locator("#hist-nohit")).to_be_visible()
    page.locator("#hist-search-clear").click()
    expect(search).to_have_value("")
    expect(page.locator("#hist-nohit")).to_be_hidden()
    expect(page.locator("[data-open='101']")).to_be_visible()
    expect(page.locator("[data-open='202']")).to_be_visible()


def test_history_search_message_match_reveals_row_with_snippet(page, web_base_url):
    """タイトル不一致でも本文一致（サーバ応答）なら行が再び現れ、抜粋が1行添えられる
    （入力停止300ms後の GET /conversations?q= 応答。対象は id=101＝タイトル「消費税率の相談」には
    検索語 "バッチ" は含まれないが、mock 上は本文一致として返る＝CONVERSATIONS_MESSAGE_MATCH_SNIPPET）。
    """
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    hit_row = page.locator("[data-open='101']")

    page.locator("#hist-search").fill("バッチ")

    # 即時（クライアント側）はタイトル不一致で一旦隠れ、300ms後の本文一致応答で再び現れる。
    expect(hit_row).to_be_visible()
    expect(hit_row.locator(".hist-snippet")).to_contain_text("バッチ")
    expect(page.locator("[data-open='202']")).to_be_hidden()


def test_history_search_content_fetch_failure_shows_error_not_no_hit(page, web_base_url):
    """RV是正: GET /conversations?q= が 5xx で失敗しても「見つかりません」を確定表示しない
    （本文一致は未確認なだけで、実際には一致する会話があるかもしれない）。代わりに「本文検索に
    失敗しました」を出し、次の入力で消える（このとき失敗経路を外し、通常どおり成功させる）。"""
    from playwright.sync_api import expect

    def fail_content_search(route):
        route.fulfill(status=500, body="{}")

    install_api_mocks(page)
    page.route("**/conversations?q=*", fail_content_search)
    page.goto(f"{web_base_url}/chat.html")

    # タイトルには現れない語（本文一致でしか見つからない想定）で検索する。
    page.locator("#hist-search").fill("バッチ")

    expect(page.locator("#hist-search-error")).to_be_visible()
    expect(page.locator("#hist-search-error")).to_have_text("本文検索に失敗しました")
    expect(page.locator("#hist-nohit")).to_be_hidden()   # 「見つかりません」の誤表示が無いこと

    # 次の入力（本文検索は復旧させて成功させる）でエラー表示は消える。
    page.unroute("**/conversations?q=*", fail_content_search)
    page.locator("#hist-search").fill("消費税")
    expect(page.locator("#hist-search-error")).to_be_hidden()
    expect(page.locator("[data-open='101']")).to_be_visible()


def test_history_search_input_has_maxlength_matching_server_limit(page, web_base_url):
    """RV是正: サーバの `q` 上限（trim後1〜100字）と揃え、101字入力で422を誘発させない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("#hist-search")).to_have_attribute("maxlength", "100")


def test_history_search_error_stays_exclusive_of_no_hit_across_rerender(page, web_base_url):
    """RV是正（2巡目・中）: 本文検索の失敗表示中に会話一覧が再描画（history.js::loadConversations()の
    #convlist innerHTML 差し替え）されても、`_applyFilter` が「見つかりません」を出してエラー表示と
    同時表示にならない（既存 e2e に自然な再描画トリガーが無いため #convlist の innerHTML を
    page.evaluate で差し替えて MutationObserver を発火させる）。"""
    from playwright.sync_api import expect

    def fail_content_search(route):
        route.fulfill(status=500, body="{}")

    install_api_mocks(page)
    page.route("**/conversations?q=*", fail_content_search)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#hist-search").fill("バッチ")
    expect(page.locator("#hist-search-error")).to_be_visible()
    expect(page.locator("#hist-nohit")).to_be_hidden()

    # #convlist の再描画（同じ内容への innerHTML 差し替え＝ history.js::loadConversations() と
    # 同型の childList mutation）を起こす。
    page.evaluate("() => { const el = document.getElementById('convlist'); el.innerHTML = el.innerHTML; }")

    expect(page.locator("#hist-search-error")).to_be_visible()
    expect(page.locator("#hist-nohit")).to_be_hidden()


def test_history_search_clearing_input_invalidates_in_flight_fetch_failure(page, web_base_url):
    """RV是正（2巡目・低）: 進行中（応答待ち）の本文検索 fetch がある状態で入力を空にすると、
    その fetch が後から失敗しても失敗表示は出ない（世代カウンタ `_fetchSeq` が空文字でも進むこと）。"""
    from playwright.sync_api import expect

    held = {}

    def _hold_content_search(route):
        held["route"] = route   # ここでは fulfill しない＝応答を明示的に保留する

    install_api_mocks(page)
    page.route("**/conversations?q=*", _hold_content_search)
    page.goto(f"{web_base_url}/chat.html")

    page.locator("#hist-search").fill("バッチ")
    page.wait_for_timeout(400)   # 300ms のデバウンスを過ぎ、fetch が発行され held に route が入るまで待つ
    assert "route" in held, "GET /conversations?q= が発行されていない（デバウンス未経過？）"

    page.locator("#hist-search").fill("")   # Esc/×クリアではなく、通常の入力で空にする

    held["route"].fulfill(status=500, body="{}")   # 保留していた旧 fetch を今になって失敗させる
    page.wait_for_timeout(100)   # 応答処理が走る猶予

    expect(page.locator("#hist-search-error")).to_be_hidden()
    expect(page.locator("#hist-nohit")).to_be_hidden()


def test_history_search_katakana_hiragana_normalization_matches_title(page, web_base_url):
    """RV是正（2巡目・低）: NFKC+lower だけでは「バッチ」（カタカナ）と「ばっち」（ひらがな）を
    区別してしまう。カタカナ→ひらがな畳み込みにより、ひらがなで検索してもカタカナタイトルの行が
    即時（クライアント側）の絞り込みで残る。"""
    from playwright.sync_api import expect

    katakana_conv = {
        "id": 301, "title": "バッチ処理の相談", "version": "v1", "pinned": False,
        "updated_at": "2026-07-01T09:00:00+00:00", "origin": "own", "read_only": False,
        "received_at": None, "shared_by_user_id": None, "shared_by_name": None, "share_status": None,
    }
    install_api_mocks(page)
    page.route("**/conversations", lambda route: route.fulfill(json=[katakana_conv]))
    page.goto(f"{web_base_url}/chat.html")
    expect(page.locator("[data-open='301']")).to_be_visible()

    page.locator("#hist-search").fill("ばっち処理")

    expect(page.locator("[data-open='301']")).to_be_visible()
