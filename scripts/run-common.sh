#!/usr/bin/env bash
# start.sh / stop.sh / status.sh が共有する定数とヘルパ。
# これは source 専用（直接実行しない）。set -e 等は呼び出し側で設定する。

# リポジトリルート（このファイルの1つ上）。呼び出し側が cd 済みでも安全なように絶対解決する。
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

# 閉域キット導入環境向け（M4・scripts/install_offline_kit.sh が tools/node/ へ Node.js を展開する）:
# tools/node/bin が存在すれば PATH の先頭へ足す。marp CLI は node スクリプト（#!/usr/bin/env node）
# のため、絶対パスで起動しても実行時に node が PATH 上で解決できる必要がある。start.sh 経由で
# 起動したアプリ（uvicorn）プロセスはこの PATH を継承する。通常環境では tools/node が無いので no-op。
if [ -d "$ROOT/tools/node/bin" ]; then
  case ":$PATH:" in
    *":$ROOT/tools/node/bin:"*) : ;;  # 既に PATH にあれば重複追加しない
    *) PATH="$ROOT/tools/node/bin:$PATH" ;;
  esac
  export PATH
fi
# 閉域キットが展開する Codex CLI（tools/codex/bin/codex＝静的バイナリへの symlink）。sherpa は
# `shutil.which("codex")` で探すため、PATH に載せるだけで見つかる（2026-08-18・キットに Codex を同梱）。
if [ -d "$ROOT/tools/codex/bin" ]; then
  case ":$PATH:" in
    *":$ROOT/tools/codex/bin:"*) : ;;
    *) PATH="$ROOT/tools/codex/bin:$PATH" ;;
  esac
  export PATH
fi

# ランタイム状態（pid/log）。data/ は .gitignore 済み＝配布物にも入らない。
# テストは APP_PID_FILE / APP_PROC_NEEDLE を環境で差し替えられる（既定は従来どおり）。
RUN_DIR="${RUN_DIR:-$ROOT/data/run}"
APP_PID_FILE="${APP_PID_FILE:-$RUN_DIR/api.pid}"
APP_LOG="$RUN_DIR/api.log"
CADDY_PID_FILE="$RUN_DIR/caddy.pid"
CADDY_LOG="$RUN_DIR/caddy.log"

# ---------------------------------------------------------------------------
# .env の読み方は**ここに一本化**する（2026-08-17）。以前は 6 通り（丸ごと source ×3・source＋2変数だけ
# 退避 ×1・「未設定なら .env から」ヘルパのコピー ×3）に割れ、優先順位もファイル指定の扱いも
# スクリプトごとに違っていた。実害: `.env` の SHERPA_PORT が既定 8000 で上書きされる／
# `SHERPA_DERIVED_DIR=… make nuke` の明示指定が .env に黙って負ける、など。
#
# 契約（全スクリプト共通）: **呼び出し側の明示指定 ＞ .env ＞ 既定**。ファイルは `SHERPA_ENV_FILE`（無ければ
# リポジトリ直下の .env）。
#   sherpa_dotenv_file            → 読むファイルのパス
#   sherpa_env_default VAR...     → 列挙した変数だけを「未設定のときに限り」.env から取り込む（少数の変数で足りる
#                                   スクリプト向け: start/stop/status/nuke/ocr-up）
#   sherpa_source_dotenv [file]   → .env を丸ごと取り込むが、**呼び出し前から環境にあった変数は上書きしない**
#                                   （アプリ本体のように全変数が要るスクリプト向け: run-api/bootstrap/demo）
# ---------------------------------------------------------------------------
sherpa_dotenv_file() { printf '%s\n' "${SHERPA_ENV_FILE:-$ROOT/.env}"; }

sherpa_env_default() {  # VAR...  （既に環境にあれば何もしない）
  local name file line value
  file="$(sherpa_dotenv_file)"
  [ -f "$file" ] || return 0
  for name in "$@"; do
    [ -n "${!name:-}" ] && continue
    line="$(grep -E "^[[:space:]]*(export[[:space:]]+)?${name}=" "$file" | tail -1 || true)"
    [ -n "$line" ] || continue
    value="${line#*=}"
    value="${value%\"}"; value="${value#\"}"      # 前後の引用符だけ外す
    value="${value%\'}"; value="${value#\'}"
    printf -v "$name" '%s' "$value"
    export "${name?}"
  done
}

sherpa_source_dotenv() {  # [file]
  local file="${1:-$(sherpa_dotenv_file)}" name keys pre_names=() pre_vals=()
  [ -f "$file" ] || return 0
  # .env に書かれているキーのうち、既に環境にあるものを退避 → source → 戻す（明示指定が勝つ）。
  keys="$(grep -oE '^[[:space:]]*(export[[:space:]]+)?[A-Za-z_][A-Za-z0-9_]*=' "$file" | sed -E 's/^[[:space:]]*(export[[:space:]]+)?//; s/=$//' | sort -u || true)"
  for name in $keys; do
    if [ -n "${!name+x}" ]; then pre_names+=("$name"); pre_vals+=("${!name}"); fi
  done
  set -a
  # shellcheck disable=SC1090
  . "$file"
  set +a
  local i
  for i in "${!pre_names[@]}"; do
    printf -v "${pre_names[$i]}" '%s' "${pre_vals[$i]}"
    export "${pre_names[$i]?}"
  done
}

# docker compose の呼び出しは**必ずこれ経由**（2026-08-18）。compose 自身は「自分のディレクトリの .env」しか
# 読まないため、本番の `SHERPA_ENV_FILE=/etc/sherpa/sherpa.env` に書いた PGPORT/SHERPA_ES_PORT/POSTGRES_PASSWORD 等が
# 無視され、ストアが既定ポート・既定パスワードで上がる事故があった。読む .env は上と同じ 1 か所
# （sherpa_dotenv_file）に揃える。ファイルが無ければ素の docker compose（compose の既定挙動＝リポジトリ直下の .env）。
# 注意: `--env-file` はサブコマンド（up/down/…）より**前**に置く。interpolation はシェル環境変数が --env-file より
# 優先されるので「呼び出し側の明示指定 ＞ .env ＞ 既定」の契約は compose 側でも同じ。
sherpa_compose() {
  local file
  file="$(sherpa_dotenv_file)"
  if [ -n "${SHERPA_ENV_FILE:-}" ] && [ ! -f "$file" ]; then
    echo "指定された SHERPA_ENV_FILE がありません: $file" >&2
    return 2
  fi
  if [ -f "$file" ]; then
    docker compose --env-file "$file" "$@"
  else
    docker compose "$@"
  fi
}

sherpa_env_default SHERPA_PORT SHERPA_HOST

# ---------------------------------------------------------------------------
# Codex CLI の API キー認証を保証する（2026-08-18）。
# `codex login --with-api-key` は ~/.codex/auth.json（{"auth_mode":"apikey",…}・0600）を書くだけで
# **通信しない**（実測: ネットワーク名前空間を切っても Successfully logged in）。冪等なので毎起動で呼んでよい。
# 方針: .env の OPENAI_API_KEY が実値なら認証する／同じキーで認証済みなら何もしない／
#       サブスク（chatgpt）で認証済みなら**上書きしない**（利用者が選んだ方式を尊重）。
# 戻り値: 0=認証済み（今回実施 or 既に済み）／1=未実施（codex 無し・キー未設定・失敗＝非致命）
# ---------------------------------------------------------------------------
sherpa_codex_ensure_auth() {
  command -v codex >/dev/null 2>&1 || return 1
  sherpa_env_default OPENAI_API_KEY
  local key="${OPENAI_API_KEY:-}" home auth
  case "$key" in ""|sk-REPLACE_ME|REPLACE_ME) return 1 ;; esac
  home="${CODEX_HOME:-$HOME/.codex}"; auth="$home/auth.json"
  if [ -f "$auth" ]; then
    if grep -q '"auth_mode"[[:space:]]*:[[:space:]]*"apikey"' "$auth" 2>/dev/null; then
      grep -qF "\"$key\"" "$auth" 2>/dev/null && return 0        # 同じキーで認証済み
    elif grep -q '"auth_mode"' "$auth" 2>/dev/null; then
      return 0                                                   # 別方式（サブスク等）＝触らない
    fi
  fi
  mkdir -p "$home" && chmod 700 "$home"
  if printf '%s\n' "$key" | codex login --with-api-key >/dev/null 2>&1; then
    echo "Codex CLI を API キーで認証しました（$auth・通信なし）"
    return 0
  fi
  echo "ⓘ Codex CLI の API キー認証に失敗しました。手動: printenv OPENAI_API_KEY | codex login --with-api-key" >&2
  return 1
}

# アプリの bind/port（run-api.sh と同じ既定）。healthz は常に loopback で確認する
# （0.0.0.0 で待受していても 127.0.0.1 で到達できる）。
SHERPA_PORT="${SHERPA_PORT:-8000}"
HEALTH_URL="http://127.0.0.1:${SHERPA_PORT}/healthz"
CHAT_URL="http://127.0.0.1:${SHERPA_PORT}/ui/chat.html"

# pid ファイルの所有プロセスを見分けるための部分文字列（コマンドラインに含まれる語）。
APP_PROC_NEEDLE="${APP_PROC_NEEDLE:-uvicorn}"
CADDY_PROC_NEEDLE="caddy"

# pid ファイルから生存している PID を stdout に返す（0）。無い/空/死亡なら非0で何も出さない。
# 注意: これだけでは「pid は生きているが再利用された別プロセス」を見分けられない。
# kill 対象を決める場合や「これは我々の起動したプロセスか」を確認する場合は
# live_matching_pid() を使うこと（RV High-1: pid 再利用対策）。
# 使い方（set -e 下でも安全なように if の条件で使う）:
#   if pid="$(live_pid "$PIDFILE")"; then echo "生存: $pid"; fi
live_pid() {
  local pf="$1" pid
  [ -f "$pf" ] || return 1
  pid="$(cat "$pf" 2>/dev/null || true)"
  [ -n "$pid" ] || return 1
  kill -0 "$pid" 2>/dev/null || return 1
  printf '%s' "$pid"
}

# PID のコマンドラインに needle（部分文字列）が含まれるか。
# /proc/$pid/cmdline（NUL 区切り→空白に変換）を優先し、無ければ `ps -o command=` にフォールバック
# （/proc が無い環境・busybox ps 等を想定）。
pid_matches() {
  local pid="$1" needle="$2" cmd=""
  if [ -r "/proc/$pid/cmdline" ]; then
    cmd="$(tr '\0' ' ' < "/proc/$pid/cmdline" 2>/dev/null || true)"
  fi
  if [ -z "$cmd" ]; then
    cmd="$(ps -o command= -p "$pid" 2>/dev/null || true)"
  fi
  [ -z "$cmd" ] && return 1
  case "$cmd" in
    *"$needle"*) return 0 ;;
    *) return 1 ;;
  esac
}

# pid ファイルが指す PID が「生存」かつ「needle にマッチ（我々が起動したプロセス）」の場合だけ
# PID を stdout に返して 0。判定結果ごとに終了コードを分ける（呼び出し側が挙動を変えられるように）:
#   0: 生存＋一致（正当な PID）
#   1: 未起動／pid ファイル無し／死亡（通常の「起動していない」）
#   2: 生存だが不一致（＝pid 再利用の疑い。プロセスには触らず pid ファイルだけ掃除すべき）
# 使い方:
#   if pid="$(live_matching_pid "$PIDFILE" "$NEEDLE")"; then ... 生存 ...
#   else rc=$?; [ "$rc" = 2 ] && echo "pid 再利用の疑い"; fi
live_matching_pid() {
  local pf="$1" needle="$2" pid
  pid="$(live_pid "$pf")" || return 1
  if pid_matches "$pid" "$needle"; then
    printf '%s' "$pid"
    return 0
  fi
  return 2
}

# healthz が 200 を返すか（curl 前提・curl が無ければ常に false）。
healthz_ok() {
  command -v curl >/dev/null 2>&1 || return 1
  curl -fsS --max-time "${SHERPA_HEALTH_CURL_TIMEOUT:-3}" "$HEALTH_URL" >/dev/null 2>&1
}

# ---------------------------------------------------------------------------
# 起動時ログローテーション（LOG-2・2026-09-03）: 起動のたびに run/caddy ログを空へ切り詰める
# （旧 `: > "$LOG"`）と、前回の障害調査ができない。既存ログが非空ならタイムスタンプ付きへ退避して
# から空で作り直し、同ファミリー（退避ファイル）の保持数（SHERPA_LOG_KEEP・既定10）超過分だけ
# 古い順に削除する。命名規約は `<stem>-YYYYmmdd-HHMMSS[-N]<拡張子>`——Python 側
# （`sherpa/log_setup.py::rotate_and_prune`・サブシステム専用ログの退避）と揃えている。
# ---------------------------------------------------------------------------

# $1=ログファイル。呼び出し側が事前に mkdir -p している前提（start.sh は RUN_DIR を作成済み）。
sherpa_rotate_log() {
  local log="$1" dir base stem suffix ts archived n
  dir="$(dirname "$log")"
  mkdir -p "$dir"
  if [ -s "$log" ]; then
    base="$(basename "$log")"
    case "$base" in
      *.*) suffix=".${base##*.}"; stem="${base%.*}" ;;
      *) suffix=""; stem="$base" ;;
    esac
    ts="$(date +%Y%m%d-%H%M%S)"
    archived="$dir/${stem}-${ts}${suffix}"
    n=2
    while [ -e "$archived" ]; do
      archived="$dir/${stem}-${ts}-${n}${suffix}"
      n=$((n + 1))
    done
    mv "$log" "$archived"
  fi
  : > "$log"
  sherpa_prune_log_family "$log"
}

# $1=ログファイル。同ファミリーの退避ファイル（$1 と同じ dir・stem・拡張子で、退避の命名規約に
# 厳密一致するものだけ＝無関係ファイルを消さないガード）を保持数超過分だけ古い順に削除する。
sherpa_prune_log_family() {
  local log="$1" dir base stem suffix keep f files=() count excess i
  sherpa_env_default SHERPA_LOG_KEEP
  keep="${SHERPA_LOG_KEEP:-10}"
  case "$keep" in ''|*[!0-9]*) keep=10 ;; esac
  dir="$(dirname "$log")"
  base="$(basename "$log")"
  case "$base" in
    *.*) suffix=".${base##*.}"; stem="${base%.*}" ;;
    *) suffix=""; stem="$base" ;;
  esac
  # 緩い glob（同 dir・同 stem・同拡張子）で候補を集めてから、退避ファイル名の厳密パターンで
  # 絞り込む（glob 展開は名前順＝タイムスタンプ順に揃うため、絞り込み後も古い順を保つ）。
  for f in "$dir"/"$stem"-*"$suffix"; do
    [ -e "$f" ] || continue
    case "$(basename "$f")" in
      "$stem"-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]"$suffix") files+=("$f") ;;
      "$stem"-[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9]-[0-9][0-9][0-9][0-9][0-9][0-9]-[0-9]*"$suffix") files+=("$f") ;;
    esac
  done
  count="${#files[@]}"
  excess=$(( count - keep ))
  [ "$excess" -gt 0 ] || return 0
  i=0
  for f in "${files[@]}"; do
    i=$((i + 1))
    [ "$i" -le "$excess" ] || break
    rm -f -- "$f"
  done
}
