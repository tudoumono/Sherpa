"""簡易チャット。読み取り部品（`tool_dispatch.run_tool`）を道具として LLM に数回使わせて 1 回答える薄いループ。
設計: docs/design/simple.md「道具ループ（`simple_chat.iter_tool_loop`）」／docs/design/interfaces.md「簡易チャットの同期応答」

- 往復（LLM とのラウンドトリップ）は最大 `MAX_ROUND_TRIPS` 回、1 往復あたりの道具呼び出しは `MAX_TOOL_CALLS_PER_ROUND` 回まで（内部固定・利用者には見せない）。
  往復が尽きても回答が確定しない、または出典を 1 件も実在確認できなかった場合は `unconfirmed=True` を返す。
- 権限は持たない: world・scope_paths は呼び出し側（`ext_api.ext_answer`）が解決・検証して渡し、ここでは再解決・ロック取得をしない。
- 使う AI は管理者設定の既定 AI（`system_settings.research_default_provider`）。`model`/`provider` は利用者から受け取らない。
  対応プロバイダは ollama/openai のみ。LLM への HTTP 呼び出しは `agentic_search._post` を使う。
- 道具は `es_search`・`graph_neighbors`・`ripgrep_search`・`read_around`（定義は `agentic_search` を再利用・ES/グラフは `tool_availability()` に従い出し分け）。
- 出典（`sources`）は、道具呼び出しで触れた doc_id のうち `parts.read.tools.verify_doc_exists` で実在を確かめられたものだけ。
  1 件も無ければ `unconfirmed=True`（`unconfirmed_reason="no_verified_sources"`）。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from typing import Iterator

from . import agentic_search, keys, llm, metering, model_catalog, store
from .parts.read.tools import verify_doc_exists
from .tool_dispatch import run_tool

_log = logging.getLogger("sherpa")

# 往復の上限（利用者に見せる設定は無い）。
MAX_ROUND_TRIPS = 3
MAX_TOOL_CALLS_PER_ROUND = 4

# 既定 AI の解決（`resolve_model_and_provider`・`default_research_provider`・`_connect_openai`・`_connect_ollama`）。

# 用途 subsearch を持つ (provider, usage) の組。model 指定・provider 省略時に両カタログへ載る曖昧なケースは、先頭（Ollama）を優先する。
_SUBSEARCH_CELLS = (("ollama", "subsearch"), ("openai", "subsearch"))

# system_settings の 1 キー（外部連携タブ「外部からの簡易回答に使う AI」）。未設定（キー無し／None／空文字）のみ "ollama" へフォールバックし、不正な値は `ValueError`（`default_research_provider`）。キー名は変えない。
_RESEARCH_PROVIDER_SETTINGS_KEY = "research_default_provider"
# 公開定数。`resolve_model_and_provider` の provider 検証にも使う。
RESEARCH_PROVIDERS = frozenset(p for p, _ in _SUBSEARCH_CELLS)

# provider コード → 管理画面 UI と同じ表示名（外部応答メッセージに生のコードを出さない）。
_PROVIDER_DISPLAY_LABELS = {"ollama": "ローカル（Ollama）", "openai": "クラウド（OpenAI）"}


class _LLMResolveError(RuntimeError):
    """既定 AI の解決中に起きた設定不備・許可リスト外の指定。`_resolve_llm` が捕捉して `LLMUnavailable` へ丸める。"""


class ModelNotAllowed(_LLMResolveError):
    """`model`/`provider` が管理者カタログ（用途 subsearch）の許可リスト・`RESEARCH_PROVIDERS` のいずれでもない。"""


class ProviderUnavailable(_LLMResolveError):
    """選択されたプロバイダに接続できない、または管理者カタログの設定不備で解決できない。"""


def default_research_provider(system_settings: dict | None) -> str:
    """既定 AI（`model`/`provider` 両方省略時）の既定プロバイダ。
    `system_settings.research_default_provider` が最優先、未設定（キー無し／`None`／空文字）は "ollama"。
    設定されているのに `RESEARCH_PROVIDERS` に無い値は `ValueError` を送出する（黙って別のプロバイダで実行しない）。
    `resolve_model_and_provider` は捕捉して `ProviderUnavailable` に変換し、`system_extras._admin_settings_view` は `"(不正な保存値)"` に畳む。
    """
    value = (system_settings or {}).get(_RESEARCH_PROVIDER_SETTINGS_KEY)
    if value is None or value == "":
        return "ollama"
    if not isinstance(value, str) or value not in RESEARCH_PROVIDERS:
        raise ValueError(
            "research_default_provider の保存値が不正です（ollama/openai のいずれでもありません）")
    return value


def resolve_model_and_provider(model: str | None, system_settings: dict,
                               provider: str | None = None) -> tuple[str, str]:
    """`model`（省略可）・`provider`（省略可・"ollama"/"openai"）→ `(provider, model)`。
    - `provider` が `RESEARCH_PROVIDERS` に無ければ最初に `ModelNotAllowed`。
    - `model` 省略: provider は指定値／省略時は `default_research_provider`。`model_catalog.resolve_model(provider,"subsearch",...)` の解決値が同じセルの `is_valid_model` を通らなければ `ProviderUnavailable`。
      `default_research_provider` の `ValueError` も `ProviderUnavailable`（503）にする。
    - `model` 指定・`provider` 指定: その provider のセルだけで allowed 判定し、許可されていなければ `ModelNotAllowed`。
    - `model` 指定・`provider` 省略: `_SUBSEARCH_CELLS` の順に判定し、最初に一致した provider を採用。どちらにも無ければ `ModelNotAllowed`。
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
                    "外部からの簡易回答の既定 AI の設定に誤りがあります。管理者に設定を確認して"
                    "ください。") from None
        usage = "subsearch"
        resolved = model_catalog.resolve_model(chosen, usage, None, system_settings=system_settings)
        if not resolved or not model_catalog.is_valid_model(chosen, usage, resolved,
                                                             system_settings=system_settings):
            raise ProviderUnavailable(
                f"外部からの簡易回答の既定モデル（{chosen}/{usage}）が設定されていません。"
                "管理画面の「使えるモデル」で既定モデルを設定してください。")
        return chosen, resolved
    if provider is not None:
        if model_catalog.is_valid_model(provider, "subsearch", model, system_settings=system_settings):
            return provider, model
        raise ModelNotAllowed(f"許可されていないモデルです: {model!r}"
                              f"（{provider} の外部からの簡易回答用途では許可されていません。"
                              "管理画面の「使えるモデル」で許可してください）")
    for cell_provider, usage in _SUBSEARCH_CELLS:
        if model_catalog.is_valid_model(cell_provider, usage, model, system_settings=system_settings):
            return cell_provider, model
    raise ModelNotAllowed(f"許可されていないモデルです: {model!r}"
                          "（管理画面の「使えるモデル」の外部からの簡易回答用途で許可してください）")


def _connect_openai(system_settings: dict) -> tuple[str, dict]:
    """送信前の fail-closed preflight。`strict=True` で鍵を解決する（`cloud_provider` が非空の不正値でも既定 openai の鍵で送らない・`keys.InvalidCloudProviderConfigError` は 503 固定文言へ変換）。
    鍵が解決できても `providers.openai_direct_block_reason`（`_select_provider` と同じ preflight）を通す（プレースホルダ鍵を「キーあり」と誤認しない）。
    `usage="subsearch"` を渡す。拒否すれば `_post` に到達しない（未送信のまま 503）。
    """
    from . import providers as _providers
    try:
        key = keys.resolve_api_key("openai", None, system_settings=system_settings, strict=True)
    except keys.InvalidCloudProviderConfigError:
        raise ProviderUnavailable(
            f"外部からの簡易回答に使う AI（{_PROVIDER_DISPLAY_LABELS['openai']}）の設定が正しく"
            "ありません。管理者に設定を確認してください。") from None
    reason = _providers.openai_direct_block_reason(key, system_settings, usage="subsearch")
    if reason is not None:
        raise ProviderUnavailable(reason)
    return llm.openai_url("chat/completions", system_settings=system_settings), \
        llm.openai_headers(key, system_settings=system_settings)


def _connect_ollama(system_settings: dict) -> tuple[str, dict]:
    url = keys.resolve_ollama_url(None, system_settings=system_settings)
    return llm.ollama_url(url.rstrip("/"), "/api/chat", system_settings=system_settings), dict(llm.JSON_HEADERS)


# 1 回の LLM 呼び出し（HTTP リクエスト 1 本）の上限秒数。残り時間がこれより短ければそちらを使う。
_MAX_PER_CALL_TIMEOUT_S = 90

_SYSTEM_PROMPT = (
    "あなたは社内資料の簡易検索アシスタントです。"
    "提供された道具（資料の全文/日本語検索・関係グラフをたどる・資料をそのまま検索・該当箇所を精読）"
    "だけを使って社内資料を調べ、実際に確認できた内容だけを根拠に日本語で簡潔に答えてください。"
    "資料から確認できないことは推測で埋めず、分からない旨を正直に答えてください。"
    "呼び出せる回数に上限があるため、早めに絞り込み、確認が済み次第すぐに回答してください。"
)

# `agentic_search` の既存ツール定義（説明・JSON スキーマ）を再利用する。
_TOOL_DEFS = {
    "es_search": (agentic_search._DESC_ES, agentic_search._PARAMS_ES_SEARCH),
    "graph_neighbors": (agentic_search._DESC_GRAPH, agentic_search._PARAMS_GRAPH),
    "ripgrep_search": (agentic_search._DESC_SEARCH, agentic_search._PARAMS_SEARCH),
    "read_around": (agentic_search._DESC_READ, agentic_search._PARAMS_READ),
}


class LLMUnavailable(RuntimeError):
    """既定の AI が未設定・未接続、または送信に失敗した（503 相当）。"""


class AnswerTimeout(RuntimeError):
    """リクエスト全体の絶対期限（`absolute_deadline`）を超過した（504 相当）。"""


def _safe_json(s):
    """tool_calls の `arguments`（JSON 文字列、または既に dict）を安全に解析する。"""
    try:
        return json.loads(s) if isinstance(s, str) else (s or {})
    except (ValueError, TypeError):
        return {}


def _build_tools(availability: dict) -> list:
    """可用性（`agentic_search.tool_availability()`）で es_search/graph_neighbors を出し分ける。grep・read_around は常に出す。"""
    names = []
    if availability.get("fulltext"):
        names.append("es_search")
    if availability.get("graph"):
        names.append("graph_neighbors")
    names += ["ripgrep_search", "read_around"]
    return [{"type": "function", "function": {"name": n, "description": _TOOL_DEFS[n][0],
                                              "parameters": _TOOL_DEFS[n][1]}} for n in names]


def _locator_from_span(span) -> str | None:
    if not isinstance(span, (list, tuple)) or len(span) != 2:
        return None
    start, end = span
    if start is None:
        return None
    return str(start) if end in (None, start) else f"{start}-{end}"


def _resolve_llm(system_settings: dict) -> tuple[str, str, str, dict]:
    """既定 AI の (provider, model, endpoint, headers)。対応は ollama/openai のみ。
    `_LLMResolveError` は全て `LLMUnavailable` へ丸める。
    """
    try:
        provider, model = resolve_model_and_provider(None, system_settings, None)
        if provider == "openai":
            endpoint, headers = _connect_openai(system_settings)
        else:
            endpoint, headers = _connect_ollama(system_settings)
    except _LLMResolveError as e:
        raise LLMUnavailable(str(e)) from e
    except llm.SsrfBlocked as e:  # 接続先が許可されていない＝既定 AI が使えない（503）
        raise LLMUnavailable("既定の AI の接続先が許可されていません") from e
    return provider, model, endpoint, headers


@dataclass
class LoopState:
    """`iter_tool_loop` が更新する実行状態。呼び出し側が例外・中断の後でも計測や出典確認に使えるよう、外から渡す可変オブジェクト。"""
    touched_docs: list = field(default_factory=list)
    cites: list = field(default_factory=list)
    tool_calls_used: int = 0
    llm_calls: int = 0
    usage_acc: dict = field(default_factory=agentic_search._new_usage_acc)
    final_text: str | None = None
    hit_round_trip_limit: bool = False
    stopped: bool = False


def _remaining_s(absolute_deadline: float | None) -> float:
    return (absolute_deadline - time.monotonic()) if absolute_deadline is not None else float("inf")


def _check_deadline(absolute_deadline: float | None) -> float:
    remaining = _remaining_s(absolute_deadline)
    if remaining <= 0:
        raise AnswerTimeout("回答の作成が制限時間内に完了しませんでした")
    return remaining


def iter_tool_loop(state: LoopState, *, provider: str, model: str, endpoint: str, headers: dict,
                   system_prompt: str, query: str, world: str, scope_paths: list | None,
                   history: list | None = None, stop_event=None, extra_context: str = "",
                   availability: dict | None = None, layer=None,
                   absolute_deadline: float | None = None) -> Iterator[dict]:
    """道具ループの本体（`answer()` とチャットの簡易 provider が共有する）。
    道具を 1 件実行するたびに `{"phase": "start"|"done", "name", "args", ("result", "docs")}` を yield し、結果は `state` に積む。
    `stop_event` が立ったら次の LLM 呼び出し・道具実行の前で `state.stopped=True` にして終える。
    期限超過は `AnswerTimeout`、送信失敗は `LLMUnavailable`。`history`（直前までの user/assistant 完全対）は system と今回の質問の間に置く。
    """
    def _per_call_timeout() -> float:
        return min(_check_deadline(absolute_deadline), _MAX_PER_CALL_TIMEOUT_S)

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    tools = _build_tools(availability if availability is not None else agentic_search.tool_availability())
    offered_names = frozenset(_TOOL_DEFS.keys()) & {t["function"]["name"] for t in tools}
    system = system_prompt + (("\n\n" + extra_context) if extra_context else "")
    msgs = [{"role": "system", "content": system}, *(history or []),
            {"role": "user", "content": query}]
    seen_docs = set(state.touched_docs)

    for _round in range(MAX_ROUND_TRIPS):
        if _stopped():
            state.stopped = True
            return
        body = {"model": model, "messages": msgs, "tools": tools}
        if provider == "ollama":
            body["stream"] = False
            body["options"] = {"temperature": 0.2}
        try:
            resp = agentic_search._post(endpoint, headers, body, timeout=_per_call_timeout())
        except AnswerTimeout:
            raise
        except Exception as e:
            from .ingest.graph_extract import _log_masked_exception
            _log_masked_exception(_log, "simple_chat: LLM 送信に失敗", e)
            if _remaining_s(absolute_deadline) <= 0:
                raise AnswerTimeout("回答の作成が制限時間内に完了しませんでした") from e
            raise LLMUnavailable("既定の AI に接続できませんでした。管理者に設定を確認してください。") from e
        state.llm_calls += 1
        agentic_search._acc_openai_usage(state.usage_acc, resp, provider == "ollama")
        msg = ((resp.get("choices") or [{}])[0].get("message") if "choices" in resp
              else resp.get("message")) or {}
        calls = msg.get("tool_calls") or []
        if not calls:
            state.final_text = agentic_search._openai_style_text(msg)
            return
        msgs.append({"role": "assistant", "content": msg.get("content") or "", "tool_calls": calls})
        for tc in calls[:MAX_TOOL_CALLS_PER_ROUND]:
            fn = tc.get("function") or {}
            name = fn.get("name")
            args = _safe_json(fn.get("arguments"))
            if name not in offered_names:
                result: dict = {"error": f"ツール {name} は使用できません"}
            else:
                if _stopped():
                    state.stopped = True
                    return
                _check_deadline(absolute_deadline)
                yield {"phase": "start", "name": name, "args": args}
                # 想定外の例外は 1 件の error 結果へ丸め、ループ全体を落とさない（`GraphSchemaEraError` だけは再送出）。
                from .ingest.world_neo4j import GraphSchemaEraError
                try:
                    result, docs, c, _cards = run_tool(name, args, world, scope_paths,
                                                       deadline=absolute_deadline, layer=layer)
                except GraphSchemaEraError:
                    raise
                except Exception as e:
                    _log.warning("simple_chat: tool 実行に失敗（%s）: %s errno=%s",
                                name, type(e).__name__, getattr(e, "errno", None))
                    result, docs, c = {"error": "ツール実行に失敗しました"}, set(), []
                state.tool_calls_used += 1
                for d in sorted(docs):
                    if d not in seen_docs:
                        seen_docs.add(d)
                        state.touched_docs.append(d)
                state.cites += c
                yield {"phase": "done", "name": name, "args": args, "result": result,
                       "docs": sorted(docs)}
            tmsg = {"role": "tool", "name": name or "", "content": json.dumps(result, ensure_ascii=False)}
            if tc.get("id"):
                tmsg["tool_call_id"] = tc["id"]
            msgs.append(tmsg)
        if len(calls) > MAX_TOOL_CALLS_PER_ROUND:
            for tc in calls[MAX_TOOL_CALLS_PER_ROUND:]:
                tmsg = {"role": "tool", "name": (tc.get("function") or {}).get("name") or "",
                        "content": json.dumps({"error": "この往復での道具呼び出し上限に達しました"},
                                              ensure_ascii=False)}
                if tc.get("id"):
                    tmsg["tool_call_id"] = tc["id"]
                msgs.append(tmsg)
    state.hit_round_trip_limit = True


def verify_sources(state: LoopState, world: str, scope_paths: list | None,
                   absolute_deadline: float | None = None) -> list[dict]:
    """触れた doc_id のうち `verify_doc_exists` で実在を確かめられたものだけを出典にする（期限超過は `AnswerTimeout`）。"""
    verified: list[dict] = []
    for doc_id in state.touched_docs:
        _check_deadline(absolute_deadline)
        if not verify_doc_exists(doc_id, world, scope_paths):
            continue
        source = {"doc_id": doc_id}
        for c in state.cites:
            if c.get("doc_id") == doc_id:
                locator = _locator_from_span(c.get("span"))
                if locator:
                    source["locator"] = locator
                break
        verified.append(source)
    return verified


def answer(*, world: str, query: str, scope_paths: list | None, key_id: int | None = None,
          system_settings: dict | None = None, absolute_deadline: float | None = None) -> dict:
    """簡易チャットの同期応答を 1 回実行する。
    `world`/`scope_paths` は呼び出し側（`ext_api.ext_answer`）が解決・検証済みの値で、ここでは再解決しない。
    `absolute_deadline`（`time.monotonic()` 系）省略時は無期限。request_id は contextvars 経由でログに紐付く。
    戻り値（`C-EXT-ANSWER-01` の 200 と同形）: `{"answer","sources","unconfirmed","unconfirmed_reason","tool_calls","elapsed_ms"}`。
    """
    started = time.monotonic()

    sys_s = system_settings if system_settings is not None else store.get_system_settings(
        connect_timeout=(None if absolute_deadline is None
                         else max(0.001, _remaining_s(absolute_deadline))),
        statement_timeout_ms=(None if absolute_deadline is None
                              else max(1, int(_remaining_s(absolute_deadline) * 1000))))

    provider, model, endpoint, headers = _resolve_llm(sys_s)

    state = LoopState()
    try:
        for _ev in iter_tool_loop(state, provider=provider, model=model, endpoint=endpoint,
                                  headers=headers, system_prompt=_SYSTEM_PROMPT, query=query,
                                  world=world, scope_paths=scope_paths,
                                  absolute_deadline=absolute_deadline):
            pass
    finally:
        if state.llm_calls > 0:
            metering.record("answer", provider, model, agentic_search._usage_or_none(state.usage_acc),
                            user_id=(f"ext:{key_id}" if key_id is not None else None),
                            world=world, calls=state.llm_calls)

    _check_deadline(absolute_deadline)  # 最後の道具実行で期限を越えたら 504 相当
    verified_sources = verify_sources(state, world, scope_paths, absolute_deadline)
    _check_deadline(absolute_deadline)  # 出典確認そのものが期限を越えた場合も 200 にしない
    final_text = state.final_text
    unconfirmed = False
    unconfirmed_reason = None
    if state.hit_round_trip_limit:
        unconfirmed = True
        unconfirmed_reason = "round_trip_limit: 往復上限に達しました"
        final_text = final_text or ""
    elif not verified_sources:
        unconfirmed = True
        unconfirmed_reason = "no_verified_sources: 実在確認できた出典がありませんでした"

    return {
        "answer": final_text or "",
        "sources": verified_sources,
        "unconfirmed": unconfirmed,
        "unconfirmed_reason": unconfirmed_reason,
        "tool_calls": state.tool_calls_used,
        "elapsed_ms": max(0, round((time.monotonic() - started) * 1000)),
    }
