"""回答ごとの利用者フィードバック（👍/👎）UI の e2e（mock_api）。

- 回答カード末尾に 👍/👎 ボタンが表示される。
- 👍 は即送信（タグ/一言なし）。送信後は選んだボタンに .on が付き、送信済みの一言が表示される。
- 👎 はタグ選択＋一言入力のポップを開閉し、「送信」でまとめて送る。
- サーバが拒否（403・共有された会話等）した場合は toast でエラーを表示する。
"""
from __future__ import annotations

import json
import re

from mock_api import IMPACT_ANSWER, install_api_mocks


def _answer_events(message_id: int = 5001):
    return [
        {"type": "answer", "conversation_id": 101,
         "message": {"id": message_id, "trace": [], "answer": IMPACT_ANSWER}},
    ]


def test_feedback_thumbs_up_sends_immediately(page, web_base_url):
    from playwright.sync_api import expect

    calls: list[dict] = []
    install_api_mocks(page, stream_events=_answer_events())

    def handle_feedback(route):
        calls.append(json.loads(route.request.post_data))
        route.fulfill(content_type="application/json",
                     body=json.dumps({"ok": True, "message_id": 5001, "rating": "up",
                                      "tags": [], "comment": None}))
    page.route("**/chat/101/messages/5001/feedback", handle_feedback)

    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    fb = page.locator(".msg-feedback").last
    expect(fb).to_be_visible()
    fb.locator('[data-fb="up"]').click()

    expect(fb.locator(".fbthanks")).to_be_visible()
    expect(fb.locator('[data-fb="up"]')).to_have_class(re.compile(r"\bon\b"))
    assert calls == [{"rating": "up", "tags": [], "comment": ""}]


def test_feedback_thumbs_down_opens_panel_and_sends_tags_and_comment(page, web_base_url):
    from playwright.sync_api import expect

    calls: list[dict] = []
    install_api_mocks(page, stream_events=_answer_events())

    def handle_feedback(route):
        calls.append(json.loads(route.request.post_data))
        route.fulfill(content_type="application/json",
                     body=json.dumps({"ok": True, "message_id": 5001, "rating": "down",
                                      "tags": ["wrong_evidence"], "comment": "根拠が古い"}))
    page.route("**/chat/101/messages/5001/feedback", handle_feedback)

    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    fb = page.locator(".msg-feedback").last
    expect(fb).to_be_visible()
    fb.locator('[data-fb="down"]').click()
    expect(fb.locator(".fbpanel")).to_be_visible()

    fb.locator('.fbtags input[value="wrong_evidence"]').check()
    fb.locator(".fbcomment").fill("根拠が古い")
    fb.locator("[data-fb-send]").click()

    expect(fb.locator(".fbthanks")).to_be_visible()
    expect(fb.locator(".fbpanel")).to_be_hidden()
    assert calls == [{"rating": "down", "tags": ["wrong_evidence"], "comment": "根拠が古い"}]


def test_feedback_denied_shows_toast_error(page, web_base_url):
    """共有された会話等でサーバが 403 を返した場合、toast でエラーを表示する。"""
    from playwright.sync_api import expect

    install_api_mocks(page, stream_events=_answer_events())

    def handle_feedback(route):
        route.fulfill(status=403, content_type="application/json",
                     body=json.dumps({"detail": "共有された会話にはフィードバックを送信できません"}))
    page.route("**/chat/101/messages/5001/feedback", handle_feedback)

    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    fb = page.locator(".msg-feedback").last
    fb.locator('[data-fb="up"]').click()

    expect(page.locator("#toast")).to_contain_text("共有された会話にはフィードバックを送信できません")
    expect(fb.locator(".fbthanks")).to_be_hidden()


def test_feedback_buttons_show_visible_japanese_labels(page, web_base_url):
    """👍/👎 ボタンは絵文字だけでなく可視テキストラベル（役に立った/役に立たなかった）を持つ
    （絵文字の意味が読み取れない利用者にも伝わるように・ツールチップ頼みにしない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, stream_events=_answer_events())
    page.goto(f"{web_base_url}/chat.html")
    page.locator("#input").fill("消費税率を変えたい。影響は？")
    page.locator("#send").click()

    fb = page.locator(".msg-feedback").last
    expect(fb).to_be_visible()
    expect(fb.locator('[data-fb="up"] .fblabel')).to_have_text("役に立った")
    expect(fb.locator('[data-fb="down"] .fblabel')).to_have_text("役に立たなかった")


def test_feedback_state_restored_from_conversation_history(page, web_base_url):
    """会話を開き直すと、サーバが同梱した feedback（rating/tags/comment）で選択状態を復元する
    （上書き前に現在値を表示）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_conversation(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "conversation": {"id": 109, "title": "フィードバック復元確認", "origin": "own",
                             "contains_personal_workspace": False},
            "messages": [
                {"id": 5101, "role": "user", "content": "消費税率を変えたい。影響は？",
                 "created_at": "2026-08-28T00:00:00+00:00"},
                {"id": 5102, "role": "assistant", "content": IMPACT_ANSWER["headline"],
                 "answer": IMPACT_ANSWER, "trace": [],
                 "feedback": {"rating": "down", "tags": ["slow"], "comment": "遅かった"},
                 "created_at": "2026-08-28T00:00:05+00:00"},
            ],
        }))
    page.route("**/conversations/109", handle_conversation)

    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(109)")

    fb = page.locator(".msg-feedback").last
    expect(fb).to_be_visible()
    expect(fb.locator('[data-fb="down"]')).to_have_class(re.compile(r"\bon\b"))
    expect(fb.locator('[data-fb="up"]')).not_to_have_class(re.compile(r"\bon\b"))
    expect(fb.locator(".fbthanks")).to_be_visible()
    expect(fb.locator(".fbpanel")).to_be_visible()
    expect(fb.locator('.fbtags input[value="slow"]')).to_be_checked()
    expect(fb.locator(".fbcomment")).to_have_value("遅かった")
