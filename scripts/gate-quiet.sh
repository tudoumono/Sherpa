#!/bin/bash
# 静かなゲート: 並走 pytest ゼロの状態で全スイートを順に流す（共有 dev DB の競合フレーク＝台帳 #19〜#22 を排した最終判定）。
# 使い方: scripts/gate-quiet.sh [--reset-db] [ログファイル]   （既定: $TMPDIR または /tmp 直下・リポジトリ内に書かない）
#   --reset-db  流す前に sherpa_test（既定名）を DROP→CREATE で初期化する（台帳 #23＝テストDB肥大対策）。
#               明示 DSN（SHERPA_TEST_PG_DSN）と併用すると実際に使う DB と初期化対象がずれるため拒否する。
# 契約: 他の pytest が動いていれば開始しない（fail-loud）。gate-lane.sh の2本のレーンスロット＋
# gate-integration.sh の専用ロック（scripts/lib/gate_common.sh::gate_acquire_all_or_none）を
# **全部**非ブロッキングで取り、1つでも取れなければ同様に開始しない（全部取れたら実行終了まで
# 保持し、他ゲートの割り込みを防ぐ＝「並走 pytest ゼロ」を保証する）。各群は timeout（INT）で
# 打ち切り＝完了後の終了ハング（台帳 #22）を吸収。INT/TERM は実行中のテストプロセスグループへ
# 転送し、全滅を待ってから終了する（保持しているロック fd は子へ継承させない）。
set -u
cd "$(dirname "$0")/.."
SELF_ROOT="$(pwd)"
# shellcheck source=scripts/lib/gate_common.sh
. "$SELF_ROOT/scripts/lib/gate_common.sh"

RESET_DB=0
ARGS=()
for a in "$@"; do
  case "$a" in
    --reset-db) RESET_DB=1 ;;
    *) ARGS+=("$a") ;;
  esac
done
log=${ARGS[0]:-${TMPDIR:-/tmp}/sherpa-gate-quiet-$(date +%Y%m%d-%H%M%S).log}
: > "$log"   # 固定パスを外から指定された場合の前回実行分混入を防ぐ（以後は tee -a で追記）

if [ "$RESET_DB" = 1 ] && [ -n "${SHERPA_TEST_PG_DSN:-}" ]; then
  echo "--reset-db と SHERPA_TEST_PG_DSN の併用はできません（--reset-db は既定名 sherpa_test を" >&2
  echo "初期化しますが、明示 DSN が指すのは別の DB のため、初期化と実行対象がずれます）" >&2
  exit 2
fi

if pgrep -f "python -m pytest" | grep -qv "^$$\$"; then
  echo "他の pytest が実行中のため開始しません:" >&2; pgrep -af "python -m pytest" >&2; exit 2
fi

if ! gate_acquire_all_or_none; then
  echo "他のゲート（lane/integration）が実行中のため開始しません（ロック: ${GATE_LANE_SLOT_LOCKS[*]} $GATE_INTEGRATION_LOCKFILE）" >&2
  exit 2
fi

PY=".venv/bin/python"
if [ "$RESET_DB" = 1 ]; then
  echo "=== sherpa_test を初期化 ($(date +%H:%M))"
  "$PY" scripts/test_db_reset.py || exit 1
fi
export SHERPA_USE_FIXTURES=1; unset OPENAI_BASE_URL CODEX_HOME
GROUP_TIMEOUT=${GATE_GROUP_TIMEOUT:-45m}
GATE_HELD_FDS=("${GATE_QUIET_FDS[@]}")
GATE_LOG_FILE="$log"
trap 'gate_handle_signal TERM' TERM
trap 'gate_handle_signal INT' INT
run(){ gate_run_group "$@"; }

run unit tests/unit
run contract tests/contract
run api-a tests/api/test_a*.py tests/api/test_c*.py
run api-b tests/api/test_[d-r]*.py
run api-st tests/api/test_[s-t]*.py
run api-uz tests/api/test_[u-z]*.py
run e2e tests/e2e
echo "=== DONE ($(date +%H:%M))" | tee -a "$log"
