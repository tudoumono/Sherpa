#!/bin/bash
# TEST-3: integration（ES/Neo4j 実機）を専用の直列キューで実行する。tests/integration には
# 固定の hardcode world_id（`worlds.register()` を使う test_worlds_admin.py・test_multi_world.py
# 等・"test_world_admin" のような文字列リテラル）に依存するテストが多数残り、`_world_setup.py::
# TEST_WORLD_ID`（env 注入）を経由しないため lane 単位で world を分離できない——2本の integration
# を同時に走らせると同じ Neo4j/ES world を取り合って壊れる。よって integration の実行同士は
# 専用ロック（scripts/lib/gate_common.sh::GATE_INTEGRATION_LOCKFILE）で直列化する。
# 加えて、同時実行数の上限（全ゲート合計2本）は gate-lane.sh と共有のスロットで数える——
# integration の実行もスロットを1本消費する（PG はレーン専用 DB を使うため --lane は必須のまま）。
# 同名レーンの二重起動排他（/tmp/sherpa-gate-<lane>.lock）は gate-lane.sh と共有する。
#
# 使い方:
#   scripts/gate-integration.sh <worktree> --lane <lane> [--keep-db] [--only <pytest引数...>]
#     <worktree>  対象 worktree の絶対/相対パス
#     --lane      必須。sherpa_test_<lane>（PG）と pytest-<lane>（`_world_setup.py::TEST_WORLD_ID`
#                 経由のテストが使う Neo4j/ES world）を作って使う
#                 （共有 sherpa_test への暗黙 fallback はしない）
#     --keep-db   終了時にレーン DB を DROP しない（既定は後始末する）
#     --only ARGS tests/integration 配下のみ許可する。既定の tests/integration 全体でなく、
#                 指定した pytest 引数だけを実行する。`-q` は下の pytest 呼び出しが既に付与する
#                 ため ARGS 側には書かなくてよい（書いても `-qq` にならないよう自動で除去する）。
#
# 契約: レーン別ロック・専用ロックはいずれもブロッキング取得（取得できなければ fail-closed で
# 終了する）。reset 前に取得し、cleanup 完了まで保持する。test_db_reset.py は対象 worktree 側
# ではなく、この gate-integration.sh 自身と同じ版を常に使う。INT/TERM は実行中のテスト
# プロセスグループへ転送し、全滅を待ってから後始末する。
set -u
cd "$(dirname "$0")/.." || exit 1
SELF_ROOT="$(pwd)"
# shellcheck source=scripts/lib/gate_common.sh
. "$SELF_ROOT/scripts/lib/gate_common.sh"

usage() { echo "使い方: scripts/gate-integration.sh <worktree> --lane <lane> [--keep-db] [--only <pytest引数...>]" >&2; }

WORKTREE=${1:-}
if [ -z "$WORKTREE" ]; then usage; exit 2; fi
shift

LANE=""
KEEP_DB=0
ONLY_ARGS=()
while [ $# -gt 0 ]; do
  case "$1" in
    --lane)
      if [ $# -lt 2 ]; then echo "--lane には値が必要です" >&2; usage; exit 2; fi
      LANE=$2; shift 2 ;;
    --keep-db) KEEP_DB=1; shift ;;
    --only)
      shift
      if [ $# -eq 0 ]; then echo "--only には pytest 引数が必要です" >&2; usage; exit 2; fi
      ONLY_ARGS=("$@"); break ;;
    *) echo "不明な引数: $1" >&2; usage; exit 2 ;;
  esac
done

# 下の pytest 呼び出しが -q を必ず付与するため、--only 側にも -q があると -qq になり合否サマリ
# 行が消える（grep ベースの抽出が壊れる）。利用者が誤って重ねても安全なようにここで除去する。
if [ "${#ONLY_ARGS[@]}" -gt 0 ]; then
  _FILTERED_ONLY_ARGS=()
  for _a in "${ONLY_ARGS[@]}"; do
    [ "$_a" = "-q" ] && continue
    _FILTERED_ONLY_ARGS+=("$_a")
  done
  ONLY_ARGS=("${_FILTERED_ONLY_ARGS[@]}")
fi

# --only の境界（tests/integration 配下のみ）。worktree/venv/DB より先に検証する＝境界違反を
# 即座に弾く。
if [ "${#ONLY_ARGS[@]}" -gt 0 ]; then
  if ! gate_validate_only_prefixes "tests/integration" "tests/integration" "${ONLY_ARGS[@]}"; then
    exit 2
  fi
fi

if [ -z "$LANE" ]; then
  echo "--lane は必須です（共有 sherpa_test への暗黙 fallback はしません）" >&2
  usage; exit 2
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

# --- レーン別ロック（同名二重起動の排他・ブロッキング・gate-lane.sh と共有） ------------------
LANE_LOCKFILE="/tmp/sherpa-gate-${LANE}.lock"
echo "=== integration ($LANE): レーン別ロック待ち ($(date +%H:%M:%S))"
if ! gate_acquire_named_lock "$LANE_LOCKFILE"; then
  echo "integration ($LANE): レーン別ロックの取得に失敗しました（$LANE_LOCKFILE）" >&2
  exit 1
fi

# --- integration 専用ロック（ブロッキング・fail-closed） -----------------------------------
echo "=== integration ($LANE): 専用ロック待ち ($(date +%H:%M:%S))"
exec {LOCK_FD}>"$GATE_INTEGRATION_LOCKFILE"
if ! flock "$LOCK_FD"; then
  echo "integration ($LANE): 専用ロックの取得に失敗しました（$GATE_INTEGRATION_LOCKFILE）" >&2
  exit 1
fi
echo "=== integration ($LANE): 専用ロック確保 ($(date +%H:%M:%S))"

# --- 同時実行数のスロット確保（上限2・gate-lane.sh と合計で共有・空くまでブロッキングで待つ） ---
echo "=== integration ($LANE): スロット待ち ($(date +%H:%M:%S))"
gate_acquire_lane_slot
echo "=== integration ($LANE): スロット ${GATE_SLOT_INDEX}/${#GATE_LANE_SLOT_LOCKS[@]} を確保 ($(date +%H:%M:%S))"
GATE_HELD_FDS=("$GATE_NAMED_LOCK_FD" "$LOCK_FD" "$GATE_SLOT_FD")

echo "=== integration ($LANE): DB $DBNAME を初期化 ($(date +%H:%M:%S))"
"$PY" "$SELF_ROOT/scripts/test_db_reset.py" --name "$DBNAME" || exit 1
LANE_DSN="$(gate_compute_lane_dsn "$PY" "$WORKTREE" "$DBNAME")" || exit 1
export SHERPA_TEST_PG_DSN="$LANE_DSN"
export SHERPA_TEST_WORLD_ID="pytest-${LANE}"   # tests/_world_setup.py::TEST_WORLD_ID を上書き（gate-lane.sh と同じレーン規約に揃える）

cleanup() {
  if [ "$KEEP_DB" = 1 ]; then
    echo "=== integration ($LANE): --keep-db 指定のため $DBNAME を残します"
  else
    echo "=== integration ($LANE): DB $DBNAME を後始末 ($(date +%H:%M:%S))"
    "$PY" "$SELF_ROOT/scripts/test_db_reset.py" --name "$DBNAME" --drop-only || exit 1
  fi
}
trap cleanup EXIT
trap 'gate_handle_signal TERM' TERM
trap 'gate_handle_signal INT' INT

cd "$WORKTREE"
log=${SHERPA_GATE_INTEGRATION_LOG:-${TMPDIR:-/tmp}/sherpa-gate-integration-$(date +%Y%m%d-%H%M%S).log}
: > "$log"   # 固定パスを外から指定された場合の前回実行分混入を防ぐ（以後は tee -a で追記）
GATE_LOG_FILE="$log"
export SHERPA_USE_FIXTURES=1; unset OPENAI_BASE_URL CODEX_HOME
GROUP_TIMEOUT=${GATE_GROUP_TIMEOUT:-45m}
if [ "${#ONLY_ARGS[@]}" -gt 0 ]; then
  TARGET=("${ONLY_ARGS[@]}")
else
  TARGET=(tests/integration)
fi
run(){ gate_run_group "$@"; }

echo "=== integration ($LANE) 開始 ($(date +%H:%M:%S))" | tee -a "$log"
TEST_FAIL=0
run integration "${TARGET[@]}" || TEST_FAIL=1
echo "=== integration ($LANE) DONE ($(date +%H:%M:%S))" | tee -a "$log"

echo "ログ: $log"
[ "$TEST_FAIL" = 0 ]
