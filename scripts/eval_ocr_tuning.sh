#!/usr/bin/env bash
# OCR の速さの設定を比べる: モデル（medium・small・mobile）× oneDNN（無・有）の 6 通りを fixtures/eval/ocr_ja で測り、
# 所要時間と語の recall を表にして出す。測った機械の値でしかない（別の機械へ持ち出さない）。
#
#   ./scripts/eval_ocr_tuning.sh [出力フォルダ]   # 既定: data/eval-results/ocr-tuning
#   SHERPA_OCR_THREADS=2 ./scripts/eval_ocr_tuning.sh   # 推論スレッド数を固定して測る
#
# 前提: make ocr-models が済んでいること（専用 venv `.venv-ocr` と data/ocr-models）。ダウンロードはしない。
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
cd "$ROOT"

VENV="$ROOT/.venv-ocr"
CACHE="${SHERPA_OCR_MODEL_CACHE:-$ROOT/data/ocr-models}"
LOCK="$ROOT/docker/ocr-models.lock.json"
OUT="${1:-$ROOT/data/eval-results/ocr-tuning}"

[ -x "$VENV/bin/python" ] && [ -d "$CACHE/official_models" ] || {
  echo "OCR の環境かモデルがありません。先に make ocr-models を実行してください。" >&2
  exit 1
}
# 固定した hash のモデルだけで測る（足りない・違うときは PaddleOCR を作る前に止める＝取得しに行かせない）
PYTHONPATH="$ROOT" "$VENV/bin/python" - "$LOCK" "$CACHE" <<'PY' || exit 1
import json, sys
from pathlib import Path
from sherpa.ingest.ocr_worker import _tree_digest
lock, cache = json.load(open(sys.argv[1])), Path(sys.argv[2])
bad = []
for model in lock["models"]:
    path = cache / model["cache_relative_path"]
    try:
        ok = path.is_dir() and _tree_digest(path) == model["tree_sha256"]
    except (OSError, ValueError):
        ok = False
    if not ok:
        bad.append(model["name"])
if bad:
    print("固定した hash と合わないモデルがあります: " + "・".join(bad) + "。make ocr-models をやり直してください。", file=sys.stderr)
    sys.exit(1)
PY
mkdir -p "$OUT"

for model in medium small mobile; do
  read -r det rec < <("$VENV/bin/python" -c '
import json, sys
det, rec = json.load(open(sys.argv[1]))["sets"][sys.argv[2]]
print(det, rec)' "$LOCK" "$model")
  for onednn in 0 1; do
    args=(--engine paddle --paddle-cache "$CACHE" --paddle-detection-model "$det" --paddle-recognition-model "$rec")
    [ "$onednn" = 1 ] && args+=(--paddle-onednn)
    [ -n "${SHERPA_OCR_THREADS:-}" ] && args+=(--paddle-cpu-threads "$SHERPA_OCR_THREADS")
    echo "==> model=$model onednn=$onednn"
    PYTHONPATH="$ROOT" "$VENV/bin/python" -m sherpa.eval.ocr_engine_poc "${args[@]}" --out "$OUT/$model-onednn$onednn.json" \
      >/dev/null 2>"$OUT/$model-onednn$onednn.err" || echo "  失敗しました（$OUT/$model-onednn$onednn.err）"
  done
done

"$VENV/bin/python" - "$OUT" <<'PY'
import json
import sys
from pathlib import Path

out = Path(sys.argv[1])
print("| モデル | oneDNN | 所要時間(秒) | 語の recall | 領域チェック | macro CER |")
print("|---|---|---|---|---|---|")
for model in ("medium", "small", "mobile"):
    for onednn in (0, 1):
        path = out / f"{model}-onednn{onednn}.json"
        if not path.is_file():
            print(f"| {model} | {'有' if onednn else '無'} | 失敗 | - | - | - |")
            continue
        summary = json.loads(path.read_text(encoding="utf-8"))["engines"][0]["summary"]
        recall, checks = summary["term_recall"], summary["region_checks"]
        print(f"| {model} | {'有' if onednn else '無'} | {summary['elapsed_seconds']} | "
              f"{recall['passed']}/{recall['total']} | {checks['passed']}/{checks['total']} | {summary['macro_character_error_rate']} |")
PY
