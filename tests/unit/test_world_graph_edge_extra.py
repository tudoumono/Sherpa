"""基盤層（CODE-2・JAVA-1 残課題#3/#4）の単体テスト。

`RefCandidate.extra` の解決後エッジへの加算的透過・`via`（docs/05-グラフ語彙.md §2 の
INVOKES/CONTAINS 一般化・2026-09-03裁定）の既知値検証・`DefResult.children` の解決索引登録を、
Java に依存しない最小のフェイクアナライザで固定する（`registry._ANALYZERS` を monkeypatch する
流儀＝`tests/unit/test_corpus_docs_analyzer_registry.py` と同じ・Java 非依存で共通層そのものを
検証する）。
"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph
from sherpa.ingest.analyzers import registry
from sherpa.ingest.analyzers._base import (Analyzer, DefItem, DefResult,
                                           RefCandidate, RefResult)

ROOT = pathlib.Path(__file__).resolve().parents[2]


class _FakeAnalyzer(Analyzer):
    """`collect_defs`/`extract_refs` の戻り値を rel_path ごとに差し替えられる最小フェイク。"""

    name = "fake"
    extensions = frozenset({".fk"})
    doctype = "fake"

    def __init__(self, defs_by_rel: dict, refs_by_rel: dict):
        self._defs = defs_by_rel
        self._refs = refs_by_rel

    def collect_defs(self, text, rel_path):
        return self._defs.get(rel_path, DefResult())

    def extract_refs(self, text, rel_path):
        return self._refs.get(rel_path, RefResult())


def _world(tmp_path, files: dict):
    wd = tmp_path / "world"
    wd.mkdir()
    for rel, content in files.items():
        p = wd / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return wd


def test_ref_extra_lands_additively_on_the_resolved_edge(tmp_path, monkeypatch):
    wd = _world(tmp_path, {"a/A.fk": "x", "a/B.fk": "y"})
    defs = {
        "a/A.fk": DefResult(primary=DefItem(label="Module", name="A")),
        "a/B.fk": DefResult(primary=DefItem(label="Module", name="B")),
    }
    refs = {
        "a/A.fk": RefResult(refs=[RefCandidate("INVOKES", "Module", "B", 1,
                                                extra={"via": "call", "confidence": "high"})]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["via"] == "call"
    assert e["confidence"] == "high"
    assert flags == []


def test_unknown_via_is_flagged_and_dropped_but_the_edge_is_still_created(tmp_path, monkeypatch):
    """未知の `via` は「黙って新値を増やさない」——エッジ自体（構造の事実）は張るが、`via` 属性
    だけを落として `unknown_via` を flags に記録する。"""
    wd = _world(tmp_path, {"a/A.fk": "x", "a/B.fk": "y"})
    defs = {
        "a/A.fk": DefResult(primary=DefItem(label="Module", name="A")),
        "a/B.fk": DefResult(primary=DefItem(label="Module", name="B")),
    }
    refs = {
        "a/A.fk": RefResult(refs=[RefCandidate("INVOKES", "Module", "B", 1,
                                                extra={"via": "totally_new_kind"})]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert "via" not in e
    assert {"reason": "unknown_via", "analyzer": "fake", "from": "a/A.fk",
            "edge_type": "INVOKES", "via": "totally_new_kind"} in flags


def test_reserved_key_in_extra_drops_the_whole_extra_and_is_flagged(tmp_path, monkeypatch):
    """共通層が確定したエッジの既存キー（例: `doc`）と衝突したら `extra` を丸ごと捨てる——
    衝突していない他のキー（`via` 含む）だけ部分採用しない（ノード側 `_sanitized_extra` と同じ規律）。"""
    wd = _world(tmp_path, {"a/A.fk": "x", "a/B.fk": "y"})
    defs = {
        "a/A.fk": DefResult(primary=DefItem(label="Module", name="A")),
        "a/B.fk": DefResult(primary=DefItem(label="Module", name="B")),
    }
    refs = {
        "a/A.fk": RefResult(refs=[RefCandidate("INVOKES", "Module", "B", 1,
                                                extra={"doc": "spoofed", "via": "call"})]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    e = next(e for e in edges if e["type"] == "INVOKES")
    assert e["doc"] == "a/A.fk"          # 上書きされていない
    assert "via" not in e                # 衝突していないキーも部分採用しない
    assert {"reason": "reserved_key_in_extra", "analyzer": "fake", "from": "a/A.fk",
            "edge_type": "INVOKES", "keys": ["doc"]} in flags


def test_children_are_registered_into_the_resolution_index_without_corrupting_primary_src(
        tmp_path, monkeypatch):
    """JAVA-1 残課題#3 是正: `DefResult.children` も `defs` 解決索引へ登録され、他ファイルからの
    参照が解決できるようになる。同時に、children 登録が `rel_name`（Pass2 の src 決定）を
    書き換えて A.fk 自身の参照の起点を壊さないことも固定する（`_index_def` と `_def` の分離）。"""
    wd = _world(tmp_path, {"a/A.fk": "x", "a/B.fk": "y"})
    defs = {
        "a/A.fk": DefResult(primary=DefItem(label="Module", name="A"),
                            children=[DefItem(label="Module", name="Helper")]),
        "a/B.fk": DefResult(primary=DefItem(label="Module", name="B")),
    }
    refs = {
        "a/A.fk": RefResult(refs=[RefCandidate("INVOKES", "Module", "B", 1)]),
        "a/B.fk": RefResult(refs=[RefCandidate("INVOKES", "Module", "Helper", 1)]),
    }
    monkeypatch.setattr(registry, "_ANALYZERS", (_FakeAnalyzer(defs, refs),))

    nodes, edges, flags = world_graph.build_world(wd, "w")
    by_cid = {n["cid"]: (n["label"], n["name"], n["path"]) for n in nodes}
    ek = {(e["type"], by_cid[e["src"]], by_cid[e["dst"]]) for e in edges}
    assert ("INVOKES", ("Module", "A", "a/A.fk"), ("Module", "B", "a/B.fk")) in ek
    assert ("INVOKES", ("Module", "B", "a/B.fk"), ("Module", "Helper", "a/A.fk")) in ek
    assert flags == []


def test_cobol_jcl_copybook_graph_is_unaffected_by_the_extra_transparency_mechanism():
    """CODE-2 の受け入れ条件: COBOL/JCL/コピーブックはどれも `RefCandidate.extra` を積まない
    ——エッジ属性の透過機構を足しても、その graph は不変（`nodes`/`edges`/`flags` を丸ごと
    byte 同一で比較・実測は git stash による before/after 比較で確認済み・報告参照）。ここでは
    恒久リグレッションとして「COBOL/JCL/コピーブック由来のエッジに `via`/追加キーが一切乗らない」
    ことを固定する。"""
    wd = ROOT / "fixtures" / "corpus" / "v1"
    nodes, edges, flags = world_graph.build_world(wd, "v1_regress_test")
    assert edges, "COBOL/JCL コーパスなら少なくとも1本はエッジが立つはず"
    by_cid = {n["cid"]: n for n in nodes}
    base_edge_keys = {"type", "src", "dst", "doc", "line", "extraction_method", "status"}
    for e in edges:
        src_node = by_cid.get(e["src"])
        if src_node and src_node.get("analyzer") in ("cobol", "jcl", "copybook"):
            assert set(e) <= base_edge_keys, e
