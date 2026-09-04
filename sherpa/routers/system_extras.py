"""システム系追加エンドポイント＋運営掲示板＋全体設定＋外部APIキー（フェーズ3スライス8・純移動）。

`GET /health/summary`・`GET /admin/health`・`GET /announcements`・`POST /admin/announcements`・
`PATCH /admin/announcements/{id}`・`DELETE /admin/announcements/{id}`・`GET /admin/settings`・
`PUT /admin/settings`・`POST/GET/DELETE /ext/v1/admin/keys`・`POST /ext/v1/admin/keys/recover`
（12ルート）を api.py から抽出する。ロジックは変更しない（コード移動のみ）。ルート表 golden の
定義順を保つため、api.py 側は `sherpa.routers.system` の `healthz_router` を include_router
した直後（`root_router` より前）に `app.include_router(system_extras.extras_router)` を1回
だけ置く。これらのルートは golden 上で連続しており、この1箇所の include で定義順は不変。

利用者本人の API キー自己発行/一覧/失効/回復 `POST/GET/DELETE /ext/v1/keys`・
`POST /ext/v1/keys/recover`（4ルート）を同じ include に追加（Cookie 認証＝`_current_user`
のみ・admin 不要）。

`_sweep_expired_announcements`（名前に反して maintenance 側・背景ポーラ/起動処理が使う）と
`_auth_bootstrap_on_startup` は api.py に残る（lifespan 起動処理のため）。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import logging
import os
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
    model_windows,
    notifications,
    research_service,
    store,
    usage_chat,
    webhooks,
    worlds,
)
from sherpa.agents import _web_search_admin_allowed
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

# router に tags を持たせない: 各エンドポイントの `tags=[...]` と結合されて二重化してしまう
# （ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す（system.py と同じパターン）。
extras_router = APIRouter()

_ANNOUNCEMENT_CATEGORIES = ("maintenance", "case", "notice")


class AnnouncementCreateReq(BaseModel):
    title: str
    body: str
    category: str = "notice"
    pinned: bool = False
    published: bool = True
    publish_at: str | None = None   # S4: ISO 8601 文字列。省略/空文字＝今すぐ公開扱い（NULL）
    expire_at: str | None = None    # S4: ISO 8601 文字列。省略/空文字＝無期限掲載（NULL）


class AnnouncementPatchReq(BaseModel):
    title: str | None = None
    body: str | None = None
    category: str | None = None
    pinned: bool | None = None
    published: bool | None = None
    # S4: 書込専用キー（openai_api_key 等）と同じ流儀＝未指定(None)は変更しない・""は NULL へクリア・
    # それ以外は ISO 8601 文字列として新しい値に更新する。
    publish_at: str | None = None
    expire_at: str | None = None


class _ModelCatalogCellReq(BaseModel):
    """`SystemSettingsReq.model_catalog[provider][usage]` の1セル（OpenAPI スキーマに形を反映させる
    ための型のみ・意味検証は `sherpa.model_catalog.validate_catalog` が行う）。"""
    allowed: list[str] = []
    default: str = ""


class SystemSettingsReq(BaseModel):
    """全体設定（system_settings）の部分更新（S1・admin のみ）。

    未指定のキーは変更しない・明示的に `null` を送るとそのキーを未設定へ戻す（env/既定へフォールバック）。
    「指定された」判定は `model_dump(exclude_unset=True)` で行うため、全フィールドの既定は `None`。
    W0（旧形式変換バックエンド）等で今後キーが増えても system_settings 自体は汎用 KV なので器は不変。
    """
    arms_enabled: list[str] | None = None      # 有効アーム名（既知名のみ）・空/未指定は env/既定へ
    legacy_backend: str | None = None          # 旧形式変換バックエンド（W0）: none|libreoffice・null は env/既定へ
    # L5（2026-09-02-RAG表現の全形式展開と文脈保持.md §8.6-1）: rag.md の LLM 成形トグル。
    # on|off・null は既定 off へフォールバック（2026-09-05 裁定・`legacy_backend` と同型）。
    rag_llm_render: str | None = None
    vlm: dict | None = None                    # 視覚読み取り（⑤ vision）の VLM 設定: {provider,model,cloud_allowed}・null は既定へ
    # R2a-S2（2026-07-13 横断レビュー対応）: Ollama 接続先の SSRF allowlist（host:port の配列）。
    # 既定（未設定=None）は loopback のみ許可（`llm.assert_ollama_url_allowed`）・null は未設定へ戻す。
    ollama_allowlist: list[str] | None = None
    # PART-6（2026-09-05-Webhook通知.md W3）: Webhook 宛先の SSRF allowlist（host:port の配列・
    # `ollama_allowlist` と同じ形）。既定（未設定=None）は loopback のみ許可
    # （`webhooks.assert_webhook_url_allowed`）・null は未設定へ戻す。
    webhook_allowlist: list[str] | None = None
    # R1b（2026-07-13 横断レビュー対応・Codex ネイティブ resume・決定5）: 会話ごとの Codex resume
    # セッション（workspace/.codex-sessions/{cid}）の保持日数。既定（未設定=None）は 0＝無制限
    # （`api._sweep_expired_codex_sessions` 参照）。null は未設定へ戻す（＝無制限に戻る）。
    # RV再検証 LOW-5: 素の `int` は pydantic の緩い型強制で `true`→1・`"14"`→14 のように暗黙変換
    # されてしまう（bool は int のサブクラス）。`StrictInt` で bool/文字列からの暗黙変換を拒否する
    # （`codex_web_search` に `StrictBool` を使っているのと同じ理由）。
    codex_session_retention_days: StrictInt | None = None
    # クラウド AI プロバイダの中央設定。
    # `cloud_provider` は openai/gemini/bedrock の排他選択（A7・既定 openai）。3つのキーは
    # 中央で保管する資格情報（`sherpa.keys.resolve_api_key` の唯一の真実源）。個人設定
    # （`user_settings`）とは別物＝非選択プロバイダの個人保存キーも中央キーも消さずに温存するが、
    # `sherpa.keys` は選択中のプロバイダ以外は常に None を返す。`ollama_url` はここでは
    # 中央の既定値（A7 排他対象外・個人設定の ollama_url が優先）。`personal_api_keys_allowed`
    # は A6（個人には発行しない原則）の唯一のスイッチ（既定 false）。
    cloud_provider: str | None = None
    personal_api_keys_allowed: StrictBool | None = None
    # WEB-1: Codex の Web 検索を許可するか（既定 false）。ON の間だけ、調べ方
    # ブロックの「Web 検索」行を表示し、チャットごとの希望（`ChatReq.web_search`）を尊重する
    # （`sherpa/providers/codex/sandbox.py::_web_search_admin_allowed` が唯一の読み手）。
    web_search_allowed: StrictBool | None = None
    # 利用者本人による外部連携 API キーの自己発行を許可するか（既定 false）。
    # `personal_api_keys_allowed` と同型のスイッチ・OFF に戻すたび（冪等）に利用者発行キーを
    # 一括失効する（設定変更と同一トランザクション・`store.apply_system_settings_and_revoke_if_disabled`）。
    user_api_keys_allowed: StrictBool | None = None
    # 自己発行キーの1日あたりの呼び出し上限（既定/上限を兼ねる）。未指定は組み込みの既定値
    # （`store.SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK`）を使う。利用者は発行時にこれ以下の
    # 値だけ指定できる（超える指定・空欄での無制限化は許さない）。admin 発行キーは対象外。
    user_api_keys_daily_quota_default: StrictInt | None = Field(default=None, ge=1, le=1_000_000)
    # PART-4a: AI 下調べ検索（POST /ext/v1/research）が model/provider 両方省略時に使う既定
    # プロバイダ（"ollama"/"openai"）。既定（未設定=None）は "ollama"（コスパ踏襲）。リクエストの
    # `provider` で明示指定すればここより優先される（`research_service.resolve_model_and_provider`
    # 参照）。
    research_default_provider: str | None = None
    openai_api_key: str | None = None
    gemini_api_key: str | None = None
    bedrock_api_key: str | None = None
    ollama_url: str | None = None
    # SET-2c（接続先の UI 移管）: OpenAI 互換 API の接続先。「接続先」欄（ラジオ・本家以外選択時のみ
    # 表示する base URL・認証ヘッダ形式・API バージョン）。null は未設定へ戻す（既定へフォールバック）。
    # 意味検証・実効値の解決は `sherpa/llm.py`（唯一の真実源）。
    openai_endpoint_kind: str | None = None       # openai(既定)/azure/custom
    openai_base_url: str | None = None
    openai_auth_header: str | None = None         # bearer(既定)/api-key
    openai_api_version: str | None = None
    # モデルカタログ（プロバイダ×用途ごとの「選べるモデル一覧＋既定」）。null は未設定へ戻す
    # （＝組み込み既定のみへ）。セル形状（allowed/default）は pydantic 型で表現し OpenAPI スキーマに
    # 反映させる。provider/usage 名の妥当性（既知集合・bedrock 除外）や default∈allowed の補正は
    # `sherpa.model_catalog.validate_catalog` が行う（dict[str, dict[str, ...]] のキー自体は
    # 任意文字列を許すため、キー側の意味検証はそちらに残す）。
    model_catalog: dict[str, dict[str, _ModelCatalogCellReq]] | None = None
    # Ollama の許可ホスト一覧は既存の `ollama_allowlist` をそのまま使う（新規フィールドは増やさない）。
    # STAT-2: 利用統計チャット（`POST /admin/usage/chat`）専用の AI 選択。利用者の実行構成
    # （`agent`）には依存せず、管理者全体で1つに統一する（"openai"|"ollama"）。null は未設定へ
    # 戻す（既定は固定値ではなく A7・`cloud_provider` 連動＝`usage_chat._default_provider` 参照）。
    # 空文字は明示的に 422（未設定へ戻すのは null のみ）。
    usage_chat_provider: str | None = None
    # SC-6c（調べる深さ・調べ方ブロック §3.2）: 標準時の基準値。既定（未指定=None）は各モジュールの
    # env 既定値（`sherpa/depth_profile.py::BASE_SETTINGS_KEYS` が対応する定数を列挙）。null は
    # 未設定へ戻す（env/既定へフォールバック）。倍率表自体（標準/深く/最大）は固定でここでは
    # 編集しない——編集できるのは「標準」が指す基準値のみ。
    depth_base_max_turns: StrictInt | None = Field(default=None, ge=1, le=200)
    depth_base_grep_max_hits: StrictInt | None = Field(default=None, ge=1, le=1000)
    depth_base_qa_max_hits: StrictInt | None = Field(default=None, ge=1, le=1000)
    depth_base_read_window: StrictInt | None = Field(default=None, ge=10, le=400)
    depth_base_impact_depth: StrictInt | None = Field(default=None, ge=1, le=64)
    depth_base_troubleshoot_depth: StrictInt | None = Field(default=None, ge=1, le=16)
    depth_base_codex_reasoning: str | None = None
    # BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4）: agentic search の
    # tool-result バイト予算を管理者設定へ昇格（SET-2「運用ポリシーは UI が唯一の持ち主」・
    # env フォールバックは ENV-CLEAN で撤去済み）。既定（未指定=None）はコード既定（精度優先・
    # §3.4 憲法1条＝262144/4194304）への フォールバック（`agentic_search.resolve_tool_result_
    # budgets()` が唯一の解決点）。範囲は元の env 側検証と同一（1件あたり=1024〜8MiB・
    # 1 run 累計=4096〜64MiB）。null は未設定へ戻す。
    agentic_budget_per_result: StrictInt | None = Field(default=None, ge=1024, le=8 * 1024 * 1024)
    agentic_budget_total: StrictInt | None = Field(default=None, ge=4096, le=64 * 1024 * 1024)
    # BUDGET-2（§3.4・2026-09-03 裁定）: モデル名→窓 tokens の管理者登録（"provider:model" キー・
    # 追加/上書き/削除）。意味検証は `sherpa.model_windows.validate_model_windows`（`_validate_
    # model_windows` が 422 へ変換）。null は未設定へ戻す（以後はプロバイダAPI/シード表/不明段のみ）。
    model_context_windows: dict[str, StrictInt] | None = None
    # チャット画面のクイック入力例（ウェルカム画面のチップ）のカスタマイズ。`{enabled, items}`
    # （意味検証は `sherpa.chat_examples.validate`・`_validate_chat_examples` が 422 へ変換）。
    # null は未設定へ戻す（既定＝表示・組み込み4例）。
    chat_examples: dict | None = None


def _normalize_world_list_field(v: list[str] | None) -> list[str] | None:
    """`allowed_worlds` の形式検証（識別子として妥当か・重複除去）。`ExtKeyCreateReq`・
    `ExtSelfKeyCreateReq` の双方の field_validator から呼ぶ共通本体（実在検証は各ハンドラ側・
    DB/走査を要するため）。None（未指定）はそのまま返す＝全 world 許可（既存キーと同じ後方互換）。
    """
    if v is None:
        return v
    for w in v:
        if not worlds.valid_world(w):
            raise ValueError(f"world 識別子が不正です: {w}")
    seen, out = set(), []                 # 重複除去（順序維持）
    for w in v:
        if w not in seen:
            seen.add(w)
            out.append(w)
    return out


# daily_quota の上限（DB は INTEGER 列＝この範囲を超える値は CHECK 制約で拒否されるが、
# ここで先に 422 にすることで整数オーバーフロー起因の 500 を防ぐ）。
_DAILY_QUOTA_MAX = 1_000_000

def _validate_client_op_id_format(v: str) -> str:
    """`client_op_id` を UUID として解析し、標準の小文字正準形（8-4-4-4-12・ハイフン区切り）へ
    正規化する。DB 側の非NULL部分一意制約（`api_keys_client_op_id_unique`）と組み合わせて
    「この1回の発行操作」を一意に指す前提のため、固定値の使い回し等で衝突しやすい任意の
    自由文字列は受け付けない。大小文字表記の違い（例: 'ABCD...' と 'abcd...'）は同じ UUID を
    指すため、正規化せずに保存すると一意制約・回復時の照合を大小文字の書き分けで迂回できて
    しまう——ここで常に正準小文字形へ揃えて以後の全経路（DB・回復API）に渡すことで、
    表記の違いが別の値として扱われる余地を無くす。"""
    try:
        return str(uuid.UUID(v))
    except (ValueError, AttributeError, TypeError) as e:
        raise ValueError("client_op_id は UUID 形式（例: 123e4567-e89b-12d3-a456-426614174000）で"
                         "指定してください") from e


class ExtKeyCreateReq(BaseModel):
    """外部連携 API キー発行（/ext/v1・admin のみ）。"""
    label: str = Field(min_length=1, max_length=100)
    # world スコープ（オプトイン）。未指定/null＝全 world 許可（既存キーと同じ後方互換）。
    # 空リストは「どの world にもアクセスできない」キー（意図的な選択・拒否はしない）。
    # 形式検証はここ（識別子として妥当か）・実在検証は `ext_key_create` 側（DB/走査を要するため）。
    allowed_worlds: list[str] | None = None
    # 有効期限（ISO 8601 文字列・announcements の publish_at/expire_at と同じ流儀）・
    # 日次クォータ（任意・1〜_DAILY_QUOTA_MAX の整数）。いずれも省略/null＝後方互換（無期限・無制限）。
    expires_at: str | None = None
    daily_quota: StrictInt | None = Field(default=None, ge=1, le=_DAILY_QUOTA_MAX)
    # 発行 UI が生成する相関トークン（任意・秘密ではない・UUID 形式のみ）。POST 応答が失われた
    # 場合に、UI が回復専用エンドポイント（`ext_key_recover`）でこの値を照合して自動失効する。
    client_op_id: str | None = Field(default=None, max_length=100)
    # PART-6（Webhook 通知・オプトイン）: 取り込み run の terminal 化を受け取る宛先 URL。
    # 未指定/null＝Webhook 無効。意味検証（http/https・宛先ポリシー）は `ext_key_create` 側
    # （`webhooks.assert_webhook_url_allowed`・DB の allowlist を読むため）。
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
    """利用者本人による外部連携 API キー発行。`system_settings.user_api_keys_allowed` が
    true のときのみ受理される。"""
    label: str = Field(min_length=1, max_length=100)
    # 本人がアクセスできる範囲 ⊆ に強制する（`_enforce_self_world_scope` 側で検証・
    # `worlds.accessible_world_ids` 経由）。ここでは形式検証のみ（`ExtKeyCreateReq` と同型）。
    allowed_worlds: list[str] | None = None
    expires_at: str | None = None
    # 未指定/null は管理者の現在の既定を適用・指定値が現在の上限を超える場合は422（`store.
    # insert_api_key` がロック内で DB から再読して確定する＝ここでの上限は入力の型検証のみ）。
    daily_quota: StrictInt | None = Field(default=None, ge=1, le=_DAILY_QUOTA_MAX)
    client_op_id: str | None = Field(default=None, max_length=100)
    # PART-6: `ExtKeyCreateReq.webhook_url` と同型（利用者自己発行キーにも同じく使える）。
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
    """曖昧な発行結果（POST 応答が届かなかった等）の回復専用リクエスト
    （`ext_key_recover`/`ext_self_key_recover` 共通）。"""
    client_op_id: str = Field(min_length=1, max_length=100)

    @field_validator("client_op_id")
    @classmethod
    def _v_client_op_id(cls, v):
        return _validate_client_op_id_format(v)


@extras_router.get("/health/summary", tags=["システム"], response_model=HealthSummaryResponse)
def health_summary(request: Request):
    """バックエンド健全性のサマリ（全画面の状態ドット用・ログイン必須）。

    `_current_user` 自体が Postgres（session_user）に依存するため、Postgres 停止時
    （＝一番この結果を必要とする時）にここで例外化しないよう try で包む。未ログイン
    （HTTPException）はそのまま re-raise、それ以外（認証DB到達不可）は down を返す。
    """
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
    """コンポーネント別の健全性詳細（システム状態画面用・admin 専用）。

    admin 確認自体が Postgres に依存するため、認証DB到達不可時は admin かどうか
    判定できない＝詳細（DSN 等が漏れうる情報）は返さず 503 のみ返す。

    UI フィードバック4（2026-07-03）: AI（openai/gemini/bedrock/ollama/codex）は状態ドット用の
    軽量チェック（env の有無/バイナリ有無だけ・per-user キーは見ない）ではなく、**この管理者本人が
    設定画面で入れた API キーも含めて実際に1回だけ接続確認**した結果に差し替える
    （`health.ai_snapshot`・per-uid キャッシュ＝自動ポーリングで実 API 呼び出しを連発しない）。

    AI との切り分け用に、登録 world への ES/グラフ実クエリ検索テスト（`health.search_snapshot`）を
    2行追加する（同じ per-uid TTL キャッシュ＝自動ポーリングでは TTL 内で最大1回/60秒に抑え、
    「再チェック」は `force=True` で必ず最新化する）。
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
    ai_ids = {"openai", "gemini", "bedrock", "ollama", "codex"}
    ai_rows = health.ai_snapshot(u["uid"], store.get_settings(u["uid"]), force=refresh)
    search_rows = health.search_snapshot(u["uid"], force=refresh)
    components = [c for c in data["components"] if c["id"] not in ai_ids] + ai_rows + search_rows
    return {**data, "components": components}


@extras_router.get("/notifications", tags=["システム"])
def notifications_list(request: Request):
    """非同期処理の完了/要対応の通知（NOTIFY-1・ホーム画面「通知」区画用・ログイン必須）。

    誰でも取り込み run の完了/失敗が見える。admin はさらにグラフ drift・LLM 成形完了・OCR
    反映待ちも見える（`notifications.list_notifications` が role で絞る）。既読管理はしない
    （毎回現在の状態から組み立てて返す）。
    """
    u = _current_user(request)
    return {"notifications": notifications.list_notifications(is_admin=u.get("role") == "admin")}


# ===== 運営掲示板（2026-07-02-利用統計とホーム掲示板.md Feature 2・S4 公開/削除タイマー） =====

def _parse_announcement_dt(value: str | None, field_label: str) -> datetime | None:
    """publish_at/expire_at の入力（ISO 8601 文字列）をパースする。空/未指定は None。

    naive（tzinfo 無し）は UTC 扱いに統一する（`ShareCreateReq.expires_at` と同じ流儀）。
    不正な形式は 422（フロントは datetime-local→`Date#toISOString()` で常にオフセット付きを送る想定）。
    """
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
    """admin 向けの状態バッジ用（S4）: unpublished / scheduled（予約公開待ち）/ expired（掲載終了）/ active（公開中）。

    RV3（2026-07）: `now` は呼び出し側で1回だけ計算して渡す（行ごとに `datetime.now()` を呼ぶと、
    応答生成の途中で時刻が進んで境界付近の行だけ判定がドリフトし得るため・一覧は必ず同一 now で揃える）。
    """
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
    """お知らせ一覧（ログイン必須・既定は公開済みのみ・ピン留め優先→新着順）。

    `include_unpublished=true` は admin 専用（非公開記事を再発見して再公開できるようにするため。
    指定時のみ admin ゲートを課す・非 admin が指定しても 403）。
    """
    u = _current_user(request)   # ログイン必須（auth 有効時）
    if include_unpublished:
        _require_admin(u)
    rows = store.list_announcements(limit=limit, offset=offset, published_only=not include_unpublished)
    now = datetime.now(timezone.utc)   # RV3: 全行で同一の now を使う（行ごとのドリフト防止）
    return {"announcements": [_announcement_out(r, now) for r in rows]}


@extras_router.post("/admin/announcements", tags=["運営掲示板"], response_model=AnnouncementMutateResponse)
def announcement_create(req: AnnouncementCreateReq, request: Request):
    """お知らせを新規作成（管理者のみ）。監査は fail-closed（share.created と同じ compensate パターン:
    書けなければ作成を取り消し 500 を返す＝「監査できない変更が成功したまま残る」状態を作らない）。"""
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
    """お知らせを部分更新（管理者のみ）。`published=false` で非公開化。
    監査は fail-closed（書けなければ更新前の状態へ復元し 500 を返す）。"""
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
    # S4: publish_at/expire_at は書込専用キーと同じ流儀＝未指定(None)は kwarg 自体を渡さない
    # （store 側の _UNSET 既定＝変更しない）。""は明示的に None（NULLへクリア）として渡す。
    dt_kwargs = {}
    if req.publish_at is not None:
        dt_kwargs["publish_at"] = _parse_announcement_dt(req.publish_at, "公開日時")
    if req.expire_at is not None:
        dt_kwargs["expire_at"] = _parse_announcement_dt(req.expire_at, "掲載終了日時")
    # RV1（2026-07・並行更新対策）: publish_at/expire_at の順序検証は `before`（この時点で既に古いかも
    # しれないスナップショット）ではなく、update_announcement 内の SELECT...FOR UPDATE で取得した
    # ロック済みの現在値に対して行う（2並行 PATCH が別々のフィールドを更新して単体検証をすり抜け、
    # 矛盾した状態が永続化される競合を防ぐ）。ここでは呼ばずに store 層へ委譲する。
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
            # RV ラウンド2: updated_at まで含めて完全に before へ戻す
            # （update_announcement は updated_at=now() を必ず打つため使わない）。
            store.restore_announcement_state(id, before)
        except Exception:
            _log.critical("compensating restore also failed for announcement %s – manual cleanup required", id)
        raise HTTPException(500, "お知らせの更新中にエラーが発生しました")
    return {"ok": True, "announcement": _announcement_out(row, datetime.now(timezone.utc))}


@extras_router.delete("/admin/announcements/{id}", tags=["運営掲示板"])
def announcement_delete(id: int, request: Request):
    """お知らせを削除（管理者のみ）。監査は fail-closed（書けなければ id/created_at/updated_at を
    含めて完全に復元し 500 を返す。RV ラウンド2: 単純な再作成だと id/created_at が変わってしまうため
    store.restore_announcement で before スナップショットどおりに復元する）。"""
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


# ===== 全体設定（system_settings・admin のみ・S1・2026-07-08-設定分離とUI整備.md）=====

_ENDPOINT_TEST_TIMEOUT_S = 10   # 接続テスト専用の短いタイムアウト（秒）。到達不能を素早く申告する

def _current_chat_provider_model(sysset: dict) -> tuple[str, str]:
    """BUDGET-2（§3.4）: 管理画面「検索1回あたりの情報量」カードに表示する「現在のモデル」——
    システム既定のチャット/エージェント用の頭脳（個人設定の上書きは見ない・本ビューは全体設定の
    ビューのため `agent_constructs.effective_agent({}, ...)` で「利用者設定なし」を渡す）。

    GET /admin/settings を絶対に壊さない（例外はどこでも握りつぶし、解決できなければ空文字を返す
    ＝呼び出し側は "unknown" として扱う）。"""
    try:
        from sherpa import agent_constructs
        agent = agent_constructs.effective_agent({}, system_settings=sysset, strict=False)
    except Exception:
        return ("", "")
    if not isinstance(agent, str) or agent not in ("openai", "ollama", "gemini", "bedrock"):
        return (agent if isinstance(agent, str) else "", "")
    try:
        if agent == "bedrock":
            # `bedrock_model` は利用者個人設定（`store.get_settings()`）の項目で system_settings
            # には無い（`model_catalog` も bedrock を対象外にしている・モジュール docstring 参照）。
            # 本ビューは特定利用者に紐付かないため、組み込み既定モデルで代表させる（個人上書きは
            # このヒント表示には反映されない・実行時の実際の予算計算は別途 provider/model を渡す
            # 呼び出し元が正しい値を使う）。
            from sherpa import agents
            mod = agents._BEDROCK_MODEL
        else:
            mod = model_catalog.resolve_model(agent, "chat", None, system_settings=sysset)
    except Exception:
        mod = ""
    return (agent, mod or "")


def _admin_settings_view() -> dict:
    """GET/PUT 共通の応答＝現行値（system_settings の生値）＋実効値（env/既定込みの解決結果）。

    UI は実効値でチェック/表を描画し、`configured`（生値・未設定なら null）で「既定に従っているか」を判別し、
    `env_default`/`default` で「未設定に戻したら何になるか」を示す（プレースホルダ表示）。
    """
    from sherpa import agentic_search, chat_service, impact_service, keys, lens_service, llm
    from sherpa.ingest import arms as ingest_arms
    from sherpa.ingest import llm_render
    from sherpa.ingest.arms import legacy_convert, vision_arm
    sysset = store.get_system_settings()
    # `sysset["openai_base_url"]`/`sysset["openai_endpoint_kind"]` は JSONB のため型を
    # 強制されず、非文字列の破損値もあり得る。`llm.openai_endpoint_kind()`/`llm.openai_base_url()`
    # はどちらの分岐（kind=openai／未設定を含む）でも型検査を判定より先に行い、不正なら
    # `ValueError` を送出する契約のため、両方を同じ try で解決する（`openai_endpoint_
    # kind()` を個別に呼び直すと、型検査の結果（成功/失敗）が呼び出しごとに揃わなくなる）。
    # 管理画面が生値を確認・修正できるよう表示は落とさず固定文字列へ倒す
    # （`system.py::_INVALID_SAVED_BASE_URL_LABEL` と同じ流儀）。
    # `openai_auth_header_style()`/`openai_api_version()` も内部で `openai_endpoint_kind()` を
    # 呼ぶため、同じ try に含める（呼び出しごとに型検査の成否が食い違わないようにする）。
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
    # `research_default_provider` も同じ流儀（型検査を判定より先に行い ValueError を送出する
    # 契約）——PUT 側で不正値は 422 で弾いているため通常は起きないが、保存後に何らかの経路で
    # 壊れた値があっても管理画面全体を 500 にしない。
    try:
        eff_research_default_provider = research_service.default_research_provider(sysset)
    except ValueError:
        eff_research_default_provider = "(不正な保存値)"
    # BUDGET-2（§3.4）: 「現在のモデル」の窓解決（登録値 > シード表 > 不明・GET はここでは段2
    # 「プロバイダAPI」を意図的に呼ばない——`GET /admin/settings` はページ表示のたびに叩かれる
    # 経路であり、Ollama `/api/show` への実ネットワーク I/O をここに乗せると (1) 管理画面の表示が
    # 外部プロバイダの応答待ちでブロックされ、(2) 実 Ollama が動く開発機でテストを流すと本物の
    # 通信が発生してしまう（受け入れ条件「実プロバイダへの通信はテストから発生しない」に抵触）。
    # ライブ照会は実行時の実際の予算計算（`agentic_search.resolve_tool_result_budgets` が
    # `openai_style`/`anthropic_style` から呼ぶ経路・run 開始時に1回だけ）でのみ行う——そちらは
    # 元々そのモデルへ実際に接続する run の一部であり、新たな I/O 経路を増やすものではない。
    # `ollama_base_url`/`anthropic_client` を省略するだけで `resolve_window_tokens` は段2を自動的に
    # スキップする（fail-safe な設計）。GET を絶対に壊さない（例外はどこでも握りつぶし「不明」に
    # 倒す）。
    _window_provider, _window_model = _current_chat_provider_model(sysset)
    try:
        _window_tokens, _window_source = model_windows.resolve_window_tokens(
            _window_provider, _window_model, system_settings=sysset)
    except Exception:
        _window_tokens, _window_source = None, "unknown"
    return {
        # クラウド AI プロバイダの中央設定。key_set はキーの値そのもの
        # を返さず有無のみ（他の *_key_set と同じ流儀）。`provider` は A7 の現在選択・`providers` は
        # 選べる値の一覧（画面はこれで <select> を描画する）。
        "cloud": {
            "provider": keys.selected_cloud_provider(sysset),
            # FBK-1 RV1: 生の保存値（未選択＝一度も PUT されていなければ None）。UI はこれで
            # 「admin が実際にラジオを操作したか」を判別する（`provider` は既定込みの実効値のため、
            # 初期表示の既定 openai と明示選択した openai を区別できない）。
            "provider_raw": keys.cloud_provider_raw(sysset),
            "providers": list(keys.CLOUD_PROVIDERS),
            "personal_api_keys_allowed": keys.personal_keys_allowed(sysset),
            "openai_key_set": bool(sysset.get("openai_api_key")),
            "gemini_key_set": bool(sysset.get("gemini_api_key")),
            "bedrock_key_set": bool(sysset.get("bedrock_api_key")),
            "ollama_url": sysset.get("ollama_url") or keys.DEFAULT_OLLAMA_URL,
            # 個人秘密キーを保存中のユーザー数（A6 を OFF で保存すると一括削除される・画面が
            # 保存前の確認ダイアログに件数を出すためのプレビュー）。
            "personal_keys_in_use_count": store.count_users_with_personal_keys(),
            # WEB-1: Codex の Web 検索を管理者が許可しているか（既定 false）。
            # チャットの調べ方ブロック（web/chat/menus.js）はこれと現在の頭脳（Codex＋OpenAI直結）
            # の両方が揃った時だけ Web 検索行を表示する（個人設定 `GET /settings` の
            # `web_search_available` と同じ実効値）。
            "web_search_allowed": _web_search_admin_allowed(sysset),
        },
        # 利用者本人による外部連携 API キー自己発行の許可トグル（既定 false・
        # personal_api_keys_allowed と同型）。`self_issued_active_count` は OFF で保存する前の
        # 確認ダイアログ用プレビュー（`personal_keys_in_use_count` と同型・失効/期限切れは除く）。
        # `daily_quota_default` は自己発行キーの1日あたり呼び出し上限の既定/上限（`configured` は
        # 管理者の生値・`effective` は未設定時のフォールバック込みの実際に適用される値・`default`
        # は組み込みのフォールバック値そのもの＝管理画面の差分強調の基準）。
        "ext_keys": {
            "user_api_keys_allowed": bool(sysset.get("user_api_keys_allowed") or False),
            "self_issued_active_count": store.count_self_issued_active_api_keys(),
            "daily_quota_default": {
                "configured": sysset.get("user_api_keys_daily_quota_default"),
                "effective": store.resolve_self_issued_daily_quota_cap(sysset),
                "default": store.SELF_ISSUED_DAILY_QUOTA_DEFAULT_FALLBACK,
            },
            # PART-4a: AI 下調べ検索（POST /ext/v1/research）の既定 AI（`configured`=管理者の生値・
            # `effective`=未設定時のフォールバック込みの実際に適用される値・`default`=組み込みの
            # フォールバック値そのもの＝差分強調の基準）。
            "research_default_provider": {
                "configured": sysset.get("research_default_provider"),
                "effective": eff_research_default_provider,
                "default": "ollama",
            },
        },
        # SET-2c: OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換）。`configured` は
        # admin が実際に保存した生値（未設定なら None）、`effective` は `sherpa/llm.py`（唯一の真実源）
        # による解決結果。base URL はここでは伏せない（管理画面の入力欄そのものであり、個人設定の
        # 読み取り専用表示（ホスト名のみ）とは別の面）。
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
        # 使えるモデル一覧＋用途別既定。管理画面は「選択中のクラウドプロバイダ＋Ollama＋Codex」の
        # 列だけを描く（冗長化対策）。`effective` は組み込み既定に管理者設定を重ねた解決結果
        # （セル単位）、`configured` は管理者が実際に保存した生値（未設定なら null）。
        "model_catalog": {
            "configured": sysset.get("model_catalog"),
            "effective": model_catalog.get_catalog(sysset),
            # 組み込み既定のみ（管理者設定を一切重ねない）。管理画面が「セルの値が既定と異なるか」を
            # 判定する基準（`configured` にセルが存在するだけでは、既定と同じ値を明示保存した場合を
            # 区別できない）。
            "builtin": model_catalog.get_catalog({}),
            "providers": list(model_catalog.PROVIDERS),
            "usages": list(model_catalog.USAGES),
        },
        "arms": {
            "known": ingest_arms.known_arm_names(),
            "enabled": ingest_arms.enabled_arm_names(),         # 実効（system_settings 反映済）
            "configured": sysset.get("arms_enabled"),           # 全体設定の生値（未設定=None＝env/既定）
            "env_default": ingest_arms.env_default_arm_names(),  # 未設定に戻したときの実効（env/既定）
            "available": ingest_arms.arm_availability(),         # 各アームがこの端末で実際に使えるか（未導入案内用）
        },
        "legacy_backend": {
            "configured": sysset.get("legacy_backend"),          # 生値（未設定=None＝env/既定に従う）
            "effective": legacy_convert.legacy_backend_name(),   # system>env>既定（none|libreoffice|office_com）
            "default": legacy_convert.env_default_backend(),     # 未設定に戻したときの実効（env/既定）
            "options": list(legacy_convert.BACKEND_OPTIONS),     # 選択肢（none|libreoffice|office_com）
            "libreoffice": {
                "available": legacy_convert.soffice_available(),  # soffice 検出の有無
                "version": legacy_convert.soffice_version(),      # 検出時のバージョン（未検出は None）
            },
            # W1/W2'（2026-07-08-旧Office変換2系統.md・feedback-batch-2026-07-08 ⑥）: office_com の到達性と動作形態。
            #   mode="direct"（同一マシン・URL 未設定かつ powershell 検出＝既定）｜"http"（別ホストのワーカー・
            #   URL 設定済み）｜"unavailable"（どちらも無し）。powershell は direct の検出状態（同一マシンで
            #   すぐ使えるか）。configured_url で「URL 未設定」と「設定済みだが不達」を UI が区別できる。
            #   versions は healthz の各 Office バージョン（不達/未検出なら None）。
            "office_com": {
                "configured_url": legacy_convert.office_com_configured(),
                "mode": legacy_convert.office_com_mode(),
                "powershell": legacy_convert.powershell_available(),
                "available": legacy_convert.office_com_available(),
                "versions": (legacy_convert.office_com_healthz() or {}).get("versions"),
            },
        },
        # L5（2026-09-02-RAG表現の全形式展開と文脈保持.md §8.6-1）: rag.md の LLM 成形トグル。
        # 既定 off（2026-09-05 裁定＝実測で文体整形のみ・コスト不釣合）。ON でも規則版と両立・既存の成形版は残る。
        "rag_llm_render": {
            "configured": sysset.get("rag_llm_render"),           # 生値（未設定=None＝既定に従う）
            "effective": llm_render.rag_llm_render_enabled(),     # system>既定（bool）
            "default": llm_render.env_default_enabled(),          # 未設定に戻したときの実効（コード既定）
            "options": ["on", "off"],
        },
        # ⑤（feedback-batch-2026-07-08）: 視覚読み取り（vision）の VLM 設定。既定＝ローカル（Ollama）・
        # クラウド（OpenAI）は cloud_allowed=true（管理者が明示許可）のときだけ有効（INGEST-MD 決定3）。
        "vlm": {
            "configured": sysset.get("vlm"),                     # 生値（未設定=None＝既定へ）
            # 解決結果（provider/model/cloud_allowed/ollama_url）。provider/model は system>既定
            # （env フォールバックは ENV-CLEAN で撤去済み）・ollama_url は env のまま（UI 項目なし）。
            "effective": vision_arm.vlm_config(),
            "default": vision_arm.env_default_vlm(),     # 未設定に戻したときの実効（cloud は常に false）
            "available": vision_arm.vlm_usable(),        # 実効的に使えるか（ネットワーク I/O なし）
            "providers": list(vision_arm._KNOWN_PROVIDERS),   # ローカル(ollama)/クラウド(openai)
            "openai_key_present": bool(vision_arm._openai_key()),   # クラウド選択時のキー未設定案内用
        },
        # R2a-S2（2026-07-13 横断レビュー対応）: Ollama 接続先の SSRF allowlist。loopback（localhost・
        # 127.0.0.0/8・::1）は allowlist の有無に関わらず常に暗黙許可されるため、ここに出るのは
        # それ以外（RFC1918 含む非 loopback）の許可先のみ（`llm.assert_ollama_url_allowed` 参照）。
        "ollama_allowlist": {
            "configured": sysset.get("ollama_allowlist"),   # 生値（未設定=None＝loopback のみ許可）
            # 実際に許可される非 loopback 接続先（DB の admin allowlist のみ・env はここへ加算しない＝
            # 初回シード時の一度きりの追加を除き、env は起動後この一覧に影響しない・
            # `llm._allowlisted_hosts()` 参照）。値そのものに秘密情報は含まない（host:port のみ）。
            "effective": sorted(f"{h}:{p}" for h, p in llm._allowlisted_hosts(sysset)),
        },
        # PART-6（W3）: Webhook 宛先の SSRF allowlist。`ollama_allowlist` と同型（loopback は
        # allowlist の有無に関わらず常に暗黙許可される・`webhooks.assert_webhook_url_allowed` 参照）。
        "webhook_allowlist": {
            "configured": sysset.get("webhook_allowlist"),
            "effective": sorted(f"{h}:{p}" for h, p in webhooks._allowlisted_hosts(sysset)),
        },
        # R1b（Codex ネイティブ resume・決定5）: 会話ごとの Codex resume セッションの保持日数。
        # 0（既定・未設定）＝無制限（`api._sweep_expired_codex_sessions` が対象外としてスキップする）。
        "codex_session_retention_days": {
            "configured": sysset.get("codex_session_retention_days"),   # 生値（未設定=None＝無制限）
            "effective": int(sysset.get("codex_session_retention_days") or 0),
        },
        # STAT-2: 利用統計チャット専用の AI 選択（利用者の実行構成には依存しない・管理者全体で統一）。
        # `effective`/`default` は A7（`cloud_provider`）連動（`usage_chat._default_provider`/
        # `_effective_provider_for_display` 参照）。保存済み `usage_chat_provider` が不正でも
        # 表示自体は落とさない（実送信時の fail-closed 判定は `_resolve_cfg` の責務・ここでは
        # 表示用のベストエフォート値を返す）。
        "usage_chat": {
            "configured": sysset.get("usage_chat_provider"),
            "effective": usage_chat._effective_provider_for_display(sysset),
            "default": usage_chat._default_provider(sysset),
            "providers": list(usage_chat._USAGE_CHAT_PROVIDERS),
        },
        # SC-6c（調べる深さ・調べ方ブロック §3.2）: 「標準」が指す基準値。`effective` は
        # `depth_profile.effective_base()`（system_settings→env→コード既定）の解決結果、
        # `default` は env/コード既定（未設定に戻したときの実効値）。倍率表自体（標準/深く/最大）は
        # 固定で管理画面に出さない（§9・編集できるのは基準値のみ）。
        "depth_profile": {
            "max_turns": {
                "configured": sysset.get("depth_base_max_turns"),
                "effective": depth_profile.effective_base(sysset, "max_turns", agentic_search.MAX_TURNS),
                "default": agentic_search.MAX_TURNS,
            },
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
                    sysset, "codex_reasoning", os.environ.get("SHERPA_CODEX_REASONING", "low")),
                "default": os.environ.get("SHERPA_CODEX_REASONING", "low"),
                "options": list(depth_profile.CODEX_REASONING_LEVELS),
            },
        },
        # BUDGET-1（2026-09-02-RAG表現の全形式展開と文脈保持.md §3.4）: agentic search の
        # tool-result バイト予算を管理者設定へ昇格（env フォールバックは撤去済み・ENV-CLEAN）。
        # `effective` は `agentic_search.resolve_tool_result_budgets()`（settings > コード既定→
        # BUDGET-2 の窓由来上限との min()）の解決結果、`default` はコード既定（未設定に戻したときの
        # 実効値・窓連動を含まない）。既定は精度優先（憲法1条）。
        # BUDGET-2（§3.4・2026-09-03 裁定）: `effective` は「現在のモデル」（`_current_chat_
        # provider_model`）を渡して解決するため、窓が判明していれば min() 済みの値になる
        # （窓が不明なら BUDGET-1 のみの値のまま＝退行しない）。
        "agentic_budget": {
            "per_result": {
                "configured": sysset.get("agentic_budget_per_result"),
                "effective": agentic_search.effective_tool_result_max_bytes(
                    sysset, provider=_window_provider or None, model=_window_model or None),
                "default": agentic_search.TOOL_RESULT_MAX_BYTES,
            },
            "total": {
                "configured": sysset.get("agentic_budget_total"),
                "effective": agentic_search.effective_tool_result_max_total_bytes(
                    sysset, provider=_window_provider or None, model=_window_model or None),
                "default": agentic_search.TOOL_RESULT_MAX_TOTAL_BYTES,
            },
            # BUDGET-2: 現在のモデルの窓解決結果（ヒント表示用）。`window_tokens`/`derived_cap_bytes`
            # は `source` が "unknown" のときのみ None（管理画面はこのとき「窓が未登録です」の
            # 平文案内＋登録欄を出す）。
            "window": {
                "provider": _window_provider,
                "model": _window_model,
                "window_tokens": _window_tokens,
                "source": _window_source,
                "derived_cap_bytes": (model_windows.derive_window_bytes(_window_tokens)
                                      if _window_tokens is not None else None),
            },
            # モデル名→窓 tokens の管理者登録値（"provider:model" キー・追加/上書き/削除は PUT
            # `model_context_windows`）。`configured` はそのままの生値（未設定なら null）。
            "model_windows": {"configured": sysset.get(model_windows.MODEL_WINDOWS_KEY)},
        },
        # チャット画面のクイック入力例（ウェルカム画面のチップ）。`configured` は生値（未設定なら
        # null）、`effective` は実際に表示される内容（非表示なら空リスト）、`default` は組み込み既定
        # （未設定に戻したときに使われる4例・`GET /settings` の非 admin 向け応答はこの既定文言自体は
        # 返さない＝フロントの組み込み既定 `web/chat/state.js::DEFAULT_EXAMPLES` と一致させる必要が
        # あるため、値がずれていないか確認する参考表示として返す）。
        "chat_examples": {
            "configured": sysset.get("chat_examples"),
            "effective": chat_examples.effective_examples(sysset),
            "default": list(chat_examples.DEFAULT_ITEMS),
            "max_items": chat_examples.MAX_ITEMS,
            "max_item_length": chat_examples.MAX_ITEM_LENGTH,
        },
    }


def _validate_arms_enabled(value):
    """`arms_enabled` の検証。None/空リストは None（未設定＝env/既定へフォールバック・S1）。
    list は既知アーム名のみ許可し、重複を畳んで返す。未知名・型不正は 422。"""
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
    return list(dict.fromkeys(names)) or None   # 重複除去・空は未設定扱い（env/既定へ）


def _validate_legacy_backend(value):
    """`legacy_backend`（W0/W1）の検証。None は未設定（env/既定へフォールバック）。

    許可は none|libreoffice|office_com（`legacy_convert.KNOWN_BACKENDS`・W1 で office_com 追加）。未知値は 422。
    `none` は「明示的に変換しない」という有効な選択（未設定へは畳まず生値のまま保存する＝env が libreoffice の
    ときでも明示的な none を尊重する）。office_com を選んでもワーカー不達なら実効は変換不可へ倒れる（fail-safe・
    保存自体は許可＝ワーカー起動待ちの間も設定を保持できる）。
    """
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
    return name


def _validate_rag_llm_render(value):
    """`rag_llm_render`（L5）の検証。None は未設定（既定 off へフォールバック）。許可は on|off のみ
    （`_validate_legacy_backend` と同型・大文字小文字は無視）。"""
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
    """`vlm`（⑤ vision の視覚モデル設定）の検証。None/空 dict は None（未設定＝既定へ）。

    受理キーは `provider`（"ollama"|"openai"）・`model`（非空文字列）・`cloud_allowed`（bool）のみ。型不正・
    未知 provider・未知キーは 422。**cloud_allowed の既定は false**（未指定なら保存しない＝resolve 側で false へ）。
    provider=openai の保存自体は許可する（cloud_allowed=false のままなら実効は無効＝画像を送らない・fail-safe）。
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
    return out or None                                       # 空 dict は未設定扱い（既定へ）


def _validate_ollama_allowlist(value):
    """`ollama_allowlist`（R2a-S2・Ollama 接続先 SSRF allowlist）の検証。None/空リストは None
    （未設定＝loopback のみ許可へフォールバック・`llm.assert_ollama_url_allowed` 参照）。

    各エントリは scheme・空白を含まない `host[:port]` 形式の文字列のみ許可し、
    `llm._canonical_host_port` で正規化した `host:port` 文字列に整えて保存する（書込時に正規化して
    おくことで、読取側 `llm._allowlisted_hosts()` の比較が単純な文字列一致で済む＝ポート省略等の
    表記ゆれで allowlist を迂回できないようにする）。不正な形式・解釈不能なホストは 422。
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
        # RV Low（2026-07-14）: userinfo（@）・path（/）・query（?）・fragment（#）を拒否する。
        # `_canonical_host_port` は hostname だけ取り出すため `127.0.0.1@evil:11434` を `evil:11434` に
        # 黙って丸めてしまい、admin の誤登録（別ホストを allowlist に入れる）を誘発する。SSRF 迂回では
        # ないが、host:port 形式を厳密化して入力ミスを 422 で弾く。
        if not e or "://" in e or any(c.isspace() for c in e) or any(c in e for c in "@/?#"):
            raise HTTPException(422, f"不正な接続先です: {entry!r}（scheme/userinfo/path/空白を含まない host:port 形式で指定してください）")
        hp = llm._canonical_host_port(f"http://{e}")
        if hp is None:
            raise HTTPException(422, f"不正な接続先です: {entry!r}（host:port 形式で指定してください）")
        out.append(llm.format_host_port(hp[0], hp[1]))   # IPv6 は角括弧付き＝読取側の再パースと round-trip
    return list(dict.fromkeys(out)) or None                  # 重複除去・空は未設定扱い（loopback のみ許可へ）


def _validate_webhook_allowlist(value):
    """`webhook_allowlist`（PART-6・W3・Webhook 宛先 SSRF allowlist）の検証。
    `_validate_ollama_allowlist` と同形式（各エントリは host:port のみ・scheme/userinfo/path/
    空白は拒否）——allowlist のエントリ自体は Webhook URL 本体と違い path/query を持たない
    （宛先の起点だけを登録する）ため、`llm._canonical_host_port` の path 拒否契約と衝突しない。
    None/空リストは None（未設定＝loopback のみ許可へフォールバック）。
    """
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
    """`cloud_provider`（A7・クラウド AI 排他選択）の検証。None は未設定（既定 openai へ）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "cloud_provider は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        return None
    from sherpa import keys
    if v not in keys.CLOUD_PROVIDERS:
        raise HTTPException(422, f"cloud_provider は {'/'.join(keys.CLOUD_PROVIDERS)} のいずれかで指定してください")
    return v


def _validate_usage_chat_provider(value):
    """`usage_chat_provider`（STAT-2・利用統計チャット専用の AI 選択）の検証。
    None（JSON `null`）だけが「未設定へ戻す」（既定は A7 連動・`usage_chat._default_provider`
    参照）。`cloud_provider`（A7）とは別物＝gemini/bedrock は選べない（`usage_chat.
    _USAGE_CHAT_PROVIDERS` が唯一の真実源）。**空文字は 422**（未設定へ戻す操作は null のみに
    一本化し、空文字を「未設定」として黙って受理しない）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "usage_chat_provider は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        raise HTTPException(
            422, "usage_chat_provider は空文字で指定できません"
                "（未設定へ戻す場合は null を指定してください）")
    if v not in usage_chat._USAGE_CHAT_PROVIDERS:
        raise HTTPException(
            422, f"usage_chat_provider は {'/'.join(usage_chat._USAGE_CHAT_PROVIDERS)} の"
                "いずれかで指定してください")
    return v


def _validate_secret_key(value, field_label: str):
    """openai/gemini/bedrock の中央 API キーの検証。None は未設定のまま・空文字は明示クリア（未設定へ戻す）。

    改行・制御文字を含む値は保存させない（422）。コピー＆ペースト事故等でキー値に `\\r`/`\\n`
    等が混入したまま保存されると、以後の全リクエストで送信時に urllib/http.client が「ヘッダ値に
    不正な文字が含まれる」例外を投げ、その例外メッセージにキー値自体がエコーされて漏洩しうる
    （実際に再現・`research_service.py` のマスク処理はあくまで最終防衛線で、根本対策は保存時点で
    弾くこと）。

    制御文字の検査は **strip() 前の生値**に対して行う（`strip()` は `\\r`/`\\n` も空白として
    削るため、先に strip してから検査すると `"\\r\\nsk-ok\\r\\n"` のように前後だけに制御文字が
    ある値が検査をすり抜けて（strip 後の中身には制御文字が残らない）そのまま保存されてしまう。
    生値の時点で弾けば、ユーザー入力に制御文字が混入していたこと自体を 422 で知らせ、黙って
    trim して保存しない）。制御文字だけの値（strip すると空文字になる非空文字列）も同じ理由で
    422 にする——「クリア（未設定へ戻す）」は利用者が明示的に空文字を送った場合だけの契約とし、
    ゴミ入力（改行だけ等）を誤ってクリア操作として受理しない。
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
    """中央既定の Ollama 接続先の検証。空文字/None は未設定（既定 localhost へ）。宛先ポリシーは
    個人設定の `ollama_url` と同じ `llm.assert_ollama_url_allowed`（loopback／admin allowlist）。

    `pending_allowlist`（省略可）: 同一 PUT リクエストで `ollama_allowlist` も一緒に更新される場合、
    検証済みの新しい候補値（`_validate_ollama_allowlist` の戻り値）を渡す。DB はまだ更新前のため
    `llm._allowlisted_hosts()`（DB を読む）は古い allowlist しか見えない。

    `strict_pending=True`（呼び出し側判定: この PUT で `ollama_url` が**実際に新しい値へ変わる**
    場合のみ渡す・`admin_settings_put` 参照）: `pending_allowlist` を**置換後の正本**として
    `llm.assert_ollama_url_allowed_in`（DB の現行 allowlist は一切見ない）で検証する。これにより
    「新しいホストへ変更しつつ、旧 allowlist にだけ残っている別ホストの権限で通ってしまう」
    （新一覧には無いのに保存できてしまい、保存直後の実行時には拒否される）不整合を防ぐ（RV 是正）。

    `strict_pending=False`（既定・URL 自体は変わらない再送、または allowlist を同時に変更しない
    通常の保存）: 従来どおり `extra_allowed` として DB の現行 allowlist に**加えて**この検証にだけ
    重ねる（DB には影響しない）。中央 URL が実際には変わらない再送を、同時に行う allowlist の
    縮小操作（中央ホストの削除を含む・既に認めている狭い例外）で巻き添えに 422 化しないための
    意図的な緩さ。
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
    """`research_default_provider`（PART-4a・外部連携タブ「AI 下調べ検索の既定 AI」）の検証。
    None は未設定（`research_service.default_research_provider()` の既定 "ollama" へフォールバック）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "research_default_provider は文字列で指定してください")
    v = value.strip().lower()
    if not v:
        return None
    if v not in research_service.RESEARCH_PROVIDERS:
        options = "/".join(sorted(research_service.RESEARCH_PROVIDERS))
        raise HTTPException(422, f"research_default_provider は {options} のいずれかで指定してください")
    return v


def _validate_depth_base_codex_reasoning(value):
    """`depth_base_codex_reasoning`（SC-6c・調べる深さの基準値「Codex の推論レベル」）の検証。
    None は未設定（env `SHERPA_CODEX_REASONING` へフォールバック）。既知の語彙
    （`sherpa.depth_profile.CODEX_REASONING_LEVELS`）以外は 422（Codex CLI へ渡す
    `model_reasoning_effort` の誤字を保存時点で弾く）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "depth_base_codex_reasoning は文字列で指定してください")
    v = value.strip().lower()
    if v not in depth_profile.CODEX_REASONING_LEVELS:
        options = "/".join(depth_profile.CODEX_REASONING_LEVELS)
        raise HTTPException(422, f"depth_base_codex_reasoning は {options} のいずれかで指定してください")
    return v


def _assert_research_default_provider_sendable(effective_settings: dict) -> None:
    """`research_default_provider` を "openai" にする PUT は、保存時点で実際に送信できる状態か
    preflight する（`research_service._connect_openai` が実行時に行う判定と全く同じ関数・同じ
    用途で判定する——`sherpa.providers.openai_direct_block_reason` に `usage="subsearch"` を
    渡す。本モジュールが実際に送信するのは下調べ検索用のモデルであり、既定の "chat" のままだと
    chat セルにだけデプロイ名を設定した環境で subsearch セルの未設定を見逃す）。プレースホルダ/
    未設定キー・Azure 等の接続先で用途別デプロイ名が無い、のいずれかなら保存自体を 422 で拒否する
    （`openai_endpoint_kind` のクロス検証と同じ「保存時点で壊れた組み合わせを作らせない」流儀・
    実際の下調べ検索が動くまで気付けない事後発覚を防ぐ）。

    `effective_settings`: この PUT 適用後に有効になる（と見なせる）設定のスナップショット
    （現在値へ `updates` を重ねたもの・呼び出し元が組み立てる）。DB の advisory lock は取らない
    （openai_endpoint_kind/base_url のペア検証と異なり、他の同時 PUT との際どい TOCTOU よりも
    「保存直後に使うと壊れている」を防ぐ実用上のガードという位置づけ・不一致が起きても実行時に
    改めて `_connect_openai` が honest に 503 で拒否する）。
    """
    from sherpa import keys as _keys, providers as _providers
    try:
        key = _keys.resolve_api_key("openai", None, system_settings=effective_settings, strict=True)
    except _keys.InvalidCloudProviderConfigError as e:
        raise HTTPException(422, f"AI 下調べ検索の既定 AI を OpenAI にできません（{e}）") from None
    reason = _providers.openai_direct_block_reason(key, effective_settings, usage="subsearch")
    if reason is not None:
        raise HTTPException(422, f"AI 下調べ検索の既定 AI を OpenAI にできません（{reason}）")


def _validate_openai_endpoint_kind(value):
    """`openai_endpoint_kind`（SET-2c・接続先の種別）の検証。None は未設定（推定へフォールバック・
    `llm.openai_endpoint_kind()` 参照）。"""
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
    """`openai_base_url`（SET-2c・接続先 URL）の検証。None/空文字は未設定（既定 OpenAI 本家へ）。
    妥当性は個人設定の `ollama_url` と同じ流儀で `llm.assert_openai_base_url_allowed` に委ねる。"""
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
    """`openai_auth_header`（SET-2c）の検証。None/空文字は未設定（既定 bearer へ）。"""
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
    """`openai_api_version`（SET-2c）の検証。None/空文字は未設定（未使用へ）。"""
    if value is None:
        return None
    if not isinstance(value, str):
        raise HTTPException(422, "openai_api_version は文字列で指定してください")
    return value.strip() or None


def _validate_model_catalog(value):
    """`model_catalog` の検証。形・意味の検証本体は `sherpa.model_catalog.validate_catalog`
    （`ValueError` を送出）で行い、ここでは 422 へ変換するだけ（他の `_validate_*` と同じ流儀）。"""
    try:
        return model_catalog.validate_catalog(value)
    except ValueError as e:
        raise HTTPException(422, str(e))


def _validate_model_windows(value):
    """`model_context_windows`（BUDGET-2・§3.4）の検証。形・意味の検証本体は
    `sherpa.model_windows.validate_model_windows`（`ValueError` を送出）で行い、ここでは 422 へ
    変換するだけ（`_validate_model_catalog` と同じ流儀）。"""
    try:
        return model_windows.validate_model_windows(value)
    except ValueError as e:
        raise HTTPException(422, str(e))


def _validate_chat_examples(value):
    """`chat_examples`（チャット画面のクイック入力例）の検証。形・意味の検証本体は
    `sherpa.chat_examples.validate`（`ValueError` を送出）で行い、ここでは 422 へ変換するだけ
    （`_validate_model_catalog` と同じ流儀）。"""
    try:
        return chat_examples.validate(value)
    except ValueError as e:
        raise HTTPException(422, str(e))


def _validate_codex_session_retention_days(value):
    """`codex_session_retention_days`（R1b・決定5）の検証。None は未設定（＝0/無制限へフォールバック）。
    0 以上の整数のみ許可（0＝無制限・明示的に保存できる）。負値・非整数は 422。"""
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise HTTPException(422, "codex_session_retention_days は0以上の整数で指定してください")
    if value < 0:
        raise HTTPException(422, "codex_session_retention_days は0以上の整数で指定してください（0=無制限）")
    return value


# GET・PUT /admin/settings には response_model を付与しない: `legacy_backend.libreoffice.version`
# （soffice 検出バージョン文字列）を含むため、response_model を付与すると OpenAPI スキーマに
# `version` プロパティが露出する。`test_world_param_compat.py::test_openapi_surface_has_no_version_parameter`
# が「退役した version 概念を API surface に再宣言しない」契約を pin しているため、この2ルートは
# `sherpa.schemas.AdminSettingsView` を TypeAdapter 契約のみで固定する（response_model 非付与）。
@extras_router.get("/admin/settings", tags=["管理者:全体設定"])
def admin_settings_get(request: Request):
    """全体設定（取り込みアーム・旧形式変換バックエンド）の現行値＋実効値を返す（admin のみ）。

    `legacy_backend.libreoffice` に soffice の検出状態（有無＋バージョン）を含める（UI の案内表示用）。
    """
    _require_admin(_current_user(request))
    return _admin_settings_view()


@extras_router.put("/admin/settings", tags=["管理者:全体設定"])
def admin_settings_put(req: SystemSettingsReq, request: Request):
    """全体設定を部分更新（admin のみ・検証・監査・fail-closed）。

    未指定キーは変更しない・`null` は未設定へ戻す（env/既定へフォールバック）。arms_enabled は既知アーム名
    のみ・legacy_backend は none|libreoffice|office_com・rag_llm_render（L5）は on|off のみ・
    vlm は provider/model/cloud_allowed の型を検証する
    （不正は 422）。ollama_allowlist（R2a-S2）は各エントリを host:port に正規化して保存する（junk は 422）。
    codex_session_retention_days（R1b）は0以上の整数のみ（0=無制限・負値は422）。openai_endpoint_kind/base_url/auth_header/
    webhook_allowlist（PART-6・W3）は ollama_allowlist と同形式で host:port に正規化して保存する
    （junk は 422）。api_version（SET-2c・接続先）は種別・URL 妥当性・ヘッダ形式を検証し、kind が openai 以外なら
    実効 base_url が空でないことも確認する（422）。usage_chat_provider（STAT-2）は openai|ollama
    のみ（不正は422・`cloud_provider` とは独立）。research_default_provider（PART-4a）は
    ollama/openai のいずれかのみ（422・`research_service.RESEARCH_PROVIDERS`）。"openai" への
    変更はこの PUT 適用後の実効設定で実送信可能性も preflight する
    （`_assert_research_default_provider_sendable`・NG は422）。depth_base_*（SC-6c・調べる深さの
    基準値）は整数6項目が範囲検証済み（StrictInt+Field）、depth_base_codex_reasoning は
    `sherpa.depth_profile.CODEX_REASONING_LEVELS` のいずれかのみ（422）。agentic_budget_per_result/
    agentic_budget_total（BUDGET-1・§3.4）も StrictInt+Field で範囲検証済み（1件あたり=1024〜8MiB・
    累計=4096〜64MiB・範囲外は422）。model_context_windows（BUDGET-2・§3.4）は "provider:model" →
    tokens の登録表（`sherpa.model_windows.validate_model_windows` が意味検証・不正は422）。
    chat_examples（チャット画面のクイック入力例）は `{enabled, items}`（items は最大8件・各1〜200文字・
    `sherpa.chat_examples.validate` が意味検証・不正は422）。
    監査 INSERT の失敗は
    `store.set_system_settings` が設定変更と**同一トランザクション**で検知し自動 rollback する（2026-07-08
    RV High: commit 後の別接続 audit＋失敗時 compensate 方式は穴があったため、原子性で置き換えた・
    announcement CRUD の compensate 方式とは異なる）。ここではその例外を 500 に変換するだけ。応答は GET と同形。
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
    if "usage_chat_provider" in provided:
        updates["usage_chat_provider"] = _validate_usage_chat_provider(provided["usage_chat_provider"])
    if "personal_api_keys_allowed" in provided:
        # StrictBool が型検証済み（非 bool は pydantic 自身が 422 にする）。
        updates["personal_api_keys_allowed"] = provided["personal_api_keys_allowed"]
    if "web_search_allowed" in provided:
        # StrictBool が型検証済み（非 bool は pydantic 自身が 422 にする）。
        updates["web_search_allowed"] = provided["web_search_allowed"]
    if "user_api_keys_allowed" in provided:
        # StrictBool が型検証済み（非 bool は pydantic 自身が 422 にする）。
        updates["user_api_keys_allowed"] = provided["user_api_keys_allowed"]
    if "user_api_keys_daily_quota_default" in provided:
        # StrictInt・範囲（1〜1,000,000）は pydantic Field が型検証済み。
        updates["user_api_keys_daily_quota_default"] = provided["user_api_keys_daily_quota_default"]
    if "research_default_provider" in provided:
        updates["research_default_provider"] = _validate_research_default_provider(
            provided["research_default_provider"])
    # SC-6c: 調べる深さの基準値（整数6項目は StrictInt+Field(ge,le) で pydantic が範囲検証済み）。
    for _k in ("depth_base_max_turns", "depth_base_grep_max_hits", "depth_base_qa_max_hits",
              "depth_base_read_window", "depth_base_impact_depth", "depth_base_troubleshoot_depth"):
        if _k in provided:
            updates[_k] = provided[_k]
    if "depth_base_codex_reasoning" in provided:
        updates["depth_base_codex_reasoning"] = _validate_depth_base_codex_reasoning(
            provided["depth_base_codex_reasoning"])
    # BUDGET-1（§3.4）: agentic search の tool-result バイト予算（2項目とも StrictInt+Field(ge,le)
    # で pydantic が範囲検証済み・カスタムバリデータ不要）。
    for _k in ("agentic_budget_per_result", "agentic_budget_total"):
        if _k in provided:
            updates[_k] = provided[_k]
    # BUDGET-2（§3.4）: モデル窓の管理者登録（"provider:model" → tokens）。
    if "model_context_windows" in provided:
        updates["model_context_windows"] = _validate_model_windows(provided["model_context_windows"])
    if "chat_examples" in provided:
        updates["chat_examples"] = _validate_chat_examples(provided["chat_examples"])
    _cloud_secret_keys = frozenset({"openai_api_key", "gemini_api_key", "bedrock_api_key"})
    for _k in _cloud_secret_keys:
        if _k in provided:
            updates[_k] = _validate_secret_key(provided[_k], _k)
    if "ollama_url" in provided:
        # 同一 PUT で ollama_allowlist も更新される場合は、その新しい候補（検証済み）で ollama_url を
        # 検証する（DB がまだ更新されていない古い allowlist で判定して誤って 422 にしない）。
        _pending_allowlist = updates["ollama_allowlist"] if "ollama_allowlist" in updates else None
        # 「URL 自体が実際に新しい値へ変わる」場合だけ、pending allowlist を置換後の正本として
        # 厳密検証する（strict_pending）。中央 URL が変わらない再送（フォームの全項目送信等）は、
        # 同時に allowlist を縮小してそのホストを含まなくなっても拒否しない（狭い例外・RV 是正）。
        _strict_pending = False
        if "ollama_allowlist" in provided and isinstance(provided["ollama_url"], str):
            _cur_central = (store.get_system_settings().get("ollama_url") or "").strip()
            _strict_pending = provided["ollama_url"].strip() != _cur_central
        updates["ollama_url"] = _validate_central_ollama_url(
            provided["ollama_url"], _pending_allowlist, strict_pending=_strict_pending)
    # SET-2c: OpenAI 互換 API の接続先（4キー）。
    if "openai_endpoint_kind" in provided:
        updates["openai_endpoint_kind"] = _validate_openai_endpoint_kind(provided["openai_endpoint_kind"])
    if "openai_base_url" in provided:
        updates["openai_base_url"] = _validate_openai_base_url(provided["openai_base_url"])
    if "openai_auth_header" in provided:
        updates["openai_auth_header"] = _validate_openai_auth_header(provided["openai_auth_header"])
    if "openai_api_version" in provided:
        updates["openai_api_version"] = _validate_openai_api_version(provided["openai_api_version"])
    # kind/base のクロス検証（kind が openai 以外なら base_url も必要）は、ここ（キャッシュ経由の
    # 現在値）では行わない。`store.set_system_settings()` が advisory lock 取得後に同一コネクション
    # から実効値を読んで検証する（並行 PUT・別 worker のキャッシュ陳腐化による TOCTOU を避ける・
    # 下の `OpenAIEndpointSettingsConflict` except 節参照）。
    if "model_catalog" in provided:
        updates["model_catalog"] = _validate_model_catalog(provided["model_catalog"])
    # research_default_provider を "openai" にする更新は、この PUT 適用後に有効になる設定
    # （現在値へ updates を重ねたもの・同じ PUT で openai_api_key 等を同時に変える場合も反映する）
    # で実送信可能性を preflight する。
    if updates.get("research_default_provider") == "openai":
        _assert_research_default_provider_sendable({**store.get_system_settings(), **updates})
    if updates:
        try:
            # `user_api_keys_allowed` を実効 OFF（false または明示 null）にする更新は、設定の
            # 適用・利用者発行キーの一括失効・監査を同一トランザクションで行う（それ以外の更新は
            # `store.set_system_settings` と同じ結果）。失効の失敗も設定変更ごと rollback する
            # （握り潰さない・fail-closed）。
            store.apply_system_settings_and_revoke_if_disabled(
                u["uid"], updates, secret_keys=_cloud_secret_keys & set(updates))
        except store.OpenAIEndpointSettingsConflict as e:
            raise HTTPException(422, str(e))
        except Exception:
            _log.critical("system_settings.updated failed (fail-closed) – keys=%s", list(updates))
            raise HTTPException(500, "全体設定の保存中にエラーが発生しました")
        # A6: personal_api_keys_allowed を false で保存するたび（true→false の遷移に限らず・
        # 冪等）、全ユーザーの個人秘密キーを一括削除する（OFF のとき個人キーは保存されない状態を
        # 保つ）。settings 本体の保存は既に成功しているため、ここが失敗しても 500 にはしない
        # （起動時の `api._purge_personal_keys_if_disabled_on_startup` が backstop）。
        if updates.get("personal_api_keys_allowed") is False:
            try:
                store.purge_personal_api_keys(actor=u["uid"])
            except Exception:
                _log.critical("personal_api_keys_allowed=false の個人キー一括削除に失敗しました"
                             "（次回起動時に再試行されます）")
    return _admin_settings_view()


class OpenaiEndpointTestReq(BaseModel):
    """管理画面「接続先」欄の接続テスト（admin 専用・`POST /admin/settings/openai-endpoint-test`）。

    タイムアウトは接続テスト専用に短くする（`_ENDPOINT_TEST_TIMEOUT_S`）: `_probe` の既定（抽出用90秒）の
    ままだと、パケットが破棄される閉域網で画面のスピナーが数分止まらない（閉域実機 2026-09-04）。
    テストの目的は疎通確認なので、10秒で「到達できません」を返すのが正しい申告。

    保存前の入力中の値でその場だけ試す。DB は書かない・秘密は保存も監査もしない。個人設定用の
    `POST /settings/test` とは別ルート: 一般ユーザーには接続先 override を一切与えない（任意の
    HTTPS 宛先へ中央キーを送信できてしまう SSRF／キー漏洩の穴になるため）。
    """
    provider: str = "openai"                    # openai（既定）／codex（Codex(OpenAI) 構成の判定）
    openai_endpoint_kind: str | None = None
    openai_base_url: str | None = None
    openai_auth_header: str | None = None
    openai_api_version: str | None = None
    openai_api_key: str | None = None            # 入力中の未保存の中央キー。省略時は保存済み中央キー。
    # `codex_model` は受け取らない。接続テストはカタログ内モデルでの疎通確認のみを目的とする＝
    # admin が任意のカタログ外モデル名を入力してカタログ検証を素通りできる経路を持たせない。
    # provider=codex は常に中央カタログ既定（`model_catalog.resolve_model` の field 省略）で解決する。


@extras_router.post("/admin/settings/openai-endpoint-test", tags=["管理者:全体設定"],
                    response_model=SettingsTestResponse)
def admin_openai_endpoint_test(req: OpenaiEndpointTestReq, request: Request):
    """OpenAI 互換 API の接続先（管理画面「接続先」欄）を、保存前の入力中の値でその場だけ試す
    （admin 専用・1回だけ最小リクエスト・DB は書かない）。

    `PUT /admin/settings` と同じ検証（種別 enum・URL 妥当性・userinfo 禁止・kind が openai 以外なら
    base_url 必須）を**通信前に**共有する（`_validate_openai_*`・`llm.assert_openai_endpoint_consistent`）。
    不正な入力は 422 で `_probe` を一切呼ばない。

    キー・モデルは常に**中央**（`user_settings=None`）で解決する。個人設定用 `/settings/test` を
    admin 本人のログインで流用すると、A6（個人キー許可）が有効な環境では admin 本人の個人キー・
    個人モデルで試してしまい、「中央キーが壊れていても緑」「正しい中央デプロイなのに個人設定の
    古いモデルで赤」という誤診断が起こる。

    provider=codex は Codex(OpenAI) 構成の接続先ブロック判定（`_codex_openai_compat_block_reason`）
    を同じ pending スナップショットで試す（Codex 分岐だけ入力中の接続先が反映されない食い違いを
    防ぐ）。モデルは常に中央カタログ既定で解決する（`codex_model` の入力は受け付けない＝カタログ外
    モデル名を接続テスト経由でカタログ検証を迂回させないため）。

    実行を監査する（`openai_endpoint.tested`）。記録するのは actor・provider・endpoint_kind・host
    のみ（`llm._redact_url_for_error` の安全な host 表現）＝キー・生 URL（path・クエリ含む）・probe
    のエラー本文は一切含めない。**監査の書き込みに失敗したら probe を呼ばずに 500 で中断する**
    （fail-closed＝未監査のまま実 API へ到達させない）。
    """
    from sherpa import agent_constructs, keys, llm, model_catalog
    from sherpa.ingest import graph_extract
    u = _current_user(request)
    _require_admin(u)
    prov = (req.provider or "openai").lower()
    if prov not in ("openai", "codex"):
        raise HTTPException(422, "provider は openai / codex のいずれか")
    sys_s = store.get_system_settings()
    # PUT と同じ検証を通信前に行う（不正なら 422・ネットワークへは一切出さない）。
    pending = dict(sys_s)
    if req.openai_endpoint_kind is not None:
        pending["openai_endpoint_kind"] = _validate_openai_endpoint_kind(req.openai_endpoint_kind)
    if req.openai_base_url is not None:
        pending["openai_base_url"] = _validate_openai_base_url(req.openai_base_url)
    if req.openai_auth_header is not None:
        pending["openai_auth_header"] = _validate_openai_auth_header(req.openai_auth_header)
    if req.openai_api_version is not None:
        pending["openai_api_version"] = _validate_openai_api_version(req.openai_api_version)
    # `pending["openai_base_url"]` は保存済み値を継承した場合、JSONB は型を強制しないため理論上
    # 非文字列（list/dict/int 等・`{}`/`[]`/`0`/`False` のような falsy な非文字列も含む）もあり得る。
    # ここは「kind が base_url を要求するのに何も設定されていない」という**欠落**だけを検出する
    # 安価な事前チェックであり、非文字列は欠落ではなく不正（型検証は下の再検証ブロックの役割）
    # として扱う: `None`／空文字列だけを「欠落」とみなし、それ以外（非文字列を含むどんな値でも）は
    # 「何か設定されている」として通す。`str(value or "")` のような素朴な falsy 潰しは `{}`/`[]`/
    # `0`/`False` を「欠落」と誤認してここで 422（監査を書く前）に倒してしまい、下の再検証
    # ブロック（不正値でも deny 監査を残してから 422 にする契約）に到達できなくなる。
    _raw_pending_base_url = pending.get("openai_base_url")
    _base_url_missing = _raw_pending_base_url is None or _raw_pending_base_url == ""
    try:
        llm.assert_openai_endpoint_consistent(
            pending.get("openai_endpoint_kind") or "openai",
            "" if _base_url_missing else "x")
    except ValueError as e:
        raise HTTPException(422, str(e))
    # 監査は probe（実 API 呼び出し）より前に行う。書き込み失敗はそのまま例外にして 500 へ変換する
    # （fail-closed・未監査のまま先へ進まない）。キー・生 URL・エラー本文は含めない。
    #
    # `pending["openai_base_url"]` はリクエストで明示された値だけでなく、`req.openai_base_url`
    # 省略時は保存済みの値をそのまま継承する（976行目）。継承側は `_validate_openai_base_url` を
    # 通っていないため、空白・バックスラッシュ混入・非文字列等を含む値が保存されていると、
    # 生値のまま扱えば `hostname` に内部パス断片が残ったり（`urlsplit`）、`llm.openai_base_url()`
    # の型検証で `ValueError` が飛んだりする。実効値（明示・継承いずれの経路でも）を
    # 使用直前に再検証し、不合格（型不正を含む）なら host 表現を作らず固定文字列へ倒したうえで
    # probe は実行しない（表示系の `system.py::_INVALID_SAVED_BASE_URL_LABEL` と同じ流儀）。
    # `eff_kind` の解決（`llm.openai_endpoint_kind()`）も try 内に含める: 同関数は
    # `openai_base_url` の型も判定分岐より先に検査する契約（`_assert_openai_endpoint_
    # settings_types_valid`）のため、kind=openai／未設定でも base_url が非文字列なら
    # ここで `ValueError` を送出しうる（kind の解決だけを外に出すと、この場合に監査へ
    # 到達する前で 500 になってしまう）。
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
    # 中央のみ（個人キー・個人モデルは一切見ない＝`user_settings=None`）。この直後で probe
    # （実 API 呼び出し）に使うため strict=True で解決する（非 strict のままだと `cloud_provider`
    # が非空の不正値でも黙って既定 openai 扱いのキーで実送信してしまい、課金を伴う接続テストの
    # honest failure 契約が崩れるため）。
    try:
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
    """`allowed_worlds` の各 world_id を個別に strict resolve する（全 world を列挙しない・
    空リスト/None は何もしない＝deny-all/無スコープに検証の必要は無い）。

    未知の world は422、resolver（registry/root）到達不可は503。
    """
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
    """API キーの有効期限は未来の日時のみ許可する（過去日を選べてしまうと発行直後から
    使えないキーができる・422）。無期限（None）はそのまま許可。"""
    if dt is not None and dt <= datetime.now(timezone.utc):
        raise HTTPException(422, f"{field_label}は未来の日時で指定してください")


def _validate_webhook_url_or_error(webhook_url: str | None) -> None:
    """`webhook_url`（PART-6・キー発行時のオプトイン）が宛先ポリシー（loopback／admin
    allowlist）を満たすか検証する。None（未指定）はそのまま許可（Webhook 無効のまま発行）。
    不許可は 422（`webhooks.assert_webhook_url_allowed` が `WebhookUrlInvalid` を送出）。"""
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
    """発行直後のレスポンス（プレーンキーを含む・**このレスポンスでのみ**返す）。
    admin 発行（`ext_key_create`）・利用者自己発行（`ext_self_key_create`）で共通の形。

    `webhook_secret`（PART-6）もここでのみ平文で返す（`webhook_url` 未指定なら両方 null）。
    """
    return {"ok": True, "id": row["id"], "key": plain, "key_prefix": row["key_prefix"],
            "label": row["label"], "created_at": str(row["created_at"]),
            "allowed_worlds": row.get("allowed_worlds"),
            "expires_at": str(row["expires_at"]) if row.get("expires_at") else None,
            "daily_quota": row.get("daily_quota"), "client_op_id": row.get("client_op_id"),
            "webhook_url": row.get("webhook_url"), "webhook_secret": row.get("webhook_secret")}


def _key_list_out(rows: list, call_counts: dict) -> dict:
    """一覧レスポンス（プレーンキーは含めない）。admin 全件一覧・利用者本人一覧で共通の形。
    `client_op_id` は秘密ではない（発行操作の相関トークン）。曖昧な発行結果の照合・自動失効は
    一覧をクライアントが走査するのではなく、専用の回復エンドポイント（`ext_key_recover`/
    `ext_self_key_recover`）がサーバー側で単一の原子的 UPDATE として行う——ここで返す値は
    参照・デバッグ用途（一覧上で「どの発行操作に対応するか」を確認できる）。

    `webhook`（PART-6）は Webhook 登録の有無のみ・`webhook_host` は宛先の host:port までは出す
    （path/query・`webhook_secret` は絶対に一覧へ出さない——`rows` の SELECT 自体が
    `webhook_secret` を含まない・`list_api_keys` 参照）。
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
                                  "webhook_url が宛先ポリシー（loopback／admin allowlist）を"
                                  "満たさない場合"),
                              **_EXT_ADMIN_RESPONSES})
def ext_key_create(req: ExtKeyCreateReq, request: Request,
                   x_request_id: str | None = ext_api._XRequestIdIn):
    """外部 API キーを発行。プレーンキーは**このレスポンスで1度だけ**返す（DB はハッシュのみ）。

    `allowed_worlds` は実在する world_id のみ許可する（形式検証は `ExtKeyCreateReq` 側・
    実在検証はここ・指定された ID だけを個別に strict resolve する＝全 world 列挙はしない）。
    未知の world は 422。`expires_at` は announcements と同じ ISO 8601 文字列（省略/null＝
    無期限）・`daily_quota` は1以上の整数（省略/null＝無制限）。監査は
    `ext_api.start_audit()` で `request.state.audit_pending` に積み、実際の書き込みは
    `ExtRequestMiddleware`（`/ext/v1/*` 全体に装着済み）が実応答ステータスで一元的に行う
    （他の X-API-Key 系ルートと同じ経路・書込先を二重化しない）。このルートも `/ext/v1/*` 配下＝
    `ExtRequestMiddleware` が X-Request-Id の解決/応答ヘッダ付与・401/403 を含む全応答への付与を
    行う（`x_request_id` はここでは OpenAPI 契約の宣言のみ・実際の解決は Cookie 認証と無関係に
    ミドルウェアが行う）。
    """
    del x_request_id
    u = _require_admin(_current_user(request))
    pending = ext_api.start_audit(request, u["uid"], "ext_api.key_created", "api_key")
    # 検証前に積む（`req.label`/`req.allowed_worlds` は Pydantic の field_validator を経た
    # 正規化済みの値）＝422/503 で失敗しても監査に「何を発行しようとしたか」（入力）が残る。
    pending["detail"].update({"label": req.label, "allowed_worlds": req.allowed_worlds,
                              "expires_at": req.expires_at, "daily_quota": req.daily_quota,
                              "webhook": bool(req.webhook_url)})
    _validate_allowed_worlds_or_error(req.allowed_worlds)
    expires_at = _parse_announcement_dt(req.expires_at, "有効期限")
    _validate_future_expiry(expires_at, "有効期限")
    _validate_webhook_url_or_error(req.webhook_url)
    # PART-6（W4）: secret は登録時に生成し平文保管する（署名生成に平文が必須）。
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
    """外部 API キー一覧（プレーンキーは含めない・全件・admin のみ）。

    `call_count` は監査台帳（直近分のみ・`store.count_ext_api_calls_by_key`）からの集計。
    `owner_uid` が非 null のキーは利用者本人が自己発行したもの——admin はこれも含めて
    全件を見え、失効もできる。
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
    """キー失効（soft・冪等）。未知 id は 404。admin は所有者を問わず任意のキー（利用者自己発行
    キーを含む）を失効できる。"""
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
    """`POST /ext/v1/admin/keys` の応答が届かなかった（タイムアウト・通信断・不正な形の応答等・
    曖昧な結果）場合の回復専用エンドポイント。

    この admin 自身（`created_by=uid` かつ `owner_uid IS NULL`＝admin 発行の行のみ）が発行操作を
    試みた `client_op_id` に一致する**未失効**キーだけを、単一の原子的 UPDATE で照合・失効する
    （一覧取得→別リクエストで DELETE、という2段構成は「一覧に他人の行も混じる」「その間に
    別の変更が起こる」隙があるため使わない——`client_op_id`・所有条件を同一 SQL の WHERE 句で
    照合するので、`client_op_id` が他人の値と衝突しても他人のキーには触れない）。
    `found: false` は「POST がそもそもサーバーに届かなかった」「まだコミットされていない」の
    いずれか（呼び出し側で有界に再試行すること・区別できない・区別する必要もない——どちらも
    「今は確定的な有効キーは無い」という意味では同じ）。
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


# ===== 利用者本人による API キー自己発行/一覧/失効/回復（4ルート） =====
# admin ルートと同じ `X-API-Key` 系ではなく、個人設定ページと同じ Cookie 認証（`_current_user`）。
# admin 権限は不要（`_require_admin` を呼ばない）——本人の分だけ見え、本人の分だけ失効・回復できる。

def _require_user_api_keys_allowed(u: dict) -> dict:
    """`system_settings.user_api_keys_allowed` が偽なら403（本人一覧/失効/回復を含む4ルート
    共通のゲート）。OFF のときは自己発行キーの機能そのものを見せない/触らせない（発行だけでなく
    一覧・失効・回復も同様に拒否する）。戻り値は以後の処理で使い回す system_settings。
    """
    sysset = store.get_system_settings()
    if not bool(sysset.get("user_api_keys_allowed")):
        raise HTTPException(403, "利用者による API キー発行は許可されていません（管理者に確認してください）")
    return sysset


def _enforce_self_world_scope(uid: str, requested: list[str] | None) -> list[str] | None:
    """利用者自己発行キーの world スコープを本人のアクセス範囲 ⊆ に強制する。

    `worlds.accessible_world_ids(uid)` が None（現状＝全ユーザーが全 world にアクセス可）なら
    そのまま返す。非 None（将来の部門/管理者スコープ実装後）になったら、未指定は本人の範囲へ
    明示的に絞り、指定された world がその範囲外なら 403。
    """
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
                                  "（loopback／admin allowlist）を満たさない場合"),
                              401: {"description": "ログインが必要です（セッション Cookie）",
                                    "headers": dict(ext_api._REQUEST_ID_OPENAPI_HEADER)}})
def ext_self_key_create(req: ExtSelfKeyCreateReq, request: Request,
                        x_request_id: str | None = ext_api._XRequestIdIn):
    """利用者本人の API キーを発行する（`system_settings.user_api_keys_allowed` が
    true のときのみ）。

    world スコープは本人がアクセスできる範囲へ強制する（`_enforce_self_world_scope`）。
    `daily_quota` の解決・上限チェックは `store.insert_api_key` が `_USER_KEY_LOCK` の下で
    DB から現在の許可トグル・現在の上限を再読して行う（ここでは生の入力をそのまま渡すだけ・
    TOCTOU対策として router 側では解決しない——利用者が上限100を読んだ直後に admin が5へ
    引き下げても、実際に書き込む瞬間の上限で判定する）。プレーンキーは発行直後のこの応答でのみ
    返す。`system_settings.user_api_keys_allowed` の最終判定も同じロック内で再確認する
    （ここでの事前チェック `_require_user_api_keys_allowed` は早期リターンのための
    best-effort・正本は store 側）。
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
    """本人が発行した API キーの一覧（プレーンキーは含めない・他人のキーは見えない）。
    機能トグルが OFF のときは一覧そのものを見せない（4ルート共通のゲート）。"""
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
    """本人が発行したキーの失効（soft・冪等）。他人/admin発行のキー・未知 id は 404
    （所有権の有無を外に出さない・`store.revoke_api_key(owner_uid=...)` が絞り込む）。
    機能トグルが OFF のときはこの操作そのものを拒否する（4ルート共通のゲート）。"""
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
    """自己発行の `POST /ext/v1/keys` の応答が届かなかった場合の回復専用エンドポイント
    （`ext_key_recover` の自己発行版・`owner_uid=uid` で照合）。機能トグルが OFF のときは
    この操作そのものを拒否する（他の自己サービス系ルートと同じゲート——OFF になった時点で
    自己発行キーは一括失効済みのため、実質的には常に `found: false` になる）。"""
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
