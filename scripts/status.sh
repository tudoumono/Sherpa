#!/usr/bin/env bash
# サーバーを構成するストア・プロセス・メモリの状態を1画面で表示する。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"

# 日本語を2桁、ASCIIを1桁として埋める。表示名は日本語・ASCIIだけの閉じた一覧。
display_width() {  # ロケールに依らず、UTF-8 の後続バイトを除いた数を文字数にする
  local text="$1" chars bytes cont
  bytes="$(printf '%s' "$text" | LC_ALL=C wc -c | tr -d ' ')"
  cont="$(printf '%s' "$text" | LC_ALL=C tr -cd '\200-\277' | LC_ALL=C wc -c | tr -d ' ')"
  chars=$((bytes - cont))
  printf '%s\n' $((chars + (bytes - chars) / 2))
}

pad() {  # $1=文字列 $2=表示幅
  local text="$1" target="$2" width spaces
  width="$(display_width "$text")"
  spaces=$((target - width))
  [ "$spaces" -lt 1 ] && spaces=1
  printf '%s%*s' "$text" "$spaces" ''
}

status_row() {  # $1=印 $2=名前 $3=説明
  printf '  '
  pad "$1" 9
  pad "$2" 22
  printf '%s\n' "$3"
}

memory_row() {  # $1=項目 $2=値
  printf '  '
  pad "$1" 32
  printf '%s\n' "$2"
}

set_store_result() {  # $1=変数の接頭辞 $2=印 $3=説明
  printf -v "${1}_mark" '%s' "$2"
  printf -v "${1}_desc" '%s' "$3"
}

probe_store() {  # $1=変数の接頭辞 $2=composeサービス名
  local prefix="$1" svc="$2" cid state health
  cid="$(sherpa_compose ps -q "$svc" 2>/dev/null || true)"
  if [ -z "$cid" ]; then
    set_store_result "$prefix" "[停止]" "コンテナが起動していません"
    return
  fi
  state="$(docker inspect -f '{{.State.Status}}' "$cid" 2>/dev/null || echo unknown)"
  health="$(docker inspect -f '{{if .State.Health}}{{.State.Health.Status}}{{else}}-{{end}}' "$cid" 2>/dev/null || echo -)"
  if [ "$state" = "running" ] && { [ "$health" = "healthy" ] || [ "$health" = "-" ]; }; then
    set_store_result "$prefix" "[OK]" "動いています"
  else
    set_store_result "$prefix" "[注意]" "動いていますが、準備が整っていません"
  fi
}

postgres_mark="[不明]"; postgres_desc="状態を確認できません"
elasticsearch_mark="[不明]"; elasticsearch_desc="状態を確認できません"
neo4j_mark="[不明]"; neo4j_desc="状態を確認できません"
if ! command -v docker >/dev/null 2>&1 || ! docker compose version >/dev/null 2>&1; then
  postgres_desc="Dockerを使えません"; elasticsearch_desc="$postgres_desc"; neo4j_desc="$postgres_desc"
elif ! docker info >/dev/null 2>&1; then
  postgres_mark="[停止]"; elasticsearch_mark="[停止]"; neo4j_mark="[停止]"
  postgres_desc="Dockerが起動していません"; elasticsearch_desc="$postgres_desc"; neo4j_desc="$postgres_desc"
else
  probe_store postgres postgres
  probe_store elasticsearch elasticsearch
  probe_store neo4j neo4j
fi

app_mark="[停止]"; app_desc="未起動"; app_pid=""
if app_pid="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE")"; then
  if healthz_ok; then
    app_mark="[OK]"; app_desc="動いています（プロセス番号 ${app_pid}・応答あり）"
  else
    app_mark="[注意]"; app_desc="動いていますが応答がありません（プロセス番号 ${app_pid}）"
  fi
else
  app_rc=$?
  if [ "$app_rc" = 2 ]; then
    app_desc="古いプロセス番号が残っています（make stopで整理できます）"
  elif [ -f "$APP_PID_FILE" ]; then
    app_desc="プロセス番号の記録だけが残っています"
  fi
fi

worker_mark="[停止]"; worker_desc="未起動"; worker_pid=""; active_world=""
if worker_pid="$(live_matching_pid "$WORKER_PID_FILE" "$WORKER_PROC_NEEDLE")"; then
  worker_mark="[OK]"; worker_desc="動いています（プロセス番号 ${worker_pid}）"
  if [ -f "$WORKER_STATE_FILE" ]; then
    active_world="$(awk -F '\t' '$1 == "run" {print $2; exit}' "$WORKER_STATE_FILE" 2>/dev/null || true)"
  fi
else
  worker_rc=$?
  if [ "$worker_rc" = 2 ]; then
    worker_desc="古いプロセス番号が残っています（make stopで整理できます）"
  fi
fi

sherpa_env_default SHERPA_LAN
LAN="${LAN:-${SHERPA_LAN:-1}}"
caddy_mark="[停止]"; caddy_desc="未起動"
if [ "$LAN" != 1 ]; then
  caddy_mark="[未使用]"; caddy_desc="使っていない"
elif caddy_pid="$(live_matching_pid "$CADDY_PID_FILE" "$CADDY_PROC_NEEDLE")"; then
  caddy_mark="[OK]"; caddy_desc="動いています（プロセス番号 ${caddy_pid}）"
else
  caddy_rc=$?
  if [ "$caddy_rc" = 2 ]; then
    caddy_desc="古いプロセス番号が残っています（make stopで整理できます）"
  elif [ -f "$CADDY_PID_FILE" ]; then
    caddy_desc="プロセス番号の記録だけが残っています"
  fi
fi

PYTHON_BIN="${SHERPA_VENV:-$ROOT/.venv}/bin/python"
[ -x "$PYTHON_BIN" ] || PYTHON_BIN="${PYTHON_BIN_FALLBACK:-python3}"
ocr_probe="$(
"$PYTHON_BIN" - 2>/dev/null <<'PY' || printf 'ERROR'
import os
import signal

os.environ.update({
    "PGCONNECT_TIMEOUT": "2",
    "PGOPTIONS": "-c statement_timeout=2000",
    "SHERPA_PG_POOL_TIMEOUT": "2",
})


def _expired(_signum, _frame):
    raise TimeoutError


try:
    from sherpa.ingest import ocr_worker
    from sherpa.store import ocr_jobs
    signal.signal(signal.SIGALRM, _expired)
    signal.setitimer(signal.ITIMER_REAL, 3)
    rows = ocr_jobs.worker_availability_summary(ocr_worker.profile_hash())["workers"] or []
    signal.setitimer(signal.ITIMER_REAL, 0)
except Exception:
    print("ERROR")
else:
    available = sum(1 for row in rows if row.get("available") is True)
    unavailable = len(rows) - available
    if not rows:
        print("NONE")
    elif unavailable:
        print(f"WARN|動作可能 {available}台・確認が必要 {unavailable}台")
    else:
        print(f"OK|{available}台が動作可能です")
PY
)"
case "$ocr_probe" in
  OK\|*) ocr_mark="[OK]"; ocr_desc="${ocr_probe#OK|}" ;;
  WARN\|*) ocr_mark="[注意]"; ocr_desc="${ocr_probe#WARN|}" ;;
  NONE) ocr_mark="[停止]"; ocr_desc="未起動" ;;
  *) ocr_mark="[不明]"; ocr_desc="状態を確認できません" ;;
esac

down_names=""
add_down() { down_names="${down_names}${down_names:+・}$1"; }
case "$postgres_mark" in "[OK]") ;; *) add_down "PostgreSQL" ;; esac
case "$elasticsearch_mark" in "[OK]") ;; *) add_down "Elasticsearch" ;; esac
case "$neo4j_mark" in "[OK]") ;; *) add_down "Neo4j" ;; esac
case "$app_mark" in "[OK]") ;; *) add_down "アプリ" ;; esac
case "$worker_mark" in "[OK]") ;; *) add_down "取り込みワーカー" ;; esac
case "$caddy_mark" in "[OK]"|"[未使用]") ;; *) add_down "Caddy" ;; esac
case "$ocr_mark" in "[OK]") ;; *) add_down "OCRワーカー" ;; esac

if [ -z "$down_names" ]; then
  echo "すべて動いています"
else
  echo "止まっているもの: $down_names"
fi

echo ""
echo "=== サーバー ==="
status_row "$postgres_mark" "PostgreSQL" "$postgres_desc"
status_row "$elasticsearch_mark" "Elasticsearch" "$elasticsearch_desc"
status_row "$neo4j_mark" "Neo4j" "$neo4j_desc"
status_row "$app_mark" "アプリ" "$app_desc"
status_row "$worker_mark" "取り込みワーカー" "$worker_desc"
status_row "$caddy_mark" "Caddy" "$caddy_desc"
status_row "$ocr_mark" "OCRワーカー" "$ocr_desc"
status_row "[情報]" "開く場所" "$CHAT_URL"

if [ -n "$active_world" ]; then
  echo ""
  echo "取り込み中: ${active_world}（詳しくは make ingest-status）"
fi

# shellcheck source=scripts/lib/mem_budget.sh
. "$ROOT/scripts/lib/mem_budget.sh"
mem_budget_load_env
mem_budget_detect
mem_budget_compute "$MB_TOTAL_DETECTED"
echo ""
echo "=== メモリの割り振り ==="
memory_row "項目" "割り振り"
memory_row "全体" "$(awk -v v="$MB_TOTAL" 'BEGIN {printf "%.1f GB", v/1024}')"
memory_row "OSと余裕" "$(awk -v v="$MB_OS" 'BEGIN {printf "%.1f GB", v/1024}')"
memory_row "Elasticsearchの作業用" "$(awk -v v="$MB_ES_HEAP" 'BEGIN {printf "%.2f GB", v/1024}')"
memory_row "Elasticsearchの上限" "$(awk -v v="$MB_ES_LIMIT" 'BEGIN {printf "%.2f GB", v/1024}')"
memory_row "Neo4jの作業用" "$(awk -v v="$MB_NEO_HEAP" 'BEGIN {printf "%.1f GB", v/1024}')"
memory_row "Neo4jの一時保存" "$(awk -v v="$MB_NEO_PC" 'BEGIN {printf "%.1f GB", v/1024}')"
if [ "$MB_NEO_TX" = 0 ]; then
  memory_row "Neo4jの1回の書き込み" "上限なし"
else
  memory_row "Neo4jの1回の書き込み" "$(awk -v v="$MB_NEO_TX" 'BEGIN {printf "%.1f GB", v/1024}')"
fi
memory_row "LibreOffice 1本" "$(awk -v v="$MB_LO" 'BEGIN {printf "%.1f GB", v/1024}')"
memory_row "OCRワーカーの上限" "$(awk -v v="$MB_OCR" 'BEGIN {printf "%.1f GB", v/1024}')"
memory_row "残り" "$(awk -v v="$MB_REST" 'BEGIN {printf "%.1f GB", v/1024}')"
echo "  注意: 残りはアプリ・取り込みワーカー・LibreOffice・PostgreSQLで使う目安です。"
if [ "$MB_REST" -lt 4096 ]; then
  echo "  注意: 残りが少ないため、メモリを増やすかOCRを止めてください。"
fi
if [ -n "$MB_NEO_NOTE" ]; then
  echo "  注意: 読めない設定を無視しました: ${MB_NEO_NOTE}（4g・4096mのように単位を付けてください）。"
fi

# OCRイメージのコードが現在のアプリと違うときだけ、案内へ注意を足す。
ocr_code_note=""
_ocr_img_ver="$(docker image inspect -f '{{index .Config.Labels "sherpa.code-version"}}' sherpa/ocr-worker:paddleocr-3.7.0-cpu 2>/dev/null || true)"
_app_ver="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || true)"
if [ -n "$_ocr_img_ver" ] && [ "$_ocr_img_ver" != "<no value>" ]; then
  _app_commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || true)"
  _cmp_img="${_ocr_img_ver%%+*}"; _cmp_app="${_app_ver%%+*}"
  if [ "$_ocr_img_ver" != "$_cmp_img" ]; then
    case "$_app_ver" in
      *+*) _cmp_img="$_ocr_img_ver"; _cmp_app="$_app_ver" ;;
      *) [ -n "$_app_commit" ] && { _cmp_img="$_ocr_img_ver"; _cmp_app="$_app_ver+$_app_commit"; } ;;
    esac
  fi
  if [ "$_cmp_img" != "$_cmp_app" ]; then
    ocr_code_note="ワーカーのコードが古い: ${_ocr_img_ver}（アプリは ${_app_ver}） → make up"
  fi
elif docker image inspect sherpa/ocr-worker:paddleocr-3.7.0-cpu >/dev/null 2>&1; then
  ocr_code_note="ワーカーのコードの版が不明です → make up"
fi

echo ""
echo "=== 案内 ==="
case "$postgres_mark$elasticsearch_mark$neo4j_mark" in
  "[OK][OK][OK]") ;;
  *) echo "  ストアを起動: make up（ログ: make logs）" ;;
esac
if [ "$app_mark" != "[OK]" ]; then
  echo "  全部を起動: make start"
elif [ "$worker_mark" != "[OK]" ]; then
  echo "  取り込みワーカーを起動: make restart-worker"
fi
[ "$caddy_mark" != "[停止]" ] || echo "  LAN公開を起動し直す: LAN=1 make start"
[ "$ocr_mark" = "[OK]" ] || echo "  OCRワーカーを起動: make up（モデルの準備: make ocr-models）"
[ -z "$ocr_code_note" ] || echo "  $ocr_code_note"
if [ -z "$down_names" ] && [ -z "$ocr_code_note" ]; then
  echo "  起動が必要なものはありません。"
fi
