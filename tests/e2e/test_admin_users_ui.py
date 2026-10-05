from __future__ import annotations

import pytest
from mock_api import USER_MEMBER, install_api_mocks
from playwright.sync_api import expect


def _extra_user(uid, display_name, status):
    return {"uid": uid, "email": f"{uid}@example.com", "display_name": display_name,
            "role": "user", "status": status, "must_change_password": False, "last_login_at": None}


def _open(page, web_base_url, **mock_kw):
    records = install_api_mocks(page, **mock_kw)
    page.goto(f"{web_base_url}/admin-users.html")
    return records


def _add_user(page, uid, name, pw, role=None):
    page.locator("#nu-uid").fill(uid)
    page.locator("#nu-name").fill(name)
    if role:
        page.locator("#nu-role").select_option(role)
    page.locator("#nu-pw").fill(pw)
    page.locator("#nu-submit").click()


def _edit(page, uid):
    page.locator(f"#user-tbody [data-edit='{uid}']").click()
    expect(page.locator("#edit-overlay")).to_be_visible()


def _submit_edit_expecting_no_change(page, records):
    before_count = len(records["admin_users_patch"])
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-err")).to_contain_text("変更点がありません")
    assert len(records["admin_users_patch"]) == before_count   # PATCH は送られない


def _patch_status(page, uid, body):
    return page.evaluate("""async ([uid, body]) => {
      const res = await fetch('/admin/users/' + uid, {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(body),
      });
      return res.status;
    }""", [uid, body])


def test_admin_users_create_and_edit_user(page, web_base_url):
    records = _open(page, web_base_url)

    expect(page.locator("#user-count")).to_have_text("(2 人)")
    expect(page.locator("#user-tbody")).to_contain_text("admin")
    expect(page.locator("#user-tbody")).to_contain_text("佐藤 太郎")

    # last_login_at はサーバの UTC を端末ロケール（JST・+9h）に変換して表示
    admin_row = page.locator("#user-tbody tr", has_text="admin")
    expect(admin_row).to_contain_text("2026-07-01 18:00")
    expect(admin_row).not_to_contain_text("09:00")

    _add_user(page, "tanaka", "田中 花子", "initial-pass", role="admin")
    expect(page.locator("#user-count")).to_have_text("(3 人)")
    row = page.locator("#user-tbody tr", has_text="tanaka")
    expect(row).to_contain_text("田中 花子")
    expect(row).to_contain_text("管理者")
    assert records["admin_users_post"][-1] == {
        "uid": "tanaka",
        "display_name": "田中 花子",
        "role": "admin",
        "password": "initial-pass",
    }

    _edit(page, "tanaka")
    # 現在の表示名で事前入力され、uid 変更不可の注記も表示する
    expect(page.locator("#edit-name")).to_have_value("田中 花子")
    expect(page.locator(".modal-note")).to_contain_text("ユーザーID は変更できません")
    page.locator("#edit-name").fill("田中花子（改）")
    page.locator("#edit-role").select_option("user")
    page.locator("#edit-status").select_option("disabled")
    page.locator("#edit-pw").fill("reset-pass")
    page.locator("#edit-submit").click()

    expect(page.locator("#edit-overlay")).to_be_hidden()
    # 状態フィルターは既定「有効のみ」＝無効化した tanaka は一覧から消える
    expect(page.locator("#f-status")).to_have_value("active")
    expect(page.locator("#user-tbody")).not_to_contain_text("tanaka")
    # 無効化は「削除」と誤認されない具体的な通知を出し、フィルターは自動で変えない
    expect(page.locator("#toast")).to_contain_text("無効化しました")
    expect(page.locator("#toast")).to_contain_text("すべて")
    assert records["admin_users_patch"][-1] == {
        "uid": "tanaka",
        "display_name": "田中花子（改）",
        "role": "user",
        "status": "disabled",
        "password": "reset-pass",
    }

    page.locator("#f-status").select_option("all")
    row = page.locator("#user-tbody tr", has_text="tanaka")
    expect(row).to_contain_text("田中花子（改）")
    expect(row).to_contain_text("ユーザー")
    expect(row).to_contain_text("無効")

    # role/display_name のみの変更（無効化ではない）は従来どおりの通知
    _edit(page, "admin")
    page.locator("#edit-name").fill("管理者（改）")
    page.locator("#edit-submit").click()
    expect(page.locator("#toast")).to_contain_text("変更しました")

    # 既存 uid で「追加」すると 409 のエラーがフォームに表示され、新規行は追加されない
    _add_user(page, "admin", "乗っ取り", "attacker-pass")
    expect(page.locator("#nu-err")).to_contain_text("既に存在します")
    expect(page.locator("#user-count")).to_have_text("(3 人)")


@pytest.mark.parametrize("uid,shown,typed,sent", [
    ("sato", "佐藤 太郎", "佐藤太郎（改）", "佐藤太郎（改）"),
    ("shirata", " 白田 一郎 ", " 白田次郎 ", "白田次郎"),   # 前後空白付きの元値を無編集保存しても「変更」と誤判定しない
])
def test_admin_users_edit_no_changes_and_minimal_patch_payload(page, web_base_url, uid, shown, typed, sent):
    """無編集保存は PATCH を送らず「変更点がありません」を表示しダイアログは閉じない。
    実際に変わったキーだけが trim 済みで PATCH に載る。"""
    records = _open(page, web_base_url, extra_users=[_extra_user("shirata", " 白田 一郎 ", "active")])

    _edit(page, uid)
    expect(page.locator("#edit-name")).to_have_value(shown)
    _submit_edit_expecting_no_change(page, records)
    expect(page.locator("#edit-overlay")).to_be_visible()

    page.locator("#edit-name").fill(typed)
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-overlay")).to_be_hidden()
    assert records["admin_users_patch"][-1] == {"uid": uid, "display_name": sent}


def test_admin_users_pending_status_shown_in_all_only(page, web_base_url):
    """pending は「有効のみ」にも「無効のみ」にも含めず、「すべて」で「保留」と表示する。
    編集ダイアログの状態 select は pending ユーザーのときだけ「保留」選択肢を持ち、無編集保存は PATCH を送らない。"""
    records = _open(page, web_base_url, extra_users=[_extra_user("yokota", "横田三郎", "pending")])

    expect(page.locator("#user-count")).to_have_text("(2/3 人)")
    expect(page.locator("#user-tbody")).not_to_contain_text("yokota")

    page.locator("#f-status").select_option("disabled")   # pending は disabled ではない
    expect(page.locator("#user-count")).to_have_text("(0/3 人)")
    expect(page.locator("#user-tbody")).not_to_contain_text("yokota")

    page.locator("#f-status").select_option("all")
    expect(page.locator("#user-count")).to_have_text("(3 人)")
    expect(page.locator("#user-tbody tr", has_text="yokota")).to_contain_text("保留")

    # active ユーザーの編集では pending 選択肢が無い
    _edit(page, "admin")
    assert page.locator("#edit-status option[value='pending']").count() == 0
    page.keyboard.press("Escape")
    expect(page.locator("#edit-overlay")).to_be_hidden()

    # pending ユーザーの編集では選択肢が現れ選択済み・無編集なら status:"" 等は送られない
    _edit(page, "yokota")
    expect(page.locator("#edit-status option[value='pending']")).to_have_count(1)
    expect(page.locator("#edit-status")).to_have_value("pending")
    _submit_edit_expecting_no_change(page, records)
    page.keyboard.press("Escape")
    expect(page.locator("#edit-overlay")).to_be_hidden()

    # 続けて active ユーザーを編集すると pending 選択肢は残っていない（張り替え確認）
    page.locator("#f-status").select_option("active")
    _edit(page, "sato")
    assert page.locator("#edit-status option[value='pending']").count() == 0


def test_admin_users_filters_disabled_while_loading_and_after_failure(page, web_base_url):
    """一覧の読込失敗中はフィルターを無効化したままにし（誤描画を防ぐ）、再読込が成功したら再び有効化する。"""
    call_count = {"n": 0}

    def flaky_list(route):
        if route.request.method == "GET" and route.request.url.endswith("/admin/users"):
            call_count["n"] += 1
            if call_count["n"] == 1:
                route.fulfill(status=500, content_type="application/json", body='{"detail":"boom"}')
                return
        route.fallback()

    install_api_mocks(page)
    page.route("**/admin/users", flaky_list)
    page.goto(f"{web_base_url}/admin-users.html")

    expect(page.locator("#user-tbody")).to_contain_text("読み込みに失敗しました")
    expect(page.locator("#f-q")).to_be_disabled()
    expect(page.locator("#f-status")).to_be_disabled()

    page.reload()   # 2回目の GET は成功
    expect(page.locator("#user-count")).to_have_text("(2 人)")
    expect(page.locator("#f-q")).to_be_enabled()
    expect(page.locator("#f-status")).to_be_enabled()


def test_admin_users_search_and_status_filter(page, web_base_url):
    """検索（uid・表示名・メールの部分一致）と状態フィルター（既定=有効のみ）。"""
    _open(page, web_base_url)
    tbody = page.locator("#user-tbody")

    expect(page.locator("#user-count")).to_have_text("(2 人)")
    expect(page.locator("#f-status")).to_have_value("active")   # 既定は「有効のみ」

    for query, shown, hidden in (("sato", "sato", "admin"), ("管理者", "admin", "sato"),
                                 ("sato@example.com", "sato", "admin")):
        page.locator("#f-q").fill(query)
        expect(tbody).to_contain_text(shown)
        expect(tbody).not_to_contain_text(hidden)
    page.locator("#f-q").fill("")

    _add_user(page, "yamada", "山田次郎", "initial-pass")
    expect(page.locator("#user-count")).to_have_text("(3 人)")
    _edit(page, "yamada")
    page.locator("#edit-status").select_option("disabled")
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-overlay")).to_be_hidden()

    expect(page.locator("#user-count")).to_have_text("(2/3 人)")
    expect(tbody).not_to_contain_text("yamada")

    page.locator("#f-status").select_option("disabled")
    expect(page.locator("#user-count")).to_have_text("(1/3 人)")
    expect(tbody).to_contain_text("yamada")
    expect(tbody).not_to_contain_text("sato")

    page.locator("#f-status").select_option("all")
    expect(page.locator("#user-count")).to_have_text("(3 人)")
    for uid in ("yamada", "admin", "sato"):
        expect(tbody).to_contain_text(uid)


def test_admin_users_denies_non_admin_user(page, web_base_url):
    records = _open(page, web_base_url, user=USER_MEMBER)

    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#main-content")).to_be_hidden()
    assert records["admin_users_post"] == []


def test_admin_users_mock_patch_matches_real_contract(page, web_base_url):
    """e2e mock の PATCH /admin/users/{uid} は実サーバと同じ契約を持つ（ドリフトの固定）。
    実差分が無ければ 422・実差分があれば 200・status="pending" や空文字の role/status は範囲外として
    422 で拒否し、拒否時は該当行の状態も変わらない。"""
    _open(page, web_base_url)
    expect(page.locator("#user-count")).to_have_text("(2 人)")

    assert _patch_status(page, "sato", {"role": "user"}) == 422   # 現在値と同じ＝実差分なし
    assert _patch_status(page, "sato", {"role": "admin"}) == 200

    for payload in (
        {"status": "pending"},
        {"role": "", "display_name": "変更名"},
        {"status": "", "display_name": "変更名"},
    ):
        assert _patch_status(page, "admin", payload) == 422, payload

    page.reload()
    admin_row = page.locator("#user-tbody tr", has_text="admin")
    expect(admin_row).to_contain_text("管理者")
    expect(admin_row).to_contain_text("有効")
    expect(page.locator("#user-tbody")).not_to_contain_text("変更名")
