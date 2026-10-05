"""静的 HTML テンプレートアナライザ。ファイル自体を主体定義（`Module`・拡張子込みファイル名）とする（children なし）。

`accepts()` は先頭64KB のアプリ画面の目印（`<form`・`<script src`・`<link rel="stylesheet">`・JSP/JSF/Thymeleaf のタグ/属性・EL式・`.action` への `<a href>`）があれば受理し、無ければ decline する。decline したファイルは `ingest.text_kind` の通常の内容推定へ回る（日本語本文主体の HTML は資料側、フォーム付き画面はコード側）。
読み取りは標準の `html.parser`（Tree-sitter には載せない）で開始タグだけを読む。コメント・`<script>`/`<style>` の本文の中のタグは読まない。参照抽出は `jsp.py` と同じ形:
- `<script src>`／`<iframe src>`／`<link href>` → `INVOKES(via=include)`。外部参照スキームは `Dropped("web_external_ref")`、`<base href>` があるファイルの相対 include は `Dropped("web_relative_under_base")`。
- `action=`/`href=` → `ACCESSES(via=config_key)`（`.action`／裸名＝Struts action キー、`/` 始まり拡張子なし＝URL キー）。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import posixpath
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

HTML_EXT = frozenset({".html", ".htm", ".xhtml"})

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


# HTML コメントの開始マーカー（`accepts()` の目印判定で、コメントアウトされた目印を除くために空白化する）。
_COMMENT_MARKER_RE = re.compile(r'<!--')


def _sanitize_comments(text: str) -> str:
    """`<!-- ... -->` を線形1パスで空白化する（改行は保持。閉じていないコメントは末尾まで）。"""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        m = _COMMENT_MARKER_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        end = text.find("-->", m.end())
        j = end + 3 if end != -1 else n
        out.append("".join("\n" if c == "\n" else " " for c in text[m.start():j]))
        i = j
    return "".join(out)


_BARE_ACTION_NAME = re.compile(r'^[A-Za-z0-9_-]+$')


def _config_key_from_action_or_href(val: str) -> tuple[str, str] | None:
    """`jsp._config_key_from_action_or_href` と同じ規則（`(名前, 種別)` を返す・重複させる）。種別は `extra["key_kind"]` として渡す。`?`/`#` 以降は除去し、外部参照スキームは対象外。"""
    if not val:
        return None
    val = _strip_query_fragment(val)
    if not val or _is_external_ref(val):
        return None
    if val.endswith(".action"):
        bare = val[: -len(".action")]
        if bare.startswith("/"):
            bare = bare[1:]
        return (bare, "action") if bare else None
    if val.startswith("/"):
        last_seg = val.rsplit("/", 1)[-1]
        return (val, "url") if "." not in last_seg else None
    return (val, "action") if _BARE_ACTION_NAME.match(val) else None


def _emit_include(raw_path: str, line: int, ref_rel: str, refs: list, dropped: list,
                  has_base: bool) -> None:
    """`jsp._emit_include` と同じ規則。"""
    path = raw_path.replace("\\", "/")
    if _is_external_ref(path):
        dropped.append(Dropped("web_external_ref", line, path))
        return
    path = _strip_query_fragment(path)
    if not path:
        return
    if path.startswith("/"):
        path = _scope_relative_include_path(ref_rel, path)
    elif has_base:
        dropped.append(Dropped("web_relative_under_base", line, path))
        return
    basename = PurePosixPath(path).name
    if basename:
        refs.append(RefCandidate("INVOKES", "Module", basename, line,
                                 extra={"via": "include", "include_path": path}))


class _TagCollector(HTMLParser):
    """開始タグ（自己終了タグ含む）を `(行, タグ名, 属性の辞書)` の列で集める。タグ名・属性名は小文字・値なしの属性は含めない。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((self.getpos()[0], tag.lower(),
                          {k.lower(): v for k, v in attrs if v is not None}))


def _collect_tags(text: str) -> list:
    parser = _TagCollector()
    parser.feed(text)
    parser.close()
    return parser.tags


# content-aware accepts()

_HEAD_SNIFF_BYTES = 64 * 1024

_ACCEPT_MARKER_PATTERNS = (
    re.compile(r'<form\b', re.I),
    re.compile(r'<script\b[^>]*\bsrc\s*=', re.I),
    re.compile(r'<iframe\b[^>]*\bsrc\s*=', re.I),
    re.compile(r'<link\b[^>]*\brel\s*=\s*["\']?stylesheet', re.I),
    re.compile(r'<jsp:', re.I),
    re.compile(r'<h:[a-zA-Z]'),
    re.compile(r'[\s<]th:[a-zA-Z-]+\s*='),
    re.compile(r'\$\{[^}]*\}'),
    re.compile(r'<%(?!--)'),
    re.compile(r'<a\b[^>]*\bhref\s*=\s*["\'][^"\']*\.action\b', re.I),
)


class HtmlTemplateAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary）。script/link → `INVOKES(via=include)`。action/href → `ACCESSES(via=config_key)`。"""

    name = "html"
    extensions = HTML_EXT
    doctype = "html"
    version = 2
    fallback_to_text_kind_when_declined = True
    # `accepts()` の目印検出は先頭64KiBを見る。読取側（`registry.resolve_lazy`・`world_graph.build_world`）もこの値に従って head を広げる。
    head_bytes = _HEAD_SNIFF_BYTES

    def accepts(self, rel_path: str, head_text: str = "") -> bool:
        # コメントアウトされた過去のフォーム/スクリプトを誤検出しないよう、コメントを除去してから判定する。
        head = _sanitize_comments(head_text[:_HEAD_SNIFF_BYTES])
        return any(p.search(head) for p in _ACCEPT_MARKER_PATTERNS)

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        refs: list = []
        dropped: list = []

        tags = _collect_tags(text)
        base_line = next((line for line, tag, attrs in tags if tag == "base" and "href" in attrs), None)
        has_base = base_line is not None
        if has_base:
            dropped.append(Dropped("web_base_href", base_line, ""))

        for line, tag, attrs in tags:
            if tag == "base":
                continue
            if tag == "script" and attrs.get("src"):
                _emit_include(attrs["src"], line, rel_path, refs, dropped, has_base)
            elif tag == "iframe" and attrs.get("src"):
                _emit_include(attrs["src"], line, rel_path, refs, dropped, has_base)
            elif tag == "link" and attrs.get("href"):
                _emit_include(attrs["href"], line, rel_path, refs, dropped, has_base)

            for key_attr in ("action", "href"):
                if key_attr in attrs:
                    resolved = _config_key_from_action_or_href(attrs[key_attr])
                    if resolved is not None:
                        key, kind = resolved
                        refs.append(RefCandidate("ACCESSES", "Config", key, line,
                                                 extra={"via": "config_key", "key_kind": kind}))

        return RefResult(refs=refs, dropped=dropped)
