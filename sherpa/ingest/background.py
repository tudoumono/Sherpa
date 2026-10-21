"""取り込み run の依頼の受付（Web アプリ側）と、取り込みワーカー側の停止要求・処理中ファイルの記録。

Web アプリは取り込みを動かさない。資料フォルダに触れる操作（登録・更新・削除など）は `submit` が
`ingest_runs` へ `queued` の 1 行（操作名＋引数）を積むだけで、実行は取り込みワーカー
（`sherpa.ingest.ingest_worker`）が拾って行う。進捗はワーカーが `ingest_runs.progress`（DB）へ書く。
同じ資料フォルダに queued／extracting の run があるとき、同じ操作・同じ引数の再要求はその run へ合流し、
別の操作は `ConflictError`（呼び出し側が 409 へ変換）にする。判定と積む処理は DB の advisory lock で直列化する。
設計: docs/design/operations.md「取り込みワーカーと版の照合」・docs/design/rag.md「更新と削除」
"""
from __future__ import annotations

import contextlib
import logging
import threading
import time

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
    """アプリ終了処理中で新規の依頼を受け付けない（呼び出し側は 503 へ変換する）。"""


class StopRequested(Exception):
    """停止要求を受けて、取り込みが区切りで止まった（再開できる中断。失敗ではない）。"""


LOW_DISK_STOP_REASON = "low_disk"


class LowDiskStop(StopRequested):
    """ディスクの空きが少ないため、取り込みが区切りで止まった（起動時に自動では再開しない＝利用者が「更新」を押すまで待つ）。"""

    def __init__(self, free_bytes: int):
        self.free_bytes = free_bytes
        super().__init__(low_disk_message(free_bytes))


def low_disk_message(free_bytes: int) -> str:
    return f"ディスクの空きが少ないため取り込みを止めました（空き {free_bytes / 1024 ** 3:.1f} GB）"


def stop_record(exc: BaseException) -> dict:
    """止まった run の記録に足す欄（`stop_reason`）と注意の flag。空きの不足で止まったときだけ中身がある。"""
    if not isinstance(exc, LowDiskStop):
        return {}
    return {"stop_reason": LOW_DISK_STOP_REASON,
            "flags": [{"doc": None, "action": "warn",
                       "reason": f"low_disk_stop:{exc.free_bytes / 1024 ** 3:.1f}"}]}


_accepting = True                  # lifespan shutdown が False にする（新規受付停止）
_ACCEPT_LOCK = threading.Lock()
_stop_event = threading.Event()    # 立つと、取り込みは次の区切り（1 ファイル・1 段）へ進まずに止まる
_tls = threading.local()           # 取り込みを実行しているスレッドの run_id
_FILE_LOCK = threading.Lock()
_file_state: dict = {"file": None, "since": 0.0}   # 今処理している原本（ワーカーの心拍が読む）


def request_stop() -> None:
    """取り込みへ「区切りで止まる」要求を出す（ワーカーの SIGTERM 専用）。処理中の 1 件は最後まで進む。"""
    _stop_event.set()


def stop_requested() -> bool:
    """停止要求が立っていて、かつ呼び出し元が取り込みの run を実行しているスレッドか。それ以外（直接呼び出し・テスト）は常に False。"""
    return _stop_event.is_set() and getattr(_tls, "run_id", None) is not None


def raise_if_stop_requested() -> None:
    """区切り（1 ファイル・1 段の前）で呼ぶ。停止要求が立っていれば `StopRequested`。"""
    if stop_requested():
        raise StopRequested()


def in_run() -> bool:
    """呼び出し元が取り込みの run を実行しているスレッドか。"""
    return getattr(_tls, "run_id", None) is not None


def in_followup() -> bool:
    """呼び出し元が取り込みワーカーの run の後追い（`followup_scope` の中）か。"""
    return bool(getattr(_tls, "followup", False))


@contextlib.contextmanager
def followup_scope():
    """run の後追い（LLM 成形の反映など）の間だけ、ディスクの空きの確認（`es_index._space_point`）を有効にする。"""
    _tls.followup = True
    try:
        yield
    finally:
        _tls.followup = False


def bind_run(run_id: int) -> None:
    """このスレッドを run_id の実行として登録する（ワーカー専用）。`stop_requested`・`note_file` がこれを見る。"""
    _tls.run_id = run_id
    note_file(None)


def unbind_run() -> None:
    _tls.run_id = None
    note_file(None)


def note_file(rel: str | None) -> None:
    """この run が今処理している原本（資料フォルダからの相対パス）を記録する。`None` で「ファイルの途中ではない」へ戻す。"""
    if getattr(_tls, "run_id", None) is None:
        return
    with _FILE_LOCK:
        _file_state["file"] = rel
        _file_state["since"] = time.time()


def current_file() -> tuple[str | None, float]:
    """`(処理中の原本, その開始 epoch 秒)`。ワーカーの心拍が DB と状態ファイルへ書く。"""
    with _FILE_LOCK:
        return _file_state["file"], _file_state["since"]


def stop_accepting() -> None:
    """新規の依頼の受付を止める（lifespan shutdown 専用）。積み済みの依頼には触れない。"""
    global _accepting
    with _ACCEPT_LOCK:
        _accepting = False


def start_accepting() -> None:
    """`stop_accepting()` と `request_stop()` を取り消す（テスト専用）。"""
    global _accepting
    with _ACCEPT_LOCK:
        _accepting = True
    _stop_event.clear()


def submit(world_id: str, op: str, fingerprint: str, payload: dict | None = None, *,
           extra_keys: tuple[str, ...] = (), created_by: str = "admin",
           progress: dict | None = None) -> tuple[int, bool]:
    """取り込みの依頼を積む。戻り値は `(run_id, joined)`。

    ① 同じ資料フォルダ（と `extra_keys`）に queued／extracting の run があり `op`/`fingerprint` が一致 → 積まずに `(既存run_id, True)`。
       不一致 → `ConflictError`。
    ② 無ければ queued の 1 行を積んで `(新規run_id, False)`。ワーカーが拾うまで何も実行しない。
    `payload` は実行に要る引数（JSON）。`fingerprint` は合流判定用の正規化文字列（payload のうち操作を同一視する部分）。
    アプリ終了処理中は `ShuttingDownError`（呼び出し側が 503 へ変換）。
    """
    from .. import app_version, store
    with _ACCEPT_LOCK:
        if not _accepting:
            raise ShuttingDownError("終了処理中のため新規の取り込みは受け付けられません")
    verdict, row = store.enqueue_ingest_run(
        world_id, op, payload or {}, fingerprint, app_version.current(),
        keys=extra_keys, created_by=created_by, progress=progress)
    if verdict == "conflict":
        raise ConflictError(row.get("version") or world_id, row.get("op") or "", row["id"])
    return row["id"], verdict == "joined"
