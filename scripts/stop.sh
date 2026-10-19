#!/usr/bin/env bash
# Sherpa を全部停止する: アプリ（pid）→ 取り込みワーカー（pid）→ Caddy（pid）→ ストア（docker compose down）の順。
#
#   ./scripts/stop.sh              # アプリ・取り込みワーカー・Caddy・ストアを全部停止
#   KEEP_STORES=1 ./scripts/stop.sh  # ストア（DB/検索）は残し、アプリ・ワーカー・Caddy だけ停止
#   STOP_ONLY=app ./scripts/stop.sh    # アプリ（Web）だけ停止（取り込みワーカーは動かしたまま＝取り込みは続く）
#   STOP_ONLY=worker ./scripts/stop.sh # 取り込みワーカーだけ停止
#   FORCE=1 ./scripts/stop.sh        # 取り込み中でも待たず、短い猶予（10 秒）で強制終了
#
# 取り込み中は、取り込みワーカーが処理中の 1 件が終わるまで待ってから止まります（上限 SHERPA_STOP_WAIT 秒・既定 1800）。
# アプリ（Web）は取り込みを持たないのですぐ止まります（前の版のアプリが取り込みを抱えているときだけ、その 1 件を待ちます）。
# 強制終了や上限超えで途中になった取り込みは、次の起動で自動的に続きから再開します。
# pid ファイルの残骸（プロセスは既に死亡）は掃除します。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
# shellcheck source=scripts/lib/install_state.sh
. "$ROOT/scripts/lib/install_state.sh"

KEEP_STORES="${KEEP_STORES:-0}"
FORCE="${FORCE:-0}"
STOP_ONLY="${STOP_ONLY:-all}"
case "$STOP_ONLY" in
  all|app|worker) ;;
  *) echo "STOP_ONLY は all / app / worker のどれかです（指定: ${STOP_ONLY}）" >&2; exit 2 ;;
esac
# 取り込み中に終了を待つ上限（秒）。取り込みが動いていないときの猶予は IDLE_WAIT 秒（従来どおり）。FORCE=1 は取り込み中でも IDLE_WAIT 秒。
IDLE_WAIT=10
if [ "$FORCE" = "1" ]; then STOP_WAIT="$IDLE_WAIT"; else STOP_WAIT="${SHERPA_STOP_WAIT:-1800}"; fi
# 前の版（0.18.x）のアプリが書く処理状況。1 行目 pid=<アプリ>、以降 run<TAB>資料フォルダ<TAB>開始(epoch秒)<TAB>処理中ファイル
# 取り込みワーカー（sherpa/ingest/ingest_worker.py）が書く処理状況（${WORKER_STATE_FILE}）も同じ形式。
INGEST_STATE_FILE="${SHERPA_INGEST_STATE_FILE_LEGACY:-$RUN_DIR/ingest_active}"
TAB="$(printf '\t')"

# 秒を「N 秒」「M 分 S 秒」に整える。
fmt_secs() {
  local n="$1"
  if [ "$n" -ge 60 ]; then echo "$(( n / 60 )) 分 $(( n % 60 )) 秒"; else echo "${n} 秒"; fi
}

# 取り込み中なら 0（NOTE_REL=処理中ファイル・NOTE_SINCE=その開始時刻）。取り込み中でなければ 1。
# $1=前の版のアプリの pid（空なら pid を問わない）。$2=処理状況ファイル（省略時は前の版のアプリのもの）。
ingest_active() {
  local fpid line file="${2:-$INGEST_STATE_FILE}"
  [ -f "$file" ] || return 1
  fpid="$(sed -n 's/^pid=//p' "$file" 2>/dev/null | head -1)"
  if [ -n "$1" ] && [ "$fpid" != "$1" ]; then return 1; fi
  line="$(grep -m1 "^run${TAB}" "$file" 2>/dev/null || true)"
  [ -n "$line" ] || return 1
  NOTE_REL=""
  NOTE_SINCE=""
  IFS="$TAB" read -r _ _ NOTE_SINCE NOTE_REL <<< "$line"
  [ -n "$NOTE_SINCE" ] || NOTE_SINCE="$(date +%s)"
  return 0
}

# 取り込み中の待ち状況を表示する（最初に 1 回の案内、以降 10 秒ごとに処理中の 1 件）。
NOTICE_ANNOUNCED=0
NOTICE_LAST=0
ingest_notice() {  # $1=現在時刻（epoch秒）
  local now="$1"
  if [ "$NOTICE_ANNOUNCED" = 0 ]; then
    echo "  取り込み中です。処理中の 1 件が終わるまで待ってから止まります（上限 $(fmt_secs "$STOP_WAIT")）。"
    NOTICE_ANNOUNCED=1
  fi
  if [ $(( now - NOTICE_LAST )) -ge 10 ]; then
    if [ -n "$NOTE_REL" ]; then
      echo "  取り込み中の 1 件の完了を待っています（${NOTE_REL}・経過 $(fmt_secs $(( now - NOTE_SINCE )))）"
    else
      echo "  取り込みの区切りまで待っています（経過 $(fmt_secs $(( now - NOTE_SINCE )))）"
    fi
    NOTICE_LAST="$now"
  fi
}

# 上限を超えて強制終了するときの案内。取り込み中だったなら、再開の見込みも伝える。
report_forced() {  # $1=取り込み中だったか(0/1)
  if [ "$1" = "1" ]; then
    echo "  待つ上限を超えたため強制終了します（SIGKILL）。取り込みは途中で止まり、次の起動で自動的に続きから再開します。" >&2
  else
    echo "  応答しないため強制終了します（SIGKILL）。" >&2
  fi
}

# 「処理中の 1 件の完了を待って止まれる版」のアプリか。新しい版だけが起動時に RUN_DIR/graceful_stop_ready へ pid を書く。
# 見つからない（古い版・判定できない）ときは古い版として扱う＝待ちが少し長くなるだけで安全側。
GRACEFUL_MARK="$RUN_DIR/graceful_stop_ready"
app_is_graceful() {  # $1=pid
  [ -f "$GRACEFUL_MARK" ] && [ "$(sed -n 's/^pid=//p' "$GRACEFUL_MARK" 2>/dev/null | head -1)" = "$1" ]
}

# 変換ログの時刻（"YYYY-MM-DD HH:MM:SS"・ローカル時刻）を epoch 秒にする。GNU date（-d）と BSD date（-j -f）の両方に対応。
log_epoch() {
  date -d "$1" +%s 2>/dev/null || date -j -f "%Y-%m-%d %H:%M:%S" "$1" +%s 2>/dev/null
}

# 古い版のアプリは終了の合図の 5 秒後に取り込みを打ち切る。MD 化の途中で合図を送ると重い 1 件が無駄になるため、
# 変換ログの最後の「MD化を開始します」が 5 秒以内（＝1 件が始まった直後）になるまで合図を送らずに待つ。
# 最後の MD 化の行が完了・失敗（ファイルの合間）や、行が無い・上限より古いときは待たない。
wait_old_app_boundary() {  # $1=pid
  local pid="$1" log="$RUN_DIR/convert.log" start now line ts t age rel last_note=0
  [ "$FORCE" = "1" ] && return 0
  start="$(date +%s)"
  while kill -0 "$pid" 2>/dev/null; do
    [ -f "$log" ] || return 0
    line="$(grep -E 'MD化を開始します|MD化が' "$log" 2>/dev/null | tail -1 || true)"
    case "$line" in *"MD化を開始します"*) ;; *) return 0 ;; esac
    ts="$(printf '%s' "$line" | cut -c1-19)"
    t="$(log_epoch "$ts" || true)"
    [ -n "$t" ] || return 0
    now="$(date +%s)"
    age=$(( now - t ))
    if [ "$age" -le 5 ] || [ "$age" -ge "$STOP_WAIT" ] || [ $(( now - start )) -ge "$STOP_WAIT" ]; then
      return 0
    fi
    if [ $(( now - last_note )) -ge 10 ]; then
      rel="$(printf '%s' "$line" | sed -e 's/.*MD化を開始します: //' -e 's/（RSS.*//')"
      echo "  このアプリは古い版のため、処理中の 1 件が終わるのを待っています（${rel}・経過 $(fmt_secs "$age")）"
      last_note="$now"
    fi
    sleep 1
  done
  return 0
}

# pid の子孫（再帰）を 1 行ずつ出す。
descendants_of() {  # $1=pid
  ps -eo pid=,ppid= 2>/dev/null | awk -v root="$1" '
    { p[NR]=$1; q[NR]=$2; n=NR }
    END {
      inset[root]=1; changed=1
      while (changed) { changed=0
        for (i=1;i<=n;i++) if (!(p[i] in inset) && (q[i] in inset)) { inset[p[i]]=1; changed=1; print p[i] } }
    }'
}

# 止めた後に残っているアプリ配下のプロセス（先読みの子・soffice など）を知らせる。勝手には止めない。
report_leftovers() {  # $1=停止前に控えた pid の一覧（空白区切り）
  local left="" p
  for p in $1; do
    if kill -0 "$p" 2>/dev/null; then left="$left $p"; fi
  done
  [ -n "$left" ] || return 0
  echo "  次のプロセスがアプリの停止後も残っています（この Sherpa が起動したもの）。不要なら kill してください:" >&2
  for p in $left; do
    ps -o pid=,ppid=,etime=,args= -p "$p" 2>/dev/null | cut -c1-200 | sed 's/^/    /' >&2 || true
  done
}

# pid の終了を待つ。取り込み中は STOP_WAIT 秒まで、そうでなければ IDLE_WAIT 秒まで（最後に取り込み中だった時刻から数える）。
# 終了したら 0、上限を超えたら 1（WAITED_INGEST=1 なら取り込み中だった）。$2=1 のときだけ取り込みの状況を見て表示する（アプリ・ワーカー用）。
# $3=worker のときは取り込みワーカーの処理状況ファイルを見る（見張りの pid ではなく中の Python の pid が書かれるため pid は問わない）。
WAITED_INGEST=0
wait_for_exit() {  # $1=pid  $2=取り込みの状況を見るか(0/1)  $3=app|worker
  local pid="$1" aware="$2" kind="${3:-app}" start now last_active active=1
  start="$(date +%s)"; last_active="$start"
  WAITED_INGEST=0; NOTICE_ANNOUNCED=0; NOTICE_LAST=0
  while kill -0 "$pid" 2>/dev/null; do
    now="$(date +%s)"
    if [ "$aware" = "1" ]; then
      if [ "$kind" = "worker" ]; then
        if ingest_active "" "$WORKER_STATE_FILE"; then active=0; else active=1; fi
      else
        if ingest_active "$pid"; then active=0; else active=1; fi
      fi
    else
      active=1
    fi
    if [ "$active" = 0 ]; then
      last_active="$now"
      WAITED_INGEST=1
      ingest_notice "$now"
    fi
    if [ $(( now - start )) -ge "$STOP_WAIT" ] || [ $(( now - last_active )) -ge "$IDLE_WAIT" ]; then
      return 1
    fi
    sleep 1
  done
  return 0
}

# systemd で常駐しているユニット（このフォルダを WorkingDirectory にしているもの）を止める。
# 止められなければ、止め方を案内してここで終わる（動いたままストアだけ止めない）。
stop_systemd_units() {
  local units unit failed=0 spid start now last_active rc mpid
  units="$(pkg_systemd_active_units)"
  [ -n "$units" ] || return 0
  for unit in $units; do
    # 取り込みワーカーのユニット（sherpa-ingest*）かどうかで、STOP_ONLY の対象を分ける。
    case "$unit" in
      *ingest*) [ "$STOP_ONLY" != "app" ] || continue ;;
      *) [ "$STOP_ONLY" != "worker" ] || continue ;;
    esac
    echo "systemd のユニット ${unit} を停止します..."
    mpid="$(systemctl show -p MainPID --value "$unit" 2>/dev/null || true)"
    case "$unit" in
      *ingest*) ;;   # 取り込みワーカーは前の版のアプリではない（古い版の合図待ちは要らない）
      *) case "$mpid" in ''|*[!0-9]*|0) ;; *) if ! app_is_graceful "$mpid"; then wait_old_app_boundary "$mpid"; fi ;; esac ;;
    esac
    # 停止は終わるまで返らないため、背景で実行して取り込みの待ち状況を表示する。上限を超えたら SIGKILL で終わらせる。
    { systemctl stop "$unit" 2>/dev/null || sudo -n systemctl stop "$unit" 2>/dev/null; } &
    spid=$!
    start="$(date +%s)"; last_active="$start"
    WAITED_INGEST=0; NOTICE_ANNOUNCED=0; NOTICE_LAST=0
    while kill -0 "$spid" 2>/dev/null; do
      now="$(date +%s)"
      if ingest_active "" || ingest_active "" "$WORKER_STATE_FILE"; then
        last_active="$now"
        WAITED_INGEST=1
        ingest_notice "$now"
      fi
      if [ $(( now - start )) -ge "$STOP_WAIT" ] || { [ "$WAITED_INGEST" = 0 ] && [ "$FORCE" = "1" ] && [ $(( now - last_active )) -ge "$IDLE_WAIT" ]; }; then
        report_forced "$WAITED_INGEST"
        systemctl kill --signal=SIGKILL "$unit" 2>/dev/null || sudo -n systemctl kill --signal=SIGKILL "$unit" 2>/dev/null || true
        break
      fi
      sleep 1
    done
    rc=0
    wait "$spid" || rc=$?
    if [ "$rc" = 0 ]; then
      echo "  ${unit} を停止しました。"
    else
      echo "✗ ${unit} を止められませんでした（権限が足りない可能性があります）。次を実行してから、もう一度 make stop を実行してください:" >&2
      echo "    sudo systemctl stop ${unit}" >&2
      failed=1
    fi
  done
  [ "$failed" = 0 ] || exit 1
}

# pid ファイルのプロセスを穏当に停止する（SIGTERM→待って SIGKILL）。
# アプリ（$4=app）と取り込みワーカー（$4=worker）は、取り込み中なら処理中の 1 件が終わるまで待つため、SIGTERM は本体の pid だけへ送る
# （子プロセスを巻き込んで処理中の 1 件を壊さない）。終了後に残った子はグループへの SIGTERM で片付ける。
# 取り込みを持たない新しい版のアプリはすぐ止まる。ワーカーの待ちはワーカーの処理状況ファイルで見る。
# それ以外と FORCE=1 は、setsid 起動＝PID がプロセスグループ長のため、まずグループ全体に送る（子も巻き取る）。
# kill する前に pid の所有者を検証する（RV High-1）: 再起動等で pid が別プロセスに再利用されて
# いた場合、無関係なプロセス（グループ）を誤って殺さないよう、pid ファイルの掃除だけに留める。
stop_pid() {  # $1=表示名  $2=pidファイル  $3=コマンドライン照合用の部分文字列（needle）  $4=app|worker（アプリ・取り込みワーカーだけ指定）
  local label="$1" pf="$2" needle="$3" kind="${4:-}" pid rc graceful=0 aware=0 kids=""
  if [ "$kind" = "app" ] || [ "$kind" = "worker" ]; then aware=1; if [ "$FORCE" != "1" ]; then graceful=1; fi; fi
  # set -e 下では非0を返す代入は if の条件式として評価しないとスクリプトごと終了してしまう
  # （bare statement は errexit の対象・if/while の条件だけが免除される）。
  if pid="$(live_matching_pid "$pf" "$needle")"; then
    rc=0
  else
    rc=$?
  fi
  case "$rc" in
    0)
      echo "$label を停止します（pid ${pid}）..."
      if [ "$aware" = "1" ]; then
        kids="$(descendants_of "$pid" | tr '\n' ' ')"
        if [ "$kind" = "app" ] && ! app_is_graceful "$pid"; then wait_old_app_boundary "$pid"; fi
      fi
      if [ "$graceful" = "1" ]; then
        kill -TERM "$pid" 2>/dev/null || true
      else
        kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      fi
      if ! wait_for_exit "$pid" "$aware" "$kind"; then
        report_forced "$WAITED_INGEST"
        kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
      elif [ "$graceful" = "1" ] && kill -0 -- "-$pid" 2>/dev/null; then
        # 本体が終わった後に残った子（先読みのワーカー等）を片付ける
        kill -TERM -- "-$pid" 2>/dev/null || true
        sleep 2
        kill -KILL -- "-$pid" 2>/dev/null || true
      fi
      rm -f "$pf"
      if [ "$aware" = "1" ]; then report_leftovers "$kids"; fi
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

stop_systemd_units
if [ "$STOP_ONLY" != "worker" ]; then
  stop_pid "Sherpa アプリ" "$APP_PID_FILE" "$APP_PROC_NEEDLE" app
fi
if [ "$STOP_ONLY" != "app" ]; then
  stop_pid "取り込みワーカー" "$WORKER_PID_FILE" "$WORKER_PROC_NEEDLE" worker
fi
if [ "$STOP_ONLY" != "app" ]; then
  reaped="$(sherpa_reap_ingest_children)"
  if [ "${reaped:-0}" != 0 ]; then echo "取り込みワーカーが残した子プロセス ${reaped} 件を止めました。"; fi
fi
if [ "$STOP_ONLY" = "all" ]; then
  stop_pid "Caddy" "$CADDY_PID_FILE" "$CADDY_PROC_NEEDLE"
fi

if [ "$STOP_ONLY" != "all" ]; then
  :
elif [ "$KEEP_STORES" = "1" ]; then
  echo "ストア（PostgreSQL / Neo4j / Elasticsearch）は KEEP_STORES=1 のため残します。"
elif ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
  echo "Docker が起動していません（Docker Desktop / dockerd を起動してください）。ストア停止をスキップします。" >&2
else
  echo "ストア（PostgreSQL / Neo4j / Elasticsearch）を停止します..."
  sherpa_compose down
fi

echo "停止しました。"
