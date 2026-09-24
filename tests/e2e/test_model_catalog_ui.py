"""モデルカタログ（docs/proposals/2026-08-23-設定の責務再設計.md §9）の e2e。

- 管理画面: 「使えるモデル」表（1枚・列=選択中のクラウド AI＋Ollama＋Codex）の既定 select・
  一覧編集モーダル・Ollama 許可ホスト一覧の入力→保存・クラウド AI 切替への追従。
- 個人設定: モデル名欄がカタログ参照の <select> になり、カタログ外の保存値は警告付きで残る
  （移行期の寛容）。「管理者の既定を使う」を選ぶと明示的に空文字を送り既存値をクリアできる。
  Ollama 接続先は許可ホスト一覧（完全 URL）から選ぶ。
"""
from __future__ import annotations

import mock_api
from mock_api import SYSTEM_SETTINGS_VIEW, install_api_mocks


def open_tab(page, key):
    """管理画面の4タブのうち `key` を表に出す（test_admin_settings_ui.py と同じ流儀）。"""
    page.locator(f'.tab-btn[data-tab="{key}"]').click()


def open_advanced(page, tab_panel_id):
    """設計要件④の「詳細」折りたたみを開く。"""
    page.locator(f'#{tab_panel_id} details.adv summary').click()


# ===== 管理画面（admin-settings.html） =====

def test_admin_model_catalog_table_renders_selected_cloud_and_ollama_and_codex_columns(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)   # 既定モックの cloud.provider は "openai"
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    table = page.locator("#model-catalog-table")
    expect(table.locator("th")).to_have_count(4)   # 用途 + 3列（openai/ollama/codex）
    expect(table).to_contain_text("OpenAI")
    expect(table).to_contain_text("ローカル（Ollama）")
    expect(table).to_contain_text("Codex")
    expect(table).to_contain_text("チャット")   # 用途名（USAGE_LABELS）
    # openai/chat セルの既定 select に組み込み既定（gpt-5.5）が選ばれている。
    default_sel = table.locator("select.mc-default[data-provider='openai'][data-usage='chat']")
    expect(default_sel).to_have_value("gpt-5.5")


def test_admin_model_catalog_bedrock_column_is_not_editable(page, web_base_url):
    """Bedrock が選択中クラウド AI のときは、その列を編集不可（実在確認済みモデルの専用機構と
    重複させない・個人設定の Bedrock 欄へ誘導する注記のみ）。"""
    from playwright.sync_api import expect

    settings = {**SYSTEM_SETTINGS_VIEW, "cloud": {**SYSTEM_SETTINGS_VIEW["cloud"], "provider": "bedrock"}}
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    table = page.locator("#model-catalog-table")
    expect(table).to_contain_text("AWS Bedrock")
    expect(table.locator("select.mc-default[data-provider='bedrock']")).to_have_count(0)
    expect(page.locator("#model-catalog-card")).to_contain_text("個人設定")


def test_admin_model_catalog_table_follows_cloud_provider_switch(page, web_base_url):
    """クラウド AI のラジオを切り替えると、保存前でも「使えるモデル」表の1列目が即座に
    追従する（保存するまで古い列のまま、という食い違いを防ぐ）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # 既定は openai
    page.goto(f"{web_base_url}/admin-settings.html")   # 既定タブ＝プロバイダ＋接続先（クラウド AI ラジオが見える）

    table = page.locator("#model-catalog-table")
    open_tab(page, "models")
    expect(table).to_contain_text("OpenAI")
    expect(table).not_to_contain_text("Gemini")

    open_tab(page, "provider")   # クラウド AI ラジオはこちらのタブ
    page.locator("input[data-cloud-provider='gemini']").check()

    open_tab(page, "models")   # 表側は切替前でも DOM は即時更新済み（保存前でも反映される）
    expect(table).to_contain_text("Gemini")
    expect(table.locator("select.mc-default[data-provider='gemini'][data-usage='chat']")).to_have_value("gemini-2.5-flash")


def test_admin_model_catalog_empty_default_shows_explicit_placeholder_not_first_option(page, web_base_url):
    """重大バグ是正（RV 4巡目 #11）: セルの既定が空（未設定＝組み込み既定へ解決）のとき、
    どの <option> にも selected を付けないとブラウザが先頭の実モデル名を選択済みに見せてしまい
    「先頭のモデルが既定」だと誤認させる。明示的な「（未設定）」を先頭に置き、それが選ばれている
    ことを確認する。"""
    from playwright.sync_api import expect

    settings = {**SYSTEM_SETTINGS_VIEW, "model_catalog": {
        **SYSTEM_SETTINGS_VIEW["model_catalog"],
        "effective": {
            **SYSTEM_SETTINGS_VIEW["model_catalog"]["effective"],
            "openai": {
                **SYSTEM_SETTINGS_VIEW["model_catalog"]["effective"]["openai"],
                "chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": ""},
            },
        },
    }}
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    sel = page.locator("select.mc-default[data-provider='openai'][data-usage='chat']")
    expect(sel).to_have_value("")
    expect(sel.locator("option", has_text="（未設定）")).to_have_count(1)


def test_admin_model_catalog_change_default_and_save(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    sel = page.locator("select.mc-default[data-provider='openai'][data-usage='chat']")
    sel.select_option("gpt-5.4-mini")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["model_catalog"]["openai"]["chat"]["default"] == "gpt-5.4-mini"
    assert "gpt-5.4-mini" in put["model_catalog"]["openai"]["chat"]["allowed"]


def test_admin_model_catalog_cell_highlight_reflects_value_diff_not_configured_presence(page, web_base_url):
    """セルの強調は「configured にセルが存在する」ではなく「組み込み既定と値が異なる」で
    判定する。管理者が既定と同じ値をわざわざ明示保存していても、値が同じなら強調しない。"""
    from playwright.sync_api import expect

    settings = {
        **SYSTEM_SETTINGS_VIEW,
        "model_catalog": {
            **SYSTEM_SETTINGS_VIEW["model_catalog"],
            # openai/chat を明示保存済み（configured にセルが存在する）だが、値は組み込み既定と同一。
            "configured": {"openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"}}},
        },
    }
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    chat_cell = page.locator("td:has(select.mc-default[data-provider='openai'][data-usage='chat'])")
    expect(chat_cell).not_to_have_class("mc-changed")

    # 値を実際に既定から変えると、今度は強調される。
    page.locator("select.mc-default[data-provider='openai'][data-usage='chat']").select_option("gpt-5.4-mini")
    expect(chat_cell).to_have_class("mc-changed")


def test_admin_model_catalog_edit_allowed_list_via_modal(page, web_base_url):
    """「一覧を編集」→ モーダルへ複数行入力 → 反映 → 保存で PUT body に載る。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    page.locator("button.mc-edit[data-provider='ollama'][data-usage='chat']").click()
    overlay = page.locator("#mc-overlay")
    expect(overlay).to_have_class("overlay open")
    expect(page.locator("#mc-modal-title")).to_contain_text("チャット")

    ta = page.locator("#mc-modal-textarea")
    ta.fill("qwen2.5\nllama3.1")
    page.locator("#mc-modal-save").click()
    expect(overlay).not_to_have_class("overlay open")

    sel = page.locator("select.mc-default[data-provider='ollama'][data-usage='chat']")
    expect(sel.locator("option")).to_have_count(2)

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert sorted(put["model_catalog"]["ollama"]["chat"]["allowed"]) == ["llama3.1", "qwen2.5"]


def test_admin_model_catalog_reorder_only_is_sent_and_order_preserved(page, web_base_url):
    """`allowed` の並び順は契約（描画・API とも保持する）。候補の**並べ替えだけ**（値の集合・既定は
    不変）でも差分として送信され、保存後も表示・PUT body ともに新しい順序が保持される
    （組み込み既定とソート済み比較で一致してしまい変更なしと誤判定されてはいけない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    # openai/chat は未編集状態で組み込み既定（["gpt-5.5", "gpt-5.4-mini"]・既定 gpt-5.5）と一致する。
    page.locator("button.mc-edit[data-provider='openai'][data-usage='chat']").click()
    ta = page.locator("#mc-modal-textarea")
    expect(ta).to_have_value("gpt-5.5\ngpt-5.4-mini")
    ta.fill("gpt-5.4-mini\ngpt-5.5")   # 同じ2件を逆順に並べ替えるだけ（既定 gpt-5.5 は変えない）
    page.locator("#mc-modal-save").click()

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    cell = records["admin_settings_put"][-1]["model_catalog"]["openai"]["chat"]
    assert cell["allowed"] == ["gpt-5.4-mini", "gpt-5.5"]   # 新しい順序のまま（ソートされない）
    assert cell["default"] == "gpt-5.5"


def _settings_with_pinned_openai_chat_and_ollama_candidate():
    """openai/chat が組み込み既定と同値（gpt-5.5）で明示保存済み・ollama/chat に候補を1つ足した
    system_settings（両テスト共通のセットアップ）。"""
    return {
        **SYSTEM_SETTINGS_VIEW,
        "model_catalog": {
            **SYSTEM_SETTINGS_VIEW["model_catalog"],
            "configured": {"openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"],
                                               "default": "gpt-5.5"}}},
            "effective": {
                **SYSTEM_SETTINGS_VIEW["model_catalog"]["effective"],
                "ollama": {**SYSTEM_SETTINGS_VIEW["model_catalog"]["effective"]["ollama"],
                          "chat": {"allowed": ["qwen2.5", "llama3.1"], "default": "qwen2.5"}},
            },
        },
    }


def test_admin_model_catalog_pin_preserved_after_change_and_revert(page, web_base_url):
    """組み込み既定と同値のセル（openai/chat=gpt-5.5）を明示保存済みの場合、別候補へ変更してから
    元の値（pin）へ戻す操作をしても、明示固定は失われない（別セルの編集・保存と組み合わせても
    PUT body に残る）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, system_settings=_settings_with_pinned_openai_chat_and_ollama_candidate())
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    sel = page.locator("select.mc-default[data-provider='openai'][data-usage='chat']")
    sel.select_option("gpt-5.4-mini")   # 別候補へ変更
    sel.select_option("gpt-5.5")         # 明示固定していた元の値へ戻す

    page.locator("select.mc-default[data-provider='ollama'][data-usage='chat']").select_option("llama3.1")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]["model_catalog"]
    assert put["openai"]["chat"]["default"] == "gpt-5.5"   # pin は残る
    assert put["ollama"]["chat"]["default"] == "llama3.1"   # 実際に変えたセルも含まれる


def test_admin_model_catalog_pin_preserved_after_modal_reflect_without_change(page, web_base_url):
    """一覧編集モーダルを開いて内容を変えずに『反映』しても、既存の明示固定（pin）は失われない
    （モーダル保存は無条件でセルを「編集済み」扱いにするため、値が変わっていない場合の扱いを
    別途確認する）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, system_settings=_settings_with_pinned_openai_chat_and_ollama_candidate())
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    page.locator("button.mc-edit[data-provider='openai'][data-usage='chat']").click()
    page.locator("#mc-modal-save").click()   # 内容は変更せずそのまま反映

    page.locator("select.mc-default[data-provider='ollama'][data-usage='chat']").select_option("llama3.1")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]["model_catalog"]
    assert put["openai"]["chat"]["default"] == "gpt-5.5"   # pin は残る
    assert put["ollama"]["chat"]["default"] == "llama3.1"


def test_admin_model_catalog_untouched_not_sent(page, web_base_url):
    """使えるモデルの表を一切触らずに保存すると model_catalog は PUT body に含まれない
    （他カード（arms 等）と同じピン留め回避のダーティフラグ流儀）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "model_catalog" not in records["admin_settings_put"][-1]


def test_admin_ollama_allowlist_textarea_saves(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    # 許可ホスト一覧は「プロバイダ＋接続先」タブ（既定で表示中）の「詳細」の中にある。
    open_advanced(page, "tabpanel-provider")

    page.locator("#cloud-ollama-allowlist").fill("10.0.0.5:11434\n10.0.0.6:11434")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert sorted(put["ollama_allowlist"]) == ["10.0.0.5:11434", "10.0.0.6:11434"]


# ===== 個人設定（settings.html） =====
# openai_model/gemini_model/ollama_model の自由選択は個人設定には無く、管理者の「使えるモデル」
# タブ（上のカタログ編集テスト群）だけで管理する。個人設定に残る Ollama の接続先選択
# （許可ホスト一覧から選ぶ・#ourl）だけが対象として残る。

def test_settings_ollama_url_select_from_allowed_hosts_and_saves(page, web_base_url):
    """許可ホスト一覧（完全 URL）から選ぶ。選択肢の値は scheme 込みの完全 URL のまま保存される
    （host:port へ丸めて再構築しない＝HTTPS が HTTP に化けない）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "ollama_url": "http://localhost:11434",
               "ollama_url_choice": {"allowed": ["http://localhost:11434", "https://ollama.lan:8443"],
                                     "default": "http://localhost:11434"}}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    ourl = page.locator("#ourl")
    values = ourl.locator("option").evaluate_all("els => els.map(e => e.value)")
    assert values == ["", "http://localhost:11434", "https://ollama.lan:8443"]   # 先頭は「管理者の既定を使う」
    expect(ourl).to_have_value("http://localhost:11434")
    expect(page.locator("#ourl-warn")).to_be_hidden()

    ourl.select_option("https://ollama.lan:8443")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["ollama_url"] == "https://ollama.lan:8443"


def test_settings_ollama_url_select_clear_to_admin_default_sends_empty_string(page, web_base_url):
    """「管理者の既定を使う」を選んで保存すると、一覧外の既存値を明示的にクリアできる
    （空文字を送る＝null に変換すると PUT が「未指定」と解釈してしまい既存値が残ってしまう不具合の
    往復確認）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "ollama_url": "http://legacy-unlisted:11434",
               "ollama_url_choice": {"allowed": ["http://localhost:11434"], "default": "http://localhost:11434"}}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    ourl = page.locator("#ourl")
    expect(ourl).to_have_value("http://legacy-unlisted:11434")
    expect(page.locator("#ourl-warn")).to_be_visible()

    ourl.select_option("")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["ollama_url"] == ""
