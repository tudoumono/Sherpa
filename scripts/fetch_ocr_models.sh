#!/usr/bin/env bash
# 画像内文字の読み取り（OCR）に使うモデルを取り出す。
#
#   ./scripts/fetch_ocr_models.sh              # data/ocr-models へ取得（既定）
#   SHERPA_OCR_MODEL_CACHE=/srv/sherpa/ocr-models ./scripts/fetch_ocr_models.sh
#
# 取得したフォルダは**そのままコピーして配れる**。閉域（インターネットに出られない）環境へは、
# ネットに出られる端末でこれを実行し、できたフォルダを丸ごと運んで同じ場所に置く。
#
# モデルは配布物に同梱していない（法務確認前・`docker/ocr-models.lock.json` の
# distribution_policy 参照）。ここで取得するのは Apache-2.0 で公開されている公式モデル。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

CACHE="${SHERPA_OCR_MODEL_CACHE:-$ROOT/data/ocr-models}"
VENV="$ROOT/.venv-ocr"
LOCK="$ROOT/docker/ocr-models.lock.json"

# OCR の依存はコアと同居できない（コア=numpy 2.5系 / paddlex=numpy<2.4 必須）。
# そのため専用の venv を使う。無ければ作る。
if [ ! -x "$VENV/bin/python" ]; then
  echo "==> OCR 用の Python 環境を作ります（$VENV）"
  python3 -m venv "$VENV"
  "$VENV/bin/pip" install -q --upgrade pip
fi
if ! "$VENV/bin/python" -c "import paddleocr" >/dev/null 2>&1; then
  echo "==> OCR の依存を入れます（数分かかります・約1.3GB）"
  "$VENV/bin/pip" install -r "$ROOT/requirements-ocr.txt"
fi

mkdir -p "$CACHE"
echo "==> モデルを取得します（約134MB）: $CACHE"
PADDLE_PDX_CACHE_HOME="$CACHE" "$VENV/bin/python" - <<'PY'
import os
from paddleocr import PaddleOCR

# 名前を明示して取得する（既定モデルは版が変わりうるため、固定した2つだけを落とす）。
PaddleOCR(
    text_detection_model_name="PP-OCRv6_medium_det",
    text_recognition_model_name="PP-OCRv6_medium_rec",
    use_doc_orientation_classify=False,
    use_doc_unwarping=False,
    use_textline_orientation=False,
    device="cpu",
)
print("downloaded into", os.environ["PADDLE_PDX_CACHE_HOME"])
PY

# 取得したものが「固定したモデルそのもの」か照合する。ここが合わないとワーカーは起動を拒む
# （別物のモデルで読み取って、精度が変わったことに気づけない状態を作らないため）。
echo "==> モデルを照合します"
PADDLE_PDX_CACHE_HOME="$CACHE" "$VENV/bin/python" - "$LOCK" <<'PY'
import json
import sys
from pathlib import Path

sys.path.insert(0, ".")
from sherpa.ingest import ocr_worker      # noqa: E402

lock = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
root = Path(ocr_worker._default_cache_home()) / "official_models"
ng = 0
for model in lock["models"]:
    actual = ocr_worker._tree_digest(root / model["name"])
    ok = actual == model["tree_sha256"]
    print(f"  {'OK  ' if ok else 'NG  '}{model['name']}")
    if not ok:
        ng += 1
        print(f"      期待 {model['tree_sha256']}")
        print(f"      実際 {actual}")
availability = ocr_worker.paddle_availability()
print(f"  ワーカーから見た状態: available={availability.available} reason={availability.unavailable_reason}")
if ng or not availability.model_hashes_valid:
    raise SystemExit("モデルが固定した内容と一致しません（上流の更新が疑われます）")
PY

echo
echo "用意できました: $CACHE"
echo "  閉域環境へ持っていく場合は、このフォルダを丸ごとコピーして同じ場所に置いてください。"
