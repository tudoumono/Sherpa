"""`OllamaProvider`（リファクタリング計画 フェーズ5 S5・`sherpa/agents.py` から純移動）。

ローカル LLM（Ollama）の頭脳。`_GenProvider`（base.py）を継承し、`_agentic_loop`/`_stream` だけを
実装する。`sherpa/agents.py` が facade として本モジュールから再エクスポートするため、
`_select_provider`（まだ agents.py に残る）の `return OllamaProvider(...)` は無改修で動く。

移動に伴い相対 import の深さが1段増える（`sherpa/agents.py` → `sherpa/providers/ollama.py`）ため
`from . import agentic_search` は `from .. import agentic_search` に変更した（挙動は不変・
参照先モジュールは変わらない）。`llm.ollama_url`/`llm.JSON_HEADERS` は元コードと同じ外部 import
形態（`from .. import llm` でモジュールごと import し `llm.X` で参照）のまま。

**地雷（危険な継ぎ目・`tests/unit/test_usage_capture.py::test_ollama_stream_captures_usage` が固定）**:
R2a #3（2026-07-14 横断レビュー対応・HIGH）で `_stream` は `urllib.request.urlopen` の直呼びを
`llm.urlopen_no_redirect`（redirect 非追跡の共有 opener・`sherpa/llm.py` 参照）経由に変更した
（allowlist 通過後の応答が 3xx redirect で任意宛先へ誘導されるのを防ぐ）。これに伴い旧来の
テストシーム（`monkeypatch.setattr(A.urllib.request, "urlopen", ...)` で `urllib.request` という
stdlib の共有モジュール属性を書き換える方式）はもう本モジュールの `_stream` には効かない
（`opener.open(...)` は `urllib.request.urlopen` を経由しないため）。`test_ollama_stream_captures_usage`
はこの変更に合わせ、`llm.urlopen_no_redirect` を直接 patch する方式へ切り替えた（`llm` は本モジュールが
`from .. import llm` でモジュールごと import しているため、`llm.urlopen_no_redirect` を書き換えれば
本モジュールの呼び出しが patch を拾う＝urllib.request の共有モジュール参照と同じ理屈）。
`agents.py`（facade）側の `import urllib.request` は
`tests/unit/test_agents_surface.py::test_agents_urllib_is_module_bound_on_facade` が pin して
いるため削除しない（OpenAI/Gemini の `_stream` は依然 `urllib.request.urlopen` を直呼びするため
必要・本モジュールの import とは独立）。

`_GenProvider.run`（base.py）が `_gather` を facade 経由で実行時解決するため、本クラス自体は
`_gather` を直接呼ばない＝危険な継ぎ目リストに `OllamaProvider` 自体は載っていない
（`_can_ask`/`_usage_meta` は base.py から直接 import してよい・patch 対象ではない）。

`system_settings`（SC-6e）: `__init__` が受け取るスナップショットを `self._system_settings`
に保持し、`_agentic_target_check`/`_agentic_loop`/`_stream`/`_attribute` の全 `llm.ollama_url()`
呼び出しへそのまま渡す（`OpenAIProvider` と同じ流儀）。省略（`None`）時は `_allowlisted_hosts()`
（`llm.py`）がキャッシュ miss で `store.get_system_settings()` を読みに行く経路が残るため、
実運用の構築経路（`sherpa/providers/__init__.py::_select_provider` の `agent == "ollama"` 分岐）は
既に解決済みの fresh `sys_s` を必ず渡す（`_select_provider` は元々 URL 解決に `sys_s` を使って
いたのに `OllamaProvider(...)` へは渡していなかった＝別世代の設定を見うる穴だった）。これにより
`_agentic_target_check`（agentic ループ開始前の「純粋な文字列検証」契約）の DB 読取と、URL 解決・
allowlist 判定の世代分裂の両方を閉じる。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Iterator

from .. import llm
from .base import _CompletionState, _GenProvider, _can_ask, _usage_meta


class OllamaProvider(_GenProvider):
    label = "ローカルLLM (Ollama)"
    provider_id = "ollama"
    # EV-0（拡張設計 §4.4）: Ollama ネイティブの自然完了理由（`done_reason`）は OpenAI 互換と同じ
    # 語彙（"stop"）を使う。
    _natural_completion_reasons = frozenset({"stop"})

    def __init__(self, url: str = "http://localhost:11434", model: str = "qwen2.5",
                 system_settings: dict | None = None):
        super().__init__()
        self._url, self.model = url.rstrip("/"), model
        # `_select_provider` が URL 解決に使ったのと同じ system_settings スナップショットを保持し、
        # 送信時（`_agentic_target_check`/`_agentic_loop`/`_stream`/`_attribute`）の allowlist 判定
        # （`llm.ollama_url`）へもそのまま渡す（`OpenAIProvider._system_settings` と同じ流儀）。
        # 省略時（`None`）は各呼び出しが `llm.py` 側で都度読み直す従来どおりの挙動（後方互換）。
        self._system_settings = system_settings

    def _agentic_target_check(self) -> None:
        """`_agentic_run`（base.py）が agentic ループ開始前に呼ぶ I/O-free allowlist 検証
        （SC-6e）。`llm.ollama_url` は文字列検証のみ（SSRF チョークポイント・ネットワーク
        I/O は一切しない）——不許可の宛先なら `SsrfBlocked` を送出し、`_agentic_loop` の
        SYSTEM/tool schema 構築（可用性の実接続チェックを伴う）へ進む前に fail-closed で
        止める。`_agentic_loop` 自身も同じ呼び出しを（`openai_style` の引数として）もう一度
        行うが、本関数は純粋な文字列検証で状態を持たないため二重に呼んでも副作用は無い。
        `system_settings=self._system_settings` を渡す: 省略すると
        `_allowlisted_hosts()` がキャッシュ miss で DB を読みに行き、「純粋な文字列検証」契約が
        崩れる。
        """
        llm.ollama_url(self._url, "/api/chat", system_settings=self._system_settings)

    def _agentic_loop(self, ctx):
        from .. import agentic_search
        from .. import depth_profile as depth_profile_mod
        # SC-6e: ターン先頭の可用性 snapshot と希望の実効集合（要求∩可用）から SYSTEM を
        # 組み立てる（OpenAIProvider._agentic_loop と同じ理由）。
        _tools_pref = (ctx.scope_meta or {}).get("tools")
        _eff_tools = agentic_search.effective_tools_pref(_tools_pref, ctx.tools_availability)
        sys = (self.system_prompt + "\n\n" if self.system_prompt else "") + \
            agentic_search.system_prompt(_eff_tools)
        # 調べる深さ（調べ方ブロック §3.2・SC-6c）: OpenAIProvider._agentic_loop と同じ計算
        # （実効基準値＝system_settings→env→コード既定・既定 "standard" は倍率×1＝挙動不変）。
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
            llm.ollama_url(self._url, "/api/chat", system_settings=self._system_settings), llm.JSON_HEADERS,
            self.model, sys, ctx.message, ctx.world, (ctx.scope_meta or {}).get("scope_paths"), ollama=True,
            stop_event=ctx.stop_event, can_ask=_can_ask(ctx.message), history=ctx.history or [],
            layer=(ctx.scope_meta or {}).get("layer"),
            max_turns=max_turns, max_hits=max_hits, window_cap=window_cap,
            tools_pref=_tools_pref, tools_availability=ctx.tools_availability)

    def _stream(self, prompt: str, completion: _CompletionState | None = None) -> Iterator[str]:
        body = json.dumps({"model": self.model, "stream": True,
                           "messages": self._messages(prompt)}).encode()
        req = urllib.request.Request(
            llm.ollama_url(self._url, "/api/chat", system_settings=self._system_settings), data=body,
            headers=llm.JSON_HEADERS)
        with llm.urlopen_no_redirect(req, timeout=self._timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                except ValueError:
                    continue
                ch = d.get("message", {}).get("content")
                if ch:
                    yield ch
                if d.get("done"):
                    # F3: 完了チャンクに prompt_eval_count/eval_count（Ollama はキャッシュ/推論の内訳なし）。
                    self._last_usage = _usage_meta(
                        self.provider_id, self.model,
                        input_tokens=d.get("prompt_eval_count"), output_tokens=d.get("eval_count"))
                    # EV-0（拡張設計 §4.4）: `done` チャンク自体の到達を終端観測とし
                    # （本文チャンク後に前触れなく EOF になった場合は `terminal_seen` が False の
                    # まま）、完了理由（"stop"＝自然完了・"length"＝打ち切り 等）を別途記録する。
                    if completion is not None:
                        completion.terminal_seen = True
                        completion.reason = d.get("done_reason")
                    break

    def _attribute(self, text: str, digest: str, ev_map: dict, call_budget=None) -> set:
        from .. import agentic_search
        return agentic_search.attribute_openai_style(
            llm.ollama_url(self._url, "/api/chat", system_settings=self._system_settings),
            llm.JSON_HEADERS, self.model, True,
            text, digest, ev_map, self._timeout, call_budget=call_budget)
