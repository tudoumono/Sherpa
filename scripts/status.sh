#!/usr/bin/env bash
# Sherpa の状態を1画面で表示する: ストア3つ・アプリ（pid＋healthz）・LAN/Caddy・URL。
# 落ちているものには起動コマンドを添えます。
set -uo pipefail   # 状態表示なので -e は付けない（一部が落ちていても最後まで表示する）

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"

# ストア1件の状態（docker inspect で state/health を見る）。
store_line() {  # $1=表示名  $2=compose サービス名
  local label="$1" svc="$2" cid state health
  cid="$(sherpa_compose ps -q "$svc" 2>/dev/null || true)"
  if [ -z "$cid" ]; then
    printf '  [停止] %-14s : コンテナ未起動\n' "$label"
    stores_down=1
    return
  fi
  state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$cid" 2>/dev/null || echo -)"
  if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "-" ]; }; then
    printf '  [OK]   %-14s : running (health=%s)\n' "$label" "$health"
  else
    printf '  [注意] %-14s : state=%s health=%s\n' "$label" "$state" "$health"
    stores_down=1
  fi
}

stores_down=0
echo "=== ストア（Docker） ==="
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  echo "  [不明] docker / docker compose が使えません（導入: make install-docker）"
  stores_down=1
elif ! docker info >/dev/null 2>&1; then
  echo "  [停止] Docker が起動していません（Docker Desktop / dockerd を起動してください）"
  stores_down=1
else
  store_line "PostgreSQL"    "postgres"
  store_line "Elasticsearch" "elasticsearch"
  store_line "Neo4j"         "neo4j"
fi

echo ""
echo "=== アプリ（FastAPI） ==="
app_down=0
if pid="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE")"; then
  if healthz_ok; then
    printf '  [OK]   uvicorn        : pid %s ・healthz 応答あり\n' "$pid"
  else
    printf '  [注意] uvicorn        : pid %s は生存するが healthz 未応答（起動途中/不調）\n' "$pid"
    app_down=1
  fi
else
  rc=$?
  if [ "$rc" = 2 ]; then
    echo "  [停止] uvicorn        : pid ファイルが古い（pid 再利用の疑い・掃除は make stop で）"
  elif [ -f "$APP_PID_FILE" ]; then
    echo "  [停止] uvicorn        : pid ファイルの残骸あり（プロセスは死亡）"
  else
    echo "  [停止] uvicorn        : 未起動"
  fi
  app_down=1
fi
printf '  URL(ローカル)         : %s\n' "$CHAT_URL"

echo ""
echo "=== LAN / Caddy ==="
if pid="$(live_matching_pid "$CADDY_PID_FILE" "$CADDY_PROC_NEEDLE")"; then
  hn="$(hostname 2>/dev/null || echo localhost)"
  printf '  [OK]   Caddy          : pid %s ・https://%s/ui/chat.html\n' "$pid" "$hn"
else
  rc=$?
  if [ "$rc" = 2 ]; then
    echo "  [停止] Caddy          : pid ファイルが古い（pid 再利用の疑い・掃除は make stop で）"
  elif [ -f "$CADDY_PID_FILE" ]; then
    echo "  [停止] Caddy          : pid ファイルの残骸あり（プロセスは死亡）"
  else
    echo "  [停止] Caddy          : 未起動（LAN 公開時のみ・LAN=1 で起動）"
  fi
fi

echo ""
echo "=== 画像内文字の読み取り（OCR） ==="
# 「動いているか」の権威はワーカー自身が DB へ書く心拍。コンテナが起動していても、モデルが
# 揃っていなければ読み取りはできないため、コンテナの有無ではなく心拍の中身を見る。
ocr_row="$("$ROOT/.venv/bin/python" - <<'PY' 2>/dev/null || true
try:
    from sherpa.ingest import ocr_worker          # profile_hash だけ使う（Paddle は import しない）
    from sherpa.store import ocr_jobs
    rows = ocr_jobs.worker_availability_summary(ocr_worker.profile_hash())["workers"]
except Exception:
    rows = []
for row in rows or []:
    print("|".join(str(row.get(k, "")) for k in
                   ("worker_id", "status", "available", "unavailable_reason", "last_seen_at")))
PY
)"
if [ -n "$ocr_row" ]; then
  while IFS='|' read -r wid wstatus wavail wreason wseen; do
    [ -n "$wid" ] || continue
    if [ "$wavail" = "True" ]; then
      printf '  [OK]   ワーカー        : %s（%s・最終応答 %s）\n' "$wid" "$wstatus" "${wseen%%.*}"
    else
      printf '  [NG]   ワーカー        : %s（%s・理由 %s）\n' "$wid" "$wstatus" "${wreason:-不明}"
    fi
  done <<< "$ocr_row"
else
  echo "  [停止] ワーカー        : 未起動（make up で起動・モデル未取得なら make ocr-models）"
fi

# 落ちているものへの案内。
if [ "$stores_down" != 0 ] || [ "$app_down" != 0 ]; then
  echo ""
  echo "── 起動していないものがあります ──"
  [ "$stores_down" != 0 ] && echo "  ストア起動:  make up   （ログ: make logs）"
  [ "$app_down" != 0 ]    && echo "  全部起動:    make start （初回セットアップ込み）"
fi
