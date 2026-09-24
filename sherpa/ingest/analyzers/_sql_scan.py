"""SQL 字句処理の共通スキャナ（アナライザ拡張 §9 S4'・波2持ち越し「共通 SQL スキャナへ寄せる」）。

`analyzers/sql.py`（DDL）・`analyzers/xml_config.py`（MyBatis SQL 本文）・`analyzers/cobol.py`（EXEC SQL）
の3箇所が呼ぶ、SQL のサニタイズ・テーブル名抽出・識別子正規化の共通実装。標準ライブラリのみで
完結する（正規表現＋位置カーソル、外部 SQL パーサは使わない）。

COBOL の EXEC SQL は複数物理行フラグメントにまたがってブロック終端（`END-EXEC`）を探す状態機械を
持つため、`sanitize()` の1回呼び出しでは足りない——`sanitize_span()`（フラグメントをまたいで
持ち越す走査状態を受け渡す版）を使う。`sanitize()` はこれを `state=None` で1回だけ呼ぶ薄い
ラッパー。
"""
from __future__ import annotations

import re

from ..identifiers import normalize_code_name as _norm

#: 識別子トークン（引用符付きはそのまま識別子として読む・DDL 側／EXEC SQL 側で共通の規則）。
#: 二重引用符の中身は `""` エスケープに対応する（`"a""b"` は1つの識別子トークン）。
#: 継続文字に `#` を含む——DB2/COBOL では `#` が識別子文字（`T#1`・`WS#X` 等）。
IDENT_TOKEN = r'(?:"(?:""|[^"])*"|`[^`]+`|\[[^\]]+\]|[A-Za-z_][\w$#]*)'

# `dot` グループ＝schema 修飾の有無（CTE 名の除外は未修飾参照にだけ適用するための判定に使う）。
_IDENT = re.compile(IDENT_TOKEN + r"(?P<dot>\s*\.\s*" + IDENT_TOKEN + r")?")
_ALIAS = re.compile(r"\s+(?:AS\s+)?(?P<word>[A-Za-z_]\w*)", re.IGNORECASE)

# `INTO` は含めない——`SELECT ... INTO :host-var FROM ...` のホスト変数受け先は参照候補にしない
# （`INSERT INTO`/`MERGE INTO` だけを対象にする）。
_CLAUSE_KEYWORD = re.compile(
    r"\b(?:FROM|JOIN|INSERT\s+INTO|MERGE\s+INTO|UPDATE|DELETE\s+FROM)\b", re.IGNORECASE)

# 別名候補として誤って飲み込まないための予約語（次節キーワード）。
_CLAUSE_STOP = frozenset({
    "WHERE", "ON", "GROUP", "ORDER", "HAVING", "UNION", "SET", "VALUES", "FOR", "WITH",
    "AND", "OR", "INNER", "LEFT", "RIGHT", "OUTER", "CROSS", "JOIN", "FROM", "END-EXEC",
})

# テーブル名候補として読み始めない先頭文字——ホスト変数（`:`）・サブクエリ/派生テーブル（`(`）・
# MyBatis の動的プレースホルダ（`#{...}`/`${...}` の `#`/`$`）。動的な形へは踏み込まず安全側に打ち切る。
_STOP_LEAD_CHARS = (":", "(", "#", "$")

# `FROM`/`JOIN` の直後に続く `LATERAL` キーワード（`JOIN LATERAL (subquery)`）——サブクエリの
# 導出テーブルであり実テーブル名ではないため、識別子候補として読み始める前に読み飛ばす。
_LATERAL_KW = re.compile(r"\s*\bLATERAL\b", re.IGNORECASE)

# `WITH [RECURSIVE] name AS ( ... )` で宣言される CTE 名（未修飾）を集めるための走査。
_WITH_KW = re.compile(r"\bWITH\b", re.IGNORECASE)
_RECURSIVE_KW = re.compile(r"\bRECURSIVE\b", re.IGNORECASE)
# CTE 宣言名のトークン（`IDENT_TOKEN` と同じ規則——引用 CTE 名も受理する）。
_CTE_NAME_TOKEN = re.compile(IDENT_TOKEN)

# 識別子直後の `@`（Oracle の DB link・`schema.table@dblink`）に続く残り（除外専用・読み飛ばすだけ）。
_DBLINK_SUFFIX = re.compile(r"[\w.$]*")


def sanitize_span(text: str, state: str | None = None, *, boundaries: tuple = (),
                   hash_line_comments: bool = False) -> tuple:
    """`sanitize()` の複数呼び出し版——直前の断片から持ち越した走査状態 `state`
    （`None`／`"block_comment"`／`"string"`）を受け取り、この断片を走査し終えた後の状態と
    合わせて `(sanitized, 走査後の state)` を返す。単一引用符文字列（`''` エスケープ対応）・
    `/* */` ブロックコメントは物理行/断片をまたいで状態を持ち越せる（COBOL の `EXEC SQL`
    ブロックが複数の物理行フラグメントにまたがる場合の共通実装）。二重引用符／バッククォート／
    角括弧の引用識別子は識別子として後段で読むためそのまま残す（断片内で閉じる前提・継続はしない）。
    `--` 行コメントは断片中の実際の改行、または `boundaries`（`text` 中の物理行境界オフセット・
    昇順）のどちらか早い方で終端する——渡さない既定（`()`）では改行のみで区切る。
    `hash_line_comments`（既定 `False`）が `True` のときだけ MySQL の `#` 行コメント
    （`#{...}` は MyBatis の動的プレースホルダのため除外）も同様に次の改行/境界まで空白化する。
    既定で無効なのは DB2/COBOL では `#` が識別子文字（`T#1`・`:WS#X` 等）のため——DDL（`sql.py`）・
    COBOL の `EXEC SQL`（`cobol.py`）は無効のまま呼ぶ。`#` を行コメントとして扱う方言は
    `xml_config.py`（MyBatis）のように明示的に `True` を渡す。戻り値は入力と同じ長さ
    （オフセットをそのまま使い回せる）。
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
        if text[i] == '"':                            # 引用識別子（二重引用符・`""` エスケープ対応）
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
        if text[i] == "`":                            # 引用識別子（バッククォート）
            out.append(text[i])
            i += 1
            while i < n and text[i] != "`":
                out.append(text[i])
                i += 1
            if i < n and text[i] == "`":
                out.append(text[i])
                i += 1
            continue
        if text[i] == "[":                            # 引用識別子（角括弧）
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
    """`sanitize_span(text, None, boundaries=boundaries, hash_line_comments=hash_line_comments)`
    を1回呼ぶだけの薄いラッパー（フラグメントをまたぐ状態の持ち越しが不要な呼び出し側＝
    `sql.py`/`xml_config.py` 用）。`hash_line_comments` は既定 `False`（詳細は `sanitize_span`
    参照）。戻り値は入力と同じ長さ（オフセットをそのまま使い回せる）。"""
    out, _state = sanitize_span(text, None, boundaries=boundaries,
                                 hash_line_comments=hash_line_comments)
    return out


def _blank_quoted_idents(sanitized: str) -> str:
    """`sanitized`（`sanitize()`/`sanitize_span()` 済み）から引用識別子（`"…"`／`` `…` ``／
    `[…]`）の中身を同じ長さの空白へ置換した文字列を返す（句キーワード探索専用——引用識別子の
    内部に偶然含まれる `FROM`/`JOIN`/`WITH`/`AS` 等を句キーワード/CTE 宣言と誤認しないため）。
    識別子そのものの抽出・別名判定は `sanitized`（本関数の引数）に対して行う——オフセットは
    共通（同じ長さ）。"""
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
    """`text[pos]` が `(` である前提で、対応する `)` の直後の位置を返す（ネスト対応・対応する
    `)` が無ければ文字列末尾）。"""
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
    """`WITH name [(col, ...)] AS ( ... ), name2 [(col, ...)] AS ( ... )` で宣言された未修飾
    CTE 名を集めて返す（FROM/JOIN 候補からの除外専用——CTE は物理テーブルではない）。名前の比較は
    `unquote_or_norm_ident()` と同じ規則（引用 CTE 名はそのまま・非引用は大文字化）で正規化した後の
    値を集合に入れる——`table_refs` 側も同じ正規化を適用した名前と比較するため、引用 CTE 宣言
    （`WITH "recent" AS (...)`）も除外できる。

    `keyword_scan`（`stmt` から引用識別子の中身を空白化した同じ長さの文字列）に対して `WITH`/
    `RECURSIVE`/`AS`/`(`/`,` の位置を探す（引用識別子内部に偶然含まれるこれらの語を誤認しないため）が、
    CTE 名そのものの読み取りは `stmt`（元の・空白化していない文字列）に対して行う——名前が
    引用識別子の場合、`keyword_scan` 上ではその中身が空白化されているため、`stmt` から読まないと
    実際の名前が取れない。1つの SQL 文（`;` で区切った単位・呼び出し側で分割済み）に対して呼ぶ想定
    ——宣言でない `WITH` 出現（マッチ失敗）は無視する。"""
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
            if pos < n and keyword_scan[pos] == "(":       # 省略可能な列リスト
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
    """引用識別子（`"..."`／`` `...` ``／`[...]`）はそのまま（大文字小文字区別・二重引用符は
    `""` エスケープを `"` へ戻す）、非引用は `identifiers.normalize_code_name()` と同じ規則で
    大文字化する（DDL/EXEC SQL/MyBatis SQL で共通の識別子規則）。"""
    token = token.strip()
    if len(token) >= 2 and token[0] == '"' and token[-1] == '"':
        return token[1:-1].replace('""', '"')
    if len(token) >= 2 and token[0] == "`" and token[-1] == "`":
        return token[1:-1]
    if len(token) >= 2 and token[0] == "[" and token[-1] == "]":
        return token[1:-1]
    return _norm(token)


def _split_sql_statements(sanitized: str) -> list:
    """`sanitized`（`sanitize()`/`sanitize_span()` 済み）を SQL 文単位（引用識別子の外側の `;` で
    区切る）に分割し `[(オフセット, 文テキスト), ...]` を返す（出現順）。

    CTE 名のスコープを SQL 文単位に限定するための分割——複文（MyBatis の複文タグ・COBOL の
    複数文 `EXEC SQL` 等、1本の呼び出しに複数の SQL 文が連結された本文）で、ある文の
    `WITH name AS (...)` が宣言した CTE 名を**別の文**の `FROM name`（そちらでは実テーブルの
    可能性がある）にまで漏らして誤除外しないようにする。コメント／文字列リテラルは
    `sanitize()`/`sanitize_span()` の時点で中身が空白化済み（`;` も含めて）のため、ここでの `;`
    探索は引用識別子（`"…"`／`` `…` ``／`[…]`）の内側だけを避ければよい。"""
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
    """`sanitized`（`sanitize()`/`sanitize_span()` 済み本文）から `FROM`/`JOIN`/`INSERT INTO`/
    `MERGE INTO`/`UPDATE`/`DELETE FROM` 直後のテーブル名候補を `[(name, offset), ...]` で返す
    （出現順）。

    `_split_sql_statements()` で SQL 文単位（`;` 区切り）に分割してから文ごとに走査する——CTE 名
    （`WITH name AS (...)`）の宣言は**その文の中でだけ**有効（別の文で同名が実テーブルとして
    使われていても除外しない）。

    カンマ区切りの複数テーブル・別名（`AS name`／`name`）は読み飛ばして捨てる。`:`（ホスト変数）・
    `(`（派生テーブル/サブクエリ）・`#`/`$`（MyBatis の動的プレースホルダ `#{...}`/`${...}`）で
    始まる項目はそこで打ち切る——動的な形へは踏み込まない安全側の判断。schema 修飾（`SCHEMA.NAME`）は
    schema 側を落とす。`FROM`/`JOIN` に限り、同じ文内の `WITH [RECURSIVE] name AS (...)` で宣言された
    CTE 名（未修飾で参照された場合のみ——`schema.x` のように修飾された参照は同名の実テーブルとみなし
    除外しない。CTE 宣言名・参照名の比較は `unquote_or_norm_ident()` と同じ規則で正規化した後の値で
    行う——引用 CTE 名（`WITH "recent" AS (...)`）も除外できる）・`LATERAL`（`JOIN LATERAL (subquery)`）・
    識別子直後に `@` が続く候補（Oracle の DB link）は参照候補から除外する（CTE/LATERAL のサブクエリ・
    DB link 先はこのファイル内の物理テーブルではないため）。句キーワード（`FROM`/`JOIN` 等）・CTE 宣言
    （`WITH ... AS (`）の探索は引用識別子の中身を空白化した別のスキャン文字列（`_blank_quoted_idents`）
    に対して行う——引用識別子の内部に偶然含まれるこれらの語を句/宣言と誤認しないため。識別子そのものの
    抽出・別名判定は文テキストに対して行う（オフセットは共通）。`offset` は `sanitized` 中のテーブル名
    トークンの開始位置に `base_offset` を加えたもの（呼び出し側が行番号などへ変換する際に使う）。
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
                if pos < n and stmt[pos] == "@":       # Oracle の DB link — 参照候補から除外
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
                    pos = am.end()                     # 別名（`AS name`／`name`）を読み飛ばす
                while pos < n and stmt[pos].isspace():
                    pos += 1
                if pos < n and stmt[pos] == ",":
                    pos += 1
                    continue
                break
    return refs
