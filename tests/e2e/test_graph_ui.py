from __future__ import annotations

import json
import re

import pytest

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
    expect(answer).to_have_css("color", "rgb(185, 28, 28)")


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
    expect(answer).to_have_css("color", "rgb(185, 28, 28)")


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
    expect(answer).not_to_have_css("color", "rgb(185, 28, 28)")


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


def test_graph_name_search_focus_and_connection_navigation_restore_overview(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/graph.html")
    expect(page.locator("#graph-loading")).to_be_hidden()
    positions = page.evaluate("cy.nodes().map(n => ({id:n.id(), ...n.position()}))")
    page.get_by_label("表示中から名前で探す", exact=True).fill("TAXCALC")
    result = page.locator("#gresults .gresult")
    expect(result).to_have_count(1)
    result.focus()
    page.keyboard.press("Enter")
    expect(page.locator("#nodecard .nn")).to_have_text("TAXCALC")
    expect(page.locator("#gcount")).to_contain_text("周辺 3 ノード")
    assert page.evaluate("cy.nodes(':visible').length") == 3
    expect(page.locator("#nodecard")).to_contain_text("INVOKES")
    page.locator("#nodecard [data-node='data:w1:TAX-RATE']").focus()
    page.keyboard.press("Enter")
    expect(page.locator("#nodecard .nn")).to_have_text("TAX-RATE")
    expect(page.locator("#gselection-clear")).to_be_focused()
    expect(page.locator("#nodecard")).to_contain_text("税計算仕様書.md")
    assert page.evaluate("cy.getElementById('doc:w1:taxspec').visible()")
    page.keyboard.press("Enter")                     # 「解除」をキーボードで実行
    expect(page.locator("#gselection-clear")).to_be_hidden()
    expect(page.locator("#gsearch")).to_be_focused()   # 消えたボタンにフォーカスを残さない
    page.locator("#fit").click()
    expect(page.locator("#gcount")).to_have_text("ノード 4・関係 3")
    expect(page.locator("#nodecard")).to_be_hidden()
    assert page.evaluate("cy.nodes(':visible').length") == 4
    assert page.evaluate("cy.nodes().map(n => ({id:n.id(), ...n.position()}))") == positions


def test_graph_type_filter_keyboard_hides_edges_and_limits_name_results(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/graph.html")
    button = page.locator("[data-ftype='Module']")
    expect(button).to_have_attribute("aria-pressed", "true")
    button.focus()
    page.keyboard.press("Space")
    expect(button).to_have_attribute("aria-pressed", "false")
    # ボタンのARIA更新とCanvasのスタイル反映は別のタイミングになる。
    page.wait_for_function("cy.nodes(':visible').length === 3 && cy.edges(':visible').length === 1")
    assert page.evaluate("cy.nodes(':visible').length") == 3
    assert page.evaluate("cy.edges(':visible').length") == 1
    page.locator("#gsearch").fill("TAXCALC")
    expect(page.locator("#gresults .gresult")).to_have_count(0)
    page.locator("#fclear").click()
    expect(page.locator("#gresults .gresult")).to_have_count(1)
    page.locator("#gsearch").fill("")
    expect(page.locator("#gresults-section")).to_be_hidden()
    expect(page.locator("#gcount")).to_have_text("ノード 4・関係 3")


def test_graph_focus_keeps_chat_bridge(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/graph.html")
    expect(page.locator("#graph-loading")).to_be_hidden()
    page.locator("#gsearch").fill("TAXCALC")
    page.locator("#gresults .gresult").click()
    page.get_by_role("button", name="この語で影響を調べる").click()
    page.wait_for_url("**/chat.html")
    expect(page.locator("#input")).to_have_value("TAXCALCを変えたい。影響は？")


def test_graph_condition_search_empty_result_and_reset_clear_focus(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/graph/search?*", lambda route: route.fulfill(json={**GRAPH, "nodes": [], "edges": []}))
    page.goto(f"{web_base_url}/graph.html")
    expect(page.locator("#graph-loading")).to_be_hidden()
    page.locator("#gsearch").fill("TAXCALC")
    page.locator("#gresults .gresult").click()
    page.locator(".gconditions summary").click()
    page.locator("#relfilter").select_option("COPIES")
    page.locator("#gfilter").click()
    expect(page.locator("#gcount")).to_have_text("検索結果 0件")
    expect(page.locator("#cy")).to_contain_text("一致するグラフ要素はありません")
    expect(page.locator("#nodecard")).to_be_hidden()
    expect(page.locator("#gresults-section")).to_be_hidden()
    page.locator("#greset").click()
    expect(page.locator("#gcount")).to_have_text("ノード 4・関係 3")
    assert page.evaluate("cy.nodes(':visible').length") == 4


def test_graph_large_star_cycle_and_parallel_edges_remain_complete_and_stable(page, web_base_url):
    from playwright.sync_api import expect

    nodes = [{**GRAPH["nodes"][2], "id": f"n{i}", "name": f"プログラム {i:03}"} for i in range(500)]
    edges = [{"source": "n0", "target": f"n{i}", "type": "INVOKES", "status": "active"}
             for i in range(1, 500)]
    edges += [{"source": "n1", "target": "n0", "type": "COPIES", "status": "active"},
              {"source": "n0", "target": "n0", "type": "INVOKES", "status": "active"}]
    install_api_mocks(page)
    page.route(re.compile(r"/graph\?"), lambda route: route.fulfill(json={**GRAPH, "nodes": nodes, "edges": edges}))
    page.goto(f"{web_base_url}/graph.html")
    expect(page.locator("#graph-loading")).to_be_hidden()
    assert page.evaluate("cy.nodes(':visible').length") == 500
    assert page.evaluate("cy.edges(':visible').length") == 501
    assert page.evaluate("cy.edges().filter(e => e.style('curve-style') === 'bezier').length") == 3
    positions = page.evaluate("cy.nodes().map(n => n.position())")
    assert len({(p["x"], p["y"]) for p in positions}) == 500
    page.locator("#relayout").click()
    assert page.evaluate("cy.nodes().map(n => n.position())") == positions
    page.locator("#gsearch").fill("プログラム")
    expect(page.locator("#gresults .gresult")).to_have_count(50)
    expect(page.locator("#gresults-count")).to_have_text("500 件")
    expect(page.locator("#gresults")).to_contain_text("先頭 50 件")
    assert page.evaluate("cy.nodes('.hi').length") == 500
    page.locator("#gsearch").fill("プログラム 499")
    page.locator("#gresults .gresult").click()
    assert page.evaluate("cy.nodes(':visible').length") == 2
    page.locator("#gselection-clear").click()
    assert page.evaluate("cy.nodes(':visible').length") == 500


@pytest.mark.parametrize("theme", ["light", "dark"])
@pytest.mark.parametrize("size", [(1440, 1000), (640, 400), (390, 844)])
def test_graph_controls_do_not_cover_canvas_and_sidebar_is_reachable(page, web_base_url, theme, size):
    from playwright.sync_api import expect

    page.set_viewport_size({"width": size[0], "height": size[1]})
    page.add_init_script(f"localStorage.setItem('sherpa-theme', '{theme}')")
    install_api_mocks(page)
    page.goto(f"{web_base_url}/graph.html")
    expect(page.locator("#graph-loading")).to_be_hidden()
    page.locator(".gconditions summary").click()
    bounds = page.evaluate("""() => {
        const canvas = document.querySelector('#cy').getBoundingClientRect();
        const controls = document.querySelector('.graphstatus').getBoundingClientRect();
        const sidebar = document.querySelector('.graphlegend').getBoundingClientRect();
        return {canvasTop:canvas.top, canvasHeight:canvas.height, controlsBottom:controls.bottom,
                canvasWidth:canvas.width, sidebarWidth:sidebar.width,
                width:document.documentElement.scrollWidth, viewport:innerWidth};
    }""")
    assert bounds["canvasTop"] >= bounds["controlsBottom"]
    assert bounds["canvasHeight"] >= 200
    assert bounds["width"] == bounds["viewport"]
    if size[0] <= 760:
        assert bounds["sidebarWidth"] == bounds["canvasWidth"]
    page.locator("#gaskbtn").scroll_into_view_if_needed()
    page.locator("#gaskbtn").focus()
    expect(page.locator("#gaskbtn")).to_be_focused()
