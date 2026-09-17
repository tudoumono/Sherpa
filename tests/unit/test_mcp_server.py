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

        resp2 = M.handle({"jsonrpc": "2.0", "id": 72, "method": "tools/call",
                          "params": {"name": "read_doc", "arguments": {"doc_id": doc_id}}})
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
