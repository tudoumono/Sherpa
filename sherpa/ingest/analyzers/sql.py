"""SQL/DDL アナライザ。`CREATE TABLE [schema.]NAME (...)`（方言・CTAS 含む）を `Table` 定義（primary＝最初の1件・2件目以降は `DefResult.extras`）として返し、列定義を `DataItem` の children（`cid_key="TABLE.COLUMN"`）にする。

制約行（`PRIMARY KEY`/`FOREIGN KEY`/`CONSTRAINT`/`UNIQUE`/`INDEX`/`KEY`/`CHECK`）は列にしない。CTAS は列を持たない。schema 除去後に同名の `Table` が複数出る場合、2件目以降は `Dropped("table_name_collision")`。
`CREATE TABLE` 以外の `CREATE`・`ALTER TABLE`・DML のみのファイルは `Dropped("ddl_unsupported"/"dml_only")` で申告する。`extract_refs` は常に空（`Table` への参照は COBOL の EXEC SQL・MyBatis SQL 側が持つ）。
識別子は引用符付きならそのまま、非引用なら `identifiers.normalize_code_name()` と同じ規則で大文字化する。schema は `extra={"schema": ...}` に保持し `name` には NAME だけを使う。コメント・文字列リテラルは `_sql_scan` でサニタイズして無視する。正規表現＋行走査。
設計: docs/design/rag.md「グラフ」
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

# `CREATE TABLE NAME AS SELECT ...`（CTAS・列リストなし）。
_CREATE_TABLE_AS_SELECT = re.compile(
    _CREATE_TABLE_PREFIX + r"(?:IF\s+NOT\s+EXISTS\s+)?"
    r"(?P<name>" + _IDENT_TOKEN + r"(?:\s*\.\s*" + _IDENT_TOKEN + r")?)"
    r"\s+AS\s+SELECT\b",
    re.IGNORECASE,
)

# `CREATE TABLE`/`ALTER TABLE` 以外の未対応 DDL/DML（定義・参照は作らず Dropped のみ）。`CREATE` は方言を列挙せず包括的に検知する（`_CREATE_ANY`）。`CREATE TABLE`/CTAS として認識済みの位置を除いた残りが対象。
_ALTER_TABLE = re.compile(r"\bALTER\s+TABLE\b", re.IGNORECASE)
_CREATE_ANY = re.compile(r"\bCREATE\b", re.IGNORECASE)

_DML_LEAD = re.compile(r"\b(?:SELECT|INSERT\s+INTO|UPDATE|DELETE\s+FROM)\b", re.IGNORECASE)

_LEADING_IDENT = re.compile(r"^\s*(" + _IDENT_TOKEN + r")")
_CONSTRAINT_LEAD = re.compile(
    r"^\s*(?:PRIMARY|FOREIGN|CONSTRAINT|UNIQUE|INDEX|KEY|CHECK)\b", re.IGNORECASE)


def _sanitize(text: str) -> str:
    """`_sql_scan.sanitize()` のエイリアス。`hash_line_comments` は既定 `False`（DB2 の DDL では `#` が識別子文字）。"""
    return _sql_scan.sanitize(text)


def _newline_offsets(text: str) -> list:
    """`text` 内の全改行位置（昇順）。`_line_at` が `bisect` で引く。"""
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
    """`_sql_scan.unquote_or_norm_ident()` のエイリアス。"""
    return _sql_scan.unquote_or_norm_ident(token)


def _split_schema_qualified(full: str) -> tuple:
    """`[schema.]NAME` を分解する（`.` は引用符の外側でのみ区切り）。戻り値は `(schema_raw|None, name_raw)`（引用符を残した生のトークン）。"""
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
    """`CREATE TABLE`（方言・CTAS 含む）→ `Table`（primary/`extras`）。列 → `DataItem`（`CONTAINS`）。schema 除去後の同名 Table・`CREATE TABLE`/CTAS 以外の `CREATE`・`ALTER TABLE`・DML のみは `Dropped`。`extract_refs` は常に空。"""

    name = "sql"
    extensions = SQL_EXT
    doctype = "sql"

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        sanitized = _sanitize(text)
        newline_offsets = _newline_offsets(sanitized)
        lines_raw = text.splitlines()
        dropped: list = []
        groups: list = []  # [(DefItem(Table), [DefItem(DataItem), ...]), ...]
        seen_table_names: set = set()  # schema 除去後の名前（cid 衝突検知）
        handled_create_starts: set = set()  # `CREATE TABLE`/CTAS として認識済みの出現位置

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

            if table_name in seen_table_names:  # schema 除去後の同名 Table は cid が衝突する
                raw_full = f"{schema_raw}.{name_raw}" if schema_raw else name_raw
                dropped.append(Dropped("table_name_collision", header_line, raw_full[:120]))
                continue

            children: list = []
            if has_columns:
                paren_start = m.end() - 1
                close = _match_paren(sanitized, paren_start)
                if close is None:  # 対応する `)` が無い＝解釈しない（黙って消さない）
                    dropped.append(Dropped("ddl_unsupported", header_line,
                                           _snippet_line(lines_raw, header_line)))
                    continue
                inner = sanitized[paren_start + 1:close]
                for offset, clause in _split_top_level(inner):
                    stripped = clause.strip()
                    if not stripped or _CONSTRAINT_LEAD.match(stripped):
                        continue  # 制約行（PRIMARY KEY 等）は列にしない
                    im = _LEADING_IDENT.match(clause)
                    if not im:
                        continue
                    col_name = _unquote_or_normalize(im.group(1))
                    if not col_name:
                        continue
                    col_line = _line_at(newline_offsets, paren_start + 1 + offset)
                    children.append(DefItem(label="DataItem", name=col_name,
                                             cid_key=f"{table_name}.{col_name}", line=col_line))
            # CTAS は列を持たない。

            seen_table_names.add(table_name)
            extra: dict = {}
            if schema_raw:
                extra["schema"] = _unquote_or_normalize(schema_raw)
            item = DefItem(label="Table", name=table_name, line=header_line, extra=extra)
            groups.append((item, children))

        for m in _ALTER_TABLE.finditer(sanitized):
            line = _line_at(newline_offsets, m.start())
            dropped.append(Dropped("ddl_unsupported", line, _snippet_line(lines_raw, line)))

        # `CREATE TABLE`/CTAS 以外の `CREATE`（未知の方言も含む）は残らず ddl_unsupported にする。
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
