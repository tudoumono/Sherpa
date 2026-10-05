"""標準 COBOL アナライザ。`PROGRAM-ID` を主体定義（`Module`）とし、`COPY`/`CALL`/`EXEC SQL`/`EXEC CICS` を参照候補として返す。

- `COPY` → `Copybook` 参照、`CALL 'X'` → `Module` 参照（INVOKES）、動的 CALL は `Dropped`。`CALL` の語が文字列リテラルの内側にあるもの（`DISPLAY "CALL 'X'"`）は呼び出しにしない（`CALL` 文のオペランドの引用符つきプログラム名は残す）。
- `EXEC SQL ... END-EXEC`（複数物理行にまたがる）は、`FROM`/`JOIN`/`INSERT INTO`/`MERGE INTO`/`UPDATE`/`DELETE FROM` 直後のテーブル名を `ACCESSES`→`Table`（`via=exec_sql`）で返す。サニタイズ・テーブル名抽出は `_sql_scan` と共通。`DECLARE ... CURSOR FOR SELECT ...`（静的カーソル）は表への参照にし、準備済みの文の名前を指すカーソル（`CURSOR FOR STMT1`）と `EXECUTE IMMEDIATE` は `Dropped("exec_sql_dynamic")`。
- `EXEC CICS` は `XCTL`/`LINK` の `PROGRAM('X')` を `INVOKES`（`via=cics_xctl`/`cics_link`）で返す。識別子（動的）は `Dropped("cics_dynamic")`、他コマンドはブロックごとに `Dropped("cics_other")` 1件。
- 文字列リテラル中の語を拾わないため、開始検索は引用文字列を空白化した文字列で行う。`END-EXEC` は `_find_end_exec` が文字列・コメントの外だけで認識する。継続行結合した論理行でも `--` コメントは物理行境界を越えない。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import bisect
import re

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (COBOL_EXT, _CALL, _COPY, _DYNAMIC_CALL,
                               _PROGRAM_ID, _blank_pseudo_text, _is_comment, _is_free_format,
                               _normalize_logical_lines, _split_statements,
                               _strip_inline_comment, _strip_quoted)
from . import _sql_scan
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

# EXEC SQL/EXEC CICS: ブロック境界・動的 SQL 検知の正規表現。`EXEC SQL`/`EXEC CICS` を1つの正規表現で拾い、種別で分岐する。
_EXEC_BLOCK_START = re.compile(r"\bEXEC\s+(SQL|CICS)\b", re.IGNORECASE)
_END_EXEC = re.compile(r"\bEND-EXEC\b", re.IGNORECASE)
# カーソル名の後・`CURSOR` の前に属性（`SCROLL`・`NO SCROLL`・`ASENSITIVE` など）を置ける。
_DECLARE_HEAD = r"\bDECLARE\s+\S+\s+(?:[A-Za-z]+\s+){0,4}?CURSOR\b"
_DECLARE_CURSOR = re.compile(_DECLARE_HEAD, re.IGNORECASE)
# `DECLARE c CURSOR [WITH HOLD …] FOR <対象>` の対象の先頭語。SELECT/WITH/VALUES/`(` なら静的、それ以外（準備済みの文の名前・ホスト変数）は動的。
_DECLARE_CURSOR_FOR = re.compile(
    _DECLARE_HEAD + r"[^;]*?\bFOR\s+\(*\s*(?P<target>[A-Za-z_:][\w-]*)", re.IGNORECASE)
_STATIC_CURSOR_TARGETS = frozenset({"SELECT", "WITH", "VALUES"})
_EXECUTE_IMMEDIATE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)


def _blank_cobol_strings(s: str) -> str:
    """COBOL 引用文字列（`'...'`／`"..."`、`''`/`""` エスケープ対応）の中身を同じ長さの空白へ置換する（`EXEC SQL` の開始検索用。長さを保つのでマッチ位置を元の `code` へそのまま使える）。"""
    buf: list = []
    i, n = 0, len(s)
    quote: str | None = None
    while i < n:
        ch = s[i]
        if quote is None:
            if ch in ("'", '"'):
                quote = ch
                buf.append(" ")
            else:
                buf.append(ch)
            i += 1
            continue
        if ch == quote:
            if s[i + 1:i + 2] == quote:  # エスケープ（連続する2個の同種引用符）
                buf.append("  ")
                i += 2
                continue
            buf.append(" ")
            quote = None
            i += 1
            continue
        buf.append(" ")
        i += 1
    return "".join(buf)


def _has_dynamic_cursor(sanitized: str) -> bool:
    """`DECLARE ... CURSOR` のうち、`FOR` の対象が SELECT/WITH/VALUES でないもの（準備済みの文の名前など）があるか。`FOR` が読めない宣言も動的として扱う。"""
    for dm in _DECLARE_CURSOR.finditer(sanitized):
        fm = _DECLARE_CURSOR_FOR.match(sanitized, dm.start())
        if fm is None or fm.group("target").upper() not in _STATIC_CURSOR_TARGETS:
            return True
    return False


def _sanitize_cics_span(text: str, state: str | None) -> tuple:
    """CICS 用の1断片サニタイズ。単一・二重引用符の両方（`''`／`""` エスケープ対応）を文字列として空白化する（`--`/`/* */` は扱わない）。

    `state`（`None`／`"string"`／`"string_double"`）は直前の断片から持ち越した走査状態で、戻り値の2つ目に走査後の状態を返す。
    """
    buf: list = []
    i, n = 0, len(text)
    while i < n:
        if state == "string":
            if text[i:i + 2] == "''":
                buf.append("  ")
                i += 2
                continue
            if text[i] == "'":
                buf.append(" ")
                i += 1
                state = None
                continue
            buf.append(" ")
            i += 1
            continue
        if state == "string_double":
            if text[i:i + 2] == '""':
                buf.append("  ")
                i += 2
                continue
            if text[i] == '"':
                buf.append(" ")
                i += 1
                state = None
                continue
            buf.append(" ")
            i += 1
            continue
        if text[i] == "'":
            buf.append(" ")
            i += 1
            state = "string"
            continue
        if text[i] == '"':
            buf.append(" ")
            i += 1
            state = "string_double"
            continue
        buf.append(text[i])
        i += 1
    return "".join(buf), state


def _find_end_exec(text: str, state: str | None, boundaries: tuple = (),
                    kind: str = "SQL") -> tuple:
    """`text` 中の実際の `END-EXEC` を探す（文字列・コメント中の `END-EXEC` では終端しない）。

    `kind`（`"SQL"`／`"CICS"`）でサニタイズを分岐する（SQL は `_sql_scan.sanitize_span`、CICS は `_sanitize_cics_span`）。戻り値は `(match_or_None, 走査後の state)`。`boundaries` は `"SQL"` のときだけ使う（継続結合された物理行境界で `--` を止める）。
    """
    if kind == "CICS":
        sanitized, state = _sanitize_cics_span(text, state)
    else:
        sanitized, state = _sql_scan.sanitize_span(text, state, boundaries=boundaries)
    return _END_EXEC.search(sanitized), state


def _seg_bounds(segs: tuple, start: int, end: int | None = None) -> tuple:
    """論理行全体の物理行境界オフセット `segs` から、部分文字列 `text[start:end)` に含まれる境界だけをスライス相対へ変換して返す（`_find_end_exec` の `boundaries` 用。`start` 自身は含めない）。"""
    return tuple(s - start for s in segs if s > start and (end is None or s < end))


def _sanitize_exec_sql_fragments(block_frags: list) -> str:
    """ブロックの断片（`(物理行, テキスト, 断片内の物理行境界オフセット)` の列）を結合しつつ、`_sql_scan.sanitize_span` でコメント・文字列を同じ長さの空白へ置換する。未閉じのコメント／文字列の状態は断片をまたいで持ち越す。結果は `" ".join(...)` と同じ長さ。"""
    state: str | None = None  # None／"block_comment"／"string"
    parts: list = []
    for _ln, frag, boundaries in block_frags:
        sanitized, state = _sql_scan.sanitize_span(frag, state, boundaries=boundaries)
        parts.append(sanitized)
    return " ".join(parts)


def _exec_sql_block_refs(block_frags: list, start_line: int) -> tuple:
    """1つの `EXEC SQL ... END-EXEC` ブロックから `ACCESSES`→`Table` 候補を抽出する。戻り値は `(refs, dropped)`。

    準備済みの文の名前を指す `DECLARE ... CURSOR`／`EXECUTE IMMEDIATE` を含むブロックはテーブル抽出をせず `Dropped("exec_sql_dynamic")` で申告する（判定はサニタイズ済み本文に対して行う）。`CURSOR FOR SELECT ...` の静的カーソルは通常の SQL と同じくテーブル名を抽出する。テーブル名抽出は `_sql_scan.table_refs`。
    """
    combined = " ".join(t for _ln, t, _b in block_frags)
    sanitized = _sanitize_exec_sql_fragments(block_frags)
    if _has_dynamic_cursor(sanitized) or _EXECUTE_IMMEDIATE.search(sanitized):
        return [], [Dropped("exec_sql_dynamic", start_line, combined.strip()[:120])]

    offset_starts: list = []
    offset_lines: list = []
    pos = 0
    for ln, t, _b in block_frags:
        offset_starts.append(pos)
        offset_lines.append(ln)
        pos += len(t) + 1  # +1 は結合時に挟んだ半角空白

    def _line_at(p: int) -> int:
        idx = bisect.bisect_right(offset_starts, p) - 1
        return offset_lines[idx if idx >= 0 else 0]

    refs: list = []
    for name, offset in _sql_scan.table_refs(sanitized):
        refs.append(RefCandidate("ACCESSES", "Table", name, _line_at(offset),
                                  extra={"via": "exec_sql"}))
    return refs, []


# EXEC CICS: XCTL/LINK の PROGRAM(...) 引数だけを読む。それ以外は cics_other。ブロック先頭のコマンド語を読む正規表現。
_CICS_COMMAND_WORD = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)")
# `PROGRAM(` の引数（引用リテラル＝静的／識別子＝動的）。CALL の引用符規則と同じ。
_CICS_PROGRAM_ARG = re.compile(
    r"\bPROGRAM\s*\(\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z][\w-]*))\s*\)", re.IGNORECASE)
# `PROGRAM` キーワード位置の探索用（`_blank_cobol_strings` した文字列上でだけ使う）。
_CICS_PROGRAM_KEYWORD = re.compile(r"\bPROGRAM\b", re.IGNORECASE)


def _exec_cics_block_refs(block_frags: list, start_line: int) -> tuple:
    """1つの `EXEC CICS ... END-EXEC` ブロックから `INVOKES`→`Module` 候補を抽出する。戻り値は `(refs, dropped)`。

    先頭コマンドが `XCTL`/`LINK` のときだけ `PROGRAM(...)` を読む。引用リテラルなら静的な呼び出し先、識別子なら `Dropped("cics_dynamic")`。他のコマンドはブロックごとに `Dropped("cics_other", line, <コマンド名>)` を1件だけ申告する。
    `PROGRAM` の探索は引用文字列を空白化した文字列で行い、引数の読み取りは未サニタイズの `combined` に対して行う。
    """
    combined = " ".join(t for _ln, t, _b in block_frags)
    stripped = combined.strip()
    cmd_m = _CICS_COMMAND_WORD.match(stripped)
    command = cmd_m.group(1).upper() if cmd_m else ""
    if command not in ("XCTL", "LINK"):
        return [], [Dropped("cics_other", start_line, command or stripped[:40])]
    via = "cics_xctl" if command == "XCTL" else "cics_link"

    offset_starts: list = []
    offset_lines: list = []
    pos = 0
    for ln, t, _b in block_frags:
        offset_starts.append(pos)
        offset_lines.append(ln)
        pos += len(t) + 1  # +1 は結合時に挟んだ半角空白

    def _line_at(p: int) -> int:
        idx = bisect.bisect_right(offset_starts, p) - 1
        return offset_lines[idx if idx >= 0 else 0]

    blanked = _blank_cobol_strings(combined)
    m = None
    for km in _CICS_PROGRAM_KEYWORD.finditer(blanked):
        cand = _CICS_PROGRAM_ARG.match(combined, km.start())
        if cand:
            m = cand
            break
    if not m:
        return [], [Dropped("cics_dynamic", start_line, stripped[:120])]
    line = _line_at(m.start())
    literal = m.group(1) if m.group(1) is not None else m.group(2)
    if literal is not None:
        return [RefCandidate("INVOKES", "Module", _norm(literal), line, extra={"via": via})], []
    return [], [Dropped("cics_dynamic", line, stripped[:120])]

# 抽出は `_normalize_logical_lines` が返す論理行（固定形式は連番・識別領域を落とし継続行を結合済み／自由形式は物理行そのまま）に対して行う。


class CobolAnalyzer(Analyzer):
    """`PROGRAM-ID` → `Module`。`COPY` → `Copybook` 参照（COPIES）。`CALL` → `Module` 参照（INVOKES）。"""

    name = "cobol"
    extensions = frozenset(COBOL_EXT)
    doctype = "cobol"
    version = 3

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        free_format = _is_free_format(text)
        entries, debug_dropped = _normalize_logical_lines(text, free_format)
        dropped = [Dropped("debug_line", ln, snippet) for ln, snippet in debug_dropped]
        pid = next((_norm(m) for logical, _ln, _segs in entries
                    if not _is_comment(logical)
                    for m in _PROGRAM_ID.findall(logical)), None)
        if not pid:
            return DefResult(dropped=dropped)
        return DefResult(primary=DefItem(label="Module", name=pid), dropped=dropped)

    @staticmethod
    def _process_normal_segment(code_part: str, logical_part: str, i: int,
                                 refs: list, dropped: list) -> None:
        """EXEC SQL ブロックの外側の1区間（COPY/CALL/動的 CALL 検知）。`code_part`/`logical_part` は同一物理行の一部分のことがあり、同じ開始位置を共有する（動的 CALL 検知だけは行末コメント切り捨て前の `logical_part` を使う）。"""
        code_part, logical_part = _blank_pseudo_text(code_part), _blank_pseudo_text(logical_part)  # REPLACING の pseudo-text は読まない
        # COPY 抽出だけは引用文字列の中身も除去する（CALL は引用符内のプログラム名を読むため適用しない）。
        for cb in _COPY.findall(_strip_quoted(code_part)):
            refs.append(RefCandidate("COPIES", "Copybook", _norm(cb), i))
        # CALL 文のオペランド（引用符つきプログラム名）だけを呼び出しにする。`CALL` の語そのものが文字列リテラルの内側にあるもの（`DISPLAY "CALL 'X'"`）は除く（リテラル内は `_blank_cobol_strings` で空白になる）。
        blanked = _blank_cobol_strings(code_part)
        for cm in _CALL.finditer(code_part):
            if blanked[cm.start()] == " ":
                continue
            refs.append(RefCandidate("INVOKES", "Module", _norm(cm.group(1)), i,
                                      extra={"via": "call"}))
        # 動的 CALL の検知は、インラインコメント切り捨て前の論理行に対して行う。
        for stmt in _split_statements(logical_part):
            # `_strip_quoted` が文字列リテラルの中身を空にするため、残る `CALL` はすべて動的呼び出し。出現ごとに個別判定する。
            for _callee in _DYNAMIC_CALL.findall(_strip_quoted(stmt)):
                # 静的には解決できないため解析せず記録だけする。
                dropped.append(Dropped("dynamic_call", i, stmt.strip()[:120]))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        refs: list = []
        free_format = _is_free_format(text)
        # デバッグ行（7桁目 `D`）は `collect_defs`（Pass1）側だけが扱う（Pass1/Pass2 で二重に `flags` へ記録しない）。
        entries, _debug_dropped = _normalize_logical_lines(text, free_format)
        dropped: list = []
        after_id = False
        exec_block: list | None = None  # None＝ブロック外／list＝EXEC SQL/CICS ブロック収集中（(行, 断片, 断片内境界)の列）
        exec_start_line: int | None = None
        exec_kind: str | None = None  # "SQL"／"CICS"（収集中のブロック種別）
        sql_state: str | None = None  # 境界スキャナの持ち越し状態
                                           # None／"block_comment"／"string"／"string_double"
        for logical, i, segs in entries:
            if _is_comment(logical):
                continue
            if not after_id:
                if _PROGRAM_ID.search(logical):
                    after_id = True
                continue
            # COPY/CALL/EXEC のリテラル抽出は、行末インラインコメント（`*>` 以降）を先に切り捨ててから行う。`code` は `logical` の先頭部分なので位置を共有する（`segs` もそのまま使える）。
            code = _strip_inline_comment(logical)

            # 位置カーソルで論理行を左から走査する。`EXEC` ブロックの前後・同一行の複数ブロックも通常の COPY/CALL 処理へ回す。開始検索は `_blank_cobol_strings` の結果に対して、終端は `_find_end_exec` に対して行う（`_seg_bounds(segs, ...)` を渡し、`--` が物理行境界を越えないようにする）。
            pos = 0
            while True:
                if exec_block is not None:  # EXEC SQL/CICS ブロック収集中（前の行から継続）
                    em, sql_state = _find_end_exec(code[pos:], sql_state, _seg_bounds(segs, pos),
                                                    exec_kind)
                    if em is None:
                        exec_block.append((i, code[pos:], _seg_bounds(segs, pos)))
                        break  # このエントリの残りは丸ごとブロックへ持ち越す
                    exec_block.append((i, code[pos:pos + em.start()],
                                        _seg_bounds(segs, pos, pos + em.start())))
                    if exec_kind == "SQL":
                        block_refs, block_dropped = _exec_sql_block_refs(exec_block, exec_start_line)
                    else:
                        block_refs, block_dropped = _exec_cics_block_refs(exec_block, exec_start_line)
                    refs.extend(block_refs)
                    dropped.extend(block_dropped)
                    exec_block = None
                    exec_kind = None
                    sql_state = None
                    pos += em.end()
                    continue

                sm = _EXEC_BLOCK_START.search(_blank_cobol_strings(code[pos:]))
                if sm is None:  # 残り全部が通常コード（EXEC なし）
                    self._process_normal_segment(code[pos:], logical[pos:], i, refs, dropped)
                    break

                self._process_normal_segment(code[pos:pos + sm.start()], logical[pos:pos + sm.start()],
                                              i, refs, dropped)
                kind = sm.group(1).upper()
                after_start_pos = pos + sm.end()
                em, sql_state = _find_end_exec(code[after_start_pos:], None,
                                                _seg_bounds(segs, after_start_pos), kind)
                if em is None:  # ブロックが次行以降へ続く
                    exec_block = [(i, code[after_start_pos:], _seg_bounds(segs, after_start_pos))]
                    exec_start_line = i
                    exec_kind = kind
                    break
                frag = [(i, code[after_start_pos:after_start_pos + em.start()],
                         _seg_bounds(segs, after_start_pos, after_start_pos + em.start()))]
                if kind == "SQL":
                    block_refs, block_dropped = _exec_sql_block_refs(frag, i)
                else:
                    block_refs, block_dropped = _exec_cics_block_refs(frag, i)
                refs.extend(block_refs)
                dropped.extend(block_dropped)
                sql_state = None
                pos = after_start_pos + em.end()  # 同一行の続き（suffix）をさらに走査する

        if exec_block is not None:  # END-EXEC 無いまま EOF＝未終端ブロック
            combined = " ".join(t for _ln, t, _b in exec_block).strip()
            reason = "exec_sql_dynamic" if exec_kind == "SQL" else "cics_dynamic"
            dropped.append(Dropped(reason, exec_start_line, combined[:120]))

        return RefResult(refs=refs, dropped=dropped)
