"""辺の根拠 `sources`（ANA-14 S7）: 同じ 2 ノードを結ぶ複数の参照を 1 本の辺の根拠として束ねる契約と、保存・読み取りの形。"""
from __future__ import annotations

import json
import pathlib

from sherpa.ingest import world_graph, world_neo4j

GOLDEN = pathlib.Path(__file__).resolve().parent / "goldens" / "world_graph_edge_sources.json"

_TWICE = {   # 同じ関数を同じ呼び出し元の 2 か所から呼ぶ（別の関数からの呼び出しは別の辺）
    "g/main.c": "int main(void) {\n  helper();\n  helper();\n  return 0;\n}\nint other(void) {\n  helper();\n  return 1;\n}\n",
    "g/helper.c": "int helper(void) {\n  return 1;\n}\n",
}
_IMPORTED = {   # 近くの同名ではなく import 先へ張る（解決規則の記録）
    "g/app/Main.java": "package app;\nimport lib.Target;\nclass Main {\n  void a() { new Target(); }\n}\n",
    "g/app/Target.java": "package app;\nclass Target {}\n",
    "g/lib/Target.java": "package lib;\nclass Target {}\n",
}
_MENTIONED = {   # 設計書がコード名を別の行で 2 回言及する
    "g/helper.c": "int helper(void) {\n  return 1;\n}\n",
    "g/設計書.md": "# 概要\nhelper を呼び出す。\n\n## 詳細\n手順の途中で再び helper を使う。\n",
}


def _build(tmp_path, files):
    for rel, text in files.items():
        p = tmp_path / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    return world_graph.build_world(tmp_path, "s7_test")


def _edge(edges, etype, src_sub, dst_sub):
    (e,) = [e for e in edges if e["type"] == etype and src_sub in e["src"] and dst_sub in e["dst"]]
    return e


def test_two_references_become_two_sources_of_one_edge(tmp_path):
    _n, edges, _f = _build(tmp_path, _TWICE)
    invokes = [e for e in edges if e["type"] == "INVOKES"]
    assert len(invokes) == 2                                  # 呼び出し元の関数ごとに 1 本（本数は参照の数でなく (始点,型,終点) の数）
    e = _edge(edges, "INVOKES", "main.c.main", "helper.c.helper")
    assert [s["line"] for s in e["sources"]] == [2, 3]
    assert (e["line"], e["via"]) == (e["sources"][0]["line"], e["sources"][0]["via"])   # 先頭の根拠が代表
    assert all(s["rule"] == "nearest_name" and s["from_def"] == {"file": "g/main.c", "key": "main.c.main"}
               for s in e["sources"])
    assert "sources_overflow_count" not in e


def test_same_reference_is_not_repeated(tmp_path):
    _n, edges, _f = _build(tmp_path, {"g/main.c": "int main(void) {\n  helper(); helper();\n}\n", "g/helper.c": _TWICE["g/helper.c"]})
    e = _edge(edges, "INVOKES", "main.c.main", "helper.c.helper")
    assert len(e["sources"]) == 1


def test_sources_are_capped_at_twenty_with_overflow_count(tmp_path):
    calls = "".join("  helper();\n" for _ in range(25))
    _n, edges, _f = _build(tmp_path, {"g/main.c": "int main(void) {\n" + calls + "}\n", "g/helper.c": _TWICE["g/helper.c"]})
    e = _edge(edges, "INVOKES", "main.c.main", "helper.c.helper")
    assert len(e["sources"]) == 20 and e["sources_overflow_count"] == 5
    assert [s["line"] for s in e["sources"]] == sorted(s["line"] for s in e["sources"]) and e["line"] == 2


def test_rule_names_the_resolution_that_decided_the_connection(tmp_path):
    _n, edges, _f = _build(tmp_path, _IMPORTED)
    e = _edge(edges, "INVOKES", "app/Main.java", "lib/Target.java")
    assert [s["rule"] for s in e["sources"]] == ["single_import"]


def test_mention_edge_keeps_where_in_the_document_it_was_written(tmp_path):
    _n, edges, _f = _build(tmp_path, _MENTIONED)
    e = _edge(edges, "DOCUMENTS", "設計書.md", "helper.c.helper")
    assert (e["doc"], e["line"], e["via"]) == ("g/設計書.md", 0, "mention")
    assert [(s["doc_id"], s["line"], s["rule"], s["locator"]) for s in e["sources"]] == [
        ("g/設計書.md", 0, "dictionary_match", "概要・本文 2 行目"),
        ("g/設計書.md", 0, "dictionary_match", "詳細・本文 5 行目")]


def test_every_emitted_rule_is_in_the_closed_list():
    root = pathlib.Path(__file__).resolve().parents[2] / "fixtures" / "corpus"
    rules = set()
    for wd, wid in (("v1", "s7_v1"), ("java1", "s7_java1")):
        _n, edges, _f = world_graph.build_world(root / wd, wid)
        rules |= {s["rule"] for e in edges for s in e.get("sources", []) if "rule" in s}
    assert rules and rules <= world_graph.EDGE_RULES


def test_sources_golden_for_the_reproduction_inputs(tmp_path):
    """同じ関数を 2 か所から呼ぶ入力・import 先を選ぶ入力・設計書が 2 回言及する入力の辺の `sources` の JSON を固定する。"""
    got = {}
    for name, files in (("called_twice", _TWICE), ("single_import", _IMPORTED), ("mentioned_twice", _MENTIONED)):
        d = tmp_path / name
        d.mkdir()
        _n, edges, _f = _build(d, files)
        got[name] = sorted(({"type": e["type"], "src": e["src"], "dst": e["dst"], "sources": e["sources"],
                             **({"sources_overflow_count": e["sources_overflow_count"]} if "sources_overflow_count" in e else {})}
                            for e in edges if "sources" in e), key=lambda x: (x["type"], x["src"], x["dst"]))
    assert got == json.loads(GOLDEN.read_text(encoding="utf-8"))


def test_edge_row_writes_only_sources():
    row = world_neo4j._edge_row({"src": "a", "dst": "b", "doc": "d", "line": 3, "via": "call",
                                 "sources": [{"doc_id": "d", "line": 3}], "sources_overflow_count": 2})
    assert json.loads(row["sources"]) == [{"doc_id": "d", "line": 3}] and row["sources_overflow"] == 2
    assert not {"source", "evidence", "rule"} & set(row)


def test_edge_view_reads_rule_from_the_first_source_and_limits_sources():
    raw = {"type": "INVOKES", "doc": "d", "line": 2, "via": "call", "sources_overflow": 1,
           "sources": json.dumps([{"doc_id": "d", "line": n, "rule": "same_package"} for n in (2, 5, 9, 12)])}
    v = world_neo4j.edge_view(raw, limit=3)
    assert v["rule"] == "same_package" and [s["line"] for s in v["sources"]] == [2, 5, 9]
    assert v["sources_overflow_count"] == 2                   # 保存の上限超過 1 ＋ 返さなかった 1
    assert world_neo4j.limit_edge_sources([v], 1)[0]["sources_overflow_count"] == 4
    assert world_neo4j.limit_edge_sources([v], -1)[0]["sources"] == []          # 範囲外は 0〜上限に収める
    assert world_neo4j.edge_view({"type": "CONTAINS", "doc": "d", "line": 1}) == {"type": "CONTAINS", "doc": "d", "line": 1}
