"""ホーム画面（運営掲示板）の e2e（S4・掲示板の公開/削除タイマー）。

一般ユーザー向けの可視性境界（scheduled/expired が出ない・publish_at/expire_at の絞り込み）は
実 DB を使う tests/api/test_announcements.py で厳密に固定済み。ここではフロント固有の振る舞い
（datetime-local ⇄ ISO(UTC) 変換・状態バッジ表示・編集フォームのプレフィル・クリアの空文字送信）
を mock 経由で確認する。
"""
from __future__ import annotations

from mock_api import USER_MEMBER, install_api_mocks


def test_home_admin_post_form_collapsed_by_default_and_toggles(page, web_base_url):
    """投稿フォームは既定で折りたたみ＝お知らせ一覧を先に見せる。
    「＋ お知らせを投稿」で展開・「キャンセル」で元の折りたたみに戻る。展開後は本文入力欄
    （#pf-title）へ、キャンセル後は再表示された開くボタン（#pf-open）へフォーカスが移る。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/home.html")

    # 既定＝折りたたみ（トリガーボタンのみ・フォーム本体は無い）。
    expect(page.locator("#pf-open")).to_be_visible()
    expect(page.locator("#pf-open")).to_have_text("＋ お知らせを投稿")
    expect(page.locator("#pf-title")).to_have_count(0)

    page.locator("#pf-open").click()
    expect(page.locator("#pf-title")).to_be_visible()
    expect(page.locator("#pf-open")).to_have_count(0)
    expect(page.locator("#pf-title")).to_be_focused()

    page.locator("#pf-cancel").click()
    expect(page.locator("#pf-open")).to_be_visible()
    expect(page.locator("#pf-title")).to_have_count(0)
    expect(page.locator("#pf-open")).to_be_focused()


def test_home_admin_post_form_blocks_cancel_while_submitting_and_collapses_on_success(page, web_base_url):
    """送信中はキャンセル・再送信を無効化する（無効化しないと、POST 待機中にキャンセルで
    フォーム DOM が破棄され、その後 POST が成功した際に一覧更新・成功通知が走らないまま
    サーバには投稿済み＝二重投稿を誘発する）。成功後はフォームが折りたたみへ戻り、
    一覧に新着が反映される。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    held: dict = {}

    def hold_create(route):
        if route.request.method == "POST" and route.request.url.endswith("/admin/announcements"):
            held["route"] = route   # fulfill せず保留＝送信中の状態を作る
            return
        route.fallback()   # 対象外（GET 一覧等）は install_api_mocks の catch-all に委譲

    page.route("**/admin/announcements", hold_create)
    page.goto(f"{web_base_url}/home.html")

    page.locator("#pf-open").click()
    page.locator("#pf-title").fill("新しいお知らせ")
    page.locator("#pf-body").fill("本文です。")
    page.locator("#pf-submit").click()

    # 送信中は無効化される＝Playwright の通常クリックは弾かれる（＝利用者も押せない）。
    # 「万一クリックが届いても外れない」ことまで見るため force で発火させる。
    expect(page.locator("#pf-submit")).to_be_disabled()
    expect(page.locator("#pf-cancel")).to_be_disabled()
    page.locator("#pf-cancel").click(force=True)
    expect(page.locator("#pf-title")).to_be_visible()   # 無効化中はキャンセルが効かない＝開いたまま

    held["route"].fallback()   # 保留していた POST を解放する（install_api_mocks の通常応答へ）

    expect(page.locator("#pf-open")).to_be_visible()   # 成功後は折りたたみへ戻る
    expect(page.locator("#pf-title")).to_have_count(0)
    expect(page.locator("#ann-list")).to_contain_text("新しいお知らせ")
    expect(page.locator("#toast")).to_contain_text("お知らせを投稿しました")
    assert len(records["announcement_create"]) == 1, "二重投稿されていないこと"


def test_home_member_does_not_see_post_form_or_toggle(page, web_base_url):
    """非管理者の表示は現状不変＝投稿フォームもその展開トリガーも出さない。"""
    from playwright.sync_api import expect

    install_api_mocks(page, user=USER_MEMBER)
    page.goto(f"{web_base_url}/home.html")

    expect(page.locator("#ann-card")).to_be_visible()
    expect(page.locator("#pf-open")).to_have_count(0)
    expect(page.locator("#pf-title")).to_have_count(0)
    expect(page.locator("#post-form-wrap")).to_be_empty()


def test_home_admin_scheduled_post_sends_utc_iso_and_shows_badge(page, web_base_url):
    """S4: datetime-local（ローカル=JST 入力）→ ISO(UTC) で送信し、応答の status に応じたバッジが出る。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/home.html")

    page.locator("#pf-open").click()   # 投稿フォームは既定で折りたたみ＝先に展開する
    expect(page.locator("#post-form-wrap")).to_contain_text("お知らせを投稿")
    page.locator("#pf-title").fill("定期メンテナンスの予告")
    page.locator("#pf-body").fill("来週メンテナンスします。")
    # datetime-local はローカル（JST）時刻として入力する。
    page.locator("#pf-publish").fill("2099-01-01T21:00")
    page.locator("#pf-submit").click()

    expect(page.locator("#ann-list")).to_contain_text("定期メンテナンスの予告")
    body = records["announcement_create"][-1]
    # JST 21:00 → UTC 12:00（-9h）。素朴な文字列送信ではなく Date#toISOString() 経由で変換されていること。
    assert body["publish_at"] == "2099-01-01T12:00:00.000Z"
    assert body["expire_at"] is None

    # サーバ（mock）は publish_at が未来＝ status: scheduled を返す＝バッジが表示される。
    row = page.locator("[data-item]", has_text="定期メンテナンスの予告")
    expect(row).to_contain_text("予約公開待ち")


def test_home_admin_edit_form_prefills_local_time_and_clear_sends_empty_string(page, web_base_url):
    """S4: 編集フォームの日時欄は保存済み UTC ISO を端末ロケール（JST）へ逆変換して表示する。
    空にして保存すると expire_at="" が送られ NULL クリアされる（省略=変更しないとは区別）。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page)
    page.goto(f"{web_base_url}/home.html")

    # 種データ（mock ANNOUNCEMENTS・id=901）: expire_at="2026-06-01T09:00:00+00:00"（UTC）＝ JST 18:00。
    # id で行を特定する（編集モードに入るとタイトルは <input value> になり textContent に出ないため）。
    row = page.locator("[data-item='901']")
    expect(row).to_contain_text("掲載終了")
    row.locator("[data-edit]").click()

    expire_input = row.locator("input[type='datetime-local']").nth(1)
    expect(expire_input).to_have_value("2026-06-01T18:00")

    expire_input.fill("")
    row.locator("[data-save]").click()

    expect(page.locator("#toast")).to_contain_text("保存しました")
    body = records["announcement_patch"][-1]
    assert body["expire_at"] == ""   # 明示クリア（未指定ではない）
    expect(row).not_to_contain_text("掲載終了")


# ===== 通知（NOTIFY-1・掲示板とは別区画） =====

def test_home_notifications_empty_state(page, web_base_url):
    """通知が0件なら「通知はありません」を平文で出す（掲示板の空表示と同じ流儀）。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/home.html")

    expect(page.locator("#notif-list")).to_contain_text("通知はありません")


def test_home_member_sees_ingest_run_notification_only(page, web_base_url):
    """取り込み run の完了/失敗は誰でも見える（admin 限定イベントは別テストで確認）。"""
    from playwright.sync_api import expect

    install_api_mocks(page, user=USER_MEMBER, notifications=[{
        "id": "ingest_run:w1:2026-09-03T01:00:00+00:00", "kind": "ingest_run", "world": "w1",
        "world_label": "4期更改", "status": "done", "message": "「4期更改」の取り込みが完了しました。",
        "created_at": "2026-09-03T01:00:00+00:00", "admin_only": False, "action": None,
    }])
    page.goto(f"{web_base_url}/home.html")

    expect(page.locator("#notif-list")).to_contain_text("4期更改")
    expect(page.locator("#notif-list")).to_contain_text("取り込みが完了しました")
    expect(page.locator("#notif-list .notif-actions")).to_have_count(0)   # 操作ボタンは無い


def test_home_admin_ocr_pending_notification_triggers_refresh_without_confirm(page, web_base_url):
    """OCR 反映待ちの通知は確認なしで既存の POST /worlds/{wid}/refresh へ委譲する。"""
    from playwright.sync_api import expect

    records = install_api_mocks(page, notifications=[{
        "id": "ocr_pending:w1:2026-09-03T03:00:00+00:00", "kind": "ocr_pending", "world": "w1",
        "world_label": "4期更改", "status": "warn",
        "message": "「4期更改」の画像文字認識（OCR）が完了しました。反映には更新が必要です。",
        "created_at": "2026-09-03T03:00:00+00:00", "admin_only": True,
        "action": {"label": "更新する", "method": "POST", "path": "/worlds/w1/refresh", "confirm": False},
    }])
    dialogs = []
    page.on("dialog", lambda d: (dialogs.append(d.message), d.accept()))
    page.goto(f"{web_base_url}/home.html")

    page.locator("#notif-list [data-notif-method]").click()

    expect(page.locator("#toast")).to_contain_text("受け付けました")
    assert len(records["world_refresh"]) == 1
    assert dialogs == []   # OCR 反映は確認ダイアログを出さない（グラフ再抽出とは違い費用が無いため）
