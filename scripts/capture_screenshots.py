#!/usr/bin/env python3
"""マニュアル用スクリーンショットの自動収集ツール（AI 不要・プログラムで再取得）。

docs/manual/*.md が参照する `images/*.png` を、既存の e2e テスト基盤
（`tests/e2e/mock_api.py` のモック API ＋ `web/` の静的配信）を再利用して
Playwright（chromium・headless）で機械的に撮り直す。画面が変わったら
`make screenshots` 一発で全画像を再生成できる状態を目指す。

- ストア（Docker/Postgres/Neo4j/ES）は不要。API は `install_api_mocks(page)` が
  `page.route` で決定的なデモデータを返す（実 e2e テストと同じ土台）。
- 静的ファイル（HTML/CSS/JS）は専用ポート（既定 8901・dev の 8000 と衝突しない）で配信。
- シーンは「登録簿」(`SCENES`) に宣言的に並べる。増やす時はここに 1 エントリ足すだけ。

使い方:
    python scripts/capture_screenshots.py            # 全シーンを生成
    python scripts/capture_screenshots.py --list      # シーン一覧（画像名→再現方法）
    python scripts/capture_screenshots.py --only chat  # 名前に "chat" を含むものだけ
    python scripts/capture_screenshots.py --port 8901  # 静的配信ポートを変更
"""
from __future__ import annotations

import argparse
import fnmatch
import functools
import json
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"
OUT_DIR = ROOT / "docs" / "manual" / "images"
E2E_DIR = ROOT / "tests" / "e2e"

# e2e テスト基盤（モック API・デモデータ）をそのまま import して使う（テストコードは変更しない）。
sys.path.insert(0, str(E2E_DIR))
from mock_api import install_api_mocks  # noqa: E402

DEFAULT_PORT = 8901
DEFAULT_VIEWPORT = (1280, 800)
WIDE_VIEWPORT = (1440, 900)   # 3 カラムの全体像・グラフ系向け

# 決定性: アニメーション/トランジション無効化＋キャレット非表示（再実行で画素を安定させる）。
_ANIM_OFF_CSS = (
    "*,*::before,*::after{"
    "animation-duration:0s!important;animation-delay:0s!important;"
    "transition-duration:0s!important;transition-delay:0s!important;"
    "scroll-behavior:auto!important;caret-color:transparent!important}"
)

# チャット 3 カラムの幅を固定（左=履歴/中央=会話/右=思考の流れ）。localStorage 由来（chat.js）。
_CHAT_COLS_INIT = "localStorage.setItem('sherpa-cols', JSON.stringify({L:300,R:380}));"


def _fulfill_json(route, payload: dict) -> None:
    route.fulfill(status=200, content_type="application/json",
                  body=json.dumps(payload, ensure_ascii=False))


# ===== モックの追加応答（既定モックに無いエンドポイント）=====
# install_api_mocks が page.route("**/*") を張った後に登録＝こちらが優先される。


def routes_es_search(page) -> None:
    """ingest.html の全文検索パネル（GET /admin/es/search）。既定モックには無いので補う。"""
    def handler(route):
        _fulfill_json(route, {"hits": [
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md", "line": 12, "score": 8.42,
             "ext": "md", "snippet": "消費税率は 10% とする。改定時は TAX-RATE を更新すること。"},
            {"doc_id": "4期/03_開発/01_ソース/TAXCALC.cbl", "line": 34, "score": 6.10,
             "ext": "cbl", "snippet": "MOVE TAX-RATE TO WS-RATE.   * 税率を作業領域へ複写"},
            {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md", "line": 48, "score": 5.03,
             "ext": "md", "snippet": "端数は切り捨て。丸め処理は BILLGEN 側で行う。"},
        ]})
    page.route("**/admin/es/search**", handler)


# ===== カスタム SSE ストリーム（既定の IMPACT_ANSWER 以外の回答を出したいシーン用）=====

_TROUBLE_ANSWER = {
    "lens": "troubleshoot",
    "headline": "夜間バッチ異常終了の原因候補は 3 件です。確度順に確認してください。",
    "route": {"path": ["障害", "原因候補", "確認手順"]},
    "summary": {"total": 3},
    "scope": {"world": "w1", "scope_paths": [], "source": "all"},
    "data": {"candidates": [
        {"name": "TAXCALC", "role": "税計算モジュール（最有力）",
         "path": ["夜間バッチ", "請求処理", "TAXCALC"]},
        {"name": "TAX-RATE", "role": "税率パラメータの参照",
         "path": ["TAXCALC", "TAX-RATE"]},
        {"name": "BILLGEN", "role": "請求書生成（関連）",
         "path": ["夜間バッチ", "BILLGEN"]},
    ]},
    "sources": [
        {"doc_id": "4期/03_開発/01_ソース/TAXCALC.cbl",
         "download_url": "/documents/download?world=w1&rel=taxcalc"},
        {"doc_id": "4期/02_設計/01_基本設計/税計算仕様書.md",
         "download_url": "/documents/download?world=w1&rel=spec"},
    ],
}

_TROUBLE_STREAM = [
    {"type": "node", "id": "understand", "kind": "think", "status": "done",
     "label": "依頼を理解", "detail": "夜間バッチの異常終了の原因調査"},
    {"type": "node", "id": "tool-grep", "kind": "tool", "status": "done",
     "label": "資料を検索（grep）", "detail": "「異常終了」"},
    {"type": "node", "id": "tool-graph", "kind": "tool", "status": "done",
     "label": "関係グラフを照会", "detail": "TAXCALC の近傍"},
    {"type": "answer", "conversation_id": 101, "message": {"answer": _TROUBLE_ANSWER}},
]

# 確認カード（clarify）: サーバは type=question イベントを送出し、chat.js の onQuestion が askcard を描く。
_CLARIFY_STREAM = [
    {"type": "node", "id": "understand", "kind": "think", "status": "done",
     "label": "依頼を理解", "detail": "調べ方を確認"},
    {"type": "question", "conversation_id": 101, "interaction_id": "lens-demo01", "mode": "single",
     "prompt": "どの調べ方をしますか？（結果が大きく変わるため確認します）",
     "options": [
         {"id": "impact", "label": "影響範囲を調べる", "description": "変更がどこに波及するかを洗い出す"},
         {"id": "qa", "label": "仕様・内容を調べる", "description": "資料の記載を検索して答える"},
         {"id": "troubleshoot", "label": "不具合の原因を調べる", "description": "異常終了などの原因候補を挙げる"},
     ],
     "allow_free_text": True, "original_message": "税率まわりを調べたい"},
]


# ===== シーンの共通操作ヘルパー =====

def _send(page, text: str) -> None:
    page.fill("#input", text)
    page.click("#send")


def _wait_answer(page) -> None:
    """回答が確定＝出典枠（.sources）が出るまで待つ。"""
    page.wait_for_selector("#messages .sources", state="visible", timeout=20000)


def _wait_brain(page) -> None:
    """入力欄付近のバー（頭脳ラベル・範囲）が /config・/world-options から埋まるまで待つ。"""
    page.wait_for_function(
        "() => { const b = document.getElementById('brain-label');"
        " return b && b.textContent && b.textContent !== '…'; }", timeout=10000)


# ===== シーン登録簿 =====

@dataclass
class Scene:
    name: str                                   # 画像ファイル名（.png なし）
    page: str                                    # web/ 配下の HTML
    how: str                                     # --list 用の再現方法サマリ
    setup: Callable | None = None                # goto 後の前準備（クリック/送信など）
    crop: str | None = None                      # 要素 locator（None=ビューポート全体）
    viewport: tuple[int, int] = DEFAULT_VIEWPORT
    mock_kwargs: dict = field(default_factory=dict)
    routes: Callable | None = None               # 追加の page.route（install_api_mocks の後に登録）
    is_chat: bool = False                         # チャット系（3 カラム幅を固定）
    settle_ms: int = 300                          # 撮影前の追加待ち
    skip: str | None = None                       # 非空なら SKIP（理由を出力）


def _scene_overview(page):
    page.click("#kbtoggle")                       # ナレッジ参照オン（範囲セレクタが出る）
    _send(page, "消費税率を変えたい。影響は？")
    _wait_answer(page)


def _scene_send_impact(page):
    _send(page, "消費税率を変えたい。影響は？")
    _wait_answer(page)


def _scene_troubleshoot(page):
    _send(page, "夜間バッチが異常終了する。原因は？")
    _wait_answer(page)


def _scene_thinking(page):
    _send(page, "消費税率を変えたい。影響は？")
    _wait_answer(page)


def _scene_clarify(page):
    _send(page, "税率まわりを調べたい")
    page.wait_for_selector(".askcard", state="visible", timeout=20000)


def _scene_kb_off(page):
    _wait_brain(page)                             # バーが埋まるのを待つだけ（既定＝オフ）


def _scene_scope_brain(page):
    _wait_brain(page)
    page.click("#kbtoggle")                       # ナレッジ参照オン＝範囲セレクタ表示
    page.wait_for_selector("#scopesel", state="visible", timeout=5000)


def _scene_graph(page):
    page.wait_for_function(
        "() => (document.getElementById('gcount')?.textContent || '').includes('ノード')",
        timeout=15000)


def _scene_graph_search(page):
    # 初期グラフの cose レイアウト（animate:true）が終わってから検索させる。
    # 検索は renderGraph 内で cy.destroy() するため、アニメ中に叩くと cytoscape が
    # null renderer に notify して落ちる（ページ内 JS エラー）。
    page.wait_for_function(
        "() => (document.getElementById('gcount')?.textContent || '').includes('ノード')",
        timeout=15000)
    page.wait_for_timeout(1800)
    page.wait_for_selector("#relfilter option[value='COPIES']", state="attached", timeout=10000)
    page.select_option("#relfilter", "COPIES")
    page.click("#gfilter")
    page.wait_for_function(
        "() => (document.getElementById('gcount')?.textContent || '').includes('検索結果')",
        timeout=10000)


def _scene_graph_ask(page):
    page.wait_for_function(
        "() => (document.getElementById('gcount')?.textContent || '').includes('ノード')",
        timeout=15000)
    page.fill("#gask", "このナレッジベースはどこが弱い？ TAX-RATE に関係するものは？")
    page.click("#gaskbtn")
    page.wait_for_function(
        "() => (document.getElementById('ganswer')?.textContent || '').trim().length > 0",
        timeout=10000)


def _scene_settings(page):
    # 「読み込み済み」の実データ信号で待つ（#agent は静的 HTML に最初からあり visible では不十分・RV Med）。
    # load() 完了＝キー欄の placeholder が /settings 応答（openai_key_set）で置き換わる。
    page.wait_for_function(
        "() => (document.getElementById('okey')?.placeholder || '').includes('設定済み')",
        timeout=10000)


def _scene_ingest_new(page):
    # 一覧の実データ（.row）が描画され、各行の状況スピナーも解消するまで待つ（RV Med）。
    page.wait_for_selector("#list .row", state="visible", timeout=10000)
    page.wait_for_function(
        "() => !document.querySelector('#list .loading, #list .loading-inline')",
        timeout=10000)


def _scene_ingest_status(page):
    # 件数サマリ（「使えます N」）と実データ行が出るまで待つ（「読み込み中」行の撮影を防ぐ・RV Med）。
    page.wait_for_function(
        "() => (document.getElementById('count')?.textContent || '').includes('使えます')",
        timeout=10000)
    page.wait_for_function(
        "() => document.querySelectorAll('#rows tr').length > 0"
        " && !document.querySelector('#rows .loading-inline')",
        timeout=10000)


def _scene_es_search(page):
    page.fill("#esq", "消費税率")
    page.click("#esbtn")
    page.wait_for_selector("#eshits .eshit", state="visible", timeout=10000)


def _scene_admin_settings(page):
    # admin-settings.js は checkAdmin()（/auth/me）成功後に load()（/admin/settings）を呼ぶ。
    # UI-TABS 化（2026-09-04）以降、アーム一覧（#arms-list）は「取り込み設定」タブの中＝既定タブでは
    # 非表示。先にタブを選択してから描画完了（既定/固定中の判定文言）を待つ。
    page.click('.tab-btn[data-tab="ingest"]')
    page.wait_for_selector("#arms-list .armrow", state="visible", timeout=10000)
    page.wait_for_function(
        "() => (document.getElementById('arms-status')?.textContent || '').length > 0",
        timeout=10000)


SCENES: list[Scene] = [
    # --- 10. 使い方：チャットで調べる ---
    Scene("10-chat-overview", "chat.html",
          "チャットを開き→ナレッジ参照オン→影響を質問。左=履歴/中央=回答/右=思考の流れの全体像",
          setup=_scene_overview, crop=None, viewport=WIDE_VIEWPORT, is_chat=True),
    Scene("10-chat-impact-card", "chat.html",
          "影響を質問し、確定した影響分析の答えカード（内訳チップ＋対象一覧）を切り出す",
          setup=_scene_send_impact, crop="#messages .msg:has(.a-row)", is_chat=True),
    Scene("10-chat-sources", "chat.html",
          "影響を質問し、回答末尾の出典フッター（📄=原本DL）を切り出す",
          setup=_scene_send_impact, crop="#messages .sources", is_chat=True),
    Scene("10-chat-troubleshoot", "chat.html",
          "トラブルシュートの回答（原因候補カード・確度順/経路つき）を SSE 差し替えで再現",
          setup=_scene_troubleshoot, crop="#messages .msg:has(.a-row)", is_chat=True,
          mock_kwargs={"stream_events": _TROUBLE_STREAM}),
    Scene("10-chat-thinking", "chat.html",
          "影響を質問し、右ペイン『思考の流れ』（検索→精読→グラフ近傍）を切り出す",
          setup=_scene_thinking, crop=".pane.right", is_chat=True),
    Scene("10-chat-clarify", "chat.html",
          "曖昧な依頼を送り、確認カード（選択肢）を SSE の question イベントで再現",
          setup=_scene_clarify, crop="#messages .msg:has(.askcard)", is_chat=True,
          mock_kwargs={"stream_events": _CLARIFY_STREAM}),
    # --- 11. 使い方：範囲とAIの切り替え ---
    Scene("11-knowledge-toggle", "chat.html",
          "入力欄付近のバー（ナレッジ参照トグル＝既定オフ）を切り出す",
          setup=_scene_kb_off, crop=".cbar", is_chat=True),
    Scene("11-scope-brain", "chat.html",
          "ナレッジ参照オンにして、範囲（フォルダ）と頭脳（AI）の選択が並ぶバーを切り出す",
          setup=_scene_scope_brain, crop=".cbar", is_chat=True),
    Scene("11-settings", "settings.html",
          "設定画面（AI 接続：OpenAI/Gemini/Ollama/Codex のキー・モデル）を上部から",
          setup=_scene_settings),
    # --- 20. 管理：資料の取り込み ---
    # S3-A: ingest-new.html は ingest.html に統合。上=資料フォルダ／下=取り込み状況を同一画面から撮る。
    Scene("20-ingest-new", "ingest.html",
          "「資料」画面の上部＝資料フォルダ（登録済み一覧＋フォルダ登録フォーム）を表示",
          setup=_scene_ingest_new, crop=".folders"),
    Scene("20-ingest-status", "ingest.html",
          "「資料」画面の下部＝取り込み状況（一覧・状態・フォルダツリー）を表示",
          setup=_scene_ingest_status, crop=".ingestwrap"),
    # --- 21. 管理：グラフと検索 ---
    Scene("21-graph", "graph.html",
          "ナレッジグラフ（cytoscape）を読み込み、レイアウト安定後に全体を撮る",
          setup=_scene_graph, viewport=WIDE_VIEWPORT, settle_ms=1800),
    Scene("21-graph-search", "graph.html",
          "関係=COPIES で絞り込み検索し、検索結果グラフを撮る",
          setup=_scene_graph_search, viewport=WIDE_VIEWPORT, settle_ms=1800),
    Scene("21-graph-ask", "graph.html",
          "グラフへの自然言語質問（右ペイン）＝管理チャットと根拠ノードを切り出す",
          setup=_scene_graph_ask, crop=".graphlegend", viewport=WIDE_VIEWPORT, settle_ms=800),
    Scene("21-es-search", "ingest.html",
          "取り込み状況内の全文検索パネルで検索し、ヒット一覧カードを切り出す",
          setup=_scene_es_search, crop=".es-card", routes=routes_es_search),
    # --- 30. システム管理 ---
    Scene("24-admin-settings", "admin-settings.html",
          "システム管理画面（取り込み設定・旧形式変換・視覚読み取りAI・管理メニュー）を上部から",
          setup=_scene_admin_settings, crop=".wrap2.frm", viewport=WIDE_VIEWPORT),
]


# ===== 静的配信サーバ（web/ を専用ポートで配る）=====

class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _start_static_server(port: int):
    handler = functools.partial(_QuietStaticHandler, directory=str(WEB))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ===== 撮影本体 =====

def _capture_scene(browser, base_url: str, scene: Scene) -> None:
    ctx = browser.new_context(
        viewport={"width": scene.viewport[0], "height": scene.viewport[1]},
        device_scale_factor=2,            # 高解像度（マニュアル向けに鮮明に）
        timezone_id="Asia/Tokyo",         # 日時表示を JST 固定（conftest と同じ理由）
        locale="ja-JP",
        reduced_motion="reduce",
    )
    try:
        if scene.is_chat:
            ctx.add_init_script(_CHAT_COLS_INIT)
        page = ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))

        install_api_mocks(page, **scene.mock_kwargs)
        if scene.routes:
            scene.routes(page)             # 既定モックの後＝こちらが優先

        page.goto(f"{base_url}/{scene.page}", wait_until="load")
        page.add_style_tag(content=_ANIM_OFF_CSS)
        if scene.setup:
            scene.setup(page)
        if scene.settle_ms:
            page.wait_for_timeout(scene.settle_ms)

        # JS エラーの検査は PNG 書き出しの前（壊れたページで既存の正常画像を上書きしない・RV Med）。
        if page_errors:
            raise RuntimeError("ページ内 JS エラー: " + " / ".join(page_errors))

        out = OUT_DIR / f"{scene.name}.png"
        if scene.crop:
            page.locator(scene.crop).last.screenshot(path=str(out))
        else:
            page.screenshot(path=str(out), full_page=False)
    finally:
        ctx.close()


def _select(scenes: list[Scene], only: str | None) -> list[Scene]:
    if not only:
        return scenes
    if only.endswith(".png"):        # マニュアルからファイル名をコピーした指定も通す（RV Low）
        only = only[: -len(".png")]
    pat = only if any(c in only for c in "*?[") else f"*{only}*"
    return [s for s in scenes if fnmatch.fnmatch(s.name, pat)]


def main() -> int:
    global OUT_DIR
    ap = argparse.ArgumentParser(description="マニュアル用スクリーンショットの自動収集")
    ap.add_argument("--only", metavar="PATTERN",
                    help="名前パターン（部分一致・glob 可）で対象を絞る（例: --only chat）")
    ap.add_argument("--list", action="store_true", help="シーン一覧（画像名→再現方法）を表示して終了")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT,
                    help=f"静的配信ポート（既定 {DEFAULT_PORT}・dev の 8000 と衝突しない）")
    ap.add_argument("--out", default=str(OUT_DIR), help="出力ディレクトリ（既定 docs/manual/images）")
    ap.add_argument("--headed", action="store_true", help="ブラウザを表示（デバッグ用）")
    args = ap.parse_args()
    OUT_DIR = Path(args.out)

    if args.list:
        print(f"シーン登録簿（全 {len(SCENES)} 件）:")
        for s in SCENES:
            mark = "  [SKIP] " if s.skip else "  "
            print(f"{mark}{s.name:<24} <- {s.page:<16} {s.how}")
            if s.skip:
                print(f"        SKIP 理由: {s.skip}")
        return 0

    scenes = _select(SCENES, args.only)
    if not scenes:
        print(f"パターン '{args.only}' に一致するシーンがありません。--list で一覧を確認してください。")
        return 2

    try:
        from playwright.sync_api import sync_playwright
    except ModuleNotFoundError:
        print("playwright 未導入。`pip install playwright` と `python -m playwright install chromium` を実行してください。")
        return 3

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    server, thread = _start_static_server(args.port)
    port = server.server_address[1]    # --port 0（OS 割当）でも実ポートで URL を組む（RV Low）
    base_url = f"http://127.0.0.1:{port}"
    print(f"静的配信: {base_url}  出力: {OUT_DIR}")

    generated, skipped, failed = [], [], []
    t0 = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            try:
                for scene in scenes:
                    if scene.skip:
                        print(f"  SKIP  {scene.name}: {scene.skip}")
                        skipped.append((scene.name, scene.skip))
                        continue
                    st = time.time()
                    try:
                        _capture_scene(browser, base_url, scene)
                        print(f"  OK    {scene.name}.png  ({time.time() - st:.1f}s)")
                        generated.append(scene.name)
                    except Exception as exc:   # noqa: BLE001 - 1 件の失敗で全体を止めない
                        print(f"  FAIL  {scene.name}: {type(exc).__name__}: {exc}")
                        failed.append((scene.name, f"{type(exc).__name__}: {exc}"))
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)

    print(f"\nサマリ: 生成 {len(generated)} / スキップ {len(skipped)} / 失敗 {len(failed)}"
          f"  （所要 {time.time() - t0:.1f}s）")
    for name, reason in skipped:
        print(f"  - SKIP {name}: {reason}")
    for name, reason in failed:
        print(f"  - FAIL {name}: {reason}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
