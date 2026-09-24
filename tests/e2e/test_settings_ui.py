from __future__ import annotations

import json
from urllib.parse import urlparse

import mock_api
from mock_api import install_api_mocks


def test_settings_save_and_connection_test_do_not_echo_saved_keys(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#agent")).to_have_value("openai_only")   # 4構成: 既定の構成id
    expect(page.locator("#okey")).to_have_value("")
    expect(page.locator("#okey")).to_have_attribute("placeholder", "設定済み（変更する時だけ入力）")

    # 実行構成は実際に選び直した時だけ送る（触っていなければ送らない）ため、既定の openai_only から
    # ollama_only へ実際に変える（key 非echo確認とは別軸だが、選び直した値が正しく送られることも
    # 併せて固定する）。
    page.locator("#agent").select_option("ollama_only")
    page.locator("#okey").fill("sk-test")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["agent"] == "ollama"
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

    page.locator("#sysprompt").fill("回答は簡潔に。")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert "agent" not in put
    assert "codex_model_provider" not in put
    assert put["system_prompt"] == "回答は簡潔に。"


def test_settings_agent_reselect_same_value_omits_agent_fields(page, web_base_url):
    """一覧にある構成を、今と同じ値へ選び直しても差分が無い＝送らない
    （値ベースのダーティ判定・admin-settings.js と同型）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#agent")).to_have_value("openai_only")
    page.locator("#agent").select_option("openai_only")
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
    expect(page.locator("#agent")).to_have_value("openai_only")

    page.locator("#agent").select_option("ollama_only")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("再読込に失敗しました")
    assert records["settings_put"][-1]["agent"] == "ollama"

    page.locator("#agent").select_option("openai_only")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["agent"] == "openai"


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
    expect(page.locator("#agent")).to_have_value("openai_only")

    page.locator("#agent").select_option("ollama_only")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("invalid")
    assert records["settings_put"] == []   # モック側の記録はフォールバック経路でしか積まれない

    # 元の値（openai_only）へ戻して保存 — 拒否された変更はサーバに適用されていない＝基準値は
    # 最初から動いていないはず。値が基準値と一致するので agent／codex_model_provider は送らない。
    page.locator("#agent").select_option("openai_only")
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
    expect(page.locator("#agent")).to_have_value("openai_only")

    page.locator("#agent").select_option("ollama_only")
    page.locator("#save").click()
    expect(page.locator("#msg .danger")).to_be_visible()

    # 選択を変えずに保存し直す（リトライ）。基準値が不明化されているため、選択が変わっていなくても
    # 両方のフィールドが送られる。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert put["agent"] == "ollama"
    assert put["codex_model_provider"] is None

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
    expect(page.locator("#agent")).to_have_value("openai_only")

    page.locator("#agent").select_option("ollama_only")
    page.locator("#save").click()
    expect(page.locator("#msg .danger")).to_be_visible()

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["settings_put"][-1]
    assert put["agent"] == "ollama"
    assert put["codex_model_provider"] is None

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put2 = records["settings_put"][-1]
    assert "agent" not in put2
    assert "codex_model_provider" not in put2


def test_settings_agent_a7_mismatch_rejected_with_422(page, web_base_url):
    """A7（クラウドプロバイダ排他選択）: 選択中でないクラウド系 agent の保存は 422 で拒否される
    （実サーバ `sherpa/routers/system.py::settings_put` と同じ・個人設定画面の <select> は既に
    A7 でフィルタ済みの選択肢しか出さないため、通常操作では起きない＝直接 fetch で契約を固定する）。"""
    settings = {**mock_api.SETTINGS_RESP, "cloud_provider": "gemini"}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    result = page.evaluate("""async () => {
      const r = await fetch('/settings', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({agent: 'openai'}),
      });
      return { status: r.status, body: await r.json() };
    }""")

    assert result["status"] == 422
    assert "クラウドプロバイダ" in result["body"]["detail"]
    assert records["settings_put"][-1] == {"agent": "openai"}


def test_settings_agent_disabled_bedrock_rejected_with_422_even_if_cloud_matches(page, web_base_url):
    """有効化していない頭脳（bedrock/gemini）の保存は、たまたま cloud_provider が一致していても
    422 で拒否される（実サーバ `agent_constructs.runtime_blocked` 相当・A7 の一致確認だけでは
    「そもそも使えない」ケースを見逃す）。"""
    settings = {**mock_api.SETTINGS_RESP, "cloud_provider": "bedrock"}   # bedrock は未有効のまま
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    result = page.evaluate("""async () => {
      const r = await fetch('/settings', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({agent: 'bedrock'}),
      });
      return { status: r.status, body: await r.json() };
    }""")

    assert result["status"] == 422
    assert "有効化していません" in result["body"]["detail"]
    assert records["settings_put"][-1] == {"agent": "bedrock"}


def test_settings_agent_a7_check_uses_provider_synced_from_admin_update(page, web_base_url):
    """個人設定（PUT /settings）の A7 判定は、管理画面（PUT /admin/settings）でクラウド
    プロバイダを切り替えた直後の値を参照する（同期していないと、古い cloud_provider を見て
    実サーバでは通るはずの保存を誤って拒否し続ける／その逆になる）。"""
    settings = {**mock_api.SETTINGS_RESP, "cloud_provider": "openai"}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    # 切替前: openai は選択中のクラウドと一致するので通る。
    before = page.evaluate("""async () => {
      const r = await fetch('/settings', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({agent: 'openai'}),
      });
      return r.status;
    }""")
    assert before == 200

    # 管理画面でクラウドプロバイダを gemini へ切り替える。
    admin_result = page.evaluate("""async () => {
      const r = await fetch('/admin/settings', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({cloud_provider: 'gemini'}),
      });
      return r.status;
    }""")
    assert admin_result == 200

    # 切替後: 個人設定の A7 判定も追従し、openai の保存は拒否される（同期が無いと 200 のまま残る）。
    after = page.evaluate("""async () => {
      const r = await fetch('/settings', {
        method: 'PUT', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({agent: 'openai'}),
      });
      return { status: r.status, body: await r.json() };
    }""")
    assert after["status"] == 422
    assert "クラウドプロバイダ" in after["body"]["detail"]
    assert len(records["settings_put"]) == 2
    assert len(records["admin_settings_put"]) == 1


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

    page.locator("#sysprompt").fill("Ctrl+S 保存の確認")
    page.keyboard.press("Control+s")

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["system_prompt"] == "Ctrl+S 保存の確認"


def test_settings_bedrock_fetch_models_replaces_options_and_saves(page, web_base_url):
    """S6: 「利用可能なモデルを取得」→ <select> の選択肢が取得結果に置き換わる → 選んで保存、の流れ。
    ユーザー指名の Sonnet 4.6 は ID をハードコードせず、動的取得の結果に出てくれば選べることを示す。

    RV MED（F5・2026-07-16再検証→N4・3巡目再検証）: 取得結果には静的 choices の1つ（jp Haiku）が
    含まれるが、もう一方（global Haiku）は含まれない。静的 choices は列挙結果の行を作らず（N4:
    重複防止）、静的 choices 再追加ループが「列挙結果の有無に関係なく」正典ラベルで必ず1回だけ
    描画する。取得後の選択肢は「列挙のうち非静的1件（us Sonnet）＋静的 choices 2件（jp Haiku・
    global Haiku）」の3件になる（旧実装は丸ごと置換で global Haiku が消えていた＝F5 の実害）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, settings=mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS)
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    expect(sel.locator("option")).to_have_count(2)   # 初期は静的2択

    page.locator("#bmodel-fetch").click()
    expect(page.locator("#bmodel-fetch-res")).to_contain_text("2件のモデルを取得しました")
    assert len(records["bedrock_models_fetch"]) == 1

    expect(sel.locator("option")).to_have_count(3)   # 非静的1件＋静的 choices 2件（常に描画）
    expect(sel.locator("option[value='global.anthropic.claude-haiku-4-5-20251001-v1:0']")).to_have_count(1)
    expect(sel.locator("option[value='jp.anthropic.claude-haiku-4-5-20251001-v1:0']")).to_have_count(1)
    sel.select_option("us.anthropic.claude-sonnet-4-6-20260115-v1:0")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["bedrock_model"] == "us.anthropic.claude-sonnet-4-6-20260115-v1:0"


def test_settings_bedrock_static_choice_survives_fetch_and_is_sent_on_save(page, web_base_url):
    """RV MED（F5・実害再現）: 静的 Global を選択中に「利用可能なモデルを取得」を押し、その一覧に
    Global が含まれていない場合でも、Global は legacy に転落せず選択肢に残り続け、保存すると
    `bedrock_model` にちゃんと Global が送信される（旧実装は select 再構築で Global が消え、
    legacy option として復活して null 送信になっていた＝保存「成功」表示なのに実際は変わらない）。"""
    from playwright.sync_api import expect

    static_global = "global.anthropic.claude-haiku-4-5-20251001-v1:0"
    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "bedrock_model": static_global, "bedrock_model_known": True,
               "bedrock_model_label": "Claude Haiku 4.5（Global 推論プロファイル）"}
    records = install_api_mocks(page, settings=settings, bedrock_models={"models": [
        {"id": "jp.anthropic.claude-sonnet-4-6-20260115-v1:0", "label": "Claude Sonnet 4.6（JP 推論プロファイル）"},
    ], "error": None})   # Global を含まない一覧
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    expect(sel).to_have_value(static_global)

    page.locator("#bmodel-fetch").click()
    expect(page.locator("#bmodel-fetch-res")).to_contain_text("1件のモデルを取得しました")
    expect(sel.locator("option[value='" + static_global + "']")).to_have_count(1)
    expect(sel.locator("option[value='" + static_global + "']")).not_to_contain_text("旧設定")
    expect(sel).to_have_value(static_global)   # 選択も維持される

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["bedrock_model"] == static_global


def test_settings_bedrock_known_models_map_keeps_static_canonical_label(page, web_base_url):
    """L4（LOW・2026-07-16 Codex RV 5巡目再検証）: クライアント側の `knownBedrockModels`
    （known 分類キャッシュ）の静的 choices エントリは、`load()` が返した保存値のラベルでも、
    verify 応答のラベルでも上書きされない（静的の正典ラベルを保つ）。R4-4 で
    `addOrSelectBedrockModelOption`/`setBedrockModelOptions` 側は対応済みだったが、`load()` 自身の
    `knownBedrockModels.set(s.bedrock_model, ...)` が保存値=静的IDの時に上書きしてしまう抜け穴が
    残っていた。"""
    from playwright.sync_api import expect

    static_id = "jp.anthropic.claude-haiku-4-5-20251001-v1:0"
    canonical_label = "Claude Haiku 4.5（JP 推論プロファイル・既定）"
    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "bedrock_model": static_id, "bedrock_model_known": True,
               "bedrock_model_label": "サーバの汎用ラベル（正典とは異なる文言）"}
    install_api_mocks(page, settings=settings, bedrock_verify={
        "ok": True, "id": static_id, "label": "verify応答の汎用ラベル（正典とは異なる文言）"})
    page.goto(f"{web_base_url}/settings.html")

    # ページ初期化時点の load() で、サーバが返した「正典とは異なる」ラベルにも関わらず、
    # knownBedrockModels の静的エントリは正典ラベルのまま（L4 のガード）。
    label = page.evaluate("knownBedrockModels.get('" + static_id + "')")
    assert label == canonical_label, label

    # verify 経由でも静的エントリは上書きされない（R4-4）。
    page.locator("#bmodel-manual").fill(static_id)
    page.locator("#bmodel-verify").click()
    expect(page.locator("#bmodel-verify-res")).to_contain_text("検証OK")
    label2 = page.evaluate("knownBedrockModels.get('" + static_id + "')")
    assert label2 == canonical_label, label2

    # option 自体の表示も正典ラベルのまま。
    opt = page.locator("#bmodel option[value='" + static_id + "']")
    expect(opt).to_have_text(canonical_label)


def test_settings_bedrock_fetch_models_failure_keeps_static_choices(page, web_base_url):
    """S6: 取得失敗（キー未設定/403等）は静的既定の選択肢のまま・理由を表示（画面を壊さない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, bedrock_models={"models": [], "error": "Bedrock の API キーが未設定です"})
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    page.locator("#bmodel-fetch").click()
    expect(page.locator("#bmodel-fetch-res")).to_contain_text("Bedrock の API キーが未設定です")
    expect(sel.locator("option")).to_have_count(2)   # 静的既定のまま


def test_settings_bedrock_verify_model_id_adds_and_selects_option(page, web_base_url):
    """バッチ2・1番（2026-07-03）: 「利用可能なモデルを取得」（一覧列挙・control-plane 権限が要る）が
    使えない構成向け。モデルIDを直接入力して「検証して追加」→ 成功したら <select> に追加＆選択状態に。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, settings=mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, bedrock_verify={
        "ok": True, "id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0",
        "label": "Claude Sonnet 4.6（JP 推論プロファイル）"})
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    expect(sel.locator("option")).to_have_count(2)

    page.locator("#bmodel-manual").fill("jp.anthropic.claude-sonnet-4-6-20260101-v1:0")
    page.locator("#bmodel-verify").click()

    expect(page.locator("#bmodel-verify-res")).to_contain_text("検証OK")
    expect(sel.locator("option")).to_have_count(3)   # 追加された
    expect(sel).to_have_value("jp.anthropic.claude-sonnet-4-6-20260101-v1:0")   # 選択状態に
    assert records["bedrock_models_verify"][-1] == {"model_id": "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"}

    # 保存は従来の PUT（追加操作自体は保存しない）。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["bedrock_model"] == "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"


def test_settings_bedrock_verify_model_id_failure_shows_reason_and_does_not_add(page, web_base_url):
    """検証失敗（形式不正/403等）は理由を表示し、<select> には追加しない。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, bedrock_verify={"ok": False, "error": "認証エラー（403）。API キー/権限を確認してください。"})
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    page.locator("#bmodel-manual").fill("jp.anthropic.claude-not-real-v1:0")
    page.locator("#bmodel-verify").click()

    expect(page.locator("#bmodel-verify-res")).to_contain_text("認証エラー（403）")
    expect(sel.locator("option")).to_have_count(2)   # 追加されていない


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


# ===== RV MED（2026-07-15）: legacy マーカーが検証済み再選択を握りつぶすフロントの実害バグ修正 =====

def test_settings_bedrock_verify_reselecting_legacy_saved_model_sends_it_on_save(page, web_base_url):
    """核心回帰: 保存済みの動的モデル（サーバが `bedrock_model_known: false`＝旧設定表示）を、同じ
    ID で verify に成功させてから保存すると、`bedrock_model` がちゃんと送信される。旧実装は既存
    option の `data-legacy` を消さずに選択するだけだったため、`selectedBedrockModel` が legacy 扱いの
    まま null を返し、保存が「成功」表示なのに実際は直前の値のまま（サーバ側は何も変わらない）だった。"""
    from playwright.sync_api import expect

    legacy_id = "us.anthropic.claude-legacy-saved-v1:0"
    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "bedrock_model": legacy_id,
               "bedrock_model_known": False, "bedrock_model_label": legacy_id}
    records = install_api_mocks(page, settings=settings, bedrock_verify={
        "ok": True, "id": legacy_id, "label": "Legacy Saved Model（検証済み）"})
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    expect(sel.locator("option")).to_have_count(3)   # 静的2択＋保存済み legacy 枠
    expect(sel).to_have_value(legacy_id)
    expect(sel.locator("option[value='" + legacy_id + "']")).to_contain_text("旧設定")

    page.locator("#bmodel-manual").fill(legacy_id)
    page.locator("#bmodel-verify").click()
    expect(page.locator("#bmodel-verify-res")).to_contain_text("検証OK")
    expect(sel.locator("option")).to_have_count(3)   # 新規追加ではなく既存枠を更新しただけ
    expect(sel).to_have_value(legacy_id)
    expect(sel.locator("option[value='" + legacy_id + "']")).not_to_contain_text("旧設定")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["bedrock_model"] == legacy_id   # legacy マーカーが外れ実送信される


def test_settings_save_waits_for_reload_before_showing_success(page, web_base_url):
    """L5（LOW・2026-07-16 Codex RV 5巡目再検証）: Playwright の `expect()` auto-wait は、`load()` を
    fire-and-forget のまま呼んでいた旧実装でも最終的には成功メッセージが出るため、R4-3
    （`save()` 内で `await load()` する是正）の効果を「保存しました」の文字列出現だけでは区別
    できない false green になりうる（Codex RV 指摘）。ここでは保存後の2回目の `GET /settings`
    （`save()` 内の自動 `load()` が発行するもの）をテスト側で意図的に保留し、解放**前**に
    「成功メッセージがまだ出ていない・保存ボタンが無効のまま」であることを明示的に確認してから
    解放する（`tests/e2e/test_chat_ui.py` の保留 route パターンを踏襲）。解放後は選択中 option が
    `option:checked` でちょうど1つ・`data-legacy` 属性が無いことを、ラベル文字列比較ではなく
    属性で確認する。
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
    expect(page.locator("#agent")).to_have_value("openai_only")   # 4構成: 既定の構成id   # 初回 load() の完了を待つ

    page.locator("#sysprompt").fill("保留GET確認用の回答方針")
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
    # ラベル文字列比較ではなく属性で確認: 選択中 option がちょうど1つ・data-legacy 属性が無い。
    checked = page.locator("#bmodel option:checked")
    expect(checked).to_have_count(1)
    assert checked.get_attribute("data-legacy") is None


def test_search_helper_toggle_saves_correctly_after_prior_reload_failure(page, web_base_url):
    """保存後の自動 `load()`（GET /settings 再読込）が失敗しても、直前の PUT 自体は正しく送信
    されており、以後の操作・保存は独立して正しく動く（「保存はできたが再読込に失敗した」表示の
    あとも2回目の保存が正しい値で送られる）。"""
    import json

    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "search_helper": ""}
    records = install_api_mocks(page, settings=settings)

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
    expect(page.locator("#search_helper")).to_have_value("")

    page.locator("#search_helper").select_option("ollama")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("再読込に失敗しました")
    assert records["settings_put"][-1]["search_helper"] == "ollama"

    page.locator("#search_helper").select_option("")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["search_helper"] == ""


def test_search_helper_invalid_value_preserved_and_not_cleared_by_unrelated_save(page, web_base_url):
    """保存済み `search_helper` が不正な値（旧データ・env 誤記等）のとき、設定画面は黙って
    ''（使わない）に見せかけず「不正な値」として保持する option を表示する。この状態のまま
    無関係な項目（system_prompt）だけを保存しても search_helper は送らない（未指定＝変更しない）
    ＝黙って解除されない（正規化不一致の是正・Bedrock モデル select の legacy option と同型）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "search_helper": "gemini"}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#search_helper")
    expect(sel).to_have_value("gemini")
    checked = sel.locator("option:checked")
    expect(checked).to_contain_text("不正な値")

    page.locator("#sysprompt").fill("テスト用の方針")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["search_helper"] is None


def test_search_helper_legacy_model_note_says_unused_not_prioritized(page, web_base_url):
    """個人設定に残る旧 `search_helper_model` の値は、実行時にはもう一切使われない（管理者の
    使えるモデル一覧の既定が適用される）。注記はこの1種類のみで、「優先されています」という
    誤った文言の分岐は無い。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP, "search_helper": "openai", "search_helper_model": "gpt-4o-mini"}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    note = page.locator("#search-helper-legacy-note")
    expect(note).to_be_visible()
    expect(note).to_contain_text("現在使われません")
    expect(note).not_to_contain_text("優先")


def test_settings_bedrock_stateful_reload_drops_unselected_fetch_options_and_keeps_reverified(page, web_base_url):
    """RV LOW（N5・2026-07-16 Codex RV 3巡目再検証）: 前回のテスト（同一 DOM で `load()` を複数回
    走らせる確認）は `GET /settings` の応答が静的固定 mock だったため、fetch で追加された「legacy
    ではない」stale な option が消えなくても検知できない false green だった（旧実装＝
    `option[data-legacy]` だけを除去する版でも通ってしまう）。ここでは `PUT /settings`／verify が
    状態を更新し `GET /settings` がそれを返す状態持ち mock（`mock_api._json`/`_post_json` を再利用・
    mock_api の既存パターンに従う）で検証する:
      (a) fetch で複数の非静的 option（X/Y/Z）を作る→1つ（X）だけ選んで保存→`save()` 内の自動
          `load()` 完了後に、選ばなかった Y/Z が消えていること（静的2択＋X の3件のみ残る）。
      (b) サーバ側の保存値が「未検証」に変わった場合に `load()` を呼び直すと legacy 表示へ正しく
          再分類され、そこから verify → 保存すると、以後の自動 `load()` でも非 legacy のまま残る
          こと。
    """
    from playwright.sync_api import expect

    # 状態持ち mock: PUT /settings と verify がこの dict/set を更新し、GET /settings が都度これを
    # 返す（mock_api.SETTINGS_RESP を土台にする＝キー集合は本物の応答形と揃える）。
    # 4構成（2026-08-15）: Bedrock を扱うため、追加AIを有効化した環境の応答を土台にする。
    state = dict(mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS)
    known_ids = {"us.anthropic.claude-x-v1:0", "us.anthropic.claude-y-v1:0", "us.anthropic.claude-z-v1:0"}
    labels = {"us.anthropic.claude-x-v1:0": "Model X", "us.anthropic.claude-y-v1:0": "Model Y",
             "us.anthropic.claude-z-v1:0": "Model Z"}

    def stateful_handler(route):
        request = route.request
        method = request.method.upper()
        path = urlparse(request.url).path
        if method == "GET" and path == "/settings":
            mock_api._json(route, dict(state))
            return
        if method == "PUT" and path == "/settings":
            body = mock_api._post_json(request)
            mid = body.get("bedrock_model")
            if mid is not None:
                state["bedrock_model"] = mid
                state["bedrock_model_known"] = mid in known_ids
                state["bedrock_model_label"] = labels.get(mid, mid)
            mock_api._json(route, dict(state))
            return
        if method == "POST" and path == "/settings/bedrock-models/verify":
            body = mock_api._post_json(request)
            mid = body.get("model_id") or ""
            known_ids.add(mid)
            labels[mid] = f"{mid}（検証済み）"
            mock_api._json(route, {"ok": True, "id": mid, "label": labels[mid]})
            return
        route.fallback()   # 対象外（他パス／他メソッド）は install_api_mocks の catch-all に委譲

    install_api_mocks(page, bedrock_models={"models": [
        {"id": "us.anthropic.claude-x-v1:0", "label": "Model X"},
        {"id": "us.anthropic.claude-y-v1:0", "label": "Model Y"},
        {"id": "us.anthropic.claude-z-v1:0", "label": "Model Z"},
    ], "error": None})
    # install_api_mocks 後に登録＝より具体的な2パターンが catch-all より先に評価される
    # （Playwright は最後に登録した route を先に評価・comment 例は mock_api.py の
    # 「page.route("**/chat/turns/running", ...) で上書きする」と同じ流儀）。
    page.route("**/settings", stateful_handler)
    page.route("**/settings/bedrock-models/verify", stateful_handler)
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    expect(sel.locator("option")).to_have_count(2)   # 初期は静的2択（既定値は静的の1つ）

    page.locator("#bmodel-fetch").click()
    expect(page.locator("#bmodel-fetch-res")).to_contain_text("3件のモデルを取得しました")
    expect(sel.locator("option")).to_have_count(5)   # 静的2＋列挙3件（X/Y/Z）

    sel.select_option("us.anthropic.claude-x-v1:0")
    page.locator("#save").click()          # save() 内で自動的に load() が走る（同一 DOM・reload 無し）
    expect(page.locator("#msg")).to_contain_text("保存しました")

    # (a) 旧実装（legacy option だけ除去）なら Y・Z が残ったまま＝5件のまま。新実装は静的2＋X の3件。
    expect(sel.locator("option")).to_have_count(3)
    expect(sel.locator("option[value='us.anthropic.claude-y-v1:0']")).to_have_count(0)
    expect(sel.locator("option[value='us.anthropic.claude-z-v1:0']")).to_have_count(0)
    expect(sel).to_have_value("us.anthropic.claude-x-v1:0")
    # RV LOW（R4-3・2026-07-16 Codex RV 4巡目再検証）: save() は load() 完了を待ってから「保存
    # しました」を表示するようになった（await load()）。「保存しました」が見えている時点で DOM の
    # 再構築も完了しているはずなので、選択中の option がちょうど1つ・非 legacy であることまで
    # 確認する（N5 の残余レース＝「表示は保存済みだが DOM 側の再構築が追いついていない」を検知）。
    x_opts = sel.locator("option[value='us.anthropic.claude-x-v1:0']")
    expect(x_opts).to_have_count(1)
    expect(x_opts).not_to_contain_text("旧設定")

    # (b) サーバ側の保存値が「未検証」に変わった場合の再分類。save() 経由だと選択中の値（X）を
    # PUT してしまい state を上書きしてしまうため、load() を直接呼んで「サーバ側の値が別セッション
    # 等で変わった」再読込を再現する（settings.js はクラシック script ＝ load はグローバル関数）。
    legacy_id = "us.anthropic.claude-legacy-v1:0"
    state["bedrock_model"] = legacy_id
    state["bedrock_model_known"] = False
    state["bedrock_model_label"] = legacy_id
    page.evaluate("load()")
    expect(sel.locator("option[value='" + legacy_id + "']")).to_contain_text("旧設定")

    page.locator("#bmodel-manual").fill(legacy_id)
    page.locator("#bmodel-verify").click()
    expect(page.locator("#bmodel-verify-res")).to_contain_text("検証OK")
    expect(sel.locator("option[value='" + legacy_id + "']")).not_to_contain_text("旧設定")

    page.locator("#save").click()          # 選択中は verify 済みの legacy_id ＝ PUT はそのまま送れる
    expect(page.locator("#msg")).to_contain_text("保存しました")
    # R4-3: ここでも「保存しました」＝ load() 完了後、選択中 option がちょうど1つ・非 legacy。
    legacy_opts = sel.locator("option[value='" + legacy_id + "']")
    expect(legacy_opts).to_have_count(1)
    expect(legacy_opts).not_to_contain_text("旧設定")
    expect(sel).to_have_value(legacy_id)


def test_settings_bedrock_known_saved_value_shows_label_not_legacy(page, web_base_url):
    """`bedrock_model_known: true` の保存値は「旧設定」表示にならず、サーバ整形済みラベルで通常
    option として表示され、再選択なしでもそのまま保存できる（known＝正当な値の証明）。"""
    from playwright.sync_api import expect

    known_id = "jp.anthropic.claude-sonnet-4-6-20260101-v1:0"
    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "bedrock_model": known_id, "bedrock_model_known": True,
               "bedrock_model_label": "Claude Sonnet 4.6（JP 推論プロファイル）"}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    expect(sel.locator("option")).to_have_count(3)
    opt = sel.locator("option[value='" + known_id + "']")
    expect(opt).to_contain_text("Claude Sonnet 4.6（JP 推論プロファイル）")
    expect(opt).not_to_contain_text("旧設定")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["bedrock_model"] == known_id   # known は legacy 扱いされない


def test_settings_bedrock_unknown_saved_value_stays_legacy_and_sends_null(page, web_base_url):
    """未検証（`bedrock_model_known: false`）の保存値は従来どおり「旧設定」表示のままになり、
    再検証せずそのまま保存すると `bedrock_model` は null で送信される（allowlist を偽装できない・
    サーバ側は現在保存中の値を保つ）。"""
    from playwright.sync_api import expect

    unknown_id = "us.anthropic.claude-unknown-v1:0"
    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "bedrock_model": unknown_id, "bedrock_model_known": False,
               "bedrock_model_label": unknown_id}
    records = install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    sel = page.locator("#bmodel")
    expect(sel.locator("option[value='" + unknown_id + "']")).to_contain_text("旧設定")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["settings_put"][-1]["bedrock_model"] is None


# ===== 個人 API キー欄の表示/非表示 =====

def test_personal_keys_disabled_hides_key_inputs_and_shows_note(page, web_base_url):
    """`personal_api_keys_allowed=false`（管理者が個人キーを許可していない・既定）のとき、
    3種のキー入力欄は隠れ、「キーは管理者が設定します」の注記が出る。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "personal_api_keys_allowed": False}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#okey-row")).to_be_hidden()
    expect(page.locator("#okey-disabled-note")).to_be_visible()
    expect(page.locator("#gkey-row")).to_be_hidden()
    expect(page.locator("#gkey-disabled-note")).to_be_visible()
    expect(page.locator("#bkey-row")).to_be_hidden()
    expect(page.locator("#bkey-disabled-note")).to_be_visible()


def test_personal_keys_allowed_shows_key_inputs(page, web_base_url):
    """`personal_api_keys_allowed=true` のときは、これまでどおりキー入力欄が見える（注記は出さない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, settings=mock_api.SETTINGS_RESP)   # 既定モックは personal_api_keys_allowed=True
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#okey-row")).to_be_visible()
    expect(page.locator("#okey-disabled-note")).to_be_hidden()


def test_a7_non_selected_cloud_provider_hides_key_row_even_when_personal_keys_allowed(page, web_base_url):
    """personal_api_keys_allowed=true でも、選択中でないクラウド AI（A7）のキー欄は隠す
    （A6 のみでなく A7 の選択状態も見る）。"""
    from playwright.sync_api import expect

    settings = {**mock_api.SETTINGS_RESP_WITH_EXTRA_AGENTS, "cloud_provider": "gemini"}
    install_api_mocks(page, settings=settings)
    page.goto(f"{web_base_url}/settings.html")

    expect(page.locator("#okey-row")).to_be_hidden()
    expect(page.locator("#okey-disabled-note")).to_be_visible()
    expect(page.locator("#okey-disabled-note")).to_contain_text("選択されていません")
    expect(page.locator("#gkey-row")).to_be_visible()
    expect(page.locator("#gkey-disabled-note")).to_be_hidden()


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
