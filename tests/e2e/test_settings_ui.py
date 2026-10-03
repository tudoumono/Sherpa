from __future__ import annotations

import json
from urllib.parse import urlparse

import mock_api
from mock_api import install_api_mocks


def test_settings_save_and_connection_test_do_not_echo_saved_keys(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#agent")).to_have_value("simple")   # 3構成: 既定の構成id
    expect(page.locator("#okey")).to_have_value("")
    expect(page.locator("#okey")).to_have_attribute("placeholder", "設定済み（変更する時だけ入力）")

    # 実行構成は実際に選び直した時だけ送る（触っていなければ送らない）ため、既定の simple から
    # codex_ollama へ実際に変える（key 非echo確認とは別軸だが、選び直した値が正しく送られることも
    # 併せて固定する）。
    page.locator("#agent").select_option("codex_ollama")
    page.locator("#okey").fill("sk-test")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["agent"] == "codex"
    assert records["settings_put"][-1]["codex_model_provider"] == "ollama"
    assert records["settings_put"][-1]["openai_api_key"] == "sk-test"

    expect(page.locator("#okey")).to_have_value("")
    page.locator("[data-test='openai']").click()
    expect(page.locator("#t-openai")).to_contain_text("接続OK")
    assert records["settings_test"][-1]["provider"] == "openai"


def test_settings_agent_untouched_save_omits_agent_fields(page, web_base_url):
    """実行構成を一切触らずに保存すると agent／codex_model_provider は PUT body に含まれない
    （無関係な保存だけで実行構成が意図せず書き換わる事故を防ぐ）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    page.locator("#okey").fill("sk-untouched")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert "agent" not in put
    assert "codex_model_provider" not in put
    assert put["openai_api_key"] == "sk-untouched"
    assert "system_prompt" not in put and "search_helper" not in put


def test_settings_agent_reselect_same_value_omits_agent_fields(page, web_base_url):
    """一覧にある構成を、今と同じ値へ選び直しても差分が無い＝送らない
    （値ベースのダーティ判定・admin-settings.js と同型）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#agent")).to_have_value("simple")
    page.locator("#agent").select_option("simple")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert "agent" not in put
    assert "codex_model_provider" not in put


def test_settings_agent_out_of_list_value_preserved_and_omitted_when_untouched(page, web_base_url):
    """保存済みの agent が現在の選択肢に無い場合（env で無効化された頭脳等）、`<select>` は
    先頭候補へ差し替えず「一覧外」の値を保持し、選び直さない限り agent は送らない
    （黙って別の頭脳へ移行させない）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "agent": "heuristic", "construct_id": "heuristic",
               "codex_model_provider": ""}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#agent")).to_have_value("heuristic")
    expect(page.locator("#agent option[value='heuristic']")).to_contain_text("現在の設定（一覧外）")
    expect(page.locator("#agent-hint")).to_contain_text("選べない設定")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert "agent" not in put
    assert "codex_model_provider" not in put


def test_settings_agent_change_sends_codex_model_provider_together(page, web_base_url):
    """実行構成を Codex(Ollama) へ変えると、agent と codex_model_provider が同じ保存で揃って送られる。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert put["agent"] == "codex"
    assert put["codex_model_provider"] == "ollama"


def test_recompute_construct_id_rejects_falsy_non_string_codex_model_provider():
    """`mock_api._recompute_construct_id` は `codex_model_provider` が文字列以外の非 None 値
    （`False`/`0`/`{}`/`[]` 等）のとき、`str(x or "")` の truthiness 判定で「未設定」に丸めて
    codex_openai へ通さず、実サーバの `construct_id()` と同じ "codex_invalid" を返す
    （PUT 側の型/allowlist 検証をすり抜けた壊れた既存データを想定した縮退・ブラウザ不要の
    直接呼び出しで固定する）。"""
    for bad in (False, 0, {}, []):
        resp = {"agent": "codex", "codex_model_provider": bad, "constructs_available": []}
        assert mock_api._recompute_construct_id(resp) == "codex_invalid", bad
    # 正当な「未設定」表現（None/空文字）は既定 openai のまま。
    for ok in (None, ""):
        resp = {"agent": "codex", "codex_model_provider": ok, "constructs_available": []}
        assert mock_api._recompute_construct_id(resp) == "codex_openai", ok


def test_settings_put_rejects_falsy_non_string_codex_model_provider(page, web_base_url):
    """PUT `/settings` の `codex_model_provider` 検証は `False`/`0`/`{}`/`[]` を
    `if _new_codex_provider` という truthiness 判定だけで見ると allowlist チェックをすり抜けて
    しまう——実サーバは Pydantic フィールド `str | None` の型検証でこれらを 422 で弾く
    （このモックが同じ結果になることを固定する）。"""
    install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")
    for bad in (False, 0, {}, []):
        status = page.evaluate(
            """(body) => fetch('/settings', {method: 'PUT',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify(body)}).then((r) => r.status)""",
            {"codex_model_provider": bad},
        )
        assert status == 422, f"{bad!r} が拒否されなかった"


def test_settings_agent_survives_reload_failure_and_resends_on_revert(page, web_base_url):
    """PUT成功→直後の自動 load()（GET）失敗、のあとに元の値へ選び直して保存すると、
    agent は再送される（基準値は PUT 成功の時点で送信済みの値へ進んでいる＝GET の成否に依存しない）。
    この前進処理を削除すると、基準値が初期値のまま残り「元の値へ戻しただけ」と誤判定されて
    2回目の保存で agent が送られなくなる。"""
    import json

    from playwright.sync_api import expect

    records = install_api_mocks(page)

    get_count = {"n": 0}

    def fail_second_settings_get(route):
        if route.request.method != "GET":
            route.fallback()
            return
        get_count["n"] += 1
        if get_count["n"] == 2:   # 1回目=初期 load()・2回目=1回目保存後の自動 load() だけ失敗させる
            route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
            return
        route.fallback()

    page.route("**/settings", fail_second_settings_get)
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("再読込に失敗しました")
    assert records["settings_put"][-1]["agent"] == "codex"

    page.locator("#agent").select_option("simple")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["agent"] == "simple"


def test_settings_agent_put_4xx_keeps_old_baseline_and_omits_fields_after_revert(page, web_base_url):
    """PUT が 4xx（明確な拒否＝未適用）で失敗した場合、基準値は書き換わらない。元の値へ選び直して
    保存すると、agent・codex_model_provider の両方が省略される（基準値を誤って不明化・前進させる
    実装だと、選択を変えずに再送してしまう現状の検査では見抜けず、ここで初めて検知できる）。"""
    import json

    from playwright.sync_api import expect

    records = install_api_mocks(page)

    put_count = {"n": 0}

    def fail_first_settings_put(route):
        if route.request.method != "PUT":
            route.fallback()
            return
        put_count["n"] += 1
        if put_count["n"] == 1:
            route.fulfill(status=422, content_type="application/json", body=json.dumps({"detail": "invalid"}))
            return
        route.fallback()

    page.route("**/settings", fail_first_settings_put)
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("invalid")
    assert records["settings_put"] == []   # モック側の記録はフォールバック経路でしか積まれない

    # 元の値（simple）へ戻して保存 — 拒否された変更はサーバに適用されていない＝基準値は
    # 最初から動いていないはず。値が基準値と一致するので agent／codex_model_provider は送らない。
    page.locator("#agent").select_option("simple")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert "agent" not in put
    assert "codex_model_provider" not in put


def test_settings_agent_put_5xx_resends_on_retry_then_omits_after_success(page, web_base_url):
    """PUT が 5xx で失敗すると、応答からサーバ側で実際にコミットされたかどうか分からない。選択を
    変えずに保存し直すだけでも agent・codex_model_provider の両方が送られる（基準値が古いままだと
    「選択は変わっていない＝差分なし」と誤判定されて省略されてしまう）。その保存が成功すれば
    基準値は具体値へ戻り、以後の保存では再び省略される。"""
    import json

    from playwright.sync_api import expect

    records = install_api_mocks(page)

    put_count = {"n": 0}

    def fail_first_settings_put(route):
        if route.request.method != "PUT":
            route.fallback()
            return
        put_count["n"] += 1
        if put_count["n"] == 1:
            route.fulfill(status=500, content_type="application/json", body=json.dumps({"detail": "boom"}))
            return
        route.fallback()

    page.route("**/settings", fail_first_settings_put)
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()
    expect(page.locator("#msg .danger")).to_be_visible()

    # 選択を変えずに保存し直す（リトライ）。基準値が不明化されているため、選択が変わっていなくても
    # 両方のフィールドが送られる。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert put["agent"] == "codex"
    assert put["codex_model_provider"] == "ollama"

    # 直前の保存が成功した＝基準値は具体値（ollama）へ進んでいる。選択を変えずにもう一度保存すると
    # 今度は省略される。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put2 = records["settings_put"][-1]
    assert "agent" not in put2
    assert "codex_model_provider" not in put2


def test_settings_agent_put_network_error_resends_on_retry_then_omits_after_success(page, web_base_url):
    """PUT が通信例外（応答が届かない）で失敗した場合も 5xx と同様、選択を変えずに保存し直すと
    agent・codex_model_provider の両方が送られ、その保存が成功すれば以後は再び省略される。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)

    put_count = {"n": 0}

    def abort_first_settings_put(route):
        if route.request.method != "PUT":
            route.fallback()
            return
        put_count["n"] += 1
        if put_count["n"] == 1:
            route.abort()
            return
        route.fallback()

    page.route("**/settings", abort_first_settings_put)
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")

    page.locator("#agent").select_option("codex_ollama")
    page.locator("#save").click()
    expect(page.locator("#msg .danger")).to_be_visible()

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert put["agent"] == "codex"
    assert put["codex_model_provider"] == "ollama"

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put2 = records["settings_put"][-1]
    assert "agent" not in put2
    assert "codex_model_provider" not in put2


def test_settings_save_bar_stays_visible_when_scrolled(page, web_base_url):
    """S3: sticky 保存バー（実ユーザー再報告「保存が遠い」）が、ページを下までスクロールしても
    追加スクロールなしでクリックできる位置に留まること（position:fixed の実効性を実機で確認）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    save = page.locator("#save")
    page.mouse.wheel(0, 100000)   # ページ最下部までスクロール
    expect(save).to_be_in_viewport()   # 追加スクロールなしで見えている＝fixed が効いている
    save.click()
    expect(page.locator("#msg")).to_contain_text("保存しました")


def test_settings_ctrl_s_saves(page, web_base_url):
    """S3: Ctrl+S（Cmd+S）でも保存できるショートカット。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    page.locator("#okey").fill("sk-ctrl-s")
    page.keyboard.press("Control+s")

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["openai_api_key"] == "sk-ctrl-s"


def test_settings_codex_agent_saves(page, web_base_url):
    """Codex のモデル・思考の深さは管理者の既定に一本化されており個人上書きは無いため、
    このページからは実行構成（頭脳）の選択だけを保存する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    page.locator("#agent").select_option("codex_openai")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["agent"] == "codex"


def test_settings_codex_connection_test_sends_unsaved_openai_key(page, web_base_url):
    """Codex＋Azure/OpenAI互換接続の接続テストは、保存前（入力中のみ）の「OpenAI」欄のキーも使う
    （他の頭脳の接続テストと同じ扱い＝未保存キーのままでも Azure 等への疎通を確かめられる）。
    モデルは管理者のカタログ既定に従う（このページからは送らない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    page.locator("#agent").select_option("codex_openai")
    page.locator("#okey").fill("sk-unsaved-azure-key")

    page.locator("[data-test='codex']").click()
    expect(page.locator("#t-codex")).to_contain_text("接続OK")
    assert records["settings_test"][-1] == {
        "provider": "codex", "openai_api_key": "sk-unsaved-azure-key"}


# ===== 保存後の再読込 =====

def test_settings_save_waits_for_reload_before_showing_success(page, web_base_url):
    """L5（LOW・2026-07-16 Codex RV 5巡目再検証）: Playwright の `expect()` auto-wait は、`load()` を
    fire-and-forget のまま呼んでいた旧実装でも最終的には成功メッセージが出るため、R4-3
    （`save()` 内で `await load()` する是正）の効果を「保存しました」の文字列出現だけでは区別
    できない false green になりうる（Codex RV 指摘）。ここでは保存後の2回目の `GET /settings`
    （`save()` 内の自動 `load()` が発行するもの）をテスト側で意図的に保留し、解放**前**に
    「成功メッセージがまだ出ていない・保存ボタンが無効のまま」であることを明示的に確認してから
    解放する（`tests/e2e/test_chat_ui.py` の保留 route パターンを踏襲）。解放後に成功メッセージが出て、
    保存ボタンが再び有効になる。
    """
    import json

    from playwright.sync_api import expect

    records = install_api_mocks(page)

    held: dict = {}
    get_count = {"n": 0}

    def hold_second_settings_get(route):
        if route.request.method != "GET":
            route.fallback()
            return
        get_count["n"] += 1
        if get_count["n"] == 1:
            route.fallback()   # 初回 GET（ページ初期化時の load()）は通常どおり応答させる
            return
        held["route"] = route   # 2回目（保存後の自動 load()）だけ保留する

    # goto より前に登録する（goto は 'load' イベントまでしか待たず、初期化スクリプトの
    # fetch('/settings') 自体の発火/完了とは非同期にずれうるため、初回 GET から一貫して
    # このハンドラを経由させ、カウンタで「何回目か」を確実に判別できるようにする）。
    page.route("**/settings", hold_second_settings_get)
    page.goto(f"{web_base_url}/settings.html")
    expect(page.locator("#agent")).to_have_value("simple")   # 3構成: 既定の構成id   # 初回 load() の完了を待つ

    page.locator("#okey").fill("sk-held-get")
    page.locator("#save").click()

    # PUT 自体は完了する（records に積まれる）はずだが、保留中の GET /settings のせいで load() が
    # 完了しない＝成功メッセージはまだ出ておらず、保存ボタンも無効のまま。
    expect(page.locator("#save")).to_be_disabled()
    assert len(records["settings_put"]) == 1, "PUT 自体は保留の影響を受けず完了しているはず"
    expect(page.locator("#msg")).not_to_contain_text("保存しました")

    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps(mock_api.SETTINGS_RESP, ensure_ascii=False))

    expect(page.locator("#msg")).to_contain_text("保存しました")
    expect(page.locator("#save")).not_to_be_disabled()


# ===== 個人 API キー欄の表示/非表示 =====

def test_personal_keys_disabled_hides_key_inputs_and_shows_note(page, web_base_url):
    """`personal_api_keys_allowed=false`（管理者が個人キーを許可していない・既定）のとき、
    キー入力欄は隠れ、「キーは管理者が設定します」の注記が出る。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "personal_api_keys_allowed": False}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#okey-row")).to_be_hidden()
    expect(page.locator("#okey-disabled-note")).to_be_visible()


def test_personal_keys_allowed_shows_key_inputs(page, web_base_url):
    """`personal_api_keys_allowed=true` のときは、これまでどおりキー入力欄が見える（注記は出さない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=mock_api.SETTINGS_RESP)   # 既定モックは personal_api_keys_allowed=True
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#okey-row")).to_be_visible()
    expect(page.locator("#okey-disabled-note")).to_be_hidden()


# ===== 外部連携（自分の API キー）=====

def test_ext_keys_card_hidden_when_disabled(page, web_base_url):
    """既定（user_api_keys_allowed=false・A6 と同型の既定 OFF）ではカード自体が出ない。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=mock_api.SETTINGS_RESP)   # 既定モックは user_api_keys_allowed=False
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#ext-keys-card")).to_be_hidden()


def test_ext_keys_card_shown_and_issue_reveals_key_once(page, web_base_url):
    """許可時はカードが出て、発行→プレーンキーの1回表示ができる。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#ext-keys-card")).to_be_visible()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("私の連携キー")
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    assert records["ext_key_self_create"][-1]["label"] == "私の連携キー"

    page.locator("#ek-modal-close").click()
    expect(page.locator("#ext-keys-list")).to_contain_text("私の連携キー")
    expect(page.locator("#ek-reveal-key")).to_have_text("")


def test_ext_keys_card_modal_cannot_close_while_issuing(page, web_base_url):
    """発行の応答待ちの間は閉じられない（管理画面と同じ状態機械）。"""
    from playwright.sync_api import expect
    import json as _json

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    # POST だけを保留にする（GET は初期表示の一覧取得で先に飛ぶため、メソッドで区別しないと
    # 先に消費されてしまう＝POST 以外は次のハンドラへ `route.fallback()` で委譲する）。
    pending = {}

    def hold(route):
        if route.request.method != "POST":
            route.fallback()
            return
        pending["route"] = route
    page.route("**/ext/v1/keys", hold)

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("保留中キー")
    page.locator("#ek-modal-submit").click()
    # submit ボタンは応答待ちの間 disabled になる（await の直前に同期的に立てるフラグ）ため、
    # これで「issuing」状態に入ったことを待つ（Playwright の自動リトライに乗る）。
    expect(page.locator("#ek-modal-submit")).to_be_disabled()

    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")

    pending["route"].fulfill(status=200, content_type="application/json", body=_json.dumps(
        {"ok": True, "id": 42, "key": "sk-ext-heldresp2", "key_prefix": "sk-ext-hel",
         "label": "保留中キー", "created_at": "2026-08-25T00:00:00+00:00",
         "allowed_worlds": None, "expires_at": None, "daily_quota": None}))
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-heldresp2")
    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).not_to_have_class("overlay open")
    expect(page.locator("#ek-reveal-key")).to_have_text("")


def test_ext_keys_card_revoke_own_key(page, web_base_url):
    """一覧から自分のキーを失効できる（確認ダイアログ経由）。"""
    from playwright.sync_api import expect
    import json as _json

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}

    def handler(route):
        if route.request.method == "GET" and route.request.url.endswith("/ext/v1/keys"):
            route.fulfill(status=200, content_type="application/json", body=_json.dumps({"keys": [
                {"id": 3, "key_prefix": "sk-ext-mine", "label": "自分のキー", "created_by": "u1",
                 "revoked_by": None, "allowed_worlds": None, "daily_quota": None, "owner_uid": "u1",
                 "created_at": "2026-08-01T00:00:00+00:00", "revoked_at": None, "last_used_at": None,
                 "expires_at": None, "call_count": 1},
            ]}))
            return
        route.continue_()

    records = install_api_mocks(page, settings=settings)
    page.route("**/ext/v1/keys", handler)
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#ext-keys-list")).to_contain_text("自分のキー")
    page.locator("[data-ek-revoke='3']").click()

    assert records["ext_key_self_revoke"] == [3]


def test_ext_keys_card_concurrent_close_reissue_survives_slow_list_refresh(page, web_base_url):
    """発行成功直後の一覧再取得が遅延している間に、次の発行が先に完了してより新しい一覧
    （両方のキーを含む）を描画した場合、後から届いた遅い（古い世代の）一覧応答が新しい描画を
    上書きしない（一覧 GET の世代番号ガード・管理画面と同型）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    records = install_api_mocks(page, settings=settings)
    get_calls = {"n": 0}
    held = {}

    def handler(route):
        if route.request.method == "GET":
            get_calls["n"] += 1
            if get_calls["n"] == 2:
                held["route"] = route   # 1本目発行成功直後の一覧取得だけ保留する
                return
        route.fallback()
    page.route("**/ext/v1/keys", handler)
    page.goto(f"{web_base_url}/settings.html")

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("1本目")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    page.locator("#ek-modal-close").click()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("2本目")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    expect(page.locator("#ext-keys-list")).to_contain_text("2本目")
    expect(page.locator("#ext-keys-list")).to_contain_text("1本目")

    assert "route" in held
    held["route"].fulfill(status=200, content_type="application/json", body=json.dumps({"keys": [
        {"id": 1, "key_prefix": "sk-ext-mock0001", "label": "1本目", "created_by": "u1",
         "revoked_by": None, "allowed_worlds": None, "daily_quota": None, "owner_uid": "u1",
         "created_at": "2026-08-25T00:00:00+00:00", "revoked_at": None, "last_used_at": None,
         "expires_at": None, "call_count": 0},
    ]}))
    page.wait_for_timeout(50)
    expect(page.locator("#ext-keys-list")).to_contain_text("2本目")
    expect(page.locator("#ext-keys-list")).to_contain_text("1本目")
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    expect(page.locator("#ek-modal-submit")).to_be_hidden()
    expect(page.locator("#ek-issue-err")).to_have_text("")
    assert len(records["ext_key_self_create"]) == 2


def test_ext_keys_card_issue_timeout_recovers_and_auto_revokes_orphan_key(page, web_base_url):
    """発行 POST が30秒応答しない場合、回復専用エンドポイント（`POST /ext/v1/keys/recover`）へ
    `client_op_id` を渡して照合し、`found: true` を確認できたら再発行を促す（issuing の永久
    ロックを解消する・一覧取得→DELETE の2段構成は使わない）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    captured = {}

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/keys"):
            captured["body"] = json.loads(route.request.post_data)
            return
        if route.request.method == "POST" and url.endswith("/ext/v1/keys/recover"):
            req = json.loads(route.request.post_data)
            if req.get("client_op_id") == captured.get("body", {}).get("client_op_id"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps(
                    {"found": True, "id": 66, "revoked_at": "2026-08-25T00:00:00+00:00"}))
                return
        route.fallback()

    install_api_mocks(page, settings=settings)
    page.route("**/ext/v1/keys", handler)
    page.route("**/ext/v1/keys/recover", handler)
    page.goto(f"{web_base_url}/settings.html")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("孤児化候補")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-modal-submit")).to_be_disabled()

    page.clock.fast_forward(31000)
    expect(page.locator("#ek-issue-err")).to_contain_text("失効しました")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()
    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).not_to_have_class("overlay open")


def test_ext_keys_card_issue_timeout_then_no_match_shows_failure_not_success(page, web_base_url):
    """回復専用エンドポイントが `found: false` を返し続けた場合は失敗として表示する（成功
    文言を出さない・管理画面と同型）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/keys"):
            return
        if route.request.method == "POST" and url.endswith("/ext/v1/keys/recover"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"found": False, "id": None, "revoked_at": None}))
            return
        route.fallback()

    install_api_mocks(page, settings=settings)
    page.route("**/ext/v1/keys", handler)
    page.route("**/ext/v1/keys/recover", handler)
    page.goto(f"{web_base_url}/settings.html")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("届いていない候補")
    page.locator("#ek-modal-submit").click()
    page.clock.fast_forward(31000)
    page.clock.fast_forward(2000)
    page.clock.fast_forward(2000)

    expect(page.locator("#ek-issue-err")).to_contain_text("失敗した可能性")
    expect(page.locator("#ek-issue-err")).not_to_contain_text("失効しました")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_ext_keys_card_modal_inert_blocks_background_keyboard_interaction(page, web_base_url):
    """モーダルが開いている間、背後（`.wrap`）は `inert` になりキーボード（Tab）操作でも
    背後へフォーカスが移らない。`aria-modal="true"` も宣言されている（管理画面と同型）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    dialog = page.locator("#ek-overlay .modal[role='dialog']")
    expect(dialog).to_have_attribute("aria-modal", "true")

    page.locator("#ext-key-issue-open").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")
    expect(page.locator(".wrap")).to_have_attribute("inert", "")

    for _ in range(15):
        page.keyboard.press("Tab")
    focused_in_modal = page.evaluate(
        "document.activeElement && !!document.activeElement.closest('#ek-overlay')")
    assert focused_in_modal, "Tab移動でフォーカスがモーダルの外（inert な背後）へ出た"

    page.locator("#ek-modal-close").click()
    expect(page.locator(".wrap")).not_to_have_attribute("inert", "")


def test_ext_keys_card_recover_malformed_found_type_retries_then_fails(page, web_base_url):
    """回復応答の `found` が true/false のどちらでもない（型崩れ）場合は不正応答として扱い、
    有界リトライの末に最終的な失敗表示になる（管理画面と同型）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/keys"):
            return
        if route.request.method == "POST" and url.endswith("/ext/v1/keys/recover"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"found": "yes"}))
            return
        route.fallback()

    install_api_mocks(page, settings=settings)
    page.route("**/ext/v1/keys", handler)
    page.route("**/ext/v1/keys/recover", handler)
    page.goto(f"{web_base_url}/settings.html")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("型崩れ候補")
    page.locator("#ek-modal-submit").click()
    page.clock.fast_forward(31000)
    page.clock.fast_forward(2000)
    page.clock.fast_forward(2000)

    expect(page.locator("#ek-issue-err")).to_contain_text("確認できませんでした")
    expect(page.locator("#ek-issue-err")).not_to_contain_text("失効しました")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_settings_ctrl_s_during_key_modal_does_not_save(page, web_base_url):
    """API キー発行モーダルが開いている間は Ctrl/Cmd+S を押しても `PUT /settings` は発生しない
    （管理画面と同型）。モーダルを閉じれば通常どおり保存できる。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    page.locator("#ext-key-issue-open").click()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["settings_put"]) == 0

    pending = {}

    def hold(route):
        if route.request.method != "POST":
            route.fallback()
            return
        pending["route"] = route
    page.route("**/ext/v1/keys", hold)
    page.locator("#ek-label").fill("ctrls候補")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["settings_put"]) == 0

    pending["route"].fulfill(status=200, content_type="application/json", body=json.dumps(
        {"ok": True, "id": 1, "key": "sk-ext-mockctrls", "key_prefix": "sk-ext-mockct",
         "label": "ctrls候補", "created_at": "2026-08-25T00:00:00+00:00",
         "allowed_worlds": None, "expires_at": None, "daily_quota": None}))
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mockctrls")
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["settings_put"]) == 0

    page.locator("#ek-modal-close").click()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["settings_put"]) == 1


def test_ext_keys_card_focus_moves_to_copy_on_success_and_back_to_opener_on_close(page, web_base_url):
    """発行成功時はコピー操作へフォーカスが移り、モーダルを閉じると開く前にフォーカスがあった
    要素（発行ボタン）へ復帰する（`to_be_focused` で確認・管理画面と同型）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    open_btn = page.locator("#ext-key-issue-open")
    open_btn.click()
    page.locator("#ek-label").fill("フォーカステスト")
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-copy")).to_be_focused()

    page.locator("#ek-modal-close").click()
    expect(open_btn).to_be_focused()


def test_settings_ctrl_s_during_key_modal_is_cancelable_and_default_prevented(page, web_base_url):
    """モーダルが開いている間の Ctrl+S は実際に `preventDefault()` されている
    （cancelable な KeyboardEvent の `defaultPrevented` を確認・管理画面と同型）。"""
    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")
    page.locator("#ext-key-issue-open").click()

    default_prevented = page.evaluate("""() => {
      const ev = new KeyboardEvent('keydown', {
        key: 's', ctrlKey: true, cancelable: true, bubbles: true });
      document.dispatchEvent(ev);
      return ev.defaultPrevented;
    }""")
    assert default_prevented is True


def test_ext_keys_card_expires_date_is_inclusive_and_min_blocks_past(page, web_base_url):
    """発行フォームの日付は選択日を含めて有効（翌日0時=JSTに失効）へ変換して送信する。
    `min` 属性は当日日付。手入力で過去日を直接セットした場合（`min` が効かない経路）も、
    送信前のクライアント側チェックが POST 自体を発生させない（管理画面と同型）。日付は
    実行日からの相対計算で導出する（固定日は将来過去日になり試験の前提が崩れる）。"""
    from datetime import datetime, timedelta, timezone

    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    future = today + timedelta(days=7)
    past = today - timedelta(days=1)

    page.locator("#ext-key-issue-open").click()
    min_attr = page.locator("#ek-expires").get_attribute("min")
    assert min_attr == today.isoformat()

    page.locator("#ek-label").fill("期限つきキー")
    page.locator("#ek-expires").fill(past.isoformat())
    before_creates = len(records["ext_key_self_create"])
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-issue-err")).to_contain_text("今日以降")
    assert len(records["ext_key_self_create"]) == before_creates

    page.locator("#ek-expires").fill(future.isoformat())
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    sent = records["ext_key_self_create"][-1]
    assert sent["expires_at"] == f"{future.isoformat()}T15:00:00.000Z"
# ===== SET-2c: 接続先の読み取り専用表示（管理画面「接続先」欄・DB `system_settings` が唯一の真実源） =====

def test_openai_endpoint_note_hidden_when_connected_to_openai(page, web_base_url):
    """既定（OpenAI 本家）では注記を出さない（DB 未設定＝`sherpa/llm.py` の fail-safe 既定）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=mock_api.SETTINGS_RESP)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#openai-endpoint-note")).to_be_hidden()


def test_openai_endpoint_note_shows_azure_host_read_only(page, web_base_url):
    """管理画面で接続先を Azure OpenAI へ切り替えた状態（DB 値）を GET /settings が返すと、
    利用者側には読み取り専用の注記（接続先の種類とホスト名のみ・パス/キーは出さない）が出る。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "openai_endpoint_kind": "azure",
               "openai_base_url_host": "myres.openai.azure.com"}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    note = page.locator("#openai-endpoint-note")
    expect(note).to_be_visible()
    expect(note).to_contain_text("Azure OpenAI")
    expect(note).to_contain_text("myres.openai.azure.com")
    expect(note).to_contain_text("デプロイ名")
