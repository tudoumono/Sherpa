#!/usr/bin/env bash
# 配布向けの起動一枚看板。初回も日常も同じ1コマンド（clone した人が最小手数で動かせること）。
#
#   ./scripts/start.sh            # serve モード（既定・本番相当。fixtures 非参照）
#   ./scripts/start.sh dev        # dev モード（fixtures 参照・開発用）
#   LAN=1 ./scripts/start.sh      # LAN 公開（0.0.0.0 で待受・平文 HTTP でログイン可・Caddy があれば HTTPS も起動）
#
# やること: 前提チェック → venv/依存（変更時だけ再インストール）→ ストア起動＋healthy 待ち
#           → アプリをバックグラウンド起動（pid/log 管理・二重起動しない）→ URL と操作を案内。
# 前提が無いときは、原因と直し方を日本語で案内して止まります（非技術者向け）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"

PY="${PYTHON_BIN:-python3}"
VENV="${SHERPA_VENV:-$ROOT/.venv}"
PORT="${SHERPA_PORT:-8000}"
# LAN 公開が既定（SHERPA_LAN 既定=1・ユーザー裁定 2026-09-04＝チームで使うサーバ製品のため）。
# このホストだけに閉じるときは LAN=0 make start（その場限り）か .env の SHERPA_LAN=0。
# 明示の LAN= が .env より優先（読み方の契約どおり）。
sherpa_env_default SHERPA_LAN
LAN="${LAN:-${SHERPA_LAN:-1}}"

# モード引数（run-api.sh と同じ意味論・既定 serve）。
MODE="${1:-serve}"
case "$MODE" in
  dev|api|development) MODE="dev" ;;
  serve|prod|production|"") MODE="serve" ;;
  *)
    echo "usage: $0 [dev|serve]   (環境変数 LAN=1 で LAN 公開)" >&2
    exit 2
    ;;
esac

# .env の OPENAI_API_KEY を見て、未設定/プレースホルダなら案内する（起動はブロックしない）。
check_openai_key() {
  # 読み方は run-common に一本化（SHERPA_ENV_FILE 対応・明示指定 ＞ .env）。
  sherpa_env_default OPENAI_API_KEY
  case "${OPENAI_API_KEY:-}" in
    "" | "sk-REPLACE_ME" | "REPLACE_ME")
      cat <<'EOF'

ⓘ OpenAI API キーが未設定です（.env の OPENAI_API_KEY）。
  ・まずは画面を試すだけなら、このままで大丈夫です（チャットの AI 選択で「ローカル（Ollama）」を選ぶと動きます）。
  ・OpenAI / Codex（OpenAI）を使うときは、.env の OPENAI_API_KEY を実際のキーに設定してください:
      OPENAI_API_KEY=sk-...（あなたのキー）
    設定後に ./scripts/start.sh を再実行してください（Codex の認証もそのとき自動で済みます）。
EOF
      ;;
    *)
      # Codex CLI があれば API キーで認証しておく（通信なし・冪等・サブスク認証済みなら触らない）。
      sherpa_codex_ensure_auth || true
      ;;
  esac
}

# --- 前提チェック（原因と直し方を日本語で案内）---
missing=0

if ! command -v "$PY" >/dev/null 2>&1; then
  cat >&2 <<EOF
✗ Python3 が見つかりません（コマンド: $PY）。
  Sherpa の起動には Python3 が必要です。
  Ubuntu / WSL2 での導入例:
    sudo apt update && sudo apt install -y python3 python3-venv python3-pip
  導入できたか確認:  python3 --version
EOF
  missing=1
fi

if ! command -v docker >/dev/null 2>&1; then
  cat >&2 <<'EOF'
✗ Docker が見つかりません。
  Sherpa は DB・検索ストアを Docker で動かします。
  このリポジトリでは次で導入できます（Ubuntu/WSL2・sudo パスワードを1回入力）:
    make install-docker
  （または Docker Desktop を導入）。導入できたか確認:  docker --version
EOF
  missing=1
elif ! docker compose version >/dev/null 2>&1; then
  cat >&2 <<'EOF'
✗ Docker Compose が使えません。
  Docker Desktop / Docker Engine が起動しているか、Docker Compose が有効かを確認してください。
  （Docker Desktop の場合は、先にアプリを起動しておく必要があります）
  確認:  docker compose version
EOF
  missing=1
fi

if [ "$missing" != 0 ]; then
  echo "" >&2
  echo "上記を解消してから、もう一度 ./scripts/start.sh を実行してください。" >&2
  exit 1
fi

# --- 同時起動の直列化（軽いロック・trap で必ず解除）---
# 「既に起動しているか」の判定から「アプリ起動」までを1つのロックで直列化し、
# 2つの start.sh が同時に「起動していない」と判定して二重起動するレースを防ぐ（RV Med-1）。
mkdir -p "$RUN_DIR"
LOCK_DIR="$RUN_DIR/start.lock"
if ! mkdir "$LOCK_DIR" 2>/dev/null; then
  cat >&2 <<EOF
✗ 別の起動処理が進行中です（$LOCK_DIR が存在）。
  少し待ってから再実行してください。前回の起動が異常終了して残っている場合は削除してください:
    rmdir "$LOCK_DIR"
EOF
  exit 1
fi
trap 'rmdir "$LOCK_DIR" 2>/dev/null || true' EXIT

# --- 既に起動していれば二重起動しない（pid の所有者を検証してから判定・RV High-1） ---
if pid="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE")"; then
  echo "Sherpa は既に起動しています（pid $pid）。"
  echo "  URL:    $CHAT_URL"
  echo "  状態:   make status"
  echo "  停止:   make stop"
  echo "  再起動: make restart"
  exit 0
fi
lp_rc=$?
if [ "$lp_rc" = 2 ]; then
  echo "pid ファイルが古いため掃除します（プロセスには触れません・pid 再利用の疑い）。"
fi
# pid ファイルの残骸（プロセスは死亡済み、または再利用された別プロセス）は掃除して起動を続ける。
[ -f "$APP_PID_FILE" ] && rm -f "$APP_PID_FILE"

# --- pid ファイルが無いのに healthz が既に応答している＝管理外の別サーバがこのポートを使っている ---
# （RV Med-1）。誤って「起動成功」と誤認しないよう、ここで止める。
if healthz_ok; then
  cat >&2 <<EOF
✗ ポート ${PORT} で別のサーバが応答しています（この start.sh の管理外です）。
  make status で状況を確認し、必要なら該当プロセスを停止してから再実行してください。
EOF
  exit 1
fi

# --- Python 環境（依存は requirements.txt / constraints.txt のハッシュが変わった時だけ再インストール）---
if [ ! -x "$VENV/bin/python" ]; then
  echo "Python 環境を作成します: $VENV"
  "$PY" -m venv "$VENV"
fi

# ハッシュ計算は sha256sum（stock macOS に無い）を避け、既に前提にしている Python で行う（RV High-2）。
# requirements.txt と constraints.txt の両方を連結してハッシュする（由来: 2026-07-13-横断レビュー対応.md
# R6a・constraints のピン更新だけでは requirements.txt が無変更のため再インストールが走らない穴を塞ぐ）。
REQ_HASH_FILE="$VENV/.requirements.sha256"
req_hash() {
  "$VENV/bin/python" -c \
    'import hashlib,sys; print(hashlib.sha256(b"".join(open(f,"rb").read() for f in sys.argv[1:])).hexdigest())' \
    requirements.txt constraints.txt
}
CUR_HASH="$(req_hash)"
if [ ! -f "$REQ_HASH_FILE" ] || [ "$(cat "$REQ_HASH_FILE" 2>/dev/null || true)" != "$CUR_HASH" ]; then
  echo "依存関係をインストールします（requirements.txt / constraints.txt の変更を検出）..."
  "$VENV/bin/python" -m pip install -r requirements.txt -c constraints.txt
  echo "$CUR_HASH" > "$REQ_HASH_FILE"
else
  echo "依存関係は最新です（requirements.txt / constraints.txt 変更なし・インストールを省略）。"
fi

# 以降の起動で使う Python は venv 側。
export PYTHON_BIN="$VENV/bin/python"
export SHERPA_PORT="$PORT"

# --- ポートの整合・占有検査（compose up の**前**）。不一致（compose 5433・アプリ 5432＝他人の DB へ繋ぐ）や
#     他プロセスの占有は起動後に気づくと分かりづらいので、ここで表を出して止める（原因と直し方は検査側が出す）。
if ! ./scripts/check-ports.sh; then
  echo "" >&2
  echo "ポートの検査で問題が見つかったため起動を中止しました。上の「直し方」に従って設定ファイルを直し、再度 make start してください。" >&2
  exit 1
fi

# --- ストア（PostgreSQL / Elasticsearch / Neo4j）起動 → healthy 3つを待つ ---
echo "ストア（PostgreSQL / Elasticsearch / Neo4j）を起動します..."
sherpa_compose up -d

echo "ローカルデータを準備し、ストアの起動を待ちます..."
# bootstrap.sh が .env 用意・ディレクトリ作成・3ストアの healthy 待ち（タイムアウト付き）を行う。
./scripts/bootstrap.sh

# OCR ワーカー（画像内文字の読み取り）。登録済み資料フォルダを見て起動先を決めるため、
# ストアが healthy になった後に呼ぶ。前提が足りなければ案内だけ出して起動は続行する。
./scripts/ocr-up.sh || true

# OpenAI API キーの案内（未設定でもブロックしない）
check_openai_key

# --- LAN 公開の bind 決定 ---
if [ "$LAN" = "1" ]; then
  export SHERPA_HOST="0.0.0.0"
  echo ""
  echo "LAN 公開モード: アプリを 0.0.0.0:${PORT} で待受します（同一ネットワークの他端末から到達可）。"
fi

# バックグラウンド起動ヘルパ: 子プロセス自身が PID を書き、指定コマンドを exec する
# （exec で PID がそのコマンドに引き継がれる＝PID ファイルの pid = 実プロセスの pid）。
# setsid があれば新セッション化（起動元ターミナルを閉じても止まらない・PID=PGID=SID）。
# 無い環境（stock macOS 等）では nohup 単独にフォールバックする（RV High-2）。
# その場合 group kill（stop.sh の `kill -TERM -$pid`）は失敗し、単独 kill にフォールバックする
# 既存経路でそのまま正しく動く（exec 連鎖のため pid ファイルの pid = 実プロセスの pid は変わらない）。
launch_bg() {  # $1=ログファイル  $2=pidファイル  $3...=exec するコマンドと引数
  local log="$1" pf="$2"
  shift 2
  if command -v setsid >/dev/null 2>&1; then
    setsid bash -c 'echo $$ > "$0"; exec "$@"' "$pf" "$@" >"$log" 2>&1 < /dev/null &
  else
    nohup bash -c 'echo $$ > "$0"; exec "$@"' "$pf" "$@" >"$log" 2>&1 < /dev/null &
  fi
}

# --- アプリをバックグラウンド起動 ---
# 前回ログが非空なら退避してから空で作り直す（起動のたびに上書きしない・LOG-2）。
sherpa_rotate_log "$APP_LOG"
echo "Sherpa アプリを起動します（モード: $MODE・ログ: ${APP_LOG#$ROOT/}）..."
launch_bg "$APP_LOG" "$APP_PID_FILE" ./scripts/run-api.sh "$MODE"

# healthz が応答するまで待つ（既定30秒）。
APP_WAIT="${SHERPA_APP_WAIT:-30}"
app_deadline=$(( $(date +%s) + APP_WAIT ))
until healthz_ok; do
  # 起動途中でプロセスが死んだら、ログの末尾を見せて失敗終了する（pid ファイルの残骸も掃除）。
  if ! live_pid "$APP_PID_FILE" >/dev/null; then
    echo "" >&2
    echo "✗ アプリの起動に失敗しました。ログの末尾:" >&2
    tail -n 20 "$APP_LOG" >&2 || true
    rm -f "$APP_PID_FILE"
    exit 1
  fi
  if [ "$(date +%s)" -ge "$app_deadline" ]; then
    echo "" >&2
    echo "✗ アプリの起動確認（healthz）が ${APP_WAIT}秒でタイムアウトしました。" >&2
    # タイムアウトした起動プロセスを残さない（RV Med-2）: 停止してから pid ファイルを消す。
    if pid="$(live_pid "$APP_PID_FILE")"; then
      echo "  起動したプロセス（pid $pid）を停止します..." >&2
      kill -TERM "-$pid" 2>/dev/null || kill -TERM "$pid" 2>/dev/null || true
      sleep 1
      if kill -0 "$pid" 2>/dev/null; then
        kill -KILL "-$pid" 2>/dev/null || kill -KILL "$pid" 2>/dev/null || true
      fi
    fi
    rm -f "$APP_PID_FILE"
    echo "  ログ末尾: ${APP_LOG#$ROOT/}" >&2
    tail -n 20 "$APP_LOG" >&2 || true
    exit 1
  fi
  sleep 1
done

# healthz が通っても、それが我々の起動した pid によるものかを最終確認する（RV Med-1: AND 判定）。
# 一致しなければ、別プロセスが同じポートで応答している（起動の途中で奪われた等）とみなし失敗させる。
if ! live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE" >/dev/null; then
  cat >&2 <<EOF
✗ healthz は応答しましたが、起動した Sherpa プロセス（pid ファイル）と一致しません。
  ポート ${PORT} を別プロセスが使っている可能性があります。make status で確認してください。
EOF
  exit 1
fi

# --- LAN 公開: Caddy（HTTPS）起動 or 平文 HTTP の警告 ---
HOSTNAME_SHORT="$(hostname 2>/dev/null || echo localhost)"
caddy_started=0
if [ "$LAN" = "1" ]; then
  if command -v caddy >/dev/null 2>&1 && [ -f "$ROOT/deploy/Caddyfile" ]; then
    if pid="$(live_matching_pid "$CADDY_PID_FILE" "$CADDY_PROC_NEEDLE")"; then
      echo "Caddy は既に起動しています（pid $pid）。"
      caddy_started=1
    else
      [ -f "$CADDY_PID_FILE" ] && rm -f "$CADDY_PID_FILE"
      sherpa_rotate_log "$CADDY_LOG"
      echo "Caddy（リバースプロキシ/HTTPS）を起動します（ログ: ${CADDY_LOG#$ROOT/}）..."
      launch_bg "$CADDY_LOG" "$CADDY_PID_FILE" caddy run --config "$ROOT/deploy/Caddyfile"
      sleep 2
      if live_pid "$CADDY_PID_FILE" >/dev/null; then
        caddy_started=1
      else
        echo "⚠ Caddy の起動に失敗しました（ログ: ${CADDY_LOG#$ROOT/}）。" >&2
        echo "  443 バインドやローカルCA導入に管理者権限が要る場合があります。" >&2
        [ -f "$CADDY_PID_FILE" ] && rm -f "$CADDY_PID_FILE"
      fi
    fi
  else
    # Caddy 無し／設定無し ＝ 平文 HTTP で LAN 公開。2026-08-17 から serve モードでもこれで動く
    # （cookie の Secure は「HTTPS で来た要求にだけ」付く＝HTTP 公開ではログインできる。sherpa/routers/auth.py）。
    # 閉域網の社内 LAN はこの形が標準。HTTPS が要るのは LAN を信用しない場合だけ。
    cat >&2 <<EOF

ⓘ LAN 公開: 平文 HTTP で公開します（http://<このホストのIP>:${PORT}/ui/chat.html）。
   閉域網の社内 LAN ならこのままログインして使えます（cookie に Secure は付きません）。
   LAN を信用しない環境で暗号化したい場合は Caddy で HTTPS 化できます:
     1) https://caddyserver.com/ から caddy を導入
     2) cp deploy/Caddyfile.example deploy/Caddyfile して <host> を編集
     3) LAN=1 ./scripts/start.sh で自動起動（https://${HOSTNAME_SHORT}/）
EOF
  fi
fi

# --- 起動完了の案内 ---
echo ""
echo "──────────────────────────────────────────────"
echo "Sherpa を起動しました（モード: $MODE）。"
echo "  ローカル:  $CHAT_URL"
if [ "$LAN" = "1" ]; then
  if [ "$caddy_started" = 1 ]; then
    echo "  LAN(HTTPS): https://${HOSTNAME_SHORT}/ui/chat.html  （Caddy 経由・証明書はローカルCA）"
    echo "             （直接の http://<host>:${PORT} でも動きます。cookie の Secure は HTTPS 側にだけ付きます）"
  else
    echo "  LAN(HTTP):  http://<このホストのIP>:${PORT}/ui/chat.html"
  fi
  echo "  WSL2 から Windows LAN へ出すには追加設定が要ります（docs/manual/40-運用.md の「LAN 公開」）。"
fi
echo ""
echo "  状態:   make status"
echo "  停止:   make stop        （アプリ＋Caddy＋ストアを全部停止）"
echo "  再起動: make restart"
echo "  ログ:   tail -f ${APP_LOG#$ROOT/}"
echo "──────────────────────────────────────────────"
