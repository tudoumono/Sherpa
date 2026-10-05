"""外部連携 API（`/ext/v1`）: APIキー認証基盤・決定的変換・エンジン分離検索＋RRF融合・discovery・原本取得・キーの world スコープ。

キー認証系（convert・search・capabilities・doc・openapi）はこの router に集約し、`api.py` から include する。admin キー発行/失効は
セッション Cookie 認証が必要なため `sherpa/routers/system_extras.py` に置き、監査は `start_audit()` を通じて `ExtRequestMiddleware` の
request-level 監査へ統合する。キー単位のレート制限は `_verify_key_sync` 内の `ratelimit.check_ext_api_rate_limit` が行う。
このファイルは `sherpa.api`・`sherpa.agents`・`sherpa.chat_service`・`sherpa.chat_router`・`sherpa.grep_tool` を import しない
（循環回避＋共有KBのみの契約）。

- convert は stateless（`office_md.to_markdown` の決定的変換のみ）。KB・台帳・ES・Neo4j へ書き込まず、一時ファイルは必ず削除する。LLM は呼ばない。
- search は `sherpa.parts.read.fused_search`（keyword=ES BM25 / vector=ES 純kNN / graph=Neo4j 影響たどり）へ委譲する。エンジン単位の不可は
  200＋`degraded[]`。world 解決は `worlds.resolve_external_world` で1回だけ行い、その `root` を引き回す（preflight 後の再解決禁止）。
- doc（原本DL）は world root を起点に `sherpa.safe_open` の symlink 差し替え耐性 open で得た fd 1本だけを使い、検証から配信まで行う。
  個人 workspace は world root の外で解決対象にならない。legacy Office（.doc/.xls/.ppt）は CFB ヘッダの健全性のみ検証する。
- `ExtRequestMiddleware`（生 ASGI ミドルウェア・`/ext/v1/*` のみ）が X-Request-Id の解決と応答ヘッダ付与、アプリログへの request_id 束縛、
  認証成功後の1リクエスト=1行監査（専用 writer スレッド経由）を行う。
設計: docs/design/external-api.md「共通の約束」
"""
from __future__ import annotations

import asyncio
import atexit
import contextlib
import contextvars
import hashlib
import hmac
import json
import logging
import os
import queue
import re
import secrets
import stat
import struct
import tempfile
import threading
import time
import zipfile
from concurrent.futures import Future, InvalidStateError
from pathlib import Path

from typing import Literal

from fastapi import (
    APIRouter,
    Depends,
    File,
    Header,
    HTTPException,
    Query,
    Request,
    Response,
    Security,
    UploadFile,
)
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, ConfigDict, Field, field_validator
from starlette.concurrency import run_in_threadpool

from sherpa import codex_jobs_worker, corpus_docs, ratelimit, safe_open, scope_infer, simple_chat, store, worlds
from sherpa import scope as scope_mod
from sherpa.parts.read import fused_search
from sherpa.store import codex_jobs as store_jobs
from sherpa.fd_response import FdFileResponse, FdOwner, content_disposition
from sherpa.ingest import office_md
from sherpa.ingest.analyzers import registry as _analyzer_registry

_log = logging.getLogger("sherpa")

router = APIRouter(prefix="/ext/v1", tags=["外部連携API"])

_KEY_PREFIX = "sk-ext-"

_CONVERT_MAX_BYTES = int(os.environ.get("SHERPA_EXT_CONVERT_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MB
_ZIP_MAX_UNCOMPRESSED = 500 * 1024 * 1024  # zip爆弾: 展開合計上限 500MB
_ZIP_MAX_RATIO = 200  # zip爆弾: 圧縮率上限
_ZIP_MAX_MEMBERS = 10_000  # zip爆弾: メンバ数上限（EOCD の bounded 検査にも使う）
_ZIP_EXTS = {".docx", ".xlsx", ".pptx"}  # OOXML＝zip コンテナ
# convert が受理する拡張子（拡張子から method を決定的に導出）
_ALLOWED_EXT = office_md.CONVERTIBLE_EXT | office_md.PDF_EXT  # {.docx,.xlsx,.pptx} | {.pdf}

# 監査の分類語彙（web/audit.html のフィルタ契約と一致させる）
_OUTCOME_SUCCESS = "success"
_OUTCOME_DENY = "deny"
_OUTCOME_ERROR = "error"
_SEVERITY_INFO = "info"
_SEVERITY_WARNING = "warning"


# ==== X-Request-Id（応答ヘッダの共通契約・OpenAPI にも宣言する）====

_REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_MAX_LEN = 200
# fullmatch 専用（`$` は末尾の改行の直前にもマッチし、末尾 LF 付きのヘッダ注入を通してしまうため `^/$` アンカーは使わない）
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_REQUEST_ID_OPENAPI_HEADER = {
    "X-Request-Id": {"schema": {"type": "string"}, "description": "リクエスト追跡ID"}}
# 422 は自動バリデーション（HTTPValidationError 形）とハンドラ内のドメインエラー（`{"detail": "文字列"}`）の両方があり得る。
# `responses=` で 422 を上書きすると既定 content が消えるため、X-Request-Id ヘッダを足すときはこの content も明示的に引き継ぐ
_VALIDATION_ERROR_CONTENT = {
    "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}}


def _validation_error_response(extra_description: str) -> dict:
    return {"description": f"入力値が不正です（自動バリデーション、または {extra_description}）",
            "content": dict(_VALIDATION_ERROR_CONTENT), "headers": dict(_REQUEST_ID_OPENAPI_HEADER)}


# 入力側の X-Request-Id を OpenAPI に明示する Header() 宣言（解決/検証は `ExtRequestMiddleware` が routing より前に行う）。
# 長さ制約は付けない（付けると不正な X-Request-Id を送っただけでリクエスト全体が 422 になる）
_XRequestIdIn = Header(
    default=None, alias="X-Request-Id",
    description="呼び出し元が指定するリクエスト追跡ID（省略時は採番される・不正な値は無視して採番）")

# アプリログへ request_id を束縛する ContextVar（`ExtRequestMiddleware` が要求ごとに set/reset する。監査 DB とは別系統）
_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("ext_request_id", default=None)


class _RequestIdLogFilter(logging.Filter):
    """`_request_id_ctx` の現在値を `record.request_id` として付与する（属性が既に在れば上書きしない＝冪等）。

    handler へ付与する（logger へは付与しない）。logger のフィルタは子ロガーへ継承されないが、handler のフィルタは届いた全レコードに効くため。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id_ctx.get() or "-"
        return True


_request_id_log_filter = _RequestIdLogFilter()


@contextlib.contextmanager
def _logging_module_lock():
    """`logging` 内部の直列化 lock を、Python バージョン差（3.13 で `_acquireLock()`/`_releaseLock()` が撤去）を吸収した context manager として貸す。"""
    acquire = getattr(logging, "_acquireLock", None)
    release = getattr(logging, "_releaseLock", None)
    if acquire is not None and release is not None:
        acquire()
        try:
            yield
        finally:
            release()
    else:
        # Python 3.13+: `logging._lock`（RLock 本体）を直接使う
        with logging._lock:
            yield


def _attach_request_id_filter() -> None:
    """`_request_id_log_filter` を既知の受信点（handler 単位）へ冪等に付与する。

    対象は root logger のその時点の handlers・`"sherpa"` と配下で作成済みの全 logger の handlers・`logging.lastResort`
    （同じ handler は重複排除して1回ずつ付与）。import 時に加えて `lifespan` の起動処理でも呼ぶ（`log_setup.configure_logging()` より後）。
    `loggerDict` はライブな dict のため、`_logging_module_lock()` を借りて root の handlers と loggerDict のスナップショットだけを取る。
    """
    with _logging_module_lock():
        targets: list[logging.Handler] = list(logging.getLogger().handlers)
        logger_snapshot = list(logging.Logger.manager.loggerDict.items())
    for name, obj in logger_snapshot:
        if isinstance(obj, logging.Logger) and (name == "sherpa" or name.startswith("sherpa.")):
            targets.extend(obj.handlers)
    if logging.lastResort is not None:
        targets.append(logging.lastResort)
    seen: set[int] = set()
    for h in targets:
        hid = id(h)
        if hid in seen:
            continue
        seen.add(hid)
        if _request_id_log_filter not in h.filters:
            h.addFilter(_request_id_log_filter)


_attach_request_id_filter()


def _resolve_request_id(raw: str | None) -> str:
    """X-Request-Id ヘッダ値を検証して採用する（不正/無指定なら自前で採番）。"""
    if raw and len(raw) <= _REQUEST_ID_MAX_LEN and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return secrets.token_hex(16)


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

_audit_write_failures = 0  # 監査書込失敗の累積カウンタ（プロセス内）


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
                         method: str, path: str, request_id: str) -> None:
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
        _facade._audit_insert(c, pending["actor"], pending["action"], pending["resource_type"],
                              pending["resource_id"], detail, outcome=outcome, reason=reason,
                              severity=pending.get("severity") or _SEVERITY_INFO, request_id=request_id)


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


async def _write_pending_audit_async(pending: dict | None, status_code: int, duration_ms: float,
                                     method: str, path: str, request_id: str) -> None:
    """`_write_pending_audit` を専用 writer スレッドの queue へ投入し、その書込みが完了するまで await する（event loop は塞がない）。

    queue 投入（飽和時は最大5秒ブロック）はデフォルト executor へ逃がす。queue 飽和・書込み失敗とも ERROR ログ＋カウンタで記録して続行する（リクエストは失敗させない）。
    """
    if pending is None:
        return
    global _audit_write_failures
    loop = asyncio.get_running_loop()
    fut = await loop.run_in_executor(None, _audit_writer.submit, pending, status_code,
                                     duration_ms, method, path, request_id)
    if fut is None:
        _audit_write_failures += 1
        _log.error("ext_api audit queue saturated, write dropped: action=%s request_id=%s (failure #%d)",
                  pending["action"], request_id, _audit_write_failures)
        return
    try:
        await asyncio.wrap_future(fut)
    except Exception:
        _audit_write_failures += 1
        _log.error("ext_api audit write failed: action=%s request_id=%s (failure #%d)",
                  pending["action"], request_id, _audit_write_failures, exc_info=True)


# ==== APIキー検証（`require_api_key` と ExtRequestMiddleware のフォールバックが共用する単一の実装）====

def _generate_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="ApiKeyAuth")


def _key_audit_detail(row: dict) -> dict:
    """キー行から監査 `detail` の共通部分を作る（`label` は常に・`owner_uid` は自己発行キー（非 NULL）のときだけ）。actor は `ext:{key_id}` のまま。"""
    d = {"label": row["label"]}
    if row.get("owner_uid") is not None:
        d["owner_uid"] = row["owner_uid"]
    return d


def _verify_key_sync(raw_key: str | None) -> dict:
    """X-API-Key の検証本体（同期・DB アクセスを含む）。`require_api_key` と `ExtRequestMiddleware` のフォールバック監査が共用する。

    有効期限（`expires_at`）・日次クォータ（`daily_quota`）・自己発行キー（`owner_uid`）の所有者状態/機能トグルも確認する（NULL の既存キーは対象外）。
    返値: 成功時 `{"ok": True, "row": <api_keys 行>}`。
    失敗時 `{"ok": False, "status": 401|429, "reason": str, "actor": "ext:<id>"|"ext:unknown", "resource_id": int|None, "retry_after": int|None, "detail": dict|None}`。
    """
    if not raw_key or not raw_key.startswith(_KEY_PREFIX):
        return {"ok": False, "status": 401, "reason": "missing_or_malformed",
                "actor": "ext:unknown", "resource_id": None, "retry_after": None, "detail": None}
    h = _hash_key(raw_key)
    row = store.api_key_by_hash(h)
    if not row or not hmac.compare_digest(row["key_hash"], h) or row["revoked_at"] is not None:
        return {"ok": False, "status": 401, "reason": "invalid_or_revoked",
                "actor": f"ext:{row['id']}" if row else "ext:unknown",
                "resource_id": row["id"] if row else None, "retry_after": None,
                "detail": _key_audit_detail(row) if row else None}
    expires_at = row.get("expires_at")
    if expires_at is not None:
        from datetime import datetime, timezone
        if expires_at <= datetime.now(timezone.utc):
            return {"ok": False, "status": 401, "reason": "expired",
                    "actor": f"ext:{row['id']}", "resource_id": row["id"],
                    "retry_after": None, "detail": _key_audit_detail(row)}
    if row.get("owner_uid") is not None:
        # 自己発行キーは毎回 (1) 機能トグルが ON、(2) 所有者が実在し `active`、を確認する（Cookie セッションと同じ前提）
        if not bool(store.get_system_settings().get("user_api_keys_allowed")):
            return {"ok": False, "status": 401, "reason": "user_keys_disabled",
                    "actor": f"ext:{row['id']}", "resource_id": row["id"],
                    "retry_after": None, "detail": _key_audit_detail(row)}
        if row.get("owner_status") != "active":
            return {"ok": False, "status": 401, "reason": "owner_inactive",
                    "actor": f"ext:{row['id']}", "resource_id": row["id"],
                    "retry_after": None, "detail": _key_audit_detail(row)}
    try:
        store.touch_api_key(row["id"])  # best-effort
    except Exception:
        pass
    remaining = ratelimit.check_ext_api_rate_limit(row["id"])
    if remaining is not None:
        return {"ok": False, "status": 429, "reason": "rate_limited",
                "actor": f"ext:{row['id']}", "resource_id": row["id"],
                "retry_after": int(remaining) + 1, "detail": _key_audit_detail(row)}
    daily_remaining = ratelimit.check_ext_api_daily_quota(row["id"], row.get("daily_quota"))
    if daily_remaining is not None:
        return {"ok": False, "status": 429, "reason": "daily_quota_exceeded",
                "actor": f"ext:{row['id']}", "resource_id": row["id"],
                "retry_after": int(daily_remaining) + 1, "detail": _key_audit_detail(row)}
    return {"ok": True, "row": row}


def _current_request_id(request: Request) -> str:
    """`ExtRequestMiddleware` が置いた request_id を読む（未装着経路では自前で解決する）。"""
    rid = getattr(request.state, "request_id", None)
    return rid if rid else _resolve_request_id(request.headers.get(_REQUEST_ID_HEADER))


def require_api_key(request: Request, key: str | None = Security(_api_key_header)) -> dict:
    """X-API-Key 検証（`_verify_key_sync` へ委譲）。失敗は一律 401（キー不存在/失効の区別を出さない）、rate limit は 429。

    返値 `{"key_id": int, "label": str, "allowed_worlds": list[str]|None, "request_id": str}`。
    `request.state.audit_pending` を初期化して成功/失敗いずれでも actor/reason/detail を積む（DB 書き込みは `ExtRequestMiddleware`）。
    """
    action, resource_type = _ACTION_BY_PATH.get(request.url.path, ("ext_api.request", "ext_request"))
    pending = _init_audit_pending(action, resource_type)
    request.state.audit_pending = pending

    result = _verify_key_sync(key)
    if not result["ok"]:
        is_rate_limited = result["status"] == 429
        pending.update(
            action="ext_api.rate_limited" if is_rate_limited else "ext_api.auth_failed",
            resource_type="api_key", resource_id=result["resource_id"], actor=result["actor"],
            reason=result["reason"], severity=_SEVERITY_WARNING,
            outcome=_OUTCOME_DENY)
        pending["detail"].update(result.get("detail") or {})
        headers = ({"Retry-After": str(result["retry_after"])}
                   if result["retry_after"] is not None else None)
        if result["reason"] == "rate_limited":
            msg = "リクエストが多すぎます（レート制限）"
        elif result["reason"] == "daily_quota_exceeded":
            msg = "最初の呼び出しから24時間ごとの枠の呼び出し上限に達しました"
        elif result["reason"] == "expired":
            msg = "APIキーの有効期限が切れています"
        elif result["reason"] == "missing_or_malformed":
            msg = "APIキーが必要です（X-API-Key ヘッダ）"
        else:
            msg = "APIキーが無効です"
        raise HTTPException(result["status"], msg, headers=headers)

    row = result["row"]
    pending["actor"] = f"ext:{row['id']}"
    if row.get("owner_uid") is not None:
        pending["detail"]["owner_uid"] = row["owner_uid"]
    key_info = {"key_id": row["id"], "label": row["label"], "allowed_worlds": row.get("allowed_worlds"),
               "request_id": _current_request_id(request)}
    request.state.ext_key = key_info
    return key_info


def _enforce_world_scope(request: Request, key: dict, world: str) -> None:
    """`key["allowed_worlds"]` が非 None かつ `world` がその中に無ければ 403。

    `request.state.audit_pending` を `ext_api.auth_failed`（reason=world_not_allowed）へ書き換える。`pending["detail"]` は update（マージ）する（置き換えると handler が先に積んだ detail が消える）。
    """
    allowed = key.get("allowed_worlds")
    if allowed is not None and world not in allowed:
        pending = getattr(request.state, "audit_pending", None)
        if pending is not None:
            pending.update(action="ext_api.auth_failed", resource_type="api_key",
                           resource_id=key["key_id"], reason="world_not_allowed",
                           severity=_SEVERITY_WARNING, outcome=_OUTCOME_DENY)
            pending["detail"].update({"world": world})
        raise HTTPException(403, "このキーはこの資料フォルダ（world）へのアクセスを許可されていません")


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


# ==== ExtRequestMiddleware（X-Request-Id・アプリログ束縛・request-level 監査の一元窓口）====

_INTERNAL_ERROR_BODY = json.dumps({"detail": "内部エラーが発生しました"}, ensure_ascii=False).encode("utf-8")


class ExtRequestMiddleware:
    """`/ext/v1/*` にだけ関与する生の ASGI ミドルウェア（`BaseHTTPMiddleware` は使わない。`send` を直接ラップして `http.response.start` にヘッダを差し込む）。

    非 ext パスは `scope["path"]` だけを見て内側 app へ即座に委譲する。ここで行うこと:
    1. X-Request-Id の解決（既存指定は大文字小文字を無視して検出）→ `scope["state"]`／ContextVar へ束縛 → 応答ヘッダへ常に1値で付与（自動422・`HTTPException`・`StreamingResponse`・未処理例外のいずれでも）。
    2. アプリログへ1行（method/path/status/duration/request_id）。
    3. 監査: `request.state.audit_pending` があれば観測した応答ステータスで `_write_pending_audit_async` を呼ぶ。無ければ（自動422や認証前キャンセル等）
       `_ACTION_BY_PATH` のルートに限りフォールバックで identity を解決して最小限の監査行を書く（`_fallback_audit_pending`）。
    4. 応答開始後の例外/キャンセルは「配信失敗」として `outcome=error`・`business_outcome=failed` で監査して再送出する。応答開始前の未処理例外は自前で 500 を返す。
       `asyncio.CancelledError` は常に再送出するが、fallback identity 解決・監査書込を単一の cleanup コルーチンにまとめて `asyncio.shield()` で保護する。
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or not scope["path"].startswith("/ext/v1/"):
            await self.app(scope, receive, send)
            return

        method = scope.get("method", "")
        path = scope["path"]
        raw_req_id = None
        for k, v in (scope.get("headers") or []):
            if k.lower() == b"x-request-id":
                raw_req_id = v
                break
        req_id = _resolve_request_id(raw_req_id.decode("latin-1") if raw_req_id else None)
        state = scope.setdefault("state", {})
        state["request_id"] = req_id
        state["t0"] = time.monotonic()
        ctx_token = _request_id_ctx.set(req_id)

        status_holder: dict = {"code": None, "started": False}
        send_exc: list[BaseException] = []  # 最初の配信例外だけ保持（自己生成500の再送も同じ経路）

        async def send_wrapper(message):
            """自己生成 500 も含め全ての send を通す唯一の経路。送信失敗は最初の1つだけ記録して re-raise する（握り潰して success 扱いにしない）。"""
            if message["type"] == "http.response.start":
                # 既存の X-Request-Id（大文字小文字を問わず）を除いてから正準値を1つだけ付ける
                raw_headers = [(hk, hv) for hk, hv in (message.get("headers") or [])
                              if hk.lower() != b"x-request-id"]
                message = {**message, "headers": raw_headers + [(b"x-request-id", req_id.encode("ascii"))]}
            try:
                await send(message)
            except Exception as e:
                if not send_exc:
                    send_exc.append(e)
                raise
            if message["type"] == "http.response.start":
                # start は実際の送信が成功して初めて確定する
                status_holder["code"] = message["status"]
                status_holder["started"] = True

        # 元の例外を保持し、cleanup 完了後に明示的に再送出する。`delivery_exc` は `send_wrapper` が記録した `send_exc` を最優先にする
        cancelled_exc: BaseException | None = None
        delivery_exc: BaseException | None = None
        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError as e:
            cancelled_exc = e
        except Exception as e:
            if status_holder["started"]:
                # 応答開始後の失敗＝配信失敗（ヘッダは追加できないため再送出のみ）
                delivery_exc = send_exc[0] if send_exc else e
                _log.exception("ext_api %s %s -> delivery failed after response start request_id=%s",
                              method, path, req_id)
            else:
                _log.exception("ext_api %s %s -> unhandled exception request_id=%s", method, path, req_id)
                try:
                    await send_wrapper({"type": "http.response.start", "status": 500,
                                       "headers": [(b"content-type", b"application/json")]})
                    await send_wrapper({"type": "http.response.body", "body": _INTERNAL_ERROR_BODY})
                except Exception:
                    # 自己生成500の送信も失敗＝`send_exc` に記録済み。下の一元判定に委ねる
                    pass
        if send_exc and delivery_exc is None:
            # `self.app(...)` が例外を投げず戻っても `send_wrapper` が失敗を観測していれば、success 扱いにしない最終防波堤
            delivery_exc = send_exc[0]

        cancelled = cancelled_exc is not None
        duration_ms = (time.monotonic() - state["t0"]) * 1000

        async def _cleanup() -> None:
            """fallback identity 解決・監査書込を1つにまとめる（`asyncio.shield()` で保護）。

            ContextVar の reset はここに含めない（shield の Task は別 Context で走り、Token は発行元の Context でしか reset できないため。shield 完了後に呼び出し元の `finally` で行う）。
            """
            pending = state.get("audit_pending")
            if pending is None:
                # `require_api_key` が実行されなかった経路（認証前キャンセル・malformed body 等）も、`_ACTION_BY_PATH` のルートならここで identity を解決する
                pending = await _fallback_audit_pending(scope)
            if cancelled and pending is not None:
                # status_holder["code"] が None/0 のままでも「成功」と誤判定されないよう outcome/business_outcome を明示する
                pending["reason"] = "cancelled"
                pending["outcome"] = _OUTCOME_ERROR
                pending["business_outcome"] = "failed"
            elif delivery_exc is not None and pending is not None:
                pending["reason"] = "delivery_failed"
                pending["outcome"] = _OUTCOME_ERROR
                pending["business_outcome"] = "failed"
            if not cancelled:
                _log.info("ext_api %s %s -> %s (%.1fms) request_id=%s",
                         method, path, status_holder["code"], duration_ms, req_id)
            await _write_pending_audit_async(
                pending, status_holder["code"] or 0, duration_ms, method, path, req_id)

        try:
            await asyncio.shield(_cleanup())
        finally:
            _request_id_ctx.reset(ctx_token)

        if cancelled_exc is not None:
            raise cancelled_exc
        if delivery_exc is not None:
            raise delivery_exc


async def _fallback_audit_pending(scope) -> dict | None:
    """`audit_pending` が一度も作られなかった終了経路（不正な JSON ボディの自動422 等）向けのフォールバック監査。

    `_ACTION_BY_PATH` のルートに限り、ヘッダから鍵を読んで identity の解決を試みる（`_verify_key_sync` を共用。DB アクセスはデフォルト executor 経由で、監査 writer の queue とは分離する）。
    admin 系（Cookie 認証）はフォールバックしない。
    """
    path = scope["path"]
    if path not in _ACTION_BY_PATH:
        return None
    action, resource_type = _ACTION_BY_PATH[path]
    raw_key = None
    for k, v in (scope.get("headers") or []):
        if k.lower() == b"x-api-key":
            raw_key = v.decode("latin-1")
            break
    loop = asyncio.get_running_loop()
    try:
        result = await loop.run_in_executor(None, _verify_key_sync, raw_key)
    except Exception:
        result = {"ok": False, "actor": "ext:unknown", "resource_id": None}
    detail = None
    if result.get("ok"):
        # 成功時の `_verify_key_sync` はキー行そのものを返すため、actor は row から組み立てる
        row = result["row"]
        actor, resource_id = f"ext:{row['id']}", row["id"]
        detail = _key_audit_detail(row)
    else:
        actor = result.get("actor") or "ext:unknown"
        resource_id = result.get("resource_id")
        detail = result.get("detail")
    pending = _init_audit_pending(action, resource_type, actor)
    pending["resource_id"] = resource_id
    pending["reason"] = "request_incomplete"
    pending["business_outcome"] = "failed"
    if detail:
        pending["detail"].update(detail)
    return pending


# ==== POST /ext/v1/convert ====

_ZIP_READ_CHUNK = 1024 * 1024  # zip爆弾: メンバ実測時の読み取り単位


def _zip_bomb_reason(p: Path, compressed_size: int) -> str | None:
    """OOXML zip の安全検査。危険なら理由文字列、安全なら None（壊れ zip は None）。

    中央ディレクトリの自己申告値（`ZipInfo.file_size`）は使わず、各メンバを実際にストリーム展開した実測バイト数で上限を判定する。
    """
    try:
        with zipfile.ZipFile(p) as z:
            infos = z.infolist()
            if len(infos) > _ZIP_MAX_MEMBERS:
                return "too_many_members"
            total = 0
            for info in infos:
                with z.open(info) as member:
                    while True:
                        chunk = member.read(_ZIP_READ_CHUNK)
                        if not chunk:
                            break
                        total += len(chunk)
                        if total > _ZIP_MAX_UNCOMPRESSED:
                            return "uncompressed_too_large"
            if compressed_size > 0 and total / compressed_size > _ZIP_MAX_RATIO:
                return "compression_ratio"
    except zipfile.BadZipFile:
        return None
    return None


_CONVERT_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    413: {"description": "ファイルサイズが上限を超えています", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("この形式は変換できません／安全でないファイルです"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/convert", responses=_CONVERT_RESPONSES)
async def ext_convert(request: Request, file: UploadFile = File(...),
                      key: dict = Depends(require_api_key),
                      x_request_id: str | None = _XRequestIdIn):
    """アップロードされた Office/PDF ファイルを決定的に Markdown へ変換する。

    stateless（KB・台帳・ES・Neo4j へ書き込まず、LLM を呼ばない）。ファイルは一時領域にのみ書き、必ず削除する。world を持たないため world スコープは対象外。
    変換は要求を受け付けるスレッドとは別の実行プールで行う。
    """
    del x_request_id  # 実際の解決は ExtRequestMiddleware（ここは OpenAPI 契約の宣言のみ）
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    with _AuditScope(request, "ext_api.convert", "ext_convert") as audit:
        audit.detail.update({"filename": filename, "ext": ext})
        if ext not in _ALLOWED_EXT:
            raise HTTPException(422, "この形式は変換できません（対応: .docx/.xlsx/.pptx/.pdf）")

        # チャンク読み＋サイズ上限（OOM 回避）
        chunks: list[bytes] = []
        total = 0
        chunk_size = 65536
        while True:
            chunk = await file.read(chunk_size)
            if not chunk:
                break
            total += len(chunk)
            if total > _CONVERT_MAX_BYTES:
                raise HTTPException(
                    413, f"ファイルサイズが上限（{_CONVERT_MAX_BYTES // 1024 // 1024}MB）を超えています")
            chunks.append(chunk)
        data = b"".join(chunks)
        size_bytes = len(data)
        audit.detail["size_bytes"] = size_bytes

        method = "pdf_text" if ext == ".pdf" else "ooxml"  # 拡張子から決定的に導出

        def _convert() -> dict:
            """一時ファイル書込・zip 爆弾検査・`to_markdown` をまとめて threadpool 内で実行する。"""
            fd, tmp_name = tempfile.mkstemp(suffix=ext)
            tmp = Path(tmp_name)
            try:
                # 書き込み自体が例外を送出しても tmp のパスは確定しているため finally で確実に削除できる
                with os.fdopen(fd, "wb") as f:
                    f.write(data)
                if ext in _ZIP_EXTS:
                    reason = _zip_bomb_reason(tmp, size_bytes)
                    if reason is not None:
                        raise HTTPException(422, "安全でないファイルです（zip 検査に失敗）")

                if ext == ".pdf" and not office_md.pdf_available():
                    audit.detail.update({"method": None, "ok": False})
                    audit.business_outcome = "failed"
                    return {"md": None, "method": None, "unsupported": True,
                            "reason": "pdf_backend_unavailable", "filename": filename,
                            "size_bytes": size_bytes}

                md = office_md.to_markdown(tmp)
                if md is None:
                    audit.detail.update({"method": None, "ok": False})
                    audit.business_outcome = "failed"
                    return {"md": None, "method": None, "unsupported": True,
                            "reason": "conversion_failed", "filename": filename,
                            "size_bytes": size_bytes}

                audit.detail.update({"method": method, "ok": True})
                return {"md": md, "method": method, "unsupported": False,
                        "filename": filename, "size_bytes": size_bytes}
            finally:
                tmp.unlink(missing_ok=True)

        return await run_in_threadpool(_convert)


# ==== POST /ext/v1/search（エンジン分離検索＋RRF融合）====

class ExtSearchReq(BaseModel):
    world: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    engines: list[Literal["keyword", "vector", "graph"]] | None = None  # None→keyword+vector
    k: int = Field(default=10, ge=1, le=50)
    scope_paths: list[str] = Field(default_factory=list)  # フォルダ prefix
    # 探す対象。既定 both＝フィルタなし。keyword/vector にのみ適用（graph は言及エッジが資料とコードを繋ぐため非適用）
    layer: Literal["docs", "code", "both"] = "both"
    weights: dict[Literal["keyword", "vector", "graph"], float] | None = None
    # graph エンジンの影響たどりの深さ（keyword/vector は無視）。既定は `impact_service.IMPACT_MAX_DEPTH`。
    # 上限（le）は外部 API の契約値12を下回らないよう `max(12, IMPACT_MAX_DEPTH)`
    depth: int = Field(default=fused_search.IMPACT_MAX_DEPTH, ge=1,
                       le=max(12, fused_search.IMPACT_MAX_DEPTH))
    # graph エンジンで、構造の結果が無いときに資料の grep 由来の推定を足すか（既定 true＝従来どおり足す）。
    # false なら推定を足さない（`coverage.graph.presumed_count` は 0）。keyword/vector は無視。
    include_presumed: bool = True
    # graph エンジンの各経路の辺ごとに返す根拠（`paths[].edges[].sources`）の最大件数。経路の本数には効かない（切った本数は `paths_omitted`）。
    # 0 は根拠を返さず `sources_omitted` に件数だけ返す。keyword/vector は無視。
    evidence_limit: int = Field(default=3, ge=0, le=10)

    @field_validator("weights")
    @classmethod
    def _w_range(cls, v):
        if v is not None and any(not (0 < x <= 10) for x in v.values()):
            raise ValueError("weights は 0 < w <= 10")
        return v


class ExtFromDef(BaseModel):
    """参照元の定義。`key` は定義のキー（`null` はそのファイルの主体）。"""
    file: str
    key: str | None


class ExtEdgeSource(BaseModel):
    """辺（つながり）の根拠 1 件。参照元の文書・行・関係の種類・接続を決めた解決規則。言及の辺は `locator`（文書内の位置）を持つ。"""
    via: str | None = None
    doc_id: str
    file: str
    line: int
    rule: str | None = None
    from_def: ExtFromDef | None = None
    locator: str | None = None
    evidence_text: str | None = None


class ExtGraphEdge(BaseModel):
    """経路の辺 1 本。`via` は関係の種類・`rule` は代表の根拠の解決規則・`sources` は根拠（先頭 `evidence_limit` 件）・
    `sources_omitted` は返さなかった根拠の件数（保存の上限を超えた分を含む）。根拠のある辺だけが `rule`・`sources`・`sources_omitted` を持つ。"""
    type: str
    doc: str | None = None
    line: int | None = None
    via: str | None = None
    rule: str | None = None
    sources: list[ExtEdgeSource] | None = None
    sources_omitted: int | None = None


class ExtGraphPath(BaseModel):
    nodes: list[str]
    edges: list[ExtGraphEdge]


class ExtHit(BaseModel):
    doc_id: str | None
    path: str | None
    line: int | None = None
    snippet: str
    score: float
    sources: dict[str, int]
    paths: list[ExtGraphPath] | None = None
    # 経路を 5 本で切ったときだけ、切った本数（`paths` に載せなかった経路の数）。
    paths_omitted: int | None = None
    # graph で見つかったヒットだけ。`structure`＝構造の辺をたどって見つかった・`presumed`＝構造の結果が無いときの資料からの推定。
    # 確からしさの格付けではない（keyword/vector との統合は `sources` が表す）。
    graph_origin: list[Literal["structure", "presumed"]] | None = None


class ExtDegraded(BaseModel):
    engine: Literal["keyword", "vector", "graph"]
    reason: str
    # 失敗の詳細（`coverage.limits[].kind` と同じ語: `timeout`・`row_cap`・`graph_unavailable`・`graph_reingest_required`）。
    # `reason` の値は増やさない。詳細が無い失敗では出ない。
    detail: str | None = None


class ExtLimit(BaseModel):
    kind: Literal["timeout", "row_cap", "depth", "result_cap", "doc_search_truncated", "card_cap",
                  "graph_unavailable", "graph_reingest_required", "plugin_failed"]


class ExtDepth(BaseModel):
    requested: int
    truncated: bool | None  # 深さの上限で止まり、先に辺が残るか（先の判定が時間内に終わらなかったときは null）


class ExtEngineCoverage(BaseModel):
    """成功したエンジン 1 つの調べ切れ具合。失敗したエンジンは持たない（`degraded` が伝える）。"""
    complete: bool  # `limits` が 1 つでもあれば false
    requested_k: int
    returned: int
    omitted: int | None = None  # そのエンジンが `k` で切って返さなかった件数（数えられないときは null）
    limits: list[ExtLimit]
    depth: ExtDepth | None = None  # graph だけ
    structural_count: int | None = None  # graph だけ（`k` で切る前の件数）
    presumed_count: int | None = None  # graph だけ


class ExtFusedCoverage(BaseModel):
    requested_k: int
    returned: int
    omitted_by_cut: int  # 融合した候補のうち、最終の `k` で切った件数（エンジン別の `omitted` とは足し合わせない）


class ExtCoverage(BaseModel):
    """使ったエンジンだけがキーになる。"""
    keyword: ExtEngineCoverage | None = None
    vector: ExtEngineCoverage | None = None
    graph: ExtEngineCoverage | None = None
    fused: ExtFusedCoverage | None = None


class ExtSearchRes(BaseModel):
    world: str
    query: str
    hits: list[ExtHit]
    engines_used: list[str]
    degraded: list[ExtDegraded]
    coverage: ExtCoverage | None = None


def _resolve_world_or_error(world: str, *, connect_timeout: float | None = None,
                            statement_timeout_ms: int | None = None) -> Path:
    """外部 API 専用の strict 解決。registry 不達／登録 root 不達は 503、未登録/未実在は 404。
    `connect_timeout`/`statement_timeout_ms`（省略可）は `worlds.resolve_external_world()` へ転送する。
    """
    try:
        res = worlds.resolve_external_world(world, connect_timeout=connect_timeout,
                                            statement_timeout_ms=statement_timeout_ms)
    except worlds.ExternalResolverError as e:
        from .ingest.graph_extract import _log_masked_exception
        _log_masked_exception(_log, "ext_api: world resolver 到達不可", e)
        raise HTTPException(
            503, "資料フォルダの参照先を確認できませんでした（一時的な障害の可能性があります）") from e
    if res.status != "ok":
        raise HTTPException(404, "資料フォルダ（world）が見つかりません")
    return res.path


_SEARCH_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "資料フォルダ（world）が見つかりません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("不明な範囲（scope_paths）が指定された場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "資料フォルダの参照先を確認できませんでした（一時的な障害）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


# `response_model_exclude_unset`: 追加の省略可の項目（`coverage`・`graph_origin`・`degraded[].detail`）は、値が無いとき null で出さず欄ごと省く
# （既存のクライアントへ見える形を増やさない。既存の項目は常に値を持つので変わらない）。
@router.post("/search", response_model=ExtSearchRes, response_model_exclude_unset=True,
             responses=_SEARCH_RESPONSES)
def ext_search(req: ExtSearchReq, request: Request, key: dict = Depends(require_api_key),
               x_request_id: str | None = _XRequestIdIn):
    """RAG検索（エンジン分離＋RRF融合）。共有 KB のみ・個人 workspace は対象外。"""
    del x_request_id
    with _AuditScope(request, "ext_api.search", "ext_search") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200],
                             "engines": req.engines or list(fused_search.DEFAULT_ENGINES),
                             "k": req.k, "depth": req.depth, "layer": req.layer})
        if not req.include_presumed:
            audit.detail["include_presumed"] = False  # 既定と違う要求の欄だけ残す
        if req.evidence_limit != 3:
            audit.detail["evidence_limit"] = req.evidence_limit
        # scope 確認・world 解決・scope_paths 検証より前に正規化して積む（どの経路で失敗しても監査に world・prefix が残る）
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        _enforce_world_scope(request, key, req.world)  # scope 外は世界の存在有無を明かさず先に 403
        root = _resolve_world_or_error(req.world)  # strict 解決は1回だけ・以降これを使い回す
        try:
            # `valid_scope_paths(strict=True)` は OSError を re-raise しうるため、search() 本体と同じ try/except に含めて 503 にする
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True):
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
            extra = {} if req.include_presumed else {"include_presumed": False}
            if req.evidence_limit != 3:
                extra["evidence_limit"] = req.evidence_limit
            res = fused_search.search(req.world, req.query, engines=req.engines, k=req.k,
                                        scope_paths=req.scope_paths, weights=req.weights,
                                        depth=req.depth, root=root, strict=True, layer=req.layer, **extra)
        except OSError as e:
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        audit.detail["result_count"] = len(res["hits"])
        audit.detail["degraded"] = [d["reason"] for d in res["degraded"]]
        complete = {e: c["complete"] for e, c in (res.get("coverage") or {}).items() if e != "fused"}
        if not all(complete.values()):
            # 打ち切りのあったエンジンがあるときだけ、エンジンごとの調べ切れ（`complete`）を残す（既定の要求の詳細は今と同じ）
            audit.detail["coverage_complete"] = complete
        return {"world": req.world, "query": req.query, **res}


# ==== 出典（sources）共通モデル（`C-EXT-ANSWER-01`／Codex ジョブ結果と共有）====

class ExtSourceItem(BaseModel):
    """出典（`sources`）の共通スキーマ。

    `doc_id`: world 内の相対パス（`GET /ext/v1/doc?world=<world>&path=<doc_id>` の `path` へそのまま渡せる）。
    `locator`（省略可）: 出典の箇所（行の範囲等）。確かめられた箇所を持たない出典は省略する。
    """
    doc_id: str
    locator: str | None = None


# ==== POST /ext/v1/answer（簡易チャットの同期応答・`C-EXT-ANSWER-01`）====
# 読み取り部品を道具として LLM に数回使わせて1回答える薄いループ（`sherpa/simple_chat.py`）を呼ぶだけで、重複実装しない

_ANSWER_TIMEOUT_S = 120  # 回答全体の絶対期限（秒）


class ExtAnswerReq(BaseModel):
    # 契約外の項目（model・provider 等）は黙って捨てず 422（指定が効いたと誤解させない）
    model_config = ConfigDict(extra="forbid")
    world: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    scope_paths: list[str] = Field(default_factory=list)  # フォルダ prefix


class ExtAnswerRes(BaseModel):
    answer: str
    sources: list[ExtSourceItem]
    unconfirmed: bool
    unconfirmed_reason: str | None = None
    tool_calls: int
    elapsed_ms: int


_ANSWER_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "資料フォルダ（world）が見つかりません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("不明な範囲（scope_paths）が指定された場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "既定の AI が使えません（未接続・未設定。フォールバックはしません）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    504: {"description": f"回答が制限時間（{_ANSWER_TIMEOUT_S}秒）内に完了しませんでした",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/answer", response_model=ExtAnswerRes, response_model_exclude_none=True, responses=_ANSWER_RESPONSES)
def ext_answer(req: ExtAnswerReq, request: Request, key: dict = Depends(require_api_key),
              x_request_id: str | None = _XRequestIdIn):
    """簡易チャットの同期応答。共有 KB のみ・個人 workspace は対象外。

    回答全体に 120 秒の絶対期限があり、world の解決・scope_paths の検証・回答の生成がその同じ期限を共有する（超えると 504）。
    `model`/`provider` は入力に持たず、管理者設定の既定 AI を使う。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.answer", "ext_answer") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200]})
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        deadline = time.monotonic() + _ANSWER_TIMEOUT_S

        def _remaining() -> float:
            return deadline - time.monotonic()

        def _deadline_exceeded() -> HTTPException:
            return HTTPException(
                504, f"回答が制限時間（{_ANSWER_TIMEOUT_S}秒）内に完了しませんでした")

        _enforce_world_scope(request, key, req.world)  # scope 外は世界の存在有無を明かさず先に 403
        root = _resolve_world_or_error(
            req.world, connect_timeout=max(0.001, _remaining()),
            statement_timeout_ms=max(1, int(_remaining() * 1000)))
        if _remaining() <= 0:
            raise _deadline_exceeded()
        try:
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True,
                                               deadline=deadline):
                if _remaining() <= 0:
                    raise _deadline_exceeded()
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
        except scope_infer.ScopeWalkDeadlineExceeded:
            raise _deadline_exceeded() from None
        except OSError as e:
            if _remaining() <= 0:
                raise _deadline_exceeded() from e
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        if _remaining() <= 0:
            raise _deadline_exceeded()
        try:
            # 入口で1回だけ解決した root に固定する（道具の中で registry を引き直さない）
            with worlds.pin_world_root(req.world, root):
                result = simple_chat.answer(
                    world=req.world, query=req.query, scope_paths=req.scope_paths,
                    key_id=key["key_id"], absolute_deadline=deadline)
        except simple_chat.AnswerTimeout as e:
            raise HTTPException(504, str(e)) from None
        except simple_chat.LLMUnavailable as e:
            audit.detail["reason"] = str(e)
            raise HTTPException(503, str(e)) from None
        audit.detail.update({"tool_calls": result["tool_calls"], "unconfirmed": result["unconfirmed"],
                             "result_count": len(result["sources"])})
        return result


# ==== Codex ジョブ（非同期・`C-EXT-CODEXJOB-*`）====
# 受付（`POST /codex/jobs`）→状態照会（`GET /codex/jobs/{id}`）→結果取得（`GET /codex/jobs/{id}/result`）→取消（`POST /codex/jobs/{id}/cancel`）の4段。
# 実行本体は `sherpa/store/codex_jobs.py`／`sherpa/codex_jobs_worker.py`（本ファイルは `sherpa.agents`/`sherpa.chat_service` を import しないため、実行可否の判定も `codex_jobs_worker.check_available()` へ委譲する）。
# 「資料フォルダスコープ外は403」が通用するのは受付だけ。状態照会・結果取得・取消は、`job_id` が存在しない場合も鍵の scope 外の場合も 404（ジョブの存在を漏らさない）


def _codex_job_visible(job: dict | None, key: dict) -> bool:
    """このジョブをこの鍵から見てよいか。ジョブは受け付けた鍵（key_id）に属し、別の鍵からは見えない。照会の時点で鍵の `allowed_worlds` から外れていれば見えない。"""
    if job is None:
        return False
    if job["key_id"] != key["key_id"]:
        return False
    allowed = key.get("allowed_worlds")
    return allowed is None or job["world"] in allowed


def _codex_job_not_found() -> HTTPException:
    return HTTPException(404, "ジョブが見つかりません")


def _iso(dt) -> str | None:
    return None if dt is None else str(dt)


_CODEX_JOB_RETRY_AFTER = {"queued": "30", "running": "60"}  # 終了状態は付けない


class ExtCodexJobSubmitReq(BaseModel):
    # 契約外の項目は黙って捨てず 422
    model_config = ConfigDict(extra="forbid")
    world: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    scope_paths: list[str] = Field(default_factory=list)  # フォルダ prefix
    # チャットと同じ調べる深さ（`depth_profile.DEPTH_PROFILES`）。省略時 "standard"。不正値は自動 422
    depth: Literal["quick", "standard", "deep", "max"] | None = None
    # 終了通知。`true` は鍵に通知先が登録済みのときだけ受け付ける（送れない通知を受け付けない）
    webhook: bool = False


class ExtCodexJobSubmitRes(BaseModel):
    job_id: str
    status: Literal["queued"]


_CODEX_JOB_SUBMIT_RESPONSES = {
    202: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "資料フォルダ（world）が見つかりません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("webhook=true なのに鍵に通知先が登録されていない場合、または不明な範囲（scope_paths）が指定された場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "Codex が今実行できません（CLI未接続・未設定・サンドボックス不可等）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/codex/jobs", status_code=202, response_model=ExtCodexJobSubmitRes,
            responses=_CODEX_JOB_SUBMIT_RESPONSES)
def ext_codex_job_submit(req: ExtCodexJobSubmitReq, request: Request,
                         key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの受付。共有 KB のみ・個人 workspace は対象外。"""
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_submit", "ext_codex_job") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200],
                             "depth": req.depth or "standard"})
        if req.webhook and store.get_api_key_webhook(key["key_id"]) is None:
            raise HTTPException(
                422, "この鍵には通知先（Webhook）が登録されていないため、webhook=true は指定できません")
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        _enforce_world_scope(request, key, req.world)  # scope 外は世界の存在有無を明かさず先に 403
        root = _resolve_world_or_error(req.world)
        try:
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True):
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
        except OSError as e:
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        try:
            codex_jobs_worker.check_available()
        except codex_jobs_worker.CodexUnavailable as e:
            _log.warning("ext_api codex job submit: Codex unavailable: %s", e)
            raise HTTPException(503, "Codex が今実行できません（管理者に接続状況の確認を依頼してください）") from None
        depth = req.depth or "standard"
        job_id = store_jobs.new_job_id()
        row = store_jobs.insert_job(job_id=job_id, key_id=key["key_id"], world=req.world,
                                    query=req.query, scope_paths=sp, depth=depth,
                                    webhook=req.webhook)
        audit.resource_id = row["id"]
        return {"job_id": row["id"], "status": row["status"]}


# ---- GET /codex/jobs/{job_id}（状態照会）----

class ExtCodexJobStatusRes(BaseModel):
    job_id: str
    status: str
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    expires_at: str | None = None


_CODEX_JOB_STATUS_RESPONSES = {
    200: {"headers": {**_REQUEST_ID_OPENAPI_HEADER,
                      "Retry-After": {"schema": {"type": "string"},
                                      "description": "次に見に来るまでの目安秒数（queued/running のみ）"}}},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "ジョブが見つかりません（別の鍵のジョブ・scope外・存在しない、のいずれも同じ404）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.get("/codex/jobs/{job_id}", response_model=ExtCodexJobStatusRes,
           response_model_exclude_none=True, responses=_CODEX_JOB_STATUS_RESPONSES)
def ext_codex_job_status(job_id: str, request: Request, response: Response,
                         key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの状態照会。"""
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_status", "ext_codex_job") as audit:
        audit.resource_id = job_id
        job = store_jobs.get_job(job_id)
        if not _codex_job_visible(job, key):
            raise _codex_job_not_found()
        job = store_jobs.expire_job_if_due(job_id, job)
        audit.detail.update({"world": job["world"], "status": job["status"]})
        retry_after = _CODEX_JOB_RETRY_AFTER.get(job["status"])
        if retry_after is not None:
            response.headers["Retry-After"] = retry_after
        return {"job_id": job["id"], "status": job["status"], "created_at": _iso(job["created_at"]),
                "started_at": _iso(job["started_at"]), "finished_at": _iso(job["finished_at"]),
                "expires_at": _iso(job["expires_at"])}


# ---- GET /codex/jobs/{job_id}/result（結果取得）----

class ExtCodexJobUnconfirmedItem(BaseModel):
    """調査で確認できなかった項目。`reason` は確認できなかった理由の定型文、または null。"""
    item: str
    reason: str | None = None


class ExtInvestigationRecord(BaseModel):
    """調査の記録。`manifest`（`question_kind`/`created_at`/`items`）・`items`/`coverage`（id→値の dict）・`reviews`（調査の途中の見直しの配列）。資料名は運ばない。"""
    complete: bool
    truncated: bool
    manifest: dict | None = None
    items: dict = {}
    coverage: dict = {}
    reviews: list = []


class ExtCodexJobResultRes(BaseModel):
    answer: str
    sources: list[ExtSourceItem]  # 出典の共通スキーマ（`ExtSourceItem`・`C-EXT-ANSWER-01` と同一）
    unconfirmed_items: list[ExtCodexJobUnconfirmedItem]
    elapsed_ms: int
    investigation: ExtInvestigationRecord | None = None  # 記録が無ければ省く（response_model_exclude_none）


_CODEX_JOB_RESULT_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "ジョブが見つかりません（別の鍵のジョブ・scope外・存在しない、のいずれも同じ404）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    409: {"description": "まだ完了していない、または失敗/取消済みです（`status`／失敗時のみ `error_code`）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    410: {"description": "保存期間（7日）を過ぎ、結果は消去済みです", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.get("/codex/jobs/{job_id}/result", response_model=ExtCodexJobResultRes,
            response_model_exclude_none=True, responses=_CODEX_JOB_RESULT_RESPONSES)
def ext_codex_job_result(job_id: str, request: Request, key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの結果取得。`completed` のときだけ200（409/410 は本文形が異なるため dict をそのまま返す）。"""
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_result", "ext_codex_job") as audit:
        audit.resource_id = job_id
        job = store_jobs.get_job(job_id)
        if not _codex_job_visible(job, key):
            raise _codex_job_not_found()
        job = store_jobs.expire_job_if_due(job_id, job)
        audit.detail.update({"world": job["world"], "status": job["status"]})
        status = job["status"]
        if status == "expired":
            return JSONResponse(status_code=410, content={"status": "expired"})
        if status != "completed":
            body = {"status": status}
            if status == "failed" and job.get("error_code"):
                body["error_code"] = job["error_code"]
            # 契約どおりの形（`{"status", "error_code"?}`）をそのまま返すため `JSONResponse` を直接使う（`HTTPException` だと `{"detail": body}` に包まれる）
            audit.business_outcome = "failed"
            return JSONResponse(status_code=409, content=body)
        return {"answer": job.get("answer") or "", "sources": list(job.get("sources") or []),
                "unconfirmed_items": list(job.get("unconfirmed_items") or []),
                "elapsed_ms": job.get("elapsed_ms") or 0,
                "investigation": job.get("investigation")}


# ---- POST /codex/jobs/{job_id}/cancel（取消）----

class ExtCodexJobCancelRes(BaseModel):
    status: str


_CODEX_JOB_CANCEL_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "ジョブが見つかりません（別の鍵のジョブ・scope外・存在しない、のいずれも同じ404）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/codex/jobs/{job_id}/cancel", response_model=ExtCodexJobCancelRes,
            responses=_CODEX_JOB_CANCEL_RESPONSES)
def ext_codex_job_cancel(job_id: str, request: Request, key: dict = Depends(require_api_key),
                         x_request_id: str | None = _XRequestIdIn):
    """Codex ジョブの取消。冪等。`queued` は即 `cancelled`、`running` は先に DB で `cancelled` を原子的に確定してから停止を通知する。
    終端状態（completed/failed/cancelled/expired）への要求は何もせず今の状態を返す。
    取消と完了が競合したときは、先に DB 行を `running` から動かした方が勝つ（取消したのに結果が返る・完了したのに cancelled と返る、のどちらも起こさない）。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.codex_job_cancel", "ext_codex_job") as audit:
        audit.resource_id = job_id
        job = store_jobs.get_job(job_id)
        if not _codex_job_visible(job, key):
            raise _codex_job_not_found()
        audit.detail.update({"world": job["world"], "status_before": job["status"]})
        if job["status"] == "queued":
            if store_jobs.cancel_if_queued(job_id):
                return {"status": "cancelled"}
            # 競合でこの一瞬の間に running 等へ進んだ場合は下の再読込へ進む
            job = store_jobs.get_job(job_id) or job
        if job["status"] == "running":
            # DB 側の確定が先。ワーカーの `mark_completed`/`mark_failed` と同じ `WHERE status='running'` 条件で競い、勝った側だけが行を書き換える
            if store_jobs.mark_cancelled(job_id, from_status="running"):
                codex_jobs_worker.signal_cancel(job_id)  # 確定後に停止を通知（best-effort）
            job = store_jobs.get_job(job_id) or job
        job = store_jobs.expire_job_if_due(job_id, job)
        audit.detail["status_after"] = job["status"]
        return {"status": job["status"]}


# ==== GET /ext/v1/capabilities（discovery）====

class ExtCapability(BaseModel):
    configured: bool
    available: bool
    reason: str | None = None


class ExtCapabilities(BaseModel):
    search: ExtCapability
    answer: ExtCapability
    codex_jobs: ExtCapability
    embed: ExtCapability
    convert: ExtCapability


class ExtWorldInfo(BaseModel):
    world: str
    document_count: int | None = None
    last_updated: str | None = None
    capabilities: ExtCapabilities


class ExtCapabilitiesRes(BaseModel):
    worlds: list[ExtWorldInfo]


def _cap(configured: bool, available: bool, reason: str | None = None) -> dict:
    """`reason` は configured/available のどちらかが偽のときだけ付ける。"""
    out: dict = {"configured": configured, "available": available}
    if not (configured and available):
        out["reason"] = reason or ("not_configured" if not configured else "backend_unreachable")
    return out


def _answer_capability() -> dict:
    """`/ext/v1/answer` の既定 AI（管理者設定）が解決でき、接続材料（鍵・接続先）が揃うか。"""
    try:
        ss = store.get_system_settings()
        provider, _model = simple_chat.resolve_model_and_provider(None, ss)
        if provider == "openai":
            simple_chat._connect_openai(ss)
        else:
            simple_chat._connect_ollama(ss)
    except Exception:
        return _cap(False, False, "not_configured")
    return _cap(True, True)


def _capability_set(codex: dict, answer: dict) -> dict:
    """資料フォルダごとの機能一覧（5つ固定）。`available` は管理者の構成（`configured`）と接続の可否で決まる。検索は常に実行可能、変換は管理者設定を持たず、埋め込みは未設定として返す。"""
    return {"search": _cap(True, True), "answer": dict(answer), "codex_jobs": dict(codex),
            "embed": _cap(False, False, "not_configured"), "convert": _cap(True, True)}


_CAPABILITIES_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("入力パラメータが不正な場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "world 一覧を確認できませんでした（一時的な障害の可能性があります）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


# `reason` は省略されうる項目（未指定は出力しない）、`document_count`/`last_updated` は明示の null を返す
@router.get("/capabilities", response_model=ExtCapabilitiesRes, response_model_exclude_unset=True,
            responses=_CAPABILITIES_RESPONSES)
def ext_capabilities(request: Request, key: dict = Depends(require_api_key),
                     x_request_id: str | None = _XRequestIdIn):
    """discovery: 資料フォルダごとに `{world, document_count, last_updated, capabilities}` を返す（機能は search/answer/codex_jobs/embed/convert の5つ固定・各 `{configured, available, reason?}`）。

    `key["allowed_worlds"]` が非 None ならそのスコープ内の world だけを返す（スコープ外 world の存在を漏らさない）。
    `document_count` は取り込み成功確定時に記録された事前集計値、`last_updated` も取り込み成功確定の時刻で、未確定（一度も成功同期していない）なら `null`（0 に潰さない）。
    ファイルツリーは走査しない。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.capabilities", "ext_capabilities") as audit:
        try:
            registry_rows = {r["world_id"]: r for r in store.list_worlds_db()}
        except Exception as e:
            raise HTTPException(
                503, "world 一覧を確認できませんでした（一時的な障害の可能性があります）") from e
        try:
            fs_ids = worlds.discover_fs_world_ids_strict()
        except worlds.ExternalResolverError as e:
            raise HTTPException(
                503, "world 一覧を確認できませんでした（一時的な障害の可能性があります）") from e
        ids = sorted(set(registry_rows) | set(fs_ids))
        allowed = key.get("allowed_worlds")
        ids = [w for w in ids if allowed is None or w in allowed]
        out = []
        caps = None  # 機能の判定は1リクエストで1回だけ（資料フォルダごとに繰り返さない）
        for wid in ids:
            row = registry_rows.get(wid)  # 同一スナップショットのみ参照（再照会しない）
            # row が None なら「未登録」として fixtures/dev のみ試す
            try:
                res = worlds.resolve_external_world(wid, registry_row=row)
            except worlds.ExternalResolverError as e:
                if row is not None:
                    # 登録済み world の root に到達できないものを静かに外すと「実在しない」と区別が付かないため、503 にする。fixtures/dev のみの未登録候補（row is None）は静かに外す
                    raise HTTPException(
                        503, "world の実在を確認できませんでした（一時的な障害の可能性があります）") from e
                continue
            if res.status != "ok":
                continue
            doc_count = None
            last_updated = None
            if row and row.get("last_synced_at") and row.get("last_sig"):
                # last_sig が空＝取り込み開始時の pre-invalidate のまま（進行中/未確定）。非空＝成功確定後の署名で、確定済みのときだけ値を報告する
                last_updated = str(row["last_synced_at"])
                doc_count = row.get("last_doc_count")
            if caps is None:
                caps = (_cap(*codex_jobs_worker.capability()), _answer_capability())
            out.append({"world": wid, "document_count": doc_count, "last_updated": last_updated,
                        "capabilities": _capability_set(*caps)})
        audit.detail["result_count"] = len(out)
        return {"worlds": out}


# ==== GET /ext/v1/doc（原本取得）====

_DOC_MAX_BYTES = int(os.environ.get("SHERPA_EXT_DOC_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MiB
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"  # 旧バイナリ Office（OLE2/CFB）共通シグネチャ
# ソース原文（コード）の拡張子はアナライザ登録簿が真実源
_UTF8_DECLARE_EXT = {".md", ".markdown", ".txt"} | _analyzer_registry.registered_extensions()
_UTF8_VALIDATE_CAP = 65536  # charset=utf-8 の宣言判定はファイル全体がこの上限以下のときだけ行う

# 拡張子→固定 Content-Type。legacy Office は含めず application/octet-stream 固定（CFB の中身は判別しない）。ソース原文はすべて text/plain
_DOC_CONTENT_TYPE = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    ".pptx": "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg",
    ".gif": "image/gif", ".bmp": "image/bmp", ".tif": "image/tiff", ".tiff": "image/tiff",
    ".md": "text/markdown", ".markdown": "text/markdown", ".txt": "text/plain",
    **{ext: "text/plain" for ext in _analyzer_registry.registered_extensions()},
}

_DOC_RESPONSES = {
    200: {
        "description": "原本ファイル（バイナリ・Content-Type は拡張子ごとの固定値。legacy Office"
                       "〔.doc/.xls/.ppt〕は application/octet-stream 固定で、中身からの形式判別はしない）",
        # 実際に返しうる全 Content-Type を列挙する（`_DOC_CONTENT_TYPE` が真実源）。legacy Office・辞書に無い拡張子は application/octet-stream
        "content": {ct: {"schema": {"type": "string", "format": "binary"}}
                   for ct in sorted({*_DOC_CONTENT_TYPE.values(), "application/octet-stream"})},
        "headers": {
            **_REQUEST_ID_OPENAPI_HEADER,
            "Content-Disposition": {"schema": {"type": "string"}},
        },
    },
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "文書が見つかりません（存在しない／範囲外／対応していない種別）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    413: {"description": "ファイルサイズが上限（既定50MiB）を超えています",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    415: {"description": "ファイルの内容が拡張子と一致しません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("world/path の形式が不正な場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "資料フォルダの参照先を確認できませんでした（一時的な障害）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


# ---- マジック検証 ----
# - legacy Office（.doc/.xls/.ppt）は CFB ヘッダの健全性のみ確認する（stream 列挙・形式判別・入れ子解析はしない）。Content-Type は application/octet-stream 固定。
# - OOXML は EOCD（末尾の central directory 終端レコード）＋central directory 自体を bounded に検証し（メンバ数上限・ZIP64・multi-disk・境界整合を含む）、
#   メンバー名もその検証済みの走査から直接得る（`zipfile.ZipFile` に central directory を再解析させない）

_IMAGE_MAGIC_BY_EXT = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    # TIFF: classic（version 42）と BigTIFF（version 43）の両方を受理する
    ".tif": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
    ".tiff": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
}

_OOXML_MAIN_PART = {
    ".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}

_ZIP_EOCD_SIG = b"PK\x05\x06"
_ZIP_EOCD_SIZE = 22
_ZIP_EOCD_MAX_COMMENT = 65535  # ZIP スペック上の comment 長の上限（2 バイトフィールド）
_ZIP64_SENTINEL = 0xFFFF  # EOCD の 16bit entry 数フィールドがこの値＝ZIP64（別レコード）
_ZIP64_SENTINEL32 = 0xFFFFFFFF  # EOCD の 32bit cd_size/cd_offset がこの値＝同上
_ZIP_CD_ENTRY_SIG = b"PK\x01\x02"  # central directory file header の署名
_ZIP_CD_ENTRY_FIXED_SIZE = 46  # 可変長フィールド（filename/extra/comment）より前の固定部

# 複数 EOCD 候補を右から左へ試す際の合算上限。候補数・全候補合算の entry 数・pread バイト数のいずれかを超えたら、それ以上候補を試さずアーカイブ全体を拒否する（偽 EOCD を並べた DoS 対策）
_ZIP_MAX_EOCD_CANDIDATES = 8
_ZIP_MAX_TOTAL_ENTRIES_WALKED = 20_000
_ZIP_MAX_TOTAL_BYTES_READ = 8 * 1024 * 1024  # 8MiB


class _ZipScanBudget:
    """複数の EOCD 候補を試す際の合算走査量（候補数・entry 数・pread バイト数）を数える実測カウンタ。

    いずれかの上限を超えると `exceeded=True` になり、以後の `note_*()` は全て False を返す（呼び出し元は候補の構造不正による次候補への進行とは区別して全体を拒否する）。
    """

    __slots__ = ("candidates_tried", "entries_walked", "bytes_read", "exceeded")

    def __init__(self) -> None:
        self.candidates_tried = 0
        self.entries_walked = 0
        self.bytes_read = 0
        self.exceeded = False

    def note_candidate(self) -> bool:
        self.candidates_tried += 1
        if self.candidates_tried > _ZIP_MAX_EOCD_CANDIDATES:
            self.exceeded = True
        return not self.exceeded

    def note_entry(self) -> bool:
        self.entries_walked += 1
        if self.entries_walked > _ZIP_MAX_TOTAL_ENTRIES_WALKED:
            self.exceeded = True
        return not self.exceeded

    def note_read(self, n: int) -> bool:
        self.bytes_read += n
        if self.bytes_read > _ZIP_MAX_TOTAL_BYTES_READ:
            self.exceeded = True
        return not self.exceeded


def _iter_eocd_candidates(tail: bytes):
    """`tail`（ファイル末尾の bounded read）の中の `PK\x05\x06` 出現を右から左へ `(idx, eocd)` として yield する。

    申告された comment_len が実際に EOF まで一致する（EOCD として形が成立しうる）候補だけを yield する。central directory との整合は検証せず、
    呼び出し元（`_zip_bounded_check_names`）が候補ごとに同一の parser で最後まで検証して最初に通過したものを採用する。
    """
    end = len(tail)
    while True:
        idx = tail.rfind(_ZIP_EOCD_SIG, 0, end)
        if idx == -1:
            return
        if len(tail) - idx >= _ZIP_EOCD_SIZE:
            eocd = tail[idx:idx + _ZIP_EOCD_SIZE]
            comment_len = struct.unpack_from("<H", eocd, 20)[0]
            if idx + _ZIP_EOCD_SIZE + comment_len == len(tail):
                yield idx, eocd
        end = idx


_ZIP_CD_DISK_NUMBER_OFFSET = 34  # disk number where file starts（2バイト）
_ZIP_CD_COMPRESSED_SIZE_OFFSET = 20  # compressed size（4バイト）
_ZIP_CD_UNCOMPRESSED_SIZE_OFFSET = 24  # uncompressed size（4バイト）
_ZIP_CD_LOCAL_HEADER_OFFSET_OFFSET = 42  # local header offset（4バイト）
_ZIP64_EXTRA_TAG = 0x0001  # extra field 内の ZIP64 拡張情報サブレコードの tag


def _cd_entry_extra_has_zip64(extra: bytes) -> bool:
    """central directory entry の "extra" フィールドに ZIP64 拡張情報（tag `0x0001`）が含まれるか。サブレコード列が壊れている場合も True（安全側＝拒否）。"""
    pos, n = 0, len(extra)
    while pos + 4 <= n:
        tag, size = struct.unpack_from("<HH", extra, pos)
        if tag == _ZIP64_EXTRA_TAG:
            return True
        pos += 4 + size
    return pos != n


def _zip_count_central_directory_entries(
    fd: int, cd_offset: int, cd_size: int, budget: _ZipScanBudget
) -> tuple[int, frozenset[bytes]] | None:
    """central directory（`cd_offset` から `cd_size` バイト）を1件ずつ上限付き exact-read で逐次走査し、実際の entry 数と各 entry のファイル名（生バイト列）を返す。

    `budget` は複数候補にまたがって共有する合算カウンタ。`note_entry()` は各 entry のループ先頭（pread の前）で、pread ごとに `note_read()` を呼び、
    合算上限を超えたら即座に None を返す（呼び出し元が `budget.exceeded` を見て他の候補も試さない）。
    次のいずれかで None（拒否）: signature 不一致・可変長フィールドの合計が `cd_size` と不整合・`_ZIP_MAX_MEMBERS` 超過／`disk number start != 0`／
    `compressed_size`/`uncompressed_size`/`local_header_offset` のいずれかが `0xFFFFFFFF`（ZIP64 sentinel）／extra に ZIP64 拡張情報／合算走査量が上限超過。
    """
    if cd_size == 0:
        return 0, frozenset()
    pos = 0
    count = 0
    names: set[bytes] = set()
    while pos < cd_size:
        if not budget.note_entry():
            return None  # 合算 entry 数上限に既に達している＝この entry の pread は行わない
        if cd_size - pos < _ZIP_CD_ENTRY_FIXED_SIZE:
            return None
        if not budget.note_read(_ZIP_CD_ENTRY_FIXED_SIZE):
            return None
        header = os.pread(fd, _ZIP_CD_ENTRY_FIXED_SIZE, cd_offset + pos)
        if len(header) != _ZIP_CD_ENTRY_FIXED_SIZE or header[:4] != _ZIP_CD_ENTRY_SIG:
            return None
        compressed_size = struct.unpack_from("<I", header, _ZIP_CD_COMPRESSED_SIZE_OFFSET)[0]
        uncompressed_size = struct.unpack_from("<I", header, _ZIP_CD_UNCOMPRESSED_SIZE_OFFSET)[0]
        disk_number_start = struct.unpack_from("<H", header, _ZIP_CD_DISK_NUMBER_OFFSET)[0]
        local_header_offset = struct.unpack_from("<I", header, _ZIP_CD_LOCAL_HEADER_OFFSET_OFFSET)[0]
        n, m, k = struct.unpack_from("<HHH", header, 28)
        if disk_number_start != 0:
            return None  # multi-disk archive は拒否
        if _ZIP64_SENTINEL32 in (compressed_size, uncompressed_size, local_header_offset):
            return None  # ZIP64 sentinel（実値は extra フィールド）
        if cd_size - pos - _ZIP_CD_ENTRY_FIXED_SIZE < n + m + k:
            return None  # 可変長フィールドの合計が cd_size をはみ出す
        if n > 0:
            if not budget.note_read(n):
                return None
            fname = os.pread(fd, n, cd_offset + pos + _ZIP_CD_ENTRY_FIXED_SIZE)
            if len(fname) != n:
                return None
            names.add(fname)
        if m > 0:
            if not budget.note_read(m):
                return None
            extra = os.pread(fd, m, cd_offset + pos + _ZIP_CD_ENTRY_FIXED_SIZE + n)
            if len(extra) != m or _cd_entry_extra_has_zip64(extra):
                return None
        pos += _ZIP_CD_ENTRY_FIXED_SIZE + n + m + k
        count += 1
        if count > _ZIP_MAX_MEMBERS:
            return None
    return (count, frozenset(names)) if pos == cd_size else None


def _zip_bounded_check_names(fd: int, size: int) -> frozenset[bytes] | None:
    """EOCD の候補を右から左へ順に試し、同一 parser で EOCD の全フィールドと central directory 自体（`_ZIP_MAX_MEMBERS` 超・ZIP64・multi-disk・`cd_offset+cd_size` の境界・実 entry 数の一致）を完全に検証できた最初の候補を採用し、そのメンバー名の集合を返す（どの候補も通過しなければ None＝アーカイブ全体を拒否）。

    rightmost が central directory と整合しない場合も、全体拒否せず次の候補（さらに左）を試す。EOCD の自己申告件数は信用せず、central directory を走査して実 entry 数を確定してから突き合わせる。
    `budget` は central directory walker を呼ぶ直前（EOCD フィールドの定数時間チェックを通過した候補）にだけ候補数を課金し、合算上限を超えたら即座にアーカイブ全体を拒否する。
    """
    if size < _ZIP_EOCD_SIZE:
        return None
    tail_size = min(size, _ZIP_EOCD_SIZE + _ZIP_EOCD_MAX_COMMENT)
    tail = os.pread(fd, tail_size, size - tail_size)
    budget = _ZipScanBudget()
    for idx, eocd in _iter_eocd_candidates(tail):
        eocd_abs_offset = (size - tail_size) + idx
        disk_number, disk_with_cd, entries_this_disk, total_entries = struct.unpack_from(
            "<HHHH", eocd, 4)
        cd_size, cd_offset = struct.unpack_from("<II", eocd, 12)
        if disk_number != 0 or disk_with_cd != 0 or entries_this_disk != total_entries:
            continue  # multi-disk は不採用
        if (total_entries == _ZIP64_SENTINEL or cd_size == _ZIP64_SENTINEL32
                or cd_offset == _ZIP64_SENTINEL32):
            continue  # ZIP64 sentinel（実値は別レコード）
        if total_entries > _ZIP_MAX_MEMBERS:
            continue
        if cd_offset + cd_size != eocd_abs_offset:
            continue  # central directory は EOCD の直前で終わっているはず（prepended data 等は不採用）
        if not budget.note_candidate():
            return None  # 候補数の合算上限を超過＝アーカイブ全体を拒否
        walked = _zip_count_central_directory_entries(fd, cd_offset, cd_size, budget)
        if walked is None:
            if budget.exceeded:
                return None  # entry 数/バイト数の合算上限を超過
            continue
        actual_count, names = walked
        if actual_count == total_entries:
            return names
    return None


def _zip_bounded_check(fd: int, size: int) -> bool:
    """`_zip_bounded_check_names` の合否のみを返す薄いラッパー。"""
    return _zip_bounded_check_names(fd, size) is not None


def _ooxml_magic_ok(fd: int, ext: str, size: int) -> bool:
    """OOXML（.docx/.xlsx/.pptx）: EOCD と central directory の bounded 検査を通過し、検証済みの走査で得たメンバー名が `[Content_Types].xml` と形式固有 main part を含むか。
    `zipfile.ZipFile` に central directory を再解析させない。fd は `os.pread` のみで動かす。
    """
    main_part = _OOXML_MAIN_PART.get(ext)
    if main_part is None:
        return False
    names = _zip_bounded_check_names(fd, size)
    if names is None:
        return False
    return b"[Content_Types].xml" in names and main_part.encode("ascii") in names


def _legacy_office_header_ok(data: bytes) -> bool:
    """.doc/.xls/.ppt: CFB（[MS-CFB]）ヘッダの署名＋version/byte order/sector shift の健全性だけを見る。OLE2 でない場合は旧形式とみなし拒否しない（拡張子で既にゲート済み）。"""
    if data[:8] != _OLE2_MAGIC:
        return True
    if len(data) < 512:
        return False  # 512 バイトヘッダ未満＝壊れている
    try:
        minor, major = struct.unpack_from("<HH", data, 24)
        byte_order = struct.unpack_from("<H", data, 28)[0]
        sector_shift = struct.unpack_from("<H", data, 30)[0]
    except struct.error:
        return False
    del minor
    if byte_order != 0xFFFE:
        return False
    if major == 3:
        return sector_shift == 9  # 512 バイトセクタ
    if major == 4:
        return sector_shift == 12  # 4096 バイトセクタ
    return False


def _doc_magic_ok(fd: int, ext: str, header: bytes, size: int) -> bool:
    """拡張子ごとに形式固有の検証を行う（バイナリ形式のみ。text 系はマジック無しのため対象外）。"""
    if ext in office_md.PDF_EXT:
        return header.startswith(b"%PDF-")
    if ext in office_md.CONVERTIBLE_EXT:
        return _ooxml_magic_ok(fd, ext, size)
    if ext in office_md.LEGACY_OFFICE_EXT:
        return _legacy_office_header_ok(os.pread(fd, 512, 0))
    if ext in office_md.IMAGE_EXT:
        sigs = _IMAGE_MAGIC_BY_EXT.get(ext)
        return bool(sigs) and any(header.startswith(s) for s in sigs)
    return True


def _looks_utf8(data: bytes) -> bool:
    """厳密な UTF-8 検証（incremental decoder・全文）。ファイル全体が上限（64KiB）以下のときだけ使う（打ち切った途中経過だと末尾の不完全な多バイト列を不正判定する）。"""
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        decoder.decode(data, final=True)
    except UnicodeDecodeError:
        return False
    return True


def _content_type_for(ext: str, fd: int, size: int) -> str:
    """text 系（charset=utf-8 を宣言する種別）はファイル全体が上限以下の場合のみ実データを検証し、妥当でなければ charset 宣言を外す。上限超過（打ち切り）は検証していないため charset を宣言しない。"""
    base = _DOC_CONTENT_TYPE.get(ext, "application/octet-stream")
    if ext in _UTF8_DECLARE_EXT and size <= _UTF8_VALIDATE_CAP:
        sample = os.pread(fd, size, 0)
        if _looks_utf8(sample):
            return f"{base}; charset=utf-8"
    return base


def _doc_path_segments(path: str) -> tuple | None:
    """`path`（query）→ 検証済み POSIX セグメント列。

    `corpus_docs.status_document_reachable` と同じ正規化（`world_graph.valid_rel_parts`＝絶対パス・`\\`・NUL・空/`.`/`..` 要素の拒否）を共有する
    （判定側と配信側で検証条件が分かれると秘匿ファイル・未対応種別の漏洩経路になる）。配信可否は `status_document_reachable` の `True` 確定だけで判定し、判定不能（`None`）は拒否する（fail-closed）。
    """
    from .ingest.world_graph import valid_rel_parts
    return valid_rel_parts(path)


@router.get("/doc", response_class=StreamingResponse, responses=_DOC_RESPONSES)
def ext_doc(request: Request, world: str = Query(..., min_length=1, max_length=100),
           path: str = Query(..., min_length=1, max_length=4096),
           key: dict = Depends(require_api_key), x_request_id: str | None = _XRequestIdIn):
    """根拠の原本DL。

    symlink を拒否し、world の root の外は開かない。対応する種別で、先頭バイトが拡張子と整合するファイルだけを返す。
    検証から配信までは同じ開いたファイルから読み、途中でパスを解決し直さない。
    取り込んだアーカイブ（zip/tar(.gz)/tgz）の中のファイルは、展開した写しを返す。
    既定で 50MiB を超えるファイルは 413。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.doc", "ext_doc") as audit:
        audit.resource_id = world
        audit.detail.update({"world": world, "path": path})
        _enforce_world_scope(request, key, world)  # scope 外は世界の存在有無を明かさず先に 403
        ext = Path(path).suffix.lower()
        # `allow_content_sniff=True`（単発の解決のため軽量テキスト枠の第2段も内容を読んで判定する）。`status_document_reachable` が `True` のときだけ配信し、
        # 判定不能（`None`）は拒否する（fail-closed。配信側の open 経路は判定側と長さ制約が異なるため）
        if corpus_docs.status_document_reachable(path, world, allow_content_sniff=True) is not True:
            raise HTTPException(404, "対応していない種別、または文書が見つかりません")
        parts = _doc_path_segments(path)
        if parts is None:
            raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
        root = _resolve_world_or_error(world)
        try:
            fd = safe_open.open_file_nofollow_walk(root, parts)
        except OSError:
            # アーカイブ取り込みの中のファイルは原本ツリーに実在しないため、`documents.resolve` と同じ優先順位（原本→展開先）で展開先（`worlds.archives_dir`）を anchor にした O_NOFOLLOW walk を再試行する
            archives_root = worlds.archives_dir(world)
            if not archives_root.is_dir():
                raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
            try:
                fd = safe_open.open_file_nofollow_walk(archives_root, parts)
            except OSError:
                raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
        try:
            st = os.fstat(fd)
            if not stat.S_ISREG(st.st_mode):
                raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
            size_bytes = st.st_size
            if size_bytes > _DOC_MAX_BYTES:
                raise HTTPException(
                    413, f"ファイルサイズが上限（{_DOC_MAX_BYTES // 1024 // 1024}MiB）を超えています")
            header = os.pread(fd, 16, 0)
            if not _doc_magic_ok(fd, ext, header, size_bytes):
                raise HTTPException(415, "ファイルの内容が拡張子と一致しません")
            media_type = _content_type_for(ext, fd, size_bytes)
        except Exception:  # HTTPException も含め、検証失敗時は配信前なので fd をここで閉じる
            os.close(fd)
            raise
        audit.detail["size_bytes"] = size_bytes
        headers = {
            # Content-Type はここで確定させ media_type=None で渡す（starlette が `text/` 始まりに自動で charset を足し、`_content_type_for` の「charset を宣言しない」判断を上書きするのを避ける）
            "Content-Type": media_type,
            "Content-Disposition": content_disposition(Path(path).name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        return FdFileResponse(FdOwner(fd), size_bytes, st.st_mtime, media_type=None, headers=headers)


# ==== GET /ext/v1/openapi.json ====

def _ext_openapi_subset(app) -> dict:
    """`app.openapi()` から `/ext/v1` 配下（admin・利用者キー管理を除く）のパスと、そこから到達可能な `components.schemas` だけを抜いた OpenAPI 文書。
    Dify カスタムツールに直接インポート可能。X-API-Key 認証のルート（convert/search/capabilities/doc/answer・Codex ジョブ受付/状態/結果/取消の4本）のみを含める。
    """
    full = app.openapi()
    paths = {p: v for p, v in full.get("paths", {}).items()
             if p.startswith("/ext/v1/") and not p.startswith("/ext/v1/admin")
             and not p.startswith("/ext/v1/keys")
             and p != "/ext/v1/openapi.json"}
    schemas = (full.get("components") or {}).get("schemas") or {}

    def _collect(obj, out: set):
        if isinstance(obj, dict):
            r = obj.get("$ref")
            if isinstance(r, str) and r.startswith("#/components/schemas/"):
                out.add(r.rsplit("/", 1)[1])
            for v in obj.values():
                _collect(v, out)
        elif isinstance(obj, list):
            for v in obj:
                _collect(v, out)

    need: set = set()
    _collect(paths, need)
    while True:
        more: set = set()
        for name in need:
            _collect(schemas.get(name, {}), more)
        if more <= need:
            break
        need |= more
    return {
        "openapi": full["openapi"],
        "info": {"title": "Sherpa External API", "version": "1.0.0",
                 "description": "MD変換・RAG検索の外部連携 API（X-API-Key 認証）"},
        "paths": paths,
        "components": {
            "schemas": {n: schemas[n] for n in sorted(need) if n in schemas},
            "securitySchemes": {"ApiKeyAuth": {"type": "apiKey", "in": "header", "name": "X-API-Key"}},
        },
        "security": [{"ApiKeyAuth": []}],
    }


@router.get("/openapi.json", include_in_schema=False)
def ext_openapi(request: Request, key: dict = Depends(require_api_key)):
    with _AuditScope(request, "ext_api.openapi", "ext_openapi") as audit:
        doc = _ext_openapi_subset(request.app)
        audit.detail["result_count"] = len(doc.get("paths") or {})
        return doc
