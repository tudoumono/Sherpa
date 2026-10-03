"""SQL 字句処理の共通スキャナ。サニタイズ・テーブル名抽出・識別子正規化を `sql.py`（DDL）・`xml_config.py`（MyBatis SQL）・`cobol.py`（EXEC SQL）が共有する。

標準ライブラリのみ（正規表現＋位置カーソル）。複数の物理行フラグメントにまたがる EXEC SQL には、走査状態を持ち越す `sanitize_span()` を使う。
"""
from __future__ import annotations

import re

from ..identifiers import normalize_code_name as _norm

# 識別子トークン（引用符付きもそのまま識別子。`""` エスケープ対応）。継続文字に `#` を含む（DB2/COBOL の識別子文字）。
IDENT_TOKEN = r'(?:"(?:""|[^"])*"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$#]*)'

# `dot` グループ＝schema 修飾の有無（CTE 名の除外は未修飾参照だけに適用する）。
_IDENT = re.compile(IDENT_TOKEN + r"(?P<dot>\s*\.\s*" + IDENT_TOKEN + r")?")
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

# `JOIN LATERAL (subquery)` の `LATERAL` は導出テーブルなので読み飛ばす。
_LATERAL_KW = re.compile(r"\s*\bLATERAL\b", re.IGNORECASE)

# `WITH [RECURSIVE] name AS ( ... )` の CTE 名を集める走査。
_WITH_KW = re.compile(r"\bWITH\b", re.IGNORECASE)
_RECURSIVE_KW = re.compile(r"\bRECURSIVE\b", re.IGNORECASE)
# CTE 宣言名のトークン（引用 CTE 名も受理する）。
_CTE_NAME_TOKEN = re.compile(IDENT_TOKEN)

# 識別子直後の `@dblink`（Oracle の DB link・除外専用）。
_DBLINK_SUFFIX = re.compile(r"[\w.$]*")


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


def table_refs(sanitized: str, *, base_offset: int = 0) -> list:
    """`sanitized` から `FROM`/`JOIN`/`INSERT INTO`/`MERGE INTO`/`UPDATE`/`DELETE FROM` 直後のテーブル名候補を `[(name, offset), ...]`（出現順）で返す。

    文ごとに走査し、CTE 名の除外はその文の中だけで有効。
    カンマ区切りの複数テーブル・別名は読み飛ばし、schema 修飾は schema 側を落とす。`:`・`(`・`#`/`$` で始まる項目は打ち切る。
    `FROM`/`JOIN` では未修飾の CTE 名・`LATERAL`・DB link 先を除く。句キーワードの探索は `_blank_quoted_idents` の結果に対して行う。
    `offset` はテーブル名トークンの開始位置に `base_offset` を加えた値。
    """
    refs: list = []
    for stmt_offset, stmt in _split_sql_statements(sanitized):
        n = len(stmt)
        keyword_scan = _blank_quoted_idents(stmt)
        cte = _cte_names(stmt, keyword_scan)
        for cm in _CLAUSE_KEYWORD.finditer(keyword_scan):
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
                simple = im.group(0).rsplit(".", 1)[-1].strip()
                qualified = im.group("dot") is not None
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
                normalized = unquote_or_norm_ident(simple)
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
