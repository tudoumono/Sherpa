#!/usr/bin/env bash
# 出荷ゲート（M7・2026-08-18・閉域実機報告③への対応）: 収集したキットを「搬入先に近いホスト
# （インストール直後の Ubuntu Server 24.04 GA 相当）」へ、ネットワーク無しで実際に apt 導入してみて、
# 削除提案・カーネル消失・導入失敗が無いことを確かめる。
#
# なぜ要るか: 閉域実機報告①（base の閉包に ubuntu-server 系メタの上げ先が無く、apt が
# 「その群と ubuntu-server ごと削除」で辻褄を合わせようとした）は、コードレビューでは見えなかった。
# 「搬入先と同じくらい古いホスト＋ネットワーク無し」で実際に apt を走らせて初めて出た不具合であり、
# make_offline_kit.sh / install_offline_kit.sh の実装を直しただけでは再発を防げない。出荷前に必ず
# このゲート（make verify-kit）を通す。
#
# やること:
#   1. 検証用ホスト像（既定タグ sherpa-wftest-host:ga）が無ければオンラインで1回だけビルドする:
#      ubuntu:noble-20240423（24.04 GA 相当のビルド）に、apt の索引だけ snapshot.ubuntu.com の
#      2024-05-01 時点（GA 直後）へ固定して ubuntu-minimal/ubuntu-standard/ubuntu-server＋
#      linux-image-generic を recommends 込みで導入し（2026-08-18 RV MED-2: 実機の既定導入
#      （Install-Recommends=yes）に近づけるため。像は肥大するがこの検証の目的＝搬入先の導入済み
#      集合の近似に照らして忠実性を優先）、apt lists を空にする。ビルドした像には来歴 label
#      （レシピ版・ベースタグ・snapshot・導入パッケージ集合のハッシュ）を付け、既存の像が
#      あってもこの来歴が一致し、かつ前提パッケージ（実機 Ubuntu Server の導入済み集合の代表＝
#      libglib2.0-bin/packagekit/software-properties-common/ubuntu-server/linux-image-generic）が
#      揃っているときだけ再利用する（別物の同名イメージが黙って通るのを防ぐ・RV MED-2）。
#      不一致・欠落なら作り直す（SHERPA_WFTEST_REBUILD_HOST=1 で常に強制再構築も可）。
#   2. そのホスト像から --network none でコンテナを起動し、キット（引数・既定 dist/offline-kit）と
#      scripts/lib/apt_offline.sh を実物ごとマウントして、apt グループを repo モードで名前導入する。
#      導入経路は install_offline_kit.sh とまったく同じ apt_offline_install
#      （scripts/lib/apt_offline.sh）を使う＝別経路を作らない。既定では検証対象の5グループ
#      （Python実行系/Docker Engine/Chromiumシステム依存/LibreOffice/Noto Sans CJK JP）は全部必須で、
#      1つでも未収集なら NG（2026-08-18 RV HIGH-1: 欠けたキット・--skip-* で作った部分キットが
#      黙って出荷ゲートを通るのを防ぐ）。部分キットの検証だけは --allow-missing-groups で従来どおり
#      「未収集」表示のまま許容できる。
#   3. 1つでも導入失敗・削除提案・カーネル消失・（既定では）必須グループの未収集があれば非0。
#      docker が無い環境では「検証できません」と明示して非0にする（黙って成功にしない）。
#
# 使い方:
#   ./scripts/verify_offline_kit_apt.sh [キットのパス] [--allow-missing-groups]   # 既定: dist/offline-kit
#   make verify-kit
#   make verify-kit ARGS="--allow-missing-groups"       # 部分キット（python/chromium のみ等）の単体検証用
#
# 環境変数:
#   SHERPA_WFTEST_HOST_IMAGE      検証用ホスト像のタグ（既定: sherpa-wftest-host:ga）
#   SHERPA_WFTEST_REBUILD_HOST=1  来歴が一致していても既存のホスト像を確認せず常に作り直す
#   SHERPA_WFTEST_SNAPSHOT        ホスト像構築に使う snapshot.ubuntu.com の時刻（既定: 20240501T000000Z）
set -Eeuo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

note()  { echo "・ $*"; }
ok()    { echo "OK: $*"; }
warn()  { echo "ⓘ  $*" >&2; }
fail()  { echo "✗ $*" >&2; }

usage() {
  cat <<'EOF'
使い方: scripts/verify_offline_kit_apt.sh [キットのパス] [--allow-missing-groups] [-h|--help]

出荷ゲート: 閉域キットを「搬入先に近いホスト（Ubuntu Server 24.04 GA 相当）」へ
--network none で実際に apt 導入し、削除提案・カーネル消失・導入失敗が無いことを確かめる。

引数:
  キットのパス   既定: dist/offline-kit

オプション:
  --allow-missing-groups   検証対象の5グループ（Python実行系/Docker Engine/Chromiumシステム依存/
                            LibreOffice/Noto Sans CJK JP）のうち未収集のものがあっても NG にしない
                            （既定は必須＝未収集は NG。--skip-* で作った部分キットの単体検証用）。

環境変数:
  SHERPA_WFTEST_HOST_IMAGE      検証用ホスト像のタグ（既定: sherpa-wftest-host:ga）
  SHERPA_WFTEST_REBUILD_HOST=1  来歴が一致していても既存のホスト像を確認せず常に作り直す
  SHERPA_WFTEST_SNAPSHOT        ホスト像構築に使う snapshot.ubuntu.com の時刻（既定: 20240501T000000Z）

終了コード: 0=導入失敗・削除提案・カーネル消失・（既定では）必須グループの未収集なし／
            非0=1つでも問題あり、または検証不能（docker 無し等）
EOF
}

KIT_ARG="dist/offline-kit"
ALLOW_MISSING_GROUPS=0
while [ $# -gt 0 ]; do
  case "$1" in
    -h|--help) usage; exit 0 ;;
    --allow-missing-groups) ALLOW_MISSING_GROUPS=1; shift ;;
    -*) fail "不明なオプションです: $1"; usage >&2; exit 2 ;;
    *) KIT_ARG="$1"; shift ;;
  esac
done

if [ ! -d "$KIT_ARG" ]; then
  fail "キットが見つかりません: $KIT_ARG"
  fail "  先に ./scripts/make_offline_kit.sh --fetch でキットを作ってください。"
  exit 1
fi
KIT_DIR="$(cd "$KIT_ARG" && pwd)"

if ! command -v docker >/dev/null 2>&1; then
  fail "docker がありません。このホストでは出荷ゲートを検証できません（黙って成功にはしません）。"
  fail "  docker が使える機体（収集機・CIランナー等）で実行してください。"
  exit 2
fi

HOST_IMAGE="${SHERPA_WFTEST_HOST_IMAGE:-sherpa-wftest-host:ga}"
HOST_BASE_TAG="ubuntu:noble-20240423"                       # 24.04 GA 相当のビルド（rolling tag と違い中身が変わらない）
SNAPSHOT="${SHERPA_WFTEST_SNAPSHOT:-20240501T000000Z}"      # GA（2024-04-25 リリース）直後の snapshot.ubuntu.com 断面
BUILD_CONTAINER="sherpa-wftest-gate-hostbuild"
# 2026-08-18 RV MED-2: レシピ（このスクリプトのホスト像構築ロジック）を変えたら上げる。ホスト像の
# label と突き合わせ、レシピ版が古い既存像は再利用せず作り直す（別世代の像を黙って使い続けない）。
HOST_RECIPE_VERSION="2"
# 実機 Ubuntu Server の導入済み集合の代表（閉域実機報告①で ubuntu-server 系メタが要求していた上げ先）。
# ホスト像にこれらが実際に導入されていることを、再利用前・構築直後の両方で明示的に確認する。
HOST_PREREQ_PACKAGES="libglib2.0-bin packagekit software-properties-common ubuntu-server linux-image-generic"

# ---------------------------------------------------------------------------
# 1. 検証用ホスト像の用意（オンライン・キャッシュされる）
# ---------------------------------------------------------------------------

# 前提パッケージが導入済みか（$1=イメージタグ）。docker run で直接 dpkg-query を叩く（コンテナに
# シェルを経由させない＝クォート地獄を避ける・値は dpkg-query 自身のフォーマット指定子でリテラルのまま渡る）。
_host_image_prereqs_ok() {
  local img="$1" pkg status
  for pkg in $HOST_PREREQ_PACKAGES; do
    status="$(docker run --rm "$img" dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null || true)"
    if [ "$status" != installed ]; then
      warn "  前提パッケージが未導入です: $pkg（$img）"
      return 1
    fi
  done
  return 0
}

# 既存のホスト像を再利用してよいか（$1=イメージタグ）。来歴 label（レシピ版・ベースタグ・snapshot）が
# 現在のこのスクリプトの値と一致し、かつ前提パッケージが揃っていれば0。1つでもずれれば作り直す
# （2026-08-18 RV MED-2: 「存在すれば中身を見ずに再利用」だと、ローカルに古い／別内容の同名イメージが
# あってもゲートが別物のホストで通ってしまうため）。
_host_image_fresh() {
  local img="$1" recipe base snap label_sha live_sha
  recipe="$(docker image inspect --format '{{index .Config.Labels "sherpa.wftest.recipe_version"}}' "$img" 2>/dev/null || true)"
  base="$(docker image inspect --format '{{index .Config.Labels "sherpa.wftest.base_tag"}}' "$img" 2>/dev/null || true)"
  snap="$(docker image inspect --format '{{index .Config.Labels "sherpa.wftest.snapshot"}}' "$img" 2>/dev/null || true)"
  if [ "$recipe" != "$HOST_RECIPE_VERSION" ] || [ "$base" != "$HOST_BASE_TAG" ] || [ "$snap" != "$SNAPSHOT" ]; then
    warn "  来歴 label が一致しません（label: recipe=$recipe base=$base snapshot=$snap / 現在: recipe=$HOST_RECIPE_VERSION base=$HOST_BASE_TAG snapshot=$SNAPSHOT）"
    return 1
  fi
  # 2026-08-18（Codex RV 2巡目 指摘6）: `_build_host_image` は導入パッケージ集合の sha256
  # （`sherpa.wftest.pkgset_sha256`）を label に書いているのに、ここで一度も比較していなかった
  # （書くだけで使っていない label＝将来ドリフトを検出できない見せかけの照合）。像に持たせた
  # `/root/PACKAGE-LIST.txt`（構築時点の `dpkg-query` 結果）を今読み直してハッシュを取り、label の
  # 値と突き合わせる。像の中身が構築後に変わっていれば（手動 exec からの `docker commit` 上書き等）
  # ここで検出して作り直す。
  label_sha="$(docker image inspect --format '{{index .Config.Labels "sherpa.wftest.pkgset_sha256"}}' "$img" 2>/dev/null || true)"
  live_sha="$(docker run --rm "$img" sha256sum /root/PACKAGE-LIST.txt 2>/dev/null | awk '{print $1}')"
  if [ -z "$label_sha" ] || [ "$label_sha" != "$live_sha" ]; then
    warn "  package-set のハッシュが一致しません（label: ${label_sha:-<なし>} / 像の中身: ${live_sha:-<読めません>}）"
    return 1
  fi
  _host_image_prereqs_ok "$img"
}

_build_host_image() {
  note "検証用ホスト像（$HOST_IMAGE）を構築します: $HOST_BASE_TAG に ubuntu-minimal/ubuntu-standard/ubuntu-server＋"
  note "  linux-image-generic を snapshot $SNAPSHOT（GA 直後）で導入し、apt lists を空にします（オンライン・数分）..."
  note "  RV MED-2: recommends 込みで導入します（--no-install-recommends は外した。実機の Ubuntu Server 標準"
  note "  導入（apt 既定 Install-Recommends=yes）に近づけ、この検証の目的＝搬入先の導入済み集合の近似の忠実性を優先する判断）。"
  docker rm -f "$BUILD_CONTAINER" >/dev/null 2>&1 || true
  docker run -d --name "$BUILD_CONTAINER" "$HOST_BASE_TAG" sleep infinity >/dev/null
  if ! docker exec "$BUILD_CONTAINER" bash -c "
    set -e
    export DEBIAN_FRONTEND=noninteractive
    sed -i 's/^Suites:.*/&\nSnapshot: $SNAPSHOT/' /etc/apt/sources.list.d/ubuntu.sources
    # GA イメージに ca-certificates が無く snapshot（https）を検証できない。ホスト像構築の間だけ緩める
    # （構築後に apt lists を空にするので、出来上がったホスト像・配布物には影響しない＝構築時だけの話）。
    apt-get -qq -o Acquire::https::Verify-Peer=false update
    apt-get -qq -o Acquire::https::Verify-Peer=false install -y \
      ubuntu-minimal ubuntu-standard ubuntu-server linux-image-generic
    dpkg-query -W -f='\${binary:Package} \${Version}\n' | sort > /root/PACKAGE-LIST.txt
    apt-get clean
    rm -rf /var/lib/apt/lists/*
    sed -i '/^Snapshot:/d' /etc/apt/sources.list.d/ubuntu.sources
  "; then
    fail "検証用ホスト像の構築に失敗しました（ネットワークが必要です。この手順だけはオンラインです）。"
    docker rm -f "$BUILD_CONTAINER" >/dev/null 2>&1 || true
    return 1
  fi
  # RV MED-2: 前提パッケージが recommends 込みでも実際に入ったことを、コミット前にその場で確認する
  # （黙って不完全なホスト像を作らない＝欠けていたら構築自体を失敗させる）。
  local pkg status
  for pkg in $HOST_PREREQ_PACKAGES; do
    status="$(docker exec "$BUILD_CONTAINER" dpkg-query -W -f='${db:Status-Status}' "$pkg" 2>/dev/null || true)"
    if [ "$status" != installed ]; then
      fail "検証用ホスト像の構築: 前提パッケージが導入されていません（$pkg）。recommends 込みでも入らない場合は"
      fail "  ホスト像のレシピ（このスクリプト §1）の見直しが必要です。"
      docker rm -f "$BUILD_CONTAINER" >/dev/null 2>&1 || true
      return 1
    fi
  done
  PKGSET_SHA256="$(docker exec "$BUILD_CONTAINER" sha256sum /root/PACKAGE-LIST.txt | awk '{print $1}')"
  # 来歴 label（レシピ版・ベースタグ・snapshot・導入パッケージ集合のハッシュ）を付けて commit する。
  # 次回以降の起動時、_host_image_fresh がこれを見て再利用可否を判断する。
  docker commit \
    --change "LABEL sherpa.wftest.recipe_version=$HOST_RECIPE_VERSION" \
    --change "LABEL sherpa.wftest.base_tag=$HOST_BASE_TAG" \
    --change "LABEL sherpa.wftest.snapshot=$SNAPSHOT" \
    --change "LABEL sherpa.wftest.pkgset_sha256=$PKGSET_SHA256" \
    "$BUILD_CONTAINER" "$HOST_IMAGE" >/dev/null
  docker rm -f "$BUILD_CONTAINER" >/dev/null 2>&1 || true
  ok "検証用ホスト像を用意しました: $HOST_IMAGE（recipe=$HOST_RECIPE_VERSION pkgset_sha256=${PKGSET_SHA256:0:12}…・導入パッケージ一覧は"
  ok "  docker run --rm $HOST_IMAGE cat /root/PACKAGE-LIST.txt で確認可）"
}

if [ "${SHERPA_WFTEST_REBUILD_HOST:-0}" = 1 ]; then
  note "SHERPA_WFTEST_REBUILD_HOST=1 のため、既存のホスト像を確認せず作り直します。"
  _build_host_image || exit 2
elif docker image inspect "$HOST_IMAGE" >/dev/null 2>&1; then
  note "検証用ホスト像（$HOST_IMAGE）は既にあります。来歴（レシピ版/ベースタグ/snapshot/前提パッケージ）を照合します..."
  if _host_image_fresh "$HOST_IMAGE"; then
    note "→ 来歴が一致するため再利用します（作り直すには SHERPA_WFTEST_REBUILD_HOST=1）。"
  else
    note "→ 来歴が一致しない（別内容・別世代の同名イメージの疑い）ため作り直します。"
    _build_host_image || exit 2
  fi
else
  _build_host_image || exit 2
fi
echo ""

# ---------------------------------------------------------------------------
# 2. --network none でキットを実導入する（apt_offline_install をそのまま使う・別経路を作らない）
# ---------------------------------------------------------------------------
DRIVER="$(mktemp)"
trap 'rm -f "$DRIVER"' EXIT
cat > "$DRIVER" <<'DRV'
set -Eeuo pipefail
note()  { echo "・ $*"; }
ok()    { echo "OK: $*"; }
warn()  { echo "ⓘ  $*" >&2; }
fail()  { echo "✗ $*" >&2; }

# shellcheck source=scripts/lib/apt_offline.sh
. /mnt/apt_offline.sh
export SHERPA_APT_OFFLINE_SUDO=""   # コンテナ内は root 実行（sudo 不要・sudo 自体が無い最小像もある）

KIT=/mnt/kit
VERIFY_FAILED=0

echo "--- 土台の照合（BASELINE） ---"
apt_offline_check_baseline "$KIT" || VERIFY_FAILED=1
echo ""

# install_offline_kit.sh が実際に呼ぶグループを、同じ経路（apt_offline_install）で導入する。
# 2026-08-18 RV HIGH-1: install_offline_kit.sh の最終検証（閉域側・導入後）は「未収集＝意図的スキップ」を
# NG にしない方針で正しい（利用者が --skip-* を選んだ結果だから）。しかしこの出荷ゲート（オンライン側・
# 搬入前）で同じ方針を採ると、収集が --skip-* で意図的に省いたのか、単に失敗して欠けただけなのかを
# 区別できないまま rc=0 で通してしまう（実導入では python 無しで .venv 作成が最初に転び、docker-engine
# 無しではストア自体が起動できず、chromium/libreoffice/fonts 無しではその機能が閉域で動かないまま出荷
# される）。よってこのゲートでは検証対象の5グループを既定で「必須」とし、未収集は NG にする。
# 部分キット（例: python + chromium 依存だけの縮小キットでゲート自体を単体検証する）は
# --allow-missing-groups（環境変数 SHERPA_WFTEST_ALLOW_MISSING_GROUPS 経由でこのコンテナへ渡る）を
# 付けたときだけ、従来どおり「未収集」表示のまま許容する。
_verify_group() {  # $1=表示名 $2=キット内の group_dir（相対）
  local name="$1" gdir="$KIT/$2" rc=0
  if [ ! -d "$gdir" ] || [ -z "$(find "$gdir" -maxdepth 1 -name '*.deb' 2>/dev/null)" ]; then
    if [ "${SHERPA_WFTEST_ALLOW_MISSING_GROUPS:-0}" = 1 ]; then
      printf '  [未収集] %s（%s）\n' "$name" "$2"
    else
      printf '  [NG]    %s 未収集（%s）＝必須グループが欠落しています（--allow-missing-groups で許容可）\n' "$name" "$2"
      VERIFY_FAILED=1
    fi
    return
  fi
  apt_offline_install "$name" "$KIT" "$gdir" || rc=$?
  if [ "$rc" = 0 ]; then
    printf '  [OK]    %s\n' "$name"
  else
    printf '  [NG]    %s（apt_offline_install rc=%s）\n' "$name" "$rc"
    VERIFY_FAILED=1
  fi
}

echo "--- apt グループの実導入（--network none・$KIT） ---"
BEFORE_KERNEL="$(_apt_offline_kernel_snapshot)"
_verify_group "Python 実行系"          "python/debs"
_verify_group "Docker Engine"          "docker-engine/debs"
_verify_group "Chromium システム依存"  "chromium/deps-debs"
_verify_group "LibreOffice"            "libreoffice/debs"
_verify_group "Noto Sans CJK JP"       "fonts/noto-cjk-debs"
echo ""

echo "--- カーネル残存（全グループ通算） ---"
if _apt_offline_kernel_verify "$BEFORE_KERNEL"; then
  printf '  [OK]    稼働中カーネルは残っています\n'
else
  printf '  [NG]    稼働中カーネルが消えています\n'
  VERIFY_FAILED=1
fi
echo ""

if [ "$VERIFY_FAILED" = 1 ]; then
  fail "出荷ゲート: NG（削除提案・導入失敗・カーネル消失のいずれかを検出しました。上のログを参照）。"
  exit 1
fi
ok "出荷ゲート: OK（削除ゼロ・カーネル残存・導入失敗なしを確認しました）。"
DRV

note "検証用ホスト（$HOST_IMAGE）を --network none で起動し、キット（$KIT_DIR）を実導入します..."
echo ""
RC=0
docker run --rm --network none \
  -e "SHERPA_WFTEST_ALLOW_MISSING_GROUPS=$ALLOW_MISSING_GROUPS" \
  -v "$KIT_DIR:/mnt/kit:ro" \
  -v "$ROOT/scripts/lib/apt_offline.sh:/mnt/apt_offline.sh:ro" \
  -v "$DRIVER:/mnt/driver.sh:ro" \
  "$HOST_IMAGE" bash /mnt/driver.sh || RC=$?
echo ""
if [ "$RC" != 0 ]; then
  fail "出荷ゲート NG（終了コード $RC）。キット（$KIT_DIR）を搬入前に修正してください。"
  exit "$RC"
fi
ok "出荷ゲート OK: $KIT_DIR は搬入先相当のホストへ削除ゼロ・カーネル残存で導入できます。"
