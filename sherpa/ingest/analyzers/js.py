"""JS アナライザ。`.js`/`.mjs` を全件受理し（`.ts` は対象外）、ファイル自体を主体定義（`Module`・拡張子込みファイル名）とする（children なし）。

読み取りは Tree-sitter（tree-sitter-javascript）の木の上で行う。コメントは木の上のコメントノードなので読まない。文字列/テンプレートリテラルは、API の引数の位置にあるものだけ参照として読む。
参照抽出（API の引数の位置）:
- 読む文字列＝`import("…")`・`import … from "…"`・`export … from "…"`・`require("…")`・`importScripts("…")`・`fetch("…")`・`axios.<method>("…")`・`.open("<METHOD>", "…")`・`url: "…"`・`location.href = "…"`・`.action = "…"` の引数。テンプレートの `${ … }` の中は式として木の上で読まれる。API の位置にないリテラルの中に書かれたコードの断片（`"import x from './b.js'; fetch('/api/z')"`）は読まず、`Dropped("js_string_code")` を 1 件申告する（リテラルの中身を JS として解析して参照が取れるときだけ。2,000 字超のリテラルと 200 回目以降の解析は行わず、ファイルにつき `Dropped("js_string_code_limit")` を 1 件〔調べなかった件数〕申告する）。
- `import ... from "./y.js"`／`require("./y")`／`importScripts("…")` → `INVOKES(via=include)`（拡張子なしは `.js` を補う・C アナライザと同じ2段解決）。`import`/`require` は `.` 始まりのパスに限る（裸のパッケージ名は除く）。外部参照スキームは `Dropped("web_external_ref")`、`/` 始まりは top scope へ連結して相対パス化、`?`/`#` 以降は除去する。
- 文字列リテラルの URL（`fetch`・`$.ajax({url})`・`axios.*`・`XMLHttpRequest.open`・`location.href =`・`.action =`）→ `ACCESSES(via=config_key)`（`/` 始まりは URL キー、`.action` は Struts action キー）。呼び出し元の型は検証しない粗い判定。
- `import(p)`・`require(p)` のように取り込み先が文字列でない呼び出しは `Dropped("js_dynamic_import")`。
- 動的連結（`"/orders/" + id`・API の引数の `${ … }` つきテンプレート）は `Dropped("js_dynamic_url")`（その呼び出しは参照にしない）。
- minified（`.min.js`、または1行4096文字超）は `Dropped("js_minified")` を1件申告し、抽出しない。
- 構文エラーの領域は `Dropped("syntax_error")` で申告し、エラーの外は読み続ける。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import bisect
import posixpath
from pathlib import PurePosixPath

from . import _ts
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult


JS_EXT = frozenset({".js", ".mjs"})

_MINIFIED_LINE_LEN = 4096

# 外部参照スキーム（include 対象外）。
_EXTERNAL_SCHEMES = ("http:", "https:", "//", "data:", "javascript:", "mailto:", "#")

# 文字列の中にコードの断片があるかを調べる前に、解析を省くための語（部分文字列）。
# 中身の再解析の上限（大きなバンドルで遅くならないため）。超えた分は解析せず、ファイルにつき 1 件 `js_string_code_limit`（調べなかった件数）で申告する（参照は API の位置から取るので取りこぼしにならない）。
_INNER_MAX_CHARS = 2000
_INNER_MAX_PARSES = 200

_CODE_HINTS = ("import", "export", "require", "fetch", "axios", "open", "url", "href", "action")


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


# --- 木の上の読み取り ---------------------------------------------------------------------

_STRING_TYPES = ("string", "template_string")


def _arg_nodes(call) -> list:
    args = call.child_by_field_name("arguments")
    if args is None:
        return []
    return [c for c in args.named_children if c.type != "comment"]


def _callee_name(fn):
    """呼び出しの関数部の末尾の名前ノード（`f`／`a.b.f` の `f`）。計算プロパティや式なら `None`。"""
    if fn is None:
        return None
    if fn.type == "identifier":
        return fn
    if fn.type == "member_expression":
        prop = fn.child_by_field_name("property")
        return prop if prop is not None and prop.type == "property_identifier" else None
    return None


def _is_relative_path(v: str) -> bool:
    """`./x`・`../x` の形（`./` だけ・`../` だけは取り込み先のファイルを指さないので除く）。"""
    return v.startswith(("./", "../")) and bool(v.split("/", 1)[1])


def _static_value(node):
    """文字列／`${}` なしのテンプレートの中身（引用符の内側・生のまま）。それ以外は `None`。"""
    if node.type == "string" or (node.type == "template_string"
                                 and not any(c.type == "template_substitution" for c in node.children)):
        if node.child_count >= 2 and not any(c.is_missing for c in node.children):
            return node.text[1:-1].decode("utf-8", errors="replace")
    return None


def _followed_by_plus(node) -> bool:
    """ノードの直後の字句（コメントを除く）が `+` か。"""
    n = node
    while n is not None:
        sib = n.next_sibling
        while sib is not None and sib.type == "comment":
            sib = sib.next_sibling
        if sib is not None:
            return sib.type == "+"
        n = n.parent
    return False


class _Hits:
    """木から集めた読み取りの生の結果。`raw` はリテラルの中身がコードの形かの判定にも使う。"""

    def __init__(self) -> None:
        # 参照の取り出し順は、種類ごとのまとまり（import／export／require／importScripts／URL）の中で文書順。
        self.import_from: list = []
        self.export_from: list = []
        self.require: list = []
        self.import_scripts: list = []
        self.urls: list = []  # (値, 行, 「/ 始まりのみ」か)
        self.dynamic_templates: list = []  # 行
        self.string_code: list = []  # 行
        self.dynamic_imports: list = []  # 行
        self.newlines: list | None = None  # 改行のバイト位置（行を引くとき 1 回だけ作る）
        self.inner_parses = 0
        self.inner_skipped = 0  # 上限で中身を調べなかったリテラルの数
        self.inner_skipped_line = 0

    def any_reference(self) -> bool:
        return bool(self.import_from or self.export_from or self.require or self.import_scripts or self.urls)


def _scan(parsed: _ts.Parsed, nested: bool, line_of) -> _Hits:
    """`parsed` の木を文書順に歩いて API の引数の位置の文字列を集める。`nested` はリテラルの中身を調べる再帰（コード断片の判定だけに使う）。"""
    hits = _Hits()
    api: set = set()  # API の引数の位置にある文字列／テンプレートのノード id
    stack = [parsed.root]
    while stack:
        node = stack.pop()
        t = node.type
        if t in ("import_statement", "export_statement"):
            src = node.child_by_field_name("source")
            if src is not None:
                api.add(src.id)
                v = _static_value(src)
                if v is not None and _is_relative_path(v):
                    (hits.import_from if t == "import_statement" else hits.export_from).append(
                        (v, line_of(node)))
        elif t == "call_expression":
            _scan_call(node, hits, api, line_of)
        elif t == "pair":
            key = node.child_by_field_name("key")
            if key is not None and key.type == "property_identifier" and parsed.text(key) == "url":
                _api_url(node.child_by_field_name("value"), line_of(key), True, hits, api)
        elif t == "assignment_expression":
            _scan_assignment(parsed, node, hits, api, line_of)
        elif t in _STRING_TYPES and not nested and node.id not in api:
            _scan_literal(parsed, node, hits, line_of)
        stack.extend(reversed(node.children))
    return hits


def _api_url(arg, line: int, need_slash: bool, hits: _Hits, api: set) -> None:
    """URL を取る引数の位置のノードを読む。文字列なら URL、`${}` つきテンプレートなら動的として申告する。"""
    if arg is None or arg.type not in _STRING_TYPES:
        return
    api.add(arg.id)
    v = _static_value(arg)
    if v is not None:
        if not need_slash or v.startswith("/"):
            hits.urls.append((v, line, need_slash))
    elif arg.type == "template_string":
        hits.dynamic_templates.append(line)


def _scan_call(node, hits: _Hits, api: set, line_of) -> None:
    fn = node.child_by_field_name("function")
    args = _arg_nodes(node)
    if fn is None or not args:
        return
    if fn.type == "import":
        _api_path(args[0], line_of(fn), hits.import_from, hits, api, relative_only=True)
        return
    name = _callee_name(fn)
    if name is None:
        return
    word = name.text.decode("utf-8", errors="replace")
    if word == "require":
        _api_path(args[0], line_of(name), hits.require, hits, api, relative_only=True)
    elif word == "importScripts":
        _api_path(args[0], line_of(name), hits.import_scripts, hits, api, relative_only=False)
    elif word == "fetch":
        _api_url(args[0], line_of(name), True, hits, api)
    elif fn.type == "member_expression":
        obj = _callee_name(fn.child_by_field_name("object"))
        if obj is not None and obj.text == b"axios" and word.isalpha():
            _api_url(args[0], line_of(obj), True, hits, api)
        elif word == "open":
            if args[0].type in _STRING_TYPES:
                api.add(args[0].id)
            if len(args) >= 2:
                _api_url(args[1], line_of(name), True, hits, api)


def _api_path(arg, line: int, bucket: list, hits: _Hits, api: set, relative_only: bool) -> None:
    """import／require／importScripts の引数の位置のノードを読む（取り込み先のパス）。"""
    if arg.type not in _STRING_TYPES:
        if relative_only and arg.type != "comment":
            hits.dynamic_imports.append(line)  # `import(p)`・`require(p)`＝取り込み先が実行時に決まる
        return
    api.add(arg.id)
    v = _static_value(arg)
    if v is not None:
        if not relative_only or _is_relative_path(v):
            bucket.append((v, line))
    elif arg.type == "template_string":
        (hits.dynamic_imports if relative_only else hits.dynamic_templates).append(line)


def _scan_assignment(parsed: _ts.Parsed, node, hits: _Hits, api: set, line_of) -> None:
    left = node.child_by_field_name("left")
    right = node.child_by_field_name("right")
    if left is None or right is None or left.type != "member_expression":
        return
    prop = left.child_by_field_name("property")
    if prop is None or prop.type != "property_identifier":
        return
    word = parsed.text(prop)
    if word == "href":
        obj = _callee_name(left.child_by_field_name("object"))
        if obj is not None and obj.text == b"location":
            _api_url(right, line_of(obj), True, hits, api)
    elif word == "action":
        _api_url(right, line_of(prop), False, hits, api)


def _scan_literal(parsed: _ts.Parsed, node, hits: _Hits, line_of) -> None:
    """API の位置にないリテラルの中身がコードの形（解析して参照が取れる）なら `string_code` に記録する。`${}` の外の区間ごとに調べる。"""
    segments: list = []
    if node.type == "string":
        if node.child_count >= 2:
            segments.append((node.start_byte + 1, node.end_byte - 1))
    else:
        seg = node.start_byte + 1
        for c in node.children:
            if c.type == "template_substitution":
                segments.append((seg, c.start_byte))
                seg = c.end_byte
        segments.append((seg, node.end_byte - 1))
    for lo, hi in segments:
        if hi <= lo:
            continue
        body = parsed.src[lo:hi].decode("utf-8", errors="replace")
        if not any(h in body for h in _CODE_HINTS):
            continue
        if len(body) > _INNER_MAX_CHARS or hits.inner_parses >= _INNER_MAX_PARSES:
            if not hits.inner_skipped:
                hits.inner_skipped_line = _line_of_byte(parsed, hits, lo)
            hits.inner_skipped += 1
            continue
        hits.inner_parses += 1
        inner = _scan(_ts.parse("javascript", body), True, lambda n: 0)
        if inner.any_reference() or inner.dynamic_templates:
            hits.string_code.append(_line_of_byte(parsed, hits, lo))
            return


def _line_of_byte(parsed: _ts.Parsed, hits: _Hits, offset: int) -> int:
    if hits.newlines is None:
        hits.newlines = [i for i, b in enumerate(parsed.src) if b == 10]
    return bisect.bisect_left(hits.newlines, offset) + 1


class JsAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary）。import/require/importScripts → `INVOKES(via=include)`。URL 文字列リテラル → `ACCESSES(via=config_key)`。動的連結/minified/構文エラー → `Dropped`。"""

    name = "js"
    extensions = JS_EXT
    doctype = "js"
    version = 3

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        return DefResult(primary=DefItem(label="Module", name=filename))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        if _is_minified(rel_path, text):
            return RefResult(dropped=[Dropped("js_minified", 1, "")])

        parsed = _ts.parse("javascript", text)
        src_lines = text.splitlines()

        def line_of(node) -> int:
            return _ts.start_line(node)

        def snippet(line: int) -> str:
            return src_lines[line - 1].strip()[:120] if 0 < line <= len(src_lines) else ""

        hits = _scan(parsed, False, line_of)
        refs: list = []
        dropped: list = []

        for line in hits.dynamic_templates:
            dropped.append(Dropped("js_dynamic_url", line, snippet(line)))
        for line in hits.dynamic_imports:
            dropped.append(Dropped("js_dynamic_import", line, snippet(line)))
        for line in hits.string_code:
            dropped.append(Dropped("js_string_code", line, snippet(line)))
        if hits.inner_skipped:
            dropped.append(Dropped("js_string_code_limit", hits.inner_skipped_line, f"{hits.inner_skipped} 件"))

        for bucket in (hits.import_from, hits.export_from, hits.require, hits.import_scripts):
            for path, line in bucket:
                _emit_include(path, line, rel_path, refs, dropped)

        # 動的連結＝直後に `+` が続く `"/…"` の文字列（同じ行は 1 件）。
        seen_lines: set = set()
        stack = [parsed.root]
        while stack:
            node = stack.pop()
            if node.type == "string":
                v = _static_value(node)
                if v is not None and v.startswith("/") and _followed_by_plus(node):
                    line = line_of(node)
                    if line not in seen_lines:
                        seen_lines.add(line)
                        dropped.append(Dropped("js_dynamic_url", line, snippet(line)))
                continue
            stack.extend(reversed(node.children))

        for val, line, _need_slash in hits.urls:
            resolved = _config_key_name(val)
            if resolved is not None:
                key, kind = resolved
                refs.append(RefCandidate("ACCESSES", "Config", key, line,
                                         extra={"via": "config_key", "key_kind": kind}))

        dropped.extend(_ts.syntax_errors(parsed))
        return RefResult(refs=refs, dropped=dropped)
