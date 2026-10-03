"""静的 HTML テンプレートアナライザ。ファイル自体を主体定義（`Module`・拡張子込みファイル名）とする（children なし）。

`accepts()` は先頭64KB のアプリ画面の目印（`<form`・`<script src`・`<link rel="stylesheet">`・JSP/JSF/Thymeleaf のタグ/属性・EL式・`.action` への `<a href>`）があれば受理し、無ければ decline する。decline したファイルは `ingest.text_kind` の通常の内容推定へ回る（日本語本文主体の HTML は資料側、フォーム付き画面はコード側）。
参照抽出は `jsp.py` と同じ形（HTML コメントを空白化し、開始タグだけを字句解析する）:
- `<script src>`／`<link href>` → `INVOKES(via=include)`。外部参照スキームは `Dropped("web_external_ref")`、`<base href>` があるファイルの相対 include は `Dropped("web_relative_under_base")`。
- `action=`/`href=` → `ACCESSES(via=config_key)`（`.action`／裸名＝Struts action キー、`/` 始まり拡張子なし＝URL キー）。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import bisect
import posixpath
import re
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


# HTML コメントの開始マーカー（線形スキャナで空白化する）。
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


def _newline_offsets(text: str) -> list:
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


# 開始タグの字句解析（`jsp.py` と同じ規則）

_TAG_RE = re.compile(
    r'<(?P<slash>/?)(?P<name>[A-Za-z][A-Za-z0-9:._-]*)(?P<attrs>(?:"[^"]*"|\'[^\']*\'|[^>"\'])*)>'
)
_ATTR_RE = re.compile(
    r'([A-Za-z_:][A-Za-z0-9_:.-]*)'
    r'(?:\s*=\s*(?:"([^"]*)"|\'([^\']*)\'|([^\s"\'=<>`]+)))?'
)
_SCRIPT_STYLE_OPEN_RE = re.compile(
    r'<(script|style)\b(?:"[^"]*"|\'[^\']*\'|[^>"\'])*>', re.I
)
_SCRIPT_CLOSE_RE = re.compile(r'</script\s*>', re.I)
_STYLE_CLOSE_RE = re.compile(r'</style\s*>', re.I)


def _parse_attrs(attrs_str: str) -> dict:
    out: dict = {}
    for m in _ATTR_RE.finditer(attrs_str):
        value = m.group(2)
        if value is None:
            value = m.group(3)
        if value is None:
            value = m.group(4)
        if value is not None:
            out[m.group(1).lower()] = value
    return out


def _script_style_body_spans(text: str) -> list:
    """`jsp._script_style_body_spans` と同じ規則。終了タグ検索は `text[body_start:]` を切り出さず、`search(text, pos)` に位置を渡す（二次時間を避ける）。"""
    spans: list = []
    pos, n = 0, len(text)
    while pos < n:
        m = _SCRIPT_STYLE_OPEN_RE.search(text, pos)
        if not m:
            break
        tag = m.group(1).lower()
        body_start = m.end()
        close_re = _SCRIPT_CLOSE_RE if tag == "script" else _STYLE_CLOSE_RE
        close_m = close_re.search(text, body_start)
        body_end = close_m.start() if close_m else n
        spans.append((body_start, body_end))
        pos = body_end
    return spans


# content-aware accepts()

_HEAD_SNIFF_BYTES = 64 * 1024

_ACCEPT_MARKER_PATTERNS = (
    re.compile(r'<form\b', re.I),
    re.compile(r'<script\b[^>]*\bsrc\s*=', re.I),
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
        sanitized = _sanitize_comments(text)
        newline_offsets = _newline_offsets(text)
        refs: list = []
        dropped: list = []

        exclude_spans = _script_style_body_spans(sanitized)
        # `jsp.py` と同じ単調ポインタ突合（`exclude_spans` を毎回全件再スキャンしない）。
        tag_matches = []
        excl_idx, n_excl = 0, len(exclude_spans)
        for m in _TAG_RE.finditer(sanitized):
            pos = m.start()
            while excl_idx < n_excl and exclude_spans[excl_idx][1] <= pos:
                excl_idx += 1
            in_excluded = excl_idx < n_excl and exclude_spans[excl_idx][0] <= pos
            if m.group("slash") or in_excluded:
                continue
            tag_matches.append((pos, m.group("name").lower(), _parse_attrs(m.group("attrs"))))

        has_base = any(tag == "base" and "href" in attrs for _pos, tag, attrs in tag_matches)
        if has_base:
            base_pos = next(pos for pos, tag, attrs in tag_matches
                           if tag == "base" and "href" in attrs)
            dropped.append(Dropped("web_base_href", _line_at(newline_offsets, base_pos), ""))

        for pos, tag, attrs in tag_matches:
            if tag == "base":
                continue
            line = _line_at(newline_offsets, pos)
            if tag == "script" and attrs.get("src"):
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
