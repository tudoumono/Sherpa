#!/usr/bin/env bash
# 取り込みワーカーの見張り: ワーカー（python -m sherpa.ingest.ingest_worker）を起動し、終了したらすぐ次を起動し直す。
# ワーカーは依頼を 1 件処理するたびに終了する（メモリを OS へ全部戻すため）ので、ここが起動し直し続ける。
# 異常終了（終了コードが 0 以外）が続くときは、起動し直す間隔を 2・4・8…秒と空ける（上限 60 秒）。
#
# Usage:
#   ./scripts/run-ingest-worker.sh serve   # production（既定）
#   ./scripts/run-ingest-worker.sh dev     # development: fixtures enabled
#
# SIGTERM／SIGINT を受けたらワーカーに SIGTERM を渡し、処理中の 1 件が終わるまで待ってから終了する。
# 優先度は nice -n ${SHERPA_INGEST_NICE}（既定 10）。先読み・LibreOffice などの子プロセスも引き継ぐ。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/lib/install_state.sh
. "$ROOT/scripts/lib/install_state.sh"
if ! pkg_install_guard; then
  exit 78
fi

MODE="${1:-serve}"
case "$MODE" in
  dev|api|development) MODE="dev" ;;
  serve|prod|production) MODE="serve" ;;
  *)
    echo "usage: $0 [dev|serve]" >&2
    exit 2
    ;;
esac

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
export MALLOC_ARENA_MAX="${MALLOC_ARENA_MAX:-2}"

if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "${SHERPA_VENV:-$ROOT/.venv}/bin/python" ]; then PYTHON_BIN="${SHERPA_VENV:-$ROOT/.venv}/bin/python"; else PYTHON_BIN="python3"; fi
fi

# 取り込みの専用ログ（MD 化・LibreOffice）はこのスクリプトの起動時に退避する（ワーカーは 1 件ごとに起動し直すので、
# ワーカー自身は退避しない。Web アプリの起動はこれらに触れない）。
sherpa_rotate_log "$RUN_DIR/convert.log"
sherpa_rotate_log "$RUN_DIR/libreoffice.log"

NICE="${SHERPA_INGEST_NICE:-10}"
case "$NICE" in ''|*[!0-9]*) NICE=10 ;; esac
if [ "$NICE" -gt 19 ]; then NICE=19; fi

stopping=0
child=0
on_term() {
  stopping=1
  if [ "$child" -gt 0 ]; then kill -TERM "$child" 2>/dev/null || true; fi
}
trap on_term TERM INT

started_version="$(cat "$ROOT/VERSION" 2>/dev/null || true)"
fails=0
while [ "$stopping" = 0 ]; do
  # 見張りの起動後にファイルが別の版へ置き換えられたら、新しいワーカーを起動しない（版の混ざったコードで動かさない）。
  now_version="$(cat "$ROOT/VERSION" 2>/dev/null || true)"
  if [ "$now_version" != "$started_version" ]; then
    echo "Sherpa のファイルが別の版（${now_version:-不明}）に置き換えられたため、取り込みワーカーの見張りを終えます（起動時の版: ${started_version:-不明}）。make restart で起動し直してください。" >&2
    break
  fi
  # 前のワーカーが突然消えた（SIGKILL・OOM・停電）ときに残った子（先読み・soffice）を、新しいワーカーの前に片付ける。
  reaped="$(sherpa_reap_ingest_children)"
  if [ "${reaped:-0}" != 0 ]; then echo "前のワーカーが残した子プロセス ${reaped} 件を止めました。" >&2; fi
  nice -n "$NICE" "$PYTHON_BIN" -m sherpa.ingest.ingest_worker &
  child=$!
  rc=0
  while :; do
    wait "$child"
    rc=$?
    kill -0 "$child" 2>/dev/null || break
  done
  child=0
  [ "$stopping" = 0 ] || break
  if [ "$rc" -eq 0 ]; then
    fails=0
  else
    fails=$(( fails + 1 ))
    delay=$(( 1 << (fails < 6 ? fails : 6) ))
    if [ "$delay" -gt 60 ]; then delay=60; fi
    echo "取り込みワーカーが異常終了しました（終了コード ${rc}）。${delay} 秒後に起動し直します。" >&2
    sleep "$delay" &
    sp=$!
    wait "$sp" || true
    kill "$sp" 2>/dev/null || true
  fi
done
