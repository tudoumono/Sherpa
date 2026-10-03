"""JS アナライザ。`.js`/`.mjs` を全件受理し（`.ts` は対象外）、ファイル自体を主体定義（`Module`・拡張子込みファイル名）とする（children なし）。

参照抽出（コメントは空白化し、文字列/テンプレートリテラルの中身は残す）:
- `import ... from "./y.js"`／`require("./y")`／`importScripts("…")` → `INVOKES(via=include)`（拡張子なしは `.js` を補う・C アナライザと同じ2段解決）。`import`/`require` は `.` 始まりのパスに限る（裸のパッケージ名は除く）。外部参照スキームは `Dropped("web_external_ref")`、`/` 始まりは top scope へ連結して相対パス化、`?`/`#` 以降は除去する。
- 文字列リテラルの URL（`fetch`・`$.ajax({url})`・`axios.*`・`XMLHttpRequest.open`・`location.href =`・`.action =`）→ `ACCESSES(via=config_key)`（`/` 始まりは URL キー、`.action` は Struts action キー）。呼び出し元の型は検証しない粗い判定。
- 動的連結（`"/orders/" + id`）は `Dropped("js_dynamic_url")`（一致区間単位で除外する）。
- minified（`.min.js`、または1行4096文字超）は `Dropped("js_minified")` を1件申告し、抽出しない。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import bisect
import posixpath
import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

JS_EXT = frozenset({".js", ".mjs"})

_MINIFIED_LINE_LEN = 4096

# 外部参照スキーム（include 対象外）。
_EXTERNAL_SCHEMES = ("http:", "https:", "//", "data:", "javascript:", "mailto:", "#")


def _is_external_ref(path: str) -> bool:
    return path.startswith(_EXTERNAL_SCHEMES)


def _strip_query_fragment(val: str) -> str:
    """`?`/`#` 以降を除去する（先に出現した方で切る）。"""
    cut = len(val)
    for ch in ("?", "#"):
        idx = val.find(ch)
        if idx != -1 and idx < cut:
            cut = idx
    return val[:cut]


def _scope_relative_include_path(ref_rel: str, raw_path: str) -> str:
    """`jsp._scope_relative_include_path` と同じ規則（共有モジュールは作らず重複させる）。"""
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
    """コメントだけを空白化し、文字列/テンプレートリテラルの中身は残す（URL/パスの読み取り用）。"""
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


# `import ... from "./x"`（side-effect の `import "./x"` も含む）。単一物理行内に限る。
_IMPORT_FROM = re.compile(r'\bimport\b[^"\'\n]*?["\'](?P<path>\.\.?/[^"\']+)["\']')
# `require("./x")`。
_REQUIRE = re.compile(r'\brequire\(\s*["\'](?P<path>\.\.?/[^"\']+)["\']\s*\)')
# `importScripts("x")`。
_IMPORT_SCRIPTS = re.compile(r'\bimportScripts\(\s*["\'](?P<path>[^"\']+)["\']')

_DYNAMIC_URL_HINT = re.compile(r'["\'](?P<path>/[^"\']*)["\']\s*\+')

# URL/パス文字列リテラルを引数に取る呼び出し（`fetch`/`axios.*`/`.open(method, url)`/`url:`／`location.href =`／`.action =`）。
_URL_CALL_SITES = re.compile(
    r'\bfetch\(\s*["\'](?P<path_fetch>/[^"\']*)["\']'
    r'|\baxios\.[a-zA-Z]+\(\s*["\'](?P<path_axios>/[^"\']*)["\']'
    r'|\.open\(\s*["\'][A-Za-z]+["\']\s*,\s*["\'](?P<path_xhr>/[^"\']*)["\']'
    r'|\burl\s*:\s*["\'](?P<path_url_key>/[^"\']*)["\']'
    r'|location\.href\s*=\s*["\'](?P<path_href>/[^"\']*)["\']'
    r'|\.action\s*=\s*["\'](?P<path_action>[^"\']*)["\']'
)


def _config_key_name(val: str) -> tuple[str, str] | None:
    """URL 文字列リテラルから `(名前, 種別)` を返す（種別は `extra["key_kind"]`）。`?`/`#` 以降は除去し、外部参照スキームは対象外。"""
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
    """`jsp._emit_include` と同じ規則（`<base>` が無いので `has_base` は持たない）。拡張子なしは `.js` を補ってから `include_path` にする。"""
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
    """ファイル自体 → `Module`（primary）。import/require/importScripts → `INVOKES(via=include)`。URL 文字列リテラル → `ACCESSES(via=config_key)`。動的連結/minified → `Dropped`。"""

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

        # 行配列は1回だけ作る（ループ内で毎回分割すると二次時間になる）。
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

        # `_URL_CALL_SITES` と `dynamic_spans` はともに位置昇順なので、単調ポインタで1回だけ突合する。
        dyn_idx, n_dyn = 0, len(dynamic_spans)
        for m in _URL_CALL_SITES.finditer(sanitized):
            u_start, u_end = m.start(), m.end()
            while dyn_idx < n_dyn and dynamic_spans[dyn_idx][1] <= u_start:
                dyn_idx += 1
            overlaps_dynamic = dyn_idx < n_dyn and dynamic_spans[dyn_idx][0] < u_end
            if overlaps_dynamic:
                continue  # 動的連結側と一致区間が重なる＝既に Dropped 済み
            val = next(v for v in m.groups() if v is not None)
            line = _line_at(newline_offsets, u_start)
            resolved = _config_key_name(val)
            if resolved is not None:
                key, kind = resolved
                refs.append(RefCandidate("ACCESSES", "Config", key, line,
                                         extra={"via": "config_key", "key_kind": kind}))

        return RefResult(refs=refs, dropped=dropped)
