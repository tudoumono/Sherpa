#!/usr/bin/env bash
# 固定版 Codex CLI（scripts/codex-version.env）を tools/codex/ にそろえる（package 一式＝bin/codex・
# codex-path/rg・Linux は codex-resources/bwrap ほか）。
# 管理者権限不要・冪等（既に固定版で付属物もそろっていれば何もしない。本体だけの古い配置は入れ直す）。make start / make codex-install から呼ぶ。
#
#   ./scripts/codex_install.sh            # 確認・無ければ導入・違えば入れ替え
#   ./scripts/codex_install.sh --check     # 導入は行わず、固定版と現状を表示するだけ
#
# ネットワーク不可・対象外OS・sha256不一致など「導入できない」場合は日本語で警告して非0を返す
# だけで、それ自体はプロセスを止めない（呼び出し側が起動を止めるかどうかを決める。start.sh は
# `./scripts/codex_install.sh || true` で無視して既存の Codex（PATH上）のまま起動を続ける）。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
# shellcheck source=scripts/codex-version.env
. "$ROOT/scripts/codex-version.env"
# shellcheck source=scripts/lib/codex_pin.sh
. "$ROOT/scripts/lib/codex_pin.sh"

MODE="install"
case "${1:-}" in
  --check|check) MODE="check" ;;
  "") ;;
  *) echo "使い方: $0 [--check]" >&2; exit 2 ;;
esac

DEST="$ROOT/tools/codex"
BIN="$DEST/bin/codex"
KEY="$(codex_pin_platform_key)"

current=""
if [ -x "$BIN" ]; then
  current="$(codex_pin_installed_version "$BIN" || true)"
fi
# 固定版で、かつ付属物（rg・Linux は bwrap）もそろっているか。版だけ一致の本体のみ配置（0.14.28〜31）は偽。
complete=0
if [ -n "$KEY" ] && codex_pin_installed_ok "$KEY" "$DEST" "$CODEX_PIN_VERSION"; then
  complete=1
fi

if [ "$MODE" = "check" ]; then
  echo "固定版: ${CODEX_PIN_VERSION}"
  if [ -n "$current" ]; then
    if [ "$current" = "$CODEX_PIN_VERSION" ]; then
      if [ "$complete" = 1 ]; then
        echo "導入済み（${BIN}）: ${current}（一致・付属物あり）"
      else
        echo "導入済み（${BIN}）: ${current}（版は一致・付属物が不足＝入れ直しが必要）"
      fi
    else
      echo "導入済み（${BIN}）: ${current}（不一致）"
    fi
  else
    echo "導入済み（${BIN}）: なし"
  fi
  if command -v codex >/dev/null 2>&1; then
    echo "PATH 上の codex --version: $(codex --version 2>/dev/null || echo '取得失敗')"
  fi
  exit 0
fi

if [ "$complete" = 1 ]; then
  echo "Codex CLI は既に固定版です（${current}・付属物あり・${BIN}）。"
  exit 0
fi
if [ -n "$current" ] && [ "$current" = "$CODEX_PIN_VERSION" ]; then
  echo "固定版ですが付属物（bwrap・rg）が無い配置です（本体だけの旧い導入）。一式で入れ直します。"
fi

# tools/codex が無く、PATH 上の codex（npm 版など）が既に固定版なら取得しない（閉域で毎回の取得待ちを避ける）。
# ただし付属物（rg・Linux は bwrap）を持つ配置のときだけ（npm 版は持つ・付属物の無い単体ファイルは取得する）。
if [ -z "$current" ] && command -v codex >/dev/null 2>&1; then
  path_ver="$(codex_pin_installed_version "$(command -v codex)" || true)"
  if [ "$path_ver" = "$CODEX_PIN_VERSION" ] && codex_pin_path_codex_has_accessories "$(command -v codex)"; then
    echo "PATH 上の Codex CLI が固定版です（${path_ver}・$(command -v codex)）。取得しません。"
    exit 0
  fi
fi

if [ -z "$KEY" ]; then
  cat >&2 <<EOF
ⓘ この OS/CPU の組み合わせ（$(uname -s) $(uname -m)）は固定版 Codex CLI の自動導入に対応していません。
  対応 OS/CPU: Linux x86_64・Linux aarch64・macOS arm64・macOS x86_64。
  PATH 上に既にある Codex（あれば）をそのまま使います。無い場合、Codex(OpenAI) 経路は使えません。
EOF
  exit 1
fi

ASSET="$(codex_pin_asset_name "$KEY")"
TMP_TARBALL="$(mktemp "${TMPDIR:-/tmp}/codex-pin.XXXXXX")"
echo "固定版 Codex CLI（${CODEX_PIN_VERSION}・${ASSET}）を取得します..."
fetch_rc=0
codex_pin_fetch_asset "$KEY" "$TMP_TARBALL" || fetch_rc=$?
if [ "$fetch_rc" -ne 0 ]; then
  rm -f "$TMP_TARBALL"
  if [ "$fetch_rc" -eq 3 ]; then
    cat >&2 <<EOF
✗ 取得した Codex CLI の配布物が sha256 と一致しませんでした（改ざん・破損の疑い）。導入しませんでした。
  scripts/codex-version.env の固定値を確認してください。
EOF
  else
    existing_msg="PATH 上にも Codex が見つかりません。Codex(OpenAI) 経路は使えません。"
    if [ -x "$BIN" ]; then
      existing_msg="既存の ${BIN}（版: ${current:-不明}）をそのまま使います。"
    elif command -v codex >/dev/null 2>&1; then
      existing_msg="PATH 上の codex（版: $(codex --version 2>/dev/null || echo 不明)）をそのまま使います。"
    fi
    cat >&2 <<EOF
ⓘ 固定版 Codex CLI を取得できませんでした（ネットワーク不可・閉域網など）。
  ${existing_msg}
  閉域網では、閉域キット（scripts/make_offline_kit.sh → scripts/install_offline_kit.sh）で固定版を導入してください。
EOF
  fi
  exit 1
fi

if ! codex_pin_extract_package "$TMP_TARBALL" "$DEST" "$CODEX_PIN_VERSION" "$KEY"; then
  rm -f "$TMP_TARBALL"
  echo "✗ Codex CLI の展開・検査・版確認に失敗しました（既存の ${DEST} は変更していません）。" >&2
  exit 1
fi
rm -f "$TMP_TARBALL"
echo "Codex CLI ${CODEX_PIN_VERSION} を導入しました（${DEST}・付属物込み）。"
