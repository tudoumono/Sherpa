#!/usr/bin/env bash
# 完全オフライン（閉域）配布キットの導入スクリプト（閉域環境で実行）。
# マニュアル: docs/manual/offline-kit.md（資材一覧・検証チェックリストはそちらを参照）。
#
# 想定運用: オンライン側で `scripts/make_offline_kit.sh --fetch` を実行したリポジトリを
# （dist/offline-kit/ を含めて）丸ごとこの閉域環境へ移送し、そのチェックアウトのルートで
# 本スクリプトを実行すると、収集済みの資材（アプリ本体・Python実行系/依存・Docker Engine/イメージ・
# Node.js/marp・Playwright Chromium（本体＋システム依存）・LibreOffice・フォント・Ollama）を導入する。
#
# 使い方:
#   ./scripts/install_offline_kit.sh                   # このチェックアウト内で完結（既定）
#   ./scripts/install_offline_kit.sh --target-dir /opt/sherpa/current
#                                                        # 別ディレクトリへ展開する場合（設計経緯:
#                                                        # 2026-07-13-横断レビュー対応.md）。
#                                                        # releases/ 配下の staging（一時名）へ tar 展開
#                                                        # するだけ→未完成マーカーを付けて
#                                                        # releases/<版>（最終絶対パス）へ据え付け→
#                                                        # 依存導入（venv 等）・最終検証は**その最終パスに
#                                                        # 対して**行う（venv は作成後に移動すると
#                                                        # shebang/pyvenv.cfg の絶対パスが壊れるため、
#                                                        # 最初から最終パスで作る）→全部成功したら
#                                                        # マーカーを消して初めて「完成版」（以後
#                                                        # immutable）を名乗り、最後に current をその版へ
#                                                        # 切り替える（検証で失敗したら current は旧版の
#                                                        # まま無傷・releases/<版> はマーカー付きのまま
#                                                        # 残る＝次回同じ版名で再実行すれば作り直される）。
#                                                        # releases/<版> がマーカー無しで既にある場合
#                                                        # （＝完成済み）は置換せず、その場で即エラーにする。
#   ./scripts/install_offline_kit.sh --target-dir /opt/sherpa/current --list-releases
#                                                        # releases/ の版一覧と現在 current が指す版を表示
#                                                        # （staging・マーカー付きの未完成版は一覧に出ない）
#   ./scripts/install_offline_kit.sh --target-dir /opt/sherpa/current --rollback-to sherpa-v0.1.0
#                                                        # current を指定版へ symlink 切替のみで戻す（再展開なし）
#
# 冪等: 再実行しても壊れない（既定モードはこのチェックアウト自身に上書きする設計）。ただし
# --target-dir 使用時は releases/<版>（完成版）は immutable なので、**同じ版名の完成版への
# 再導入はできない**（エラーで中止・別の版名にするか、稼働中でないことを確認して手動で削除
# してから再実行する。未完成マーカー付きの残骸は同じ版名のまま再実行すれば自動で作り直される）。
# 各ステップは対応する資材が dist/offline-kit/ に無ければ「収集していないためスキップ」して次へ進む。
#
# 注意（M2）: このスクリプトは **root で直接実行しない**。内部で必要な操作だけ sudo を使う設計。
# root で実行すると Chromium/Ollama 等が /root 配下に展開され、sherpa/agents.py の自動検出
# （実行ユーザーの $HOME を見る）から見えなくなるため。
set -Eeuo pipefail   # -E: 下の ERR trap（失敗箇所の表示）を関数内にも継承する

if [ "$(id -u)" = 0 ]; then
  echo "✗ このスクリプトを root 直接では実行しないでください（sudo 経由の操作のみ内部で使います）。" >&2
  echo "  root で実行すると Chromium/Ollama 等が /root 配下に展開され、sherpa/agents.py の" >&2
  echo "  自動検出（実行ユーザーの \$HOME を見る）から見えなくなります。一般ユーザーで実行してください。" >&2
  exit 1
fi

# RV MEDIUM（2026-07-15 再々RV・実機確認済みの実害）: 相対 --target-dir を絶対パス化する際は、
# 呼び出し時の cwd を基準にする（ユーザーが実行したディレクトリからの相対パスとして解釈するのが
# 自然な期待動作）。この直後の `cd "$ROOT"` より前に控えておかないと、絶対パス化が
# ROOT（このチェックアウトのルート）基準になってしまい、例えば --target-dir ./sherpa/current が
# チェックアウト自身の sherpa/（Python パッケージ）配下を指してしまう事故になり得る（実機で再現・
# テストで踏んで確認済み）。
ORIG_PWD="$(pwd)"

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

OUT="$ROOT/dist/offline-kit"
TARGET_DIR="$ROOT"

ACTION="install"
ROLLBACK_TO=""
# RV（設計変更・2026-07-15 5巡目RV）: releases/<版> 直下に置く不完全マーカー。存在する間は
# 「未完成（依存導入・検証のどこかで失敗した、または進行中）」を表し、--list-releases/
# --rollback-to の対象から除外する。定数化して section 1 と rollback/list-releases 双方で
# 同じ名前を使う。
_INCOMPLETE_MARKER=".sherpa-incomplete"

usage() {
  cat <<'EOF'
使い方: scripts/install_offline_kit.sh [オプション]

オプション:
  --target-dir <path>   アプリ本体を展開する先を指定する（既定: このチェックアウト自身で完結・展開しない）。
                         /opt/sherpa/current 等の別ディレクトリへ展開する運用向け（設計経緯:
                         2026-07-13-横断レビュー対応.md）。相対パスも受理するが内部で絶対パス化する。
                         <target-dir の親>/releases/ 配下の staging（一時名）へ tar 展開だけ行い、
                         未完成マーカー付きで releases/<版>（最終絶対パス）へ据え付ける。依存導入
                         （venv 等）・最終検証はその最終パスに対して行い（venv は移動すると壊れる
                         ため）、全部成功したらマーカーを消して初めて releases/<版>（以後
                         immutable）を確定し、最後に <target-dir> をその版へ切り替える
                         （symlink・途中で失敗すれば <target-dir> は元の版のまま。systemd 等は
                         <target-dir> 参照のまま）。releases/<版> がマーカー無しで既に存在する
                         場合（完成済み）は置換せず即エラーにする（別の版名で作り直すか、手動で
                         削除してから再実行する）。
  --list-releases        --target-dir の releases/ にある版一覧と、現在 current が指す版を表示して終了
                         （--target-dir と併用必須・展開/導入は行わない）。
  --rollback-to <name>   --target-dir を releases/<name> へ symlink 切替のみで戻す（再展開なし）。
                         <name> は --list-releases で表示される版ディレクトリ名
                         （--target-dir と併用必須・展開/導入は行わない）。
  -h, --help             このヘルプを表示する

前提: dist/offline-kit/（scripts/make_offline_kit.sh --fetch の成果物）が
      このチェックアウトに含まれていること（オンライン側から丸ごと移送）。
      ただし --list-releases / --rollback-to は dist/offline-kit/ 不要（symlink 操作のみ）。
EOF
}

while [ $# -gt 0 ]; do
  case "$1" in
    --target-dir)
      [ $# -ge 2 ] || { echo "エラー: --target-dir にはパスが必要です" >&2; exit 2; }
      TARGET_DIR="$2"; shift 2 ;;
    --list-releases)
      ACTION="list-releases"; shift 1 ;;
    --rollback-to)
      [ $# -ge 2 ] || { echo "エラー: --rollback-to には版名が必要です（--list-releases で確認）" >&2; exit 2; }
      ACTION="rollback"; ROLLBACK_TO="$2"; shift 2 ;;
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
echo "版: $(cat "$ROOT/VERSION" 2>/dev/null || echo 不明) / 実行: $ACTION / target: $TARGET_DIR / host: $(uname -srm)"
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
  echo "   コマンド : L$line: $cmd（終了コード $rc）" >&2
  echo "   ログ全文 : $_LOG_FILE" >&2
  echo "   このログファイル1本を持ち出せば、オンライン側で原因調査ができます。" >&2
}
trap '_on_err $? $LINENO "$BASH_COMMAND"' ERR

# RV MEDIUM（2026-07-15 再々RV）: --target-dir は相対パスでも受理してきたが、以降の
# dirname/basename によるパス計算・symlink 実体比較（readlink -f との突き合わせ）を安定させるため、
# 冒頭で絶対パス化する。**`-s`（--no-symlinks）が必須**: `--target-dir` は「current という symlink
# 自身のパス」を指すもので、既に symlink が存在する状態（2回目以降の実行・rollback 等）で
# `realpath -m`（symlink 解決あり）を使うと、絶対パス化のつもりが symlink の**リンク先**
# （releases/<現版>）へすり替わってしまい、以降ずっと current ではなく現版ディレクトリを
# 直接指してしまう実害バグになる（実機確認済み）。`-s -m` で「'..' 等の字面だけ正規化し、
# symlink は一切辿らない・未作成パスも許容する」絶対パス化にする。
if [ "$TARGET_DIR" != "$ROOT" ]; then
  if ! command -v realpath >/dev/null 2>&1; then
    fail "realpath コマンドが見つかりません（coreutils 未導入の疑い）。--target-dir の絶対パス化に必要です。"
    exit 1
  fi
  # 相対パスは呼び出し時の cwd（$ORIG_PWD・上で cd "$ROOT" より前に控えた値）基準で解決する。
  case "$TARGET_DIR" in
    /*) : ;;
    *) TARGET_DIR="$ORIG_PWD/$TARGET_DIR" ;;
  esac
  TARGET_DIR="$(realpath -s -m "$TARGET_DIR")"
fi

# RV HIGH/MEDIUM（2026-07-15 再RV）: 一時ディレクトリ・バックグラウンドプロセスは、正常終了だけでなく
# set -e（errexit）経由の異常終了でも必ず後始末する。RETURN trap は errexit 経由の関数離脱では
# 発火しない（bash の既知の挙動・実測で確認済み）ため、スクリプト全体で共有する単一の EXIT trap に
# 登録する方式にする（関数ローカルの trap には頼らない）。
_CLEANUP_DIRS=()
_KEEPALIVE_PID=""
_register_cleanup_dir() { _CLEANUP_DIRS+=("$1"); }
_on_exit() {
  local rc=$? d
  for d in "${_CLEANUP_DIRS[@]:-}"; do
    [ -n "$d" ] && rm -rf "$d" 2>/dev/null || true
  done
  [ -n "$_KEEPALIVE_PID" ] && kill "$_KEEPALIVE_PID" 2>/dev/null || true
  # 失敗の出口はどこであれ、最後に必ず「どこまで進んで・ログはどこか」で締める
  # （ERR 経由なら詳細は表示済み＝まとめだけ。fail→exit 1 の経路でもここは通る）。
  if [ "$rc" != 0 ] && [ "${_ERR_SHOWN:-0}" != 1 ]; then
    echo "" >&2
    echo "✗ 導入は完了していません（ステップ「${CURRENT_STEP:-（準備段階）}」で中断・終了コード $rc）" >&2
    echo "   ログ全文 : ${_LOG_FILE:-（ログ開始前）}" >&2
  elif [ "$rc" = 0 ] && [ -n "${_LOG_FILE:-}" ]; then
    echo "ログ: $_LOG_FILE"
  fi
}
trap _on_exit EXIT

# 2026-07-13-横断レビュー対応.md R6b（版ごとのフォルダに展開し、current リンクを一度に切り替える方式）:
# <target-dir> をシンボリックリンクとして原子的に切り替える。
# `ln -sfn` は「既存 symlink の削除→新規作成」の2 syscall で非アトミック（途中でクラッシュ/競合すると
# <target-dir> が消えた状態が観測され得る）。また固定名の一時パスは、同時に2つ導入/rollback が
# 走った場合に衝突し得る（RV MEDIUM 2026-07-15）ため、`mktemp -d` で同一親配下に**専用ディレクトリ**を
# 作り（mktemp -d 自体は O_EXCL 相当でレースしない）、その中に固定名で symlink を作ってから
# `mv -T <tmp>/current <target>` の rename(2) 一発で置き換える。空になった一時ディレクトリは
# 上の EXIT trap 経由で必ず片付く。
_atomic_symlink_swap() {  # $1=切り替え先の symlink パス（例 .../current） $2=リンク先の実ディレクトリ
  local target="$1" dest="$2" tmpdir
  # 呼び出し側は `if _atomic_symlink_swap ...; then` の形でも使う。bash は if の条件式の中では
  # 関数本体の全コマンドの errexit を止める（実測確認済み）ため、mktemp -d 失敗を素通りさせると
  # $tmpdir が空文字になり `ln -s ... "$tmpdir/current"` が `/current`（ルート直下）を指しかねない。
  # ここだけは明示的に || return 1 で止める。
  tmpdir="$(mktemp -d "$(dirname "$target")/.sherpa-current-tmp.XXXXXX")" || return 1
  [ -d "$tmpdir" ] || return 1
  _register_cleanup_dir "$tmpdir"
  ln -s "$dest" "$tmpdir/current"
  mv -T "$tmpdir/current" "$target"
}

# RV（設計変更・2026-07-15 4/5巡目RV）: releases/<版> は immutable（マーカー削除で確定した後は
# 二度と置換しない）にしたため、途中で失敗した残骸（tar 展開専用の `.staging-*` ディレクトリ・
# 据え付け後だが依存導入/検証を終えられなかった未完成マーカー付きの releases/<版>）を掃除する
# 責任は主に上の EXIT trap（`.staging-*` のみ）と、次回同じ版名での再実行時の rm -rf
# （未完成マーカー付き releases/<版> のみ）が担う。ただし SIGKILL・電源断等、trap 自体が
# 発火できない本物のクラッシュでは両方とも残り得る。削除まではしない（never-delete・中身を
# 検分する余地を残す）が、次回実行時に気づけるよう一覧して警告する。
_warn_stale_staging() {  # $1=RELEASES_DIR
  local releases_dir="$1" d
  for d in "$releases_dir"/.staging-*/; do
    [ -d "$d" ] || continue
    warn "前回の異常終了（クラッシュ等）の残骸の可能性がある staging ディレクトリがあります: $d"
    warn "  中身を確認し、不要なら削除してください（.staging-* は releases/ の正式な版ではありません）。"
  done
  for d in "$releases_dir"/*/; do
    [ -d "$d" ] || continue
    [ -e "${d}${_INCOMPLETE_MARKER}" ] || continue
    warn "未完成マーカー付きの版ディレクトリがあります（依存導入・検証を完了できなかった残骸の可能性）: $d"
    warn "  --list-releases/--rollback-to には出ません。同じ版名で再実行すれば自動で作り直されます。"
  done
}

# RV HIGH（2026-07-16 6巡目RV）: 未完成マーカーは「進行中」と「失敗して放置」を区別しない。
# そのため、同じ releases/ に対して導入（--target-dir）とロールバック（--rollback-to）が
# 並行して走ると、片方が「進行中の版」を失敗の残骸と誤認して rm -rf したり、確定前の版へ
# 切り替えてしまったりする競合が起こり得る。releases 単位の advisory lock（flock・非
# ブロッキング）で、状態確認から切替完了までを排他することで構造的に防ぐ（§2 で不採用とした
# hash-lock とは別物・world_lock（store.py の advisory lock）と同じ思想）。
_acquire_releases_lock() {  # $1=RELEASES_DIR
  local releases_dir="$1"
  exec 9>"$releases_dir/.lock"
  if ! flock -n 9; then
    fail "別の導入/ロールバック作業が進行中のようです（ロック: $releases_dir/.lock）。"
    fail "  同時に複数の導入/ロールバックは行えません。完了を待ってから再実行してください。"
    exit 1
  fi
}

# R6b: --list-releases / --rollback-to は dist/offline-kit/ も sudo も不要な symlink 操作のみのため、
# 通常の導入フロー（sha256 検証・sudo 前払い等）より前に処理して終了する。
if [ "$ACTION" = "list-releases" ] || [ "$ACTION" = "rollback" ]; then
  if [ "$TARGET_DIR" = "$ROOT" ]; then
    fail "--list-releases / --rollback-to は --target-dir と併用してください（既定モードには versioned dir がありません）。"
    exit 2
  fi
  RELEASES_DIR="$(dirname "$TARGET_DIR")/releases"
  if [ ! -d "$RELEASES_DIR" ]; then
    fail "releases ディレクトリが見つかりません: $RELEASES_DIR"
    fail "  --target-dir を指定した通常の導入（--rollback-to/--list-releases 無し）を先に1回実行してください。"
    exit 1
  fi
  _warn_stale_staging "$RELEASES_DIR"

  CURRENT_REAL=""
  if [ -L "$TARGET_DIR" ]; then
    CURRENT_REAL="$(readlink -f "$TARGET_DIR")"
  fi

  if [ "$ACTION" = "list-releases" ]; then
    echo "releases: $RELEASES_DIR"
    echo "current:  $TARGET_DIR -> ${CURRENT_REAL:-（symlink ではない/未導入）}"
    echo ""
    found=0
    for d in "$RELEASES_DIR"/*/; do
      [ -d "$d" ] || continue
      # RV（設計変更・2026-07-15 5巡目RV）: 未完成マーカー付きの版（依存導入・検証の途中で
      # 失敗/中断した残骸）は一覧から除外する（.staging-* と同じ扱い＝完成版だけを見せる）。
      [ -e "${d}${_INCOMPLETE_MARKER}" ] && continue
      found=1
      name="$(basename "$d")"
      if [ -n "$CURRENT_REAL" ] && [ "$(readlink -f "$d")" = "$CURRENT_REAL" ]; then
        printf '  * %s  (current)\n' "$name"
      else
        printf '    %s\n' "$name"
      fi
    done
    [ "$found" = 1 ] || echo "  （releases 配下に版がありません）"
    exit 0
  fi

  # ACTION=rollback
  # RV HIGH（2026-07-16 6巡目RV）: 状態確認（この直後）から切替完了まで releases 単位で排他する。
  # --target-dir の導入と同時に走ると、進行中の未完成版を誤って対象にしうるため。
  _acquire_releases_lock "$RELEASES_DIR"

  # RV MEDIUM（2026-07-15 再々RV／4巡目RV）: 版名は releases/ 直下の単一ディレクトリ名のみを
  # 受理する。'/' を含む名前は releases/ の外を指せてしまい、'.' 始まりの名前（'.'・'..' に加え
  # `.staging-*` の staging 残骸も含む）は releases 自体・その親・確定していない途中状態の
  # ディレクトリを指しうる。
  case "$ROLLBACK_TO" in
    ""|*/*|.*)
      fail "--rollback-to の値が不正です: '$ROLLBACK_TO'"
      fail "  releases/ 直下の版ディレクトリ名を1つだけ指定してください（'/' を含む名前・'.' で"
      fail "  始まる名前（staging の残骸等）は不可・--list-releases で確認）。"
      exit 2
      ;;
  esac
  # RV MEDIUM（2026-07-15 再RV）: TARGET_DIR が「未存在」でも「symlink」でもない場合
  # （＝旧方式の直接展開が残ったまま）は、rollback（symlink 切替）の対象になり得ないため、
  # 分かりにくく壊す前に明示的に fail する。
  if [ -e "$TARGET_DIR" ] && [ ! -L "$TARGET_DIR" ]; then
    fail "$TARGET_DIR は symlink ではありません（旧方式の直接展開が残ったままの可能性）。"
    fail "  --rollback-to は symlink の切替のみを行うため、この状態には使えません。"
    fail "  先に --rollback-to 無しの通常導入（--target-dir のみ）を1回実行してから、"
    fail "  改めて --rollback-to を実行してください。"
    exit 1
  fi
  ROLLBACK_DIR="$RELEASES_DIR/$ROLLBACK_TO"
  # RV MEDIUM（2026-07-15 再々RV）: releases/<name> 自体が symlink だと、releases/ 配下は
  # 実ディレクトリのみという前提が崩れ、想定外の場所へ current を向けてしまい得る。
  if [ -L "$ROLLBACK_DIR" ]; then
    fail "releases/$ROLLBACK_TO が symlink です。releases/ 配下は実ディレクトリのみを想定しています。"
    exit 1
  fi
  if [ ! -d "$ROLLBACK_DIR" ]; then
    fail "指定した版が releases に見つかりません: $ROLLBACK_DIR"
    fail "  --list-releases で版名を確認してください。"
    exit 1
  fi
  # RV（設計変更・2026-07-15 5巡目RV）: 未完成マーカー付きの版（依存導入・検証の途中で
  # 失敗/中断した残骸）へのロールバックは拒否する。current がこの版を指すことは無い
  # はずだが（マーカー削除後にしか切替しない設計）、rollback 先としても選べないようにする。
  if [ -e "$ROLLBACK_DIR/$_INCOMPLETE_MARKER" ]; then
    fail "指定した版は未完成マーカー付きです（依存導入・検証が完了していません）: $ROLLBACK_DIR"
    fail "  --list-releases で確認できる完成済みの版名を指定してください。"
    exit 1
  fi
  if [ ! -w "$(dirname "$TARGET_DIR")" ]; then
    fail "$(dirname "$TARGET_DIR") へ書き込めません（root で直接実行はできない設計のため、"
    fail "  親ディレクトリの所有者/権限を実行ユーザーに合わせて用意し直してください）。"
    exit 1
  fi
  # RV HIGH（2026-07-16 6巡目RV）: 切替直前にもう一度、切替先が実ディレクトリかつマーカー無し
  # （＝完成版）であることを再検査する（flock で排他している以上ここで変わっているはずは
  # ないが、念のための防御の重ね）。
  if [ ! -d "$ROLLBACK_DIR" ] || [ -e "$ROLLBACK_DIR/$_INCOMPLETE_MARKER" ]; then
    fail "切替直前の再検査に失敗しました（$ROLLBACK_DIR が実ディレクトリでない、またはマーカーが残っています）。"
    exit 1
  fi
  _atomic_symlink_swap "$TARGET_DIR" "$ROLLBACK_DIR"
  ok "current を $ROLLBACK_TO へ切り替えました（symlink のみ・再展開なし）: $TARGET_DIR -> $ROLLBACK_DIR"
  note "systemd で常駐運用している場合は再起動してください（例: sudo systemctl restart sherpa-api.service）。"
  exit 0
fi

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
echo "導入先: $TARGET_DIR"
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

# ---------------------------------------------------------------------------
# 1. アプリ本体（sha256 検証・必要なら --target-dir 用の版ディレクトリへ展開）
#
# RV HIGH（2026-07-15 5巡目RV・venv 移動不可問題の根本解決）: 当初案は「staging で venv まで
# 作ってから releases/<版> へ mv」だったが、venv は shebang・pyvenv.cfg に**絶対パスを埋め込む**
# ため、作成後に mv（パスが変わる）すると venv が壊れる。対策: staging は**tar 展開のみ**に留め、
# 展開直後に staging 内へ不完全マーカー（.sherpa-incomplete）を作ってから releases/<版> という
# **最終絶対パス**へ mv（この時点ではまだ「未完成版」）。venv 作成・依存導入・最終検証は
# **据え付け後の最終パスに対して**行う（venv の shebang が最初から最終パスになる）。
# 全部成功したらマーカーを削除して初めて「完成版」を名乗り（13番）、--target-dir 時はそこから
# current への symlink 切替を行う。途中のどこかで失敗すれば current は旧版のまま無傷。
# 以降の 2〜12 番は $INSTALL_DIR（--target-dir 未指定なら $ROOT、指定時は今回据え付けた
# releases/<版> の最終パス）に対して行う。$TARGET_DIR（= current の symlink パス）へは
# マーカー削除後（13番）まで書かない。
# ---------------------------------------------------------------------------
_step "1. アプリ本体"
INSTALL_DIR="$TARGET_DIR"
PENDING_MARKER_PATH=""
PENDING_SWAP_TO=""
APP_TARBALL=""
if [ -d "$OUT/app" ]; then
  APP_TARBALL="$(find "$OUT/app" -maxdepth 1 -name '*.tar.gz' | head -1 || true)"
fi

# RV HIGH（2026-07-15 4巡目RV）: --target-dir 運用でアプリ tarball が無いまま続行すると、
# 依存/ツールの導入先が「既存 current の実体への黙示フォールバック」になっていた
# （検証なしに稼働中の版を直接いじる経路）。--target-dir 時はここで即エラーにする
# （既定モード＝--target-dir 未指定は、従来どおり警告してスキップし継続する）。
if [ -z "$APP_TARBALL" ] && [ "$TARGET_DIR" != "$ROOT" ]; then
  fail "アプリ本体の tarball が見つかりません（$OUT/app/）。--target-dir 運用ではこれが無いと"
  fail "  導入できません。オンライン側で ./scripts/make_offline_kit.sh --fetch を実行し直し、"
  fail "  dist/offline-kit/ ごとこの閉域環境へ移送し直してください。"
  exit 1
fi

if [ -n "$APP_TARBALL" ]; then
  APP_SHA="$APP_TARBALL.sha256"
  if [ -f "$APP_SHA" ]; then
    # sha256 ファイルはベース名のみを記録している（offline-kit.md の既存注意点）ため、
    # 同じディレクトリで検証する。
    if (cd "$OUT/app" && sha256sum -c "$(basename "$APP_SHA")"); then
      ok "アプリ本体の sha256 検証OK: $(basename "$APP_TARBALL")"
    else
      fail "アプリ本体の sha256 検証に失敗しました（改ざん・転送中の破損の疑い）: $APP_TARBALL"
      exit 1
    fi
  else
    warn "sha256 ファイルが見つかりません（検証をスキップ）: $APP_SHA"
  fi
  if [ "$TARGET_DIR" != "$ROOT" ]; then
    # RV HIGH（2026-07-16 6巡目RV）: venv 作成・tar 展開・各種ツールの展開・releases/ 自体の
    # mkdir は、実行時の umask に依存してパーミッションが決まる。呼び出し環境の umask が
    # 厳しい設定（例 077）だと、release ルートだけ chmod 755 しても releases/ 自体や中身の
    # ファイル（.venv/bin/python 等）が別ユーザーから読めない/実行できないままになり得る。
    # --target-dir の構築区間全体（releases/ の mkdir を含む）で umask 022（644/755 相当）を
    # 明示し、全工程が終わったら（13番）元に戻す。ディレクトリ作成より前に設定すること。
    _ORIG_UMASK="$(umask)"
    umask 022

    # <TARGET_DIR の親>/releases/<版> に展開する（設計経緯: 2026-07-13-横断レビュー対応.md）。
    # 旧版ディレクトリは releases/ に残るため rollback は symlink を戻すだけ（--rollback-to 参照）。
    TARGET_PARENT="$(dirname "$TARGET_DIR")"
    RELEASES_DIR="$TARGET_PARENT/releases"
    RELEASE_NAME="$(basename "$APP_TARBALL" .tar.gz)"
    RELEASE_DIR="$RELEASES_DIR/$RELEASE_NAME"
    # 親ディレクトリが実行ユーザーの書ける場所であることが前提（/opt 等は事前に
    # `sudo install -d -o "$USER" -g "$(id -gn)" <親パス>` で所有者付きで作っておくこと）。
    if ! mkdir -p "$TARGET_PARENT" 2>/dev/null || [ ! -w "$TARGET_PARENT" ]; then
      fail "$TARGET_PARENT を作成できない/書き込めません。/opt 等の特権パスは事前に"
      fail "  sudo install -d -o \"\$USER\" -g \"\$(id -gn)\" $TARGET_PARENT  で用意してから再実行してください。"
      exit 1
    fi
    mkdir -p "$RELEASES_DIR"
    # umask 022 を設定する前に releases/ が既に作られていた場合（旧い導入・別 umask での
    # 導入等）に備え、ここでも明示的に 755 へ揃えておく（mkdir -p は既存ディレクトリの
    # パーミッションを変えないため）。
    chmod 755 "$RELEASES_DIR"
    _warn_stale_staging "$RELEASES_DIR"

    # RV HIGH（2026-07-16 6巡目RV）: 状態確認（この直後）から切替完了まで releases 単位で
    # 排他する。--rollback-to と同時に走ると、進行中の未完成版を誤って対象にしうるため。
    _acquire_releases_lock "$RELEASES_DIR"

    # RV（設計変更・2026-07-15 4/5巡目RV）: releases/<版> は immutable にする（マーカーが
    # 消えた「完成版」は二度と書き換えない）。既存の releases/<版> の扱いは3通り:
    # (1) 無ければ新規に進める。(2) マーカー付きで存在＝過去の失敗導入の残骸（current が
    # これを指すことは構造上あり得ない＝マーカー削除後にしか切替しないため）＝安全に
    # rm -rf して作り直せる。(3) マーカー無しで存在＝完成済みの版＝即エラー（置換・
    # rm -rf は全廃・稼働版を誤って壊すレースが構造的に起こらないようにするため）。
    if [ -e "$RELEASE_DIR" ]; then
      if [ -e "$RELEASE_DIR/$_INCOMPLETE_MARKER" ]; then
        # RV HIGH（2026-07-16 6巡目RV・防御の重ね）: flock で排他している以上ここに来る時点で
        # current がこの版を指しているはずはないが、rm -rf する前にもう一度だけ確認する。
        _rm_check_current_real=""
        if [ -L "$TARGET_DIR" ]; then
          _rm_check_current_real="$(readlink -f "$TARGET_DIR")"
        fi
        _rm_check_release_real="$(readlink -f "$RELEASE_DIR")"
        if [ -n "$_rm_check_current_real" ] && [ "$_rm_check_current_real" = "$_rm_check_release_real" ]; then
          fail "内部エラー: current が未完成マーカー付きの $RELEASE_DIR を指しています。安全のため削除を中止します。"
          exit 1
        fi
        note "$RELEASE_DIR は前回の失敗導入の残骸です（未完成マーカーあり）。作り直します..."
        rm -rf "$RELEASE_DIR"
      else
        fail "版 $RELEASE_NAME は既に releases/ に存在します: $RELEASE_DIR"
        fail "  同じ版名の再導入・上書きは行いません（releases/<版> は immutable な設計）。"
        fail "  別の版名で作り直すか、稼働中でないことを確認したうえで手動で削除してから再実行してください。"
        exit 1
      fi
    fi

    # 展開は releases/ 配下の staging（tar 展開専用の一時ディレクトリ）で行う。
    # venv 等はここでは作らない（後述の理由により最終パスで作る）。
    STAGING_DIR="$(mktemp -d "$RELEASES_DIR/.staging-$RELEASE_NAME-XXXXXX")"
    _register_cleanup_dir "$STAGING_DIR"
    note "$STAGING_DIR へ展開します（staging・tar 展開のみ）..."
    tar -xzf "$APP_TARBALL" -C "$STAGING_DIR" --strip-components=1

    # 展開直後、まだ staging にいる間に不完全マーカーを作る（mv 後も一緒に運ばれる）。
    touch "$STAGING_DIR/$_INCOMPLETE_MARKER"

    # releases/<版> という最終絶対パスへ据え付ける（mv＝同一 fs 上の rename）。
    # ここから先、venv 作成・依存導入・最終検証は全部この最終パスに対して行う
    # （venv の shebang・pyvenv.cfg の絶対パスが最初から最終パスになる＝再度 mv しない）。
    mv -T "$STAGING_DIR" "$RELEASE_DIR"

    # RV HIGH（2026-07-15 5巡目RV）: mktemp -d は 0700 で作る。mv はディレクトリ自身の
    # モードを変えないため、そのままだと別ユーザー（例 systemd の User=sherpa-api）が
    # traverse できない release になってしまう。据え付け直後に 755 へ戻す
    # （中身の各ファイル/ディレクトリの権限は tar 側の記録に従うため、ここは release
    # ルート自身のみでよい）。
    chmod 755 "$RELEASE_DIR"
    ok "アプリ本体を展開しました（未完成マーカー付き）: $RELEASE_DIR"

    INSTALL_DIR="$RELEASE_DIR"
    PENDING_MARKER_PATH="$RELEASE_DIR/$_INCOMPLETE_MARKER"
  else
    note "既定（このチェックアウトで完結）のため、展開はスキップします（このチェックアウト自体が本体です）。"
  fi
else
  warn "アプリ本体の tarball が見つかりません（$OUT/app/）。スキップします。"
fi

# .env の初期作成（2026-09-04 ユーザー裁定）: 無ければ .env.example から作成する。
# **既存の .env には絶対に触れない**（上書き・追記とも不可＝運用中の設定・秘密を壊さない。
# cp -n は「上書きしなかった」ことを静かに成功にしてしまうため使わず、存在チェックを明示する）。
if [ -f "$INSTALL_DIR/.env" ]; then
  note ".env は既に存在します（保持・上書きしません）: $INSTALL_DIR/.env"
elif [ -f "$INSTALL_DIR/.env.example" ]; then
  cp "$INSTALL_DIR/.env.example" "$INSTALL_DIR/.env"
  ok ".env を作成しました（.env.example から）。冒頭「0. 本番チェックリスト」節を必ず設定してください: $INSTALL_DIR/.env"
else
  warn ".env.example が見つかりません（$INSTALL_DIR）。.env の初期作成をスキップします。"
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
_PROBE_VENV="$(mktemp -d)/venv-probe"
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
  if "$VENV/bin/python" -m pip install --no-index --find-links "$OUT/wheels" \
      -r "$INSTALL_DIR/requirements.txt" -c "$INSTALL_DIR/constraints.txt"; then
    ok "Python 依存を --no-index でインストールしました: $VENV"
  else
    fail "pip install --no-index に失敗しました。wheel 一式（$OUT/wheels/）とこの機体の Python バージョンが"
    fail "一致しているか確認してください（$OUT/wheels/COLLECTED-WITH-PYTHON-VERSION.txt を参照）。"
    exit 1
  fi
else
  warn "wheel 一式が見つかりません（$OUT/wheels/）。オンライン側で収集していないためスキップします。"
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
  warn "marp-cli の tarball が見つかりません（$MARP_TARBALL）。オンライン側で収集していないためスキップします。"
fi
echo ""

# ---------------------------------------------------------------------------
# 7b. Codex CLI（tools/codex/ へ展開・静的バイナリを PATH に載せる）
#     sherpa は `shutil.which("codex")` で探す。run-common.sh が tools/codex/bin を PATH に足す。
#     認証は Sherpa を動かすユーザーで `printenv OPENAI_API_KEY | codex login --with-api-key`
#     （~/.codex/auth.json を書くだけ・通信不要・実測 2026-08-18）。推論時だけ OpenAI へ出る。
# ---------------------------------------------------------------------------
_step "7b. Codex CLI"
CODEX_TARBALL="$(ls "$OUT"/codex/openai-codex-*-linux-x64.tar.gz 2>/dev/null | head -1 || true)"
if [ -n "$CODEX_TARBALL" ] && [ -f "$CODEX_TARBALL" ]; then
  if [ -f "$OUT/codex/SHA256SUMS" ] && ! ( cd "$OUT/codex" && sha256sum -c --quiet SHA256SUMS ); then
    fail "Codex CLI の tarball が SHA256SUMS と一致しません（搬入時の破損/すり替え）。"; exit 1
  fi
  CODEX_DEST="$INSTALL_DIR/tools/codex"
  rm -rf "$CODEX_DEST/node_modules/@openai/codex"
  mkdir -p "$CODEX_DEST/node_modules/@openai" "$CODEX_DEST/bin"
  tar -xzf "$CODEX_TARBALL" -C "$CODEX_DEST/node_modules/@openai"
  CODEX_BIN_REAL="$CODEX_DEST/node_modules/@openai/codex/node_modules/@openai/codex-linux-x64/vendor/x86_64-unknown-linux-musl/bin/codex"
  if [ ! -x "$CODEX_BIN_REAL" ]; then
    fail "Codex CLI の実行ファイルが展開後に見つかりません: $CODEX_BIN_REAL"; exit 1
  fi
  # node に依存しない静的バイナリへ直接リンク（--skip-node のキットでも Codex は動く）。
  ln -sfn "$CODEX_BIN_REAL" "$CODEX_DEST/bin/codex"
  ok "Codex CLI を展開しました: $CODEX_DEST/bin/codex（$(cat "$OUT/codex/VERSION" 2>/dev/null || echo ?)）"
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
  warn "Codex CLI の tarball が見つかりません（$OUT/codex/）。--skip-codex で収集していない場合はスキップします。"
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
  warn "Playwright Chromium の tarball が見つかりません（$CHROMIUM_TARBALL）。オンライン側で収集していないためスキップします。"
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
    TMP_UNZIP="$(mktemp -d)"
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
[ -n "$(ls "$OUT"/codex/openai-codex-*-linux-x64.tar.gz 2>/dev/null)" ] && CODEX_COLLECTED=1
_verify "Codex CLI（tools/codex/bin/codex --version）" "$CODEX_COLLECTED" "'$INSTALL_DIR/tools/codex/bin/codex' --version"
# モデルの照合はワーカー自身が起動時に行う（固定 hash と一致しなければ available=false）。
# ここではイメージが読み込めていること・モデルが置かれていることだけを見る。
_verify "OCR（ワーカーのイメージとモデル）" "$OCR_COLLECTED" \
  "$DOCKER_CMD image inspect sherpa/ocr-worker:paddleocr-3.7.0-cpu >/dev/null && [ -d \"${SHERPA_OCR_MODEL_CACHE:-$INSTALL_DIR/data/ocr-models}/official_models\" ]"
echo ""

# ---------------------------------------------------------------------------
# 13. 版の確定（不完全マーカーの削除）と current の切替
#     （--target-dir 使用時のみ・全工程完了後の最後の一歩）
#
# RV HIGH（2026-07-15 再々RV・核心／5巡目RVで finalize 手段を mv→マーカー削除に変更）:
# ここまでの 2〜12 番は全部 $INSTALL_DIR（今回据え付けた releases/<版> の最終パス。venv 等の
# 絶対パスは既にこの最終パスで焼き込まれている）に対して行ってきた。releases/<版> が
# 「本物の完成版」を名乗るのは検証まで全部成功してマーカーを消した瞬間、current
# （$TARGET_DIR）を新しい版へ向けるのはさらにその後の、この最後の一歩だけにする。
# 途中のどこかで失敗していれば（最終検証 NG も含め）current は元の版のまま無傷・
# releases/<版> にはマーカーが残ったまま＝ --list-releases/--rollback-to には出ない
# （次回同じ版名で再実行すれば rm -rf されて作り直される）。
# ---------------------------------------------------------------------------
if [ -n "$PENDING_MARKER_PATH" ]; then
  echo "--- 13. 版の確定と current の切替 ---"
  if [ "$VERIFY_FAILED" = 1 ]; then
    fail "最終検証で NG があるため、版の確定も current の切替も行いません。"
    fail "  $TARGET_DIR は元の版のまま無傷です。$RELEASE_DIR は未完成マーカー付きのまま"
    fail "  残ります（--list-releases/--rollback-to からは見えません）。問題を解消すれば"
    fail "  同じ版名でそのまま再実行できます（残骸は自動で作り直されます）。"
  else
    # -------------------------------------------------------------------------
    # 13a. 更新時のバックアップ。**版の確定（マーカー削除）より前**に行う。
    # アプリは --rollback-to で戻せても DB は前進している可能性があるため、停止中なら current
    # 切替前の1点を必須取得する。ストア稼働中だけは要件どおり警告して続行（無停止更新を妨げない）。
    # 停止中なのに env/volume/image 等の不備で取得できない場合は fail-close とし、未完成マーカーを
    # 残して current を切り替えない。SHERPA_BACKUP_BEFORE_SWITCH=0 は運用者による明示的な抑止。
    # -------------------------------------------------------------------------
    if { [ -e "$TARGET_DIR" ] || [ -L "$TARGET_DIR" ]; } && [ "${SHERPA_BACKUP_BEFORE_SWITCH:-1}" != 0 ]; then
      echo "--- 13a. 切替前のバックアップ（更新時） ---"
      # 「稼働中か」の判定は backup.sh に委ねる（同じ env ファイル・同じ project 解決規則・同じ docker 呼び出しで
      # 判定させる。以前は 13a が素の docker とシェル環境だけで別判定しており、env ファイル側の project 名や
      # sudo docker 運用でずれて「稼働中は警告して続行」のはずが fail-close で更新が止まっていた・RV 実測）。
      # backup.sh の終了コード: 0=取得 / 3=稼働中（警告して続行） / それ以外=本当の失敗（fail-close）。
      _BK_ENV="${SHERPA_ENV_FILE:-}"
      [ -z "$_BK_ENV" ] && [ -f /etc/sherpa/sherpa.env ] && _BK_ENV=/etc/sherpa/sherpa.env
      _BK_DIR="${SHERPA_BACKUP_DIR:-$(dirname "$TARGET_DIR")/backups}"
      if [ -z "$_BK_ENV" ]; then
        warn "env ファイル（SHERPA_ENV_FILE / /etc/sherpa/sherpa.env）が見つかりません。SHERPA_USERS_DIR が確定できないため、"
        warn "  個人領域はこのキット配下の既定（空）として退避される可能性があります。DB ボリュームは退避します。"
      fi
      _BK_RC=0
      if [ -n "$_BK_ENV" ]; then
        SHERPA_ENV_FILE="$_BK_ENV" SHERPA_BACKUP_DIR="$_BK_DIR" SHERPA_DOCKER="$DOCKER_CMD" "$ROOT/scripts/backup.sh" || _BK_RC=$?
      else
        SHERPA_BACKUP_DIR="$_BK_DIR" SHERPA_DOCKER="$DOCKER_CMD" "$ROOT/scripts/backup.sh" || _BK_RC=$?
      fi
      case "$_BK_RC" in
        0) ok "切替前のバックアップを取りました: $_BK_DIR/（最新の <日時>/・戻すには make restore FROM=<そのdir>）" ;;
        3) warn "バックアップ未取得（ストア/アプリ稼働中）。更新前の1点を残すには make stop && make backup を推奨します。"
           warn "  （このまま current の切替は続行します。旧版へ戻す際、DB の状態は保証されません）" ;;
        *) fail "切替前のバックアップに失敗したため（exit=$_BK_RC）、版の確定と current の切替を中止します。"
           fail "  $TARGET_DIR は元の版のままです。原因を直して同じ導入を再実行してください。"
           VERIFY_FAILED=1 ;;
      esac
    fi

    # RV（設計変更・2026-07-15 5巡目RV）: 検証まで全部成功して初めてマーカーを消す。
    # これが「完成版」を名乗る唯一の瞬間で、immutable にする狙いどおり、これ以降
    # releases/<版> の中身が書き換わることは二度とない。
    if [ "$VERIFY_FAILED" != 1 ] && rm -f "$PENDING_MARKER_PATH"; then
      ok "版を確定しました（未完成マーカーを削除）: $RELEASE_DIR"
      # RV HIGH（2026-07-16 6巡目RV・防御の重ね）: current 切替の直前に、切替先が実ディレクトリ
      # かつマーカー無し（＝確定済み）であることをもう一度確認する（flock で排他している以上
      # ここで変わっているはずはないが、念のため）。
      if [ ! -d "$RELEASE_DIR" ] || [ -e "$PENDING_MARKER_PATH" ]; then
        fail "内部エラー: 切替直前の再検査に失敗しました（$RELEASE_DIR が実ディレクトリでない、"
        fail "  またはマーカー削除後にも関わらず $PENDING_MARKER_PATH が存在します）。current の切替を中止します。"
        VERIFY_FAILED=1
      else
        PENDING_SWAP_TO="$RELEASE_DIR"
      fi
    elif [ "$VERIFY_FAILED" != 1 ]; then
      fail "版の確定（マーカー削除）に失敗しました: $PENDING_MARKER_PATH"
      fail "  $TARGET_DIR は元の版のまま無傷です。"
      VERIFY_FAILED=1
    fi
  fi

  if [ -n "$PENDING_SWAP_TO" ]; then
    # RV HIGH（2026-07-15 4巡目RV）: 「一時リンクの準備（mktemp・ln -s）を先に完了→旧方式の
    # 退避→mv -T 切替」の順にする。旧方式（symlink でない実ディレクトリ）からの一度きりの
    # 移行は、退避を先にしてから一時リンクを準備する順序だと、mktemp/ln -s の失敗が
    # 「退避後・current 欠損」というタイミングで起き得る。ここでは _atomic_symlink_swap の
    # 内部で一時リンクを先に準備してから mv -T するため、この関数を `if` の条件式として
    # 呼ぶことで（bash は if の条件式評価中は呼び出した関数本体全体の errexit を止める・
    # 実測確認済み）、関数内の mktemp/ln -s の失敗も含めて確実に else 節（退避の巻き戻し）に
    # 到達させる。データはここに置かない契約のため中身は使い捨てのはずだが、確証が持てる
    # までは消さずリネーム退避する（never-delete: バックアップ確認とデータ監査が終わるまで
    # 保持する）。
    LEGACY_BACKUP_DIR=""
    if [ -e "$TARGET_DIR" ] && [ ! -L "$TARGET_DIR" ]; then
      LEGACY_BACKUP_DIR="$TARGET_DIR.pre-versioned.$(date +%Y%m%d%H%M%S)"
      mv "$TARGET_DIR" "$LEGACY_BACKUP_DIR"
      note "旧方式（symlink でない直接展開）の $TARGET_DIR を退避しました: $LEGACY_BACKUP_DIR"
      note "  （アプリ本体のみが対象の想定・データ領域は別 env 変数で外出し済みの前提。"
      note "   バックアップの確認とデータ監査が終わるまでは削除せず保持してください）"
    fi
    if _atomic_symlink_swap "$TARGET_DIR" "$PENDING_SWAP_TO"; then
      ok "current を切り替えました: $TARGET_DIR -> $PENDING_SWAP_TO"
      note "ロールバックする場合: $0 --target-dir \"$TARGET_DIR\" --rollback-to <版名>（--list-releases で確認）"
    else
      fail "current の切替に失敗しました。"
      if [ -n "$LEGACY_BACKUP_DIR" ]; then
        mv "$LEGACY_BACKUP_DIR" "$TARGET_DIR"
        fail "  退避していた旧ディレクトリを $TARGET_DIR へ戻しました（無傷）。"
      fi
      VERIFY_FAILED=1
    fi
  fi

  # RV HIGH（2026-07-16 6巡目RV）: --target-dir 構築区間だけの umask 022 を元に戻す
  # （このプロセス自体はここで終わりに近いが、パターンとして明示的に対にしておく）。
  umask "$_ORIG_UMASK"
  echo ""
fi

# ---------------------------------------------------------------------------
# サマリ
# ---------------------------------------------------------------------------
echo "=== 導入完了 ==="
echo ""
echo "次にやること:"
# RV MEDIUM（2026-07-16 6巡目RV）: --target-dir（releases/<版> は完成後 immutable）では
# release 自体への書込みを案内しない。設定ファイルは常に /etc/sherpa/sherpa.env のみを案内し、
# 起動例にも SHERPA_ENV_FILE を明示する（既定モードの表示はこれまでどおり変えない）。
# 閉域実機報告⑥（2026-08-18）: 従来の案内（SHERPA_AGENT=heuristic か ollama。OPENAI_API_KEY 等は
# 空のままにする）は sherpa/agent_constructs.py の契約と食い違っていた。heuristic は
# SHERPA_EXTRA_AGENTS でも有効化しない限り選択肢に無い値として無視され、常に codex へ倒れる
# （案内どおりに設定した閉域ホストで「AI が答えない」の根本原因になっていた）。さらに本キットは
# Codex CLI を同梱し（7b）、OPENAI_API_KEY があれば導入時点で認証まで自動で済ませるため、
# 「OPENAI_API_KEY 等は空のままにする」も実態と合わない。実際に動く3通りに書き直す
# （実装＝sherpa/agent_constructs.py・sherpa/providers/__init__.py は変更していない）。
if [ "$TARGET_DIR" != "$ROOT" ]; then
  echo "  1) /etc/sherpa/sherpa.env を設定する（閉域での構成は実際には3通り）:"
else
  echo "  1) $TARGET_DIR/.env（または /etc/sherpa/sherpa.env）を設定する（閉域での構成は実際には3通り）:"
fi
# S3（2026-08-18-AzureOpenAI対応）: 実行環境が Azure OpenAI 経由のこともあるため、a) に Azure の
# 設定（OPENAI_BASE_URL＋モデル欄=デプロイ名）を追記した（sherpa/llm.py::openai_base_url。Azure 分岐は
# 作らず「OpenAI 互換の接続先」を設定化しただけ＝OPENAI_API_KEY を設定する導線自体は変えていない）。
echo "     a) OpenAI または Azure OpenAI へ穴あけがある: OPENAI_API_KEY を設定する（Azure ならその"
echo "        キー。同梱の Codex CLI は 7b でキーがあれば認証まで自動で済んでいる。SHERPA_AGENT は"
echo "        書かなくてよい＝未指定なら自動選択される）。Azure なら加えて"
echo "        OPENAI_BASE_URL=https://<リソース名>.openai.azure.com/openai/v1/ を設定する"
echo "        （モデル欄には Azure の「デプロイ名」を入れる）。"
echo "     b) 外へ出られない・ローカル LLM（Ollama）がある: SHERPA_AGENT=ollama（＋ OLLAMA_URL）。"
echo "     c) AI を一切使わない（定型文の簡易応答）: SHERPA_EXTRA_AGENTS=heuristic と"
echo "        SHERPA_AGENT=heuristic の両方を設定する（片方だけだと選べない値として無視され"
echo "        codex へ倒れる）。"
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
if [ "$TARGET_DIR" != "$ROOT" ]; then
  echo "     例: SHERPA_ENV_FILE=/etc/sherpa/sherpa.env $TARGET_DIR/scripts/start.sh serve       # 127.0.0.1 のみ"
  echo "         SHERPA_ENV_FILE=/etc/sherpa/sherpa.env LAN=1 $TARGET_DIR/scripts/start.sh serve  # LAN 公開（make start と同じ）"
else
  echo "     例: SHERPA_ENV_FILE=/etc/sherpa/sherpa.env make start        # 127.0.0.1 のみ"
  echo "         SHERPA_ENV_FILE=/etc/sherpa/sherpa.env LAN=1 make start  # LAN 公開"
fi
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
