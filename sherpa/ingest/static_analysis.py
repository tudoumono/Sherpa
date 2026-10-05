"""静的解析のパース・プリミティブ（COBOL/JCL/Copybook の構文を拾う正規表現と判定ヘルパ）。

構造グラフの生成は `sherpa.ingest.analyzers` 配下の言語アナライザが行い、ここはそれらが import して使う。
ソースは読むだけで実行しない。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re


# COBOL の COPY/CALL/EXEC PGM・PROGRAM-ID・項目・JOB を拾う構文
_PROGRAM_ID = re.compile(r"PROGRAM-ID\s*\.\s*([A-Z0-9#@$-]+)", re.I)
# COPY/CALL の前は COBOL 識別子境界（直前が `A-Z0-9#@$-` でない）で区切る（`WS-COPY` 等の誤検知を避ける）
_COPY = re.compile(r"(?<![A-Z0-9#@$-])COPY\s+([A-Z0-9#@$-]+)", re.I)
# `'...'` に加え `"..."` の CALL も受理する
_CALL = re.compile(r"(?<![A-Z0-9#@$-])CALL\s+['\"]([^'\"]+)['\"]", re.I)
_ITEM = re.compile(r"^\s*(\d{2})\s+([A-Z0-9#@$-]+)")          # レベル項目
_VALUE = re.compile(r"\bVALUE\s+([+-]?[0-9]+(?:\.[0-9]+)?)", re.I)  # VALUE 句の数値リテラル
_JOB = re.compile(r"^//(\S+)\s+JOB\b", re.I)
# 名前欄（ステップ名）は省略できるため `\S*`
_EXEC = re.compile(r"^//(\S*)\s+EXEC\s+PGM=([A-Z0-9#@$-]+)", re.I)
# `PGM=&NAME`（シンボリック）は解決先を特定できないため `Dropped("pgm_symbolic", ...)` 用に別判定する
_JCL_EXEC_PGM_SYMBOLIC = re.compile(r"^//(\S*)\s+EXEC\s+PGM=(&\S+)", re.I)

# 未対応構文の検知用（`Dropped` として記録するための判定）。`CALL` の前は COBOL 識別子境界で区切る
_DYNAMIC_CALL = re.compile(r"(?<![A-Z0-9#@$-])CALL\s+([A-Z][A-Z0-9#@$-]*)\b", re.I)
_JCL_PROC = re.compile(r"^//(\S*)\s+PROC\b", re.I)                     # PROC 定義文
# PGM= に限らない EXEC（カタログドプロシージャ／インストリーム PROC の実行）。`PROC=` は省略可。
# `PGM=` 系は `_EXEC`/`_JCL_EXEC_PGM_SYMBOLIC` が担当するため `(?!PGM=)` で除く。名前欄は省略可。
_JCL_EXEC_PROC = re.compile(r"^//(\S*)\s+EXEC\s+(?!PGM=)(?:PROC=)?(\S+)", re.I)
_JCL_INCLUDE = re.compile(r"^//\S*\s*INCLUDE\b", re.I)                 # INCLUDE（MEMBER= を伴わない未対応形の検知用）
_JCL_INCLUDE_MEMBER = re.compile(r"^//\S*\s*INCLUDE\s+MEMBER\s*=\s*(\S+)", re.I)  # INCLUDE MEMBER=
# COBOL 文字列リテラル（`'...'`／`"..."`）
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
# 固定形式/自由形式は入力から判定する。コメント行でない最初の指示文
# （`>>SOURCE [FORMAT] [IS] FREE/FIXED`）で決め、途中の切替は見ない。指示が無ければ固定形式。
_SOURCE_FORMAT_DIRECTIVE = re.compile(r">>SOURCE\s+(?:FORMAT\s+)?(?:IS\s+)?(FREE|FIXED)\b", re.I)
# `WITH DEBUGGING MODE`: これがあるファイルだけ 7 桁目 `D` のデバッグ行を通常行として解析する
_WITH_DEBUGGING_MODE = re.compile(r"WITH\s+DEBUGGING\s+MODE", re.I)
# 様式判定（`_detect_column1_style`）用の見出し/レベル項目の検知。`ID` 単体も `ID DIVISION.` として有効
_DIVISION_HEADER = re.compile(
    r"\b(?:IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", re.I)
_LEVEL_ITEM_COLUMN1 = re.compile(r"^\d{2}\s+[A-Z0-9#@$-]", re.I)
# 固定列の証拠: 8 桁目（CP932 幅で数える）から始まるレベル番号付き項目定義
_LEVEL_ITEM_FIXED = re.compile(r"^\s*\d{2}\s+[A-Z0-9#@$-]+", re.I)

COBOL_EXT = {".cbl", ".cob", ".cobol"}
COPYBOOK_EXT = {".cpy", ".copybook"}
JCL_EXT = {".jcl"}




def _is_seq_area(line: str) -> bool:
    """1〜6桁が連番領域として妥当か（数字または空白のみ）。"""
    return all(c.isdigit() or c == " " for c in line[:6])


def _is_comment(line: str) -> bool:
    """COBOL/JCL のコメント行（固定形式 桁7の `*`/`/`、行頭 `*`、JCL `//*`）。

    桁7の判定は連番領域が妥当な行にだけ適用する（`X = A * B` のようなコード行を誤判定しないため）。
    """
    s = line.lstrip()
    if s.startswith("*") or s.startswith("//*"):
        return True
    return len(line) > 6 and line[6] in "*/" and _is_seq_area(line)


def _is_comment_column1_style(line: str) -> bool:
    """列1始まり（column-1 style）ファイル専用のコメント判定: 1桁目が `*` の行だけ。"""
    return line.startswith("*")


def _fixed_column_slice(line: str, start: int, stop: int) -> str:
    """固定形式の 0 始まり桁範囲を元の文字列から取り出す。

    桁は CP932 の幅で数え、表せない文字は 1 文字 1 桁とする。範囲境界をまたぐ全角文字は含めない。
    """
    if line.isascii():
        return line[start:stop]
    column = 0
    chars = []
    for char in line:
        next_column = column + len(char.encode("cp932", errors="replace"))
        if next_column > stop:
            break
        if column >= start:
            chars.append(char)
        column = next_column
    return "".join(chars)


def _is_comment_fixed_columns(line: str) -> bool:
    """固定列（fixed columns）ファイル専用のコメント判定: 連番領域の内容に関係なく 7 桁目（CP932 幅）の `*`/`/` をコメントとする。"""
    return _fixed_column_slice(line, 6, 7) in ("*", "/")


def _is_word_char(ch: str) -> bool:
    """COBOL 識別子文字（`A-Z0-9#@$-`）か。"""
    return bool(ch) and ch.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#@$-"


def _is_free_format(text: str) -> bool:
    """`text`（ファイル全体）が自由形式か。コメント行でない最初の指示文（FREE/FIXED）で決める。指示が無ければ固定形式。"""
    for line in text.splitlines():
        if _is_comment(line):
            continue
        m = _SOURCE_FORMAT_DIRECTIVE.search(line)
        if m:
            return m.group(1).upper() == "FREE"
    return False


def _detect_column1_style(lines: list) -> bool:
    """自由形式でない固定形式ファイルの様式（列1始まり／固定列）をファイル単位で 1 つに決める。

    ① 固定列の証拠を先に見る: コメントでない行のうち、7 桁目が有効な indicator（空白・`D`/`d`・`-`・`*`・`/`）で、
       8〜72 桁の先頭が DIVISION 見出し・`PROGRAM-ID`・レベル番号付き項目のいずれかなら固定列（True にしない）。
    ② 固定列の証拠が無いときだけ、1〜7 桁目以内から DIVISION 見出しが始まる行、または 1 桁目からレベル番号付き
       項目が始まる行があれば列1始まり（True）。
    どちらも無ければ固定列。検出限界: 見出しもレベル項目も無い列1始まりの断片は固定列として扱い、先頭 7 文字を落とす。
    """
    for line in lines:
        if _is_comment(line):
            continue
        if _fixed_column_slice(line, 6, 7) not in (" ", "D", "d", "-", "*", "/"):
            continue
        code_area = _fixed_column_slice(line, 7, 72).lstrip()
        if (_DIVISION_HEADER.match(code_area)
                or _PROGRAM_ID.match(code_area)
                or _LEVEL_ITEM_FIXED.match(code_area)):
            return False
    for line in lines:
        if _is_comment(line):
            continue
        m = _DIVISION_HEADER.search(line)
        if m and m.start() <= 6:
            return True
        if _LEVEL_ITEM_COLUMN1.match(line):
            return True
    return False


def _has_debugging_mode(logical: list) -> bool:
    """固定列ファイルに `WITH DEBUGGING MODE` の宣言があるか。

    `logical` は D 行を継続結合から除外した第 1 段の論理行（`(text, first_physical_line, segment_starts)` の列）。
    引用符の中身は `_strip_quoted` で除いてから探す。
    """
    return any(_WITH_DEBUGGING_MODE.search(_strip_quoted(text))
               for text, _line_no, _segs in logical)


def _split_statements(line: str) -> list:
    """1行を COBOL の文/CALL スコープ単位に分割する（引用符の外側のピリオドと `END-CALL` が区切り）。"""
    stmts: list = []
    buf: list = []
    in_quote = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == "'":
            in_quote = not in_quote
            buf.append(ch)
            i += 1
            continue
        if not in_quote:
            if ch == ".":
                stmts.append("".join(buf))
                buf = []
                i += 1
                continue
            if (line[i:i + 8].upper() == "END-CALL"
                    and (i == 0 or not _is_word_char(line[i - 1]))
                    and (i + 8 >= n or not _is_word_char(line[i + 8]))):
                stmts.append("".join(buf))
                buf = []
                i += 8
                continue
        buf.append(ch)
        i += 1
    if buf:
        stmts.append("".join(buf))
    return stmts


_PSEUDO_TEXT_RE = re.compile(r"==.*?==")


def _blank_pseudo_text(s: str) -> str:
    """`COPY … REPLACING ==…== BY ==…==` の pseudo-text を同じ長さの空白へ置換する（中の `CALL`／`COPY` を構文と誤認しないため）。"""
    return _PSEUDO_TEXT_RE.sub(lambda m: " " * len(m.group(0)), s)


def _strip_quoted(s: str) -> str:
    """引用符（`'...'`／`"..."`）の中身を除去する（文字列リテラル中の語を構文と誤認しないための前処理）。
    `CALL '...'` 自体のリテラル抽出（`_CALL`）には使わない。"""
    return _QUOTED_RE.sub("''", s)


def _scan_quote_state(s: str, state: str | None) -> str | None:
    """`s` を左から走査し、引用符状態 `state`（`None`／`'`／`"`）から続けた場合の走査後の状態を返す。二重化はリテラル内の 1 文字として扱う。"""
    i, n = 0, len(s)
    while i < n:
        ch = s[i]
        if state is None:
            if ch in ("'", '"'):
                state = ch
            i += 1
            continue
        if ch == state:
            if s[i + 1:i + 2] == state:
                i += 2                              # 二重化＝リテラル内の1文字
                continue
            state = None
            i += 1
            continue
        i += 1
    return state


_ENDS_WITH_KEYWORD_BEFORE_OPERAND = re.compile(r"(?<![A-Z0-9#@$-])(?:CALL|COPY)$", re.I)


def _build_fixed_column_logical(physical_lines: list, include_debug: bool) -> tuple:
    """固定列ファイルの物理行から論理行を組み立てる（`_normalize_logical_lines` の下請け・継続結合の状態機械）。

    `include_debug=False`: 7 桁目 `D`/`d`（デバッグ行）を状態機械から除いて `debug_lines` へ積む。
    `include_debug=True`: D 行も通常行として組み込む。
    7 桁目 `-` の行は直前の論理行へ連結する（リテラル継続は列位置を維持し、それ以外は空白なしで直結。ただし直前が `CALL`／`COPY` の語で終わるときは空白 1 つで区切る）。
    コメント行と 7〜72 桁が空白だけの行は除く。
    戻り値は `(logical, debug_lines)`。`logical` は `(text, first_physical_line, segment_starts)` の列で、
    `segment_starts` は `text` 中の物理行境界オフセット（昇順・先頭 0）。
    """
    logical: list = []
    debug_lines: list = []
    frags: list = []
    frag_line = None
    frag_segments: list = []                         # 物理行境界のオフセット
    frag_len = 0                                      # "".join(frags) の長さ
    quote_state: str | None = None                  # 直近の断片終端時点の引用符状態

    def flush():
        nonlocal frags, frag_line, frag_segments, frag_len
        if frags:
            logical.append(("".join(frags), frag_line, tuple(frag_segments)))
        frags, frag_line, frag_segments, frag_len = [], None, [], 0

    for i, line in enumerate(physical_lines, 1):
        if _is_comment_fixed_columns(line):
            continue
        if not _fixed_column_slice(line, 6, 72).strip():
            continue                                # 空行は結合対象から除外
        indicator = _fixed_column_slice(line, 6, 7)
        if indicator in ("D", "d") and not include_debug:
            debug_lines.append((i, _fixed_column_slice(line, 7, 72).strip()[:120]))
            continue
        if indicator == "-":
            cont_area = _fixed_column_slice(line, 11, 72)
            if not frags:
                cont = cont_area.lstrip()
                if cont[:1] in ("'", '"'):
                    cont = cont[1:]
                frags = [cont]
                frag_line = i                        # 直前行が無い場合は単独行扱い
                frag_segments = [0]
                frag_len = len(cont)
                quote_state = _scan_quote_state(cont, None)
                continue
            if quote_state is not None:
                # リテラル継続: 継続行の先頭の同種引用符を除去して連結する
                stripped = cont_area.lstrip()
                cont = stripped[1:] if stripped[:1] == quote_state else cont_area
            else:
                # 非リテラル継続: 直前断片の末尾空白と継続行の先頭空白を除いて直結する
                old_len = len(frags[-1])
                frags[-1] = frags[-1].rstrip()
                if _ENDS_WITH_KEYWORD_BEFORE_OPERAND.search(frags[-1]):
                    frags[-1] += " "                 # `CALL`／`COPY` の直後の継続は語の区切り（オペランドと連結しない）
                frag_len -= old_len - len(frags[-1])
                cont = cont_area.lstrip()
            frag_segments.append(frag_len)
            frags.append(cont)
            frag_len += len(cont)
            quote_state = _scan_quote_state(cont, quote_state)
            continue
        flush()
        frag_line = i
        frags = [_fixed_column_slice(line, 7, 72)]
        frag_segments = [0]
        frag_len = len(frags[0])
        quote_state = _scan_quote_state(frags[0], None)
    flush()
    return logical, debug_lines


def _normalize_logical_lines(text: str, free_format: bool) -> tuple:
    """COBOL の物理行を論理行へ正規化する（COBOL/コピーブック共通）。

    ① 自由形式: 物理行をそのまま `(line, line_no, (0,))` の列で返す。
    ② 列1始まり: コメント行以外の物理行をそのまま 1 行ずつ論理行にする（結合しない）。
    ③ 固定列: 連番領域と 73 桁以降を落とし、8〜72 桁だけを残して継続行を結合する。
       デバッグ行は、まず D 行を除いて論理行を組み、`WITH DEBUGGING MODE` 宣言が無ければ除いた D 行を
       `debug_dropped` へ積み、宣言があれば D 行を通常行として組み直す。
    戻り値は `(entries, debug_dropped)`。`entries` は `(logical_text, first_physical_line, segment_starts)` の列。
    未対応: 72 桁目で閉じる引用符の直後に同種の引用符で始まる継続行。
    """
    if free_format:
        return [(line, i, (0,)) for i, line in enumerate(text.splitlines(), 1)], []

    physical_lines = text.splitlines()
    if _detect_column1_style(physical_lines):
        entries = [(line, i, (0,)) for i, line in enumerate(physical_lines, 1)
                   if not _is_comment_column1_style(line)]
        return entries, []

    logical, debug_lines = _build_fixed_column_logical(physical_lines, include_debug=False)
    if _has_debugging_mode(logical):
        logical, _debug_lines = _build_fixed_column_logical(physical_lines, include_debug=True)
        return logical, []
    return logical, debug_lines


def _strip_inline_comment(line: str) -> str:
    """引用符の外側にある `*>`（自由形式のインラインコメント）以降を切り捨てる。"""
    in_quote = None
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_quote:
            if ch == in_quote:
                in_quote = None
            i += 1
            continue
        if ch in ("'", '"'):
            in_quote = ch
            i += 1
            continue
        if ch == "*" and line[i + 1:i + 2] == ">":
            return line[:i]
        i += 1
    return line
