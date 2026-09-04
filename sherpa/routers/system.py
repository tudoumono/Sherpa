"""システム系エンドポイント（フェーズ3スライス1・純移動）。

`/healthz`・`/`（ルート）・`/config`・`/settings*`（ユーザー設定系）を api.py から抽出する。
ロジックは変更しない（コード移動のみ）。ルート表 golden の定義順を保つため、api.py 側は
この3つの router（`healthz_router` / `settings_router` / `root_router`）を、元のエンドポイント
位置にそれぞれ `app.include_router(...)` する（`sherpa.ext_api` と同じ分離パターン）。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import logging
import threading
import time

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import JSONResponse, RedirectResponse
from pydantic import BaseModel, StrictBool

from sherpa import agent_constructs, keys, llm, model_catalog, search_helper, store
# RV MED（N1・2026-07-16 Codex RV 3巡目再検証）: `_bedrock_key_fingerprint` は `sherpa/store/settings.py`
# へ移設した（`add_bedrock_verified_models` が同一トランザクション内で使う必要があるため）。ここでは
# store facade から re-export する（`sherpa.routers.system._bedrock_key_fingerprint`／
# `sherpa.api._bedrock_key_fingerprint` の既存参照・テスト互換を保つ・ロジックは無い純粋関数なので
# 他の「危険な継ぎ目」のような実行時解決は不要）。
from sherpa.store import _bedrock_key_fingerprint  # noqa: F401
from sherpa.agents import (
    AGENT_PROVIDERS,
    BEDROCK_MODEL_CHOICES,
    BEDROCK_MODEL_ID_RE,
    BedrockProvider,
    _bedrock_auth_available,
    _bedrock_profile_label,
    _BEDROCK_MODEL,
    _redact_bedrock_secret,
    _web_search_admin_allowed,
    list_bedrock_inference_profiles,
    provider_info,
)
from sherpa.deps import _current_user
from sherpa.schemas import (
    BedrockModelsResponse,
    BedrockVerifyResponse,
    ConfigResponse,
    SettingsResponse,
    SettingsTestResponse,
)

_log = logging.getLogger("sherpa")


# ===== /config・/settings*（設定） =====
# router に tags を持たせない: 各エンドポイントの `tags=["設定"]` と結合されて "設定,設定" に
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す。
settings_router = APIRouter()


class SettingsReq(BaseModel):
    """個人設定の PUT ボディ。プロバイダ/モデルの選択（`extract_provider`／`graph_provider`／
    `intent_provider`／`embed_provider`／`search_helper_model`／`intent_model`／`openai_model`／
    `gemini_model`／`ollama_model`／`codex_model`）は個人設定に無い＝管理者の使えるモデル一覧
    （`model_catalog`）・選択中のクラウドプロバイダだけで決まる。`codex_reasoning`（Codex の
    推論深さ）も個人設定に無いが、こちらは使えるモデル一覧の対象外＝環境変数
    `SHERPA_CODEX_REASONING`（既定 `low`）だけで決まる。これらの旧フィールド名は未知フィールド
    として黙って無視される（pydantic 既定の extra="ignore"）＝送っても 422 にはならず、
    保存もされない。`bedrock_model` は例外（実在確認済みモデルの専用機構のため個人設定に残す）。"""
    agent: str | None = None
    # 4構成（2026-08-15）: Codex CLI が接続するモデル提供元（openai / ollama）。Codex 構成のみ有効。
    codex_model_provider: str | None = None
    # RV LOW（2026-07-03）: web_search はネット到達可否を左右するフラグ＝"1"/"true" 等の緩い型強制を
    # 受理せず、JSON の真偽値のみ許可する（StrictBool）。既存 SettingsReq の他フィールドは文字列/
    # 未使用の bool のみで security-relevant な on/off フラグはこれが唯一＝他フィールドとの整合が
    # 問題になる箇所は無い（ChatReq.knowledge 等の UI トグルは非セキュリティ的で対象外・変更なし）。
    codex_web_search: StrictBool | None = None   # 管理者が許可した時だけ実際に効く（agents._web_search_disabled_value）
    openai_api_key: str | None = None
    ollama_url: str | None = None
    gemini_api_key: str | None = None
    bedrock_model: str | None = None
    bedrock_api_key: str | None = None
    # 検索アシスタント（2026-08-15・`sherpa/search_helper.py`）: 下調べだけを安いモデルへ任せる
    # 利用者ごとの設定。''＝使わない／'ollama'／'openai'。モデルは管理者のカタログ既定を使う。
    search_helper: str | None = None
    system_prompt: str | None = None


# Bedrock モデル選択の allowlist（単一の真実源は sherpa/agents.py の BEDROCK_MODEL_CHOICES）。
# 誤設定によるモデル指定ミスを防ぐため PUT /settings で検証する（2026-07 決定）。
# RV MED（2026-07-15）: 当初は静的選択肢 ∪ `BEDROCK_MODEL_ID_RE` の**形式一致のみ**で許可していたが、
# それだと `jp.anthropic.not-a-real-model-v999:999` のような形だけ正しい架空 ID が verify を経ずに
# 保存でき、チャット/グラフQA/グラフ抽出が Bedrock 4xx で全滅する実害があった（Codex RV 指摘）。
# 「保存できるのは実在確認済みIDだけ」に締める＝静的 choices ∪ そのユーザーが verify/列挙で実在
# 確認済みの ID（`store.add_bedrock_verified_models`）∪ 現在保存中の値（grandfather・no-op 再保存を
# 422 にしない）の membership 判定のみとし、正規表現単独では許可しない（形式チェックは
# `POST /settings/bedrock-models/verify` が probe 前のガードとして引き続き使う）。
_BEDROCK_MODEL_IDS = frozenset(model_id for model_id, _label in BEDROCK_MODEL_CHOICES)


def _bedrock_model_id_valid(model_id: str, verified: list, current: str | None) -> bool:
    """`model_id` が「静的 choices」「このユーザーが実在確認済み（verified）」「現在保存中の値
    （grandfather）」のいずれかに含まれるか（membership 判定のみ・形式一致だけでは真にならない）。"""
    return (model_id in _BEDROCK_MODEL_IDS
            or model_id in (verified or [])
            or (current is not None and model_id == current))


# 接続先（OpenAI／Azure OpenAI／その他 OpenAI 互換）を個人設定画面へ読み取り専用で表示するための
# 補助。接続先そのものの設定（管理画面）・判定は `sherpa/llm.py::openai_base_url`／
# `openai_endpoint_kind` が唯一の真実源（DB `system_settings`）。実行時例外（DB 不達等）は握って
# 安全側（openai・ホスト名なし＝画面には何も出ない）へ倒す。
_INVALID_SAVED_BASE_URL_LABEL = "(不正な保存値)"


def _openai_base_url_host(system_settings: dict | None = None) -> str:
    """`llm.openai_base_url()` からホスト名だけを取り出す。パス・クエリ・認証情報は含めない
    （設定画面に出すのはホスト名だけ・CLAUDE.md の「キーやパスは出さない」契約）。

    `system_settings`（省略可）は `llm.openai_base_url()` へそのまま渡す。`_public_settings` は
    1応答内で読んだ単一スナップショットを渡す（省略時に自分で読み直すと、`_openai_endpoint_kind`
    と別々の DB 読みになり、応答の途中で admin 更新が挟まった場合に kind と host が別世代の値の
    まま混在しうる）。

    表示前に `llm.assert_openai_base_url_allowed()` で再検証する: 空白・バックスラッシュ混入等を
    含む値は `urlsplit` がこれらを構造区切りとして扱わずそのまま `hostname` に含めてしまうため、
    再検証なしでは内部パスの断片が生のまま画面へ出る。不合格なら `hostname` を取り出さず
    固定文字列（`_INVALID_SAVED_BASE_URL_LABEL`）を返す。"""
    try:
        url = llm.openai_base_url(system_settings)
    except Exception:
        return ""
    if not url:
        return ""
    try:
        llm.assert_openai_base_url_allowed(url)
    except ValueError:
        return _INVALID_SAVED_BASE_URL_LABEL
    except Exception:
        return ""
    try:
        from urllib.parse import urlsplit
        return urlsplit(url).hostname or ""
    except Exception:
        return ""


def _openai_endpoint_kind(system_settings: dict | None = None) -> str:
    """`llm.openai_endpoint_kind()` の値。例外時は "openai"（既定・画面に注記を出さない）へ倒す。

    `system_settings`（省略可）は `_openai_base_url_host` と同じ理由でそのまま渡す。"""
    try:
        return llm.openai_endpoint_kind(system_settings) or "openai"
    except Exception:
        return "openai"


def _ollama_url_choice(s: dict, system_settings: dict | None = None) -> dict:
    """個人設定の Ollama 接続先 `<select>` の選択肢（`model_catalog[field]` と同じ
    `{"allowed": [...], "default": "..."}` 形 ＋ `legacy`）。許可ホスト一覧から選ぶ・空文字は
    「管理者の既定を使う」（モデル名欄と同じ規約）。

    `allowed` には**許可ポリシー（loopback／admin allowlist）を実際に満たす完全 URL だけ**を返す
    （選んでも保存時に 422 になる選択肢を UI に出さない）。利用者の現在の保存値が許可されていない
    （旧 admin allowlist の削除等で失効した）場合は `allowed` へ混ぜず、`legacy` に別枠で返す
    （画面側はこれを「一覧外（失効）」として警告表示できる）。

    `allowed` は **完全 URL（scheme 込み）を保持する**（host:port へ正規化して返すと、選択・保存・
    接続テストの往復で https が http に化ける・IPv6 の角括弧が失われる等の劣化が起きるため）。
    scheme が分かっている実際の URL（中央既定・利用者の現在の保存値）を admin allowlist からの
    合成エントリより**先に**追加する（host:port が既に他のエントリで解決済みなら重複追加しない＝
    scheme 不明な allowlist から `http://` を補って合成したエントリが、既に分かっている https を
    上書きしない）。

    重大バグ是正（RV 3巡目 #8）: 同一 host:port の実 URL 同士（例: 中央既定が `http://host:443`・
    利用者の現在値が `https://host:443`）が衝突する場合、以前は「先勝ち」で中央（http）が個人の
    https を隠してしまい、policy-valid な https 現在値が `allowed` にも `legacy` にも現れず
    UI で「一覧外」と誤表示していた。ここでは同一 host:port が既にある場合、後から来た URL が
    `https://` かつ既存が `https://` でなければ**置換**する（scheme 不明な allowlist からの
    `http://` 合成エントリが正しい https を上書きすることはない＝合成分は必ず最後に追加される
    ため、常に「他の実 URL が既に埋めた枠」を尊重する）。

    `system_settings`（省略可）: `ollama_url`（中央既定）・`llm._allowlisted_hosts()` の両方の
    解決をそれで行う（省略時は自分で読む）。
    """
    sysset = system_settings if system_settings is not None else store.get_system_settings()
    central_url = (sysset.get("ollama_url") or "").strip() or keys.DEFAULT_OLLAMA_URL
    allowlisted = llm._allowlisted_hosts(sysset)
    allowed: list[str] = []
    seen: dict[tuple[str, int], int] = {}   # host:port -> allowed 内の index

    def _policy_valid(hp: tuple[str, int]) -> bool:
        return llm.is_loopback_host(hp[0]) or hp in allowlisted

    def _add(url: str | None):
        if not url:
            return
        hp = llm._canonical_host_port(url)
        if hp is None or not _policy_valid(hp):
            return
        full = url if "://" in url else f"http://{url}"
        idx = seen.get(hp)
        if idx is None:
            seen[hp] = len(allowed)
            allowed.append(full)
            return
        if full.startswith("https://") and not allowed[idx].startswith("https://"):
            allowed[idx] = full   # https が同一 host:port の http を置換する

    # scheme が分かっている実URL（中央既定・利用者の現在値）を先に確定させ、admin allowlist からの
    # http:// 合成エントリは最後に（未確定分だけ）補う。
    _add(central_url)
    current = (s.get("ollama_url") or "").strip()
    _add(current)
    for host, port in sorted(allowlisted):
        _add(f"http://{llm.format_host_port(host, port)}")

    legacy = None
    if current:
        hp = llm._canonical_host_port(current)
        if hp is None or not _policy_valid(hp):
            legacy = current

    return {"allowed": allowed, "default": central_url, "legacy": legacy}


def _model_choice_table_by_provider(system_settings: dict | None = None) -> dict:
    """`intent_model`／`search_helper_model` のように実効プロバイダが実行時まで決まらない欄
    （`FIELD_CELLS` の中で複数プロバイダを跨ぐもの）について、プロバイダごとの選択肢を全パターン
    事前に返す（`{field: {provider: {"allowed": [...], "default": "..."}}}`）。個人設定画面
    （`web/settings.js`）が intent_provider／search_helper のセレクタを変更した瞬間に、サーバへ
    再往復せずモデル欄の選択肢を再描画できるようにするため（RV 是正）。保存済みの実効プロバイダに
    基づく `model_catalog[field]`（`_public_settings` 参照）とは別に、**選べる全プロバイダ**を返す
    （未保存の「見込み」選択に追従するため）。

    `system_settings`（省略可）は `field_choice_info()` へそのまま渡す。
    """
    out: dict = {}
    for field, cells in model_catalog.FIELD_CELLS.items():
        providers = sorted({p for p, _u in cells})
        if len(providers) < 2:
            continue
        out[field] = {p: model_catalog.field_choice_info(field, provider=p, system_settings=system_settings)
                      for p in providers}
    return out


def _public_settings(s: dict) -> dict:
    bedrock_model = s.get("bedrock_model") or _BEDROCK_MODEL
    bedrock_known = bedrock_model in _BEDROCK_MODEL_IDS or bedrock_model in (s.get("bedrock_verified_models") or [])
    # key_set は「今この設定で実際に使えるキーがあるか」＝`keys.resolve_api_key`（中央/個人・
    # A6/A7 込み）の結果で判定する。個人キー許可 OFF・または A7 非選択プロバイダなら、保存済みの
    # 個人キーがあっても false になる（＝画面に「未設定」と映る＝実態と一致させる）。
    # この関数内の system_settings 依存の解決（3キー・agent 既定選択・cloud_provider・
    # construct_id の A7 判定・モデルカタログ・Ollama allowlist）は同じスナップショットで行う
    # （個別に読み直すと、応答の途中で admin 更新が挟まった場合に「新旧が混ざった1レスポンス」に
    # なりうる窓を塞ぐ）。
    sys_s = store.get_system_settings()
    openai_key = keys.resolve_api_key("openai", s, system_settings=sys_s)
    gemini_key = keys.resolve_api_key("gemini", s, system_settings=sys_s)
    bedrock_key = keys.resolve_api_key("bedrock", s, system_settings=sys_s)
    return {"agent": s["agent"] or agent_constructs.default_agent(sys_s),
            # WEB-1: web_search の管理者許可は system_settings.web_search_allowed（管理画面
            # 「プロバイダ＋接続先」タブ）が唯一の真実源。ここでは調べ方ブロックの Web 検索行の
            # 表示条件（web/chat/menus.js 参照）としてのみ使う——実行に使うかどうかはチャット
            # ごとの `ChatReq.web_search` のみを見る（旧: 個人設定 `codex_web_search` は列を
            # 残すが実行経路では読まない）。
            "web_search_available": _web_search_admin_allowed(sys_s),
            "codex_web_search": bool(s.get("codex_web_search")),
            # RV MED（2026-08-18 Codex RV 2巡目 指摘3）: 真偽値のみだとプレースホルダでも「設定済み」と
            # 表示し得た。判定を provider 選択・health と同じ `agent_constructs.is_real_api_key` に揃える
            # （利用者が設定画面で入れた実キーの扱いは変えない＝プレースホルダ文字列と一致しない限り真）。
            "openai_key_set": agent_constructs.is_real_api_key(openai_key),
            # 接続先の種類（openai/azure/custom）とホスト名のみ（キー・パスは出さない）。
            # 管理画面「AIプロバイダ（クラウド）」カードの設定であり、ユーザーごとには変わらない。
            "openai_endpoint_kind": _openai_endpoint_kind(sys_s),
            "openai_base_url_host": _openai_base_url_host(sys_s),
            "ollama_url": s["ollama_url"],
            "gemini_key_set": bool(gemini_key),
            "bedrock_model": bedrock_model,
            "bedrock_key_set": bool(bedrock_key),
            # RV MED（2026-07-15）: フロント（web/settings.js）が「旧設定（legacy）」表示と検証済み
            # 表示を区別するための情報。known=true なら静的 choices か verified 済み＝正当な値。
            "bedrock_model_known": bedrock_known,
            "bedrock_model_label": _bedrock_profile_label(bedrock_model, ""),
            # 4構成（2026-08-15・agent_constructs）: 現在の構成と、この環境で選べる構成の一覧。
            # 画面はこの一覧だけを描画する＝env で無効な AI は選択肢にも入力欄にも出さない。
            "codex_model_provider": s.get("codex_model_provider") or "",
            # 検索アシスタント（2026-08-15）: 下調べを安いモデルへ任せる利用者ごとの設定。
            "search_helper": s.get("search_helper") or "",
            # 旧・個人上書き時代のモデル指定（個人設定に入力欄は無い・保存もされない）。実行時には
            # もう使われないが、以前この画面で選んだ値が DB に残っている利用者へ「もう使われて
            # いない」と伝えるためだけに読み取り専用で返す（web/settings.js の注記表示）。
            "search_helper_model": s.get("search_helper_model") or "",
            "construct_id": agent_constructs.construct_id(s, system_settings=sys_s),
            "constructs_available": agent_constructs.available_constructs(system_settings=sys_s),
            "system_prompt": s.get("system_prompt", ""),
            "cloud_provider": keys.selected_cloud_provider(sys_s),
            "personal_api_keys_allowed": keys.personal_keys_allowed(sys_s),
            # 個人設定の「外部連携」欄を出し分けるためのフラグ（既定 false・
            # personal_api_keys_allowed と同型）。
            "user_api_keys_allowed": bool(sys_s.get("user_api_keys_allowed") or False),
            # 自己発行キーの1日あたり呼び出し上限（既定/上限・管理者統制）。発行フォームの
            # プレースホルダ表示用（未指定時に何件が適用されるかを事前に見せる）。
            "user_api_keys_daily_quota_default": store.resolve_self_issued_daily_quota_cap(sys_s),
            # 各モデル名欄の選択肢。プロバイダが固定の欄（openai_model 等）は積集合、実効プロバイダが
            # 解決できる欄（intent_model/search_helper_model・`_effective_provider_for_field` 参照）は
            # そのプロバイダのセルだけに絞り込む（保存時検証と一致させる＝画面に出るのに PUT すると
            # 422 になる選択肢を無くす・RV 是正）。保存済みの値がカタログ外（旧・自由入力時代の値等）
            # でも拒否しない（移行期の寛容）＝ここでは選択肢だけを返し、警告表示は画面側。
            "model_catalog": {
                field: model_catalog.field_choice_info(
                    field, provider=_effective_provider_for_field(field, s, system_settings=sys_s)[0],
                    system_settings=sys_s)
                for field in model_catalog.FIELD_CELLS
            },
            # intent_model／search_helper_model のプロバイダ別選択肢（未保存のセレクタ変更に画面が
            # 再往復無しで追従するため・`web/settings.js::_resyncModelChoicesForProvider` 参照）。
            "model_catalog_by_provider": _model_choice_table_by_provider(sys_s),
            # 個人の Ollama 接続先は許可ホスト一覧から選ぶ（許可ホスト一覧＋中央既定・full URL 保持）。
            "ollama_url_choice": _ollama_url_choice(s, system_settings=sys_s)}


@settings_router.get("/config", tags=["設定"], response_model=ConfigResponse)
def config_get(request: Request):
    """利用可能な AI プロバイダ情報（現在の設定を踏まえた provider_info）を返す。"""
    u = _current_user(request)
    return provider_info(store.get_settings(u["uid"]))


@settings_router.get("/settings", tags=["設定"], response_model=SettingsResponse)
def settings_get(request: Request):
    """現在ユーザーの設定を返す（API キーは有無のみ・値は返さない）。"""
    u = _current_user(request)
    return _public_settings(store.get_settings(u["uid"]))


# S6（2026-07-03）: `GET /settings/bedrock-models` の per-user 結果キャッシュ（プロセス内 dict・TTL 付き）。
# 設定画面を開くたびに control-plane を叩かないため。過剰設計しない＝これで十分（複数ワーカー構成では
# ワーカーごとに独立したキャッシュになるが、TTL が短いため実害は小さい）。
# RV MEDIUM（2026-07-03）: entry は「どのキーで取得したか」の fingerprint（値そのものは持たない・
# sha256 先頭16桁）を持ち、read/write のどちらでも**その時点の現在キー**と不一致なら破棄する。
# 素朴な dict[uid]=(...) だけだと、GET が古いキーで control-plane 呼び出し中に PUT でキーが変わった
# 場合、GET 完了時に古い結果を書き戻してしまい、以後 TTL（5分）はそのユーザーに新キーの結果が
# 一切反映されない read-modify-write 競合になる。dict の複合操作（読取判定・書込）は
# `threading.Lock` で保護する（ネットワーク呼び出し自体はロック外＝他ユーザーの読み書きを塞がない）。
_BEDROCK_MODELS_CACHE: dict[str, tuple[float, str, list, str | None]] = {}
_BEDROCK_MODELS_CACHE_TTL = 300.0   # 5分
_BEDROCK_MODELS_CACHE_LOCK = threading.Lock()

# RV LOW（R4-2・2026-07-16 Codex RV 4巡目再検証）: per-uid キャッシュ世代カウンタ。fp の再確認
# （ロック外・低速な記録処理を挟む）から実際のキャッシュ書込（ロック内）までの間に、別リクエストが
# キーを変更（`settings_put` がキャッシュを pop する箇所）すると、その"別リクエスト"自身の新しい
# fetch＋書込が先に完了していた場合、この"古いリクエスト"の遅延書込がそれを上書きしてしまう
# （stale なキーの fingerprint で書くだけなので次回読取で即座にキャッシュミスになり、誤った内容が
# 提供されるわけではないが、有効なキャッシュが無駄に潰れ、無用な control-plane 再呼び出しを招く）。
# 対処: `settings_put` のキー変更時 pop と同じ箇所でこの世代を increment し、`settings_bedrock_models`
# は最初のキャッシュ参照時点の世代を記憶しておき、実際に書き込む直前（ロック内）に世代が変わって
# いないか確認する（変わっていれば書込をスキップ＝新しい entry を残す）。
_BEDROCK_MODELS_CACHE_GEN: dict[str, int] = {}


def _bedrock_record_and_filter(uid: str, models: list, fp: str) -> list | None:
    """`models`（列挙/キャッシュヒット結果・`[{"id","label"}]`）のうち動的な ID だけを実在確認済み
    テーブルへ記録し、応答を「記録に成功した動的 ID ∪ 静的 choices」へ絞り込む。`None` は
    fingerprint 不一致（キー変更中）で何も記録していない場合（呼び出し側は N2 のエラー応答を返す）。

    RV LOW（L2・2026-07-16 Codex RV 5巡目再検証）: 静的 choices（`_BEDROCK_MODEL_IDS`）は
    `_bedrock_model_id_valid` が無条件で受理するため、実在確認済みテーブルへ記録する必要が無い。
    記録すると動的分の容量（`_BEDROCK_VERIFIED_MODELS_MAX`）を無駄に消費し、満杯時に「静的なのに
    保存枠不足」という誤ったエラーを招く実害があった（Codex RV 指摘）。渡す ids からあらかじめ
    静的分を除外する（store 層は「静的」という概念を知らない汎用のまま・フィルタはこのルータ層だけで
    行う）。応答フィルタの `keep` は既存どおり `∪ _BEDROCK_MODEL_IDS` で静的を無条件に含める。

    RV LOW（C1・2026-07-16 Codex RV 6巡目再検証）: 動的分が無い（全て静的＝`dynamic_ids` が空）場合、
    記録処理自体を丸ごとスキップするだけだと fingerprint 再確認も素通りしてしまう。静的 choices は
    `PUT /settings` が無条件で受理するため保存契約自体は破れないが、非静的経路（キー変更中は
    「設定が変更されました」で正直に失敗を伝える）と意味論を揃えるため、この場合も現在の
    fingerprint を再確認する。
    """
    dynamic_ids = [m["id"] for m in models if m["id"] not in _BEDROCK_MODEL_IDS]
    if dynamic_ids:
        retained = store.add_bedrock_verified_models(uid, dynamic_ids, expected_key_fp=fp)
        if retained is None:
            return None
    else:
        if _bedrock_key_fingerprint(store.get_settings(uid).get("bedrock_api_key")) != fp:
            return None
        retained = []
    keep = set(retained) | _BEDROCK_MODEL_IDS
    return [m for m in models if m["id"] in keep]


@settings_router.get("/settings/bedrock-models", tags=["設定"], response_model=BedrockModelsResponse)
def settings_bedrock_models(request: Request):
    """現在ユーザーの Bedrock 設定でアカウントが実際に使える推論プロファイルを取得する（S6）。

    admin 限定にしない（自分の頭脳選択のための情報・他人の設定は見えない）。ログイン中ユーザーの
    保存済み `bedrock_api_key`（未設定ならサーバ側 env）で control-plane を叩き、ACTIVE な anthropic
    系のみを返す。キー自体は応答に含めない。失敗（キー無し/403/ネットワーク）でも 200 のまま
    `{"models": [], "error": "<短い理由>"}` を返す（設定画面の UX を壊さない）。per-user 5分キャッシュ
    （key の fingerprint が変わっていれば期限内でも破棄・上の RV MEDIUM 参照）。

    中核契約（Codex RV 3巡目）:「この応答に含まれる ID は必ず `PUT /settings` で保存できる」。
      - RV MED（N1・2026-07-16再検証）: 取得できた ID 群の記録は `store.add_bedrock_verified_models`
        の `expected_key_fp` に開始時点の fingerprint を渡し、**行ロック取得後・DB 書込直前に
        同一トランザクション内で**現在の `bedrock_api_key` と再照合させる（呼び出し側でスナップ
        ショットを取って別途比較する旧方式は、比較〜記録の間に別リクエストのキー変更がコミット
        される TOCTOU を埋め切れなかった）。不一致（`None` が返る）なら記録していない。
      - RV MED（N2・2026-07-16再検証）: 不一致時は verify と同じ意味論に統一し、その場の応答も
        `{"models": [], "error": "設定が変更されました。もう一度お試しください"}` にする
        （models をそのまま返すと「表示はされたが保存すると 422」という握りつぶしの変種になるため）。
      - RV MED（N3・2026-07-16再検証）: `add_bedrock_verified_models` の返り値（cap 適用後も実際に
        残った ID のサブセット）で応答を「保持セット ∪ 静的 choices」にフィルタする（`_bedrock_
        record_and_filter` 参照）。cap（`_BEDROCK_VERIFIED_MODELS_MAX`）値に依らず、返す ID は必ず
        保存可能というのが構造的に保証される（AWS `ListInferenceProfiles` は1回の応答が cap を
        超えうる・同定数のコメント参照）。
      - RV LOW（L2・2026-07-16 Codex RV 5巡目再検証）: 記録対象（`add_bedrock_verified_models` へ
        渡す ids）からは静的 choices を除外する（`_bedrock_record_and_filter` 参照）。静的は無条件で
        受理されるため記録不要＝動的分の容量を無駄に消費しない（満杯時に静的まで「保存枠不足」に
        なる誤りを防ぐ）。
      - キャッシュヒット早期 return 側でも記録は毎回行う（F3: 記録は冪等・ボタン押下時のみの経路
        なので DB 1往復は許容・cap 溢れで store 側から evict された ID がキャッシュには残っているのに
        保存不能になる、という穴を塞ぐ）。models が空（失敗結果）の場合は記録は不要（何も無いため）
        だが、キャッシュ書込自体は取得完了後に世代（`_BEDROCK_MODELS_CACHE_GEN`）が変わっていない
        時だけ行う（RV MEDIUM 3・キー変更中の stale write 対策・R4-2 で fp 再確認から世代カウンタへ
        統一）。
      - RV LOW（R4-2・2026-07-16 Codex RV 4巡目再検証）: 記録成功後・実際のキャッシュ書込までの間に
        別リクエストがキーを変更し、かつその別リクエストの新しい fetch＋書込が先に完了していると、
        この（古い）リクエストの遅延書込がその有効な entry を潰しうる（stale 提供にはならないが
        無用な control-plane 再呼び出しを招く）。`_BEDROCK_MODELS_CACHE_GEN`（per-uid 世代カウンタ・
        `settings_put` のキー変更時 pop と同時に increment）をこのリクエスト開始時点で記憶しておき、
        書込直前（ロック内）に世代が変わっていないか確認してから書く。
      - RV LOW（L1・2026-07-16 Codex RV 5巡目再検証）: 上の世代の捕捉は**関数の最初**（`key` の
        読取より前）で行う。捕捉がキー読取の後だと、「このリクエストがキーを読んだ直後・まだ世代を
        捕捉する前」に別リクエストが世代を進めた場合、このリクエストは既に進んだ後の世代を自分の
        基準として捕捉してしまい、古いキーの結果を書込直前チェックが「変化無し」と誤認する
        （R4-2 の是正が不完全だった穴）。

    RV MED（親検収・2026-07-16）: `_BEDROCK_MODELS_CACHE_LOCK` は「低速 I/O はロック外」が既存設計
    原則（列挙の外向き通信をロック外にしているのと同じ理由）。キャッシュヒット側の記録
    （`store.add_bedrock_verified_models` の DB 書込・FOR UPDATE 待ちを含みうる）をロックを握ったまま
    呼ぶと、同一ユーザーの並行 verify が行ロックを握っている間、その待ちで**全ユーザー**の列挙
    キャッシュ読取がブロックされてしまう（DB 障害時はさらに長く握る）。ロック内では
    ヒット判定とスナップショット取得（`cached` タプルは不変）だけを行い、記録・return はロックを
    抜けてから行う。
    """
    u = _current_user(request)
    uid = u["uid"]
    # RV LOW（L1・2026-07-16 Codex RV 5巡目再検証）: 世代の捕捉は**このリクエストの最初**（キー読取
    # より前）で行う。以前はキー読取の後に捕捉していたため、「このリクエストがキーを読んだ直後・
    # まだ世代を捕捉する前」に別リクエストが PUT でキーを変更（世代 increment）すると、この
    # リクエストは（古いキーで処理を続けているにもかかわらず）既に進んだ後の世代を自分の基準として
    # 捕捉してしまい、その後の書込直前チェックが「何も変わっていない」と誤認してしまう（結果、
    # 別の正当なリクエストが書いた新しい有効なキャッシュ entry を、この（古いキーの）リクエストが
    # 上書きしうる）。世代捕捉をキー読取より前に置くことで、キー読取以降に起きた変化は必ず
    # 検知できるようにする。
    gen = _BEDROCK_MODELS_CACHE_GEN.get(uid, 0)
    key = store.get_settings(uid).get("bedrock_api_key")
    fp = _bedrock_key_fingerprint(key)
    now = time.monotonic()
    with _BEDROCK_MODELS_CACHE_LOCK:
        cached = _BEDROCK_MODELS_CACHE.get(uid)
        hit = cached if cached and cached[1] == fp and now - cached[0] < _BEDROCK_MODELS_CACHE_TTL else None
    if hit is not None:
        hit_models, hit_error = hit[2], hit[3]
        if hit_models:
            hit_models = _bedrock_record_and_filter(uid, hit_models, fp)
            if hit_models is None:
                return {"models": [], "error": "設定が変更されました。もう一度お試しください"}
        return {"models": hit_models, "error": hit_error}
    models, error = list_bedrock_inference_profiles(key)   # ロック外（低速な外向き通信を他ユーザーへ波及させない）
    if models:
        models = _bedrock_record_and_filter(uid, models, fp)
        if models is None:
            return {"models": [], "error": "設定が変更されました。もう一度お試しください"}
        # R4-2: 記録が終わった今この瞬間でも、世代が変わっていなければ書く（変わっていれば、別
        # リクエストの新しい有効な entry を古い fp で潰さないようスキップする）。応答自体は
        # models/error をそのまま返す（stale 提供の心配は無い＝この応答は「このリクエスト自身が
        # 今取得した」内容そのもの）。
        with _BEDROCK_MODELS_CACHE_LOCK:
            if _BEDROCK_MODELS_CACHE_GEN.get(uid, 0) == gen:
                _BEDROCK_MODELS_CACHE[uid] = (now, fp, models, error)
        return {"models": models, "error": error}
    # models が空（失敗）: 記録するものが無いが、取得中にキーが変わっていた場合の stale なキャッシュ
    # 書込（RV MEDIUM 3→R4-2 で世代カウンタに統一）は models の有無に関係なく起こりうるため、
    # ここでも世代が変わっていないか確認してから書く。
    with _BEDROCK_MODELS_CACHE_LOCK:
        if _BEDROCK_MODELS_CACHE_GEN.get(uid, 0) == gen:
            _BEDROCK_MODELS_CACHE[uid] = (now, fp, models, error)
    return {"models": models, "error": error}


# バッチ2・1番（2026-07-03）: 実環境で「接続テストOK・モデル取得は失敗（既定選択肢のまま）」の報告。
# 容疑は Bedrock API キー（Bearer）が runtime（InvokeModel）専用で control-plane（ListInferenceProfiles）
# 権限が無いケース＝`GET /settings/bedrock-models` の動的列挙が使えない。列挙に頼らず、ユーザーが
# 分かっているモデルID（推論プロファイルID）を**実際に1回叩いて検証**してから追加できる経路を用意する。
# ID の推測ハードコードはしない（BEDROCK_MODEL_CHOICES のコメント参照）＝あくまで検証つき手動入力。
_BEDROCK_VERIFY_TIMEOUT = 8.0     # 設定画面のボタン押下待ち＝短め（GET 側の _BEDROCK_LIST_TIMEOUT と同程度）
_BEDROCK_VERIFY_MIN_INTERVAL = 5.0   # per-user 連打抑制（悪用防止・実 API 課金/レート制限を消費するため）
_bedrock_verify_lock = threading.Lock()
_bedrock_verify_last_call: dict[str, float] = {}   # uid -> monotonic 秒


class BedrockVerifyReq(BaseModel):
    model_id: str


@settings_router.post("/settings/bedrock-models/verify", tags=["設定"], response_model=BedrockVerifyResponse)
def settings_bedrock_models_verify(req: BedrockVerifyReq, request: Request):
    """モデルID（推論プロファイルID）を実際に1回（max_tokens=1）叩いて検証する（S6 の動的列挙が
    使えない構成向け・バッチ2 1番）。成功のみ `{"ok": true, "id", "label"}`。失敗（形式不正/連打/
    401403/ネットワーク/検証中のキー変更）は `{"ok": false, "error": "<短い理由>"}`（連打抑制のみ 429）。
    キー自体は応答に含めない（既存の redact/固定文言流儀を踏襲）。
    """
    u = _current_user(request)
    uid = u["uid"]
    now = time.monotonic()
    with _bedrock_verify_lock:
        last = _bedrock_verify_last_call.get(uid)
        if last is not None and now - last < _BEDROCK_VERIFY_MIN_INTERVAL:
            raise HTTPException(429, "検証の間隔が短すぎます。しばらく待って再試行してください。")
        _bedrock_verify_last_call[uid] = now   # 形式不正でも「試行」自体はレート制限の対象にする

    model_id = (req.model_id or "").strip()
    if not BEDROCK_MODEL_ID_RE.fullmatch(model_id):
        return {"ok": False, "error": "モデルIDの形式が正しくありません（例: jp.anthropic.claude-xxx-v1:0）"}

    settings = store.get_settings(uid)
    api_key = settings.get("bedrock_api_key")
    if not _bedrock_auth_available(api_key):
        return {"ok": False, "error": "Bedrock の API キー/AWS 認証情報が未設定です"}
    # RV MED（F2・2026-07-15→N1・2026-07-16再検証）: probe（実 I/O・時間がかかりうる）の**前**に
    # このユーザーのキーの fingerprint を取っておく。記録は `store.add_bedrock_verified_models` の
    # `expected_key_fp` に渡し、行ロック取得後・DB 書込直前に**同一トランザクション内で**再照合させる
    # （probe 後にここで別途 SELECT して比較する旧方式は、比較〜記録の間に別リクエストのキー変更が
    # コミットされる TOCTOU を埋め切れなかった）。不一致（`None` が返る）なら記録しない＝ok:true の
    # まま未記録だと、選択肢には追加されたのに保存が 422 になる、握りつぶしの変種を作ってしまうため、
    # ここは ok:false で理由を返す。
    fp_before = _bedrock_key_fingerprint(api_key)

    ok, detail = BedrockProvider(None, model_id, api_key).probe(
        timeout=_BEDROCK_VERIFY_TIMEOUT, max_tokens=1)
    if not ok:
        _log.warning("bedrock model verify failed: uid=%s model=%s", uid, model_id)
        # BedrockProvider.probe() は内部で安全境界（_safe_bedrock_detail）を通すが、ここでも
        # _redact_bedrock_secret を重ねる（probe() 自体が差し替えられても抜けない最終防衛線・
        # 二重適用は無害）。
        return {"ok": False, "error": _redact_bedrock_secret(detail, api_key) or "接続に失敗しました"}

    # RV LOW（L2・2026-07-16 Codex RV 5巡目再検証）: 静的 choices（`_BEDROCK_MODEL_IDS`）は
    # `_bedrock_model_id_valid` が無条件で受理するため、実在確認済みテーブルへの記録は不要。
    # probe には意味がある（このユーザーの実際の AWS 権限で本当に呼べるかを確認できる）ので probe
    # 自体は通常どおり行うが、記録はスキップして直接 ok:true を返す（動的分の容量が満杯でも、静的
    # ID の verify が「保存枠不足」という誤ったエラーになる実害を防ぐ）。
    if model_id in _BEDROCK_MODEL_IDS:
        # RV LOW（C1・2026-07-16 Codex RV 6巡目再検証）: 記録処理自体をスキップするこのファストパス
        # でも、probe 完了後に現在キーの fingerprint を再確認する。静的 choices は PUT /settings が
        # 無条件で受理するため保存契約自体は破れないが、非静的経路（キー変更中は「設定が変更され
        # ました」で正直に失敗を伝える）と体験を揃える。
        if _bedrock_key_fingerprint(store.get_settings(uid).get("bedrock_api_key")) != fp_before:
            return {"ok": False, "error": "設定が変更されました。もう一度お試しください"}
        return {"ok": True, "id": model_id, "label": _bedrock_profile_label(model_id, "")}

    # 実際に1回叩いて成功した ID を「実在確認済み」として記録する（`_bedrock_model_id_valid` の
    # 正本・以後 PUT /settings で保存できるようになる）。
    retained = store.add_bedrock_verified_models(uid, [model_id], expected_key_fp=fp_before)
    if retained is None:
        return {"ok": False, "error": "設定が変更されました。もう一度お試しください"}
    # RV MED（R4-1・2026-07-16 Codex RV 4巡目再検証）: 単調保持（monotonic）への是正により、cap
    # （`_BEDROCK_VERIFIED_MODELS_MAX`）が満杯だと新規 ID は記録されない（evict して押し込むことは
    # しない＝既存 ID を後から取り消さないため）。probe 自体は成功していても ok:true で「選択肢には
    # 追加されたのに保存できない」握りつぶしを作らないよう、専用のエラーで正直に失敗を返す。
    if model_id not in retained:
        return {"ok": False, "error": "検証済みモデルIDの保存枠（200件）に達しています"}
    return {"ok": True, "id": model_id, "label": _bedrock_profile_label(model_id, "")}


def _effective_provider_for_field(field: str, settings: dict,
                                  system_settings: dict | None = None) -> tuple[str | None, bool]:
    """モデル名フィールドが実際に使われるプロバイダを `settings`（1つの設定値の集合＝保存済みの
    現在値、またはリクエストを重ねた保存後の見込み値のいずれでもよい）から求める。

    戻り値 `(provider, consumed)` の3状態（RV 是正）:
      - `(具体的なプロバイダ名, True)`: そのプロバイダのセルだけで判定する（`field_valid`/
        `field_choice_info` に `provider=` を渡す）。
      - `(None, True)`: **未確定**（auto の解決がキー等の実行時状態に依存し、この呼び出し時点では
        判断しない）。呼び出し側は複数プロバイダの和集合で寛容に判定する。
      - `(None, False)`: **非消費**（このフィールドの値は現在の選択では実行時に一切使われない・
        例: `intent_provider` が実質的に bedrock へ解決される場合は intent 分類に bedrock 経路が
        無いため `intent_model` を消費しない、`search_helper=""` は検索アシスタント自体が無効で
        `search_helper_model` を消費しない）。呼び出し側はモデル名検証を省略する（対象外として
        常に許可）＝正当な切替・無効化が、消費されない古い保存値によって誤って 422 にならない
        ようにする。

    `openai_model`/`gemini_model`/`ollama_model`/`codex_model` はプロバイダが固定（欄名そのものが
    プロバイダを表す）なので対象外＝ `(None, True)` を返す（`field_valid` 側が単一プロバイダの
    積集合で判定する）。

    `intent_provider`（または `extract_provider` フォールバック）の決定は `llm.resolve_provider_selection`
    （`pick_provider_selector` で明示 `auto` と空文字継承を区別し、auto に落ちた場合は実行時の
    `select_provider()` と同じ関数（`llm.resolve_auto_provider`）で解決する共有ラッパー・RV 是正）を
    使う。ここで解決を諦めて `(None, True)`（和集合フォールバック）に倒すと、保存時検証が「実行時は
    特定の1プロバイダに決まるのに、保存時は何でも許してしまう」という食い違いを起こす。

    `system_settings`（省略可）: auto 解決へそのまま渡す（省略時は自分で読む）。
    """
    if field == "intent_model":
        selector = llm.pick_provider_selector(settings.get("intent_provider"), settings.get("extract_provider"))
        if selector == "bedrock":
            return None, False   # intent 分類に bedrock 経路は無い（現状の runtime 実装・非消費）
        return llm.resolve_provider_selection(
            settings.get("intent_provider"), settings.get("extract_provider"), settings=settings,
            system_settings=system_settings), True
    if field == "search_helper_model":
        choice = str(settings.get("search_helper") or "").strip().lower()
        if not choice:
            return None, False   # 検索アシスタント無効化＝非消費
        return (choice if choice in (search_helper.OLLAMA, search_helper.OPENAI) else None), True
    return None, True


@settings_router.put("/settings", tags=["設定"], response_model=SettingsResponse)
def settings_put(req: SettingsReq, request: Request):
    """現在ユーザーの設定を更新（未指定フィールドは変更しない）。

    `bedrock_model` は allowlist 検証する（空文字/未指定＝既定を許可・静的選択肢／このユーザーが
    verify・列挙で実在確認済みの ID／現在保存中の値（grandfather）のいずれかでなければ 422）。
    RV MED（2026-07-15）: 以前は `BEDROCK_MODEL_ID_RE` の**形式一致だけ**でも許可していたため、
    形だけ正しい架空 ID が verify を経ずに保存できてしまう穴があった（Codex RV 指摘・実害）。
    正規表現単独では通さない＝`store.add_bedrock_verified_models` に記録済みの ID だけを許可する
    （2026-07 決定・BEDROCK_MODEL_CHOICES 参照）。`agent` も同様に allowlist 検証する
    （RV HIGH・2026-07-03: 監査ログに任意文字列が入るのを防ぐため。単一の真実源は
    `sherpa.agents.AGENT_PROVIDERS`）。

    R2a-S2（2026-07-13 横断レビュー対応）: `ollama_url` は保存前に `llm.assert_ollama_url_allowed`
    で宛先ポリシー（loopback／admin allowlist）を検証する。ブロック時は汎用メッセージの 422 のみ返す
    （到達可否の詳細は返さない＝到達オラクル対策・詳細は POST /settings/test 側で丸める）。

    A6（個人 API キー原則）: `personal_api_keys_allowed`（既定 false・管理画面で設定）が偽のときは
    クラウド AI のキー3種（openai/gemini/bedrock）を一切書かせない（422）。個人には発行しない、という
    原則をコードで強制する＝キー入力欄自体は UI 側で非表示にするが、直接 API を叩かれても拒否する。
    """
    u = _current_user(request)
    uid = u["uid"]
    # この関数内の system_settings 依存の判定・解決（A6・A7・Ollama URL/allowlist・モデル
    # カタログ）はすべて同じスナップショットで行う（個別に読み直すと、検証の途中で admin 設定が
    # 変わった場合に判定が矛盾しうる）。
    # 個人キーの実書込みだけは、このスナップショットが古くなる余地（admin が事後に無効化＋一括
    # 削除する競合）が残るため、`store.update_settings()` が書込み直前に advisory lock 付きで
    # 別途再確認する（`store.PersonalKeysDisallowedError` 参照・下の except 節）。
    sys_s = store.get_system_settings()
    if not keys.personal_keys_allowed(sys_s):
        for _k in ("openai_api_key", "gemini_api_key", "bedrock_api_key"):
            if getattr(req, _k) is not None:
                raise HTTPException(422, "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）")
    # grandfather 判定（現在保存中の値）用に一度だけ取得する（bedrock verified 一覧・各モデル名欄の
    # 現在値の両方に使う）。
    cur = store.get_settings(uid)
    # このリクエストを重ねた「保存後に成立する設定」。モデル名/接続先の検証・実効プロバイダ解決
    # ・接続先 probe など、複数箇所で同じ「保存後の姿」を見る必要があるため一度だけ作る
    # （`is not None`＝送られてきたフィールドだけ重ねる・明示的な "" も正しく重なる）。
    pending = {**cur, **{k: v for k, v in req.model_dump().items() if v is not None}}
    if req.bedrock_model:
        if not _bedrock_model_id_valid(req.bedrock_model, cur.get("bedrock_verified_models"),
                                       cur.get("bedrock_model")):
            raise HTTPException(422, "bedrock_model は選択肢から選ぶか、モデル取得/検証済みのIDを指定してください")
    if req.agent and req.agent not in AGENT_PROVIDERS:
        raise HTTPException(422, "agent は heuristic / codex / openai / ollama / gemini / bedrock のいずれか")
    # 標準MVPは4構成だけを見せる（決定 2026-08-15）。env で有効化していない外部AIは保存させない
    # ＝画面から消えているのに設定だけ残る状態を作らない（`agent_constructs` 参照）。
    if req.agent and agent_constructs.runtime_blocked(req.agent):
        raise HTTPException(422, "この AI はこの環境では利用できません（管理者が有効化していません）")
    # A7（クラウドプロバイダ排他選択）: 選択中でないクラウド系 agent（openai/gemini/bedrock）は
    # 保存させない（保存できても実行時に ollama へフォールバックするだけの構成を作らせない）。
    if req.agent and agent_constructs.agent_requires_unselected_cloud(req.agent, sys_s):
        raise HTTPException(422, "この AI は現在選択されているクラウドプロバイダではありません"
                                 "（管理画面でプロバイダを切り替えるか、別の AI を選んでください）")
    if req.codex_model_provider and req.codex_model_provider not in agent_constructs.CODEX_MODEL_PROVIDERS:
        raise HTTPException(422, "codex_model_provider は openai / ollama のいずれか")
    if req.search_helper is not None and req.search_helper not in search_helper.CHOICES:
        raise HTTPException(422, "search_helper は空（使わない）/ ollama / openai のいずれか")
    if req.search_helper == search_helper.OLLAMA:
        # 保存時に実際へ届くか確かめる（決定 2026-08-15）＝「選んだのに黙って効かない」を避ける。
        # 宛先ポリシー（loopback/allowlist）→ 実 probe の順（settings_test と同じ流儀）。
        # `keys.resolve_ollama_url(pending)` を使う（`req.ollama_url or ...` という truthy 判定は、
        # UI が送る明示的な ""（個人 override をクリア＝中央既定を使う指示）を「未指定」として扱い、
        # 個人未設定＋中央が共有 Ollama という正当な構成を localhost で probe して 422 にしてしまう
        # 実害を避けるため）。モデルは常に管理者のカタログ既定で probe する（個人設定のモデル名
        # 上書きは無い）。
        _url = keys.resolve_ollama_url(pending, system_settings=sys_s)
        try:
            llm.assert_ollama_url_allowed(_url, system_settings=sys_s)
        except llm.SsrfBlocked:
            raise HTTPException(422, "指定された Ollama 接続先は許可されていません"
                                     "（admin が allowlist に登録した host:port のみ使えます）")
        from sherpa.ingest import graph_extract
        _model = model_catalog.resolve_model("ollama", "subsearch", None, system_settings=sys_s)
        _ok, _ = graph_extract._probe({"provider": "ollama", "url": _url, "model": _model})
        if not _ok:
            raise HTTPException(422, "ローカル（Ollama）に接続できませんでした。"
                                     "Ollama が起動しているか、モデル名を確認してください")
    if req.ollama_url:
        try:
            llm.assert_ollama_url_allowed(req.ollama_url, system_settings=sys_s)
        except llm.SsrfBlocked:
            raise HTTPException(422, "指定された Ollama 接続先は許可されていません"
                                     "（admin が allowlist に登録した host:port のみ保存できます）")
    fields = {k: v for k, v in req.model_dump().items() if v is not None}
    try:
        store.update_settings(uid, **fields)
    except store.PersonalKeysDisallowedError:
        # A6 の事前チェック（上の sys_s）通過後、この保存の書込み直前に admin が無効化した
        # （store.update_settings が同一トランザクションで再確認し fail-closed した）。
        raise HTTPException(422, "個人 API キーは無効化されています（管理者が中央設定でキーを管理します）")
    if "bedrock_api_key" in fields:
        # キーを変えたら古いキーでの列挙結果を次回取得まで持ち越さない（S6・過度な TTL 待ちを避ける）。
        # GET 側と同じロックで保護する（dict 操作自体は GIL で原子的だが、GET の
        # read-check→write の複合操作と時系列を揃えるため同じロックを使う・RV MEDIUM）。
        # R4-2: 世代カウンタも同時に increment する（GET 側が「記録直後・キャッシュ書込直前」に
        # 世代の変化を検知し、この pop の後に別の GET が新しい有効な entry を書いても、それより
        # 前に開始していた古い GET の遅延書込がそれを潰さないようにするため）。
        with _BEDROCK_MODELS_CACHE_LOCK:
            _BEDROCK_MODELS_CACHE.pop(uid, None)
            _BEDROCK_MODELS_CACHE_GEN[uid] = _BEDROCK_MODELS_CACHE_GEN.get(uid, 0) + 1
    try:
        # API key の before/after は <set>/<unset>/<cleared> のみ記録（値は保存しない）。
        _audit_settings_update(uid, fields)
    except Exception:
        _log.warning("audit write failed for settings.updated (best-effort)")
    return _public_settings(store.get_settings(uid))


def _audit_settings_update(uid: str, fields: dict) -> None:
    """settings 更新の監査（API key は状態のみ・値は保存しない）。"""
    secret_keys = {"openai_api_key", "gemini_api_key", "bedrock_api_key"}
    changed: dict = {}
    for k, v in fields.items():
        if k in secret_keys:
            changed[k] = "<set>" if v else "<cleared>"
        elif k == "system_prompt":
            changed[k] = {"changed": True, "len": len(v) if v else 0}
        elif k == "ollama_url" and v:
            # 多層防御: 通常は保存前に `assert_ollama_url_allowed`（`_canonical_host_port` が
            # userinfo 付き URL を拒否する）を通るため userinfo が残った値はここへ届かないはずだが、
            # 監査ログ側でも念のため除去する（`llm._redact_url_for_error` と同じロジックを再利用）。
            changed[k] = llm._redact_url_for_error(v) or "<不正なURL>"
        else:
            changed[k] = v
    store.audit(uid, "settings.updated", "settings", f"settings:{uid}",
                detail={"changed_fields": list(fields.keys()), "changes": changed},
                outcome="success")


def _round_ollama_probe_detail(detail: str) -> str:
    """`POST /settings/test`（provider=ollama）の失敗理由を丸める（R2a-S2: 到達オラクル低減）。

    Connection refused/timeout/reset/DNS 失敗の区別を出さない（内部ホスト/ポートの生死判別に使える
    ポートスキャンオラクルになるため・到達可否そのものは allowlist で塞ぐ＝ここは detail の粒度だけ）。
    認証失敗（401 相当）だけは区別を残す（`graph_extract._http_detail` は HTTPError を
    `f"{e.code} ...: ..."` 形式に整形するため、401 はここで先頭一致する）。
    """
    if detail.startswith("401"):
        return detail
    return "Ollama への接続に失敗しました（詳細は表示しません）"


class TestReq(BaseModel):
    provider: str                              # openai / gemini / ollama / codex / bedrock
    openai_api_key: str | None = None          # 未入力なら保存済みキーで試す（入力時はそれで試す）
    gemini_api_key: str | None = None
    ollama_url: str | None = None
    bedrock_model: str | None = None           # Bedrock だけ例外＝下記参照
    bedrock_api_key: str | None = None
    # `bedrock_region` はここに置かない（region は常に東京固定＝`BedrockProvider`/`_bedrock_region`
    # 参照。入力を受け取っても無視されるだけの死んだフィールドを API 面に残さない）。
    # モデル名（openai/gemini/ollama/codex）はここに置かない。`/settings/test` はログイン済みなら
    # 誰でも呼べる（管理者確認なし・レート制限もない）ため、任意のモデル名を受け取ると一般ユーザーが
    # 実 probe（外部 API への実リクエスト）へ任意の値を到達させられてしまう。モデルは常に管理者の
    # 使えるモデル一覧の解決値で probe する（Bedrock だけ例外＝実在確認済みモデルの専用機構
    # （`store.add_bedrock_verified_models`）が別にあり、確認前の入力中の ID を試す用途がある）。
    # 接続先（kind/base_url/auth_header/api_version）の override もここに**置かない**。
    # `/settings/test` はログイン済みなら誰でも呼べる（管理者確認なし）ため、一般ユーザーが任意の
    # HTTPS 宛先を指定して中央キーを送信できてしまう SSRF／キー漏洩の穴になる。保存前の入力中の
    # 接続先で試す機能は admin 専用（`POST /admin/settings/openai-endpoint-test`・system_extras.py）
    # に分離した。この `/settings/test` は常に**保存済みの** system_settings をそのまま使う。


@settings_router.post("/settings/test", tags=["設定"], response_model=SettingsTestResponse)
def settings_test(req: TestReq, request: Request):
    """API キー/モデルの**接続テスト**（1回だけ最小リクエスト）。保存はしない。入力中のキーで試せる。

    返値 `{ok, provider, model, detail}`。ok=False の detail に実エラー（401=認証/429=クォータ/モデル不明 等）を載せる。

    R2a-S2（2026-07-13 横断レビュー対応）: provider=ollama は宛先ポリシー（loopback／admin
    allowlist）を probe 前に検証し、ブロック時は probe せず汎用メッセージの 422 を返す。probe した
    上での失敗は Connection refused/timeout/reset/DNS の区別を出さず丸める（`_round_ollama_probe_detail`
    参照・到達可否の詳細を返す到達オラクルを避ける。401 相当の認証失敗だけは区別を残す）。
    """
    from sherpa.ingest import graph_extract
    u = _current_user(request)
    s = store.get_settings(u["uid"])
    # system_settings 依存の解決（キー・モデル・URL・宛先許可）はこの1回のスナップショットで行う
    # （個別に読み直すと、接続テスト中に admin 更新が挟まった場合に判定が新旧混在しうる）。
    sys_s = store.get_system_settings()
    # 正規化不一致の是正: 前後の空白を除去してから比較する（実行時の共通判定＝A7/agent/
    # search_helper 等と同じ流儀・" OLLAMA " のような値を誤って「不明な provider」扱いしない）。
    prov = str(req.provider or "").strip().lower()
    pending = dict(s)
    # `ollama_url` は個人設定として残る欄＝送られてこなければ保存済みの値をそのまま使う
    # （`req.model or s.get(...)` という truthy 判定は、UI が送る明示的な `""`（クリア＝中央既定に
    # 従う）を「未指定」として扱ってしまい、保存済みの古い値のまま接続テストしてしまうため、
    # 値をそのまま重ねる＝`keys.resolve_ollama_url()` が空文字を正しく「既定へフォールバック」と
    # して扱う）。
    if req.ollama_url is not None:
        pending["ollama_url"] = req.ollama_url
    # モデル名は個人設定に無く TestReq にも無い（管理者の使えるモデル一覧の解決値のみで probe
    # する・一般ユーザーが任意のモデル名を実 probe へ到達させられないようにするため）。
    if prov == "codex":                       # Codex は CLI＝キー不要。CLI の有無とログイン状態を確認（フル exec はしない）
        import shutil
        import subprocess
        model = model_catalog.resolve_model("codex", "codex", None, system_settings=sys_s)
        # `codex login status`（下記）はモデル名を一切見ない（ログイン状態だけを見る）ため、
        # 文法として不正なモデル名（`CodexProvider` が実行時に拒否する値）でも subprocess 経路は
        # ok=True を返してしまう。ここで共通文法（`CodexProvider` と同じ判定）を先に確認する。
        if model and not model_catalog.CODEX_MODEL_NAME_RE.fullmatch(model):
            return {"ok": False, "provider": "codex", "model": model,
                    "detail": "モデル名の形式が不正です（使える文字: 英数字 . _ / - ・64文字以内）"}
        if not shutil.which("codex"):
            return {"ok": False, "provider": "codex", "model": model, "detail": "codex CLI が見つかりません（インストール/PATH を確認）"}
        # MED-4（2026-08-18 Codex RV）: 接続先が Azure 等（`openai_endpoint_kind() != "openai"`）の
        # Codex(OpenAI) 構成は `codex login status`（auth.json のログイン状態）が実際に動くかと無関係
        # （env のキーで接続する設計・`sandbox.py::_codex_clean_env` 参照）。以前はこの分岐が無く
        # 「CLI あり＋ログイン済み」であれば ok=True を返してしまい、実際には `_select_provider` が
        # `_UnwiredProvider` を返す（実キー/デプロイ名/サンドボックス/base URL のいずれか不足）
        # 構成でも接続テストだけ緑になる不整合があった。`_select_provider` と判定ロジックを共有する
        # （重複実装しない）ため `providers._codex_openai_compat_block_reason` を呼ぶ。入力中の未保存の
        # キー（`req.openai_api_key`）も試せるよう明示 override として渡す（モデル名は個人上書きが
        # 無いため明示指定しない）。
        # Codex(Ollama) 構成: サンドボックス無効時は `_select_provider` と同じ理由で fail-closed
        # （実行時と接続テストで判定が食い違うと、保存前は緑なのに実行時だけ honest failure になる
        # 不整合が起きる）。それ以外（従来どおり login status を見る＝Azure 判定と無関係）。
        from sherpa import llm as _llm
        # 正規化不一致の是正: 生値の raw string 比較でなく、実行時（_select_provider）と同じ
        # 共通resolver（`agent_constructs.codex_model_provider`）を通す＝「anthropic」等の不正値・
        # 前後空白は同じ判定/エラーになる（保存前は緑なのに実行時だけ食い違う事故を防ぐ）。
        try:
            codex_provider_choice = agent_constructs.codex_model_provider(s)
        except agent_constructs.InvalidCodexModelProviderError as e:
            return {"ok": False, "provider": "codex", "model": model, "detail": str(e)}
        # Ollama 分岐を先に見る（_select_provider と同じ順序＝providers/__init__.py 参照）。
        # 先に openai_endpoint_kind を評価すると、Codex(Ollama) 利用時でも無関係な OpenAI 系
        # 設定の型破損（JSONB の非文字列値）で ValueError になり、接続テストが false negative
        # になってしまう。
        if codex_provider_choice == "ollama":
            from sherpa.providers import _codex_ollama_sandbox_disabled_reason
            sandbox_reason = _codex_ollama_sandbox_disabled_reason()
            if sandbox_reason is not None:
                return {"ok": False, "provider": "codex", "model": model, "detail": sandbox_reason}
            _codex_kind = None
        else:
            # `sys_s`（保存済み中央設定）の openai_endpoint_kind/openai_base_url は JSONB のため
            # 非文字列の破損値もあり得る。`openai_endpoint_kind()` は判定分岐より先に型検査する契約
            # のため、破損時は ValueError を送出しうる＝ここで捕捉して正直な失敗にする
            # （未捕捉のまま 500 にしない）。
            try:
                _codex_kind = _llm.openai_endpoint_kind(sys_s)
            except ValueError:
                return {"ok": False, "provider": "codex", "model": model,
                        "detail": "接続先の設定が不正です。管理者に確認してください"}
        if codex_provider_choice != "ollama" and _codex_kind != "openai":
            from sherpa.providers import _codex_openai_compat_block_reason
            probe_settings = {**s, "openai_api_key": req.openai_api_key or s.get("openai_api_key")}
            # 入力中の未保存キー（req.openai_api_key）は A6（personal_api_keys_allowed）の対象外で
            # 試せるよう、明示 override として渡す（保存・ログ出力はしない）。モデル名は個人上書きが
            # 無い＝`model`（管理者のカタログ解決値）と常に一致するため明示指定しない。
            reason = _codex_openai_compat_block_reason(probe_settings, explicit_openai_api_key=req.openai_api_key,
                                                        system_settings=sys_s)
            if reason is not None:
                return {"ok": False, "provider": "codex", "model": model, "detail": reason}
            # 形式確認（サンドボックス有効・base URL 妥当・実キー・非既定デプロイ名）だけでは、
            # キー無効／デプロイ名不在／権限不足／DNS 不到達等の実失敗を「接続OK」と誤表示して
            # しまう。ここで実際に1回だけ最小リクエストする（Codex CLI 自体は起動しない＝
            # Codex が使うのと同じ base_url/キー/デプロイ名への直接プローブ。`graph_extract._probe`
            # は他プロバイダの接続テストと共有する唯一の実 HTTP 経路＝テストはここを差し替える）。
            # Codex CLI 経由（Responses API・実際の config.toml 生成物）の生死判定は
            # `make azure-smoke ARGS="--codex"` が別途担う。
            resolved_key = probe_settings["openai_api_key"] or keys.resolve_api_key("openai", s, system_settings=sys_s)
            ok, detail = graph_extract._probe({"provider": "openai", "key": resolved_key, "model": model,
                                              "openai_endpoint_override": sys_s})
            return {"ok": ok, "provider": "codex", "model": model,
                    "detail": ("接続OK（Azure OpenAI 等: 実際に接続して確認済み・codex login の状態は問いません）"
                               if ok else detail)}
        try:
            r = subprocess.run(["codex", "login", "status"], capture_output=True, text=True, timeout=20)
            ok = r.returncode == 0
            detail = "接続OK" if ok else ((r.stderr or r.stdout or "未ログイン（codex login が必要）").strip()[:200])
        except Exception as e:
            ok, detail = False, f"{type(e).__name__}"[:200]
        return {"ok": ok, "provider": "codex", "model": model, "detail": detail}
    if prov == "bedrock":                     # Bedrock は Anthropic SDK 直呼び（messages.create を max_tokens=16 で1回）
        from sherpa.agents import BedrockProvider
        model = req.bedrock_model or s.get("bedrock_model") or _BEDROCK_MODEL
        # 入力キー優先（未保存でも試せる）→中央/個人（A6/A7）解決。SDK の env(Bearer) チェーンには
        # もう委ねない（env はシード専用という所有原則に合わせる）。この直後で probe（実 API
        # 呼び出し）に使うため strict=True で解決する（課金を伴う接続テストは非 strict の
        # 寛容キー解決で実送信してはならないため）。
        try:
            api_key = req.bedrock_api_key or keys.resolve_api_key("bedrock", s, system_settings=sys_s, strict=True)
        except keys.InvalidCloudProviderConfigError as e:
            return {"ok": False, "provider": "bedrock", "model": model, "detail": str(e)}
        # region は常に東京固定（`BedrockProvider`/`_bedrock_region` 参照・入力面を持たない）。
        ok, detail = BedrockProvider(None, model, api_key).probe()
        # BedrockProvider.probe() は内部で安全境界（_safe_bedrock_detail）を通すが、ここでも
        # _redact_bedrock_secret を重ねる（probe() 自体が差し替えられても抜けない最終防衛線・
        # req.bedrock_api_key は未保存の入力中キーのこともあり、なおさら厳重に・二重適用は無害）。
        safe_detail = _redact_bedrock_secret(detail, api_key) if not ok else "接続OK"
        return {"ok": ok, "provider": "bedrock", "model": model, "detail": safe_detail}
    if prov == "gemini":
        model = model_catalog.resolve_model("gemini", "chat", None, system_settings=sys_s)
        # この直後で probe（実 API 呼び出し）に使うため strict=True で解決する（課金を伴う
        # 接続テストは非 strict の寛容キー解決で実送信してはならないため）。
        try:
            gemini_key = req.gemini_api_key or keys.resolve_api_key("gemini", s, system_settings=sys_s, strict=True)
        except keys.InvalidCloudProviderConfigError as e:
            return {"ok": False, "provider": "gemini", "model": model, "detail": str(e)}
        cfg = {"provider": "gemini", "key": gemini_key, "model": model}
    elif prov == "openai":
        model = model_catalog.resolve_model("openai", "chat", None, system_settings=sys_s)
        # この直後で probe（実 API 呼び出し）に使うため strict=True で解決する（課金を伴う
        # 接続テストは非 strict の寛容キー解決で実送信してはならないため）。
        try:
            openai_key = req.openai_api_key or keys.resolve_api_key("openai", s, system_settings=sys_s, strict=True)
        except keys.InvalidCloudProviderConfigError as e:
            return {"ok": False, "provider": "openai", "model": model, "detail": str(e)}
        # RV MED（2026-08-18 Codex RV 2巡目 指摘3）: env の OPENAI_API_KEY がプレースホルダのままだと、
        # 真偽値だけの判定は「キーあり」と誤認して実 API へ probe しに行き、分かりにくい 401 になる。
        # 他の消費箇所（provider 選択・health・設定済み表示）と同じ `is_real_api_key` で早期に弾く
        # （下の `not cfg.get("key")` 判定が `keys.NO_CENTRAL_KEY_MESSAGE` を返す・利用者が入力した
        # 実キーの扱いは変えない）。
        cfg = {"provider": "openai",
               "key": openai_key if agent_constructs.is_real_api_key(openai_key) else None,
               "model": model,
               # 接続先も含め、この probe 全体を入口で読んだ1つの `sys_s` だけで完結させる
               # （`complete_json` が送信時に別途 system_settings を読み直すと、この probe の
               # 判定中に admin 保存が挟まった場合、旧キーを新接続先へ送る等の混在が起こり得る）。
               # 一般ユーザーの接続先 override は受け付けない（admin 専用の
               # `POST /admin/settings/openai-endpoint-test` へ分離済み）。
               "openai_endpoint_override": sys_s}
    elif prov == "ollama":
        cfg = {"provider": "ollama", "url": keys.resolve_ollama_url(pending, system_settings=sys_s),
               "model": model_catalog.resolve_model("ollama", "chat", None, system_settings=sys_s)}
        # R2a-S2: probe（実 I/O）の**前**に宛先ポリシーを検証する。ブロック時は probe せず汎用
        # メッセージの 422 のみ返す（到達可否の詳細を返さない＝到達オラクル対策）。
        try:
            llm.assert_ollama_url_allowed(cfg["url"], system_settings=sys_s)
        except llm.SsrfBlocked:
            raise HTTPException(422, "指定された Ollama 接続先は許可されていません"
                                     "（admin が allowlist に登録した host:port のみ確認できます）")
    else:
        raise HTTPException(422, "provider は openai / gemini / ollama / codex / bedrock のいずれか")
    if prov in ("openai", "gemini") and not cfg.get("key"):
        return {"ok": False, "provider": prov, "model": cfg["model"], "detail": keys.NO_CENTRAL_KEY_MESSAGE}
    ok, detail = graph_extract._probe(cfg)
    if prov == "ollama" and not ok:
        # R2a-S2: Connection refused/timeout/reset/DNS の区別を出さない（ポートスキャンオラクル低減・
        # 401 相当の認証失敗だけは区別を残す）。
        detail = _round_ollama_probe_detail(detail)
    return {"ok": ok, "provider": prov, "model": cfg["model"],
            "detail": "接続OK" if ok else detail}


# ===== /healthz =====
# router に tags を持たせない（settings_router と同じ理由・タグ二重化を避ける）。
healthz_router = APIRouter()

# R5 RV LOW（2026-07-15）: 未 ready 中に未認証 /healthz が重なると、全リクエストが advisory lock に
# 並んで各自 DDL 全文を実行し、接続/スレッドが滞留し得る。再初期化はプロセス内 single-flight
# （非ブロッキング）にし、進行中なら試行せず即 503 を返す（sync エンドポイントは threadpool で
# 並行実行されるため lock が要る）。
_schema_init_inflight = threading.Lock()

# env→system_settings シード再試行の single-flight（schema 初期化とは独立＝schema 自体は ready の
# まま「シードだけ」が一時的に失敗した場合（DB の瞬断等）も、次の healthz 呼び出しで再試行できる
# ようにする（schema-ready への「遷移」の瞬間だけに絞ると、その回だけ DB が落ちていた場合に
# 永久に再試行されなくなる）。
_seed_retry_inflight = threading.Lock()


@healthz_router.get("/healthz", tags=["システム"])
def healthz():
    """死活監視用エンドポイント（R5: schema readiness 連動＝liveness→readiness 化）。

    `store.schema_ready()` が False（未適用・lifespan 起動時に DB 不達だった等）なら
    `store.init_schema()` を一度だけ試み（プロセス内 single-flight・進行中なら試行しない）、
    それでも ready でなければ 503 を返す（2026-07-13-横断レビュー対応.md R5）。ready なら従来どおり 200。

    schema が ready（今回の呼び出しで初めて ready になった場合も、すでに ready だった場合も両方）
    のたびに `api._seed_settings_from_env()` を試みる（起動時の env→system_settings シードが
    DB 不達で完了マーカーを付けられなかった場合の再試行経路）。シード自体は冪等
    （`store.seed_system_settings_once` が ON CONFLICT DO NOTHING で保護）なため、
    ready 確認のたびに呼んでも安全＝完了マーカーがあれば内部で即座に何もしない。
    """
    if not store.schema_ready() and _schema_init_inflight.acquire(blocking=False):
        try:
            store.init_schema()
        except Exception:
            pass
        finally:
            _schema_init_inflight.release()
    if store.schema_ready() and _seed_retry_inflight.acquire(blocking=False):
        try:
            from sherpa import api as _api   # 循環回避のため関数内 import（api.py は本モジュールを import 済み）
            _api._seed_settings_from_env()
            _api._seed_ollama_url_from_env()   # 同じ single-flight／再試行の枠に相乗り（独立したマーカー）
            _api._confirm_legacy_env_seed_marker()   # 同じ single-flight／再試行の枠に相乗り（旧マーカー互換）
            _api._catchup_ollama_allowlist_for_central_url()   # 同じ single-flight／再試行の枠に相乗り
            _api._seed_openai_endpoint_from_env()   # 同じ single-flight／再試行の枠に相乗り（独立したマーカー）
            _api._seed_depth_profile_from_env()   # 同じ single-flight／再試行の枠に相乗り（独立したマーカー）
            model_catalog.seed_catalog_once()   # 同じ single-flight／再試行の枠に相乗り（独立したマーカー）
        except Exception:
            pass
        finally:
            _seed_retry_inflight.release()
    if not store.schema_ready():
        return JSONResponse(status_code=503, content={"ok": False, "detail": "schema not ready"})
    return {"ok": True}


# ===== / (ルート) =====

root_router = APIRouter()


@root_router.get("/", tags=["システム"])
def _root():
    """ルートアクセスをトップ画面（/ui/home.html・運営掲示板）へ redirect。チャット直リンクは不変。"""
    return RedirectResponse("/ui/home.html")
