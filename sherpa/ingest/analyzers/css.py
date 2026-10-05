"""CSS アナライザ。`.css` を全件受理し、ファイル自体を主体定義（`Module`・拡張子込みファイル名）とする（children なし）。

読み取りは Tree-sitter（tree-sitter-css）の木の上で行う。参照は `@import url("x.css")`／`@import "x.css"` → `INVOKES(via=include)`（C アナライザと同じ2段解決）のみ。セレクタ・プロパティ値の `url(...)` は対象外。
外部参照スキームは `Dropped("web_external_ref")`、`/` 始まりは top scope へ連結して相対パス化、`?`/`#` 以降は除去する。コメントは木の上のコメントノードなので読まない。構文エラーの領域は `Dropped("syntax_error")` で申告し、エラーの外は読み続ける。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import posixpath
from pathlib import PurePosixPath

from . import _ts
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult


CSS_EXT = frozenset({".css"})

# 外部参照スキーム（include 対象外）。
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
    """`jsp._scope_relative_include_path` と同じ規則（共有モジュールは作らず重複させる）。"""
    stripped = raw_path.lstrip("/")
    if "/" not in ref_rel:
        return stripped
    top_scope = ref_rel.split("/", 1)[0]
    target = f"{top_scope}/{stripped}"
    base_dir = ref_rel.rsplit("/", 1)[0]
    return posixpath.relpath(target, start=base_dir)


def _emit_include(raw_path: str, line: int, ref_rel: str, refs: list, dropped: list) -> None:
    """`jsp._emit_include` と同じ規則（`<base>` の概念が無いので `has_base` は持たない）。"""
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
    version = 2

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        parsed = _ts.parse("css", text)
        refs: list = []
        dropped: list = []
        stack = [parsed.root]
        while stack:
            node = stack.pop()
            if node.type == "import_statement":
                target = _import_target(parsed, node)
                if target is not None:
                    _emit_include(target, _ts.start_line(node), rel_path, refs, dropped)
                continue
            stack.extend(reversed(node.children))
        dropped.extend(_ts.syntax_errors(parsed))
        return RefResult(refs=refs, dropped=dropped)


def _import_target(parsed: _ts.Parsed, node) -> str | None:
    """`@import` の取り込み先（`url(...)` の中、または文字列）。引用符の内側を生のまま返す。"""
    for c in node.named_children:
        if c.type == "string_value":
            return parsed.text(c)[1:-1]
        if c.type == "call_expression":
            fn = c.child_by_field_name("function") or c.children[0]
            if parsed.text(fn).lower() != "url":
                return None
            for a in c.named_children:
                if a.type == "arguments":
                    for v in a.named_children:
                        if v.type == "string_value":
                            return parsed.text(v)[1:-1]
                        if v.type == "plain_value":
                            return parsed.text(v).strip()
            return None
    return None
