"""FastAPI: MVP の薄い入口（鏡モデル・MIRROR-MODEL §3/§8）。

POST /impact/run {start, world, scope_paths} → analysis_id ／ GET /impact/{id} → 影響一覧。
API パラメータは **world（登録ディレクトリ）のみ**。旧 `version` の互換受理は語彙統一フェーズ2・
第2段（2026-07-13）で終了済み（未宣言パラメータとして黙って無視される）。版ライフサイクル/
auto-scope 確定フローは撤去済（即反映ライブ鏡・範囲＝フォルダ）。結果は in-memory（単一 worker 前提）。
"""
from __future__ import annotations

import logging
import os
import shutil
import threading
import time
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request
from fastapi.openapi.docs import (
    get_swagger_ui_html,
    get_swagger_ui_oauth2_redirect_html,
    swagger_ui_default_parameters,
)
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles

from sherpa import auth, ext_api, store, worlds
# scope_mod は api.py 自身の残留ハンドラからは呼ばれなくなったが、tests/api/test_world_param_compat.py
# の `monkeypatch.setattr(api.scope_mod, "scope_tree", ...)`（モジュール属性 patch・impact router と
# 同一オブジェクト共有）が参照するため import を維持する（スライス6）。
from sherpa import scope as scope_mod
from sherpa.deps import (
    _DEFAULT_WORLD,
    _USERS_DIR,
    _browse_roots,
    _ensure_initial_admin,
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
# system.py 内の module-private シンボルへの直接参照（`api._bedrock_model_id_valid` 等）を持つ
# 既存テスト（tests/api/test_bedrock_settings.py）互換のため、フラット名前空間に再エクスポートする。
from sherpa.routers.system import (  # noqa: F401
    _BEDROCK_MODELS_CACHE,
    _BEDROCK_MODELS_CACHE_GEN,
    _BEDROCK_MODELS_CACHE_LOCK,
    _bedrock_key_fingerprint,
    _bedrock_model_id_valid,
    _bedrock_verify_last_call,
)
# shares/workspace router の一部シンボルは既存テストが `sherpa.api` 属性参照
# （inspect.getsource 等）で取るため、フラット名前空間に再エクスポートする（スライス3）。
from sherpa.routers.shares import conversation_share_create  # noqa: F401
from sherpa.routers.workspace import (  # noqa: F401
    _WORKSPACE_ALLOWED_EXT,
    _WORKSPACE_SEARCHABLE_EXT,
    _safe_workspace_filename,
    workspace_file_download,
    workspace_search,
)
# _analyses/_analyses_lock/_seq/_ANALYSES_TTL_SECONDS は sherpa/routers/impact.py へ移動済み
# （tests/api/test_impact_idor.py の api._analyses[...] / api._ANALYSES_TTL_SECONDS 参照は
# 同一オブジェクトの属性参照として互換のまま動く・純移動＋再エクスポート）。
from sherpa.routers.impact import _ANALYSES_TTL_SECONDS, _analyses, _analyses_lock, _seq  # noqa: F401
# worlds router の一部シンボルは既存テストが `sherpa.api` 属性参照で取るため、
# フラット名前空間に再エクスポートする（スライス6）。
from sherpa.routers.worlds import _ingest_summary, _subdirs  # noqa: F401
# chat router の一部シンボルは既存テストが `sherpa.api` 属性参照（item 代入/pop・直接呼び出し）で
# 取るため、フラット名前空間に再エクスポートする（スライス7）。ChatReq は tests/api/test_scope_mc.py、
# _STREAM_STOP_EVENTS/_STREAM_STOP_LOCK は tests/api/test_chat_m8.py（同一 dict/Lock オブジェクトの
# item 操作のみ）、_persist_turn_crash は tests/api/test_chat_turns.py（関数オブジェクトの直接呼び出し）。
from sherpa.routers.chat import (  # noqa: F401
    ChatReq,
    _STREAM_STOP_EVENTS,
    _STREAM_STOP_LOCK,
    _persist_turn_crash,
)
from sherpa.ingest import worker as ingest_worker
from sherpa.lifespan import lifespan

_log = logging.getLogger("sherpa")

# _DEFAULT_WORLD は sherpa.deps 定義（ここでは再エクスポートを import 済み）。_WORLD_PATTERN / _WorldField は
# sherpa.deps 定義だが api.py 残留ハンドラからは呼ばれなくなった（スライス7）ため再エクスポートしない。
# _XLSX は sherpa/routers/impact.py へ移動済み（impact_export 専用）。
# セッション Cookie 名・セッション有効期間（_SESSION_DAYS）は sherpa/routers/auth.py へ移動済み（スライス8）。
# 個人 workspace のルート（sherpa.deps 定義・ここでは再エクスポートを import 済み）。
# _WORKSPACE_MAX_BYTES / _WORKSPACE_TTL_DAYS / _WORKSPACE_ALLOWED_EXT / _SAFE_FILENAME_CHARS は
# sherpa/routers/workspace.py へ移動済み（_WORKSPACE_ALLOWED_EXT / _WORKSPACE_SEARCHABLE_EXT は
# 上記で再エクスポート・`is` 同一性を維持）。

_TAGS_METADATA = [
    {"name": "認証", "description": "ログイン・ログアウト・現在ユーザー取得。"},
    {"name": "管理者:ユーザー管理", "description": "ユーザーの作成・一覧・無効化・role変更・パスワード再設定（管理者のみ）。"},
    {"name": "管理者:監査ログ", "description": "監査ログの閲覧とhash-chain整合性検証（管理者のみ）。"},
    {"name": "管理者:利用統計", "description": "会話・メッセージから集計する利用統計（ヒアリング候補の発見・管理者のみ）。"},
    {"name": "運営掲示板", "description": "トップ画面のお知らせ（メンテナンス・活用事例・お知らせ）の閲覧・投稿・編集・削除。"},
    {"name": "会話共有", "description": "会話の共有リンク発行・受領・取消。"},
    {"name": "個人ワークスペース", "description": "個人ファイルのアップロード・一覧・削除・grep検索（共有KBには索引化しない）。"},
    {"name": "影響分析", "description": "ナレッジグラフを起点とした影響範囲分析の実行・取得・Excel出力。"},
    {"name": "範囲", "description": "検索・分析対象をフォルダ単位で絞り込むスコープツリー。"},
    {"name": "チャット", "description": "チャット（同期・SSEストリーミング）。"},
    {"name": "設定", "description": "AIプロバイダ・モデル・system_prompt等の設定取得/更新/接続テスト。"},
    {"name": "会話管理", "description": "会話履歴の一覧・取得・削除・ピン留め・改名。"},
    {"name": "トラブルシュート・QA", "description": "症状からの原因調査、仕様問い合わせ（QA）レンズ。"},
    {"name": "文書", "description": "文書台帳の参照と原本ダウンロード。"},
    {"name": "資料フォルダ(World)管理", "description": "登録ディレクトリ（world）の登録・状態確認・差分・再取込・削除、フォルダ選択、業務語対応の下案/承認/無効化。"},
    {"name": "ナレッジグラフ", "description": "ナレッジグラフの可視化データ・検索・自然言語質問。"},
    {"name": "システム", "description": "ヘルスチェック・ルート・静的UI配信。"},
    {"name": "管理者:外部APIキー", "description": "外部連携 API（/ext/v1）のキー発行・一覧・失効（管理者のみ）。"},
]

app = FastAPI(
    title="Sherpa MVP",
    description="社内文書向け Agentic RAG 基盤。取り込み→抽出→ナレッジグラフ→影響分析／チャット／トラブルシュートまでを提供するAPI。",
    openapi_tags=_TAGS_METADATA,
    lifespan=lifespan,
    # /docs（Swagger UI）・/docs/oauth2-redirect は web/vendor/ 同梱資産だけで配信する自前ルート
    # （外部ネットワーク参照ゼロ・下記2関数）。ルート表の定義順（tests/api/test_route_snapshot.py の
    # golden）を保つため、既定ルートと同じ位置（FastAPI() 直後）に置く。ReDoc（/redoc）は提供しない
    # （利用者向け契約は Swagger UI のみ・404）。
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
        # validatorUrl は外部（validator.swagger.io）への問い合わせ先＝閉域LANでは到達できない。
        swagger_ui_parameters={**swagger_ui_default_parameters, "validatorUrl": None},
    )


@app.get(app.swagger_ui_oauth2_redirect_url, include_in_schema=False)
async def swagger_ui_redirect() -> HTMLResponse:
    return get_swagger_ui_oauth2_redirect_html()


# 外部連携 API（/ext/v1・APIキー認証系）。ext_api は sherpa.api を import しない（循環回避・D1）。
app.include_router(ext_api.router)
# /ext/v1/* の全応答へ X-Request-Id を一元付与し、認証成功後の全終了経路を1リクエスト=1行で監査する
# （生 ASGI ミドルウェア・自動422/未処理例外も含む・他ルートには関与しない）。
app.add_middleware(ext_api.ExtRequestMiddleware)


# ===== current-user 依存（SHERPA_AUTH_DISABLED=1 は admin 固定・sherpa.deps へ移動済み）=====
# ensure_workspace も sherpa.deps へ移動済み（ここでは再エクスポートを import 済み）。
# _workspace_files_dir / _confined_path / _safe_workspace_filename は
# sherpa/routers/workspace.py へ移動済み（_safe_workspace_filename は上記で再エクスポート）。

# _current_user / _require_admin は sherpa.deps 定義。api.py 残留ハンドラからは呼ばれなくなった
# （最後の呼び出し元だった認証・システム追加系がスライス8で routers/auth.py・
# routers/system_extras.py へ移動）ため、api.py 側の再エクスポートはやめた（router 側が直接 import）。

# _analyses/_analyses_lock/_seq/_ANALYSES_TTL_SECONDS/_impact_gc_expired は
# sherpa/routers/impact.py へ移動済み（_analyses 等は上記で再エクスポート）。
_auth_startup_audited = False


# _driver は sherpa.deps へ移動済み（neo4j_session と一体）。


# ChatReq は sherpa/routers/chat.py へ移動済み（ここでは再エクスポートを import 済み）。

# TroubleshootReq / QaReq は sherpa/routers/impact.py（lens_router）へ移動済み。
# GraphAskReq は sherpa/routers/graph.py へ移動済み。
# RerunReq は sherpa/routers/worlds.py（ingest_runs_router）へ移動済み。

# SettingsReq / _BEDROCK_MODEL_IDS / _bedrock_model_id_valid は sherpa/routers/system.py へ移動済み。

# _resolve_world / validated_scope（内部の _check_scope 含む）/ neo4j_session は sherpa.deps 定義。
# api.py 残留ハンドラからは呼ばれなくなった（最後の呼び出し元だったチャット系がスライス7で
# sherpa/routers/chat.py へ移動）ため、api.py 側の再エクスポートはやめた（router 側が直接 import）。
# _require_world は validated_scope の内部専用だが、tests/integration/test_worlds_admin.py が
# `from sherpa.api import _require_world` で直接 import するため互換のため引き続き import・再エクスポートする。


# POST /auth/login・GET /auth/me・POST /auth/logout・POST /auth/change-password
# （LoginReq/PasswordChangeReq・_set_session_cookie/_clear_session_cookie/_is_secure・_SESSION_DAYS も
# 含む）は sherpa/routers/auth.py へ移動済み（auth_router を include_router・下記 /admin/users
# 登録の直前）。`_ensure_initial_admin` は sherpa/deps.py へ移動済み（ここでは再エクスポートを
# import 済み・tests/unit/test_auth_defaults.py の `api._ensure_initial_admin()` 互換）。
# `_validate_new_password` も sherpa.deps へ移動済み（ここでは再エクスポートを import 済み）。

# ShareCreateReq / ShareRevokeReq は sherpa/routers/shares.py へ移動済み。

# _ANNOUNCEMENT_CATEGORIES / AnnouncementCreateReq / AnnouncementPatchReq / SystemSettingsReq /
# ExtKeyCreateReq・GET /health/summary・GET /admin/health・GET/POST/PATCH/DELETE /announcements*・
# GET/PUT /admin/settings・POST/GET/DELETE /ext/v1/admin/keys・POST /ext/v1/admin/keys/recover
# （12ルート）は
# sherpa/routers/system_extras.py へ移動済み（extras_router を include_router・healthz_router
# include の直後・下記参照）。`_sweep_expired_announcements`（名前に反して maintenance 側）と
# `_auth_bootstrap_on_startup`（lifespan 起動処理）は api.py に残る。


app.include_router(auth_routes.auth_router)


# /admin/users (GET/POST/PATCH) は sherpa/routers/admin_users.py へ移動済み（router を include_router）。
app.include_router(admin_users.router)


# /admin/audit・/admin/audit/verify・/admin/audit/export・/admin/usage/stats は
# sherpa/routers/audit_usage.py へ移動済み（audit_usage_router を include_router）。
app.include_router(audit_usage.audit_usage_router)


# GET /admin/improvement-log/export（改善ログのエクスポート）。
app.include_router(improvement_log.improvement_log_router)


# /users/suggest・POST /conversations/{cid}/shares・GET /share/conversations/{token}・
# POST /conversation-shares/{share_id}/revoke は sherpa/routers/shares.py へ移動済み
# （shares.router を include_router・conversation_share_create は上記で再エクスポート）。
app.include_router(shares.router)


# /workspace/files (POST/GET/DELETE)・/workspace/files/{file_id}/download・/workspace/search は
# sherpa/routers/workspace.py へ移動済み（workspace.router を include_router・workspace_search /
# workspace_file_download は上記で再エクスポート）。sweep/GC（_sweep_expired_workspace 等）は
# lifespan 契約のため api.py に残す（下記参照）。
app.include_router(workspace.router)


# /impact/run・/impact/{aid}・/impact/{aid}/export.xlsx・/scopes は
# sherpa/routers/impact.py（impact_router）へ移動済み。
app.include_router(impact.impact_router)


# /chat・/chat/stream・/chat/stream/stop・/chat/turns・/chat/turns/{turn_id}/stream・
# /chat/turns/running・/chat/turns/{turn_id}/stop は sherpa/routers/chat.py（chat_router）へ
# 移動済み（_check_chat_write/_STREAM_STOP_LOCK/_STREAM_STOP_EVENTS/_persist_turn_crash/
# _turn_run_fn もこのモジュールへ純移動・上記で必要な分だけ再エクスポート）。
app.include_router(chat.chat_router)


# /config・/settings* は sherpa/routers/system.py へ移動済み（settings_router を include_router）。
app.include_router(system.settings_router)


# /conversations・/conversations/{cid}（GET/DELETE/PATCH）・/conversations/{cid}/pin は
# sherpa/routers/conversations.py へ移動済み（PinReq/RenameReq もそちらへ移動）。
app.include_router(conversations.router)


# /troubleshoot/run・/qa/run は sherpa/routers/impact.py（lens_router）へ移動済み。
app.include_router(impact.lens_router)


# /documents/download は sherpa/routers/documents.py（download_router）へ移動済み。
app.include_router(documents_routes.download_router)


# /ingest/preview は sherpa/routers/worlds.py（ingest_preview_router）へ移動済み。
app.include_router(worlds_routes.ingest_preview_router)


# /documents・/admin/es/search は sherpa/routers/documents.py（documents_router）へ移動済み。
app.include_router(documents_routes.documents_router)


# /world-options・/fs/list・/worlds・/worlds/{wid}/status・/worlds/{wid}/recount・POST /worlds・
# /worlds/diff・/worlds/{wid}/diff・/worlds/{wid}/rebind・/worlds/{wid}/refresh・
# /worlds/{wid}/rag_regenerate_rules・/worlds/{wid}/reconvert・DELETE /worlds/{wid}
# （13ルート）は sherpa/routers/worlds.py（worlds_router）へ移動済み。GRAPH-SRC（2026-09-04）で
# /worlds/{wid}/extract・/worlds/{wid}/concepts/propose・confirm・disable の4ルートを撤去。
app.include_router(worlds_routes.worlds_router)


# /graph・/graph/facets・/graph/search・/graph/ask（＋_graph_node_limit）は
# sherpa/routers/graph.py へ移動済み。
app.include_router(graph.router)


# /ingest/rerun・/ingest/runs は sherpa/routers/worlds.py（ingest_runs_router）へ移動済み。
app.include_router(worlds_routes.ingest_runs_router)


# /healthz は sherpa/routers/system.py へ移動済み（healthz_router を include_router）。
app.include_router(system.healthz_router)


# GET /health/summary・GET /admin/health（＋運営掲示板・全体設定・外部APIキー・計12ルート）は
# sherpa/routers/system_extras.py へ移動済み（extras_router を include_router・これらのルートは
# golden 上で連続しているためこの1箇所で定義順は不変）。
app.include_router(system_extras.extras_router)


def _auth_bootstrap_on_startup():
    """認証の起動時状態をDBへ記録する（DB不可ならログのみ）。lifespan（sherpa.lifespan）が起動時に呼ぶ。"""
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


# ===== 定期ポーリング（変更検知して再取り込み・即反映ライブ鏡）=====
# 既定オフ。`SHERPA_POLL_SECONDS=300` 等で有効化（>0）。意図実行（/worlds/{id}/refresh）とは別系統。
def _under_fixtures(p) -> bool:
    """path（env 値や registry root）が **symlink 追跡後**に fixtures コーパスを指すか。

    `.resolve()` で alias（例 `/tmp/kb -> .../fixtures/corpus`）も実体まで辿ってから `fixtures` 要素を判定＝
    literal な part 一致だけだと symlink で素通りするため（Codex RV HIGH）。非実在パスでも resolve は例外にしない。
    """
    if not p:
        return False
    try:
        return "fixtures" in Path(p).resolve().parts
    except Exception:
        return False


def _warn_fixtures():
    """fixtures（架空 golden）に到達し得る設定なら起動時に大きく警告＝本番で silently ON にしない（C・本番非参照の担保）。

    検査は **フラグ（`SHERPA_USE_FIXTURES`）／fixtures を指せる env 経路**
    （`SHERPA_KB_DIR`／`SHERPA_DERIVED_DIR`）**／主経路＝DB レジストリの
    world root_path** を点検する＝フラグOFFでも KB パスや登録 world が fixtures を指せば抜け穴になるため
    （Codex RV BLOCKER/HIGH）。旧・意味層機構の環境変数2種（concepts/semantic 系）は GRAPH-SRC
    （2026-09-04）で読み手（旧 `worlds.semantic_paths()`）ごと撤去済みのためこの一覧からも外した。
    判定は symlink 追跡後（`_under_fixtures`）。`SHERPA_ENV` が prod/production で
    **いずれか1つでも fixtures を指す**なら**設定ミス**として起動拒否（fail-closed）。dev は大警告のうえ続行。
    なお `make serve` は自身で `SHERPA_ENV=production` を立てるので shell 継承フラグ/誤登録も拒否される。
    """
    import logging
    reasons = ["SHERPA_USE_FIXTURES が有効"] if worlds._fixtures() else []
    for name in ("SHERPA_KB_DIR", "SHERPA_DERIVED_DIR"):
        val = os.environ.get(name)
        if _under_fixtures(val):
            reasons.append("%s=%s が fixtures を指す（symlink 追跡後）" % (name, val))
    try:                                                 # 主経路＝登録 world の root_path が fixtures（DB best-effort）
        for row in store.list_worlds_db():
            if _under_fixtures(row["root_path"]):
                reasons.append("登録 world '%s' の root_path が fixtures を指す" % row["world_id"])
    except Exception:
        pass
    if not reasons:                                      # fixtures へ到達する設定が一切無い＝本番の正常系
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
    if is_prod:                                          # 本番マーカー＋fixtures 到達経路＝設定ミス → fail-closed
        log.error(banner)
        raise RuntimeError(
            "設定ミス: SHERPA_ENV=%s（本番）で fixtures に到達し得る設定があります（%s）。"
            "本番でテストデータを配信しないため起動を拒否します（該当を外すか `make serve` を使用）。"
            % (env, " / ".join(reasons)))
    log.warning(banner)


def _warn_test_db_isolated():
    """`SHERPA_TEST_DB_ISOLATED` が起動時に立っていたら大きく警告（本番なら起動拒否）。

    `tests/conftest.py` がテスト用 DB 分離を有効化した時に立てる内部フラグで、
    `sherpa.reconcile.reconcile_derivatives()` の孤児掃除を**無警告のまま全面 skip** させる
    （2026-07-03 インシデント対応）。この skip 自体はテスト実行中は正しい安全策だが、
    誤って `serve`/実運用プロセスの環境にこの env が残っていると、孤児掃除が**恒久的に
    無効化されたまま気付かれない**という別の事故を生む（RV MEDIUM）。`_warn_fixtures` と
    同じ流儀＝dev は大警告のうえ続行、`SHERPA_ENV=production` は設定ミスとして起動拒否。
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

    Codex sandbox が無効（`SHERPA_CODEX_SANDBOX` off）だと、Codex 実行時の permission profile による
    読取封じ込め（KB＋authoring のみに限定）が外れ、旧 `-s workspace-write`（読取全開・多層防御を
    OS ユーザ分離のみに委ねる緊急時の逃げ道）へフォールバックする（agents.py:1697-1710 参照）。
    これは緊急時専用の一時的な逃げ道であり、本番でこの逃げ道が有効なまま気付かれず稼働する事故を
    防ぐため、`_warn_fixtures` 等と同じ流儀＝dev は大警告のうえ続行、`SHERPA_ENV=production` は
    設定ミスとして起動拒否する（2026-07-13-横断レビュー対応.md R4）。
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
    """`SHERPA_ENV=production` で初期 admin パスワードが**未設定**なら、`_ensure_initial_admin()` が
    DB にパスワードを刻む**前**に fail-closed で起動拒否する（`_warn_fixtures` と同じ流儀）。

    判定は **env（`SHERPA_ADMIN_PASSWORD`）のみ**で行い、DB 内の実際の admin パスワードハッシュは見ない。
    `_ensure_initial_admin()`（`sherpa/deps.py` 定義）を読むと、既定パスワードは「admin 行がまだ
    `password_hash` を持たない＝初回シード時」にしか参照されない（`if admin and admin.get("password_hash"):
    return admin` で即 return）ため、env のみの判定でも「production では
    SHERPA_ADMIN_PASSWORD を実際の値に設定する」運用を一律強制できる（DB 照会をしてまで精密判定する
    コストには見合わない）。

    **明示設定されていれば開発既定と同値でも起動を許す**（ユーザー決定 2026-07-10・2026-09-03 に
    ENV-ONE で一度撤回→同日ユーザー裁定で復元）: 閉域網前提かつ初回ログインでパスワード変更が強制
    （`must_change_password=true`）されるため、既定値で起動しても最初のログインで必ずローテーション
    される。拒否するのは**未設定（空文字・空白のみ含む）のみ**（空白のみは `strip()` すると空になり
    「未設定」と同じ扱い。`auth.initial_admin_password()` は env をそのまま `or` で使うため、
    空白のみの env はそちらでは「明示設定あり」として通ってしまう＝ここで弾かないと空白パスワードが
    初期値になる）。
    """
    import logging
    from sherpa import auth
    env = os.environ.get("SHERPA_ENV", "").strip().lower()
    is_prod = env in ("prod", "production")
    configured = os.environ.get("SHERPA_ADMIN_PASSWORD")
    if configured is not None and configured.strip():
        return   # 明示設定あり＝起動許可（開発既定と同値でも初回ログインの変更強制でローテーションされる）
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
    """`SHERPA_ENV=production` で env の値に配布テンプレのプレースホルダ（`CHANGE_ME`）が残って
    いたら fail-closed で起動拒否する（ENV-ONE・env 例の1本化・2026-09-03・`_warn_fixtures` と
    同じ流儀）。`.env.example`（唯一の例＝本番もここからコピーする）冒頭の「0. 本番チェックリスト」
    節はこの語を含む値をコメントで示す——それを未編集のまま持ち込んだ場合を検知する。

    どのキーが該当したかはログへ**平文で**出す（運用者が直せるように）。値そのものは伏せる
    （秘密を漏らさないため）。
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
    """本番プロファイルで `SHERPA_AUTH_DISABLED` が設定されていたら起動時に1回だけ ERROR ログを残す
    （TOGGLE-RM・2026-09-03）。

    `auth.auth_disabled()` は `SHERPA_ENV` が prod/production のとき既にこの env を無視し常に False
    を返す（`sherpa/auth.py` 参照・fail-safe）ため、これは起動時の誤設定**検知**（可観測性）のみで
    起動は止めない（合成 admin は実際には有効化されないため `_warn_fixtures` 等と違い fail-closed に
    する必要がない＝運用者が env の消し忘れに気付けるよう警告するだけ）。
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


# env → system_settings への**初回シードのみ**（以後 env は読まない）。対象は `sherpa.keys` が解決に
# 使う3キー（openai/gemini/bedrock の API キー）＋個人キー許可フラグ＋ Web 検索管理者許可フラグ
# （WEB-1・`SHERPA_ALLOW_WEB_SEARCH` → `web_search_allowed`）。`OLLAMA_URL` は対象外
# （`_seed_ollama_url_from_env()` が独立マーカーで扱う・下記参照）。値は system_settings
# 側のキー名で揃える（`sherpa/keys.py::_KEY_FIELD` と同名）。
_SEED_ENV_KEYS = (
    ("OPENAI_API_KEY", "openai_api_key"),
    ("GEMINI_API_KEY", "gemini_api_key"),
    # Bedrock は Bearer キーの別名2つ（`sherpa/providers/bedrock.py::_BEDROCK_ENV_KEYS` と同順）のうち
    # 先に見つかった方を採用する（SigV4/AWS_ACCESS_KEY_ID 等のチェーンはインフラ管理のまま・対象外）。
    ("AWS_BEARER_TOKEN_BEDROCK", "bedrock_api_key"),
    ("ANTHROPIC_AWS_API_KEY", "bedrock_api_key"),
)

_SEED_SECRET_KEYS = frozenset({"openai_api_key", "gemini_api_key", "bedrock_api_key"})

# 資格情報シード完了マーカー（system_settings のキー）。存在すれば資格情報のシードは完全に
# 終わっている＝env は二度と読まない。`store.seed_system_settings_once` がこのキー（guard_key）を
# 「今この瞬間に存在するか」で全 INSERT を条件付けるため、マーカーが立った後は他のキーが個別に
# 削除されても env から再挿入されない。

# `_ENV_SEED_MARKER_KEY`（"env_seed_version"）は分離前のコードが「資格情報も Ollama も含めて
# env→system_settings の初回シードが完了した」と理解する唯一のシグナルだった。分離後は資格情報
# 自身の冪等性ガードには `_CREDENTIAL_SEED_MARKER_KEY` を使い、`env_seed_version` は
# `_confirm_legacy_env_seed_marker()` が両方の新ガード（資格情報／Ollama）が確定した後にだけ
# 追いつき確定する互換 aggregate へ格下げする。

# なぜこの分離が必要か: 資格情報自身の冪等性ガードに `env_seed_version` を使い続けたまま Ollama
# だけ別ガードへ分離すると、旧統合コードで一度でも起動済みの環境（`env_seed_version` あり・新
# ガードなし）にアップグレードしたとき、`_seed_ollama_url_from_env()` は「未シード」と誤認して
# 残存 env から Ollama 接続先を再読・再認可してしまう（admin が意図的に削除した接続先が復活する）。
# この問題は資格情報側にも対称的に起こり得るため、資格情報にも専用ガードを設け、
# どちらの関数も「新ガードが無く、かつ旧 `env_seed_version` が既にある」場合は
# **env を一切再読せず**、新ガードだけを「移行済み」として確定する（下記の両関数を参照）。

# rollback 時（DB 復元は必須契約ではない・docs/manual/41-運用Runbook.md 参照）の互換のため、
# `env_seed_version` は「資格情報シード成功」と「Ollama シード終了（成功・対象なし いずれも含む）」
# の**両方**が確定して初めて `_confirm_legacy_env_seed_marker()` が書く。資格情報シードの直後に
# 中断し、旧版（分離前）へロールバックした場合、旧版は `env_seed_version` が無いことを見て
# 自分の契約どおり env を再評価できる（Ollama を含めて）。
_CREDENTIAL_SEED_MARKER_KEY = "credential_seed_version"
_CREDENTIAL_SEED_VERSION = 1

_ENV_SEED_MARKER_KEY = "env_seed_version"
_ENV_SEED_VERSION = 1


def _seed_settings_from_env():
    """env の資格情報を system_settings へ**一生に一度だけ**取り込む（`OLLAMA_URL` は対象外・
    `_seed_ollama_url_from_env()` 参照）。

    完了マーカー（`system_settings.credential_seed_version`）の事前チェック（下の
    `sysset.get(...)`）は TTL キャッシュ済みの安価な早期 return に過ぎず、正しさの根拠ではない。
    実際の不変条件（「マーカーが立った後は env から何も書かない」）は
    `store.seed_system_settings_once` が保証する（マーカーの有無をキーごとの INSERT 文自身に
    `WHERE NOT EXISTS` で埋め込み、同一トランザクション内で都度確認する）＝この事前チェックが
    どれだけ古くても、管理者がその間に特定のキーだけ削除していても、実際の書込みは常に「今」の
    マーカー有無で判定されるため復活しない。

    旧 `env_seed_version`（分離前のコードが資格情報／Ollama 両方の完了として理解する唯一の
    シグナル）が既にある環境（`_CREDENTIAL_SEED_MARKER_KEY` 分離より前に一度でも起動済み）では、
    env を再読して復活させてはならない。この判定も上と同じ理由でキャッシュ経由の事前チェックを
    根拠にできないため、`store.migrate_marker_if_legacy_exists()` に advisory lock 取得後・
    フレッシュに確認させる（存在すれば env は一切読まず `_CREDENTIAL_SEED_MARKER_KEY` だけを
    「移行済み」として確定する・`store.migrate_marker_if_legacy_exists` の docstring 参照）。

    DB 到達不可などで例外になった場合は何も書かない＝次回起動、または `healthz()` の
    ready 確認のたび（`routers/system.py::healthz` 参照・シード自体が冪等なため無害）に再試行される。

    DB に値があり env も設定されていて食い違う場合は、無視されたキー名を集約して1行だけ警告する。
    """
    from sherpa import agent_constructs, store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（env→system_settings）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_CREDENTIAL_SEED_MARKER_KEY) is not None:
        return   # 安価な早期 return（上の docstring 参照。上書き防止自体はここに依存しない）。
    try:
        if store.migrate_marker_if_legacy_exists(
                _CREDENTIAL_SEED_MARKER_KEY, _ENV_SEED_MARKER_KEY, _CREDENTIAL_SEED_VERSION):
            return
    except Exception as e:
        log.warning("起動時シード（credential_seed_version 移行）に失敗しました（DB 不達の可能性）: %s", e)
        return
    try:
        candidate: dict[str, object] = {}
        env_name_of: dict[str, str] = {}   # sys_key -> env_name（不一致警告の逆引き用）
        for env_name, sys_key in _SEED_ENV_KEYS:
            if sys_key in candidate:        # bedrock の別名2つのうち先に見つかった方だけ使う
                continue
            raw = os.environ.get(env_name)
            if raw is None:
                continue
            value = raw.strip()
            if not value:
                continue
            if sys_key == "openai_api_key" and not agent_constructs.is_real_api_key(value):
                continue                    # .env.example のプレースホルダはシードしない
            candidate[sys_key] = value
            env_name_of[sys_key] = env_name
        raw_personal = os.environ.get("SHERPA_PERSONAL_API_KEYS")
        if raw_personal is not None:
            candidate["personal_api_keys_allowed"] = raw_personal.strip().lower() in ("1", "true", "yes", "on")
            env_name_of["personal_api_keys_allowed"] = "SHERPA_PERSONAL_API_KEYS"
        # WEB-1: Codex の web_search 管理者許可フラグ。system_settings（管理画面「プロバイダ＋
        # 接続先」タブ）が唯一の真実源＝env は他の資格情報と同じくこの初回シードのみ
        # （`sherpa/providers/codex/sandbox.py::_web_search_admin_allowed` 参照）。
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


def _confirm_legacy_env_seed_marker():
    """`_CREDENTIAL_SEED_MARKER_KEY`／`_OLLAMA_URL_SEED_MARKER_KEY` が両方確定した後、互換のため
    旧共有マーカー（`env_seed_version`）も追いつき確定する（`_seed_settings_from_env`
    の docstring 参照）。

    `_seed_settings_from_env()`・`_seed_ollama_url_from_env()` の**両方の後**（lifespan・healthz
    のどちらでも同じ順序）に呼ぶ。どちらか一方でも未確定なら何もしない（次回呼び出しで再評価）。
    書き込みは `env_seed_version` の1キーのみ（他のキーには一切触れない）。
    """
    from sherpa import store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（env_seed_version 互換確定）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_ENV_SEED_MARKER_KEY) is not None:
        return
    if sysset.get(_CREDENTIAL_SEED_MARKER_KEY) is None or sysset.get(_OLLAMA_URL_SEED_MARKER_KEY) is None:
        return
    try:
        store.seed_system_settings_once(
            {_ENV_SEED_MARKER_KEY: _ENV_SEED_VERSION}, guard_key=_ENV_SEED_MARKER_KEY)
    except Exception as e:
        log.warning("起動時シード（env_seed_version 互換確定）に失敗しました（DB 不達の可能性）: %s", e)


# OLLAMA_URL の env→system_settings 初回シード。独立した完了マーカー（`env_seed_version` とは別）を
# 持つ（`_seed_openai_endpoint_from_env` と同じ型）。OLLAMA_URL 専用のマーカーに分離しているため、
# 不正な間は**このマーカーだけ**確定しない＝他の資格情報（openai_api_key 等）の確定は妨げず、
# 「env を直した後の次回起動で再評価される」という docstring の約束をこのマーカーだけで果たす。
_OLLAMA_URL_SEED_MARKER_KEY = "ollama_url_seed_version"
_OLLAMA_URL_SEED_VERSION = 1


def _seed_ollama_url_from_env():
    """`OLLAMA_URL` を system_settings（`ollama_url`）へ**一生に一度だけ**取り込む。

    `OLLAMA_URL` 未設定（env に無い）はそれ自体正常なのでマーカーだけ確定する（既定＝localhost の
    まま・以後の起動のたびに無駄な再評価をしない）。DB 到達不可・形式が不正（userinfo/path/query
    混入・不正 scheme 等）ならマーカーを立てずに warning を残して return する＝次回起動時に env を
    直せば再評価される（`_seed_openai_endpoint_from_env` と同じ契約）。

    旧 `env_seed_version`（分離前のコードが資格情報／Ollama 両方の完了として理解する唯一の
    シグナル）が既にある環境（`_OLLAMA_URL_SEED_MARKER_KEY` 分離より前に一度でも起動済み）では、
    env を再読せず `_OLLAMA_URL_SEED_MARKER_KEY` を「移行済み」として確定するだけにする＝
    URL・allowlist・fingerprint を一切復活させない（admin が意図的に削除した接続先が、アップグレード
    後に残存 env から再送・再認可される穴を防ぐ）。この判定は
    `store.migrate_marker_if_legacy_exists()` が advisory lock 取得後にフレッシュに行う
    （`_seed_settings_from_env`／`store.migrate_marker_if_legacy_exists` の docstring 参照・
    OLLAMA_URL の形式が不正でも、レガシー環境であればこの判定を優先する＝形式チェックより前に行う）。

    正当な値は host:port のみへ正規化して保存する（userinfo・余分な path/query を含む値をそのまま
    system_settings／監査へ残さない・admin PUT 経路の `_validate_central_ollama_url`／
    `_canonical_host_port` の正規化と揃える）。非 loopback ホストは同一トランザクションで
    `ollama_allowlist` へも追記する（`llm._allowlisted_hosts()` は env を一切見ないため＝所有原則・
    env は初回シード専用。追記しないと、シードされた直後からその中央既定そのものへ誰も接続できなく
    なる＝`assert_ollama_url_allowed` が弾く）。`ollama_allowlist_merge` は「ollama_url がこの
    トランザクションで実際に新規挿入できた場合だけ」host:port を追記する（`store.seed_system_settings_once`
    参照・URL と認可をペアで原子的に確定させる）。
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
    try:
        if store.migrate_marker_if_legacy_exists(
                _OLLAMA_URL_SEED_MARKER_KEY, _ENV_SEED_MARKER_KEY, _OLLAMA_URL_SEED_VERSION):
            return
    except Exception as e:
        log.warning("起動時シード（ollama_url_seed_version 移行）に失敗しました（DB 不達の可能性）: %s", e)
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


# `_seed_settings_from_env` の是正（中央 ollama_url を allowlist へ同時に加える）は「マーカーが
# 既に無い場合だけ」動く。これより前に一度でも起動した環境（`env_seed_version` 済み）はこの分岐を
# 二度と通らないため、専用の完了マーカーで**一度だけ**独立に追いつきを試みる（v2・簡素化裁定・
# RV 4巡目）。旧マーカー名 `ollama_allowlist_env_seed_catchup`（値一致だけを provenance とみなす
# v1）は判断材料が乏しく、admin の削除操作を復活させ得る穴があったため、判定方式ごと別マーカーへ
# 切り替える（旧マーカーの有無はもう見ない＝ v2 は監査ログから独立に証明を試みる）。
_OLLAMA_ALLOWLIST_CATCHUP_MARKER_KEY_V2 = "ollama_allowlist_env_seed_catchup_v2"


def _catchup_ollama_allowlist_for_central_url():
    """既に env シード済みの環境向けの一度きりの追いつき評価＋恒常的な健全性警告（RV 4巡目・
    簡素化裁定）。

    この救済は「このセッション以前に旧版 seed を踏んだ既存展開」のための一度きりのもので、
    対象は実質 dev 環境のみ。複雑化するより fail-closed に倒す:
      1. v2 marker が無ければ `store.catchup_ollama_allowlist_for_env_seeded_url_v2()` を一度だけ
         試みる（監査ログから「env シードが `ollama_url` を挿入し、以後 admin 操作が無い」ことを
         証明できた場合だけ追加する・証明できなければ何も書かない＝fail-closed）。
      2. marker の有無に関わらず**毎回**（healthz のシード再試行サイクルごと）、現在の中央
         `ollama_url`（非 loopback）が現在の `ollama_allowlist` に無ければ警告ログを1行残す
         （自動修復はしない・管理画面での手動追加へ誘導する）。証明できずスキップされた場合も、
         この警告で運用者が気づけるようにする。
    """
    from sherpa import llm, store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時移行（ollama_allowlist 追いつき）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_OLLAMA_ALLOWLIST_CATCHUP_MARKER_KEY_V2) is None:
        try:
            reason = store.catchup_ollama_allowlist_for_env_seeded_url_v2(
                guard_key=_OLLAMA_ALLOWLIST_CATCHUP_MARKER_KEY_V2)
            if reason == "added":
                log.info("起動時移行: 中央 Ollama 接続先を ollama_allowlist へ追加しました")
            sysset = store.get_system_settings()   # marker/allowlist が変わった可能性があるため再読込
        except Exception as e:
            log.warning("起動時移行（ollama_allowlist 追いつき）に失敗しました（DB 不達の可能性）: %s", e)
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


# OpenAI 接続先（Azure OpenAI 等・SET-2c）の env→system_settings 初回シード。独立した完了マーカー
# （`env_seed_version` とは別）を持つ＝この機能追加より前に一度でも起動済みの環境（`env_seed_version`
# は既に存在する）でも、このシードだけは改めて一度だけ評価される（`model_catalog.seed_catalog_once`
# と同じ理由・型：`store.seed_system_settings_once` は guard_key の存在有無で全キーを一括判定するため、
# 既存の `env_seed_version` を共有すると、それより後に追加したこの4キーは既存展開では永遠にシード
# されない）。
_OPENAI_ENDPOINT_SEED_MARKER_KEY = "openai_endpoint_seed_version"
_OPENAI_ENDPOINT_SEED_VERSION = 1


def _openai_endpoint_seed_candidate() -> dict:
    """env から起動時シード候補を組み立てる（I/O なし・DB へは触れない）。

    実体は `sherpa.llm.openai_endpoint_seed_candidate()`（`sherpa/api.py` は FastAPI アプリ全体を
    import する重い依存を持つため、依存未導入でも動く必要がある `scripts/check_production_openai_probe.py`
    等の軽量な呼び出し元は `sherpa.llm` 側を直接呼ぶ）。この関数はモジュール内の既存呼び出し
    （`_seed_openai_endpoint_from_env`）とテストの互換のためだけに残す薄い委譲。
    """
    from sherpa import llm
    return llm.openai_endpoint_seed_candidate()


def _seed_openai_endpoint_from_env():
    """OpenAI 互換 API の接続先（`OPENAI_BASE_URL`／`SHERPA_OPENAI_AUTH_HEADER`／
    `SHERPA_OPENAI_API_VERSION`／`SHERPA_OPENAI_ENDPOINT_KIND`）を system_settings へ**一生に一度だけ**
    取り込む（以後 env は読まない・`sherpa/llm.py` 参照）。`OPENAI_EMBED_MODEL` は
    `model_catalog.seed_catalog_once`（openai/embed）が別途扱う＝ここでは触れない。

    `_openai_endpoint_seed_candidate()` が候補全体を検証する。不正（base URL 不正・kind/auth_header
    の値が不正・kind が openai 以外なのに base_url が無い等）なら**完了マーカーを立てずに** warning
    を残して return する＝次回起動（または `healthz()` の再試行）で env を直した後に再評価される
    （不正な値だけ無視して残りを永久確定することはしない）。env に何も設定されていない（候補が空）
    場合は、それ自体は正常なのでマーカーだけ確定する（既定＝OpenAI 本家のまま・以後の healthz
    呼び出しのたびに無駄な再評価をしない）。

    DB 到達不可などで例外になった場合も同様にマーカーを立てず return する（次回起動、または
    `healthz()` の ready 確認のたびに再試行される・`_seed_settings_from_env` と同じ形）。

    env 候補が**不正で確定できない**場合は、DB 到達不可（一時障害・次回再試行を待てばよい）と区別し、
    `llm.set_openai_endpoint_seed_blocked()` でプロセス内フラグを立てる（OpenAI 系 I/O 全体を
    fail-closed にする・`sherpa/llm.py` 参照）。DB 上は「未設定＝OpenAI 本家既定」と区別が付かない
    ため、黙って本家既定へ fail-safe すると本家向けでないキー/リクエストを誤って本家へ送りかねない
    （正当な既定と未確定を区別する）。候補が有効（空も含む）に確定した／DB 一時障害だった場合は、
    以前のブロックが残っていればここで解除する（env をホットフィックスした場合の後続 `healthz()`
    再試行が復旧できるように）。
    """
    from sherpa import llm, store
    log = logging.getLogger("sherpa")
    try:
        sysset = store.get_system_settings()
    except Exception as e:
        log.warning("起動時シード（openai_endpoint）に失敗しました（DB 不達の可能性）: %s", e)
        return
    if sysset.get(_OPENAI_ENDPOINT_SEED_MARKER_KEY) is not None:
        # マーカーが既にある＝候補は（この worker か、他の手段で）確定済み。この worker 自身の
        # プロセス内フラグが blocked のまま残っていれば解除する（マーカー確定を検知しないと、
        # 一度 blocked になった worker は再起動しない限り fail-closed が永続してしまう）。
        # DB へ「誰が解除したか」を書き戻す共有化はしない（プロセス内フラグの簡素さを保つ裁定・
        # Runbook の運用契約参照）。
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
        llm.set_openai_endpoint_seed_blocked(None)   # 以前のブロックが残っていれば解除（ホットフィックス後の再試行）
    except Exception as e:
        log.warning("起動時シード（openai_endpoint）に失敗しました（DB 不達の可能性）: %s", e)


# 調べる深さ（SC-6c・調べ方ブロック §3.2）の基準値7項目・env→system_settings 初回シード。独立した
# 完了マーカーを持つ（`_seed_openai_endpoint_from_env`／`model_catalog.seed_catalog_once` と同じ
# 「一度だけ」方式）。候補値は各モジュールの既存 env 定数（`agentic_search.MAX_TURNS` 等）＋
# `chat_service.QA_MAX_HITS_DEFAULT`（env非依存の固定既定）をそのまま複製するだけ——ここで env を
# 直接読まない（各モジュールの既存定数を単一の真実源のまま保つ・`sherpa/depth_profile.py` の
# 契約と同じ）。シード後は admin-settings.html の基準値編集（system_settings）が唯一の真実源になり、
# 事後の env 変更・削除は反映されなくなる（SET-2「設定の所有権はUI」原則・WEB-1/openai_endpoint と
# 同じ契約）。数値6項目は常に有効（各モジュールの既存定数は起動時に既に検証済み）なため
# fail-closed ブロック経路は不要。**例外は Codex 推論のみ**＝env 由来の自由文字列のため語彙検証し、
# 不正なら7項目・マーカーとも書かずに保留する（下の docstring 参照）。
_DEPTH_PROFILE_SEED_MARKER_KEY = "depth_profile_seed_version"
_DEPTH_PROFILE_SEED_VERSION = 1


def _seed_depth_profile_from_env():
    """調べる深さの基準値7項目を system_settings へ**一生に一度だけ**取り込む。

    完了マーカー（`depth_profile_seed_version`）があれば env は一切読まない（管理者が基準値を
    削除した後の再起動で env 既定値から復活しない）。DB 到達不可などで例外になった場合はマーカーを
    立てず return する（次回起動、または `healthz()` の ready 確認のたびに再試行される・
    `_seed_openai_endpoint_from_env` と同じ形）。

    `SHERPA_CODEX_REASONING` は管理 API の `_validate_depth_base_codex_reasoning` と同じ
    `strip().lower()` 正規化後、既知語彙（`depth_profile.CODEX_REASONING_LEVELS`）でなければ
    エラーログのみ出し、**7項目・マーカーとも一切書かず**終了する（不正値を一回性マーカー付きで
    永続化すると、env を直しても健全な値へ自動回復しなくなるため）。env 修正後は次回起動、または
    `healthz()` の再試行で7項目が一括で確定する。
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
    reasoning_env = os.environ.get("SHERPA_CODEX_REASONING", "low").strip().lower()
    if reasoning_env not in depth_profile.CODEX_REASONING_LEVELS:
        log.error(
            "起動時シード（depth_profile）を見送りました: SHERPA_CODEX_REASONING の値が不正です"
            "（%r）。選べる値: %s。env を修正してください（基準値7項目は未シードのまま残り、"
            "修正後の再起動または healthz の再試行で一括シードされます）。",
            reasoning_env, ", ".join(depth_profile.CODEX_REASONING_LEVELS))
        return
    try:
        keys = depth_profile.BASE_SETTINGS_KEYS
        candidate = {
            keys["max_turns"]: agentic_search.MAX_TURNS,
            keys["grep_max_hits"]: agentic_search.MAX_HITS,
            keys["qa_max_hits"]: chat_service.QA_MAX_HITS_DEFAULT,
            keys["read_window"]: agentic_search.READ_WINDOW,
            keys["impact_depth"]: impact_service.IMPACT_MAX_DEPTH,
            keys["troubleshoot_depth"]: lens_service.TROUBLESHOOT_GRAPH_DEPTH,
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


def _purge_personal_keys_if_disabled_on_startup():
    """A6（個人 API キー原則）が偽なら、起動のたびに全ユーザーの個人秘密キーを一括削除する。

    `_seed_settings_from_env()` の後に呼ぶ（初回起動時は personal_api_keys_allowed 自体が env から
    シードされることがあるため、シードで確定した後の値で判定する）。冪等
    （`store.purge_personal_api_keys` は実際に変更した行数だけを返す）ため、削除対象が無ければ
    何もログしない。既に false のまま個人キーが残っている既存 DB（アップグレード直後等）にも
    この不変条件を適用するための経路。
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
    """チャットターンのバックグラウンド実行（覗き窓方式）はプロセス内レジストリ（`sherpa.chat_turns`）
    のため uvicorn workers=1 前提（docs/proposals/2026-07-03-チャット背景実行.md §制約・明記済み）。

    workers>1 だと、ターンを開始した worker と購読/停止の HTTP リクエストを受けた worker が別
    プロセスになり得て、レジストリを共有しないため 404（見つからない）や取りこぼしが起きる
    （chat_turns の他、ratelimit の in-memory 状態も同じく worker 間非共有）。
    `scripts/run-api.sh` は `SHERPA_UVICORN_WORKERS` を `uvicorn --workers` へそのまま渡す実装
    （子 worker プロセスにも同じ env が継承される）。**production では fail-closed で起動拒否する**
    （2026-07-13-横断レビュー対応.md R4・`_warn_fixtures` 等と同じ流儀へ格上げ）。非 production は
    チャットを使わない構成もあり得るため起動そのものは止めず、大きく警告するだけに留める。
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


# worlds/fs 系ハンドラは sherpa/routers/worlds.py へ移動済み（スライス6）だが、この関数自体は
# lifespan.py:31 が `api._warn_browse_roots_missing()` を属性経由で直接呼ぶ起動処理のため api.py に
# 残す（_browse_roots は sherpa.deps へ移動・上記で再エクスポート済みの裸名を呼ぶ）。
def _warn_browse_roots_missing():
    """フォルダ選択の許可ルート（既定 `/mnt`）が1つも存在しなければ起動時に警告する。

    既定 `/mnt` は WSL（Windows ドライブ自動マウント）前提で、素の Linux サーバでは
    通常空か存在しない。取り込み登録の入口（`/fs/list`）が事実上使えないまま気付かれない
    事故を防ぐため、`_warn_fixtures` 等と同じ流儀＝大警告のみ（fail-closed にはしない・
    `SHERPA_BROWSE_ROOTS` で任意のルートを指せる環境もあり得るため起動は止めない）。
    既定値そのものは変えない（警告追加のみ・非破壊）。
    """
    import logging
    roots = _browse_roots()

    def _is_dir_safe(r: Path) -> bool:
        # is_dir() は権限エラー・壊れたマウント等で OSError を投げ得る。警告のみ・起動を
        # 絶対に壊さない要件のため、判定不能は「存在しない」扱いにする（fail-closed にはしない）。
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
        "!! Linux サーバ等（WSL の /mnt 自動マウント前提が成り立たない環境）では\n"
        "!! SHERPA_BROWSE_ROOTS 環境変数で資料フォルダの親ディレクトリを指定してください\n"
        "!!   例: SHERPA_BROWSE_ROOTS=/srv/sherpa-data\n"
        + "!" * 72,
        ":".join(str(r) for r in roots))


def _start_poller():
    import logging
    import threading
    import time
    from datetime import datetime, timezone

    from sherpa.ingest import background
    secs = int(os.environ.get("SHERPA_POLL_SECONDS", "0") or 0)
    if secs <= 0:
        return
    log = logging.getLogger("sherpa")

    # ING-3: 定期ポーリングの sync も `POST /worlds/{wid}/refresh` と同じ arbiter
    # （`sherpa.ingest.background.start_or_join`）を通す——世界単位の実行を1本化し、HTTP 経由の
    # 手動更新と裏でぶつからないようにする。op("refresh")/fingerprint は手動 refresh
    # （無引数＝`routers.worlds._fingerprint({})`）と揃えた固定値 `"{}"`——同じ操作として合流でき、
    # 他の操作（extract/delete 等）が実行中なら合流せず今回はスキップする（次回ポーリングで
    # 再試行すれば足りるため、静かに諦めてよい・ConflictError を利用者へ伝える先が無い）。
    _REFRESH_FP = "{}"

    def _loop():
        while True:
            time.sleep(secs)
            try:
                for r in store.list_worlds_db():          # 登録済み資料フォルダを巡回（変わった時だけ再取り込み）
                    wid = r["world_id"]

                    def _create_run(_wid=wid):
                        row = store.start_ingest_run(
                            _wid, scan_root=None, created_by="admin",
                            progress={"stage": "accepted",
                                     "stage_label": ingest_worker.STAGE_LABELS["accepted"],
                                     "done": None, "total": None,
                                     "updated_at": datetime.now(timezone.utc).isoformat()})
                        return row["id"]

                    try:
                        background.start_or_join(
                            wid, "refresh", _REFRESH_FP, _create_run,
                            lambda run_id, _wid=wid: ingest_worker.sync(_wid, run_id=run_id))
                    except (background.ConflictError, background.ShuttingDownError):
                        continue                          # 他の操作が実行中/終了処理中＝今回はスキップ
                    except Exception as e:
                        log.warning("poll sync failed for %s: %s", wid, e)
            except Exception as e:
                log.warning("poll loop error: %s", e)
            # W4: 個人 workspace の TTL 掃除＋孤児 GC も定期実行（起動時のみ→定期化）。
            _run_workspace_maintenance()

    threading.Thread(target=_loop, daemon=True, name="sherpa-poller").start()
    log.info("folder poller started: every %ss", secs)


def _reconcile_orphans():
    """起動時に孤児派生物（ES/Neo4j/派生MD）を自動掃除（不可視の自己修復）。

    別スレッドで best-effort＝起動を止めない／ES・Neo4j 未起動でも安全（registry 不確実なら何もしない）。
    取込/削除の直後にも走るが、コード更新で命名規則が変わった等の取りこぼしを起動時にも回収する。
    """
    import threading

    def _run():
        try:
            from sherpa import reconcile
            reconcile.reconcile_derivatives()
        except Exception:
            pass

    threading.Thread(target=_run, daemon=True, name="sherpa-reconcile").start()


def _sweep_expired_workspace() -> dict:
    """期限切れ個人 workspace ファイルを掃除（W4 TTL 自動掃除）。

    安全設計（reconcile と同じ「安全に自動」思想）:
    - DB 取得失敗（接続不可）なら一切削除しない（fail-safe）。
    - 物理ファイルは必ず files_dir 配下 resolve/relative_to で閉じ込め確認後のみ削除（base-confined）。
    - symlink は削除しない（is_symlink() で事前チェック）。
    - 無効化ユーザー（status='disabled'）の行は expired_workspace_files() が除外済み。
    - 物理削除失敗は best-effort（台帳 status='expired' は最低限記録）。
    - 共有 RAG（ES/Neo4j）には一切触れない（personal_workspace_files 台帳のみ）。
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

        # HIGH-2/3: 台帳の条件付き UPDATE（claim）を先に実行し、成功した場合のみ物理削除する。
        # claim は expires_at<=now() / status='uploaded' / owner not disabled を再検証する
        # （SELECT→UPDATE のギャップを排除し、再アップロード/無効化の競合を閉鎖）。
        try:
            claimed = store.claim_workspace_file_expired(fid)
        except Exception as e:
            _log.warning("sweep_expired: ledger claim failed uid=%s fid=%s: %s", uid, fid, e)
            failed += 1
            continue                # DB 障害 = fail-closed（物理削除しない）

        if claimed is None:
            # 条件不成立（再アップロード・無効化・タイムゾーン境界等）= skip。
            _log.debug("sweep_expired: claim not matched uid=%s fid=%s (already handled or disabled)",
                       uid, fid)
            continue

        # claim 成功 → advisory lock を取得してから物理削除する。
        # workspace_file_lock は upload と sweep を (uid, rel_path) 単位で直列化する
        # Postgres advisory lock。upload も同じ lock を取るため、
        # 「claim → unlink」と「write → DB upsert」が同時実行されない（TOCTOU 完全閉鎖）。
        try:
            with store.workspace_file_lock(uid, rel_path):
                # lock 内で DB を再確認: lock 取得前に re-upload が完了していた場合は skip。
                if not store.no_live_upload_for_path(uid, rel_path):
                    _log.warning(
                        "sweep_expired: re-upload detected under lock, skipping uid=%s rel=%s",
                        uid, rel_path)
                    failed += 1
                    continue

                p = Path(original_path)
                files_dir = _USERS_DIR.resolve() / uid / "workspace" / "files"
                # symlink 脱出防止: resolve 前の raw パスで is_symlink() を確認する。
                if p.is_symlink():
                    _log.warning("sweep_expired: symlink rejected uid=%s rel=%s", uid, rel_path)
                    failed += 1
                    continue
                # base-confined: resolve して files_dir 配下に収まることを確認してから削除。
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
    """台帳に無い物理ファイル（孤児）を best-effort で掃除（W4 補完・[[reconcile]] と同思想）。

    安全設計（_sweep_expired_workspace と同一）:
    - DB 不可 / user 取得不可 なら触らない（fail-safe）。
    - 無効化ユーザー（users.status='disabled'）の領域は保持（掃除しない）。
    - files_dir 配下のみ・symlink は触らない・advisory lock で upload と直列化。
    - 台帳の live rel_path 集合（status='uploaded'）に無い物理ファイルのみ削除（ledger=真実源）。
      lock 内で no_live_upload_for_path を再確認するので、live 集合が古くても誤削除しない。
    - 共有 RAG（ES/Neo4j）には一切触れない（personal_workspace_files 台帳のみ）。
    """
    base = _USERS_DIR.resolve()
    if not base.is_dir():
        return {"skipped": "no_users_dir"}
    deleted, failed = 0, 0
    for udir in base.iterdir():
        if udir.is_symlink() or not udir.is_dir():
            continue
        uid = udir.name
        # RV HIGH: 親コンポーネント（workspace/・files/）の symlink を拒否し、
        #   files_dir の実体が udir 配下に収まることを確認してから触る（symlink 先の外部削除を防ぐ）。
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
            u = store.get_user(uid)                 # DB 不可 → 例外 → この uid は触らない（fail-safe）
        except Exception as e:
            _log.warning("gc_orphan: user lookup failed uid=%s: %s", uid, e)
            continue
        # RV BLOCKER: user 不明（None）は絶対に削除しない（fail-safe）。disabled も保持。
        if u is None or u.get("status") == "disabled":
            continue
        try:
            live = store.live_workspace_rel_paths(uid)   # DB 不可 → 例外 → 触らない
        except Exception as e:
            _log.warning("gc_orphan: live rel paths failed uid=%s: %s", uid, e)
            continue
        for p in sorted(files_dir.iterdir()):       # files/ 直下（rel_path = ファイル名）
            try:
                if p.is_symlink() or not p.is_file():
                    continue
                rel = p.name
                if rel in live:
                    continue                        # 台帳に生きている = 正規ファイル（保持）
                with store.workspace_file_lock(uid, rel):
                    if not store.no_live_upload_for_path(uid, rel):
                        continue                    # lock 中に upload 検出 = 保持（TOCTOU 閉鎖）
                    # RV MEDIUM: lock 内で user 状態を再確認（scan 後に disabled 化された場合は保持）。
                    try:
                        u2 = store.get_user(uid)
                    except Exception:
                        continue                    # DB 不可 = fail-safe
                    if u2 is None or u2.get("status") == "disabled":
                        continue
                    try:
                        p.resolve().relative_to(files_dir.resolve())   # base-confined 再確認
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
    """R1b（会話継続・Codex ネイティブ resume・決定5）: 会話ごとの Codex resume セッション
    （`workspace/.codex-sessions/{cid}`）の TTL 掃除。

    保持日数は admin 設定 `codex_session_retention_days`（system_settings・既定 0=無制限）。
    0 以下（未設定含む）ならスイープしない。判定はディレクトリ自体の mtime（`CodexProvider` が
    毎ターン `config.toml` をこの直下に作り直すため、実行するたびに更新される＝最終利用時刻の
    近似として十分・DB 台帳は持たない＝`_gc_orphan_workspace_files` と同じ「fs 実体が真実源」思想）。
    安全設計（既存 workspace TTL sweep と同一）:
    - system_settings 取得不可・users_dir 不在なら一切削除しない（fail-safe）。
    - symlink は触らない（is_symlink() 事前チェック）。
    - 削除は `.codex-sessions/{cid}` ディレクトリ配下に閉じ込め確認（relative_to）してから行う。
    - 共有 RAG（ES/Neo4j）・conversations 行には一切触れない（セッション実体のみ）。
    """
    try:
        retention_days = int(store.get_system_settings().get("codex_session_retention_days") or 0)
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
                    continue                                  # 保持期間内＝まだ resume 対象として残す
                cdir.resolve().relative_to(sessions_root_resolved)   # base-confined 再確認
                shutil.rmtree(cdir, ignore_errors=False)
                deleted += 1
            except Exception as e:
                _log.warning("sweep_expired_codex_sessions: failed uid=%s cid=%s: %s", udir.name, cdir.name, e)
                failed += 1
    if deleted or failed:
        _log.info("sweep_expired_codex_sessions: deleted=%d failed=%d", deleted, failed)
    return {"deleted": deleted, "failed": failed}


def _sweep_expired_announcements() -> dict:
    """掲載終了日時（expire_at）を過ぎたお知らせを物理削除する（S4・掲示板の公開/削除タイマー）。

    workspace TTL sweep と同じポーラループに相乗り。RV2（2026-07・TOCTOU 対策）: 削除は
    `store.delete_expired_announcements()`（DELETE 文自体に条件を持たせ、削除の瞬間に各行の
    最新状態を再評価する）に一本化した。以前は「列挙 → 各 id を無条件削除」だったため、
    列挙〜削除の間に admin が expire_at を延長/クリアした行まで巻き添えで消えてしまう競合があった。
    sweep が走る前でも一覧クエリの条件（`list_announcements`）で既に見えなくなっているため、
    削除が多少遅れても実害は無い（fail-safe）。
    監査は **fail-open**（sweep 自体は止めない・書けなくても削除は成功のまま）にする点が
    announcement.created/updated/deleted の通常 CRUD（fail-closed・監査できなければ変更ごと取消す）
    と異なる: あちらは admin が同期 HTTP で待っている操作なので取消して 500 を返せるが、
    こちらは背景ポーラの自動処理なので、監査が書けないからといって期限切れ表示を復活させる方が
    かえって奇妙（`_sweep_expired_workspace` と同じ「背景処理は best-effort」の流儀に倣う）。
    """
    try:
        rows = store.delete_expired_announcements()   # DB 不可なら例外 → 呼出元が握って skip（fail-safe）
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
                                     # RV4: 削除の根拠（掲載期間）を監査に残す。
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
    """W4/S4/R1b: 期限切れ TTL 掃除＋孤児 GC＋掲示板タイマーの自動削除＋Codex resume セッションの
    保持期限掃除を順に best-effort 実行（startup／poll ループ共通）。"""
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


def _sweep_expired_on_startup():
    """起動時に期限切れ workspace ファイル掃除＋孤児 GC（W4・別スレッド・best-effort）。lifespan が起動時に呼ぶ。"""
    import threading

    def _run():
        try:
            _run_workspace_maintenance()
        except Exception:
            pass  # startup を止めない

    threading.Thread(target=_run, daemon=True, name="sherpa-ws-ttl").start()


# 運営掲示板（GET/POST/PATCH/DELETE /announcements*）・全体設定（GET/PUT /admin/settings）・
# 外部APIキー（POST/GET/DELETE /ext/v1/admin/keys）は sherpa/routers/system_extras.py へ移動済み
# （extras_router の include は healthz_router include の直後・上記参照）。

# ===== 画面（M7）: web/ を /ui で配信し、/ は /ui へ =====
_WEB = Path(__file__).resolve().parents[1] / "web"


class _CachedStaticFiles(StaticFiles):
    """`/ui` 配信に Cache-Control を付与する（S3・2026-07・実ユーザー再報告「保存バーが見えない」対策）。

    素の `StaticFiles` は Cache-Control を付けない＝ブラウザのヒューリスティックキャッシュに委ねられ、
    デプロイでサーバ側の html/css/js を直しても旧アセットが表示され続けることがあった。html/css/js は
    毎回サーバへ確認させる（`no-cache`＝ETag 等での再検証は残るので帯域は無駄にしない・304 で軽い）。
    フォント（woff2）は内容がほぼ不変なので長期キャッシュ可。
    """
    def file_response(self, full_path, stat_result, scope, status_code=200):
        resp = super().file_response(full_path, stat_result, scope, status_code)
        resp.headers["Cache-Control"] = ("public, max-age=31536000, immutable"
                                         if str(full_path).endswith(".woff2") else "no-cache")
        return resp


class _ManualSrcStaticFiles(_CachedStaticFiles):
    """docs/manual/*.md と manifest.json だけを読み取り専用配信する（M-A・マニュアル一本化）。

    manual.js が正本 Markdown をその場でレンダリングするための配信元。`StaticFiles` はディレクトリ
    総取りなので、README.md・images/（既に /ui/manual-images で別配信）まで見えてしまう。ここでは
    「トップレベル・拡張子 .md（README.md を除く）または manifest.json のみ」を許可し、それ以外は
    404 にする（サブパス＝トラバーサル試行も `/` を含む時点で弾かれる）。
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


# / (ルート) は sherpa/routers/system.py へ移動済み（root_router を include_router）。
app.include_router(system.root_router)


if _WEB.is_dir():
    # 使い方（manual.html）が参照する画面キャプチャを読み取り専用で配信する。画像の実体は
    # web/ の外（docs/manual/images/）にあるため専用マウント。StaticFiles がパストラバーサルを
    # 遮断する（ディレクトリ外への `..` は 404）。`/ui` の総取りより先に登録して優先させる。
    _MANUAL_IMAGES = Path(__file__).resolve().parents[1] / "docs" / "manual" / "images"
    if _MANUAL_IMAGES.is_dir():
        app.mount("/ui/manual-images",
                  _CachedStaticFiles(directory=str(_MANUAL_IMAGES)), name="manual-images")
    # マニュアル一本化（M-A）: 正本 docs/manual/*.md を manual.js がその場でレンダリングするための
    # 読み取り専用配信。.md（README.md 除く）と manifest.json のみ許可（_ManualSrcStaticFiles）。
    _MANUAL_SRC = Path(__file__).resolve().parents[1] / "docs" / "manual"
    if _MANUAL_SRC.is_dir():
        app.mount("/ui/manual-src",
                  _ManualSrcStaticFiles(directory=str(_MANUAL_SRC)), name="manual-src")
    app.mount("/ui", _CachedStaticFiles(directory=str(_WEB), html=True), name="ui")
