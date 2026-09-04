#!/usr/bin/env bash
# Single entrypoint for running the Sherpa FastAPI app.
#
# Usage:
#   ./scripts/run-api.sh dev    # development: fixtures enabled
#   ./scripts/run-api.sh serve  # production: fixtures forcibly disabled
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 閉域キット導入環境向け（install_offline_kit.sh が tools/node/ へ Node.js を展開する）:
# marp CLI は node スクリプトのため uvicorn プロセスの PATH に node が必要。start.sh 経由なら
# run-common.sh が同じ配線をするが、本スクリプトは直接起動も案内されている（offline-kit.md）ため
# ここでも足す。通常環境では tools/node が無いので no-op。
if [ -d "$ROOT/tools/node/bin" ]; then
  case ":$PATH:" in
    *":$ROOT/tools/node/bin:"*) : ;;
    *) PATH="$ROOT/tools/node/bin:$PATH" ;;
  esac
  export PATH
fi

MODE="${1:-serve}"
if [ "$#" -gt 0 ]; then shift; fi

case "$MODE" in
  dev|api|development) MODE="dev" ;;
  serve|prod|production) MODE="serve" ;;
  *)
    echo "usage: $0 [dev|serve] [uvicorn extra args...]" >&2
    exit 2
    ;;
esac

# worker 数は SHERPA_UVICORN_WORKERS に一本化する（起動ガード _warn_multi_worker_chat_turns が
# 参照する唯一の真実源）。extra args で --workers/-w を後置すると uvicorn の後勝ちで実 worker だけ
# 増え、env を見る production 拒否ガードをすり抜けるため、ここで拒否する（2026-07-13-横断レビュー対応.md R4 RV HIGH）。
for _a in "$@"; do
  case "$_a" in
    --workers|--workers=*|-w)
      echo "error: --workers/-w は extra args で渡せません。worker 数は SHERPA_UVICORN_WORKERS で指定してください" >&2
      echo "       （env を見る起動ガードのすり抜け防止・複数 worker は chat_turns/ratelimit が非共有）。" >&2
      exit 2
      ;;
  esac
done

# .env を取り込む。読み方は scripts/run-common.sh に一本化（呼び出し側の明示指定 ＞ .env ＞ 既定）。
# 以前は SHERPA_HOST/SHERPA_PORT の 2 変数だけを退避して .env の上書きから守っていたが、他の変数は
# .env が明示指定に勝つ状態だった。sherpa_source_dotenv は「呼び出し前から環境にあった変数は上書きしない」
# を全変数に適用する（例: `LAN=1 ./scripts/start.sh` の SHERPA_HOST=0.0.0.0 は .env の 127.0.0.1 に負けない）。
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
ENV_FILE="$(sherpa_dotenv_file)"
if [ -f "$ENV_FILE" ]; then
  sherpa_source_dotenv "$ENV_FILE"
elif [ "${SHERPA_REQUIRE_ENV_FILE:-0}" = "1" ]; then
  echo "missing env file: $ENV_FILE" >&2
  exit 1
fi

if [ "$MODE" = "dev" ]; then
  export SHERPA_ENV="development"
  export SHERPA_USE_FIXTURES="1"
else
  export SHERPA_ENV="production"
  unset SHERPA_USE_FIXTURES
fi

export PYTHONUNBUFFERED="${PYTHONUNBUFFERED:-1}"
export PYTHONDONTWRITEBYTECODE="${PYTHONDONTWRITEBYTECODE:-1}"

# Python は .venv を正とする（Makefile の PY と同じ規約）。以前は python3（システム）に落ちていたため、
# `make serve` / `make api` を直接叩くと依存の無い python で uvicorn を起動しようとして転んでいた。
if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "${SHERPA_VENV:-$ROOT/.venv}/bin/python" ]; then PYTHON_BIN="${SHERPA_VENV:-$ROOT/.venv}/bin/python"; else PYTHON_BIN="python3"; fi
fi
APP="${SHERPA_ASGI_APP:-sherpa.api:app}"
HOST="${SHERPA_HOST:-127.0.0.1}"
PORT="${SHERPA_PORT:-8000}"
WORKERS="${SHERPA_UVICORN_WORKERS:-1}"

exec "$PYTHON_BIN" -m uvicorn "$APP" \
  --host "$HOST" \
  --port "$PORT" \
  --workers "$WORKERS" \
  "$@"
