"""Phase2 A2 の単体テスト: Codex の mcp_tool_call(graph_neighbors) → UI カード(candidates) 復元。

`agents._mcp_neighbors_from`（result JSON のパース堅牢性）と
`agents._apply_codex_neighbors`（troubleshoot 限定の上書き＋name 重複排除＋summary 整合）を
Codex サブプロセス無しで直接検証する（A2 の回帰固定）。
"""
from __future__ import annotations

import inspect
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


# ==== rv-periphery #11: 旧世代グラフの構造化エラー（mcp_server.py::handle が isError で返す）を
# `GraphSchemaEraError` へ再構成する `_graph_schema_era_from_item` ====

def _era_item(**kw):
    body = {"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"}
    body.update(kw)
    return {"result": {"content": [{"type": "text", "text": json.dumps(body)}], "isError": True}}


def test_graph_schema_era_from_item_reconstructs_error_from_structured_result():
    err = A._graph_schema_era_from_item(_era_item(), "v1", "troubleshoot")
    from sherpa.ingest.world_neo4j import GraphSchemaEraError
    assert isinstance(err, GraphSchemaEraError)
    assert err.world == "v1" and err.stored_era == "old-era" and err.lens == "troubleshoot"


def test_graph_schema_era_from_item_none_for_normal_result():
    """通常の（isError の無い）graph_neighbors 結果は None——`_mcp_neighbors_from` の対象のまま。"""
    assert A._graph_schema_era_from_item(_item([]), "v1", None) is None


def test_graph_schema_era_from_item_none_when_isError_but_different_code():
    """`isError: true` でも既知の `graph_reingest_required` 以外のコードは None
    （他のツールレベルエラー・例えば run_tool 自体のエラー dict を誤検知しない）。"""
    body = {"result": {"content": [{"text": json.dumps({"error": "unknown tool: x"})}], "isError": True}}
    assert A._graph_schema_era_from_item(body, "v1", None) is None


def test_graph_schema_era_from_item_none_for_broken_shapes():
    assert A._graph_schema_era_from_item({}, "v1", None) is None                       # result 無し
    assert A._graph_schema_era_from_item({"result": {}}, "v1", None) is None            # isError 無し
    assert A._graph_schema_era_from_item(
        {"result": {"content": [{"text": "{bad"}], "isError": True}}, "v1", None) is None   # 壊れ JSON


# ==== rv-periphery #7/#11: `_run_authoring` の配線（Popen 必須・ソース検査・
# test_agents_ask_user.py と同じ既存慣行）====

def _src():
    return inspect.getsource(A.CodexProvider._run_authoring)


def test_tlabel_dict_includes_folder_tree_and_compare_documents():
    """RV是正（rv-periphery #7）: folder_tree/compare_documents は MCP 経由で Codex にも
    公開済みだが、mcp_tool_call の表示用ラベル辞書（`tlabel`）に対応が無く「その他の処理」の
    汎用ラベルに丸まっていた（`improvement_log._TOOL_CALL_LABELS` の集計対象からも漏れる）。"""
    src = _src()
    assert '"folder_tree": "フォルダ構成を確認"' in src
    assert '"compare_documents": "世代間の差分を比較"' in src


def test_graph_schema_era_detection_wired_before_neighbors_extraction():
    """RV是正（rv-periphery #11）: graph_neighbors の mcp_tool_call item を処理する際、
    `_graph_schema_era_from_item` を先に見て、検知しなければ従来どおり `_mcp_neighbors_from`
    を呼ぶ（era エラーを近傍データとして誤って読まない）。"""
    src = _src()
    i_check = src.index("_graph_schema_era_from_item(")
    i_neighbors = src.index("_mcp_neighbors_from(item)")
    assert i_check < i_neighbors, "era 検知が近傍抽出より後に配線されている"


def test_graph_schema_era_error_raised_after_swallowing_try_blocks():
    """RV是正（rv-periphery #11）: 検知した `_graph_schema_era_error` は、mcp_tool_call を処理する
    `for line in proc.stdout:` を包む2重の `except Exception:`（技術的失敗を `_stream_error` へ
    丸める・_attempt 自身とこの呼び出し元）を両方抜けた後で re-raise する——そのブロック内で
    直接 raise すると握り潰されてしまうため。"""
    src = _src()
    i_flag_set = src.index("_graph_schema_era_error = _era_err")
    i_raise = src.index("raise _graph_schema_era_error")
    assert i_raise > i_flag_set
    # ask_user の question 優先分岐（`if codex_question is not None:`）と同様、finally の後・
    # 通常の後処理（成果物台帳登録等）より前で判定する——この文言はループ内（ask_user 捕捉時）
    # にも1回出るため、最後（final check）の出現位置で比較する。
    i_question_check = src.rindex("if codex_question is not None:")
    assert i_raise < i_question_check
