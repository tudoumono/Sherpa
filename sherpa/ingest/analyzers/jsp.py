"""JSP アナライザ。`.jsp`/`.jspx`/`.jspf`/`.tag`/`.tagx` を全件受理し、ファイル自体を主体定義（`Module`・拡張子込みファイル名）とする（children なし）。

コメント（`<%-- --%>`／`<!-- -->`）は線形スキャナで空白化し、タグ走査は開始タグだけの字句解析（属性順不同・引用符省略・大文字タグ名可）で行う。`<script>`/`<style>` の本文は走査対象外。
参照抽出:
- `<%@ include file>`／`<jsp:include page>`／`<c:import url>`／`<jsp:directive.include file>`／`<script src>`／`<link href>` → `INVOKES(via=include)`（C アナライザと同じ2段解決）。外部参照スキームは `Dropped("web_external_ref")`。`/` 始まりは top scope へ連結して参照元からの相対パスにする。`<base href>` があるファイルの相対 include は `Dropped("web_relative_under_base")`、`<base href>` 自体も `Dropped("web_base_href")` を1件申告する。`?`/`#` 以降は除去する。
- `action=`/`href=` 属性値 → `ACCESSES(via=config_key)`。裸名・`.action` 拡張子＝Struts action キー、拡張子なしの `/` 始まり＝URL キー。判定は属性値の形だけで行う粗い判定。
- `<jsp:useBean class="FQCN">` → `INVOKES(via=bean_class, qualified=True)`。`id` 属性・EL 式は対象外。
- `<%@ page import>`・`<%@ taglib tagdir>` はエッジ化しない。
- スクリプトレット内の Java は解析せず、ファイルにつき `Dropped("jsp_scriptlet")` を1件申告する。
大文字小文字は区別しない（タグ名・属性名は小文字化して判定する）。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import bisect
import posixpath
import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

JSP_EXT = frozenset({".jsp", ".jspx", ".jspf", ".tag", ".tagx"})

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
    """`/` 始まりの include を参照元の top scope へ連結し、参照元ファイルからの相対パスに変換する（共通層 `_resolve_include_relpath` は参照元相対しか解決しないため）。"""
    stripped = raw_path.lstrip("/")
    if "/" not in ref_rel:
        return stripped
    top_scope = ref_rel.split("/", 1)[0]
    target = f"{top_scope}/{stripped}"
    base_dir = ref_rel.rsplit("/", 1)[0]
    return posixpath.relpath(target, start=base_dir)


# JSP コメント／HTML コメントの開始マーカー（線形スキャナで空白化する）。
_COMMENT_MARKER_RE = re.compile(r'<%--|<!--')


def _sanitize_comments(text: str) -> str:
    """`<%-- ... --%>`／`<!-- ... -->` を線形1パスで空白化する（改行は保持。閉じていないコメントは末尾まで）。"""
    out: list = []
    i, n = 0, len(text)
    while i < n:
        m = _COMMENT_MARKER_RE.search(text, i)
        if not m:
            out.append(text[i:])
            break
        out.append(text[i:m.start()])
        if m.group(0) == "<%--":
            end = text.find("--%>", m.end())
            close_len = 4
        else:
            end = text.find("-->", m.end())
            close_len = 3
        j = end + close_len if end != -1 else n
        out.append("".join("\n" if c == "\n" else " " for c in text[m.start():j]))
        i = j
    return "".join(out)


_INCLUDE_DIRECTIVE = re.compile(r'<%@\s*include\s+file\s*=\s*["\'](?P<path>[^"\']+)["\']')

# スクリプトレット（`<%@`/`<%=`/`<%--` を除く `<% ... %>`）。
_SCRIPTLET = re.compile(r'<%(?!@|=|--)(?P<body>.*?)%>', re.S)


def _newline_offsets(text: str) -> list:
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


_BARE_ACTION_NAME = re.compile(r'^[A-Za-z0-9_-]+$')


def _config_key_from_action_or_href(val: str) -> tuple[str, str] | None:
    """`action`/`href` 属性値から `(名前, 種別)` を返す（種別は `extra["key_kind"]`）。

    `?`/`#` 以降は除去し、外部参照スキームは対象外。`.action` 拡張子＝Struts action キー（拡張子と先頭 `/` を落とす）、`/` 始まりで最終セグメントに `.` なし＝URL キー、`/`/`.` を含まない裸名＝Struts action キー。どれにも該当しなければ `None`。
    """
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
        if "." not in last_seg:
            return val, "url"
        return None
    if _BARE_ACTION_NAME.match(val):
        return val, "action"
    return None


def _emit_include(raw_path: str, line: int, ref_rel: str, refs: list, dropped: list,
                  has_base: bool) -> None:
    """include 系参照候補1件の処理（外部参照除外・`/` 始まりの scope 相対化・`<base>` 配下の相対 include の抑止・basename 抽出）。"""
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


# 開始タグの字句解析（属性順不同・引用符省略・大文字タグ名を許容）

# 開始/終了タグ本体（属性文字列は「引用符区間 or `>`/引用符以外の1文字」の繰り返しとして消費する）。
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
    """`<script>`/`<style>` の本文区間（開始タグ直後〜終了タグ直前）。属性走査の対象から外す。終了タグ検索は `text[body_start:]` を切り出さず、`search(text, pos)` に位置を渡す（二次時間を避ける）。"""
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


class JspAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary）。include/script/link → `INVOKES(via=include)`。action/href → `ACCESSES(via=config_key)`。`useBean class` → `INVOKES(via=bean_class, qualified)`。"""

    name = "jsp"
    extensions = JSP_EXT
    doctype = "jsp"
    version = 2

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize_comments(text)
        newline_offsets = _newline_offsets(text)
        refs: list = []
        dropped: list = []

        exclude_spans = _script_style_body_spans(sanitized)
        # `_TAG_RE.finditer` と `exclude_spans` はともに位置昇順なので、単調ポインタで1回だけ突合する。
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

        for m in _INCLUDE_DIRECTIVE.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            _emit_include(m.group("path"), line, rel_path, refs, dropped, has_base)

        for pos, tag, attrs in tag_matches:
            if tag == "base":
                continue
            line = _line_at(newline_offsets, pos)
            if tag == "script" and attrs.get("src"):
                _emit_include(attrs["src"], line, rel_path, refs, dropped, has_base)
            elif tag == "link" and attrs.get("href"):
                _emit_include(attrs["href"], line, rel_path, refs, dropped, has_base)
            elif tag == "jsp:include" and attrs.get("page"):
                _emit_include(attrs["page"], line, rel_path, refs, dropped, has_base)
            elif tag == "c:import" and attrs.get("url"):
                _emit_include(attrs["url"], line, rel_path, refs, dropped, has_base)
            elif tag == "jsp:directive.include" and attrs.get("file"):
                _emit_include(attrs["file"], line, rel_path, refs, dropped, has_base)
            elif tag == "jsp:usebean" and attrs.get("class"):
                refs.append(RefCandidate("INVOKES", "Module", attrs["class"], line,
                                         extra={"via": "bean_class", "qualified": True}))

            for key_attr in ("action", "href"):
                if key_attr in attrs:
                    resolved = _config_key_from_action_or_href(attrs[key_attr])
                    if resolved is not None:
                        key, kind = resolved
                        refs.append(RefCandidate("ACCESSES", "Config", key, line,
                                                 extra={"via": "config_key", "key_kind": kind}))

        first_scriptlet = _SCRIPTLET.search(sanitized)
        if first_scriptlet:
            line = _line_at(newline_offsets, first_scriptlet.start())
            lines = text.splitlines()
            snippet = lines[line - 1].strip()[:120] if line - 1 < len(lines) else ""
            dropped.append(Dropped("jsp_scriptlet", line, snippet))

        return RefResult(refs=refs, dropped=dropped)
