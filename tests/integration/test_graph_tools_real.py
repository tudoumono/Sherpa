"""Codex の道具 `graph_resolve`・`graph_impact` を実 Neo4j で確かめる（要 Neo4j・`make up`）。

`fixtures/corpus/ana-s6` を `build_world` で作り、使い捨ての資料フォルダとして取り込む。受入条件（提案書 S6）:
① 同名の別ノードを混ぜない ② 言及（DOCUMENTS）を影響に数えない ③ 深さ・件数の上限で未探索が残るとき明示する
④ ソース限定（層 code）でも使え、資料の名前が漏れない。応答の形は golden（`tests/contract/goldens/graph_tools_responses.json`）で固定する
（再生成: `SHERPA_REGEN_GRAPH_TOOLS_GOLDEN=1`）。
"""
from __future__ import annotations

import json
import os
import pathlib

import pytest

from sherpa import tool_dispatch
from sherpa.ingest import world_graph, world_neo4j
from _world_registry import register_test_world

ROOT = pathlib.Path(__file__).resolve().parents[2]
FIXTURE = ROOT / "fixtures" / "corpus" / "ana-s6"
GOLDEN = ROOT / "tests" / "contract" / "goldens" / "graph_tools_responses.json"
WORLD = "ana_s6_gt_test"           # 使い捨て（テスト後に削除）


def _cid(path: str, name: str) -> str:
    return f"module:{WORLD}:{path}#{name}"


@pytest.fixture(scope="module")
def loaded():
    from neo4j import GraphDatabase
    nodes, edges, _flags = world_graph.build_world(FIXTURE, WORLD)
    # 取り込みは秘匿名のファイルを読まないが、グラフに残っていても出力側で止まることを確かめるため、秘匿名のノードと辺を足す
    bill = _cid("mention/src/Billing.java", "Billing")

    def node(path, name, label="Module"):
        cid = f"document:{WORLD}:{path}" if label == "Document" else _cid(path, name)
        return {"cid": cid, "label": label, "name": name, "top_scope": "mention", "path": path, "status": "active"}

    cred_doc = node("mention/docs/credentials.md", "credentials.md", "Document")
    cred_code = node("mention/src/credentials.py", "SECRETCFG")
    # パスに `#` を含む秘匿名のノードを経路の途中に置く（識別子からパスを切り出す実装だと判定を抜ける）: Zed → Hashed → Billing
    hashed = node("mention/src/foo#x/credentials.py", "HASHED")
    zed = node("mention/src/Zed.java", "Zed")
    # 根拠（sources）に秘匿名・範囲外の資料を含む辺: Sec2 → Tgt（5 件のうち返してよいのは 3 件）
    tgt = node("mention/src/Tgt.java", "Tgt")
    sec2 = node("mention/src/Sec2.java", "Sec2")
    nodes += [cred_doc, cred_code, hashed, zed, tgt, sec2]

    def edge(src, dst, doc, line=1, etype="INVOKES", **kw):
        return {"type": etype, "src": src["cid"], "dst": dst if isinstance(dst, str) else dst["cid"], "doc": doc,
                "line": line, "status": "active", **kw}

    def source(doc, line, **kw):
        return {"via": "call", "doc_id": doc, "file": doc, "line": line, "rule": "same_package", **kw}

    edges += [edge(zed, hashed, "mention/src/Zed.java"),
              edge(hashed, bill, "mention/src/foo#x/credentials.py"),
              edge(cred_doc, bill, "mention/docs/credentials.md", 0, "DOCUMENTS", via="mention"),
              edge(cred_code, bill, "mention/src/credentials.py"),
              edge(sec2, tgt, "mention/src/Sec2.java", 3, via="call", sources_overflow_count=2, sources=[
                  source("mention/src/Sec2.java", 3), source("mention/src/credentials.py", 9),
                  source("other/Out.java", 1),
                  source("mention/src/More.java", 5, rule="import",
                         from_def={"file": "mention/src/credentials.py", "key": "k"}),
                  source("mention/src/Last.java", 7)])]
    env = world_neo4j._env()
    world_neo4j.load_world(nodes, edges, WORLD, env["uri"], env["user"], env["pw"])
    register_test_world(WORLD)
    drv = GraphDatabase.driver(env["uri"], auth=(env["user"], env["pw"]))
    try:
        yield drv
    finally:
        with drv.session() as s:
            s.run("MATCH (n) WHERE n.world_id=$w AND (n:Entity OR n:SherpaMeta) DETACH DELETE n", w=WORLD)
        drv.close()


def _run(name, args, layer=None, scope=None):
    return tool_dispatch.run_tool(name, args, WORLD, scope, layer=layer)[0]


def _impact_names(result):
    return [i["name"] for i in result["impact"]]


def test_same_name_candidates_are_listed_separately_and_impact_does_not_mix(loaded):
    res = _run("graph_resolve", {"name": "Account"})
    cands = {c["path"]: c for c in res["candidates"]}
    assert set(cands) == {"dup/a/Account.java", "dup/b/Account.java"} and res["count"] == 2
    assert res["coverage"]["complete"] is True
    imp_a = _run("graph_impact", {"canonical_id": cands["dup/a/Account.java"]["canonical_id"]})
    imp_b = _run("graph_impact", {"canonical_id": cands["dup/b/Account.java"]["canonical_id"]})
    assert _impact_names(imp_a) == ["UseA"] and _impact_names(imp_b) == ["UseB"]
    assert imp_a["start"]["path"] == "dup/a/Account.java"


def test_mentions_are_related_documents_not_impact(loaded):
    cid = _cid("mention/src/Billing.java", "Billing")
    res = _run("graph_impact", {"canonical_id": cid})
    assert _impact_names(res) == ["Invoicer"]
    docs = res["related_documents"]
    assert [(d["path"], d["via"]) for d in docs] == [("mention/docs/spec.md", "mention")]
    assert all(i["path"] != "mention/docs/spec.md" for i in res["impact"])


def test_code_layer_uses_graph_impact_without_any_document(loaded):
    cid = _cid("mention/src/Billing.java", "Billing")
    res = _run("graph_impact", {"canonical_id": cid}, layer="code")
    assert _impact_names(res) == ["Invoicer"] and "related_documents" not in res
    assert "spec.md" not in json.dumps(res, ensure_ascii=False)
    doc = _run("graph_impact", {"canonical_id": f"document:{WORLD}:mention/docs/spec.md"}, layer="code")
    assert doc["error"] == "graph_start_not_found"
    docs_found = _run("graph_resolve", {"name": "spec", "kind": "Document"}, layer="code")
    assert docs_found["candidates"] == []


def test_docs_layer_is_rejected_like_graph_neighbors(loaded):
    for name, args in (("graph_resolve", {"name": "Account"}), ("graph_impact", {"canonical_id": "x"})):
        assert "error" in _run(name, args, layer="docs")


def test_unexplored_remainder_is_reported(loaded):
    hub = _cid("cap/Hub.java", "Hub")
    capped = _run("graph_impact", {"canonical_id": hub, "limit": 3})
    assert len(capped["impact"]) == 3 and capped["count"] == 5 and capped["truncated"] is True
    assert capped["coverage"]["complete"] is False and capped["coverage"]["omitted"] == 2
    assert {"kind": "result_cap", "stage": "impact"} in capped["coverage"]["limits"]
    link6 = _cid("chain/Link6.java", "Link6")
    shallow = _run("graph_impact", {"canonical_id": link6, "depth": 2})
    assert _impact_names(shallow) == ["Link5", "Link4"]
    assert shallow["coverage"]["depth"] == {"requested": 2, "truncated": True}
    assert {"kind": "depth", "stage": "impact"} in shallow["coverage"]["limits"]
    full = _run("graph_impact", {"canonical_id": link6, "depth": 5})
    assert _impact_names(full) == ["Link5", "Link4", "Link3", "Link2", "Link1"]
    assert full["coverage"]["complete"] is True and full["coverage"]["depth"]["truncated"] is False


def test_resolve_cap_and_filters(loaded):
    res = _run("graph_resolve", {"name": "User", "limit": 2})
    assert len(res["candidates"]) == 2
    # 上限＋1 件だけ取るので総数は分からない（count・omitted は null）
    assert res["count"] is None
    assert res["coverage"] == {"complete": False, "limits": [{"kind": "result_cap", "stage": "anchor"}], "omitted": None}
    only = _run("graph_resolve", {"path": "chain/", "kind": "Module"})
    assert only["count"] == 6 and {c["kind"] for c in only["candidates"]} == {"Module"}


def test_old_era_graph_is_rejected(loaded):
    with loaded.session() as s:
        s.run("MATCH (m:SherpaMeta {world_id:$w}) SET m.schema_era='old'", w=WORLD)
    try:
        for name, args in (("graph_resolve", {"name": "Account"}), ("graph_impact", {"canonical_id": _cid("cap/Hub.java", "Hub")})):
            res = _run(name, args)
            assert res == {"error": "graph_reingest_required", "world": WORLD, "stored_era": "old"}
    finally:
        with loaded.session() as s:
            s.run("MATCH (m:SherpaMeta {world_id:$w}) SET m.schema_era=$e", w=WORLD, e=world_neo4j.GRAPH_SCHEMA_ERA)


def test_response_shapes_match_golden(loaded):
    """正常（候補・影響＋関連文書）・上限・深さの先・入力の誤り・見つからない起点の応答の形。"""
    cases = {
        "resolve_same_name": _run("graph_resolve", {"name": "Account"}),
        "resolve_capped": _run("graph_resolve", {"name": "User", "limit": 2}),
        "impact_with_related_documents": _run("graph_impact", {"canonical_id": _cid("mention/src/Billing.java", "Billing")}),
        "impact_result_cap": _run("graph_impact", {"canonical_id": _cid("cap/Hub.java", "Hub"), "limit": 2}),
        "impact_depth_truncated": _run("graph_impact", {"canonical_id": _cid("chain/Link3.java", "Link3"), "depth": 1}),
        "impact_code_layer": _run("graph_impact", {"canonical_id": _cid("mention/src/Billing.java", "Billing")}, layer="code"),
        "impact_edge_sources_filtered": _run("graph_impact", {"canonical_id": _cid("mention/src/Tgt.java", "Tgt")}),
        "impact_unresolved": _run("graph_impact", {"canonical_id": _cid("amb/x/Helper.java", "Helper")}),
        "invalid_args": _run("graph_impact", {}),
        "start_not_found": _run("graph_impact", {"canonical_id": "module:nope:x#X"}),
    }
    text = json.dumps(cases, ensure_ascii=False, indent=1, sort_keys=True)
    if os.environ.get("SHERPA_REGEN_GRAPH_TOOLS_GOLDEN") == "1":
        GOLDEN.write_text(text + "\n", encoding="utf-8")
    assert json.loads(GOLDEN.read_text(encoding="utf-8")) == json.loads(text)


def test_sensitive_nodes_never_appear_in_any_output(loaded):
    """秘匿名のファイルに属するノード（資料・コード）は、候補・起点・関連文書・影響先のどこにも出ず、起点にすると存在しない扱い。"""
    assert _run("graph_resolve", {"name": "cred", "kind": "Document"})["candidates"] == []
    assert _run("graph_resolve", {"name": "SECRET"})["candidates"] == []
    assert _run("graph_resolve", {"path": "credentials"})["candidates"] == []
    for cid in (f"document:{WORLD}:mention/docs/credentials.md", _cid("mention/src/credentials.py", "SECRETCFG")):
        assert _run("graph_impact", {"canonical_id": cid})["error"] == "graph_start_not_found"
    for layer in (None, "code"):
        res = _run("graph_impact", {"canonical_id": _cid("mention/src/Billing.java", "Billing")}, layer=layer)
        text = json.dumps(res, ensure_ascii=False)
        assert "credentials" not in text and "SECRETCFG" not in text
        assert "HASHED" not in text and "Zed" not in text  # `#` を含むパスの秘匿ノードを経由する先も出さない
        assert _impact_names(res) == ["Invoicer"]


def test_start_outside_scope_is_rejected(loaded):
    hub = _cid("cap/Hub.java", "Hub")
    assert _run("graph_impact", {"canonical_id": hub}, scope=["mention"])["error"] == "graph_start_not_found"
    assert _run("graph_impact", {"canonical_id": hub}, scope=["cap"])["count"] == 5
    assert _run("graph_resolve", {"name": "Hub"}, scope=["mention"])["candidates"] == []


def test_impact_evidence_has_edge_sources_without_sensitive_or_out_of_scope_documents(loaded):
    tgt = _cid("mention/src/Tgt.java", "Tgt")
    res = _run("graph_impact", {"canonical_id": tgt})
    (item,) = res["impact"]
    assert item["name"] == "Sec2" and item["evidence_available"] is True
    (ev,) = item["evidence"]
    assert (ev["via"], ev["line"], ev["rule"]) == ("call", 3, "same_package")
    docs = [s["doc_id"] for s in ev["sources"]]
    # 秘匿名の資料を指す根拠と、秘匿名の定義ファイル（`from_def.file`）を持つ根拠は、根拠ごと除く
    assert docs == ["mention/src/Sec2.java", "other/Out.java", "mention/src/Last.java"]
    assert ev["sources_overflow_count"] == 2                                            # 伏せた分は足さない
    scoped = _run("graph_impact", {"canonical_id": tgt}, scope=["mention"])["impact"][0]["evidence"][0]
    assert [s["doc_id"] for s in scoped["sources"]] == ["mention/src/Sec2.java", "mention/src/Last.java"]
    assert "credentials" not in json.dumps(res, ensure_ascii=False)
    limited = _run("graph_impact", {"canonical_id": tgt, "evidence_limit": 1})["impact"][0]["evidence"][0]
    assert [s["doc_id"] for s in limited["sources"]] == ["mention/src/Sec2.java"]
    assert limited["sources_overflow_count"] == 4
    none = _run("graph_impact", {"canonical_id": tgt, "evidence_limit": 0})["impact"][0]["evidence"][0]
    assert none["sources"] == [] and none["sources_overflow_count"] == 5


def test_valid_sources_behind_rejected_ones_are_kept_after_verification(loaded, monkeypatch):
    """根拠は検証で除いた後に件数で切る: [有効, 秘匿, 範囲外・不存在, 秘匿の定義つき, 有効] の後ろの有効な根拠が、先頭の件数に押し出されて消えない。"""
    from sherpa.parts.read import tools
    tgt = _cid("mention/src/Tgt.java", "Tgt")
    ev = _run("graph_impact", {"canonical_id": tgt, "evidence_limit": 2}, scope=["mention"])["impact"][0]["evidence"][0]
    assert [s["doc_id"] for s in ev["sources"]] == ["mention/src/Sec2.java", "mention/src/Last.java"]
    monkeypatch.setattr(tools, "verify_doc_exists", lambda d, w, sp=None: d != "other/Out.java")
    res = tool_dispatch.run_tool("graph_neighbors", {"name": "Tgt"}, WORLD, None, graph_only=True)[0]
    (nb,) = [n for n in res["neighbors"] if n["name"] == "Sec2"]
    assert [s["doc_id"] for s in nb["edges"][0]["sources"]] == ["mention/src/Sec2.java", "mention/src/Last.java"]


def test_impact_returns_unresolved_for_the_start_name(loaded):
    x = _run("graph_resolve", {"name": "Helper", "path": "amb/x"})["candidates"][0]["canonical_id"]
    res = _run("graph_impact", {"canonical_id": x})
    assert res["unresolved"]["available"] is True
    assert [(u["path"], u["reason"], u["name"]) for u in res["unresolved"]["items"]] == [("amb/Main.java", "ambiguous", "Helper")]
    assert _run("graph_impact", {"canonical_id": x}, scope=["dup"]).get("error") == "graph_start_not_found"


def test_impact_unresolved_is_not_attached_when_the_start_lookup_was_cut(loaded, monkeypatch):
    from sherpa.ingest import world_neo4j as WN

    def boom(*a, **k):
        raise WN.GraphQueryOverloadError("timeout", world=WORLD)

    monkeypatch.setattr(WN, "world_impact", boom)
    res = _run("graph_impact", {"canonical_id": _cid("cap/Hub.java", "Hub")})
    assert "unresolved" not in res and res["coverage"]["complete"] is False
