"""es_search が上限で切ったとき、取得した断片の数・返した資料の数・取得の上限を分けて申告する契約。
上限は資料の数ではなく断片の数で、同じ資料の断片が枠を埋めることがある。"""
from __future__ import annotations

import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
from sherpa import documents, es_index, mcp_server, tool_dispatch   # noqa: E402


def _same_doc_hits():
    return ([{"doc_id": "a.md", "line": i, "text": f"本文{i}", "ext": ".md"} for i in range(1, 5)]
            + [{"doc_id": "b.md", "line": 1, "text": "本文B", "ext": ".md"}])


def _patch(monkeypatch, hits):
    monkeypatch.setattr(documents, "existing_rels", lambda world, rels, **kw: set(rels))
    monkeypatch.setattr(es_index, "search", lambda world, q, scope_paths=None, k=20, layer=None, **kw: (hits, None))


def test_es_search_reports_chunks_docs_and_cap_when_one_doc_fills_the_cap(monkeypatch):
    _patch(monkeypatch, _same_doc_hits())
    view, _, _, _ = tool_dispatch.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=5)
    assert view["truncated"] is True
    assert view["fetched_chunks"] == 5 and view["returned_docs"] == 2 and view["fetch_cap"] == 5
    assert view["fetched_chunks"] > view["returned_docs"]


def test_es_search_report_survives_final_clip(monkeypatch):
    _patch(monkeypatch, _same_doc_hits())
    view, _, _, _ = tool_dispatch.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=5)
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "420")
    clipped, was_clipped = mcp_server._clip_tool_result(view, name="es_search", args={})
    assert was_clipped is True and len(clipped["hits"]) < len(view["hits"])
    assert (clipped["fetched_chunks"], clipped["returned_docs"], clipped["fetch_cap"]) == (5, 2, 5)


def _final_returned(monkeypatch, view, budget=None, drop_docs=()):
    """MCP の出口（最終のクリップと管理者の除外の濾過）を通した結果を返す。"""
    monkeypatch.setenv("SHERPA_MCP_WORLD", "v1")
    monkeypatch.setattr(mcp_server.tool_dispatch, "run_tool", lambda *a, **kw: (view, set(), [], []))
    if budget:
        monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", str(budget))
    if drop_docs:
        def _drop(obj, world):
            return {**obj, "hits": [h for h in obj["hits"] if h["doc_id"] not in drop_docs]}
        monkeypatch.setattr("sherpa.path_rules.filter_output", _drop)
    mcp_server._seen_tool_calls.clear()
    resp = mcp_server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                              "params": {"name": "es_search", "arguments": {"query": "x"}}})
    return json.loads(resp["result"]["content"][0]["text"])


def _docs_view():
    hits = [{"doc_id": f"d{i}.md", "line": 1, "text": "x" * 200} for i in range(6)]
    return {"hits": hits, "truncated": True, "fetched_chunks": 6, "returned_docs": 6, "fetch_cap": 6}


def test_returned_docs_is_recounted_after_final_clip(monkeypatch):
    out = _final_returned(monkeypatch, _docs_view(), budget=700)
    n = len({h["doc_id"] for h in out["hits"]})
    assert 0 < n < 6 and out["returned_docs"] == n
    assert (out["fetched_chunks"], out["fetch_cap"]) == (6, 6)


def test_returned_docs_is_recounted_after_admin_exclusion(monkeypatch):
    out = _final_returned(monkeypatch, _docs_view(), drop_docs={"d0.md", "d1.md"})
    assert len(out["hits"]) == 4 and out["returned_docs"] == 4
    assert (out["fetched_chunks"], out["fetch_cap"]) == (6, 6)


def test_returned_docs_is_recounted_on_the_simple_chat_exit(monkeypatch):
    """簡易チャットは MCP を通らず run_tool の結果を AI へ渡す。管理者の除外で資料が落ちたら、そこで数え直す。"""
    from sherpa import path_rules
    _patch(monkeypatch, _same_doc_hits())
    rules = path_rules.Rules([path_rules.Rule("zz/other.md", path_rules.KIND_EXCLUDE)])  # 出口の濾過が動く設定（中身の除外は濾過の差し替えで起こす）
    monkeypatch.setattr(path_rules, "rules_for", lambda world: rules)
    monkeypatch.setattr(path_rules, "filter_output", lambda obj, world: (
        {**obj, "hits": [h for h in obj["hits"] if h.get("doc_id") != "b.md"]} if isinstance(obj, dict) and "hits" in obj else obj))
    view, _, _, _ = tool_dispatch.run_tool("es_search", {"query": "x"}, "v1", None, max_hits=5)
    assert {h["doc_id"] for h in view["hits"]} == {"a.md"} and view["returned_docs"] == 1
    assert (view["fetched_chunks"], view["fetch_cap"]) == (5, 5)
