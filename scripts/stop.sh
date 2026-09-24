#!/usr/bin/env bash
# Sherpa を全部停止する: アプリ（pid）→ Caddy（pid）→ ストア（docker compose down）の順。
#
#   ./scripts/stop.sh              # アプリ・Caddy・ストアを全部停止
#   KEEP_STORES=1 ./scripts/stop.sh  # ストア（DB/検索）は残し、アプリと Caddy だけ停止
#
# pid ファイルの残骸（プロセスは既に死亡）は掃除します。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"

KEEP_STORES="${KEEP_STORES:-0}"

# pid ファイルのプロセスを穏当に停止する（SIGTERM→数秒待って SIGKILL）。
# setsid 起動＝PID がプロセスグループ長のため、まずグループ全体に送る（子も巻き取る）。
# kill する前に pid の所有者を検証する（RV High-1）: 再起動等で pid が別プロセスに再利用されて
# いた場合、無関係なプロセス（グループ）を誤って殺さないよう、pid ファイルの掃除だけに留める。
stop_pid() {  # $1=表示名  $2=pidファイル  $3=コマンドライン照合用の部分文字列（needle）
  local label="$1" pf="$2" needle="$3" pid rc
  # set -e 下では非0を返す代入は if の条件式として評価しないとスクリプトごと終了してしまう
  # （bare statement は errexit の対象・if/while の条件だけが免除される）。
  if pid="$(live_matching_pid "$pf" "$needle")"; then
    rc=0
  else
    rc=$?
  fi
  case "$rc" in
    0)
      echo "$label を停止します（pid $pid）..."
      kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      local deadline=$(( $(date +%s) + ${SHERPA_STOP_WAIT:-10} ))
      while kill -0 "$pid" 2>/dev/null; do
        if [ "$(date +%s)" -ge "$deadline" ]; then
          echo "  応答しないため強制終了します（SIGKILL）。" >&2
          kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
          break
        fi
        sleep 1
      done
      rm -f "$pf"
      ;;
    2)
      echo "$label: pid ファイルが古いため掃除します（プロセスは触りません・pid 再利用の疑い）。"
      rm -f "$pf"
      ;;
    *)
      if [ -f "$pf" ]; then
        echo "$label は起動していません（pid ファイルの残骸を掃除）。"
        rm -f "$pf"
      else
        echo "$label は起動していません。"
      fi
      ;;
  esac
}

stop_pid "Sherpa アプリ" "$APP_PID_FILE" "$APP_PROC_NEEDLE"
stop_pid "Caddy" "$CADDY_PID_FILE" "$CADDY_PROC_NEEDLE"

if [ "$KEEP_STORES" = "1" ]; then
  echo "ストア（PostgreSQL / Neo4j / Elasticsearch）は KEEP_STORES=1 のため残します。"
elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker が起動していません（Docker Desktop / dockerd を起動してください）。ストア停止をスキップします。" >&2
else
  echo "ストア（PostgreSQL / Neo4j / Elasticsearch）を停止します..."
  sherpa_compose down
fi

echo "停止しました。"
