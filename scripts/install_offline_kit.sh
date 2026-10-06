#!/usr/bin/env bash
# 完全オフライン（閉域）配布キットの導入スクリプト（閉域の Linux で実行）。
# 通常は直接実行せず、パッケージのルートの ./install.sh（フルのパッケージ）から呼ばれる。
# マニュアル: docs/manual/offline-kit.md（資材一覧・検証チェックリスト）／INSTALL.md（導入・更新の手順）。
#
# 展開済みのパッケージ（Sherpa/dist/offline-kit/ に収集済みの資材がある）のルートで実行すると、資材
# （Python実行系/依存・Docker Engine/イメージ・Node.js/marp・Playwright Chromium（本体＋システム依存）・
# LibreOffice・フォント・Codex CLI・OCR・Ollama）をこのフォルダ自身に導入する。アプリ本体の展開・
# 古いファイルの片付け・インストールの記録は ./install.sh の責務。
#
# 使い方:
#   ./scripts/install_offline_kit.sh
#
# 冪等: 再実行しても壊れない。各ステップは対応する資材が dist/offline-kit/ に無ければ
# 「収集していないためスキップ」して次へ進む。
#
# 注意（M2）: このスクリプトは **root で直接実行しない**。内部で必要な操作だけ sudo を使う設計。
# root で実行すると Chromium/Ollama 等が /root 配下に展開され、sherpa/agents.py の自動検出
# （実行ユーザーの $HOME を見る）から見えなくなるため。

set -Eeuo pipefail   # -E: 下の ERR trap（失敗箇所の表示）を関数内にも継承する
[ "$(uname -s)" = "Linux" ] || { echo "このスクリプトは Linux 専用です（閉域キットの導入は apt・dpkg を使います）。macOS では使えません。" >&2; exit 2; }

if [ "$(id -u)" = 0 ]; then
  echo "✗ このスクリプトを root 直接では実行しないでください（sudo 経由の操作のみ内部で使います）。" >&2
  echo "  root で実行すると Chromium/Ollama 等が /root 配下に展開され、sherpa/agents.py の" >&2
  echo "  自動検出（実行ユーザーの \$HOME を見る）から見えなくなります。一般ユーザーで実行してください。" >&2
  exit 1
fi

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

# Codex CLI（固定版）の展開に使う（sherpa_sha256_hex・codex_pin_extract_bin）。
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
# shellcheck source=scripts/codex-version.env
. "$ROOT/scripts/codex-version.env"
# shellcheck source=scripts/lib/codex_pin.sh
. "$ROOT/scripts/lib/codex_pin.sh"

OUT="$ROOT/dist/offline-kit"
INSTALL_DIR="$ROOT"

usage() {
  cat <<'EOF'
使い方: scripts/install_offline_kit.sh [-h]

前提: dist/offline-kit/（scripts/make_offline_kit.sh --fetch の成果物）が
      このフォルダに含まれていること（フルのパッケージに同梱）。通常は ./install.sh から呼ばれる。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    *) echo "エラー: 不明なオプションです: $1" >&2; usage >&2; exit 2 ;;
  esac
done


note()  { echo "・ $*"; }
ok()    { echo "OK: $*"; }
warn()  { echo "ⓘ  $*" >&2; }
fail()  { echo "✗ $*" >&2; }

# ---------------------------------------------------------------------------
# ログと失敗時の出どころ表示（2026-08-17）
# 閉域では失敗時に「端末の目視」だけが頼りになりがちで、原因調査にはログを持ち出せることが重要。
# 全出力（stdout/stderr）をログファイルへも複写し、失敗時は「どのステップで・何のコマンドが・
# 終了コード何で」止まったかと、ログの場所を最後にまとめて表示する。
# 置き場は data/install-logs/（`make nuke` が消す data/run とは分ける＝初期化後も導入記録は残る）。
# ---------------------------------------------------------------------------
_LOG_FILE="$ROOT/data/install-logs/install-offline-$(date +%Y%m%d-%H%M%S).log"
mkdir -p "$(dirname "$_LOG_FILE")"
exec > >(tee -a "$_LOG_FILE") 2>&1
echo "=== Sherpa オフライン導入 $(date -Iseconds) ==="
echo "版: $(cat "$ROOT/VERSION" 2>/dev/null || echo 不明) / host: $(uname -srm)"
echo "ログ: $_LOG_FILE"
echo ""

CURRENT_STEP="（準備段階）"
_step() { CURRENT_STEP="$1"; echo "--- $1 ---"; }

# set -e で死ぬ瞬間に「どこで」を必ず言う。`|| rc=$?`・if 条件で握った失敗には発火しない
# （それらは各所が自分の fail メッセージを出す＝下の EXIT 側のまとめだけが付く）。
_ERR_SHOWN=0
_on_err() {
  local rc=$1 line=$2 cmd=$3
  _ERR_SHOWN=1
  echo "" >&2
  echo "✗ 導入はここで失敗しました" >&2
  echo "   ステップ : $CURRENT_STEP" >&2
  echo "   コマンド : L$line: ${cmd}（終了コード ${rc}）" >&2
  echo "   ログ全文 : $_LOG_FILE" >&2
  echo "   このログファイル1本を持ち出せば、オンライン側で原因調査ができます。" >&2
}
trap '_on_err $? $LINENO "$BASH_COMMAND"' ERR

# 失敗・正常のどの終わり方でも、バックグラウンドの sudo 延命を止め、最後に「どこまで進んで・ログはどこか」で締める。
_KEEPALIVE_PID=""
_on_exit() {
  local rc=$?
  [ -n "$_KEEPALIVE_PID" ] && kill "$_KEEPALIVE_PID" 2>/dev/null || true
  # ERR 経由なら詳細は表示済み＝まとめだけ。fail→exit 1 の経路でもここは通る。
  if [ "$rc" != 0 ] && [ "${_ERR_SHOWN:-0}" != 1 ]; then
    echo "" >&2
    echo "✗ 導入は完了していません（ステップ「${CURRENT_STEP:-（準備段階）}」で中断・終了コード ${rc}）" >&2
    echo "   ログ全文 : ${_LOG_FILE:-（ログ開始前）}" >&2
  elif [ "$rc" = 0 ] && [ -n "${_LOG_FILE:-}" ]; then
    echo "ログ: $_LOG_FILE"
  fi
}
trap _on_exit EXIT


# M3→2026-08-17: ローカル .deb 一式の導入は scripts/lib/apt_offline.sh に集約した（-s 先行・--no-remove・
# 非対話・索引付きキットは file: repo として名前解決・カーネル残存確認・不足名の表示）。
# 旧 `sudo apt-get install -y ./*.deb` は削除提案（稼働カーネルを含み得る）を黙って通し、土台ずれで全停止
# したため廃止（診断 2026-08-17）。ここは 5 箇所の呼び出しの意味（戻り値 0/1/2）を変えない薄いラッパ。
# 戻り値: 0=導入成功 1=該当 .deb が無い（スキップ・非致命的） 2=導入失敗（致命的）
# shellcheck source=scripts/lib/apt_offline.sh
. "$ROOT/scripts/lib/apt_offline.sh"
_apt_install_local_debs() {  # $1=説明（ログ用） $2=.deb を含むディレクトリ（名前は $2/PACKAGES から）
  apt_offline_install "$1" "$OUT" "$2"
}

echo "=== 完全オフライン配布キットの導入 ==="
echo "資材: $OUT"
echo "導入先: $INSTALL_DIR"
echo ""


# ---------------------------------------------------------------------------
# 0. dist/offline-kit/ の存在確認
# ---------------------------------------------------------------------------
if [ ! -d "$OUT" ]; then
  fail "$OUT が見つかりません。"
  fail "オンライン側で ./scripts/make_offline_kit.sh --fetch を実行したチェックアウトを、"
  fail "dist/offline-kit/ ごとこの閉域環境へ移送してください。"
  exit 1
fi
ok "資材ディレクトリを確認: $OUT"
echo ""

# ---------------------------------------------------------------------------
# M2: sudo を前払いし、長時間ステップ（apt-get install・docker load 等）の途中で
# 認証が失効しないよう、バックグラウンドで sudo -n true を定期実行して延命する。
# ---------------------------------------------------------------------------
echo "このスクリプトは以降の手順で sudo 権限を使います"
echo "（Docker Engine/Python実行系/LibreOffice/フォントの導入、docker サービス有効化等）。"
sudo -v
(
  while true; do
    sleep 60
    sudo -n true 2>/dev/null || exit
  done
) &
_KEEPALIVE_PID=$!
echo ""
# ---------------------------------------------------------------------------
# 0.5 土台の事前照合（2026-08-17）: 収集側が書いた BASELINE（OS/版/コードネーム/arch/収集日/イメージ digest）
# とこの機体を照合し、不一致なら .deb 導入に入る前に止める（依存名・版が合わず途中で止まるより早く・明確に）。
# SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH=1 で警告のみ。BASELINE の無い旧キットは警告して続行。
# ---------------------------------------------------------------------------
_step "0.5 土台の事前照合（BASELINE）"
if [ -f "$OUT/BASELINE" ]; then
  echo "BASELINE: $(tr '\n' ' ' < "$OUT/BASELINE")"
fi
apt_offline_check_baseline "$OUT" || exit 1
echo ""

_step "1. 設定ファイルの初期作成"
# .env の初期作成（2026-09-04 ユーザー裁定）: 無ければ .env.example から作成する。
# **既存の .env には絶対に触れない**（上書き・追記とも不可＝運用中の設定・秘密を壊さない。
# cp -n は「上書きしなかった」ことを静かに成功にしてしまうため使わず、存在チェックを明示する）。
if [ -f "$INSTALL_DIR/.env" ]; then
  note ".env は既に存在します（保持・上書きしません）: $INSTALL_DIR/.env"
elif [ -f "$INSTALL_DIR/.env.example" ]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  ok ".env を作成しました（.env.example から）。冒頭「0. 本番チェックリスト」節を必ず設定してください: $INSTALL_DIR/.env"
else
  warn ".env.example が見つかりません（${INSTALL_DIR}）。.env の初期作成をスキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 2. Python 実行系（python3 / python3-venv / python3-pip・H3）
#    素の閉域ホストには python3-venv が無いことが多く、venv 作成が最初に転ぶため先に導入する。
# ---------------------------------------------------------------------------
_step "2. Python 実行系＋基本ツール"
# R4: `python3 -c 'import venv'` は python3-venv が未導入の Debian/Ubuntu でも成功する
# （venv モジュール自体は python3-minimal に含まれ、実体の ensurepip 相当が別パッケージ）。
# 実際に venv を作れるかで判定する（一時ディレクトリに作って即削除）。
# 同梱 .deb には xz-utils/unzip/fontconfig（Node 展開・HackGen 展開・fc-cache）も含むため、
# それらの有無も併せて判定する。
_PROBE_VENV="$(mktemp -d "${TMPDIR:-/tmp}/sherpa.XXXXXX")/venv-probe"
if command -v python3 >/dev/null 2>&1 && python3 -m venv "$_PROBE_VENV" >/dev/null 2>&1 \
    && command -v xz >/dev/null 2>&1 && command -v unzip >/dev/null 2>&1 && command -v fc-cache >/dev/null 2>&1; then
  rm -rf "$(dirname "$_PROBE_VENV")"
  note "python3/venv・xz・unzip・fontconfig は既に使えるため、同梱分の導入をスキップします。"
else
  rm -rf "$(dirname "$_PROBE_VENV")" 2>/dev/null || true
  rc=0
  _apt_install_local_debs "Python 実行系" "$OUT/python/debs" || rc=$?
  [ "$rc" = 2 ] && exit 1
fi
echo ""

# ---------------------------------------------------------------------------
# 3. Docker Engine 本体（H3: Docker Engine の debs → systemctl enable → usermod の順）
# ---------------------------------------------------------------------------
_step "3. Docker Engine"
DOCKER_GROUP_JUST_ADDED=0
if command -v docker >/dev/null 2>&1; then
  note "docker は既に導入されているため、この手順はスキップします。"
else
  rc=0
  _apt_install_local_debs "Docker Engine" "$OUT/docker-engine/debs" || rc=$?
  if [ "$rc" = 2 ]; then
    exit 1
  elif [ "$rc" = 0 ]; then
    note "docker サービスを有効化・起動します（systemctl enable --now docker）..."
    sudo systemctl enable --now docker
    note "現在のユーザーを docker グループへ追加します（反映には再ログインが必要です）..."
    sudo usermod -aG docker "$USER"
    DOCKER_GROUP_JUST_ADDED=1
    ok "Docker Engine を導入しました。"
  fi
fi
# R2: usermod -aG は現行シェルのグループに即反映されない（再ログインが要る）。今回入れたばかりの
# 場合に加え、既存 Docker でも実行ユーザーが docker グループ未所属なら同じ権限エラーになるため、
# docker info の実疎通で判定して sudo docker へ逃がす（sudo は前払い済み）。
DOCKER_CMD="docker"
if [ "$DOCKER_GROUP_JUST_ADDED" = 1 ]; then
  DOCKER_CMD="sudo docker"
elif command -v docker >/dev/null 2>&1 && ! docker info >/dev/null 2>&1; then
  DOCKER_CMD="sudo docker"
fi
echo ""

# ---------------------------------------------------------------------------
# 4. Docker イメージ（docker load）
# ---------------------------------------------------------------------------
_step "4. Docker イメージ"
if [ -d "$OUT/docker-images" ] && [ -n "$(find "$OUT/docker-images" -maxdepth 1 -name '*.tar' 2>/dev/null)" ]; then
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker が見つかりません。3番の Docker Engine 導入が必要です（.deb 未収集ならオンライン側で収集してください）。"
    exit 1
  fi
  for tarfile in "$OUT/docker-images"/*.tar; do
    note "docker load -i $tarfile"
    $DOCKER_CMD load -i "$tarfile"
  done
  ok "Docker イメージを読み込みました。"
  $DOCKER_CMD images | grep -E 'postgres|neo4j|es-kuromoji' || true
else
  warn "Docker イメージが見つかりません（$OUT/docker-images/）。オンライン側で収集していないためスキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 5. Python venv（--no-index・PyPI に一切出ない）
# ---------------------------------------------------------------------------
_step "5. Python 依存（venv・オフライン install）"
PY="${PYTHON_BIN:-python3}"
# R6a RV2 MEDIUM（2026-07-15）: manifest があるなら**無条件に**検証へ入る。外側を「*.whl/*.tar.gz が
# 在るか」でゲートすると、転送破損で wheel が全滅し SHA256SUMS だけ残った壊れ方が「未収集」扱いで
# スキップされ成功終了する（fail-open）。拡張子判定は manifest の無い旧キットのスキップ判定にだけ使う。
if [ -f "$OUT/wheels/SHA256SUMS" ] || { [ -d "$OUT/wheels" ] && [ -n "$(find "$OUT/wheels" -maxdepth 1 \( -name '*.whl' -o -name '*.tar.gz' \) 2>/dev/null)" ]; }; then
  # R6a（2026-07-13-横断レビュー対応.md）: pip install --no-index の前に wheels の SHA256 manifest を
  # 検証する。改ざん・転送破損（sha256sum -c＝掲載ファイルの欠落も検出）に加え、manifest 未掲載
  # エントリの混入も検出する（--find-links は同名でも版の高い野良 wheel を優先採用しうるため、
  # リストにない混入は握り潰さない）。
  # 未掲載検出の対象は SHA256SUMS 自身を除く**全ディレクトリエントリ**（RV HIGH 2026-07-15 ×2:
  # pip は .whl/.tar.gz 以外にも .zip/.tgz/.tar.bz2 等を候補にする＝拡張子の列挙だと漏れが残る。
  # さらに -type f だとシンボリックリンクを見逃し、正規名のリンクは pip の候補になる＝type も絞らない）。
  if [ -f "$OUT/wheels/SHA256SUMS" ]; then
    note "wheels の SHA256 manifest を検証します（sha256sum -c）..."
    if ! (cd "$OUT/wheels" && sha256sum -c SHA256SUMS); then
      fail "wheels の SHA256 manifest 検証に失敗しました（改ざん・破損・欠落の疑い）: $OUT/wheels/SHA256SUMS"
      exit 1
    fi
    ok "wheels の SHA256 manifest 検証OK。"

    # set -euo pipefail 下の防御: find の空ヒット・awk の出力なしは正常系のため、
    # ここでの比較は代入を分けて行い、途中で落ちないようにする（RV コメント群と同じ流儀）。
    ACTUAL_WHEEL_FILES="$(cd "$OUT/wheels" && find . -mindepth 1 -maxdepth 1 ! -name 'SHA256SUMS' -printf '%f\n' | sort || true)"
    MANIFEST_WHEEL_FILES="$(awk '{print $2}' "$OUT/wheels/SHA256SUMS" | sort || true)"
    EXTRA_WHEEL_FILES="$(comm -23 <(printf '%s\n' "$ACTUAL_WHEEL_FILES") <(printf '%s\n' "$MANIFEST_WHEEL_FILES") || true)"
    if [ -n "$EXTRA_WHEEL_FILES" ]; then
      fail "wheels に SHA256 manifest 未掲載のエントリがあります（野良 wheel/リンク混入の疑い）:"
      printf '%s\n' "$EXTRA_WHEEL_FILES" | while IFS= read -r f; do fail "  $f"; done
      exit 1
    fi
  else
    warn "wheels の SHA256 manifest が見つかりません（$OUT/wheels/SHA256SUMS）。R6a 以前に収集された"
    warn "  旧キットのため検証をスキップして続行します。"
  fi

  if ! command -v "$PY" >/dev/null 2>&1; then
    fail "$PY が見つかりません。2番の Python 実行系導入を確認してください。"
    exit 1
  fi
  VENV="$INSTALL_DIR/.venv"
  if [ ! -x "$VENV/bin/python" ]; then
    note "venv を作成します: $VENV"
    "$PY" -m venv "$VENV"
  else
    note "既存の venv を再利用します: $VENV"
  fi
  # Tree-sitter（本体＋文法）はホイールだけを入れる（sdist が混じっていたら閉域でビルドへ進まず止める）。
  if "$VENV/bin/python" -m pip install --no-index --find-links "$OUT/wheels" \
      --only-binary tree-sitter,tree-sitter-java,tree-sitter-c-sharp,tree-sitter-c,tree-sitter-javascript,tree-sitter-bash,tree-sitter-css,tree-sitter-embedded-template \
      -r "$INSTALL_DIR/requirements.txt" -c "$INSTALL_DIR/constraints.txt"; then
    ok "Python 依存を --no-index でインストールしました: $VENV"
    # start.sh が「依存は最新」と判断できるよう、同じ式でハッシュを記録する（無いと閉域で PyPI へ出て止まる）。
    # shellcheck source=scripts/lib/req_hash.sh
    . "$ROOT/scripts/lib/req_hash.sh"
    (cd "$INSTALL_DIR" && req_hash "$VENV/bin/python") > "$VENV/.requirements.sha256"
  else
    fail "pip install --no-index に失敗しました。wheel 一式（$OUT/wheels/）とこの機体の Python バージョンが"
    fail "一致しているか確認してください（$OUT/wheels/COLLECTED-WITH-PYTHON-VERSION.txt を参照）。"
    exit 1
  fi
else
  fail "wheel 一式が見つかりません（$OUT/wheels/）。Python 依存を導入できないため中止します。"
  fail "  対処: オンライン側で make_offline_kit.sh --fetch を実行して wheels を収集したキットを搬入してください。"
  exit 1
fi
echo ""

# ---------------------------------------------------------------------------
# 6. Node.js（tools/node/ へ展開）
# ---------------------------------------------------------------------------
_step "6. Node.js"
# 空ディレクトリは zip 等の移送で落ちることがあるため、find 前に存在を確認する（HackGen と同じ防御）。
NODE_TARBALL=""
if [ -d "$OUT/node" ]; then
  NODE_TARBALL="$(find "$OUT/node" -maxdepth 1 -name 'node-v*.tar.xz' | head -1 || true)"
fi
if [ -n "$NODE_TARBALL" ]; then
  NODE_DEST="$INSTALL_DIR/tools/node"
  rm -rf "$NODE_DEST"
  mkdir -p "$NODE_DEST"
  tar -xJf "$NODE_TARBALL" -C "$NODE_DEST" --strip-components=1
  ok "Node.js を展開しました: $NODE_DEST"
  # M4: scripts/run-common.sh が tools/node/bin を自動で PATH の先頭へ足すため、手動設定は不要
  # （start.sh 等 run-common.sh を source するスクリプト経由で起動した場合のみ有効）。
  note "PATH は scripts/run-common.sh が自動で通します（tools/node/bin が存在する時のみ・手動設定は不要）。"
else
  warn "Node.js の tarball が見つかりません（$OUT/node/）。オンライン側で収集していないためスキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 7. marp-cli（tools/marp/node_modules へ展開・sherpa/agents.py _marp_bin が参照）
# ---------------------------------------------------------------------------
_step "7. marp-cli"
MARP_TARBALL="$OUT/marp/tools-marp-node_modules.tar.gz"
if [ -f "$MARP_TARBALL" ]; then
  MARP_DEST="$INSTALL_DIR/tools/marp"
  rm -rf "$MARP_DEST/node_modules"
  mkdir -p "$MARP_DEST"
  tar -xzf "$MARP_TARBALL" -C "$MARP_DEST"
  ok "marp-cli を展開しました: $MARP_DEST/node_modules"
else
  warn "marp-cli の tarball が見つかりません（${MARP_TARBALL}）。オンライン側で収集していないためスキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 7b. Codex CLI（固定版・package 一式を tools/codex/ へ展開＝bin/codex・codex-path/rg・Linux は codex-resources/bwrap。npm/node は不要）
#     sherpa は `shutil.which("codex")` で探す。run-common.sh が tools/codex/bin を PATH に足す。
#     認証は Sherpa を動かすユーザーで `printenv OPENAI_API_KEY | codex login --with-api-key`
#     （~/.codex/auth.json を書くだけ・通信不要・実測 2026-08-18）。推論時だけ OpenAI へ出る。
# ---------------------------------------------------------------------------
_step "7b. Codex CLI"
# このホストの OS/CPU の固定アセット名だけを選び、リポジトリに固定した sha256 と照合する
# （キット内の SHA256SUMS やファイルの並びには頼らない）。
CODEX_KEY="$(codex_pin_platform_key)"
CODEX_TARBALL=""
[ -n "$CODEX_KEY" ] && CODEX_TARBALL="$OUT/codex/$(codex_pin_asset_name "$CODEX_KEY")"
if [ -n "$CODEX_TARBALL" ] && [ -f "$CODEX_TARBALL" ]; then
  if [ "$(sherpa_sha256_hex "$CODEX_TARBALL")" != "$(codex_pin_sha256 "$CODEX_KEY")" ]; then
    fail "Codex CLI の tarball が固定の sha256（scripts/codex-version.env）と一致しません（搬入時の破損/すり替え）。"; exit 1
  fi
  CODEX_DEST="$INSTALL_DIR/tools/codex"
  # 検査（脱出パス・リンクの拒否・付属物の存在・版）→ tools/codex 全体を rename で入れ替える
  # （旧キットの npm 導入ツリー・本体だけの旧い配置の残りも丸ごと置き換わる）。
  if ! codex_pin_extract_package "$CODEX_TARBALL" "$CODEX_DEST" "$CODEX_PIN_VERSION" "$CODEX_KEY"; then
    fail "Codex CLI の展開・検査・版確認に失敗しました: $CODEX_TARBALL"; exit 1
  fi
  ok "Codex CLI を展開しました: $CODEX_DEST/bin/codex（${CODEX_PIN_VERSION}・bwrap・rg 等の付属物込み）"
  # env ファイル（SHERPA_ENV_FILE ＞ /etc/sherpa/sherpa.env）に OPENAI_API_KEY があれば、ここで認証まで済ませる
  # （通信なし・冪等・run-common の sherpa_codex_ensure_auth）。**この導入を実行しているユーザーの** ~/.codex に
  # 書くので、Sherpa を動かすユーザーで導入していることが前提（root 実行は冒頭で拒否済み）。
  _CX_ENV="${SHERPA_ENV_FILE:-}"; [ -z "$_CX_ENV" ] && [ -f /etc/sherpa/sherpa.env ] && _CX_ENV=/etc/sherpa/sherpa.env
  if [ -n "$_CX_ENV" ] && [ -f "$_CX_ENV" ]; then
    # shellcheck source=scripts/run-common.sh
    if ( SHERPA_ENV_FILE="$_CX_ENV" PATH="$CODEX_DEST/bin:$PATH"; . "$ROOT/scripts/run-common.sh"; sherpa_codex_ensure_auth ); then
      ok "Codex CLI の API キー認証を済ませました（$_CX_ENV の OPENAI_API_KEY・通信なし）"
    else
      note "  Codex CLI の認証は未実施（$_CX_ENV に OPENAI_API_KEY が無い等）。make start 時にキーがあれば自動で行います。"
      note "  手動なら: printenv OPENAI_API_KEY | codex login --with-api-key（Sherpa を動かすユーザーで・通信不要）"
    fi
  else
    note "  認証は make start が .env の OPENAI_API_KEY で自動的に行います（通信不要）。"
    note "  手動なら: printenv OPENAI_API_KEY | codex login --with-api-key"
  fi
  note "  推論時は OpenAI API へ到達できる必要があります（閉域なら api.openai.com への穴あけ）。"
else
  warn "このホスト（$(uname -s) $(uname -m)）向けの Codex CLI の tarball が見つかりません（$OUT/codex/）。--skip-codex で収集していないか、キットの対象 OS/CPU（CODEX_PIN_KIT_PLATFORM）が違います。スキップします。"
  note "  この状態では Codex(OpenAI)/Codex(Ollama) 構成は使えません（OpenAI 直結・ローカル(Ollama) は使えます）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 8. Playwright Chromium（本体＋システム共有ライブラリ .deb）
# ---------------------------------------------------------------------------
_step "8. Playwright Chromium"
CHROMIUM_TARBALL="$OUT/chromium/ms-playwright-chromium.tar.gz"
if [ -f "$CHROMIUM_TARBALL" ]; then
  PLAYWRIGHT_HOME="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
  mkdir -p "$PLAYWRIGHT_HOME"
  # L2: 収集側は ms-playwright/ 丸ごとでなく chromium-*/chromium_headless_shell-*/ffmpeg-* だけを
  # tar 化している（tar 内はこれらのディレクトリが直下に並ぶ構造）ので、展開先も PLAYWRIGHT_HOME
  # 直下に上書き展開すれば _detect_chrome_path() のグロブパターンに合う配置になる
  # （PLAYWRIGHT_HOME 全体を rm -rf すると firefox/webkit 等の既存ブラウザを壊すため、
  # 展開されるディレクトリだけを事前に消してから展開する）。
  tar -tzf "$CHROMIUM_TARBALL" | awk -F/ '{print $1}' | sort -u | while read -r d; do
    [ -n "$d" ] && rm -rf "${PLAYWRIGHT_HOME:?}/$d"
  done
  tar -xzf "$CHROMIUM_TARBALL" -C "$PLAYWRIGHT_HOME"
  ok "Playwright Chromium 本体を展開しました: $PLAYWRIGHT_HOME"

  # H2: システム共有ライブラリ（libnss3 等）。無いと Chromium が起動せず PDF/PPTX 出力が失敗する。
  rc=0
  _apt_install_local_debs "Chromium システム依存" "$OUT/chromium/deps-debs" || rc=$?
  if [ "$rc" = 2 ]; then
    warn "Chromium システム依存の導入に失敗しました（本体は展開済み・PDF/PPTX 出力は失敗する可能性があります）。"
  fi
else
  warn "Playwright Chromium の tarball が見つかりません（${CHROMIUM_TARBALL}）。オンライン側で収集していないためスキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 9. LibreOffice
# ---------------------------------------------------------------------------
_step "9. LibreOffice"
rc=0
_apt_install_local_debs "LibreOffice" "$OUT/libreoffice/debs" || rc=$?
[ "$rc" = 2 ] && exit 1
echo ""

# ---------------------------------------------------------------------------
# 10. フォント（Noto Sans CJK JP: apt_offline_install（-s 先行・--no-remove）／HackGen: 展開して ~/.local/share/fonts/ へ）
# ---------------------------------------------------------------------------
_step "10. フォント"
_apt_install_local_debs "Noto Sans CJK JP" "$OUT/fonts/noto-cjk-debs" || true

# RV High（2026-07-09）: fonts/hackgen が無い（収集スキップ/失敗）場合、find の exit 1 が
# pipefail 経由で代入を失敗させ set -e で即死する＝「未収集ならスキップ」に到達しない。
HACKGEN_ZIP=""
if [ -d "$OUT/fonts/hackgen" ]; then
  HACKGEN_ZIP="$(find "$OUT/fonts/hackgen" -maxdepth 1 -name '*.zip' | head -1 || true)"
fi
if [ -n "$HACKGEN_ZIP" ]; then
  if command -v unzip >/dev/null 2>&1; then
    FONT_DEST="$HOME/.local/share/fonts"
    mkdir -p "$FONT_DEST"
    TMP_UNZIP="$(mktemp -d "${TMPDIR:-/tmp}/sherpa.XXXXXX")"
    unzip -oq "$HACKGEN_ZIP" -d "$TMP_UNZIP"
    find "$TMP_UNZIP" -type f \( -name '*.ttf' -o -name '*.otf' \) -exec cp -f {} "$FONT_DEST/" \;
    rm -rf "$TMP_UNZIP"
    if command -v fc-cache >/dev/null 2>&1; then
      fc-cache -f "$FONT_DEST" >/dev/null 2>&1 || true
    fi
    ok "HackGen を導入しました: $FONT_DEST"
  else
    warn "unzip が見つかりません。HackGen の展開をスキップします。"
  fi
else
  warn "HackGen の zip が見つかりません（$OUT/fonts/hackgen/）。スキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 11. Ollama モデルデータ（~/.ollama へコピー）
# ---------------------------------------------------------------------------
_step "11. Ollama モデルデータ"
if [ -d "$OUT/ollama/dot-ollama" ]; then
  OLLAMA_HOME="${OLLAMA_MODELS_DIR:-$HOME/.ollama}"
  mkdir -p "$OLLAMA_HOME"
  cp -a "$OUT/ollama/dot-ollama/." "$OLLAMA_HOME/"
  ok "Ollama モデルデータを導入しました: $OLLAMA_HOME"
  note "ollama 本体バイナリは含まれません。別途このホストへ導入してください。"
else
  warn "Ollama モデルデータが見つかりません（$OUT/ollama/dot-ollama/）。--with-ollama で収集していないためスキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 11b. OCR（画像内文字の読み取り・既定ON）
#      イメージ・モデルの2つを置く。モデルは読み取り専用でワーカーへ渡すため、
#      アプリの派生物とは別の場所（data/ocr-models）に置く。
# ---------------------------------------------------------------------------
_step "11b. OCR（画像内文字の読み取り）"
if [ -d "$OUT/ocr" ] && [ -f "$OUT/ocr/ocr-worker-paddleocr-3.7.0-cpu.tar" ]; then
  $DOCKER_CMD load -i "$OUT/ocr/ocr-worker-paddleocr-3.7.0-cpu.tar"   # 工程4と同じく sudo フォールバック（グループ追加直後は素の docker が permission denied・閉域実機 2026-09-04）
  ok "OCR ワーカーのイメージを読み込みました。"
  if [ -d "$OUT/ocr/models" ]; then
    OCR_MODEL_DEST="${SHERPA_OCR_MODEL_CACHE:-$INSTALL_DIR/data/ocr-models}"
    mkdir -p "$OCR_MODEL_DEST"
    cp -a "$OUT/ocr/models/." "$OCR_MODEL_DEST/"
    ok "OCR のモデルを配置しました: $OCR_MODEL_DEST"
  else
    warn "OCR のモデルが見つかりません（$OUT/ocr/models/）。画像内文字は読み取れません。"
  fi
  note "資料フォルダを登録したあと make start（または make up）でワーカーが起動します。"
else
  warn "OCR の資材が見つかりません（$OUT/ocr/）。--skip-ocr で収集していない場合はスキップされます。"
  note "この状態でも取り込み・検索は動きます（画像の中の文字だけが読まれません）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 12. 最終検証（M6）: 各項目を実行して OK/NG/未収集 の一覧表を表示する。
#     未収集（対応する資材をそもそも集めていない）場合は NG 扱いにしない。
# ---------------------------------------------------------------------------
_step "12. 最終検証"
VERIFY_FAILED=0
_verify() {  # $1=項目名 $2=collected?(0/1) $3=check用コマンド文字列（bash -c で実行）
  local name="$1" collected="$2" cmd="$3"
  if [ "$collected" != 1 ]; then
    printf '  [未収集] %s\n' "$name"
    return
  fi
  if bash -c "$cmd" >/dev/null 2>&1; then
    printf '  [OK]    %s\n' "$name"
  else
    printf '  [NG]    %s\n' "$name"
    VERIFY_FAILED=1
  fi
}

# R1: set -e 下で `A && B && C=1` は A/B が偽のとき行全体が非ゼロ終了しスクリプトごと落ちる
# （「未収集」を検出したいまさにその場面で落ちる）。if 文で判定し、代入自体は必ず成功させる。
DOCKER_IMAGES_COLLECTED=0
if [ -d "$OUT/docker-images" ] && [ -n "$(find "$OUT/docker-images" -maxdepth 1 -name '*.tar' 2>/dev/null)" ]; then
  DOCKER_IMAGES_COLLECTED=1
fi
WHEELS_COLLECTED=0
if [ -d "$OUT/wheels" ] && [ -n "$(find "$OUT/wheels" -maxdepth 1 \( -name '*.whl' -o -name '*.tar.gz' \) 2>/dev/null)" ]; then
  WHEELS_COLLECTED=1
fi
NODE_COLLECTED=0
if [ -n "$(find "$OUT/node" -maxdepth 1 -name 'node-v*.tar.xz' 2>/dev/null)" ]; then
  NODE_COLLECTED=1
fi
MARP_COLLECTED=0
[ -f "$MARP_TARBALL" ] && MARP_COLLECTED=1
CHROMIUM_COLLECTED=0
[ -f "$CHROMIUM_TARBALL" ] && CHROMIUM_COLLECTED=1
LIBREOFFICE_COLLECTED=0
if [ -d "$OUT/libreoffice/debs" ] && [ -n "$(find "$OUT/libreoffice/debs" -maxdepth 1 -name '*.deb' 2>/dev/null)" ]; then
  LIBREOFFICE_COLLECTED=1
fi
FONTS_COLLECTED=0
if { [ -d "$OUT/fonts/noto-cjk-debs" ] && [ -n "$(find "$OUT/fonts/noto-cjk-debs" -maxdepth 1 -name '*.deb' 2>/dev/null)" ]; } || [ -n "$HACKGEN_ZIP" ]; then
  FONTS_COLLECTED=1
fi

echo "検証結果:"
_verify "Docker イメージ（postgres:16 / neo4j:5-community / sherpa/es-kuromoji:8.19.20）" "$DOCKER_IMAGES_COLLECTED" \
  "$DOCKER_CMD images --format '{{.Repository}}:{{.Tag}}' | grep -qE '^postgres:16$' && $DOCKER_CMD images --format '{{.Repository}}:{{.Tag}}' | grep -qE '^neo4j:5-community$' && $DOCKER_CMD images --format '{{.Repository}}:{{.Tag}}' | grep -qE '^sherpa/es-kuromoji:8.19.20$'"
_verify "Python 依存（pip check）" "$WHEELS_COLLECTED" "'$INSTALL_DIR/.venv/bin/python' -m pip check"
_verify "Node.js（tools/node/bin/node）" "$NODE_COLLECTED" "'$INSTALL_DIR/tools/node/bin/node' --version"
_verify "marp-cli（PATH に tools/node/bin を通した状態）" "$MARP_COLLECTED" \
  "PATH=\"$INSTALL_DIR/tools/node/bin:\$PATH\" '$INSTALL_DIR/tools/marp/node_modules/.bin/marp' --version"
_verify "Playwright Chromium（chromium-*/chrome-linux64/chrome）" "$CHROMIUM_COLLECTED" \
  "chrome_bin=\$(ls \"${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}\"/chromium-*/chrome-linux64/chrome 2>/dev/null | head -1); [ -n \"\$chrome_bin\" ] && \"\$chrome_bin\" --version"
_verify "LibreOffice（soffice）" "$LIBREOFFICE_COLLECTED" "soffice --version"
_verify "フォント（Noto Sans CJK JP / HackGen）" "$FONTS_COLLECTED" "fc-list | grep -iE 'noto sans cjk|hackgen'"
OCR_COLLECTED=0
[ -f "$OUT/ocr/ocr-worker-paddleocr-3.7.0-cpu.tar" ] && OCR_COLLECTED=1
CODEX_COLLECTED=0
[ -n "$(ls "$OUT"/codex/codex-*.tar.gz 2>/dev/null)" ] && CODEX_COLLECTED=1
_verify "Codex CLI（tools/codex/bin/codex --version・codex-path/rg）" "$CODEX_COLLECTED" "'$INSTALL_DIR/tools/codex/bin/codex' --version && [ -x '$INSTALL_DIR/tools/codex/codex-path/rg' ]"
# モデルの照合はワーカー自身が起動時に行う（固定 hash と一致しなければ available=false）。
# ここではイメージが読み込めていること・モデルが置かれていることだけを見る。
_verify "OCR（ワーカーのイメージとモデル）" "$OCR_COLLECTED" \
  "$DOCKER_CMD image inspect sherpa/ocr-worker:paddleocr-3.7.0-cpu >/dev/null && [ -d \"${SHERPA_OCR_MODEL_CACHE:-$INSTALL_DIR/data/ocr-models}/official_models\" ]"
echo ""

# ---------------------------------------------------------------------------
# サマリ
# ---------------------------------------------------------------------------
echo "=== 導入完了 ==="
echo ""
echo "次にやること:"
# 本キットは Codex CLI を同梱し（7b）、OPENAI_API_KEY があれば導入時点で認証まで自動で済ませる
# ため、「OPENAI_API_KEY 等は空のままにする」とは案内しない。AI なしの定型応答（heuristic）は
# チャットで閉じており案内しない（実装＝sherpa/agent_constructs.py・sherpa/providers/__init__.py）。
echo "  1) $INSTALL_DIR/.env（または /etc/sherpa/sherpa.env）を設定する（閉域での構成は実際には2通り）:"
# S3（2026-08-18-AzureOpenAI対応）: 実行環境が Azure OpenAI 経由のこともあるため、a) に Azure の
# 設定（OPENAI_BASE_URL＋モデル欄=デプロイ名）を追記した（sherpa/llm.py::openai_base_url。Azure 分岐は
# 作らず「OpenAI 互換の接続先」を設定化しただけ＝OPENAI_API_KEY を設定する導線自体は変えていない）。
echo "     a) OpenAI または Azure OpenAI へ穴あけがある: OPENAI_API_KEY を設定する（Azure ならその"
echo "        キー。同梱の Codex CLI は 7b でキーがあれば認証まで自動で済んでいる。頭脳は"
echo "        自動選択され、チャットで選び直せる）。Azure なら加えて"
echo "        OPENAI_BASE_URL=https://<リソース名>.openai.azure.com/openai/v1/ を設定する"
echo "        （モデル欄には Azure の「デプロイ名」を入れる）。"
echo "     b) 外へ出られない・ローカル LLM（Ollama）がある: OLLAMA_URL を設定する（頭脳は簡易が選ばれる）。"
echo "     （AI を使わない定型文の応答はチャットでは廃止しました。a) か b) のどちらかが必要です。）"
# 閉域実機報告⑧（2026-08-18）: 「不足していれば恒久設定」だけの案内だと、(a) 既に十分な値をさらに
# 下げてしまう・(b) 同居する別製品が別ファイルで設定したより大きい値を後勝ちで踏む、の事故になる
# （実機は /etc/sysctl.d/10-map-count.conf=1048576 を同居製品が置いており、素直に 262144 を書くと
# 下がって同居製品が壊れた）。「現在値が足りているか」「既存設定の探し方」「後勝ちの順序」を明示する。
echo "  2) Elasticsearch は vm.max_map_count=262144 以上を要求します。現在値を確認してください:"
echo "       sysctl vm.max_map_count"
echo "     262144 以上なら何もしないでください（下げると壊れます・同居する別製品がより大きい値を"
echo "     要求している場合があります）。既存の設定ファイルを探すには:"
echo "       grep -rn max_map_count /etc/sysctl.conf /etc/sysctl.d/"
echo "     不足しているときだけ、新規ファイルとして追加してください:"
echo "       echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-sherpa-vm-max-map-count.conf"
echo "       sudo sysctl --system"
echo "     /etc/sysctl.d/ はファイル名の番号順に読まれ後勝ちです。同居製品が別ファイル（例 10-*.conf）で"
echo "     より大きい値を設定している場合は、そちらが優先されるよう Sherpa 用ファイルは置かないでください"
echo "     （どうしても両方置くなら、Sherpa 側を同居製品より小さい番号にする）。"
# 閉域実機報告⑦（2026-08-18）: 末尾の起動案内が docker compose up -d に続けて run-api.sh を素の
# serve 引数で直接起動するだけ（127.0.0.1 待受固定）で、社内 LAN の他端末から使わせる前提なのに
# LAN=1 の案内が無かった。起動はポート検査まで一括で行う make start（＝scripts/start.sh）を
# 主に案内し、LAN 公開の付け方を明示する。
echo "  3) 起動は make start（ストア起動＋アプリ起動＋ポート検査を一括で行います。docker compose up -d"
echo "     は内部で実行されるため個別に呼ぶ必要はありません）。社内 LAN の他端末からも使わせるなら"
echo "     LAN=1 を付けてください（毎回付けたくなければ env ファイルに SHERPA_LAN=1 と書けば LAN=1 無しの"
echo "     make start でも LAN 公開になります）。このホストだけで使う（127.0.0.1 のみで足りる）なら"
echo "     どちらも付けなくて構いません。"
echo "     例: SHERPA_ENV_FILE=/etc/sherpa/sherpa.env make start        # 127.0.0.1 のみ"
echo "         SHERPA_ENV_FILE=/etc/sherpa/sherpa.env LAN=1 make start  # LAN 公開"
if [ "$DOCKER_GROUP_JUST_ADDED" = 1 ]; then
  echo "  ※ docker グループへの追加をしました。反映には再ログイン（または newgrp docker）が必要です。"
fi
echo ""
echo "詳しい検証チェックリストは docs/manual/offline-kit.md を参照してください。"

if [ "$VERIFY_FAILED" = 1 ]; then
  echo "" >&2
  fail "最終検証で NG があります（上記の検証結果を参照）。"
  exit 1
fi
