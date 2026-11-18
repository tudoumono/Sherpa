#!/usr/bin/env bash
# 資料フォルダごとの取り込みと画像内文字の読み取りの状態を表示する。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"

ENV_FILE="$(sherpa_dotenv_file)"
if [ -f "$ENV_FILE" ]; then
  sherpa_source_dotenv "$ENV_FILE"
elif [ "${SHERPA_REQUIRE_ENV_FILE:-0}" = 1 ]; then
  echo "指定された設定ファイルがありません"
  exit 0
fi

PYTHON_BIN="${SHERPA_VENV:-$ROOT/.venv}/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}" exec "$PYTHON_BIN" "$ROOT/scripts/ingest_status.py"
