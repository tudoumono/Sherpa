"""道具 `name_lookup` が MCP に出ること（一覧・説明・呼び出し・調査の台帳への記録）。実 Postgres・グラフは使わない。"""
from __future__ import annotations

import json
import os
import uuid

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")

from sherpa import agentic_search, mcp_coverage_outcome, store  # noqa: E402
from sherpa import mcp_server as M  # noqa: E402
from sherpa.store import name_ledger  # noqa: E402
from tests._replace_row import offline_row   # noqa: E402

pytestmark = pytest.mark.unit


@pytest.fixture
def world(monkeypatch):
    wid = "namemcp_" + uuid.uuid4().hex[:8]
    name_ledger.replace_rows(wid, [
        {"name": "ZZMPROG0", "kind": "Module", "source": "declared", "rel_path": "gen1/src/ZZMPROG1.cbl"},
        {"name": "ZZMPROG1", "kind": "Module", "source": "filename", "rel_path": "gen1/src/ZZMPROG1.cbl"}], row=offline_row(wid))
    monkeypatch.setenv("SHERPA_MCP_WORLD", wid)
    monkeypatch.delenv("SHERPA_MCP_SCOPE", raising=False)
    M._seen_tool_calls.clear()
    yield wid
    store.replace_documents(wid, [], row=offline_row(wid))
    M._seen_tool_calls.clear()


def _rpc(method, params=None):
    msg = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        msg["params"] = params
    return M.handle(msg)


def test_name_lookup_is_listed_with_shared_schema():
    tools = {t["name"]: t for t in _rpc("tools/list")["result"]["tools"]}
    assert tools["name_lookup"]["description"] == agentic_search._DESC_NAME_LOOKUP
    assert tools["name_lookup"]["inputSchema"] == agentic_search._PARAMS_NAME_LOOKUP
    assert tools["name_lookup"]["inputSchema"]["required"] == ["name"]


def test_name_lookup_call_returns_declared_match(world):
    resp = _rpc("tools/call", {"name": "name_lookup", "arguments": {"name": "ZZMPROG0"}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert resp["result"]["isError"] is False
    assert [(m["source"], m["rel_path"]) for m in body["matches"]] == [("declared", "gen1/src/ZZMPROG1.cbl")]


def test_name_lookup_outcome_for_investigation_ledger():
    out = mcp_coverage_outcome.coverage_outcome
    assert out("name_lookup", {"matches": [{"rel_path": "a"}]}, False) == "hit"
    assert out("name_lookup", {"matches": [], "candidates": [{"name": "A1"}]}, False) == "hit"
    assert out("name_lookup", {"matches": [], "candidates": []}, False) == "no_hits"
    assert out("name_lookup", {"matches": [], "degrade_reason": "name_ledger_empty"}, False) == "error"
    assert out("name_lookup", {"matches": [1], "truncated": True}, False) == "limit"
