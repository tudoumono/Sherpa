"""シェル／バッチアナライザ（アナライザ拡張 波3 レーン B＝シェル/バッチファイル定義）。

`.sh`/`.bash`/`.ksh`/`.zsh`（POSIX 系）と `.bat`/`.cmd`（Windows バッチ）を**全件受理**する
（`accepts()` は既定のまま）——1本のクラスで拡張子により POSIX/bat の2書式へ分岐する
（`_is_bat()`）。ファイル自体を主体定義（`Batch`・`extra["batch_kind"]` で `"shell"`/`"bat"` を
区別）とする。関数（`name() {`／`function name {`）・bat のラベル（`:label`）は children にしない
（構造的な子定義としては認識しない・検出限界として明記）。

**設定キー children（`Config`・キー単位・properties/xml_config と同じ契約）**: POSIX の
`export KEY=value`／`KEY=value`（行頭・`readonly`/`declare -x` も同形・先頭に代入が付いた
コマンド行 `KEY=value cmd…` も代入部分だけを拾う）、bat の `set KEY=value`／`set "KEY=value"`
（外側の引用符は構文として扱い、内側からキー/値を取る）
→ `DefItem(label="Config", name=<裸キー>, cid_key="key:env:"+<裸キー>,
extra={"config_value": ..., "key_kind": "env"})`（`key_kind="env"`＝RV 裁定のキー種別名前空間・
シェル/バッチの環境変数系・`cid_key` に `key_kind` を挟むのは xml_config.py 等の他の Config キー
producer と cid 形式を揃えるため）。同一ファイル内の重複キーは最初の1つ
（properties/yaml_config と同じ既定＝黙って後続を捨てる・XML だけ申告する既存の非対称はここでも
変えない）。**値なし宣言**
（`export KEY`／`readonly KEY`／`declare -x KEY`）は Config 定義にせず、継承依存として
`ACCESSES(via=config_key, key_kind="env")` を返す（値を持たない＝このファイルでは定義しておらず
外部の環境へ依存するだけのため）。`env KEY=value cmd…`（`env` 組み込みが子プロセスにだけ与える
一時代入）の `KEY=value` も Config 定義にはしない（後続コマンドの解析だけ行う）。

**参照**（`extract_refs`）:
- 行は未引用の `;`・`|`・`&&`・`||` でコマンド位置に分割し、`then`/`do`/`else` の直後・`{`/`(`
  の直後もコマンド開始位置として扱う。各コマンド開始位置では `exec`/`nohup`/`time`/`sudo`/`env`
  （代入群を挟む・代入自体は解析対象にしない）をラッパーとして読み飛ばしてから分類する——
  複数コマンドを持つ1行（`true && java …`／`echo ok | node …`／`if …; then ./RUN; fi`／
  `for x in …; do ./RUN; done` 等）でも各位置の呼び出しが拾える。
- 他スクリプトの呼び出し（`./x.sh`／`sh x.sh`／`bash x.sh`／`. x.sh`／`source x.sh`・bat の
  `call x.bat`／`x.bat`）→ `INVOKES(via=include, include_path=<元のパス>)`（kind=`Batch`・
  C アナライザの `#include` と同じ2段解決＝world_graph 側が相対パス完全一致→拡張子込み
  basename の同一 top_scope 内最近傍にフォールバックする）。対象パスの拡張子が既知のスクリプト系
  （`.sh`/`.bash`/`.ksh`/`.zsh`/`.bat`/`.cmd`）でなければこの判定は成立しない（例: `./PAYROLL` は
  次項の実行ファイル呼び出しへ回る）。
- `java` 起動行はトークン単位で JVM オプション（`-D…`／`-X…`は単体、`-cp`/`-classpath`/`-p`/
  `--…` 系は値を伴うため次トークンごと）を読み飛ばし、最初の非オプション引数を見る。`.` を含む
  識別子形（FQCN）なら `INVOKES(via=call, qualified=True)`（kind=`Module`）。`-jar <jar>` に
  達したら以降は読まず `Dropped("shell_jar", line, jar)`（jar はノード化しない・main class の
  読み取りが構造的にできないため）。
- COBOL/実行ファイルの単独行呼び出し（`./PGM`／`PGM`・コマンド全体が大文字英数字のみ）→
  `INVOKES(via=call)`（kind=`Module`）。
- `sqlplus … @x.sql`／`psql -f x.sql`／`mysql < x.sql` → `Dropped("shell_sql_script", line, path)`
  （SQL スクリプトはノード化しない検出限界）。
- `python x.py`／`perl …` → `Dropped("shell_unsupported_runtime", line, "python"|"perl")`。
  `node x.js` → `INVOKES(via=call, include_path=<元のパス>)`（kind=`Module`・JS の `Module`
  命名＝拡張子込みファイル名と揃える）。
- 変数参照 `$KEY`／`${KEY}`／`${KEY:-d}`／`${KEY-d}`／`${KEY:=d}`／`${KEY:?}`／`${KEY#…}`／
  `${#KEY}`（POSIX）・`%KEY%`（bat）→ `ACCESSES(via=config_key, key_kind="env")`（`Config` キー・
  共通層の A9＝同一 top_scope 内の同名キー全件へ接続）。**大文字＋アンダースコア（＋数字）のみ**を
  設定キーとみなす——`$1`/`$@`/`$?` 等の位置/特殊変数、小文字のローカル変数は対象外（先頭文字が
  大文字である正規表現でそのまま除外される）。`\\$KEY`（バックスラッシュでエスケープされた
  リテラル `$`）は有効なエスケープとみなし変数参照として拾わない。
  **定義側が同一ファイル内で `KEY=` を持つ変数は自己参照とみなし、参照エッジを張らない**
  （ローカル変数の読み書きであって外部設定キーへの依存ではないため）。
- bat の組み込みコマンド（`ECHO`/`EXIT`/`SET`/`GOTO`/`PAUSE`/`CLS`/`TYPE`/`COPY`/`DEL`/`MOVE`/
  `MKDIR`/`RMDIR`/`IF`/`FOR`/`CALL`/`START`/`PUSHD`/`POPD`/`SHIFT`/`TITLE`/`COLOR`/`REM`）は
  実行ファイル単独行呼び出しの判定より先に除外する（大文字だけの組み込みコマンド名を
  `Module` と誤認しない）。
- `crontab`/`nohup`/`&` は特別扱いしない（他のどの規則にも一致しない限り無視される）。

**サニタイズ**: `#`（POSIX・未引用の位置のみ）／`REM`・`::`（bat・行頭のみ）はコメントとして
無視する。シングルクォート（`'…'`）の中身は空白化する（POSIX は変数展開されない構文のため
変数参照走査からも除外される）。ダブルクォート（`"…"`）は中身を保持する——変数参照は `"…"` 内も
拾う仕様のため。ヒアドキュメント（未引用の `<<EOF` 〜 終端行・`<<-` も同形）の本文は読み飛ばし、
1件ずつ `Dropped("shell_heredoc", 開始line, 終端識別子)` を申告する（POSIX のみ・bat には無い構文）。
ヒアドキュメント開始の判定はコメント・引用符を認識した上で行う——`# … <<EOF`（コメント内）や
`"literal <<EOF"`（引用符内の文字列）は演算子として扱わない。

**検出限界**: 行継続（バックスラッシュ改行）は結合しない（単一物理行のみを見る・他アナライザと
同じ「見逃しは許容するが誤った候補は作らない」流儀）。関数/ラベルは children にしない。
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

SHELL_EXT = frozenset({".sh", ".bash", ".ksh", ".zsh"})
BAT_EXT = frozenset({".bat", ".cmd"})
SHELL_BATCH_EXT = SHELL_EXT | BAT_EXT

_VALUE_TRUNCATE = 200

# 既知のスクリプト系拡張子（呼び出し先の1段目判定・大文字小文字を区別しない）。
_SCRIPT_CALL_EXTS = (".sh", ".bash", ".ksh", ".zsh", ".bat", ".cmd")

# --- 設定キー定義（`KEY=value`）--- 値は引用符付き文字列か非空白の1トークン——後続の
# コマンド（`VAR=1 ./run.sh` 等）と切り分けるため、値は行末までではなく最初の空白までで止める。
_ASSIGN_VALUE = r'(?:"[^"]*"|\S*)'
_POSIX_ASSIGN = re.compile(
    r"^\s*(?:export\s+|readonly\s+|declare\s+-x\s+)?([A-Za-z_][A-Za-z0-9_]*)="
    rf"({_ASSIGN_VALUE})(?:\s+\S.*)?\s*$"
)
# bat の `set KEY=value`／`set "KEY=value"`（外側の引用符は構文——中身からキー/値を取る）。
_BAT_ASSIGN = re.compile(
    r'^\s*set\s+(?:"([A-Za-z_][A-Za-z0-9_]*)=([^"]*)"|([A-Za-z_][A-Za-z0-9_]*)=(.*))\s*$',
    re.IGNORECASE)
# 値なし宣言（`export KEY`／`readonly KEY`／`declare -x KEY`）＝ Config 定義にはせず
# 継承依存の ACCESSES として扱う（RV 裁定）。
_POSIX_DECL_NO_VALUE = re.compile(r"^\s*(?:export|readonly|declare\s+-x)\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")

# --- 他スクリプト呼び出し ---
_POSIX_DOT_SOURCE = re.compile(r"^\s*\.\s+(\S+)")
_POSIX_SOURCE_KW = re.compile(r"^\s*source\s+(\S+)")
_POSIX_INTERP_KW = re.compile(r"^\s*(?:sh|bash|ksh|zsh)\s+(\S+)")
_POSIX_BARE_PATH = re.compile(r"^\s*(\./\S+)")
_BAT_CALL_KW = re.compile(r"^\s*call\s+(\S+)", re.IGNORECASE)
_BAT_BARE_PATH = re.compile(r"^\s*(\S+\.(?:bat|cmd))\b", re.IGNORECASE)

# --- コマンド位置の分割（未引用の `;`・`|`・`&&`・`||`）／制御キーワード／ラッパー ---
_SEP = re.compile(r"&&|\|\||;|\|")
_LEADING_CONTROL = re.compile(r"^\s*(?:then|do|else)\b\s*")
_LEADING_BRACE = re.compile(r"^\s*[{(]\s*")
_WRAPPER_KW = re.compile(r"^\s*(?:exec|nohup|time|sudo|env)\s+", re.IGNORECASE)
# ラッパー（特に `env`）の後ろに続く代入群は Config 化せず読み飛ばすだけ（`VAR=1 ./run.sh` の
# 先頭代入自体もここで読み飛ばし、後続コマンドの開始位置を露出させる）。
_ASSIGN_PREFIX = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|\S*)\s*")

# --- java 呼び出し ---
_JAVA_LINE = re.compile(r"^\s*java\b")
_JAVA_SHORT_OPT_WITH_ARG = frozenset({"-cp", "-classpath", "-p"})
_FQCN_TOKEN = re.compile(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b")

# --- SQL スクリプト呼び出し（ノード化しない）---
_SQL_AT = re.compile(r"@(\S+\.sql)\b", re.IGNORECASE)
_SQL_DASH_F = re.compile(r"-f\s+(\S+\.sql)\b", re.IGNORECASE)
_SQL_REDIRECT = re.compile(r"<\s*(\S+\.sql)\b", re.IGNORECASE)

# --- 他ランタイム ---
_NODE_KW = re.compile(r"^\s*node\s+(\S+\.m?js)\b", re.IGNORECASE)
_PYTHON_KW = re.compile(r"^\s*python[23]?\s+\S", re.IGNORECASE)
_PERL_KW = re.compile(r"^\s*perl\s+\S", re.IGNORECASE)

# --- 実行ファイルの単独行呼び出し（COBOL 等・行全体が大文字英数字のみ）---
_BARE_EXEC = re.compile(r"^(?:\./)?([A-Z][A-Z0-9]*)$")

# bat の組み込みコマンド（大文字だけの単独行でも `Module` の実行ファイル呼び出しにしない）。
_BAT_BUILTINS = frozenset({
    "ECHO", "EXIT", "SET", "GOTO", "PAUSE", "CLS", "TYPE", "COPY", "DEL", "MOVE",
    "MKDIR", "RMDIR", "IF", "FOR", "CALL", "START", "PUSHD", "POPD", "SHIFT", "TITLE", "COLOR", "REM",
})

# --- 変数参照（大文字＋アンダースコア＋数字のみ＝設定キー扱い・`\$` エスケープは除外）---
_VAR_DOLLAR_BRACE = re.compile(r"(?<!\\)\$\{#?([A-Z][A-Z0-9_]*)(?:[:#%][^}]*)?\}")
_VAR_DOLLAR_BARE = re.compile(r"(?<!\\)\$([A-Z][A-Z0-9_]*)\b")
_VAR_PERCENT = re.compile(r"%([A-Z][A-Z0-9_]*)%")

# --- bat の行頭コメント（`REM`／`::`）---
_BAT_REM = re.compile(r"^rem(\s|$)", re.IGNORECASE)

# --- ヒアドキュメント開始（`<<EOF`／`<<-EOF`／引用符付き終端識別子）---
_HEREDOC_START = re.compile(r"<<-?\s*([\'\"]?)(\w+)\1")


def _sanitize_line(line: str, is_bat: bool) -> str:
    """コメント除去＋シングルクォート内を空白化した1行を返す（構造マッチ／変数参照走査の共通材料）。

    ダブルクォート内は保持する（変数参照は `"…"` 内も拾う仕様のため）。POSIX の `#` は未引用の
    ものだけをコメント開始として扱う（bat の `REM`/`::` は行頭のみなので呼び出し側で先に判定する）。
    """
    out: list = []
    in_s = in_d = False
    for ch in line:
        if not is_bat and not in_s and not in_d and ch == "#":
            break
        if ch == "'" and not in_d:
            in_s = not in_s
            out.append(" ")
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            out.append(ch)
            continue
        if in_s:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _heredoc_probe(line: str) -> str:
    """ヒアドキュメント開始判定専用のマスク済み行を返す——未引用の `#` 以降はコメントとして
    切り捨て、シングル/ダブル引用符の中身は空白化する（`# … <<EOF` や `"literal <<EOF"` のような
    コメント/文字列内の出現を演算子として誤認しないための材料）。"""
    out: list = []
    in_s = in_d = False
    for ch in line:
        if not in_s and not in_d and ch == "#":
            break
        if ch == "'" and not in_d:
            in_s = not in_s
            out.append(" ")
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            out.append(" ")
            continue
        if in_s or in_d:
            out.append(" ")
        else:
            out.append(ch)
    return "".join(out)


def _unquote(value: str) -> str:
    """値の前後を囲む1組の引用符（`'…'`／`"…"`）だけを剥がす（属性の中身までは踏み込まない）。"""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _heredoc_skip_lines(lines: list) -> tuple:
    """ヒアドキュメント本文（開始行の次行〜終端行）の行番号集合と、`Dropped` 候補（開始行ごとに
    1件）を返す（POSIX のみ・bat には無い構文なので呼び出し側で `is_bat` の場合は呼ばない）。"""
    skip: set = set()
    dropped: list = []
    i, n = 0, len(lines)
    while i < n:
        m = _HEREDOC_START.search(_heredoc_probe(lines[i]))
        if m:
            delim = m.group(2)
            dropped.append(Dropped("shell_heredoc", i + 1, delim))
            j = i + 1
            while j < n and lines[j].strip() != delim:
                skip.add(j + 1)
                j += 1
            if j < n:
                skip.add(j + 1)          # 終端行自体も本文の走査対象にしない
            i = j + 1
            continue
        i += 1
    return skip, dropped


def _is_comment_line(stripped: str, is_bat: bool) -> bool:
    if is_bat:
        return bool(_BAT_REM.match(stripped)) or stripped.startswith("::")
    return stripped[:1] == "#"


def _scan_assignments(lines: list, is_bat: bool) -> list:
    """`KEY=value`（POSIX の `export`/`readonly`/`declare -x` 込み・bat の `set`）を
    `[(key, value, line_no), ...]` で返す（`collect_defs` の children 抽出と、`extract_refs` の
    自己参照除外の両方が使う単一の材料）。"""
    skip = set() if is_bat else _heredoc_skip_lines(lines)[0]
    out: list = []
    pattern = _BAT_ASSIGN if is_bat else _POSIX_ASSIGN
    for line_no, raw in enumerate(lines, 1):
        if line_no in skip:
            continue
        stripped = raw.lstrip()
        if _is_comment_line(stripped, is_bat):
            continue
        m = pattern.match(_sanitize_line(raw, is_bat))
        if m:
            if is_bat:
                key = m.group(1) if m.group(1) is not None else m.group(3)
                value = m.group(2) if m.group(1) is not None else m.group(4)
            else:
                key, value = m.group(1), m.group(2)
            out.append((key, value, line_no))
    return out


def _normalize_sep(path: str) -> str:
    return path.replace("\\", "/")


def _match_script_call(line: str, is_bat: bool) -> str | None:
    """他スクリプト呼び出しのパス文字列（拡張子が既知のスクリプト系のときだけ）を返す。"""
    patterns = (_BAT_CALL_KW, _BAT_BARE_PATH) if is_bat else (
        _POSIX_DOT_SOURCE, _POSIX_SOURCE_KW, _POSIX_INTERP_KW, _POSIX_BARE_PATH)
    for pat in patterns:
        m = pat.match(line)
        if m:
            target = m.group(1)
            if target.lower().endswith(_SCRIPT_CALL_EXTS):
                return target
    return None


def _match_java(line: str):
    """`java` 起動行から呼び出し先を返す（`("class", FQCN)`／`("jar", jar名)`／該当なしは
    `None`）。JVM オプション（`-D…`／`-X…`は単体、`-cp`/`-classpath`/`-p`/`--…` 系は値を伴う）を
    トークン単位で読み飛ばし、最初の非オプション引数を見る——class mode では `.` を含む識別子
    （FQCN）のときだけ main class として返す（旧来どおり非修飾クラス名は対象外）。"""
    if not _JAVA_LINE.match(line):
        return None
    tokens = line.split()
    i, n = 1, len(tokens)
    while i < n:
        tok = tokens[i]
        if tok == "-jar":
            if i + 1 < n:
                return "jar", PurePosixPath(tokens[i + 1]).name
            return None
        if tok.startswith("-D") or tok.startswith("-X"):
            i += 1
            continue
        if tok in _JAVA_SHORT_OPT_WITH_ARG or tok.startswith("--"):
            i += 2
            continue
        if tok.startswith("-"):
            i += 1
            continue
        if _FQCN_TOKEN.fullmatch(tok):
            return "class", tok
        return None
    return None


def _match_sql_script(line: str) -> str | None:
    """`sqlplus`/`psql`/`mysql` の SQL スクリプト実行行からパスを返す（該当なしは `None`）。"""
    stripped = line.lstrip()
    first = stripped.split(None, 1)[0].lower() if stripped else ""
    if first == "sqlplus":
        m = _SQL_AT.search(line)
    elif first == "psql":
        m = _SQL_DASH_F.search(line)
    elif first == "mysql":
        m = _SQL_REDIRECT.search(line)
    else:
        m = None
    return m.group(1) if m else None


def _match_other_runtime(line: str):
    """`node`/`python`/`perl` 起動行を判定する（`("node", パス)`／`("python"|"perl", None)`／`None`）。"""
    m = _NODE_KW.match(line)
    if m:
        return "node", m.group(1)
    if _PYTHON_KW.match(line):
        return "python", None
    if _PERL_KW.match(line):
        return "perl", None
    return None


def _split_top_level(line: str) -> list:
    """未引用の `;`・`|`・`&&`・`||` でコマンド位置を分割する（二重引用符内は分割対象にしない・
    シングルクォートは呼び出し側の `_sanitize_line` で既に空白化済み）。"""
    segments: list = []
    start = 0
    in_d = False
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if ch == '"':
            in_d = not in_d
            i += 1
            continue
        if not in_d:
            m = _SEP.match(line, i)
            if m:
                segments.append(line[start:i])
                i = start = m.end()
                continue
        i += 1
    segments.append(line[start:])
    return segments


def _strip_command_prefix(segment: str) -> str:
    """コマンド開始位置に前置される制御キーワード（`then`/`do`/`else`・`{`/`(`）とラッパー
    （`exec`/`nohup`/`time`/`sudo`/`env`＋代入群）を除去し、実行コマンドの開始位置だけを残す
    （`VAR=1 ./run.sh` のような先頭代入付きコマンドも代入部分を読み飛ばす）。"""
    s = segment
    changed = True
    while changed:
        changed = False
        for pat in (_LEADING_CONTROL, _LEADING_BRACE, _WRAPPER_KW, _ASSIGN_PREFIX):
            m = pat.match(s)
            if m:
                s = s[m.end():]
                changed = True
                break
    return s


def _classify_line(line: str, is_bat: bool):
    """1コマンド位置の構造マッチ結果を1つ返す（優先順位はこの判定順のまま・該当なしは `None`）。"""
    target = _match_script_call(line, is_bat)
    if target is not None:
        return "script_call", target
    java = _match_java(line)
    if java is not None:
        return "java", java
    sql_path = _match_sql_script(line)
    if sql_path is not None:
        return "sql", sql_path
    runtime = _match_other_runtime(line)
    if runtime is not None:
        return "runtime", runtime
    bare = _BARE_EXEC.fullmatch(line.strip())
    if bare and not (is_bat and bare.group(1) in _BAT_BUILTINS):
        return "bare_exec", bare.group(1)
    return None


def _scan_var_refs(line: str, is_bat: bool) -> list:
    if is_bat:
        return [m.group(1) for m in _VAR_PERCENT.finditer(line)]
    return ([m.group(1) for m in _VAR_DOLLAR_BRACE.finditer(line)]
            + [m.group(1) for m in _VAR_DOLLAR_BARE.finditer(line)])


class ShellBatchAnalyzer(Analyzer):
    """シェル（`.sh`/`.bash`/`.ksh`/`.zsh`）／バッチ（`.bat`/`.cmd`）→ `Batch`（primary）。
    `export`/`set` 等のキー代入 → `Config` children（`key_kind="env"`）。他スクリプト呼び出し・
    `java`/`node` 起動・実行ファイル単独行呼び出し・変数参照を参照候補として返す。"""

    name = "shell"
    extensions = SHELL_BATCH_EXT
    doctype = "shell"

    @staticmethod
    def _is_bat(rel_path: str) -> bool:
        return PurePosixPath(rel_path).suffix.lower() in BAT_EXT

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        is_bat = self._is_bat(rel_path)
        primary = DefItem(label="Batch", name=PurePosixPath(rel_path).name,
                          extra={"batch_kind": "bat" if is_bat else "shell"})
        lines = text.splitlines()
        children: list = []
        seen: set = set()
        for key, value, line_no in _scan_assignments(lines, is_bat):
            if key in seen:                              # 同一ファイル内の重複キーは最初の1つ
                continue
            seen.add(key)
            children.append(DefItem(label="Config", name=key, line=line_no,
                                    cid_key=f"key:env:{key}",
                                    extra={"config_value": _unquote(value)[:_VALUE_TRUNCATE],
                                          "key_kind": "env"}))
        return DefResult(primary=primary, children=children)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        is_bat = self._is_bat(rel_path)
        lines = text.splitlines()
        if is_bat:
            skip: set = set()
            dropped: list = []
        else:
            skip, dropped = _heredoc_skip_lines(lines)
            dropped = list(dropped)
        local_keys = {k for k, _v, _ln in _scan_assignments(lines, is_bat)}

        refs: list = []
        for line_no, raw in enumerate(lines, 1):
            if line_no in skip:
                continue
            stripped = raw.lstrip()
            if _is_comment_line(stripped, is_bat):
                continue
            line = _sanitize_line(raw, is_bat)

            if not is_bat:
                decl_m = _POSIX_DECL_NO_VALUE.match(line)
                if decl_m:
                    refs.append(RefCandidate("ACCESSES", "Config", decl_m.group(1), line_no,
                                             extra={"via": "config_key", "key_kind": "env"}))

            for segment in _split_top_level(line):
                classified = _classify_line(_strip_command_prefix(segment), is_bat)
                if classified is None:
                    continue
                kind, payload = classified
                if kind == "script_call":
                    norm = _normalize_sep(payload)
                    refs.append(RefCandidate("INVOKES", "Batch", PurePosixPath(norm).name, line_no,
                                             extra={"via": "include", "include_path": norm}))
                elif kind == "java":
                    java_kind, name = payload
                    if java_kind == "jar":
                        dropped.append(Dropped("shell_jar", line_no, name))
                    else:
                        refs.append(RefCandidate("INVOKES", "Module", name, line_no,
                                                 extra={"via": "call", "qualified": True}))
                elif kind == "sql":
                    dropped.append(Dropped("shell_sql_script", line_no, payload))
                elif kind == "runtime":
                    rt, arg = payload
                    if rt == "node":
                        norm = _normalize_sep(arg)
                        refs.append(RefCandidate("INVOKES", "Module", PurePosixPath(norm).name, line_no,
                                                 extra={"via": "call", "include_path": norm}))
                    else:
                        dropped.append(Dropped("shell_unsupported_runtime", line_no, rt))
                elif kind == "bare_exec":
                    refs.append(RefCandidate("INVOKES", "Module", payload, line_no, extra={"via": "call"}))

            for var_name in _scan_var_refs(line, is_bat):
                if var_name in local_keys:                 # 自己参照（同一ファイル内で定義済み）は張らない
                    continue
                refs.append(RefCandidate("ACCESSES", "Config", var_name, line_no,
                                         extra={"via": "config_key", "key_kind": "env"}))

        return RefResult(refs=refs, dropped=dropped)
