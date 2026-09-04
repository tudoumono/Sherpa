---
name: pptx-japanese-business
description: .pptx（PowerPoint）ファイルを新規作成・編集するとき、または説明資料・提案スライド・報告スライドなど日本語の発表資料を作るときに使う。
---

# PowerPoint（.pptx）日本語業務文書 作成レシピ

Sherpa 管理のベーススキル（自作）。ライブラリは `python-pptx`（導入済・追加インストール不要）。

## 共通ルール（このスキル固有・AGENTS.md の全体ルールと併用）

- 成果物は**カレントディレクトリ（authoring 直下）**に保存する。サブフォルダは作らない。
- ファイル名は内容が分かる日本語＋`.pptx`（例: `消費税率変更_説明資料.pptx`）。
  OS で問題になる文字（`/ \ : * ? " < > |`）は使わない。
- ネットワークアクセス（画像DL・外部フォント取得等）は不可。ローカルの情報だけで完結させる。
- 作成が終わったら、**作ったファイル名**と**内容の要約（2〜4文）**を回答の最後に日本語で報告する。

## 基本の型（16:9・表紙スライド）

```python
from pptx import Presentation
from pptx.util import Inches, Pt, Emu

prs = Presentation()
prs.slide_width = Inches(13.333)    # 16:9（既定は 4:3 なので明示的に変更する）
prs.slide_height = Inches(7.5)

title_slide_layout = prs.slide_layouts[0]   # 0=タイトルスライド
slide = prs.slides.add_slide(title_slide_layout)
slide.shapes.title.text = "消費税率変更 説明資料"
slide.placeholders[1].text = "調査結果のご報告"   # サブタイトル
```

## 本文スライド（タイトル＋箇条書き、レベル分け）

```python
bullet_layout = prs.slide_layouts[1]   # 1=タイトル+コンテンツ
slide = prs.slides.add_slide(bullet_layout)
slide.shapes.title.text = "影響範囲"

body = slide.placeholders[1].text_frame
body.clear()
first = True
for item in [
    ("BILLINGJOB（確実）", 0),
    ("税額計算ロジックを直接参照", 1),
    ("TAXCALC（要確認）", 0),
    ("関連コピーブック経由", 1),
]:
    text, level = item
    p = body.paragraphs[0] if first else body.add_paragraph()
    first = False
    p.text = text
    p.level = level   # 0=第1階層、1=第2階層（インデントが付く）
```

## フォントサイズ・太字（タイトル/本文の見やすさ）

```python
from pptx.util import Pt

for p in body.paragraphs:
    for run in p.runs:
        run.font.size = Pt(20) if p.level == 0 else Pt(16)
        run.font.bold = (p.level == 0)
```

## 表（比較表・一覧をスライドに埋め込む）

```python
rows, cols = 3, 3
left, top, width, height = Inches(0.7), Inches(1.5), Inches(12), Inches(4)
gtable = slide.shapes.add_table(rows, cols, left, top, width, height).table
headers = ["区分", "対象", "判定"]
for i, h in enumerate(headers):
    gtable.cell(0, i).text = h
data = [("Module", "BILLINGJOB", "確実"), ("Module", "TAXCALC", "要確認")]
for r, row in enumerate(data, start=1):
    for c, val in enumerate(row):
        gtable.cell(r, c).text = str(val)
```

## テキストボックス（自由配置の補足説明）

```python
box = slide.shapes.add_textbox(Inches(0.7), Inches(6.5), Inches(12), Inches(0.6))
tf = box.text_frame
tf.text = "※ 確実=コードから直接確認・要確認=資料からの関連推定"
tf.paragraphs[0].runs[0].font.size = Pt(12)
```

## 保存

```python
prs.save("消費税率変更_説明資料.pptx")
```

## 注意

- レイアウト番号（`slide_layouts[N]`）は既定テンプレートの並び（0=タイトル, 1=タイトル+コンテンツ,
  5=タイトルのみ, 6=白紙 等）。凝ったレイアウトが必要なければ 0 と 1 だけで大半の資料は組める。
- 1スライドに詰め込みすぎない（箇条書きは目安5〜7行まで）。情報が多い場合はスライドを分ける。
- 出典・根拠を含める場合は、ユーザから渡された事実（doc_id・引用等）をそのまま転記する（推測で埋めない）。
