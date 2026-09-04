from __future__ import annotations

import json
import re

from mock_api import USAGE_STATS_DEFAULT, install_api_mocks


def test_usage_trends_section_renders_all_new_metrics(page, web_base_url):
    """バッチ3（2026-07-03）: 「利用の傾向」セクション（ゼロヒット率・ヒートマップ・world別・
    頭脳別・週次アクティブ+再訪率・原本DL数）が実データで表示される。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html")

    # ゼロヒット率タイル（totals.zero_hit.rate=0.2142... → 21%）。
    expect(page.locator("#t-zerohit")).to_have_text("21%")

    # ランキング表にゼロヒット率列が追加され、seed どおりの値が入る（admin=27%, sato=0%）。
    admin_row = page.locator("#usage-tbody tr.u-row", has_text="admin").first
    expect(admin_row.locator(".zhr-cell")).to_have_text("27%")

    # ヒートマップ: 空状態が消え、SVG が描画される（セルが1つ以上存在する）。
    expect(page.locator("#heatmap-empty")).to_be_hidden()
    expect(page.locator("#heatmap-svg .cell")).to_have_count(24 * 7)

    # world別横棒: 空状態が消え、"test" ラベルの棒が描画される。
    expect(page.locator("#chart-world-empty")).to_be_hidden()
    expect(page.locator("#chart-world-svg")).to_contain_text("test")

    # 頭脳別横棒＋常設凡例（色だけに頼らない）。
    expect(page.locator("#chart-provider-empty")).to_be_hidden()
    legend = page.locator("#chart-provider-legend")
    expect(legend).to_contain_text("簡易（AIなし）")
    expect(legend).to_contain_text("OpenAI API")
    expect(legend).to_contain_text("Codex")

    # 週次アクティブユーザー＋再訪率（seed: revisit_rate=0.5 → 50%）。
    expect(page.locator("#chart-weekly-empty")).to_be_hidden()
    expect(page.locator("#revisit-rate-val")).to_have_text("50%")

    # 原本ダウンロード数（日別トレンド）＋見出し脇の期間合計（RV LOW再検証・seed: downloads.total=5）。
    expect(page.locator("#chart-dl-empty")).to_be_hidden()
    expect(page.locator("#dl-total-badge")).to_have_text("期間合計 5件")

    # F3（2026-07-07／2026-07-08 金額表示は撤去）: トークン（tiles・頭脳/モデル別表・日別チャート）。
    expect(page.locator("#t-tok-input")).to_have_text("12,000")
    expect(page.locator("#t-tok-output")).to_have_text("1,800")
    expect(page.locator("#chart-tokin-empty")).to_be_hidden()
    model_tbody = page.locator("#token-model-tbody")
    expect(model_tbody).to_contain_text("Codex")
    expect(model_tbody).to_contain_text("gpt-5.5")
    expect(page.locator("#token-user-tbody")).to_contain_text("管理者")


def test_usage_trends_section_handles_empty_data_without_crashing(page, web_base_url):
    """空データ（dev DB 掃除済み・少数データでも壊れない描画が必須）: 各セクションが正直に
    空状態を表示し、JS エラーで画面全体が壊れない。"""
    from playwright.sync_api import expect

    empty = {
        "users": [], "totals": {"turns": 0, "active_users": 0, "conversations": 0},
        "daily": [], "period": {"start": "2026-06-04", "end": "2026-07-03", "days": 30},
        "zero_hit": {"knowledge_turns": 0, "zero_hit_turns": 0, "rate": None},
        "worlds": [], "providers": [], "heatmap": [],
        "retention": {"weekly": [], "revisit_rate": None},
        "downloads": {"total": 0, "daily": []},
    }
    install_api_mocks(page, usage_stats=empty)
    page.goto(f"{web_base_url}/usage.html")

    expect(page.locator("#t-zerohit")).to_have_text("—")
    expect(page.locator("#usage-tbody .empty-row")).to_be_visible()

    expect(page.locator("#heatmap-empty")).to_be_visible()
    expect(page.locator("#chart-world-empty")).to_be_visible()
    expect(page.locator("#chart-provider-empty")).to_be_visible()
    expect(page.locator("#chart-weekly-empty")).to_be_visible()
    expect(page.locator("#chart-dl-empty")).to_be_visible()
    expect(page.locator("#dl-total-badge")).to_have_text("期間合計 0件")
    expect(page.locator("#revisit-rate-val")).to_have_text("算出できません（データ不足）")

    # 空状態でも「利用の傾向」の各カードタイトル自体は表示され続けている（画面が壊れていない）。
    expect(page.locator("text=フォルダ別利用量")).to_be_visible()
    expect(page.locator("text=頭脳（AI）別利用比率")).to_be_visible()

    # F3: トークン表示も空状態で壊れない（tokens キーが無い応答＝undefined でも空表示）。
    expect(page.locator("#t-tok-input")).to_have_text("0")
    expect(page.locator("#chart-tokin-empty")).to_be_visible()
    expect(page.locator("#token-model-tbody .empty-row")).to_be_visible()


def test_usage_chat_notice_uses_plain_language_not_jargon(page, web_base_url):
    """統計チャットの案内文は honest_failure 等の専門語を使わず、平文（「見つからないと正直に
    答えた割合」）で説明する（`docs/04-画面の原則.md`＝専門用語ゼロ）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html")

    notice = page.locator(".uc-send-notice")
    expect(notice).to_contain_text("見つからないと正直に答えた割合")
    expect(notice).not_to_contain_text("honest_failure")


def test_usage_chat_shows_server_notes_as_hints(page, web_base_url):
    """サーバ応答の `notes`（例: 改善ログの要約を取得できなかった旨）は黙って捨てず、
    回答の下にヒントとして表示する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/usage.html")

    def handle_usage_chat(route):
        route.fulfill(content_type="application/json", body=json.dumps({
            "answer": "今月は特に目立った変化はありません。",
            "notes": ["改善ログの要約を取得できませんでした。"],
        }))
    page.route("**/admin/usage/chat", handle_usage_chat)

    page.locator("#usage-chat-input").fill("今月はどう？")
    page.locator("#usage-chat-send").click()

    messages = page.locator("#usage-chat-messages")
    expect(messages).to_contain_text("今月は特に目立った変化はありません。")
    expect(messages.locator(".uc-hint")).to_contain_text("改善ログの要約を取得できませんでした。")


def test_token_kind_table_renders(page, web_base_url):
    """S1（2026-07-15-LLMオーケストレーション実装計画.md §3）: 「用途別」表に日本語 kind ラベルと、
    usage を報告しないプロバイダ（Gemini の embed）の null トークンに対する「—」表示を確認する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # USAGE_STATS_DEFAULT（tokens.by_kind に chat/intent/embed の3行を含む）
    page.goto(f"{web_base_url}/usage.html")

    kind_tbody = page.locator("#token-kind-tbody")
    expect(page.locator("#token-kind-card")).to_be_visible()
    expect(kind_tbody).to_contain_text("会話")          # kind=chat
    expect(kind_tbody).to_contain_text("依頼の仕分け")     # kind=intent
    expect(kind_tbody).to_contain_text("検索の索引づくり")  # kind=embed

    # gemini/embed 行はトークン列が全て null（報告不能マーカー）＝「—」で表示される。
    embed_row = kind_tbody.locator("tr", has_text="検索の索引づくり")
    expect(embed_row).to_contain_text("—")


def test_token_kind_table_hidden_when_absent(page, web_base_url):
    """`tokens.by_kind` が無い応答（旧 API 互換）でも pageerror なく「用途別」カードが隠れる。"""
    from playwright.sync_api import expect

    install_api_mocks(page, usage_stats={})
    page.goto(f"{web_base_url}/usage.html")

    expect(page.locator("#token-kind-card")).to_be_hidden()


def test_usage_period_switch_refetches_and_rerenders_trends(page, web_base_url):
    """期間切替（7/30/90日）に「利用の傾向」セクションも追従する。"""
    from playwright.sync_api import expect

    seven_day = dict(USAGE_STATS_DEFAULT)
    seven_day["period"] = {"start": "2026-06-27", "end": "2026-07-03", "days": 7}
    seven_day["zero_hit"] = {"knowledge_turns": 2, "zero_hit_turns": 1, "rate": 0.5}

    records = install_api_mocks(page, usage_stats=seven_day)
    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#t-zerohit")).to_have_text("50%")   # 初期表示（既定30日）でも同じモックが返る

    page.locator("[data-days='7']").click()
    expect(page.locator(".period-bar [data-days='7']")).to_have_class(re.compile(r"\bon\b"))
    assert records["admin_usage_stats"][-1]["days"] == ["7"]
    expect(page.locator("#t-zerohit")).to_have_text("50%")


# ===== STAT-2: 統計チャットの「今回だけ」一時プロバイダ切替（保存しない・リクエスト単位） =====
# `POST /admin/usage/chat` は install_api_mocks の共通ハンドラに含まれないため、ここで個別に
# page.route を足す（既存の graph.html/ingest.html の e2e と同じ流儀・Playwright は後から登録した
# 方を先にマッチさせるため、install_api_mocks より後に登録すれば共通ハンドラを迂回できる）。

def _handle_usage_chat(sent_bodies, answer="テストの回答です", default_provider_used="openai",
                       endpoint_kind=None):
    """応答は実サーバと同じ形（`answer`/`provider_used`/`endpoint_kind`）。
    `provider_used` はリクエストの `provider`（一時上書き）があればそれを、無ければ
    `default_provider_used`（既定・実サーバの `usage_chat.effective` に相当）を返す——
    実サーバの「実際に使った provider を返す」契約を素直に模す。"""
    def _handle(route):
        body = json.loads(route.request.post_data or "{}")
        sent_bodies.append(body)
        provider_used = body.get("provider") or default_provider_used
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"answer": answer, "provider_used": provider_used,
                                       "endpoint_kind": endpoint_kind}))
    return _handle


def test_usage_chat_notice_shows_configured_provider_and_default_send_omits_override(page, web_base_url):
    """通知欄が管理画面の専用設定（GET /admin/settings の usage_chat.effective）を反映し、
    「今回だけ」トグルを触らずに送信すると provider を送らない（管理画面の設定どおりに任せる）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # SYSTEM_SETTINGS_VIEW.usage_chat.effective == "openai"（既定）
    sent_bodies: list = []
    page.route("**/admin/usage/chat", _handle_usage_chat(sent_bodies))

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")
    expect(page.locator('[data-uc-provider=""]')).to_have_attribute("aria-pressed", "true")

    page.locator("#usage-chat-input").fill("今月一番使っているユーザーは？")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages")).to_contain_text("テストの回答です")
    assert "provider" not in sent_bodies[-1], "既定のまま送信した場合は provider を送らない"


def test_usage_chat_temporary_toggle_overrides_one_send_without_persisting(page, web_base_url):
    """「今回だけ」トグルは画面の一時状態のみ（保存しない）: 選ぶとその回の送信にだけ
    provider を添える。「既定」へ戻すと以後の送信は再び provider を送らない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    sent_bodies: list = []
    page.route("**/admin/usage/chat", _handle_usage_chat(sent_bodies, answer="回答"))

    page.goto(f"{web_base_url}/usage.html")

    ollama_btn = page.locator('[data-uc-provider="ollama"]')
    ollama_btn.click()
    expect(ollama_btn).to_have_attribute("aria-pressed", "true")
    expect(page.locator('[data-uc-provider=""]')).to_have_attribute("aria-pressed", "false")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")

    page.locator("#usage-chat-input").fill("質問1")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages")).to_contain_text("回答")
    assert sent_bodies[-1].get("provider") == "ollama"

    page.locator('[data-uc-provider=""]').click()
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")

    page.locator("#usage-chat-input").fill("質問2")
    page.locator("#usage-chat-send").click()
    # 「回答」は1回目の送信で既に画面に出ているため、コンテナ全体への contain_text だと
    # 2回目の完了を待たずに（レースして）真になる。直近の吹き出し（.msg の最後の要素）だけを
    # 見て、2回目の応答が実際に反映されるまで待つ。
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("回答")
    assert "provider" not in sent_bodies[-1], "「既定」へ戻したので一時上書きは尾を引かない"


def test_usage_chat_shows_loading_state_before_settings_resolve_and_disables_send(page, web_base_url):
    """設定取得（GET /admin/settings）が完了するまで、通知欄は「確認中…」を表示し、
    'OpenAI' 等の未確認の値を先出ししない。送信ボタンも無効のまま
    （「今回だけ」上書きを選ぶまでは送信させない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    held: dict = {}

    def hold_settings(route):
        if route.request.method == "GET":
            held["route"] = route   # fulfill せず保留＝取得中の状態を作る
            return
        route.fallback()
    page.route("**/admin/settings", hold_settings)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("確認中…")
    expect(page.locator("#usage-chat-send")).to_be_disabled()

    held["route"].fallback()   # install_api_mocks の通常応答へ解放
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")
    expect(page.locator("#usage-chat-send")).to_be_enabled()


def test_usage_chat_settings_fetch_failure_shows_error_and_disables_send(page, web_base_url):
    """設定取得が失敗したら、通知欄に明示エラーを表示し送信ボタンを
    無効のままにする（黙って既定 'openai' 扱いで送信させない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)

    def fail_settings(route):
        if route.request.method == "GET":
            route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
            return
        route.fallback()
    page.route("**/admin/settings", fail_settings)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text(
        "送信先を取得できませんでした（再読み込みしてください）")
    expect(page.locator("#usage-chat-send")).to_be_disabled()

    # 「今回だけ」上書きを選べば、その送信先は確定しているため送信できる（設定取得の成否に
    # 関わらず、明示的な上書きは常に優先する）。
    page.locator('[data-uc-provider="ollama"]').click()
    expect(page.locator("#usage-chat-send")).to_be_enabled()


def test_usage_chat_notice_shows_cloud_openai_compatible_label_for_azure_endpoint(page, web_base_url):
    """OpenAI の接続先が実際には Azure/その他 OpenAI 互換エンドポイントの場合、「OpenAI」ではなく
    実態に即した表示にする（`openai_endpoint.effective.kind` に合わせる・
    Azure 等へ送っているのに OpenAI 社そのものへ送っていると誤解させない）。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["openai_endpoint"]["effective"]["kind"] = "azure"
    install_api_mocks(page, system_settings=settings)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: クラウド（OpenAI 互換）")


def test_usage_chat_refetches_settings_before_default_send_and_updates_note(page, web_base_url):
    """provider を省略する送信は、送信直前に管理設定を再取得し、
    ページ読み込み後に他セッションが変更した usage_chat_provider を通知欄へ反映する
    （表示と実際の送信先の食い違いを防ぐ）。"""
    from playwright.sync_api import expect
    import mock_api

    install_api_mocks(page)
    calls = {"n": 0}

    def settings_route(route):
        if route.request.method != "GET":
            route.fallback()
            return
        calls["n"] += 1
        if calls["n"] == 1:
            route.fallback()   # 初回（ページ読み込み時）は通常のモック応答（openai）
            return
        # 2回目以降（送信直前の再取得）は他セッションが ollama へ変更した状況を模す。
        changed = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
        changed["usage_chat"] = {"configured": "ollama", "effective": "ollama",
                                 "default": "openai", "providers": ["openai", "ollama"]}
        route.fulfill(status=200, content_type="application/json", body=json.dumps(changed))
    page.route("**/admin/settings", settings_route)
    sent_bodies: list = []
    # 送信直前の再取得後は system_settings が ollama を返す想定なので、POST 応答の
    # `provider_used`（省略送信＝実サーバが専用設定から解決する値）も ollama に揃える
    # （実サーバなら両者は常に同じ設定を見るため一致する）。
    page.route("**/admin/usage/chat", _handle_usage_chat(sent_bodies, default_provider_used="ollama"))

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")

    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages")).to_contain_text("テストの回答です")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")
    assert "provider" not in sent_bodies[-1], "省略のまま（サーバの現在設定に委ねる）"
    assert calls["n"] == 2, "初回読み込み＋送信直前の再取得の2回のはず"


def test_usage_chat_notice_updates_from_response_provider_used_after_send(page, web_base_url):
    """送信前の表示（予定）と応答の `provider_used` が食い違う場合、送信後の表示は応答の値を
    信頼する（GET と POST の間の競合を応答時点の確定値で吸収する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # usage_chat.effective == "openai"（送信前の「予定」表示はこちら）
    sent_bodies: list = []
    # 応答は pre-send の予定（openai）と異なる値（ollama）を返す＝実際に食い違いが起きた状況を模す。
    page.route("**/admin/usage/chat", _handle_usage_chat(sent_bodies, default_provider_used="ollama"))

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")   # 送信前は「予定」

    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages")).to_contain_text("テストの回答です")
    # 応答の provider_used（ollama）で表示が確定する。
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")


def test_usage_chat_blocks_default_send_when_saved_value_invalid(page, web_base_url):
    """`usage_chat.effective` が選択肢に無い（保存値が不正）場合、
    既定送信（provider 省略）は送信不可のまま——「今回だけ」で明示指定した場合のみ送信できる。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "gemini", "effective": "(不正な保存値)",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    install_api_mocks(page, system_settings=settings)
    sent_bodies: list = []
    page.route("**/admin/usage/chat", _handle_usage_chat(sent_bodies, default_provider_used="ollama"))

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_contain_text("不正です")
    expect(page.locator("#usage-chat-send")).to_be_disabled()

    # 「今回だけ」で明示指定すれば送信できる（上書き自体が送信先を確定させるため）。
    page.locator('[data-uc-provider="ollama"]').click()
    expect(page.locator("#usage-chat-send")).to_be_enabled()
    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages")).to_contain_text("テストの回答です")
    assert sent_bodies[-1].get("provider") == "ollama"


def test_usage_chat_override_send_in_flight_toggle_does_not_change_sent_request(page, web_base_url):
    """送信中にトグルを操作しても、実際に送るリクエストの provider は送信開始時点の値のまま。
    「次の送信先」（現在の選択・`#usage-chat-provider-note`）と「前回の送信先」（直前に実際に
    送った確定値・`#usage-chat-last-sent-note`）は別状態・別表示のため、応答到着後も互いを
    上書きしない（可変な現在のトグル状態を応答処理の時点で読み直すと、送信中の操作で
    「実際に送ったのとは違う値」を参照してしまう）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    held: dict = {}

    def hold_chat(route):
        if route.request.method == "POST":
            held["route"] = route   # fulfill せず保留＝送信中の状態を作る
            return
        route.fallback()
    page.route("**/admin/usage/chat", hold_chat)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")

    page.locator('[data-uc-provider="ollama"]').click()
    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    # 上書き送信も送信直前に設定を再取得してから POST するため、POST 到達までの間に
    # 追加のラウンドトリップが挟まる——固定時間内でポーリングして待つ（`expect_request` は
    # この追加の非同期区間と相性が悪く、まれに検出できないことがある）。
    import time
    deadline = time.time() + 5
    while "route" not in held and time.time() < deadline:
        page.wait_for_timeout(20)
    assert "route" in held, "送信（POST）が捕捉されているはず"

    # 送信が保留されている間に、別のトグル（openai）へ切り替える。「次の送信先」は即座に
    # 「予定」として変わるが、既に送ってしまったリクエスト自体は送信開始時点（ollama）のまま
    # 変わらない。
    page.locator('[data-uc-provider="openai"]').click()
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")

    body = json.loads(held["route"].request.post_data or "{}")
    assert body.get("provider") == "ollama", \
        "送信中にトグルを openai へ変えても、実際に送ったリクエストは送信開始時点の ollama のまま"

    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps({"answer": "回答", "provider_used": "ollama",
                                           "endpoint_kind": None}))
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("回答")
    # 応答の確定値（ollama・送信開始時点の上書き）は「前回の送信先」へ反映する。
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text("前回の送信先: ローカル（Ollama）")
    # 「次の送信先」は送信中に切り替えた現在の選択（openai）のまま——確定値の到着で
    # 上書き/巻き戻しされない。
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")


def test_usage_chat_default_send_toggled_during_settings_refetch_still_uses_captured_default(
        page, web_base_url):
    """既定送信（provider 省略）の直前の設定再取得（GET /admin/settings）が保留されている間に
    「今回だけ」トグルへ切り替えても、この送信は送信開始時点（override なし＝既定）のまま進む:
    本文に provider を含めず、応答の確定値は既定側（`_ucDefaultProvider`・「前回の送信先」）へ
    正しく反映する。可変な現在のトグル状態を再取得後に読み直すと、既定送信のはずが途中で
    「今回だけ」上書き送信に化けてしまう。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    held: dict = {}
    calls = {"get": 0}

    def hold_settings_refetch(route):
        if route.request.method != "GET":
            route.fallback()
            return
        calls["get"] += 1
        if calls["get"] == 1:
            route.fallback()   # 初回（ページ読み込み時）は通常のモック応答
            return
        held["settings_route"] = route   # 2回目（送信直前の再取得）を保留する
    page.route("**/admin/settings", hold_settings_refetch)

    sent_bodies: list = []
    page.route("**/admin/usage/chat",
              _handle_usage_chat(sent_bodies, default_provider_used="openai"))

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")

    page.locator("#usage-chat-input").fill("質問")
    with page.expect_request("**/admin/settings"):
        page.locator("#usage-chat-send").click()   # override なし＝既定送信として開始
    assert "settings_route" in held, "送信直前の設定再取得（GET）が保留されているはず"

    # 再取得が保留されている間に「今回だけ ollama」へ切り替える。「次の送信先」は即座に反映する。
    page.locator('[data-uc-provider="ollama"]').click()
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")

    held["settings_route"].fallback()   # 再取得を解放（通常のモック応答＝openai のまま）
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("テストの回答です")

    assert "provider" not in sent_bodies[-1], \
        "送信開始時点は既定（override なし）だったため、途中でトグルを変えても provider を送らない"
    # 応答の確定値（openai・既定側）は「前回の送信先」へ反映する。
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text("前回の送信先: OpenAI")
    # 「次の送信先」は送信中に切り替えた現在の選択（ollama）のまま——応答で汚染されない。
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")


def test_usage_chat_override_response_azure_kind_shown_in_last_sent_not_next_send(page, web_base_url):
    """「今回だけ」上書き送信の確定 provider_used/endpoint_kind は「前回の送信先」
    （`#usage-chat-last-sent-note`）へ反映する（「次の送信先」＝次回の既定送信の表示は
    汚染しない）。既定 Ollama で成功した直後に「今回だけ OpenAI」を選んで送信し、応答が
    Azure 経由（endpoint_kind="azure"）だった場合、「前回の送信先」は「OpenAI」ではなく
    「クラウド（OpenAI 互換）」と表示する。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "ollama", "effective": "ollama",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    install_api_mocks(page, system_settings=settings)
    sent_bodies: list = []

    def chat_route(route):
        body = json.loads(route.request.post_data or "{}")
        sent_bodies.append(body)
        if body.get("provider") == "openai":
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"answer": "回答2", "provider_used": "openai",
                                           "endpoint_kind": "azure"}))
        else:
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"answer": "回答1", "provider_used": "ollama",
                                           "endpoint_kind": None}))
    page.route("**/admin/usage/chat", chat_route)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")

    page.locator("#usage-chat-input").fill("質問1")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("回答1")
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text("前回の送信先: ローカル（Ollama）")

    page.locator('[data-uc-provider="openai"]').click()
    page.locator("#usage-chat-input").fill("質問2")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("回答2")
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text(
        "前回の送信先: クラウド（OpenAI 互換）")

    # 「次の送信先」（既定状態）は上書き送信の確定値で汚染されていない:「既定」へ戻すと ollama のまま。
    page.locator('[data-uc-provider=""]').click()
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")


def test_usage_chat_default_ollama_success_does_not_corrupt_openai_endpoint_kind(page, web_base_url):
    """既定 Ollama の送信結果（endpoint_kind=null）は、GET /admin/settings 由来の openai
    接続先種別（`_ucOpenaiEndpointKind`）を上書きしない。既定 Ollama で1回送信した直後に
    「今回だけ OpenAI」へ切り替えても、「次の送信先」は実際の接続先設定（Azure）どおり
    「クラウド（OpenAI 互換）」と表示する（直前の ollama 送信結果の null に化けて
    「OpenAI」に誤表示しない）。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "ollama", "effective": "ollama",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    settings["openai_endpoint"]["effective"]["kind"] = "azure"
    install_api_mocks(page, system_settings=settings)
    sent_bodies: list = []
    page.route("**/admin/usage/chat",
              _handle_usage_chat(sent_bodies, default_provider_used="ollama", endpoint_kind=None))

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")

    page.locator("#usage-chat-input").fill("質問1")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("テストの回答です")
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text("前回の送信先: ローカル（Ollama）")

    page.locator('[data-uc-provider="openai"]').click()
    # まだ送信していない時点の「次の送信先」——直前の ollama 送信結果に汚染されず、
    # 設定（azure）どおりに表示する。
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: クラウド（OpenAI 互換）")


def test_usage_chat_returning_to_default_after_override_send_still_shows_settings_error(
        page, web_base_url):
    """設定取得の失敗/不正状態（`_ucNoticeError`）は、「今回だけ」上書き送信が成功しても消えない
    ——「前回の送信先」は独立した別要素で確定値を表示し、「次の送信先」欄はエラーのまま。
    その後「既定」へ戻すと、古い送信先ではなく元のエラー案内へ戻る（送信不可の状態と表示が
    食い違わない）。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "gemini", "effective": "(不正な保存値)",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    install_api_mocks(page, system_settings=settings)
    page.route("**/admin/usage/chat", _handle_usage_chat([], default_provider_used="ollama"))

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_contain_text("不正です")
    expect(page.locator("#usage-chat-send")).to_be_disabled()

    page.locator('[data-uc-provider="ollama"]').click()
    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("テストの回答です")
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text("前回の送信先: ローカル（Ollama）")

    page.locator('[data-uc-provider=""]').click()
    # 送信不可のままで、表示も（古い送信先ではなく）元のエラー案内に戻る。
    expect(page.locator("#usage-chat-send")).to_be_disabled()
    expect(page.locator("#usage-chat-provider-note")).to_contain_text("不正です")


def test_usage_chat_502_failure_updates_last_sent_note_since_actually_sent(page, web_base_url):
    """502（実送信を試みたが失敗）は応答の provider_used/endpoint_kind で「前回の送信先」を
    更新する——実際に使った送信先は確定しているため（`common.js::api` が非2xx応答の JSON
    本文を `err.status`/`err.body` として渡す契約を利用する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)

    def chat_route(route):
        route.fulfill(status=502, content_type="application/json",
                      body=json.dumps({"detail": "送信に失敗しました", "provider_used": "openai",
                                       "endpoint_kind": "azure"}))
    page.route("**/admin/usage/chat", chat_route)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text(
        "前回の送信先: （まだ送信していません）")

    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("送信に失敗しました")
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text(
        "前回の送信先: クラウド（OpenAI 互換）")


def test_usage_chat_503_failure_does_not_update_last_sent_note_since_unsent(page, web_base_url):
    """503（未送信）は応答に provider_used が入っていても「前回の送信先」を更新しない
    ——実際には送っていないため（502 とは対称的な扱い）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)

    def chat_route(route):
        route.fulfill(status=503, content_type="application/json",
                      body=json.dumps({"detail": "未接続です", "provider_used": "openai",
                                       "endpoint_kind": "openai"}))
    page.route("**/admin/usage/chat", chat_route)

    page.goto(f"{web_base_url}/usage.html")
    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("未接続です")
    expect(page.locator("#usage-chat-last-sent-note")).to_have_text(
        "前回の送信先: （まだ送信していません）")


def test_usage_chat_override_send_refetches_settings_for_fresh_endpoint_kind(page, web_base_url):
    """「今回だけ openai」の送信も、送信直前に設定を再取得して openai 接続先種別
    （`_ucOpenaiEndpointKind`）を確定させてから送る——既定送信と同じ経路で再取得する
    （初回読み込み時点の古い/未確認だった接続先種別のまま送らない）。"""
    from playwright.sync_api import expect
    import mock_api

    install_api_mocks(page)
    calls = {"n": 0}

    def settings_route(route):
        if route.request.method != "GET":
            route.fallback()
            return
        calls["n"] += 1
        if calls["n"] == 1:
            route.fallback()   # 初回（ページ読み込み時）: openai_endpoint.kind = "openai"（既定）
            return
        # 2回目以降（送信直前の再取得）: 他セッションが Azure へ切り替えた状況を模す。
        changed = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
        changed["openai_endpoint"]["effective"]["kind"] = "azure"
        route.fulfill(status=200, content_type="application/json", body=json.dumps(changed))
    page.route("**/admin/settings", settings_route)

    sent_bodies: list = []
    page.route("**/admin/usage/chat", _handle_usage_chat(sent_bodies, endpoint_kind="azure"))

    page.goto(f"{web_base_url}/usage.html")
    page.locator('[data-uc-provider="openai"]').click()
    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("テストの回答です")
    assert calls["n"] == 2, "上書き送信でも送信直前に設定を再取得するはず"
    assert sent_bodies[-1].get("provider") == "openai"
    # 再取得後の openai 接続先種別（azure）が「次の送信先」表示にも反映される。
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: クラウド（OpenAI 互換）")


def test_usage_chat_override_send_blocked_when_settings_refetch_fails(page, web_base_url):
    """「今回だけ」上書き送信も、送信直前の設定再取得（GET /admin/settings）自体が失敗したら
    送信を中断し、明示エラーを表示する——接続先種別を確定できないまま送らない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    calls = {"n": 0}

    def fail_second_get(route):
        if route.request.method != "GET":
            route.fallback()
            return
        calls["n"] += 1
        if calls["n"] == 1:
            route.fallback()
            return
        route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
    page.route("**/admin/settings", fail_second_get)

    chat_calls: list = []

    def _record_and_fulfill(route):
        chat_calls.append(route.request.post_data)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"answer": "x", "provider_used": "ollama", "endpoint_kind": None}))
    page.route("**/admin/usage/chat", _record_and_fulfill)

    page.goto(f"{web_base_url}/usage.html")
    page.locator('[data-uc-provider="ollama"]').click()
    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("接続先の設定を確認できなかった")
    assert calls["n"] == 2, "上書き送信でも送信直前に再取得を試みるはず"
    assert chat_calls == [], "設定再取得に失敗したのに実送信してはいけない"


def test_usage_chat_override_shows_selection_while_settings_error_shown_separately(
        page, web_base_url):
    """設定取得失敗/保存値不正の状態でも、「今回だけ」上書きを選ぶと「次の送信先」欄
    （`#usage-chat-provider-note`）は上書きの選択（現在の選択）を優先して表示し、設定エラーの
    案内は別行（`#usage-chat-settings-error-note`）へ併記する——上書き中もエラーが見えなく
    ならない。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "gemini", "effective": "(不正な保存値)",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    install_api_mocks(page, system_settings=settings)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_contain_text("不正です")
    expect(page.locator("#usage-chat-settings-error-note")).to_be_hidden()

    page.locator('[data-uc-provider="ollama"]').click()
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: ローカル（Ollama）")
    expect(page.locator("#usage-chat-settings-error-note")).to_be_visible()
    expect(page.locator("#usage-chat-settings-error-note")).to_contain_text("不正です")

    page.locator('[data-uc-provider=""]').click()
    expect(page.locator("#usage-chat-provider-note")).to_contain_text("不正です")
    expect(page.locator("#usage-chat-settings-error-note")).to_be_hidden()


def test_usage_chat_default_send_refetch_failure_clears_stale_default_and_shows_error(
        page, web_base_url):
    """初回の設定取得（GET /admin/settings）が成功して「送信先: OpenAI」等の表示になった後、
    既定送信直前の再取得が失敗した場合、古い `_ucDefaultProvider` を残さず「次の送信先」を
    エラー表示に切り替える（POST は行わない）——最初の成功で残った既定値のせいで、実際には
    未確認なのに送信できるように見えてはいけない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    calls = {"n": 0}

    def settings_route(route):
        if route.request.method != "GET":
            route.fallback()
            return
        calls["n"] += 1
        if calls["n"] == 1:
            route.fallback()   # 初回（ページ読み込み時）は成功
            return
        route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
    page.route("**/admin/settings", settings_route)

    chat_calls: list = []

    def _record_and_fulfill(route):
        chat_calls.append(route.request.post_data)
        route.fulfill(status=200, content_type="application/json",
                      body=json.dumps({"answer": "x", "provider_used": "openai", "endpoint_kind": None}))
    page.route("**/admin/usage/chat", _record_and_fulfill)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")

    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text(
        "送信先の設定を確認できなかった")
    # 古い既定値（OpenAI）に化けて「送信できそう」に見えてはいけない——明示エラーへ切り替わる。
    expect(page.locator("#usage-chat-provider-note")).to_have_text(
        "送信先を取得できませんでした（再読み込みしてください）")
    assert chat_calls == [], "設定再取得に失敗したのに実送信してはいけない"
    assert calls["n"] == 2, "初回読み込み＋送信直前の再取得の2回のはず"


def test_usage_chat_stale_settings_response_does_not_overwrite_newer_one(page, web_base_url):
    """初期読み込みの設定取得（GET /admin/settings）が保留されている間に「今回だけ」上書き
    送信を行うと、送信直前の再取得（2回目の GET）が別に走る。この2回目が先に解決した後、
    保留していた1回目（古い世代）が遅れて到着しても、既に確定した状態を巻き戻してはいけない
    （世代番号で古い応答を捨てる）。"""
    from playwright.sync_api import expect
    import time

    install_api_mocks(page)
    held: dict = {}
    calls = {"n": 0}

    def settings_route(route):
        if route.request.method != "GET":
            route.fallback()
            return
        calls["n"] += 1
        if calls["n"] == 1:
            held["first"] = route   # 初回（ページ読み込み時）を保留する
            return
        route.fallback()   # 2回目（送信直前の再取得）はすぐ解決する（通常のモック応答）
    page.route("**/admin/settings", settings_route)
    page.route("**/admin/usage/chat", _handle_usage_chat([], default_provider_used="openai"))

    page.goto(f"{web_base_url}/usage.html")
    deadline = time.time() + 5
    while "first" not in held and time.time() < deadline:
        page.wait_for_timeout(20)
    assert "first" in held, "初回の設定取得（GET）が保留されているはず"
    expect(page.locator("#usage-chat-provider-note")).to_have_text("確認中…")

    # 初回取得が保留されている間に「今回だけ openai」を選んで送信する——上書き送信は
    # `_ucSettingsFetchOk`（2回目の再取得の成否）だけを見るため、初回取得の未完了に
    # 関わらず送信できる。
    page.locator('[data-uc-provider="openai"]').click()
    page.locator("#usage-chat-input").fill("質問")
    page.locator("#usage-chat-send").click()
    expect(page.locator("#usage-chat-messages .msg").last).to_contain_text("テストの回答です")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")

    # 保留していた初回（古い世代）の応答が今ごろ遅れて到着しても（今回はエラーを模す）、
    # 既に確定した表示・送信可否を巻き戻してはいけない。
    expect(page.locator("#usage-chat-settings-error-note")).to_be_hidden()
    held["first"].fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
    page.wait_for_timeout(100)
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")
    expect(page.locator("#usage-chat-send")).to_be_enabled()
    # 「次の送信先」欄は上書き中のため常に override を優先して表示する（世代ガードを外しても
    # ここは変わらず「送信先: OpenAI」のまま＝false green の原因）。世代ガードが実際に効いて
    # いることは、上書きに隠れないこの別行（`_ucNoticeError` 用）が非表示のままであることで
    # 確認する——世代ガードが無ければ、遅れて届いた 500 が `_ucNoticeError` を設定し、
    # override 中でも見えるこの別行が表示されてしまう。
    expect(page.locator("#usage-chat-settings-error-note")).to_be_hidden()


def test_usage_chat_openai_key_hint_shown_when_a7_not_openai(page, web_base_url):
    """A7（`cloud_provider`）が openai 以外（例: gemini）の間、「今回だけ OpenAI」ボタンの
    近くに、中央 OpenAI キーが実行構成が OpenAI の時しか使えない旨の注記を出す——A7 の
    排他選択契約により、この状態で「今回だけ OpenAI」を選んでも実際には 503（未接続）に
    なるため、選ぶ前に理由が分かるようにする。A7 が openai なら注記は出ない。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["cloud"]["provider"] = "gemini"
    install_api_mocks(page, system_settings=settings)

    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-openai-key-hint")).to_be_visible()
    expect(page.locator("#usage-chat-openai-key-hint")).to_contain_text(
        "OpenAI のキーは頭脳の選択が OpenAI のときだけ使えます")
    expect(page.locator("#usage-chat-openai-key-hint")).to_contain_text("現在: Gemini")


def test_usage_chat_openai_key_hint_hidden_when_a7_is_openai(page, web_base_url):
    """A7 が openai の間は、OpenAI キーが使えない旨の注記は出ない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # cloud.provider == "openai"（既定）
    page.goto(f"{web_base_url}/usage.html")
    expect(page.locator("#usage-chat-provider-note")).to_have_text("送信先: OpenAI")
    expect(page.locator("#usage-chat-openai-key-hint")).to_be_hidden()
