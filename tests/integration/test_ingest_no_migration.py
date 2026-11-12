"""取り込みの番号の列が付く前の DB を起動時の移行で開いても、既存の行が番号 0 になり、2 回目の起動でも変わらないこと。

直す前のスキーマ（`tests/fixtures/schema/before_ingest_no.sql`＝`pg_dump --schema-only` の出力）に行を入れてから移行する。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from sherpa.store import db, replace_rows

FIXTURE = Path(__file__).resolve().parents[1] / "fixtures" / "schema" / "before_ingest_no.sql"
NEW_COLUMNS = {
    "worlds": ["shown_ingest_no"],
    "documents": ["ingest_no", "doc_ingest_no"],
    "name_ledger": ["ingest_no"],
    "name_ledger_built": ["ingest_no"],
}


def _columns(c, table):
    return {r[0] for r in c.execute(
        "SELECT column_name FROM information_schema.columns WHERE table_name=%s", (table,)).fetchall()}


@pytest.fixture
def old_schema_dsn(monkeypatch):
    base = db._dsn()
    name = f"sherpa_ingest_no_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(base, autocommit=True, connect_timeout=3)
    except Exception as exc:
        pytest.skip(f"PostgreSQL に接続できない: {exc.__class__.__name__}")
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
        dsn = psycopg.conninfo.make_conninfo(base, dbname=name)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute(FIXTURE.read_text(encoding="utf-8"))
        monkeypatch.setenv("SHERPA_PG_DSN", dsn)
        yield dsn
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()


def _init(monkeypatch):
    monkeypatch.setattr(db, "_inited", False)
    db.init_schema()


def test_existing_rows_get_ingest_no_zero_and_rerun_is_stable(old_schema_dsn, monkeypatch):
    with psycopg.connect(old_schema_dsn, autocommit=True) as c:
        for table, cols in NEW_COLUMNS.items():
            assert not (set(cols) & _columns(c, table)), f"移行前の {table} に新しい列がある"
        c.execute("INSERT INTO worlds (world_id, root_path) VALUES ('w1', '/mnt/w1')")
        c.execute("INSERT INTO documents (version, name) VALUES ('w1', 'DOC_A'), ('w1', 'DOC_B')")
        c.execute("INSERT INTO name_ledger (world, name, source, rel_path) VALUES ('w1', 'ALPHA', 'filename', 'a/ALPHA.cbl')")
        c.execute("INSERT INTO name_ledger_built (world) VALUES ('w1')")

    def snapshot():
        with psycopg.connect(old_schema_dsn) as c:
            return {
                "worlds": c.execute("SELECT shown_ingest_no FROM worlds").fetchall(),
                "documents": c.execute("SELECT ingest_no, doc_ingest_no FROM documents ORDER BY name").fetchall(),
                "name_ledger": c.execute("SELECT ingest_no FROM name_ledger").fetchall(),
                "built": c.execute("SELECT ingest_no FROM name_ledger_built").fetchall(),
                "replacements": c.execute("SELECT count(*) FROM ingest_replacements").fetchone(),
            }

    _init(monkeypatch)
    first = snapshot()
    assert first == {
        "worlds": [(0,)], "documents": [(0, 0), (0, 0)], "name_ledger": [(0,)], "built": [(0,)],
        "replacements": (0,),
    }
    _init(monkeypatch)
    assert snapshot() == first


def test_next_ingest_no_increases_from_one(old_schema_dsn, monkeypatch):
    _init(monkeypatch)
    with psycopg.connect(old_schema_dsn, autocommit=True, row_factory=psycopg.rows.dict_row) as c:
        a = replace_rows.next_ingest_no(c)
        b = replace_rows.next_ingest_no(c)
    assert (a, b) == (1, 2)


def test_migration_lets_two_numbers_hold_the_same_name_and_keeps_old_rows(old_schema_dsn, monkeypatch):
    with psycopg.connect(old_schema_dsn, autocommit=True) as c:
        c.execute("INSERT INTO documents (version, name) VALUES ('w1', 'DOC_A')")
        c.execute("INSERT INTO name_ledger_built (world) VALUES ('w1')")
    _init(monkeypatch)
    _init(monkeypatch)
    with psycopg.connect(old_schema_dsn, autocommit=True) as c:
        c.execute("INSERT INTO documents (version, name, ingest_no) VALUES ('w1', 'DOC_A', 3)")
        c.execute("INSERT INTO name_ledger_built (world, ingest_no) VALUES ('w1', 3)")
        with pytest.raises(psycopg.errors.UniqueViolation):
            c.execute("INSERT INTO documents (version, name, ingest_no) VALUES ('w1', 'DOC_A', 3)")
        assert c.execute("SELECT ingest_no FROM documents ORDER BY ingest_no").fetchall() == [(0,), (3,)]
        assert c.execute("SELECT ingest_no FROM name_ledger_built ORDER BY ingest_no").fetchall() == [(0,), (3,)]
        assert c.execute("SELECT 1 FROM pg_indexes WHERE indexname='doc_ver_no_name_prefix'").fetchone()
