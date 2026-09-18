# CLAUDE.md — 業務AI基盤

現行設計の入口は **`docs/01-現行設計の入口.md`**（着手前に一読・正典/as-built へ誘導）。
このファイルは Claude Code が毎セッション守るべき「振る舞いの契約」。

## プロジェクト概要

社内業務ドキュメントを対象に、Codex エージェント中核の Agentic RAG で
検索・分析・改修の影響範囲調査・ドキュメントチェック・トラブルシュートを行う基盤。

スタック: WSL / FastAPI / Codex CLI / OpenAI API / Ollama / Elasticsearch / Neo4j / RDB

**コンセプト（決定2026-09-19）**: Sherpa は**ソースが神様**として検索し回答する。新規開発 PJ 向けの RAG
というより、保守運用・改修など**すでにあるシステム**で最大限効果を発揮するアプリ。設計書とソースが
食い違えばソースを正とし食い違いを報告する。評価（見直し）は根拠の量でなく必要な根拠種別（ソースは常に
必須）の充足で判定する。グラフ・ES が不調でもソース直読（grep／原本読取）で回答する＝グラフ不調は回答
不能の理由にしない。一次情報は内部リポの提案書（2026-09-19・非公開）。

## 厳守ルール（振る舞いの契約）

### ファイル/ディレクトリ
- 登録ディレクトリ（world）配下（本番 `data/kb/{world}/…`）は **読み取り専用**。編集・削除・移動を絶対にしない。
  （フォルダ木＝範囲そのもの＝鏡。Office/PDF系は決定的MD、cobol/jcl/copybook は plain text・MD化しない）
- ただし dev の `fixtures/corpus/**` は**テストデータ＝再構成・変更可**（制約はテストが緑のままであることだけ・決定2026-06-28）。本番の READ-ONLY と混同しない。
- 書き込みは `users/{user_id}/workspace/` のみ。成果物は `workspace/outputs/` に出力する。
- 個人アップロード（`workspace`）は **grep のみ**。ベクトル/グラフ RAG に索引化しない。

### 検索・経路
- 経路は依頼内容で選択: 直接 grep（共有 `md/` ＋ ソース原文 `src/` ＋ 個人 `workspace/`）/ RAG-ES / RAG-Neo4j。
- RAG（ES・Neo4j）は **共有 KB のみ**。個人ファイルは RAG の引用元に出さない。

### 範囲スコープ（**取り込みモデルは `docs/03-鏡モデル.md` が正典**・決定2026-06-28）
- **「ディレクトリの鏡」**＝登録ディレクトリ全体が**1つの世界**（1グラフ＋1 ES インデックス）。フォルダ木が**そのまま範囲**。
- **範囲はフォルダのフィルタ**（どの階層でも＝部分木を1世界として検索/分析）。グラフも ES も同じフィルタ。
- **同一性＝パス**（複製は別ノード）。**リンク（COPY/CALL）＝同世代（トップフォルダ）内の最近傍**・世代跨ぎなし。同名は「対応」関係で表示。
- **即反映（ライブ鏡）**：更新＝置換／削除＝消える。**「版」概念はアプリから撤去**（世代＝トップフォルダ／履歴・差分は Git は検討事項）。
- Neo4j は **world 単位**（論理1世界＋フォルダフィルタ）。物理分離は将来の最適化。※旧「版」中心表現は MIRROR-MODEL に置換済。

### モデル分担
- メイン = OpenAI API、サブ = ローカル LLM（`--oss`）。
- 検索/RAG クエリ生成はサブが下書き → メインが検証 → 実行。

### 会話・共有
- 会話履歴の正本は DB。各会話に Codex `session_id` を紐付け、`resume` で継続。
- 共有はログイン必須＋招待ユーザーのみ＋閲覧専用＋有効期限。

### コメント規律（決定2026-08-22）
- コードコメントは**現在の契約・不変条件・非自明な理由のみ**。修正の経緯・RV番号・実装日は Git/PR へ、一般化できる教訓は `docs/17-開発の教訓.md` へ溜める（既存の履歴コメントは段階整理中＝新規コードから本規律）。

### 削除
- 削除は `documents` 台帳を辿り、**原本（`uploads/` or ソース `src/`）・MD・ES・Neo4j** の派生物まで一括伝播。

### セキュリティ
- メイン推論は外部 OpenAI。**テキスト送信は可だが OpenAI にファイルを永続化しない**
  （Files API 不使用、本文テキストのみ）。区分によるローカル振り分けは将来拡張。
- 一時 embed（チェック時）は**共有 KB インデックスに書かない**。クエリベクトルのみで
  照合 → 破棄し、個人文書を RAG の引用元に出さない。
- 鍵・トークン・`.env` の内容を端末に出さない（cat/echo 禁止）。確認は長さ/接頭辞か実呼び出しのステータスコードのみ
  （PreToolUse フック `scripts/claude-hooks/deny-secrets.sh` が機械的に拒否する）。

### 作業委譲（Claude Code サブエージェント運用）
- タスク実行は適切な粒度で `.claude/agents/` のサブエージェントへ委譲（self-contained な指示＝対象・既存規約・受け入れ条件・やらないこと）。
  メインは**立案・契約管理・検収**（diff レビュー＋作成物ごとの Codex RV）。自己判断の例外は可。
- モデル3段: コーディング＝`feature-implementer`（Sonnet 既定）／軽微・機械的＝`light-task-runner`（Haiku）／
  文書作成＝`doc-writer`（Sonnet）。**Opus は設計判断が濃い例外のみ**（使ったら理由を報告）。迷ったら軽い方から。
- 対象ファイルが重ならなければ**並行実行**。サブエージェント/テストは10分級前提で**一発完走**（細切れの待ち・ポーリングをしない）。
- 並列レーンは `lane` スキル（`.claude/skills/lane/`）で起票→委譲→検収→合流する。サブエージェントの完了報告は
  参考情報＝検収はメインが `make gate-slice BASE=main` を自分で走らせた出力だけを根拠にする（自己申告・スモーク数値は根拠にしない）。

### 開発の型（正典＝`docs/20-開発ハーネス.md`）
- ループ: 提案書→スライス→委譲→検収→敵対 RV（1巡以上・台帳）→最小是正→段階ゲート→マージ→現在地への追随→
  変わった前提を書いて次スライスを再考。
- 敵対 RV の前提3点（閉域LAN・最大20人・単一worker）・指摘の3分類（契約違反/データ破壊/セキュリティ＝採用・
  エッジは受容記録が既定・改善案は却下）。
- テスト規範（要約）: 新規は実害の再現に限る・モックは外部境界だけ・単体テスト全件は壁時計上限あり。
- 2エージェント同一チェックアウト禁止＝**worktree 分離**。Agent の worktree は `origin/main` 基点＝`make hooks`
  の post-checkout でローカル main へ揃える（レーン指示でも `git merge-base main HEAD` を確認）。詳細は 20 を参照。

## 正典・詳細への入口（契約の根拠）

- **取り込み/範囲/同一性の正典＝`docs/03-鏡モデル.md`**（登録ディレクトリ＝1世界・範囲＝フォルダ prefix・同一性＝パス・即反映・
  「版」概念は撤去。**言及エッジ（`DOCUMENTS via="mention"`・辞書突合で Document→コードを木を跨いで繋ぐ）は
  構造リンクの世代内限定規律の制度化された例外**＝`docs/05-グラフ語彙.md` §2/§5 参照）。
  実装＝`ingest/world_graph.py`／`ingest/world_neo4j.py`／`worlds.py`／`scope.py`。
  **退役した概念・API（版ライフサイクル・旧 canonical_id の版修飾・auto-scope 推定/要確認(scope)・`versions`/`merge`/`neo4j_load`/`semantic`・
  検索接続前の並走chunk系統＝`search_render`/`chunker`/`status_semantics`・`.search_blocks.jsonl`/`.chunks.jsonl`・
  OCR 観測の検索専用描画＝`observation_render.render`/`render_many`・`.rag_observations.md`/`.rag_observation_chunks.jsonl`
  （O1 で rag.md へ統合済み・grep も観測ツリーを直接走査しない。`observation_render.py` 自体は
  `.ai_observations.jsonl`＝`office_md._load_ocr_observation_sets` が読む Observation Set 本体の
  永続化・世代管理として現役）・**rag/legacy 系統切替トグル**（`SHERPA_SEARCH_RAG_GREP`／
  `SHERPA_SEARCH_RAG_ES`／`SHERPA_ES_PARENT_RETURN`・rag_chunks 破損時の per-file legacy 縮退は
  別契約のため対象外）・**`usage_metering`**（system_settings フィールド・env `SHERPA_USAGE_METERING`・
  `metering.enabled()`）・**`SHERPA_EXEC_EVENT_V2`**（v1 平坦ログへの退避分岐）・
  **`SHERPA_AGENTIC_EVIDENCE_VERIFY`**（機械検証の明示 OFF 退避口）
  （TOGGLE-RM・2026-09-03＝実益のない切替の概念ごと撤去・常時ON化）・
  **意味層 LLM 抽出（`graph_extract.extract_world`/l_extract）・`concept_propose`/`auto_bridge`/`concepts.json`・
  `REALIZES`・概念ラベル7種（`Parameter`/`BusinessRule`/`Function`/`Screen`/`Report`/`Standard`/`Incident`）と
  概念エッジ7種（`USES`/`REFERENCES`/`IMPLEMENTED_BY`/`PRODUCED_BY`/`CONFORMS_TO`/`RELATES_TO`/`REALIZES`）・
  確実/要確認判定（ノードの `extraction_method` による信頼度分岐・影響一覧のフィルタチップ/行バッジ）**
  （GRAPH-SRC・2026-09-04＝ソース正典化で撤去・復活させない）は復活させない。**
- **画面の最上位原則＝`docs/04-画面の原則.md`**（タスク起点・専門用語ゼロ・回答末尾に出典＝原本DL）。利用者UC（UC-1〜3）は全て
  **会話で依頼→会話に答え**（`docs/12-ユースケース.md`）＝チャットが主入口。初期 MVP・縦切りの経緯は内部リポの文書（非公開・履歴）。
- **GraphRAG オントロジー＝`docs/05-グラフ語彙.md`**（網羅性優先＋経路を返す／クローズド語彙／決定的構造のみ事前計算・意味の解釈はクエリ時のエージェント）。影響分析の抽出・クエリはこれに従う。
- **as-built／現在地**: 認証/共有/監査/個人workspace＝`docs/06-認証と個人領域.md`／データモデル＝`docs/07-データモデル.md`／
  実装現在地＝実装の現在地（内部リポの文書・非公開。チェックはコードに遅れる＝コードで確認）。決定経緯は内部リポの引継ぎ文書（非公開）。
- KB は全社1つ＋利用者登録可（将来：管理者/部門スコープ）。テキスト送信可・ファイルは OpenAI に残さない・チェック時の一時 embed は破棄（上の「セキュリティ」節が契約）。
