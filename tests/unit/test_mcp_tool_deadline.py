"""MCP の道具 1 回ごとの期限（`SHERPA_MCP_TOOL_DEADLINE_SEC`）の単体テスト。

資料フォルダは本物の一時ディレクトリ。遅さは「行数の多いファイルを多数」で作り、期限を短くして再現する
（1 ファイルずつ眠らせない）。外部境界の Neo4j だけ fake session に差し替える。
"""
from __future__ import annotations

import io
import json
import threading
import time

import neo4j as neo4j_mod
import pytest

from sherpa import grep_tool
from sherpa import lens_service as ls
from sherpa import mcp_server as M
from sherpa import tool_dispatch
from sherpa.ingest import world_neo4j

_BIG_FILES = 30            # 全部を走査すると 10 秒以上かかる量（1 ファイル約 0.35 秒）
_BIG_LINES = 1_000_000
_DEADLINE_SEC = 0.5
_WALL_LIMIT_SEC = 6.0      # 期限が効いていれば 1 秒前後で返る。効いていない旧動作はここを超える


@pytest.fixture(autouse=True)
def _reset_duplicate_call_cache():
    M._seen_tool_calls.clear()
    yield
    M._seen_tool_calls.clear()


@pytest.fixture
def slow_world(monkeypatch, tmp_path):
    """先頭に小さな一致ファイル 1 つ、続けて行数の多い一致ファイル多数を持つ資料フォルダ。"""
    root = tmp_path / "world"
    root.mkdir()
    (root / "a_small.txt").write_text("NEEDLE\n", encoding="utf-8")
    body = "x\n" * _BIG_LINES + "NEEDLE\n"
    for i in range(_BIG_FILES):
        (root / f"b_big_{i:02d}.txt").write_text(body, encoding="utf-8")
    monkeypatch.setenv("SHERPA_MCP_WORLD", "v1")
    monkeypatch.setenv("SHERPA_MCP_WORLD_ROOT", str(root))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    monkeypatch.delenv("SHERPA_MCP_SCOPE", raising=False)
    monkeypatch.delenv("SHERPA_MCP_LAYER", raising=False)
    monkeypatch.delenv("SHERPA_MCP_TOOLSET", raising=False)
    monkeypatch.setenv("SHERPA_MCP_TOOL_DEADLINE_SEC", str(_DEADLINE_SEC))
    return root


def _call_line(rid, name, arguments):
    return json.dumps({"jsonrpc": "2.0", "id": rid, "method": "tools/call",
                       "params": {"name": name, "arguments": arguments}})


def test_serve_answers_both_calls_when_search_exceeds_deadline(slow_world):
    """期限を超える ripgrep_search の後ろの呼び出しまで、上限内に両方へ応答が返る。"""
    lines = [_call_line(1, "ripgrep_search", {"query": "NEEDLE"}),
             _call_line(2, "read_around", {"doc_id": "a_small.txt", "line": 1})]
    out = io.StringIO()
    t = threading.Thread(target=M.serve, kwargs={"stdin": io.StringIO("\n".join(lines) + "\n"), "stdout": out},
                         daemon=True)
    t0 = time.monotonic()
    t.start()
    t.join(_WALL_LIMIT_SEC)
    assert not t.is_alive(), "期限が効かず、1 件目の検索が上限内に返らない"
    assert time.monotonic() - t0 < _WALL_LIMIT_SEC

    responses = {r["id"]: r for r in (json.loads(x) for x in out.getvalue().splitlines() if x.strip())}
    assert set(responses) == {1, 2}
    first = json.loads(responses[1]["result"]["content"][0]["text"])
    assert first["deadline_exceeded"] is True and first["truncated"] is True
    assert "path_prefix" in first["notice"] and "es_search" in first["notice"]
    assert "next_offset" not in first
    assert responses[1]["result"]["isError"] is False
    second = json.loads(responses[2]["result"]["content"][0]["text"])
    assert responses[2]["result"]["isError"] is False
    assert "error" not in second and "deadline_exceeded" not in second and "NEEDLE" in second["text"]


def test_ripgrep_search_returns_hits_found_before_deadline(slow_world):
    """期限切れでも、それまでに見つかったヒットを返す（途中の結果を捨てない）。"""
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "NEEDLE"}}})
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["deadline_exceeded"] is True
    docs = [h["doc_id"] for h in body["hits"]]
    assert docs and docs[0] == "a_small.txt"
    assert len(docs) < _BIG_FILES + 1


def test_grep_search_default_still_raises_on_deadline(slow_world):
    """既定の呼び出しは従来どおり、期限切れで例外（途中の結果は返さない）。"""
    with pytest.raises(grep_tool.GrepDeadlineExceeded):
        grep_tool.grep_search("NEEDLE", "v1", deadline=time.monotonic() + _DEADLINE_SEC)


def test_tool_deadline_exceeded_becomes_tool_error_not_rpc_error(monkeypatch):
    """ripgrep_search 以外で期限の例外が上がっても、isError の通常の道具の結果にする。"""
    def _boom(*a, **kw):
        raise grep_tool.GrepDeadlineExceeded("x")

    monkeypatch.setattr(M.tool_dispatch, "run_tool", _boom)
    sidecar = []
    monkeypatch.setattr(M, "_sidecar_append", sidecar.append)
    resp = M.handle({"jsonrpc": "2.0", "id": 7, "method": "tools/call",
                     "params": {"name": "list_docs", "arguments": {}}})
    assert "error" not in resp
    assert resp["result"]["isError"] is True
    body = json.loads(resp["result"]["content"][0]["text"])
    assert body["error"] == "tool_deadline_exceeded" and body["notice"]
    assert {"kind": "limit", "field": "search_truncated"} == {k: sidecar[0][k] for k in ("kind", "field")}


@pytest.mark.parametrize("raw, expected", [
    (None, 120.0), ("", 120.0), ("abc", 120.0), ("0", 120.0), ("-5", 120.0), ("nan", 120.0), ("inf", 120.0),
    ("30", 30.0), ("0.5", 0.5), ("9999", M._TOOL_DEADLINE_MAX_SEC),
])
def test_tool_deadline_sec_parsing(monkeypatch, raw, expected):
    if raw is None:
        monkeypatch.delenv("SHERPA_MCP_TOOL_DEADLINE_SEC", raising=False)
    else:
        monkeypatch.setenv("SHERPA_MCP_TOOL_DEADLINE_SEC", raw)
    assert M._tool_deadline_sec() == expected
    assert M._tool_deadline_sec() < 300


def test_handle_tool_call_passes_deadline_to_run_tool(monkeypatch):
    seen = {}

    def _stub(name, args, world, scope_paths, **kw):
        seen.update(kw)
        return {"count": 0, "docs": []}, set(), [], []

    monkeypatch.setattr(M.tool_dispatch, "run_tool", _stub)
    monkeypatch.setenv("SHERPA_MCP_TOOL_DEADLINE_SEC", "30")
    t0 = time.monotonic()
    M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "list_docs", "arguments": {}}})
    assert t0 + 29 < seen["deadline"] < time.monotonic() + 30
    assert seen["partial_on_deadline"] is True


def test_graph_neighbors_anchor_grep_receives_deadline(slow_world, monkeypatch):
    """graph_neighbors の起点名の grep に期限が届き、期限切れは打ち切りの申告つきで返る。"""
    session_rows = [{"cid": "module:v1:a#NEEDLE", "name": "NEEDLE", "label": "Module", "status": "active",
                     "path_names": ["ROOT", "NEEDLE"], "edges": [{"type": "USES", "doc": "a_small.txt"}], "dist": 1}]

    class _Result:
        def __init__(self, rows):
            self._rows = rows

        def __iter__(self):
            return iter(type("R", (), {"data": lambda self, d=d: dict(d)})() for d in self._rows)

        def consume(self):
            pass

        def data(self):
            return [dict(r) for r in self._rows]

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def run(self, query, **params):
            return _Result(session_rows)

    class _Driver:
        def session(self):
            return _Session()

        def close(self):
            pass

    monkeypatch.setattr(neo4j_mod.GraphDatabase, "driver", lambda uri, auth: _Driver())
    monkeypatch.setattr(world_neo4j, "_env", lambda: {"uri": "bolt://x", "user": "u", "pw": "p"})
    monkeypatch.setattr(ls, "read_unresolved", lambda *a, **k: {"available": False, "items": [], "omitted": 0})

    t0 = time.monotonic()
    result, *_ = tool_dispatch.run_tool("graph_neighbors", {"name": "NEEDLE"}, "v1", None,
                                        deadline=time.monotonic() + _DEADLINE_SEC, partial_on_deadline=True)
    assert time.monotonic() - t0 < _WALL_LIMIT_SEC
    assert result["deadline_exceeded"] is True
    assert result["truncated"] is True
    assert "path_prefix" in result["notice"]
    assert any(lim["kind"] == "timeout" and lim.get("stage") == "docs" for lim in result["coverage"]["limits"])


# ===== 期限より前に走る全木走査・読み取り道具 =====

def test_tree_signature_stops_at_deadline(tmp_path):
    from sherpa import scope_infer, search_cache
    for i in range(300):
        (tmp_path / f"d{i:03d}").mkdir()
    assert isinstance(search_cache.tree_signature([tmp_path]), str)
    with pytest.raises(scope_infer.ScopeWalkDeadlineExceeded):
        search_cache.tree_signature([tmp_path], deadline=time.monotonic() - 1)


def test_grep_search_passes_deadline_to_tree_signature_before_rg(slow_world, monkeypatch):
    """rg の前に走る署名計算の木走査にも、grep の期限が届く。"""
    from sherpa import search_cache
    seen = []
    real = search_cache.tree_signature

    def _spy(dirs, deadline=None):
        seen.append(deadline)
        return real(dirs, deadline=deadline)

    monkeypatch.setattr(search_cache, "tree_signature", _spy)
    deadline = time.monotonic() + 60
    grep_tool.grep_search("a_small_marker_none", "v1", deadline=deadline)
    assert seen and all(d == deadline for d in seen)


def test_grep_search_tree_signature_walk_raises_grep_deadline(tmp_path, monkeypatch):
    """署名計算の木走査が期限を超えたら GrepDeadlineExceeded（partial 指定なら候補なし・deadline_exceeded）。"""
    root = tmp_path / "w"
    root.mkdir()
    (root / "a.txt").write_text("NEEDLE\n", encoding="utf-8")
    monkeypatch.setenv("SHERPA_MCP_WORLD", "v1")
    monkeypatch.setenv("SHERPA_MCP_WORLD_ROOT", str(root))
    monkeypatch.setenv("SHERPA_DERIVED_DIR", str(tmp_path / "derived"))
    from sherpa import search_cache
    real = search_cache.tree_signature
    monkeypatch.setattr(search_cache, "tree_signature", lambda dirs, deadline=None: real(dirs, deadline=time.monotonic() - 1))
    with pytest.raises(grep_tool.GrepDeadlineExceeded):
        grep_tool.grep_search("NEEDLE", "v1", deadline=time.monotonic() + 60)
    stats: dict = {}
    assert grep_tool.grep_search("NEEDLE", "v1", deadline=time.monotonic() + 60, stats=stats,
                                 partial_on_deadline=True) == []
    assert stats["deadline_exceeded"] is True


def test_walk_importance_fallback_walk_honors_deadline(tmp_path):
    for i in range(5):
        (tmp_path / f"f{i}.txt").write_text("x\n", encoding="utf-8")
    assert grep_tool._walk_importance("v1", tmp_path) == {}
    assert grep_tool._walk_importance("v1", tmp_path, time.monotonic() + 60) == {}
    with pytest.raises(grep_tool.GrepDeadlineExceeded):
        grep_tool._walk_importance("v1", tmp_path, time.monotonic() - 1)


def test_candidate_entries_checks_deadline_periodically(tmp_path):
    cand = {}
    for i in range(grep_tool._DEADLINE_CHECK_ENTRIES + 20):
        p = tmp_path / f"f{i:04d}.txt"
        p.write_text("x\n", encoding="utf-8")
        cand[str(p)] = {0: 1}
    args = (cand, [(tmp_path, False)], None, None)
    assert len(list(grep_tool._candidate_entries(*args))) == len(cand)
    with pytest.raises(grep_tool.GrepDeadlineExceeded):
        list(grep_tool._candidate_entries(*args, deadline=time.monotonic() - 1))


@pytest.mark.parametrize("tool, arguments", [
    ("file_head", {"doc_id": "a_small.txt"}),
    ("read_doc", {"doc_id": "a_small.txt"}),
    ("read_around", {"doc_id": "a_small.txt", "line": 1}),
    ("doc_outline", {"doc_id": "a_small.txt"}),
])
def test_read_tools_do_not_read_when_deadline_already_passed(slow_world, monkeypatch, tool, arguments):
    monkeypatch.setenv("SHERPA_MCP_TOOL_DEADLINE_SEC", "0.000001")
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": tool, "arguments": arguments}})
    assert "error" not in resp and resp["result"]["isError"] is True
    assert json.loads(resp["result"]["content"][0]["text"])["error"] == "tool_deadline_exceeded"


@pytest.mark.parametrize("tool", ["read_doc", "doc_outline", "read_around"])
def test_text_read_loops_stop_at_deadline(slow_world, monkeypatch, tool):
    """行数の多いファイルの読み取りループも、期限で打ち切られる。"""
    monkeypatch.setenv("SHERPA_MCP_TOOL_DEADLINE_SEC", "0.05")
    arguments = {"doc_id": "b_big_00.txt"}
    if tool == "read_around":
        arguments["line"] = _BIG_LINES
    t0 = time.monotonic()
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": tool, "arguments": arguments}})
    assert resp["result"]["isError"] is True
    assert json.loads(resp["result"]["content"][0]["text"])["error"] == "tool_deadline_exceeded"
    assert time.monotonic() - t0 < 2.0


def test_doc_reader_time_budget_honors_call_deadline():
    from sherpa import doc_readers
    assert doc_readers._TimeBudget().tick(every=1) is False
    with doc_readers.call_deadline(time.monotonic() - 1):
        assert doc_readers._TimeBudget().tick(every=1) is True
    assert doc_readers._TimeBudget().tick(every=1) is False
