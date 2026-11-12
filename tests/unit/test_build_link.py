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


def test_link_control_paragraphs_pair_per_executable(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": "//NIGHT JOB\n//S1 EXEC PGM=PAY100\n//S2 EXEC PGM=PAY200\n",
        "app/ソース/link.mk": ("INCLUDE OBJLIB(PAYMAIN)\nINCLUDE OBJLIB(SUB01)\nNAME PAY100(R)\n\n"
                              "INCLUDE OBJLIB(PAYTWO)\nNAME PAY200(R)\n"),
        "app/ソース/PAYMAIN.cbl": "       PROGRAM-ID. PAYMAIN.\n",
        "app/ソース/SUB01.cbl": "       PROGRAM-ID. SUB01.\n",
        "app/ソース/PAYTWO.cbl": "       PROGRAM-ID. PAYTWO.\n",
    })
    nodes, edges, flags, _report = _build(tmp_path)
    assert _materials(nodes, edges, "PAY100") == ["app/ソース/PAYMAIN.cbl", "app/ソース/SUB01.cbl"]
    assert _materials(nodes, edges, "PAY200") == ["app/ソース/PAYTWO.cbl"]
    assert not {"PAY100", "PAY200"} & _unresolved_names(flags)


def test_declared_name_edge_is_retargeted_when_old_target_is_a_material(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": "//NIGHT JOB\n//S1 EXEC PGM=DIRECT\n",
        "app/src/Makefile": "DIRECT: DIRECT.o UTIL.o\n",
        "app/src/DIRECT.cbl": "       PROGRAM-ID. DIRECT.\n",
        "app/src/UTIL.cbl": "       PROGRAM-ID. UTIL.\n",
    })
    nodes, edges, _flags, report = _build(tmp_path)
    assert _materials(nodes, edges, "DIRECT") == ["app/src/DIRECT.cbl", "app/src/UTIL.cbl"]
    assert _invokers(nodes, edges, "DIRECT") == ["app/job/night.jcl"]
    assert not [e for e in edges if e["type"] == "INVOKES" and nodes[e["dst"]]["label"] == "Module"]
    assert report["linked_names"] == 1 and report["dest_mismatch_count"] == 0


def test_declared_name_edge_is_kept_when_old_target_is_not_a_material(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": "//NIGHT JOB\n//S1 EXEC PGM=DIRECT\n",
        "app/src/Makefile": "DIRECT: OTHER.o\n",
        "app/src/DIRECT.cbl": "       PROGRAM-ID. DIRECT.\n",
        "app/src/OTHER.cbl": "       PROGRAM-ID. OTHER.\n",
    })
    nodes, edges, _flags, report = _build(tmp_path)
    assert not _execs(nodes)
    dst = [nodes[e["dst"]]["path"] for e in edges if e["type"] == "INVOKES"]
    assert dst == ["app/src/DIRECT.cbl"]
    assert report["dest_mismatch_count"] == 1 and report["dest_mismatch"][0]["name"] == "DIRECT"


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


def _period_world(tmp_path, extra):
    _write(tmp_path, {
        "app/改修/1期/job/night.jcl": JOB,
        "app/改修/1期/readme.txt": "x\n",
        "app/改修/2期/readme.txt": "x\n",
        "app/改修/3期/readme.txt": "x\n",
        **extra,
    })
    return _build(tmp_path)


def test_build_definition_in_a_later_period_does_not_serve_an_earlier_period_call(tmp_path):
    nodes, edges, flags, _report = _period_world(tmp_path, {
        "app/改修/3期/src/Makefile": "calc: main.o\n\tcc -o calc main.o\n",
        "app/改修/3期/src/main.c": "int main(void){return 0;}\n",
    })
    assert "CALC" not in _execs(nodes)
    assert not [e for e in edges if e["type"] in ("INVOKES", "BUILT_FROM") and "/3期/" in e["dst"]]
    assert "CALC" in _unresolved_names(flags)


def test_build_definition_in_the_same_period_is_linked_and_confirmed(tmp_path):
    nodes, edges, _flags, _report = _period_world(tmp_path, {
        "app/改修/1期/src/Makefile": "calc: main.o\n\tcc -o calc main.o\n",
        "app/改修/1期/src/main.c": "int main(void){return 0;}\n",
    })
    assert _materials(nodes, edges, "CALC") == ["app/改修/1期/src/main.c"]
    made = [e for e in edges if e["type"] in ("INVOKES", "BUILT_FROM") and "build:" in e["dst"] + e["src"]]
    assert len(made) == 2 and all(e.get("confirm") == "confirmed" for e in made)


def test_build_definition_with_undetermined_parent_period_is_flagged_with_candidates(tmp_path):
    _write(tmp_path, {
        "帳票/改修/初版/job/night.jcl": JOB,
        "帳票/改修/改良版/変更一覧.csv": "改良版 の変更一覧（基準: 初版）\n追加,src/Makefile\n",
        "帳票/改修/改良版/src/Makefile": "calc: main.o\n\tcc -o calc main.o\n",
        "帳票/改修/改良版/src/main.c": "int main(void){return 0;}\n",
    })
    nodes, edges, flags, _report = _build(tmp_path)
    assert "CALC" not in _execs(nodes)
    flag = next(f for f in flags if f.get("name") == "CALC")
    assert flag["reason"] == "parent_period_undetermined"
    assert flag["candidate_paths"] == ["帳票/改修/改良版/src/Makefile"]


def test_call_with_undetermined_parent_period_is_linked_by_a_build_definition_in_its_own_period(tmp_path):
    _write(tmp_path, {
        "帳票/改修/初版/job/night.jcl": JOB,
        "帳票/改修/初版/src/Makefile": "calc: main.o\n\tcc -o calc main.o\n",
        "帳票/改修/初版/src/main.c": "int main(void){return 0;}\n",
        "帳票/改修/改良版/変更一覧.csv": "改良版 の変更一覧（基準: 初版）\n追加,src/calc.cbl\n",
        "帳票/改修/改良版/src/calc.cbl": "       PROGRAM-ID. CALC.\n",
    })
    nodes, edges, flags, _report = _build(tmp_path)
    assert _materials(nodes, edges, "CALC") == ["帳票/改修/初版/src/main.c"]
    assert not [f for f in flags if f.get("name") == "CALC"]


def test_ambiguous_call_marked_period_undetermined_drops_the_candidate_count(tmp_path):
    _write(tmp_path, {
        "帳票/改修/初版/job/night.jcl": JOB,
        "帳票/改修/初版/a/calc.cbl": "       PROGRAM-ID. CALC.\n",
        "帳票/改修/初版/b/calc.cbl": "       PROGRAM-ID. CALC.\n",
        "帳票/改修/改良版/変更一覧.csv": "改良版 の変更一覧（基準: 初版）\n追加,src/Makefile\n",
        "帳票/改修/改良版/src/Makefile": "calc: main.o\n\tcc -o calc main.o\n",
        "帳票/改修/改良版/src/main.c": "int main(void){return 0;}\n",
    })
    nodes, edges, flags, _report = _build(tmp_path)
    flag = next(f for f in flags if f.get("name") == "CALC")
    assert flag["reason"] == "parent_period_undetermined"
    assert "candidates" not in flag


def test_name_rows_have_build_rows_for_each_material(tmp_path):
    from sherpa.ingest import name_rows
    _write(tmp_path, {
        "app/job/night.jcl": JOB,
        "app/src/Makefile": "calc: main.o util.o\n\tcc -o calc main.o util.o\n",
        "app/src/main.c": "int main(void){return 0;}\n",
        "app/src/util.c": "int util(void){return 0;}\n",
    })
    nodes, edges, _flags = world_graph.build_world(tmp_path, "w", resolve_config={}, build_marks=None)
    rows = name_rows.build_rows([], nodes, edges)
    build = sorted((r["name"], r["kind"], r["rel_path"]) for r in rows if r["source"] == "build")
    assert build == [("CALC", "Executable", "app/src/main.c"), ("CALC", "Executable", "app/src/util.c")]
    assert not [r for r in name_rows.build_rows([], nodes) if r["source"] == "build"]


def test_applied_signature_compares_results_not_marks(tmp_path):
    _write(tmp_path, {"app/src/Makefile": "calc: main.o\n", "app/src/main.c": "int main(void){return 0;}\n", "app/job/a.jcl": "//A JOB\n"})
    files = [(tmp_path / r, r) for r in ("app/src/Makefile", "app/src/main.c", "app/job/a.jcl")]
    base = build_link.applied_signature(files, {})
    assert base
    assert build_link.applied_signature(files, {"app/job": "skip"}) == base          # 読むファイルが変わらない印
    assert build_link.applied_signature(files, {"app/src/Makefile": "skip"}) == ""   # 読むファイルが無くなる印
    assert build_link.applied_signature(files, {"app/job/a.jcl": "read"}) != base    # 読むファイルが増える印
    (tmp_path / "app/src/Makefile").write_bytes(b"x" * (build_link.READ_MAX_BYTES + 1))
    assert build_link.applied_signature(files, {}) not in ("", base)                  # 大きさの上限で外れたことも値に入る


def test_adjacent_definitions_of_two_resolved_executables_are_not_merged(tmp_path):
    _write(tmp_path, {
        "app/job/night.jcl": "//NIGHT JOB\n//S1 EXEC PGM=AAA\n//S2 EXEC PGM=BBB\n",
        "app/src/link.mk": "NAME AAA\nINCLUDE AAA.o\nNAME BBB\nINCLUDE BBB.o\n",
        "app/src/AAA.cbl": "       PROGRAM-ID. AAA.\n",
        "app/src/BBB.cbl": "       PROGRAM-ID. BBB.\n",
    })
    nodes, edges, _flags, _report = _build(tmp_path)
    assert not _execs(nodes)                                  # どちらの実行名にも、他方の材料を混ぜた組を作らない
    declared = sorted(nodes[e["dst"]]["path"] for e in edges if e["type"] == "INVOKES")
    assert declared == ["app/src/AAA.cbl", "app/src/BBB.cbl"]  # 宣言名でつながった線は残る
