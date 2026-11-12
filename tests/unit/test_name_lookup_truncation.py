"""`name_lookup` が候補・一致を上限で切ったとき、結果・調査の記録の区分・道具の呼び出しの記録が揃って「打ち切り」になること。"""
from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa import mcp_coverage_outcome, store, tool_call_log  # noqa: E402
from sherpa import mcp_server as M  # noqa: E402
from sherpa.parts.read import name_lookup  # noqa: E402
from sherpa.store import name_ledger  # noqa: E402
from tests._replace_row import offline_row   # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def world(monkeypatch):
    wid = "nametrunc_" + uuid.uuid4().hex[:8]
    monkeypatch.setenv("SHERPA_MCP_WORLD", wid)
    monkeypatch.delenv("SHERPA_MCP_SCOPE", raising=False)
    M._seen_tool_calls.clear()
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    M._seen_tool_calls.clear()


def _call(name: str) -> dict:
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "name_lookup", "arguments": {"name": name}}})
    return json.loads(resp["result"]["content"][0]["text"])


def _classify(body: dict) -> tuple[str, bool]:
    outcome = mcp_coverage_outcome.coverage_outcome("name_lookup", body, False)
    logged = tool_call_log.summarize_result("name_lookup", {}, body, False, {})["truncated"]
    return outcome, logged


def _near_rows(n: int) -> list:
    return [{"name": f"ZZNEAR{i:02d}", "kind": "Module", "source": "declared", "rel_path": f"gen1/src/N{i:02d}.cbl"}
            for i in range(1, n + 1)]


def test_near_candidates_over_limit_are_marked_truncated(world):
    name_ledger.replace_rows(world, _near_rows(name_lookup.CANDIDATE_MAX + 1), row=offline_row(world))
    body = _call("ZZNEAR00")
    assert (len(body["candidates"]), body["candidates_total"]) == (name_lookup.CANDIDATE_MAX, name_lookup.CANDIDATE_MAX + 1)
    assert body["candidates_truncated"] is True
    assert body["truncated"] is False
    assert _classify(body) == ("limit", True)


def test_near_candidates_within_limit_are_not_truncated(world):
    name_ledger.replace_rows(world, _near_rows(name_lookup.CANDIDATE_MAX), row=offline_row(world))
    body = _call("ZZNEAR00")
    assert body["candidates_truncated"] is False
    assert _classify(body) == ("hit", False)


def test_matches_over_limit_are_marked_truncated(world):
    n = name_lookup.MATCH_MAX + 1
    name_ledger.replace_rows(world, [
        {"name": "ZZSAME", "kind": "Module", "source": "declared", "rel_path": f"gen1/src/S{i:03d}.cbl"} for i in range(n)], row=offline_row(world))
    body = _call("ZZSAME")
    assert (len(body["matches"]), body["count"]) == (name_lookup.MATCH_MAX, n)
    assert body["truncated"] is True
    assert _classify(body) == ("limit", True)
