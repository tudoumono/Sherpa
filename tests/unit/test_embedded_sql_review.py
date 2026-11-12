"""ソースの中の SQL を読む部品の取りこぼし・誤読の再現（文字列の中の EXEC SQL・SQL ではない案内の文字列・動的な表名）。"""
from __future__ import annotations

from sherpa.ingest.analyzers import _embedded_sql as e
from sherpa.ingest.analyzers.c import CAnalyzer


def _names(src):
    _code, refs, dropped = e.read_embedded_sql(src)
    return [r.name for r in refs], [d.reason for d in dropped]


def test_exec_sql_inside_a_string_does_not_blank_the_following_code():
    src = 'int f(void){ puts("EXEC SQL failed"); helper(); return 0; }\n'
    code, refs, _d = e.read_embedded_sql(src)
    assert code == src and not refs
    calls = [r.name for r in CAnalyzer().extract_refs(src, "a.c").refs if r.extra.get("via") == "call"]
    assert "helper" in calls


def test_exec_sql_in_code_after_a_string_with_the_same_words_still_reads():
    src = 'puts("EXEC SQL"); EXEC SQL SELECT 1 FROM T1;\n'
    assert _names(src)[0] == ["T1"]


def test_dynamic_table_name_with_spaced_qualifier_is_dropped_not_registered():
    names, dropped = _names('q = "SELECT * FROM SALES . TBL_%s";\n')
    assert names == [] and dropped == ["string_sql_dynamic_table"]


def test_prose_string_does_not_become_a_table_reference():
    assert _names('puts("SELECT from the dropdown");\n') == ([], [])
    assert _names('puts("Update the settings to continue");\n') == ([], [])


def test_real_statement_shapes_still_read():
    src = ('a = "INSERT INTO T1 (A) VALUES (1)"; b = "UPDATE T2 SET A = 1"; c = "DELETE FROM T3 WHERE A = 1"; '
           'd = "MERGE INTO T4 USING T5 ON 1=1"; f = "SELECT A, B FROM T6 x JOIN T7 y ON 1=1";\n')
    assert _names(src)[0] == ["T1", "T2", "T3", "T4", "T6", "T7"]


def test_compact_and_aliased_statements_still_read():
    src = ('a = "SELECT* FROM T1"; b = "UPDATE T2 SET (A,B)=(1,2)"; c = "DELETE FROM T3 t WHERE A=1"; '
           'd = "UPDATE T4 u SET A = 1"; f = "DELETE FROM T5 AS x WHERE A=1";\n')
    assert _names(src)[0] == ["T1", "T2", "T3", "T4", "T5"]


def test_ordinary_sql_with_subquery_with_and_join_still_reads():
    src = ('a = "SELECT A FROM (SELECT B FROM T6) x JOIN T7 y ON x.B = y.B WHERE A IN (SELECT C FROM T8)"; '
           'b = "WITH c AS (SELECT 1 FROM T9) SELECT * FROM c JOIN T10 ON 1=1";\n')
    assert _names(src)[0] == ["T6", "T7", "T8", "T9", "T10"]


def test_prose_with_alias_like_shapes_is_still_not_sql():
    assert _names('puts("Delete from the list"); puts("Update the settings to continue");\n') == ([], [])



def test_qualified_dynamic_table_names_are_dropped():
    for q in ("SELECT * FROM SALES.%s", "SELECT * FROM SALES . %s", "SELECT * FROM SALES."):
        names, dropped = _names(f'q = "{q}";\n')
        assert names == [] and dropped == ["string_sql_dynamic_table"], q


def test_open_paren_alone_is_not_a_tuple_assignment():
    assert _names('puts("Update the data set (optional)");\n') == ([], [])
    assert _names('q = "UPDATE T2 SET (A,B)=(1,2)";\n')[0] == ["T2"]
