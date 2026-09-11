---
name: lane
description: 並列レーン開発（複数サブエージェントを worktree で走らせ、メインが検収して main へ合流する）を型どおりに回す。起票→委譲→検収→合流の定型手順を機械化し、サブエージェントの自己申告を検収の根拠にしない。
---

並列レーン開発の実行手順。`docs/20-開発ハーネス.md` §2（役割と入出力）・§5（ゲート）・§8（作業の作法）が
正典——本スキルはその型を機械化した入口であり、判断基準（役割の入出力契約・ゲート段階・worktree 分離）は
正典側にある。

## 前提

- 実装の委譲は worktree 分離で行う（複数のエージェントが同じチェックアウトで同時に作業しない）。
- **サブエージェントの完了報告は「何を回したか」の参考情報に過ぎない**。検収の根拠は、メインが各 worktree で
  自分自身が実行したゲートの出力（要約行＋終了コード）だけである。
- 対象ファイルが重なるレーンは直列で回す（同時に触ると衝突が検収を汚す）。

## 手順

**① 起票**

レーン定義を短く固める。

- レーン名: `[a-z0-9-]`。
- ブランチ名: `<topic>/<lane>`。
- 対象ファイル: 触ってよい範囲。
- 受け入れ条件: 実行するコマンドと期待する結果。
- やらないこと: スコープ外の明記。

対象ファイルが重なるレーンが複数あれば、直列の順序を先に決めておく。

**② 準備**

Agent ツールを `isolation: "worktree"` で起動する。委譲文の冒頭には必ず基点確認の手順を入れる
（③のプロンプトが自動で組み込む）。

```
git merge-base main HEAD    # ← git rev-parse main と一致しなければ基点が古い
git rev-parse main
# 不一致なら:
git reset --hard main
```

**③ 委譲文の生成**

```
.claude/skills/lane/scripts/lane_prompt.py \
  --lane <レーン名> \
  --branch <topic>/<lane> \
  --goal "<このレーンの目的>" \
  --files "<対象ファイルの説明>" \
  --accept "<受け入れ条件>" \
  --avoid "<やらないこと>" \
  [--conventions "<既存規約への参照>"] \
  [--tests "<回すべきテストの指示>"]
```

標準出力に日本語の定型委譲文が出る（同じ引数なら同じ文字列＝決定的）。この文字列をそのまま Agent の
prompt として渡す——基点確認・報告形式（必須節）・コミット規約（`Co-Authored-By: Claude Fable 5.1
<noreply@anthropic.com>` を含む1コミット）・テストの作法（モックは外部境界だけ・テストを外して緑に
しない）は `lane_prompt.py` が自動で組み込むので、呼び出し側が毎回手で書く必要はない。

**④ 検収（サブエージェントの報告を根拠にしない）**

メインが各 worktree で自分自身のシェルから直接実行する。

```
make gate-slice BASE=main
```

**この出力（要約行＋終了コード）だけを検収の根拠にする。** サブエージェントの完了報告に書かれた
テスト結果・SMOKE 数値は「何を回したと申告しているか」の参考にとどめ、それ自体を緑の証拠として
扱わない。ゲートが赤なら、報告の内容に関わらず未完了として差し戻す。

**⑤ 収束表**

```
.claude/skills/lane/scripts/lane_status.sh
```

出力される Markdown 表をそのまま会話に貼る。列は「レーン(ブランチ) / パス / 基点=main? / ahead/behind
main / 変更ファイル数 / 未コミット件数 / 状態」。**最終ゲート結果は表に出ない**（④でメインが実行した
出力を別途貼る）。状態が「要rebase」のレーンは、合流前にそのレーン側で `git rebase main` してから
`make gate-slice BASE=main` を再実行する。「衝突」のレーンは自動解決せず、⑥までブロックとして扱う。

**⑥ 合流**

収束表で衝突が少ない順に処理する。

```
git merge --no-ff <branch>
```

rebase が要る場合（状態が「要rebase」）は、まず対象 worktree 側で `git rebase main` し、
`make gate-slice BASE=main` を再実行してから合流する。衝突は自動解決しない——衝突が出たレーンは表に
「ブロック」として残し、人が解消してから改めて④からやり直す。合流後は `rv` スキルで敵対 RV を回す
（RV は正典どおり `docs/20-開発ハーネス.md` §3・§4）。

**⑦ 後片付け**

```
git worktree remove <path>
git branch -d <branch>
```

未処理（作業中・衝突・要rebase）の worktree は消さない。

## やらないこと

- 自動マージ（衝突検出なしでの `git merge` 連打）はしない。
- 衝突の自動解決はしない（⑥のとおり人が解消する）。
- テスト実行の自動並列化はしない——`scripts/gate-lane.sh` のスロット上限（同時実行数2本・
  `scripts/lib/gate_common.sh::GATE_LANE_SLOT_LOCKS`）をそのまま使い、これを迂回する仕組みを
  本スキルには足さない。

## 引数・選択の要点

- `lane_prompt.py` の全引数は必須（`--conventions`・`--tests` を除く）——self-contained な依頼文に
  するため、目的・対象・受け入れ条件・やらないことを毎回明示的に書く。
- `lane_status.sh` は読み取り専用（`git worktree list --porcelain` と `git merge-tree --write-tree`
  のみ使う・working tree も ref も変更しない）。main 本体と `tmp/worktrees/rv-*`（RV 用の切り離し
  worktree）は表から除く。
- 検収の根拠は常にメインが自分で回したゲートの出力——「サブエージェントが緑と言った」は根拠にならない。
