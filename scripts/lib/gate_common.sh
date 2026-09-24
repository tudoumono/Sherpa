#!/usr/bin/env bash
# gate-lane.sh / gate-integration.sh / gate-quiet.sh 共通ヘルパ（TEST-3・レーン別 world 分離）。
# source 専用。
#
# 使い方（source）:
#   . "$(dirname "$0")/lib/gate_common.sh"
#   validate_lane_name "$LANE" || exit 2
#   PY="$(gate_resolve_venv_python "$WORKTREE")"
#   LANE_DSN="$(gate_compute_lane_dsn "$PY" "$WORKTREE" "sherpa_test_$LANE")"
#   gate_acquire_named_lock "/tmp/sherpa-gate-$LANE.lock" || exit 1   # 同名 cross-entry 排他
#   gate_acquire_lane_slot                                            # 上限2・空くまで待つ
#   GATE_HELD_FDS=("$GATE_NAMED_LOCK_FD" "$GATE_SLOT_FD")
#   GATE_LOG_FILE="$log"
#   run(){ gate_run_group "$@"; }
#   trap 'gate_handle_signal TERM' TERM; trap 'gate_handle_signal INT' INT
#   run unit tests/unit; run contract tests/contract   # launcher が直接呼ぶ（パイプ／サブシェル
#                                                       # で包まない・下記 gate_run_group 参照）

# 固定パス（環境変数による上書きなし）。tests/_world_setup.py が env `SHERPA_TEST_WORLD_ID`
# （gate-lane.sh／gate-integration.sh がレーンごとに `pytest-<lane>` を注入）で Neo4j/ES の
# world をレーンごとに分離するため、PG（レーン別 DB）に加えて world も真に分離できる。
# 以下のロックは、それでも残る共有資源を守るためのもの:
#   - GATE_LANE_SLOT_LOCKS: 同時実行数の上限（gate-lane.sh のレーン＋gate-integration.sh の
#     実行が合計で消費する・メモリ実測・ES/Neo4j への同時負荷を考慮し 2 本のスロットに制限）。
#   - GATE_INTEGRATION_LOCKFILE: gate-integration.sh 専用の直列キュー（tests/integration には
#     lane 注入で分離できない固定 world_id 依存のテストが残るため、integration の実行同士を
#     1本だけに絞る。スロットの消費とは別に、これも合わせて保持する）。
#   - GATE_ADMISSION_LOCKFILE: スロット待ちの待機者を直列化する admission ロック（飢餓防止・
#     下記 gate_acquire_lane_slot 参照）。
GATE_LANE_SLOT_LOCKS=(/tmp/sherpa-gate-slot-1.lock /tmp/sherpa-gate-slot-2.lock)
GATE_INTEGRATION_LOCKFILE="/tmp/sherpa-gate-integration.lock"
GATE_ADMISSION_LOCKFILE="/tmp/sherpa-gate-admission.lock"
GATE_LANE_POLL_SECONDS=5   # 固定値（環境変数による上書きなし・全スロット使用中の再試行間隔）

# gate-lane.sh／gate-integration.sh 共通: 指定パスのロックをブロッキングで確保する（同名レーンの
# cross-entry 排他に使う——`/tmp/sherpa-gate-<lane>.lock` を lane 経由・integration 経由の両方が
# 取り合うことで、同じレーン名を2つの入口から同時に起動できないようにする）。確保できた fd を
# GATE_NAMED_LOCK_FD に残す（プロセス終了で自動解放）。
gate_acquire_named_lock() {
  local lockfile="$1"
  local fd
  exec {fd}>"$lockfile"
  if ! flock "$fd"; then
    return 1
  fi
  GATE_NAMED_LOCK_FD=$fd
  return 0
}

# gate-lane.sh／gate-integration.sh 共通: 空いているレーンスロットを1つブロッキングで確保する
# （GATE_LANE_SLOT_LOCKS の本数が上限・全部埋まっていれば空くまで再試行する）。
# 待機者は GATE_ADMISSION_LOCKFILE をブロッキングで取ってからスロットを走査し、確保できた
# 直後（または全滅と分かった直後）に admission を解放する——複数の待機者が同時に非ブロッキング
# flock を撃ち合って早い者勝ちになる（結果的に特定の待機者だけ運悪く取り損ね続ける）飢餓を防ぐ。
# 確保できたスロット番号（1始まり）を GATE_SLOT_INDEX に、fd 番号を GATE_SLOT_FD に残す
# （呼び出し元は GATE_HELD_FDS に積んで gate_run_group の子への非継承に使う）。
gate_acquire_lane_slot() {
  while true; do
    local admission_fd
    exec {admission_fd}>"$GATE_ADMISSION_LOCKFILE"
    flock "$admission_fd"
    local i=1 slot fd
    for slot in "${GATE_LANE_SLOT_LOCKS[@]}"; do
      exec {fd}>"$slot"
      if flock -n "$fd"; then
        GATE_SLOT_INDEX=$i
        GATE_SLOT_FD=$fd
        exec {admission_fd}>&-
        return 0
      fi
      exec {fd}>&-
      i=$((i + 1))
    done
    exec {admission_fd}>&-
    sleep "$GATE_LANE_POLL_SECONDS"
  done
}

# gate-quiet.sh 専用: 全レーンスロット＋integration 専用ロックを非ブロッキングで全部確保する
# （「他のゲートが1つも動いていない」の保証に必要な全ロック）。1つでも取れなければ、それまでに
# 確保した分を解放して 1 を返す（部分確保のまま居座らない）。確保できた fd は GATE_QUIET_FDS に
# 残る（プロセス終了で自動解放・明示 unlock は不要）。
gate_acquire_all_or_none() {
  GATE_QUIET_FDS=()
  local lockfile fd f2
  for lockfile in "${GATE_LANE_SLOT_LOCKS[@]}" "$GATE_INTEGRATION_LOCKFILE"; do
    exec {fd}>"$lockfile"
    if ! flock -n "$fd"; then
      for f2 in "${GATE_QUIET_FDS[@]}"; do exec {f2}>&-; done
      GATE_QUIET_FDS=()
      return 1
    fi
    GATE_QUIET_FDS+=("$fd")
  done
  return 0
}

# gate-lane.sh／gate-integration.sh／gate-quiet.sh 共通: 1群を実行する。**呼び出し元はこの関数を
# パイプ（`| tee`）やサブシェル（`{ ... } &`）で包まず、launcher 本体のトップレベルから直接
# 呼ばなければならない**——bash の `wait PID` は「呼び出し元シェルの直接の子」にしか使えず、
# 間にもう1段シェル層（パイプはそれ自体が1段フォークする）を挟むと、gate_run_group が起動した
# テスト子は launcher から見て孫 PID になる。孫 PID に対する `wait` は「そのPIDは子ではない」で
# 即座に 127 を返して空振りする——gate_handle_signal がテスト子へ signal を送った直後に空振り
# `wait` で素通りして exit してしまい、`trap cleanup EXIT`（DB drop）がテスト子の実プロセス終了・
# DB 接続のクローズより先行しうる（実測で発見）。launcher 直下で直接呼べば、gate_run_group 内の
# `wait "$GATE_RUN_PID"`（テスト子の setsid プロセスグループ leader を直接待つ）も
# gate_handle_signal 内の同じ `wait` も、どちらも正しく「直接の子を待つ」になる。
#
# 前面パイプラインでの `wait` 遅延（bash が非対話シェルの前面パイプライン完了まで trap 実行を
# 遅延させる仕様）を避けるためにも、この「パイプで包まない」構成がそのまま効く——launcher の
# 唯一のブロッキング点が素の `wait` 組み込み呼び出しになるため、trap は signal 到着後ただちに
# 実行される（実測で確認済み）。
#
# テストプロセスは setsid で専用の新しいプロセスグループとして起動する（gate_handle_signal が
# このグループへ丸ごとシグナルを転送できるようにするため）。呼び出し元が保持しているロック fd
# （GATE_HELD_FDS に fd 番号を積んでおく）は、子（テストプロセス）へ継承させない——fd を明示
# close してから exec する（`flock -o` 相当）。
#
# ログは `GATE_LOG_FILE`（呼び出し元が設定）へ `tee -a` で都度追記する（1本の長寿命パイプに
# しない）——長寿命パイプにすると、パイプ相手の `tee` 自身もテスト子と同じ枠組みで一緒に
# 殺されやすく、シグナル処理後に書く「pytest 末尾の要約・exit 理由」が tee の消滅で失われうる
# （実測で発見）。都度 `tee -a` にすれば、その都度のプロセスは短命に完了し、シグナル処理後の
# 要約出力も無傷でログに残る。
#
# 使い方: 呼び出し元が PY・GROUP_TIMEOUT・GATE_LOG_FILE を変数で用意する。RC_FILE が設定されて
# いれば終了コードを追記する（未設定でもよい＝gate-quiet.sh はこの追記を使わない）。
#
# GATE_EXTRACT_PATTERN はテスト子の生出力から残す行を選ぶ grep パターン（pytest の合否サマリ等）
# ＋ gate_handle_signal が書く `SCRIPT_EXIT` マーカー行——両方を同じ語彙として扱う（ログを後から
# 走査するときに「テストの合否」と「launcher 自体が signal で終了したか」を同じ抽出基準で
# 拾えるようにするため）。
GATE_EXTRACT_PATTERN='^FAILED|passed|failed|Killed|MemoryError|Timeout|^SCRIPT_EXIT'
GATE_HELD_FDS=()
GATE_RUN_PID=""
GATE_RUN_OUT=""
GATE_LOG_FILE=""

gate_run_group() {
  local name="$1"; shift
  echo "=== $name ($(date +%H:%M:%S))" | tee -a "${GATE_LOG_FILE:-/dev/null}"
  local out; out=$(mktemp)
  GATE_RUN_OUT="$out"
  (
    local _held
    for _held in "${GATE_HELD_FDS[@]}"; do
      [ -n "$_held" ] && eval "exec ${_held}>&-"
    done
    exec setsid timeout -s INT "$GROUP_TIMEOUT" "$PY" -m pytest "$@" -q -p no:cacheprovider -rf > "$out" 2>&1
  ) &
  GATE_RUN_PID=$!
  wait "$GATE_RUN_PID"
  local rc=$?
  grep -E "$GATE_EXTRACT_PATTERN" "$out" | tail -30 | tee -a "${GATE_LOG_FILE:-/dev/null}"
  rm -f "$out"
  echo "exit=${rc}" | tee -a "${GATE_LOG_FILE:-/dev/null}"
  [ -n "${RC_FILE:-}" ] && echo "$rc" >> "$RC_FILE"
  # 状態のクリアは要約・exit= を書き終えた後（両方とも GATE_RUN_OUT を使い終えてから）。
  GATE_RUN_PID=""
  GATE_RUN_OUT=""
  return "$rc"
}

# gate-lane.sh／gate-integration.sh／gate-quiet.sh 共通: INT/TERM を受けたら、今動いている
# テストプロセスグループ（GATE_RUN_PID・gate_run_group が setsid で確保済み・launcher 本体の
# 直接の子——上の gate_run_group の契約参照）へ同じシグナルを転送し、確実に `wait` してから
# 要約（pytest 出力の末尾＋exit 理由＋SCRIPT_EXIT マーカー）を GATE_LOG_FILE へ書き、
# `trap cleanup EXIT` へ制御を渡す（DB drop 等の後始末はテスト子の実プロセス終了後に行われる）。
# 呼び出し元は `trap 'gate_handle_signal TERM' TERM; trap 'gate_handle_signal INT' INT` のように
# 登録する。
gate_handle_signal() {
  local sig="$1"
  if [ -n "$GATE_RUN_PID" ]; then
    kill -s "$sig" "-$GATE_RUN_PID" 2>/dev/null
    wait "$GATE_RUN_PID" 2>/dev/null
    local rc=$?
    if [ -n "$GATE_RUN_OUT" ]; then
      grep -E "$GATE_EXTRACT_PATTERN" "$GATE_RUN_OUT" | tail -30 | tee -a "${GATE_LOG_FILE:-/dev/null}"
      rm -f "$GATE_RUN_OUT"
    fi
    echo "exit=${rc}（signal ${sig} を受けて停止）" | tee -a "${GATE_LOG_FILE:-/dev/null}"
    echo "SCRIPT_EXIT(signal=${sig})" | tee -a "${GATE_LOG_FILE:-/dev/null}"
    [ -n "${RC_FILE:-}" ] && echo "$rc" >> "$RC_FILE"
    # 状態のクリアは要約・exit=・SCRIPT_EXIT を書き終えた後（全て GATE_RUN_PID/GATE_RUN_OUT の
    # 値を使い終えてから）。
    GATE_RUN_PID=""
    GATE_RUN_OUT=""
  fi
  case "$sig" in
    INT) exit 130 ;;
    TERM) exit 143 ;;
    *) exit 1 ;;
  esac
}

# gate-lane.sh／gate-integration.sh 共通: --only の対象パスが許可ディレクトリ配下に収まっている
# か検証する（境界の取り違えでレーン間の分離契約を壊さないようにする）。$1 はエラー
# メッセージに出す許可範囲のラベル、$2 は許可 prefix をスペース区切りで並べた1文字列、残りは
# 検証対象（ONLY_ARGS）。`-` 始まりの pytest オプション（値も含む）は対象外。
# `..` パス要素（例 `tests/api/../integration/x.py`）は prefix 判定より**前**に拒否する——
# 素朴な prefix 文字列一致だけだと、許可 prefix で始まりつつ `..` で実際には別ディレクトリを
# 指すパスをすり抜けさせてしまう。違反があれば標準エラーへ理由を出し 1 を返す（呼び出し元は
# exit 2 する）。
gate_validate_only_prefixes() {
  local label="$1" prefixes="$2"; shift 2
  local arg prefix ok
  for arg in "$@"; do
    case "$arg" in
      -*) continue ;;
    esac
    case "$arg" in
      *"/../"*|"../"*|*"/.."|"..")
        echo "--only にディレクトリ迂回（..）は使えません: $arg" >&2
        return 1
        ;;
    esac
    ok=0
    for prefix in $prefixes; do
      case "$arg" in
        "$prefix"|"$prefix"/*) ok=1; break ;;
      esac
    done
    if [ "$ok" != 1 ]; then
      echo "--only は ${label} 配下のみ許可します: $arg" >&2
      return 1
    fi
  done
  return 0
}

# レーン名は DB 名サフィックス（sherpa_test_<lane>）に直結する。PostgreSQL の識別子上限
# 63 bytes から "sherpa_test_" の12文字を引いた51文字を上限にし、scripts/test_db_reset.py の
# 許可パターンと揃える。
validate_lane_name() {
  [[ "$1" =~ ^[a-z0-9]{1,51}$ ]]
}

# worktree 自身の .venv を優先し、無ければ git worktree の主リポジトリ（`git rev-parse
# --git-common-dir` の親）の .venv を間借りする。git worktree には .venv が複製されない
# （docs/17-開発の教訓.md 参照）ため、隔離 worktree での実行を成立させるのに必要。
gate_resolve_venv_python() {
  local worktree="$1"
  if [ -x "$worktree/.venv/bin/python" ]; then
    echo "$worktree/.venv/bin/python"; return 0
  fi
  local common_git_dir main_root
  common_git_dir=$(git -C "$worktree" rev-parse --git-common-dir 2>/dev/null || true)
  if [ -n "$common_git_dir" ]; then
    case "$common_git_dir" in
      /*) : ;;
      *) common_git_dir="$worktree/$common_git_dir" ;;
    esac
    main_root="$(cd "$common_git_dir/.." && pwd)"
    if [ -x "$main_root/.venv/bin/python" ]; then
      echo "$main_root/.venv/bin/python"; return 0
    fi
  fi
  echo "$worktree/.venv/bin/python"   # 無くてもそのまま返す（呼び出し元の -x チェックで気づかせる）
}

# tests/conftest.py::_setup_test_pg_dsn の _swap_dbname と同じ発想（dbname だけ差し替えた DSN を
# 作る）を小さく複製する。conftest.py は import すると即座に DB 分離処理が走る契約（変更禁止）の
# ため、import して再利用するのではなく同じロジックをここに独立して持つ。
gate_compute_lane_dsn() {
  local py="$1" worktree="$2" dbname="$3"
  ( cd "$worktree" && "$py" - "$dbname" <<'PYEOF'
import sys
sys.path.insert(0, ".")
from sherpa import store
from psycopg import conninfo as _ci
d = _ci.conninfo_to_dict(store._dsn())
d["dbname"] = sys.argv[1]
print(_ci.make_conninfo(**d))
PYEOF
  )
}
