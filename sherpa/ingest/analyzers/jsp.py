"""JSP アナライザ。`.jsp`/`.jspx`/`.jspf`/`.tag`/`.tagx` を全件受理し、ファイル自体を主体定義（`Module`・拡張子込みファイル名）とする（children なし）。

読み取りは 2 段。① Tree-sitter（tree-sitter-embedded-template）でテンプレート本文と `<% … %>` の埋め込みに分ける。JSP コメント `<%-- … --%>`（`--%>` までがコメント）と埋め込みは空白に置き換える（行番号は変えない）。② 残ったテンプレート本文は標準の `html.parser` で開始タグだけを読む（属性順不同・引用符省略・大文字タグ名可・`<script>`/`<style>` の本文と HTML コメントの中は読まない）。埋め込みの Java（`<% %>`・`<%= %>`・`<%! %>`）は tree-sitter-java で構文だけ確かめる（行は JSP の行に揃える）。`<%@ … %>` の指令は属性だけ読む。
参照抽出:
- `<%@ include file>`／`<jsp:include page>`／`<c:import url>`／`<jsp:directive.include file>`／`<script src>`／`<link href>` → `INVOKES(via=include)`（C アナライザと同じ2段解決）。外部参照スキームは `Dropped("web_external_ref")`。`/` 始まりは top scope へ連結して参照元からの相対パスにする。`<base href>` があるファイルの相対 include は `Dropped("web_relative_under_base")`、`<base href>` 自体も `Dropped("web_base_href")` を1件申告する。`?`/`#` 以降は除去する。
- `action=`/`href=` 属性値 → `ACCESSES(via=config_key)`。裸名・`.action` 拡張子＝Struts action キー、拡張子なしの `/` 始まり＝URL キー。判定は属性値の形だけで行う粗い判定。
- `<jsp:useBean class="FQCN">` → `INVOKES(via=bean_class, qualified=True)`。`id` 属性・EL 式は対象外。
- `<%@ page import>`・`<%@ taglib tagdir>` はエッジ化しない。
- スクリプトレット内の Java から参照は取らず、ファイルにつき `Dropped("jsp_scriptlet")` を1件申告する。構文エラーの領域（JSP の埋め込みの分割・埋め込みの Java）は `Dropped("syntax_error")` で申告し、エラーの外は読み続ける。
HTML コメント `<!-- -->` の中の `<%@ include %>` も JSP の仕様どおり処理されるので拾う。
大文字小文字は区別しない（タグ名・属性名は小文字化して判定する）。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import posixpath
import re
from html.parser import HTMLParser
from pathlib import PurePosixPath

from . import _ts
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


class _TagCollector(HTMLParser):
    """開始タグ（自己終了タグ含む）を `(行, タグ名, 属性の辞書)` の列で集める。タグ名・属性名は小文字・値なしの属性は含めない。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=False)
        self.tags: list = []

    def handle_starttag(self, tag, attrs):
        self.tags.append((self.getpos()[0], tag.lower(),
                          {k.lower(): v.strip() for k, v in attrs if v is not None}))


def _collect_tags(text: str) -> list:
    parser = _TagCollector()
    parser.feed(text)
    parser.close()
    return parser.tags


# 埋め込み・コメントの区間を、改行だけ残して空白にする変換表。
_MASK_TABLE = bytes(10 if i == 10 else 32 for i in range(256))

_DIRECTIVE_NODES = ("directive", "output_directive", "comment_directive", "ERROR")


def _split_template(parsed: _ts.Parsed) -> tuple:
    """テンプレートを `(埋め込みを空白にしたテキスト, 指令, コードの塊)` に分ける。

    指令＝`<%@ … %>` の `(行, 名前, 属性の辞書)`。コードの塊＝埋め込みの Java の `(種別, 行, コード)`（種別は宣言 `decl`・スクリプトレット `scriptlet`・式 `expr`）。
    JSP コメント `<%-- … --%>` は次の `--%>` まで（途中の `%>` で終わらない）。
    """
    src = parsed.src
    buf = bytearray(src)
    directives: list = []
    code_blocks: list = []  # (種別 "decl"|"scriptlet"|"expr", 開始行, コード)
    comment_end = 0

    def mask(lo: int, hi: int) -> None:
        buf[lo:hi] = src[lo:hi].translate(_MASK_TABLE)

    for node in parsed.root.children:
        if node.end_byte <= comment_end:
            mask(node.start_byte, node.end_byte)
            continue
        if node.type == "content":
            if node.start_byte < comment_end:
                mask(node.start_byte, comment_end)
            continue
        text = parsed.text(node)
        if text.startswith("<%--"):
            close = src.find(b"--%>", node.start_byte + 4)
            comment_end = len(src) if close < 0 else close + 4
            mask(node.start_byte, comment_end)
            continue
        mask(node.start_byte, node.end_byte)
        code = next((c for c in node.children if c.type in ("code", "comment")), None)
        if node.type == "ERROR" or code is None or node.type == "comment_directive":
            continue
        body = parsed.text(code)
        row = _ts.start_line(code)
        stripped = body.lstrip()
        if stripped.startswith("@"):
            parts = stripped[1:].split(None, 1)
            if parts:
                attrs = {k.lower(): v for _l, _t, a in _collect_tags("<x " + (parts[1] if len(parts) > 1 else "") + ">")
                         for k, v in a.items()}
                directives.append((_ts.start_line(node), parts[0].lower(), attrs))
        elif node.type == "output_directive":
            code_blocks.append(("expr", row, body))
        elif stripped.startswith("!"):
            code_blocks.append(("decl", row, body.replace("!", " ", 1)))
        else:
            code_blocks.append(("scriptlet", row, body))
    return buf.decode("utf-8", errors="replace"), directives, code_blocks


def _java_syntax_errors(code_blocks: list, sink: _ts.SyntaxErrorSink) -> None:
    """埋め込みの Java を、宣言（クラスの本体）とスクリプトレット・式（メソッドの本体）に分けて 1 つの Java にして構文だけ確かめる。コードは JSP と同じ行に置く。"""
    for wrapper_open, kinds in (("class _J { ", ("decl",)), ("class _J { void _m() { ", ("scriptlet", "expr"))):
        blocks = [b for b in code_blocks if b[0] in kinds]
        if not blocks:
            continue
        last_row = max(row + body.count("\n") for _k, row, body in blocks)
        rows: list = [""] * (last_row + 1)
        for kind, row, body in blocks:
            pieces = body.replace("\r", "").split("\n")
            if kind == "expr":
                pieces[0] = "out.print(" + pieces[0]
                pieces[-1] += ");"
            for k, piece in enumerate(pieces):
                rows[row - 1 + k] += " " + piece
        rows[0] = wrapper_open + rows[0]
        rows[-1] += " }" if kinds == ("decl",) else " } }"
        _ts.collect_syntax_errors(_ts.parse("java", "\n".join(rows)), sink)


class JspAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary）。include/script/link → `INVOKES(via=include)`。action/href → `ACCESSES(via=config_key)`。`useBean class` → `INVOKES(via=bean_class, qualified)`。"""

    name = "jsp"
    extensions = JSP_EXT
    doctype = "jsp"
    version = 3

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        parsed = _ts.parse("embedded_template", text)
        masked, directives, code_blocks = _split_template(parsed)
        refs: list = []
        dropped: list = []

        tags = _collect_tags(masked)
        base_line = next((line for line, tag, attrs in tags if tag == "base" and "href" in attrs), None)
        has_base = base_line is not None
        if has_base:
            dropped.append(Dropped("web_base_href", base_line, ""))

        for line, name, attrs in directives:
            if name == "include" and attrs.get("file"):
                _emit_include(attrs["file"], line, rel_path, refs, dropped, has_base)

        for line, tag, attrs in tags:
            if tag == "base":
                continue
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

        first_scriptlet = next((b for b in code_blocks if b[0] in ("scriptlet", "decl")), None)
        if first_scriptlet:
            line = first_scriptlet[1]
            lines = text.splitlines()
            snippet = lines[line - 1].strip()[:120] if line - 1 < len(lines) else ""
            dropped.append(Dropped("jsp_scriptlet", line, snippet))

        sink = _ts.SyntaxErrorSink()
        _ts.collect_syntax_errors(parsed, sink)
        _java_syntax_errors(code_blocks, sink)
        dropped.extend(sink.result())
        return RefResult(refs=refs, dropped=dropped)
