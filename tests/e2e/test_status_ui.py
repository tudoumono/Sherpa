from __future__ import annotations

from mock_api import install_api_mocks


def test_status_page_shows_ai_components_including_gemini(page, web_base_url):
    """UI フィードバック4（2026-07-03）: システム状態ページに AI 各プロバイダ（gemini 含む・
    旧実装は含んでいなかった）が表示される。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/status.html")

    tbody = page.locator("#health-tbody")
    expect(tbody).to_contain_text("OpenAI API")
    expect(tbody).to_contain_text("Gemini（Google）")
    expect(tbody).to_contain_text("AWS Bedrock（Claude）")
    expect(tbody).to_contain_text("ローカルLLM（Ollama）")
    expect(tbody).to_contain_text("Codex CLI（AIエージェント）")
    # 失敗した項目には対処ヒントが出る（bedrock は既定モックで認証失敗にしてある）。
    expect(tbody).to_contain_text("認証失敗")


def test_status_recheck_button_shows_checking_state_then_result(page, web_base_url):
    """UI フィードバック4: 「再チェック」クリック中はボタンがdisabled＋スピナー表示になり、
    各行が「確認中…」になる。完了すると結果に置き換わり、ボタンも元に戻る。

    /admin/health（refresh=1 のみ）を明示的に保留し、チェック中の状態を決定的に観測する
    （mock_api の既定モックは即座に応答するため、そのままでは中間状態を捉えられない・
    参照: [[feedback_e2e_sse_mock_timing]] と同種の「保留してから明示的に fulfill する」手法）。
    """
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}

    def handle_health(route):
        qs = route.request.url
        if "refresh=1" in qs:
            pending["route"] = route   # 再チェック分だけ保留（初期ロードは通常どおり即応答させたいので別ルート）
            return
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "status": "ok", "checked_at": "2026-07-03T09:00:00+00:00", "ttl_seconds": 15,
            "components": [{"id": "postgres", "label": "PostgreSQL", "impact": "down",
                            "ok": True, "detail": None, "latency_ms": 3}],
        }))

    page.route("**/admin/health**", handle_health)
    page.goto(f"{web_base_url}/status.html")
    expect(page.locator("#health-tbody")).to_contain_text("PostgreSQL")

    page.locator("#recheck-btn").click()

    btn = page.locator("#recheck-btn")
    expect(btn).to_be_disabled()
    expect(btn).to_contain_text("確認中")
    expect(page.locator("#health-tbody")).to_contain_text("確認中…")

    # 保留していた再チェック分を、AI を含む完全な結果で応答させる。
    body = json.dumps({"status": "ok", "checked_at": "2026-07-03T09:05:00+00:00", "ttl_seconds": 15,
                       "components": [
                           {"id": "postgres", "label": "PostgreSQL", "impact": "down",
                            "ok": True, "detail": None, "latency_ms": 3},
                           {"id": "gemini", "label": "Gemini（Google）", "impact": "none",
                            "ok": True, "detail": None, "latency_ms": 150},
                       ]})
    pending["route"].fulfill(status=200, content_type="application/json", body=body)

    expect(btn).not_to_be_disabled()
    expect(btn).to_have_text("再チェック")
    expect(page.locator("#health-tbody")).to_contain_text("Gemini（Google）")


def test_status_polling_pauses_when_hidden_and_resumes_once_visible(page, web_base_url):
    """性能是正②（Sherpa.visibilityInterval・性能台帳 QW4）: status.html の /admin/health
    定期ポーリングは非表示タブでは止まり、可視化に戻った瞬間に1回即時実行してから再開する。

    `page.clock.fast_forward` はページ内の仮想時刻を進めるだけで、モック応答（Playwright
    ルート・実ネットワーク相当の非同期往復）の到達は保証しない。各ステップ後に
    `page.wait_for_timeout` の実時間待ちを挟んで確定させてから件数を検証する。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    calls = {"n": 0}

    def handle_health(route):
        calls["n"] += 1
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "status": "ok", "checked_at": "2026-07-03T09:00:00+00:00", "ttl_seconds": 15,
            "components": [{"id": "postgres", "label": "PostgreSQL", "impact": "down",
                            "ok": True, "detail": None, "latency_ms": 3}],
        }))

    page.route("**/admin/health**", handle_health)
    # clock は goto の前に install する（ページ読込直後に張られる setInterval 自体を仮想化
    # するため）。goto の後に install すると、その setInterval は実タイマーのまま残り、
    # 「hidden 中は呼ばれない」assert が単に短時間しか待っていないだけの空振りになりうる。
    page.clock.install()
    page.goto(f"{web_base_url}/status.html")
    expect(page.locator("#health-tbody")).to_contain_text("PostgreSQL")
    assert calls["n"] == 1   # init() の初回呼び出し

    _set_tab_hidden(page, True)
    page.clock.fast_forward(60000)   # POLL_MS（45秒）を超えても非表示中は呼ばれない
    page.wait_for_timeout(200)
    assert calls["n"] == 1

    _set_tab_hidden(page, False)
    page.wait_for_timeout(200)
    assert calls["n"] == 2   # 可視化に戻った瞬間の即時1回

    page.clock.fast_forward(46000)   # 再開した定期ポーリングが動く
    page.wait_for_timeout(200)
    assert calls["n"] == 3


def _set_tab_hidden(page, hidden: bool) -> None:
    """Page Visibility API を Playwright から模擬する（`document.hidden` は読取専用の
    getter のため、own-property で上書きしてから `visibilitychange` を発火させる）。"""
    page.evaluate(
        "(h) => { Object.defineProperty(document, 'hidden', { value: h, configurable: true }); "
        "document.dispatchEvent(new Event('visibilitychange')); }",
        hidden,
    )
