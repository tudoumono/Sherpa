from __future__ import annotations

import json
import re

from mock_api import GRAPH, USER_MEMBER, install_api_mocks


def test_graph_page_loads_and_filters(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/graph.html")

    expect(page.locator("#gcount")).to_contain_text("ノード 4・関係 3")
    expect(page.locator("#legtypes")).to_contain_text("文書")

    page.locator("#legtypes .ftog", has_text="文書").click()
    expect(page.locator("#fclear")).to_be_visible()
    expect(page.locator("#gcount")).to_contain_text("表示")

    page.locator("#gsearch").fill("存在しない語")
    expect(page.locator("#gcount")).to_contain_text("一致なし")


def test_graph_ai_question_posts_graph_ask(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/graph.html")

    expect(page.locator("#gcount")).to_contain_text("ノード 4・関係 3")
    page.locator("#gask").fill("TAX-RATE に関係するプログラムは？")
    page.locator("#gaskbtn").click()

    expect(page.locator("#ganswer")).to_contain_text("TAX-RATE は消費税率と TAXCALC に関係します。")
    expect(page.locator("#ganswer")).to_contain_text("文書")
    expect(page.locator("#ganswer")).to_contain_text("TAXCALC")
    assert records["graph_ask"][-1] == {
        "question": "TAX-RATE に関係するプログラムは？",
        "world": "w1",
        "scope_paths": [],
    }


def test_graph_ai_question_shows_llm_unavailable_as_error(page, web_base_url):
    """status="llm_unavailable"（AI 未接続）は通常の回答と見分けが付かない表示にしない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/graph/ask", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({
            "status": "llm_unavailable", "world": "w1", "question": "TAX-RATE に関係するプログラムは？",
            "answer": "AI に接続できないため、この質問には回答できません（中央の API キーが未設定です）。",
            "cited_nodes": [], "docs": [], "summary": None,
        }),
    ))
    page.goto(f"{web_base_url}/graph.html")

    expect(page.locator("#gcount")).to_contain_text("ノード 4・関係 3")
    page.locator("#gask").fill("TAX-RATE に関係するプログラムは？")
    page.locator("#gaskbtn").click()

    answer = page.locator("#ganswer .ganswer-text")
    expect(answer).to_contain_text("AI に接続できないため")
    expect(answer).to_have_css("color", "rgb(220, 38, 38)")


def test_graph_ai_question_shows_failed_as_error(page, web_base_url):
    """status="failed"（回答生成中の例外・graph_admin.py::ask_graph の except 分岐）も
    llm_unavailable と同様にエラー表示にする（graph.js::renderAskResult は両方を isError 扱いに
    している）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/graph/ask", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({
            "status": "failed", "world": "w1", "question": "TAX-RATE に関係するプログラムは？",
            "answer": "回答の生成中にエラーが発生しました。時間をおいて再度お試しください。",
            "cited_nodes": [], "docs": [], "summary": None,
        }),
    ))
    page.goto(f"{web_base_url}/graph.html")

    expect(page.locator("#gcount")).to_contain_text("ノード 4・関係 3")
    page.locator("#gask").fill("TAX-RATE に関係するプログラムは？")
    page.locator("#gaskbtn").click()

    answer = page.locator("#ganswer .ganswer-text")
    expect(answer).to_contain_text("回答の生成中にエラーが発生しました")
    expect(answer).to_have_css("color", "rgb(220, 38, 38)")


def test_graph_ai_question_no_evidence_is_not_shown_as_error(page, web_base_url):
    """status="no_graph_evidence"（グラフに根拠が無かっただけ）は正常回答の見た目のまま
    （danger色にしない）＝llm_unavailable/failed とだけ区別する回帰確認。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/graph/ask", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({
            "status": "no_graph_evidence", "world": "w1", "question": "NOEXIST の関連は？",
            "answer": "グラフに根拠が見つかりませんでした（確証なし）。用語や範囲を変えて試してください。",
            "cited_nodes": [], "docs": [], "summary": None,
        }),
    ))
    page.goto(f"{web_base_url}/graph.html")

    expect(page.locator("#gcount")).to_contain_text("ノード 4・関係 3")
    page.locator("#gask").fill("NOEXIST の関連は？")
    page.locator("#gaskbtn").click()

    answer = page.locator("#ganswer .ganswer-text")
    expect(answer).to_contain_text("グラフに根拠が見つかりませんでした")
    expect(answer).not_to_have_css("color", "rgb(220, 38, 38)")


def test_graph_show_all_reveals_full_graph(page, web_base_url):
    """②graph 軽量化: 初期は主要ノードのみ＋「すべて表示」で全件（専門用語ゼロの文言）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    # /graph を上書き: 初期（limit なし）＝主要3件で truncated、「すべて表示」（limit=0）＝全4件。
    truncated = {**GRAPH, "nodes": GRAPH["nodes"][:3], "edges": GRAPH["edges"][:2],
                 "total_nodes": 4, "total_edges": 3, "truncated": True}

    def graph_route(route):
        payload = GRAPH if "limit=0" in route.request.url else truncated
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))

    page.route(re.compile(r"/graph\?"), graph_route)       # /graph/facets・/graph/search は素通し
    page.goto(f"{web_base_url}/graph.html")

    expect(page.locator("#gcount")).to_contain_text("主要な 3 件")
    expect(page.locator("#gcount")).to_contain_text("全 4 件")
    expect(page.locator("#showall")).to_be_visible()

    page.locator("#showall").click()
    expect(page.locator("#gcount")).to_contain_text("ノード 4・関係 3")
    expect(page.locator("#showall")).to_be_hidden()


def test_graph_truncated_search_guides_to_show_all(page, web_base_url):
    """②graph 軽量化 RV是正（2026-07-08 Med#2）: 主要ノードのみ表示中にクイック名検索で見つからない場合、
    「表示中には見つかりません」＋「すべて表示」への案内を出す（サーバ検索への寄せ替えはしない・スコープ維持）。
    """
    from playwright.sync_api import expect

    install_api_mocks(page)
    # 「請求機能」（index 3）を初期表示から外す＝主要3件のみ表示中で未ロード扱い。
    truncated = {**GRAPH, "nodes": GRAPH["nodes"][:3], "edges": GRAPH["edges"][:2],
                 "total_nodes": 4, "total_edges": 3, "truncated": True}

    def graph_route(route):
        payload = GRAPH if "limit=0" in route.request.url else truncated
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps(payload, ensure_ascii=False))

    page.route(re.compile(r"/graph\?"), graph_route)
    page.goto(f"{web_base_url}/graph.html")

    expect(page.locator("#gcount")).to_contain_text("主要な 3 件")
    page.locator("#gsearch").fill("請求機能")             # 未ロードノード名で検索
    expect(page.locator("#gcount")).to_contain_text("表示中には見つかりません")
    expect(page.locator("#gcount")).to_contain_text("すべて表示")

    # truncated=False の通常時は従来どおりの文言（回帰確認）。
    page.locator("#gsearch").fill("")
    page.locator("#showall").click()
    expect(page.locator("#gcount")).to_contain_text("ノード 4・関係 3")
    page.locator("#gsearch").fill("存在しない語")
    expect(page.locator("#gcount")).to_contain_text("一致なし")


def test_page_admin_only(page, web_base_url):
    """`/graph`・`/graph/facets`・`/graph/search`・`POST /graph/ask` は全て admin 限定 API のため、
    この画面全体を ingest.html（W1）と同じ「非 admin は access-denied だけ見せる」パターンで
    丸ごとガードする（CLEAN-1・W1 の残・同型是正・2026-09-03）。

    ingest.html と同じ nav の出し分け（`web/nav.js`）でも「ナレッジグラフ」タブは admin のみ表示。
    """
    from playwright.sync_api import expect

    install_api_mocks(page)                       # 既定=admin
    page.goto(f"{web_base_url}/graph.html")
    expect(page.locator("#main-content")).to_be_visible()
    expect(page.locator("#access-denied")).to_be_hidden()
    expect(page.locator('.nav a[href="graph.html"]')).to_be_visible()

    install_api_mocks(page, user=USER_MEMBER)     # 非 admin（後掛けの route が優先される）
    page.goto(f"{web_base_url}/graph.html")
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#access-denied")).to_contain_text("管理者権限が必要です")
    # nav にも「ナレッジグラフ」タブが出ない（admin-settings.html/ingest.html と同じ出し分け）。
    expect(page.locator('.nav a[href="graph.html"]')).to_have_count(0)

    page.route("**/auth/me", lambda route: route.fulfill(status=500, body="{}"))
    page.goto(f"{web_base_url}/graph.html")       # 判定失敗＝fail-safe で access-denied 側
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#access-denied")).to_be_visible()
