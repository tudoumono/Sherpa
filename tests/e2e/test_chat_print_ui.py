"""会話全体の「PDF（印刷で保存）」の e2e（mock_api）。

- メニューの PDF で #print-root に質問と回答カードの本文が入る（ボタン・思考の流れ・履歴は入らない）。
- 印刷の表示（emulate_media=print）では #print-root だけが見え、履歴・思考の流れ・入力欄は見えない。
- ダークテーマでも印刷は白地・黒字。
"""
from __future__ import annotations

import json

import pytest

from mock_api import install_api_mocks

expect = pytest.importorskip("playwright.sync_api").expect

Q = "消費税率を変えると何に影響しますか"
BODY = "請求バッチの税計算が影響を受けます。\n\n| 項目 | 内容 |\n| --- | --- |\n| 税率 | 10% |"
DOC = "4期/02_設計/01_基本設計/税計算仕様書.md"
ANSWER = {
    "lens": "investigate",
    "body": BODY,
    "headline": BODY,
    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
    "how": {"mode": "investigate", "doc_focus": False},
    "referenced_docs": [{"path": DOC, "ranges": [[3, 5]]}],
    "data": {"reconciliation": [
        {"item": "税率の初期値", "verdict": "mismatch",
         "spec": {"text": "8%", "doc_id": DOC, "line": 3},
         "source": {"text": "10%", "doc_id": "TAXCALC.cbl", "line": 12}},
    ]},
    "usage": {"input_tokens": 1200, "output_tokens": 300},
}
TRACE = [{"type": "node", "id": "understand", "kind": "thought", "status": "done", "label": "依頼を理解", "detail": "x"}]
CONV = {
    "conversation": {"id": 777, "title": "税率の会話", "origin": "own", "version": "v1", "read_only": False,
                     "contains_personal_workspace": False},
    "messages": [
        {"id": 1, "role": "user", "content": Q, "created_at": "2026-07-01T08:40:00+00:00"},
        {"id": 2, "role": "assistant", "answer": ANSWER, "trace": TRACE, "created_at": "2026-07-01T08:41:20+00:00"},
    ],
}


def _open_with_conv(page, base):
    install_api_mocks(page)
    page.route("**/conversations/777", lambda r: r.fulfill(
        status=200, content_type="application/json", body=json.dumps(CONV, ensure_ascii=False)))
    page.goto(f"{base}/chat.html")
    page.evaluate("window.__sherpaChatTest.openConversation(777)")
    expect(page.locator("#messages")).to_contain_text("請求バッチの税計算")


def _press_pdf(page):
    page.evaluate("window.__printed = 0; window.print = () => { window.__printed += 1; }; 0")
    page.locator("#exportbtn").click()
    page.locator("#exportmenu [data-exp='pdf']").click()
    assert page.evaluate("window.__printed") == 1


def test_pdf_builds_print_root_with_question_and_answer_card_only(page, web_base_url):
    _open_with_conv(page, web_base_url)
    _press_pdf(page)

    root = page.locator("#print-root")
    expect(root.locator(".pr-title")).to_have_text("税率の会話")
    expect(root.locator(".pr-qtext")).to_have_text(Q)
    expect(root).to_contain_text("請求バッチの税計算が影響を受けます")
    expect(root.locator("table.md-table")).to_contain_text("10%")
    expect(root.locator(".reconcile")).to_contain_text("食い違い")
    expect(root).to_contain_text("参照した資料")
    expect(root).to_contain_text("税計算仕様書.md")
    for sel in ("button", "input", "textarea", ".msg-feedback", ".usage-meta", "[data-showtrace]", ".welcome-msg", ".fturn"):
        expect(root.locator(sel)).to_have_count(0)
    assert page.evaluate("document.getElementById('print-root').parentElement === document.body")

    page.evaluate("window.dispatchEvent(new Event('afterprint'))")
    expect(page.locator("#print-root")).to_have_count(0)


def test_pdf_marks_running_turn_as_pending(page, web_base_url):
    _open_with_conv(page, web_base_url)
    page.evaluate("""() => {
      const d = document.createElement('div'); d.className = 'msg';
      d.innerHTML = '<div class="a-row"><div class="a-body"><div class="thinking">回答を作成しています...</div></div></div>';
      document.getElementById('messages').appendChild(d);
    }""")
    page.evaluate("window.__sherpaChatTest.sending = true")
    _press_pdf(page)
    expect(page.locator("#print-root .pr-pending")).to_have_text("（回答中です。この回答は含まれていません）")
    expect(page.locator("#print-root .thinking")).to_have_count(0)


def test_pdf_keeps_a_failed_turn_message_when_nothing_is_running(page, web_base_url):
    _open_with_conv(page, web_base_url)
    page.evaluate("""() => {
      const d = document.createElement('div'); d.className = 'msg';
      d.innerHTML = '<div class="a-row"><div class="a-body"><div class="thinking">接続エラー。もう一度お試しください。</div></div></div>';
      document.getElementById('messages').appendChild(d);
    }""")
    _press_pdf(page)
    expect(page.locator("#print-root .pr-pending")).to_have_count(0)
    expect(page.locator("#print-root")).to_contain_text("接続エラー。もう一度お試しください。")


def test_printing_other_pages_is_not_blank(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/manual.html")
    page.emulate_media(media="print")
    assert page.evaluate("[...document.body.children].some((el) => getComputedStyle(el).display !== 'none' && el.tagName !== 'SCRIPT')")


def test_pdf_without_conversation_shows_toast_and_does_not_print(page, web_base_url):
    install_api_mocks(page)
    page.goto(f"{web_base_url}/chat.html")
    page.evaluate("window.__printed = 0; window.print = () => { window.__printed += 1; }; 0")
    page.locator("#exportbtn").click()
    page.locator("#exportmenu [data-exp='pdf']").click()
    expect(page.locator("#toast")).to_contain_text("書き出す内容がありません")
    assert page.evaluate("window.__printed") == 0
    expect(page.locator("#print-root")).to_have_count(0)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_print_media_shows_only_print_root_in_light_colors(page, web_base_url, theme):
    _open_with_conv(page, web_base_url)
    page.evaluate(f"document.documentElement.dataset.theme = '{theme}'")
    _press_pdf(page)
    page.emulate_media(media="print")

    expect(page.locator("#print-root")).to_be_visible()
    for sel in (".pane.left", ".pane.right", ".composer", "#messages", "sherpa-topbar"):
        expect(page.locator(sel)).to_be_hidden()
    assert page.evaluate("getComputedStyle(document.body).backgroundColor") == "rgb(255, 255, 255)"
    assert page.evaluate("getComputedStyle(document.querySelector('#print-root .pr-qtext')).color") == "rgb(0, 0, 0)"
    # 画面の高さを超えて通常の流れで全部出る（中央の領域で切れない）
    assert page.evaluate("document.getElementById('print-root').scrollHeight >= document.getElementById('print-root').clientHeight")
    assert page.evaluate("getComputedStyle(document.getElementById('print-root')).overflow") == "visible"
    # 表は紙幅に収まる
    assert page.evaluate("""() => { const t = document.querySelector('#print-root .recon-table');
      return t.getBoundingClientRect().right <= document.getElementById('print-root').getBoundingClientRect().right + 1; }""")
