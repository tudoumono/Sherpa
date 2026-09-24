from __future__ import annotations

from playwright.sync_api import expect

from .database import real_ai_turn_checkpoint


def prepare_chat(page, config, world_id: str) -> None:
    page.goto(config.base_url + "/ui/chat.html")
    expect(page.locator("#input")).to_be_visible()
    page.wait_for_function(
        "() => { const s=document.getElementById('version'); return s && s.options.length && s.value; }",
        timeout=config.timeout_ms,
    )
    world_select = page.locator("#version")
    if world_select.input_value() != world_id:
        # 複数Worldがあるときだけ選択UIが表示される。1件のときは
        # 製品が自動選択した隠しselectを強制操作せず、実効値を検証する。
        expect(world_select).to_be_visible()
        world_select.select_option(world_id)
    assert world_select.input_value() == world_id
    kb = page.locator("#kbtoggle")
    if kb.get_attribute("aria-pressed") != "true":
        expect(kb).to_be_enabled()
        kb.click()
    expect(kb).to_have_attribute("aria-pressed", "true")


def start_turn_from_ui(page, config, question: str) -> dict:
    checkpoint = real_ai_turn_checkpoint(config.database_url, config.expected_env_path)
    page.locator("#input").fill(question)
    with page.expect_response(
        lambda response: response.request.method == "POST" and response.url.endswith("/chat/turns"),
        timeout=config.timeout_ms,
    ) as start_info:
        page.locator("#send").click()
    response = start_info.value
    assert response.status == 200, response.text()
    started = response.json()
    assert started.get("turn_id") and started.get("conversation_id"), started
    started["_real_ai_checkpoint"] = checkpoint
    return started


def wait_for_completed_ui(page, timeout_ms: int) -> None:
    expect(page.locator("#rt")).to_contain_text("完了", timeout=timeout_ms)
    expect(page.locator("#messages .msg.user")).not_to_have_count(0)
    expect(page.locator("#messages .msg:not(.user)")).not_to_have_count(0)


def ui_trace_nodes(page) -> list[dict]:
    return (
        page.locator("#flow details.fturn")
        .last.locator(".fstep")
        .evaluate_all(
            """els => els.map(el => ({
          label: (el.querySelector('.flabel') || {}).textContent || '',
          detail: (el.querySelector('.fdetail') || {}).textContent || '',
          status: el.classList.contains('done') ? 'done' : (el.classList.contains('active') ? 'active' : ''),
          kind: el.classList.contains('tool') ? 'tool' : 'think'
        }))"""
        )
    )


def last_assistant_message(conversation: dict) -> dict:
    messages = conversation.get("messages") or []
    assistants = [message for message in messages if message.get("role") == "assistant"]
    assert assistants, f"conversation has no assistant message: {conversation}"
    return assistants[-1]
