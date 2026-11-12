"""チャットの状態の失敗（履歴・調査記録・個人ファイル・統計・意図分類・空の回答・実行中の本文）が、成功に見えないことの試験。"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

import pytest  # noqa: E402

from sherpa import answer_shape, chat_finish, chat_personal, chat_records, intent_llm, store  # noqa: E402
from sherpa import chat_service as CS  # noqa: E402
from sherpa import simple_chat  # noqa: E402
from sherpa.agents import Ctx, SimpleProvider  # noqa: E402
from sherpa.store import turn_metrics  # noqa: E402
from test_intent_hybrid import _CONFLICT  # noqa: E402


def _new_conv():
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")
    return store.create_conversation(user_id="admin", world="v1", title="lane3")["id"]


class _Spy:
    ran = False

    def run(self, ctx):
        _Spy.ran = True
        yield {"type": "_result", "env": {"lens": "qa", "headline": "ok", "summary": {"total": 0}, "data": {},
                                          "sources": [], "scope": {"world": "v1", "scope_paths": [], "source": "all"}},
               "decision": {"lens": "qa", "input": "q", "reason": "t"}}


def test_history_read_failure_ends_turn_as_visible_failure(monkeypatch):
    cid = _new_conv()
    _Spy.ran = False
    monkeypatch.setattr(CS, "get_provider", lambda settings, **kw: _Spy())
    monkeypatch.setattr(CS, "plain_provider_for", lambda *a, **k: None)
    monkeypatch.setattr(store, "recent_messages", lambda c, limit: (_ for _ in ()).throw(RuntimeError("db")))
    events = list(CS.stream_message(None, "続きです", conversation_id=cid, knowledge=False))
    msg = next(e for e in events if e["type"] == "answer")["message"]
    assert _Spy.ran is False
    assert msg["answer"]["completion"] == "failed" and "読み取れなかった" in msg["content"]


def test_record_and_notice_both_failing_still_tells_the_screen(monkeypatch):
    monkeypatch.setattr(chat_records.store_investigation, "save_investigation_record",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    monkeypatch.setattr(chat_records.store, "append_answer_notice",
                        lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    answer = {"lens": "qa", "headline": "回答", "investigation": {"recorded": True}}
    answer_shape.seal(answer)
    row = {"id": 1, "answer": answer, "content": "回答"}
    live = chat_records._save_investigation_record({"complete": True}, 1, 1, row)
    assert live["answer"]["investigation"]["recorded"] is False
    assert "investigation_record_failed" in [n["kind"] for n in live["answer"]["notices"]]
    assert row["answer"]["investigation"]["recorded"] is True   # 渡した行は書き換えない


def test_unreadable_personal_file_is_reported_not_silently_skipped(tmp_path, monkeypatch):
    files = tmp_path / "u1" / "workspace" / "files"
    files.mkdir(parents=True)
    (files / "a.txt").write_text("税率の話", encoding="utf-8")
    (files / "b.txt").write_text("税率の話", encoding="utf-8")
    (files / "b.txt").chmod(0)
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda uid: {"a.txt", "b.txt"})
    try:
        hits = chat_personal._personal_grep_hits("u1", "税率", str(tmp_path))
    finally:
        (files / "b.txt").chmod(0o644)
    assert [h["rel_path"] for h in hits] == ["a.txt"] and hits.unreadable == 1
    env = {"lens": "qa", "headline": "x"}
    chat_finish.resolve_used_personal(env, None, personal=True, personal_hits=hits, round_personal=False)
    assert "personal_unreadable" in [n["kind"] for n in env["notices"]]


def test_personal_file_missing_on_disk_is_counted_as_unreadable(tmp_path, monkeypatch):
    """台帳にあるのに実体が消えた個人ファイルを黙って飛ばさず、読めなかった数に入れる。"""
    files = tmp_path / "u1" / "workspace" / "files"
    files.mkdir(parents=True)
    (files / "a.txt").write_text("税率の話", encoding="utf-8")
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda uid: {"a.txt", "gone.txt"})
    hits = chat_personal._personal_grep_hits("u1", "税率", str(tmp_path))
    assert [h["rel_path"] for h in hits] == ["a.txt"] and hits.unreadable == 1


def test_missing_personal_files_dir_counts_ledger_files_as_unreadable(tmp_path, monkeypatch):
    """個人ファイルを置くフォルダごと消えていても、台帳にあるファイルを読めなかった数に入れる（「一致なし」に見せない）。"""
    (tmp_path / "u1" / "workspace").mkdir(parents=True)
    monkeypatch.setattr(store, "live_workspace_rel_paths", lambda uid: {"a.txt", "b.txt"})
    hits = chat_personal._personal_grep_hits("u1", "税率", str(tmp_path))
    assert list(hits) == [] and hits.unreadable == 2


def test_stats_write_failure_adds_a_notice(monkeypatch):
    cid = _new_conv()
    monkeypatch.setattr(turn_metrics, "upsert", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("db")))
    env = {"lens": "qa", "headline": "回答", "summary": {"total": 0}, "data": {}, "sources": []}
    answer_shape.seal(env)
    msg = store.add_message(cid, "assistant", "回答", lens="qa", answer=env)
    monkeypatch.undo()
    out = chat_finish.note_stats_unsaved(msg)
    assert "stats_unsaved" in [n["kind"] for n in out["answer"]["notices"]]


def test_intent_classifier_failure_is_not_a_plain_ambiguity(monkeypatch):
    def _fail(*a, **k):
        raise intent_llm.IntentClassifierError("x")
    monkeypatch.setattr(intent_llm, "classify", _fail)
    d = CS._build_router([], "v1", {"agent": "codex"}, can_ask=True)(_CONFLICT)
    assert d["lens"] == "clarify" and d["question"]["prompt"] == CS.INTENT_FAILED_PROMPT
    with pytest.raises(intent_llm.IntentClassifierError):
        CS._build_router([], "v1", {"agent": "codex"}, can_ask=False)(_CONFLICT)


def test_empty_model_text_is_an_error_not_an_answer(monkeypatch):
    def _empty(state, **kw):
        state.llm_calls = 1
        state.final_text = ""
        return
        yield
    monkeypatch.setattr(simple_chat, "iter_tool_loop", _empty)
    p = SimpleProvider("ollama", "m", "http://localhost:11434/api/chat", {}, system_settings={})
    ctx = Ctx(message="質問", world="v1", knowledge=True, route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda d: [], tools_availability={"fulltext": False, "graph": False})
    env = next(e for e in p.run(ctx) if e["type"] == "_result")["env"]
    assert env["agentic_failure"] == "error" and simple_chat.EMPTY_ANSWER_MESSAGE in env["headline"]
    monkeypatch.setattr(simple_chat, "_resolve_llm", lambda s: ("ollama", "m", "http://x", {}))
    with pytest.raises(simple_chat.LLMUnavailable):
        simple_chat.answer(world="v1", query="q", scope_paths=None, system_settings={})


def test_simple_provider_emits_partial_text_before_tool_call(monkeypatch):
    from sherpa import agentic_search
    seq = [{"message": {"content": "まず障害記録を確認します。", "tool_calls": [
                {"id": "c0", "function": {"name": "ripgrep_search", "arguments": '{"query":"該当なしxyz99"}'}}]}},
           {"message": {"content": "確認できませんでした。"}}]
    monkeypatch.setattr(agentic_search, "_post", lambda *a, **k: seq.pop(0))
    p = SimpleProvider("ollama", "m", "http://localhost:11434/api/chat", {}, system_settings={})
    ctx = Ctx(message="質問", world="v1", knowledge=True, route=lambda m: {"lens": "qa", "input": m, "reason": "t"},
              scope_meta={"world": "v1", "scope_paths": [], "source": "all"},
              make_sources=lambda d: [], tools_availability={"fulltext": False, "graph": False})
    partial = [e for e in p.run(ctx) if e["type"] == "answer_partial"]
    assert [e["text"] for e in partial] == ["まず障害記録を確認します。"]
