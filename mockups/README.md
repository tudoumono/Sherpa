# UI モック（確認用）

要件を目で確認するための静的 HTML モック。ビルド不要、ブラウザで開くだけ。
VSCode なら各 .html を右クリック →「Open with Live Preview」、または既定ブラウザで開く。

> **注**: これらは M7 期のモックで、実画面は M9/M10 で置換済み。**「版セレクタ／版管理」を描くモック**
> （`admin-versions.html`・`chat-impact-analysis.html` の R1・`file-manager.html` の FM3）は**版モデル時代**のもので、
> 鏡モデル（正典 [../docs/03-鏡モデル.md](../docs/03-鏡モデル.md)）では「版」概念は撤去され**取込ディレクトリ（world）＋範囲＝フォルダ prefix**に置換されている。

> **[design-samples/](design-samples/)** ＝ フロントデザイン洗練の**方向性選定用サンプル3案**
> （2026-07-02・入口は design-samples/index.html・本番 web/ 未反映）。

| ファイル | 画面 | 確認できる要件 |
|----------|------|----------------|
| [chat-impact-analysis.html](chat-impact-analysis.html) | チャット / UC-1 影響範囲分析 | R1 版セレクタ・R2 経路チップ・R4 影響一覧二段・R5 経路展開＋確信度（●確実/○要確認）・R6 参照DL・R8 トレース |
| [chat-doc-check.html](chat-doc-check.html) | チャット / UC-2 ドキュメントチェック | R9 添付・R10 対象文書ビューア＋指摘ハイライト・R11 機密モード・R12 採否・R13 一時embedライフサイクル・R14 根拠双方向 |
| [chat-scope.html](chat-scope.html) | チャット / UC-4 スコープ参照（Phase2） | R21 参照範囲セレクタ・R22 参照中ソースパネル・R23 スコープ強制（KB部分木／workspaceとは別軸） |
| [thinkflow-requirements.html](thinkflow-requirements.html) | ThinkFlow 風チャット / 要件整理 | 左ナビ・中央チャット・右カラムの処理ステートマシン付きビジュアルタイムライン・再生バー |
| [file-manager.html](file-manager.html) | ファイル管理 | FM1 添付TTL＋削除・FM2 MD化ステータス＋読取専用バナー・FM3 版選択／静的解析ステータス |
| [admin-usage.html](admin-usage.html) | 管理 / UC-A1 利用統計ダッシュボード | A1 期間・A3 KPI・A4 推移・A5 経路/UC内訳・A6 コスト/予算・A7 利用者ランキング・A8 KB参照・A9 成果の質 |
| [admin-ingest.html](admin-ingest.html) | 管理 / UC-A2 KB取り込み管理 | A12 トラック別進捗・A14 失敗再実行・A15 静的解析判定＋Lフォールバック・A16 バルク＋重複・**A17 抽出プレビュー（名寄せ＝精度の品質ゲート）** |
| [admin-versions.html](admin-versions.html) | 管理 / UC-A3 版管理（＋UC-A4 削除プレビュー） | A18 状態バッジ・A20 凍結/アーカイブ＋影響・A21 最新alias付替え・A22 Neo4j版DB・A23 削除プレビュー（dangling evidence）・A24 二段階確認 |
| [admin-users.html](admin-users.html) | 管理 / UC-A5 ユーザー管理 | A27 role編集・A28 部門スコープ（将来）・A29 無効化 |
| [admin-cost.html](admin-cost.html) | 管理 / UC-A6 コスト・APIキー運用 | A30 予算＋アラート・A31 部門別按分・A32 キーローテ（値非表示）・A33 機密比率＋外部送信ガード |

> 管理画面はサブナビ（利用統計／KB取り込み／版管理／ユーザー／コスト・キー）で相互に行き来できます。

## 操作できる箇所
- 影響/チェックカードの「全画面ビュー」ボタン → 結果オーバーレイ
- 影響一覧: 確信度フィルタ／行展開で依存チェーン表示
- ドキュメントチェック: 対象文書のハイライト ⇔ 指摘カードが連動、採否トグル、種別フィルタ
- ファイル管理: フォルダツリーをクリックで表示切替（読取専用バナー／TTL が変化）
- 右ペインのタブ切替（実行トレース／参照）

## 対象幅（デスクトップ前提）
これらは**業務デスクトップ向けモック**で、`min-width:1024px`（狭幅は横スクロール）を宣言済み。
スマホ/狭幅の本格対応（サイドバーの drawer 化・カード縦積み・表の横スクロール枠）は**実装フェーズの課題**
（Phase 1 以降）。モックはデスクトップでの確認用。

## デザイン
配色・角丸・フォントは各ファイル先頭の `:root` トークンに集約。ここを変えれば一括リスキン可能
（現状: Sherpa＝落ち着いたティール accent `#0d9488`、確実=緑 / 要確認=橙 / 規約違反=赤）。
