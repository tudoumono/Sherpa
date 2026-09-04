from __future__ import annotations

import re

from mock_api import PREVIEW, WORLD, WORLD_STATUS_RESP, install_api_mocks


def test_status_shows_failed_run_and_generic_office_md_warning(page, web_base_url):
    """`last_run_status=failed` と汎用 `office_md:*` warning は、ファイル名付きの
    `office_md_blocked:` 個別表示とは別に、状況欄へ出す（summaryNote）。
    `office_md_blocked:{doc}\\t{reason}` はタブ区切りのため、`doc` 自体に `:` を含んでいても
    正しいファイル名として表示される。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **WORLD_STATUS_RESP,
            "last_run_status": "failed",
            "last_run_warnings": [
                "office_md:derived_publish_failed:OSError",
                "office_md_blocked:a:b.xlsx\tunhandled_exception:RuntimeError",
            ],
        }))
    page.route("**/worlds/w1/status", handle_status)

    page.goto(f"{web_base_url}/ingest.html")

    stat = page.locator('[data-stat="w1"]')
    expect(stat).to_contain_text("前回の取り込みは失敗しました")
    expect(stat).to_contain_text("Office文書のテキスト化処理自体に問題がありました")
    expect(stat).to_contain_text("a:b.xlsx")            # `:` を含むファイル名でも途切れない


def test_status_shows_analyzer_declined_breakdown(page, web_base_url):
    """accepts() 全滅（担当アナライザは居たが内容判定で不採用）の内訳を要約に出す——
    既存の資料種別に該当する「担当なし（資料扱い）」と、該当しない「未対応」を分けて表示する
    （§7 裁定10・summaryText が analyzer_declined/analyzer_declined_as_document を無視していた
    穴を塞ぐ）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **WORLD_STATUS_RESP,
            "analyzer_declined_as_document": 2,
            "analyzer_declined": 3,
        }))
    page.route("**/worlds/w1/status", handle_status)

    page.goto(f"{web_base_url}/ingest.html")

    stat = page.locator('[data-stat="w1"]')
    expect(stat).to_contain_text("担当なし（資料扱い）2 件")
    expect(stat).to_contain_text("未対応 3 件")


def test_status_shows_unreadable_code_file_blocked_message_with_doc_name(page, web_base_url):
    """不可読コードによる全体停止（`unreadable_code_file`）は対象ファイル名付きで状況欄へ出す
    （`last_run_blocked` の doc/reason を使う・`last_run_warnings` の reason のみでは doc が届かない）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **WORLD_STATUS_RESP,
            "last_run_status": "failed",
            "last_run_warnings": ["unreadable_code_file"],
            "last_run_blocked": [{"doc": "PROG.cbl", "reason": "unreadable_code_file"}],
        }))
    page.route("**/worlds/w1/status", handle_status)

    page.goto(f"{web_base_url}/ingest.html")

    stat = page.locator('[data-stat="w1"]')
    expect(stat).to_contain_text("取り込みを止めました")
    expect(stat).to_contain_text("PROG.cbl")
    expect(stat).to_contain_text("読み取れませんでした")


def test_document_list_shows_unreadable_as_distinct_from_ready(page, web_base_url):
    """文書一覧の `state=unreadable` は `STATE.ready`（既定）へ倒れず「読み取り不可」と表示する。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_preview(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **PREVIEW,
            "documents": [
                {"name": "PROG.cbl", "path": "PROG.cbl", "doctype": None, "branch": None, "analyzer": None,
                 "state": "unreadable", "label": "読み取れません", "reason": "read_failed",
                 "folder": "", "top_scope": "", "phase": "", "category": ""},
            ],
        }))
    page.route("**/ingest/preview*", handle_preview)

    page.goto(f"{web_base_url}/ingest.html")

    row = page.locator("#rows tr", has_text="PROG.cbl")
    expect(row).to_contain_text("読み取り不可")
    expect(row).not_to_contain_text("使えます")


def test_document_list_shows_unknown_when_last_run_status_unavailable(page, web_base_url):
    """`state=unknown`（直近 run の blocked 確認自体ができなかった）は `STATE.ready` へ倒れず
    「状態を確認できませんでした」と表示する（黙って「使えます」にしない）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_preview(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **PREVIEW,
            "documents": [
                {"name": "PROG.cbl", "path": "PROG.cbl", "doctype": "cobol", "branch": "source",
                 "analyzer": "cobol",
                 "state": "unknown", "label": "状態を確認できませんでした", "reason": None,
                 "folder": "", "top_scope": "", "phase": "", "category": ""},
            ],
        }))
    page.route("**/ingest/preview*", handle_preview)

    page.goto(f"{web_base_url}/ingest.html")

    row = page.locator("#rows tr", has_text="PROG.cbl")
    expect(row).to_contain_text("状態を確認できませんでした")
    expect(row).not_to_contain_text("使えます")

    page.click('[data-state="failed"]')
    expect(row).to_be_visible()                        # 失敗フィルタでも消えない


def test_unreadable_row_survives_failure_filter_and_shows_reason_and_rerun(page, web_base_url):
    """`state=unreadable` の行は「⚠ 失敗」フィルタで消えず、理由・「やり直す」も出す
    （失敗の一種として扱う・専門用語ゼロの3状態集約とは別に個別バッジは維持）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_preview(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **PREVIEW,
            "documents": [
                {"name": "PROG.cbl", "path": "PROG.cbl", "doctype": "cobol", "branch": "source",
                 "analyzer": "cobol",
                 "state": "unreadable", "label": "読み取れません", "reason": "unreadable_code_file",
                 "folder": "", "top_scope": "", "phase": "", "category": ""},
            ],
        }))
    page.route("**/ingest/preview*", handle_preview)

    page.goto(f"{web_base_url}/ingest.html")

    page.click('[data-state="failed"]')
    row = page.locator("#rows tr", has_text="PROG.cbl")
    expect(row).to_be_visible()
    expect(row).to_contain_text("読み取り不可")
    expect(row).to_contain_text("コードを読み取れなかったため取り込みを止めました")
    expect(row).not_to_contain_text("unreadable_code_file")
    expect(row.locator('button[data-rerun="PROG.cbl"]')).to_be_visible()


def test_ingest_new_redirects_to_merged_page(page, web_base_url):
    """S3-A: 旧「資料を取り込む」(ingest-new.html) は統合画面 ingest.html へリダイレクトする
    （ブックマーク救済の薄いリダイレクト。機能・実体は ingest.html 側）。"""
    install_api_mocks(page)
    page.goto(f"{web_base_url}/ingest-new.html")
    page.wait_for_url("**/ingest.html")
    # 統合画面（上=資料フォルダ／下=取り込み状況）が実際に描画される。
    from playwright.sync_api import expect

    expect(page.locator("#list")).to_contain_text("4期更改")
    expect(page.locator("#rows")).to_contain_text("税計算仕様書.md")


def test_folder_picker_diff_and_register_flow(page, web_base_url):
    from playwright.sync_api import expect

    import json

    records = install_api_mocks(page)
    # 単一World契約: 登録フォームは未登録のときだけ出る＝この流れは未登録状態から始める。
    state = {"registered": False}

    def handle_worlds(route):
        if route.request.method == "POST":
            state["registered"] = True
            # ING-3: 登録は即受付・取り込みは背景実行（{ok, world_id, run_id, joined, note}）。
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "world_id": "w1", "run_id": 501, "joined": False,
                "note": "受け付けました。状況は取り込み状況でご確認ください。",
            }))
            records["world_register"].append(route.request.post_data_json)
            return
        worlds = [WORLD] if state["registered"] else []
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"worlds": worlds}))

    page.route("**/worlds", handle_worlds)
    page.goto(f"{web_base_url}/ingest.html")

    expect(page.locator("#regcard")).to_be_visible()          # 未登録＝登録フォームが出る
    expect(page.locator("#currentcard")).to_be_hidden()

    page.locator("#pickbtn").click()
    expect(page.locator("#ovl")).to_have_class(re.compile(r"\bopen\b"))
    page.locator("#pbody [data-cd='/mnt/c']").click()
    page.locator("#pbody [data-cd='/mnt/c/ProjectA']").click()
    page.locator("#pchoose").click()

    expect(page.locator("#chosen")).to_contain_text("/mnt/c/ProjectA")
    expect(page.locator("#label")).to_have_value("ProjectA")
    expect(page.locator("#diffbtn")).to_be_enabled()
    expect(page.locator("#regbtn")).to_be_enabled()

    page.locator("#diffbtn").click()
    expect(page.locator("#diffout")).to_contain_text("追加 2")
    expect(page.locator("#diffout")).to_contain_text("TAXCALC.cbl")

    page.locator("#regbtn").click()
    expect(page.locator("#regmsg")).to_contain_text("受け付けました")
    # 登録が済んだら登録フォームは消え、登録中のフォルダと操作だけが残る（更新の入口は1つ）
    expect(page.locator("#regcard")).to_be_hidden()
    expect(page.locator("#currentcard")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("4期更改")
    expect(page.locator('[data-refresh="w1"]')).to_be_visible()

    assert records["world_diff"][-1]["path"] == "/mnt/c/ProjectA"
    assert records["world_register"][-1]["path"] == "/mnt/c/ProjectA"
    assert records["world_register"][-1]["label"] == "ProjectA"


def test_register_success_resyncs_status_section(page, web_base_url):
    """RV High1（2026-07-08）: 資料フォルダを登録すると、下段（取り込み状況）が自動で再同期され、
    登録した資料フォルダの文書が表示される。単一World契約では未登録→1件の境界で確認する
    （選択の余地が無いため、下段セレクタは常に非表示）。
    """
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    new_world = {"world_id": "w2", "label": "ProjectA", "root_path": "/mnt/c/ProjectA",
                 "storage_mode": "external_reference"}
    new_preview = {**PREVIEW, "documents": [
        {"name": "新フォルダ文書.md", "doctype": "md", "state": "ready", "branch": "office", "analyzer": None,
         "folder": "", "top_scope": "ProjectA"},
    ]}
    state = {"registered": False}

    def handle_worlds(route):
        if route.request.method == "POST":
            state["registered"] = True
            # ING-3: 登録は即受付・取り込みは背景実行（{ok, world_id, run_id, joined, note}）。
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "world_id": new_world["world_id"], "run_id": 502, "joined": False,
                "note": "受け付けました。状況は取り込み状況でご確認ください。",
            }))
            return
        worlds = [new_world] if state["registered"] else []      # 未登録 → 登録で1件
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"worlds": worlds}))

    def handle_preview(route):
        body = new_preview if (state["registered"] and "world=w2" in route.request.url) else PREVIEW
        route.fulfill(status=200, content_type="application/json", body=json.dumps(body))

    def handle_status(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {"ok": True, "indexed": 1, "office_md": 1, "skipped_office": 0, "office_failed": 0,
             "skipped_other": 0, "graph_nodes": 0, "es_chunks": 1}))

    page.route("**/worlds", handle_worlds)
    page.route("**/ingest/preview**", handle_preview)
    page.route("**/worlds/*/status", handle_status)

    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#regcard")).to_be_visible()                 # 未登録＝登録フォーム
    expect(page.locator("#version")).to_be_hidden()                  # 選択の余地なし＝常に非表示

    page.locator("#pickbtn").click()
    page.locator("#pbody [data-cd='/mnt/c']").click()
    page.locator("#pbody [data-cd='/mnt/c/ProjectA']").click()
    page.locator("#pchoose").click()
    page.locator("#regbtn").click()

    expect(page.locator("#regmsg")).to_contain_text("受け付けました")
    # RV High1: 下段が自動で再同期され、登録した資料フォルダの文書が表示される。
    expect(page.locator("#rows")).to_contain_text("新フォルダ文書.md")
    expect(page.locator("#version")).to_have_value("w2")
    expect(page.locator("#version")).to_be_hidden()                  # 1件でも選択UIは出さない


def test_delete_returns_screen_to_unregistered_state(page, web_base_url):
    """単一World契約: 登録中の資料フォルダを削除すると未登録状態へ戻り、
    別のフォルダを登録できる画面（登録フォーム）が再び出る。

    ING-3: 削除も即受付・派生物wipeは背景実行。受付直後は行に「検索用データを削除しています」の
    進捗が出て（`running_progress`）、行のポーリング（loadStat）が完了（status が 404）を検知すると
    一覧が自動で未登録状態へ戻る。3秒間隔のポーリングは `page.clock` で早送りして確認する。
    """
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    state = {"accepted": False, "done": False}

    def handle_worlds(route):
        worlds = [] if state["done"] else [WORLD]
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"worlds": worlds}))

    def handle_delete(route):
        state["accepted"] = True
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "world_id": "w1", "run_id": 601, "joined": False,
            "note": "受け付けました。削除が完了すると一覧から消えます。",
        }))

    def handle_status(route):
        if not state["accepted"]:
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"ok": True, "world_id": "w1", "indexed": 1, "office_md": 0, "skipped_office": 0,
                 "office_failed": 0, "skipped_other": 0, "graph_nodes": 0, "es_chunks": 1,
                 "running_progress": None}))
            return
        if not state["done"]:
            state["done"] = True   # 次回ポーリングから完了（404）を返す
            route.fulfill(status=200, content_type="application/json", body=json.dumps(
                {"ok": True, "world_id": "w1", "indexed": 1, "office_md": 0, "skipped_office": 0,
                 "office_failed": 0, "skipped_other": 0, "graph_nodes": 0, "es_chunks": 1,
                 "running_progress": {"stage": "deleting", "stage_label": "検索用データを削除しています",
                                       "done": None, "total": None,
                                       "updated_at": "2026-09-01T00:00:00+00:00"}}))
            return
        route.fulfill(status=404, content_type="application/json", body=json.dumps(
            {"detail": "資料フォルダが見つかりません"}))

    page.route("**/worlds", handle_worlds)
    page.route("**/worlds/w1", handle_delete)
    page.route("**/worlds/w1/status", handle_status)

    page.goto(f"{web_base_url}/ingest.html")
    page.clock.install()
    expect(page.locator("#currentcard")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("4期更改")
    expect(page.locator("#regcard")).to_be_hidden()        # 登録済み＝登録フォームは出さない

    page.once("dialog", lambda d: d.accept())
    page.locator('[data-del="w1"]').click()

    # 受付直後: まだ一覧に残るが「削除中」の進捗（1段のみ）が行に出る
    expect(page.locator('[data-stat="w1"]')).to_contain_text("検索用データを削除しています")

    # 自己ポーリング（3秒間隔）を進め、削除完了（status 404）を検知させる
    page.clock.fast_forward(3500)

    # 削除 → 未登録状態へ戻り、別フォルダを登録できるようになる
    expect(page.locator("#regcard")).to_be_visible()
    expect(page.locator("#currentcard")).to_be_hidden()
    expect(page.locator("#list")).not_to_contain_text("4期更改")


def test_ingest_status_page_shows_loading_then_rows(page, web_base_url):
    """UI フィードバック5（2026-07-03）: 取り込み状況ページ（ingest.html）の一覧・ツリーに
    読み込み中表示（既存の .loading-inline/spinner 流儀）が出て、データ到着後に一覧へ切り替わる。

    既定モックの /ingest/preview は即応答するため、保留してから明示的に fulfill する手法で
    ローディング状態を決定的に観測する（参照: [[feedback_e2e_sse_mock_timing]] と同種の手法）。
    """
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    pending: dict = {}

    def handle_preview(route):
        pending["route"] = route

    page.route("**/ingest/preview**", handle_preview)
    page.goto(f"{web_base_url}/ingest.html")

    expect(page.locator("#rows")).to_contain_text("読み込み中")
    expect(page.locator("#tree")).to_contain_text("読み込み中")

    pending["route"].fulfill(status=200, content_type="application/json", body=json.dumps(PREVIEW))

    expect(page.locator("#rows")).to_contain_text("税計算仕様書.md")
    expect(page.locator("#rows")).not_to_contain_text("読み込み中")


def test_no_world_shows_plain_empty_state_not_stuck_spinner(page, web_base_url):
    """UI-ING是正1（利用者報告 2026-09-03）: 資料フォルダが1件も登録されていないと、範囲ツリー・
    一覧のスピナーがずっと回り続けていた不具合の修正。`/ingest/preview` は world 未指定/空文字を
    422 で拒否する（バックエンド側は妥当な入口検証）ため、素通しで fetch すると `documents` を
    欠いた応答で例外が起き、スピナーが `_LOADING_INLINE` のまま固まっていた。worlds 0件は
    fetch 自体を行わず平文の空状態へ倒す。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/worlds", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"worlds": []})))

    preview_calls = []

    def handle_preview(route):
        preview_calls.append(route.request.url)
        # 実バックエンドの実際の挙動（world 未指定/空文字は 422）を再現する。
        route.fulfill(status=422, content_type="application/json", body=json.dumps(
            {"detail": [{"type": "string_pattern_mismatch", "loc": ["query", "world"]}]}))
    page.route("**/ingest/preview**", handle_preview)

    page.goto(f"{web_base_url}/ingest.html")

    expect(page.locator("#tree")).to_contain_text("まだ資料フォルダが登録されていません")
    expect(page.locator("#rows")).to_contain_text("まだ資料フォルダが登録されていません")
    expect(page.locator("#tree .spinner")).to_have_count(0)
    expect(page.locator("#rows .spinner")).to_have_count(0)
    assert preview_calls == []      # worlds 0件は /ingest/preview 自体を呼ばない


def test_preview_failure_shows_plain_error_not_stuck_spinner(page, web_base_url):
    """UI-ING是正1（利用者報告 2026-09-03）: `/ingest/preview` が失敗（503等）してもスピナーが
    残らず、平文のエラー表示に倒れる（`load()` を try/catch で包む・worlds は1件登録済みの通常経路）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    page.route("**/ingest/preview**", lambda route: route.fulfill(
        status=503, content_type="application/json",
        body=json.dumps({"detail": "グラフを読み込めません。しばらくしてからお試しください"})))

    page.goto(f"{web_base_url}/ingest.html")

    expect(page.locator("#tree")).to_contain_text("取り込み状況を取得できません")
    expect(page.locator("#rows")).to_contain_text("取り込み状況を取得できません")
    expect(page.locator("#tree .spinner")).to_have_count(0)
    expect(page.locator("#rows .spinner")).to_have_count(0)


def test_register_shows_optimistic_row_before_world_appears(page, web_base_url):
    """UI-ING是正2（利用者報告 2026-09-03）: 登録受付直後、`GET /worlds` にまだ現れていなくても
    （背景で世界行を作成中）、受付応答自身が返す `world_id` で「取り込み中…」の楽観的な
    プレースホルダ行を即時表示する（`trackNewRegistration` が実際の行を検出するまで待たせない）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)
    state = {"registered": False}

    def handle_worlds(route):
        if route.request.method == "POST":
            state["registered"] = True
            route.fulfill(status=200, content_type="application/json", body=json.dumps({
                "ok": True, "world_id": "w9", "run_id": 901, "joined": False,
                "note": "受け付けました。状況は取り込み状況でご確認ください。",
            }))
            return
        # 受付直後は GET /worlds にまだ現れない（世界行の作成自体が背景処理のため）。
        route.fulfill(status=200, content_type="application/json", body=json.dumps({"worlds": []}))
    page.route("**/worlds", handle_worlds)
    # trackNewRegistration の run 追跡（未検出のまま extracting 継続扱い）——本テストは
    # プレースホルダの即時表示だけを見るため、これ以上は進めない。
    page.route("**/ingest/runs**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps({"world": "w9", "runs": []})))

    page.goto(f"{web_base_url}/ingest.html")
    page.clock.install()      # 追跡ループの再ポーリング（setTimeout）を進めない＝この状態で固定して観察

    page.locator("#pickbtn").click()
    page.locator("#pbody [data-cd='/mnt/c']").click()
    page.locator("#pbody [data-cd='/mnt/c/ProjectA']").click()
    page.locator("#pchoose").click()
    page.locator("#regbtn").click()

    expect(page.locator("#regcard")).to_be_hidden()
    expect(page.locator("#currentcard")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("ProjectA")
    expect(page.locator("#list")).to_contain_text("取り込み中")


def test_ingest_documents_show_provenance_badges(page, web_base_url):
    """S2: 各文書に「どう読み取ったか」の平文バッジ（専門用語ゼロ）が出る。データ源は取り込み時の来歴。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/ingest.html")

    expect(page.locator("#rows")).to_contain_text("旧料金表.xls")
    rows = page.locator("#rows")
    # method バッジ（Office 直接読み取り / 旧形式変換 / 視覚読み取り=markitdown_ocr）。
    expect(rows).to_contain_text("Office から直接読み取り")
    expect(rows).to_contain_text("旧形式を変換してから読み取り（LibreOffice）")
    expect(rows).to_contain_text("AI が画像を見て読み取り（数値は要確認）")
    # 照合差分（注意トーン・ツールチップ付き）。
    expect(rows).to_contain_text("照合で差分あり")
    tip = rows.locator(".provbadge.warn").first
    expect(tip).to_have_attribute("title", "別の方法で読むと追加の内容が見つかりました。原本を確認してください")


def test_ingest_document_row_shows_analyzer_provenance(page, web_base_url):
    """コード文書（branch=source）の行に「解析: <表示名>」を出す（§7 裁定2の受入条件＝取り込み
    画面の根拠表示で担当アナライザを参照できるようにする）。内部名（analyzer=cobol）はそのまま
    出さず表示ラベル（COBOL）にする。資料（branch=office）の行には出さない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/ingest.html")

    cobol_row = page.locator("#rows tr", has_text="TAXCALC.cbl")
    expect(cobol_row).to_contain_text("解析: COBOL")
    expect(cobol_row).not_to_contain_text("解析: cobol")

    office_row = page.locator("#rows tr", has_text="税計算仕様書.md")
    expect(office_row).not_to_contain_text("解析:")


def test_ingest_document_row_analyzer_badge_uses_analyzer_not_doctype(page, web_base_url):
    """「解析:」表示は `analyzer`（Analyzer.name）を使う——`doctype`（種別表示用の別項目）とは
    独立していることを、name≠doctype のダミー言語で固定する（doctype をそのまま使う実装への
    回帰を検出する・§7 裁定2）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_preview(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **PREVIEW,
            "documents": [
                {"name": "thing.dummy", "path": "thing.dummy", "doctype": "ダミー言語",
                 "branch": "source", "analyzer": "dummylang", "state": "ready",
                 "label": "使えます", "reason": None,
                 "folder": "", "top_scope": "", "phase": "", "category": ""},
            ],
        }))
    page.route("**/ingest/preview*", handle_preview)

    page.goto(f"{web_base_url}/ingest.html")

    row = page.locator("#rows tr", has_text="thing.dummy")
    # 未知の名前は加工せずそのまま表示する（大文字化しない・Sherpa.analyzerLabel の契約）。
    expect(row).to_contain_text("解析: dummylang")
    expect(row).not_to_contain_text("解析: ダミー言語")


def test_ingest_preview_panel_shows_analyzer_provenance(page, web_base_url):
    """プレビュー（抽出された要素）の各項目にも担当アナライザの来歴を出す（§7 裁定2）。
    コード分（TAX-RATE・analyzer=cobol）だけに出て、文書分（税計算仕様書.md・analyzer=None）には出ない。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/ingest.html")
    page.click("#detailbtn")

    ents = page.locator("#pv-ents")
    tax_rate_ent = ents.locator(".ent", has_text="TAX-RATE")
    expect(tax_rate_ent).to_contain_text("解析: COBOL")

    doc_ent = ents.locator(".ent", has_text="税計算仕様書.md")
    expect(doc_ent).not_to_contain_text("解析:")


def test_ingest_es_hit_shows_provenance(page, web_base_url):
    """S2: 全文検索のヒットカードに由来（例「AI が画像から読み取り」）と照合差分の注意を表示する。"""
    from playwright.sync_api import expect

    install_api_mocks(page)
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#rows")).to_contain_text("旧料金表.xls")   # 一覧読み込み完了を待つ

    page.locator("#esq").fill("料金")
    page.locator("#esbtn").click()

    hits = page.locator("#eshits")
    expect(hits).to_contain_text("スキャン図面.pdf")
    expect(hits).to_contain_text("AI が画像から読み取り")
    expect(hits).to_contain_text("照合で差分あり")


def test_ingest_legacy_ocr_method_shows_backward_compat_badge(page, web_base_url):
    """RV Med（Codex gpt-5.5/xhigh・2026-07-08 R1）: tesseract 撤去（`ocr` アーム廃止）前に作られた
    派生 md の来歴（method="ocr"／extraction_method="ocr"）は、次回の派生 md 全再ビルドまで既存の
    meta.json/ES メタに残る。バッジ／ヒットカードはこの旧値でも無表示にならず「旧方式」だとわかる
    平文で表示互換を保つ（新規で method="ocr" が作られることはもう無い＝表示のみの後方互換）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    legacy_preview = {**PREVIEW, "documents": [
        ({**d, "provenance": {"method": "ocr", "confidence": 0.4}}
         if d["name"] == "4期/02_設計/01_基本設計/スキャン図面.pdf" else d)
        for d in PREVIEW["documents"]
    ]}

    def handle_preview(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps(legacy_preview))

    def handle_es_search(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "world": "w1", "query": "料金", "scope_paths": [], "hits": [
                {"doc_id": "4期/02_設計/01_基本設計/スキャン図面.pdf", "line": 3,
                 "snippet": "旧方式で読み取った本文。", "score": 2.1, "ext": ".pdf",
                 "extraction_method": "ocr", "confidence": 0.4},
            ]}))

    page.route("**/ingest/preview**", handle_preview)
    page.route("**/admin/es/search**", handle_es_search)

    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#rows")).to_contain_text("スキャン図面.pdf")
    expect(page.locator("#rows")).to_contain_text("画像から文字を読み取り（旧方式）")   # 一覧バッジ（後方互換）

    page.locator("#esq").fill("料金")
    page.locator("#esbtn").click()
    hits = page.locator("#eshits")
    expect(hits).to_contain_text("スキャン図面.pdf")
    expect(hits).to_contain_text("画像から読み取り（旧）")                              # ヒットカード（後方互換）


def test_detail_button_admin_only(page, web_base_url):
    """「詳細（管理）」は admin のみ表示（中身が /admin/ API 依存・S3 RV 指摘の回帰ガード）。

    既定モックユーザー=admin では表示、非 admin（USER_MEMBER）と /auth/me 失敗時は
    fail-safe で非表示のまま。
    """
    from playwright.sync_api import expect

    from mock_api import USER_MEMBER

    install_api_mocks(page)                       # 既定=admin
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#detailbtn")).to_be_visible()

    install_api_mocks(page, user=USER_MEMBER)     # 非 admin（後掛けの route が優先される）
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#detailbtn")).to_be_hidden()

    page.route("**/auth/me", lambda route: route.fulfill(status=500, body="{}"))
    page.goto(f"{web_base_url}/ingest.html")      # 判定失敗＝fail-safe で非表示
    expect(page.locator("#detailbtn")).to_be_hidden()


def test_page_admin_only(page, web_base_url):
    """資料フォルダ登録/更新/削除/グラフ生成・取り込み状況（/ingest/preview）・全文検索
    （/admin/es/search）は全て admin 限定 API のため、この画面全体を admin-settings.html /
    audit.html と同じ「非 admin は access-denied だけ見せる」パターンで丸ごとガードする
    （W1・共有ナレッジの更新は管理者のみ・2026-09-03）。

    admin-settings.html と同じ nav の出し分け（`web/nav.js`）でも「資料」タブは admin のみ表示。
    """
    from playwright.sync_api import expect

    from mock_api import USER_MEMBER

    install_api_mocks(page)                       # 既定=admin
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#main-content")).to_be_visible()
    expect(page.locator("#access-denied")).to_be_hidden()
    expect(page.locator('.nav a[href="ingest.html"]')).to_be_visible()

    install_api_mocks(page, user=USER_MEMBER)     # 非 admin（後掛けの route が優先される）
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#access-denied")).to_contain_text("管理者権限が必要です")
    # nav にも「資料」タブが出ない（admin-settings.html の「システム管理」と同じ出し分け）。
    expect(page.locator('.nav a[href="ingest.html"]')).to_have_count(0)

    page.route("**/auth/me", lambda route: route.fulfill(status=500, body="{}"))
    page.goto(f"{web_base_url}/ingest.html")      # 判定失敗＝fail-safe で access-denied 側
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#access-denied")).to_be_visible()


def test_importance_badge_and_source_shown_in_ledger(page, web_base_url):
    """台帳（文書一覧）に `_重要度.txt` 由来の重要度バッジが出る（値・理由・由来はホバーで確認）。

    重要度が無い文書（既定モックの他の資料）はバッジ列が空のまま＝後方互換。既定 PREVIEW は
    実 fixtures（`_重要度.txt` 無し）と形状を合わせる契約（test_mock_api_contract）があるため、
    importance フィールドはこのテストだけローカルに追加した preview で確認する。
    """
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    preview_with_importance = {**PREVIEW, "documents": [
        ({**d, "importance": "高", "importance_reason": "税制改正の一次資料",
          "importance_source": "4期/02_設計/_重要度.txt:1行目"}
         if d["name"] == "4期/02_設計/01_基本設計/税計算仕様書.md" else d)
        for d in PREVIEW["documents"]
    ]}
    page.route("**/ingest/preview**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(preview_with_importance)))

    page.goto(f"{web_base_url}/ingest.html")

    row = page.locator("#rows tr", has_text="税計算仕様書.md")
    badge = row.locator(".impbadge")
    expect(badge).to_have_text("高")
    expect(badge).to_have_attribute("title", re.compile("重要度 高.*税制改正の一次資料.*_重要度\\.txt"))

    other_row = page.locator("#rows tr", has_text="TAXCALC.cbl")
    expect(other_row.locator(".impbadge")).to_have_count(0)


def test_importance_control_file_diagnostics_banner(page, web_base_url):
    """`_重要度.txt` の構文エラーはツリー付近の小さな警告バナーに出る（無ければ非表示）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    diag_preview = {**PREVIEW, "importance_diagnostics": [
        {"config_path": "4期/02_設計/_重要度.txt", "line": 3, "column": 1,
         "code": "invalid_value", "message": "値が正しくありません。「高」「中」「低」「なし」のいずれかにしてください"},
    ]}

    page.route("**/ingest/preview**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(diag_preview)))

    page.goto(f"{web_base_url}/ingest.html")
    banner = page.locator("#impdiag")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("_重要度.txt")
    expect(banner).to_contain_text("4期/02_設計/_重要度.txt:3行目")

    # 診断が無い（既定モック）場合はバナーが出ない。
    page.route("**/ingest/preview**", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(PREVIEW)))
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#impdiag")).to_be_hidden()


# ===================================================================================
# ING-2: 集計時刻＋再集計ボタン（`GET /worlds/{id}/status` はキャッシュを読むだけでフォルダを歩かない）
# ===================================================================================

def test_status_shows_counts_as_of_and_recount_button(page, web_base_url):
    """件数の後ろに集計時刻を表示し、「再集計」ボタンを押すと `POST /worlds/{id}/recount` を呼んで
    表示を更新する（未集計＝`counts_as_of=None` は「（未集計）」と表示する）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    page.route("**/worlds/w1/status", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({**WORLD_STATUS_RESP, "counts_as_of": None})))

    page.goto(f"{web_base_url}/ingest.html")
    stat = page.locator('[data-stat="w1"]')
    expect(stat).to_contain_text("（未集計）")

    recount_calls = []

    def handle_recount(route):
        recount_calls.append(route.request.method)
        route.fulfill(status=200, content_type="application/json", body=json.dumps(
            {**WORLD_STATUS_RESP, "ok": True, "world_id": "w1",
             "counts_as_of": "2026-09-01T03:12:00+00:00"}))
    page.route("**/worlds/w1/recount", handle_recount)
    page.route("**/worlds/w1/status", lambda route: route.fulfill(
        status=200, content_type="application/json",
        body=json.dumps({**WORLD_STATUS_RESP, "counts_as_of": "2026-09-01T03:12:00+00:00"})))

    stat.locator('[data-recount="w1"]').click()
    expect(stat).to_contain_text("時点")
    expect(stat).not_to_contain_text("未集計")
    assert recount_calls == ["POST"]


# ===================================================================================
# ING-1: 失敗一覧の詳細折りたたみ＋再変換ボタン
# ===================================================================================

def test_status_detail_shows_failed_files_with_reason_and_reconvert_button(page, web_base_url):
    """「詳細を表示」の折りたたみに失敗ファイル一覧（平文の理由＋対処）と各段の要約を出し、
    各行の「再変換」ボタンが対象 rel を持つ。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    status_with_detail = {
        **WORLD_STATUS_RESP,
        "office_failed": 1,
        "failed_files": {
            "items": [{"doc": "旧料金表.xls", "stage": "legacy_conversion", "reason": "legacy_conversion_timeout"}],
            "total": 1, "truncated": False,
        },
        "stage_summary": {
            "office_md": {"converted": 2, "failed": 1, "unsupported": 0},
            "es": {"available": True, "error": None, "chunks": 6},
            "neo4j": {"nodes": 4, "edges": 3, "duration_sec": 0.5},
        },
        "failure_reason_catalog": {
            "legacy_conversion_timeout": {"label": "タイムアウト", "advice": "時間をおいて再試行してください。"},
        },
    }
    page.route("**/worlds/w1/status", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(status_with_detail)))

    page.goto(f"{web_base_url}/ingest.html")
    stat = page.locator('[data-stat="w1"]')
    details = stat.locator("details.adv")
    expect(details).to_be_visible()
    details.locator("summary").click()
    expect(details).to_contain_text("旧料金表.xls")
    expect(details).to_contain_text("タイムアウト")
    expect(details).to_contain_text("時間をおいて再試行してください")
    expect(details).to_contain_text("MD変換")

    btn = details.locator('[data-reconvert-wid="w1"][data-rel="旧料金表.xls"]')
    expect(btn).to_have_count(1)


def test_reconvert_button_calls_reconvert_endpoint_with_rel(page, web_base_url):
    """失敗一覧の「再変換」ボタンは確認ダイアログの上で `POST /worlds/{id}/reconvert` を
    `{rel}` 付きで呼ぶ。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    status_with_detail = {
        **WORLD_STATUS_RESP,
        "failed_files": {
            "items": [{"doc": "旧料金表.xls", "stage": "legacy_conversion", "reason": "legacy_conversion_timeout"}],
            "total": 1, "truncated": False,
        },
    }
    page.route("**/worlds/w1/status", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(status_with_detail)))

    reconvert_bodies = []

    def handle_reconvert(route):
        reconvert_bodies.append(json.loads(route.request.post_data or "{}"))
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            "ok": True, "world_id": "w1", "rel": "旧料金表.xls", "changed": True, "status": "auto_published",
            "ledger": 3, "flags": [], "summary": WORLD_STATUS_RESP, "note": "更新と同じ処理が走りました。"}))
    page.route("**/worlds/w1/reconvert", handle_reconvert)

    page.goto(f"{web_base_url}/ingest.html")
    stat = page.locator('[data-stat="w1"]')
    stat.locator("details.adv summary").click()

    page.once("dialog", lambda d: d.accept())
    stat.locator('[data-reconvert-wid="w1"][data-rel="旧料金表.xls"]').click()

    expect(page.locator('[data-stat="w1"]')).not_to_contain_text("再変換できません")   # エラーにならず完了する
    assert reconvert_bodies == [{"rel": "旧料金表.xls"}]


def test_pickbtn_disabled_while_ingest_running(page, web_base_url):
    """ING-3b（利用者報告 2026-09-04）: 登録ボタン（`pickbtn`）は、`setIngestBusy` が止める行ボタン
    （refresh/rag-rules/del）と違って world_id に紐付かず対象外だった——`worlds.register` は登録処理
    全体（多くの場合 es_index 段を含み数時間かかりうる）をグローバル advisory lock の下で行うため、
    実行中に別の登録を投げると新規リクエストが lock 待ちで固まって見える。`loadStat` が集計する
    実行中 world 集合が1件でもあれば無効化し、平文の理由（`title`）を添える。実行中でなくなれば
    再び有効化される。

    w1 は既に登録済み（`WORLD`）のため `regcard`（`pickbtn` を含む）自体は非表示だが、`pickbtn` の
    `disabled`/`title` は表示状態と独立に検証できる（要素は DOM に残ったまま）。"""
    import json

    from playwright.sync_api import expect

    install_api_mocks(page)

    def handle_running(route):
        route.fulfill(status=200, content_type="application/json", body=json.dumps({
            **WORLD_STATUS_RESP,
            "running_progress": {"stage": "es_index", "stage_label": "全文索引に登録し、ベクトル化中",
                                 "done": 3, "total": 10, "updated_at": "2026-09-04T00:00:00+00:00"},
        }))
    page.route("**/worlds/w1/status", handle_running)

    page.goto(f"{web_base_url}/ingest.html")

    pickbtn = page.locator("#pickbtn")
    expect(pickbtn).to_be_disabled()
    expect(pickbtn).to_have_attribute("title", "取り込みの実行中は登録できません")

    page.route("**/worlds/w1/status", lambda route: route.fulfill(
        status=200, content_type="application/json", body=json.dumps(WORLD_STATUS_RESP)))
    page.reload()

    expect(pickbtn).to_be_enabled()


# ソース正典化（`docs/proposals/2026-09-04-グラフのソース正典化.md`・S3）: 「グラフを生成」ボタン
# （`data-extract`）・`extractWorld()`・`isGraphExtractFailure`（llm_unavailable/llm_error 案内）は
# web/ingest.js から機構ごと撤去済み（意味層 LLM 抽出は K9 で撤去・バックエンドの `/extract` は
# 後続レーンで撤去予定）。この機構だけを検証していた
# `test_extract_failure_shows_selected_cloud_hint_not_local_ai` は対象機能の消滅により削除。
