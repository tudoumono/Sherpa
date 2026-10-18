---
name: investigate-impact
description: 変更や改修が及ぼす影響範囲を聞かれたとき、または「〜を変えたら」「〜に影響ある？」といった調査に使う。
---

# 影響範囲調査レシピ

Sherpa 管理のベーススキル（自作）。型は「変更対象を特定→関係グラフで接続を確かめる→無ければ原本で補う
→接続の有無を根拠に答える」。**症状語（落ちる/止まる/エラー 等）をそのまま検索語にしない**
（症状からの調査は investigate-cause スキルの領分）。

## 1. 最初に開くもの（当たりの付け方）

1. 変更対象（例: 税率・項目名・プログラム名）を `ripgrep_search(query=...)` で特定する（ソースを探せるのはこれだけ。
   `es_search` は設計書などの資料が対象でソースは含まない）。名前が1つでも判明したら、以降は grep を反復するより関係グラフで辿るほうが早い。
2. 判明した名前を起点に、**2 段**で影響先を求める。①`graph_resolve(name=..., path=..., kind=...)` で起点の候補を並べ
   （同名の別ノードは別の候補＝`path` と `qualified_name` で見分ける）、正しい 1 件の `canonical_id` を選ぶ。②その
   `canonical_id` を `graph_impact(canonical_id=...)` へ渡すと、構造の依存（COPIES／CONTAINS／INVOKES／ACCESSES）だけを
   矢印の逆向きにたどった影響先が、段数（`distance`）・経路（`trace`・`route`）つきで返る。言及（DOCUMENTS）は影響に数えず
   `related_documents`（関連資料）に分かれる。候補が複数で選べないときは `ask_user` で確認してよい。
3. 影響を求める用途は `graph_impact` を使い、`graph_neighbors`（無向・言及を含む近傍）は関連部品を広く見たいときに補う。

手順の骨格: ①変更対象に依存する部品・記述を特定 → ②影響先候補（例: 夜間バッチ＝JCL/ジョブ）を特定 →
③両者の接続（COPIES／INVOKES／ACCESSES／CONTAINS＝構造的な依存の経路）を `graph_impact`（補助に
`graph_neighbors`）で当たる。**`graph_impact` の `route` と `graph_neighbors` の近傍は、辺の種類と向き（from→to）が返る**。COPIES／INVOKES／ACCESSES／
CONTAINS だけで構成された経路は影響の根拠にしてよい。**向きの読み方: 影響は矢印をさかのぼる。`A →COPIES→ B` は A が B を取り込んでいる＝**B を変えると A が影響を受ける**（変更対象から出ていく矢印の先は影響先ではない）。経路に DOCUMENTS（言及）・CORRESPONDS_TO（同名の対応）の辺や、`unverified` の辺（裏付け原本が実在確認できない）が 1 本でも含まれる近傍は構造的な依存ではない＝候補どまり（原本で確認する）。**
経路の先の実際の記述を引用したいときだけ原本を開く。

**設計書も可能な限り探して読む**（`list_docs`／`ripgrep_search` で設計書側の記述を探し、ソースと突き合わせる）。ソースは必ず読む。設計書・ログ・設定が見つからなくても調査を止めず、見つからなかった旨を回答に書く。

**影響先の定義**: コピーブックの項目の桁・型などレコードの配置が変わる変更では、項目を実行文で使うかに関わらず、
そのコピーブックを取り込むプログラムはすべて影響先（取り込むだけのものも含む）。「実行文で使う箇所」などは内訳として
示してよいが、対象一覧から外さない。利用者が「実行文で使うものだけ」など範囲を限定した場合だけ、その範囲に絞る。
依頼の範囲と回答の範囲が違うときは、その差を回答に書く。

## 2. 中身の確認（原本を Python で開いて補う）

`graph_neighbors` で経路が見つからない、または経路はあるが実際の依存内容（どの行・どの設定を参照して
いるか）まで確認したいときは、原本を Python で開いて本文参照を確かめる。

```python
# 影響先候補（例: JCL・COBOL ソース・設定ファイル）を直接開いて、変更対象の名前がどこでどう
# 参照されているかを確認する。src/ は CP932（Shift_JIS）のことがあるため、決め打ちで utf-8
# 置換読みにせず実際の符号化を判定してから読む（BOM・strict UTF-8 を優先し、
# それ以外は置換文字が少ない場合だけ CP932 を選ぶ）。
def read_src(p):
    with open(p, "rb") as f:
        b = f.read()
    if b.startswith(b"\xef\xbb\xbf"):
        return b[3:].decode("utf-8", "replace")
    try:
        return b.decode("utf-8")
    except UnicodeDecodeError:
        u, c = b.decode("utf-8", "replace"), b.decode("cp932", "replace")
        return c if c.count("�") < u.count("�") else u

target_name = "TAXRATE"   # 変更対象の名前（例）
for i, line in enumerate(read_src(candidate_path).splitlines(), start=1):
    if target_name in line:
        print(i, line.rstrip())
```

Excel/Word/PowerPoint/PDF の設計書側に影響先の記述があるときは investigate-spec スキルの雛形
（openpyxl・python-docx・python-pptx・pdfplumber を `read_only=True` 等の読取専用で開く）をそのまま使う。

## 3. 完了条件と中断

- `graph_impact` は結果をページで返す。応答の `transfer.next_offset` が null でないときは、同じ条件に `offset=next_offset` と応答の
  `generation` を付けて呼び直し、`transfer.total` と受け取った件数の合計が一致する（`next_offset` が null になる）まで続ける。
  続きを取るまでは一覧が途中＝先頭のページだけで「すべて」と書かない。一覧は `evidence_limit=0` で最後まで取る（各影響先は識別子・種別・名前・パス・段数と、辺の種類の列 `via` だけの軽い形で、
  1 ページに多く入る）。根拠（`route`・`evidence`）が要る対象だけ `evidence_limit` を 1 以上にして取り直す。関連資料（`related_documents`）・流れの候補（`flow_candidates`）も残りがあれば `transfer.other_parts`
  に出るので、`part` を `related_documents`／`flow` にして同じ条件で続きを取る。
  `graph_generation_mismatch`・`graph_page_expired`・`graph_page_mismatch` が返ったら、`offset` を 0 に戻して最初から取り直す。
  取り直さずに合算しない。
- `graph_impact` の確認は 2 つに分けて読む。`exploration.complete` が `false` のときは、影響先が空でも「影響なし」とは言えない＝未確認。
  理由は `exploration.limits[].kind`（`depth`＝深さの先に影響先が残る→`depth` を上げて呼び直す／`timeout`・`row_cap`＝範囲を絞る）。
  `transfer.complete` が `false` のときは探索済みの結果をまだ受け取りきれていない＝上のとおり続きを取る。
  どちらも未完了のまま答えるときは、未確認の範囲を回答に明示する。
- `graph_neighbors` の各近傍は辺ごとの種類と向き（from→to）が返る。COPIES／INVOKES／ACCESSES／CONTAINS
  だけで構成された経路は影響の根拠にしてよい（影響は矢印をさかのぼる＝変更対象へ矢印が向いている側が
  影響を受ける）。経路に DOCUMENTS（言及）・CORRESPONDS_TO（同名の対応）の辺や `unverified` の辺（裏付け原本が
  実在確認できない）が 1 本でも含まれる近傍は候補どまり＝原本で確認する（同じ文書が触れているだけの言及を「影響あり」と断定しない）。経路の先の
  実際の記述を引用したいときだけ原本を開く。
- 経路が見つからない（確実な波及が0件）場合は、原本を直接開いて本文参照で1〜2箇所確認した上で
  「経路は確認できなかった（要確認）」と明示する——存在しない接続をでっち上げない。
- 影響先候補が複数に割れて経路の有無だけでは決め手がないときは、`ask_user` で起点や影響先の絞り込みを
  確認してよい（1実行につき1回まで）。
- 影響先を「すべて」求められた場合も同じ完了条件——波及経路を辿り切ってから答える（検索3回・根拠1件
  では終えない）。`graph_neighbors` の結果に `truncated:true` が付いたら近傍は部分集合（`count` が総数・
  続きは取れない）＝その範囲を未確認として明示し「すべて」「影響なし」と書かない。
- **中断**（利用者停止・通信エラー・既存の反復／情報量予算への到達）のときは、確認済みの経路・
  未確認の範囲・中断理由を分けて書き、部分結果を「すべて」「影響なし」と断定しない。

## 4. 回答の形

- **接続の有無を根拠として明示する**。向きは平易語で示す（「PGM1 が CPY1 を取り込んでいるので CPY1 の変更は
  PGM1 に影響する」「JOB1 が PGM1 を呼び出している」「参照は確認できず」等・原本で確認した箇所を添える）。
  内部のエッジ名（COPIES 等）やラベル名は本文に出さない。症状語で検索を乱発した過程は書かない。
- 波及が0件（確実な経路が見つからない）のときも「影響なし」と断定せず、「確認できた経路は無い（要確認）」
  と経路探索の限界とセットで書く。
- 確定した事実（原本で確認できた参照・呼び出しの行）と推定（経路は無いが名前の類似から
  推測される関連等）は分けて書き、推定には「推定」と明示する。
- 影響範囲を一覧で問われたら該当する全件をパス付きで列挙する（省略しない）。
- 回答では、確認した対象の一覧（全件・パス付き）と、AI の説明・分類・要約、除外した対象とその理由を分けて書く。
  検索に出ただけの候補を確認済みの対象として書かない。
- 本文の中に出典の一覧は書かない。回答の最後に『参照した資料:』の行を置き、実際に開いて根拠にした
  資料を1行1件、資料フォルダからの相対パスで列挙する。
