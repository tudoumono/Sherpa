"""F3（2026-07-07-フィードバック一括.md）: 各 Provider の usage capture の単体テスト。

- 単発ストリーミング `_stream`（OpenAI/Gemini/Ollama/Bedrock）が本物の usage を `_last_usage` に拾う。
- agentic ループ（openai_style/gemini/anthropic_style）が全ツールターンの usage を合算し `final` に載せる。
- Codex `turn.completed` イベントの usage 抽出（純粋ヘルパ）と `_run_authoring` への配線（ソース検査）。
- 停止/ask_user では usage 無し（best-effort）。

すべて LLM 応答はモック（外部 API を実呼び出ししない）。
"""
from __future__ import annotations

import inspect
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
from sherpa import agents as A  # noqa: E402
from sherpa import agentic_search as AS  # noqa: E402
from sherpa.providers.base import _CompletionState  # noqa: E402


class _FakeResp:
    """urllib.request.urlopen の戻り（context manager ＋ byte 行イテレータ）を模す。"""
    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return iter(self._lines)

    def __exit__(self, *a):
        return False


def _patch_urlopen(monkeypatch, lines):
    monkeypatch.setattr(A.urllib.request, "urlopen",
                        lambda req, timeout=None: _FakeResp(lines))


def _patch_no_redirect_urlopen(monkeypatch, lines):
    """`providers/ollama.py::_stream`／`providers/openai.py::_stream` 専用の patch。
    R2a #3（2026-07-14・ollama）／HIGH-3（2026-08-18 Codex RV・openai）で両 `_stream` は
    `urllib.request.urlopen` の直呼びをやめ `llm.urlopen_no_redirect`（redirect 非追跡の共有
    opener）経由に変更したため、`_patch_urlopen`（urllib.request.urlopen を patch）はもう
    どちらの `_stream` にも効かない（`sherpa/providers/ollama.py`・`openai.py` の地雷コメント参照）。
    旧名 `_patch_ollama_urlopen`（HIGH-3 是正で openai にも使うため改名）。"""
    from sherpa import llm as _llm
    monkeypatch.setattr(_llm, "urlopen_no_redirect",
                        lambda req, timeout=None: _FakeResp(lines))


# ---- 単発ストリーミング _stream の usage capture ----

def test_openai_stream_captures_usage(monkeypatch):
    lines = [
        b'data: {"choices":[{"delta":{"content":"\xe5\x9b\x9e\xe7\xad\x94"}}]}\n',
        b'data: {"choices":[],"usage":{"prompt_tokens":100,"completion_tokens":20,'
        b'"prompt_tokens_details":{"cached_tokens":30},"completion_tokens_details":{"reasoning_tokens":5}}}\n',
        b'data: [DONE]\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    # system_settings={} を明示: is_local の on_prem 判定（llm.openai_endpoint_kind）が
    # DB へ問い合わせに行かないようにする（このテストの主眼は usage の形であって接続先種別ではない）。
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5", system_settings={})
    text = "".join(p._stream("こんにちは"))
    assert "回答" in text
    assert p._last_usage == {"provider": "openai", "model": "gpt-5.5",
                             "input_tokens": 100, "cached_input_tokens": 30,
                             "output_tokens": 20, "reasoning_output_tokens": 5, "is_local": "cloud"}


def test_gemini_stream_captures_usage(monkeypatch):
    lines = [
        b'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n',
        b'data: {"usageMetadata":{"promptTokenCount":100,"candidatesTokenCount":20,'
        b'"cachedContentTokenCount":30,"thoughtsTokenCount":5}}\n',
    ]
    _patch_urlopen(monkeypatch, lines)
    p = A.GeminiProvider("key-dummy", "gemini-2.5-flash")
    text = "".join(p._stream("hi"))
    assert text == "Hi"
    assert p._last_usage == {"provider": "gemini", "model": "gemini-2.5-flash",
                             "input_tokens": 100, "cached_input_tokens": 30,
                             "output_tokens": 20, "reasoning_output_tokens": 5, "is_local": "cloud"}


def test_ollama_stream_captures_usage(monkeypatch):
    lines = [
        b'{"message":{"content":"Hi"}}\n',
        b'{"done":true,"prompt_eval_count":100,"eval_count":20}\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OllamaProvider("http://localhost:11434", "qwen2.5")
    text = "".join(p._stream("hi"))
    assert text == "Hi"
    assert p._last_usage == {"provider": "ollama", "model": "qwen2.5",
                             "input_tokens": 100, "cached_input_tokens": 0,
                             "output_tokens": 20, "reasoning_output_tokens": 0, "is_local": "local"}


class _FakeBedrockStream:
    def __init__(self, texts, usage, stop_reason=None):
        self.text_stream = iter(texts)
        self._usage = usage
        self._stop_reason = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def get_final_message(self):
        return type("M", (), {"usage": self._usage, "stop_reason": self._stop_reason})()


def test_bedrock_stream_captures_usage():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 5,
             "cache_creation_input_tokens": 3, "output_tokens": 7}
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "stream": staticmethod(lambda **kw: _FakeBedrockStream(["Hi ", "there"], usage))})()})()
    text = "".join(p._stream("hi"))
    assert text == "Hi there"
    # cached ⊆ input へ正規化: input = 10 + 5 + 3 = 18・cached = 5
    assert p._last_usage == {"provider": "bedrock",
                             "model": "jp.anthropic.claude-haiku-4-5-20251001-v1:0",
                             "input_tokens": 18, "cached_input_tokens": 5,
                             "output_tokens": 7, "reasoning_output_tokens": 0, "is_local": "cloud"}


# ---- EV-0（拡張設計 §4.4）: 単発ストリーミング _stream の完了状態 capture ----
# plan/hybrid（providers/base.py）が帰属直前に参照する呼び出しローカルの `_CompletionState`
# （`terminal_seen`/`reason`/`truncated`）を、各方言の完了通知（OpenAI/Ollama互換 finish_reason／
# Ollama done_reason／Gemini finishReason／Anthropic/Bedrock stop_reason）から正しく拾えること、
# および終端フレーム自体を観測できなかった場合は `terminal_seen` が False のまま（＝未完了と
# 判定される）ことを固定する。`_last_completion_reason` のような `self.` 属性ではなく、呼び出し
# ごとに新規生成した `_CompletionState` を `_stream(prompt, completion=...)` へ渡す契約
# （`providers/base.py::_CompletionState` docstring 参照）。

def test_gen_provider_natural_completion_reasons_exact_per_dialect():
    """`_GenProvider` の既定は空集合（fail-closed）——具象サブクラスが明示的に上書きしない限り、
    どんな完了理由も「自然完了」と認めない。4具象 Provider（openai/ollama/gemini/bedrock）は
    それぞれちょうど自分の方言の理由集合を宣言する（他方言の値を混ぜない・欠落させない）。
    `_CompletionState` へ `provider._natural_completion_reasons` をそのまま渡す契約どおりに、
    allowlist に含まれる理由では truncated=False、含まれない理由（他方言の正当値を含む）では
    truncated=True になることも併せて表駆動で固定する。
    """
    assert A._GenProvider._natural_completion_reasons == frozenset()

    table = [
        (A.OpenAIProvider, frozenset({"stop"})),
        (A.OllamaProvider, frozenset({"stop"})),
        (A.GeminiProvider, frozenset({"STOP"})),
        (A.BedrockProvider, frozenset({"end_turn", "stop_sequence"})),
    ]
    for cls, expected in table:
        assert cls._natural_completion_reasons == expected

    all_reasons = frozenset().union(*(expected for _, expected in table))
    for cls, expected in table:
        for reason in expected:
            completion = _CompletionState(cls._natural_completion_reasons)
            completion.terminal_seen = True
            completion.reason = reason
            assert completion.truncated is False, (cls, reason)
        for reason in all_reasons - expected:   # 他方言の正当値は自分には不正
            completion = _CompletionState(cls._natural_completion_reasons)
            completion.terminal_seen = True
            completion.reason = reason
            assert completion.truncated is True, (cls, reason)


def test_openai_stream_captures_completion_reason_length(monkeypatch):
    lines = [
        b'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"length"}]}\n',
        b'data: [DONE]\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    completion = _CompletionState()
    "".join(p._stream("こんにちは", completion=completion))
    assert completion.terminal_seen is True
    assert completion.reason == "length"
    assert completion.truncated is True   # "length" は自然完了 allowlist に無い


def test_openai_stream_completion_reason_stop_is_not_truncated(monkeypatch):
    lines = [
        b'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}\n',
        b'data: {"choices":[{"delta":{},"finish_reason":"stop"}]}\n',
        b'data: [DONE]\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    completion = _CompletionState()
    "".join(p._stream("こんにちは", completion=completion))
    assert completion.reason == "stop"
    assert completion.truncated is False   # 自然完了 allowlist に含まれる＝plan/hybrid は帰属を省略しない


def test_openai_stream_eof_without_terminal_frame_leaves_terminal_seen_false(monkeypatch):
    """本文チャンクの後、`[DONE]`/`finish_reason` を一度も見ないまま EOF になった場合（例: 上流/
    プロキシが接続を打ち切った）、`terminal_seen` は False のまま——`reason` が None でも
    「未完了」と明確に判定できる（終端欠落は allowlist 判定だけでは拾えない）。"""
    lines = [b'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}\n']
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    completion = _CompletionState()
    text = "".join(p._stream("こんにちは", completion=completion))
    assert text == "a"
    assert completion.terminal_seen is False
    assert completion.reason is None
    assert completion.truncated is True


def test_completion_state_non_string_reason_is_truncated_not_typeerror():
    """`reason` が文字列でない（壊れた upstream 応答が finish_reason へ dict/list 等を返した）場合、
    自然完了 allowlist（frozenset）への `in` 判定で `TypeError`（非 hashable）を出さず、常に
    「未完了」（帰属を省略）として扱う——fail-closed（拡張設計 §4.4）。"""
    completion = _CompletionState()
    completion.terminal_seen = True
    completion.reason = {"unexpected": "shape"}   # dict は非 hashable（frozenset の `in` は TypeError）
    assert completion.truncated is True

    completion2 = _CompletionState()
    completion2.terminal_seen = True
    completion2.reason = ["stop"]                 # list も同様に非 hashable
    assert completion2.truncated is True

    completion3 = _CompletionState()
    completion3.terminal_seen = True
    completion3.reason = 200                      # 数値（hashable だが文字列ではない）も未完了扱い
    assert completion3.truncated is True


def test_openai_stream_non_string_finish_reason_does_not_raise(monkeypatch):
    """upstream が壊れて `finish_reason` に文字列以外（例: object）を返しても `_stream` 自体は
    例外を出さずに完走し、`completion.truncated` は True（未完了扱い）のまま——本文配信後に
    帰属直前の判定で `TypeError` を出して落ちない（end-to-end）。"""
    lines = [
        b'data: {"choices":[{"delta":{"content":"a"},"finish_reason":null}]}\n',
        b'data: {"choices":[{"delta":{},"finish_reason":{"bad":true}}]}\n',
        b'data: [DONE]\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    completion = _CompletionState()
    text = "".join(p._stream("こんにちは", completion=completion))
    assert text == "a"
    assert completion.terminal_seen is True
    assert completion.reason == {"bad": True}
    assert completion.truncated is True   # 例外にならず「未完了」扱いになる


def test_openai_stream_completion_omitted_is_backward_compatible(monkeypatch):
    """`completion` 省略時（既存の直接呼び出し）は例外を起こさず、単に完了状態を記録しない。"""
    lines = [
        b'data: {"choices":[{"delta":{"content":"a"}}]}\n',
        b'data: [DONE]\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    assert "".join(p._stream("こんにちは")) == "a"


def test_gemini_stream_captures_completion_reason_max_tokens(monkeypatch):
    lines = [
        b'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n',
        b'data: {"candidates":[{"finishReason":"MAX_TOKENS"}]}\n',
    ]
    _patch_urlopen(monkeypatch, lines)
    p = A.GeminiProvider("key-dummy", "gemini-2.5-flash")
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.terminal_seen is True
    assert completion.reason == "MAX_TOKENS"
    assert completion.truncated is True


def test_gemini_stream_completion_reason_stop_is_not_truncated(monkeypatch):
    lines = [
        b'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n',
        b'data: {"candidates":[{"finishReason":"STOP"}]}\n',
    ]
    _patch_urlopen(monkeypatch, lines)
    p = A.GeminiProvider("key-dummy", "gemini-2.5-flash")
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.truncated is False


def test_gemini_stream_eof_without_finish_reason_leaves_terminal_seen_false(monkeypatch):
    lines = [b'data: {"candidates":[{"content":{"parts":[{"text":"Hi"}]}}]}\n']
    _patch_urlopen(monkeypatch, lines)
    p = A.GeminiProvider("key-dummy", "gemini-2.5-flash")
    completion = _CompletionState()
    text = "".join(p._stream("hi", completion=completion))
    assert text == "Hi"
    assert completion.terminal_seen is False
    assert completion.truncated is True


def test_ollama_stream_captures_completion_reason_length(monkeypatch):
    lines = [
        b'{"message":{"content":"Hi"}}\n',
        b'{"done":true,"done_reason":"length","prompt_eval_count":100,"eval_count":20}\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OllamaProvider("http://localhost:11434", "qwen2.5")
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.terminal_seen is True
    assert completion.reason == "length"
    assert completion.truncated is True


def test_ollama_stream_completion_reason_stop_is_not_truncated(monkeypatch):
    lines = [
        b'{"message":{"content":"Hi"}}\n',
        b'{"done":true,"done_reason":"stop","prompt_eval_count":100,"eval_count":20}\n',
    ]
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OllamaProvider("http://localhost:11434", "qwen2.5")
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.truncated is False


def test_ollama_stream_eof_without_done_leaves_terminal_seen_false(monkeypatch):
    lines = [b'{"message":{"content":"Hi"}}\n']   # "done" チャンクが来ないまま EOF
    _patch_no_redirect_urlopen(monkeypatch, lines)
    p = A.OllamaProvider("http://localhost:11434", "qwen2.5")
    completion = _CompletionState()
    text = "".join(p._stream("hi", completion=completion))
    assert text == "Hi"
    assert completion.terminal_seen is False
    assert completion.truncated is True


def test_bedrock_stream_captures_completion_reason_max_tokens():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "output_tokens": 7}
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "stream": staticmethod(lambda **kw: _FakeBedrockStream(
            ["Hi"], usage, stop_reason="max_tokens"))})()})()
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.terminal_seen is True
    assert completion.reason == "max_tokens"
    assert completion.truncated is True


def test_bedrock_stream_completion_reason_end_turn_is_not_truncated():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "output_tokens": 7}
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "stream": staticmethod(lambda **kw: _FakeBedrockStream(
            ["Hi"], usage, stop_reason="end_turn"))})()})()
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.truncated is False


def test_bedrock_stream_completion_reason_stop_sequence_is_not_truncated():
    usage = {"input_tokens": 10, "cache_read_input_tokens": 0,
             "cache_creation_input_tokens": 0, "output_tokens": 7}
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "stream": staticmethod(lambda **kw: _FakeBedrockStream(
            ["Hi"], usage, stop_reason="stop_sequence"))})()})()
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.truncated is False


def test_bedrock_stream_completion_terminal_not_seen_on_get_final_message_failure():
    """`get_final_message()` が例外を投げたら（usage 同様）`terminal_seen` を**立てない**——
    で反転した契約: 取得失敗を「終端を観測できた（reason=None）」と偽装しない。呼び出し元は
    `terminal_seen=False` を見て未完了と判定する（fail-closed・旧 fail-open 挙動の反転）。"""
    class _BoomStream:
        def __init__(self):
            self.text_stream = iter(["Hi"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            raise RuntimeError("boom")

    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "stream": staticmethod(lambda **kw: _BoomStream())})()})()
    completion = _CompletionState()
    "".join(p._stream("hi", completion=completion))
    assert completion.terminal_seen is False
    assert completion.reason is None
    assert completion.truncated is True


# ---- run() が _last_usage を env["usage"] に載せる（単発経路の配線） ----

def test_run_attaches_usage_to_env(monkeypatch):
    """OpenAIProvider.run の単発ストリーミング経路（agentic 非対象レンズ）は _last_usage を env に乗せる。"""
    from sherpa.agents import Ctx
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")

    def fake_stream(prompt):
        p._last_usage = A._usage_meta("openai", "gpt-5.5", input_tokens=42, output_tokens=7,
                                      system_settings={})   # is_local が DB を叩かないよう固定
        yield "答え"
    monkeypatch.setattr(p, "_stream", fake_stream)
    # impact レンズ＝agentic_search 非対象＝単発ストリーミング経路に入る。dispatch が env を返す。
    ctx = Ctx(message="影響は?", world="v1", knowledge=True,
              route=lambda m: {"lens": "impact", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {"lens": "impact", "headline": "", "summary": {}, "data": {},
                                          "sources": [], "scope": {}},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda docs: [])
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    assert result["env"]["usage"] == {"provider": "openai", "model": "gpt-5.5",
                                      "input_tokens": 42, "cached_input_tokens": 0,
                                      "output_tokens": 7, "reasoning_output_tokens": 0,
                                      "is_local": "cloud"}


def test_run_logs_chat_usage_line(monkeypatch, caplog):
    """LOG-UX（2026-09-04）: kind="chat" は metering.record() を通らない（answer.usage への二重計上を
    避ける契約）ため、_GenProvider.run() が _log_chat_usage() 経由で sherpa.usage へ直接1行出す
    （上の test_run_attaches_usage_to_env と同じ単発経路・elapsed/world も乗ること）。"""
    from sherpa.agents import Ctx
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")

    def fake_stream(prompt):
        p._last_usage = A._usage_meta("openai", "gpt-5.5", input_tokens=42, output_tokens=7,
                                      system_settings={})
        yield "答え"
    monkeypatch.setattr(p, "_stream", fake_stream)
    ctx = Ctx(message="影響は?", world="v1", knowledge=True,
              route=lambda m: {"lens": "impact", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {"lens": "impact", "headline": "", "summary": {}, "data": {},
                                          "sources": [], "scope": {}},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda docs: [])
    with caplog.at_level("INFO", logger="sherpa.usage"):
        list(p.run(ctx))
    lines = [r.message for r in caplog.records if r.name == "sherpa.usage"]
    assert len(lines) == 1
    assert "kind=chat" in lines[0] and "provider=openai" in lines[0] and "model=gpt-5.5" in lines[0]
    assert "in=42" in lines[0] and "out=7" in lines[0]
    assert "elapsed=" in lines[0]
    assert "world=v1" in lines[0]


# ---- agentic ループの usage 合算（final に載る） ----

def test_openai_style_accumulates_usage(monkeypatch):
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        {"choices": [{"message": {"content": "答えです。"}}],
         "usage": {"prompt_tokens": 50, "completion_tokens": 20,
                   "prompt_tokens_details": {"cached_tokens": 40}}},
    ]
    monkeypatch.setattr(AS, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    final = next(ev for ev in AS.openai_style("http://x", {}, "gpt-5.5", AS.SYSTEM, "q", "v1", None)
                 if "final" in ev)
    # 2ターン分を合算
    assert final["usage"] == {"input_tokens": 150, "cached_input_tokens": 40,
                              "output_tokens": 30, "reasoning_output_tokens": 0}


def test_openai_style_ollama_accumulates_toplevel_counts(monkeypatch):
    seq = [{"message": {"content": "答え"}, "prompt_eval_count": 80, "eval_count": 12}]
    monkeypatch.setattr(AS, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    final = next(ev for ev in AS.openai_style("http://x", {}, "qwen2.5", AS.SYSTEM, "q", "v1", None, ollama=True)
                 if "final" in ev)
    assert final["usage"] == {"input_tokens": 80, "cached_input_tokens": 0,
                              "output_tokens": 12, "reasoning_output_tokens": 0}


def test_gemini_accumulates_usage(monkeypatch):
    seq = [{"candidates": [{"content": {"parts": [{"text": "答え"}]}}],
            "usageMetadata": {"promptTokenCount": 70, "candidatesTokenCount": 8,
                              "cachedContentTokenCount": 10, "thoughtsTokenCount": 3}}]
    monkeypatch.setattr(AS, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    final = next(ev for ev in AS.gemini("key", "gemini-2.5-flash", AS.SYSTEM, "q", "v1", None)
                 if "final" in ev)
    assert final["usage"] == {"input_tokens": 70, "cached_input_tokens": 10,
                              "output_tokens": 8, "reasoning_output_tokens": 3}


def test_anthropic_style_accumulates_usage():
    class _Blk:
        def __init__(self, text):
            self.type, self.text = "text", text

    class _Resp:
        stop_reason = "end_turn"
        content = [_Blk("答えです。")]
        usage = {"input_tokens": 60, "cache_read_input_tokens": 5,
                 "cache_creation_input_tokens": 2, "output_tokens": 9}

    client = type("C", (), {"messages": type("MS", (), {
        "create": staticmethod(lambda **kw: _Resp())})()})()
    final = next(ev for ev in AS.anthropic_style(client, "claude-haiku-4-5", "sys", "q", "v1", None)
                 if "final" in ev)
    assert final["usage"] == {"input_tokens": 67, "cached_input_tokens": 5,
                              "output_tokens": 9, "reasoning_output_tokens": 0}


def test_agentic_run_wraps_usage_with_provider_and_model(monkeypatch):
    """agents._agentic_run は agentic_search の生 usage を provider/model 付き標準形にして env に乗せる。"""
    from sherpa.agents import Ctx
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}],
         "usage": {"prompt_tokens": 100, "completion_tokens": 10}},
        {"choices": [{"message": {"content": "答えです。"}}],
         "usage": {"prompt_tokens": 50, "completion_tokens": 20}},
    ]
    monkeypatch.setattr(AS, "_post", lambda url, headers, body, timeout=90: seq.pop(0))
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5", system_settings={})   # is_local が DB を叩かないよう固定
    ctx = Ctx(message="消費税率は?", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda docs: [{"doc_id": d} for d in docs])
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    assert result["env"]["usage"] == {"provider": "openai", "model": "gpt-5.5",
                                      "input_tokens": 150, "cached_input_tokens": 0,
                                      "output_tokens": 30, "reasoning_output_tokens": 0,
                                      "is_local": "cloud"}


# ---- Codex turn.completed（純粋ヘルパ） ----

def test_usage_from_turn_completed_parses_real_shape():
    ev = {"type": "turn.completed", "usage": {"input_tokens": 341026, "cached_input_tokens": 244864,
                                              "output_tokens": 12318, "reasoning_output_tokens": 9392}}
    assert A._usage_from_turn_completed(ev, "gpt-5.5", system_settings={}) == {
        "provider": "codex", "model": "gpt-5.5", "input_tokens": 341026,
        "cached_input_tokens": 244864, "output_tokens": 12318, "reasoning_output_tokens": 9392,
        "is_local": "cloud"}


def test_usage_from_turn_completed_codex_model_provider_drives_is_local():
    """Codex は常に provider_id="codex" を名乗るため、実際の接続先は呼び出し元が明示した
    `codex_model_provider`（"ollama"/"openai"）でしか分からない（`agent_constructs.is_local`
    へそのまま渡す・省略時は "openai" 相当＝`llm.openai_endpoint_kind`/接続先ホスト次第で
    cloud/on_prem/cloud_compat）。"""
    ev = {"type": "turn.completed", "usage": {"input_tokens": 10, "cached_input_tokens": 0,
                                              "output_tokens": 2, "reasoning_output_tokens": 0}}
    assert A._usage_from_turn_completed(ev, "qwen2.5", codex_model_provider="ollama")["is_local"] == "local"
    assert A._usage_from_turn_completed(
        ev, "gpt-5.5", codex_model_provider="openai", system_settings={})["is_local"] == "cloud"
    assert A._usage_from_turn_completed(
        ev, "gpt-5.5", codex_model_provider="openai",
        system_settings={"openai_endpoint_kind": "custom",
                         "openai_base_url": "http://10.0.0.5:8000/v1"})["is_local"] == "on_prem"
    assert A._usage_from_turn_completed(
        ev, "gpt-5.5", codex_model_provider="openai",
        system_settings={"openai_endpoint_kind": "custom",
                         "openai_base_url": "https://api.example.com/v1"})["is_local"] == "cloud_compat"


def test_usage_from_turn_completed_none_for_other_events():
    assert A._usage_from_turn_completed({"type": "item.completed"}, "gpt-5.5") is None
    assert A._usage_from_turn_completed({"type": "turn.completed"}, "gpt-5.5") is None   # usage 無し
    assert A._usage_from_turn_completed({"type": "turn.completed", "usage": "x"}, "gpt-5.5") is None


def test_codex_usage_wired_in_run_authoring():
    """CodexProvider._run_authoring が turn.completed を拾って env["usage"] に載せること（Popen 必須のためソース検査）。"""
    src = inspect.getsource(A.CodexProvider._run_authoring)
    assert 'e.get("type") == "turn.completed"' in src
    assert 'codex_model_provider="ollama" if self._ollama_base_url is not None else "openai"' in src
    assert 'env["usage"] = codex_usage' in src
