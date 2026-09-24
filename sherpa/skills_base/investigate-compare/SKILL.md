---
name: investigate-compare
description: 資料の世代（トップフォルダ）間の比較や「何が変わったか」を聞かれたときに使う。
---

# 世代の比較レシピ

Sherpa 管理のベーススキル（自作）。型は「compare_documents で変更点の当たりを付ける→両世代の原本を
Python で開いて実際の値・記述を突き合わせる」。**対応する文書が一意に決まらないときは候補を示して
ask_user で確認する**（世代（トップフォルダ）を跨いだ「同一」はパスの対応関係でしか分からないため）。

## 1. 最初に開くもの（当たりの付け方）

1. 比較したい2文書の `doc_id` が両方分かっているときは
   `compare_documents(left_doc_id=..., right_doc_id=...)` を直接呼ぶ。
2. 片方の `doc_id` と比べたい世代（トップフォルダ名）だけ分かっているときは
   `compare_documents(source_doc_id=..., target_generation=...)` で対応文書を自動発見させる。
3. 戻り値が `status: needs_disambiguation` なら、`candidates`（doc_id 一覧）を会話で提示し、
   `ask_user` でどちらか確認してから `left_doc_id`/`right_doc_id` で呼び直す。`candidates` が空なら
   対応する文書が無い＝「対応文書が見つからない（要確認）」と答える（`ask_user` は使わない・選択肢が作れない）。
4. `status: unsupported`（rag.md を持たない文書＝コード原文等）のときは、compare_documents に頼らず
   §2 の Python 突合せへ進む。

`compare_documents` は rag.md（検索用正本）の unified diff——**変更点の当たりを付けるための道具**であり、
最終確認は必ず両方の原本を Python で開いて行う。

## 2. 中身の確認（読取ツールが第一・丸ごとの突合は定型外）

`compare_documents` が示した差分行の周辺を、ピンポイントで1箇所だけ確認したいときはまず読取ツール
（`xlsx_range`／`docx_paragraphs`／`pptx_slides`／`pdf_pages`）で該当シート・段落・スライド・ページを
直接読む（Python を書かない）。

両世代を丸ごと突き合わせて対応行・追加/削除を洗い出す作業（difflib による構造化比較）は読取ツールの
1回の呼び出しでは終わらない定型外の作業——ここは Python で原本を直接開いてよい（読取専用モードを
必ず使う）。

### Excel（同じシート名・範囲を両世代で比較）

```python
import difflib
from openpyxl import load_workbook

wb_old = load_workbook(old_path, read_only=True, data_only=True)
wb_new = load_workbook(new_path, read_only=True, data_only=True)
ws_old, ws_new = wb_old["Sheet1"], wb_new["Sheet1"]
# 行の挿入・削除で位置がずれるので、番号合わせ（zip）ではなく difflib で対応を取る（全行が対象・
# 末尾の追加も拾う）。行数が多いシートは先に ws.max_row を見て、必要なら列を絞ってから比較する。
rows_old = [str(r) for r in ws_old.iter_rows(min_row=1, max_row=ws_old.max_row, values_only=True)]
rows_new = [str(r) for r in ws_new.iter_rows(min_row=1, max_row=ws_new.max_row, values_only=True)]
for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, rows_old, rows_new, autojunk=False).get_opcodes():
    if tag == "equal":
        continue
    print(tag, "旧 行", i1 + 1, "-", i2, rows_old[i1:i2], "/ 新 行", j1 + 1, "-", j2, rows_new[j1:j2])
```

### Word / PDF（段落・ページ単位で突き合わせる）

```python
import difflib
from docx import Document

doc_old, doc_new = Document(old_path), Document(new_path)
paras_old = [p.text for p in doc_old.paragraphs]
paras_new = [p.text for p in doc_new.paragraphs]
# 段落の追加・削除で位置がずれるので、番号合わせではなく difflib で対応を取る（末尾の追加・削除も拾う・
# autojunk=False＝同じ行が何度も出る表形式でも挿入後を置換扱いにしない）
for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, paras_old, paras_new, autojunk=False).get_opcodes():
    if tag == "equal":
        continue
    print(tag, "旧 段落", i1, "-", i2, paras_old[i1:i2], "/ 新 段落", j1, "-", j2, paras_new[j1:j2])
# 表の中の値は doc.tables を走査する（paragraphs には表内の文が含まれない）。表ごとに行を
# 「セルの並び」の文字列にして difflib で対応を取る＝行の挿入・削除で位置がずれても既存の値を
# 「変更」と誤らず、入れ替えは旧値→新値として出る（座標だけの照合や集合差分ではどちらかが壊れる）。
def _rows(doc):
    return [[" | ".join(c.text for c in r.cells) for r in t.rows] for t in doc.tables]
tabs_old, tabs_new = _rows(doc_old), _rows(doc_new)
# 表同士もまず difflib で対応付ける（表の追加・削除・順序の入れ替えを、既存表の変更と誤らない）
keys_old, keys_new = [tuple(t) for t in tabs_old], [tuple(t) for t in tabs_new]   # 行境界を保つ（改行連結はセル内改行と混同する）
for tag, i1, i2, j1, j2 in difflib.SequenceMatcher(None, keys_old, keys_new, autojunk=False).get_opcodes():
    if tag == "equal":
        continue
    if tag == "replace" and (i2 - i1) == (j2 - j1):        # 同数の表が対応＝表の中を行単位で比較
        for k in range(i2 - i1):
            ro, rn = tabs_old[i1 + k], tabs_new[j1 + k]
            for t2, a1, a2, b1, b2 in difflib.SequenceMatcher(None, ro, rn, autojunk=False).get_opcodes():
                if t2 != "equal":
                    print("表", i1 + k, "→", j1 + k, t2, "旧 行", a1, "-", a2, ro[a1:a2], "/ 新 行", b1, "-", b2, rn[b1:b2])
    else:                                                    # 対応しない表＝追加・削除として全行を出す
        for k in range(i1, i2):
            print("削除された表", k, tabs_old[k])
        for k in range(j1, j2):
            print("追加された表", k, tabs_new[k])
```

PDF は `pdfplumber.open(path)` で両世代のページテキストを列（1 ページ 1 要素）にし、上と同じく difflib で
対応を取って比較する（ページの増減も拾う・ページ番号合わせはしない）。

### テキスト・コード（unified diff で突き合わせる）

```python
import difflib

with open(old_path, encoding="utf-8", errors="replace") as f:
    old_lines = f.readlines()
with open(new_path, encoding="utf-8", errors="replace") as f:
    new_lines = f.readlines()
diff = difflib.unified_diff(old_lines, new_lines, fromfile=str(old_path), tofile=str(new_path))
print("".join(diff))
```

## 3. 完了条件と中断

- `compare_documents` の差分箇所を両世代の原本で実際に確認し、変更内容（値・記述）を具体的に言えたら終える。
- 対応文書が一意に決まらず候補が複数残るときは、無理に推測で選ばず `ask_user` で確認する
  （質問は1実行につき1回まで）。
- `status: unsupported` かつ原本同士を Python で開いても対応関係が取れない（構造が大きく違う等）ときは、
  「機械的な突合せはできない（要確認）」と正直に答える。
- 変更点を「すべて」求められた場合も同じ完了条件——差分箇所を確認し切ってから答える。diff に
  `truncated:true` が付いたら続きは取れない（引数に続きの指定が無い）＝その範囲を未確認として明示し、
  「すべて」と書かない（両世代の原本を Python で開いて残りを突き合わせるなら、それを終えてから）。
- **中断**（利用者停止・通信エラー・既存の反復／情報量予算への到達）のときは、確認済みの差分・
  未確認の範囲・中断理由を分けて書き、部分結果を「すべて」と断定しない。

## 4. 回答の形

- 「何が変わったか」は**具体的な変更内容**（旧の値→新の値、追加/削除された記述）で示す。差分行数だけの
  報告で終えない。
- 確定した事実（原本で確認できた変更）と推定（rag.md の diff だけで原本未確認の箇所）は分けて書き、
  推定には「推定」と明示する。
- 対応文書の判定根拠（世代を除いた相対パスの一致・利用者への確認結果）を明示する。
- 本文の中に出典の一覧は書かない。回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした
  資料（両世代とも）を1行1件、資料フォルダからの相対パスで列挙する。
