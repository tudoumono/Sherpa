"""web 静的アセットの構文・整合性ゲート（リファクタリング計画フェーズ6 S1 の担保新設）。

chat.js の module 化（S4）・common.js 分離（S2）を安全に進めるための保険。ビルドチェーンを
導入しない（web は素の JS を維持する方針・docs/proposals/2026-07-02-リファクタリング計画.md
フェーズ6節）ため、分割の挙動不変をここで機械的に固定する5本柱:

  1. `node --check` による構文ゲート（node 不在なら skip・危険地雷10「404/構文全死」の検知器）。
  2. DOM id ⇔ JS 参照の突合。各ページ JS の `$('id')`/`getElementById('id')` が実在する id
     （HTML の id 属性 ∪ そのJS自身が生成するマークアップ内 id ∪ nav.js 生成 id）を指すこと。
     逆向き: HTML の inline `on*="funcName(...)"` が参照する関数名がそのページの JS に
     実際に定義されていること。
  3. 全 HTML の `<script src type>` 出現順 golden（危険地雷8・9: nav.js/common.js の読み込み順
     変更を意図した diff としてレビューに乗せる）。
  4. window 公開名 golden/allowlist（「module は window を汚さない」契約・例外は common.js の
     `window.Sherpa`（フェーズ6 S2 で nav.js から分離）と e2e seam `window.__sherpaChatTest`
     （フェーズ6 S1）のみ）。
  5. import グラフ突合（フェーズ6 S4）: web/**/*.js の静的 import 文が指すファイルが実在すること・
     chat.html の module entry（chat.js）から辿った到達集合に web/chat/ 配下の全ファイルが
     含まれること（孤児＝どこからも import されない分割後ファイルの検知・危険地雷10 の静的側）。

動的に組み立てる id（`$('prefix-' + var)` 形）は静的 regex では拾えないため、実装を読んで
洗い出し `DYNAMIC_ID_LOOKUPS` に明示する（誤検知しない設計を優先＝機械判定できないものは
ホワイトリストで扱う）。

golden の更新手順（**意図した変更のときだけ**実行し、差分を目視確認してからコミットする。
tests/unit/test_store_surface.py と同じ運用）:

    SHERPA_USE_FIXTURES=1 PYTHONPATH=. .venv/bin/python -c \
        "import sys; sys.path.insert(0, 'tests/unit'); \
         import test_web_assets as t; t._write_goldens()"
"""
from __future__ import annotations

import pathlib
import re
import shutil
import subprocess

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
WEB = ROOT / "web"
_SCRIPT_ORDER_GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "web_script_order.txt"

# nav.js が <sherpa-topbar> のパース中同期 upgrade（classic script 必須・危険地雷8）で生成する
# id（web/nav.js を実際に読んで列挙）。chat.js 等の $('themebtn') 等はここで作られる id を指す。
NAV_GENERATED_IDS = {
    "sherpa-nav", "turnnotice", "healthdot", "themebtn", "userwrap",
    "topbar-user", "usermenu", "um-name", "um-role", "um-changepw",
    "um-logout", "um-note",
}

# 動的 id 組み立て（`$('prefix-' + var)` 形。静的 regex では末尾の識別子まで解決できない）の
# ホワイトリスト。実装を読んで洗い出し済みの唯一の1件（新規に増えたら追記・下の
# test_dynamic_id_lookups_whitelist_targets_exist が実在チェックする＝形骸化を防ぐ）。
DYNAMIC_ID_LOOKUPS = {
    "settings.js": ["t-openai", "t-gemini", "t-bedrock", "t-ollama", "t-codex"],   # $('t-' + provider)
}

_ID_CALL_RE = re.compile(r"""\$\(\s*['"]([\w.\-]+)['"]\s*\)|getElementById\(\s*['"]([\w.\-]+)['"]\s*\)""")
_ID_ATTR_RE = re.compile(r"""id=["']([\w.\-]+)["']""")
_SCRIPT_SRC_RE = re.compile(r'<script\s+([^>]*?)src="([^"]+)"([^>]*)>')
_INLINE_ONCLICK_CALL_RE = re.compile(r"""on\w+=["']([A-Za-z_]\w*)\(""")
# 静的 import/export-from 文の相対パス（`import {...} from '../x.js'`・副作用のみの `import '../x.js'`・
# `export {...} from '../x.js'` にマッチ）。RV LOW（2026-07-14 フェーズ6）: 全文検索だとコメント/文字列内の
# `import ... from './x.js'` に偽陽性（S8 で実際に発生）のため**行頭アンカー**（コメント行は `//`・` *`
# 始まりでマッチしない）。動的 import() は test_no_dynamic_imports_in_web_js が別途禁止。
_STATIC_IMPORT_RE = re.compile(
    r"""^\s*(?:import|export)\s+(?:[^'"]*?from\s+)?["'](\.[^'"]+)["']""", re.M)
_DYNAMIC_IMPORT_RE = re.compile(r"""\bimport\s*\(""")
_NAV_SHERPA_MEMBER_RE = re.compile(r"^\s*([A-Za-z_$][\w$]*)\s*:", re.M)
_WINDOW_ASSIGN_RE = re.compile(r"window\.([A-Za-z_$][\w$]*)\s*=")

NAV_SHERPA_MEMBERS = {"$", "esc", "api", "getJSON", "fmtDateTime", "downloadBlob", "analyzerLabel", "mdLite",
                      "visibilityInterval"}  # POLL-1（性能台帳 QW4）: 非表示タブで止まる setInterval ラッパ
# 例外は common.js の Sherpa（共通ユーティリティ・フェーズ6 S2 で nav.js から分離）と
# chat.js の e2e seam __sherpaChatTest のみ（危険地雷1・window 公開名 golden 節）。
WINDOW_ASSIGNMENT_ALLOWLIST = {"Sherpa", "__sherpaChatTest"}


def _web_js_files() -> list[pathlib.Path]:
    """web/**/*.js（vendor/ 除外）の再帰列挙。空振り検知の下限つき（分割後もサブフォルダに追随）。
    RV LOW（2026-07-14 フェーズ6）: トップレベルだけで16件あるため下限15では rglob→glob の退行を
    検知できない。分割後の実数（23件）基準＋ web/chat/ 配下の存在を明示 assert。"""
    files = sorted(p for p in WEB.rglob("*.js") if "vendor" not in p.relative_to(WEB).parts)
    assert len(files) >= 23, f"走査対象JSファイルが少なすぎる（再帰 glob の退行の疑い）: {len(files)} 件"
    assert any("chat" in p.relative_to(WEB).parts[:-1] for p in files), \
        "web/chat/ 配下の分割ファイルが走査対象に含まれていない（再帰 glob の退行）"
    return files


def _own_script_srcs(html_path: pathlib.Path) -> list[str]:
    """そのページ自身の JS（nav.js・vendor/ を除いた <script src=...>）をタグ出現順で返す。"""
    text = html_path.read_text(encoding="utf-8")
    srcs = [m.group(2) for m in _SCRIPT_SRC_RE.finditer(text)]
    return [s for s in srcs if s != "nav.js" and not s.startswith("vendor/")]


def _page_js_closure(html_path: pathlib.Path) -> list[pathlib.Path]:
    """そのページの JS 集合＝ <script src=...> 直参照＋ module entry からの静的 import 推移閉包。
    RV LOW（2026-07-14 フェーズ6）: DOM id 突合が HTML 直参照の script しか見ておらず、
    module 化後の web/chat/*.js の `$('id')` が静的検査から漏れていた指摘の回収。"""
    seen: list[pathlib.Path] = []
    stack = [(WEB / rel).resolve() for rel in _own_script_srcs(html_path)]
    while stack:
        cur = stack.pop()
        if cur in seen or not cur.exists():
            continue
        seen.append(cur)
        stack.extend(_static_import_targets(cur))
    return seen


# ===== 1. node --check 構文ゲート =====

def test_node_check_syntax_all_web_js():
    """全 web JS（vendor 除く）が `node --check` を通ること（node 不在なら skip）。"""
    node = shutil.which("node")
    if not node:
        pytest.skip("node が見つからない（node --check 構文ゲートは対象外）")
    failures = []
    for p in _web_js_files():
        r = subprocess.run([node, "--check", str(p)], capture_output=True, text=True)
        if r.returncode != 0:
            failures.append(f"{p.relative_to(WEB)}: {r.stderr.strip()}")
    assert not failures, "\n".join(failures)


# ===== 2. DOM id <-> JS 参照の突合 =====

def test_dom_id_references_resolve_to_existing_ids():
    """各ページ JS の `$('id')`/`getElementById('id')` が、そのページの HTML id属性・
    そのJS自身が生成するマークアップ内 id・nav.js 生成 id のいずれかに実在することを確認する。
    対象 JS は HTML 直参照＋module entry からの import 推移閉包（web/chat/*.js を含む）。"""
    problems = []
    for html_path in sorted(WEB.glob("*.html")):
        page_js = _page_js_closure(html_path)
        if not page_js:
            continue   # ingest-new.html: リダイレクト stub・自前 JS 無し
        html_ids = set(_ID_ATTR_RE.findall(html_path.read_text(encoding="utf-8")))
        used_ids: set[str] = set()
        own_generated_ids: set[str] = set()
        for js_path in page_js:
            src = js_path.read_text(encoding="utf-8")
            for m in _ID_CALL_RE.finditer(src):
                used_ids.add(m.group(1) or m.group(2))
            own_generated_ids |= set(_ID_ATTR_RE.findall(src))
        allowed = html_ids | own_generated_ids | NAV_GENERATED_IDS
        missing = used_ids - allowed
        if missing:
            problems.append(f"{html_path.name}: {sorted(missing)}")
    assert not problems, (
        "存在しない id を参照している（動的組み立てなら DYNAMIC_ID_LOOKUPS に追記すること）:\n"
        + "\n".join(problems)
    )


def test_dynamic_id_lookups_whitelist_targets_exist():
    """動的 id 組み立て（regex では拾えない `$('prefix-' + var)` 形）のホワイトリストが指す id が、
    実際に対応する HTML に存在することを確認する（ホワイトリストの形骸化防止）。"""
    for js_name, ids in DYNAMIC_ID_LOOKUPS.items():
        owner_html = [h for h in sorted(WEB.glob("*.html")) if js_name in _own_script_srcs(h)]
        assert owner_html, f"{js_name} を読み込む HTML が見つからない"
        html_ids: set[str] = set()
        for h in owner_html:
            html_ids |= set(_ID_ATTR_RE.findall(h.read_text(encoding="utf-8")))
        missing = [i for i in ids if i not in html_ids]
        assert not missing, f"{js_name} の動的 id ホワイトリストが実在しない id を含む: {missing}"


def test_inline_onclick_functions_are_defined_in_page_js():
    """HTML の inline `on*="funcName(...)"`（データ非混入・test_no_inline_handlers_in_web_js の
    対象＝onclick="...${...}" とは別物）が参照する関数名が、そのページの JS に実際に定義されて
    いることを確認する（逆向きの整合性。例: ingest.html の setState/closePrev・
    workspace.html の loadFiles。admin-users.html/chat.html の inline は
    `document.getElementById(...).hidden=true` のみで関数呼び出しが無いため対象が0件になる）。"""
    problems = []
    for html_path in sorted(WEB.glob("*.html")):
        text = html_path.read_text(encoding="utf-8")
        names = sorted(set(_INLINE_ONCLICK_CALL_RE.findall(text)))
        if not names:
            continue
        own_js = _own_script_srcs(html_path)
        js_src = "".join((WEB / rel).read_text(encoding="utf-8") for rel in own_js)
        for name in names:
            defined = (
                re.search(rf"\bfunction\s+{re.escape(name)}\s*\(", js_src)
                or re.search(rf"\b(?:const|let|var)\s+{re.escape(name)}\s*=", js_src)
            )
            if not defined:
                problems.append(f"{html_path.name}: on*=\"{name}(...)\" だが {own_js} に定義が見つからない")
    assert not problems, "\n".join(problems)


# ===== 3. script タグ順 golden =====

def _script_order_lines() -> list[str]:
    lines = []
    for html_path in sorted(WEB.glob("*.html")):
        text = html_path.read_text(encoding="utf-8")
        for m in _SCRIPT_SRC_RE.finditer(text):
            pre, src, post = m.groups()
            type_m = re.search(r'type="([^"]+)"', pre + post)
            lines.append(f"{html_path.name}\t{src}\t{type_m.group(1) if type_m else '(none)'}")
    return lines


def test_script_tag_order_matches_golden():
    """全 web/*.html の `<script src type>` 出現順が golden と完全一致する。

    common.js 分離（S2）・module 化（S4）で1ページでも読み込み順を崩すと検知する
    （危険地雷8「nav.js は classic 維持必須」・危険地雷9「common.js は全15 HTML の読み込み順編集」）。
    """
    assert _SCRIPT_ORDER_GOLDEN.exists(), f"golden 未生成: {_SCRIPT_ORDER_GOLDEN}（module docstring の手順で作成）"
    expected = _SCRIPT_ORDER_GOLDEN.read_text(encoding="utf-8").splitlines()
    actual = _script_order_lines()
    assert actual == expected, (
        "web/*.html の <script src type> 出現順が golden と不一致。意図した読み込み順変更なら "
        "module docstring の手順で golden を更新すること。\n"
        f"追加/変化した行: {[l for l in actual if l not in expected]}\n"
        f"消えた行: {[l for l in expected if l not in actual]}"
    )


# ===== 4. window 公開名 golden/allowlist =====

def test_nav_sherpa_members_match_fixed_set():
    """`window.Sherpa`（common.js の共通ユーティリティ・フェーズ6 S2 で nav.js から分離）の
    メンバー構成が固定9種のまま変わっていない（9種目= POLL-1 の visibilityInterval）。"""
    text = (WEB / "common.js").read_text(encoding="utf-8")
    m = re.search(r"window\.Sherpa\s*=\s*window\.Sherpa\s*\|\|\s*\{(.*?)\n\};", text, re.S)
    assert m, "window.Sherpa の定義が見つからない（common.js の実装が変わった）"
    members = set(_NAV_SHERPA_MEMBER_RE.findall(m.group(1)))
    assert members == NAV_SHERPA_MEMBERS, (
        f"window.Sherpa のメンバー構成が変わった: 追加={members - NAV_SHERPA_MEMBERS} "
        f"消失={NAV_SHERPA_MEMBERS - members}"
    )


def test_window_assignments_are_allowlisted():
    """`window.<name> =` 代入は allowlist の名前のみ（「module は window を汚さない」契約。
    例外は common.js の Sherpa と chat.js の e2e seam __sherpaChatTest のみ・危険地雷1相当）。"""
    found: set[str] = set()
    for p in _web_js_files():
        found |= set(_WINDOW_ASSIGN_RE.findall(p.read_text(encoding="utf-8")))
    assert found == WINDOW_ASSIGNMENT_ALLOWLIST, (
        f"window.* 代入が allowlist と不一致: 追加={found - WINDOW_ASSIGNMENT_ALLOWLIST} "
        f"消失={WINDOW_ASSIGNMENT_ALLOWLIST - found}"
    )


# ===== 5. import グラフ突合（フェーズ6 S4） =====

def _module_script_entries() -> dict[str, pathlib.Path]:
    """`<script type="module" src="...">` の entry を HTML名→ファイルパスで返す（module 化済ページのみ）。"""
    entries: dict[str, pathlib.Path] = {}
    for html_path in sorted(WEB.glob("*.html")):
        text = html_path.read_text(encoding="utf-8")
        for m in _SCRIPT_SRC_RE.finditer(text):
            pre, src, post = m.groups()
            if 'type="module"' in (pre + post):
                entries[html_path.name] = (WEB / src).resolve()
    return entries


def _static_import_targets(js_path: pathlib.Path) -> list[pathlib.Path]:
    """js_path 内の静的 import 文が指す相対パスを、実ファイルパス（解決のみ・実在確認はしない）で返す。"""
    src = js_path.read_text(encoding="utf-8")
    return [(js_path.parent / spec).resolve() for spec in _STATIC_IMPORT_RE.findall(src)]


def test_static_imports_resolve_to_existing_files():
    """web/**/*.js の静的 import 文が指すファイルが実在すること（危険地雷10「404 は実行時まで検知
    されない」の静的側の代替）。"""
    problems = []
    for p in _web_js_files():
        for target in _static_import_targets(p):
            if not target.exists():
                problems.append(f"{p.relative_to(WEB)} -> import 先が存在しない: {target}")
    assert not problems, "\n".join(problems)


def test_no_dynamic_imports_in_web_js():
    """動的 import() の禁止（危険地雷7・10: DCL 後初期化の死ダイアログ／実行時までエラー不可視の
    温床）。RV LOW（2026-07-14 フェーズ6）: 従来は地雷本文の禁則のみで機械化されていなかった。"""
    problems = []
    for p in _web_js_files():
        for i, line in enumerate(p.read_text(encoding="utf-8").splitlines(), 1):
            stripped = line.lstrip()
            if stripped.startswith(("//", "*", "/*")):
                continue   # コメント内の言及（設計メモ等）は許容
            if _DYNAMIC_IMPORT_RE.search(line):
                problems.append(f"{p.relative_to(WEB)}:{i}: {line.strip()[:80]}")
    assert not problems, "動的 import() は禁止（フェーズ6 危険地雷7・10）:\n" + "\n".join(problems)


def test_chat_module_graph_has_no_orphans():
    """chat.html の module entry（chat.js）から静的 import を辿った到達集合に、web/chat/ 配下の
    全ファイルが含まれること（孤児＝どこからも import されず配線が漏れた分割後ファイルの検知）。"""
    entries = _module_script_entries()
    assert "chat.html" in entries, 'chat.html に <script type="module"> entry が見つからない'
    entry = entries["chat.html"]
    seen = {entry}
    stack = [entry]
    while stack:
        cur = stack.pop()
        for target in _static_import_targets(cur):
            if target not in seen:
                seen.add(target)
                stack.append(target)
    chat_dir = WEB / "chat"
    all_chat_files = {p.resolve() for p in chat_dir.rglob("*.js")} if chat_dir.exists() else set()
    orphans = {p.relative_to(WEB) for p in (all_chat_files - seen)}
    assert not orphans, f"web/chat/ 配下に孤児（どこからも import されない）ファイルがある: {sorted(orphans)}"


def _write_goldens() -> None:
    """現状の web/ から golden を書き出す（更新手順は module docstring 参照）。"""
    _SCRIPT_ORDER_GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    _SCRIPT_ORDER_GOLDEN.write_text("\n".join(_script_order_lines()) + "\n", encoding="utf-8")
