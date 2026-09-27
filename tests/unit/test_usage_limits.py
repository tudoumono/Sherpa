"""利用統計の経路別制限計測契約。"""
from __future__ import annotations

import pytest

from sherpa.store import usage

pytestmark = pytest.mark.unit


def _row(provider: str) -> dict:
    row = {"provider": provider, "turns": 3}
    for field, kind in usage._USAGE_LIMIT_FIELD_KINDS.items():
        row[f"{field}_turns"] = 0
        if kind == "count":
            row[f"{field}_total"] = 0
    return row


def test_limits_use_explicit_provider_contract_and_ignore_unknown_providers():
    codex = _row("codex")
    codex["context_compactions_turns"] = 2
    codex["context_compactions_total"] = 4
    codex["synthesis_truncated_turns"] = 1
    codex["depth_escalated_turns"] = 1

    codex_out = usage._usage_limits_provider_row(codex)
    assert codex_out["tool_result_clipped_turns"] == 0
    for field in ("context_compactions", "synthesis_truncated", "depth_escalated"):
        assert codex_out[f"{field}_turns"] is None
    assert codex_out["context_compactions_total"] is None
    assert codex_out["duplicate_tool_call_turns"] == 0

    api = _row("openai")
    api["context_compactions_turns"] = 0
    api["context_compactions_total"] = 0
    api["duplicate_tool_call_turns"] = 3
    api["duplicate_tool_call_total"] = 5
    api_out = usage._usage_limits_provider_row(api)
    assert api_out["context_compactions_turns"] == 0
    assert api_out["context_compactions_total"] == 0
    assert api_out["duplicate_tool_call_turns"] is None
    assert api_out["duplicate_tool_call_total"] is None
    # イベント時だけ保存するフラグも、既知API経路では未発生の0件を保つ。
    assert api_out["synthesis_truncated_turns"] == 0
    assert api_out["tool_calls_exhausted_turns"] is None

    for provider in ("ollama", "gemini", "bedrock"):
        row = _row(provider)
        assert usage._usage_limits_provider_row(row)["context_compactions_turns"] == 0

    unknown = _row("unknown")
    unknown["tool_result_clipped_turns"] = 9
    unknown["tool_result_clipped_total"] = 12
    unknown_out = usage._usage_limits_provider_row(unknown)
    assert unknown_out["turns"] == unknown["turns"]
    assert all(value is None for key, value in unknown_out.items() if key not in ("provider", "turns"))
