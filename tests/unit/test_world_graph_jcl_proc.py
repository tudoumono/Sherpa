"""`world_graph.build_world()` 経由の JCL PROC/INCLUDE 展開の統合テスト（アナライザ拡張 S5a・
docs/proposals/2026-09-05-アナライザ拡張.md §4(d)/§9 S5a）。

`fixtures/corpus/jcl-proc` を実際に `build_world()` へ通し、`Batch(JOB) -INVOKES(via=exec_proc)->
Batch(PROC) -INVOKES-> Module` の2段・`INCLUDE MEMBER=` の `Batch` 化・世代（トップフォルダ）跨ぎで
繋がらないこと・存在しない参照先の `unresolved` flag・影響 traversal（`_IMPACT_REL`）が
`Batch→Batch→Module` を辿れることを固定する。

`exec_proc`/`include_member` は `analyzers/_base.KNOWN_VIA`/`VIA_PRIORITY` に登録済み（RV波1是正）。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph
from sherpa.ingest.world_neo4j import _IMPACT_REL

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "jcl-proc"
WORLD_ID = "jcl_proc_test"


def _build():
    return world_graph.build_world(WORLD_DIR, WORLD_ID)


def _node_keys(nodes):
    return {(n["label"], n["name"], n["path"]): n for n in nodes}


def _edge_tuples(nodes, edges):
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    return {(e["type"], by_cid[e["src"]], by_cid[e["dst"]], e.get("via")) for e in edges}


def test_job_proc_include_and_direct_module_nodes():
    nodes, _edges, _flags = _build()
    by_key = _node_keys(nodes)
    assert by_key[("Batch", "JOB1", "gen1/JOB1.jcl")]["jcl_kind"] == "job"
    assert by_key[("Batch", "STEPPROC", "gen1/STEPPROC.jcl")]["jcl_kind"] == "proc"
    assert by_key[("Batch", "STEP2PROC", "gen1/STEP2PROC.jcl")]["jcl_kind"] == "proc"    # 名前無し PROC → ファイル名ステム
    assert by_key[("Batch", "COMMON", "gen1/COMMON.jcl")]["jcl_kind"] == "include"        # INCLUDE 断片
    assert ("Module", "PGMA", "gen1/PGMA.cbl") in by_key
    assert ("Module", "PGMC", "gen1/PGMC.cbl") in by_key
    assert ("Module", "DIRECT", "gen1/DIRECT.cbl") in by_key
    assert ("Batch", "STEPPROC", "gen2/STEPPROC.jcl") in by_key                          # 別世代の同名 PROC


def test_exec_proc_and_include_member_two_hop_invokes():
    """`Batch(JOB1) -INVOKES(via=exec_proc)-> Batch(STEPPROC) -INVOKES-> Module(PGMA)` と
    `Batch(JOB1) -INVOKES(via=include_member)-> Batch(COMMON) -INVOKES-> Module(PGMC)` の
    2段（実体展開はしない・世代内最近傍で解決）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    job1 = ("Batch", "JOB1", "gen1/JOB1.jcl")
    stepproc_gen1 = ("Batch", "STEPPROC", "gen1/STEPPROC.jcl")
    step2proc = ("Batch", "STEP2PROC", "gen1/STEP2PROC.jcl")
    common = ("Batch", "COMMON", "gen1/COMMON.jcl")
    pgma = ("Module", "PGMA", "gen1/PGMA.cbl")
    pgmc = ("Module", "PGMC", "gen1/PGMC.cbl")
    direct = ("Module", "DIRECT", "gen1/DIRECT.cbl")

    assert ("INVOKES", job1, stepproc_gen1, "exec_proc") in tuples
    assert ("INVOKES", stepproc_gen1, pgma, None) in tuples
    assert ("INVOKES", job1, step2proc, "exec_proc") in tuples          # PROC= 接頭辞無し・名前無し PROC 側
    assert ("INVOKES", step2proc, pgma, None) in tuples
    assert ("INVOKES", job1, common, "include_member") in tuples
    assert ("INVOKES", common, pgmc, None) in tuples
    assert ("INVOKES", job1, direct, None) in tuples                    # EXEC PGM= 直接


def test_cross_generation_same_name_proc_does_not_link():
    """別フォルダ（世代）の同名 `STEPPROC` へは繋がらない（構造リンクは世代を跨がない）。"""
    nodes, edges, _flags = _build()
    tuples = _edge_tuples(nodes, edges)
    job1 = ("Batch", "JOB1", "gen1/JOB1.jcl")
    stepproc_gen2 = ("Batch", "STEPPROC", "gen2/STEPPROC.jcl")
    assert not any(t[0] == "INVOKES" and t[1] == job1 and t[2] == stepproc_gen2 for t in tuples)


def test_missing_proc_reference_is_unresolved_flag():
    """world 内に存在しない PROC（`GHOSTPROC`）への参照は `unresolved` flag に倒れる
    （誤った先を推測しない・エッジは張らない）。"""
    nodes, _edges, flags = _build()
    assert not any(n["name"] == "GHOSTPROC" for n in nodes)   # 解決できない参照先はノード化しない
    reasons = {(fl["reason"], fl.get("kind"), fl.get("name")) for fl in flags}
    assert ("unresolved", "Batch", "GHOSTPROC") in reasons


def test_impact_traversal_reaches_job_from_program_via_batch_chain():
    """影響探索（`world_neo4j._IMPACT_REL`＝COPIES/CONTAINS/INVOKES/ACCESSES）は `Batch→Batch`
    経由も辿るので、`Module(PGMA)` を起点に逆向きに辿ると `Batch(JOB1)` まで届く
    （world_neo4j の Cypher は使わず、グラフ dict 上で手動 BFS して検証する）。"""
    nodes, edges, _flags = _build()
    by_key = {(n["label"], n["name"], n["path"]): n["cid"] for n in nodes}
    pgma_cid = by_key[("Module", "PGMA", "gen1/PGMA.cbl")]
    job1_cid = by_key[("Batch", "JOB1", "gen1/JOB1.jcl")]

    impact_types = set(_IMPACT_REL.split("|"))
    # affected（逆向き＝dst から src への辺）を BFS: dst→[src,...]
    reverse_adj: dict = {}
    for e in edges:
        if e["type"] in impact_types:
            reverse_adj.setdefault(e["dst"], []).append(e["src"])

    seen = {pgma_cid}
    frontier = [pgma_cid]
    while frontier:
        nxt = []
        for cid in frontier:
            for src in reverse_adj.get(cid, []):
                if src not in seen:
                    seen.add(src)
                    nxt.append(src)
        frontier = nxt

    assert job1_cid in seen
