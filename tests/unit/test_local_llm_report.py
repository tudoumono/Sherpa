"""ローカル LLM の調べ（`scripts/local_llm_report.py`）の判定が、数字どおりに立つか。"""
from __future__ import annotations

import importlib.util
import json
import pathlib
import sys

import pytest

pytestmark = pytest.mark.unit

ROOT = pathlib.Path(__file__).resolve().parents[2]
_spec = importlib.util.spec_from_file_location("local_llm_report", ROOT / "scripts" / "local_llm_report.py")
R = importlib.util.module_from_spec(_spec)
sys.modules["local_llm_report"] = R
_spec.loader.exec_module(R)


def _rollout(tmp_path, provider, inputs, calls=(), error=None, reasoning=0):
    rows = [{"type": "session_meta", "payload": {"model_provider": provider, "cli_version": "1"}}]
    for i, n in enumerate(inputs):
        info = {"last_token_usage": {"input_tokens": n}, "model_context_window": 100000,
                "total_token_usage": {"output_tokens": 5, "reasoning_output_tokens": reasoning}}
        rows.append({"type": "event_msg", "payload": {"type": "token_count", "info": info}})
    for i, (ns, name, out) in enumerate(calls):
        rows.append({"type": "response_item", "payload": {"type": "function_call", "namespace": ns, "name": name, "call_id": str(i)}})
        rows.append({"type": "response_item", "payload": {"type": "function_call_output", "call_id": str(i), "output": out}})
    done = {"type": "task_complete"}
    if error:
        done["error"] = {"message": error}
    rows.append({"type": "event_msg", "payload": done})
    p = tmp_path / "rollout-x.jsonl"
    p.write_text("\n".join(json.dumps(r) for r in rows))
    return R.rollout_stats(str(p))


def test_context_overflow_is_flagged(tmp_path):
    st = _rollout(tmp_path, "local-ollama", [900, 1000],
                  error="request (1010 tokens) exceeds the available context size (1000 tokens), try increasing it")
    js = R.judge(st, None)
    assert "枠で落ちた" in js and "枠がぎりぎり" in js


def test_many_tool_failures_are_flagged(tmp_path):
    bad = "unsupported call: sample_tool"
    st = _rollout(tmp_path, "local-ollama", [100, 200],
                  calls=[("", "sample_tool", bad), ("", "sample_tool", bad), ("mcp__sample", "es_search", "ok")], reasoning=3)
    js = R.judge(st, 10000)
    assert "道具の呼び出しの失敗が多い" in js
    assert st["fails"] == {"unsupported_call": 2}
    assert "検索の道具を使い分けていない" not in js


def test_cloud_turn_gets_no_judgement(tmp_path):
    st = _rollout(tmp_path, "openai", [900, 1000], calls=[("", "exec", "unsupported call: x")])
    assert R.judge(st, 1000) == []
