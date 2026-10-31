"""`web/chat/render.js` の `linkDocsInBodyHTML`（本文の中の資料名を出典のリンクにする）を実ファイルから切り出して node で動かす。

`render.js` 全体は `Sherpa` グローバルに依存して Node 単体では動かないため、`referencedDocRows` と `linkDocsInBodyHTML` の定義だけを
切り出し、`esc` は `web/common.js` の実体を使う（`test_render_js_budget_note.py` と同じ流儀）。
"""
from __future__ import annotations

import json
import pathlib
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
RENDER_JS = ROOT / "web" / "chat" / "render.js"
COMMON_JS = ROOT / "web" / "common.js"

DOC = "設計/帳票/ledger_a.md"
DOC2 = "設計/共通/dup_name.md"
DOC3 = "設計/共通2/dup_name.md"


def _snippet() -> str:
    common = COMMON_JS.read_text(encoding="utf-8")
    start = common.index("const _sherpaEsc")
    esc = common[start:common.index("\n\n", start)] + "\nconst esc = _sherpaEsc;"
    src = RENDER_JS.read_text(encoding="utf-8")
    a = src.index("const _cnt =")
    b = src.index("export function impactListView")
    return esc + "\n" + src[a:b].replace("export function", "function")


def _link(html: str, answer: dict) -> str:
    node = shutil.which("node")
    if not node:
        pytest.skip("node が見つからない")
    script = _snippet() + f"\nconsole.log(JSON.stringify(linkDocsInBodyHTML({json.dumps(html)}, {json.dumps(answer)})));"
    r = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=10)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


def _answer(*paths, no_url=()):
    return {"referenced_docs": [{"path": p, "ranges": []} for p in paths],
            "sources": [{"doc_id": p, "download_url": f"/documents/download?world=w&rel={p}"}
                        for p in paths if p not in no_url]}


def test_quoted_names_and_unquoted_full_paths_become_download_links():
    out = _link(f"<p>{DOC} と「ledger_a.md」と <code>ledger_a.md:12</code> と ledger_a.md</p>", _answer(DOC))
    link = f'<a href="/documents/download?world=w&amp;rel={DOC}" data-dl>'
    assert out.count(link) == 3
    assert f"<p>{link}{DOC}</a> と「{link}ledger_a.md</a>」と <code>{link}ledger_a.md:12</a></code> と ledger_a.md</p>" == out


def test_duplicated_file_name_among_the_listed_docs_is_not_linked_but_full_paths_are():
    out = _link(f"<p>{DOC2} と {DOC3} と「dup_name.md」</p>", _answer(DOC2, DOC3))
    assert out.count("<a ") == 2 and "「dup_name.md」</p>" in out


def test_docs_without_a_download_url_and_unlisted_names_are_left_alone():
    out = _link(f"<p>{DOC} と「other.md」</p>", _answer(DOC, no_url=(DOC,)))
    assert "<a " not in out
    assert _link("<p>「ledger_a.md」</p>", {"headline": "x"}) == "<p>「ledger_a.md」</p>"


def test_links_and_code_blocks_are_left_alone_and_escaping_is_kept():
    html = (f'<p><a href="x">{DOC}</a> &lt;{DOC}&gt;</p>'
            f"<pre><code>{DOC}\nledger_a.md</code></pre>")
    out = _link(html, _answer(DOC))
    assert f'<a href="x">{DOC}</a>' in out                       # 既にリンクの中は触らない
    assert "&lt;<a " in out and "</a>&gt;" in out               # エスケープ済みの記号はそのまま
    assert out.count("<a ") == 2 and "<pre><code>" + DOC + "\nledger_a.md</code></pre>" in out   # コードブロックは対象外


def test_unquoted_paths_inside_longer_paths_or_urls_and_bare_names_are_not_linked():
    doc = "x/原価.xlsx"
    out = _link("<p>https://h/x/原価.xlsx と y/x/原価.xlsx と x/原価.xlsx.bak と 原価.xlsx と 資料はx/原価.xlsxです</p>",
                _answer(doc))
    assert out.count("<a ") == 1 and "資料は<a " in out


def test_names_with_several_line_numbers_paths_followed_by_more_path_and_ambiguous_names():
    out = _link(f"<p><code>ledger_a.md:12, 16</code> と {DOC}/more と {DOC}@rev と {DOC}:12</p>", _answer(DOC))
    assert out.count("<a ") == 2 and f"{DOC}/more" in out and f"{DOC}@rev" in out   # 長いパスの途中はリンクにしない
    ans = {**_answer(DOC2), "sources_unverified": [{"path": "dup_name.md", "reason": "同じ名前の資料が複数あります"}]}
    assert "<a " not in _link("<p>「dup_name.md」</p>", ans)                           # 範囲で名前が重なるものはファイル名だけではリンクにしない


def test_paths_in_url_query_values_are_not_linked():
    out = _link(f"<p>https://h/?file={DOC} と https://h/?a=1&amp;f={DOC} と &lt;{DOC}&gt;</p>", _answer(DOC))
    assert out.count("<a ") == 1 and "&lt;<a " in out


def test_full_width_letters_next_to_a_path_are_not_linked():
    assert "<a " not in _link(f"<p>Ａ{DOC} と {DOC}Ｂ</p>", _answer(DOC))


def test_file_name_is_unique_only_if_it_is_unique_among_all_sources():
    ans = _answer(DOC2)
    ans["sources"].append({"doc_id": DOC3, "download_url": "/dl?rel=other"})
    assert "<a " not in _link("<p>「dup_name.md」</p>", ans)                 # 表示しきれなかった出典に同じ名前がある


def test_query_or_fragment_after_a_path_and_hidden_unconfirmed_rows_block_links():
    out = _link(f"<p>{DOC}?rev=1 と {DOC}#L2 と {DOC}&amp;download=1 と {DOC}=foo と {DOC}</p>", _answer(DOC))
    assert out.count("<a ") == 1 and out.endswith(f'と <a href="/documents/download?world=w&amp;rel={DOC}" data-dl>{DOC}</a></p>')
    ans = {**_answer(DOC), "sources_unverified_more": 3}
    assert "<a " not in _link("<p>「ledger_a.md」</p>", ans)              # 表示しきれない確認できなかった資料に同じ名前がありうる
