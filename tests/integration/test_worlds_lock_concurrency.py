"""R3-S3: worlds.rebind/delete の world_lock 直列化＋自己デッドロック非再発（要 Postgres＋Neo4j）。

`worlds.rebind`/`worlds.delete` は外側で `store.world_lock` を**1回だけ**取り、内側では lock-free の
`worker._run_locked`/`worker._wipe_locked` を直接呼ぶ（`worker.run`/`worker.wipe_world` は各自ロックを
取るため、外側で既に保持している状態から呼ぶと session-level advisory lock は**別コネクション再入不可**
＝自己デッドロックする）。

ここでは実際の `worker.run`/`worlds.rebind`/`worlds.delete` を**別スレッド**で走らせ、`store.replace_documents`
（`_run_locked`/`_wipe_locked` の双方が world_lock 保持区間の終盤に必ず1回通る共通の差し込み点）を最初の
1呼び出しだけ意図的に足止めして「先着が world_lock を保持している最中」を確実に作る。後着はその間
ブロックされ（`join(timeout=短)` で alive を確認）、足止め解除後に直列実行され、最終状態
（台帳/Neo4j/world registry/派生dir）が後着の結果と整合することを固定する。

自己デッドロック regression（rebind/delete が内側で `run`/`wipe_world` を再度呼んでしまう）が入ると、
後着スレッドは world_lock 待ちのまま**永久に**戻らない。`join` は必ずタイムアウト付きで呼び、
テスト自体がハングしないことを担保する（後片付けも同様に bounded）。
"""
from __future__ import annotations

import pathlib
import shutil
import tempfile
import threading

from _world_setup import driver

from sherpa import store, worlds
from sherpa.ingest import worker

W = "test_world_lock_concurrency"

_SHORT = 0.5     # 「まだブロックされているはず」の確認用（短い）
_LONG = 20.0     # 「完了しているはず」の確認用（ES/Neo4j 往復込みの余裕）


from _corpus_helpers import _mk


def _names(drv, wid=W):
    with drv.session() as s:
        return sorted(r["n"] for r in s.run(
            "MATCH (x:Entity {world_id:$w}) RETURN x.name AS n", w=wid))


class _Stall:
    """`store.replace_documents` の**最初の1回だけ**呼び出し中に足止めする。

    `_run_locked`（run/rebind 経路）・`_wipe_locked`（delete 経路）はどちらも world_lock 保持区間の終盤で
    必ずこれを1回呼ぶ＝「先着がロックを保持している最中」を作る共通の差し込み点として使い回せる。
    """

    def __init__(self):
        self.acquired = threading.Event()   # 足止めに入った（＝先着がロックを保持中）
        self.release_ = threading.Event()   # 続行してよい合図
        self._n = 0
        self._orig = store.replace_documents

        def _stalling(world, rows):
            self._n += 1
            if self._n == 1:
                self.acquired.set()
                self.release_.wait(timeout=_LONG)
            return self._orig(world, rows)

        store.replace_documents = _stalling

    def restore(self):
        store.replace_documents = self._orig


def _threaded(fn, *a, **kw):
    box = {}

    def _run():
        try:
            box["result"] = fn(*a, **kw)
        except Exception as e:                    # 後段で box["error"] を確認できるよう捕捉（スレッド内例外は握りつぶされるため）
            box["error"] = e

    th = threading.Thread(target=_run, daemon=True)
    return th, box


def _safe_cleanup(paths, drv, wid=W):
    """後片付けも bounded（自己デッドロック regression 時に finally 自体がハングしない防御）。"""
    done = threading.Event()

    def _try_delete():
        try:
            worlds.delete(wid)
        except Exception:
            pass
        finally:
            done.set()

    th = threading.Thread(target=_try_delete, daemon=True)
    th.start()
    th.join(timeout=_LONG)
    if not done.is_set():                          # world_lock 経由の削除がハング＝lock 非依存の直接クリーンアップへ
        try:
            store.delete_world_row(wid)
        except Exception:
            pass
    with drv.session() as s:
        s.run("MATCH (n:Entity {world_id:$w}) DETACH DELETE n", w=wid)
    drv.close()
    for p in paths:
        shutil.rmtree(p, ignore_errors=True)


def test_run_then_rebind_serializes_and_ends_consistent():
    """先着 run が world_lock 保持中、後着 rebind はブロックされ、run 完了後に直列実行される。"""
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    _mk(a, "案件X", "RUNFOO")
    _mk(b, "案件Y", "REBOUND")
    b_resolved = pathlib.Path(b).resolve()
    drv = driver()
    stall = None
    try:
        worlds.register(W, str(pathlib.Path(a).resolve()))
        assert _names(drv) == ["RUNFOO"]

        stall = _Stall()
        th_run, box_run = _threaded(worker.run, W, reflect=True)
        th_run.start()
        assert stall.acquired.wait(timeout=10), "run が world_lock 保持区間（replace_documents）まで進まない"

        th_rebind, box_rebind = _threaded(worlds.rebind, W, str(b_resolved))
        th_rebind.start()
        th_rebind.join(timeout=_SHORT)
        assert th_rebind.is_alive(), "rebind が先着 run の world_lock 保持中にブロックされていない"

        stall.release_.set()                       # run を完了させる（world_lock を解放）
        th_run.join(timeout=_LONG)
        assert not th_run.is_alive(), "run が完了しない（自己デッドロックの疑い）"
        assert "error" not in box_run, box_run.get("error")

        th_rebind.join(timeout=_LONG)
        assert not th_rebind.is_alive(), "rebind が world_lock 解放後も完了しない（自己デッドロックの疑い）"
        assert "error" not in box_rebind, box_rebind.get("error")

        # 最終状態＝後着 rebind の結果に整合（台帳/Neo4j/world registry/派生dir）
        assert worlds.world_dir(W) == b_resolved
        assert _names(drv) == ["REBOUND"]
        assert {d["name"] for d in store.list_documents(W)} == {"案件Y/03_開発/01_ソース/REBOUND.cbl"}
        der = worlds.derived_dir(W)
        assert der.exists()
        assert not der.with_name("." + der.name + ".rebind-bak").exists()   # 成功＝退避バックアップは破棄済み
    finally:
        if stall is not None:
            stall.restore()
        _safe_cleanup([a, b], drv)


def test_run_then_delete_serializes_and_ends_consistent():
    """先着 run が world_lock 保持中、後着 delete はブロックされ、run 完了後に直列実行される。"""
    a = tempfile.mkdtemp()
    _mk(a, "案件X", "RUNBAZ")
    drv = driver()
    stall = None
    try:
        worlds.register(W, str(pathlib.Path(a).resolve()))
        assert _names(drv) == ["RUNBAZ"]

        stall = _Stall()
        th_run, box_run = _threaded(worker.run, W, reflect=True)
        th_run.start()
        assert stall.acquired.wait(timeout=10), "run が world_lock 保持区間（replace_documents）まで進まない"

        th_delete, box_delete = _threaded(worlds.delete, W)
        th_delete.start()
        th_delete.join(timeout=_SHORT)
        assert th_delete.is_alive(), "delete が先着 run の world_lock 保持中にブロックされていない"

        stall.release_.set()
        th_run.join(timeout=_LONG)
        assert not th_run.is_alive(), "run が完了しない（自己デッドロックの疑い）"
        assert "error" not in box_run, box_run.get("error")

        th_delete.join(timeout=_LONG)
        assert not th_delete.is_alive(), "delete が world_lock 解放後も完了しない（自己デッドロックの疑い）"
        assert "error" not in box_delete, box_delete.get("error")
        assert box_delete.get("result") is True

        assert store.get_world(W) is None
        assert _names(drv) == []
        assert store.list_documents(W) == []
        assert not worlds.derived_dir(W).exists()
    finally:
        if stall is not None:
            stall.restore()
        _safe_cleanup([a], drv)


def test_rebind_then_delete_serializes_and_ends_consistent():
    """先着 rebind が world_lock 保持中、後着 delete はブロックされ、rebind 完了後に直列実行される。"""
    a, b = tempfile.mkdtemp(), tempfile.mkdtemp()
    _mk(a, "案件X", "REBFOO")
    _mk(b, "案件Y", "REBBAR")
    b_resolved = pathlib.Path(b).resolve()
    drv = driver()
    stall = None
    try:
        worlds.register(W, str(pathlib.Path(a).resolve()))
        assert _names(drv) == ["REBFOO"]

        stall = _Stall()
        th_rebind, box_rebind = _threaded(worlds.rebind, W, str(b_resolved))
        th_rebind.start()
        assert stall.acquired.wait(timeout=10), "rebind が world_lock 保持区間（replace_documents）まで進まない"

        th_delete, box_delete = _threaded(worlds.delete, W)
        th_delete.start()
        th_delete.join(timeout=_SHORT)
        assert th_delete.is_alive(), "delete が先着 rebind の world_lock 保持中にブロックされていない"

        stall.release_.set()                        # rebind を完了させる（b へ切替＋world_lock 解放）
        th_rebind.join(timeout=_LONG)
        assert not th_rebind.is_alive(), "rebind が完了しない（自己デッドロックの疑い）"
        assert "error" not in box_rebind, box_rebind.get("error")

        th_delete.join(timeout=_LONG)
        assert not th_delete.is_alive(), "delete が world_lock 解放後も完了しない（自己デッドロックの疑い）"
        assert "error" not in box_delete, box_delete.get("error")
        assert box_delete.get("result") is True

        # rebind が b へ切替を完了した**後**に delete が効いた＝world 自体が消える
        assert store.get_world(W) is None
        assert _names(drv) == []
        assert store.list_documents(W) == []
        assert not worlds.derived_dir(W).exists()
    finally:
        if stall is not None:
            stall.restore()
        _safe_cleanup([a, b], drv)
