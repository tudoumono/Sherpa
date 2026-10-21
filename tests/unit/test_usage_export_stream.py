"""`usage_export.build_export_zip` が行を流し書きしても、CSV と activity のバイト列が一括で組んだ場合と一致すること。

期待値は、全行をメモリに持つ前提の組み方（`_csv_bytes`＋`format_turn`）をテスト側で再現して作る。
"""
from __future__ import annotations

import io
import zipfile
from datetime import datetime, timedelta, timezone

from sherpa import usage_export as UE
from sherpa.turn_activity_format import format_turn, label_agents, merge_tools

_JST = timezone(timedelta(hours=9))


def _row(conv, msg, *, codex=True):
    activity = None
    if codex:
        activity = {
            "source": "codex_rollout", "app_version": "0.19.5", "phases_ms": {"prepare": 10, "agent": 20, "post": 5},
            "settings": {"mode": "investigate", "depth": 2},
            "agents": [
                {"role": "parent", "model": "gpt-test", "tokens": {"input_tokens": 100, "output_tokens": 10},
                 "rounds": [[100, 10, 1, 0]], "compactions": [],
                 "tools": {"search": {"calls": 2, "bytes": 30, "max_bytes": 20, "ms": 4}}},
                {"role": "child", "model": "gpt-test", "tokens": {"input_tokens": 50, "output_tokens": 5},
                 "rounds": [[50, 5, 0, 0]], "compactions": [], "tools": {}},
            ],
        }
    return {
        "conversation_id": conv, "message_id": msg,
        "message_created_at": datetime(2026, 10, 1, 9, msg % 50, tzinfo=_JST), "uid": f"=user{conv}", "lens": "qa",
        "answer": {"usage": {"provider": "codex", "model": "gpt-test", "input_tokens": 150},
                   "limits": {}, "activity": activity, "stop_kind": "completed", "codex_error_code": None,
                   "duration_ms": 1234, "investigation": {"complete": True, "continuations": 0}},
    }


def _aux(i):
    return {"ts": datetime(2026, 10, 1, 9, i, tzinfo=_JST), "kind": "embed", "provider": "openai", "model": "m-1",
            "input_tokens": i, "calls": 1, "elapsed_ms": 3, "uid": "user1", "conversation_id": 1}


def test_streamed_zip_matches_one_shot_encoding():
    turns = [_row(1, 11), _row(1, 12, codex=False), _row(2, 21), _row(7, 71), _row(7, 72)]
    aux = [_aux(i) for i in range(3)]
    summary = {"period": {"start": "2026-10-01", "end": "2026-10-01", "from": "a", "to": "b"},
               "daily": [{"date": "2026-10-01", "active_users": 2, "turns": 5}],
               "tokens": {"daily": [{"date": "2026-10-01", "input": 9, "output": 1}]}}
    buf = io.BytesIO()
    UE.build_export_zip(buf, summary=summary, turn_rows=iter(turns), aux_rows=iter(aux),
                        retrieved_at=datetime(2026, 10, 2, tzinfo=timezone.utc), app_ver="0.19.5")
    zf = zipfile.ZipFile(buf)

    agents, tools = [], []
    for r in turns:
        act = r["answer"]["activity"]
        if act and act.get("source") == "codex_rollout":
            for label, agent in label_agents(act["agents"]):
                agents.append(UE._agent_row(r["conversation_id"], r["message_id"], label, agent))
                for name, t in merge_tools(agent.get("tools")).items():
                    tools.append(UE._tool_row(r["conversation_id"], r["message_id"], label, name, t))
    assert zf.read("turns.csv") == UE._csv_bytes(UE._TURNS_HEADER, [UE._turns_row(r, r["answer"]) for r in turns])
    assert zf.read("agents.csv") == UE._csv_bytes(UE._AGENTS_HEADER, agents)
    assert zf.read("tools.csv") == UE._csv_bytes(UE._TOOLS_HEADER, tools)
    assert zf.read("aux_calls.csv") == UE._csv_bytes(UE._AUX_HEADER, [UE._aux_row(r) for r in aux])
    assert zf.read("daily.csv") == UE._csv_bytes(UE._DAILY_HEADER, UE._daily_rows(summary))

    for conv in (1, 2, 7):
        rows = [r for r in turns if r["conversation_id"] == conv]
        lines = [f"会話 {conv}（回答 {len(rows)} 件）"]
        for r in rows:
            a = r["answer"]
            lines.extend(format_turn({"id": r["message_id"], "created_at": r["message_created_at"],
                                      "activity": a["activity"], "stop_kind": a["stop_kind"],
                                      "codex_error_code": a["codex_error_code"], "duration_ms": a["duration_ms"],
                                      "investigation": a["investigation"]}))
        assert zf.read(f"activity/{conv}.txt") == ("\n".join(lines) + "\n").encode("utf-8")
    assert sorted(n for n in zf.namelist() if n.startswith("activity/")) == [
        "activity/1.txt", "activity/2.txt", "activity/7.txt"]


def test_streamed_zip_with_no_rows_keeps_header_only_files():
    buf = io.BytesIO()
    UE.build_export_zip(buf, summary={"period": {}}, turn_rows=iter([]), aux_rows=iter([]),
                        retrieved_at=datetime(2026, 10, 2, tzinfo=timezone.utc), app_ver=None)
    zf = zipfile.ZipFile(buf)
    assert zf.read("turns.csv") == UE._csv_bytes(UE._TURNS_HEADER, [])
    assert zf.read("agents.csv") == UE._csv_bytes(UE._AGENTS_HEADER, [])
    assert not [n for n in zf.namelist() if n.startswith("activity/")]
