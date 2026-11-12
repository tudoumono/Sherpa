"""名前の対応（`name_rules`）のグラフ解決: 未解決の参照だけを、置き換えた目標の形に一致する定義全部へつなぐ。曖昧・決まり無しは今のまま。"""
from __future__ import annotations

from sherpa.ingest import world_graph

WID = "namerules_test"
_CALLER = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. ZZMAIN1.\n       PROCEDURE DIVISION.\n           CALL 'ZZAPROG0'.\n           STOP RUN.\n"
_CALLEE = "       IDENTIFICATION DIVISION.\n       PROGRAM-ID. ZZAPROG1.\n       PROCEDURE DIVISION.\n           STOP RUN.\n"
_RULES = {"name_rules": [{"from": "*0", "to": "*1"}]}


def _write(root, rel, text):
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")


def _calls(root, cfg=None):
    nodes, edges, flags = world_graph.build_world(root, WID, resolve_config=cfg)
    by = {n["cid"]: n["path"] for n in nodes}
    got = [(by[e["src"]], by[e["dst"]], [s.get("rule") for s in e.get("sources", [])]) for e in edges if e["type"] == "INVOKES"]
    return got, [f for f in flags if f.get("reason") in ("ambiguous", "unresolved", "cross_scope")]


def test_name_rule_connects_a_reference_to_the_renamed_definition(tmp_path):
    _write(tmp_path, "G/src/ZZMAIN1.cbl", _CALLER)
    _write(tmp_path, "G/src/ZZAPROG1.cbl", _CALLEE)
    got, flags = _calls(tmp_path)
    assert got == [] and [f["reason"] for f in flags] == ["unresolved"]
    got, flags = _calls(tmp_path, _RULES)
    assert got == [("G/src/ZZMAIN1.cbl", "G/src/ZZAPROG1.cbl", ["name_rule_setting"])] and flags == []


def test_name_rule_is_not_used_for_ambiguous_definitions(tmp_path):
    _write(tmp_path, "G/src/ZZMAIN1.cbl", _CALLER)
    _write(tmp_path, "G/a/ZZAPROG0.cbl", _CALLEE.replace("ZZAPROG1", "ZZAPROG0"))
    _write(tmp_path, "G/b/ZZAPROG0.cbl", _CALLEE.replace("ZZAPROG1", "ZZAPROG0"))
    _write(tmp_path, "G/src/ZZAPROG1.cbl", _CALLEE)
    got, flags = _calls(tmp_path, _RULES)
    assert got == [] and [f["reason"] for f in flags] == ["ambiguous"]


_JCL = "//ZZJOB1  JOB\n//STEP1   EXEC PGM=ZZAPROG00\n"
_C = "int main(void) { return 0; }\n"
_MULTI = {"name_rules": [{"from": "*00", "to": "*0?"}]}


def _execs(root, cfg=None):
    nodes, edges, flags = world_graph.build_world(root, WID, resolve_config=cfg)
    by = {n["cid"]: n["path"] for n in nodes}
    got = sorted((by[e["dst"]], [s.get("rule") for s in e.get("sources", [])]) for e in edges
                 if by[e["src"]] == "G/job/ZZJOB1.jcl")
    return got, [f["reason"] for f in flags if f.get("reason") in ("ambiguous", "unresolved", "cross_scope")]


def _jcl_world(root):
    _write(root, "G/job/ZZJOB1.jcl", _JCL)
    for n in ("ZZAPROG01", "ZZAPROG02", "ZZBPROG01"):
        _write(root, f"G/src/{n}.c", _C)


def test_name_rule_with_question_mark_connects_one_reference_to_many_sources(tmp_path):
    _jcl_world(tmp_path)
    got, flags = _execs(tmp_path)
    assert got == [] and flags == ["unresolved"]
    got, flags = _execs(tmp_path, _MULTI)
    assert got == [("G/src/ZZAPROG01.c", ["name_rule_setting"]), ("G/src/ZZAPROG02.c", ["name_rule_setting"])] and flags == []


def test_name_rule_with_question_mark_does_not_cross_the_top_folder(tmp_path):
    _write(tmp_path, "G/job/ZZJOB1.jcl", _JCL)
    _write(tmp_path, "H/src/ZZAPROG01.c", _C)
    got, flags = _execs(tmp_path, _MULTI)
    assert got == [] and flags == ["unresolved"]


def test_name_rule_with_too_many_matches_is_reported_ambiguous(tmp_path):
    _write(tmp_path, "G/job/ZZJOB1.jcl", _JCL)
    for i in range(21):
        _write(tmp_path, f"G/src/ZZAPROG0{i:02d}.c", _C)
    got, flags = _execs(tmp_path, {"name_rules": [{"from": "*00", "to": "*0??"}]})
    assert got == [] and flags == ["ambiguous"]
    for p in sorted((tmp_path / "G/src").glob("*.c"))[20:]:        # 20 本なら張る
        p.unlink()
    got, flags = _execs(tmp_path, {"name_rules": [{"from": "*00", "to": "*0??"}]})
    assert len(got) == 20 and flags == []


def test_name_rule_does_not_connect_to_qualified_child_definitions(tmp_path):
    """目標の形に拡張子を書いても、ソースの中の関数（修飾名の子の定義）へは張らない。"""
    _write(tmp_path, "G/job/ZZJOB1.jcl", _JCL)
    _write(tmp_path, "G/src/ZZAPROG01.c", "int main(void) { return 0; }\n")
    got, flags = _execs(tmp_path, {"name_rules": [{"from": "*00", "to": "*01.c"}]})
    assert got == [("G/src/ZZAPROG01.c", ["name_rule_setting"])] and flags == []
