"""AI 下調べ検索（PART-4・`docs/proposals/2026-08-24-部品API設計.md` §「PART-4」・§8）。

チャットを介さない部品として、既存のチャット内 agentic search（`sherpa/agentic_search.py`）を
直接呼ぶだけの薄い層。`graph_admin.ask_graph`（管理グラフ質問・agentic_search を直接使う既存の
非チャット呼び出し元）と同じ構え——`sherpa.agents`／`sherpa.chat_service`／`sherpa.chat_router`
は import しない（`ext_api.py` と同じ循環回避・共有 KB のみの契約）。

Evidence Packet（EXT-2）の組み立て（citation の再重複排除・構造 Evidence の重複排除・
`ev-*` 採番）は `sherpa/providers/base.py` の private ヘルパ4つ（`_dedupe_citations_and_evidence`／
`_dedupe_structural_evidence`／`_evidence_packet_evidence`／`_omitted_evidence_gap_note`）を
読み取り専用で再利用する（同ファイルへは書き込まない・重複実装しない）。

model パラメータの許容値は管理者カタログ（`model_catalog`・用途 `subsearch`）が唯一の真実源。
現状カタログは openai/ollama の2プロバイダにしか `subsearch` セルを持たない（gemini/bedrock は
未対応）ため、本モジュールもこの2択のみを扱う。`provider`（省略可）で明示指定できる。両方
省略時は管理者設定「外部連携」タブの既定プロバイダ（`system_settings.research_default_provider`・
未設定なら Ollama＝コスパ踏襲・`default_research_provider()` 参照）を使う。プロバイダ/モデルが
利用不能・未接続・設定不備のいずれの場合もフォールバックせずエラーにする。

接続不可（接続拒否・名前解決失敗・TLS 検証失敗・ホスト/ネットワーク到達不能）は「予期しない
例外」の固定文言ではなく、どのプロバイダに繋がらなかったかが分かる専用の固定文言にする
（`agentic_search._is_connection_failure`・下の `except Exception` 分岐 docstring 参照。応答
タイムアウトはこの分類に含めない＝下記参照）——生の例外文字列・接続先 URL は外部応答に出さない
（provider 名は UI と同じ表示語彙のみ）。

**world の解決と固定**: `world` 文字列だけを受け取り、`sherpa.store.db.world_lock_shared()`
（PostgreSQL の共有 advisory lock・`world_lock`＝排他ロックと同じキーを取り合う）を保持した
**状態で** `worlds.resolve_external_world()` を自前で（再）解決し、`worlds.pin_world_root()` で
関数の残り全体に固定する。呼び出し側（`ext_api.py`）の preflight 解決（root・scope_paths 検証の
どちらも）は 404/422 を早く返すための軽い確認に留め、その結果は使わない——共有ロックを取る**前**に
解決した root は rebind の TOCTOU に対して脆弱なため。scope_paths も pinned root で改めて
authoritative に検証する（`InvalidScope`・呼び出し元は 422 にする）。デッドラインは
`absolute_deadline` 引数優先（`ext_api.ext_research` がハンドラ入口で確定した「リクエスト全体の
絶対期限」をそのまま渡す・下記「デッドライン」参照）——本関数が `timeout_s` から独自に
`time.monotonic()` を起点として絶対期限を作り直すと、preflight の所要時間ぶん丸ごと再付与
されてしまう。共有ロックは research 同士を並行させつつ、rebind/削除/取り込み
（`sherpa.ingest.worker` が使う排他 `world_lock`）とだけ直列化する。ロック取得は残り時間で
`lock_timeout`/`connect_timeout` を掛け、超過は `ProviderUnavailable`（503・デッドライン優先の
再分類対象外＝ロック競合は honest に 503 のまま返す）。

**デッドライン**: `absolute_deadline`（`time.monotonic()` 系の絶対値）が指定されていればそれを
そのまま使い、省略時（テスト等の直接呼び出し）のみ `timeout_s`（省略時は既定値）から本関数の
入口時刻を起点に計算する（詳細は `absolute_deadline` 引数の説明を参照）。ロック取得・LLM 呼び出し
（`agentic_search.openai_style` の `timeout` に 0引数 callable を渡し、ターンごとにその時点の
残り時間で再評価させる）・帰属呼び出しの各段階へ「その時点の残り時間」を渡す。成功パスは結果を
組み立てても即 return しない——共有ロックの解放と `finally` の `metering.record`（いずれも
所要時間が読めない DB 往復）を終えた**後**、関数の最後で改めて残り時間を確認してから返す
（黙った期限超過 200 を出さない）。`InvalidScope`（共有ロックを保持した状態から raise されうる）
も同じ扱い——即座に呼び出し元へは伝えず、ロック解放・metering を終えた後の共通デッドライン判定を
必ず経由させる（そうしないと、ロック**解放中**に期限を越えたケースでも 422 を返してしまう）。
ロック競合（503）を除き、期限超過はすべて `ResearchTimeout`（504）に収束させる（黙った
200/422/503 にしない・通信中の例外の種別を問わず `_remaining()<=0` を最優先する）。
"""
from __future__ import annotations

import logging
import os
import threading
import time

import psycopg

from . import agentic_search, citations, keys, llm, metering, model_catalog, store, worlds
from . import scope as scope_mod
from .providers import base as _provider_base
from .store.db import world_lock_shared

# `store/db.py`/`ext_api.py`等と同じ共有ロガー（新しいロガーを増やさない・単一の真実源）。
_log = logging.getLogger("sherpa")

# 用途 subsearch を持つ (provider, usage) の組。順序はそのまま「model 指定・provider 省略時、
# その model が両カタログに登場する曖昧なケースの優先度」——Ollama を先に見る（コスパ踏襲の既定と
# 同じ優先＝同名モデルが両カタログに登場する将来の事故でも安価な側へ倒れる）。model 省略時の既定
# provider は `default_research_provider()`（管理者設定・未設定なら ollama）を使う＝このタプルの
# 並びとは別の解決経路。
_SUBSEARCH_CELLS = (("ollama", "subsearch"), ("openai", "subsearch"))

# system_settings の1キー（PART-4a・外部連携タブ「AI 下調べ検索の既定 AI」）。未設定
# （キー無し／None／空文字）のみ "ollama"（コスパ踏襲・既存既定と同値）にフォールバックする。
# 設定されているのに不正な値（未知文字列・非文字列）はフォールバックせず ValueError にする
# （`default_research_provider` 参照）。
_RESEARCH_PROVIDER_SETTINGS_KEY = "research_default_provider"
# 公開定数（`system_extras.py` 等の他モジュールから参照する・`resolve_model_and_provider` 自身の
# provider 引数検証にも使う単一の真実源）。
RESEARCH_PROVIDERS = frozenset(p for p, _ in _SUBSEARCH_CELLS)

# provider コード（"ollama"/"openai"）→ 管理画面 UI と同じ表示名。外部応答メッセージの語彙を
# UI（`web/admin-settings.html` の vlm-provider/ext-research-default-provider select）と揃える
# ため、生の provider コードをそのままメッセージへ出さない。
_PROVIDER_DISPLAY_LABELS = {"ollama": "ローカル（Ollama）", "openai": "クラウド（OpenAI）"}


def default_research_provider(system_settings: dict | None) -> str:
    """下調べ検索（`model`/`provider` 両方省略時）の既定プロバイダ。

    管理画面「外部連携」タブの選択（`system_settings.research_default_provider`）が最優先・
    **未設定**（キー無し／`None`／空文字）は "ollama"（従来の固定既定と同値）。

    設定されているのに `RESEARCH_PROVIDERS` に無い値（未知文字列・非文字列＝典型的には
    JSONB の破損）は **フォールバックしない**——黙って "ollama" を使うと、保存されているはずの
    provider と異なるものが無言で実行される（モデル/プロバイダ利用不能時はフォールバックせず
    エラーにするという本モジュールの契約に反する）。代わりに `ValueError` を送出し、判断を
    呼び出し側へ委ねる:
    - `resolve_model_and_provider`（実行時）はこれを捕捉して `ProviderUnavailable`（503・
      未計測）へ変換する。`_validate_research_default_provider`（PUT 側）が既に不正値を 422
      で拒否しているため、ここでの検知は「保存後に何らかの経路で壊れた値」への防波堤。
    - `system_extras.py::_admin_settings_view`（表示用途）は他の破損値
      （`openai_endpoint_kind` 等）と同じ流儀で捕捉し `"(不正な保存値)"` へ畳む
      （管理画面全体を 500 にしない）。
    """
    value = (system_settings or {}).get(_RESEARCH_PROVIDER_SETTINGS_KEY)
    if value is None or value == "":
        return "ollama"
    if not isinstance(value, str) or value not in RESEARCH_PROVIDERS:
        raise ValueError(
            "research_default_provider の保存値が不正です（ollama/openai のいずれでもありません）")
    return value


def _connection_unavailable_message(provider: str) -> str:
    """接続失敗（`agentic_search._is_connection_failure`）専用の固定文言。provider の表示名は
    UI 語彙（`_PROVIDER_DISPLAY_LABELS`）に揃える——生の provider コード・URL・例外文字列は
    出さない。`run_research` は `provider` 解決（`resolve_model_and_provider`）が成功した
    **後**にしかこの分岐へ到達しないため、`provider` は常に非 None（"ollama"/"openai"）。"""
    return f"下調べに使う AI（{_PROVIDER_DISPLAY_LABELS[provider]}）に接続できません。管理者に設定を確認してください。"


# 反復上限の絶対上限（ExtResearchReq.max_iterations の Field(le=...) と揃える・`ext_api.py` 側）。
# 実際の反映は `agentic_search.MAX_TURNS`（env 由来）との min を取る＝管理者が env で下げていれば
# そちらを優先する（多層防御）。
_MAX_ITERATIONS_CEILING = 12

# 1回の LLM 呼び出し（HTTP リクエスト1本）に許す上限秒数。残り時間がこれより短ければそちらを
# 使う——残り時間をそのまま個別タイムアウトに使うと、応答が来ない1回が全体デッドラインを
# 大きく食い潰しうる（無制限ではない）。
_MAX_PER_CALL_TIMEOUT_S = 90

# `finally` の `metering.record()`（成否に関わらず実行を試みる・§8.3）専用の固定予算。この時点
# ではリクエストの残り時間（`_remaining()`）が既に 0 以下のことがあり得るため、残り時間ベースでは
# 「即座に諦める」か「無期限」のどちらかに倒れてしまう。「記録は試みるが無期限にはブロックしない」
# という独立した契約として、小さい固定秒数で bound する（残り時間を再利用しない）。
_METERING_DB_TIMEOUT_S = 5

_ZERO_USAGE = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0,
              "reasoning_output_tokens": 0}


class _ResearchError(RuntimeError):
    """失敗時も監査へ「どこまで解決/実行できたか」を残すための共通属性
    （`model_used`/`provider_used`/`llm_calls`・いずれも解決/計測できていなければ None）。
    """

    def __init__(self, message: str, *, model_used: str | None = None,
                provider_used: str | None = None, llm_calls: int | None = None):
        super().__init__(message)
        self.model_used = model_used
        self.provider_used = provider_used
        self.llm_calls = llm_calls


class ModelNotAllowed(_ResearchError):
    """`model` が管理者カタログ（用途 subsearch）の許可リストに無い（呼び出し元は 400 にする）。

    `_ResearchError` を継承する——`resolve_model_and_provider` 自体はロック取得より**前**に
    起きるため共有ロック解放は経由しないが、それより前段の `store.get_system_settings()`
    （設定読取）が長引いて既にデッドラインを超えていることがあり得るため、即座に 400 を確定
    させず `run_research` 末尾の共通デッドライン判定へ通す必要がある（期限超過後に 400 を
    返さないため）。
    """


class InvalidScope(_ResearchError):
    """`scope_paths` が共有ロック内で再解決した現在の world に対して無効
    （呼び出し元は 422 にする）。preflight（`ext_api._resolve_world_or_error` 後の検証）は
    ロック**前**の root を見ているため、取り込み中の変化を拾えない場合がある。

    `_ResearchError` を継承する——共有ロックを保持した状態から raise されるため、`run_research`
    末尾の共通デッドライン判定（ロック解放・metering の**後**に `_remaining()` を確認する箇所）
    を経由させる必要がある（期限超過後に 422 を返さないため）。
    """


class ProviderUnavailable(_ResearchError):
    """選択されたプロバイダ（openai/ollama）に接続できない、管理者カタログの設定不備
    （既定モデル空欄等）で解決できない、world/設定情報が一時的に確認できない（rebind 中の
    ロック競合を含む）、または最終合成の通信が失敗した。呼び出し元は別プロバイダへ黙って
    倒さず、そのままエラーとして呼び出し元（外部 API 利用者）へ返す。
    """


class ResearchTimeout(_ResearchError):
    """リクエスト全体のデッドライン（`timeout_s`・開始時刻基準）を超過した（呼び出し元は504にする）。"""


def _default_timeout_s() -> int:
    """`timeout_s` 省略時の既定（`search_helper._default_llm_timeout()` と同じ env・既定値）。"""
    return int(os.environ.get("SHERPA_LLM_TIMEOUT", "60"))


def research_task_id(request_id: str | None) -> str:
    """Evidence Packet の `task_id`（`ext-research:{request_id}`）。

    §8.1「下流の実行イベントへの伝播」の伝播先識別子はこれ1つ。`agentic_search.py` は
    Execution Event v2（`exec_event.build_event`）を1箇所（`_eval_node`）でしか呼ばないが、
    そこは (a) 評価フェーズ（`depth in ("medium","deep")`）専用で `run_research` は `depth` を
    一切渡さない＝到達しない、(b) 到達しても `task_id` を渡していない、の2重に伝播先として使えない
    （`tests/unit/test_research_service.py::
    test_no_exec_event_build_event_calls_during_successful_research` が runtime spy で固定）。
    実際に request_id が伝播している経路は現状 Evidence Packet の `task_id` と監査行の
    `request_id` の2つのみ（`_eval_node` へは配線しない）。
    """
    return f"ext-research:{request_id}" if request_id else "ext-research"


def resolve_model_and_provider(model: str | None, system_settings: dict,
                               provider: str | None = None) -> tuple[str, str]:
    """`model`（省略可）・`provider`（省略可・"ollama"/"openai"）→ `(provider, model)`。

    `model` 省略時: 使う provider は `provider`（指定時はそれに固定）／省略時は
    `default_research_provider(system_settings)`（管理者設定「外部連携」タブの既定・未設定なら
    "ollama"）。`model_catalog.resolve_model(provider,"subsearch",...)` の解決値を使うが、
    **その解決値自身が同じセルの `is_valid_model` を通らなければ設定不備として `ProviderUnavailable`**
    にする——管理者がカタログを明示設定した場合（例: `allowed` はあるが `default` が空欄）、
    `resolve_model` は組み込み既定（例 `qwen2.5`）へフォールバックするが、その組み込み既定が
    管理者の `allowed` に含まれないことがあり、黙って使うと「許可されていないはずのモデルを
    省略時にだけこっそり使う」という矛盾になる。この場合も `provider_used` は判明しているため
    例外へ載せる（呼び出し回数はゼロ）。

    `model` 指定・`provider` 指定時: その provider のセルでのみ allowed 判定する（優先順位探索は
    しない）。許可されていなければ `ModelNotAllowed`。

    `model` 指定・`provider` 省略時: `_SUBSEARCH_CELLS` の順に allowed 判定し、最初に一致した
    provider を採用する（同名モデルが両カタログに登場する曖昧なケースの tie-break・
    `_SUBSEARCH_CELLS` docstring 参照）。どちらのセルにも無ければ `ModelNotAllowed`。

    `provider` 自体が `RESEARCH_PROVIDERS`（"ollama"/"openai"）に無い値なら、他の判定より先に
    `ModelNotAllowed` にする（`ExtResearchReq.provider` は pydantic の `Literal` で外部境界を
    422 にするが、本関数を直接呼ぶ経路もこれ1箇所で弾く・任意文字列がこの先の判定・メッセージへ
    素通りしない）。

    `model`・`provider` 両方省略時、`default_research_provider` が管理者設定の保存値を不正
    （`RESEARCH_PROVIDERS` に無い・非文字列）と判定して `ValueError` を送出した場合は
    `ProviderUnavailable`（503・未計測）へ変換する——PUT 側は既に不正値を 422 で拒否しているため、
    ここで検知するのは「保存後に何らかの経路で壊れた値」への防波堤。
    """
    if provider is not None and provider not in RESEARCH_PROVIDERS:
        raise ModelNotAllowed(
            f"許可されていない provider です: {provider!r}"
            f"（{'/'.join(sorted(RESEARCH_PROVIDERS))} のいずれかを指定してください）")
    if not model:
        if provider is not None:
            chosen = provider
        else:
            try:
                chosen = default_research_provider(system_settings)
            except ValueError:
                raise ProviderUnavailable(
                    "下調べ検索の既定 AI の設定に誤りがあります。管理者に設定を確認してください。",
                    llm_calls=0) from None
        usage = "subsearch"
        resolved = model_catalog.resolve_model(chosen, usage, None, system_settings=system_settings)
        if not resolved or not model_catalog.is_valid_model(chosen, usage, resolved,
                                                             system_settings=system_settings):
            raise ProviderUnavailable(
                f"下調べ検索の既定モデル（{chosen}/{usage}）が設定されていません。"
                "管理画面の「使えるモデル」で既定モデルを設定してください。",
                provider_used=chosen, llm_calls=0)
        return chosen, resolved
    if provider is not None:
        if model_catalog.is_valid_model(provider, "subsearch", model, system_settings=system_settings):
            return provider, model
        raise ModelNotAllowed(f"許可されていないモデルです: {model!r}"
                              f"（{provider} の下調べ検索用途では許可されていません。"
                              "管理画面の「使えるモデル」で許可してください）")
    for cell_provider, usage in _SUBSEARCH_CELLS:
        if model_catalog.is_valid_model(cell_provider, usage, model, system_settings=system_settings):
            return cell_provider, model
    raise ModelNotAllowed(f"許可されていないモデルです: {model!r}"
                          "（管理画面の「使えるモデル」の下調べ検索用途で許可してください）")


def _connect_openai(system_settings: dict) -> tuple[str, dict]:
    """送信前 fail-closed preflight。`strict=True` で鍵を解決する——`cloud_provider`（A7）が
    非空の不正値でも黙って既定 openai へ倒れたキーで実送信しない（`keys.
    InvalidCloudProviderConfigError` は 503 固定文言へ変換・利用者へは生の設定値を出さない）。
    鍵が解決できても `providers.openai_direct_block_reason`（「OpenAI 直結」共通 preflight・
    `usage_chat.py`・`_select_provider` と同じ関数）を必ず通す——真偽値だけの `if not key` 判定は
    `.env.example` のプレースホルダ（`sk-REPLACE_ME` 等）を「キーあり」と誤認し、Azure 等の接続先で
    用途別デプロイ名が無いまま送信して気付きにくい失敗になるのを防ぐ。`usage="subsearch"` を渡す
    ——本モジュールが実際に送信するのは下調べ検索（subsearch）用のモデルであり、既定の "chat" の
    ままだと chat セルにだけデプロイ名を設定した環境で subsearch セルの未設定を見逃す（`ext_api.py`
    がハンドラ入口で確定した用途とここでの preflight が食い違わないよう、常に同じ `usage` を使う・
    `system_extras.py::_assert_research_default_provider_sendable` の保存時 preflight も同じ）。
    ここで拒否すれば `_post` に到達しない＝`llm_calls` は加算されない（未計測のまま 503）。"""
    from . import providers as _providers
    try:
        key = keys.resolve_api_key("openai", None, system_settings=system_settings, strict=True)
    except keys.InvalidCloudProviderConfigError:
        raise ProviderUnavailable(
            f"下調べに使う AI（{_PROVIDER_DISPLAY_LABELS['openai']}）の設定が正しくありません。"
            "管理者に設定を確認してください。") from None
    reason = _providers.openai_direct_block_reason(key, system_settings, usage="subsearch")
    if reason is not None:
        raise ProviderUnavailable(reason)
    return llm.openai_url("chat/completions", system_settings=system_settings), \
        llm.openai_headers(key, system_settings=system_settings)


def _connect_ollama(system_settings: dict) -> tuple[str, dict]:
    url = keys.resolve_ollama_url(None, system_settings=system_settings)
    return llm.ollama_url(url.rstrip("/"), "/api/chat"), dict(llm.JSON_HEADERS)


def _truncate_preferring_used(entries: list, limit: int) -> tuple[list, list[str]]:
    """`max_results` 超過時、回答が実際に使った Evidence（`used=True`）を未使用分より優先して残す。

    先頭からの単純切り詰めだと、回答の根拠として実際に引用された ev-* が Packet・監査から
    消えうる。採否が決まった後の並び順は元の ev-* 採番順を保つ（used を先頭へ寄せない＝
    ev-1, ev-2, ... の通し番号としての読みやすさを壊さない）。

    `used=True` の件数自体が `limit` を超える場合、優先しても収まりきらない分は
    それでも切り捨てる——戻り値の第2要素 `dropped_used_ids`（切り捨てられた使用済み
    evidence_id の一覧）を呼び出し元が `remaining_gaps` へ注記できるようにする
    （優先しても消えることがある事実を利用者へ隠さない）。
    """
    if len(entries) <= limit:
        return entries, []
    used = [e for e in entries if e.get("used")]
    unused = [e for e in entries if not e.get("used")]
    keep_ids = {id(e) for e in (used + unused)[:limit]}
    dropped_used_ids = [e.get("evidence_id") for e in used if id(e) not in keep_ids]
    return [e for e in entries if id(e) in keep_ids], dropped_used_ids


def _sanitize_structural_evidence(structural_meta: list, world: str) -> list:
    """内部専用の Neo4j canonical_id（`ingest/world_graph._cid`＝`label:world:rel#name`・
    MIRROR-MODEL §2.1）を外部応答に出さない。

    `agentic_search._card_structural_evidence` は、裏付け doc を1件も主張しない graph card
    （純粋なグラフ位相情報）を `verification_method="graph_node_verified"` として構造 Evidence 化する
    際、`matched_doc_ids` に内部 cid（機械検証 ON 既定時）または `label:name`（明示 OFF 時）を
    そのまま入れる契約。ここでは `card_meta`（`_dedupe_structural_evidence` 通過後・
    `providers/base.py::_safe_card_meta` の allowlist を通す**前**の生の値・`label`/`path`/`name`
    を持つ）から `label:world:path` 形式（実 cid と同じ構成要素だが内部 canonical_id そのものでは
    ない）を組んで置き換える——一意性・追跡可能性（EXT-2）を保ちつつ内部 ID は漏らさない。

    `path`（`lens_service.neo4j_related` が返す `path_names`）は既にその対象ノード自身の名前を
    **末尾に含む**ため、`name` をここで再結合しない（末尾が重複した `ROOT/X/X` のような表現に
    ならないようにする）。`path` が空（本来 topology-only card では起きない想定だが防御的に）
    なら `name` へフォールバックする。裏付け doc があるカード（`verification_method=
    "graph_verified"`）は実 doc パスのみを持つため対象外。
    """
    out = []
    for m in structural_meta:
        if m.get("source_type") != "graph" or m.get("verification_method") != "graph_node_verified":
            out.append(m)
            continue
        cm = m.get("card_meta") or {}
        label = (cm.get("label") or "node").strip().lower() or "node"
        path = cm.get("path") or []
        path_str = "/".join(str(p) for p in path if p) or (cm.get("name") or "unknown")
        out.append({**m, "matched_doc_ids": [f"{label}:{world}:{path_str}"]})
    return out


def run_research(*, world: str, query: str, scope_paths: list | None, model: str | None,
                 max_iterations: int | None, max_results: int, timeout_s: int | None,
                 key_id: int, request_id: str | None = None,
                 system_settings: dict | None = None,
                 absolute_deadline: float | None = None,
                 provider: str | None = None) -> dict:
    """1回の下調べ検索を実行し、要約＋Evidence Packet（EXT-2）を返す。

    `agentic_search.openai_style`（Ollama/OpenAI 共用の tool-use ループ）を直接呼ぶ——チャットの
    main 経路（`providers/base.py::_GenProvider._agentic_run` の非 plan/非 hybrid 分岐）と同じ
    単発呼び出しの構え。

    `depth`（Depth/Cost/Verification Profile・EXT-5 未実装）は渡さない＝評価フェーズ（Research
    Cycle 境界の `submit_evaluation`）は発動しない（`agentic_search.openai_style` の既定 "light"
    のまま・チャットの既存呼び出し元と同じ未接続状態を踏襲する）。

    **帰属（EV-0・使った ev-N の申告）は重複排除で ev-N 採番がずれた場合だけやり直す**（拡張設計
    §4.4「回答確定後の追加呼び出しは1回」）: `openai_style` 自身が内部で行う帰属は、その時点の
    （重複排除**前**の）添字に対して行われる。研究サービス側の `_dedupe_citations_and_evidence`
    （重なる span の統合・完全重複の削除）・`_dedupe_structural_evidence` で件数が変わらなければ
    （＝重複/重なりが実際には無かった）ev-N の採番（位置ベース）は内部の帰属時点と同一のままの
    ため、`final.get("attributed_ev_ids")` をそのまま再利用する（追加の LLM 呼び出しをしない）。
    件数が変わった場合のみ、重複排除後の citations/構造 Evidence から digest を組み直し、
    `final.get("attribution_eligible")`（`openai_style` が自然完了 allowlist を満たしたと判定した
    場合のみ True）が立っている場合に限り、帰属呼び出しを1回追加で行う（LLM 呼び出しが1回増える・
    `llm_calls`/累積 usage に反映）——重複排除が実際に何も変えない大半のケースでは合計1回のまま
    （二重呼び出しの是正）。

    **digest 打ち切りと Packet の整合**（EXT-2 の追跡可能性）: `build_evidence_digest` は件数/
    バイト数上限で末尾を打ち切ることがある。Packet の `evidence[]` は実際に digest へ載った
    ev-N（`adopted_ev_ids`）だけに絞り、省略された件数は `remaining_gaps` に注記する
    （`providers/base.py::_omitted_evidence_gap_note` を再利用）。

    戻り値の `iterations` は実行した思考/ツール手順の可視ステップ数（`agentic_search` が yield する
    `node` イベント数）——課金相当の実 LLM 呼び出し回数（ツールターン＋再合成＋帰属呼び出しの合計）
    は別途 `llm_calls` で返す（両者は一致しない）。

    例外: `ModelNotAllowed`（400 相当）／`InvalidScope`（422 相当・pinned root での authoritative
    scope_paths 再検証に失敗）／`ProviderUnavailable`（503 相当・鍵未設定/接続失敗/カタログ設定
    不備/ロック競合/world・設定確認不能/最終合成の通信失敗のいずれも含む・フォールバックしない）／
    `ResearchTimeout`（504 相当・`timeout_s` 超過——ロック競合による 503 を除き、通信中の例外の
    種別を問わずデッドライン超過を優先する）。いずれも `model_used`/`provider_used`/`llm_calls`
    （解決/計測できた範囲で）を属性として持つ——失敗時も監査へ「どこまで実行してから失敗したか」を
    残すため。失敗までに実際に発行した LLM 呼び出し分の usage は、成功時と同じ `metering.record`
    へ記録する。

    `absolute_deadline`（省略可・`time.monotonic()` 系の絶対値）: 指定時は `timeout_s` から
    再計算せずこの値をそのまま絶対期限として使う。呼び出し元（`ext_api.ext_research`）は preflight
    を含む「リクエスト全体の絶対期限」をハンドラ入口で確定済みのため、それをそのまま渡す——
    `timeout_s`（残り秒数）へ一度変換してから本関数が改めて `time.monotonic() + timeout_s` で
    絶対期限を作り直すと、(1) 整数秒への切り上げ（最大 1 秒未満の水増し）と (2) 変換〜本関数の
    呼び出しに実際にかかる僅かな時間の両方が積み重なり、元の絶対期限を最大で約1秒超えてから
    200 を返しうる。省略時（テスト等の直接呼び出し）は従来どおり `timeout_s`（省略時は
    既定値）から本関数の入口時刻を基準に計算する。`deadline_s`（メッセージ表示用）は常に
    `timeout_s` 基準のまま（`absolute_deadline` 指定時も呼び出し元は表示に意味のある値
    ＝リクエスト全体の元の timeout_s をそのまま渡す契約）。

    `provider`（省略可・"ollama"/"openai"）: 指定時はこの provider に固定して解決する
    （`resolve_model_and_provider` へそのまま渡す）。省略時は管理者設定「外部連携」タブの既定
    プロバイダ（未設定なら "ollama"）を使う——`resolve_model_and_provider` docstring 参照。
    """
    # 引数 `provider` は下の `resolve_model_and_provider` 呼び出し1箇所でしか使わない——直後で
    # 同名のローカル変数 `provider`（解決済みプロバイダの追跡用・関数全体で参照）を None 初期化
    # して上書きするため、呼び出しまでの間だけ別名で保持する。
    _requested_provider = provider
    absolute_deadline = absolute_deadline if absolute_deadline is not None else (
        time.monotonic() + (timeout_s if timeout_s is not None else _default_timeout_s()))
    deadline_s = timeout_s if timeout_s is not None else _default_timeout_s()
    # 関数内の複数の except 節から使う（モジュール全体では使わないため、他の `graph_extract`
    # 呼び出し元＝`health.py` 等と同じくローカル import に留める）。
    from .ingest.graph_extract import _log_masked_exception

    def _remaining() -> float:
        return absolute_deadline - time.monotonic()

    turns = _MAX_ITERATIONS_CEILING if max_iterations is None else max_iterations
    turns = max(1, min(turns, agentic_search.MAX_TURNS, _MAX_ITERATIONS_CEILING))

    usage_acc = {"calls": 0, "tokens": None}
    node_count = 0
    final: dict | None = None
    stop_event = threading.Event()
    timer: threading.Timer | None = None
    provider: str | None = None
    mod: str | None = None
    resolved_secret: str | None = None   # 予期しない例外のログをマスクする際に使う実キー（openai/azure）
    error: "_ResearchError | None" = None
    success_result: dict | None = None   # ロック解放・metering の後で最終デッドライン確認してから返す
    lock_contention = False   # ロック競合由来のエラーはデッドライン優先の再分類から除外する

    def _turn_timeout() -> int:
        remaining = _remaining()
        if remaining <= 0:
            # 既に期限切れ。`agentic_search.openai_style` の callable timeout 契約は「秒数を
            # 返す」だけで「送信するな」を伝える経路が無いため、この直前の1回を完全には止められ
            # ない（呼び出し元は既に `_send(..., timeout=_resolve_timeout(timeout))` の引数評価
            # 段階＝実送信の直前まで進んでいる）。代わりに `stop_event` を前倒しで発火させ、
            # `threading.Timer`（絶対期限ちょうどに発火・OS スケジューリングの遅延を持ちうる）を
            # 待たずに、以後のターン・tail の再合成/最終合成（本モジュールが送信直前で
            # stop_event を再確認する箇所）を確実に止める——「期限後に何回も課金/待機し続ける」
            # ことを防ぐ（1回分の下振れだけに抑える）。
            stop_event.set()
        return max(1, min(remaining, _MAX_PER_CALL_TIMEOUT_S))

    try:
        try:
            sys_s = system_settings if system_settings is not None else store.get_system_settings(
                connect_timeout=_remaining(), statement_timeout_ms=max(1, int(_remaining() * 1000)))
        except Exception as e:
            _log_masked_exception(_log, "research: 設定情報の取得に失敗", e)
            raise ProviderUnavailable(
                "設定情報を確認できませんでした（一時的な障害の可能性があります）",
                llm_calls=0) from e

        provider, mod = resolve_model_and_provider(model, sys_s, _requested_provider)   # 呼び出しゼロ（失敗時も記録不要）

        try:
            with world_lock_shared(world, timeout_ms=max(1, int(_remaining() * 1000)),
                                   connect_timeout=max(0.5, _remaining())):
                # rebind（`sherpa.ingest.worker` の排他 world_lock）とはここで直列化される——
                # このロックを取った後に解決する root は rebind と競合しない。呼び出し側
                # （`ext_api.py`）の preflight 解決はロック**前**の値のため、ここでは使わず
                # 自前で再解決する（TOCTOU 対策・ロック順序: 先に共有ロック→その内側で解決）。
                try:
                    resolution = worlds.resolve_external_world(
                        world, connect_timeout=_remaining(),
                        statement_timeout_ms=max(1, int(_remaining() * 1000)))
                except worlds.ExternalResolverError as e:
                    _log_masked_exception(_log, "research: world resolver 到達不可", e)
                    raise ProviderUnavailable(
                        "資料フォルダの参照先を確認できませんでした（一時的な障害の可能性があります）"
                    ) from e
                if resolution.status != "ok":
                    raise ProviderUnavailable(
                        "資料フォルダ（world）を確認できませんでした（取り込み中に変更された可能性があります）")

                with worlds.pin_world_root(world, resolution.path):
                    # scope_paths も pinned root で authoritative に再検証する（preflight は
                    # ロック**前**の root を見ているため、取り込み中の変化を拾えないことがある）。
                    # ここで単純に `_remaining()<=0` を見て `ResearchTimeout` へ倒しても不十分
                    # ——このスコープを抜ける際に共有ロックの解放（DB 往復）が走り、その
                    # 「解放中」に期限を越えることがあるため、raise 時点のチェックだけでは
                    # 間に合わない。`InvalidScope` は単に raise するだけにし、末尾の共通デッドライン
                    # 判定（ロック解放・metering の**後**に `_remaining()` を確認する箇所）へ通す。
                    try:
                        if not scope_mod.valid_scope_paths(world, scope_paths, root=resolution.path,
                                                           strict=True, deadline=absolute_deadline):
                            raise InvalidScope("不明な範囲（scope_paths）が指定されました")
                    except OSError as e:
                        _log_masked_exception(_log, "research: scope_paths 走査中の OSError", e)
                        raise ProviderUnavailable(
                            "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）"
                        ) from e
                    # `ScopeWalkDeadlineExceeded`（`deadline` 超過）は OSError ではないためここでは
                    # 捕捉しない——`except Exception as e:`（下）が拾い、末尾の共通デッドライン判定
                    # （既にデッドライン超過済みのため必ず該当する）が `ResearchTimeout` へ倒す。

                    if provider == "openai":
                        endpoint, headers = _connect_openai(sys_s)
                        resolved_secret = keys.resolve_api_key("openai", None, system_settings=sys_s)
                    else:
                        endpoint, headers = _connect_ollama(sys_s)

                    if _remaining() <= 0:
                        raise ResearchTimeout(
                            f"調査が制限時間（{deadline_s}秒）内に完了しませんでした"
                            "（world の確認・ロック待ちで時間を使い切りました）")
                    timer = threading.Timer(max(0.01, _remaining()), stop_event.set)
                    timer.daemon = True
                    timer.start()
                    try:
                        for ev in agentic_search.openai_style(
                                endpoint, headers, mod, agentic_search.SYSTEM, query, world,
                                scope_paths, ollama=(provider == "ollama"), can_ask=False,
                                max_turns=turns, timeout=_turn_timeout, usage_acc=usage_acc,
                                stop_event=stop_event, tool_deadline=absolute_deadline):
                            if "node" in ev:
                                # TRACE-HITS の結果ノード（event_type="tool_completed"・件数表示の
                                # 表示専用サガー）は数えない——数えると1ツール実行が2ステップに
                                # 見え、外部クライアントの iterations 指標が経年比較できなくなる
                                # （§8.1 の「可視ステップ数」＝操作の数を維持する）。
                                if ev["node"].get("event_type") != "tool_completed":
                                    node_count += 1
                            if "final" in ev:
                                final = ev
                    finally:
                        timer.cancel()

                    if final is None:
                        if stop_event.is_set() or _remaining() <= 0:
                            raise ResearchTimeout(
                                f"調査が制限時間（{deadline_s}秒）内に完了しませんでした")
                        raise ProviderUnavailable(
                            "agentic search がタスクを完了できませんでした（応答なし）")
                    if final.get("synthesis_failed"):
                        # `failure_kind=="connection"`（`agentic_search` が最終合成/再合成の例外を
                        # 判定済み）だけ provider 付き専用文言。それ以外（配信は成功したが応答が
                        # 異常・content_filter 等）は従来の汎用文言のまま。
                        if final.get("failure_kind") == "connection":
                            raise ProviderUnavailable(_connection_unavailable_message(provider))
                        raise ProviderUnavailable(
                            "回答の合成中にAIプロバイダとの通信に失敗しました。時間をおいて再試行してください。")

                    orig_cites = final.get("cites") or []
                    orig_structural = final.get("structural_evidence_meta") or []
                    cites, evidence_meta, merge_dropped = _provider_base._dedupe_citations_and_evidence(
                        orig_cites, final.get("evidence_meta") or [], world)
                    dropped_citations = (final.get("dropped_citations") or []) + merge_dropped
                    structural_evidence_meta = _provider_base._dedupe_structural_evidence(orig_structural)
                    structural_evidence_meta = _sanitize_structural_evidence(structural_evidence_meta, world)
                    has_structural_evidence = (final.get("has_structural_evidence", False)
                                              or bool(structural_evidence_meta))
                    combined_evidence_meta = evidence_meta + structural_evidence_meta
                    # 重複排除・重なり統合・統合span再検証はいずれも件数を減らす方向にしか働かない
                    # （新規追加・並べ替えはしない）——件数が変わっていなければ、内部の帰属が使った
                    # ev-N 採番（位置ベース）と完全に一致したままだと判定できる（docstring 参照）。
                    dedup_unchanged = (len(cites) == len(orig_cites)
                                      and len(structural_evidence_meta) == len(orig_structural))

                    digest, ev_map = "", {}
                    if combined_evidence_meta:
                        digest, ev_map = agentic_search.build_evidence_digest(cites, combined_evidence_meta)
                    adopted_ev_ids = set(ev_map.keys())

                    answer_text = final.get("final") or ""
                    if not answer_text and final.get("attribution_eligible"):
                        # `attribution_eligible` は「この回答本文を生成した呼び出しの finish_reason
                        # が自然完了（"stop"）allowlist に入っていた」ことを示す（`agentic_search.
                        # openai_style` の `_eligible` 参照・budget_exceeded 等の意図的な空回答
                        # 経路はこの時点まで到達せず早期 return するためここには来ない）。自然完了
                        # したのに本文が空＝実質的な合成失敗（モデルが空応答を返した）であり、
                        # `synthesis_failed`（通信失敗）と同じ扱いにする——空の `answer` で黙って
                        # 200 を返さない。
                        raise ProviderUnavailable(
                            "回答の合成中にAIプロバイダから空の応答が返されました。"
                            "時間をおいて再試行してください。")
                    attributed_ev_ids: set = set()
                    remaining_for_attr = _remaining()
                    if dedup_unchanged:
                        # ev-N 採番が内部の帰属時点と変わっていない——`openai_style` が既に1回
                        # 発行した帰属呼び出しの結果をそのまま再利用し、同じ目的の呼び出しを
                        # 二重に発行しない（拡張設計 §4.4・docstring 参照）。
                        attributed_ev_ids = set(final.get("attributed_ev_ids") or set())
                    elif (final.get("attribution_eligible") and answer_text and digest and ev_map
                            and remaining_for_attr > 1):
                        # 主ループの累計 usage（トークン）へ合算する（ゼロから始めない）——
                        # `attribute_openai_style` は渡された `usage` dict に加算し、その
                        # 合計を `usage_acc["tokens"]` へ上書きする契約のため、空の dict を
                        # 渡すと主ループ分のトークンが失われる。
                        merged_usage = dict(final.get("usage") or _ZERO_USAGE)
                        attr_timeout = max(1, min(remaining_for_attr, _MAX_PER_CALL_TIMEOUT_S))
                        attributed_ev_ids = agentic_search.attribute_openai_style(
                            endpoint, headers, mod, provider == "ollama",
                            agentic_search._redact(answer_text), digest, ev_map,
                            attr_timeout, usage=merged_usage, usage_acc=usage_acc)

                    evidence_entries = _provider_base._evidence_packet_evidence(
                        combined_evidence_meta, attributed_ev_ids, adopted_ev_ids)
                    # 切り詰め前の「実際に使った ev-* の全集合」（監査の ev_ids 用・§8.4）——
                    # 切り詰め後の Packet からだけ拾うと、max_results 上限で漏れた使用済み分が
                    # 監査からも消えてしまう。
                    used_ev_ids = [e["evidence_id"] for e in evidence_entries if e.get("used")]
                    evidence_entries, dropped_used_ev_ids = _truncate_preferring_used(
                        evidence_entries, max_results)

                    investigation_status = "sufficient" if (cites or has_structural_evidence) else "insufficient"
                    remaining_gaps = [f"{d.get('doc_id')} ({d.get('reason')})" for d in dropped_citations]
                    remaining_gaps += _provider_base._omitted_evidence_gap_note(
                        combined_evidence_meta, adopted_ev_ids)
                    if dropped_used_ev_ids:
                        remaining_gaps.append(
                            f"使用済み Evidence {len(dropped_used_ev_ids)} 件が max_results 上限で"
                            f"省略されました（{', '.join(dropped_used_ev_ids)}）")
                    packet = citations.build_evidence_packet(
                        task_id=research_task_id(request_id), investigation_status=investigation_status,
                        summary="", evidence=evidence_entries, remaining_gaps=remaining_gaps,
                        candidates_seen=len(evidence_meta) + len(structural_evidence_meta) + len(dropped_citations),
                        candidates_inspected=len(final.get("docs") or []),
                        evidence_selected=len(evidence_entries),
                        stop_reason=final.get("stop_reason") or "unknown")

                    if _remaining() <= 0:   # 早期の中間確認（結果組み立て自体が長引いた場合の fast path）。
                        raise ResearchTimeout(                       # 権威ある最終確認は finally（ロック解放・
                            f"調査が制限時間（{deadline_s}秒）内に完了しませんでした（結果組み立て後）")

                    # ここでは return しない——このスコープを抜けると共有ロックの解放（DB 往復）が
                    # 走り、その後 finally で metering.record（`_METERING_DB_TIMEOUT_S` で bound
                    # された固定予算の DB 接続）も走る。どちらも即座には終わらないため、この時点の
                    # 確認だけでは「期限を越えた 200」を防げない。結果は保持だけして、共有ロック
                    # 解放・metering が終わった後（関数末尾）で改めて残り時間を確認してから返す。
                    success_result = {"world": world, "query": query, "answer": answer_text,
                                      "evidence_packet": packet, "model_used": mod,
                                      "provider_used": provider, "iterations": node_count,
                                      "llm_calls": usage_acc.get("calls", 0),
                                      # 監査専用（`ExtResearchRes` には含まれず応答へは出ない）:
                                      # max_results 切り詰めの影響を受けない「実際に使った ev-*
                                      # の全集合」。`ext_api.py` の監査 ev_ids はこれを使う。
                                      "used_ev_ids": used_ev_ids}
        except psycopg.errors.LockNotAvailable as e:
            lock_contention = True
            _log_masked_exception(_log, "research: 共有ロック競合", e)
            raise ProviderUnavailable(
                "資料フォルダの更新処理と競合しています。しばらくしてから再試行してください。") from e
    except _ResearchError as e:
        # `ModelNotAllowed`/`InvalidScope` もここに含む（両方とも `_ResearchError` を継承）。
        # `ModelNotAllowed` はロック取得より**前**（`resolve_model_and_provider`）にしか起きない
        # ため共有ロック解放は経由しないが、それより前段の設定読取（`store.get_system_settings()`）
        # 自体が長引いて既にデッドラインを超えていることがあり得るため、即座に 400 を確定させず
        # ここへ合流させる。`InvalidScope` は共有ロックを保持した状態から raise されうるため、
        # このスコープを抜ける際のロック解放（DB 往復）中に期限を越えることがある。どちらも
        # 即座に再送出せず一度 `error` へ保持し、下の共通デッドライン判定（ロック解放・metering
        # の**後**に `_remaining()` を確認する）へ通す（期限超過後に 400/422 を返さないため）。
        error = e
    except Exception as e:
        # 外部向け detail に生の例外文字列を使わない——改行等を含む不正なキー値が urllib/
        # http.client の例外メッセージへ Authorization/api-key ヘッダ値ごとエコーされることがある
        # （上流ライブラリの実装依存で発生しうる）。そのまま `ProviderUnavailable` のメッセージ
        # （= ext_api.py がそのまま外部 503 の detail にする）へ載せると秘密情報が外部
        # レスポンスへ漏れる。外部へは固定文言だけを返し、原因は `_log_masked_exception`
        # （`resolved_secret`・openai/azure を渡す——api-key ヘッダ方式は接頭辞なしで値そのものが
        # ヘッダになるため、汎用パターンだけでは捕まらないことがある）でマスクしてから
        # サーバーログにのみ残す（生の例外オブジェクトはログへ出さない）。
        _log_masked_exception(_log, "research: 予期しない例外", e, resolved_secret)
        # 接続失敗判定は LLM 送信由来の例外だけに適用する——本 except は
        # `agentic_search.openai_style()` 呼び出し区間全体（ツール実行含む）を1つで受けるため、
        # grep 等のファイル I/O 障害（SMB/NFS 切断の `ConnectionResetError` 等）が
        # `_is_connection_failure` と偶然同じ型を持つ場合に「AI に接続できません」と誤報しうる。
        # `_send`（agentic_search.py）が物理送信の例外にだけ付与する `_sherpa_llm_send_error`
        # マーカーを併用し、マーカーが無い例外は型が一致してもこの分岐に入れない。
        if getattr(e, "_sherpa_llm_send_error", False) and agentic_search._is_connection_failure(e):
            error = ProviderUnavailable(_connection_unavailable_message(provider))
        else:
            error = ProviderUnavailable(
                "AIプロバイダとの通信で予期しないエラーが発生しました。時間をおいて再試行してください。")
        # __cause__ は raw e のまま保持しない（明示的に None のまま）——server 側の記録は上の
        # マスク済みログで十分であり、`error` がこの後 HTTP 応答経路（ASGI ミドルウェアの
        # delivery-failure ログ等）で traceback 化されると、Python の traceback formatter は
        # __cause__ チェーンを無条件に辿って表示するため、それを raw e に繋いだままだと秘密
        # （マスク前のメッセージ）がサーバーログへ別経路で再度出力されてしまう。
    finally:
        # 成否に関わらず、実際に発行した分のコストは記録する（§8.3・失敗時も含む）。この時点では
        # 残り時間（`_remaining()`）が既に尽きていることがあるため、残り時間ベースではなく
        # `_METERING_DB_TIMEOUT_S`（固定・小さい）で bound する——「記録は試みるが無期限には
        # ブロックしない」契約（失敗時はマスク付きで `metering.record` 内部がログする）。
        llm_calls = usage_acc.get("calls", 0)
        if llm_calls > 0:
            metering.record("research", provider, mod, usage_acc.get("tokens"),
                            user_id=f"ext:{key_id}", world=world, calls=llm_calls,
                            connect_timeout=_METERING_DB_TIMEOUT_S,
                            statement_timeout_ms=_METERING_DB_TIMEOUT_S * 1000)

    # 通信中に発生した例外の種別を問わず、既にデッドラインを超えていれば 504 を優先する
    # （503 化して「通信障害」と誤って報告しない）。ロック競合（503・honest busy signal）だけは
    # 例外扱いにする——lock_timeout 自体が残り時間ベースのため、ここで再分類すると常に 504化
    # されてしまい「busy」というシグナルが失われる。
    llm_calls = usage_acc.get("calls", 0)
    if error is None:
        # 成功パス（例外なし）。共有ロック解放・上の finally の metering.record が終わった
        # **後**の、この関数で最後となる残り時間確認——ここを通って初めて 200 を返す
        # （黙った期限超過 200 を出さない・§8 の契約）。
        if success_result is not None and _remaining() > 0:
            return success_result
        error = ResearchTimeout(
            f"調査が制限時間（{deadline_s}秒）内に完了しませんでした（結果返却直前）")
    elif not isinstance(error, ResearchTimeout) and not lock_contention and _remaining() <= 0:
        reclassified = ResearchTimeout(f"調査が制限時間（{deadline_s}秒）内に完了しませんでした")
        reclassified.__cause__ = error
        error = reclassified
    error.model_used = error.model_used or mod
    error.provider_used = error.provider_used or provider
    error.llm_calls = llm_calls if error.llm_calls is None else error.llm_calls
    raise error
