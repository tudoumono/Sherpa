#!/usr/bin/env bash
# LibreOffice の前段変換（旧形式 .doc/.xls/.ppt → OOXML）で時間切れになった資料を、直近の取り込みの記録（失敗の一覧）から集める。
# 拡張子ごと・大きさの帯ごとの件数と、.xls のシート数・行数の目安を出す（読むだけ・何も書き換えない）。
#   WORLD=<名前>  1 つの資料フォルダに絞る（省略で全部）
# 守ること: 資料のファイル名・フォルダ名・パスを出力に出さない（件数と帯だけ）。設定ファイルの中身は端末に出さない。
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

exec "$PYTHON_BIN" "$ROOT/scripts/lo_timeout_report.py" ${args[@]+"${args[@]}"}
