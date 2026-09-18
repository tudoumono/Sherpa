"""API 応答スキーマ集約。

`tests/e2e/mock_api.py` の `MOCKED` レジストリ（`len(mock_api.MOCKED)` 件・実測は
`tests/api/test_response_schemas.py::test_mocked_registry_route_count_matches_docstring_claim`
で固定）を対象に、実 TestClient 応答から「実測で」書き起こした pydantic 応答モデル。
ここに定義したモデルは2通りに使われる:

  1. **TypeAdapter 契約テスト**（`tests/api/test_response_schemas.py`）: `MOCKED` の実応答を
     ここのモデルで validate する（response_model は付与していないので挙動リスクはゼロ）。
  2. **response_model 付与**（各 `sherpa/routers/*.py`）: この中の一部（キー集合が常に固定＝
     条件分岐で増減しない）だけに実際に付与する。

**設計方針**:
  - 応答内容は変更しない（挙動不変が絶対条件）。DB 由来の値をハンドラが `str(...)` で明示的に
    文字列化している箇所（例: `workspace_file_list` の `created_at`）は、モデル側も `str` 型にする
    （`datetime` 型にすると pydantic が再シリアライズし、既存の `str(datetime)` 形式（空白区切り）
    と食い違う＝挙動変更になるため）。ハンドラが生の datetime を返す箇所は下記 `WireDateTime` 型を使う
    （生の `datetime` 型は使わない）。
  - **datetime 型は必ず `WireDateTime`（下で定義）を使う。生の `datetime` を直接フィールド型にしない**:
    pydantic v2 は response_model 経由の aware datetime を
    既定で `...Z`（Z サフィックス）に正規化するが、response_model 非付与時（FastAPI 既定の
    jsonable_encoder）は `dt.isoformat()`（`+00:00` オフセット表記）を返す＝response_model の
    有無だけで同じ値のワイヤー表現が変わってしまう（実測で確認済み）。`WireDateTime` は
    `PlainSerializer` で明示的に `.isoformat()` を使うため、response_model 付与の有無に関わらず
    元の表現に一致する。
  - キー集合が分岐で変わる応答のうち、分岐が `Literal` で判別可能な有限個（かつ各分岐内は完全に
    固定形）のものは `Union[...]`（pydantic の smart-mode union）で表現し response_model を付与する
    （実測: FastAPI は各分岐に応じて正しい方だけを直列化し、他分岐のキーを混ぜない）。
    例: `POST /worlds`（`action` で2分岐）・`POST /settings/bedrock-models/verify`（`ok` で2分岐）。
  - キー集合がネストした list 要素の中で個別に増減する応答（例: `GET /admin/es/search` の
    `hits[].extraction_method` は値がある時だけキーが付く）は response_model を付与しない
    （TypeAdapter 契約のみ・キー欠落/型不一致による黙殺・500化を避ける）。
  - 深くネストした自由形式 JSON（監査ログの `detail`/`before_state`/`after_state`、抽出フラグ等）は
    `Any` / `dict[str, Any]` で緩く受ける（内容を検証しすぎて実データで 500 化しないため）。
  - フィールド名に `version` を含むモデル（`ConversationSummary`・`AdminSettingsView` の
    `libreoffice.version`）は response_model を付与しない: 付与すると OpenAPI スキーマに
    `version` プロパティが露出し、`tests/api/test_world_param_compat.py::
    test_openapi_surface_has_no_version_parameter`（退役した version/世代 概念を API surface に
    再宣言しない contract）を壊す。値そのもの（DB 列・ソフトウェアバージョン文字列）は退役概念とは
    無関係だが、このガードはフィールド名の文字列一致で判定するため機械的に従う。
"""
from __future__ import annotations

from datetime import datetime
from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, Field, PlainSerializer, StrictInt, StrictStr

# pydantic v2 は response_model 経由だと
# aware datetime を既定で `2026-07-01T09:10:00Z`（Z サフィックス）に正規化する。response_model
# 非付与時（FastAPI 既定の jsonable_encoder）は `dt.isoformat()`（`+00:00` オフセット表記）を返す
# ため、response_model を付与しただけで**同じ datetime 値のワイヤー表現が変わってしまう**
# （挙動不変の絶対条件に反する・実測で確認済み）。`PlainSerializer` で明示的に `.isoformat()` を
# 使う型を1つ定義し、datetime を持つ全フィールドをこれに統一する（response_model 付与の有無に
# 関わらず同じ表現になる・将来 response_model を付与するルートが増えても再発しない）。
WireDateTime = Annotated[datetime, PlainSerializer(lambda v: v.isoformat(), return_type=str)]


# ===================================================================================
# 認証（sherpa/routers/auth.py）
# ===================================================================================

class AuthMeResponse(BaseModel):
    """GET /auth/me（auth.py::auth_me）。"""
    uid: str
    email: str | None
    display_name: str | None
    role: str
    must_change_password: bool
    auth_disabled: bool


class AuthLoginResponse(BaseModel):
    """POST /auth/login（auth.py::auth_login）。"""
    ok: bool
    uid: str
    must_change_password: bool
    next: str | None


class OkResponse(BaseModel):
    """`{"ok": true}` のみを返すエンドポイント共通形（auth_logout 等）。"""
    ok: bool


# ===================================================================================
# システム（sherpa/routers/system.py・sherpa/routers/system_extras.py）
# ===================================================================================

class HealthSummaryResponse(BaseModel):
    """GET /health/summary（system_extras.py::health_summary・health.summary()）。"""
    status: str
    checked_at: str


class HealthComponent(BaseModel):
    """`GET /admin/health` の `components[]` 要素（health.py::_check_one）。`hint` は `ok=False` の
    時だけキーが付く（`ok=True` では欠落）ため `None` 既定にしているが、response_model は付与しない
    （TypeAdapter 契約のみ・欠落キーを常時 `null` 出力に変えてしまう挙動変化を避ける）。"""
    id: str
    label: str
    impact: str
    ok: bool
    detail: str | None
    latency_ms: int
    hint: str | None = None


class AdminHealthResponse(BaseModel):
    """GET /admin/health（system_extras.py::admin_health）。response_model 非付与（上記 `hint` の
    条件付きキーのため・TypeAdapter 契約のみ）。"""
    status: str
    checked_at: str
    ttl_seconds: float
    components: list[HealthComponent]


class BedrockModelChoice(BaseModel):
    """`GET /settings/bedrock-models` の `models[]` 要素（providers/bedrock.py::list_bedrock_inference_profiles）。"""
    id: str
    label: str


class BedrockModelsResponse(BaseModel):
    """GET /settings/bedrock-models（system.py::settings_bedrock_models）。"""
    models: list[BedrockModelChoice]
    error: str | None


class BedrockVerifyOk(BaseModel):
    """POST /settings/bedrock-models/verify・成功分岐（`ok` で判別可能・system.py::settings_bedrock_models_verify）。"""
    ok: Literal[True]
    id: str
    label: str


class BedrockVerifyErr(BaseModel):
    """POST /settings/bedrock-models/verify・失敗分岐（形式不正/連打/401403/ネットワーク、すべて同形）。"""
    ok: Literal[False]
    error: str


BedrockVerifyResponse = Union[BedrockVerifyOk, BedrockVerifyErr]


class ConfigResponse(BaseModel):
    """GET /config（system.py::config_get・providers/__init__.py::provider_info）。"""
    agent: str
    label: str
    model: str


class ConstructChoice(BaseModel):
    """GET・PUT /settings の `constructs_available[]` 要素（`sherpa/agent_constructs.py`）。

    `agent`/`codex_model_provider` は保存すべき設定値そのもの＝画面はこの2つをそのまま PUT する。
    """
    id: str
    agent: str
    codex_model_provider: str | None
    label: str
    hint: str


class ModelChoiceInfo(BaseModel):
    """GET・PUT /settings の `model_catalog[field]`（`sherpa/model_catalog.py::field_choice_info`）で
    使う共通の選択肢形。`allowed` が空でも `default` が非空のことがある（カタログ未編集＝組み込み
    既定のみの状態）。"""
    allowed: list[str]
    default: str


class OllamaUrlChoiceInfo(ModelChoiceInfo):
    """`ollama_url_choice`（`system.py::_ollama_url_choice`）専用の選択肢形。`allowed` は許可ポリシー
    （loopback／admin allowlist）を満たす完全 URL のみ。`legacy` は利用者の現在の保存値が許可されて
    いない場合だけ非 null（`allowed` には混ぜない＝選んでも 422 になる値を選択肢として出さない）。"""
    legacy: str | None


class SettingsResponse(BaseModel):
    """GET・PUT /settings（system.py::_public_settings。全キー常時存在）。

    モデル名・機能別プロバイダの個人設定（旧 openai_model/gemini_model/ollama_model/codex_model/
    extract_provider/graph_provider/intent_provider/embed_provider/intent_model）は個人設定に無い
    ＝応答にも含めない（管理者の使えるモデル一覧（`model_catalog`）・選択中のクラウドプロバイダ
    だけで決まる）。旧 `codex_reasoning`（Codex の推論深さ）も個人設定に無いが、使えるモデル一覧の
    対象外＝環境変数 `SHERPA_CODEX_REASONING` だけで決まる。`bedrock_model` は例外（実在確認済み
    モデルの専用機構のため個人設定に残す）。`search_helper_model` も例外（個人設定に入力欄・保存
    経路は無いが、以前この画面で選んだ値が DB に残っている利用者への注記表示のため読み取り専用
    で返す・web/settings.js 参照）。
    """
    agent: str
    web_search_available: bool
    codex_web_search: bool
    openai_key_set: bool
    # 接続先の種類
    # （openai/azure/custom）とホスト名のみ（キー・パスは出さない）。
    openai_endpoint_kind: str
    openai_base_url_host: str
    ollama_url: str | None
    gemini_key_set: bool
    bedrock_model: str
    bedrock_key_set: bool
    # legacy 表示と検証済み表示の区別用（system.py::_public_settings）。
    bedrock_model_known: bool
    bedrock_model_label: str
    # 4構成（`sherpa/agent_constructs.py`）: 標準MVPが見せる実行構成
    # （openai_only / ollama_only / codex_openai / codex_ollama）。`constructs_available` は
    # この環境で選べるものだけ（env `SHERPA_EXTRA_AGENTS` で追加AIを有効化したら増える）。
    codex_model_provider: str
    # 検索アシスタント（`sherpa/search_helper.py`）: 下調べ（worker）だけを安いモデルへ任せる
    # 利用者ごとの設定（''＝安いモデルを使わず頭脳自身が worker／'ollama'／'openai'）。
    # モデル名は管理者のカタログ既定に従う。
    search_helper: str
    # 旧・個人上書き時代のモデル指定（読み取り専用・注記表示のみ）。
    search_helper_model: str
    construct_id: str
    constructs_available: list[ConstructChoice]
    system_prompt: str
    # A7 の現在選択（openai/gemini/bedrock）と
    # A6 の個人キー許可フラグ（既定 false）。画面がキー欄の表示/非表示・注記を切り替えるための情報。
    cloud_provider: str
    personal_api_keys_allowed: bool
    # 利用者本人による外部連携 API キー自己発行の許可トグル（admin が管理画面で設定・既定
    # false）。画面が個人設定の「外部連携」欄を出し分けるための情報（`personal_api_keys_allowed`
    # と同型の出し分けパターン）。
    user_api_keys_allowed: bool
    # 自己発行キーの1日あたり呼び出し上限（既定/上限・管理者統制）。発行フォームのプレース
    # ホルダ表示用。
    user_api_keys_daily_quota_default: int
    # モデル名欄ごとの選択肢（`sherpa/model_catalog.py`。system.py::_public_settings が
    # `model_catalog.FIELD_CELLS` の全フィールドを常に含める）。個人設定にモデル名欄は無いが、
    # 管理画面の「使えるモデル」との配線を共有するため引き続き返す。
    model_catalog: dict[str, ModelChoiceInfo]
    # プロバイダ別選択肢（system.py::_model_choice_table_by_provider）。
    model_catalog_by_provider: dict[str, dict[str, ModelChoiceInfo]]
    # 個人の Ollama 接続先 `<select>` の選択肢（system.py::_ollama_url_choice。allowed は完全 URL）。
    ollama_url_choice: OllamaUrlChoiceInfo
    # チャット画面のクイック入力例（管理者設定・`sherpa/chat_examples.py::public_examples`）。
    # None＝未設定（フロントの組み込み既定を使う）。配列（空含む）＝管理者の明示設定（空＝非表示）。
    chat_examples: list[str] | None


class SettingsTestResponse(BaseModel):
    """POST /settings/test（system.py::settings_test。全分岐で同じ4キー）。"""
    ok: bool
    provider: str
    model: str
    detail: str | None


# ---- 管理者:全体設定（system_extras.py::_admin_settings_view）----

class CloudInfo(BaseModel):
    """クラウド AI プロバイダの中央設定（A6/A7）。"""
    provider: str
    # `provider` は既定 openai への読み替え込みの実効値
    # （常に非 null）。`provider_raw` は `cloud_provider` の生の保存値（未選択＝一度も PUT されて
    # いない場合は null）——UI はこれで「admin が実際に選んだか」を判別する（`provider` だけでは
    # 初期表示の既定 openai と明示選択した openai を区別できない）。
    provider_raw: str | None
    providers: list[str]
    personal_api_keys_allowed: bool
    openai_key_set: bool
    gemini_key_set: bool
    bedrock_key_set: bool
    ollama_url: str
    personal_keys_in_use_count: int
    # WEB-1: Codex の Web 検索を管理者が許可しているか（既定 false）。
    web_search_allowed: bool


class ExtKeysDailyQuotaDefaultInfo(BaseModel):
    """自己発行キーの1日あたり呼び出し上限の既定/上限（`configured`=管理者の生値・
    `effective`=未設定時のフォールバック込みで実際に適用される値・`default`=組み込みの
    フォールバック値そのもの＝差分強調の基準）。"""
    configured: int | None
    effective: int
    default: int


class ExtKeysResearchProviderInfo(BaseModel):
    """AI 下調べ検索（POST /ext/v1/research）の既定 AI（PART-4a・`configured`=管理者の生値・
    `effective`=未設定時のフォールバック込みで実際に適用される値・`default`=組み込みの
    フォールバック値そのもの＝差分強調の基準）。"""
    configured: str | None
    effective: str
    default: str


class ExtKeysAdminInfo(BaseModel):
    """外部連携 API キー: 利用者本人による自己発行の許可トグル
    （system_extras.py::_admin_settings_view）。`self_issued_active_count` は OFF で保存する前の
    確認ダイアログ用プレビュー（失効済み/期限切れは含まない・`CloudInfo.personal_keys_in_use_count`
    と同型）。"""
    user_api_keys_allowed: bool
    self_issued_active_count: int
    daily_quota_default: ExtKeysDailyQuotaDefaultInfo
    research_default_provider: ExtKeysResearchProviderInfo


# ---- 外部連携 API キー管理（system_extras.py::_key_created_out/_key_list_out・
# POST/GET/DELETE /ext/v1/admin/keys・POST/GET/DELETE /ext/v1/keys で共通の形）----

class ExtKeyCreatedResponse(BaseModel):
    """発行直後のレスポンス（プレーンキーを含む・このレスポンスでのみ返す）。admin 発行・
    利用者自己発行のどちらも同じ形（`system_extras.py::_key_created_out`）。

    `webhook_secret`（PART-6）も**このレスポンスでのみ**平文で返す（`webhook_url` を指定した
    ときだけ非 null・以後は一覧にも二度と出さない）。"""
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
    """キー一覧の1行（プレーンキーは含めない）。`webhook`（PART-6）は Webhook 登録の有無のみ・
    `webhook_host`（登録時のみ）は宛先の host:port（path/query・`webhook_secret` は含めない）。"""
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
    """曖昧な発行結果（POST 応答が届かなかった等）の回復専用エンドポイントの応答。
    `found=True` のときだけ実際に失効している（呼び出し側は found を見て文言を出し分ける）。"""
    found: bool
    id: int | None
    revoked_at: str | None
class OpenaiEndpointConfigured(BaseModel):
    """`openai_endpoint.configured`（admin が実際に保存した生値・未設定なら null）。"""
    kind: str | None
    base_url: str | None
    auth_header: str | None
    api_version: str | None


class OpenaiEndpointEffective(BaseModel):
    """`openai_endpoint.effective`（`sherpa/llm.py` による解決結果）。"""
    kind: str
    base_url: str
    auth_header: str
    api_version: str


class OpenaiEndpointInfo(BaseModel):
    """SET-2c: OpenAI 互換 API の接続先（本家／Azure OpenAI／その他 OpenAI 互換）。"""
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
    """Webhook 宛先の SSRF allowlist。
    `OllamaAllowlistInfo` と同形（`configured`=管理者の生値・`effective`=loopback を除いた
    実際に許可される非 loopback 接続先の host:port）。"""
    configured: list[str] | None
    effective: list[str]


class CodexSessionRetentionInfo(BaseModel):
    configured: int | None
    effective: int


class ChatExamplesAdminInfo(BaseModel):
    """チャット画面のクイック入力例（`sherpa/chat_examples.py`）。`configured` は保存されている
    生値（未設定なら None・破損値も含め透過表示するため `Any`＝他の *AdminInfo と同じ理由）。
    `effective` は実際に表示される内容（非表示なら空リスト）、`default` は組み込み既定4例。"""
    configured: dict[str, Any] | None
    effective: list[str]
    default: list[str]
    max_items: int
    max_item_length: int


class UsageChatAdminInfo(BaseModel):
    """STAT-2: 利用統計チャット専用の AI 選択（`usage_chat_provider`・"openai"|"ollama"・
    利用者の実行構成には依存しない・管理者全体で統一）。`configured` は保存されている生値
    （未設定なら `None`）——DB への直接書き込み等の破損値（"openai"/"ollama"以外の文字列に
    限らず、非文字列型も含む）をそのまま透過して表示する契約のため `Any` にする
    （`str | None` だと非文字列の破損値で応答全体の pydantic 検証が失敗し、
    `GET /admin/settings` 自体が 500 化してしまう＝モジュール docstring の
    「深くネストした自由形式 JSON」節と同じ理由）。"""
    configured: Any
    effective: str
    default: str
    providers: list[str]


class ModelCatalogAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `model_catalog`（`sherpa/model_catalog.py`・
    system_extras.py::_admin_settings_view）。`configured` は管理者が実際に保存した生値（未設定なら
    null）、`effective` は組み込み既定に管理者設定を重ねた解決結果（プロバイダ→用途→セル）。"""
    configured: dict[str, dict[str, ModelChoiceInfo]] | None
    effective: dict[str, dict[str, ModelChoiceInfo]]
    # 組み込み既定のみ（管理者設定を一切重ねない解決結果）。管理画面が「セルの値が既定と異なるか」を
    # 判定するための基準値（`effective` はセル単位で管理者設定込みのため差分判定の基準にできない）。
    builtin: dict[str, dict[str, ModelChoiceInfo]]
    providers: list[str]
    usages: list[str]


class DepthProfileBaseInfo(BaseModel):
    """SC-6c（調べる深さ・調べ方ブロック §3.2）の整数系基準値1項目（標準時の値）。
    `configured` は管理者が実際に保存した生値（未設定なら `None`）、`effective` は
    `system_settings → env → コード既定` の解決結果、`default` は env/コード既定
    （未設定に戻したときの実効値）。"""
    configured: int | None
    effective: int
    default: int


class DepthProfileCodexReasoningInfo(BaseModel):
    """SC-6c: Codex 推論レベルの基準値（`sherpa.depth_profile.CODEX_REASONING_LEVELS` の1つ）。"""
    configured: str | None
    effective: str
    default: str
    options: list[str]


class CodexWorkerModelInfo(BaseModel):
    """multi_agent（`[agents.worker]`）の worker モデル（実装ベース探索の回復 S1・案 B）。
    `configured` は管理者が保存した生値（未設定なら `None`）、`effective` は
    `sherpa.providers.codex.sandbox._codex_worker_model` の解決結果、`default` は
    フォールバック定数（`_CODEX_WORKER_MODEL_FALLBACK`）。"""
    configured: str | None
    effective: str
    default: str


class DepthProfileAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `depth_profile`（SC-6c・`sherpa/depth_profile.py`・
    system_extras.py::_admin_settings_view）。調べる深さ（標準/深く/最大）が掛ける倍率の
    **基準値**（標準時の値）のみを持つ——倍率表自体（§3.2）は固定で編集対象外。"""
    max_turns: DepthProfileBaseInfo
    grep_max_hits: DepthProfileBaseInfo
    qa_max_hits: DepthProfileBaseInfo
    read_window: DepthProfileBaseInfo
    impact_depth: DepthProfileBaseInfo
    troubleshoot_depth: DepthProfileBaseInfo
    codex_reasoning: DepthProfileCodexReasoningInfo


class ChatMaxTurnsAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `chat_max_turns`（同時実行の上限・`sherpa/chat_turns.py::
    effective_limits`・system_extras.py::_admin_settings_view）。`per_user`/`global` は
    `DepthProfileBaseInfo` と同型（configured=管理者の生値・effective=system_settings→env の
    解決結果・default=env 既定＝未設定に戻したときの実効値）。`global` は Python の予約語のため
    属性名は `global_`（`Field(alias="global")`）——JSON 上のキーは `chat_max_turns.global` のまま。"""
    per_user: DepthProfileBaseInfo
    global_: DepthProfileBaseInfo = Field(alias="global")


class ModelWindowResolutionInfo(BaseModel):
    """`agentic_budget.window`（BUDGET-2・§3.4・`sherpa/model_windows.py::resolve_window_tokens`・
    system_extras.py::_current_chat_provider_model）。現在のモデル（システム既定のチャット/
    エージェント頭脳）の窓の4段解決結果。`provider`/`model` は解決できないときは空文字。
    `window_tokens`/`derived_cap_bytes` は `source="unknown"`（登録値/API/シードのどれにも
    無い）のときのみ null——このとき管理画面は「窓が未登録です」の平文案内＋登録欄を出す。"""
    provider: str
    model: str
    window_tokens: int | None
    source: str
    derived_cap_bytes: int | None


class ModelWindowsRegisteredInfo(BaseModel):
    """`agentic_budget.model_windows`（BUDGET-2・§3.4）。モデル名→窓 tokens の管理者登録表
    （"provider:model" キー・追加/上書き/削除は PUT `model_context_windows`）。`configured` は
    生値そのもの（未設定なら null）。"""
    configured: dict[str, int] | None


class AgenticBudgetAdminInfo(BaseModel):
    """GET・PUT /admin/settings の `agentic_budget`（`sherpa/agentic_search.py::
    resolve_tool_result_budgets`・system_extras.py::_admin_settings_view）。agentic search の
    tool-result バイト予算（1件あたり／1 run 累計）——`DepthProfileBaseInfo` と同型
    （configured=管理者の生値・effective=system_settings→env→コード既定→窓由来上限との
    min() の解決結果・default=env/コード既定＝未設定に戻したときの実効値・窓連動を含まない）。
    `window`/`model_windows` は窓のヒント表示＋管理者登録表。"""
    per_result: DepthProfileBaseInfo
    total: DepthProfileBaseInfo
    window: ModelWindowResolutionInfo
    model_windows: ModelWindowsRegisteredInfo


class AdminSettingsView(BaseModel):
    """GET・PUT /admin/settings（system_extras.py::_admin_settings_view。全キー常時存在）。

    response_model は付与しない: `legacy_backend.libreoffice.version`（soffice 検出バージョン
    文字列）が OpenAPI スキーマに `version` プロパティとして露出し、
    `test_world_param_compat.py::test_openapi_surface_has_no_version_parameter`（退役した
    version/世代 概念を API surface に再宣言しない contract）に抵触するため。TypeAdapter 契約のみ。
    """
    cloud: CloudInfo
    ext_keys: ExtKeysAdminInfo
    openai_endpoint: OpenaiEndpointInfo
    model_catalog: ModelCatalogAdminInfo
    arms: ArmsInfo
    legacy_backend: LegacyBackendInfo
    rag_llm_render: RagLlmRenderInfo
    vlm: VlmInfo
    ollama_allowlist: OllamaAllowlistInfo
    webhook_allowlist: WebhookAllowlistInfo
    codex_session_retention_days: CodexSessionRetentionInfo
    usage_chat: UsageChatAdminInfo
    depth_profile: DepthProfileAdminInfo
    chat_max_turns: ChatMaxTurnsAdminInfo
    agentic_budget: AgenticBudgetAdminInfo
    # API 経路の 1 応答あたりのツール実行上限（`agentic_search.effective_max_tools_per_turn`）。
    # `DepthProfileBaseInfo` と同型（configured=管理者の生値・effective=解決結果・default=コード既定）。
    agentic_tool_limit: DepthProfileBaseInfo
    # 埋め込み HTTP の同時送信数（`embeddings.effective_embed_parallel`）。
    # `DepthProfileBaseInfo` と同型。env フォールバックは持たない（default=EMBED_PARALLEL_DEFAULT）。
    embed_parallel: DepthProfileBaseInfo
    # 「最大」の深さが許す査読の巡数（`depth_profile.effective_max_review_rounds`）。
    # `DepthProfileBaseInfo` と同型。env フォールバックは持たない（default=MAX_REVIEW_ROUNDS_DEFAULT）。
    max_review_rounds: DepthProfileBaseInfo
    # multi_agent（S6）の worker モデル（`sandbox._codex_worker_model`）。env フォールバックは
    # 持たない（default=`_CODEX_WORKER_MODEL_FALLBACK`）。
    codex_worker_model: CodexWorkerModelInfo
    chat_examples: ChatExamplesAdminInfo


# ---- 運営掲示板（system_extras.py::_announcement_out）----

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
    """GET /announcements。"""
    announcements: list[AnnouncementOut]


class AnnouncementMutateResponse(BaseModel):
    """POST /admin/announcements・PATCH /admin/announcements/{id}。"""
    ok: bool
    announcement: AnnouncementOut


# ===================================================================================
# 管理者:ユーザー管理（sherpa/routers/admin_users.py）
# ===================================================================================

class UserRow(BaseModel):
    """`GET /admin/users`（store.list_users）の1行。"""
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
    """`store.create_user` の RETURNING 列（`last_login_at` を持たない・UserRow と別形）。"""
    uid: str
    email: str | None
    display_name: str | None
    role: str
    status: str
    must_change_password: bool


class AdminUserCreateResponse(BaseModel):
    ok: bool
    user: UserCreateRow


class AdminUserPatchResponse(BaseModel):
    ok: bool
    uid: str


# ===================================================================================
# 管理者:監査ログ（sherpa/routers/audit_usage.py）
# ===================================================================================

class AuditRow(BaseModel):
    """`GET /admin/audit`（store.list_audit）の1行。列は固定 SELECT・値は自由形式 JSON を含む。"""
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


# ---- 管理者:利用統計（audit_usage.py::admin_usage_stats・store/usage.py::usage_stats）----
# 全キーが常時存在（分岐で増減しない・末尾の return 文が単一の固定 dict リテラル）ため response_model を付与する。

class UsageLens(BaseModel):
    impact: int
    qa: int
    troubleshoot: int
    chat: int


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
    # 実際に使った半開区間 `[from, to)`（ISO 8601・オフセット付き）。`days` 指定でも `from`/`to`
    # 指定でも入る（`start`/`end` は従来どおり JST 暦日で `end` は**含む**終了日＝画面の日別
    # チャートのゼロ埋め範囲。意味を変えないためこちらを別フィールドとして足す）。UsagePeriod を
    # 返す他の経路（期間指定を持たない集計）では未設定のことがあるため任意。
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
    """`sherpa/stop_kind.py` の閉じた8値
    （`completed`/`stopped_by_user`/`budget`/`no_evidence`/`transport_error`/`timeout`/
    `codex_silent`/`codex_partial`）またはそのいずれにも該当しない過去データ/未計測経路の
    `unknown` のいずれか。"""
    stop_kind: str
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
    output: int
    reasoning_output: int


class UsageTokenByUser(BaseModel):
    uid: str
    display_name: str
    turns: int
    input: int
    cached_input: int
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
    output: int
    reasoning_output: int


class UsageTokenByKind(BaseModel):
    """用途別（kind）内訳。

    chat 行は messages.answer->'usage' 由来（トークン列は常に int）。それ以外の kind（`metering.KINDS`
    参照・intent/embed/graph_ask/vlm 等）は usage_events 由来で、プロバイダが usage を報告しなかった
    行はトークン列が None（「報告不能」マーカー・0 に丸めない）。

    `elapsed_ms_total`/`elapsed_ms_avg`/`elapsed_n`:
    usage_events.elapsed_ms（計測スコープの無い呼び出しは NULL）の合計・平均（NULL 行を除く）・
    計測ありの行数。chat 行は対象外（別契約）＝常に total/avg=None・n=0。
    """
    kind: str
    provider: str
    model: str
    calls: int
    input: int | None
    cached_input: int | None
    output: int | None
    reasoning_output: int | None
    elapsed_ms_total: int | None
    elapsed_ms_avg: float | None
    elapsed_n: int


class UsageTokenByUserKind(BaseModel):
    """ユーザー別 × 用途別（kind）内訳。`UsageTokenByKind` の内訳をユーザーごとに分けたもの。
    利用者に紐付かない呼び出し（取り込み時の埋め込み・画像読み取り・rag_render 等）は含まれないため、
    同一 kind の合計は対応する `UsageTokenByKind` 行以下になりうる。null の意味・chat 行の
    elapsed 扱いは `UsageTokenByKind` と同じ。"""
    uid: str
    display_name: str
    kind: str
    calls: int
    input: int | None
    cached_input: int | None
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
    """会話あたりの user ターン数分布。

    値は期間内の user ターン数（`turn_created_at` が期間内）を会話ごとに数えたもの（対象は期間内に
    user ターンが 1 件以上ある会話・会話の全履歴ではない）。対象会話が無ければ全て None。
    """
    avg: float | None
    median: float | None
    max: int | None
    p90: float | None


class UsageResponseTimeRow(BaseModel):
    """回答時間（ミリ秒・`messages.answer->>'duration_ms'`）の分布統計（1グループ分）。

    `provider` は経路別行（`response_time.by_provider`）でのみ設定され、全体行
    （`response_time.overall`）では常に `None`。対象0件なら `avg`/`median`/`max`/`p90` は
    `None`（`n`=0）——`_compute_response_time_stats` 参照。
    """
    provider: str | None
    avg: float | None
    median: float | None
    max: int | None
    p90: float | None
    n: int


class UsageResponseTime(BaseModel):
    """回答時間の分布：全体（`overall`）と経路（`provider`）別（`by_provider`）。

    対象は期間内の assistant 行のうち `duration_ms` が保存されている行のみ（利用者の明示停止・
    実行中のターンは assistant 自体を保存しないため対象外）。確認カード（`lens='clarify'`＝回答前の
    一時停止）も含めない。
    """
    overall: UsageResponseTimeRow
    by_provider: list[UsageResponseTimeRow]


class UsageConversationKindRow(BaseModel):
    """会話ごとの用途別（kind）内訳（`conversations_top[].kinds` の1行）。null の意味・chat 行の
    elapsed 扱いは `UsageTokenByKind` と同じ。"""
    kind: str
    calls: int
    input: int | None
    cached_input: int | None
    output: int | None
    reasoning_output: int | None
    elapsed_ms_total: int | None
    elapsed_ms_avg: float | None
    elapsed_n: int


class UsageConversationRow(BaseModel):
    """会話ごとの補助 AI 使用量（`docs/proposals/2026-09-12-利用統計の拡充2.md` §2 (b)）。

    トークン合計（`kinds` 内の input+output の合算）の降順で上位20件のみ（`usage_stats` 側で
    切り詰め済み）。`user_turns` は期間内の user ターン数（`conversation_turns` と同じ母集団）。
    `response_time_avg_ms` は `duration_ms` が保存された assistant 行のみの平均（0件なら None）。
    タイトル・本文は含まない。
    """
    conversation_id: int
    uid: str
    display_name: str
    world: str | None
    user_turns: int
    kinds: list[UsageConversationKindRow]
    response_time_avg_ms: float | None


class UsageLimitsByProviderRow(BaseModel):
    """経路（provider）別の「打ち切りの内訳」（`InvestigationState.limits`・制限そのものは
    変えない計測専用）。

    `turns` はこの provider の対象ターン総数（分母）。`*_turns` は回数系キーが1回以上／bool系
    キーが真だったターン数、`*_total` は回数系キーの合計回数（bool系には無い）。旧行
    （`answer.limits` キー自体が無い）は全項目0として母数（`turns`）にだけ数える。
    """
    provider: str
    turns: int
    tool_result_clipped_turns: int
    tool_result_clipped_total: int
    total_budget_hit_turns: int
    context_compactions_turns: int
    context_compactions_total: int
    synthesis_truncated_turns: int
    # 必要な根拠種別が揃わず深さを1段だけ自動で引き上げたターン数（`providers/base.py` の
    # 巡ループが `limits.depth_escalated` を立てる）。
    depth_escalated_turns: int
    search_truncated_turns: int
    search_truncated_total: int
    auto_continues_turns: int
    auto_continues_total: int
    # 縮退（バックエンド不調）の計数——意味論は「このターンで初めて検出されたか」＝初回検出の計数。
    backend_unavailable_fulltext_turns: int
    backend_unavailable_graph_turns: int
    graph_reingest_required_turns: int


class UsageLimits(BaseModel):
    """内部制限の打ち切り分布（`usage_stats()["limits"]`）。"""
    by_provider: list[UsageLimitsByProviderRow]


class UsageRoundDepthProviderRow(BaseModel):
    """深さ×経路別の巡別記録（`chat-round`・表示専用）の活動量（`usage_stats()
    ["rounds"]["by_depth_provider"]`）。課金の正本ではない（`tokens.*` と二重に数えない）。

    `limits`: 巡内 limits 増分の合算（数値系は合計・当たったか系は真になった巡数）。
    `verdicts`/`stops`: evaluator の判定・巡を止めた理由の分類別件数（固定語彙のラベルのみ・
    本文は含まない）。`missing_codes`: 不足軸の閉じた分類別件数（自由文の `missing` は含まない）。"""
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
    """深さ×経路×**巡番号**別の巡別記録の活動量（`usage_stats()["rounds"]["by_round"]`）。
    `UsageRoundDepthProviderRow` と同じ集計を巡番号（`meta.round`）単位で分けたもの——
    「2巡目は1巡目より効くか」の判断に使う。`round_no` が取れない行は `None` に畳み込む。"""
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
    """深さ×経路×到達巡数ごとのターン件数（`usage_stats()["rounds"]["round_distribution"]`）。
    `rounds_reached` はそのターンで実際に到達した巡番号の最大値。"""
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
    """理由コード分布の2軸: `final`＝最終回答の主張（`answer.data.claims`）、
    `rounds`＝巡別記録（`chat-round`）の主張内訳を巡単位で合算したもの。"""
    final: list[UsageReasonCodeRow]
    rounds: list[UsageReasonCodeRow]


class UsageRounds(BaseModel):
    """巡別記録（`usage_stats()["rounds"]`）。本文は含まない。"""
    by_depth_provider: list[UsageRoundDepthProviderRow]
    by_round: list[UsageRoundByRoundRow]
    round_distribution: list[UsageRoundDistributionRow]
    unmatched_rounds: int
    reason_codes: UsageRoundReasonCodes


class UsageQualityRunRow(BaseModel):
    """品質採点の条件×巡数別集計（`usage_stats()["quality_runs"]["by_rounds"]`）。
    採点そのものの本文（質問/回答）は保存しない——ここは件数と費用のみ。

    `condition`: 採点した条件（`store/usage.py::QUALITY_RUN_CONDITIONS` の閉集合）。巡数だけでは
    見直しを回さない条件同士（main と depth2-standard・どちらも `rounds=0`）が混ざるため、
    集計はこの条件別に分ける。"""
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
    """POST /admin/usage/quality-runs 入力（品質採点の入口）。

    採点そのものの本文（質問/回答）は受け付けない——フィールド自体が無い。件数は0以上。
    `rounds` も0以上（見直しを一度も回さない条件＝`main`/`depth2-standard` を登録できる）。
    `condition` は閉集合（自由文にしない＝表記ゆれで集計が割れるのを防ぐ）。
    `executed_from`/`executed_to` は質問セットを実行した期間（ISO 8601・オフセット必須・
    半開区間 `[from, to)`）——集計はこの実行期間で照会するため、採点を後日登録しても元の期間で
    読める。
    `cost_usd` は任意（費用が取れなければ省略）——**型注釈自体には** `allow_inf_nan=False`/`ge=0` を
    付けない: pydantic の制約違反として弾くと、FastAPI の既定 422 ハンドラが違反した生の値
    （`inf`/`nan`）をエラー本文の `input` にそのまま埋め込もうとし、`JSONResponse`（既定
    `allow_nan=False`）のレンダリングで `ValueError` を送出して**500 に化ける**。有限・非負の判定は
    `routers/audit_usage.py::admin_usage_quality_run_create` がハンドラ内で行い、生の値を
    埋め込まない定型メッセージで 422 を返す（`store.record_depth_quality_run` 側にも同じ判定の
    二重防御がある）。
    `run_id`（任意）は呼び出し側指定の冪等キー——同じ値で再送しても2行目を作らない
    （`store.record_depth_quality_run` の一意制約・監査書込み失敗後のリトライでの二重計上対策）。"""
    rounds: StrictInt = Field(ge=0)
    condition: Literal["main", "depth2-standard", "depth2-deep", "depth2-max"]
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
    # `run_id` 重複で `record_depth_quality_run` が新規行を作らなかった（ON CONFLICT DO NOTHING）
    # ときは False（`run_id` の値自体はレスポンスに出さない）。
    inserted: bool = True


class AdminUsageStatsResponse(BaseModel):
    """GET /admin/usage/stats（store/usage.py::usage_stats）。"""
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
    response_time: UsageResponseTime
    conversations_top: list[UsageConversationRow]
    limits: UsageLimits
    rounds: UsageRounds
    quality_runs: UsageQualityRuns


class UsageChatToolCall(BaseModel):
    """`UsageChatResponse.tool_calls` の1件。呼んだ調査ツールの名前と引数
    （数値と id のみ・本文/タイトル/鍵は一切含まない）。"""
    name: str
    args: dict = {}


class UsageChatResponse(BaseModel):
    """POST /admin/usage/chat（sherpa/usage_chat.py::answer_usage_question）。

    `provider_used`/`endpoint_kind`: 実際に使われた AI（"openai"|"ollama"）と、openai 使用時
    の接続先種別（"openai"|"azure"|"custom"・ollama 使用時は `None`）。画面は「今回だけ」トグルを使わない送信の送信先表示を、送信前の予定表示ではなく
    この確定値で更新する（GET /admin/settings とこの POST の間に他セッションが専用設定を
    変更した場合の食い違いを、応答時点の値で吸収する）。

    `notes`: 画面へそのまま見せる注記（改善ログの要約が取得できなかった場合の告知など・
    通常は空リスト）。

    `tool_calls`: 今回の質問応答で実際に呼んだ調査ツール（`usage_overview`等7つ・
    `agentic_search.usage_openai_tools()`）の名前と引数。渡された統計データだけで答えた場合は
    空リスト。"""
    answer: str
    provider_used: str
    endpoint_kind: str | None = None
    notes: list[str] = []
    tool_calls: list[UsageChatToolCall] = []


# ===================================================================================
# 個人ワークスペース（sherpa/routers/workspace.py）
# ===================================================================================

class WorkspaceFileRow(BaseModel):
    """`GET /workspace/files`。`created_at`/`expires_at` はハンドラが `str(...)` 済み＝ str 型のまま。"""
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


# ===================================================================================
# 資料フォルダ(World)管理（sherpa/routers/worlds.py）
# ===================================================================================

class PublicWorld(BaseModel):
    """`worlds.py::_public_world`。"""
    world_id: str
    label: str | None
    root_path: str | None
    storage_mode: str | None


class WorldsListResponse(BaseModel):
    """GET /worlds。"""
    worlds: list[PublicWorld]


class WorldOptionsResponse(BaseModel):
    """GET /world-options。"""
    worlds: list[str]
    labels: dict[str, str]


class FsEntry(BaseModel):
    name: str
    path: str


class FsListResponse(BaseModel):
    """GET /fs/list。"""
    path: str
    parent: str | None
    entries: list[FsEntry]


# ---- GET /ingest/preview（preview_service.py::build_preview）----
# response_model は付与しない: `documents[].provenance`（corpus_docs.provenance_summary が真値の
# 時だけ足す）・`documents[].importance`（`_重要度.txt` の解決結果がある時だけ足す・無ければ3キー
# とも省略）が条件付きキーのため（TypeAdapter 契約のみ・欠落キーを常時 `null` 出力に変える挙動変化を
# 避ける）。名寄せ（merges）・extraction_method は持たない（全ノード/エッジが常に static のため
# 意味を持たない）。

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
    entities_static: int
    relations: int
    relations_static: int
    deprecated: int
    hidden: int
    documents: int


class ImportanceDiagnostic(BaseModel):
    """`_重要度.txt` の構文診断1件（`ingest.importance.Diagnostic` の実測形）。"""
    config_path: str
    line: int | None
    column: int
    code: str
    message: str


class IngestPreviewResponse(BaseModel):
    """GET /ingest/preview。response_model 非付与（上記 documents/merges の条件付きキーのため）。"""
    world: str
    label: str | None
    counts: IngestPreviewCounts
    documents: list[IngestPreviewDocument]
    issues: list[Any]
    entities: list[IngestPreviewEntity]
    relations: list[IngestPreviewRelation]
    importance_diagnostics: list[ImportanceDiagnostic]


class IngestBlockedDoc(BaseModel):
    """`_ingest_summary` の `last_run_blocked` 要素（対象ファイルが特定できる blocked flag の doc/reason）。"""
    doc: str
    reason: str


class RunProgress(BaseModel):
    """実行中 run の逐次進捗（ING-3・`ingest_runs.progress`）。内部段キー（`stage`）に対応する
    利用者向け平文は `stage_label`（サーバ定数・`sherpa.ingest.worker.STAGE_LABELS`）。
    `done`/`total`＝ファイル単位の進捗（段によっては件数を持たず両方 `None`）。"""
    stage: str
    stage_label: str
    done: int | None
    total: int | None
    updated_at: str


class IngestSummaryFields(BaseModel):
    """`worlds.py::_ingest_summary`（`corpus_docs.scan_report` のキャッシュ ＋ graph/ES 件数・最終実行状態）。

    `scanned`〜`unreadable`（ING-2）は `worlds.last_scan_report` のキャッシュ（sync 完走時／
    `POST /worlds/{wid}/recount` の時だけ更新・**このエンドポイント自身はフォルダを歩かない**）。
    `counts_as_of`＝そのキャッシュの記録時刻（`None`＝未集計・再集計を促す）。
    `failed_files`／`partial_extraction_suspected`／`stage_summary`（ING-1）は最新 run の
    `extraction_snapshot` 由来（run が無い/該当データが無ければ `None`）。
    `failure_reason_catalog`／`partial_extraction_advice` は閉じた理由語彙の平文辞書（常時同一・
    `sherpa.ingest.failure_reasons` が単一の真実源）。
    `last_run_warnings`/`last_run_blocked` は元の flags を打ち切って導出したもの——
    `last_run_flags_total`＝打切り前の flags 総数・`last_run_flags_truncated`＝打切りが発生したか。
    `last_run_id`／`running_progress`（ING-3）＝最新 run の id と実行中進捗（実行中でなければ
    `running_progress` は `None`）。
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
    counts_as_of: str | None
    graph_nodes: int
    graph_edges: int
    es_chunks: int | None
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
    """GET /worlds/{wid}/status（`_ingest_summary` の全キーを**フラットに**展開・`worlds.py::world_status`）。
    `last_synced_at` はハンドラが `str(...)` 済み＝ str 型のまま。"""
    ok: bool
    world_id: str
    label: str | None
    root_path: str | None
    last_synced_at: str | None


class WorldDiffResponse(BaseModel):
    """POST /worlds/diff・GET /worlds/{wid}/diff（`worlds.py::_diff_payload` ＋ `ingest_worker.diff_dir`。
    全キー常時存在）。"""
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


class WorldIngestAcceptedResponse(BaseModel):
    """POST /worlds・POST /worlds/{wid}/refresh・DELETE /worlds/{wid}・
    POST /worlds/{wid}/rebind・POST /ingest/rerun（即受付契約・HTTP 202）。

    取り込み本体（登録/更新の再取り込み・削除の派生物wipe・参照先変更の
    破棄→再作成・強制フル再構築）は背景（`sherpa.ingest.background`）で継続する——この応答は
    「受け付けた」ことだけを示す。
    `run_id`＝`ingest_runs.id`（受付処理自身が O(1) の INSERT で確保してから
    背景実行へ渡すため、この応答の時点で**必ず**判明している＝非 null）。`joined=True`＝world
    単位の多重クリック制御（操作種別＋正規化 payload の一致）により新規実行はせず既存 run へ
    合流した（不一致なら 409）。進捗・結果は `GET /worlds/{wid}/status` の `last_run_*`／進捗中
    フィールドで確認する（削除成功後は world 行自体が消えるため同エンドポイントは 404 になる）。
    """
    ok: bool
    world_id: str
    run_id: int
    joined: bool
    note: str


class WorldRecountResponse(IngestSummaryFields):
    """POST /worlds/{wid}/recount（ING-2・`corpus_docs.scan_report` を明示的に再実行してキャッシュし直す・
    唯一の明示的な実走査・同期のまま＝2TB 走査でも軽量な metadata 集計のため背景化していない）。
    `worlds.py::world_recount` が `_ingest_summary` をフラットに展開して返す。"""
    ok: bool
    world_id: str


class WorldReconvertResponse(BaseModel):
    """POST /worlds/{wid}/reconvert（ING-1・1ファイルの旧形式変換キャッシュを落として world 全体を sync・
    同期のまま＝対象は1ファイルのみで背景化していない）。`summary` は `_ingest_summary` をネスト。"""
    ok: bool
    world_id: str
    rel: str
    changed: bool
    status: str
    ledger: int | None
    flags: list[Any]
    summary: IngestSummaryFields
    note: str


# ===================================================================================
# ナレッジグラフ（sherpa/routers/graph.py）
# ===================================================================================

class GraphNode(BaseModel):
    """GET /graph（preview_service.py::graph_view の nodes）。"""
    id: str
    name: str | None
    type: str | None
    type_ja: str | None
    status: str
    value: Any
    top_scope: str | None
    path: str | None


class GraphEdge(BaseModel):
    """GET /graph（preview_service.py::graph_view の edges）。"""
    source: str
    target: str
    type: str
    status: str


class GraphCounts(BaseModel):
    """`preview_service.py::_counts`（S3・K12＝全ノード/エッジが常に static のため llm/both は撤去済み）。"""
    entities: int
    entities_static: int
    relations: int
    relations_static: int
    deprecated: int
    hidden: int
    documents: int


class GraphResponse(BaseModel):
    """GET /graph（`graph.py::graph_get`。`signature` はルーターが ETag 用に pop 済み＝応答に含まれない）。"""
    world: str
    nodes: list[GraphNode]
    edges: list[GraphEdge]
    counts: GraphCounts
    total_nodes: int
    total_edges: int
    truncated: bool


class GraphFacetsResponse(BaseModel):
    """GET /graph/facets（graph_admin.py::facets）。"""
    node_labels: list[str]
    node_labels_ja: dict[str, str]
    relationship_types: list[str]
    condition_fields: list[str]


class GraphSearchNode(BaseModel):
    """GET /graph/search（graph_admin.py::_node。GraphNode と別形＝phase/category を持つ）。"""
    id: str
    name: str | None
    type: str | None
    type_ja: str | None
    em: str
    status: str
    value: Any
    top_scope: str | None
    phase: str | None
    category: str | None
    path: str | None


class GraphSearchCounts(BaseModel):
    """`graph_admin.py::_rows_to_graph` 既定の counts（`{"nodes":.., "edges":..}` ＝ GraphCounts と別形）。"""
    nodes: int
    edges: int


class GraphSearchResponse(BaseModel):
    """GET /graph/search（graph_admin.py::_rows_to_graph）。"""
    world: str
    nodes: list[GraphSearchNode]
    edges: list[GraphEdge]
    counts: GraphSearchCounts


# ---- POST /graph/ask（graph_admin.py::ask_graph・graph.py::_knowledge_status_summary）----
# 全分岐（llm_unavailable/failed/no_graph_evidence/ok）が同じキー集合を返す（`status` の値だけが違う）
# ため response_model を付与できる。

class KnowledgeIsolatedNode(BaseModel):
    name: str | None
    type: str | None
    path: str | None


class KnowledgeWeakDocument(BaseModel):
    name: str | None
    doctype: str | None
    category: str | None


class KnowledgeIngestError(BaseModel):
    id: Any
    status: str | None
    created_at: str


class KnowledgeStatusSummary(BaseModel):
    """graph.py::_knowledge_status_summary（`POST /graph/ask` の `summary` フィールド）。"""
    world: str
    scope_paths: list[str]
    documents: int
    graph_nodes: int
    graph_edges: int
    isolated_node_count: int
    isolated_nodes: list[KnowledgeIsolatedNode]
    weak_document_count: int
    weak_documents: list[KnowledgeWeakDocument]
    recent_ingest_errors: list[KnowledgeIngestError]


class GraphAskCitedNode(BaseModel):
    name: str | None
    label: str | None
    type_ja: str | None
    role: Any
    category: Any
    distance: Any
    path: list[Any]
    edges: list[Any]


class GraphAskResponse(BaseModel):
    """POST /graph/ask（graph.py::graph_ask・graph_admin.py::ask_graph）。"""
    status: str
    world: str
    question: str
    answer: str
    cited_nodes: list[GraphAskCitedNode]
    docs: list[str]
    summary: KnowledgeStatusSummary


# ===================================================================================
# 範囲（sherpa/routers/impact.py::scopes）
# ===================================================================================

class ScopeItem(BaseModel):
    path: str
    label: str
    depth: int
    count: int


class ScopesResponse(BaseModel):
    """GET /scopes（scope.py::scope_tree）。"""
    world: str
    label: str | None
    scopes: list[ScopeItem]


# ===================================================================================
# 会話管理・会話共有（sherpa/routers/conversations.py・sherpa/routers/shares.py）
# ===================================================================================

class ForkedFromInfo(BaseModel):
    """SH-1: フォーク元の出所表示（編集不可）。`name` は表示名未設定/退会等で `None` になりうる。"""
    share_id: int
    user_id: str
    name: str | None
    at: WireDateTime


class ConversationSearchMatch(BaseModel):
    """H1（履歴検索）: `GET /conversations?q=`（store.search_conversations）が行に付ける一致箇所。
    `where="title"` はタイトル一致、`"message"` は本文（messages.content／answer.headline）一致。"""
    where: Literal["title", "message"]
    snippet: str


class ConversationSummary(BaseModel):
    """`GET /conversations`（store.list_conversations／`q` 指定時は store.search_conversations）の1行。
    `match` は `q` 指定時のみ付く（省略時は常に欠落・従来どおり）。

    response_model は付与しない: `version`（DB 列・歴史的名称＝世代/世界 ID の実体・語彙統一の
    スコープ外＝DB 不変）が OpenAPI スキーマに `version` プロパティとして露出し、
    `test_world_param_compat.py::test_openapi_surface_has_no_version_parameter`（退役した
    version/世代 概念を API surface に再宣言しない contract）に抵触するため。TypeAdapter 契約のみ。
    """
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
    forked_from: ForkedFromInfo | None = None
    match: ConversationSearchMatch | None = None


class ConversationDetailConv(BaseModel):
    """`GET /conversations/{cid}` の `conversation`（store/shares.py::get_conversation_for_read。
    無効な受領共有分岐でも同じ列を返す＝形は不変）。"""
    id: int
    user_id: str
    version: str
    title: str | None
    codex_session_id: str | None
    origin: str
    source_conversation_id: int | None
    share_id: int | None
    shared_by_user_id: str | None
    read_only: bool
    contains_personal_workspace: bool
    forked_from_share_id: int | None
    forked_from_user_id: str | None
    forked_at: WireDateTime | None
    created_at: WireDateTime
    updated_at: WireDateTime


class ConversationMessage(BaseModel):
    """`messages[]` の1件。受領共有は `route`/`trace` を `None` に伏せるが、キー自体は消えない
    （`answer` はレンズごとに中身が異なる多相構造のため `Any`）。`feedback` は読者本人が投稿した
    フィードバック（{rating,tags,comment}・無ければ `None`。受領共有の閲覧者には常に `None`
    ＝所有者のフィードバックは漏らさない）。"""
    id: int
    role: str
    content: str
    lens: str | None
    route: Any
    trace: Any
    answer: Any
    feedback: Any = None
    created_at: WireDateTime


class ConversationDetailResponse(BaseModel):
    """GET /conversations/{cid}（store/shares.py::get_conversation_for_read）。response_model 非付与:
    `share_status` は無効/個人ブロックの受領共有の時だけ付くトップレベルの条件付きキーのため
    （TypeAdapter 契約のみ・欠落キーを常時 `null` 出力に変える挙動変化を避ける）。"""
    conversation: ConversationDetailConv
    messages: list[ConversationMessage]
    share_status: str | None = None


class UserSuggestItem(BaseModel):
    uid: str
    display_name: str | None


class UsersSuggestResponse(BaseModel):
    """GET /users/suggest。"""
    users: list[UserSuggestItem]


class ShareCreateResponse(BaseModel):
    """POST /conversations/{cid}/shares（shares.py::conversation_share_create）。"""
    ok: bool
    share_id: int
    url: str
    note: str


class ConversationForkResponse(BaseModel):
    """POST /conversations/{wid}/fork（SH-1・shares.py::conversation_fork）。"""
    ok: bool
    conversation_id: int


class ConversationShareRefreshResponse(BaseModel):
    """POST /conversation-shares/{share_id}/refresh（SH-2・shares.py::conversation_share_refresh）。"""
    ok: bool
    share_id: int
    refreshed_at: WireDateTime


class ShareInviteeItem(BaseModel):
    uid: str
    name: str | None
    accepted_at: WireDateTime | None


class ShareListItem(BaseModel):
    """`GET /conversations/{cid}/shares`（SH-2・store/shares.py::list_shares_for_conversation）の1件。"""
    share_id: int
    sanitized: bool
    created_at: WireDateTime
    expires_at: WireDateTime | None
    revoked_at: WireDateTime | None
    refreshed_at: WireDateTime | None
    last_used_at: WireDateTime | None
    invitees: list[ShareInviteeItem]


# ===================================================================================
# チャット（sherpa/routers/chat.py）
# ===================================================================================

class ChatTurnStartResponse(BaseModel):
    """POST /chat/turns（chat.py::chat_turns_start）。"""
    turn_id: str
    conversation_id: int


class ChatTurnStopResponse(BaseModel):
    """POST /chat/turns/{turn_id}/stop。"""
    ok: bool


class ChatTurnRunning(BaseModel):
    """`GET /chat/turns/running` の1要素。`started_at` はハンドラが `.isoformat()` 済み＝ str 型のまま。"""
    turn_id: str
    conversation_id: int
    started_at: str
    uid: str | None = None   # `all=true`（管理者）のときだけ値が入る（それ以外は null）


class ChatTurnsRunningResponse(BaseModel):
    turns: list[ChatTurnRunning]


# ===================================================================================
# 文書（sherpa/routers/documents.py）
# ===================================================================================

class EsSearchHit(BaseModel):
    """`GET /admin/es/search` の hits[] 要素。`extraction_method`/`confidence`/`has_conflicts` は
    元コードが値がある時だけキーを足す（欠落時デフォルト無し）ため `Any` で緩衝し、response_model は
    付与しない（TypeAdapter 契約のみ）。"""
    doc_id: str
    line: Any
    snippet: str
    score: Any
    ext: Any
    extraction_method: Any = None
    confidence: Any = None
    has_conflicts: Any = None


class EsSearchResponse(BaseModel):
    """GET /admin/es/search（documents.py::_admin_es_search_endpoint）。response_model 非付与
    （hits[] のキーが条件付きのため・TypeAdapter 契約のみ）。"""
    world: str
    query: str
    scope_paths: list[str]
    hits: list[EsSearchHit]
