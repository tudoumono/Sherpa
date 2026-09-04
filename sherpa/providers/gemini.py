"""`GeminiProvider`（リファクタリング計画 フェーズ5 S6・`sherpa/agents.py` から純移動）。

Gemini API の頭脳。`_GenProvider`（base.py）を継承し、`_agentic_loop`/`_stream` だけを実装する。
`sherpa/agents.py` が facade として本モジュールから再エクスポートするため、`_select_provider`
（まだ agents.py に残る）の `return GeminiProvider(...)` は無改修で動く。

移動に伴い相対 import の深さが1段増える（`sherpa/agents.py` → `sherpa/providers/gemini.py`）ため
`from . import agentic_search` は `from .. import agentic_search` に変更した（挙動は不変・
参照先モジュールは変わらない）。`llm.gemini_url`/`llm.gemini_headers` は元コードと同じ外部 import
形態（`from .. import llm` でモジュールごと import し `llm.X` で参照）のまま。

**地雷（危険な継ぎ目・`tests/unit/test_usage_capture.py::test_gemini_stream_captures_usage` が固定）**:
`_stream` の `with urllib.request.urlopen(...)` は元コードのフレームのままにする（`import
urllib.request` を本モジュールに持たせる）。`_patch_urlopen` は
`monkeypatch.setattr(A.urllib.request, "urlopen", ...)`＝`sherpa.agents.urllib.request` という
stdlib モジュールそのものの属性を書き換えるため、`urllib.request` は Python では単一の共有
モジュールオブジェクトであり、どのモジュールが `import urllib.request` していても同じ
オブジェクトを指す。したがって本モジュールから `urllib.request.urlopen` を呼んでも patch は
効く（S5 ollama.py と同じ理由・facade の `import urllib.request` は別テストが pin するため削除しない）。

`_GenProvider.run`（base.py）が `_gather` を facade 経由で実行時解決するため、本クラス自体は
`_gather` を直接呼ばない＝危険な継ぎ目リストに `GeminiProvider` 自体は載っていない
（`_can_ask`/`_usage_meta` は base.py から直接 import してよい・patch 対象ではない）。
`_gemini_usage` は本モジュールに実体を置く（`agents.py` は facade 経由の再エクスポート）。
"""
from __future__ import annotations

import json
import urllib.request
from typing import Iterator

from .. import llm
from .base import _CompletionState, _GenProvider, _can_ask, _usage_meta


def _gemini_usage(provider_id: str, model: str | None, um: dict) -> dict:
    """Gemini の usageMetadata → 標準 usage メタ（cached=cachedContentTokenCount・reasoning=thoughtsTokenCount）。"""
    um = um or {}
    # Gemini の promptTokenCount はキャッシュ分を含む（cached ⊆ input）。
    return _usage_meta(provider_id, model,
                       input_tokens=um.get("promptTokenCount"),
                       cached_input_tokens=um.get("cachedContentTokenCount"),
                       output_tokens=um.get("candidatesTokenCount"),
                       reasoning_output_tokens=um.get("thoughtsTokenCount"))


class GeminiProvider(_GenProvider):
    label = "Gemini"
    provider_id = "gemini"
    # EV-0（拡張設計 §4.4）: Gemini の自然完了理由（`candidates[0].finishReason`）。
    _natural_completion_reasons = frozenset({"STOP"})

    def __init__(self, api_key: str, model: str = "gemini-2.5-flash"):
        super().__init__()
        self._key, self.model = api_key, model

    def _agentic_loop(self, ctx):
        from .. import agentic_search
        # SC-6e: ターン先頭の可用性 snapshot と希望の実効集合（要求∩可用）から SYSTEM を
        # 組み立てる（OpenAIProvider._agentic_loop と同じ理由）。
        _tools_pref = (ctx.scope_meta or {}).get("tools")
        _eff_tools = agentic_search.effective_tools_pref(_tools_pref, ctx.tools_availability)
        sys = (self.system_prompt + "\n\n" if self.system_prompt else "") + \
            agentic_search.system_prompt(_eff_tools)
        return agentic_search.gemini(self._key, self.model, sys, ctx.message, ctx.world,
                                     (ctx.scope_meta or {}).get("scope_paths"), stop_event=ctx.stop_event,
                                     can_ask=_can_ask(ctx.message), history=ctx.history or [],
                                     layer=(ctx.scope_meta or {}).get("layer"),
                                     tools_pref=_tools_pref, tools_availability=ctx.tools_availability)

    def _stream(self, prompt: str, completion: _CompletionState | None = None) -> Iterator[str]:
        # R1a: 直前ターンの履歴（あれば）を prompt の前に role マップして並べる（assistant→model）。
        # `self._history` は上流（Ctx.history）で既にキャップ済み・空なら従来と完全同一の contents。
        contents = [{"role": ("model" if h.get("role") == "assistant" else "user"),
                    "parts": [{"text": h.get("content", "")}]} for h in self._history]
        contents.append({"role": "user", "parts": [{"text": prompt}]})
        body = {"contents": contents, "generationConfig": {"temperature": 0.2}}
        if self.system_prompt:
            body["system_instruction"] = {"parts": [{"text": self.system_prompt}]}
        url = llm.gemini_url(self.model, "streamGenerateContent", sse=True)
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers=llm.gemini_headers(self._key))   # 本文テキストのみ送信
        with urllib.request.urlopen(req, timeout=self._timeout) as r:
            for raw in r:
                line = raw.decode("utf-8", "replace").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if not payload:
                    continue
                try:
                    obj = json.loads(payload)
                    parts = (obj.get("candidates") or [{}])[0].get("content", {}).get("parts", [])
                    ch = parts[0].get("text") if parts else None
                except (ValueError, IndexError, KeyError):
                    obj, ch = {}, None
                if obj.get("usageMetadata"):                  # F3: 各 SSE チャンクの累積 usageMetadata（末尾が最終値）
                    self._last_usage = _gemini_usage(self.provider_id, self.model, obj["usageMetadata"])
                # EV-0（拡張設計 §4.4）: 完了理由（"STOP"＝自然完了・"MAX_TOKENS"＝打ち切り 等）を
                # 拾う。observation 自体（finishReason の到達）が終端シグナル——本文チャンク後に
                # 前触れなく EOF になった場合（finishReason を一度も見ない）は `terminal_seen` が
                # False のまま。
                fr = (obj.get("candidates") or [{}])[0].get("finishReason")
                if fr and completion is not None:
                    completion.terminal_seen = True
                    completion.reason = fr
                if ch:
                    yield ch

    def _attribute(self, text: str, digest: str, ev_map: dict, call_budget=None) -> set:
        from .. import agentic_search
        return agentic_search.attribute_gemini(
            llm.gemini_url(self.model), llm.gemini_headers(self._key), text, digest, ev_map,
            call_budget=call_budget)
