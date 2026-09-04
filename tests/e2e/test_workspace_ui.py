from __future__ import annotations

from mock_api import install_api_mocks


def test_workspace_upload_search_and_delete_flow(page, web_base_url, tmp_path):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(f"{web_base_url}/workspace.html")

    expect(page.locator("#file-list")).to_contain_text("onboarding.md")
    expect(page.locator("#file-count")).to_have_text("(2 件)")
    expect(page.locator(".notice").first).to_contain_text("共有ナレッジベース")

    upload = tmp_path / "memo.md"
    upload.write_text("TAX-RATE は消費税率の個人メモです。\n", encoding="utf-8")
    page.locator("#file-input").set_input_files(str(upload))

    expect(page.locator("#upload-msg")).to_contain_text("アップロード完了: memo.md")
    expect(page.locator("#file-list")).to_contain_text("memo.md")
    assert records["workspace_uploads"][-1]["filename"] == "memo.md"

    page.locator("#search-q").fill("TAX-RATE")
    page.locator("#search-btn").click()
    expect(page.locator("#search-results")).to_contain_text("個人ファイル内ヒット")
    expect(page.locator("#search-results")).to_contain_text("notes/tax.csv")
    assert records["workspace_search"][-1]["q"] == ["TAX-RATE"]

    page.locator("[data-fileid='501']").click()
    expect(page.locator("#file-list")).not_to_contain_text("onboarding.md")
    assert records["workspace_delete"][-1] == 501
