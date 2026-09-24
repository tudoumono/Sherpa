"""C# アナライザ（docs/proposals/2026-09-05-アナライザ拡張.md §4(a)/§9 S7・A1＝本体のみ・FW なし）。

`class`/`interface`/`struct`/`enum`/`record`（`record class`/`record struct` を含む・ファイル主体）
を主体定義（`Module`）とし、Java と同様に同一ファイル内の非 public 型を子定義（`Module`・
`CONTAINS`）として返す。`namespace X;`（ファイルスコープ）と `namespace X { ... }`（ブロック
スコープ）の両方に対応し、`cid_key` に `Namespace.Type` の完全修飾名を持たせる（Java の package
修飾と同型・§4(c)' の qualified 名2段解決の対象になる）。

C# は基底クラス/実装インタフェースを `:` 1つで併記し `implements` 相当のキーワードを持たない
ため、判定を誤って作り込むより単純化する——**base list の全エントリを `via=extends` に統一**
する（`docs/proposals` の記載どおり `implements` は使わない）。base list の対象範囲は `where`
（ジェネリクス制約）の手前までに限定し、`enum X : int` の `:`（underlying type・継承ではない）
は base list として扱わない（enum 宣言自体を対象外にする）。フィールド/プロパティ/引数の
宣言型は Java と同じ枠組みで `via=field_type`（`var` は除外・ジェネリクス1段まで）。
`using X;`（インポート形）は参照にしない（ヒントのみ・Java の import と同じ扱い）。
`using Alias = Namespace.Real;`（エイリアス形）は辞書化し、base/field/new の型トークンが
エイリアスなら実体（`Namespace.Real`）への `qualified` 参照（`extra={"qualified": True}`）に
置換して既存の完全修飾名2段解決へ渡す。`[Inject]` 格上げは行わない（A2）。`new X(...)` は
`via=call`。`partial class`（複数ファイル分割）は解決せず `Dropped("cs_partial", ...)` として
申告する。

外部パーサは使わず、正規表現＋行走査で確実に取れるものだけ取る（COBOL/JCL/Java と同じ流儀）。
コメント（`//`・`/* */`）と文字列/char/逐語的文字列リテラル（`@"..."`・二重引用符のエスケープが
`""` になる点が通常の文字列と異なる）は `_sanitize()` で空白化してから走査する。

大文字小文字は区別する（正規化しない・C# は大文字小文字を区別する言語）。

**検出限界**: ジェネリクス制約（`where T : class`）は無視する（base list 抽出を `where` の手前で
打ち切るだけで、制約自体は解析しない）。複数物理行にまたがる型ヘッダ/宣言は見逃す（安全側）。
"""
from __future__ import annotations

import re

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

CSHARP_EXT = frozenset({".cs"})

_NAMESPACE_FILE_SCOPED = re.compile(r'^\s*namespace\s+([\w.]+)\s*;', re.M)
_NAMESPACE_BLOCK = re.compile(r'^\s*namespace\s+([\w.]+)\s*\{', re.M)
# `using Alias = Namespace.Real;`（エイリアス形・item9）。プレーンな `using X;`（インポート形）は
# エイリアス化しないため別枠で辞書化するだけで、参照候補には出さない。
_USING_ALIAS = re.compile(r'^\s*using\s+([A-Za-z_][\w]*)\s*=\s*([\w.]+)\s*;', re.M)
# `record`（位置指定 primary constructor）は `record class`/`record struct` の2形も併記できる——
# 後続の型キーワードを挟んでから名前が来る点だけ他の宣言と異なる。
_TYPE_DECL = re.compile(
    r'\b(?:partial\s+)?(?:class|interface|struct|enum|record(?:\s+(?:class|struct))?)\s+([A-Za-z_][\w]*)'
)
_PARTIAL_MODIFIER = re.compile(r'\bpartial\b')
_PUBLIC_MODIFIER = re.compile(r'\bpublic\b')
_ENUM_KEYWORD = re.compile(r'\benum\b')
_WHERE_KEYWORD = re.compile(r'\bwhere\b')
# base list 本体（`where` 手前に切り詰めた範囲だけを対象にする・呼び出し側で truncate 済み）。
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
# フィールド／自動実装プロパティ宣言（クラス直下＝深度1限定）。終端は `;`（フィールド/式形プロパティ）
# ／`=`（初期化子）／`{`（`{ get; set; }` 形の自動実装プロパティ）のいずれか。
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
    """コメント（`//`・`/* */`）と文字列/char/逐語的文字列リテラルの中身を空白化した、同じ行数の
    文字列を返す（偽マッチ除外専用・改行は保持し行番号が原本と1対1のまま）。"""
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
        if ch == "@" and text[i:i + 2] == '@"':            # 逐語的文字列（`""` はエスケープされた `"`）
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
    """base list（`: Base, IFoo` 等）のカンマ区切り要素から型トークンを取り出す。`.` を含む
    完全修飾トークン（`A.Base` 等）は完全名のまま返す——単純名へ落とさない（`_emit_type_ref` 側で
    `.` の有無により `qualified` 参照へ振り分ける）。"""
    names = []
    for part in _strip_generics(raw).split(","):
        token = re.sub(r"[^\w.]", "", part).strip(".")
        if token:
            names.append(token)
    return names


def _namespace_and_depth_offset(sanitized: str):
    """`namespace` の宣言形（ファイルスコープ／ブロックスコープ）を判定し、`(package, depth_offset)`
    を返す。ブロックスコープ（`namespace X { ... }`）はそれ自体が波括弧1段を消費するため、内側の
    型宣言を「トップレベル」とみなす基準の深度を1つずらす（Java には無い C# 固有の事情）。
    """
    fm = _NAMESPACE_FILE_SCOPED.search(sanitized)
    if fm:
        return fm.group(1), 0
    bm = _NAMESPACE_BLOCK.search(sanitized)
    if bm:
        return bm.group(1), 1
    return None, 0


def _iter_top_level_type_decls(sanitized: str, depth_offset: int):
    """`depth_offset`（`namespace` ブロックの有無で0または1）と同じ波括弧深度の型宣言を
    `(match, line, is_public)` で返す。それより深い（内部クラス等）は `nested` として返す。"""
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
    """`INVOKES` 候補を1件積む——`name` が `using` エイリアス（item9）なら実体（完全修飾名）への
    `qualified` 参照に置換して既存の完全修飾名2段解決（`_resolve_qualified`）へ渡す。`name` 自体が
    既に `.` を含む完全修飾トークン（base list/宣言型/`new` で直接書かれた完全名）の場合も
    同じ `qualified` 参照にする——単純名へ落とさず完全名を保持したまま解決へ渡す（alias 置換と
    同じ経路。単純名フォールバックは共通層 `world_graph._link` 側が担う）。"""
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
    """宣言型（＋1段のジェネリクス型引数）を `INVOKES(via=field_type)` 候補として積む。
    共通型（`_JDK_LIKE_COMMON_TYPES`）・小文字始まり（`var`/プリミティブ含む）は候補にしない。
    `type_token`（および各ジェネリクス型引数）が `.` を含む完全修飾名（`A.Base`/`C.Dep` 等）の
    場合、判定（大文字始まり・共通型除外）には末尾セグメントを使うが、参照そのものは完全名の
    まま渡す（`_emit_type_ref` 側で `qualified` 参照へ振り分ける——単純名へ落とさない）。"""
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
    """フィールド/自動実装プロパティ/コンストラクタ引数/メソッド引数の宣言型を参照候補として抽出
    する（トップレベル型の直下＝`depth_offset + 1` に限定・Java 版と同じ「メソッド本体内は対象外」
    規律）。"""
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

        # `partial class`（複数ファイル分割）は解決せず申告するだけ（Dropped・撤去した children には
        # しない——ファイル自体の主体定義は通常どおり作る）。
        for m, line, _pub in top:
            if _PARTIAL_MODIFIER.search(m.group(0)):        # `partial` は `_TYPE_DECL` 自身の
                                                             # マッチ文字列内に含まれる（マッチ前ではない）
                snippet = (lines_raw[line - 1].strip()[:120] if line - 1 < len(lines_raw) else "")
                dropped.append(Dropped("cs_partial", line, snippet))

        primary = DefItem(label="Module", name=primary_name, cid_key=qualified)
        # 非 primary 型（同一ファイル内の内部/非 public 型）にも namespace 込みの `cid_key` を
        # 設定する——別 namespace の同名型と区別できないと、`using Alias = N.Child;` のような
        # 完全修飾参照が意図した namespace の型へ一意に解決できない（`_resolve_qualified` は
        # `cid_key` の完全一致でしか引けないため）。
        children = [
            DefItem(label="Module", name=m.group(1),
                    cid_key=f"{package}.{m.group(1)}" if package else m.group(1), line=line)
            for i, (m, line, _pub) in enumerate(top) if i != primary_idx
        ]

        return DefResult(primary=primary, children=children, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        _package, depth_offset = _namespace_and_depth_offset(sanitized)
        # `using Alias = Namespace.Real;`（item9）を辞書化する——プレーンな `using X;` はヒントの
        # ままエッジ化しない（マッチ対象が違う正規表現のため自然に除外される）。
        aliases = dict(_USING_ALIAS.findall(sanitized))
        refs: list = []

        top, _nested = _iter_top_level_type_decls(sanitized, depth_offset)
        for m, line, _pub in top:
            if _ENUM_KEYWORD.search(m.group(0)):
                continue                                  # `enum X : int` の `:` は underlying type
                                                            # であり base list ではない（item3）
            header = _header_of(sanitized, m.end())
            wm = _WHERE_KEYWORD.search(header)
            base_part = header[:wm.start()] if wm else header   # `where`（ジェネリクス制約）手前まで
            bm = _BASE_LIST.search(base_part)
            if bm:
                for name in _split_type_list(bm.group("bases")):
                    _emit_type_ref(refs, name, line, "extends", aliases)

        for m in _CALL_LIKE.finditer(sanitized):
            line = _line_at(sanitized, m.start())
            # `.` を含む完全修飾トークン（`C.Target()` 等）は完全名のまま渡す——単純名へ落とさない
            # （`_emit_type_ref` 側で `qualified` 参照へ振り分ける）。
            name = _strip_generics(m.group("type")).strip(".")
            if name:
                _emit_type_ref(refs, name, line, "call", aliases)

        refs.extend(_collect_declared_type_refs(sanitized, depth_offset, aliases))

        # `using X;`（インポート形）はヒントのみ（Java の import と同じくエッジ化しない）。
        return RefResult(refs=refs)
