from __future__ import annotations

import re

from mock_api import install_api_mocks


def test_audit_log_loads_and_filters(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/audit.html")

    expect(page.locator("#audit-tbody")).to_contain_text("auth.login")
    expect(page.locator("#audit-tbody")).to_contain_text("share.created")
    expect(page.locator("#pager-info")).to_contain_text("1–6 件")

    # S3: created_at はサーバの UTC（"2026-07-01T09:10:00+00:00"）を端末ロケール（JST・+9h・秒まで）に変換して表示。
    login_row = page.locator("#audit-tbody tr", has_text="auth.login").first
    expect(login_row).to_contain_text("2026-07-01 18:10:00")
    expect(login_row).not_to_contain_text("09:10")

    page.locator("#f-actor").fill("admin")
    page.locator("#f-action").fill("share.*")
    page.locator("#f-outcome").select_option("success")
    page.locator("#search-btn").click()

    expect(page.locator("#audit-tbody")).to_contain_text("share.created")
    expect(page.locator("#audit-tbody")).not_to_contain_text("auth.login_failed")
    query = records["audit_queries"][-1]
    assert query["actor"] == ["admin"]
    assert query["action"] == ["share.*"]
    assert query["outcome"] == ["success"]
    assert query["limit"] == ["100"]
    assert query["offset"] == ["0"]


def test_audit_log_exports_current_filters(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/audit.html")

    page.locator("#f-actor").fill("admin")
    page.locator("#f-action").fill("auth.*")
    with page.expect_download() as dl:
        page.locator("#export-csv").click()
    download = dl.value

    expect(page.locator("#export-status")).to_contain_text("sherpa-audit-20260701-120000.csv")
    assert download.suggested_filename == "sherpa-audit-20260701-120000.csv"
    query = records["audit_exports"][-1]
    assert query["format"] == ["csv"]
    assert query["actor"] == ["admin"]
    assert query["action"] == ["auth.*"]


def test_audit_columns_default_hides_detail_and_toggle_persists(page, web_base_url):
    """UI フィードバック6（2026-07-03）: 表示項目カスタマイズ。既定は「詳細」列だけ非表示・
    チェックボックスで表示切替・端末ローカル（localStorage）に保存されリロード後も維持される。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/audit.html")

    detail_cell = page.locator("#audit-tbody .col-detail").first
    ts_cell = page.locator("#audit-tbody .col-ts").first
    expect(ts_cell).to_be_visible()
    expect(detail_cell).to_be_hidden()
    expect(page.locator("#colcfg input[data-col='detail']")).not_to_be_checked()
    expect(page.locator("#colcfg input[data-col='ts']")).to_be_checked()

    page.locator("#colcfg input[data-col='detail']").check()
    expect(detail_cell).to_be_visible()

    page.locator("#colcfg input[data-col='ts']").uncheck()
    expect(ts_cell).to_be_hidden()

    # リロード後も localStorage の設定が復元される。
    page.reload()
    expect(page.locator("#audit-tbody .col-detail").first).to_be_visible()
    expect(page.locator("#audit-tbody .col-ts").first).to_be_hidden()
    expect(page.locator("#colcfg input[data-col='detail']")).to_be_checked()
    expect(page.locator("#colcfg input[data-col='ts']")).not_to_be_checked()


def test_audit_row_click_expands_full_detail_regardless_of_column_visibility(page, web_base_url):
    """UI フィードバック6: 「詳細」列が既定非表示でも、行クリック（or「詳細」ボタン）で
    その行だけ完全な detail JSON を展開できる。再クリックで閉じる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/audit.html")

    login_row = page.locator("#audit-tbody tr.audit-row", has_text="auth.login").first
    expect(page.locator("#audit-tbody .detail-row")).to_have_count(0)

    login_row.click()
    detail_row = page.locator("#audit-tbody .detail-row")
    expect(detail_row).to_have_count(1)
    expect(detail_row).to_contain_text("password")

    login_row.click()
    expect(page.locator("#audit-tbody .detail-row")).to_have_count(0)


def test_audit_pending_and_failure_rows_show_evidence_labels_and_request_id(page, web_base_url):
    """RV9 #7: POST /admin/usage/chat の pending→結果2行契約（fail-closed）が監査画面に正しく
    反映される: pending 行は「送信前記録」（中立色 oc-pending）・結果行は「失敗」（エラー色
    oc-error）として表示され、両者は同じ request_id で対応付けられる（outcome セルの title
    属性・行クリック展開の詳細先頭のどちらからも確認できる）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/audit.html")

    rows = page.locator("#audit-tbody tr.audit-row", has_text="admin.usage_chat_asked")
    expect(rows).to_have_count(2)
    pending_row = rows.nth(0)
    failure_row = rows.nth(1)

    expect(pending_row.locator(".col-outcome")).to_have_text("送信前記録")
    expect(pending_row.locator(".col-outcome")).to_have_class(re.compile(r"\boc-pending\b"))
    expect(failure_row.locator(".col-outcome")).to_have_text("失敗")
    expect(failure_row.locator(".col-outcome")).to_have_class(re.compile(r"\boc-error\b"))

    # request_id は title 属性で対応付け可能（pending 行・結果行とも同じ値）。
    pending_title = pending_row.locator(".col-outcome").get_attribute("title")
    failure_title = failure_row.locator(".col-outcome").get_attribute("title")
    assert pending_title == "request_id: req-usagechat-e2e-001"
    assert failure_title == "request_id: req-usagechat-e2e-001"

    # request_id を持たない行（既存の auth.login 等）は title 属性自体を持たない（空文字を出さない）。
    login_row = page.locator("#audit-tbody tr.audit-row", has_text="auth.login").first
    assert login_row.locator(".col-outcome").get_attribute("title") is None

    # 行クリック展開の詳細先頭にも request_id が出る（成功/失敗の対応付けに使える）。
    pending_row.click()
    detail_row = page.locator("#audit-tbody .detail-row")
    expect(detail_row).to_contain_text("request_id: req-usagechat-e2e-001")


def test_audit_outcome_filter_includes_pending_and_failure(page, web_base_url):
    """結果フィルタに pending・failure が選択肢として存在し、実際に絞り込める
    （`_filtered_audit_rows` の outcome 完全一致・audit.html のフィルタ選択肢）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/audit.html")

    page.locator("#f-outcome").select_option("pending")
    page.locator("#search-btn").click()
    expect(page.locator("#audit-tbody")).to_contain_text("送信前記録")
    expect(page.locator("#audit-tbody")).not_to_contain_text("auth.login")
    assert records["audit_queries"][-1]["outcome"] == ["pending"]

    page.locator("#f-outcome").select_option("failure")
    page.locator("#search-btn").click()
    expect(page.locator("#audit-tbody")).to_contain_text("失敗")
    expect(page.locator("#audit-tbody")).not_to_contain_text("送信前記録")
    assert records["audit_queries"][-1]["outcome"] == ["failure"]

