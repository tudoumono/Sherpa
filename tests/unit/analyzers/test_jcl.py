"""`JclAnalyzer` の単体テスト（`collect_defs`/`extract_refs` の入出力・docs/05 トラック S）。"""
from __future__ import annotations

from sherpa.ingest.analyzers.jcl import JclAnalyzer

A = JclAnalyzer()


def test_extensions_match_static_analysis_jcl_ext():
    from sherpa.ingest.static_analysis import JCL_EXT
    assert A.extensions == frozenset(JCL_EXT)
    assert A.name == "jcl"


def test_collect_defs_extracts_job_as_batch():
    text = "//NIGHTLY  JOB (ACCT),'DAILY BATCH'\n//STEP1    EXEC PGM=TAXCALC\n"
    res = A.collect_defs(text, "案件A/NIGHTLY.jcl")
    assert res.primary is not None
    assert res.primary.label == "Batch" and res.primary.name == "NIGHTLY"
    assert res.children == []


def test_collect_defs_returns_no_primary_without_job_statement():
    text = "//STEP1    EXEC PGM=TAXCALC\n"
    res = A.collect_defs(text, "x.jcl")
    assert res.primary is None


def test_collect_defs_ignores_job_in_comment_line():
    text = "//* NIGHTLY JOB (ACCT)\n//STEP1    EXEC PGM=TAXCALC\n"
    res = A.collect_defs(text, "x.jcl")
    assert res.primary is None


def test_extract_refs_finds_exec_pgm_as_module_invokes():
    text = (
        "//NIGHTLY  JOB (ACCT),'DAILY'\n"
        "//STEP1    EXEC PGM=TAXCALC\n"
        "//STEP2    EXEC PGM=BILLGEN\n"
    )
    res = A.extract_refs(text, "NIGHTLY.jcl")
    got = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert got == {("INVOKES", "Module", "TAXCALC"), ("INVOKES", "Module", "BILLGEN")}
    assert res.dropped == []


def test_extract_refs_ignores_comment_lines():
    text = (
        "//NIGHTLY  JOB (ACCT),'DAILY'\n"
        "//* EXEC PGM=SHOULD-NOT-APPEAR\n"
        "//STEP1    EXEC PGM=TAXCALC\n"
    )
    res = A.extract_refs(text, "NIGHTLY.jcl")
    names = {r.name for r in res.refs}
    assert names == {"TAXCALC"}


def test_extract_refs_flags_proc_exec_as_dropped_not_resolved():
    """`EXEC PGM=` でない EXEC（カタログドプロシージャ実行）は解決せず `dropped` に記録する。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC MYPROC\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.refs == []
    assert len(res.dropped) == 1
    d = res.dropped[0]
    assert d.reason == "proc_exec" and d.line == 2 and "MYPROC" in d.snippet


def test_extract_refs_flags_include_member_as_dropped():
    """`// INCLUDE MEMBER=` は解決せず `dropped` に記録する。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n// INCLUDE MEMBER=SHAREDJC\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.refs == []
    assert len(res.dropped) == 1 and res.dropped[0].reason == "include_member"


def test_extract_refs_does_not_double_count_pgm_exec_as_proc_exec():
    """`EXEC PGM=` は通常どおり ref になり、`proc_exec` として二重に dropped へは入らない。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC PGM=TAXCALC\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert {r.name for r in res.refs} == {"TAXCALC"}
    assert res.dropped == []
