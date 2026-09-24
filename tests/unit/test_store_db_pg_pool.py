"""`store.db` の PG コネクションプール契約（性能台帳#17 QW2・DB 不要）。

`_connect()`（引数無し）が `_get_pg_pool()` 経由でプールを使うこと、`with _connect() as c:`
（大多数の呼び出し元）と素の `conn = _connect(); ...; conn.close()`（一部テスト・
`tests/api/test_system_settings.py`/`test_bedrock_settings.py` の advisory lock 直接検証）の
両方でプールへ正しく返却されること、kwargs 付き呼び出し（`connect_timeout=`/`options=`）は
プールへ触れず ad-hoc `psycopg.connect()` のまま残ること、枯渇時は `PoolTimeout` がそのまま
伝播すること、を偽プール/偽接続で固定する。

`world_lock`/`world_lock_shared`/`world_registry_lock`/`workspace_file_lock`（session-level
advisory lock を持つ・プール対象外）が `_get_pg_pool()` に一切触れないことも固定する
（プールの使い回し接続に session lock を残すと解放後に別リクエストへ漏れるため）。

実際の PostgreSQL に対するプールの生成・接続・枯渇の実地検証は
`tests/integration/`（`ConnectionPool` 自体の正しさは upstream の責務）ではなく、本ファイルは
本リポジトリが `_PooledConnection`/`_connect()` に足した薄いラッパー層のロジックのみを検証する
（唯一の例外: 末尾の GUC 汚染回帰テストは実 PostgreSQL が必要・DB 不到達なら graceful skip）。
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _reset_pg_pool_singleton():
    """本ファイルのテストがプロセス内シングルトン `db._PG_POOL` を汚染しないようにする。

    テスト内で生成したプール（偽 `_make_pg_pool`・実 `ConnectionPool` いずれも）は teardown で
    閉じてから元の状態へ戻す（他のテストファイルが引き続き実 DB プールを使えるようにする）。
    """
    from sherpa.store import db

    saved = db._PG_POOL
    db._PG_POOL = None
    try:
        yield
    finally:
        if db._PG_POOL is not None and db._PG_POOL is not saved:
            try:
                db._PG_POOL.close(timeout=1.0)
            except Exception:
                pass
        db._PG_POOL = saved


class _FakeRealConn:
    """`psycopg.Connection` の with 契約（commit/rollback）を模した偽接続。"""

    def __init__(self):
        self.calls: list[str] = []
        self.executed: list = []

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.calls.append("rollback" if exc_type else "commit")
        return False   # 例外は握り潰さない（元の Connection.__exit__ と同じ契約）

    def execute(self, sql, params=None):
        self.executed.append(sql)
        return self

    def fetchone(self):
        return {"x": 1}


class _FakePool:
    def __init__(self, conn=None):
        self.conn = conn or _FakeRealConn()
        self.putconn_calls: list = []
        self.getconn_calls = 0

    def getconn(self, timeout=None):
        self.getconn_calls += 1
        return self.conn

    def putconn(self, conn):
        self.putconn_calls.append(conn)


# ---- with 利用（大多数の呼び出し元パターン） ----

def test_connect_with_block_binds_real_connection_and_commits_then_returns_to_pool(monkeypatch):
    from sherpa.store import db

    pool = _FakePool()
    monkeypatch.setattr(db, "_get_pg_pool", lambda: pool)

    with db._connect() as c:
        assert c is pool.conn   # with 内で束縛される c は実接続そのもの（ラッパーではない）
        c.execute("select 1")

    assert pool.conn.calls == ["commit"]
    assert pool.putconn_calls == [pool.conn]


def test_connect_with_block_exception_rolls_back_and_still_returns_to_pool(monkeypatch):
    from sherpa.store import db

    pool = _FakePool()
    monkeypatch.setattr(db, "_get_pg_pool", lambda: pool)

    with pytest.raises(ValueError, match="boom"):
        with db._connect() as c:
            raise ValueError("boom")

    assert pool.conn.calls == ["rollback"]
    assert pool.putconn_calls == [pool.conn]   # 例外時も必ずプールへ返す（finally で release）


# ---- 素の呼び出し（with を使わない一部テスト・advisory lock 直接検証） ----

def test_connect_raw_usage_delegates_attributes_and_close_returns_to_pool(monkeypatch):
    from sherpa.store import db

    pool = _FakePool()
    monkeypatch.setattr(db, "_get_pg_pool", lambda: pool)

    conn = db._connect()
    conn.execute("select pg_backend_pid()")
    assert pool.conn.executed == ["select pg_backend_pid()"]   # __getattr__ 委譲

    conn.close()
    assert pool.putconn_calls == [pool.conn]   # 素の Connection.close() ではなく putconn() 経由の返却


def test_connect_raw_usage_release_is_idempotent(monkeypatch):
    """`.close()` を複数回呼んでも `putconn` は1回だけ（二重返却によるプール状態破壊を防ぐ）。"""
    from sherpa.store import db

    pool = _FakePool()
    monkeypatch.setattr(db, "_get_pg_pool", lambda: pool)

    conn = db._connect()
    conn.close()
    conn.close()
    assert len(pool.putconn_calls) == 1


def test_pooled_connection_raises_after_release_instead_of_silent_reuse(monkeypatch):
    """RV代替 M2: 返却済みの `_PooledConnection` へ属性アクセスすると明示例外になる。

    返却後（`close()`／with 終了後）にこのラッパー経由で再度 `.execute()` 等を呼ぶと、その
    物理接続は既に別のプール利用者へ貸し出されている可能性がある——黙って `getattr` を通すと
    別リクエストの接続へ意図せず SQL を流す「静かな成功」になるため、`_conn` への参照を切って
    `PooledConnectionReleasedError` を送出することを固定する。
    """
    from sherpa.store import db

    pool = _FakePool()
    monkeypatch.setattr(db, "_get_pg_pool", lambda: pool)

    conn = db._connect()
    conn.execute("select 1")
    conn.close()

    with pytest.raises(db.PooledConnectionReleasedError):
        conn.execute("select 2")


# ---- kwargs 付き呼び出し（プール非対応の特殊呼び出し・従来どおり ad-hoc connect） ----

def test_connect_with_kwargs_bypasses_pool_entirely(monkeypatch):
    from sherpa.store import db

    def _pool_must_not_be_touched():
        raise AssertionError("kwargs 付き _connect() はプールへ一切触れてはならない")

    monkeypatch.setattr(db, "_get_pg_pool", _pool_must_not_be_touched)
    monkeypatch.setattr(db, "_dsn", lambda: "dsn-for-adhoc")

    seen = {}

    def fake_connect(dsn, **kw):
        seen["dsn"] = dsn
        seen["kw"] = kw
        return _FakeRealConn()

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)

    with db._connect(connect_timeout=3) as c:
        c.execute("select 1")

    assert seen["dsn"] == "dsn-for-adhoc"
    assert seen["kw"].get("connect_timeout") == 3
    assert seen["kw"].get("row_factory") is db.dict_row


def test_connect_with_options_kwarg_also_bypasses_pool(monkeypatch):
    """`api_keys.py` の call-count 集計（`options=f"-c statement_timeout=..."`）と同型の呼び出し。"""
    from sherpa.store import db

    monkeypatch.setattr(db, "_get_pg_pool",
                        lambda: (_ for _ in ()).throw(AssertionError("プールへ触れてはならない")))
    seen = {}

    def fake_connect(dsn, **kw):
        seen["kw"] = kw
        return _FakeRealConn()

    monkeypatch.setattr(db.psycopg, "connect", fake_connect)
    with db._connect(options="-c statement_timeout=500") as c:
        pass
    assert seen["kw"].get("options") == "-c statement_timeout=500"


# ---- 枯渇時の明示エラー ----

def test_connect_propagates_pool_timeout_without_swallowing(monkeypatch):
    from sherpa.store import db
    import psycopg_pool

    class _ExhaustedPool:
        def getconn(self, timeout=None):
            raise psycopg_pool.PoolTimeout("couldn't get a connection after 10.00 sec")

    monkeypatch.setattr(db, "_get_pg_pool", lambda: _ExhaustedPool())

    with pytest.raises(psycopg_pool.PoolTimeout):
        db._connect()


# ---- プール生成・シングルトン・DSN 捕捉（テスト DB 分離／レーン DB 切替の土台） ----

def test_make_pg_pool_captures_dsn_and_size_env_at_creation_time(monkeypatch):
    """プールは生成時点の `_dsn()`/env サイズ設定を捕まえる——`tests/conftest.py::_setup_test_pg_dsn`
    は「どの `_connect()` 呼び出しよりも先」に `SHERPA_PG_DSN` を書き換えるため、遅延生成される
    プールは常にテスト用 DSN（`sherpa_test`／レーン別 DSN）を正しく捕まえる（本体側の docstring
    `_get_pg_pool` 参照）。ここでは生成時点の値が反映されることだけを最小コストで固定する
    （実際に到達可能である必要はない・`min_size=0` で背景接続を起こさない）。
    """
    from sherpa.store import db

    monkeypatch.setattr(db, "_dsn", lambda: "host=lane-db port=5 dbname=lanetest user=u password=p")
    monkeypatch.setenv("SHERPA_PG_POOL_MIN", "0")
    monkeypatch.setenv("SHERPA_PG_POOL_MAX", "1")
    monkeypatch.setenv("SHERPA_PG_POOL_TIMEOUT", "0.2")

    pool = db._make_pg_pool()
    try:
        assert "lane-db" in pool.conninfo
        assert pool.timeout == 0.2
    finally:
        pool.close(timeout=1.0)


def test_get_pg_pool_is_lazy_singleton(monkeypatch):
    from sherpa.store import db

    calls = []

    class _StubPool:
        def close(self, timeout=5.0):
            pass

    def fake_make():
        calls.append(1)
        return _StubPool()

    monkeypatch.setattr(db, "_make_pg_pool", fake_make)
    p1 = db._get_pg_pool()
    p2 = db._get_pg_pool()
    assert p1 is p2
    assert len(calls) == 1


def test_close_pg_pool_resets_singleton_and_is_idempotent():
    from sherpa.store import db

    closed = []

    class _StubPool:
        def close(self, timeout=5.0):
            closed.append(timeout)

    db._PG_POOL = _StubPool()
    db.close_pg_pool()
    assert closed == [5.0]
    assert db._PG_POOL is None

    db.close_pg_pool()   # 未生成状態での二重呼び出しは安全（何もしない）
    assert closed == [5.0]


# ---- advisory lock（session-level）を持つ経路はプール対象外 ----

def test_advisory_lock_helpers_never_touch_pg_pool(monkeypatch):
    """`world_lock`/`world_lock_shared`/`world_registry_lock`/`workspace_file_lock` は
    session-level advisory lock を専用接続で保持する——プールの使い回し接続にこれを残すと、
    解放後に別リクエストが同じ物理接続を受け取ってしまい lock が意図せず漏れる。この4関数が
    `_get_pg_pool()` に一切触れないことを固定する（既存の `psycopg.connect` 直接呼び出しは
    `test_world_lock_shared_guard.py`/`test_world_lock_unlock_guard.py` が別途検証済み）。
    """
    from sherpa.store import db

    class _FakeAdvisoryConn:
        def execute(self, sql, params=None):
            return self

        def close(self):
            pass

    monkeypatch.setattr(db, "_ensure", lambda: None)
    monkeypatch.setattr(db.psycopg, "connect", lambda dsn, **kw: _FakeAdvisoryConn())

    def _pool_must_not_be_touched():
        raise AssertionError("advisory lock 経路はプールへ一切触れてはならない")

    monkeypatch.setattr(db, "_get_pg_pool", _pool_must_not_be_touched)

    with db.world_lock("w1"):
        pass
    with db.world_lock_shared("w1"):
        pass
    with db.world_registry_lock():
        pass
    with db.workspace_file_lock("u1", "a/b.txt"):
        pass


# ---- H1是正の回帰: statement_timeout_ms のみ（connect_timeout 無し＝プール経由）でも
#      返却後の接続に GUC が残らない（実 PostgreSQL 必要・DB 不到達なら graceful skip）----

def test_smt_only_call_does_not_leak_statement_timeout_into_pooled_connection(monkeypatch):
    """`connect_timeout` 無し・`statement_timeout_ms` のみを渡す呼び出し
    （`store.settings._read_system_settings_fresh` 等・`connect_kwargs={}` となりプール経由で
    接続を取得する）で `SET LOCAL`（`SET` ではなく）を使っていることの回帰固定。

    素の `SET statement_timeout` のままだと、その接続がプールへ返却された後も session-level の
    GUC が残り、次にこの物理接続を借りた無関係な呼び出しへ意図しない短い statement_timeout を
    漏らす（GUC 汚染）。プールを1本固定（min=max=1）にして同一の物理接続が確実に使い回される
    ことを保証したうえで、smt-only 呼び出しの前後で `SHOW statement_timeout` が変化していない
    ことを確認する。
    """
    import psycopg

    from sherpa.store import db
    from sherpa.store import settings as settings_mod

    try:
        with db._connect(connect_timeout=5) as probe_conn:
            probe_conn.execute("SELECT 1")
    except psycopg.OperationalError as e:
        pytest.skip(f"DB down: {e}")

    monkeypatch.setenv("SHERPA_PG_POOL_MIN", "1")
    monkeypatch.setenv("SHERPA_PG_POOL_MAX", "1")

    with db._connect() as c0:
        pid0 = c0.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        baseline = next(iter(c0.execute("SHOW statement_timeout").fetchone().values()))

    settings_mod._read_system_settings_fresh(connect_timeout=None, statement_timeout_ms=50)

    with db._connect() as c2:
        pid2 = c2.execute("SELECT pg_backend_pid() AS pid").fetchone()["pid"]
        after = next(iter(c2.execute("SHOW statement_timeout").fetchone().values()))

    assert pid2 == pid0, "テスト前提が崩れている（同一物理接続の使い回しを確認できなかった）"
    assert after == baseline, (
        f"smt-only 呼び出し後、プールへ返却された接続に statement_timeout が残っている"
        f"（baseline={baseline!r} after={after!r}）"
    )
