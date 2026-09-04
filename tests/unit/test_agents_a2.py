"""Phase2 A2 の単体テスト: Codex の mcp_tool_call(graph_neighbors) → UI カード(candidates) 復元。

`agents._mcp_neighbors_from`（result JSON のパース堅牢性）と
`agents._apply_codex_neighbors`（troubleshoot 限定の上書き＋name 重複排除＋summary 整合）を
Codex サブプロセス無しで直接検証する（A2 の回帰固定）。
"""
from __future__ import annotations

import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
from sherpa import agents as A  # noqa: E402


def _item(neighbors):
    """mcp_server が返す graph_neighbors の完了 item 形（result.content[].text=JSON 文字列）。"""
    return {"result": {"content": [{"type": "text", "text": json.dumps({"neighbors": neighbors})}]}}


def test_neighbors_from_valid():
    ns = [{"name": "BILLINGJOB", "label": "Module", "role": "実装", "path": ["請求", "BILLINGJOB"]}]
    assert A._mcp_neighbors_from(_item(ns)) == ns


def test_neighbors_from_broken_or_empty():
    assert A._mcp_neighbors_from({}) == []                                       # result 無し
    assert A._mcp_neighbors_from({"result": {"content": []}}) == []              # content 空
    assert A._mcp_neighbors_from({"result": {"content": [{"text": "{bad"}]}}) == []   # 壊れ JSON
    # neighbors が list でない / payload が dict でない → []（.get で落とさない・RV LOW）
    assert A._mcp_neighbors_from({"result": {"content": [{"text": json.dumps({"neighbors": "x"})}]}}) == []
    assert A._mcp_neighbors_from({"result": {"content": [{"text": json.dumps([1, 2])}]}}) == []


def test_apply_overrides_troubleshoot_and_dedups():
    env = {"data": {"candidates": [{"name": "OLD"}]}, "summary": {"total": 1}}
    mcp = [{"name": "A"}, {"name": "A"}, {"name": "B"}, {"name": None}, "x"]    # 重複/None/非dict 混在
    A._apply_codex_neighbors(env, mcp, "troubleshoot")
    assert [c["name"] for c in env["data"]["candidates"]] == ["A", "B"]         # _gather 由来 OLD を上書き＋重複排除
    assert env["summary"]["total"] == 2                                          # summary も Codex 由来に整合


def test_apply_noop_for_non_troubleshoot_or_empty():
    env = {"data": {"candidates": [{"name": "OLD"}]}, "summary": {"total": 1}}
    A._apply_codex_neighbors(env, [{"name": "A"}], "qa")                         # qa は上書きしない
    assert env["data"]["candidates"] == [{"name": "OLD"}]
    A._apply_codex_neighbors(env, [], "troubleshoot")                           # 近傍無しは無変更
    assert env["data"]["candidates"] == [{"name": "OLD"}] and env["summary"]["total"] == 1
