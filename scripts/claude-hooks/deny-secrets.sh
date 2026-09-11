#!/usr/bin/env bash
# Claude Code PreToolUse フック（matcher: Bash）。鍵・トークン・.env の内容を端末に出すコマンドを
# 機械的に拒否する（exit 2・stderr に理由）。該当しなければ exit 0。
# 判定本体は python（.venv があればそれ、無ければ python3）に委ねる。stdin はフックへの JSON
# （tool_input.command）を保持したまま渡す必要があるため、python コードは `-c` 引数で渡し、
# ヒアドキュメントを python の stdin に食わせない。
set -u

ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
PY="$ROOT/.venv/bin/python"
if [ ! -x "$PY" ]; then
    PY="python3"
fi

CODE=$(cat <<'PYEOF'
import json
import re
import sys

READ_CMDS = {"cat", "less", "more", "head", "tail", "bat", "sed", "awk", "cp", "source"}
VAR_KEYWORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "DSN", "CREDENTIAL")
VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")


def is_env_path(token: str) -> bool:
    t = token.strip("'\"")
    base = t.rsplit("/", 1)[-1]
    if base == ".env":
        return True
    if base.startswith(".env.") and not base.startswith(".env.example"):
        return True
    return False


def split_segments(command: str) -> list:
    segments = []
    buf = []
    quote = None
    i = 0
    n = len(command)
    while i < n:
        c = command[i]
        if quote:
            buf.append(c)
            if c == quote:
                quote = None
            i += 1
            continue
        if c in ("'", '"'):
            quote = c
            buf.append(c)
            i += 1
            continue
        if command[i:i + 2] in ("&&", "||"):
            segments.append("".join(buf))
            buf = []
            i += 2
            continue
        if c in (";", "|", "&", "\n"):
            segments.append("".join(buf))
            buf = []
            i += 1
            continue
        buf.append(c)
        i += 1
    segments.append("".join(buf))
    return [s.strip() for s in segments if s.strip()]


def check_segment(segment: str):
    try:
        import shlex
        tokens = shlex.split(segment)
    except ValueError:
        tokens = segment.split()
    if not tokens:
        return None
    cmd = tokens[0].rsplit("/", 1)[-1]
    rest = tokens[1:]

    if cmd == "printenv":
        return "printenv は環境変数を丸ごと端末に出す"

    if cmd == "env" and not rest:
        return "引数無し env は環境変数を丸ごと端末に出す"

    if cmd == "grep":
        if "-c" in rest:
            return None
        if any(is_env_path(t) for t in rest):
            return ".env 系ファイルを grep で表示しようとしている"
        return None

    if cmd in READ_CMDS or cmd == ".":
        if any(is_env_path(t) for t in rest):
            return "%s で .env 系ファイルの中身を端末に出そうとしている" % cmd
        return None

    if cmd in ("echo", "printf"):
        for name in VAR_RE.findall(segment):
            upper = name.upper()
            if any(k in upper for k in VAR_KEYWORDS):
                return "%s が機密名を含む変数 ${%s} を展開しようとしている" % (cmd, name)
        return None

    return None


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except Exception:
        return 0
    command = payload.get("tool_input", {}).get("command")
    if not isinstance(command, str) or not command.strip():
        return 0
    for segment in split_segments(command):
        reason = check_segment(segment)
        if reason:
            sys.stderr.write(
                "[deny-secrets] 拒否: " + reason + "\n"
                "代替: 長さ/接頭辞での確認（例: cut -c1-4）や、実呼び出しのステータスコードのみで確認してください。\n"
            )
            return 2
    return 0


sys.exit(main())
PYEOF
)

exec "$PY" -c "$CODE"
