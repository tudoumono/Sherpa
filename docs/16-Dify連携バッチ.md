# Dify KB 取り込みバッチ

Sherpa の知識パイプラインとは別に、ローカルフォルダを Dify の Knowledge Base API へ直接登録するための単体バッチ。

> **位置づけ（2026-06-30）**: 本書は**手動の一回限りバッチ**（パイプライン外）。**今後のフューチャー**として、
> **Dify を自己ホストでインストール**し、**ES と同じ要領で取り込み時に Dify KB へも自動追加**する
> 統合（`sherpa/dify_kb.py` を `sherpa/es_index.py` 相当にして `sherpa/ingest/worker.py` の
> 取り込み/削除フックに並行で差す）を計画している。

## 方針

- Dify の Knowledge Pipeline API は使わない。
- フォルダ境界ごとに Dify Knowledge Base を作成/再利用する。
- ファイルは Dify の `Create Document by File` API で登録する。
- 既定では入力ルート直下の第1階層ごとに KB を切り、配下ファイルを再帰登録する。
- 同名ファイル衝突を避けるため、Dify 上の文書名は既定で KB 内相対パスを `__` で連結した名前にする。

## 前提

環境変数に Dify の Knowledge Base API キーと API ベースURLを設定する。

```bash
export DIFY_API_KEY='...'
export DIFY_BASE_URL='https://api.dify.ai/v1'
```

self-hosted の場合は `DIFY_BASE_URL` を対象ホストの `/v1` まで含めて指定する。

## 使い方

まず dry-run で、どのフォルダがどの KB になるか確認する。

```bash
python3 scripts/dify_kb_import.py /path/to/import-root --dry-run
```

登録を実行する。

```bash
python3 scripts/dify_kb_import.py /path/to/import-root \
  --dataset-prefix 'Sherpa-' \
  --permission all_team_members \
  --wait \
  --state-file /tmp/dify-kb-import.jsonl
```

## KB の切り方

既定は第1階層ごと。

```text
import-root/
  sales/a.pdf       -> KB: sales
  sales/spec/b.pdf  -> KB: sales
  ops/runbook.md    -> KB: ops
```

第2階層までを KB 境界にする。

```bash
python3 scripts/dify_kb_import.py /path/to/import-root --dataset-depth 2
```

ファイルが入っている各フォルダを KB にする。

```bash
python3 scripts/dify_kb_import.py /path/to/import-root --leaf-datasets
```

全ファイルを1つの KB に入れる。

```bash
python3 scripts/dify_kb_import.py /path/to/import-root --dataset-depth 0
```

## 既存文書の扱い

既定は同じ文書名が Dify 側にある場合はスキップ。

```bash
# スキップ
python3 scripts/dify_kb_import.py /path/to/import-root --on-existing skip

# 既存文書を更新
python3 scripts/dify_kb_import.py /path/to/import-root --on-existing update

# 重複作成を許可
python3 scripts/dify_kb_import.py /path/to/import-root --on-existing create
```

## 対象ファイル

既定対象は一般的な文書/テキスト系拡張子のみ。

```text
.csv .docx .htm .html .json .jsonl .md .markdown .pdf .pptx .rtf .txt .xlsx .xml
```

全ファイルを Dify に渡す場合:

```bash
python3 scripts/dify_kb_import.py /path/to/import-root --extensions '*'
```

glob で絞る場合:

```bash
python3 scripts/dify_kb_import.py /path/to/import-root \
  --include 'docs/**/*.pdf' \
  --exclude '**/draft/**'
```

## メタデータ

既定で各 KB に以下の metadata field を作り、登録後の文書へ付与する。

- `source_relative_path`
- `source_folder`
- `source_dataset_key`

不要な場合:

```bash
python3 scripts/dify_kb_import.py /path/to/import-root --no-metadata
```

## 主要オプション

```text
--indexing-technique high_quality|economy
--doc-form text_model|hierarchical_model|qa_model
--doc-language Japanese
--embedding-model ...
--embedding-model-provider ...
--wait
--poll-interval 5
--wait-timeout 900
--state-file /tmp/dify-kb-import.jsonl
```

## 注意

Dify の Knowledge Base API キーは、同じアカウント配下の可視 KB を操作できる権限を持つ。リポジトリにキーを置かず、環境変数や実行基盤のシークレット管理から注入する。
