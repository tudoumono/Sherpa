#!/usr/bin/env bash
# 回答に時間がかかる原因の調べ（DB・原本は書き換えない。②の文字列検索だけは回答のときと同じく検索の使い回し用の置き場に書く）。
# ① 資料の置き場（原本・派生）の速さ ② 回答のときと同じ設定で起動した道具のサーバーの速さ ③ 直近の回答の道具ごとの時間 を出す。
#   WORLD=<名前>        1 つの資料フォルダに絞る（省略で全部）
#   DAYS=<日数>         ③で見る期間（既定 2 日）
#   SKIP=walk,rg,mcp,turns  飛ばす調べ（カンマ区切り）
#   RG_TIMEOUT=<秒>     rg 1 回の打ち切り（既定 120 秒）
#   WALK_TIMEOUT=<秒>   フォルダを歩く打ち切り（既定 300 秒）
#   TURNS_TIMEOUT=<秒>  ③の打ち切り（既定 300 秒）
# 守ること: 資料のファイル名・フォルダ名・パス・ホスト名を出力に出さない（件数と時間だけ）。設定ファイルの中身は端末に出さない。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

_sherpa_env_file_explicit="${SHERPA_ENV_FILE:+1}"
export SHERPA_ENV_FILE="${SHERPA_ENV_FILE:-$ROOT/.env}"
if [ -n "$_sherpa_env_file_explicit" ] && [ ! -f "$SHERPA_ENV_FILE" ]; then
  echo "指定された SHERPA_ENV_FILE が見つかりません（通常ファイルではありません）" >&2
  exit 2
fi
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_source_dotenv "$SHERPA_ENV_FILE"

if [ -x "$ROOT/.venv/bin/python" ]; then
  PYTHON_BIN="${PYTHON_BIN:-$ROOT/.venv/bin/python}"
else
  PYTHON_BIN="${PYTHON_BIN:-python3}"
fi

args=()
[ -z "${WORLD:-}" ] || args+=(--world "$WORLD")
[ -z "${DAYS:-}" ] || args+=(--days "$DAYS")
[ -z "${SKIP:-}" ] || args+=(--skip "$SKIP")
[ -z "${RG_TIMEOUT:-}" ] || args+=(--rg-timeout "$RG_TIMEOUT")
[ -z "${WALK_TIMEOUT:-}" ] || args+=(--walk-timeout "$WALK_TIMEOUT")
[ -z "${TURNS_TIMEOUT:-}" ] || args+=(--turns-timeout "$TURNS_TIMEOUT")
export RUN_DIR

exec "$PYTHON_BIN" "$ROOT/scripts/slow_turn_report.py" ${args[@]+"${args[@]}"}
