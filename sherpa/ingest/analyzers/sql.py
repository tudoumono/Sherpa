"""SQL/DDL アナライザ（アナライザ拡張 §4(a)・§4(g)＝A1）。

`CREATE TABLE [schema.]NAME (...)`（`GLOBAL`/`LOCAL TEMPORARY` 方言・`CREATE TABLE NAME AS SELECT`
＝CTAS も受理）を `Table` 定義（primary＝最初の1件・2件目以降は
`DefResult.extras`＝アナライザ拡張 A10）として返し、列定義を `DataItem` の children
（`Copybook` の `GROUP.ITEM` と同型の修飾名＝`cid_key="TABLE.COLUMN"`）とする。制約行
（`PRIMARY KEY`/`FOREIGN KEY`/`CONSTRAINT`/`UNIQUE`/`INDEX`/`KEY`/`CHECK`）は列にしない。CTAS は
列定義を持たない（`SELECT` 側のスキーマ推論はしない）。

同一ファイル内で schema 修飾を落とした後に同名の `Table` が複数出現した場合（例: `a.orders`/
`b.orders`）、cid（`label`+`world`+`path`+`name`）が衝突するため2件目以降はノード化せず
`Dropped("table_name_collision", ...)` として申告する（最初の1件だけ定義）。

`CREATE TABLE`/`ALTER TABLE`（未対応方言含む）以外の `CREATE`（`VIEW`/`PROCEDURE`/`FUNCTION`/
`TRIGGER`/`INDEX`/`SEQUENCE`/`SCHEMA` 等）・`ALTER TABLE`・DML のみのファイル（`CREATE TABLE` を
一切含まない）は `Dropped("ddl_unsupported"/"dml_only", ...)` として申告する（定義・参照は作らない・
未知の `CREATE` 方言も黙って落とさない）。`extract_refs` は常に空——DDL は
他ファイルを参照しない（`Table` への参照は EXEC SQL（COBOL）・MyBatis SQL 本文（S4'）側が持つ）。

識別子は引用符（`"..."`／`` `...` ``／`[...]`）付きならそのまま（大文字小文字区別）、非引用なら
`identifiers.normalize_code_name()` と同じ規則で大文字化する（§4(g)＝多くの SQL 方言は非引用
識別子を大文字小文字区別なしで扱うため、COBOL と同じ正規化を使う）。schema 修飾（`SCHEMA.NAME`）は
schema を落として `extra={"schema": ...}` に保持し、`name` には NAME だけを使う。

コメント（`--`・`/* */`）と文字列リテラル（`'...'`・`''` エスケープ対応）はサニタイズして無視する
（引用識別子の中身は識別子として読むためサニタイズしない）。サニタイズ・識別子正規化の規則は
`_sql_scan`（§9 S4'・COBOL の EXEC SQL と共通化した SQL 字句処理）を使う。外部パーサは使わず
正規表現＋行走査（COBOL/JCL/Java 等と同じ流儀）。行番号は改行位置の配列を1回だけ作り `bisect` で引く
（大規模ファイルでも `_line_at` の呼び出し1回が対数時間）。
"""
from __future__ import annotations

import bisect
import re

from . import _sql_scan
from ._base import Analyzer, DefGroup, DefItem, DefResult, Dropped, RefResult

SQL_EXT = frozenset({".sql"})

_IDENT_TOKEN = _sql_scan.IDENT_TOKEN

# `CREATE [GLOBAL|LOCAL] TEMPORARY TABLE`（方言）も受理する共通プレフィックス。
_CREATE_TABLE_PREFIX = r"\bCREATE\s+(?:(?:GLOBAL|LOCAL)\s+TEMPORARY\s+)?TABLE\s+"

_CREATE_TABLE = re.compile(
    _CREATE_TABLE_PREFIX + r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>" + _IDENT_TOKEN + r"(?:\s*\.\s*" + _IDENT_TOKEN + r")?)"
    r"\s*\(",
    re.IGNORECASE,
)

# `CREATE TABLE NAME AS SELECT ...`（CTAS・列リストなし＝列なしの Table）。
_CREATE_TABLE_AS_SELECT = re.compile(
    _CREATE_TABLE_PREFIX + r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>" + _IDENT_TOKEN + r"(?:\s*\.\s*" + _IDENT_TOKEN + r")?)"
    r"\s+AS\s+SELECT\b",
    re.IGNORECASE,
)

# `CREATE TABLE`/`ALTER TABLE` 以外の未対応 DDL/DML（definition・参照は作らず Dropped のみ・
# §7 やらないこと）。`CREATE` 側は個別方言を列挙せず包括的に検知する（`_CREATE_ANY`・下記）——
# `CREATE TABLE`/CTAS として認識済みの出現位置を除いた残り全部が対象。
_ALTER_TABLE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)
_CREATE_ANY = re.compile(r"\bCREATE\b", re.IGNORECASE)

_DML_LEAD = re.compile(r"\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b", re.IGNORECASE)

_LEADING_IDENT = re.compile(r"^\s*(" + _IDENT_TOKEN + r")")
_CONSTRAINT_LEAD = re.compile(
    r"^\s*(?:PRIMARY|FOREIGN|CONSTRAINT|UNIQUE|INDEX|KEY|CHECK)\b", re.IGNORECASE)


def _sanitize(text: str) -> str:
    """`_sql_scan.sanitize()` のエイリアス（DDL 側は物理行境界を跨ぐ必要が無いため既定のまま呼ぶ）。
    `hash_line_comments` は既定 `False`（DB2 の DDL では `#` が識別子文字——`#` 行コメントの
    MySQL 方言はここでは扱わない）。"""
    return _sql_scan.sanitize(text)


def _newline_offsets(text: str) -> list:
    """`text` 内の全改行位置（昇順）。`collect_defs` 1回につき1回だけ作り、`_line_at` は
    これを `bisect` で引く（大規模ファイルでも対数時間）。
    """
    return [i for i, ch in enumerate(text) if ch == "\n"]


def _line_at(newline_offsets: list, pos: int) -> int:
    return bisect.bisect_left(newline_offsets, pos) + 1


def _match_paren(s: str, open_pos: int) -> int | None:
    """`s[open_pos]`（`(`）に対応する閉じ `)` の位置を返す（見つからなければ `None`）。"""
    depth = 0
    for j in range(open_pos, len(s)):
        if s[j] == "(":
            depth += 1
        elif s[j] == ")":
            depth -= 1
            if depth == 0:
                return j
    return None


def _split_top_level(s: str) -> list:
    """ネストした `(...)` を跨がないトップレベルのカンマで分割する（各要素は `(相対開始位置, テキスト)`）。"""
    parts: list = []
    depth = 0
    start = 0
    for i, ch in enumerate(s):
        if ch == "(":
            depth += 1
        elif ch == ")":
            depth = max(0, depth - 1)
        elif ch == "," and depth == 0:
            parts.append((start, s[start:i]))
            start = i + 1
    parts.append((start, s[start:]))
    return parts


def _unquote_or_normalize(token: str) -> str:
    """`_sql_scan.unquote_or_norm_ident()` のエイリアス（§4(a)/§4(g)）。"""
    return _sql_scan.unquote_or_norm_ident(token)


def _split_schema_qualified(full: str) -> tuple:
    """`[schema.]NAME` を分解する（`.` は引用符の外側でのみ区切りとして扱う）。

    戻り値は `(schema_raw|None, name_raw)`——両方とも生の（引用符を残した）トークン文字列。
    """
    close = None
    for i, ch in enumerate(full):
        if close:
            if ch == close:
                close = None
            continue
        if ch in ('"', "`"):
            close = ch
            continue
        if ch == "[":
            close = "]"
            continue
        if ch == ".":
            return full[:i].strip(), full[i + 1:].strip()
    return None, full.strip()


def _snippet_line(lines_raw: list, line: int) -> str:
    return lines_raw[line - 1].strip()[:120] if 0 < line <= len(lines_raw) else ""


class SqlDdlAnalyzer(Analyzer):
    """`CREATE TABLE [schema.]NAME (...)`（方言・CTAS 含む）→ `Table`（primary/`extras`・A10）。
    列 → `DataItem`（`CONTAINS`）。schema 除去後の同名 Table・`CREATE TABLE`/CTAS 以外の
    `CREATE`・`ALTER TABLE`・DML のみは `Dropped`。`extract_refs` は常に空（DDL は他ファイルを
    参照しない）。
    """

    name = "sql"
    extensions = SQL_EXT
    doctype = "sql"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        sanitized = _sanitize(text)
        newline_offsets = _newline_offsets(sanitized)
        lines_raw = text.splitlines()
        dropped: list = []
        groups: list = []                             # [(DefItem(Table), [DefItem(DataItem), ...]), ...]
        seen_table_names: set = set()                 # schema 除去後の名前（cid 衝突検知）
        handled_create_starts: set = set()            # `CREATE TABLE`/CTAS として認識済みの出現位置

        # `CREATE TABLE`（列あり）と CTAS（列なし）は出現順にまとめて処理する。
        create_matches = sorted(
            [(m, True) for m in _CREATE_TABLE.finditer(sanitized)]
            + [(m, False) for m in _CREATE_TABLE_AS_SELECT.finditer(sanitized)],
            key=lambda pair: pair[0].start(),
        )

        for m, has_columns in create_matches:
            handled_create_starts.add(m.start())
            header_line = _line_at(newline_offsets, m.start())
            schema_raw, name_raw = _split_schema_qualified(m.group("name"))
            table_name = _unquote_or_normalize(name_raw)

            if table_name in seen_table_names:          # schema 除去後の同名 Table は cid が衝突する
                raw_full = f"{schema_raw}.{name_raw}" if schema_raw else name_raw
                dropped.append(Dropped("table_name_collision", header_line, raw_full[:120]))
                continue

            children: list = []
            if has_columns:
                paren_start = m.end() - 1
                close = _match_paren(sanitized, paren_start)
                if close is None:                      # 対応する `)` が無い＝解釈しない（黙って消さない）
                    dropped.append(Dropped("ddl_unsupported", header_line,
                                           _snippet_line(lines_raw, header_line)))
                    continue
                inner = sanitized[paren_start + 1:close]
                for offset, clause in _split_top_level(inner):
                    stripped = clause.strip()
                    if not stripped or _CONSTRAINT_LEAD.match(stripped):
                        continue                        # 制約行（PRIMARY KEY 等）は列にしない
                    im = _LEADING_IDENT.match(clause)
                    if not im:
                        continue
                    col_name = _unquote_or_normalize(im.group(1))
                    if not col_name:
                        continue
                    col_line = _line_at(newline_offsets, paren_start + 1 + offset)
                    children.append(DefItem(label="DataItem", name=col_name,
                                             cid_key=f"{table_name}.{col_name}", line=col_line))
            # CTAS（`has_columns=False`）は列を持たない（`SELECT` 側のスキーマ推論はしない）。

            seen_table_names.add(table_name)
            extra: dict = {}
            if schema_raw:
                extra["schema"] = _unquote_or_normalize(schema_raw)
            item = DefItem(label="Table", name=table_name, line=header_line, extra=extra)
            groups.append((item, children))

        for m in _ALTER_TABLE.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            dropped.append(Dropped("ddl_unsupported", line, _snippet_line(lines_raw, line)))

        # `CREATE TABLE`/CTAS 以外の `CREATE`（未知の方言も含む）は残らず ddl_unsupported にする
        # （黙って落とさない）——個別の方言名は列挙しない包括判定。
        for m in _CREATE_ANY.finditer(sanitized):
            if m.start() in handled_create_starts:
                continue
            line = _line_at(newline_offsets, m.start())
            dropped.append(Dropped("ddl_unsupported", line, _snippet_line(lines_raw, line)))

        if not groups:
            dm = _DML_LEAD.search(sanitized)
            if dm and not any(d.reason == "ddl_unsupported" for d in dropped):
                line = _line_at(newline_offsets, dm.start())
                dropped.append(Dropped("dml_only", line, _snippet_line(lines_raw, line)))
            return DefResult(dropped=dropped)

        primary_item, primary_children = groups[0]
        extras = [DefGroup(primary=grp_item, children=grp_children)
                  for grp_item, grp_children in groups[1:]]
        return DefResult(primary=primary_item, children=primary_children, extras=extras, dropped=dropped)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        return RefResult()
