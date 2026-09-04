#!/usr/bin/env bash
# 全ログを1画面で見るための入口（`make logs`・2026-09-04 ユーザー依頼・LOG-UX）。
# アプリ側ログ（data/run/*.log）と Docker ストアのログ（postgres/neo4j/elasticsearch/ocr-worker）を
# 合流させ、[mem] 行（メモリと主要プロセス RSS）も添えて1画面で追う。名前は統一名前空間
# （アプリ側・Docker 側どちらの名前でも同じ引数で指定できる）。外部依存は docker（無くてもアプリ側
# だけで動く＝fail-soft）。詳しい使い方は ./scripts/logs.sh -h を参照（対応する名前を実環境から動的に
# 表示する）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh" 2>/dev/null || true
LOG_DIR="${SHERPA_LOG_DIR:-data/run}"

LINES=20 GREP="" LIST=0 MEM_INTERVAL=10 REPORT=0 REPORT_ALL=0 PRINT_HELP=0
NAMES=()
EXCLUDES=()
while [ $# -gt 0 ]; do
  case "$1" in
    -n) LINES="$2"; shift 2 ;;
    -m) MEM_INTERVAL="$2"; shift 2 ;;
    -g|--grep) GREP="$2"; shift 2 ;;
    -l|--list) LIST=1; shift ;;
    -r|--report) REPORT=1; shift ;;
    -A|--all) REPORT_ALL=1; shift ;;
    -x) EXCLUDES+=("$2"); shift 2 ;;
    -h|--help) PRINT_HELP=1; shift ;;
    *) NAMES+=("$1"); shift ;;
  esac
done

# ---- 名前空間の発見（実環境から動的に。docker が引けない環境はアプリ側のみへ fail-soft） ----

APP_FILES=()
while IFS= read -r f; do [ -n "$f" ] && APP_FILES+=("$f"); done \
  < <(ls "$LOG_DIR"/*.log 2>/dev/null | grep -v '\.log\.[0-9]*$' || true)
APP_NAMES=()
for f in "${APP_FILES[@]}"; do APP_NAMES+=("$(basename "$f" .log)"); done

DOCKER_AVAILABLE=0
DOCKER_SERVICES=()
if command -v docker >/dev/null 2>&1 && command -v sherpa_compose >/dev/null 2>&1; then
  while IFS= read -r s; do [ -n "$s" ] && DOCKER_SERVICES+=("$s"); done \
    < <(sherpa_compose --profile ocr config --services 2>/dev/null || true)
  [ ${#DOCKER_SERVICES[@]} -gt 0 ] && DOCKER_AVAILABLE=1
fi

# 別名（利用者の慣用短縮形）→ 実サービス名。実サービス名自体もそのまま通す。
_docker_canonical() {   # $1=生の名前 -> 一致すれば実サービス名を1行出力・不一致は非0
  local n="$1" want="$1" s
  case "$n" in
    es) want="elasticsearch" ;;
    ocr) want="ocr-worker" ;;
    pg) want="postgres" ;;
  esac
  for s in "${DOCKER_SERVICES[@]}"; do
    [ "$s" = "$want" ] && { echo "$s"; return 0; }
  done
  return 1
}

_candidates_str() {
  local extra=""
  if [ "$DOCKER_AVAILABLE" = 1 ]; then
    extra="  |  Docker側: ${DOCKER_SERVICES[*]}（別名: es→elasticsearch, ocr→ocr-worker, pg→postgres）"
  else
    extra="  |  Docker側: （利用できません・docker が無い/権限が無い/ストア未定義）"
  fi
  echo "アプリ側: ${APP_NAMES[*]:-(なし)}${extra}"
}

_resolve_name() {   # $1=生の名前 -> "app:<path>" または "docker:<service>" を出力・不一致は非0
  local n="$1" base="${1%.log}" f
  for f in "${APP_FILES[@]}"; do
    [ "$(basename "$f" .log)" = "$base" ] && { echo "app:$f"; return 0; }
  done
  if [ "$DOCKER_AVAILABLE" = 1 ]; then
    local d
    if d="$(_docker_canonical "$base")"; then echo "docker:$d"; return 0; fi
  fi
  return 1
}

# $@ = 生の名前群。解決できれば RES_APP[]/RES_DOCKER[] を埋めて 0 を返す。
# 解決できない名前が1つでもあれば、見つからなかった名前と候補一覧を stderr へ出して非0を返す
# （黙って空にしない＝追加要件4）。
_resolve_list() {
  RES_APP=(); RES_DOCKER=()
  local unknown=() n res
  for n in "$@"; do
    if res="$(_resolve_name "$n")"; then
      case "$res" in
        app:*) RES_APP+=("${res#app:}") ;;
        docker:*) RES_DOCKER+=("${res#docker:}") ;;
      esac
    else
      unknown+=("$n")
    fi
  done
  if [ ${#unknown[@]} -gt 0 ]; then
    echo "見つかりません: ${unknown[*]}" >&2
    echo "候補: $(_candidates_str)" >&2
    return 1
  fi
  return 0
}

# ---- ヘルプ（動的な名前一覧つき・追加要件4/7） ----

_print_help() {
  cat <<EOF
使い方: ./scripts/logs.sh [オプション] [名前...]

アプリ側ログ（$LOG_DIR/*.log）と Docker ストアのログを1画面に合流して追います
（[mem] 行＝メモリと主要プロセス RSS も既定 ${MEM_INTERVAL} 秒おき）。名前を1つでも指定すると、
指定していない側（アプリ/Docker）は出しません。

オプション:
  -n N          追う前にまず末尾 N 行を表示（既定 20）
  -g PATTERN    正規表現に一致する行だけ表示（タグ付与後に適用）
  -m N          [mem] 行の間隔を N 秒に（既定 10・0 で出さない）
  -x 名前       表示対象から除外（複数回指定可・位置引数で選んだ後に除外を適用）
  -l, --list    追わずに一覧と各ログの末尾だけ表示して終了
  -r, --report  追わずに集計レポートを表示して終了（scripts/log_report.py・アプリ側ログが対象）
  -A, --all     -r と併用: 退避された過去世代（ローテーション）も連結して集計
  -h, --help    このヘルプを表示

指定できる名前（実環境から取得）:
  $(_candidates_str)

よく使う例:
  資料取り込みを監視する        ./scripts/logs.sh convert embed libreoffice -m 5
  エラー・警告だけ拾う          ./scripts/logs.sh -g 'ERROR|WARN|失敗|✗'
  アプリ全般（api のノイズ抜き） ./scripts/logs.sh -x api
  ストア（Docker）も含め全部     ./scripts/logs.sh
  今の状況を一覧で（追わない）    ./scripts/logs.sh -l
  実行後の集計レポート           ./scripts/logs.sh -r        （過去世代も含める: -r -A）

make 経由: make logs ARGS="convert embed"  /  make logs ARGS="-h"
EOF
}

if [ "$PRINT_HELP" = 1 ]; then
  _print_help
  exit 0
fi

# ---- 表示対象の決定（位置引数で選択 → -x で除外・追加要件6） ----

if [ ${#NAMES[@]} -gt 0 ]; then
  _resolve_list "${NAMES[@]}" || exit 1
  SEL_APP=("${RES_APP[@]}"); SEL_DOCKER=("${RES_DOCKER[@]}")
else
  SEL_APP=("${APP_FILES[@]}")
  if [ "$DOCKER_AVAILABLE" = 1 ]; then SEL_DOCKER=("${DOCKER_SERVICES[@]}"); else SEL_DOCKER=(); fi
fi

if [ ${#EXCLUDES[@]} -gt 0 ]; then
  _resolve_list "${EXCLUDES[@]}" || exit 1
  EX_APP=("${RES_APP[@]}"); EX_DOCKER=("${RES_DOCKER[@]}")
  NEW_APP=()
  for f in "${SEL_APP[@]}"; do
    skip=0
    for e in "${EX_APP[@]}"; do [ "$f" = "$e" ] && skip=1; done
    [ $skip -eq 0 ] && NEW_APP+=("$f")
  done
  SEL_APP=("${NEW_APP[@]}")
  NEW_DOCKER=()
  for s in "${SEL_DOCKER[@]}"; do
    skip=0
    for e in "${EX_DOCKER[@]}"; do [ "$s" = "$e" ] && skip=1; done
    [ $skip -eq 0 ] && NEW_DOCKER+=("$s")
  done
  SEL_DOCKER=("${NEW_DOCKER[@]}")
fi

if [ ${#SEL_APP[@]} -eq 0 ] && [ ${#SEL_DOCKER[@]} -eq 0 ]; then
  echo "表示対象がありません（絞り込み/除外の結果が空です）" >&2
  exit 1
fi

# ---- -r/--report: 追わずに集計して終了 ----

if [ "$REPORT" = 1 ]; then
  if [ ${#SEL_DOCKER[@]} -gt 0 ]; then
    echo "-r はアプリ側ログ（ファイル）のみ対象です。Docker 側の名前は無視します: ${SEL_DOCKER[*]}" >&2
  fi
  if [ ${#SEL_APP[@]} -eq 0 ]; then
    echo "レポート対象のアプリ側ログがありません" >&2
    exit 1
  fi
  PYBIN="python3"
  [ -x "$ROOT/.venv/bin/python" ] && PYBIN="$ROOT/.venv/bin/python"
  REPORT_NAMES=()
  for f in "${SEL_APP[@]}"; do REPORT_NAMES+=("$(basename "$f" .log)"); done
  ARGS_R=(--log-dir "$LOG_DIR")
  [ "$REPORT_ALL" = 1 ] && ARGS_R+=(--all)
  exec "$PYBIN" "$ROOT/scripts/log_report.py" "${ARGS_R[@]}" "${REPORT_NAMES[@]}"
fi

# ---- -l/--list: 追わずに一覧・末尾だけ表示して終了 ----

if [ "$LIST" = 1 ]; then
  for f in "${SEL_APP[@]}"; do
    printf '\n\033[1m== %s（%s・%s行）==\033[0m\n' "$f" "$(du -h "$f" | cut -f1)" "$(wc -l < "$f")"
    tail -n 5 "$f"
  done
  if [ ${#SEL_DOCKER[@]} -gt 0 ]; then
    for s in "${SEL_DOCKER[@]}"; do
      printf '\n\033[1m== docker:%s ==\033[0m\n' "$s"
      sherpa_compose logs --no-color --tail=5 "$s" 2>&1 || echo "（取得できません）"
    done
  fi
  exit 0
fi

# ---- 追いモード: アプリ側 tail ＋ Docker compose logs ＋ [mem] を1画面へ合流 ----

# [mem] 行は他の2系統（下記 FIFO 経由の tail/docker）とは独立に、このスクリプトの標準出力へ
# 直接書く（行単位の書き込みなので混ざっても壊れない・元実装から踏襲）。
MEM_PID=""
if [ "$MEM_INTERVAL" -gt 0 ] 2>/dev/null; then
  (
    while :; do
      line=$(awk '/MemTotal/{t=$2} /MemAvailable/{a=$2} END{printf "空き%.1fG / 全体%.1fG", a/1048576, t/1048576}' /proc/meminfo)
      procs=$(ps -eo rss=,comm= --sort=-rss | awk '$2 ~ /python|soffice|ollama|node|uvicorn/ && $1 > 51200 {printf " %s=%.1fG", $2, $1/1048576; if (++n >= 4) exit}')
      printf '\033[90m[mem] %s |%s\033[0m\n' "$line" "${procs:- (大口プロセスなし)}"
      sleep "$MEM_INTERVAL"
    done
  ) &
  MEM_PID=$!
fi

TAIL_PID=""
DOCKER_PID=""
FIFO=""
_cleanup() {
  [ -n "$MEM_PID" ] && kill "$MEM_PID" 2>/dev/null
  [ -n "$TAIL_PID" ] && kill "$TAIL_PID" 2>/dev/null
  [ -n "$DOCKER_PID" ] && kill "$DOCKER_PID" 2>/dev/null
  [ -n "$FIFO" ] && rm -f "$FIFO"
}
trap _cleanup EXIT INT TERM

if [ ${#SEL_DOCKER[@]} -gt 0 ] && [ "$DOCKER_AVAILABLE" != 1 ]; then
  echo "Docker ストアのログは利用できません（docker が無い/権限が無い）。アプリ側のみ表示します。" >&2
fi

SINGLE=""
# `tail` はそれ自身に渡すファイルが1個だけだと "==> path <==" ヘッダを出さない（Docker 側が
# 同時に流れているかどうかは無関係・tail 単体の挙動）ため、その場合は名前を先に確定しておく
# （元実装から踏襲）。
[ ${#SEL_APP[@]} -eq 1 ] && SINGLE="$(basename "${SEL_APP[0]}" .log)"

# 2つの背景ジョブ（アプリ tail・docker compose logs）の標準出力を名前付きパイプへ合流させ、1本の
# awk で色付け・タグ付けする。**`{ cmd1 & cmd2 & wait; } | awk` は使わない**——パイプの左辺は
# bash が別サブシェルで実行するため、その中で捕まえた `$!`（TAIL_PID/DOCKER_PID）はサブシェル内の
# 変数にしかならず、外側（このスクリプト本体）の `_cleanup` からは値が見えず kill できない。
# 実測で `docker compose logs -f` の子プロセスが孤児化して残るバグを踏んだ——FIFO 経由なら
# `tail`/`docker compose` はどちらもこのシェル自身の直接の子（`&` のみ・パイプではない）として
# 起動でき、`$!` が正しい PID を指す。awk 側は FIFO を素の入力リダイレクトで読む（追加プロセス無し）。
if command -v mktemp >/dev/null 2>&1; then
  FIFO="$(mktemp -u)"
else
  FIFO="/tmp/sherpa-logs-$$.fifo"
fi
mkfifo "$FIFO"

if [ ${#SEL_APP[@]} -gt 0 ]; then
  tail -n "$LINES" -F "${SEL_APP[@]}" > "$FIFO" 2>/dev/null &
  TAIL_PID=$!
fi
if [ ${#SEL_DOCKER[@]} -gt 0 ] && [ "$DOCKER_AVAILABLE" = 1 ]; then
  sherpa_compose logs -f --tail="$LINES" "${SEL_DOCKER[@]}" > "$FIFO" 2>&1 &
  DOCKER_PID=$!
fi

awk -v grepPat="$GREP" -v single="$SINGLE" '
BEGIN {
  ncolor = split("36 34 35 32 33", colors, " ")
  if (single != "") { tag = single; tagcolor = colors[1]; seen[single] = 1; nseen = 1 }
  else { tag = "?"; tagcolor = 37 }
}
/^[A-Za-z0-9_.-]+-[0-9]+ *\| ?/ {
  name = $0
  sub(/-[0-9]+ .*/, "", name)
  dname = "docker:" name
  rest = $0
  sub(/^[A-Za-z0-9_.-]+-[0-9]+ *\| ?/, "", rest)
  if (!(dname in seen)) { seen[dname] = (++nseen - 1) % ncolor + 1 }
  line = sprintf("\033[%sm[%s]\033[0m %s", colors[seen[dname]], dname, rest)
  if (grepPat != "" && line !~ grepPat) next
  if (rest ~ /ERROR|CRITICAL|Traceback|失敗|✗/)      printf "\033[31m%s\033[0m\n", line
  else if (rest ~ /WARN/)                             printf "\033[33m%s\033[0m\n", line
  else if (rest ~ /OK:|完了|成功|stores up/)          printf "\033[32m%s\033[0m\n", line
  else print line
  fflush()
  next
}
/^==> .* <==$/ {
  path = $2
  name = path; sub(/.*\//, "", name); sub(/\.log$/, "", name)
  if (!(name in seen)) { seen[name] = (++nseen - 1) % ncolor + 1 }
  tag = name; tagcolor = colors[seen[name]]
  next
}
{
  line = sprintf("\033[%sm[%s]\033[0m %s", tagcolor, tag, $0)
  if (grepPat != "" && line !~ grepPat) next
  if ($0 ~ /ERROR|CRITICAL|Traceback|失敗|✗/)      printf "\033[31m%s\033[0m\n", line
  else if ($0 ~ /WARN/)                              printf "\033[33m%s\033[0m\n", line
  else if ($0 ~ /OK:|完了|成功|stores up/)           printf "\033[32m%s\033[0m\n", line
  else print line
  fflush()
}' < "$FIFO"
