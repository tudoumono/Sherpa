from __future__ import annotations

import os
import time

import pytest


def _uid(prefix: str) -> str:
    return f"{prefix}-{int(time.time() * 1000)}"


def _create_user_via_admin_ui(page, live_base_url: str, uid: str, password: str,
                              *, display_name: str = "Live E2E User", role: str = "user"):
    from playwright.sync_api import expect

    page.goto(f"{live_base_url}/ui/admin-users.html")
    expect(page.locator("#main-content")).to_be_visible()
    page.locator("#nu-uid").fill(uid)
    page.locator("#nu-name").fill(display_name)
    page.locator("#nu-role").select_option(role)
    page.locator("#nu-pw").fill(password)
    page.locator("#nu-submit").click()

    row = page.locator("#user-tbody tr", has_text=uid)
    expect(row).to_contain_text(display_name)
    return row


def _force_heuristic(page, live_base_url: str, api_put_json):
    r = api_put_json(f"{live_base_url}/settings", {"agent": "heuristic"})
    assert r.ok, f"failed to set heuristic agent: {r.status} {r.text()}"


def _send_and_wait_answer(page, message: str, timeout: int = 30000):
    """メッセージを送って『思考の流れ』が完了するまで待ち、直近の回答カード（assistant 側の
    最後の .msg）を返す（フェーズ7 S7・UC 縦切り3本の共通ヘルパ）。"""
    from playwright.sync_api import expect

    page.locator("#input").fill(message)
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了", timeout=timeout)
    return page.locator("#messages .msg:not(.user)").last


def test_live_login_logout_and_admin_navigation(page, live_base_url, admin_login):
    from playwright.sync_api import expect

    admin_login("/ui/chat.html")

    expect(page.locator("#sherpa-nav")).to_contain_text("ユーザー管理")
    expect(page.locator("#sherpa-nav")).to_contain_text("監査ログ")
    expect(page.locator("#sherpa-nav")).to_contain_text("マイワークスペース")

    page.on("dialog", lambda dialog: dialog.accept())
    page.locator("#logoutbtn").click()
    page.wait_for_url("**/ui/login.html**", timeout=7000)
    expect(page.locator("#form")).to_be_visible()


def test_live_admin_creates_member_and_member_is_denied_admin_ui(
    page, live_base_url, admin_login, login_as
):
    from playwright.sync_api import expect

    uid = _uid("e2e-user")
    password = "Live-e2e-pass-123"

    admin_login("/ui/admin-users.html")
    _create_user_via_admin_ui(page, live_base_url, uid, password, display_name="Live E2E Member")

    page.context.clear_cookies()
    login_as(uid, password, "/ui/admin-users.html")

    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#main-content")).to_be_hidden()


def test_live_workspace_upload_search_delete(page, live_base_url, admin_login, tmp_path):
    from playwright.sync_api import expect

    token = _uid("workspace-token")
    filename = f"live_{token}.md".replace("-", "_")
    upload = tmp_path / filename
    upload.write_text(f"{token}\nTAX-RATE live workspace search\n", encoding="utf-8")

    admin_login("/ui/workspace.html")
    page.on("dialog", lambda dialog: dialog.accept())

    page.locator("#file-input").set_input_files(str(upload))
    expect(page.locator("#upload-msg")).to_contain_text(f"アップロード完了: {filename}")
    expect(page.locator("#file-list")).to_contain_text(filename)

    page.locator("#search-q").fill(token)
    page.locator("#search-btn").click()
    expect(page.locator("#search-results")).to_contain_text("個人ファイル内ヒット")
    expect(page.locator("#search-results")).to_contain_text(filename)

    row = page.locator("#file-list tr", has_text=filename)
    row.locator("[data-fileid]").click()
    expect(page.locator("#file-list")).not_to_contain_text(filename)


def test_live_conversation_share_owner_to_invited_user(
    page, live_base_url, admin_login, login_as, api_put_json
):
    from playwright.sync_api import expect

    invitee_uid = _uid("e2e-share")
    invitee_password = "Live-share-pass-123"
    message = f"共有 live E2E {invitee_uid}"

    admin_login("/ui/admin-users.html")
    _create_user_via_admin_ui(page, live_base_url, invitee_uid, invitee_password,
                              display_name="Live E2E Invitee")
    _force_heuristic(page, live_base_url, api_put_json)

    page.goto(f"{live_base_url}/ui/chat.html")
    page.locator("#input").fill(message)
    page.locator("#send").click()
    expect(page.locator("#rt")).to_contain_text("完了", timeout=20000)

    page.locator("#sharebtn").click()
    expect(page.locator("#share-overlay")).to_be_visible()
    page.locator("#share-invitees").fill(invitee_uid)
    page.locator("#share-days").select_option("1")
    page.locator("#share-submit").click()
    expect(page.locator("#share-result")).to_be_visible()
    share_url = page.locator("#share-url-val").inner_text()
    assert "/share/conversations/" in share_url

    page.context.clear_cookies()
    page.goto(share_url)
    page.wait_for_url("**/ui/login.html**", timeout=7000)
    assert "next=" in page.url
    page.locator("#username").fill(invitee_uid)
    page.locator("#password").fill(invitee_password)
    page.locator("#submit").click()

    page.wait_for_url("**/ui/chat.html?conversation_id=**", timeout=10000)
    expect(page.locator("#convlist")).to_contain_text("共有")
    expect(page.locator("#convlist")).to_contain_text(message[:40])


def test_live_codex_mcp_stream_smoke(page, live_base_url, admin_login, api_put_json):
    """任意実行: Codex/MCP を実サービスで流す smoke。

    Codex 認証・Neo4j/ES・MCP 設定が必要なので通常は skip。
    """
    from playwright.sync_api import expect

    if os.environ.get("SHERPA_E2E_LIVE_MCP") != "1":
        pytest.skip("set SHERPA_E2E_LIVE_MCP=1 to run the optional Codex/MCP live smoke")

    admin_login("/ui/chat.html")
    r = api_put_json(f"{live_base_url}/settings", {"agent": "codex"})
    assert r.ok, f"failed to set codex agent: {r.status} {r.text()}"

    page.goto(f"{live_base_url}/ui/chat.html")
    page.locator("#kbtoggle").click()
    page.locator("#input").fill("TAX-RATE に関係するプログラムを MCP で調べて")
    page.locator("#send").click()

    expect(page.locator("#flow")).to_contain_text("関連ノード", timeout=120000)
    expect(page.locator("#rt")).to_contain_text("完了", timeout=120000)


# ---- フェーズ7 S7: e2e_live の UC 縦切り3本（docs/12-ユースケース.md UC-1/UC-3・仕様問い合わせ） ----
# 頭脳は既存の共有縦切りと同じ手法で heuristic 強制＝Codex 実呼び出しなしで決定的に判定する
# （Codex/MCP 実走は上の test_live_codex_mcp_stream_smoke の役割のまま・重複させない）。
# fixtures/corpus/v1（税テーマ）の既知データ（tests/api/test_api.py・tests/integration/test_lens_m6.py
# で確認済みの到達ノード集合）を踏まえた意味的 assert のみ（件数ピンなし）。

def test_live_uc1_impact_lens_answer_card(page, live_base_url, admin_login, api_put_json):
    """UC-1 影響範囲分析（docs/12-ユースケース.md）: 「消費税率を変えたら影響は？」→ impact レンズの
    影響カード（到達ノードに TAXCALC/請求機能系を含む・件数ピンなし）＋出典（原本DL導線）が返る。"""
    from playwright.sync_api import expect

    admin_login("/ui/chat.html")
    _force_heuristic(page, live_base_url, api_put_json)

    page.goto(f"{live_base_url}/ui/chat.html")
    page.locator("#kbtoggle").click()   # 既定オフ→オン（社内資料を参照させる）
    card = _send_and_wait_answer(page, "消費税率を変えたら影響は？")

    expect(card).to_contain_text("影響範囲分析")
    expect(card).to_contain_text("影響")                 # 答え先頭（headline）に「影響」
    # 到達ノードに TAXCALC/請求機能系が含まれる（意味的 assert・件数ピンなし）
    expect(card).to_contain_text("TAXCALC")
    expect(card).to_contain_text("請求機能")
    # 出典（原本DL導線）の存在
    expect(card.locator(".sources a[data-dl]").first).to_be_visible()


def test_live_uc3_troubleshoot_lens_answer_card(page, live_base_url, admin_login, api_put_json):
    """UC-3 トラブルシュート（docs/12-ユースケース.md）: 「夜間バッチ NIGHTLY でエラーが出た」系の症状に
    troubleshoot レンズの原因候補カード＋関連手順書（締めバッチ運用手順）の出典が返る。"""
    from playwright.sync_api import expect

    admin_login("/ui/chat.html")
    _force_heuristic(page, live_base_url, api_put_json)

    page.goto(f"{live_base_url}/ui/chat.html")
    page.locator("#kbtoggle").click()
    card = _send_and_wait_answer(page, "夜間バッチ NIGHTLY でエラーが出た。原因候補は？")

    expect(card).to_contain_text("トラブルシュート")
    # 関連手順書（運用手順ドキュメント）が原因候補＋出典の双方に出る
    expect(card).to_contain_text("締めバッチ運用手順")
    expect(card.locator(".sources")).to_contain_text("締めバッチ運用手順")


def test_live_uc_qa_lens_answer_card(page, live_base_url, admin_login, api_put_json):
    """仕様問い合わせ（qa レンズ・計画書の文言どおり UC-2 ドキュメントチェックではなく仕様問い合わせ）:
    「端数処理の仕様は？」→ qa レンズの回答＋出典（税計算仕様書の該当節）が返る。"""
    from playwright.sync_api import expect

    admin_login("/ui/chat.html")
    _force_heuristic(page, live_base_url, api_put_json)

    page.goto(f"{live_base_url}/ui/chat.html")
    page.locator("#kbtoggle").click()
    card = _send_and_wait_answer(page, "端数処理の仕様は？")

    expect(card).to_contain_text("仕様問い合わせ")
    expect(card).to_contain_text("税計算仕様書")
    expect(card.locator(".sources a[data-dl]").first).to_be_visible()
