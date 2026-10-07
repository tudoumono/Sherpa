"""API 応答スキーマ集約。

実 TestClient 応答から書き起こした pydantic 応答モデルで、`tests/api/test_mock_api_contract.py` の TypeAdapter 契約テストが
`tests/e2e/mock_api.py` の `MOCKED` 応答を検証する。キー集合が常に固定のモデルだけ各 router の response_model に付与する。

守ること:
- 応答内容は変えない。ハンドラが `str(...)` で文字列化している値（例: `created_at`）は `str` 型にする。
- datetime は必ず `WireDateTime` を使う（生の `datetime` を直接フィールド型にしない）。
- 分岐が `Literal` で判別できる有限個の固定形は `Union[...]` で表し response_model を付与する（例: `POST /worlds`）。
- ネストした list 要素の中でキーが増減する応答は response_model を付与しない。
- 自由形式 JSON（監査ログの `detail` 等）は `Any` / `dict[str, Any]` で緩く受ける。
- フィールド名に `version` を含むモデル（`ConversationSummary`・`AdminSettingsView`）は response_model を付与しない
  （OpenAPI に `version` を露出させない `tests/api/test_world_param_compat.py::test_openapi_surface_has_no_version_parameter` に従う）。
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal

from pydantic import BaseModel, Field, PlainSerializer, StrictInt, StrictStr

# 全 datetime フィールドの型。`PlainSerializer` で `.isoformat()` を明示し、response_model の有無で `Z` 表記と `+00:00` 表記が変わらないようにする
WireDateTime = Annotated[datetime, PlainSerializer(lambda v: v.isoformat(), return_type=str)]


# ===== 認証（sherpa/routers/auth.py） =====

class AuthMeResponse(BaseModel):
    """現在のログインユーザー（GET /auth/me）。"""
    uid: str
    email: str | None
    display_name: str | None
    role: str
    must_change_password: bool
    auth_disabled: bool


class AuthLoginResponse(BaseModel):
    """ログイン結果（POST /auth/login）。"""
    ok: bool
    uid: str
    must_change_password: bool
    next: str | None


class OkResponse(BaseModel):
    """`{"ok": true}` のみを返すエンドポイント共通形。"""
    ok: bool


# ===== システム（sherpa/routers/system.py・system_extras.py） =====

class HealthSummaryResponse(BaseModel):
    """状態ドット用の最小サマリ（GET /health/summary）。"""
    status: str
    checked_at: str


class ConfigResponse(BaseModel):
    """現在の実行構成（GET /config）。"""
    agent: str
    label: str
    model: str


class ConstructChoice(BaseModel):
    """GET・PUT /settings の `constructs_available[]` 要素。`agent`/`codex_model_provider` は画面がそのまま PUT する設定値。"""
    id: str
    agent: str
    codex_model_provider: str | None
    label: str
    hint: str


class ModelChoiceInfo(BaseModel):
    """GET・PUT /settings の `ollama_url_choice` や GET・PUT /admin/settings の `model_catalog` のセルで使う選択肢の共通形。`allowed` が空でも `default` が非空のことがある。"""
    allowed: list[str]
    default: str


class OllamaUrlChoiceInfo(ModelChoiceInfo):
    """個人の Ollama 接続先の選択肢。`allowed` は許可ポリシーを満たす完全 URL のみ。`legacy` は現在の保存値が許可されていない場合だけ非 null。"""
    legacy: str | None


class SettingsResponse(BaseModel):
    """GET・PUT /settings（全キー常時存在）。

    モデル名・機能別プロバイダ・Web 検索の希望は個人設定に無い（モデルは管理者の使えるモデル一覧と選択中のクラウドプロバイダだけで決まり、Web 検索はチャットごとの希望だけで決まる）ため応答に含めない。
    """
    agent: str
    web_search_available: bool
    openai_key_set: bool
    # 接続先の種類（openai/azure/custom）とホスト名のみ（キー・パスは出さない）
    openai_endpoint_kind: str
    openai_base_url_host: str
    ollama_url: str | None
    # この環境で選べる実行構成（simple / codex_openai / codex_ollama）
    codex_model_provider: str
    construct_id: str
    constructs_available: list[ConstructChoice]
    # 現在選択中のクラウドプロバイダ、と個人キー許可フラグ（既定 false）
    cloud_provider: str
    personal_api_keys_allowed: bool
    # 利用者本人による外部連携 API キー自己発行の許可（管理者設定・既定 false）
    user_api_keys_allowed: bool
    # 自己発行キーの1日あたり呼び出し上限（既定/上限・管理者統制）
    user_api_keys_daily_quota_default: int
    # 個人の Ollama 接続先 `<select>` の選択肢（allowed は完全 URL）
    ollama_url_choice: OllamaUrlChoiceInfo
    # チャット画面のクイック入力例。None＝未設定（フロントの組み込み既定を使う）、配列（空含む）＝管理者の明示設定（空＝非表示）
    chat_examples: list[str] | None


class SettingsTestResponse(BaseModel):
    """接続テスト結果（POST /settings/test）。全分岐で同じ4キー。"""
    ok: bool
    provider: str
    model: str
    detail: str | None


# ---- 管理者:全体設定 ----

class CloudInfo(BaseModel):
    """クラウド AI プロバイダの中央設定。"""
    provider: str
    # `provider` は既定 openai への読み替え込みの実効値。`provider_raw` は生の保存値（未選択＝一度も PUT されていなければ null）
    provider_raw: str | None
    providers: list[str]
    personal_api_keys_allowed: bool
    openai_key_set: bool
    ollama_url: str
    # 保存済みの `cloud_provider` が閉じたプロバイダ（gemini/bedrock）のときだけその名前。それ以外は null
    retired_provider: str | None
    personal_keys_in_use_count: int
    # Codex の Web 検索を管理者が許可しているか（既定 false）
    web_search_allowed: bool


class ExtKeysDailyQuotaDefaultInfo(BaseModel):
    """自己発行キーの1日あたり呼び出し上限（`configured`=管理者の生値・`effective`=実際に適用される値・`default`=組み込みの既定値）。"""
    configured: int | None
    effective: int
    default: int


class ExtKeysResearchProviderInfo(BaseModel):
    """外部からの簡易回答（POST /ext/v1/answer）の既定 AI（`configured`=管理者の生値・`effective`=実際に適用される値・`default`=組み込みの既定値）。"""
    configured: str | None
    effective: str
    default: str


class ExtKeysAdminInfo(BaseModel):
    """外部連携 API キーの自己発行の許可。`self_issued_active_count` は OFF にする前の確認用の件数（失効済み/期限切れは含まない）。"""
    user_api_keys_allowed: bool
    self_issued_active_count: int
    daily_quota_default: ExtKeysDailyQuotaDefaultInfo
    research_default_provider: ExtKeysResearchProviderInfo


# ---- 外部連携 API キー管理（POST/GET/DELETE /ext/v1/admin/keys・POST/GET/DELETE /ext/v1/keys で共通の形） ----

class ExtKeyCreatedResponse(BaseModel):
    """発行直後のレスポンス（プレーンキーを含み、このレスポンスでのみ返す）。

    `webhook_secret` もこのレスポンスでのみ平文で返す（`webhook_url` を指定したときだけ非 null）。
    """
    ok: bool
    id: int
    key: str
    key_prefix: str
    label: str
    created_at: str
    allowed_worlds: list[str] | None
    expires_at: str | None
    daily_quota: int | None
    client_op_id: str | None
    webhook_url: str | None
    webhook_secret: str | None


class ExtKeyListItem(BaseModel):
    """キー一覧の1行（プレーンキーは含めない）。`webhook` は Webhook 登録の有無、`webhook_host` は宛先の host:port のみ。"""
    id: int
    key_prefix: str
    label: str
    created_by: str
    revoked_by: str | None
    allowed_worlds: list[str] | None
    daily_quota: int | None
    owner_uid: str | None
    client_op_id: str | None
    created_at: str
    revoked_at: str | None
    last_used_at: str | None
    expires_at: str | None
    call_count: int
    webhook: bool
    webhook_host: str | None


class ExtKeyListResponse(BaseModel):
    keys: list[ExtKeyListItem]


class ExtKeyRevokeResponse(BaseModel):
    ok: bool
    id: int
    revoked_at: str


class ExtKeyRecoverResponse(BaseModel):
    """曖昧な発行結果の回復用エンドポイントの応答。`found=True` のときだけ実際に失効している。"""
    found: bool
    id: int | None
    revoked_at: str | None
class OpenaiEndpointConfigured(BaseModel):
    """`openai_endpoint.configured`（管理者が保存した生値・未設定なら null）。"""
    kind: str | None
    base_url: str | None
    auth_header: str | None
    api_version: str | None


class OpenaiEndpointEffective(BaseModel):
    """`openai_endpoint.effective`（解決結果）。"""
    kind: str
    base_url: str
    auth_header: str
    api_version: str


class OpenaiEndpointInfo(BaseModel):
    """OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換）。"""
    configured: OpenaiEndpointConfigured
    effective: OpenaiEndpointEffective
    kinds: list[str]
    auth_headers: list[str]


class ArmsInfo(BaseModel):
    known: list[str]
    enabled: list[str]
    configured: list[str] | None
    env_default: list[str]
    available: dict[str, bool]


class LibreofficeInfo(BaseModel):
    available: bool
    version: str | None


class OfficeComInfo(BaseModel):
    configured_url: bool
    mode: str
    powershell: bool
    available: bool
    versions: dict[str, Any] | None


class RequiredToolInfo(BaseModel):
    id: str
    label: str
    installed: bool
    version: str | None = None
    used_by: list[str]
    how_to_install: str
    detail: str | None = None


class LegacyBackendInfo(BaseModel):
    configured: str | None
    effective: str
    default: str
    options: list[str]
    libreoffice: LibreofficeInfo
    office_com: OfficeComInfo


class RagLlmRenderInfo(BaseModel):
    """rag.md の LLM 成形トグル。"""
    configured: str | None
    effective: bool
    default: bool
    options: list[str]


class VlmInfo(BaseModel):
    configured: dict[str, Any] | None
    effective: dict[str, Any]
    default: dict[str, Any]
    available: bool
    providers: list[str]
    openai_key_present: bool


class OllamaAllowlistInfo(BaseModel):
    configured: list[str] | None
    effective: list[str]


class WebhookAllowlistInfo(BaseModel):
    """Webhook 宛先の許可リスト（`configured`=管理者の生値・`effective`=実際に許可される非 loopback の host:port）。"""
    configured: list[str] | None
    effective: list[str]


class CodexSessionRetentionInfo(BaseModel):
    """`configured`=管理者が保存した生値（未設定なら null）、`effective`=実効値、`default`=未設定時の既定日数（`0` は明示設定時のみ「無制限」）。"""
    configured: int | None
    effective: int
    default: int


class ChatExamplesAdminInfo(BaseModel):
    """チャット画面のクイック入力例。`configured`=保存されている生値（未設定なら null）、`effective`=実際に表示される内容（非表示なら空リスト）、`default`=組み込み既定4例。"""
    configured: dict[str, Any] | None
    effective: list[str]
    default: list[str]
    max_items: int
    max_item_length: int


class ModelCatalogAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `model_catalog`。`configured`=管理者が保存した生値（未設定なら null）、`effective`=組み込み既定に管理者設定を重ねた解決結果（プロバイダ→用途→セル）。"""
    configured: dict[str, dict[str, ModelChoiceInfo]] | None
    effective: dict[str, dict[str, ModelChoiceInfo]]
    # 組み込み既定のみの解決結果（セルの値が既定と異なるかの判定基準）
    builtin: dict[str, dict[str, ModelChoiceInfo]]
    providers: list[str]
    usages: list[str]


class DepthProfileBaseInfo(BaseModel):
    """調べる深さの整数系基準値1項目（標準時の値）。`configured`=管理者が保存した生値（未設定なら null）、`effective`=解決結果、`default`=未設定に戻したときの実効値。"""
    configured: int | None
    effective: int
    default: int


class DepthProfileCodexReasoningInfo(BaseModel):
    """Codex 推論レベルの基準値（`sherpa.depth_profile.CODEX_REASONING_LEVELS` の1つ）。"""
    configured: str | None
    effective: str
    default: str
    options: list[str]


class CodexWorkerModelInfo(BaseModel):
    """multi_agent の worker モデル。`configured`=管理者が保存した生値（未設定なら null）、`effective`=解決結果、`default`=フォールバック値。"""
    configured: str | None
    effective: str
    default: str


class CodexModeInfo(BaseModel):
    """`codex_mode`（素の Codex モード）。`configured`=管理者が保存した生値（未設定なら null）、`effective`=解決結果、`default`="standard"、`options`=選べる値。"""
    configured: str | None
    effective: str
    default: str
    options: list[str]


class EmbedProviderInfo(BaseModel):
    """`embed_provider`（埋め込みの接続先）。`ollama_model` は "ollama" のとき使う埋め込みモデル名。"""
    configured: str | None
    effective: str
    default: str
    options: list[str]
    ollama_model: str


class DepthProfileAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `depth_profile`。調べる深さ（クイック/標準/深く/最大）が掛ける倍率の基準値（クイック・標準時の値）のみを持つ（倍率表は固定）。
    `codex_reasoning` は深さで変わらない。"""
    grep_max_hits: DepthProfileBaseInfo
    qa_max_hits: DepthProfileBaseInfo
    read_window: DepthProfileBaseInfo
    impact_depth: DepthProfileBaseInfo
    troubleshoot_depth: DepthProfileBaseInfo
    codex_reasoning: DepthProfileCodexReasoningInfo


class ChatMaxTurnsAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `chat_max_turns`（同時実行の上限）。`per_user`/`global` は `DepthProfileBaseInfo` と同型。
    `global` は予約語のため属性名は `global_`（JSON 上のキーは `global`）。"""
    per_user: DepthProfileBaseInfo
    global_: DepthProfileBaseInfo = Field(alias="global")


class AgenticBudgetAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `agentic_budget`（Codex の MCP ツール結果1件あたりのバイト予算）。`DepthProfileBaseInfo` と同型。"""
    per_result: DepthProfileBaseInfo


class WorkspaceAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `workspace`（個人ファイル）。`max_bytes`=1 件の上限（バイト）、`ttl_days`=保持日数（0 は無期限）。どちらも `DepthProfileBaseInfo` と同型。"""
    max_bytes: DepthProfileBaseInfo
    ttl_days: DepthProfileBaseInfo


class AdminSettingsView(BaseModel):
    """GET・PUT /admin/settings（全キー常時存在）。

    response_model は付与しない（`legacy_backend.libreoffice.version` が OpenAPI に `version` を露出させるため）。
    """
    cloud: CloudInfo
    ext_keys: ExtKeysAdminInfo
    openai_endpoint: OpenaiEndpointInfo
    model_catalog: ModelCatalogAdminInfo
    arms: ArmsInfo
    legacy_backend: LegacyBackendInfo
    required_tools: list[RequiredToolInfo]
    rag_llm_render: RagLlmRenderInfo
    vlm: VlmInfo
    ollama_allowlist: OllamaAllowlistInfo
    webhook_allowlist: WebhookAllowlistInfo
    codex_session_retention_days: CodexSessionRetentionInfo
    depth_profile: DepthProfileAdminInfo
    chat_max_turns: ChatMaxTurnsAdminInfo
    agentic_budget: AgenticBudgetAdminInfo
    workspace: WorkspaceAdminInfo
    # 埋め込み HTTP の同時送信数。`DepthProfileBaseInfo` と同型
    embed_parallel: DepthProfileBaseInfo
    embed_provider: EmbedProviderInfo
    # 「最大」の深さが許す査読の巡数。`DepthProfileBaseInfo` と同型
    max_review_rounds: DepthProfileBaseInfo
    # multi_agent の worker モデル
    codex_worker_model: CodexWorkerModelInfo
    # 素の Codex モード（default="standard"）
    codex_mode: CodexModeInfo
    chat_examples: ChatExamplesAdminInfo


# ---- 運営掲示板 ----

class AnnouncementOut(BaseModel):
    id: int
    author_uid: str
    title: str
    body: str
    category: str
    pinned: bool
    published: bool
    publish_at: WireDateTime | None
    expire_at: WireDateTime | None
    status: str
    created_at: WireDateTime
    updated_at: WireDateTime


class AnnouncementsListResponse(BaseModel):
    """お知らせ一覧（GET /announcements）。"""
    announcements: list[AnnouncementOut]


class AnnouncementMutateResponse(BaseModel):
    """お知らせの作成・更新結果（POST /admin/announcements・PATCH /admin/announcements/{id}）。"""
    ok: bool
    announcement: AnnouncementOut


# ===== 管理者:ユーザー管理（sherpa/routers/admin_users.py） =====

class UserRow(BaseModel):
    """ユーザー一覧（GET /admin/users）の1行。"""
    uid: str
    email: str | None
    display_name: str | None
    role: str
    status: str
    must_change_password: bool
    last_login_at: WireDateTime | None


class AdminUsersListResponse(BaseModel):
    users: list[UserRow]


class UserCreateRow(BaseModel):
    """ユーザー作成の応答（`last_login_at` を持たない・`UserRow` と別形）。"""
    uid: str
    email: str | None
    display_name: str | None
    role: str
    status: str
    must_change_password: bool


class AdminUserCreateResponse(BaseModel):
    ok: bool
    user: UserCreateRow


class AdminUserImportResponse(BaseModel):
    created: int
    uids: list[str]


class AdminUserPatchResponse(BaseModel):
    ok: bool
    uid: str


# ===== 管理者:監査ログ（sherpa/routers/audit_usage.py） =====

class AuditRow(BaseModel):
    """監査ログ（GET /admin/audit）の1行。値は自由形式 JSON を含む。"""
    id: int
    actor_user_id: str | None
    action: str
    resource_type: str | None
    resource_id: str | None
    detail: Any
    outcome: str | None
    reason: str | None
    severity: str | None
    request_id: str | None
    session_id: str | None
    ip_hash: str | None
    user_agent: str | None
    before_state: Any
    after_state: Any
    created_at: WireDateTime


class AdminAuditListResponse(BaseModel):
    rows: list[AuditRow]
    count: int
    offset: int
    limit: int


# ---- 管理者:利用統計。全キーが常時存在するため response_model を付与する ----

class UsageLens(BaseModel):
    impact: int
    qa: int
    troubleshoot: int
    chat: int
    investigate: int = 0
    author: int = 0


class UsageUserRow(BaseModel):
    uid: str
    display_name: str
    turns: int
    conversations: int
    active_days: int
    last_active: WireDateTime | None
    lens: UsageLens
    personal_turns: int
    worlds: list[str]
    logins: int
    downloads: int
    uploads: int
    shares: int
    knowledge_turns: int
    zero_hit_turns: int
    zero_hit_rate: float | None


class UsageTotals(BaseModel):
    turns: int
    active_users: int
    conversations: int


class UsageDailyPoint(BaseModel):
    date: str
    turns: int
    active_users: int


class UsagePeriod(BaseModel):
    start: str
    end: str
    days: int
    # 実際に使った半開区間 `[from, to)`（ISO 8601・オフセット付き）。`start`/`end` は JST 暦日で `end` は含む終了日。期間指定を持たない集計では未設定のことがある
    from_: str | None = Field(default=None, alias="from")
    to: str | None = None


class UsageZeroHit(BaseModel):
    knowledge_turns: int
    zero_hit_turns: int
    rate: float | None


class UsageWorldRow(BaseModel):
    world: str
    turns: int


class UsageProviderRow(BaseModel):
    provider: str
    turns: int


class UsageStopKindRow(BaseModel):
    """ターンの終了理由。`completed`/`stopped_by_user`/`budget`/`no_evidence`/`transport_error`/`timeout`/`codex_silent`/`codex_partial` の8値、または過去データ・未計測経路の `unknown`。"""
    stop_kind: str
    turns: int


class UsageCompletionRow(BaseModel):
    """回答の完了状態。`complete`/`partial`/`stopped`/`failed`、または旧形式の行の `unknown`。"""
    completion: str
    turns: int


class UsageHeatmapCell(BaseModel):
    weekday: int
    hour: int
    count: int


class UsageRetentionWeek(BaseModel):
    week_start: str
    active_users: int


class UsageRetention(BaseModel):
    weekly: list[UsageRetentionWeek]
    revisit_rate: float | None


class UsageDownloadDaily(BaseModel):
    date: str
    count: int


class UsageDownloads(BaseModel):
    total: int
    daily: list[UsageDownloadDaily]


class UsageTokenByModel(BaseModel):
    provider: str
    model: str
    turns: int
    input: int
    cached_input: int
    cache_write: int | None
    cache_write_unknown: int
    output: int
    reasoning_output: int


class UsageTokenByUser(BaseModel):
    uid: str
    display_name: str
    turns: int
    input: int
    cached_input: int
    cache_write: int | None
    cache_write_unknown: int
    output: int
    reasoning_output: int


class UsageTokenDaily(BaseModel):
    date: str
    input: int
    output: int


class UsageTokenTotals(BaseModel):
    turns: int
    input: int
    cached_input: int
    cache_write: int | None
    cache_write_unknown: int
    output: int
    reasoning_output: int


class UsageTokenByKind(BaseModel):
    """用途別（kind）内訳。

    `cache_write`（キャッシュへの書き込み）は報告のあった行だけの合計で、全行が不明なら None・`cache_write_unknown` は不明だった行（ターン／呼び出し）の数（0 より大きければ「不明を含む」）。

    chat 行は `messages.answer->'usage'` 由来（トークン列は常に int）。それ以外の kind は `usage_events` 由来で、プロバイダが usage を報告しなかった行はトークン列が None（0 に丸めない）。
    `elapsed_ms_total`/`elapsed_ms_avg`/`elapsed_n` は計測ありの行の合計・平均・行数（chat 行は対象外＝total/avg=None・n=0）。
    """
    kind: str
    provider: str
    model: str
    calls: int
    input: int | None
    cached_input: int | None
    cache_write: int | None
    cache_write_unknown: int
    output: int | None
    reasoning_output: int | None
    elapsed_ms_total: int | None
    elapsed_ms_avg: float | None
    elapsed_n: int


class UsageTokenByUserKind(BaseModel):
    """ユーザー別 × 用途別（kind）内訳（`UsageTokenByKind` をユーザーごとに分けたもの）。利用者に紐付かない呼び出しは含まれない。"""
    uid: str
    display_name: str
    kind: str
    calls: int
    input: int | None
    cached_input: int | None
    cache_write: int | None
    cache_write_unknown: int
    output: int | None
    reasoning_output: int | None
    elapsed_ms_total: int | None
    elapsed_ms_avg: float | None
    elapsed_n: int


class UsageTokens(BaseModel):
    totals: UsageTokenTotals
    by_model: list[UsageTokenByModel]
    by_user: list[UsageTokenByUser]
    daily: list[UsageTokenDaily]
    by_kind: list[UsageTokenByKind]
    by_user_kind: list[UsageTokenByUserKind]


class UsageConversationTurns(BaseModel):
    """会話あたりの user ターン数分布（期間内の user ターンが1件以上ある会話が対象。対象が無ければ分布は None）。
    `session_eligible` は期間内2ターン以上の会話数、`session_recorded` はそのうち現在の Codex セッション ID を持つ会話数。
    """
    avg: float | None
    median: float | None
    max: int | None
    p90: float | None
    session_eligible: int
    session_recorded: int


class UsageResponseTimeRow(BaseModel):
    """回答時間（ミリ秒）の分布統計（1グループ分）。`provider` は経路別行でのみ設定され、全体行では `None`。対象0件なら `avg`/`median`/`max`/`p90` は `None`（`n`=0）。"""
    provider: str | None
    avg: float | None
    median: float | None
    max: int | None
    p90: float | None
    n: int


class UsageResponseTime(BaseModel):
    """回答時間の分布：全体（`overall`）と経路別（`by_provider`）。

    対象は期間内の assistant 行のうち `duration_ms` が保存されている行のみ（確認カードと、実行中・保存されずに止まったターンは含まない。停止しても回答が保存されたターンは含む）。
    """
    overall: UsageResponseTimeRow
    by_provider: list[UsageResponseTimeRow]


class UsageConversationKindRow(BaseModel):
    """会話ごとの用途別（kind）内訳（`conversations_top[].kinds` の1行）。null の意味は `UsageTokenByKind` と同じ。"""
    kind: str
    calls: int
    input: int | None
    cached_input: int | None
    cache_write: int | None
    cache_write_unknown: int
    output: int | None
    reasoning_output: int | None
    elapsed_ms_total: int | None
    elapsed_ms_avg: float | None
    elapsed_n: int


class UsageConversationRow(BaseModel):
    """会話ごとの補助 AI 使用量。トークン合計の降順で上位20件のみ。
    `user_turns` は期間内の user ターン数、`response_time_avg_ms` は `duration_ms` が保存された assistant 行のみの平均（0件なら None）。タイトル・本文は含まない。
    """
    conversation_id: int
    uid: str
    display_name: str
    world: str | None
    user_turns: int
    kinds: list[UsageConversationKindRow]
    response_time_avg_ms: float | None


class UsageLimitsByProviderRow(BaseModel):
    """経路（provider）別の「打ち切りの内訳」（計測専用）。

    `turns` は対象ターン総数（分母）。`*_turns` は回数系キーが1回以上／bool系キーが真だったターン数、`*_total` は回数系キーの合計回数。経路で記録しない項目は None。
    """
    provider: str
    turns: int
    tool_result_clipped_turns: int | None
    tool_result_clipped_total: int | None
    total_budget_hit_turns: int | None
    context_compactions_turns: int | None
    context_compactions_total: int | None
    synthesis_truncated_turns: int | None
    # 必要な根拠種別が揃わず深さを1段だけ自動で引き上げたターン数
    depth_escalated_turns: int | None
    search_truncated_turns: int | None
    search_truncated_total: int | None
    auto_continues_turns: int | None
    auto_continues_total: int | None
    # 同一条件のツール呼び出しを2回目以降省略した回数（Codex 経路のみ）
    duplicate_tool_call_turns: int | None
    duplicate_tool_call_total: int | None
    # 縮退（バックエンド不調）の計数（このターンで初めて検出された回数）
    backend_unavailable_fulltext_turns: int | None
    backend_unavailable_graph_turns: int | None
    graph_reingest_required_turns: int | None
    # MCP ツール呼び出し回数の上限に到達したターン数（Codex 経路のみ）
    tool_calls_exhausted_turns: int | None


class UsageLimits(BaseModel):
    """内部制限の打ち切り分布。"""
    by_provider: list[UsageLimitsByProviderRow]


class UsageRoundDepthProviderRow(BaseModel):
    """深さ×経路別の巡別記録の活動量（表示専用・課金の正本ではない）。
    `limits`: 巡内 limits 増分の合算。`verdicts`/`stops`: evaluator の判定・巡を止めた理由の分類別件数。`missing_codes`: 不足軸の閉じた分類別件数。
    """
    depth_profile: str
    provider: str
    rounds: int
    citations_delta_total: float
    citations_delta_avg: float | None
    elapsed_ms_total: int
    elapsed_ms_avg: float | None
    elapsed_n: int
    input_tokens: int
    output_tokens: int
    tokens_n: int
    claims: dict[str, int]
    reason_codes: dict[str, int]
    limits: dict[str, int]
    verdicts: dict[str, int]
    stops: dict[str, int]
    missing_codes: dict[str, int]


class UsageRoundByRoundRow(BaseModel):
    """深さ×経路×巡番号別の巡別記録の活動量（`UsageRoundDepthProviderRow` を巡番号単位で分けたもの。`round_no` が取れない行は `None`）。"""
    depth_profile: str
    provider: str
    round_no: int | None
    rounds: int
    citations_delta_total: float
    citations_delta_avg: float | None
    elapsed_ms_total: int
    elapsed_ms_avg: float | None
    elapsed_n: int
    input_tokens: int
    output_tokens: int
    tokens_n: int
    claims: dict[str, int]
    reason_codes: dict[str, int]
    limits: dict[str, int]
    verdicts: dict[str, int]
    stops: dict[str, int]
    missing_codes: dict[str, int]


class UsageRoundDistributionRow(BaseModel):
    """深さ×経路×到達巡数ごとのターン件数。`rounds_reached` はそのターンで到達した巡番号の最大値。"""
    depth_profile: str
    provider: str
    rounds_reached: int
    turns: int


class UsageReasonCodeRow(BaseModel):
    """不明（`status='unknown'`）の理由コード分布1件（主張単位・深さ×経路別）。"""
    depth_profile: str
    provider: str
    reason_code: str
    claims: int


class UsageRoundReasonCodes(BaseModel):
    """理由コード分布: `final`＝最終回答の主張、`rounds`＝巡別記録の主張内訳を巡単位で合算したもの。"""
    final: list[UsageReasonCodeRow]
    rounds: list[UsageReasonCodeRow]


class UsageRounds(BaseModel):
    """巡別記録（本文は含まない）。"""
    by_depth_provider: list[UsageRoundDepthProviderRow]
    by_round: list[UsageRoundByRoundRow]
    round_distribution: list[UsageRoundDistributionRow]
    unmatched_rounds: int
    reason_codes: UsageRoundReasonCodes


class UsageQualityRunRow(BaseModel):
    """品質採点の条件×巡数別集計（件数と費用のみ・質問/回答の本文は保存しない）。
    `condition`: 採点した条件（閉集合）。巡数だけでは区別できない条件同士を分けて集計する。
    """
    condition: str | None
    rounds: int
    runs: int
    correct: int
    wrong_assertion: int
    missing: int
    regressed: int
    unrated: int
    cost_usd_total: float | None


class UsageQualityRuns(BaseModel):
    period: UsagePeriod
    by_rounds: list[UsageQualityRunRow]


class AdminUsageQualityRunReq(BaseModel):
    """品質採点の登録入力（POST /admin/usage/quality-runs）。質問/回答の本文は受け付けない。

    件数・`rounds` は0以上。`condition` は閉集合。`executed_from`/`executed_to` は質問セットを実行した期間（ISO 8601・オフセット必須・半開区間 `[from, to)`）。
    `cost_usd` は任意（有限・非負。違反は定型メッセージの 422）。`run_id`（任意）は冪等キーで、同じ値で再送しても2行目を作らない。
    """
    rounds: StrictInt = Field(ge=0)
    condition: Literal["main", "depth2-quick", "depth2-standard", "depth2-deep", "depth2-max"]
    executed_from: StrictStr = Field(min_length=1, max_length=64)
    executed_to: StrictStr = Field(min_length=1, max_length=64)
    correct: StrictInt = Field(default=0, ge=0)
    wrong_assertion: StrictInt = Field(default=0, ge=0)
    missing: StrictInt = Field(default=0, ge=0)
    regressed: StrictInt = Field(default=0, ge=0)
    unrated: StrictInt = Field(default=0, ge=0)
    cost_usd: float | None = Field(default=None)
    run_id: StrictStr | None = Field(default=None, min_length=1, max_length=200)


class AdminUsageQualityRunAck(BaseModel):
    ok: bool = True
    # `run_id` 重複で新規行を作らなかったときは False
    inserted: bool = True


class UsageToolCallRole(BaseModel):
    calls: int
    found: int
    ms: int
    errors: int
    truncated: int


class UsageToolCallRow(UsageToolCallRole):
    """道具ごとの累計（`by_role` は parent/child/undetermined の内訳）。"""
    tool: str
    by_role: dict[str, UsageToolCallRole]


class UsageToolCalls(BaseModel):
    """道具の呼び出しの累計（記録のあるターンだけ・過去のターンは含まない）。"""
    turns: int
    missing: int
    tools: list[UsageToolCallRow]
    nudged_turns: int = 0
    nudged_then_used: int = 0
    no_tool_use_turns: int = 0
    opened_docs: int = 0
    opened_unknown_turns: int = 0


class UsageImpact(BaseModel):
    """影響一覧のあるターンだけの件数（行の名前・パスは持たない）。"""
    traced_turns: int = 0
    untraced_turns: int = 0
    candidate: int = 0
    inspected: int = 0
    used: int = 0
    unmapped: int = 0
    more: int = 0
    hidden: int = 0


class AdminUsageStatsResponse(BaseModel):
    """利用統計（GET /admin/usage/stats）。"""
    users: list[UsageUserRow]
    totals: UsageTotals
    daily: list[UsageDailyPoint]
    period: UsagePeriod
    zero_hit: UsageZeroHit
    worlds: list[UsageWorldRow]
    providers: list[UsageProviderRow]
    heatmap: list[UsageHeatmapCell]
    retention: UsageRetention
    downloads: UsageDownloads
    tokens: UsageTokens
    conversation_turns: UsageConversationTurns
    resume_rate: float | None
    stop_kinds: list[UsageStopKindRow]
    stopped_turns: int
    completions: list[UsageCompletionRow]
    response_time: UsageResponseTime
    conversations_top: list[UsageConversationRow]
    limits: UsageLimits
    tool_calls: UsageToolCalls = Field(default_factory=lambda: UsageToolCalls(turns=0, missing=0, tools=[]))
    impact: UsageImpact = Field(default_factory=UsageImpact)
    rounds: UsageRounds
    quality_runs: UsageQualityRuns


# ===== 個人ワークスペース（sherpa/routers/workspace.py） =====

class WorkspaceFileRow(BaseModel):
    """個人 workspace のファイル一覧（GET /workspace/files）。`created_at`/`expires_at` は文字列。"""
    id: int
    rel_path: str
    size_bytes: int
    created_at: str
    expires_at: str | None


class WorkspaceFilesListResponse(BaseModel):
    files: list[WorkspaceFileRow]


class WorkspaceFileUploadResponse(BaseModel):
    ok: bool
    id: int
    rel_path: str
    size_bytes: int
    sha256: str


class WorkspaceFileDeleteResponse(BaseModel):
    ok: bool
    id: int
    rel_path: str


class WorkspaceSearchHit(BaseModel):
    rel_path: str
    line: int
    text: str
    match: str


class WorkspaceSearchResponse(BaseModel):
    query: str
    source: str
    hits: list[WorkspaceSearchHit]


# ===== 資料フォルダ管理（sherpa/routers/worlds.py） =====

class PublicWorld(BaseModel):
    """公開用の資料フォルダ情報。"""
    world_id: str
    label: str | None
    root_path: str | None
    storage_mode: str | None


class WorldsListResponse(BaseModel):
    """資料フォルダ一覧（GET /worlds）。"""
    worlds: list[PublicWorld]


class WorldOptionsResponse(BaseModel):
    """資料フォルダの選択肢（GET /world-options）。"""
    worlds: list[str]
    labels: dict[str, str]


class FsEntry(BaseModel):
    name: str
    path: str


class FsListResponse(BaseModel):
    """フォルダ一覧（GET /fs/list）。"""
    path: str
    parent: str | None
    entries: list[FsEntry]


# ---- GET /ingest/preview ----
# response_model は付与しない（`documents[].provenance`・`documents[].importance` が条件付きキーのため。TypeAdapter 契約のみ）

class IngestPreviewDocument(BaseModel):
    name: str
    doctype: str | None
    branch: str | None
    analyzer: str | None
    top_scope: str | None
    phase: str | None
    category: str | None
    folder: str
    state: str
    label: str
    reason: str | None
    provenance: dict[str, Any] | None = None
    importance: Literal["高", "中", "低"] | None = None
    importance_reason: str | None = None
    importance_source: str | None = None


class IngestPreviewEntity(BaseModel):
    name: str
    label: str
    status: str
    value: Any
    top_scope: str | None
    phase: str | None
    path: str | None
    analyzer: str | None = None


class IngestPreviewRelation(BaseModel):
    type: str
    src: str
    dst: str
    src_label: str
    dst_label: str
    status: str
    doc: str


class IngestPreviewCounts(BaseModel):
    entities: int
    relations: int
    deprecated: int
    hidden: int
    documents: int


class ImportanceDiagnostic(BaseModel):
    """`_重要度.txt` の構文診断1件。"""
    config_path: str
    line: int | None
    column: int
    code: str
    message: str


class IngestPreviewResponse(BaseModel):
    """取り込みのプレビュー（GET /ingest/preview）。response_model は付与しない（条件付きキーのため）。"""
    world: str
    label: str | None
    counts: IngestPreviewCounts
    documents: list[IngestPreviewDocument]
    issues: list[Any]
    entities: list[IngestPreviewEntity]
    relations: list[IngestPreviewRelation]
    importance_diagnostics: list[ImportanceDiagnostic]


class IngestBlockedDoc(BaseModel):
    """`last_run_blocked` の要素（対象ファイルが特定できる blocked flag の doc/reason）。"""
    doc: str
    reason: str


class IngestNotice(BaseModel):
    """`ingest_notices` の要素。取り込みで黙って落とした・粗くしたものの種類（`code`）と件数（`count`）。`count` は数えられない種類では 1。"""
    code: str
    count: int


class RunProgress(BaseModel):
    """実行中 run の逐次進捗。`stage_label` は内部段キー（`stage`）に対応する利用者向けの平文。`done`/`total` はファイル単位の進捗（件数を持たない段では両方 `None`）。"""
    stage: str
    stage_label: str
    done: int | None
    total: int | None
    updated_at: str


class IngestSummaryFields(BaseModel):
    """資料フォルダの取り込み状況（スキャン結果のキャッシュ＋グラフ/ES 件数・最終実行状態）。

    `scanned`〜`unreadable` は sync 完走時と `POST /worlds/{wid}/recount` のときだけ更新されるキャッシュで、このエンドポイント自身はフォルダを歩かない。
    `counts_as_of` はそのキャッシュの記録時刻（`None`＝未集計）。`sensitive_excluded`/`unreachable_as_text`/`unreachable_as_text_by_ext`/
    `unreachable_by_reason`（`"encoding_undetermined"`/`"binary"`）/`encoding_partial_count`（一部が化けている件数）も同じキャッシュ由来。
    `failed_files`/`partial_extraction_suspected`/`stage_summary` は最新 run の由来（無ければ `None`）。
    `failure_reason_catalog`/`partial_extraction_advice` は閉じた理由語彙の平文辞書。
    `walk_skipped`＝木の走査で辿らなかったものの件数（`symlink`/`unreadable_dir`/`unreadable_file`/`outside_root`・集計キャッシュ由来）。
    `ingest_notices`＝黙って落とした・粗くしたものの `{code, count}`（走査・関係グラフ・全文索引の各段から導出・名前は持たない。無ければ空）。
    `last_run_warnings`/`last_run_blocked` は flags を打ち切って導出したもの（`last_run_flags_total`=打切り前の総数・`last_run_flags_truncated`=打切りの有無）。
    `last_run_id`/`running_progress` は最新 run の id と実行中進捗（実行中でなければ `running_progress` は `None`）。
    """
    scanned: int
    indexed: int
    by_doctype: dict[str, int]
    office_md: int
    skipped_office: int
    office_failed: int
    skipped_other: int
    skipped_ext: dict[str, int]
    analyzer_declined: int
    analyzer_declined_as_document: int
    unreadable: int
    sensitive_excluded: int
    unreachable_as_text: int
    unreachable_as_text_by_ext: dict[str, int]
    unreachable_by_reason: dict[str, int]
    encoding_partial_count: int
    walk_skipped: dict[str, int] = {}
    ingest_notices: list[IngestNotice] = []
    counts_as_of: str | None
    graph_nodes: int
    graph_edges: int
    es_chunks: int | None
    es_state: Literal["ok", "reflecting", "failed", "unavailable", "unknown"]
    es_error: str | None
    es_index_kept: bool | None
    last_run_id: int | None
    last_run_status: str | None
    last_run_warnings: list[str]
    last_run_blocked: list[IngestBlockedDoc]
    last_run_flags_total: int
    last_run_flags_truncated: bool
    failed_files: dict[str, Any] | None
    partial_extraction_suspected: dict[str, Any] | None
    stage_summary: dict[str, Any] | None
    running_progress: RunProgress | None
    failure_reason_catalog: dict[str, dict[str, str]]
    partial_extraction_advice: str


class WorldStatusResponse(IngestSummaryFields):
    """資料フォルダの状況（GET /worlds/{wid}/status）。取り込み状況の全キーをフラットに展開する。`last_synced_at` は文字列。"""
    ok: bool
    world_id: str
    label: str | None
    root_path: str | None
    last_synced_at: str | None
    resolve_settings_pending: bool   # 資料の探し方の設定が、最後に作ったグラフにまだ反映されていない（「更新」で取り込み直す）


class WorldDiffResponse(BaseModel):
    """差分プレビュー（POST /worlds/diff）。全キー常時存在。"""
    ok: bool
    registered: bool
    world_id: str | None
    label: str | None
    root_path: str
    added: list[str]
    removed: list[str]
    changed: list[str]
    total: int
    indexed: int


class WorldResolveSettingsResponse(BaseModel):
    """資料フォルダの解決範囲の設定（GET/PUT /worlds/{wid}/resolve-settings）。

    `copy_paths`＝COPY の取り込み元の場所（優先順・資料フォルダの root からの相対パス）。`path_aliases`＝パスの別名 → 場所。
    `warnings` は資料フォルダの中に見つからない場所（保存は止めない）。`changed` は PUT で内容が変わったか
    （変わると資料フォルダ全体を取り込み直す）。`refresh_started` は PUT でその取り込み直しを起こせたか（false で changed なら別の処理が実行中＝更新待ち）。GET では常に false。
    """
    ok: bool
    world_id: str
    copy_paths: list[str]
    path_aliases: dict[str, str]
    warnings: list[str]
    changed: bool
    refresh_started: bool
    note: str


class WorldIngestAcceptedResponse(BaseModel):
    """取り込み操作の受付結果（POST /worlds・POST /worlds/{wid}/refresh・DELETE /worlds/{wid}・POST /worlds/{wid}/rebind・POST /ingest/rerun。HTTP 202）。

    本体処理は背景で継続し、この応答は「受け付けた」ことだけを示す。`run_id` は受付時点で必ず判明している（非 null）。
    `joined=True` は多重クリック制御（操作種別＋正規化 payload の一致）により新規実行せず既存 run へ合流した場合（不一致なら 409）。
    進捗・結果は `GET /worlds/{wid}/status` で確認する（削除成功後は 404）。
    """
    ok: bool
    world_id: str
    run_id: int
    joined: bool
    note: str


class WorldRecountResponse(IngestSummaryFields):
    """再集計（POST /worlds/{wid}/recount）。スキャンを明示的に再実行してキャッシュし直し、取り込み状況をフラットに展開して返す（同期）。"""
    ok: bool
    world_id: str


class WorldReconvertResponse(BaseModel):
    """1ファイルの再変換（POST /worlds/{wid}/reconvert）。旧形式変換キャッシュを落として world 全体を sync する（同期）。`summary` は取り込み状況をネストする。"""
    ok: bool
    world_id: str
    rel: str
    changed: bool
    status: str
    ledger: int | None
    flags: list[Any]
    summary: IngestSummaryFields
    note: str


# ===== ナレッジグラフ（sherpa/routers/graph.py） =====

class GraphNode(BaseModel):
    """グラフのノード（GET /graph の nodes）。"""
    id: str
    name: str | None
    type: str | None
    type_ja: str | None
    status: str
    value: Any
    top_scope: str | None
    path: str | None


class GraphEdge(BaseModel):
    """グラフのエッジ（GET /graph の edges）。"""
    source: str
    target: str
    type: str
    status: str


class GraphCounts(BaseModel):
    """グラフの件数。"""
    entities: int
    relations: int
    deprecated: int
    hidden: int
    documents: int


class GraphResponse(BaseModel):
    """グラフ全体（GET /graph）。"""
    world: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    counts: GraphCounts
    total_nodes: int
    total_edges: int
    truncated: bool


class GraphFacetsResponse(BaseModel):
    """グラフの絞り込み候補（GET /graph/facets）。"""
    node_labels: list[str]
    node_labels_ja: dict[str, str]
    relationship_types: list[str]
    condition_fields: list[str]


class GraphSearchNode(BaseModel):
    """検索結果のノード（GET /graph/search。`GraphNode` と別形で phase/category を持つ）。"""
    id: str
    name: str | None
    type: str | None
    type_ja: str | None
    status: str
    value: Any
    top_scope: str | None
    phase: str | None
    category: str | None
    path: str | None


class GraphSearchCounts(BaseModel):
    """検索結果の件数（`{"nodes":.., "edges":..}`・`GraphCounts` と別形）。"""
    nodes: int
    edges: int


class GraphSearchResponse(BaseModel):
    """グラフ検索の結果（GET /graph/search）。"""
    world: str
    nodes: list[GraphSearchNode]
    edges: list[GraphEdge]
    counts: GraphSearchCounts


# ===== 範囲（sherpa/routers/impact.py::scopes） =====

class ScopeItem(BaseModel):
    path: str
    label: str
    depth: int
    count: int


class ScopesResponse(BaseModel):
    """範囲セレクタ用のフォルダ木（GET /scopes）。"""
    world: str
    label: str | None
    scopes: list[ScopeItem]


# ===== 会話管理・会話共有（sherpa/routers/conversations.py・shares.py） =====

class ForkedFromInfo(BaseModel):
    """フォーク元の出所表示（編集不可）。`name` は表示名未設定/退会等で `None` になりうる。"""
    share_id: int
    user_id: str
    name: str | None
    at: WireDateTime


class ConversationSearchMatch(BaseModel):
    """履歴検索（`GET /conversations?q=`）が行に付ける一致箇所。`where="title"` はタイトル一致、`"message"` は本文一致。"""
    where: Literal["title", "message"]
    snippet: str


class ConversationSummary(BaseModel):
    """会話一覧（`GET /conversations`）の1行。`match` は `q` 指定時のみ付く。response_model は付与しない（`version` が OpenAPI に露出するため）。"""
    id: int
    title: str | None
    version: str
    pinned: bool
    updated_at: WireDateTime
    origin: str
    read_only: bool
    received_at: WireDateTime | None
    shared_by_user_id: str | None
    shared_by_name: str | None
    share_status: str | None
    share_expires_at: WireDateTime | None = None  # 受領共有の実効期限（それ以外は None）
    forked_from: ForkedFromInfo | None = None
    match: ConversationSearchMatch | None = None


class UserSuggestItem(BaseModel):
    uid: str
    display_name: str | None


class UsersSuggestResponse(BaseModel):
    """ユーザー候補（GET /users/suggest）。"""
    users: list[UserSuggestItem]


class ShareCreateResponse(BaseModel):
    """会話共有の作成（POST /conversations/{cid}/shares）。"""
    ok: bool
    share_id: int
    url: str
    note: str


class ConversationForkResponse(BaseModel):
    """会話のフォーク（POST /conversations/{wid}/fork）。"""
    ok: bool
    conversation_id: int


class ConversationShareRefreshResponse(BaseModel):
    """会話共有の更新（POST /conversation-shares/{share_id}/refresh）。"""
    ok: bool
    share_id: int
    refreshed_at: WireDateTime


class ConversationShareExtendResponse(BaseModel):
    """会話共有の期限延長（POST /conversation-shares/{share_id}/extend）。"""
    ok: bool
    share_id: int
    expires_at: WireDateTime


class ShareInviteeItem(BaseModel):
    uid: str
    name: str | None
    accepted_at: WireDateTime | None


class ShareListItem(BaseModel):
    """会話共有一覧（`GET /conversations/{cid}/shares`）の1件。"""
    share_id: int
    sanitized: bool
    created_at: WireDateTime
    expires_at: WireDateTime | None
    revoked_at: WireDateTime | None
    refreshed_at: WireDateTime | None
    last_used_at: WireDateTime | None
    invitees: list[ShareInviteeItem]


# ===== チャット（sherpa/routers/chat.py） =====

class ChatTurnStartResponse(BaseModel):
    """チャットターンの開始（POST /chat/turns）。"""
    turn_id: str
    conversation_id: int


class ChatTurnStopResponse(BaseModel):
    """チャットターンの停止（POST /chat/turns/{turn_id}/stop）。"""
    ok: bool


class ChatTurnRunning(BaseModel):
    """`GET /chat/turns/running` の1要素。`started_at` は文字列。"""
    turn_id: str
    conversation_id: int
    started_at: str
    uid: str | None = None  # `all=true`（管理者）のときだけ値が入る（それ以外は null）


class ChatTurnsRunningResponse(BaseModel):
    turns: list[ChatTurnRunning]



# ===== 回答への評価（sherpa/routers/feedback_admin.py） =====

class FeedbackTagCount(BaseModel):
    tag: str
    count: int
    down: int


class FeedbackModeCount(BaseModel):
    mode: str
    up: int
    down: int


class FeedbackProviderCount(BaseModel):
    provider: str
    up: int
    down: int


class FeedbackDailyCount(BaseModel):
    date: str
    up: int
    down: int


class AdminFeedbackSummaryResponse(BaseModel):
    """評価の集計（GET /admin/feedback/summary）。"""
    rated: int
    up: int
    down: int
    truncated: bool = False
    max_rows: int = 0
    tags: list[FeedbackTagCount]
    by_mode: list[FeedbackModeCount]
    by_provider: list[FeedbackProviderCount]
    daily: list[FeedbackDailyCount]


class FeedbackItemUser(BaseModel):
    uid: str
    display_name: str | None


class FeedbackItem(BaseModel):
    id: int
    created_at: WireDateTime
    user: FeedbackItemUser
    rating: Literal["up", "down"]
    tags: list[str]
    comment: str | None
    question_head: str | None
    mode: str | None
    provider: str | None
    completion: str | None
    duration_ms: int | None


class AdminFeedbackItemsResponse(BaseModel):
    """評価の一覧（GET /admin/feedback/items）。"""
    items: list[FeedbackItem]
    next_before: int | None
