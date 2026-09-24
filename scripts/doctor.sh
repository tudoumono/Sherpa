#!/usr/bin/env bash
# 導入先の統合セットアップ検査（`make doctor`）。ストア疎通・設定妥当性・LLM 最小プローブ・
# Codex 経路を1コマンドで確認する。
#
# 検査本体は scripts/doctor_checks.py（単体テスト可能な独立モジュール）。このスクリプトは
# `.env`（既定・`scripts/check-production.sh` は `.env.production` が既定＝本番専用だが、doctor は
# `make up` 直後の通常運用環境を検査する道具のため既定を `.env` にする）を読み込んでから実行する
# だけの薄いラッパー（読み方は run-common.sh に一本化＝`sherpa_source_dotenv` を使う）。
#
# 課金の可能性がある実 API プローブ（OpenAI/Gemini/Bedrock。Ollama はローカルのため対象外）は
# 既定で無効。`make doctor PROBE_CLOUD=1` で有効化する（doctor_checks.py::probe_cloud_enabled）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 呼び出し側が SHERPA_ENV_FILE を明示指定したのに、そのパスが通常ファイルとして存在しない
# （タイプミス・ディレクトリを指定した等）場合は、意図した設定を読めないまま黙って続行せず
# エラー終了する（沈黙した誤設定は「DB/ES に接続できない」等の的外れな NG として現れ、原因調査を
# 誤らせる）。既定値（未指定時の $ROOT/.env）が存在しないのは正常な状態として許容する
# （導入直後で .env をまだ作っていない・env を直接渡す運用等）。
_sherpa_env_file_explicit="${SHERPA_ENV_FILE:+1}"
export SHERPA_ENV_FILE="${SHERPA_ENV_FILE:-$ROOT/.env}"
if [ -n "$_sherpa_env_file_explicit" ] && [ ! -f "$SHERPA_ENV_FILE" ]; then
  echo "指定された SHERPA_ENV_FILE が見つかりません（通常ファイルではありません）: $SHERPA_ENV_FILE" >&2
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

exec "$PYTHON_BIN" "$ROOT/scripts/doctor_checks.py" "$@"
