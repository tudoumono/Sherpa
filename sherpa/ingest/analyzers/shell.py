"""シェル／バッチアナライザ。ファイル自体を主体定義（`Batch`・`extra["batch_kind"]` は `"shell"`/`"bat"`）とし、設定キーを `Config` children、他スクリプト・プログラム呼び出しと環境変数の参照を参照候補として返す。

`.sh`/`.bash`/`.ksh`/`.zsh`（POSIX）と `.bat`/`.cmd`（Windows バッチ）を全件受理し、拡張子で書式を分岐する（`_is_bat()`）。関数・bat ラベルは children にしない。

設定キー children: POSIX の `export KEY=value`／`KEY=value`（`readonly`/`declare -x` 含む）、bat の `set KEY=value` → `DefItem(label="Config", name=<裸キー>, cid_key="key:env:"+<裸キー>, extra={"config_value", "key_kind": "env"})`。同一ファイル内の重複キーは最初の1つ。値なし宣言（`export KEY` 等）は定義にせず `ACCESSES(via=config_key, key_kind="env")` を返す。`env KEY=value cmd` の一時代入は定義にしない。

参照（`extract_refs`）:
- 行を未引用の `;`・`|`・`&&`・`||` でコマンド位置に分割し、`then`/`do`/`else`・`{`/`(` の直後も開始位置とする。`exec`/`nohup`/`time`/`sudo`/`env` は読み飛ばして分類する。
- 他スクリプト呼び出し（`./x.sh`・`sh x.sh`・`. x.sh`・`source x.sh`・bat の `call x.bat`）→ `INVOKES(via=include, include_path=<元のパス>)`（kind=`Batch`・C アナライザと同じ2段解決）。既知のスクリプト拡張子のときだけ成立する。
- `java` 起動行は JVM オプションを読み飛ばし、最初の非オプション引数が FQCN なら `INVOKES(via=call, qualified=True)`。`-jar` は `Dropped("shell_jar")`。
- COBOL/実行ファイルの単独行呼び出し（コマンド全体が大文字英数字のみ）→ `INVOKES(via=call)`（kind=`Module`）。bat の組み込みコマンドは先に除外する。
- `sqlplus @x.sql`／`psql -f`／`mysql <` → `Dropped("shell_sql_script")`。`python`/`perl` → `Dropped("shell_unsupported_runtime")`。`node x.js` → `INVOKES(via=call, include_path=<元のパス>)`。
- 変数参照（`$KEY`・`${KEY...}`・bat の `%KEY%`）→ `ACCESSES(via=config_key, key_kind="env")`。大文字＋アンダースコア（＋数字）のみを設定キーとみなし、`\\$KEY` は拾わない。同一ファイル内で `KEY=` を定義している変数は自己参照なので張らない。

サニタイズ: `#`（POSIX・未引用）／`REM`・`::`（bat・行頭）はコメントとして無視し、シングルクォートの中身は空白化する（ダブルクォートは中身を保持）。ヒアドキュメント本文は読み飛ばし、1件ずつ `Dropped("shell_heredoc")` を申告する。
検出限界: 行継続（バックスラッシュ改行）は結合しない。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult

SHELL_EXT = frozenset({".sh", ".bash", ".ksh", ".zsh"})
BAT_EXT = frozenset({".bat", ".cmd"})
SHELL_BATCH_EXT = SHELL_EXT | BAT_EXT

_VALUE_TRUNCATE = 200

# 既知のスクリプト系拡張子（呼び出し先の1段目判定・大文字小文字は区別しない）。
_SCRIPT_CALL_EXTS = (".sh", ".bash", ".ksh", ".zsh", ".bat", ".cmd")

# 設定キー定義（`KEY=value`）。値は引用符付き文字列か非空白の1トークン（後続コマンド `VAR=1 ./run.sh` と切り分けるため最初の空白で止める）。
_ASSIGN_VALUE = r'(?:"[^"]*"|\S*)'
_POSIX_ASSIGN = re.compile(
    r"^\s*(?:export\s+|readonly\s+|declare\s+-x\s+)?([A-Za-z_][A-Za-z0-9_]*)="
    rf"({_ASSIGN_VALUE})(?:\s+\S.*)?\s*$"
)
# bat の `set KEY=value`／`set "KEY=value"`（外側の引用符は構文として扱い、中身からキー/値を取る）。
_BAT_ASSIGN = re.compile(
    r'^\s*set\s+(?:"([A-Za-z_][A-Za-z0-9_]*)=([^"]*)"|([A-Za-z_][A-Za-z0-9_]*)=(.*))\s*$',
    re.IGNORECASE)
# 値なし宣言（`export KEY` 等）は Config 定義にせず、継承依存の ACCESSES として扱う。
_POSIX_DECL_NO_VALUE = re.compile(r"^\s*(?:export|readonly|declare\s+-x)\s+([A-Za-z_][A-Za-z0-9_]*)\s*$")

# 他スクリプト呼び出し
_POSIX_DOT_SOURCE = re.compile(r"^\s*\.\s+(\S+)")
_POSIX_SOURCE_KW = re.compile(r"^\s*source\s+(\S+)")
_POSIX_INTERP_KW = re.compile(r"^\s*(?:sh|bash|ksh|zsh)\s+(\S+)")
_POSIX_BARE_PATH = re.compile(r"^\s*(\./\S+)")
_BAT_CALL_KW = re.compile(r"^\s*call\s+(\S+)", re.IGNORECASE)
_BAT_BARE_PATH = re.compile(r"^\s*(\S+\.(?:bat|cmd))\b", re.IGNORECASE)

# コマンド位置の分割（未引用の `;`・`|`・`&&`・`||`）／制御キーワード／ラッパー
_SEP = re.compile(r"&&|\|\||;|\|")
_LEADING_CONTROL = re.compile(r"^\s*(?:then|do|else)\b\s*")
_LEADING_BRACE = re.compile(r"^\s*[{(]\s*")
_WRAPPER_KW = re.compile(r"^\s*(?:exec|nohup|time|sudo|env)\s+", re.IGNORECASE)
# ラッパー（特に `env`）の後ろの代入群は Config 化せず読み飛ばし、後続コマンドの開始位置を露出させる。
_ASSIGN_PREFIX = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|\S*)\s*")

# java 呼び出し
_JAVA_LINE = re.compile(r"^\s*java\b")
_JAVA_SHORT_OPT_WITH_ARG = frozenset({"-cp", "-classpath", "-p"})
_FQCN_TOKEN = re.compile(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b")

# SQL スクリプト呼び出し（ノード化しない）
_SQL_AT = re.compile(r"@(\S+\.sql)\b", re.IGNORECASE)
_SQL_DASH_F = re.compile(r"-f\s+(\S+\.sql)\b", re.IGNORECASE)
_SQL_REDIRECT = re.compile(r"<\s*(\S+\.sql)\b", re.IGNORECASE)

# 他ランタイム
_NODE_KW = re.compile(r"^\s*node\s+(\S+\.m?js)\b", re.IGNORECASE)
_PYTHON_KW = re.compile(r"^\s*python[23]?\s+\S", re.IGNORECASE)
_PERL_KW = re.compile(r"^\s*perl\s+\S", re.IGNORECASE)

# 実行ファイルの単独行呼び出し（COBOL 等・行全体が大文字英数字のみ）
_BARE_EXEC = re.compile(r"^(?:\./)?([A-Z][A-Z0-9]*)$")

# bat の組み込みコマンド（大文字だけの単独行でも `Module` の実行ファイル呼び出しにしない）。
_BAT_BUILTINS = frozenset({
    "ECHO", "EXIT", "SET", "GOTO", "PAUSE", "CLS", "TYPE", "COPY", "DEL", "MOVE",
    "MKDIR", "RMDIR", "IF", "FOR", "CALL", "START", "PUSHD", "POPD", "SHIFT", "TITLE", "COLOR", "REM",
})

# 変数参照（大文字＋アンダースコア＋数字のみ＝設定キー扱い・`\$` エスケープは除外）
_VAR_DOLLAR_BRACE = re.compile(r"(?<!\\)\$\{#?([A-Z][A-Z0-9_]*)(?:[:#%][^}]*)?\}")
_VAR_DOLLAR_BARE = re.compile(r"(?<!\\)\$([A-Z][A-Z0-9_]*)\b")
_VAR_PERCENT = re.compile(r"%([A-Z][A-Z0-9_]*)%")

# bat の行頭コメント（`REM`／`::`）
_BAT_REM = re.compile(r"^rem(\s|$)", re.IGNORECASE)

# ヒアドキュメント開始（`<<EOF`／`<<-EOF`／引用符付き終端識別子）
_HEREDOC_START = re.compile(r"<<-?\s*([\'\"]?)(\w+)\1")


def _sanitize_line(line: str, is_bat: bool) -> str:
    """コメント除去＋シングルクォート内を空白化した1行を返す。

    ダブルクォート内は保持する（変数参照は `"…"` 内も拾うため）。POSIX の `#` は未引用のものだけをコメント開始とする（bat の `REM`/`::` は呼び出し側で先に判定する）。
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
    """ヒアドキュメント開始判定用のマスク済み行を返す。未引用の `#` 以降を切り捨て、引用符の中身を空白化する（コメント・文字列内の `<<EOF` を演算子と誤認しないため）。"""
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
    """値の前後を囲む1組の引用符（`'…'`／`"…"`）だけを剥がす。"""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _heredoc_skip_lines(lines: list) -> tuple:
    """ヒアドキュメント本文（開始行の次行〜終端行）の行番号集合と、`Dropped` 候補（開始行ごとに1件）を返す（POSIX のみ。bat では呼ばない）。"""
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
                skip.add(j + 1)  # 終端行自体も本文の走査対象にしない
            i = j + 1
            continue
        i += 1
    return skip, dropped


def _is_comment_line(stripped: str, is_bat: bool) -> bool:
    if is_bat:
        return bool(_BAT_REM.match(stripped)) or stripped.startswith("::")
    return stripped[:1] == "#"


def _scan_assignments(lines: list, is_bat: bool) -> list:
    """`KEY=value`（POSIX の `export`/`readonly`/`declare -x` 込み・bat の `set`）を `[(key, value, line_no), ...]` で返す（`collect_defs` の children 抽出と `extract_refs` の自己参照除外が共有する）。"""
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
    """他スクリプト呼び出しのパス文字列（既知のスクリプト拡張子のときだけ）を返す。"""
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
    """`java` 起動行から呼び出し先（`("class", FQCN)`／`("jar", jar名)`／該当なしは `None`）を返す。

    JVM オプション（`-D…`／`-X…` は単体、`-cp`/`-classpath`/`-p`/`--…` 系は値を伴う）を読み飛ばし、最初の非オプション引数を見る。class mode では `.` を含む識別子（FQCN）だけを返す。
    """
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
    """未引用の `;`・`|`・`&&`・`||` でコマンド位置を分割する（二重引用符内は分割しない。シングルクォートは `_sanitize_line` で空白化済み）。"""
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
    """コマンド開始位置の制御キーワード（`then`/`do`/`else`・`{`/`(`）とラッパー（`exec`/`nohup`/`time`/`sudo`/`env`＋代入群）を除去し、実行コマンドの開始位置だけを残す。"""
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
    """1コマンド位置の構造マッチ結果を1つ返す（優先順位はこの判定順・該当なしは `None`）。"""
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
    """シェル／バッチ → `Batch`（primary）。キー代入 → `Config` children（`key_kind="env"`）。他スクリプト呼び出し・`java`/`node` 起動・実行ファイル単独行呼び出し・変数参照を参照候補として返す。"""

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
            if key in seen:  # 同一ファイル内の重複キーは最初の1つ
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
                if var_name in local_keys:  # 自己参照（同一ファイル内で定義済み）は張らない
                    continue
                refs.append(RefCandidate("ACCESSES", "Config", var_name, line_no,
                                         extra={"via": "config_key", "key_kind": "env"}))

        return RefResult(refs=refs, dropped=dropped)
