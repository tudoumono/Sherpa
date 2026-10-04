r"""C アナライザ。`.c`/`.h` を全件受理し、ファイル自体を主体定義（`Module`・拡張子込みのファイル名）、トップレベルの関数定義（`.c`）／プロトタイプ宣言（`.h`）を子定義（`<ファイル名>.<関数名>`・extra `c_kind`）として返す。

参照: `#include "x.h"` は `via=include`（`include_path` は `\` を `/` に正規化・始点はファイルの主体・パス区切りを含む相対パスは完全一致でしか解決せず、一致しなければ最近傍へ倒さず未解決＝`path_exact`）、関数呼び出しは `via=call`（他ファイルの関数 children を単純名で最近傍解決）。
呼び出しの始点（`source_symbol_id`）は、その行を本体に含む関数定義（`<ファイル名>.<関数名>`）。関数の外の呼び出しはファイルの主体。同じ行に複数の定義がかかる行・定義の閉じ `}` の後ろに同じ行のコードが続く行の呼び出しは決められないので主体にし、`Dropped("ambiguous_source_symbol")` で申告する。
読み取りは Tree-sitter（tree-sitter-c・`_ts`）。関数定義は複数行・K&R 形式・`extern "C" { }` の中・`#if` の枝の中も読む（K&R 形式は通常の定義として子定義・始点になる）。
申告: 構文エラーの領域は `syntax_error`（`extern "C"` の括弧を `#ifdef __cplusplus` で分ける定型句は除く）、`.h` に C++ 専用の予約語（`class`/`namespace`/`template`）は `cxx_header`、同じファイルで `#define 名(...)` した関数形式マクロの呼び出しは `c_macro_call`（参照にしない）、呼び出し先が名前でない形（`(*fp)(x)`・`p->fn(x)`・`tbl[i](x)`）は `c_dynamic_call`。`#if` の条件式・マクロ本体の中は読まない。大文字小文字は区別する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import PurePosixPath

from . import _ts
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult


C_EXT = frozenset({".c", ".h"})

# 制御構文キーワード（呼び出しから除外する）。
_CONTROL_KEYWORDS = frozenset({"if", "for", "while", "switch", "return", "sizeof"})

# `.h` に現れたら C++ 専用構文とみなす予約語。
_CXX_ONLY_WORDS = frozenset({"class", "namespace", "template"})

# 宣言の並びをそのまま包むノード（`#if` の枝・`extern "C" { }`・構文エラーの領域）。中の宣言を読み続ける。
_CONTAINERS = frozenset({
    "preproc_if", "preproc_ifdef", "preproc_else", "preproc_elif", "preproc_elifdef",
    "linkage_specification", "declaration_list", "ERROR",
})

_SNIPPET_MAX = 120


def _captured(parsed: _ts.Parsed, pattern: str, name: str) -> list:
    """クエリの捕捉を出現順（開始位置の昇順）に並べる（`_ts.captures` の列は出現順とは限らない）。"""
    return sorted(_ts.captures(parsed, pattern).get(name, []), key=lambda n: n.start_byte)


_EMPTY_DEFINE = re.compile(r'^[ \t]*#[ \t]*define[ \t]+([A-Za-z_]\w*)[ \t]*(?:/\*.*?\*/[ \t]*|//.*)?$', re.M)
_BUILTIN_TYPE_WORDS = frozenset({"int", "void", "char", "short", "long", "float", "double", "unsigned", "signed", "struct", "union", "enum"})
_LINE_HEAD = re.compile(r'^([ \t]*)([A-Za-z_]\w*)([ \t]+)(?=([A-Za-z_]\w*))', re.M)
_TYPEDEF_NAMES = re.compile(r'\btypedef\b[^;{}]*?\b([A-Za-z_]\w*)[ \t]*(?:\[[^\]\n]*\])?[ \t]*;|\}[ \t]*([A-Za-z_]\w*)[ \t]*;')
_MACRO_NAME = re.compile(r'[A-Z][A-Z0-9_]*\Z')


def _preprocessor_line_starts(text: str) -> set:
    """プリプロセッサ指令（継続行を含む）に属する行の先頭位置の集合。"""
    out: set = set()
    pos, cont = 0, False
    for line in text.split("\n"):
        if cont or line.lstrip().startswith("#"):
            out.add(pos)
            cont = line.rstrip("\r").endswith("\\")
        pos += len(line) + 1
    return out


_EXTERN_C_BRACE = re.compile(r'extern[ \t\n]*"[ ]*"[ \t\n]*\Z')
_BARE_CALL = re.compile(r'^[ \t]*([A-Z][A-Z0-9_]*)[ \t]*\(', re.M)


def _mask(text: str, pp: set) -> str:
    """コメント・文字列・文字リテラル・プリプロセッサ指令の中身を空白にした同じ長さの文字列（改行は残す）。"""
    out = list(text)
    i, n = 0, len(text)
    line_pp = False
    line_start = True
    while i < n:
        ch = text[i]
        if line_start:
            line_pp = i in pp
            line_start = False
        if ch == "\n":
            line_start = True
            i += 1
            continue
        if line_pp:
            out[i] = " "
            i += 1
            continue
        two = text[i:i + 2]
        if two == "/*" or two == "//":
            end = text.find("*/", i + 2) + 2 if two == "/*" else text.find("\n", i)
            if end < (2 if two == "/*" else 0):
                end = n
            for k in range(i, end):
                if out[k] != "\n":
                    out[k] = " "
            i = end
            continue
        if ch in "\"'":
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            for k in range(i + 1, min(j, n)):
                if out[k] != "\n":
                    out[k] = " "
            i = j + 1
            continue
        i += 1
    return "".join(out)


def _blank_bare_macro_calls(text: str, pp: set) -> str:
    """(c) ファイルの最上位（関数の本体の外）で、大文字・数字・`_` だけの名前 ＋ 閉じる括弧 だけで終わる行（セミコロン無しのマクロの呼び出し
    `DECLARE_X(Foo)`）を空白にする。後ろに `{`・`=` が続くものは触らない。"""
    masked = _mask(text, pp)
    out = list(text)
    depth, stack, line_depth = 0, [], {}
    pos = 0
    for line in masked.split("\n"):
        line_depth[pos] = depth
        for k, ch in enumerate(line):
            if ch == "{":
                skip = bool(_EXTERN_C_BRACE.search(masked[max(0, pos + k - 40):pos + k]))
                stack.append(skip)
                depth += 0 if skip else 1
            elif ch == "}" and stack:
                depth -= 0 if stack.pop() else 1
        pos += len(line) + 1
    for m in _BARE_CALL.finditer(masked):
        start = m.start()
        if line_depth.get(start, 1) != 0 or start in pp:
            continue
        i, d = m.end(), 1
        while i < len(masked) and d:
            d += (masked[i] == "(") - (masked[i] == ")")
            i += 1
        if d:
            continue
        eol = masked.find("\n", i)
        rest = masked[i:eol if eol != -1 else len(masked)]
        if rest.strip():
            continue
        nxt = masked[i:].lstrip()
        if nxt[:1] in ("{", "=", ";", ","):
            continue
        for k in range(m.start(1), i):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _blank_macro_noise(text: str) -> str:
    """文法が読めない 2 つの定型を、行と位置を変えずに空白へ置き換える（申告は出さない・エラーではない）。対象は宣言の先頭の 1 語だけ。
    (a) その行より前に、同じファイルで中身の無いマクロとして `#define` された名前（`#define API`）。
    (c) 最上位のセミコロン無しのマクロの呼び出し行（`DECLARE_X(Foo)`）全体。
    (b) 組み込みの型の語か同じファイルの typedef 名の直前に置かれた大文字・数字・`_` だけの名前（`API int f(void);`）。
    プリプロセッサ指令（継続行を含む）の中は触らない。"""
    text = _blank_bare_macro_calls(text, _preprocessor_line_starts(text))
    empties = {}
    for m in _EMPTY_DEFINE.finditer(text):
        empties.setdefault(m.group(1), m.end())
    pp = _preprocessor_line_starts(text)
    typedefs = None

    def head(m):
        nonlocal typedefs
        if m.start() in pp:
            return m.group(0)
        word, nxt = m.group(2), m.group(4)
        if word in empties and m.start(2) > empties[word]:
            pass
        elif _MACRO_NAME.match(word) and word not in _BUILTIN_TYPE_WORDS:
            if nxt not in _BUILTIN_TYPE_WORDS:
                if typedefs is None:
                    typedefs = {g for t in _TYPEDEF_NAMES.finditer(text) for g in t.groups() if g}
                if nxt not in typedefs:
                    return m.group(0)
        else:
            return m.group(0)
        return m.group(1) + " " * len(word) + m.group(3)

    return _LINE_HEAD.sub(head, text)


def _parse(text: str) -> _ts.Parsed:
    return _ts.parse("c", _blank_macro_noise(text))


def _members(node):
    for ch in node.children:
        if ch.type in _CONTAINERS:
            yield from _members(ch)
        else:
            yield ch


def _func_name(declarator):
    """関数宣言子（`*` の付いた戻り値も可）から関数名のノードを返す。`(*fp)(…)` など名前が直接でないものは `None`。"""
    n = declarator
    while n is not None and n.type == "pointer_declarator":
        n = n.child_by_field_name("declarator")
    if n is None or n.type != "function_declarator":
        return None
    inner = n.child_by_field_name("declarator")
    return inner if inner is not None and inner.type == "identifier" else None


@dataclass
class _Func:
    name: str
    line: int          # 関数名の行
    start: int         # 定義の開始行（宣言のみは `line`）
    end: int           # 定義の終了行
    definition: bool
    clean: bool        # 定義の閉じ `}` の後ろに同じ行のコードが続かない
    qualified: bool = False   # `Widget::run` のような C++ の修飾つきの名前


def _trailing_code(parsed: _ts.Parsed, node) -> bool:
    row = node.end_point[0]
    sib = node.next_sibling
    while sib is not None and sib.start_point[0] == row:
        if sib.type != "comment" and parsed.text(sib).strip() != ";":
            return True
        sib = sib.next_sibling
    return False


def _is_qualified(parsed: _ts.Parsed, name) -> bool:
    return parsed.src[:name.start_byte].rstrip().endswith(b"::")


def _scan_funcs(parsed: _ts.Parsed) -> list:
    """トップレベル（`#if` の枝・`extern "C"` の中を含む）の関数定義とプロトタイプ宣言を出現順に返す。"""
    out: list = []
    for ch in _members(parsed.root):
        if ch.type == "function_definition":
            name = _func_name(ch.child_by_field_name("declarator"))
            if name is not None:
                out.append(_Func(parsed.text(name), _ts.start_line(name), _ts.start_line(ch), _ts.end_line(ch), True,
                                 not _trailing_code(parsed, ch), _is_qualified(parsed, name)))
        elif ch.type == "declaration":
            for decl in ch.children_by_field_name("declarator"):
                name = _func_name(decl)
                if name is not None:
                    line = _ts.start_line(name)
                    out.append(_Func(parsed.text(name), line, line, line, False, True, _is_qualified(parsed, name)))
    return out


def _cxx_header_line(parsed: _ts.Parsed) -> int | None:
    hits = [n for n in _ts.captures(parsed, "[(identifier) (type_identifier)] @t").get("t", [])
            if parsed.text(n) in _CXX_ONLY_WORDS]
    return _ts.start_line(min(hits, key=lambda n: n.start_byte)) if hits else None


_EXTERN_C_OPEN = re.compile(r'^[ \t]*#[ \t]*if[^\n]*__cplusplus[^\n]*\n[ \t]*extern[ \t]+"C"[ \t]*\{', re.M)
_EXTERN_C_CLOSE = re.compile(r'^[ \t]*#[ \t]*if[^\n]*__cplusplus[^\n]*\n[ \t]*\}', re.M)


def _syntax_errors(parsed: _ts.Parsed, text: str) -> list:
    """構文エラーの申告。`#ifdef __cplusplus extern "C" { #endif … #ifdef __cplusplus } #endif` の括弧の対応が `#if` の枝に分かれる形は
    文法が `#endif` の欠落を 1 つ報告するが、中身は正しく読めているので申告しない。この定型が開閉とも見つかったときだけ最初の 1 件を除き、他の未閉鎖の `#if` は申告する。"""
    errors = _ts.syntax_errors(parsed)
    if _EXTERN_C_OPEN.search(text) and _EXTERN_C_CLOSE.search(text):
        for i, d in enumerate(errors):
            if d.snippet == "missing #endif":
                return errors[:i] + errors[i + 1:]
    return errors


def _line_text(text: str, line: int) -> str:
    lines = text.split("\n")
    return lines[line - 1].strip()[:_SNIPPET_MAX] if 0 < line <= len(lines) else ""


class CAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary・拡張子込みファイル名）。トップレベル関数定義/プロトタイプ
    → `Module`（children・`<ファイル名>.<関数名>` 修飾）。`#include`/呼び出し → `INVOKES` 候補。"""

    name = "c"
    extensions = C_EXT
    resolves_calls_by_simple_name = True
    doctype = "c"
    version = 4

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        is_header = PurePosixPath(rel_path).suffix.lower() == ".h"
        parsed = _parse(text)
        # `.c` は定義だけを children 化する（`;` 終端の外部宣言を偽 child にしない）。`.h` は宣言（`;`）も children 化する。
        # `c_kind` を children の索引へ持たせる（単純名の呼び出し解決が定義を宣言より優先するため）。
        children = [
            DefItem(label="Module", name=f.name, cid_key=f"{filename}.{f.name}", line=f.line,
                    extra={"c_kind": "definition" if f.definition else "declaration"})
            for f in _scan_funcs(parsed) if not f.qualified and (is_header or f.definition)
        ]
        dropped: list = []
        if is_header:
            lines = [f.line for f in _scan_funcs(parsed) if f.qualified]
            keyword = _cxx_header_line(parsed)
            line = min(lines + ([keyword] if keyword is not None else []), default=None)
            if line is not None:
                dropped.append(Dropped("cxx_header", line, _line_text(text, line)))
        dropped += _syntax_errors(parsed, text)
        return DefResult(primary=DefItem(label="Module", name=filename), children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        parsed = _parse(text)
        refs: list = []
        dropped: list = []

        for p in _captured(parsed, "(preproc_include path: (string_literal) @p)", "p"):
            local = parsed.text(p)[1:-1].replace("\\", "/")  # Windows 区切り正規化
            basename = PurePosixPath(local).name
            if basename:
                refs.append(RefCandidate("INVOKES", "Module", basename, _ts.start_line(p),
                                         extra={"via": "include", "include_path": local, "path_exact": True}))

        filename = PurePosixPath(rel_path).name
        spans = [(f.name, f.start, f.end, f.clean) for f in _scan_funcs(parsed) if f.definition and not f.qualified]
        ambiguous_lines: set = set()

        def owner_of(line: int):
            """行を本体に含む関数定義の `source_symbol_id`（関数の外＝ファイル直下は `None`）。

            同じ行に 2 つ以上の定義がかかる行・定義の閉じ `}` の後ろに同じ行のコードが続く行は決められないので主体（`None`）にし、`Dropped("ambiguous_source_symbol")` を 1 行 1 件残す。
            """
            hits = [name for name, start, end, _clean in spans if start <= line <= end]
            shared = len(hits) > 1 or any(end == line and not clean for _n, _s, end, clean in spans)
            if shared:
                if line not in ambiguous_lines:
                    ambiguous_lines.add(line)
                    dropped.append(Dropped("ambiguous_source_symbol", line, ", ".join(f"{filename}.{n}" for n in hits)))
                return None
            return (rel_path, f"{filename}.{hits[0]}") if hits else None

        macro_names = {parsed.text(n) for n in
                       _ts.captures(parsed, "(preproc_function_def name: (identifier) @m)").get("m", [])}
        conditions = [(n.start_byte, n.end_byte) for n in _ts.captures(
            parsed, "[(preproc_if condition: (_) @c) (preproc_elif condition: (_) @c)]").get("c", [])]

        for call in _captured(parsed, "(call_expression) @call", "call"):
            if any(lo <= call.start_byte and call.end_byte <= hi for lo, hi in conditions):
                continue                                  # `#if FOO(1)` の条件式は呼び出しではない
            callee = call.child_by_field_name("function")
            if callee is None:
                continue
            line = _ts.start_line(callee)
            if callee.type != "identifier":
                dropped.append(Dropped("c_dynamic_call", line, _line_text(text, line)))
                continue
            name = parsed.text(callee)
            if name in _CONTROL_KEYWORDS:
                continue
            if name in macro_names:
                dropped.append(Dropped("c_macro_call", line, name))
                continue
            refs.append(RefCandidate("INVOKES", "Module", name, line, extra={"via": "call"},
                                     source_symbol_id=owner_of(line)))

        return RefResult(refs=refs, dropped=dropped)
