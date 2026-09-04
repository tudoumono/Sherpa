#!/bin/bash
# TEST-3: レーンごとに Postgres DB（sherpa_test_<lane>）と Neo4j/ES の world（`pytest-<lane>`・
# tests/_world_setup.py 参照）を分離し、unit→contract→api→e2e を1レーンとして実行する。
# tests/integration は含めない（scripts/gate-integration.sh が別入口・固定 world_id に依存する
# テストが残るため独立の直列キューを維持する）。
#
# 使い方:
#   scripts/gate-lane.sh <worktree> <lane> [--keep-db] [--only <pytest引数...>]
#     <worktree>  対象 worktree の絶対/相対パス（その sherpa/・tests/ を使う。test_db_reset.py は
#                 対象 worktree 側ではなく、この gate-lane.sh 自身と同じ版を常に使う）
#     <lane>      DB名／world id サフィックス（[a-z0-9]{1,51}）。sherpa_test_<lane>（PG）と
#                 pytest-<lane>（Neo4j/ES world）を作って使う
#     --keep-db   終了時にレーン DB を DROP しない（調査用に残す・既定は後始末する）
#     --only ARGS tests/unit・tests/contract・tests/api・tests/e2e 配下のみ許可（群分けせず
#                 指定した pytest 引数だけを1回実行する・動作確認・dry-run 用）。tests/integration
#                 や tests 全体は拒否する（それらのレーン分離契約が別なため）。`-q` は下の run() が
#                 既に付与するため ARGS 側には書かなくてよい（書いても `-qq` にならないよう自動で
#                 除去する）。
#
# 例:
#   scripts/gate-lane.sh /home/tudo/projects/Sherpa-h2 h2
#   scripts/gate-lane.sh /home/tudo/projects/Sherpa-test1 smoke --only tests/unit/test_layer.py
#
# 契約: 同一レーン名の二重起動はレーン別ロック（/tmp/sherpa-gate-<lane>.lock・ブロッキング・
# gate-integration.sh とも共有＝同名レーンを lane/integration 両入口から同時に起動できない）で
# 排他する。同時実行数は2本のスロット（scripts/lib/gate_common.sh::GATE_LANE_SLOT_LOCKS・
# gate-integration.sh の実行とも合計で共有）を上限とし、空くまでブロッキングで待つ（どちらも
# fail-closed で即終了はしない＝待つ）。reset 前に両方を取得し、cleanup 完了まで保持する。
# INT/TERM は実行中のテストプロセスグループへ転送し、全滅を待ってから後始末する。
set -u
cd "$(dirname "$0")/.." || exit 1
SELF_ROOT="$(pwd)"
# shellcheck source=scripts/lib/gate_common.sh
. "$SELF_ROOT/scripts/lib/gate_common.sh"

usage() { echo "使い方: scripts/gate-lane.sh <worktree> <lane> [--keep-db] [--only <pytest引数...>]" >&2; }

WORKTREE=${1:-}
LANE=${2:-}
if [ -z "$WORKTREE" ] || [ -z "$LANE" ]; then usage; exit 2; fi
shift 2

KEEP_DB=0
ONLY_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --keep-db) KEEP_DB=1; shift ;;
    --only)
      shift
      if [ $# -eq 0 ]; then echo "--only には pytest 引数が必要です" >&2; usage; exit 2; fi
      ONLY_ARGS=("$@"); break ;;
    *) echo "不明な引数: $1" >&2; usage; exit 2 ;;
  esac
done

# run() が -q を必ず付与するため、--only 側にも -q があると -qq になり合否サマリ行が消える
# （grep ベースの抽出が壊れる）。利用者が誤って重ねても安全なようにここで除去する。
if [ "${#ONLY_ARGS[@]}" -gt 0 ]; then
  _FILTERED_ONLY_ARGS=()
  for _a in "${ONLY_ARGS[@]}"; do
    [ "$_a" = "-q" ] && continue
    _FILTERED_ONLY_ARGS+=("$_a")
  done
  ONLY_ARGS=("${_FILTERED_ONLY_ARGS[@]}")
fi

# --only の境界（tests/unit・tests/contract・tests/api・tests/e2e 配下のみ・tests/integration や
# tests 全体は拒否）。worktree/venv/DB より先に検証する＝境界違反を即座に弾く。
if [ "${#ONLY_ARGS[@]}" -gt 0 ]; then
  if ! gate_validate_only_prefixes "tests/unit・tests/contract・tests/api・tests/e2e" \
      "tests/unit tests/contract tests/api tests/e2e" "${ONLY_ARGS[@]}"; then
    exit 2
  fi
fi

if ! validate_lane_name "$LANE"; then
  echo "lane は [a-z0-9]{1,51} のみ許可します（DB名 sherpa_test_<lane> に使うため）: $LANE" >&2
  exit 2
fi
[ -d "$WORKTREE" ] || { echo "worktree が見つかりません: $WORKTREE" >&2; exit 2; }
WORKTREE="$(cd "$WORKTREE" && pwd)"
DBNAME="sherpa_test_${LANE}"

PY="$(gate_resolve_venv_python "$WORKTREE")"
if [ ! -x "$PY" ]; then
  echo "venv の python が見つかりません（$PY）。worktree または主リポジトリに .venv を用意してください" >&2
  exit 2
fi

# --- レーン別ロック（同名二重起動の排他・ブロッキング・gate-integration.sh と共有） ----------
LANE_LOCKFILE="/tmp/sherpa-gate-${LANE}.lock"
echo "=== レーン $LANE: レーン別ロック待ち ($(date +%H:%M:%S))"
if ! gate_acquire_named_lock "$LANE_LOCKFILE"; then
  echo "レーン $LANE: レーン別ロックの取得に失敗しました（$LANE_LOCKFILE）" >&2
  exit 1
fi

# --- 同時実行数のスロット確保（上限2・gate-integration.sh と合計で共有・空くまでブロッキングで待つ） ---
echo "=== レーン $LANE: スロット待ち ($(date +%H:%M:%S))"
gate_acquire_lane_slot
echo "=== レーン $LANE: スロット ${GATE_SLOT_INDEX}/${#GATE_LANE_SLOT_LOCKS[@]} を確保 ($(date +%H:%M:%S))"
GATE_HELD_FDS=("$GATE_NAMED_LOCK_FD" "$GATE_SLOT_FD")

echo "=== レーン $LANE: DB $DBNAME を初期化 ($(date +%H:%M:%S))"
"$PY" "$SELF_ROOT/scripts/test_db_reset.py" --name "$DBNAME" || exit 1

LANE_DSN="$(gate_compute_lane_dsn "$PY" "$WORKTREE" "$DBNAME")" || exit 1
export SHERPA_TEST_PG_DSN="$LANE_DSN"
export SHERPA_TEST_WORLD_ID="pytest-${LANE}"   # tests/_world_setup.py::TEST_WORLD_ID を上書き＝Neo4j/ES の world をレーンごとに分離

cleanup() {
  if [ "$KEEP_DB" = 1 ]; then
    echo "=== レーン $LANE: --keep-db 指定のため $DBNAME を残します"
  else
    echo "=== レーン $LANE: DB $DBNAME を後始末 ($(date +%H:%M:%S))"
    "$PY" "$SELF_ROOT/scripts/test_db_reset.py" --name "$DBNAME" --drop-only || exit 1
  fi
}
trap cleanup EXIT
trap 'gate_handle_signal TERM' TERM
trap 'gate_handle_signal INT' INT

cd "$WORKTREE"
log=${SHERPA_GATE_LANE_LOG:-${TMPDIR:-/tmp}/sherpa-gate-lane-${LANE}-$(date +%Y%m%d-%H%M%S).log}
: > "$log"   # 固定パスを外から指定された場合の前回実行分混入を防ぐ（以後は tee -a で追記）
GATE_LOG_FILE="$log"
export SHERPA_USE_FIXTURES=1; unset OPENAI_BASE_URL CODEX_HOME
GROUP_TIMEOUT=${GATE_GROUP_TIMEOUT:-45m}
run(){ gate_run_group "$@"; }

echo "=== レーン $LANE (DB=$DBNAME) 開始 ($(date +%H:%M:%S))" | tee -a "$log"
TEST_FAIL=0
if [ "${#ONLY_ARGS[@]}" -gt 0 ]; then
  run only "${ONLY_ARGS[@]}" || TEST_FAIL=1
else
  run unit tests/unit || TEST_FAIL=1
  run contract tests/contract || TEST_FAIL=1
  run api-a tests/api/test_[a-c]*.py || TEST_FAIL=1
  run api-b tests/api/test_[d-r]*.py || TEST_FAIL=1
  run api-st tests/api/test_[s-t]*.py || TEST_FAIL=1
  run api-uz tests/api/test_[u-z]*.py || TEST_FAIL=1
  run e2e tests/e2e || TEST_FAIL=1
fi
echo "=== レーン $LANE DONE ($(date +%H:%M:%S))" | tee -a "$log"

echo "ログ: $log"
[ "$TEST_FAIL" = 0 ]
