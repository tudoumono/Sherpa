"""標準 COBOL アナライザ（docs/05-グラフ語彙.md §4 トラック S）。

`PROGRAM-ID` を主体定義（`Module`）とし、`COPY`/`CALL`/`EXEC SQL` を参照候補として返す。標準1本
（ベンダー差の概念は持たない・§7 裁定1）。標準で解釈できない構文（動的 CALL＝識別子呼び出し・
動的 SQL 等）は解析せず `dropped` に記録して落とす（黙って消さない）。

`EXEC SQL ... END-EXEC` ブロック（アナライザ拡張 §4(d)）は物理行をまたいで（COBOL の継続行
マーカーを伴わず素朴に複数物理行へ分かれるのが通例）出現するため、`entries` を状態機械で走査し
ブロックの断片を集めてから `FROM`/`JOIN`/`INSERT INTO`/`MERGE INTO`/`UPDATE`/`DELETE FROM` の直後のテーブル名を
`RefCandidate("ACCESSES", "Table", ..., extra={"via": "exec_sql"})` として返す。サニタイズ・
テーブル名抽出・識別子正規化（引用符付きはそのまま／非引用は大文字化）は DDL 側
（`analyzers/sql.py`）と共通の `_sql_scan`（`sanitize_span`/`table_refs`/`unquote_or_norm_ident`）
に委ねる（§4(g)）。`EXEC SQL` の開始検索は COBOL 引用文字列（`'...'`/`"..."`）を同長の空白に
置換した文字列に対して行い（文字列リテラル中の偽陽性語を拾わない）、ブロック収集中は
`_sql_scan.sanitize_span` の走査状態（`--`・`/* */`・単一引用符文字列）を断片（物理行）をまたいで
持ち越す境界スキャナ（`_find_end_exec`）で実際の `END-EXEC` だけを認識する（文字列/コメント内の
`END-EXEC` で終了しない）。`DECLARE ... CURSOR`／`EXECUTE IMMEDIATE`（動的 SQL）を含むブロックは
テーブル抽出をせず `Dropped("exec_sql_dynamic", ...)` として申告する。同一物理行に
`EXEC SQL ... END-EXEC` の前後（prefix/suffix）や複数ブロックが同居する場合も、位置カーソルで
区切って前後を通常の COPY/CALL/次の EXEC SQL 処理へ回す（取りこぼさない）。

`EXEC CICS ... END-EXEC` ブロックも同じ境界スキャナ（`_find_end_exec`・`segment_starts` の流儀）で
走査する——開始検索は `EXEC SQL` と同じ正規表現（`EXEC` の後続が `SQL`/`CICS` のどちらかで分岐）を
使う。`END-EXEC` の終端判定はブロック種別（`kind="SQL"`/`"CICS"`）で保護規則が分かれる——SQL は
`_sql_scan.sanitize_span` で単一引用符文字列・`--`・`/* */` の状態を維持する一方、CICS は
cobol.py 側の `_sanitize_cics_span` で単一・二重引用符の両方（`''`／`""` エスケープ対応）を
保護し `--`/`/* */` は扱わない（CICS にコメント構文はなく、SQL とは異なり `"..."` はここでは
識別子ではなく文字列として保護する必要があるため共通スキャナには寄せない）。ブロック本文の
先頭コマンドが `XCTL`/`LINK` のときだけ `PROGRAM(...)` の引数を読み、引用リテラルなら
`RefCandidate("INVOKES", "Module", X, line, extra={"via": "cics_xctl" | "cics_link"})`
（名前は `normalize_code_name` で大文字化・COBOL の CALL と同じ規則）、識別子（動的）なら
`Dropped("cics_dynamic", line, snippet)` として申告する。`PROGRAM` キーワードの探索自体も
`_blank_cobol_strings` した文字列上で行い、他の引数の引用文字列値の中に偶然現れた
`PROGRAM(...)` を引数解析の対象にしない。`XCTL`/`LINK` 以外の CICS コマンド
（`SEND`/`RECEIVE`/`READ`/`WRITE`/`RETURN` 等）は参照を作らず、ブロック単位で
`Dropped("cics_other", line, <コマンド名>)` を1件だけ申告する（黙って落とさない・文単位ではなく
ブロック単位で集約し大量申告を避ける）。

COBOL の継続行マーカー（7桁目 `-`）で1つの `entries` エントリ（論理行）へ結合された複数物理行の
場合も、`--` 行コメントは連結前の物理行境界を越えて後続の物理行の内容まで巻き込まない
（`_normalize_logical_lines` の `segment_starts` を `_seg_bounds` でスライス相対へ変換し
`_sql_scan.sanitize_span`/`_find_end_exec` に渡す）。一方 `END-` と継続行の `EXEC` のように継続結合で
物理行をまたいで分割されたキーワード自体は、結合後の1つの文字列として連続したトークンのまま
認識する（境界は `--` の走査終端としてのみ使う・トークン検索自体を分断しない）。
"""
from __future__ import annotations

import bisect
import re

from ..identifiers import normalize_code_name as _norm
from ..static_analysis import (COBOL_EXT, _CALL, _COPY, _DYNAMIC_CALL,
                               _PROGRAM_ID, _is_comment, _is_free_format,
                               _normalize_logical_lines, _split_statements,
                               _strip_inline_comment, _strip_quoted)
from . import _sql_scan
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

# --- EXEC SQL/EXEC CICS（§4(d)）: ブロック境界・動的 SQL 検知の正規表現 ---
# `EXEC SQL`/`EXEC CICS` の両方を1つの正規表現で拾い、以降の処理はキャプチャした種別で分岐する。
# サニタイズ・テーブル節キーワード・識別子の抽出規則は `_sql_scan`（DDL 側と共通）に委ねる。
_EXEC_BLOCK_START = re.compile(r"\bEXEC\s+(SQL|CICS)\b", re.IGNORECASE)
_END_EXEC = re.compile(r"\bEND-EXEC\b", re.IGNORECASE)
_DECLARE_CURSOR = re.compile(r"\bDECLARE\s+\S+\s+CURSOR\b", re.IGNORECASE)
_EXECUTE_IMMEDIATE = re.compile(r"\bEXECUTE\s+IMMEDIATE\b", re.IGNORECASE)


def _blank_cobol_strings(s: str) -> str:
    """COBOL 引用文字列（`'...'`／`"..."`、`''`/`\"\"` エスケープ対応）の中身を同じ長さの空白へ
    置換する（`EXEC SQL` の開始検索専用——`DISPLAY 'EXEC SQL FAKE END-EXEC'` のような文字列
    リテラル中の偽陽性語を拾わないため）。`_strip_quoted`（`static_analysis.py`）と違い長さを
    保つため、マッチ位置をそのまま元の `code` の位置として使い回せる。
    """
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
            if s[i + 1:i + 2] == quote:            # エスケープ（連続する2個の同種引用符）
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


def _sanitize_cics_span(text: str, state: str | None) -> tuple:
    """CICS 用の1断片サニタイズ——単一・二重引用符の両方（`''`／`""` エスケープ対応）を
    文字列として保護する（`--`/`/* */` は扱わない・CICS にコメント構文はないため）。SQL 側の
    `"..."`（識別子として保護・そのまま残す）とは異なり、CICS の `"..."` は文字列リテラルとして
    中身を空白化する（`_sql_scan` には寄せない・§4(d)）。`state`（`None`／`"string"`／
    `"string_double"`）は直前の断片から持ち越した走査状態で、戻り値の2つ目にこの断片を走査し
    終えた後の状態を返す（フラグメント/物理行をまたいで持ち越すため）。
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
    """`text` 中の**実際の** `END-EXEC` を探す（§4(d)——文字列/コメント中の `END-EXEC` で
    ブロックを終端しない・例: `SELECT 'END-EXEC' AS X FROM ORDERS`／`-- END-EXEC`）。`kind`
    （`"SQL"`／`"CICS"`）でブロック種別に応じたサニタイズへ分岐する——`"SQL"` は
    `_sql_scan.sanitize_span`（単一引用符・`--`・`/* */`）、`"CICS"` は `_sanitize_cics_span`
    （単一・二重引用符の両方。例: `CHANNEL("END-EXEC") PROGRAM("TARGET")`）。戻り値は
    `(match_or_None, 走査後の state)`。`match` の位置は `text` の位置そのまま使える
    （どちらのサニタイズも同じ長さを保つ）。`boundaries` は `"SQL"` のときだけ使う（継続結合
    された物理行境界で `--` を止める）。
    """
    if kind == "CICS":
        sanitized, state = _sanitize_cics_span(text, state)
    else:
        sanitized, state = _sql_scan.sanitize_span(text, state, boundaries=boundaries)
    return _END_EXEC.search(sanitized), state


def _seg_bounds(segs: tuple, start: int, end: int | None = None) -> tuple:
    """1エントリ（論理行）全体の物理行境界オフセット `segs`（`_normalize_logical_lines` の
    `segment_starts`）から、そのエントリの部分文字列 `text[start:end)` に含まれる境界だけを、
    スライス相対オフセット（`start` 自身は含めない＝スライス先頭は境界に数えない）へ変換して
    返す（`_find_end_exec` の `boundaries` 引数用）。
    """
    return tuple(s - start for s in segs if s > start and (end is None or s < end))


def _sanitize_exec_sql_fragments(block_frags: list) -> str:
    """ブロックの断片（`(物理行, テキスト, 断片内の物理行境界オフセット)` の列）を結合しつつ、
    `_sql_scan.sanitize_span` で `--` 行コメント・`/* */` ブロックコメント・単一引用符文字列
    （`''` エスケープ対応）の中身を同じ長さの空白へ置換する（引用識別子はそのまま残す・
    偽マッチ除外専用）。1断片自体が継続結合で複数物理行を連結したものである場合、その内部境界
    （3つ目の要素）を渡すことで `--` がその境界を越えない。未閉じのブロックコメント／文字列の
    状態はフラグメント（物理行）をまたいで持ち越す。結合結果は
    `" ".join(t for _ln, t, _b in block_frags)` と同じ長さ（オフセット計算をそのまま使い回せる）。
    """
    state: str | None = None                          # None／"block_comment"／"string"
    parts: list = []
    for _ln, frag, boundaries in block_frags:
        sanitized, state = _sql_scan.sanitize_span(frag, state, boundaries=boundaries)
        parts.append(sanitized)
    return " ".join(parts)


def _exec_sql_block_refs(block_frags: list, start_line: int) -> tuple:
    """1つの `EXEC SQL ... END-EXEC` ブロック（`(物理行, 断片テキスト, 断片内の物理行境界
    オフセット)` の列）から `ACCESSES`→`Table` 候補を抽出する。戻り値は `(refs, dropped)`。

    `DECLARE ... CURSOR`／`EXECUTE IMMEDIATE`（動的 SQL）を含むブロックはテーブル抽出をせず、
    ブロック全体を `Dropped("exec_sql_dynamic", ...)` として申告する（判定はサニタイズ済み本文＝
    コメント・文字列リテラルの中身を空白化した後の文字列に対して行う——`-- DECLARE C CURSOR` の
    ようなコメント中の語を動的 SQL と誤認しないため）。節キーワードの探索・テーブル名抽出は
    `_sql_scan.table_refs`（DDL 側と共通の規則。CTE 名／`LATERAL`／Oracle の DB link 宛先の
    除外も含む）に委ねる。
    """
    combined = " ".join(t for _ln, t, _b in block_frags)
    sanitized = _sanitize_exec_sql_fragments(block_frags)
    if _DECLARE_CURSOR.search(sanitized) or _EXECUTE_IMMEDIATE.search(sanitized):
        return [], [Dropped("exec_sql_dynamic", start_line, combined.strip()[:120])]

    offset_starts: list = []
    offset_lines: list = []
    pos = 0
    for ln, t, _b in block_frags:
        offset_starts.append(pos)
        offset_lines.append(ln)
        pos += len(t) + 1                             # +1 は結合時に挟んだ半角空白

    def _line_at(p: int) -> int:
        idx = bisect.bisect_right(offset_starts, p) - 1
        return offset_lines[idx if idx >= 0 else 0]

    refs: list = []
    for name, offset in _sql_scan.table_refs(sanitized):
        refs.append(RefCandidate("ACCESSES", "Table", name, _line_at(offset),
                                  extra={"via": "exec_sql"}))
    return refs, []


# --- EXEC CICS（§4(d)・S5b）: XCTL/LINK の PROGRAM(...) 引数だけを読む・それ以外は cics_other ---
# ブロック先頭のコマンド語（`EXEC CICS` の直後の最初の単語）を読むだけの単純な正規表現。
_CICS_COMMAND_WORD = re.compile(r"^\s*([A-Za-z][A-Za-z0-9]*)")
# `PROGRAM(` の引数（引用リテラル＝静的解決可能／識別子＝動的）。CALL の引用符規則（`'...'`／`"..."`）
# と同じ。
_CICS_PROGRAM_ARG = re.compile(
    r"\bPROGRAM\s*\(\s*(?:'([^']*)'|\"([^\"]*)\"|([A-Za-z][\w-]*))\s*\)", re.IGNORECASE)
# `PROGRAM` キーワード自体の位置だけを探す（引用文字列の中身に現れた偽陽性を除外するため、
# `_blank_cobol_strings` した文字列上でだけ使う・§4(d) RV是正）。
_CICS_PROGRAM_KEYWORD = re.compile(r"\bPROGRAM\b", re.IGNORECASE)


def _exec_cics_block_refs(block_frags: list, start_line: int) -> tuple:
    """1つの `EXEC CICS ... END-EXEC` ブロック（`(物理行, 断片テキスト, 断片内の物理行境界
    オフセット)` の列）から `INVOKES`→`Module` 候補を抽出する。戻り値は `(refs, dropped)`。

    ブロック先頭のコマンド語が `XCTL`/`LINK` のときだけ `PROGRAM(...)` の引数を読む——引用
    リテラル（`'...'`／`"..."`）なら CALL と同じ規則で静的解決可能な呼び出し先として扱い、
    識別子（動的）なら `Dropped("cics_dynamic", ...)` として申告する。`XCTL`/`LINK` 以外の
    CICS コマンドは参照を作らず、ブロック単位で `Dropped("cics_other", line, <コマンド名>)` を
    1件だけ申告する（黙って落とさない・文単位ではなくブロック単位で集約し大量申告を避ける）。

    `PROGRAM` キーワードの探索は `_blank_cobol_strings`（引用文字列の中身を同長の空白へ置換）
    した文字列上で行う——他の引数の引用文字列値の中に `PROGRAM(...)` という文字列が偶然現れても
    （例: `CHANNEL("PROGRAM('FAKE')") PROGRAM('REAL')`）、引用符の外にある本物のキーワードだけを
    見つける（§4(d) RV是正）。実際の引数解析（引用リテラルの中身読み取り）は見つけた位置から
    未サニタイズの `combined` に対して行う。
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
        pos += len(t) + 1                             # +1 は結合時に挟んだ半角空白

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

# PROGRAM-ID/COPY/CALL/動的 CALL のすべての抽出は `_normalize_logical_lines`（static_analysis.py・
# S1）が返す論理行に対して行う——固定形式は1〜6桁連番・73桁以降（識別領域）を落とし、継続行
# （7桁目 `-`）を直前行へ結合した後の (logical_text, first_physical_line) を使う。自由形式は
# 物理行をそのまま返す（列制限・継続行結合なし）。


class CobolAnalyzer(Analyzer):
    """`PROGRAM-ID` → `Module`。`COPY` → `Copybook` 参照（COPIES）。`CALL` → `Module` 参照（INVOKES）。"""

    name = "cobol"
    extensions = frozenset(COBOL_EXT)
    doctype = "cobol"

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
        """EXEC SQL ブロックの外側の1区間（COPY/CALL/動的 CALL 検知）。`code_part`/`logical_part`
        は同一物理行内の一部分のこともある（複数 EXEC SQL ブロックの prefix/suffix・§4(d)）。
        `logical_part` は `code_part` と同じ開始位置を共有する対応区間（行末インラインコメントの
        有無だけが異なりうる——動的 CALL 検知だけはコメント切り捨て前の `logical_part` を使う）。
        """
        # COPY 抽出だけはさらに引用文字列の中身も除去する（`DISPLAY 'COPY FAKECPY'.` のような
        # 文字列リテラル中の語を誤検知しない）。CALL は引用符の中身（呼び出し先プログラム名）
        # 自体を読むため、こちらには適用しない。
        for cb in _COPY.findall(_strip_quoted(code_part)):
            refs.append(RefCandidate("COPIES", "Copybook", _norm(cb), i))
        # CALL は文単位（ピリオド／END-CALL 区切り）で判定する——同一行に複数の CALL 文が
        # 並ぶ場合（`CALL 'A'. CALL B.`・`CALL 'A' END-CALL CALL B END-CALL.`）でも
        # 取りこぼさない。引用文字列の中身は動的呼び出し判定の対象外にする（`DISPLAY 'CALL X'.`
        # のような文字列リテラルを誤検知しない）。
        for stmt in _split_statements(code_part):
            for callee in _CALL.findall(stmt):
                refs.append(RefCandidate("INVOKES", "Module", _norm(callee), i,
                                          extra={"via": "call"}))
        # 動的 CALL の検知は正規化済みの論理行（インラインコメント切り捨て前）に対して行う
        # ——固定形式は既に採番/識別領域（73桁以降）を落とし済み、自由形式は元の行のまま。
        for stmt in _split_statements(logical_part):
            # `_strip_quoted` が文字列リテラルの中身を空にするため、残る `CALL` はすべて
            # リテラルでない（動的）呼び出し——同一断片に literal CALL が同居していても
            # （例: `CALL 'A' ON EXCEPTION CALL B END-CALL`）出現ごとに個別判定する。
            for _callee in _DYNAMIC_CALL.findall(_strip_quoted(stmt)):
                # 静的には解決できないため解析せず記録するだけ（誤った固定先を推測しない）。
                dropped.append(Dropped("dynamic_call", i, stmt.strip()[:120]))

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        refs: list = []
        free_format = _is_free_format(text)
        # デバッグ行（7桁目 `D`）の所有は `collect_defs`（Pass1）側だけにする——
        # `_normalize_logical_lines` は debug 行を `entries` から既に除外済みのため、ここでは
        # 読み飛ばすだけでよく、Dropped として返さない（同じ行を Pass1/Pass2 の両方で二重に
        # flags へ記録しないため）。
        entries, _debug_dropped = _normalize_logical_lines(text, free_format)
        dropped: list = []
        after_id = False
        exec_block: list | None = None    # None＝ブロック外／list＝EXEC SQL/CICS ブロック収集中（(行, 断片, 断片内境界)の列）
        exec_start_line: int | None = None
        exec_kind: str | None = None      # "SQL"／"CICS"（収集中のブロック種別・完了時の抽出先を分岐する）
        sql_state: str | None = None      # exec_block 収集中に持ち越す境界スキャナの状態
                                           # （None／"block_comment"／"string"／"string_double"・
                                           # 物理行をまたいで持ち越す・exec_kind で保護規則が分岐）
        for logical, i, segs in entries:
            if _is_comment(logical):
                continue
            if not after_id:
                if _PROGRAM_ID.search(logical):
                    after_id = True
                continue
            # COPY/CALL/EXEC SQL のリテラル抽出は、行末インラインコメント（`*>` 以降・
            # rv-s2-mention #5）を先に切り捨ててから行う——`MOVE X TO Y. *> COPY FAKE` のような
            # 行末コメント中の語を構文と誤認しない（`_is_comment` は行全体がコメントの場合しか見ない）。
            # `code` は常に `logical` の prefix（同じ文字列の先頭部分）——`_strip_inline_comment` は
            # 末尾を切り捨てるだけなので、`code` 内の位置はそのまま `logical` の同じ位置を指す。
            # `segs`（`logical` 全体の継続結合前の物理行境界オフセット）も `code` へそのまま
            # 使い回せる（`_seg_bounds` がスライスへ変換する）。
            code = _strip_inline_comment(logical)

            # 位置カーソルで論理行を左から走査する——`EXEC SQL ... END-EXEC` の前後（prefix/suffix）
            # や同一行内の複数ブロックも取りこぼさず、通常の COPY/CALL/次の EXEC SQL 処理へ回す
            # （§4(d)）。`EXEC SQL` の開始検索は COBOL 引用文字列を同長の空白に置換した
            # `_blank_cobol_strings` の結果に対して行い（`DISPLAY 'EXEC SQL ... END-EXEC'` のような
            # 文字列リテラル中の偽陽性語を拾わない）、ブロック収集中の `END-EXEC` は
            # `_find_end_exec`（ブロック種別ごとに保護規則が分岐する境界スキャナ・SQL は単一
            # 引用符・`--`・`/* */`、CICS は単一・二重引用符の両方）で実際の終端だけを認識する。
            # マッチ位置はどちらも同長置換なので元の `code`
            # の位置をそのまま使える——前後コード（prefix/suffix）は元文字列の位置を共有する。
            # `_find_end_exec` には常に `_seg_bounds(segs, ...)` を渡す——1エントリ（論理行）が
            # 継続結合で複数物理行を連結したものである場合でも、`--` 行コメントが連結前の
            # 物理行境界を越えて後続の物理行まで巻き込まないようにするため（§4(d) RV是正）。
            pos = 0
            while True:
                if exec_block is not None:            # EXEC SQL/CICS ブロック収集中（前の行から継続）
                    em, sql_state = _find_end_exec(code[pos:], sql_state, _seg_bounds(segs, pos),
                                                    exec_kind)
                    if em is None:
                        exec_block.append((i, code[pos:], _seg_bounds(segs, pos)))
                        break                          # このエントリの残りは丸ごとブロックへ持ち越す
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
                if sm is None:                         # 残り全部が通常コード（EXEC SQL/CICS なし）
                    self._process_normal_segment(code[pos:], logical[pos:], i, refs, dropped)
                    break

                self._process_normal_segment(code[pos:pos + sm.start()], logical[pos:pos + sm.start()],
                                              i, refs, dropped)
                kind = sm.group(1).upper()
                after_start_pos = pos + sm.end()
                em, sql_state = _find_end_exec(code[after_start_pos:], None,
                                                _seg_bounds(segs, after_start_pos), kind)
                if em is None:                         # ブロックが次行以降へ続く
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
                pos = after_start_pos + em.end()       # 同一行の続き（suffix）をさらに走査する

        if exec_block is not None:                    # END-EXEC 無いまま EOF＝未終端ブロック
            combined = " ".join(t for _ln, t, _b in exec_block).strip()
            reason = "exec_sql_dynamic" if exec_kind == "SQL" else "cics_dynamic"
            dropped.append(Dropped(reason, exec_start_line, combined[:120]))

        return RefResult(refs=refs, dropped=dropped)
