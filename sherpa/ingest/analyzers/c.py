r"""C アナライザ。`.c`/`.h` を全件受理し、ファイル自体を主体定義（`Module`・拡張子込みのファイル名）、トップレベルの関数定義（`.c`）／プロトタイプ宣言（`.h`）を子定義（`<ファイル名>.<関数名>`・extra `c_kind`）として返す。

参照: `#include "x.h"` は `via=include`（`include_path` は `\` を `/` に正規化）、関数呼び出しは `via=call`（他ファイルの関数 children を単純名で最近傍解決）。
シグネチャ判定は単一物理行・波括弧深度0に限る粗い判定（複数行は見逃す）。K&R 形式・マクロ関数呼び出し・`.h` 内の C++ 構文は `Dropped` で申告する。大文字小文字は区別する。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import bisect
import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

C_EXT = frozenset({".c", ".h"})

# 制御構文キーワード（戻り値型・関数呼び出しから除外する）。
_CONTROL_KEYWORDS = frozenset({"if", "for", "while", "switch", "return", "sizeof"})

# 関数定義／プロトタイプ宣言（単一物理行・粗い判定）: [modifiers] rtype name(args) {|;
_FUNC_SIG = re.compile(
    r'^(?:(?:static|inline|extern)\s+)*'
    r'(?P<rtype>[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*)'
    r'[\s*]+'
    r'(?P<name>[A-Za-z_]\w*)\s*'
    r'\((?P<args>[^;{}()]*)\)\s*'
    r'(?P<term>[{;])'
)

_INCLUDE = re.compile(r'^\s*#\s*include\s*(?:"(?P<local>[^"]+)"|<[^>]+>)', re.M)

_MACRO_DEFINE = re.compile(r'^\s*#\s*define\s+(?P<name>[A-Za-z_]\w*)\(', re.M)

_DYNAMIC_CALL = re.compile(r'\(\s*\*\s*[A-Za-z_]\w*\s*\)\s*\(')

# 通常の呼び出し（`identifier(`）。直後が `(*` の関数ポインタ型宣言は除く。
_CALL = re.compile(r'\b(?P<name>[A-Za-z_]\w*)\s*\((?!\s*\*)')

_KNR_HEADER = re.compile(
    r'^(?:(?:static|inline|extern)\s+)*'
    r'(?P<rtype>[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*)'
    r'[\s*]+'
    r'(?P<name>[A-Za-z_]\w*)\s*'
    r'\((?P<args>[^;{}()]*)\)\s*$'
)
_KNR_PARAM_DECL = re.compile(r'^[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*[\s*]+[A-Za-z_]\w*\s*;\s*$')

# `(*名)(` の前置部が型指定子だけの宣言形かの判定（`static`/`extern`/`const`＋識別子のみ許す）。
_POINTER_DECL_PREFIX = re.compile(
    r'^\s*(?:(?:static|extern|const)\s+)*[A-Za-z_]\w*(?:\s+[A-Za-z_]\w*)*[\s*]*$'
)

# `.h` に現れたら C++ 専用構文とみなす予約語。
_CXX_ONLY_HEADER = re.compile(r'\b(?:class|namespace|template)\b')


def _sanitize(text: str) -> str:
    """コメントと文字列/char リテラルの中身を空白化した同じ行数の文字列を返す（偽マッチ除外用・行番号は原本と1対1）。"""
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
        if ch == '"' or ch == "'":
            quote = ch
            out.append(" ")
            i += 1
            while i < n and text[i] != quote:
                if text[i] == "\\" and i + 1 < n:
                    out.append("  ")
                    i += 2
                    continue
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(" ")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _sanitize_comments_only(text: str) -> str:
    """コメントだけを空白化し、文字列リテラルは残す（`#include "path"` のパスを読むため）。"""
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
        if ch == '"' or ch == "'":
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
    """`text` 内の全改行位置（昇順）。`_line_at` が `bisect` で引く。"""
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


def _preprocessor_line_numbers(text: str) -> set:
    """`#` 始まり（プリプロセッサ指令）の物理行番号の集合。関数定義/呼び出しの検出から除く。"""
    return {i for i, line in enumerate(text.splitlines(), 1) if line.lstrip().startswith("#")}


def _scan_func_decls(sanitized: str, pp_lines: set) -> list:
    """波括弧深度0の関数定義/プロトタイプ宣言を `(name, line, name_start, term)` で返す（`term` は `"{"`＝定義／`";"`＝宣言）。

    `name_start` は呼び出し走査から除外するため、`term` は `.c`/`.h` で children 化を使い分けるために使う。
    """
    out: list = []
    depth = 0
    pos = 0
    for i, raw_line in enumerate(sanitized.split("\n"), 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        line_start = pos
        pos += len(raw_line) + 1  # 改行分だけ次行の開始位置へ進める
        if i in pp_lines or line_depth != 0:
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        m = _FUNC_SIG.match(stripped)
        if not m:
            continue
        if m.group("rtype").strip() in _CONTROL_KEYWORDS:
            continue
        # `stripped` の先頭空白除去分を `raw_line` のオフセットへ補正する。
        offset_in_line = len(raw_line) - len(raw_line.lstrip())
        name_start = line_start + offset_in_line + m.start("name")
        out.append((m.group("name"), i, name_start, m.group("term")))
    return out


def _scan_knr_definitions(sanitized: str, pp_lines: set) -> list:
    """波括弧深度0の K&R 形式の関数定義ヘッダを `(name, line, name_start)` で返す。

    `name(params)` 単独行の直後の非空行が `型 名;` 形のパラメータ宣言であることを確認してから採用する。採用位置は呼び出し走査から除き、`Dropped("c_knr_definition", ...)` で申告する。
    """
    out: list = []
    depth = 0
    pos = 0
    lines = sanitized.split("\n")
    for i, raw_line in enumerate(lines, 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        line_start = pos
        pos += len(raw_line) + 1
        if i in pp_lines or line_depth != 0:
            continue
        stripped = raw_line.strip()
        if not stripped:
            continue
        m = _KNR_HEADER.match(stripped)
        if not m:
            continue
        if m.group("rtype").strip() in _CONTROL_KEYWORDS:
            continue
        nxt = None
        for j in range(i, len(lines)):
            candidate = lines[j].strip()
            if candidate:
                nxt = candidate
                break
        if nxt is None or not _KNR_PARAM_DECL.match(nxt):
            continue
        offset_in_line = len(raw_line) - len(raw_line.lstrip())
        name_start = line_start + offset_in_line + m.start("name")
        out.append((m.group("name"), i, name_start))
    return out


def _looks_like_pointer_decl_prefix(line_prefix: str) -> bool:
    """`(*名)(` の前置部が関数ポインタの宣言形（型指定子だけ）かを判定する。先頭トークンが制御構文キーワード（`return (*fp)(x);` 等）なら宣言とみなさない。"""
    if not _POINTER_DECL_PREFIX.match(line_prefix):
        return False
    tokens = line_prefix.split()
    return bool(tokens) and tokens[0] not in _CONTROL_KEYWORDS


class CAnalyzer(Analyzer):
    """ファイル自体 → `Module`（primary・拡張子込みファイル名）。トップレベル関数定義/プロトタイプ
    → `Module`（children・`<ファイル名>.<関数名>` 修飾）。`#include`/呼び出し → `INVOKES` 候補。"""

    name = "c"
    extensions = C_EXT
    resolves_calls_by_simple_name = True
    doctype = "c"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        filename = PurePosixPath(rel_path).name
        is_header = PurePosixPath(rel_path).suffix.lower() == ".h"
        sanitized = _sanitize(text)
        lines_raw = text.splitlines()
        pp_lines = _preprocessor_line_numbers(text)
        # `.c` は定義（`{` 終端）だけを children 化する（`;` 終端の外部宣言を偽 child にしない）。`.h` は宣言（`;`）も children 化する。
        decls = _scan_func_decls(sanitized, pp_lines)
        if is_header:
            child_decls = decls
        else:
            child_decls = [d for d in decls if d[3] == "{"]
        # `c_kind` を children の索引へ持たせる（単純名の呼び出し解決が定義を宣言より優先するため）。
        children = [
            DefItem(label="Module", name=name, cid_key=f"{filename}.{name}", line=line,
                    extra={"c_kind": "definition" if term == "{" else "declaration"})
            for name, line, _start, term in child_decls
        ]
        dropped: list = []
        if is_header:
            cxx_m = _CXX_ONLY_HEADER.search(sanitized)
            if cxx_m:
                line = sanitized.count("\n", 0, cxx_m.start()) + 1
                snippet = lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else ""
                dropped.append(Dropped("cxx_header", line, snippet))
        return DefResult(primary=DefItem(label="Module", name=filename), children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        comments_only = _sanitize_comments_only(text)
        pp_lines = _preprocessor_line_numbers(text)
        newline_offsets = _newline_offsets(text)
        refs: list = []
        dropped: list = []

        for m in _INCLUDE.finditer(comments_only):
            line = _line_at(newline_offsets, m.start())
            local = m.group("local")
            if not local:
                continue
            local = local.replace("\\", "/")  # Windows 区切り正規化
            basename = PurePosixPath(local).name
            if basename:
                refs.append(RefCandidate("INVOKES", "Module", basename, line,
                                         extra={"via": "include", "include_path": local}))

        for m in _DYNAMIC_CALL.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            if line in pp_lines:
                continue
            line_start = sanitized.rfind("\n", 0, m.start()) + 1
            line_prefix = sanitized[line_start:m.start()]
            if _looks_like_pointer_decl_prefix(line_prefix):
                continue  # 前置部が型指定子だけの宣言形（`int (*fp)(int);`）は
                                                            # 呼び出しではないので黙って見逃す（検出限界）。
            dropped.append(Dropped("c_dynamic_call", line, sanitized.splitlines()[line - 1].strip()[:120]))

        decl_positions = {start for _name, _line, start, _term in _scan_func_decls(sanitized, pp_lines)}
        knr_defs = _scan_knr_definitions(sanitized, pp_lines)
        knr_positions = {start for _name, _line, start in knr_defs}
        for name, line, _start in knr_defs:
            dropped.append(Dropped("c_knr_definition", line, sanitized.splitlines()[line - 1].strip()[:120]))
        macro_names = {m.group("name") for m in _MACRO_DEFINE.finditer(sanitized)}

        for m in _CALL.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            if line in pp_lines or m.start("name") in decl_positions or m.start("name") in knr_positions:
                continue
            name = m.group("name")
            if name in _CONTROL_KEYWORDS:
                continue
            if name in macro_names:
                dropped.append(Dropped("c_macro_call", line, name))
                continue
            refs.append(RefCandidate("INVOKES", "Module", name, line, extra={"via": "call"}))

        return RefResult(refs=refs, dropped=dropped)
