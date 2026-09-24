#!/usr/bin/env bash
# B-full 本番サンドボックス: OS ユーザ分離＋所有権（docs/08-実行権限と隔離.md §0/§1）。
#
# permission profile（アプリ層・sherpa/agents.py）と併せた**多層防御**の下層を機構化する:
#   - kb（uploads/md/src）は sherpa-ingest 所有・group 書込なし → sherpa-agent は FS レベルで書込不可。
#   - sherpa-agent は sherpa-ingest グループに入れない（所有権で kb 改変を不可能にする）。
#   - users/{uid}/workspace は sherpa-workspace グループで FastAPI/Codex 実行プロセスが書ける。
#     cross-user はアプリが per-user に紐付け＋profile が封じ込める。
#
# 冪等（再実行安全）。root で実行。--dry-run で確認、--verify で現状点検のみ。
#
# 使い方:
#   sudo scripts/setup-runtime-users.sh            # 適用
#   sudo scripts/setup-runtime-users.sh --dry-run  # 何をするか表示のみ
#   sudo scripts/setup-runtime-users.sh --verify    # 所有権/mode/グループを点検
set -euo pipefail

ROOT="${SHERPA_SRV_ROOT:-/srv/sherpa}"
GROUP="sherpa"
G_WORKSPACE="sherpa-workspace"
U_INGEST="sherpa-ingest"
U_AGENT="sherpa-agent"
U_API="sherpa-api"

DRY_RUN=0
VERIFY_ONLY=0
for a in "$@"; do
  case "$a" in
    --dry-run) DRY_RUN=1 ;;
    --verify)  VERIFY_ONLY=1 ;;
    *) echo "unknown arg: $a" >&2; exit 2 ;;
  esac
done

run() {  # dry-run 対応の実行ラッパ
  if [ "$DRY_RUN" = 1 ]; then echo "DRY: $*"; else "$@"; fi
}

need_root() {
  if [ "$(id -u)" != "0" ] && [ "$DRY_RUN" != 1 ]; then
    echo "root で実行してください（sudo）。--dry-run は非 root でも可。" >&2; exit 1
  fi
}

ensure_group() {
  if getent group "$1" >/dev/null; then echo "group $1: exists"; else run groupadd --system "$1"; echo "group $1: created"; fi
}

ensure_user() {  # $1=user $2=primary-group  （nologin・system・no home 追加なし）
  if getent passwd "$1" >/dev/null; then
    echo "user $1: exists"
  else
    run useradd --system --no-create-home --shell /usr/sbin/nologin -g "$2" "$1"
    echo "user $1: created (primary group=$2)"
  fi
}

# ---- verify: 所有権/mode/グループの点検（失敗を集計して非0終了）----
verify() {
  local fail=0
  echo "== verify $ROOT =="
  # sherpa-agent が sherpa-ingest グループに**入っていない**こと（kb 書込を所有権で不可能に）
  if id -nG "$U_AGENT" 2>/dev/null | tr ' ' '\n' | grep -qx "$U_INGEST"; then
    echo "NG: $U_AGENT is in group $U_INGEST (kb を書けてしまう)"; fail=1
  else
    echo "OK: $U_AGENT not in $U_INGEST group"
  fi
  for u in "$U_AGENT" "$U_API"; do
    if id -nG "$u" 2>/dev/null | tr ' ' '\n' | grep -qx "$G_WORKSPACE"; then
      echo "OK: $u in $G_WORKSPACE group"
    else
      echo "NG: $u is not in $G_WORKSPACE group (workspace を共有できない)"; fail=1
    fi
  done
  # kb の所有者が sherpa-ingest であること
  for d in kb kb/uploads kb/md kb/src; do
    p="$ROOT/$d"; [ -e "$p" ] || { echo "-- $p: 未作成"; continue; }
    owner="$(stat -c '%U' "$p")"
    if [ "$owner" = "$U_INGEST" ]; then echo "OK: $p owner=$owner"; else echo "NG: $p owner=$owner (期待 $U_INGEST)"; fail=1; fi
    # group 書込ビットが無いこと（020 が立っていない）
    perm="$(stat -c '%a' "$p")"
    if [ "$(( 0$perm & 020 ))" -ne 0 ]; then echo "NG: $p mode=$perm に group 書込ビット"; fail=1; fi
  done
  # users/ が sherpa-agent 所有であること
  if [ -e "$ROOT/users" ]; then
    owner="$(stat -c '%U' "$ROOT/users")"
    group="$(stat -c '%G' "$ROOT/users")"
    perm="$(stat -c '%a' "$ROOT/users")"
    [ "$owner" = "$U_AGENT" ] && echo "OK: users owner=$owner" || { echo "NG: users owner=$owner (期待 $U_AGENT)"; fail=1; }
    [ "$group" = "$G_WORKSPACE" ] && echo "OK: users group=$group" || { echo "NG: users group=$group (期待 $G_WORKSPACE)"; fail=1; }
    if [ "$(( 0$perm & 020 ))" -eq 0 ]; then echo "NG: $ROOT/users mode=$perm に group 書込なし"; fail=1; fi
  fi
  if [ -e "$ROOT/derived" ]; then
    owner="$(stat -c '%U' "$ROOT/derived")"
    group="$(stat -c '%G' "$ROOT/derived")"
    [ "$owner" = "$U_API" ] && echo "OK: derived owner=$owner" || { echo "NG: derived owner=$owner (期待 $U_API)"; fail=1; }
    [ "$group" = "$G_WORKSPACE" ] && echo "OK: derived group=$group" || { echo "NG: derived group=$group (期待 $G_WORKSPACE)"; fail=1; }
  fi
  return $fail
}

need_root
if [ "$VERIFY_ONLY" = 1 ]; then verify; exit $?; fi

echo "== B-full runtime users/dirs setup (root=$ROOT) =="
ensure_group "$GROUP"
ensure_group "$G_WORKSPACE"
# sherpa-ingest: kb の唯一の書込者。primary group = sherpa。
ensure_user "$U_INGEST" "$GROUP"
# sherpa-agent: Codex 実行。**専用 primary group（自分自身）**にして sherpa グループへは追加しない
#   → sherpa グループ経由でも kb を書けない。
if ! getent group "$U_AGENT" >/dev/null; then run groupadd --system "$U_AGENT"; fi
ensure_user "$U_AGENT" "$U_AGENT"
if [ "$DRY_RUN" != 1 ]; then usermod -a -G "$G_WORKSPACE" "$U_AGENT" || true; fi
# sherpa-api: FastAPI。読み取り用に sherpa グループへ（kb read）。
ensure_user "$U_API" "$GROUP"
if [ "$DRY_RUN" != 1 ]; then
  usermod -a -G "$GROUP" "$U_API" || true
  usermod -a -G "$G_WORKSPACE" "$U_API" || true
fi

# ---- ディレクトリ実体（RUNTIME-SANDBOX §1）----
run mkdir -p "$ROOT/kb/uploads" "$ROOT/kb/md" "$ROOT/kb/src" "$ROOT/users" "$ROOT/derived"
# kb は sherpa-ingest:sherpa・group 書込なし（0755/dir・0644/file 想定）。agent は read-only。
run chown -R "$U_INGEST:$GROUP" "$ROOT/kb"
run chmod -R u=rwX,g=rX,o=rX "$ROOT/kb"     # group 書込なし＝agent(sherpa グループでも)書けない
# users/ 直下は workspace 共有グループで FastAPI と Codex 実行プロセスが作成/書込できる。
run chown "$U_AGENT:$G_WORKSPACE" "$ROOT/users"
run chmod 0770 "$ROOT/users"
# derived/ は取り込み・再生成可能な派生物。現行実装では FastAPI プロセスが書く。
run chown "$U_API:$G_WORKSPACE" "$ROOT/derived"
run chmod 0770 "$ROOT/derived"

echo ""
echo "== 適用後の点検 =="
if [ "$DRY_RUN" != 1 ]; then verify || echo "(verify に NG あり・上を確認)"; else echo "DRY: verify skipped"; fi
echo ""
echo "次: FastAPI を $U_API:$G_WORKSPACE で動かす（deploy/systemd/sherpa-api.service）。"
echo "     SHERPA_USERS_DIR=$ROOT/users, KB は $ROOT/kb（read-only）。"
