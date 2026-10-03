#!/usr/bin/env bash
# Sherpa を「入れたて」の状態へ戻す（完全初期化）。
#
#   make nuke            # 消す対象を一覧表示 → yes → 表示された環境名を入力 → 5 秒の猶予 → 実行
#   make nuke YES=I-UNDERSTAND-ALL-DATA-WILL-BE-DELETED
#                        # 確認を省いて実行（スクリプト/CI 用・この文字列と完全一致のときだけ。本番では不可）
#   make nuke KEEP_ENV=1 # .env は残す（既定でも .env は消さない。将来の拡張用の明示指定）
#
# 消すもの:
#   - ストア（Postgres / Neo4j / Elasticsearch）のデータ＝**アカウント・会話履歴・監査ログ・台帳**
#   - 派生物（Office→MD・Evidence・チャンク）
#   - OCR 観測
#   - 個人領域（`users/{user_id}/workspace`＝アップロードした個人ファイルと出力物）
#   - ローカル KB（`data/kb`）と実行時の pid/ログ
#
# 消さないもの:
#   - **登録した資料フォルダの中身**（読み取り専用＝Sherpa は元から一切書き換えない）
#   - `.env`（接続先や API キーの設定）
#   - `fixtures/`（テストデータ）と `data/eval-*`（評価資産・取得に時間がかかる）
#
# 消す先は `.env` の設定を見て決める。本番のように `SHERPA_DERIVED_DIR=/srv/sherpa/derived`
# としている環境で `data/derived` だけ消して「初期化した」と誤解しないため。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# 確認を省く指定は、長い決まった文字列と完全一致したときだけ（旧 YES=1 は廃止）。
SKIP_TOKEN="I-UNDERSTAND-ALL-DATA-WILL-BE-DELETED"
YES="${YES:-}"
if [ "$YES" = "0" ]; then YES=""; fi
if [ -n "$YES" ] && [ "$YES" != "$SKIP_TOKEN" ]; then
  echo "この指定（YES=${YES}）は廃止しました。何も消していません。" >&2
  echo "使い方: make nuke（対話で 2 回確認します）。スクリプト用に確認を省くときは YES=${SKIP_TOKEN}" >&2
  exit 2
fi

# .env の設定（保存先の指定）を読む。読み方は scripts/run-common.sh に一本化してある
# （呼び出し側の明示指定 ＞ .env ＞ 既定・SHERPA_ENV_FILE 対応）。ここは戻せない操作なので、
# 「画面に出した対象」と「実際に消す対象」が必ず一致することを最優先にする。
# 明示した環境変数ファイルが読めないときは、消す先も本番かどうかも決められないので中止する
if [ -n "${SHERPA_ENV_FILE:-}" ] && { [ ! -f "$SHERPA_ENV_FILE" ] || [ ! -r "$SHERPA_ENV_FILE" ]; }; then
  echo "✗ SHERPA_ENV_FILE で指定したファイルが読めません。何も消していません。" >&2
  exit 1
fi
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_env_default SHERPA_DERIVED_DIR SHERPA_USERS_DIR SHERPA_OBSERVATION_DIR SHERPA_KB_DIR

# 本番では確認を省く指定を受け付けず、必ず対話で確認する。環境変数に加えて環境変数ファイル
# （SHERPA_ENV_FILE 指定を含む）の SHERPA_ENV も見る（どちらかが prod/production なら本番）。
sherpa_env_default SHERPA_ENV
FILE_SHERPA_ENV="$(unset SHERPA_ENV; sherpa_env_default SHERPA_ENV; printf '%s' "${SHERPA_ENV:-}")"
PRODUCTION=0
for _env_value in "${SHERPA_ENV:-}" "$FILE_SHERPA_ENV"; do
  case "$(printf '%s' "$_env_value" | tr '[:upper:]' '[:lower:]')" in
    prod|production) PRODUCTION=1 ;;
  esac
done
if [ -n "$YES" ] && [ "$PRODUCTION" = 1 ]; then
  echo "本番（SHERPA_ENV=production）では確認を省く指定は使えません。何も消していません。" >&2
  echo "make nuke を対話端末で実行し、2 回の確認に答えてください。" >&2
  exit 2
fi

# 相対指定は「リポジトリ基準」に揃える（アプリの規約は cwd 基準だが、make はここで実行される）。
abspath() {  # $1=path
  case "$1" in
    /*) printf '%s\n' "$1" ;;
    *)  printf '%s\n' "$ROOT/${1#./}" ;;
  esac
}

DERIVED="$(abspath "${SHERPA_DERIVED_DIR:-data/derived}")"
USERS="$(abspath "${SHERPA_USERS_DIR:-data/users}")"
OBSERVATIONS="$(abspath "${SHERPA_OBSERVATION_DIR:-data/observations}")"
KB="$(abspath "${SHERPA_KB_DIR:-data/kb}")"
RUN_STATE="$ROOT/data/run"

TARGETS=("$DERIVED" "$USERS" "$OBSERVATIONS" "$KB" "$RUN_STATE")

# --- 安全弁 ---------------------------------------------------------------
# 設定ミス（空文字・`/`・リポジトリ丸ごと）で無関係な木を消さない。ここは戻せない操作なので
# 「怪しければ実行しない」を徹底する。
for target in "${TARGETS[@]}"; do
  if [ -z "$target" ]; then
    echo "✗ 消す先が空です（.env の SHERPA_*_DIR を確認してください）" >&2
    exit 1
  fi
  # 末尾の `/` を落としてから解決する（`/` と `//` と `/data/` を同じものとして扱う）。
  trimmed="$target"
  while [ "$trimmed" != "/" ] && [ "${trimmed%/}" != "$trimmed" ]; do trimmed="${trimmed%/}"; done

  if [ "$trimmed" = "/" ]; then
    resolved="/"
  else
    # 親が実在するなら実体で解決する（symlink 経由で別の木を指していないか見るため）。
    # 親が無い＝まだ作られていない保存先なので、指定文字列のまま検査する。
    parent="$(cd "$(dirname "$trimmed")" 2>/dev/null && pwd -P)" || parent=""
    resolved="${parent:+${parent%/}/$(basename "$trimmed")}"
    resolved="${resolved:-$trimmed}"
  fi

  if [ "$resolved" = "/" ] || [ "$resolved" = "$ROOT" ] || [ "$resolved" = "$HOME" ]; then
    echo "✗ 消す先がルート/リポジトリ/ホームそのものです: $resolved" >&2
    echo "  .env の SHERPA_*_DIR を見直してください。" >&2
    exit 1
  fi
  # `/data` `/srv` のような最上位直下は、他の用途と同居している可能性が高いので拒む
  # （保存先は `/srv/sherpa/derived` のように専用の階層を切って指定する）。
  case "$resolved" in
    /*/*) : ;;                     # 2階層以上＝OK
    /*)   echo "✗ 消す先がファイルシステム直下です: $resolved" >&2
          echo "  専用の階層（例 /srv/sherpa/derived）を指定してください。" >&2
          exit 1 ;;
  esac
done

# 登録した資料フォルダを絶対に消さない（CLAUDE.md「登録ディレクトリ配下は読み取り専用」）。
# レジストリは Postgres にあり、この時点ではまだ止めていないので照会できる。
#
# 「登録が0件」と「照会できなかった」は**別物**なので必ず区別する（0件を照会失敗として毎回
# 警告すると、本当に照会できていないときの警告が埋もれる）。成功時は必ず先頭に OK 行を出す。
WORLD_QUERY="$(.venv/bin/python - <<'PY' 2>/dev/null || true
try:
    from sherpa import store
    rows = store.list_worlds_db()
except Exception:
    raise SystemExit(1)
print("OK")
for row in rows:
    path = (row or {}).get("root_path")
    if path:
        print(path)
PY
)"
if [ "${WORLD_QUERY%%$'\n'*}" = "OK" ]; then
  WORLD_CHECK="ok"
  WORLD_ROOTS="$(printf '%s\n' "$WORLD_QUERY" | tail -n +2)"
else
  # 照会できなかった（ストア停止中・DB 未設定など）。**黙って飛ばさず**画面に出して
  # 利用者に判断してもらう。消す対象は派生物と個人領域だけなので通常は問題ないが、
  # 保存先を資料フォルダと重ねる設定ミスをここで検出できていない状態ではある。
  WORLD_CHECK="skipped"
  WORLD_ROOTS=""
fi
if [ -n "$WORLD_ROOTS" ]; then
  while IFS= read -r world_root; do
    [ -n "$world_root" ] || continue
    for target in "${TARGETS[@]}"; do
      # target が資料フォルダそのもの、またはその祖先なら中止（消すと原本が飛ぶ）。
      case "$world_root/" in
        "$target"/*) echo "✗ 消す先が登録済みの資料フォルダを含みます: $target ⊃ $world_root" >&2
                     echo "  資料フォルダは読み取り専用です。.env の保存先設定を見直してください。" >&2
                     exit 1 ;;
      esac
    done
  done <<< "$WORLD_ROOTS"
fi

# --- 確認 -----------------------------------------------------------------
echo "Sherpa を完全に初期化します。次のものが消えます。"
echo
echo "  ストアのデータ（アカウント・会話履歴・監査ログ・台帳）"
for target in "${TARGETS[@]}"; do
  if [ -e "$target" ]; then
    printf '  %s\n' "$target"
  else
    printf '  %s  (無し)\n' "$target"
  fi
done
echo
echo "  資料フォルダの中身・.env・fixtures・data/eval-* は消しません。"
if [ "$WORLD_CHECK" = "skipped" ]; then
  echo
  echo "  ※ 登録済みの資料フォルダを照会できませんでした（ストア停止中など）。"
  echo "     上の消す先が資料フォルダと重なっていないか、目で確かめてください。"
fi
echo
if [ -z "$YES" ]; then
  # 確認は人が端末で答える前提。端末につながっていなければ確認できないので消さない。
  if [ ! -t 0 ]; then
    echo "標準入力が端末ではなく確認できないため中止しました（何も消していません）。" >&2
    echo "スクリプトから実行するときは YES=${SKIP_TOKEN}（本番では不可）。" >&2
    exit 1
  fi
  echo "すべて消えます。元に戻せません。"
  printf '続けるなら yes と入力してください: '
  read -r answer || answer=""
  if [ "$answer" != "yes" ]; then
    # 利用者が選んだ「やめる」は失敗ではない＝make がエラー表示しないよう 0 で終える。
    echo "中止しました（何も消していません）。"
    exit 0
  fi
  CONFIRM_WORD="$(hostname -s 2>/dev/null || uname -n 2>/dev/null || true)"
  CONFIRM_LABEL="この環境の名前"
  if [ -z "$CONFIRM_WORD" ]; then
    # 名前が取れないときは、その場で作る 6 桁の乱数を確認コードにする（固定値には倒さない）。
    CONFIRM_WORD="$(od -An -N4 -tu4 /dev/urandom 2>/dev/null | tr -d ' \n' || true)"
    if ! [[ "$CONFIRM_WORD" =~ ^[0-9]+$ ]]; then
      echo "確認用の名前も乱数も用意できないため中止しました（何も消していません）。" >&2
      exit 1
    fi
    CONFIRM_WORD="$(printf '%06d' $((CONFIRM_WORD % 1000000)))"
    CONFIRM_LABEL="確認コード"
  fi
  echo
  echo "本当に実行しますか？ 確認のため、次の語を入力してください。"
  echo "  ${CONFIRM_LABEL}: ${CONFIRM_WORD}"
  printf '入力: '
  read -r answer || answer=""
  if [ "$answer" != "$CONFIRM_WORD" ]; then
    echo "名前が一致しません。中止しました（何も消していません）。"
    exit 0
  fi
  COUNTDOWN=5   # 猶予は固定（外から変えられない）
  while [ "$COUNTDOWN" -gt 0 ]; do
    printf '\r%s 秒後に消します（Ctrl+C で中止できます）  ' "$COUNTDOWN"
    sleep 1
    COUNTDOWN=$((COUNTDOWN - 1))
  done
  echo
fi

# --- 実行 -----------------------------------------------------------------
# アプリを先に止める。動いたままだと消した直後に作り直されて中途半端な状態が残る。
echo "==> アプリとストアを停止します"
./scripts/stop.sh || true

# ストアのボリュームごと削除。`--profile ocr` を付けないと OCR ワーカーが compose の管理から
# 外れて残る（profile 指定時だけ対象になる）ため、常に付けて呼ぶ。
echo "==> ストアのデータを削除します"
sherpa_compose --profile ocr down -v --remove-orphans || sherpa_compose down -v || true

echo "==> ローカルの保存先を削除します"
for target in "${TARGETS[@]}"; do
  if [ -e "$target" ]; then
    rm -rf "$target"
    printf '  削除: %s\n' "$target"
  fi
done

echo
echo "初期化しました。次に起動すると、管理者アカウントの作り直しから始まります。"
echo "  make start        # ストアとアプリを起動"
