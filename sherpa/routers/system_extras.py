"""システム系の追加エンドポイント（ヘルス・運営掲示板・全体設定・外部APIキー）。
`GET /health/summary`・`GET /admin/health`・`GET /announcements`・`POST /admin/announcements`・`PATCH /admin/announcements/{id}`・`DELETE /admin/announcements/{id}`・`GET /admin/settings`・`PUT /admin/settings`・`POST/GET/DELETE /ext/v1/admin/keys`・`POST /ext/v1/admin/keys/recover` の 12 ルートと、利用者本人の API キー自己発行/一覧/失効/回復 `POST/GET/DELETE /ext/v1/keys`・`POST /ext/v1/keys/recover`（Cookie 認証・admin 不要）の 4 ルート。
api.py は `sherpa.routers.system` の `healthz_router` の直後・`root_router` の前に `app.include_router(system_extras.extras_router)` を 1 回だけ置く（ルート表 golden の定義順を保つため）。`_sweep_expired_announcements` と `_auth_bootstrap_on_startup` は lifespan 起動処理のため api.py に残る。
`sherpa.api` を import しない。
設計: docs/design/settings.md「管理画面（システム管理）の設定」
"""
from __future__ import annotations

import logging
import secrets
import uuid
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field, StrictBool, StrictInt, field_validator

from sherpa import (
    chat_examples,
    depth_profile,
    ext_api,
    health,
    model_catalog,
    notifications,
    required_tools,
    simple_chat,
    store,
    webhooks,
    workspace_limits,
    worlds,
)
from sherpa.agents import _web_search_admin_allowed
from sherpa.providers.codex import sandbox as codex_sandbox
from sherpa.deps import _current_user, _require_admin
from sherpa.schemas import (
    AnnouncementMutateResponse,
    AnnouncementsListResponse,
    ExtKeyCreatedResponse,
    ExtKeyListResponse,
    ExtKeyRecoverResponse,
    ExtKeyRevokeResponse,
    HealthSummaryResponse,
    SettingsTestResponse,
)

_log = logging.getLogger("sherpa")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
extras_router = APIRouter()

_ANNOUNCEMENT_CATEGORIES = ("maintenance", "case", "notice")

# 会話ごとの Codex resume セッションの保持日数の既定。未設定（None）はこの既定日数にフォールバックし、`0` は明示的な「無制限」（未設定と 0 を区別する・`effective_codex_session_retention_days`）。
CODEX_SESSION_RETENTION_DAYS_DEFAULT = 30


def effective_codex_session_retention_days(system_settings: dict | None) -> int:
    """`codex_session_retention_days` の実効値（`api._sweep_expired_codex_sessions` と `GET /admin/settings` の表示が呼ぶ唯一の判定）。未設定（None・キー欠落）は `CODEX_SESSION_RETENTION_DAYS_DEFAULT`、明示的な `0` だけが無制限。壊れた保存値（負値・非 int）は既定日数に倒す。"""
    if not isinstance(system_settings, dict):
        return CODEX_SESSION_RETENTION_DAYS_DEFAULT
    raw = system_settings.get("codex_session_retention_days")
    if raw is None:
        return CODEX_SESSION_RETENTION_DAYS_DEFAULT
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return CODEX_SESSION_RETENTION_DAYS_DEFAULT
    if value < 0:
        return CODEX_SESSION_RETENTION_DAYS_DEFAULT
    return value


def _effective_codex_worker_model(sysset: dict) -> str:
    """`GET /admin/settings` の `codex_worker_model.effective`。
    `model_catalog.resolve_model("codex","codex",...)` は内部で `llm.openai_endpoint_kind()` を通り、保存済み `openai_base_url` が壊れた値だと `ValueError` を送出する。表示専用のこの経路で `GET /admin/settings` を 500 にしないため、解決できなければ固定フォールバック（`_CODEX_WORKER_MODEL_FALLBACK`）に倒す。
    Codex(Ollama) 構成は利用者ごとの設定でシステム側から判定できないため、この値は Codex(OpenAI 系) の実効値（Ollama 構成の利用者は本体と同じモデルタグになる）。
    """
    try:
        main_model = model_catalog.resolve_model("codex", "codex", None, system_settings=sysset)
        return codex_sandbox._codex_worker_model(sysset, main_model=main_model)
    except (ValueError, TypeError):
        return codex_sandbox._codex_worker_model(sysset)


class AnnouncementCreateReq(BaseModel):
    title: str
    body: str
    category: str = "notice"
    pinned: bool = False
    published: bool = True
    publish_at: str | None = None  # ISO 8601 文字列。省略/空文字＝今すぐ公開扱い（NULL）。
    expire_at: str | None = None  # ISO 8601 文字列。省略/空文字＝無期限掲載（NULL）。


class AnnouncementPatchReq(BaseModel):
    title: str | None = None
    body: str | None = None
    category: str | None = None
    pinned: bool | None = None
    published: bool | None = None
    # 書込専用キーと同じ扱い: 未指定(None)は変更しない・""は NULL へクリア・それ以外は ISO 8601 文字列として更新する。
    publish_at: str | None = None
    expire_at: str | None = None


class _ModelCatalogCellReq(BaseModel):
    """`SystemSettingsReq.model_catalog[provider][usage]` の 1 セル（allowed/default）。"""
    allowed: list[str] = []
    default: str = ""


class SystemSettingsReq(BaseModel):
    """全体設定（system_settings）の部分更新リクエスト（管理者のみ）。未指定のキーは変更せず、明示的に `null` を送るとそのキーを未設定へ戻す（コードの既定へ戻る）。"""
    arms_enabled: list[str] | None = None  # 有効アーム名（既知名のみ）。空/未指定は既定へ。
    legacy_backend: str | None = None  # 旧形式変換バックエンド: none|libreoffice|office_com。null は既定へ。
    # rag.md の LLM 成形トグル。on|off。null は既定 off へフォールバックする（`legacy_backend` と同型）。
    rag_llm_render: str | None = None
    vlm: dict | None = None  # 視覚読み取りの VLM 設定 {provider,model,cloud_allowed}。null は既定へ。
    # Ollama 接続先の SSRF allowlist（host:port の配列）。未設定(None)は loopback のみ許可（`llm.assert_ollama_url_allowed`）。null は未設定へ戻す。
    ollama_allowlist: list[str] | None = None
    # Webhook 宛先の SSRF allowlist（host:port の配列）。`ollama_allowlist` と違い loopback も例外にせず、未設定(None)は全拒否（`webhooks.assert_webhook_url_allowed`）。null は未設定へ戻す。
    webhook_allowlist: list[str] | None = None
    # 会話ごとの Codex resume セッション（workspace/.codex-sessions/{cid}）の保持日数。未設定(None)は `CODEX_SESSION_RETENTION_DAYS_DEFAULT`（30日）、明示的な 0 は「無制限」。null は未設定へ戻す。
    # `StrictInt` で bool/文字列からの暗黙変換を拒否する（`codex_web_search` に `StrictBool` を使うのと同じ理由）。
    codex_session_retention_days: StrictInt | None = None
    # クラウド AI プロバイダの中央設定。`cloud_provider` は openai だけ選べる（既定 openai・gemini/bedrock は 422）。`openai_api_key` は中央で保管する資格情報（`sherpa.keys.resolve_api_key` が唯一の真実源）で、個人設定（`user_settings`）とは別物。`ollama_url` は中央の既定値（個人設定の ollama_url が優先）。`personal_api_keys_allowed` は個人キーを許すかの唯一のスイッチ（既定 false）。
    cloud_provider: str | None = None
    personal_api_keys_allowed: StrictBool | None = None
    # Codex の Web 検索を許可するか（既定 false）。ON の間だけ調べ方ブロックの「Web 検索」行を表示し、チャットごとの希望（`ChatReq.web_search`）を尊重する（`sherpa/providers/codex/sandbox.py::_web_search_admin_allowed` が唯一の読み手）。
    web_search_allowed: StrictBool | None = None
    # 利用者本人による外部連携 API キーの自己発行を許可するか（既定 false）。OFF に戻すたび（冪等）に利用者発行キーを一括失効する（設定変更と同一トランザクション・`store.apply_system_settings_and_revoke_if_disabled`）。
    user_api_keys_allowed: StrictBool | None = None
    # 自己発行キーの 1 日あたりの呼び出し上限（既定/上限を兼ねる）。未指定は組み込みの既定値（`store.SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK`）。利用者は発行時にこれ以下の値だけ指定できる。admin 発行キーは対象外。
    user_api_keys_daily_quota_default: StrictInt | None = Field(default=None, ge=1, le=1_000_000)
    # 簡易回答に使う AI のプロバイダ（"ollama"/"openai"）。チャットの簡易と外部 API（POST /ext/v1/answer）が共通で読む（管理画面の表示名は「簡易回答に使う AI」）。未設定(None)は "ollama"。`sherpa.simple_chat.resolve_model_and_provider` が読む。
    research_default_provider: str | None = None
    openai_api_key: str | None = None
    ollama_url: str | None = None
    # OpenAI 互換 API の接続先（ラジオ・本家以外のときの base URL・認証ヘッダ形式・API バージョン）。null は未設定へ戻す。意味検証・実効値の解決は `sherpa/llm.py` が唯一の真実源。
    openai_endpoint_kind: str | None = None  # openai(既定)/azure/custom。
    openai_base_url: str | None = None
    openai_auth_header: str | None = None  # bearer(既定)/api-key。
    openai_api_version: str | None = None
    # モデルカタログ（プロバイダ×用途ごとの「選べるモデル一覧＋既定」）。null は未設定へ戻す（組み込み既定のみ）。セル形状（allowed/default）は pydantic 型で表現し、provider/usage 名の妥当性と default∈allowed の補正は `sherpa.model_catalog.validate_catalog` が行う。
    model_catalog: dict[str, dict[str, _ModelCatalogCellReq]] | None = None
    # Ollama の許可ホスト一覧は既存の `ollama_allowlist` を使う。
    # 調べる深さ（標準/深く/最大）が掛ける倍率の基準値（標準時の値）。未指定(None)は各モジュールのコード既定値（`sherpa/depth_profile.py::BASE_SETTINGS_KEYS` が対応する定数を列挙）。null は未設定へ戻す。倍率表自体は固定でここでは編集しない。
    depth_base_grep_max_hits: StrictInt | None = Field(default=None, ge=1, le=1000)
    depth_base_qa_max_hits: StrictInt | None = Field(default=None, ge=1, le=1000)
    depth_base_read_window: StrictInt | None = Field(default=None, ge=10, le=400)
    depth_base_impact_depth: StrictInt | None = Field(default=None, ge=1, le=64)
    depth_base_troubleshoot_depth: StrictInt | None = Field(default=None, ge=1, le=16)
    depth_base_codex_reasoning: str | None = None
    # 埋め込み HTTP の同時送信数（`sherpa.embeddings.embed()` の有界スレッドプール）。未指定(None)は `embeddings.EMBED_PARALLEL_DEFAULT`（4）。null は未設定へ戻す。
    embed_parallel: StrictInt | None = Field(default=None, ge=1, le=16)
    # 埋め込みの接続先。"auto"（回答用に選んだクラウドに従う・既定）／"ollama"（常に中央 Ollama）。null は未設定（既定 auto）へ戻す。回答側の cloud_provider とは独立。
    embed_provider: str | None = None
    # 「最大」の深さが許す査読の巡数（`depth_profile.review_rounds_for`）。クイック 0・標準 2・深く 4 は固定で、設定はこの 1 項目だけ。未指定(None)は `depth_profile.MAX_REVIEW_ROUNDS_DEFAULT`（7）。null は未設定へ戻す。
    max_review_rounds: StrictInt | None = Field(
        default=None, ge=depth_profile.MAX_REVIEW_ROUNDS_MIN, le=depth_profile.MAX_REVIEW_ROUNDS_MAX)
    # multi_agent の worker モデル（`sherpa.providers.codex.sandbox._codex_worker_model`）。未指定(None)は `_CODEX_WORKER_MODEL_FALLBACK`。空文字・null は未設定へ戻す。
    codex_worker_model: str | None = None
    # 素の Codex モード。"standard"（既定・Sherpa の検索ツールと調査台帳を使う）／"plain"（Codex が自分で資料を読む・試験用）。未指定(None)は既定 "standard"。
    # 設計: docs/design/codex.md「素の Codex モード」
    codex_mode: str | None = None
    # チャット同時実行の上限（背景実行の受付・超過は 429・`sherpa/chat_turns.py::effective_limits`）。未指定(None)はコード既定値（`chat_turns.MAX_TURNS_PER_USER`/`MAX_TURNS_GLOBAL`）。null は未設定へ戻す。
    chat_max_turns_per_user: StrictInt | None = Field(default=None, ge=1, le=16)
    chat_max_turns_global: StrictInt | None = Field(default=None, ge=1, le=64)
    # Codex の MCP ツール結果 1 件あたりのバイト予算。未指定(None)はコード既定（262144・`agentic_search.effective_tool_result_max_bytes()` が唯一の解決点）。null は未設定へ戻す。撤去済みの旧キー（`agentic_max_tools_per_turn`/`agentic_budget_total`/`depth_base_max_turns`）は送られても無視する。
    agentic_budget_per_result: StrictInt | None = Field(default=None, ge=1024, le=8 * 1024 * 1024)
    # 個人ファイル（workspace）の 1 件あたりの上限（バイト）と保持日数（0 は無期限）。未指定(None)は `workspace_limits` の既定（10 MiB・90 日）。null は未設定へ戻す。
    workspace_max_bytes: StrictInt | None = Field(
        default=None, ge=workspace_limits.MAX_BYTES_MIN, le=workspace_limits.MAX_BYTES_MAX)
    workspace_ttl_days: StrictInt | None = Field(
        default=None, ge=workspace_limits.TTL_DAYS_MIN, le=workspace_limits.TTL_DAYS_MAX)
    # チャット画面のクイック入力例（ウェルカム画面のチップ）のカスタマイズ `{enabled, items}`。意味検証は `sherpa.chat_examples.validate`（`_validate_chat_examples` が 422 へ変換）。null は未設定へ戻す（既定＝表示・組み込み 4 例）。
    chat_examples: dict | None = None


def _normalize_world_list_field(v: list[str] | None) -> list[str] | None:
    """`allowed_worlds` の形式検証（識別子として妥当か・重複除去）。`ExtKeyCreateReq`・`ExtSelfKeyCreateReq` の field_validator が共用する（実在検証は各ハンドラ側）。None（未指定）はそのまま返す＝全 world 許可。"""
    if v is None:
        return v
    for w in v:
        if not worlds.valid_world(w):
            raise ValueError(f"world 識別子が不正です: {w}")
    seen, out = set(), []  # 重複除去（順序維持）。
    for w in v:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# daily_quota の上限。DB の CHECK 制約より先に 422 にして、整数オーバーフロー起因の 500 を防ぐ。
_DAILY_QUOTA_MAX = 1_000_000

def _validate_client_op_id_format(v: str) -> str:
    """`client_op_id` を UUID として解析し、標準の小文字正準形（8-4-4-4-12・ハイフン区切り）へ正規化する。DB の非NULL部分一意制約（`api_keys_client_op_id_unique`）と組み合わせて「この 1 回の発行操作」を一意に指すため、任意の自由文字列は受け付けず、大小文字違いも正準形に揃えて一意制約と回復時の照合を迂回できないようにする。"""
    try:
        return str(uuid.UUID(v))
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError("client_op_id は UUID 形式（例: 123e4567-e89b-12d3-a456-426614174000）で"
                         "指定してください") from e


class ExtKeyCreateReq(BaseModel):
    """外部連携 API キーの発行リクエスト（/ext/v1・admin のみ）。"""
    label: str = Field(min_length=1, max_length=100)
    # world スコープ（オプトイン）。未指定/null＝全 world 許可、空リスト＝どの world にもアクセスできないキー。形式検証はここ、実在検証は `ext_key_create` 側。
    allowed_worlds: list[str] | None = None
    # 有効期限（ISO 8601 文字列）と日次クォータ（任意・1〜_DAILY_QUOTA_MAX の整数）。いずれも省略/null＝無期限・無制限。
    expires_at: str | None = None
    daily_quota: StrictInt | None = Field(default=None, ge=1, le=_DAILY_QUOTA_MAX)
    # 発行 UI が生成する相関トークン（任意・秘密ではない・UUID 形式のみ）。POST 応答が失われた場合に、UI が回復エンドポイント（`ext_key_recover`）でこの値を照合して自動失効する。
    client_op_id: str | None = Field(default=None, max_length=100)
    # Webhook 通知の宛先 URL（取り込み run の terminal 化を受け取る）。未指定/null＝Webhook 無効。意味検証（http/https・宛先ポリシー）は `ext_key_create` 側（`webhooks.assert_webhook_url_allowed`）。
    webhook_url: str | None = Field(default=None, max_length=2048)

    @field_validator("allowed_worlds")
    @classmethod
    def _v_allowed_worlds(cls, v):
        return _normalize_world_list_field(v)

    @field_validator("client_op_id")
    @classmethod
    def _v_client_op_id(cls, v):
        return v if v is None else _validate_client_op_id_format(v)

    @field_validator("webhook_url")
    @classmethod
    def _v_webhook_url(cls, v):
        return v.strip() if isinstance(v, str) and v.strip() else None


class ExtSelfKeyCreateReq(BaseModel):
    """利用者本人による外部連携 API キー発行のリクエスト。`system_settings.user_api_keys_allowed` が true のときのみ受理される。"""
    label: str = Field(min_length=1, max_length=100)
    # 本人がアクセスできる範囲の部分集合に強制する（`_enforce_self_world_scope` が `worlds.accessible_world_ids` で検証）。ここでは形式検証のみ（`ExtKeyCreateReq` と同型）。
    allowed_worlds: list[str] | None = None
    expires_at: str | None = None
    # 未指定/null は管理者の現在の既定を適用し、指定値が現在の上限を超える場合は 422 にする（確定は `store.insert_api_key` がロック内で DB から再読して行う・ここでは型検証のみ）。
    daily_quota: StrictInt | None = Field(default=None, ge=1, le=_DAILY_QUOTA_MAX)
    client_op_id: str | None = Field(default=None, max_length=100)
    # `ExtKeyCreateReq.webhook_url` と同型（自己発行キーにも使える）。
    webhook_url: str | None = Field(default=None, max_length=2048)

    @field_validator("allowed_worlds")
    @classmethod
    def _v_allowed_worlds(cls, v):
        return _normalize_world_list_field(v)

    @field_validator("webhook_url")
    @classmethod
    def _v_webhook_url(cls, v):
        return v.strip() if isinstance(v, str) and v.strip() else None

    @field_validator("client_op_id")
    @classmethod
    def _v_client_op_id(cls, v):
        return v if v is None else _validate_client_op_id_format(v)


class ExtKeyRecoverReq(BaseModel):
    """曖昧な発行結果（POST 応答が届かなかった等）の回復リクエスト。"""
    client_op_id: str = Field(min_length=1, max_length=100)

    @field_validator("client_op_id")
    @classmethod
    def _v_client_op_id(cls, v):
        return _validate_client_op_id_format(v)


@extras_router.get("/health/summary", tags=["システム"], response_model=HealthSummaryResponse)
def health_summary(request: Request):
    """バックエンド健全性のサマリを返す（全画面の状態ドット用・ログイン必須）。Postgres 停止時にも例外にならず down を返す（未ログインは 401）。"""
    try:
        _current_user(request)
    except HTTPException:
        raise
    except Exception:
        return {"status": "down",
                "checked_at": datetime.now(timezone.utc).isoformat()}
    return health.summary()


@extras_router.get("/admin/health", tags=["システム"])
def admin_health(request: Request, refresh: bool = False):
    """コンポーネント別の健全性詳細を返す（システム状態画面用・admin 専用）。
    認証 DB に到達できないときは詳細（DSN 等）を返さず 503 のみ返す。AI（openai/ollama/codex）は、この管理者本人が設定画面で入れた API キーも含めて実際に 1 回だけ接続確認した結果を返し、登録 world への ES/グラフ検索テストも含む（結果は利用者ごとに 60 秒キャッシュされ、「再チェック」は `force=True` で最新化する）。
    """
    try:
        u = _require_admin(_current_user(request))
    except HTTPException:
        raise
    except Exception:
        raise HTTPException(
            503,
            "認証データベース（PostgreSQL）に到達できないため、詳細を取得できません。"
            "make up でストアの復旧を確認してください。",
        )
    data = health.snapshot(force=refresh)
    ai_ids = {"openai", "ollama", "codex"}
    ai_rows = health.ai_snapshot(u["uid"], store.get_settings(u["uid"]), force=refresh)
    search_rows = health.search_snapshot(u["uid"], force=refresh)
    components = [c for c in data["components"] if c["id"] not in ai_ids] + ai_rows + search_rows
    return {**data, "components": components}


@extras_router.get("/notifications", tags=["システム"])
def notifications_list(request: Request):
    """非同期処理の完了/要対応の通知を返す（ホーム画面「通知」区画用・ログイン必須）。
    誰でも取り込み run の完了/失敗が見える。admin はさらにグラフ drift・LLM 成形完了・OCR 反映待ちも見える。自分が所有する共有の期限が 7 日以内に来る場合は本人にだけ見える。既読管理はせず、毎回現在の状態から組み立てる。
    """
    u = _current_user(request)
    return {"notifications": notifications.list_notifications(is_admin=u.get("role") == "admin", uid=u["uid"])}


# 運営掲示板（公開/削除タイマー）。

def _parse_announcement_dt(value: str | None, field_label: str) -> datetime | None:
    """publish_at/expire_at の入力（ISO 8601 文字列）をパースする。空/未指定は None。naive（tzinfo 無し）は UTC 扱いに統一し（`ShareCreateReq.expires_at` と同じ）、不正な形式は 422。"""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        raise HTTPException(422, f"{field_label}の形式が不正です（ISO 8601 で指定してください）")
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _announcement_status(row: dict, now: datetime) -> str:
    """admin 向けの状態バッジ用: unpublished / scheduled（予約公開待ち）/ expired（掲載終了）/ active（公開中）。`now` は呼び出し側が 1 回だけ計算して渡す（一覧は同一 now で揃える）。"""
    if not row["published"]:
        return "unpublished"
    pub_at, exp_at = row.get("publish_at"), row.get("expire_at")
    if pub_at and pub_at > now:
        return "scheduled"
    if exp_at and exp_at <= now:
        return "expired"
    return "active"


def _announcement_out(row: dict, now: datetime) -> dict:
    return {
        "id": row["id"], "author_uid": row["author_uid"], "title": row["title"],
        "body": row["body"], "category": row["category"], "pinned": row["pinned"],
        "published": row["published"], "publish_at": row.get("publish_at"), "expire_at": row.get("expire_at"),
        "status": _announcement_status(row, now),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


@extras_router.get("/announcements", tags=["運営掲示板"], response_model=AnnouncementsListResponse)
def announcements_list(request: Request, limit: int = Query(20, ge=1, le=100), offset: int = Query(0, ge=0),
                       include_unpublished: bool = Query(False)):
    """お知らせ一覧を返す（ログイン必須・既定は公開済みのみ・ピン留め優先→新着順）。`include_unpublished=true` は admin 専用（非 admin が指定すると 403）。"""
    u = _current_user(request)  # ログイン必須（auth 有効時）。
    if include_unpublished:
        _require_admin(u)
    rows = store.list_announcements(limit=limit, offset=offset, published_only=not include_unpublished)
    now = datetime.now(timezone.utc)  # 全行で同一の now を使う。
    return {"announcements": [_announcement_out(r, now) for r in rows]}


@extras_router.post("/admin/announcements", tags=["運営掲示板"], response_model=AnnouncementMutateResponse)
def announcement_create(req: AnnouncementCreateReq, request: Request):
    """お知らせを新規作成する（管理者のみ）。監査は fail-closed: 書けなければ作成を取り消して 500 を返す。"""
    u = _current_user(request)
    _require_admin(u)
    title = (req.title or "").strip()
    body = (req.body or "").strip()
    if not title:
        raise HTTPException(422, "タイトルは必須です")
    if not body:
        raise HTTPException(422, "本文は必須です")
    if req.category not in _ANNOUNCEMENT_CATEGORIES:
        raise HTTPException(422, "category は maintenance / case / notice のみ")
    publish_at = _parse_announcement_dt(req.publish_at, "公開日時")
    expire_at = _parse_announcement_dt(req.expire_at, "掲載終了日時")
    if publish_at and expire_at and publish_at > expire_at:
        raise HTTPException(422, "公開日時は掲載終了日時より前にしてください")
    row = store.create_announcement(u["uid"], title, body, category=req.category,
                                    pinned=req.pinned, published=req.published,
                                    publish_at=publish_at, expire_at=expire_at)
    try:
        store.audit(u["uid"], "announcement.created", "announcement", f"announcement:{row['id']}",
                    after_state={"title": title, "category": req.category, "published": req.published,
                                 "publish_at": publish_at.isoformat() if publish_at else None,
                                 "expire_at": expire_at.isoformat() if expire_at else None},
                    outcome="success", severity="info")
    except Exception:
        _log.critical("audit write failed for announcement.created – deleting announcement %s (fail-closed)",
                      row["id"])
        try:
            store.delete_announcement(row["id"])
        except Exception:
            _log.critical("compensating delete also failed for announcement %s – manual cleanup required",
                          row["id"])
        raise HTTPException(500, "お知らせの作成中にエラーが発生しました")
    return {"ok": True, "announcement": _announcement_out(row, datetime.now(timezone.utc))}


@extras_router.patch("/admin/announcements/{id}", tags=["運営掲示板"], response_model=AnnouncementMutateResponse)
def announcement_patch(id: int, req: AnnouncementPatchReq, request: Request):
    """お知らせを部分更新する（管理者のみ）。`published=false` で非公開化。監査は fail-closed: 書けなければ更新前の状態へ復元して 500 を返す。"""
    u = _current_user(request)
    _require_admin(u)
    before = store.get_announcement(id)
    if not before:
        raise HTTPException(404, "お知らせが見つかりません")
    title = req.title.strip() if req.title is not None else None
    if title is not None and not title:
        raise HTTPException(422, "タイトルは空にできません")
    body = req.body.strip() if req.body is not None else None
    if body is not None and not body:
        raise HTTPException(422, "本文は空にできません")
    if req.category is not None and req.category not in _ANNOUNCEMENT_CATEGORIES:
        raise HTTPException(422, "category は maintenance / case / notice のみ")
    # publish_at/expire_at は書込専用キーと同じ扱い: 未指定(None)は kwarg 自体を渡さず（store 側の `_UNSET`＝変更しない）、""は明示的に None（NULL へクリア）として渡す。
    dt_kwargs = {}
    if req.publish_at is not None:
        dt_kwargs["publish_at"] = _parse_announcement_dt(req.publish_at, "公開日時")
    if req.expire_at is not None:
        dt_kwargs["expire_at"] = _parse_announcement_dt(req.expire_at, "掲載終了日時")
    # publish_at/expire_at の順序検証は、`update_announcement` 内の `SELECT...FOR UPDATE` で取得したロック済みの現在値に対して行う（並行 PATCH の競合を防ぐ）。ここでは呼ばず store 層へ委譲する。
    try:
        row = store.update_announcement(id, title=title, body=body, category=req.category,
                                        pinned=req.pinned, published=req.published, **dt_kwargs)
    except store.AnnouncementOrderError as e:
        raise HTTPException(422, str(e))
    if row is None:
        raise HTTPException(404, "お知らせが見つかりません")
    try:
        store.audit(u["uid"], "announcement.updated", "announcement", f"announcement:{id}",
                    before_state={"title": before["title"], "category": before["category"],
                                  "published": before["published"],
                                  "publish_at": before["publish_at"].isoformat() if before.get("publish_at") else None,
                                  "expire_at": before["expire_at"].isoformat() if before.get("expire_at") else None},
                    after_state={"title": row["title"], "category": row["category"],
                                 "published": row["published"],
                                 "publish_at": row["publish_at"].isoformat() if row.get("publish_at") else None,
                                 "expire_at": row["expire_at"].isoformat() if row.get("expire_at") else None},
                    outcome="success", severity="info")
    except Exception:
        _log.critical("audit write failed for announcement.updated – restoring announcement %s (fail-closed)", id)
        try:
            # updated_at まで含めて完全に before へ戻す（`update_announcement` は updated_at=now() を打つため使わない）。
            store.restore_announcement_state(id, before)
        except Exception:
            _log.critical("compensating restore also failed for announcement %s – manual cleanup required", id)
        raise HTTPException(500, "お知らせの更新中にエラーが発生しました")
    return {"ok": True, "announcement": _announcement_out(row, datetime.now(timezone.utc))}


@extras_router.delete("/admin/announcements/{id}", tags=["運営掲示板"])
def announcement_delete(id: int, request: Request):
    """お知らせを削除する（管理者のみ）。監査は fail-closed: 書けなければ id/created_at/updated_at を含めて削除前の状態へ復元し、500 を返す。"""
    u = _current_user(request)
    _require_admin(u)
    before = store.get_announcement(id)
    if not before:
        raise HTTPException(404, "お知らせが見つかりません")
    store.delete_announcement(id)
    try:
        store.audit(u["uid"], "announcement.deleted", "announcement", f"announcement:{id}",
                    before_state={"title": before["title"], "category": before["category"],
                                  "publish_at": before["publish_at"].isoformat() if before.get("publish_at") else None,
                                  "expire_at": before["expire_at"].isoformat() if before.get("expire_at") else None},
                    outcome="success", severity="info")
    except Exception:
        _log.critical("audit write failed for announcement.deleted – restoring announcement %s (fail-closed)", id)
        try:
            store.restore_announcement(before)
        except Exception:
            _log.critical("compensating restore also failed for announcement %s – manual cleanup required", id)
        raise HTTPException(500, "お知らせの削除中にエラーが発生しました")
    return {"ok": True}


# 全体設定（system_settings・admin のみ）。

_ENDPOINT_TEST_TIMEOUT_S = 10  # 接続テスト専用の短いタイムアウト（秒）。到達不能を素早く申告する。


def _admin_settings_view() -> dict:
    """GET/PUT 共通の応答。現行値（system_settings の生値）と実効値（既定込みの解決結果）を返す。`configured` は生値（未設定なら null）、`env_default`/`default` は未設定に戻したときの値で、UI の既定表示に使う。"""
    from sherpa import agentic_search, chat_service, chat_turns, embeddings, impact_service, keys, lens_service, llm
    from sherpa.ingest import arms as ingest_arms
    from sherpa.ingest import llm_render
    from sherpa.ingest.arms import legacy_convert, vision_arm
    sysset = store.get_system_settings()
    # `sysset["openai_base_url"]`/`sysset["openai_endpoint_kind"]` は JSONB のため非文字列の破損値がありうる。`llm.openai_endpoint_kind()`/`llm.openai_base_url()` は判定より先に型検査して不正なら `ValueError` を送出するため、両方を同じ try で解決する。管理画面が生値を確認・修正できるよう、表示は落とさず固定文字列へ倒す（`system.py::_INVALID_SAVED_BASE_URL_LABEL` と同じ）。`openai_auth_header_style()`/`openai_api_version()` も内部で `openai_endpoint_kind()` を呼ぶため同じ try に含める。
    try:
        eff_openai_kind = llm.openai_endpoint_kind(sysset)
        eff_openai_base_url = llm.openai_base_url(sysset)
        eff_openai_auth_header = llm.openai_auth_header_style(sysset)
        eff_openai_api_version = llm.openai_api_version(sysset)
    except ValueError:
        eff_openai_kind = "(不正な保存値)"
        eff_openai_base_url = "(不正な保存値)"
        eff_openai_auth_header = "(不正な保存値)"
        eff_openai_api_version = "(不正な保存値)"
    # `research_default_provider` も型検査を判定より先に行い ValueError を送出する。保存後に壊れた値があっても管理画面全体を 500 にしない。
    try:
        eff_research_default_provider = simple_chat.default_research_provider(sysset)
    except ValueError:
        eff_research_default_provider = "(不正な保存値)"
    return {
        # クラウド AI プロバイダの中央設定。key_set はキー値そのものではなく有無のみ。`provider` は現在の選択、`providers` は選べる値の一覧（画面の `<select>` 用）。
        "cloud": {
            "provider": keys.selected_cloud_provider(sysset),
            # 生の保存値（一度も PUT されていなければ None）。UI が「admin が実際に操作したか」を判別する（`provider` は既定込みの実効値のため区別できない）。
            "provider_raw": keys.cloud_provider_raw(sysset),
            "providers": list(keys.CLOUD_PROVIDERS),
            "personal_api_keys_allowed": keys.personal_keys_allowed(sysset),
            "openai_key_set": bool(sysset.get("openai_api_key")),
            "retired_provider": keys.retired_cloud_provider(sysset),
            "ollama_url": sysset.get("ollama_url") or keys.DEFAULT_OLLAMA_URL,
            # 個人秘密キーを保存中のユーザー数（`personal_api_keys_allowed` を OFF で保存すると一括削除される・保存前の確認ダイアログ用）。
            "personal_keys_in_use_count": store.count_users_with_personal_keys(),
            # Codex の Web 検索を管理者が許可しているか（既定 false）。チャットの調べ方ブロック（web/chat/menus.js）は、これと現在の頭脳（Codex＋OpenAI直結）が揃ったときだけ Web 検索行を表示する。
            "web_search_allowed": _web_search_admin_allowed(sysset),
        },
        # 利用者本人による外部連携 API キー自己発行の許可トグル（既定 false）。`self_issued_active_count` は OFF で保存する前の確認ダイアログ用（失効/期限切れは除く）。`daily_quota_default` は自己発行キーの 1 日あたり呼び出し上限の既定/上限（`configured`＝管理者の生値・`effective`＝未設定時のフォールバック込みの適用値・`default`＝組み込みのフォールバック値）。
        "ext_keys": {
            "user_api_keys_allowed": bool(sysset.get("user_api_keys_allowed") or False),
            "self_issued_active_count": store.count_self_issued_active_api_keys(),
            "daily_quota_default": {
                "configured": sysset.get("user_api_keys_daily_quota_default"),
                "effective": store.resolve_self_issued_daily_quota_cap(sysset),
                "default": store.SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK,
            },
            # 外部からの簡易回答（POST /ext/v1/answer）の既定 AI（`configured`＝管理者の生値・`effective`＝未設定時のフォールバック込みの適用値・`default`＝組み込みのフォールバック値）。
            "research_default_provider": {
                "configured": sysset.get("research_default_provider"),
                "effective": eff_research_default_provider,
                "default": "ollama",
            },
        },
        # OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換）。`configured` は admin が保存した生値（未設定なら None）、`effective` は `sherpa/llm.py` による解決結果。base URL は管理画面の入力欄そのものなので伏せない（個人設定の読み取り専用表示はホスト名のみ）。
        "openai_endpoint": {
            "configured": {
                "kind": sysset.get("openai_endpoint_kind"),
                "base_url": sysset.get("openai_base_url"),
                "auth_header": sysset.get("openai_auth_header"),
                "api_version": sysset.get("openai_api_version"),
            },
            "effective": {
                "kind": eff_openai_kind,
                "base_url": eff_openai_base_url,
                "auth_header": eff_openai_auth_header,
                "api_version": eff_openai_api_version,
            },
            "kinds": ["openai", "azure", "custom"],
            "auth_headers": ["bearer", "api-key"],
        },
        # 使えるモデル一覧＋用途別既定。管理画面は「選択中のクラウドプロバイダ＋Ollama＋Codex」の列だけを描く。`effective` は組み込み既定に管理者設定を重ねた解決結果（セル単位）、`configured` は管理者が保存した生値（未設定なら null）。
        "model_catalog": {
            "configured": sysset.get("model_catalog"),
            "effective": model_catalog.get_catalog(sysset),
            # 組み込み既定のみ（管理者設定を重ねない）。管理画面がセルの値が既定と異なるかを判定する基準。
            "builtin": model_catalog.get_catalog({}),
            "providers": list(model_catalog.PROVIDERS),
            "usages": list(model_catalog.USAGES),
        },
        "arms": {
            "known": ingest_arms.known_arm_names(),
            "enabled": ingest_arms.enabled_arm_names(),  # 実効（system_settings 反映済）。
            "configured": sysset.get("arms_enabled"),  # 全体設定の生値（未設定=None＝既定）。
            "env_default": ingest_arms.env_default_arm_names(),  # 未設定に戻したときの実効（既定）。
            "available": ingest_arms.arm_availability(),  # 各アームがこの端末で実際に使えるか（未導入案内用）。
        },
        "legacy_backend": {
            "configured": sysset.get("legacy_backend"),  # 生値（未設定=None＝既定に従う）。
            "effective": legacy_convert.legacy_backend_name(),  # system>既定（none|libreoffice|office_com）。
            "default": legacy_convert.env_default_backend(),  # 未設定に戻したときの実効（既定）。
            "options": list(legacy_convert.BACKEND_OPTIONS),  # 選択肢（none|libreoffice|office_com）。
            "libreoffice": {
                "available": legacy_convert.soffice_available(),  # soffice 検出の有無。
                "version": legacy_convert.soffice_version(),  # 検出時のバージョン（未検出は None）。
            },
            # office_com の到達性と動作形態。mode="direct"（同一マシン・URL 未設定かつ powershell 検出＝既定）｜"http"（別ホストのワーカー・URL 設定済み）｜"unavailable"（どちらも無し）。configured_url で「URL 未設定」と「設定済みだが不達」を UI が区別できる。versions は healthz の各 Office バージョン（不達/未検出なら None）。
            "office_com": {
                "configured_url": legacy_convert.office_com_configured(),
                "mode": legacy_convert.office_com_mode(),
                "powershell": legacy_convert.powershell_available(),
                "available": legacy_convert.office_com_available(),
                "versions": (legacy_convert.office_com_healthz() or {}).get("versions"),
            },
        },
        # 外部の道具の導入状況（sherpa/required_tools.py・短時間キャッシュ）。
        "required_tools": required_tools.snapshot(),
        # rag.md の LLM 成形トグル。既定 off。ON でも規則版と両立し、既存の成形版は残る。
        "rag_llm_render": {
            "configured": sysset.get("rag_llm_render"),  # 生値（未設定=None＝既定に従う）。
            "effective": llm_render.rag_llm_render_enabled(),  # system>既定（bool）。
            "default": llm_render.env_default_enabled(),  # 未設定に戻したときの実効（コード既定）。
            "options": ["on", "off"],
        },
        # 視覚読み取り（vision）の VLM 設定。既定＝ローカル（Ollama）。クラウド（OpenAI）は cloud_allowed=true（管理者が明示許可）のときだけ有効。
        "vlm": {
            "configured": sysset.get("vlm"),  # 生値（未設定=None＝既定へ）。
            # 解決結果（provider/model/cloud_allowed/ollama_url）。provider/model/ollama_url は system>既定（ollama_url は中央の Ollama 接続先）。
            "effective": vision_arm.vlm_config(),
            "default": vision_arm.env_default_vlm(),  # 未設定に戻したときの実効（cloud は常に false）。
            "available": vision_arm.vlm_usable(),  # 実効的に使えるか（ネットワーク I/O なし）。
            "providers": list(vision_arm._KNOWN_PROVIDERS),  # ローカル(ollama)/クラウド(openai)。
            "openai_key_present": bool(vision_arm._openai_key()),  # クラウド選択時のキー未設定案内用。
        },
        # Ollama 接続先の SSRF allowlist。loopback（localhost・127.0.0.0/8・::1）は常に暗黙許可されるため、ここに出るのはそれ以外（RFC1918 含む）の許可先のみ（`llm.assert_ollama_url_allowed`）。
        "ollama_allowlist": {
            "configured": sysset.get("ollama_allowlist"),  # 生値（未設定=None＝loopback のみ許可）。
            # 実際に許可される非 loopback 接続先（DB の admin allowlist のみ・env は初回シード時の一度きりの追加を除き影響しない・`llm._allowlisted_hosts()`）。host:port のみで秘密情報は含まない。
            "effective": sorted(f"{h}:{p}" for h, p in llm._allowlisted_hosts(sysset)),
        },
        # Webhook 宛先の SSRF allowlist。`ollama_allowlist` と型は同じだが loopback を暗黙許可せず、ここに出る host:port が許可先の全て（`webhooks.assert_webhook_url_allowed`）。
        "webhook_allowlist": {
            "configured": sysset.get("webhook_allowlist"),
            "effective": sorted(f"{h}:{p}" for h, p in webhooks._allowlisted_hosts(sysset)),
        },
        # 会話ごとの Codex resume セッションの保持日数。未設定は既定 30 日、明示的な 0 だけが「無制限」（`effective_codex_session_retention_days`・`api._sweep_expired_codex_sessions`）。
        "codex_session_retention_days": {
            "configured": sysset.get("codex_session_retention_days"),  # 生値（未設定=None）。
            "effective": effective_codex_session_retention_days(sysset),
            "default": CODEX_SESSION_RETENTION_DAYS_DEFAULT,
        },
        # 素の Codex モード。`effective` は `codex_sandbox.codex_mode()`（実行時に provider.py が見るのと同じ値）。plain では下の depth_profile（見直しの回数等）は Codex に渡らない（設定自体は残す）。
        "codex_mode": {
            "configured": sysset.get("codex_mode"),
            "effective": codex_sandbox.codex_mode(sysset),
            "default": "standard",
            "options": list(codex_sandbox.CODEX_MODES),
        },
        # 調べる深さの基準値（標準時の値）。`effective` は `depth_profile.effective_base()`（system_settings→コード既定）の解決結果、`default` はコード既定（未設定に戻したときの実効値）。倍率表自体は固定でここでは編集しない。
        "depth_profile": {
            "grep_max_hits": {
                "configured": sysset.get("depth_base_grep_max_hits"),
                "effective": depth_profile.effective_base(sysset, "grep_max_hits", agentic_search.MAX_HITS),
                "default": agentic_search.MAX_HITS,
            },
            "qa_max_hits": {
                "configured": sysset.get("depth_base_qa_max_hits"),
                "effective": depth_profile.effective_base(
                    sysset, "qa_max_hits", chat_service.QA_MAX_HITS_DEFAULT),
                "default": chat_service.QA_MAX_HITS_DEFAULT,
            },
            "read_window": {
                "configured": sysset.get("depth_base_read_window"),
                "effective": depth_profile.effective_base(sysset, "read_window", agentic_search.READ_WINDOW),
                "default": agentic_search.READ_WINDOW,
            },
            "impact_depth": {
                "configured": sysset.get("depth_base_impact_depth"),
                "effective": depth_profile.effective_base(
                    sysset, "impact_depth", impact_service.IMPACT_MAX_DEPTH),
                "default": impact_service.IMPACT_MAX_DEPTH,
            },
            "troubleshoot_depth": {
                "configured": sysset.get("depth_base_troubleshoot_depth"),
                "effective": depth_profile.effective_base(
                    sysset, "troubleshoot_depth", lens_service.TROUBLESHOOT_GRAPH_DEPTH),
                "default": lens_service.TROUBLESHOOT_GRAPH_DEPTH,
            },
            "codex_reasoning": {
                "configured": sysset.get("depth_base_codex_reasoning"),
                "effective": depth_profile.effective_base(
                    sysset, "codex_reasoning", depth_profile.CODEX_REASONING_DEFAULT),
                "default": depth_profile.CODEX_REASONING_DEFAULT,
                "options": list(depth_profile.CODEX_REASONING_LEVELS),
            },
        },
        # 埋め込み HTTP の同時送信数（`embeddings.embed()` が `_provider_batches` を並列送信する本数）。
        "embed_provider": {
            "configured": sysset.get("embed_provider"),
            "effective": embeddings.effective_embed_provider(sysset),
            "default": embeddings.EMBED_PROVIDER_DEFAULT,
            "options": list(embeddings.EMBED_PROVIDERS),
            "ollama_model": model_catalog.resolve_model("ollama", "embed", None, system_settings=sysset)
                            or embeddings._MODELS["ollama"][0],
        },
        "embed_parallel": {
            "configured": sysset.get("embed_parallel"),
            "effective": embeddings.effective_embed_parallel(sysset),
            "default": embeddings.EMBED_PARALLEL_DEFAULT,
        },
        # 「最大」の深さが許す査読の巡数（`depth_profile.review_rounds_for`）。クイック 0・標準 2・深く 4 はコード固定で、設定はこの 1 項目だけ。
        "max_review_rounds": {
            "configured": sysset.get("max_review_rounds"),
            "effective": depth_profile.effective_max_review_rounds(sysset),
            "default": depth_profile.MAX_REVIEW_ROUNDS_DEFAULT,
        },
        # multi_agent の worker モデル。`effective` は本体 Codex が実際に使うモデル名（カタログの codex/codex 既定値）を Azure 判定に使って解決する（渡さないと Azure かつ未設定のとき実在しないフォールバック固定値を表示してしまう）。解決自体が壊れた保存値で失敗しても、表示専用のこの経路は 500 にしない（`_effective_codex_worker_model`）。
        "codex_worker_model": {
            "configured": sysset.get("codex_worker_model"),
            "effective": _effective_codex_worker_model(sysset),
            "default": codex_sandbox._CODEX_WORKER_MODEL_FALLBACK,
        },
        # 同時実行の上限（背景実行の受付・超過は 429・`sherpa/chat_turns.py::effective_limits`）。`effective` はターン受付が実際に使う値（`effective_limits()` を直接呼ぶ）、`default` はコード既定。
        "chat_max_turns": {
            "per_user": {
                "configured": sysset.get("chat_max_turns_per_user"),
                "effective": chat_turns.effective_limits()[0],
                "default": chat_turns.MAX_TURNS_PER_USER,
            },
            "global": {
                "configured": sysset.get("chat_max_turns_global"),
                "effective": chat_turns.effective_limits()[1],
                "default": chat_turns.MAX_TURNS_GLOBAL,
            },
        },
        # Codex の MCP ツール結果 1 件あたりのバイト予算（管理者設定）。`effective` は `agentic_search.effective_tool_result_max_bytes()`（settings > コード既定）の解決結果、`default` はコード既定（精度優先）。
        "agentic_budget": {
            "per_result": {
                "configured": sysset.get("agentic_budget_per_result"),
                "effective": agentic_search.effective_tool_result_max_bytes(sysset),
                "default": agentic_search.TOOL_RESULT_MAX_BYTES,
            },
        },
        # 個人ファイル（workspace）の 1 件あたりの上限と保持日数。`effective` は `workspace_limits` が実際に使う値、`default` はコード既定。
        "workspace": {
            "max_bytes": {
                "configured": sysset.get("workspace_max_bytes"),
                "effective": workspace_limits.max_bytes(),
                "default": workspace_limits.MAX_BYTES_DEFAULT,
            },
            "ttl_days": {
                "configured": sysset.get("workspace_ttl_days"),
                "effective": workspace_limits.ttl_days(),
                "default": workspace_limits.TTL_DAYS_DEFAULT,
            },
        },
        # チャット画面のクイック入力例（ウェルカム画面のチップ）。`configured` は生値（未設定なら null）、`effective` は実際に表示される内容（非表示なら空リスト）、`default` は組み込み既定の 4 例（フロントの `web/chat/state.js::DEFAULT_EXAMPLES` と一致させる必要があり、ずれの確認用に返す）。
        "chat_examples": {
            "configured": sysset.get("chat_examples"),
            "effective": chat_examples.effective_examples(sysset),
            "default": list(chat_examples.DEFAULT_ITEMS),
            "max_items": chat_examples.MAX_ITEMS,
            "max_item_length": chat_examples.MAX_ITEM_LENGTH,
        },
    }


def _validate_arms_enabled(value):
    """`arms_enabled` の検証。None/空リストは None（未設定＝既定へ）。list は既知アーム名のみ許可し、重複を畳んで返す。未知名・型不正は 422。"""
    if value is None:
        return None
    if not isinstance(value, list):
        raise HTTPException(422, "arms_enabled はアーム名の配列で指定してください")
    from sherpa.ingest import arms as ingest_arms
    known = set(ingest_arms.known_arm_names())
    names = []
    for n in value:
        if not isinstance(n, str):
            raise HTTPException(422, "arms_enabled の各要素はアーム名（文字列）で指定してください")
        name = n.strip()
        if not name:
            continue
        if name not in known:
            raise HTTPException(422, f"未知のアーム名です: {name}（既知: {', '.join(sorted(known))}）")
        names.append(name)
    return list(dict.fromkeys(names)) or None  # 重複除去・空は未設定扱い（既定へ）。


def _validate_legacy_backend(value):
    """`legacy_backend` の検証。None は未設定（既定へ）。許可は none|libreoffice|office_com（`legacy_convert.KNOWN_BACKENDS`）で、未知値は 422。`none` は明示的な選択として生値のまま保存する。office_com はワーカー不達でも保存でき、実効は変換不可へ倒れる。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "legacy_backend は文字列で指定してください")
    name = value.strip()
    if not name:
        return None
    from sherpa.ingest.arms import legacy_convert
    if name not in legacy_convert.KNOWN_BACKENDS:
        raise HTTPException(
            422, f"未対応の変換バックエンドです: {name}"
                 f"（利用可能: {', '.join(sorted(legacy_convert.KNOWN_BACKENDS))}）")
    if name == "libreoffice" and not legacy_convert.soffice_available():
        raise HTTPException(422, "LibreOffice が入っていません（入れ方: sudo apt-get install -y libreoffice）")
    return name


def _validate_rag_llm_render(value):
    """`rag_llm_render` の検証。None は未設定（既定 off）。許可は on|off のみ（大文字小文字は無視）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "rag_llm_render は文字列で指定してください")
    name = value.strip().lower()
    if not name:
        return None
    if name not in ("on", "off"):
        raise HTTPException(422, "rag_llm_render は on または off で指定してください")
    return name


def _validate_vlm(value):
    """`vlm` の検証。None/空 dict は None（未設定＝既定へ）。
    受理キーは `provider`（"ollama"|"openai"）・`model`（非空文字列）・`cloud_allowed`（bool）のみで、型不正・未知 provider・未知キーは 422。`cloud_allowed` の既定は false（未指定なら保存しない）。provider=openai の保存自体は許可するが、cloud_allowed=false のままなら実効は無効（画像を送らない）。
    """
    if value is None:
        return None
    if not isinstance(value, dict):
        raise HTTPException(422, "vlm はオブジェクト（provider/model/cloud_allowed）で指定してください")
    from sherpa.ingest.arms import vision_arm
    known_keys = {"provider", "model", "cloud_allowed"}
    unknown = set(value) - known_keys
    if unknown:
        raise HTTPException(422, f"vlm の未知のキーです: {', '.join(sorted(unknown))}"
                                 f"（利用可能: {', '.join(sorted(known_keys))}）")
    out: dict = {}
    if "provider" in value and value["provider"] is not None:
        prov = value["provider"]
        if not isinstance(prov, str) or prov not in vision_arm._KNOWN_PROVIDERS:
            raise HTTPException(422, f"vlm.provider は {', '.join(vision_arm._KNOWN_PROVIDERS)} "
                                     "のいずれかで指定してください")
        out["provider"] = prov
    if "model" in value and value["model"] is not None:
        model = value["model"]
        if not isinstance(model, str) or not model.strip():
            raise HTTPException(422, "vlm.model は空でない文字列で指定してください")
        out["model"] = model.strip()
    if "cloud_allowed" in value and value["cloud_allowed"] is not None:
        if not isinstance(value["cloud_allowed"], bool):
            raise HTTPException(422, "vlm.cloud_allowed は true/false で指定してください")
        out["cloud_allowed"] = value["cloud_allowed"]
    return out or None  # 空 dict は未設定扱い（既定へ）。


def _validate_ollama_allowlist(value):
    """`ollama_allowlist` の検証。None/空リストは None（未設定＝loopback のみ許可・`llm.assert_ollama_url_allowed`）。
    各エントリは scheme・空白を含まない `host[:port]` の文字列のみ許可し、`llm._canonical_host_port` で `host:port` に正規化して保存する（読取側の比較を文字列一致で済ませ、表記ゆれで迂回できないようにする）。不正な形式・解釈不能なホストは 422。
    """
    if value is None:
        return None
    if not isinstance(value, list):
        raise HTTPException(422, "ollama_allowlist は host:port の配列で指定してください")
    from sherpa import llm
    out = []
    for entry in value:
        if not isinstance(entry, str):
            raise HTTPException(422, "ollama_allowlist の各要素は host:port（文字列）で指定してください")
        e = entry.strip()
        # userinfo（@）・path（/）・query（?）・fragment（#）を拒否する（`_canonical_host_port` は hostname だけ取り出すため `127.0.0.1@evil:11434` が `evil:11434` に丸まる誤登録を防ぐ）。
        if not e or "://" in e or any(c.isspace() for c in e) or any(c in e for c in "@/?#"):
            raise HTTPException(422, f"不正な接続先です: {entry!r}（scheme/userinfo/path/空白を含まない host:port 形式で指定してください）")
        hp = llm._canonical_host_port(f"http://{e}")
        if hp is None:
            raise HTTPException(422, f"不正な接続先です: {entry!r}（host:port 形式で指定してください）")
        out.append(llm.format_host_port(hp[0], hp[1]))  # IPv6 は角括弧付き（読取側の再パースと round-trip する）。
    return list(dict.fromkeys(out)) or None  # 重複除去・空は未設定扱い（loopback のみ許可へ）。


def _validate_webhook_allowlist(value):
    """`webhook_allowlist` の検証。`_validate_ollama_allowlist` と同形式（各エントリは host:port のみ・scheme/userinfo/path/空白は拒否）。None/空リストは None（未設定＝全拒否。loopback も例外にしない）。"""
    if value is None:
        return None
    if not isinstance(value, list):
        raise HTTPException(422, "webhook_allowlist は host:port の配列で指定してください")
    from sherpa import llm
    out = []
    for entry in value:
        if not isinstance(entry, str):
            raise HTTPException(422, "webhook_allowlist の各要素は host:port（文字列）で指定してください")
        e = entry.strip()
        if not e or "://" in e or any(c.isspace() for c in e) or any(c in e for c in "@/?#"):
            raise HTTPException(422, f"不正な接続先です: {entry!r}（scheme/userinfo/path/空白を含まない host:port 形式で指定してください）")
        hp = llm._canonical_host_port(f"http://{e}")
        if hp is None:
            raise HTTPException(422, f"不正な接続先です: {entry!r}（host:port 形式で指定してください）")
        out.append(llm.format_host_port(hp[0], hp[1]))
    return list(dict.fromkeys(out)) or None


def _validate_cloud_provider(value):
    """`cloud_provider` の検証。None は未設定（既定 openai へ）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "cloud_provider は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        return None
    from sherpa import keys
    if v in keys.RETIRED_CLOUD_PROVIDERS:
        raise HTTPException(422, f"cloud_provider の {v} は利用できなくなりました"
                                 f"（{'/'.join(keys.CLOUD_PROVIDERS)} を指定してください）")
    if v not in keys.CLOUD_PROVIDERS:
        raise HTTPException(422, f"cloud_provider は {'/'.join(keys.CLOUD_PROVIDERS)} のいずれかで指定してください")
    return v


def _validate_secret_key(value, field_label: str):
    """openai の中央 API キーの検証。None は未設定のまま・空文字は明示クリア（未設定へ戻す）。
    改行・制御文字を含む値は 422（保存されると送信時にヘッダ値エラーの例外メッセージへキー値が混入して漏洩しうる）。検査は `strip()` 前の生値に対して行い、前後だけに制御文字がある値や制御文字だけの値も 422 にする（クリアは利用者が明示的に空文字を送ったときだけ）。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, f"{field_label} は文字列で指定してください")
    if value and any(ord(c) < 0x20 or ord(c) == 0x7f for c in value):
        raise HTTPException(422, f"{field_label} に改行・制御文字を含めることはできません")
    v = value.strip()
    return v or None


def _validate_central_ollama_url(value, pending_allowlist: list[str] | None = None, *,
                                 strict_pending: bool = False):
    """中央既定の Ollama 接続先の検証。空文字/None は未設定（既定 localhost へ）。宛先ポリシーは個人設定の `ollama_url` と同じ `llm.assert_ollama_url_allowed`（loopback／admin allowlist）。
    `pending_allowlist`（省略可）: 同じ PUT で `ollama_allowlist` も更新されるとき、検証済みの新しい候補値（`_validate_ollama_allowlist` の戻り値）を渡す（DB はまだ更新前のため）。
    `strict_pending=True`（この PUT で `ollama_url` が実際に新しい値へ変わる場合だけ渡す・`admin_settings_put` 参照）: `pending_allowlist` を置換後の正本として `llm.assert_ollama_url_allowed_in` で検証し、DB の現行 allowlist は見ない。
    `strict_pending=False`（既定）: DB の現行 allowlist に `extra_allowed` としてこの検証だけ重ねる（URL が変わらない再送を、同時の allowlist 縮小で 422 にしないための緩さ）。
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "ollama_url は文字列で指定してください")
    v = value.strip()
    if not v:
        return None
    from sherpa import llm
    try:
        if strict_pending:
            allowed = set()
            for entry in (pending_allowlist or []):
                hp = llm._canonical_host_port(f"http://{entry}")
                if hp is not None:
                    allowed.add(hp)
            llm.assert_ollama_url_allowed_in(v, allowed)
        else:
            extra_allowed = None
            if pending_allowlist is not None:
                extra_allowed = set()
                for entry in pending_allowlist:
                    hp = llm._canonical_host_port(f"http://{entry}")
                    if hp is not None:
                        extra_allowed.add(hp)
            llm.assert_ollama_url_allowed(v, extra_allowed=extra_allowed)
    except llm.SsrfBlocked:
        raise HTTPException(422, "指定された Ollama 接続先は許可されていません"
                                 "（admin が allowlist に登録した host:port のみ保存できます）")
    return v


def _validate_research_default_provider(value):
    """`research_default_provider` の検証。None は未設定（`simple_chat.default_research_provider()` の既定 "ollama"）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "research_default_provider は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        return None
    if v not in simple_chat.RESEARCH_PROVIDERS:
        options = "/".join(sorted(simple_chat.RESEARCH_PROVIDERS))
        raise HTTPException(422, f"research_default_provider は {options} のいずれかで指定してください")
    return v


def _validate_depth_base_codex_reasoning(value):
    """`depth_base_codex_reasoning` の検証。None は未設定（コードの既定）。既知の語彙（`sherpa.depth_profile.CODEX_REASONING_LEVELS`）以外は 422。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "depth_base_codex_reasoning は文字列で指定してください")
    v = value.strip().lower()
    if v not in depth_profile.CODEX_REASONING_LEVELS:
        options = "/".join(depth_profile.CODEX_REASONING_LEVELS)
        raise HTTPException(422, f"depth_base_codex_reasoning は {options} のいずれかで指定してください")
    return v


def _validate_codex_worker_model(value):
    """`codex_worker_model` の検証。None は未設定（`_CODEX_WORKER_MODEL_FALLBACK`）。語彙検証はしない（空文字は None と同じ）。制御文字（CR/LF 等・DEL）を含む値は 422（`_write_codex_agent_role_configs` が TOML の 1 行文字列として書くため）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "codex_worker_model は文字列で指定してください")
    v = value.strip()
    if not v:
        return None
    if any(ch < " " or ch == "\x7f" for ch in v):
        raise HTTPException(422, "codex_worker_model に制御文字は使えません")
    return v


def _validate_embed_provider(value):
    """`embed_provider` の検証。None は未設定（既定 "auto"）。閉じた語彙以外は 422。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "embed_provider は文字列で指定してください")
    from sherpa import embeddings
    v = value.strip().lower()
    if v not in embeddings.EMBED_PROVIDERS:
        raise HTTPException(422, f"embed_provider は {'/'.join(embeddings.EMBED_PROVIDERS)} のいずれかで指定してください")
    return v


def _validate_codex_mode(value):
    """`codex_mode` の検証。None は未設定（既定 "standard"）。閉じた語彙（`codex_sandbox.CODEX_MODES`）以外は 422（空文字も未設定扱いにしない）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "codex_mode は文字列で指定してください")
    v = value.strip().lower()
    if v not in codex_sandbox.CODEX_MODES:
        options = "/".join(codex_sandbox.CODEX_MODES)
        raise HTTPException(422, f"codex_mode は {options} のいずれかで指定してください")
    return v


def _assert_research_default_provider_sendable(effective_settings: dict) -> None:
    """`research_default_provider` を "openai" にする PUT は、保存時点で実際に送信できる状態かを preflight する（`simple_chat._connect_openai` と同じ `sherpa.providers.openai_direct_block_reason` を `usage="subsearch"` で呼ぶ）。プレースホルダ/未設定キー・Azure 等で用途別デプロイ名が無いなら 422 で拒否する。
    `effective_settings`: この PUT 適用後に有効になる設定のスナップショット（現在値へ `updates` を重ねたもの・呼び出し元が組み立てる）。DB の advisory lock は取らない（不一致が起きても実行時に `_connect_openai` が 503 で拒否する）。
    """
    from sherpa import keys as _keys, providers as _providers
    try:
        key = _keys.resolve_api_key("openai", None, system_settings=effective_settings, strict=True)
    except _keys.InvalidCloudProviderConfigError as e:
        raise HTTPException(
            422, f"簡易回答に使う AI を OpenAI にできません（{e}）") from None
    reason = _providers.openai_direct_block_reason(key, effective_settings, usage="subsearch")
    if reason is not None:
        raise HTTPException(422, f"簡易回答に使う AI を OpenAI にできません（{reason}）")


def _validate_openai_endpoint_kind(value):
    """`openai_endpoint_kind`（接続先の種別）の検証。None は未設定（推定へフォールバック・`llm.openai_endpoint_kind()`）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "openai_endpoint_kind は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        return None
    if v not in ("openai", "azure", "custom"):
        raise HTTPException(422, "openai_endpoint_kind は openai/azure/custom のいずれかで指定してください")
    return v


def _validate_openai_base_url(value):
    """`openai_base_url`（接続先 URL）の検証。None/空文字は未設定（既定 OpenAI 本家へ）。妥当性は `llm.assert_openai_base_url_allowed` に委ねる。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "openai_base_url は文字列で指定してください")
    v = value.strip()
    if not v:
        return None
    from sherpa import llm
    try:
        llm.assert_openai_base_url_allowed(v)
    except ValueError as e:
        raise HTTPException(422, str(e))
    return v.rstrip("/")


def _validate_openai_auth_header(value):
    """`openai_auth_header` の検証。None/空文字は未設定（既定 bearer へ）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "openai_auth_header は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        return None
    if v not in ("bearer", "api-key"):
        raise HTTPException(422, "openai_auth_header は bearer/api-key のいずれかで指定してください")
    return v


def _validate_openai_api_version(value):
    """`openai_api_version` の検証。None/空文字は未設定（未使用へ）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "openai_api_version は文字列で指定してください")
    return value.strip() or None


def _validate_model_catalog(value):
    """`model_catalog` の検証。形・意味の検証本体は `sherpa.model_catalog.validate_catalog`（`ValueError`）で、ここでは 422 へ変換する。"""
    try:
        return model_catalog.validate_catalog(value)
    except ValueError as e:
        raise HTTPException(422, str(e))


def _validate_chat_examples(value):
    """`chat_examples`（チャット画面のクイック入力例）の検証。検証本体は `sherpa.chat_examples.validate`（`ValueError`）で、ここでは 422 へ変換する。"""
    try:
        return chat_examples.validate(value)
    except ValueError as e:
        raise HTTPException(422, str(e))


def _validate_codex_session_retention_days(value):
    """`codex_session_retention_days` の検証。None は未設定（既定 30 日・`effective_codex_session_retention_days`）。0 以上の整数のみ許可（0＝無制限）。負値・非整数は 422。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "codex_session_retention_days は0以上の整数で指定してください")
    if value < 0:
        raise HTTPException(422, "codex_session_retention_days は0以上の整数で指定してください（0=無制限）")
    return value


# GET・PUT /admin/settings には response_model を付けない。`legacy_backend.libreoffice.version` を含むため、response_model を付けると OpenAPI に `version` が露出して `test_openapi_surface_has_no_version_parameter` に反する。この 2 ルートは `sherpa.schemas.AdminSettingsView` を TypeAdapter 契約のみで固定する。
@extras_router.get("/admin/settings", tags=["管理者:全体設定"])
def admin_settings_get(request: Request):
    """全体設定（取り込みアーム・旧形式変換バックエンド）の現行値＋実効値を返す（admin のみ）。`legacy_backend.libreoffice` に soffice の検出状態（有無＋バージョン）を含める。"""
    _require_admin(_current_user(request))
    return _admin_settings_view()


@extras_router.put("/admin/settings", tags=["管理者:全体設定"])
def admin_settings_put(req: SystemSettingsReq, request: Request):
    """全体設定を部分更新する（admin のみ・検証・監査・fail-closed）。応答は GET と同形。
    未指定キーは変更せず、`null` は未設定へ戻す（コードの既定へ戻る）。検証:
    - arms_enabled: 既知アーム名のみ。legacy_backend: none|libreoffice|office_com。rag_llm_render: on|off。vlm: provider/model/cloud_allowed の型。
    - ollama_allowlist/webhook_allowlist: 各エントリを host:port に正規化して保存（不正は 422）。
    - codex_session_retention_days: 0 以上の整数（0=無制限）。
    - openai_endpoint_kind/base_url/auth_header/api_version: 種別・URL 妥当性・ヘッダ形式を検証し、kind が openai 以外なら実効 base_url が空でないことも確認する。
    - research_default_provider: ollama/openai のみ。"openai" への変更は適用後の実効設定で実送信可能性も確認する。
    - depth_base_*: 整数 5 項目は範囲検証済み。depth_base_codex_reasoning は定義済みの推論レベルのみ。
    - chat_max_turns_per_user/chat_max_turns_global: 1〜16／1〜64。agentic_budget_per_result: 1024〜8MiB。
    - workspace_max_bytes: 1MiB〜1GiB。workspace_ttl_days: 0〜3650（0=無期限）。
    - chat_examples: `{enabled, items}`（items は最大 8 件・各 1〜200 文字）。codex_worker_model: 文字列のみ（空文字/null は未設定へ戻す）。codex_mode: standard/plain のみ。
    設定変更と監査は同一トランザクションで行い、監査に失敗すれば設定変更も取り消されて 500 を返す。
    """
    u = _current_user(request)
    _require_admin(u)
    provided = req.model_dump(exclude_unset=True)
    updates: dict = {}
    if "arms_enabled" in provided:
        updates["arms_enabled"] = _validate_arms_enabled(provided["arms_enabled"])
    if "legacy_backend" in provided:
        updates["legacy_backend"] = _validate_legacy_backend(provided["legacy_backend"])
    if "rag_llm_render" in provided:
        updates["rag_llm_render"] = _validate_rag_llm_render(provided["rag_llm_render"])
    if "vlm" in provided:
        updates["vlm"] = _validate_vlm(provided["vlm"])
    if "ollama_allowlist" in provided:
        updates["ollama_allowlist"] = _validate_ollama_allowlist(provided["ollama_allowlist"])
    if "webhook_allowlist" in provided:
        updates["webhook_allowlist"] = _validate_webhook_allowlist(provided["webhook_allowlist"])
    if "codex_session_retention_days" in provided:
        updates["codex_session_retention_days"] = _validate_codex_session_retention_days(
            provided["codex_session_retention_days"])
    # クラウド AI プロバイダの中央設定。
    if "cloud_provider" in provided:
        updates["cloud_provider"] = _validate_cloud_provider(provided["cloud_provider"])
    if "personal_api_keys_allowed" in provided:
        # StrictBool が型検証済み（非 bool は pydantic が 422 にする）。
        updates["personal_api_keys_allowed"] = provided["personal_api_keys_allowed"]
    if "web_search_allowed" in provided:
        # StrictBool が型検証済み（非 bool は pydantic が 422 にする）。
        updates["web_search_allowed"] = provided["web_search_allowed"]
    if "user_api_keys_allowed" in provided:
        # StrictBool が型検証済み（非 bool は pydantic が 422 にする）。
        updates["user_api_keys_allowed"] = provided["user_api_keys_allowed"]
    if "user_api_keys_daily_quota_default" in provided:
        # StrictInt・範囲（1〜1,000,000）は pydantic Field が型検証済み。
        updates["user_api_keys_daily_quota_default"] = provided["user_api_keys_daily_quota_default"]
    if "research_default_provider" in provided:
        updates["research_default_provider"] = _validate_research_default_provider(
            provided["research_default_provider"])
    # 調べる深さの基準値（整数 5 項目は StrictInt+Field(ge,le) で範囲検証済み）。
    for _k in ("depth_base_grep_max_hits", "depth_base_qa_max_hits",
              "depth_base_read_window", "depth_base_impact_depth", "depth_base_troubleshoot_depth"):
        if _k in provided:
            updates[_k] = provided[_k]
    if "depth_base_codex_reasoning" in provided:
        updates["depth_base_codex_reasoning"] = _validate_depth_base_codex_reasoning(
            provided["depth_base_codex_reasoning"])
    if "embed_provider" in provided:
        updates["embed_provider"] = _validate_embed_provider(provided["embed_provider"])
    if "embed_parallel" in provided:
        # StrictInt・範囲（1〜16）は pydantic Field が型検証済み。
        updates["embed_parallel"] = provided["embed_parallel"]
    if "max_review_rounds" in provided:
        # StrictInt・範囲は pydantic Field が型検証済み。
        updates["max_review_rounds"] = provided["max_review_rounds"]
    if "codex_worker_model" in provided:
        updates["codex_worker_model"] = _validate_codex_worker_model(provided["codex_worker_model"])
    if "codex_mode" in provided:
        updates["codex_mode"] = _validate_codex_mode(provided["codex_mode"])
    # チャット同時実行の上限（2 項目とも StrictInt+Field(ge,le) で範囲検証済み）。
    for _k in ("chat_max_turns_per_user", "chat_max_turns_global"):
        if _k in provided:
            updates[_k] = provided[_k]
    # Codex の MCP ツール結果 1 件あたりのバイト予算（StrictInt+Field(ge,le) で範囲検証済み）。
    if "agentic_budget_per_result" in provided:
        updates["agentic_budget_per_result"] = provided["agentic_budget_per_result"]
    # 個人ファイルの上限・保持日数（StrictInt+Field(ge,le) で範囲検証済み）。
    for _k in ("workspace_max_bytes", "workspace_ttl_days"):
        if _k in provided:
            updates[_k] = provided[_k]
    if "chat_examples" in provided:
        updates["chat_examples"] = _validate_chat_examples(provided["chat_examples"])
    _cloud_secret_keys = frozenset({"openai_api_key"})
    for _k in _cloud_secret_keys:
        if _k in provided:
            updates[_k] = _validate_secret_key(provided[_k], _k)
    if "ollama_url" in provided:
        # 同じ PUT で ollama_allowlist も更新される場合は、その新しい候補（検証済み）で ollama_url を検証する。
        _pending_allowlist = updates["ollama_allowlist"] if "ollama_allowlist" in updates else None
        # URL 自体が実際に新しい値へ変わる場合だけ、pending allowlist を置換後の正本として厳密検証する（strict_pending）。URL が変わらない再送は、同時の allowlist 縮小でそのホストが外れても拒否しない。
        _strict_pending = False
        if "ollama_allowlist" in provided and isinstance(provided["ollama_url"], str):
            _cur_central = (store.get_system_settings().get("ollama_url") or "").strip()
            _strict_pending = provided["ollama_url"].strip() != _cur_central
        updates["ollama_url"] = _validate_central_ollama_url(
            provided["ollama_url"], _pending_allowlist, strict_pending=_strict_pending)
    # OpenAI 互換 API の接続先（4 キー）。
    if "openai_endpoint_kind" in provided:
        updates["openai_endpoint_kind"] = _validate_openai_endpoint_kind(provided["openai_endpoint_kind"])
    if "openai_base_url" in provided:
        updates["openai_base_url"] = _validate_openai_base_url(provided["openai_base_url"])
    if "openai_auth_header" in provided:
        updates["openai_auth_header"] = _validate_openai_auth_header(provided["openai_auth_header"])
    if "openai_api_version" in provided:
        updates["openai_api_version"] = _validate_openai_api_version(provided["openai_api_version"])
    # kind/base のクロス検証（kind が openai 以外なら base_url も必要）はここでは行わない。`store.set_system_settings()` が advisory lock 取得後に同一コネクションから実効値を読んで検証する（`OpenAIEndpointSettingsConflict` の except 節）。
    if "model_catalog" in provided:
        updates["model_catalog"] = _validate_model_catalog(provided["model_catalog"])
    # research_default_provider を "openai" にする更新は、この PUT 適用後に有効になる設定（現在値へ updates を重ねたもの）で実送信可能性を preflight する。
    if updates.get("research_default_provider") == "openai":
        _assert_research_default_provider_sendable({**store.get_system_settings(), **updates})
    if updates:
        try:
            # `user_api_keys_allowed` を実効 OFF（false または明示 null）にする更新は、設定の適用・利用者発行キーの一括失効・監査を同一トランザクションで行う（失効の失敗も設定変更ごと rollback する）。それ以外の更新は `store.set_system_settings` と同じ結果。
            store.apply_system_settings_and_revoke_if_disabled(
                u["uid"], updates, secret_keys=_cloud_secret_keys & set(updates))
        except store.OpenAIEndpointSettingsConflict as e:
            raise HTTPException(422, str(e))
        except Exception:
            _log.critical("system_settings.updated failed (fail-closed) – keys=%s", list(updates))
            raise HTTPException(500, "全体設定の保存中にエラーが発生しました")
        # personal_api_keys_allowed を false で保存するたび（冪等）、全ユーザーの個人秘密キーを一括削除する。設定本体の保存は成功済みのため、失敗しても 500 にはしない（起動時の `api._purge_personal_keys_if_disabled_on_startup` が backstop）。
        if updates.get("personal_api_keys_allowed") is False:
            try:
                store.purge_personal_api_keys(actor=u["uid"])
            except Exception:
                _log.critical("personal_api_keys_allowed=false の個人キー一括削除に失敗しました"
                             "（次回起動時に再試行されます）")
    return _admin_settings_view()


class OpenaiEndpointTestReq(BaseModel):
    """管理画面「接続先」欄の接続テスト（admin 専用・`POST /admin/settings/openai-endpoint-test`）。
    タイムアウトは接続テスト専用に短い（10 秒で「到達できません」を返す）。保存前の入力中の値でその場だけ試し、DB は書かず、秘密は保存も監査もしない。個人設定用の `POST /settings/test` とは別ルートで、一般ユーザーには接続先 override を与えない。
    """
    provider: str = "openai"  # openai（既定）／codex（Codex(OpenAI) 構成の判定）。
    openai_endpoint_kind: str | None = None
    openai_base_url: str | None = None
    openai_auth_header: str | None = None
    openai_api_version: str | None = None
    openai_api_key: str | None = None  # 入力中の未保存の中央キー。省略時は保存済み中央キー。
    # `codex_model` は受け取らない（カタログ外のモデル名で検証を迂回させない）。provider=codex は常に中央カタログ既定（`model_catalog.resolve_model` の field 省略）で解決する。


@extras_router.post("/admin/settings/openai-endpoint-test", tags=["管理者:全体設定"],
                    response_model=SettingsTestResponse)
def admin_openai_endpoint_test(req: OpenaiEndpointTestReq, request: Request):
    """OpenAI 互換 API の接続先（管理画面「接続先」欄）を、保存前の入力中の値でその場だけ試す（admin 専用・1 回だけ最小リクエスト・DB は書かない）。
    `PUT /admin/settings` と同じ検証（種別・URL 妥当性・userinfo 禁止・kind が openai 以外なら base_url 必須）を通信前に行い、不正な入力は 422 で接続しない。キー・モデルは常に中央設定で解決し、provider=codex は Codex(OpenAI) 構成の接続先ブロック判定を同じ入力で試す。
    実行を監査する（`openai_endpoint.tested`・記録は actor・provider・endpoint_kind・host のみで、キー・生 URL・エラー本文は含めない）。監査の書き込みに失敗したら接続せず 500 で中断する（fail-closed）。
    """
    from sherpa import agent_constructs, keys, llm, model_catalog
    from sherpa.ingest import graph_extract
    u = _current_user(request)
    _require_admin(u)
    prov = (req.provider or "openai").lower()
    if prov not in ("openai", "codex"):
        raise HTTPException(422, "provider は openai / codex のいずれか")
    sys_s = store.get_system_settings()
    # PUT と同じ検証を通信前に行う（不正なら 422・ネットワークへは出さない）。
    pending = dict(sys_s)
    if req.openai_endpoint_kind is not None:
        pending["openai_endpoint_kind"] = _validate_openai_endpoint_kind(req.openai_endpoint_kind)
    if req.openai_base_url is not None:
        pending["openai_base_url"] = _validate_openai_base_url(req.openai_base_url)
    if req.openai_auth_header is not None:
        pending["openai_auth_header"] = _validate_openai_auth_header(req.openai_auth_header)
    if req.openai_api_version is not None:
        pending["openai_api_version"] = _validate_openai_api_version(req.openai_api_version)
    # `pending["openai_base_url"]` は保存済み値を継承した場合、非文字列（falsy な `{}`/`[]`/`0`/`False` を含む）もありうる。ここは「kind が base_url を要求するのに未設定」という欠落だけを検出する事前チェックで、`None`/空文字列だけを欠落とみなし、それ以外は通して下の再検証ブロック（不正値でも deny 監査を残してから 422 にする）に任せる。
    _raw_pending_base_url = pending.get("openai_base_url")
    _base_url_missing = _raw_pending_base_url is None or _raw_pending_base_url == ""
    try:
        llm.assert_openai_endpoint_consistent(
            pending.get("openai_endpoint_kind") or "openai",
            "" if _base_url_missing else "x")
    except ValueError as e:
        raise HTTPException(422, str(e))
    # 監査は probe（実 API 呼び出し）より前に行う。書き込み失敗は 500 に変換する（fail-closed・キー・生 URL・エラー本文は含めない）。
    # `pending["openai_base_url"]` は、`req.openai_base_url` 省略時に保存済みの値を継承し、その値は `_validate_openai_base_url` を通っていない。実効値（明示・継承いずれも）を使用直前に再検証し、不合格（型不正を含む）なら host 表現を作らず固定文字列へ倒して probe は実行しない（`system.py::_INVALID_SAVED_BASE_URL_LABEL` と同じ）。`eff_kind` の解決（`llm.openai_endpoint_kind()`）も `openai_base_url` の型検査で `ValueError` を出しうるため try 内に含める。
    try:
        eff_kind = llm.openai_endpoint_kind(pending)
        eff_base_url = llm.openai_base_url(pending)
        llm.assert_openai_base_url_allowed(eff_base_url)
    except ValueError:
        eff_kind = "(不正な保存値)"
        eff_host = "(不正な保存値)"
        base_url_valid = False
    else:
        eff_host = llm._redact_url_for_error(eff_base_url) or "(不正な保存値)"
        base_url_valid = True
    try:
        store.audit(u["uid"], "openai_endpoint.tested", "system_settings", None,
                    detail={"provider": prov, "endpoint_kind": eff_kind, "host": eff_host},
                    outcome="success" if base_url_valid else "deny",
                    reason=None if base_url_valid else "invalid_base_url",
                    severity="info" if base_url_valid else "warning")
    except Exception:
        _log.critical("audit write failed for openai_endpoint.tested (fail-closed) – probe を実行しません")
        raise HTTPException(500, "接続テストの監査記録に失敗したため中断しました")
    if not base_url_valid:
        raise HTTPException(422, "保存されている接続先 URL が不正です。管理画面で接続先を設定し直してください")
    # 中央のみ（個人キー・個人モデルは見ない＝`user_settings=None`）。この直後の probe（実 API 呼び出し）に使うため strict=True で解決する（課金を伴う接続テストを寛容なキー解決で実送信しない）。
    try:
        keys.selected_cloud_provider(sys_s, strict=True)  # 入力中のキーでも廃止済み保存値では送信しない。
        central_key = req.openai_api_key or keys.resolve_api_key(
            "openai", None, system_settings=sys_s, strict=True)
    except keys.InvalidCloudProviderConfigError as e:
        return {"ok": False, "provider": prov, "model": "", "detail": str(e)}
    if prov == "codex":
        from sherpa.providers import _codex_openai_compat_block_reason
        codex_model = model_catalog.resolve_model("codex", "codex", None, system_settings=pending)
        reason = _codex_openai_compat_block_reason(
            {}, explicit_openai_api_key=central_key, system_settings=pending)
        if reason is not None:
            return {"ok": False, "provider": "codex", "model": codex_model, "detail": reason}
        ok, detail = graph_extract._probe({"provider": "openai", "key": central_key, "model": codex_model,
                                          "openai_endpoint_override": pending},
                                         timeout=_ENDPOINT_TEST_TIMEOUT_S)
        return {"ok": ok, "provider": "codex", "model": codex_model,
                "detail": "接続OK" if ok else detail}
    if not agent_constructs.is_real_api_key(central_key):
        return {"ok": False, "provider": "openai", "model": "", "detail": keys.NO_CENTRAL_KEY_MESSAGE}
    model = model_catalog.resolve_model("openai", "chat", None, system_settings=pending)
    ok, detail = graph_extract._probe({"provider": "openai", "key": central_key, "model": model,
                                      "openai_endpoint_override": pending},
                                     timeout=_ENDPOINT_TEST_TIMEOUT_S)
    return {"ok": ok, "provider": "openai", "model": model, "detail": "接続OK" if ok else detail}


def _validate_allowed_worlds_or_error(allowed_worlds: list[str] | None) -> None:
    """`allowed_worlds` の各 world_id を個別に strict resolve する（全 world は列挙しない・空リスト/None は何もしない）。未知の world は 422、resolver（registry/root）到達不可は 503。"""
    if not allowed_worlds:
        return
    for wid in allowed_worlds:
        try:
            res = worlds.resolve_external_world(wid)
        except worlds.ExternalResolverError as e:
            raise HTTPException(
                503, "world の実在を確認できませんでした（一時的な障害の可能性があります）") from e
        if res.status != "ok":
            raise HTTPException(422, f"未知の world が指定されました: {wid}")


def _validate_future_expiry(dt: datetime | None, field_label: str) -> None:
    """API キーの有効期限は未来の日時のみ許可する（過去日は 422）。無期限（None）はそのまま許可。"""
    if dt is not None and dt <= datetime.now(timezone.utc):
        raise HTTPException(422, f"{field_label}は未来の日時で指定してください")


def _validate_webhook_url_or_error(webhook_url: str | None) -> None:
    """`webhook_url`（キー発行時のオプトイン）が宛先ポリシー（admin allowlist・loopback も対象）を満たすか検証する。None（未指定）は許可（Webhook 無効のまま発行）。不許可は 422（`webhooks.assert_webhook_url_allowed` が `WebhookUrlInvalid` を送出）。"""
    if webhook_url is None:
        return
    try:
        webhooks.assert_webhook_url_allowed(webhook_url)
    except webhooks.WebhookUrlInvalid as e:
        raise HTTPException(422, str(e)) from e


_EXT_ADMIN_AUTH_RESPONSES = {
    401: {"description": "ログインが必要です（セッション Cookie）", "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "管理者権限が必要です", "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
}
_EXT_ADMIN_RESPONSES = {
    **_EXT_ADMIN_AUTH_RESPONSES,
    503: {"description": "world の実在を確認できませんでした（一時的な障害の可能性があります）",
          "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
}


def _key_created_out(row: dict, plain: str) -> dict:
    """発行直後のレスポンス（プレーンキーを含み、このレスポンスでのみ返す）。admin 発行・利用者自己発行で共通の形。`webhook_secret` もここでのみ平文で返す（`webhook_url` 未指定なら両方 null）。"""
    return {"ok": True, "id": row["id"], "key": plain, "key_prefix": row["key_prefix"],
            "label": row["label"], "created_at": str(row["created_at"]),
            "allowed_worlds": row.get("allowed_worlds"),
            "expires_at": str(row["expires_at"]) if row.get("expires_at") else None,
            "daily_quota": row.get("daily_quota"), "client_op_id": row.get("client_op_id"),
            "webhook_url": row.get("webhook_url"), "webhook_secret": row.get("webhook_secret")}


def _key_list_out(rows: list, call_counts: dict) -> dict:
    """キー一覧のレスポンス（プレーンキーは含めない）。admin 全件一覧・利用者本人一覧で共通の形。
    `client_op_id` は発行操作の相関トークン（秘密ではない・参照用）。`webhook` は Webhook 登録の有無のみ、`webhook_host` は宛先の host:port までで、path/query・`webhook_secret` は返さない。
    """
    return {"keys": [{**{k: r[k] for k in
                          ("id", "key_prefix", "label", "created_by", "revoked_by", "allowed_worlds",
                           "daily_quota", "owner_uid", "client_op_id")},
                      "created_at": str(r["created_at"]),
                      "revoked_at": str(r["revoked_at"]) if r["revoked_at"] else None,
                      "last_used_at": str(r["last_used_at"]) if r["last_used_at"] else None,
                      "expires_at": str(r["expires_at"]) if r.get("expires_at") else None,
                      "call_count": call_counts.get(r["id"], 0),
                      "webhook": bool(r.get("webhook_url")),
                      "webhook_host": webhooks._host_port_for_audit(r["webhook_url"])
                                      if r.get("webhook_url") else None}
                     for r in rows]}


@extras_router.post("/ext/v1/admin/keys", tags=["管理者:外部APIキー"],
                    response_model=ExtKeyCreatedResponse,
                    responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              409: {"description": "client_op_id が既存キーと重複しています"
                                                    "（同じ操作トークンで二重に発行しようとした）",
                                    "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              422: ext_api._validation_error_response(
                                  "allowed_worlds に実在しない world_id が含まれる場合、"
                                  "有効期限が過去日時の場合、client_op_id が UUID 形式でない場合、"
                                  "webhook_url が宛先ポリシー（admin allowlist・"
                                  "loopback 含む）を満たさない場合"),
                              **_EXT_ADMIN_RESPONSES})
def ext_key_create(req: ExtKeyCreateReq, request: Request,
                   x_request_id: str | None = ext_api._XRequestIdIn):
    """外部 API キーを発行する（admin のみ）。プレーンキーはこのレスポンスで 1 度だけ返す（DB はハッシュのみ）。
    `allowed_worlds` は実在する world_id のみ許可する（未知の world は 422）。`expires_at` は ISO 8601 文字列（省略/null＝無期限）・`daily_quota` は 1 以上の整数（省略/null＝無制限）。
    """
    del x_request_id
    u = _require_admin(_current_user(request))
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_created", "api_key")
    # 検証前に積む（`req.label`/`req.allowed_worlds` は正規化済みの値）。422/503 で失敗しても、何を発行しようとしたかが監査に残る。
    pending["detail"].update({"label": req.label, "allowed_worlds": req.allowed_worlds,
                              "expires_at": req.expires_at, "daily_quota": req.daily_quota,
                              "webhook": bool(req.webhook_url)})
    _validate_allowed_worlds_or_error(req.allowed_worlds)
    expires_at = _parse_announcement_dt(req.expires_at, "有効期限")
    _validate_future_expiry(expires_at, "有効期限")
    _validate_webhook_url_or_error(req.webhook_url)
    # secret は登録時に生成し平文保管する（署名生成に平文が必要）。
    webhook_secret = secrets.token_urlsafe(32) if req.webhook_url else None
    plain = ext_api._generate_key()
    try:
        row = store.insert_api_key(ext_api._hash_key(plain), plain[:12], req.label, u["uid"],
                                   allowed_worlds=req.allowed_worlds, expires_at=expires_at,
                                   daily_quota=req.daily_quota, client_op_id=req.client_op_id,
                                   webhook_url=req.webhook_url, webhook_secret=webhook_secret)
    except store.ClientOpIdConflictError as e:
        pending["reason"] = "client_op_id_conflict"
        raise HTTPException(409, str(e)) from e
    pending["resource_id"] = str(row["id"])
    pending["detail"].update({"label": row["label"], "key_prefix": row["key_prefix"],
                              "allowed_worlds": row.get("allowed_worlds"),
                              "expires_at": str(row["expires_at"]) if row.get("expires_at") else None,
                              "daily_quota": row.get("daily_quota")})
    return _key_created_out(row, plain)


@extras_router.get("/ext/v1/admin/keys", tags=["管理者:外部APIキー"],
                   response_model=ExtKeyListResponse,
                   responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                             **_EXT_ADMIN_AUTH_RESPONSES})
def ext_key_list(request: Request, x_request_id: str | None = ext_api._XRequestIdIn):
    """外部 API キー一覧を返す（プレーンキーは含めない・全件・admin のみ）。
    `call_count` は直近分の呼び出し回数。`owner_uid` が非 null のキーは利用者本人が自己発行したもので、admin はこれも含めて全件を見え、失効もできる。
    """
    del x_request_id
    u = _require_admin(_current_user(request))
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_listed", "api_key")
    rows = store.list_api_keys()
    pending["detail"]["result_count"] = len(rows)
    call_counts = store.count_ext_api_calls_by_key([r["id"] for r in rows])
    return _key_list_out(rows, call_counts)


@extras_router.delete("/ext/v1/admin/keys/{key_id}", tags=["管理者:外部APIキー"],
                      response_model=ExtKeyRevokeResponse,
                      responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                                404: {"description": "キーが見つかりません",
                                      "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                                422: ext_api._validation_error_response("key_id が整数でない場合"),
                                **_EXT_ADMIN_AUTH_RESPONSES})
def ext_key_revoke(key_id: int, request: Request,
                   x_request_id: str | None = ext_api._XRequestIdIn):
    """キーを失効する（soft・冪等）。未知 id は 404。admin は所有者を問わず任意のキー（利用者自己発行を含む）を失効できる。"""
    del x_request_id
    u = _require_admin(_current_user(request))
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_revoked", "api_key",
                                  resource_id=str(key_id))
    row = store.revoke_api_key(key_id, u["uid"])
    if not row:
        pending["reason"] = "not_found"
        raise HTTPException(404, "キーが見つかりません")
    pending["detail"].update({"key_prefix": row["key_prefix"], "label": row["label"]})
    return {"ok": True, "id": key_id, "revoked_at": str(row["revoked_at"])}


@extras_router.post("/ext/v1/admin/keys/recover", tags=["管理者:外部APIキー"],
                    response_model=ExtKeyRecoverResponse,
                    responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              422: ext_api._validation_error_response(
                                  "client_op_id が UUID 形式でない場合"),
                              **_EXT_ADMIN_AUTH_RESPONSES})
def ext_key_recover(req: ExtKeyRecoverReq, request: Request,
                    x_request_id: str | None = ext_api._XRequestIdIn):
    """`POST /ext/v1/admin/keys` の応答が届かなかった（タイムアウト・通信断・不正な形の応答等）場合の回復専用エンドポイント。
    この admin 自身が発行操作を試みた `client_op_id` に一致する未失効キーだけを、単一の原子的な操作で照合・失効する（他人のキーには触れない）。
    `found: false` は「POST がサーバーに届かなかった」か「まだコミットされていない」のいずれか（呼び出し側で有界に再試行する）。
    """
    del x_request_id
    u = _require_admin(_current_user(request))
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_recover_attempted", "api_key")
    pending["detail"]["client_op_id"] = req.client_op_id
    row = store.revoke_unconfirmed_key_by_client_op_id(
        req.client_op_id, u["uid"], created_by=u["uid"])
    if row is None:
        pending["reason"] = "no_match"
        return {"found": False, "id": None, "revoked_at": None}
    pending["resource_id"] = str(row["id"])
    return {"found": True, "id": row["id"], "revoked_at": str(row["revoked_at"])}


# 利用者本人による API キーの自己発行/一覧/失効/回復（4 ルート）。個人設定ページと同じ Cookie 認証（`_current_user`）で、admin 権限は不要（`_require_admin` を呼ばない）。本人の分だけ見え、本人の分だけ失効・回復できる。

def _require_user_api_keys_allowed(u: dict) -> dict:
    """`system_settings.user_api_keys_allowed` が偽なら 403（本人一覧/失効/回復を含む 4 ルート共通のゲート）。戻り値は以後の処理で使い回す system_settings。"""
    sysset = store.get_system_settings()
    if not bool(sysset.get("user_api_keys_allowed")):
        raise HTTPException(403, "利用者による API キー発行は許可されていません（管理者に確認してください）")
    return sysset


def _enforce_self_world_scope(uid: str, requested: list[str] | None) -> list[str] | None:
    """利用者自己発行キーの world スコープを本人のアクセス範囲の部分集合に強制する。`worlds.accessible_world_ids(uid)` が None（現状＝全ユーザーが全 world にアクセス可）ならそのまま返し、非 None なら未指定は本人の範囲へ絞り、範囲外の world を指定されたら 403。"""
    accessible = worlds.accessible_world_ids(uid)
    if accessible is None:
        return requested
    accessible_set = set(accessible)
    if requested is None:
        return sorted(accessible_set)
    outside = [w for w in requested if w not in accessible_set]
    if outside:
        raise HTTPException(
            403, f"アクセスできない資料フォルダ（world）が指定されています: {', '.join(outside)}")
    return requested


@extras_router.post("/ext/v1/keys", tags=["外部連携API"],
                    response_model=ExtKeyCreatedResponse,
                    responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              403: {"description": "利用者による API キー発行が許可されていません、"
                                                    "またはアクセスできない world が指定されています",
                                    "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              409: {"description": "client_op_id が既存キーと重複しています"
                                                    "（同じ操作トークンで二重に発行しようとした）",
                                    "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              422: ext_api._validation_error_response(
                                  "allowed_worlds に実在しない world_id が含まれる場合、"
                                  "有効期限が過去日時の場合、daily_quota が現在の上限を超える場合、"
                                  "client_op_id が UUID 形式でない場合、webhook_url が宛先ポリシー"
                                  "（admin allowlist・loopback 含む）を満たさない場合"),
                              401: {"description": "ログインが必要です（セッション Cookie）",
                                    "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)}})
def ext_self_key_create(req: ExtSelfKeyCreateReq, request: Request,
                        x_request_id: str | None = ext_api._XRequestIdIn):
    """利用者本人の API キーを発行する（`system_settings.user_api_keys_allowed` が true のときのみ）。
    world スコープは本人のアクセス範囲に絞られる。`daily_quota` の上限チェックと許可トグルの最終判定は、発行の書込み直前にロック内で DB から再読して行う（先に行う事前チェックは早期リターン用）。プレーンキーはこの応答でのみ返す。
    """
    del x_request_id
    u = _current_user(request)
    _require_user_api_keys_allowed(u)
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_created", "api_key")
    pending["detail"].update({"label": req.label, "allowed_worlds": req.allowed_worlds,
                              "expires_at": req.expires_at, "daily_quota": req.daily_quota,
                              "self_issued": True, "webhook": bool(req.webhook_url)})
    _validate_allowed_worlds_or_error(req.allowed_worlds)
    allowed_worlds = _enforce_self_world_scope(u["uid"], req.allowed_worlds)
    expires_at = _parse_announcement_dt(req.expires_at, "有効期限")
    _validate_future_expiry(expires_at, "有効期限")
    _validate_webhook_url_or_error(req.webhook_url)
    webhook_secret = secrets.token_urlsafe(32) if req.webhook_url else None
    plain = ext_api._generate_key()
    try:
        row = store.insert_api_key(ext_api._hash_key(plain), plain[:12], req.label, u["uid"],
                                   allowed_worlds=allowed_worlds, expires_at=expires_at,
                                   daily_quota=req.daily_quota, owner_uid=u["uid"],
                                   client_op_id=req.client_op_id,
                                   webhook_url=req.webhook_url, webhook_secret=webhook_secret)
    except store.UserApiKeysDisallowedError as e:
        pending["reason"] = "user_api_keys_disallowed"
        raise HTTPException(403, str(e)) from e
    except store.SelfIssuedQuotaExceededError as e:
        pending["reason"] = "daily_quota_exceeded"
        raise HTTPException(422, str(e)) from e
    except store.ClientOpIdConflictError as e:
        pending["reason"] = "client_op_id_conflict"
        raise HTTPException(409, str(e)) from e
    pending["resource_id"] = str(row["id"])
    pending["detail"].update({"label": row["label"], "key_prefix": row["key_prefix"],
                              "allowed_worlds": row.get("allowed_worlds"),
                              "expires_at": str(row["expires_at"]) if row.get("expires_at") else None,
                              "daily_quota": row.get("daily_quota")})
    return _key_created_out(row, plain)


@extras_router.get("/ext/v1/keys", tags=["外部連携API"],
                   response_model=ExtKeyListResponse,
                   responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                             403: {"description": "利用者による API キー発行が許可されていません",
                                   "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                             401: {"description": "ログインが必要です（セッション Cookie）",
                                   "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)}})
def ext_self_key_list(request: Request, x_request_id: str | None = ext_api._XRequestIdIn):
    """本人が発行した API キーの一覧を返す（プレーンキーは含めない・他人のキーは見えない）。機能トグルが OFF のときは一覧そのものを見せない。"""
    del x_request_id
    u = _current_user(request)
    _require_user_api_keys_allowed(u)
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_listed", "api_key")
    rows = store.list_api_keys(owner_uid=u["uid"])
    pending["detail"]["result_count"] = len(rows)
    call_counts = store.count_ext_api_calls_by_key([r["id"] for r in rows])
    return _key_list_out(rows, call_counts)


@extras_router.delete("/ext/v1/keys/{key_id}", tags=["外部連携API"],
                      response_model=ExtKeyRevokeResponse,
                      responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                                403: {"description": "利用者による API キー発行が許可されていません",
                                      "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                                404: {"description": "キーが見つかりません",
                                      "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                                422: ext_api._validation_error_response("key_id が整数でない場合"),
                                401: {"description": "ログインが必要です（セッション Cookie）",
                                      "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)}})
def ext_self_key_revoke(key_id: int, request: Request,
                        x_request_id: str | None = ext_api._XRequestIdIn):
    """本人が発行したキーを失効する（soft・冪等）。他人/admin 発行のキー・未知 id は 404。機能トグルが OFF のときは拒否する。"""
    del x_request_id
    u = _current_user(request)
    _require_user_api_keys_allowed(u)
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_revoked", "api_key",
                                  resource_id=str(key_id))
    row = store.revoke_api_key(key_id, u["uid"], owner_uid=u["uid"])
    if not row:
        pending["reason"] = "not_found"
        raise HTTPException(404, "キーが見つかりません")
    pending["detail"].update({"key_prefix": row["key_prefix"], "label": row["label"]})
    return {"ok": True, "id": key_id, "revoked_at": str(row["revoked_at"])}


@extras_router.post("/ext/v1/keys/recover", tags=["外部連携API"],
                    response_model=ExtKeyRecoverResponse,
                    responses={200: {"headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              403: {"description": "利用者による API キー発行が許可されていません",
                                    "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)},
                              422: ext_api._validation_error_response(
                                  "client_op_id が UUID 形式でない場合"),
                              401: {"description": "ログインが必要です（セッション Cookie）",
                                    "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)}})
def ext_self_key_recover(req: ExtKeyRecoverReq, request: Request,
                         x_request_id: str | None = ext_api._XRequestIdIn):
    """自己発行の `POST /ext/v1/keys` の応答が届かなかった場合の回復専用エンドポイント（自己発行版・本人のキーだけを照合）。機能トグルが OFF のときは拒否する（OFF になった時点で自己発行キーは一括失効済みのため、実質は常に `found: false`）。"""
    del x_request_id
    u = _current_user(request)
    _require_user_api_keys_allowed(u)
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_recover_attempted", "api_key")
    pending["detail"]["client_op_id"] = req.client_op_id
    row = store.revoke_unconfirmed_key_by_client_op_id(
        req.client_op_id, u["uid"], owner_uid=u["uid"])
    if row is None:
        pending["reason"] = "no_match"
        return {"found": False, "id": None, "revoked_at": None}
    pending["resource_id"] = str(row["id"])
    return {"found": True, "id": row["id"], "revoked_at": str(row["revoked_at"])}
