"""FastAPI アプリの入口。各 router を include し、起動時処理（lifespan が呼ぶ `_warn_*`・シード・孤児掃除・TTL 掃除）と `/ui` 配信を持つ。

API パラメータの資料フォルダ指定は `world` のみ。
設計: docs/design/architecture.md「コンポーネント図（主要コンテナごと）」
"""
from __future__ import annotations

import logging
import os
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
    swagger_ui_default_parameters,
)
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from sherpa import auth, ext_api, store, worlds
# テストが `api.scope_mod` を属性参照するため import を維持する
from sherpa import scope as scope_mod
from sherpa.deps import (
    _USERS_DIR,
    _browse_roots,
    _ensure_initial_admin,
    _remove_codex_session_dir,
    _require_world,
    _validate_new_password,
    ensure_workspace,
)
from sherpa.routers import (
    admin_users,
    audit_usage,
    chat,
    conversations,
    graph,
    impact,
    improvement_log,
    shares,
    system,
    system_extras,
    workspace,
)
from sherpa.routers import auth as auth_routes
from sherpa.routers import documents as documents_routes
from sherpa.routers import worlds as worlds_routes
# 既存テストが `sherpa.api` 属性参照で取るため再エクスポートする
from sherpa.routers.shares import conversation_share_create  # noqa: F401
from sherpa.routers.workspace import (  # noqa: F401
    _WORKSPACE_ALLOWED_EXT,
    _WORKSPACE_SEARCHABLE_EXT,
    _safe_workspace_filename,
    workspace_file_download,
    workspace_search,
)
# 同上
from sherpa.routers.worlds import _ingest_summary, _subdirs  # noqa: F401
# 同上（`ChatReq`・`_persist_turn_crash` をテストが `sherpa.api` 経由で参照する）
from sherpa.routers.chat import (  # noqa: F401
    ChatReq,
    _persist_turn_crash,
)
from sherpa.lifespan import lifespan

_log = logging.getLogger("sherpa")

# `_require_world` は `tests/integration/test_worlds_admin.py` が `sherpa.api` から import するため再エクスポートする

_TAGS_METADATA = [
    {"name": "認証", "description": "ログイン・ログアウト・現在ユーザー取得。"},
    {"name": "管理者:ユーザー管理", "description": "ユーザーの作成・一覧・無効化・role変更・パスワード再設定（管理者のみ）。"},
    {"name": "管理者:監査ログ", "description": "監査ログの閲覧とhash-chain整合性検証（管理者のみ）。"},
    {"name": "管理者:利用統計", "description": "会話・メッセージから集計する利用統計（ヒアリング候補の発見・管理者のみ）。"},
    {"name": "運営掲示板", "description": "トップ画面のお知らせ（メンテナンス・活用事例・お知らせ）の閲覧・投稿・編集・削除。"},
    {"name": "会話共有", "description": "会話の共有リンク発行・受領・取消。"},
    {"name": "個人ワークスペース", "description": "個人ファイルのアップロード・一覧・削除・grep検索（共有KBには索引化しない）。"},
    {"name": "範囲", "description": "検索・分析対象をフォルダ単位で絞り込むスコープツリー。"},
    {"name": "チャット", "description": "チャットのターンの開始・SSE での受け取り・停止。"},
    {"name": "設定", "description": "AIプロバイダ・モデル・system_prompt等の設定取得/更新/接続テスト。"},
    {"name": "会話管理", "description": "会話履歴の一覧・取得・削除・ピン留め・改名。"},
    {"name": "文書", "description": "文書台帳の参照と原本ダウンロード。"},
    {"name": "資料フォルダ(World)管理", "description": "登録ディレクトリ（world）の登録・状態確認・差分・再取込・削除、フォルダ選択。"},
    {"name": "ナレッジグラフ", "description": "ナレッジグラフの可視化データ・検索。"},
    {"name": "システム", "description": "ヘルスチェック・ルート・静的UI配信。"},
    {"name": "管理者:外部APIキー", "description": "外部連携 API（/ext/v1）のキー発行・一覧・失効（管理者のみ）。"},
]

app = FastAPI(
    title="Sherpa MVP",
    description="社内文書向け Agentic RAG 基盤。取り込み→抽出→ナレッジグラフ→影響分析／チャット／トラブルシュートまでを提供するAPI。",
    openapi_tags=_TAGS_METADATA,
    lifespan=lifespan,
    # /docs（Swagger UI）は web/vendor/ 同梱資産だけで配信する自前ルート（外部ネットワーク参照なし・下記2関数）。
    # ルート表の定義順を保つため `FastAPI()` 直後に置く。ReDoc は提供しない
    docs_url=None,
    redoc_url=None,
)


@app.get("/docs", include_in_schema=False)
async def swagger_ui_html() -> HTMLResponse:
    return get_swagger_ui_html(
        openapi_url=app.openapi_url,
        title=f"{app.title} - Swagger UI",
        oauth2_redirect_url=app.swagger_ui_oauth2_redirect_url,
        swagger_js_url="/ui/vendor/swagger-ui-bundle.js",
        swagger_css_url="/ui/vendor/swagger-ui.css",
        swagger_favicon_url="/ui/vendor/swagger-ui-favicon.png",
        # validatorUrl は外部への問い合わせ先＝閉域LANでは到達できない
        swagger_ui_parameters={**swagger_ui_default_parameters, "validatorUrl": None},
    )


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
async def swagger_ui_redirect() -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


# 外部連携 API（/ext/v1）。`ext_api` は `sherpa.api` を import しない（循環回避）
app.include_router(ext_api.router)
# /ext/v1/* の全応答へ X-Request-Id を付与し、認証成功後の終了経路を1リクエスト=1行で監査する（生 ASGI ミドルウェア）
app.add_middleware(ext_api.ExtRequestMiddleware)




_auth_startup_audited = False













app.include_router(auth_routes.auth_router)


app.include_router(admin_users.router)


app.include_router(audit_usage.audit_usage_router)


app.include_router(improvement_log.improvement_log_router)


app.include_router(shares.router)


app.include_router(workspace.router)


app.include_router(impact.impact_router)


app.include_router(chat.chat_router)


app.include_router(system.settings_router)


app.include_router(conversations.router)


app.include_router(documents_routes.download_router)


app.include_router(worlds_routes.ingest_preview_router)


app.include_router(documents_routes.documents_router)


app.include_router(worlds_routes.worlds_router)


app.include_router(graph.router)


app.include_router(worlds_routes.ingest_runs_router)


app.include_router(system.healthz_router)


app.include_router(system_extras.extras_router)


def _auth_bootstrap_on_startup():
    """認証の起動時状態をDBへ記録する（DB不可ならログのみ）。lifespan が起動時に呼ぶ。"""
    global _auth_startup_audited
    if _auth_startup_audited:
        return
    _auth_startup_audited = True
    try:
        if auth.auth_disabled():
            store.audit("system:startup", "auth.disabled_mode", "system", "auth",
                        detail={"env": "SHERPA_AUTH_DISABLED"},
                        outcome="success", severity="warning")
        else:
            _ensure_initial_admin()
    except Exception as e:
        _log.warning("auth startup audit/bootstrap skipped: %s", e)


def _under_fixtures(p) -> bool:
    """path（env 値や registry root）が symlink 追跡後に fixtures コーパスを指すか（`.resolve()` で alias も実体まで辿る）。"""
    if not p:
        return False
    try:
        return "fixtures" in Path(p).resolve().parts
    except Exception:
        return False


def _warn_fixtures():
    """fixtures（架空 golden）に到達し得る設定なら起動時に大きく警告する（本番で silently ON にしない）。

    点検対象は `SHERPA_USE_FIXTURES`・`SHERPA_KB_DIR`/`SHERPA_DERIVED_DIR`・DB レジストリの world root_path（判定は symlink 追跡後）。
    `SHERPA_ENV` が prod/production でいずれか1つでも fixtures を指せば起動拒否（fail-closed）、dev は大警告のうえ続行する。
    """
    import logging
    reasons = ["SHERPA_USE_FIXTURES が有効"] if worlds._fixtures() else []
    for name in ("SHERPA_KB_DIR", "SHERPA_DERIVED_DIR"):
        val = os.environ.get(name)
        if _under_fixtures(val):
            reasons.append("%s=%s が fixtures を指す（symlink 追跡後）" % (name, val))
    try:  # 主経路＝登録 world の root_path が fixtures（DB best-effort）
        for row in store.list_worlds_db():
            if _under_fixtures(row["root_path"]):
                reasons.append("登録 world '%s' の root_path が fixtures を指す" % row["world_id"])
    except Exception:
        pass
    if not reasons:  # fixtures へ到達する設定が一切無い＝正常系
        return
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    is_prod = env in ("prod", "production")
    log = logging.getLogger("sherpa")
    banner = (
        "\n" + "!" * 72 + "\n"
        "!! DEV/TEST MODE: fixtures（架空 golden コーパス）に到達し得る設定です。\n"
        "!!   理由: " + " / ".join(reasons) + "\n"
        "!! これはテスト/開発専用です。本番環境では絶対に設定しないでください（本番非参照）。\n"
        "!! 本番起動は `make serve`（フラグ無し・SHERPA_ENV=production）を使用してください。\n"
        + "!" * 72)
    if is_prod:  # 本番マーカー＋fixtures 到達経路＝設定ミス → fail-closed
        log.error(banner)
        raise RuntimeError(
            "設定ミス: SHERPA_ENV=%s（本番）で fixtures に到達し得る設定があります（%s）。"
            "本番でテストデータを配信しないため起動を拒否します（該当を外すか `make serve` を使用）。"
            % (env, " / ".join(reasons)))
    log.warning(banner)


def _warn_test_db_isolated():
    """`SHERPA_TEST_DB_ISOLATED`（テスト用 DB 分離の内部フラグ）が起動時に立っていたら大きく警告する（本番なら起動拒否）。

    このフラグは孤児掃除（`reconcile_derivatives()`）を無警告で全面 skip させるため、実運用に残ると掃除が恒久的に無効になる。
    dev は大警告のうえ続行、`SHERPA_ENV=production` は起動拒否（`_warn_fixtures` と同じ流儀）。
    """
    import logging
    if not os.environ.get("SHERPA_TEST_DB_ISOLATED"):
        return
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    is_prod = env in ("prod", "production")
    log = logging.getLogger("sherpa")
    banner = (
        "\n" + "!" * 72 + "\n"
        "!! DEV/TEST MODE: SHERPA_TEST_DB_ISOLATED が有効です。\n"
        "!!   これが立っていると、孤児派生物の自動掃除（reconcile_derivatives）が\n"
        "!!   Neo4j/ES/data-derived について全面的に skip されます（テスト専用の安全策）。\n"
        "!! これはテスト専用です。本番/実運用プロセスでは絶対に設定しないでください。\n"
        + "!" * 72)
    if is_prod:
        log.error(banner)
        raise RuntimeError(
            "設定ミス: SHERPA_ENV=%s（本番）で SHERPA_TEST_DB_ISOLATED が有効です。"
            "孤児掃除が無効化されたまま本番稼働しないよう起動を拒否します（該当 env を外してください）。"
            % env)
    log.warning(banner)


def _warn_codex_sandbox_disabled():
    """production で Codex sandbox（`agents._codex_sandbox_enabled()`）が無効なら起動を拒否する。

    無効だと読取封じ込め（permission profile）が外れ、旧 `-s workspace-write` へフォールバックする（緊急時専用の逃げ道）。
    dev は大警告のうえ続行、`SHERPA_ENV=production` は起動拒否。
    """
    import logging
    from sherpa import agents
    if agents._codex_sandbox_enabled():
        return
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    is_prod = env in ("prod", "production")
    log = logging.getLogger("sherpa")
    banner = (
        "\n" + "!" * 72 + "\n"
        "!! SHERPA_CODEX_SANDBOX が無効です（Codex sandbox off）。\n"
        "!!   Codex 実行時の読取封じ込め（permission profile・KB＋authoring 限定）が外れ、\n"
        "!!   旧 `-s workspace-write`（読取全開）へフォールバックします。\n"
        "!! これは緊急時専用の逃げ道です。通常運用・本番では有効にしてください。\n"
        + "!" * 72)
    if is_prod:
        log.error(banner)
        raise RuntimeError(
            "設定ミス: SHERPA_ENV=%s（本番）で SHERPA_CODEX_SANDBOX が無効です。"
            "Codex の読取封じ込めが外れたまま本番稼働しないよう起動を拒否します"
            "（SHERPA_CODEX_SANDBOX を未設定にするか有効値にしてください）。"
            % env)
    log.warning(banner)


def _warn_default_admin_password():
    """`SHERPA_ENV=production` で初期 admin パスワード（`SHERPA_ADMIN_PASSWORD`）が未設定なら、`_ensure_initial_admin()` が DB に刻む前に起動拒否する。

    判定は env のみ（DB のハッシュは見ない）。空白のみは未設定と同じ扱い。明示設定があれば開発既定と同値でも起動を許す
    （初回ログインでパスワード変更が強制されるため）。
    """
    import logging
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    is_prod = env in ("prod", "production")
    configured = os.environ.get("SHERPA_ADMIN_PASSWORD")
    if configured is not None and configured.strip():
        return  # 明示設定あり＝起動許可
    log = logging.getLogger("sherpa")
    reason = "SHERPA_ADMIN_PASSWORD が未設定です"
    banner = (
        "\n" + "!" * 72 + "\n"
        "!! " + reason + "（初期 admin パスワードの明示設定が必要）。\n"
        "!! .env（または SHERPA_ENV_FILE の env ファイル）に SHERPA_ADMIN_PASSWORD を実際の値で設定してください。\n"
        + "!" * 72)
    if is_prod:
        log.error(banner)
        raise RuntimeError(
            "設定ミス: SHERPA_ENV=%s（本番）で%s。"
            "開発既定パスワードのまま本番稼働しないよう起動を拒否します"
            "（SHERPA_ADMIN_PASSWORD を実際の値に設定してください）。"
            % (env, reason))
    log.warning(banner)


def _warn_change_me_placeholders():
    """`SHERPA_ENV=production` で env の値に配布テンプレのプレースホルダ（`CHANGE_ME`）が残っていたら起動拒否する。

    該当したキー名はログへ平文で出し、値は伏せる。
    """
    import logging
    hit_keys = sorted(k for k, v in os.environ.items() if v and "CHANGE_ME" in v)
    if not hit_keys:
        return
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    is_prod = env in ("prod", "production")
    log = logging.getLogger("sherpa")
    keys_text = ", ".join(hit_keys)
    banner = (
        "\n" + "!" * 72 + "\n"
        "!! env にプレースホルダ（CHANGE_ME）が残っています: " + keys_text + "\n"
        "!!   .env.example の「0. 本番チェックリスト」節を埋め忘れていませんか？\n"
        "!! 配布テンプレの値のままです。実際の値に置き換えてください。\n"
        + "!" * 72)
    if is_prod:
        log.error(banner)
        raise RuntimeError(
            "設定ミス: SHERPA_ENV=%s（本番）で CHANGE_ME プレースホルダが残っている env があります"
            "（%s）。テンプレの値のまま本番稼働しないよう起動を拒否します（実際の値に置き換えてください）。"
            % (env, keys_text))
    log.warning(banner)


def _warn_auth_disabled_in_production():
    """本番プロファイルで `SHERPA_AUTH_DISABLED` が設定されていたら起動時に1回だけ ERROR ログを残す（検知のみで起動は止めない）。

    `auth.auth_disabled()` は production ではこの env を無視する。
    """
    import logging
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    if env not in ("prod", "production"):
        return
    if not os.environ.get("SHERPA_AUTH_DISABLED"):
        return
    logging.getLogger("sherpa").error(
        "設定ミス: SHERPA_ENV=%s（本番）で SHERPA_AUTH_DISABLED が設定されていますが無視されます"
        "（本番プロファイルでは合成 admin の互換モードは常に無効です・env から削除してください）。",
        env)


# env → system_settings への初回シードのみ（以後 env は読まない）。対象は `sherpa.keys` が使うキー（openai の API キー）・
# 個人キー許可フラグ・Web 検索管理者許可フラグ。`OLLAMA_URL` は `_seed_ollama_url_from_env()` が独立マーカーで扱う
_SEED_ENV_KEYS = (
    ("OPENAI_API_KEY", "openai_api_key"),
)

_SEED_SECRET_KEYS = frozenset({"openai_api_key"})

# 資格情報シード完了マーカー。存在すれば env は二度と読まない（`store.seed_system_settings_once` が全 INSERT をこのキーの存在で条件付ける）
_CREDENTIAL_SEED_MARKER_KEY = "credential_seed_version"
_CREDENTIAL_SEED_VERSION = 1


def _seed_settings_from_env():
    """env の資格情報を system_settings へ一生に一度だけ取り込む（`OLLAMA_URL` は対象外）。

    完了マーカー（`credential_seed_version`）の事前チェックは安価な早期 return で、不変条件は `store.seed_system_settings_once`
    （INSERT 文自身がマーカー有無を `WHERE NOT EXISTS` で確認する）が保証する。
    DB 到達不可などで例外になったら何も書かず、次回起動または `healthz()` の再試行に委ねる。
    DB の値と env が食い違えば、無視されたキー名を集約して1行だけ警告する。
    """
    from sherpa import agent_constructs, store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（env→system_settings）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_CREDENTIAL_SEED_MARKER_KEY) is not None:
        return  # 安価な早期 return（上書き防止自体はここに依存しない）
    try:
        candidate: dict[str, object] = {}
        env_name_of: dict[str, str] = {}  # sys_key -> env_name（不一致警告の逆引き用）
        for env_name, sys_key in _SEED_ENV_KEYS:
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            value = raw.strip()
            if not value:
                continue
            if sys_key == "openai_api_key" and not agent_constructs.is_real_api_key(value):
                continue  # .env.example のプレースホルダはシードしない
            candidate[sys_key] = value
            env_name_of[sys_key] = env_name
        raw_personal = os.environ.get("SHERPA_PERSONAL_API_KEYS")
        if raw_personal is not None:
            candidate["personal_api_keys_allowed"] = raw_personal.strip().lower() in ("1", "true", "yes", "on")
            env_name_of["personal_api_keys_allowed"] = "SHERPA_PERSONAL_API_KEYS"
        # Codex の web_search 管理者許可フラグ（env は初回シードのみ）
        raw_web_search = os.environ.get("SHERPA_ALLOW_WEB_SEARCH")
        if raw_web_search is not None:
            candidate["web_search_allowed"] = raw_web_search.strip().lower() in ("1", "true", "yes", "on")
            env_name_of["web_search_allowed"] = "SHERPA_ALLOW_WEB_SEARCH"
        candidate[_CREDENTIAL_SEED_MARKER_KEY] = _CREDENTIAL_SEED_VERSION
        applied, conflicts = store.seed_system_settings_once(
            candidate, guard_key=_CREDENTIAL_SEED_MARKER_KEY,
            secret_keys=_SEED_SECRET_KEYS & set(candidate))
        copied = sorted(k for k in applied if k != _CREDENTIAL_SEED_MARKER_KEY)
        if copied:
            log.info("起動時シード: env から system_settings へ取り込みました（%s）", ", ".join(copied))
        ignored_env_names = sorted(
            env_name_of[k] for k, cur in conflicts.items()
            if k in env_name_of and cur != candidate[k])
        if ignored_env_names:
            log.warning("起動時シード: env の %s は無視されます（管理画面の設定が有効です）",
                       "/".join(ignored_env_names))
    except Exception as e:
        log.warning("起動時シード（env→system_settings）に失敗しました（DB 不達の可能性）: %s", e)


# OLLAMA_URL の env→system_settings 初回シード。独立した完了マーカーを持つ（不正な間はこのマーカーだけ確定しない）
_OLLAMA_URL_SEED_MARKER_KEY = "ollama_url_seed_version"
_OLLAMA_URL_SEED_VERSION = 1


def _seed_ollama_url_from_env():
    """`OLLAMA_URL` を system_settings（`ollama_url`）へ一生に一度だけ取り込む。

    未設定なら正常としてマーカーだけ確定する。DB 到達不可・形式不正（userinfo/path/query 混入・不正 scheme 等）ならマーカーを立てず
    warning を残して return する（env を直せば次回起動で再評価）。
    正当な値は host:port へ正規化して保存し、非 loopback ホストは同一トランザクションで `ollama_allowlist` へも追記する
    （`ollama_url` を新規挿入できたときだけ・URL と認可を原子的に確定）。
    """
    from sherpa import llm, store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（OLLAMA_URL）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_OLLAMA_URL_SEED_MARKER_KEY) is not None:
        return
    raw = (os.environ.get("OLLAMA_URL") or "").strip()
    try:
        if not raw:
            store.seed_system_settings_once(
                {_OLLAMA_URL_SEED_MARKER_KEY: _OLLAMA_URL_SEED_VERSION},
                guard_key=_OLLAMA_URL_SEED_MARKER_KEY)
            return
        hp = llm._canonical_host_port(raw)
        if hp is None:
            log.warning("起動時シード: OLLAMA_URL の形式が不正なため無視します"
                       "（userinfo・path・query は指定できません・host:port のみ）。env を直せば"
                       "次回起動時に再評価されます。")
            return
        from urllib.parse import urlparse as _urlparse
        scheme = _urlparse(raw).scheme.lower()
        normalized = f"{scheme}://{llm.format_host_port(hp[0], hp[1])}"
        ollama_host_entry = None if llm.is_loopback_host(hp[0]) else llm.format_host_port(hp[0], hp[1])
        candidate = {"ollama_url": normalized, _OLLAMA_URL_SEED_MARKER_KEY: _OLLAMA_URL_SEED_VERSION}
        applied, _conflicts = store.seed_system_settings_once(
            candidate, guard_key=_OLLAMA_URL_SEED_MARKER_KEY,
            ollama_allowlist_merge=("ollama_url", ollama_host_entry) if ollama_host_entry else None)
        if "ollama_url" in applied:
            log.info("起動時シード: env から system_settings へ取り込みました（ollama_url）")
    except Exception as e:
        log.warning("起動時シード（OLLAMA_URL）に失敗しました（DB 不達の可能性）: %s", e)


def _warn_central_ollama_not_allowed():
    """現在の中央 `ollama_url`（非 loopback）が `ollama_allowlist` に無ければ警告ログを1行残す（自動修復はせず、管理画面での手動追加へ誘導する）。"""
    from sherpa import llm, store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時点検（ollama_allowlist）に失敗しました（DB 不達の可能性）: %s", e)
        return
    central = str(sysset.get("ollama_url") or "").strip()
    if not central:
        return
    hp = llm._canonical_host_port(central)
    if hp is None or llm.is_loopback_host(hp[0]):
        return
    entry = llm.format_host_port(hp[0], hp[1])
    if entry not in (sysset.get("ollama_allowlist") or []):
        log.warning(
            "Ollama 中央接続先(%s)が許可一覧にありません。管理画面で追加してください。", entry)


# OpenAI 接続先の env→system_settings 初回シード。独立した完了マーカーを持つ
_OPENAI_ENDPOINT_SEED_MARKER_KEY = "openai_endpoint_seed_version"
_OPENAI_ENDPOINT_SEED_VERSION = 1


def _openai_endpoint_seed_candidate() -> dict:
    """env からシード候補を組み立てる薄い委譲（I/O なし）。実体は `sherpa.llm.openai_endpoint_seed_candidate()`。"""
    from sherpa import llm
    return llm.openai_endpoint_seed_candidate()


def _seed_openai_endpoint_from_env():
    """OpenAI 互換 API の接続先（`OPENAI_BASE_URL`/`SHERPA_OPENAI_AUTH_HEADER`/`SHERPA_OPENAI_API_VERSION`/`SHERPA_OPENAI_ENDPOINT_KIND`）を
    system_settings へ一生に一度だけ取り込む（以後 env は読まない。`OPENAI_EMBED_MODEL` は `model_catalog.seed_catalog_once` が扱う）。

    候補が不正なら完了マーカーを立てず warning を残して return する（env を直せば再評価）。候補が空なら正常としてマーカーだけ確定する。
    DB 到達不可でもマーカーを立てず return する。候補が不正で確定できないときは `llm.set_openai_endpoint_seed_blocked()` で
    プロセス内フラグを立てて OpenAI 系 I/O を fail-closed にし、候補が確定した／DB 一時障害だった場合は以前のブロックを解除する。
    """
    from sherpa import llm, store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（openai_endpoint）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_OPENAI_ENDPOINT_SEED_MARKER_KEY) is not None:
        # マーカーが既にある＝確定済み。この worker のプロセス内フラグが blocked のままなら解除する
        if llm.openai_endpoint_seed_blocked_reason() is not None:
            log.info("起動時シード（openai_endpoint）: マーカー確定済みを検知し、このプロセスの"
                    "ブロックを解除します")
            llm.set_openai_endpoint_seed_blocked(None)
        return
    try:
        candidate = _openai_endpoint_seed_candidate()
    except ValueError as e:
        log.error(
            "起動時シード: OpenAI 接続先の env 設定が不正なため取り込みません。"
            "OpenAI 系の通信を停止します（env を修正して再起動してください）: %s", e)
        llm.set_openai_endpoint_seed_blocked(str(e))
        return
    try:
        candidate[_OPENAI_ENDPOINT_SEED_MARKER_KEY] = _OPENAI_ENDPOINT_SEED_VERSION
        applied, _conflicts = store.seed_system_settings_once(
            candidate, guard_key=_OPENAI_ENDPOINT_SEED_MARKER_KEY)
        copied = sorted(k for k in applied if k != _OPENAI_ENDPOINT_SEED_MARKER_KEY)
        if copied:
            log.info("起動時シード: OpenAI 接続先を system_settings へ取り込みました（%s）", ", ".join(copied))
        llm.set_openai_endpoint_seed_blocked(None)  # 以前のブロックが残っていれば解除
    except Exception as e:
        log.warning("起動時シード（openai_endpoint）に失敗しました（DB 不達の可能性）: %s", e)


def _env_int_in_range(name: str, lo: int, hi: int) -> int | None:
    """初回シード用の env 整数解析。未設定・非整数・範囲外は None（シードしない＝コード既定のまま）。"""
    raw = os.environ.get(name)
    if raw is None:
        return None
    try:
        v = int(raw.strip())
    except ValueError:
        return None
    return v if lo <= v <= hi else None


# 調べる深さの基準値6項目の env→system_settings 初回シード。独立した完了マーカーを持つ。値は env（有効な値のみ）→各モジュールのコード既定の順で決め、
# シード後は管理画面の基準値編集が唯一の真実源（実行時は env を読まない）。Codex 推論のみ env 由来の自由文字列のため語彙検証する
_DEPTH_PROFILE_SEED_MARKER_KEY = "depth_profile_seed_version"
_DEPTH_PROFILE_SEED_VERSION = 1


def _seed_depth_profile_from_env():
    """調べる深さの基準値6項目を system_settings へ一生に一度だけ取り込む。

    完了マーカー（`depth_profile_seed_version`）があれば env は読まない。DB 到達不可などで例外になったらマーカーを立てず return する。
    `SHERPA_CODEX_REASONING` は `strip().lower()` 正規化後に `depth_profile.CODEX_REASONING_LEVELS` に無ければエラーログのみ出し、
    6項目・マーカーとも書かず終了する（env 修正後の次回起動または `healthz()` の再試行で確定する）。
    """
    from sherpa import agentic_search, chat_service, depth_profile, impact_service, lens_service
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（depth_profile）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_DEPTH_PROFILE_SEED_MARKER_KEY) is not None:
        return
    reasoning_env = (os.environ.get("SHERPA_CODEX_REASONING") or depth_profile.CODEX_REASONING_DEFAULT).strip().lower()
    if reasoning_env not in depth_profile.CODEX_REASONING_LEVELS:
        log.error(
            "起動時シード（depth_profile）を見送りました: SHERPA_CODEX_REASONING の値が不正です"
            "（%r）。選べる値: %s。env を修正してください（基準値6項目は未シードのまま残り、"
            "修正後の再起動または healthz の再試行で一括シードされます）。",
            reasoning_env, ", ".join(depth_profile.CODEX_REASONING_LEVELS))
        return
    try:
        keys = depth_profile.BASE_SETTINGS_KEYS

        def _pick(env_name: str, lo: int, hi: int, default: int) -> int:
            v = _env_int_in_range(env_name, lo, hi)
            return default if v is None else v

        candidate = {
            keys["grep_max_hits"]: _pick("SHERPA_GREP_MAX_HITS", 1, agentic_search.MAX_HITS_ABS_MAX,
                                         agentic_search.MAX_HITS),
            keys["qa_max_hits"]: chat_service.QA_MAX_HITS_DEFAULT,
            keys["read_window"]: _pick("SHERPA_READ_WINDOW", 10, agentic_search.READ_WINDOW_ABS_MAX,
                                       agentic_search.READ_WINDOW),
            keys["impact_depth"]: _pick("SHERPA_IMPACT_MAX_DEPTH", 1, impact_service.IMPACT_MAX_DEPTH_ABS_MAX,
                                        impact_service.IMPACT_MAX_DEPTH),
            keys["troubleshoot_depth"]: _pick("SHERPA_TROUBLESHOOT_GRAPH_DEPTH", 1,
                                              lens_service.TROUBLESHOOT_GRAPH_DEPTH_ABS_MAX,
                                              lens_service.TROUBLESHOOT_GRAPH_DEPTH),
            keys["codex_reasoning"]: reasoning_env,
            _DEPTH_PROFILE_SEED_MARKER_KEY: _DEPTH_PROFILE_SEED_VERSION,
        }
        applied, _conflicts = store.seed_system_settings_once(
            candidate, guard_key=_DEPTH_PROFILE_SEED_MARKER_KEY)
        copied = sorted(k for k in applied if k != _DEPTH_PROFILE_SEED_MARKER_KEY)
        if copied:
            log.info("起動時シード: 調べる深さの基準値を system_settings へ取り込みました（%s）", ", ".join(copied))
    except Exception as e:
        log.warning("起動時シード（depth_profile）に失敗しました（DB 不達の可能性）: %s", e)


# 画面で変えられる運用設定（同時実行の上限・取り込みの読み取り方式・旧形式の変換・個人ファイルの上限と保持日数）の env→system_settings 初回シード。
# 独立した完了マーカーを持つ。env が設定され有効なものだけを取り込み（未設定・不正な項目は取り込まない＝コード既定）、
# シード後は管理画面が唯一の真実源（実行時は env を読まない）。
_SCREEN_SETTINGS_SEED_MARKER_KEY = "screen_settings_seed_version"
_SCREEN_SETTINGS_SEED_VERSION = 1


def _seed_screen_settings_from_env():
    """画面で変えられる運用設定のうち env に値があるものを system_settings へ一生に一度だけ取り込む（既存の保存値は上書きしない）。

    対象: `SHERPA_CHAT_MAX_TURNS_PER_USER`/`_GLOBAL`・`SHERPA_ARMS`（既知のアーム名のみ）・`SHERPA_LEGACY_BACKEND`（既知の値のみ）・
    `SHERPA_WORKSPACE_MAX_BYTES`・`SHERPA_WORKSPACE_TTL_DAYS`。完了マーカーがあれば env は読まない。DB 不達ならマーカーを立てず return する。
    """
    from sherpa import workspace_limits
    from sherpa.ingest import arms as ingest_arms
    from sherpa.ingest.arms import legacy_convert
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（screen_settings）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_SCREEN_SETTINGS_SEED_MARKER_KEY) is not None:
        return
    try:
        candidate: dict = {}
        for env_name, key, lo, hi in (
                ("SHERPA_CHAT_MAX_TURNS_PER_USER", "chat_max_turns_per_user", 1, 16),
                ("SHERPA_CHAT_MAX_TURNS_GLOBAL", "chat_max_turns_global", 1, 64),
                ("SHERPA_WORKSPACE_MAX_BYTES", workspace_limits.MAX_BYTES_KEY,
                 workspace_limits.MAX_BYTES_MIN, workspace_limits.MAX_BYTES_MAX),
                ("SHERPA_WORKSPACE_TTL_DAYS", workspace_limits.TTL_DAYS_KEY,
                 workspace_limits.TTL_DAYS_MIN, workspace_limits.TTL_DAYS_MAX)):
            v = _env_int_in_range(env_name, lo, hi)
            if v is not None:
                candidate[key] = v
        raw_arms = os.environ.get("SHERPA_ARMS")
        if raw_arms is not None:
            known = set(ingest_arms.known_arm_names())
            names = list(dict.fromkeys(n.strip() for n in raw_arms.split(",") if n.strip() in known))
            if names:
                candidate["arms_enabled"] = names
        raw_backend = (os.environ.get("SHERPA_LEGACY_BACKEND") or "").strip()
        if raw_backend in legacy_convert.KNOWN_BACKENDS:
            candidate["legacy_backend"] = raw_backend
        candidate[_SCREEN_SETTINGS_SEED_MARKER_KEY] = _SCREEN_SETTINGS_SEED_VERSION
        applied, _conflicts = store.seed_system_settings_once(
            candidate, guard_key=_SCREEN_SETTINGS_SEED_MARKER_KEY)
        copied = sorted(k for k in applied if k != _SCREEN_SETTINGS_SEED_MARKER_KEY)
        if copied:
            log.info("起動時シード: 運用設定を system_settings へ取り込みました（%s）", ", ".join(copied))
    except Exception as e:
        log.warning("起動時シード（screen_settings）に失敗しました（DB 不達の可能性）: %s", e)


# 個人設定の頭脳（agent）の env→user_settings 初回シード。独立した完了マーカーを持つ。
_USER_AGENT_SEED_MARKER_KEY = "user_agent_seed_version"
_USER_AGENT_SEED_VERSION = 1


def _seed_user_agent_from_env():
    """`SHERPA_AGENT` があれば、頭脳が未選択の既存利用者全員へその値を一生に一度だけ保存する（選択済みの利用者・新規利用者は変えない）。

    旧 openai/ollama は簡易（simple）へ読み替え、標準の頭脳（codex/simple）以外（閉じた頭脳・不正値）は保存しない。完了マーカーがあれば env は読まない。env が無い・保存できない値のときは何もしない（マーカーも立てない）。DB 不達ならマーカーを立てず return する。
    """
    from sherpa import agent_constructs
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（個人設定の頭脳）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_USER_AGENT_SEED_MARKER_KEY) is not None:
        return
    raw = (os.environ.get("SHERPA_AGENT") or "").strip().lower()
    try:
        if raw in agent_constructs.STANDARD_AGENTS:
            n = store.seed_user_agent_once(raw, _USER_AGENT_SEED_MARKER_KEY, _USER_AGENT_SEED_VERSION)
            if n:
                log.info("起動時シード: 頭脳が未選択の利用者 %d 人へ SHERPA_AGENT の値を保存しました", n)
        elif raw:
            log.warning("起動時シード: SHERPA_AGENT=%r は保存できる頭脳ではないため無視します", raw)
    except Exception as e:
        log.warning("起動時シード（個人設定の頭脳）に失敗しました（DB 不達の可能性）: %s", e)


# 旧 `SHERPA_VLM_OLLAMA_URL` の env→system_settings（中央の `ollama_url`）初回シード。独立した完了マーカーを持つ。
_VLM_OLLAMA_URL_SEED_MARKER_KEY = "vlm_ollama_url_seed_version"
_VLM_OLLAMA_URL_SEED_VERSION = 1


def _seed_vlm_ollama_url_from_env():
    """旧 `SHERPA_VLM_OLLAMA_URL` を、中央の `ollama_url` が未保存のときだけ中央の値として一生に一度だけ保存する（非 loopback は許可一覧へも追記）。
    中央の値が既にあり旧値と違うときは警告を 1 行だけ残す（自動では変えない）。`_seed_ollama_url_from_env` の後に呼ぶ。env が無い・形式不正・DB 不達のときはマーカーを立てず return する。
    """
    from sherpa import llm
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（VLM の Ollama 接続先）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_VLM_OLLAMA_URL_SEED_MARKER_KEY) is not None:
        return
    raw = (os.environ.get("SHERPA_VLM_OLLAMA_URL") or "").strip()
    marker = {_VLM_OLLAMA_URL_SEED_MARKER_KEY: _VLM_OLLAMA_URL_SEED_VERSION}
    try:
        if not raw:
            return
        hp = llm._canonical_host_port(raw)
        if hp is None:
            log.warning("起動時シード: SHERPA_VLM_OLLAMA_URL の形式が不正なため無視します（host:port のみ）。")
            return
        central = str(sysset.get("ollama_url") or "").strip()
        if central:
            if llm.ollama_url_fingerprint(central) != llm.format_host_port(hp[0], hp[1]):
                log.warning("SHERPA_VLM_OLLAMA_URL は中央の Ollama 接続先と異なりますが、画像の読み取りは中央の接続先を使います。"
                            "必要なら管理画面で中央の接続先を直してください。")
            store.seed_system_settings_once(marker, guard_key=_VLM_OLLAMA_URL_SEED_MARKER_KEY)
            return
        from urllib.parse import urlparse as _urlparse
        normalized = f"{_urlparse(raw).scheme.lower()}://{llm.format_host_port(hp[0], hp[1])}"
        entry = None if llm.is_loopback_host(hp[0]) else llm.format_host_port(hp[0], hp[1])
        applied, _conflicts = store.seed_system_settings_once(
            {"ollama_url": normalized, **marker}, guard_key=_VLM_OLLAMA_URL_SEED_MARKER_KEY,
            ollama_allowlist_merge=("ollama_url", entry) if entry else None)
        if "ollama_url" in applied:
            log.info("起動時シード: SHERPA_VLM_OLLAMA_URL を中央の Ollama 接続先へ取り込みました（ollama_url）")
    except Exception as e:
        log.warning("起動時シード（VLM の Ollama 接続先）に失敗しました（DB 不達の可能性）: %s", e)


def _purge_personal_keys_if_disabled_on_startup():
    """A6（個人 API キー原則）が偽なら、起動のたびに全ユーザーの個人秘密キーを一括削除する。

    `_seed_settings_from_env()` の後に呼ぶ。冪等（削除対象が無ければログしない）。
    """
    from sherpa import keys, store
    log = logging.getLogger("sherpa")
    try:
        if keys.personal_keys_allowed():
            return
        count = store.purge_personal_api_keys(actor="system")
        if count:
            log.info("起動時: personal_api_keys_allowed=false のため個人 API キーを一括削除しました（%d 件）", count)
    except Exception as e:
        log.warning("起動時の個人キー一括削除に失敗しました（DB 不達の可能性）: %s", e)


def _warn_multi_worker_chat_turns():
    """チャットターンのバックグラウンド実行はプロセス内レジストリ（`sherpa.chat_turns`）のため uvicorn workers=1 前提。

    workers>1 だと別 worker がレジストリを共有せず 404 や取りこぼしが起きる（`ratelimit` の状態も同様）。
    production では起動拒否、非 production は大きく警告するだけ。
    """
    import logging
    try:
        workers = int(os.environ.get("SHERPA_UVICORN_WORKERS", "1") or "1")
    except ValueError:
        return
    if workers <= 1:
        return
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    is_prod = env in ("prod", "production")
    log = logging.getLogger("sherpa")
    banner = (
        "\n" + "!" * 72 + "\n"
        "!! SHERPA_UVICORN_WORKERS=%s（複数 worker）が設定されています。\n"
        "!! チャットターンのバックグラウンド実行（背景実行・覗き窓方式）はプロセス内レジストリのため\n"
        "!! 複数 worker 構成は非対応です（ターンの購読/停止が別 worker に届くと 404 になり得ます）。\n"
        "!! workers=1 での運用を推奨します（将来 Redis 等の共有レジストリ導入まで）。\n"
        + "!" * 72)
    if is_prod:
        log.error(banner, workers)
        raise RuntimeError(
            "設定ミス: SHERPA_ENV=%s（本番）で SHERPA_UVICORN_WORKERS=%s（複数 worker）です。"
            "チャットターンのバックグラウンド実行・ratelimit はプロセス内レジストリのため複数 worker "
            "構成は非対応です。起動を拒否します（workers=1 で運用してください）。"
            % (env, workers))
    log.warning(banner, workers)


def _warn_browse_roots_missing():
    """フォルダ選択の許可ルート（既定 `/mnt:/srv:/home:/Users`）が1つも存在しなければ起動時に警告する（fail-closed にはしない）。"""
    import logging
    roots = _browse_roots()

    def _is_dir_safe(r: Path) -> bool:
        # 判定不能（権限エラー・壊れたマウント等の OSError）は「存在しない」扱いにする
        try:
            return r.is_dir()
        except Exception:
            return False

    if any(_is_dir_safe(r) for r in roots):
        return
    log = logging.getLogger("sherpa")
    log.warning(
        "\n" + "!" * 72 + "\n"
        "!! フォルダ選択のルート（%s）が存在しません。\n"
        "!! Linux サーバ等（WSL の /mnt 自動マウント前提が成り立たない環境）や macOS では\n"
        "!! SHERPA_BROWSE_ROOTS 環境変数で資料フォルダの親ディレクトリを指定してください\n"
        "!!   例（Linux サーバ）: SHERPA_BROWSE_ROOTS=/srv/sherpa-data\n"
        "!!   例（macOS）:       SHERPA_BROWSE_ROOTS=/Users\n"
        + "!" * 72,
        ":".join(str(r) for r in roots))


def _reconcile_orphans():
    """起動時に孤児派生物（ES/Neo4j/派生MD）を別スレッドで自動掃除する（best-effort・起動を止めない）。"""
    import threading

    def _run():
        try:
            from sherpa import reconcile
            reconcile.reconcile_derivatives()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="sherpa-reconcile").start()


def _sweep_expired_workspace() -> dict:
    """期限切れ個人 workspace ファイルを掃除する（TTL 自動掃除）。

    - DB 取得失敗なら一切削除しない。
    - 物理ファイルは `files_dir` 配下に resolve/relative_to で閉じ込め確認後のみ削除する。symlink は削除しない。
    - 無効化ユーザーの行は `expired_workspace_files()` が除外済み。物理削除失敗は best-effort（台帳 status='expired' は記録する）。
    - 共有 RAG（ES/Neo4j）には触れない。
    """
    try:
        rows = store.expired_workspace_files()  # DB 不可なら例外 → 呼出元が握る
    except Exception as e:
        _log.warning("sweep_expired: DB unreachable, skipping sweep: %s", e)
        return {"skipped": "db_unreachable"}

    deleted, failed = 0, 0
    for row in rows:
        fid = row["id"]
        uid = row["user_id"]
        rel_path = row["rel_path"]
        original_path = row["original_path"]

        # 台帳の条件付き UPDATE（claim）を先に実行し、成功した場合のみ物理削除する（再アップロード/無効化との競合を防ぐ）
        try:
            claimed = store.claim_workspace_file_expired(fid)
        except Exception as e:
            _log.warning("sweep_expired: ledger claim failed uid=%s fid=%s: %s", uid, fid, e)
            failed += 1
            continue  # DB 障害 = 物理削除しない

        if claimed is None:
            # 条件不成立（再アップロード・無効化等）= skip
            _log.debug("sweep_expired: claim not matched uid=%s fid=%s (already handled or disabled)",
                       uid, fid)
            continue

        # claim 成功 → advisory lock（`workspace_file_lock`・upload と sweep を (uid, rel_path) 単位で直列化）を取得してから物理削除する
        try:
            with store.workspace_file_lock(uid, rel_path):
                # lock 内で DB を再確認（lock 取得前に re-upload が完了していれば skip）
                if not store.no_live_upload_for_path(uid, rel_path):
                    _log.warning(
                        "sweep_expired: re-upload detected under lock, skipping uid=%s rel=%s",
                        uid, rel_path)
                    failed += 1
                    continue

                p = Path(original_path)
                files_dir = _USERS_DIR.resolve() / uid / "workspace" / "files"
                # symlink 脱出防止: resolve 前の raw パスで `is_symlink()` を確認する
                if p.is_symlink():
                    _log.warning("sweep_expired: symlink rejected uid=%s rel=%s", uid, rel_path)
                    failed += 1
                    continue
                # resolve して files_dir 配下に収まることを確認してから削除する
                try:
                    p.resolve().relative_to(files_dir.resolve())
                except ValueError:
                    _log.warning("sweep_expired: path outside files_dir, skipping uid=%s rel=%s",
                                 uid, rel_path)
                    failed += 1
                    continue
                p.unlink(missing_ok=True)
        except Exception as e:
            _log.warning("sweep_expired: physical delete failed uid=%s fid=%s: %s", uid, fid, e)
            failed += 1
            continue
        deleted += 1

    if deleted or failed:
        _log.info("sweep_expired: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


def _gc_orphan_workspace_files() -> dict:
    """台帳に無い物理ファイル（孤児）を best-effort で掃除する。

    - DB 不可 / user 取得不可なら触らない。無効化ユーザーの領域は保持する。
    - `files_dir` 配下のみ・symlink は触らない・advisory lock で upload と直列化する。
    - 台帳の live rel_path 集合に無い物理ファイルのみ削除する（lock 内で `no_live_upload_for_path` を再確認する）。
    - 共有 RAG（ES/Neo4j）には触れない。
    """
    base = _USERS_DIR.resolve()
    if not base.is_dir():
        return {"skipped": "no_users_dir"}
    deleted, failed = 0, 0
    for udir in base.iterdir():
        if udir.is_symlink() or not udir.is_dir():
            continue
        uid = udir.name
        # 親コンポーネント（workspace/・files/）の symlink を拒否し、files_dir の実体が udir 配下に収まることを確認してから触る
        ws_dir = udir / "workspace"
        files_dir = ws_dir / "files"
        if ws_dir.is_symlink() or files_dir.is_symlink() or not files_dir.is_dir():
            continue
        try:
            files_dir.resolve().relative_to(udir.resolve())
        except ValueError:
            _log.warning("gc_orphan: files_dir escapes user dir, skipping uid=%s", uid)
            continue
        try:
            u = store.get_user(uid)  # DB 不可 → 例外 → この uid は触らない
        except Exception as e:
            _log.warning("gc_orphan: user lookup failed uid=%s: %s", uid, e)
            continue
        # user 不明（None）は削除しない。disabled も保持
        if u is None or u.get("status") == "disabled":
            continue
        try:
            live = store.live_workspace_rel_paths(uid)  # DB 不可 → 例外 → 触らない
        except Exception as e:
            _log.warning("gc_orphan: live rel paths failed uid=%s: %s", uid, e)
            continue
        for p in sorted(files_dir.iterdir()):  # files/ 直下（rel_path = ファイル名）
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                rel = p.name
                if rel in live:
                    continue  # 台帳に生きている = 正規ファイル（保持）
                with store.workspace_file_lock(uid, rel):
                    if not store.no_live_upload_for_path(uid, rel):
                        continue  # lock 中に upload 検出 = 保持
                    # lock 内で user 状態を再確認（scan 後に disabled 化されていれば保持）
                    try:
                        u2 = store.get_user(uid)
                    except Exception:
                        continue  # DB 不可 = 触らない
                    if u2 is None or u2.get("status") == "disabled":
                        continue
                    try:
                        p.resolve().relative_to(files_dir.resolve())  # base-confined 再確認
                    except ValueError:
                        _log.warning("gc_orphan: outside files_dir uid=%s rel=%s", uid, rel)
                        failed += 1
                        continue
                    p.unlink(missing_ok=True)
                    deleted += 1
            except Exception as e:
                _log.warning("gc_orphan: failed uid=%s file=%s: %s", uid, p.name, e)
                failed += 1
    if deleted or failed:
        _log.info("gc_orphan: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


def _sweep_expired_codex_sessions() -> dict:
    """会話ごとの Codex resume セッション（`workspace/.codex-sessions/{cid}`）の TTL 掃除。

    保持日数は admin 設定 `codex_session_retention_days`（未設定は既定30日・`system_extras.effective_codex_session_retention_days` が判定。
    0 を明示したときはスイープしない）。判定はディレクトリ自体の mtime。
    - system_settings 取得不可・users_dir 不在なら削除しない。symlink は触らない。
    - 削除は `.codex-sessions/{cid}` 配下に閉じ込め確認（relative_to）してから行う。
    - 共有 RAG（ES/Neo4j）・conversations 行には触れない。
    子エージェントの rollout も同じディレクトリ木のため `shutil.rmtree(cdir)` が一括で消す。
    """
    try:
        retention_days = system_extras.effective_codex_session_retention_days(store.get_system_settings())
    except Exception as e:
        _log.warning("sweep_expired_codex_sessions: system_settings 取得失敗、skip: %s", e)
        return {"skipped": "settings_unreachable"}
    if retention_days <= 0:
        return {"skipped": "unlimited"}
    base = _USERS_DIR.resolve()
    if not base.is_dir():
        return {"skipped": "no_users_dir"}
    cutoff = time.time() - retention_days * 86400
    deleted, failed = 0, 0
    for udir in base.iterdir():
        if udir.is_symlink() or not udir.is_dir():
            continue
        sessions_root = udir / "workspace" / ".codex-sessions"
        if sessions_root.is_symlink() or not sessions_root.is_dir():
            continue
        try:
            sessions_root_resolved = sessions_root.resolve()
            sessions_root_resolved.relative_to(udir.resolve())
        except (OSError, ValueError):
            _log.warning("sweep_expired_codex_sessions: sessions dir escapes user dir, skipping uid=%s", udir.name)
            continue
        for cdir in sessions_root.iterdir():
            try:
                if cdir.is_symlink() or not cdir.is_dir():
                    continue
                if cdir.stat().st_mtime > cutoff:
                    continue  # 保持期間内＝まだ resume 対象として残す
                _remove_codex_session_dir(cdir, sessions_root)  # base-confined 再確認 + rmtree
                deleted += 1
            except Exception as e:
                _log.warning("sweep_expired_codex_sessions: failed uid=%s cid=%s: %s", udir.name, cdir.name, e)
                failed += 1
    if deleted or failed:
        _log.info("sweep_expired_codex_sessions: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


def _sweep_expired_announcements() -> dict:
    """掲載終了日時（expire_at）を過ぎたお知らせを物理削除する（掲示板の公開/削除タイマー）。

    削除は `store.delete_expired_announcements()`（DELETE 文自体に条件を持たせ、削除の瞬間に各行の最新状態を再評価する）に一本化する。
    監査は fail-open（背景ポーラの自動処理のため、書けなくても削除は成功のまま）。
    """
    try:
        rows = store.delete_expired_announcements()  # DB 不可なら例外 → 呼出元が握って skip
    except Exception as e:
        _log.warning("sweep_expired_announcements: DB unreachable, skipping: %s", e)
        return {"skipped": "db_unreachable"}
    deleted = 0
    for row in rows:
        deleted += 1
        try:
            store.audit("system:sweep", "announcement.expired_deleted", "announcement",
                        f"announcement:{row['id']}",
                        before_state={"title": row["title"], "category": row["category"],
                                     # 削除の根拠（掲載期間）を監査に残す
                                     "publish_at": row["publish_at"].isoformat() if row.get("publish_at") else None,
                                     "expire_at": row["expire_at"].isoformat() if row.get("expire_at") else None},
                        outcome="success", severity="info")
        except Exception as e:
            _log.warning("sweep_expired_announcements: audit write failed id=%s (fail-open, "
                         "delete kept): %s", row["id"], e)
    if deleted:
        _log.info("sweep_expired_announcements: deleted=%d", deleted)
    return {"deleted": deleted}


def _run_workspace_maintenance() -> None:
    """期限切れ TTL 掃除・孤児 GC・掲示板タイマーの自動削除・Codex resume セッションの保持期限掃除を順に best-effort 実行する（起動時と定期ループ共通）。"""
    try:
        _sweep_expired_workspace()
    except Exception as e:
        _log.warning("workspace maintenance (sweep) error: %s", e)
    try:
        _gc_orphan_workspace_files()
    except Exception as e:
        _log.warning("workspace maintenance (gc) error: %s", e)
    try:
        _sweep_expired_announcements()
    except Exception as e:
        _log.warning("workspace maintenance (announcements sweep) error: %s", e)
    try:
        _sweep_expired_codex_sessions()
    except Exception as e:
        _log.warning("workspace maintenance (codex session sweep) error: %s", e)


def _sweep_expired_on_startup(stop=None):
    """起動時に期限切れ workspace ファイル掃除＋孤児 GC を別スレッドで実行する（best-effort）。lifespan が起動時に呼ぶ。

    `stop`（threading.Event）が立っていれば実行せず終了する。起動したスレッドを返し、lifespan が終了時（PG プールを閉じる前）に join する。
    """
    import threading

    def _run():
        if stop is not None and stop.is_set():
            return
        try:
            _run_workspace_maintenance()
        except Exception:
            pass  # startup を止めない

    t = threading.Thread(target=_run, daemon=True, name="sherpa-ws-ttl")
    t.start()
    return t


# 個人ファイルの期限切れ掃除・孤児 GC などを繰り返す間隔（秒）。
WORKSPACE_MAINTENANCE_INTERVAL_SEC = 3600.0


def _start_workspace_maintenance_loop(stop):
    """個人ファイルの期限切れ掃除・孤児 GC・掲示板の自動削除・Codex セッションの保持期限掃除を `WORKSPACE_MAINTENANCE_INTERVAL_SEC` ごとに繰り返す常駐スレッドを起動する（best-effort・`stop`（threading.Event）が立つと終了）。起動時の 1 回は `_sweep_expired_on_startup` が行う。起動したスレッドを返す。"""
    import threading

    def _loop():
        while not stop.wait(WORKSPACE_MAINTENANCE_INTERVAL_SEC):
            try:
                _run_workspace_maintenance()
            except Exception as e:
                _log.warning("workspace maintenance loop error: %s", e)

    t = threading.Thread(target=_loop, daemon=True, name="sherpa-ws-maintenance")
    t.start()
    return t


def _backfill_turn_metrics_on_startup():
    """起動時に `turn_metrics` の行が無い回答だけを別スレッドで補完する（best-effort・冪等・起動を止めない）。"""
    import threading

    def _run():
        try:
            from sherpa.store.turn_metrics import backfill_missing
            r = backfill_missing()
            _log.info("turn_metrics 起動時補完: written=%s failed=%s", r["written"], r["failed"])
        except Exception as e:
            _log.warning("turn_metrics 起動時補完に失敗しました（起動は続行・次回起動で再試行）: %s", e)

    threading.Thread(target=_run, daemon=True, name="sherpa-turn-metrics-backfill").start()



# ===== 画面: web/ を /ui で配信し、/ は /ui へ =====
_WEB = Path(__file__).resolve().parents[1] / "web"


class _CachedStaticFiles(StaticFiles):
    """`/ui` 配信に Cache-Control を付与する。html/css/js は毎回サーバへ再検証させ（`no-cache`）、フォント（woff2）は長期キャッシュ可。"""
    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable"
                                         if str(full_path).endswith(".woff2") else "no-cache")
        return resp


class _ManualSrcStaticFiles(_CachedStaticFiles):
    """docs/manual/*.md と manifest.json だけを読み取り専用配信する。

    トップレベルの拡張子 .md（README.md を除く）または manifest.json のみ許可し、それ以外は 404（サブパスも `/` を含む時点で弾く）。
    """
    _ALLOWED_JSON = {"manifest.json"}

    def _allowed(self, path: str) -> bool:
        rel = path.strip("/")
        if not rel or "/" in rel:
            return False
        if rel in self._ALLOWED_JSON:
            return True
        return rel.lower().endswith(".md") and rel.lower() != "readme.md"

    async def get_response(self, path, scope):
        if not self._allowed(path):
            raise HTTPException(status_code=404)
        return await super().get_response(path, scope)


app.include_router(system.root_router)


if _WEB.is_dir():
    # 使い方（manual.html）が参照する画面キャプチャを読み取り専用で配信する（実体は web/ の外の docs/manual/images/）。`/ui` の総取りより先に登録する
    _MANUAL_IMAGES = Path(__file__).resolve().parents[1] / "docs" / "manual" / "images"
    if _MANUAL_IMAGES.is_dir():
        app.mount("/ui/manual-images",
                  _CachedStaticFiles(directory=str(_MANUAL_IMAGES)), name="manual-images")
    # 正本 docs/manual/*.md を manual.js がレンダリングするための読み取り専用配信（_ManualSrcStaticFiles）
    _MANUAL_SRC = Path(__file__).resolve().parents[1] / "docs" / "manual"
    if _MANUAL_SRC.is_dir():
        app.mount("/ui/manual-src",
                  _ManualSrcStaticFiles(directory=str(_MANUAL_SRC)), name="manual-src")
    app.mount("/ui", _CachedStaticFiles(directory=str(_WEB), html=True), name="ui")
