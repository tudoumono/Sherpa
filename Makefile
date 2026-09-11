# Sherpa MVP — 起動・運用タスク
#
# `make` だけを打つと、下の一覧（help）が出ます。
.PHONY: help start stop restart status check-ports up down ps logs bootstrap demo mirror install-docker ocr-models \
        graph-load graph-verify graph api serve prod-check verify-kit verify-extension dist nuke notice notice-check \
        test test-unit test-api test-contract test-integration test-e2e test-e2e-live \
        test-ui-automation test-ui-automation-smoke test-ui-automation-chat test-ui-automation-env \
        test-db-reset screenshots backup restore azure-smoke doctor \
        gate-slice gate-merge gate-release gate-ci test-inventory test-durations

# 引数なしの `make` は一覧表示にする（いきなりサーバが起動すると事故になるため）。
.DEFAULT_GOAL := help

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

help:             ## このコマンド一覧を表示
	@echo "Sherpa — make の使い方"
	@echo
	@grep -hE '^[a-zA-Z0-9_-]+:.*## ' $(MAKEFILE_LIST) \
		| sed -E 's/^([a-zA-Z0-9_-]+):[^#]*## /  \1|/' \
		| awk -F'|' '{printf "  %-18s %s\n", $$1, $$2}'
	@echo
	@echo "  よく使う順: make start → make status → make stop"

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
	@./scripts/ocr-up.sh || true

ocr-models:        ## OCR のモデルを取得（約134MB・閉域へはこのフォルダを丸ごとコピー）
	./scripts/fetch_ocr_models.sh

notice:            ## 帰属表示（NOTICE）・ライセンス全文・SBOM を生成（dist/notice/）
	$(PY) scripts/gen_notice.py

notice-check:      ## 上記の生成物が最新かを検査（差分があれば失敗）
	$(PY) scripts/gen_notice.py --check

down:              ## ストア停止（OCR ワーカーも止める）
	$(COMPOSE_ALL) down

ps:                ## 状態（OCR ワーカーを含む）
	$(COMPOSE_ALL) ps

logs:              ## 全ログを1画面で（アプリ+Docker+mem合流・ARGS で絞り込み: convert embed postgres 等。-r でレポート・-h でヘルプ）
	./scripts/logs.sh $(ARGS)

bootstrap:         ## ローカル利用ディレクトリ作成＋.env 用意＋ストア待ち
	./scripts/bootstrap.sh

demo:              ## M0 demo: Codex が workspace で走り kb を read-only で読める
	./scripts/demo_codex.sh

mirror:            ## 鏡モデル契約テスト（docker 不要）
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/contract/test_mirror_contract.py -m contract

install-docker:    ## Docker Engine を入れる（sudo パスワードを1回入力）
	bash scripts/install-docker.sh

graph-load:        ## v1 world を Neo4j に投入（鏡モデル・make up ＋ pip install -r requirements.txt 必要）
	SHERPA_USE_FIXTURES=1 $(PY) -c "from sherpa import worlds; from sherpa.ingest import world_graph as g, world_neo4j as w; wd=worlds.world_dir('v1'); n,e,f=g.build_world(wd,'v1'); env=w._env(); print('loaded', w.load_world(n,e,'v1',env['uri'],env['user'],env['pw']),'flags',f)"

graph-verify:      ## 実 Neo4j で v1 world の影響 golden を再現するか検証
	SHERPA_USE_FIXTURES=1 $(PY) scripts/graph_verify.py

graph: graph-load graph-verify  ## 投入＋検証

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

test-e2e-live:      ## ブラウザ結合 UI テスト（起動済み FastAPI/DB を実利用。SHERPA_AUTH_ENABLED=1 前提）
	$(PY) -m pytest tests/e2e_live

test-ui-automation: ## 独立UI自動化: 全機能・全env・全実サービス（実API課金あり）
	$(PY) -m ui_automation run --suite full $(UI_AUTOMATION_ARGS)

test-ui-automation-smoke: ## 独立UI自動化: 実stackの最小確認（全網羅の合格証明ではない）
	$(PY) -m ui_automation run --suite smoke $(UI_AUTOMATION_ARGS)

test-ui-automation-chat: ## 独立UI自動化: 実AIチャット＋構造化実行トレース（実API課金あり）
	$(PY) -m ui_automation run --suite chat $(UI_AUTOMATION_ARGS)

test-ui-automation-env: ## 独立UI自動化: 環境変数マトリクス（profileごとに実processを再起動）
	$(PY) -m ui_automation run --suite env $(UI_AUTOMATION_ARGS)

screenshots:        ## マニュアル用画像を再生成（モックAPI＋Playwright・docker不要。ARGS で --only 等を渡せる）
	$(PY) scripts/capture_screenshots.py $(ARGS)

test: test-unit test-api test-contract test-integration  ## 単体＋API＋契約＋結合（ブラウザ系は含まない→test-e2e / test-e2e-live）

# --- 開発ハーネスのゲート（段階表は docs/20-開発ハーネス.md §5） ------------------------------

gate-slice:         ## 変更ファイルから該当テストを自動選択して実行（BASE=<ref> 既定 main）
	$(PY) scripts/gate_slice.py --base $(BASE)

gate-merge: SHELL := /usr/bin/bash
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

gate-release: SHELL := /usr/bin/bash
gate-release:       ## リリース前フルゲート（結合+ブラウザ結合+本番前チェック。VERIFY_KIT=1で追加）
	set -euo pipefail; \
	$(MAKE) test; \
	$(MAKE) test-e2e; \
	$(MAKE) prod-check; \
	if [ "$(VERIFY_KIT)" = "1" ]; then $(MAKE) verify-kit; fi

gate-ci: SHELL := /usr/bin/bash
gate-ci:            ## CI用（ruff+単体全件+契約・予算チェック込み・Postgres serviceのみ前提）
	set -euo pipefail; \
	. scripts/lib/gate_budget.sh; \
	ruff check . ; \
	gate_run_unit_contract_budgeted "$(PY)"

test-inventory: SHELL := /usr/bin/bash
test-inventory:     ## テスト件数表（ディレクトリ別）＋遅い20本（test-durations 呼び出し）
	set -euo pipefail; \
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/unit tests/contract --collect-only -q \
		| grep -oE '^tests/[^/]+/' | sort | uniq -c | sort -rn
	$(MAKE) test-durations

test-durations:     ## 単体+契約を --durations=20 で実行し遅い20本を表示
	SHERPA_USE_FIXTURES=1 $(PY) -m pytest tests/unit tests/contract -q --durations=20

test-db-reset:      ## テスト専用 DB sherpa_test を作り直す（DROP→CREATE・無ければ CREATE のみ）
	$(PY) scripts/test_db_reset.py

api:               ## FastAPI 起動（dev 専用・fixtures フラグ ON＝架空 golden を grep 併用。本番では使わない→serve）
	./scripts/run-api.sh dev

serve:             ## FastAPI 起動（本番・fixtures 非参照。SHERPA_ENV=production で fixtures を指す設定があれば起動拒否＝継承フラグも遮断）
	./scripts/run-api.sh serve

prod-check:        ## 本番 env/依存関係の軽い事前点検（SHERPA_ENV_FILE で env ファイル指定）
	./scripts/check-production.sh

verify-kit:        ## オフラインキットの出荷ゲート（docker必須・搬入先相当ホストへ--network noneで実導入。ARGSでキットのパス指定可・既定dist/offline-kit）
	./scripts/verify_offline_kit_apt.sh $(ARGS)

verify-extension:  ## 拡張の契約検査（アナライザ／頭脳provider／変換アーム／MCPツール・docs/21-拡張の契約.md）
	$(PY) scripts/verify_extension.py

dist: notice       ## 配布物 tarball を生成（版名＋sha256＋NOTICE/SBOM・fixtures/tests/mockups 非同梱）
	@mkdir -p dist
	# NOTICE/ライセンス全文/SBOM は生成物のため git archive には入らない。tar を素で作ってから
	# 追記し、最後に圧縮する（帰属表示は配布物に同梱されていなければ意味がない）。
	git archive --format=tar --prefix=sherpa-$(SHERPA_VERSION)/ \
		-o dist/sherpa-$(SHERPA_VERSION).tar HEAD
	tar --append --file=dist/sherpa-$(SHERPA_VERSION).tar \
		--transform 's,^dist/notice,sherpa-$(SHERPA_VERSION),' \
		dist/notice/NOTICE.md dist/notice/THIRD-PARTY-LICENSES.txt dist/notice/sbom.cdx.json
	gzip -f dist/sherpa-$(SHERPA_VERSION).tar
	cd dist && sha256sum sherpa-$(SHERPA_VERSION).tar.gz > sherpa-$(SHERPA_VERSION).tar.gz.sha256
	@echo "created: dist/sherpa-$(SHERPA_VERSION).tar.gz (+ .sha256)  展開すると sherpa-$(SHERPA_VERSION)/ フォルダ"

backup:            ## データを退避（停止中のストア＋個人領域＋.env → data/backups/<日時>/。ARGS=--stop/--with-derived/--dry-run）
	./scripts/backup.sh $(ARGS)

restore:           ## バックアップから戻す（make restore FROM=data/backups/<日時>・YES=1 で確認省略）
	@test -n "$(FROM)" || { echo "使い方: make restore FROM=data/backups/<日時>"; exit 2; }
	YES="$(YES)" ./scripts/restore.sh "$(FROM)"

azure-smoke:        ## Azure OpenAI（等の OpenAI 互換接続先）への実疎通を確認（実 API 課金あり・確認プロンプト）。ARGS で --env-file/--dry-run 等を渡せる（例: ARGS="--env-file azure.env --yes"）
	$(PY) scripts/azure_smoke.py $(ARGS)

doctor:            ## 導入先の統合セットアップ検査（ストア疎通/ES版+kuromoji/設定/LLM最小プローブ/Codex経路・読み取り専用）。PROBE_CLOUD=1 で課金プロバイダの実接続も確認
	PROBE_CLOUD="$(PROBE_CLOUD)" ./scripts/doctor.sh

nuke:              ## 完全初期化（ストア＋派生物＋個人領域＋OCR観測を消去。資料フォルダと .env は残す。YES=1 で確認省略）
	YES="$(YES)" ./scripts/nuke.sh
