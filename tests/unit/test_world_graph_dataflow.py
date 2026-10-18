"""データの流れ（ファイル経由の受け渡し・コピーブック項目の使用）の `world_graph.build_world` 統合テスト。

`fixtures/dataflow`（架空: SALESCALC が `PROD.SALES.CALC` を書き REPORTGEN が読む）を実際に `build_world()` へ通し、
`Module -FLOWS_TO(write_file)-> Dataset -FLOWS_TO(read_file)-> Module`・`DataItem -FLOWS_TO(use)-> Module` と、
`Dataset` の同一性（世代, DSN）・一時データセット・結べない DD 名の申告・世代をまたがないことを固定する。
影響たどり（`graph_impact`）が流れを `flow_candidates` に別枠で返し `impact` を変えないことは、Neo4j の代わりに
`build_world` の出力をそのまま問い合わせに答えるセッションで確かめる。実 Neo4j との一致は `tests/integration/test_impact_bfs_real.py`。
"""
from __future__ import annotations

import pathlib

import pytest

from sherpa import graph_tools
from sherpa.ingest import world_graph
from sherpa.ingest import world_neo4j as WN

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "dataflow"
WORLD_ID = "dataflow_test"


def _build(wd=WORLD_DIR):
    return world_graph.build_world(wd, WORLD_ID)


def _flows(nodes, edges):
    by = {n["cid"]: n for n in nodes}
    return {(by[e["src"]]["name"], by[e["dst"]]["name"], e["via"]) for e in edges if e["type"] == "FLOWS_TO"}


def _world(tmp_path, files: dict):
    wd = tmp_path / "world"
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return wd


def _cbl(name, select="", fd="", proc="", copy=""):
    return (f"       IDENTIFICATION DIVISION.\n       PROGRAM-ID. {name}.\n       ENVIRONMENT DIVISION.\n"
            "       INPUT-OUTPUT SECTION.\n       FILE-CONTROL.\n" + select +
            "       DATA DIVISION.\n       FILE SECTION.\n" + fd + "       WORKING-STORAGE SECTION.\n" + copy +
            "       PROCEDURE DIVISION.\n" + proc + "           STOP RUN.\n")


def _sel(file, dd):
    return f"           SELECT {file} ASSIGN TO UT-S-{dd}.\n"


def _fd(file, rec):
    return f"       FD  {file}.\n       01  {rec} PIC X(80).\n"


# ---- fixtures/dataflow ----

def test_file_handover_between_two_programs_goes_through_one_dataset_node():
    nodes, edges, _flags = _build()
    assert {("SALESCALC", "PROD.SALES.CALC", "write_file"), ("PROD.SALES.CALC", "REPORTGEN", "read_file")} <= _flows(nodes, edges)
    datasets = [n for n in nodes if n["label"] == "Dataset"]
    assert {(n["top_scope"], n["name"]) for n in datasets} == {("gen1", "PROD.SALES.CALC"), ("gen2", "PROD.SALES.CALC")}
    assert len({n["cid"] for n in datasets}) == 2                           # 世代が違えば別の節点（世代をまたがない）


def test_dataset_node_identity_path_sources_and_value():
    nodes, _edges, _flags = _build()
    ds = next(n for n in nodes if n["label"] == "Dataset" and n["top_scope"] == "gen1")
    assert ds["cid"] == f"dataset:{WORLD_ID}:gen1#PROD.SALES.CALC"
    assert ds["path"] == "gen1/NIGHTLY1.jcl"                                 # その DSN を最初に書いた JCL（rel 昇順）
    assert [(s["doc_id"], s["line"]) for s in ds["sources"]] == [("gen1/NIGHTLY1.jcl", 3), ("gen1/NIGHTLY2.jcl", 3)]
    assert ds["value"] == "gdg=true disp=NEW" and ds["analyzer"] == "jcl"
    assert ds["category"] is None or isinstance(ds["category"], str)


def test_dataset_membership_and_dd_bindings_are_not_capped_with_evidence(tmp_path):
    prog = _cbl("READPG", _sel("IN-F", "INDD"), _fd("IN-F", "IN-R"), "           OPEN INPUT IN-F.\n")
    files = {"g/READPG.cbl": prog}
    paths = [f"g/p{i:02}/J.jcl" for i in range(world_graph.EDGE_SOURCES_MAX + 2)]
    files.update({p: "//J JOB\n//S EXEC PGM=READPG\n//INDD DD DSN=DEMO.SHARED,DISP=SHR\n" for p in paths})
    nodes, edges, _flags = _build(_world(tmp_path, files))
    ds = next(n for n in nodes if n["label"] == "Dataset")
    flow = next(e for e in edges if e["type"] == "FLOWS_TO")
    assert ds["membership_paths"] == paths and flow["binding_paths"] == paths
    assert len(ds["sources"]) == world_graph.EDGE_SOURCES_MAX
    assert len(flow["sources"]) == world_graph.EDGE_SOURCES_MAX


def test_edge_carries_both_the_program_statement_and_the_dd_statement_as_sources():
    nodes, edges, _flags = _build()
    by = {n["cid"]: n for n in nodes}
    w = next(e for e in edges if e["type"] == "FLOWS_TO" and e["via"] == "write_file" and by[e["dst"]]["top_scope"] == "gen1")
    assert (w["doc"], w["line"]) == ("gen1/SALESCALC.cbl", 19)
    srcs = [(s["doc_id"], s["line"], s.get("rule"), s.get("evidence_text")) for s in w["sources"]]
    assert srcs[0][:3] == ("gen1/SALESCALC.cbl", 19, "dd_name_match")
    assert ("gen1/NIGHTLY1.jcl", 3, "dd_name_match", "DD CALCOUT DSN=PROD.SALES.CALC(+1) DISP=NEW,CATLG,DELETE") in srcs


def test_gdg_generations_land_on_the_same_node_and_the_relative_generation_is_kept_as_evidence():
    nodes, edges, _flags = _build()
    by = {n["cid"]: n for n in nodes}
    r = next(e for e in edges if e["type"] == "FLOWS_TO" and e["via"] == "read_file" and by[e["src"]]["top_scope"] == "gen1")
    assert any(s.get("evidence_text") == "DD RPTIN DSN=PROD.SALES.CALC(0) DISP=SHR" for s in r["sources"])


def test_unbound_ddname_is_reported_as_unresolved_not_dropped_silently():
    nodes, _edges, flags = _build()
    f = next(f for f in flags if f.get("kind") == "Dataset")
    assert (f["reason"], f["from"], f["name"], f["via"]) == ("unresolved", "gen1/SALESCALC.cbl", "AUDITDD", "write_file")
    sc = next(n for n in nodes if n["name"] == "SALESCALC")
    assert [(u["reason"], u["kind"], u["name"], u["via"]) for u in sc["unresolved"]] == [
        ("unresolved", "Dataset", "AUDITDD", "write_file")]
    assert "AUDITDD" in sc["unresolved_names"]


def test_use_edge_links_only_items_of_copied_copybooks():
    nodes, edges, _flags = _build()
    by = {n["cid"]: n for n in nodes}
    uses = [(by[e["src"]]["name"], by[e["dst"]]["name"], e["doc"], e["line"], e["sources"][0]["rule"]) for e in edges
            if e["type"] == "FLOWS_TO" and e["via"] == "use"]
    assert uses == [("TAX-RATE", "SALESCALC", "gen1/SALESCALC.cbl", 21, "copy_item_name")]


def test_everything_else_is_unchanged_and_new_vocabulary_is_in_the_closed_sets():
    from sherpa.ingest.analyzers import registry
    nodes, edges, _flags = _build()
    assert {n["label"] for n in nodes} <= registry.NODE_LABELS and {e["type"] for e in edges} <= registry.EDGE_TYPES
    assert {e["via"] for e in edges if e["type"] == "FLOWS_TO"} <= registry.KNOWN_VIA
    assert _build() == (nodes, edges, _flags)                                # 決定的


# ---- 場合分け（一時ディレクトリの世界）----

def test_temporary_dataset_is_the_same_only_inside_one_jcl_file(tmp_path):
    prog_w = _cbl("WRITEPG", _sel("OUT-F", "TMPOUT"), _fd("OUT-F", "OUT-R"), "           OPEN OUTPUT OUT-F.\n")
    prog_r = _cbl("READPG", _sel("IN-F", "TMPIN"), _fd("IN-F", "IN-R"), "           OPEN INPUT IN-F.\n")
    wd = _world(tmp_path, {
        "g/WRITEPG.cbl": prog_w, "g/READPG.cbl": prog_r,
        "g/ONE.jcl": "//ONE JOB\n//S1 EXEC PGM=WRITEPG\n//TMPOUT DD DSN=&&WORK,DISP=(NEW,PASS)\n"
                     "//S2 EXEC PGM=READPG\n//TMPIN DD DSN=&&WORK,DISP=(OLD,DELETE)\n",
        "g/TWO.jcl": "//TWO JOB\n//S1 EXEC PGM=READPG\n//TMPIN DD DSN=&&WORK,DISP=(OLD,DELETE)\n",
    })
    nodes, edges, _flags = _build(wd)
    temp = sorted(n["cid"] for n in nodes if n["label"] == "Dataset")
    assert temp == [f"dataset:{WORLD_ID}:g#g/ONE.jcl:&&WORK", f"dataset:{WORLD_ID}:g#g/TWO.jcl:&&WORK"]
    by = {n["cid"]: n for n in nodes}
    pairs = {(by[e["src"]]["name"], by[e["dst"]]["name"], by[e["src" if e["via"] == "read_file" else "dst"]]["path"])
             for e in edges if e["type"] == "FLOWS_TO"}
    assert ("WRITEPG", "&&WORK", "g/ONE.jcl") in pairs
    assert sum(1 for e in edges if e["type"] == "FLOWS_TO" and by[e["src"]]["name"] == "&&WORK") == 2     # READPG は ONE と TWO で別の &&WORK を読む
    one_read = [e for e in edges if e["type"] == "FLOWS_TO" and e["src"] == f"dataset:{WORLD_ID}:g#g/ONE.jcl:&&WORK"]
    assert len(one_read) == 1 and one_read[0]["dst"].startswith("module:") and "READPG" in one_read[0]["dst"]


def test_one_program_run_by_two_jobs_with_different_dsns_gets_every_dataset(tmp_path):
    prog = _cbl("READPG", _sel("IN-F", "INDD"), _fd("IN-F", "IN-R"), "           OPEN INPUT IN-F.\n")
    wd = _world(tmp_path, {
        "g/READPG.cbl": prog,
        "g/A.jcl": "//A JOB\n//S EXEC PGM=READPG\n//INDD DD DSN=PROD.ONE,DISP=SHR\n//   DD DSN=PROD.TWO,DISP=SHR\n",
        "g/B.jcl": "//B JOB\n//S EXEC PGM=READPG\n//INDD DD DSN=PROD.THREE,DISP=SHR\n",
    })
    nodes, edges, _flags = _build(wd)
    assert {(s, d) for s, d, v in _flows(nodes, edges) if v == "read_file"} == {
        ("PROD.ONE", "READPG"), ("PROD.TWO", "READPG"), ("PROD.THREE", "READPG")}


def test_i_o_program_gets_both_directions(tmp_path):
    prog = _cbl("UPDPG", _sel("MST", "MASTER"), _fd("MST", "MST-R"), "           OPEN I-O MST.\n")
    wd = _world(tmp_path, {"g/UPDPG.cbl": prog, "g/J.jcl": "//J JOB\n//S EXEC PGM=UPDPG\n//MASTER DD DSN=PROD.MASTER,DISP=OLD\n"})
    nodes, edges, _flags = _build(wd)
    assert _flows(nodes, edges) == {("UPDPG", "PROD.MASTER", "write_file"), ("PROD.MASTER", "UPDPG", "read_file")}


def test_unsupported_dd_forms_make_no_dataset_and_are_reported_by_the_analyzer(tmp_path):
    prog = _cbl("READPG", _sel("IN-F", "INDD"), _fd("IN-F", "IN-R"), "           OPEN INPUT IN-F.\n")
    wd = _world(tmp_path, {"g/READPG.cbl": prog,
                           "g/J.jcl": "//J JOB\n//S EXEC PGM=READPG\n//INDD DD DSN=&HLQ..DATA,DISP=SHR\n//S2 EXEC PGM=SORT\n"
                                      "//SORTIN DD DSN=PROD.X\n"})
    nodes, edges, flags = _build(wd)
    assert not [n for n in nodes if n["label"] == "Dataset"] and not [e for e in edges if e["type"] == "FLOWS_TO"]
    why = sorted(f["why"] for f in flags if f.get("reason") == "dropped_syntax")
    assert why == ["dsn_symbolic", "utility_step"]
    assert any(f.get("kind") == "Dataset" and f["name"] == "INDD" for f in flags)        # 結べなかった DD 名は申告する


def test_unresolved_exec_pgm_is_reported_once_by_the_existing_flag_and_its_dds_are_not_used(tmp_path):
    wd = _world(tmp_path, {"g/J.jcl": "//J JOB\n//S EXEC PGM=NOSUCHPG\n//INDD DD DSN=PROD.X,DISP=SHR\n"})
    nodes, _edges, flags = _build(wd)
    assert [f for f in flags if f.get("reason") == "unresolved"] == [
        {"reason": "unresolved", "from": "g/J.jcl", "kind": "Module", "name": "NOSUCHPG", "line": 2, "via": None,
         "from_def": {"file": "g/J.jcl", "key": None}}]
    assert not [n for n in nodes if n["label"] == "Dataset"]


def test_use_needs_the_copy_and_follows_nested_copy_and_skips_ambiguous_names(tmp_path):
    wd = _world(tmp_path, {
        "g/OUTER.cpy": "       COPY INNER.\n       01  OUTER-ITEM PIC X.\n",
        "g/INNER.cpy": "       01  INNER-ITEM PIC X.\n       01  DUP-ITEM PIC X.\n",
        "g/OTHER.cpy": "       01  DUP-ITEM PIC X.\n       01  OTHER-ITEM PIC X.\n",
        "g/PGA.cbl": _cbl("PGA", proc="           MOVE INNER-ITEM TO OUTER-ITEM.\n           MOVE DUP-ITEM TO OTHER-ITEM.\n",
                          copy="           COPY OUTER.\n           COPY OTHER.\n"),
        "g/PGB.cbl": _cbl("PGB", proc="           MOVE INNER-ITEM TO WS-X.\n"),
    })
    nodes, edges, flags = _build(wd)
    by = {n["cid"]: n for n in nodes}
    uses = {(by[e["src"]]["name"], by[e["dst"]]["name"]) for e in edges if e["type"] == "FLOWS_TO" and e["via"] == "use"}
    assert uses == {("INNER-ITEM", "PGA"), ("OUTER-ITEM", "PGA"), ("OTHER-ITEM", "PGA")}         # PGB は COPY していないので出ない
    amb = [f for f in flags if f.get("reason") == "use_ambiguous"]
    assert amb == [{"reason": "use_ambiguous", "from": "g/PGA.cbl", "count": 1, "names": ["DUP-ITEM"]}]


def test_dataflow_does_not_cross_generations_even_with_the_same_dsn(tmp_path):
    prog_w = _cbl("WRITEPG", _sel("OUT-F", "OUTDD"), _fd("OUT-F", "OUT-R"), "           OPEN OUTPUT OUT-F.\n")
    prog_r = _cbl("READPG", _sel("IN-F", "INDD"), _fd("IN-F", "IN-R"), "           OPEN INPUT IN-F.\n")
    wd = _world(tmp_path, {
        "g1/WRITEPG.cbl": prog_w, "g1/J.jcl": "//J JOB\n//S EXEC PGM=WRITEPG\n//OUTDD DD DSN=PROD.SHARED,DISP=NEW\n",
        "g2/READPG.cbl": prog_r, "g2/J.jcl": "//J JOB\n//S EXEC PGM=READPG\n//INDD DD DSN=PROD.SHARED,DISP=SHR\n"})
    nodes, edges, _flags = _build(wd)
    assert len([n for n in nodes if n["label"] == "Dataset"]) == 2
    by = {n["cid"]: n for n in nodes}
    for e in edges:
        if e["type"] == "FLOWS_TO":
            assert by[e["src"]]["top_scope"] == by[e["dst"]]["top_scope"]


# ---- graph_impact（Neo4j の代わりに build_world の出力へ答えるセッション）----

class _WorldSession:
    """`build_world` の `(nodes, edges)` をそのまま問い合わせに答える。`world_impact` が投げる 1 段の展開（構造の入辺・`FLOWS_TO` の出辺）だけを扱う。"""

    def __init__(self, nodes, edges):
        self.nodes = {n["cid"]: n for n in nodes}
        self.edges = edges
        self.impact_rel = set(WN._IMPACT_REL.split("|"))

    def run(self, query, **p):
        from test_world_neo4j_overload import _FakeResult
        text = query.text if hasattr(query, "text") else query
        if "SherpaMeta" in text:
            return _FakeResult([{"c": 1, "era": WN.GRAPH_SCHEMA_ERA}])
        if "s.canonical_id IN $starts" in text:
            return _FakeResult([{"cid": c, "name": self.nodes[c]["name"], "path": self.nodes[c]["path"]}
                                for c in p["starts"] if c in self.nodes])
        out = "[e:FLOWS_TO]" in text
        rows = []
        for e in self.edges:
            if (e["type"] == "FLOWS_TO") != out or (not out and e["type"] not in self.impact_rel):
                continue
            near, far = (e["src"], e["dst"]) if out else (e["dst"], e["src"])
            if near not in p["frontier"]:
                continue
            m = self.nodes[far]
            rows.append({"cid": far, "name": m["name"], "label": m["label"], "status": "active", "dpath": m["path"],
                         "top": m["top_scope"], "analyzer": m.get("analyzer"), "to_cid": near,
                         "edge": {"type": e["type"], "doc": e["doc"], "line": e["line"], "via": e.get("via"),
                                  "sources": WN._sources_json(e.get("sources")), "sources_overflow": 0}})
        if "$seen" in text:
            return _FakeResult([{"x": 1}] if any(r["cid"] not in p["seen"] for r in rows) else [])
        return _FakeResult(rows)


@pytest.fixture
def impact_env(monkeypatch):
    nodes, edges, _flags = _build()
    by_name = {}
    for n in nodes:
        by_name.setdefault((n["name"], n["top_scope"]), n)
    monkeypatch.setattr(WN, "get_world_entities", lambda s, cids, *a, **k: [
        {"canonical_id": c, "label": s.nodes[c]["label"], "name": s.nodes[c]["name"], "path": s.nodes[c]["path"]} for c in cids])
    monkeypatch.setattr(WN, "read_unresolved", lambda *a, **k: {"available": True, "items": [], "omitted": 0})
    monkeypatch.setattr(WN, "world_related_documents", lambda *a, **k: [])
    monkeypatch.setattr(WN, "read_plugin_failures", lambda *a, **k: [])
    monkeypatch.setattr(WN, "read_unparsed_sources", lambda *a, **k: None)
    monkeypatch.setattr(WN, "read_generation", lambda *a, **k: "g1")
    return _WorldSession(nodes, edges), by_name


@pytest.mark.parametrize("start", [("RATE-CPY", "gen1"), ("TAX-RATE", "gen1")])
def test_graph_impact_returns_the_reader_program_as_a_flow_candidate_and_keeps_impact_unchanged(impact_env, start):
    session, by_name = impact_env
    cid = by_name[start]["cid"]
    kw = {"canonical_id": cid, "depth": 5, "limit": 50, "evidence_limit": 3}
    before = graph_tools._impact(session, "w", None, False, False, None, **kw, flow_depth=0, page_bytes=10**6)
    res = graph_tools._impact(session, "w", None, False, False, None, **kw, flow_depth=3, page_bytes=10**6)
    assert res["impact"] == before["impact"] and res["count"] == before["count"]
    assert {i["name"] for i in res["impact"]} >= {"SALESCALC", "NIGHTLY1"} and "REPORTGEN" not in {i["name"] for i in res["impact"]}
    cands = {c["name"]: c for c in res["flow_candidates"]}
    assert set(cands) == {"PROD.SALES.CALC", "REPORTGEN", "NIGHTLY2"}                  # 世代 gen2 の REPORTGEN は出ない
    rep = cands["REPORTGEN"]
    vias = [e.get("via") for e in rep["evidence"] if e["from"] != e["to"]]
    assert vias[:2] == ["read_file", "write_file"]                                    # 候補 → 起点の並び（データの向きでは write_file → read_file）
    assert rep["trace"][:3] == ["REPORTGEN", "PROD.SALES.CALC", "SALESCALC"] and rep["flow_edges"] == 2
    assert [r["type"] for r in rep["route"]][:2] == ["FLOWS_TO", "FLOWS_TO"]
    assert res["summary"]["flow_total"] == 3 and res["coverage"]["flow"] == {"requested": 3, "truncated": False}
    assert all(c["path"].startswith("gen1/") for c in res["flow_candidates"])


def test_flow_depth_one_stops_at_the_dataset_and_reports_what_is_left(impact_env):
    session, by_name = impact_env
    res = graph_tools._impact(session, "w", None, False, False, None, canonical_id=by_name[("RATE-CPY", "gen1")]["cid"],
                              depth=5, limit=50, evidence_limit=3, flow_depth=1)
    assert [c["name"] for c in res["flow_candidates"]] == ["PROD.SALES.CALC"]
    assert res["coverage"]["flow"] == {"requested": 1, "truncated": True} and res["truncated"] is True



@pytest.mark.parametrize("start,expected_impact", [(("RATE-CPY", "gen1"), {"SALESCALC", "NIGHTLY1"}),
                                                   (("TAX-RATE", "gen1"), {"RATE-CPY", "SALESCALC", "NIGHTLY1"})])
def test_graph_impact_pages_of_one_cover_the_real_traversal_with_no_gap_or_repeat(impact_env, start, expected_impact):
    """実際の段ごとの探索（循環・複数経路を含む）の結果を 1 件ずつのページで最後まで取ると、手で決めた集合と一致する。"""
    session, by_name = impact_env
    kw = {"canonical_id": by_name[start]["cid"], "depth": 5, "evidence_limit": 0, "flow_depth": 3, "limit": 1}
    names, flows, offset, token = [], [], 0, None
    for part, sink, key in (("impact", names, "impact"), ("flow", flows, "flow_candidates")):
        offset, token = 0, None
        while offset is not None:
            res = graph_tools._impact(session, "w", None, False, False, None, **kw, part=part, offset=offset, generation=token)
            assert "error" not in res, res
            sink += [x["name"] for x in res[key]]
            offset, token = res["transfer"]["next_offset"], res["generation"]
    assert sorted(names) == sorted(expected_impact) and sorted(flows) == ["NIGHTLY2", "PROD.SALES.CALC", "REPORTGEN"]
