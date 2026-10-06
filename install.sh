#!/usr/bin/env bash
# Sherpa のインストール（初回も更新も同じ入口・make 不要）。
#
#   cd <作業フォルダ>/Sherpa
#   ./install.sh 2>&1 | tee ~/install-sherpa.log
#
# 手順書: INSTALL.md／運用: docs/manual/40-運用.md
#
# ① パッケージの種類（full／app）を PACKAGE-INFO から読む
# ② インストール中の印（data/.installing）を置く（成功したときだけ消す）
# ③ 展開したファイルを PACKAGE-MANIFEST.sha256 で照合する
# ④ このプラットフォームの指紋の項目があるか確かめる
# ⑤ 更新なら、データのバックアップを取る
# ⑥ app: 依存の指紋を照合する（合わなければ依存に触れず止まる）
#    full: .venv と tools の実行物を脇へ退避し、最終の場所で作り直す（Linux=同梱の資材でオフライン／
#          macOS=オンライン・Python と Codex のあと scripts/install_macos_parts.sh が Node・marp・Chromium・
#          LibreOffice・フォントを入れる。Chromium・LibreOffice・フォントはシステム側＝退避の対象外）。
#          作ったあとに環境を測って指紋と合うことを確かめる（失敗したら退避を戻す）
# ⑦ 前回の一覧にあって今回無い古いファイルを、ハッシュが一致するものだけ片付ける
# ⑧ 記録（版・コミット・指紋・ファイル一覧）を更新して印を消す
#
# 環境変数: SHERPA_BACKUP_BEFORE_SWITCH=0（更新前の自動バックアップを抑止）／PYTHON_BIN（venv を作る Python）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT"
# shellcheck source=scripts/lib/install_state.sh
. "$ROOT/scripts/lib/install_state.sh"
# shellcheck source=scripts/codex-version.env
. "$ROOT/scripts/codex-version.env"
# shellcheck source=scripts/lib/codex_pin.sh
. "$ROOT/scripts/lib/codex_pin.sh"

note() { echo "・ $*"; }
ok()   { echo "OK: $*"; }
warn() { echo "ⓘ  $*" >&2; }
fail() { echo "✗ $*" >&2; }

case "${1:-}" in
  -h|--help)
    sed -n '2,22p' "$0" | sed 's/^# \{0,1\}//'
    exit 0 ;;
  "") ;;
  *) fail "使い方: ./install.sh（引数はありません）"; exit 2 ;;
esac

INFO="$ROOT/$PKG_INFO_NAME"
MANIFEST="$ROOT/$PKG_MANIFEST_NAME"
MARK="$(pkg_installing_file)"
RECORD="$(pkg_record_file)"
RECORD_PREV="$(pkg_record_prev_file)"
MANIFEST_RECORD="$(pkg_manifest_record)"
KEPT_FILE=""
FP_FILE=""
MARK_PLACED=0
PARKED=0

if [ ! -f "$INFO" ] || [ ! -f "$MANIFEST" ]; then
  fail "このフォルダは配布パッケージの展開先ではありません（${PKG_INFO_NAME}／${PKG_MANIFEST_NAME} がありません）。"
  fail "  開発用のチェックアウトは make start で起動します。パッケージは tar xzf で展開した Sherpa/ の中で ./install.sh を実行します。"
  exit 1
fi
KIND="$(pkg_kv_get "$INFO" kind)"
VERSION="$(pkg_kv_get "$INFO" version)"
COMMIT="$(pkg_kv_get "$INFO" commit)"
case "$KIND" in
  full|app) ;;
  *) fail "パッケージの種類を読めません（kind=${KIND:-空}）。展開が壊れているか、古い形式のパッケージです。"; exit 1 ;;
esac

PLATFORM="${SHERPA_INSTALL_PLATFORM:-$(codex_pin_platform_key)}"

_cleanup() {
  local rc=$?
  [ -n "$KEPT_FILE" ] && rm -f "$KEPT_FILE"
  [ -n "$FP_FILE" ] && rm -f "$FP_FILE"
  if [ "$rc" != 0 ] && [ "$PARKED" = 1 ]; then
    restore_sets
    echo "前の .venv／tools を元の場所へ戻しました。" >&2
  fi
  if [ "$rc" != 0 ] && [ "$MARK_PLACED" = 1 ]; then
    echo "" >&2
    echo "✗ インストールは完了していません（終了コード ${rc}）。「インストール中」の印が残っているため、make start も" >&2
    echo "  systemctl start・再起動も断られます。上のメッセージの原因を直して ./install.sh をやり直してください。" >&2
  fi
}
trap _cleanup EXIT

echo "=== Sherpa インストール（種類: ${KIND}・版: ${VERSION}・コミット: ${COMMIT}・環境: ${PLATFORM:-不明}）==="

# --- ② 動作中のアプリを見つけたら先へ進まない ---
mkdir -p "$ROOT/data"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
if pid="$(live_matching_pid "$APP_PID_FILE" "$APP_PROC_NEEDLE" 2>/dev/null)"; then
  fail "Sherpa が動いています（pid ${pid}）。make stop で止めてから ./install.sh を実行してください。"
  exit 1
fi
active_units="$(pkg_systemd_active_units)"
if [ -n "$active_units" ]; then
  fail "systemd で動いているユニットがあります: $(echo "$active_units" | tr '\n' ' ')"
  fail "  make stop（または sudo systemctl stop <ユニット名>）で止めてから ./install.sh を実行してください。"
  exit 1
fi

printf 'started=%s\nversion=%s\ncommit=%s\nkind=%s\n' "$(date +%Y-%m-%dT%H:%M:%S)" "$VERSION" "$COMMIT" "$KIND" > "$MARK"
MARK_PLACED=1

# --- ③ 展開したファイルの照合（途中で止まった展開・破損を見つける） ---
note "展開したファイルを $PKG_MANIFEST_NAME で照合します..."
if command -v sha256sum >/dev/null 2>&1; then
  verify_cmd="sha256sum -c"
else
  verify_cmd="shasum -a 256 -c"
fi
verify_rc=0
verify_out="$($verify_cmd "$MANIFEST" 2>&1)" || verify_rc=$?
if [ "$verify_rc" != 0 ]; then
  echo "$verify_out" | grep -v ': OK$' | head -20 >&2 || true
  fail "展開したファイルが一覧と一致しません（展開が途中で止まった・破損の疑い）。圧縮ファイルの .sha256 を照合し直し、展開し直してください。"
  exit 1
fi
ok "展開したファイルは一覧と一致しています。"

# --- ④ このプラットフォームの指紋の項目 ---
if [ -z "$PLATFORM" ] || ! grep -q "^fp\.${PLATFORM}\." "$INFO"; then
  fail "このパッケージはこのプラットフォーム（${PLATFORM:-不明}）に対応していません。対応する環境用のパッケージを使ってください。"
  exit 1
fi

# --- 実行物の退避・復元 ---
park_sets() {
  local item
  for item in $PKG_SWAP_ITEMS; do
    rm -rf "$ROOT/$item.prev-old"
    if [ -e "$ROOT/$item.prev" ]; then mv "$ROOT/$item.prev" "$ROOT/$item.prev-old"; fi
    if [ -e "$ROOT/$item" ]; then mv "$ROOT/$item" "$ROOT/$item.prev"; fi
  done
  PARKED=1
}
restore_sets() {
  local item
  for item in $PKG_SWAP_ITEMS; do
    rm -rf "$ROOT/$item"
    if [ -e "$ROOT/$item.prev" ]; then mv "$ROOT/$item.prev" "$ROOT/$item"; fi
    if [ -e "$ROOT/$item.prev-old" ]; then mv "$ROOT/$item.prev-old" "$ROOT/$item.prev"; fi
  done
  PARKED=0
}
# 導入の成功が確定してから、さらに前の組（.prev-old）を捨てる。
discard_older_sets() {
  local item
  for item in $PKG_SWAP_ITEMS; do rm -rf "$ROOT/$item.prev-old"; done
  PARKED=0
}
# 新しい資材が無く作り直されなかった tools の項目は、退避したものを元の場所へ戻して残す。
keep_unreplaced_tools() {
  local item
  for item in $PKG_SWAP_ITEMS; do
    case "$item" in .venv|tools/codex) continue ;; esac
    if [ ! -e "$ROOT/$item" ] && [ -e "$ROOT/$item.prev" ]; then
      mv "$ROOT/$item.prev" "$ROOT/$item"
      note "このパッケージの資材に無いため、今ある ${item} をそのまま残します。"
    fi
  done
}
swap_sets() {
  local item
  for item in $PKG_SWAP_ITEMS; do
    if [ -e "$ROOT/$item" ] || [ -e "$ROOT/$item.prev" ]; then
      mv "$ROOT/$item" "$ROOT/$item.swap" 2>/dev/null || true
      if [ -e "$ROOT/$item.prev" ]; then mv "$ROOT/$item.prev" "$ROOT/$item"; fi
      if [ -e "$ROOT/$item.swap" ]; then mv "$ROOT/$item.swap" "$ROOT/$item.prev"; fi
    fi
  done
  if [ -f "$RECORD_PREV" ]; then
    mv "$RECORD" "$RECORD.swap"
    mv "$RECORD_PREV" "$RECORD"
    mv "$RECORD.swap" "$RECORD_PREV"
  fi
}

# --- ⑤ 更新前のバックアップ（データだけ・アプリと .venv／tools は含まない） ---
backup_before_install() {
  [ "${SHERPA_BACKUP_BEFORE_SWITCH:-1}" != 0 ] || return 0
  [ -f "$RECORD" ] || return 0
  local bk_env="${SHERPA_ENV_FILE:-}" rc=0
  if [ -z "$bk_env" ] && [ -f /etc/sherpa/sherpa.env ]; then bk_env=/etc/sherpa/sherpa.env; fi
  echo "--- 更新前のバックアップ ---"
  if [ -n "$bk_env" ]; then
    SHERPA_ENV_FILE="$bk_env" "$ROOT/scripts/backup.sh" || rc=$?
  else
    "$ROOT/scripts/backup.sh" || rc=$?
  fi
  case "$rc" in
    0) ok "更新前のバックアップを取りました（戻すには make restore FROM=<バックアップの dir>）。" ;;
    3) warn "バックアップを取れませんでした（ストアが動いています）。make stop のあと make backup を推奨します。インストールは続けます。" ;;
    *) fail "更新前のバックアップに失敗したため（exit=${rc}）、インストールを中止します。原因を直して ./install.sh をやり直してください。"
       exit 1 ;;
  esac
}

detect_docker() {
  if [ -n "${SHERPA_DOCKER:-}" ]; then return 0; fi
  if command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
    SHERPA_DOCKER="sudo docker"
  else
    SHERPA_DOCKER="docker"
  fi
  export SHERPA_DOCKER
}

tool_python() { printf '%s\n' "$ROOT/.venv/bin/python"; }

# --- フルのパッケージ: プラットフォームごとの準備 ---
prepare_linux() {
  local installer="${SHERPA_INSTALL_KIT_INSTALLER:-$ROOT/scripts/install_offline_kit.sh}"
  if [ ! -d "$ROOT/dist/offline-kit" ]; then
    fail "フルのパッケージのはずですが、オフラインの資材（dist/offline-kit）がありません。展開し直してください。"
    return 1
  fi
  bash "$installer"
}
prepare_macos() {
  local py="${PYTHON_BIN:-python3}"
  # shellcheck source=scripts/lib/req_hash.sh
  . "$ROOT/scripts/lib/req_hash.sh"
  local want have
  want="$(pkg_kv_get "$INFO" "fp.${PLATFORM}.python")"
  have="$("$py" -c "import sys;print('%d.%d/%s' % (sys.version_info[0], sys.version_info[1], sys.implementation.cache_tag))" 2>/dev/null || true)"
  if [ -n "$want" ] && [ "$have" != "$want" ]; then
    fail "この Python（${py}）は ${have:-不明} で、パッケージが期待する ${want} と違います。何も作らずに止めます。"
    fail "  合う版の Python を PYTHON_BIN で指定して ./install.sh をやり直してください（例: PYTHON_BIN=python${want%%/*} ./install.sh）。"
    return 1
  fi
  "$py" -m venv "$ROOT/.venv" || return 1
  "$ROOT/.venv/bin/python" -m pip install \
    --only-binary tree-sitter,tree-sitter-java,tree-sitter-c-sharp,tree-sitter-c,tree-sitter-javascript,tree-sitter-bash,tree-sitter-css,tree-sitter-embedded-template \
    -r requirements.txt -c constraints.txt || return 1
  req_hash "$ROOT/.venv/bin/python" > "$ROOT/.venv/.requirements.sha256"
  ./scripts/codex_install.sh || return 1
  bash "$ROOT/scripts/install_macos_parts.sh" || return 1
}

# --- ⑦ 古いファイルの片付け（前回の一覧にあって今回無く、ハッシュが一致するものだけ） ---
protected_path() {
  case "$1" in
    .env|.env/*|data|data/*|.venv|.venv/*|.venv.prev|.venv.prev/*|tools|tools/*) return 0 ;;
    /*|..|../*|*/..|*/../*) return 0 ;;
  esac
  return 1
}
cleanup_old_files() {
  local old_sorted new_sorted cand hash cur removed=0 dir
  if [ ! -f "$MANIFEST_RECORD" ]; then
    note "前回のファイル一覧が無いため、古いファイルの片付けはしません（この仕組みより前の版からの更新）。"
    return 0
  fi
  old_sorted="$(mktemp "${TMPDIR:-/tmp}/sherpa-install.XXXXXX")"; new_sorted="$(mktemp "${TMPDIR:-/tmp}/sherpa-install.XXXXXX")"
  sed -E 's/^[0-9a-f]{64}  //' "$MANIFEST_RECORD" | LC_ALL=C sort > "$old_sorted"
  sed -E 's/^[0-9a-f]{64}  //' "$MANIFEST" | LC_ALL=C sort > "$new_sorted"
  while IFS= read -r cand; do
    [ -n "$cand" ] || continue
    if protected_path "$cand"; then continue; fi
    [ -f "$ROOT/$cand" ] || continue
    hash="$(awk -v p="$cand" '{h=$1; sub(/^[0-9a-f]+  /, ""); if ($0 == p) {print h; exit}}' "$MANIFEST_RECORD")"
    cur="$(sherpa_sha256_hex "$ROOT/$cand")"
    if [ -n "$hash" ] && [ "$hash" = "$cur" ]; then
      rm -f "$ROOT/$cand"
      removed=$((removed + 1))
      dir="$(dirname "$cand")"
      while [ "$dir" != "." ] && [ "$dir" != "/" ] && ! protected_path "$dir"; do
        rmdir "$ROOT/$dir" 2>/dev/null || break
        dir="$(dirname "$dir")"
      done
    else
      echo "$cand" >> "$KEPT_FILE"
    fi
  done < <(LC_ALL=C comm -23 "$old_sorted" "$new_sorted")
  rm -f "$old_sorted" "$new_sorted"
  note "古いファイルを ${removed} 件片付けました。"
}

write_record() {  # $1=指紋の行（fp.<部品>=<値>）のファイル
  local tmp="$RECORD.tmp"
  {
    echo "version=$VERSION"
    echo "commit=$COMMIT"
    echo "kind=$KIND"
    echo "platform=$PLATFORM"
    echo "installed_at=$(date +%Y-%m-%dT%H:%M:%S)"
    cat "$1"
  } > "$tmp"
  if [ "$KIND" = full ] && [ -f "$RECORD" ]; then cp "$RECORD" "$RECORD_PREV"; fi
  mv "$tmp" "$RECORD"
  cp "$MANIFEST" "$MANIFEST_RECORD"
}

backup_before_install

KEPT_FILE="$(mktemp "${TMPDIR:-/tmp}/sherpa-install.XXXXXX")"
FP_FILE="$(mktemp "${TMPDIR:-/tmp}/sherpa-install.XXXXXX")"
: > "$KEPT_FILE"

# --- ⑥ 依存 ---
if [ "$KIND" = app ]; then
  echo "--- 依存の指紋の照合（アプリだけのパッケージ・依存には触れません） ---"
  detect_docker
  need_full() {
    fail "このパッケージ（アプリだけ）は、今の環境の依存と合いません。依存には触れず、ここで止まります。"
    fail "  出口は 2 つあります。どちらでもデータには触れません。"
    fail "  (a) 同じ版のフルのパッケージを置き、通常の手順（照合→展開→./install.sh）で入れ直す。"
    fail "  (b) 前の版の圧縮ファイルを展開し直し、./install.sh して前の版へ戻る。"
    exit 1
  }
  if [ ! -x "$(tool_python)" ]; then
    fail "依存（.venv）がありません。初回導入にはフルのパッケージが必要です。"
    need_full
  fi
  rc=0
  "$(tool_python)" "$ROOT/scripts/lib/pkg_tool.py" fp-app --root "$ROOT" --platform "$PLATFORM" \
    --record "$RECORD" --out "$FP_FILE" || rc=$?
  if [ "$rc" = 2 ]; then exit 1; fi
  if [ "$rc" != 0 ] && [ -x "$ROOT/.venv.prev/bin/python" ] && [ -f "$RECORD_PREV" ]; then
    prev_rc=0
    "$(tool_python)" "$ROOT/scripts/lib/pkg_tool.py" fp-app --root "$ROOT" --platform "$PLATFORM" \
      --record "$RECORD_PREV" --prev --out "$FP_FILE" || prev_rc=$?
    if [ "$prev_rc" = 0 ]; then
      note "残してある前の .venv／tools がこのパッケージと合うため、そちらへ戻します。"
      swap_sets
      rc=0
      "$(tool_python)" "$ROOT/scripts/lib/pkg_tool.py" fp-app --root "$ROOT" --platform "$PLATFORM" \
        --record "$RECORD" --out "$FP_FILE" || rc=$?
    fi
  fi
  if [ "$rc" != 0 ]; then need_full; fi
  ok "依存の指紋がパッケージ・前回の記録・今の環境で一致しました。"
else
  echo "--- 依存の準備（フルのパッケージ） ---"
  park_sets
  prep_rc=0
  case "$PLATFORM" in
    linux_*) prepare_linux || prep_rc=$? ;;
    darwin_*) prepare_macos || prep_rc=$? ;;
    *) fail "未対応のプラットフォームです: $PLATFORM"; prep_rc=1 ;;
  esac
  if [ "$prep_rc" != 0 ]; then
    restore_sets
    fail "依存の準備に失敗しました。前の .venv／tools を元の場所へ戻しました。"
    exit 1
  fi
  keep_unreplaced_tools
  detect_docker
  fp_rc=0
  if [ -x "$(tool_python)" ]; then
    "$(tool_python)" "$ROOT/scripts/lib/pkg_tool.py" fp-full --root "$ROOT" --platform "$PLATFORM" --out "$FP_FILE" || fp_rc=$?
  else
    fp_rc=1
  fi
  if [ "$fp_rc" != 0 ]; then
    restore_sets
    fail "導入後の環境を測ったところ、パッケージの指紋と合いませんでした（上の一覧）。前の .venv／tools を元の場所へ戻しました。"
    exit 1
  fi
  ok "導入後の環境がパッケージの指紋と一致しました（前の組は ${PKG_SWAP_ITEMS// /・} の .prev に、更新が確認できるまで残します）。"
fi

# --- ⑦⑧ 片付け・記録・印 ---
cleanup_old_files
write_record "$FP_FILE"
discard_older_sets
rm -f "$MARK"
MARK_PLACED=0

echo ""
echo "=== インストール完了（版 ${VERSION}） ==="
if [ -s "$KEPT_FILE" ]; then
  echo "次のファイルは、前回のパッケージのものですが手が入っていたため消さずに残しました（不要なら自分で削除してください）:"
  sed 's/^/  /' "$KEPT_FILE"
fi
echo ""
echo "次にやること（INSTALL.md の「確認して起動する」）:"
echo "  make check-ports && make start"
