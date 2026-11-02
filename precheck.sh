#!/usr/bin/env bash
# Sherpa の導入前の事前確認（読み取り専用）。この環境に足りないものを一覧で見せる。
#
#   展開の前に単独で: bash sherpa-<版>-precheck-<コミット>.sh
#   展開したあと    : cd Sherpa && ./precheck.sh
#   オプション      : --dir <導入先フォルダ>（既定はカレント。空きや書き込みを見る場所）
#                     --api-url <URL>（推論の接続先。既定 https://api.openai.com/v1/models）
#                     --help
#
# 各項目を次の 4 通りで表示し、最後に件数をまとめる。
#   OK:                           問題ありません
#   ・ パッケージが入れます        導入のときに自動で入ります（何もしなくてよい）
#   ✗ 自分で用意してください       導入の前に、あなた（または管理者）が用意するものです
#   ⓘ  注意                       動きますが、確かめてほしいことです
# 終了コード: 0＝「自分で用意」が 0 件／1＝1 件以上／2＝使い方の誤り
#
# 対象: Ubuntu 24.04（x86_64）・WSL2（Ubuntu 24.04）・macOS（arm64／x86_64）。
# 手順書: INSTALL.md／環境変数: SHERPA_PRECHECK_SKIP_ONLINE=1（macOS の導入で取りに行く先への到達確認を省く）
#
# ① 引数と環境の種類 ② パッケージの情報 ③ 資源・導入先・ユーザー・道具 ④ Docker・systemd・各種設定
# ⑤ ポート・文字コード ⑥ macOS 固有（Homebrew・Python） ⑦ 通信 ⑧ 任意・既存の導入 ⑨ まとめ
# 守ること: sudo を呼ばない。何も入れず、ファイルを書かない。.env と鍵の中身を読まない・出さない。
#   外への通信は到達確認（HTTP の状態コードを見るだけ・認証ヘッダなし）に限る。
#   macOS 標準の bash 3.2 で動く書き方に留める（連想配列・mapfile・${x,,} を使わない）。
set -u

N_MUST=0
N_WARN=0

SEC_N=0
sec()    { SEC_N=$((SEC_N + 1)); echo ""; echo "--- ${SEC_N}. $* ---"; }
r_ok()   { echo "OK: $*"; }
r_auto() { echo "・ パッケージが入れます（何もしなくてよい）: $*"; }
r_info() { echo "・ $*"; }
r_must() { N_MUST=$((N_MUST + 1)); echo "✗ 自分で用意してください: $1"; [ -z "${2:-}" ] || echo "    → $2"; }
r_warn() { N_WARN=$((N_WARN + 1)); echo "ⓘ  注意: $1"; [ -z "${2:-}" ] || echo "    → $2"; }

usage() {
  cat <<'EOF'
使い方: bash precheck.sh [--dir <導入先フォルダ>] [--api-url <URL>] [--help]

導入の前に、この環境に足りないものを一覧で見せます（何も入れず、何も書きません）。
  --dir <導入先フォルダ>   ディスクの空きや書き込みを見る場所（既定はカレント）。
                           PACKAGE-INFO か sherpa-*-full-*.tar.gz があれば、版の照合にも使います。
  --api-url <URL>          推論の接続先への到達確認に使う URL（既定 https://api.openai.com/v1/models）。
                           Azure OpenAI などを使う人は、その URL を指定します。
  --help                   この説明を表示します。

表示の見かた:
  OK:                              問題ありません
  ・ パッケージが入れます           導入のときに自動で入ります（何もしなくてよい）
  ✗ 自分で用意してください          導入の前に、あなた（または管理者）が用意するものです
  ⓘ  注意                          動きますが、確かめてほしいことです

終了コード: 0＝「自分で用意」が 0 件／1＝1 件以上／2＝使い方の誤り
対象: Ubuntu 24.04（x86_64）・WSL2（Ubuntu 24.04）・macOS。手順書: INSTALL.md
EOF
}

die_usage() {
  echo "✗ $*" >&2
  echo "使い方: bash precheck.sh [--dir <導入先フォルダ>] [--api-url <URL>] [--help]" >&2
  exit 2
}

have() { command -v "$1" >/dev/null 2>&1; }

# 秒数を決めてコマンドを実行する。標準出力と標準エラーを TL_OUT に、終了コードを TL_RC に入れる（時間切れは 124）。
TL_OUT=""
TL_RC=0
tlimit() {
  local secs="$1" raw
  shift
  # 時間切れはコマンドのプロセスグループごと終わらせる（子が出力を握ったまま残ると戻らないため）。
  raw="$(
    set -m
    "$@" 2>&1 &
    cpid=$!
    exec 2>/dev/null
    ( sleep "${secs}"; kill -TERM -- "-${cpid}"; sleep 2; kill -KILL -- "-${cpid}" ) >/dev/null 2>&1 &
    wpid=$!
    rc=0
    wait "${cpid}" 2>/dev/null || rc=$?
    pkill -P "${wpid}" 2>/dev/null
    kill "${wpid}" 2>/dev/null
    printf '\n__RC__=%s\n' "${rc}"
  )"
  TL_RC="${raw##*__RC__=}"
  TL_OUT="${raw%__RC__=*}"
  while [ "${TL_OUT%$'\n'}" != "${TL_OUT}" ]; do TL_OUT="${TL_OUT%$'\n'}"; done
  case "${TL_RC}" in 143|137) TL_RC=124 ;; esac
  return 0
}

gb1() { awk -v m="$1" 'BEGIN { printf "%.1f", m / 1024 }'; }

# ---------------------------------------------------------------------------
# ① 引数と環境の種類
# ---------------------------------------------------------------------------
DIR="$(pwd)"
API_URL="https://api.openai.com/v1/models"
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --dir)
      [ $# -ge 2 ] || die_usage "--dir には導入先フォルダを続けてください。"
      case "$2" in -*|"") die_usage "--dir に渡す値が不正です: '$2'" ;; esac
      DIR="$2"; shift 2 ;;
    --dir=*) DIR="${1#--dir=}"; [ -n "${DIR}" ] || die_usage "--dir に渡す値が空です。"; shift ;;
    --api-url)
      [ $# -ge 2 ] || die_usage "--api-url には URL を続けてください。"
      API_URL="$2"; shift 2 ;;
    --api-url=*) API_URL="${1#--api-url=}"; shift ;;
    *) die_usage "不明なオプションです: $1" ;;
  esac
done
case "${API_URL}" in
  http://?*|https://?*) ;;
  *) die_usage "--api-url は http:// か https:// で始まる URL にしてください: '${API_URL}'" ;;
esac

UNAME_S="$(uname -s)"
UNAME_M="$(uname -m)"
UNAME_R="$(uname -r)"
OS_KIND="other"
case "${UNAME_S}" in
  Darwin) OS_KIND="macos" ;;
  Linux)
    OS_KIND="linux"
    if [ -n "${WSL_DISTRO_NAME:-}" ] || [ -n "${WSL_INTEROP:-}" ] || grep -qi microsoft /proc/version 2>/dev/null; then
      case "${UNAME_R}" in
        *Microsoft*) OS_KIND="wsl1" ;;
        *) OS_KIND="wsl2" ;;
      esac
    fi ;;
esac
IS_LINUX=0
case "${OS_KIND}" in linux|wsl2|wsl1) IS_LINUX=1 ;; esac

PLATFORM_KEY=""
case "${UNAME_S}:${UNAME_M}" in
  Linux:x86_64|Linux:amd64) PLATFORM_KEY="linux_x86_64" ;;
  Linux:aarch64|Linux:arm64) PLATFORM_KEY="linux_aarch64" ;;
  Darwin:arm64) PLATFORM_KEY="darwin_arm64" ;;
  Darwin:x86_64) PLATFORM_KEY="darwin_x86_64" ;;
esac

DIR_OK=0
DIR_ABS=""
if [ -d "${DIR}" ]; then
  DIR_ABS="$(cd "${DIR}" 2>/dev/null && pwd -P)"
  [ -n "${DIR_ABS}" ] && DIR_OK=1
fi
INSTALLED=0
if [ "${DIR_OK}" = 1 ] && { [ -f "${DIR_ABS}/Sherpa/data/.installed" ] || [ -f "${DIR_ABS}/data/.installed" ]; }; then INSTALLED=1; fi

echo "=== Sherpa 導入前の確認（読み取りだけ・何も入れません） ==="
echo "表示の見かた:"
echo "  OK:                              問題ありません"
echo "  ・ パッケージが入れます           導入のときに自動で入ります（何もしなくてよい）"
echo "  ✗ 自分で用意してください          導入の前に、あなた（または管理者）が用意するものです"
echo "  ⓘ  注意                          動きますが、確かめてほしいことです"

finish() {
  echo ""
  echo "=== 結果: 自分で用意するもの ${N_MUST} 件・注意 ${N_WARN} 件 ==="
  if [ "${N_MUST}" = 0 ]; then
    echo "このまま導入に進めます（注意の項目には先に目を通してください）。次は INSTALL.md の「導入」の 1 です。"
    exit 0
  fi
  echo "✗ の項目を用意してから、もう一度このスクリプトを流してください。"
  exit 1
}

sec "この環境の種類"
case "${OS_KIND}" in
  macos)
    r_ok "macOS $(sw_vers -productVersion 2>/dev/null || echo '（版を読めません）')" ;;
  wsl1)
    r_must "この環境は対象外です（WSL1 です）。" \
      "WSL2 に切り替えてください（PowerShell で wsl -l -v で確認し、wsl --set-version <ディストリ名> 2）。手順書: INSTALL.md"
    finish ;;
  linux|wsl2)
    OS_ID=""; OS_VER=""
    if [ -r /etc/os-release ]; then
      OS_ID="$(. /etc/os-release 2>/dev/null; printf '%s' "${ID:-}")"
      OS_VER="$(. /etc/os-release 2>/dev/null; printf '%s' "${VERSION_ID:-}")"
    fi
    OS_NAME="Linux"
    [ "${OS_KIND}" = wsl2 ] && OS_NAME="WSL2 上の Linux"
    if [ "${OS_ID}" != ubuntu ]; then
      r_must "この環境は対象外です（${OS_NAME}・${OS_ID:-種類を読めません}）。" \
        "Ubuntu 24.04 の機械（または WSL2 の Ubuntu 24.04）を用意してください。手順書: docs/manual/offline-kit.md"
      finish
    elif [ "${OS_VER}" != "24.04" ]; then
      r_must "${OS_NAME}の Ubuntu ${OS_VER} です。パッケージの資材は Ubuntu 24.04 用で、違うと導入が途中で止まります。" \
        "Ubuntu 24.04 の機械（または WSL2 の Ubuntu 24.04）を用意してください。手順書: docs/manual/offline-kit.md"
    else
      r_ok "${OS_NAME}（Ubuntu 24.04）"
    fi ;;
  *)
    r_must "この環境は対象外です（${UNAME_S}）。" "Ubuntu 24.04・WSL2・macOS のいずれかを用意してください。手順書: INSTALL.md"
    finish ;;
esac

case "${UNAME_S}:${UNAME_M}" in
  Linux:x86_64|Linux:amd64) r_ok "CPU の種類: x86_64" ;;
  Darwin:arm64|Darwin:x86_64) r_ok "CPU の種類: ${UNAME_M}" ;;
  *) r_must "CPU の種類（${UNAME_M}）はパッケージが対応していません。" "Linux は x86_64、macOS は arm64 または x86_64 の機械を用意してください。" ;;
esac
NCPU=""
if have nproc; then NCPU="$(nproc 2>/dev/null)"; fi
[ -n "${NCPU}" ] || NCPU="$(sysctl -n hw.ncpu 2>/dev/null || getconf _NPROCESSORS_ONLN 2>/dev/null || echo "")"
case "${NCPU}" in
  ""|*[!0-9]*) r_info "CPU の数: 読み取れませんでした" ;;
  *)
    if [ "${NCPU}" -lt 4 ]; then
      r_warn "CPU の数が ${NCPU} 個です（4 個以上を推奨）。動きますが、取り込みや検索が遅くなります。"
    else
      r_ok "CPU の数: ${NCPU} 個"
    fi ;;
esac

# ---------------------------------------------------------------------------
# ② パッケージの情報（版の照合に使う）
# ---------------------------------------------------------------------------
PKG_TEXT=""
PKG_FROM=""
APP_ONLY_ARCHIVE=""
pkg_get() { printf '%s\n' "${PKG_TEXT}" | grep -m1 "^$1=" | cut -d= -f2-; }

read_package() {
  local f newest="" cand tarflags=""
  [ "${DIR_OK}" = 1 ] || return 0
  for cand in "${DIR_ABS}/PACKAGE-INFO" "${DIR_ABS}/Sherpa/PACKAGE-INFO"; do
    if [ -f "${cand}" ]; then
      PKG_TEXT="$(cat "${cand}" 2>/dev/null)"
      PKG_FROM="${cand}"
      return 0
    fi
  done
  for f in "${DIR_ABS}"/sherpa-*-full-*.tar.gz; do
    [ -f "${f}" ] || continue
    if [ -z "${newest}" ] || [ "${f}" -nt "${newest}" ]; then newest="${f}"; fi
  done
  if [ -n "${newest}" ]; then
    if tar --version 2>/dev/null | grep -q "GNU tar"; then tarflags="--occurrence=1"; fi
    tlimit 20 tar -xzOf "${newest}" ${tarflags} Sherpa/PACKAGE-INFO
    if [ "${TL_RC}" = 0 ] && printf '%s\n' "${TL_OUT}" | grep -q "^kind="; then
      PKG_TEXT="${TL_OUT}"
      PKG_FROM="${newest}"
      return 0
    fi
  fi
  for f in "${DIR_ABS}"/sherpa-*-app-*.tar.gz; do
    [ -f "${f}" ] && APP_ONLY_ARCHIVE="${f}"
  done
  return 0
}

sec "パッケージ"
PKG_KIND=""
read_package
if [ -n "${PKG_TEXT}" ]; then
  PKG_KIND="$(pkg_get kind)"
  PKG_VER="$(pkg_get version)"
  PKG_COMMIT="$(pkg_get commit)"
  case "${PKG_KIND}" in
    full) PKG_KIND_JA="フル" ;;
    app) PKG_KIND_JA="アプリだけ" ;;
    *) PKG_KIND_JA="種類不明" ;;
  esac
  r_ok "パッケージ: ${PKG_KIND_JA}・版 ${PKG_VER:-不明}・コミット ${PKG_COMMIT:-不明}（読んだ場所: ${PKG_FROM}）"
  if [ -n "${PLATFORM_KEY}" ] && ! printf '%s\n' "${PKG_TEXT}" | grep -q "^fp\.${PLATFORM_KEY}\."; then
    r_must "このパッケージはこの環境（${PLATFORM_KEY}）に対応していません。" "この環境に対応したパッケージを使ってください。手順書: INSTALL.md「パッケージの種類」"
  fi
  if [ "${PKG_KIND}" = app ] && [ "${INSTALLED}" = 0 ]; then
    r_must "アプリだけのパッケージです。初回導入にはフルのパッケージが必要です。" "フルのパッケージ（sherpa-<版>-full-<コミット>.tar.gz）を使ってください。手順書: INSTALL.md「パッケージの種類」"
  fi
elif [ -n "${APP_ONLY_ARCHIVE}" ] && [ "${INSTALLED}" = 0 ]; then
  r_must "アプリだけのパッケージしか見つかりません（${APP_ONLY_ARCHIVE}）。初回導入にはフルのパッケージが必要です。" \
    "フルのパッケージ（sherpa-<版>-full-<コミット>.tar.gz）を同じフォルダに置いてください。手順書: INSTALL.md「パッケージの種類」"
else
  r_warn "パッケージが見つからないので、版の照合は省きます。" \
    "導入先のフォルダ（--dir）に PACKAGE-INFO か sherpa-*-full-*.tar.gz を置いてから流すと、Python の版なども照合できます。"
fi

# ---------------------------------------------------------------------------
# ③ 資源・導入先・ユーザー・道具
# ---------------------------------------------------------------------------
# docker の状態を一度だけ調べる（メモリ・ディスク・Docker の各項目で使う）。
DK_STATE="none"    # none=未導入／up=使える／denied=権限なし／down=デーモンに繋がらない／timeout=応答なし
DK_VER=""; DK_ROOT=""; DK_MEM_MB=""; DK_OS=""; DK_PATH=""; DK_COMPOSE="unknown"
probe_docker() {
  local line a b c d
  have docker || return 0
  DK_PATH="$(command -v docker)"
  tlimit 10 docker info --format '{{.ServerVersion}}|{{.DockerRootDir}}|{{.MemTotal}}|{{.OperatingSystem}}'
  if [ "${TL_RC}" = 0 ]; then
    DK_STATE="up"
    line="$(printf '%s\n' "${TL_OUT}" | tail -n 1)"
    IFS='|' read -r a b c d <<EOF
${line}
EOF
    DK_VER="${a}"; DK_ROOT="${b}"; DK_OS="${d}"
    case "${c}" in ""|*[!0-9]*) ;; *) DK_MEM_MB=$((c / 1048576)) ;; esac
  elif [ "${TL_RC}" = 124 ]; then
    DK_STATE="timeout"
  elif printf '%s\n' "${TL_OUT}" | grep -q "could not be found in this WSL"; then
    DK_STATE="none"
  elif printf '%s\n' "${TL_OUT}" | grep -qi "permission denied"; then
    DK_STATE="denied"
  else
    DK_STATE="down"
  fi
  tlimit 10 docker compose version
  if [ "${TL_RC}" = 0 ]; then DK_COMPOSE="yes"; else DK_COMPOSE="no"; fi
}
probe_docker

sec "メモリとディスク"
MEM_MB=""
if [ "${IS_LINUX}" = 1 ]; then
  MEM_MB="$(awk '/^MemTotal:/ { print int($2 / 1024) }' /proc/meminfo 2>/dev/null)"
else
  mem_bytes="$(sysctl -n hw.memsize 2>/dev/null)"
  case "${mem_bytes}" in ""|*[!0-9]*) ;; *) MEM_MB=$((mem_bytes / 1048576)) ;; esac
fi
# 表示される合計は物理メモリより数 % 小さいため、8GB・16GB の境目に余裕を持たせる。
MEM_MUST_MB=7500
MEM_WARN_MB=15500
case "${MEM_MB}" in
  ""|*[!0-9]*) r_info "メモリ: 読み取れませんでした" ;;
  *)
    MEM_JUDGE_MB="${MEM_MB}"
    MEM_TEXT="約 $(gb1 "${MEM_MB}") GB"
    MEM_HINT="docs/manual/40-運用.md「メモリの割り振り」"
    if [ "${OS_KIND}" = wsl2 ]; then
      MEM_TEXT="WSL に割り当てられた量は約 $(gb1 "${MEM_MB}") GB"
      MEM_HINT="C:\\Users\\<ユーザー名>\\.wslconfig の [wsl2] に memory=16GB のように書き、PowerShell で wsl --shutdown して WSL を起こし直すと増やせます。${MEM_HINT}"
    fi
    if [ "${OS_KIND}" = macos ] && [ -n "${DK_MEM_MB}" ]; then
      MEM_TEXT="Mac 本体 約 $(gb1 "${MEM_MB}") GB・Docker Desktop に割り当てた量 約 $(gb1 "${DK_MEM_MB}") GB（小さい方で判定）"
      if [ "${DK_MEM_MB}" -lt "${MEM_JUDGE_MB}" ]; then MEM_JUDGE_MB="${DK_MEM_MB}"; fi
      MEM_HINT="Docker Desktop の設定（Resources）で割り当てを増やせます。${MEM_HINT}"
    fi
    if [ "${MEM_JUDGE_MB}" -lt "${MEM_MUST_MB}" ]; then
      r_must "メモリが足りません（${MEM_TEXT}・8GB 以上が必要）。" "${MEM_HINT}"
    elif [ "${MEM_JUDGE_MB}" -lt "${MEM_WARN_MB}" ]; then
      r_warn "メモリが少なめです（${MEM_TEXT}・16GB 以上を推奨）。" \
        "画像内の文字の読み取り（OCR）は重いので、動きが悪いときは .env に SHERPA_OCR_ENABLED=0 と書いて OCR を止めてください。${MEM_HINT}"
    else
      r_ok "メモリ: ${MEM_TEXT}"
    fi ;;
esac

fs_free_mb() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print int($4 / 1024) }'; }
fs_id() { df -Pk "$1" 2>/dev/null | awk 'NR == 2 { print $1 "|" $6 }'; }
existing_ancestor() {
  local p="$1"
  while [ -n "${p}" ] && [ ! -e "${p}" ] && [ "${p}" != "/" ]; do p="$(dirname "${p}")"; done
  printf '%s' "${p}"
}

if [ "${DIR_OK}" = 1 ]; then
  FREE_MB="$(fs_free_mb "${DIR_ABS}")"
  case "${FREE_MB}" in
    ""|*[!0-9]*) r_info "ディスクの空き: 読み取れませんでした（${DIR_ABS}）" ;;
    *)
      if [ "${IS_LINUX}" = 1 ]; then
        if [ "${FREE_MB}" -lt 30720 ]; then
          r_must "ディスクの空きが足りません（${DIR_ABS} に約 $(gb1 "${FREE_MB}") GB・30GB 以上が必要）。" \
            "空きを作るか、空きの多い場所を --dir に指定してください（資材が約 15GB、展開後や Python の環境などで 30GB 前後を使います）。"
        else
          r_ok "ディスクの空き: 約 $(gb1 "${FREE_MB}") GB（${DIR_ABS}）"
        fi
      else
        if [ "${FREE_MB}" -lt 15360 ]; then
          r_warn "ディスクの空きが少なめです（${DIR_ABS} に約 $(gb1 "${FREE_MB}") GB）。15GB 以上あると安心です。" \
            "Docker のイメージだけで約 6GB（検索 1.6GB・OCR 2.6GB・DB 1.7GB を実測）、これに Python の環境・Chromium・LibreOffice が加わり、資料の取り込み結果は資料の量に応じて増えます。"
        else
          r_ok "ディスクの空き: 約 $(gb1 "${FREE_MB}") GB（${DIR_ABS}）"
        fi
      fi
      if [ "${IS_LINUX}" = 1 ]; then
        DK_PLACE="${DK_ROOT:-/var/lib}"
        DK_REAL="$(existing_ancestor "${DK_PLACE}")"
        if [ -n "${DK_REAL}" ] && [ "$(fs_id "${DK_REAL}")" != "$(fs_id "${DIR_ABS}")" ]; then
          DK_FREE_MB="$(fs_free_mb "${DK_REAL}")"
          case "${DK_FREE_MB}" in
            ""|*[!0-9]*) ;;
            *)
              if [ "${DK_FREE_MB}" -lt 30720 ]; then
                r_warn "Docker のデータ置き場（${DK_PLACE}）は別の場所で、空きは約 $(gb1 "${DK_FREE_MB}") GB です。" \
                  "Docker のイメージ（約 数 GB）がここに入ります。30GB 以上あると安心です。"
              else
                r_ok "Docker のデータ置き場（${DK_PLACE}）の空き: 約 $(gb1 "${DK_FREE_MB}") GB"
              fi ;;
          esac
        fi
      fi ;;
  esac
fi

sec "導入先のフォルダ"
if [ "${DIR_OK}" = 0 ]; then
  r_must "導入先のフォルダが見つかりません（${DIR}）。" "パッケージを置いたフォルダを --dir に指定してください。"
else
  if [ -w "${DIR_ABS}" ] && [ -x "${DIR_ABS}" ]; then
    r_ok "書き込めます: ${DIR_ABS}"
  else
    r_must "このフォルダに書き込めません（${DIR_ABS}）。" "自分のホームの下など、書き込めるフォルダを使ってください。"
  fi
  if [ "${IS_LINUX}" = 1 ] && [ -r /proc/mounts ]; then
    MOUNT_OPTS="$(awk -v p="${DIR_ABS}" '
      { mp = $2; gsub(/\\040/, " ", mp)
        if (mp == "/" || p == mp || index(p, mp "/") == 1) { if (length(mp) >= best) { best = length(mp); opts = $4 } } }
      END { print opts }' /proc/mounts 2>/dev/null)"
    case ",${MOUNT_OPTS}," in
      *,noexec,*) r_must "このフォルダは noexec でマウントされていて、プログラムを動かせません（${DIR_ABS}）。" "noexec でない場所（自分のホームの下など）を使ってください。" ;;
    esac
  fi
  if [ "${OS_KIND}" = wsl2 ]; then
    case "${DIR_ABS}" in
      /mnt/[a-zA-Z]|/mnt/[a-zA-Z]/*)
        r_warn "Windows 側のフォルダ（${DIR_ABS}）です。遅く、ファイルの権限も正しく扱えません。" \
          "WSL の中（例: ホームの下）に置き直すことをおすすめします。" ;;
    esac
  fi
  case "${DIR_ABS}" in
    *" "*) r_warn "フォルダの名前に空白が含まれています（${DIR_ABS}）。" "動かないスクリプトが出ることがあります。空白のない場所に置くことをおすすめします。" ;;
  esac
fi

sec "実行するユーザー"
if [ "$(id -u)" = 0 ]; then
  if [ "${OS_KIND}" = macos ]; then
    r_must "root で実行しています。" "一般ユーザーで実行してください（Homebrew は root では動きません）。"
  else
    r_must "root で実行しています。" "一般ユーザーで入れてください（導入の資材スクリプトは root を拒否します）。手順書: docs/manual/offline-kit.md「閉域側: セットアップ手順」"
  fi
else
  r_ok "一般ユーザーです（$(id -un)）"
  GROUPS_NOW=" $(id -Gn 2>/dev/null) "
  if [ "${OS_KIND}" = macos ]; then
    case "${GROUPS_NOW}" in
      *" admin "*) r_ok "管理者（admin グループ）です。Homebrew で入れるときに必要になります。" ;;
      *) r_warn "管理者（admin グループ）ではありません。" "LibreOffice などを Homebrew で入れるとき、管理者のパスワードが要ります。管理者に相談してください。" ;;
    esac
  else
    case "${GROUPS_NOW}" in
      *" sudo "*|*" admin "*) r_ok "sudo が使えそうです（sudo グループ）。導入の途中でパスワードを聞かれます。" ;;
      *) r_warn "sudo グループに入っていません。sudo が使えるか分かりません。" "導入は途中で sudo を使います（Docker・Python の実行系などを入れるため）。使えない場合は管理者に相談してください。" ;;
    esac
  fi
fi

sec "導入の前から必要な道具"
MISSING_BASE=""
SUM_TOOL="sha256sum"
if [ "${OS_KIND}" = macos ]; then
  SUM_TOOL="shasum"
  for t in tar gzip shasum curl; do have "${t}" || MISSING_BASE="${MISSING_BASE} ${t}"; done
else
  for t in tar gzip sha256sum; do have "${t}" || MISSING_BASE="${MISSING_BASE} ${t}"; done
fi
if [ -n "${MISSING_BASE}" ]; then
  r_must "次のコマンドがありません:${MISSING_BASE}" "展開と照合に使います。管理者に入れてもらってください。"
else
  r_ok "展開と照合の道具（tar・gzip・${SUM_TOOL}）があります。"
fi
if [ "${IS_LINUX}" = 1 ]; then
  MISSING_PKG=""
  have python3 || MISSING_PKG="${MISSING_PKG} python3"
  if have dpkg-query; then
    for p in python3-venv python3-pip; do
      dpkg-query -W -f='${Status}' "${p}" 2>/dev/null | grep -q "install ok installed" || MISSING_PKG="${MISSING_PKG} ${p}"
    done
  fi
  have make || MISSING_PKG="${MISSING_PKG} make"
  have curl || MISSING_PKG="${MISSING_PKG} curl"
  have xz || MISSING_PKG="${MISSING_PKG} xz-utils"
  have unzip || MISSING_PKG="${MISSING_PKG} unzip"
  have fc-cache || MISSING_PKG="${MISSING_PKG} fontconfig"
  if [ -n "${MISSING_PKG}" ]; then
    r_auto "まだ入っていない道具:${MISSING_PKG}（導入のときにパッケージの資材から入ります）"
  else
    r_ok "基本の道具（python3・venv・pip・make・curl・xz・unzip・fontconfig）があります。"
  fi
  if have python3 && [ -n "${PKG_TEXT}" ] && [ -n "${PLATFORM_KEY}" ]; then
    PY_WANT="$(pkg_get "fp.${PLATFORM_KEY}.python")"
    tlimit 10 python3 -c "import sys;print('%d.%d/%s' % (sys.version_info[0], sys.version_info[1], sys.implementation.cache_tag))"
    PY_HAVE="${TL_OUT}"
    if [ -n "${PY_WANT}" ] && [ "${TL_RC}" = 0 ] && [ "${PY_HAVE}" != "${PY_WANT}" ]; then
      r_must "今ある python3 は ${PY_HAVE} で、パッケージが期待する ${PY_WANT} と違います。" \
        "合う版の Python を入れ、PYTHON_BIN=python${PY_WANT%%/*} ./install.sh のように指定してください（Ubuntu 24.04 の標準は 3.12 です）。"
    fi
  fi
  r_auto "Python の依存・Node.js・marp・Chromium・LibreOffice・日本語フォント・Codex・OCR（./install.sh が資材から入れます）"
fi

# ---------------------------------------------------------------------------
# ④ Docker・systemd・各種設定
# ---------------------------------------------------------------------------
sec "Docker"
if [ "${IS_LINUX}" = 1 ]; then
  if [ "${DK_STATE}" = none ]; then
    r_auto "Docker（Docker Engine と docker compose）はまだ入っていません。導入のときに入り、あなたを docker グループに加えます（導入のあと、一度ログインし直してください）。"
  else
    case "${DK_STATE}" in
      up) r_ok "Docker ${DK_VER:-}（動いています）" ;;
      denied)
        case " $(id -Gn 2>/dev/null) " in
          *" docker "*) r_warn "docker グループには入っていますが、このログインにはまだ反映されていません。" "ログインし直してください（WSL は wsl --shutdown のあと開き直します）。" ;;
          *) r_must "docker を使う権限がありません（docker グループに入っていません）。" "sudo usermod -aG docker \"\$USER\" を実行し、ログインし直してください。" ;;
        esac ;;
      timeout) r_must "docker が 10 秒待っても応答しません。" "sudo systemctl status docker で状態を確かめ、動かしてください。" ;;
      *) r_must "docker は入っていますが、動いていません（デーモンに繋がりません）。" "sudo systemctl start docker で起動してください（WSL は先に systemd を有効にします。次の項目を参照）。" ;;
    esac
    if [ "${DK_COMPOSE}" = no ]; then
      r_must "docker compose（Docker Compose のプラグイン）がありません。" "docker compose が使える Docker に入れ替えるか、プラグインを追加してください。"
    elif [ "${DK_STATE}" = up ]; then
      r_ok "docker compose があります。"
    fi
    if [ "${OS_KIND}" = wsl2 ]; then
      DK_REAL_PATH="$(readlink -f "${DK_PATH}" 2>/dev/null || echo "${DK_PATH}")"
      case "${DK_REAL_PATH}:${DK_OS}" in
        *docker-desktop*|/mnt/*|*"Docker Desktop"*)
          r_warn "WSL に Docker Desktop の連携が見えています（${DK_REAL_PATH}）。WSL の中の Docker Engine と二重になります。" \
            "Docker Desktop の WSL 連携を切るか、使う Docker を一つに決めてください。" ;;
      esac
    fi
  fi
else
  MAC_DOCKER_APP=0
  if [ -d /Applications/Docker.app ] || [ -d "${HOME}/Applications/Docker.app" ]; then MAC_DOCKER_APP=1; fi
  if [ "${DK_STATE}" = none ] && [ "${MAC_DOCKER_APP}" = 0 ]; then
    r_must "Docker Desktop が入っていません。" "Docker Desktop を入れて起動してください（導入では入りません）。手順書: INSTALL.md「パッケージの種類」"
  elif [ "${DK_STATE}" = none ]; then
    r_must "Docker Desktop は入っていますが、まだ起動していません。" "Docker Desktop を起動し、メニューバーのアイコンが落ち着くまで待ってください。"
  elif [ "${DK_STATE}" = up ]; then
    r_ok "Docker Desktop が動いています（${DK_VER:-}）"
    if [ "${DK_COMPOSE}" = no ]; then r_must "docker compose がありません。" "Docker Desktop を入れ直すか更新してください。"; fi
  else
    r_must "Docker Desktop が動いていません（docker に繋がりません）。" "Docker Desktop を起動し、メニューバーのアイコンが落ち着くまで待ってください。"
  fi
fi

if [ "${IS_LINUX}" = 1 ]; then
  sec "systemd・カーネルの設定・Codex のサンドボックス"
  if [ "$(cat /proc/1/comm 2>/dev/null)" = systemd ]; then
    r_ok "systemd が動いています。"
  elif [ "${OS_KIND}" = wsl2 ]; then
    r_must "WSL で systemd が有効になっていません（Docker Engine の導入に要ります）。" \
      "/etc/wsl.conf に [boot] と systemd=true を書き、PowerShell で wsl --shutdown して開き直してください。"
  else
    r_must "systemd が動いていません（Docker Engine の導入に要ります）。" "systemd で動く Ubuntu 24.04 を使ってください。"
  fi

  MAP_COUNT="$(cat /proc/sys/vm/max_map_count 2>/dev/null)"
  case "${MAP_COUNT}" in
    ""|*[!0-9]*) r_info "vm.max_map_count: 読み取れませんでした" ;;
    *)
      if [ "${MAP_COUNT}" -lt 262144 ]; then
        r_warn "vm.max_map_count が ${MAP_COUNT} です（検索の Elasticsearch には 262144 以上が要ります）。" \
          "導入のあと INSTALL.md の「確認して起動します」で値を確かめ、足りないときだけ docs/manual/offline-kit.md の「vm.max_map_count」の手順で直します（sudo が要ります）。"
      else
        r_ok "vm.max_map_count: ${MAP_COUNT}（262144 以上）"
      fi ;;
  esac

  USERNS_VALUE="$(cat /proc/sys/kernel/apparmor_restrict_unprivileged_userns 2>/dev/null)"
  if have unshare; then
    tlimit 5 unshare -Urn true
    if [ "${TL_RC}" = 0 ]; then
      r_ok "Codex のサンドボックス（ユーザー名前空間）が使えます。"
    elif [ "${USERNS_VALUE}" = 1 ]; then
      r_warn "このままでは Codex のサンドボックスを起動できません（AppArmor がユーザー名前空間を制限しています）。" \
        "展開したあと、sudo bash scripts/setup-codex-sandbox.sh apply で直せます。"
    else
      r_warn "ユーザー名前空間を作れません。原因は AppArmor の制限以外かもしれません。" \
        "展開したあと、scripts/setup-codex-sandbox.sh check で詳しく確かめられます。"
    fi
  else
    r_info "Codex のサンドボックス: unshare が無いため確かめられませんでした"
  fi
fi

# ---------------------------------------------------------------------------
# ⑤ ポート・文字コード
# ---------------------------------------------------------------------------
sec "ポートの空き"
# 使用中なら、使っているものの名前（分かれば）を出す。何も使っていなければ空。
port_user() {
  local port="$1" out=""
  if [ "${OS_KIND}" = macos ]; then
    if have lsof; then
      out="$(lsof +c 0 -nP -iTCP:"${port}" -sTCP:LISTEN 2>/dev/null | awk 'NR == 2 { print $1 }')"
      [ -n "${out}" ] && { printf '%s' "${out}"; return 0; }
    fi
    if have netstat && netstat -an -p tcp 2>/dev/null | awk -v p="${port}" '$NF == "LISTEN" && $4 ~ ("[.:]" p "$") { f = 1 } END { exit !f }'; then
      printf '%s' "不明"
    fi
  else
    if have ss; then
      ss -ltn 2>/dev/null | awk -v p="${port}" 'NR > 1 && $4 ~ (":" p "$") { f = 1 } END { exit !f }' && printf '%s' "使用中"
    elif have netstat; then
      netstat -ltn 2>/dev/null | awk -v p="${port}" 'NR > 2 && $4 ~ (":" p "$") { f = 1 } END { exit !f }' && printf '%s' "使用中"
    fi
  fi
  return 0
}
if [ "${IS_LINUX}" = 1 ] && ! have ss && ! have netstat; then
  r_warn "ss も netstat も無いため、ポートの空きを確かめられません。" "導入のあと make check-ports で確かめられます。"
elif [ "${OS_KIND}" = macos ] && ! have lsof && ! have netstat; then
  r_warn "lsof も netstat も無いため、ポートの空きを確かめられません。" "導入のあと make check-ports で確かめられます。"
else
  PORT_BUSY=""
  PORT_LIST=""
  for spec in SHERPA_PORT:8000:画面 PGPORT:5432:データベース SHERPA_ES_PORT:9200:検索 SHERPA_NEO4J_BOLT_PORT:7687:グラフ SHERPA_NEO4J_HTTP_PORT:7474:グラフの管理; do
    pname="${spec%%:*}"; rest="${spec#*:}"; pdef="${rest%%:*}"; plabel="${rest#*:}"
    pval="${!pname:-${pdef}}"
    case "${pval}" in ""|*[!0-9]*) pval="${pdef}" ;; esac
    PORT_LIST="${PORT_LIST} ${pval}"
    who="$(port_user "${pval}")"
    if [ -n "${who}" ]; then PORT_BUSY="${PORT_BUSY} ${pval}（${plabel}・${who}）"; fi
  done
  if [ -n "${PORT_BUSY}" ]; then
    r_warn "使われているポートがあります:${PORT_BUSY}" \
      "すでに Sherpa が動いている更新なら正常です。別のものが使っているなら、.env でポートを変えてください（SHERPA_PORT など）。手順書: INSTALL.md「確認して起動します」"
  else
    r_ok "必要なポート（${PORT_LIST# }）は空いています。"
  fi
fi

sec "文字コード"
CHARMAP="$(locale charmap 2>/dev/null)"
case "${CHARMAP}" in
  UTF-8|utf-8|UTF8|utf8) r_ok "文字コードは UTF-8 です。" ;;
  *) r_warn "文字コードが UTF-8 ではありません（${CHARMAP:-不明}）。日本語が文字化けすることがあります。" "export LANG=C.UTF-8（または ja_JP.UTF-8）を設定してから流し直してください。" ;;
esac

# ---------------------------------------------------------------------------
# ⑥ macOS 固有（Homebrew・Python）
# ---------------------------------------------------------------------------
BREW_BIN=""
CASKS_MISSING=""
if [ "${OS_KIND}" = macos ]; then
  sec "Homebrew と Python（macOS は導入がオンラインで準備します）"
  BREW_BIN="$(command -v brew 2>/dev/null || true)"
  if [ -z "${BREW_BIN}" ]; then
    for cand in /opt/homebrew/bin/brew /usr/local/bin/brew; do
      if [ -x "${cand}" ]; then BREW_BIN="${cand}"; break; fi
    done
  fi
  if [ -z "${BREW_BIN}" ]; then
    r_must "Homebrew が入っていません。" "https://brew.sh の手順で入れてください（LibreOffice と日本語フォントを入れるのに使います）。手順書: INSTALL.md「パッケージの種類」"
  else
    r_ok "Homebrew があります（${BREW_BIN}）"
    export HOMEBREW_NO_AUTO_UPDATE=1 HOMEBREW_NO_ANALYTICS=1 HOMEBREW_NO_ENV_HINTS=1
    tlimit 20 "${BREW_BIN}" list --cask
    for cask in libreoffice font-noto-sans-cjk-jp font-hackgen; do
      printf '%s\n' "${TL_OUT}" | grep -qx "${cask}" || CASKS_MISSING="${CASKS_MISSING} ${cask}"
    done
    if [ "${TL_RC}" != 0 ]; then
      r_warn "Homebrew の入れ済みの一覧を読めませんでした。" "brew list --cask が動くか確かめてください。"
      CASKS_MISSING=" libreoffice font-noto-sans-cjk-jp font-hackgen"
    elif [ -z "${CASKS_MISSING}" ]; then
      r_ok "LibreOffice と日本語フォントは入っています。"
    else
      r_auto "LibreOffice・日本語フォントのうち、まだ入っていないもの:${CASKS_MISSING}（./install.sh が Homebrew で入れます）"
    fi
  fi
  r_auto "Python の環境（.venv）・Codex・Node.js・marp・Chromium（./install.sh がオンラインで準備します）"
  r_info "入りません: Docker Desktop（自分で入れる）・OCR のモデル（使うときだけ make ocr-models）"

  # Python: パッケージが期待する版を、./install.sh と同じ式で照合する。
  PY_BIN=""
  if [ -n "${PYTHON_BIN:-}" ]; then PY_BIN="$(command -v "${PYTHON_BIN}" 2>/dev/null || true)"; else PY_BIN="$(command -v python3 2>/dev/null || true)"; fi
  if [ "${PY_BIN}" = /usr/bin/python3 ] && ! xcode-select -p >/dev/null 2>&1; then PY_BIN=""; fi
  PY_WANT=""
  [ -n "${PKG_TEXT}" ] && [ -n "${PLATFORM_KEY}" ] && PY_WANT="$(pkg_get "fp.${PLATFORM_KEY}.python")"
  PY_WANT_MINOR="${PY_WANT%%/*}"
  if [ -z "${PY_BIN}" ]; then
    r_must "Python（python3）が見つかりません${PY_WANT_MINOR:+（パッケージは ${PY_WANT_MINOR} を期待します）}。" \
      "brew install python@${PY_WANT_MINOR:-3.12} で入れるか、PYTHON_BIN に使う Python を指定してください。"
  else
    tlimit 10 "${PY_BIN}" -c "import sys;print('%d.%d/%s' % (sys.version_info[0], sys.version_info[1], sys.implementation.cache_tag))"
    PY_HAVE="${TL_OUT}"
    if [ "${TL_RC}" != 0 ]; then
      r_must "Python（${PY_BIN}）を実行できません。" "brew install python@${PY_WANT_MINOR:-3.12} で入れ直すか、PYTHON_BIN に使う Python を指定してください。"
    elif [ -z "${PY_WANT}" ]; then
      r_warn "Python は ${PY_HAVE} です。パッケージの情報が無いため、期待する版との照合は省きます。" "パッケージを --dir に置いてから流し直すと照合できます。"
    elif [ "${PY_HAVE}" = "${PY_WANT}" ]; then
      r_ok "Python は ${PY_HAVE}（パッケージが期待する版）です。"
    else
      r_must "Python（${PY_BIN}）は ${PY_HAVE} で、パッケージが期待する ${PY_WANT} と違います。" \
        "brew install python@${PY_WANT_MINOR} で入れ、PYTHON_BIN=python${PY_WANT_MINOR} ./install.sh のように指定してください。"
    fi
  fi
fi

# ---------------------------------------------------------------------------
# ⑦ 通信（到達確認だけ・HTTP の状態コードを見る）
# ---------------------------------------------------------------------------
sec "通信"
NET_LIST=""
NET_COUNT=0
# 確かめる先を足す。$1=ラベル $2=URL $3=must（届かなければ自分で用意）か warn（注意）
# URL の中の認証情報（user:pass@）は外してから確かめ・表示する。
net_add() {
  local url
  url="$(printf '%s' "$2" | sed -E 's#^([A-Za-z][A-Za-z0-9+.-]*://)[^/@]*@#\1#')"
  NET_LIST="${NET_LIST}$3|$1|${url}
"
  NET_COUNT=$((NET_COUNT + 1))
}
net_add "推論の接続先" "${API_URL}" must

if [ "${OS_KIND}" = macos ]; then
  if [ "${SHERPA_PRECHECK_SKIP_ONLINE:-0}" = 1 ]; then
    r_warn "導入で取りに行く先への到達確認を省きました（SHERPA_PRECHECK_SKIP_ONLINE=1）。" "本番の確認では、この環境変数を付けずに流してください。"
  else
    net_add "Node.js の配布元" "${SHERPA_NODE_DIST_BASE:-https://nodejs.org/dist}/" must
    if [ -n "${PIP_INDEX_URL:-}" ]; then
      net_add "Python パッケージの配布元（PIP_INDEX_URL）" "${PIP_INDEX_URL%/}/" must
    else
      net_add "Python パッケージの配布元（PyPI）" "https://pypi.org/simple/pip/" must
      net_add "Python パッケージのファイル置き場" "https://files.pythonhosted.org/" must
    fi
    net_add "npm の配布元（marp）" "${npm_config_registry:-${NPM_CONFIG_REGISTRY:-https://registry.npmjs.org}}/" must
    net_add "Chromium の配布元（Playwright）" "${PLAYWRIGHT_DOWNLOAD_HOST:-https://cdn.playwright.dev}/" must
    net_add "Codex の配布元（GitHub）" "${CODEX_PIN_BASE_URL:-https://github.com/openai/codex/releases/download}/" must
    net_add "Docker のイメージ置き場（Docker Hub）" "https://registry-1.docker.io/v2/" must
    net_add "Docker のイメージ置き場（Elastic）" "https://docker.elastic.co/v2/" must
    net_add "OCR のイメージづくりの配布元（Debian）" "https://deb.debian.org/debian/" warn
    if [ -n "${BREW_BIN}" ] && [ -n "${CASKS_MISSING}" ]; then
      net_add "Homebrew の情報" "${HOMEBREW_API_DOMAIN:-https://formulae.brew.sh/api}/" must
      net_add "LibreOffice の取得先" "https://download.documentfoundation.org/" must
      net_add "日本語フォントの取得先（GitHub）" "https://github.com/" must
    fi
  fi
else
  r_info "Linux／WSL の導入に、外への通信は要りません（資材はパッケージに入っています）。推論の接続先（OpenAI など）へは、起動後に通信します。"
fi

# curl の結果コードから HTTP の応答があったかを見る（000＝届かない）。
# curl は -q を先頭に置き、~/.curlrc（認証ヘッダなど）を読ませない。
net_http_code() { curl -q -s -o /dev/null --connect-timeout 5 --max-time 6 -w '%{http_code}' "$1" 2>/dev/null; }
NET_CODES=""
if ! have curl; then
  r_warn "curl が無いため、通信の到達確認を省きます。" "導入のときに curl が入ります。そのあとで、このスクリプトを流し直すと確かめられます。"
else
  # 並列で一度に確かめる。curl が並列に対応していないときは、一件ずつ確かめる。
  set --
  while IFS='|' read -r kind label url; do
    [ -n "${url}" ] || continue
    set -- "$@" -o /dev/null "${url}"
  done <<EOF
${NET_LIST}
EOF
  NET_PAR="$(curl -q -s --parallel --parallel-max 16 --connect-timeout 5 --max-time 6 -w '%{urlnum} %{http_code}\n' "$@" 2>/dev/null)"
  if [ "$(printf '%s\n' "${NET_PAR}" | grep -c '^[0-9][0-9]* [0-9][0-9][0-9]$')" = "${NET_COUNT}" ]; then
    NET_CODES="${NET_PAR}"
  else
    idx=0
    while IFS='|' read -r kind label url; do
      [ -n "${url}" ] || continue
      NET_CODES="${NET_CODES}${idx} $(net_http_code "${url}")
"
      idx=$((idx + 1))
    done <<EOF
${NET_LIST}
EOF
  fi
  idx=0
  NET_FAILED=0
  while IFS='|' read -r kind label url; do
    [ -n "${url}" ] || continue
    code="$(printf '%s\n' "${NET_CODES}" | awk -v i="${idx}" '$1 == i { print $2; exit }')"
    idx=$((idx + 1))
    case "${code}" in
      ""|000)
        NET_FAILED=1
        if [ "${kind}" = must ]; then
          r_must "${label}（${url}）に届きません。" "ネットワーク担当に、この先への通信（https）を許可してもらってください。プロキシ経由なら https_proxy を設定してから流し直します。"
        else
          r_warn "${label}（${url}）に届きません。" "届かないと、この機能だけが使えません。必要ならネットワーク担当に相談してください。"
        fi ;;
      407)
        NET_FAILED=1
        if [ "${kind}" = must ]; then
          r_must "${label}（${url}）へはプロキシの認証が要るようです（状態 407）。" "プロキシの認証情報を設定してから流し直してください。"
        else
          r_warn "${label}（${url}）へはプロキシの認証が要るようです（状態 407）。" "プロキシの認証情報を設定してから流し直してください。"
        fi ;;
      *) r_ok "${label}に届きます（状態 ${code}）" ;;
    esac
  done <<EOF
${NET_LIST}
EOF
  if [ "${NET_FAILED}" = 1 ]; then
    r_info "推論の接続先を Azure OpenAI などに変える場合は --api-url <URL> で指定できます。外へ一切出ない構成（Ollama だけで使う）の人は、推論の接続先の項目を読み飛ばしてかまいません（docs/manual/offline-kit.md「前提」）。"
  fi
fi

# ---------------------------------------------------------------------------
# ⑧ 任意・既存の導入
# ---------------------------------------------------------------------------
sec "任意の部品と、すでにある導入"
if have ollama; then
  r_info "Ollama（サブの LLM・任意）: 入っています"
else
  r_info "Ollama（サブの LLM・任意）: 入っていません。パッケージにも入りませんが、無くても動きます。"
fi
if [ "${INSTALLED}" = 1 ]; then
  INSTALLED_VER=""
  for rec in "${DIR_ABS}/Sherpa/data/.installed" "${DIR_ABS}/data/.installed"; do
    if [ -f "${rec}" ]; then INSTALLED_VER="$(grep -m1 '^version=' "${rec}" | cut -d= -f2-)"; break; fi
  done
  r_info "すでに Sherpa が導入されています${INSTALLED_VER:+（版 ${INSTALLED_VER}）}。更新として扱われます。更新の前に make stop で止めてください。手順書: INSTALL.md「更新」"
else
  r_info "Sherpa はまだ導入されていません。初回の導入として扱われます。"
fi

finish
