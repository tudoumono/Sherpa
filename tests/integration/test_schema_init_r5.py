"""R5: schema 初期化の直列化＋記録専用 schema_version（2026-07-13-横断レビュー対応.md §3 R5）。

`store.init_schema()` を `pg_advisory_lock` で直列化した効果を、複数 **OS プロセス**が同時に
呼んでも例外なく完了することで固定する（advisory lock はプロセス間で有効。スレッドでは
「別プロセスの起動」という本番シナリオの再現力が弱いため、指示どおり subprocess を使う）。

理想は「まっさらな状態」からの競争（DDL 未適用の状態で複数プロセスが同時に `CREATE TABLE`/
`ALTER TABLE` 系へ突入する方が、既存スキーマへの再実行より衝突の再現力が高い）。接続ロールに
`CREATEDB` があれば使い捨てのスクラッチ DB（`CREATE DATABASE` → 子プロセスへ `SHERPA_PG_DSN`
として渡す → 完了後に `DROP DATABASE`）で確認する。`tests/conftest.py`（ルート）の
`_setup_test_pg_dsn()` が元 DSN（`SHERPA_ORIG_PG_DSN`）の接続で毎回 `sherpa_test` を
`CREATE DATABASE` しており、そのロールに `CREATEDB` があることは既に実証済み。
それでも権限不足等で作成に失敗した場合は、既存のテスト DB（`sherpa_test`）に対する並行実行に
フォールバックする（この場合「まっさらな状態からの競争」自体は再現できず、「同時実行しても
例外を出さない」ことのみを固定する＝再現力の限界。以下 `scratch_dsn` fixture 参照）。

DB 不到達（Postgres 自体が落ちている）は他の integration/api テストと同じ流儀で graceful SKIP。
"""
from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
import time

import psycopg
import pytest
from psycopg import conninfo as _ci

from sherpa import store
from sherpa.store import db as store_db

ROOT = pathlib.Path(__file__).resolve().parents[2]

_CHILD_SRC = (
    "import sys; sys.path.insert(0, {root!r}); "
    "from sherpa import store; store.init_schema(); "
    "print('OK', store.schema_ready())"
)


def _admin_dsn() -> str:
    """CREATEDB 権限を持つことが実証済みの接続（ルート conftest 参照）。"""
    return os.environ.get("SHERPA_ORIG_PG_DSN") or store._dsn()


def _spawn_child(dsn: str) -> subprocess.Popen:
    env = dict(os.environ)
    env["SHERPA_PG_DSN"] = dsn
    return subprocess.Popen(
        [sys.executable, "-c", _CHILD_SRC.format(root=str(ROOT))],
        env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
    )


@pytest.fixture
def scratch_dsn():
    """使い捨てスクラッチ DB の DSN を返す（`CREATEDB` 不可なら既存テスト DB にフォールバック）。

    戻り値は `(dsn, is_scratch)`。`is_scratch=True` なら本当にまっさらな DB（DDL 未適用）。
    """
    admin_dsn = _admin_dsn()
    try:
        admin = psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5)
    except Exception as e:
        pytest.skip(f"infra down: {e}")
        return

    scratch_name = f"sherpa_test_r5_schema_{os.getpid()}"
    created = False
    try:
        try:
            admin.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')   # 前回の残骸（異常終了時）を掃除
            admin.execute(f'CREATE DATABASE "{scratch_name}"')
            created = True
        except Exception:
            pass   # CREATEDB 不可等 → フォールバック
    finally:
        admin.close()

    if created:
        dsn = _ci.make_conninfo(**{**_ci.conninfo_to_dict(admin_dsn), "dbname": scratch_name})
        try:
            yield dsn, True
        finally:
            try:
                with psycopg.connect(admin_dsn, autocommit=True, connect_timeout=5) as c:
                    c.execute(f'DROP DATABASE IF EXISTS "{scratch_name}"')
            except Exception:
                pass
    else:
        yield store._dsn(), False   # フォールバック: 現行テスト DB（sherpa_test）を共有で使う


def test_concurrent_init_schema_no_exception(scratch_dsn):
    """4 プロセス同時 `init_schema()` が全プロセス例外なく完了する（R5 受け入れ①）。

    `scratch_dsn` がまっさらな DB を用意できた場合、DDL 未適用状態から4プロセスが同時に
    突入する＝advisory lock 直列化が無ければ `CREATE TABLE`/`ALTER TABLE`/`DO $$ ... CHECK`
    系の競合で deadlock/エラーが起きやすい状況を意図的に作る。
    """
    dsn, _is_scratch = scratch_dsn
    procs = [_spawn_child(dsn) for _ in range(4)]
    results = []
    try:
        for p in procs:
            out, err = p.communicate(timeout=90)
            results.append((p.returncode, out, err))
    except subprocess.TimeoutExpired:
        # RV LOW（2026-07-15）: timeout で例外脱出すると残りの子が advisory lock / scratch DB 接続を
        # 保持し続け、fixture の DROP DATABASE も失敗して残骸化する。必ず全子プロセスを回収してから fail。
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.communicate()
        pytest.fail("init_schema の子プロセスが 90 秒で完了しなかった（deadlock 回帰の疑い）")
    for rc, out, err in results:
        assert rc == 0, f"init_schema が子プロセスで失敗しました: rc={rc}\nstdout={out}\nstderr={err}"
        assert "OK True" in out, f"schema_ready() が True になっていません: {out}"
    # deadlock 等の痕跡が無いこと（stderr にも出ない）を明示的に確認する。
    for _rc, _out, err in results:
        assert "deadlock" not in err.lower()


def _try_init() -> bool:
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"infra down: {e}")
        return False


def test_init_schema_migrates_legacy_db_without_schema_version_table():
    """`schema_version` が無い既存 DB（R5 以前相当）からの起動でも正常に再作成・スタンプされる。"""
    if not _try_init():
        return
    with store._connect() as c:
        c.execute("DROP TABLE IF EXISTS schema_version")
    store.init_schema()
    with store._connect() as c:
        row = c.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()
    assert row["n"] >= 1, "schema_version が再作成・スタンプされていません"


def test_init_schema_stamp_idempotent_no_duplicate_rows():
    """`init_schema()` を2回呼んでも `schema_version` の行数が増えない（記録専用スタンプの冪等性）。"""
    if not _try_init():
        return
    with store._connect() as c:
        before = c.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    store.init_schema()
    with store._connect() as c:
        after = c.execute("SELECT COUNT(*) AS n FROM schema_version").fetchone()["n"]
    assert after == before, f"schema_version の行数が増えた: before={before} after={after}"
    assert store_db._SCHEMA_HASH   # モジュール定数が定義されていること（回帰の目印）


# ===== PERF-1（台帳#17）: idx_messages_created_at の CONCURRENTLY 作成 =====

def _index_row(relname: str):
    with store._connect() as c:
        return c.execute(
            "SELECT i.indisvalid, ix.indexdef FROM pg_index i "
            "JOIN pg_class c ON c.oid = i.indexrelid "
            "JOIN pg_indexes ix ON ix.indexname = c.relname "
            "WHERE c.relname = %s",
            (relname,),
        ).fetchone()


def test_created_at_index_lock_does_not_collide_with_world_lock(monkeypatch):
    """`ensure_messages_created_at_index()` の advisory lock（2引数形・`_CREATED_AT_INDEX_LOCK_CLASSID`/
    `_CREATED_AT_INDEX_LOCK_KEY`）は `world_lock()` の1引数形の鍵空間と別名前空間であり、
    world_id が偶然 "idx_messages_created_at" と一致する `world_lock` を保持していても
    索引構築側の advisory lock は取得できる（衝突していれば取れずスキップされる）ことを
    直接確認する。

    `_try_init()`（`init_schema()`）が起動するバックグラウンドの索引構築スレッドは、この
    テストが検証したいのと同じ advisory lock を実運用の目的でも取得しにいく。放置すると、
    そのスレッドがロックを保持している最中にこのテストの `pg_try_advisory_lock` が競合し、
    「world_lock との衝突」ではなく単なるタイミングの偶然で失敗しうる（かつこのテストが
    先にロックを奪うと本来の索引構築がスキップされる副作用も生む）。バックグラウンド起動
    そのものを無効化してから検証することでこの競争を排除する。
    """
    monkeypatch.setattr(store_db, "_ensure_messages_created_at_index_background", lambda: None)
    if not _try_init():
        return
    with store_db.world_lock("idx_messages_created_at"):
        with store._connect() as c:
            row = c.execute(
                "SELECT pg_try_advisory_lock(%s, %s) AS got",
                (store_db._CREATED_AT_INDEX_LOCK_CLASSID, store_db._CREATED_AT_INDEX_LOCK_KEY),
            ).fetchone()
            got = row["got"]
            if got:
                c.execute(
                    "SELECT pg_advisory_unlock(%s, %s)",
                    (store_db._CREATED_AT_INDEX_LOCK_CLASSID, store_db._CREATED_AT_INDEX_LOCK_KEY),
                )
    assert got, (
        "world_lock(\"idx_messages_created_at\") 保持中に索引構築の advisory lock が"
        "取得できなかった（鍵空間が衝突している疑い）"
    )


def test_init_schema_does_not_wait_for_created_at_index_build():
    """`idx_messages_created_at` が `_SCHEMA`（起動の単一トランザクション）から外れていること、
    及びバックグラウンドで有効に構築されることを確認する（正常系）。"""
    if not _try_init():
        return
    assert not any("idx_messages_created_at" in stmt for stmt in store_db._SCHEMA), (
        "idx_messages_created_at が _SCHEMA（起動の単一トランザクション）に含まれている"
        "（CONCURRENTLY 構築へ移行したはず）"
    )
    store.init_schema()
    assert store.schema_ready() is True, "init_schema() 呼び出し直後に readiness が確定していない"
    # バックグラウンドスレッドが構築を終えるまで短時間だけポーリングする（テストDB規模なら数百ms〜数秒）。
    deadline = time.time() + 15
    row = None
    while time.time() < deadline:
        row = _index_row("idx_messages_created_at")
        if row is not None and row["indisvalid"]:
            break
        time.sleep(0.2)
    assert row is not None and row["indisvalid"], (
        f"idx_messages_created_at がバックグラウンドで有効に構築されなかった: {row}"
    )


def test_init_schema_does_not_block_on_created_at_index_build(monkeypatch):
    """`init_schema()` が `idx_messages_created_at` の構築完了を待たない（別 daemon スレッドで
    起動するだけ・join しない）ことを、構築処理自体を人為的にブロックさせて確認する。

    実際の CONCURRENTLY 構築時間や共有DBのデータ量に依存するタイミング計測（「大量行を投入して
    構築が遅くなるのを実測する」）は、実行環境や他レーンの負荷でフレーキーになりやすく、かつ
    確認のためだけに大量データを共有DBへ投入することになる。代わりに
    `ensure_messages_created_at_index` を「呼ばれたら合図を立てて待機し続ける」スタブへ差し替え、
    `init_schema()` がそれでも短時間で返ることを直接確認する（「thread 起動の事実＋非 join」を
    構造的に固定する）。
    """
    if not _try_init():
        return
    import threading

    build_started = threading.Event()
    release_build = threading.Event()

    def _blocking_ensure() -> None:
        build_started.set()
        release_build.wait(timeout=10)   # テスト側が明示的に release するまで動かない（=構築中を模す）

    monkeypatch.setattr(store_db, "ensure_messages_created_at_index", _blocking_ensure)
    # 他テストで既に一度スレッドが起動済み（プロセス生存期間で1回のガード）だと今回は
    # 起動されないため、フラグを明示的に落として今回も起動させる。
    monkeypatch.setattr(store_db, "_created_at_index_thread_started", False)

    try:
        t0 = time.time()
        store.init_schema()
        elapsed = time.time() - t0

        assert store.schema_ready() is True
        assert elapsed < 2.0, (
            f"init_schema() が idx_messages_created_at の構築完了を待ってブロックした: "
            f"elapsed={elapsed:.3f}s"
        )
        assert build_started.wait(timeout=5), (
            "バックグラウンドスレッドが ensure_messages_created_at_index を呼んでいない"
        )
    finally:
        release_build.set()   # ブロック中のスタブ呼び出しを終わらせてスレッドを回収する


def test_ensure_messages_created_at_index_is_idempotent():
    """`ensure_messages_created_at_index()` を複数回呼んでも例外にならず、索引は1つのまま有効。"""
    if not _try_init():
        return
    store_db.ensure_messages_created_at_index()
    store_db.ensure_messages_created_at_index()
    row = _index_row("idx_messages_created_at")
    assert row is not None and row["indisvalid"]
    with store._connect() as c:
        n = c.execute(
            "SELECT COUNT(*) AS n FROM pg_class WHERE relname = 'idx_messages_created_at'"
        ).fetchone()["n"]
    assert n == 1, f"idx_messages_created_at が複数存在する: {n}"


def test_ensure_messages_created_at_index_repairs_invalid_index():
    """CONCURRENTLY 構築が失敗して INVALID な索引が残った状態から、次回呼び出しで
    DROP → 作り直しにより有効な索引へ自己修復する（superuser 権限不要の再現手順:
    重複値のある列に UNIQUE INDEX CONCURRENTLY を試みて意図的に失敗させ、INVALID を残す）。
    """
    if not _try_init():
        return
    conv = None
    try:
        with store._connect() as c:
            c.execute("DROP INDEX IF EXISTS idx_messages_created_at")
        # 重複する created_at を持つ行を最低2件用意する（UNIQUE 制約違反で CONCURRENTLY が失敗する）。
        conv = store.create_conversation(user_id="admin", world="r5-idx-repair")
        dup_ts_msgs = [
            store.add_message(conv["id"], "user", "dup-a"),
            store.add_message(conv["id"], "user", "dup-b"),
        ]
        with psycopg.connect(store._dsn()) as c:
            c.execute(
                "UPDATE messages SET created_at = '2020-01-01T00:00:00Z' WHERE id = ANY(%s)",
                ([m["id"] for m in dup_ts_msgs],),
            )
        with psycopg.connect(store._dsn(), autocommit=True) as c:
            with pytest.raises(Exception):
                c.execute(
                    "CREATE UNIQUE INDEX CONCURRENTLY idx_messages_created_at "
                    "ON messages (created_at)"
                )
        row = _index_row("idx_messages_created_at")
        assert row is not None and not row["indisvalid"], (
            f"意図した INVALID 索引の再現に失敗した（前提が崩れている）: {row}"
        )

        store_db.ensure_messages_created_at_index()

        row = _index_row("idx_messages_created_at")
        assert row is not None and row["indisvalid"], f"INVALID 索引から自己修復しなかった: {row}"
        assert "UNIQUE" not in (row["indexdef"] or "").upper(), (
            "作り直し後も UNIQUE のまま（本来の非 UNIQUE 定義に戻っていない）"
        )
    finally:
        # DROP をこの try に含めた（セットアップ含め try/finally 内）ため、途中のどこで失敗しても
        # 索引は必ずここで復元を試みる（共有索引・後続テストへの影響防止）。
        if conv is not None:
            with store._connect() as c:
                c.execute("DELETE FROM conversations WHERE id=%s", (conv["id"],))
        store_db.ensure_messages_created_at_index()   # 通常の索引状態へ戻す



def test_ensure_messages_created_at_index_skips_when_same_name_different_definition(caplog):
    """`idx_messages_created_at` という名前で想定（messages(created_at)）と異なる定義の索引が
    既に有効に存在する場合、DROP/CREATE を一切行わず警告ログを残してスキップする
    （運用者判断に委ねる・db.py の ensure_messages_created_at_index() 契約コメント参照）。
    セットアップ（DROP・別定義索引の作成）も try/finally 内に含め、途中失敗時も
    共有索引を確実に復元する。"""
    if not _try_init():
        return
    try:
        with store._connect() as c:
            c.execute("DROP INDEX IF EXISTS idx_messages_created_at")
        # 想定と異なる定義（conversation_id への単純索引）を同じ名前で作る。
        with psycopg.connect(store._dsn(), autocommit=True) as c:
            c.execute("CREATE INDEX idx_messages_created_at ON messages (conversation_id)")

        with caplog.at_level(logging.WARNING, logger="sherpa"):
            store_db.ensure_messages_created_at_index()

        row = _index_row("idx_messages_created_at")
        assert row is not None and row["indisvalid"], f"既存の別定義索引が壊れた: {row}"
        assert "conversation_id" in (row["indexdef"] or ""), (
            f"既存の別定義索引が想定の定義へ書き換わってしまった（DROP/CREATE が実行された疑い）: {row}"
        )
        assert any("異なります" in rec.message for rec in caplog.records), (
            "定義不一致の警告ログが出ていない"
        )
    finally:
        with store._connect() as c:
            c.execute("DROP INDEX IF EXISTS idx_messages_created_at")
        store_db.ensure_messages_created_at_index()   # 通常の索引状態へ戻す
