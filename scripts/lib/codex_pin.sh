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

# $1=platform key  $2=パッケージのルート（tools/codex 相当）-> 付属物がそろっていれば 0。
# 必須: bin/codex・codex-path/rg（共通）、Linux は codex-resources/bwrap（サンドボックス）。
# bwrap・rg が無いと OS 側に bubblewrap の無い機械でシェルのコマンドが全て失敗する。
codex_pin_package_complete() {
  local key="$1" root="$2"
  [ -x "$root/bin/codex" ] || return 1
  [ -x "$root/bin/codex-code-mode-host" ] || return 1
  [ -x "$root/codex-path/rg" ] || return 1
  [ -x "$root/codex-resources/zsh/bin/zsh" ] || return 1
  case "$key" in
    linux_*) [ -x "$root/codex-resources/bwrap" ] || return 1 ;;
  esac
  return 0
}

# $1=platform key  $2=パッケージのルート  $3=期待する版 -> 版が一致し付属物もそろっていれば 0。
codex_pin_installed_ok() {
  local key="$1" root="$2" want="$3" got
  codex_pin_package_complete "$key" "$root" || return 1
  got="$(codex_pin_installed_version "$root/bin/codex" || true)"
  [ "$got" = "$want" ]
}

# $1=PATH 上の codex のパス -> 付属物の一式（codex_pin_package_complete と同じ基準）を持つ配置なら 0。
# 単体の実行ファイルは、その一つ上（`bin/` の親）を一式のルートとして確かめる。npm 版（実体が .js の
# ランチャー）は、同梱の `node_modules/@openai/codex-*/vendor/<triple>/` を一式のルートとして確かめる。
codex_pin_path_codex_has_accessories() {
  local exe="$1" real key root triple
  key="$(codex_pin_platform_key)"
  [ -n "$key" ] || return 1
  real="$(readlink -f "$exe" 2>/dev/null || realpath "$exe" 2>/dev/null || printf '%s' "$exe")"
  case "$real" in
    *.js)
      case "$key" in
        linux_x86_64) triple="x86_64-unknown-linux-musl" ;;
        linux_aarch64) triple="aarch64-unknown-linux-musl" ;;
        darwin_arm64) triple="aarch64-apple-darwin" ;;
        darwin_x86_64) triple="x86_64-apple-darwin" ;;
        *) return 1 ;;
      esac
      for root in "$(dirname "$real")"/../node_modules/@openai/codex-*/vendor/"$triple"/; do
        [ -d "$root" ] || continue
        codex_pin_package_complete "$key" "${root%/}" && return 0
      done
      return 1
      ;;
  esac
  codex_pin_package_complete "$key" "$(dirname "$real")/.."
}

# $1=tar.gz  -> アーカイブの中身が安全なら 0（脱出パス・絶対パス・リンク・デバイス等を含めば非0）。
# 一覧の種別文字（tar -tv の先頭）は '-'（通常ファイル）と 'd'（ディレクトリ）だけを許す。
codex_pin_inspect_archive() {
  local tarball="$1" name type
  tar -tzf "$tarball" >/dev/null 2>&1 || { echo "codex_pin_inspect_archive: 読めないアーカイブです: $tarball" >&2; return 1; }
  while IFS= read -r name; do
    case "$name" in
      /*|..|../*|*/..|*/../*) echo "codex_pin_inspect_archive: 不正なパス（脱出・絶対パス）: ${name}" >&2; return 1 ;;
    esac
  done < <(tar -tzf "$tarball" 2>/dev/null)
  while IFS= read -r type; do
    case "$type" in
      -|d) ;;
      *) echo "codex_pin_inspect_archive: リンク・デバイス等の種別を含みます（${type}）: $tarball" >&2; return 1 ;;
    esac
  done < <(tar -tvzf "$tarball" 2>/dev/null | cut -c1)
  return 0
}

# $1=tar.gz（codex-package-<triple>.tar.gz）  $2=導入先ディレクトリ（例 tools/codex）
# $3=期待する版（例 0.153.4）  $4=platform key
# 導入先と同じファイルシステムの作業領域（導入先の親の隠しフォルダ）へ展開 → 中身の検査（脱出パス・
# 絶対パス・リンク・デバイスの拒否／bin/codex と付属物の存在）→ 展開した bin/codex の --version を確認 →
# 導入先全体を rename で入れ替える。途中で失敗しても既存の導入先は壊れない（古い残りは混ざらない＝
# 旧い本体だけの配置・npm の残りも丸ごと置き換わる）。
codex_pin_extract_package() {
  local tarball="$1" dest="$2" want="$3" key="$4" parent stage got old
  parent="$(dirname "$dest")"
  mkdir -p "$parent" || return 1
  codex_pin_inspect_archive "$tarball" || return 1
  stage="$(mktemp -d "$parent/.codex-staging.XXXXXX")" || return 1
  if ! tar -xzf "$tarball" -C "$stage" 2>/dev/null; then
    echo "codex_pin_extract_package: 展開に失敗しました: $tarball" >&2
    rm -rf "$stage"; return 1
  fi
  # 展開後にも念のため、通常ファイルとディレクトリ以外（リンク・デバイス）が無いことを確かめる。
  if [ -n "$(find "$stage" ! -type f ! -type d -print -quit)" ]; then
    echo "codex_pin_extract_package: リンク・デバイス等を含みます: $tarball" >&2
    rm -rf "$stage"; return 1
  fi
  chmod -R u+rwX "$stage"
  if ! codex_pin_package_complete "$key" "$stage"; then
    echo "codex_pin_extract_package: 必須の付属物がありません（bin/codex・bin/codex-code-mode-host・codex-path/rg・codex-resources/zsh・Linux は codex-resources/bwrap）: $tarball" >&2
    rm -rf "$stage"; return 1
  fi
  got="$(codex_pin_installed_version "$stage/bin/codex" || true)"
  if [ "$got" != "$want" ]; then
    echo "codex_pin_extract_package: 展開した Codex CLI の版が一致しません（期待 ${want}・実際 ${got:-取得失敗}）: $tarball" >&2
    rm -rf "$stage"; return 1
  fi
  old=""
  if [ -e "$dest" ] || [ -L "$dest" ]; then
    old="$parent/.codex-old.$$.$RANDOM"
    if ! mv "$dest" "$old"; then
      rm -rf "$stage"; return 1
    fi
  fi
  if ! mv "$stage" "$dest"; then
    [ -n "$old" ] && mv "$old" "$dest"
    rm -rf "$stage"; return 1
  fi
  [ -n "$old" ] && rm -rf "$old"
  return 0
}
