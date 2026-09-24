"""C2（探索ループの文脈整理・調査結果集約と並列実行の改善方針）の実行基盤テスト。

`agentic_search.openai_style`/`anthropic_style`/`gemini` は `msgs`（会話履歴）が
`SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES` を超えたら、最新 `SHERPA_AGENTIC_KEEP_RECENT_TOOLS` 回分の
ツール往復を残し、それより古い「assistant(tool_calls)＋対応する tool 結果全部」の組を1通の
`InvestigationState.render()` 要約へ置換する。本ファイルは偽エンドポイント（`test_agentic_search.py`と
同じ流儀）で予算を小さくし、(a) 置換後も tool_call_id と結果の対応が壊れない、(b) 最新 K 回は残る、
(c) 状態メッセージは1通、(d) 予算内なら何も起きない、(e) 3 方言（OpenAI/Anthropic/Gemini）で同じ、
を固定する。LLM は stub（コスト0）。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa import agentic_search as A  # noqa: E402


class _ABlock:
    """Anthropic SDK のコンテンツブロックの最小スタブ（`test_agentic_search.py` と同型）。"""

    def __init__(self, type, text=None, name=None, input=None, id=None):
        self.type, self.text, self.name, self.input, self.id = type, text, name, input, id


class _AResp:
    def __init__(self, content, stop_reason="end_turn"):
        self.content, self.stop_reason = content, stop_reason


class _AMessages:
    def __init__(self, seq):
        self._seq, self.calls = list(seq), []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self._seq.pop(0)


class _AClient:
    def __init__(self, seq):
        self.messages = _AMessages(seq)


def _fake_run_tool(name, args, world, scope_paths, **kw):
    """`ripgrep_search` 固定の重い結果（`msgs` を素早く肥大化させて予算超過を発火させる）。"""
    return ({"hits": [{"doc_id": "a.md", "span": [1, 1], "text": "x" * 300}]}, {"a.md"}, [], [])


def _assert_openai_pairing_intact(messages: list) -> None:
    """各 `assistant(tool_calls)` の tool_call_id 全てに、直後（間に他の assistant/tool を挟まず）
    対応する `role="tool"` メッセージが揃っていることを確認する（組の途中で切れていないか）。"""
    i = 0
    while i < len(messages):
        m = messages[i]
        if m.get("role") == "assistant" and m.get("tool_calls"):
            ids = [tc["id"] for tc in m["tool_calls"] if tc.get("id")]
            j = i + 1
            seen = []
            while j < len(messages) and messages[j].get("role") == "tool":
                seen.append(messages[j].get("tool_call_id"))
                j += 1
            assert seen == ids, (seen, ids, messages)
            i = j
        else:
            i += 1


def _summary_messages(messages: list) -> list:
    """置換後の状態メッセージ（`content` が文字列の方言＝OpenAI/Anthropic 共通）だけを拾う。
    Gemini（`parts` がリスト）は各テストが専用に判定する。"""
    return [m for m in messages
           if isinstance(m.get("content"), str) and m["content"].startswith("【ここまでの調査状態】")]


# ===== OpenAI 方言 =====

def test_openai_style_no_compaction_when_within_budget():
    """(d) 予算内なら何も起きない——既定予算では数回のツール往復程度で置換は発生しない。"""
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
          for i in range(3)]
    seq.append({"choices": [{"message": {"content": "final answer"}}]})
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(json.loads(json.dumps(body)))
        return seq.pop(0)

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, _fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert not any(ev.get("node", {}).get("label") == "調査の文脈を整理" for ev in events)
    last_messages = bodies[-1]["messages"]
    assert _summary_messages(last_messages) == []
    _assert_openai_pairing_intact(last_messages)
    # 3回分すべてのツール往復がそのまま残っている（要約に置換されていない）。
    assert sum(1 for m in last_messages if m.get("role") == "tool") == 3


def test_openai_style_compaction_keeps_recent_rounds_and_single_summary(monkeypatch):
    """(a)(b)(c) 予算超過時: 最新 K 回のツール往復は生のまま残り（tool_call_id 対応も無事）、
    それより古い分は1通の要約メッセージへ置換される（要約は常に1通だけ）。"""
    monkeypatch.setattr(A, "SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES", 1500)
    monkeypatch.setattr(A, "SHERPA_AGENTIC_KEEP_RECENT_TOOLS", 2)
    n_rounds = 8
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
          for i in range(n_rounds)]
    seq.append({"choices": [{"message": {"content": "final answer"}}]})
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(json.loads(json.dumps(body)))
        return seq.pop(0)

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, _fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    compaction_nodes = [ev["node"] for ev in events if ev.get("node", {}).get("label") == "調査の文脈を整理"]
    assert compaction_nodes   # 少なくとも1回は発火した
    assert all(n["kind"] == "think" for n in compaction_nodes)   # 「行動」ではなく「考える」操作
    last_messages = bodies[-1]["messages"]
    summaries = _summary_messages(last_messages)
    assert len(summaries) == 1   # 要約メッセージは常に1通だけ（積み増ししない）
    _assert_openai_pairing_intact(last_messages)   # tool_call_id と結果の対応が壊れていない
    # 直近 K=2 回分は生のまま残る（それより古い c0..c5 は要約へ吸収済み）。
    tool_msgs = [m for m in last_messages if m.get("role") == "tool"]
    assert len(tool_msgs) == 2
    assistant_calls = [m for m in last_messages if m.get("role") == "assistant" and m.get("tool_calls")]
    kept_ids = {tc["id"] for m in assistant_calls for tc in m["tool_calls"]}
    assert kept_ids == {f"c{n_rounds - 2}", f"c{n_rounds - 1}"}
    for old_id in (f"c{i}" for i in range(n_rounds - 2)):
        assert old_id not in str(last_messages)   # 古い tool_call_id は要約に一切残らない
    # system・元の質問は触らない。
    assert last_messages[0] == {"role": "system", "content": A.SYSTEM}
    assert last_messages[1] == {"role": "user", "content": "質問"}


def test_openai_style_compaction_counted_in_final_limits(monkeypatch):
    """利用統計「打ち切りの内訳」計測: 文脈整理が発火した回数だけ最終 payload の
    `limits["context_compactions"]` に載る（`InvestigationState.limits`・制限自体は変えない）。"""
    monkeypatch.setattr(A, "SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES", 1500)
    monkeypatch.setattr(A, "SHERPA_AGENTIC_KEEP_RECENT_TOOLS", 2)
    n_rounds = 8
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
          for i in range(n_rounds)]
    seq.append({"choices": [{"message": {"content": "final answer"}}]})

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = (lambda url, headers, body, timeout=90: seq.pop(0)), _fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    compaction_count = sum(1 for ev in events if ev.get("node", {}).get("label") == "調査の文脈を整理")
    assert compaction_count >= 1
    final_events = [ev for ev in events if "final" in ev]
    assert final_events
    assert final_events[-1]["limits"]["context_compactions"] == compaction_count


def test_openai_style_compaction_summary_reflects_investigation_state(monkeypatch):
    """置換後の要約は生の JSON ではなく `InvestigationState.render()` の整形済み文面（見つけた
    根拠・呼び出し記録）——古い tool 結果を無言で消すのではなく、何を調べたかを引き継ぐ。"""
    monkeypatch.setattr(A, "SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES", 1500)
    monkeypatch.setattr(A, "SHERPA_AGENTIC_KEEP_RECENT_TOOLS", 2)
    n_rounds = 8
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
          for i in range(n_rounds)]
    seq.append({"choices": [{"message": {"content": "final answer"}}]})
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(json.loads(json.dumps(body)))
        return seq.pop(0)

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, _fake_run_tool
    try:
        list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    summary = _summary_messages(bodies[-1]["messages"])[0]["content"]
    assert "【呼び出し記録】" in summary
    assert "ripgrep_search" in summary


# ===== Anthropic 方言 =====

def test_anthropic_style_compaction_keeps_recent_rounds_and_pairing(monkeypatch):
    """(e) Anthropic 方言でも同じ契約——1組＝assistant(tool_use) 1件＋user(tool_result 配列) 1件。"""
    monkeypatch.setattr(A, "SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES", 1500)
    monkeypatch.setattr(A, "SHERPA_AGENTIC_KEEP_RECENT_TOOLS", 2)
    n_rounds = 8
    seq = [_AResp([_ABlock("tool_use", name="ripgrep_search", input={"query": "x"}, id=f"tu{i}")],
                 stop_reason="tool_use") for i in range(n_rounds)]
    seq.append(_AResp([_ABlock("text", "final answer")], stop_reason="end_turn"))
    client = _AClient(seq)
    orig_run_tool = A.run_tool
    A.run_tool = _fake_run_tool
    try:
        events = list(A.anthropic_style(client, "anthropic.claude-opus-4-8", A.SYSTEM, "質問", "v1", None))
    finally:
        A.run_tool = orig_run_tool
    assert any(ev.get("node", {}).get("label") == "調査の文脈を整理" for ev in events)
    last_messages = client.messages.calls[-1]["messages"]
    summaries = _summary_messages(last_messages)
    assert len(summaries) == 1
    # 直近 K=2 回分＝assistant(tool_use) 2件＋user(tool_result) 2件がそのまま残る。
    tool_use_msgs = [m for m in last_messages if m.get("role") == "assistant"]
    tool_result_msgs = [m for m in last_messages if m.get("role") == "user" and isinstance(m.get("content"), list)]
    assert len(tool_use_msgs) == 2 and len(tool_result_msgs) == 2
    kept_ids = {b.id for m in tool_use_msgs for b in m["content"]}
    assert kept_ids == {f"tu{n_rounds - 2}", f"tu{n_rounds - 1}"}


# ===== Gemini 方言 =====

def test_gemini_compaction_keeps_recent_rounds_and_pairing(monkeypatch):
    """(e) Gemini 方言でも同じ契約——1組＝role=model 1件＋role=user(functionResponse 配列) 1件。"""
    monkeypatch.setattr(A, "SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES", 1500)
    monkeypatch.setattr(A, "SHERPA_AGENTIC_KEEP_RECENT_TOOLS", 2)
    n_rounds = 8
    seq = [{"candidates": [{"content": {"parts": [
               {"functionCall": {"name": "ripgrep_search", "args": {"query": "x"}}}]}}]}
          for _ in range(n_rounds)]
    seq.append({"candidates": [{"content": {"parts": [{"text": "final answer"}]}}]})
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(json.loads(json.dumps(body)))
        return seq.pop(0)

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, _fake_run_tool
    try:
        events = list(A.gemini("k", "gemini-2.5-flash", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert any(ev.get("node", {}).get("label") == "調査の文脈を整理" for ev in events)
    last_contents = bodies[-1]["contents"]
    # Gemini の content は {"parts": [{"text": ...}]} 形——`_summary_messages`（content が文字列の
    # 方言向け）の判定には合わないため、ここだけ専用に判定する。
    summaries = [m for m in last_contents
                if m.get("parts") and isinstance(m["parts"][0], dict)
                and str(m["parts"][0].get("text", "")).startswith("【ここまでの調査状態】")]
    assert len(summaries) == 1
    model_msgs = [m for m in last_contents if m.get("role") == "model"]
    user_fr_msgs = [m for m in last_contents if m.get("role") == "user"
                   and m.get("parts") and isinstance(m["parts"][0], dict)
                   and "functionResponse" in m["parts"][0]]
    assert len(model_msgs) == 2 and len(user_fr_msgs) == 2


# ===== RV是正3（中）: glob_search/doc_outline/compare_documents の実質的結果は
# 文脈整理後も render() の要約から読み取れる（生のツール結果 JSON は消えても state は消えない）=====

def test_glob_search_result_content_survives_context_compaction(monkeypatch):
    """コーディネータ報告の再現: glob_search で見つけたパスは、そのラウンドが古くなって
    要約へ置換された後も、置換後の状態メッセージ（render() の出力）から読み取れる
    ——件数だけが残り内容が失われる、という回帰を固定する。"""
    monkeypatch.setattr(A, "SHERPA_AGENTIC_CONTEXT_BUDGET_BYTES", 1500)
    monkeypatch.setattr(A, "SHERPA_AGENTIC_KEEP_RECENT_TOOLS", 2)

    def fake_run_tool(name, args, world, scope_paths, **kw):
        if name == "glob_search":
            return ({"count": 2, "paths": ["src/BILLING.cbl", "src/TAXCALC.cbl"], "truncated": False},
                    set(), [], [])
        return _fake_run_tool(name, args, world, scope_paths, **kw)

    n_rounds = 8
    seq = [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": "c0", "function": {"name": "glob_search", "arguments": '{"pattern":"*.cbl"}'}}]}}]}]
    seq += [{"choices": [{"message": {"content": "", "tool_calls": [
               {"id": f"c{i}", "function": {"name": "ripgrep_search", "arguments": '{"query":"x"}'}}]}}]}
           for i in range(1, n_rounds)]
    seq.append({"choices": [{"message": {"content": "final answer"}}]})
    bodies = []

    def fake_post(url, headers, body, timeout=90):
        bodies.append(json.loads(json.dumps(body)))
        return seq.pop(0)

    orig_post, orig_run_tool = A._post, A.run_tool
    A._post, A.run_tool = fake_post, fake_run_tool
    try:
        events = list(A.openai_style("http://x", {}, "gpt-5.5", A.SYSTEM, "質問", "v1", None))
    finally:
        A._post, A.run_tool = orig_post, orig_run_tool
    assert any(ev.get("node", {}).get("label") == "調査の文脈を整理" for ev in events)
    last_messages = bodies[-1]["messages"]
    summary = _summary_messages(last_messages)[0]["content"]
    # c0（glob_search のラウンド）は直近 K=2 に含まれない（c6/c7 が直近）ため要約側に吸収されている。
    assert not any(m.get("role") == "assistant" and m.get("tool_calls")
                  and m["tool_calls"][0]["id"] == "c0" for m in last_messages)
    assert "BILLING.cbl" in summary and "TAXCALC.cbl" in summary
