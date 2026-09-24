"""`store.db.world_lock_shared`（共有 advisory lock）の実 PostgreSQL 相互排他検証（要 Postgres）。

PART-4（`sherpa/research_service.py`）が rebind（`sherpa.ingest.worker` の排他 `world_lock`）との
TOCTOU を避けるために使う共有ロックの意味論そのものを、複数コネクションで実測する:

  1. 共有ロック同士は並行できる（research 同士がブロックし合わない）。
  2. 排他ロック（`world_lock`）保持中は共有ロックの取得が待たされる（rebind 中は research が待つ）。
  3. 共有ロック保持中は排他ロックの取得が待たされる（research 中は rebind が待つ）。
  4. `timeout_ms` を超えると `psycopg.errors.LockNotAvailable`（呼び出し元は 503 にする）。

Neo4j は不要（PostgreSQL の advisory lock だけの検証）。
"""
from __future__ import annotations

import threading
import time

import pytest

from sherpa import store
from sherpa.store.db import world_lock, world_lock_shared, world_registry_lock

_WORLD = "test_world_lock_shared_semantics"


def _try_init():
    try:
        store.init_schema()
        return True
    except Exception as e:
        pytest.skip(f"DB down: {e}")


def test_shared_locks_run_concurrently():
    if not _try_init():
        return
    order: list[str] = []

    def hold(tag, dur):
        with world_lock_shared(_WORLD):
            order.append(f"{tag}-in")
            time.sleep(dur)
            order.append(f"{tag}-out")

    t1 = threading.Thread(target=hold, args=("A", 0.3))
    t2 = threading.Thread(target=hold, args=("B", 0.3))
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order.count("A-in") == 1 and order.count("B-in") == 1
    # B が A の解放を待たずに入っていること（並行実行の証拠）。
    assert order.index("B-in") < order.index("A-out"), f"共有ロック同士が直列化されている: {order}"


def test_exclusive_lock_blocks_shared_lock():
    if not _try_init():
        return
    order: list[str] = []

    def hold_exclusive():
        with world_lock(_WORLD):
            order.append("excl-in")
            time.sleep(0.3)
            order.append("excl-out")

    def take_shared():
        with world_lock_shared(_WORLD, timeout_ms=5000):
            order.append("shared-in")

    t1 = threading.Thread(target=hold_exclusive)
    t2 = threading.Thread(target=take_shared)
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order.index("shared-in") > order.index("excl-out"), (
        f"排他ロック保持中に共有ロックが取れてしまっている: {order}")


def test_shared_lock_blocks_exclusive_lock():
    if not _try_init():
        return
    order: list[str] = []

    def hold_shared():
        with world_lock_shared(_WORLD):
            order.append("shared-in")
            time.sleep(0.3)
            order.append("shared-out")

    def take_exclusive():
        with world_lock(_WORLD):
            order.append("excl-in")

    t1 = threading.Thread(target=hold_shared)
    t2 = threading.Thread(target=take_exclusive)
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order.index("excl-in") > order.index("shared-out"), (
        f"共有ロック保持中に排他ロックが取れてしまっている: {order}")


def test_world_lock_does_not_collide_with_world_registry_lock_for_world_id_world_registry():
    """`world_id="world-registry"`（入力規則上は有効な識別子）は `world_registry_lock()` と
    key 空間を分ける——以前は両方とも `sha1(f"{_KB_ID}:world-registry")`
    という同じ1引数形の鍵になっており、この名前の world への取り込み/検索が新規登録の直列化
    ロックと不当に競合しうる自己衝突があった。"""
    if not _try_init():
        return
    order: list[str] = []

    def hold_registry():
        with world_registry_lock():
            order.append("registry-in")
            time.sleep(0.3)
            order.append("registry-out")

    def hold_world_named_registry():
        with world_lock("world-registry"):
            order.append("world-in")
            time.sleep(0.05)
            order.append("world-out")

    t1 = threading.Thread(target=hold_registry)
    t2 = threading.Thread(target=hold_world_named_registry)
    t1.start()
    time.sleep(0.05)
    t2.start()
    t1.join(timeout=5)
    t2.join(timeout=5)
    assert order.index("world-in") < order.index("registry-out"), (
        f"world_lock('world-registry') が world_registry_lock() と衝突している: {order}")


def test_shared_lock_timeout_raises_lock_not_available():
    if not _try_init():
        return
    import psycopg

    def hold_exclusive_long():
        with world_lock(_WORLD):
            time.sleep(1.0)

    t = threading.Thread(target=hold_exclusive_long)
    t.start()
    time.sleep(0.1)
    try:
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with world_lock_shared(_WORLD, timeout_ms=200):
                pass
    finally:
        t.join(timeout=5)


def test_exclusive_lock_timeout_raises_lock_not_available():
    """`world_lock(timeout_ms=...)`（排他ロック同士の競合・短時間ロック〔recount/reconvert 等〕が
    他の排他処理〔rebind/delete 等〕と競合した場合に長時間ブロックせず 409/503 を返せるようにする用途）。
    `timeout_ms` 省略時は無制限に待つ既存呼び出し元の挙動は変えない——ここでは明示的に
    渡した場合の新しい挙動だけを確認する。
    """
    if not _try_init():
        return
    import psycopg

    def hold_exclusive_long():
        with world_lock(_WORLD):
            time.sleep(1.0)

    t = threading.Thread(target=hold_exclusive_long)
    t.start()
    time.sleep(0.1)
    try:
        with pytest.raises(psycopg.errors.LockNotAvailable):
            with world_lock(_WORLD, timeout_ms=200):
                pass
    finally:
        t.join(timeout=5)
