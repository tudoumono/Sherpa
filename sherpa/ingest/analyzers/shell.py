"""シェル／バッチアナライザ。ファイル自体を主体定義（`Batch`・`extra["batch_kind"]` は `"shell"`/`"bat"`）とし、設定キーを `Config` children、他スクリプト・プログラム呼び出しと環境変数の参照を参照候補として返す。

`.sh`/`.bash`/`.ksh`/`.zsh`（POSIX）と `.bat`/`.cmd`（Windows バッチ）を全件受理し、拡張子で書式を分岐する（`_is_bat()`）。関数・bat ラベルは children にしない。

設定キー children: POSIX の `export KEY=value`／`KEY=value`（コマンドの前置きの `KEY=value cmd` 含む・`readonly`/`declare -x` 含む）、bat の `set KEY=value` → `DefItem(label="Config", name=<裸キー>, cid_key="key:env:"+<裸キー>, extra={"config_value", "key_kind": "env"})`。同一ファイル内の重複キーは最初の1つ。値なし宣言（`export KEY` 等）は定義にせず `ACCESSES(via=config_key, key_kind="env")` を返す。`env KEY=value cmd` の一時代入は定義にしない。

参照（`extract_refs`）:
- コマンドごとに（`;`・`|`・`&&`・`||`・`then`/`do`/`else`・`{`/`(`・`$(…)` の中も）、先頭の `exec`/`nohup`/`time`/`sudo`/`env` と `KEY=value` を読み飛ばして分類する。
- 他スクリプト呼び出し（`./x.sh`・`sh x.sh`・`. x.sh`・`source x.sh`・bat の `call x.bat`）→ `INVOKES(via=include, include_path=<元のパス>)`（kind=`Batch`・C アナライザと同じ2段解決）。既知のスクリプト拡張子のときだけ成立する。
- `java` 起動行は JVM オプションを読み飛ばし、最初の非オプション引数が FQCN なら `INVOKES(via=call, qualified=True)`。`-jar` は `Dropped("shell_jar")`。
- COBOL/実行ファイルの単独行呼び出し（コマンド全体が大文字英数字のみ）→ `INVOKES(via=call)`（kind=`Module`）。bat の組み込みコマンドは先に除外する。
- `sqlplus @x.sql`／`psql -f`／`mysql <` → `Dropped("shell_sql_script")`。`python`/`perl` → `Dropped("shell_unsupported_runtime")`。`node x.js` → `INVOKES(via=call, include_path=<元のパス>)`。
- 変数参照（`$KEY`・`${KEY...}`・bat の `%KEY%`）→ `ACCESSES(via=config_key, key_kind="env")`。大文字＋アンダースコア（＋数字）のみを設定キーとみなし、`\\$KEY` は拾わない。ヒアドキュメント本文の中は読まない。同一ファイル内で `KEY=` を定義している変数は自己参照なので張らない。

読み取り: POSIX シェルは Tree-sitter（tree-sitter-bash）の木の上で行う。コメントはコメントノード、シングルクォートの中身は文字列ノードなので読まない。ヒアドキュメントだけは、tree-sitter-bash が `<<A <<B`（1 行に 2 つ）や `<<EOF; cmd`（終端語の直後の `;`）を誤読して本文がファイル末まで広がるため、木にかける前に本文と開始記号を空白へ置き換える（行番号は変えない）。本文は読み飛ばし、終端語ごとに `Dropped("shell_heredoc")` を申告する（終端語が引用符つき `<<'EOF'`・`<<"END"` の形・`<<-`・ヒア文字列 `<<<`・算術式 `$((1<<2))` の扱いは従来どおり）。構文エラーの領域は `Dropped("syntax_error")` で申告し、エラーの外は読み続ける。
bat／cmd は Tree-sitter の文法が無いため従来の行ごとの読み取り（`REM`／`::` コメント・`set`・`call`・`%VAR%`）のまま。
設計: docs/design/rag.md「グラフ」
"""
from __future__ import annotations

import re
from pathlib import PurePosixPath

from . import _ts
from ._base import Analyzer, DefItem, DefResult, Dropped, RefCandidate, RefResult


SHELL_EXT = frozenset({".sh", ".bash", ".ksh", ".zsh"})
BAT_EXT = frozenset({".bat", ".cmd"})
SHELL_BATCH_EXT = SHELL_EXT | BAT_EXT

_VALUE_TRUNCATE = 200

# 既知のスクリプト系拡張子（呼び出し先の1段目判定・大文字小文字は区別しない）。
_SCRIPT_CALL_EXTS = (".sh", ".bash", ".ksh", ".zsh", ".bat", ".cmd")

# bat の `set KEY=value`／`set "KEY=value"`（外側の引用符は構文として扱い、中身からキー/値を取る）。
_BAT_ASSIGN = re.compile(
    r'^\s*set\s+(?:"([A-Za-z_][A-Za-z0-9_]*)=([^"]*)"|([A-Za-z_][A-Za-z0-9_]*)=(.*))\s*$',
    re.IGNORECASE)

# bat の他スクリプト呼び出し
_BAT_CALL_KW = re.compile(r"^\s*call\s+(\S+)", re.IGNORECASE)
_BAT_BARE_PATH = re.compile(r"^\s*(\S+\.(?:bat|cmd))\b", re.IGNORECASE)

# bat のコマンド位置の分割（未引用の `;`・`|`・`&&`・`||`）／制御キーワード／ラッパー
_SEP = re.compile(r"&&|\|\||;|\|")
_LEADING_CONTROL = re.compile(r"^\s*(?:then|do|else)\b\s*")
_LEADING_BRACE = re.compile(r"^\s*[{(]\s*")
_WRAPPER_KW = re.compile(r"^\s*(?:exec|nohup|time|sudo|env)\s+", re.IGNORECASE)
_ASSIGN_PREFIX = re.compile(r"^\s*[A-Za-z_][A-Za-z0-9_]*=(?:\"[^\"]*\"|\S*)\s*")

# java 呼び出し
_JAVA_LINE = re.compile(r"^\s*java\b")
_JAVA_SHORT_OPT_WITH_ARG = frozenset({"-cp", "-classpath", "-p"})
_FQCN_TOKEN = re.compile(r"\b[A-Za-z_$][\w$]*(?:\.[A-Za-z_$][\w$]*)+\b")

# SQL スクリプト呼び出し（ノード化しない）
_SQL_AT = re.compile(r"@(\S+\.sql)\b", re.IGNORECASE)
_SQL_DASH_F = re.compile(r"-f\s+(\S+\.sql)\b", re.IGNORECASE)
_SQL_REDIRECT = re.compile(r"<\s*(\S+\.sql)\b", re.IGNORECASE)
_SQL_FILE = re.compile(r"\S+\.sql", re.IGNORECASE)

# 他ランタイム
_NODE_KW = re.compile(r"^\s*node\s+(\S+\.m?js)\b", re.IGNORECASE)
_PYTHON_KW = re.compile(r"^\s*python[23]?\s+\S", re.IGNORECASE)
_PERL_KW = re.compile(r"^\s*perl\s+\S", re.IGNORECASE)
_PYTHON_WORD = re.compile(r"python[23]?", re.IGNORECASE)

# 実行ファイルの単独行呼び出し（COBOL 等・行全体が大文字英数字のみ）
_BARE_EXEC = re.compile(r"^(?:\./)?([A-Z][A-Z0-9]*)$")

# bat の組み込みコマンド（大文字だけの単独行でも `Module` の実行ファイル呼び出しにしない）。
_BAT_BUILTINS = frozenset({
    "ECHO", "EXIT", "SET", "GOTO", "PAUSE", "CLS", "TYPE", "COPY", "DEL", "MOVE",
    "MKDIR", "RMDIR", "IF", "FOR", "CALL", "START", "PUSHD", "POPD", "SHIFT", "TITLE", "COLOR", "REM",
})

# 変数参照（大文字＋アンダースコア＋数字のみ＝設定キー扱い）
_VAR_NAME = re.compile(r"[A-Z][A-Z0-9_]*")
_VAR_PERCENT = re.compile(r"%([A-Z][A-Z0-9_]*)%")

# bat の行頭コメント（`REM`／`::`）
_BAT_REM = re.compile(r"^rem(\s|$)", re.IGNORECASE)

# ヒアドキュメント開始（`<<EOF`／`<<-EOF`／引用符付き終端識別子）
_HEREDOC_START = re.compile(r"""<<(-?)[ \t]*(?:'([^']+)'|"([^"]+)"|([^\s'"<>;|&()\\]+))""")

_WRAPPERS = frozenset({"exec", "nohup", "time", "sudo", "env"})
_ASSIGN_WORD = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=")


# --- bat（行ごとの読み取り）---------------------------------------------------------------

def _sanitize_line(line: str) -> str:
    """bat の 1 行からシングルクォート内を空白化した行を返す（ダブルクォート内は保持する）。"""
    out: list = []
    in_s = in_d = False
    for ch in line:
        if ch == "'" and not in_d:
            in_s = not in_s
            out.append(" ")
            continue
        if ch == '"' and not in_s:
            in_d = not in_d
            out.append(ch)
            continue
        out.append(" " if in_s else ch)
    return "".join(out)


def _is_bat_comment_line(stripped: str) -> bool:
    return bool(_BAT_REM.match(stripped)) or stripped.startswith("::")


def _scan_bat_assignments(lines: list) -> list:
    """bat の `set KEY=value` を `[(key, value, line_no), ...]` で返す。"""
    out: list = []
    for line_no, raw in enumerate(lines, 1):
        if _is_bat_comment_line(raw.lstrip()):
            continue
        m = _BAT_ASSIGN.match(_sanitize_line(raw))
        if m:
            key = m.group(1) if m.group(1) is not None else m.group(3)
            value = m.group(2) if m.group(1) is not None else m.group(4)
            out.append((key, value, line_no))
    return out


def _unquote(value: str) -> str:
    """値の前後を囲む1組の引用符（`'…'`／`"…"`）だけを剥がす。"""
    v = value.strip()
    if len(v) >= 2 and v[0] == v[-1] and v[0] in ("'", '"'):
        return v[1:-1]
    return v


def _normalize_sep(path: str) -> str:
    return path.replace("\\", "/")


def _match_bat_script_call(line: str) -> str | None:
    """bat の他スクリプト呼び出しのパス文字列（既知のスクリプト拡張子のときだけ）を返す。"""
    for pat in (_BAT_CALL_KW, _BAT_BARE_PATH):
        m = pat.match(line)
        if m and m.group(1).lower().endswith(_SCRIPT_CALL_EXTS):
            return m.group(1)
    return None


def _java_target(tokens: list):
    """`java` の引数列（`java` の次から）から呼び出し先（`("class", FQCN)`／`("jar", jar名)`／該当なしは `None`）を返す。

    JVM オプション（`-D…`／`-X…` は単体、`-cp`/`-classpath`/`-p`/`--…` 系は値を伴う）を読み飛ばし、最初の非オプション引数を見る。class mode では `.` を含む識別子（FQCN）だけを返す。
    """
    i, n = 0, len(tokens)
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


def _match_java(line: str):
    if not _JAVA_LINE.match(line):
        return None
    return _java_target(line.split()[1:])


def _match_sql_script(line: str) -> str | None:
    """bat の `sqlplus`/`psql`/`mysql` の SQL スクリプト実行行からパスを返す（該当なしは `None`）。"""
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
    """bat の `node`/`python`/`perl` 起動行を判定する（`("node", パス)`／`("python"|"perl", None)`／`None`）。"""
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


def _classify_bat_line(line: str):
    """bat の 1コマンド位置の構造マッチ結果を1つ返す（優先順位はこの判定順・該当なしは `None`）。"""
    target = _match_bat_script_call(line)
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
    if bare and bare.group(1) not in _BAT_BUILTINS:
        return "bare_exec", bare.group(1)
    return None


# --- POSIX シェル（Tree-sitter の木の上の読み取り）---------------------------------------------

def _heredoc_delims(line: str, state: dict) -> list:
    """1 行の中のヒアドキュメント開始を `(終端語, `<<-` か, 開始位置, 終了位置)` の列で出現順に返す（`<<EOF`・`<<-EOF`・`<<'EOF'`・`<<"END"`）。

    引用符・未引用の `#` 以降のコメント・算術式 `$((…))`/`((…))` の中の `<<`、ヒア文字列 `<<<` は開始としない。引用形（`<<'END)'`）は引用符の中を終端語とし、引用なしは空白・メタ文字以外の連続（`END-OF-FILE` など）を終端語とする。`#` は語頭のものだけがコメント。`${…}`・バックスラッシュでエスケープした文字は読み飛ばす。引用符と括弧の状態は `state`（行をまたいで持ち回る）に持つので、複数行の引用符の中の `<<` は開始としない。
    """
    out: list = []
    stack: list = state["stack"]  # 開き括弧の種別: "A"＝算術 `((`、"P"＝通常
    in_s, in_d = state["s"], state["d"]
    i, n = 0, len(line)
    while i < n:
        ch = line[i]
        if in_s:
            in_s = ch != "'"
        elif in_d:
            if ch == "\\":
                i += 1
            elif ch == '"':
                in_d = False
        elif ch == "\\":
            i += 1  # エスケープされた 1 文字（`\'`・`\"`・`\#` など）は構文として読まない
        elif ch == "$" and line[i + 1:i + 2] == "{":
            end = line.find("}", i + 2)  # パラメータ展開 `${x#foo}` の中は読まない
            i = n if end < 0 else end
        elif ch == "#" and (i == 0 or line[i - 1] in " \t;|&("):
            break
        elif ch == "'":
            in_s = True
        elif ch == '"':
            in_d = True
        elif ch == "(":
            if line[i + 1:i + 2] == "(":
                stack.append("A")
                i += 1
            else:
                stack.append("P")
        elif ch == ")":
            if stack:
                if stack[-1] == "A" and line[i + 1:i + 2] == ")":
                    i += 1
                stack.pop()
        elif ch == "<" and line[i:i + 2] == "<<" and "A" not in stack:
            if line[i + 2:i + 3] == "<":  # ヒア文字列
                i += 3
                continue
            m = _HEREDOC_START.match(line, i)
            if m:
                out.append((m.group(2) or m.group(3) or m.group(4), m.group(1) == "-", m.start(), m.end()))
                i = m.end()
                continue
            i += 2
            continue
        i += 1
    state["s"], state["d"] = in_s, in_d
    return out


def _blank(s: str) -> str:
    """UTF-8 のバイト数を保ったまま空白にする（木の位置で元のテキストを引けるように）。"""
    return " " * len(s.encode("utf-8"))


def _continues(line: str) -> bool:
    """行末が（エスケープされていない）バックスラッシュか。"""
    body = line.rstrip("\r")
    return (len(body) - len(body.rstrip("\\"))) % 2 == 1


def _mask_heredocs(text: str) -> tuple:
    """ヒアドキュメントの開始記号（`<<-'EOF'` など）と本文（終端行まで）を空白へ置き換えたテキストと、`Dropped("shell_heredoc")`（終端語ごとに 1 件）を返す。行数・行番号・バイト位置は変えない。"""
    lines = text.split("\n")
    n = len(lines)
    dropped: list = []
    state: dict = {"s": False, "d": False, "stack": []}
    i = 0
    while i < n:
        delims = _heredoc_delims(lines[i].rstrip("\r"), state)
        if not delims:
            i += 1
            continue
        line = lines[i]
        for _d, _t, start, end in reversed(delims):
            line = line[:start] + _blank(line[start:end]) + line[end:]
        lines[i] = line
        j = i + 1
        while j < n and _continues(lines[j - 1]):  # 行継続（`\\` で終わる行）の続きは本文でなくコマンドの一部
            j += 1
        for delim, strip_tabs, _s, _e in delims:  # 1 行に複数あれば本文は順に続く
            dropped.append(Dropped("shell_heredoc", i + 1, delim))
            # 終端行は終端語と完全一致。`<<-` だけは行頭のタブを許す。
            while j < n:
                cur = lines[j].rstrip("\r")
                if (cur.lstrip("\t") if strip_tabs else cur) == delim:
                    break
                lines[j] = _blank(lines[j])
                j += 1
            if j < n:
                lines[j] = _blank(lines[j])  # 終端行自体も読まない
                j += 1
        i = j
    return "\n".join(lines), dropped


def _word_text(parsed: _ts.Parsed, node) -> str:
    """単語ノードの値。静的な引用符つきの文字列は引用符の内側、それ以外（展開を含むものなど）はソースのまま。"""
    if node.type == "raw_string":
        return parsed.text(node)[1:-1]
    if node.type == "string" and all(c.type in ('"', "string_content") for c in node.children):
        return parsed.text(node)[1:-1]
    return parsed.text(node)


def _command_words(parsed: _ts.Parsed, node) -> list:
    """`command` ノードの前置きの代入を除いた語（コマンド名・引数）の列。"""
    words: list = []
    for c in node.children:
        if c.type == "command_name":
            inner = c.named_children[0] if c.named_children else c
            words.append(_word_text(parsed, inner))
        elif c.type not in ("variable_assignment", "comment", "file_redirect", "heredoc_redirect"):
            words.append(_word_text(parsed, c))
    return words


def _redirect_inputs(parsed: _ts.Parsed, node) -> list:
    """`command` を包む `redirected_statement` の `< file` の入力先。"""
    parent = node.parent
    if parent is None or parent.type != "redirected_statement":
        return []
    out: list = []
    for c in parent.children:
        if c.type == "file_redirect":
            kids = c.children
            if kids and kids[0].type == "<" and len(kids) >= 2:
                out.append(_word_text(parsed, kids[-1]))
    return out


def _classify_words(words: list, inputs: list):
    """1 コマンドの語（ラッパー除去済み）の構造マッチ結果を1つ返す（優先順位はこの判定順・該当なしは `None`）。"""
    w0 = words[0]
    target = None
    if w0 in (".", "source") and len(words) > 1:
        target = words[1]
    elif w0 in ("sh", "bash", "ksh", "zsh") and len(words) > 1:
        target = words[1]
    elif w0.startswith("./"):
        target = w0
    if target is not None and target.lower().endswith(_SCRIPT_CALL_EXTS):
        return "script_call", target
    if w0 == "java":
        java = _java_target(words[1:])
        if java is not None:
            return "java", java
    low = w0.lower()
    sql = None
    if low == "sqlplus":
        sql = next((m.group(1) for m in (_SQL_AT.search(w) for w in words[1:]) if m), None)
    elif low == "psql":
        sql = next((words[i + 1] for i, w in enumerate(words[1:], 1)
                    if w == "-f" and i + 1 < len(words) and _SQL_FILE.fullmatch(words[i + 1])), None)
    elif low == "mysql":
        sql = next((p for p in inputs if _SQL_FILE.fullmatch(p)), None)
    if sql is not None:
        return "sql", sql
    if low == "node" and len(words) > 1 and words[1].lower().endswith((".js", ".mjs")):
        return "runtime", ("node", words[1])
    if _PYTHON_WORD.fullmatch(w0) and len(words) > 1:
        return "runtime", ("python", None)
    if low == "perl" and len(words) > 1:
        return "runtime", ("perl", None)
    if len(words) == 1:
        bare = _BARE_EXEC.fullmatch(w0)
        if bare:
            return "bare_exec", bare.group(1)
    return None


def _strip_wrappers(words: list) -> list:
    """先頭のラッパー（`exec`/`nohup`/`time`/`sudo`/`env`）と `KEY=value` を読み飛ばす。"""
    i = 0
    while i < len(words):
        w = words[i]
        if w.lower() in _WRAPPERS or _ASSIGN_WORD.match(w):
            i += 1
            continue
        break
    return words[i:]


def _is_exported_decl(parsed: _ts.Parsed, node) -> bool:
    """`export`／`readonly`／`declare -x` の宣言か（`local`・`typeset`・`-x` なしの `declare` は除く）。"""
    first = node.children[0]
    kw = parsed.text(first)
    if kw in ("export", "readonly"):
        return True
    return kw == "declare" and any(c.type == "word" and parsed.text(c) == "-x" for c in node.children)


def _shell_assignments(parsed: _ts.Parsed, original: bytes) -> list:
    """`KEY=value`（`export`/`readonly`/`declare -x` 込み・コマンドの前置き込み）を文書順に `[(key, value, line_no), ...]` で返す。値は（ヒアドキュメントを空白にする前の）元のテキストから取る。"""
    out: list = []
    stack = [parsed.root]
    while stack:
        node = stack.pop()
        t = node.type
        if t == "variable_assignment":
            parent = node.parent
            if (parent is not None and parent.type == "declaration_command"
                    and not _is_exported_decl(parsed, parent)):
                continue
            if any(c.type == "+=" for c in node.children):
                continue
            name = node.child_by_field_name("name")
            if name is not None and name.type == "subscript":  # `ARR[key]=v` は変数 `ARR` の定義
                name = name.child_by_field_name("name") or name
            value = node.child_by_field_name("value")
            out.append((parsed.text(name) if name is not None else "",
                        original[value.start_byte:value.end_byte].decode("utf-8", errors="replace")
                        if value is not None else "",
                        _ts.start_line(node)))
        elif t in ("heredoc_body", "comment"):
            continue
        stack.extend(reversed(node.children))
    return [a for a in out if a[0]]


class ShellBatchAnalyzer(Analyzer):
    """シェル／バッチ → `Batch`（primary）。キー代入 → `Config` children（`key_kind="env"`）。他スクリプト呼び出し・`java`/`node` 起動・実行ファイル単独行呼び出し・変数参照を参照候補として返す。"""

    name = "shell"
    extensions = SHELL_BATCH_EXT
    doctype = "shell"
    version = 3

    @staticmethod
    def _is_bat(rel_path: str) -> bool:
        return PurePosixPath(rel_path).suffix.lower() in BAT_EXT

    def collect_defs(self, text: str, rel_path: str) -> DefResult:
        is_bat = self._is_bat(rel_path)
        primary = DefItem(label="Batch", name=PurePosixPath(rel_path).name,
                          extra={"batch_kind": "bat" if is_bat else "shell"})
        if is_bat:
            assignments = _scan_bat_assignments(text.splitlines())
        else:
            assignments = _shell_assignments(_ts.parse("bash", _mask_heredocs(text)[0]), text.encode("utf-8"))
        children: list = []
        seen: set = set()
        for key, value, line_no in assignments:
            if key in seen:  # 同一ファイル内の重複キーは最初の1つ
                continue
            seen.add(key)
            children.append(DefItem(label="Config", name=key, line=line_no,
                                    cid_key=f"key:env:{key}",
                                    extra={"config_value": _unquote(value)[:_VALUE_TRUNCATE],
                                          "key_kind": "env"}))
        return DefResult(primary=primary, children=children)

    def extract_refs(self, text: str, rel_path: str) -> RefResult:
        if self._is_bat(rel_path):
            return self._extract_bat_refs(text)
        return self._extract_shell_refs(text)

    @staticmethod
    def _command_ref(kind: str, payload, line_no: int, refs: list, dropped: list) -> None:
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

    def _extract_shell_refs(self, text: str) -> RefResult:
        masked, heredoc_dropped = _mask_heredocs(text)
        parsed = _ts.parse("bash", masked)
        local_keys = {k for k, _v, _ln in _shell_assignments(parsed, parsed.src)}

        keyed: list = []  # (行, 同じ行の中の順（宣言→コマンド→変数）, RefCandidate)
        dropped: list = list(heredoc_dropped)
        cmd_dropped: list = []
        stack = [parsed.root]
        while stack:
            node = stack.pop()
            t = node.type
            if t in ("comment", "heredoc_body"):
                continue
            line = _ts.start_line(node)
            if t == "heredoc_redirect":  # 開始記号の読み取りで漏れたもの（`"$(… <<EOF …)"` の中など）は木が見つける
                start = next((c for c in node.children if c.type == "heredoc_start"), None)
                if start is not None:
                    dropped.append(Dropped("shell_heredoc", line, parsed.text(start).strip("'\"\\")))
            if t == "declaration_command" and _is_exported_decl(parsed, node):
                for c in node.children:
                    if c.type == "variable_name":
                        keyed.append((line, 0, RefCandidate(
                            "ACCESSES", "Config", parsed.text(c), line,
                            extra={"via": "config_key", "key_kind": "env"})))
            elif t == "command":
                words = _strip_wrappers(_command_words(parsed, node))
                if words:
                    classified = _classify_words(words, _redirect_inputs(parsed, node))
                    if classified is not None:
                        tmp_refs: list = []
                        self._command_ref(classified[0], classified[1], line, tmp_refs, cmd_dropped)
                        keyed.extend((line, 1, r) for r in tmp_refs)
            elif t in ("simple_expansion", "expansion"):
                var = next((c for c in node.children if c.type == "variable_name"), None)
                if var is not None:
                    name = parsed.text(var)
                    if _VAR_NAME.fullmatch(name) and name not in local_keys:
                        keyed.append((line, 2, RefCandidate(
                            "ACCESSES", "Config", name, line,
                            extra={"via": "config_key", "key_kind": "env"})))
            stack.extend(reversed(node.children))

        keyed.sort(key=lambda k: (k[0], k[1]))
        dropped.extend(sorted(cmd_dropped, key=lambda d: d.line))
        dropped.extend(_ts.syntax_errors(parsed))
        return RefResult(refs=[r for _l, _b, r in keyed], dropped=dropped)

    def _extract_bat_refs(self, text: str) -> RefResult:
        lines = text.splitlines()
        local_keys = {k for k, _v, _ln in _scan_bat_assignments(lines)}
        refs: list = []
        dropped: list = []
        for line_no, raw in enumerate(lines, 1):
            if _is_bat_comment_line(raw.lstrip()):
                continue
            line = _sanitize_line(raw)
            for segment in _split_top_level(line):
                classified = _classify_bat_line(_strip_command_prefix(segment))
                if classified is not None:
                    self._command_ref(classified[0], classified[1], line_no, refs, dropped)
            for m in _VAR_PERCENT.finditer(line):
                if m.group(1) in local_keys:  # 自己参照（同一ファイル内で定義済み）は張らない
                    continue
                refs.append(RefCandidate("ACCESSES", "Config", m.group(1), line_no,
                                         extra={"via": "config_key", "key_kind": "env"}))
        return RefResult(refs=refs, dropped=dropped)
