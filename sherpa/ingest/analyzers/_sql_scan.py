"""SQL 字句処理の共通スキャナ。サニタイズ・テーブル名抽出・識別子正規化を `sql.py`（DDL）・`xml_config.py`（MyBatis SQL）・`cobol.py`（EXEC SQL）が共有する。

標準ライブラリのみ（正規表現＋位置カーソル）。複数の物理行フラグメントにまたがる EXEC SQL には、走査状態を持ち越す `sanitize_span()` を使う。
"""
from __future__ import annotations

import re

from ..identifiers import normalize_code_name as _norm

# 識別子トークン（引用符付きもそのまま識別子。`""` エスケープ対応）。継続文字に `#` を含む（DB2/COBOL の識別子文字）。
IDENT_TOKEN = r'(?:"(?:""|[^"])*"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$#]*)'

# `dot` グループ＝schema 修飾の有無（CTE 名の除外は未修飾参照だけに適用する）。
_IDENT = re.compile(IDENT_TOKEN + r"(?P<dot>(?:\s*\.\s*" + IDENT_TOKEN + r")*)")
_ALIAS = re.compile(r"\s+(?:AS\s+)?(?P<word>[A-Za-z_]\w*)", re.IGNORECASE)

# `INTO` は含めない（`SELECT ... INTO :host-var` のホスト変数受けは参照にしない。`INSERT INTO`/`MERGE INTO` だけが対象）。
_CLAUSE_KEYWORD = re.compile(
    r"\b(?:FROM|JOIN|INSERT\s+INTO|MERGE\s+INTO|UPDATE|DELETE\s+FROM)\b", re.IGNORECASE)

# 別名として飲み込まない予約語。
_CLAUSE_STOP = frozenset({
    "WHERE", "ON", "GROUP", "ORDER", "HAVING", "UNION", "SET", "VALUES", "FOR", "WITH",
    "AND", "OR", "INNER", "LEFT", "RIGHT", "OUTER", "CROSS", "JOIN", "FROM", "END-EXEC",
})

# テーブル名として読み始めない先頭文字: ホスト変数 `:`・サブクエリ `(`・MyBatis の動的プレースホルダ `#`/`$`。
_STOP_LEAD_CHARS = (":", "(", "#", "$")

# 括弧の中がこれらで始まれば副問い合わせ（関数の引数ではない）。
_SUBQUERY_LEADS = frozenset({"SELECT", "WITH"})

# `JOIN LATERAL (subquery)` の `LATERAL` は導出テーブルなので読み飛ばす。
_LATERAL_KW = re.compile(r"\s*\bLATERAL\b", re.IGNORECASE)

# `WITH [RECURSIVE] name AS ( ... )` の CTE 名を集める走査。
_WITH_KW = re.compile(r"\bWITH\b", re.IGNORECASE)
_RECURSIVE_KW = re.compile(r"\bRECURSIVE\b", re.IGNORECASE)
# CTE 宣言名のトークン（引用 CTE 名も受理する）。
_CTE_NAME_TOKEN = re.compile(IDENT_TOKEN)

# 識別子直後の `@dblink`（Oracle の DB link・除外専用）。
_DBLINK_SUFFIX = re.compile(r"[\w.$]*")


def split_qualified(full: str) -> list:
    """`[DB.][schema.]NAME` を引用符の外側の `.` で分割する（各要素は引用符を残した生のトークン）。引用符の中の `.` は区切りにしない。"""
    parts: list = []
    close = None
    start = 0
    for i, ch in enumerate(full):
        if close:
            if ch == close:
                close = None
            continue
        if ch in ('"', "`"):
            close = ch
        elif ch == "[":
            close = "]"
        elif ch == ".":
            parts.append(full[start:i].strip())
            start = i + 1
    parts.append(full[start:].strip())
    return parts


def table_schema_name(full: str) -> tuple:
    """表名の生トークン列 → `(schema|None, name, ok)`（正規化済み）。1 部＝名前のみ・2 部＝`schema.name`・3 部＝先頭の場所名（DB）を捨てて `schema.name`。
    4 部以上は表として読めないので `ok=False`（`name` は全体の生の文字列）。"""
    parts = split_qualified(full)
    if len(parts) > 3:
        return None, full.strip(), False
    schema = unquote_or_norm_ident(parts[-2]) if len(parts) >= 2 else None
    return schema, unquote_or_norm_ident(parts[-1]), True


class TableName(str):
    """表の参照名。文字列としては `SCHEMA.NAME`（schema を書いたとき）／`NAME`。解決側は文字列を再分解せず、`schema`・`simple`（引用符内の `.` と区別するため別の属性）を読む。
    `supported=False` は 4 部以上の名前（表として解決しない）。"""

    def __new__(cls, schema, simple, supported=True):
        obj = super().__new__(cls, f"{schema}.{simple}" if schema else simple)
        obj.schema = schema
        obj.simple = simple
        obj.supported = supported
        return obj

    def __eq__(self, other):
        if isinstance(other, TableName):
            return (self.schema, self.simple) == (other.schema, other.simple)
        return str.__eq__(self, other)

    def __ne__(self, other):
        return not self.__eq__(other)

    __hash__ = str.__hash__


# 動的な表名（MyBatis の `${...}` を含む表名）の開始位置。
_DYNAMIC_TABLE = re.compile(
    r"\b(?:FROM|JOIN|INSERT\s+INTO|MERGE\s+INTO|UPDATE|DELETE\s+FROM)\s+[\w.$\"`\[\]]*\$\{", re.IGNORECASE)


def dynamic_table_offsets(sanitized: str) -> list:
    """句キーワード直後の表名が `${...}` を含む位置（キーワード先頭のオフセット）の一覧。"""
    return [m.start() for m in _DYNAMIC_TABLE.finditer(sanitized)]


def sanitize_span(text: str, state: str | None = None, *, boundaries: tuple = (),
                   hash_line_comments: bool = False) -> tuple:
    """`sanitize()` の複数呼び出し版。直前の断片から持ち越した走査状態 `state`（`None`／`"block_comment"`／`"string"`）を受け取り、`(sanitized, 走査後の state)` を返す。

    文字列リテラル・`/* */`・`--` 行コメントを空白化する。引用識別子は残す。`--` は改行か `boundaries`（物理行境界オフセット・昇順）の早い方で終わる。
    `hash_line_comments=True` のときだけ `#` 行コメント（MyBatis の `#{...}` は除く）も空白化する。`#` が識別子文字の DDL・EXEC SQL は既定 `False` のまま呼ぶ。戻り値は入力と同じ長さ。
    """
    out: list = []
    i, n = 0, len(text)
    while i < n:
        if state == "block_comment":
            if text[i:i + 2] == "*/":
                out.append("  ")
                i += 2
                state = None
            else:
                out.append("\n" if text[i] == "\n" else " ")
                i += 1
            continue
        if state == "string":
            if text[i:i + 2] == "''":
                out.append("  ")
                i += 2
                continue
            if text[i] == "'":
                out.append(" ")
                i += 1
                state = None
                continue
            out.append("\n" if text[i] == "\n" else " ")
            i += 1
            continue
        if text[i:i + 2] == "--" or (
                hash_line_comments and text[i] == "#" and text[i:i + 2] != "#{"):
            end = n
            for b in boundaries:
                if b > i:
                    end = b
                    break
            nl = text.find("\n", i, end)
            if nl != -1:
                end = nl
            out.append(" " * (end - i))
            i = end
            continue
        if text[i:i + 2] == "/*":
            out.append("  ")
            i += 2
            state = "block_comment"
            continue
        if text[i] == "'":
            out.append(" ")
            i += 1
            state = "string"
            continue
        if text[i] == '"':  # 引用識別子（二重引用符）
            out.append(text[i])
            i += 1
            while i < n:
                if text[i:i + 2] == '""':
                    out.append(text[i:i + 2])
                    i += 2
                    continue
                if text[i] == '"':
                    break
                out.append(text[i])
                i += 1
            if i < n and text[i] == '"':
                out.append(text[i])
                i += 1
            continue
        if text[i] == "`":  # 引用識別子（バッククォート）
            out.append(text[i])
            i += 1
            while i < n and text[i] != "`":
                out.append(text[i])
                i += 1
            if i < n and text[i] == "`":
                out.append(text[i])
                i += 1
            continue
        if text[i] == "[":  # 引用識別子（角括弧）
            out.append(text[i])
            i += 1
            while i < n and text[i] != "]":
                out.append(text[i])
                i += 1
            if i < n and text[i] == "]":
                out.append(text[i])
                i += 1
            continue
        out.append(text[i])
        i += 1
    return "".join(out), state


def sanitize(text: str, *, boundaries: tuple = (), hash_line_comments: bool = False) -> str:
    """`sanitize_span(text, None, ...)` を1回呼ぶだけのラッパー（状態の持ち越しが不要な呼び出し側用）。戻り値は入力と同じ長さ。"""
    out, _state = sanitize_span(text, None, boundaries=boundaries,
                                 hash_line_comments=hash_line_comments)
    return out


def _blank_quoted_idents(sanitized: str) -> str:
    """`sanitized` の引用識別子の中身を同じ長さの空白へ置換した文字列を返す（句キーワード探索専用。引用識別子内の `FROM`/`JOIN`/`WITH`/`AS` を誤認しないため）。"""
    out: list = []
    i, n = 0, len(sanitized)
    while i < n:
        ch = sanitized[i]
        if ch == '"':
            out.append('"')
            i += 1
            while i < n:
                if sanitized[i:i + 2] == '""':
                    out.append("  ")
                    i += 2
                    continue
                if sanitized[i] == '"':
                    break
                out.append(" ")
                i += 1
            if i < n and sanitized[i] == '"':
                out.append('"')
                i += 1
            continue
        if ch == "`":
            out.append("`")
            i += 1
            while i < n and sanitized[i] != "`":
                out.append(" ")
                i += 1
            if i < n and sanitized[i] == "`":
                out.append("`")
                i += 1
            continue
        if ch == "[":
            out.append("[")
            i += 1
            while i < n and sanitized[i] != "]":
                out.append(" ")
                i += 1
            if i < n and sanitized[i] == "]":
                out.append("]")
                i += 1
            continue
        out.append(ch)
        i += 1
    return "".join(out)


def _skip_balanced_parens(text: str, pos: int) -> int:
    """`text[pos]` が `(` の前提で、対応する `)` の直後の位置を返す（無ければ末尾）。"""
    depth = 0
    n = len(text)
    i = pos
    while i < n:
        if text[i] == "(":
            depth += 1
        elif text[i] == ")":
            depth -= 1
            if depth == 0:
                return i + 1
        i += 1
    return n


def _cte_names(stmt: str, keyword_scan: str) -> frozenset:
    """`WITH name [(col, ...)] AS ( ... ), ...` で宣言された未修飾 CTE 名を集める（FROM/JOIN 候補から除外するため）。名前は `unquote_or_norm_ident()` の規則で正規化する。

    キーワード位置は `keyword_scan`（引用識別子を空白化した文字列）で探し、名前そのものは元の `stmt` から読む。1つの SQL 文に対して呼ぶ。
    """
    names: set = set()
    n = len(keyword_scan)
    for wm in _WITH_KW.finditer(keyword_scan):
        pos = wm.end()
        while pos < n and keyword_scan[pos].isspace():
            pos += 1
        rm = _RECURSIVE_KW.match(keyword_scan, pos)
        if rm:
            pos = rm.end()
        while True:
            while pos < n and keyword_scan[pos].isspace():
                pos += 1
            nm = _CTE_NAME_TOKEN.match(stmt, pos)
            if not nm:
                break
            name = unquote_or_norm_ident(nm.group(0))
            pos = nm.end()
            while pos < n and keyword_scan[pos].isspace():
                pos += 1
            if pos < n and keyword_scan[pos] == "(":  # 省略可能な列リスト
                pos = _skip_balanced_parens(keyword_scan, pos)
                while pos < n and keyword_scan[pos].isspace():
                    pos += 1
            is_as = (keyword_scan[pos:pos + 2].upper() == "AS"
                     and not (pos + 2 < n and (keyword_scan[pos + 2].isalnum()
                                                or keyword_scan[pos + 2] == "_")))
            if not is_as:
                break
            pos += 2
            while pos < n and keyword_scan[pos].isspace():
                pos += 1
            if pos >= n or keyword_scan[pos] != "(":
                break
            names.add(name)
            pos = _skip_balanced_parens(keyword_scan, pos)
            while pos < n and keyword_scan[pos].isspace():
                pos += 1
            if pos < n and keyword_scan[pos] == ",":
                pos += 1
                continue
            break
    return frozenset(names)


def unquote_or_norm_ident(token: str) -> str:
    """引用識別子はそのまま（`""` エスケープは `"` へ戻す）、非引用は `identifiers.normalize_code_name()` と同じ規則で大文字化する。"""
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1].replace('""', '"')
    if len(token) >= 2 and token[0] == "`" and token[-1] == "`":
        return token[1:-1]
    if len(token) >= 2 and token[0] == "[" and token[-1] == "]":
        return token[1:-1]
    return _norm(token)


def _split_sql_statements(sanitized: str) -> list:
    """`sanitized` を SQL 文単位（引用識別子の外の `;` 区切り）に分割し `[(オフセット, 文テキスト), ...]` を返す。CTE 名のスコープを文単位に限るための分割。"""
    parts: list = []
    n = len(sanitized)
    start = 0
    i = 0
    while i < n:
        ch = sanitized[i]
        if ch == '"':
            i += 1
            while i < n:
                if sanitized[i:i + 2] == '""':
                    i += 2
                    continue
                if sanitized[i] == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch == "`":
            i += 1
            while i < n and sanitized[i] != "`":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == "[":
            i += 1
            while i < n and sanitized[i] != "]":
                i += 1
            if i < n:
                i += 1
            continue
        if ch == ";":
            parts.append((start, sanitized[start:i]))
            start = i + 1
            i += 1
            continue
        i += 1
    parts.append((start, sanitized[start:]))
    return parts


def _function_from_positions(keyword_scan: str) -> frozenset:
    """関数呼び出しの括弧（識別子の直後の `(`）の直下にある `FROM`（`EXTRACT(YEAR FROM 列)`・`SUBSTRING(x FROM 2)`・`TRIM(BOTH ' ' FROM 列)` など）の開始位置を返す。括弧の中が `SELECT`／`WITH` で始まるもの（副問い合わせ。`IN (SELECT … FROM t)`・`FROM (SELECT … FROM t)` を含む）は関数ではないので対象外。"""
    if "(" not in keyword_scan:
        return frozenset()
    from_starts = {m.start() for m in _CLAUSE_KEYWORD.finditer(keyword_scan)
                   if m.group(0).upper() == "FROM"}
    n = len(keyword_scan)
    found: set = set()
    stack: list = []  # 開き括弧ごとの (位置, 直前が識別子か)
    for i, ch in enumerate(keyword_scan):
        if ch == "(":
            j = i
            while j > 0 and keyword_scan[j - 1].isspace():
                j -= 1
            stack.append((i, j > 0 and (keyword_scan[j - 1].isalnum() or keyword_scan[j - 1] in '_$#"`]')))  # 引用識別子の関数名も関数
        elif ch == ")":
            if stack:
                stack.pop()
        elif i in from_starts and stack and stack[-1][1]:
            k = stack[-1][0] + 1
            while k < n and keyword_scan[k].isspace():
                k += 1
            m = k
            while m < n and (keyword_scan[m].isalnum() or keyword_scan[m] == "_"):
                m += 1
            if keyword_scan[k:m].upper() not in _SUBQUERY_LEADS:
                found.add(i)
    return frozenset(found)


def table_refs(sanitized: str, *, base_offset: int = 0) -> list:
    """`sanitized` から `FROM`/`JOIN`/`INSERT INTO`/`MERGE INTO`/`UPDATE`/`DELETE FROM` 直後のテーブル名候補を `[(name, offset), ...]`（出現順）で返す。schema を書いた参照の `name` は修飾名 `SCHEMA.NAME`（解決側が schema で引く）、書いていなければ `NAME`。

    文ごとに走査し、CTE 名の除外はその文の中だけで有効。
    カンマ区切りの複数テーブル・別名は読み飛ばす。`:`・`(`・`#`/`$` で始まる項目は打ち切る。
    `FROM`/`JOIN` では未修飾の CTE 名・`LATERAL`・DB link 先を除く。関数呼び出しの括弧（識別子の直後の `(`）の直下の `FROM`（`EXTRACT(… FROM 列)` など）は表の句として読まない。ただし括弧の中が `SELECT`/`WITH` で始まる副問い合わせは読む。句キーワードの探索は `_blank_quoted_idents` の結果に対して行う。
    `offset` はテーブル名トークンの開始位置に `base_offset` を加えた値。
    """
    refs: list = []
    for stmt_offset, stmt in _split_sql_statements(sanitized):
        n = len(stmt)
        keyword_scan = _blank_quoted_idents(stmt)
        cte = _cte_names(stmt, keyword_scan)
        expr_froms = _function_from_positions(keyword_scan)
        for cm in _CLAUSE_KEYWORD.finditer(keyword_scan):
            if cm.start() in expr_froms:
                continue
            clause = cm.group(0).strip().upper()
            is_from_or_join = clause.startswith("FROM") or clause.startswith("JOIN")
            pos = cm.end()
            if is_from_or_join:
                lm = _LATERAL_KW.match(stmt, pos)
                if lm:
                    pos = lm.end()
            while True:
                while pos < n and stmt[pos].isspace():
                    pos += 1
                if pos >= n or stmt[pos] in _STOP_LEAD_CHARS:
                    break
                im = _IDENT.match(stmt, pos)
                if not im:
                    break
                qualified = bool(im.group("dot"))
                full = im.group(0).strip()
                schema, simple_name, supported = table_schema_name(full)
                simple = split_qualified(full)[-1]
                pos = im.end()
                if pos < n and stmt[pos] == "@":  # Oracle の DB link は除外
                    dm = _DBLINK_SUFFIX.match(stmt, pos + 1)
                    pos = dm.end() if dm else pos + 1
                    while pos < n and stmt[pos].isspace():
                        pos += 1
                    if pos < n and stmt[pos] == ",":
                        pos += 1
                        continue
                    break
                if simple.upper() in _CLAUSE_STOP:
                    break
                normalized = TableName(schema, simple_name, supported)
                if not (is_from_or_join and not qualified and normalized in cte):
                    refs.append((normalized, base_offset + stmt_offset + im.start()))
                am = _ALIAS.match(stmt, pos)
                if am and am.group("word").upper() not in _CLAUSE_STOP:
                    pos = am.end()  # 別名（`AS name`／`name`）を読み飛ばす
                while pos < n and stmt[pos].isspace():
                    pos += 1
                if pos < n and stmt[pos] == ",":
                    pos += 1
                    continue
                break
    return refs
