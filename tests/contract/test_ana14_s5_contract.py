"""ANA-14 S5（SQL の schema の区別）の受入契約。

`fixtures/corpus/ana-s5` を `build_world()` に通し、Table ノード・ACCESSES の辺・申告（`flags`）を完全一致で比べる。
"""
from __future__ import annotations

import pathlib

import pytest

from sherpa.ingest import world_graph

ROOT = pathlib.Path(__file__).resolve().parents[2]
WORLD_DIR = ROOT / "fixtures" / "corpus" / "ana-s5"
P = "ana-s5:g/"


@pytest.fixture(scope="module")
def built():
    return world_graph.build_world(WORLD_DIR, "ana-s5")


def test_same_name_tables_in_two_schemas_are_both_nodes_and_cid_of_the_first_is_unchanged(built):
    nodes, _edges, _flags = built
    tables = {n["cid"]: (n["name"], n.get("schema"), n.get("qualified_name"))
              for n in nodes if n["label"] == "Table"}
    assert tables == {
        f"table:{P}sql/ledger.sql#LEDGER": ("LEDGER", "A", "A.LEDGER"),
        f"table:{P}sql/plain.sql#X": ("X", None, None),
        f"table:{P}sql/mixed.sql#M": ("M", None, None),
        f"table:{P}sql/mixed.sql#S.M": ("M", "S", "S.M"),
        f"table:{P}sql/quoted.sql#T.X": ("T.X", None, None),   # 引用符の中の `.` は schema 区切りではない
        f"table:{P}sql/three.sql#TT": ("TT", "S3", "S3.TT"),   # 3 部名は先頭の DB を捨てる
        f"table:{P}sql/qonly.sql#Y": ("Y", "Q", "Q.Y"),
        f"table:{P}sql/schemas.sql#CUSTOMER": ("CUSTOMER", "A", "A.CUSTOMER"),    # 1 件目の cid は schema を含まない
        f"table:{P}sql/schemas.sql#B.CUSTOMER": ("CUSTOMER", "B", "B.CUSTOMER"),  # 2 件目は別の識別子・表示名は同じ
    }
    columns = sorted(n["cid"] for n in nodes if n["label"] == "DataItem")
    assert columns == sorted([
        f"dataitem:{P}sql/mixed.sql#M.ID",
        f"dataitem:{P}sql/mixed.sql#S.M.ID",
        f"dataitem:{P}sql/quoted.sql#T.X.ID",
        f"dataitem:{P}sql/three.sql#TT.ID",
        f"dataitem:{P}sql/ledger.sql#LEDGER.ID",
        f"dataitem:{P}sql/plain.sql#X.ID",
        f"dataitem:{P}sql/qonly.sql#Y.ID",
        f"dataitem:{P}sql/schemas.sql#B.CUSTOMER.ID",
        f"dataitem:{P}sql/schemas.sql#B.CUSTOMER.REGION",
        f"dataitem:{P}sql/schemas.sql#CUSTOMER.ID",
        f"dataitem:{P}sql/schemas.sql#CUSTOMER.NAME",
    ])


def test_accesses_follow_the_written_schema_and_never_cross_to_another(built):
    _nodes, edges, _flags = built
    accesses = sorted((e["src"], e["dst"], e["via"]) for e in edges if e["type"] == "ACCESSES")
    assert accesses == sorted([
        (f"config:{P}mybatis/CustMapper.xml#CustMapper.xml", f"table:{P}sql/schemas.sql#B.CUSTOMER", "mapper_sql"),
        (f"module:{P}cobol/LEDGPG.cbl#LEDGPG", f"table:{P}sql/ledger.sql#LEDGER", "exec_sql"),   # 1 つなら修飾なしでも今どおり
        (f"module:{P}vba/modQual.bas#MODQUAL.LOADB", f"table:{P}sql/schemas.sql#B.CUSTOMER", "vba_sql"),
        (f"module:{P}cobol/QUALB.cbl#QUALB", f"table:{P}sql/schemas.sql#B.CUSTOMER", "exec_sql"),
        (f"module:{P}cobol/QUALX.cbl#QUALX", f"table:{P}sql/plain.sql#X", "exec_sql"),  # schema 無しの DDL へ（修飾つき参照）
        (f"module:{P}cobol/QUOTED.cbl#QUOTED", f"table:{P}sql/quoted.sql#T.X", "exec_sql"),
        (f"module:{P}cobol/THREEPG.cbl#THREEPG", f"table:{P}sql/three.sql#TT", "exec_sql"),
    ])


def test_missing_schema_is_unresolved_and_unqualified_name_with_two_schemas_is_ambiguous(built):
    _nodes, _edges, flags = built
    reported = sorted((f["reason"], f.get("from"), f.get("name"), f.get("why"))
                      for f in flags if f["reason"] != "mention_ambiguous_names")
    assert reported == sorted([
        ("ambiguous", "g/cobol/MIXPG.cbl", "M", None),   # schema 無し 1 件＋schema 付き 1 件
        ("ambiguous", "g/cobol/PLAINPG.cbl", "CUSTOMER", None),
        ("dropped_syntax", "g/mybatis/DynMapper.xml", None, "xml:mybatis: mapper_sql_dynamic_table"),   # `${schema}.CUSTOMER`
        ("dropped_syntax", "g/sql/three.sql", None, "table_name_unsupported"),   # 4 部名の DDL
        ("unresolved", "g/cobol/FOURPG.cbl", "A.B.C.D", None),   # 4 部名の参照
        ("unresolved_qualifier", "g/mybatis/DynMapper.xml", "demo.DynMapper", None),   # mapper の namespace の Java 型が無い（S3）
        ("dropped_syntax", "g/sql/schemas.sql", None, "table_name_collision"),   # 同じ schema＋名前の重複
        ("dropped_syntax", "g/sql/schemas.sql", None, "table_name_collision"),   # schema の無い同名（cid を分けられない）
        ("unresolved", "g/cobol/QUALC.cbl", "C.CUSTOMER", None),
        ("unresolved", "g/vba/modQual.bas", "Z.CUSTOMER", None),   # VBA の SQL 文字列＝schema Z は無い
        ("unresolved", "g/cobol/QUALY.cbl", "P.Y", None),   # schema Q の Y だけ＝別 schema へは張らない
        ("unresolved_qualifier", "g/mybatis/CustMapper.xml", "demo.CustMapper", None),   # mapper の namespace の Java 型が無い（S3）
    ])
