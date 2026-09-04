"""Sherpa MCP サーバ（stdio・自前実装）の単体テスト。Codex 不要＝JSON-RPC ハンドラと serve ループを直接検証。

ツール実装は agentic_search.run_tool を再利用（v1 フィクスチャの filesystem grep・Neo4j 不要）。
"""
from __future__ import annotations

import io
import json
import os

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
os.environ["SHERPA_MCP_WORLD"] = "v1"
os.environ.pop("SHERPA_MCP_SCOPE", None)
from sherpa import mcp_server as M   # noqa: E402
import _corpus_expect as CE   # noqa: E402   # フィクスチャ実走査ベースの list_docs 期待値（フェーズ7 S1）


def test_initialize_handshake():
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize",
                     "params": {"protocolVersion": "2025-06-18", "capabilities": {}}})
    assert resp["id"] == 1
    r = resp["result"]
    assert r["protocolVersion"] == "2025-06-18"             # クライアント要求を返す
    assert "tools" in r["capabilities"] and r["serverInfo"]["name"] == "sherpa"


def test_notification_no_response():
    assert M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_tools_list_exposes_family():
    resp = M.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    names = {t["name"] for t in resp["result"]["tools"]}
    # S2（ask_user-improvements.md）: ask_user も Codex に公開（旧「非公開」から変更）。
    assert {"list_docs", "ripgrep_search", "read_around", "graph_neighbors", "ask_user"} <= names
    for t in resp["result"]["tools"]:                       # 各ツールに JSON schema がある
        assert t["inputSchema"]["type"] == "object"
    ask = next(t for t in resp["result"]["tools"] if t["name"] == "ask_user")
    from sherpa import agentic_search
    assert ask["description"] == agentic_search._DESC_ASK    # 制約文言は agentic と同一（二重管理しない）


def test_tools_call_ask_user_first_then_second_within_execution():
    """S2 ガード③: 1実行1回。MCP サーバは codex exec 1回＝1プロセスなので、1回目と2回目で
    別のツール結果文言を返す（ラッパー側も2回目を無視するが、ここは Codex が受け取る本文の検証）。"""
    M._ASK_STATE["count"] = 0                               # プロセス寿命カウンタ＝実行の頭でリセット
    args = {"prompt": "対象範囲は？", "mode": "single",
            "options": [{"label": "A"}, {"label": "B"}]}
    r1 = M.handle({"jsonrpc": "2.0", "id": 10, "method": "tools/call",
                   "params": {"name": "ask_user", "arguments": args}})
    r2 = M.handle({"jsonrpc": "2.0", "id": 11, "method": "tools/call",
                   "params": {"name": "ask_user", "arguments": args}})
    assert r1["result"]["isError"] is False and r2["result"]["isError"] is False
    assert M._ASK_RESULT_FIRST in r1["result"]["content"][0]["text"]
    assert M._ASK_RESULT_AGAIN in r2["result"]["content"][0]["text"]
    assert "既に質問済み" in r2["result"]["content"][0]["text"]


def test_ask_disabled_env_hides_tool_and_forces_again_reply():
    """S2 RV HIGH（2026-07-07）: 確認ID 付き再送実行では SHERPA_MCP_ASK_DISABLED=1 が立つ
    （agents._mcp_env の ask_disabled 引数経由）。このとき (a) tools/list に ask_user が出ない
    （呼べる道具を最初から見せない）、(b) それでも tools/call で呼ばれたら**初回でも**
    _ASK_RESULT_AGAIN を返す（プロンプト指示に反して呼ばれた場合の防御・質問カードを出さず
    調査を打ち切らせない）。実行ベース（env を実際に立てて handle() を直接叩く）で固定する。"""
    os.environ["SHERPA_MCP_ASK_DISABLED"] = "1"
    M._ASK_STATE["count"] = 0
    try:
        assert M._ask_disabled() is True
        resp = M.handle({"jsonrpc": "2.0", "id": 20, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        assert "ask_user" not in names
        assert {"list_docs", "ripgrep_search", "read_around", "graph_neighbors"} <= names

        call = M.handle({"jsonrpc": "2.0", "id": 21, "method": "tools/call",
                         "params": {"name": "ask_user", "arguments": {
                             "prompt": "無視されるはず", "mode": "single",
                             "options": [{"label": "A"}, {"label": "B"}]}}})
        assert call["result"]["isError"] is False
        assert call["result"]["content"][0]["text"] == M._ASK_RESULT_AGAIN   # 初回でも「既に質問済み」扱い
        assert M._ASK_STATE["count"] == 0                                   # 1実行1回カウンタは消費しない
    finally:
        os.environ.pop("SHERPA_MCP_ASK_DISABLED", None)


def test_tools_call_ripgrep_on_fixtures():
    resp = M.handle({"jsonrpc": "2.0", "id": 3, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "TAX-RATE"}}})
    content = resp["result"]["content"]
    assert content[0]["type"] == "text"
    payload = json.loads(content[0]["text"])
    assert payload["hits"] and not resp["result"]["isError"]


def test_tools_call_list_docs_on_fixtures():
    """S1: MCP 経由でも list_docs が台帳の一覧/件数を返す（Codex 側の同じ穴を塞ぐ）。"""
    resp = M.handle({"jsonrpc": "2.0", "id": 6, "method": "tools/call",
                     "params": {"name": "list_docs", "arguments": {"path_prefix": "4期/02_設計"}}})
    content = resp["result"]["content"]
    payload = json.loads(content[0]["text"])
    assert not resp["result"]["isError"]
    expected = CE.count_under("4期/02_設計")                        # fixtures 実走査由来（フェーズ7 S1）
    assert payload["count"] == expected and len(payload["docs"]) == expected
    assert all(d["rel_path"].startswith("4期/02_設計/") for d in payload["docs"])


def test_tools_call_graph_neighbors_stubbed():
    from sherpa import lens_service
    fake = [{"name": "BILLINGJOB", "label": "Module", "category": "プログラム", "role": "実装",
             "distance": 2, "path": ["請求", "BILLINGJOB"], "evidence": {"edges": [], "grep": []}}]
    orig = lens_service.neighbor_cards
    lens_service.neighbor_cards = lambda world, term, sp=None: list(fake)
    try:
        resp = M.handle({"jsonrpc": "2.0", "id": 4, "method": "tools/call",
                         "params": {"name": "graph_neighbors", "arguments": {"name": "請求"}}})
        payload = json.loads(resp["result"]["content"][0]["text"])
        assert payload["neighbors"][0]["name"] == "BILLINGJOB"   # UI 用に name/role/path を含む compact view
        assert payload["neighbors"][0]["role"] == "実装" and payload["neighbors"][0]["path"]
    finally:
        lens_service.neighbor_cards = orig


def test_unknown_method_errors():
    resp = M.handle({"jsonrpc": "2.0", "id": 5, "method": "bogus/method"})
    assert resp["error"]["code"] == -32601


def test_world_and_scope_from_env():
    os.environ["SHERPA_MCP_SCOPE"] = "4期\n00_共通"
    try:
        assert M._world() == "v1" and M._scope() == ["4期", "00_共通"]
    finally:
        os.environ.pop("SHERPA_MCP_SCOPE", None)
    assert M._scope() is None                               # 未設定は None（全体）


# ===== 探す対象（層フィルタ） =====

def test_layer_from_env():
    os.environ["SHERPA_MCP_LAYER"] = "code"
    try:
        assert M._layer() == "code"
    finally:
        os.environ.pop("SHERPA_MCP_LAYER", None)
    assert M._layer() is None                               # 未設定は None（both 扱い）


def test_tools_call_forwards_layer_to_run_tool(monkeypatch):
    """`tools/call` は起動時 env の layer を `agentic_search.run_tool` へそのまま転送する。"""
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured["layer"] = kw.get("layer")
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(M.agentic_search, "run_tool", fake_run_tool)
    os.environ["SHERPA_MCP_LAYER"] = "docs"
    try:
        M.handle({"jsonrpc": "2.0", "id": 30, "method": "tools/call",
                  "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    finally:
        os.environ.pop("SHERPA_MCP_LAYER", None)
    assert captured.get("layer") == "docs"


def test_tools_call_ripgrep_respects_layer_on_fixtures():
    """実 fixtures 上で layer=code を渡すと資料（.md）ヒットが除外される
    （"TAX-RATE" は .md と .cbl/.cpy の両方に実在する語・test_agentic_search.py と同じ前提）。"""
    os.environ["SHERPA_MCP_LAYER"] = "code"
    try:
        resp = M.handle({"jsonrpc": "2.0", "id": 31, "method": "tools/call",
                         "params": {"name": "ripgrep_search", "arguments": {"query": "TAX-RATE"}}})
    finally:
        os.environ.pop("SHERPA_MCP_LAYER", None)
    payload = json.loads(resp["result"]["content"][0]["text"])
    import pathlib
    exts = {pathlib.Path(h["doc_id"]).suffix.lower() for h in payload["hits"]}
    assert exts and not (exts & {".md", ".markdown"})


def test_tools_list_hides_graph_neighbors_when_layer_restricted():
    """正典 §3.4: 層が限定されている間は tools/list に graph_neighbors 自体を出さない
    （呼べる道具を最初から見せない・ask_user の SHERPA_MCP_ASK_DISABLED と同じ思想）。"""
    for lyr in ("docs", "code"):
        os.environ["SHERPA_MCP_LAYER"] = lyr
        try:
            resp = M.handle({"jsonrpc": "2.0", "id": 40, "method": "tools/list"})
            names = {t["name"] for t in resp["result"]["tools"]}
        finally:
            os.environ.pop("SHERPA_MCP_LAYER", None)
        assert "graph_neighbors" not in names, lyr
        assert {"list_docs", "ripgrep_search", "read_around"} <= names, lyr   # 他のツールは残る


def test_tools_list_keeps_graph_neighbors_when_layer_both_or_unset():
    for lyr in (None, "both"):
        if lyr is not None:
            os.environ["SHERPA_MCP_LAYER"] = lyr
        try:
            resp = M.handle({"jsonrpc": "2.0", "id": 41, "method": "tools/list"})
            names = {t["name"] for t in resp["result"]["tools"]}
        finally:
            os.environ.pop("SHERPA_MCP_LAYER", None)
        assert "graph_neighbors" in names, lyr


def test_tools_call_graph_neighbors_rejected_when_layer_restricted(monkeypatch):
    """tools/list から隠すだけでなく、直接 tools/call されても run_tool 側で拒否する（多層防御）。"""
    from sherpa import lens_service

    def _boom(world, term, sp=None):
        raise AssertionError("層限定なのに neighbor_cards が呼ばれている")

    monkeypatch.setattr(lens_service, "neighbor_cards", _boom)
    os.environ["SHERPA_MCP_LAYER"] = "code"
    try:
        resp = M.handle({"jsonrpc": "2.0", "id": 42, "method": "tools/call",
                         "params": {"name": "graph_neighbors", "arguments": {"name": "請求"}}})
    finally:
        os.environ.pop("SHERPA_MCP_LAYER", None)
    assert resp["result"]["isError"] is True
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert "error" in payload


def test_serve_loop_roundtrip():
    """serve() が改行区切り JSON-RPC を読み、応答を1行ずつ返す（通知は応答なし）。"""
    lines = [
        json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
        json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
        json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"}),
    ]
    out = io.StringIO()
    M.serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    assert [r["id"] for r in responses] == [1, 2]          # 通知には応答が無い＝2件だけ
    assert any(t["name"] == "ripgrep_search" for t in responses[1]["result"]["tools"])
