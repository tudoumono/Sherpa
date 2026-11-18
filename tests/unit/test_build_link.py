"""ビルドの定義から実行名とソースをつなぐ段: 架空の資料（C・Java・C#・COBOL）で、呼び出し→実行名→材料がつながること。"""
from __future__ import annotations

import pytest

from sherpa import build_marks
from sherpa.ingest import build_link, world_graph
from sherpa.ingest.check import BAD, GOOD, WARN, inspect_folder


def _write(root, files):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _build(root, marks=None):
    nodes, edges, flags = world_graph.build_world(root, "w", resolve_config={}, build_marks=marks)
    report, rest = world_graph.pop_build_report(flags)
    return {n["cid"]: n for n in nodes}, edges, rest, report


def _execs(nodes):
    return {n["name"]: n for n in nodes.values() if n["label"] == "Executable"}


def _materials(nodes, edges, name):
    cid = _execs(nodes)[name]["cid"]
    return sorted(nodes[e["dst"]]["path"] for e in edges if e["type"] == "BUILT_FROM" and e["src"] == cid)


def _invokers(nodes, edges, name):
    cid = _execs(nodes)[name]["cid"]
    return sorted(nodes[e["src"]]["path"] for e in edges if e["type"] == "INVOKES" and e["dst"] == cid)


def _unresolved_names(flags):
    return {f["name"] for f in flags if f.get("reason") == "unresolved"}


JOB = "//NIGHT JOB\n//STEP1 EXEC PGM=CALC\n"


def test_c_makefile_line_links_call_to_materials(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": JOB,
        "app/src/Makefile": "calc: main.o util.o\n\tcc -o calc main.o util.o\n",
        "app/src/main.c": "int main(void){return 0;}\n",
        "app/src/util.c": "int util(void){return 0;}\n",
    })
    nodes, edges, flags, report = _build(tmp_path)
    assert _materials(nodes, edges, "CALC") == ["app/src/main.c", "app/src/util.c"]
    assert _invokers(nodes, edges, "CALC") == ["app/job/night.jcl"]
    assert "CALC" not in _unresolved_names(flags)
    assert report["read"] == 1 and report["linked_names"] == 1 and report["scopes"]["line"] == 1


def test_java_ant_multiline_block(tmp_path):
    _write(tmp_path, {
        "app/job/run.sh": "#!/bin/sh\nBILLING\n",
        "app/src/build.xml": '<project>\n<target name="BILLING">\n <javac>\n  Main.java\n  Tax.java\n </javac>\n</target>\n</project>\n',
        "app/src/Main.java": "public class Main {}\n",
        "app/src/Tax.java": "public class Tax {}\n",
    })
    nodes, edges, _flags, _report = _build(tmp_path)
    assert _materials(nodes, edges, "BILLING") == ["app/src/Main.java", "app/src/Tax.java"]


def test_csharp_assembly_name_whole_file(tmp_path):
    _write(tmp_path, {
        "app/job/run.sh": "#!/bin/sh\nBILLING\n",
        "app/source/Billing.csproj": ('<Project>\n<PropertyGroup>\n<AssemblyName>Billing</AssemblyName>\n</PropertyGroup>\n'
                                      '<ItemGroup>\n<Compile Include="a\\Tax.cs" />\n<Compile Include="Pay.cs" />\n</ItemGroup>\n</Project>\n'),
        "app/source/a/Tax.cs": "public class Tax {}\n",
        "app/source/Pay.cs": "public class Pay {}\n",
    })
    nodes, edges, _flags, report = _build(tmp_path)
    assert _materials(nodes, edges, "BILLING") == ["app/source/Pay.cs", "app/source/a/Tax.cs"]
    assert report["scopes"]["block"] == 1


def test_link_control_paragraphs_and_resolved_calls_do_not_block(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": "//NIGHT JOB\n//S1 EXEC PGM=PAY100\n//S2 EXEC PGM=PAY200\n",
        "app/ソース/link.mk": ("INCLUDE OBJLIB(PAYMAIN)\nINCLUDE OBJLIB(SUB01)\nNAME PAY100(R)\n\n"
                              "INCLUDE OBJLIB(PAYTWO)\nNAME PAY200(R)\n"),
        "app/ソース/PAYMAIN.cbl": "       PROGRAM-ID. PAYMAIN.\n       PROCEDURE DIVISION.\n           CALL 'SUB01'.\n",
        "app/ソース/SUB01.cbl": "       PROGRAM-ID. SUB01.\n",
        "app/ソース/PAYTWO.cbl": "       PROGRAM-ID. PAYTWO.\n",
    })
    nodes, edges, flags, _report = _build(tmp_path)
    assert _materials(nodes, edges, "PAY100") == ["app/ソース/PAYMAIN.cbl", "app/ソース/SUB01.cbl"]
    assert _materials(nodes, edges, "PAY200") == ["app/ソース/PAYTWO.cbl"]
    assert not {"PAY100", "PAY200"} & _unresolved_names(flags)


def test_declared_name_edge_is_kept(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": "//NIGHT JOB\n//S1 EXEC PGM=DIRECT\n",
        "app/src/Makefile": "DIRECT: DIRECT.o UTIL.o\n",
        "app/src/DIRECT.cbl": "       PROGRAM-ID. DIRECT.\n",
        "app/src/UTIL.cbl": "       PROGRAM-ID. UTIL.\n",
    })
    nodes, edges, _flags, report = _build(tmp_path)
    assert not _execs(nodes)
    assert report["linked_names"] == 0
    dst = [nodes[e["dst"]]["path"] for e in edges if e["type"] == "INVOKES"]
    assert dst == ["app/src/DIRECT.cbl"]


def test_comments_are_not_read(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": JOB,
        "app/src/Makefile": "# CALC old.c\nCALC: main.o\n",
        "app/src/main.c": "int main(void){return 0;}\n",
        "app/src/old.c": "int old(void){return 0;}\n",
    })
    nodes, edges, _flags, _report = _build(tmp_path)
    assert _materials(nodes, edges, "CALC") == ["app/src/main.c"]


def test_outside_source_group_is_not_read_unless_marked(tmp_path):
    files = {
        "app/job/night.jcl": JOB,
        "app/build/link.ctl": "CALC main.c\n",
        "app/lib/main.c": "int main(void){return 0;}\n",
    }
    _write(tmp_path, files)
    nodes, _edges, flags, report = _build(tmp_path)
    assert not _execs(nodes) and "CALC" in _unresolved_names(flags) and report == {}
    nodes, edges, flags, report = _build(tmp_path, {"app/build/link.ctl": "read"})
    assert _materials(nodes, edges, "CALC") == ["app/lib/main.c"]
    assert report["marks_sig"] == build_link.marks_signature({"app/build/link.ctl": "read"})


def test_general_config_read_only_when_folder_marked_and_skip_wins(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": JOB,
        "app/src/link.json": '{"CALC": "main.c"}\n',
        "app/src/main.c": "int main(void){return 0;}\n",
    })
    assert not _execs(_build(tmp_path)[0])
    nodes, edges, _flags, _report = _build(tmp_path, {"app/src": "read"})
    assert _materials(nodes, edges, "CALC") == ["app/src/main.c"]
    assert not _execs(_build(tmp_path, {"app/src": "read", "app/src/link.json": "skip"})[0])


def test_conflict_at_same_distance_leaves_call_unlinked(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": JOB,
        "app/a/src/Makefile": "CALC: x.o\n",
        "app/b/src/Makefile": "CALC: y.o\n",
        "app/a/src/x.c": "int x(void){return 0;}\n",
        "app/b/src/y.c": "int y(void){return 0;}\n",
    })
    nodes, _edges, flags, report = _build(tmp_path)
    assert not _execs(nodes) and "CALC" in _unresolved_names(flags) and report["conflict_count"] == 1


def test_top_folder_boundary(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": JOB,
        "other/src/Makefile": "CALC: x.o\n",
        "other/src/x.c": "int x(void){return 0;}\n",
    })
    assert not _execs(_build(tmp_path)[0])


def test_unreadable_shape_and_no_analyzer_inputs_reported(tmp_path):
    _write(tmp_path, {
        "app/job/run.sh": "#!/bin/sh\nCALC\nTOOL\nNIGHTLY\n",
        "app/src/Makefile": "CALC TOOL: x.o\n",
        "app/src/x.c": "int x(void){return 0;}\n",
        "app/src/pyproject.toml": 'NIGHTLY = "batch.py"\n',
        "app/src/batch.py": "print(1)\n",
    })
    nodes, _edges, flags, report = _build(tmp_path)
    assert not _execs(nodes)
    assert {"CALC", "TOOL", "NIGHTLY"} <= _unresolved_names(flags)
    assert {u["name"] for u in report["unreadable"]} == {"CALC", "TOOL"}
    assert report["input_unresolved"][0]["token"] == "batch.py" and report["input_unresolved"][0]["why"] == "no_analyzer"


def test_multiple_executables_in_one_definition_have_distinct_nodes(tmp_path):
    _write(tmp_path, {
        "app/job/run.sh": "#!/bin/sh\nCALC\nTOOL\n",
        "app/src/Makefile": "CALC: a.o\n\nTOOL: b.o\n",
        "app/src/a.c": "int a(void){return 0;}\n",
        "app/src/b.c": "int b(void){return 0;}\n",
    })
    nodes, _edges, _flags, _report = _build(tmp_path)
    assert len({n["cid"] for n in _execs(nodes).values()}) == 2
    assert {n["path"] for n in _execs(nodes).values()} == {"app/src/Makefile"}


def test_full_width_source_folder_name():
    assert build_link.in_source_group("p/ＳＲＣ/Makefile")
    assert build_link.in_source_group("p/SOURCE/x/Makefile")
    assert not build_link.in_source_group("p/build/Makefile")


def test_rule5_report():
    rep = inspect_folder(["P/1期/a.txt"], lambda _r: None, build={"report": {
        "marks_sig": "", "read": 2, "linked_names": 1, "scopes": {"line": 1, "block": 0, "file": 0},
        "unreadable": [{"path": "P/src/Makefile", "why": "no_pair", "name": "CALC"}], "unreadable_count": 1,
        "conflicts": [{"name": "X", "from": "a", "line": 1, "candidates": ["b", "c"]}], "conflict_count": 1}, "marks_sig": "abc"})
    assert rep.pick("決まり5", GOOD)[0].count == 2
    warn = rep.pick("決まり5", WARN)
    assert any("まだグラフに効いていない" in it.what for it in warn)
    assert any("P/src/Makefile" in e for it in warn for e in it.examples)
    assert rep.pick("決まり5", BAD)[0].count == 1


def test_rule5_absent_without_build_state():
    rep = inspect_folder(["P/1期/a.txt"], lambda _r: None)
    assert not [it for it in rep.items if it.rule == "決まり5"]


def test_unreadable_marks_are_not_treated_as_no_marks(monkeypatch):
    from sherpa import store

    def _boom():
        raise OSError("db down")
    monkeypatch.setattr(store, "get_system_settings", _boom)
    with pytest.raises(OSError):
        build_marks.load("w")


def test_path_written_in_definition_picks_the_exact_file_when_names_differ_only_in_case():
    files = [(None, "app/src/A.c"), (None, "app/src/a.c")]
    sources = build_link._Sources(files, {})
    assert sources.resolve("./A.c", "app/src/Makefile") == ("app/src/A.c", "")
    assert sources.resolve("./a.c", "app/src/Makefile") == ("app/src/a.c", "")
    assert sources.resolve("./A.C", "app/src/Makefile") == (None, "ambiguous")


def test_case_folded_path_counts_only_the_same_top_folder():
    sources = build_link._Sources([(None, "app/src/a.c"), (None, "APP/src/A.c")], {})
    assert sources.resolve("./A.C", "app/src/Makefile") == ("app/src/a.c", "")
