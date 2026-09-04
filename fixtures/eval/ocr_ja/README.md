# 日本語OCRエンジン比較fixture

Office/PDFに埋め込まれた画像内文字を、OCRエンジン間で同じ条件により比較する。製品の変換結果を正解には使わず、
`oracle.json`に人手で固定した文字、元画像内の領域、fixture hashを正解とする。本番文書、本番由来のファイル名、件数、
サイズは含まない。

評価する4ケースは単純な水増しではない。

- `office_screen`: Excel、DOCX、PPTXで共用する業務画面画像。表、識別子、状態、処理ID、注記を含む。
- `scan_pdf`: text layerを持たないPDFの日本語scanページ。
- `hybrid_pdf`: 古いtext layerを現行ページ画像が全面被覆するPDFの可視ページ。
- `deprecated_stamp`: Excelのセル範囲へ重なる画像オブジェクト内の「廃止」。

このsuiteが直接測るのは、固定PNGに対する文字転記、pixel bbox、領域別の識別子・数値・状態の保持である。
Office/PDF containerから画像を漏れなく取り出す精度、重なり先のセル、矢印方向、表構造の復元、MDへの採用判断は測らない。
それらをOCRの文字精度と混同しない。

## 外部エンジンの共通入力

DGX Spark等でNemotron Parseを実行するときは、`external_observations.example.json`と同じ
`sherpa-ocr-observations-v1`へ変換する。bboxは変形後画像ではなく`oracle.json`記載の原画像pixel座標へ戻す。
各観測は`text`、`confidence`、`bbox=[left, top, right, bottom]`を持つ。confidenceがないモデルは`null`でよい。

```bash
python -m sherpa.eval.ocr_engine_poc \
  --external /path/to/nemotron-observations.json \
  --out data/eval-results/ocr-engine-poc/nemotron-dgx.json
```

モデル名だけでなくrevision、推論ライブラリ、GPU、量子化、prompt、画像前処理、モデルartifact hashを
`metadata`へ保存する。取得元の可変な最新版を同じ結果として扱わない。

## ローカルPoC

Tesseract/PaddleOCRは製品requirementsへ追加せず、隔離した環境で実行する。2026-08-10の実測に使用したpinは
Tesseract 5.3.4、PaddleOCR 3.7.0、PaddlePaddle 3.3.0、`PP-OCRv6_medium_det`、
`PP-OCRv6_medium_rec`である。PaddleのCPU推論ではoneDNN/PIR互換エラーを避けるため`enable_mkldnn=False`を
評価条件に固定している。

評価器のJSONは各engineについて原文、confidence、bbox、所要時間、region check、語の種別別recall、CER、
モデルartifactのtree hashを保存する。CERは認識順の影響を受けるため、主判定は元画像領域ごとの語recallとする。
