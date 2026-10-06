"""Codex の 1 ターンの数え方と、MCP へ渡す ID の契約。

初回・自動の続き・resume 失敗後の新しいセッションは同じターンとして合算し、次の利用者のターンで 0 に戻す。
親・子・判定不能は別の状態として持つ。偽 codex は test_codex_auto_continue.py の多段階版を使う。
"""
from __future__ import annotations

import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")

import test_codex_auto_continue as helper  # noqa: E402

from sherpa import agents as A  # noqa: E402
from sherpa.providers.codex import provider as P  # noqa: E402
from sherpa.providers.codex import sandbox as SB  # noqa: E402
from sherpa.providers.codex.turn_state import (  # noqa: E402
    TOOL_USE_UNDETERMINED, TOOL_USE_UNUSED, TOOL_USE_USED, TurnToolUse,
)


def _capture_states(monkeypatch) -> list:
    states: list = []
    real = P.CodexTurnState

    def _factory(*a, **kw):
        st = real(*a, **kw)
        states.append(st)
        return st

    monkeypatch.setattr(P, "CodexTurnState", _factory)
    return states


def _attempt_nos(calls) -> list:
    key = "mcp_servers.sherpa.env.SHERPA_MCP_ATTEMPT_NO="
    return [next(a.split("=", 1)[1].strip('"') for a in c if a.startswith(key)) for c in calls]


def test_tool_use_verdict_keeps_parent_child_and_undetermined_apart():
    t = TurnToolUse()
    assert t.verdict() == TOOL_USE_UNUSED
    t.child_spawned = True
    assert t.verdict() == TOOL_USE_UNDETERMINED
    t.parent_shell_events += 1
    assert t.verdict() == TOOL_USE_USED
    t = TurnToolUse()
    t.record_unreliable = True
    assert t.verdict() == TOOL_USE_UNDETERMINED


def test_continuation_attempts_share_one_turn_and_next_turn_starts_from_zero(tmp_path, monkeypatch):
    states = _capture_states(monkeypatch)
    steps = [
        {"thread_id": "TH-T1", "agent_messages": ["まず資料を確認します。次に調べます。"], "no_tools": True,
         "usage": helper._usage()},
        {"thread_id": "TH-T1", "agent_messages": ["依存先を確認しました。"],
         "tool_ids": ["item_0"], "usage": helper._usage()},
        {"thread_id": "TH-T2", "agent_messages": ["確認した結果、問題ありません。"], "usage": helper._usage()},
    ]
    _, _, calls = helper._drive(tmp_path, monkeypatch, steps, "turn-state-continue", 931)
    assert len(calls) == 2
    first = states[0]
    assert first._attempt_no == 2
    assert first._tool_use.parent_shell_events == 1  # 2 回目の実行のコマンドが同じターンに合算される
    assert first._tool_use.verdict() == TOOL_USE_USED
    assert _attempt_nos(calls) == ["1", "2"]
    # 次の利用者のターンは別の状態で、数え直す。
    helper._run(A.CodexProvider(), helper._ctx(uid="turn-state-continue", conversation_id=931))
    second = states[1]
    assert second is not first and second.turn_uid != first.turn_uid
    assert second._tool_use.parent_shell_events == 0


def test_new_session_after_failed_resume_counts_in_the_same_turn(tmp_path, monkeypatch):
    states = _capture_states(monkeypatch)
    steps = [
        {"exit_code": 1, "no_tools": True},
        {"thread_id": "TH-NEW", "agent_messages": ["依存先を確認しました。"], "tool_ids": ["item_0"],
         "usage": helper._usage()},
    ]
    ctx = helper._ctx(uid="turn-state-fallback", conversation_id=934, codex_session_id="SID-GONE")
    _, _, calls = helper._drive(tmp_path, monkeypatch, steps, "turn-state-fallback", 934, ctx=ctx)
    assert len(calls) == 2 and "resume" in calls[0] and "resume" not in calls[1]
    assert len(states) == 1
    assert states[0]._tool_use.parent_shell_events == 1
    assert _attempt_nos(calls) == ["1", "2"]


def test_child_agent_makes_tool_use_undetermined_not_unused(tmp_path, monkeypatch):
    states = _capture_states(monkeypatch)
    steps = [{"thread_id": "TH-KID", "agent_messages": ["確認した結果、問題ありません。"], "no_tools": True,
              "extra_events": [{"type": "item.completed", "item": {
                  "id": "i0", "type": "collab_tool_call", "tool": "spawn_agent",
                  "receiver_thread_ids": ["kid-thread"]}}],
              "usage": helper._usage()}]
    helper._drive(tmp_path, monkeypatch, steps, "turn-state-child", 932)
    assert states[0]._tool_use.verdict() == TOOL_USE_UNDETERMINED


def test_mcp_receives_conversation_turn_and_attempt_ids(tmp_path, monkeypatch):
    states = _capture_states(monkeypatch)
    seen: list = []
    real = SB._write_codex_authoring_config

    def _spy(*a, **kw):
        seen.append(dict(kw.get("extra_mcp_env") or {}))
        return real(*a, **kw)

    monkeypatch.setattr(SB, "_write_codex_authoring_config", _spy)
    steps = [{"thread_id": "TH-ID", "agent_messages": ["確認した結果、問題ありません。"], "usage": helper._usage()}]
    helper._drive(tmp_path, monkeypatch, steps, "turn-state-ids", 933)
    assert seen and seen[0]["SHERPA_MCP_CONVERSATION_ID"] == "933"
    assert seen[0]["SHERPA_MCP_TURN_ID"] == states[0].turn_uid




def test_tool_zero_answer_is_nudged_once_then_noted_without_stopping(tmp_path, monkeypatch):
    states = _capture_states(monkeypatch)
    steps = [
        {"thread_id": "TH-Z", "agent_messages": ["資料は見ていませんが、たぶん問題ありません。"], "no_tools": True, "usage": helper._usage()},
        {"thread_id": "TH-Z", "agent_messages": ["やはり問題ありません。"], "usage": helper._usage()},
    ]
    ctx = helper._ctx(uid="tool-zero-1", conversation_id=941, codex_session_id="SID-ZERO")
    _, env, calls = helper._drive(tmp_path, monkeypatch, steps, "tool-zero-1", 941, ctx=ctx)
    assert "resume" in calls[0]  # 会話の続きのターンでも促す
    assert len(calls) == 2  # 促しは 1 回だけ（2 回目は促さない）
    assert states[0]._tool_use.verdict() == TOOL_USE_UNUSED
    assert env["headline"].endswith("やはり問題ありません。")
    assert [n["kind"] for n in env["notices"]] == ["no_tool_use"]
    assert env.get("completion") != "partial"


def test_tool_zero_nudge_is_not_issued_after_a_tool(tmp_path, monkeypatch):
    steps = [{"thread_id": "TH-Z2", "agent_messages": ["確認しました。問題ありません。"],
              "tool_ids": ["item_0"], "usage": helper._usage()}]
    _, env, calls = helper._drive(tmp_path, monkeypatch, steps, "tool-zero-2", 942)
    assert len(calls) == 1 and not env.get("notices")


def test_ledger_and_ask_tools_do_not_count_as_looking_into_the_documents(tmp_path, monkeypatch):
    states = _capture_states(monkeypatch)
    ledger_call = {"type": "item.completed", "item": {
        "id": "i0", "type": "mcp_tool_call", "tool": "ledger_status", "status": "completed", "arguments": {}}}
    steps = [{"thread_id": "TH-Z3", "agent_messages": ["台帳だけ書きました。"], "no_tools": True,
              "extra_events": [ledger_call], "usage": helper._usage()}]
    helper._drive(tmp_path, monkeypatch, steps, "tool-zero-3", 943)
    assert states[0]._tool_use.parent_sherpa_events == 0 and states[0]._tool_zero_nudged

