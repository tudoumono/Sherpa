#!/usr/bin/env bash
# 単体+契約テストを壁時計予算付きで実行する共通ヘルパ（`make gate-merge`／`make gate-ci` で
# 重複させないための source 専用ファイル）。呼び出し元は bash（`set -o pipefail` 前提）で
# `. scripts/lib/gate_budget.sh` してから `gate_run_unit_contract_budgeted "$PY"` を呼ぶ。
#
# 予算は `SHERPA_UNIT_BUDGET_SEC`（既定 300 秒＝5分・docs/20-開発ハーネス.md §6 の確定値）。
# 超過は pytest 自体が緑でも失敗として扱う（結果・所要秒数は成功・失敗いずれでも表示する）。
set -o pipefail

gate_run_unit_contract_budgeted() {
  local py="$1"
  local budget="${SHERPA_UNIT_BUDGET_SEC:-300}"
  local start=$SECONDS
  # 呼び出し元は `set -euo pipefail` 前提（Makefile 側）。pytest を素の simple command のまま
  # 置くと errexit で赤の瞬間に関数ごと即終了し、下の所要秒数・終了コードの表示に到達しない。
  # if の条件式に置くことで errexit の対象から外し、赤でも必ず結果行を出してから非ゼロで返す。
  local rc
  if SHERPA_USE_FIXTURES=1 "$py" -m pytest tests/unit tests/contract -q; then
    rc=0
  else
    rc=$?
  fi
  local elapsed=$((SECONDS - start))
  echo "単体+契約: ${elapsed}s（予算 ${budget}s・pytest 終了コード ${rc}）"
  if [ "$elapsed" -gt "$budget" ]; then
    echo "NG: 単体+契約の壁時計が予算 ${budget}s を超えました（${elapsed}s）"
    [ "$rc" -eq 0 ] && rc=1
  fi
  return "$rc"
}
