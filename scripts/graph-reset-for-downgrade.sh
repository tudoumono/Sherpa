#!/usr/bin/env bash
# 世代で絞らない旧版へ戻す前に流す（make graph-reset-for-downgrade）。
# 全部の資料フォルダのグラフ（Entity ノード）を小分けに消し、世代の印（SherpaMeta）も消す。
# 旧版の次の取り込みが、グラフを作り直す。資料フォルダの中身・ES・Postgres は触らない。
# 接続先は .env（SHERPA_ENV_FILE 対応）の NEO4J_URI / NEO4J_USER / NEO4J_PASSWORD。パスワードは表示しない。
set -uo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_env_default NEO4J_URI NEO4J_USER NEO4J_PASSWORD SHERPA_NEO4J_BOLT_PORT

if [ -z "${PYTHON_BIN:-}" ]; then
  if [ -x "${SHERPA_VENV:-$ROOT/.venv}/bin/python" ]; then PYTHON_BIN="${SHERPA_VENV:-$ROOT/.venv}/bin/python"; else PYTHON_BIN="python3"; fi
fi

TARGET_URI="$("$PYTHON_BIN" -c 'from sherpa.ingest import world_neo4j as w; print(w.default_neo4j_uri())')" || exit 1

echo "旧版へ戻す前の処理です。次を消します（戻せません）:"
echo "  接続先: $TARGET_URI"
echo "  全部の資料フォルダのグラフ（Entity ノード）と、グラフの世代の印（SherpaMeta）"
echo "消したあと、旧版の次の取り込みで全部の資料フォルダのグラフが作り直されます。"
printf "続けますか？ (y/N): "
read -r ANSWER
if [ "$ANSWER" != "y" ]; then
  echo "何も消していません。"
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
from sherpa.ingest import world_neo4j as w

env = w._env()
done = w.reset_graph_for_downgrade(env["uri"], env["user"], env["pw"])
print(f"消しました: ノード {done['entities']} 件・世代の印 {done['metas']} 件")
PY
