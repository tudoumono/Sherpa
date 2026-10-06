"""道具の呼び出しの記録（プロセスごとのファイル → ターンの終わりに 1 本・親か子か・サーバーのログ）の契約。"""
from __future__ import annotations

import json
import os
import sys
import threading
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("SHERPA_USE_FIXTURES", "1")
os.environ.setdefault("SHERPA_DISABLE_EMBED", "1")
os.environ["SHERPA_MCP_WORLD"] = "v1"
os.environ.pop("SHERPA_MCP_SCOPE", None)

from sherpa import mcp_server as M  # noqa: E402
from sherpa import tool_call_log as T  # noqa: E402
from sherpa.providers.codex import call_log as CL  # noqa: E402

TURN = "turn-aaa"


def _row(proc, seq, tool, ts, *, turn=TURN, attempt=1, **extra):
    return {"v": 1, "turn": turn, "conv": "7", "attempt": attempt, "proc": proc, "seq": seq,
            "call_id": f"{proc}:{seq}", "ts": ts, "tool": tool, "status": "ok", "ms": 1, **extra}


def _write(directory: Path, name: str, rows, raw_tail: str = "") -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / name).write_text("".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows) + raw_tail,
                                  encoding="utf-8")


def test_parent_and_child_write_at_once_without_mixing_and_merge_in_time_order(tmp_path):
    writers = [T.CallLogWriter(tmp_path, pid=100 + i) for i in range(3)]

    def _go(w):
        for _ in range(200):
            seq = w.next_seq()
            assert w.append(_row(w.pid, seq, "ripgrep_search", 1000 + seq, args={"query": "税率" * 50}))

    threads = [threading.Thread(target=_go, args=(w,)) for w in writers]
    [t.start() for t in threads]
    [t.join() for t in threads]
    merged = T.merge_call_logs(tmp_path, TURN)
    assert len(merged.rows) == 600 and merged.missing == 0
    assert [(r["ts"], r["proc"], r["seq"]) for r in merged.rows] == sorted(
        (r["ts"], r["proc"], r["seq"]) for r in merged.rows)


def test_merge_ignores_other_turns_dedups_and_counts_what_is_lost(tmp_path):
    _write(tmp_path, "calls-1-1.jsonl",
           [_row(1, 1, "read_doc", 1.0), _row(1, 1, "read_doc", 1.0), _row(1, 2, "read_doc", 2.0),
            _row(1, 4, "read_doc", 4.0)],
           raw_tail='{"v": 1, "turn": "turn-aaa", "proc": 1, "seq": 3, "tool": "re')  # 途中で切れた行（通し番号 3）
    _write(tmp_path, "calls-9-1.jsonl", [_row(9, 1, "read_doc", 2.0, turn="other")])
    (tmp_path / "calls-2-1.jsonl").write_bytes(b"\xff\xfe broken\n")
    (tmp_path / "calls-3-1.jsonl").symlink_to(tmp_path / "nowhere")
    merged = T.merge_call_logs(tmp_path, TURN)
    assert [r["call_id"] for r in merged.rows] == ["1:1", "1:2", "1:4"]
    assert merged.foreign_rows == 1 and merged.duplicate_rows == 1
    assert merged.broken_lines == 2 and merged.unreadable_files == 1
    assert merged.seq_gaps == 0  # 通し番号 3 は壊れた行から拾えるので抜けと二重に数えない
    assert merged.missing == 3


def test_parent_is_the_process_whose_calls_match_the_parent_events(tmp_path):
    _write(tmp_path, "calls-1-1.jsonl", [_row(1, 1, "ripgrep_search", 1.0), _row(1, 2, "read_doc", 3.0)])
    _write(tmp_path, "calls-2-1.jsonl", [_row(2, 1, "graph_impact", 2.0)])
    merged = T.merge_call_logs(tmp_path, TURN)
    T.assign_roles(merged, {1: ["ripgrep_search", "read_doc"]}, parent_record_reliable=True)
    assert {r["proc"]: r["role"] for r in merged.rows} == {1: "parent", 2: "child"}

    # 同じ並びの子がいて決められないときは「子」と決めつけず判定不能・合う者がいなければ全部判定不能
    _write(tmp_path, "calls-3-1.jsonl", [_row(3, 1, "ripgrep_search", 4.0), _row(3, 2, "read_doc", 5.0)])
    twin = T.merge_call_logs(tmp_path, TURN)
    T.assign_roles(twin, {1: ["ripgrep_search", "read_doc"]}, parent_record_reliable=True)
    assert {r["proc"]: r["role"] for r in twin.rows} == {1: "undetermined", 2: "child", 3: "undetermined"}
    T.assign_roles(twin, {1: ["glob_search"]}, parent_record_reliable=True)
    assert {r["role"] for r in twin.rows} == {"undetermined"}
    # 親の記録を信用できないとき、親の呼び出しが 0 回に見えても全部を子としない
    T.assign_roles(twin, {}, parent_record_reliable=False)
    assert {r["role"] for r in twin.rows} == {"undetermined"}


def test_mcp_entry_records_one_line_per_call_and_keeps_response_and_hides_sensitive_names(tmp_path, monkeypatch):
    monkeypatch.setenv(T.ENV_DIR, str(tmp_path))
    monkeypatch.setenv("SHERPA_MCP_TURN_ID", TURN)
    monkeypatch.setenv("SHERPA_MCP_CONVERSATION_ID", "7")
    monkeypatch.setenv("SHERPA_MCP_ATTEMPT_NO", "2")
    monkeypatch.setattr(M, "_call_log_writer", None)
    M._seen_tool_calls.clear()
    hits = [{"doc_id": "a.md", "line": 3, "text": "x"}, {"doc_id": "server.pem", "line": 1, "text": "y"}]
    monkeypatch.setattr(M.tool_dispatch, "run_tool",
                        lambda *a, **kw: ({"hits": hits, "truncated": False}, set(), [], []))
    resp = M.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                     "params": {"name": "ripgrep_search", "arguments": {"query": "TAX"}}})
    assert json.loads(resp["result"]["content"][0]["text"])["hits"] == hits  # 応答は変わらない
    merged = T.merge_call_logs(tmp_path, TURN)
    (row,) = merged.rows
    assert (row["tool"], row["attempt"], row["conv"], row["status"], row["count"]) == (
        "ripgrep_search", 2, "7", "ok", 2)
    assert row["call_id"] == f"{os.getpid()}:1" and row["args"] == {"query": "TAX"}
    assert row["docs"] == [{"doc": "a.md", "range": "3"}] and row["docs_hidden"] == 1
    assert "server.pem" not in (next(tmp_path.glob("calls-*.jsonl"))).read_text(encoding="utf-8")


def test_server_log_has_one_key_value_line_per_call_without_sensitive_names_and_diag_masks_it():
    sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "scripts"))
    import collect_diagnostics as D

    row = _row(5, 1, "read_doc", 1.0, role="parent", count=1,
               args={"query": "請求 締め", "range": "シート1!A1:C9", "doc": "顧客/請求書.xlsx"},
               docs=[{"doc": "顧客/請求書.xlsx", "range": "10-20"}, {"doc": "server.pem"}])
    st = SimpleNamespace(turn_uid=TURN)
    line = CL._call_line(st, SimpleNamespace(conversation_id=7), row)
    assert line.startswith("tool_call conv=7 turn=turn-aaa attempt=1 call=5:1 role=parent tool=read_doc")
    assert 'query="請求 締め"' in line and 'range="シート1!A1:C9"' in line
    assert line.endswith("doc=顧客/請求書.xlsx@10-20") and "server.pem" not in line
    masked = D._mask_body(line)
    for secret in ("請求", "シート1", "顧客"):
        assert secret not in masked
    assert "query=<masked>" in masked and "range=<masked>" in masked




def test_route_found_docs_and_totals_from_merged_calls_without_sensitive_names_or_unsearched_listing(tmp_path):
    rows = [
        _row(1, 1, "ripgrep_search", 1.0, count=3, ms=5, role="parent", args={"query": "税率"},
             docs=[{"doc": "a.md", "range": "3"}, {"doc": "b.md", "range": "7"}, {"doc": "c.md", "range": "1"}],
             docs_hidden=1),
        _row(1, 2, "es_search", 2.0, count=1, ms=7, role="parent", args={"query": "端数"}, docs=[{"doc": "a.md", "range": "9"}]),
        _row(1, 3, "read_doc", 3.0, role="parent", args={"doc": "b.md", "range": "1 行目から"}, docs=[{"doc": "b.md", "range": "1-40"}]),
        _row(1, 4, "list_docs", 4.0, count=9, role="parent", docs=[{"doc": "z.md"}]),
        _row(2, 1, "ripgrep_search", 5.0, count=0, status="error", role="child", error_kind="timeout"),
    ]
    _write(tmp_path, "calls-1-1.jsonl", rows[:4])
    _write(tmp_path, "calls-2-1.jsonl", rows[4:])
    merged = T.merge_call_logs(tmp_path, TURN)

    found, hidden, omitted = T.found_docs(merged, T.opened_docs(merged))
    assert [e["doc"] for e in found] == ["a.md", "b.md", "c.md"]  # 検索語が多い順→ヒット数順→最初に見つかった順。一覧の z.md は対象外
    assert found[0]["queries"] == ["税率", "端数"] and found[0]["lines"] == [3, 9]
    assert [e["opened"] for e in found] == [False, True, False] and hidden == 1 and omitted == 0

    route, route_omitted = T.route_rows(merged)
    assert len(route) == 5 and route_omitted == 0 and route[0]["query"] == "税率" and route[0]["docs_hidden"] == 1
    assert route[2]["docs"] == [{"doc": "b.md", "range": "1-40"}]

    totals = {(t["tool"], t["role"]): t for t in T.tool_totals(merged)}
    assert totals[("ripgrep_search", "parent")]["found"] == 3 and totals[("ripgrep_search", "parent")]["errors"] == 0
    assert totals[("ripgrep_search", "child")]["errors"] == 1 and totals[("es_search", "parent")]["ms"] == 7

    # 回答の found_docs: 開かれた資料と「参照した資料」に挙がった資料は入らない
    st = SimpleNamespace(call_log=merged, _mcp_read_docs=[])
    assert CL.found_docs_for_answer(st, [[["c.md"], ["x.md"]]]) == ([{"path": "a.md"}], 1, 0)


def test_referenced_docs_merge_ranges_and_mark_unopened_office_only():
    assert CL.merge_line_ranges(["12-40", "30-50", "51", "5", "x", "9-3"]) == [(5, 5), (12, 51)]
    rows = [
        {"tool": "read_doc", "status": "ok", "docs": [{"doc": "a.md", "range": "1-10"}, {"doc": "b.xlsx", "range": "?"}]},
        {"tool": "read_around", "status": "ok", "docs": [{"doc": "a.md", "range": "11-20"}]},
        {"tool": "read_doc", "status": "error", "docs": [{"doc": "z.md", "range": "1-2"}]},
    ]
    merged = SimpleNamespace(rows=rows, missing=0)
    st = SimpleNamespace(call_log=merged, _mcp_read_docs=["c.docx"])
    out, more = CL.referenced_docs_for_answer(st, ["a.md", "b.xlsx", "c.docx", "d.pdf", "e.md", "z.md"])
    by = {r["path"]: r for r in out}
    assert more == 0 and by["a.md"]["ranges"] == [[1, 20]] and not by["a.md"]["unopened"]
    assert not by["b.xlsx"]["unopened"] and not by["c.docx"]["unopened"]  # 開いた記録がある
    assert by["d.pdf"]["unopened"] is True and by["e.md"]["unopened"] is False and by["z.md"]["ranges"] == []
    # 記録が欠けているときは断定しない
    st2 = SimpleNamespace(call_log=SimpleNamespace(rows=rows, missing=1), _mcp_read_docs=[])
    assert CL.referenced_docs_for_answer(st2, ["d.pdf"])[0][0]["unopened"] is False
