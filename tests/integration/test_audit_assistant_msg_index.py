"""`chat.turn` 監査の `message_id_assistant` 部分索引（評価・改善ログの質問との対応付け）が有効に作られ、対応付けの引きに使われること。"""
from __future__ import annotations

import psycopg
import pytest

from sherpa import store
from sherpa.store import db as store_db

_NAME = "idx_audit_chat_turn_assistant_msg"


def _index_valid() -> bool:
    with store._connect() as c:
        row = c.execute("SELECT i.indisvalid FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid "
                        "WHERE c.relname = %s", (_NAME,)).fetchone()
    return bool(row and row["indisvalid"])


def test_audit_assistant_message_index_is_built_idempotently_and_used():
    try:
        store._ensure()
    except Exception:
        pytest.skip("DB down（Postgres 未起動）")
    store_db.ensure_audit_assistant_message_index()
    store_db.ensure_audit_assistant_message_index()
    assert _index_valid()
    with psycopg.connect(store._dsn(), autocommit=True) as c:
        c.execute("SET enable_seqscan = off")
        plan = "\n".join(r[0] for r in c.execute(
            "EXPLAIN SELECT (a.detail->>'message_id_user')::integer FROM audit_log a "
            "WHERE a.action = 'chat.turn' AND (a.detail->>'message_id_assistant')::integer = 1 "
            "ORDER BY a.id DESC LIMIT 1").fetchall())
    assert _NAME in plan, plan
