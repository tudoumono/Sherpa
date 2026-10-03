"""OOXML 共通生抽出層。Word/Excel/PowerPoint の生 OOXML から「Markdown に出ない構造」を切り出す共通ヘルパ群。

`office_md.py`（決定的 Markdown 変換）と `arms/ooxml_arm.py`（document-ir 構築）の両方から参照し、同じ抽出処理を重複実装しない。
- `word.py`: 隠し文字・削除本文・ハイパーリンク先・テキストボックス・脚注・コメント・ヘッダ/フッタ。
- `powerpoint.py`: 非表示スライド・発表者ノート・スライドサイズ。覆い（occlusion）判定の幾何ロジックは `office_md.py` のものを import して再利用する。
- `excel.py`: 非表示シート/行/列・名前付き範囲・コメント・ハイパーリンク・外部ブック参照。連続領域（非空セルの4連結成分＝「表」）の検出 `regions()` は `office_md._xlsx_md`（`human_md.render_xlsx` 経由）とも共有する。
設計: docs/design/rag.md「アーム一覧」
"""
