---
name: xlsx-japanese-business
description: .xlsx（Excel）ファイルを新規作成・編集するとき、または一覧表・集計表・比較表など表形式の日本語業務資料を作るときに使う。
---

# Excel（.xlsx）日本語業務文書 作成レシピ

Sherpa 管理のベーススキル（自作）。ライブラリは `openpyxl`（導入済・追加インストール不要）。

## 共通ルール（このスキル固有・AGENTS.md の全体ルールと併用）

- 成果物は**カレントディレクトリ（authoring 直下）**に保存する。サブフォルダは作らない。
- ファイル名は内容が分かる日本語＋`.xlsx`（例: `4期消費税率変更_影響一覧.xlsx`）。
  OS で問題になる文字（`/ \ : * ? " < > |`）は使わない。
- ネットワークアクセス（画像DL・外部フォント取得等）は不可。ローカルの情報だけで完結させる。
- 作成が終わったら、**作ったファイル名**と**内容の要約（2〜4文）**を回答の最後に日本語で報告する。

## 基本の型

```python
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Border, Side, Alignment
from openpyxl.utils import get_column_letter

wb = Workbook()
ws = wb.active
ws.title = "一覧"          # シート名は日本語可（31文字以内・: \ / ? * [ ] は使えない）

headers = ["区分", "対象", "内容", "備考"]
ws.append(headers)

for row in data:            # data は dict または list のリスト
    ws.append([row["区分"], row["対象"], row["内容"], row.get("備考", "")])

wb.save("消費税率変更_影響一覧.xlsx")
```

## ヘッダ行のスタイル（太字・背景色・中央寄せ）

```python
header_font = Font(bold=True, color="FFFFFF")
header_fill = PatternFill(start_color="4472C4", end_color="4472C4", fill_type="solid")
header_align = Alignment(horizontal="center", vertical="center")

for cell in ws[1]:                 # 1行目＝ヘッダ
    cell.font = header_font
    cell.fill = header_fill
    cell.alignment = header_align

ws.freeze_panes = "A2"             # ヘッダ行を固定してスクロールしても見える
```

## 罫線（全セル細罫線）

```python
thin = Side(style="thin", color="B7B7B7")
border = Border(left=thin, right=thin, top=thin, bottom=thin)
for row in ws.iter_rows(min_row=1, max_row=ws.max_row, max_col=len(headers)):
    for cell in row:
        cell.border = border
```

## 列幅の自動調整（日本語は全角換算で少し広めに）

```python
def _width(text: str) -> int:
    # 全角文字（ざっくり判定）は幅2、半角は幅1として概算。
    return sum(2 if ord(c) > 255 else 1 for c in str(text))

for i, header in enumerate(headers, start=1):
    col = get_column_letter(i)
    max_len = max([_width(header)] + [_width(ws.cell(r, i).value or "") for r in range(2, ws.max_row + 1)])
    ws.column_dimensions[col].width = min(max_len + 4, 60)   # 上限60（極端に広い列を避ける）
```

## 数値・日付・パーセントの書式

```python
ws.cell(row=2, column=3).number_format = "#,##0"       # 千区切り整数
ws.cell(row=2, column=4).number_format = "0.0%"         # パーセント（値は 0.1 のように小数で入れる）
ws.cell(row=2, column=5).number_format = "yyyy/mm/dd"   # 日付
```

## 複数シート（集計＋明細など）

```python
ws2 = wb.create_sheet("集計")   # 追加シートも日本語名で可
```

## 判定・状態列に色を付ける（例: 確実/要確認）

```python
ok_fill = PatternFill(start_color="C6EFCE", end_color="C6EFCE", fill_type="solid")     # 緑系
warn_fill = PatternFill(start_color="FFEB9C", end_color="FFEB9C", fill_type="solid")   # 黄系
for r in range(2, ws.max_row + 1):
    cell = ws.cell(row=r, column=3)   # 判定列の例
    if cell.value == "確実":
        cell.fill = ok_fill
    elif cell.value == "要確認":
        cell.fill = warn_fill
```

## 注意

- 大量行（数千行超）でも openpyxl は問題なく扱える。ただしセルごとの罫線ループは行数が多いと遅くなるため、
  数万行規模ならヘッダのみスタイル適用に留める判断もしてよい。
- 数式を書く場合は `ws.cell(...).value = "=SUM(A2:A10)"` の形（文字列としてそのまま入れる）。
- 出典・根拠を含める場合は、行の末尾に「根拠文書」列を足し、ユーザから渡された事実（doc_id・引用等）を
  そのまま転記する（推測で埋めない）。
