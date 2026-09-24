#!/usr/bin/env bash
# 完全オフライン（閉域）配布キットの収集スクリプト（オンライン環境で実行）。
# マニュアル: docs/manual/offline-kit.md（章立て・搬入/検証手順はそちらを参照）。
#
# 収集する資材:
#   1. アプリ本体 dist（make dist・ネットワーク不要）
#   2. Python 依存 wheel 一式（pip download）
#   3. Python 実行系 .deb（python3 / python3-venv / python3-pip。素の閉域ホストに python3-venv が
#      無いことが多く、venv 作成が最初に転ぶため同梱する）
#   4. Docker イメージ（postgres / neo4j / elasticsearch+kuromoji を docker save）
#   5. Docker Engine 本体 .deb（docker-ce 一式・download.docker.com のリポジトリ鍵も来歴として保存）
#   6. Ollama モデルデータ（既定は案内表示のみ・サイズが大きいため個別オプトイン。
#      ollama 本体バイナリは含まない＝閉域側ホストへ別途導入すること）
#   7. 日本語フォント（Noto Sans CJK JP・HackGen。SIL OFL 1.1・バンドル/再配布可でライセンス確認済み）
#   8. Node.js 本体 + marp-cli（marp スキルの実行に必須。Node は公式 tarball を sha256 検証込みで収集）
#   9. Playwright Chromium（marp の PDF/PPTX 出力に必須。本体 tar に加え、起動に要るシステム共有
#      ライブラリ .deb も収集する＝同梱しないと最小構成ホストで起動できない）
#  10. LibreOffice（旧形式 Office 変換バックエンドの選択肢の一つ・sherpa/ingest/arms/legacy_convert.py）
#  11. OCR（画像内文字の読み取り・既定ON）＝隔離ワーカーのイメージ＋専用 wheel＋モデル。
#  14. Codex CLI（npm 導入済みツリー・linux-x64 静的バイナリ込み。--skip-codex で除外）
#
# apt 系収集（3・5・7・9のdeps・10）は既定で「素の対象OSコンテナ内」で行う（$APT_BASE_IMAGE）。
# `apt-get install --download-only` は収集マシンの導入済み状態を基準に依存解決するため、収集マシンに
# 既に入っているパッケージの .deb は集まらない＝まっさらな閉域ホストで依存不足になりうる。素のコンテナで
# 行えばこの問題を回避でき、sudo も不要（docker 権限のみ）。Docker が無い収集マシンではフォールバックとして
# この機体へ直接 --download-only する（sudo 使用・「収集マシンに既に入っている分は集まらない」旨を警告表示）。
#
# 冪等: 再実行安全。実際に収集するステップ（1は常時／2・3・4・5・7〜13は --fetch／6は --with-ollama）は、
# 12=土台の閉包（base/debs・--skip-base で除外）、13=repo 索引（Packages/Release）＋BASELINE（2026-08-17）。
# 書き込み先ディレクトリを毎回空にしてから作り直すため、古いバージョンの成果物が混入しません。
# 既定では 1 のみ実行し、それ以外は「実行計画」を表示するだけで実行しません（回線・ディスクを大きく使うため）。
# 実際に集めるには --fetch、さらに --with-ollama <model>（6）を指定してください。
#
# 使い方:
#   ./scripts/make_offline_kit.sh                    # 計画表示のみ（1だけ実行）
#   ./scripts/make_offline_kit.sh --fetch             # 1〜5, 7〜11 を実行
#   ./scripts/make_offline_kit.sh --fetch --with-ollama gpt-oss:20b   # 上記 + 6
#   ./scripts/make_offline_kit.sh --fetch --skip-docker  # Docker イメージ収集のみ省略
#   BASE_CLOSURE_PACKAGES="ubuntu-minimal ubuntu-standard ubuntu-server"  # 12 の追加閉包（環境変数で上書き可）
#   SHERPA_WHEELS_IN_CONTAINER=0 PYTHON_BIN=python3.12 ./scripts/make_offline_kit.sh --fetch  # 端末の Python で wheel 収集
#   ./scripts/make_offline_kit.sh --fetch --skip-fonts --skip-node --skip-chromium \
#       --skip-libreoffice --skip-docker-engine --skip-python-system --skip-ocr
#                                                      # marp/LibreOffice/Docker Engine 等を除外（サイズ削減）
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"
# docker compose の呼び出し（OCR イメージの build）を sherpa_compose に揃えるため（.env の受け渡しを一本化）。
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"

OUT="$ROOT/dist/offline-kit"
NODE_VERSION="22.22.3"   # 08-実行権限と隔離.md / 2026-07-07-Marpスライド作成.md の実績ピン留め版
# apt 収集に使う素のコンテナのベースイメージ。閉域側ホストの OS と合わせること
# （パッケージ名・依存関係が Ubuntu バージョンで変わりうる。既定は現行の収集/検証実績に合わせて 24.04）。
APT_BASE_IMAGE="${APT_BASE_IMAGE:-ubuntu:24.04}"
FETCH=0
SKIP_DOCKER=0
SKIP_DOCKER_ENGINE=0
SKIP_PYTHON_SYSTEM=0
SKIP_FONTS=0
SKIP_NODE=0
SKIP_CHROMIUM=0
SKIP_LIBREOFFICE=0
SKIP_OCR=0
SKIP_BASE=0
SKIP_CODEX=0
WITH_OLLAMA=""

usage() {
  cat <<'EOF'
使い方: scripts/make_offline_kit.sh [オプション]

オプション:
  --fetch                pip download・docker pull/build/save・フォント/Node/Chromium/LibreOffice/
                          Docker Engine/Python実行系 の取得を実際に実行する（既定は実行計画の表示のみ）
  --with-ollama <model>   指定した Ollama モデルを pull し、~/.ollama を収集資材へ
                          コピーする（既定は案内表示のみ。--fetch と併用可否は問わない）
  --skip-docker           Docker イメージ（postgres/neo4j/es）の収集をスキップする
  --skip-docker-engine    Docker Engine 本体 .deb の収集をスキップする
  --skip-python-system    python3/python3-venv/python3-pip の .deb 収集をスキップする
  --skip-fonts            フォント（Noto Sans CJK JP・HackGen）の収集をスキップする
  --skip-node             Node.js 本体・marp-cli の収集をスキップする
  --skip-chromium         Playwright Chromium（本体＋システム依存）の収集をスキップする
  --skip-libreoffice      LibreOffice の収集をスキップする
  --skip-ocr              OCR（画像内文字の読み取り）資材の収集をスキップする
  --skip-base             土台の閉包（素のコンテナ導入済みパッケージの .deb・base/debs）をスキップする
  --skip-codex            Codex CLI（npm の導入済みツリー）の収集をスキップする
  -h, --help              このヘルプを表示する

出力先: dist/offline-kit/
詳しい手順・搬入方法・閉域側セットアップは docs/manual/offline-kit.md を参照してください。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --fetch) FETCH=1; shift ;;
    --with-ollama)
      [ $# -ge 2 ] || { echo "エラー: --with-ollama にはモデル名が必要です（例: --with-ollama gpt-oss:20b）" >&2; exit 2; }
      case "$2" in
        ""|-*)
          echo "エラー: --with-ollama に渡すモデル名が不正です（空、または '-' で始まっています）: '$2'" >&2
          echo "  例: --with-ollama gpt-oss:20b" >&2
          exit 2 ;;
      esac
      WITH_OLLAMA="$2"; shift 2 ;;
    --skip-docker) SKIP_DOCKER=1; shift ;;
    --skip-docker-engine) SKIP_DOCKER_ENGINE=1; shift ;;
    --skip-python-system) SKIP_PYTHON_SYSTEM=1; shift ;;
    --skip-fonts) SKIP_FONTS=1; shift ;;
    --skip-node) SKIP_NODE=1; shift ;;
    --skip-chromium) SKIP_CHROMIUM=1; shift ;;
    --skip-libreoffice) SKIP_LIBREOFFICE=1; shift ;;
    --skip-ocr) SKIP_OCR=1; shift ;;
    --skip-base) SKIP_BASE=1; shift ;;
    --skip-codex) SKIP_CODEX=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "エラー: 不明なオプションです: $1" >&2; usage >&2; exit 2 ;;
  esac
done

note()  { echo "・ $*"; }
ok()    { echo "OK: $*"; }
warn()  { echo "ⓘ  $*" >&2; }
fail()  { echo "✗ $*" >&2; }
reset_dir() { rm -rf "$1"; mkdir -p "$1"; }  # 冪等: 再生成前に空にし、古い成果物の混入を防ぐ

# ---------------------------------------------------------------------------
# H1: apt 系収集の共通ヘルパ。素の $APT_BASE_IMAGE コンテナ内で
# `apt-get install --download-only` を実行し、依存を含む .deb 一式を集める。
# 収集マシンの導入済みパッケージ状態に依存しない完全なクロージャが得られる（sudo 不要）。
# ---------------------------------------------------------------------------
_apt_collect_debs() {
  # $1 = 収集するパッケージ名（space区切り）
  # $2 = .deb を置く最終ディレクトリ（例: $OUT/libreoffice/debs）。親ディレクトリを /out として
  #      コンテナへマウントするので、$3 の追加セットアップから他の来歴ファイル
  #      （例: docker.asc）を /out/<ファイル名> として同じ親へ書ける。
  # $3 = 追加セットアップ（コンテナ内 apt-get update の後、install --download-only の前に実行）
  local packages="$1" dest_dir="$2" extra_setup="${3:-}"
  local base_dir leaf
  base_dir="$(dirname "$dest_dir")"
  leaf="$(basename "$dest_dir")"
  mkdir -p "$base_dir"
  reset_dir "$dest_dir"
  if ! command -v docker >/dev/null 2>&1; then
    warn "docker が見つかりません。素の対象OSコンテナ方式が使えないため、この機体へ直接"
    warn "  apt-get install --download-only でフォールバック収集します（sudo 使用）。"
    warn "  収集マシンに既に入っているパッケージ分の .deb は集まりません＝完全性は保証されません。"
    sudo apt-get update
    _apt_collect_debs_fallback "$packages" "$dest_dir" || return $?
    _apt_write_packages "$packages" "$dest_dir"
    return 0
  fi
  note "docker run --rm $APT_BASE_IMAGE（素の対象OSコンテナ）で [$packages] を download-only 収集します..."
  # R3: コンテナ内は root 実行のため、素のまま書き出すと bind mount 先（ホスト側）の .deb が
  # root 所有になり、再実行時に一般ユーザー権限の reset_dir()（rm -rf）が失敗して --fetch の
  # 冪等性が壊れる。コンテナ内で呼び出し側ユーザーの uid:gid へ chown してから抜ける。
  docker run --rm -v "$base_dir:/out" "$APT_BASE_IMAGE" bash -c "
    set -e
    apt-get update
    $extra_setup
    apt-get install --download-only -y $packages
    mkdir -p '/out/$leaf'
    cp /var/cache/apt/archives/*.deb '/out/$leaf/'
    chown -R $(id -u):$(id -g) /out
  " || return $?
  # 2026-08-17 RV HIGH: docker run の後に別コマンドを置いたことで、`if _apt_collect_debs …; then` の中では
  # set -e が効かず docker の失敗（.deb ゼロ）が握り潰され「OK」と出て収集が続行していた。失敗は必ず
  # 呼び出し元へ返し、PACKAGES は成功したときだけ書く。
  _apt_write_packages "$packages" "$dest_dir"
}

# 2026-08-17: 導入側（scripts/lib/apt_offline.sh）が「トップレベル名だけを apt に解かせる」ため、
# 各グループの要求パッケージ名を group_dir/PACKAGES に残す（./*.deb 全列挙＝版の強制をやめる根拠）。
_apt_write_packages() {  # $1=名前（空白区切り） $2=dest_dir
  printf '%s\n' "$1" > "$2/PACKAGES"
}

# 2026-08-17: 「土台の閉包」。素のコンテナに**導入済み**のパッケージ（libc6/perl-base/zlib1g/gcc-14-base/
# dpkg…）は download-only の閉包から**外れる**（apt は導入済みを集めない）。ところが ubuntu:24.04 の
# タグはイメージ再ビルドで土台が noble-updates 版に更新されており、インストール直後の閉域ホスト
# （リリース当日版）より新しいことがある。閉包内の .deb は「土台＝更新後版」への厳密依存
# （Depends: libc6 (= 2.39-0ubuntu8.8) 等）を持つため、閉域側では unmet で全停止（実測 2026-08-17）。
# 土台の .deb も集めて索引に載せれば apt が土台ごと解決できる（削除ゼロ・実測済み）。
#
# 2026-08-18（閉域実機レポート①）: 最小イメージ（ubuntu:24.04）の導入済み集合だけでは足りない。実機の
# Ubuntu Server には ubuntu-server/ubuntu-standard 系メタが要求する群（libglib2.0-bin / packagekit /
# software-properties-common …）が導入済みで、キット側の .deb（例: Chromium 依存の libglib2.0-0t64 新版）が
# それらの**上げ先**を要求する。上げ先が索引に無いと apt は「その群を削除して ubuntu-server も削除」で辻褄を
# 合わせようとし、--no-remove で fail-close（実機は無傷だがその群は入らない）。よって最小イメージの閉包に
# 加えて $BASE_CLOSURE_PACKAGES（既定: ubuntu-minimal ubuntu-standard ubuntu-server）を --download-only で
# **追加収集**し、実機の導入済み集合の上げ先も base/debs に含める（導入はしない＝コンテナは肥大しない。
# 実機での手当て BASE_CLOSURE_PACKAGES 導入で 92→583 個・解消を確認）。
#
# 2026-08-18 RV MED-3: $BASE_CLOSURE_PACKAGES は下の _apt_collect_base_closure 内で docker の
# `bash -c "…"` 文字列へそのまま埋め込まれる（外側 bash が展開してからコンテナへ渡す）ため、シェルの
# メタ文字（; | & $() ` 等）を含む値を渡されるとコンテナ内で任意コマンドが実行され得る。パッケージ名
# として許される文字（英小文字・数字・+/-/. のみ）だけを受け付け、それ以外は事前に拒否する。
_validate_base_closure_packages() {  # $1=BASE_CLOSURE_PACKAGES の値。パッケージ名以外の文字を拒否
  local val="$1" tok
  [ -z "$val" ] && return 0   # 空は「追加閉包なし＝最小イメージ分だけ」という妥当な指定
  local -a toks
  # shellcheck disable=SC2206
  read -ra toks <<<"$val"     # glob 展開されない word 分割（for tok in $val は * 等でグロブしうるため使わない）
  for tok in "${toks[@]}"; do
    if ! printf '%s' "$tok" | grep -qE '^[a-z0-9][a-z0-9+.-]*$'; then
      fail "BASE_CLOSURE_PACKAGES に不正な値があります（Debian パッケージ名として許可されない文字）: '$tok'"
      fail "  許可される文字: 半角英小文字・数字・'+'・'-'・'.'（先頭は英小文字か数字）。空白区切りで複数指定可。"
      fail "  docker の bash -c 文字列へそのまま埋め込むため、シェルのメタ文字は事前に拒否しています。"
      return 1
    fi
  done
  return 0
}
BASE_CLOSURE_PACKAGES="${BASE_CLOSURE_PACKAGES:-ubuntu-minimal ubuntu-standard ubuntu-server}"
_validate_base_closure_packages "$BASE_CLOSURE_PACKAGES" || exit 1
# 2026-08-18（Codex RV 2巡目 指摘5）: 従来は土台の閉包（base/debs）が docker 必須のまま致命化されており
# （RV1是正で「失敗しても warn だけで続行」→「収集失敗はキット生成を止める」へ変更した際、docker が無い
# 機体は毎回この失敗経路を踏むことになった）、docs/manual/offline-kit.md が案内している「docker の無い
# 収集機」経路が壊れていた（他の apt 系グループには `_apt_collect_debs_fallback` があるのに、土台の閉包
# だけ逃げ道が無い）。`--skip-base` で回避させると、この是正の発端だった実機不具合（閉域実機報告①＝
# 土台の閉包の欠落）を再導入するので逃げ道にしない。`_apt_collect_debs_fallback` と同じ作法（sudo・
# `Dir::Cache::Archives` を一時ディレクトリへ差し替え）で、**収集機自身が対象 OS（$APT_BASE_IMAGE と
# 同じ Ubuntu 24.04 x86_64）である**という前提の下、収集機の導入済み集合＋$BASE_CLOSURE_PACKAGES の
# 閉包を --download-only で集める（素の対象OSコンテナのような分離はできない＝収集機の OS が閉域ホストと
# 違うと正しい閉包にならない。docker のある機体での収集を引き続き推奨する・呼び出し側で警告表示する）。
_apt_collect_base_closure_fallback() {  # $1=dest_dir（$OUT/base/debs）
  local dest_dir="$1" cache_dir
  cache_dir="$(mktemp -d)"
  mkdir -p "$cache_dir/archives/partial" "$dest_dir"
  sudo apt-get update
  # shellcheck disable=SC2046
  if ! sudo apt-get -o "Dir::Cache::Archives=$cache_dir/archives" install --download-only --reinstall -y \
      $(dpkg-query -W -f='${binary:Package}\n'); then
    rm -rf "$cache_dir"
    return 1
  fi
  if [ -n "$BASE_CLOSURE_PACKAGES" ]; then
    # shellcheck disable=SC2086
    if ! sudo apt-get -o "Dir::Cache::Archives=$cache_dir/archives" install --download-only -y $BASE_CLOSURE_PACKAGES; then
      rm -rf "$cache_dir"
      return 1
    fi
  fi
  find "$cache_dir/archives" -maxdepth 1 -name '*.deb' -exec cp -f {} "$dest_dir/" \;
  rm -rf "$cache_dir"
  return 0
}
_apt_collect_base_closure() {  # $1=dest_dir（$OUT/base/debs）
  local dest_dir="$1" base_dir leaf
  base_dir="$(dirname "$dest_dir")"; leaf="$(basename "$dest_dir")"
  mkdir -p "$base_dir"; reset_dir "$dest_dir"
  if ! command -v docker >/dev/null 2>&1; then
    warn "docker が見つかりません。この機体（$APT_BASE_IMAGE と同じ OS である前提）へ直接 apt-get install"
    warn "  --download-only --reinstall でフォールバック収集します（sudo 使用）。収集機の OS が閉域ホストと"
    warn "  異なる場合は正しい閉包になりません＝docker のある機体での収集を推奨します。"
    if _apt_collect_base_closure_fallback "$dest_dir"; then
      _apt_write_packages "" "$dest_dir"   # 名前指定で入れるものではない（索引の候補として置くだけ）
      printf '%s\n' "$BASE_CLOSURE_PACKAGES" > "$dest_dir/BASE_CLOSURE_PACKAGES"   # 何を上げ先として集めたかの来歴
      return 0
    fi
    return 1
  fi
  note "docker run --rm $APT_BASE_IMAGE で導入済みパッケージ全部を --reinstall --download-only 収集します（土台の閉包）..."
  note "  さらに [$BASE_CLOSURE_PACKAGES] の閉包も --download-only で追加収集します（実機 Ubuntu Server の導入済み集合の上げ先）。"
  docker run --rm -v "$base_dir:/out" "$APT_BASE_IMAGE" bash -c "
    set -e
    apt-get update
    apt-get clean
    apt-get install --download-only --reinstall -y \$(dpkg-query -W -f='\${binary:Package}\n')
    # RV: 'ls *.deb | wc -l' はマッチ0件だと ls が stderr にエラーを吐く（pipefail 環境へ持ち出すと
    # set -e で落ちる／持ち出さなくても無駄な stderr ノイズが出る）。find なら 0件でも黙って 0 を返す。
    n_min=\$(find /var/cache/apt/archives -maxdepth 1 -name '*.deb' | wc -l)
    # 追加分（download-only なので導入はしない）。空なら最小イメージの閉包だけで終える。
    if [ -n \"$BASE_CLOSURE_PACKAGES\" ]; then
      apt-get install --download-only -y $BASE_CLOSURE_PACKAGES
    fi
    n_all=\$(find /var/cache/apt/archives -maxdepth 1 -name '*.deb' | wc -l)
    echo \"土台の閉包: 最小イメージ分 \$n_min 個 + [$BASE_CLOSURE_PACKAGES] 分 \$((n_all - n_min)) 個 = 合計 \$n_all 個\"
    mkdir -p '/out/$leaf'
    cp /var/cache/apt/archives/*.deb '/out/$leaf/'
    chown -R $(id -u):$(id -g) /out
  " || return $?
  _apt_write_packages "" "$dest_dir"   # 名前指定で入れるものではない（索引の候補として置くだけ）
  printf '%s\n' "$BASE_CLOSURE_PACKAGES" > "$dest_dir/BASE_CLOSURE_PACKAGES"   # 何を上げ先として集めたかの来歴
}

# 2026-08-17: $OUT 直下にフラット repo の索引（Packages/Packages.gz/Release）を置く。導入側はこれを
# `deb [trusted=yes] file:<kit_root> ./` として読み、Filename（./<group>/debs/x.deb 相対）で .deb を解決する。
# apt-ftparchive は apt-utils（ubuntu:24.04 イメージには無い）＝素のコンテナで入れて実行する。
# Release は同じディレクトリへ直接書くと自分自身が対象に入るため一旦別名→mv。Packages を触ったら Release
# も作り直す（Hash Sum mismatch で update が止まる）。
_apt_build_repo_index() {  # $1=$OUT
  local out="$1"
  if ! command -v docker >/dev/null 2>&1; then
    if command -v apt-ftparchive >/dev/null 2>&1; then
      ( cd "$out" && apt-ftparchive packages . > Packages && gzip -k -f Packages \
        && apt-ftparchive -o APT::FTPArchive::Release::Origin=sherpa-offline -o APT::FTPArchive::Release::Suite=local \
             release . > .Release.tmp && mv .Release.tmp Release )
      return $?
    fi
    warn "docker も apt-ftparchive も無いため repo 索引を作れません（導入側は ./*.deb 列挙にフォールバックします）。"
    return 1
  fi
  docker run --rm -v "$out:/out" "$APT_BASE_IMAGE" bash -c "
    set -e
    apt-get update
    apt-get install -y --no-install-recommends apt-utils
    cd /out
    rm -f Packages Packages.gz Release
    apt-ftparchive packages . > Packages
    gzip -k -f Packages
    apt-ftparchive -o APT::FTPArchive::Release::Origin=sherpa-offline -o APT::FTPArchive::Release::Suite=local \
      release . > /tmp/Release && mv /tmp/Release /out/Release
    chown $(id -u):$(id -g) /out/Packages /out/Packages.gz /out/Release
  " || return $?
  _apt_warn_mixed_generations "$out/Packages"
}

# 一部グループだけ再収集すると（--skip-* 併用）、グループ間で世代の違う .deb が同居した索引ができ、
# 厳密依存（libc6-dev (= X) と libc6 の版違い等）が閉域側で unmet になり得る。同一 Package が複数 Version で
# 索引に載っていたら警告する（再収集は全グループ揃えて行うのが正）。
_apt_warn_mixed_generations() {  # $1=Packages
  local dup
  dup="$(awk '/^Package: /{p=$2} /^Version: /{print p" "$2}' "$1" 2>/dev/null | sort -u | awk '{print $1}' | uniq -d | head -20)"
  if [ -n "$dup" ]; then
    warn "repo 索引に同じパッケージが複数の版で載っています（一部グループだけ再収集した世代混在の疑い）:"
    while IFS= read -r n; do warn "  - $n"; done <<<"$dup"
    warn "  閉域側で厳密依存が unmet になり得ます。--skip-* を外して全グループを同時に再収集してください。"
  fi
}

# 2026-08-17: BASELINE（導入側 apt_offline_check_baseline と対）。素のコンテナ内で os-release/arch を取り、
# ホスト側で digest/Created を足す（タグ文字列だけでは同一タグ内ドリフトが追えない）。
_apt_write_baseline() {  # $1=$OUT
  local out="$1" digest="" created="" pyver=""
  pyver="$(ls "$out"/python/debs/python3_*.deb 2>/dev/null | head -1 | sed -E 's/.*python3_([^_]+)_.*/\1/' || true)"
  if command -v docker >/dev/null 2>&1; then
    digest="$(docker image inspect --format '{{if .RepoDigests}}{{index .RepoDigests 0}}{{end}}' "$APT_BASE_IMAGE" 2>/dev/null || true)"
    created="$(docker image inspect --format '{{.Created}}' "$APT_BASE_IMAGE" 2>/dev/null || true)"
    docker run --rm -v "$out:/out" -v "$ROOT/scripts/lib/apt_offline.sh:/apt_offline.sh:ro" "$APT_BASE_IMAGE" bash -c "
      set -e
      bash /apt_offline.sh write-baseline /out/BASELINE '$APT_BASE_IMAGE' '$digest' '$created' '$pyver'
      chown $(id -u):$(id -g) /out/BASELINE
    "
  else
    # shellcheck source=scripts/lib/apt_offline.sh
    . "$ROOT/scripts/lib/apt_offline.sh"
    apt_offline_write_baseline "$out/BASELINE" "(host)" "" "" "$pyver"
  fi
}

# 2026-08-18（閉域実機レポート②）: wheel は収集時の Python の major.minor に紐づく（ABI/タグ）。収集側の
# Python 版を「記録するだけ」だと、3.11 の収集機で作ったキットが黙って完成し閉域で初めて壊れる。対象 OS の
# python3（素のコンテナの候補版＝BASELINE の PYTHON_DEB_VERSION、無ければ apt-cache policy）と major.minor を
# 突き合わせ、不一致なら収集を止める（SHERPA_ALLOW_PY_MISMATCH=1 で警告のみ）。
_target_python_mm() {  # 対象 OS の python3 の major.minor を stdout に（分からなければ空）
  local ver=""
  # 2026-08-18 RV LOW-4: 「BASELINE の IMAGE= タグ文字列が一致すれば流用」だと、ローカルの
  # $APT_BASE_IMAGE タグが（同じタグ名のまま）差し替わっていたり BASELINE が古いまま残っていても
  # ABI 照合が黙って通ってしまう（false pass・タグは digest を保証しない）。docker が使えるなら
  # BASELINE には頼らず、毎回対象イメージへ直接問い合わせて候補版を読み直す。
  #
  # 2026-08-18（Codex RV 2巡目 指摘4）: 以前は `command -v docker` の**有無**だけで docker/apt-cache/
  # BASELINE のどれか1つに分岐（if/elif/elif）しており、「docker コマンドはあるが daemon が停止／
  # イメージが pull できない」場合に `docker run` 自体が失敗して `ver` が空になっても、その1分岐で
  # 確定してしまい apt-cache/BASELINE へフォールスルーしなかった（＝докker が「使えそうに見える」だけで
  # 実際には問い合わせできない機体で、対象版が特定できないまま次段の照合が黙ってスキップされていた）。
  # ここは「使えるコマンドがあるか」ではなく「実際に版が取れたか」で順に試す（各手段の結果が空なら
  # 次の手段を試す）。
  if command -v docker >/dev/null 2>&1; then
    ver="$(docker run --rm "$APT_BASE_IMAGE" bash -c "apt-get update -qq >/dev/null 2>&1; apt-cache policy python3 | sed -n 's/^  Candidate: //p'" 2>/dev/null | head -1 || true)"
  fi
  if [ -z "$ver" ] && command -v apt-cache >/dev/null 2>&1; then
    ver="$(apt-cache policy python3 2>/dev/null | sed -n 's/^  Candidate: //p' | head -1 || true)"
  fi
  if [ -z "$ver" ] && [ -f "$OUT/BASELINE" ] && [ "$(sed -n 's/^IMAGE=//p' "$OUT/BASELINE" | head -1)" = "$APT_BASE_IMAGE" ]; then
    # docker も apt-cache も版を取れなかった機体でのみ、最後の手段として前回収集時点の記録を借用する
    # （タグ文字列の一致だけを見ており digest までは照合していない＝この経路だけは弱い照合であることを警告する）。
    warn "docker/apt-cache のどちらからも対象 OS の python3 版を取得できなかったため、前回の BASELINE（タグ文字列一致のみ）から借用します。"
    ver="$(sed -n 's/^PYTHON_DEB_VERSION=//p' "$OUT/BASELINE" | head -1)"
  fi
  # 3.12.3-0ubuntu2.1 → 3.12
  printf '%s\n' "$ver" | sed -nE 's/^([0-9]+\.[0-9]+).*/\1/p' | head -1
}
_check_python_version_match() {  # $1=収集に使う python の major.minor
  local host_mm="$1" target_mm
  target_mm="$(_target_python_mm)"
  if [ -z "$target_mm" ]; then
    # 2026-08-18（Codex RV 2巡目 指摘4）: 以前はここで warn だけして続行していた＝fail-open。
    # docker はあるが daemon 停止／pull 不可のときも同じ経路に落ちるため、「版照合という安全網が
    # 無言で外れたまま ABI 不一致キットが完成する」実害になっていた。どうしても対象 python3 の版を
    # 特定できないなら収集そのものを止める（既存の SHERPA_ALLOW_PY_MISMATCH=1 を明示 escape hatch
    # として、そのときだけ警告で続行する＝不一致が確認できた場合と同じ緩和経路に揃える）。
    if [ "${SHERPA_ALLOW_PY_MISMATCH:-0}" = 1 ]; then
      warn "対象 OS（$APT_BASE_IMAGE）の python3 版を特定できませんが SHERPA_ALLOW_PY_MISMATCH=1 のため照合をスキップして続行します（wheel の ABI 不一致で閉域で壊れ得ます）。"
      return 0
    fi
    fail "対象 OS（$APT_BASE_IMAGE）の python3 版を特定できませんでした（docker 不通・apt-cache 無し・BASELINE 未整備のいずれか）。"
    fail "  収集側 Python（$host_mm）が閉域側の python3 と ABI 一致するか確認できないため、収集を中止します。"
    fail "  対処: docker を復旧する（daemon 起動・レジストリへ到達可能にする）か、apt-cache が使える機体で"
    fail "        実行するか、承知の上なら SHERPA_ALLOW_PY_MISMATCH=1 を付けてください。"
    return 1
  fi
  if [ "$host_mm" = "$target_mm" ]; then
    ok "Python 版の照合: 収集側 $host_mm ＝ 対象 OS の python3 $target_mm"
    return 0
  fi
  if [ "${SHERPA_ALLOW_PY_MISMATCH:-0}" = 1 ]; then
    warn "収集側 Python $host_mm と対象 OS の python3 $target_mm が不一致ですが SHERPA_ALLOW_PY_MISMATCH=1 のため続行します（wheel の ABI 不一致で閉域で壊れ得ます）。"
    return 0
  fi
  fail "収集側 Python（$host_mm）と対象 OS（$APT_BASE_IMAGE）の python3（$target_mm）の major.minor が一致しません。"
  fail "  このまま集めた wheel は閉域側の python3 では入りません（ABI 不一致）。収集を中止します。"
  fail "  対処: docker のある機体で実行する（既定で素のコンテナ内の python3 で pip download する）か、"
  fail "        PYTHON_BIN=python$target_mm を指定するか、承知の上なら SHERPA_ALLOW_PY_MISMATCH=1 を付けてください。"
  return 1
}

# wheel の収集。docker があれば既定で「素の対象 OS コンテナ内の python3」で pip download する
# （端末の Python 版に依存しない＝閉域側の python3 と必ず同じ major.minor になる）。SHERPA_WHEELS_IN_CONTAINER=0
# で端末の $PY を使う旧経路（このときは上の版照合が必須）。
# $1=requirements ファイル（$ROOT からの相対） $2=constraints（同・空なら無し） $3=出力先ディレクトリ
_pip_download_wheels() {
  local req="$1" cons="${2:-}" dest="$3" cons_opt=()
  mkdir -p "$dest"
  if [ "$WHEELS_IN_CONTAINER" = 1 ]; then
    [ -n "$cons" ] && cons_opt=(-c "/src/$cons")
    note "docker run --rm $APT_BASE_IMAGE（素の対象OSコンテナ・python3-pip 導入）で pip download -r $req ${cons:+-c $cons} → $dest"
    docker run --rm -v "$ROOT:/src:ro" -v "$dest:/out" "$APT_BASE_IMAGE" bash -c "
      set -e
      export DEBIAN_FRONTEND=noninteractive
      apt-get update -qq >/dev/null
      apt-get install -y -qq python3 python3-pip >/dev/null
      python3 --version > /out/COLLECTED-WITH-PYTHON-VERSION.txt 2>&1
      python3 -m pip download --disable-pip-version-check -r '/src/$req' ${cons_opt[*]} -d /out
      chown -R $(id -u):$(id -g) /out
    " || return $?
  else
    [ -n "$cons" ] && cons_opt=(-c "$ROOT/$cons")
    "$PY" --version > "$dest/COLLECTED-WITH-PYTHON-VERSION.txt" 2>&1 || true
    "$PY" -m pip download -r "$ROOT/$req" "${cons_opt[@]}" -d "$dest" || return $?
  fi
}

# Docker が使えない収集マシン向けのフォールバック（この機体へ直接収集・sudo 使用）。
# システムの /var/cache/apt/archives を汚さないよう Dir::Cache::Archives を一時ディレクトリへ差し替える。
_apt_collect_debs_fallback() {
  local packages="$1" dest_dir="$2" cache_dir
  cache_dir="$(mktemp -d)"
  mkdir -p "$cache_dir/archives/partial" "$dest_dir"
  if sudo apt-get -o "Dir::Cache::Archives=$cache_dir/archives" install --download-only -y $packages; then
    find "$cache_dir/archives" -maxdepth 1 -name '*.deb' -exec cp -f {} "$dest_dir/" \;
    rm -rf "$cache_dir"
    return 0
  fi
  rm -rf "$cache_dir"
  return 1
}

mkdir -p "$OUT/app" "$OUT/wheels" "$OUT/python" "$OUT/docker-images" "$OUT/docker-engine" "$OUT/ollama" \
  "$OUT/fonts" "$OUT/node" "$OUT/marp" "$OUT/chromium" "$OUT/libreoffice" "$OUT/ocr" "$OUT/base"

echo "=== 完全オフライン配布キットの収集 ==="
echo "出力先: $OUT"
echo "apt 収集ベースイメージ: $APT_BASE_IMAGE"
echo ""

# ---------------------------------------------------------------------------
# 1. アプリ本体 dist（ネットワーク不要・常に実行）
# ---------------------------------------------------------------------------
echo "--- 1. アプリ本体 (make dist) ---"
if ! command -v git >/dev/null 2>&1; then
  fail "git が見つかりません。make dist には git が必要です。"
  exit 1
fi
make dist
# バージョン名は Makefile と同じ規則（タグがあれば git describe、無ければ VERSION ファイル）。
SHERPA_VERSION="$(git describe --tags --exact-match 2>/dev/null || printf 'v%s' "$(cat VERSION 2>/dev/null || echo 0.0.0)")"
TARBALL="dist/sherpa-$SHERPA_VERSION.tar.gz"
if [ -f "$TARBALL" ]; then
  reset_dir "$OUT/app"
  cp -f "$TARBALL" "$TARBALL.sha256" "$OUT/app/"
  ok "アプリ本体: $OUT/app/$(basename "$TARBALL")"
else
  fail "make dist の成果物が見つかりません: $TARBALL"
  exit 1
fi
echo ""

# ---------------------------------------------------------------------------
# 2. Python 依存 wheel 一式
# ---------------------------------------------------------------------------
echo "--- 2. Python 依存（pip download） ---"
PY="${PYTHON_BIN:-python3}"
# 既定: docker があれば素の対象 OS コンテナ内で pip download（端末の Python 版に依存しない）。
if [ -z "${SHERPA_WHEELS_IN_CONTAINER:-}" ]; then
  if command -v docker >/dev/null 2>&1; then WHEELS_IN_CONTAINER=1; else WHEELS_IN_CONTAINER=0; fi
else
  WHEELS_IN_CONTAINER="$SHERPA_WHEELS_IN_CONTAINER"
fi
if [ "$FETCH" = 1 ]; then
  if [ "$WHEELS_IN_CONTAINER" = 1 ]; then
    note "wheel は $APT_BASE_IMAGE コンテナ内の python3 で収集します（SHERPA_WHEELS_IN_CONTAINER=0 で端末の $PY を使う）。"
  else
    if ! command -v "$PY" >/dev/null 2>&1; then
      fail "$PY が見つかりません。Python3 を導入してください。"
      exit 1
    fi
    # 端末の Python で集めるときは、対象 OS の python3 と major.minor が一致することを必須にする（②）。
    HOST_PY_MM="$("$PY" -c 'import sys; print("%d.%d" % sys.version_info[:2])')"
    _check_python_version_match "$HOST_PY_MM" || exit 1
  fi
  reset_dir "$OUT/wheels"
  if _pip_download_wheels requirements.txt constraints.txt "$OUT/wheels"; then
    note "収集に使った Python バージョンを $OUT/wheels/COLLECTED-WITH-PYTHON-VERSION.txt に記録しました（$(cat "$OUT/wheels/COLLECTED-WITH-PYTHON-VERSION.txt" 2>/dev/null || true)）。"
    ok "wheel 一式: $OUT/wheels/"
  else
    fail "pip download に失敗しました。ネットワーク接続、requirements.txt / constraints.txt の内容を確認してください。"
    exit 1
  fi
  # R6a（2026-07-13-横断レビュー対応.md）: --find-links は同名でも版の高い野良 wheel を優先採用しうるため、
  # 収集時点の wheels/ 内容の SHA256 manifest を残し、install 側（install_offline_kit.sh）で検証できるようにする。
  # 対象は SHA256SUMS 自身を除く**全ファイル**（RV HIGH 2026-07-15: pip は .whl/.tar.gz 以外にも
  # .zip/.tgz/.tar.bz2 等を install 候補にする＝拡張子の列挙だと漏れが構造的に残るため、列挙をやめて
  # 全ファイルを対象化する。COLLECTED-WITH-PYTHON-VERSION.txt も含まれるが実害はない）。
  # find -maxdepth 1 ベース（glob の 0 件ヒットで落ちない・set -euo pipefail 下で安全）。
  note "wheels の SHA256 manifest を生成します（$OUT/wheels/SHA256SUMS）..."
  if [ -z "$(cd "$OUT/wheels" && find . -maxdepth 1 -name '*.whl' -print -quit)" ]; then
    fail "$OUT/wheels/ に *.whl が1つもありません（pip download 成功後にこれは異常です）。"
    exit 1
  fi
  # RV HIGH（2026-07-15 2巡目）: 通常ファイル以外（シンボリックリンク・ディレクトリ等）は manifest に
  # 載せられず install 側の未掲載検出で必ず fail になるため、収集時点で拒否する（pip download は
  # 通常ファイルしか書かない＝これが出たら収集環境の異常）。
  NONREG_ENTRIES="$(cd "$OUT/wheels" && find . -mindepth 1 -maxdepth 1 ! -type f -printf '%f\n' | sort)"
  if [ -n "$NONREG_ENTRIES" ]; then
    fail "$OUT/wheels/ に通常ファイル以外のエントリがあります（収集環境の異常）: $NONREG_ENTRIES"
    exit 1
  fi
  WHEEL_FILES="$(cd "$OUT/wheels" && find . -maxdepth 1 -type f ! -name 'SHA256SUMS' -printf '%f\n' | sort)"
  (cd "$OUT/wheels" && printf '%s\n' "$WHEEL_FILES" | xargs -d '\n' sha256sum > SHA256SUMS)
  ok "SHA256 manifest: $OUT/wheels/SHA256SUMS"
else
  if [ "$WHEELS_IN_CONTAINER" = 1 ]; then
    note "[計画] $APT_BASE_IMAGE コンテナ内の python3 で pip download -r requirements.txt -c constraints.txt → $OUT/wheels"
  else
    note "[計画] $PY -m pip download -r requirements.txt -c constraints.txt -d $OUT/wheels（対象 OS の python3 と版照合あり）"
  fi
  note "[計画] wheels の SHA256 manifest（$OUT/wheels/SHA256SUMS）も生成（SHA256SUMS 自身を除く全ファイルが対象）"
  note "→ 実行するには --fetch を指定してください。"
fi
echo ""

# ---------------------------------------------------------------------------
# 3. Python 実行系 .deb（python3 / python3-venv / python3-pip・H3）
#    素の閉域ホストには python3-venv が入っていないことが多く、venv 作成が最初に転ぶため同梱する。
# ---------------------------------------------------------------------------
echo "--- 3. Python 実行系＋基本ツール（.deb） ---"
# xz-utils=Node tarball(.tar.xz) 展開・unzip=HackGen 展開・fontconfig=fc-cache/fc-list（最終検証）に必要。
# 素の閉域ホストにはどれも無いことがあるため Python 実行系と一緒に集める。
# curl=起動スクリプトのストア健康確認（bootstrap.sh/status.sh）／make=make start 等の入口。
# どちらも「入れたばかりの Linux」には無いことがある（server minimal は curl 無し・make は常に無し）ため同梱する。
PYTHON_SYSTEM_PACKAGES="python3 python3-venv python3-pip xz-utils unzip fontconfig curl make"
if [ "$FETCH" = 1 ] && [ "$SKIP_PYTHON_SYSTEM" != 1 ]; then
  if _apt_collect_debs "$PYTHON_SYSTEM_PACKAGES" "$OUT/python/debs"; then
    ok "Python 実行系（.deb 一式）: $OUT/python/debs/"
  else
    fail "Python 実行系の収集に失敗しました。"
    exit 1
  fi
elif [ "$SKIP_PYTHON_SYSTEM" = 1 ]; then
  warn "--skip-python-system が指定されたため、Python 実行系の収集をスキップしました。"
else
  note "[計画] $APT_BASE_IMAGE コンテナで [$PYTHON_SYSTEM_PACKAGES] を download-only 収集"
  note "  → $OUT/python/debs/"
  note "→ 実行するには --fetch を指定してください（--skip-python-system で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 4. Docker イメージ（postgres / neo4j / elasticsearch+kuromoji）
# ---------------------------------------------------------------------------
echo "--- 4. Docker イメージ ---"
ES_IMAGE="sherpa/es-kuromoji:8.19.20"
PG_IMAGE="postgres:16"
NEO4J_IMAGE="neo4j:5-community"

if [ "$FETCH" = 1 ] && [ "$SKIP_DOCKER" != 1 ]; then
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker が見つかりません。導入するか、--skip-docker を指定してください。"
    exit 1
  fi
  note "postgres/neo4j を pull します..."
  docker pull "$PG_IMAGE"
  docker pull "$NEO4J_IMAGE"
  note "elasticsearch+kuromoji を docker/elasticsearch から build します..."
  docker build -t "$ES_IMAGE" ./docker/elasticsearch
  reset_dir "$OUT/docker-images"
  note "docker save でイメージを書き出します（数百MB〜1GB超・時間がかかります）..."
  docker save "$PG_IMAGE" -o "$OUT/docker-images/postgres-16.tar"
  docker save "$NEO4J_IMAGE" -o "$OUT/docker-images/neo4j-5-community.tar"
  docker save "$ES_IMAGE" -o "$OUT/docker-images/es-kuromoji-8.19.20.tar"
  ok "docker イメージ: $OUT/docker-images/"
elif [ "$SKIP_DOCKER" = 1 ]; then
  warn "--skip-docker が指定されたため、Docker イメージの収集をスキップしました。"
else
  note "[計画] docker pull $PG_IMAGE"
  note "[計画] docker pull $NEO4J_IMAGE"
  note "[計画] docker build -t $ES_IMAGE ./docker/elasticsearch"
  note "[計画] docker save (それぞれ) -o $OUT/docker-images/*.tar"
  note "→ 実行するには --fetch を指定してください（--skip-docker で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 5. Docker Engine 本体 .deb（H3・scripts/install-docker.sh のリポジトリ追加手順を踏襲）
#    収集マシン自身の docker を使って素のコンテナ内でリポジトリ追加→download-only する。
# ---------------------------------------------------------------------------
echo "--- 5. Docker Engine 本体（.deb） ---"
DOCKER_ENGINE_PACKAGES="docker-ce docker-ce-cli containerd.io docker-buildx-plugin docker-compose-plugin"
# シングルクオート代入＝この時点では展開しない。$(...)・$VERSION_CODENAME はコンテナ内 bash が展開する。
DOCKER_ENGINE_EXTRA_SETUP='
    apt-get install -y ca-certificates curl gnupg
    install -m 0755 -d /etc/apt/keyrings
    curl -fsSL https://download.docker.com/linux/ubuntu/gpg -o /etc/apt/keyrings/docker.asc
    chmod a+r /etc/apt/keyrings/docker.asc
    cp /etc/apt/keyrings/docker.asc /out/docker.asc
    . /etc/os-release
    echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.asc] https://download.docker.com/linux/ubuntu $VERSION_CODENAME stable" > /etc/apt/sources.list.d/docker.list
    apt-get update
'
if [ "$FETCH" = 1 ] && [ "$SKIP_DOCKER_ENGINE" != 1 ]; then
  if ! command -v docker >/dev/null 2>&1; then
    fail "docker が見つかりません（収集マシン自身に要る・導入するか --skip-docker-engine を指定してください）。"
    exit 1
  fi
  if _apt_collect_debs "$DOCKER_ENGINE_PACKAGES" "$OUT/docker-engine/debs" "$DOCKER_ENGINE_EXTRA_SETUP"; then
    ok "Docker Engine（.deb 一式）: $OUT/docker-engine/debs/（鍵: $OUT/docker-engine/docker.asc）"
  else
    fail "Docker Engine の収集に失敗しました。"
    exit 1
  fi
elif [ "$SKIP_DOCKER_ENGINE" = 1 ]; then
  warn "--skip-docker-engine が指定されたため、Docker Engine の収集をスキップしました。"
else
  note "[計画] $APT_BASE_IMAGE コンテナでリポジトリ追加（download.docker.com・scripts/install-docker.sh と同じ鍵）"
  note "  → [$DOCKER_ENGINE_PACKAGES] を download-only 収集 → $OUT/docker-engine/debs/"
  note "  鍵（docker.asc）も来歴として $OUT/docker-engine/docker.asc へ保存"
  note "→ 実行するには --fetch を指定してください（--skip-docker-engine で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 6. Ollama モデルデータ（既定は案内表示のみ・サイズが大きいため個別オプトイン）
#    ※ このスクリプトが収集するのは ~/.ollama のモデルデータのみ。
#      ollama 本体バイナリは含まない＝閉域側ホストへ別途導入すること。
# ---------------------------------------------------------------------------
echo "--- 6. Ollama モデルデータ（ローカルLLM・任意） ---"
if [ -n "$WITH_OLLAMA" ]; then
  if ! command -v ollama >/dev/null 2>&1; then
    fail "ollama コマンドが見つかりません。先に https://ollama.com/download からこの機体へ導入してください。"
    exit 1
  fi
  note "モデル '$WITH_OLLAMA' を pull します（サイズが大きい場合、時間がかかります）..."
  if ollama pull "$WITH_OLLAMA"; then
    ok "モデル pull 完了: $WITH_OLLAMA"
  else
    fail "ollama pull に失敗しました。モデル名・ネットワーク接続を確認してください。"
    exit 1
  fi
  OLLAMA_HOME="${OLLAMA_MODELS_DIR:-$HOME/.ollama}"
  if [ -d "$OLLAMA_HOME" ]; then
    note "$OLLAMA_HOME を $OUT/ollama/dot-ollama/ へコピーします..."
    reset_dir "$OUT/ollama/dot-ollama"
    cp -a "$OLLAMA_HOME/." "$OUT/ollama/dot-ollama/"
    ok "Ollama モデルデータ: $OUT/ollama/dot-ollama/（本体バイナリは含まない）"
  else
    fail "$OLLAMA_HOME が見つかりません（OLLAMA_MODELS_DIR で場所を指定できます）。"
    exit 1
  fi
else
  note "[計画] このマシンに ollama を導入し、モデルを pull して ~/.ollama のモデルデータを収集します。"
  note "  1) https://ollama.com/download から Linux 用インストーラを取得・実行（ネットワーク要）"
  note "  2) ollama pull <model>            例: ollama pull gpt-oss:20b"
  note "  3) ~/.ollama を $OUT/ollama/dot-ollama/ へコピー"
  note "→ 2〜3 をこのスクリプトに行わせるには --with-ollama <model> を指定してください。"
  note "  （モデルは数GB〜数十GBになるため、既定では自動 pull しません。ollama 本体バイナリは"
  note "   このスクリプトでは収集しません＝閉域側ホストへ別途導入してください）"
fi
echo ""

# ---------------------------------------------------------------------------
# 7. フォント（Noto Sans CJK JP・HackGen。SIL OFL 1.1・バンドル/再配布可でライセンス確認済み）
# ---------------------------------------------------------------------------
echo "--- 7. フォント（Noto Sans CJK JP・HackGen） ---"
HACKGEN_API="https://api.github.com/repos/yuru7/HackGen/releases/latest"
if [ "$FETCH" = 1 ] && [ "$SKIP_FONTS" != 1 ]; then
  reset_dir "$OUT/fonts"
  if _apt_collect_debs "fonts-noto-cjk" "$OUT/fonts/noto-cjk-debs"; then
    ok "Noto Sans CJK JP（.deb 一式）: $OUT/fonts/noto-cjk-debs/"
  else
    warn "fonts-noto-cjk の収集に失敗しました（収集を継続します）。"
  fi

  note "HackGen の最新リリースを $HACKGEN_API から解決します..."
  HACKGEN_JSON="$(curl -fsSL "$HACKGEN_API" || true)"
  # L1: 通常版（ベース名が HackGen_v で始まる・Nerd Font 版でない）を優先。無ければ先頭 zip にフォールバック。
  # RV High（2026-07-09）: set -o pipefail 下では grep の 0 件ヒット（exit 1）で代入ごとスクリプトが
  # 落ち、下の「解決できませんでした」フォールバックに到達しない。抽出は 0 件を正常として扱う。
  HACKGEN_URLS="$(printf '%s' "$HACKGEN_JSON" | grep -o '"browser_download_url": *"[^"]*\.zip"' | sed -E 's/.*"(https[^"]+)"/\1/' || true)"
  HACKGEN_URL="$(printf '%s\n' "$HACKGEN_URLS" | grep -E '/HackGen_v[0-9]' | head -1 || true)"
  [ -n "$HACKGEN_URL" ] || HACKGEN_URL="$(printf '%s\n' "$HACKGEN_URLS" | head -1)"
  if [ -n "$HACKGEN_URL" ]; then
    mkdir -p "$OUT/fonts/hackgen"
    if curl -fsSL "$HACKGEN_URL" -o "$OUT/fonts/hackgen/$(basename "$HACKGEN_URL")"; then
      ok "HackGen（zip）: $OUT/fonts/hackgen/$(basename "$HACKGEN_URL")"
    else
      warn "HackGen のダウンロードに失敗しました: $HACKGEN_URL（収集を継続します）。"
    fi
  else
    warn "HackGen の最新リリース URL を解決できませんでした（GitHub API 制限・ネットワーク不通の可能性・収集を継続します）。"
  fi
elif [ "$SKIP_FONTS" = 1 ]; then
  warn "--skip-fonts が指定されたため、フォントの収集をスキップしました。"
else
  note "[計画] $APT_BASE_IMAGE コンテナで fonts-noto-cjk を download-only 収集 → $OUT/fonts/noto-cjk-debs/"
  note "[計画] curl -fsSL $HACKGEN_API から最新 zip の URL を解決してダウンロード（通常版優先）→ $OUT/fonts/hackgen/"
  note "→ 実行するには --fetch を指定してください（--skip-fonts で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 8. Node.js 本体 + marp-cli（marp スキルの実行に必須・RUNTIME-SANDBOX §9）
# ---------------------------------------------------------------------------
echo "--- 8. Node.js + marp-cli ---"
NODE_TARBALL="node-v${NODE_VERSION}-linux-x64.tar.xz"
NODE_URL="https://nodejs.org/dist/v${NODE_VERSION}/${NODE_TARBALL}"
NODE_SHASUMS_URL="https://nodejs.org/dist/v${NODE_VERSION}/SHASUMS256.txt"
if [ "$FETCH" = 1 ] && [ "$SKIP_NODE" != 1 ]; then
  reset_dir "$OUT/node"
  reset_dir "$OUT/marp"
  note "Node.js v${NODE_VERSION} を取得します: $NODE_URL"
  if curl -fsSL "$NODE_URL" -o "$OUT/node/$NODE_TARBALL" && curl -fsSL "$NODE_SHASUMS_URL" -o "$OUT/node/SHASUMS256.txt"; then
    if (cd "$OUT/node" && grep " $NODE_TARBALL\$" SHASUMS256.txt | sha256sum -c -); then
      ok "Node.js: $OUT/node/$NODE_TARBALL（sha256 検証OK）"
    else
      fail "Node.js tarball の sha256 検証に失敗しました（改ざん・破損の疑い）。$OUT/node/ を確認してください。"
      exit 1
    fi
  else
    fail "Node.js の取得に失敗しました。ネットワーク接続を確認してください。"
    exit 1
  fi

  if [ -d "$ROOT/tools/marp/node_modules" ]; then
    note "既存の tools/marp/node_modules を tar 化します..."
  elif command -v npm >/dev/null 2>&1; then
    note "npm --prefix tools/marp install @marp-team/marp-cli を実行します（ネットワーク要）..."
    npm --prefix "$ROOT/tools/marp" install @marp-team/marp-cli
  else
    fail "tools/marp/node_modules が無く、npm も見つかりません。marp-cli を導入できません。"
    exit 1
  fi
  tar -C "$ROOT/tools/marp" -czf "$OUT/marp/tools-marp-node_modules.tar.gz" node_modules
  ok "marp-cli: $OUT/marp/tools-marp-node_modules.tar.gz"
elif [ "$SKIP_NODE" = 1 ]; then
  warn "--skip-node が指定されたため、Node.js/marp-cli の収集をスキップしました。"
else
  note "[計画] curl -fsSL $NODE_URL -o $OUT/node/$NODE_TARBALL（+ SHASUMS256.txt で sha256 検証）"
  note "[計画] tools/marp/node_modules が無ければ npm --prefix tools/marp install @marp-team/marp-cli"
  note "  → tar czf $OUT/marp/tools-marp-node_modules.tar.gz"
  note "→ 実行するには --fetch を指定してください（--skip-node で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 9. Playwright Chromium（本体＋システム共有ライブラリ .deb・marp の PDF/PPTX レンダに必須）
#    sherpa/agents.py _detect_chrome_path が chromium-*/chrome-linux64/chrome を自動検出する。
# ---------------------------------------------------------------------------
echo "--- 9. Playwright Chromium ---"
# H2: Chromium のシステム依存パッケージ（playwright nativeDeps の "ubuntu24.04-x64".chromium と同一・全21個）。
# `install-deps --dry-run` は「収集マシンで欠けている差分」しか報告しない（依存が揃った開発機では常に空）
# ため、素の閉域ホスト向けの完全リストは動的には得られない＝この静的リストが正。
# APT_BASE_IMAGE を変えた場合は playwright の同リスト（該当 OS キー）に合わせて更新すること。
# 依存クロージャ自体はコンテナ内 apt が解決するので、ここはトップレベル名だけでよい。
CHROMIUM_DEPS="libasound2t64 libatk-bridge2.0-0t64 libatk1.0-0t64 libatspi2.0-0t64 libcairo2 libcups2t64 libdbus-1-3 libdrm2 libgbm1 libglib2.0-0t64 libnspr4 libnss3 libpango-1.0-0 libx11-6 libxcb1 libxcomposite1 libxdamage1 libxext6 libxfixes3 libxkbcommon0 libxrandr2"
if [ "$FETCH" = 1 ] && [ "$SKIP_CHROMIUM" != 1 ]; then
  reset_dir "$OUT/chromium"
  # M1: Ubuntu 24.04 は PEP 668（externally-managed-environment）でシステム Python に直接
  # pip install できないため、使い捨ての一時 venv に playwright を入れる。
  PW_VENV="$(mktemp -d)"
  note "一時 venv に playwright を導入します: $PW_VENV"
  "$PY" -m venv "$PW_VENV"
  "$PW_VENV/bin/python" -m pip install --quiet --upgrade pip playwright

  note "playwright install chromium を実行します（数百MB・時間がかかります）..."
  if ! "$PW_VENV/bin/python" -m playwright install chromium; then
    fail "playwright install chromium に失敗しました。ネットワーク接続を確認してください。"
    rm -rf "$PW_VENV"
    exit 1
  fi
  PLAYWRIGHT_HOME="${PLAYWRIGHT_BROWSERS_PATH:-$HOME/.cache/ms-playwright}"
  if [ ! -d "$PLAYWRIGHT_HOME" ]; then
    fail "$PLAYWRIGHT_HOME が見つかりません（PLAYWRIGHT_BROWSERS_PATH で場所を指定できます）。"
    rm -rf "$PW_VENV"
    exit 1
  fi
  # L2: firefox/webkit 等が収集機に入っていても膨らまないよう chromium 系ディレクトリだけを tar 化する
  # （_detect_chrome_path() が使うのは chromium-*/chrome-linux64/chrome のみ・ffmpeg は録画補助用途）。
  CHROMIUM_DIRS="$(cd "$PLAYWRIGHT_HOME" && find . -maxdepth 1 -type d \
    \( -name 'chromium-*' -o -name 'chromium_headless_shell-*' -o -name 'ffmpeg-*' \) -printf '%f\n')"
  if [ -z "$CHROMIUM_DIRS" ]; then
    fail "$PLAYWRIGHT_HOME に chromium-* ディレクトリが見つかりません。"
    rm -rf "$PW_VENV"
    exit 1
  fi
  # shellcheck disable=SC2086
  tar -C "$PLAYWRIGHT_HOME" -czf "$OUT/chromium/ms-playwright-chromium.tar.gz" $CHROMIUM_DIRS
  ok "Playwright Chromium 本体: $OUT/chromium/ms-playwright-chromium.tar.gz"

  rm -rf "$PW_VENV"

  # H2: Chromium はシステム共有ライブラリ（libnss3 等）が無いと起動しない。tar で運ぶブラウザ本体
  # だけでは最小構成の閉域ホストで PDF/PPTX 出力が全滅するため、依存 .deb も収集する。
  note "収集するシステム依存パッケージ: $CHROMIUM_DEPS"
  if _apt_collect_debs "$CHROMIUM_DEPS" "$OUT/chromium/deps-debs"; then
    ok "Chromium システム依存（.deb 一式）: $OUT/chromium/deps-debs/"
  else
    warn "Chromium システム依存の収集に失敗しました（本体 tar は収集済み・収集を継続します）。"
  fi
elif [ "$SKIP_CHROMIUM" = 1 ]; then
  warn "--skip-chromium が指定されたため、Playwright Chromium の収集をスキップしました。"
else
  note "[計画] 一時 venv に playwright を導入し playwright install chromium を実行"
  note "  → ~/.cache/ms-playwright/ の chromium-*/chromium_headless_shell-*/ffmpeg-* だけを"
  note "    tar czf $OUT/chromium/ms-playwright-chromium.tar.gz"
  note "[計画] Chromium のシステム依存（playwright 公式リスト・全21個）を"
  note "  $APT_BASE_IMAGE コンテナで download-only 収集 → $OUT/chromium/deps-debs/"
  note "→ 実行するには --fetch を指定してください（--skip-chromium で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 10. LibreOffice（旧形式 Office 変換バックエンドの選択肢の一つ・legacy_convert.py）
# ---------------------------------------------------------------------------
echo "--- 10. LibreOffice ---"
if [ "$FETCH" = 1 ] && [ "$SKIP_LIBREOFFICE" != 1 ]; then
  if _apt_collect_debs "libreoffice" "$OUT/libreoffice/debs"; then
    ok "LibreOffice（.deb 一式）: $OUT/libreoffice/debs/"
  else
    fail "LibreOffice の収集に失敗しました。"
    exit 1
  fi
elif [ "$SKIP_LIBREOFFICE" = 1 ]; then
  warn "--skip-libreoffice が指定されたため、LibreOffice の収集をスキップしました。"
else
  note "[計画] $APT_BASE_IMAGE コンテナで libreoffice を download-only 収集（依存込み・数百MB）"
  note "  → $OUT/libreoffice/debs/"
  note "→ 実行するには --fetch を指定してください（--skip-libreoffice で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 11. OCR（画像内文字の読み取り・既定ON）
#     3点そろって初めて動く: ①隔離ワーカーのイメージ ②専用 wheel ③モデル。
#     依存はコア（numpy 2.5系）と両立しない（paddlex は numpy<2.4 必須）ため、wheel も
#     Docker イメージもコアとは別に集める。
# ---------------------------------------------------------------------------
echo "--- 11. OCR（画像内文字の読み取り） ---"
if [ "$FETCH" = 1 ] && [ "$SKIP_OCR" != 1 ]; then
  # ① 隔離ワーカーのイメージ（アプリのコードを焼き込むため、配布物の版と対で作る）
  rm -rf "$OUT/ocr"; mkdir -p "$OUT/ocr"
  if sherpa_compose --profile ocr build ocr-worker >/dev/null 2>&1; then
    docker save sherpa/ocr-worker:paddleocr-3.7.0-cpu -o "$OUT/ocr/ocr-worker-paddleocr-3.7.0-cpu.tar"
    ok "OCR ワーカーのイメージ: $OUT/ocr/ocr-worker-paddleocr-3.7.0-cpu.tar"
  else
    fail "OCR ワーカーのイメージ作成に失敗しました（--skip-ocr で除外できます）。"
    exit 1
  fi
  # ② 専用 wheel（コンテナを使わずホストで動かす場合や、モデル取得スクリプト用）
  mkdir -p "$OUT/ocr/wheels"
  _pip_download_wheels requirements-ocr.txt "" "$OUT/ocr/wheels" >/dev/null
  ( cd "$OUT/ocr/wheels" && sha256sum ./* > SHA256SUMS )
  ok "OCR の wheel 一式: $OUT/ocr/wheels/"
  # ③ モデル（約134MB・Apache-2.0。配布可否は docker/ocr-models.lock.json の方針欄を参照）
  SHERPA_OCR_MODEL_CACHE="$OUT/ocr/models" "$ROOT/scripts/fetch_ocr_models.sh" >/dev/null
  cp "$ROOT/docker/ocr-models.lock.json" "$OUT/ocr/"
  ok "OCR のモデル: $OUT/ocr/models/（照合済み）"
elif [ "$SKIP_OCR" = 1 ]; then
  warn "--skip-ocr が指定されたため、OCR 資材の収集をスキップしました（閉域では画像内文字を読めません）。"
else
  note "[計画] OCR ワーカーのイメージを build → docker save（約550MB）"
  note "[計画] requirements-ocr.txt の wheel を pip download（約1.3GB 相当）"
  note "[計画] PP-OCRv6 モデルを取得し docker/ocr-models.lock.json と照合（約134MB）"
  note "  → $OUT/ocr/"
  note "→ 実行するには --fetch を指定してください（--skip-ocr で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 12. 土台の閉包（2026-08-17・理由は _apt_collect_base_closure のコメント）
#     コンテナの土台はイメージ再ビルドで更新され、インストール直後の閉域ホストより新しいことがある＝
#     「依存が土台で満たされる」前提が崩れる。土台の .deb も集めて索引に載せる。
# ---------------------------------------------------------------------------
echo "--- 12. 土台の閉包（base/debs） ---"
if [ "$FETCH" = 1 ] && [ "$SKIP_BASE" != 1 ]; then
  if _apt_collect_base_closure "$OUT/base/debs"; then
    ok "土台の閉包（.deb 一式）: $OUT/base/debs/"
  else
    # 2026-08-18 RV MED-3: 従来は warn で続行していたが、今回の実機不具合そのものが base 閉包の欠落
    # （閉域実機報告①）であり、warn のまま完成させると「直したつもりの壊れたキット」を出荷しかねない。
    # $BASE_CLOSURE_PACKAGES の typo・無効なパッケージ名で apt が失敗する場合もこの経路に落ちる＝
    # 明示的に省略した（--skip-base）場合を除き、収集失敗はキット生成を止める。
    fail "土台の閉包の収集に失敗しました（閉域ホストの土台がキットより古いと導入が unmet で止まります）。"
    fail "  BASE_CLOSURE_PACKAGES（現在値: [$BASE_CLOSURE_PACKAGES]）の typo・無効なパッケージ名の可能性があります。"
    fail "  意図して省略するなら --skip-base を指定してください（未収集は導入側で NG にはなりません）。"
    exit 1
  fi
elif [ "$SKIP_BASE" = 1 ]; then
  warn "--skip-base が指定されたため、土台の閉包をスキップしました。"
else
  note "[計画] $APT_BASE_IMAGE コンテナの導入済みパッケージ全部を --reinstall --download-only 収集 → $OUT/base/debs/"
  note "→ 実行するには --fetch を指定してください（--skip-base で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# 13. repo 索引（$OUT 直下 Packages/Packages.gz/Release）と BASELINE（2026-08-17）
#     .deb を 1 つでも集めたら索引を作り直す（Packages と .deb の不整合は Hash Sum mismatch で導入側が止まる）。
# ---------------------------------------------------------------------------
echo "--- 13. repo 索引と BASELINE ---"
if [ "$FETCH" = 1 ]; then
  if [ -n "$(find "$OUT" -name '*.deb' -print -quit 2>/dev/null)" ]; then
    if _apt_build_repo_index "$OUT"; then
      ok "repo 索引: $OUT/Packages（$(grep -c '^Package:' "$OUT/Packages" 2>/dev/null || echo 0) 件）・Release"
    else
      warn "repo 索引を作れませんでした（導入側は ./*.deb 列挙にフォールバックします）。"
    fi
  else
    note ".deb が無いため repo 索引は作りません。"
  fi
  if _apt_write_baseline "$OUT"; then
    ok "BASELINE: $OUT/BASELINE"
  else
    warn "BASELINE を書けませんでした（導入側は照合をスキップします）。"
  fi
else
  note "[計画] $OUT 直下に apt-ftparchive で Packages/Release を生成し、BASELINE（OS/arch/収集日/digest）を書く"
fi
echo ""

# ---------------------------------------------------------------------------
# 14. Codex CLI（Codex(OpenAI) 構成に必須・「OpenAI へだけ NW 穴あけ」の閉域で使う）
#     npm の導入済みツリー（@openai/codex ＋ 同梱の linux-x64 静的バイナリ）を tar で運ぶ。閉域では
#     `npm install` の依存解決（optionalDependencies の platform 別パッケージ）を再現しにくいため。
#     認証は閉域側で `printenv OPENAI_API_KEY | codex login --with-api-key`（通信不要・実測 2026-08-18）。
# ---------------------------------------------------------------------------
echo "--- 14. Codex CLI ---"
if [ "$FETCH" = 1 ] && [ "$SKIP_CODEX" != 1 ]; then
  reset_dir "$OUT/codex"
  CODEX_PKG_DIR="$(npm root -g 2>/dev/null)/@openai/codex"
  if [ -d "$CODEX_PKG_DIR" ] && [ -f "$CODEX_PKG_DIR/package.json" ]; then
    CODEX_VER="$(node -p "require(process.argv[1]).version" "$CODEX_PKG_DIR/package.json")"   # node は npm と同居（端末の Python に依存させない）
    if [ ! -x "$CODEX_PKG_DIR/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex" ]; then
      fail "Codex の linux-x64 バイナリが見つかりません（収集機が linux-x64 でないか、導入が不完全）。--skip-codex で除外できます。"
      exit 1
    fi
    tar -C "$(dirname "$CODEX_PKG_DIR")" -czf "$OUT/codex/openai-codex-$CODEX_VER-linux-x64.tar.gz" codex
    printf '%s\n' "$CODEX_VER" > "$OUT/codex/VERSION"
    ( cd "$OUT/codex" && sha256sum ./*.tar.gz > SHA256SUMS )
    ok "Codex CLI $CODEX_VER: $OUT/codex/（$(du -h "$OUT/codex"/*.tar.gz | cut -f1)）"
  else
    fail "Codex CLI が導入されていません（npm install -g @openai/codex）。--skip-codex で除外できます。"
    exit 1
  fi
elif [ "$SKIP_CODEX" = 1 ]; then
  warn "--skip-codex が指定されたため、Codex CLI の収集をスキップしました（閉域で Codex 構成は使えません）。"
else
  note "[計画] $(npm root -g 2>/dev/null)/@openai/codex を tar 化（linux-x64 静的バイナリ込み・tar 後 約130MB）"
  note "  → $OUT/codex/"
  note "→ 実行するには --fetch を指定してください（--skip-codex で除外可）。"
fi
echo ""

# ---------------------------------------------------------------------------
# MANIFEST
# ---------------------------------------------------------------------------
MANIFEST="$OUT/MANIFEST.txt"
{
  echo "Sherpa offline-kit MANIFEST"
  echo "生成日時: $(date -Iseconds 2>/dev/null || date)"
  echo "SHERPA_VERSION: $SHERPA_VERSION"
  echo "APT_BASE_IMAGE: $APT_BASE_IMAGE"
  echo ""
  echo "内容:"
  find "$OUT" -maxdepth 2 -mindepth 1 | sort
} > "$MANIFEST"
ok "MANIFEST: $MANIFEST"

echo ""
echo "=== 完了 ==="
echo "出力先: $OUT"
echo "閉域側への搬入・セットアップ手順は docs/manual/offline-kit.md を参照してください。"
