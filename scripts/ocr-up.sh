#!/usr/bin/env bash
# 画像内文字の読み取り（OCR）ワーカーを起動する。`make start` / `make up` から呼ばれる。
#
# **前提が揃っていなくても起動を止めない。** 何が足りないかを表示して、そのまま先へ進む。
# OCR は取り込みや検索の必須部品ではなく、揃っていなければ「読み取り結果が出ない」だけだから。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

skip() { echo "  OCR（画像内文字の読み取り）は起動しません: $1"; exit 0; }

# .env の設定を読む（読み方は scripts/run-common.sh に一本化: 明示指定 ＞ .env ＞ 既定）。
# shellcheck source=scripts/run-common.sh
. "$ROOT/scripts/run-common.sh"
sherpa_env_default SHERPA_OCR_ENABLED SHERPA_OCR_WORLD_ROOT SHERPA_OCR_MODEL_CACHE SHERPA_DERIVED_DIR SHERPA_OBSERVATION_DIR

case "$(printf '%s' "${SHERPA_OCR_ENABLED:-1}" | tr '[:upper:]' '[:lower:]')" in
  0|false|no|off) skip "SHERPA_OCR_ENABLED で無効にしています" ;;
esac

MODEL_CACHE="${SHERPA_OCR_MODEL_CACHE:-$ROOT/data/ocr-models}"
if [ ! -d "$MODEL_CACHE/official_models" ]; then
  skip "モデルが未取得です（./scripts/fetch_ocr_models.sh で取得できます・約134MB）"
fi
export SHERPA_OCR_MODEL_CACHE="$MODEL_CACHE"

# ワーカーへ read-only で渡す資料フォルダの親。明示が無ければ登録済みの資料フォルダから決める
# （資料フォルダは1つだけ登録できる契約なので、その1本がそのまま許可範囲になる）。
if [ -z "${SHERPA_OCR_WORLD_ROOT:-}" ]; then
  SHERPA_OCR_WORLD_ROOT="$("$ROOT/.venv/bin/python" - <<'PY' 2>/dev/null || true
try:
    from sherpa import store
    rows = [r for r in store.list_worlds_db() if (r or {}).get("root_path")]
except Exception:
    rows = []
if len(rows) == 1:
    print(rows[0]["root_path"])
PY
)"
fi
if [ -z "${SHERPA_OCR_WORLD_ROOT:-}" ]; then
  skip "資料フォルダが未登録です（登録したあと make restart で起動します）"
fi
if [ ! -d "$SHERPA_OCR_WORLD_ROOT" ]; then
  skip "資料フォルダに到達できません: $SHERPA_OCR_WORLD_ROOT"
fi
export SHERPA_OCR_WORLD_ROOT

# 観測の置き場（ここだけワーカーに書き込みを許す）。資料フォルダと派生物とは別の木にする。
mkdir -p "${SHERPA_OBSERVATION_DIR:-$ROOT/data/observations}"

# イメージには `sherpa/` のコードが焼き込まれる。**毎回ビルドし直す**のが要点で、さもないと
# コードを直したのにワーカーだけ古いまま動き、原因の分からない不整合になる（実際に踏んだ:
# 世代IDの作り方を直したのに、コンテナ内は旧実装のままでジョブが全て cancelled になった）。
# 依存の導入層はキャッシュが効き、変わるのは末尾の COPY だけなので2回目以降は数秒で終わる。
if ! docker image inspect sherpa/ocr-worker:paddleocr-3.7.0-cpu >/dev/null 2>&1; then
  echo "  OCR ワーカーのイメージを作ります（初回のみ・数分かかります）..."
fi

echo "  OCR（画像内文字の読み取り）ワーカーを起動します..."
# ワーカーへ焼くコードの版（VERSION ＋ git のコミット・コミットが取れなければ VERSION のみ）。
SHERPA_APP_VERSION="$(tr -d '[:space:]' < "$ROOT/VERSION" 2>/dev/null || echo unknown)"
_ocr_commit="$(git -C "$ROOT" rev-parse --short HEAD 2>/dev/null || true)"
[ -n "$_ocr_commit" ] && SHERPA_APP_VERSION="$SHERPA_APP_VERSION+$_ocr_commit"
export SHERPA_APP_VERSION

# 閉域（オフライン導入）ではベースイメージが無く、フルビルドできない。その場合も**コードは必ず更新する**:
# 導入済みイメージの上に、現在の sherpa/ だけを載せ替えた薄いイメージを作って同じ名前で付け替える
# （ネットワーク不要）。ベースが引ける環境（開発機）は従来どおり毎回フルビルドする。
_ocr_image=sherpa/ocr-worker:paddleocr-3.7.0-cpu
_ocr_base="$(awk 'toupper($1)=="FROM"{print $2; exit}' "$ROOT/docker/ocr/Dockerfile" 2>/dev/null)"
if [ -n "$_ocr_base" ] && ! docker image inspect "$_ocr_base" >/dev/null 2>&1 \
   && docker image inspect "$_ocr_image" >/dev/null 2>&1; then
  echo "  （ベースイメージが無いため、導入済みイメージにアプリのコードだけを載せ替えます）"
  # 載せ替え済みのイメージの上へ重ねると層が増え続けるため、素のイメージを別名で保持してそこから作る。
  if [ "$(docker image inspect -f '{{index .Config.Labels "sherpa.overlay"}}' "$_ocr_image" 2>/dev/null)" != "1" ]; then
    docker tag "$_ocr_image" "$_ocr_image-base"
  fi
  if ! docker build -q -f "$ROOT/docker/ocr/Dockerfile.overlay" \
    --build-arg "BASE_IMAGE=$_ocr_image-base" --build-arg "SHERPA_APP_VERSION=$SHERPA_APP_VERSION" \
    -t "$_ocr_image" "$ROOT" >/dev/null; then
    # 黙って古いワーカーを動かし続けない（起動の成功に見せない）。意図したスキップ（exit 0）とは区別する。
    echo "  【失敗】OCR ワーカーのコード更新に失敗しました。ワーカーは古いコードのままです。" >&2
    echo "    Docker の状態を確認し、もう一度 make up を実行してください。" >&2
    exit 1
  fi
  sherpa_compose --profile ocr up -d ocr-worker
else
  # 版ラベルは別ファイルで重ねる（docker-compose.yml 本体は固定測定に束縛されるため触らない）。
  COMPOSE_FILE="$ROOT/docker-compose.yml:$ROOT/docker/ocr/compose.version.yml" \
    sherpa_compose --profile ocr up -d --build ocr-worker
fi
echo "    資料フォルダ（読み取り専用）: $SHERPA_OCR_WORLD_ROOT"
echo "    モデル（読み取り専用）      : $MODEL_CACHE"
