# Test Layout

Sherpa のテストは依存の重さで分けています。

- `unit/`: 外部サービスなしで走る単体寄りのテスト。必要に応じて fixtures や stub は使います。
- `api/`: FastAPI `TestClient` 経由の API/画面配信テスト。テストによって Neo4j/Postgres が必要です。
- `integration/`: Neo4j/Postgres/Elasticsearch など実サービスを使う結合テスト、またはそれに近い受け入れテスト。
- `contract/`: 鏡モデルなど、設計上の契約を固定するテスト。
- `e2e/`: Playwright で実ブラウザを操作する UI テスト。DB/API はモックし、`web/` を静的配信して検証します。
- `e2e_live/`: Playwright で実ブラウザを操作し、起動済み FastAPI/DB/個人 workspace を実際に叩く結合 UI テスト。

共通の fixture/helper は `tests/_world_setup.py` に置き、カテゴリ配下のテストから参照します。
ルートの `tests/conftest.py` が、リポジトリルートと `tests/` を `sys.path` に載せ、テストの所在
ディレクトリ（`tests/unit/…` 等）に応じて `unit`/`api`/`contract`/`integration`/`e2e`/`e2e_live`
マーカーを自動付与します（`-m unit` などの選別がファイル書き換えなしで効きます）。

`tests/api/conftest.py` と `tests/integration/conftest.py` が、それぞれのディレクトリ配下の
どの `test_*.py` よりも先に読み込まれる性質を使い、import 時にしか読まれない env
（`sherpa.api._USERS_DIR` / `_WORKSPACE_TTL_DAYS` 等）をディレクトリ内で一度だけ確定します。
ログイン不要の互換モード（`SHERPA_AUTH_DISABLED=1`）が要るテストは、`auth_disabled` fixture
（`tests/api/conftest.py` 提供）か、ファイル内の `autouse` fixture で明示します
（`auth.auth_disabled()` は呼び出し時に env を読むため、`monkeypatch` で足りテスト終了後に自動復元）。

## 開発環境（venv）

開発・テストは `.venv` を正とします。テスト実行者は dev 依存を入れてください。
利用者向けの `scripts/start.sh` はランタイム依存（`requirements.txt`）だけを入れるため、
テスト依存（pytest/ruff/playwright ほか）は入りません。

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt -c constraints.txt
```

- `requirements.txt`: ランタイム依存のみ。
- `requirements-dev.txt`: `-r requirements.txt` ＋ テスト/lint 用（httpx, pytest, pytest-cov, ruff, playwright）。
- `constraints.txt`: 再現用のバージョンピン止め（`.venv` の `pip freeze` 由来）。更新は venv 再構築時に
  `pip freeze` で作り直します。

## 実行

```bash
make test-unit          # pytest tests/unit -m unit（外部サービス不要）
make test-contract      # pytest tests/contract -m contract（外部サービス不要）
make test-api           # pytest tests/api -m api（Neo4j/Postgres を使うものを含む）
make test-integration   # pytest tests/integration -m integration（実サービスを使う）
make test-e2e           # pytest tests/e2e（ブラウザ・DB不要・API はモック）
make test-e2e-live      # pytest tests/e2e_live（起動済み FastAPI/DB を実利用）
make test               # unit + api + contract + integration
```

各 test ターゲットの Python は Makefile の `PY`（既定 `.venv/bin/python`、無ければ `python3`）で解決します。

`unit`/`api`/`contract`/`integration` はすべて pytest に一本化済みです（フェーズ0 スライス2）。
`api`/`integration` はディレクトリ配下の `conftest.py` が env の確定と `auth_disabled` fixture を
提供することで、1プロセス内での複数ファイル同居（import 時 env 衝突・`TestClient` 生成順）を
解消しています。

pytest を直に使う場合の例:

```bash
SHERPA_USE_FIXTURES=1 .venv/bin/python -m pytest                                  # 速い層（既定収集=unit+contract）
SHERPA_USE_FIXTURES=1 .venv/bin/python -m pytest tests/api -m api                 # API 層（要ストア）
SHERPA_USE_FIXTURES=1 .venv/bin/python -m pytest tests/integration -m integration # 結合層（要ストア）
SHERPA_USE_FIXTURES=1 .venv/bin/python -m pytest tests/unit -m unit --cov=sherpa  # カバレッジ
```

既定収集（`testpaths`）は `tests/unit` と `tests/contract` だけです（外部サービス不要で数秒〜数分の
速い層のみを既定にする狙い）。`api`/`integration` はストアが要るため明示パス指定
（`pytest tests/api` 等）で実行します。

`e2e/`・`e2e_live/` は既定の収集から外してあります（ブラウザ/起動済みサーバが要るため）。
`python3 -m pytest tests/e2e` のように**明示指定**すれば従来どおり実行できます。

E2E は初回だけブラウザの導入が必要です。

```bash
.venv/bin/python -m playwright install chromium
```

Live E2E は、別ターミナルで認証有効の FastAPI を起動してから実行します。既定の接続先は `http://127.0.0.1:8000` です。

```bash
SHERPA_ADMIN_PASSWORD=admin-pass SHERPA_ENV=dev SHERPA_USE_FIXTURES=1 \
  .venv/bin/python -m uvicorn sherpa.api:app --host 127.0.0.1 --port 8000

SHERPA_E2E_LIVE_BASE_URL=http://127.0.0.1:8000 SHERPA_ADMIN_PASSWORD=admin-pass \
  make test-e2e-live
```

`e2e_live/` は admin ログイン、ユーザー作成、権限拒否、workspace upload/search/delete、会話共有の受領までを実サービスで確認します。Codex/MCP の実 smoke は重いため既定では skip し、必要な環境が揃った時だけ `SHERPA_E2E_LIVE_MCP=1 make test-e2e-live` で走らせます。

## CI

`.github/workflows/test.yml` が push/PR で lint＋`unit`/`contract` を回し、PR ではストアを起動して
`pytest tests/api -m api` を実行します（詳細は `docs/proposals/2026-07-02-リファクタリング計画.md` フェーズ0）。
