"""監査ログの検証（`verify_audit_chain`）が、実行中の監査の書き込みを待たせないこと（実 DB）。

検証の途中（先頭情報を読んだ後）に別接続から監査を書き、外部 API の監査と同じ
`lock_timeout=3s` の内に書き終わることを確かめる。検証が advisory lock を握ったままだと、この書き込みは待たされて失敗する。
"""
from __future__ import annotations

import sys
import threading
import time

import psycopg
import pytest
from psycopg.rows import dict_row

from sherpa import store


def test_audit_write_is_not_blocked_while_verify_is_running(monkeypatch):
    try:
        store._ensure()
    except Exception:
        pytest.skip("DB down（Postgres 未起動）")

    import sherpa.store.audit  # noqa: F401  (sys.modules に確実に載せる)
    audit_mod = sys.modules["sherpa.store.audit"]

    store.audit("u_verify_nb", "test.verify_nb", "test", "r1")
    store.audit("u_verify_nb", "test.verify_nb", "test", "r2")

    in_verify = threading.Event()
    write_done = threading.Event()
    real_connect = audit_mod._connect

    class _Conn:
        """先頭情報を読んだ直後（検証のトランザクションの途中）で、書き込みが終わるまで止める。"""

        def __init__(self, conn):
            self._conn = conn
            self._paused = False

        def execute(self, query, *args, **kwargs):
            cur = self._conn.execute(query, *args, **kwargs)
            if not self._paused and "audit_chain_head" in str(query):
                self._paused = True
                in_verify.set()
                write_done.wait(timeout=20)
            return cur

        def __getattr__(self, name):
            return getattr(self._conn, name)

    class _ConnCM:
        def __enter__(self):
            self._cm = real_connect()
            return _Conn(self._cm.__enter__())

        def __exit__(self, *exc):
            return self._cm.__exit__(*exc)

    monkeypatch.setattr(audit_mod, "_connect", lambda: _ConnCM())

    result: dict = {}
    t = threading.Thread(target=lambda: result.update(store.verify_audit_chain()), daemon=True)
    t.start()
    try:
        assert in_verify.wait(timeout=20), "検証が行の検証まで進まなかった"
        t0 = time.perf_counter()
        with psycopg.connect(store._dsn(), row_factory=dict_row,
                             options="-c lock_timeout=3000 -c statement_timeout=5000") as c:
            store._audit_insert(c, "ext:test", "ext_api.search", "ext_search", None, {"result_count": 1})
        waited = time.perf_counter() - t0
    finally:
        write_done.set()
        t.join(timeout=60)
    assert not t.is_alive(), "検証が終わらない"
    assert waited < 3.0, f"検証中の監査の書き込みが待たされた: {waited:.2f}s"
    assert set(result) == {"ok", "checked", "broken_at", "reason"}
    assert result["checked"] >= 2
