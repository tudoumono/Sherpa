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
# 名前欄（ステップ名）は `\S*`——JCL では名前欄の省略（`// EXEC ...`）が可能なため `\S+` だと
# 名前欄なしの EXEC 文を取りこぼす。
_EXEC = re.compile(r"^//(\S*)\s+EXEC\s+PGM=([A-Z0-9#@$-]+)", re.I)
# `PGM=` の対象がシンボリックパラメータ（`PGM=&NAME`）の場合。静的に解決先を特定できないため
# `_JCL_EXEC_PROC`（Batch 参照）には流さず、`Dropped("pgm_symbolic", ...)` 専用に判定する
# （`_EXEC` の文字クラスは `&` を含まないため元々マッチしない＝この行だけを狙って拾う）。
_JCL_EXEC_PGM_SYMBOLIC = re.compile(r"^//(\S*)\s+EXEC\s+PGM=(&\S+)", re.I)

# 未対応構文の検知用（解決はしない・`Dropped` として記録するためだけの判定）。
# `CALL` の前は `\b`（標準の word 境界）ではなく COBOL 識別子境界（直前が `A-Z0-9#@$-` でない）を
# 使う——`\b` はハイフンを非 word 文字扱いするため、`MOVE WS-CALL TO RESULT.` のような識別子の
# 一部（`WS-CALL`）の中の "CALL" を独立した語と誤認識し、続く "TO" を動的呼び出し先と誤検知する。
_DYNAMIC_CALL = re.compile(r"(?<![A-Z0-9#@$-])CALL\s+([A-Z][A-Z0-9#@$-]*)\b", re.I)
_JCL_PROC = re.compile(r"^//(\S*)\s+PROC\b", re.I)                     # PROC 定義文（`//name PROC`／`// PROC`＝名前なし）
# PGM= に限らない EXEC（カタログドプロシージャ実行／インストリーム PROC 実行）。`PROC=` 接頭辞は
# 任意（`EXEC PROC=X` と `EXEC X` の両方を同じ対象名で拾う）。対象名の末尾に付くパラメータ
# （`,PARM=...` 等、EXEC の対象名との間に空白が無い）は呼び出し側が `,` で切り落とす。
# `(?!PGM=)` で `PGM=` 系（`_EXEC`／`_JCL_EXEC_PGM_SYMBOLIC` が担当）を明示的に除外する
# ——除外しないと `PGM=&X` のようなシンボリック対象が対象名 `PGM=&X` ごと Batch 参照に誤って
# 流れ込んでいた。名前欄は `\S*`（省略可）。
_JCL_EXEC_PROC = re.compile(r"^//(\S*)\s+EXEC\s+(?!PGM=)(?:PROC=)?(\S+)", re.I)
_JCL_INCLUDE = re.compile(r"^//\S*\s*INCLUDE\b", re.I)                 # // INCLUDE（MEMBER= を伴わない未対応形の検知用）
_JCL_INCLUDE_MEMBER = re.compile(r"^//\S*\s*INCLUDE\s+MEMBER\s*=\s*(\S+)", re.I)  # // INCLUDE MEMBER=
# COBOL 文字列リテラル（`'...'`／`"..."`）。rv-s2-mention #5（2026-09-05）で二重引用符も対象へ拡張
# （元は単一引用符のみ・`_strip_quoted` の docstring 参照）。
_QUOTED_RE = re.compile(r"'[^']*'|\"[^\"]*\"")
# 固定形式/自由形式は入力から判定する（設定は持たない・docs/proposals/2026-08-29-コード解析層の
# コンポーネント化.md §2.2・§5）。ファイル単位の近似——物理行を順に見て、コメント行でない行に
# 現れた**最初の**指示文（`>>SOURCE FORMAT FREE/FIXED`・`>>SOURCE FREE/FIXED`・
# `>>SOURCE FORMAT IS FREE/FIXED` も同義）で判定し、それ以降の途中切替は見ない（対応する場合は
# CODE-2「静的解析の深化」の対象）。指示が無ければ固定形式（既定）。
_SOURCE_FORMAT_DIRECTIVE = re.compile(r">>SOURCE\s+(?:FORMAT\s+)?(?:IS\s+)?(FREE|FIXED)\b", re.I)
# `WITH DEBUGGING MODE`（ENVIRONMENT DIVISION の宣言）: これがあるファイルだけ 7桁目 `D` の
# デバッグ行を通常行として解析する（無ければ解析せず落とす・`_normalize_logical_lines` 参照）。
_WITH_DEBUGGING_MODE = re.compile(r"WITH\s+DEBUGGING\s+MODE", re.I)
# ファイルの様式判定（`_detect_column1_style` 参照）に使う見出し/レベル項目の検知。
# `ID` 単体も `ID DIVISION.`（`IDENTIFICATION` の短縮表記）として有効なため候補に含める・
# `\b` で他識別子の部分文字列（例: `INVALID DIVISION` 中の `ID`）に誤爆しないようにする。
_DIVISION_HEADER = re.compile(
    r"\b(?:IDENTIFICATION|ID|ENVIRONMENT|DATA|PROCEDURE)\s+DIVISION\b", re.I)
_LEVEL_ITEM_COLUMN1 = re.compile(r"^\d{2}\s+[A-Z0-9#@$-]", re.I)
# 固定列の優先証拠（`_detect_column1_style` 参照）: 8桁目（`line[7:72]`）から始まるレベル
# 番号付き項目定義。`_LEVEL_ITEM_COLUMN1` とはアンカーの基準列が異なるだけで文字集合は同じ。
_LEVEL_ITEM_FIXED = re.compile(r"^\s*\d{2}\s+[A-Z0-9#@$-]+", re.I)

COBOL_EXT = {".cbl", ".cob", ".cobol"}
COPYBOOK_EXT = {".cpy", ".copybook"}
JCL_EXT = {".jcl"}




def _is_seq_area(line: str) -> bool:
    """1〜6桁が連番領域として妥当か（数字または空白のみ）。`_is_comment` が7桁目 indicator の
    意味を前提してよいかを判定するためだけに使う（ファイルの様式そのものは行単位でなく
    `_detect_column1_style` がファイル単位で1つに決める）。"""
    return all(c.isdigit() or c == " " for c in line[:6])


def _is_comment(line: str) -> bool:
    """COBOL/JCL のコメント行（固定形式 桁7の `*`/`/`、行頭 `*`、JCL `//*`）。

    桁7の判定は連番領域（`_is_seq_area`）が妥当な行にだけ適用する——連番領域が無い行
    （1桁目からコードが始まる行）では7桁目に採番/インジケータの意味が無いため、行頭の
    `*` だけで判定する（さもないと `X = A * B` のような桁7に偶然演算子が来るコード行を
    誤ってコメントと判定しうる）。
    """
    s = line.lstrip()
    if s.startswith("*") or s.startswith("//*"):
        return True
    return len(line) > 6 and line[6] in "*/" and _is_seq_area(line)


def _is_comment_column1_style(line: str) -> bool:
    """列1始まり（column-1 style）ファイル専用のコメント判定: 1桁目が `*` の行だけ
    （`lstrip` しない——連番/字下げが無い様式では桁位置がそのまま意味を持つため）。
    固定形式・自由形式・JCL が使う `_is_comment` はこの関数を使わず、署名・挙動とも不変。
    """
    return line.startswith("*")


def _is_comment_fixed_columns(line: str) -> bool:
    """固定列（fixed columns）ファイル専用のコメント判定: 1〜6桁（連番領域）の内容に関係なく
    7桁目（0始まり index 6）の `*`/`/` をコメント行として扱う（`_is_seq_area` による連番領域の
    妥当性チェックはしない——様式判定で既に固定列と確定しているファイルでは、連番領域が数字
    専用か英数字混在かに関わらず7桁目 indicator の意味は変わらないため。`A00030*    COPY
    FAKE.` のような英数字連番＋7桁目 `*` の行を、`_is_seq_area` 経由の `_is_comment` は
    連番領域が数字専用でないという理由でコメントと判定できず取りこぼしていた）。
    `_normalize_logical_lines`（固定列分岐）が使う。
    自由形式・JCL・列1始まりが使う `_is_comment`/`_is_comment_column1_style` は署名・挙動とも不変。
    """
    return len(line) > 6 and line[6] in "*/"


def _is_word_char(ch: str) -> bool:
    """COBOL 識別子文字（`A-Z0-9#@$-`・`_PROGRAM_ID` 等の識別子系正規表現と同じ文字集合）。"""
    return bool(ch) and ch.upper() in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789#@$-"


def _is_free_format(text: str) -> bool:
    """`text`（ファイル全体）が自由形式か（`_SOURCE_FORMAT_DIRECTIVE` 参照）。

    物理行を順に見て、コメント行（`_is_comment`）でない行に現れた最初の指示文
    （FREE/FIXED どちらか）で決める——コメント中の指示文は見ない・指示が無ければ固定形式。
    """
    for line in text.splitlines():
        if _is_comment(line):
            continue
        m = _SOURCE_FORMAT_DIRECTIVE.search(line)
        if m:
            return m.group(1).upper() == "FREE"
    return False


def _detect_column1_style(lines: list) -> bool:
    """自由形式でない固定形式ファイルの様式（列1始まり／固定列）をファイル単位で1つに決める
    （行ごとの個別判定は行わない——連番の無い行の直後に続く継続行を誤って独立行にする等、
    ファイル内で様式を混在させると構造的に破綻するため）。

    **優先1: 固定列の証拠を先に見る**（列1証拠より優先し、見つかればそこで確定して列1証拠は
    見ない）。コメントでない物理行のうち、7桁目（0始まり index 6）が固定列の有効な
    indicator（空白・`D`/`d`・`-`・`*`・`/`）であり、かつ8桁目以降（`line[7:72]` の字下げを
    許容するため `.lstrip()` する）から次のいずれかが始まる行が1つでもあれば「固定列
    （fixed columns）」と確定する:

    - `IDENTIFICATION`/`ID`/`ENVIRONMENT`/`DATA`/`PROCEDURE` + `DIVISION` の見出し。
    - `PROGRAM-ID` 句。
    - レベル番号付き項目定義（`_LEVEL_ITEM_FIXED`）。

    これらは8桁目以降（字下げを許容）に見出し等が来ること自体が「連番領域＋indicator 桁が
    ある」証拠になる（優先1が無ければ、列1証拠だけを見た場合に `01 ABC PROGRAM-ID. P.` の
    ような行が `_LEVEL_ITEM_COLUMN1` にも偶然一致し、真に固定列のファイルを誤って列1始まりと
    判定してしまう）。7桁目 indicator を要求するのは逆方向の誤爆対策——列1始まりの文
    （例: `DISPLAY 01 UPON CONSOLE.`）は8桁目以降にレベル項目に似た断片（`01 UPON...`）を
    偶然含みうるが、7桁目が有効な indicator でなければ固定列の証拠として採用しない。

    **優先2: 固定列の証拠が1つも無いときだけ**、コメントでない物理行のうち以下のいずれかが
    1つでもあれば「列1始まり（column-1 style・採番/字下げが無く1桁目からコードが始まる
    様式）」と判定する:

    - 1〜7桁目以内（0始まりの `match.start() <= 6`）から `IDENTIFICATION`/`ID`/
      `ENVIRONMENT`/`DATA`/`PROCEDURE` + `DIVISION` の見出しが始まる行（固定列なら見出しは
      連番領域＋indicator 桁の後＝8桁目以降にしか現れないため、1〜7桁目以内に見出しが
      現れること自体が「連番領域が無い」証拠になる）。
    - 1桁目からレベル番号付き項目定義（`^\\d{2}\\s+[A-Z0-9#@$-]`）が始まる行。

    どちらの証拠も無ければ「固定列（fixed columns）」——ファイル内の全物理行に列位置の意味
    （連番領域・7桁 indicator・73桁以降の識別領域）があるとみなす。呼び出し元
    （`_normalize_logical_lines`）は自由形式でないと分かっているファイルにだけ使う。

    **検出限界**: DIVISION 見出しもレベル項目も無い列1始まりの断片（`COPY`/`REPLACE` だけの
    コピーブック、字下げされた列1系コピーブック等）は固定列（優先2の証拠が無いため既定の
    固定列）として扱われ、先頭7文字を連番領域として誤って落とす。列1始まりの文が固定列証拠に
    見える逆方向の誤爆は7桁目 indicator の要求で抑止するが完全ではない（対応する場合は
    CODE-2「静的解析の深化」の対象）。
    """
    for line in lines:
        if _is_comment(line):
            continue
        if line[6:7] not in (" ", "D", "d", "-", "*", "/"):
            continue
        code_area = line[7:72].lstrip()
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
    """固定列（fixed columns）ファイルに `WITH DEBUGGING MODE` の宣言があるか
    （`_WITH_DEBUGGING_MODE` 参照）。

    唯一の呼び出し元（`_normalize_logical_lines`）は固定列と確定したファイルにしか呼ばない
    ——列1始まりファイルはデバッグ行（7桁目 indicator）という概念自体を持たないため
    デバッグ行判定をしない（`_normalize_logical_lines` の列1始まり分岐を参照）。

    引数 `logical` は継続結合・列切り出し（1〜6桁の連番領域・73桁以降の識別領域の除外）が
    済んだ論理行の列——`(text, first_physical_line, segment_starts)` の3値タプル（本判定は
    `segment_starts` を使わない）を渡す。呼び出し元は D 行を継続結合の状態機械から完全に
    除外した第1段の論理行（`_build_fixed_column_logical` の `include_debug=False` 結果）
    だけを渡す——宣言はデバッグ行自体には書かれないため、D 行由来の論理行を対象に含める
    必要が無い。

    各論理行の `text` に `_strip_quoted` を掛けてから探す。継続結合済みの論理行を対象にする
    ため、`DISPLAY 'WITH DEBUGGING MODE'.` のような単一行内リテラルだけでなく、継続行で
    閉じる複数行リテラル（`_normalize_logical_lines` が既に1つの論理行へ結合済み）の中身も
    `_strip_quoted` だけで除外できる——行をまたぐ引用符状態を別途持ち越す必要はない。
    """
    return any(_WITH_DEBUGGING_MODE.search(_strip_quoted(text))
               for text, _line_no, _segs in logical)


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


def _scan_quote_state(s: str, state: str | None) -> str | None:
    """`s` を左から走査し、`state`（`None`／`'`／`\"`＝走査開始時点の引用符状態）から続けた場合の
    走査後の状態を返す。二重化（`''`／`\"\"`）は1個のリテラル内文字として扱う（開閉のトグルに
    しない）——`_normalize_logical_lines` の継続行がリテラル継続かどうかの判定に使う。
    """
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
                i += 2                              # 二重化＝リテラル内の1文字（トグルしない）
                continue
            state = None
            i += 1
            continue
        i += 1
    return state


def _build_fixed_column_logical(physical_lines: list, include_debug: bool) -> tuple:
    """固定列（fixed columns）ファイルの物理行から論理行を組み立てる（継続結合の状態機械の実体・
    `_normalize_logical_lines` の下請け）。

    `include_debug=False`（宣言の有無を判定する第1段）: 7桁目 `D`/`d`（デバッグ行）を継続結合の
    状態機械から完全に見えない存在として飛ばす——`flush()` を呼ばず、`frags`/`frag_line`/
    `quote_state` のいずれも更新しない。そのため D 行の直後に続く7桁目 `-`（継続行）は、D 行を
    素通りして**その手前まで組み立てていた論理行**へ結合される（D 行自身が継続の起点・対象に
    なることは無い）。D 行はその場で `debug_lines` に `(line_no, snippet)` として個別に積む
    （コメント・空行の除外は通常行と同じ判定を先に通す）。

    `include_debug=True`（宣言ありと確定した後の第2段）: D 行も通常行と同じ手順で論理行に
    組み込む（継続結合も同様に扱う・`debug_lines` には何も積まない）。

    どちらの場合も、7桁目 indicator が `-`（継続行）の行は直前の論理行の末尾へ12桁目以降を
    連結する——直前行が引用符（`'`／`"`）を閉じていない（同種引用符の出現数が奇数・二重化
    `''`／`""` は1個の文字として数える）場合は**リテラル継続**として列位置を維持する（直前行の
    72桁までをそのまま＝末尾空白もリテラルの一部にし、継続行は先頭の非空白が同種引用符なら
    それを除去して連結する）。それ以外（非リテラル継続）は「直前行の最後の非空白文字」と
    「継続行の最初の非空白文字」を空白なしで連結する（`prev.rstrip() + cont.lstrip()`）。

    コメント行（`_is_comment_fixed_columns`）と7〜72桁が空白だけの物理行は結合対象の探索からも
    除外する。戻り値は `(logical, debug_lines)`——`logical` は `(text, first_physical_line,
    segment_starts)` の列（`first_physical_line` は常にその論理行の先頭の物理行）。
    `segment_starts` は `text` 中で継続結合の境界（＝元の物理行の境目）となるオフセットの列
    （昇順・先頭は常に `0`）——EXEC SQL の `--` 行コメント走査（`analyzers/cobol.py`）が、
    継続結合で1つの論理行へ連結された複数物理行をまたいでコメントを伸ばさないために使う
    （物理行境界を跨いだ先の内容までコメントとして飲み込まない・§4(d) RV是正）。
    """
    logical: list = []
    debug_lines: list = []
    frags: list = []
    frag_line = None
    frag_segments: list = []                         # (text 中のオフセット) の列＝物理行境界
    frag_len = 0                                      # "".join(frags) の長さを都度追跡（二次時間対策）
    quote_state: str | None = None                  # 直近に取り込んだ断片終端時点の引用符状態

    def flush():
        nonlocal frags, frag_line, frag_segments, frag_len
        if frags:
            logical.append(("".join(frags), frag_line, tuple(frag_segments)))
        frags, frag_line, frag_segments, frag_len = [], None, [], 0

    for i, line in enumerate(physical_lines, 1):
        if _is_comment_fixed_columns(line):
            continue
        if not line[6:72].strip():
            continue                                # 空行（7〜72桁が空白のみ）は結合対象から除外
        indicator = line[6:7]
        if indicator in ("D", "d") and not include_debug:
            debug_lines.append((i, line[7:72].strip()[:120]))
            continue                                # 状態機械から見えない存在として飛ばす
        if indicator == "-":
            cont_area = line[11:72]
            if not frags:
                cont = cont_area.lstrip()
                if cont[:1] in ("'", '"'):
                    cont = cont[1:]
                frags = [cont]
                frag_line = i                        # 直前行が無い異常系: 単独行扱い（推測しない）
                frag_segments = [0]
                frag_len = len(cont)
                quote_state = _scan_quote_state(cont, None)
                continue
            if quote_state is not None:
                # リテラル継続: 直前行の列位置を維持済み（末尾空白もリテラルの一部）。継続行の
                # 先頭非空白が同種引用符ならそれを除去して連結する。
                stripped = cont_area.lstrip()
                cont = stripped[1:] if stripped[:1] == quote_state else cont_area
            else:
                # 非リテラル継続: 直前断片の末尾空白＋継続行の先頭空白を除去して直結する
                # （rstrip で縮む分だけ、持ち越した長さも合わせて縮める）。
                old_len = len(frags[-1])
                frags[-1] = frags[-1].rstrip()
                frag_len -= old_len - len(frags[-1])
                cont = cont_area.lstrip()
            frag_segments.append(frag_len)            # この継続行の断片が始まるオフセット
            frags.append(cont)
            frag_len += len(cont)
            quote_state = _scan_quote_state(cont, quote_state)
            continue
        flush()
        frag_line = i
        frags = [line[7:72]]
        frag_segments = [0]
        frag_len = len(frags[0])
        quote_state = _scan_quote_state(frags[0], None)
    flush()
    return logical, debug_lines


def _normalize_logical_lines(text: str, free_format: bool) -> tuple:
    """COBOL の物理行を論理行へ正規化する（COBOL/コピーブック共通・S1）。

    自由形式（`free_format=True`）にはこの正規化を適用しない——物理行をそのまま
    `(line, line_no)` の列として返す（列制限・継続行結合・デバッグ行判定をしない・コメント判定は
    呼び出し側が従来どおり `_is_comment` で行う）。`debug_dropped` は常に空（indicator 桁の概念が
    自由形式には無い）。

    自由形式でないファイルは、様式（列1始まり／固定列）をファイル単位で1つに決める
    （`_detect_column1_style`・行ごとの個別判定はしない）。

    **列1始まり（column-1 style）**: 採番/字下げが無く1桁目からコードが始まる様式。列の
    切り落とし・継続行結合・デバッグ行判定を一切せず、コメント行（`_is_comment_column1_style`
    ＝1桁目が `*` の行のみ）以外の物理行をそのまま1つずつ論理行にする（前後の行と結合しない
    ——`COPY PART` の次行に継続行らしき `-B.` があっても2つの独立した論理行のまま）。
    `debug_dropped` は常に空。

    **固定列（fixed columns）**: 全物理行に列位置の意味があるとみなし、無条件に
    1〜6桁の連番領域と 73桁以降（識別領域）を落とし、8〜72桁の実コード領域だけを残す
    （継続結合・コメント／空行の除外の詳細は `_build_fixed_column_logical` 参照）。

    7桁目 indicator が `D`/`d`（デバッグ行）の有無は2段構えで扱う——D 行を継続結合の対象に
    含めるかどうかで組み立て結果が変わりうるため、宣言の有無を先に確定させてから最終結果を
    選ぶ必要がある:

    1. まず D 行を継続結合の状態機械から除外した論理行（`_build_fixed_column_logical` の
       `include_debug=False`）を組み立てる。この時点の論理行は D 行由来のものを一切含まない
       ため、そのまま `_has_debugging_mode` に渡して `WITH DEBUGGING MODE` 宣言の有無を判定する。
    2. 宣言が無ければこの論理行をそのまま採用し、除外した D 物理行を `(line_no, snippet)` として
       `debug_dropped` に積む（黙って消さない・呼び出し側が `Dropped("debug_line", ...)` に
       変換する）。宣言があれば D 行も通常行として組み込んで論理行を**組み直し**（`include_debug=
       True`）、その結果を採用する（`debug_dropped` は空）。

    戻り値は `(entries, debug_dropped)` の2値タプル。`entries` は `(logical_text,
    first_physical_line, segment_starts)` の列——`first_physical_line` は常にその論理行の
    **先頭の物理行**（1始まり）を指す（継続結合後も来歴は変わらない）。`segment_starts` は
    `logical_text` 中の物理行境界オフセットの列（`_build_fixed_column_logical` 参照）——
    自由形式／列1始まりは継続結合をしないため常に `(0,)`（論理行＝物理行1個）。

    **未対応構文**: 72桁目で閉じる引用符の直後、次の継続行が同種の引用符で始まる特殊な継続
    （二重引用符化 `''`/`""` と紛らわしい境界ケース）は対応しない（CODE-2「静的解析の深化」
    の対象）。
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
