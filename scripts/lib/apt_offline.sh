#!/usr/bin/env bash
# 閉域導入の apt 経路（共有ライブラリ・source 用／CLI でも実行可）。
#
# 背景（2026-08-17 実測・診断「apt 経路の再現/監査」）:
#   旧実装 `sudo apt-get install -y ./*.deb` は
#     (a) 依存解決の結果として既存パッケージの削除を「-y」が無言で承認する（稼働中カーネル
#         linux-image-* まで連鎖し得る＝閉域実機で紫の debconf 画面が出た報告の有力経路・推測）、
#     (b) 収集コンテナ（ubuntu:24.04 は再ビルドで土台が更新される）と閉域ホストの土台がずれると
#         「Depends: libc6 (= 新版) but 旧版 is to be installed」等の unmet で全停止（実測）、
#     (c) DEBIAN_FRONTEND 未指定＋sudo env_reset で debconf/conffile 質問が端末で止まり得る、
#   という 3 つの弱点を持っていた。本ライブラリはこれを次で塞ぐ:
#     - 常に `-s`（シミュレーション）を先行し、出力に Remv（削除）があれば**実行前に中止**して
#       何を消そうとしたかを表示する。実行時も `--no-remove` で構造的に削除を禁止する。
#     - kit_root に apt-ftparchive の索引（Packages/Release）があれば、それを file: のフラット repo
#       として一時 sourcelist だけで apt に読ませ、**トップレベル名**を解かせる（./*.deb 全列挙と違い、
#       閉域側が新しい版を持っていても降格にならず、土台の閉包 base/debs も候補に載る）。
#     - 索引の無い旧キットは ./*.deb 列挙にフォールバック（同じく -s 先行＋--no-remove）。
#     - DEBIAN_FRONTEND=noninteractive・--force-confdef/--force-confold・NEEDRESTART_MODE=l を
#       sudo の内側で渡す（sudo は env_reset のため呼び出し側の export は届かない）。
#     - 失敗時は「不足しているパッケージ名」を抽出して表示し、オンライン側の収集に足すべき名前を案内。
#     - 導入後に稼働中カーネル（linux-image-*）が消えていないことを確認する。
#
# 使い方（source）:
#   . scripts/lib/apt_offline.sh
#   apt_offline_install "Python 実行系" "$OUT" "$OUT/python/debs"          # 名前は group_dir/PACKAGES から
#   apt_offline_install "Docker Engine" "$OUT" "$OUT/docker-engine/debs" docker-ce docker-ce-cli
#   apt_offline_check_baseline "$OUT"                                     # 土台の事前照合
# 使い方（CLI・Docker 検証や将来のテストで実コードを叩く）:
#   bash scripts/lib/apt_offline.sh install <desc> <kit_root> <group_dir> [names...]
#   bash scripts/lib/apt_offline.sh baseline <kit_root>
#   bash scripts/lib/apt_offline.sh write-baseline <kit_root>   # 収集側（コンテナ内）で BASELINE を書く
#
# 環境変数:
#   SHERPA_APT_OFFLINE_SUDO   apt-get 等の前置コマンド（既定: root なら空、それ以外は "sudo"）。
#                             テストは空にして PATH 上の偽 apt-get を叩かせる。
#   SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH=1  BASELINE 不一致を警告のみに緩和。
#
# 戻り値（apt_offline_install）: 0=導入成功 1=該当 .deb が無い（スキップ・非致命的） 2=導入失敗（致命的）

# --- 出力ヘルパ（呼び出し元が定義済みならそちらを尊重） -------------------------------------
if ! declare -F note >/dev/null 2>&1; then note()  { echo "・ $*"; }; fi
if ! declare -F ok   >/dev/null 2>&1; then ok()    { echo "OK: $*"; }; fi
if ! declare -F warn >/dev/null 2>&1; then warn()  { echo "ⓘ  $*" >&2; }; fi
if ! declare -F fail >/dev/null 2>&1; then fail()  { echo "✗ $*" >&2; }; fi

if [ -z "${SHERPA_APT_OFFLINE_SUDO+x}" ]; then
  if [ "$(id -u)" = 0 ]; then SHERPA_APT_OFFLINE_SUDO=""; else SHERPA_APT_OFFLINE_SUDO="sudo"; fi
fi

# 非対話・削除禁止の共通引数。--allow-downgrades は付けない（man apt-get: "can potentially destroy
# your system"）。降格が出た＝土台ずれの兆候なので止めるのが正。Recommends 方針は収集側（Ubuntu 既定＝
# Recommends 込み）と揃えるため --no-install-recommends も付けない。
# LC_ALL=C.UTF-8: apt の出力を英語に固定する。sudo が LANG/LC_* を通す機体（env_check 既定・日本語ロケール）や
# root 直実行では「以下のパッケージは「削除」されます」等に翻訳され、下の英語パターンの抽出が全滅する（実測）。
_APT_OFFLINE_ENV=(DEBIAN_FRONTEND=noninteractive NEEDRESTART_MODE=l LC_ALL=C.UTF-8)
_APT_OFFLINE_OPTS=(-y --no-remove
  -o Dpkg::Options::=--force-confdef -o Dpkg::Options::=--force-confold
  -o DPkg::Lock::Timeout=300)

# sudo の内側で環境変数を渡す（`sudo VAR=val cmd`＝SETENV は ALL 許可で暗黙）。root/テストなら env で。
_apt_offline_run() {
  if [ -n "$SHERPA_APT_OFFLINE_SUDO" ]; then
    # shellcheck disable=SC2086
    $SHERPA_APT_OFFLINE_SUDO "${_APT_OFFLINE_ENV[@]}" "$@"
  else
    env "${_APT_OFFLINE_ENV[@]}" "$@"
  fi
}

# シミュレーション出力から「不足パッケージ名」を抽出して表示（オンライン側の収集に足すべき名前）。
_apt_offline_report_missing() {  # $1=シミュレーション出力ファイル
  local log="$1" missing
  missing="$( { grep -oE '(Depends|Breaks|Conflicts|PreDepends): [^ ]+( \([^)]*\))? but' "$log" 2>/dev/null \
    | sed -E 's/^[A-Za-z]+: //; s/ but$//'; grep -oE 'Unable to locate package [^ ]+' "$log" 2>/dev/null \
    | sed -E 's/^Unable to locate package /(索引に無い) /'; } | sort -u || true)"
  if [ -n "$missing" ]; then
    fail "  不足している（閉包に無い／版が合わない）パッケージ:"
    while IFS= read -r line; do fail "    - $line"; done <<<"$missing"
    fail "  → オンライン側の収集（make_offline_kit.sh）にこれらの名前を足すか、土台の閉包（--fetch の"
    fail "    「土台の閉包」ステップ＝base/debs）を含むキットで再収集してください。"
    fail "    土台のずれ（libc6/perl-base/zlib1g 等の版違い）は BASELINE の照合結果も参照。"
  fi
  if grep -qE 'not installable' "$log" 2>/dev/null; then
    fail "  「not installable」= 閉包にその .deb 自体が無い（収集側コンテナに導入済みで集まらなかった）。"
  fi
  if grep -qE 'DOWNGRADED' "$log" 2>/dev/null; then
    fail "  降格（DOWNGRADED）が要求されました。閉域側の方が新しい版を持っています＝キットの土台が古い。"
    fail "  --allow-downgrades は使いません。索引付きキット（repo 名指定）で再導入するか再収集してください。"
  fi
}

# 稼働中カーネルの残存確認。導入前に「linux-image-*」の一覧を取り、導入後に消えたものがあれば fail。
_apt_offline_kernel_snapshot() {
  dpkg-query -W -f='${binary:Package}\n' 'linux-image*' 2>/dev/null | sort || true
}
_apt_offline_kernel_verify() {  # $1=導入前スナップショット（改行区切り）
  local before="$1" after gone running
  after="$(_apt_offline_kernel_snapshot)"
  gone="$(comm -23 <(printf '%s\n' "$before" | sed '/^$/d') <(printf '%s\n' "$after" | sed '/^$/d') || true)"
  if [ -n "$gone" ]; then
    fail "導入の結果、カーネルパッケージが消えています（重大）: $(echo "$gone" | tr '\n' ' ')"
    fail "  再起動しないでください。sudo apt-get install --reinstall <上記> で復旧してから続行してください。"
    return 1
  fi
  running="linux-image-$(uname -r)"
  if printf '%s\n' "$before" | grep -qx "$running"; then
    if ! dpkg-query -W -f='${db:Status-Status}\n' "$running" 2>/dev/null | grep -qx installed; then
      fail "稼働中カーネル $running が installed 状態ではなくなっています（重大）。再起動しないでください。"
      return 1
    fi
  fi
  return 0
}

# シミュレーション結果の検査。Remv があれば中止（何を消そうとしたか表示）。
_apt_offline_check_sim() {  # $1=desc $2=sim log
  local desc="$1" log="$2" remv listed
  # 実 apt は --no-remove 付きの -s で削除が要ると「will be REMOVED: 一覧」＋
  # 「E: Packages need to be removed but remove is disabled.」を出して非0で返す（Remv 行は出ない）。
  # 偽 apt / 古い apt は Remv/Purg 行を出す。どちらの形でも拾う。
  remv="$(grep -E '^(Remv|Purg) ' "$log" 2>/dev/null || true)"
  listed="$(awk '/will be REMOVED/{f=1;next} f&&/^  /{print "    " $0;next} f{f=0}' "$log" 2>/dev/null || true)"
  if [ -n "$remv" ] || [ -n "$listed" ] || grep -qE 'need to be removed but remove is disabled' "$log" 2>/dev/null; then
    fail "$desc の導入は既存パッケージの削除を伴うため中止します（削除は一切許可しません）:"
    while IFS= read -r line; do [ -n "$line" ] && fail "    $line"; done <<<"$remv"
    while IFS= read -r line; do [ -n "$line" ] && fail "$line"; done <<<"$listed"
    if grep -qE 'linux-image' "$log" 2>/dev/null; then
      fail "  ※ 稼働中カーネル（linux-image-*）が削除対象に含まれています。閉包の穴（土台のずれ）が原因です。"
    fi
    _apt_offline_report_missing "$log"
    return 1
  fi
  return 0
}

# 本体。$1=説明 $2=kit_root（索引 Packages の置き場＝dist/offline-kit） $3=group_dir（.deb 群） $4..=パッケージ名
apt_offline_install() {
  local desc="$1" kit_root="$2" group_dir="$3"; shift 3
  local names=("$@") mode before rc=0 simlog tmpd A=() kit_uri
  if [ ! -d "$group_dir" ] || [ -z "$(find "$group_dir" -maxdepth 1 -name '*.deb' 2>/dev/null)" ]; then
    warn "$desc の .deb が見つかりません（$group_dir）。オンライン側で収集していないためスキップします。"
    return 1
  fi
  # 相対パスは必ず絶対化する。apt の file: URI は絶対パス必須（相対だと "Invalid URI, local URIS must
  # not start with //"）、./*.deb 列挙もローカルファイルは ./ か / 始まりでないと「パッケージ名」扱いになる（実測）。
  group_dir="$(cd "$group_dir" && pwd -P)"
  [ -d "$kit_root" ] && kit_root="$(cd "$kit_root" && pwd -P)"
  if [ ${#names[@]} -eq 0 ] && [ -f "$group_dir/PACKAGES" ]; then
    # shellcheck disable=SC2207
    names=($(tr -s ' \n\t' '   ' < "$group_dir/PACKAGES"))
  fi
  if [ -f "$kit_root/Packages" ] && [ ${#names[@]} -gt 0 ]; then
    mode="repo"
  else
    mode="files"
    [ -f "$kit_root/Packages" ] || note "$desc: 索引（$kit_root/Packages）が無い旧キットのため ./*.deb 列挙で導入します。"
  fi
  simlog="$(mktemp)"
  before="$(_apt_offline_kernel_snapshot)"

  if [ "$mode" = repo ]; then
    tmpd="$(mktemp -d)"
    mkdir -p "$tmpd/lists/partial"
    # sources.list の一行形式は空白で区切るため、パスに空白/%/#/? があると誤解釈される（実測: 空白入りで
    # 「File not found - …/dists/…」）。URI としてパーセントエンコードして書く。
    kit_uri="${kit_root//%/%25}"; kit_uri="${kit_uri// /%20}"; kit_uri="${kit_uri//#/%23}"; kit_uri="${kit_uri//\?/%3F}"
    echo "deb [trusted=yes] file:$kit_uri ./" > "$tmpd/sources.list"
    # 既存 /etc/apt を汚さず・ネット源（sources.list.d/ubuntu.sources）も読まない（sourceparts=-）。
    # APT::Sandbox::User=root: _apt ユーザーが $HOME(0750) 配下のキットを辿れず update が失敗するのを避ける
    # （sha256 で搬入検証済みのローカル媒体なので許容）。
    # Acquire::Check-Date=false: 閉域機は NTP が無く時計が収集日より前のことがある。Release の Date 検査に
    # かかると "Release file … is not valid yet" で update が止まる（実測）。sha256 で搬入検証済みのローカル
    # 媒体なので Date 検査を外しても安全性は落ちない。
    A=(-o "Dir::Etc::sourcelist=$tmpd/sources.list" -o "Dir::Etc::sourceparts=-"
       -o "Dir::State::lists=$tmpd/lists" -o "APT::Sandbox::User=root"
       -o "Acquire::Check-Date=false")
    note "$desc: ローカル repo（file:$kit_root）から [${names[*]}] を導入します..."
    if ! _apt_offline_run apt-get "${A[@]}" update; then
      fail "$desc: ローカル repo の読み込み（apt-get update）に失敗しました。考えられる原因:"
      fail "  - 索引（Packages/Release）が壊れている／.deb を差し替えたのに索引を作り直していない（Hash Sum mismatch）"
      fail "  - キットの置き場のパスに apt が扱えない文字がある（上の Err 行の URI を確認）"
      fail "  - sudo がコマンド限定で環境変数を渡せない（sudoers に SETENV が要る・\"not allowed to set\" が出る）"
      rm -rf "$tmpd" "$simlog"; return 2
    fi
    note "$desc: 事前シミュレーション（apt-get -s）..."
    _apt_offline_run apt-get "${A[@]}" "${_APT_OFFLINE_OPTS[@]}" -s install "${names[@]}" > "$simlog" 2>&1 || rc=$?
    cat "$simlog"
    if [ "${rc:-0}" != 0 ]; then
      # 削除が要る場合は --no-remove により -s 自体が非0で返る（実測）。理由を「依存が解決できない」で
      # 誤魔化さず、削除を伴うこと（カーネル含有なら警告）を先に言う。
      _apt_offline_check_sim "$desc" "$simlog" || { rm -rf "$tmpd" "$simlog"; return 2; }
      fail "$desc: 事前シミュレーションで依存が解決できませんでした（何も変更していません）。"
      _apt_offline_report_missing "$simlog"; rm -rf "$tmpd" "$simlog"; return 2
    fi
    if ! _apt_offline_check_sim "$desc" "$simlog"; then rm -rf "$tmpd" "$simlog"; return 2; fi
    note "$desc: 本導入（--no-remove・非対話）..."
    if _apt_offline_run apt-get "${A[@]}" "${_APT_OFFLINE_OPTS[@]}" install "${names[@]}"; then rc=0; else rc=$?; fi
    rm -rf "$tmpd"
  else
    note "$desc: 事前シミュレーション（apt-get -s ./*.deb）..."
    _apt_offline_run apt-get "${_APT_OFFLINE_OPTS[@]}" -s install "$group_dir"/*.deb > "$simlog" 2>&1 || rc=$?
    cat "$simlog"
    if [ "${rc:-0}" != 0 ]; then
      _apt_offline_check_sim "$desc" "$simlog" || { rm -f "$simlog"; return 2; }
      fail "$desc: 事前シミュレーションで依存が解決できませんでした（何も変更していません）。"
      _apt_offline_report_missing "$simlog"; rm -f "$simlog"; return 2
    fi
    if ! _apt_offline_check_sim "$desc" "$simlog"; then rm -f "$simlog"; return 2; fi
    note "$desc: 本導入（--no-remove・非対話）..."
    if _apt_offline_run apt-get "${_APT_OFFLINE_OPTS[@]}" install "$group_dir"/*.deb; then rc=0; else rc=$?; fi
  fi
  rm -f "$simlog"

  if [ "$rc" != 0 ]; then
    fail "$desc の導入に失敗しました（apt-get exit=$rc）。"
    fail "  復旧を試すには: sudo dpkg --configure -a; sudo apt-get -f -s install で計画を確認し（削除が無いこと）、"
    fail "  sudo apt-get -f --no-remove install  の後、再実行してください（-f だけの実行は削除を提案し得るので使わない）。"
    fail "  それでも失敗する場合は、オンライン側でこの機体と同じ OS/アーキテクチャ（BASELINE 参照）で収集し直してください。"
    return 2
  fi
  if ! _apt_offline_kernel_verify "$before"; then return 2; fi
  ok "$desc を導入しました（削除ゼロ・カーネル残存を確認）。"
  return 0
}

# --- BASELINE（土台の照合） ------------------------------------------------------------------
# 収集側（素のコンテナ内）で書く。key=value 行。
apt_offline_write_baseline() {  # $1=出力先ファイル [$2=IMAGE $3=IMAGE_DIGEST $4=IMAGE_CREATED $5=PYTHON_DEB_VERSION]
  local out="$1" image="${2:-}" digest="${3:-}" created="${4:-}" pyver="${5:-}"
  # shellcheck disable=SC1091
  . /etc/os-release
  {
    echo "ID=${ID:-}"
    echo "VERSION_ID=${VERSION_ID:-}"
    echo "VERSION_CODENAME=${VERSION_CODENAME:-}"
    echo "VERSION=${VERSION:-}"
    echo "ARCH=$(dpkg --print-architecture)"
    echo "PYTHON_DEB_VERSION=$pyver"
    echo "COLLECTED_AT=$(date -Iseconds)"
    echo "IMAGE=$image"
    echo "IMAGE_DIGEST=$digest"
    echo "IMAGE_CREATED=$created"
    echo "APT_VERSION=$(apt-get --version 2>/dev/null | head -1)"
    echo "LIBC6=$(dpkg-query -W -f='${Version}' libc6 2>/dev/null || true)"
  } > "$out"
}

_baseline_get() { sed -n "s/^$2=//p" "$1" | head -1; }

# 導入側。kit_root/BASELINE とホストを照合。不一致は fail（SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH=1 で警告のみ）。
apt_offline_check_baseline() {  # $1=kit_root
  local kit_root="$1" b="$1/BASELINE" mism=0 host_id host_ver host_code host_arch k_id k_ver k_code k_arch
  if [ ! -f "$b" ]; then
    warn "BASELINE（$b）がありません（旧キット）。土台の照合をスキップして続行しますが、"
    warn "  収集元と OS/アーキテクチャがずれていると .deb 導入が依存不足で止まります（-s 先行で検出はします）。"
    return 0
  fi
  # shellcheck disable=SC1091
  host_id="$(. /etc/os-release; echo "${ID:-}")"
  host_ver="$(. /etc/os-release; echo "${VERSION_ID:-}")"
  host_code="$(. /etc/os-release; echo "${VERSION_CODENAME:-}")"
  host_arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
  k_id="$(_baseline_get "$b" ID)"; k_ver="$(_baseline_get "$b" VERSION_ID)"
  k_code="$(_baseline_get "$b" VERSION_CODENAME)"; k_arch="$(_baseline_get "$b" ARCH)"
  note "土台の照合（BASELINE: 収集 $(_baseline_get "$b" COLLECTED_AT) / イメージ $(_baseline_get "$b" IMAGE) $(_baseline_get "$b" IMAGE_DIGEST)）"
  printf '  %-18s %-24s %-24s %s\n' "項目" "キット(BASELINE)" "この機体" "判定"
  local row
  for row in "ID:$k_id:$host_id" "VERSION_ID:$k_ver:$host_ver" "VERSION_CODENAME:$k_code:$host_code" "ARCH:$k_arch:$host_arch"; do
    local key kv hv verdict
    IFS=: read -r key kv hv <<<"$row"
    if [ -n "$kv" ] && [ "$kv" != "$hv" ]; then verdict="不一致"; mism=1; else verdict="一致"; fi
    printf '  %-18s %-24s %-24s %s\n' "$key" "${kv:-?}" "${hv:-?}" "$verdict"
  done
  if [ "$mism" = 1 ]; then
    if [ "${SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH:-0}" = 1 ]; then
      warn "BASELINE と不一致ですが SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH=1 のため続行します（自己責任）。"
      return 0
    fi
    fail "収集元（BASELINE）とこの機体の OS/アーキテクチャが一致しません。.deb の依存名・版が合わず導入できません。"
    fail "  この機体と同じ OS（例: APT_BASE_IMAGE=ubuntu:<版>）で収集し直してください。"
    fail "  どうしても続行する場合のみ SHERPA_OFFLINE_ALLOW_BASELINE_MISMATCH=1 を付けて再実行してください。"
    return 1
  fi
  ok "土台の照合: 一致（ID/VERSION_ID/CODENAME/ARCH）"
  return 0
}

# --- CLI モード ------------------------------------------------------------------------------
if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  set -Eeuo pipefail
  cmd="${1:-}"; shift || true
  case "$cmd" in
    install)        apt_offline_install "$@" ;;
    baseline)       apt_offline_check_baseline "$@" ;;
    write-baseline) apt_offline_write_baseline "$@" ;;
    *) echo "usage: $0 install <desc> <kit_root> <group_dir> [names...] | baseline <kit_root> | write-baseline <out> [image digest created pyver]" >&2; exit 2 ;;
  esac
fi
