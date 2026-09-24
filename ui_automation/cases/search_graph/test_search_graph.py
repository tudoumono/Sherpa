from __future__ import annotations

import re

import pytest
from playwright.sync_api import expect

from ui_automation.support.control import start_isolated_neo4j, stop_isolated_neo4j
from ui_automation.support.database import usage_event_after, usage_event_checkpoint
from ui_automation.support.live_api import LiveApi


pytestmark = [pytest.mark.ui_automation, pytest.mark.search_graph, pytest.mark.destructive]


def test_real_search_and_graph_surfaces(
    admin_page,
    live_api,
    ui_config,
    artifact_case,
    real_world,
    isolated_stack,
):
    search_path = f"/admin/es/search?world={real_world}&query=SHERPA-LIVE-ALPHA-927"
    search = live_api.get_json(search_path, save_as="state/es-search.json")
    hits = search.get("hits") or []
    assert hits, "Elasticsearch returned no hit for the ingested fixture token"
    assert any("SHERPA-LIVE-ALPHA-927" in str(hit) for hit in hits)

    graph = live_api.get_json(f"/graph?world={real_world}&limit=0", save_as="state/graph.json")
    nodes = graph.get("nodes") or []
    assert any("TAXCALC" in str(node.get("name") or "") for node in nodes), "real graph contains no TAXCALC node from the COBOL fixture"
    assert graph.get("edges"), "real graph contains no relationship from the fixture"

    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    admin_page.locator("#esq").fill("SHERPA-LIVE-ALPHA-927")
    admin_page.locator("#esbtn").click()
    expect(admin_page.locator("#eshits")).to_contain_text("SHERPA-LIVE-ALPHA-927")
    artifact_case.attest_control_state(
        control_key="esq",
        state="normal",
        assertion="fixture固有検索語の入力が実Elasticsearch hit本文へ反映された",
    )
    expect(admin_page.locator("#eshits")).to_contain_text("SHERPA-LIVE-ALPHA-927")
    artifact_case.attest_control_state(
        control_key="esbtn",
        state="normal",
        assertion="全文検索buttonが実Elasticsearchへ問い合わせfixture固有hitを表示した",
    )
    artifact_case.screenshot(admin_page, 10, "search-real-elasticsearch-hit")

    stop_isolated_neo4j(ui_config, artifact_case)
    recovery_state = {"done": False}

    def ensure_neo4j_recovered() -> None:
        if recovery_state["done"]:
            return
        start_isolated_neo4j(ui_config, artifact_case)
        recovery_state["done"] = True

    artifact_case.add_cleanup("restore isolated Neo4j after retry case", ensure_neo4j_recovered)
    artifact_case.allow_http_error_console(
        method="GET",
        path="/graph",
        status=500,
        expected_count=2,
        reason="the case stops real Neo4j and requires both initial and retry graph requests to return HTTP 500",
    )
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and response.url.split("?", 1)[0].endswith("/graph"),
        timeout=ui_config.timeout_ms,
    ) as unavailable_graph_info:
        admin_page.goto(ui_config.base_url + "/ui/graph.html")
    unavailable_graph = unavailable_graph_info.value
    assert unavailable_graph.status >= 500, "graph endpoint did not expose the real Neo4j outage as a service failure"
    expect(admin_page.locator("#gcount")).to_have_text("読み込みに失敗しました")
    retry = admin_page.locator("#graph-retry")
    expect(retry).to_be_visible()
    artifact_case.screenshot(admin_page, 15, "graph-real-neo4j-outage-retry-visible")

    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and response.url.split("?", 1)[0].endswith("/graph"),
        timeout=ui_config.timeout_ms,
    ) as failed_retry_info:
        retry.click()
    assert failed_retry_info.value.status >= 500, "retry while Neo4j was stopped unexpectedly succeeded"
    expect(admin_page.locator("#gcount")).to_have_text("読み込みに失敗しました")
    expect(admin_page.locator("#graph-retry")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="graph-retry",
        state="abnormal",
        assertion="Neo4j実停止中の再試行が5xxとなり空graphを成功表示せず再試行状態を維持した",
    )

    ensure_neo4j_recovered()
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and response.url.split("?", 1)[0].endswith("/graph"),
        timeout=ui_config.timeout_ms,
    ) as recovered_graph_info:
        retry.click()
    assert recovered_graph_info.value.status == 200, recovered_graph_info.value.text()
    expect(admin_page.locator("#graph-loading")).to_be_hidden()
    expect(admin_page.locator("#gcount")).to_contain_text("ノード")
    expect(admin_page.locator("#graph-retry")).to_have_count(0)
    artifact_case.attest_control_state(
        control_key="graph-retry",
        state="normal",
        assertion="runner所有Neo4j復旧後の再試行が200となり実node件数とcanvasを復元した",
    )
    artifact_case.screenshot(admin_page, 18, "graph-retry-restored-real-neo4j-data")
    admin_page.locator("#gsearch").fill("TAXCALC")
    expect(admin_page.locator("#gcount")).not_to_contain_text("一致なし")
    artifact_case.attest_control_state(
        control_key="gsearch",
        state="normal",
        assertion="実graph内TAXCALC検索が一致なしにならず対象nodeを絞り込んだ",
    )
    admin_page.locator("#gsearch").fill("SHERPA-GRAPH-NO-MATCH-927")
    expect(admin_page.locator("#gcount")).to_contain_text(re.compile("一致なし|表示中には見つかりません"))
    artifact_case.attest_control_state(
        control_key="gsearch",
        state="abnormal",
        assertion="存在しないgraph語句を全件hitにせず一致なしの状態として表示した",
    )
    admin_page.locator("#gsearch").fill("")

    admin_page.locator("#fieldfilter").select_option("status")
    admin_page.locator("#opfilter").select_option("eq")
    admin_page.locator("#condvalue").fill("SHERPA-GRAPH-NO-MATCH-927")
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/graph/search?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as empty_condition_info:
        admin_page.locator("#gfilter").click()
    empty_condition_result = empty_condition_info.value.json()
    assert empty_condition_info.value.status == 200 and not (empty_condition_result.get("nodes") or [])
    expect(admin_page.locator("#gcount")).to_have_text("検索結果 0件")
    artifact_case.attest_control_state(
        control_key="condvalue",
        state="abnormal",
        assertion="該当しないfield値の実graph検索がnode 0件を返し偽hitを描画しなかった",
    )
    assert not (empty_condition_result.get("nodes") or [])
    artifact_case.attest_control_state(
        control_key="gfilter",
        state="abnormal",
        assertion="該当しないgraph条件の検索buttonが全件ではなく0件結果を表示した",
    )

    admin_page.locator("#greset").click()
    expect(admin_page.locator("#greset")).to_be_hidden()
    admin_page.locator("#opfilter").select_option("contains")
    admin_page.locator("#fieldfilter").select_option("status")
    admin_page.locator("#opfilter").select_option("eq")
    admin_page.locator("#condvalue").fill("active")
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/graph/search?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as condition_info:
        admin_page.locator("#gfilter").click()
    condition_result = condition_info.value.json()
    assert condition_info.value.status == 200 and condition_result.get("nodes")
    artifact_case.attest_control_state(
        control_key="fieldfilter",
        state="normal",
        assertion="status field選択を含む実graph検索が一致nodeを返した",
    )
    assert condition_result.get("nodes")
    artifact_case.attest_control_state(
        control_key="opfilter",
        state="normal",
        assertion="eq operator選択を含む実graph検索が一致nodeを返した",
    )
    assert condition_result.get("nodes")
    artifact_case.attest_control_state(
        control_key="condvalue",
        state="normal",
        assertion="active条件値を含む実graph検索が一致nodeを返した",
    )
    assert condition_result.get("nodes")
    artifact_case.attest_control_state(
        control_key="gfilter",
        state="normal",
        assertion="有効なfield条件の検索buttonが実Neo4j nodeを返して描画した",
    )
    expect(admin_page.locator("#greset")).to_be_visible()
    admin_page.locator("#greset").click()
    expect(admin_page.locator("#greset")).to_be_hidden()
    artifact_case.attest_control_state(
        control_key="greset",
        state="normal",
        assertion="graph検索reset操作が検索状態を解除しreset buttonを非表示にした",
    )

    legend_toggle = admin_page.locator(".graphlegend .ftog").first
    expect(legend_toggle).to_be_visible()
    facet_key = str(legend_toggle.get_attribute("data-ftype") or legend_toggle.get_attribute("data-fconf") or "")
    assert facet_key, "real graph legend exposed no selectable facet identity"
    artifact_case.arm_control(legend_toggle, control_key="@selector:.ftog")
    legend_toggle.click()
    expect(legend_toggle).to_have_class(re.compile(r"\boff\b"))
    artifact_case.attest_control_state(
        control_key="@selector:.ftog",
        state="normal",
        assertion=f"実graph facet {facet_key} を選択し対象legend行だけをoff表示へ変更した",
    )
    expect(admin_page.locator("#fclear")).to_be_visible()
    admin_page.locator("#fclear").click()
    expect(admin_page.locator("#fclear")).to_be_hidden()
    artifact_case.attest_control_state(
        control_key="fclear",
        state="normal",
        assertion="legend filterのclear操作が選択状態を解除しclear buttonを非表示にした",
    )

    show_all = admin_page.locator("#showall")
    if show_all.is_visible():
        with admin_page.expect_response(
            lambda response: response.request.method == "GET" and "/graph?" in response.url and "limit=0" in response.url,
            timeout=ui_config.timeout_ms,
        ) as show_all_info:
            show_all.click()
        assert show_all_info.value.status in {200, 304}
        artifact_case.attest_control_state(
            control_key="showall",
            state="normal",
            assertion="すべて表示操作がlimit 0の実graph取得を200または304で完了した",
        )
    else:
        assert graph.get("truncated") is False, "show-all control is hidden although the real graph response is truncated"

    admin_page.locator("#relayout").click()
    expect(admin_page.locator("#cy canvas")).not_to_have_count(0)
    artifact_case.attest_control_state(
        control_key="relayout",
        state="normal",
        assertion="再配置操作後も実graph canvasが存在し描画状態を維持した",
    )
    admin_page.locator("#fit").click()
    expect(admin_page.locator("#cy canvas")).not_to_have_count(0)
    artifact_case.attest_control_state(
        control_key="fit",
        state="normal",
        assertion="viewport fit操作後も実graph canvasが存在し操作可能な描画を維持した",
    )
    artifact_case.screenshot(admin_page, 20, "graph-real-taxcalc-node-filtered")

    selected = admin_page.evaluate(
        """() => {
          if (typeof cy === 'undefined' || !cy || cy.nodes().length === 0) return false;
          cy.nodes().first().emit('tap');
          return true;
        }"""
    )
    assert selected is True, "real graph did not expose an interactive node"
    contextual_ask = admin_page.locator("#nodecard [data-ask]")
    expect(contextual_ask).to_be_visible()
    selected_label = contextual_ask.get_attribute("data-ask")
    assert selected_label
    ask_authorization = artifact_case.arm_control_authorization(
        admin_page,
        control_key="@selector:[data-ask]",
    )
    assert ask_authorization["status"] == 200 and ask_authorization["role"] == "admin"
    contextual_ask.click()
    admin_page.wait_for_url("**/ui/chat.html**", timeout=ui_config.timeout_ms)
    expect(admin_page.locator("#input")).to_have_value(f"{selected_label}を変えたい。影響は？")
    artifact_case.attest_control_state(
        control_key="@selector:[data-ask]",
        state="normal",
        assertion="選択した実graph node名を含む影響質問がchat入力へ正確に引き渡された",
    )
    artifact_case.screenshot(admin_page, 30, "graph-node-context-question-transferred-to-chat")


def test_real_graph_facets_relationship_search_and_grounded_question(
    admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack
):
    graph = live_api.get_json(
        LiveApi.query("/graph", world=real_world, limit=0),
        save_as="state/graph-full.json",
    )
    edges = graph.get("edges") or []
    assert edges, "real fixture graph has no relationship to search"
    relationship = str(edges[0].get("type") or "")
    assert relationship
    facets = live_api.get_json("/graph/facets", save_as="state/graph-facets.json")
    assert relationship in (facets.get("relationship_types") or []), (
        "an observed graph relationship is absent from the real facet vocabulary"
    )
    assert {
        "category",
        "phase",
        "role",
        "top_scope",
        "status",
        "extraction_method",
    } <= set(facets.get("condition_fields") or [])
    searched = live_api.get_json(
        LiveApi.query("/graph/search", world=real_world, relationship=relationship),
        save_as="state/graph-relationship-search.json",
    )
    assert searched.get("nodes") and searched.get("edges"), "Neo4j relationship search returned no grounded result"

    admin_page.goto(ui_config.base_url + "/ui/graph.html")
    expect(admin_page.locator("#graph-loading")).to_be_hidden(timeout=ui_config.timeout_ms)
    expect(admin_page.locator(f'#relfilter option[value="{relationship}"]')).to_have_count(1)
    admin_page.locator("#relfilter").select_option(relationship)
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/graph/search?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as search_info:
        admin_page.locator("#gfilter").click()
    assert search_info.value.status == 200
    expect(admin_page.locator("#gcount")).to_contain_text("検索結果 ノード")
    artifact_case.attest_control_state(
        control_key="relfilter",
        state="normal",
        assertion="実Neo4j facet由来relationship選択が検索結果nodeを表示した",
    )
    expect(admin_page.locator("#gcount")).to_contain_text("検索結果 ノード")
    artifact_case.attest_control_state(
        control_key="gfilter",
        state="normal",
        assertion="relationship条件の検索buttonが実Neo4j結果を200で表示した",
    )
    artifact_case.screenshot(admin_page, 10, "graph-real-facet-relationship-search-results")

    question = "TAXCALC と NIGHTLY はどのようにつながっていますか。実グラフの経路を示してください。"
    usage_checkpoint = usage_event_checkpoint(ui_config.database_url, "graph_ask")
    admin_page.locator("#gask").fill(" ")
    admin_page.locator("#gaskbtn").click()
    expect(admin_page.locator("#ganswer")).to_be_empty()
    artifact_case.attest_control_state(
        control_key="gask",
        state="abnormal",
        assertion="空白だけのgraph質問をgrounded回答として送信せず回答欄を空のまま維持した",
    )
    expect(admin_page.locator("#ganswer")).to_be_empty()
    artifact_case.attest_control_state(
        control_key="gaskbtn",
        state="abnormal",
        assertion="空白質問で質問buttonを操作しても実回答や成功表示を生成しなかった",
    )
    admin_page.locator("#gask").fill(question)
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/graph/ask"),
        timeout=ui_config.timeout_ms,
    ) as ask_info:
        admin_page.locator("#gaskbtn").click()
    response = ask_info.value
    assert response.status == 200, response.text()
    answer = response.json()
    artifact_case.write_json("state/graph-grounded-answer.json", answer)
    assert answer.get("status") == "ok", "real graph question did not complete with graph evidence"
    assert answer.get("cited_nodes"), "real graph answer cited no Neo4j node"
    artifact_case.attest_control_state(
        control_key="gask",
        state="normal",
        assertion="実graph質問文がNeo4j根拠nodeを引用するok回答として処理された",
    )
    assert answer.get("cited_nodes")
    artifact_case.attest_control_state(
        control_key="gaskbtn",
        state="normal",
        assertion="質問buttonが実graph askを200完了しNeo4j引用nodeを返した",
    )
    assert "TAXCALC" in str(answer.get("answer") or answer.get("cited_nodes") or "")
    summary = answer.get("summary") or {}
    assert int(summary.get("graph_nodes") or 0) > 0 and int(summary.get("graph_edges") or 0) > 0
    usage = usage_event_after(
        ui_config.database_url,
        "graph_ask",
        usage_checkpoint,
        world=real_world,
    )
    artifact_case.record_usage_event(
        usage,
        turn_id=f"graph-ask:{usage['id']}",
        operation="graph-ask",
    )
    expect(admin_page.locator("#ganswer")).to_contain_text("TAXCALC", timeout=ui_config.timeout_ms)
    artifact_case.screenshot(admin_page, 20, "graph-real-ai-answer-with-cited-node-path")
