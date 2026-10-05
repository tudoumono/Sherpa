"""Phase2 A2 の単体テスト: Codex の mcp_tool_call(graph_neighbors) → UI カード(candidates) 復元。

`mcp._mcp_neighbors_from`（result JSON のパース堅牢性）と
`mcp._apply_codex_neighbors`（troubleshoot 限定の上書き＋name 重複排除＋summary 整合）を
Codex サブプロセス無しで直接検証する（A2 の回帰固定）。
"""
from __future__ import annotations

import inspect
import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa.providers.codex import mcp as MCP  # noqa: E402


def _item(neighbors):
    """mcp_server が返す graph_neighbors の完了 item 形（result.content[].text=JSON 文字列）。"""
    return {"result": {"content": [{"type": "text", "text": json.dumps({"neighbors": neighbors})}]}}


def test_neighbors_from_valid():
    ns = [{"name": "BILLINGJOB", "label": "Module", "role": "実装", "path": ["請求", "BILLINGJOB"]}]
    assert MCP._mcp_neighbors_from(_item(ns)) == ns


def test_neighbors_from_broken_or_empty():
    assert MCP._mcp_neighbors_from({}) == []                                       # result 無し
    assert MCP._mcp_neighbors_from({"result": {"content": []}}) == []              # content 空
    assert MCP._mcp_neighbors_from({"result": {"content": [{"text": "{bad"}]}}) == []   # 壊れ JSON
    # neighbors が list でない / payload が dict でない → []（.get で落とさない・RV LOW）
    assert MCP._mcp_neighbors_from({"result": {"content": [{"text": json.dumps({"neighbors": "x"})}]}}) == []
    assert MCP._mcp_neighbors_from({"result": {"content": [{"text": json.dumps([1, 2])}]}}) == []


def test_apply_overrides_troubleshoot_and_dedups():
    env = {"data": {"candidates": [{"name": "OLD"}]}, "summary": {"total": 1}}
    mcp = [{"name": "A"}, {"name": "A"}, {"name": "B"}, {"name": None}, "x"]    # 重複/None/非dict 混在
    MCP._apply_codex_neighbors(env, mcp, "troubleshoot")
    assert [c["name"] for c in env["data"]["candidates"]] == ["A", "B"]         # _gather 由来 OLD を上書き＋重複排除
    assert env["summary"]["total"] == 2                                          # summary も Codex 由来に整合


def test_apply_noop_for_non_troubleshoot_or_empty():
    env = {"data": {"candidates": [{"name": "OLD"}]}, "summary": {"total": 1}}
    MCP._apply_codex_neighbors(env, [{"name": "A"}], "qa")                         # qa は上書きしない
    assert env["data"]["candidates"] == [{"name": "OLD"}]
    MCP._apply_codex_neighbors(env, [], "troubleshoot")                           # 近傍無しは無変更
    assert env["data"]["candidates"] == [{"name": "OLD"}] and env["summary"]["total"] == 1


# ==== rv-periphery #11: 旧世代グラフの構造化エラー（mcp_server.py::handle が isError で返す）を
# `GraphSchemaEraError` へ再構成する `_graph_schema_era_from_item` ====

def _era_item(**kw):
    body = {"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"}
    body.update(kw)
    return {"result": {"content": [{"type": "text", "text": json.dumps(body)}], "isError": True}}


def test_graph_schema_era_from_item_reconstructs_error_from_structured_result():
    err = MCP._graph_schema_era_from_item(_era_item(), "v1", "troubleshoot")
    from sherpa.ingest.world_neo4j import GraphSchemaEraError
    assert isinstance(err, GraphSchemaEraError)
    assert err.world == "v1" and err.stored_era == "old-era" and err.lens == "troubleshoot"


def test_graph_schema_era_from_item_none_for_normal_result():
    """通常の（isError の無い）graph_neighbors 結果は None——`_mcp_neighbors_from` の対象のまま。"""
    assert MCP._graph_schema_era_from_item(_item([]), "v1", None) is None


def test_graph_schema_era_from_item_none_when_isError_but_different_code():
    """`isError: true` でも既知の `graph_reingest_required` 以外のコードは None
    （他のツールレベルエラー・例えば run_tool 自体のエラー dict を誤検知しない）。"""
    body = {"result": {"content": [{"text": json.dumps({"error": "unknown tool: x"})}], "isError": True}}
    assert MCP._graph_schema_era_from_item(body, "v1", None) is None


def test_graph_schema_era_from_item_none_for_broken_shapes():
    assert MCP._graph_schema_era_from_item({}, "v1", None) is None                       # result 無し
    assert MCP._graph_schema_era_from_item({"result": {}}, "v1", None) is None            # isError 無し
    assert MCP._graph_schema_era_from_item(
        {"result": {"content": [{"text": "{bad"}], "isError": True}}, "v1", None) is None   # 壊れ JSON


