#!/usr/bin/env bash
# make backup で取ったバックアップからストア・個人領域を戻す。
#
#   make restore FROM=data/backups/20260818-103000        # 内容を表示 → `yes` で実行
#   make restore FROM=... YES=1                            # 確認せず実行（スクリプト用）
#
# 手順:
#   1. MANIFEST の sha256 を照合（1つでも合わなければ何もせず止まる）
#   2. アプリ/ストア停止中を要求（動いていれば止めない・make stop を案内）
#   3. 各ボリューム: docker volume rm → create → tar 展開（バックアップに含まれるものだけ）
#   4. 個人領域: 今のディレクトリを <dir>.before-restore-<日時> へ退避してから展開
#      （派生物 derived.tar.gz があれば同様）
#   5. env は**上書きしない**（今の .env と差分があればキー名だけ表示・接続先や鍵は手で判断する）
#
# ボリュームは消して作り直す（上書き展開だと消えたはずのファイルが残って壊れる）。今の内容を残したい
# なら、復元の**前に** make backup でもう1つ取っておく（この script は退避しない・案内するだけ）。
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_env_default SHERPA_USERS_DIR SHERPA_DERIVED_DIR SHERPA_COMPOSE_PROJECT COMPOSE_PROJECT_NAME

note()  { echo "・ $*"; }
ok()    { echo "OK: $*"; }
warn()  { echo "ⓘ  $*" >&2; }
fail()  { echo "✗ $*" >&2; }

usage() {
  cat <<'EOF'
使い方: scripts/restore.sh <backup_dir>   （YES=1 で確認省略）
  <backup_dir> は make backup が作った data/backups/<日時>/（MANIFEST を含む）
環境変数: SHERPA_COMPOSE_PROJECT / COMPOSE_PROJECT_NAME（復元先ボリュームの接頭辞。既定は MANIFEST の project → sherpa-mvp）
          SHERPA_USERS_DIR / SHERPA_DERIVED_DIR（既定は .env → data/users, data/derived）
EOF
}

YES="${YES:-0}"
SRC=""
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --yes) YES=1 ;;
    -*) fail "不明な引数: $1"; usage >&2; exit 2 ;;
    *) [ -z "$SRC" ] || { fail "引数が多すぎます"; usage >&2; exit 2; }; SRC="$1" ;;
  esac
  shift
done
[ -n "$SRC" ] || { usage >&2; exit 2; }

abspath() {
  local path
  case "$1" in /*) path="$1" ;; *) path="$ROOT/${1#./}" ;; esac
  realpath -m -- "$path"
}
path_contains() {  # $1 が $2 と同じ、またはその祖先
  [ "$1" = "$2" ] || case "$2" in "$1"/*) return 0 ;; *) return 1 ;; esac
}
paths_overlap() { path_contains "$1" "$2" || path_contains "$2" "$1"; }

SRC="$(abspath "$SRC")"
MANIFEST="$SRC/MANIFEST"
[ -d "$SRC" ] || { fail "バックアップディレクトリがありません: $SRC"; exit 1; }
[ -f "$MANIFEST" ] || { fail "MANIFEST がありません（make backup で作ったディレクトリを指定してください）: $SRC"; exit 1; }
[ ! -L "$MANIFEST" ] || { fail "MANIFEST は symlink を受け付けません: $MANIFEST"; exit 1; }
grep -q '^sherpa_backup=1' "$MANIFEST" || { fail "MANIFEST の形式が違います: $MANIFEST"; exit 1; }

manifest_get() { grep -E "^$1=" "$MANIFEST" | head -1 | cut -d= -f2- || true; }

SRC_PROJECT="$(manifest_get project)"
[[ "$SRC_PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  fail "MANIFEST の project 名が不正です: $SRC_PROJECT"; exit 1;
}
[ "$(manifest_get complete)" = 1 ] || {
  fail "バックアップに完成印 complete=1 がありません（取得途中のディレクトリの疑い）: $SRC"; exit 1;
}

# --- 1. sha256 照合（何かを消す前に必ず） ---------------------------------------
note "MANIFEST の sha256 を照合中..."
declare -A ALLOWED=() SEEN=()
ALLOWED[env]=1
ALLOWED[users.tar.gz]=1
ALLOWED[derived.tar.gz]=1
for suffix in pg neo4j es; do ALLOWED["volumes/${SRC_PROJECT}_${suffix}.tar.gz"]=1; done

SUM_COUNT=0
while IFS= read -r line; do
  [[ "$line" =~ ^([0-9a-f]{64})\ \ (.+)$ ]] || continue
  expected="${BASH_REMATCH[1]}"
  rel="${BASH_REMATCH[2]}"
  if [ -z "${ALLOWED[$rel]:-}" ]; then
    fail "MANIFEST に許可されていない復元対象があります: $rel"
    exit 1
  fi
  if [ -n "${SEEN[$rel]:-}" ]; then
    fail "MANIFEST に同じ復元対象が重複しています: $rel"
    exit 1
  fi
  payload="$SRC/$rel"
  if [ ! -f "$payload" ] || [ -L "$payload" ]; then
    fail "MANIFEST の復元対象が通常ファイルではありません: $rel"
    exit 1
  fi
  actual="$(sha256sum "$payload" | awk '{print $1}')"
  if [ "$actual" != "$expected" ]; then
    fail "sha256 が一致しないファイルがあります: $rel"
    fail "復元を中止します（何も変更していません）。"
    exit 1
  fi
  SEEN["$rel"]=1
  SUM_COUNT=$((SUM_COUNT + 1))
done < <(sed -n '/^\[sha256\]$/,$p' "$MANIFEST")

if [ "$SUM_COUNT" = 0 ]; then
  fail "MANIFEST に sha256 が1件もありません（バックアップが途中で失敗している疑い）: $MANIFEST"
  exit 1
fi

# checksum 対象外の追加 payload も拒否する。旧実装は volumes/*.tar.gz を glob していたため、
# MANIFEST 外の victim.tar.gz を置くだけで任意名の Docker volume が削除対象になっていた。
while IFS= read -r -d '' payload; do
  rel="${payload#"$SRC/"}"
  [ "$rel" = MANIFEST ] && continue
  if [ -z "${ALLOWED[$rel]:-}" ]; then
    fail "MANIFEST にないファイルがあります: $rel（復元対象へ混入させないため中止）"
    exit 1
  fi
  if [ -z "${SEEN[$rel]:-}" ]; then
    fail "MANIFEST にないファイルがあります: $rel（sha256 未照合）"
    exit 1
  fi
done < <(find "$SRC" -mindepth 1 ! -type d -print0)
ok "sha256 照合OK（$SUM_COUNT ファイル・未登録 payload なし）"

# --- 2. 対象の決定 ------------------------------------------------------------------
PROJECT="${SHERPA_COMPOSE_PROJECT:-${COMPOSE_PROJECT_NAME:-$SRC_PROJECT}}"
PROJECT="${PROJECT:-sherpa-mvp}"
USERS="$(abspath "${SHERPA_USERS_DIR:-data/users}")"
DERIVED="$(abspath "${SHERPA_DERIVED_DIR:-data/derived}")"
ENV_FILE="$(sherpa_dotenv_file)"
STAMP="$(date +%Y%m%d-%H%M%S)"

[[ "$PROJECT" =~ ^[a-z0-9][a-z0-9_-]*$ ]] || {
  fail "復元先 compose project 名が不正です: $PROJECT"; exit 1;
}

VOL_TARS=()
for suffix in pg neo4j es; do
  f="$SRC/volumes/${SRC_PROJECT}_${suffix}.tar.gz"
  [ -f "$f" ] && VOL_TARS+=("$f")
done

# tar 自体を全件読めることを、volume や現在の個人領域へ触る前に確認する。
ARCHIVES=("${VOL_TARS[@]}")
[ -f "$SRC/users.tar.gz" ] && ARCHIVES+=("$SRC/users.tar.gz")
[ -f "$SRC/derived.tar.gz" ] && ARCHIVES+=("$SRC/derived.tar.gz")
for f in "${ARCHIVES[@]}"; do
  if ! tar tzf "$f" >/dev/null; then
    fail "tar.gz を読み取れません: ${f#"$SRC/"}。復元を中止します（何も変更していません）。"
    exit 1
  fi
  # 一覧全体を shell 変数へ保持しない（大規模 ES volume でメモリを食い尽くさない）。grep は -q を
  # 使わず最後まで読み、pipefail 下で tar の SIGPIPE を誤判定しない。
  if tar tzf "$f" | grep -E '(^/|(^|/)\.\.(/|$))' >/dev/null; then
    fail "展開先の外を指す危険な path が tar.gz にあります: ${f#"$SRC/"}"
    exit 1
  fi
done

# 現在領域の退避先が既にある場合や、repo/backup 自体を包含する広すぎる path は、volume を消す前に拒否する。
for spec in "個人領域:$USERS:users.tar.gz" "派生物:$DERIVED:derived.tar.gz"; do
  label="${spec%%:*}"; rest="${spec#*:}"; dir="${rest%%:*}"; archive="${rest#*:}"
  [ -f "$SRC/$archive" ] || continue
  if [ "$dir" = / ] || path_contains "$dir" "$ROOT" || paths_overlap "$dir" "$SRC"; then
    fail "$label の復元先が広すぎるか、repo/バックアップ元と重なっています: $dir"
    exit 1
  fi
  if [ -e "$dir.before-restore-$STAMP" ] || [ -L "$dir.before-restore-$STAMP" ]; then
    fail "$label の退避先が既にあります: $dir.before-restore-$STAMP"
    exit 1
  fi
done
if [ -f "$SRC/users.tar.gz" ] && [ -f "$SRC/derived.tar.gz" ] && paths_overlap "$USERS" "$DERIVED"; then
  fail "個人領域と派生物の復元先が重なっています: $USERS / $DERIVED"
  exit 1
fi

APP_RUNNING=""
# アプリの稼働判定は project 名に依らない（pid ファイルはこのリポジトリのアプリを指す・RV）。
if APP_RUNNING="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE" 2>/dev/null)"; then
  fail "アプリが稼働中です（pid=$APP_RUNNING）。個人領域/ストアへの書込みを止めるため、make stop してから再実行してください。"
  exit 1
fi

if [ ${#VOL_TARS[@]} -gt 0 ]; then
  # docker の呼び出しは SHERPA_DOCKER で差し替え可能（install_offline_kit.sh は sudo docker 運用のとき
# DOCKER_CMD="sudo docker" をここへ渡す。以前は素の docker 固定で、その経路では必ず失敗していた・RV）。
_docker() { ${SHERPA_DOCKER:-docker} "$@"; }
command -v docker >/dev/null 2>&1 || { fail "docker が見つかりません（ボリュームの復元に必要）"; exit 1; }
  _docker info >/dev/null 2>&1 || { fail "Docker が起動していません"; exit 1; }
  # ボリューム名はバックアップ時のもの（<元project>_pg）。復元先 project が違えば接頭辞を付け替える。
  # 参照コンテナは**停止中も含めて**（docker ps -a）検出する。`docker volume rm` は停止中のコンテナが
  # 掴んでいるだけでも失敗し、削除ループの途中で止まると「pg だけ backup 版・他は現状」の部分復元に
  # なる（RV 2026-08-18 実測: `backup --stop`（docker stop）直後の restore がこの経路で失敗）。
  # 1本でも参照があれば**何も消す前に**中止する。
  ATTACHED_ALL=""
  for f in "${VOL_TARS[@]}"; do
    base="$(basename "$f" .tar.gz)"
    vol="${base/#${SRC_PROJECT}_/${PROJECT}_}"
    attached="$(_docker ps -a --format '{{.Names}}\t{{.State}}' --filter "volume=$vol" 2>/dev/null || true)"
    ATTACHED_ALL="${ATTACHED_ALL}${ATTACHED_ALL:+$'\n'}$attached"
  done
  ATTACHED_ALL="$(printf '%s\n' "$ATTACHED_ALL" | sed '/^$/d' | sort -u)"
  if [ -n "$ATTACHED_ALL" ]; then
    fail "ボリュームを参照しているコンテナがあります（稼働中/停止中を問わず、掴まれたままでは差し替えられません）:"
    while IFS=$'\t' read -r cname cstate; do fail "    $cname（$cstate）"; done <<<"$ATTACHED_ALL"
    fail "  make stop（compose down＝コンテナは削除・ボリュームは残る）を実行してから再実行してください。"
    fail "  compose 管理外のコンテナなら docker rm <名前> で外してください。"
    exit 1
  fi
  IMAGES="$(_docker image ls --format '{{.Repository}}:{{.Tag}}' | grep -v '<none>' || true)"
  IMAGE=""
  for cand in postgres:16 ubuntu:24.04 neo4j:5-community sherpa/es-kuromoji:8.19.20; do
    if printf '%s\n' "$IMAGES" | grep -qxF "$cand"; then IMAGE="$cand"; break; fi
  done
  [ -n "$IMAGE" ] || { fail "tar を実行するための既知の docker イメージがありません"; exit 1; }
fi

echo "=== Sherpa 復元 ==="
echo "元:            $SRC（$(manifest_get version) / $(manifest_get created) / $(manifest_get host)）"
echo "プロジェクト:  $PROJECT"
for f in "${VOL_TARS[@]}"; do
  base="$(basename "$f" .tar.gz)"
  echo "ボリューム:    ${base/#${SRC_PROJECT}_/${PROJECT}_}  ← $(basename "$f")（消して作り直す）"
done
[ -f "$SRC/users.tar.gz" ]   && echo "個人領域:      $USERS  （今の内容は $USERS.before-restore-$STAMP へ退避）"
[ -f "$SRC/derived.tar.gz" ] && echo "派生物:        $DERIVED  （今の内容は $DERIVED.before-restore-$STAMP へ退避）"
[ -f "$SRC/env" ]            && echo ".env:          上書きしない（$ENV_FILE と差分があれば表示のみ）"
echo ""
echo "今のストア内容は消えます。残したいなら先に make backup をもう1つ取ってください。"
if [ "$YES" != "1" ]; then
  printf '実行するなら yes と入力してください: '
  read -r answer
  if [ "$answer" != "yes" ]; then echo "中止しました（何も変更していません）。"; exit 0; fi
fi

# --- 3. ボリューム ---------------------------------------------------------------------
# rm の前に、実際に展開に使うイメージ＋bind mount で全 tar が読めることを確かめる（ホスト側の
# `tar tzf` はコンテナ内の可読性を保証しない）。ここで転べば何も消していない。
for f in "${VOL_TARS[@]}"; do
  if ! _docker run --rm --entrypoint tar -v "$SRC/volumes:/b:ro" "$IMAGE" tzf "/b/$(basename "$f")" >/dev/null 2>&1; then
    fail "コンテナ内から $(basename "$f") を読めません（bind mount/権限/SELinux ラベルを確認）。何も変更していません。"
    exit 1
  fi
done
for f in "${VOL_TARS[@]}"; do
  base="$(basename "$f" .tar.gz)"
  vol="${base/#${SRC_PROJECT}_/${PROJECT}_}"
  note "ボリューム $vol を作り直して展開中..."
  if _docker volume inspect "$vol" >/dev/null 2>&1; then
    _docker volume rm "$vol" >/dev/null || { fail "既存 volume を削除できません: $vol（何かが使用中でないか確認してください）"; exit 1; }
  fi
  _docker volume create "$vol" >/dev/null
  _docker run --rm --entrypoint tar -v "$vol:/v" -v "$SRC/volumes:/b:ro" "$IMAGE" \
    xzf "/b/$(basename "$f")" --numeric-owner -C /v
  ok "$vol"
done

# --- 4. 個人領域・派生物（今のものは退避してから展開） --------------------------------------
restore_dir() {  # $1=tar $2=展開先
  local tar="$1" dir="$2" parent tmp before
  parent="$(dirname "$dir")"
  before="$dir.before-restore-$STAMP"
  if ! mkdir -p "$parent"; then
    fail "復元先の親ディレクトリを作れません: $parent"
    return 1
  fi
  if ! tmp="$(mktemp -d "$parent/.sherpa-restore-$(basename "$dir").XXXXXX")"; then
    fail "復元用の一時ディレクトリを作れません: $parent"
    return 1
  fi
  if ! tar xzf "$tar" -C "$tmp"; then
    rm -rf -- "$tmp"
    fail "展開に失敗しました: $tar（現在の $dir は変更していません）"
    return 1
  fi
  if [ -e "$dir" ]; then
    if ! mv "$dir" "$before"; then
      rm -rf -- "$tmp"
      fail "現在の内容を退避できません: $dir → $before"
      return 1
    fi
    note "今の内容を退避: $before"
  elif [ -L "$dir" ]; then
    if ! mv "$dir" "$before"; then
      rm -rf -- "$tmp"
      fail "現在の symlink を退避できません: $dir → $before"
      return 1
    fi
    note "今の symlink を退避: $before"
  fi
  if ! mv "$tmp" "$dir"; then
    if [ -e "$before" ] || [ -L "$before" ]; then mv "$before" "$dir"; fi
    rm -rf -- "$tmp"
    fail "復元済みディレクトリへの切替に失敗しました: $dir"
    return 1
  fi
  ok "$dir"
}
if [ -f "$SRC/users.tar.gz" ]; then restore_dir "$SRC/users.tar.gz" "$USERS"; fi
if [ -f "$SRC/derived.tar.gz" ]; then restore_dir "$SRC/derived.tar.gz" "$DERIVED"; fi

# --- 5. env は上書きしない -----------------------------------------------------------------
if [ -f "$SRC/env" ]; then
  if [ -f "$ENV_FILE" ]; then
    if diff -q "$SRC/env" "$ENV_FILE" >/dev/null 2>&1; then
      ok ".env はバックアップと同一（$ENV_FILE）"
    else
      warn ".env に差分があります（上書きしていません・必要なら手で反映）: $ENV_FILE ← $SRC/env"
      # API key/password を端末ログへ漏らさない。差分行から変数名だけを抽出し、値は一切表示しない。
      CHANGED_KEYS="$(diff -U0 "$ENV_FILE" "$SRC/env" \
        | sed -nE 's/^[+-][[:space:]]*(export[[:space:]]+)?([A-Za-z_][A-Za-z0-9_]*)=.*/\2/p' \
        | sort -u | paste -sd ', ' - || true)"
      if [ -n "$CHANGED_KEYS" ]; then
        warn "  差分のあるキー（値は秘密保護のため非表示）: $CHANGED_KEYS"
      else
        warn "  変数値以外（コメント/書式）に差分があります。値は表示しません。"
      fi
    fi
  else
    warn "現在の .env がありません（$ENV_FILE）。バックアップの $SRC/env を参考に用意してください（自動では置きません）。"
  fi
fi

echo ""
ok "復元完了。次は make start で起動して確認してください。"
