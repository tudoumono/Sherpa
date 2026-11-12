"""退役した列・表（取り込み元の登録・使わなくなった取り込み記録の列・閉じた AI 提供元の鍵の列）の実 DB テスト。

新しい DB には作らない。旧い DB には起動時の移行（`init_schema`）が、列は値ごと、取り込み元の登録の表は
行が 1 件も無いときだけ撤去する（残っている登録は消さない）。

要 Postgres（`sherpa_test` 分離DB）。DB 不可は SKIP（他の tests/unit/test_*_store_roundtrip.py と同じ流儀）。
"""
from __future__ import annotations

import pytest

from sherpa import store

_RETIRED_COLUMNS = {
    "ingest_runs": ("ingest_source_id", "scan_root", "scope_mapping_overrides", "republished_at"),
    "user_settings": ("gemini_api_key", "bedrock_api_key"),
}


def _try_init() -> None:
    try:
        store.init_schema()
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def _columns(table: str) -> set:
    with store._connect() as c:
        rows = c.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = %s", (table,)).fetchall()
    return {r["column_name"] for r in rows}


def _table_exists(table: str) -> bool:
    with store._connect() as c:
        return c.execute("SELECT to_regclass(%s) AS t", (table,)).fetchone()["t"] is not None


def _create_legacy_sources_table() -> None:
    with store._connect() as c:
        c.execute("CREATE TABLE IF NOT EXISTS ingest_sources (id SERIAL PRIMARY KEY, label TEXT)")


def test_fresh_schema_does_not_have_retired_columns_or_sources_table():
    _try_init()
    for table, retired in _RETIRED_COLUMNS.items():
        assert not (set(retired) & _columns(table)), table
    assert not _table_exists("ingest_sources")


def test_migration_drops_retired_columns_with_their_values():
    _try_init()
    with store._connect() as c:
        c.execute("ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS scan_root TEXT")
        c.execute("ALTER TABLE ingest_runs ADD COLUMN IF NOT EXISTS republished_at TIMESTAMPTZ")
        c.execute("ALTER TABLE user_settings ADD COLUMN IF NOT EXISTS bedrock_api_key TEXT")
    assert {"scan_root", "republished_at"} <= _columns("ingest_runs")
    store.init_schema()
    for table, retired in _RETIRED_COLUMNS.items():
        assert not (set(retired) & _columns(table)), table


def test_migration_keeps_legacy_sources_table_while_rows_remain_and_drops_it_when_empty():
    _try_init()
    _create_legacy_sources_table()
    try:
        with store._connect() as c:
            c.execute("DELETE FROM ingest_sources")
            c.execute("INSERT INTO ingest_sources (label) VALUES ('残っている登録')")
        store.init_schema()
        assert _table_exists("ingest_sources"), "行が残っている表を消してはいけない"
        with store._connect() as c:
            c.execute("DELETE FROM ingest_sources")
        store.init_schema()
        assert not _table_exists("ingest_sources")
    finally:
        with store._connect() as c:
            c.execute("DROP TABLE IF EXISTS ingest_sources")
