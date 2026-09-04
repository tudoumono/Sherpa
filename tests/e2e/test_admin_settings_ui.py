"""システム管理画面（admin-settings.html・S1 2026-07-08-設定分離とUI整備.md）の e2e。

- admin: 取り込みアームのチェックボックスが描画され、保存が PUT /admin/settings を叩く。
  （コスト単価表・為替は撤去済み・2026-07-08 フィードバック⑦＝金額表示をやめトークン数のみに）。
- 「既定に戻す」= 該当キーを null で送る。
- Med2（RV 2026-07-08）: アーム欄を触っていなければ arms_enabled は PUT body に含めない（ダーティフラグ）。
- 非 admin: アクセス拒否表示・保存バー非表示・ナビに「システム管理」が出ない。
- ナビ整理: 「個人設定」は全員・「システム管理」は admin のみ。
- Med4（RV 2026-07-08・W0）: 旧形式変換の「（既定）」マーカーは view.legacy_backend.default に動的追従
  （固定文言でない）・configured と effective を区別・専用の「既定に戻す」導線がある。
"""
from __future__ import annotations

import json

import pytest

from mock_api import SYSTEM_SETTINGS_VIEW, USER_MEMBER, install_api_mocks


def open_tab(page, key):
    """管理画面の5タブ（プロバイダ＋接続先/使えるモデル/取り込み/利用量/外部連携）のうち `key` を表に出す。
    既定で表示されている「プロバイダ＋接続先」（"provider"）以外の要素を操作する e2e は、
    アクション（click/fill/select_option 等）の actionability チェック（可視であること）を
    満たすため、操作前に必ずこれを呼ぶ。"""
    page.locator(f'.tab-btn[data-tab="{key}"]').click()


def open_advanced(page, tab_panel_id):
    """設計要件④の「詳細」折りたたみ（`<details class="adv">`）を開く（タブごとに1箇所）。"""
    page.locator(f'#{tab_panel_id} details.adv summary').click()


def re_compile_hash(tab):
    import re
    return re.compile(re.escape(f"#{tab}") + r"$")


def test_admin_settings_renders_and_saves(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    # 取り込みアーム（既知4つ・tesseract 直の ocr は撤去済み 2026-07-08）が平文説明つきで描画される。
    arms = page.locator("#arms-list input[type=checkbox]")
    expect(arms).to_have_count(3)
    expect(page.locator("#arms-list")).to_contain_text("Office 文書から直接読み取り")
    expect(page.locator("#arms-list")).to_contain_text("PDF の文字を抽出")
    expect(page.locator("#arms-list")).to_contain_text("画像・スキャン文書を AI が見て読み取り")  # vision（視覚読み取り）
    expect(page.locator("#arms-list input[data-arm='vision']")).to_be_enabled()
    # Med2: 未設定（configured=None）＝「既定に従っています」の平文ヒント。
    expect(page.locator("#arms-status")).to_contain_text("既定")

    # コスト単価表・為替は撤去済み＝画面に無い。
    expect(page.locator("#prices-body")).to_have_count(0)
    expect(page.locator("#usd-jpy")).to_have_count(0)

    # pdf_text を外して保存。
    page.locator("#arms-list input[data-arm='pdf_text']").uncheck()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["arms_enabled"] == ["ooxml"]        # チェック済のみ・触ったので含まれる
    assert "token_prices" not in put and "usd_jpy" not in put   # 金額系は送らない（撤去済み）
    # 保存後の応答は configured が付くので「固定中」の表示に変わる。
    expect(page.locator("#arms-status")).to_contain_text("固定中")


def test_admin_settings_save_without_touching_arms_omits_arms_enabled(page, web_base_url):
    """Med2: アーム欄も旧形式ラジオも触らずに保存すると、PUT body にどのキーも含まれない（空 body）
    （含めてしまうと、後で既定=env が変わっても system_settings に固定された古い一覧に追従できなくなる）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert "arms_enabled" not in put
    assert "legacy_backend" not in put            # W0: 旧形式ラジオも触っていなければ送らない（ピン留め回避）
    assert put == {}                               # 何も触っていない＝空 body


def test_admin_settings_save_after_touching_arms_includes_arms_enabled(page, web_base_url):
    """アームのチェックを実際に変えると、以降の保存で arms_enabled が含まれる。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator("#arms-list input[data-arm='pdf_text']").uncheck()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["arms_enabled"] == ["ooxml"]


def test_admin_settings_reverting_arms_to_original_state_omits_arms_enabled(page, web_base_url):
    """ダーティ判定は「触ったか」ではなく「render() 時点の基準値と今の値が異なるか」で行う。
    チェックを外して戻す（元の状態に復元する）と、丸印も PUT 対象からも外れる。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    checkbox = page.locator("#arms-list input[data-arm='pdf_text']")
    checkbox.uncheck()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()
    checkbox.check()   # 元の状態に戻す
    expect(page.locator("#tab-dot-ingest")).to_be_hidden()

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "arms_enabled" not in records["admin_settings_put"][-1]


def test_admin_settings_ingest_tab_reset_sends_null_for_arms_legacy_vlm_and_rag_llm_render(
        page, web_base_url):
    """「すべて既定に戻す」はタブ単位（カードごとの個別 reset ボタンはこのタブ共通の1つに
    統合済み）。「取り込み」タブのリセットは arms_enabled・legacy_backend・vlm・rag_llm_render
    （L5・U1）・agentic_budget_per_result・agentic_budget_total（BUDGET-1・§3.4）・
    model_context_windows（BUDGET-2・§3.4）の7キーをまとめて null で送る。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator('[data-reset-tab="ingest"]').click()
    expect(page.locator("#tab-reset-res-ingest")).to_contain_text("既定に戻しました")
    assert records["admin_settings_put"][-1] == {
        "arms_enabled": None, "legacy_backend": None, "vlm": None, "rag_llm_render": None,
        "agentic_budget_per_result": None, "agentic_budget_total": None,
        "model_context_windows": None}


def test_admin_settings_rag_llm_render_card_shows_plain_language_and_cost_notice(page, web_base_url):
    """L5（U1）: 検索用文書の読みやすい整形カードは平文（「LLM」「rag.md」等の内部語彙を出さない）で、
    利用料が発生することを明示する（04-画面の原則.md の専門用語ゼロ・見えない課金の防止）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    card = page.locator("#rag-llm-render-card")
    expect(card).to_be_visible()
    expect(card).to_contain_text("利用料が発生します")
    expect(card).not_to_contain_text("LLM")
    expect(card).not_to_contain_text("rag.md")
    # 既定 effective=True（モックの system_settings_resp）＝チェック済みで表示。
    expect(page.locator("#rag-llm-render")).to_be_checked()
    expect(page.locator("#rag-llm-render-status")).to_contain_text("既定に従っています")


def test_admin_settings_rag_llm_render_toggle(page, web_base_url):
    """触ってから保存すると rag_llm_render が "on"/"off" 文字列で PUT body に含まれる
    （rag_llm_render と同じダーティフラグ流儀・バックエンドは on|off 文字列のみ受け付ける）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator("#rag-llm-render").uncheck()   # 既定 effective=True からオフへ
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1]["rag_llm_render"] == "off"


def test_admin_settings_rag_llm_render_save_without_touching_omits_key(page, web_base_url):
    """未触の保存は rag_llm_render を PUT body に含めない（ピン留め回避・exclude_unset 意味論）。"""
    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#save").click()
    assert "rag_llm_render" not in records["admin_settings_put"][-1]


def test_admin_settings_rag_llm_render_toggle_reverted_omits_key_and_hides_dot(page, web_base_url):
    """トグルを元（既定のオン）に戻すと、丸印も PUT 対象からも外れる（値の差分で判定）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    cb = page.locator("#rag-llm-render")
    cb.uncheck()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()
    cb.check()
    expect(page.locator("#tab-dot-ingest")).to_be_hidden()

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "rag_llm_render" not in records["admin_settings_put"][-1]


def test_admin_settings_rag_llm_render_highlight_differs_from_default(page, web_base_url):
    """既定から変えた項目だけ強調する（取り込みタブ・rag_llm_render と同型）。"""
    import re

    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["rag_llm_render"] = {
        "configured": "off", "effective": False, "default": True, "options": ["on", "off"]}
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    expect(page.locator("#rag-llm-render")).not_to_be_checked()
    expect(page.locator("#rag-llm-render-status")).to_contain_text("固定中")
    expect(page.locator("#rag-llm-render-card")).to_have_class(re.compile(r"\bcfg-changed\b"))


# ===== BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・env→管理者設定への昇格）=====
# 「検索1回あたりの情報量（予算）」カード（取り込みタブ・U1 の rag-llm-render-card と同じタブ・
# depth_profile の整数欄と同じ流儀）。入力欄は KB 単位（表示/保存の境界で bytes と変換）。

def test_agentic_budget_card_renders_unset_state(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    expect(page.locator("#agentic-budget-per-result")).to_have_value("")
    expect(page.locator("#agentic-budget-per-result-hint")).to_contain_text("未設定です")
    expect(page.locator("#agentic-budget-total")).to_have_value("")
    expect(page.locator("#agentic-budget-total-hint")).to_contain_text("未設定です")


def test_agentic_budget_card_renders_configured_values_in_kb(page, web_base_url):
    """保存済みの予算（bytes）は KB へ換算して表示する（256000 bytes → 250KB）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["agentic_budget"]["per_result"] = {
        "configured": 256000, "effective": 256000, "default": 262144}
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    expect(page.locator("#agentic-budget-per-result")).to_have_value("250")
    expect(page.locator("#agentic-budget-per-result-hint")).to_contain_text("で固定中です")


def test_agentic_budget_card_save_sends_kb_converted_to_bytes(page, web_base_url):
    """入力（KB）は 1024 倍して bytes で PUT する。触っていない項目は送らない。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator("#agentic-budget-per-result").fill("500")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    body = records["admin_settings_put"][-1]
    assert body.get("agentic_budget_per_result") == 500 * 1024
    assert "agentic_budget_total" not in body   # 触っていない項目は送らない


def test_agentic_budget_card_clear_field_sends_null(page, web_base_url):
    """既に設定済みの欄を空欄に戻して保存すると null（未設定へ戻す）を送る。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["agentic_budget"]["total"] = {
        "configured": 2_000_000, "effective": 2_000_000, "default": 4194304}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    expect(page.locator("#agentic-budget-total")).to_have_value(str(round(2_000_000 / 1024)))

    page.locator("#agentic-budget-total").fill("")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1].get("agentic_budget_total") is None


def test_agentic_budget_card_save_rejects_out_of_range_client_side(page, web_base_url):
    """保存ボタン押下時、サーバと同じ範囲（1〜8192KB）を超える値は日本語エラーを表示して
    PUT 自体を送らない（422 の配列表示が読めなくなる問題を未然に防ぐ・depth_profile と同型）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator("#agentic-budget-per-result").fill("8193")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text(
        "検索結果1件あたりの上限は1〜8192（KB）の整数で指定してください")
    assert records["admin_settings_put"] == []


def test_agentic_budget_card_save_rejects_zero_client_side(page, web_base_url):
    """0 は下限未満（`min=4`）として拒否される（サーバの `ge=4096` bytes と同じ境界）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator("#agentic-budget-total").fill("0")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text(
        "1回の検索全体の上限は4〜65536（KB）の整数で指定してください")
    assert records["admin_settings_put"] == []


def test_agentic_budget_card_highlight_differs_from_default(page, web_base_url):
    """既定から変えた項目だけ強調する（depth_profile/rag_llm_render と同型）。"""
    import re

    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["agentic_budget"]["per_result"] = {
        "configured": 100_000, "effective": 100_000, "default": 262144}
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    expect(page.locator("#agentic-budget-per-result")).to_have_class(re.compile(r"\bcfg-changed\b"))
    expect(page.locator("#agentic-budget-total")).not_to_have_class(re.compile(r"\bcfg-changed\b"))


def test_agentic_budget_card_reset_tab_included_in_ingest_reset(page, web_base_url):
    """「取り込み」タブの「既定に戻す」は agentic_budget_per_result/agentic_budget_total も
    まとめて null で送る（他の4キーと同じ1つのボタン）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["agentic_budget"]["per_result"] = {
        "configured": 100_000, "effective": 100_000, "default": 262144}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    expect(page.locator("#agentic-budget-per-result")).to_have_value(str(round(100_000 / 1024)))

    page.locator('[data-reset-tab="ingest"]').click()
    expect(page.locator("#tab-reset-res-ingest")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["agentic_budget_per_result"] is None
    assert put["agentic_budget_total"] is None
    expect(page.locator("#agentic-budget-per-result")).to_have_value("")


# ===== BUDGET-2（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4・2026-09-03 裁定・
# モデル窓連動・min() 方式）=====

def test_agentic_budget_window_unknown_shows_plain_language_notice(page, web_base_url):
    """窓が不明（登録値/API/シードのどれにも無い）なら、平文の案内（申告）を出す
    （§3.4「限界に当たったら黙らない」）。mock 既定はこの unknown 状態。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    expect(page.locator("#agentic-budget-window-unknown")).to_be_visible()
    expect(page.locator("#agentic-budget-window-unknown")).to_contain_text("一度に読める量が未登録です")
    expect(page.locator("#agentic-budget-window-status")).to_contain_text("現在のモデル")


def test_agentic_budget_window_resolved_shows_tokens_source_and_cap(page, web_base_url):
    """窓が判明していれば、出所（登録値/自動取得/組み込み）とトークン数・自動調整後の上限を表示し、
    不明時の案内は隠す。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["agentic_budget"]["window"] = {
        "provider": "openai", "model": "gpt-4o-mini", "window_tokens": 128_000,
        "source": "seed", "derived_cap_bytes": 96_000}
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    status = page.locator("#agentic-budget-window-status")
    expect(status).to_contain_text("gpt-4o-mini")
    expect(status).to_contain_text("128,000")
    expect(status).to_contain_text("このアプリに組み込みの一覧")
    expect(status).to_contain_text("94KB")   # 96000 bytes ≒ 94KB（_fmtBytesHuman と同じ換算）
    expect(page.locator("#agentic-budget-window-unknown")).to_be_hidden()


def test_model_windows_table_renders_registered_rows(page, web_base_url):
    """登録済みの窓（"provider:model" → tokens）は表の行として描画される。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["agentic_budget"]["model_windows"] = {
        "configured": {"openai:gpt-4o": 128000, "ollama:qwen2.5": 32768}}
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    rows = page.locator("#agentic-model-windows-rows tr")
    expect(rows).to_have_count(2)
    # 行の値は <input>/<select> の value（textContent には現れない）——モデル名の集合で照合する。
    model_inputs = page.locator("#agentic-model-windows-rows .mw-model")
    values = [model_inputs.nth(i).input_value() for i in range(model_inputs.count())]
    assert set(values) == {"gpt-4o", "qwen2.5"}


def test_model_windows_table_add_row_and_save_sends_new_entry(page, web_base_url):
    """「行を追加」→入力→保存で、PUT body に "provider:model": tokens が乗る。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator("#agentic-model-windows-add").click()
    row = page.locator("#agentic-model-windows-rows tr").last
    row.locator(".mw-provider").select_option("openai")
    row.locator(".mw-model").fill("gpt-4o")
    row.locator(".mw-tokens").fill("128000")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    body = records["admin_settings_put"][-1]
    assert body.get("model_context_windows") == {"openai:gpt-4o": 128000}


def test_model_windows_table_delete_row_and_save_sends_remaining(page, web_base_url):
    """既存の行を削除して保存すると、残った行だけを送る（全削除なら null）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["agentic_budget"]["model_windows"] = {
        "configured": {"openai:gpt-4o": 128000}}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    expect(page.locator("#agentic-model-windows-rows tr")).to_have_count(1)

    page.locator("#agentic-model-windows-rows .mw-remove").click()
    expect(page.locator("#agentic-model-windows-rows tr")).to_have_count(0)
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1].get("model_context_windows") is None


def test_model_windows_table_save_rejects_invalid_tokens_client_side(page, web_base_url):
    """トークン数が範囲外（0）だと、日本語エラーを表示して PUT 自体を送らない
    （agentic_budget の range validation と同じ流儀）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator("#agentic-model-windows-add").click()
    row = page.locator("#agentic-model-windows-rows tr").last
    row.locator(".mw-model").fill("bad-model")
    row.locator(".mw-tokens").fill("0")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("一度に読める量（トークン数）は1〜10,000,000の整数で指定してください")
    assert records["admin_settings_put"] == []


def test_model_windows_table_included_in_ingest_reset(page, web_base_url):
    """「取り込み」タブの「既定に戻す」は model_context_windows も null で送る。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["agentic_budget"]["model_windows"] = {
        "configured": {"openai:gpt-4o": 128000}}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")

    page.locator('[data-reset-tab="ingest"]').click()
    expect(page.locator("#tab-reset-res-ingest")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["model_context_windows"] is None
    expect(page.locator("#agentic-model-windows-rows tr")).to_have_count(0)


def test_admin_settings_provider_tab_reset_sends_explicit_false_for_personal_keys(page, web_base_url):
    """裁定3: personal_api_keys_allowed のリセットは null ではなく明示 false を送る（実効既定と
    同値・バックエンドの一括削除は値が厳密に false になった時だけ発火するため、null では
    削除が起きず確認ダイアログの案内と食い違う）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["personal_api_keys_allowed"] = True
    system_settings["cloud"]["personal_keys_in_use_count"] = 3
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["personal_api_keys_allowed"] is False
    assert put["cloud_provider"] is None


def test_admin_settings_provider_tab_reset_cancel_aborts_when_confirm_rejected(page, web_base_url):
    """個人キー保有者がいる状態でのリセット確認ダイアログを棄却すると、PUT 自体が送られない
    （既存の手動 OFF 保存の確認ダイアログと同型の安全弁）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["personal_api_keys_allowed"] = True
    system_settings["cloud"]["personal_keys_in_use_count"] = 3
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator('[data-reset-tab="provider"]').click()   # ダイアログはハンドラ未登録＝既定で自動棄却
    expect(page.locator("#tab-reset-res-provider")).not_to_contain_text("既定に戻しました")
    assert records["admin_settings_put"] == []


def test_admin_settings_models_tab_reset_sends_model_catalog_null(page, web_base_url):
    """「使えるモデル」タブのリセットは model_catalog のみを対象にする（表示と実対象の一致）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    page.locator('[data-reset-tab="models"]').click()
    expect(page.locator("#tab-reset-res-models")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["model_catalog"] is None


def test_admin_settings_usage_tab_reset_sends_usage_chat_provider(page, web_base_url):
    """「利用量」タブのリセットは usage_chat_provider（STAT-2）を対象にする
    （user_api_keys_allowed・quota は「外部連携」タブのリセットが扱う）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    page.locator('[data-reset-tab="usage"]').click()
    expect(page.locator("#tab-reset-res-usage")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["usage_chat_provider"] is None
    assert "user_api_keys_allowed" not in put
    assert "user_api_keys_daily_quota_default" not in put


def test_admin_settings_extkeys_tab_reset_sends_explicit_false_and_null_quota(page, web_base_url):
    """「外部連携」タブのリセットは user_api_keys_allowed（実効既定と同値の明示 false）・
    user_api_keys_daily_quota_default（既定 quota への null）を対象に含める。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["ext_keys"]["user_api_keys_allowed"] = True
    system_settings["ext_keys"]["self_issued_active_count"] = 0
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    page.locator('[data-reset-tab="extkeys"]').click()
    expect(page.locator("#tab-reset-res-extkeys")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["user_api_keys_allowed"] is False
    assert put["user_api_keys_daily_quota_default"] is None
    assert "usage_chat_provider" not in put


def test_admin_settings_extkeys_tab_reset_confirms_when_self_issued_keys_active(page, web_base_url):
    """外部連携タブのリセットが利用者発行キーを失効させる場合、既存の失効確認ダイアログと
    同型の確認を経る。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["ext_keys"]["user_api_keys_allowed"] = True
    system_settings["ext_keys"]["self_issued_active_count"] = 2
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    page.locator('[data-reset-tab="extkeys"]').click()   # 未登録＝既定で自動棄却
    expect(page.locator("#tab-reset-res-extkeys")).not_to_contain_text("既定に戻しました")
    assert records["admin_settings_put"] == []

    page.once("dialog", lambda d: d.accept())
    page.locator('[data-reset-tab="extkeys"]').click()
    expect(page.locator("#tab-reset-res-extkeys")).to_contain_text("既定に戻しました")
    assert records["admin_settings_put"][-1]["user_api_keys_allowed"] is False


def test_admin_settings_put_rejected_by_research_provider_preflight_does_not_revoke_self_issued_keys(
        page, web_base_url):
    """同一 PUT で「利用者のキー発行を許可する」を OFF にしつつ research_default_provider を
    （中央 OpenAI キー未設定のまま）openai へ変更すると、保存時 preflight で PUT 全体が 422
    拒否される——このとき自己発行キーの失効（モックの `ext_keys_store` 直接書き換え）も
    実行されていない（モックの原子性・失敗した保存の副作用が部分的にだけ残らない）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["ext_keys"]["user_api_keys_allowed"] = True
    system_settings["ext_keys"]["self_issued_active_count"] = 1
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    # 自己発行キーを実際に1本発行する（owner_uid 付きの行を ext_keys_store に作る）。一覧は
    # ページ読み込み時に1回だけ取得される（発行後に自動更新されない）ため、reload で権威ある
    # 一覧を取り直す。
    page.evaluate("""() => fetch('/ext/v1/keys', {
      method: 'POST', headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({label: 'atomic-test-self-key'}),
    })""")
    page.reload()

    open_tab(page, "extkeys")
    expect(page.locator("#ext-keys-list")).to_contain_text("atomic-test-self-key")
    expect(page.locator("#ext-keys-list")).to_contain_text("有効")

    page.locator("#ext-keys-user-allowed").uncheck()
    page.locator("#ext-research-default-provider").select_option("openai")
    page.once("dialog", lambda d: d.accept())
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("OpenAI にできません")

    # 拒否された PUT の副作用が残っていないか、再読込して権威ある状態を確認する
    # （save() は失敗時に一覧を再取得しないため、ここでの reload が必須）。
    page.reload()
    open_tab(page, "extkeys")
    expect(page.locator("#ext-keys-user-allowed")).to_be_checked()
    expect(page.locator("#ext-keys-list")).to_contain_text("atomic-test-self-key")
    expect(page.locator("#ext-keys-list")).to_contain_text("有効")


def test_admin_settings_research_default_provider_renders_saves_and_dirty_by_value(page, web_base_url):
    """「外部連携」タブの「AI 下調べ検索の既定 AI」select は render 時の effective 値を基準にした
    値差分で dirty 判定する（touched フラグではない＝変更してから元に戻すと丸印が消える）。
    中央 OpenAI キーが設定済みの状態で保存すると research_default_provider が PUT body に含まれ、
    ヒント文言は UI 語彙（「ローカル（Ollama）」）と揃っている。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True   # 保存時 preflight を通す（下の否定形テストと対）
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    sel = page.locator("#ext-research-default-provider")
    expect(sel).to_have_value("ollama")
    hint = page.locator("#ext-research-default-provider-hint")
    expect(hint).to_contain_text("未設定")
    expect(hint).to_contain_text("ローカル（Ollama）")

    sel.select_option("openai")
    expect(page.locator("#tab-dot-extkeys")).to_be_visible()
    sel.select_option("ollama")   # 元に戻す＝値は render 時と同じ＝丸印は消える
    expect(page.locator("#tab-dot-extkeys")).to_be_hidden()

    sel.select_option("openai")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["research_default_provider"] == "openai"
    # 保存後の応答は configured が付くので「固定中」表示に変わる。
    expect(hint).to_contain_text("固定中")


def test_admin_settings_research_default_provider_openai_save_rejected_without_key(page, web_base_url):
    """中央 OpenAI キー未設定のまま「AI 下調べ検索の既定 AI」を OpenAI にして保存すると、
    実サーバの保存時 preflight（`_assert_research_default_provider_sendable`）と同じく 422 で
    拒否され、保存されずにエラー表示になる（既定のモック状態＝openai_key_set は False）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    page.locator("#ext-research-default-provider").select_option("openai")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("OpenAI にできません")
    expect(page.locator("#msg")).not_to_contain_text("保存しました")


def test_admin_settings_research_default_provider_invalid_saved_value_shown_not_rounded(page, web_base_url):
    """保存値が ollama/openai のどちらでもない（破損 JSONB・`system_extras.py` が返す
    "(不正な保存値)"）場合、select は黙って「ローカル（Ollama）」に丸めず、その値をそのまま
    示す一時的な選択肢を表示し、平文の注意文言も出す。この状態のまま保存しても何も送らない
    （破損状態が保存の基準値として扱われない）。ollama/openai を選び直せば通常どおり保存できる。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["ext_keys"]["research_default_provider"] = {
        "configured": "gemini", "effective": "(不正な保存値)", "default": "ollama"}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    sel = page.locator("#ext-research-default-provider")
    expect(sel).to_have_value("__invalid__")
    expect(sel.locator("option:checked")).to_have_text("(不正な保存値)")
    expect(page.locator("#ext-research-default-provider-invalid")).to_be_visible()
    expect(page.locator("#ext-research-default-provider-invalid")).to_contain_text("正しくありません")

    # 破損状態のまま保存しても research_default_provider は送らない（触っていない扱い）。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "research_default_provider" not in records["admin_settings_put"][-1]

    # ollama/openai を選び直せば通常どおり送られ、警告は消える。
    sel.select_option("ollama")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1]["research_default_provider"] == "ollama"
    expect(page.locator("#ext-research-default-provider-invalid")).to_be_hidden()


def test_admin_settings_research_default_provider_save_without_touching_omits_key(page, web_base_url):
    """触らずに保存すると research_default_provider は PUT body に含まれない（ピン留め回避）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "research_default_provider" not in records["admin_settings_put"][-1]


def test_admin_settings_extkeys_tab_reset_includes_research_default_provider_null(page, web_base_url):
    """外部連携タブのリセットは research_default_provider も null（未設定へ戻す）を対象に含める。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["ext_keys"]["research_default_provider"] = {
        "configured": "openai", "effective": "openai", "default": "ollama"}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    expect(page.locator("#ext-research-default-provider")).to_have_value("openai")

    page.locator('[data-reset-tab="extkeys"]').click()
    expect(page.locator("#tab-reset-res-extkeys")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["research_default_provider"] is None
    expect(page.locator("#ext-research-default-provider")).to_have_value("ollama")


def test_admin_settings_tab_reset_preserves_other_tab_unsaved_draft_and_dirty_dot(page, web_base_url):
    """タブ単位リセットは対象タブの描画・状態だけを更新し、他タブの未保存編集・丸印・
    書込専用キー入力は無言で消さない。取り込みタブで未保存のまま、プロバイダタブの
    キー入力・利用量タブのリセットを行っても、取り込みタブの編集内容は残る。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    # プロバイダタブ（既定表示）で未保存のキー入力を残す。
    page.locator("#cloud-key").fill("sk-unsaved-draft")

    # 取り込みタブで未保存の編集を行う。
    open_tab(page, "ingest")
    page.locator("#arms-list input[data-arm='pdf_text']").uncheck()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()

    # 利用量タブをリセットする（取り込み・プロバイダの未保存編集とは無関係のはず）。
    open_tab(page, "usage")
    page.locator('[data-reset-tab="usage"]').click()
    expect(page.locator("#tab-reset-res-usage")).to_contain_text("既定に戻しました")

    # 取り込みタブの未保存編集・丸印が残っている。
    open_tab(page, "ingest")
    expect(page.locator("#arms-list input[data-arm='pdf_text']")).not_to_be_checked()
    expect(page.locator("#tab-dot-ingest")).to_be_visible()

    # プロバイダタブの未保存キー入力も残っている。
    open_tab(page, "provider")
    expect(page.locator("#cloud-key")).to_have_value("sk-unsaved-draft")

    # 保存すると、取り込み（触った）とプロバイダ（キー入力）の両方が PUT body に載る
    # （利用量タブのリセットに巻き込まれて消えていない）。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["arms_enabled"] == ["ooxml"]
    assert put["openai_api_key"] == "sk-unsaved-draft"


def test_admin_settings_legacy_backend_radio_and_missing_notice(page, web_base_url):
    """W0/W1: 旧形式（.doc/.xls/.ppt）変換の3択ラジオが描画される。soffice 未検出時は LibreOffice を、
    office_com ワーカー不達時は Office 連携を選べず、それぞれ案内が出る。"""
    from playwright.sync_api import expect

    install_api_mocks(page)   # 既定 = soffice 未検出・office_com ワーカー未設定（SYSTEM_SETTINGS_VIEW）
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")   # 旧形式変換は「詳細」の中（設計要件④）

    expect(page.locator("#legacy-block")).to_be_visible()
    radios = page.locator("#legacy-radios input[type=radio]")
    expect(radios).to_have_count(3)
    expect(page.locator("#legacy-radios input[data-legacy='none']")).to_be_checked()      # 既定＝使わない
    expect(page.locator("#legacy-radios input[data-legacy='libreoffice']")).to_be_disabled()
    expect(page.locator("#legacy-radios input[data-legacy='office_com']")).to_be_disabled()
    expect(page.locator("#legacy-radios")).to_contain_text("使わない（既定）")
    expect(page.locator("#legacy-radios")).to_contain_text("Office 連携")
    expect(page.locator("#legacy-status")).to_contain_text("既定に従っています")
    expect(page.locator("#legacy-lo-missing")).to_be_visible()
    expect(page.locator("#legacy-lo-missing")).to_contain_text("LibreOffice が見つかりません")
    # office_com は URL 未設定＝「接続先 SHERPA_OFFICE_COM_URL を設定してください」案内。
    expect(page.locator("#legacy-oc-missing")).to_be_visible()
    expect(page.locator("#legacy-oc-missing")).to_contain_text("SHERPA_OFFICE_COM_URL")


def test_admin_settings_legacy_backend_office_com_reachable(page, web_base_url):
    """W1: office_com ワーカー到達可なら Office 連携を選択でき、保存で legacy_backend が PUT body に含まれる。
    URL 設定済みで到達可＝起動案内は出ない・検出できた Office バージョンが表示される。"""
    from playwright.sync_api import expect

    view = {**SYSTEM_SETTINGS_VIEW, "legacy_backend": {
        "configured": None, "effective": "none", "default": "none",
        "options": ["none", "libreoffice", "office_com"],
        "libreoffice": {"available": False, "version": None},
        "office_com": {"configured_url": True, "available": True,
                       "versions": {"word": "16.0", "excel": "16.0", "powerpoint": "16.0"}}}}
    records = install_api_mocks(page, system_settings=view)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")   # 旧形式変換は「詳細」の中（設計要件④）

    expect(page.locator("#legacy-oc-missing")).to_be_hidden()
    expect(page.locator("#legacy-radios")).to_contain_text("Word 16.0")   # versions 要約表示
    oc = page.locator("#legacy-radios input[data-legacy='office_com']")
    expect(oc).to_be_enabled()
    oc.check()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["legacy_backend"] == "office_com"
    expect(page.locator("#legacy-radios input[data-legacy='office_com']")).to_be_checked()
    expect(page.locator("#legacy-status")).to_contain_text("固定中")


def test_admin_settings_legacy_backend_office_com_direct(page, web_base_url):
    """W2'（2026-07-08）: URL 未設定でも direct（同一マシンの WSL 連携）が検出できれば Office 連携を選べる
    （起動案内は出ない・「このパソコンの Office を直接使用」と表示）。"""
    from playwright.sync_api import expect

    view = {**SYSTEM_SETTINGS_VIEW, "legacy_backend": {
        "configured": None, "effective": "none", "default": "none",
        "options": ["none", "libreoffice", "office_com"],
        "libreoffice": {"available": False, "version": None},
        "office_com": {"configured_url": False, "mode": "direct", "powershell": True,
                       "available": True,
                       "versions": {"word": "16.0", "excel": "16.0", "powerpoint": "16.0"}}}}
    records = install_api_mocks(page, system_settings=view)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")   # 旧形式変換は「詳細」の中（設計要件④）

    expect(page.locator("#legacy-oc-missing")).to_be_hidden()     # 設定不要＝案内は出ない
    expect(page.locator("#legacy-radios")).to_contain_text("このパソコンの Office を直接使用")
    oc = page.locator("#legacy-radios input[data-legacy='office_com']")
    expect(oc).to_be_enabled()
    oc.check()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["legacy_backend"] == "office_com"
    expect(page.locator("#legacy-radios input[data-legacy='office_com']")).to_be_checked()


def test_admin_settings_legacy_backend_select_and_save(page, web_base_url):
    """W0: soffice 検出時は LibreOffice を選択でき、保存で legacy_backend が PUT body に含まれる。"""
    from playwright.sync_api import expect

    avail = {**SYSTEM_SETTINGS_VIEW, "legacy_backend": {
        "configured": None, "effective": "none", "default": "none",
        "options": ["none", "libreoffice"],
        "libreoffice": {"available": True, "version": "LibreOffice 7.5"}}}
    records = install_api_mocks(page, system_settings=avail)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")   # 旧形式変換は「詳細」の中（設計要件④）

    expect(page.locator("#legacy-lo-missing")).to_be_hidden()
    lo = page.locator("#legacy-radios input[data-legacy='libreoffice']")
    expect(lo).to_be_enabled()
    lo.check()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["legacy_backend"] == "libreoffice"   # 触ったので含まれる・選択値が送られる
    # 保存後の応答で configured が付き、ラジオは LibreOffice のまま。
    expect(page.locator("#legacy-radios input[data-legacy='libreoffice']")).to_be_checked()
    expect(page.locator("#legacy-status")).to_contain_text("固定中")


def test_admin_settings_legacy_backend_default_marker_follows_env_default(page, web_base_url):
    """Med4（RV 2026-07-08）: 「（既定）」マーカーは固定文言でなく view.legacy_backend.default に動的追従する
    （env 既定が libreoffice の環境で「使わない（既定）」と誤表示しない）。configured='none'（明示選択）は
    effective とは別の情報として保持され、選択状態と「固定中」表示に反映される
    （未設定 null と明示 none を区別できないと、明示 none を選んでも「既定に従っています」と誤表示する）。"""
    from playwright.sync_api import expect

    view = {**SYSTEM_SETTINGS_VIEW, "legacy_backend": {
        "configured": "none", "effective": "none", "default": "libreoffice",
        "options": ["none", "libreoffice"],
        "libreoffice": {"available": True, "version": "LibreOffice 7.5"}}}
    install_api_mocks(page, system_settings=view)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")   # 旧形式変換は「詳細」の中（設計要件④）

    radios_block = page.locator("#legacy-radios")
    expect(radios_block).to_contain_text("LibreOffice で変換（既定）")   # 既定マーカーは libreoffice 側
    expect(radios_block).not_to_contain_text("使わない（既定）")         # none には付かない（誤表示解消）
    expect(page.locator("#legacy-radios input[data-legacy='none']")).to_be_checked()   # 明示 none が選択状態
    expect(page.locator("#legacy-status")).to_contain_text("固定中")     # configured!=null＝固定中表示


def test_admin_settings_vlm_renders_and_saves(page, web_base_url):
    """⑤（feedback-batch-2026-07-08）: 視覚読み取り（markitdown_ocr）の VLM 設定小節。
    既定＝ローカル(Ollama)・モデル名・クラウド許可チェックボックス＋注意文言が描画され、
    クラウドに切り替えると OpenAI キー未設定の案内が出る。触って保存すると vlm が PUT body に含まれる。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")   # 旧形式変換・視覚読み取りは「詳細」の中（設計要件④）

    expect(page.locator("#vlm-block")).to_be_visible()
    # 既定＝ローカル(Ollama)・モデル qwen2.5vl・クラウド未許可。
    expect(page.locator("#vlm-provider")).to_have_value("ollama")
    expect(page.locator("#vlm-model")).to_have_value("qwen2.5vl")
    expect(page.locator("#vlm-cloud-allowed")).not_to_be_checked()
    # クラウド送信の注意文言が出ている（専門用語ゼロ・04-画面の原則.md）。
    expect(page.locator("#vlm-block")).to_contain_text("画像が外部の AI")
    # 既定に従っている状態のヒント。
    expect(page.locator("#vlm-status")).to_contain_text("既定")

    # クラウド(OpenAI)へ切り替える → キー未設定案内（openai_key_present=False）が出る。
    page.locator("#vlm-provider").select_option("openai")
    expect(page.locator("#vlm-key-missing")).to_be_visible()
    expect(page.locator("#vlm-key-missing")).to_contain_text("OPENAI_API_KEY")
    # クラウド許可にチェックしてモデルを変えて保存。
    page.locator("#vlm-cloud-allowed").check()
    page.locator("#vlm-model").fill("gpt-4o")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["vlm"] == {"provider": "openai", "model": "gpt-4o", "cloud_allowed": True}
    # 保存後の応答は configured が付くので「固定中」の表示に変わる。
    expect(page.locator("#vlm-status")).to_contain_text("固定中")


def test_admin_settings_vlm_save_without_touching_omits_vlm(page, web_base_url):
    """⑤: VLM 設定を触らずに保存すると vlm は PUT body に含まれない（ピン留め回避）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "vlm" not in records["admin_settings_put"][-1]


def test_admin_settings_usage_chat_ai_radio_toggle(page, web_base_url):
    """STAT-2: 「利用統計チャットに使う AI」ラジオは3択（実行構成に合わせる／OpenAI に固定／
    ローカル(Ollama) に固定）で、選択状態は `configured`（生の保存値）基準——
    `configured=None` なら effective が何であれ「実行構成に合わせる」がチェックされる。
    触ってから保存すると usage_chat_provider が PUT body に含まれる。
    未触の保存は含めない（rag_llm_render と同じダーティフラグ流儀）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)   # usage_chat.configured=None, effective="openai"（既定）
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    expect(page.locator("#usage-chat-ai-card")).to_be_visible()
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider=""]')).to_be_checked()

    page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]').check()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1]["usage_chat_provider"] == "ollama"


def test_admin_settings_usage_chat_ai_save_without_touching_omits_key(page, web_base_url):
    """未触の保存は usage_chat_provider を PUT body に含めない（exclude_unset 意味論）。"""
    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#save").click()
    assert "usage_chat_provider" not in records["admin_settings_put"][-1]


def test_admin_settings_usage_chat_ai_save_reflects_and_reload_persists(page, web_base_url):
    """保存すると PUT 応答（render(view)）にすぐ反映され、再読込（GET）でも同じ値が返る
    （mock_api の PUT が system_settings_resp を in-place 更新する契約・
    rag_llm_render 等の他フィールドと同型）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]').check()
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]')).to_be_checked()

    page.reload()
    open_tab(page, "usage")
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]')).to_be_checked()


def test_admin_settings_usage_chat_ai_toggle_reverted_hides_dot(page, web_base_url):
    """ラジオを ollama にしてから元（`configured=None`＝「実行構成に合わせる」）に戻すと、
    丸印も PUT 対象からも外れる（rag_llm_render の同型テストと同じ値差分ベースの判定）。
    baseline は `configured` 基準のため、戻す先は effective と同じ値（"openai"）の固定
    ラジオではなく「実行構成に合わせる」ラジオである。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)   # usage_chat.configured=None（既定）
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    ollama_radio = page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]')
    follow_radio = page.locator('#usage-chat-ai-radios input[data-usage-chat-provider=""]')
    ollama_radio.check()
    expect(page.locator("#tab-dot-usage")).to_be_visible()
    follow_radio.check()
    expect(page.locator("#tab-dot-usage")).to_be_hidden()

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "usage_chat_provider" not in records["admin_settings_put"][-1]


def test_admin_settings_usage_chat_ai_fix_survives_simultaneous_a7_change(page, web_base_url):
    """A7（`cloud_provider`）を変更するのと同時に、利用統計チャット専用 AI を現在の実効値へ
    明示固定して1回で保存しても、A7 の変更によって黙って反転しない。
    `configured=None`（実行構成に合わせる）のまま「OpenAI に固定」を選ばずに保存すると、
    usage_chat_provider は PUT から省略され、直後に A7 を Gemini へ変えた保存で実効値が
    Ollama へ反転してしまっていた——「OpenAI に固定」を明示選択すれば、A7 が何であれ
    usage_chat_provider が常に明示送信されるため反転しない。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)   # usage_chat.configured=None, cloud.provider="openai"
    page.goto(f"{web_base_url}/admin-settings.html")

    # 1回の保存の中で、A7 を Gemini へ変更しつつ、利用統計チャット専用 AI は
    # 現在の実効値（OpenAI）で明示固定する。
    page.locator("input[data-cloud-provider='gemini']").check()
    open_tab(page, "usage")
    page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="openai"]').check()

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")

    put = records["admin_settings_put"][-1]
    assert put.get("cloud_provider") == "gemini"
    assert put.get("usage_chat_provider") == "openai", \
        "同時保存で明示固定した usage_chat_provider が省略されてはいけない"

    # 保存直後の反映・再読込後の両方で、A7 が Gemini になっても OpenAI に固定されたまま
    # （黙って Ollama へ反転しない）。
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="openai"]')).to_be_checked()
    page.reload()
    open_tab(page, "usage")
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="openai"]')).to_be_checked()


def test_admin_settings_usage_chat_ai_switch_from_fixed_to_follow_sends_explicit_null(
        page, web_base_url):
    """明示固定（`configured="ollama"`）から「実行構成に合わせる」へ切り替えて保存すると、
    PUT body に `usage_chat_provider: null` が明示送信される——baseline（"ollama"）から
    「実行構成に合わせる」（`configured` の DOM 表現 `""`）へ実際に値が変わったので、
    rag_llm_render 等と同じダーティフラグ流儀で保存対象になる。初期値が既に「実行構成に
    合わせる」で、触らずに保存すると usage_chat_provider が PUT から省略されるケース
    （`test_admin_settings_usage_chat_ai_save_without_touching_omits_key` 等）とは別の経路
    ——どちらも「今の選択のまま」だが、前者は明示的な変更（null を送る）、後者は無変更
    （省略する）という違いがある。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "ollama", "effective": "ollama",
                              "default": "openai", "providers": ["openai", "ollama"]}
    records = install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]')).to_be_checked()

    page.locator('#usage-chat-ai-radios input[data-usage-chat-provider=""]').check()
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")

    put = records["admin_settings_put"][-1]
    assert "usage_chat_provider" in put, "「実行構成に合わせる」への変更は明示送信されるはず"
    assert put["usage_chat_provider"] is None

    # 保存直後の反映・再読込後の両方で「実行構成に合わせる」のまま。
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider=""]')).to_be_checked()
    page.reload()
    open_tab(page, "usage")
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider=""]')).to_be_checked()


def test_admin_settings_usage_chat_ai_openai_fix_shows_hint_when_a7_not_openai(page, web_base_url):
    """A7（`cloud_provider`）が openai 以外（例: gemini）の間、「OpenAI に固定」ラジオの横に、
    中央 OpenAI キーが実行構成が OpenAI の時しか使えない旨の注記を出す——A7 の排他選択契約
    により、この状態で「OpenAI に固定」しても実際には 503（未接続）になるため、選ぶ前に
    理由が分かるようにする。A7 が openai なら注記は出ない。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["cloud"]["provider"] = "gemini"
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    expect(page.locator("#usage-chat-ai-radios")).to_contain_text(
        "OpenAI のキーは頭脳の選択が OpenAI のときだけ使えます")
    expect(page.locator("#usage-chat-ai-radios")).to_contain_text("現在: Gemini")

    # A7 を openai へ戻すと注記は消える（保存せず、同じページ内の別タブの変更だけでは反映
    # されないため、A7 を openai に戻した状態を最初から与えて再確認する）。
    settings2 = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings2["cloud"]["provider"] = "openai"
    install_api_mocks(page, system_settings=settings2)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")
    expect(page.locator("#usage-chat-ai-radios")).not_to_contain_text("頭脳の選択が OpenAI のとき")


def test_admin_settings_usage_chat_ai_shows_explicit_error_when_response_malformed(page, web_base_url):
    """`usage_chat` が欠落/形状不正な応答でも、カードを隠したり
    'openai' へ黙って補完したりせず、明示エラーを表示する。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    del settings["usage_chat"]
    records = install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    expect(page.locator("#usage-chat-ai-card")).to_be_visible()
    expect(page.locator("#usage-chat-ai-radios")).to_contain_text("読み込めませんでした")
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider]')).to_have_count(0)

    # 壊れたデータのまま保存しても usage_chat_provider は送らない（誤った値を捏造しない）。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "usage_chat_provider" not in records["admin_settings_put"][-1]


def test_admin_settings_usage_chat_ai_missing_configured_key_shows_explicit_error(page, web_base_url):
    """`usage_chat` 自体はあっても `configured` キーが欠落（`undefined`）している応答は、
    `null`（未設定＝実行構成に合わせる、という正当な値）と取り違えず、明示エラーを表示する
    ——`configured` の値ではなくキーの有無で判定するため、欠落を「未設定」と黙って
    同一視しない。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    del settings["usage_chat"]["configured"]
    records = install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    expect(page.locator("#usage-chat-ai-card")).to_be_visible()
    expect(page.locator("#usage-chat-ai-radios")).to_contain_text("読み込めませんでした")
    expect(page.locator('#usage-chat-ai-radios input[data-usage-chat-provider]')).to_have_count(0)

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "usage_chat_provider" not in records["admin_settings_put"][-1]


def test_admin_settings_ingest_and_usage_highlight_differ_from_default(page, web_base_url):
    """既定から変えた項目だけ強調する（取り込み・利用量タブ）。既定と一致する間は強調しない。"""
    import re

    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    # 取り込み: enabled が env_default と異なる＝差分あり。
    settings["arms"]["configured"] = ["ooxml"]
    settings["arms"]["enabled"] = ["ooxml"]
    # STAT-2: 利用統計チャットに使う AI も effective が default（openai）と異なる＝差分あり。
    settings["usage_chat"] = {"configured": "ollama", "effective": "ollama", "default": "openai",
                              "providers": ["openai", "ollama"]}
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    open_tab(page, "ingest")
    expect(page.locator("#arms-list")).to_have_class("cfg-changed")

    open_tab(page, "usage")
    expect(page.locator("#usage-chat-ai-card")).to_have_class(re.compile(r"\bcfg-changed\b"))


def test_admin_settings_tab_selection_persists_across_reload_via_url_hash(page, web_base_url):
    """タブは URL ハッシュで記憶する。別タブを選んで再読込しても同じタブが開いたまま、
    不正なハッシュ値は既定タブ（プロバイダ＋接続先）へ倒す。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    expect(page).to_have_url(re_compile_hash("ingest"))

    page.reload()
    expect(page.locator('.tab-btn[data-tab="ingest"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#tabpanel-ingest")).to_be_visible()
    expect(page.locator("#tabpanel-provider")).to_be_hidden()

    page.goto(f"{web_base_url}/admin-settings.html#no-such-tab")
    expect(page.locator('.tab-btn[data-tab="provider"]')).to_have_attribute("aria-selected", "true")
    expect(page.locator("#tabpanel-provider")).to_be_visible()


def test_admin_settings_denied_for_non_admin(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page, user=USER_MEMBER)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#save-bar")).to_be_hidden()


def test_nav_system_admin_visible_for_admin_only(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page)   # 既定 = admin
    page.goto(f"{web_base_url}/home.html")

    nav = page.locator("#sherpa-nav")
    expect(nav.get_by_text("個人設定", exact=True)).to_be_visible()      # 全員
    expect(nav.get_by_text("システム管理", exact=True)).to_be_visible()  # admin


def test_nav_system_admin_hidden_for_non_admin(page, web_base_url):
    from playwright.sync_api import expect

    install_api_mocks(page, user=USER_MEMBER)
    page.goto(f"{web_base_url}/home.html")

    nav = page.locator("#sherpa-nav")
    expect(nav.get_by_text("個人設定", exact=True)).to_be_visible()      # 全員
    expect(nav.get_by_text("システム管理", exact=True)).to_have_count(0)  # 非 admin には出ない


# ===== クラウド AI プロバイダの中央設定 =====

def test_admin_cloud_provider_renders_defaults_and_switches_key_block(page, web_base_url):
    """既定モック（openai 選択・3キーとも未設定・個人キー許可 OFF）の描画と、ラジオ切替でキー欄の
    ラベル/プレースホルダが選択中プロバイダに追従することを確認する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("input[data-cloud-provider='openai']")).to_be_checked()
    expect(page.locator("#cloud-key-label")).to_contain_text("OpenAI")
    expect(page.locator("#cloud-key")).to_have_value("")
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "未設定")
    expect(page.locator("#personal-keys-allowed")).not_to_be_checked()
    expect(page.locator("#cloud-status")).to_contain_text("OpenAI")
    expect(page.locator("#cloud-status")).to_contain_text("中央設定のみ")
    expect(page.locator("#cloud-ollama-url")).to_have_value("http://localhost:11434")

    page.locator("input[data-cloud-provider='gemini']").check()
    expect(page.locator("#cloud-key-label")).to_contain_text("Gemini")
    # プロバイダ切替でキー欄は再描画され、入力しかけていた値は残らない（前プロバイダ向けの誤送信防止）。
    expect(page.locator("#cloud-key")).to_have_value("")


def test_admin_cloud_provider_save_sends_only_touched_fields(page, web_base_url):
    """触った項目だけ PUT body に含まれる（他カードと同じピン留め回避の流儀）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("input[data-cloud-provider='bedrock']").check()
    page.locator("#cloud-key").fill("bedrock-secret-key")
    page.locator("#personal-keys-allowed").check()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["cloud_provider"] == "bedrock"
    assert put["bedrock_api_key"] == "bedrock-secret-key"
    assert put["personal_api_keys_allowed"] is True
    # 触っていない項目（アーム・旧形式変換等）は送らない。
    assert "arms_enabled" not in put and "legacy_backend" not in put
    assert "openai_api_key" not in put and "gemini_api_key" not in put


def test_admin_cloud_provider_explicit_click_on_default_still_saves_raw_value(page, web_base_url):
    """FBK-1 RV1（境界回帰#2・2026-09-01）: 初期表示の既定（openai）のラジオを明示的にクリックして
    キーを保存すると、値が変わらなくても `cloud_provider` が PUT body に含まれる（`provider_raw`
    が未設定＝一度も選んでいない状態から、明示選択済みへ変わる）。一方、ラジオに一切触れず
    他の項目（個人キー許可）だけを保存する場合は、従来どおり `cloud_provider` を送らない
    （未操作の保存でクラウド選択を勝手に作らない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    # 既定で openai が選択済み（`provider_raw` は None）のラジオを明示的にクリックする
    # （値は変わらない＝native の `change` イベントは発火しない状態でも拾えることを確認する）。
    page.locator("input[data-cloud-provider='openai']").click()
    page.locator("#cloud-key").fill("openai-secret-key")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["cloud_provider"] == "openai"
    assert put["openai_api_key"] == "openai-secret-key"


def test_admin_cloud_provider_untouched_radio_omits_cloud_provider_on_other_save(page, web_base_url):
    """FBK-1 RV1（境界回帰#2）: クラウド選択ラジオに一切触れず、無関係な項目（個人キー許可）だけを
    保存した場合は `cloud_provider` を PUT body に含めない（未操作の別設定保存で raw 値を
    勝手に作らない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#personal-keys-allowed").check()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert "cloud_provider" not in put
    assert put["personal_api_keys_allowed"] is True


def test_admin_cloud_provider_recovers_from_invalid_raw_by_reselecting_rounded_value(page, web_base_url):
    """FBK-1 RV2（境界回帰#4・2026-09-01）: 不正な `cloud_provider`（`provider_raw`）が保存されている
    構成では、画面は既定 openai へ丸めて表示するが、admin が案内どおり openai を選び直しても
    （丸め後の値と一致するだけなので）以前は「変更なし」に見えて保存対象から漏れていた。
    正規化した raw と現在選択が食い違う場合は、touched なら値が変わらなくても送る。"""
    import json

    import mock_api
    from playwright.sync_api import expect

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["provider"] = "openai"           # 丸め後の実効値
    system_settings["cloud"]["provider_raw"] = "not-a-real-provider"   # 不正な生値
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    # 既に画面上は openai が選択済み（丸め後の値）——それを明示的にクリックし直す。
    page.locator("input[data-cloud-provider='openai']").click()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["cloud_provider"] == "openai"


def test_admin_personal_keys_toggle_shows_deletion_note(page, web_base_url):
    """トグル付近に「OFF の状態では個人キーは保存されない（既存は削除される）」の注意書きが出る。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    note = page.locator("#personal-keys-allowed").locator("xpath=ancestor::label").locator(".arm-d")
    expect(note).to_contain_text("個人キーは保存されません")
    expect(note).to_contain_text("削除されます")


def test_admin_personal_keys_off_save_confirms_with_count_and_cancel_aborts(page, web_base_url):
    """personal_api_keys_allowed を ON→OFF で保存すると、個人キーを保有する利用者数を示す確認
    ダイアログが出る。キャンセルすると保存全体を中断する（他フィールドの変更も含めて何も送らない）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["personal_api_keys_allowed"] = True
    system_settings["cloud"]["personal_keys_in_use_count"] = 3
    records = install_api_mocks(page, system_settings=system_settings)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#personal-keys-allowed")).to_be_checked()
    page.locator("#personal-keys-allowed").uncheck()
    page.locator("#save").click()

    assert dialogs and "3 人" in dialogs[0] and "削除されます" in dialogs[0]
    expect(page.locator("#msg")).to_contain_text("保存を取り消しました")
    assert records["admin_settings_put"] == []   # PUT 自体が送られない


def test_admin_personal_keys_off_save_proceeds_when_confirmed(page, web_base_url):
    """確認ダイアログで OK すれば通常どおり保存される。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["personal_api_keys_allowed"] = True
    system_settings["cloud"]["personal_keys_in_use_count"] = 2
    records = install_api_mocks(page, system_settings=system_settings)
    page.on("dialog", lambda d: d.accept())
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#personal-keys-allowed").uncheck()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["personal_api_keys_allowed"] is False


def test_admin_personal_keys_off_save_skips_dialog_when_no_keys_in_use(page, web_base_url):
    """削除対象が0件のとき（誰も個人キーを保存していない）は確認ダイアログを出さずそのまま保存する。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["personal_api_keys_allowed"] = True
    system_settings["cloud"]["personal_keys_in_use_count"] = 0
    records = install_api_mocks(page, system_settings=system_settings)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#personal-keys-allowed").uncheck()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert dialogs == []
    assert records["admin_settings_put"][-1]["personal_api_keys_allowed"] is False


def test_admin_cloud_key_shows_configured_placeholder_when_key_set(page, web_base_url):
    """キー設定済み（openai_key_set=true）のときは値を返さずプレースホルダで示す（秘密は露出しない）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#cloud-key")).to_have_value("")
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "設定済み（変更する場合のみ入力）")


def test_admin_cloud_key_clear_button_sends_empty_string_after_confirm(page, web_base_url):
    """中央 API キーは書込専用欄のため、空のまま保存しても「未入力＝変更しない」として無視され
    クリアできない。専用の「キーを削除」操作を確認ダイアログ経由で実行したときだけ、その場で
    空文字を PUT する。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()

    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しました")
    assert records["admin_settings_put"][-1] == {"openai_api_key": ""}
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "未設定")


def test_admin_cloud_key_clear_preserves_other_unsaved_edits_and_dirty_dot(page, web_base_url):
    """キー削除の成功後にプロバイダタブ全体を保存済み値で再描画すると、同タブの他の未保存編集
    （個人キー許可トグル等）が無言破棄され、未保存丸印も消えてしまう。キー削除は実際に変わった
    キー欄の表示だけを更新し、他の未保存編集・丸印は保持されなければならない。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    system_settings["cloud"]["personal_api_keys_allowed"] = False
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    # プロバイダタブに未保存編集を作る（保存済み値 false から変更）。
    page.locator("#personal-keys-allowed").check()
    expect(page.locator("#tab-dot-provider")).to_be_visible()

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しました")

    # キー欄の表示は更新される。
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "未設定")
    # 他の未保存編集（個人キー許可トグル）は保存済み値で巻き戻されず残る。
    expect(page.locator("#personal-keys-allowed")).to_be_checked()
    # タブの未保存丸印も消えない。
    expect(page.locator("#tab-dot-provider")).to_be_visible()
    # このリクエストではキー削除以外は送られていない（無関係な項目を巻き込まない）。
    assert records["admin_settings_put"][-1] == {"openai_api_key": ""}


def test_admin_cloud_key_clear_updates_vlm_key_missing_warning(page, web_base_url):
    """中央 OpenAI キーを削除すると、視覚読み取り（VLM）のクラウド（OpenAI）向けキー未設定警告も
    追従する（キー有無に依存する他の案内が、プロバイダタブ全体は再描画しない削除操作のあとも
    古いままにならないことを固定する）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    system_settings["vlm"]["openai_key_present"] = True
    system_settings["vlm"]["effective"]["provider"] = "openai"
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    open_tab(page, "ingest")
    open_advanced(page, "tabpanel-ingest")   # VLM 設定は「詳細設定」折りたたみの中にある（開いたまま）
    expect(page.locator("#vlm-key-missing")).to_be_hidden()   # キー設定済み＝警告は出ない

    open_tab(page, "provider")
    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しました")
    assert records["admin_settings_put"][-1] == {"openai_api_key": ""}

    open_tab(page, "ingest")
    expect(page.locator("#vlm-key-missing")).to_be_visible()
    expect(page.locator("#vlm-key-missing")).to_contain_text("OPENAI_API_KEY")


def test_admin_cloud_key_clear_button_disabled_when_key_not_set(page, web_base_url):
    """削除できるキーが無い（未設定）ときは「キーを削除」ボタンが disabled になる
    （誤操作の確認ダイアログを無駄に出さない）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = False
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#cloud-key-clear")).to_be_disabled()


def test_admin_cloud_key_clear_button_disabled_immediately_after_clear(page, web_base_url):
    """キー設定済みでは有効、削除直後は再び disabled へ戻る。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    expect(page.locator("#cloud-key-clear")).to_be_enabled()

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しました")
    expect(page.locator("#cloud-key-clear")).to_be_disabled()


def test_admin_cloud_provider_switch_clears_previous_delete_result_text(page, web_base_url):
    """クラウドプロバイダを切り替えると、前のプロバイダに対する削除結果表示
    （「✓ 削除しました」等）が新しいプロバイダの結果に見えてしまわないようクリアされる。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しました")

    page.locator("input[data-cloud-provider='gemini']").check()
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")


def test_admin_cloud_key_clear_pending_response_does_not_clobber_switched_provider(page, web_base_url):
    """削除待ち中に別のプロバイダへ切り替えて未保存のキーを入力していると、遅れて届いた古い
    削除応答（元のプロバイダ向け）が切替先の入力・表示を上書きしてはいけない（要求時のプロバイダ・
    世代を捕捉し、応答時に不一致なら描画・文言更新を破棄する）。"""
    import json as _json

    from playwright.sync_api import expect
    import mock_api

    system_settings = _json.loads(_json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)

    held = {}

    def hold_admin_settings_put(route):
        if route.request.method != "PUT":
            route.fallback()
            return
        held["route"] = route

    page.route("**/admin/settings", hold_admin_settings_put)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()   # openai のキー削除を要求（応答は保留中）
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しています")

    # 応答が保留中に gemini へ切り替え、未保存のキーを入力する。
    page.locator("input[data-cloud-provider='gemini']").check()
    page.locator("#cloud-key").fill("AIza-unsaved-gemini-key")

    # 保留していた openai 向けの削除応答（成功）を解放する。
    held["route"].fulfill(status=200, content_type="application/json",
                          body=_json.dumps(system_settings, ensure_ascii=False))
    page.wait_for_timeout(200)

    # gemini 向けの未保存入力・ラベル表示は無傷のまま（openai 向けの応答で上書きされない）。
    expect(page.locator("#cloud-key")).to_have_value("AIza-unsaved-gemini-key")
    expect(page.locator("#cloud-key-label")).to_contain_text("Gemini")
    # 古い応答による誤った成功表示も出ない（gemini のキーが削除されたと誤解させない）。
    expect(page.locator("#cloud-key-clear-res")).not_to_contain_text("削除しました")


def test_admin_cloud_key_clear_roundtrip_switch_stale_response_does_not_corrupt(page, web_base_url):
    """openai→gemini→openai と往復してから遅れて届く削除応答は、世代が不一致（各切替が削除待ちの
    世代を進める）になった時点でもう「今の真実」を代表しない＝_view・表示のどちらにも一切反映
    しない（判定を先に行い、不一致なら丸ごと捨てる。応答の内容を無視して「削除された」
    という1点だけを常に反映する案は、この削除より後に同じ provider へ完了した別の保存の結果を
    巻き戻してしまうため採らない）。ここでは、この破棄が例外を起こさないこと、往復後に
    「削除しています...」等の残留表示が出ないことだけを確認する（往復のみ＝競合する保存が無い
    ケースでの最終表示の正しさは、次の実際の取得/保存まで保証しない）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)

    held = {}

    def hold_admin_settings_put(route):
        if route.request.method != "PUT":
            route.fallback()
            return
        held["route"] = route

    page.route("**/admin/settings", hold_admin_settings_put)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()   # openai の削除を要求（応答は保留中）
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しています")

    # 応答が届く前に gemini → openai と往復する（各切替が削除待ちの世代を進める）。
    page.locator("input[data-cloud-provider='gemini']").check()
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")
    page.locator("input[data-cloud-provider='openai']").check()
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")

    cleared = json.loads(json.dumps(system_settings))
    cleared["cloud"]["openai_key_set"] = False
    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps(cleared, ensure_ascii=False))
    page.wait_for_timeout(200)

    # 不一致で捨てられた＝「削除しています...」の残留も「✓ 削除しました」の誤表示も出ない。
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")


def test_admin_cloud_key_clear_pending_response_does_not_revert_newly_saved_key(page, web_base_url):
    """削除待ち中に同じプロバイダへ新しいキーを保存すると、後から届く
    古い削除応答（不一致）が、その新しいキーの「設定済み」表示を「未設定」へ巻き戻してはいけない。
    判定（世代・プロバイダ一致）を先に行い、不一致なら _view には一切触れない（応答の値を見て
    「削除された」という事実を無条件に信用する実装だと、この巻き戻りが起きる）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)

    held = {}

    def hold_first_put(route):
        if route.request.method != "PUT" or "route" in held:
            route.fallback()
            return
        held["route"] = route

    page.route("**/admin/settings", hold_first_put)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()   # openai の削除を要求（応答は保留中＝最初の PUT）
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しています")

    # 同じ openai へ新しいキーを入力して保存する（2件目の PUT・こちらは素通しで即応答）。
    page.locator("#cloud-key").fill("sk-new-key-after-clear-started")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")

    # 保留していた最初の削除応答を、後から解放する（不一致＝もう「今の真実」ではない）。応答の
    # 中身自体は「削除」という自分の操作の結果を正直に示す（openai_key_set=False）が、これは
    # 2件目の保存より前の状態を表す＝古い（不一致で捨てられるべき）ことを確認するのが狙い。
    stale_after_clear = json.loads(json.dumps(system_settings))
    stale_after_clear["cloud"]["openai_key_set"] = False
    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps(stale_after_clear, ensure_ascii=False))
    page.wait_for_timeout(200)

    # _view.cloud が古い応答で汚染されていないかは、保存直後の描画だけでは見えない（保存の
    # render() が既に正しい表示を出した後だから）。gemini → openai と切り替えて _view.cloud
    # から再描画させ、内部状態そのものが巻き戻っていないことを確認する。
    page.locator("input[data-cloud-provider='gemini']").check()
    page.locator("input[data-cloud-provider='openai']").check()

    # 新しく保存したキーの「設定済み」表示が、遅れて届いた削除応答で「未設定」へ戻っていないこと。
    expect(page.locator("#cloud-key-clear")).to_be_enabled()
    expect(page.locator("#cloud-key")).to_have_attribute(
        "placeholder", "設定済み（変更する場合のみ入力）")


def test_admin_cloud_key_input_after_clear_start_is_not_wiped_by_stale_response(page, web_base_url):
    """削除待ち中に（切替を挟まず）同じ欄へ新しいキーを入力し始めると、後から届く削除応答は
    もう「今の入力」を代表しない＝世代を進めて描画（`renderCloudKeyBlock` は入力欄を空へ戻す）を
    破棄する。入力した値は削除応答到着後も無傷のままであること。入力した時点で
    「削除しています...」の残留表示もニュートラルへ戻る（もう関係ない操作が進行中に見えない）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)

    held = {}

    def hold_admin_settings_put(route):
        if route.request.method != "PUT":
            route.fallback()
            return
        held["route"] = route

    page.route("**/admin/settings", hold_admin_settings_put)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しています")

    page.locator("#cloud-key").fill("sk-typed-after-clear-started")
    # 入力した時点で「削除しています...」の残留がニュートラルへ戻る（応答を待たずに）。
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")

    cleared = json.loads(json.dumps(system_settings))
    cleared["cloud"]["openai_key_set"] = False
    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps(cleared, ensure_ascii=False))
    page.wait_for_timeout(200)

    expect(page.locator("#cloud-key")).to_have_value("sk-typed-after-clear-started")
    expect(page.locator("#cloud-key-clear-res")).not_to_contain_text("削除しました")


def test_admin_cloud_key_clear_pending_message_cleared_after_save(page, web_base_url):
    """削除待ち中に（同じプロバイダで）保存すると、保存はその場で削除待ちの世代を
    無効化する（`_invalidateCloudKeyClear()`）ため、「削除しています...」の残留表示も保存完了を
    待たずニュートラルへ戻る。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings)

    held = {}

    def hold_first_put(route):
        if route.request.method != "PUT" or "route" in held:
            route.fallback()
            return
        held["route"] = route

    page.route("**/admin/settings", hold_first_put)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()   # 最初の PUT（削除）は保留のまま
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しています")

    page.locator("#cloud-key").fill("sk-saved-after-clear-started")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    # 保存が invalidate した時点で「削除しています...」の残留は消えている。
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")


def test_admin_cloud_key_clear_pending_message_cleared_by_other_tab_reset(page, web_base_url):
    """削除待ちの無効化はプロバイダタブのリセットに限らない。「使えるモデル」タブの
    「既定に戻す」でも削除世代を進める（残留する「削除しています...」がニュートラルへ戻る）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings_view = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings_view["cloud"]["openai_key_set"] = True
    install_api_mocks(page, system_settings=system_settings_view)

    held = {}

    def hold_first_put(route):
        if route.request.method != "PUT" or "route" in held:
            route.fallback()
            return
        held["route"] = route

    page.route("**/admin/settings", hold_first_put)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.once("dialog", lambda d: d.accept())
    page.locator("#cloud-key-clear").click()   # 最初の PUT（削除）は保留のまま
    expect(page.locator("#cloud-key-clear-res")).to_contain_text("削除しています")

    open_tab(page, "models")
    page.locator('[data-reset-tab="models"]').click()
    expect(page.locator("#tab-reset-res-models")).to_contain_text("既定に戻しました")

    open_tab(page, "provider")
    # 「使えるモデル」タブのリセットで削除世代が進んでいる＝残留表示は既にニュートラル。
    expect(page.locator("#cloud-key-clear-res")).to_have_text("")


def test_admin_cloud_key_clear_button_cancel_sends_nothing(page, web_base_url):
    """確認ダイアログを棄却すると PUT 自体が送られない（誤操作防止）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#cloud-key-clear").click()   # ダイアログはハンドラ未登録＝既定で自動棄却

    expect(page.locator("#cloud-key-clear-res")).to_have_text("")
    assert records["admin_settings_put"] == []
    expect(page.locator("#cloud-key")).to_have_attribute("placeholder", "設定済み（変更する場合のみ入力）")


def test_admin_cloud_key_empty_field_save_does_not_clear_key(page, web_base_url):
    """キー欄を空のまま（何も入力せず）保存しても、既存の中央キーは変更されない
    （書込専用欄の「未入力＝変更しない」契約は維持する・クリアは専用操作のみ）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["cloud"]["openai_key_set"] = True
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert "openai_api_key" not in records["admin_settings_put"][-1]


# ===== 外部連携（API キー）=====

def _ek_key_row(**overrides):
    row = {"id": 1, "key_prefix": "sk-ext-mock", "label": "既存キー", "created_by": "admin",
           "revoked_by": None, "allowed_worlds": None, "daily_quota": None, "owner_uid": None,
           "created_at": "2026-08-01T00:00:00+00:00", "revoked_at": None, "last_used_at": None,
           "expires_at": None, "call_count": 0}
    row.update(overrides)
    return row


def test_admin_ext_keys_card_renders_list(page, web_base_url):
    """一覧（ラベル・prefix・world スコープ・作成日・最終利用日時・呼び出し数・期限・状態）が描画される。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    ek_calls = {"list": 0}

    def handler(route):
        if route.request.method == "GET" and route.request.url.endswith("/ext/v1/admin/keys"):
            ek_calls["list"] += 1
            route.fulfill(status=200, content_type="application/json", body=json.dumps({"keys": [
                _ek_key_row(id=1, label="Dify連携", key_prefix="sk-ext-abc1", allowed_worlds=["test"],
                           call_count=42, last_used_at="2026-08-20T09:00:00+00:00"),
                _ek_key_row(id=2, label="失効済みキー", key_prefix="sk-ext-old1",
                           revoked_at="2026-08-10T00:00:00+00:00", revoked_by="admin"),
            ]}))
            return
        route.continue_()

    install_api_mocks(page, system_settings=system_settings)
    page.route("**/ext/v1/admin/keys", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    expect(page.locator("#ext-keys-list")).to_contain_text("Dify連携")
    expect(page.locator("#ext-keys-list")).to_contain_text("sk-ext-abc1")
    expect(page.locator("#ext-keys-list")).to_contain_text("test")
    expect(page.locator("#ext-keys-list")).to_contain_text("42")
    expect(page.locator("#ext-keys-list")).to_contain_text("失効済みキー")
    expect(page.locator("#ext-keys-list")).to_contain_text("失効済み")
    assert ek_calls["list"] >= 1


def test_admin_ext_key_issue_shows_plain_key_once(page, web_base_url):
    """発行フォーム送信→発行直後だけプレーンキーが表示される（コピー導線・再表示不可の注記）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    page.locator("#ext-key-issue-open").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")
    page.locator("#ek-label").fill("新しい連携キー")
    page.locator("#ek-quota").fill("100")
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-reveal")).to_be_visible()
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    expect(page.locator("#ek-issue-form")).to_be_hidden()
    assert records["ext_key_admin_create"][-1]["label"] == "新しい連携キー"
    assert records["ext_key_admin_create"][-1]["daily_quota"] == 100

    # 発行後は一覧に反映される（モーダルを閉じてから確認）。
    page.locator("#ek-modal-close").click()
    expect(page.locator("#ext-keys-list")).to_contain_text("新しい連携キー")
    # 閉じた後は平文が DOM に残らない（再表示不可の実質的な保証）。
    expect(page.locator("#ek-reveal-key")).to_have_text("")


def test_admin_ext_key_modal_cannot_close_while_issuing(page, web_base_url):
    """発行の応答待ち（issuing）の間は ✕・キャンセル・背景クリックのいずれでも閉じられない
    （応答後に必ずキーが1回は表示される・見せる前に閉じて有効キーだけ残る事故を防ぐ）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    # POST だけを保留にして「issuing」状態を作る（GET は初期表示の一覧取得で先に飛ぶため、
    # メソッドで区別しないと `times=1` がそちらを消費してしまう＝POST 以外は次のハンドラへ
    # `route.fallback()` で委譲する）。
    pending = {}

    def hold(route):
        if route.request.method != "POST":
            route.fallback()
            return
        pending["route"] = route
    page.route("**/ext/v1/admin/keys", hold)

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("保留中キー")
    page.locator("#ek-modal-submit").click()
    # submit ボタンは応答待ちの間 disabled になる（await の直前に同期的に立てるフラグ）ため、
    # これで「issuing」状態に入ったことを待つ（Playwright の自動リトライに乗る）。
    expect(page.locator("#ek-modal-submit")).to_be_disabled()

    # 応答が返るまでは閉じられない。
    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")
    page.locator("#ek-modal-cancel").click()
    expect(page.locator("#ek-overlay")).to_have_class("overlay open")

    # 応答が返ったら閉鎖可能になり、キーが表示される。
    import json as _json
    pending["route"].fulfill(status=200, content_type="application/json", body=_json.dumps(
        {"ok": True, "id": 99, "key": "sk-ext-heldresp", "key_prefix": "sk-ext-hel",
         "label": "保留中キー", "created_at": "2026-08-25T00:00:00+00:00",
         "allowed_worlds": None, "expires_at": None, "daily_quota": None}))
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-heldresp")
    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).not_to_have_class("overlay open")
    expect(page.locator("#ek-reveal-key")).to_have_text("")


def test_admin_ext_key_copy_failure_shows_error_not_success(page, web_base_url):
    """クリップボード API・execCommand の双方が失敗したら、成功表示を出さない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    # navigator.clipboard.writeText と execCommand の両方を失敗させる。
    page.evaluate("""() => {
      Object.defineProperty(navigator, 'clipboard', {
        value: { writeText: () => Promise.reject(new Error('denied')) }, configurable: true });
      document.execCommand = () => false;
    }""")

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("コピー失敗テスト")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-reveal")).to_be_visible()

    page.locator("#ek-copy").click()
    expect(page.locator("#ek-copy-res")).to_have_text("✗ コピーできませんでした（選択してコピーしてください）")


def test_admin_ext_key_revoke_asks_confirm_and_calls_delete(page, web_base_url):
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))

    def handler(route):
        if route.request.method == "GET" and route.request.url.endswith("/ext/v1/admin/keys"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"keys": [_ek_key_row(id=7, label="削除対象")]}))
            return
        route.continue_()

    records = install_api_mocks(page, system_settings=system_settings)
    page.route("**/ext/v1/admin/keys", handler)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    expect(page.locator("#ext-keys-list")).to_contain_text("削除対象")
    page.locator("[data-ek-revoke='7']").click()

    assert dialogs and "削除対象" in dialogs[0]
    assert records["ext_key_admin_revoke"] == [7]


def test_admin_ext_key_concurrent_close_reissue_survives_slow_list_refresh(page, web_base_url):
    """発行成功直後の一覧再取得（`loadExtKeys()`）が遅延している間に、次の発行が先に完了して
    より新しい一覧（両方のキーを含む）を描画した場合、後から届いた遅い（1本目しか知らない古い
    世代の）一覧応答が新しい描画を上書きしない（一覧 GET の世代番号ガード）。一覧の再取得は
    発行の成否判定から意図的に切り離されている（操作トークンで古いセッションの後始末が新しい
    セッションのボタン状態も壊さない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    get_calls = {"n": 0}
    held = {}

    def handler(route):
        if route.request.method == "GET":
            get_calls["n"] += 1
            if get_calls["n"] == 2:
                held["route"] = route   # 1本目発行成功直後の一覧取得（POST後のGET）だけ保留する
                return
        route.fallback()
    page.route("**/ext/v1/admin/keys", handler)
    page.goto(f"{web_base_url}/admin-settings.html")   # call #1（初期表示）は即応答
    open_tab(page, "extkeys")

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("1本目")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    page.locator("#ek-modal-close").click()
    # ここで call #2（1本目成功時の loadExtKeys()）が保留中のはず。

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("2本目")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    # call #3（2本目成功時の loadExtKeys()）は保留されないため先に届き、両方のキーを描画する。
    expect(page.locator("#ext-keys-list")).to_contain_text("2本目")
    expect(page.locator("#ext-keys-list")).to_contain_text("1本目")

    # 保留していた call #2（1本目しか知らない古い世代の応答）を今ここで解決する。世代番号ガード
    # により、既に描画済みのより新しい（両方を含む）一覧が古い応答で巻き戻されない。
    assert "route" in held
    held["route"].fulfill(status=200, content_type="application/json",
                          body=json.dumps({"keys": [_ek_key_row(id=1, label="1本目")]}))
    page.wait_for_timeout(50)
    expect(page.locator("#ext-keys-list")).to_contain_text("2本目")
    expect(page.locator("#ext-keys-list")).to_contain_text("1本目")
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    expect(page.locator("#ek-modal-submit")).to_be_hidden()
    expect(page.locator("#ek-issue-err")).to_have_text("")
    assert len(records["ext_key_admin_create"]) == 2


def test_admin_ext_key_issue_timeout_recovers_and_auto_revokes_orphan_key(page, web_base_url):
    """発行 POST が30秒応答しない（曖昧な失敗）場合、回復専用エンドポイント
    （`POST /ext/v1/admin/keys/recover`）へ `client_op_id` を渡して照合し、`found: true` を
    確認できたら（サーバー側で既に失効済みとして扱い）再発行を促す（issuing の永久ロックを
    解消する・一覧取得→DELETE の2段構成は使わない）。"""
    from playwright.sync_api import expect

    captured = {}

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys"):
            captured["body"] = json.loads(route.request.post_data)
            return   # 応答しない＝タイムアウトを誘発する
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys/recover"):
            req = json.loads(route.request.post_data)
            if req.get("client_op_id") == captured.get("body", {}).get("client_op_id"):
                route.fulfill(status=200, content_type="application/json", body=json.dumps(
                    {"found": True, "id": 55, "revoked_at": "2026-08-25T00:00:00+00:00"}))
                return
        route.fallback()

    install_api_mocks(page)
    page.route("**/ext/v1/admin/keys", handler)
    page.route("**/ext/v1/admin/keys/recover", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("孤児化候補")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-modal-submit")).to_be_disabled()

    page.clock.fast_forward(31000)
    expect(page.locator("#ek-issue-err")).to_contain_text("失効しました")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()
    # issuing の永久ロックが解消され、閉じる操作も再び効くようになる。
    page.locator("#ek-modal-close").click()
    expect(page.locator("#ek-overlay")).not_to_have_class("overlay open")


def test_admin_ext_key_issue_timeout_then_no_match_shows_failure_not_success(page, web_base_url):
    """回復専用エンドポイントが `found: false`（該当キーなし＝POST がサーバーに届いていな
    かった）を返し続けた場合は、失効に成功したかのような文言を出さず、失敗として表示する
    （失効を確認できた場合のみ成功文言を出す・曖昧なまま成功したように見せない）。有界リトライ
    （3回×2秒間隔）の全試行が not_found でも、いずれ失敗表示で確定してボタンが復帰することを
    確認する。"""
    from playwright.sync_api import expect

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys"):
            return   # 応答しない＝タイムアウトを誘発する
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys/recover"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"found": False, "id": None, "revoked_at": None}))
            return
        route.fallback()

    install_api_mocks(page)
    page.route("**/ext/v1/admin/keys", handler)
    page.route("**/ext/v1/admin/keys/recover", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("届いていない候補")
    page.locator("#ek-modal-submit").click()
    page.clock.fast_forward(31000)
    # 3回×2秒の有界リトライ（found:false のたび2秒待つ）も仮想時間で進める。
    page.clock.fast_forward(2000)
    page.clock.fast_forward(2000)

    expect(page.locator("#ek-issue-err")).to_contain_text("失敗した可能性")
    expect(page.locator("#ek-issue-err")).not_to_contain_text("失効しました")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_admin_ext_key_modal_inert_blocks_background_keyboard_interaction(page, web_base_url):
    """モーダルが開いている間、背後（`.wrap`）は `inert` になりキーボード（Tab）操作でも
    背後へフォーカスが移らない（クリック・キーボードのどちらでも背後を叩けない）。
    `aria-modal="true"` も宣言されている。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

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


def test_admin_ext_key_issue_502_with_html_body_treated_as_ambiguous(page, web_base_url):
    """非2xx応答でも本文が妥当な JSON でなければ（例: リバースプロキシが返す 502 の HTML 本文）、
    確定的な拒否ではなく曖昧な結果として回復導線へ回す（サーバーの処理が実際には完了していた
    可能性を排除できないため・ステータス判定より本文の解析可否を先に見る）。"""
    from playwright.sync_api import expect

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys"):
            route.fulfill(status=502, content_type="text/html",
                          body="<html><body>Bad Gateway</body></html>")
            return
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys/recover"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"found": False, "id": None, "revoked_at": None}))
            return
        route.fallback()

    install_api_mocks(page)
    page.route("**/ext/v1/admin/keys", handler)
    page.route("**/ext/v1/admin/keys/recover", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("502候補")
    page.locator("#ek-modal-submit").click()
    page.clock.fast_forward(2000)
    page.clock.fast_forward(2000)

    expect(page.locator("#ek-issue-err")).to_contain_text("失敗した可能性")
    expect(page.locator("#ek-issue-err")).not_to_contain_text("エラー (502)")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_admin_ext_key_issue_valid_json_4xx_is_confirmed_error_not_ambiguous(page, web_base_url):
    """妥当な JSON を持つ非2xx応答（自分のアプリが明示的に拒否した）は曖昧な結果として扱わず、
    確定的なエラーとしてそのまま表示する（回復導線には入らない・「確認しています…」を経由しない）。"""
    from playwright.sync_api import expect

    def handler(route):
        if route.request.method == "POST" and route.request.url.endswith("/ext/v1/admin/keys"):
            route.fulfill(status=422, content_type="application/json",
                          body=json.dumps({"detail": "許可されていない world です"}))
            return
        route.fallback()

    install_api_mocks(page)
    page.route("**/ext/v1/admin/keys", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("422候補")
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-issue-err")).to_have_text("許可されていない world です")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_admin_ext_key_issue_body_stall_after_headers_treated_as_ambiguous(page, web_base_url):
    """本文の読み取りだけが詰まる（ヘッダは正常に届く）場合も、締切（`fetch()`＋`json()` 全体）が
    効いて曖昧な結果として扱われる（`fetch()` 単体の `AbortController` だけでは取りこぼすケース・
    `Response.prototype.json` を差し替えて本文ストリームが詰まった状態を JS レベルで確定的に
    再現する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    page.evaluate("""() => {
      const orig = Response.prototype.json;
      Response.prototype.json = function () {
        if (this.url && this.url.includes('/ext/v1/admin/keys') && !this.url.includes('recover')) {
          return new Promise(() => {});
        }
        return orig.call(this);
      };
    }""")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("stall候補")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-modal-submit")).to_be_disabled()

    page.clock.fast_forward(31000)
    expect(page.locator("#ek-issue-err")).to_contain_text("失効しました")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_admin_ext_key_issue_network_disconnect_treated_as_ambiguous(page, web_base_url):
    """fetch 自体が失敗する通信断（`route.abort()`）も曖昧な結果として扱い、回復導線へ入る。"""
    from playwright.sync_api import expect

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys"):
            route.abort("connectionreset")
            return
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys/recover"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"found": False, "id": None, "revoked_at": None}))
            return
        route.fallback()

    install_api_mocks(page)
    page.route("**/ext/v1/admin/keys", handler)
    page.route("**/ext/v1/admin/keys/recover", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    page.clock.install()

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("通信断候補")
    page.locator("#ek-modal-submit").click()
    page.clock.fast_forward(2000)
    page.clock.fast_forward(2000)

    expect(page.locator("#ek-issue-err")).to_contain_text("失敗した可能性")
    expect(page.locator("#ek-modal-submit")).to_be_enabled()


def test_admin_ext_key_recover_malformed_found_type_retries_then_fails(page, web_base_url):
    """回復応答の `found` が true/false のどちらでもない（型崩れ）場合は不正応答として扱い、
    確認できた（found:true）扱いにも確定的な not_found 扱いにもしない——有界リトライの末に
    最終的な失敗表示（確認できなかった旨）になる。"""
    from playwright.sync_api import expect

    def handler(route):
        url = route.request.url
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys"):
            return   # 応答しない＝タイムアウトを誘発する
        if route.request.method == "POST" and url.endswith("/ext/v1/admin/keys/recover"):
            route.fulfill(status=200, content_type="application/json",
                          body=json.dumps({"found": "yes"}))   # 型崩れ（真偽値ではない）
            return
        route.fallback()

    install_api_mocks(page)
    page.route("**/ext/v1/admin/keys", handler)
    page.route("**/ext/v1/admin/keys/recover", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
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


def test_admin_ext_key_modal_open_and_submit_direct_reentry_blocked_during_issuing(page, web_base_url):
    """issuing 中に `openExtKeyModal()`/`submitExtKeyIssue()` を直接（ボタンの disabled 状態を
    経由せず）呼び出しても、二重の POST は飛ばない（内部ガードがボタンの見た目の状態に依存
    しないことを確認する）。"""
    from playwright.sync_api import expect

    post_count = {"n": 0}
    pending = {}

    def handler(route):
        if route.request.method == "POST" and route.request.url.endswith("/ext/v1/admin/keys"):
            post_count["n"] += 1
            pending["route"] = route
            return
        route.fallback()

    install_api_mocks(page)
    page.route("**/ext/v1/admin/keys", handler)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    page.locator("#ext-key-issue-open").click()
    page.locator("#ek-label").fill("再入テスト")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    assert post_count["n"] == 1

    page.evaluate("openExtKeyModal()")
    page.evaluate("submitExtKeyIssue()")
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    assert post_count["n"] == 1, "issuing 中の直接再入で2本目の POST が飛んだ"

    assert "route" in pending
    pending["route"].fulfill(status=200, content_type="application/json", body=json.dumps(
        {"ok": True, "id": 1, "key": "sk-ext-mockreentry", "key_prefix": "sk-ext-mockre",
         "label": "再入テスト", "created_at": "2026-08-25T00:00:00+00:00",
         "allowed_worlds": None, "expires_at": None, "daily_quota": None}))
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mockreentry")
    assert post_count["n"] == 1


def test_admin_ctrl_s_during_key_modal_does_not_save(page, web_base_url):
    """API キー発行モーダルが開いている間（idle・issuing・キー表示中のいずれの状態でも）
    Ctrl/Cmd+S を押しても `PUT /admin/settings` は発生しない（既定動作の抑止だけ行う）。
    モーダルを閉じれば通常どおり保存できる。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    # idle 状態（フォーム入力中）。
    page.locator("#ext-key-issue-open").click()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["admin_settings_put"]) == 0

    # issuing 状態（応答待ち・POST を保留する）。
    pending = {}

    def hold(route):
        if route.request.method != "POST":
            route.fallback()
            return
        pending["route"] = route
    page.route("**/ext/v1/admin/keys", hold)
    page.locator("#ek-label").fill("ctrls候補")
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-modal-submit")).to_be_disabled()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["admin_settings_put"]) == 0

    # revealed 状態（キー表示中）。
    pending["route"].fulfill(status=200, content_type="application/json", body=json.dumps(
        {"ok": True, "id": 1, "key": "sk-ext-mockctrls", "key_prefix": "sk-ext-mockct",
         "label": "ctrls候補", "created_at": "2026-08-25T00:00:00+00:00",
         "allowed_worlds": None, "expires_at": None, "daily_quota": None}))
    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mockctrls")
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["admin_settings_put"]) == 0

    # モーダルを閉じれば通常どおり Ctrl+S で保存できる。
    page.locator("#ek-modal-close").click()
    page.keyboard.press("Control+s")
    page.wait_for_timeout(50)
    assert len(records["admin_settings_put"]) == 1


@pytest.mark.parametrize("prefix,is_self", [
    ("/ext/v1/admin/keys", False),
    ("/ext/v1/keys", True),
], ids=["admin", "self"])
def test_ext_key_mock_case_insensitive_full_contract(page, web_base_url, prefix, is_self):
    """モックの `client_op_id` 大小文字非区別契約を、一連の流れで直接 assert する（admin・
    本人をパラメータ化）: (1) 大文字で発行→応答は正準小文字形、(2) 大小文字だけ変えて再発行
    →409（同じ UUID とみなす）、(3) 発行時と異なる大小文字表記で回復を照会→一致する。"""
    import mock_api

    if is_self:
        settings = {**mock_api.SETTINGS_RESP, "user_api_keys_allowed": True}
        install_api_mocks(page, settings=settings)
        page.goto(f"{web_base_url}/settings.html")
    else:
        install_api_mocks(page)
        page.goto(f"{web_base_url}/admin-settings.html")
        open_tab(page, "extkeys")

    result = page.evaluate("""async (prefix) => {
      const post = (body) => fetch(prefix, {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      }).then(async (r) => ({ status: r.status, body: await r.json() }));

      const opId = crypto.randomUUID();
      const created = await post({ label: 'case-full', client_op_id: opId.toUpperCase() });
      const reissue = await post({ label: 'case-full-2', client_op_id: opId.toLowerCase() });
      const recovered = await fetch(prefix + '/recover', {
        method: 'POST', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ client_op_id: opId.toUpperCase() }),
      }).then((r) => r.json());

      return { opId, created, reissue, recovered };
    }""", prefix)

    assert result["created"]["status"] == 200, result["created"]
    assert result["created"]["body"]["client_op_id"] == result["opId"].lower()
    assert result["reissue"]["status"] == 409, result["reissue"]
    assert result["recovered"]["found"] is True
    assert result["recovered"]["id"] == result["created"]["body"]["id"]


def test_admin_ext_key_focus_moves_to_copy_on_success_and_back_to_opener_on_close(page, web_base_url):
    """発行成功時はコピー操作へフォーカスが移り、モーダルを閉じると開く前にフォーカスがあった
    要素（発行ボタン）へ復帰する（`to_be_focused` で厳密に確認する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    open_btn = page.locator("#ext-key-issue-open")
    open_btn.click()
    page.locator("#ek-label").fill("フォーカステスト")
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-copy")).to_be_focused()

    page.locator("#ek-modal-close").click()
    expect(open_btn).to_be_focused()


def test_admin_ctrl_s_during_key_modal_is_cancelable_and_default_prevented(page, web_base_url):
    """モーダルが開いている間の Ctrl+S は、save() を呼ばないだけでなく実際に
    `preventDefault()` されている（cancelable な KeyboardEvent の `defaultPrevented` を確認）。"""
    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")
    page.locator("#ext-key-issue-open").click()

    default_prevented = page.evaluate("""() => {
      const ev = new KeyboardEvent('keydown', {
        key: 's', ctrlKey: true, cancelable: true, bubbles: true });
      document.dispatchEvent(ev);
      return ev.defaultPrevented;
    }""")
    assert default_prevented is True


def test_admin_ext_key_expires_date_is_inclusive_and_min_blocks_past(page, web_base_url):
    """発行フォームの日付は「選択日を含めて有効」＝翌日 0 時（ローカル=JST）に失効させる形へ
    変換して送信する。`min` 属性は当日日付（日付ピッカー経由の過去日選択を防ぐ）。手入力・
    貼り付けで過去日を直接セットした場合（`min` が効かない経路）も、送信前のクライアント側
    チェックが POST 自体を発生させない（サーバ側422はあくまで最後の砦）。日付は実行日からの
    相対計算（今日基準の未来日・過去日）で導出し、ハードコードした固定日に依存しない
    （固定日はやがて過去日になり試験の前提が崩れる）。"""
    from datetime import datetime, timedelta, timezone

    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    # conftest が context の timezone_id="Asia/Tokyo" を固定している（ブラウザ側は常に JST）。
    # テストプロセス（Python）側の「今日」もホスト OS の実 TZ に関わらず UTC+9 で明示計算し、
    # ブラウザ側の基準とズレないようにする（ホストが UTC 等だと date.today() は一致しない）。
    today = (datetime.now(timezone.utc) + timedelta(hours=9)).date()
    future = today + timedelta(days=7)
    past = today - timedelta(days=1)

    page.locator("#ext-key-issue-open").click()
    min_attr = page.locator("#ek-expires").get_attribute("min")
    assert min_attr == today.isoformat()

    page.locator("#ek-label").fill("期限つきキー")
    page.locator("#ek-expires").fill(past.isoformat())
    before_creates = len(records["ext_key_admin_create"])
    page.locator("#ek-modal-submit").click()
    expect(page.locator("#ek-issue-err")).to_contain_text("今日以降")
    assert len(records["ext_key_admin_create"]) == before_creates   # POST 自体が発生していない

    page.locator("#ek-expires").fill(future.isoformat())
    page.locator("#ek-modal-submit").click()

    expect(page.locator("#ek-reveal-key")).to_contain_text("sk-ext-mock")
    sent = records["ext_key_admin_create"][-1]
    # 選択日を含めて有効＝翌日0時（JST）に失効＝UTC では選択日の15:00。
    assert sent["expires_at"] == f"{future.isoformat()}T15:00:00.000Z"


def test_admin_user_api_keys_toggle_off_confirms_with_count_and_cancel_aborts(page, web_base_url):
    """user_api_keys_allowed を ON→OFF で保存すると、利用者発行キーの有効件数を示す確認ダイアログが
    出る（A6 の personal_api_keys_allowed と同型）。キャンセルすると保存全体を中断する。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["ext_keys"]["user_api_keys_allowed"] = True
    system_settings["ext_keys"]["self_issued_active_count"] = 4
    records = install_api_mocks(page, system_settings=system_settings)
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.dismiss()))
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    expect(page.locator("#ext-keys-user-allowed")).to_be_checked()
    page.locator("#ext-keys-user-allowed").uncheck()
    page.locator("#save").click()

    assert dialogs and "4 件" in dialogs[0] and "失効" in dialogs[0]
    expect(page.locator("#msg")).to_contain_text("保存を取り消しました")
    assert records["admin_settings_put"] == []


def test_admin_user_api_keys_toggle_on_save_sends_flag(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    expect(page.locator("#ext-keys-user-allowed")).not_to_be_checked()
    page.locator("#ext-keys-user-allowed").check()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1]["user_api_keys_allowed"] is True


def test_admin_user_api_keys_quota_default_save_and_clear(page, web_base_url):
    """利用者キーの1日あたり呼び出し上限（既定/上限）を設定・クリアできる。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["ext_keys"]["daily_quota_default"] = {"configured": 50, "effective": 50}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "extkeys")

    expect(page.locator("#ext-keys-user-quota-default")).to_have_value("50")
    expect(page.locator("#ext-keys-user-quota-default-hint")).to_contain_text("この値で固定中")

    page.locator("#ext-keys-user-quota-default").fill("30")
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1]["user_api_keys_daily_quota_default"] == 30


# ===== SET-2c: OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換） =====

def test_admin_openai_endpoint_renders_default_and_hides_detail_fields(page, web_base_url):
    """既定モック（kind=openai・未設定）は「OpenAI 本家」が選択済みで、詳細欄（base URL 等）は
    隠れている。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("input[data-openai-endpoint-kind='openai']")).to_be_checked()
    expect(page.locator("#openai-endpoint-fields")).to_be_hidden()


def test_admin_openai_endpoint_switch_to_azure_reveals_fields_and_saves(page, web_base_url):
    """「Azure OpenAI」を選ぶと詳細欄が現れ、入力して保存すると4フィールドが PUT body に含まれる。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    expect(page.locator("#openai-endpoint-fields")).to_be_visible()
    open_advanced(page, "tabpanel-provider")   # 認証ヘッダ形式・API バージョンは「詳細」の中（設計要件④）

    page.locator("#openai-endpoint-base-url").fill("https://myres.openai.azure.com/openai/v1")
    page.locator("#openai-endpoint-auth-header").select_option("api-key")
    page.locator("#openai-endpoint-api-version").fill("2026-05-01-preview")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["openai_endpoint_kind"] == "azure"
    assert put["openai_base_url"] == "https://myres.openai.azure.com/openai/v1"
    assert put["openai_auth_header"] == "api-key"
    assert put["openai_api_version"] == "2026-05-01-preview"


def test_admin_openai_endpoint_switch_back_to_openai_preserves_detail_fields(page, web_base_url):
    """Azure 等から「OpenAI 本家」へ戻して保存しても、base URL 等の詳細欄は
    null 化しない（PUT body には含めず現在の保存値をそのまま維持する）。`llm.py` は
    kind=openai の間これらの値を常に無視する契約なので、null 化は不要かつ custom→openai→custom
    の往復で値を失う原因になっていた（保存だけ送る＝kind のみ）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"] = {
        "kind": "azure", "base_url": "https://myres.openai.azure.com/openai/v1",
        "auth_header": "bearer", "api_version": None}
    system_settings["openai_endpoint"]["effective"] = {
        "kind": "azure", "base_url": "https://myres.openai.azure.com/openai/v1",
        "auth_header": "bearer", "api_version": ""}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("input[data-openai-endpoint-kind='azure']")).to_be_checked()
    expect(page.locator("#openai-endpoint-base-url")).to_have_value(
        "https://myres.openai.azure.com/openai/v1")

    page.locator("input[data-openai-endpoint-kind='openai']").check()
    expect(page.locator("#openai-endpoint-fields")).to_be_hidden()
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["openai_endpoint_kind"] == "openai"
    assert "openai_base_url" not in put
    assert "openai_auth_header" not in put
    assert "openai_api_version" not in put


def test_admin_openai_endpoint_auth_header_alone_change_is_saved(page, web_base_url):
    """認証ヘッダ形式・API バージョンは「詳細」折りたたみの中にあり `#openai-endpoint-fields`
    の外に置かれている。この2項目「だけ」を変えても保存される（監視は DOM の親子関係でなく
    値そのもので判定する）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"] = {
        "kind": "azure", "base_url": "https://myres.openai.azure.com/openai/v1",
        "auth_header": "bearer", "api_version": None}
    system_settings["openai_endpoint"]["effective"] = {
        "kind": "azure", "base_url": "https://myres.openai.azure.com/openai/v1",
        "auth_header": "bearer", "api_version": ""}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_advanced(page, "tabpanel-provider")

    page.locator("#openai-endpoint-auth-header").select_option("api-key")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["openai_endpoint_kind"] == "azure"
    assert put["openai_auth_header"] == "api-key"
    assert put["openai_base_url"] == "https://myres.openai.azure.com/openai/v1"


def test_admin_openai_endpoint_api_version_alone_change_is_saved(page, web_base_url):
    """上と対称: API バージョンだけを変えても保存される。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"] = {
        "kind": "azure", "base_url": "https://myres.openai.azure.com/openai/v1",
        "auth_header": "bearer", "api_version": None}
    system_settings["openai_endpoint"]["effective"] = {
        "kind": "azure", "base_url": "https://myres.openai.azure.com/openai/v1",
        "auth_header": "bearer", "api_version": ""}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_advanced(page, "tabpanel-provider")

    page.locator("#openai-endpoint-api-version").fill("2026-06-01-preview")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["openai_endpoint_kind"] == "azure"
    assert put["openai_api_version"] == "2026-06-01-preview"


def test_admin_openai_endpoint_untouched_omits_all_fields(page, web_base_url):
    """接続先欄を一切触らずに保存すると、4フィールドとも PUT body に含まれない（ピン留め回避）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    for key in ("openai_endpoint_kind", "openai_base_url", "openai_auth_header", "openai_api_version"):
        assert key not in put


def test_admin_openai_endpoint_embed_deployment_updates_model_catalog(page, web_base_url):
    """「埋め込みのデプロイ名」欄（本家以外選択時のみ表示）は model_catalog（openai/embed）へ
    直接反映される（別の system_settings キーは作らない・唯一の真実源）。接続先ラジオ自体は
    触っていないため `openai_endpoint_kind` は PUT body に含まれない（別経路の独立性）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"]["kind"] = "azure"
    system_settings["openai_endpoint"]["effective"]["kind"] = "azure"
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#openai-endpoint-fields")).to_be_visible()
    page.locator("#openai-endpoint-embed-deployment").fill("my-embed-deployment")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert "openai_endpoint_kind" not in put   # 接続先ラジオ自体は触っていない
    cell = put["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "my-embed-deployment"
    assert "my-embed-deployment" in cell["allowed"]


def test_admin_embed_deployment_change_via_catalog_tab_syncs_to_provider_field(page, web_base_url):
    """「使えるモデル」タブで openai/embed の既定を変えると、「プロバイダ＋接続先」タブに
    残っている埋め込みデプロイ名欄の表示もその場で同期する（同じ `model_catalog.openai.embed` の
    二重の編集面が食い違わない・唯一の状態源）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"]["kind"] = "azure"
    system_settings["openai_endpoint"]["effective"]["kind"] = "azure"
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    open_tab(page, "models")
    sel = page.locator("select.mc-default[data-provider='openai'][data-usage='embed']")
    sel.select_option("text-embedding-3-large")

    open_tab(page, "provider")
    expect(page.locator("#openai-endpoint-embed-deployment")).to_have_value("text-embedding-3-large")


def test_admin_embed_deployment_last_touched_field_wins_on_save(page, web_base_url):
    """「使えるモデル」タブでカタログの既定を変えたあと、「プロバイダ＋接続先」タブの
    埋め込みデプロイ名欄を別の値へ直接編集すると、後から触った方（欄への直接編集）が保存される
    （古い方の入力欄の値が保存時に上書きし直す実害を防ぐ）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"]["kind"] = "azure"
    system_settings["openai_endpoint"]["effective"]["kind"] = "azure"
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    open_tab(page, "models")
    page.locator("select.mc-default[data-provider='openai'][data-usage='embed']") \
        .select_option("text-embedding-3-large")

    open_tab(page, "provider")
    page.locator("#openai-endpoint-embed-deployment").fill("later-typed-deployment")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    cell = records["admin_settings_put"][-1]["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "later-typed-deployment"


def test_admin_embed_deployment_change_via_catalog_only_saves_without_endpoint_kind(page, web_base_url):
    """使えるモデルタブだけで openai/embed の既定を変えて保存すると、その変更が PUT body の
    model_catalog に載る一方、接続先（openai_endpoint_kind 等）には一切触れていないため
    含まれない（catalog 側だけの変更が正しく検知・送信されることを固定する）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    page.locator("select.mc-default[data-provider='openai'][data-usage='embed']") \
        .select_option("text-embedding-3-large")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]
    assert put["model_catalog"]["openai"]["embed"]["default"] == "text-embedding-3-large"
    assert "openai_endpoint_kind" not in put


def test_admin_embed_deployment_last_touched_field_wins_reverse_order(page, web_base_url):
    """上（カタログ→欄）と逆順: 「プロバイダ＋接続先」タブの欄を先に編集し、後から「使えるモデル」
    タブのカタログで既定を変えると、後から触った方（カタログ側）が保存される。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"]["kind"] = "azure"
    system_settings["openai_endpoint"]["effective"]["kind"] = "azure"
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#openai-endpoint-embed-deployment").fill("earlier-typed-deployment")

    open_tab(page, "models")
    page.locator("select.mc-default[data-provider='openai'][data-usage='embed']") \
        .select_option("text-embedding-3-large")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    cell = records["admin_settings_put"][-1]["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "text-embedding-3-large"


def test_admin_embed_deployment_keystroke_by_keystroke_typing_does_not_accumulate_partial_values(
        page, web_base_url):
    """1文字ずつの逐次入力（'input' イベントの連続発火）では allowed 一覧へ確定していない
    途中の値（'d'・'de'・'dep'…）を反映しない。確定時（blur/change）に最終値1つだけを反映する。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"]["kind"] = "azure"
    system_settings["openai_endpoint"]["effective"]["kind"] = "azure"
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    field = page.locator("#openai-endpoint-embed-deployment")
    field.fill("")   # 既定値が入っているため、まず空にしてから逐次入力する
    field.press_sequentially("deploy-x")   # 1文字ずつ 'input' を発火させる（確定はまだしない）
    page.locator("#openai-endpoint-base-url").click()   # blur＝確定（'change' 発火）
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    cell = records["admin_settings_put"][-1]["model_catalog"]["openai"]["embed"]
    assert cell["default"] == "deploy-x"
    assert cell["allowed"].count("deploy-x") == 1   # 途中の 'd'・'de'・'dep'… は allowed に無い
    for partial in ("d", "de", "dep", "depl", "deplo", "deploy", "deploy-"):
        assert partial not in cell["allowed"]


def test_admin_embed_deployment_single_change_lights_up_dot_immediately(page, web_base_url):
    """埋め込み欄を確定編集した（'change'＝blur）その1イベントで、次のイベントを待たずに
    その場でプロバイダタブの未保存丸印が付く（状態更新は同じイベント内で dirty 判定より
    先に済んでいる必要がある）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"]["kind"] = "azure"
    system_settings["openai_endpoint"]["effective"]["kind"] = "azure"
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#tab-dot-provider")).to_be_hidden()
    field = page.locator("#openai-endpoint-embed-deployment")
    field.fill("one-shot-deployment")
    # Tab で確定させる（クリックで他要素へ移ると、その click イベント自体が改めて
    # refreshTabDots を呼んでしまい、'change' 単体で丸印が付いたかを判別できなくなる。
    # Tab はフォーカス移動のみで別イベントを発生させないため、この1回の 'change' だけで
    # 丸印が付くかを厳密に確認できる）。
    field.press("Tab")
    expect(page.locator("#tab-dot-provider")).to_be_visible()


def test_admin_settings_provider_tab_reset_does_not_leak_unsaved_models_tab_draft(page, web_base_url):
    """「使えるモデル」タブで未保存のまま他セル（openai/chat）を変更しても、プロバイダ
    タブのリセットはその draft を PUT body へ同送しない（暗黙保存しない）。何も保存済みの
    カタログ設定が無い状態では model_catalog は null のまま送られる。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)   # model_catalog.configured は既定で None（未設定）
    page.goto(f"{web_base_url}/admin-settings.html")

    open_tab(page, "models")
    page.locator("select.mc-default[data-provider='openai'][data-usage='chat']") \
        .select_option("gpt-5.4-mini")   # 保存しない未保存編集

    open_tab(page, "provider")
    page.locator('[data-reset-tab="provider"]').click()

    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["model_catalog"] is None   # chat の未保存編集が紛れ込んでいない


def test_admin_settings_models_tab_reset_does_not_leak_unsaved_provider_tab_embed_draft(page, web_base_url):
    """プロバイダタブで未保存のまま埋め込みデプロイ名を編集しても、使えるモデルタブの
    リセットはその draft を PUT body へ同送しない（対象キーのみ・null で送る）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["openai_endpoint"]["configured"]["kind"] = "azure"
    system_settings["openai_endpoint"]["effective"]["kind"] = "azure"
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#openai-endpoint-embed-deployment").fill("unsaved-provider-draft")

    open_tab(page, "models")
    page.locator('[data-reset-tab="models"]').click()

    expect(page.locator("#tab-reset-res-models")).to_contain_text("既定に戻しました")
    put = records["admin_settings_put"][-1]
    assert put["model_catalog"] is None   # プロバイダタブの未保存 draft が紛れ込んでいない


def test_admin_settings_provider_tab_reset_clears_embed_and_preserves_other_saved_cells(page, web_base_url):
    """プロバイダタブのリセットは、保存済みの他セル（openai/chat）はそのまま残し、
    openai/embed だけを既定へ戻す（configured から embed キーだけを取り除いて送る）。
    リセット後は埋め込み欄の表示も実際に組み込み既定へ戻る（サーバで実際に既定へ戻ったことの確認）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["model_catalog"]["configured"] = {
        "openai": {
            "chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.4-mini"},
            "embed": {"allowed": ["text-embedding-3-small", "custom-embed-deployment"],
                      "default": "custom-embed-deployment"},
        },
    }
    system_settings["model_catalog"]["effective"]["openai"]["chat"]["default"] = "gpt-5.4-mini"
    system_settings["model_catalog"]["effective"]["openai"]["embed"] = {
        "allowed": ["text-embedding-3-small", "custom-embed-deployment"],
        "default": "custom-embed-deployment"}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    expect(page.locator("#openai-endpoint-embed-deployment")).to_have_value("custom-embed-deployment")

    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")

    put = records["admin_settings_put"][-1]
    assert put["model_catalog"] == {
        "openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.4-mini"}}}
    # サーバで実際に組み込み既定へ戻ったことが表示にも反映される。
    expect(page.locator("#openai-endpoint-embed-deployment")).to_have_value("text-embedding-3-small")


def test_admin_settings_normal_save_after_reset_does_not_refix_cleared_or_untouched_cells(page, web_base_url):
    """プロバイダタブのリセットで openai/embed を未設定へ戻した後、使えるモデルタブで別セルを
    編集して通常保存しても、その通常保存が model_catalog を丸ごと送るせいで（全置換の契約）
    リセットした embed や触っていないセルまで組み込み既定の値で明示固定されてはいけない。
    保存される model_catalog には、今回実際に編集したセルだけが載る。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["model_catalog"]["configured"] = {
        "openai": {"embed": {"allowed": ["text-embedding-3-small", "custom-embed-deployment"],
                             "default": "custom-embed-deployment"}}}
    system_settings["model_catalog"]["effective"]["openai"]["embed"] = {
        "allowed": ["text-embedding-3-small", "custom-embed-deployment"],
        "default": "custom-embed-deployment"}
    # ollama/chat の一覧に候補を1つ足す（既定は組み込みのまま＝未編集時は差分なし）。
    system_settings["model_catalog"]["effective"]["ollama"]["chat"] = {
        "allowed": ["qwen2.5", "llama3.1"], "default": "qwen2.5"}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
    assert records["admin_settings_put"][-1]["model_catalog"] is None   # リセット自体は正しく null

    open_tab(page, "models")
    page.locator("select.mc-default[data-provider='ollama'][data-usage='chat']") \
        .select_option("llama3.1")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]["model_catalog"]
    assert put == {"ollama": {"chat": {"allowed": ["qwen2.5", "llama3.1"], "default": "llama3.1"}}}
    assert "openai" not in put   # リセットした embed も、触っていない openai の他セルも含まれない


def test_admin_settings_save_reverting_cell_to_builtin_default_omits_it(page, web_base_url):
    """保存済みで組み込み既定と異なっていたセル（openai/chat）を、組み込み既定と同じ値へ選び直して
    保存すると、そのセルは PUT body の model_catalog から落ちる（明示設定を作らない＝組み込み既定
    への追従に戻る）。同時に編集した別のセル（ollama/chat）は引き続き含まれる。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["model_catalog"]["configured"] = {
        "openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.4-mini"}}}
    system_settings["model_catalog"]["effective"]["openai"]["chat"]["default"] = "gpt-5.4-mini"
    # ollama/chat の一覧に候補を1つ足す（既定は組み込みのまま＝未編集時は差分なし）。
    system_settings["model_catalog"]["effective"]["ollama"]["chat"] = {
        "allowed": ["qwen2.5", "llama3.1"], "default": "qwen2.5"}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    # openai/chat を組み込み既定（gpt-5.5）へ選び直す＝保存済みのカスタム値から既定へ戻す。
    page.locator("select.mc-default[data-provider='openai'][data-usage='chat']") \
        .select_option("gpt-5.5")
    # 別セルは新たにカスタム値へ変える（こちらは含まれるべき）。
    page.locator("select.mc-default[data-provider='ollama'][data-usage='chat']") \
        .select_option("llama3.1")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]["model_catalog"]
    assert "openai" not in put   # 既定と同値へ戻したセルは含まれない
    assert put["ollama"]["chat"]["default"] == "llama3.1"   # 実際に変えたセルは含まれる


def test_admin_settings_save_preserves_untouched_cell_saved_at_builtin_default_value(page, web_base_url):
    """保存済みで組み込み既定と**同値**のセル（openai/chat＝"gpt-5.5"）を明示保存していた場合、
    そのセルを一切編集せずに別セル（ollama/chat）だけを編集して保存しても、openai/chat は
    PUT body の model_catalog から落ちない（明示固定した事実・provenance を保つ＝将来の組み込み
    既定変更に管理者操作なしで追従してしまわないようにする）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    # openai/chat は既定と同じ値（gpt-5.5）で明示保存済み（allowed に候補を1つ増やしただけ、
    # という状況を模す＝値そのものは組み込み既定と一致する）。
    system_settings["model_catalog"]["configured"] = {
        "openai": {"chat": {"allowed": ["gpt-5.5", "gpt-5.4-mini"], "default": "gpt-5.5"}}}
    # ollama/chat の一覧に候補を1つ足す（既定は組み込みのまま＝未編集時は差分なし）。
    system_settings["model_catalog"]["effective"]["ollama"]["chat"] = {
        "allowed": ["qwen2.5", "llama3.1"], "default": "qwen2.5"}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "models")

    # openai/chat には一切触れない。別セルだけを編集する。
    page.locator("select.mc-default[data-provider='ollama'][data-usage='chat']") \
        .select_option("llama3.1")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    put = records["admin_settings_put"][-1]["model_catalog"]
    assert put["openai"]["chat"]["default"] == "gpt-5.5"   # 未編集セルは明示保存のまま残る
    assert put["ollama"]["chat"]["default"] == "llama3.1"   # 実際に変えたセルも含まれる


def test_admin_openai_endpoint_test_button_sends_input_values(page, web_base_url):
    """「接続テスト」は保存前の入力中の値でその場だけ試す（保存しない・admin 専用の
    POST /admin/settings/openai-endpoint-test へ分離済み＝個人設定用 /settings/test は呼ばない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    page.locator("#openai-endpoint-base-url").fill("https://myres.openai.azure.com/openai/v1")
    page.locator("#openai-endpoint-test").click()

    expect(page.locator("#openai-endpoint-test-res")).to_contain_text("接続OK")
    body = records["admin_openai_endpoint_test"][-1]
    assert body["provider"] == "openai"
    assert body["openai_endpoint_kind"] == "azure"
    assert body["openai_base_url"] == "https://myres.openai.azure.com/openai/v1"
    assert records["settings_test"] == []        # 個人設定用エンドポイントは呼ばない
    assert records["admin_settings_put"] == []   # 保存はしていない


def test_admin_openai_endpoint_save_azure_without_base_url_shows_error(page, web_base_url):
    """「Azure OpenAI」を選んだまま接続先 URL を空にして保存すると、
    PUT と同じクロス検証（kind!=openai なら base_url 必須）で拒否されエラー表示になる。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    # base URL 欄は空のまま保存する。
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("接続先 URL")


def test_admin_openai_endpoint_test_azure_without_base_url_shows_error(page, web_base_url):
    """「接続テスト」（POST /admin/settings/openai-endpoint-test）も PUT と
    同じクロス検証を共有する契約（`{"openai_endpoint_kind": "azure"}` 単独送信は mock でも 422）。
    実サーバなら拒否される組み合わせを e2e 上だけ素通りさせることはない。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("input[data-openai-endpoint-kind='azure']").check()
    # base URL 欄は空のまま接続テストする（保存はしない）。
    page.locator("#openai-endpoint-test").click()

    expect(page.locator("#openai-endpoint-test-res")).to_contain_text("接続先 URL")
    body = records["admin_openai_endpoint_test"][-1]
    assert body["openai_endpoint_kind"] == "azure"
    assert not body.get("openai_base_url")
    assert records["admin_settings_put"] == []   # 保存はしていない


def test_mock_openai_endpoint_pending_inherits_saved_base_url_when_body_has_kind_only():
    """mock のマージ純関数を直接固定する（実ブラウザの admin-settings.js は kind!=openai
    のとき base_url も一緒に送るため、`{"openai_endpoint_kind": "azure"}` 単独の body は UI 経由
    では作れない＝この組合せの検証は mock の純関数自体を直接呼んで確認する）。保存済み base_url が
    ある状態で kind だけの body を渡しても、pending は保存済み base_url を引き継ぎ、クロス検証は
    422 にならない。"""
    import mock_api

    configured = {"kind": "azure", "base_url": "https://res.openai.azure.com",
                 "auth_header": "bearer", "api_version": None}
    pending = mock_api._mock_openai_endpoint_pending(configured, {"openai_endpoint_kind": "azure"})
    assert pending["openai_base_url"] == "https://res.openai.azure.com"
    kind = mock_api._mock_infer_openai_endpoint_kind(
        pending["openai_endpoint_kind"], pending["openai_base_url"] or "")
    assert mock_api._mock_validate_openai_endpoint_cross(kind, pending["openai_base_url"] or "") is None


def test_mock_openai_endpoint_pending_still_422_when_kind_only_and_no_saved_base():
    """対照: 保存済み base_url が無い状態（既定モックと同じ）なら、kind のみの body は従来どおり
    クロス検証で拒否される（`test_admin_openai_endpoint_test_azure_without_base_url_shows_error` の
    Playwright 経由の確認を、mock の純関数単体でも固定する）。"""
    import mock_api

    configured = {"kind": None, "base_url": None, "auth_header": None, "api_version": None}
    pending = mock_api._mock_openai_endpoint_pending(configured, {"openai_endpoint_kind": "azure"})
    kind = mock_api._mock_infer_openai_endpoint_kind(
        pending["openai_endpoint_kind"], pending["openai_base_url"] or "")
    assert mock_api._mock_validate_openai_endpoint_cross(kind, pending["openai_base_url"] or "") is not None


def test_admin_openai_endpoint_save_does_not_mutate_shared_system_settings_view_constant(page, web_base_url):
    """PUT ハンドラの状態保持更新（`clear()`/`update()`）が、モジュール定数
    `mock_api.SYSTEM_SETTINGS_VIEW`（他テストと共有）自体を書き換えていないことを固定する
    （`install_api_mocks` 省略時の `system_settings_resp` deep-copy 漏れの再発防止）。"""
    import copy

    import mock_api

    before = copy.deepcopy(mock_api.SYSTEM_SETTINGS_VIEW)
    assert before["openai_endpoint"]["configured"]["kind"] is None

    install_api_mocks(page)   # system_settings 省略＝定数をそのまま使う既定経路
    page.goto(f"{web_base_url}/admin-settings.html")
    page.locator("input[data-openai-endpoint-kind='azure']").check()
    page.locator("#openai-endpoint-base-url").fill("https://mutation-check.openai.azure.com")
    page.locator("#save").click()

    from playwright.sync_api import expect
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert mock_api.SYSTEM_SETTINGS_VIEW == before, \
        "PUT ハンドラがモジュール定数 SYSTEM_SETTINGS_VIEW を直接書き換えた（deep-copy 漏れの再発）"


def test_admin_openai_endpoint_save_does_not_mutate_caller_supplied_system_settings_dict(page, web_base_url):
    """`install_api_mocks(system_settings=caller_dict)` のように呼び出し元が自前の dict を
    渡した場合も、その dict 自体は書き換えられない（`system_settings_resp` は常に deep-copy して
    切り離される・`SYSTEM_SETTINGS_VIEW` 定数不変テストとは別に、呼び出し元所有の dict でも同じ
    契約が成り立つことを固定する）。"""
    import copy
    import json as _json

    import mock_api
    from playwright.sync_api import expect

    caller_dict = _json.loads(_json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    before = copy.deepcopy(caller_dict)
    assert before["openai_endpoint"]["configured"]["kind"] is None

    install_api_mocks(page, system_settings=caller_dict)
    page.goto(f"{web_base_url}/admin-settings.html")
    page.locator("input[data-openai-endpoint-kind='azure']").check()
    page.locator("#openai-endpoint-base-url").fill("https://caller-dict-mutation-check.openai.azure.com")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert caller_dict == before, \
        "PUT ハンドラが呼び出し元所有の system_settings dict を直接書き換えた（deep-copy 漏れ）"


def test_admin_settings_usage_chat_ai_shows_warning_for_invalid_configured_value(page, web_base_url):
    """`usage_chat.configured` が選択肢に無い（旧データ・手動編集等で不正な値）場合、
    ラジオを固定表示せず明示の注意文を出す（黙って既定へ丸めて正常な選択の
    ように見せない）。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "gemini", "effective": "(不正な保存値)",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    records = install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    expect(page.locator("#usage-chat-ai-radios")).to_contain_text("不正です")
    # 内部の非表示センチネル（`_USAGE_CHAT_PROVIDER_INVALID`）はチェック状態でも除外し、
    # 実際に選べる（見える）ラジオがどれも選ばれていないことだけを確認する。
    expect(page.locator(
        '#usage-chat-ai-radios input[data-usage-chat-provider]:checked:not([hidden])'
    )).to_have_count(0)
    # 選び直して保存はできる（保存対象から外れていない）。
    page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]').check()
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1].get("usage_chat_provider") == "ollama"


def test_admin_settings_usage_chat_ai_invalid_saved_not_cleared_by_unrelated_save(page, web_base_url):
    """保存値が不正な間、選び直さずに保存しても、`usage_chat_provider` を黙って null
    （既定へ戻す＝不正値の暗黙解除）で送ってはいけない——PUT body に `usage_chat_provider`
    キー自体が含まれないこと。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "gemini", "effective": "(不正な保存値)",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    records = install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    expect(page.locator("#usage-chat-ai-radios")).to_contain_text("不正です")
    # 内部の非表示センチネル（`_USAGE_CHAT_PROVIDER_INVALID`）はチェック状態でも除外し、
    # 実際に選べる（見える）ラジオがどれも選ばれていないことだけを確認する。
    expect(page.locator(
        '#usage-chat-ai-radios input[data-usage-chat-provider]:checked:not([hidden])'
    )).to_have_count(0)

    # usage_chat_provider には一切触れず、そのまま保存する。
    page.locator("#save").click()
    expect(page.locator("#msg")).to_contain_text("保存しました")

    put = records["admin_settings_put"][-1]
    assert "usage_chat_provider" not in put, \
        "選び直していないのに usage_chat_provider が送られている（不正値を暗黙に解除している）"


def test_admin_settings_usage_chat_ai_invalid_sentinel_not_keyboard_or_ax_reachable(
        page, web_base_url):
    """保存値が不正な間に混ぜる非表示センチネル（`_USAGE_CHAT_PROVIDER_INVALID`）は、
    Tab／矢印キーで到達できず、アクセシビリティツリー（ロール）にも露出しない
    （`hidden` 属性の既定挙動任せにせず `aria-hidden`/`tabindex="-1"` を明示する）。"""
    from playwright.sync_api import expect
    import mock_api

    settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    settings["usage_chat"] = {"configured": "gemini", "effective": "(不正な保存値)",
                              "default": "ollama", "providers": ["openai", "ollama"]}
    install_api_mocks(page, system_settings=settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "usage")

    sentinel = page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="__invalid__"]')
    expect(sentinel).to_have_attribute("aria-hidden", "true")
    expect(sentinel).to_have_attribute("tabindex", "-1")

    # AX ツリー（role=radio）に露出するのは実在する3択（実行構成に合わせる／openai／ollama）
    # だけ。
    expect(page.locator("#usage-chat-ai-radios").get_by_role("radio")).to_have_count(3)

    _REAL_VALUES = ("", "openai", "ollama")   # "" ＝「実行構成に合わせる」ラジオの値

    # 実際に Tab キーで到達しないこと: この card の次に来る focusable 要素（タブの既定リセット
    # ボタン）から Shift+Tab で戻ると、ブラウザは `hidden`/`tabindex="-1"` の要素を
    # ロービング tabindex から除外するため、チェック状態がセンチネル側にあってもグループ内の
    # 実在するいずれかのラジオへ着地する（センチネルには乗らない）。
    page.locator('[data-reset-tab="usage"]').focus()
    page.keyboard.press("Shift+Tab")
    focused_back = page.evaluate("document.activeElement.getAttribute('data-usage-chat-provider')")
    assert focused_back in _REAL_VALUES, \
        "Shift+Tab でラジオグループへ戻った時、センチネルではなく実在するラジオに着地するはず"

    # 末尾（ollama）から ArrowDown で循環しても、センチネルに止まらず実在するラジオへ戻る
    # （非表示センチネルはロービング tabindex の輪から除外されている）。
    page.locator('#usage-chat-ai-radios input[data-usage-chat-provider="ollama"]').focus()
    page.keyboard.press("ArrowDown")
    focused_wrap = page.evaluate("document.activeElement.getAttribute('data-usage-chat-provider')")
    assert focused_wrap in _REAL_VALUES, \
        "末尾から ArrowDown で循環する時、センチネルではなく実在するラジオへ戻るはず"


# ===== SC-6c: 調べる深さの基準値（調べ方ブロック §3.2・「プロバイダ＋接続先」タブの追加カード） =====

def test_depth_profile_card_renders_unset_state(page, web_base_url):
    """未設定（既定モック）は全欄が空欄・ヒントは組み込み既定を案内する。Codex 推論レベルは
    「環境設定の既定に従う」（空選択肢）が選ばれ、他の6項目と同じ未設定表示になる
    （選択済みの具体レベルを表示すると、単独リセット＝空選択肢の再選択と区別できなくなる）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#depth-base-max-turns")).to_have_value("")
    expect(page.locator("#depth-base-max-turns-hint")).to_contain_text("未設定です")
    expect(page.locator("#depth-base-codex-reasoning")).to_have_value("")
    expect(page.locator("#depth-base-codex-reasoning-hint")).to_contain_text("未設定です")


def test_depth_profile_card_renders_configured_values(page, web_base_url):
    """管理者が既に保存済みの基準値は、各欄に生値（configured）が表示される。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["depth_profile"]["max_turns"] = {"configured": 20, "effective": 20, "default": 12}
    system_settings["depth_profile"]["codex_reasoning"] = {
        "configured": "high", "effective": "high", "default": "low",
        "options": ["minimal", "low", "medium", "high", "xhigh"]}
    install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator("#depth-base-max-turns")).to_have_value("20")
    expect(page.locator("#depth-base-max-turns-hint")).to_contain_text("この値で固定中")
    expect(page.locator("#depth-base-codex-reasoning")).to_have_value("high")
    expect(page.locator("#depth-base-codex-reasoning-hint")).to_contain_text("この値で固定中")


def test_depth_profile_card_save_sends_changed_fields_only(page, web_base_url):
    """変更した項目だけを PUT body に含める（触っていない項目は送らない・他タブと同じダーティ判定）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#depth-base-max-turns").fill("30")
    page.locator("#depth-base-codex-reasoning").select_option("xhigh")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    body = records["admin_settings_put"][-1]
    assert body.get("depth_base_max_turns") == 30
    assert body.get("depth_base_codex_reasoning") == "xhigh"
    assert "depth_base_grep_max_hits" not in body   # 触っていない項目は送らない


def test_depth_profile_card_clear_field_sends_null(page, web_base_url):
    """既に設定済みの欄を空欄に戻して保存すると null（未設定へ戻す）を送る。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["depth_profile"]["read_window"] = {"configured": 80, "effective": 80, "default": 40}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    expect(page.locator("#depth-base-read-window")).to_have_value("80")

    page.locator("#depth-base-read-window").fill("")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    assert records["admin_settings_put"][-1].get("depth_base_read_window") is None


def test_depth_profile_card_codex_reasoning_standalone_reset_sends_null_only(page, web_base_url):
    """Codex 推論レベルだけを「環境設定の既定に従う」（空選択肢）に選び直して保存すると、
    その項目だけ null を送る（「このタブを既定に戻す」ボタンを押さなくても単独でリセットできる・
    他の6項目には触れていないので一緒に送らない）。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["depth_profile"]["codex_reasoning"] = {
        "configured": "high", "effective": "high", "default": "low",
        "options": ["minimal", "low", "medium", "high", "xhigh"]}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    expect(page.locator("#depth-base-codex-reasoning")).to_have_value("high")

    page.locator("#depth-base-codex-reasoning").select_option("")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("保存しました")
    body = records["admin_settings_put"][-1]
    assert body.get("depth_base_codex_reasoning") is None
    assert "depth_base_max_turns" not in body   # 他の項目には触れていない


def test_depth_profile_card_reset_tab_nulls_all_seven_fields(page, web_base_url):
    """「このタブを既定に戻す」（プロバイダ＋接続先タブ）は調べる深さの基準値7項目も対象に含む。"""
    from playwright.sync_api import expect
    import mock_api

    system_settings = json.loads(json.dumps(mock_api.SYSTEM_SETTINGS_VIEW))
    system_settings["depth_profile"]["max_turns"] = {"configured": 20, "effective": 20, "default": 12}
    records = install_api_mocks(page, system_settings=system_settings)
    page.goto(f"{web_base_url}/admin-settings.html")
    expect(page.locator("#depth-base-max-turns")).to_have_value("20")

    page.locator('[data-reset-tab="provider"]').click()
    expect(page.locator("#tab-reset-res-provider")).to_contain_text("既定に戻しました")
    expect(page.locator("#depth-base-max-turns")).to_have_value("")

    body = records["admin_settings_put"][-1]
    for key in ("depth_base_max_turns", "depth_base_grep_max_hits", "depth_base_qa_max_hits",
               "depth_base_read_window", "depth_base_impact_depth", "depth_base_troubleshoot_depth",
               "depth_base_codex_reasoning"):
        assert body.get(key) is None, key


def test_mock_validate_depth_base_rejects_negative_zero_and_upper_plus_one():
    """mock の 422 契約を実サーバの範囲（`SystemSettingsReq` の Field(ge,le)）と同じ境界で固定する
    （負値・0・上限+1）。クライアント側検証（`validateDepthProfileInputs`）がブラウザ経由では
    これらの値の送信自体を防ぐため、mock の純関数を直接呼んで契約を確認する。"""
    import mock_api

    assert mock_api._mock_validate_depth_base({"depth_base_max_turns": -1}) is not None
    assert mock_api._mock_validate_depth_base({"depth_base_max_turns": 0}) is not None
    assert mock_api._mock_validate_depth_base({"depth_base_max_turns": 201}) is not None
    assert mock_api._mock_validate_depth_base({"depth_base_read_window": 9}) is not None    # 下限10未満
    assert mock_api._mock_validate_depth_base({"depth_base_read_window": 401}) is not None  # 上限400+1
    assert mock_api._mock_validate_depth_base({"depth_base_troubleshoot_depth": 17}) is not None


def test_mock_validate_depth_base_int_error_shape_matches_real_pydantic_detail():
    """整数6項目の422は実APIの`detail`と同形（pydanticのField(ge,le)違反はリスト・
    `loc`/`type`/`msg`/`input`/`ctx`を持つ）にする——文字列1本の独自形式にすると、`[object Object]`
    のような表示崩れを検出できるe2e/クライアント側の対応漏れを見逃す。"""
    import mock_api

    err = mock_api._mock_validate_depth_base({"depth_base_max_turns": 0})
    assert isinstance(err, list) and len(err) == 1
    assert err[0]["type"] == "greater_than_equal"
    assert err[0]["loc"] == ["body", "depth_base_max_turns"]
    assert err[0]["ctx"] == {"ge": 1}

    err_hi = mock_api._mock_validate_depth_base({"depth_base_max_turns": 500})
    assert err_hi[0]["type"] == "less_than_equal" and err_hi[0]["ctx"] == {"le": 200}

    err_type = mock_api._mock_validate_depth_base({"depth_base_max_turns": "twelve"})
    assert err_type[0]["type"] == "int_type"

    # codex_reasoning の語彙不一致は実APIの HTTPException と同じ文字列（リストではない）。
    err_reasoning = mock_api._mock_validate_depth_base({"depth_base_codex_reasoning": "very-high"})
    assert isinstance(err_reasoning, str)


def test_mock_validate_depth_base_accepts_boundary_values_and_null():
    """対照: 下限・上限ちょうどの値と null（未設定へ戻す）は受理される（誤って範囲を狭めていないか）。"""
    import mock_api

    assert mock_api._mock_validate_depth_base({"depth_base_max_turns": 1}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_max_turns": 200}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_read_window": 10}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_read_window": 400}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_max_turns": None}) is None


def test_mock_validate_depth_base_rejects_unknown_codex_reasoning_level():
    """depth_base_codex_reasoning は `sherpa.depth_profile.CODEX_REASONING_LEVELS` 以外（正規化後
    でも未知の語彙）は422。既知語彙は大文字・前後空白があっても実APIと同じ正規化
    （`strip().lower()`）で受理する（"High"/" HIGH " も422にしない）。null（環境設定の既定に
    従う）・型不一致（非文字列）も実APIと同じ扱いにする。"""
    import mock_api

    assert mock_api._mock_validate_depth_base({"depth_base_codex_reasoning": "very-high"}) is not None
    assert mock_api._mock_validate_depth_base({"depth_base_codex_reasoning": "xhigh"}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_codex_reasoning": "High"}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_codex_reasoning": " HIGH "}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_codex_reasoning": None}) is None
    assert mock_api._mock_validate_depth_base({"depth_base_codex_reasoning": 123}) is not None


def test_admin_settings_put_normalizes_and_saves_codex_reasoning_case_and_whitespace(page, web_base_url):
    """`depth_base_codex_reasoning` は実APIと同じ正規化（`strip().lower()`）で受理し、正規化後の
    値（"high"）を保存する（" HIGH " のような大文字・前後空白付きの値を422にしない・保存された
    値も生値のままでなく正規化後の値になる）。実UIの select は既定の語彙しか選べないため
    （タイプミス系の入力経路が無い）、`page.evaluate` で直接 fetch してこの防御自体を確認する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    status = page.evaluate("""
        async () => {
          const res = await fetch('/admin/settings', {
            method: 'PUT', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ depth_base_codex_reasoning: ' HIGH ' }),
          });
          return res.status;
        }
    """)
    assert status == 200
    assert records["admin_settings_put"][-1].get("depth_base_codex_reasoning") == " HIGH "   # 送信値は生のまま記録
    page.reload()
    expect(page.locator("#depth-base-codex-reasoning")).to_have_value("high")   # 保存値は正規化後


def test_admin_settings_put_returns_422_for_out_of_range_depth_base(page, web_base_url):
    """mock の PUT ハンドラ自体が範囲外の depth_base_* を 422 で拒否し、state を変更しない
    （クライアント検証を素通りさせた場合の防御・`system_settings_resp` が汚染されないことも確認）。
    UI 経由では `validateDepthProfileInputs()` がこの値の送信自体を止めるため、
    `page.evaluate` でフォーム検証を経由せず直接 fetch する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    status = page.evaluate("""
        async () => {
          const res = await fetch('/admin/settings', {
            method: 'PUT', credentials: 'include',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ depth_base_max_turns: 0 }),
          });
          return res.status;
        }
    """)
    assert status == 422
    # 直後の GET（再読込）が汚染されていない＝422 で state 変更していないことの確認。
    page.reload()
    expect(page.locator("#depth-base-max-turns")).to_have_value("")


def test_depth_profile_card_save_rejects_out_of_range_client_side(page, web_base_url):
    """保存ボタン押下時、サーバと同じ範囲を超える値は日本語エラーを表示して PUT 自体を送らない
    （422 の配列表示が読めなくなる問題を未然に防ぐ）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#depth-base-max-turns").fill("201")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("探索の反復回数は1〜200の整数で指定してください")
    assert records["admin_settings_put"] == []


def test_depth_profile_card_save_rejects_zero_client_side(page, web_base_url):
    """0 は下限未満（`min=1`）として拒否される（サーバの `ge=1` と同じ境界）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator("#depth-base-troubleshoot-depth").fill("0")
    page.locator("#save").click()

    expect(page.locator("#msg")).to_contain_text("原因を調べる近傍の深さは1〜16の整数で指定してください")
    assert records["admin_settings_put"] == []


# ===== UI-TABS2: 管理系ページの埋め込みタブ（iframe・2026-09-04 フィードバック是正）=====
# 旧・リンクタブ（<a class="tab-link" href="...">）は実装がページ遷移そのものだった
# （「デザインが他タブと異なる」「完全に画面遷移していてタブの移動になっていない」という閉域
# 実機フィードバックを受け、本物のタブ挙動＝同一ページ内の iframe 埋め込みへ置き換えた）。

def test_admin_settings_tab_bar_renders_four_embed_tabs_as_real_tabs(page, web_base_url):
    """管理系ページへの入口（ユーザー管理・利用統計・監査ログ・システム状態）が、
    旧・リンク（<a class="tab-link">）ではなく設定タブと同じ本物のタブ（role="tab"）として
    描画される（見た目も .tab-btn と同一様式・href は持たない）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator('#admin-tabs a.tab-link')).to_have_count(0)   # 旧・リンクタブは撤去済み
    embed_btns = page.locator(
        '#admin-tabs .tab-btn[data-tab="users"], #admin-tabs .tab-btn[data-tab="usage-page"], '
        '#admin-tabs .tab-btn[data-tab="audit"], #admin-tabs .tab-btn[data-tab="status"]')
    expect(embed_btns).to_have_count(4)
    for tab_key in ("users", "usage-page", "audit", "status"):
        btn = page.locator(f'#admin-tabs .tab-btn[data-tab="{tab_key}"]')
        expect(btn).to_have_attribute("role", "tab")
        assert btn.get_attribute("href") is None   # button のため href を持たない
        expect(btn).to_have_attribute("aria-selected", "false")


def test_admin_settings_no_bottom_menucard_section(page, web_base_url):
    """旧・下部の「管理メニュー」カード区画（menugrid/menucard）は撤去済み。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    expect(page.locator(".menugrid")).to_have_count(0)
    expect(page.locator("a.menucard")).to_have_count(0)


def test_admin_settings_embed_tab_click_stays_on_page_and_shows_iframe_panel(page, web_base_url):
    """埋め込みタブをクリックしても URL（ハッシュ以外）は変わらず、admin-settings.html に
    留まったまま対応する iframe パネルが表示される（(a)）。設定タブに未保存の変更があっても
    確認ダイアログは出ない（画面を離れないため不要・旧・confirm 連携コードは撤去済み）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")
    open_tab(page, "ingest")
    page.locator("#arms-list input[data-arm='pdf_text']").uncheck()   # ingest タブをダーティにする
    expect(page.locator("#tab-dot-ingest")).to_be_visible()

    page.locator('.tab-btn[data-tab="status"]').click()   # ダイアログハンドラ未登録でも構わない＝出ない想定

    expect(page).to_have_url(f"{web_base_url}/admin-settings.html#status")   # 遷移ではなくハッシュのみ変化
    expect(page.locator("#tabpanel-status")).to_be_visible()
    expect(page.locator("#tabpanel-status iframe.embed-frame")).to_be_visible()
    expect(page.locator('.tab-btn[data-tab="status"]')).to_have_attribute("aria-selected", "true")


def test_admin_settings_embed_tab_iframe_src_has_embed_param(page, web_base_url):
    """選択した埋め込みタブの iframe src は対象ページ + `?embed=1` になる（(b)）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    page.locator('.tab-btn[data-tab="users"]').click()
    expect(page.locator("#embed-frame-users")).to_have_attribute("src", "admin-users.html?embed=1")

    page.locator('.tab-btn[data-tab="usage-page"]').click()
    expect(page.locator("#embed-frame-usage-page")).to_have_attribute("src", "usage.html?embed=1")

    page.locator('.tab-btn[data-tab="audit"]').click()
    expect(page.locator("#embed-frame-audit")).to_have_attribute("src", "audit.html?embed=1")

    page.locator('.tab-btn[data-tab="status"]').click()
    expect(page.locator("#embed-frame-status")).to_have_attribute("src", "status.html?embed=1")


def test_admin_settings_embed_tab_iframes_lazy_load_only_selected(page, web_base_url):
    """未選択の埋め込みタブの iframe は src を持たない（遅延ロード・(c)）。選択すると
    そのタブの iframe だけ src が付き、他タブは付いたままにならない（他タブは未選択のまま）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-settings.html")

    for frame_id in ("embed-frame-users", "embed-frame-usage-page",
                      "embed-frame-audit", "embed-frame-status"):
        assert page.locator(f"#{frame_id}").get_attribute("src") is None

    page.locator('.tab-btn[data-tab="audit"]').click()
    expect(page.locator("#embed-frame-audit")).to_have_attribute("src", "audit.html?embed=1")
    # 他タブの iframe は未選択のまま＝src 未設定のまま。
    for frame_id in ("embed-frame-users", "embed-frame-usage-page", "embed-frame-status"):
        assert page.locator(f"#{frame_id}").get_attribute("src") is None


def test_admin_users_standalone_open_without_embed_param_shows_own_nav(page, web_base_url):
    """埋め込み先ページ（例: admin-users.html）を `?embed` 無しで単独直開きすると、従来どおり
    自分のトップバー/ナビが見える（(d)・単独アクセス経路は完全に不変）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")

    expect(page.locator("sherpa-topbar")).to_be_visible()
    expect(page.locator("#sherpa-nav")).to_be_visible()
    assert page.evaluate("document.documentElement.classList.contains('embedded')") is False


def test_admin_users_embed_param_hides_own_nav(page, web_base_url):
    """`?embed=1` 付きで開くと、自分のトップバー/ナビが隠れる（管理タブからの iframe 埋め込み時の
    見た目）。管理者ガード等の機能そのものは変わらず、ユーザー一覧は描画される。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html?embed=1")

    expect(page.locator("html")).to_have_class("embedded")
    expect(page.locator("sherpa-topbar")).to_be_hidden()
    expect(page.locator("#user-tbody tr").first).to_be_visible()
