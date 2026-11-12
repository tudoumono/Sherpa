from __future__ import annotations

from mock_api import install_api_mocks


def test_workspace_upload_search_and_delete_flow(page, web_base_url, tmp_path):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.goto(f"{web_base_url}/workspace.html")

    expect(page.locator("#file-list")).to_contain_text("onboarding.md")
    expect(page.locator("#file-count")).to_have_text("(2 件)")
    expect(page.locator(".notice").first).to_contain_text("社内資料")

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


def test_workspace_delete_with_physical_failure_is_not_reported_as_deleted(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.on("dialog", lambda dialog: dialog.accept())
    page.route("**/workspace/files/501", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"ok": false, "id": 501, "rel_path": "onboarding.md", "physical_deleted": false}')
        if route.request.method == "DELETE" else route.fallback())
    page.goto(f"{web_base_url}/workspace.html")

    page.locator("[data-fileid='501']").click()
    expect(page.locator("#toast")).to_contain_text("ファイル本体を消せませんでした")
    expect(page.locator("#toast")).not_to_have_text("削除しました")


def test_workspace_search_shows_files_that_could_not_be_searched(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/workspace/search?**", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body='{"query": "x", "source": "個人ファイル内ヒット", "hits": [], "unsearched": ["broken.md"]}'))
    page.goto(f"{web_base_url}/workspace.html")

    page.locator("#search-q").fill("x")
    page.locator("#search-btn").click()
    expect(page.locator("#search-results")).to_contain_text("読み取れなかったファイルが 1 件")
    expect(page.locator("#search-results")).to_contain_text("broken.md")
    expect(page.locator("#search-results")).not_to_have_text("一致なし")
