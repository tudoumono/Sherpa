#!/usr/bin/env bash
# Sherpa MVP — bootstrap: ローカル利用ディレクトリ作成・.env 用意・ストア起動待ち。
# 前提: `make up` でストアを起動済み（または本スクリプトが待つ）。
set -euo pipefail
ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# .env を用意（無ければ雛形から）
[ -f .env ] || { cp .env.example .env; echo "created .env from .env.example (set the AI key in the admin UI)"; }

# .env の非 secret 設定（SHERPA_VERSION など）を反映（読み方は scripts/run-common.sh に一本化:
# 呼び出し側の明示指定 ＞ .env ＞ 既定・SHERPA_ENV_FILE 対応）。
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_source_dotenv

# ローカルデータディレクトリ（本番は /srv/sherpa）。kb は読み取り専用運用、workspace は書込可。
VER="${SHERPA_VERSION:-v1}"
UID_="${SHERPA_UID:-admin}"
mkdir -p "data/kb/uploads/$VER" "data/kb/md/$VER" "data/kb/src/$VER"
mkdir -p "data/users/$UID_/workspace/outputs" "data/users/$UID_/workspace/tmp"
echo "dirs ready:"
echo "  data/kb/{uploads,md,src}/$VER   (read-only KB)"
echo "  data/users/$UID_/workspace       (writable)"

# curl の存在確認（下のストア待ちで使う）
if ! command -v curl >/dev/null 2>&1; then
  cat >&2 <<'EOF'
✗ curl が見つかりません（ストアの起動確認に使います）。
  Ubuntu / WSL2 での導入例:
    sudo apt update && sudo apt install -y curl
EOF
  exit 1
fi

# ストア起動待ち（無限ループにせず、既定120秒でタイムアウト＝案内して終了）。
STORE_WAIT="${SHERPA_STORE_WAIT:-120}"
wait_for() {  # $1=表示名  $2...=生存確認コマンド
  local label="$1"; shift
  local deadline=$(( $(date +%s) + STORE_WAIT ))
  until "$@" >/dev/null 2>&1; do
    if [ "$(date +%s)" -ge "$deadline" ]; then
      cat >&2 <<EOF

✗ $label の起動待ちがタイムアウトしました（${STORE_WAIT}秒）。次を確認してください:
    make status        # ストアの状態
    make logs          # ログ（アプリ＋ストアを1画面に合流。絞り込みは make logs ARGS="postgres"）
    docker ps          # コンテナが起動しているか
  ・Docker Desktop / Docker Engine が起動しているか確認してください。
  ・ポート競合が無いか確認してください（PostgreSQL 5432 / Elasticsearch 9200 / Neo4j 7474・7687）。
    既存サービスと衝突する場合は .env で変更できます（PGPORT / SHERPA_ES_PORT /
    SHERPA_NEO4J_HTTP_PORT / SHERPA_NEO4J_BOLT_PORT の1か所。アプリ側も同じ変数を見るので URL の書き換えは不要）。
  待ち時間を延ばすには SHERPA_STORE_WAIT=300 を指定して再実行できます。
EOF
      exit 1
    fi
    sleep 1
  done
}

echo "waiting for stores (postgres / es / neo4j) ... (最大 ${STORE_WAIT}秒)"
wait_for "PostgreSQL" sherpa_compose exec -T postgres pg_isready -U sherpa
wait_for "Elasticsearch" curl -sf "http://localhost:${SHERPA_ES_PORT:-9200}"
wait_for "Neo4j" curl -sf "http://localhost:${SHERPA_NEO4J_HTTP_PORT:-7474}"
echo "stores up. seed admin applied via initdb (users: admin@sherpa.local)."
echo "ready."
