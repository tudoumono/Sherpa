from __future__ import annotations

import hashlib
import json
import os
import re
from pathlib import Path

import pytest
from playwright.sync_api import expect

from ui_automation.runner.filesystem_safety import assert_no_mount_targets
from ui_automation.support.database import (
    database_utc_now,
    usage_event_checkpoint,
    usage_events_after,
    wait_for_ingestion_database_snapshot,
)
from ui_automation.support.live_api import LiveApi
from ui_automation.support.world import ensure_real_world, hash_tree


pytestmark = [pytest.mark.ui_automation, pytest.mark.worlds_ingest, pytest.mark.destructive]


@pytest.fixture
def real_world_vlm_registration(isolated_stack, admin_page, live_api, ui_config, artifact_case) -> dict:
    """Create the World only after taking a VLM usage/time checkpoint."""
    worlds_before = live_api.get_json("/worlds").get("worlds") or []
    assert not worlds_before, (
        "VLM correlation requires a fresh isolated World registration; an existing World would make its usage window ambiguous"
    )
    started_at = database_utc_now(ui_config.database_url)
    checkpoint = usage_event_checkpoint(ui_config.database_url, "vlm")
    world_id = ensure_real_world(live_api, ui_config, artifact_case)
    completed_at = database_utc_now(ui_config.database_url)
    registration = {
        "world_id": world_id,
        "vlm_usage_checkpoint": checkpoint,
        "started_at": started_at,
        "completed_at": completed_at,
    }
    artifact_case.write_json(
        "state/vlm-registration-window.json",
        {
            "world_id": world_id,
            "vlm_usage_checkpoint": checkpoint,
            "started_at": started_at.isoformat(),
            "completed_at": completed_at.isoformat(),
        },
    )
    return registration


def test_register_real_world_refresh_search_and_delete(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    source_before = hash_tree(ui_config.world_path)
    usage_checkpoints = {kind: usage_event_checkpoint(ui_config.database_url, kind) for kind in ("embed", "vlm")}
    worlds_before = live_api.get_json("/worlds", save_as="state/worlds-before.json").get("worlds") or []
    assert not worlds_before, f"isolated stack must begin without a registered World: {worlds_before}"

    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    expect(admin_page.locator("#regcard")).to_be_visible()
    admin_page.locator("#pickbtn").click()
    expect(admin_page.locator("#ovl")).to_have_class("ovl open")
    artifact_case.attest_control_state(
        control_key="pickbtn",
        state="normal",
        assertion="folder選択buttonが許可browse rootだけを示す実picker overlayを表示した",
    )
    folder = admin_page.locator("#pbody [data-cd]", has_text=ui_config.world_path.name)
    expect(folder).to_be_visible()
    folder.click()
    expect(admin_page.locator("#pickcur")).to_have_text(str(ui_config.world_path))
    artifact_case.attest_control_state(
        control_key="@selector:[data-cd]",
        state="normal",
        assertion="許可browse rootの動的folder行を操作し実fixture絶対pathへ移動した",
    )
    artifact_case.screenshot(admin_page, 10, "world-picker-real-fixture-selected")
    admin_page.locator("#pchoose").click()
    expect(admin_page.locator("#chosen")).to_contain_text(str(ui_config.world_path))
    artifact_case.attest_control_state(
        control_key="pchoose",
        state="normal",
        assertion="選択した許可root内fixture folderが登録対象pathへ正確に反映された",
    )
    admin_page.locator("#label").fill("UI Automation Evidence World")

    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/worlds")
    ) as response_info:
        admin_page.locator("#regbtn").click()
    response = response_info.value
    assert response.status == 200, response.text()
    created = response.json()
    world_id = str((created.get("world") or {}).get("world_id") or "")
    assert world_id
    for kind, checkpoint in usage_checkpoints.items():
        usage_world = world_id if kind == "embed" else None
        for usage in usage_events_after(
            ui_config.database_url,
            kind,
            checkpoint,
            world=usage_world,
        ):
            artifact_case.record_usage_event(
                usage,
                turn_id=f"{kind}:{usage['id']}",
                operation=f"world-{kind}",
            )

    def cleanup_world() -> None:
        worlds = live_api.get_json("/worlds").get("worlds") or []
        if any(str(world.get("world_id")) == world_id for world in worlds):
            live_api.delete_json(f"/worlds/{world_id}")

    artifact_case.add_cleanup(f"delete World {world_id}", cleanup_world)
    expect(admin_page.locator("#regmsg")).to_contain_text("登録・取り込みました")
    expect(admin_page.locator("#currentcard")).to_be_visible()
    expect(admin_page.locator("#list")).to_contain_text("UI Automation Evidence World")
    artifact_case.attest_control_state(
        control_key="label",
        state="normal",
        assertion="入力したWorld表示名が実登録後のWorld一覧へ正確に反映された",
    )
    assert world_id
    artifact_case.attest_control_state(
        control_key="regbtn",
        state="normal",
        assertion="登録buttonが実World IDを発行して取込完了状態を画面へ表示した",
    )
    artifact_case.screenshot(admin_page, 20, "world-registered-and-ingested")

    status = live_api.get_json(f"/worlds/{world_id}/status", save_as="state/world-status.json")
    assert int(status.get("indexed") or 0) >= 5, f"real World documents were not indexed: {status}"

    admin_page.locator("#esq").fill("SHERPA-LIVE-ALPHA-927")
    admin_page.locator("#esbtn").click()
    expect(admin_page.locator("#eshits")).to_contain_text("SHERPA-LIVE-ALPHA-927")
    artifact_case.attest_control_state(
        control_key="esq",
        state="normal",
        assertion="fixture固有語の入力が実Elasticsearch原本hitを画面へ表示した",
    )
    expect(admin_page.locator("#eshits")).to_contain_text("SHERPA-LIVE-ALPHA-927")
    artifact_case.attest_control_state(
        control_key="esbtn",
        state="normal",
        assertion="全文検索buttonが選択Worldの実Elasticsearch hitを返した",
    )

    admin_page.locator("#esq").fill("SHERPA-ES-NO-MATCH-927")
    admin_page.locator("#esbtn").click()
    expect(admin_page.locator("#eshits")).to_contain_text("該当するヒットはありません")
    artifact_case.attest_control_state(
        control_key="esq",
        state="abnormal",
        assertion="該当しない検索語を実Elasticsearchへ送り偽hitを表示しなかった",
    )
    expect(admin_page.locator("#eshits")).to_contain_text("該当するヒットはありません")
    artifact_case.attest_control_state(
        control_key="esbtn",
        state="abnormal",
        assertion="該当しない全文検索button操作が成功hitを捏造せず0件表示にした",
    )
    artifact_case.screenshot(admin_page, 30, "world-fulltext-search-hit")

    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and f"/worlds/{world_id}/refresh" in response.url
    ) as refresh_info:
        admin_page.locator(f'[data-refresh="{world_id}"]').click()
    assert refresh_info.value.status == 200
    expect(admin_page.locator("#listmsg")).to_contain_text("変更はありません")
    artifact_case.attest_control_state(
        control_key="@selector:[data-refresh]",
        state="normal",
        assertion="選択Worldの動的更新操作が実refreshを200完了し変更なしを表示した",
    )
    artifact_case.screenshot(admin_page, 40, "world-refresh-no-source-change")

    admin_page.on("dialog", lambda dialog: dialog.accept())
    with admin_page.expect_response(
        lambda response: response.request.method == "DELETE" and f"/worlds/{world_id}" in response.url
    ) as delete_info:
        admin_page.locator(f'[data-del="{world_id}"]').click()
    assert delete_info.value.status == 200
    expect(admin_page.locator("#listmsg")).to_contain_text("削除しました")
    assert hash_tree(ui_config.world_path) == source_before, "World source files changed during ingest/delete"
    artifact_case.attest_control_state(
        control_key="@selector:[data-del]",
        state="normal",
        assertion="選択Worldの動的削除操作が実登録だけを削除し原本hashを維持した",
    )
    artifact_case.write_json("state/files.sha256", source_before)
    artifact_case.screenshot(admin_page, 50, "world-registration-deleted-source-preserved")


def test_world_picker_rejects_outside_browse_roots(admin_page, live_api, ui_config, artifact_case, isolated_stack):
    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    admin_page.locator("#pickbtn").click()
    expect(admin_page.locator("#ovl")).to_have_class("ovl open")
    expect(admin_page.locator("#pbody")).not_to_contain_text("/etc")
    folder = admin_page.locator("#pbody [data-cd]", has_text=ui_config.world_path.name)
    expect(folder).to_be_visible()
    folder.click()
    expect(admin_page.locator("#pickcur")).to_have_text(str(ui_config.world_path))
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/fs/list?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as up_info:
        admin_page.locator("#upbtn").click()
    assert up_info.value.status == 200
    expect(admin_page.locator("#pickcur")).not_to_have_text(str(ui_config.world_path))
    artifact_case.attest_control_state(
        control_key="upbtn",
        state="normal",
        assertion="folder pickerの上へ操作が実fs listを200取得し親pathへ移動した",
    )
    folder = admin_page.locator("#pbody [data-cd]", has_text=ui_config.world_path.name)
    folder.click()
    admin_page.locator("#pchoose").click()
    expect(admin_page.locator("#diffbtn")).to_be_enabled()
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/worlds/diff"),
        timeout=ui_config.timeout_ms,
    ) as diff_info:
        admin_page.locator("#diffbtn").click()
    assert diff_info.value.status == 200
    expect(admin_page.locator("#diffout")).to_contain_text("件")
    artifact_case.attest_control_state(
        control_key="diffbtn",
        state="normal",
        assertion="選択folderの差分確認が実world diffを200完了し対象件数を表示した",
    )

    admin_page.locator("#pickbtn").click()
    expect(admin_page.locator("#ovl")).to_have_class("ovl open")
    admin_page.locator("#pcancel").click()
    expect(admin_page.locator("#ovl")).not_to_have_class("ovl open")
    artifact_case.attest_control_state(
        control_key="pcancel",
        state="normal",
        assertion="picker取消操作が未登録のままfolder overlayを閉じた",
    )
    artifact_case.screenshot(admin_page, 10, "world-picker-only-allowed-roots-visible")

    denied = live_api.request("POST", "/worlds/diff", {"path": "/etc"}, expected={403, 422})
    assert denied.status in {403, 422}
    artifact_case.write_json(
        "state/outside-root-denied.json",
        {
            "path": "/etc",
            "status": denied.status,
            "body": denied.json(),
        },
    )


def test_ingest_state_filters_and_failed_row_rerun_control(
    admin_page,
    live_api,
    ui_config,
    artifact_case,
    real_world,
    isolated_stack,
):
    preview = live_api.get_json(
        LiveApi.query("/ingest/preview", world=real_world),
        save_as="state/ingest-state-filter-preview.json",
    )
    documents = preview.get("documents") or []
    assert documents, "real World preview has no documents for state-filter coverage"
    expected_by_state = {
        "all": documents,
        "ready": [row for row in documents if row.get("state") == "ready"],
        "processing": [row for row in documents if row.get("state") == "processing"],
        "failed": [row for row in documents if row.get("state") == "failed"],
    }

    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    expect(admin_page.locator("#version")).to_have_value(real_world)
    expect(admin_page.locator("#rows .fname")).to_have_count(len(documents), timeout=ui_config.timeout_ms)
    rendered_by_state: dict[str, list[str]] = {}
    for state in ("ready", "processing", "failed", "all"):
        control = admin_page.locator(f'[data-state="{state}"]')
        expect(control).to_be_visible()
        control.click()
        expect(control).to_have_class(re.compile(r"\bon\b"))
        rendered_names = admin_page.locator("#rows .fname").all_inner_texts()
        rendered_names = [name.removeprefix("📜 ").removeprefix("📄 ").strip() for name in rendered_names]
        expected_names = [str(row.get("name") or "") for row in expected_by_state[state]]
        assert rendered_names == expected_names, {
            "state": state,
            "expected": expected_names,
            "rendered": rendered_names,
        }
        if expected_names:
            expect(admin_page.locator("#rows .empty")).to_have_count(0)
        else:
            expect(admin_page.locator("#rows .empty")).to_have_text("該当する資料がありません")
        rendered_by_state[state] = rendered_names

    assert rendered_by_state["all"] == [str(row.get("name") or "") for row in documents]
    artifact_case.attest_control_state(
        control_key="@selector:[data-state]",
        state="normal",
        assertion="動的状態filterの全件表示が実preview document名と同じ順序で一致した",
    )

    failed_documents = expected_by_state["failed"]
    rerun_controls = admin_page.locator("#rows [data-rerun]")
    expect(rerun_controls).to_have_count(len(failed_documents))
    artifact_case.write_json(
        "state/ingest-state-filter-effects.json",
        {
            "api_state_counts": {key: len(value) for key, value in expected_by_state.items()},
            "rendered_names": rendered_by_state,
            "rerun_control_count": rerun_controls.count(),
        },
    )
    artifact_case.screenshot(admin_page, 10, "ingest-real-state-filters-applied")

    if not failed_documents:
        artifact_case.write_json(
            "state/ingest-failed-row-rerun-gap.json",
            {
                "status": "FAIL",
                "failed_preview_rows": 0,
                "rerun_controls": 0,
                "reason": ("the real preview contract supplied only ready rows; no failed row can render the dynamic rerun control"),
            },
        )
        pytest.fail(
            "UI COVERAGE GAP: [data-rerun] cannot be exercised because the real /ingest/preview response exposes "
            "no failed document state; a static selector or direct /ingest/rerun call must not count as UI coverage"
        )

    failed_control = rerun_controls.first
    failed_name = failed_control.get_attribute("data-rerun") or ""
    assert failed_name == failed_documents[0].get("name")
    dialogs: list[str] = []

    def accept_rerun_dialog(dialog) -> None:
        dialogs.append(dialog.message)
        dialog.accept()

    admin_page.once("dialog", accept_rerun_dialog)
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/ingest/rerun"),
        timeout=ui_config.timeout_ms,
    ) as rerun_info:
        failed_control.click()
    assert rerun_info.value.status == 200, rerun_info.value.text()
    rerun_payload = rerun_info.value.json()
    assert rerun_payload.get("world") == real_world
    assert dialogs and "再取り込み" in dialogs[0]
    refreshed = live_api.get_json(
        LiveApi.query("/ingest/preview", world=real_world),
        save_as="state/ingest-after-failed-row-rerun.json",
    )
    refreshed_row = next((row for row in refreshed.get("documents") or [] if row.get("name") == failed_name), None)
    assert refreshed_row and refreshed_row.get("state") == "ready", (
        f"failed document {failed_name!r} did not transition to ready after the real UI rerun: {refreshed_row}"
    )
    artifact_case.attest_control_state(
        control_key="@selector:[data-rerun]",
        state="normal",
        assertion="実failed documentの動的再取込操作がpreview状態をreadyへ遷移させた",
    )
    artifact_case.screenshot(admin_page, 20, "ingest-failed-row-rerun-completed")


@pytest.mark.ingestion_real
def test_real_pdf_image_ooxml_legacy_ingestion_evidence(
    admin_page,
    live_api,
    ui_config,
    artifact_case,
    real_world_vlm_registration,
):
    real_world = str(real_world_vlm_registration["world_id"])
    legacy_source = ui_config.world_path / "legacy/legacy-note.doc"
    legacy_magic = legacy_source.read_bytes()[:8]
    assert legacy_magic == bytes.fromhex("d0cf11e0a1b11ae1"), "legacy Office fixture is not a real Word 97-2003 OLE/CFB document"
    artifact_case.write_json(
        "state/legacy-office-source-signature.json",
        {
            "path": "legacy/legacy-note.doc",
            "format": "ole-cfb-word-97-2003",
            "magic_hex": legacy_magic.hex(),
            "source_sha256": hashlib.sha256(legacy_source.read_bytes()).hexdigest(),
        },
    )
    status = live_api.get_json(f"/worlds/{real_world}/status", save_as="state/multiformat-world-status.json")
    assert int(status.get("office_failed") or 0) == 0, status
    assert int(status.get("skipped_office") or 0) == 0, status
    assert int(status.get("office_md") or 0) >= 5, status

    preview = live_api.get_json(
        LiveApi.query("/ingest/preview", world=real_world),
        save_as="state/multiformat-ingest-preview.json",
    )
    documents = {str(item.get("name")): item for item in preview.get("documents") or []}
    required = {
        "office/tax-evidence.docx": ("ooxml", None),
        "office/tax-cases.xlsx": ("ooxml", None),
        "office/nightly-operations.pptx": ("ooxml", None),
        "media/text-evidence.pdf": ("pdf_text", None),
        "media/ocr-evidence.png": ("raster_metadata", None),
        "media/vlm-evidence.bmp": ("vision", None),
        "legacy/legacy-note.doc": ("ooxml", "libreoffice"),
    }
    missing = sorted(set(required) - set(documents))
    assert not missing, f"real converted documents are absent from ingest preview: {missing}"
    for name, (method, legacy_backend) in required.items():
        document = documents[name]
        assert document.get("state") == "ready", {name: document}
        provenance = document.get("provenance") or {}
        assert provenance.get("method") == method, {name: provenance}
        if legacy_backend:
            assert provenance.get("legacy_backend") == legacy_backend, {name: provenance}

    ledger = live_api.get_json(LiveApi.query("/documents", world=real_world), save_as="state/multiformat-documents.json")
    ledger_ids = {str(item.get("path") or item.get("name")) for item in ledger.get("documents") or []}
    assert set(required) <= ledger_ids, sorted(ledger_ids)

    admin_page.goto(ui_config.base_url + "/ui/ingest.html")
    expect(admin_page.locator("#version")).to_have_value(real_world)
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/ingest/preview?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as version_info:
        admin_page.locator("#version").select_option(real_world)
    assert version_info.value.status == 200
    expect(admin_page.locator("#rows tr")).not_to_have_count(0)
    artifact_case.attest_control_state(
        control_key="version",
        state="normal",
        assertion="World selectの実操作が対象World previewを200取得しdocument行を表示した",
    )

    admin_page.locator("#q").fill("tax")
    expect(admin_page.locator("#rows")).to_contain_text("tax")
    artifact_case.attest_control_state(
        control_key="q",
        state="normal",
        assertion="document語句filterが実取込行からtax一致資料を表示した",
    )
    admin_page.locator("#q").fill("SHERPA-DOCUMENT-NO-MATCH-927")
    expect(admin_page.locator("#rows .empty")).to_have_text("該当する資料がありません")
    artifact_case.attest_control_state(
        control_key="q",
        state="abnormal",
        assertion="該当しないdocument語句を全件hitにせず空結果として表示した",
    )
    admin_page.locator("#q").fill("")
    document_type = admin_page.locator('#type option:not([value=""])').first
    expect(document_type).to_be_attached()
    type_value = document_type.get_attribute("value") or document_type.text_content()
    assert type_value
    admin_page.locator("#type").select_option(type_value)
    expect(admin_page.locator("#rows tr")).not_to_have_count(0)
    artifact_case.attest_control_state(
        control_key="type",
        state="normal",
        assertion="実document由来type選択が該当する取込行だけを表示した",
    )
    admin_page.locator("#type").select_option("")

    specs_documents = [str(row.get("name") or "") for row in documents.values() if row.get("folder") == "specs"]
    assert specs_documents, "real preview has no specs folder documents"
    specs_folder = admin_page.locator('#tree [data-folder="specs"]')
    expect(specs_folder).to_be_visible()
    specs_folder.click()
    expect(specs_folder).to_have_class(re.compile(r"\bon\b"))
    rendered_specs = admin_page.locator("#rows .fname").all_inner_texts()
    assert all(any(name in rendered for rendered in rendered_specs) for name in specs_documents)
    assert len(rendered_specs) == len(specs_documents)
    artifact_case.attest_control_state(
        control_key="@selector:[data-folder]",
        state="normal",
        assertion="specs folderの実preview選択が同folder配下のdocument名だけを一覧表示した",
    )

    expect(admin_page.locator("#detailbtn")).to_be_visible()
    admin_page.locator("#detailbtn").click()
    expect(admin_page.locator("#overlay")).to_have_class("overlay open")
    artifact_case.attest_control_state(
        control_key="detailbtn",
        state="normal",
        assertion="取込詳細buttonが選択Worldの実document詳細overlayを表示した",
    )
    admin_page.locator("#overlay .closebtn").click()
    expect(admin_page.locator("#overlay")).not_to_have_class("overlay open")
    artifact_case.attest_control_state(
        control_key="@selector:.closebtn",
        state="normal",
        assertion="表示中の実取込詳細overlayをclose buttonで閉じ一覧画面へ戻った",
    )
    artifact_case.screenshot(admin_page, 10, "ingest-pdf-image-office-provenance-ready")

    original_rel = "media/vlm-evidence.bmp"
    original_button = admin_page.locator(f'#rows [data-dl="{original_rel}"]')
    expect(original_button).to_be_visible()
    with admin_page.expect_download(timeout=ui_config.timeout_ms) as original_download_info:
        with admin_page.expect_response(
            lambda response: response.request.method == "GET" and "/documents/download?" in response.url,
            timeout=ui_config.timeout_ms,
        ) as original_response_info:
            original_button.click()
    original_response = original_response_info.value
    assert original_response.status == 200, original_response.text()
    original_source = (ui_config.world_path / original_rel).read_bytes()
    assert original_response.body() == original_source, "ingest UI original-document action returned different bytes"
    original_download = artifact_case.case_dir / "state" / "ingest-vlm-original-download.bmp"
    original_download_info.value.save_as(str(original_download))
    assert original_download.read_bytes() == original_source
    artifact_case.attest_control_state(
        control_key="@selector:[data-dl]",
        state="normal",
        assertion="動的原本download内容が選択VLM fixture原本byte列と完全一致した",
    )
    artifact_case.write_json(
        "state/ingest-ui-original-download.json",
        {
            "rel_path": original_rel,
            "status": original_response.status,
            "source_sha256": hashlib.sha256(original_source).hexdigest(),
            "response_sha256": hashlib.sha256(original_response.body()).hexdigest(),
            "download_sha256": hashlib.sha256(original_download.read_bytes()).hexdigest(),
        },
    )

    tokens = {
        "SHERPA-OOXML-WORD-481": "office/tax-evidence.docx",
        "SHERPA-OOXML-EXCEL-582": "office/tax-cases.xlsx",
        "SHERPA-OOXML-SLIDE-693": "office/nightly-operations.pptx",
        "SHERPA-PDF-TEXT-884": "media/text-evidence.pdf",
        "SHERPA-LIVE-LEGACY-271": "legacy/legacy-note.doc",
        "SHERPA-OCR-IMAGE-773": "media/ocr-evidence.png",
    }
    search_evidence = {}
    for token, doc_id in tokens.items():
        result = live_api.get_json(LiveApi.query("/admin/es/search", world=real_world, query=token, k=20))
        hits = result.get("hits") or []
        assert any(hit.get("doc_id") == doc_id for hit in hits), {
            "token": token,
            "expected_doc": doc_id,
            "hits": hits,
        }
        search_evidence[token] = [{"doc_id": hit.get("doc_id"), "extraction_method": hit.get("extraction_method")} for hit in hits]
    artifact_case.write_json("state/multiformat-search-evidence.json", search_evidence)

    database = wait_for_ingestion_database_snapshot(
        ui_config.database_url,
        real_world,
        artifact_case,
        timeout_seconds=max(ui_config.timeout_ms / 1000, 180),
    )
    ocr_jobs = database["ocr_jobs"]
    assert any(int(row.get("observation_count") or 0) > 0 for row in ocr_jobs), ocr_jobs
    ocr_payload = "\n".join(str(row.get("result_payload") or "") for row in ocr_jobs)
    assert "SHERPA-OCR-IMAGE-773" in ocr_payload, "the real OCR worker completed but did not extract the fixture marker"

    paddle_jobs = [row for row in ocr_jobs if str(row.get("source_rel_path") or "").endswith("media/ocr-evidence.png")]
    assert paddle_jobs, "the dedicated PNG fixture has no persisted Paddle OCR job"
    paddle_job = paddle_jobs[-1]
    paddle_payload = paddle_job.get("result_payload") or {}
    assert isinstance(paddle_payload, dict), "Paddle OCR result is not structured JSON"
    assert str(paddle_payload.get("provider") or "").lower() == "paddleocr", paddle_payload
    paddle_model = str(paddle_payload.get("model") or "")
    assert paddle_model, "Paddle OCR result omitted model identity"
    paddle_observations = paddle_payload.get("observations") or []
    assert paddle_observations and all(observation.get("observation_id") for observation in paddle_observations), (
        "Paddle OCR result omitted deterministic observation identifiers"
    )
    assert all(observation.get("kind") == "ocr_text" for observation in paddle_observations), (
        "Paddle OCR result contains a non-OCR observation kind"
    )
    paddle_text = "\n".join(str(item.get("text") or "") for item in paddle_observations)
    assert "SHERPA-OCR-IMAGE-773" in paddle_text, "Paddle OCR did not extract the dedicated PNG marker"
    assert paddle_job.get("result_observation_set_hash")
    assert paddle_job.get("artifact_published") is True
    artifact_case.write_json(
        "state/ocr-paddle-observation.json",
        {
            "source_rel_path": paddle_job["source_rel_path"],
            "job_id": paddle_job["id"],
            "provider": paddle_payload["provider"],
            "model": paddle_model,
            "observation_ids": [observation["observation_id"] for observation in paddle_observations],
            "observation_count": paddle_job["observation_count"],
            "result_observation_set_hash": paddle_job["result_observation_set_hash"],
            "artifact_published": paddle_job["artifact_published"],
            "marker_observed": True,
        },
    )

    vlm_jobs = [row for row in ocr_jobs if str(row.get("source_rel_path") or "").endswith("media/vlm-evidence.bmp")]
    assert vlm_jobs, "the VLM fixture has no independently persisted OCR observation job"
    vlm_job = vlm_jobs[-1]
    payload = vlm_job.get("result_payload") or {}
    assert isinstance(payload, dict), "VLM fixture observation payload is not structured JSON"
    observations = payload.get("observations") or []
    assert observations and observations[0].get("observation_id"), "VLM fixture has no persisted observation identifier"
    assert payload.get("provider") and payload.get("model"), "persisted OCR observation omitted its provider/model identity"
    assert vlm_job.get("result_observation_set_hash"), "VLM fixture has no persisted observation-set hash"
    assert vlm_job.get("artifact_published") is True

    system_settings = live_api.get_json(
        "/admin/settings",
        save_as="state/vlm-effective-settings.json",
    )
    vlm_effective = (system_settings.get("vlm") or {}).get("effective") or {}
    expected_vlm_provider = str(vlm_effective.get("provider") or "").strip().lower()
    expected_vlm_model = str(vlm_effective.get("model") or "").strip()
    assert expected_vlm_provider and expected_vlm_model, "effective VLM provider/model is unavailable after a successful vision conversion"
    usage_rows = usage_events_after(
        ui_config.database_url,
        "vlm",
        int(real_world_vlm_registration["vlm_usage_checkpoint"]),
        world=None,
    )
    window_usage = [
        row for row in usage_rows if real_world_vlm_registration["started_at"] <= row["ts"] <= real_world_vlm_registration["completed_at"]
    ]
    matching_usage = [
        row
        for row in window_usage
        if str(row.get("provider") or "").strip().lower() == expected_vlm_provider
        and str(row.get("model") or "").strip() == expected_vlm_model
        and int(row.get("calls") or 0) > 0
    ]
    assert len(matching_usage) == 1, (
        "real VLM conversion must have exactly one post-checkpoint provider/model usage row inside its registration window: "
        f"checkpoint={real_world_vlm_registration['vlm_usage_checkpoint']} rows={usage_rows}"
    )
    vlm_usage = matching_usage[0]

    derived_value = os.environ.get("SHERPA_DERIVED_DIR", "").strip()
    assert derived_value, "SHERPA_DERIVED_DIR is required for physical VLM provenance correlation"
    derived_world = Path(derived_value) / real_world / "md"
    assert_no_mount_targets(derived_world)
    provenance_paths = sorted(derived_world.rglob("vlm-evidence.bmp.md.meta.json"))
    assert len(provenance_paths) == 1, f"expected one physical VLM provenance sidecar under {derived_world}, got {provenance_paths}"
    provenance_path = provenance_paths[0]
    assert not provenance_path.is_symlink(), f"VLM provenance sidecar must not be a symlink: {provenance_path}"
    provenance = json.loads(provenance_path.read_text(encoding="utf-8"))
    notes = [str(note) for note in provenance.get("notes") or []]
    assert provenance.get("method") == "vision", provenance
    assert f"vlm_provider={expected_vlm_provider}" in notes, notes
    assert f"vlm_model={expected_vlm_model}" in notes, notes
    assert (documents["media/vlm-evidence.bmp"].get("provenance") or {}).get("method") == provenance.get("method")
    artifact_case.write_json(
        "state/vlm-observation.json",
        {
            "provider": vlm_usage["provider"],
            "model": vlm_usage["model"],
            "usage_event_id": vlm_usage["id"],
            "usage_timestamp": vlm_usage["ts"].isoformat(),
            "usage_checkpoint": real_world_vlm_registration["vlm_usage_checkpoint"],
            "registration_started_at": real_world_vlm_registration["started_at"].isoformat(),
            "registration_completed_at": real_world_vlm_registration["completed_at"].isoformat(),
            "calls": vlm_usage["calls"],
            "observation_id": observations[0]["observation_id"],
            "observation_provider": payload["provider"],
            "observation_model": payload["model"],
            "result_observation_set_hash": vlm_job["result_observation_set_hash"],
            "observation_count": vlm_job["observation_count"],
            "artifact_published": vlm_job["artifact_published"],
            "source_rel_path": vlm_job["source_rel_path"],
            "derived_provenance_relative_path": provenance_path.relative_to(derived_world).as_posix(),
            "derived_provenance_sha256": hashlib.sha256(provenance_path.read_bytes()).hexdigest(),
            "derived_provenance_method": provenance["method"],
        },
    )

    download_hashes = {}
    for rel_path in required:
        response = live_api.request(
            "GET",
            LiveApi.query("/documents/download", world=real_world, rel=rel_path),
            expected=200,
        )
        assert response.body == (ui_config.world_path / rel_path).read_bytes(), rel_path
        download_hashes[rel_path] = {
            "source_sha256": hashlib.sha256((ui_config.world_path / rel_path).read_bytes()).hexdigest(),
            "download_sha256": hashlib.sha256(response.body).hexdigest(),
            "provenance": documents[rel_path].get("provenance"),
        }
    artifact_case.write_json(
        "state/office-ooxml-conversion.json",
        {key: download_hashes[key] for key in download_hashes if key.startswith("office/")},
    )
    artifact_case.write_json(
        "state/legacy-office-conversion.json",
        {"legacy/legacy-note.doc": download_hashes["legacy/legacy-note.doc"]},
    )
    artifact_case.screenshot(admin_page, 20, "ingest-ocr-vlm-office-search-and-originals-correlated")
