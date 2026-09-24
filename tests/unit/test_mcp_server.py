"""Sherpa MCP サーバ（stdio・自前実装）の単体テスト。Codex 不要＝JSON-RPC ハンドラと serve ループを直接検証。

ツール実装は agentic_search.run_tool を再利用（v1 フィクスチャの filesystem grep・Neo4j 不要）。
"""
from __future__ import annotations

import io
import json
import os

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
os.environ["SHERPA_MCP_WORLD"] = "v1"
os.environ.pop("SHERPA_MCP_SCOPE", None)
from sherpa import mcp_server as M   # noqa: E402
import _corpus_expect as CE   # noqa: E402   # フィクスチャ実走査ベースの list_docs 期待値（フェーズ7 S1）


def _ledger_call(name, arguments):
    response = M.handle({"jsonrpc": "2.0", "id": 400, "method": "tools/call",
                         "params": {"name": name, "arguments": arguments}})["result"]
    body = json.loads(response["content"][0]["text"])
    assert response["isError"] is bool(body.get("error"))
    return body


def _ledger_item(item_id="a", **changes):
    return {"id": item_id, "kind": "row", "subject": "対象", "required_checks": ["source"],
            "evidence": [{"kind": "source", "path": "src/a.py", "line": 1}],
            "status": "source_confirmed", "reason": "", "owner": "parent", **changes}


def test_ledger_tools_exposed_with_exact_input_fields():
    response = M.handle({"jsonrpc": "2.0", "id": 401, "method": "tools/list"})
    definitions = {tool["name"]: tool for tool in response["result"]["tools"]}
    expected = {"ledger_manifest_set": {"question_kind", "items"},
                "ledger_item_put": set(_ledger_item()), "ledger_status": set()}
    for name, fields in expected.items():
        schema = definitions[name]["inputSchema"]
        assert set(schema["properties"]) == fields
        assert set(schema.get("required", [])) == fields
        assert schema["additionalProperties"] is False
        assert "ファイルを直接書かず" in definitions[name]["description"]


def test_ledger_tools_persist_and_preserve_created_at(tmp_path, monkeypatch):
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    fresh = _ledger_call("ledger_status", {})
    assert fresh["manifest_invalid"] is True and fresh["items"] == 0
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}
    created_at = json.loads((directory / "manifest.json").read_text())["created_at"]
    assert created_at
    assert _ledger_call("ledger_status", {})["missing"] == ["a"]
    item = _ledger_item()
    assert _ledger_call("ledger_item_put", item) == {"ok": True, "id": "a"}
    assert json.loads((directory / "items/a.json").read_text()) == item
    done = _ledger_call("ledger_status", {})
    assert done["complete"] is True
    assert done["counts"]["source_confirmed"] == 1
    assert _ledger_call("ledger_manifest_set", {"question_kind": "compare", "items": ["a", "b"]}) == {"ok": True}
    assert json.loads((directory / "manifest.json").read_text()) == {
        "question_kind": "compare", "items": ["a", "b"], "created_at": created_at}
    assert _ledger_call("ledger_status", {})["missing"] == ["b"]


def test_ledger_status_reports_unsatisfied_invalid_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a", "bad", "missing"]})
    item = _ledger_item(status="spec_only", required_checks=["source", "spec_doc"],
                        evidence=[{"kind": "spec_doc", "path": "docs/spec.md", "line": 2}])
    assert _ledger_call("ledger_item_put", item) == {"ok": True, "id": "a"}
    (tmp_path / "items/bad.json").write_text("{broken", encoding="utf-8")
    status = _ledger_call("ledger_status", {})
    assert status["complete"] is False
    assert status["manifest_invalid"] is False
    assert status["items"] == 2
    assert status["non_terminal"] == ["a"]
    assert status["unsatisfied"] == {"a": ["source"]}
    assert status["invalid"] == ["bad"]
    assert status["missing"] == ["missing"]
    assert status["counts"]["spec_only"] == 1

    # SHERPA_MCP_LEDGER_REQUIRED_EXTRA（provider.py がそのターンの範囲判定から渡す）が設定されて
    # いると、item 自身が required_checks に source を宣言していなくても ledger_status の
    # unsatisfied に source が載る——Codex の自己確認（final を出す前の ledger_status 呼び出し）でも
    # provider 側のゲートと同じ結論になる。
    monkeypatch.setenv("SHERPA_MCP_LEDGER_REQUIRED_EXTRA", "source")
    assert _ledger_call("ledger_manifest_set",
                        {"question_kind": "list", "items": ["a", "bad", "missing", "c"]}) == {"ok": True}
    item_no_source_declared = _ledger_item(
        item_id="c", status="spec_only", required_checks=["spec_doc"],
        evidence=[{"kind": "spec_doc", "path": "docs/other.md", "line": 1}])
    assert _ledger_call("ledger_item_put", item_no_source_declared) == {"ok": True, "id": "c"}
    status_with_required_extra = _ledger_call("ledger_status", {})
    assert status_with_required_extra["unsatisfied"]["c"] == ["source"]


@pytest.mark.parametrize("arguments", [{"question_kind": "list"},
                                      {"question_kind": "", "items": ["a"]},
                                      {"question_kind": "list", "items": [1]}])
def test_ledger_manifest_invalid_returns_problems_without_writing(tmp_path, monkeypatch, arguments):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    result = _ledger_call("ledger_manifest_set", arguments)
    assert result["error"] == "ledger_manifest_invalid"
    assert result["problems"]
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("changes", [{"status": "done"}, {"required_checks": []},
                                    {"evidence": []}, {"extra": "body"},
                                    {"id": "../outside"}, {"id": "a/b"}, {"id": ""}, {"id": ".hidden"}])
def test_ledger_item_invalid_returns_problems_without_replacing_item(tmp_path, monkeypatch, changes):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    item = _ledger_item()
    _ledger_call("ledger_item_put", item)
    result = _ledger_call("ledger_item_put", {**item, **changes})
    assert result["error"] == "ledger_item_invalid"
    assert result["problems"]
    assert json.loads((tmp_path / "items/a.json").read_text()) == item
    assert [path.name for path in (tmp_path / "items").iterdir()] == ["a.json"]


@pytest.mark.parametrize("name", ["ledger_manifest_set", "ledger_item_put", "ledger_status"])
def test_ledger_tools_require_directory_env(monkeypatch, name):
    monkeypatch.delenv("SHERPA_MCP_LEDGER_DIR", raising=False)
    assert _ledger_call(name, {}) == {"error": "ledger_unavailable"}


def test_ledger_tools_exempt_from_clipping_duplicates_and_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path / "investigation"))
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "1")
    sidecar = tmp_path / "sidecar.jsonl"
    sidecar.write_text("", encoding="utf-8")
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    for name, arguments in [("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}),
                            ("ledger_item_put", _ledger_item()), ("ledger_status", {})]:
        first = _ledger_call(name, arguments)
        assert _ledger_call(name, arguments) == first
        assert "error" not in first and "truncated" not in first
    assert first["complete"] is True
    assert sidecar.read_text() == ""


@pytest.mark.parametrize("name,arguments", [
    ("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}),
    ("ledger_item_put", _ledger_item()),
])
def test_ledger_write_failure_returns_error_and_log(tmp_path, monkeypatch, capsys, name, arguments):
    destination = tmp_path / "not-a-directory"
    destination.write_text("keep", encoding="utf-8")
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(destination))
    result = _ledger_call(name, arguments)
    assert result["error"] == "ledger_write_failed"
    assert result["problems"]
    assert f"{name} failed:" in capsys.readouterr().err
    assert destination.read_text() == "keep"


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


def test_tools_list_exposes_read_doc_outline_glob_search():
    """read_doc/doc_outline/glob_search は read_around 等と同じ土台系＝常に公開する
    （schema/description は agentic_search と共通・二重管理しない）。"""
    resp = M.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/list"})
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"read_doc", "doc_outline", "glob_search"} <= names
    from sherpa import agentic_search
    byname = {t["name"]: t for t in tools}
    assert byname["read_doc"]["description"] == agentic_search._DESC_READ_DOC
    assert byname["read_doc"]["inputSchema"] == agentic_search._PARAMS_READ_DOC
    assert byname["doc_outline"]["description"] == agentic_search._DESC_OUTLINE
    assert byname["doc_outline"]["inputSchema"] == agentic_search._PARAMS_OUTLINE
    assert byname["glob_search"]["description"] == agentic_search._DESC_GLOB
    assert byname["glob_search"]["inputSchema"] == agentic_search._PARAMS_GLOB


def test_tools_list_order_stable_with_and_without_es(monkeypatch):
    """es_search の有無に関わらず、他の土台系ツールの並びが崩れない
    （es_search は名前で ripgrep_search の位置を探して直後に差し込むだけ）。"""
    foundation = ["list_docs", "folder_tree", "ripgrep_search", "glob_search", "doc_outline",
                 "read_doc", "read_around", "compare_documents"]

    monkeypatch.setattr(M.es_index, "available", lambda: False)
    resp = M.handle({"jsonrpc": "2.0", "id": 8, "method": "tools/list"})
    names_no_es = [t["name"] for t in resp["result"]["tools"]]
    assert "es_search" not in names_no_es
    assert [n for n in names_no_es if n in foundation] == foundation

    monkeypatch.setattr(M.es_index, "available", lambda: True)
    resp2 = M.handle({"jsonrpc": "2.0", "id": 9, "method": "tools/list"})
    names_es = [t["name"] for t in resp2["result"]["tools"]]
    assert names_es.index("es_search") == names_es.index("ripgrep_search") + 1
    assert [n for n in names_es if n in foundation] == foundation


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


def test_tools_call_read_doc_and_doc_outline_on_fixtures():
    """MCP 経由でも read_doc（通読）・doc_outline（見出し構造）が結果を返す
    （既存の test_tools_call_ripgrep_on_fixtures と同じ流儀・doc_id は実ヒットから取る）。"""
    hit_resp = M.handle({"jsonrpc": "2.0", "id": 50, "method": "tools/call",
                         "params": {"name": "ripgrep_search", "arguments": {"query": "TAX-RATE"}}})
    hit_payload = json.loads(hit_resp["result"]["content"][0]["text"])
    doc_id = hit_payload["hits"][0]["doc_id"]

    resp = M.handle({"jsonrpc": "2.0", "id": 51, "method": "tools/call",
                     "params": {"name": "read_doc", "arguments": {"doc_id": doc_id}}})
    assert not resp["result"]["isError"]
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["doc_id"] == doc_id and "text" in payload and "total_lines" in payload

    resp2 = M.handle({"jsonrpc": "2.0", "id": 52, "method": "tools/call",
                      "params": {"name": "doc_outline", "arguments": {"doc_id": doc_id}}})
    assert not resp2["result"]["isError"]
    payload2 = json.loads(resp2["result"]["content"][0]["text"])
    assert payload2["doc_id"] == doc_id and "headings" in payload2


def test_tools_call_glob_search_on_fixtures():
    """MCP 経由で glob_search がファイル名パターン一致のパス一覧を返す。"""
    resp = M.handle({"jsonrpc": "2.0", "id": 53, "method": "tools/call",
                     "params": {"name": "glob_search", "arguments": {"pattern": "*.md"}}})
    assert not resp["result"]["isError"]
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload["count"] > 0 and payload["paths"]
    assert all(p.lower().endswith(".md") for p in payload["paths"])


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


def test_tools_call_graph_schema_era_error_returns_structured_tool_error(monkeypatch):
    """RV是正（rv-periphery #11・2026-09-05）: `graph_neighbors` が旧世代グラフ
    （`GraphSchemaEraError`）を検知したら、汎用の JSON-RPC プロトコルエラー（-32603）に丸めず、
    通常のツール結果と同じ経路（`isError: true`・`content[].text` に安定した機械可読コード
    `graph_reingest_required`）で返す——Codex 側の `item["result"]` にそのまま載せるため。"""
    def _boom(name, args, world, scope_paths, **kw):
        raise M.GraphSchemaEraError(world, "old-era", lens="troubleshoot")
    monkeypatch.setattr(M.agentic_search, "run_tool", _boom)
    resp = M.handle({"jsonrpc": "2.0", "id": 40, "method": "tools/call",
                     "params": {"name": "graph_neighbors", "arguments": {"name": "請求"}}})
    assert "error" not in resp                        # JSON-RPC プロトコルエラーではない
    assert resp["result"]["isError"] is True
    payload = json.loads(resp["result"]["content"][0]["text"])
    assert payload == {"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"}


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


@pytest.mark.usefixtures("upstream_only_registry")
def test_tools_call_ripgrep_respects_layer_on_fixtures():
    """実 fixtures 上で layer=code を渡すと資料（.md）ヒットが除外される
    （"TAX-RATE" は .md と .cbl/.cpy の両方に実在する語・test_agentic_search.py と同じ前提）。

    上流限定固定（`upstream_only_registry`）——フォークが `.md` を担当する拡張アナライザを登録すると
    `.md` が資料（office）ではなくコード（source）判定になり、layer=code から除外される前提
    （§ 開発ハーネス S4・敵対 RV 是正）が崩れるため。"""
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


def test_toolset_plain_exposes_only_three_and_rejects_others(monkeypatch):
    """素の Codex モード（`SHERPA_MCP_TOOLSET=plain`・docs/proposals/2026-09-24-素のCodexモード.md
    §3）は es_search・graph_neighbors・ask_user だけを tools/list に出す。それ以外（grep/読取/
    list_docs 系・台帳ツール）は直接 tools/call されても、tools/list に出していなくても存在しない
    ツールと同じエラーで拒否する（多層防御）。未設定（既定）はこのテストの外で全ツールが出ることを
    他のテストが固定済み。"""
    monkeypatch.setattr(M.es_index, "available", lambda: True)
    os.environ["SHERPA_MCP_TOOLSET"] = "plain"
    try:
        resp = M.handle({"jsonrpc": "2.0", "id": 60, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
        assert names == {"graph_neighbors", "ask_user"}   # es_search も出さない（埋め込みの待ちで遅い）
        # 公開しないツールの名前を説明文で指示しない（呼べば拒否されるだけ）。
        descs = " ".join(t["description"] for t in resp["result"]["tools"])
        assert not any(n in descs for n in ("ripgrep_search", "read_doc", "read_around", "list_docs"))

        rejected = M.handle({"jsonrpc": "2.0", "id": 61, "method": "tools/call",
                             "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
        assert rejected["result"]["isError"] is True
        assert json.loads(rejected["result"]["content"][0]["text"]) == {
            "error": "unknown tool: ripgrep_search"}

        rejected_ledger = M.handle({"jsonrpc": "2.0", "id": 62, "method": "tools/call",
                                    "params": {"name": "ledger_status", "arguments": {}}})
        assert rejected_ledger["result"]["isError"] is True
        assert json.loads(rejected_ledger["result"]["content"][0]["text"]) == {
            "error": "unknown tool: ledger_status"}
    finally:
        os.environ.pop("SHERPA_MCP_TOOLSET", None)


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


# ===== S3b: 原本読取ツール（`docs/proposals/2026-09-10-Codex原本直読と調査スキル.md` §2-9）=====

def test_tools_list_exposes_original_read_tools():
    """原本読取ツール6本が tools/list に出る（schema/description は agentic_search と共通）。"""
    resp = M.handle({"jsonrpc": "2.0", "id": 50, "method": "tools/list"})
    tools = resp["result"]["tools"]
    names = {t["name"] for t in tools}
    assert {"xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages", "file_head"} <= names
    from sherpa import agentic_search
    byname = {t["name"]: t for t in tools}
    assert byname["xlsx_range"]["description"] == agentic_search._DESC_XLSX_RANGE
    assert byname["xlsx_range"]["inputSchema"] == agentic_search._PARAMS_XLSX_RANGE
    assert byname["file_head"]["description"] == agentic_search._DESC_FILE_HEAD
    assert byname["file_head"]["inputSchema"] == agentic_search._PARAMS_FILE_HEAD


def test_tools_list_hides_office_read_tools_when_layer_code_but_keeps_file_head():
    """探す対象がソースに限定されている間は Office/PDF の5本を隠す（Office は常に docs 側扱い）。
    `file_head`（テキスト・コード）は層に関係なく公開したまま（`run_tool` 側が個別に絞る）。"""
    os.environ["SHERPA_MCP_LAYER"] = "code"
    try:
        resp = M.handle({"jsonrpc": "2.0", "id": 51, "method": "tools/list"})
        names = {t["name"] for t in resp["result"]["tools"]}
    finally:
        os.environ.pop("SHERPA_MCP_LAYER", None)
    for hidden in ("xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages"):
        assert hidden not in names, hidden
    assert "file_head" in names


def test_tools_list_keeps_office_read_tools_when_layer_docs_both_or_unset():
    for lyr in (None, "both", "docs"):
        if lyr is not None:
            os.environ["SHERPA_MCP_LAYER"] = lyr
        try:
            resp = M.handle({"jsonrpc": "2.0", "id": 52, "method": "tools/list"})
            names = {t["name"] for t in resp["result"]["tools"]}
        finally:
            os.environ.pop("SHERPA_MCP_LAYER", None)
        assert {"xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages"} <= names, lyr


# ===== DEPTH-2 S3b: サイドカー（子エージェントの MCP 呼出・ask_user の観測）=====
# `docs/proposals/2026-09-17-深さの再定義とレビュー巡.md` §2.6/§9.1・受け入れ条件(1)(3)(6)。
# `SHERPA_MCP_SIDECAR` が設定されている間だけ、run_dir 配下の JSONL へ doc_id／ツール名／種別／
# 時刻（と ask_user の質問）だけを追記する（本文は書かない）。未設定時は既存どおり何もしない。

def test_read_doc_writes_sidecar_entry_without_body_when_configured(tmp_path):
    """read_doc の成功呼出は SHERPA_MCP_SIDECAR へ {kind, tool, doc_id, ts} だけを1行追記する
    （資料本文＝payload["text"] 相当は一切含まない）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    os.environ["SHERPA_MCP_SIDECAR"] = str(sidecar)
    try:
        hit_resp = M.handle({"jsonrpc": "2.0", "id": 60, "method": "tools/call",
                             "params": {"name": "ripgrep_search", "arguments": {"query": "TAX-RATE"}}})
        doc_id = json.loads(hit_resp["result"]["content"][0]["text"])["hits"][0]["doc_id"]

        resp = M.handle({"jsonrpc": "2.0", "id": 61, "method": "tools/call",
                         "params": {"name": "read_doc", "arguments": {"doc_id": doc_id}}})
        assert not resp["result"]["isError"]

        lines = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
        read_entries = [e for e in lines if e.get("kind") == "read" and e.get("tool") == "read_doc"]
        assert len(read_entries) == 1
        entry = read_entries[0]
        assert entry["doc_id"] == doc_id
        assert set(entry.keys()) == {"kind", "tool", "doc_id", "ts"}   # 本文フィールドが無い
        assert isinstance(entry["ts"], (int, float))
    finally:
        os.environ.pop("SHERPA_MCP_SIDECAR", None)


def test_ripgrep_search_does_not_write_sidecar_entry():
    """`read_doc`/`doc_outline` 等の doc_id を持つ読取系ツール以外（ripgrep_search）は
    サイドカーに書かない（読取＝本文を開いた事実だけを記録する契約）。"""
    with __import__("tempfile").TemporaryDirectory() as d:
        sidecar = __import__("pathlib").Path(d) / "sidecar.jsonl"
        os.environ["SHERPA_MCP_SIDECAR"] = str(sidecar)
        try:
            M.handle({"jsonrpc": "2.0", "id": 62, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "TAX-RATE"}}})
            assert not sidecar.exists()
        finally:
            os.environ.pop("SHERPA_MCP_SIDECAR", None)


def test_sidecar_not_written_when_env_unset(tmp_path):
    """`SHERPA_MCP_SIDECAR` 未設定時は既存どおり何も書かない（fail-open・既存動作と同一）。"""
    os.environ.pop("SHERPA_MCP_SIDECAR", None)
    marker = tmp_path / "sidecar.jsonl"
    hit_resp = M.handle({"jsonrpc": "2.0", "id": 63, "method": "tools/call",
                         "params": {"name": "ripgrep_search", "arguments": {"query": "TAX-RATE"}}})
    doc_id = json.loads(hit_resp["result"]["content"][0]["text"])["hits"][0]["doc_id"]
    M.handle({"jsonrpc": "2.0", "id": 64, "method": "tools/call",
             "params": {"name": "read_doc", "arguments": {"doc_id": doc_id}}})
    assert not marker.exists()


def test_ask_user_first_call_writes_sidecar_question_entry(tmp_path):
    """初回の ask_user はサイドカーへ質問（`_question_from_args` と同じ形）を書く。2回目以降
    （既存の「1実行1回」ガード）は追加で書かない——サイドカーの ask_user 件数は増えない。
    既存の応答文言（`_ASK_RESULT_FIRST`/`_ASK_RESULT_AGAIN`・`isError` False）は変わらない
    （単一エージェント実行の既存契約を壊さない）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    os.environ["SHERPA_MCP_SIDECAR"] = str(sidecar)
    M._ASK_STATE["count"] = 0
    args = {"prompt": "対象範囲は？", "mode": "single",
            "options": [{"label": "A"}, {"label": "B"}]}
    try:
        r1 = M.handle({"jsonrpc": "2.0", "id": 65, "method": "tools/call",
                       "params": {"name": "ask_user", "arguments": args}})
        r2 = M.handle({"jsonrpc": "2.0", "id": 66, "method": "tools/call",
                       "params": {"name": "ask_user", "arguments": args}})
        assert r1["result"]["isError"] is False and r2["result"]["isError"] is False
        assert M._ASK_RESULT_FIRST in r1["result"]["content"][0]["text"]
        assert M._ASK_RESULT_AGAIN in r2["result"]["content"][0]["text"]

        lines = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
        ask_entries = [e for e in lines if e.get("kind") == "ask_user"]
        assert len(ask_entries) == 1, "1実行1回のガードと同じ数え方＝2回目は書かない"
        q = ask_entries[0]["question"]
        assert q["prompt"] == "対象範囲は？" and q["mode"] == "single"
        assert len(q["options"]) == 2
    finally:
        os.environ.pop("SHERPA_MCP_SIDECAR", None)
        M._ASK_STATE["count"] = 0


def test_sidecar_append_write_failure_logs_warning_once_without_body(tmp_path, capsys):
    """#29 是正: サイドカーへの書込失敗は完全に無言化せず、型と errno だけ（本文・パスは
    含めない）を stderr へ1回だけ知らせる（fail-open＝ツール呼出自体は成功のまま・
    2回目以降は再度出さない）。"""
    # 親ディレクトリが無いパス＝open("a") が FileNotFoundError（OSError 派生）で必ず失敗する。
    bad_sidecar = tmp_path / "does-not-exist" / "sidecar.jsonl"
    os.environ["SHERPA_MCP_SIDECAR"] = str(bad_sidecar)
    M._sidecar_write_failed_once = False
    try:
        hit_resp = M.handle({"jsonrpc": "2.0", "id": 70, "method": "tools/call",
                             "params": {"name": "ripgrep_search", "arguments": {"query": "TAX-RATE"}}})
        doc_id = json.loads(hit_resp["result"]["content"][0]["text"])["hits"][0]["doc_id"]

        resp = M.handle({"jsonrpc": "2.0", "id": 71, "method": "tools/call",
                         "params": {"name": "read_doc", "arguments": {"doc_id": doc_id}}})
        assert not resp["result"]["isError"], "サイドカー書込失敗はツール呼出自体を失敗させない"

        # 2回目は `start_line` を明示（既定と同じ1行目からだが引数を変えて重複実行の抑止
        # （duplicate_tool_call）に引っ掛からないようにする・本テストの主眼は
        # サイドカー書込失敗の警告が1回だけ出ることの検証で、同一引数の再実行ではない）。
        resp2 = M.handle({"jsonrpc": "2.0", "id": 72, "method": "tools/call",
                          "params": {"name": "read_doc", "arguments": {"doc_id": doc_id, "start_line": 1}}})
        assert not resp2["result"]["isError"]

        err = capsys.readouterr().err
        lines = [l for l in err.splitlines() if "sidecar write failed" in l]
        assert len(lines) == 1, f"warning は1回だけのはず: {err!r}"
        assert "FileNotFoundError" in lines[0]
        assert doc_id not in lines[0] and str(bad_sidecar) not in lines[0], \
            "本文・パスの中身をログに出してはいけない"
    finally:
        os.environ.pop("SHERPA_MCP_SIDECAR", None)
        M._sidecar_write_failed_once = False


# S4（縮退の可視化と計数）: 子エージェント（spawn_agent された worker/evaluator）の MCP 呼出は親の
# `--json` に現れないため、障害もサイドカーの新種別 `{"kind": "error", "code", "tool", "ts"}` として
# 記録する（閉じたコードだけ・本文や資料名は書かない）。

def test_graph_schema_era_writes_sidecar_error_entry_with_code_only(tmp_path, monkeypatch):
    """旧世代グラフ（`graph_reingest_required`）を子が受け取ったら、親が観測できるよう
    サイドカーへ閉じたコードだけを1行書く（world/stored_era 等の値は書かない）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))

    def _boom(name, args, world, scope_paths, **kw):
        raise M.GraphSchemaEraError(world, "old-era", lens="troubleshoot")
    monkeypatch.setattr(M.agentic_search, "run_tool", _boom)
    resp = M.handle({"jsonrpc": "2.0", "id": 80, "method": "tools/call",
                     "params": {"name": "graph_neighbors", "arguments": {"name": "請求"}}})
    assert resp["result"]["isError"] is True
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    assert len(entries) == 1
    assert entries[0]["kind"] == "error" and entries[0]["code"] == "graph_reingest_required"
    assert entries[0]["tool"] == "graph_neighbors"
    assert set(entries[0].keys()) == {"kind", "code", "tool", "ts"}   # 本文・world・stored_era を書かない


def test_tool_error_without_known_code_writes_no_sidecar_entry(tmp_path, monkeypatch):
    """自由文のツールエラー（閉じたコードを持たない）はサイドカーに書かない——観測できるのは
    閉集合のコードだけという契約（本文が台帳へ漏れない）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    resp = M.handle({"jsonrpc": "2.0", "id": 81, "method": "tools/call",
                     "params": {"name": "read_around", "arguments": {"doc_id": "居ない.md", "line": 1}}})
    assert resp["result"]["isError"] is True
    assert not sidecar.exists()


# ===== Azure 実機是正: MCP ツール結果1件あたりのバイト予算（API 経路と同じ effective_* を再利用）=====
# 1回の調査で集めたツール結果だけで Codex の文脈枠を使い切っていた事象への対処。1件あたりの
# 上限を掛け、超過の事実をサイドカーへ記録する（累計の上限は撤去済み・下の節参照）。

@pytest.fixture(autouse=True)
def _reset_duplicate_call_cache():
    """`_seen_tool_calls`（同一クエリの重複検知キャッシュ・モジュール変数でテスト間を持ち越さない）
    をリセットする——複数のテスト関数が同じ (tool, args) を独立に使い回すため、持ち越すと
    後続テストの1回目の呼出が「重複」と誤検知される。"""
    M._seen_tool_calls.clear()
    yield
    M._seen_tool_calls.clear()


def test_large_tool_result_is_clipped_with_truncated_marker(monkeypatch):
    """1件あたりの予算（`effective_tool_result_max_bytes`）を超えた結果は構造を保った
    クリップ（`hits` を末尾から落とす）で `truncated`/`next_offset` 付きの dict に差し替わる
    （呼出自体は失敗にしない・平文置換ではない）。1件も丸ごとは残せない予算でも `hits=[]` に
    縮退させず、先頭ヒットの本文を縮めて1件残す（`next_offset` を進めて重複拒否を避ける）。"""
    monkeypatch.setattr(M.agentic_search, "effective_tool_result_max_bytes", lambda **kw: 256)
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["x" * 1000]}, [], [], []))
    resp = M.handle({"jsonrpc": "2.0", "id": 90, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    assert resp["result"]["isError"] is False
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["truncated"] is True
    assert body["partial_hit"] is True
    assert len(body["hits"]) == 1 and body["hits"][0] != ""   # 空ページに縮退していない
    assert body["next_offset"] == 1                # offset省略＝0起点＋1件消費＝続きの位置
    assert "clipped_bytes" not in body


# 累計（1 run 全体）のツール結果バイト予算は撤去済み（`docs/proposals/
# 2026-09-21-調査台帳を文脈の外に置く.md` §1/§2）——MCP プロセス単位の累計は Codex CLI の自動圧縮で
# 文脈が空いてもリセットされず、到達後は本文系ツールが永続的に拒否されていた。1件あたりの予算
# （`test_large_tool_result_is_clipped_with_truncated_marker` 等）は残る。

def test_total_bytes_not_capped_beyond_former_ceiling(monkeypatch):
    """累計が旧天井（1MiB）を大きく超える量を呼び続けても、本文系ツールは拒否されない
    （受け入れ条件2・累計バイト予算の撤去）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "999999")   # 1件あたりの予算はここでは対象外
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["y" * 50_000]}, [], [], []))
    total_bytes = 0
    for i in range(30):
        r = M.handle({"jsonrpc": "2.0", "id": 100 + i, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": f"y{i}"}}})
        assert r["result"]["isError"] is False, f"call {i} was rejected"
        total_bytes += len(r["result"]["content"][0]["text"].encode("utf-8"))
    assert total_bytes > 1024 * 1024, "旧天井(1MiB)を実際に超える量を送っていることの確認"


def test_total_budget_hit_sidecar_entry_never_written(tmp_path, monkeypatch):
    """累計予算の判定自体が無いため、サイドカーへ
    `{"kind":"limit","field":"total_budget_hit"}` は一度も書かれない
    （`sherpa/store/usage.py`・`stop_kind.py` が読むキー自体は撤去しない・値が常に立たないことの確認）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "999999")
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["z" * 50_000]}, [], [], []))
    for i in range(20):
        M.handle({"jsonrpc": "2.0", "id": 200 + i, "method": "tools/call",
                 "params": {"name": "ripgrep_search", "arguments": {"query": f"z{i}"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if sidecar.exists() else []
    limit_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "total_budget_hit"]
    assert limit_entries == []


def test_tool_result_clipped_writes_sidecar_limit_entry(tmp_path, monkeypatch):
    """1件あたりのクリップが起きた呼出ごとにサイドカーへ
    `{"kind":"limit","field":"tool_result_clipped"}` を書く。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setattr(M.agentic_search, "effective_tool_result_max_bytes", lambda **kw: 64)
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["w" * 1000]}, [], [], []))
    M.handle({"jsonrpc": "2.0", "id": 110, "method": "tools/call",
             "params": {"name": "ripgrep_search", "arguments": {"query": "w"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    clip_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "tool_result_clipped"]
    assert len(clip_entries) == 1


# ===== ツール呼び出し回数の上限は撤去済み =====
# 旧 `SHERPA_MCP_TOOL_MAX_CALLS`（クイックのときだけ親が渡していた・`depth_base_max_turns` の
# 実効値流用）到達時に返していた `tool_call_budget_exhausted` は、調査を終了させる信号だった
# （`docs/proposals/2026-09-21-調査台帳を文脈の外に置く.md` §1/§2）。MCP サーバはこの env をもう
# 読まない——古い親プロセス由来の env が残っていても無視される。

def test_tool_calls_not_capped_even_with_legacy_max_calls_env(monkeypatch):
    """`SHERPA_MCP_TOOL_MAX_CALLS`（クイックの実効値相当・撤去済み）が env に残っていても、
    17回以上呼んでも `tool_call_budget_exhausted` を返さない（受け入れ条件2・クイック相当）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_MAX_CALLS", "1")
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["y"]}, [], [], []))
    for i in range(20):
        r = M.handle({"jsonrpc": "2.0", "id": 200 + i, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": f"y{i}"}}})
        assert r["result"]["isError"] is False, f"call {i} was rejected"


def test_tool_calls_exhausted_sidecar_entry_never_written(tmp_path, monkeypatch):
    """呼び出し回数の判定自体が無いため、サイドカーへ
    `{"kind":"limit","field":"tool_calls_exhausted"}` は一度も書かれない
    （`sherpa/store/usage.py`・`stop_kind.py` が読むキー自体は撤去しない・値が常に立たないことの確認）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setenv("SHERPA_MCP_TOOL_MAX_CALLS", "1")
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["z"]}, [], [], []))
    for i in range(5):
        M.handle({"jsonrpc": "2.0", "id": 210 + i, "method": "tools/call",
                 "params": {"name": "ripgrep_search", "arguments": {"query": f"z{i}"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if sidecar.exists() else []
    limit_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "tool_calls_exhausted"]
    assert limit_entries == []


# ===== env 経由の予算上書き（`SHERPA_MCP_TOOL_BUDGET_BYTES`＝1件あたり）=====
# 親プロセス（`CodexProvider`）が窓由来の実効値を解決した上でこの env を渡す（provider.py 参照）。
# ここでは MCP サーバ側が「env があれば優先・無い/不正なら従来の effective_* 呼び出しへ
# フォールバックする」ことだけを検証する（窓解決そのものは model_windows/agentic_search 側のテスト対象）。
# `SHERPA_MCP_TOOL_BUDGET_TOTAL_BYTES`（累計・撤去済み）は読まれないことを別途検証する。

def test_env_budget_bytes_takes_precedence_over_effective_call(monkeypatch):
    """1件あたり予算の env が設定されていれば `effective_tool_result_max_bytes` を呼ばない。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "64")
    called = []
    monkeypatch.setattr(M.agentic_search, "effective_tool_result_max_bytes",
                        lambda **kw: called.append(1) or 999999)
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["x" * 1000]}, [], [], []))
    resp = M.handle({"jsonrpc": "2.0", "id": 120, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["truncated"] is True
    assert not called, "env 優先時は従来の effective_tool_result_max_bytes を呼ばない"


def test_env_budget_total_bytes_is_no_longer_read(monkeypatch):
    """`SHERPA_MCP_TOOL_BUDGET_TOTAL_BYTES`（累計予算・撤去済み）は設定されていても読まれない
    ——`effective_tool_result_max_total_bytes` を呼ばず、本文系ツールも打ち切られない。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_TOTAL_BYTES", "8")
    called = []
    monkeypatch.setattr(M.agentic_search, "effective_tool_result_max_total_bytes",
                        lambda **kw: called.append(1) or 999999)
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["y" * 100]}, [], [], []))
    for i in range(5):
        r = M.handle({"jsonrpc": "2.0", "id": 121 + i, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": f"y{i}"}}})
        assert r["result"]["isError"] is False
    assert not called, "累計予算の env は撤去済み＝effective_tool_result_max_total_bytes を呼ばない"


@pytest.mark.parametrize("env_value", ["0", "-1", "not-a-number", ""])
def test_env_budget_bytes_invalid_falls_back_to_effective_call(monkeypatch, env_value):
    """未設定/不正値（0以下・数値でない・空文字）は従来の `effective_tool_result_max_bytes` へ
    フォールバックする（親プロセスから渡らない実行=単体テスト・手動起動で退行しない契約）。"""
    if env_value:
        monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", env_value)
    monkeypatch.setattr(M.agentic_search, "effective_tool_result_max_bytes", lambda **kw: 256)
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["x" * 1000]}, [], [], []))
    resp = M.handle({"jsonrpc": "2.0", "id": 123, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["truncated"] is True
    assert len(body["hits"]) == 1 and body["hits"][0] != ""   # 1件も残せない予算でも空ページにしない
    assert body["next_offset"] == 1         # 構造化クリップ（hits）に置き換わった＝平文置換ではない
    assert "clipped_bytes" not in body


# ===== 実装ベース探索の回復・根本原因対応A: env の hits/window/bytes が run_tool へ実際に届く =====
# `mcp_server.py` は従来 `run_tool()` へ `max_hits`/`window_cap`/`tool_result_max_bytes` を
# 一切渡していなかった（管理画面の基準値も調べる深さの倍率も一切効かず、env 既定
# `SHERPA_GREP_MAX_HITS`/`SHERPA_READ_WINDOW`・コード既定 262144 だけが効いていた）。

def test_tools_call_forwards_env_hits_window_and_bytes_to_run_tool(monkeypatch):
    """`SHERPA_MCP_TOOL_MAX_HITS`/`_WINDOW_CAP`/`_BUDGET_BYTES`（親＝`CodexProvider` が
    調べる深さ連動込みで解決した実効値）が実際に `run_tool()` の `max_hits`/`window_cap`/
    `tool_result_max_bytes` として渡る。"""
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured.update(kw)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(M.agentic_search, "run_tool", fake_run_tool)
    monkeypatch.setenv("SHERPA_MCP_TOOL_MAX_HITS", "77")
    monkeypatch.setenv("SHERPA_MCP_TOOL_WINDOW_CAP", "88")
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "99999")
    M.handle({"jsonrpc": "2.0", "id": 200, "method": "tools/call",
             "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    assert captured.get("max_hits") == 77
    assert captured.get("window_cap") == 88
    assert captured.get("tool_result_max_bytes") == 99999


def test_tools_call_hits_window_and_bytes_default_to_none_when_env_unset(monkeypatch):
    """env 未設定時は `run_tool()` へ `None` を渡し、`run_tool()` 自身のモジュール既定
    （`MAX_HITS`/`READ_WINDOW`/`TOOL_RESULT_MAX_BYTES`）へフォールバックさせる（親から渡らない
    実行＝単体テスト・手動起動で退行しない契約）。"""
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured.update(kw)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(M.agentic_search, "run_tool", fake_run_tool)
    for var in ("SHERPA_MCP_TOOL_MAX_HITS", "SHERPA_MCP_TOOL_WINDOW_CAP", "SHERPA_MCP_TOOL_BUDGET_BYTES"):
        monkeypatch.delenv(var, raising=False)
    M.handle({"jsonrpc": "2.0", "id": 201, "method": "tools/call",
             "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    assert captured.get("max_hits") is None
    assert captured.get("window_cap") is None
    assert captured.get("tool_result_max_bytes") is None


# ===== 実装ベース探索の回復・根本原因対応B: 再JSON化でバイト上限を超えるバグの是正 =====
# `_clip_tool_result` は直列化済み文字列を素朴に切って `{"text": ...}` に入れ直して**再度**
# `json.dumps` するため、引用符・バックスラッシュの再エスケープで最終バイト数が上限の約2倍に
# 膨らみうる（引用符だらけの入力に限らず、JSON 構造そのものが引用符を多用するため一般的に
# 起こる）。最終的に `content[].text` へ載る実バイト数で判定・保証することを検証する。

def test_clip_tool_result_final_json_stays_within_budget_for_quote_heavy_content(monkeypatch):
    """引用符だらけの入力（過去の実害の再現・単純な半分クリップでは上限の約2倍に膨らんでいた）
    でも、`_clip_tool_result` が返す dict を最終的に `json.dumps` した実バイト数が予算以下になる。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "65536")
    result = {"text": '"' * 70000}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 65536


def test_clip_tool_result_plain_ascii_content_still_bounded(monkeypatch):
    """エスケープを要しない通常の内容でも、包んだ最終形が上限以下になる契約は変わらない。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "256")
    result = {"hits": ["x" * 1000]}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 256


def test_tools_call_final_wire_text_stays_within_budget_for_quote_heavy_result(monkeypatch):
    """`handle()` が実際に返す JSON-RPC の `content[].text`（Codex が読む生のバイト列）自体が
    予算を超えない——`_clip_tool_result` 単体でなく end-to-end で保証する。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "65536")
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"text": '"' * 70000}, [], [], []))
    resp = M.handle({"jsonrpc": "2.0", "id": 210, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    wire_text = resp["result"]["content"][0]["text"]
    assert len(wire_text.encode("utf-8")) <= 65536


def test_ripgrep_search_oversized_hit_set_absorbed_before_outer_clip(monkeypatch):
    """`agentic_search.run_tool` は実装（モックしない）——1件あたりのバイト予算をヒット数で
    均等割りしただけでは付帯情報/JSON構造分の超過を吸収できない場合があるが、`run_tool` 自身が
    直列化後の実バイト数で収まりを保証するため、`_clip_tool_result`（外側クリップ）が結果全体を
    `{"truncated":..,"text":..}` の平文へ潰すことは起きない——`next_offset`・全ヒットの `doc_id`
    が構造ごと残る。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", str(64 * 1024))
    hits = [{"doc_id": f"doc{i:03d}.md", "path": f"/x/doc{i:03d}.md", "ext": ".md",
            "line": 1, "span": [1, 1], "text": "x" * 5000, "match": "x"} for i in range(30)]
    monkeypatch.setattr(M.agentic_search.grep_tool, "grep_search", lambda *a, **kw: hits)
    resp = M.handle({"jsonrpc": "2.0", "id": 211, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    assert resp["result"]["isError"] is False
    wire_text = resp["result"]["content"][0]["text"]
    assert len(wire_text.encode("utf-8")) <= 64 * 1024
    body = json.loads(wire_text)
    assert "clipped_bytes" not in body   # 外側クリップの平文置換が起きていない証跡
    assert body.get("next_offset") == 30
    assert [h["doc_id"] for h in body["hits"]] == [f"doc{i:03d}.md" for i in range(30)]


# ===== 調査台帳を文脈の外に置く §2/§5: 外側クリップ（最終防衛線）も構造を保ったまま続きを取れる形にする =====
# `run_tool` 自身の直列化後保証（per-hit シュリンク・`_finish_reader_result`）が効かない極端な
# ケースでも、`_clip_tool_result` が結果の形（`hits`／`text`+`end_line`）を知っていれば、続きの
# 位置（`next_offset`／`start_line=end_line+1`）を保った構造化クリップへ回す——形が分からない結果
# だけ、従来どおりの平文置換（fail-open）に落ちる。

def test_clip_tool_result_hits_field_preserves_structure_and_next_offset(monkeypatch):
    """`hits` を持つ結果は末尾のヒットから落として構造を保つ——`truncated`/`next_offset` が残り、
    残ったヒットは先頭から連続する。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "900")
    hits = [{"doc_id": f"doc{i:03d}.md", "line": i + 1, "text": "x" * 200} for i in range(10)]
    result = {"hits": hits}
    clipped, was_clipped = M._clip_tool_result(result, name="ripgrep_search", args={})
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 900
    assert clipped["truncated"] is True
    kept = clipped["hits"]
    assert 0 < len(kept) < len(hits)
    assert kept == hits[:len(kept)]                # 残ったヒットは先頭から連続
    assert clipped["next_offset"] == len(kept)     # offset省略＝0起点＋残した件数＝続きの位置


def test_clip_tool_result_hits_field_next_offset_uses_call_offset_not_reverse_calc(monkeypatch):
    """RV是正: 最終ページ（ヒット数がページ幅未満＝`agentic_search.py` 側は `next_offset` を
    付けない）を外側クリップがさらに縮めても、`next_offset` は呼び出し引数の `offset` を基準に
    計算する——既存の `result["next_offset"]` から逆算すると基準を持てず巻き戻る
    （offset=20 の最終5件を2件に切ったときに正しい22ではなく2を返していた不具合の再現）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "700")
    hits = [{"doc_id": f"doc{i:03d}.md", "line": i + 1, "text": "x" * 200} for i in range(20, 25)]
    result = {"hits": hits}   # 最終ページ＝next_offset を持たない（grep_search の実際の返り値どおり）
    clipped, was_clipped = M._clip_tool_result(result, name="ripgrep_search", args={"offset": 20})
    assert was_clipped is True
    kept = clipped["hits"]
    assert len(kept) == 2
    assert kept == hits[:2]
    assert clipped["next_offset"] == 22


def test_clip_tool_result_es_search_hits_never_get_next_offset(monkeypatch):
    """RV是正: es_search はページングを持たない——`hits` を外側クリップで切っても `next_offset`
    は一切付けない（`truncated=true` だけで「続きは取れない」を伝える）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "900")
    hits = [{"doc_id": f"doc{i:03d}.md", "line": i + 1, "text": "x" * 200} for i in range(10)]
    result = {"hits": hits}
    clipped, was_clipped = M._clip_tool_result(result, name="es_search", args={})
    assert was_clipped is True
    assert clipped["truncated"] is True
    assert 0 < len(clipped["hits"]) < len(hits)
    assert "next_offset" not in clipped


def test_clip_tool_result_hits_single_oversized_hit_shrinks_body_instead_of_going_empty(monkeypatch):
    """RV是正: 先頭ヒット1件すら丸ごとは残せなくても `hits=[]`（進捗ゼロの offset 据え置き）に
    縮退しない——本文（`text`）を縮めてでも位置情報付きの1件を残し、`next_offset=offset+1` で
    進捗を保証する（`hits=[]`・`next_offset` 不変のページは続きの呼出が同一引数になり
    `_is_duplicate_tool_call` の重複拒否でその検索を先へ進められなくなっていた）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "500")
    hit = {"doc_id": "a/b/c.md", "line": 1, "text": "y" * 50000}   # 位置情報は小さく本文だけ巨大
    result = {"hits": [hit]}
    clipped, was_clipped = M._clip_tool_result(result, name="ripgrep_search", args={"offset": 20})
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 500
    assert clipped["truncated"] is True
    assert clipped["partial_hit"] is True
    assert len(clipped["hits"]) == 1
    kept = clipped["hits"][0]
    assert kept["doc_id"] == "a/b/c.md"             # 位置情報は保たれる
    assert 0 < len(kept["text"]) < len(hit["text"])  # 本文だけが縮む
    assert clipped["next_offset"] == 21              # offset(20) + 消費した1件


def test_clip_tool_result_hits_position_info_alone_over_budget_returns_error(monkeypatch):
    """RV是正の repro: 位置情報（`doc_id` 等・`text` を除く）だけで `max_bytes` を超える場合
    （例: 極端に長い doc_id）、本文を空にしても収まらないため成功ページを装わず
    `tool_result_budget_too_small` エラーを返す——`hits=[]`・`next_offset` 据え置きの偽の
    成功ページで重複拒否ループに陥らせない。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "900")
    hit = {"doc_id": "d" * 1007, "line": 1, "text": "y" * 5000}   # doc_id 自体が予算超
    result = {"hits": [hit]}
    clipped, was_clipped = M._clip_tool_result(result, name="ripgrep_search", args={"offset": 20})
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 900
    assert clipped == {"error": "tool_result_budget_too_small",
                       "hint": M._TOOL_RESULT_BUDGET_TOO_SMALL_HINT, "offset": 20}


def test_handle_reports_is_error_when_outer_clip_replaces_result_with_error(monkeypatch):
    """RV是正: `is_error` はクリップ前の結果で確定するため、`_clip_tool_result` がクリップ後に
    `{"error": ...}`（`tool_result_budget_too_small`）へ置き換えても `isError=false` のまま
    返っていた——`handle()` を通した最終応答で `isError=true` になることを固定する。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "900")
    hit = {"doc_id": "d" * 1007, "line": 1, "text": "y" * 5000}   # doc_id 自体が予算超
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": [hit]}, set(), [], []))
    resp = M.handle({"jsonrpc": "2.0", "id": 700, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "x", "offset": 20}}})
    assert resp["result"]["isError"] is True
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["error"] == "tool_result_budget_too_small"


def test_clip_tool_result_read_doc_field_preserves_end_line_and_cuts_on_line_boundary(monkeypatch):
    """`text`＋`end_line` を持つ結果（read_doc）は行境界で落とし、`end_line`/`total_lines` を残す
    ——続きは `start_line=end_line+1` で取れる。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "800")
    lines = [f"{i}: line content number {i} " + "x" * 50 for i in range(1, 51)]
    result = {"doc_id": "a.md", "start_line": 1, "end_line": 50, "total_lines": 500,
             "text": "\n".join(lines)}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 800
    assert clipped["truncated"] is True
    assert clipped["total_lines"] == 500            # 総行数の申告は変えない
    kept_lines = clipped["text"].split("\n")
    assert 0 < len(kept_lines) < len(lines)
    assert kept_lines == lines[:len(kept_lines)]     # 行境界で切れている（途中で切れた行が無い）
    assert clipped["end_line"] == len(kept_lines)    # start_line=1 なので end_line==保持行数


def test_clip_tool_result_read_doc_single_long_line_keeps_partial_text_and_progresses(monkeypatch):
    """RV是正: 1行も丸ごとは収まらない（1行だけの極端に長い本文）場合、`text=""`・進捗ゼロで
    返すと同一引数の再読取が重複拒否で弾かれ二度と読めなくなる——先頭行を予算内へ縮めてでも
    非空の `text` を返し、`end_line=start_line`（消費済み扱い）で進捗を保証する。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "40000")
    line = "x" * 65536
    result = {"doc_id": "a.md", "start_line": 5, "end_line": 5, "total_lines": 100, "text": line}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 40000
    assert clipped["text"] != ""
    assert clipped["end_line"] == 5                 # start_line と同じ＝その行を消費済み扱い
    assert clipped["partial_line"] is True
    assert clipped["truncated"] is True
    assert "start_line=6" in clipped["note"]


def test_clip_tool_result_unknown_shape_falls_back_with_note(monkeypatch):
    """`hits`/`end_line` のどちらの形でもない結果は従来どおり平文置換だが、続きの位置を保証
    できないことを `note` で明示する。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "500")
    result = {"foo": "x" * 100000}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True
    final_bytes = len(json.dumps(clipped, ensure_ascii=False).encode("utf-8"))
    assert final_bytes <= 500
    assert clipped["truncated"] is True
    assert clipped["note"] == M._CLIP_FALLBACK_NOTE


def test_clip_tool_result_under_budget_is_byte_identical(monkeypatch):
    """予算以下の結果は無変更（バイト一致）——早期リターンの契約を明示的に固定する。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "65536")
    result = {"hits": [{"doc_id": "a.md", "line": 1, "text": "ok"}]}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is False
    assert clipped is result
    assert json.dumps(clipped, ensure_ascii=False) == json.dumps(result, ensure_ascii=False)


# ===== 実装ベース探索の回復・根本原因対応C: 同一クエリの重複実行の抑止 =====
# 実機では同じ ripgrep_search が2回ずつ走り、同一結果を二重に文脈へ積んでいた。

def test_duplicate_tool_call_returns_error_without_calling_run_tool(monkeypatch):
    """同一 (ツール名, 正規化引数) の2回目は `run_tool` を呼ばず `duplicate_tool_call` を返す。"""
    calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        calls.append((name, dict(args)))
        return ({"hits": ["ok"]}, set(), [], [])

    monkeypatch.setattr(M.agentic_search, "run_tool", fake_run_tool)
    r1 = M.handle({"jsonrpc": "2.0", "id": 300, "method": "tools/call",
                  "params": {"name": "ripgrep_search", "arguments": {"query": "同じ条件"}}})
    r2 = M.handle({"jsonrpc": "2.0", "id": 301, "method": "tools/call",
                  "params": {"name": "ripgrep_search", "arguments": {"query": "同じ条件"}}})
    assert not r1["result"]["isError"]
    assert r2["result"]["isError"] is True
    body2 = json.loads(r2["result"]["content"][0]["text"])
    assert body2["error"] == "duplicate_tool_call"
    assert len(calls) == 1, "2回目は run_tool を呼んではいけない（本文を再送しない）"


def test_duplicate_tool_call_argument_order_is_order_independent(monkeypatch):
    """引数の順序が違うだけの呼出は同一視する（キー順に依存しない正規化）。"""
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"doc_id": "a.md", "text": "..."}, set(), [], []))
    r1 = M.handle({"jsonrpc": "2.0", "id": 302, "method": "tools/call",
                  "params": {"name": "read_doc", "arguments": {"doc_id": "a.md", "start_line": 1}}})
    r2 = M.handle({"jsonrpc": "2.0", "id": 303, "method": "tools/call",
                  "params": {"name": "read_doc", "arguments": {"start_line": 1, "doc_id": "a.md"}}})
    assert not r1["result"]["isError"]
    assert r2["result"]["isError"] is True
    assert json.loads(r2["result"]["content"][0]["text"])["error"] == "duplicate_tool_call"


def test_duplicate_tool_call_writes_sidecar_limit_entry(tmp_path, monkeypatch):
    """重複検知したら `{"kind":"limit","field":"duplicate_tool_call"}` を1行書く
    （`tool_result_clipped` と同じサイドカー経路・書式）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["ok"]}, set(), [], []))
    M.handle({"jsonrpc": "2.0", "id": 304, "method": "tools/call",
             "params": {"name": "ripgrep_search", "arguments": {"query": "重複検知テスト"}}})
    M.handle({"jsonrpc": "2.0", "id": 305, "method": "tools/call",
             "params": {"name": "ripgrep_search", "arguments": {"query": "重複検知テスト"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    dup_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "duplicate_tool_call"]
    assert len(dup_entries) == 1


def test_duplicate_tool_call_exempts_budget_exempt_tools(monkeypatch):
    """`_BUDGET_EXEMPT_TOOLS`（list_docs/folder_tree）は同一引数の繰り返し呼出でも重複扱いに
    しない（一覧のみの土台系ツールを繰り返し呼ぶこと自体は実害が無い）。"""
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"count": 0, "docs": []}, set(), [], []))
    args = {"path_prefix": ""}
    r1 = M.handle({"jsonrpc": "2.0", "id": 306, "method": "tools/call",
                  "params": {"name": "list_docs", "arguments": args}})
    r2 = M.handle({"jsonrpc": "2.0", "id": 307, "method": "tools/call",
                  "params": {"name": "list_docs", "arguments": args}})
    assert not r1["result"]["isError"] and not r2["result"]["isError"]


def test_duplicate_tool_call_cache_is_lru_bounded(monkeypatch):
    """件数上限（`_DUPLICATE_CALL_CACHE_MAX`）を超えたら最も古いキーから捨てる
    （無限に覚え続けてプロセスのメモリを圧迫しない）。"""
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["ok"]}, set(), [], []))
    for i in range(M._DUPLICATE_CALL_CACHE_MAX + 1):
        M.handle({"jsonrpc": "2.0", "id": 400 + i, "method": "tools/call",
                 "params": {"name": "ripgrep_search", "arguments": {"query": f"q{i}"}}})
    # 最初のキー（query=q0）は上限超過で追い出されているはず＝再実行しても重複扱いにならない。
    resp = M.handle({"jsonrpc": "2.0", "id": 500, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "q0"}}})
    assert not resp["result"]["isError"]


# ===== 実装ベース探索の回復・根本原因対応D: run_tool() 自身の内部切り詰めが統計に載らない =====
# API 経路（`agentic_search._record_run_tool_limits`）は `_SEARCH_TRUNCATED_TOOLS`/`_BYTE_CLIP_TOOLS`
# と同じ判定キーで検索打ち切り・1件あたりクリップを数えるが、Codex 経路（mcp_server.py）はこれまで
# 後段のバイト予算クリップ（`_clip_tool_result`）しか数えていなかった。同じ集合・同じ判定を再利用する。

def test_internal_search_truncated_writes_sidecar_limit_entry(tmp_path, monkeypatch):
    """検索系ツール（`agentic_search._SEARCH_TRUNCATED_TOOLS`）が内部で `truncated=True` を
    返したら `{"kind":"limit","field":"search_truncated"}` を書く（API 経路と同じ判定キー）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"hits": ["a"], "truncated": True}, set(), [], []))
    M.handle({"jsonrpc": "2.0", "id": 600, "method": "tools/call",
             "params": {"name": "ripgrep_search", "arguments": {"query": "x"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    st_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "search_truncated"]
    assert len(st_entries) == 1


def test_internal_text_truncated_writes_sidecar_tool_result_clipped_entry(tmp_path, monkeypatch):
    """`read_doc`（`agentic_search._BYTE_CLIP_TOOLS`）が内部で `text_truncated=True` を返したら
    `{"kind":"limit","field":"tool_result_clipped"}` を書く（後段のバイト予算クリップとは別経路）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"text": "short", "text_truncated": True}, set(), [], []))
    M.handle({"jsonrpc": "2.0", "id": 601, "method": "tools/call",
             "params": {"name": "read_doc", "arguments": {"doc_id": "a.md"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    clip_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "tool_result_clipped"]
    assert len(clip_entries) == 1


def test_internal_byte_clipped_flag_also_writes_tool_result_clipped_entry(tmp_path, monkeypatch):
    """原本読取ツールの `byte_clipped` フラグ（`_finish_reader_result` が立てる印）も
    同じ `tool_result_clipped` として数える。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"text": "short", "byte_clipped": True}, set(), [], []))
    M.handle({"jsonrpc": "2.0", "id": 602, "method": "tools/call",
             "params": {"name": "file_head", "arguments": {"doc_id": "a.md"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    clip_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "tool_result_clipped"]
    assert len(clip_entries) == 1


def test_internal_and_outer_clip_in_same_call_counted_once(tmp_path, monkeypatch):
    """1回の呼出で内部切り詰め（`text_truncated`）と後段バイト予算クリップの両方が起きても、
    `tool_result_clipped` サイドカーは1回だけ書く（二重計上しない）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setattr(M.agentic_search, "effective_tool_result_max_bytes", lambda **kw: 64)
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"text": "x" * 1000, "text_truncated": True}, set(), [], []))
    M.handle({"jsonrpc": "2.0", "id": 603, "method": "tools/call",
             "params": {"name": "read_doc", "arguments": {"doc_id": "a.md"}}})
    entries = [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]
    clip_entries = [e for e in entries if e.get("kind") == "limit" and e.get("field") == "tool_result_clipped"]
    assert len(clip_entries) == 1, f"二重計上している: {entries!r}"


def test_internal_truncation_flags_ignored_for_tools_outside_either_set(tmp_path, monkeypatch):
    """判定はツール名で閉じている——`_SEARCH_TRUNCATED_TOOLS`/`_BYTE_CLIP_TOOLS` のどちらにも
    属さないツール（`folder_tree`）は `truncated`/`text_truncated` が立っていても何も書かない。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setattr(M.agentic_search, "run_tool",
                        lambda *a, **kw: ({"tree": {}, "truncated": True, "text_truncated": True},
                                          set(), [], []))
    M.handle({"jsonrpc": "2.0", "id": 604, "method": "tools/call",
             "params": {"name": "folder_tree", "arguments": {}}})
    assert not sidecar.exists()


def test_ledger_item_put_refuses_symlinked_items_dir(tmp_path, monkeypatch):
    """台帳 dir は model-shell から書ける場所にある——`items/` を外部への symlink に差し替えても、
    サンドボックスの外で動く本サーバがリンク先へ書かない（fail-closed）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = tmp_path / "investigation"
    directory.mkdir()
    (directory / "items").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    body = _ledger_call("ledger_item_put", _ledger_item())
    assert body["error"] == "ledger_write_failed"
    assert list(outside.iterdir()) == []


def test_ledger_manifest_set_repairs_invalid_regular_file(tmp_path, monkeypatch):
    """内容が規約に合わない manifest（通常ファイル）は検証済み入力で修復できる——本体の継続ゲートが
    「修復してから続けて」と催促する経路の受け口。created_at は既存が文字列なら保持する。"""
    directory = tmp_path / "investigation"
    directory.mkdir()
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    (directory / "manifest.json").write_text(json.dumps({"question_kind": "list", "items": ["a"]}))
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}
    repaired = json.loads((directory / "manifest.json").read_text())
    assert repaired["created_at"] and set(repaired) == {"question_kind", "created_at", "items"}
    (directory / "manifest.json").write_text(json.dumps({"created_at": "2026-09-22T00:00:00+00:00", "items": []}))
    assert _ledger_call("ledger_manifest_set", {"question_kind": "compare", "items": ["a"]}) == {"ok": True}
    assert json.loads((directory / "manifest.json").read_text()) == {
        "question_kind": "compare", "created_at": "2026-09-22T00:00:00+00:00", "items": ["a"]}
    (directory / "manifest.json").write_text("{broken")
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}


def test_ledger_manifest_set_refuses_symlinked_manifest(tmp_path, monkeypatch):
    directory = tmp_path / "investigation"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (directory / "manifest.json").symlink_to(outside)
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    body = _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]})
    assert body["error"] == "ledger_manifest_invalid"
    assert outside.read_text() == "{}"

