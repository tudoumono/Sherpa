"""CSS アナライザ（docs/proposals/2026-09-05-アナライザ拡張.md §13 波3 レーン A）。

`.css` を**全件受理**する。ファイル自体を主体定義（`Module`・primary・拡張子込みファイル名）とし、
children は持たない。参照は `@import url("x.css")`／`@import "x.css"`（`url(...)` の引用符省略形
`@import url(x.css)` も含む）→ `INVOKES(via=include)`（C アナライザと同じ2段解決）のみ。
セレクタ・プロパティ値（`background: url(...)` 等の画像/フォント参照を含む）は対象外
——`@import` 以外は何もしない（検出限界として明記）。

外部参照スキーム（`http:`/`https:`/`//`/`data:`/`javascript:`/`mailto:`/`#`）は
`Dropped("web_external_ref", line, uri)`（include 対象外）。`/` 始まりは参照元の top scope へ
連結して参照元ファイルからの相対パスに変換する。`?`/`#` 以降は basename/`include_path` 算出前に
除去する。

コメント（`/* ... */`）の中身は空白化してから走査する（CSS に行コメント `//` は無い）。
"""
from __future__ import annotations

import bisect
import posixpath
import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

CSS_EXT = frozenset({".css"})

# 外部参照スキーム（include 対象外・URL キーにもしない）。
_EXTERNAL_SCHEMES = ("http:", "https:", "//", "data:", "javascript:", "mailto:", "#")


def _is_external_ref(path: str) -> bool:
    return path.startswith(_EXTERNAL_SCHEMES)


def _strip_query_fragment(val: str) -> str:
    """`?`/`#` 以降を除去する（先に出現した方で切る・basename取得の前段）。"""
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


_COMMENT_RE = re.compile(r'/\*.*?\*/', re.S)


def _blank(m: re.Match) -> str:
    return "".join("\n" if c == "\n" else " " for c in m.group(0))


def _sanitize_comments(text: str) -> str:
    return _COMMENT_RE.sub(_blank, text)


# `@import url("x.css")` / `@import url('x.css')` / `@import url(x.css)`（引用符省略）。
_IMPORT_URL_FN = re.compile(r'@import\s+url\(\s*["\']?(?P<path>[^"\')]+)["\']?\s*\)')
# `@import "x.css"` / `@import 'x.css'`（`url()` を使わない形）。
_IMPORT_BARE = re.compile(r'@import\s+["\'](?P<path>[^"\']+)["\']')


def _newline_offsets(text: str) -> list:
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


def _emit_include(raw_path: str, line: int, ref_rel: str, refs: list, dropped: list) -> None:
    """`jsp._emit_include` と同じ規則だが `<base>` の概念が無いため `has_base` を持たない。"""
    path = raw_path.replace("\\", "/")
    if _is_external_ref(path):
        dropped.append(Dropped("web_external_ref", line, path))
        return
    path = _strip_query_fragment(path)
    if not path:
        return
    if path.startswith("/"):
        path = _scope_relative_include_path(ref_rel, path)
    basename = PurePosixPath(path).name
    if basename:
        refs.append(RefCandidate("INVOKES", "Module", basename, line,
                                 extra={"via": "include", "include_path": path}))


class CssAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary・拡張子込みファイル名）。children なし。
    `@import` → `INVOKES(via=include)`。それ以外は何もしない。"""

    name = "css"
    extensions = CSS_EXT
    doctype = "css"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize_comments(text)
        newline_offsets = _newline_offsets(text)
        refs: list = []
        dropped: list = []
        for pattern in (_IMPORT_URL_FN, _IMPORT_BARE):
            for m in pattern.finditer(sanitized):
                line = _line_at(newline_offsets, m.start())
                _emit_include(m.group("path"), line, rel_path, refs, dropped)
        return RefResult(refs=refs, dropped=dropped)
