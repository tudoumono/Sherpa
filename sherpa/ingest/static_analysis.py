"""静的解析のパース・プリミティブ（COBOL/JCL/Copybook 構文・ONTOLOGY §4 の MVP 部分集合）。

構造グラフ生成は `sherpa.ingest.analyzers` 配下の言語アナライザ（`collect_defs`/`extract_refs`）が
担い、本モジュールには**正規表現と判定ヘルパだけ**を残す（アナライザが import して再利用）。
ソースは実行しない（読むだけ）。
"""
from __future__ import annotations

import re

from .identifiers import normalize_code_name as _norm   # 正規化は identifiers に集約（単一の真実源・RV DRY）

# ONTOLOGY §4 の構文（アナライザが COPY/CALL/EXEC PGM・PROGRAM-ID・項目・JOB を拾う）
_PROGRAM_ID = re.compile(r"PROGRAM-ID\s*\.\s*([A-Z0-9#@$-]+)", re.I)
# `\b` は `-` を非 word 文字として扱うため `WS-COPY ITEM` / `WS-CALL 'X'` のような COBOL 識別子の
# 末尾に偶然 COPY/CALL が現れるケースを偽参照として拾っていた（CODE-1a RV7 指摘・2026-09-05 是正）。
# 前方は `_DYNAMIC_CALL` と同じ COBOL 識別子境界（直前が `A-Z0-9#@$-` でない）を使う。
_COPY = re.compile(r"(?<![A-Z0-9#@$-])COPY\s+([A-Z0-9#@$-]+)", re.I)
# rv-s2-mention #5（2026-09-05）: `'...'` に加え `"..."` の CALL も INVOKES として受理する
# （`_strip_quoted`/`_strip_inline_comment` と組み合わせて呼び出し元 `analyzers/cobol.py` が使う）。
_CALL = re.compile(r"(?<![A-Z0-9#@$-])CALL\s+['\"]([^'\"]+)['\"]", re.I)
_ITEM = re.compile(r"^\s*(\d{2})\s+([A-Z0-9#@$-]+)")          # 01/05.. レベル項目
_VALUE = re.compile(r"\bVALUE\s+([+-]?[0-9]+(?:\.[0-9]+)?)", re.I)  # VALUE 句の数値リテラル
_JOB = re.compile(r"^//(\S+)\s+JOB\b", re.I)
_EXEC = re.compile(r"^//(\S+)\s+EXEC\s+PGM=([A-Z0-9#@$-]+)", re.I)

# 未対応構文の検知用（解決はしない・`Dropped` として記録するためだけの判定）。
# `CALL` の前は `\b`（標準の word 境界）ではなく COBOL 識別子境界（直前が `A-Z0-9#@$-` でない）を
# 使う——`\b` はハイフンを非 word 文字扱いするため、`MOVE WS-CALL TO RESULT.` のような識別子の
# 一部（`WS-CALL`）の中の "CALL" を独立した語と誤認識し、続く "TO" を動的呼び出し先と誤検知する。
_DYNAMIC_CALL = re.compile(r"(?<![A-Z0-9#@$-])CALL\s+([A-Z][A-Z0-9#@$-]*)\b", re.I)
_JCL_EXEC_ANY = re.compile(r"^//(\S+)\s+EXEC\s+(\S+)", re.I)           # PGM= に限らない EXEC（PROC 実行を含む）
_JCL_INCLUDE = re.compile(r"^//\S*\s*INCLUDE\b", re.I)                 # // INCLUDE MEMBER=
# COBOL 文字列リテラル（`'...'`／`"..."`）。rv-s2-mention #5（2026-09-05）で二重引用符も対象へ拡張
# （元は単一引用符のみ・`_strip_quoted` の docstring 参照）。
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
# 固定形式/自由形式は入力から判定する（設定は持たない・docs/proposals/2026-08-29-コード解析層の
# コンポーネント化.md §2.2・§5）。ファイル単位の近似——ファイル中で**最初に現れる**指示文
# （`>>SOURCE FORMAT FREE/FIXED`・`>>SOURCE FREE/FIXED` も同義）で判定し、それ以降の途中切替や
# コメント中の指示は見ない（対応する場合は CODE-2「静的解析の深化」の対象）。指示が無ければ
# 固定形式（既定）。
_SOURCE_FORMAT_DIRECTIVE = re.compile(r">>SOURCE\s+(?:FORMAT\s+)?(FREE|FIXED)\b", re.I)

COBOL_EXT = {".cbl", ".cob", ".cobol"}
COPYBOOK_EXT = {".cpy", ".copybook"}
JCL_EXT = {".jcl"}




def _is_comment(line: str) -> bool:
    """COBOL/JCL のコメント行（固定形式 桁7の `*`/`/`、行頭 `*`、JCL `//*`）。"""
    s = line.lstrip()
    return s.startswith("*") or s.startswith("//*") or (len(line) > 6 and line[6] in "*/")


def _is_word_char(ch: str) -> bool:
    """COBOL 識別子文字（`A-Z0-9#@$-`・`_PROGRAM_ID` 等の識別子系正規表現と同じ文字集合）。"""
    return bool(ch) and ch.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#@$-"


def _is_free_format(text: str) -> bool:
    """`text`（ファイル全体）が自由形式か（`_SOURCE_FORMAT_DIRECTIVE` 参照）。

    ファイル中で最初に現れる指示文（FREE/FIXED どちらか）で決める——指示が無ければ固定形式。
    """
    m = _SOURCE_FORMAT_DIRECTIVE.search(text)
    return bool(m) and m.group(1).upper() == "FREE"


def _split_statements(line: str) -> list:
    """1行を COBOL の文/CALL スコープ単位に分割する（引用符の外側の**ピリオド**と**`END-CALL`**が区切り）。

    同一文に `CALL 'A' END-CALL CALL B END-CALL` のように複数の CALL スコープがピリオドを挟まず
    並ぶ場合でも、CALL ごとに独立した断片として判定できるようにする（`END-CALL` を境界に含めないと、
    後続の動的 CALL が同じ断片に literal CALL と同居して見逃される）。引用符 `'...'` の中の
    ピリオド/`END-CALL` は区切りに使わない（文字列リテラルの中身を誤って割らない）。
    """
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


def _strip_quoted(s: str) -> str:
    """引用符（`'...'`／`"..."`）の中身を除去する（`DISPLAY 'CALL X'`／`DISPLAY "COPY X"` のような
    文字列リテラル中の語を、動的 CALL 判定・COPY 抽出が構文と誤認しないようにするための前処理・
    rv-s2-mention #5 で二重引用符にも対応）。`CALL '...'`/`CALL "..."` 自体のリテラル抽出
    （`_CALL`）には使わない——そちらは引用符の**中身**（呼び出し先プログラム名）を読む側のため。"""
    return _QUOTED_RE.sub("''", s)


def _strip_inline_comment(line: str) -> str:
    """引用符の外側にある `*>`（自由形式のインラインコメント）以降を切り捨てる（rv-s2-mention #5）。

    `_is_comment` は行**全体**が `*` 始まりのコメント行かどうかしか見ない——`MOVE X TO Y. *> COPY
    FAKE` のように実コードに続けて書かれた行末コメントは対象外だった。COPY/CALL のリテラル抽出
    （`analyzers/cobol.py` の `_COPY`/`_CALL` 呼び出し）の前段でこれを切り捨てないと、コメント中の
    語を構文と誤認する。引用符 `'...'`/`"..."` の中の `*>` は区切りに使わない（文字列リテラル内の
    偶然の一致を誤って打ち切らない）。
    """
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
