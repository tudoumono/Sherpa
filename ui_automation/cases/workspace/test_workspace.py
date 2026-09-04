from __future__ import annotations

import hashlib

import pytest
from playwright.sync_api import expect


pytestmark = [pytest.mark.ui_automation, pytest.mark.workspace, pytest.mark.destructive]


def test_real_upload_search_download_delete(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    upload = ui_config.world_path / "datasets" / "tax-cases.csv"
    rejected_upload = ui_config.world_path / "media" / "text-evidence.pdf"
    original_hash = hashlib.sha256(upload.read_bytes()).hexdigest()
    admin_page.goto(ui_config.base_url + "/ui/workspace.html")
    admin_page.on("dialog", lambda dialog: dialog.accept())
    dropzone = admin_page.locator("#dropzone")
    file_input = admin_page.locator("#file-input")
    assert file_input.count() == 1, "dropzone must be backed by the real workspace file input"
    assert ".csv" in str(file_input.get_attribute("accept") or "")

    artifact_case.arm_control(dropzone, control_key="dropzone")
    with admin_page.expect_file_chooser(timeout=ui_config.timeout_ms) as rejected_chooser_info:
        dropzone.click()
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/workspace/files"),
        timeout=ui_config.timeout_ms,
    ) as rejected_upload_info:
        rejected_chooser_info.value.set_files(str(rejected_upload))
    assert rejected_upload_info.value.status == 422
    expect(admin_page.locator("#upload-msg")).to_contain_text("エラー")
    rejected_listing = live_api.get_json("/workspace/files", save_as="state/workspace-rejected-file-list.json")
    assert all(row.get("rel_path") != rejected_upload.name for row in rejected_listing.get("files") or [])
    artifact_case.attest_control_state(
        control_key="dropzone",
        state="abnormal",
        assertion="dropzoneから選択した許可外PDFを実upload APIが422で拒否しworkspace一覧へ保存しなかった",
    )
    artifact_case.attest_control_state(
        control_key="file-input",
        state="abnormal",
        assertion="file chooserへ指定した許可外PDFをupload完了表示にせず実workspace台帳へ追加しなかった",
    )
    artifact_case.screenshot(admin_page, 5, "workspace-dropzone-rejected-unsupported-real-pdf")

    artifact_case.arm_control(dropzone, control_key="dropzone")
    with admin_page.expect_file_chooser(timeout=ui_config.timeout_ms) as chooser_info:
        dropzone.click()
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/workspace/files"),
        timeout=ui_config.timeout_ms,
    ) as upload_info:
        chooser_info.value.set_files(str(upload))
    assert upload_info.value.status == 200
    assert upload_info.value.json().get("rel_path") == upload.name
    expect(admin_page.locator("#upload-msg")).to_contain_text(f"アップロード完了: {upload.name}")
    expect(admin_page.locator("#file-list")).to_contain_text(upload.name)
    artifact_case.attest_control_state(
        control_key="dropzone",
        state="normal",
        assertion="dropzoneの実file chooserから許可CSVを選択しupload APIと一覧へ同じ保存file名を反映した",
    )
    artifact_case.screenshot(admin_page, 10, "workspace-real-file-uploaded")

    listing = live_api.get_json("/workspace/files", save_as="state/workspace-files.json")
    matches = [row for row in listing.get("files", []) if row.get("rel_path") == upload.name]
    assert len(matches) == 1
    file_id = matches[0]["id"]

    refresh = admin_page.locator("button.btn-ghost", has_text="更新")
    artifact_case.arm_unkeyed_control(
        refresh,
        control_key="@unkeyed:web/workspace.html:96:button",
    )
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and response.url.endswith("/workspace/files"),
        timeout=ui_config.timeout_ms,
    ) as refresh_info:
        refresh.click()
    assert refresh_info.value.status == 200
    refreshed = refresh_info.value.json()
    assert {str(row.get("id")) for row in refreshed.get("files") or []} == {str(row.get("id")) for row in listing.get("files") or []}
    expect(admin_page.locator("#file-list")).to_contain_text(upload.name)
    expect(admin_page.locator("#file-count")).to_have_text(f"({len(refreshed.get('files') or [])} 件)")
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/workspace.html:96:button",
        state="normal",
        assertion="更新buttonが実workspace一覧APIを再取得しfile ID集合と画面件数を同じ最新値へ描画した",
    )
    artifact_case.screenshot(admin_page, 15, "workspace-refresh-reloaded-current-real-file-list")

    def cleanup_file() -> None:
        current = live_api.get_json("/workspace/files").get("files") or []
        if any(str(row.get("id")) == str(file_id) for row in current):
            live_api.delete_json(f"/workspace/files/{file_id}")

    artifact_case.add_cleanup(f"delete workspace file {file_id}", cleanup_file)
    downloaded = live_api.request("GET", f"/workspace/files/{file_id}/download", expected=200)
    assert hashlib.sha256(downloaded.body).hexdigest() == original_hash
    artifact_case.attest_control_state(
        control_key="file-input",
        state="normal",
        assertion="選択した実fileがworkspace一覧へ保存されdownload内容のhashも原本と一致した",
    )
    artifact_case.write_json(
        "state/workspace-download.json",
        {
            "file_id": file_id,
            "sha256": original_hash,
            "bytes": len(downloaded.body),
        },
    )

    admin_page.locator("#search-q").fill("SHERPA-LIVE-ALPHA-927")
    admin_page.locator("#search-btn").click()
    expect(admin_page.locator("#search-results")).to_contain_text("個人ファイル内ヒット")
    expect(admin_page.locator("#search-results")).to_contain_text(upload.name)
    artifact_case.attest_control_state(
        control_key="search-q",
        state="normal",
        assertion="fixture固有語を入力した検索結果に対象の実workspace fileだけが表示された",
    )
    artifact_case.attest_control_state(
        control_key="search-btn",
        state="normal",
        assertion="workspace検索操作後に固有語hit表示と対象file名を確認した",
    )
    artifact_case.screenshot(admin_page, 20, "workspace-real-grep-hit")

    with admin_page.expect_response(
        lambda response: response.request.method == "DELETE" and f"/workspace/files/{file_id}" in response.url
    ) as delete_info:
        admin_page.locator(f'[data-fileid="{file_id}"]').click()
    assert delete_info.value.status == 200
    expect(admin_page.locator("#file-list")).not_to_contain_text(upload.name)
    assert hashlib.sha256(upload.read_bytes()).hexdigest() == original_hash
    artifact_case.attest_control_state(
        control_key="@selector:[data-fileid]",
        state="normal",
        assertion="選択したworkspace fileだけが削除されfixture原本hashは変化しなかった",
    )
    artifact_case.screenshot(admin_page, 30, "workspace-file-deleted-source-preserved")

    artifact_case.arm_unkeyed_control(
        refresh,
        control_key="@unkeyed:web/workspace.html:96:button",
    )
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and response.url.endswith("/workspace/files"),
        timeout=ui_config.timeout_ms,
    ) as deleted_refresh_info:
        refresh.click()
    assert deleted_refresh_info.value.status == 200
    deleted_refresh = deleted_refresh_info.value.json()
    assert all(str(row.get("id")) != str(file_id) for row in deleted_refresh.get("files") or [])
    expect(admin_page.locator("#file-list")).not_to_contain_text(upload.name)
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/workspace.html:96:button",
        state="abnormal",
        assertion="削除後の更新で実APIから消えたfileを一覧へ復活させず古い成功状態として残さなかった",
    )
    artifact_case.screenshot(admin_page, 35, "workspace-refresh-kept-deleted-file-absent")


def test_workspace_file_share(browser, admin_page, live_api, ui_config, artifact_case, isolated_stack):
    upload = ui_config.world_path / "datasets" / "tax-cases.csv"
    admin_page.goto(ui_config.base_url + "/ui/workspace.html")
    admin_page.on("dialog", lambda dialog: dialog.accept())
    admin_page.locator("#file-input").set_input_files(str(upload))
    expect(admin_page.locator("#upload-msg")).to_contain_text(f"アップロード完了: {upload.name}")
    listing = live_api.get_json("/workspace/files", save_as="state/workspace-share-file.json")
    match = next(
        (row for row in listing.get("files") or [] if row.get("rel_path") == upload.name),
        None,
    )
    assert match, "workspace upload did not create a shareable file row"
    file_id = int(match["id"])
    artifact_case.add_cleanup(
        f"delete workspace share file {file_id}",
        lambda: live_api.request("DELETE", f"/workspace/files/{file_id}", expected={200, 404}),
    )
    artifact_case.screenshot(admin_page, 10, "workspace-file-ready-for-share-current-state")

    share_button = admin_page.locator(f'[data-share-fileid="{file_id}"]')
    expect(share_button).to_be_visible()
    share_button.click()
    overlay = admin_page.locator("#workspace-share-overlay")
    expect(overlay).to_be_visible()
    artifact_case.stop_trace(save=False)
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith(f"/workspace/files/{file_id}/shares"),
        timeout=ui_config.timeout_ms,
    ) as share_info:
        admin_page.locator("#workspace-share-submit").click()
    assert share_info.value.status == 200, share_info.value.text()
    shared = share_info.value.json()
    share_url = str(shared.get("url") or "")
    share_id = str(shared.get("share_id") or shared.get("id") or "")
    token = share_url.rsplit("/", 1)[-1]
    artifact_case.register_secret(token)
    artifact_case.register_secret(share_url)
    assert shared.get("ok") is True, "workspace share did not report success"
    assert token and share_id, "workspace share returned no token or share identifier"
    overlay.evaluate("element => { element.textContent = ''; element.hidden = true; }")
    artifact_case.start_trace(admin_page.context)
    artifact_case.screenshot(admin_page, 20, "workspace-share-created-and-secret-cleared")

    anonymous_context = browser.new_context(viewport={"width": 1100, "height": 760}, locale="ja-JP")
    anonymous_page = anonymous_context.new_page()
    artifact_case.attach_page(anonymous_page)
    try:
        anonymous_page.goto(ui_config.base_url + "/ui/login.html")
        fetched = anonymous_page.evaluate(
            """async (url) => {
              const response = await fetch(url, {credentials: 'omit'});
              const bytes = Array.from(new Uint8Array(await response.arrayBuffer()));
              return {status: response.status, bytes,
                      contentType: response.headers.get('content-type') || ''};
            }""",
            share_url,
        )
        assert fetched["status"] == 200, "anonymous workspace share URL did not permit the intended download"
        downloaded = bytes(fetched["bytes"])
        source = upload.read_bytes()
        assert downloaded == source, "anonymous workspace share returned different file content"
        artifact_case.write_json(
            "state/workspace-anonymous-share-access.json",
            {
                "share_id": share_id,
                "share_url_sha256": hashlib.sha256(share_url.encode()).hexdigest(),
                "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
                "status": fetched["status"],
                "content_type": fetched["contentType"],
                "download_sha256": hashlib.sha256(downloaded).hexdigest(),
                "source_sha256": hashlib.sha256(source).hexdigest(),
            },
        )
        artifact_case.screenshot(anonymous_page, 30, "workspace-share-downloaded-from-anonymous-context")

        revoke = admin_page.locator(
            f'[data-revoke-shareid="{share_id}"], [data-revoke-workspace-share="{share_id}"], #workspace-share-revoke',
        ).first
        expect(revoke).to_be_visible()
        with admin_page.expect_response(
            lambda response: response.request.method == "DELETE" and share_id in response.url,
            timeout=ui_config.timeout_ms,
        ) as revoke_info:
            revoke.click()
        assert revoke_info.value.status == 200, revoke_info.value.text()
        denied_after_revoke = anonymous_page.evaluate(
            """async (url) => {
              const response = await fetch(url, {credentials: 'omit'});
              return {status: response.status};
            }""",
            share_url,
        )
        assert denied_after_revoke["status"] in {403, 404, 410}, "revoked workspace share remained anonymously downloadable"
        artifact_case.screenshot(admin_page, 40, "workspace-share-revoked-by-owner-ui")

        with admin_page.expect_response(
            lambda response: response.request.method == "DELETE" and response.url.endswith(f"/workspace/files/{file_id}"),
            timeout=ui_config.timeout_ms,
        ) as delete_info:
            admin_page.locator(f'[data-fileid="{file_id}"]').click()
        assert delete_info.value.status == 200, delete_info.value.text()
        denied_after_delete = anonymous_page.evaluate(
            """async (url) => {
              const response = await fetch(url, {credentials: 'omit'});
              return {status: response.status};
            }""",
            share_url,
        )
        assert denied_after_delete["status"] in {403, 404, 410}
        artifact_case.write_json(
            "state/workspace-share-revocation.json",
            {
                "share_id": share_id,
                "anonymous_status_after_revoke": denied_after_revoke["status"],
                "anonymous_status_after_file_delete": denied_after_delete["status"],
                "original_source_sha256": hashlib.sha256(source).hexdigest(),
            },
        )
        artifact_case.screenshot(admin_page, 50, "workspace-file-deleted-share-remains-inaccessible")
    finally:
        anonymous_context.close()
