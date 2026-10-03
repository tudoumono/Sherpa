"""`SimpleProvider`（チャットの「簡易（検索して答える）」）。

簡易回答 API と同じ道具ループ（`iter_tool_loop`）をチャットから使う頭脳。巡・調査台帳・下調べ役は持たない。
`provider_id` は実際に呼んだ LLM（`openai`/`ollama`）。接続先の許可判定（SSRF）は構築時に `_resolve_llm` で行う。
設計: docs/design/simple.md「チャットの頭脳としての包み（`SimpleProvider`）」
"""
from __future__ import annotations

import time
from typing import Iterator

from .. import agentic_search, layer as layer_mod
from .. import simple_chat
from .base import Ctx, Provider, _log_chat_usage, _node, _usage_meta

LABEL = "簡易（検索して答える）"
GUIDANCE = "網羅性・確証が要る質問は「Codex 調査」へ。"
AUTHOR_MESSAGE = "資料作成は Codex 調査で行ってください。"
UNCONFIRMED_NOTICE = "※ 確認できた出典が不足しているため、内容は未確認です。"
ROUND_LIMIT_NOTICE = "※ 調べる回数の上限に達したため、回答は途中までです。"

_TOOL_LABELS = {
    "es_search": "資料を全文検索",
    "graph_neighbors": "関係グラフをたどる",
    "ripgrep_search": "資料をそのまま検索",
    "read_around": "該当箇所を精読",
}


class SimpleProvider(Provider):
    label = LABEL

    def __init__(self, provider: str, model: str, endpoint: str, headers: dict,
                 system_settings: dict | None = None):
        self.provider_id = provider
        self.model = model
        self._endpoint, self._headers = endpoint, headers
        self._system_settings = system_settings
        self._history: list = []

    # ---- 検索して答える ----
    def run(self, ctx: Ctx) -> Iterator[dict]:
        self._history = list(ctx.history or [])
        decision = ctx.route(ctx.message)
        if decision.get("lens") == "clarify":
            yield decision["question"]
            return
        scope_meta = ctx.scope_meta or {}
        scope_paths = scope_meta.get("scope_paths")
        yield _node("understand", "think", "質問を理解", "内容を把握しました", "done")
        if decision.get("lens") == "author":
            yield from self._finish(ctx, AUTHOR_MESSAGE, [], None, "資料作成は対象外",
                                    usage=None, failure=None)
            return

        extra = (f"【個人ファイル内ヒット（本人のみ・共有不可）】\n{ctx.personal_facts}"
                 if ctx.personal_facts else "")
        state = simple_chat.LoopState()
        t0 = time.monotonic()
        failure: str | None = None
        try:
            for ev in simple_chat.iter_tool_loop(
                    state, provider=self.provider_id, model=self.model, endpoint=self._endpoint,
                    headers=self._headers, system_prompt=simple_chat._SYSTEM_PROMPT,
                    query=ctx.message, world=ctx.world, scope_paths=scope_paths,
                    history=self._history, stop_event=ctx.stop_event, extra_context=extra,
                    availability=ctx.tools_availability, layer=scope_meta.get("layer")):
                nid = f"simple-{state.tool_calls_used + (1 if ev['phase'] == 'start' else 0)}"
                label = _TOOL_LABELS.get(ev["name"], ev["name"])
                if ev["phase"] == "start":
                    yield _node(nid, "tool", label, "照会しています", "active")
                else:
                    detail = ("失敗しました" if (ev.get("result") or {}).get("error")
                              else f"{len(ev.get('docs') or [])}件の資料に触れました")
                    yield _node(nid, "tool", label, detail, "done")
        except (simple_chat.LLMUnavailable, simple_chat.AnswerTimeout) as e:
            from .. import stop_kind as stop_kind_mod
            failure = stop_kind_mod.from_exception(e) or "error"
        finally:
            usage = (_usage_meta(self.provider_id, self.model,
                                 **(agentic_search._usage_or_none(state.usage_acc) or {}),
                                 system_settings=self._system_settings)
                     if state.llm_calls > 0 else None)

        if state.stopped or (ctx.stop_event is not None and ctx.stop_event.is_set()):
            return  # 停止時は `_result` を出さない
        if failure is not None:
            msg = f"{LABEL} の AI に接続できませんでした。管理者に設定を確認してください。"
            yield from self._finish(ctx, msg, [], None, "AI に接続できません",
                                    usage=usage, failure=failure, elapsed=time.monotonic() - t0)
            return

        verified = simple_chat.verify_sources(state, ctx.world, scope_paths)
        if ctx.stop_event is not None and ctx.stop_event.is_set():
            return  # 停止時は `_result` を出さない
        verified_ids = [s["doc_id"] for s in verified]
        sources = ctx.make_sources(verified_ids) if (ctx.make_sources and verified_ids) else []
        notice = None
        if state.hit_round_trip_limit:
            notice = ROUND_LIMIT_NOTICE
        elif not verified_ids:
            notice = UNCONFIRMED_NOTICE
        yield from self._finish(ctx, state.final_text or "", sources, notice, "回答しました",
                                usage=usage, failure=None, elapsed=time.monotonic() - t0)

    def _finish(self, ctx: Ctx, text: str, sources: list, notice: str | None, detail: str, *,
                usage: dict | None, failure: str | None, elapsed: float | None = None) -> Iterator[dict]:
        parts = [p for p in (notice, text.strip(), GUIDANCE) if p]
        headline = "\n\n".join(parts)
        yield _node("brain", "think", f"考える（{self.label}）", detail, "done")
        yield {"type": "answer_delta", "text": headline}
        env = {"lens": "qa", "headline": headline, "summary": {"total": len(sources)}, "data": {},
               "sources": sources,
               "scope": layer_mod.scope_with_layer(ctx.scope_meta, world=ctx.world, lens="qa")}
        if failure is not None:
            env["agentic_failure"] = failure
        if usage:
            env["usage"] = usage
            _log_chat_usage(usage, elapsed, ctx.world)
        if ctx.personal_facts:
            env["_personal_facts"] = ctx.personal_facts
        yield {"type": "_result", "env": env,
               "decision": {"lens": "qa", "input": ctx.message, "reason": "簡易（検索して答える）"}}
