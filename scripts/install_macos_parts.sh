#!/usr/bin/env bash
# macOS のフル導入で、Python と Codex 以外の部品（Node・marp・Chromium・LibreOffice・フォント）をそろえる（オンライン）。
# ./install.sh（prepare_macos）から呼ばれる。Linux のフル導入（install_offline_kit.sh）と同じ部品を入れる。
#
#   tools/ の下（./install.sh が .venv と一緒に退避・作り直し・戻しをする）: Node（tools/node）・marp（tools/marp/node_modules）
#   tools/ の外（システム側・退避の対象外）: Chromium（~/.cache/ms-playwright）・LibreOffice／フォント（Homebrew の cask）
#
# 設計: docs/proposals/2026-10-05-パッケージと導入・更新の一本化.md「依存の指紋」／docs/manual/40-運用.md「導入」。
# 手順: ① Homebrew を探す ② Node ③ marp ④ Chromium ⑤ brew の cask（LibreOffice・フォント） ⑥ Docker を確かめる
# 守ること: macOS 標準の bash 3.2・BSD コマンドで動く書き方に留める。.env と data/ には触れない。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
# shellcheck source=scripts/node-version.env
. "$ROOT/scripts/node-version.env"

note() { echo "・ $*"; }
ok()   { echo "OK: $*"; }
warn() { echo "ⓘ  $*" >&2; }
fail() { echo "✗ $*" >&2; }

PLAYWRIGHT_HOME="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
NODE_DIST_BASE="${SHERPA_NODE_DIST_BASE:-https://nodejs.org/dist}"
CASKS="libreoffice font-noto-sans-cjk-jp font-hackgen"

# --- ① Homebrew ---
BREW="$(command -v brew 2>/dev/null || true)"
if [ -z "$BREW" ]; then
  for cand in /opt/homebrew/bin/brew /usr/local/bin/brew; do
    if [ -x "$cand" ]; then BREW="$cand"; break; fi
  done
fi
if [ -z "$BREW" ]; then
  fail "Homebrew が見つかりません。LibreOffice とフォントを入れるのに使います。"
  fail "  https://brew.sh の手順で Homebrew を入れてから、./install.sh をやり直してください。"
  exit 1
fi

# --- ② Node（tools/node・公式の固定版を sha256 で照合して展開） ---
echo "--- Node.js ---"
case "$(uname -m)" in
  arm64) node_arch=arm64 ;;
  x86_64) node_arch=x64 ;;
  *) fail "未対応の CPU です: $(uname -m)"; exit 1 ;;
esac
node_tar="node-v${NODE_VERSION}-darwin-${node_arch}.tar.gz"
node_work="$(mktemp -d "${TMPDIR:-/tmp}/sherpa-node.XXXXXX")"
trap 'rm -rf "$node_work"' EXIT
curl -fsSL "$NODE_DIST_BASE/v${NODE_VERSION}/${node_tar}" -o "$node_work/$node_tar" \
  && curl -fsSL "$NODE_DIST_BASE/v${NODE_VERSION}/SHASUMS256.txt" -o "$node_work/SHASUMS256.txt" \
  || { fail "Node.js を取得できません。インターネットにつながっているか確認してください。"; exit 1; }
node_want="$(awk -v f="$node_tar" '$2 == f {print $1; exit}' "$node_work/SHASUMS256.txt")"
node_got="$(sherpa_sha256_hex "$node_work/$node_tar")"
if [ -z "$node_want" ] || [ "$node_want" != "$node_got" ]; then
  fail "Node.js の sha256 が一覧と合いません（改ざん・破損の疑い）。"
  exit 1
fi
rm -rf "$ROOT/tools/node"
mkdir -p "$ROOT/tools/node"
tar -xzf "$node_work/$node_tar" -C "$ROOT/tools/node" --strip-components=1
ok "Node.js v${NODE_VERSION} を tools/node に入れました。"
PATH="$ROOT/tools/node/bin:$PATH"

# --- ③ marp（tools/marp/node_modules・package-lock.json の版どおり） ---
echo "--- marp-cli ---"
rm -rf "$ROOT/tools/marp/node_modules"
npm --prefix "$ROOT/tools/marp" ci --no-audit --no-fund
ok "marp-cli を tools/marp/node_modules に入れました。"

# --- ④ Chromium（PDF/PPTX の書き出しに使う・システム側の ~/.cache/ms-playwright） ---
echo "--- Chromium ---"
pw_venv="$node_work/playwright-venv"
"${PYTHON_BIN:-python3}" -m venv "$pw_venv"
"$pw_venv/bin/python" -m pip install --quiet playwright
PLAYWRIGHT_BROWSERS_PATH="$PLAYWRIGHT_HOME" "$pw_venv/bin/python" -m playwright install chromium
ok "Chromium を $PLAYWRIGHT_HOME に入れました（システム側＝./install.sh の退避の対象外）。"

# --- ⑤ LibreOffice・フォント（Homebrew の cask・システム側） ---
echo "--- LibreOffice・フォント ---"
for cask in $CASKS; do
  if "$BREW" list --cask "$cask" >/dev/null 2>&1; then
    note "${cask} は入っています。"
  else
    "$BREW" install --cask "$cask"
  fi
done
ok "LibreOffice とフォント（Noto Sans CJK JP・HackGen）がそろいました（システム側）。"

# --- ⑥ Docker（Docker Desktop は自動では入れない） ---
if ! command -v docker >/dev/null 2>&1; then
  warn "Docker が見つかりません。Docker Desktop を入れて起動してください（make start が Postgres・Neo4j・Elasticsearch に使います）。"
fi
note "OCR（画像内文字の読み取り）のモデルは入れません。使うときは make ocr-models で取得します。"
