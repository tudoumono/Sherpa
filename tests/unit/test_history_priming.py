"""R1a（会話継続・履歴 priming）の break-and-confirm テスト
（docs/proposals/2026-07-13-横断レビュー対応.md §R1a）。

「追質問が前ターンを理解しない」を解消するため、`Ctx.history`（chat_service._history_pairs が
構築する直前ターンの (user, assistant) 完全対・二重キャップ済み）が各 provider（openai/ollama/
gemini/bedrock・単発＋agentic）と Codex の実送信 body/messages/contents/プロンプトへ実際に注入
されることを、外部 HTTP を実際に叩かずに検証する。seam は各 provider の既存テストの流儀に倣う:

- gemini（単発）: `urllib.request.urlopen` を `sherpa.agents.urllib.request`（共有モジュール属性）
  経由で patch（test_usage_capture.py と同じ・R2a 後も gemini の `_stream` は直呼びのまま）。
- openai／ollama（単発）: `llm.urlopen_no_redirect` を patch（ollama は R2a #3・openai は HIGH-3
  〔2026-08-18 Codex RV〕の地雷・test_usage_capture.py の `_patch_no_redirect_urlopen` と同じ流儀。
  build_opener 切替は urllib.request.urlopen の monkeypatch を静かに無効化するため、この経路だけは
  別 seam が必要）。
- bedrock: `p._client` を fake に差し替え（test_bedrock_provider.py と同じ）。
- agentic 3スタイル（openai_style/anthropic_style/gemini）: `agentic_search._post` 差し替え／
  anthropic は fake client（test_agentic_search.py と同じ）。
- Codex: `_prompt`/`_prompt_mcp` を直接呼ぶ（プロセス起動なし）。

`chat_service._history_pairs` 自体の単体テスト（完全対抽出・二重キャップ・確認ID 回帰等）は
tests/unit/test_chat_service.py 側にある。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa import agentic_search as AS  # noqa: E402
from sherpa import agents as A  # noqa: E402
from sherpa.agents import Ctx  # noqa: E402

# 直前ターン1対（chat_service._history_pairs が返す shape そのもの＝時系列順の user/assistant）。
_HISTORY = [{"role": "user", "content": "前回の質問です"}, {"role": "assistant", "content": "前回の回答です"}]


class _FakeResp:
    """urllib.request.urlopen / llm.urlopen_no_redirect の戻り（context manager ＋行イテレータ）を模す
    （test_usage_capture.py と同じ流儀）。"""

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return iter(self._lines)

    def __exit__(self, *a):
        return False


# ---- base.py: _GenProvider.run() が history を確定させる（provider 非依存の土台） ----

def test_gen_provider_run_sets_history_attribute_before_dispatch():
    class _StubGen(A._GenProvider):
        label = "stub"

        def _stream(self, prompt):
            yield "stub"

    p = _StubGen()
    ctx = Ctx(message="q", world="v1", knowledge=False,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {}, history=list(_HISTORY))
    list(p.run(ctx))
    assert p._history == _HISTORY


def test_messages_with_default_empty_history_matches_legacy_shape():
    """run() を経由しない直接呼び出しは class 属性の既定 `[]` にフォールバックする（安全側）。"""
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    assert p._history == []
    assert p._messages("Q") == [{"role": "user", "content": "Q"}]


def test_codex_run_sets_history_attribute_before_knowledge_off_branch():
    p = A.CodexProvider()
    ctx = Ctx(message="hi", world="v1", knowledge=False,
              route=lambda m: {}, dispatch=lambda l, i: {}, history=list(_HISTORY))
    list(p.run(ctx))
    assert p._history == _HISTORY


# ---- 単発ストリーミング（knowledge オフ・_plain_run 経路）: 4社とも history を注入する ----

def test_openai_plain_run_injects_history_before_current_message(monkeypatch):
    from sherpa import llm as _llm

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp([b'data: {"choices":[{"delta":{"content":"OK"}}]}\n', b'data: [DONE]\n'])

    # HIGH-3（2026-08-18 Codex RV）: openai._stream は urllib.request.urlopen 直呼びをやめ
    # llm.urlopen_no_redirect 経由になった（ollama と同じ seam・上のモジュール docstring 参照）。
    monkeypatch.setattr(_llm, "urlopen_no_redirect", fake_urlopen)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="続きを教えて", world="v1", knowledge=False,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {}, history=list(_HISTORY))
    list(p.run(ctx))
    msgs = captured["body"]["messages"]
    assert msgs[0] == _HISTORY[0] and msgs[1] == _HISTORY[1]
    assert msgs[-1]["role"] == "user"


def test_ollama_plain_run_injects_history_before_current_message(monkeypatch):
    from sherpa import llm as _llm

    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp([b'{"message":{"content":"OK"}}\n', b'{"done":true}\n'])

    monkeypatch.setattr(_llm, "urlopen_no_redirect", fake_urlopen)
    p = A.OllamaProvider("http://localhost:11434", "qwen2.5")
    ctx = Ctx(message="続きを教えて", world="v1", knowledge=False,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {}, history=list(_HISTORY))
    list(p.run(ctx))
    msgs = captured["body"]["messages"]
    assert msgs[0] == _HISTORY[0] and msgs[1] == _HISTORY[1]
    assert msgs[-1]["role"] == "user"


def test_gemini_plain_run_injects_history_before_current_message(monkeypatch):
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode())
        return _FakeResp([b'data: {"candidates":[{"content":{"parts":[{"text":"OK"}]}}]}\n'])

    monkeypatch.setattr(A.urllib.request, "urlopen", fake_urlopen)
    p = A.GeminiProvider("key-dummy", "gemini-2.5-flash")
    ctx = Ctx(message="続きを教えて", world="v1", knowledge=False,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {}, history=list(_HISTORY))
    list(p.run(ctx))
    contents = captured["body"]["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "前回の質問です"}]}
    assert contents[1] == {"role": "model", "parts": [{"text": "前回の回答です"}]}
    assert contents[-1]["role"] == "user"


def test_gemini_plain_run_history_empty_matches_legacy_single_content():
    """履歴なしなら contents は従来どおり現在の user 1件だけ（role マップの新コードでも回帰なし）。"""
    p = A.GeminiProvider("key-dummy", "gemini-2.5-flash")
    assert p._history == []


def test_bedrock_plain_run_injects_history_before_current_message():
    class _Stream:
        def __init__(self):
            self.text_stream = iter(["OK"])

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def get_final_message(self):
            return type("M", (), {"usage": None})()

    calls = []
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "stream": staticmethod(lambda **kw: (calls.append(kw), _Stream())[1])})()})()
    ctx = Ctx(message="続きを教えて", world="v1", knowledge=False,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {}, history=list(_HISTORY))
    list(p.run(ctx))
    msgs = calls[0]["messages"]
    assert msgs[0] == _HISTORY[0] and msgs[1] == _HISTORY[1]
    assert msgs[-1]["role"] == "user"


class _BedrockBlock:
    def __init__(self, type, text=None):
        self.type, self.text = type, text


class _BedrockResp:
    def __init__(self, content):
        self.content = content


def test_bedrock_complete_prepends_history():
    """`_complete` は現状どの本番経路からも呼ばれていない（grep 確認済み・probe/`_stream` とは独立）が、
    契約どおり `_stream` と同じく history を前置する（将来の呼び出し元に備えた一貫性）。"""
    calls = []
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "create": staticmethod(lambda **kw: (calls.append(kw), _BedrockResp([_BedrockBlock("text", "ok")]))[1])})()})()
    p._history = list(_HISTORY)
    p._complete("続きを教えて")
    assert calls[0]["messages"][0] == _HISTORY[0] and calls[0]["messages"][1] == _HISTORY[1]
    assert calls[0]["messages"][-1] == {"role": "user", "content": "続きを教えて"}


def test_bedrock_probe_does_not_leak_history():
    """RV 事前確認: `probe()` は自前で messages を組み立てる（`_stream`/`_complete` を経由しない）ため、
    `self._history` が残留していても影響しない。"""
    calls = []
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = type("C", (), {"messages": type("MS", (), {
        "create": staticmethod(lambda **kw: (calls.append(kw), _BedrockResp([_BedrockBlock("text", "pong")]))[1])})()})()
    p._history = list(_HISTORY)   # 直前ターンの残留を模す（本来は run() 冒頭で毎ターン設定し直される）
    ok, detail = p.probe()
    assert ok is True
    assert calls[0]["messages"] == [{"role": "user", "content": "ping"}]   # history は混ざらない


# ---- agentic 3スタイル: 初期 msgs/messages/contents に history が現在の user の前に入る ----

def test_openai_style_places_history_after_system_before_user(monkeypatch):
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured.setdefault("bodies", []).append(body)
        return {"choices": [{"message": {"content": "回答です。"}}]}

    monkeypatch.setattr(AS, "_post", fake_post)
    list(AS.openai_style("http://x", {}, "gpt-5.5", AS.SYSTEM, "続きを教えて", "v1", None,
                         history=list(_HISTORY)))
    msgs = captured["bodies"][0]["messages"]
    assert msgs[0] == {"role": "system", "content": AS.SYSTEM}
    assert msgs[1] == _HISTORY[0] and msgs[2] == _HISTORY[1]
    # 帰属は回答完了後の別呼び出しで取る設計（拡張設計 §4.4）＝現在の user メッセージは無加工。
    assert msgs[3] == {"role": "user", "content": "続きを教えて"}


def test_anthropic_style_places_history_before_current_user():
    class _Blk:
        type = "text"
        text = "回答です。"

    class _Resp:
        stop_reason = "end_turn"
        content = [_Blk()]
        usage = None

    calls = []
    client = type("C", (), {"messages": type("MS", (), {
        "create": staticmethod(lambda **kw: (calls.append(kw), _Resp())[1])})()})()
    list(AS.anthropic_style(client, "m", "sys", "続きを教えて", "v1", None, history=list(_HISTORY)))
    msgs = calls[0]["messages"]
    assert msgs[0] == _HISTORY[0] and msgs[1] == _HISTORY[1]
    assert msgs[-1] == {"role": "user", "content": "続きを教えて"}
    assert "system" not in calls[0] or calls[0].get("system") == "sys"   # system は kwargs のまま（messages に混ぜない）


def test_gemini_agentic_maps_roles_and_places_history_before_current_user(monkeypatch):
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured["body"] = body
        return {"candidates": [{"content": {"parts": [{"text": "回答です。"}]}}]}

    monkeypatch.setattr(AS, "_post", fake_post)
    list(AS.gemini("key", "gemini-2.5-flash", AS.SYSTEM, "続きを教えて", "v1", None, history=list(_HISTORY)))
    contents = captured["body"]["contents"]
    assert contents[0] == {"role": "user", "parts": [{"text": "前回の質問です"}]}
    assert contents[1] == {"role": "model", "parts": [{"text": "前回の回答です"}]}
    assert contents[2] == {"role": "user", "parts": [{"text": "続きを教えて"}]}


# ---- 履歴なし（既定）は history 引数追加前と同形の初期 msgs/messages/contents
# （帰属は回答完了後の別呼び出しで取る設計＝初期 user は不変・既存 pin の再確認） ----

def test_openai_style_history_omitted_keeps_legacy_two_message_shape(monkeypatch):
    """既存 pin（test_agentic_search.py::test_provider_agentic_run_folds_personal_facts_into_prompt が
    暗黙 pin する body["messages"][1] が最初の user）と同じ契約を history 引数の観点から明示する。"""
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        captured["body"] = body
        return {"choices": [{"message": {"content": "回答です。"}}]}

    monkeypatch.setattr(AS, "_post", fake_post)
    list(AS.openai_style("http://x", {}, "gpt-5.5", AS.SYSTEM, "質問", "v1", None))
    assert captured["body"]["messages"] == [
        {"role": "system", "content": AS.SYSTEM},
        {"role": "user", "content": "質問"}]


# ---- provider 経由の end-to-end 配線（_agentic_loop から agentic_search への history 伝搬） ----

def test_openai_provider_agentic_run_passes_history_to_initial_messages(monkeypatch):
    # searched=True にするため、最初のターンで tool_call を1回踏ませてから最終回答を返す
    # （test_provider_agentic_run_builds_env と同じ流儀・tool_call 無しだと searched=False で
    # `_agentic_run` が RuntimeError を投げ単発フォールバックへ落ちてしまう）。
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "続きの回答です。"}}]},
    ]
    # `body["messages"]` は openai_style 側でループ中に同一リストが破壊的更新されるため、参照を
    # 貯めずに初回呼び出し時点の内容をその場で複製して残す（test_provider_agentic_run_folds_
    # personal_facts_into_prompt と同じ地雷回避）。
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        if "first_messages" not in captured:
            captured["first_messages"] = list(body["messages"])
        return seq.pop(0)

    monkeypatch.setattr(AS, "_post", fake_post)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="続きを教えて", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda docs: [{"doc_id": d} for d in docs], history=list(_HISTORY))
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    msgs = captured["first_messages"]                          # 初回リクエストの初期 msgs（複製済み）
    assert msgs[0]["role"] == "system"
    assert msgs[1] == _HISTORY[0] and msgs[2] == _HISTORY[1]
    assert msgs[-1] == {"role": "user", "content": "続きを教えて"}
    assert result["env"]["headline"] == "続きの回答です。"


def test_bedrock_provider_agentic_run_passes_history_to_initial_messages():
    class _ToolBlk:
        type = "tool_use"
        name = "ripgrep_search"
        input = {"query": "TAX-RATE"}
        id = "tu1"

    class _TextBlk:
        type = "text"
        text = "続きの回答です。"

    class _ToolResp:
        stop_reason = "tool_use"
        content = [_ToolBlk()]
        usage = None

    class _FinalResp:
        stop_reason = "end_turn"
        content = [_TextBlk()]
        usage = None

    # searched=True にするため tool_use → tool_result → 最終回答の2周にする
    # （test_anthropic_style_tool_loop_two_turns と同じ流儀・tool_use 無しは _agentic_run の
    # RuntimeError で単発フォールバックへ落ちてしまう）。
    # `kwargs["messages"]` は anthropic_style 側でループ中に同一リストが破壊的更新されるため、
    # 初回呼び出し時点の内容をその場で複製して残す（openai_style と同じ地雷回避）。
    seq = [_ToolResp(), _FinalResp()]
    captured = {}

    def fake_create(**kw):
        if "first_messages" not in captured:
            captured["first_messages"] = list(kw["messages"])
        return seq.pop(0)

    client = type("C", (), {"messages": type("MS", (), {
        "create": staticmethod(fake_create)})()})()
    p = A.BedrockProvider(model="jp.anthropic.claude-haiku-4-5-20251001-v1:0")
    p._client = client
    ctx = Ctx(message="続きを教えて", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda docs: [{"doc_id": d} for d in docs], history=list(_HISTORY))
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    msgs = captured["first_messages"]                          # 初回リクエストの初期 messages（複製済み）
    assert msgs[0] == _HISTORY[0] and msgs[1] == _HISTORY[1]
    assert msgs[-1] == {"role": "user", "content": "続きを教えて"}
    assert result["env"]["headline"] == "続きの回答です。"


def test_agentic_run_ctx_replace_with_personal_facts_preserves_history(monkeypatch):
    """`_agentic_run`（base.py）は personal_facts がある場合 `dataclasses.replace` で新 ctx.message を
    作る。replace は明示指定していないフィールド（history/conversation_id）をそのまま引き継ぐことの実証。"""
    seq = [
        {"choices": [{"message": {"content": "", "tool_calls": [
            {"id": "c1", "function": {"name": "ripgrep_search", "arguments": '{"query":"TAX-RATE"}'}}]}}]},
        {"choices": [{"message": {"content": "回答です。"}}]},
    ]
    # body["messages"] は同一リストが破壊的更新されるため、初回呼び出し時点の内容を複製して残す
    # （openai_style 側の既存地雷・上の他テストと同じ回避）。
    captured = {}

    def fake_post(url, headers, body, timeout=90):
        if "first_messages" not in captured:
            captured["first_messages"] = list(body["messages"])
        return seq.pop(0)

    monkeypatch.setattr(AS, "_post", fake_post)
    p = A.OpenAIProvider("sk-dummy", "gpt-5.5")
    ctx = Ctx(message="続きを教えて", world="v1", knowledge=True,
              route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              dispatch=lambda lens, inp: {},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda docs: [{"doc_id": d} for d in docs], history=list(_HISTORY),
              personal_facts="[個人ファイル: x.txt 行1] メモXYZ")
    result = next(ev for ev in p.run(ctx) if ev["type"] == "_result")
    assert result["env"]["headline"] == "回答です。"                    # フォールバックに落ちていないことの確認
    msgs = captured["first_messages"]                                  # 初回リクエストの初期 msgs（複製済み）
    assert msgs[1] == _HISTORY[0] and msgs[2] == _HISTORY[1]           # history は別チャネルのまま保持
    assert "メモXYZ" in msgs[-1]["content"] and "続きを教えて" in msgs[-1]["content"]   # personal_facts は message 側


# ---- Codex: _prompt/_prompt_mcp に履歴ブロックが挿入される（プロセス起動なし） ----

def test_codex_prompt_includes_history_block_before_question():
    p = A.CodexProvider()
    p._history = list(_HISTORY)
    prompt = p._prompt("続きを教えて", "qa", {"headline": "h", "data": {}, "summary": {}}, "v1")
    assert "【直前の会話（参考・新しいものが下）】" in prompt
    assert prompt.index("前回の回答です") < prompt.index("【質問】")
    assert "【質問】続きを教えて" in prompt


def test_codex_prompt_history_empty_matches_legacy_output_exactly():
    env = {"headline": "h", "data": {}, "summary": {}}
    p_empty = A.CodexProvider()             # 既定 _history=[]（run() 未実行）
    p_explicit = A.CodexProvider()
    p_explicit._history = []
    out_empty = p_empty._prompt("質問", "qa", env, "v1")
    assert out_empty == p_explicit._prompt("質問", "qa", env, "v1")
    assert "【直前の会話" not in out_empty


def test_codex_prompt_mcp_includes_history_block_before_question():
    p = A.CodexProvider()
    p._history = list(_HISTORY)
    prompt = p._prompt_mcp("続きを教えて", "qa", "v1")
    assert "【直前の会話（参考・新しいものが下）】" in prompt
    assert prompt.index("前回の回答です") < prompt.index("【質問】")


def test_codex_prompt_mcp_author_includes_history_block_before_request():
    p = A.CodexProvider()
    p._history = list(_HISTORY)
    prompt = p._prompt_mcp("Excelにまとめて", "author", "v1")
    assert prompt.index("前回の回答です") < prompt.index("【依頼】")


def test_codex_prompt_mcp_history_empty_matches_legacy_output_exactly():
    p_empty = A.CodexProvider()
    p_explicit = A.CodexProvider()
    p_explicit._history = []
    out_empty = p_empty._prompt_mcp("質問", "qa", "v1")
    assert out_empty == p_explicit._prompt_mcp("質問", "qa", "v1")
    assert "【直前の会話" not in out_empty
