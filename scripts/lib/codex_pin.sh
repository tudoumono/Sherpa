#!/usr/bin/env bash
# 固定版 Codex CLI の導入ロジック（source 専用）。呼び出し側が先に
# `. scripts/codex-version.env` して定数を読み込んでいる前提（このファイル自身は読まない）。
# 使い方: scripts/codex_install.sh・scripts/make_offline_kit.sh・scripts/install_offline_kit.sh。
set -o pipefail

# uname -s/-m から配布物キー（CODEX_PIN_ASSET_<key> 等の添字）へ写像する。対応外なら空文字列。
codex_pin_platform_key() {
  local os cpu
  os="$(uname -s)"
  cpu="$(uname -m)"
  case "$os" in
    Linux)
      case "$cpu" in
        x86_64|amd64) printf '%s' "linux_x86_64" ;;
        aarch64|arm64) printf '%s' "linux_aarch64" ;;
        *) printf '%s' "" ;;
      esac
      ;;
    Darwin)
      case "$cpu" in
        arm64) printf '%s' "darwin_arm64" ;;
        x86_64) printf '%s' "darwin_x86_64" ;;
        *) printf '%s' "" ;;
      esac
      ;;
    *) printf '%s' "" ;;
  esac
}

# $1=platform key -> 配布物ファイル名（例: codex-x86_64-unknown-linux-musl.tar.gz）。未設定なら空。
# 注意: `local a="$1" b="...${a}..."` は不可（多重代入は全 RHS を先に評価するため、set -u 下で
# a が未定義のまま b の展開に使われ unbound variable になる）。var は key を使わずに構築する。
codex_pin_asset_name() {
  local key="$1" var
  var="CODEX_PIN_ASSET_${key}"
  printf '%s' "${!var:-}"
}

# $1=platform key -> 期待する sha256（小文字16進）。未設定なら空。
codex_pin_sha256() {
  local key="$1" var
  var="CODEX_PIN_SHA256_${key}"
  printf '%s' "${!var:-}"
}

# $1=platform key -> ダウンロード URL。アセット名が無ければ非0。
codex_pin_url() {
  local key="$1" asset
  asset="$(codex_pin_asset_name "$key")"
  [ -n "$asset" ] || return 1
  printf '%s/%s/%s' "${CODEX_PIN_BASE_URL%/}" "$CODEX_PIN_RELEASE_TAG" "$asset"
}

# $1=実行ファイルのパス -> "codex-cli X.Y.Z" の版部分だけを stdout へ。実行できなければ非0・空文字列。
codex_pin_installed_version() {
  local bin="$1" out
  [ -x "$bin" ] || return 1
  out="$("$bin" --version 2>/dev/null)" || return 1
  printf '%s' "${out##* }"
}

# $1=platform key  $2=保存先パス
# ダウンロード→sha256照合。戻り値: 0=OK／2=取得できなかった（ネットワーク不可・404等・fail-open用）／
# 3=sha256不一致（改ざん・破損の疑い・fail-closed）／1=platform key不明などの呼び出し誤り。
# 失敗時は $2 を残さない。取得元は CODEX_PIN_BASE_URL（テストは file:// 等へ差し替えて実ネットワークに出ない）。
codex_pin_fetch_asset() {
  local key="$1" dest="$2" url expect actual
  url="$(codex_pin_url "$key")" || { echo "codex_pin_fetch_asset: 未対応の platform key: $key" >&2; return 1; }
  expect="$(codex_pin_sha256 "$key")"
  if [ -z "$expect" ]; then
    echo "codex_pin_fetch_asset: sha256 が未設定です（${key}）" >&2
    return 1
  fi
  if ! curl -fsSL --connect-timeout "${CODEX_PIN_CURL_CONNECT_TIMEOUT:-10}" \
       --max-time "${CODEX_PIN_CURL_MAX_TIME:-120}" "$url" -o "$dest" 2>/dev/null; then
    rm -f "$dest"
    return 2
  fi
  actual="$(sherpa_sha256_hex "$dest")"
  if [ "$actual" != "$expect" ]; then
    rm -f "$dest"
    echo "codex_pin_fetch_asset: sha256 不一致（${key}）: 期待 ${expect} / 実際 ${actual}" >&2
    return 3
  fi
  return 0
}

# $1=tar.gz  $2=展開先の実行ファイルパス  $3=期待する版（例 0.153.4）
# 配布物は「サブディレクトリ無し・1ファイルのみ」の tar.gz（実機確認済み・scripts/codex-version.env
# 冒頭コメント参照）。それ以外の構成は想定外として拒否する。
# 展開先と同じフォルダの一時領域で展開し、`--version` が期待する版を返したときだけ rename で差し替える
# （同一ファイルシステム内の rename＝途中で失敗しても既存の $2 は壊れない）。
codex_pin_extract_bin() {
  local tarball="$1" dest="$2" want="$3" dir tmp entries n got
  dir="$(dirname "$dest")"
  mkdir -p "$dir" || return 1
  tmp="$(mktemp -d "$dir/.codex-staging.XXXXXX")" || return 1
  if ! tar -xzf "$tarball" -C "$tmp" 2>/dev/null; then
    rm -rf "$tmp"
    return 1
  fi
  entries=("$tmp"/*)
  n="${#entries[@]}"
  if [ "$n" -ne 1 ] || [ ! -f "${entries[0]}" ] || [ -L "${entries[0]}" ]; then
    echo "codex_pin_extract_bin: 想定外の tar 構成です（1ファイルのみを期待）: $tarball" >&2
    rm -rf "$tmp"
    return 1
  fi
  chmod +x "${entries[0]}"
  got="$(codex_pin_installed_version "${entries[0]}" || true)"
  if [ "$got" != "$want" ]; then
    echo "codex_pin_extract_bin: 展開した Codex CLI の版が一致しません（期待 ${want}・実際 ${got:-取得失敗}）: $tarball" >&2
    rm -rf "$tmp"
    return 1
  fi
  if ! mv -f "${entries[0]}" "$dest"; then
    rm -rf "$tmp"
    return 1
  fi
  rm -rf "$tmp"
  return 0
}
