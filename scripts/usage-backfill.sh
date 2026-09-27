#!/usr/bin/env bash
# turn_metrics/turn_tool_stats への移し替え（`make usage-backfill`）の薄いラッパー。運用環境の
# 設定ファイルを doctor.sh/diag.sh と同じ読み方（`sherpa_source_dotenv`・既定 $ROOT/.env・
# SHERPA_ENV_FILE で差し替え）で取り込んでから実行する（読まずに実行すると store が既定の
# 接続先を使い、正しい DB へ書けない）。設定ファイルの中身は端末に出さない。
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

exec "$PYTHON_BIN" -c "from sherpa import store; n = store.backfill_all(); print(f'turn_metrics backfill: {n} 件')"
