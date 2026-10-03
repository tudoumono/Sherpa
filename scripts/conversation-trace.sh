#!/usr/bin/env bash
# 会話トレースの書き出し（`make trace`）の薄いラッパー。運用環境の設定ファイル（SHERPA_ENV_FILE で
# 差し替え可）を取り込んでから実行する。設定ファイルの中身は端末に出さない。
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

exec "$PYTHON_BIN" "$ROOT/scripts/conversation_trace.py" "$@"
