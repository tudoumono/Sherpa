"""`OpenAIProvider`（リファクタリング計画 フェーズ5 S6・`sherpa/agents.py` から純移動）。

OpenAI API の頭脳。`_GenProvider`（base.py）を継承し、`_agentic_loop`/`_stream` だけを実装する。
`sherpa/agents.py` が facade として本モジュールから再エクスポートするため、`_select_provider`
（まだ agents.py に残る）の `return OpenAIProvider(...)` は無改修で動く。

移動に伴い相対 import の深さが1段増える（`sherpa/agents.py` → `sherpa/providers/openai.py`）ため
`from . import agentic_search` は `from .. import agentic_search` に変更した（挙動は不変・
参照先モジュールは変わらない）。`llm.openai_url("chat/completions")`/`llm.openai_headers` は
元コードと同じ外部 import 形態（`from .. import llm` でモジュールごと import し `llm.X` で参照）
のまま。接続先（Azure OpenAI 等）は `system_settings`（DB・管理画面「接続先」欄）が唯一の真実源
（`sherpa/llm.py` 参照）＝`__init__` が受け取る `system_settings` スナップショットをそのまま
`llm.openai_url()`/`openai_headers()` へ渡す（省略時は呼び出し時に都度読み直す）。

**モジュール名の衝突に注意**: 本モジュール名は `openai`（PyPI の `openai` パッケージと同名）だが、
本体は `llm.py` 経由で HTTP を直叩きしており `import openai`（SDK）はしていない
（元コードのまま・S6 でも変更なし）。相対 import（`from .. import llm` 等）のみを使うため、
Python の絶対 import 解決で PyPI の `openai` パッケージと衝突することはない。

**地雷（危険な継ぎ目・更新: HIGH-3・2026-08-18 Codex RV）**: `_stream` は元コードのフレーム
（`with urllib.request.urlopen(...)`・`import urllib.request` を本モジュールに持たせる）を長らく
維持していたが、`OPENAI_BASE_URL` を admin が設定可能になった（S1）ことで base URL が可変になり、
3xx redirect で `Authorization`/`api-key` ヘッダが別ホストへ転送されうる穴が生まれた
（`providers/ollama.py::_stream` が R2a #3 で `urllib.request.urlopen` 直呼びから
`llm.urlopen_no_redirect`（redirect 非追跡の共有 opener）へ切り替えたのと同じ理由・同モジュールの
地雷コメント参照）。HIGH-3 で本 `_stream` も `llm.urlopen_no_redirect` 経由に揃えた。
これに伴い `tests/unit/test_usage_capture.py::test_openai_stream_captures_usage`／
`tests/unit/test_history_priming.py::test_openai_plain_run_injects_history_before_current_message`
の patch シームは、`urllib.request.urlopen` 直 patch（`A.urllib.request.urlopen`）ではもう本
`_stream` を拾えない（`opener.open(...)` は `urllib.request.urlopen` を経由しないため）ため、
`llm.urlopen_no_redirect` を直接 patch する方式へ切り替えた（`ollama.py` の既存パターンと同じ・
`monkeypatch.setattr(_llm, "urlopen_no_redirect", ...)`）。`import urllib.request`（`Request` の
組み立てに引き続き使う）と facade の `import urllib.request`（Gemini の `_stream` はまだ直呼びの
まま）は変更なし＝`tests/unit/test_agents_surface.py::test_agents_urllib_is_module_bound_on_facade`
は無改修で通る。

`_GenProvider.run`（base.py）が `_gather` を facade 経由で実行時解決するため、本クラス自体は
`_gather` を直接呼ばない＝危険な継ぎ目リストに `OpenAIProvider` 自体は載っていない
（`_can_ask`/`_usage_meta` は base.py から直接 import してよい・patch 対象ではない）。
`_openai_usage` は本モジュールに実体を置く（`agents.py` は facade 経由の再エクスポート）。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Iterator

from .. import llm
from .base import _CompletionState, _GenProvider, _can_ask, _usage_meta


def _openai_usage(provider_id: str, model: str | None, u: dict,
                  system_settings: dict | None = None) -> dict:
    """OpenAI Chat Completions の usage（prompt_tokens/completion_tokens＋details）→ 標準 usage メタ。
    `system_settings`（省略可）は `_usage_meta`（→ `agent_constructs.is_local`）の on_prem 判定
    （`llm.openai_endpoint_kind`）にそのまま渡す。"""
    u = u or {}
    pd = u.get("prompt_tokens_details") or {}
    cd = u.get("completion_tokens_details") or {}
    return _usage_meta(provider_id, model,
                       input_tokens=u.get("prompt_tokens"),
                       cached_input_tokens=pd.get("cached_tokens"),
                       output_tokens=u.get("completion_tokens"),
                       reasoning_output_tokens=cd.get("reasoning_tokens"),
                       system_settings=system_settings)


class OpenAIProvider(_GenProvider):
    label = "OpenAI API"
    provider_id = "openai"
    # EV-0（拡張設計 §4.4）: OpenAI 互換の自然完了理由（`choices[0].finish_reason`）。
    _natural_completion_reasons = frozenset({"stop"})

    def __init__(self, api_key: str, model: str = "gpt-5.5", system_settings: dict | None = None):
        super().__init__()
        self._key, self.model = api_key, model
        # `_select_provider` が key/model の解決に使ったのと同じ system_settings スナップショットを
        # 保持し、送信時（`_agentic_loop`/`_stream`/`_attribute`）の接続先解決
        # （`llm.openai_url`/`openai_headers`）へもそのまま渡す。省略時（`None`）は各呼び出しが
        # `llm.py` 側で都度読み直す従来どおりの挙動（後方互換）。
        self._system_settings = system_settings

    def _agentic_target_check(self) -> None:
        """`_agentic_run`（base.py）が agentic ループ開始前に呼ぶ I/O-free allowlist 検証
        （SC-6e）。`llm.openai_url` は base_url 解決＋許可判定のみ（SSRF チョークポイント・
        ネットワーク I/O は一切しない）——Azure 等のカスタム接続先が不許可なら例外を送出し、
        `_agentic_loop` の SYSTEM/tool schema 構築（可用性の実接続チェックを伴う）へ進む前に
        fail-closed で止める。`_agentic_loop` 自身も同じ呼び出しを（`openai_style` の引数として）
        もう一度行うが、本関数は純粋な検証で状態を持たないため二重に呼んでも副作用は無い。
        """
        llm.openai_url("chat/completions", system_settings=self._system_settings)

    def _agentic_loop(self, ctx):
        from .. import agentic_search
        from .. import depth_profile as depth_profile_mod
        # SC-6e: ターン先頭の可用性 snapshot（`ctx.tools_availability`）と希望
        # （`scope_meta["tools"]`）の実効集合（要求∩可用）から SYSTEM を組み立てる——要求だけから
        # 作ると、実接続不能で提示されないツール（例: Neo4j 不達の graph_neighbors）を SYSTEM が
        # 推奨してしまう。`openai_style` へは希望値のまま渡し、AND 判定自体は
        # `openai_style` 側の同じ snapshot で行う（二重計算を避けつつ結果は一致する）。
        _tools_pref = (ctx.scope_meta or {}).get("tools")
        _eff_tools = agentic_search.effective_tools_pref(_tools_pref, ctx.tools_availability)
        sys = (self.system_prompt + "\n\n" if self.system_prompt else "") + \
            agentic_search.system_prompt(_eff_tools)
        # 調べる深さ（調べ方ブロック §3.2・SC-6c）: 実効基準値（system_settings→env→コード既定）に
        # 倍率をかけた値を openai_style/run_tool へ渡す（既定 "standard" は倍率×1＝挙動不変）。
        profile = (ctx.scope_meta or {}).get("depth_profile")
        max_turns = depth_profile_mod.scaled_turns(
            depth_profile_mod.effective_base(self._system_settings, "max_turns", agentic_search.MAX_TURNS),
            profile)
        max_hits = depth_profile_mod.scaled_ratio(
            depth_profile_mod.effective_base(self._system_settings, "grep_max_hits", agentic_search.MAX_HITS),
            profile, abs_max=agentic_search.MAX_HITS_ABS_MAX)
        window_cap = depth_profile_mod.scaled_ratio(
            depth_profile_mod.effective_base(self._system_settings, "read_window", agentic_search.READ_WINDOW),
            profile, abs_max=agentic_search.READ_WINDOW_ABS_MAX)
        return agentic_search.openai_style(
            llm.openai_url("chat/completions", system_settings=self._system_settings),
            llm.openai_headers(self._key, system_settings=self._system_settings),
            self.model, sys, ctx.message, ctx.world, (ctx.scope_meta or {}).get("scope_paths"),
            stop_event=ctx.stop_event, can_ask=_can_ask(ctx.message), history=ctx.history or [],
            layer=(ctx.scope_meta or {}).get("layer"),
            max_turns=max_turns, max_hits=max_hits, window_cap=window_cap,
            tools_pref=_tools_pref, tools_availability=ctx.tools_availability)

    def _stream(self, prompt: str, completion: _CompletionState | None = None) -> Iterator[str]:
        # temperature は送らない（gpt-5.5 系は既定値(1)以外を拒否し 400 になる・2026-08-15 実測）。
        body = json.dumps({"model": self.model, "stream": True,
                           # F3: 末尾に choices 空・usage 付きチャンクを1つ返させる（トークン記録用）。
                           "stream_options": {"include_usage": True},
                           "messages": self._messages(prompt)}).encode()
        req = urllib.request.Request(
            llm.openai_url("chat/completions", system_settings=self._system_settings), data=body,
            headers=llm.openai_headers(self._key, system_settings=self._system_settings))
        # HIGH-3（2026-08-18 Codex RV）: base URL が可変（Azure 等）になったため、3xx redirect で
        # Authorization/api-key が別ホストへ転送されないよう redirect 非追跡の共有 opener を使う
        # （module docstring・ollama.py の同種地雷コメント参照）。本文テキストのみ送信（CLAUDE.md）。
        # streaming は `post_json`/`openai_post_json` を通らない生 urlopen のため、Request 構築後・
        # opener 呼出直前にここで直接 `assert_openai_io_allowed()` を再確認する（`openai_url()`/
        # `openai_headers()` は呼び出し時点の確認のみ・多層防御の最終段）。
        llm.assert_openai_io_allowed()
        with llm.urlopen_no_redirect(req, timeout=self._timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    # EV-0（拡張設計 §4.4）: SSE ストリームが形式どおり終端した、を観測。
                    # 本文チャンク後に前触れなく EOF になった場合（`[DONE]` を一度も見ない）は
                    # `terminal_seen` が False のまま＝呼び出し元は未完了として扱う。
                    if completion is not None:
                        completion.terminal_seen = True
                    break
                try:
                    obj = json.loads(data)
                except ValueError:
                    continue
                if obj.get("usage"):                          # F3: usage チャンク（choices は空）
                    self._last_usage = _openai_usage(self.provider_id, self.model, obj["usage"],
                                                     self._system_settings)
                choice0 = (obj.get("choices") or [{}])[0]
                # EV-0（拡張設計 §4.4）: 最終チャンクの finish_reason を拾う（"stop"＝自然完了・
                # "length"＝打ち切り 等）。途中のチャンクは通常 None のため、非 None を上書きで
                # 残せばよい（複数回来ない）。finish_reason 自体の観測も終端シグナルとして扱う。
                fr = choice0.get("finish_reason")
                if fr and completion is not None:
                    completion.terminal_seen = True
                    completion.reason = fr
                ch = choice0.get("delta", {}).get("content")
                if ch:
                    yield ch

    def _attribute(self, text: str, digest: str, ev_map: dict, call_budget=None) -> set:
        from .. import agentic_search
        return agentic_search.attribute_openai_style(
            llm.openai_url("chat/completions", system_settings=self._system_settings),
            llm.openai_headers(self._key, system_settings=self._system_settings), self.model, False,
            text, digest, ev_map, self._timeout, call_budget=call_budget)
