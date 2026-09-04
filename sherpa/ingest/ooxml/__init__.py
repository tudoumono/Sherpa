"""OOXML 共通生抽出層（DOC-IR-002・提案書 §7 フェーズ2・外部レビュー#5）。

`office_md.py`（決定的 Markdown 変換・値の権威）と `arms/ooxml_arm.py`（document-ir-v1 並行構築）の
どちらからも参照できる、Word/Excel/PowerPoint の生 OOXML から「MD には出ない構造」を切り出すための
共通ヘルパをここに集約する。**同じ抽出処理を複数箇所で重複実装しない**（外部レビュー#5 の指摘）ための
置き場所。

Word（`word.py`・DOC-IR-002）: 隠し文字・削除本文・ハイパーリンク先・テキストボックス・脚注・コメント・
ヘッダ/フッタは MD が表示しない情報のため、MD 側（`office_md._docx_md`）に対応する実装は無く二重実装は
発生しない。

PowerPoint（`powerpoint.py`・DOC-IR-003）: 非表示スライド・発表者ノート・スライドサイズは Word 同様 MD に
出ない情報のため独立実装。一方、覆い（occlusion）判定の幾何ロジックは MD 側（`office_md.py` の A5＝
`_pptx_bbox`／`_pptx_has_solid_fill`／`_bbox_intersection_ratio`／`_OCCLUSION_RATIO` 等）に既存実装がある
ため、`powerpoint.py` はそれを import して再利用する（二重実装しない）。将来 MD 側がこれら（Word の
隠し構造・PowerPoint の非表示スライド等）の表現を必要とする場合も、新たに XML を歩く実装を増やすのでは
なく本パッケージを参照する、という位置付け。

Excel（`excel.py`・DOC-IR-004）: 非表示シート/行/列・名前付き範囲・コメント・ハイパーリンク・外部
ブック参照は MD が表示しない構造のため独立実装。連続領域（非空セルの4連結成分＝「表」）の検出
（`regions()`）自体は、H2（`docs/proposals/2026-08-28-人間向けMDの刷新.md`）以降 `office_md._xlsx_md`
（人間向け MD・`sherpa/ingest/human_md.py::render_xlsx` 経由）とも共有する共通土台になっている
（旧・シート丸ごと1枚の打切り付きパイプ表は撤去済み・二重実装しない）。
"""
