"""つながりの確認のうち、道具のサーバーへの直接の呼び出し（`sherpa/connectivity_tools.py`）。
外部の境界（子プロセス）だけ、JSON-RPC を話す小さな偽のサーバーに差し替える。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

from sherpa import connectivity_tools as ct

BASE = list(ct.BASE_TOOLS)
SECRET_BODY = "社外秘の本文ZZ-SECRET-BODY"
SECRET_FILE = "秘密の設計書ZZ-SECRET-NAME"

_FAKE = r'''
import json, os, sys
cfg = json.load(open(os.environ["FAKE_CFG"]))
if cfg.get("mode") == "exit":
    sys.exit(3)
log = open(cfg["log"], "a")
for line in sys.stdin:
    req = json.loads(line)
    m, rid = req.get("method"), req.get("id")
    if rid is None:
        continue
    if cfg.get("mode") == "hang" and m == "tools/call":
        import time; time.sleep(60)
    if m == "initialize":
        out = {"protocolVersion": "x", "capabilities": {}, "serverInfo": {}}
    elif m == "tools/list":
        out = {"tools": [{"name": n} for n in cfg["tools"]]}
    else:
        name = req["params"]["name"]
        log.write(json.dumps({"tool": name, "args": req["params"]["arguments"]}) + "\n"); log.flush()
        spec = cfg["results"].get(name, {"body": {"ok": True}})
        out = {"content": [{"type": "text", "text": json.dumps(spec["body"], ensure_ascii=False)}],
               "isError": bool(spec.get("error"))}
    sys.stdout.write(json.dumps({"jsonrpc": "2.0", "id": rid, "result": out}) + "\n"); sys.stdout.flush()
'''


@pytest.fixture
def fake(tmp_path, monkeypatch):
    script = tmp_path / "fake_server.py"
    script.write_text(_FAKE, encoding="utf-8")
    cfg_path, log = tmp_path / "cfg.json", tmp_path / "calls.jsonl"
    cfg = {"tools": BASE + ["es_search"], "results": {}, "log": str(log), "mode": None}
    docs = [{"rel_path": f"{SECRET_FILE}.txt", "doctype": "text", "state": "ready"}]
    cfg["results"]["list_docs"] = {"body": {"count": 1, "docs": docs}}

    def save():
        cfg_path.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    save()
    monkeypatch.setattr(ct, "_SERVER_CMD", [sys.executable, str(script)])
    monkeypatch.setattr(ct, "_server_env", lambda: {**os.environ, "FAKE_CFG": str(cfg_path)})

    class F:
        def __init__(self):
            self.cfg = cfg

        def run(self, **k):
            save()
            return {r["id"]: r for r in ct.run_server_checks(**k)}

        def calls(self):
            return [json.loads(x)["tool"] for x in log.read_text().splitlines()] if log.exists() else []
    return F()


def test_all_rows_pass_and_ledger_and_ask_tools_are_never_called(fake):
    """道具のまとまりごとに 1 行。台帳・質問・ページ画像・比較の道具は、一覧にあっても呼ばない。"""
    fake.cfg["tools"] += ["ask_user", "ledger_put", "ledger_status", "pdf_page_image", "compare_documents"]
    rows = fake.run()
    assert list(rows) == [c for c, _l in ct.ROWS]
    assert rows["tools_list"]["status"] == "ok" and rows["tools_list"]["note"] == f"{len(fake.cfg['tools'])}本"
    assert rows["tools_docs"]["status"] == rows["tools_read"]["status"] == rows["tools_es"]["status"] == "ok"
    assert rows["tools_graph"]["status"] == "skipped" and rows["tools_office"]["status"] == "skipped"
    called = set(fake.calls())
    assert called and not called & {"ask_user", "ledger_put", "ledger_status", "pdf_page_image", "compare_documents"}


def test_missing_base_tool_fails_the_list_row_and_names_it(fake):
    fake.cfg["tools"].remove("read_around")
    rows = fake.run()
    assert rows["tools_list"]["status"] == "ng" and "read_around" in rows["tools_list"]["reason"]


def test_a_tool_error_fails_only_its_row(fake):
    fake.cfg["results"]["read_doc"] = {"error": True, "body": {"error": "read_failed", "hint": SECRET_BODY}}
    rows = fake.run()
    assert rows["tools_read"]["status"] == "ng" and "read_doc（read_failed）" in rows["tools_read"]["reason"]
    assert rows["tools_docs"]["status"] == "ok" and rows["tools_es"]["status"] == "ok"


def test_results_never_carry_tool_bodies_or_document_names(fake):
    fake.cfg["results"]["ripgrep_search"] = {"body": {"hits": [{"text": SECRET_BODY}]}}
    fake.cfg["results"]["doc_outline"] = {"error": True, "body": {"error": "Bad Kind!", "text": SECRET_BODY, "path": SECRET_FILE}}
    blob = json.dumps(fake.run(), ensure_ascii=False)
    assert SECRET_BODY not in blob and SECRET_FILE not in blob and "ZZ-SECRET" not in blob


def test_graph_rows_use_a_name_from_the_tools_and_skip_without_one(fake):
    fake.cfg["tools"] += ["graph_neighbors", "graph_resolve", "graph_impact"]
    fake.cfg["results"]["name_lookup"] = {"body": {"matches": [{"name": "AAA"}]}}
    fake.cfg["results"]["graph_resolve"] = {"body": {"candidates": [{"canonical_id": "c1"}]}}
    rows = fake.run()
    assert rows["tools_graph"]["status"] == "ok" and rows["tools_graph"]["note"] == "graph_neighbors・graph_resolve・graph_impact"
    fake.cfg["results"]["graph_neighbors"] = {"error": True, "body": {"error": "graph_start_not_found"}}
    assert fake.run()["tools_graph"]["status"] == "ok"   # 起点が見つからない応答は、道具が動いて答えた
    fake.cfg["results"]["graph_neighbors"] = {"error": True, "body": {"error": "neo4j_unavailable"}}
    assert fake.run()["tools_graph"]["status"] == "ng"


def test_office_row_calls_only_the_tools_that_match_listed_documents(fake):
    fake.cfg["tools"] += ["pdf_pages", "xlsx_sheets"]
    fake.cfg["results"]["list_docs"] = {"body": {"count": 2, "docs": [
        {"rel_path": "a.pdf", "state": "ready"}, {"rel_path": "b.xlsx", "state": "ready"}]}}
    rows = fake.run()
    assert rows["tools_office"]["status"] == "ok" and set(rows["tools_office"]["note"].split("・")) == {"pdf_pages", "xlsx_sheets"}
    assert "docx_paragraphs" not in fake.calls()


def test_no_documents_skips_the_read_row(fake):
    fake.cfg["results"]["list_docs"] = {"body": {"count": 0, "docs": []}}
    assert fake.run()["tools_read"]["status"] == "skipped"


def test_es_row_is_skipped_without_the_tool(fake):
    fake.cfg["tools"].remove("es_search")
    assert fake.run()["tools_es"]["status"] == "skipped"


def test_server_that_cannot_start_leaves_every_row_incomplete(fake):
    fake.cfg["mode"] = "exit"
    rows = fake.run()
    assert set(rows) == {c for c, _l in ct.ROWS} and all(r["status"] == "incomplete" for r in rows.values())


def test_hung_server_is_cut_off_and_the_child_is_killed(fake, monkeypatch):
    """応答が無い道具は全体の上限で打ち切り、子プロセスを残さない。"""
    fake.cfg["mode"] = "hang"
    monkeypatch.setattr(ct, "CALL_TIMEOUT_S", 0.5)
    pids = []
    real = subprocess.Popen

    def spy(*a, **k):
        p = real(*a, **k)
        pids.append(p.pid)
        return p
    monkeypatch.setattr(ct.subprocess, "Popen", spy)
    rows = fake.run(total_timeout_s=5)
    assert rows["tools_list"]["status"] == "ok"
    assert all(rows[c]["status"] == "incomplete" for c in ("tools_docs", "tools_read", "tools_es", "tools_graph", "tools_office"))
    (pid,) = pids
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def test_graph_row_fails_when_the_name_lookup_itself_fails_and_says_which_tool_was_not_run(fake):
    fake.cfg["tools"] += ["graph_neighbors", "graph_resolve", "graph_impact"]
    fake.cfg["results"]["name_lookup"] = {"error": True, "body": {"error": "ledger_unavailable"}}
    row = fake.run()["tools_graph"]
    assert row["status"] == "ng" and "name_lookup" in row["reason"]
    fake.cfg["results"]["name_lookup"] = {"body": {"matches": [{"name": "AAA"}]}}
    fake.cfg["results"]["graph_resolve"] = {"body": {"candidates": []}}
    row = fake.run()["tools_graph"]
    assert row["status"] == "ok" and "graph_impact は起点の候補が無く流していない" in row["note"]


def test_documents_with_the_same_name_do_not_repeat_the_same_name_lookup(fake):
    fake.cfg["tools"] += ["graph_neighbors"]
    fake.cfg["results"]["list_docs"] = {"body": {"count": 2, "docs": [
        {"rel_path": "a/README.md", "state": "ready"}, {"rel_path": "b/README.md", "state": "ready"},
        {"rel_path": "c/readme.md", "state": "ready"}]}}
    fake.cfg["results"]["name_lookup"] = {"body": {"matches": []}}
    assert fake.run()["tools_graph"]["status"] == "skipped"
    assert fake.calls().count("name_lookup") == 1   # 表記だけ違う名前も、道具のサーバーと同じ見方で 1 回にまとめる


def test_repeated_names_do_not_use_up_the_tries_for_the_start_name(fake):
    fake.cfg["tools"] += ["graph_neighbors"]
    docs = [{"rel_path": f"d{i}/README.md", "state": "ready"} for i in range(12)] + [{"rel_path": "src/AAA.txt", "state": "ready"}]
    fake.cfg["results"]["list_docs"] = {"body": {"count": len(docs), "docs": docs}}
    fake.cfg["results"]["name_lookup"] = {"body": {"matches": []}}
    fake.run()
    assert fake.calls().count("name_lookup") == 2   # README と AAA を 1 回ずつ
