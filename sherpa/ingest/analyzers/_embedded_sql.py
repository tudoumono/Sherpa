"""ソースの中の SQL を読む共通の部品（言語を問わない）。`EXEC SQL ... ;`（または `END-EXEC`）の文と、文字列リテラルに書かれた SQL から表の参照を取り出す。
読むのは文の形だけ（表の名前の抽出は `_sql_scan`）。コメントの書き方と文字列の引用符は呼び出す言語の解析器が渡す。
設計: docs/05-グラフ語彙.md（ACCESSES の via=exec_sql／string_sql）・docs/design/scope.md「ビルドの定義と実行名」と同じ「言語を問わない」方針
"""
from __future__ import annotations

import re
from dataclasses import dataclass

from . import _sql_scan
from ._base import Dropped, RefCandidate

_EXEC_SQL = re.compile(r"\bEXEC[ \t\r\n]+SQL\b", re.IGNORECASE)
_END_EXEC = re.compile(r"\bEND-EXEC\b", re.IGNORECASE)
# 文字列の SQL とみなす文の形（動詞の語の一覧ではなく、表の句を持つ文の骨組み）
_NAME = r"[\w\"`\[][^\s(;]*"
_ALIAS = r"(?:\s+(?:AS\s+)?\w+)?"
_STATEMENT_SHAPES = tuple(re.compile(p, re.IGNORECASE | re.DOTALL) for p in (
    r"^\s*(?:WITH\b.*)?SELECT(?=[\s*(])\s*(?!FROM\b)\S.*?\bFROM\s*[\w\"`\[(]",   # 選択式が空でない（`SELECT*` のように詰めて書いてもよい）
    rf"^\s*INSERT\s+INTO\s+{_NAME}\s*(?:\(|VALUES\b|SELECT\b|DEFAULT\b)",
    rf"^\s*UPDATE\s+{_NAME}{_ALIAS}\s+SET\s*(?:[\w\"`\[][^\s=]*\s*=|\([^()]*\)\s*=)",   # 代入は 1 項目か、組（`(A,B)=(…)`）
    rf"^\s*DELETE\s+FROM\s+{_NAME}(?:{_ALIAS}\s+WHERE\b|\s*;|\s*$)",
    rf"^\s*MERGE\s+INTO\s+{_NAME}\s+(?:AS\s+\w+\s+)?USING\b"))
_DYNAMIC_AFTER = frozenset("%?${")
_ESCAPES = {"n": " ", "t": " ", "r": " ", "\\": "\\", '"': '"', "'": "'"}
_SNIPPET_MAX = 120


@dataclass
class Segment:
    kind: str   # "code"／"comment"／"string"
    start: int
    end: int


def lex(text: str, *, line_comments=("//",), block_comment=("/*", "*/"), quotes=('"',)) -> list:
    """コメントと文字列リテラルの範囲に分ける。文字列は `\\` で次の 1 文字を飛ばし、閉じなければ行末で終わる。"""
    segs: list = []
    i, n, code_from = 0, len(text), 0

    def flush(upto: int) -> None:
        if upto > code_from:
            segs.append(Segment("code", code_from, upto))

    while i < n:
        ch = text[i]
        if block_comment and text.startswith(block_comment[0], i):
            flush(i)
            end = text.find(block_comment[1], i + len(block_comment[0]))
            end = n if end < 0 else end + len(block_comment[1])
            segs.append(Segment("comment", i, end))
            i = code_from = end
        elif any(text.startswith(p, i) for p in line_comments):
            flush(i)
            end = text.find("\n", i)
            end = n if end < 0 else end
            segs.append(Segment("comment", i, end))
            i = code_from = end
        elif ch in quotes:
            flush(i)
            j = i + 1
            while j < n and text[j] != ch and text[j] != "\n":
                j += 2 if text[j] == "\\" else 1
            end = min(j + 1, n) if j < n and text[j] == ch else min(j, n)
            segs.append(Segment("string", i, end))
            i = code_from = end
        else:
            i += 1
    flush(n)
    return segs


def _blank(text: str, spans) -> str:
    out = list(text)
    for lo, hi in spans:
        for k in range(lo, min(hi, len(out))):
            if out[k] != "\n":
                out[k] = " "
    return "".join(out)


def _line_of(text: str, pos: int) -> int:
    return text.count("\n", 0, pos) + 1


def _exec_sql_end(text: str, pos: int) -> int:
    """`EXEC SQL` の後ろから、文の終わり（引用符の外の `;`、または `END-EXEC`）の次の位置を返す。無ければ文末。"""
    quote = None
    i, n = pos, len(text)
    while i < n:
        ch = text[i]
        if quote:
            quote = None if ch == quote else quote
        elif ch in "'\"":
            quote = ch
        elif ch == ";":
            return i + 1
        elif ch in "eE":
            m = _END_EXEC.match(text, i)
            if m and (i == 0 or not (text[i - 1].isalnum() or text[i - 1] == "_")):
                return m.end()
        i += 1
    return n


def read_embedded_sql(text: str, **lex_kw) -> tuple:
    """`(EXEC SQL の文を空白にした元の文字列, 表の参照の一覧, 申告の一覧)` を返す。
    ① コメントを除いて見る ② `EXEC SQL` の文を表の参照にして空白にする（その言語の構文解析に渡さない） ③ 残りの文字列リテラルから SQL の文を表の参照にする。
    文字列は隣り合うもの（間が空白とコメントだけ、または `+`・`&`）を 1 つにつなげて読む。参照の行は文・文字列の始まりの行。
    守ること: 文字数と改行の位置を変えない（行番号を保つ）。"""
    segs = lex(text, **lex_kw)
    plain = _blank(text, [(s.start, s.end) for s in segs if s.kind == "comment"])
    refs: list = []
    dropped: list = []
    blocks: list = []
    pos = 0
    in_code = _blank(plain, [(s.start, s.end) for s in segs if s.kind == "string"])   # 文字列の中の `EXEC SQL` は文の始まりにしない
    while True:
        m = _EXEC_SQL.search(in_code, pos)
        if not m:
            break
        end = _exec_sql_end(plain, m.end())
        blocks.append((m.start(), end))
        body = plain[m.end():end]
        if body.rstrip().endswith(";"):
            body = body.rstrip()[:-1]
        sanitized = _sql_scan.sanitize(body)
        line0 = _line_of(plain, m.start())
        for name, offset in _sql_scan.table_refs(sanitized):
            refs.append(RefCandidate("ACCESSES", "Table", name, line0 + body.count("\n", 0, offset), extra={"via": "exec_sql"}))
        pos = end
    code = _blank(plain, blocks)
    string_refs, string_dropped = _string_sql(code, [s for s in lex(code, **lex_kw) if s.kind != "comment"])
    return _blank(text, blocks), refs + string_refs, dropped + string_dropped


def _unescape(raw: str) -> str:
    out: list = []
    i = 0
    while i < len(raw):
        if raw[i] == "\\" and i + 1 < len(raw):
            out.append(_ESCAPES.get(raw[i + 1], raw[i + 1]))
            i += 2
        else:
            out.append(raw[i])
            i += 1
    return "".join(out)


def _string_groups(text: str, segs: list) -> list:
    """隣り合う文字列リテラルのまとまり `[(始まりの位置, つなげた中身)]`。間が空白・`+`・`&` だけのものをつなぐ。"""
    groups: list = []
    prev_end = None
    for s in segs:
        if s.kind != "string":
            continue
        body = _unescape(text[s.start + 1:s.end - 1] if text[s.end - 1:s.end] == text[s.start] else text[s.start + 1:s.end])
        gap = text[prev_end:s.start] if prev_end is not None else None
        if groups and gap is not None and not gap.strip(" \t\r\n+&"):
            groups[-1][1].append(body)
        else:
            groups.append((s.start, [body]))
        prev_end = s.end
    return [(start, "".join(parts)) for start, parts in groups]


def _dynamic_name(text: str, start: int, end: int) -> bool:
    """表名の語が、実行時に決まる部分とつながっているか。前後が動的な印のとき、または語の後ろに修飾の区切り `.` が続いて名前が完結しないとき。"""
    rest = text[end:].lstrip()
    return (start > 0 and text[start - 1] in _DYNAMIC_AFTER) or (end < len(text) and text[end] in _DYNAMIC_AFTER) or rest.startswith(".")


def _string_sql(text: str, segs: list) -> tuple:
    refs: list = []
    dropped: list = []
    for start, combined in _string_groups(text, segs):
        if not any(p.search(combined) for p in _STATEMENT_SHAPES):
            continue
        line = _line_of(text, start)
        for name, start_pos, end in _sql_scan.table_ref_spans(_sql_scan.sanitize(combined)):   # 原文の上の位置で動的な部分かを見る
            if _dynamic_name(combined, start_pos, end):
                dropped.append(Dropped("string_sql_dynamic_table", line, " ".join(combined.split())[:_SNIPPET_MAX]))
                continue
            refs.append(RefCandidate("ACCESSES", "Table", name, line, extra={"via": "string_sql"}))
    return refs, dropped
