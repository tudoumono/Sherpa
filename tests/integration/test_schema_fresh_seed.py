"""新しい PostgreSQL（docker-compose の初回起動）でスキーマの初期化が通ること。

初回起動では scripts/seed_admin.sql が email と role だけの users 表を先に作るため、
アプリ側の CREATE TABLE IF NOT EXISTS users は何もせず、残りの列は ALTER で足す必要がある。
"""
from __future__ import annotations

import uuid
from pathlib import Path

import psycopg
import pytest

from sherpa.store import db

ROOT = Path(__file__).resolve().parents[2]


def test_init_schema_succeeds_after_compose_seed_on_fresh_database(monkeypatch):
    base = db._dsn()
    name = f"sherpa_fresh_seed_{uuid.uuid4().hex[:8]}"
    try:
        admin = psycopg.connect(base, autocommit=True, connect_timeout=3)
    except Exception as exc:
        pytest.skip(f"PostgreSQL に接続できない: {exc.__class__.__name__}")
    try:
        admin.execute(f'CREATE DATABASE "{name}"')
        dsn = psycopg.conninfo.make_conninfo(base, dbname=name)
        with psycopg.connect(dsn, autocommit=True) as c:
            c.execute((ROOT / "scripts" / "seed_admin.sql").read_text(encoding="utf-8"))
        monkeypatch.setenv("SHERPA_PG_DSN", dsn)
        monkeypatch.setattr(db, "_inited", False)
        db.init_schema()
        with psycopg.connect(dsn) as c:
            cols = {r[0] for r in c.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name='users'").fetchall()}
            admin_uid = c.execute("SELECT uid FROM users WHERE email='admin@sherpa.local'").fetchone()
        assert {"uid", "display_name", "password_hash", "status", "must_change_password",
                "last_login_at", "updated_at"} <= cols
        assert admin_uid is not None and admin_uid[0] == "admin"
    finally:
        admin.execute(f'DROP DATABASE IF EXISTS "{name}" WITH (FORCE)')
        admin.close()
