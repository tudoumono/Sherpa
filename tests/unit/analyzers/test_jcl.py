"""`JclAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S）。"""
from __future__ import annotations

import pytest

from sherpa.ingest.analyzers.jcl import JclAnalyzer

A = JclAnalyzer()
JOB = "//NIGHTLY  JOB (ACCT),'DAILY'\n"


def test_extensions_match_static_analysis_jcl_ext():
    from sherpa.ingest.static_analysis import JCL_EXT
    assert A.extensions == frozenset(JCL_EXT)
    assert A.name == "jcl"


# ---- primary（JOB／PROC／INCLUDE 断片）----
PRIMARY_CASES = {
    "job": ("//NIGHTLY  JOB (ACCT),'DAILY BATCH'\n//STEP1    EXEC PGM=TAXCALC\n", "案件A/NIGHTLY.jcl", "NIGHTLY", "job"),
    "job_in_comment_falls_back_to_include": (
        "//* NIGHTLY JOB (ACCT)\n//STEP1    EXEC PGM=TAXCALC\n", "案件A/x.jcl", "X", "include"),
    "named_proc_without_job": ("//STEPPROC PROC\n//STEP1    EXEC PGM=PGMA\n", "案件A/STEPPROC.jcl", "STEPPROC", "proc"),
    "unnamed_proc_falls_back_to_file_stem": ("// PROC\n//STEP1    EXEC PGM=PGMA\n", "案件A/STEP2PROC.jcl", "STEP2PROC", "proc"),
    "include_fragment_without_job_or_proc": ("//STEPX    EXEC PGM=PGMC\n", "案件A/COMMON.jcl", "COMMON", "include"),
    "job_wins_over_instream_proc": (
        JOB + "//INNERPRC PROC\n//STEP1    EXEC PGM=TAXCALC\n// PEND\n//STEP2    EXEC PGM=BILLGEN\n",
        "案件A/NIGHTLY.jcl", "NIGHTLY", "job"),
}


@pytest.mark.parametrize("text,path,name,kind", PRIMARY_CASES.values(), ids=PRIMARY_CASES)
def test_collect_defs_primary(text, path, name, kind):
    res = A.collect_defs(text, path)
    assert res.primary is not None and res.primary.label == "Batch" and res.primary.name == name
    assert res.primary.extra == {"jcl_kind": kind}
    assert res.children == []


# ---- refs: (入力, 参照[(edge, kind, name, via)], Dropped[(reason, line|None, snippet に含む語|None)]) ----
def M(name):
    return ("INVOKES", "Module", name, None)


def B(name, via):
    return ("INVOKES", "Batch", name, via)


REFS_CASES = {
    "exec_pgm_as_module": (JOB + "//STEP1    EXEC PGM=TAXCALC\n//STEP2    EXEC PGM=BILLGEN\n",
                           [M("TAXCALC"), M("BILLGEN")], []),
    "comment_lines_ignored": (JOB + "//* EXEC PGM=SHOULD-NOT-APPEAR\n//STEP1    EXEC PGM=TAXCALC\n", [M("TAXCALC")], None),
    "exec_proc_equals_form": (JOB + "//STEP1    EXEC PROC=MYPROC\n", [B("MYPROC", "exec_proc")], []),
    "exec_bare_name_form": (JOB + "//STEP1    EXEC MYPROC\n", [B("MYPROC", "exec_proc")], []),
    "exec_proc_strips_trailing_params": (JOB + "//STEP1    EXEC PROC=MYPROC,PARM='Y'\n", [B("MYPROC", "exec_proc")], None),
    "symbolic_exec_target_dropped": (JOB + "//STEP1    EXEC &PROCNAME\n", [], [("proc_symbolic", 2, "//STEP1    EXEC &PROCNAME")]),
    "include_member": (JOB + "// INCLUDE MEMBER=SHAREDJC\n", [B("SHAREDJC", "include_member")], []),
    "include_without_member_dropped": (JOB + "// INCLUDE SOMETHING-ELSE\n", [], [("include_unparsed", 2, "// INCLUDE SOMETHING-ELSE")]),
    "pgm_not_double_counted_as_proc": (JOB + "//STEP1    EXEC PGM=TAXCALC\n", [M("TAXCALC")], []),
    "exec_with_omitted_name_field": (JOB + "//         EXEC MYPROC\n//         EXEC PGM=PGMA\n",
                                     [B("MYPROC", "exec_proc"), M("PGMA")], []),
    "symbolic_pgm_dropped_not_batch_ref": (JOB + "//STEP1    EXEC PGM=&PROGRAM\n", [], [("pgm_symbolic", 2, "//STEP1    EXEC PGM=&PROGRAM")]),
    "instream_proc_exec_pgm_still_invoked": (
        JOB + "//INNERPRC PROC\n//STEP1    EXEC PGM=TAXCALC\n// PEND\n//STEP2    EXEC PGM=BILLGEN\n",
        [M("TAXCALC"), M("BILLGEN")], []),
}


@pytest.mark.parametrize("text,refs,dropped", REFS_CASES.values(), ids=REFS_CASES)
def test_extract_refs(text, refs, dropped):
    res = A.extract_refs(text, "NIGHTLY.jcl")
    got = [(r.edge_type, r.kind, r.name, r.extra.get("via")) for r in res.refs]
    assert got == refs
    if dropped is not None:
        assert len(res.dropped) == len(dropped)
        for d, (reason, line, needle) in zip(res.dropped, dropped):
            assert d.reason == reason
            assert line is None or d.line == line
            assert needle is None or needle == d.snippet
