# Sherpa MVP — 起動・運用タスク
#
# `make` だけを打つと、下の一覧（help）が出ます。
.PHONY: help hooks dev-setup start stop restart status check-ports up down ps logs l bootstrap install-docker ocr-models \
        api serve prod-check verify-kit verify-extension dist nuke notice notice-check \
        test test-unit test-api test-contract test-integration test-e2e \
        test-db-reset screenshots backup restore usage-backfill trace azure-smoke codex-compat doctor sandbox-check \
        codex-install codex-version \
        gate-slice gate-merge gate-release gate-ci test-inventory test-durations doc-lint

# 引数なしの `make` は一覧表示にする（いきなりサーバが起動すると事故になるため）。
.DEFAULT_GOAL := help

# make logs の後ろに並べた語（make logs convert embed / make l c e m / make logs help）は scripts/logs.sh の
# 引数として渡す。残りの目標は何もしない目標にし、同名の実目標（help・api）は定義しない
# （MAKECMDGOALS の慣用の書き方・GNU make 3.81 でも動く）。語は環境変数で渡し、シェル文字列に埋め込まない。
# n=500 は make の変数代入として来るため $(n)／$(N) で受ける。
ifneq ($(filter logs l,$(firstword $(MAKECMDGOALS))),)
LOGS_WORDS := $(wordlist 2,$(words $(MAKECMDGOALS)),$(MAKECMDGOALS))
# 後ろの語に help・api 以外の実在の目標名（start・nuke など）があれば、何も実行せず解析の段階で止める
# （ダミーの定義が後ろの実定義に上書きされて実目標が走る事故を防ぐ）。
LOGS_REAL := $(filter-out help api,$(shell sed -n 's/^\([a-zA-Z0-9_-][a-zA-Z0-9_-]*\):.*/\1/p' $(MAKEFILE_LIST)))
LOGS_BAD := $(filter $(LOGS_REAL),$(LOGS_WORDS))
ifneq ($(LOGS_BAD),)
$(error make logs の後ろには logs.sh の名前・短縮・動作の語だけ書けます（指定できない語: $(LOGS_BAD)）。指定できる名前: make logs help で一覧。動作の語: help(h) mem(m) report(r) err n=数字)
endif
ifneq ($(LOGS_WORDS),)
.PHONY: $(LOGS_WORDS)
$(LOGS_WORDS):
	@:
endif
endif

# 配布物のバージョン: タグ上なら git tag（例 v0.1.0）、そうでなければ VERSION ファイル（v プレフィックス付与）。
SHERPA_VERSION := $(shell git describe --tags --exact-match 2>/dev/null || printf 'v%s' "$$(cat VERSION 2>/dev/null || echo 0.0.0)")

# テスト系ターゲットの Python。開発は .venv を正とする（無ければ python3 にフォールバック）。
PY ?= $(shell test -x .venv/bin/python && echo .venv/bin/python || echo python3)

# 開発ハーネスのゲート系ターゲットが比較する基点 ref（docs/20-開発ハーネス.md §5）。
BASE ?= main
# 単体+契約テストの壁時計予算（秒・確定値5分・docs/20-開発ハーネス.md §6）。環境変数で上書き可。
SHERPA_UNIT_BUDGET_SEC ?= 300
# make の変数代入（?= 含む）は既定では recipe のシェルへ自動で渡らない（コマンドライン代入や
# 元から環境変数だった場合を除く）。scripts/lib/gate_budget.sh は環境変数として読むため export する。
export SHERPA_UNIT_BUDGET_SEC

hooks:            ## git フック（scripts/git-hooks）を有効化＝Agent worktree の基点をローカル main に自動で揃える
	git config core.hooksPath scripts/git-hooks
	@echo "core.hooksPath=scripts/git-hooks（post-checkout: .claude/worktrees/agent-* の基点を main へ揃える）"

dev-setup:         ## 新しい貢献者向け: venv＋開発依存＋git フック＋.env雛形を1コマンドで用意（何度実行しても安全。CONTRIBUTING.md 参照）
	./scripts/dev-setup.sh

ifeq ($(LOGS_WORDS),)
help:             ## このコマンド一覧を表示
	@echo "Sherpa — make の使い方"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) \
		| sed -E 's/^([a-zA-Z0-9_-]+):[^#]*## /  \1|/' \
		| awk -F'|' '{printf "  %-18s %s\n", $$1, $$2}'
	@echo
	@echo "  よく使う順: make start → make status → make stop"
endif

start:            ## 利用者向け: 依存/ストア/アプリを一括起動（LAN=1 で LAN 公開・MODE=dev で開発モード）
	LAN="$(LAN)" ./scripts/start.sh $(MODE)

stop:             ## 利用者向け: 全部停止（アプリ＋Caddy＋ストア。KEEP_STORES=1 でストアを残す）
	KEEP_STORES="$(KEEP_STORES)" ./scripts/stop.sh

restart:          ## 利用者向け: アプリ（＋Caddy）を再起動（ストアは維持・LAN/MODE は start と同じ）
	KEEP_STORES=1 ./scripts/stop.sh || true
	LAN="$(LAN)" ./scripts/start.sh $(MODE)

status:           ## 利用者向け: 一枚看板の状態表示（ストア/アプリ/Caddy/URL）
	./scripts/status.sh

check-ports:      ## ポートの整合（compose 公開⇔アプリ接続先）と占有（他プロセス）を検査
	./scripts/check-ports.sh

# compose の `profiles` 付きサービス（OCR ワーカー）は、profile を指定した時だけ対象になる。
# down/ps/logs で付け忘れると「止めたつもりのワーカーが動き続ける」ため、常に付けて呼ぶ。
# up は既定 OFF を守るため付けない（OCR は `docker compose --profile ocr up -d ocr-worker`）。
# compose は自分のディレクトリの .env しか読まないため、SHERPA_ENV_FILE（本番 /etc/sherpa/sherpa.env 等）か
# リポジトリ直下の .env が存在すれば --env-file で渡す（scripts/run-common.sh の sherpa_compose と同じ契約）。
# シェル環境の明示指定は --env-file より優先される（compose の仕様）。
SHERPA_ENV_FILE ?= .env
# 呼び出し側が明示したファイルが無い場合は、リポジトリの .env／compose 既定へ黙って落とさない。
# 何も明示せず既定 .env 自体が無い fresh checkout だけは、compose の `${VAR:-default}` で従来どおり起動できる。
ifneq ($(filter environment command line override,$(origin SHERPA_ENV_FILE)),)
ifeq ($(wildcard $(SHERPA_ENV_FILE)),)
$(error 指定された SHERPA_ENV_FILE がありません: $(SHERPA_ENV_FILE))
endif
endif
COMPOSE_ENV_FLAG := $(if $(wildcard $(SHERPA_ENV_FILE)),--env-file "$(SHERPA_ENV_FILE)",)
COMPOSE := docker compose $(COMPOSE_ENV_FLAG)
COMPOSE_ALL := $(COMPOSE) --profile ocr

up:                ## ストア起動（Postgres/Neo4j/ES）＋ OCR ワーカー（前提が揃っていれば）
	$(COMPOSE) up -d
	@./scripts/ocr-up.sh

ocr-models:        ## OCR のモデルを取得（約134MB・閉域へはこのフォルダを丸ごとコピー）
	./scripts/fetch_ocr_models.sh

notice:            ## 帰属表示（NOTICE）・ライセンス全文・SBOM を生成（dist/notice/）
	$(PY) scripts/gen_notice.py

notice-check:      ## 上記の生成物が最新かを検査（差分があれば失敗）
	$(PY) scripts/gen_notice.py --check

doc-lint:           ## docs/templates・docs/design・docs/proposals の文書規約を検査（front matter/ID/リンク/アンカー/契約ブロック）
	$(PY) scripts/doc_lint.py

down:              ## ストア停止（OCR ワーカーも止める）
	$(COMPOSE_ALL) down

ps:                ## 状態（OCR ワーカーを含む）
	$(COMPOSE_ALL) ps

logs: export LOGS_WORDS := $(LOGS_WORDS)
logs: export LOGS_NAME := $(NAME)
logs: export LOGS_MEM := $(MEM)
logs: export LOGS_REPORT := $(REPORT)
logs: export LOGS_HELP := $(HELP)
logs: export LOGS_N := $(if $(n),$(n),$(N))
logs:              ## 全ログを1画面で（make l c e＝convert と embed・h で一覧・m でメモリ行・r で集計・err でエラーだけ・n=500 で末尾行数）
	./scripts/logs.sh $(ARGS)

l: logs            ## logs の短縮（make l c e m / make l h）

bootstrap:         ## ローカル利用ディレクトリ作成＋.env 用意＋ストア待ち
	./scripts/bootstrap.sh

install-docker:    ## Docker Engine を入れる（sudo パスワードを1回入力）
	bash scripts/install-docker.sh

test-unit:         ## 単体テスト（外部サービス不要・pytest -m unit）
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/unit -m unit

test-api:          ## FastAPI/TestClient 系（ものにより Neo4j/Postgres 使用・pytest -m api）
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/api -m api

test-contract:     ## 契約テスト（鏡モデルなど・pytest -m contract）
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/contract -m contract

test-integration:  ## 結合テスト（Neo4j/Postgres/ES など外部サービスを使うものを含む・pytest -m integration）
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/integration -m integration

test-e2e:           ## ブラウザ UI テスト（Playwright・DB不要・APIはモック）
	$(PY) -m pytest tests/e2e

screenshots:        ## マニュアル用画像を再生成（モックAPI＋Playwright・docker不要。ARGS で --only 等を渡せる）
	$(PY) scripts/capture_screenshots.py $(ARGS)

test: test-unit test-api test-contract test-integration  ## 単体＋API＋契約＋結合（ブラウザ系は含まない→test-e2e）

# --- 開発ハーネスのゲート（段階表は docs/20-開発ハーネス.md §5） ------------------------------

gate-slice:         ## 変更ファイルから該当テストを自動選択して実行（BASE=<ref> 既定 main）
	$(PY) scripts/gate_slice.py --base $(BASE)

# set -euo pipefail を使う target の shell。bash の場所は OS で違う（macOS は /bin/bash）ので PATH から解決する。
BASH_BIN := $(shell command -v bash 2>/dev/null || echo /bin/bash)
gate-merge: SHELL := $(BASH_BIN)
gate-merge:         ## マージ前ゲート（単体全件+契約＋変更領域の必須スイート。BASE=<ref> 既定 main）
	set -euo pipefail; \
	. scripts/lib/gate_budget.sh; \
	ruff check . ; \
	gate_run_unit_contract_budgeted "$(PY)" ; \
	areas_out=$$($(PY) scripts/gate_slice.py --base $(BASE) --areas-only) ; \
	echo "$$areas_out" ; \
	if echo "$$areas_out" | grep -q '^AREA:e2e$$'; then $(MAKE) test-e2e; fi ; \
	if echo "$$areas_out" | grep -q '^AREA:integration$$'; then $(MAKE) test-integration; fi ; \
	if echo "$$areas_out" | grep -q '^AREA:api$$'; then $(MAKE) test-api; fi

# gate-merge の「基本セット」は tests/unit tests/contract（DB不要）とし、tests/api は変更領域が
# api（sherpa/routers/** 変更）のときだけ追加実行する（tests/api 自体は Neo4j/Postgres を使う
# テストを含む＝DB 要否を機械的に仕分ける仕組みは無いため、DB が使える手元環境での運用に委ねる。
# routers/** を変更しない限り gate-merge は API 層を要求しない）。

gate-release: SHELL := $(BASH_BIN)
gate-release:       ## リリース前フルゲート（結合+ブラウザ結合+本番前チェック。VERIFY_KIT=1で追加）
	set -euo pipefail; \
	$(MAKE) test; \
	$(MAKE) test-e2e; \
	$(MAKE) prod-check; \
	if [ "$(VERIFY_KIT)" = "1" ]; then $(MAKE) verify-kit; fi

gate-ci: SHELL := $(BASH_BIN)
gate-ci:            ## CI用（ruff+単体全件+契約・予算チェック込み・Postgres serviceのみ前提）
	set -euo pipefail; \
	. scripts/lib/gate_budget.sh; \
	ruff check . ; \
	gate_run_unit_contract_budgeted "$(PY)"

test-inventory: SHELL := $(BASH_BIN)
test-inventory:     ## テスト件数表（ディレクトリ別）＋遅い20本（test-durations 呼び出し）
	set -euo pipefail; \
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/unit tests/contract --collect-only -q \
		| grep -oE '^tests/[^/]+/' | sort | uniq -c | sort -rn
	$(MAKE) test-durations

test-durations:     ## 単体+契約を --durations=20 で実行し遅い20本を表示
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/unit tests/contract -q --durations=20

test-db-reset:      ## テスト専用 DB を作り直す（DROP→CREATE・既定は共有 sherpa_test。DBNAME=sherpa_test_template でひな型を作り直す）
	$(PY) scripts/test_db_reset.py $(if $(DBNAME),--name $(DBNAME))

ifeq ($(LOGS_WORDS),)
api:               ## FastAPI 起動（dev 専用・fixtures フラグ ON＝架空 golden を grep 併用。本番では使わない→serve）
	./scripts/run-api.sh dev
endif

serve:             ## FastAPI 起動（本番・fixtures 非参照。SHERPA_ENV=production で fixtures を指す設定があれば起動拒否＝継承フラグも遮断）
	./scripts/run-api.sh serve

prod-check:        ## 本番 env/依存関係の軽い事前点検（SHERPA_ENV_FILE で env ファイル指定）
	./scripts/check-production.sh

verify-kit:        ## オフラインキットの出荷ゲート（docker必須・搬入先相当ホストへ--network noneで実導入。ARGSでキットのパス指定可・既定dist/offline-kit）
	./scripts/verify_offline_kit_apt.sh $(ARGS)

verify-extension:  ## 拡張の契約検査（アナライザ／頭脳provider／変換アーム／MCPツール・docs/21-拡張の契約.md）
	$(PY) scripts/verify_extension.py

dist: notice       ## 配布物 tarball を生成（版名＋sha256＋NOTICE/SBOM・fixtures/tests 非同梱）
	@mkdir -p dist
	# NOTICE/ライセンス全文/SBOM は生成物のため git archive には入らない。tar を素で作ってから
	# 追記し、最後に圧縮する（帰属表示は配布物に同梱されていなければ意味がない）。GNU tar／sha256sum が
	# あればそれを使い、無い環境（macOS の標準構成）だけ Python（tarfile／hashlib）で同じ中身にする。
	git archive --format=tar --prefix=sherpa-$(SHERPA_VERSION)/ \
		-o dist/sherpa-$(SHERPA_VERSION).tar HEAD
	if tar --version 2>/dev/null | grep -q 'GNU tar'; then \
		tar --append --file=dist/sherpa-$(SHERPA_VERSION).tar \
			--transform 's,^dist/notice,sherpa-$(SHERPA_VERSION),' \
			dist/notice/NOTICE.md dist/notice/THIRD-PARTY-LICENSES.txt dist/notice/sbom.cdx.json; \
	else \
		$(PY) scripts/lib/portable_tools.py tar-append dist/sherpa-$(SHERPA_VERSION).tar sherpa-$(SHERPA_VERSION) \
			dist/notice/NOTICE.md dist/notice/THIRD-PARTY-LICENSES.txt dist/notice/sbom.cdx.json; \
	fi
	gzip -f dist/sherpa-$(SHERPA_VERSION).tar
	if command -v sha256sum >/dev/null 2>&1; then \
		cd dist && sha256sum sherpa-$(SHERPA_VERSION).tar.gz > sherpa-$(SHERPA_VERSION).tar.gz.sha256; \
	else \
		$(PY) scripts/lib/portable_tools.py sha256 --basename dist/sherpa-$(SHERPA_VERSION).tar.gz > dist/sherpa-$(SHERPA_VERSION).tar.gz.sha256; \
	fi
	@echo "created: dist/sherpa-$(SHERPA_VERSION).tar.gz (+ .sha256)  展開すると sherpa-$(SHERPA_VERSION)/ フォルダ"

backup:            ## データを退避（停止中のストア＋個人領域＋.env → data/backups/<日時>/。ARGS=--stop/--with-derived/--dry-run）
	./scripts/backup.sh $(ARGS)

restore:           ## バックアップから戻す（make restore FROM=data/backups/<日時>・YES=1 で確認省略）
	@test -n "$(FROM)" || { echo "使い方: make restore FROM=data/backups/<日時>"; exit 2; }
	YES="$(YES)" ./scripts/restore.sh "$(FROM)"

usage-backfill:    ## 既存のassistantメッセージをturn_metrics/turn_tool_statsへ一度だけ移す（冪等・複数回実行可）
	./scripts/usage-backfill.sh

trace:             ## 会話の各ターンを段・道具の呼び出し（時刻・引数・件数・打ち切り）・トークン・調査台帳の流れで書き出す（make trace CONV=<会話番号>[,<会話番号>...] [MASK=1 [MASK_MODELS=1]] [OUT=<出力ファイル>]・読み取りだけ・MASK=1 で本文や資料名を伏せる・MASK_MODELS=1 でモデル名も伏せる）
	@case "$${CONV:-}" in ""|*[!0-9,]*) echo "使い方: make trace CONV=<会話番号>[,<会話番号>...] [MASK=1] [OUT=<出力ファイル>]（会話番号は数字とカンマだけ）"; exit 2;; esac; \
	set -- --conv "$${CONV}"; \
	if [ "$${MASK:-}" = 1 ]; then set -- "$$@" --mask; fi; \
	if [ "$${MASK_MODELS:-}" = 1 ]; then set -- "$$@" --mask-models; fi; \
	if [ -n "$${OUT:-}" ]; then set -- "$$@" --out "$${OUT}"; fi; \
	./scripts/conversation-trace.sh "$$@"

azure-smoke:        ## Azure OpenAI（等の OpenAI 互換接続先）への実疎通を確認（実 API 課金あり・確認プロンプト）。ARGS で --env-file/--dry-run 等を渡せる（例: ARGS="--env-file azure.env --yes"）
	$(PY) scripts/azure_smoke.py $(ARGS)

codex-compat:      ## Codex CLI を新しい版へ上げる前の互換の通し試験（実 API は呼ばない・偽の接続先で本番の Codex 実行経路を1ターン確認）
	SHERPA_USE_FIXTURES=1 $(PY) scripts/codex_compat.py

doctor:            ## 導入先の統合セットアップ検査（ストア疎通/ES版+kuromoji/設定/LLM最小プローブ/Codex経路・読み取り専用）。PROBE_CLOUD=1 で課金プロバイダの実接続も確認
	PROBE_CLOUD="$(PROBE_CLOUD)" ./scripts/doctor.sh

sandbox-check:     ## Codex のサンドボックス（bubblewrap）の前提を確認（Ubuntu 23.10+ の AppArmor ユーザー名前空間制限・root不要）。直すには sudo bash scripts/setup-codex-sandbox.sh apply
	./scripts/setup-codex-sandbox.sh check

codex-install:      ## 固定版 Codex CLI（scripts/codex-version.env）を tools/codex/ に導入（既に固定版なら何もしない・管理者権限不要。make start も同じ確認を行う）
	./scripts/codex_install.sh

codex-version:      ## 固定版と、tools/codex/・PATH 上に実際にある Codex CLI の版を表示（導入は行わない）
	./scripts/codex_install.sh --check

diag:              ## 解析用のログ回収バンドルを作る（機密を含めない・dist/diag/）。ARGS で --days/--out 等を渡せる
	./scripts/diag.sh $(ARGS)

nuke:              ## 完全初期化（ストア＋派生物＋個人領域＋OCR観測を消去。資料フォルダと .env は残す。確認は 2 回・端末必須。スクリプト用の省略は YES=I-UNDERSTAND-ALL-DATA-WILL-BE-DELETED・本番では不可）
	YES="$(YES)" ./scripts/nuke.sh
