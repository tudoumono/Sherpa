"""ソースの中の SQL をグラフへ: C の `EXEC SQL`・文字列の SQL・`.pc`・定義の無い表（架空の資料）。"""
from __future__ import annotations

from sherpa.ingest import world_graph

PC = """#include "util.h"
int load(void){
  EXEC SQL SELECT NAME INTO :n FROM CUSTOMER WHERE ID = :i;
  const char *q = "SELECT * FROM ORDERS o "
                  "JOIN ITEMS i ON 1=1";
  const char *d = "DELETE FROM TBL_%s";
  puts("Update failed");
  helper();
  return 0;
}
"""


def _write(root, files):
    for rel, body in files.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body, encoding="utf-8")


def _build(root):
    nodes, edges, flags = world_graph.build_world(root, "w", resolve_config={}, build_marks=None)
    by = {n["cid"]: n for n in nodes}
    return by, edges, flags


def _access(by, edges):
    return sorted((by[e["src"]]["name"], by[e["dst"]]["name"], by[e["dst"]]["path"], e.get("via"))
                  for e in edges if e["type"] == "ACCESSES")


def test_pc_reads_exec_sql_and_string_sql_into_undefined_tables(tmp_path):
    _write(tmp_path, {"sysA/src/load.pc": PC, "sysA/src/util.h": "void helper(void);\n", "sysA/src/helper.c": "void helper(void){}\n"})
    by, edges, flags = _build(tmp_path)
    assert _access(by, edges) == [
        ("load", "CUSTOMER", "sysA", "exec_sql"), ("load", "ITEMS", "sysA", "string_sql"),
        ("load", "ORDERS", "sysA", "string_sql")]
    assert not [f for f in flags if f.get("reason") == "syntax_error"]
    assert not [n for n in by.values() if n["name"] in ("TBL_", "FAILED")]
    undefined = [n for n in by.values() if n["label"] == "Table"]
    assert {n["name"] for n in undefined} == {"CUSTOMER", "ITEMS", "ORDERS"} and all(n["value"] == "undefined" for n in undefined)
    assert any(e["type"] == "INVOKES" and by[e["src"]]["name"] == "load" and by[e["dst"]]["name"] == "helper" for e in edges)


def test_defined_table_wins_over_undefined_and_stays_inside_top_folder(tmp_path):
    _write(tmp_path, {
        "sysA/src/load.pc": PC, "sysA/src/helper.c": "void helper(void){}\n", "sysA/src/util.h": "void helper(void);\n",
        "sysA/db/ddl.sql": "CREATE TABLE CUSTOMER (ID INT, NAME VARCHAR(10));\n",
        "sysB/src/other.pc": "int g(void){ EXEC SQL SELECT 1 FROM CUSTOMER; return 0; }\n",
    })
    by, edges, _flags = _build(tmp_path)
    got = {(by[e["src"]]["path"], by[e["dst"]]["name"], by[e["dst"]]["path"]) for e in edges if e["type"] == "ACCESSES"}
    assert ("sysA/src/load.pc", "CUSTOMER", "sysA/db/ddl.sql") in got
    assert ("sysB/src/other.pc", "CUSTOMER", "sysB") in got
    assert ("sysB/src/other.pc", "CUSTOMER", "sysA/db/ddl.sql") not in got


def test_cobol_exec_sql_without_definition_reaches_an_undefined_table(tmp_path):
    cbl = ("       IDENTIFICATION DIVISION.\n       PROGRAM-ID. PAYX.\n       PROCEDURE DIVISION.\n"
           "           EXEC SQL\n               SELECT A INTO :B FROM LEDGER\n           END-EXEC.\n           STOP RUN.\n")
    _write(tmp_path, {"sysA/src/PAYX.cbl": cbl})
    by, edges, _flags = _build(tmp_path)
    assert [(by[e["src"]]["name"], by[e["dst"]]["name"], e.get("via")) for e in edges if e["type"] == "ACCESSES"] == [("PAYX", "LEDGER", "exec_sql")]


def test_existing_unresolved_table_refs_of_other_languages_stay_unresolved(tmp_path):
    _write(tmp_path, {"sysA/m/Mapper.xml": '<mapper namespace="a.B"><select id="s">SELECT * FROM NOWHERE</select></mapper>\n'})
    by, edges, flags = _build(tmp_path)
    assert not [n for n in by.values() if n["label"] == "Table"]


def test_same_name_undefined_tables_in_two_schemas_are_two_nodes(tmp_path):
    _write(tmp_path, {"sysA/src/a.pc": "int f(void){ EXEC SQL SELECT 1 FROM SALES.EVENTS; EXEC SQL SELECT 1 FROM AUDIT.EVENTS; return 0; }\n"})
    by, edges, _flags = _build(tmp_path)
    tables = [n for n in by.values() if n["label"] == "Table"]
    assert sorted((n["name"], n.get("schema")) for n in tables) == [("EVENTS", "AUDIT"), ("EVENTS", "SALES")]
    assert len({n["cid"] for n in tables}) == 2
    assert len([e for e in edges if e["type"] == "ACCESSES"]) == 2


def test_undefined_table_keeps_every_referencing_source_as_membership(tmp_path):
    _write(tmp_path, {"sysA/src/a.pc": "int f(void){ EXEC SQL SELECT 1 FROM T9; return 0; }\n",
                      "sysA/other/b.pc": "int g(void){ EXEC SQL SELECT 1 FROM T9; return 0; }\n"})
    by, _edges, _flags = _build(tmp_path)
    [t] = [n for n in by.values() if n["label"] == "Table"]
    assert sorted(t["membership_paths"]) == ["sysA/other/b.pc", "sysA/src/a.pc"]
