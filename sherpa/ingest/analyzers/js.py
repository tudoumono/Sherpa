"""JS アナライザ（docs/proposals/2026-09-05-アナライザ拡張.md §13 波3 レーン A）。

`.js`/`.mjs` を**全件受理**する（`.ts` は対象外・型情報が別に要る言語のため本スライスのスコープ外）。
ファイル自体を主体定義（`Module`・primary・拡張子込みファイル名）とし、children は持たない
——動的言語のため関数を構造的な子定義として抽出しない（検出限界として明記・呼び出し先解決も
行わない）。

**参照抽出**（コメント `//`・`/* */` の中身は空白化してから走査・文字列/テンプレートリテラルの
中身は温存する——URL/パスは文字列リテラルの中に書かれるため）:
- `import x from "./y.js"`／`require("./y")`／`importScripts("…")` → `INVOKES(via=include)`
  （拡張子なしは `.js` を補ってから `include_path` にする・C アナライザと同じ2段解決）。
  バックティック始まり（`import`/`require` はバックティックの識別子形パス）は対象外——node_modules
  由来の裸パッケージ名（`import React from "react"`）と区別するため、`import`/`require` は `.`
  始まり（`./`/`../`）のパスに限定する。`importScripts` はワーカースクリプトの慣習上バックティック
  も含め任意の文字列を受理する。外部参照スキーム（`http:`/`https:`/`//`/`data:`/`javascript:`/
  `mailto:`/`#`）は `Dropped("web_external_ref", line, uri)`（include 対象外）。`/` 始まりは参照元
  top scope へ連結して参照元からの相対パスに変換する。`?`/`#` 以降は basename/`include_path` 算出前に
  除去する。
- 文字列リテラルの URL（`fetch("/api/x")`・`$.ajax({url: "/x"})`・`axios.get("/x")`・
  `XMLHttpRequest.open("GET", "/x")`・`location.href = "/x"`・`form.action = "x.action"`）→
  `ACCESSES(via=config_key)`（`/` 始まりはそのまま URL キー、`.action` 拡張子は Struts action キー
  ＝拡張子と先頭 `/` を落とす）。判定前に `?`/`#` 以降を除去し、外部参照スキーム（`//` 始まりの
  protocol-relative URL 等）は対象外にする。**構造情報は見ない**（呼び出し元オブジェクトの型までは
  検証しない・`url:` キーはどの呼び出しの引数でも同じ扱いになる＝粗い判定の裏返しの検出限界）。
- 動的な連結（文字列リテラル直後に `+` 結合・例 `"/orders/" + id`）は静的な値が決まらないため
  `Dropped("js_dynamic_url", line)`（同じ行の他の静的 URL 呼び出しは、連結の一致区間と重ならない
  限り通常どおり参照になる——除外は行単位ではなく match span 単位で行う）。
- minified（`.min.js` ファイル名、または1行が4096文字超）は `Dropped("js_minified", 1)` を1件
  申告し、それ以上の参照抽出を行わない（サニタイズ自体もコスト削減のためスキップする）。

正規表現による粗い判定のため、コメント/文字列外に偶然同じ字面（`fetch(`・`url:` 等）が現れた場合の
誤検出は考慮しない（他の言語アナライザと同じ限界）。JS の正規表現リテラル（`/pattern/`）はコメント
判定の対象にしない（`//`/`/*` の2文字のみを見るため単独の `/` はそのまま素通りする）。
"""
from __future__ import annotations

import bisect
import posixpath
import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

JS_EXT = frozenset({".js", ".mjs"})

_MINIFIED_LINE_LEN = 4096

# 外部参照スキーム（include 対象外・URL キーにもしない）。
_EXTERNAL_SCHEMES = ("http:", "https:", "//", "data:", "javascript:", "mailto:", "#")


def _is_external_ref(path: str) -> bool:
    return path.startswith(_EXTERNAL_SCHEMES)


def _strip_query_fragment(val: str) -> str:
    """`?`/`#` 以降を除去する（先に出現した方で切る・action判定/拡張子判定/basename取得の前段）。"""
    cut = len(val)
    for ch in ("?", "#"):
        idx = val.find(ch)
        if idx != -1 and idx < cut:
            cut = idx
    return val[:cut]


def _scope_relative_include_path(ref_rel: str, raw_path: str) -> str:
    """`jsp._scope_relative_include_path` と同じ規則（重複させる・共有モジュールを増やさない方針）。"""
    stripped = raw_path.lstrip("/")
    if "/" not in ref_rel:
        return stripped
    top_scope = ref_rel.split("/", 1)[0]
    target = f"{top_scope}/{stripped}"
    base_dir = ref_rel.rsplit("/", 1)[0]
    return posixpath.relpath(target, start=base_dir)


def _is_minified(rel_path: str, text: str) -> bool:
    if PurePosixPath(rel_path).name.lower().endswith(".min.js"):
        return True
    return any(len(ln) > _MINIFIED_LINE_LEN for ln in text.splitlines())


def _sanitize_comments_only(text: str) -> str:
    """コメント（`//`・`/* */`）だけを空白化し、文字列/テンプレートリテラルの中身は残す
    （URL/パスの読み取りに必要・c.py の同名ヘルパと同じ役割、テンプレートリテラルのバックティックも
    文字列と同格に扱う点だけが異なる）。"""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        ch = text[i]
        if ch == "/" and text[i:i + 2] == "/*":
            out.append("  ")
            i += 2
            while i < n and text[i:i + 2] != "*/":
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append("  ")
                i += 2
            continue
        if ch == "/" and text[i:i + 2] == "//":
            out.append("  ")
            i += 2
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch in ('"', "'", "`"):
            quote = ch
            out.append(ch)
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                out.append(text[i])
                i += 1
            if i < n:
                out.append(quote)
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _newline_offsets(text: str) -> list:
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


# `import ... from "./x"` （side-effect の `import "./x"` も含む・`from` は任意）。単一物理行内に
# 限定する（`[^"'\n]` で改行を跨がせない＝複数行 import 文は見逃す・C アナライザと同じ安全側の限界）。
_IMPORT_FROM = re.compile(r'\bimport\b[^"\'\n]*?["\'](?P<path>\.\.?/[^"\']+)["\']')
# `require("./x")`。
_REQUIRE = re.compile(r'\brequire\(\s*["\'](?P<path>\.\.?/[^"\']+)["\']\s*\)')
# `importScripts("x")`。
_IMPORT_SCRIPTS = re.compile(r'\bimportScripts\(\s*["\'](?P<path>[^"\']+)["\']')

# 文字列リテラル直後の `+`（動的連結）。
_DYNAMIC_URL_HINT = re.compile(r'["\'](?P<path>/[^"\']*)["\']\s*\+')

# URL/パス文字列リテラルを引数に取る呼び出し（`fetch`/`axios.*`/`.open(method, url)`/`url:`
# キー＝`$.ajax({url: ...})` 相当／`location.href =`／`.action =`）。
_URL_CALL_SITES = re.compile(
    r'\bfetch\(\s*["\'](?P<path_fetch>/[^"\']*)["\']'
    r'|\baxios\.[a-zA-Z]+\(\s*["\'](?P<path_axios>/[^"\']*)["\']'
    r'|\.open\(\s*["\'][A-Za-z]+["\']\s*,\s*["\'](?P<path_xhr>/[^"\']*)["\']'
    r'|\burl\s*:\s*["\'](?P<path_url_key>/[^"\']*)["\']'
    r'|location\.href\s*=\s*["\'](?P<path_href>/[^"\']*)["\']'
    r'|\.action\s*=\s*["\'](?P<path_action>[^"\']*)["\']'
)


def _config_key_name(val: str) -> tuple[str, str] | None:
    """URL 文字列リテラルから `(名前, 種別)` を返す（種別は `extra["key_kind"]` として
    `RefCandidate` にそのまま渡す・Config キーの名前空間分離）。判定前に `?`/`#` 以降を除去し、
    外部参照スキーム（`//` 始まりの protocol-relative URL 等）は対象外にする。"""
    val = _strip_query_fragment(val)
    if not val or _is_external_ref(val):
        return None
    if val.endswith(".action"):
        bare = val[: -len(".action")]
        if bare.startswith("/"):
            bare = bare[1:]
        return (bare, "action") if bare else None
    return (val, "url") if val.startswith("/") else None


def _emit_include(raw_path: str, line: int, ref_rel: str, refs: list, dropped: list) -> None:
    """`jsp._emit_include` と同じ規則だが `<base>` の概念が無いため `has_base` を持たない。
    拡張子なしは `.js` を補ってから `include_path` にする（`import`/`require`/`importScripts`
    いずれも同じ扱い・既存挙動を維持）。"""
    path = raw_path.replace("\\", "/")
    if _is_external_ref(path):
        dropped.append(Dropped("web_external_ref", line, path))
        return
    path = _strip_query_fragment(path)
    if not path:
        return
    if path.startswith("/"):
        path = _scope_relative_include_path(ref_rel, path)
    if not PurePosixPath(path).suffix:
        path += ".js"
    basename = PurePosixPath(path).name
    if basename:
        refs.append(RefCandidate("INVOKES", "Module", basename, line,
                                 extra={"via": "include", "include_path": path}))


class JsAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary・拡張子込みファイル名）。children なし。
    import/require/importScripts → `INVOKES(via=include)`。URL 文字列リテラル →
    `ACCESSES(via=config_key)`。動的連結/minified → `Dropped`。"""

    name = "js"
    extensions = JS_EXT
    doctype = "js"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        if _is_minified(rel_path, text):
            return RefResult(dropped=[Dropped("js_minified", 1, "")])

        sanitized = _sanitize_comments_only(text)
        newline_offsets = _newline_offsets(text)
        refs: list = []
        dropped: list = []

        for pattern in (_IMPORT_FROM, _REQUIRE, _IMPORT_SCRIPTS):
            for m in pattern.finditer(sanitized):
                line = _line_at(newline_offsets, m.start())
                _emit_include(m.group("path"), line, rel_path, refs, dropped)

        # 行配列は1回だけ作る——`sanitized.splitlines()` をループ内で毎回呼ぶと動的連結の
        # 出現件数に比例して全文再分割のコストが掛かり二次時間になる。
        sanitized_lines = sanitized.splitlines()
        dynamic_spans = [m.span() for m in _DYNAMIC_URL_HINT.finditer(sanitized)]
        seen_lines: set = set()
        for start, end in dynamic_spans:
            line = _line_at(newline_offsets, start)
            if line in seen_lines:
                continue
            seen_lines.add(line)
            snippet = sanitized_lines[line - 1].strip()[:120]
            dropped.append(Dropped("js_dynamic_url", line, snippet))

        # `_URL_CALL_SITES.finditer` は出現順（位置昇順）で返し、`dynamic_spans` も同じ順で
        # 作られている（`_DYNAMIC_URL_HINT.finditer` も位置昇順・互いに重ならない）ため、
        # 単調ポインタで1回だけ突合する——URL 候補ごとに `dynamic_spans` 全件を線形走査する
        # 二次時間を避ける。
        dyn_idx, n_dyn = 0, len(dynamic_spans)
        for m in _URL_CALL_SITES.finditer(sanitized):
            u_start, u_end = m.start(), m.end()
            while dyn_idx < n_dyn and dynamic_spans[dyn_idx][1] <= u_start:
                dyn_idx += 1
            overlaps_dynamic = dyn_idx < n_dyn and dynamic_spans[dyn_idx][0] < u_end
            if overlaps_dynamic:
                continue                                # 動的連結側と一致区間が重なる＝既に Dropped 済み
            val = next(v for v in m.groups() if v is not None)
            line = _line_at(newline_offsets, u_start)
            resolved = _config_key_name(val)
            if resolved is not None:
                key, kind = resolved
                refs.append(RefCandidate("ACCESSES", "Config", key, line,
                                         extra={"via": "config_key", "key_kind": kind}))

        return RefResult(refs=refs, dropped=dropped)
