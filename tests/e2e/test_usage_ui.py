from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest

from mock_api import USAGE_STATS_DEFAULT, install_api_mocks


def test_usage_trends_section_renders_all_new_metrics(page, web_base_url):
    """バッチ3（2026-07-03）: 「利用の傾向」セクション（ゼロヒット率・ヒートマップ・world別・
    頭脳別・週次アクティブ+再訪率・原本DL数）が実データで表示される。"""
    from playwright.sync_api import expect

    stats = json.loads(json.dumps(USAGE_STATS_DEFAULT))
    stats["quality_runs"]["by_rounds"][1]["condition"] = "depth2-quick"
    stats["quality_runs"]["by_rounds"][1]["rounds"] = 0
    install_api_mocks(page, usage_stats=stats)
    page.goto(f"{web_base_url}/usage.html")

    review = page.locator("#review-stats")
    expect(review.locator("table")).to_have_count(3)
    expect(review.locator("table").nth(0).locator("tbody tr")).to_have_count(1)
    expect(review.locator("table").nth(1).locator("tbody tr")).to_have_count(2)
    quality_table = review.locator("table").nth(2)
    expect(quality_table.locator("tbody tr")).to_have_count(2)
    expect(quality_table.locator("thead th").first).to_have_text("条件")
    expect(quality_table).to_contain_text("本番相当")
    expect(quality_table).to_contain_text("見直しなし")
    expect(review).to_contain_text("未調査: 3")
    expect(review).to_contain_text("ソース未確認: 2")   # 不足種別（閉集合）も平文ラベルで出る
    expect(review).to_contain_text("次の見直しへ: 4")
    expect(review).to_contain_text("0.36")

    # ゼロヒット率タイル（totals.zero_hit.rate=0.2142... → 21%）。
    expect(page.locator("#t-zerohit")).to_have_text("21%")

    # ランキング表にゼロヒット率列が追加され、seed どおりの値が入る（admin=27%, sato=0%）。
    admin_row = page.locator("#usage-tbody tr.u-row", has_text="admin").first
    expect(admin_row.locator(".zhr-cell")).to_have_text("27%")

    page.get_by_role("tab", name="利用者", exact=True).click()
    # ヒートマップ: 空状態が消え、SVG が描画される（セルが1つ以上存在する）。
    expect(page.locator("#heatmap-empty")).to_be_hidden()
    expect(page.locator("#heatmap-svg .cell")).to_have_count(24 * 7)

    # world別横棒: 空状態が消え、"test" ラベルの棒が描画される。
    expect(page.locator("#chart-world-empty")).to_be_hidden()
    expect(page.locator("#chart-world-svg")).to_contain_text("test")

    # 頭脳別横棒＋常設凡例（色だけに頼らない）。
    expect(page.locator("#chart-provider-empty")).to_be_hidden()
    legend = page.locator("#chart-provider-legend")
    expect(legend).to_contain_text("簡易（AIなし）")
    expect(legend).to_contain_text("OpenAI API")
    expect(legend).to_contain_text("Codex")

    # 週次アクティブユーザー＋再訪率（seed: revisit_rate=0.5 → 50%）。
    expect(page.locator("#chart-weekly-empty")).to_be_hidden()
    expect(page.locator("#revisit-rate-val")).to_have_text("50%")

    # 原本ダウンロード数（日別トレンド）＋見出し脇の期間合計（RV LOW再検証・seed: downloads.total=5）。
    expect(page.locator("#chart-dl-empty")).to_be_hidden()
    expect(page.locator("#dl-total-badge")).to_have_text("期間合計 5件")

    page.get_by_role("tab", name="トークン", exact=True).click()
    # F3（2026-07-07／2026-07-08 金額表示は撤去）: トークン（tiles・頭脳/モデル別表・日別チャート）。
    expect(page.locator("#t-tok-input")).to_have_text("12,000")
    expect(page.locator("#t-tok-output")).to_have_text("1,800")
    expect(page.locator("#chart-tokin-empty")).to_be_hidden()
    model_tbody = page.locator("#token-model-tbody")
    expect(model_tbody).to_contain_text("Codex")
    expect(model_tbody).to_contain_text("gpt-5.5")
    expect(page.locator("#token-user-tbody")).to_contain_text("管理者")


def test_usage_trends_section_handles_empty_data_without_crashing(page, web_base_url):
    """空データ（dev DB 掃除済み・少数データでも壊れない描画が必須）: 各セクションが正直に
    空状態を表示し、JS エラーで画面全体が壊れない。"""
    from playwright.sync_api import expect

    empty = {
        "users": [], "totals": {"turns": 0, "active_users": 0, "conversations": 0},
        "daily": [], "period": {"start": "2026-06-04", "end": "2026-07-03", "days": 30},
        "zero_hit": {"knowledge_turns": 0, "zero_hit_turns": 0, "rate": None},
        "worlds": [], "providers": [], "heatmap": [],
        "retention": {"weekly": [], "revisit_rate": None},
        "downloads": {"total": 0, "daily": []},
    }
    install_api_mocks(page, usage_stats=empty)
    page.goto(f"{web_base_url}/usage.html")

    expect(page.locator("#t-zerohit")).to_have_text("対象なし")
    page.get_by_role("tab", name="利用者", exact=True).click()
    expect(page.locator("#usage-tbody .empty-row")).to_be_visible()
    expect(page.locator("#review-stats table")).to_have_count(3)
    page.get_by_role("tab", name="品質", exact=True).click()
    expect(page.locator("#review-stats .hint", has_text="見直しの機能はこの環境では未導入です。")).to_be_visible()
    expect(page.locator("#review-stats tbody td")).to_have_text([
        "この期間の記録はありません。",
        "この期間の記録はありません。",
        "この期間の記録はありません。",
    ])

    page.get_by_role("tab", name="利用者", exact=True).click()
    expect(page.locator("#heatmap-empty")).to_be_visible()
    expect(page.locator("#chart-world-empty")).to_be_visible()
    expect(page.locator("#chart-provider-empty")).to_be_visible()
    expect(page.locator("#chart-weekly-empty")).to_be_visible()
    expect(page.locator("#chart-dl-empty")).to_be_visible()
    expect(page.locator("#dl-total-badge")).to_have_text("期間合計 0件")
    expect(page.locator("#revisit-rate-val")).to_have_text("算出できません（データ不足）")

    # 空状態でも「利用の傾向」の各カードタイトル自体は表示され続けている（画面が壊れていない）。
    expect(page.locator("text=フォルダ別利用量")).to_be_visible()
    expect(page.locator("text=頭脳（AI）別利用比率")).to_be_visible()

    # F3: トークン表示も空状態で壊れない（tokens キーが無い応答＝undefined でも空表示）。
    page.get_by_role("tab", name="トークン", exact=True).click()
    expect(page.locator("#t-tok-input")).to_have_text("未取得")
    expect(page.locator("#chart-tokin-empty")).to_be_visible()
    expect(page.locator("#token-model-tbody .empty-row")).to_be_visible()

    # 新6項目も、対応するキーが応答に全く無くても（旧 API 応答/計測前）壊れない。
    # (1)(4) 用途別・ユーザー別×用途別＝空/不在ならカードごと隠す（token-kind-card と同じ流儀）。
    expect(page.locator("#token-kind-card")).to_be_hidden()
    expect(page.locator("#token-user-kind-card")).to_be_hidden()
    # (2) 終了理由の分布＝既存の頭脳別/world別バーと同じ空状態表示・停止数バッジは0件表示。
    page.get_by_role("tab", name="品質", exact=True).click()
    expect(page.locator("#chart-stopkind-empty")).to_be_visible()
    expect(page.locator("#stopkind-total-badge")).to_have_text("利用者停止 未取得")
    # (3) 会話あたりのやり取り回数・resume率＝サマリタイルと同じ「—」表示（カードは隠さない）。
    expect(page.locator("#t-turns-avg")).to_have_text("未取得")
    expect(page.locator("#t-turns-max")).to_have_text("未取得")
    expect(page.locator("#t-resume-rate")).to_have_text("未取得")
    # (6) 回答時間の分布＝`overall` 行はつねに存在する契約なので「全体」行だけ「—」で描画される。
    rt_tbody = page.locator("#response-time-tbody")
    expect(rt_tbody).to_contain_text("全体")
    expect(rt_tbody).to_contain_text("未取得")
    # (5) 会話別上位＝空/不在ならカードごと隠す。
    expect(page.locator("#conversations-top-card")).to_be_hidden()
    # 打ち切りの内訳＝limits キーが応答に無くても表は空のまま（クラッシュしない）。
    expect(page.locator("#limits-tbody tr")).to_have_count(0)


def test_usage_stat4_new_metrics_render_with_default_seed(page, web_base_url):
    """（docs/archive/2026-09-12-利用統計の拡充2.md §2 (c)）: 見えていなかった6項目が
    USAGE_STATS_DEFAULT の値どおりに描画される。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html#tokens?days=30")

    # (1) 用途別（kind）表に所要時間の列（合計・平均・件数）。
    kind_tbody = page.locator("#token-kind-tbody")
    chat_row = kind_tbody.locator("tr", has_text="会話").first
    expect(chat_row).to_contain_text("未計測")        # chat 行は所要時間を持たない（API 契約＝None）
    expect(chat_row).not_to_contain_text("秒")
    embed_row = kind_tbody.locator("tr", has_text="検索の索引づくり")
    expect(embed_row).to_contain_text("未計測")       # elapsed_ms_total=None（報告不能マーカー）

    # (2) 終了理由の分布（平文ラベル）と停止数のバッジ。
    stopkind_svg = page.locator("#chart-stopkind-svg")
    expect(page.locator("#chart-stopkind-empty")).to_be_hidden()
    expect(stopkind_svg).to_contain_text("完了")
    expect(stopkind_svg).to_contain_text("14")
    expect(stopkind_svg).to_contain_text("調査の上限")
    expect(stopkind_svg).to_contain_text("不明")
    expect(page.locator("#stopkind-total-badge")).to_have_text("利用者停止 1件")

    # (3) 会話あたりのやり取り回数（avg/median/max/p90）と resume 率。
    expect(page.locator("#t-turns-avg")).to_have_text("3.0")
    expect(page.locator("#t-turns-median")).to_have_text("2.0")
    expect(page.locator("#t-turns-p90")).to_have_text("5.0")
    expect(page.locator("#t-turns-max")).to_have_text("6")
    expect(page.locator("#t-resume-rate")).to_have_text("50%")

    # (4) ユーザー別×用途別内訳。
    page.get_by_role("tab", name="トークン", exact=True).click()
    expect(page.locator("#token-user-kind-card")).to_be_visible()
    ukind_tbody = page.locator("#token-user-kind-tbody")
    admin_intent_row = ukind_tbody.locator("tr", has_text="依頼の仕分け").first
    expect(admin_intent_row).to_contain_text("管理者")
    expect(admin_intent_row).to_contain_text("0.9秒")   # elapsed_ms_total=900
    expect(admin_intent_row).to_contain_text("0.3秒")   # elapsed_ms_avg=300.0

    # (5) 会話別上位（トークン合計降順・1行目は admin の会話501）。会話 id はテキストのみ（リンクではない）。
    expect(page.locator("#conversations-top-card")).to_be_visible()
    top_rows = page.locator("#conversations-top-tbody tr")
    first_row = top_rows.first
    expect(first_row).to_contain_text("#501")
    expect(first_row).to_contain_text("管理者")
    expect(first_row).to_contain_text("test")
    expect(first_row).to_contain_text("4.5秒")   # response_time_avg_ms=4500.0
    expect(first_row.locator("a")).to_have_count(0)   # 会話 id はリンクにしない

    # (6) 回答時間の分布（全体＋経路別）。
    rt_tbody = page.locator("#response-time-tbody")
    expect(rt_tbody).to_contain_text("全体")
    overall_row = rt_tbody.locator("tr", has_text="全体")
    expect(overall_row).to_contain_text("9.0秒")   # p90=9000.0
    codex_row = rt_tbody.locator("tr", has_text="Codex")
    expect(codex_row).to_contain_text("4.5秒")   # avg=4500.0


def test_usage_limits_table_renders_by_provider(page, web_base_url):
    """打ち切りの内訳（`InvestigationState.limits`・経路別）が USAGE_STATS_DEFAULT の値どおりに描画される。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html#quality?days=30")

    limits_tbody = page.locator("#limits-tbody")
    codex_row = limits_tbody.locator("tr", has_text="Codex")
    expect(codex_row).to_contain_text("10")     # turns
    expect(codex_row).to_contain_text("3件（計5回）")     # tool_result_clipped
    expect(codex_row).to_contain_text("1件")              # total_budget_hit（bool 系・合計は出さない）
    expect(codex_row).to_contain_text("4件（計9回）")     # search_truncated
    expect(codex_row).to_contain_text("2件（計3回）")     # auto_continues
    openai_row = limits_tbody.locator("tr", has_text="OpenAI")
    expect(openai_row).to_contain_text("0件（計0回）")    # tool_result_clipped=0
    # S2/S4: 自動引き上げ・バックエンド不調の列も見出しと値が並ぶ（bool 系＝件数のみ）。
    heads = page.locator("#limits-tbody").locator("xpath=../thead//th")
    expect(heads).to_contain_text(["自動で深く調べた"])
    expect(heads).to_contain_text(["全文検索が使えなかった"])
    expect(heads).to_contain_text(["グラフが使えなかった"])
    expect(heads).to_contain_text(["グラフは再取り込み待ち"])
    codex_cells = dict(zip(heads.all_text_contents(), codex_row.locator("td").all_text_contents()))
    api_cells = dict(zip(heads.all_text_contents(), openai_row.locator("td").all_text_contents()))
    assert codex_cells["履歴の整理"] == "未計測"
    assert codex_cells["清書の打ち切り"] == "未計測"
    assert codex_cells["自動で深く調べた"] == "未計測"
    assert codex_cells["同じ条件の再検索を省略"] == "2件（計4回）"
    assert codex_cells["全文検索が使えなかった"] == "1件"
    assert codex_cells["グラフが使えなかった"] == "2件"
    assert codex_cells["グラフは再取り込み待ち"] == "1件"
    assert api_cells["履歴の整理"] == "1件（計1回）"
    assert api_cells["清書の打ち切り"] == "1件"
    assert api_cells["同じ条件の再検索を省略"] == "未計測"
    tooltip = page.locator(".chart-title", has_text="打ち切りの内訳").locator(".info-dot")
    expect(tooltip).to_have_attribute("title", re.compile(r"Codexで数える列.*API経路で数える列"))


def test_token_kind_table_renders(page, web_base_url):
    """S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: 「用途別」表に日本語 kind ラベルと、
    usage を報告しないプロバイダ（Gemini の embed）の null トークンに対する「—」表示を確認する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # USAGE_STATS_DEFAULT（tokens.by_kind に chat×2（codex/gemini）/intent/embed の 4 行を含む）
    page.goto(f"{web_base_url}/usage.html#tokens?days=30")

    kind_tbody = page.locator("#token-kind-tbody")
    expect(page.locator("#token-kind-card")).to_be_visible()
    expect(kind_tbody).to_contain_text("会話")          # kind=chat
    expect(kind_tbody).to_contain_text("依頼の仕分け")     # kind=intent
    expect(kind_tbody).to_contain_text("検索の索引づくり")  # kind=embed

    # gemini/embed 行はトークン列が全て null（報告不能マーカー）＝「—」で表示される。
    embed_row = kind_tbody.locator("tr", has_text="検索の索引づくり")
    expect(embed_row).to_contain_text("未計測")


def test_token_kind_table_hidden_when_absent(page, web_base_url):
    """`tokens.by_kind` が無い応答（旧 API 互換）でも pageerror なく「用途別」カードが隠れる。"""
    from playwright.sync_api import expect

    install_api_mocks(page, usage_stats={})
    page.goto(f"{web_base_url}/usage.html#tokens?days=30")

    expect(page.locator("#token-kind-card")).to_be_hidden()


def test_usage_period_switch_refetches_and_rerenders_trends(page, web_base_url):
    """期間切替（7/30/90日）に「利用の傾向」セクションも追従する。"""
    from playwright.sync_api import expect

    seven_day = dict(USAGE_STATS_DEFAULT)
    seven_day["period"] = {"start": "2026-06-27", "end": "2026-07-03", "days": 7}
    seven_day["zero_hit"] = {"knowledge_turns": 2, "zero_hit_turns": 1, "rate": 0.5}

    records = install_api_mocks(page, usage_stats=seven_day)
    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#t-zerohit")).to_have_text("50%")   # 初期表示（既定30日）でも同じモックが返る

    page.locator("[data-days='7']").click()
    expect(page.locator(".period-bar [data-days='7']")).to_have_class(re.compile(r"\bon\b"))
    assert records["admin_usage_stats"][-1]["days"] == ["7"]
    expect(page.locator("#t-zerohit")).to_have_text("50%")


def test_usage_custom_period_sends_jst_half_open_range(page, web_base_url):
    """開始日・終了日の指定は JST の [開始日 00:00, 終了日の翌日 00:00) で取得し、URL にも残す。
    開始日が終了日より後なら取得せずに理由を出す。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html#tokens?days=30")
    expect(page.locator("#usage-stat-panels")).to_be_visible()
    sent = len(records["admin_usage_stats"])

    page.locator("#period-start").fill("2026-02-10")
    page.locator("#period-end").fill("2026-02-01")
    page.locator("#period-range button[type=submit]").click()
    expect(page.locator("#period-range-error")).to_have_text("開始日は終了日以前にしてください")
    assert len(records["admin_usage_stats"]) == sent

    page.locator("#period-start").fill("2026-01-01")
    page.locator("#period-end").fill("2026-01-31")
    with page.expect_request(lambda r: "/admin/usage/stats?" in r.url) as req:
        page.locator("#period-range button[type=submit]").click()
    expect(page.locator("#period-range-error")).to_be_empty()
    expect(page).to_have_url(re.compile(r"#tokens\?start=2026-01-01&end=2026-01-31$"))
    expect(page.locator(".period-bar .filterchip.on")).to_have_count(0)
    query = parse_qs(urlparse(req.value.url).query)
    assert query["from"] == ["2026-01-01T00:00:00+09:00"]
    assert query["to"] == ["2026-02-01T00:00:00+09:00"]
    assert "days" not in query

    # URL に直接書いた不正な期間も、取得せずに理由を出す（開いた直後も同じ）。
    sent = len(records["admin_usage_stats"])
    page.goto(f"{web_base_url}/usage.html#tokens?start=2026-02-10&end=2026-02-01")
    expect(page.locator("#period-range-error")).to_have_text("開始日は終了日以前にしてください")
    page.reload()
    expect(page.locator("#period-range-error")).to_have_text("開始日は終了日以前にしてください")
    expect(page.locator("#usage-stat-panels")).to_be_hidden()
    page.locator("#usage-tab-quality").click()
    expect(page).to_have_url(re.compile(r"#quality\?start=2026-02-10&end=2026-02-01$"))
    assert len(records["admin_usage_stats"]) == sent


# ===== STAT-2: 統計チャットの「今回だけ」一時プロバイダ切替（保存しない・リクエスト単位） =====
# `POST /admin/usage/chat` は install_api_mocks の共通ハンドラに含まれないため、ここで個別に
# page.route を足す（既存の graph.html/ingest.html の e2e と同じ流儀・Playwright は後から登録した
# 方を先にマッチさせるため、install_api_mocks より後に登録すれば共通ハンドラを迂回できる）。

@pytest.mark.parametrize("embedding", ["", "?embed=1"])
def test_usage_overview_and_tabs_preserve_period_through_history(page, web_base_url, embedding):
    """概要から詳細へ進み、戻る・再読込でも同じ期間で調べられる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html{embedding}")
    expect(page.locator("#usage-standalone")).to_be_visible() if embedding else expect(page.locator("#usage-standalone")).to_be_hidden()
    expect(page.locator("#summary-tiles")).to_be_visible()
    expect(page.locator("#review-stats-card")).to_be_hidden()
    assert page.locator(".period-bar").bounding_box()["y"] < page.locator("#summary-tiles").bounding_box()["y"]
    page.locator('[data-days="7"]').click()
    expect(page).to_have_url(re.compile(r"#overview\?days=7$"))
    page.get_by_role("tab", name="品質", exact=True).click()
    expect(page.locator("#review-stats-card")).to_be_visible()
    expect(page.locator("#session-details")).not_to_have_attribute("open", "")
    page.get_by_role("tab", name="品質", exact=True).press("ArrowRight")
    expect(page.get_by_role("tab", name="トークン", exact=True)).to_be_focused()
    expect(page.locator("#token-tiles")).to_be_visible()
    page.go_back()
    expect(page.get_by_role("tab", name="品質", exact=True)).to_have_attribute("aria-selected", "true")
    page.reload()
    expect(page.locator("#review-stats-card")).to_be_visible()
    expect(page.locator('[data-days="7"]')).to_have_attribute("aria-pressed", "true")
    page.get_by_role("tab", name="概要", exact=True).click()
    expect(page.locator("#summary-tiles")).to_be_visible()
    expect(page.locator("#review-stats-card")).to_be_hidden()


@pytest.mark.parametrize("user_count", [10, 12])
def test_usage_metric_definitions_missing_values_and_export(page, web_base_url, user_count):
    """集計と母数の意味を確認し、0・未計測を区別した状態で保存できる。"""
    from playwright.sync_api import expect

    stats = json.loads(json.dumps(USAGE_STATS_DEFAULT))
    stats["tokens"]["totals"].update(input=0, output=None)
    stats["tokens"]["by_user"] = [
        {"uid": f"sample-user-{i}", "display_name": f"サンプル利用者{i}",
         "turns": 1, "input": 1000 - i, "output": 10}
        for i in range(user_count)
    ]
    install_api_mocks(page, usage_stats=stats)
    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#zero-hit-counts")).to_contain_text("3件 / 社内資料参照の回答 14件")
    page.locator("#usage-definitions summary").click()
    expect(page.locator("#usage-definitions")).to_contain_text("回答のない質問は対象外")
    expect(page.locator("#usage-period-label")).to_contain_text("JST")
    page.get_by_role("tab", name="品質", exact=True).click()
    page.locator("#session-details summary").click()
    expect(page.locator("#t-resume-rate")).to_have_text("50%")
    expect(page.locator("#session-counts")).to_have_text("IDあり 2件 / 対象会話 4件")
    expect(page.locator("#session-details")).to_contain_text("成功率ではありません")
    page.get_by_role("tab", name="トークン", exact=True).click()
    expect(page.locator("#t-tok-input")).to_have_text("0")
    expect(page.locator("#t-tok-output")).to_have_text("未計測")
    expect(page.locator("#token-user-tbody tr")).to_have_count(10)
    expect(page.locator("#token-user-tbody .user-uid")).to_have_text(
        [row["uid"] for row in stats["tokens"]["by_user"][:10]])
    page.locator("#usage-definitions summary").click()
    with page.expect_download() as download:
        page.locator("#usage-export").click()
    data = json.loads(download.value.path().read_text())
    assert data["period"] == stats["period"]
    assert data["stats"]["tokens"]["totals"]["output"] is None
    assert data["stats"]["tokens"]["by_user"] == stats["tokens"]["by_user"][:10]
    assert data["stats"]["tokens"]["by_user_kind"] == stats["tokens"]["by_user_kind"]
    assert "再開成功率は計測していません" in data["definitions"]
    assert data["retrieved_at"]


def test_usage_export_zip_button_requests_current_period(page, web_base_url):
    """「明細を保存（ZIP）」は画面が表示している期間（load() が /admin/usage/stats へ渡すのと
    同じクエリの組み立て）で /admin/usage/export を取得し、応答のファイル名で保存する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-export-detail")).to_be_enabled()

    with page.expect_download() as download:
        page.locator("#usage-export-detail").click()
    assert download.value.suggested_filename == "usage-detail-20260601-20260630.zip"
    assert records["admin_usage_export"][-1]["days"] == ["30"]

    page.locator('[data-days="7"]').click()
    expect(page.locator("#usage-export-detail")).to_be_enabled()
    with page.expect_download():
        page.locator("#usage-export-detail").click()
    assert records["admin_usage_export"][-1]["days"] == ["7"]

    page.locator("#period-start").fill("2026-01-01")
    page.locator("#period-end").fill("2026-01-31")
    page.locator("#period-range button[type=submit]").click()
    expect(page.locator("#usage-export-detail")).to_be_enabled()
    with page.expect_download():
        page.locator("#usage-export-detail").click()
    query = records["admin_usage_export"][-1]
    assert query["from"] == ["2026-01-01T00:00:00+09:00"]
    assert query["to"] == ["2026-02-01T00:00:00+09:00"]
    assert "days" not in query


def test_usage_failed_period_does_not_display_or_export_previous_data(page, web_base_url):
    """期間変更の取得失敗を、前期間の成功した値で隠さない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#summary-tiles")).to_be_visible()
    page.route("**/admin/usage/stats?days=7", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"detail":"集計失敗"}'))
    page.locator('[data-days="7"]').click()
    expect(page.locator("#usage-load-status")).to_contain_text("取得できませんでした")
    expect(page.locator("#usage-period-label")).to_have_text("7日間（取得失敗）")
    expect(page.locator("#summary-tiles")).to_be_hidden()
    expect(page.locator("#usage-export")).to_be_disabled()
    expect(page.locator("#usage-export-detail")).to_be_disabled()
    page.get_by_role("tab", name="トークン", exact=True).click()
    expect(page.locator("#token-tiles")).to_be_hidden()
    page.locator('[data-days="90"]').click()
    expect(page.locator("#token-tiles")).to_be_visible()
    expect(page.locator("#usage-export")).to_be_enabled()
    expect(page.locator("#usage-export-detail")).to_be_enabled()


def test_usage_embedded_condition_can_open_as_standalone(page, web_base_url):
    """管理画面のiframeから、条件をURLで再現できる単独表示へ移れる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html#usage-page")
    frame = page.frame_locator("#embed-frame-usage-page")
    expect(frame.locator("#summary-tiles")).to_be_visible()
    frame.locator('[data-days="7"]').click()
    frame.get_by_role("tab", name="品質", exact=True).click()
    frame.get_by_role("link", name="この条件で単独表示").click()
    expect(page).to_have_url(f"{web_base_url}/usage.html#quality?days=7")
    expect(page.locator("#review-stats-card")).to_be_visible()
    page.reload()
    expect(page.get_by_role("tab", name="品質", exact=True)).to_have_attribute("aria-selected", "true")
    expect(page.locator('[data-days="7"]')).to_have_attribute("aria-pressed", "true")


def test_usage_limits_preserve_recorded_values_and_mark_unmeasured(page, web_base_url):
    """経路によって記録値を隠さず、未計測を0件や対象外と混同しない。"""
    from playwright.sync_api import expect

    stats = json.loads(json.dumps(USAGE_STATS_DEFAULT))
    api = stats["limits"]["by_provider"][1]
    api.update(context_compactions_turns=0, context_compactions_total=0,
               duplicate_tool_call_turns=None, duplicate_tool_call_total=None,
               tool_calls_exhausted_turns=None, auto_continues_turns=None)
    del api["total_budget_hit_turns"]
    unknown = dict(api)
    unknown["provider"] = "unknown"
    for key in unknown:
        if key not in ("provider", "turns"):
            unknown[key] = None
    stats["limits"]["by_provider"].append(unknown)
    install_api_mocks(page, usage_stats=stats)
    page.goto(f"{web_base_url}/usage.html#quality?days=30")
    heads = page.locator("#limits-tbody").locator("xpath=../thead//th")
    expect(page.locator("#limits-tbody tr")).to_have_count(3)
    codex_cells, api_cells, unknown_cells = [
        dict(zip(heads.all_text_contents(), row.locator("td").all_text_contents()))
        for row in page.locator("#limits-tbody tr").all()
    ]
    assert codex_cells["履歴の整理"] == "未計測"
    assert codex_cells["清書の打ち切り"] == "未計測"
    assert codex_cells["自動で深く調べた"] == "未計測"
    assert api_cells["履歴の整理"] == "0件（計0回）"
    assert api_cells["同じ条件の再検索を省略"] == "未計測"
    assert api_cells["調査の回数上限に到達"] == "未計測"
    assert api_cells["自動継続"] == "未計測"
    assert api_cells["累計上限到達"] == "未計測"
    assert unknown_cells["経路"] == "不明"
    assert all(value == "未計測" for key, value in unknown_cells.items()
               if key not in ("経路", "対象ターン数"))


@pytest.mark.parametrize("missing", ["omitted", "null", "empty"])
def test_usage_zero_hit_missing_does_not_break_rendering(page, web_base_url, missing):
    """出典なし集計の欠落でも、描画関数が例外を出さず他の統計を読める。"""
    from playwright.sync_api import expect

    stats = json.loads(json.dumps(USAGE_STATS_DEFAULT))
    if missing == "omitted":
        del stats["zero_hit"]
    else:
        stats["zero_hit"] = None if missing == "null" else {}
    install_api_mocks(page, usage_stats=stats)
    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#summary-tiles")).to_be_visible()
    if missing == "omitted":
        page.evaluate("renderZeroHitTile()")
    else:
        page.evaluate("value => renderZeroHitTile(value)", stats["zero_hit"])
    expect(page.locator("#t-zerohit")).to_have_text("未取得")
    expect(page.locator("#zero-hit-counts")).to_contain_text("出典なし 未取得件")
    expect(page.locator("#t-turns")).to_have_text(str(stats["totals"]["turns"]))
    expect(page.locator("#usage-export")).to_be_enabled()
