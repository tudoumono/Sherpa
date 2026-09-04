from __future__ import annotations

import hashlib
import os
import re
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import pytest
from playwright.sync_api import expect

from ui_automation.support.chat import answer_event, assert_real_ai_result
from ui_automation.support.chat_flow import (
    last_assistant_message,
    prepare_chat,
    start_turn_from_ui,
    wait_for_completed_ui,
)
from ui_automation.support.database import (
    conversation_session_id,
    set_nonexistent_conversation_session,
)
from ui_automation.support.environment_probes import _safe_codex_invocation_snapshot
from ui_automation.support.live_api import LiveApi
from ui_automation.support.ui import (
    AdminCredentials,
    login_without_trace,
    runtime_password,
    unique_id,
)


pytestmark = [pytest.mark.ui_automation, pytest.mark.chat, pytest.mark.destructive]


def _cleanup_conversation(api, conversation_id: int) -> None:
    response = api.request("GET", f"/conversations/{conversation_id}", expected={200, 404})
    if response.status == 200:
        api.delete_json(f"/conversations/{conversation_id}")


def _cleanup_workspace_file(api, file_id: int) -> None:
    api.request("DELETE", f"/workspace/files/{file_id}", expected={200, 404})


def _start_codex_invocation_watch(observations: list[dict]) -> tuple[threading.Event, threading.Thread, list[str]]:
    """Observe only redacted Codex process properties, starting before the UI POST.

    A resume attempt can fail faster than the chat response itself.  Polling
    after ``start_turn_from_ui`` therefore cannot prove which subprocess was
    launched for this turn.  The watcher deliberately never retains argv,
    prompts, session ids, environment values, or process ids.
    """

    pid_file = Path(os.environ.get("APP_PID_FILE", ""))
    assert pid_file.is_file(), "Codex invocation observation requires the isolated app PID file"
    app_pid = int(pid_file.read_text(encoding="utf-8").strip())
    stop = threading.Event()
    ready = threading.Event()
    errors: list[str] = []

    def watch() -> None:
        previous: tuple[tuple[str, object], ...] | None = None
        while not stop.is_set():
            try:
                snapshot = _safe_codex_invocation_snapshot(app_pid)
            except (OSError, RuntimeError, ValueError) as exc:
                errors.append(f"{type(exc).__name__}: {exc}")
                ready.set()
                return
            ready.set()
            if snapshot is not None:
                signature = tuple(sorted(snapshot.items()))
                if signature != previous:
                    observations.append(dict(snapshot))
                    previous = signature
            stop.wait(0.003)

    thread = threading.Thread(target=watch, name="sherpa-ui-codex-process-watch", daemon=True)
    thread.start()
    assert ready.wait(timeout=2), "Codex invocation watcher did not start before the UI turn"
    return stop, thread, errors


def _stop_codex_invocation_watch(watcher: tuple[threading.Event, threading.Thread, list[str]] | None) -> None:
    if watcher is None:
        return
    stop, thread, errors = watcher
    stop.set()
    thread.join(timeout=2)
    assert not thread.is_alive(), "Codex invocation watcher did not stop"
    assert not errors, f"Codex invocation watcher failed: {errors}"


def _application_log_offsets(config) -> dict[Path, int]:
    assert config.expected_env_path is not None
    service_root = config.expected_env_path.parent.parent / "services"
    offsets = {path: path.stat().st_size for path in sorted(service_root.glob("app*.log")) if path.is_file()}
    assert offsets, "runner produced no application log before the Codex turn"
    return offsets


def _application_log_suffix(offsets: dict[Path, int]) -> tuple[str, list[dict]]:
    chunks: list[str] = []
    evidence: list[dict] = []
    for path, offset in offsets.items():
        try:
            with path.open("rb") as handle:
                handle.seek(offset)
                raw = handle.read()
        except OSError:
            raw = b""
        chunks.append(raw.decode("utf-8", errors="replace"))
        evidence.append(
            {
                "file": path.name,
                "start_offset": offset,
                "observed_bytes": len(raw),
                "observed_bytes_sha256": hashlib.sha256(raw).hexdigest(),
            }
        )
    return "".join(chunks), evidence


def _complete_real_turn(
    page,
    api,
    config,
    evidence,
    settings,
    question: str,
    *,
    codex_invocations: list[dict] | None = None,
) -> tuple[int, str, dict, list[dict]]:
    watcher = _start_codex_invocation_watch(codex_invocations) if codex_invocations is not None else None
    try:
        started = start_turn_from_ui(page, config, question)
        cid, tid = int(started["conversation_id"]), str(started["turn_id"])
        events = api.collect_sse(
            f"/chat/turns/{tid}/stream?cursor=0",
            save_as=f"network/turn-{tid[:12]}.jsonl",
        )
        answer_event(events)
        wait_for_completed_ui(page, config.timeout_ms)
        conversation = api.get_json(f"/conversations/{cid}")
        assistant = last_assistant_message(conversation)
        assert_real_ai_result(
            settings,
            events,
            assistant,
            require_tool=True,
            evidence=evidence,
            turn_id=tid,
            conversation_id=cid,
            database_url=config.database_url,
            checkpoint=started["_real_ai_checkpoint"],
        )
        return cid, tid, assistant, events
    finally:
        _stop_codex_invocation_watch(watcher)


def _session_digest(value: str | None) -> str | None:
    return hashlib.sha256(value.encode()).hexdigest() if value else None


def test_followup_session_rename_pin_switch_and_delete(admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack):
    settings = live_api.get_json("/settings", save_as="state/lifecycle-settings.json")
    prepare_chat(admin_page, ui_config, real_world)
    first_cid, first_tid, first_answer, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "SHERPA-LIVE-ALPHA-927 の検証用税率を根拠ファイルとともに調べてください。",
    )
    artifact_case.add_cleanup(
        f"delete conversation {first_cid}",
        lambda: _cleanup_conversation(live_api, first_cid),
    )
    assert "12.5" in str(first_answer.get("answer") or first_answer.get("content") or "")
    admin_page.locator("#exportbtn").click()
    expect(admin_page.locator("#exportmenu")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="exportbtn",
        state="normal",
        assertion="完了済み実conversationのexport buttonが形式選択menuを表示した",
    )
    with admin_page.expect_download(timeout=ui_config.timeout_ms) as export_info:
        admin_page.locator('#exportmenu [data-exp="md"]').click()
    exported_chat = artifact_case.case_dir / "state" / "chat-export.md"
    export_info.value.save_as(str(exported_chat))
    exported_text = exported_chat.read_text(encoding="utf-8")
    assert "12.5" in exported_text and "SHERPA-LIVE-ALPHA-927" in exported_text
    artifact_case.attest_control_state(
        control_key="@selector:[data-exp]",
        state="normal",
        assertion="Markdown会話exportに実回答の固有税率と質問tokenがともに含まれた",
    )
    first_session = conversation_session_id(ui_config.database_url, first_cid)
    artifact_case.register_secret(first_session)
    configured_provider = str(((first_answer.get("answer") or {}).get("usage") or {}).get("provider") or "")
    if configured_provider == "codex":
        assert first_session, "Codex completed without a persisted native session"
    else:
        assert first_session is None, "a non-Codex provider persisted a Codex session"
    artifact_case.screenshot(admin_page, 10, "chat-first-conversation-complete")

    second_cid, second_tid, second_answer, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "前の質問と同じ税計算ポリシーで、端数処理と呼出元の夜間ジョブを続けて確認してください。",
    )
    assert second_cid == first_cid, "follow-up created a different conversation"
    assert second_tid != first_tid
    followup_text = str(second_answer.get("answer") or second_answer.get("content") or "")
    assert "切り捨て" in followup_text and "NIGHTLY" in followup_text, followup_text[:500]
    second_session = conversation_session_id(ui_config.database_url, first_cid)
    artifact_case.register_secret(second_session)
    if configured_provider == "codex":
        assert _session_digest(second_session) == _session_digest(first_session), "Codex follow-up did not continue the native session"
    artifact_case.write_json(
        "state/followup-session-correlation.json",
        {
            "provider": configured_provider,
            "conversation_id": first_cid,
            "turn_ids_distinct": first_tid != second_tid,
            "native_session_present": bool(second_session),
            "native_session_continued": bool(first_session and first_session == second_session),
        },
    )
    artifact_case.screenshot(admin_page, 20, "chat-followup-same-conversation-and-session")

    new_title = unique_id("ui-renamed-chat")
    admin_page.once("dialog", lambda dialog: dialog.accept(new_title))
    with admin_page.expect_response(
        lambda response: response.request.method == "PATCH" and response.url.endswith(f"/conversations/{first_cid}"),
        timeout=ui_config.timeout_ms,
    ) as rename_info:
        admin_page.locator(f'#convlist [data-rename="{first_cid}"]').click()
    assert rename_info.value.status == 200
    expect(admin_page.locator("#conv-title")).to_have_text(new_title)
    artifact_case.attest_control_state(
        control_key="@selector:[data-rename]",
        state="normal",
        assertion="動的rename操作が対象conversationだけを変更し表示titleへ反映した",
    )

    header_title = unique_id("ui-header-renamed-chat")
    admin_page.once("dialog", lambda dialog: dialog.accept(header_title))
    title_control = admin_page.locator("#conv-title")
    artifact_case.arm_control(title_control, control_key="conv-title")
    with admin_page.expect_response(
        lambda response: response.request.method == "PATCH" and response.url.endswith(f"/conversations/{first_cid}"),
        timeout=ui_config.timeout_ms,
    ) as header_rename_info:
        title_control.click()
    assert header_rename_info.value.status == 200
    expect(title_control).to_have_text(header_title)
    renamed_record = live_api.get_json(f"/conversations/{first_cid}")
    assert (renamed_record.get("conversation") or {}).get("title") == header_title
    artifact_case.attest_control_state(
        control_key="conv-title",
        state="normal",
        assertion="header titleの直接操作で対象conversationだけを改名しAPI保存値とも一致した",
    )

    admin_page.once("dialog", lambda dialog: dialog.dismiss())
    artifact_case.arm_control(title_control, control_key="conv-title")
    title_control.click()
    admin_page.wait_for_timeout(200)
    expect(title_control).to_have_text(header_title)
    cancelled_record = live_api.get_json(f"/conversations/{first_cid}")
    assert (cancelled_record.get("conversation") or {}).get("title") == header_title
    artifact_case.attest_control_state(
        control_key="conv-title",
        state="abnormal",
        assertion="header改名promptを取消した操作が表示titleにも実APIにも変更を残さなかった",
    )
    new_title = header_title

    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith(f"/conversations/{first_cid}/pin"),
        timeout=ui_config.timeout_ms,
    ) as pin_info:
        admin_page.locator(f'#convlist [data-pin="{first_cid}"]').click()
    assert pin_info.value.status == 200 and pin_info.value.json().get("pinned") is True
    expect(admin_page.locator(f'#convlist [data-open="{first_cid}"]')).to_have_class(re.compile(r"\bpinned\b"))
    artifact_case.attest_control_state(
        control_key="@selector:[data-pin]",
        state="normal",
        assertion="動的pin操作が対象conversationの実API状態とpinned classへ反映された",
    )
    artifact_case.screenshot(admin_page, 30, "chat-conversation-renamed-and-pinned")

    admin_page.locator("#newbtn").click()
    expect(admin_page.locator("#conv-title")).to_have_text("新しい会話")
    artifact_case.attest_control_state(
        control_key="newbtn",
        state="normal",
        assertion="新規会話buttonが既存履歴を維持したまま空の新しい会話画面へ切り替えた",
    )
    new_cid, _, new_answer, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "SHERPA-LIVE-REFERENCE-314 の夜間運用時刻を根拠資料から確認してください。",
    )
    assert new_cid != first_cid
    artifact_case.add_cleanup(
        f"delete conversation {new_cid}",
        lambda: _cleanup_conversation(live_api, new_cid),
    )
    assert "02:15" in str(new_answer.get("answer") or new_answer.get("content") or "")
    artifact_case.screenshot(admin_page, 40, "chat-second-conversation-complete")

    first_conversation_row = admin_page.locator(f'#convlist [data-open="{first_cid}"]')
    artifact_case.arm_control(first_conversation_row, control_key="@selector:[data-open]")
    first_conversation_row.click()
    expect(admin_page.locator("#conv-title")).to_have_text(new_title)
    expect(admin_page.locator("#messages")).to_contain_text("切り捨て")
    artifact_case.attest_control_state(
        control_key="@selector:[data-open]",
        state="normal",
        assertion="選択した実conversation行を開き同じIDのtitleと保存済み履歴を表示した",
    )
    admin_page.locator(f'#convlist [data-open="{new_cid}"]').click()
    expect(admin_page.locator("#messages")).to_contain_text("02:15")
    artifact_case.screenshot(admin_page, 50, "chat-conversation-switch-preserves-distinct-history")

    admin_page.once("dialog", lambda dialog: dialog.accept())
    with admin_page.expect_response(
        lambda response: response.request.method == "DELETE" and response.url.endswith(f"/conversations/{new_cid}"),
        timeout=ui_config.timeout_ms,
    ) as delete_info:
        admin_page.locator(f'#convlist [data-del="{new_cid}"]').click()
    assert delete_info.value.status == 200
    assert live_api.request("GET", f"/conversations/{new_cid}", expected=404).status == 404
    artifact_case.attest_control_state(
        control_key="@selector:[data-del]",
        state="normal",
        assertion="動的削除操作が対象conversationだけを削除し実APIも404になった",
    )
    artifact_case.screenshot(admin_page, 60, "chat-conversation-deleted-without-history-residue")


def test_share_url_acceptance_and_revocation(browser, admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack):
    settings = live_api.get_json("/settings", save_as="state/share-settings.json")
    prepare_chat(admin_page, ui_config, real_world)
    cid, _, assistant, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "SHERPA-LIVE-ALPHA-927 の税率を、共有可能な社内資料だけから確認してください。",
    )
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    assert not (assistant.get("answer") or {}).get("personal_sources")

    uid = unique_id("ui-share-member")
    initial_password = runtime_password()
    changed_password = runtime_password()
    artifact_case.register_secret(initial_password)
    artifact_case.register_secret(changed_password)
    created = live_api.post_json(
        "/admin/users",
        {"uid": uid, "display_name": "UI Share Member", "role": "user", "password": initial_password},
    )
    assert created.get("ok") is True
    artifact_case.add_cleanup(
        f"disable share member {uid}",
        lambda: live_api.patch_json(f"/admin/users/{uid}", {"status": "disabled"}),
    )

    admin_page.locator("#sharebtn").click()
    expect(admin_page.locator("#share-overlay")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="sharebtn",
        state="normal",
        assertion="所有する実conversationの共有buttonが共有form overlayを表示した",
    )
    admin_page.locator("#share-close").click()
    expect(admin_page.locator("#share-overlay")).to_be_hidden()
    artifact_case.attest_control_state(
        control_key="share-close",
        state="normal",
        assertion="共有dialogのclose操作が未送信のままoverlayを閉じた",
    )
    admin_page.locator("#sharebtn").click()
    expect(admin_page.locator("#share-overlay")).to_be_visible()
    admin_page.locator("#share-cancel").click()
    expect(admin_page.locator("#share-overlay")).to_be_hidden()
    artifact_case.attest_control_state(
        control_key="share-cancel",
        state="normal",
        assertion="共有取消操作が共有を作成せず未送信formを閉じた",
    )
    admin_page.locator(f'#convlist [data-sharecid="{cid}"]').click()
    expect(admin_page.locator("#share-overlay")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="@selector:[data-sharecid]",
        state="normal",
        assertion="動的共有操作が選択conversation IDを持つ共有overlayを表示した",
    )

    admin_page.locator("#share-submit").click()
    expect(admin_page.locator("#share-err")).to_contain_text("招待するユーザー名")
    expect(admin_page.locator("#share-overlay")).to_be_visible()
    artifact_case.attest_control_state(
        control_key="share-submit",
        state="abnormal",
        assertion="招待相手なしの共有送信をform内で拒否し成功画面へ切り替えなかった",
    )
    invitee_input = admin_page.locator("#share-invitees")
    missing_invitee = unique_id("missing-share-user")
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/users/suggest?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as missing_suggest_info:
        invitee_input.fill(missing_invitee)
    assert missing_suggest_info.value.status == 200, missing_suggest_info.value.text()
    expect(admin_page.locator("#share-invitee-suggest [data-pick-invitee]")).to_have_count(0)
    artifact_case.attest_control_state(
        control_key="share-invitees",
        state="abnormal",
        assertion="存在しない実user検索が候補controlを生成せず共有相手へ追加されなかった",
    )
    invitee_input.fill("")
    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/users/suggest?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as first_suggest_info:
        invitee_input.fill(uid)
    assert first_suggest_info.value.status == 200, first_suggest_info.value.text()
    suggestion = admin_page.locator("#share-invitee-suggest [data-pick-invitee]", has_text=uid)
    expect(suggestion).to_be_visible()
    artifact_case.attest_control_state(
        control_key="share-invitees",
        state="normal",
        assertion="実在するuser名入力が実suggest APIから一致する招待候補を表示した",
    )
    suggestion.click()
    expect(admin_page.locator("#share-invitee-suggest")).to_be_hidden()
    expect(invitee_input).to_have_value("")
    invitee_chip = admin_page.locator("#share-invitee-chips .invitee-chip", has_text="UI Share Member")
    expect(invitee_chip).to_be_visible()
    artifact_case.attest_control_state(
        control_key="@selector:[data-pick-invitee]",
        state="normal",
        assertion="動的候補選択が指定した実userだけを招待chipとして追加した",
    )
    remove_invitee = invitee_chip.locator("[data-rm-invitee]")
    expect(remove_invitee).to_be_visible()
    remove_invitee.click()
    expect(invitee_chip).to_have_count(0)
    artifact_case.attest_control_state(
        control_key="@selector:[data-rm-invitee]",
        state="normal",
        assertion="動的削除操作が選択済み招待user chipを共有対象から取り除いた",
    )

    with admin_page.expect_response(
        lambda response: response.request.method == "GET" and "/users/suggest?" in response.url,
        timeout=ui_config.timeout_ms,
    ) as second_suggest_info:
        invitee_input.fill(uid)
    assert second_suggest_info.value.status == 200, second_suggest_info.value.text()
    second_suggestion = admin_page.locator("#share-invitee-suggest [data-pick-invitee]", has_text=uid)
    expect(second_suggestion).to_be_visible()
    second_suggestion.click()
    expect(admin_page.locator("#share-invitee-chips .invitee-chip", has_text="UI Share Member")).to_be_visible()
    expect(invitee_input).to_have_value("")
    admin_page.locator("#share-days").select_option("7")
    expect(admin_page.locator("#share-days")).to_have_value("7")
    artifact_case.attest_control_state(
        control_key="share-days",
        state="normal",
        assertion="共有期限selectの7日指定がform値へ正確に反映された",
    )
    artifact_case.stop_trace(save=False)
    with admin_page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith(f"/conversations/{cid}/shares"),
        timeout=ui_config.timeout_ms,
    ) as share_info:
        admin_page.locator("#share-submit").click()
    shared = share_info.value.json()
    share_path = str(shared.get("url") or "")
    token = share_path.rsplit("/", 1)[-1]
    share_url = ui_config.base_url + share_path
    artifact_case.register_secret(token)
    artifact_case.register_secret(share_url)
    assert shared.get("ok") is True, "share creation did not report success"
    assert shared.get("share_id"), "share creation did not return an identifier"
    assert token, "share creation did not return an invitation token"
    artifact_case.attest_control_state(
        control_key="share-submit",
        state="normal",
        assertion="実userと期限を指定した共有送信がshare IDと一回限りtokenを発行した",
    )
    share_id = int(shared["share_id"])
    admin_page.locator("#share-copy").click()
    expect(admin_page.locator("#toast")).to_contain_text("コピーしました")
    artifact_case.attest_control_state(
        control_key="share-copy",
        state="normal",
        assertion="発行済み共有URLのcopy操作がclipboard完了toastを表示した",
    )
    result_close = admin_page.locator("#share-result > div:last-child > button.mini")
    artifact_case.arm_unkeyed_control(
        result_close,
        control_key="@unkeyed:web/chat.html:179:button",
    )
    result_close.click()
    expect(admin_page.locator("#share-overlay")).to_be_hidden()
    expect(admin_page.locator("#share-result")).to_be_hidden()
    artifact_case.attest_control_state(
        control_key="@unkeyed:web/chat.html:179:button",
        state="normal",
        assertion="発行済み共有結果の閉じるbuttonが結果dialogだけを閉じて所有者の会話画面へ戻した",
    )
    admin_page.locator("#share-url-val").evaluate("element => { element.textContent = ''; }")
    artifact_case.start_trace(admin_page.context)
    artifact_case.screenshot(admin_page, 10, "chat-share-link-issued-and-secret-cleared")

    member_context = browser.new_context(viewport={"width": 1366, "height": 900}, locale="ja-JP")
    member_context.set_default_timeout(ui_config.timeout_ms)
    member = member_context.new_page()
    credentials = AdminCredentials(
        username=uid,
        initial_password=initial_password,
        changed_password=changed_password,
        active_password=initial_password,
    )
    try:
        artifact_case.attach_page(member)
        changed_now = login_without_trace(
            member,
            ui_config.base_url,
            credentials,
            "/ui/chat.html",
            ui_config.timeout_ms,
            artifact_case,
        )
        assert changed_now, "fresh invited user was not forced through password change"
        member.goto(share_url)
        member.wait_for_url("**/ui/chat.html?conversation_id=*", timeout=ui_config.timeout_ms)
        wrapper_id = int(parse_qs(urlsplit(member.url).query)["conversation_id"][0])
        received_row = member.locator(f'#convlist [data-open="{wrapper_id}"]')
        expect(received_row).to_contain_text("共有")
        artifact_case.arm_control(received_row, control_key="@selector:[data-open]")
        received_row.click()
        expect(member.locator("#messages")).to_contain_text("12.5", timeout=ui_config.timeout_ms)
        expect(received_row.locator("[data-rename]")).to_have_count(0)
        expect(received_row.locator("[data-sharecid]")).to_have_count(0)
        member_api = LiveApi(ui_config.base_url, member_context, artifact_case)
        readonly = member_api.request(
            "PATCH",
            f"/conversations/{wrapper_id}",
            {"title": "must-not-change"},
            expected=403,
        )
        assert readonly.status == 403, "received share unexpectedly allowed owner mutation"
        artifact_case.attest_control_state(
            control_key="@selector:[data-open]",
            state="normal",
            assertion="招待された実共有conversation行が同じ共有履歴をreadonlyで表示した",
        )
        artifact_case.screenshot(member, 20, "chat-shared-conversation-opened-by-invitee")

        revoked = live_api.post_json(f"/conversation-shares/{share_id}/revoke", {})
        assert revoked.get("ok") is True
        denied_response = member.goto(share_url)
        assert denied_response is not None and denied_response.status == 403
        member.goto(ui_config.base_url + "/ui/chat.html")
        inactive_row = member.locator(f'#convlist [data-open="{wrapper_id}"]')
        expect(inactive_row).to_contain_text("共有取消")
        expect(inactive_row).to_have_attribute("data-inactive", "1")
        title_before_inactive_click = member.locator("#conv-title").inner_text()
        artifact_case.arm_control(inactive_row, control_key="@selector:[data-open]")
        inactive_row.click()
        expect(member.locator("#toast")).to_contain_text("共有は期限切れまたは取消済み")
        expect(member.locator("#conv-title")).to_have_text(title_before_inactive_click)
        artifact_case.attest_control_state(
            control_key="@selector:[data-open]",
            state="abnormal",
            assertion="取消済み共有行の操作を拒否し別conversationの内容を開いた状態にしなかった",
        )
        artifact_case.screenshot(member, 30, "chat-revoked-share-is-inactive-and-cannot-reopen")
        member_api.delete_json(f"/conversations/{wrapper_id}")
    finally:
        member_context.close()

    audit = live_api.get_json(
        f"/admin/audit?resource_id=share:{share_id}&limit=20",
        save_as="state/share-audit.json",
    )
    actions = {row.get("action") for row in audit.get("rows", [])}
    assert {"share.created", "share.accepted", "share.revoked"} <= actions, actions


def test_codex_created_artifact_downloads_from_real_workspace(admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack):
    settings = live_api.get_json("/settings", save_as="state/artifact-settings.json")
    prepare_chat(admin_page, ui_config, real_world)
    token = unique_id("SHERPA-REAL-ARTIFACT").upper()
    cid, _, assistant, events = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        (
            "specs/tax-policy.md を実際に読み、authoring直下へ "
            "ui-automation-artifact.md というMarkdownを作成してください。"
            f"本文には {token} と検証用税率を必ず含めてください。"
        ),
    )
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    answer = assistant.get("answer") or {}
    provider = str((answer.get("usage") or {}).get("provider") or "").lower()
    if provider != "codex":
        assert not answer.get("created_files"), f"{provider} displayed a workspace artifact although it has no authoring execution path"
        artifact_case.write_json(
            "state/non-codex-artifact-boundary.json",
            {"provider": provider, "created_files_absent": True, "real_turn_completed": bool(events)},
        )
        artifact_case.screenshot(admin_page, 10, f"chat-{provider}-does-not-pretend-artifact-created")
        return

    created_files = answer.get("created_files") or []
    match = next(
        (item for item in created_files if str(item.get("name") or "").endswith("ui-automation-artifact.md")),
        None,
    )
    assert match, f"Codex did not register the requested real artifact: {created_files}"
    download_url = str(match.get("download_url") or "")
    file_id = int(download_url.split("/")[-2])
    artifact_case.add_cleanup(
        f"delete workspace artifact {file_id}",
        lambda: _cleanup_workspace_file(live_api, file_id),
    )
    link = admin_page.locator(f'#messages a[data-dl][href="{download_url}"]')
    expect(link).to_be_visible()
    with admin_page.expect_download(timeout=ui_config.timeout_ms) as download_info:
        link.click()
    download = download_info.value
    saved = artifact_case.case_dir / "state" / "downloaded-ui-automation-artifact.md"
    download.save_as(str(saved))
    content = saved.read_text(encoding="utf-8")
    assert token in content and "12.5" in content
    artifact_case.attest_control_state(
        control_key="@selector:[data-dl]",
        state="normal",
        assertion="Codex作成workspace成果物のdownload本文が固有tokenと税率を保持した",
    )
    listed = live_api.get_json("/workspace/files", save_as="state/artifact-workspace-list.json")
    assert any(int(item["id"]) == file_id for item in listed.get("files", []))
    artifact_case.screenshot(admin_page, 20, "chat-codex-artifact-card-and-download-verified")

    artifact_name = str(match.get("name") or "")
    workspace_link = admin_page.locator("#messages .created-files-link").last
    expect(workspace_link).to_be_visible()
    expect(workspace_link).to_have_attribute("href", "workspace.html")
    workspace_authorization = artifact_case.arm_control_authorization(
        admin_page,
        control_key="@selector:.created-files-link",
    )
    assert workspace_authorization["status"] == 200 and workspace_authorization["role"] == "admin"
    workspace_link.click()
    expect(admin_page).to_have_url(re.compile(r"/ui/workspace\.html(?:[?#].*)?$"), timeout=ui_config.timeout_ms)
    workspace_file_list = admin_page.locator("#file-list")
    expect(workspace_file_list).to_have_attribute("aria-busy", "false", timeout=ui_config.timeout_ms)
    expect(workspace_file_list).to_contain_text(artifact_name)
    workspace_after_navigation = live_api.get_json(
        "/workspace/files",
        save_as="state/artifact-workspace-after-card-navigation.json",
    )
    assert any(
        int(item["id"]) == file_id and str(item.get("rel_path") or "") == artifact_name
        for item in workspace_after_navigation.get("files", [])
    ), "workspace card navigation did not show the same real file registered by the Codex turn"
    artifact_case.attest_control_state(
        control_key="@selector:.created-files-link",
        state="normal",
        assertion="成果物cardから遷移した実workspaceのfile名とIDが同じCodex turnの保存値と一致した",
    )
    artifact_case.screenshot(admin_page, 30, "chat-created-file-card-opened-correlated-workspace")

    deleted = live_api.request("DELETE", f"/workspace/files/{file_id}", expected=200)
    assert deleted.status == 200
    workspace_after_delete = live_api.get_json(
        "/workspace/files",
        save_as="state/artifact-workspace-after-real-delete.json",
    )
    assert all(int(item["id"]) != file_id for item in workspace_after_delete.get("files", []))

    admin_page.goto(ui_config.base_url + f"/ui/chat.html?conv={cid}")
    stale_workspace_link = admin_page.locator("#messages .created-files-link").last
    expect(stale_workspace_link).to_be_visible(timeout=ui_config.timeout_ms)
    stale_workspace_authorization = artifact_case.arm_control_authorization(
        admin_page,
        control_key="@selector:.created-files-link",
    )
    assert stale_workspace_authorization["status"] == 200 and stale_workspace_authorization["role"] == "admin"
    stale_workspace_link.click()
    expect(admin_page).to_have_url(re.compile(r"/ui/workspace\.html(?:[?#].*)?$"), timeout=ui_config.timeout_ms)
    workspace_file_list = admin_page.locator("#file-list")
    expect(workspace_file_list).to_have_attribute("aria-busy", "false", timeout=ui_config.timeout_ms)
    expect(workspace_file_list).not_to_contain_text(artifact_name)
    missing_file_list = live_api.get_json(
        "/workspace/files",
        save_as="state/artifact-workspace-missing-file-after-card-navigation.json",
    )
    assert all(int(item["id"]) != file_id for item in missing_file_list.get("files", []))
    artifact_case.attest_control_state(
        control_key="@selector:.created-files-link",
        state="abnormal",
        assertion="削除済み成果物cardから遷移しても欠落fileをworkspace上の作成済み成果物として表示しなかった",
    )
    artifact_case.screenshot(admin_page, 40, "chat-missing-created-file-not-shown-as-workspace-success")


def test_codex_native_resume_fallback_and_new_session(admin_page, live_api, ui_config, artifact_case, real_world, isolated_stack):
    settings = live_api.get_json("/settings", save_as="state/codex-resume-settings.json")
    prepare_chat(admin_page, ui_config, real_world)
    cid, _, first_assistant, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "SHERPA-LIVE-ALPHA-927 の税率と根拠ファイルを実ツールで確認してください。",
    )
    artifact_case.add_cleanup(f"delete conversation {cid}", lambda: _cleanup_conversation(live_api, cid))
    provider = str(((first_assistant.get("answer") or {}).get("usage") or {}).get("provider") or "")
    first_session = conversation_session_id(ui_config.database_url, cid)
    artifact_case.register_secret(first_session)
    if provider != "codex":
        assert first_session is None, "a non-Codex profile persisted a Codex native session"
        artifact_case.write_json(
            "state/non-codex-session-boundary.json",
            {"provider": provider, "native_session_absent": True},
        )
        artifact_case.screenshot(admin_page, 10, f"chat-{provider}-native-session-boundary")
        return

    assert first_session, "Codex completed without persisting its native session"
    native_invocations: list[dict] = []
    same_cid, _, native_assistant, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "同じ会話の続きとして、端数処理とNIGHTLYからの呼出関係も実ツールで確認してください。",
        codex_invocations=native_invocations,
    )
    assert same_cid == cid
    native_session = conversation_session_id(ui_config.database_url, cid)
    artifact_case.register_secret(native_session)
    assert native_session == first_session, "Codex native resume did not continue the original session"
    assert any(observation.get("resume") is True for observation in native_invocations), (
        "persisted Codex session stayed unchanged, but no native resume subprocess was observed"
    )
    assert "NIGHTLY" in str(native_assistant.get("answer") or native_assistant.get("content") or "")
    artifact_case.screenshot(admin_page, 20, "chat-codex-native-resume-same-session-complete")

    missing_session = unique_id("codex-session-not-present")
    artifact_case.register_secret(missing_session)
    set_nonexistent_conversation_session(ui_config.database_url, cid, missing_session)
    fallback_log_offsets = _application_log_offsets(ui_config)
    fallback_invocations: list[dict] = []
    fallback_cid, _, fallback_assistant, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "履歴を保ったまま、税率の正本と参考資料の違いを改めて実ツールで確認してください。",
        codex_invocations=fallback_invocations,
    )
    assert fallback_cid == cid
    fallback_session = conversation_session_id(ui_config.database_url, cid)
    artifact_case.register_secret(fallback_session)
    assert fallback_session and fallback_session not in {missing_session, first_session}, (
        "failed native resume did not replace the missing session with a fresh real session"
    )
    assert any(observation.get("resume") is False for observation in fallback_invocations), (
        "missing-session turn completed, but its fresh fallback Codex subprocess was not observed"
    )
    assert "12.5" in str(fallback_assistant.get("answer") or fallback_assistant.get("content") or "")
    deadline = time.monotonic() + max(ui_config.timeout_ms / 1000, 10)
    fallback_log_text = ""
    fallback_log_evidence: list[dict] = []
    while time.monotonic() < deadline:
        fallback_log_text, fallback_log_evidence = _application_log_suffix(fallback_log_offsets)
        if "codex resume failed (no output)" in fallback_log_text and f"sid={missing_session}" in fallback_log_text:
            break
        time.sleep(0.1)
    resume_failure_logged = "codex resume failed (no output)" in fallback_log_text
    missing_session_correlated = f"sid={missing_session}" in fallback_log_text
    assert resume_failure_logged and missing_session_correlated, (
        "the post-checkpoint application log did not correlate this exact missing Codex session with its native-resume failure"
    )
    artifact_case.write_json(
        "state/codex-fallback-log-correlation.json",
        {
            "checkpoint_scope": "current-turn-only",
            "resume_failure_logged": resume_failure_logged,
            "missing_session_correlated_before_redaction": missing_session_correlated,
            "raw_log_persisted": False,
            "log_suffixes": fallback_log_evidence,
        },
    )
    artifact_case.screenshot(admin_page, 30, "chat-codex-missing-session-fell-back-to-fresh-session")

    admin_page.locator("#newbtn").click()
    new_conversation_invocations: list[dict] = []
    new_cid, _, _, _ = _complete_real_turn(
        admin_page,
        live_api,
        ui_config,
        artifact_case,
        settings,
        "SHERPA-LIVE-REFERENCE-314 の運用時刻を新しい会話として実ツールで確認してください。",
        codex_invocations=new_conversation_invocations,
    )
    artifact_case.add_cleanup(
        f"delete conversation {new_cid}",
        lambda: _cleanup_conversation(live_api, new_cid),
    )
    assert new_cid != cid
    new_session = conversation_session_id(ui_config.database_url, new_cid)
    artifact_case.register_secret(new_session)
    assert new_session and new_session not in {first_session, fallback_session}, "a new conversation reused a prior Codex native session"
    assert any(observation.get("resume") is False for observation in new_conversation_invocations), (
        "new conversation launched Codex through resume instead of a fresh native session"
    )
    artifact_case.write_json(
        "state/codex-session-lifecycle.json",
        {
            "provider": provider,
            "native_resume_same": native_session == first_session,
            "fallback_replaced_missing": fallback_session not in {missing_session, first_session},
            "new_conversation_new_session": new_session not in {first_session, fallback_session},
            "native_resume_invocations": native_invocations,
            "missing_session_fallback_invocations": fallback_invocations,
            "new_conversation_invocations": new_conversation_invocations,
            "session_hashes": {
                "first": _session_digest(first_session),
                "fallback": _session_digest(fallback_session),
                "new": _session_digest(new_session),
            },
        },
    )
    artifact_case.screenshot(admin_page, 40, "chat-codex-new-conversation-has-distinct-session")
