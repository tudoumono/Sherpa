"""優先・除外の見直し（出口の一括の濾過・上限の前の除外・派生物の deny・出典の注記）の試験。名前は架空。"""
from __future__ import annotations

import copy
import json

import pytest

from sherpa import path_rules as PR

WORLD = "prrv3"


@pytest.fixture(autouse=True)
def _env(monkeypatch):
    monkeypatch.delenv("SHERPA_MCP_WORLD", raising=False)
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "ZZold", "kind": "exclude", "outside_scope": False},
                                                       {"path": "ZZsecret/x.docx", "kind": "exclude", "outside_scope": False, "target_type": "file"}]}))


# ---- 出口の一括の濾過（構造で決める・本文は消さない） ----

def test_filter_drops_elements_whose_path_field_hits_an_excluded_place():
    res = {"count": 4, "items": [
        {"name": "a", "path": "ZZold/a.md"},
        {"name": "b", "rag_path": "ZZsecret/x.docx.rag.md"},             # 派生物を指す欄の派生名
        {"name": "c", "cid": f"module:{WORLD}:ZZold/m.cbl#Prog"},          # グラフの識別子の中のパス
        {"name": "d", "doc_id": "ZZnew/d.md"}],
        "omitted": 5, "overflow_count": 2,
        "evidence": {"sources": [{"doc_id": "ZZold/q.md"}, {"doc_id": "ZZnew/ok.md"}], "sources_overflow_count": 3},
        "edges": [{"from_def": {"file": "ZZold/f.cbl"}}, {"from_def": {"file": "ZZnew/f.cbl"}}]}
    out = PR.filter_output(res, WORLD)
    assert [i["name"] for i in out["items"]] == ["d"]
    assert out["count"] is None                       # 直前の list が複数縮んだ＝数え直せない件数は null
    assert out["omitted"] is None and out["overflow_count"] is None
    assert out["evidence"]["sources"] == [{"doc_id": "ZZnew/ok.md"}] and out["evidence"]["sources_overflow_count"] is None
    assert out["edges"] == [{"from_def": {"file": "ZZnew/f.cbl"}}]
    assert PR.filter_output({"paths": ["ZZold/a.md", "ZZnew/z.md"], "total": 2}, WORLD) == {"paths": ["ZZnew/z.md"], "total": 1}
    assert PR.filter_output({"hits": [{"path": "ZZold/a.md"}, {"path": "ZZnew/b.md"}], "count": 2}, WORLD) == {
        "hits": [{"path": "ZZnew/b.md"}], "count": 1}


def test_filter_never_removes_body_text_or_guesses_from_appearance():
    """本文の文字列は一切消さない（見える資料の本文が除外のパスと一致しても、句読点・空白・長さで判定しない）。"""
    res = {"hits": [
        {"doc_id": "ZZnew/a.md", "text": "ZZold/a.md"},                        # 本文が除外のパスと完全一致
        {"doc_id": "ZZnew/b.md", "text": "ZZold/b.md を参照してください"},
        {"doc_id": "ZZnew/c.md", "text": "詳細は ZZold/c.md。以上"},
        {"doc_id": "ZZnew/d.md", "text": "line1\nZZold/d.md\nline3", "snippet": "ZZold", "quote": "ZZold/d.md"}],
        "answer": "ZZold/x.md という資料は、除外されています。", "query": "ZZold", "note": "ZZold/n.md"}
    assert PR.filter_output(res, WORLD) == res


def test_filter_without_rules_returns_the_same_object(monkeypatch):
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({}))
    res = {"items": [{"zz_path": "ZZold/a.md"}], "omitted": 3}
    snapshot = copy.deepcopy(res)
    assert PR.filter_output(res, WORLD) is res and res == snapshot
    assert PR.filter_output(res, "other-world") is res


def test_filter_leaves_a_clean_result_untouched():
    res = {"hits": [{"doc_id": "ZZnew/a.md"}], "count": 1, "omitted": 0}
    out = PR.filter_output(res, WORLD)
    assert out is res


def test_run_tool_graph_run_and_mcp_boundaries_apply_the_filter(monkeypatch):
    """境界（run_tool・graph_tools.run・MCP の応答・チャットの確定）が、資料を指す欄の漏れを最後に落とす。"""
    from sherpa import chat_service, graph_tools
    from sherpa.parts.read import tools
    leak = {"hits": [{"path": "ZZold/a.md"}, {"path": "ZZnew/ok.md"}], "count": 2}
    assert tools.run_tool.__wrapped__ is not None                           # run_tool はこの網で包まれている
    wrapped = PR.filtered_tool(lambda *a, **k: (copy.deepcopy(leak), {"ZZold/a.md", "ZZnew/ok.md"},
                                                [{"doc_id": "ZZold/a.md"}], [{"path": "ZZold/z"}]))
    res, docs, cites, cards = wrapped("ripgrep_search", {}, WORLD, None)
    assert res["hits"] == [{"path": "ZZnew/ok.md"}] and res["count"] == 1
    assert docs == {"ZZnew/ok.md"} and cites == [] and cards == []
    monkeypatch.setattr(graph_tools, "_run", lambda *a, **k: copy.deepcopy(leak))
    assert graph_tools.run("graph_resolve", {}, WORLD, None)["hits"] == [{"path": "ZZnew/ok.md"}]
    env = {"scope": {"world": WORLD}, "data": {"items": [{"path": "ZZold/a.md"}]}, "sources": [{"doc_id": "ZZold/a.md"}],
           "headline": "x"}
    out = chat_service._finalize(env, {"lens": "qa", "reason": "r"})
    assert out["data"]["items"] == [] and out["sources"] == []
    from sherpa import mcp_server
    monkeypatch.setenv("SHERPA_MCP_WORLD", WORLD)
    monkeypatch.setattr(mcp_server.tool_dispatch, "run_tool",
                        lambda *a, **k: (copy.deepcopy(leak), set(), [], []))
    r = mcp_server._handle_tool_call(1, {"name": "ripgrep_search", "arguments": {"query": "x"}})
    text = json.dumps(r, ensure_ascii=False)
    assert "ZZold" not in text and "ZZnew/ok.md" in text


# ---- 簡易チャットの出典の注記 ----

def test_simple_chat_sources_keep_the_outside_scope_note(monkeypatch):
    from sherpa import simple_chat
    monkeypatch.setenv(PR.ENV_KEY, json.dumps({WORLD: [{"path": "ZZcommon", "kind": "priority", "outside_scope": True}]}))
    monkeypatch.setattr(simple_chat, "verify_doc_exists", lambda *a, **k: True)
    state = simple_chat.LoopState()
    state.touched_docs = ["ZZcommon/r.md", "ZZsysB/n.md"]
    got = simple_chat.verify_sources(state, WORLD, ["ZZsysB"])
    assert got == [{"doc_id": "ZZcommon/r.md", "note": PR.NOTE_OUTSIDE_SCOPE}, {"doc_id": "ZZsysB/n.md"}]


# ---- Codex のサンドボックス: 派生物の全部 ----

def test_sandbox_denies_every_derived_artifact_of_an_excluded_file(tmp_path):
    from sherpa.providers.codex import sandbox
    kb, md, rag = tmp_path / "kb", tmp_path / "md", tmp_path / "rag"
    for d in (kb / "A", md / "A", rag / "A"):
        d.mkdir(parents=True)
    (kb / "A" / "x.docx").write_text("x")
    (kb / "A" / "xy.docx").write_text("keep")                 # 名前の接頭が同じだけの別ファイルは対象外
    (md / "A" / "x.docx.md").write_text("x")
    (md / "A" / "x.docx.md.meta.json").write_text("{}")
    (rag / "A" / "x.docx.rag.md").write_text("x")
    (rag / "A" / "x.docx.rag_chunks.jsonl").write_text("{}")
    (rag / "A" / "x.docx.assets").mkdir()
    (rag / "A" / "x.docx.assets" / "p1.png").write_text("png")
    rules = PR.Rules(PR.validate_entries([{"path": "A/x.docx", "kind": "exclude", "target_type": "file"}]))
    deny = sandbox._exclude_deny_entries([str(kb), str(md), str(rag)], rules,
                                         derived_roots=[str(md.resolve()), str(rag.resolve())])
    names = {p.replace(str(tmp_path.resolve()) + "/", "") for p in deny}
    assert names == {"kb/A/x.docx", "md/A/x.docx.md", "md/A/x.docx.md.meta.json", "rag/A/x.docx.rag.md",
                     "rag/A/x.docx.rag_chunks.jsonl", "rag/A/x.docx.assets"}


def test_graph_tools_edge_evidence_drops_stored_overflow_when_rules_exist():
    from sherpa import graph_tools
    raw = {"type": "INVOKES", "from": "a", "to": "b", "doc": "ZZnew/a.cbl", "line": 1, "via": "call", "sources_overflow": 4,
           "sources": json.dumps([{"via": "call", "doc_id": "ZZnew/a.cbl", "file": "ZZnew/a.cbl"},
                                  {"via": "call", "doc_id": "ZZold/b.cbl", "file": "ZZold/b.cbl"}])}
    ev = graph_tools._edge_evidence([raw], ["c0", "c1"], None, 3)
    assert ev[0]["sources_overflow_count"] == 4                                   # 設定が無ければ従来どおり
    token = graph_tools._RULES.set(PR.rules_for(WORLD))
    try:
        ev = graph_tools._edge_evidence([raw], ["c0", "c1"], None, 3)
    finally:
        graph_tools._RULES.reset(token)
    assert [x["doc_id"] for x in ev[0]["sources"]] == ["ZZnew/a.cbl"] and ev[0]["sources_overflow_count"] == 0


def test_sanitized_share_keeps_the_outside_scope_note():
    from sherpa.store import shares
    ans = {"sources": [{"doc_id": "ZZcommon/r.md", "note": PR.NOTE_OUTSIDE_SCOPE, "secret": "x"}, {"doc_id": "ZZnew/a.md"}],
           "scope": {"world": WORLD}}
    out = shares._safe_share_answer(ans)
    assert out["sources"][0]["note"] == PR.NOTE_OUTSIDE_SCOPE and "secret" not in out["sources"][0]
    assert "note" not in out["sources"][1]
