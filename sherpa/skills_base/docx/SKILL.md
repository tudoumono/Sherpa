---
name: docx-japanese-business
description: .docx（Word）ファイルを新規作成・編集するとき、または報告書・調査結果まとめ・議事録など文章主体の日本語業務資料を作るときに使う。
---

# Word（.docx）日本語業務文書 作成レシピ

Sherpa 管理のベーススキル（自作）。ライブラリは `python-docx`（導入済・追加インストール不要）。

## 共通ルール（このスキル固有・AGENTS.md の全体ルールと併用）

- 成果物は**カレントディレクトリ（authoring 直下）**に保存する。サブフォルダは作らない。
- ファイル名は内容が分かる日本語＋`.docx`（例: `消費税率変更_調査報告書.docx`）。
  OS で問題になる文字（`/ \ : * ? " < > |`）は使わない。
- ネットワークアクセス（画像DL・外部フォント取得等）は不可。ローカルの情報だけで完結させる。
- 作成が終わったら、**作ったファイル名**と**内容の要約（2〜4文）**を回答の最後に日本語で報告する。

## 基本の型

```python
from docx import Document
from docx.shared import Pt, Cm
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn

doc = Document()

# 既定フォントを日本語で読みやすいものに（Word 標準の游明朝/游ゴシックが入っていない環境向けの保険として
# 明示指定。東アジア言語フォントは w:eastAsia 側も設定しないと反映されないことがある）。
style = doc.styles["Normal"]
style.font.name = "游ゴシック"
style.font.size = Pt(10.5)
style.element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), "游ゴシック")

doc.add_heading("消費税率変更 調査報告書", level=0)   # 表題
doc.save("消費税率変更_調査報告書.docx")
```

既定フォントだけでは見出しスタイル（Heading）等には反映されないことがある。個別の run（段落中の一部強調等）
に日本語フォントを当てたい場合は、以下のヘルパー関数を使う。

```python
from docx.oxml.ns import qn

def set_japanese_font(run, name="游ゴシック", size=None):
    run.font.name = name
    run._element.get_or_add_rPr().rFonts.set(qn("w:eastAsia"), name)
    if size:
        run.font.size = size
```

## 見出し階層

```python
doc.add_heading("1. 背景", level=1)
doc.add_paragraph("消費税率の変更に伴い、影響範囲を調査した。")
doc.add_heading("1.1 対象範囲", level=2)
```

## 表（ヘッダ行を太字にした一覧）

```python
headers = ["区分", "対象", "内容", "根拠文書"]
table = doc.add_table(rows=1, cols=len(headers))
table.style = "Light Grid Accent 1"     # 罫線・縞模様つきの既定スタイル

hdr_cells = table.rows[0].cells
for i, h in enumerate(headers):
    hdr_cells[i].text = h
    for p in hdr_cells[i].paragraphs:
        for r in p.runs:
            r.font.bold = True

for row in data:   # data は dict のリスト
    cells = table.add_row().cells
    cells[0].text = row["区分"]
    cells[1].text = row["対象"]
    cells[2].text = row["内容"]
    cells[3].text = row.get("根拠文書", "")
```

## 箇条書き（番号なし／番号あり）

```python
doc.add_paragraph("影響を受ける処理:", style="Normal")
for name in ["BILLINGJOB", "TAXCALC"]:
    doc.add_paragraph(name, style="List Bullet")

doc.add_paragraph("対応手順:")
for step in ["現状調査", "改修方針の決定", "テスト", "リリース"]:
    doc.add_paragraph(step, style="List Number")
```

## 段落の強調（太字・下線）と中央寄せ

```python
p = doc.add_paragraph()
run = p.add_run("重要: 4/1 リリース予定")
run.bold = True
run.underline = True
p.alignment = WD_ALIGN_PARAGRAPH.CENTER
```

## ページ区切り

```python
doc.add_page_break()
```

## 注意

- 見出し（`add_heading`）を使うと自動で目次対応の見出しスタイルが付く。章立てのある報告書では
  見出しレベルを一貫させる（1→2→3 の飛び番をしない）。
- 出典・根拠を含める場合は、ユーザから渡された事実（doc_id・引用等）をそのまま転記する（推測で埋めない）。
- 表が長大になる場合、無理に1ページに収めようとせず自然改ページに任せてよい。
