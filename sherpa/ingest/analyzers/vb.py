"""VB アナライザ（docs/proposals/2026-09-05-アナライザ拡張.md §9 波3 レーン C・ユーザー裁定
2026-09-06）。VB.NET（`.vb`）・VB6/VBA エクスポート（`.bas`/`.cls`/`.frm`/`.ctl`）・VBScript
（`.vbs`）を1本のアナライザで扱う（1種別1本・方言差は `accepts` ではなく拡張子で内部分岐する）。

**主体**: VB.NET は `Namespace X` 配下の `Class/Module/Structure/Interface/Enum`
（public または最初のトップレベル型を primary・qualified `cid_key="Namespace.Type"`・他の型は
children）。`Namespace` はネスト・複数出現に対応する（スタックで管理し、その時点で有効な
namespace で各トップレベル型を修飾する——ファイル内に複数の独立した `Namespace` ブロックが
あっても、後方のブロックの型が前方のブロックの namespace を引き継がない）。VB6/VBA は
`Attribute VB_Name = "..."` の名前（`.frm` は無ければ `Begin VB.Form Name` の名前・どちらも
無ければファイル名ステム）を primary（`Module`）とする——VB6/VBA には `Class`/`Module`
キーワードのブロック構文自体が無い（ファイル自体が1個の型）。

**children**: `Sub`/`Function`/`Property`（自動実装の単一行プロパティは block を持たない・
`Get` 行が直後に続く形、または VB6/VBA の `Property Get/Let/Set`（accessor 語の後が本来の名前・
本体は必ず `End Property` まで続く）だけを block として扱う）は修飾名 `<Type>.<Name>`（VB6/VBA
は `<primary名>.<Name>`）を `cid_key` に持つ children（C の `<ファイル名>.<関数名>` と同型）。
`Interface` メンバー・`MustOverride` 修飾子付きメンバーは本体を持たない（`End Sub/Function` を
待たない）ため frame を積まない——直後の別メンバー宣言が同じ手続きの内側に飲み込まれない。
入れ子の型（VB.NET の `Class` の中の `Class` 等）は `Dropped("vb_nested_type", ...)` として
申告するだけで、型自体はもちろんその中の手続きも children にはしない。同一 `(label, cid_key,
c_kind)` の重複（overload・VB6 の `Property Get/Let/Set` 三つ組）は1件へ集約する（definition を
declaration より優先）。`extra["c_kind"]` は本体を持つ通常形なら `"definition"`、`Declare
Function/Sub ... Lib` なら `"declaration"`（宣言だけで実体を持たない・C のプロトタイプ宣言と
同型）。`resolves_calls_by_simple_name = True` により、`via=call` の単純名参照は world_graph 側の
2段目（`simple_name_defs`）で同一 top_scope 内の children へ解決される（§9・C と同じ仕組みを流用）。

**参照**: `Inherits X`／`Implements X` → `INVOKES via=extends`（C# と同じ統一・`implements` は
使わない）。`Imports A.B` はヒントのみ（参照にしない）。`New X(...)`／`New X`（`Dim x As New X`
を含む）→ `via=call`。宣言型（`Dim x As T`／引数／戻り値／`Private WithEvents x As T`）→
`via=field_type`（組み込み型 `String/Integer/Long/Boolean/Object/Variant/Date/Double/Decimal/
Byte/Char/Short/Single` は除外）。`.` を含む完全修飾トークン（base list／宣言型／`New`
いずれも）は `extra={"qualified": True}` を付け、共通層の完全修飾名2段解決
（cid_key 完全一致→無ければ単純名フォールバック）に渡す（C# の `_emit_type_ref` と同型）。
手続き呼び出し `Call Foo(`／`Foo(`／`Foo arg1, arg2`（VB 固有の括弧なし呼び出し）→ `via=call`
（単純名。2段目解決）。コメント除去済みの1論理行を `:` で区切った文単位で走査するため
`Foo: Bar` の両方、および単一行 `If 条件 Then <文>` の実行部（`<文>`）も検出する。組み込み手続き
（`MsgBox`／`Print`／`Input`／`Open`／`Close`／`Kill`／`Randomize`／`DoEvents`／`Beep`・
`Debug.Print`／`Err.Raise`）は除外リストで参照にしない（実行時のI/O・診断ステートメントで
呼び出し先が定義として存在しないため）。自ファイル内の手続き呼び出しも除外しない（primary と
children の cid は常に異なるため自己ループにはならない・C と同じ理由）。

`.frm`/`.ctl` のデザイナ部（`Begin ... End`・`BeginProperty ... EndProperty` を含む・入れ子可）は
対応する Begin/End で丸ごと読み飛ばす（プロパティ値行 `Caption = "Main"` やネストした
`BeginProperty Font` を誤って呼び出しと判定しない）——ファイル冒頭からトップレベルの `End` まで
がデザイナ部、それ以降（`Attribute`・手続き本体）だけが通常の参照走査の対象になる。

`CreateObject("ProgID")`／`GetObject(...)` → `Dropped("vb_late_bound", line, progid)`。
`CallByName(...)`／`Application.Run "X"` → `Dropped("vb_dynamic_call", line, snippet)`
（いずれも実行時にしか呼び出し先が決まらない動的呼び出しのため解決しない）。

**SQL 文字列**（VBA/VB6 の ADO/DAO で頻出）: 文字列リテラル、または `&`（同一論理行）／`_`
（行継続）で連結された文字列リテラルの並びに `SELECT|INSERT|UPDATE|DELETE|MERGE` が含まれる場合、
連結後の文字列（変数/数値部分は `?` に置換）を `_sql_scan.sanitize`→`table_refs` にかけ
`ACCESSES via="vba_sql"`（`Table`）を返す（`vba_sql` は `_base.KNOWN_VIA`/`VIA_PRIORITY` の既知
via・波3 統合で追加）。連結の判定は**先頭が文字列リテラルの連鎖**
（`"..."（& (文字列|識別子|数値)）*`）だけを対象にする——変数始まりの連結（`x & "SELECT..."`）は
検出しない（安全側の限界）。テーブル識別子の途中に動的な置換（`?`）が隣接する候補（`"...FROM
ORD" & suffix` → `"...FROM ORD?"`）は実際のテーブル名を復元できないため
`Dropped("vba_sql_dynamic_table", line, snippet)` として申告するだけで解決しない（`"FROM " & tbl`
のように識別子全体が `?` に置き換わる形は `table_refs` がそもそも候補として拾わないため、
未解決のまま＝現状どおり）。

大文字小文字は区別しない（`identifiers.normalize_code_name` で定義・参照とも大文字化・COBOL と
同じ規則）。完全修飾名（`Namespace.Type`・`<Type>.<Name>`）も同じ関数で丸ごと大文字化する
（内部の `.` はそのまま・`normalize_code_name` は前後空白/末尾ドット除去＋大文字化のみ）。VB は
識別子を大文字化するため、C#/Java の**全大文字で定義された**型名（例 `API`）とは同世代なら
一致し得る（表記が一致するものだけ）——「同一世代のパス最近傍・完全一致」の既存契約の範囲として
許容する（言語ドメインで索引を分けない）。

行継続 `_`（行末の空白+アンダースコア）は物理行を1つの論理行へ結合してから走査する
（`_logical_lines`）——結合後の論理行1本につき「開始物理行番号」を1つだけ報告する（COBOL の
論理行と同じ粒度・複数物理行にまたがる構文でも位置合わせのために文字オフセットまでは追わない）。
コメント（`'`・文の先頭の `REM`（単語境界必須・`RemoveHandler` 等を誤認しない））・文字列リテラル
（`"..."`・`""` エスケープ）は `_sanitize()`（構造走査用・コメント/文字列とも空白化）／
`_sanitize_comments_only()`（SQL 文字列抽出用・コメントだけ空白化し文字列は残す）の2枚を使い分ける。
`#If ... #End If`（条件コンパイル）は指令行自体を特別扱いせず、両分岐とも通常のコードとして
読む（`#` 始まりの指令キーワードは本アナライザが認識するどの語彙とも一致しないため自然に無視
される・条件自体は評価しない＝限界として明記）。

**検出限界**: `Property` の自動実装／ブロック判定（VB.NET 形）は次行が `Get`（単独行）かどうかの
1行先読みに依存する（複雑な属性行を挟む形は見逃す）。手続き呼び出しはパース対象の文に丸括弧が
1つも無い場合だけ「括弧なし呼び出し」として扱う——式の途中に埋め込まれた戻り値呼び出し
（`x = Foo(1)` のような代入式の中の呼び出しは丸括弧経路で拾うが、`x = Foo`（括弧なし・関数の
戻り値を変数へ代入する形）は代入文と区別できないため検出しない。`With` ブロック内の暗黙メンバ
アクセス（`.Foo`）・イベント配線（`Handles`／`AddHandler`）自体の解決・単一行の `Sub()...End Sub`
形コロン区切り複文は対象外（安全側の見逃し）。
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from . import _sql_scan
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult
from ..identifiers import normalize_code_name as _norm

VB_EXT = frozenset({".vb", ".bas", ".cls", ".frm", ".ctl", ".vbs"})

# --- 継続行（` _` 行末）・コメント（`'`／文頭の `REM`）・文字列リテラル（`"..."`・`""` エスケープ）---

_CONTINUATION = re.compile(r'[ \t]_\s*$')


def _sanitize_generic(text: str, *, blank_strings: bool) -> str:
    """コメント（`'`・文の先頭の `REM`）を空白化し、`blank_strings` が真なら文字列リテラルの
    中身（と引用符自体）も空白化する。同じ長さ・同じ改行位置を保つ（偽マッチ除外専用）。"""
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


# --- VB.NET: Namespace/Class/Module/Structure/Interface/Enum・Sub/Function/Property ---

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
_GET_BARE = re.compile(r'^(?:(?:Public|Private|Friend|Protected)\s+)?Get\s*$', re.I)
_MUSTOVERRIDE = re.compile(r'\bMustOverride\b', re.I)


def _nearest_container(stack: list):
    """最も近い囲みの型（`Namespace` を除く）を `(kind, name, nested)` で返す（無ければ
    `None`）。`nested`＝そのコンテナ自身が入れ子（`vb_nested_type` として Dropped 済み）か。"""
    for frame in reversed(stack):
        if frame["frame"] == "container" and frame["kind"].lower() != "namespace":
            return frame["kind"], frame["name"], frame.get("nested", False)
    return None


def _scan_structure(logical: list) -> tuple:
    """`logical`（`_logical_lines` の戻り値）を状態機械で走査し、
    `(top_types, nested_dropped, procedures)` を返す。

    `top_types`＝`[{"kind","name","line","is_public","namespace"}, ...]`（各時点で有効な
    namespace スタックを結合した文字列|None を個別に持つ・トップレベル型のみ。VB6/VBA では
    `Namespace`/`Class` キーワード自体が現れないため常に空）。
    `procedures`＝`[{"kind","name","line","term","owner": (kind,name)|None}, ...]`
    （`owner`＝最も近い囲みの型。VB6/VBA は常に `None`＝primary 直下という意味で扱う。
    入れ子型の中の手続きはここに現れない——`owner_nested` の時点で除外する）。
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
                stack.pop()
            continue                                      # 手続き本体内は container/procedure 検知しない

        m_cclose = _CONTAINER_CLOSE.match(stripped)
        if m_cclose and stack and stack[-1]["frame"] == "container" \
                and stack[-1]["kind"].lower() == m_cclose.group("kind").lower():
            popped = stack.pop()
            if popped["kind"].lower() == "namespace":
                namespace_stack.pop()
            continue

        m_decl = _DECLARE.match(stripped)
        if m_decl:
            owner = _nearest_container(stack)
            if not (owner and owner[2]):                  # 入れ子型内の宣言は children にしない
                owner_pair = (owner[0], owner[1]) if owner else None
                procedures.append({"kind": m_decl.group("kind"), "name": m_decl.group("name"),
                                   "line": line_no, "term": "declaration", "owner": owner_pair})
            continue

        m_copen = _CONTAINER_OPEN.match(stripped)
        if m_copen:
            kind = m_copen.group("kind")
            name = m_copen.group("name")
            if kind.lower() == "namespace":
                namespace_stack.append(name)
                stack.append({"frame": "container", "kind": "Namespace", "name": name,
                              "nested": False})
                continue
            depth = sum(1 for f in stack if f["frame"] == "container" and f["kind"].lower() != "namespace")
            is_public = bool(re.search(r'\bPublic\b', m_copen.group("mods"), re.I))
            is_nested = depth > 0
            ns = ".".join(namespace_stack) if namespace_stack else None
            if not is_nested:
                top_types.append({"kind": kind, "name": name, "line": line_no, "is_public": is_public,
                                  "namespace": ns})
            else:
                nested_dropped.append(Dropped("vb_nested_type", line_no, name))
            stack.append({"frame": "container", "kind": kind, "name": name, "nested": is_nested})
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
            if kind.lower() == "property":
                if accessor is not None:                  # VB6/VBA の `Property Get/Let/Set`
                    push_frame = not no_body
                else:                                      # VB.NET: 次行が単独の `Get` なら full block
                    nxt = logical[idx + 1][1].strip() if idx + 1 < n else ""
                    push_frame = (not no_body) and bool(_GET_BARE.match(nxt))
                if push_frame:
                    stack.append({"frame": "procedure", "kind": "Property", "name": name})
                if not owner_nested:
                    procedures.append({"kind": "Property", "name": name, "line": line_no,
                                       "term": "definition", "owner": owner_pair})
                continue
            if not no_body:
                stack.append({"frame": "procedure", "kind": kind, "name": name})
            if not owner_nested:
                procedures.append({"kind": kind, "name": name, "line": line_no,
                                   "term": "definition", "owner": owner_pair})
            continue

    return top_types, nested_dropped, procedures


def _dedupe_children(children: list) -> list:
    """同一 `(label, cid_key)` の重複 child を1件に集約する（overload・VB6 の
    `Property Get/Let/Set` 三つ組など）。`c_kind` は definition を declaration より優先する
    （返却順は初出のまま保つ）。"""
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


# --- VB6/VBA: primary 名（`Attribute VB_Name`／`Begin VB.Form`／ファイル名ステム）---

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


# --- .frm/.ctl のデザイナ部（`Begin ... End`／`BeginProperty ... EndProperty`）の読み飛ばし ---

_DESIGN_BEGIN = re.compile(r'^(?:Begin|BeginProperty)\b', re.I)
_DESIGN_END = re.compile(r'^(?:End|EndProperty)\s*$', re.I)


# --- 参照抽出: Inherits/Implements・New・宣言型（As Type）・呼び出し・late/dynamic call ---

_INHERITS_OR_IMPLEMENTS = re.compile(
    r'^(?:Inherits|Implements)\s+(?P<name>[A-Za-z_][\w.]*)', re.I)
_NEW_EXPR = re.compile(r'\bNew\s+(?P<type>[A-Za-z_][\w.]*)', re.I)
_AS_TYPE = re.compile(r'\bAs\s+(?P<type>[A-Za-z_][\w.]*)', re.I)
_CALL_PAREN = re.compile(r'\b(?P<name>[A-Za-z_][\w.]*)\s*\(', re.I)
_SINGLE_LINE_IF_THEN = re.compile(r'^If\b.*?\bThen\b\s*(?P<stmt>\S.*)$', re.I)

_BUILTIN_TYPES = frozenset({
    "STRING", "INTEGER", "LONG", "BOOLEAN", "OBJECT", "VARIANT", "DATE", "DOUBLE", "DECIMAL",
    "BYTE", "CHAR", "SHORT", "SINGLE",
})

_LATE_BOUND_NAMES = frozenset({"CREATEOBJECT", "GETOBJECT"})
_DYNAMIC_CALL_NAMES = frozenset({"CALLBYNAME"})

# 実行時 I/O・診断用の組み込み手続き——呼び出し先が定義として存在しないため参照にしない。
# 修飾形（`Debug.Print`/`Err.Raise`）は完全一致のみ除外し、無修飾の単語は最後のセグメントで判定する。
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


def _emit_ref(refs: list, name: str, line: int, via: str) -> None:
    normalized = _norm(name)
    extra = {"via": via}
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
    """単一行 `If 条件 Then <文>` の実行部だけを取り出す（マッチしなければそのまま返す——複数行
    If ブロックのヘッダ行はここでは変換されず、`_RESERVED_LEADERS` の `IF` 除外に任せる）。"""
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
        return                                              # 代入/比較文（宣言の一部）と誤認しない
    last_seg = name.rsplit(".", 1)[-1].upper()
    if name.upper() == "APPLICATION.RUN" or last_seg in _DYNAMIC_CALL_NAMES:
        dropped.append(Dropped("vb_dynamic_call", line_no, _dynamic_call_snippet(cb_line)))
        return
    if last_seg in _LATE_BOUND_NAMES:
        # `CreateObject`/`GetObject` は戻り値を使う関数呼び出しのため括弧を伴わない形は実在しない
        # ——万一現れても progid が取れないため通常の call にはせず黙って見逃す（検出限界）。
        return
    if _is_builtin_proc(name):
        return
    _emit_ref(refs, name, line_no, "call")


def _scan_call_refs(san_line: str, cb_line: str, line_no: int, refs: list, dropped: list) -> None:
    """1論理行分の呼び出し系参照（`New`／宣言型／`Inherits`/`Implements`／通常呼び出し／
    括弧なし呼び出し／late-bound・dynamic call の Dropped 化）を抽出する。"""
    stripped = san_line.strip()
    if not stripped:
        return

    for m in _INHERITS_OR_IMPLEMENTS.finditer(stripped):
        _emit_ref(refs, m.group("name"), line_no, "extends")

    for m in _AS_TYPE.finditer(san_line):
        type_token = m.group("type")
        simple = type_token.rsplit(".", 1)[-1].upper()
        if simple in _BUILTIN_TYPES:
            continue
        _emit_ref(refs, type_token, line_no, "field_type")

    new_starts = set()
    for m in _NEW_EXPR.finditer(san_line):
        new_starts.add(m.start("type"))
        _emit_ref(refs, m.group("type"), line_no, "call")

    if _is_header_like(stripped):
        return                                            # 定義ヘッダ自身は呼び出しとして扱わない

    for m in _CALL_PAREN.finditer(san_line):
        name = m.group("name")
        if m.start("name") in new_starts:
            continue                                       # `New X(...)` の型名と二重計上しない
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

    # 括弧なし呼び出し: コメント除去済みの文を `:` で区切り、文ごとに走査する（単一行
    # `If 条件 Then <文>` の実行部も対象に含める）。
    for segment in stripped.split(":"):
        seg = segment.strip()
        if not seg:
            continue
        stmt = _then_statement(seg)
        if not stmt or "(" in stmt:
            continue
        _scan_bareword_statement(stmt, cb_line, line_no, refs, dropped)


# --- SQL 文字列（文字列リテラル、または `&`/`_` 継続で連結された文字列）---

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
                # 識別子の途中に動的な置換（`?`）が隣接する候補（`ORD` & suffix → `"...ORD?"`）は
                # 実テーブル名を復元できないため解決せず申告するだけにする。
                dropped.append(Dropped("vba_sql_dynamic_table", line_no, combined.strip()[:120]))
                continue
            refs.append(RefCandidate("ACCESSES", "Table", name, line_no, extra={"via": "vba_sql"}))
    return refs


class VbAnalyzer(Analyzer):
    """VB.NET（`.vb`）・VB6/VBA エクスポート（`.bas`/`.cls`/`.frm`/`.ctl`）・VBScript（`.vbs`）。
    全件受理（方言差は拡張子で内部分岐・1種別1本）。"""

    name = "vb"
    extensions = VB_EXT
    resolves_calls_by_simple_name = True
    doctype = "vb"

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
            primary = DefItem(label="Module", name=primary_name, cid_key=primary_cid_key)
            children: list = []
            for i, t in enumerate(top_types):
                if i == primary_idx:
                    continue
                nm = _norm(t["name"])
                qk = _norm(f"{t['namespace']}.{t['name']}") if t["namespace"] else nm
                children.append(DefItem(label="Module", name=nm, cid_key=qk, line=t["line"]))
            for p in procedures:
                owner_name = p["owner"][1] if p["owner"] else primary_t["name"]
                pname = _norm(p["name"])
                cid_key = _norm(f"{owner_name}.{p['name']}")
                children.append(DefItem(label="Module", name=pname, cid_key=cid_key, line=p["line"],
                                        extra={"c_kind": p["term"]}))
            return DefResult(primary=primary, children=_dedupe_children(children), dropped=dropped)

        # VB6/VBA/VBScript: ファイル自体が primary（Namespace/Class ブロック構文が無い）。
        primary_name_raw = _vb6_primary_name(text, rel_path)
        primary_name = _norm(primary_name_raw)
        primary = DefItem(label="Module", name=primary_name)
        children = []
        for p in procedures:
            pname = _norm(p["name"])
            cid_key = _norm(f"{primary_name_raw}.{p['name']}")
            children.append(DefItem(label="Module", name=pname, cid_key=cid_key, line=p["line"],
                                    extra={"c_kind": p["term"]}))
        return DefResult(primary=primary, children=_dedupe_children(children), dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        sanitized = _sanitize(text)
        comments_blanked = _sanitize_comments_only(text)
        logical = _logical_lines(sanitized, comments_blanked)
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
                continue                                  # デザイナ部の中は参照走査しない
            if _DESIGN_BEGIN.match(stripped):
                design_depth += 1
                continue
            _scan_call_refs(san, cb, line_no, refs, dropped)
            refs.extend(_sql_refs_from_line(cb, line_no, dropped))
        return RefResult(refs=refs, dropped=dropped)
