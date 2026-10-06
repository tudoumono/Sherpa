# 完全オフライン（閉域）向け配布資材キット

このページは、インターネットに一切出られない閉域ネットワークで Sherpa を動かすための手順です。
対象は運用担当者（オンライン環境で資材を作る人／閉域環境にセットアップする人）です。

> 資材の網羅方針・導入順序・コマンド規律の**正典は [docs/18-オフライン構築.md](../18-オフライン構築.md)**。
> 本書は読みながら作業する手順書。

## 前提（誇張しない・機能マトリクスは下記）

外部 LLM（Codex／OpenAI API）は外部ネットワークへの HTTP 呼び出しを伴います
（[08-実行権限と隔離.md §4](../08-実行権限と隔離.md)）。閉域の構成は2通りに分けて考えます。

- **完全閉域（外へ一切出ない）**: 使える「頭脳」は、ローカルLLM（Ollama）を使うもの——**簡易**（管理画面の「簡易回答に使う AI」をローカル（Ollama）にする。既定）と、接続先を Ollama にした **Codex 調査（Ollama）**——だけです（AI を使わない定型応答はチャットでは廃止しました）。
- **閉域＋OpenAI または Azure OpenAI へだけ穴あけ**（本製品の標準想定・既定は `api.openai.com` への到達だけを
  許可）: 上に加えて、**簡易（OpenAI を選んだとき）** と **Codex 調査（OpenAI）** が使えます。**Azure OpenAI**（Private Link
  経由を含む）を使う場合は、到達先ホストが `<リソース名>.openai.azure.com` になるため、プロキシ/
  ファイアウォールの許可先をそちらに変えてください（`OPENAI_BASE_URL` の設定方法は
  「[24. システム管理](24-システム管理.md#接続先を-azure-openai-にする)」参照）。Codex CLI はキットが
  同梱し（`--skip-codex` で除外可）、認証は **導入スクリプトと `make start` が `.env`（`SHERPA_ENV_FILE`）の
  `OPENAI_API_KEY` で自動的に行います**（`~/.codex/auth.json` を書くだけ・**通信不要**・冪等。サブスク
  認証済みなら触らない）。手動なら `printenv OPENAI_API_KEY | codex login --with-api-key`。推論時だけ
  穴を通ります。

検索（grep・Elasticsearch 全文検索）・取り込み・グラフ（Neo4j 影響分析）・統計・掲示板など、
**AI 頭脳を必要としない機能はそのまま動きます**（詳しくは[機能マトリクス](#機能マトリクス)）。

Marp（スライド PDF/PPTX 出力）と LibreOffice（旧形式 Office 変換）は、閉域でも**同梱すれば動きます**が、
同梱しなければ次のように機能が縮退します（誇張しない）。

- **Marp**: HTML 出力は Node.js＋marp-cli さえ同梱すれば常に可能。**PDF/PPTX 出力は Playwright Chromium も
  同梱した場合のみ**動きます（[sherpa/providers/codex/sandbox.py](../../sherpa/providers/codex/sandbox.py) の `_detect_chrome_path()` が
  自動検出）。同梱しなければ HTML のみのフォールバックになります（marp スキルの仕様どおり）。
- **LibreOffice**: 旧形式 Office（`.doc`/`.xls`/`.ppt`）→ 新形式変換の**選択肢の一つ**
  （[sherpa/ingest/arms/legacy_convert.py](../../sherpa/ingest/arms/legacy_convert.py)）。同梱して
  `legacy_backend=libreoffice` を設定した場合のみ有効。未同梱／未設定では「変換できない（未対応）」に
  fail-safe で倒れます（他のバックエンドである `office_com` は Windows 側の別ワーカーが必要で、この
  配布キットの対象外）。

## 全体像

```
[オンライン環境]                                    [搬入]                [閉域環境]
 このリポジトリを clone                                                    作業フォルダに
   ├ make_offline_kit.sh --fetch で資材を集める                            圧縮ファイルを置き
   │    ├ pip download（wheel一式）                                         照合（sha256）→展開
   │    ├ python3 系 .deb（素の対象OSコンテナ）       圧縮ファイル          → Sherpa/ ができる
   │    ├ docker build+save                           （.tar.gz）と         → ./install.sh
   │    │  （postgres/neo4j/es+kuromoji）           ──→ .sha256 を ──→      （Python実行系→
   │    ├ Docker Engine 本体 .deb（同上）             許可された経路で        Docker Engine→
   │    ├ フォント（Noto CJK・HackGen）               搬入                    docker load→
   │    ├ Node.js + marp-cli                                                  pip install --no-index→
   │    ├ Playwright Chromium（本体+deps）                                    Node/marp/Chromium(+deps)/
   │    ├ LibreOffice（同上）                                                 LibreOffice/フォント/
   │    └ ollama pull（任意・大きい）                                         Ollama を順に導入→
   │         dist/offline-kit/                                                最終検証→指紋の照合）
   └ make package-full（アプリ＋資材を 1 つの圧縮ファイルに）
```

apt 系の資材（python3 系・Docker Engine・フォント・Chromium のシステム依存・LibreOffice）は、収集マシンに
**既に入っているパッケージ**の .deb を集められない `apt-get install --download-only` の制約を避けるため、
**素の対象OSコンテナ内**（既定 `ubuntu:24.04`・`APT_BASE_IMAGE` で変更可）で収集します。これにより、まっさらな
閉域ホストでも依存パッケージ込みで完全に導入できます（収集には収集マシン自身の Docker が必要・sudo 不要）。

1. **オンライン側**でこのリポジトリを clone し、`./scripts/make_offline_kit.sh --fetch` で資材一式
   （`dist/offline-kit/`）を作り、`make package-full` でアプリと資材を **1 つの圧縮ファイル**
   （`dist/sherpa-<版>-full-<コミット>.tar.gz`＋`.sha256`）にする。
2. 組織の規定に従って、**圧縮ファイルと `.sha256`** を閉域環境へ**搬入**する（USB 等の可搬媒体、または承認された
   ファイル転送経路。搬入手段自体は組織のセキュリティ規程に従ってください。本書は資材の作り方・使い方のみを
   扱います）。
3. **閉域側**で、作業用のフォルダに置いて照合→展開し、できた `Sherpa/` で `./install.sh` を1回実行する
   （`docker load`・`pip install --no-index`・Node/marp/Chromium/LibreOffice/フォント/Ollama の展開を一括で行う）。
   `.env` を設定して `make start` で起動する。
4. **検証チェックリスト**で、機能が動くこと・外部到達が無いことを確認する。

## 資材一覧

`scripts/make_offline_kit.sh` が集める資材と、集め方の対応です。

| 資材 | 集め方 | 出力先 |
|------|--------|--------|
| アプリ本体（古い方式の tar.gz。パッケージには入らず `./install.sh` も使わない） | `make dist`（`git archive`・ネットワーク不要） | `dist/offline-kit/app/sherpa-vX.Y.Z.tar.gz` (+`.sha256`) |
| Python 依存（wheel一式） | `pip download -r requirements.txt -c constraints.txt` | `dist/offline-kit/wheels/`（+`SHA256SUMS`） |
| Python 実行系＋基本ツール（python3/venv/pip・xz-utils/unzip/fontconfig・**curl/make**） | 素の対象OSコンテナで `apt-get install --download-only`（curl=起動時のストア健康確認・make=`make start` の入口。素の Linux に無い） | `dist/offline-kit/python/debs/` |
| Docker イメージ：PostgreSQL | `docker pull postgres:16` → `docker save` | `dist/offline-kit/docker-images/postgres-16.tar` |
| Docker イメージ：Neo4j | `docker pull neo4j:5-community` → `docker save` | `dist/offline-kit/docker-images/neo4j-5-community.tar` |
| Docker イメージ：Elasticsearch（kuromoji 焼込） | `docker/elasticsearch/Dockerfile` を `docker build` → `docker save` | `dist/offline-kit/docker-images/es-kuromoji-8.19.20.tar` |
| Docker Engine 本体 | 素の対象OSコンテナでリポジトリ追加（download.docker.com）→`apt-get install --download-only` | `dist/offline-kit/docker-engine/debs/`（鍵: `docker-engine/docker.asc`） |
| Ollama モデルデータ（任意・本体バイナリは含まない） | `ollama pull <model>` 後、`~/.ollama` をコピー | `dist/offline-kit/ollama/dot-ollama/` |
| フォント：Noto Sans CJK JP | 素の対象OSコンテナで `apt-get install --download-only`（SIL OFL 1.1・バンドル可） | `dist/offline-kit/fonts/noto-cjk-debs/` |
| フォント：HackGen | GitHub releases API から最新 zip（通常版優先）を解決してダウンロード（SIL OFL 1.1・バンドル可） | `dist/offline-kit/fonts/hackgen/` |
| Node.js 本体（v22 系・marp 実行に必須） | 公式 tarball を curl 取得＋`SHASUMS256.txt` で sha256 検証 | `dist/offline-kit/node/` |
| marp-cli 本体 | `tools/marp/node_modules` を tar 化（無ければ `npm install` 後に tar 化） | `dist/offline-kit/marp/tools-marp-node_modules.tar.gz` |
| Playwright Chromium 本体（Marp の PDF/PPTX 出力に必須） | 一時 venv で `playwright install chromium`（`chromium-*`系ディレクトリのみ tar 化） | `dist/offline-kit/chromium/ms-playwright-chromium.tar.gz` |
| Playwright Chromium のシステム依存（libnss3 等） | playwright 公式の依存リスト（ubuntu24.04-x64・全21個）を素の対象OSコンテナで download-only | `dist/offline-kit/chromium/deps-debs/` |
| LibreOffice（旧形式 Office 変換の選択肢の一つ） | 素の対象OSコンテナで `apt-get install --download-only`（依存込み・数百MB） | `dist/offline-kit/libreoffice/debs/` |
| OCR ワーカーのイメージ（画像内文字の読み取り・既定ON） | `docker compose --profile ocr build` → `docker save`（約550MB） | `dist/offline-kit/ocr/ocr-worker-paddleocr-3.7.0-cpu.tar` |
| OCR の Python 依存（wheel一式） | `pip download -r requirements-ocr.txt`（コアとは**別**＝numpy の要求が両立しない） | `dist/offline-kit/ocr/wheels/`（+`SHA256SUMS`） |
| **土台の閉包 .deb**（libc6/perl-base/zlib1g 等・収集コンテナに導入済みの全パッケージ） | 素の対象OSコンテナで `apt-get install --download-only --reinstall $(dpkg-query -W …)`（`--skip-base` で除外可）。収集イメージの土台は再ビルドで更新済みのことがあり、閉域ホスト（リリース版）より新しい＝依存が土台で満たされる前提が崩れるため同梱する | `dist/offline-kit/base/debs/` |
| **repo 索引＋BASELINE**（`Packages`/`Packages.gz`/`Release`・`BASELINE`・各グループの `debs/PACKAGES`） | 素のコンテナで `apt-utils` を入れ `apt-ftparchive packages/release`。BASELINE は収集元の OS/版/arch/収集日/イメージ digest | `dist/offline-kit/` 直下 |
| OCR モデル（PP-OCRv6 det/rec・Apache-2.0） | `scripts/fetch_ocr_models.sh` で取得し `docker/ocr-models.lock.json` と照合（約134MB） | `dist/offline-kit/ocr/models/` |
| **Codex CLI**（Apache-2.0・固定版の package 一式＝本体＋bwrap・rg 等） | GitHub の公式リリースから固定版（`scripts/codex-version.env`）を取得し sha256 を照合（既定 Linux x86_64・`CODEX_PIN_KIT_PLATFORM` で変更・`--skip-codex` で除外） | `dist/offline-kit/codex/`（導入先 `tools/codex/`・実行ファイルは `bin/codex`） |

### なぜ apt 系の資材を「素のコンテナ内」で収集するか

`apt-get install --download-only` は、実行したマシンの**インストール済み状態を基準に依存解決**します。
つまり、収集マシンに既に入っているパッケージがあると、その分の .deb は集まりません。開発機や CI で普段
使っているマシンをそのまま収集に使うと、まっさらな閉域ホストへ持って行ったときに依存不足で `apt-get
install` が失敗することがあります。

これを避けるため、python3 系・Docker Engine・フォント（Noto Sans CJK JP）・Chromium のシステム依存・
LibreOffice・**土台の閉包**（次項）はすべて `docker run --rm <APT_BASE_IMAGE> ...` で起動した
**素の対象OSコンテナ内**で `apt-get update && apt-get install --download-only` を実行し、依存関係も
含めた完全な .deb 一式を集めます。ベースイメージは `scripts/make_offline_kit.sh` 冒頭の
`APT_BASE_IMAGE`（既定 `ubuntu:24.04`）で指定し、**閉域側ホストの OS バージョンと合わせてください**
（パッケージ名・依存関係が Ubuntu バージョンで変わりうるため）。この方式には収集マシン自身の Docker が
必要ですが、sudo は不要です（コンテナ内は root で実行）。

収集マシンに Docker が無い場合は、この機体へ直接 `apt-get install --download-only`（sudo 使用）する
フォールバックに自動で切り替わります（土台の閉包を含む・2026-08-18 是正＝以前は土台の閉包だけ
docker 必須で、docker の無い収集機ではキット生成そのものが止まっていた）。その場合は警告が表示され、
**収集マシンに既に入っているパッケージ分の .deb は集まらない＝完全性は保証されません**。土台の閉包の
フォールバックは**収集機自身が閉域側と同じ OS（既定 Ubuntu 24.04 x86_64）であること**が前提です
（素の対象OSコンテナのような分離ができないため）。異なる OS の収集機しか無い場合は docker のある機体
での収集を強く推奨します。

### なぜ Elasticsearch だけ build が要るか

PostgreSQL / Neo4j は素の公式イメージのままで日本語（UTF-8）を扱えます。Elasticsearch の日本語全文検索
（BM25・形態素解析）だけは `analysis-kuromoji` プラグインが必要で、公式イメージには入っていません。
`docker-compose.yml` は `elasticsearch` サービスを `build: ./docker/elasticsearch` で
[`docker/elasticsearch/Dockerfile`](../../docker/elasticsearch/Dockerfile) からビルドし、
`sherpa/es-kuromoji:8.19.20` というローカルタグを付けています。オンライン環境ではこのビルドが
一度必要（公式イメージ + `elasticsearch-plugin install` はネットワークを使います）。閉域側は
`docker load` するだけで、ビルドは不要です。

### Python 依存とバージョン一致の注意

`pip download -r requirements.txt -c constraints.txt -d <出力先>` は、実行したマシンの **Python
バージョン・OS・CPU アーキテクチャに一致する wheel** をダウンロードします。閉域側の Python が
メジャー.マイナー版（例 3.12）や OS/アーキテクチャで異なると、`pip install --no-index` 時に
一致する wheel が見つからずに失敗することがあります。**オンライン側の収集マシンと閉域側の実行マシンは、
同じ OS 系統・同じ Python バージョンにそろえてください**（WSL/Ubuntu 同士など）。
`scripts/make_offline_kit.sh --fetch` は収集時の `python3 --version` を
`dist/offline-kit/wheels/COLLECTED-WITH-PYTHON-VERSION.txt` に記録するので、閉域側で突き合わせて確認できます。

`requirements-dev.txt`（テスト・lint 用）はここでは集めません。配布物はテスト資産を含みません
（[40-運用](40-運用.md) の本番⇄テスト分離の原則どおり）。閉域側で開発・テストも行う場合は、この配布キットとは
別に `requirements-dev.txt` 分の wheel を同様の手順で追加収集してください。

### OCR（画像内文字の読み取り）

**既定で有効**な機能なので、閉域でも使うなら3つそろえて搬入します。1つでも欠けると読み取りだけが
行われません（取り込みと検索は通常どおり動きます）。

| 要るもの | 理由 |
|---|---|
| ワーカーのイメージ | 依存がアプリ本体と**両立しない**ため別プロセスで動かす（コア=numpy 2.5系／paddlex=numpy&lt;2.4 必須） |
| モデル（約134MB） | 配布物に同梱していない（法務確認前・`docker/ocr-models.lock.json` の `distribution_policy`） |
| 資料フォルダの読み取り専用マウント | ワーカーへ渡す範囲を `SHERPA_OCR_WORLD_ROOT` で明示する |

モデルは**内容を固定してあり、起動時に照合**します。上流が更新して中身が変わると
`model_hash_mismatch` で起動を拒みます（別物のモデルで読み取って精度が変わったことに気づけない、
という状態を避けるため）。閉域側では取得済みフォルダをそのままコピーするので、この照合は必ず通ります。

**ワーカーのイメージにはアプリのコードが焼き込まれます。** 版を上げるときはイメージも作り直してください
（`make start` / `make up` は毎回ビルドし直すため、通常運用では意識不要です）。

### フォント

現行の `web/` UI はシステムフォント（`"Hiragino Kaku Gothic ProN","Noto Sans JP","Yu Gothic UI",Meiryo,system-ui,sans-serif`）
のみを使い、Google Fonts 等の外部 CDN には依存していません。したがって画面表示自体には閉域化のためのフォント収集は
不要です。一方、**Marp スライドの日本語表示**には日本語フォントが要るため、`make_offline_kit.sh --fetch` は
Noto Sans CJK JP（`apt-get install --download-only` で .deb 一式）と HackGen（GitHub releases から zip）を
自動収集します。どちらも SIL Open Font License 1.1 でバンドル・再配布が許諾されています。将来 `.woff2` 等の
自前 Web フォントを画面表示に採用した場合は、`dist/offline-kit/fonts/` に置いて閉域側の静的配信に含めてください。

## オンライン側: 資材の作成

> 収集は **Ubuntu 24.04（x86_64）の Linux 上**で実行する（VM/物理/WSL2 は問わない）。実行前の
> 確認コマンド5つ（`uname -m`＝x86_64・OS/Python 版・Docker・空き20GB）と搬出の規律は
> [docs/18-オフライン構築.md](../18-オフライン構築.md) の「オンライン側（収集環境）」。

```bash
git clone <このリポジトリ> Sherpa && cd Sherpa

# 1) 計画だけ確認（既定は実行しない・何が起きるか事前に見る）
./scripts/make_offline_kit.sh

# 2) 実際に収集する（アプリ本体・Python依存・Dockerイメージ・フォント・Node/marp・Chromium・LibreOffice）
./scripts/make_offline_kit.sh --fetch

# 3) ローカルLLM（Ollama）も同梱する場合（モデルは数GB〜数十GB・任意）
./scripts/make_offline_kit.sh --fetch --with-ollama gpt-oss:20b

# 4) 搬入前の出荷ゲート（docker 必須・必ず通す。2026-08-18・閉域実機報告③）
make verify-kit

# 5) パッケージを作る（資材を先に集めてあることが条件。dist/ に圧縮ファイルと .sha256 ができる）
make package-full   # 初回導入・依存が変わる更新用（アプリ＋資材）
make package-app    # 依存が変わらない更新用（アプリだけ）
```

Docker が使えない収集マシンの場合は `--skip-docker` を付けて Docker イメージの収集を省略できます
（その場合、Docker イメージは別途・別マシンで収集して同じ `dist/offline-kit/docker-images/` に合流させてください）。
同様に、不要な資材は個別に除外できます: `--skip-docker-engine`（Docker Engine 本体）・
`--skip-python-system`（python3 系）・`--skip-fonts`・`--skip-node`・`--skip-chromium`・`--skip-libreoffice`・
`--skip-base`（土台の閉包。閉域ホストの土台がキットより古いと導入が unmet で止まるため、通常は省略しない）。

収集結果は `dist/offline-kit/`（`dist/` は Git 管理外）にまとまり、`MANIFEST.txt` に内容一覧が記録されます。
`make package-full` はこの資材を `Sherpa/dist/offline-kit/` に入れた圧縮ファイルを作ります（最上位フォルダは常に
`Sherpa/`・ファイル名は `sherpa-<版>-<full|app>-<短いコミットハッシュ>.tar.gz`・隣に `.sha256`）。圧縮ファイルの
先頭の項目は `PACKAGE-INFO`（種類・版・コミット・プラットフォームごとの依存の指紋）、次が
`PACKAGE-MANIFEST.sha256`（アプリのファイル一覧）です。`.env`・`data/`・`.venv`・`tools/` の実行物は入りません。
`make package-app` はアプリだけの圧縮ファイルを作りますが、指紋を資材から求めるため `dist/offline-kit/` が必要です。
**搬入するのは圧縮ファイルと `.sha256` だけ**です（チェックアウト全体を移送する必要はありません）。

**搬入前に `make verify-kit` を必ず通してください**（`scripts/verify_offline_kit_apt.sh`）。docker で
「搬入先に近いホスト」（インストール直後の Ubuntu Server 24.04 GA 相当）を用意し、`--network none` で
実際にキットを apt 導入してみて、削除提案・カーネル消失・導入失敗が無いことを確かめる出荷ゲートです。
土台の閉包に穴があると閉域側で初めて「削除提案で fail-close」に気づくことになり、往復のコストが大きい
ため、コードレビューではなくこのゲートで検出します（詳細: [docs/18-オフライン構築.md](../18-オフライン構築.md) §2 原則5・§6）。

## 閉域側: セットアップ手順

導入も更新も**同じ手順**です（照合 → 展開 → `./install.sh` → 確認 → `make start`）。同じ手順書がパッケージの
`INSTALL.md` にも入っています。以下の `<作業フォルダ>`・`<圧縮ファイル名>`・`<サーバー>`・`<SHERPA_PORT>` は、
ご利用の環境の値に読み替えてください。

パッケージは 2 種類です。**初回導入はフルだけ**です（アプリだけのパッケージには資材が無く、閉域の Linux は
オンラインで取れないため、初回には使えません。`./install.sh` が依存に触れずに止まり、フルが必要と案内します）。

| 種類 | 中身 | 使いどき |
|---|---|---|
| フル | アプリ＋オフラインの資材（`Sherpa/dist/offline-kit/`） | 初回導入、および依存が変わった更新 |
| アプリだけ | アプリのみ（小さい） | 依存がすでに入っている環境の、依存が変わらない更新 |

`Sherpa/` の中のパッケージのファイルは**アプリのファイルで、更新のたびに置き換わります。編集しないでください**。
設定は `.env`（または `/etc/sherpa/sherpa.env`）に、データと利用者のファイルは `data/` に置きます（どちらも
パッケージに入っていないので、上書き展開で消えません）。

### 導入（初回・フルのパッケージ）

1. 作業用のフォルダに圧縮ファイルと `.sha256` を置き、**展開の前に**照合します。失敗したら展開しません
   （既存の `Sherpa/` には触れません）。

   ```bash
   cd <作業フォルダ>
   sha256sum -c <圧縮ファイル名>.sha256         # Linux
   shasum -a 256 -c <圧縮ファイル名>.sha256     # macOS（同じ照合）
   tar xzf <圧縮ファイル名>                      # Sherpa/ ができる
   ```

2. インストールします（ログを残します）。**一般ユーザーとして**実行してください（root 直接ではなく）。

   ```bash
   cd <作業フォルダ>/Sherpa
   ./install.sh 2>&1 | tee ~/install-sherpa.log
   ```

   `./install.sh` は共通の入口で、`make` が無くても動きます。最初にパッケージのメタデータ（`PACKAGE-INFO`）で
   フル／アプリだけを読み、Linux のフルのときだけ `scripts/install_offline_kit.sh`（同梱の資材でオフライン導入）を
   呼びます（macOS はオフラインの資材を使わず、`./install.sh` がオンラインで準備します）。最後の「最終検証」が
   OK（Ollama は対象外）なら完了です。終わったら一度ログインし直してください（docker グループの権限は、
   ログインし直すまで効きません）。

   `root` で直接実行するとエラーで止まります。Chromium/Ollama 等が `/root` 配下に展開されてしまい、
   `sherpa/providers/codex/sandbox.py` の自動検出（実行ユーザーの `$HOME` を見る）から見えなくなるためです。必要な操作
   （パッケージ導入・`systemctl`・`usermod` 等）はスクリプト内部が個別に `sudo` を使います。スクリプト冒頭で
   `sudo -v` によりパスワードを一度だけ聞かれ、以降は長時間の手順（.deb 導入・`docker load` 等）の途中で
   sudo 認証が失効しないよう、バックグラウンドで自動延命されます。

   導入の流れは次のとおりです。展開したファイルの照合（`PACKAGE-MANIFEST.sha256`）→ このプラットフォームの指紋の
   項目の確認 → Python 実行系（python3/venv/pip）→ Docker Engine 本体（未導入の場合のみ・
   `systemctl enable --now docker` と `usermod -aG docker` を含む）→ Docker イメージの `docker load` →
   `pip install --no-index`（`.venv` 作成込み）→ Node.js/marp-cli の展開 → Playwright Chromium
   （本体＋システム依存 .deb）の展開 → LibreOffice/フォントの導入 → Ollama モデルデータのコピー →
   **最終検証**（各資材の動作確認一覧）→ 導入後の環境の指紋とパッケージの指紋の照合 → 記録の更新。
   収集済みの資材があるものだけ順に実行されます（未収集の資材は「スキップしました」と表示して次へ進みます）。
   今の `.venv` と `tools/` の実行物は `.prev` に退避してから最終の場所で作り直し、失敗したら元へ戻します。

   .deb の導入は `dpkg -i` ではなく `scripts/lib/apt_offline.sh`（`apt_offline_install`）で行います: 導入の最初に
   `BASELINE` と機体の OS/arch を照合し、各グループは **`apt-get -s`（シミュレーション）を先行 → 削除提案（Remv）が
   あれば中止 → `--no-remove`・非対話で本導入 → 稼働カーネル（linux-image-*）の残存確認**。索引（`Packages`）付き
   キットは `deb [trusted=yes] file:<kit_root> ./` のローカル repo として読み、`debs/PACKAGES` の名前だけを apt に
   解かせます（`./*.deb` 全列挙より安全＝降格にならず、土台の閉包も候補に載る）。失敗時は「不足しているパッケージ名」
   を表示するので、オンライン側の収集に足して作り直してください。復旧は `sudo dpkg --configure -a` のあと
   `sudo apt-get -f -s install` で計画に削除が無いことを確かめてから `sudo apt-get -f --no-remove install`
   （`-f` 単独は削除を提案し得るので使わない）。土台の閉包を入れると libc6 等が更新されるため、導入後の再起動は運用判断。

3. 設定ファイルを用意します（**初回のみ**）。`./install.sh`（Linux のフル）は、`.env` が無ければ `.env.example`
   から作ります（既存の `.env` には触れません）。本番で `/etc/sherpa/sherpa.env` を使うときは、既にあれば**上書きせず**、
   無ければ `.env.example` から作って、冒頭「0. 本番チェックリスト」節を設定します。

   ```bash
   sudo install -d /etc/sherpa
   if [ ! -e /etc/sherpa/sherpa.env ]; then
     sudo cp .env.example /etc/sherpa/sherpa.env
     sudo editor /etc/sherpa/sherpa.env
   else
     echo "/etc/sherpa/sherpa.env は既に存在するため上書きしません（前回の設定をそのまま使います）"
   fi
   ```

   データ系パス（`SHERPA_USERS_DIR`・`SHERPA_DERIVED_DIR`）は、`Sherpa/data/` の下か、アプリのフォルダの外の
   固定パス（例 `/srv/sherpa/...`）にします。`Sherpa/data/` 以外のアプリのフォルダの中は、更新で置き換わるため
   `check-production` が断ります。

4. 確認して起動します。

   ```bash
   cd <作業フォルダ>/Sherpa
   docker ps                                  # エラーにならないこと
   sysctl vm.max_map_count                    # 262144 以上なら何もしない（Linux のみ）
   sudo bash scripts/setup-codex-sandbox.sh   # 「このままでは…起動できない」と出たら apply で直す（Linux のみ）
   make check-ports && make start
   ```

   `SHERPA_ENV=production` のときは、`make start` が起動の前に `check-production` の検査を自動で行います。

   **Ubuntu 23.10 以降なら、Codex のサンドボックスを必ず確認してください**: AppArmor の
   ユーザー名前空間制限で Codex のサンドボックス（bubblewrap）が起動できず、資料フォルダを直接
   読めないまま別の経路で応答してしまうことがあります（詳しくは [40-運用.md](40-運用.md) の
   「Codex のサンドボックス」節）。閉域網はこの障害が特に気付きにくいため（外部から様子を確認
   できない）、導入時に確認してください。

   ```bash
   sudo bash scripts/setup-codex-sandbox.sh          # 確認のみ（既定 check・何も変更しません）
   sudo bash scripts/setup-codex-sandbox.sh apply    # 制限が原因なら緩和します（OS の設定を変更）
   ```

   `apply` のあと、`make doctor` の「Codex のサンドボックス」項目が OK になることを確かめてください。

5. 起動後に確かめます。
   1. ブラウザで `http://<サーバー>:<SHERPA_PORT>/` を開き、admin でログインできる。
   2. 管理画面で頭脳が Codex になっている。
   3. 管理「資料フォルダ」で資料を登録し、取り込みが終わる。
   4. 新しい会話で 1 つ依頼し、回答と出典が返る。

systemd で常駐化する場合は、同梱の `deploy/systemd/sherpa-api.service`（Sherpa フォルダを直接指す）を、置き場に
合わせてパスを直して使います（[40-運用.md](40-運用.md) の「本番展開の標準手順」）。

### 更新

1. `make stop`（systemd で常駐させているときは有効なユニットも止めます。止められないときは
   `sudo systemctl stop <ユニット名>` を促すメッセージが出ます）。
2. 同じ作業用のフォルダに新しい圧縮ファイルを置き、導入の 1 と同じく**照合してから展開**します
   （`Sherpa/` が上書きされます）。
3. そのあとは導入の 2〜5 と同じです。更新のインストールは、作業の前にデータのバックアップを自動で取ります
   （`SHERPA_BACKUP_BEFORE_SWITCH=0` で抑止。**ストアが動いていると取らずに警告して続行**するので、更新前に
   `make stop` してから実行するのが安全です。停止中なのにバックアップに失敗したときはインストールを中止します）。
   バックアップが守るのはデータ（DB・個人領域・設定）で、アプリ・`.venv`・`tools/` は含みません。
4. **前の版の圧縮ファイルは、更新が確認できるまで作業用のフォルダに残してください**（元に戻すため）。

`./install.sh` は、前回の一覧にあって今回の一覧に無いファイルを、**今のハッシュが記録したハッシュと同じときだけ**
削除します（手を入れたファイルは残し、最後に一覧で表示します。`.env`・`data/`・`.venv`・`tools/` は決して
消しません）。アプリだけで更新すると、古い `dist/offline-kit` もこの規則で片付きます。

**アプリだけのパッケージで「フルが必要」と止まったとき**: 依存（Python パッケージ・Docker イメージ・Codex CLI・
Node／marp・Chromium・LibreOffice・OCR・フォントなど）の版が変わっている、または部品が欠けていると、
`./install.sh` は `.venv`・`tools/`・依存・データに触れず、「インストール中」の印（`data/.installing`）を残したまま
止まります。印があるので `make start` も systemd からの起動も断られます。出口は 2 つです（どちらもデータには
触れません）。(a) 同じ版の**フルのパッケージ**を置き、通常の手順で入れ直す。(b) **前の版の圧縮ファイル**を
展開し直し、`./install.sh` して前の版へ戻る。

### 元に戻す

1. `make stop`。
2. 前の版の圧縮ファイルを、同じ手順（照合 → 展開 → `./install.sh`）で入れ直します。インストールは、残してある
   前の `.venv`／`tools/` か、そのパッケージの資材から、合う組を戻します。
3. データの形が変わる版のときは、更新前のバックアップから `make restore FROM=<バックアップの dir>` でデータを戻します
   （DB は前進のみのため、アプリだけ戻しても多くは動きますが、保証はされません）。

### 途中で失敗したとき

- 圧縮ファイルの展開は `./install.sh` より前に起きます。展開しただけで `./install.sh` が成功していない間は、
  `data/` の記録の版・コミットとパッケージのメタデータが食い違うため、`make start`・`systemctl start`・再起動は
  断られます（展開が途中で止まっても、先頭のメタデータは書かれているので同じです）。
- インストールが途中で失敗した場合は、`data/.installing`（インストール中の印）が残る間、起動できません。原因を
  直して `./install.sh` をやり直してください。成功すると印が消えます。
- 起動は依存を自分では入れません。依存の記録（requirements／constraints）と違うときは起動を断り、`./install.sh` を
  案内します。
- 同じ版を入れ直す展開が途中で止まった場合（ディスク不足など）は、版・コミットが同じなので起動の拒否では
  見つかりません。展開のあとは必ず `./install.sh` を実行してください（展開したファイルを一覧のハッシュで確かめます）。

### 手動で行う場合の内訳（`install_offline_kit.sh` が内部で行っていること）

```bash
# 1) 展開したファイルの整合性を確認（Sherpa/ の直下で・PACKAGE-MANIFEST.sha256 の一覧と照合）
sha256sum -c PACKAGE-MANIFEST.sha256

# 2) Python 実行系（venv 作成が最初に転ばないよう Docker Engine より先に導入）
bash scripts/lib/apt_offline.sh install "python/debs" "$PWD/dist/offline-kit" "$PWD/dist/offline-kit/python/debs"   # -s 先行・--no-remove・カーネル残存確認（生の apt-get install -y ./*.deb は使わない）

# 3) Docker Engine 本体（未導入の場合のみ。鍵は dist/offline-kit/docker-engine/docker.asc に来歴保存済み）
bash scripts/lib/apt_offline.sh install "docker-engine/debs" "$PWD/dist/offline-kit" "$PWD/dist/offline-kit/docker-engine/debs"   # -s 先行・--no-remove・カーネル残存確認（生の apt-get install -y ./*.deb は使わない）
sudo systemctl enable --now docker   # systemd 前提（WSL2/通常Ubuntuは対応。非systemdホストは別途 dockerd 起動手順が必要）
sudo usermod -aG docker "$USER"   # 反映には再ログイン（または newgrp docker）が必要。反映前は sudo docker で代替

# 4) Docker イメージを読み込む（docker pull は使わない・タグは save 時点のものがそのまま復元される）
docker load -i dist/offline-kit/docker-images/postgres-16.tar
docker load -i dist/offline-kit/docker-images/neo4j-5-community.tar
docker load -i dist/offline-kit/docker-images/es-kuromoji-8.19.20.tar
docker images | grep -E 'postgres|neo4j|sherpa/es-kuromoji'   # 取り込めたタグを確認

# 4.5) wheels の整合性確認（sha256 ファイルはベース名のみを記録しているため、同じディレクトリで
#      検証する。R6a: install_offline_kit.sh はこれを pip install の前に自動実行する）
(cd dist/offline-kit/wheels && sha256sum -c SHA256SUMS)

# 5) Python 環境（オフライン install・--no-index で PyPI に一切アクセスしない。
#    constraints.txt も渡し、収集時とバージョンを固定する）
python3 -m venv .venv
.venv/bin/python -m pip install --no-index --find-links dist/offline-kit/wheels \
  -r requirements.txt -c constraints.txt

# 6) Node.js（tools/node/ へ展開。PATH は scripts/run-common.sh が自動で通すため手動設定は不要）
mkdir -p tools/node
tar -xJf dist/offline-kit/node/node-v*.tar.xz -C tools/node --strip-components=1

# 7) marp-cli（tools/marp/node_modules へ展開・sherpa/providers/codex/sandbox.py の _marp_bin が自動検出）
mkdir -p tools/marp
tar -xzf dist/offline-kit/marp/tools-marp-node_modules.tar.gz -C tools/marp

# 8) Playwright Chromium（本体を ~/.cache/ms-playwright/ へ展開・_detect_chrome_path が自動検出。
#    システム依存 .deb も必ず導入する＝無いと Chromium が起動せず PDF/PPTX 出力が失敗する）
tar -xzf dist/offline-kit/chromium/ms-playwright-chromium.tar.gz -C ~/.cache/ms-playwright/
bash scripts/lib/apt_offline.sh install "chromium/deps-debs" "$PWD/dist/offline-kit" "$PWD/dist/offline-kit/chromium/deps-debs"   # -s 先行・--no-remove・カーネル残存確認（生の apt-get install -y ./*.deb は使わない）

# 9) LibreOffice（旧形式 Office 変換の選択肢の一つ。導入後は system_settings/env の
#    legacy_backend=libreoffice を設定して初めて有効になる）
bash scripts/lib/apt_offline.sh install "libreoffice/debs" "$PWD/dist/offline-kit" "$PWD/dist/offline-kit/libreoffice/debs"   # -s 先行・--no-remove・カーネル残存確認（生の apt-get install -y ./*.deb は使わない）

# 10) フォント（Marp スライドの日本語表示用）
bash scripts/lib/apt_offline.sh install "fonts/noto-cjk-debs" "$PWD/dist/offline-kit" "$PWD/dist/offline-kit/fonts/noto-cjk-debs"   # -s 先行・--no-remove・カーネル残存確認（生の apt-get install -y ./*.deb は使わない）
unzip dist/offline-kit/fonts/hackgen/*.zip -d /tmp/hackgen
mkdir -p ~/.local/share/fonts && cp /tmp/hackgen/**/*.[ot]tf ~/.local/share/fonts/
fc-cache -f

# 11) Ollama（同梱した場合。同梱するのは「モデルデータ」のみで、ollama 本体バイナリは
#     含まれない＝別途ホストへ導入しておくこと。導入方法はネットワーク有無・OSで変わるため
#     組織の標準手順に従う）
mkdir -p ~/.ollama
cp -a dist/offline-kit/ollama/dot-ollama/. ~/.ollama/
ollama list   # 取り込んだモデルが見えることを確認

# 12) env 設定（本番配置と同じ場所・40-運用の手順と揃える）
sudo install -d /etc/sherpa
sudo cp .env.example /etc/sherpa/sherpa.env
sudo editor /etc/sherpa/sherpa.env   # まず冒頭「0. 本番チェックリスト」節を設定する
```

`/etc/sherpa/sherpa.env` の頭脳（AI）の設定は、閉域での構成によって実際には2通りです
（AI を使わない定型応答はチャットでは廃止しました）。頭脳は自動選択され、チャットで選び直せます（選べるのは `codex`／`simple`）。以前は Codex CLI の有無だけで
判断していましたが、キットが Codex CLI を同梱するようになり「CLI はあるが認証は無い」ホストが
現実的になったため、条件を「CLI があり、かつ使える認証（実 `OPENAI_API_KEY` か `codex login`
済みの `~/.codex/auth.json`）がある」へ変更しました（2026-08-18 Codex RV 2巡目 指摘2）。満たさなければ
簡易が選ばれます。簡易が使う AI は管理画面の「簡易回答に使う AI」の設定で、未設定ならローカル（Ollama）です）。

```bash
SHERPA_ENV=production

# 1) OpenAI または Azure OpenAI へ穴あけがある（同梱の Codex CLI を使う）: OPENAI_API_KEY を
#    設定する（Azure ならそのキー）。頭脳は自動選択される。
OPENAI_API_KEY=
# Azure OpenAI を使う場合だけ、加えて以下を設定する（モデル欄には Azure の「デプロイ名」を入れる）。
# OPENAI_BASE_URL=https://<リソース名>.openai.azure.com/openai/v1/

# 2) 外へ出られない・ローカルLLM（Ollama）がある: 以下を設定する（頭脳は簡易が選ばれる）。
OLLAMA_URL=http://127.0.0.1:11434

```

ストアの起動・以降のアプリ起動は通常の本番手順と同じです（[40-運用](40-運用.md) の「本番展開の標準手順」）。
`docker-compose.yml` の `POSTGRES_PASSWORD` / `NEO4J_PASSWORD` は Compose の変数展開なので、**シェル環境か
`--env-file` で明示的に渡さない限り `/etc/sherpa/sherpa.env` を読みません**（プロジェクト直下の `.env` だけが
既定で読まれる）。既定パスワードのまま起動してアプリ側 env と食い違う事故を避けるため、必ず
`--env-file` を付けて起動してください。`docker load` 済みでタグ（`sherpa/es-kuromoji:8.19.20` 等）が
ローカルに揃っていれば、pull もビルドも行わず既存イメージをそのまま使います。

Elasticsearch の起動には `vm.max_map_count=262144` 以上が要ります（閉域実機報告・2026-08-18）。
起動前に現在値を確認してください。

```bash
sysctl vm.max_map_count
```

**262144 以上ならそのままで構いません（下げないでください）**。同居する別製品がより大きい値を
要求して既に設定している場合があり、Sherpa の都合で下げるとその製品が壊れます。既存の設定を
探すには `grep -rn max_map_count /etc/sysctl.conf /etc/sysctl.d/` を使ってください。不足している
ときだけ、新規ファイルとして追加します。

```bash
echo 'vm.max_map_count=262144' | sudo tee /etc/sysctl.d/99-sherpa-vm-max-map-count.conf
sudo sysctl --system
```

`/etc/sysctl.d/` は**ファイル名の番号順に読まれ、後勝ち**です。同居製品が別ファイル（例
`10-map-count.conf`）でより大きい値を設定している場合は、そちらが優先されるよう Sherpa 用の
ファイルは置かないでください（どうしても両方置くなら、Sherpa 側を同居製品より小さい番号にします）。

```bash
# 以下は Sherpa/（パッケージを展開したフォルダ）の直下で実行
# ※ Docker Engine をこの手順で入れた直後は docker グループが未反映＝再ログイン（または
#   newgrp docker）してから実行する。再ログイン前に進める場合は sudo docker compose ... を使う。
sudo ./scripts/setup-runtime-users.sh
SHERPA_ENV_FILE=/etc/sherpa/sherpa.env ./scripts/check-production.sh
# 起動は make start（ストア起動＝docker compose up -d・アプリ起動・ポート検査を一括で行います）。
# 社内 LAN への公開（SHERPA_LAN=1＝0.0.0.0 待受）が**既定**です——閉域網で他端末から
# 使わせる前提のため。このホストだけに閉じたい（127.0.0.1 のみ）場合は env に
# SHERPA_LAN=0 を書くか、その回だけ LAN=0 を付けます。LAN=1 の明示も従来どおり有効です。
SHERPA_ENV_FILE=/etc/sherpa/sherpa.env make start          # 既定＝社内 LAN へ公開（平文 HTTP）
# SHERPA_ENV_FILE=/etc/sherpa/sherpa.env LAN=0 make start  # このホストのみ（127.0.0.1）
```

## 検証チェックリスト

`install_offline_kit.sh` は末尾で、収集済みの資材ごとに **OK/NG/未収集** の一覧表を自動表示します
（Docker イメージ・`pip check`・Node.js・marp-cli・Chromium・LibreOffice・フォントの動作確認。1件でも NG が
あれば非0で終了します）。以下はそれに加えて、セットアップ後に手動で確認する項目です。

**土台が壊れていないこと**
- [ ] 導入ログ（`data/install-logs/`）に「0.5 土台の事前照合」が一致で通り、各 .deb グループが「削除ゼロ・カーネル残存を確認」で終わっている。
- [ ] `dpkg -l 'linux-image*' | grep ^ii` に稼働中カーネル（`linux-image-$(uname -r)`）と `linux-image-generic`/`-virtual` 等のメタパッケージが残っている（消えていたら**再起動せず** `--reinstall` で復旧）。

**機能が動くこと**
- [ ] `curl -fsS http://127.0.0.1:8000/healthz` が成功する。
- [ ] `/health/summary`（画面右上の状態ドット）で PostgreSQL / Neo4j / Elasticsearch が緑になる。
- [ ] ログインでき、資料フォルダ（world）を登録・取り込みできる（[20-管理-取り込み](20-管理-取り込み.md)）。
- [ ] チャットで「簡易（検索して答える）」を選び（簡易回答に使う AI がローカル（Ollama））、検索した回答と出典が返る（Ollama 同梱時）。
- [ ] チャットで「Codex 調査（Ollama）」を選び、応答が返る（Codex CLI と Ollama 同梱時）。
- [ ] 影響分析（Neo4j）・全文検索（Elasticsearch）・利用統計・掲示板（トップ画面）が動く。
- [ ] Marp スライドの HTML 出力ができる（Node.js/marp-cli 同梱時）。
- [ ] Marp スライドの PDF/PPTX 出力ができ、日本語が文字化けしない（Playwright Chromium 本体＋
      システム依存 .deb・フォント同梱時。システム依存が未導入だと Chromium 自体が起動できず失敗する）。
- [ ] 旧形式 Office（`.doc`/`.xls`/`.ppt`）の取り込みで新形式への変換が動く
      （LibreOffice 同梱＋`legacy_backend=libreoffice` 設定時。未設定なら「変換できない」表示になることを確認）。
- [ ] Elasticsearch が起動していること（`docker compose ps` で `elasticsearch` が healthy）。
      起動しない場合は `sysctl vm.max_map_count` を確認してください（`262144` 以上が必要）。
      不足しているときだけ上の「vm.max_map_count」節の手順で `99-sherpa-vm-max-map-count.conf` を
      追加してください（**既に 262144 以上ある場合や、同居製品がより大きい値を設定している場合は
      Sherpa 側のファイルを追加しない**＝`/etc/sysctl.d/` は後勝ちのため、下げる方向の上書きを
      避けます）。`./scripts/check-production.sh` もこの値を検査します。

**外部到達が無いこと**

サーバ env にも利用者の個人設定にも API キーが無い状態が、閉域での正しい状態です。この状態では
OpenAI は**そもそもネットワークへ出ません**（`sherpa/providers/__init__.py` の `_select_provider` が
キー未解決を検出した時点で `_UnwiredProvider` に倒し、「未接続」の応答を即返す実装のため）。Codex だけは
キー有無に関係なく Codex CLI を実際に起動するため、閉域では到達できずタイムアウト/接続エラーで失敗します。

- [ ] 「簡易回答に使う AI」を OpenAI にした状態で、チャットの頭脳に「簡易」を選んで質問すると、接続を試みず
      即座に「（頭脳名）はまだ接続されていません」という**設定不足の応答**になること（＝キーが無ければ
      ネットワークに出ないことの確認。固定回答が「未接続」の正直な文言であり、嘘の正常回答でないことも確認）。
- [ ] チャットの頭脳選択で「Codex 調査（OpenAI）」を選んで質問すると、成功はせず**タイムアウトまたは接続エラーで失敗する**こと
      （＝外部に出ようとして到達できず落ちる。Codex は key gate が無いため必ず接続を試みる）。
- [ ] 利用者の個人設定（設定画面）に、移行前などに登録した古い OpenAI の API キーが
      残っていないか確認する。**キーが残っていると「未接続」にならず実際に外部接続を試みてしまう**
      （タイムアウト/接続エラーにはなるが、無用な外向き通信の試行が発生する）。
- [ ] 閉域環境のネットワーク監視（ファイアウォールログ／プロキシログ等、組織の監視手段）で、Sherpa プロセスから
      外部（80/443等）への送信が発生していないことを確認する。
- [ ] `.env`（または `sherpa.env`）に `OPENAI_API_KEY` が設定されていないことを確認する（サーバ側にキーがあると、その頭脳を選んだユーザー全員が
      外部呼び出しを試みる余地になるため。キー一覧は [90-リファレンス](90-リファレンス.md) の設定表）。
- [ ] `pip install` 時に `--no-index` が使われ、PyPI へのアクセスが発生していないこと（オンライン側の
      収集手順どおりに wheel が揃っていれば、通常のネットワークアクセスなしで成功する）。

## 機能マトリクス

| 機能 | 閉域で使えるか | 備考 |
|------|:---:|------|
| 検索（grep・全文/ES） | ○ | 外部ネットワーク不要 |
| 取り込み（フォルダ登録・Office/PDF→MD） | ○ | Office COM ワーカーもローカル実行（[11-Office変換.md](../11-Office変換.md)） |
| グラフ（Neo4j 影響分析） | ○ | 静的解析＋辞書突合（言及エッジ）だけで完結。AI は使いません |
| 統計（管理者向け利用統計） | ○ | 画面表示はローカル集計のみ。外部送信なし |
| 掲示板（トップ画面・お知らせ） | ○ | ローカル DB のみ |
| チャット応答（簡易・ローカルLLM／Ollama） | △ | 動くがモデル次第で応答品質が変わる。事前にモデルを閉域へ搬入する必要あり |
| チャット応答（Codex 調査（Ollama）） | △ | Codex CLI（同梱）が接続先の Ollama へつなぐ。モデル次第で応答品質が変わる。Ollama 0.13.3 以降が必要 |
| Codex 調査（OpenAI）（調査・作成系） | △ | 完全閉域は ×（接続先を Ollama にした構成は上の行）。**OpenAI または Azure OpenAI へだけ穴あけ**した閉域なら ○（Codex CLI はキット同梱・認証は通信不要。Azure OpenAI 接続時は Web 検索は使えません） |
| 外部頭脳（OpenAI を使う簡易） | × | 外部エンドポイントへの HTTP 呼び出しが必須（OpenAI API は Azure OpenAI 経由も含む・接続先設定は「[24. システム管理](24-システム管理.md#接続先を-azure-openai-にする)」） |
| エージェント検索（agentic grep 反復） | △ | 仕組み自体はローカルで動くが、判断する頭脳がローカルLLMに限られるため精度はその頭脳次第 |
| Marp スライド（HTML 出力） | ○ | Node.js＋marp-cli の同梱が必要。未同梱なら marp スキル自体が使えない |
| Marp スライド（PDF/PPTX 出力） | △ | 上記に加え Playwright Chromium 本体＋システム依存 .deb（libnss3 等）と日本語フォントの同梱が必要。未同梱なら HTML のみへ縮退 |
| LibreOffice（旧形式 Office 変換） | △ | 同梱＋`legacy_backend=libreoffice` 設定が必要。未設定は fail-safe で「変換できない」表示に倒れる |
| OCR（画像内文字の読み取り） | ○ | ローカル推論のみで外部通信は不要。イメージ・モデルの同梱が必要で、未同梱なら「画像の中の文字が読まれない」だけに縮退する |

## 関連文書・機密モード（`local_only`）との関係

- セキュリティ上の決まりとして、特に「メイン推論は外部 OpenAI」「テキスト送信は可だがファイルは
  永続化しない」は**外部 AI を使う場合**の契約であり、本書の閉域構成では外部 AI 自体を使わないため、
  この契約の対象外（＝そもそも外部送信が発生しない）になります。
- **機密モード（`local_only`）**は、アプリが推論を Ollama に振り、OpenAI 等への外部送信を行わない
  **動作モードの実装**として検討中です（未実装・[08-実行権限と隔離.md §4](../08-実行権限と隔離.md)
  にも設計メモあり）。本書の閉域配布キットは `local_only` モードの実装を前提にせず、**外部プロバイダを選ばない
  運用**（設定・キー未投入・ネットワーク遮断）で同等の状態を今すぐ作る手順です。`local_only` が実装されれば、
  アプリ側が誤って外部頭脳へ切り替わることをコード上でも防げるようになり、本書の運用回避策より一段階強い
  保証になります。

## 実行しない・省略した重い手順（このマニュアル整備時点）

このページと `scripts/make_offline_kit.sh` の整備にあたっては、回線・容量を使う以下は**実行していません**。
実際の配布キット作成時はオンライン環境で実行してください。

- `./scripts/make_offline_kit.sh --fetch`（pip download・docker pull/build/save・python3系/Docker Engine/
  フォント/Node/Chromium(+システム依存)/LibreOffice の取得。素の対象OSコンテナでの apt 収集を含む）
- `./scripts/make_offline_kit.sh --fetch --with-ollama <model>`（Ollama モデル pull）
- 閉域側での `docker load` / `pip install --no-index` / `./scripts/install_offline_kit.sh`
  （Docker Engine 導入・sudo 前払い・最終検証を含む）の実機検証
