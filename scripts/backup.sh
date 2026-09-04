#!/usr/bin/env bash
# Sherpa のデータを丸ごと退避する（更新の切替前・障害前の保険）。
#
#   make backup                     # 停止中のアプリ/ストア＋個人領域＋.env を data/backups/<日時>/ へ
#   make backup ARGS=--stop         # 動いていれば止めてから取る（make stop 相当）
#   make backup ARGS=--with-derived # 派生物（再取り込みで再生成できるもの）も含める
#   make backup ARGS=--dry-run      # 何をどこへ取るかの計画だけ表示（何も書かない）
#
# 取るもの（<SHERPA_BACKUP_DIR>/<YYYYmmdd-HHMMSS>/）:
#   volumes/<vol>.tar.gz ×3   ストアのボリューム（<project>_pg / _neo4j / _es）
#   users.tar.gz              個人領域（SHERPA_USERS_DIR・既定 data/users）
#   derived.tar.gz            派生物（--with-derived 時のみ・SHERPA_DERIVED_DIR）
#   env                       .env のコピー（API キー等の秘密を含む → dir 0700 / file 0600）
#   MANIFEST                  版・日時・元パス・イメージ ID・各 tar の sha256（restore が照合する）
#
# **ストアは停止中であることを要求する。** 動作中のボリュームを tar すると、Postgres の WAL と
# データファイル・ES のセグメントとトランスログ・Neo4j のストアとログが別々の瞬間で読まれ、
# 復元しても起動できない／黙って一部が欠けた状態になり得る。整合した1点を取るには止めるのが
# 一番確実で安い（このシステムの規模なら停止は数十秒で済む）。
#
# tar は docker のワークコンテナで実行する（`docker run --rm -v <vol>:/v:ro -v <dest>:/b <image> tar ...`）。
# 閉域では使えるイメージが限られるので、image は手元にあるものから選ぶ（postgres:16 → ubuntu:24.04 → …）。
#
# make nuke は data/backups を消さない（nuke.sh の TARGETS に含まれない）＝初期化してもここは残る。
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# .env の読み方は run-common.sh に一本化（明示指定 ＞ .env ＞ 既定・SHERPA_ENV_FILE 対応）。
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_env_default SHERPA_USERS_DIR SHERPA_DERIVED_DIR SHERPA_BACKUP_DIR SHERPA_COMPOSE_PROJECT COMPOSE_PROJECT_NAME

note()  { echo "・ $*"; }
ok()    { echo "OK: $*"; }
warn()  { echo "ⓘ  $*" >&2; }
fail()  { echo "✗ $*" >&2; }

usage() {
  cat <<'EOF'
使い方: scripts/backup.sh [--stop] [--with-derived] [--dry-run]
  --stop          ストア（＋アプリ）が動いていれば止めてから取る（make stop 相当）
  --with-derived  派生物（SHERPA_DERIVED_DIR）も含める（再取り込みで再生成できるので既定は含めない）
  --dry-run       計画だけ表示して何も書かない
環境変数: SHERPA_BACKUP_DIR（既定 data/backups）/ SHERPA_COMPOSE_PROJECT（既定 sherpa-mvp＝ボリューム名の接頭辞）
          SHERPA_USERS_DIR / SHERPA_DERIVED_DIR / SHERPA_ENV_FILE（.env の場所）
EOF
}

STOP=0; WITH_DERIVED=0; DRY_RUN=0
while [ $# -gt 0 ]; do
  case "$1" in
    --stop) STOP=1 ;;
    --with-derived) WITH_DERIVED=1 ;;
    --dry-run) DRY_RUN=1 ;;
    -h|--help) usage; exit 0 ;;
    *) fail "不明な引数: $1"; usage >&2; exit 2 ;;
  esac
  shift
done

# 相対指定は「リポジトリ基準」に揃え、既存 symlink も実体へ解決する。
abspath() {
  local path
  case "$1" in /*) path="$1" ;; *) path="$ROOT/${1#./}" ;; esac
  realpath -m -- "$path"
}
path_contains() {  # $1 が $2 と同じ、またはその祖先
  [ "$1" = "$2" ] || case "$2" in "$1"/*) return 0 ;; *) return 1 ;; esac
}
paths_overlap() { path_contains "$1" "$2" || path_contains "$2" "$1"; }

PROJECT="${SHERPA_COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-sherpa-mvp}}"
BACKUP_DIR="$(abspath "${SHERPA_BACKUP_DIR:-data/backups}")"
USERS="$(abspath "${SHERPA_USERS_DIR:-data/users}")"
DERIVED="$(abspath "${SHERPA_DERIVED_DIR:-data/derived}")"
ENV_FILE="$(sherpa_dotenv_file)"
STAMP="$(date +%Y%m%d-%H%M%S)"
DEST="$BACKUP_DIR/$STAMP"
VOLUMES=("${PROJECT}_pg" "${PROJECT}_neo4j" "${PROJECT}_es")

if [ -n "${SHERPA_ENV_FILE:-}" ] && [ ! -f "$ENV_FILE" ]; then
  fail "指定された SHERPA_ENV_FILE がありません: $ENV_FILE（env を欠いた不完全なバックアップは作りません）"
  exit 1
fi

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  fail "compose project 名が不正です: $PROJECT（小文字英数字で始まる英数字・_・- のみ）"; exit 1;
}
# chmod/tar の対象に広すぎるパスや自己包含を許さない。BACKUP_DIR が users/derived の中にあると、
# 作成中のバックアップ自身を tar が再帰的に読み続けるため、実行前に止める。
if [ "$BACKUP_DIR" = / ] || [ "$BACKUP_DIR" = "$ROOT" ] || path_contains "$BACKUP_DIR" "$ROOT"; then
  fail "SHERPA_BACKUP_DIR が広すぎます: $BACKUP_DIR（専用のサブディレクトリを指定してください）"; exit 1
fi
for spec in "個人領域:$USERS" "派生物:$DERIVED"; do
  label="${spec%%:*}"; dir="${spec#*:}"
  if [ "$dir" = / ] || path_contains "$dir" "$ROOT"; then
    fail "$label のパスが広すぎます: $dir（専用のサブディレクトリを指定してください）"; exit 1
  fi
done
if paths_overlap "$BACKUP_DIR" "$USERS" || { [ "$WITH_DERIVED" = 1 ] && paths_overlap "$BACKUP_DIR" "$DERIVED"; }; then
  fail "バックアップ先を個人領域/派生物の内側（またはその親）には置けません。tar の自己包含を避けるため別の専用ディレクトリを指定してください。"
  exit 1
fi
if [ "$WITH_DERIVED" = 1 ] && paths_overlap "$USERS" "$DERIVED"; then
  fail "SHERPA_USERS_DIR と SHERPA_DERIVED_DIR が重なっています: $USERS / $DERIVED"
  exit 1
fi

# docker の呼び出しは SHERPA_DOCKER で差し替え可能（install_offline_kit.sh は sudo docker 運用のとき
# DOCKER_CMD="sudo docker" をここへ渡す。以前は素の docker 固定で、その経路では必ず失敗していた・RV）。
_docker() { ${SHERPA_DOCKER:-docker} "$@"; }
command -v docker >/dev/null 2>&1 || { fail "docker が見つかりません（ボリュームの退避に必要）"; exit 1; }
_docker info >/dev/null 2>&1 || { fail "Docker が起動していません（Docker Desktop / dockerd を起動してください）"; exit 1; }

# --- 停止中の確認 -----------------------------------------------------------
# コンテナ名の規則ではなく、対象 volume を実際に mount している稼働コンテナを調べる。
# 手動接続されたコンテナや compose の命名変更も見逃さない。
running_containers() {
  local v
  for v in "${VOLUMES[@]}"; do
    _docker ps --format '{{.Names}}' --filter "volume=$v"
  done | sort -u
}
RUNNING="$(running_containers)"
# アプリの稼働判定は compose の project 名と無関係（pid ファイルはこのリポジトリのアプリを指す）＝常に見る。
# 以前は project=sherpa-mvp のときだけ見ていたため、別 project 名では稼働中でも個人領域を tar できていた（RV）。
APP_RUNNING="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE" 2>/dev/null || true)"
if { [ -n "$RUNNING" ] || [ -n "$APP_RUNNING" ]; } && [ "$STOP" = 1 ] && [ "$DRY_RUN" = 0 ]; then
  note "アプリ/ストアが動いているので止めます（--stop）: app=${APP_RUNNING:-停止} containers=$(echo "${RUNNING:-停止}" | tr '\n' ' ')"
  # app/Caddy は個人領域への書込みも止める。ストアは対象 volume を使うコンテナだけを明示停止する。
  KEEP_STORES=1 ./scripts/stop.sh
  if [ -n "$RUNNING" ]; then
    # 停止は「止める」ではなく「外す」（docker stop だとコンテナがボリュームを掴んだまま残り、
    # 直後の restore が `volume is in use` で失敗する・RV 実測）。compose 管理下なら down（ボリュームは残る）、
    # それ以外は stop→rm。
    # shellcheck disable=SC2086
    if [ "$PROJECT" = "${COMPOSE_PROJECT_NAME:-sherpa-mvp}" ] || [ "$PROJECT" = sherpa-mvp ]; then
      sherpa_compose --profile ocr down >/dev/null 2>&1 || { _docker stop $RUNNING >/dev/null; _docker rm $RUNNING >/dev/null; }
    else
      _docker stop $RUNNING >/dev/null; _docker rm $RUNNING >/dev/null
    fi
  fi
  RUNNING="$(running_containers)"
  APP_RUNNING="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE" 2>/dev/null || true)"
fi
if [ -n "$RUNNING" ] || [ -n "$APP_RUNNING" ]; then
  ACTIVE="app=${APP_RUNNING:-停止} containers=$(echo "${RUNNING:-停止}" | tr '\n' ' ')"
  if [ "$DRY_RUN" = 1 ]; then
    warn "アプリ/ストアが稼働中です（実行時は失敗します）: $ACTIVE"
    warn "  make stop してから、または --stop 付きで実行してください。"
  else
    fail "アプリ/ストアが稼働中です: $ACTIVE"
    fail "  動作中のボリューム/個人領域を tar すると不整合になり得るため取りません。"
    fail "  make stop してから実行するか、--stop を付けてください（make backup ARGS=--stop）。"
    exit 3   # 3=稼働中（呼び出し側が「警告して続行」と「本当の失敗」を区別できるように）
  fi
fi

# --- ワークイメージの選択 ---------------------------------------------------
# 閉域では pull できないので、手元にあるものだけが候補。root で動く小さめのものを優先する
# （postgres:16 は root・tar 同梱・キットに必ずある。ES は非 root ユーザで動くため後回し）。
IMAGES="$(_docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' || true)"
IMAGE=""
for cand in postgres:16 ubuntu:24.04 neo4j:5-community sherpa/es-kuromoji:8.19.20; do
  if printf '%s\n' "$IMAGES" | grep -qxF "$cand"; then IMAGE="$cand"; break; fi
done
if [ -z "$IMAGE" ]; then
  fail "tar を実行するための既知の docker イメージがありません。"
  fail "  postgres:16 か ubuntu:24.04 を docker load してから再実行してください。"
  exit 1
fi

# --- 対象の確認 ---------------------------------------------------------------
MISSING_VOL=()
for v in "${VOLUMES[@]}"; do
  _docker volume inspect "$v" >/dev/null 2>&1 || MISSING_VOL+=("$v")
done

echo "=== Sherpa バックアップ ==="
echo "出力先:        $DEST"
echo "プロジェクト:  $PROJECT"
echo "ボリューム:    ${VOLUMES[*]}"
[ ${#MISSING_VOL[@]} -gt 0 ] && echo "  （存在しないため飛ばす: ${MISSING_VOL[*]}）"
echo "個人領域:      $USERS$( [ -d "$USERS" ] || echo '  (無し→空の状態として取得)')"
if [ "$WITH_DERIVED" = 1 ]; then
  echo "派生物:        $DERIVED$( [ -d "$DERIVED" ] || echo '  (無し→空の状態として取得)')"
else
  echo "派生物:        含めない（--with-derived で含める。再取り込みで再生成可）"
fi
echo ".env:          $ENV_FILE$( [ -f "$ENV_FILE" ] || echo '  (無し→飛ばす)')"
echo "ワークイメージ: $IMAGE"
echo ""

if [ ${#MISSING_VOL[@]} -gt 0 ]; then
  if [ "$DRY_RUN" = 1 ]; then
    warn "必要な3 volumeが揃っていないため、実行時は失敗します: ${MISSING_VOL[*]}"
  else
    fail "必要な3 volumeが揃っていません: ${MISSING_VOL[*]}"
    fail "  project 名（SHERPA_COMPOSE_PROJECT / COMPOSE_PROJECT_NAME）を確認してください。不完全なバックアップは作りません。"
    exit 1
  fi
fi

if [ "$DRY_RUN" = 1 ]; then
  ok "--dry-run のため何も書きませんでした。"
  exit 0
fi

# --- 実行 -----------------------------------------------------------------------
# 秘密（.env）を含むので、ディレクトリは作った瞬間から 0700 にする。
umask 077
BACKUP_DIR_CREATED=0
[ -d "$BACKUP_DIR" ] || { mkdir -p "$BACKUP_DIR"; BACKUP_DIR_CREATED=1; }
if ! mkdir "$DEST"; then
  fail "同じ時刻のバックアップ先が既にあります（混在を防ぐため上書きしません）: $DEST"
  exit 1
fi
mkdir "$DEST/volumes"
chmod 700 "$DEST"
# 置き場（BACKUP_DIR）は自分が作ったときだけ 0700 に絞る。共有 NAS 等の既存ディレクトリを無条件に
# chmod すると他ユーザ運用を壊す／所有者でなければ失敗する（RV）。既存なら権限はそのまま。
[ "$BACKUP_DIR_CREATED" = 1 ] && chmod 700 "$BACKUP_DIR"

MANIFEST="$DEST/MANIFEST"
{
  echo "sherpa_backup=1"
  echo "version=$(cat "$ROOT/VERSION" 2>/dev/null || echo unknown)"
  echo "created=$(date -Iseconds)"
  echo "host=$(hostname)"
  echo "project=$PROJECT"
  echo "work_image=$IMAGE"
  echo "users_dir=$USERS"
  [ "$WITH_DERIVED" = 1 ] && echo "derived_dir=$DERIVED"
  echo "env_file=$ENV_FILE"
  # 復元先のイメージ版が変わっていると DB ファイル形式が合わないことがあるので、取得時点のイメージ ID を残す。
  for ref in $(grep -E '^\s+image:' "$ROOT/docker-compose.yml" 2>/dev/null | awk '{print $2}'); do
    id="$(_docker image inspect --format '{{.Id}}' "$ref" 2>/dev/null || echo '(未取得)')"
    echo "image=$ref $id"
  done
} > "$MANIFEST"

# コンテナ内は root で tar を作る（Postgres 等のデータは 0700・別 uid のため非 root では読めない）。
# 出来た tar は root 所有になり得るので、自分の uid に chown してから 0600 にする（rootless docker
# なら最初から自分の所有）。--numeric-owner: 復元先で uid/名前の対応が違っても元の uid をそのまま戻す。
UID_GID="$(id -u):$(id -g)"
for v in "${VOLUMES[@]}"; do
  case " ${MISSING_VOL[*]:-} " in *" $v "*) continue ;; esac
  note "ボリューム $v を退避中..."
  _docker run --rm --entrypoint tar -v "$v:/v:ro" -v "$DEST/volumes:/b" "$IMAGE" \
    czf "/b/$v.tar.gz" --numeric-owner -C /v .
  if [ "$(stat -c %u "$DEST/volumes/$v.tar.gz")" != "$(id -u)" ]; then
    _docker run --rm --entrypoint chown -v "$DEST/volumes:/b" "$IMAGE" "$UID_GID" "/b/$v.tar.gz"
  fi
  chmod 600 "$DEST/volumes/$v.tar.gz"
  ok "volumes/$v.tar.gz ($(du -h "$DEST/volumes/$v.tar.gz" | cut -f1))"
done

if [ -d "$USERS" ]; then
  note "個人領域を退避中: $USERS"
  tar czf "$DEST/users.tar.gz" -C "$USERS" .
  ok "users.tar.gz ($(du -h "$DEST/users.tar.gz" | cut -f1))"
else
  warn "個人領域が見つかりません: $USERS（SHERPA_USERS_DIR が未設定／別の場所にありませんか？）。空の状態として退避します。"
  tar czf "$DEST/users.tar.gz" --files-from /dev/null
  ok "users.tar.gz（空）"
fi
if [ "$WITH_DERIVED" = 1 ] && [ -d "$DERIVED" ]; then
  note "派生物を退避中: $DERIVED"
  tar czf "$DEST/derived.tar.gz" -C "$DERIVED" .
  ok "derived.tar.gz ($(du -h "$DEST/derived.tar.gz" | cut -f1))"
elif [ "$WITH_DERIVED" = 1 ]; then
  note "派生物は未作成のため、空の状態として退避します: $DERIVED"
  tar czf "$DEST/derived.tar.gz" --files-from /dev/null
  ok "derived.tar.gz（空）"
fi
if [ -f "$ENV_FILE" ]; then
  cp "$ENV_FILE" "$DEST/env"
  chmod 600 "$DEST/env"
  ok "env（$ENV_FILE のコピー・0600）"
fi

# sha256 は MANIFEST の末尾に `sha256sum` 形式で並べる（restore が `sha256sum -c` で照合する）。
(
  cd "$DEST"
  echo "[sha256]"
  find . -type f ! -name MANIFEST | sed 's|^\./||' | sort | xargs -r sha256sum
) >> "$MANIFEST"
# 途中失敗したディレクトリを復元元に使わせない。全 payload の checksum を書き終えた最後にだけ完成印を付ける。
echo "complete=1" >> "$MANIFEST"
chmod 600 "$MANIFEST"
chmod 700 "$DEST/volumes"

echo ""
ok "バックアップ完了: $DEST"
echo "  復元は: make restore FROM=$DEST"
echo "  （復元はストア停止中に行い、現在の内容は復元前にもう一度 make backup で退避することを勧めます）"
