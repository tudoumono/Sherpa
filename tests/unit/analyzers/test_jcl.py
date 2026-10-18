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


# ---- DD DSN（dd_bindings）----

def _dd(text):
    res = A.extract_refs(text, "NIGHTLY.jcl")
    return [(b.exec_line, b.ddname, b.dsn, b.disp, b.gdg_rel, b.line) for b in res.dd_bindings], res


STEP = "//STEP1    EXEC PGM=SALESCALC\n"

DD_CASES = {
    "plain_dsn_and_disp": (
        JOB + STEP + "//OUTDD    DD DSN=PROD.SALES.CALC,DISP=(NEW,CATLG,DELETE)\n",
        [(2, "OUTDD", "PROD.SALES.CALC", "NEW,CATLG,DELETE", "", 3)]),
    "dsname_is_the_same_as_dsn": (JOB + STEP + "//INDD     DD DSNAME=PROD.IN,DISP=SHR\n", [(2, "INDD", "PROD.IN", "SHR", "", 3)]),
    "quoted_dsn_and_dsname_are_normalized_before_gdg": (
        JOB + STEP + "//OUTDD    DD DSN='PROD.SALES.CALC(+1)'\n//INDD     DD DSNAME='PROD.SALES.CALC(0)'\n"
        "//PLAIN    DD DSN=PROD.SALES.CALC\n",
        [(2, "OUTDD", "PROD.SALES.CALC", "", "+1", 3), (2, "INDD", "PROD.SALES.CALC", "", "0", 4),
         (2, "PLAIN", "PROD.SALES.CALC", "", "", 5)]),
    "dsn_after_other_parameters": (
        JOB + STEP + "//INDD     DD UNIT=SYSDA,DSN=PROD.IN\n", [(2, "INDD", "PROD.IN", "", "", 3)]),
    "continuation_lines_are_joined": (
        JOB + STEP + "//OUTDD    DD DSN=PROD.SALES.CALC,\n//            DISP=(NEW,CATLG),\n//*  comment between\n"
        "//            UNIT=SYSDA\n//NEXTDD   DD DSN=PROD.NEXT\n",
        [(2, "OUTDD", "PROD.SALES.CALC", "NEW,CATLG", "", 3), (2, "NEXTDD", "PROD.NEXT", "", "", 7)]),
    "columns_after_72_are_ignored": (
        JOB + STEP + "//INDD     DD DSN=PROD.IN,DISP=SHR" + " " * 40 + "DSN=OTHER\n", [(2, "INDD", "PROD.IN", "SHR", "", 3)]),
    "comment_after_operands_is_not_a_continuation": (
        JOB + STEP + "//INDD     DD DSN=PROD.IN   NOTE, ABOUT THIS\n//OUTDD    DD DSN=PROD.OUT\n",
        [(2, "INDD", "PROD.IN", "", "", 3), (2, "OUTDD", "PROD.OUT", "", "", 4)]),
    "concatenation_adds_sources_to_the_previous_dd": (
        JOB + STEP + "//INDD     DD DSN=PROD.IN1,DISP=SHR\n//         DD DSN=PROD.IN2,DISP=SHR\n",
        [(2, "INDD", "PROD.IN1", "SHR", "", 3), (2, "INDD", "PROD.IN2", "SHR", "", 4)]),
    "gdg_relative_generation_is_split_off": (
        JOB + STEP + "//OUTDD    DD DSN=PROD.SALES.CALC(+1),DISP=(NEW,CATLG)\n//INDD     DD DSN=PROD.SALES.CALC(0),DISP=SHR\n"
        "//PREVDD   DD DSN=PROD.SALES.CALC(-1),DISP=SHR\n",
        [(2, "OUTDD", "PROD.SALES.CALC", "NEW,CATLG", "+1", 3), (2, "INDD", "PROD.SALES.CALC", "SHR", "0", 4),
         (2, "PREVDD", "PROD.SALES.CALC", "SHR", "-1", 5)]),
    "pds_member_is_not_a_generation": (
        JOB + STEP + "//LIBDD    DD DSN=PROD.LIB(MEMBER1),DISP=SHR\n", [(2, "LIBDD", "PROD.LIB(MEMBER1)", "SHR", "", 3)]),
    "temporary_dataset_keeps_the_double_ampersand": (
        JOB + STEP + "//WORK     DD DSN=&&TEMP1,DISP=(NEW,PASS)\n", [(2, "WORK", "&&TEMP1", "NEW,PASS", "", 3)]),
    "backreference_takes_the_dsn_of_the_referenced_dd": (
        JOB + STEP + "//OUTDD    DD DSN=PROD.SALES.CALC(+1),DISP=(NEW,CATLG)\n//STEP2    EXEC PGM=REPORTGEN\n"
        "//INDD     DD DSN=*.STEP1.OUTDD,DISP=SHR\n",
        [(2, "OUTDD", "PROD.SALES.CALC", "NEW,CATLG", "+1", 3), (4, "INDD", "PROD.SALES.CALC", "SHR", "+1", 5)]),
    "dd_before_any_exec_pgm_is_ignored": (JOB + "//INDD     DD DSN=PROD.IN\n" + STEP, []),
    "dd_of_a_proc_step_is_not_bound": (JOB + "//STEP1    EXEC MYPROC\n//INDD     DD DSN=PROD.IN\n", []),
    "second_step_keeps_its_own_exec_line": (
        JOB + STEP + "//A        DD DSN=PROD.A\n//STEP2    EXEC PGM=OTHERPGM\n//B        DD DSN=PROD.B\n",
        [(2, "A", "PROD.A", "", "", 3), (4, "B", "PROD.B", "", "", 5)]),
}


@pytest.mark.parametrize("text,expected", DD_CASES.values(), ids=DD_CASES)
def test_dd_bindings(text, expected):
    got, _res = _dd(text)
    assert got == expected


NOT_DATASETS = {
    "sysout": "//SYSOUT   DD SYSOUT=*\n",
    "dummy": "//OUTDD    DD DUMMY\n",
    "instream": "//SYSIN    DD *\nDATA LINE DSN=NOT.A.DSN\n/*\n",
    "instream_data": "//SYSIN    DD DATA\n",
    "ddname_reference": "//OUTDD    DD DDNAME=OTHER\n",
}


@pytest.mark.parametrize("dd", NOT_DATASETS.values(), ids=NOT_DATASETS)
def test_dd_without_a_dsn_emits_nothing(dd):
    got, res = _dd(JOB + STEP + dd)
    assert got == [] and res.dropped == []


DD_DROPPED = {
    "symbolic_dsn": ("//INDD     DD DSN=&HLQ..SALES,DISP=SHR\n", "dsn_symbolic", 3),
    "symbolic_dsn_in_the_middle": ("//INDD     DD DSN=PROD.&ENV.SALES\n", "dsn_symbolic", 3),
    "dd_override_of_a_proc_step": ("//STEP1.INDD DD DSN=PROD.IN\n", "dd_override", 3),
    "unresolved_backreference": ("//INDD     DD DSN=*.NOSTEP.NODD\n", "dsn_backref_unresolved", 3),
}


@pytest.mark.parametrize("dd,reason,line", DD_DROPPED.values(), ids=DD_DROPPED)
def test_unsupported_dd_forms_are_dropped_with_a_closed_reason(dd, reason, line):
    got, res = _dd(JOB + STEP + dd)
    assert got == [] and [(d.reason, d.line) for d in res.dropped] == [(reason, line)]


def test_utility_step_is_dropped_and_its_dds_are_not_bound():
    got, res = _dd(JOB + "//SORTSTEP EXEC PGM=SORT\n//SORTIN   DD DSN=PROD.IN,DISP=SHR\n//SORTOUT  DD DSN=PROD.OUT\n" + STEP
                   + "//INDD     DD DSN=PROD.MAIN\n")
    assert got == [(5, "INDD", "PROD.MAIN", "", "", 6)]
    assert [(d.reason, d.line) for d in res.dropped] == [("utility_step", 2)]


def test_existing_refs_are_unchanged_by_dd_parsing():
    res = A.extract_refs(JOB + STEP + "//OUTDD    DD DSN=PROD.X,\n//            DISP=SHR\n", "NIGHTLY.jcl")
    assert [(r.edge_type, r.kind, r.name, r.line) for r in res.refs] == [("INVOKES", "Module", "SALESCALC", 2)]


def test_version_is_bumped_for_the_new_vocabulary():
    assert A.version == 2
