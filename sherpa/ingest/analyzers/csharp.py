"""C# アナライザ（本体のみ・FW なし）。`class`/`interface`/`struct`/`enum`/`record` を主体定義（`Module`）とし、同一ファイル内の非 public 型を子定義（`CONTAINS`）として返す。`namespace X;` と `namespace X { ... }` の両方に対応し、`cid_key` は `Namespace.Type`。

参照（`INVOKES`）:
- base list の全エントリを `via=extends`（`where` の手前まで。`enum X : int` は対象外）。
- フィールド/プロパティ/引数の宣言型を `via=field_type`、`new X(...)` を `via=call`。
- `using X;`・`using Alias = N.T;` は参照にしない。全ての型参照に `type_ref`（共通層が `file_context` の namespace・using・別名で解決する）を付ける。
- `namespace` と `using`（通常・`static`・エイリアス）は `RefResult.file_context` として共通層へ渡す。1 ファイルに namespace ブロックが複数あれば、型ごとにその型を囲む namespace を `cid_key`・`source_symbol_id`・`file_context.namespaces`（参照の行から引く）に使う。
- 参照の始点（`source_symbol_id`）は、その行を本体に含む型。主体以外の型は `(rel_path, "<namespace>.<型名>")`（`cid_key`）、主体の型・型の外は省略（ファイルの主体）。メソッド単位の始点は持たない。
- `partial class` は `Dropped("cs_partial")` で申告する。
正規表現＋行走査（コメント・文字列・逐語的文字列は `_sanitize()` で空白化）。大文字小文字は区別する。複数物理行の宣言は見逃す。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re

from ._base import (Analyzer, DefItem, DefResult, Dropped, FileContext, ImportItem, RefCandidate, RefResult,
                    body_end_line)

CSHARP_EXT = frozenset({".cs"})

_NAMESPACE_FILE_SCOPED = re.compile(r'^\s*namespace\s+([\w.]+)\s*;', re.M)
_NAMESPACE_BLOCK = re.compile(r'^\s*namespace\s+([\w.]+)\s*\{', re.M)
# `using` ディレクティブ全形（`global using`・`using static`・エイリアス）。`using (...)`／`using var x = ...;` の文にはマッチしない。
_USING_DIRECTIVE = re.compile(
    r'^\s*(?P<global>global\s+)?using\s+(?P<static>static\s+)?(?:(?P<alias>[A-Za-z_]\w*)\s*=\s*)?(?P<name>[\w.]+)\s*;', re.M)
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


_GLOBAL_MARK = "__gbl__"


def _sanitize(text: str) -> str:
    """コメントと文字列/char/逐語的文字列リテラルの中身を空白化した同じ行数の文字列を返す（偽マッチ除外用・行番号は原本と1対1）。"""
    out: list = []
    i, n = 0, len(text)
    line_blank = True                                   # 行頭からここまで空白だけか（プリプロセッサ行の判定）
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append("\n")
            i += 1
            line_blank = True
            continue
        if line_blank and ch == "#":                    # `#region {`・`#if` などのプリプロセッサ行は丸ごと空白化（括弧を数えない）
            while i < n and text[i] != "\n":
                out.append(" ")
                i += 1
            continue
        if ch not in " \t":
            line_blank = False
        if ch == '"' and text[i:i + 3] == '"""':        # raw string（`"` を 3 つ以上で囲む・`$"""` の補間も同じ）。同じ数の `"` で閉じる
            k = 3
            while text[i + k:i + k + 1] == '"':
                k += 1
            out.append(" " * k)
            i += k
            while i < n and text[i:i + k] != '"' * k:
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            if i < n:
                out.append(" " * k)
                i += k
            continue
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
    # `global::X` は名前の先頭の印 `__gbl__X` にする（絶対指定。`_emit_type_ref` が `absolute` の参照にする）。
    return re.sub(r"\bglobal::", _GLOBAL_MARK, "".join(out))


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


def _namespace_blocks(sanitized: str) -> list:
    """ブロックスコープの `namespace X { ... }` を `(開き位置, 閉じ位置, 完全な名前)` の一覧で返す（入れ子は親の名前を前置・外側が先）。"""
    out: list = []
    for m in _NAMESPACE_BLOCK.finditer(sanitized):
        brace = m.end() - 1
        depth = 0
        end = len(sanitized) - 1
        for i in range(brace, len(sanitized)):
            if sanitized[i] == "{":
                depth += 1
            elif sanitized[i] == "}":
                depth -= 1
                if depth == 0:
                    end = i
                    break
        parents = [b for b in out if b[0] < brace < b[1]]
        out.append((brace, end, f"{parents[-1][2]}.{m.group(1)}" if parents else m.group(1)))
    return out


def _iter_top_level_type_decls(sanitized: str):
    """namespace 直下の型宣言を `(match, line, is_public, namespace)` で返す（`namespace` はその型を囲むブロック／ファイルスコープの名前・無ければ `None`）。
    より深い（内部クラス等）は `nested` として返す。"""
    blocks = _namespace_blocks(sanitized)
    fm = _NAMESPACE_FILE_SCOPED.search(sanitized)
    file_ns = fm.group(1) if fm else None
    depth = 0
    pos = 0
    top: list = []
    nested: list = []
    for m in _TYPE_DECL.finditer(sanitized):
        depth += sanitized.count("{", pos, m.start()) - sanitized.count("}", pos, m.start())
        pos = m.end()
        line = _line_at(sanitized, m.start())
        enclosing = [b for b in blocks if b[0] < m.start() < b[1]]
        if depth != len(enclosing):
            nested.append((m, line))
            continue
        line_start = sanitized.rfind("\n", 0, m.start()) + 1
        is_public = bool(_PUBLIC_MODIFIER.search(sanitized[line_start:m.start()]))
        top.append((m, line, is_public, enclosing[-1][2] if enclosing else file_ns))
    return top, nested


def _fqn(namespace, name: str) -> str:
    return f"{namespace}.{name}" if namespace else name


def _primary_index(top: list) -> int:
    """トップレベル型のうちファイルの主体にする型の添字（最初の public 型・無ければ先頭）。`collect_defs` と `extract_refs` で共有する。"""
    return next((i for i, (_m, _l, pub, _ns) in enumerate(top) if pub), 0)


def _header_of(sanitized: str, decl_end: int) -> str:
    window_end = min(len(sanitized), decl_end + _HEADER_SCAN_LIMIT)
    brace = sanitized.find("{", decl_end, window_end)
    return sanitized[decl_end:brace if brace != -1 else window_end]


def _emit_type_ref(refs: list, name: str, line: int, via: str) -> None:
    """`INVOKES` 候補を1件積む。`.` を含む完全修飾トークンは `qualified` 参照にする（単純名へ落とさない）。別名（`using A = …`）は共通層が `file_context` の別名で展開する。"""
    extra = {"via": via, "type_ref": True}
    if name.startswith(_GLOBAL_MARK):                  # `global::X.Y`＝今の namespace の下を探さず完全修飾名として解決する
        name = name[len(_GLOBAL_MARK):]
        extra["absolute"] = True
    if "." in name:
        extra["qualified"] = True
    refs.append(RefCandidate("INVOKES", "Module", name, line, extra=extra))


def _emit_declared_type_refs(refs: list, type_token: str, generics_token: str | None, line: int) -> None:
    """宣言型（＋1段のジェネリクス型引数）を `INVOKES(via=field_type)` 候補として積む。共通型・小文字始まり（`var`/プリミティブ）は除く。完全修飾名は、判定には末尾セグメントを使い、参照は完全名のまま渡す。"""
    simple = type_token.replace(_GLOBAL_MARK, "").rsplit(".", 1)[-1]
    if simple[:1].isupper() and simple not in _JDK_LIKE_COMMON_TYPES:
        _emit_type_ref(refs, type_token, line, "field_type")
    if not generics_token:
        return
    inner = generics_token.strip("<>")
    for arg in _split_top_level_commas(inner):
        arg = _strip_generics(arg).strip()
        arg = re.sub(r"\[\]\s*$", "", arg).strip().rstrip("?")
        simple_arg = arg.replace(_GLOBAL_MARK, "").rsplit(".", 1)[-1]
        if (simple_arg[:1].isupper() and simple_arg not in _JDK_LIKE_COMMON_TYPES
                and re.fullmatch(r"[A-Za-z_][\w]*", simple_arg)):
            _emit_type_ref(refs, arg, line, "field_type")


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


def _collect_declared_type_refs(sanitized: str, block_lines: list) -> list:
    """フィールド/自動実装プロパティ/コンストラクタ/メソッド引数の宣言型を参照候補として抽出する（トップレベル型の直下＝囲む namespace ブロックの数 + 1 の深さのみ。メソッド本体内は対象外）。"""
    refs: list = []
    depth = 0
    for i, raw_line in enumerate(sanitized.split("\n"), 1):
        line_depth = depth
        depth += raw_line.count("{") - raw_line.count("}")
        stripped = raw_line.strip()
        if not stripped:
            continue
        if _ANNOTATION_ONLY_LINE.match(stripped):
            continue
        if line_depth != sum(1 for lo, hi in block_lines if lo < i < hi) + 1:
            continue
        body = _LEADING_ATTRIBUTES.sub("", stripped)
        fm = _FIELD_OR_PROP_DECL.match(body)
        if fm:
            _emit_declared_type_refs(refs, fm.group("type"), fm.group("generics"), i)
            continue
        params = _find_param_list(body)
        if params is not None:
            for entry in _split_top_level_commas(params):
                entry = entry.strip()
                if not entry:
                    continue
                pm = _PARAM_ENTRY_TYPE.match(entry)
                if pm:
                    _emit_declared_type_refs(refs, pm.group("type"), pm.group("generics"), i)
    return refs


def _using_imports(sanitized: str, block_lines: list | None = None) -> list:
    """`using` ディレクティブの一覧。`using N;` は名前空間の import（ワイルドカード）、`using static T;` は型の名前を持ち込まない static、`using A = N.T;` は別名。"""
    return [ImportItem(kind=("alias" if m.group("alias") else "single" if m.group("static") else "wildcard"),
                       name=m.group("name").replace(_GLOBAL_MARK, ""), alias=m.group("alias"), static=bool(m.group("static")),
                       line=_line_at(sanitized, m.start("name")), is_global=bool(m.group("global")),
                       scope=_using_scope(_line_at(sanitized, m.start("name")), block_lines))
            for m in _USING_DIRECTIVE.finditer(sanitized)]


def _using_scope(line: int, block_lines: list | None):
    """`line` の `using` を囲む最も内側の namespace ブロックの `(開始行, 終了行)`。ブロックの外（ファイル先頭）は `None`。"""
    inside = [(lo, hi) for lo, hi in (block_lines or []) if lo < line < hi]
    return min(inside, key=lambda r: r[1] - r[0]) if inside else None


class CSharpAnalyzer(Analyzer):
    """`class/interface/struct/enum/record`（public＝primary・他は children）→ `Module`。
    継承/実装（`: Base, IFoo`・全件 `via=extends`）・宣言型（`via=field_type`）・`new X(...)`
    （`via=call`）→ `INVOKES` 候補。"""

    name = "csharp"
    extensions = CSHARP_EXT
    doctype = "csharp"
    requires_file_context = True
    resolves_parent_namespaces = True
    version = 3

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        sanitized = _sanitize(text)
        lines_raw = text.splitlines()
        top, nested = _iter_top_level_type_decls(sanitized)
        dropped = [Dropped("nested_type", line,
                           (lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else ""))
                   for _m, line in nested]
        if not top:
            return DefResult(dropped=dropped)

        primary_idx = _primary_index(top)
        primary_m, primary_line, _pub, primary_ns = top[primary_idx]
        primary_name = primary_m.group(1)

        # `partial class` は解決せず `Dropped` で申告するだけ（ファイルの主体定義は通常どおり作る）。
        for m, line, _pub, _ns in top:
            if _PARTIAL_MODIFIER.search(m.group(0)):  # `partial` は `_TYPE_DECL` 自身の
                                                             # マッチ文字列内に含まれる
                snippet = (lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else "")
                dropped.append(Dropped("cs_partial", line, snippet))

        primary_key = _fqn(primary_ns, primary_name)
        primary = DefItem(label="Module", name=primary_name, cid_key=primary_key)
        # 非 primary 型にも、その型を囲む namespace 込みの `cid_key` を設定する（別 namespace の同名型と区別し、完全修飾参照を一意に解決するため）。
        # 同じキーの型（`partial` の同名）は 1 つのノード（primary・既出の child と同じ cid を二重に作らない）。
        children = []
        seen_keys = {primary_key}
        for i, (m, line, _pub, ns) in enumerate(top):
            key = _fqn(ns, m.group(1))
            if i == primary_idx or key in seen_keys:
                continue
            seen_keys.add(key)
            children.append(DefItem(label="Module", name=m.group(1), cid_key=key, line=line))

        return DefResult(primary=primary, children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        refs: list = []

        top, _nested = _iter_top_level_type_decls(sanitized)
        for m, line, _pub, _ns in top:
            if _ENUM_KEYWORD.search(m.group(0)):
                continue  # `enum X : int` の `:` は underlying type
                                                            # であり base list ではない
            header = _header_of(sanitized, m.end())
            wm = _WHERE_KEYWORD.search(header)
            base_part = header[:wm.start()] if wm else header  # `where`（ジェネリクス制約）手前まで
            bm = _BASE_LIST.search(base_part)
            if bm:
                for name in _split_type_list(bm.group("bases")):
                    _emit_type_ref(refs, name, line, "extends")

        for m in _CALL_LIKE.finditer(sanitized):
            line = _line_at(sanitized, m.start())
            # `.` を含む完全修飾トークンは完全名のまま渡す。
            name = _strip_generics(m.group("type")).strip(".")
            if name:
                _emit_type_ref(refs, name, line, "call")

        block_lines = [(_line_at(sanitized, lo), _line_at(sanitized, hi)) for lo, hi, _n in _namespace_blocks(sanitized)]
        refs.extend(_collect_declared_type_refs(sanitized, block_lines))

        # 始点＝その行を本体に含む型（主体以外の型だけ。主体・型の外は省略＝ファイルの主体）。キーは `collect_defs` の children の `cid_key`。
        # 同じキーの型（`partial` の同名）は 1 つの定義。同じ行に 2 つ以上の型がかかる行は決められないので主体にし、
        # `Dropped("ambiguous_source_symbol")` を 1 行 1 件残す。
        primary_idx = _primary_index(top)
        spans = [(_fqn(ns, m.group(1)), line, body_end_line(sanitized, m.end()))
                 for m, line, _pub, ns in top]
        primary_key = spans[primary_idx][0] if spans else None
        ambiguous_dropped: list = []
        ambiguous_lines: set = set()
        for ref in refs:
            hits = [key for key, start, end in spans if start <= ref.line <= end]
            if len(set(hits)) > 1:
                if ref.line not in ambiguous_lines:
                    ambiguous_lines.add(ref.line)
                    ambiguous_dropped.append(Dropped("ambiguous_source_symbol", ref.line, ", ".join(hits)))
            elif hits and hits[0] != primary_key:
                ref.source_symbol_id = (rel_path, hits[0])

        # 参照ごとの namespace は、その行を含む型（本体の範囲）の namespace。`using X;` は参照にせず `file_context` として渡す。
        namespaces = [(line, body_end_line(sanitized, m.end()), ns) for m, line, _pub, ns in top]
        package = top[_primary_index(top)][3] if top else None
        return RefResult(refs=refs, dropped=ambiguous_dropped, file_context=FileContext(package=package, namespaces=namespaces,
                                                             imports=_using_imports(sanitized, block_lines)))

    def global_imports(self, text: str, rel_path: str) -> list:
        return [i for i in _using_imports(_sanitize(text)) if i.is_global]
