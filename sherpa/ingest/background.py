"""取り込み run の背景実行（資料フォルダに触れる登録・更新・削除などの操作で共通）。

プロセス内レジストリ＋daemon thread で実行し、進捗は `ingest_runs.progress`（DB）へ書く。
資料フォルダ単位で「今どの run_id・どの操作を実行中か」だけを覚え、同じ操作・同じ payload の
再要求は実行中の run へ合流し、別の操作は `ConflictError`（呼び出し側が 409 へ変換）にする。
設計: docs/design/rag.md「更新と削除」
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

_log = logging.getLogger("sherpa")


class ConflictError(Exception):
    """同じ world で操作種別／payload が異なる run が実行中（呼び出し側は 409 へ変換する）。"""

    def __init__(self, world_id: str, existing_op: str, existing_run_id: int):
        self.world_id = world_id
        self.existing_op = existing_op
        self.existing_run_id = existing_run_id
        super().__init__(
            f"別の処理が実行中です（world={world_id} 実行中の操作={existing_op} run_id={existing_run_id}）")


class ShuttingDownError(Exception):
    """アプリ終了処理中で新規の背景実行を受け付けない（呼び出し側は 503 へ変換する）。"""


@dataclass
class _BgRun:
    world_id: str
    op: str
    fingerprint: str
    run_id: int
    done: bool = False


_REGISTRY: dict[str, _BgRun] = {}
_REGISTRY_LOCK = threading.Lock()
_accepting = True   # lifespan shutdown が False にする（新規受付停止）


def stop_accepting() -> None:
    """新規の背景実行受付を止める（lifespan shutdown 専用）。実行中のスレッドは止めない。

    `start_or_join` の受理判定と同じ `_REGISTRY_LOCK` で直列化する。
    """
    global _accepting
    with _REGISTRY_LOCK:
        _accepting = False


def start_accepting() -> None:
    """`stop_accepting()` を取り消す（テスト専用）。"""
    global _accepting
    with _REGISTRY_LOCK:
        _accepting = True


def drain(timeout: float = 30.0) -> None:
    """レジストリが空になる（実行中の背景スレッドが無くなる）まで待つ（lifespan shutdown 専用）。"""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        with _REGISTRY_LOCK:
            if not _REGISTRY:
                return
        time.sleep(0.1)


def start_or_join(world_id: str, op: str, fingerprint: str, create_run: Callable[[], int],
                  work_fn: Callable[[int], None], *, extra_keys: tuple[str, ...] = ()
                  ) -> tuple[int, bool]:
    """資料フォルダ単位の単一実行。

    ① 実行中の run があり `op`/`fingerprint` が一致 → 合流して `(既存run_id, True)` を返す。
       不一致 → `ConflictError`。
    ② 実行中でなければ `create_run()` で run_id を確保し、レジストリへ登録して `work_fn(run_id)` を
       daemon thread で起動し `(新規run_id, False)` を返す。

    `extra_keys` は同じ実行を追加登録する別名キー（資料フォルダ行が出現する前後どちらのキーで
    来ても実行中を検出するため）。実行中判定は全キーを見て、完了時は全キーから外す。
    受付確認・実行中判定・`create_run()`・登録は同一の `_REGISTRY_LOCK` 区間で行う。
    アプリ終了処理中は `ShuttingDownError`（呼び出し側が 503 へ変換）。
    """
    keys = (world_id,) + tuple(k for k in extra_keys if k != world_id)
    with _REGISTRY_LOCK:
        if not _accepting:
            raise ShuttingDownError("シャットダウン中のため新規の取り込みは受け付けられません")
        for key in keys:
            existing = _REGISTRY.get(key)
            if existing is not None and not existing.done:
                if existing.op == op and existing.fingerprint == fingerprint:
                    return existing.run_id, True
                raise ConflictError(key, existing.op, existing.run_id)
        run_id = create_run()
        bg = _BgRun(world_id=world_id, op=op, fingerprint=fingerprint, run_id=run_id)
        for key in keys:
            _REGISTRY[key] = bg

    def _runner():
        try:
            work_fn(run_id)
        except Exception:
            _log.warning("背景実行が未捕捉の例外で終了しました: world=%s op=%s run_id=%s",
                        world_id, op, run_id, exc_info=True)
        finally:
            # work_fn が terminal 化しなかった行（status='extracting' のまま）だけ failed へ落とす
            try:
                from .. import store, webhooks
                if store.fail_close_if_extracting(
                        run_id, reason="background_worker_exited_without_terminal_status"):
                    _log.warning(
                        "背景実行が run を terminal 化せずに終了したため failed へ格下げしました: "
                        "world=%s op=%s run_id=%s", world_id, op, run_id)
                    # この格下げも terminal 化なので Webhook 通知する
                    try:
                        webhooks.notify_run_terminal(world_id, run_id, op, "failed")
                    except Exception:
                        _log.warning(
                            "Webhook 通知の起動に失敗しました（best-effort）: world=%s run_id=%s",
                            world_id, run_id, exc_info=True)
            except Exception:
                _log.warning("最外周の failed 格下げ自体に失敗しました（best-effort）: "
                            "world=%s run_id=%s", world_id, run_id, exc_info=True)
            bg.done = True
            with _REGISTRY_LOCK:
                for key in keys:
                    if _REGISTRY.get(key) is bg:   # 別の新規実行に既に差し替わっていたら消さない
                        _REGISTRY.pop(key, None)

    threading.Thread(target=_runner, daemon=True, name=f"sherpa-ingest-{world_id}").start()
    return run_id, False


def is_running(world_id: str) -> bool:
    """この world の背景実行がプロセス内レジストリ上で「実行中」かどうか（テスト/診断用）。"""
    with _REGISTRY_LOCK:
        bg = _REGISTRY.get(world_id)
        return bg is not None and not bg.done
