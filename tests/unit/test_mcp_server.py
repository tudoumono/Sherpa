"""Sherpa MCP サーバ（stdio・自前実装）の単体テスト。Codex 不要＝JSON-RPC ハンドラと serve ループを直接検証。

ツール実装は agentic_search.run_tool を再利用（v1 フィクスチャの filesystem grep・Neo4j 不要）。
"""
from __future__ import annotations

import io
import json
import os
import pathlib

import pytest

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
os.environ["SHERPA_MCP_WORLD"] = "v1"
os.environ.pop("SHERPA_MCP_SCOPE", None)
from sherpa import mcp_server as M   # noqa: E402
from sherpa import investigation_ledger as IL   # noqa: E402
from sherpa import agentic_search   # noqa: E402
from sherpa import grep_tool   # noqa: E402
import _corpus_expect as CE   # noqa: E402   # フィクスチャ実走査ベースの list_docs 期待値


@pytest.fixture(autouse=True)
def _reset_duplicate_call_cache():
    """同一クエリ重複検知キャッシュ（モジュール変数）をテスト間で持ち越さない。"""
    M._seen_tool_calls.clear()
    yield
    M._seen_tool_calls.clear()


def _rpc(method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return M.handle(msg)


def _call(name, arguments=None):
    return _rpc("tools/call", {"name": name, "arguments": arguments or {}})


def _body(resp):
    return json.loads(resp["result"]["content"][0]["text"])


def _tool_names(resp=None):
    return {t["name"] for t in (resp or _rpc("tools/list"))["result"]["tools"]}


def _entries(sidecar):
    return [json.loads(l) for l in sidecar.read_text(encoding="utf-8").splitlines() if l.strip()]


def _first_tax_rate_doc_id():
    return _body(_call("ripgrep_search", {"query": "TAX-RATE"}))["hits"][0]["doc_id"]


def _stub_run_tool(monkeypatch, result):
    monkeypatch.setattr(M.tool_dispatch, "run_tool", lambda *a, **kw: (result, set(), [], []))


def _raise_graph_era(monkeypatch):
    def _boom(name, args, world, scope_paths, **kw):
        raise M.GraphSchemaEraError(world, "old-era", lens="troubleshoot")
    monkeypatch.setattr(M.tool_dispatch, "run_tool", _boom)


def _final_bytes(obj):
    return len(json.dumps(obj, ensure_ascii=False).encode("utf-8"))


def _hits(n, start=0, text="x" * 200):
    return [{"doc_id": f"doc{i:03d}.md", "line": i + 1, "text": text} for i in range(start, start + n)]


# ===== 台帳ツール =====

def _ledger_call(name, arguments):
    response = _call(name, arguments)["result"]
    body = json.loads(response["content"][0]["text"])
    assert response["isError"] is bool(body.get("error"))
    return body


def _ledger_item(item_id="a", **changes):
    return {"id": item_id, "kind": "row", "subject": "対象", "required_checks": ["source"],
            "evidence": [{"kind": "source", "path": "src/a.py", "line": 1}],
            "status": "source_confirmed", "reason": "", "owner": "parent", **changes}


def _review(**changes):
    return {"purpose": "依頼の目的", "perspectives": ["画面"], "summary": "分かったこと",
            "added_items": [], "removed_items": [], "verdict": "mostly_answered",
            "extra_perspectives": [], **changes}


def test_ledger_tools_exposed_with_exact_input_fields():
    definitions = {tool["name"]: tool for tool in _rpc("tools/list")["result"]["tools"]}
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
    assert done["complete"] is True and done["counts"]["source_confirmed"] == 1
    assert _ledger_call("ledger_manifest_set", {"question_kind": "compare", "items": ["a", "b"]}) == {"ok": True}
    assert json.loads((directory / "manifest.json").read_text()) == {
        "question_kind": "compare", "items": ["a", "b"], "created_at": created_at}
    assert _ledger_call("ledger_status", {})["missing"] == ["b"]


def test_ledger_review_put_rejected_before_any_item_terminalizes_then_accepted(tmp_path, monkeypatch):
    """終端の item が 1 件も無い時点の ledger_review_put は書き込まず拒否・終端後は受理され terminal_count が付く。"""
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]})
    _ledger_call("ledger_item_put", _ledger_item(status="pending", evidence=[]))
    assert _ledger_call("ledger_review_put", _review())["error"] == "ledger_review_rejected"
    assert not (directory / "reviews.jsonl").exists()
    _ledger_call("ledger_item_put", _ledger_item())
    assert _ledger_call("ledger_review_put", _review()) == {"ok": True}
    assert json.loads((directory / "reviews.jsonl").read_text().splitlines()[0])["terminal_count"] == 1


def test_ledger_review_put_ignores_terminal_items_outside_the_manifest(tmp_path, monkeypatch):
    """terminal_count は manifest 登録済み id の終端だけを数える（目録外 "b" が終端でも "a" 非終端なら拒否）。"""
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]})
    _ledger_call("ledger_item_put", _ledger_item("a", status="pending", evidence=[]))
    _ledger_call("ledger_item_put", _ledger_item("b"))
    assert _ledger_call("ledger_review_put", _review())["error"] == "ledger_review_rejected"
    assert not (directory / "reviews.jsonl").exists()

    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a", "b"]})
    _ledger_call("ledger_item_put", _ledger_item("a"))
    assert _ledger_call("ledger_review_put", _review()) == {"ok": True}
    assert json.loads((directory / "reviews.jsonl").read_text().splitlines()[-1])["terminal_count"] == 2


def test_ledger_status_reports_unsatisfied_invalid_and_missing(tmp_path, monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a", "bad", "missing"]})
    item = _ledger_item(status="spec_only", required_checks=["source", "spec_doc"],
                        evidence=[{"kind": "spec_doc", "path": "docs/spec.md", "line": 2}])
    assert _ledger_call("ledger_item_put", item) == {"ok": True, "id": "a"}
    (tmp_path / "items/bad.json").write_text("{broken", encoding="utf-8")
    status = _ledger_call("ledger_status", {})
    assert status["complete"] is False and status["manifest_invalid"] is False and status["items"] == 2
    assert status["non_terminal"] == ["a"] and status["unsatisfied"] == {"a": ["source"]}
    assert status["invalid"] == ["bad"] and status["missing"] == ["missing"]
    assert status["counts"]["spec_only"] == 1

    # SHERPA_MCP_LEDGER_REQUIRED_EXTRA があれば、item が source を宣言していなくても unsatisfied に載る。
    monkeypatch.setenv("SHERPA_MCP_LEDGER_REQUIRED_EXTRA", "source")
    assert _ledger_call("ledger_manifest_set",
                        {"question_kind": "list", "items": ["a", "bad", "missing", "c"]}) == {"ok": True}
    no_source_declared = _ledger_item(
        item_id="c", status="spec_only", required_checks=["spec_doc"],
        evidence=[{"kind": "spec_doc", "path": "docs/other.md", "line": 1}])
    assert _ledger_call("ledger_item_put", no_source_declared) == {"ok": True, "id": "c"}
    assert _ledger_call("ledger_status", {})["unsatisfied"]["c"] == ["source"]


@pytest.mark.parametrize("arguments", [{"question_kind": "list"},
                                      {"question_kind": "", "items": ["a"]},
                                      {"question_kind": "list", "items": [1]}])
def test_ledger_manifest_invalid_returns_problems_without_writing(tmp_path, monkeypatch, arguments):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    result = _ledger_call("ledger_manifest_set", arguments)
    assert result["error"] == "ledger_manifest_invalid" and result["problems"]
    assert not (tmp_path / "manifest.json").exists()


@pytest.mark.parametrize("changes", [{"status": "done"}, {"required_checks": []},
                                    {"evidence": []}, {"extra": "body"},
                                    {"id": "../outside"}, {"id": "a/b"}, {"id": ""}, {"id": ".hidden"}])
def test_ledger_item_invalid_returns_problems_without_replacing_item(tmp_path, monkeypatch, changes):
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(tmp_path))
    item = _ledger_item()
    _ledger_call("ledger_item_put", item)
    result = _ledger_call("ledger_item_put", {**item, **changes})
    assert result["error"] == "ledger_item_invalid" and result["problems"]
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
    assert result["error"] == "ledger_write_failed" and result["problems"]
    assert f"{name} failed:" in capsys.readouterr().err
    assert destination.read_text() == "keep"


def test_ledger_item_put_refuses_symlinked_items_dir(tmp_path, monkeypatch):
    """items/ を外部への symlink に差し替えても、リンク先へ書かない（fail-closed）。"""
    outside = tmp_path / "outside"
    outside.mkdir()
    directory = tmp_path / "investigation"
    directory.mkdir()
    (directory / "items").symlink_to(outside, target_is_directory=True)
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    assert _ledger_call("ledger_item_put", _ledger_item())["error"] == "ledger_write_failed"
    assert list(outside.iterdir()) == []


def test_ledger_manifest_set_repairs_invalid_regular_file(tmp_path, monkeypatch):
    """規約に合わない manifest（通常ファイル）は検証済み入力で修復できる。created_at は既存が文字列なら保持。"""
    directory = tmp_path / "investigation"
    directory.mkdir()
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    manifest = directory / "manifest.json"
    manifest.write_text(json.dumps({"question_kind": "list", "items": ["a"]}))
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}
    repaired = json.loads(manifest.read_text())
    assert repaired["created_at"] and set(repaired) == {"question_kind", "created_at", "items"}
    manifest.write_text(json.dumps({"created_at": "2026-09-22T00:00:00+00:00", "items": []}))
    assert _ledger_call("ledger_manifest_set", {"question_kind": "compare", "items": ["a"]}) == {"ok": True}
    assert json.loads(manifest.read_text()) == {
        "question_kind": "compare", "created_at": "2026-09-22T00:00:00+00:00", "items": ["a"]}
    manifest.write_text("{broken")
    assert _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]}) == {"ok": True}


def test_ledger_manifest_set_refuses_symlinked_manifest(tmp_path, monkeypatch):
    directory = tmp_path / "investigation"
    directory.mkdir()
    outside = tmp_path / "outside.json"
    outside.write_text("{}")
    (directory / "manifest.json").symlink_to(outside)
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    body = _ledger_call("ledger_manifest_set", {"question_kind": "list", "items": ["a"]})
    assert body["error"] == "ledger_manifest_invalid" and outside.read_text() == "{}"


# ===== プロトコル・tools/list =====

def test_protocol_basics_initialize_notification_unknown_method():
    r = _rpc("initialize", {"protocolVersion": "2025-06-18", "capabilities": {}})["result"]
    assert r["protocolVersion"] == "2025-06-18"
    assert "tools" in r["capabilities"] and r["serverInfo"]["name"] == "sherpa"
    assert M.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None
    assert _rpc("bogus/method")["error"]["code"] == -32601


def test_serve_loop_roundtrip():
    """serve() は改行区切り JSON-RPC を読み応答を 1 行ずつ返す（通知は応答なし）。"""
    lines = [json.dumps({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}}),
             json.dumps({"jsonrpc": "2.0", "method": "notifications/initialized"}),
             json.dumps({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})]
    out = io.StringIO()
    M.serve(stdin=io.StringIO("\n".join(lines) + "\n"), stdout=out)
    responses = [json.loads(l) for l in out.getvalue().splitlines() if l.strip()]
    assert [r["id"] for r in responses] == [1, 2]
    assert any(t["name"] == "ripgrep_search" for t in responses[1]["result"]["tools"])


def test_tools_list_exposes_family_with_schemas_shared_with_agentic_search():
    tools = _rpc("tools/list")["result"]["tools"]
    byname = {t["name"]: t for t in tools}
    assert {"list_docs", "ripgrep_search", "read_around", "graph_neighbors", "ask_user", "read_doc", "doc_outline",
            "glob_search", "xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages",
            "file_head"} <= set(byname)
    assert all(t["inputSchema"]["type"] == "object" for t in tools)
    for name, desc, params in [
            ("ask_user", agentic_search._DESC_ASK, None),
            ("read_doc", agentic_search._DESC_READ_DOC, agentic_search._PARAMS_READ_DOC),
            ("doc_outline", agentic_search._DESC_OUTLINE, agentic_search._PARAMS_OUTLINE),
            ("glob_search", agentic_search._DESC_GLOB, agentic_search._PARAMS_GLOB),
            ("xlsx_range", agentic_search._DESC_XLSX_RANGE, agentic_search._PARAMS_XLSX_RANGE),
            ("file_head", agentic_search._DESC_FILE_HEAD, agentic_search._PARAMS_FILE_HEAD)]:
        assert byname[name]["description"] == desc, name
        if params is not None:
            assert byname[name]["inputSchema"] == params, name


def test_tools_list_order_stable_with_and_without_es(monkeypatch):
    """es_search は ripgrep_search の直後に差し込まれるだけで、他の土台系ツールの並びは崩れない。"""
    foundation = ["list_docs", "folder_tree", "ripgrep_search", "glob_search", "doc_outline",
                  "read_doc", "read_around", "compare_documents"]
    monkeypatch.setattr(M.es_index, "available", lambda: False)
    names_no_es = [t["name"] for t in _rpc("tools/list")["result"]["tools"]]
    assert "es_search" not in names_no_es and [n for n in names_no_es if n in foundation] == foundation
    monkeypatch.setattr(M.es_index, "available", lambda: True)
    names_es = [t["name"] for t in _rpc("tools/list")["result"]["tools"]]
    assert names_es.index("es_search") == names_es.index("ripgrep_search") + 1
    assert [n for n in names_es if n in foundation] == foundation


def test_world_scope_and_layer_from_env(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_SCOPE", "4期\n00_共通")
    monkeypatch.setenv("SHERPA_MCP_LAYER", "code")
    assert M._world() == "v1" and M._scope() == ["4期", "00_共通"] and M._layer() == "code"
    monkeypatch.delenv("SHERPA_MCP_SCOPE")
    monkeypatch.delenv("SHERPA_MCP_LAYER")
    assert M._scope() is None and M._layer() is None


@pytest.mark.parametrize("layer", [None, "both", "docs", "code"])
def test_tools_list_by_layer(monkeypatch, layer):
    """層が限定されている間は graph_neighbors（docs/code）と Office/PDF 読取 5 本（code）を出さない。
    file_head・他の土台系は層に関係なく残る。"""
    if layer:
        monkeypatch.setenv("SHERPA_MCP_LAYER", layer)
    else:
        monkeypatch.delenv("SHERPA_MCP_LAYER", raising=False)
    names = _tool_names()
    assert {"list_docs", "ripgrep_search", "read_around", "file_head"} <= names
    assert ("graph_neighbors" in names) is (layer in (None, "both"))
    # 影響の道具（構造の辺だけ）は資料のみの層でだけ外す＝ソース限定でも出す
    assert ({"graph_resolve", "graph_impact"} <= names) is (layer != "docs")
    assert not (layer == "docs" and {"graph_resolve", "graph_impact"} & names)
    office = {"xlsx_sheets", "xlsx_range", "docx_paragraphs", "pptx_slides", "pdf_pages"}
    assert (office <= names) is (layer != "code")
    assert not (layer == "code" and office & names)


def test_toolset_plain_exposes_only_three_and_rejects_others(monkeypatch):
    """素の Codex モード（plain）は es_search・graph_neighbors・ask_user だけを出す。他は直接 tools/call
    されても存在しないツールと同じエラーで拒否する。graph_neighbors の schema に item は出さない。"""
    monkeypatch.setattr(M.es_index, "available", lambda: True)
    monkeypatch.setenv("SHERPA_MCP_TOOLSET", "plain")
    resp = _rpc("tools/list")
    assert _tool_names(resp) == {"graph_neighbors", "ask_user"}  # graph_resolve・graph_impact は plain に出さない
    descs = " ".join(t["description"] for t in resp["result"]["tools"])
    assert not any(n in descs for n in ("ripgrep_search", "read_doc", "read_around", "list_docs"))
    graph = next(t for t in resp["result"]["tools"] if t["name"] == "graph_neighbors")
    assert "item" not in graph["inputSchema"]["properties"]
    for name, args in (("ripgrep_search", {"query": "x"}), ("ledger_status", {})):
        rejected = _call(name, args)
        assert rejected["result"]["isError"] is True
        assert _body(rejected) == {"error": f"unknown tool: {name}"}


# ===== ask_user =====

def test_ask_user_first_then_again_within_execution_and_sidecar(tmp_path, monkeypatch):
    """1 実行 1 回: 1 回目と 2 回目で別の結果文言。サイドカーへ書く質問は初回のみ。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    M._ASK_STATE["count"] = 0
    args = {"prompt": "対象範囲は？", "mode": "single", "options": [{"label": "A"}, {"label": "B"}]}
    try:
        r1, r2 = _call("ask_user", args), _call("ask_user", args)
        assert r1["result"]["isError"] is False and r2["result"]["isError"] is False
        assert M._ASK_RESULT_FIRST in r1["result"]["content"][0]["text"]
        assert M._ASK_RESULT_AGAIN in r2["result"]["content"][0]["text"]
        assert "既に質問済み" in r2["result"]["content"][0]["text"]
        ask_entries = [e for e in _entries(sidecar) if e.get("kind") == "ask_user"]
        assert len(ask_entries) == 1
        q = ask_entries[0]["question"]
        assert q["prompt"] == "対象範囲は？" and q["mode"] == "single" and len(q["options"]) == 2
    finally:
        M._ASK_STATE["count"] = 0


def test_ask_disabled_env_hides_tool_and_forces_again_reply(monkeypatch):
    """確認 ID 付き再送では ask_user を tools/list に出さず、呼ばれても初回から「既に質問済み」を返す。"""
    monkeypatch.setenv("SHERPA_MCP_ASK_DISABLED", "1")
    M._ASK_STATE["count"] = 0
    assert M._ask_disabled() is True
    names = _tool_names()
    assert "ask_user" not in names and {"list_docs", "ripgrep_search", "read_around", "graph_neighbors"} <= names
    call = _call("ask_user", {"prompt": "無視されるはず", "mode": "single",
                              "options": [{"label": "A"}, {"label": "B"}]})
    assert call["result"]["isError"] is False
    assert call["result"]["content"][0]["text"] == M._ASK_RESULT_AGAIN
    assert M._ASK_STATE["count"] == 0


# ===== フィクスチャ上のツール呼び出し・層フィルタ =====

def test_tools_call_ripgrep_list_docs_read_doc_outline_glob_on_fixtures():
    resp = _call("ripgrep_search", {"query": "TAX-RATE"})
    assert resp["result"]["content"][0]["type"] == "text"
    assert _body(resp)["hits"] and not resp["result"]["isError"]
    doc_id = _body(resp)["hits"][0]["doc_id"]

    resp = _call("list_docs", {"path_prefix": "4期/02_設計"})
    payload = _body(resp)
    expected = CE.count_under("4期/02_設計")
    assert not resp["result"]["isError"] and payload["count"] == expected and len(payload["docs"]) == expected
    assert all(d["rel_path"].startswith("4期/02_設計/") for d in payload["docs"])

    resp = _call("read_doc", {"doc_id": doc_id})
    assert not resp["result"]["isError"]
    payload = _body(resp)
    assert payload["doc_id"] == doc_id and "text" in payload and "total_lines" in payload
    resp = _call("doc_outline", {"doc_id": doc_id})
    assert not resp["result"]["isError"]
    assert _body(resp)["doc_id"] == doc_id and "headings" in _body(resp)

    resp = _call("glob_search", {"pattern": "*.md"})
    payload = _body(resp)
    assert not resp["result"]["isError"] and payload["count"] > 0
    assert all(p.lower().endswith(".md") for p in payload["paths"])


def test_tools_call_graph_neighbors_stubbed(monkeypatch):
    from sherpa import lens_service
    fake = [{"name": "BILLINGJOB", "label": "Module", "category": "プログラム", "role": "実装",
             "distance": 2, "path": ["請求", "BILLINGJOB"], "evidence": {"edges": [], "grep": []}}]
    monkeypatch.setattr(lens_service, "neighbor_cards", lambda world, term, sp=None: list(fake))
    n = _body(_call("graph_neighbors", {"name": "請求"}))["neighbors"][0]
    assert n["name"] == "BILLINGJOB" and n["role"] == "実装" and n["path"]


def test_graph_schema_era_error_is_structured_tool_error_with_sidecar_code_and_coverage(tmp_path, monkeypatch):
    """旧世代グラフは JSON-RPC エラーでなく isError の通常ツール結果（graph_reingest_required）で返す。
    サイドカーには閉じたコードだけ（world/stored_era は書かない）、item 付きなら coverage へ error を記録する。"""
    sidecar = tmp_path / "sidecar.jsonl"
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    _raise_graph_era(monkeypatch)
    resp = _call("graph_neighbors", {"name": "請求", "item": "e"})
    assert "error" not in resp and resp["result"]["isError"] is True
    assert _body(resp) == {"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"}
    entries = _entries(sidecar)
    assert len(entries) == 1 and set(entries[0]) == {"kind", "code", "tool", "ts"}
    assert entries[0]["kind"] == "error" and entries[0]["code"] == "graph_reingest_required"
    assert entries[0]["tool"] == "graph_neighbors"
    assert IL.load_coverage(directory) == {"e": ("error",)}


def test_tools_call_graph_impact_and_resolve_stubbed(tmp_path, monkeypatch):
    """通常の呼び出しが成功し、結果が item の台帳記録（打ち切りは limit）まで届く。層の限定中でも graph_impact は通る。"""
    from sherpa import graph_tools
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    seen = []

    def fake_run(name, args, world, sp, layer=None):
        seen.append((name, layer))
        if name == "graph_resolve":
            return {"candidates": [{"canonical_id": "c1", "name": "A"}], "count": 1,
                    "coverage": {"complete": True, "limits": [], "omitted": 0}}
        return {"start": {"canonical_id": "c1"}, "impact": [], "count": 0, "truncated": True,
                "coverage": {"complete": False, "limits": [{"kind": "depth", "stage": "impact"}], "omitted": None,
                             "depth": {"requested": 5, "truncated": True}}}

    monkeypatch.setattr(graph_tools, "run", fake_run)
    monkeypatch.setenv("SHERPA_MCP_LAYER", "code")
    assert _body(_call("graph_resolve", {"name": "A", "item": "r"}))["candidates"][0]["canonical_id"] == "c1"
    resp = _call("graph_impact", {"canonical_id": "c1", "item": "i"})
    assert not resp["result"]["isError"] and _body(resp)["coverage"]["complete"] is False
    assert seen == [("graph_resolve", "code"), ("graph_impact", "code")]
    assert IL.load_coverage(directory) == {"r": ("hit",), "i": ("limit",)}


def test_graph_impact_rejected_when_layer_is_docs_and_era_error_is_structured(tmp_path, monkeypatch):
    from sherpa import graph_tools
    monkeypatch.setattr(graph_tools, "run", lambda *a, **k: (_ for _ in ()).throw(AssertionError("層 docs で実行された")))
    monkeypatch.setenv("SHERPA_MCP_LAYER", "docs")
    resp = _call("graph_impact", {"canonical_id": "c1"})
    assert resp["result"]["isError"] is True and "error" in _body(resp)
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    monkeypatch.delenv("SHERPA_MCP_LAYER")
    monkeypatch.setattr(graph_tools, "run", lambda *a, **k: (_ for _ in ()).throw(M.GraphSchemaEraError("v1", "old-era")))
    resp = _call("graph_resolve", {"name": "A"})
    assert resp["result"]["isError"] is True
    assert _body(resp) == {"error": "graph_reingest_required", "world": "v1", "stored_era": "old-era"}
    assert _entries(sidecar)[0]["code"] == "graph_reingest_required"


def test_graph_impact_result_over_budget_keeps_coverage_and_start(monkeypatch):
    """最終防衛線のクリップは影響先の末尾から削り、start・coverage を残して result_cap を足す。"""
    big = {"start": {"canonical_id": "c1"}, "count": 40,
           "impact": [{"canonical_id": f"c{i}", "name": "N" * 100} for i in range(40)],
           "coverage": {"complete": True, "limits": [], "omitted": 0}}
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "2000")
    clipped, was_clipped = M._clip_tool_result(big, name="graph_impact", args={"canonical_id": "c1"})
    assert was_clipped and _final_bytes(clipped) <= 2000 and clipped["start"] == {"canonical_id": "c1"}
    assert clipped["truncated"] is True and clipped["count"] == 40
    assert {"kind": "result_cap", "stage": "impact"} in clipped["coverage"]["limits"]
    assert clipped["coverage"]["omitted"] == 40 - len(clipped["impact"])


def test_tool_error_without_known_code_writes_no_sidecar_entry(tmp_path, monkeypatch):
    """自由文のツールエラーはサイドカーに書かない（観測できるのは閉集合のコードだけ）。"""
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    resp = _call("read_around", {"doc_id": "居ない.md", "line": 1})
    assert resp["result"]["isError"] is True and not sidecar.exists()


def test_tools_call_forwards_layer_to_run_tool(monkeypatch):
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured["layer"] = kw.get("layer")
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(M.tool_dispatch, "run_tool", fake_run_tool)
    monkeypatch.setenv("SHERPA_MCP_LAYER", "docs")
    _call("ripgrep_search", {"query": "x"})
    assert captured.get("layer") == "docs"


@pytest.mark.usefixtures("upstream_only_registry")
def test_tools_call_ripgrep_respects_layer_on_fixtures(monkeypatch):
    """layer=code では資料（.md）ヒットが除外される（"TAX-RATE" は .md と .cbl/.cpy の両方に実在する語）。"""
    monkeypatch.setenv("SHERPA_MCP_LAYER", "code")
    exts = {pathlib.Path(h["doc_id"]).suffix.lower() for h in _body(_call("ripgrep_search", {"query": "TAX-RATE"}))["hits"]}
    assert exts and not (exts & {".md", ".markdown"})


def test_tools_call_graph_neighbors_rejected_when_layer_restricted(monkeypatch):
    """tools/list から隠すだけでなく、直接 tools/call されても run_tool 側で拒否する（多層防御）。"""
    from sherpa import lens_service

    def _boom(world, term, sp=None):
        raise AssertionError("層限定なのに neighbor_cards が呼ばれている")

    monkeypatch.setattr(lens_service, "neighbor_cards", _boom)
    monkeypatch.setenv("SHERPA_MCP_LAYER", "code")
    resp = _call("graph_neighbors", {"name": "請求"})
    assert resp["result"]["isError"] is True and "error" in _body(resp)


# ===== サイドカー（子エージェントの MCP 呼出観測・本文は書かない） =====

def test_read_doc_writes_sidecar_entry_without_body_and_ripgrep_writes_none(tmp_path, monkeypatch):
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    doc_id = _first_tax_rate_doc_id()
    assert not sidecar.exists()           # 読取系以外（ripgrep_search）は書かない
    assert not _call("read_doc", {"doc_id": doc_id})["result"]["isError"]
    reads = [e for e in _entries(sidecar) if e.get("kind") == "read" and e.get("tool") == "read_doc"]
    assert len(reads) == 1 and reads[0]["doc_id"] == doc_id
    assert set(reads[0]) == {"kind", "tool", "doc_id", "ts"} and isinstance(reads[0]["ts"], (int, float))


def test_sidecar_append_write_failure_logs_warning_once_without_body(tmp_path, monkeypatch, capsys):
    """サイドカー書込失敗は型と errno だけを stderr へ 1 回だけ知らせる（ツール呼出は成功のまま）。"""
    bad_sidecar = tmp_path / "does-not-exist" / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(bad_sidecar))
    M._sidecar_write_failed_once = False
    try:
        doc_id = _first_tax_rate_doc_id()
        assert not _call("read_doc", {"doc_id": doc_id})["result"]["isError"]
        # 2 回目は start_line を足して重複拒否を避ける（主眼は警告が 1 回だけ出ること）。
        assert not _call("read_doc", {"doc_id": doc_id, "start_line": 1})["result"]["isError"]
        err = capsys.readouterr().err
        lines = [l for l in err.splitlines() if "sidecar write failed" in l]
        assert len(lines) == 1 and "FileNotFoundError" in lines[0]
        assert doc_id not in lines[0] and str(bad_sidecar) not in lines[0]
    finally:
        M._sidecar_write_failed_once = False


@pytest.mark.parametrize("tool, arguments, result, budget, field", [
    ("ripgrep_search", {"query": "x"}, {"hits": ["a"], "truncated": True}, None, "search_truncated"),
    ("read_doc", {"doc_id": "a.md"}, {"text": "short", "text_truncated": True}, None, "tool_result_clipped"),
    ("file_head", {"doc_id": "a.md"}, {"text": "short", "byte_clipped": True}, None, "tool_result_clipped"),
    ("ripgrep_search", {"query": "w"}, {"hits": ["w" * 1000]}, 64, "tool_result_clipped"),     # 後段のバイト予算クリップ
    ("read_doc", {"doc_id": "a.md"}, {"text": "x" * 1000, "text_truncated": True}, 64, "tool_result_clipped"),  # 内部＋後段でも 1 回
    ("folder_tree", {}, {"tree": {}, "truncated": True, "text_truncated": True}, None, None),   # 対象外ツールは書かない
])
def test_sidecar_limit_entries(tmp_path, monkeypatch, tool, arguments, result, budget, field):
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    if budget:
        monkeypatch.setattr(agentic_search, "effective_tool_result_max_bytes", lambda **kw: budget)
    _stub_run_tool(monkeypatch, result)
    _call(tool, arguments)
    if field is None:
        assert not sidecar.exists()
        return
    limits = [e for e in _entries(sidecar) if e.get("kind") == "limit"]
    assert [e["field"] for e in limits] == [field], limits


def test_duplicate_tool_call_returns_error_without_calling_run_tool_and_writes_sidecar(tmp_path, monkeypatch):
    sidecar = tmp_path / "sidecar.jsonl"
    monkeypatch.setenv("SHERPA_MCP_SIDECAR", str(sidecar))
    calls = []

    def fake_run_tool(name, args, world, scope_paths, **kw):
        calls.append(name)
        return ({"hits": ["ok"]}, set(), [], [])

    monkeypatch.setattr(M.tool_dispatch, "run_tool", fake_run_tool)
    r1 = _call("ripgrep_search", {"query": "同じ条件"})
    r2 = _call("ripgrep_search", {"query": "同じ条件"})
    assert not r1["result"]["isError"] and r2["result"]["isError"] is True
    assert _body(r2)["error"] == "duplicate_tool_call"
    assert len(calls) == 1
    dups = [e for e in _entries(sidecar) if e.get("kind") == "limit" and e.get("field") == "duplicate_tool_call"]
    assert len(dups) == 1


def test_duplicate_tool_call_argument_order_is_order_independent(monkeypatch):
    _stub_run_tool(monkeypatch, {"doc_id": "a.md", "text": "..."})
    r1 = _call("read_doc", {"doc_id": "a.md", "start_line": 1})
    r2 = _call("read_doc", {"start_line": 1, "doc_id": "a.md"})
    assert not r1["result"]["isError"] and r2["result"]["isError"] is True
    assert _body(r2)["error"] == "duplicate_tool_call"


def test_duplicate_tool_call_exempts_budget_exempt_tools(monkeypatch):
    _stub_run_tool(monkeypatch, {"count": 0, "docs": []})
    r1 = _call("list_docs", {"path_prefix": ""})
    r2 = _call("list_docs", {"path_prefix": ""})
    assert not r1["result"]["isError"] and not r2["result"]["isError"]


def test_duplicate_tool_call_cache_is_lru_bounded(monkeypatch):
    _stub_run_tool(monkeypatch, {"hits": ["ok"]})
    for i in range(M._DUPLICATE_CALL_CACHE_MAX + 1):
        _call("ripgrep_search", {"query": f"q{i}"})
    assert not _call("ripgrep_search", {"query": "q0"})["result"]["isError"]    # 最古キーは追い出し済み


def test_duplicate_tool_call_excludes_item_from_key_and_backfills_new_item_coverage(tmp_path, monkeypatch):
    """item だけが違う同一条件の呼出は重複拒否されるが、item 付きなら初回の結果区分をその item にも記録する。"""
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    _stub_run_tool(monkeypatch, {"hits": [{"doc_id": "a.md", "text": "x"}]})
    r1 = _call("ripgrep_search", {"query": "COD16-DUP-ITEM-KEY", "item": "h1"})
    r2 = _call("ripgrep_search", {"query": "COD16-DUP-ITEM-KEY", "item": "h2"})
    assert not r1["result"]["isError"] and r2["result"]["isError"]
    assert _body(r2)["error"] == "duplicate_tool_call"
    assert IL.load_coverage(directory) == {"h1": ("hit",), "h2": ("hit",)}


# ===== ツール結果のバイト予算（1 件あたり）=====

@pytest.mark.parametrize("env_value", ["0", "-1", "not-a-number", ""])
def test_env_budget_bytes_invalid_falls_back_to_effective_call(monkeypatch, env_value):
    """未設定/不正値は effective_tool_result_max_bytes へフォールバック。1 件も残せない予算でも空ページにしない。"""
    if env_value:
        monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", env_value)
    monkeypatch.setattr(agentic_search, "effective_tool_result_max_bytes", lambda **kw: 256)
    _stub_run_tool(monkeypatch, {"hits": ["x" * 1000]})
    body = _body(_call("ripgrep_search", {"query": "x"}))
    assert body["truncated"] is True and body["partial_hit"] is True
    assert len(body["hits"]) == 1 and body["hits"][0] != ""
    assert body["next_offset"] == 1 and "clipped_bytes" not in body


def test_env_budget_bytes_takes_precedence_over_effective_call(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "64")
    called = []
    monkeypatch.setattr(agentic_search, "effective_tool_result_max_bytes", lambda **kw: called.append(1) or 999999)
    _stub_run_tool(monkeypatch, {"hits": ["x" * 1000]})
    assert _body(_call("ripgrep_search", {"query": "x"}))["truncated"] is True
    assert not called


@pytest.mark.parametrize("env", [
    {"SHERPA_MCP_TOOL_MAX_HITS": "77", "SHERPA_MCP_TOOL_WINDOW_CAP": "88", "SHERPA_MCP_TOOL_BUDGET_BYTES": "99999"},
    {},
])
def test_tools_call_forwards_env_hits_window_and_bytes_to_run_tool(monkeypatch, env):
    """env の hits/window/bytes が run_tool へ届く。未設定は None（run_tool 自身の既定へフォールバック）。"""
    captured = {}

    def fake_run_tool(name, args, world, scope_paths, **kw):
        captured.update(kw)
        return ({"hits": []}, set(), [], [])

    monkeypatch.setattr(M.tool_dispatch, "run_tool", fake_run_tool)
    for var in ("SHERPA_MCP_TOOL_MAX_HITS", "SHERPA_MCP_TOOL_WINDOW_CAP", "SHERPA_MCP_TOOL_BUDGET_BYTES"):
        monkeypatch.delenv(var, raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    _call("ripgrep_search", {"query": "x"})
    assert (captured.get("max_hits"), captured.get("window_cap"), captured.get("tool_result_max_bytes")) == (
        (77, 88, 99999) if env else (None, None, None))


@pytest.mark.parametrize("result, budget", [
    ({"text": '"' * 70000}, 65536),        # 引用符だらけ＝再エスケープで上限の約 2 倍に膨らんでいた実害の再現
    ({"hits": ["x" * 1000]}, 256),
])
def test_clip_tool_result_final_json_stays_within_budget(monkeypatch, result, budget):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", str(budget))
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True and _final_bytes(clipped) <= budget


def test_tools_call_final_wire_text_stays_within_budget_for_quote_heavy_result(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "65536")
    _stub_run_tool(monkeypatch, {"text": '"' * 70000})
    wire_text = _call("ripgrep_search", {"query": "x"})["result"]["content"][0]["text"]
    assert len(wire_text.encode("utf-8")) <= 65536


def test_ripgrep_search_oversized_hit_set_absorbed_before_outer_clip(monkeypatch):
    """run_tool は実装を使う。直列化後の実バイト数で収まりを保証するため、外側クリップが平文へ潰さず
    next_offset・全ヒットの doc_id が構造ごと残る。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", str(64 * 1024))
    n_hits = agentic_search.MAX_HITS
    hits = [{"doc_id": f"doc{i:03d}.md", "path": f"/x/doc{i:03d}.md", "ext": ".md",
             "line": 1, "span": [1, 1], "text": "x" * 5000, "match": "x"} for i in range(n_hits)]
    monkeypatch.setattr(grep_tool, "grep_search", lambda *a, **kw: hits)
    resp = _call("ripgrep_search", {"query": "x"})
    assert resp["result"]["isError"] is False
    wire_text = resp["result"]["content"][0]["text"]
    assert len(wire_text.encode("utf-8")) <= 64 * 1024
    body = json.loads(wire_text)
    assert "clipped_bytes" not in body and body.get("next_offset") == n_hits
    assert [h["doc_id"] for h in body["hits"]] == [f"doc{i:03d}.md" for i in range(n_hits)]


def test_clip_tool_result_hits_field_preserves_structure_and_next_offset(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "900")
    hits = _hits(10)
    clipped, was_clipped = M._clip_tool_result({"hits": hits}, name="ripgrep_search", args={})
    assert was_clipped is True and _final_bytes(clipped) <= 900 and clipped["truncated"] is True
    kept = clipped["hits"]
    assert 0 < len(kept) < len(hits) and kept == hits[:len(kept)]
    assert clipped["next_offset"] == len(kept)


def test_clip_tool_result_hits_field_next_offset_uses_call_offset_not_reverse_calc(monkeypatch):
    """最終ページ（next_offset を持たない）を外側クリップがさらに縮めても next_offset は呼出 offset 基準
    （offset=20 の最終 5 件を 2 件に切ったとき 2 に巻き戻っていた不具合の再現）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "700")
    hits = _hits(5, start=20)
    clipped, was_clipped = M._clip_tool_result({"hits": hits}, name="ripgrep_search", args={"offset": 20})
    assert was_clipped is True and len(clipped["hits"]) == 2 and clipped["hits"] == hits[:2]
    assert clipped["next_offset"] == 22


def test_clip_tool_result_es_search_hits_never_get_next_offset(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "900")
    hits = _hits(10)
    clipped, was_clipped = M._clip_tool_result({"hits": hits}, name="es_search", args={})
    assert was_clipped is True and clipped["truncated"] is True
    assert 0 < len(clipped["hits"]) < len(hits) and "next_offset" not in clipped


def test_clip_tool_result_hits_single_oversized_hit_shrinks_body_instead_of_going_empty(monkeypatch):
    """先頭 1 件すら丸ごと残せなくても hits=[] に縮退せず、本文を縮めて位置情報付きの 1 件を残し
    next_offset=offset+1 で進捗を保証する（進捗ゼロだと同一引数の再呼出が重複拒否で詰む）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "500")
    hit = {"doc_id": "a/b/c.md", "line": 1, "text": "y" * 50000}
    clipped, was_clipped = M._clip_tool_result({"hits": [hit]}, name="ripgrep_search", args={"offset": 20})
    assert was_clipped is True and _final_bytes(clipped) <= 500
    assert clipped["truncated"] is True and clipped["partial_hit"] is True and len(clipped["hits"]) == 1
    kept = clipped["hits"][0]
    assert kept["doc_id"] == "a/b/c.md" and 0 < len(kept["text"]) < len(hit["text"])
    assert clipped["next_offset"] == 21


def test_clip_tool_result_hits_position_info_alone_over_budget_returns_error(monkeypatch):
    """位置情報だけで予算を超える（極端に長い doc_id）場合は偽の成功ページでなく tool_result_budget_too_small。
    handle() 経由でも isError=true になる（クリップ前の結果で確定していた不具合の再現）。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "900")
    hit = {"doc_id": "d" * 1007, "line": 1, "text": "y" * 5000}
    clipped, was_clipped = M._clip_tool_result({"hits": [hit]}, name="ripgrep_search", args={"offset": 20})
    assert was_clipped is True and _final_bytes(clipped) <= 900
    assert clipped == {"error": "tool_result_budget_too_small",
                       "hint": M._TOOL_RESULT_BUDGET_TOO_SMALL_HINT, "offset": 20}
    _stub_run_tool(monkeypatch, {"hits": [hit]})
    resp = _call("ripgrep_search", {"query": "x", "offset": 20})
    assert resp["result"]["isError"] is True and _body(resp)["error"] == "tool_result_budget_too_small"


def test_clip_tool_result_read_doc_field_preserves_end_line_and_cuts_on_line_boundary(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "800")
    lines = [f"{i}: line content number {i} " + "x" * 50 for i in range(1, 51)]
    result = {"doc_id": "a.md", "start_line": 1, "end_line": 50, "total_lines": 500, "text": "\n".join(lines)}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True and _final_bytes(clipped) <= 800
    assert clipped["truncated"] is True and clipped["total_lines"] == 500
    kept_lines = clipped["text"].split("\n")
    assert 0 < len(kept_lines) < len(lines) and kept_lines == lines[:len(kept_lines)]
    assert clipped["end_line"] == len(kept_lines)


def test_clip_tool_result_read_doc_single_long_line_keeps_partial_text_and_progresses(monkeypatch):
    """1 行も丸ごとは収まらない場合も、先頭行を縮めて非空の text を返し end_line=start_line で進捗を保証する。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "40000")
    result = {"doc_id": "a.md", "start_line": 5, "end_line": 5, "total_lines": 100, "text": "x" * 65536}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is True and _final_bytes(clipped) <= 40000
    assert clipped["text"] != "" and clipped["end_line"] == 5
    assert clipped["partial_line"] is True and clipped["truncated"] is True
    assert "start_line=6" in clipped["note"]


def test_clip_tool_result_unknown_shape_falls_back_with_note(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "500")
    clipped, was_clipped = M._clip_tool_result({"foo": "x" * 100000})
    assert was_clipped is True and _final_bytes(clipped) <= 500
    assert clipped["truncated"] is True and clipped["note"] == M._CLIP_FALLBACK_NOTE


def test_clip_tool_result_read_around_keeps_encoding_caution_when_fallback_truncates(monkeypatch):
    """read_around の結果は fail-open の先頭切り詰めに落ちる——encoding_caution を text より前に置くので印が生き残る。"""
    from sherpa import corpus_docs
    caution = corpus_docs._ENCODING_CAUTION["partial"]
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "400")
    result = {"doc_id": "memo.txt", "encoding_caution": caution, "text": "x" * 5000}
    clipped, was_clipped = M._clip_tool_result(result, name="read_around", args={})
    assert was_clipped is True and _final_bytes(clipped) <= 400
    assert f'"encoding_caution": "{caution}"' in clipped["text"]


def test_clip_tool_result_under_budget_is_byte_identical(monkeypatch):
    monkeypatch.setenv("SHERPA_MCP_TOOL_BUDGET_BYTES", "65536")
    result = {"hits": [{"doc_id": "a.md", "line": 1, "text": "ok"}]}
    clipped, was_clipped = M._clip_tool_result(result)
    assert was_clipped is False and clipped is result


# ===== COD-16: 項目ごとの未確認（item → coverage.jsonl）=====

_ITEM_TOOLS = ("ripgrep_search", "es_search", "read_doc", "read_around", "file_head", "graph_neighbors",
               "graph_resolve", "graph_impact")


def test_item_param_present_optional_on_all_six_tools():
    byname = {t["name"]: t for t in _rpc("tools/list")["result"]["tools"]}
    for name in _ITEM_TOOLS:
        schema = byname[name]["inputSchema"]
        assert "item" in schema["properties"] and "item" not in schema.get("required", []), name
        assert "item" in byname[name]["description"], name


def test_item_coverage_recorded_for_hit_and_no_hits_outcomes_in_shared_ledger_dir(tmp_path, monkeypatch):
    """同じ台帳 dir（子 worker と共有）に呼出ごとに積み上がる。item なしは書かれない。"""
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    assert not _call("ripgrep_search", {"query": "TAX"})["result"]["isError"]
    assert not (directory / "coverage.jsonl").exists()
    assert not _call("ripgrep_search", {"query": "TAX-RATE", "item": "a"})["result"]["isError"]
    assert IL.load_coverage(directory)["a"] == ("hit",)
    first = json.loads((directory / "coverage.jsonl").read_text(encoding="utf-8").splitlines()[0])
    assert first["tool"] == "ripgrep_search" and set(first) == {"item", "tool", "outcome", "ts"}
    assert not _call("ripgrep_search", {"query": "ZZZ_NO_SUCH_TOKEN_XYZ", "item": "a"})["result"]["isError"]
    assert IL.load_coverage(directory) == {"a": ("hit", "no_hits")}


@pytest.mark.parametrize("case", ["unsafe_item_id", "ledger_dir_unset"])
def test_item_coverage_not_recorded_and_tool_call_unaffected(tmp_path, monkeypatch, case):
    """不正な item id・台帳 dir 未設定（plain 含む）は fail-open/fail-closed でツール呼出を壊さず何も書かない。"""
    directory = tmp_path / "investigation"
    if case == "ledger_dir_unset":
        monkeypatch.delenv("SHERPA_MCP_LEDGER_DIR", raising=False)
        item = "c"
    else:
        monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
        item = "../outside"
    assert not _call("ripgrep_search", {"query": "TAX-RATE", "item": item})["result"]["isError"]
    assert not (directory / "coverage.jsonl").exists()


def test_item_coverage_read_doc_outcomes(tmp_path, monkeypatch):
    """読めない doc は unreadable。引数誤り（range 外）は unreadable と混同せず記録しない。"""
    directory = tmp_path / "investigation"
    monkeypatch.setenv("SHERPA_MCP_LEDGER_DIR", str(directory))
    assert _call("read_doc", {"doc_id": "居ない.md", "item": "d"})["result"]["isError"]
    assert IL.load_coverage(directory) == {"d": ("unreadable",)}
    doc_id = _first_tax_rate_doc_id()
    resp = _call("read_doc", {"doc_id": doc_id, "start_line": 999999, "item": "g"})
    assert resp["result"]["isError"] and "range 外" in _body(resp)["error"]
    assert "g" not in IL.load_coverage(directory)


@pytest.mark.parametrize("name,result,is_error,expected", [
    ("ripgrep_search", {"hits": []}, False, "no_hits"),
    ("ripgrep_search", {"hits": [{"doc_id": "a", "text": "x"}], "truncated": True}, False, "limit"),
    ("ripgrep_search", {"hits": [{"doc_id": "a", "text": "x", "text_truncated": True}]}, False, "truncated"),
    ("ripgrep_search", {"hits": [{"doc_id": "a", "text": "x"}]}, False, "hit"),
    ("es_search", {"hits": []}, False, "no_hits"),
    ("read_doc", {"text": "x", "end_line": 1}, False, "hit"),
    ("read_doc", {"text": "x", "end_line": 1, "text_truncated": True}, False, "truncated"),
    ("read_doc", {"error": "読めない"}, True, "unreadable"),
    ("read_around", {"error": "読めない"}, True, "unreadable"),
    ("file_head", {"error": "読めない"}, True, "unreadable"),
    ("graph_neighbors", {"neighbors": []}, False, "no_hits"),
    ("graph_neighbors", {"neighbors": [{"name": "X"}], "truncated": True, "count": 5}, False, "limit"),
    ("graph_neighbors", {"neighbors": [{"name": "X"}]}, False, "hit"),
    ("graph_neighbors", {"neighbors": [], "error_code": "graph_unavailable"}, False, "error"),
    ("graph_impact", {"impact": [], "coverage": {"complete": True, "limits": [], "omitted": 0}}, False, "no_hits"),
    ("graph_impact", {"impact": [], "coverage": {"complete": False, "limits": [{"kind": "depth"}], "omitted": None}},
     False, "limit"),
    ("graph_impact", {"impact": [{"name": "X"}], "coverage": {"complete": True, "limits": [], "omitted": 0}}, False, "hit"),
    ("graph_impact", {"impact": [], "error_code": "graph_unavailable"}, False, "error"),
    ("graph_resolve", {"candidates": [{"name": "X"}], "truncated": True}, False, "limit"),
    ("graph_resolve", {"candidates": [{"name": "X"}]}, False, "hit"),
    # es_search の degrade_reason／truncated／truncated_docs は 0 件判定より前に見る
    ("es_search", {"hits": [], "degrade_reason": "es_unavailable"}, False, "error"),
    ("es_search", {"hits": [{"doc_id": "a", "text": "x"}], "degrade_reason": "es_query_failed"}, False, "error"),
    ("es_search", {"hits": [], "degrade_reason": "es_query_rejected"}, False, "error"),
    ("es_search", {"hits": [], "truncated": True}, False, "limit"),
    ("ripgrep_search", {"hits": [], "truncated_docs": ["b.md"]}, False, "truncated"),
    ("ripgrep_search", {"hits": [{"doc_id": "a", "text": "x"}], "truncated_docs": ["b.md"]}, False, "truncated"),
    # read_doc/read_around の引数誤りは「読めない」にせず記録しない
    ("read_doc", {"error": "range 外です", "error_code": agentic_search._READ_INVALID_ARGS_ERROR_CODE}, True, None),
    ("read_around", {"error": "line/window は整数で", "error_code": agentic_search._READ_INVALID_ARGS_ERROR_CODE},
     True, None),
])
def test_coverage_outcome_classification(name, result, is_error, expected):
    assert M._coverage_outcome(name, result, is_error) == expected
