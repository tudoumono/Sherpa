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
    assert res.primary.extra == {"jcl_kind": "job"}
    assert res.children == []


def test_collect_defs_ignores_job_in_comment_line_and_falls_back_to_include():
    """JOB がコメント行に書かれていても JOB とは認識しない——PROC 文も無いので
    ファイル名ステムの `Batch`（`jcl_kind="include"`）へフォールバックする。"""
    text = "//* NIGHTLY JOB (ACCT)\n//STEP1    EXEC PGM=TAXCALC\n"
    res = A.collect_defs(text, "案件A/x.jcl")
    assert res.primary is not None
    assert res.primary.label == "Batch" and res.primary.name == "X"
    assert res.primary.extra == {"jcl_kind": "include"}


def test_collect_defs_named_proc_without_job_is_batch():
    text = "//STEPPROC PROC\n//STEP1    EXEC PGM=PGMA\n"
    res = A.collect_defs(text, "案件A/STEPPROC.jcl")
    assert res.primary is not None
    assert res.primary.label == "Batch" and res.primary.name == "STEPPROC"
    assert res.primary.extra == {"jcl_kind": "proc"}


def test_collect_defs_unnamed_proc_falls_back_to_file_stem():
    text = "// PROC\n//STEP1    EXEC PGM=PGMA\n"
    res = A.collect_defs(text, "案件A/STEP2PROC.jcl")
    assert res.primary is not None
    assert res.primary.label == "Batch" and res.primary.name == "STEP2PROC"
    assert res.primary.extra == {"jcl_kind": "proc"}


def test_collect_defs_include_fragment_without_job_or_proc_is_batch_from_stem():
    """JOB も PROC 文も無い JCL 断片（INCLUDE 対象）はファイル名ステムの `Batch` になる。"""
    text = "//STEPX    EXEC PGM=PGMC\n"
    res = A.collect_defs(text, "案件A/COMMON.jcl")
    assert res.primary is not None
    assert res.primary.label == "Batch" and res.primary.name == "COMMON"
    assert res.primary.extra == {"jcl_kind": "include"}


def test_collect_defs_job_wins_over_instream_proc():
    """JOB を持つファイル内のインストリーム PROC（`//name PROC` 〜 `// PEND`）は展開せず、
    主体はあくまで JOB 側の `Batch` のまま（インストリーム PROC 名を主体にしない）。"""
    text = (
        "//NIGHTLY  JOB (ACCT),'DAILY'\n"
        "//INNERPRC PROC\n"
        "//STEP1    EXEC PGM=TAXCALC\n"
        "// PEND\n"
        "//STEP2    EXEC PGM=BILLGEN\n"
    )
    res = A.collect_defs(text, "案件A/NIGHTLY.jcl")
    assert res.primary is not None
    assert res.primary.label == "Batch" and res.primary.name == "NIGHTLY"
    assert res.primary.extra == {"jcl_kind": "job"}


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


def test_extract_refs_exec_proc_equals_form_is_batch_invokes():
    """`EXEC PROC=x`（カタログドプロシージャ実行）は `Batch` への `INVOKES`（`via=exec_proc`）。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC PROC=MYPROC\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.dropped == []
    assert len(res.refs) == 1
    r = res.refs[0]
    assert (r.edge_type, r.kind, r.name) == ("INVOKES", "Batch", "MYPROC")
    assert r.extra == {"via": "exec_proc"}


def test_extract_refs_exec_bare_name_form_is_batch_invokes():
    """`PROC=` 接頭辞の無い `EXEC x`（カタログドプロシージャ実行の簡略形）も同じ扱い。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC MYPROC\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.dropped == []
    assert len(res.refs) == 1
    r = res.refs[0]
    assert (r.edge_type, r.kind, r.name) == ("INVOKES", "Batch", "MYPROC")
    assert r.extra == {"via": "exec_proc"}


def test_extract_refs_exec_proc_strips_trailing_params():
    """`EXEC PROC=X,PARM='Y'` のように空白無しでパラメータが続く場合、対象名だけを拾う。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC PROC=MYPROC,PARM='Y'\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert {r.name for r in res.refs} == {"MYPROC"}


def test_extract_refs_flags_symbolic_exec_target_as_dropped():
    """EXEC の対象がシンボリックパラメータ（`&NAME`）は静的に解決先を特定できないため dropped。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC &PROCNAME\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.refs == []
    assert len(res.dropped) == 1
    d = res.dropped[0]
    assert d.reason == "proc_symbolic" and d.line == 2 and "PROCNAME" in d.snippet


def test_extract_refs_include_member_is_batch_invokes():
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n// INCLUDE MEMBER=SHAREDJC\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.dropped == []
    assert len(res.refs) == 1
    r = res.refs[0]
    assert (r.edge_type, r.kind, r.name) == ("INVOKES", "Batch", "SHAREDJC")
    assert r.extra == {"via": "include_member"}


def test_extract_refs_flags_include_without_member_as_dropped():
    """`INCLUDE` はあるが `MEMBER=` を伴わない未対応形は黙って消さず dropped に記録する。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n// INCLUDE SOMETHING-ELSE\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.refs == []
    assert len(res.dropped) == 1 and res.dropped[0].reason == "include_unparsed"


def test_extract_refs_does_not_double_count_pgm_exec_as_proc_exec():
    """`EXEC PGM=` は通常どおり ref になり、`exec_proc` として二重には拾わない。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC PGM=TAXCALC\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert {r.name for r in res.refs} == {"TAXCALC"}
    assert res.dropped == []


def test_extract_refs_exec_with_omitted_name_field_is_still_detected():
    """名前欄（ステップ名）を省略した `// EXEC ...` も検出する（`\\S+` だと名前欄なしを
    取りこぼしていた）。`// EXEC MYPROC` は Batch(MYPROC)（via=exec_proc）、
    `// EXEC PGM=PGMA` は Module(PGMA) の INVOKES になる。"""
    text = (
        "//NIGHTLY  JOB (ACCT),'DAILY'\n"
        "//         EXEC MYPROC\n"
        "//         EXEC PGM=PGMA\n"
    )
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.dropped == []
    got = {(r.edge_type, r.kind, r.name, r.extra.get("via")) for r in res.refs}
    assert got == {
        ("INVOKES", "Batch", "MYPROC", "exec_proc"),
        ("INVOKES", "Module", "PGMA", None),
    }


def test_extract_refs_flags_symbolic_pgm_as_dropped_not_batch_ref():
    """`EXEC PGM=&PROGRAM`（シンボリックな PGM 指定）は Batch 参照に誤変換せず、
    `pgm_symbolic` として dropped に記録する。"""
    text = "//NIGHTLY  JOB (ACCT),'DAILY'\n//STEP1    EXEC PGM=&PROGRAM\n"
    res = A.extract_refs(text, "NIGHTLY.jcl")
    assert res.refs == []
    assert len(res.dropped) == 1
    d = res.dropped[0]
    assert d.reason == "pgm_symbolic" and d.line == 2 and "PROGRAM" in d.snippet


def test_extract_refs_instream_proc_exec_pgm_unchanged():
    """JOB 内のインストリーム PROC（`//name PROC` 〜 `// PEND`）の中の `EXEC PGM=` も
    従来どおり JOB の Batch からの INVOKES として拾う（展開せず挙動不変）。"""
    text = (
        "//NIGHTLY  JOB (ACCT),'DAILY'\n"
        "//INNERPRC PROC\n"
        "//STEP1    EXEC PGM=TAXCALC\n"
        "// PEND\n"
        "//STEP2    EXEC PGM=BILLGEN\n"
    )
    res = A.extract_refs(text, "NIGHTLY.jcl")
    got = {(r.edge_type, r.kind, r.name) for r in res.refs}
    assert got == {("INVOKES", "Module", "TAXCALC"), ("INVOKES", "Module", "BILLGEN")}
    assert res.dropped == []
