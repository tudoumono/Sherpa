"""未解決の申告の結び付け（`from_def`・候補数・保存形）と、主定義の無いファイル・COPY の循環の申告（ANA-14 S1b）。"""
from __future__ import annotations

import pathlib

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]


def _world(tmp_path, files: dict):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return world_graph.build_world(tmp_path, "s1b_test")


def _reasons(flags, reason):
    return [f for f in flags if f["reason"] == reason]


# 主定義が無い（型宣言が入れ子の中だけ）Java。`new` は参照として読まれるが、始点にする主体が無い。
_NO_TYPE_3 = "void m() {\n new A1();\n new B1();\n new C1();\n}\n"
_NESTED_ONLY = "{\n class In {}\n}\nvoid m() { new A1(); }\n"


def test_no_primary_definition_counts_refs_and_keeps_lines(tmp_path):
    """① 参照 3 件（別々の行）→ 件数 3・`lines` 3 件・辺と未解決の申告は出ない。"""
    nodes, edges, flags = _world(tmp_path, {"g/A.java": _NO_TYPE_3})
    f = _reasons(flags, "no_primary_definition")
    assert f == [{"reason": "no_primary_definition", "analyzer": "java", "from": "g/A.java",
                  "count": 3, "line": 2, "lines": [2, 3, 4]}]
    assert not nodes and not edges
    assert not any(x.get("kind") for x in flags)


def test_no_primary_definition_absent_without_refs_or_with_primary(tmp_path):
    """② 参照 0 件・主定義なし → 出ない。④ 主定義があるファイル → 出ない。"""
    _n, _e, flags = _world(tmp_path, {"g/Empty.java": "// nothing\n", "g/B.java": "class B { void m() { new Zq(); } }\n"})
    assert not _reasons(flags, "no_primary_definition")


def test_no_primary_definition_is_separate_from_dropped_syntax(tmp_path):
    """③ `Dropped` もあるファイル → `dropped_syntax` と `no_primary_definition` が 1 件ずつ。"""
    _n, _e, flags = _world(tmp_path, {"g/C.java": _NESTED_ONLY})
    assert len(_reasons(flags, "dropped_syntax")) == 1
    assert len(_reasons(flags, "no_primary_definition")) == 1


def test_no_primary_definition_lines_are_capped(tmp_path):
    text = "void m() {\n" + "".join(f" new T{i}();\n" for i in range(25)) + "}\n"
    _n, _e, flags = _world(tmp_path, {"g/Big.java": text})
    (f,) = _reasons(flags, "no_primary_definition")
    assert f["count"] == 25 and len(f["lines"]) == 20 and f["lines_omitted"] == 5
    assert f["lines"] == sorted(f["lines"]) and f["line"] == f["lines"][0]


def _unresolved_of(nodes, rel):
    (n,) = [n for n in nodes if n["path"] == rel and "unresolved" in n]
    return n


def test_from_def_is_the_definition_for_languages_with_def_nodes_else_the_subject(tmp_path):
    """定義ノードがある言語（C・VB）は参照元の定義、無い言語（COBOL）・定義の外は主体（`key: null`）。"""
    nodes, _e, flags = _world(tmp_path, {
        "g/a.c": "int run(void) {\n    return missing();\n}\nint seed = absent();\n",
        "g/P.cbl": "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. PX.\n       PROCEDURE DIVISION.\n           CALL 'NOPE'.\n",
    })
    c = {i["name"]: i["from_def"] for i in _unresolved_of(nodes, "g/a.c")["unresolved"]}
    assert c == {"missing": {"file": "g/a.c", "key": "a.c.run"}, "absent": {"file": "g/a.c", "key": None}}
    (cob,) = _unresolved_of(nodes, "g/P.cbl")["unresolved"]
    assert cob["from_def"] == {"file": "g/P.cbl", "key": None}
    assert all("from_def" in f for f in flags if f["reason"] == "unresolved")


def test_from_def_vb_key_includes_the_namespace(tmp_path):
    nodes, _e, flags = _world(tmp_path, {"g/Shop.vb": (
        "Namespace Acme.Shop\n    Public Class Cart\n        Public Sub Add()\n"
        "            Dim h As Missing1\n        End Sub\n    End Class\nEnd Namespace\n")})
    (f,) = _reasons(flags, "unresolved")
    assert f["from_def"] == {"file": "g/Shop.vb", "key": "ACME.SHOP.CART.ADD"}


def test_qualifier_miss_reason_is_stored_with_from_def(tmp_path):
    """S3 の理由（`unresolved_qualifier`）も、理由を列挙せずに同じ経路で保存の項目になる。"""
    nodes, _e, flags = _world(tmp_path, {
        "g/lib/Helper.java": "package lib;\npublic class Helper {}\n",
        "g/app/Main.java": "package app;\npublic class Main {\n void m() { new approved.Helper(); }\n}\n"})
    (f,) = _reasons(flags, "unresolved_qualifier")
    (it,) = _unresolved_of(nodes, "g/app/Main.java")["unresolved"]
    assert it["reason"] == "unresolved_qualifier" and it["name"] == "approved.Helper"
    assert it["from_def"] == f["from_def"] == {"file": "g/app/Main.java", "key": None}


def test_ambiguous_flag_carries_candidate_count_and_is_stored_on_the_subject_node(tmp_path):
    nodes, _e, flags = _world(tmp_path, {
        "g/x/Helper.java": "class Helper {}\n", "g/y/Helper.java": "class Helper {}\n",
        "g/Main.java": "class Main { void m() { new Helper(); } }\n"})
    (f,) = _reasons(flags, "ambiguous")
    assert f["candidates"] == 2 and f["from_def"] == {"file": "g/Main.java", "key": None}
    n = _unresolved_of(nodes, "g/Main.java")
    assert n["unresolved_names"] == ["Helper"]
    assert n["unresolved"] == [{"line": 1, "reason": "ambiguous", "kind": "Module", "name": "Helper",
                                "via": "call", "from_def": {"file": "g/Main.java", "key": None}, "candidates": 2}]
    assert "unresolved_overflow_count" not in n


def test_stored_unresolved_is_capped_at_50_per_file_with_overflow_count(tmp_path):
    body = "".join(f" new Q{i:03d}();\n" for i in range(60))
    nodes, _e, flags = _world(tmp_path, {"g/Many.java": "class Many {\n void m() {\n" + body + " }\n}\n"})
    n = _unresolved_of(nodes, "g/Many.java")
    assert len(n["unresolved"]) == 50 and n["unresolved_overflow_count"] == 10
    assert len(n["unresolved_names"]) == 60                      # 検索用の名前は保存の上限で切らない
    assert len(_reasons(flags, "unresolved")) == 60              # 取り込み記録（flags）は全件


def test_copy_cycle_across_files_is_flagged_once_and_edges_are_kept():
    nodes, edges, flags = world_graph.build_world(ROOT / "fixtures" / "corpus" / "ana-s4", "ana-s4")
    cycles = _reasons(flags, "copy_cycle")
    assert cycles == [{"reason": "copy_cycle", "paths": ["cobol/CYCA.cpy", "cobol/CYCB.cpy"]}]
    path = {n["cid"]: n["path"] for n in nodes}
    pairs = {(path[e["src"]], path[e["dst"]]) for e in edges if e["type"] == "COPIES"}
    assert {("cobol/CYCA.cpy", "cobol/CYCB.cpy"), ("cobol/CYCB.cpy", "cobol/CYCA.cpy")} <= pairs


def test_graph_storage_version_is_part_of_the_schema_era(monkeypatch):
    from sherpa.ingest import world_neo4j
    base = world_neo4j._compute_graph_schema_era()
    monkeypatch.setattr(world_neo4j, "GRAPH_STORAGE_VERSION", world_neo4j.GRAPH_STORAGE_VERSION + 1)
    assert world_neo4j._compute_graph_schema_era() != base


def test_cap_keeps_reasons_with_project_candidates_before_plain_unresolved(tmp_path):
    """上限で切るとき、ambiguous などプロジェクトの中に候補がありうる理由が、素の unresolved に押し出されない。"""
    body = "".join(f" new Q{i:03d}();\n" for i in range(60)) + " new Helper();\n"
    nodes, _e, _f = _world(tmp_path, {
        "g/x/Helper.java": "class Helper {}\n", "g/y/Helper.java": "class Helper {}\n",
        "g/Many.java": "class Many {\n void m() {\n" + body + " }\n}\n"})
    n = _unresolved_of(nodes, "g/Many.java")
    assert len(n["unresolved"]) == 50 and n["unresolved_overflow_count"] == 11
    assert [it["line"] for it in n["unresolved"]] == sorted(it["line"] for it in n["unresolved"])
    assert sum(it["reason"] == "ambiguous" for it in n["unresolved"]) == 1
