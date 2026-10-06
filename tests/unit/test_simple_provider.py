"""`sherpa/providers/simple.py::SimpleProvider`（チャットの「簡易（検索して答える）」）の単体テスト。

LLM は外部境界のため HTTP 層（`agentic_search._post`）だけを固定応答に差し替える。道具の実行・出典の
実在確認は `fixtures/corpus/v1` の実ファイルに対して本物が動く。
"""
from __future__ import annotations

import os
import threading

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

from sherpa import agentic_search as A  # noqa: E402
from sherpa import providers as providers_pkg  # noqa: E402
from sherpa.agents import Ctx, SimpleProvider  # noqa: E402
from sherpa.providers import simple as simple_mod  # noqa: E402

_REAL_DOC = "4期/04_運用/障害記録.md"   # fixtures/corpus/v1 の実在ファイル
_AVAIL = {"fulltext": False, "graph": False}   # 道具は ripgrep_search/read_around のみ


def _tool_msg(*calls) -> dict:
    return {"message": {"content": "", "tool_calls": [
        {"id": f"c{i}", "function": {"name": n, "arguments": a}} for i, (n, a) in enumerate(calls)]}}


def _final(text: str) -> dict:
    return {"message": {"content": text}}


def _read_real() -> dict:
    return _tool_msg(("read_around", f'{{"doc_id":"{_REAL_DOC}","line":1}}'))


@pytest.fixture
def posts(monkeypatch):
    """`agentic_search._post` を固定応答列に差し替え、送信ボディを記録する。"""
    seq: list = []
    bodies: list = []

    def _fake(url, headers, body, timeout=90):
        bodies.append(body)
        item = seq.pop(0)
        return item() if callable(item) else item

    monkeypatch.setattr(A, "_post", _fake)
    monkeypatch.setattr(A, "_tools_availability_cache", {"at": 0.0, "data": None})
    return seq, bodies


def _provider() -> SimpleProvider:
    return SimpleProvider("ollama", "qwen2.5", "http://localhost:11434/api/chat", {}, system_settings={})


def _ctx(message="税率改定の障害は？", *, knowledge=True, lens="qa", history=None, stop_event=None) -> Ctx:
    return Ctx(message=message, world="v1", knowledge=knowledge,
               route=lambda m: {"lens": lens, "input": m, "reason": "t"},
               dispatch=lambda lens_, inp: {},
               scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
               make_sources=lambda docs: [{"doc_id": d} for d in docs],
               tools_availability=_AVAIL, history=history, stop_event=stop_event)


def _result(events: list) -> dict | None:
    return next((e for e in events if e["type"] == "_result"), None)


def test_sources_are_only_touched_and_verified_docs(posts):
    seq, _ = posts
    seq += [_read_real(), _final("障害の記録を確認しました。")]
    events = list(_provider().run(_ctx()))
    env = _result(events)["env"]
    assert env["lens"] == "qa"
    assert [s["doc_id"] for s in env["sources"]] == [_REAL_DOC]
    assert env["headline"].startswith("障害の記録を確認しました。")
    assert env["headline"].endswith(simple_mod.GUIDANCE)
    assert simple_mod.UNCONFIRMED_NOTICE not in env["headline"]
    assert env["usage"]["provider"] == "ollama" and env["usage"]["is_local"] == "local"
    tool_nodes = [e for e in events if e["type"] == "node" and e["kind"] == "tool"]
    assert [n["status"] for n in tool_nodes] == ["active", "done"]   # 道具ごとに思考ノードを出す


def test_no_verified_source_is_prefixed_with_unconfirmed_notice(posts):
    seq, _ = posts
    seq += [_final("資料からは確認できませんでした。")]
    env = _result(list(_provider().run(_ctx())))["env"]
    assert env["sources"] == []
    assert env["headline"].startswith(simple_mod.UNCONFIRMED_NOTICE)
    assert env["headline"].endswith(simple_mod.GUIDANCE)


def test_round_trip_and_per_round_tool_limits(posts):
    from sherpa import simple_chat
    seq, bodies = posts
    five = _tool_msg(*[("ripgrep_search", '{"query":"該当なしのダミー語xyz99"}')] * 5)
    seq += [five] * simple_chat.MAX_ROUND_TRIPS
    events = list(_provider().run(_ctx()))
    env = _result(events)["env"]
    assert len(bodies) == simple_chat.MAX_ROUND_TRIPS
    started = [e for e in events if e["type"] == "node" and e["kind"] == "tool" and e["status"] == "active"]
    assert len(started) == simple_chat.MAX_ROUND_TRIPS * simple_chat.MAX_TOOL_CALLS_PER_ROUND
    assert env["headline"].startswith(simple_mod.ROUND_LIMIT_NOTICE)


def test_stop_ends_without_result(posts, monkeypatch):
    seq, bodies = posts
    ev = threading.Event()
    ev.set()
    assert _result(list(_provider().run(_ctx(stop_event=ev)))) is None
    assert bodies == []   # 停止済みなら LLM を呼ばない

    ev2 = threading.Event()
    seq.append(lambda: (ev2.set(), _read_real())[1])   # 1回目の応答中に停止が入る
    events = list(_provider().run(_ctx(stop_event=ev2)))
    assert _result(events) is None
    assert len(bodies) == 1   # 停止後は道具も次の LLM 呼び出しも走らない

    # 出典の実在確認の最中に停止が入った場合も回答を確定させない。
    ev3 = threading.Event()
    real_verify = simple_mod.simple_chat.verify_doc_exists
    monkeypatch.setattr(simple_mod.simple_chat, "verify_doc_exists",
                        lambda *a, **kw: (ev3.set(), real_verify(*a, **kw))[1])
    seq += [_read_real(), _final("確認しました。")]
    assert _result(list(_provider().run(_ctx(stop_event=ev3)))) is None


def test_knowledge_flag_never_turns_simple_into_a_plain_chat(posts):
    """SimpleProvider 自体は knowledge フラグで素の会話に変わらない（オフは `PlainChatProvider` が答える）。検索して答え、
    出典の注記と Codex 調査への案内を付ける。"""
    seq, bodies = posts
    seq += [_final("こんにちは。")]
    env = _result(list(_provider().run(_ctx("こんにちは", knowledge=False))))["env"]
    assert bodies[0]["tools"]
    assert env["lens"] == "qa"
    assert env["headline"].startswith(simple_mod.UNCONFIRMED_NOTICE)
    assert env["headline"].endswith(simple_mod.GUIDANCE)


def test_history_is_passed_between_system_and_question(posts):
    seq, bodies = posts
    seq += [_final("はい。")]
    list(_provider().run(_ctx(history=[{"role": "user", "content": "前の質問"},
                                         {"role": "assistant", "content": "前の答え"}])))
    roles = [m["role"] for m in bodies[0]["messages"]]
    assert roles == ["system", "user", "assistant", "user"]
    assert bodies[0]["messages"][-1]["content"] == "税率改定の障害は？"


def test_author_lens_returns_honest_message_without_calling_the_llm(posts):
    _, bodies = posts
    env = _result(list(_provider().run(_ctx("提案書を作って", lens="author"))))["env"]
    assert bodies == []
    assert simple_mod.AUTHOR_MESSAGE in env["headline"] and env["sources"] == []


def test_llm_failure_is_honest_and_not_counted_as_completed(monkeypatch):
    def _boom(*a, **kw):
        raise OSError("接続できない")

    monkeypatch.setattr(A, "_post", _boom)
    env = _result(list(_provider().run(_ctx())))["env"]
    assert env["agentic_failure"]
    assert "接続できませんでした" in env["headline"]


def test_select_provider_unwired_when_simple_ai_cannot_be_resolved():
    p = providers_pkg.get_provider({"agent": "simple"},
                                   system_settings={"research_default_provider": "not-a-real-provider"})
    assert isinstance(p, providers_pkg._UnwiredProvider)
    assert "簡易" in p.label


def test_select_provider_builds_simple_with_resolved_llm():
    p = providers_pkg.get_provider({"agent": "simple"}, system_settings={})
    assert isinstance(p, SimpleProvider)
    assert (p.provider_id, p.model) == ("ollama", "qwen2.5")




def test_plain_chat_calls_the_ai_without_tools_and_marks_knowledge_off(posts):
    """資料参照オフ: 道具を渡さず 1 回で答え、出典なし・資料を参照していない注記つき（履歴は通常どおり渡す）。"""
    seq, bodies = posts
    seq += [_final("こんにちは。")]
    p = simple_mod.PlainChatProvider("ollama", "qwen2.5", "http://localhost:11434/api/chat", {})
    ctx = _ctx("こんにちは", knowledge=False,
               history=[{"role": "user", "content": "前の質問"}, {"role": "assistant", "content": "前の答え"}])
    result = _result(list(p.run(ctx)))
    env = result["env"]
    assert len(bodies) == 1 and "tools" not in bodies[0]
    assert [m["role"] for m in bodies[0]["messages"]] == ["system", "user", "assistant", "user"]
    assert env["lens"] == "chat" and env["sources"] == [] and env["scope"]["source"] == "off"
    assert [n["kind"] for n in env["notices"]] == ["knowledge_off"]
    assert env["headline"] == "こんにちは。" and "agentic_failure" not in env
    from sherpa import answer_shape
    sealed = answer_shape.seal(env)
    assert sealed["body"] == "こんにちは。"
    assert sealed["headline"].startswith(simple_mod.KNOWLEDGE_OFF_NOTICE)


def test_plain_provider_is_chosen_for_codex_and_simple_but_not_other_agents():
    for agent in ("codex", "simple"):
        p = providers_pkg.plain_provider_for({"agent": agent}, {})
        assert isinstance(p, simple_mod.PlainChatProvider)
        assert (p.provider_id, p.model) == ("ollama", "qwen2.5")
    assert providers_pkg.plain_provider_for({"agent": "heuristic"}, {}) is None


def test_plain_chat_does_not_retry_when_the_ai_returns_a_tool_call(posts):
    """道具なしで tool call が返っても呼び直さない（本文が無ければ失敗として返す）。"""
    seq, bodies = posts
    seq += [_tool_msg(("read_around", '{"doc_id":"x","line":1}')), _final("使われないはず")]
    p = simple_mod.PlainChatProvider("ollama", "qwen2.5", "http://localhost:11434/api/chat", {})
    env = _result(list(p.run(_ctx("こんにちは", knowledge=False))))["env"]
    assert len(bodies) == 1
    assert env["agentic_failure"] == "error" and "knowledge_off" not in str(env.get("notices"))
