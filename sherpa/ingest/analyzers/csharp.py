"""C# アナライザ（本体のみ・FW なし）。`class`/`interface`/`struct`/`enum`/`record` を主体定義（`Module`）とし、同一ファイル内の非 public 型を子定義（`CONTAINS`）として返す。`namespace X;` と `namespace X { ... }` の両方に対応し、`cid_key` は `Namespace.Type`。

参照（`INVOKES`）:
- base list の全エントリを `via=extends`（`where` の手前まで。`enum X : int` は対象外）。
- フィールド/プロパティ/引数の宣言型を `via=field_type`、`new X(...)` を `via=call`。
- `using Alias = Namespace.Real;` は辞書化し、型トークンがエイリアスなら実体への `qualified` 参照に置換する。`using X;` は参照にしない。
- `partial class` は `Dropped("cs_partial")` で申告する。
正規表現＋行走査（コメント・文字列・逐語的文字列は `_sanitize()` で空白化）。大文字小文字は区別する。複数物理行の宣言は見逃す。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

CSHARP_EXT = frozenset({".cs"})

_NAMESPACE_FILE_SCOPED = re.compile(r'^\s*namespace\s+([\w.]+)\s*;', re.M)
_NAMESPACE_BLOCK = re.compile(r'^\s*namespace\s+([\w.]+)\s*\{', re.M)
# `using Alias = Namespace.Real;`（エイリアス形）。`using X;`（インポート形）は参照候補に出さない。
_USING_ALIAS = re.compile(r'^\s*using\s+([A-Za-z_][\w]*)\s*=\s*([\w.]+)\s*;', re.M)
# `record` は `record class`/`record struct` の形もあり、型キーワードを挟んでから名前が来る。
_TYPE_DECL = re.compile(
    r'\b(?:partial\s+)?(?:class|interface|struct|enum|record(?:\s+(?:class|struct))?)\s+([A-Za-z_][\w]*)'
)
_PARTIAL_MODIFIER = re.compile(r'\bpartial\b')
_PUBLIC_MODIFIER = re.compile(r'\bpublic\b')
_ENUM_KEYWORD = re.compile(r'\benum\b')
_WHERE_KEYWORD = re.compile(r'\bwhere\b')
# base list 本体（`where` 手前に切り詰めた範囲だけが対象・呼び出し側で truncate 済み）。
_BASE_LIST = re.compile(r':\s*(?P<bases>[^{;]+)', re.S)
_HEADER_SCAN_LIMIT = 4000

_CALL_LIKE = re.compile(r'\bnew\s+(?P<type>[A-Za-z_][\w.]*)(?:\s*<[^>{};]*>)?\s*\(')

_JDK_LIKE_COMMON_TYPES = frozenset({
    "object", "Object", "string", "String", "bool", "Boolean", "byte", "Byte", "sbyte",
    "short", "Int16", "int", "Int32", "long", "Int64", "float", "Single", "double", "Double",
    "decimal", "Decimal", "char", "Char", "void", "var", "dynamic", "Task", "Action", "Func",
    "List", "IList", "IEnumerable", "ICollection", "Dictionary", "IDictionary", "Nullable",
    "DateTime", "TimeSpan", "Guid", "Exception", "Type",
})

_ANNOTATION_ONLY_LINE = re.compile(r'^\[(?P<name>[A-Za-z_][\w]*)(?:\([^)]*\))?\]\s*$')
_LEADING_ATTRIBUTES = re.compile(r'^(?:\[[A-Za-z_][\w]*(?:\([^)]*\))?\]\s*)+')
# フィールド／自動実装プロパティ宣言（クラス直下＝深度1）。終端は `;`／`=`／`{` のいずれか。
_FIELD_OR_PROP_DECL = re.compile(
    r'^(?:(?:public|private|protected|internal|static|readonly|const|virtual|override|sealed|'
    r'abstract|new)\s+)*'
    r'(?P<type>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)'
    r'(?P<generics><[^<>{};]*>)?'
    r'\??'
    r'(?:\s*\[\])*'
    r'\s+[A-Za-z_][\w]*\s*(?P<term>[=;]|\{)'
)
_METHOD_NAME_PAREN = re.compile(r'\b([A-Za-z_][\w]*)\s*\(')
_PARAM_ENTRY_TYPE = re.compile(
    r'^(?:(?:ref|out|in|params|this)\s+)*'
    r'(?:\[[A-Za-z_][\w]*(?:\([^)]*\))?\]\s*)*'
    r'(?P<type>[A-Za-z_][\w]*(?:\.[A-Za-z_][\w]*)*)'
    r'(?P<generics><[^<>]*>)?'
    r'\??'
    r'(?:\s*\[\])*'
    r'\s+[A-Za-z_][\w]*(?:\s*=.*)?$'
)


def _sanitize(text: str) -> str:
    """コメントと文字列/char/逐語的文字列リテラルの中身を空白化した同じ行数の文字列を返す（偽マッチ除外用・行番号は原本と1対1）。"""
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
        if ch == "@" and text[i:i + 2] == '@"':  # 逐語的文字列（`""` はエスケープされた `"`）
            out.append("  ")
            i += 2
            while i < n:
                if text[i] == '"' and text[i:i + 2] == '""':
                    out.append("  ")
                    i += 2
                    continue
                if text[i] == '"':
                    break
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
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


def _line_at(sanitized: str, pos: int) -> int:
    return sanitized.count("\n", 0, pos) + 1


def _strip_generics(s: str) -> str:
    out: list = []
    depth = 0
    for ch in s:
        if ch == "<":
            depth += 1
            continue
        if ch == ">":
            if depth > 0:
                depth -= 1
            continue
        if depth == 0:
            out.append(ch)
    return "".join(out)


def _split_top_level_commas(s: str) -> list:
    parts: list = []
    depth = 0
    buf: list = []
    for ch in s:
        if ch == "<":
            depth += 1
        elif ch == ">":
            depth = max(0, depth - 1)
        if ch == "," and depth == 0:
            parts.append("".join(buf))
            buf = []
        else:
            buf.append(ch)
    if buf:
        parts.append("".join(buf))
    return parts


def _split_type_list(raw: str) -> list:
    """base list のカンマ区切り要素から型トークンを取り出す。`.` を含む完全修飾トークンは完全名のまま返す。"""
    names = []
    for part in _strip_generics(raw).split(","):
        token = re.sub(r"[^\w.]", "", part).strip(".")
        if token:
            names.append(token)
    return names


def _namespace_and_depth_offset(sanitized: str):
    """`namespace` の宣言形（ファイルスコープ／ブロックスコープ）を判定し `(package, depth_offset)` を返す。ブロックスコープは波括弧1段を消費するため、型宣言の基準深度を1つずらす。"""
    fm = _NAMESPACE_FILE_SCOPED.search(sanitized)
    if fm:
        return fm.group(1), 0
    bm = _NAMESPACE_BLOCK.search(sanitized)
    if bm:
        return bm.group(1), 1
    return None, 0


def _iter_top_level_type_decls(sanitized: str, depth_offset: int):
    """`depth_offset` と同じ波括弧深度の型宣言を `(match, line, is_public)` で返す。より深い（内部クラス等）は `nested` として返す。"""
    depth = 0
    pos = 0
    top: list = []
    nested: list = []
    for m in _TYPE_DECL.finditer(sanitized):
        depth += sanitized.count("{", pos, m.start()) - sanitized.count("}", pos, m.start())
        pos = m.end()
        line = _line_at(sanitized, m.start())
        if depth != depth_offset:
            nested.append((m, line))
            continue
        line_start = sanitized.rfind("\n", 0, m.start()) + 1
        is_public = bool(_PUBLIC_MODIFIER.search(sanitized[line_start:m.start()]))
        top.append((m, line, is_public))
    return top, nested


def _header_of(sanitized: str, decl_end: int) -> str:
    window_end = min(len(sanitized), decl_end + _HEADER_SCAN_LIMIT)
    brace = sanitized.find("{", decl_end, window_end)
    return sanitized[decl_end:brace if brace != -1 else window_end]


def _emit_type_ref(refs: list, name: str, line: int, via: str, aliases: dict) -> None:
    """`INVOKES` 候補を1件積む。`name` が `using` エイリアスなら実体（完全修飾名）の `qualified` 参照に置換する。`.` を含む完全修飾トークンも同じ `qualified` 参照にする（単純名へ落とさない）。"""
    if name in aliases:
        refs.append(RefCandidate("INVOKES", "Module", aliases[name], line,
                                 extra={"via": via, "qualified": True}))
    elif "." in name:
        refs.append(RefCandidate("INVOKES", "Module", name, line,
                                 extra={"via": via, "qualified": True}))
    else:
        refs.append(RefCandidate("INVOKES", "Module", name, line, extra={"via": via}))


def _emit_declared_type_refs(refs: list, type_token: str, generics_token: str | None, line: int,
                             aliases: dict) -> None:
    """宣言型（＋1段のジェネリクス型引数）を `INVOKES(via=field_type)` 候補として積む。共通型・小文字始まり（`var`/プリミティブ）は除く。完全修飾名は、判定には末尾セグメントを使い、参照は完全名のまま渡す。"""
    simple = type_token.rsplit(".", 1)[-1]
    if simple[:1].isupper() and simple not in _JDK_LIKE_COMMON_TYPES:
        _emit_type_ref(refs, type_token, line, "field_type", aliases)
    if not generics_token:
        return
    inner = generics_token.strip("<>")
    for arg in _split_top_level_commas(inner):
        arg = _strip_generics(arg).strip()
        arg = re.sub(r"\[\]\s*$", "", arg).strip().rstrip("?")
        simple_arg = arg.rsplit(".", 1)[-1]
        if (simple_arg[:1].isupper() and simple_arg not in _JDK_LIKE_COMMON_TYPES
                and re.fullmatch(r"[A-Za-z_][\w]*", simple_arg)):
            _emit_type_ref(refs, arg, line, "field_type", aliases)


def _find_param_list(line: str):
    for m in _METHOD_NAME_PAREN.finditer(line):
        if m.group(1) == "new":
            continue
        pre = line[:m.start()].rstrip()
        if pre.endswith("new") and (len(pre) == 3 or not pre[-4].isalnum()):
            continue
        open_pos = m.end() - 1
        depth = 0
        for j in range(open_pos, len(line)):
            if line[j] == "(":
                depth += 1
            elif line[j] == ")":
                depth -= 1
                if depth == 0:
                    return line[open_pos + 1:j]
        return None
    return None


def _collect_declared_type_refs(sanitized: str, depth_offset: int, aliases: dict) -> list:
    """フィールド/自動実装プロパティ/コンストラクタ/メソッド引数の宣言型を参照候補として抽出する（トップレベル型の直下＝`depth_offset + 1` のみ。メソッド本体内は対象外）。"""
    refs: list = []
    depth = 0
    target_depth = depth_offset + 1
    for i, raw_line in enumerate(sanitized.split("\n"), 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _ANNOTATION_ONLY_LINE.match(stripped):
            continue
        if line_depth != target_depth:
            continue
        body = _LEADING_ATTRIBUTES.sub("", stripped)
        fm = _FIELD_OR_PROP_DECL.match(body)
        if fm:
            _emit_declared_type_refs(refs, fm.group("type"), fm.group("generics"), i, aliases)
            continue
        params = _find_param_list(body)
        if params is not None:
            for entry in _split_top_level_commas(params):
                entry = entry.strip()
                if not entry:
                    continue
                pm = _PARAM_ENTRY_TYPE.match(entry)
                if pm:
                    _emit_declared_type_refs(refs, pm.group("type"), pm.group("generics"), i, aliases)
    return refs


class CSharpAnalyzer(Analyzer):
    """`class/interface/struct/enum/record`（public＝primary・他は children）→ `Module`。
    継承/実装（`: Base, IFoo`・全件 `via=extends`）・宣言型（`via=field_type`）・`new X(...)`
    （`via=call`）→ `INVOKES` 候補。"""

    name = "csharp"
    extensions = CSHARP_EXT
    doctype = "csharp"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        sanitized = _sanitize(text)
        lines_raw = text.splitlines()
        package, depth_offset = _namespace_and_depth_offset(sanitized)
        top, nested = _iter_top_level_type_decls(sanitized, depth_offset)
        dropped = [Dropped("nested_type", line,
                           (lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else ""))
                   for _m, line in nested]
        if not top:
            return DefResult(dropped=dropped)

        primary_idx = next((i for i, (_m, _l, pub) in enumerate(top) if pub), 0)
        primary_m, primary_line, _pub = top[primary_idx]
        primary_name = primary_m.group(1)
        qualified = f"{package}.{primary_name}" if package else primary_name

        # `partial class` は解決せず `Dropped` で申告するだけ（ファイルの主体定義は通常どおり作る）。
        for m, line, _pub in top:
            if _PARTIAL_MODIFIER.search(m.group(0)):  # `partial` は `_TYPE_DECL` 自身の
                                                             # マッチ文字列内に含まれる
                snippet = (lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else "")
                dropped.append(Dropped("cs_partial", line, snippet))

        primary = DefItem(label="Module", name=primary_name, cid_key=qualified)
        # 非 primary 型にも namespace 込みの `cid_key` を設定する（別 namespace の同名型と区別し、完全修飾参照を一意に解決するため）。
        children = [
            DefItem(label="Module", name=m.group(1),
                    cid_key=f"{package}.{m.group(1)}" if package else m.group(1), line=line)
            for i, (m, line, _pub) in enumerate(top) if i != primary_idx
        ]

        return DefResult(primary=primary, children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        _package, depth_offset = _namespace_and_depth_offset(sanitized)
        # `using Alias = Namespace.Real;` を辞書化する。
        aliases = dict(_USING_ALIAS.findall(sanitized))
        refs: list = []

        top, _nested = _iter_top_level_type_decls(sanitized, depth_offset)
        for m, line, _pub in top:
            if _ENUM_KEYWORD.search(m.group(0)):
                continue  # `enum X : int` の `:` は underlying type
                                                            # であり base list ではない
            header = _header_of(sanitized, m.end())
            wm = _WHERE_KEYWORD.search(header)
            base_part = header[:wm.start()] if wm else header  # `where`（ジェネリクス制約）手前まで
            bm = _BASE_LIST.search(base_part)
            if bm:
                for name in _split_type_list(bm.group("bases")):
                    _emit_type_ref(refs, name, line, "extends", aliases)

        for m in _CALL_LIKE.finditer(sanitized):
            line = _line_at(sanitized, m.start())
            # `.` を含む完全修飾トークンは完全名のまま渡す。
            name = _strip_generics(m.group("type")).strip(".")
            if name:
                _emit_type_ref(refs, name, line, "call", aliases)

        refs.extend(_collect_declared_type_refs(sanitized, depth_offset, aliases))

        # `using X;`（インポート形）はヒントのみでエッジ化しない。
        return RefResult(refs=refs)
