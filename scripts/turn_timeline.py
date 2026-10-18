#!/usr/bin/env python3
"""Codex の会話記録（rollout JSONL）から、1 ターンの時間の内訳を出す（読み取りだけ・DB 不要）。

使い方: python3 scripts/turn_timeline.py <users_dir>/<uid>/workspace/.codex-sessions/<conversation_id> [--json]
内訳: 実行（attempt）ごとに、モデルの応答待ち（道具の結果が返ってから次の道具呼び出し・本文が出るまでの隙間）／
MCP 道具の実行時間（mcp_tool_call_end.duration）／シェル（exec）の時間／往復回数／再送した入力トークン（round ごとの input_tokens の総和）。
本文・資料名・検索語は出さない（道具名・件数・時間だけ）。
"""
from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path


def _epoch(ts) -> float | None:
    if not isinstance(ts, str):
        return None
    try:
        return datetime.fromisoformat(ts.replace("Z", "+00:00")).astimezone(timezone.utc).timestamp()
    except ValueError:
        return None


def read_threads(conv_dir: Path) -> list[dict]:
    threads = []
    for p in sorted(conv_dir.glob("sessions/**/*.jsonl")):
        try:
            lines = p.read_bytes().decode("utf-8", errors="replace").splitlines()
        except OSError:
            continue
        if not lines:
            continue
        try:
            meta = json.loads(lines[0])
        except ValueError:
            continue
        if meta.get("type") != "session_meta":
            continue
        payload = meta.get("payload") or {}
        th = {"path": str(p), "tid": payload.get("id"), "subagent": payload.get("thread_source") == "subagent",
              "events": []}
        for raw in lines[1:]:
            try:
                rec = json.loads(raw)
            except ValueError:
                continue
            ep = _epoch(rec.get("timestamp")) if isinstance(rec, dict) else None
            if ep is not None:
                th["events"].append((ep, rec))
        threads.append(th)
    return threads


def analyze(threads: list[dict]) -> dict:
    """親スレッドの `task_started` で attempt を区切り、attempt ごとの内訳を返す。"""
    parent = [t for t in threads if not t["subagent"]]
    children = [t for t in threads if t["subagent"]]
    attempts = []
    for th in parent:
        cur = None
        open_calls: dict = {}
        last_output_ts = None
        for ep, rec in th["events"]:
            pl = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            pt = pl.get("type")
            if rec.get("type") == "event_msg" and pt == "task_started":
                cur = {"start": ep, "end": ep, "tool_calls": 0, "mcp_ms": 0.0, "exec_ms": 0.0, "model_wait_ms": 0.0,
                       "rounds": 0, "input_tokens_resent": 0, "cached_input_tokens": 0, "output_tokens": 0,
                       "tools": defaultdict(int), "max_input_tokens": 0}
                attempts.append(cur); open_calls = {}; last_output_ts = ep
                continue
            if cur is None:
                continue
            cur["end"] = max(cur["end"], ep)
            if rec.get("type") == "response_item" and pt in ("function_call", "custom_tool_call"):
                cur["tool_calls"] += 1
                name = str(pl.get("name") or "?")
                cur["tools"][name] += 1
                if last_output_ts is not None:
                    cur["model_wait_ms"] += max(0.0, (ep - last_output_ts) * 1000)
                cid = pl.get("call_id")
                if cid:
                    open_calls[cid] = (ep, name)
            elif rec.get("type") == "response_item" and pt in ("function_call_output", "custom_tool_call_output"):
                cid = pl.get("call_id")
                if cid in open_calls:
                    t0, name = open_calls.pop(cid)
                    if pt == "custom_tool_call_output":
                        cur["exec_ms"] += (ep - t0) * 1000
                last_output_ts = ep
            elif rec.get("type") == "event_msg" and pt == "mcp_tool_call_end":
                d = pl.get("duration") or {}
                cur["mcp_ms"] += (d.get("secs", 0) or 0) * 1000 + (d.get("nanos", 0) or 0) / 1e6
            elif rec.get("type") == "event_msg" and pt == "token_count":
                info = pl.get("info") if isinstance(pl.get("info"), dict) else {}
                u = info.get("last_token_usage") if isinstance(info.get("last_token_usage"), dict) else {}
                it = u.get("input_tokens"); ct = u.get("cached_input_tokens"); ot = u.get("output_tokens")
                if isinstance(it, int):
                    cur["rounds"] += 1
                    cur["input_tokens_resent"] += it
                    cur["max_input_tokens"] = max(cur["max_input_tokens"], it)
                if isinstance(ct, int):
                    cur["cached_input_tokens"] += ct
                if isinstance(ot, int):
                    cur["output_tokens"] += ot
            elif rec.get("type") == "event_msg" and pt == "agent_message":
                if last_output_ts is not None:
                    cur["model_wait_ms"] += max(0.0, (ep - last_output_ts) * 1000); last_output_ts = ep
    for a in attempts:
        a["wall_ms"] = (a["end"] - a["start"]) * 1000
        a["tools"] = dict(a["tools"])
    child_summary = {"threads": len(children), "tool_calls": 0, "mcp_ms": 0.0}
    for th in children:
        for ep, rec in th["events"]:
            pl = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
            if rec.get("type") == "response_item" and pl.get("type") in ("function_call", "custom_tool_call"):
                child_summary["tool_calls"] += 1
            if rec.get("type") == "event_msg" and pl.get("type") == "mcp_tool_call_end":
                d = pl.get("duration") or {}
                child_summary["mcp_ms"] += (d.get("secs", 0) or 0) * 1000 + (d.get("nanos", 0) or 0) / 1e6
    total = {k: sum(a[k] for a in attempts) for k in ("wall_ms", "tool_calls", "mcp_ms", "exec_ms", "model_wait_ms", "rounds", "input_tokens_resent", "output_tokens")}
    return {"attempts": attempts, "children": child_summary, "total": total}


def render(result: dict) -> str:
    out = []
    t = result["total"]
    out.append(f"attempts={len(result['attempts'])} wall={t['wall_ms']/1000:.0f}s rounds={t['rounds']} tool_calls={t['tool_calls']} "
               f"model_wait={t['model_wait_ms']/1000:.0f}s mcp_tools={t['mcp_ms']/1000:.0f}s exec={t['exec_ms']/1000:.0f}s "
               f"input_tokens_resent={t['input_tokens_resent']} output_tokens={t['output_tokens']}")
    for i, a in enumerate(result["attempts"], 1):
        share = (a["model_wait_ms"] / a["wall_ms"] * 100) if a["wall_ms"] else 0
        out.append(f"  [{i}] wall={a['wall_ms']/1000:.0f}s rounds={a['rounds']} calls={a['tool_calls']} model_wait={a['model_wait_ms']/1000:.0f}s({share:.0f}%) "
                   f"mcp={a['mcp_ms']/1000:.0f}s exec={a['exec_ms']/1000:.0f}s input_resent={a['input_tokens_resent']} max_input={a['max_input_tokens']} tools={a['tools']}")
    c = result["children"]
    out.append(f"  children: threads={c['threads']} tool_calls={c['tool_calls']} mcp={c['mcp_ms']/1000:.0f}s")
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("conv_dir"); ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    res = analyze(read_threads(Path(a.conv_dir)))
    print(json.dumps(res, ensure_ascii=False, indent=1) if a.json else render(res))
    return 0


if __name__ == "__main__":
    sys.exit(main())
