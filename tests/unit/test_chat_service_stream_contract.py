"""stream_message が出すイベントの列と、保存・監査・停止の扱いを固定する（関数の分割で変えないための比較用）。
設計: docs/design/chat.md「仕上げと保存」
"""
from __future__ import annotations

import threading

import pytest

from sherpa import chat_history, chat_service as CS
from sherpa import store
from tests.unit.test_chat_service import (
    _NODE, _answer_message, _audits, _fixed_question, _fixed_result, _mock_store_no_db, _rows, _turn,
    _no_plain_provider,  # noqa: F401  (autouse fixture)
)


class _CtxProvider:
    """渡された Ctx を記録し、決められたイベントを流す。"""

    def __init__(self, events):
        self.events = events
        self.ctx = None

    def run(self, ctx):
        self.ctx = ctx
        return iter(self.events)


def _use_provider(monkeypatch, provider):
    seen: dict = {}

    def _get(settings, **kw):
        seen["settings"] = settings
        seen["system_settings"] = kw.get("system_settings")
        return provider

    monkeypatch.setattr(CS, "get_provider", _get)
    return seen


def _types(events):
    return [e["type"] for e in events]


def test_answer_turn_event_order_saved_rows_and_audit(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    audits = _audits(monkeypatch)
    user_saved: list = []
    _use_provider(monkeypatch, _CtxProvider([_NODE, _NODE, _fixed_result("回答")]))
    events = _turn("順序の質問", on_user_saved=lambda mid, p: user_saved.append((mid, p)))
    assert _types(events) == ["trace_meta", "node", "node", "answer"]
    assert events[0] == {"type": "trace_meta", "trace_version": 2}
    assert [r["role"] for r in saved] == ["user", "assistant"]
    assert saved[0]["content"] == "順序の質問"
    assert user_saved == [(1, False)]
    msg = events[-1]["message"]
    assert events[-1]["conversation_id"] == 999 and msg is saved[1]
    assert msg["lens"] == "qa" and msg["content"] == "回答"
    assert [n["id"] for n in msg["trace"]] == ["understand"]       # id 単位で dedup
    assert msg["answer"]["trace_version"] == 2
    assert isinstance(msg["answer"]["duration_ms"], int)
    assert msg["answer"]["activity"]["source"] == "none"
    assert msg["answer"]["route"] == {"lens": "qa", "reason": "t", "path": CS._ROUTE_PATH.get("qa", [])}
    assert len(audits) == 1
    assert audits[0]["message_id_user"] == 1 and audits[0]["message_id_assistant"] == 2
    assert audits[0]["lens"] == "qa" and audits[0]["stopped"] is False and audits[0]["personal"] is False


def test_clarify_turn_events_and_audit(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    audits = _audits(monkeypatch)
    _use_provider(monkeypatch, _CtxProvider([_NODE, _fixed_question()]))
    events = _turn("確認の質問")
    assert _types(events) == ["trace_meta", "node", "question"]
    q = events[-1]
    assert q["conversation_id"] == 999 and q["original_message"] == "確認の質問" and q["interaction_id"] == "q1"
    assert [r["role"] for r in saved] == ["user", "assistant"]
    assert saved[1]["lens"] == "clarify" and saved[1]["content"] == "確認したいことがあります。"
    assert saved[1]["answer"]["question"]["original_message"] == "確認の質問"
    assert saved[1]["answer"]["activity"]["source"] == "none"
    assert [n["id"] for n in saved[1]["trace"]] == ["understand"]
    assert audits[0]["lens"] == "clarify" and audits[0]["message_id_assistant"] == saved[1]["id"]


def test_stop_without_terminal_audits_stopped_and_saves_no_assistant(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    audits = _audits(monkeypatch)
    stop = threading.Event()

    class _P:
        def run(self, ctx):
            yield dict(_NODE)
            stop.set()
            yield {"type": "node", "id": "late", "kind": "think", "label": "x", "detail": "", "status": "done"}
            yield {"type": "answer_delta", "text": "あ"}

    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _P())
    events = _turn("停止", stop_event=stop)
    assert _types(events) == ["trace_meta", "node", "stopped"]
    assert events[-1] == {"type": "stopped", "conversation_id": 999}
    assert _rows(saved, "assistant") == []
    assert len(audits) == 1
    assert audits[0]["lens"] == "stopped" and audits[0]["stopped"] is True
    assert audits[0]["message_id_assistant"] is None and audits[0]["message_id_user"] == 1


def test_stop_with_codex_multi_agent_result_saves_stopped_answer(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    audits = _audits(monkeypatch)
    stop = threading.Event()
    stop.set()
    result = _fixed_result("途中までの回答")
    result["env"]["codex_multi_agent"] = True
    recovered: dict = {}
    _use_provider(monkeypatch, _CtxProvider([result]))
    events = _turn("停止後に結果", stop_event=stop, recovered_result=recovered)
    assert _types(events) == ["trace_meta", "answer"]
    ans = events[-1]["message"]["answer"]
    assert ans["completion"] == "stopped" and ans["stopped_by_user"] is True and "_terminal" not in ans
    assert saved[-1]["role"] == "assistant"
    assert audits[0]["lens"] == "stopped" and audits[0]["stopped"] is True
    assert audits[0]["message_id_assistant"] == saved[-1]["id"]
    assert recovered["stopped"] is True and recovered["saved_message"] is saved[-1]
    assert recovered["result"]["type"] == "_result" and recovered["personal"] is False


def test_recovered_result_filled_for_codex_multi_agent_without_stop(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    result = _fixed_result("Codex 回答")
    result["env"]["codex_multi_agent"] = True
    result["env"]["codex_wrote_files"] = ["a.md"]
    recovered: dict = {}
    _use_provider(monkeypatch, _CtxProvider([_NODE, result]))
    events = _turn("復元", recovered_result=recovered)
    assert recovered["stopped"] is False and recovered["personal"] is True
    assert recovered["saved_message"] is saved[-1] and recovered["trace"].keys() == {"understand"}
    assert saved[-1]["personal"] is True and _types(events)[-1] == "answer"


def test_recovered_result_untouched_without_codex_multi_agent(monkeypatch):
    _mock_store_no_db(monkeypatch)
    recovered: dict = {}
    _use_provider(monkeypatch, _CtxProvider([_fixed_result("通常")]))
    _turn(recovered_result=recovered)
    assert recovered == {}


def test_history_unavailable_ends_turn_with_failed_answer_before_provider(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    audits = _audits(monkeypatch)
    prov = _CtxProvider([_fixed_result("呼ばれない")])
    _use_provider(monkeypatch, prov)

    def _boom(conversation_id):
        raise chat_history.HistoryUnavailable("履歴を読めません")

    monkeypatch.setattr(CS, "_history_pairs", _boom)
    user_saved: list = []
    events = _turn("履歴失敗", personal=True, on_user_saved=lambda mid, p: user_saved.append((mid, p)))
    assert _types(events) == ["trace_meta", "answer"]
    msg = events[-1]["message"]
    assert msg["answer"]["completion"] == "failed" and msg["content"] == "履歴を読めません"
    assert prov.ctx is None
    assert [r["role"] for r in saved] == ["user", "assistant"] and user_saved == [(1, True)]
    assert saved[0]["personal"] is True and saved[1]["personal"] is True
    assert audits[0]["lens"] == "error"


def test_ctx_carries_history_codex_resume_and_web_search_override(monkeypatch):
    _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(CS, "_history_pairs", lambda cid: [("前の質問", "前の回答")])
    monkeypatch.setattr(store, "get_session_id", lambda cid: "sess-1")
    monkeypatch.setattr(store, "get_codex_usage_total", lambda cid: {"total_tokens": 7})
    monkeypatch.setattr(store, "get_settings", lambda uid: {"codex_web_search": True, "agent": "codex"})
    prov = _CtxProvider([_fixed_result("resume")])
    seen = _use_provider(monkeypatch, prov)
    stop = threading.Event()
    _turn("続き", web_search=False, stop_event=stop, doc_focus=True)
    ctx = prov.ctx
    assert ctx.history == [("前の質問", "前の回答")]
    assert ctx.codex_session_id == "sess-1" and ctx.codex_usage_prev_total == {"total_tokens": 7}
    assert ctx.conversation_id == 999 and ctx.message == "続き" and ctx.world == "v1"
    assert ctx.knowledge is False and ctx.scope_meta is None and ctx.make_sources is None
    assert ctx.tools_availability is None and ctx.stop_event is stop
    assert ctx.personal is False and ctx.personal_facts == "" and ctx.doc_focus is False
    assert isinstance(ctx.turn_started_mono, float)
    assert seen["settings"]["codex_web_search"] is False and seen["settings"]["agent"] == "codex"


def test_explicit_provider_settings_and_snapshots_are_used_as_given(monkeypatch):
    _mock_store_no_db(monkeypatch)
    monkeypatch.setattr(store, "get_settings", lambda uid: pytest.fail("settings を読み直さない"))
    monkeypatch.setattr(store, "_read_system_settings_fresh", lambda **kw: pytest.fail("読み直さない"))
    monkeypatch.setattr(CS, "get_provider", lambda *a, **k: pytest.fail("渡された provider を使う"))
    prov = _CtxProvider([_fixed_result("given")])
    avail = {"grep": True}
    _turn(provider=prov, settings={"agent": "api"}, sys_settings={"x": 1}, tools_availability=avail)
    assert prov.ctx.tools_availability is avail


def test_plain_provider_used_when_knowledge_off(monkeypatch):
    _mock_store_no_db(monkeypatch)
    plain = _CtxProvider([_fixed_result("plain")])
    monkeypatch.setattr(CS, "plain_provider_for", lambda s, ss: plain)
    monkeypatch.setattr(CS, "get_provider", lambda *a, **k: pytest.fail("plain を優先"))
    msg = _answer_message(_turn())
    assert msg["content"] == "plain" and plain.ctx is not None


def test_knowledge_turn_with_personal_hits_marks_personal_and_feeds_ctx(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    calls: dict = {}
    hits = [{"path": "memo.md", "line": 1, "text": "個人のメモ"}]
    monkeypatch.setattr(CS, "_known_terms", lambda session, world: calls.setdefault("known", ["語"]))
    monkeypatch.setattr(CS, "_resolve_scope",
                        lambda *a: {"world": "v1", "scope_paths": ["a/"], "source": "all"})
    monkeypatch.setattr(CS.agentic_search, "tool_availability", lambda: {"grep": True})
    monkeypatch.setattr(CS, "_personal_grep_hits",
                        lambda uid, message, users_dir: calls.setdefault("hits", hits))
    monkeypatch.setattr(CS, "_personal_facts", lambda h, m: "個人の事実")
    import sherpa.chat_finish as CF
    monkeypatch.setattr(CF, "_personal_citations", lambda h: [{"doc_id": "memo.md"}])
    prov = _CtxProvider([_fixed_result("個人あり")])
    _use_provider(monkeypatch, prov)
    events = _turn("個人も見て", knowledge=True, personal=True)
    ctx = prov.ctx
    assert ctx.knowledge is True and ctx.personal is True and ctx.personal_facts == "個人の事実"
    assert ctx.scope_meta["scope_paths"] == ["a/"] and ctx.tools_availability == {"grep": True}
    assert callable(ctx.make_sources)
    assert _types(events) == ["trace_meta", "answer"]
    msg = events[-1]["message"]
    assert msg["personal"] is True and msg["answer"]["personal_sources"] == [{"doc_id": "memo.md"}]
    assert _rows(saved, "user")[0]["personal"] is True


def test_persistence_failure_after_user_saved_propagates_after_trace_meta(monkeypatch):
    _mock_store_no_db(monkeypatch)
    inner = store.add_message

    def _fail_assistant(conversation_id, role, content="", **k):
        if role == "assistant":
            raise RuntimeError("write failed")
        return inner(conversation_id, role, content, **k)

    monkeypatch.setattr(store, "add_message", _fail_assistant)
    _use_provider(monkeypatch, _CtxProvider([_NODE, _fixed_result("x")]))
    got: list = []
    with pytest.raises(RuntimeError, match="write failed"):
        for ev in CS.stream_message(None, "q", world="v1", conversation_id=999, user_id="admin"):
            got.append(ev["type"])
    assert got == ["trace_meta", "node"]


def test_conversation_created_when_id_is_none(monkeypatch):
    saved = _mock_store_no_db(monkeypatch)
    created: list = []
    monkeypatch.setattr(store, "create_conversation",
                        lambda **kw: created.append(kw) or {"id": 55})
    _use_provider(monkeypatch, _CtxProvider([_fixed_result("新規")]))
    events = list(CS.stream_message(None, "  新しい話題  ", world="v1", conversation_id=None, user_id="u1"))
    assert created == [{"user_id": "u1", "world": "v1", "title": "新しい話題"}]
    assert events[-1]["conversation_id"] == 55 and saved[0]["conversation_id"] == 55
