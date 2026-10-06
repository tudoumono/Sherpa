"""停止・保存例外での部分回答の永続化を、偽 CLI と実 DB で確認する。"""
from __future__ import annotations

import json
import inspect
import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "unit"))
import test_codex_workspace_authoring as H
import test_codex_ledger_gate as L
from _store_helpers import get_conversation
from sherpa import agents as A, chat_service as CS, store
from sherpa.routers import chat as chat_routes
from sherpa.store import investigation_records as records
from sherpa.store.db import _connect
from sherpa.deps import neo4j_session


def _stream(cid, stop_event=None, **kw):
    with neo4j_session() as session:
        yield from CS.stream_message(session, "対象の仕様を調べて", "v1", conversation_id=cid,
                             knowledge=True, lens="qa", user_id="admin", stop_event=stop_event,
                             provider=A.CodexProvider(), settings={"agent": "codex"}, sys_settings={},
                             tools_availability={}, **kw)


def _install(tmp_path, monkeypatch, body):
    H._install_codex(tmp_path, monkeypatch, H._PY + "import json, pathlib, sys, time\n" + body)
    monkeypatch.setenv("SHERPA_USE_FIXTURES", "1")
    monkeypatch.setenv("SHERPA_CODEX_OUTPUT_SCHEMA", "2")
    monkeypatch.setenv("SHERPA_EMIT_PACE", "0")


def _ledger_script(ledger):
    body = "p = pathlib.Path.cwd() / '.tmp/investigation'\n"
    body += "(p / 'items').mkdir(parents=True, exist_ok=True)\n"
    body += f"(p / 'manifest.json').write_text({json.dumps(ledger['manifest'])!r})\n"
    body += f"(p / 'items/a.json').write_text({json.dumps(ledger['items']['a'])!r})\n"
    if ledger.get("reviews"):
        body += f"(p / 'reviews.jsonl').write_text({''.join(json.dumps(r) + chr(10) for r in ledger['reviews'])!r})\n"
    return body


@pytest.mark.parametrize("ask,stop_at", [(False, "node"), (True, "node"), (False, "delta")])
def test_stopped_answer_sources_and_ledger_survive_reload(tmp_path, monkeypatch, ask, stop_at):
    body = _ledger_script(L._open())
    body += H._msg(L._final("回収済みの結論です。\n参照した資料:\n- " + H._DOC))
    item = ({"type": "mcp_tool_call", "id": "ready", "tool": "ask_user", "status": "completed",
             "arguments": {"prompt": "調べる対象を選んでください", "choices": ["対象 A", "対象 B"]}}
            if ask else {"type": "command_execution", "id": "ready", "command": "ls", "status": "completed"})
    body += H._emit({"type": "item.completed", "item": item})
    body += "sys.stdout.flush()\n"
    if stop_at == "node":
        body += "time.sleep(30)\n"
    _install(tmp_path, monkeypatch, body)
    cid = store.create_conversation(user_id="admin", world="v1", title="partial stop")["id"]
    stop = threading.Event()
    events = []
    for ev in _stream(cid, stop):
        events.append(ev)
        if ((stop_at == "node" and ev.get("id") == "cx-ready")
                or (stop_at == "delta" and ev.get("type") == "answer_delta")):
            stop.set()
    live = next(e["message"] for e in events if e["type"] == "answer")
    saved = next(m for m in get_conversation(cid)["messages"] if m["role"] == "assistant")
    assert saved["id"] == live["id"]
    answer = saved["answer"]
    assert "回収済みの結論です。" in answer["headline"] and "停止" in answer["headline"]
    # 本文には停止の注記が混ざらず、再読み込みしても注記は notices に残る。
    assert "回収済みの結論です。" in answer["body"] and "停止" not in answer["body"]
    assert "stopped" in [n["kind"] for n in answer["notices"]]
    assert answer["completion"] == "stopped" and answer["stop_kind"] == "stopped_by_user"
    assert answer["sources"] and not answer["investigation"]["complete"]
    assert records.get_investigation_record(saved["id"])


def test_save_failure_preserves_answer_in_crash_record(tmp_path, monkeypatch):
    _install(tmp_path, monkeypatch, _ledger_script(L._complete()) + H._msg(L._final("回収済みの結論です。")))
    cid = store.create_conversation(user_id="admin", world="v1", title="partial save")["id"]
    with _connect() as c:
        c.execute("""CREATE FUNCTION reject_complete_answer() RETURNS trigger LANGUAGE plpgsql AS $$
            BEGIN IF NEW.conversation_id = %s AND NEW.role = 'assistant'
                AND COALESCE(NEW.answer->>'completion', '') <> 'partial' THEN
                RAISE EXCEPTION 'test storage failure'; END IF; RETURN NEW; END $$""" % cid)
        c.execute("CREATE TRIGGER reject_complete_answer BEFORE INSERT ON messages FOR EACH ROW EXECUTE FUNCTION reject_complete_answer()")
        c.commit()
    events = []
    run = chat_routes._turn_run_fn(
        "対象の仕様を調べて", "v1", "admin", [], True, False, lens="qa",
        provider=A.CodexProvider(), settings={"agent": "codex"}, sys_settings={},
        tools_availability={})(cid)
    try:
        with pytest.raises(Exception):
            run(threading.Event(), events.append)
        saved = next(m for m in get_conversation(cid)["messages"] if m["role"] == "assistant")
        assert "回収済みの結論です。" in saved["content"] and "エラー" in saved["content"]
        assert saved["answer"]["completion"] == "partial"
        assert saved["answer"]["stop_kind"] != "completed"
        assert next(ev for ev in events if ev["type"] == "answer")["message"]["id"] == saved["id"]
    finally:
        with _connect() as c:
            c.execute("DROP TRIGGER reject_complete_answer ON messages")
            c.execute("DROP FUNCTION reject_complete_answer()")
            c.commit()


def test_stop_between_result_checks_saves_exactly_one_answer(tmp_path, monkeypatch):
    """結果の破棄判定直前に停止し、本文・出典・台帳を一度だけ保存する。"""
    _install(tmp_path, monkeypatch, _ledger_script(L._complete()) + H._msg(
        L._final("回収済みの結論です。\n参照した資料:\n- " + H._DOC)))
    cid = store.create_conversation(user_id="admin", world="v1", title="stop race")["id"]
    stop = threading.Event()
    lines, start = inspect.getsourcelines(CS.stream_message)
    stop_line = start + next(i for i, line in enumerate(lines) if line.strip() ==
                            "if stop_event is not None and stop_event.is_set() and not _is_stopped_terminal(ev):")
    stopped = []

    def stop_at_result_check(frame, event, arg):
        if frame.f_code is not CS.stream_message.__code__:
            return None
        if (event == "line" and frame.f_lineno == stop_line
                and frame.f_locals["ev"]["type"] == "_result" and not stopped):
            stop.set()
            stopped.append(True)
        return stop_at_result_check

    previous_trace = sys.gettrace()
    try:
        sys.settrace(stop_at_result_check)
        events = list(_stream(cid, stop))
    finally:
        sys.settrace(previous_trace)
    assert stopped == [True]
    answers = [e["message"] for e in events if e["type"] == "answer"]
    saved = [m for m in get_conversation(cid)["messages"] if m["role"] == "assistant"]
    assert len(answers) == len(saved) == 1
    assert answers[0]["id"] == saved[0]["id"]
    answer = saved[0]["answer"]
    assert "回収済みの結論です。" in answer["headline"]
    assert answer["completion"] == "stopped" and answer["stop_kind"] == "stopped_by_user"
    assert answer["sources"] and records.get_investigation_record(saved[0]["id"])
    assert not any(e["type"] == "stopped" for e in events)
