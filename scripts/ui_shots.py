#!/usr/bin/env python3
"""UI 改善の比較用スクリーンショット行列（モック API＋Playwright・docker 不要）。

docs/proposals/2026-09-07-UI改善.md §7 の検証表（画面／テーマ／ビューポート・ズーム／状態）を
機械的に撮る道具。マニュアル画像（scripts/capture_screenshots.py・docs/manual/images）とは別物で、
出力は `tmp/ui-shots/<label>/`（git 管理外）。ブランチごとに label を変えて撮り、`--compare` で
並べた HTML を作って見比べる（A/B＝main／design/claude／design/codex）。

- ストア（Docker/Postgres/Neo4j/ES）は不要。API は `tests/e2e/mock_api.install_api_mocks` が
  `page.route` で決定的なデモデータを返す（e2e テスト・マニュアル画像と同じ土台）。
- テーマは localStorage `sherpa-theme` を init script で仕込む（各ページの <head> が読む既存経路）。
- 200% ズームは「CSS ビューポートを半分＋device_scale_factor=2」で再現する（1280×800 の窓を
  ブラウザで 200% にした見え方＝CSS 640×400）。
- 検証観点（hover でしか出ない操作・iframe・フォーカス）はシーンの setup で状態を作る。

使い方:
    python scripts/ui_shots.py --label main                 # 全シーン（light/dark × 1280/1920 ＋ 一部 200%）
    python scripts/ui_shots.py --label claude --only admin  # 名前に admin を含むシーンだけ
    python scripts/ui_shots.py --compare main claude        # tmp/ui-shots/compare-main-vs-claude.html
    python scripts/ui_shots.py --list
"""
from __future__ import annotations

import argparse
import fnmatch
import functools
import html
import json
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Callable

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "web"                 # --web で別の worktree の web/ に差し替えられる（A/B の各ブランチを同じ条件で撮る）
E2E_DIR = ROOT / "tests" / "e2e"


def _shared_root() -> Path:
    """出力先はメイン checkout の tmp/ に固定する（git worktree から実行しても同じ場所へ集める＝
    ブランチ間の比較を1つの index で見るため）。git が無い/失敗時は自分の ROOT。"""
    try:
        out = subprocess.run(["git", "rev-parse", "--git-common-dir"], cwd=ROOT,
                             capture_output=True, text=True, check=True).stdout.strip()
        common = Path(out)
        if not common.is_absolute():
            common = ROOT / common
        return common.resolve().parent
    except Exception:   # noqa: BLE001
        return ROOT


OUT_ROOT = _shared_root() / "tmp" / "ui-shots"

sys.path.insert(0, str(E2E_DIR))
from mock_api import install_api_mocks  # noqa: E402

DEFAULT_PORT = 8902   # capture_screenshots.py（8901）と同時に動かせるよう別ポート

# 決定性: アニメーション/トランジション無効化＋キャレット非表示。
_ANIM_OFF_CSS = (
    "*,*::before,*::after{"
    "animation-duration:0s!important;animation-delay:0s!important;"
    "transition-duration:0s!important;transition-delay:0s!important;"
    "scroll-behavior:auto!important;caret-color:transparent!important}"
)
# チャット 3 カラムの幅（localStorage 由来・chat.js）。シーンごとに1本の init script だけで仕込む
# （page 側と context 側の add_init_script は評価順が保証されないため重ねない）。
_CHAT_COLS_DEFAULT = {"L": 300, "R": 380}
_CHAT_COLS_NARROW = {"L": 200, "R": 300}     # 左ペイン最小幅（ドラッグ下限＝chat.js の Lmin）

# ビューポート: 名前 → (CSS 幅, CSS 高さ, device_scale_factor)
VIEWS = {
    "w1280": (1280, 800, 1),
    "w1920": (1920, 1080, 1),
    "z200": (640, 400, 2),     # 1280×800 の窓を 200% ズーム
}
DEFAULT_VIEWS = ["w1280", "w1920"]
THEMES = ["light", "dark"]


# ===== シーンの共通操作 =====

def _send(page, text: str) -> None:
    page.fill("#input", text)
    page.click("#send")


def _wait_answer(page) -> None:
    page.wait_for_selector("#messages .sources", state="visible", timeout=20000)


def _wait_brain(page) -> None:
    page.wait_for_function(
        "() => { const b = document.getElementById('brain-label');"
        " return b && b.textContent && b.textContent !== '…'; }", timeout=10000)


def _scene_chat_welcome(page):
    _wait_brain(page)


def _scene_chat_answer(page):
    _send(page, "消費税率を変えたい。影響は？")
    _wait_answer(page)


def _scene_chat_history_hover(page):
    """履歴行の補助操作（hover でしか出ない .cacts）を出した状態。"""
    page.wait_for_selector("#convlist .conv", state="visible", timeout=10000)
    page.locator("#convlist .conv").first.hover()
    page.wait_for_timeout(150)


def _scene_chat_history_menu(page):
    """履歴行の「その他」メニューを開いた状態（U2 以降のブランチ用・無ければ hover と同じ）。"""
    page.wait_for_selector("#convlist .conv", state="visible", timeout=10000)
    trig = page.locator("#convlist [data-menu], #convlist [data-conv-menu]").first
    if trig.count():
        trig.click()
        page.wait_for_timeout(150)
    else:
        page.locator("#convlist .conv").first.hover()
        page.wait_for_timeout(150)


def _scene_chat_history_keyboard(page):
    """キーボードだけで履歴行へ到達しようとした状態（Tab を進めてフォーカス位置を可視化）。"""
    page.wait_for_selector("#convlist .conv", state="visible", timeout=10000)
    page.locator("#newbtn").focus()
    page.keyboard.press("Tab")
    page.wait_for_timeout(150)


def _scene_chat_fontmenu(page):
    _wait_brain(page)
    page.click("#fontbtn")
    page.wait_for_selector("#fontmenu", state="visible", timeout=5000)


def _scene_chat_brainmenu(page):
    _wait_brain(page)
    page.click("#brainbadge")
    page.wait_for_selector("#brainmenu", state="visible", timeout=5000)


def _scene_chat_narrow_left(page):
    """左ペインを最小幅（200px）まで狭めた状態（履歴行の操作領域の検証用・幅は Scene.cols で仕込む）。"""
    page.wait_for_selector("#convlist .conv", state="visible", timeout=10000)
    trig = page.locator("#convlist [data-menu], #convlist [data-conv-menu]").first
    if trig.count():
        trig.click()            # メニューの収まりも同時に見る
    else:
        page.locator("#convlist .conv").first.hover()
    page.wait_for_timeout(150)


def _scene_settings(page):
    page.wait_for_function(
        "() => (document.getElementById('okey')?.placeholder || '').includes('設定済み')",
        timeout=10000)


def _wait_admin_loaded(page):
    page.wait_for_selector("#cloud-provider-radios input", state="attached", timeout=10000)


def _admin_tab(key: str, *, embed: bool = False):
    def setup(page):
        _wait_admin_loaded(page)
        page.click(f'.tab-btn[data-tab="{key}"]')
        if embed:
            frame = page.locator(f"#embed-frame-{key}")
            frame.wait_for(state="visible", timeout=5000)
            page.wait_for_timeout(900)   # iframe 内の初期描画（モック応答）を待つ
        else:
            page.wait_for_timeout(200)
    return setup


def _scene_admin_tab_keyboard(page):
    """タブバーにフォーカスを置いた状態（フォーカス表示の有無を確認する）。"""
    _wait_admin_loaded(page)
    page.locator('.tab-btn[data-tab="provider"]').focus()
    page.wait_for_timeout(100)


def _scene_home(page):
    page.wait_for_selector(".ann, .ann-empty", state="visible", timeout=10000)


def _scene_ingest(page):
    page.wait_for_selector("#list .row", state="visible", timeout=10000)
    page.wait_for_function(
        "() => !document.querySelector('#list .loading, #list .loading-inline')", timeout=10000)


def _scene_usage(page):
    page.wait_for_selector("tr.u-row", state="visible", timeout=10000)


def _scene_audit(page):
    page.wait_for_selector("tr.audit-row", state="visible", timeout=10000)


def _scene_admin_users(page):
    page.wait_for_selector("tbody tr", state="visible", timeout=10000)


def _scene_status(page):
    page.wait_for_selector(".status-pill", state="visible", timeout=10000)


def _scene_workspace(page):
    page.wait_for_selector(".file-table", state="attached", timeout=10000)
    page.wait_for_timeout(300)


def _scene_graph(page):
    page.wait_for_function(
        "() => (document.getElementById('gcount')?.textContent || '').includes('ノード')", timeout=15000)
    page.wait_for_timeout(1500)


def _scene_login(page):
    page.wait_for_selector(".login-card", state="visible", timeout=5000)


def _scene_manual(page):
    page.wait_for_selector(".manual-doc", state="visible", timeout=10000)


@dataclass
class Scene:
    name: str
    page: str
    how: str
    setup: Callable | None = None
    is_chat: bool = False
    full: bool = False              # True=ページ全体（縦長のフォーム画面向け）
    zoom: bool = False              # True=z200（200% ズーム）も撮る
    mock_kwargs: dict = field(default_factory=dict)
    settle_ms: int = 250
    cols: dict | None = None        # チャットの 3 カラム幅（None＝既定 _CHAT_COLS_DEFAULT）


SCENES: list[Scene] = [
    # --- チャット ---
    Scene("chat-welcome", "chat.html", "初期表示（質問例カード・調べ方ブロック）", _scene_chat_welcome, is_chat=True, zoom=True),
    Scene("chat-answer", "chat.html", "影響検索の回答（吹き出し・答えカード・出典・右ペイン）", _scene_chat_answer, is_chat=True, zoom=True),
    Scene("chat-history-hover", "chat.html", "履歴行を hover（補助操作の見え方）", _scene_chat_history_hover, is_chat=True),
    Scene("chat-history-menu", "chat.html", "履歴行の「その他」メニューを開く（無ければ hover）", _scene_chat_history_menu, is_chat=True),
    Scene("chat-history-keyboard", "chat.html", "新規ボタンから Tab を1回（履歴行へキーボードで到達できるか）", _scene_chat_history_keyboard, is_chat=True),
    Scene("chat-history-narrow", "chat.html", "左ペイン最小幅（200px）でその他メニューを開く", _scene_chat_narrow_left, is_chat=True, cols=_CHAT_COLS_NARROW),
    Scene("chat-fontmenu", "chat.html", "文字サイズメニューを開く", _scene_chat_fontmenu, is_chat=True),
    Scene("chat-brainmenu", "chat.html", "頭脳メニューを開く", _scene_chat_brainmenu, is_chat=True),
    # --- 個人設定 ---
    Scene("settings", "settings.html", "個人設定（全体）", _scene_settings, full=True, zoom=True),
    # --- システム管理 ---
    Scene("admin-provider", "admin-settings.html", "プロバイダ＋接続先タブ", _admin_tab("provider"), full=True, zoom=True),
    Scene("admin-models", "admin-settings.html", "使えるモデルタブ", _admin_tab("models"), full=True),
    Scene("admin-ingest", "admin-settings.html", "取り込みタブ", _admin_tab("ingest"), full=True),
    Scene("admin-usage", "admin-settings.html", "利用量タブ（統計チャットの AI）", _admin_tab("usage"), full=True),
    Scene("admin-extkeys", "admin-settings.html", "外部連携タブ", _admin_tab("extkeys"), full=True),
    Scene("admin-users-embed", "admin-settings.html", "ユーザー管理（iframe 埋め込み）", _admin_tab("users", embed=True), zoom=True),
    Scene("admin-usage-page-embed", "admin-settings.html", "利用統計（iframe 埋め込み）", _admin_tab("usage-page", embed=True)),
    Scene("admin-audit-embed", "admin-settings.html", "監査ログ（iframe 埋め込み）", _admin_tab("audit", embed=True)),
    Scene("admin-status-embed", "admin-settings.html", "システム状態（iframe 埋め込み）", _admin_tab("status", embed=True)),
    Scene("admin-tab-keyboard", "admin-settings.html", "タブにフォーカス（focus-visible の有無）", _scene_admin_tab_keyboard),
    # --- その他の画面（単体表示） ---
    Scene("home", "home.html", "ホーム（お知らせ）", _scene_home, full=True),
    Scene("ingest", "ingest.html", "資料（フォルダ一覧＋取り込み状況）", _scene_ingest, full=True),
    Scene("usage", "usage.html", "利用統計（単体）", _scene_usage, full=True),
    Scene("audit", "audit.html", "監査ログ（単体）", _scene_audit, full=True),
    Scene("admin-users", "admin-users.html", "ユーザー管理（単体）", _scene_admin_users, full=True),
    Scene("status", "status.html", "システム状態（単体）", _scene_status, full=True),
    Scene("workspace", "workspace.html", "マイワークスペース", _scene_workspace, full=True),
    Scene("graph", "graph.html", "ナレッジグラフ", _scene_graph),
    Scene("manual", "manual.html", "使い方", _scene_manual),
    Scene("login", "login.html", "ログイン", _scene_login, mock_kwargs={"auth_status": 401}),
]


# ===== 静的配信 =====

class _QuietStaticHandler(SimpleHTTPRequestHandler):
    def log_message(self, *_args):
        pass


def _start_static_server(port: int, web_dir: Path):
    handler = functools.partial(_QuietStaticHandler, directory=str(web_dir))
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, thread


# ===== 撮影 =====

def _shot_name(scene: Scene, theme: str, view: str) -> str:
    return f"{scene.name}__{theme}__{view}.png"


def _capture(browser, base_url: str, out_dir: Path, scene: Scene, theme: str, view: str) -> None:
    w, h, dsf = VIEWS[view]
    ctx = browser.new_context(
        viewport={"width": w, "height": h}, device_scale_factor=dsf,
        timezone_id="Asia/Tokyo", locale="ja-JP", reduced_motion="reduce",
    )
    try:
        ctx.add_init_script(f"localStorage.setItem('sherpa-theme', '{theme}');")
        if scene.is_chat:
            cols = json.dumps(scene.cols or _CHAT_COLS_DEFAULT)
            ctx.add_init_script(f"localStorage.setItem('sherpa-cols', JSON.stringify({cols}));")
        page = ctx.new_page()
        page_errors: list[str] = []
        page.on("pageerror", lambda exc: page_errors.append(str(exc)))
        install_api_mocks(page, **scene.mock_kwargs)
        page.goto(f"{base_url}/{scene.page}", wait_until="load")
        page.add_style_tag(content=_ANIM_OFF_CSS)
        if scene.setup:
            scene.setup(page)
        if scene.settle_ms:
            page.wait_for_timeout(scene.settle_ms)
        if page_errors:
            raise RuntimeError("ページ内 JS エラー: " + " / ".join(page_errors))
        page.screenshot(path=str(out_dir / _shot_name(scene, theme, view)), full_page=scene.full)
    finally:
        ctx.close()


def _select(scenes: list[Scene], only: str | None) -> list[Scene]:
    if not only:
        return scenes
    pat = only if any(c in only for c in "*?[") else f"*{only}*"
    return [s for s in scenes if fnmatch.fnmatch(s.name, pat)]


def _views_for(scene: Scene, views: list[str]) -> list[str]:
    out = [v for v in views if v != "z200"]
    if "z200" in views and scene.zoom:
        out.append("z200")
    return out


# ===== 一覧 HTML（1 label の contact sheet／複数 label の並列比較）=====

_PAGE_CSS = (
    "body{font-family:system-ui,sans-serif;margin:16px;background:#eee;color:#222}"
    "h1{font-size:18px}h2{font-size:14px;margin:26px 0 8px;padding-top:10px;border-top:1px solid #ccc}"
    ".row{display:flex;gap:12px;align-items:flex-start;overflow-x:auto}"
    ".cell{flex:0 0 auto}.cell figcaption{font-size:11px;color:#555;margin-bottom:4px}"
    "img{display:block;max-width:640px;max-height:520px;border:1px solid #bbb;background:#fff}"
    "img.z{max-width:480px}.missing{width:320px;height:120px;display:grid;place-items:center;"
    "border:1px dashed #999;color:#999;font-size:12px}"
)


def _img_or_missing(path: Path, rel: str, cls: str = "") -> str:
    if path.exists():
        return f'<a href="{html.escape(rel)}" target="_blank"><img class="{cls}" loading="lazy" src="{html.escape(rel)}"></a>'
    return '<div class="missing">未撮影</div>'


def write_index(label: str) -> Path:
    out_dir = OUT_ROOT / label
    parts = [f"<!doctype html><meta charset='utf-8'><title>ui-shots {html.escape(label)}</title>",
             f"<style>{_PAGE_CSS}</style><h1>ui-shots: {html.escape(label)}</h1>"]
    for scene in SCENES:
        parts.append(f"<h2>{html.escape(scene.name)} — {html.escape(scene.how)}</h2><div class='row'>")
        for theme in THEMES:
            for view in VIEWS:
                fn = _shot_name(scene, theme, view)
                cls = "z" if view == "z200" else ""
                parts.append(f"<figure class='cell'><figcaption>{theme} / {view}</figcaption>"
                             f"{_img_or_missing(out_dir / fn, fn, cls)}</figure>")
        parts.append("</div>")
    p = out_dir / "index.html"
    p.write_text("\n".join(parts), encoding="utf-8")
    return p


def write_compare(labels: list[str]) -> Path:
    """label ごとの列を並べる比較ページ（画面／テーマ／サイズをプルダウンで切替・同じ横幅の縮尺で表示）。
    画像が無い組合せは「未撮影」と出す。全組合せの一覧は末尾の折りたたみに残す。"""
    scenes = [{"name": sc.name, "how": sc.how,
               "views": [v for v in VIEWS if any((OUT_ROOT / lb / _shot_name(sc, t, v)).exists() for lb in labels for t in THEMES)]}
              for sc in SCENES]
    scenes = [sc for sc in scenes if sc["views"]]
    exists = {lb: sorted(p.name for p in (OUT_ROOT / lb).glob("*.png")) if (OUT_ROOT / lb).exists() else [] for lb in labels}
    data = {"labels": labels, "scenes": scenes, "themes": THEMES, "views": list(VIEWS), "exists": exists,
            "viewLabel": {"w1280": "1280 × 800", "w1920": "1920 × 1080", "z200": "200% ズーム（1280 × 800 の窓）"}}
    css = (
        "*{box-sizing:border-box}body{margin:0;padding:20px 24px;background:#f4f7f6;color:#182c27;font:15px/1.6 system-ui,sans-serif}"
        "h1{font-size:22px;margin:0 0 4px}p{margin:6px 0 12px}form{display:flex;gap:16px;flex-wrap:wrap;align-items:end;padding:8px 0 14px}"
        "label{display:grid;gap:4px;font-weight:600;font-size:13px}select{font:inherit;padding:7px 10px;border:1px solid #718079;border-radius:8px;background:#fff;min-width:200px}"
        "select:focus-visible,a:focus-visible,button:focus-visible{outline:3px solid #0f766e;outline-offset:2px}"
        ".compare{display:grid;grid-template-columns:repeat(var(--n,3),minmax(0,1fr));gap:16px;align-items:start}"
        "figure{margin:0;min-width:0;background:#fff;border:1px solid #c7d1cc;border-radius:12px;overflow:hidden}"
        "figcaption{padding:10px 14px;font-weight:700;border-bottom:1px solid #c7d1cc;display:flex;justify-content:space-between;gap:8px}"
        "figcaption a{font-weight:400;font-size:12px;color:#0f766e}img{display:block;width:100%;height:auto}"
        ".missing{padding:40px;text-align:center;color:#6b7a73}#meta{font-size:13px;color:#4e6058}"
        "details{margin-top:28px}summary{cursor:pointer;font-weight:700}.all h3{font-size:13px;margin:18px 0 6px;color:#4e6058}"
        ".all .row{display:flex;gap:10px;overflow-x:auto}.all img{max-width:420px;border:1px solid #c7d1cc}"
        "@media(max-width:1000px){.compare{grid-template-columns:1fr}}"
    )
    js = r"""
const D = __DATA__;
const $ = (id) => document.getElementById(id);
const sceneSel = $('scene'), themeSel = $('theme'), viewSel = $('view');
D.scenes.forEach((sc) => { const o = document.createElement('option'); o.value = sc.name; o.textContent = sc.name + ' — ' + sc.how; sceneSel.appendChild(o); });
D.themes.forEach((t) => { const o = document.createElement('option'); o.value = t; o.textContent = t === 'light' ? 'ライト' : 'ダーク'; themeSel.appendChild(o); });
function fillViews() {
  const sc = D.scenes.find((s) => s.name === sceneSel.value); const cur = viewSel.value;
  viewSel.innerHTML = '';
  sc.views.forEach((v) => { const o = document.createElement('option'); o.value = v; o.textContent = D.viewLabel[v] || v; viewSel.appendChild(o); });
  if (sc.views.includes(cur)) viewSel.value = cur;
}
function render() {
  const sc = sceneSel.value, t = themeSel.value, v = viewSel.value;
  const fn = `${sc}__${t}__${v}.png`;
  const box = $('compare'); box.innerHTML = ''; box.style.setProperty('--n', D.labels.length);
  D.labels.forEach((lb) => {
    const f = document.createElement('figure');
    const has = D.exists[lb].includes(fn);
    const src = `${lb}/${fn}`;
    f.innerHTML = `<figcaption><span>${lb}</span>${has ? `<a href="${src}" target="_blank">原寸で開く</a>` : ''}</figcaption>`
      + (has ? `<img src="${src}" alt="${lb}: ${sc} ${t} ${v}" loading="lazy">` : `<div class="missing">未撮影</div>`);
    box.appendChild(f);
  });
  $('meta').textContent = `${sc}（${(D.scenes.find((s) => s.name === sc) || {}).how || ''}）／ ${t} ／ ${D.viewLabel[v] || v}`;
  try { localStorage.setItem('ui-shots-compare', JSON.stringify({ sc, t, v })); } catch (e) {}
}
try { const st = JSON.parse(localStorage.getItem('ui-shots-compare') || 'null'); if (st) { sceneSel.value = st.sc; themeSel.value = st.t; } } catch (e) {}
if (!sceneSel.value) sceneSel.selectedIndex = 0;
fillViews();
try { const st = JSON.parse(localStorage.getItem('ui-shots-compare') || 'null'); if (st && st.v) viewSel.value = st.v; } catch (e) {}
sceneSel.addEventListener('change', () => { fillViews(); render(); });
themeSel.addEventListener('change', render);
viewSel.addEventListener('change', render);
document.addEventListener('keydown', (e) => {
  if (e.target.tagName === 'SELECT') return;
  if (e.key === 'ArrowRight' || e.key === 'ArrowLeft') { const d = e.key === 'ArrowRight' ? 1 : -1; sceneSel.selectedIndex = (sceneSel.selectedIndex + d + sceneSel.options.length) % sceneSel.options.length; fillViews(); render(); }
  if (e.key === 't') { themeSel.selectedIndex = (themeSel.selectedIndex + 1) % themeSel.options.length; render(); }
});
render();
"""
    parts = [f"<!doctype html><html lang='ja'><meta charset='utf-8'><meta name='viewport' content='width=device-width,initial-scale=1'>",
             f"<title>Sherpa UI 比較 {html.escape(' / '.join(labels))}</title><style>{css}</style>",
             f"<h1>Sherpa UI 比較: {html.escape(' / '.join(labels))}</h1>",
             "<p>同じモック API・同じ Chromium・同じ操作で撮った画面を、ブランチ（列）ごとに並べます。画像は同じ横幅の縮尺です"
             "（原寸は各列のリンク）。←/→ で画面を送り、t でテーマを切り替えられます。</p>",
             "<form><label>画面<select id='scene'></select></label><label>テーマ<select id='theme'></select></label>"
             "<label>撮影サイズ<select id='view'></select></label></form><p id='meta' aria-live='polite'></p>",
             "<div class='compare' id='compare'></div>",
             "<details><summary>全組合せの一覧（スクロール）</summary><div class='all'>"]
    for sc in SCENES:
        for theme in THEMES:
            for view in VIEWS:
                fn = _shot_name(sc, theme, view)
                if not any((OUT_ROOT / lb / fn).exists() for lb in labels):
                    continue
                parts.append(f"<h3>{html.escape(sc.name)} / {theme} / {view}</h3><div class='row'>")
                for lb in labels:
                    parts.append(f"<figure class='cell'><figcaption>{html.escape(lb)}</figcaption>{_img_or_missing(OUT_ROOT / lb / fn, f'{lb}/{fn}')}</figure>")
                parts.append("</div>")
    parts.append("</div></details>")
    parts.append("<script>" + js.replace("__DATA__", json.dumps(data, ensure_ascii=False)) + "</script>")
    p = OUT_ROOT / f"compare-{'-vs-'.join(labels)}.html"
    p.write_text("\n".join(parts), encoding="utf-8")
    return p


# ===== main =====

def main() -> int:
    global OUT_ROOT
    ap = argparse.ArgumentParser(description="UI 改善の比較用スクリーンショット行列")
    ap.add_argument("--label", help="出力ラベル（tmp/ui-shots/<label>/・例: main / claude / codex）")
    ap.add_argument("--only", metavar="PATTERN", help="シーン名の部分一致・glob（例: --only admin）")
    ap.add_argument("--themes", default=",".join(THEMES), help="light,dark（既定は両方）")
    ap.add_argument("--views", default=",".join(DEFAULT_VIEWS + ["z200"]),
                    help="w1280,w1920,z200（既定は全部・z200 は zoom 指定のあるシーンだけ）")
    ap.add_argument("--compare", nargs="+", metavar="LABEL", help="撮影せず、既存の label 同士を並べた HTML を作る")
    ap.add_argument("--list", action="store_true", help="シーン一覧を表示して終了")
    ap.add_argument("--out", help=f"出力ルート（既定 {OUT_ROOT}）")
    ap.add_argument("--web", help=f"配信する web/ ディレクトリ（既定 {WEB}・別ブランチの worktree を指す）")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--headed", action="store_true")
    args = ap.parse_args()
    if args.out:
        OUT_ROOT = Path(args.out)

    if args.list:
        for s in SCENES:
            print(f"  {s.name:26s} {s.page:22s} {'full ' if s.full else '     '}{'z200 ' if s.zoom else '     '}{s.how}")
        return 0
    if args.compare:
        p = write_compare(args.compare)
        print(f"比較 HTML: {p}")
        return 0
    if not args.label:
        ap.error("--label が必要です（または --compare / --list）")

    themes = [t for t in args.themes.split(",") if t in THEMES]
    views = [v for v in args.views.split(",") if v in VIEWS]
    scenes = _select(SCENES, args.only)
    out_dir = OUT_ROOT / args.label
    out_dir.mkdir(parents=True, exist_ok=True)

    from playwright.sync_api import sync_playwright
    web_dir = Path(args.web).resolve() if args.web else WEB
    if not (web_dir / "chat.html").exists():
        ap.error(f"web ディレクトリが不正です: {web_dir}")
    server, thread = _start_static_server(args.port, web_dir)
    base_url = f"http://127.0.0.1:{args.port}"
    ok: list[str] = []
    failed: list[tuple[str, str]] = []
    t0 = time.time()
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=not args.headed)
            try:
                for scene in scenes:
                    for theme in themes:
                        for view in _views_for(scene, views):
                            name = _shot_name(scene, theme, view)
                            st = time.time()
                            try:
                                _capture(browser, base_url, out_dir, scene, theme, view)
                                ok.append(name)
                                print(f"  OK    {name}  ({time.time() - st:.1f}s)")
                            except Exception as exc:   # noqa: BLE001 - 1 件の失敗で止めない
                                failed.append((name, f"{type(exc).__name__}: {exc}"))
                                print(f"  FAIL  {name}: {type(exc).__name__}: {str(exc)[:200]}")
            finally:
                browser.close()
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
    idx = write_index(args.label)
    print(f"\n生成 {len(ok)} / 失敗 {len(failed)}（{time.time() - t0:.1f}s・web={web_dir}）→ {idx}")
    for name, reason in failed:
        print(f"  - FAIL {name}: {reason}")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
