"""VB アナライザ。VB.NET（`.vb`）・VB6/VBA エクスポート（`.bas`/`.cls`/`.frm`/`.ctl`）・VBScript（`.vbs`）を拡張子で内部分岐して1本で扱う。型を主体定義（`Module`）、`Sub`/`Function`/`Property` を children（`cid_key="<Type>.<Name>"`）として返す。

主体: VB.NET は `Namespace` 配下の `Class/Module/Structure/Interface/Enum`（public または最初のトップレベル型が primary・`cid_key="Namespace.Type"`・他の型は children）。`Namespace` はネスト・複数出現に対応する。VB6/VBA は `Attribute VB_Name`（`.frm` は `Begin VB.Form` の名前、無ければファイル名ステム）を primary とする。入れ子の型は `Dropped("vb_nested_type")` で申告し、中の手続きも children にしない。
children: 同一 `(label, cid_key, c_kind)` の重複（overload・`Property Get/Let/Set`）は1件へ集約する。`extra["c_kind"]` は通常形が `"definition"`、`Declare ... Lib` が `"declaration"`。`Interface` メンバー・`MustOverride` は本体を持たない。`resolves_calls_by_simple_name = True`（C と同じ2段目の単純名解決）。

参照（`INVOKES`）:
- `Inherits X`／`Implements X` → `via=extends`。`Imports` は参照にせず、`.vb` の `RefResult.file_context`（`package`＝主体の `Namespace`・`imports`＝`Imports` の宣言順。名前空間の import は `wildcard`・`Imports A = N.T` は `alias`）として共通層へ渡す。VB6/VBA/VBScript は `file_context` を返さない。
- `.vbproj`（`RootNamespace`・プロジェクト全体の `Import`）は `_vb_project` が読み、共通層（`world_graph`）が当てはめる: Root Namespace は名前の照合のときだけ型・手続きの完全修飾名の頭に足し（ノードの識別子は変えない）、プロジェクトの Import は `file_context.imports` へ足す。VB6/VBA/VBScript には当てない。
- `New X(...)` → `via=call`。宣言型（`Dim x As T`・引数・戻り値）→ `via=field_type`（組み込み型は除く）。完全修飾トークンは `extra={"qualified": True}`。型名の参照（`Inherits`/`Implements`・`New`・`As`）には `type_ref` を付け、`.vb` では共通層が `file_context` の Namespace・`Imports` で解決する（親の Namespace→グローバル→`Imports` の順）。
- 手続き呼び出し（`Call Foo(`・`Foo(`・括弧なしの `Foo arg1, arg2`）→ `via=call`。`:` 区切りの文単位と単一行 `If ... Then <文>` の実行部も走査する。`MsgBox`・`Debug.Print` 等の組み込み手続きは除外する。
- `.frm`/`.ctl` のデザイナ部（`Begin ... End`）は読み飛ばす。
- 参照の始点（`source_symbol_id`）は、その行を含む最も内側の定義: `Sub`/`Function`/`Property`（`Declare` は宣言の行だけ）→ 主体以外のトップレベル型（`.vb`）の順。主体の型の直下・型の外はファイルの主体（省略）。キーは children の `cid_key`。
- `CreateObject`/`GetObject` → `Dropped("vb_late_bound")`、`CallByName`/`Application.Run` → `Dropped("vb_dynamic_call")`。
- SQL 文字列: 文字列リテラル（`&`／`_` 連結を含む・先頭が文字列リテラルの連鎖のみ）に `SELECT|INSERT|UPDATE|DELETE|MERGE` があれば、`_sql_scan` で `ACCESSES via="vba_sql"`（`Table`）を返す。テーブル名の途中が動的な候補は `Dropped("vba_sql_dynamic_table")`。

大文字小文字は区別しない（`identifiers.normalize_code_name` で定義・参照とも大文字化）。行継続 `_` は `_logical_lines` で論理行へ結合し、開始物理行番号を報告する。コメント・文字列は `_sanitize()`（構造走査用）と `_sanitize_comments_only()`（SQL 文字列抽出用）で空白化する。`#If` は評価せず両分岐を読む。
検出限界: `Property` のブロック判定は次行が単独の `Get` かの1行先読み。`x = Foo`（括弧なし）の戻り値代入・`With` 内の `.Foo`・イベント配線・単一行の `Sub()...End Sub` は対象外。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from . import _sql_scan
from ._base import Analyzer, DefItem, DefResult, Dropped, FileContext, ImportItem, RefCandidate, RefResult
from ..identifiers import normalize_code_name as _norm

VB_EXT = frozenset({".vb", ".bas", ".cls", ".frm", ".ctl", ".vbs"})

# 継続行（` _` 行末）・コメント（`'`／文頭の `REM`）・文字列リテラル（`"..."`・`""` エスケープ）

_CONTINUATION = re.compile(r'[ \t]_\s*$')


def _sanitize_generic(text: str, *, blank_strings: bool) -> str:
    """コメント（`'`・文の先頭の `REM`）を空白化し、`blank_strings` が真なら文字列リテラルの中身（と引用符）も空白化する。同じ長さ・同じ改行位置を保つ。"""
    out: list = []
    i, n = 0, len(text)
    stmt_start = True
    while i < n:
        ch = text[i]
        if ch == "\n":
            out.append("\n")
            i += 1
            stmt_start = True
            continue
        if ch in " \t":
            out.append(ch)
            i += 1
            continue
        if (stmt_start and text[i:i + 3].upper() == "REM"
                and (i + 3 >= n or not (text[i + 3].isalnum() or text[i + 3] == "_"))):
            nl = text.find("\n", i)
            end = nl if nl != -1 else n
            out.append(" " * (end - i))
            i = end
            continue
        if ch == "'":
            nl = text.find("\n", i)
            end = nl if nl != -1 else n
            out.append(" " * (end - i))
            i = end
            continue
        if ch == '"':
            out.append(" " if blank_strings else '"')
            i += 1
            while i < n:
                if text[i:i + 2] == '""':
                    out.append("  " if blank_strings else '""')
                    i += 2
                    continue
                if text[i] == '"':
                    break
                if text[i] == "\n":
                    out.append("\n")
                    i += 1
                    continue
                out.append(" " if blank_strings else text[i])
                i += 1
            if i < n and text[i] == '"':
                out.append(" " if blank_strings else '"')
                i += 1
            stmt_start = False
            continue
        if ch == ":":
            out.append(":")
            i += 1
            stmt_start = True
            continue
        out.append(ch)
        i += 1
        stmt_start = False
    return "".join(out)


def _sanitize(text: str) -> str:
    """構造走査用（コメント・文字列とも空白化）。"""
    return _sanitize_generic(text, blank_strings=True)


def _sanitize_comments_only(text: str) -> str:
    """SQL 文字列抽出用（コメントだけ空白化・文字列の中身は残す）。"""
    return _sanitize_generic(text, blank_strings=False)


def _logical_lines(sanitized: str, comments_blanked: str) -> list:
    """行継続（` _` 行末）で物理行を論理行へ結合する。`sanitized`（継続判定・構造走査用）と
    `comments_blanked`（SQL 文字列抽出用）を同じ行境界でグルーピングし、`(開始物理行番号,
    結合後sanitized, 結合後comments_blanked)` の列を返す。"""
    san_lines = sanitized.split("\n")
    cb_lines = comments_blanked.split("\n")
    out: list = []
    i, n = 0, len(san_lines)
    while i < n:
        start_line = i + 1
        san_parts: list = []
        cb_parts: list = []
        while True:
            san_line = san_lines[i]
            cb_line = cb_lines[i] if i < len(cb_lines) else ""
            m = _CONTINUATION.search(san_line)
            if m:
                san_parts.append(san_line[:m.start()])
                cb_parts.append(cb_line[:m.start()] if len(cb_line) >= m.start() else cb_line)
                i += 1
                if i >= n:
                    break
                continue
            san_parts.append(san_line)
            cb_parts.append(cb_line)
            i += 1
            break
        out.append((start_line, " ".join(san_parts), " ".join(cb_parts)))
    return out


# VB.NET: Namespace/Class/Module/Structure/Interface/Enum・Sub/Function/Property

_CONTAINER_OPEN = re.compile(
    r'^(?P<mods>(?:(?:Public|Private|Friend|Protected|MustInherit|NotInheritable|Partial)\s+)*)'
    r'(?P<kind>Namespace|Class|Module|Structure|Interface|Enum)\s+(?P<name>[A-Za-z_][\w.]*)', re.I)
_CONTAINER_CLOSE = re.compile(
    r'^End\s+(?P<kind>Namespace|Class|Module|Structure|Interface|Enum)\s*$', re.I)
_PROC_OPEN = re.compile(
    r'^(?P<mods>(?:(?:Public|Private|Friend|Protected|Static|Shared|Overrides|Overridable|'
    r'NotOverridable|MustOverride|Overloads|Shadows|ReadOnly|WriteOnly|Default|Async|Iterator)\s+)*)'
    r'(?P<kind>Sub|Function|Property)\s+(?:(?P<accessor>Get|Let|Set)\s+)?(?P<name>[A-Za-z_]\w*)', re.I)
_PROC_CLOSE = re.compile(r'^End\s+(?P<kind>Sub|Function|Property)\s*$', re.I)
_DECLARE = re.compile(
    r'^(?:(?:Public|Private|Friend)\s+)?Declare\s+(?:PtrSafe\s+)?(?P<kind>Sub|Function)\s+'
    r'(?P<name>[A-Za-z_]\w*)\s+Lib\b', re.I)
_ACCESSOR_BARE = re.compile(r'^(?:(?:Public|Private|Friend|Protected)\s+)?(?:Get\s*|Set\b.*)$', re.I)
_MUSTOVERRIDE = re.compile(r'\bMustOverride\b', re.I)


def _nearest_container(stack: list):
    """最も近い囲みの型（`Namespace` を除く）を `(kind, name, nested)` で返す（無ければ `None`）。`nested`＝そのコンテナ自身が入れ子（`vb_nested_type` として Dropped 済み）か。"""
    for frame in reversed(stack):
        if frame["frame"] == "container" and frame["kind"].lower() != "namespace":
            return frame["kind"], frame["name"], frame.get("nested", False)
    return None


def _scan_structure(logical: list) -> tuple:
    """`logical`（`_logical_lines` の戻り値）を状態機械で走査し、`(top_types, nested_dropped, procedures)` を返す。

    `top_types`＝`[{"kind","name","line","end","is_public","namespace"}, ...]`（トップレベル型のみ。VB6/VBA では常に空。`end`＝`End <型>` の行・閉じなければ最終行）。
    `procedures`＝`[{"kind","name","line","end","term","owner": (kind,name)|None, "namespace": 囲む Namespace|None}, ...]`（`owner`＝最も近い囲みの型。VB6/VBA は常に `None`。入れ子型の中の手続きは含まない。`end`＝`End Sub` 等の行・本体の無いものは `line`）。
    """
    stack: list = []
    namespace_stack: list = []
    top_types: list = []
    nested_dropped: list = []
    procedures: list = []
    n = len(logical)

    for idx, (line_no, san, _cb) in enumerate(logical):
        stripped = san.strip()
        if not stripped:
            continue

        if stack and stack[-1]["frame"] == "procedure":
            m_pclose = _PROC_CLOSE.match(stripped)
            if m_pclose and stack[-1]["kind"].lower() == m_pclose.group("kind").lower():
                popped = stack.pop()
                if popped["rec"] is not None:
                    popped["rec"]["end"] = line_no
            continue  # 手続き本体内は container/procedure を検知しない

        m_cclose = _CONTAINER_CLOSE.match(stripped)
        if m_cclose and stack and stack[-1]["frame"] == "container" \
                and stack[-1]["kind"].lower() == m_cclose.group("kind").lower():
            popped = stack.pop()
            if popped["kind"].lower() == "namespace":
                namespace_stack.pop()
            elif popped["rec"] is not None:
                popped["rec"]["end"] = line_no
            continue

        m_decl = _DECLARE.match(stripped)
        if m_decl:
            owner = _nearest_container(stack)
            if not (owner and owner[2]):
                owner_pair = (owner[0], owner[1]) if owner else None
                procedures.append({"kind": m_decl.group("kind"), "name": m_decl.group("name"),
                                   "line": line_no, "end": line_no, "term": "declaration", "owner": owner_pair,
                                   "namespace": _join_ns(namespace_stack)})
            continue

        m_copen = _CONTAINER_OPEN.match(stripped)
        if m_copen:
            kind = m_copen.group("kind")
            name = m_copen.group("name")
            if kind.lower() == "namespace":
                namespace_stack.append(_drop_global(name))
                stack.append({"frame": "container", "kind": "Namespace", "name": name,
                              "nested": False, "rec": None})
                continue
            depth = sum(1 for f in stack if f["frame"] == "container" and f["kind"].lower() != "namespace")
            is_public = bool(re.search(r'\bPublic\b', m_copen.group("mods"), re.I))
            is_nested = depth > 0
            ns = _join_ns(namespace_stack)
            rec = None
            if not is_nested:
                rec = {"kind": kind, "name": name, "line": line_no, "end": None, "is_public": is_public,
                       "namespace": ns}
                top_types.append(rec)
            else:
                nested_dropped.append(Dropped("vb_nested_type", line_no, name))
            stack.append({"frame": "container", "kind": kind, "name": name, "nested": is_nested, "rec": rec})
            continue

        m_popen = _PROC_OPEN.match(stripped)
        if m_popen:
            kind = m_popen.group("kind")
            name = m_popen.group("name")
            accessor = m_popen.group("accessor")
            mods = m_popen.group("mods")
            owner = _nearest_container(stack)
            owner_nested = bool(owner and owner[2])
            owner_pair = (owner[0], owner[1]) if owner else None
            no_body = bool(_MUSTOVERRIDE.search(mods)) or (owner is not None and owner[0].lower() == "interface")
            rec = None
            if not owner_nested:
                rec = {"kind": "Property" if kind.lower() == "property" else kind, "name": name,
                       "line": line_no, "end": line_no, "term": "definition", "owner": owner_pair,
                       "namespace": _join_ns(namespace_stack)}
                procedures.append(rec)
            if kind.lower() == "property":
                if accessor is not None:
                    push_frame = not no_body
                else:
                    nxt = logical[idx + 1][1].strip() if idx + 1 < n else ""
                    push_frame = (not no_body) and bool(_ACCESSOR_BARE.match(nxt))
                if push_frame:
                    stack.append({"frame": "procedure", "kind": "Property", "name": name, "rec": rec})
                continue
            if not no_body:
                stack.append({"frame": "procedure", "kind": kind, "name": name, "rec": rec})
            continue

    last_line = logical[-1][0] if logical else 0
    for t in top_types:
        if t["end"] is None:                              # 閉じない型は最終行まで
            t["end"] = last_line
    for frame in stack:
        if frame["frame"] == "procedure" and frame["rec"] is not None:
            frame["rec"]["end"] = last_line               # 閉じない手続きは最終行まで
    return top_types, nested_dropped, procedures


def _dedupe_children(children: list) -> list:
    """同一 `(label, cid_key)` の重複 child を1件に集約する（overload・`Property Get/Let/Set` 三つ組など）。`c_kind` は definition を declaration より優先する（返却順は初出のまま）。"""
    best: dict = {}
    order: list = []
    for child in children:
        key = (child.label, child.cid_key)
        prev = best.get(key)
        if prev is None:
            best[key] = child
            order.append(key)
            continue
        prev_kind = (prev.extra or {}).get("c_kind")
        cur_kind = (child.extra or {}).get("c_kind")
        if prev_kind == "declaration" and cur_kind == "definition":
            best[key] = child
    return [best[k] for k in order]


# VB6/VBA: primary 名（`Attribute VB_Name`／`Begin VB.Form`／ファイル名ステム）

_VB_NAME = re.compile(r'^\s*Attribute\s+VB_Name\s*=\s*"(?P<name>[^"]*)"', re.M | re.I)
_BEGIN_FORM = re.compile(r'^\s*Begin\s+VB\.Form\s+(?P<name>\w+)', re.M | re.I)


def _vb6_primary_name(text: str, rel_path: str) -> str:
    m = _VB_NAME.search(text)
    if m and m.group("name"):
        return m.group("name")
    m = _BEGIN_FORM.search(text)
    if m:
        return m.group("name")
    return PurePosixPath(rel_path).stem


# .frm/.ctl のデザイナ部（`Begin ... End`／`BeginProperty ... EndProperty`）の読み飛ばし

_DESIGN_BEGIN = re.compile(r'^(?:Begin|BeginProperty)\b', re.I)
_DESIGN_END = re.compile(r'^(?:End|EndProperty)\s*$', re.I)


# 参照抽出: Inherits/Implements・New・宣言型（As Type）・呼び出し・late/dynamic call

_INHERITS_OR_IMPLEMENTS = re.compile(
    r'^(?:Inherits|Implements)\s+(?P<name>[A-Za-z_][\w.]*)', re.I)
_NEW_EXPR = re.compile(r'\bNew\s+(?P<type>[A-Za-z_][\w.]*)', re.I)
_AS_TYPE = re.compile(r'\bAs\s+(?P<type>[A-Za-z_][\w.]*)', re.I)
_CALL_PAREN = re.compile(r'\b(?P<name>[A-Za-z_][\w.]*)\s*\(', re.I)
# `Imports N`／`Imports Alias = N.T`（`Imports <xmlns=...>` は識別子で始まらないので対象外）。
_IMPORTS = re.compile(r'^Imports\s+(?:(?P<alias>[A-Za-z_]\w*)\s*=\s*)?(?P<name>[A-Za-z_][\w.]*)\s*$', re.I)
_SINGLE_LINE_IF_THEN = re.compile(r'^If\b.*?\bThen\b\s*(?P<stmt>\S.*)$', re.I)

_BUILTIN_TYPES = frozenset({
    "STRING", "INTEGER", "LONG", "BOOLEAN", "OBJECT", "VARIANT", "DATE", "DOUBLE", "DECIMAL",
    "BYTE", "CHAR", "SHORT", "SINGLE",
})

_LATE_BOUND_NAMES = frozenset({"CREATEOBJECT", "GETOBJECT"})
_DYNAMIC_CALL_NAMES = frozenset({"CALLBYNAME"})

# 実行時 I/O・診断用の組み込み手続き（呼び出し先が定義として存在しないため参照にしない）。修飾形は完全一致のみ、無修飾は最後のセグメントで判定する。
_BUILTIN_PROC_QUALIFIED = frozenset({"DEBUG.PRINT", "ERR.RAISE"})
_BUILTIN_PROC_BARE = frozenset({
    "MSGBOX", "PRINT", "INPUT", "OPEN", "CLOSE", "KILL", "RANDOMIZE", "DOEVENTS", "BEEP",
})


def _is_builtin_proc(name: str) -> bool:
    upper = name.upper()
    if upper in _BUILTIN_PROC_QUALIFIED:
        return True
    return "." not in name and upper in _BUILTIN_PROC_BARE


_RESERVED_LEADERS = frozenset({
    "IF", "THEN", "ELSE", "ELSEIF", "END", "FOR", "EACH", "NEXT", "WHILE", "WEND", "DO", "LOOP",
    "SELECT", "CASE", "WITH", "TRY", "CATCH", "FINALLY", "THROW", "SYNCLOCK", "USING",
    "DIM", "CONST", "STATIC", "REDIM", "ERASE", "SET", "LET", "GET", "RETURN", "EXIT", "GOTO",
    "ON", "RESUME", "ERROR", "OPTION", "IMPORTS", "INHERITS", "IMPLEMENTS", "ATTRIBUTE",
    "BEGIN", "TYPE", "NAMESPACE", "CLASS", "MODULE", "STRUCTURE", "INTERFACE", "ENUM",
    "SUB", "FUNCTION", "PROPERTY", "DECLARE", "PUBLIC", "PRIVATE", "FRIEND", "PROTECTED",
    "SHARED", "SHADOWS", "OVERRIDES", "OVERRIDABLE", "NOTOVERRIDABLE", "MUSTOVERRIDE",
    "MUSTINHERIT", "NOTINHERITABLE", "PARTIAL", "OVERLOADS", "READONLY", "WRITEONLY",
    "DEFAULT", "ASYNC", "ITERATOR", "NEW", "RAISEEVENT", "ADDHANDLER", "REMOVEHANDLER",
    "HANDLES", "REM", "CALL", "OPTIONAL", "BYVAL", "BYREF", "PARAMARRAY",
    "AS", "TO", "STEP", "IN", "IS", "ISNOT", "NOT", "AND", "OR", "XOR", "MOD", "LIKE",
    "TRUE", "FALSE", "NOTHING", "ME", "MYBASE", "MYCLASS", "GETTYPE", "CTYPE", "DIRECTCAST",
    "TRYCAST", "TYPEOF", "PRESERVE", "VERSION",
})

# 定義ヘッダ行（container/procedure open・close・Declare）は呼び出し走査から除外する。
_HEADER_LIKE = (_CONTAINER_OPEN, _CONTAINER_CLOSE, _PROC_OPEN, _PROC_CLOSE, _DECLARE,
               _INHERITS_OR_IMPLEMENTS)


def _is_header_like(stripped: str) -> bool:
    return any(p.match(stripped) for p in _HEADER_LIKE)


def _drop_global(name: str) -> str:
    """名前の先頭の `Global.`（ルート名前空間の指定）を外す。`Global` だけならルート＝空文字。"""
    if name.upper() == "GLOBAL":
        return ""
    return name[7:] if name.upper().startswith("GLOBAL.") else name


def _join_ns(stack: list):
    """Namespace の入れ子（`Global` は外してある）を `A.B` へつなぐ。ルートだけなら `None`。"""
    return ".".join(s for s in stack if s) or None


def _emit_ref(refs: list, name: str, line: int, via: str, type_ref: bool = False) -> None:
    name = _drop_global(name)
    if not name:
        return
    normalized = _norm(name)
    extra = {"via": via}
    if type_ref:
        extra["type_ref"] = True
    if "." in name:
        extra["qualified"] = True
    refs.append(RefCandidate("INVOKES", "Module", normalized, line, extra=extra))


def _dynamic_call_snippet(comments_blanked_line: str) -> str:
    return comments_blanked_line.strip()[:120]


_CREATE_OBJECT_ARG = re.compile(r'CreateObject\s*\(\s*"(?P<progid>(?:""|[^"])*)"', re.I)
_GET_OBJECT_ARG = re.compile(r'GetObject\s*\((?P<args>[^)]*)', re.I)


def _late_bound_progid(name_upper: str, cb_line: str, start: int) -> str:
    if name_upper == "CREATEOBJECT":
        m = _CREATE_OBJECT_ARG.match(cb_line, start)
        if m:
            return m.group("progid").replace('""', '"')
        return ""
    m = _GET_OBJECT_ARG.match(cb_line, start)
    if m:
        return m.group("args").strip()
    return ""


def _then_statement(segment: str) -> str:
    """単一行 `If 条件 Then <文>` の実行部だけを取り出す（マッチしなければそのまま返す。複数行 If のヘッダ行は `_RESERVED_LEADERS` の `IF` 除外に任せる）。"""
    m = _SINGLE_LINE_IF_THEN.match(segment)
    return m.group("stmt").strip() if m else segment


def _scan_bareword_statement(stmt: str, cb_line: str, line_no: int, refs: list, dropped: list) -> None:
    """括弧を伴わない1文（`Call Foo`／`Foo arg1, arg2`）を呼び出し参照へ変換する。"""
    m = re.match(r'^(?:Call\s+)?(?P<name>[A-Za-z_][\w.]*)(?:\s+(?P<rest>\S.*))?$', stmt, re.I)
    if not m:
        return
    name = m.group("name")
    first_seg = name.split(".", 1)[0].upper()
    if first_seg in _RESERVED_LEADERS:
        return
    rest = m.group("rest")
    if rest and rest.lstrip().startswith(("=", "<", ">")):
        return
    last_seg = name.rsplit(".", 1)[-1].upper()
    if name.upper() == "APPLICATION.RUN" or last_seg in _DYNAMIC_CALL_NAMES:
        dropped.append(Dropped("vb_dynamic_call", line_no, _dynamic_call_snippet(cb_line)))
        return
    if last_seg in _LATE_BOUND_NAMES:
        # `CreateObject`/`GetObject` は戻り値を使う関数呼び出しのため、括弧なしの形は通常の call にせず黙って見逃す（検出限界）。
        return
    if _is_builtin_proc(name):
        return
    _emit_ref(refs, name, line_no, "call")


def _scan_call_refs(san_line: str, cb_line: str, line_no: int, refs: list, dropped: list) -> None:
    """1論理行分の呼び出し系参照（`New`／宣言型／`Inherits`/`Implements`／通常呼び出し／括弧なし呼び出し／late-bound・dynamic call の Dropped 化）を抽出する。"""
    stripped = san_line.strip()
    if not stripped:
        return

    for m in _INHERITS_OR_IMPLEMENTS.finditer(stripped):
        _emit_ref(refs, m.group("name"), line_no, "extends", type_ref=True)

    for m in _AS_TYPE.finditer(san_line):
        type_token = m.group("type")
        simple = type_token.rsplit(".", 1)[-1].upper()
        if simple in _BUILTIN_TYPES:
            continue
        _emit_ref(refs, type_token, line_no, "field_type", type_ref=True)

    new_starts = set()
    for m in _NEW_EXPR.finditer(san_line):
        new_starts.add(m.start("type"))
        _emit_ref(refs, m.group("type"), line_no, "call", type_ref=True)

    if _is_header_like(stripped):
        return

    for m in _CALL_PAREN.finditer(san_line):
        name = m.group("name")
        if m.start("name") in new_starts:
            continue  # `New X(...)` の型名と二重計上しない
        last_seg = name.rsplit(".", 1)[-1].upper()
        first_seg = name.split(".", 1)[0].upper()
        if first_seg in _RESERVED_LEADERS or last_seg in _RESERVED_LEADERS:
            continue
        if last_seg in _LATE_BOUND_NAMES:
            progid = _late_bound_progid(last_seg, cb_line, m.start())
            dropped.append(Dropped("vb_late_bound", line_no, progid))
            continue
        if last_seg in _DYNAMIC_CALL_NAMES or name.upper() == "APPLICATION.RUN":
            dropped.append(Dropped("vb_dynamic_call", line_no, _dynamic_call_snippet(cb_line)))
            continue
        if _is_builtin_proc(name):
            continue
        _emit_ref(refs, name, line_no, "call")

    # 括弧なし呼び出し: コメント除去済みの文を `:` で区切り、文ごとに走査する（単一行 `If 条件 Then <文>` の実行部も含む）。
    for segment in stripped.split(":"):
        seg = segment.strip()
        if not seg:
            continue
        stmt = _then_statement(seg)
        if not stmt or "(" in stmt:
            continue
        _scan_bareword_statement(stmt, cb_line, line_no, refs, dropped)


# SQL 文字列（文字列リテラル、または `&`/`_` 継続で連結された文字列）

_SQL_KEYWORD = re.compile(r'\b(?:SELECT|INSERT|UPDATE|DELETE|MERGE)\b', re.I)
_CONCAT_CHAIN = re.compile(
    r'"(?:""|[^"])*"(?:\s*&\s*(?:"(?:""|[^"])*"|[A-Za-z_][\w.]*|\d+(?:\.\d+)?))*')
_CONCAT_TOKEN = re.compile(r'"(?:""|[^"])*"|[A-Za-z_][\w.]*|\d+(?:\.\d+)?')


def _sql_refs_from_line(cb_line: str, line_no: int, dropped: list) -> list:
    refs: list = []
    for m in _CONCAT_CHAIN.finditer(cb_line):
        parts: list = []
        for tok_m in _CONCAT_TOKEN.finditer(m.group(0)):
            tok = tok_m.group(0)
            if tok.startswith('"'):
                parts.append(tok[1:-1].replace('""', '"'))
            else:
                parts.append("?")
        combined = "".join(parts)
        if not _SQL_KEYWORD.search(combined):
            continue
        sanitized_sql = _sql_scan.sanitize(combined)
        for name, offset in _sql_scan.table_refs(sanitized_sql):
            end = offset + len(name)
            if end < len(combined) and combined[end] == "?":
                # 識別子の途中に動的な置換（`?`）が隣接する候補は実テーブル名を復元できないため、解決せず申告するだけにする。
                dropped.append(Dropped("vba_sql_dynamic_table", line_no, combined.strip()[:120]))
                continue
            refs.append(RefCandidate("ACCESSES", "Table", name, line_no, extra={"via": "vba_sql"}))
    return refs


def _type_key(t: dict) -> str:
    """トップレベル型の定義キー（`DefItem.key`＝cid の材料）。namespace があれば `Namespace.Type`。"""
    return _norm(f"{t['namespace']}.{t['name']}") if t["namespace"] else _norm(t["name"])


def _procedure_key(p: dict, default_owner_raw: str) -> str:
    """手続きの定義キー（`<囲みの型>.<手続き名>`）。囲みの型が無ければ `default_owner_raw`（主体名）。"""
    owner_name = p["owner"][1] if p["owner"] else default_owner_raw
    ns = p.get("namespace")
    return _norm(f"{ns}.{owner_name}.{p['name']}" if ns else f"{owner_name}.{p['name']}")


def _owner_ranges(ext: str, text: str, rel_path: str, top_types: list, procedures: list) -> list:
    """参照の始点を決める範囲の一覧 `(start_line, end_line, key)`。

    手続き（`Sub`/`Function`/`Property`・`Declare`）と、主体以外のトップレベル型（`.vb` のみ）。主体の型・手続きと型の外は範囲に入れない（＝ファイルの主体）。
    キーは `collect_defs` が返す children の `cid_key` と同じ（`_type_key`・`_procedure_key`）。
    """
    if ext == ".vb":
        if not top_types:
            return []
        primary_idx = next((i for i, t in enumerate(top_types) if t["is_public"]), 0)
        default_owner_raw = top_types[primary_idx]["name"]
        ranges = [(t["line"], t["end"], _type_key(t)) for i, t in enumerate(top_types) if i != primary_idx]
    else:
        default_owner_raw = _vb6_primary_name(text, rel_path)
        ranges = []
    ranges.extend((p["line"], p["end"], _procedure_key(p, default_owner_raw)) for p in procedures)
    return ranges


def _innermost_owner(ranges: list, line: int):
    """`line` を含む範囲のうち最も内側（開始行が最大・同じなら終了行が最小）のキー。無ければ `None`。"""
    best = None
    for start, end, key in ranges:
        if start <= line <= end and (best is None or (start, -end) > (best[0], -best[1])):
            best = (start, end, key)
    return None if best is None else best[2]


class VbAnalyzer(Analyzer):
    """VB.NET・VB6/VBA エクスポート・VBScript。全件受理（方言差は拡張子で内部分岐）。"""

    name = "vb"
    extensions = VB_EXT
    resolves_calls_by_simple_name = True
    resolves_parent_namespaces = True
    doctype = "vb"
    version = 4

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        ext = PurePosixPath(rel_path).suffix.lower()
        sanitized = _sanitize(text)
        comments_blanked = _sanitize_comments_only(text)
        logical = _logical_lines(sanitized, comments_blanked)
        top_types, nested_dropped, procedures = _scan_structure(logical)
        dropped = list(nested_dropped)

        if ext == ".vb":
            if not top_types:
                return DefResult(dropped=dropped)
            primary_idx = next((i for i, t in enumerate(top_types) if t["is_public"]), 0)
            primary_t = top_types[primary_idx]
            primary_name = _norm(primary_t["name"])
            primary_cid_key = (_norm(f"{primary_t['namespace']}.{primary_t['name']}")
                               if primary_t["namespace"] else None)
            primary = DefItem(label="Module", name=primary_name, cid_key=primary_cid_key,
                              qualified=_type_key(primary_t))
            children: list = []
            for i, t in enumerate(top_types):
                if i == primary_idx:
                    continue
                children.append(DefItem(label="Module", name=_norm(t["name"]), cid_key=_type_key(t),
                                        line=t["line"]))
            for p in procedures:
                pname = _norm(p["name"])
                cid_key = _procedure_key(p, primary_t["name"])
                children.append(DefItem(label="Module", name=pname, cid_key=cid_key, line=p["line"],
                                        extra={"c_kind": p["term"]}))
            return DefResult(primary=primary, children=_dedupe_children(children), dropped=dropped)

        # VB6/VBA/VBScript: ファイル自体が primary（Namespace/Class ブロック構文が無い）。
        primary_name_raw = _vb6_primary_name(text, rel_path)
        primary_name = _norm(primary_name_raw)
        primary = DefItem(label="Module", name=primary_name, qualified=primary_name)   # VB6 の型はグローバル名前空間
        children = []
        for p in procedures:
            pname = _norm(p["name"])
            cid_key = _procedure_key(p, primary_name_raw)
            children.append(DefItem(label="Module", name=pname, cid_key=cid_key, line=p["line"],
                                    extra={"c_kind": p["term"]}))
        return DefResult(primary=primary, children=_dedupe_children(children), dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        comments_blanked = _sanitize_comments_only(text)
        logical = _logical_lines(sanitized, comments_blanked)
        top_types, _nested, procedures = _scan_structure(logical)
        ranges = _owner_ranges(PurePosixPath(rel_path).suffix.lower(), text, rel_path, top_types, procedures)
        refs: list = []
        dropped: list = []
        design_depth = 0
        for line_no, san, cb in logical:
            stripped = san.strip()
            if design_depth > 0:
                if _DESIGN_END.match(stripped):
                    design_depth -= 1
                elif _DESIGN_BEGIN.match(stripped):
                    design_depth += 1
                continue
            if _DESIGN_BEGIN.match(stripped):
                design_depth += 1
                continue
            _scan_call_refs(san, cb, line_no, refs, dropped)
            refs.extend(_sql_refs_from_line(cb, line_no, dropped))
        for ref in refs:
            key = _innermost_owner(ranges, ref.line)
            if key is not None:
                ref.source_symbol_id = (rel_path, key)
        file_context = None
        if PurePosixPath(rel_path).suffix.lower() == ".vb":
            imports = []
            for line_no, san, _cb in logical:
                m = _IMPORTS.match(san.strip())
                if m and _drop_global(m.group("name")):
                    imports.append(ImportItem(kind="alias" if m.group("alias") else "wildcard",
                                              name=_norm(_drop_global(m.group("name"))),
                                              alias=_norm(m.group("alias")) if m.group("alias") else None,
                                              line=line_no))
            primary_ns = None
            if top_types:
                primary_t = top_types[next((i for i, t in enumerate(top_types) if t["is_public"]), 0)]
                primary_ns = _norm(primary_t["namespace"]) if primary_t["namespace"] else None
            namespaces = [(t["line"], t["end"], _norm(t["namespace"]) if t["namespace"] else None) for t in top_types]
            file_context = FileContext(package=primary_ns, imports=imports, namespaces=namespaces)
        return RefResult(refs=refs, dropped=dropped, file_context=file_context)
