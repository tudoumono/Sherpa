from __future__ import annotations

import json
import re
from urllib.parse import parse_qs, urlparse

import pytest
from mock_api import USAGE_STATS_DEFAULT, install_api_mocks
from playwright.sync_api import expect

INVALID_RANGE = "開始日は終了日以前にしてください"


def _stats():
    return json.loads(json.dumps(USAGE_STATS_DEFAULT))


def _tab(page, name):
    return page.get_by_role("tab", name=name, exact=True)


def _open(page, web_base_url, path="usage.html", **mock_kw):
    records = install_api_mocks(page, **mock_kw)
    page.goto(f"{web_base_url}/{path}")
    return records


def _limit_cells(page):
    heads = page.locator("#limits-tbody").locator("xpath=../thead//th")
    return heads, [dict(zip(heads.all_text_contents(), row.locator("td").all_text_contents()))
                   for row in page.locator("#limits-tbody tr").all()]


def test_usage_trends_section_renders_all_new_metrics(page, web_base_url):
    """「利用の傾向」（ゼロヒット率・ヒートマップ・資料フォルダ別・頭脳別・週次アクティブ+再訪率・原本DL数）と
    トークンが実データで表示される。"""
    stats = _stats()
    stats["quality_runs"]["by_rounds"][1]["condition"] = "depth2-quick"
    stats["quality_runs"]["by_rounds"][1]["rounds"] = 0
    _open(page, web_base_url, usage_stats=stats)

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

    expect(page.locator("#t-zerohit")).to_have_text("21%")   # rate=0.2142...
    admin_row = page.locator("#usage-tbody tr.u-row", has_text="admin").first
    expect(admin_row.locator(".zhr-cell")).to_have_text("27%")

    _tab(page, "利用者").click()
    expect(page.locator("#heatmap-empty")).to_be_hidden()
    expect(page.locator("#heatmap-svg .cell")).to_have_count(24 * 7)
    expect(page.locator("#chart-world-empty")).to_be_hidden()
    expect(page.locator("#chart-world-svg")).to_contain_text("test")
    expect(page.locator("#chart-provider-empty")).to_be_hidden()
    legend = page.locator("#chart-provider-legend")   # 常設凡例（色だけに頼らない）
    expect(legend).to_contain_text("簡易（AIなし）")
    expect(legend).to_contain_text("OpenAI API")
    expect(legend).to_contain_text("Codex")
    expect(page.locator("#chart-weekly-empty")).to_be_hidden()
    expect(page.locator("#revisit-rate-val")).to_have_text("50%")
    expect(page.locator("#chart-dl-empty")).to_be_hidden()
    expect(page.locator("#dl-total-badge")).to_have_text("期間合計 5件")

    _tab(page, "トークン").click()
    expect(page.locator("#t-tok-input")).to_have_text("12,000")
    expect(page.locator("#t-tok-output")).to_have_text("1,800")
    expect(page.locator("#chart-tokin-empty")).to_be_hidden()
    model_tbody = page.locator("#token-model-tbody")
    expect(model_tbody).to_contain_text("Codex")
    expect(model_tbody).to_contain_text("gpt-5.5")
    expect(page.locator("#token-user-tbody")).to_contain_text("管理者")


def test_usage_trends_section_handles_empty_data_without_crashing(page, web_base_url):
    """空データでも各セクションが正直に空状態を表示し、JS エラーで画面全体が壊れない。
    対応するキーが応答に全く無くても（旧 API 応答/計測前）壊れない。"""
    empty = {
        "users": [], "totals": {"turns": 0, "active_users": 0, "conversations": 0},
        "daily": [], "period": {"start": "2026-06-04", "end": "2026-07-03", "days": 30},
        "zero_hit": {"knowledge_turns": 0, "zero_hit_turns": 0, "rate": None},
        "worlds": [], "providers": [], "heatmap": [],
        "retention": {"weekly": [], "revisit_rate": None},
        "downloads": {"total": 0, "daily": []},
    }
    _open(page, web_base_url, usage_stats=empty)

    expect(page.locator("#t-zerohit")).to_have_text("対象なし")
    _tab(page, "利用者").click()
    expect(page.locator("#usage-tbody .empty-row")).to_be_visible()
    expect(page.locator("#review-stats table")).to_have_count(3)
    _tab(page, "品質").click()
    expect(page.locator("#review-stats .hint", has_text="見直しの機能はこの環境では未導入です。")).to_be_visible()
    expect(page.locator("#review-stats tbody td")).to_have_text(["この期間の記録はありません。"] * 3)

    _tab(page, "利用者").click()
    for empty_id in ("heatmap", "chart-world", "chart-provider", "chart-weekly", "chart-dl"):
        expect(page.locator(f"#{empty_id}-empty")).to_be_visible()
    expect(page.locator("#dl-total-badge")).to_have_text("期間合計 0件")
    expect(page.locator("#revisit-rate-val")).to_have_text("算出できません（データ不足）")
    expect(page.locator("text=フォルダ別利用量")).to_be_visible()   # カードタイトル自体は表示され続ける
    expect(page.locator("text=頭脳（AI）別利用比率")).to_be_visible()

    _tab(page, "トークン").click()
    expect(page.locator("#t-tok-input")).to_have_text("未取得")
    expect(page.locator("#chart-tokin-empty")).to_be_visible()
    expect(page.locator("#token-model-tbody .empty-row")).to_be_visible()
    expect(page.locator("#token-kind-card")).to_be_hidden()   # 空/不在ならカードごと隠す
    expect(page.locator("#token-user-kind-card")).to_be_hidden()

    _tab(page, "品質").click()
    expect(page.locator("#chart-stopkind-empty")).to_be_visible()
    expect(page.locator("#stopkind-total-badge")).to_have_text("利用者停止 未取得")
    expect(page.locator("#t-turns-avg")).to_have_text("未取得")
    expect(page.locator("#t-turns-max")).to_have_text("未取得")
    expect(page.locator("#t-resume-rate")).to_have_text("未取得")
    rt_tbody = page.locator("#response-time-tbody")   # `overall` 行はつねに存在する契約
    expect(rt_tbody).to_contain_text("全体")
    expect(rt_tbody).to_contain_text("未取得")
    expect(page.locator("#conversations-top-card")).to_be_hidden()
    expect(page.locator("#limits-tbody tr")).to_have_count(0)   # limits キーが無くてもクラッシュしない


def test_usage_stat4_new_metrics_render_with_default_seed(page, web_base_url):
    """用途別・終了理由・会話あたりのやり取り・ユーザー別×用途別・会話別上位・回答時間が既定 seed の値どおりに描画される。"""
    _open(page, web_base_url, "usage.html#tokens?days=30")

    expect(page.locator("#token-kind-card")).to_be_visible()
    kind_tbody = page.locator("#token-kind-tbody")
    expect(kind_tbody).to_contain_text("会話")
    expect(kind_tbody).to_contain_text("依頼の仕分け")
    chat_row = kind_tbody.locator("tr", has_text="会話").first
    expect(chat_row).to_contain_text("未計測")        # chat 行は所要時間を持たない（API 契約＝None）
    expect(chat_row).not_to_contain_text("秒")
    embed_row = kind_tbody.locator("tr", has_text="検索の索引づくり")   # 報告不能マーカー（null）＝未計測
    expect(embed_row).to_contain_text("検索の索引づくり")
    expect(embed_row).to_contain_text("未計測")

    stopkind_svg = page.locator("#chart-stopkind-svg")
    expect(page.locator("#chart-stopkind-empty")).to_be_hidden()
    for text in ("完了", "14", "調査の上限", "不明"):
        expect(stopkind_svg).to_contain_text(text)
    expect(page.locator("#stopkind-total-badge")).to_have_text("利用者停止 1件")

    expect(page.locator("#t-turns-avg")).to_have_text("3.0")
    expect(page.locator("#t-turns-median")).to_have_text("2.0")
    expect(page.locator("#t-turns-p90")).to_have_text("5.0")
    expect(page.locator("#t-turns-max")).to_have_text("6")
    expect(page.locator("#t-resume-rate")).to_have_text("50%")

    _tab(page, "トークン").click()
    expect(page.locator("#token-user-kind-card")).to_be_visible()
    admin_intent_row = page.locator("#token-user-kind-tbody").locator("tr", has_text="依頼の仕分け").first
    expect(admin_intent_row).to_contain_text("管理者")
    expect(admin_intent_row).to_contain_text("0.9秒")   # elapsed_ms_total=900
    expect(admin_intent_row).to_contain_text("0.3秒")   # elapsed_ms_avg=300.0

    expect(page.locator("#conversations-top-card")).to_be_visible()
    first_row = page.locator("#conversations-top-tbody tr").first   # トークン合計降順・admin の会話501
    expect(first_row).to_contain_text("#501")
    expect(first_row).to_contain_text("管理者")
    expect(first_row).to_contain_text("test")
    expect(first_row).to_contain_text("4.5秒")
    expect(first_row.locator("a")).to_have_count(0)   # 会話 id はリンクにしない

    rt_tbody = page.locator("#response-time-tbody")
    expect(rt_tbody).to_contain_text("全体")
    expect(rt_tbody.locator("tr", has_text="全体")).to_contain_text("9.0秒")   # p90
    expect(rt_tbody.locator("tr", has_text="Codex")).to_contain_text("4.5秒")   # avg


def test_usage_limits_table_renders_by_provider(page, web_base_url):
    """打ち切りの内訳（経路別）が既定 seed の値どおりに描画される。"""
    _open(page, web_base_url, "usage.html#quality?days=30")

    limits_tbody = page.locator("#limits-tbody")
    codex_row = limits_tbody.locator("tr", has_text="Codex")
    expect(codex_row).to_contain_text("10")
    expect(codex_row).to_contain_text("3件（計5回）")
    expect(codex_row).to_contain_text("1件")              # bool 系・合計は出さない
    expect(codex_row).to_contain_text("4件（計9回）")
    expect(codex_row).to_contain_text("2件（計3回）")
    expect(limits_tbody.locator("tr", has_text="OpenAI")).to_contain_text("0件（計0回）")
    heads, (codex_cells, api_cells) = _limit_cells(page)
    for head in ("自動で深く調べた", "全文検索が使えなかった", "グラフが使えなかった", "グラフは再取り込み待ち"):
        expect(heads).to_contain_text([head])
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


def test_token_kind_table_hidden_when_absent(page, web_base_url):
    """`tokens.by_kind` が無い応答（旧 API 互換）でも pageerror なく「用途別」カードが隠れる。"""
    _open(page, web_base_url, "usage.html#tokens?days=30", usage_stats={})
    expect(page.locator("#token-kind-card")).to_be_hidden()


def test_usage_custom_period_sends_jst_half_open_range(page, web_base_url):
    """開始日・終了日の指定は JST の [開始日 00:00, 終了日の翌日 00:00) で取得し、URL にも残す。
    開始日が終了日より後なら取得せずに理由を出す。"""
    records = _open(page, web_base_url, "usage.html#tokens?days=30")
    expect(page.locator("#usage-stat-panels")).to_be_visible()
    sent = len(records["admin_usage_stats"])

    page.locator("#period-start").fill("2026-02-10")
    page.locator("#period-end").fill("2026-02-01")
    page.locator("#period-range button[type=submit]").click()
    expect(page.locator("#period-range-error")).to_have_text(INVALID_RANGE)
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

    # URL に直接書いた不正な期間も、取得せずに理由を出す（開いた直後も同じ）
    sent = len(records["admin_usage_stats"])
    page.goto(f"{web_base_url}/usage.html#tokens?start=2026-02-10&end=2026-02-01")
    expect(page.locator("#period-range-error")).to_have_text(INVALID_RANGE)
    page.reload()
    expect(page.locator("#period-range-error")).to_have_text(INVALID_RANGE)
    expect(page.locator("#usage-stat-panels")).to_be_hidden()
    page.locator("#usage-tab-quality").click()
    expect(page).to_have_url(re.compile(r"#quality\?start=2026-02-10&end=2026-02-01$"))
    assert len(records["admin_usage_stats"]) == sent


@pytest.mark.parametrize("embedding", ["", "?embed=1"])
def test_usage_overview_and_tabs_preserve_period_through_history(page, web_base_url, embedding):
    """概要から詳細へ進み、戻る・再読込でも同じ期間で調べられる。"""
    _open(page, web_base_url, f"usage.html{embedding}")
    if embedding:
        expect(page.locator("#usage-standalone")).to_be_visible()
    else:
        expect(page.locator("#usage-standalone")).to_be_hidden()
    expect(page.locator("#summary-tiles")).to_be_visible()
    expect(page.locator("#review-stats-card")).to_be_hidden()
    assert page.locator(".period-bar").bounding_box()["y"] < page.locator("#summary-tiles").bounding_box()["y"]
    page.locator('[data-days="7"]').click()
    expect(page).to_have_url(re.compile(r"#overview\?days=7$"))
    _tab(page, "品質").click()
    expect(page.locator("#review-stats-card")).to_be_visible()
    expect(page.locator("#session-details")).not_to_have_attribute("open", "")
    _tab(page, "品質").press("ArrowRight")
    expect(_tab(page, "トークン")).to_be_focused()
    expect(page.locator("#token-tiles")).to_be_visible()
    page.go_back()
    expect(_tab(page, "品質")).to_have_attribute("aria-selected", "true")
    page.reload()
    expect(page.locator("#review-stats-card")).to_be_visible()
    expect(page.locator('[data-days="7"]')).to_have_attribute("aria-pressed", "true")
    _tab(page, "概要").click()
    expect(page.locator("#summary-tiles")).to_be_visible()
    expect(page.locator("#review-stats-card")).to_be_hidden()


@pytest.mark.parametrize("user_count", [10, 12])
def test_usage_metric_definitions_missing_values_and_export(page, web_base_url, user_count):
    """集計と母数の意味を確認し、0・未計測を区別した状態で JSON に書き出せる。"""
    stats = _stats()
    stats["tokens"]["totals"].update(input=0, output=None)
    stats["tokens"]["by_user"] = [
        {"uid": f"sample-user-{i}", "display_name": f"サンプル利用者{i}",
         "turns": 1, "input": 1000 - i, "output": 10}
        for i in range(user_count)
    ]
    _open(page, web_base_url, usage_stats=stats)
    expect(page.locator("#zero-hit-counts")).to_contain_text("3件 / 社内資料参照の回答 14件")
    page.locator("#usage-definitions summary").click()
    expect(page.locator("#usage-definitions")).to_contain_text("回答のない質問は対象外")
    expect(page.locator("#usage-period-label")).to_contain_text("JST")
    _tab(page, "品質").click()
    page.locator("#session-details summary").click()
    expect(page.locator("#t-resume-rate")).to_have_text("50%")
    expect(page.locator("#session-counts")).to_have_text("IDあり 2件 / 対象会話 4件")
    expect(page.locator("#session-details")).to_contain_text("成功率ではありません")
    _tab(page, "トークン").click()
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
    """期間切替（7/30/90日）に統計が追従し、「明細を保存（ZIP）」は画面が表示している期間で /admin/usage/export を
    取得して応答のファイル名で保存する。"""
    seven_day = dict(USAGE_STATS_DEFAULT)
    seven_day["period"] = {"start": "2026-06-27", "end": "2026-07-03", "days": 7}
    seven_day["zero_hit"] = {"knowledge_turns": 2, "zero_hit_turns": 1, "rate": 0.5}
    records = _open(page, web_base_url, usage_stats=seven_day)
    expect(page.locator("#t-zerohit")).to_have_text("50%")
    export = page.locator("#usage-export-detail")
    expect(export).to_be_enabled()

    with page.expect_download() as download:
        export.click()
    assert download.value.suggested_filename == "usage-detail-20260601-20260630.zip"
    assert records["admin_usage_export"][-1]["days"] == ["30"]

    # 期間切替は画面遷移を挟んで取得を始めるため、取得要求が出るまでは旧期間のボタンが有効なまま残る。
    # 要求の発出（＝ボタンが取得中の無効状態に入った後）を待ってから有効化を待つ。
    with page.expect_request(lambda r: "/admin/usage/stats" in r.url and "days=7" in r.url):
        page.locator("[data-days='7']").click()
    expect(page.locator(".period-bar [data-days='7']")).to_have_class(re.compile(r"\bon\b"))
    expect(export).to_be_enabled()
    assert records["admin_usage_stats"][-1]["days"] == ["7"]
    expect(page.locator("#t-zerohit")).to_have_text("50%")
    with page.expect_download():
        export.click()
    assert records["admin_usage_export"][-1]["days"] == ["7"]

    page.locator("#period-start").fill("2026-01-01")
    page.locator("#period-end").fill("2026-01-31")
    with page.expect_request(lambda r: "/admin/usage/stats" in r.url and "from=" in r.url):
        page.locator("#period-range button[type=submit]").click()
    expect(export).to_be_enabled()
    with page.expect_download():
        export.click()
    query = records["admin_usage_export"][-1]
    assert query["from"] == ["2026-01-01T00:00:00+09:00"]
    assert query["to"] == ["2026-02-01T00:00:00+09:00"]
    assert "days" not in query


def test_usage_failed_period_does_not_display_or_export_previous_data(page, web_base_url):
    """期間変更の取得失敗を、前期間の成功した値で隠さない。"""
    _open(page, web_base_url)
    expect(page.locator("#summary-tiles")).to_be_visible()
    page.route("**/admin/usage/stats?days=7", lambda route: route.fulfill(
        status=500, content_type="application/json", body='{"detail":"集計失敗"}'))
    page.locator('[data-days="7"]').click()
    expect(page.locator("#usage-load-status")).to_contain_text("取得できませんでした")
    expect(page.locator("#usage-period-label")).to_have_text("7日間（取得失敗）")
    expect(page.locator("#summary-tiles")).to_be_hidden()
    expect(page.locator("#usage-export")).to_be_disabled()
    expect(page.locator("#usage-export-detail")).to_be_disabled()
    _tab(page, "トークン").click()
    expect(page.locator("#token-tiles")).to_be_hidden()
    page.locator('[data-days="90"]').click()
    expect(page.locator("#token-tiles")).to_be_visible()
    expect(page.locator("#usage-export")).to_be_enabled()
    expect(page.locator("#usage-export-detail")).to_be_enabled()


def test_usage_embedded_condition_can_open_as_standalone(page, web_base_url):
    """管理画面の iframe から、条件を URL で再現できる単独表示へ移れる。"""
    _open(page, web_base_url, "admin-settings.html#usage-page")
    frame = page.frame_locator("#embed-frame-usage-page")
    expect(frame.locator("#summary-tiles")).to_be_visible()
    frame.locator('[data-days="7"]').click()
    frame.get_by_role("tab", name="品質", exact=True).click()
    frame.get_by_role("link", name="この条件で単独表示").click()
    expect(page).to_have_url(f"{web_base_url}/usage.html#quality?days=7")
    expect(page.locator("#review-stats-card")).to_be_visible()
    page.reload()
    expect(_tab(page, "品質")).to_have_attribute("aria-selected", "true")
    expect(page.locator('[data-days="7"]')).to_have_attribute("aria-pressed", "true")


def test_usage_limits_preserve_recorded_values_and_mark_unmeasured(page, web_base_url):
    """経路によって記録値を隠さず、未計測を0件や対象外と混同しない。"""
    stats = _stats()
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
    _open(page, web_base_url, "usage.html#quality?days=30", usage_stats=stats)
    expect(page.locator("#limits-tbody tr")).to_have_count(3)
    _, (codex_cells, api_cells, unknown_cells) = _limit_cells(page)
    assert codex_cells["履歴の整理"] == "未計測"
    assert codex_cells["清書の打ち切り"] == "未計測"
    assert codex_cells["自動で深く調べた"] == "未計測"
    assert api_cells["履歴の整理"] == "0件（計0回）"
    for head in ("同じ条件の再検索を省略", "調査の回数上限に到達", "自動継続", "累計上限到達"):
        assert api_cells[head] == "未計測"
    assert unknown_cells["経路"] == "不明"
    assert all(value == "未計測" for key, value in unknown_cells.items()
               if key not in ("経路", "対象ターン数"))


@pytest.mark.parametrize("missing", ["omitted", "null", "empty"])
def test_usage_zero_hit_missing_does_not_break_rendering(page, web_base_url, missing):
    """出典なし集計の欠落でも、描画関数が例外を出さず他の統計を読める。"""
    stats = _stats()
    if missing == "omitted":
        del stats["zero_hit"]
    else:
        stats["zero_hit"] = None if missing == "null" else {}
    _open(page, web_base_url, usage_stats=stats)
    expect(page.locator("#summary-tiles")).to_be_visible()
    if missing == "omitted":
        page.evaluate("renderZeroHitTile()")
    else:
        page.evaluate("value => renderZeroHitTile(value)", stats["zero_hit"])
    expect(page.locator("#t-zerohit")).to_have_text("未取得")
    expect(page.locator("#zero-hit-counts")).to_contain_text("出典なし 未取得件")
    expect(page.locator("#t-turns")).to_have_text(str(stats["totals"]["turns"]))
    expect(page.locator("#usage-export")).to_be_enabled()
