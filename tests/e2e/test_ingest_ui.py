from __future__ import annotations

import json
import re

import pytest
from mock_api import PREVIEW, USER_MEMBER, WORLD, WORLD_STATUS_RESP, install_api_mocks
from playwright.sync_api import expect

RUNNING_PROGRESS = {"stage": "es_index", "stage_label": "全文索引に登録し、ベクトル化中",
                    "done": 42, "total": 100, "updated_at": "2026-09-04T00:00:00+00:00"}
ACCEPTED = "受け付けました。状況は取り込み状況でご確認ください。"
SCAN_PDF = "4期/02_設計/01_基本設計/スキャン図面.pdf"


def _fulfill(route, body, status=200):
    route.fulfill(status=status, content_type="application/json", body=json.dumps(body))


def _route_json(page, pattern, body, status=200):
    page.route(pattern, lambda route: _fulfill(route, body, status))


def _open(page, web_base_url, status=None, preview=None, **mock_kw):
    """ingest.html を開く。status／preview を渡すと /worlds/w1/status・/ingest/preview の応答を差し替える。"""
    records = install_api_mocks(page, **mock_kw)
    if status is not None:
        _route_json(page, "**/worlds/w1/status", {**WORLD_STATUS_RESP, **status})
    if preview is not None:
        _route_json(page, "**/ingest/preview**", preview)
    page.goto(f"{web_base_url}/ingest.html")
    return records


def _doc(name, **kw):
    return {"name": name, "path": name, "doctype": None, "branch": None, "analyzer": None,
            "state": "ready", "label": "使えます", "reason": None,
            "folder": "", "top_scope": "", "phase": "", "category": "", **kw}


def _pick_project_a(page):
    page.locator("#pickbtn").click()
    page.locator("#pbody [data-cd='/mnt/c']").click()
    page.locator("#pbody [data-cd='/mnt/c/ProjectA']").click()
    page.locator("#pchoose").click()


@pytest.mark.parametrize("status,texts", [
    ({"last_run_status": "failed",
      "last_run_warnings": ["office_md:derived_publish_failed:OSError",
                            "office_md_blocked:a:b.xlsx\tunhandled_exception:RuntimeError"]},
     # `:` を含むファイル名でも途切れない（doc と reason はタブ区切り）
     ["前回の取り込みは失敗しました", "Office文書のテキスト化処理自体に問題がありました", "a:b.xlsx"]),
    ({"analyzer_declined_as_document": 2, "analyzer_declined": 3},
     ["担当なし（資料扱い）2 件", "未対応 3 件"]),
    ({"unreachable_as_text": 5, "unreachable_by_reason": {"encoding_undetermined": 2, "binary": 1},
      "encoding_partial_count": 4},
     ["本文が読めず対象外 5 件", "文字コード判別不能 2", "バイナリ 1", "一部が化けている（要確認） 4 件"]),
    ({"last_run_status": "failed", "last_run_warnings": ["unreadable_code_file"],
      "last_run_blocked": [{"doc": "PROG.cbl", "reason": "unreadable_code_file"}]},
     ["取り込みを止めました", "PROG.cbl", "読み取れませんでした"]),
], ids=["failed_and_generic_office_md", "analyzer_declined", "unreachable_breakdown", "unreadable_code_blocked"])
def test_status_summary_shows_run_failures_and_breakdowns(page, web_base_url, status, texts):
    """状況欄: 前回の失敗・汎用 office_md warning・アナライザ不採用の内訳・読めず対象外の理由別内訳・
    不可読コードによる停止（対象ファイル名付き）を平文で出す。"""
    _open(page, web_base_url, status=status)
    stat = page.locator('[data-stat="w1"]')
    for text in texts:
        expect(stat).to_contain_text(text)


@pytest.mark.parametrize("doc,texts", [
    (_doc("PROG.cbl", state="unreadable", label="読み取れません", reason="read_failed"),
     ["読み取り不可"]),
    (_doc("PROG.cbl", doctype="cobol", branch="source", analyzer="cobol",
          state="unknown", label="状態を確認できませんでした"),
     ["状態を確認できませんでした"]),
    (_doc("PROG.cbl", doctype="cobol", branch="source", analyzer="cobol",
          state="unreadable", label="読み取れません", reason="unreadable_code_file"),
     ["読み取り不可", "コードを読み取れなかったため取り込みを止めました"]),
], ids=["unreadable", "unknown", "unreadable_code_file"])
def test_document_list_shows_unreadable_and_unknown_as_distinct_from_ready(page, web_base_url, doc, texts):
    """`state=unreadable`／`unknown` は `STATE.ready` へ倒れず（黙って「使えます」にしない）専用の表示になり、
    「⚠ 失敗」フィルタでも消えない。読み取り不可の行は理由と「やり直す」を出す。"""
    _open(page, web_base_url, preview={**PREVIEW, "documents": [doc]})

    row = page.locator("#rows tr", has_text="PROG.cbl")
    for text in texts:
        expect(row).to_contain_text(text)
    expect(row).not_to_contain_text("使えます")
    expect(row).not_to_contain_text("unreadable_code_file")

    page.click('[data-state="failed"]')
    expect(row).to_be_visible()
    if doc["state"] == "unreadable":
        expect(row.locator('button[data-rerun="PROG.cbl"]')).to_be_visible()


def test_ingest_new_redirects_to_merged_page(page, web_base_url):
    """旧「資料を取り込む」(ingest-new.html) は統合画面 ingest.html へリダイレクトする。"""
    install_api_mocks(page)
    page.goto(f"{web_base_url}/ingest-new.html")
    page.wait_for_url("**/ingest.html")
    expect(page.locator("#list")).to_contain_text("4期更改")
    expect(page.locator("#rows")).to_contain_text("税計算仕様書.md")


def test_folder_picker_diff_and_register_flow(page, web_base_url):
    records = install_api_mocks(page)
    state = {"registered": False}   # 登録フォームは未登録のときだけ出る＝未登録状態から始める

    def handle_worlds(route):
        if route.request.method == "POST":
            state["registered"] = True
            _fulfill(route, {"ok": True, "world_id": "w1", "run_id": 501, "joined": False, "note": ACCEPTED})
            records["world_register"].append(route.request.post_data_json)
            return
        _fulfill(route, {"worlds": [WORLD] if state["registered"] else []})

    page.route("**/worlds", handle_worlds)
    page.goto(f"{web_base_url}/ingest.html")

    expect(page.locator("#regcard")).to_be_visible()
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
    """資料フォルダを登録すると、下段（取り込み状況）が自動で再同期され、登録した資料フォルダの文書が表示される。
    単一の資料フォルダ契約では選択の余地が無いため、下段セレクタは常に非表示。"""
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
            _fulfill(route, {"ok": True, "world_id": new_world["world_id"], "run_id": 502, "joined": False,
                             "note": ACCEPTED})
            return
        _fulfill(route, {"worlds": [new_world] if state["registered"] else []})

    def handle_preview(route):
        _fulfill(route, new_preview if (state["registered"] and "world=w2" in route.request.url) else PREVIEW)

    page.route("**/worlds", handle_worlds)
    page.route("**/ingest/preview**", handle_preview)
    _route_json(page, "**/worlds/*/status",
                {"ok": True, "indexed": 1, "office_md": 1, "skipped_office": 0, "office_failed": 0,
                 "skipped_other": 0, "graph_nodes": 0, "es_chunks": 1})

    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#regcard")).to_be_visible()
    expect(page.locator("#version")).to_be_hidden()

    _pick_project_a(page)
    page.locator("#regbtn").click()

    expect(page.locator("#regmsg")).to_contain_text("受け付けました")
    expect(page.locator("#rows")).to_contain_text("新フォルダ文書.md")
    expect(page.locator("#version")).to_have_value("w2")
    expect(page.locator("#version")).to_be_hidden()   # 1件でも選択UIは出さない


def test_register_shows_optimistic_row_before_world_appears(page, web_base_url):
    """登録受付直後、`GET /worlds` にまだ現れていなくても、受付応答の `world_id` で「取り込み中…」の
    楽観的なプレースホルダ行を即時表示する。"""
    install_api_mocks(page)

    def handle_worlds(route):
        if route.request.method == "POST":
            _fulfill(route, {"ok": True, "world_id": "w9", "run_id": 901, "joined": False, "note": ACCEPTED})
            return
        _fulfill(route, {"worlds": []})   # 世界行の作成自体が背景処理のため受付直後はまだ現れない

    page.route("**/worlds", handle_worlds)
    _route_json(page, "**/ingest/runs**", {"world": "w9", "runs": []})

    page.goto(f"{web_base_url}/ingest.html")
    page.clock.install()      # 追跡ループの再ポーリングを進めない＝この状態で固定して観察

    _pick_project_a(page)
    page.locator("#regbtn").click()

    expect(page.locator("#regcard")).to_be_hidden()
    expect(page.locator("#currentcard")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("ProjectA")
    expect(page.locator("#list")).to_contain_text("取り込み中")


def test_delete_returns_screen_to_unregistered_state(page, web_base_url):
    """登録中の資料フォルダを削除すると未登録状態へ戻り、別のフォルダを登録できる画面が再び出る。
    削除も即受付・派生物の削除は背景実行。受付直後は行に「検索用データを削除しています」の進捗が出て、
    行のポーリング（3秒間隔・`page.clock` で早送り）が完了（status が 404）を検知すると一覧が未登録状態へ戻る。"""
    install_api_mocks(page)
    state = {"accepted": False, "done": False}
    counts = {"ok": True, "world_id": "w1", "indexed": 1, "office_md": 0, "skipped_office": 0,
              "office_failed": 0, "skipped_other": 0, "graph_nodes": 0, "es_chunks": 1}

    def handle_delete(route):
        state["accepted"] = True
        _fulfill(route, {"ok": True, "world_id": "w1", "run_id": 601, "joined": False,
                         "note": "受け付けました。削除が完了すると一覧から消えます。"})

    def handle_status(route):
        if not state["accepted"]:
            _fulfill(route, {**counts, "running_progress": None})
        elif not state["done"]:
            state["done"] = True   # 次回ポーリングから完了（404）を返す
            _fulfill(route, {**counts, "running_progress": {
                "stage": "deleting", "stage_label": "検索用データを削除しています",
                "done": None, "total": None, "updated_at": "2026-09-01T00:00:00+00:00"}})
        else:
            _fulfill(route, {"detail": "資料フォルダが見つかりません"}, status=404)

    page.route("**/worlds", lambda route: _fulfill(route, {"worlds": [] if state["done"] else [WORLD]}))
    page.route("**/worlds/w1", handle_delete)
    page.route("**/worlds/w1/status", handle_status)

    page.goto(f"{web_base_url}/ingest.html")
    page.clock.install()
    expect(page.locator("#currentcard")).to_be_visible()
    expect(page.locator("#list")).to_contain_text("4期更改")
    expect(page.locator("#regcard")).to_be_hidden()

    page.once("dialog", lambda d: d.accept())
    page.locator('[data-del="w1"]').click()
    expect(page.locator('[data-stat="w1"]')).to_contain_text("検索用データを削除しています")

    page.clock.fast_forward(3500)
    expect(page.locator("#regcard")).to_be_visible()
    expect(page.locator("#currentcard")).to_be_hidden()
    expect(page.locator("#list")).not_to_contain_text("4期更改")


def test_ingest_status_page_shows_loading_then_rows(page, web_base_url):
    """一覧・ツリーに読み込み中表示が出て、データ到着後に一覧へ切り替わる（応答を保留して決定的に観測）。"""
    install_api_mocks(page)
    pending: dict = {}
    page.route("**/ingest/preview**", lambda route: pending.update(route=route))
    page.goto(f"{web_base_url}/ingest.html")

    expect(page.locator("#rows")).to_contain_text("読み込み中")
    expect(page.locator("#tree")).to_contain_text("読み込み中")

    _fulfill(pending["route"], PREVIEW)
    expect(page.locator("#rows")).to_contain_text("税計算仕様書.md")
    expect(page.locator("#rows")).not_to_contain_text("読み込み中")


@pytest.mark.parametrize("registered", [False, True], ids=["no_world", "preview_failure"])
def test_preview_unavailable_shows_plain_state_not_stuck_spinner(page, web_base_url, registered):
    """範囲ツリー・一覧のスピナーが回り続けない（回帰）。資料フォルダが1件も無いときは `/ingest/preview`
    （world 未指定を 422 で拒否）自体を呼ばず平文の空状態へ、`/ingest/preview` が失敗（503等）しても平文のエラー表示へ倒れる。"""
    install_api_mocks(page)
    preview_calls = []

    def handle_preview(route):
        preview_calls.append(route.request.url)
        if registered:
            _fulfill(route, {"detail": "グラフを読み込めません。しばらくしてからお試しください"}, 503)
        else:
            _fulfill(route, {"detail": [{"type": "string_pattern_mismatch", "loc": ["query", "world"]}]}, 422)

    if not registered:
        _route_json(page, "**/worlds", {"worlds": []})
    page.route("**/ingest/preview**", handle_preview)
    page.goto(f"{web_base_url}/ingest.html")

    text = "取り込み状況を取得できません" if registered else "まだ資料フォルダが登録されていません"
    for pane in ("#tree", "#rows"):
        expect(page.locator(pane)).to_contain_text(text)
        expect(page.locator(f"{pane} .spinner")).to_have_count(0)
    assert bool(preview_calls) == registered   # 資料フォルダ 0 件は preview 自体を呼ばない


def test_ingest_documents_show_provenance_badges(page, web_base_url):
    """各文書に「どう読み取ったか」の平文バッジが出る。コード文書（branch=source）の行とプレビューの各項目には
    担当アナライザ「解析: <表示名>」を出し、資料（analyzer=None）には出さない。全文検索のヒットカードにも由来を出す。"""
    _open(page, web_base_url)

    rows = page.locator("#rows")
    expect(rows).to_contain_text("旧料金表.xls")
    expect(rows).to_contain_text("Office から直接読み取り")
    expect(rows).to_contain_text("旧形式を変換してから読み取り（LibreOffice）")
    expect(rows).to_contain_text("AI が画像を見て読み取り（数値は要確認）")
    expect(rows).to_contain_text("照合で差分あり")
    tip = rows.locator(".provbadge.warn").first
    expect(tip).to_have_attribute("title", "別の方法で読むと追加の内容が見つかりました。原本を確認してください")

    cobol_row = rows.locator("tr", has_text="TAXCALC.cbl")
    expect(cobol_row).to_contain_text("解析: COBOL")   # 内部名（cobol）でなく表示ラベル
    expect(cobol_row).not_to_contain_text("解析: cobol")
    expect(rows.locator("tr", has_text="税計算仕様書.md")).not_to_contain_text("解析:")

    page.locator("#esq").fill("料金")
    page.locator("#esbtn").click()
    hits = page.locator("#eshits")
    expect(hits).to_contain_text("スキャン図面.pdf")
    expect(hits).to_contain_text("AI が画像から読み取り")
    expect(hits).to_contain_text("照合で差分あり")

    page.click("#detailbtn")
    ents = page.locator("#pv-ents")
    expect(ents.locator(".ent", has_text="TAX-RATE")).to_contain_text("解析: COBOL")
    expect(ents.locator(".ent", has_text="税計算仕様書.md")).not_to_contain_text("解析:")


def test_ingest_legacy_ocr_method_shows_backward_compat_badge(page, web_base_url):
    """tesseract 撤去前に作られた派生 md の来歴（method="ocr"／extraction_method="ocr"）は、バッジ／ヒットカードで
    無表示にならず「旧方式」だとわかる平文で表示する（表示のみの後方互換）。
    「解析:」表示は `analyzer`（Analyzer.name）を使い `doctype` とは独立（回帰）で、未知の名前は大文字化しない。"""
    dummy = _doc("thing.dummy", doctype="ダミー言語", branch="source", analyzer="dummylang")
    legacy_preview = {**PREVIEW, "documents": [dummy] + [
        ({**d, "provenance": {"method": "ocr", "confidence": 0.4}} if d["name"] == SCAN_PDF else d)
        for d in PREVIEW["documents"]
    ]}
    _open(page, web_base_url, preview=legacy_preview)
    _route_json(page, "**/admin/es/search**", {
        "world": "w1", "query": "料金", "scope_paths": [], "hits": [
            {"doc_id": SCAN_PDF, "line": 3, "snippet": "旧方式で読み取った本文。", "score": 2.1,
             "ext": ".pdf", "extraction_method": "ocr", "confidence": 0.4}]})
    page.reload()

    expect(page.locator("#rows")).to_contain_text("スキャン図面.pdf")
    expect(page.locator("#rows")).to_contain_text("画像から文字を読み取り（旧方式）")
    row = page.locator("#rows tr", has_text="thing.dummy")
    expect(row).to_contain_text("解析: dummylang")
    expect(row).not_to_contain_text("解析: ダミー言語")

    page.locator("#esq").fill("料金")
    page.locator("#esbtn").click()
    hits = page.locator("#eshits")
    expect(hits).to_contain_text("スキャン図面.pdf")
    expect(hits).to_contain_text("画像から読み取り（旧）")


def test_page_and_detail_button_admin_only(page, web_base_url):
    """資料フォルダ登録/更新/削除・取り込み状況・全文検索は admin 限定 API のため、画面全体を非 admin には
    access-denied だけ見せる（nav の「資料」タブも admin のみ）。「詳細（管理）」も admin のみ。
    /auth/me 失敗時は fail-safe で非表示・access-denied 側。"""
    install_api_mocks(page)                       # 既定=admin
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#main-content")).to_be_visible()
    expect(page.locator("#access-denied")).to_be_hidden()
    expect(page.locator('.nav a[href="ingest.html"]')).to_be_visible()
    expect(page.locator("#detailbtn")).to_be_visible()

    install_api_mocks(page, user=USER_MEMBER)     # 後掛けの route が優先される
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#access-denied")).to_contain_text("管理者権限が必要です")
    expect(page.locator('.nav a[href="ingest.html"]')).to_have_count(0)
    expect(page.locator("#detailbtn")).to_be_hidden()

    page.route("**/auth/me", lambda route: route.fulfill(status=500, body="{}"))
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#main-content")).to_be_hidden()
    expect(page.locator("#access-denied")).to_be_visible()
    expect(page.locator("#detailbtn")).to_be_hidden()


def test_importance_badge_and_diagnostics_banner(page, web_base_url):
    """台帳（文書一覧）に `_重要度.txt` 由来の重要度バッジが出る（値・理由・由来はホバー・重要度が無い文書は空のまま）。
    `_重要度.txt` の構文エラーはツリー付近の小さな警告バナーに出る（無ければ非表示）。"""
    preview_with_importance = {**PREVIEW, "importance_diagnostics": [
        {"config_path": "4期/02_設計/_重要度.txt", "line": 3, "column": 1,
         "code": "invalid_value", "message": "値が正しくありません。「高」「中」「低」「なし」のいずれかにしてください"},
    ], "documents": [
        ({**d, "importance": "高", "importance_reason": "税制改正の一次資料",
          "importance_source": "4期/02_設計/_重要度.txt:1行目"}
         if d["name"] == "4期/02_設計/01_基本設計/税計算仕様書.md" else d)
        for d in PREVIEW["documents"]
    ]}
    _open(page, web_base_url, preview=preview_with_importance)

    badge = page.locator("#rows tr", has_text="税計算仕様書.md").locator(".impbadge")
    expect(badge).to_have_text("高")
    expect(badge).to_have_attribute("title", re.compile("重要度 高.*税制改正の一次資料.*_重要度\\.txt"))
    expect(page.locator("#rows tr", has_text="TAXCALC.cbl").locator(".impbadge")).to_have_count(0)
    banner = page.locator("#impdiag")
    expect(banner).to_be_visible()
    expect(banner).to_contain_text("_重要度.txt")
    expect(banner).to_contain_text("4期/02_設計/_重要度.txt:3行目")

    _route_json(page, "**/ingest/preview**", PREVIEW)
    page.goto(f"{web_base_url}/ingest.html")
    expect(page.locator("#impdiag")).to_be_hidden()


def test_status_shows_counts_as_of_and_recount_button(page, web_base_url):
    """件数の後ろに集計時刻を表示し、「再集計」ボタンで `POST /worlds/{id}/recount` を呼んで表示を更新する
    （未集計＝`counts_as_of=None` は「（未集計）」）。"""
    _open(page, web_base_url, status={"counts_as_of": None})
    stat = page.locator('[data-stat="w1"]')
    expect(stat).to_contain_text("（未集計）")

    recount_calls = []

    def handle_recount(route):
        recount_calls.append(route.request.method)
        _fulfill(route, {**WORLD_STATUS_RESP, "ok": True, "world_id": "w1",
                         "counts_as_of": "2026-09-01T03:12:00+00:00"})

    page.route("**/worlds/w1/recount", handle_recount)
    _route_json(page, "**/worlds/w1/status", {**WORLD_STATUS_RESP, "counts_as_of": "2026-09-01T03:12:00+00:00"})

    stat.locator('[data-recount="w1"]').click()
    expect(stat).to_contain_text("時点")
    expect(stat).not_to_contain_text("未集計")
    assert recount_calls == ["POST"]


def test_status_detail_shows_failed_files_and_reconvert_button_calls_endpoint(page, web_base_url):
    """「詳細を表示」の折りたたみに失敗ファイル一覧（平文の理由＋対処）と各段の要約を出し、各行の「再変換」ボタンは
    確認ダイアログの上で `POST /worlds/{id}/reconvert` を `{rel}` 付きで呼ぶ。"""
    _open(page, web_base_url, status={
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
    })
    reconvert_bodies = []

    def handle_reconvert(route):
        reconvert_bodies.append(json.loads(route.request.post_data or "{}"))
        _fulfill(route, {"ok": True, "world_id": "w1", "rel": "旧料金表.xls", "changed": True,
                         "status": "auto_published", "ledger": 3, "flags": [], "summary": WORLD_STATUS_RESP,
                         "note": "更新と同じ処理が走りました。"})

    page.route("**/worlds/w1/reconvert", handle_reconvert)

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
    page.once("dialog", lambda d: d.accept())
    btn.click()

    expect(page.locator('[data-stat="w1"]')).not_to_contain_text("再変換できません")   # エラーにならず完了する
    assert reconvert_bodies == [{"rel": "旧料金表.xls"}]


def test_pickbtn_disabled_while_ingest_running(page, web_base_url):
    """登録ボタン（`pickbtn`）は、実行中の資料フォルダが1件でもあれば無効化し平文の理由（`title`）を添える
    （登録処理全体は長時間かつ排他のため、実行中に別の登録を投げると固まって見える）。実行中でなくなれば再び有効化される。
    登録済みのため `regcard` 自体は非表示だが、`disabled`/`title` は表示状態と独立に検証できる。"""
    _open(page, web_base_url, status={"running_progress": RUNNING_PROGRESS})

    pickbtn = page.locator("#pickbtn")
    expect(pickbtn).to_be_disabled()
    expect(pickbtn).to_have_attribute("title", "取り込みの実行中は登録できません")
    steps = page.locator(".ingest-steps")   # 取り込み系の段では 済✓/現在(件数付き)/残り が1行に並ぶ
    expect(steps).to_be_visible()
    expect(steps.locator(".step.done")).to_have_count(3)          # scanning/office_md/graph_build が済
    expect(steps.locator(".step.now")).to_contain_text("全文索引・ベクトル化（42/100）")
    expect(steps.locator(".step.todo")).to_have_count(1)          # finalize が残り

    _route_json(page, "**/worlds/w1/status", WORLD_STATUS_RESP)
    page.reload()
    expect(pickbtn).to_be_enabled()


def test_pickbtn_recovers_after_transient_status_failure_during_polling(page, web_base_url):
    """`loadStat` の自己ポーリング中に 404 以外の一時的な失敗（ネットワーク瞬断・5xx等）が起きても、既知の実行中
    資料フォルダのポーリングは止まらない（止まると `pickbtn` が「実行中」表示のまま永久に無効化される回帰）。"""
    install_api_mocks(page)
    state = {"calls": 0}

    def handle_status(route):
        state["calls"] += 1
        if state["calls"] == 1:
            _fulfill(route, {**WORLD_STATUS_RESP, "running_progress": RUNNING_PROGRESS})
        elif state["calls"] == 2:
            _fulfill(route, {"detail": "boom"}, status=500)
        else:
            _fulfill(route, WORLD_STATUS_RESP)

    page.route("**/worlds/w1/status", handle_status)
    page.goto(f"{web_base_url}/ingest.html")
    page.clock.install()

    pickbtn = page.locator("#pickbtn")
    expect(pickbtn).to_be_disabled()

    page.clock.fast_forward(3500)   # 2回目のポーリング＝一時的な500失敗（ポーリングは継続する）
    expect(page.locator('[data-stat="w1"]')).to_contain_text("状況を取得できませんでした")
    expect(pickbtn).to_be_disabled()

    page.clock.fast_forward(3500)   # 3回目のポーリング＝復旧して running_progress が無くなる
    expect(pickbtn).to_be_enabled()


def test_resolve_settings_save_confirms_and_shows_warning(page, web_base_url):
    """資料の探し方の設定: 保存は確認してから送り、取り込み直しの案内と見つからない場所の警告を出す。"""
    records = _open(page, web_base_url)
    expect(page.locator("#resolvecard")).to_be_visible()
    page.wait_for_load_state("networkidle")              # 設定の読み込み（GET）が終わってから入力する
    page.once("dialog", lambda d: d.accept())
    page.locator("#rs-copy").fill("SystemA/COPYLIB")
    page.locator("#rs-save").click()
    expect(page.locator("#rs-msg")).to_contain_text("取り込み直しています")
    expect(page.locator("#rs-msg")).to_contain_text("SystemA/COPYLIB")
    assert records["resolve_settings_put"] == [{"copy_paths": ["SystemA/COPYLIB"], "path_aliases": {}}]


def test_unreflected_resolve_settings_stay_visible_after_reload(page, web_base_url):
    """未反映の設定は、状況の API が返す限り画面を開き直しても「更新が必要」と出続ける。"""
    _open(page, web_base_url, status={"resolve_settings_pending": True})
    expect(page.locator("#list")).to_contain_text("更新が必要です")
