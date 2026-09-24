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

READ_CMDS = {"cat", "less", "more", "head", "tail", "bat", "awk", "source"}
# `.env` を引数に取っても中身を端末に表示しない操作（複製/移動/一覧/存在確認/件数・部分表示）。
# 下記 `segment_mentions_env` の保険判定の対象から外す。cut は含めない——`-d= -f2-` や
# `-c1-200` は中身を丸ごと出せるため、先頭の短い範囲（`-c1-8` 以内）だけを `_cut_is_prefix_only`
# で個別に許可する（CLAUDE.md が許可する「接頭辞の確認」の範囲）。
NONDISPLAY_CMDS = {"cp", "mv", "ls", "test", "wc"}
_CUT_PREFIX_MAX = 8
_CUT_RANGE_RE = re.compile(r"^-[cb](?:1-)?([0-9]+)$")
VAR_KEYWORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "DSN", "CREDENTIAL")
VAR_RE = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# `${VAR:0:N}`（部分展開・offset は 0 固定＝接頭辞・N は _CUT_PREFIX_MAX 以内。offset を許すと
# 8 文字ずつ連結して全体を出せる）・`${#VAR}`（長さ）は
# CLAUDE.md が許可する長さ/接頭辞確認そのもの。変数名走査（VAR_RE）の前にこの形だけをマスクして
# 無害化する（N が大きい部分展開は鍵全体を出せるためマスクしない＝拒否側に落ちる）。
SAFE_EXPANSION_RE = re.compile(
    r"\$\{(?:#[A-Za-z_][A-Za-z0-9_]*|[A-Za-z_][A-Za-z0-9_]*:0:(?P<n>[0-9]+))\}"
)
# 複製/移動先が端末や既存の fd なら「表示しない操作」ではない（NONDISPLAY_CMDS の除外を無効にする）。
_DEV_OUTPUT_RE = re.compile(r"^(?:/dev/(?:stdout|stderr|tty|fd/)|/proc/(?:self|[0-9]+)/fd/)")


def _mask_safe_expansions(segment: str) -> str:
    def _repl(m):
        n = m.group("n")
        return "" if (n is None or int(n) <= _CUT_PREFIX_MAX) else m.group(0)
    return SAFE_EXPANSION_RE.sub(_repl, segment)
_SHORT_FLAGS_RE = re.compile(r"^-[A-Za-z]+$")
_SED_INPLACE_RE = re.compile(r"^-[A-Za-z]*i[A-Za-z0-9.]*$")


def is_env_path(token: str) -> bool:
    t = token.strip("'\"")
    base = t.rsplit("/", 1)[-1]
    if base == ".env":
        return True
    if base.startswith(".env.") and not base.startswith(".env.example"):
        return True
    return False


def _cut_is_prefix_only(rest) -> bool:
    """`cut -c1-N`／`-cN`（N<=_CUT_PREFIX_MAX・`-b` も同様）だけを接頭辞確認として許す。
    `-d`/`-f`（フィールド抽出）や広い範囲は中身の表示になるため許さない。"""
    ranges = [t for t in rest if t.startswith("-")]
    if not ranges:
        return False
    for t in ranges:
        m = _CUT_RANGE_RE.match(t)
        if not m or int(m.group(1)) > _CUT_PREFIX_MAX:
            return False
    return True


# 先行文字にリダイレクト（< >）とバッククォートも含める＝`cat <.env`／`$(<.env)` の形も言及として拾う。
_ENV_MENTION_RE = re.compile(r'(?:^|[\s"\x27/=(<>`])\.env(?![A-Za-z0-9_-])')


def segment_mentions_env(segment: str) -> bool:
    """セグメント全体（`$(...)` 展開・python 文字列内の参照・glob 展開まで含む）に
    `.env`/`.env.*`（`.env.example` を除く）への言及があるかを、コマンドの区切りに依らず
    粗く走査する。先頭トークンだけを見る判定（READ_CMDS 等）を素通りする経路の保険。"""
    # パス境界付きで照合する（`azure.env` のような別名の env ファイルや `foo.envelope` は対象外）。
    for m in _ENV_MENTION_RE.finditer(segment):
        tail = segment[m.end():]
        if tail.startswith(".example"):
            continue
        return True
    return False


def _grep_is_count_only(rest: list) -> bool:
    # `-c`／`-ic`／`-ci` 等、結合フラグの束に `c`（件数表示）が含まれれば件数表示扱い。
    return any(_SHORT_FLAGS_RE.match(t) and "c" in t[1:] for t in rest)


def _sed_is_in_place(rest: list) -> bool:
    # `-i`／`-ni`／`-i.bak`／`--in-place[=SUFFIX]` は書き込みであり表示ではない。
    for t in rest:
        if t == "--in-place" or t.startswith("--in-place="):
            return True
        if _SED_INPLACE_RE.match(t):
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

    # 引数にコマンド置換（`$(...)`／バッククォート）を含むセグメントは、allowlist のコマンドでも
    # 早期 return しない＝置換の中身（`$(cat …)` 等）を末尾の保険判定に必ず通す。
    has_subst = "$(" in segment or "`" in segment

    if (cmd in NONDISPLAY_CMDS and not has_subst
            and not any(_DEV_OUTPUT_RE.match(t.strip("'\"")) for t in rest)):
        return None

    if cmd == "cut" and not has_subst and _cut_is_prefix_only(rest):
        return None

    if cmd == "grep":
        if _grep_is_count_only(rest) and not has_subst:
            return None
        if any(is_env_path(t) for t in rest):
            return ".env 系ファイルを grep で表示しようとしている"
        # 件数表示以外は早期 return しない（リダイレクト直後 `grep KEY <.env` やグロブ
        # `grep KEY .env*` は先頭引数の完全一致では拾えないため、下の保険判定へ必ず進める）。

    if cmd == "sed":
        if _sed_is_in_place(rest) and not has_subst:
            return None
        if any(is_env_path(t) for t in rest):
            return "sed で .env 系ファイルの中身を端末に出そうとしている"
    elif cmd in READ_CMDS or cmd == ".":
        if any(is_env_path(t) for t in rest):
            return "%s で .env 系ファイルの中身を端末に出そうとしている" % cmd
    elif cmd in ("echo", "printf"):
        masked = _mask_safe_expansions(segment)
        for name in VAR_RE.findall(masked):
            upper = name.upper()
            if any(k in upper for k in VAR_KEYWORDS):
                return "%s が機密名を含む変数 ${%s} を展開しようとしている" % (cmd, name)

    # 保険判定: 上記の個別判定を素通りしても、セグメント全体に .env への言及が
    # あれば拒否する（$(...) 展開・timeout 等のラッパー・python -c 文字列内の参照まで拾う）。
    if segment_mentions_env(segment):
        return ".env 系ファイルの中身を端末に出す可能性がある記述を検知した"

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
