"""外部 API の監査: 分類語彙・監査下書き・専用 writer スレッド＋bounded queue・`_AuditScope`。
設計: docs/design/external-api.md「共通の約束」
"""
from __future__ import annotations

import asyncio
import atexit
import logging
import queue
import threading
from concurrent.futures import Future, InvalidStateError

from fastapi import Request

from sherpa import ext_audit_health, outbox_sender
from sherpa.store import outbox

_log = logging.getLogger("sherpa")

# 監査の分類語彙（web/audit.html のフィルタ契約と一致させる）
_OUTCOME_SUCCESS = "success"
_OUTCOME_DENY = "deny"
_OUTCOME_ERROR = "error"
_SEVERITY_INFO = "info"
_SEVERITY_WARNING = "warning"


# path → (action, resource_type)。固定パスの X-API-Key ゲート付きエンドポイントのみ（handler が実行されない終了経路のフォールバック監査用）
_ACTION_BY_PATH = {
    "/ext/v1/convert": ("ext_api.convert", "ext_convert"),
    "/ext/v1/search": ("ext_api.search", "ext_search"),
    "/ext/v1/capabilities": ("ext_api.capabilities", "ext_capabilities"),
    "/ext/v1/doc": ("ext_api.doc", "ext_doc"),
    "/ext/v1/answer": ("ext_api.answer", "ext_answer"),
    "/ext/v1/openapi.json": ("ext_api.openapi", "ext_openapi"),
    # サブ資源（`/codex/jobs/{job_id}` 等）は job_id が path に入るためここでは捕捉できず、汎用既定へ倒れる
    "/ext/v1/codex/jobs": ("ext_api.codex_job_submit", "ext_codex_job"),
}

_HTTP_OUTCOME_REASON = {
    401: "unauthorized", 403: "forbidden", 404: "not_found", 413: "payload_too_large",
    415: "unsupported_media_type", 422: "validation_error", 429: "rate_limited",
    503: "unavailable", 504: "timeout",
}
_DENIED_STATUS = frozenset({401, 403, 429})  # 認可/レート制限に起因＝business_outcome="denied"


def _init_audit_pending(action: str, resource_type: str, actor: str = "ext:unknown") -> dict:
    """監査下書き（`request.state.audit_pending` へ置く辞書）を1つ作る単一の初期化点。"""
    return {"actor": actor, "action": action, "resource_type": resource_type,
            "resource_id": None, "detail": {}, "reason": None, "severity": None,
            "outcome": None, "business_outcome": "ok"}


def start_audit(request: Request, actor: str, action: str, resource_type: str,
                resource_id=None) -> dict:
    """管理系ルート（Cookie 認証）が使う監査下書きの初期化。ext-key 系と同じ `request.state.audit_pending` 機構に乗せ、`ExtRequestMiddleware` が実応答ステータスで1行だけ書く。"""
    pending = _init_audit_pending(action, resource_type, actor)
    pending["resource_id"] = resource_id
    request.state.audit_pending = pending
    return pending


_AUDIT_DB_CONNECT_TIMEOUT_S = 5.0
_AUDIT_DB_LOCK_TIMEOUT_MS = 3000
_AUDIT_DB_STATEMENT_TIMEOUT_MS = 5000


def _audit_db_connect():
    """監査書込み専用の接続（接続・advisory lock 待ち・statement 実行に上限時間を設ける）。

    `store.audit()`（timeout 無し）は使わない（writer スレッドが DB 不調で無期限にブロックすると以降の全監査書込みが止まるため）。
    """
    import psycopg
    from psycopg.rows import dict_row

    from sherpa.store.db import _dsn
    return psycopg.connect(
        _dsn(), row_factory=dict_row, connect_timeout=_AUDIT_DB_CONNECT_TIMEOUT_S,
        options=f"-c lock_timeout={_AUDIT_DB_LOCK_TIMEOUT_MS} -c statement_timeout={_AUDIT_DB_STATEMENT_TIMEOUT_MS}")


def _write_pending_audit(pending: dict | None, status_code: int, duration_ms: float,
                         method: str, path: str, request_id: str, *, outbox_row: dict | None = None) -> None:
    """`request.state.audit_pending` と、観測した応答ステータスから監査行を1つだけ書く（`_AuditWriter` の writer スレッド上でのみ）。

    - `result_count` は未設定なら 0。
    - status>=400 で handler が business_outcome を明示していなければ、401/403/429 は "denied"、それ以外は "failed"。
    - DB 列 `outcome`/`severity` は `web/audit.html` のフィルタ語彙に固定し、DB 専用列は追加しない（`detail` JSONB 内に収める）。
    - 監査書込失敗は握り潰さず re-raise する（`_AuditWriter._run()` が Future へ渡し、呼び出し側が ERROR ログ＋カウンタを記録する）。
    - `store._ensure()` は呼ばない（`init_schema()` は上限時間が無く、専用接続の timeout を迂回してしまう）。
    """
    if pending is None:
        return
    business_outcome = pending.get("business_outcome", "ok")
    if status_code >= 400 and business_outcome == "ok":
        business_outcome = "denied" if status_code in _DENIED_STATUS else "failed"
    detail = {**pending["detail"]}
    detail.setdefault("result_count", 0)
    detail.update(http_status=status_code, duration_ms=round(duration_ms, 2),
                 method=method, path=path, business_outcome=business_outcome)
    outcome = pending.get("outcome") or (_OUTCOME_SUCCESS if status_code < 400 else _OUTCOME_ERROR)
    reason = pending.get("reason")
    if reason is None and status_code >= 400:
        reason = _HTTP_OUTCOME_REASON.get(status_code, "error")
    from sherpa import store as _facade  # 実行時解決（monkeypatch シーム維持）
    with _audit_db_connect() as c:
        if outbox_row is not None and not outbox.holds(c, outbox_row):
            return  # 貸出を失った（別の送り手が引き継いだ）。書くのは今の貸出の持ち主
        _facade._audit_insert(c, pending["actor"], pending["action"], pending["resource_type"],
                              pending["resource_id"], detail, outcome=outcome, reason=reason,
                              severity=pending.get("severity") or _SEVERITY_INFO, request_id=request_id)
        if outbox_row is not None:  # 配送待ちからの書き直し: 監査行の確定と完了の記録を同じトランザクションにする（二重に書かない）
            c.execute("UPDATE delivery_outbox SET status='done', finished_at=now() WHERE id=%s",
                      (outbox_row["id"],))


_AUDIT_QUEUE_MAXSIZE = 1000
_AUDIT_QUEUE_PUT_TIMEOUT_S = 5.0  # 飽和時に待つ秒数（それでも空かなければ諦める）
_AUDIT_QUEUE_DRAIN_TIMEOUT_S = 10.0


_WRITER_RUNNING = "running"
_WRITER_STOPPING = "stopping"
_WRITER_STOPPED = "stopped"


class _AuditWriter:
    """監査 DB 書込み専用の単一 writer スレッド＋bounded queue（lifespan が起動/停止を管理するプロセス内シングルトン）。

    queue が飽和したら少し待ち（既定5秒）、それでも空かなければ ERROR ログを出してその1行だけ諦める。`submit()` は writer 未起動なら
    `_lazy_start()` するが、一度でも明示的に `stop()` された後は lazy start しない（`_explicitly_stopped`）。

    守ること:
    - lock は2つ。`_lock` は状態と `submit()` の「受付可否確認＋queue 投入」を不可分にし、`_transition_lock` は `start()`/`_lazy_start()`/`stop()`
      のライフサイクル操作全体を直列化する。**lock 順序は常に `_transition_lock` → `_lock`**（`submit()` の lazy start は `_lock` を手放してから `_lazy_start()` を呼ぶ）。
    - 世代管理: `join(timeout)` がタイムアウトしても `self._thread` を None にしない。新スレッドは旧世代の終了（`_stopped_event`）を確認してから起こし、
      確認できなければ起動を諦める（二重 writer を作らない）。`_sentinel_put` で sentinel の二重投入を防ぎ、`_start_locked()` で `_stopped_event`/`_sentinel_put` をリセットする。
    - `_run()` の finally は「`_restart_requested` を見て `_state` を STOPPED へ確定」と「`_stopped_event.set()`」を同じ `_lock` 区間で行い、
      `self._thread is my_thread`（現行世代）でなければ何も触らない。
    - 完了通知は `concurrent.futures.Future`。呼び出し側がキャンセル済みの Future への `set_result`/`set_exception` は `_resolve_future()` で必ず捕まえる。
    - `self._thread` のクリアは `self._thread is thread`（`stop()` が捕まえた thread と同一）のときだけ行う。
    - スレッドは `daemon=True`（`stop()` を呼ばない異常系でプロセス終了を妨げないため）。
    """

    def __init__(self, maxsize: int = _AUDIT_QUEUE_MAXSIZE):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._state = _WRITER_STOPPED
        self._lock = threading.Lock()
        self._transition_lock = threading.Lock()
        # lazy start は「一度も明示的に stop() されていない」インスタンスに限る（stop() 直後の遅延 submit が writer を蘇らせないため）
        self._explicitly_stopped = False
        self._sentinel_put = False  # 現世代で shutdown sentinel を投入済みか（stop() の再試行が二重投入しない）
        self._stopped_event = threading.Event()
        self._stopped_event.set()  # 初期状態＝スレッド無し＝（この世代は）停止済み
        # `_start_locked()` が旧世代の終了待ちでタイムアウトし起動を諦めたときに立てる
        self._restart_requested = False

    def start(self) -> bool:
        """明示的な起動（lifespan 等）。`_explicitly_stopped` を無条件で解除して起動を試みる。

        戻り値: RUNNING を確立できたら True。旧世代がまだ停止しきっていない等で起こせなければ False（呼び出し元が ERROR ログを出す）。
        False でも `_restart_requested` が立ち、旧世代が終わった時点で `_run()` の finally が STOPPED へ確定するため、以後の `submit()`/`start()` で再起動できる。
        """
        with self._transition_lock:
            with self._lock:
                self._explicitly_stopped = False
            return self._start_locked()

    def _lazy_start(self) -> bool:
        """`submit()` の遅延起動専用。`_transition_lock` 取得後に `_explicitly_stopped` を再判定してから起動する。"""
        with self._transition_lock:
            with self._lock:
                if self._explicitly_stopped:
                    return False
            return self._start_locked()

    def _start_locked(self) -> bool:
        """`_transition_lock` 保持中に呼ぶ（`start()`/`_lazy_start()` の内部実装）。

        稼働中なら何もせず True。そうでなければ旧世代の終了（`_stopped_event` が set 済み）を確認してから新スレッドを起こし、
        待っても set されなければ起動を諦め（ERROR ログ・`_state`/`_thread` は不変）、`_restart_requested` を立てる。
        """
        with self._lock:
            if self._state == _WRITER_RUNNING and self._thread is not None and self._thread.is_alive():
                return True  # 既に稼働中
        if not self._stopped_event.wait(timeout=_AUDIT_QUEUE_DRAIN_TIMEOUT_S):
            # wait() がタイムアウトしても旧世代の finally が完了している可能性があるため、`_lock` を取って最終確認する
            with self._lock:
                if not self._stopped_event.is_set():
                    self._restart_requested = True
                    _log.error("ext_api audit writer: 旧世代のスレッドが停止しないため start() を"
                              "諦めます（同じ queue を新旧スレッドで取り合わせない。旧世代の終了後に"
                              "自己回復します）")
                    return False
        with self._lock:
            self._state = _WRITER_RUNNING
            self._sentinel_put = False
            self._stopped_event.clear()
            self._thread = threading.Thread(target=self._run, name="ext-audit-writer", daemon=True)
            self._thread.start()
        return True

    def _run(self) -> None:
        my_thread = threading.current_thread()
        try:
            while True:
                item = self._q.get()
                try:
                    if item is None:  # 停止シグナル（`stop()` が投入する）
                        break
                    pending, status_code, duration_ms, method, path, request_id, fut = item
                    try:
                        _write_pending_audit(pending, status_code, duration_ms, method, path, request_id)
                    except Exception as e:
                        # `_write_pending_audit` は re-raise する契約。ここが例外を捕まえる唯一の場所で、writer loop は落とさず Future 経由で呼び出し元へ伝える
                        self._resolve_future(fut, exc=e)
                    else:
                        self._resolve_future(fut, exc=None)
                finally:
                    self._q.task_done()
        finally:
            # 状態確定と Event の set は同じ `_lock` 区間で行う（先に Event だけ set すると、別スレッドが新世代を起動した後にこの finally が古い判定で state を上書きしうる）。
            # `self._thread is my_thread`（現行世代）でなければ state も Event も触らない（新世代の `_stopped_event` を誤って set して二重 writer にしないため）
            with self._lock:
                if self._thread is my_thread:
                    if self._state == _WRITER_STOPPING and self._restart_requested:
                        self._state = _WRITER_STOPPED
                        self._restart_requested = False
                    self._stopped_event.set()

    @staticmethod
    def _resolve_future(fut: Future | None, *, exc: Exception | None) -> None:
        if fut is None:
            return
        try:
            if exc is None:
                fut.set_result(None)
            else:
                fut.set_exception(exc)
        except InvalidStateError:
            pass  # 呼び出し側が既に諦めている（Future キャンセル済み等）

    def submit(self, pending, status_code, duration_ms, method, path, request_id) -> Future | None:
        """キューへ投入する（同期・呼び出し側が別スレッドへ逃がすこと）。

        書込み完了時に解決される `concurrent.futures.Future` を返す（呼び出し元が `asyncio.wrap_future()` で await し、このリクエスト自身の監査書込みの完了を待つ）。
        受付可否の確認と投入は同じ `_lock` 区間で不可分に行い、lazy start が必要なら `_lock` を手放してから `_lazy_start()` を経由する（lock 順序の逆転を避ける）。
        飽和時は `_AUDIT_QUEUE_PUT_TIMEOUT_S` 秒だけ待ち、空かなければ None を返す。
        """
        with self._lock:
            needs_lazy_start = self._state == _WRITER_STOPPED and not self._explicitly_stopped
        if needs_lazy_start:
            self._lazy_start()  # stop() 後は再起動しない（`_explicitly_stopped`）
        with self._lock:
            if self._state != _WRITER_RUNNING:
                return None  # STOPPING/明示的に STOPPED 済みは新規受付しない
            fut: Future = Future()
            try:
                self._q.put((pending, status_code, duration_ms, method, path, request_id, fut),
                           timeout=_AUDIT_QUEUE_PUT_TIMEOUT_S)
            except queue.Full:
                return None
            return fut

    def stop(self, drain_timeout: float = _AUDIT_QUEUE_DRAIN_TIMEOUT_S) -> None:
        """新規受付を止め、既存キューを writer スレッドに回収させてから終了する（lifespan shutdown）。

        `_transition_lock` を保持したまま sentinel 投入と `thread.join()` まで行う。`join` タイムアウト後の再試行でも `_sentinel_put` が立っていれば投入し直さない。
        """
        with self._transition_lock:
            with self._lock:
                if self._state == _WRITER_STOPPED:
                    self._explicitly_stopped = True  # 既に停止済みでも「明示的に止めた」ことは記録する
                    return
                self._state = _WRITER_STOPPING  # 以後 submit() は拒否される
                self._explicitly_stopped = True  # submit() の lazy start を以後禁止する
                thread = self._thread
                already_put = self._sentinel_put
            if thread is None:
                with self._lock:
                    self._state = _WRITER_STOPPED
                return
            if not already_put:
                try:
                    self._q.put(None, timeout=drain_timeout)  # 既存 item の後ろに確実に入る
                    with self._lock:
                        self._sentinel_put = True
                except queue.Full:
                    _log.error("ext_api audit writer: shutdown 信号の投入がタイムアウトしました（queue 飽和）")
            thread.join(timeout=drain_timeout)
            with self._lock:
                if thread.is_alive():
                    _log.error("ext_api audit writer: stop() の join がタイムアウトしました"
                              "（writer スレッドはまだ生存中・二重起動防止のため参照を保持します。"
                              "再試行時は sentinel を再投入しない）")
                    return  # state は STOPPING のまま（以後の submit() も拒否され続ける）
                if self._thread is thread:  # 同一スレッドの場合だけ参照をクリアする
                    self._thread = None
                self._state = _WRITER_STOPPED


_audit_writer = _AuditWriter()  # lifespan が start()/stop() を呼ぶ
# 保険: lifespan を実行しない埋め込みでも、queue の未処理 item が失われないよう atexit で `stop()` を呼ぶ（未起動/既停止なら no-op）
atexit.register(_audit_writer.stop)


def _park_for_retry(pending: dict, status_code: int, duration_ms: float, method: str, path: str,
                    request_id: str) -> None:
    """書けなかった監査行を配送待ちの表へ残す（送り手が後で書き直す）。残せなかったことは ERROR ログに出す。"""
    try:
        outbox.add("ext_audit", {"pending": pending, "status_code": status_code,
                                 "duration_ms": duration_ms, "method": method, "path": path,
                                 "request_id": request_id})
    except Exception:
        _log.error("ext_api audit: 配送待ちへも残せませんでした（この1行は失われます）: action=%s request_id=%s",
                  pending.get("action"), request_id, exc_info=True)


def _redeliver_audit(row: dict) -> None:
    """配送待ちの監査行を書き直す。監査行の INSERT と配送待ちの完了記録は同じトランザクションで確定する。失敗は例外のまま伝え、送り手が再送を予約する。"""
    p = row["payload"]
    _write_pending_audit(p["pending"], p["status_code"], p["duration_ms"], p["method"], p["path"],
                         p["request_id"], outbox_row=row)


def _audit_gave_up(row: dict, reason: str) -> None:
    p = row["payload"]
    _log.error("ext_api audit: 再送の上限で諦めました（配送待ちの表に残ります）: action=%s request_id=%s reason=%s",
              p["pending"].get("action"), p["request_id"], reason)


AUDIT_OUTBOX_KIND = outbox_sender.Kind(deliver=_redeliver_audit, delays=(10, 60, 300),
                                       on_gave_up=_audit_gave_up)


async def _write_pending_audit_async(pending: dict | None, status_code: int, duration_ms: float,
                                     method: str, path: str, request_id: str) -> None:
    """`_write_pending_audit` を専用 writer スレッドの queue へ投入し、その書込みが完了するまで await する（event loop は塞がない）。

    queue 投入（飽和時は最大5秒ブロック）はデフォルト executor へ逃がす。queue 飽和・書込み失敗は ERROR ログ＋カウンタで記録し、
    その行を配送待ちの表へ残して後で書き直す（リクエストは失敗させない）。
    """
    if pending is None:
        return
    loop = asyncio.get_running_loop()
    fut = await loop.run_in_executor(None, _audit_writer.submit, pending, status_code,
                                     duration_ms, method, path, request_id)
    if fut is None:
        n = ext_audit_health.record_failure()
        _log.error("ext_api audit queue saturated, write deferred: action=%s request_id=%s (failure #%d)",
                  pending["action"], request_id, n)
        await loop.run_in_executor(None, _park_for_retry, pending, status_code, duration_ms, method,
                                   path, request_id)
        return
    try:
        await asyncio.wrap_future(fut)
    except Exception:
        n = ext_audit_health.record_failure()
        _log.error("ext_api audit write failed: action=%s request_id=%s (failure #%d)",
                  pending["action"], request_id, n, exc_info=True)
        await loop.run_in_executor(None, _park_for_retry, pending, status_code, duration_ms, method,
                                   path, request_id)


class _AuditScope:
    """`with _AuditScope(request, action, resource_type) as audit:` の中で `audit.resource_id`/`audit.detail`/`audit.business_outcome` を埋める。

    DB へは書かず `request.state.audit_pending` を更新するだけ（実際の監査行は `ExtRequestMiddleware` が観測した応答ステータスで1回だけ書く）。
    """

    __slots__ = ("pending",)

    def __init__(self, request: Request, action: str, resource_type: str):
        pending = getattr(request.state, "audit_pending", None)
        if pending is None:
            pending = _init_audit_pending(action, resource_type)
            request.state.audit_pending = pending
        pending["action"] = action
        pending["resource_type"] = resource_type
        self.pending = pending

    @property
    def resource_id(self):
        return self.pending["resource_id"]

    @resource_id.setter
    def resource_id(self, value) -> None:
        self.pending["resource_id"] = value

    @property
    def detail(self) -> dict:
        return self.pending["detail"]

    @property
    def business_outcome(self) -> str:
        return self.pending["business_outcome"]

    @business_outcome.setter
    def business_outcome(self, value: str) -> None:
        self.pending["business_outcome"] = value

    def __enter__(self) -> "_AuditScope":
        return self

    def __exit__(self, exc_type, exc, tb) -> bool:
        return False  # 例外は常に再送出（監査は ExtRequestMiddleware の役割）
