"""`world_neo4j._node_row()`（Neo4j UNWIND 行の組み立て）の単体テスト（RV波1是正）。

対象: `jcl_kind`（アナライザ拡張 S5a・JCL の `Batch` 種別）が UNWIND 行へそのまま乗ること、
持たないノード（JCL 以外・旧 world 相当）は `None` になること。純関数なので実 Neo4j は不要。
"""
from __future__ import annotations

from sherpa.ingest import world_neo4j as wn


def test_node_row_carries_jcl_kind_when_present():
    n = {"cid": "batch:w:gen1/JOB1.jcl#JOB1", "name": "JOB1", "jcl_kind": "job"}
    row = wn._node_row(n)
    assert row["jcl_kind"] == "job"


def test_node_row_carries_table_schema_and_qualified_name():
    n = {"cid": "table:w:g/a.sql#CUSTOMER", "name": "CUSTOMER", "schema": "A", "qualified_name": "A.CUSTOMER"}
    row = wn._node_row(n)
    assert (row["schema"], row["qualified_name"]) == ("A", "A.CUSTOMER")
    plain = wn._node_row({"cid": "table:w:g/b.sql#X", "name": "X"})
    assert (plain["schema"], plain["qualified_name"]) == (None, None)


def test_node_row_jcl_kind_is_none_when_absent():
    """JCL 以外のノード（例: Module）は `jcl_kind` を持たない——UNWIND 行では null（既存キーの
    追加のみ・無いノードを埋めない）。"""
    n = {"cid": "module:w:pkg/Foo.cbl#FOO", "name": "FOO"}
    row = wn._node_row(n)
    assert row["jcl_kind"] is None
