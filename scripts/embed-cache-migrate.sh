#!/usr/bin/env bash
# 埋め込みキャッシュを新しい形式へ手で写し替える（make embed-cache-migrate）。取り込みワーカーと同じ環境（.env）で python -m sherpa.embed_cache_migrate を動かす。
# 同じ資料フォルダを取り込みと同時に触らない（資料フォルダのロックを取れないものは見送る）。
#   WORLD=<名前>  1 つの資料フォルダに絞る
#   DROP=1        空きが足りないときの最後の手段。写し替えずに古いキャッシュを消す（次の取り込みで埋め込みの API を呼び直す）
#   MODE=dev      開発モード（既定は本番）
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
ENV_FILE="$(sherpa_dotenv_file)"
if [ -f "$ENV_FILE" ]; then
  sherpa_source_dotenv "$ENV_FILE"
fi

if [ "${MODE:-serve}" = "dev" ]; then
  export SHERPA_ENV="development"
  export SHERPA_USE_FIXTURES="1"
else
  export SHERPA_ENV="production"
  unset SHERPA_USE_FIXTURES
fi

if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "${SHERPA_VENV:-$ROOT/.venv}/bin/python" ]; then PYTHON_BIN="${SHERPA_VENV:-$ROOT/.venv}/bin/python"; else PYTHON_BIN="python3"; fi
fi

exec "$PYTHON_BIN" -m sherpa.embed_cache_migrate
