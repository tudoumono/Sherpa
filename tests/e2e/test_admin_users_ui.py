from __future__ import annotations

from mock_api import USER_MEMBER, install_api_mocks


def test_admin_users_create_and_edit_user(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")

    expect(page.locator("#user-count")).to_have_text("(2 人)")
    expect(page.locator("#user-tbody")).to_contain_text("admin")
    expect(page.locator("#user-tbody")).to_contain_text("佐藤 太郎")

    # S3: last_login_at はサーバの UTC（"2026-07-01T09:00:00+00:00"）を端末ロケール（JST・+9h）に変換して表示。
    admin_row = page.locator("#user-tbody tr", has_text="admin")
    expect(admin_row).to_contain_text("2026-07-01 18:00")
    expect(admin_row).not_to_contain_text("09:00")

    page.locator("#nu-uid").fill("tanaka")
    page.locator("#nu-name").fill("田中 花子")
    page.locator("#nu-role").select_option("admin")
    page.locator("#nu-pw").fill("initial-pass")
    page.locator("#nu-submit").click()

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

    page.locator("#user-tbody [data-edit='tanaka']").click()
    expect(page.locator("#edit-overlay")).to_be_visible()
    # USR-1: 編集ダイアログは現在の表示名で事前入力され、uid 変更不可の注記も表示する。
    expect(page.locator("#edit-name")).to_have_value("田中 花子")
    expect(page.locator(".modal-note")).to_contain_text("ユーザーID は変更できません")
    page.locator("#edit-name").fill("田中花子（改）")
    page.locator("#edit-role").select_option("user")
    page.locator("#edit-status").select_option("disabled")
    page.locator("#edit-pw").fill("reset-pass")
    page.locator("#edit-submit").click()

    expect(page.locator("#edit-overlay")).to_be_hidden()
    # 状態フィルターは既定「有効のみ」＝無効化した tanaka は一覧から消える。
    expect(page.locator("#f-status")).to_have_value("active")
    expect(page.locator("#user-tbody")).not_to_contain_text("tanaka")
    assert records["admin_users_patch"][-1] == {
        "uid": "tanaka",
        "display_name": "田中花子（改）",
        "role": "user",
        "status": "disabled",
        "password": "reset-pass",
    }

    # 「すべて」に切り替えると無効化済みユーザーも表示される。
    page.locator("#f-status").select_option("all")
    row = page.locator("#user-tbody tr", has_text="tanaka")
    expect(row).to_contain_text("田中花子（改）")
    expect(row).to_contain_text("ユーザー")
    expect(row).to_contain_text("無効")


def test_admin_users_edit_no_changes_and_minimal_patch_payload(page, web_base_url):
    """USR-1: 無編集保存は PATCH を送らず「変更点がありません」を表示する。
    実際に変わったキーだけが PATCH に載る（他フィールドの現在値を一緒に送らない）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")

    page.locator("#user-tbody [data-edit='sato']").click()
    expect(page.locator("#edit-overlay")).to_be_visible()
    before_count = len(records["admin_users_patch"])
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-err")).to_contain_text("変更点がありません")
    expect(page.locator("#edit-overlay")).to_be_visible()   # ダイアログは閉じない
    assert len(records["admin_users_patch"]) == before_count   # PATCH は送られない

    # 表示名だけを変えて保存すると、そのキーだけが PATCH に載る。
    page.locator("#edit-name").fill("佐藤太郎（改）")
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-overlay")).to_be_hidden()
    assert records["admin_users_patch"][-1] == {"uid": "sato", "display_name": "佐藤太郎（改）"}


def test_admin_users_edit_whitespace_only_display_name_not_sent_as_change(page, web_base_url):
    """USR-1 RV2: 前後空白を含む表示名を無編集のまま保存しても PATCH を送らない
    （trim 済み同士で比較すると、空白付きの元値を無編集保存しただけで「変更」と誤判定してしまう）。
    実際に入力を変えた場合は trim 済みの値を送る。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, extra_users=[
        {"uid": "shirata", "email": "shirata@example.com", "display_name": " 白田 一郎 ",
         "role": "user", "status": "active", "must_change_password": False, "last_login_at": None},
    ])
    page.goto(f"{web_base_url}/admin-users.html")

    page.locator("#user-tbody [data-edit='shirata']").click()
    expect(page.locator("#edit-overlay")).to_be_visible()
    expect(page.locator("#edit-name")).to_have_value(" 白田 一郎 ")
    before_count = len(records["admin_users_patch"])
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-err")).to_contain_text("変更点がありません")
    assert len(records["admin_users_patch"]) == before_count   # PATCH は送られない

    # 実際に編集した場合は trim 済みの値を送る。
    page.locator("#edit-name").fill(" 白田次郎 ")
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-overlay")).to_be_hidden()
    assert records["admin_users_patch"][-1] == {"uid": "shirata", "display_name": "白田次郎"}


def test_admin_users_pending_status_shown_in_all_only(page, web_base_url):
    """USR-1: pending は「有効のみ」にも「無効のみ」にも含めず、「すべて」で「保留」と表示する
    （DB契約は active/disabled/pending・ログイン可能なのは active のみ）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, extra_users=[
        {"uid": "yokota", "email": "yokota@example.com", "display_name": "横田三郎",
         "role": "user", "status": "pending", "must_change_password": False, "last_login_at": None},
    ])
    page.goto(f"{web_base_url}/admin-users.html")

    # 既定（有効のみ）では pending は畳まれる。
    expect(page.locator("#user-count")).to_have_text("(2/3 人)")
    expect(page.locator("#user-tbody")).not_to_contain_text("yokota")

    # 「無効のみ」にも含まれない（pending は disabled ではない）。
    page.locator("#f-status").select_option("disabled")
    expect(page.locator("#user-count")).to_have_text("(0/3 人)")
    expect(page.locator("#user-tbody")).not_to_contain_text("yokota")

    # 「すべて」で見え、状態は「保留」と表示される。
    page.locator("#f-status").select_option("all")
    expect(page.locator("#user-count")).to_have_text("(3 人)")
    row = page.locator("#user-tbody tr", has_text="yokota")
    expect(row).to_contain_text("保留")


def test_admin_users_edit_pending_status_select_has_matching_option(page, web_base_url):
    """USR-1 RV2 テール外観察: pending ユーザーの編集ダイアログの状態 select に一致する
    選択肢がなく status:"" が混入する既存穴を塞ぐ。状態欄は「保留（現状のまま）」が選択済みで
    表示され、他を触らず保存すれば PATCH は送られない（無編集扱い）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, extra_users=[
        {"uid": "yokota", "email": "yokota@example.com", "display_name": "横田三郎",
         "role": "user", "status": "pending", "must_change_password": False, "last_login_at": None},
    ])
    page.goto(f"{web_base_url}/admin-users.html")
    page.locator("#f-status").select_option("all")

    page.locator("#user-tbody [data-edit='yokota']").click()
    expect(page.locator("#edit-overlay")).to_be_visible()
    expect(page.locator("#edit-status")).to_have_value("pending")

    before_count = len(records["admin_users_patch"])
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-err")).to_contain_text("変更点がありません")
    assert len(records["admin_users_patch"]) == before_count   # status:"" 等は送られない


def test_admin_users_filters_disabled_while_loading_and_after_failure(page, web_base_url):
    """USR-1: 一覧の読込失敗中はフィルターを無効化したままにし（誤描画を防ぐ）、
    再読込が成功したら再び有効化する。"""
    from playwright.sync_api import expect

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

    # 初回読込は失敗＝フィルターは無効のまま（失敗表示中の操作で誤描画させない）。
    expect(page.locator("#user-tbody")).to_contain_text("読み込みに失敗しました")
    expect(page.locator("#f-q")).to_be_disabled()
    expect(page.locator("#f-status")).to_be_disabled()

    # 再読込（2回目の GET は成功）＝フィルターが再び有効になる。
    page.reload()
    expect(page.locator("#user-count")).to_have_text("(2 人)")
    expect(page.locator("#f-q")).to_be_enabled()
    expect(page.locator("#f-status")).to_be_enabled()


def test_admin_users_disable_shows_specific_toast_and_keeps_filter(page, web_base_url):
    """USR-1: 無効化時は「削除」と誤認されない具体的な通知を出し、フィルターは自動で変えない
    （既定=有効のみのまま＝無効化した行は一覧から消えるが理由は通知でわかる）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")

    page.locator("#user-tbody [data-edit='sato']").click()
    page.locator("#edit-status").select_option("disabled")
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-overlay")).to_be_hidden()
    expect(page.locator("#toast")).to_contain_text("無効化しました")
    expect(page.locator("#toast")).to_contain_text("すべて")
    expect(page.locator("#f-status")).to_have_value("active")   # フィルターは自動で変わらない
    expect(page.locator("#user-tbody")).not_to_contain_text("sato")   # 既定フィルターで畳まれる

    # role/display_name のみの変更（無効化ではない）は従来どおりの通知のまま。
    page.locator("#f-status").select_option("all")
    page.locator("#user-tbody [data-edit='admin']").click()
    page.locator("#edit-name").fill("管理者（改）")
    page.locator("#edit-submit").click()
    expect(page.locator("#toast")).to_contain_text("変更しました")


def test_admin_users_search_and_status_filter(page, web_base_url):
    """USR-1: 検索（uid・表示名・メールの部分一致）と状態フィルター（既定=有効のみ）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")

    expect(page.locator("#user-count")).to_have_text("(2 人)")
    expect(page.locator("#f-status")).to_have_value("active")   # 既定は「有効のみ」

    # 検索: uid の部分一致。
    page.locator("#f-q").fill("sato")
    expect(page.locator("#user-tbody")).to_contain_text("sato")
    expect(page.locator("#user-tbody")).not_to_contain_text("admin")

    # 検索: 表示名の部分一致。
    page.locator("#f-q").fill("管理者")
    expect(page.locator("#user-tbody")).to_contain_text("admin")
    expect(page.locator("#user-tbody")).not_to_contain_text("sato")

    # 検索: メールの部分一致。
    page.locator("#f-q").fill("sato@example.com")
    expect(page.locator("#user-tbody")).to_contain_text("sato")
    expect(page.locator("#user-tbody")).not_to_contain_text("admin")

    page.locator("#f-q").fill("")

    # 新規ユーザーを追加してから無効化＝状態フィルターの検証材料にする。
    page.locator("#nu-uid").fill("yamada")
    page.locator("#nu-name").fill("山田次郎")
    page.locator("#nu-pw").fill("initial-pass")
    page.locator("#nu-submit").click()
    expect(page.locator("#user-count")).to_have_text("(3 人)")

    page.locator("#user-tbody [data-edit='yamada']").click()
    page.locator("#edit-status").select_option("disabled")
    page.locator("#edit-submit").click()
    expect(page.locator("#edit-overlay")).to_be_hidden()

    # 既定（有効のみ）では無効化済み yamada が畳まれる。
    expect(page.locator("#user-count")).to_have_text("(2/3 人)")
    expect(page.locator("#user-tbody")).not_to_contain_text("yamada")

    # 「無効のみ」では yamada だけが見える。
    page.locator("#f-status").select_option("disabled")
    expect(page.locator("#user-count")).to_have_text("(1/3 人)")
    expect(page.locator("#user-tbody")).to_contain_text("yamada")
    expect(page.locator("#user-tbody")).not_to_contain_text("sato")

    # 「すべて」では3人とも見える。
    page.locator("#f-status").select_option("all")
    expect(page.locator("#user-count")).to_have_text("(3 人)")
    expect(page.locator("#user-tbody")).to_contain_text("yamada")
    expect(page.locator("#user-tbody")).to_contain_text("admin")
    expect(page.locator("#user-tbody")).to_contain_text("sato")


def test_admin_users_create_duplicate_uid_shows_409_error_and_does_not_add_row(page, web_base_url):
    """RV「バッチ2」4番（2026-07-03）: 既存 uid（例: admin）で「追加」すると 409 のエラーが
    フォームに表示され、ユーザー一覧に新規行は追加されない（既存の汎用エラー表示だけで
    サーバの 409 メッセージがそのまま届くことの確認）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")
    expect(page.locator("#user-count")).to_have_text("(2 人)")

    page.locator("#nu-uid").fill("admin")   # 既存の uid（mock_api.USERS の1件目）
    page.locator("#nu-name").fill("乗っ取り")
    page.locator("#nu-pw").fill("attacker-pass")
    page.locator("#nu-submit").click()

    expect(page.locator("#nu-err")).to_contain_text("既に存在します")
    expect(page.locator("#user-count")).to_have_text("(2 人)")   # 行は増えていない


def test_admin_users_denies_non_admin_user(page, web_base_url):
    from playwright.sync_api import expect

    records = install_api_mocks(page, user=USER_MEMBER)
    page.goto(f"{web_base_url}/admin-users.html")

    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#main-content")).to_be_hidden()
    assert records["admin_users_post"] == []


def test_admin_users_mock_patch_matches_real_no_diff_422_contract(page, web_base_url):
    """USR-1 RV2 b3: e2e mock の PATCH /admin/users/{uid} は実サーバと同じく、実差分が
    無ければ 422 を返す（UI は無編集時に送らないため通常経路では踏まないが、直PATCHの
    契約モックとしてドリフトしないことを固定する）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")
    expect(page.locator("#user-count")).to_have_text("(2 人)")

    # sato の現在値（role="user"）と同じ値を送っても実差分は無い＝422。
    no_diff_status = page.evaluate("""async () => {
      const res = await fetch('/admin/users/sato', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({role: 'user'}),
      });
      return res.status;
    }""")
    assert no_diff_status == 422

    # 実際に異なる値を送れば実差分あり＝200。
    real_diff_status = page.evaluate("""async () => {
      const res = await fetch('/admin/users/sato', {
        method: 'PATCH', headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({role: 'admin'}),
      });
      return res.status;
    }""")
    assert real_diff_status == 200


def test_admin_users_mock_patch_rejects_invalid_status_and_role_values(page, web_base_url):
    """USR-1 RV3: mock の PATCH も実APIと同じ allowlist 検証を行い、status="pending" や
    空文字の role/status を422で拒否する（is not None 判定＝空文字は「未指定」ではなく
    範囲外の値として拒否）。拒否時は該当行の状態も変わらない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/admin-users.html")
    expect(page.locator("#user-count")).to_have_text("(2 人)")

    for payload in (
        {"status": "pending"},
        {"role": "", "display_name": "変更名"},
        {"status": "", "display_name": "変更名"},
    ):
        status = page.evaluate("""async (body) => {
          const res = await fetch('/admin/users/admin', {
            method: 'PATCH', headers: {'Content-Type': 'application/json'},
            body: JSON.stringify(body),
          });
          return res.status;
        }""", payload)
        assert status == 422, payload

    page.reload()
    admin_row = page.locator("#user-tbody tr", has_text="admin")
    expect(admin_row).to_contain_text("管理者")
    expect(admin_row).to_contain_text("有効")
    expect(page.locator("#user-tbody")).not_to_contain_text("変更名")


def test_admin_users_edit_pending_option_only_for_pending_users(page, web_base_url):
    """USR-1 RV3: 「保留（現状のまま）」選択肢は元状態が pending のユーザーの編集ダイアログに
    だけ現れる（active/disabled のユーザーでは選べない＝誤って pending へ変更しようとして
    実APIの422になる混乱を防ぐ）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, extra_users=[
        {"uid": "yokota", "email": "yokota@example.com", "display_name": "横田三郎",
         "role": "user", "status": "pending", "must_change_password": False, "last_login_at": None},
    ])
    page.goto(f"{web_base_url}/admin-users.html")

    # active ユーザー（admin）の編集では pending 選択肢が無い。
    page.locator("#user-tbody [data-edit='admin']").click()
    expect(page.locator("#edit-overlay")).to_be_visible()
    assert page.locator("#edit-status option[value='pending']").count() == 0
    page.keyboard.press("Escape")
    expect(page.locator("#edit-overlay")).to_be_hidden()

    # pending ユーザー（yokota）の編集では選択肢が現れ、選択済みで表示される。
    page.locator("#f-status").select_option("all")
    page.locator("#user-tbody [data-edit='yokota']").click()
    expect(page.locator("#edit-status option[value='pending']")).to_have_count(1)
    expect(page.locator("#edit-status")).to_have_value("pending")
    page.keyboard.press("Escape")
    expect(page.locator("#edit-overlay")).to_be_hidden()

    # 続けて active ユーザー（sato）を編集すると pending 選択肢は残っていない（張り替え確認）。
    page.locator("#f-status").select_option("active")
    page.locator("#user-tbody [data-edit='sato']").click()
    assert page.locator("#edit-status option[value='pending']").count() == 0
