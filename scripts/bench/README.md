# scripts/bench — 検索速度・検索漏れの計測道具（2026-10-15 の探索実験）

実験の報告は `docs/reports/2026-10-15-検索速度と検索漏れの根本改善.md`（検索側）と `docs/reports/2026-10-15-データ構築と情報表現の再検証.md`（構築側）。ここにあるのは再現用の道具で、本番コードは変えない。

| 道具 | 何をするか |
|---|---|
| `gen_corpus.py` | 依存グラフが既知の合成コーパス（COBOL／コピーブック／JCL／設計書 MD・架空名のみ）と、影響範囲の正解 `gold.json` を作る。`--seed` 固定で決定的。 |
| `bench_grep.py` | `grep_search`（rg で候補を絞る本番経路）／素の `rg`／台帳走査（`documents_for`・`world_rel_set`）の wall と CPU を比べる。`profile` を付けると cProfile を出す。ファイル分類のキャッシュは `<corpus の親>/derived-<label>/bench/search_cache/` に残る（消すと冷えた状態、2 回目以降が温かい状態）。 |
| `impact_eval.py` | 影響範囲の解き方の比較。キーワード検索（rg）／静的グラフの逆向き閉包／両者の和を `gold.json` と突き合わせ Recall・Precision・時間を出す。 |
| `proto_grep_rg.py` | 試作: rg で候補を取り必要件数で止める grep。CP932 は非 ASCII の検索語のときだけ 2 回目を走らせる。 |
| `proto_impact_pack.py` | 試作: 影響範囲の「根拠パック」（影響先ごとに COPY/CALL/EXEC 文の path:line を機械的に付けて 1 回で返す・台帳 item の自動生成）。 |
| `preserve_check.py` | 原本（xlsx/docx/pptx）の値が rag.md のチャンク・人向け MD にどれだけ残るか（値の保持率・行のまとまり）を数える。 |
| `proto_term_bridge.py` | 試作: 設計書の表から「業務語（日本語）↔ 識別子」の橋を `identifier_mentions` から作り、日本語の問いを識別子→グラフ閉包へ落とす。 |
| `proto_dataflow.py` | 試作: COBOL/JCL から READS/WRITES（SELECT…ASSIGN ＋ DD DSN）・FLOWS_TO（MOVE/COMPUTE）を規則で抜き、ファイル経由のプログラム間の流れを追う。 |
| `bench_dataflow.py` | 本番の `build_world` で構築時間・ファイル数・流れの辺数を計測する。`python scripts/bench/bench_dataflow.py <corpus> --out <結果.json>`。平均 100 ms／ファイルを超えると終了コード 1。 |
| `ledger_eval.py` | 実 API で台帳 A/B/C を比較し、回答・活動記録・計画書・時間内訳・採点を保存する。手順は末尾。 |
| `prepare_self_corpus.py` | Sherpa 自身のソースと設計書（コミット固定）から、台帳評価の `self` 系統の資料フォルダ・manifest・正解を機械的に作る（Codex・API 不使用）。末尾の節を参照。 |
| `cpu_sampler.py` | 1 ターンの間、どのプロセスが CPU を使っているかを `/proc` から 1 秒ごとに記録する。 |

`scripts/turn_timeline.py` は本番の会話記録（`.codex-sessions/<会話番号>/`）から、実行（attempt）ごとのモデル応答待ち・MCP 道具の時間・往復回数・再送トークンを出す（DB 不要・本文は出さない）。

## 再現手順（4 コア・16GB で約 15 分）

```bash
B=/tmp/sherpa-bench; mkdir -p $B
python3 scripts/bench/gen_corpus.py --out $B/corpora/S --programs 300  --copybooks 60   --jobs 60   --docs 200
python3 scripts/bench/gen_corpus.py --out $B/corpora/M --programs 3000 --copybooks 400  --jobs 500  --docs 2000
python3 scripts/bench/gen_corpus.py --out $B/corpora/L --programs 12000 --copybooks 1500 --jobs 2000 --docs 8000
python3 scripts/bench/bench_grep.py $B/corpora/M M $B profile
python3 scripts/bench/impact_eval.py $B/corpora/M M $B
python3 scripts/bench/proto_grep_rg.py $B/corpora/L "CP0007-ITEM-01" "消費税率" "WS-COUNT"
```

本番環境で 30 分の内訳を取るには（読み取りだけ）:

```bash
# 1 ターン分の内訳（attempt ごとのモデル待ち・道具の時間・往復回数・再送トークン）
python3 scripts/turn_timeline.py data/users/<uid>/workspace/.codex-sessions/<会話番号>
# 質問を送る前に起動し、どのプロセスが CPU を使っているかを記録する
python3 scripts/bench/cpu_sampler.py --seconds 2400 --out /tmp/cpu.jsonl
```

## 評価セットの固定（S5）— テストとして固定した

2026-10-15 の報告 §1 発見 5「正解付きの評価セットが無い」への対応。道具（`gen_corpus.py`）の合成コーパスを**テスト時に生成**し、影響範囲の 40 問を gold と突き合わせる。生成物はリポジトリに入れない（`--seed 7` で決定的＝gold も毎回同じ）。採点の本体は `eval_gate.py`（テストと手元実行が共有。手元では `python3 scripts/bench/eval_gate.py [--scale S|T]`）。

| 何を | 規模 | どこで走るか | しきい値 |
|---|---|---|---|
| 静的グラフ閉包（`world_graph.build_world` の JSON から逆向きに閉包・Neo4j 無し）を gold と照合 | 合成コーパス S（300 プログラム・60 コピーブック・60 ジョブ・200 文書・40 問） | `tests/contract/test_impact_eval_gate.py`。`pytest tests/contract` のほか、CI の `make gate-ci`・手元の `make gate-merge`（単体＋契約）に含まれる | Recall 1.000・Precision 1.000（全 40 問で一致） |
| S2 の BFS 経路（`world_impact`＋`world_related_documents`）を同じ 40 問で採点 | 同じ S | `tests/integration/test_impact_bfs_eval.py`（要 Neo4j）。`gate-merge` で `AREA:integration` が出たときに走る | 深さ 30 で 1.000／1.000。既定の深さ（10）では余計 0・漏れた問いは必ず `depth_truncated` |

- 規模は S のまま。生成 0.2 秒＋`build_world` 約 2.5 秒＋採点で、契約テスト 5 本が約 3 秒（目安の 30 秒以内）なので縮めていない。M 以上は桁が違うので固定しない。
- 起点は項目名（無ければコピーブック）、影響先は COPIES/CONTAINS/INVOKES/ACCESSES の逆向き閉包、設計書は DOCUMENTS で指す文書。gold の正否は生成器が決める（AI が書いた正解ではない）。
- S の最長の依存鎖は 13 段あり、本番の既定の深さ `IMPACT_MAX_DEPTH`（10）より深い。そのため Neo4j 側は深さ 30 で正解一致を見て、既定の深さでは「余計を出さない・漏れたら `depth_truncated` を立てる」を見る（既定の深さでは 40 問中 20 問で漏れる）。

### 落ちたときの読み方

出力は問いごとに「起点（コピーブック / 項目）」・Recall・Precision と、**漏れ**（gold にあって結果に無い）・**余計**（結果にあって gold に無い）を、プログラム／ジョブ／設計書に分けて架空名で並べる（先頭 10 問・種類ごと 12 件まで）。

- 漏れが多い・同じ種類のプログラム／ジョブが全問で漏れる → 閉包が辺の種類を落としている（解析器がその辺を出さない等）。深い問いだけ漏れる → 逆向きの深さを切っている。設計書だけ漏れる → DOCUMENTS が付かない。
- 余計が出る → 辺の過剰解決（同名の取り違え・コメント中の名前を依存に数えた等）。
- `test_gold_is_meaningful` が落ちる → `gen_corpus.py` を変えた。意図した変更なら件数の下限を見直す。

### 退行の検知の確かめ方（2026-10-16 に実施済み）

一時的に閉包を壊して赤になることを確認し、元に戻す（`git checkout <ファイル>`）。

1. 解析器の辺を 1 種類落とす（Neo4j 無し）: `sherpa/ingest/world_graph.py` の `build_world` の末尾 `return list(nodes.values()), edges, flags` を、`INVOKES` の辺を除いた `edges` を返す形にする。`pytest tests/contract/test_impact_eval_gate.py` で `test_static_closure_matches_gold_exactly` が赤（Recall 0.228・40 問とも漏れ）。
2. 実 Neo4j の BFS 側で辺の種類を落とす: `sherpa/ingest/world_neo4j.py` の `_IMPACT_REL` から `INVOKES` を外す。`pytest tests/integration/test_impact_bfs_eval.py` が 2 本とも赤（Recall 0.228）。

採点関数だけの検知は `test_dropping_an_edge_kind_turns_the_score_red`（INVOKES／COPIES を落としたグラフで Recall < 1）が常時見ている。

### v1 に対する「人が確かめた 10 問」を足す場所

合成コーパスの gold は生成器由来で、実物の揺れ（固有の書き方・表記ゆれ）は測れない。`fixtures/corpus/v1` に対して人が根拠を確かめた 10 問は、`tests/contract/test_impact_eval_v1_human.py` として足す（問い・起点・期待する影響先は fixtures 側の JSON に人が書き、`eval_gate.score_question` と `format_report` で同じ形式の差分を出す）。AI が作った答えをそのまま正解にしない。まだ無い。

## 台帳 3 構成を実 API で比べる

`ledger_eval.py` は専用 Compose project `sherpa-ledger-eval` の Postgres/ES/Neo4j と、ホストの Sherpa API を使う。
認証・設定・資料フォルダ登録/取り込み・チャットは通常の API を通す。Codex CLI は 0.162.1、モデルは
`gpt-5.6-sol`、深さは `standard`。A の見直し上限は既存の 2 回、B/C は 1 回。
埋め込み接続先を環境から継承せず、中央設定も空へ戻して DB を読み返し、`SHERPA_DISABLE_EMBED=1` と合わせて BM25 のみにする。

データは先に外部で準備する。登録ルートのトップフォルダを `code/`・`multihop/`・`jemhop/` とし、
manifest・正解・取得ログはその外に置く。`corpus_files` は登録ルート内の全ファイルを列挙する。
質問順は manifest の順番のままで、smoke は各系統の先頭 1 問、pilot は先頭 2 問を使う。

```json
{
  "seed": 7,
  "corpus_root": "/absolute/eval/datasets/converted/corpus",
  "corpus_files": ["code/src/example.cbl", "multihop/article.md", "jemhop/article.md"],
  "questions": [
    {
      "id": "code-01", "system": "code", "question": "架空の項目を変えた影響は？",
      "expected_paths": ["code/src/example.cbl"], "hop": 1, "scope_paths": ["code"]
    }
  ]
}
```

`system` は `code`/`multihop`/`jemhop`。`scope_paths` 省略時は系統名のフォルダに固定。
`answer`（参考値の文字列一致）・`null`（該当なし問題）・`type` は任意。本番は 10/15/10 問が必須。
コーパス本文の SHA-256、manifest、実装コミット、ポート等を計測条件に含め、条件違いの結果混在を拒否する。
計測 checkout は追跡対象に未コミット変更がない状態へ固定し、準備・API 起動・試行/再利用の直前と試行完了時にも
変更と HEAD の一致を確認する。未追跡ファイルには触れない。pilot 後の報告書は別 checkout で編集する。
実測した `activity.settings` の mode が `standard`、schema_level が `2`、モデル/深さ/台帳構成が指定どおりであることも検査する。

```bash
python scripts/bench/ledger_eval.py all \
  --manifest /absolute/eval/datasets/converted/manifest.json \
  --out /absolute/eval/results/YYYYMMDD-HHMM \
  --allow-schema-bootstrap
```

この 1 コマンドでストア準備→通常 API 起動→資料取り込み→smoke 9 試行→pilot 18 試行まで進む。
`pilot-estimate.json` の全 105 試行の見込みを報告書冒頭に記載してから本番へ進む。

```bash
python scripts/bench/ledger_eval.py run \
  --manifest /absolute/eval/datasets/converted/manifest.json \
  --out /absolute/eval/results/YYYYMMDD-HHMM --estimate-recorded
```

`prepare`/`smoke`/`pilot`/`run`/`retry`/`summarize` を個別にも指定できる。同じ条件の pilot の完答・製品の部分回答の試行は
本番で再利用し、二重計上しない。smoke は別保存。利用枠・429・CLI 終端の利用枠エラーを検知したら終了コード 3 で止まり、
その出力フォルダは `stopped.json` が残る間は新規試行を始めない。基盤障害・原因未確定が `--max-infra-streak`（既定 2）件続いたら
終了コード 4 で止まる。その他の失敗は終了コード 2。

### 試行の分類（採点から外す・残す）

`result.status=ok` や API の `codex_error_code` の空だけでは判定しない。保存回答の `completion`・`stop_kind`・通知（`notices`）、
CLI 終端の診断（親 rollout 末尾の `task_complete.error`、`server_overloaded`／`Selected model is at capacity`）、ランナーのログの
CLI 終了コードを順に見る（規則は `ledger_scoring.classify_trial`）。

| 分類 | 意味 | Recall/Precision |
|---|---|---|
| 完答(a) `complete` | 保存回答が完了で、CLI 終端にエラーなし | 入れる |
| 製品部分(b) `product_partial` | CLI は正常終了し、見直しの巻き戻し（`review_reverted`）・調査未完了などの部分回答 | 入れる（完答と別に件数を出す。「aのみ」の列もある） |
| 基盤障害(c) `infrastructure` | 容量（`server_overloaded`）・サービス失敗・CLI 異常終了・ランナー失敗・利用枠 | 入れない。時間は「障害秒」として残す |
| 停止(d) `stopped` | 親からの停止（`interrupted`）・未完のまま残った記録 | 入れない（停止秒を別に出す） |
| 原因未確定 `unverified` | 回収された部分回答だが CLI 側の証拠が無い・保存回答なし | 入れない。`--only unverified` で再試行できる |

道具の回数は CLI 外側の `activity.agents[].tools`（コード実行は `exec` に包まれる）ではなく、サーバ側の `answer.call_stats.tools` を
役割別から道具名で合算して出す。外側の回数とは加算せず、`calls` の合計と `missing=0` を照合できた試行だけを「観測が完全」として
ES回数の平均に入れる。`call_stats` が無い試行の ES回数は 0 ではなく欠測。結果の行には `server_tool_counts`・`server_tool_roles`・
`tool_observation_complete` が入る。

### 保存済みの結果を分類し直す（読み取り専用）

```bash
python scripts/bench/ledger_reclassify.py --results /absolute/eval/results/YYYYMMDD-HHMM \
  --out /absolute/eval/results/YYYYMMDD-HHMM/analysis/reclassify-YYYYMMDD
```

`--phase-dir smoke` で動作確認の分も出せる。元の `scores.jsonl`・`summary.md`・`result.json` は読むだけで、`--out` には
`classification.jsonl`・`scores.jsonl`・`summary.md`・`behavior.md`・`reclassify-meta.json` を**新規ファイルとして**書く（既存があれば停止）。
モデル・API・Docker は使わない。

### 基盤障害・停止だけを再試行する

```bash
python scripts/bench/ledger_eval.py retry \
  --manifest /absolute/eval/datasets/converted/manifest.json \
  --retry-from /absolute/eval/results/YYYYMMDD-HHMM --only infrastructure,stopped
```

`--only` の既定は `infrastructure,stopped`（`product_partial`・`unverified` も指定できる）。元の試行の記録は動かさず、
再試行は同じ質問フォルダの `retries/<試行番号>/`（2 以降）に別の試行として保存する。集計は質問ごとに**最新の有効な試行**
（完答または製品の部分回答）を使い、無ければ最新の試行の分類で数える。障害・停止の試行の時間は再試行で解消した後も
障害秒に残る。記録の無い質問は新規に走らせない（未実施ぶんは通常の `pilot`/`run` で進める）。

- `--retry-from` は出力フォルダ自体で、`--out` は省略するか同じ値にする。
- `metadata.json` の版の指紋（実装 HEAD・CLI の版とハッシュ・モデル・資料と質問の fingerprint・ポート等）が現在の条件と違うと、
  混ぜずに違う項目名を示して停止する。その場合は新しい `--out` で新規に計測する。
- 利用枠の入口: 前回の停止記録 `stopped.json` があると停止する。枠のリセットを利用者が確認したら `--clear-usage-stop` で
  別名（`stopped-<番号>.json`）へ退避して解除する。さらに、最後に観測した rollout の利用枠に到達フラグ
  （`rate_limit_reached_type`・`spend_control_reached`）があり、リセット時刻前なら停止する。使用率の高さだけでは止めない。
  各試行の後も到達フラグを見て、立っていれば次の試行を始めない。

新しい出力先の `smoke` または `pilot` に `--allow-schema-bootstrap` を付けると、prepare も自動で行う。
例えば上の `all` を `smoke` に替えた 1 コマンドで、ストア起動から API・登録/取り込み・質問・保存/採点まで進む。
承認記録が既にあれば検証して使い、承認フラグも承認記録もなければ API を起動しない。

出力は `metadata.json`、`scores.jsonl`（本番/再利用 pilot を 1 問 1 行）、`summary.md`（構成/系統/hop 別）、
`behavior.md`、`trials/<style>/<system>/<id>/` の回答/会話/活動記録/計画書/stats/時間内訳/`classification.json`（分類と根拠）、
再試行は `retries/<試行番号>/`。
本文と参照資料のパス集合を正解と比較し、非正解・架空パスも Precision の分母へ入れる。
母数 0 の Recall/Precision と欠測値は `null`、集計は項目ごとの有効件数を併記する。
見直し計数は A=台帳の見直し記録件数、B=Sherpa の見直し要求数、C=計画書の見直し記録件数。
A の最終点検要求数、B の追加あり回数は別欄に残し、設定上限や実施件数と混同しない。
spawn、作業宣言の自動継続、実際の resume、入力 token、道具別回数も残す。
生 rollout の本文・認証ファイルは複製せず、既存 `turn_timeline.py` による内訳だけ保存する。

Docker を含むコマンド全体を通常の昇格経路で実行する。既定ポートは PG 15433、Neo4j 17475/17688、ES 19201、API 18072。
スキーマ適用先は `localhost:15433` の **`sherpa_ledger_eval` のみ**。`prepare` は初回作成/再起動時の API の
冪等 DDL の承認を得た後に `--allow-schema-bootstrap` を付ける。通常の `sherpa` や業務 DB は使わない。
project ラベル・ポート・DB 名・所有記録を確認し、所有記録のない DB に資料/会話があれば停止する。
ストア・評価データの削除や `down -v` は行わない。認証は既存 Codex ChatGPT ログインを使い、評価専用の
Web ログイン資格情報は `runtime/admin-private.json`（0600）に置く。端末や報告書へ内容を出さない。

### API キーで回す（ChatGPT の週の利用枠を使わない）

既定は ChatGPT ログイン・`gpt-5.6-sol`。API キーで回すときは、評価専用の `CODEX_HOME` と `--model` を渡す。

1. 費用の上限を**先に**決める。OpenAI の管理画面で、この評価専用のプロジェクト（または API キー）に月額の上限を設定する。
   予備計測（`pilot`）の `pilot-estimate.json` の `tokens`（試行ごとの新規入力・キャッシュ入力・出力と、全体の見込み）が費用の見込みの材料になる。
   本番（`run`）は、この見込みを単価に掛けて上限内に収まることを確かめてから始める。
2. 評価専用の `CODEX_HOME`（例 `/Users/sanomsf/work/sherpa-eval/codex-home-api`）を作り、**利用者が自分で**
   `CODEX_HOME=<そのフォルダ> codex login --with-api-key` を実行して API キーでログインする。ランナーは認証ファイルの
   存在を確かめるだけで、中身は読まず、表示も複製もしない。個人の `~/.codex` は使わない（`--auth-mode apikey` では `--codex-home` が必須）。
3. 次のように渡す。`--model` は本体と下調べ役（worker）の両方に設定され（管理画面の設定 `codex_worker_model` と使えるモデルの一覧を
   評価 API へ入れる。製品コードは変えない）、実行後の `activity.settings.model` もこの値と一致することを検査する。

```bash
python scripts/bench/ledger_eval.py pilot \
  --manifest /absolute/eval/datasets/converted/manifest.json \
  --out /absolute/eval/results/YYYYMMDD-HHMM-api \
  --model gpt-5.5 --codex-home /Users/sanomsf/work/sherpa-eval/codex-home-api --auth-mode apikey \
  --allow-schema-bootstrap
```

`metadata.json` の版の指紋にはモデル名と、**明示した** `--auth-mode`（`chatgpt`／`apikey`）が入る。認証ファイルの中身から判定することはない。
ChatGPT 認証の既存の出力フォルダに `--model` や `--auth-mode` を変えて続けると、指紋が違うと停止する。別の `--out` を使うこと。
`--codex-home` の場所は指紋に入れず `metadata.json` に記録だけする。

### Sherpa 自身を質問セットにする（`self` 系統）

`prepare_self_corpus.py` が、Sherpa のソースと設計書（コミット固定）から資料フォルダ・manifest・正解を機械的に作る
（`--out` は新しい空のフォルダ。リポジトリの外）。作ったものはランナーに `--manifest <出力>/manifest.json` で渡す。
`self` は他の 3 系統と別の manifest で評価する（問数は manifest から数える）。`needs_user_review` の付いた問い（食い違い型の正解）は、
利用者の確認が済むまで既定で評価から外れる。確認後に含めるときは `--include-unreviewed`。
一覧型は件数が多いほど Recall が下がりやすく、Python の解析器が無いためグラフはほぼ使われない（manifest の `scoring_notes`）。
