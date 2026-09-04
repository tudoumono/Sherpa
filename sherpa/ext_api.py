"""外部連携 API（`/ext/v1`）— APIキー認証基盤・決定的変換・エンジン分離検索＋RRF融合・
discovery・原本取得・キーの world スコープ。

docs/proposals/2026-07-07-外部API化とDify.md／docs/proposals/2026-08-24-部品API設計.md の
スコープを実装する（sync/run照会は不要と判断・実装しない。キー単位のレート制限は
`_verify_key_sync` 内の `sherpa.ratelimit.check_ext_api_rate_limit` 呼び出しで別途実装済み）。

キー認証系（convert・search・capabilities・doc・openapi）はこの router に集約し、`api.py` から
`app.include_router()` する。admin キー発行/失効の3本だけは既存流儀どおり `api.py`（実体は
`sherpa/routers/system_extras.py`）に直付け（セッション Cookie 認証 `_current_user`/
`_require_admin` が必要なため）——ただし監査は `start_audit()` を通じて本ファイルの
`ExtRequestMiddleware` と同じ request-level 監査へ統合する。

**このファイルは `sherpa.api`・`sherpa.agents`・`sherpa.chat_service`・`sherpa.chat_router`・
`sherpa.grep_tool` を import しない**（循環回避＋共有KBのみの契約）。

convert は stateless: KB・台帳・ES・Neo4j には一切書き込まない。アーム/マージ機構は呼ばない
（`office_md.to_markdown` の決定的変換のみ）。ファイルは一時領域にのみ書き、必ず削除する。
OpenAI へのファイル永続化なし（LLM を一切呼ばない）。

search は `sherpa.search_service`（keyword=ES BM25 / vector=ES 純kNN / graph=Neo4j 影響たどり）へ
委譲するだけ（世界の存在検証・scope 妥当性検証はここで行う）。エンジン単位の不可は 200＋`degraded[]`
（API 契約・黙ってすり替えない）。world 解決は `worlds.resolve_external_world`（registry 不達・
登録 root 不達は 503・fixtures/dev フォールバックは registry 到達時のみ）で1回だけ行い、その
`root` を `scope.valid_scope_paths`／`search_service.search` へ引き回す（preflight 後の再解決禁止）。

doc（原本DL）は `worlds.resolve_external_world` で解決した world root を起点に、
`sherpa.safe_open` の symlink 差し替え耐性 open（`/` から O_NOFOLLOW 一段ずつ dir_fd 相対で
辿る）で得た fd 1本だけを使って検証（種別・マジック・サイズ）から配信まで行う。個人 workspace
は world root の外＝解決対象にならない（契約どおり出せない）。legacy Office（.doc/.xls/.ppt）は
CFB ヘッダの健全性のみ検証する（stream 列挙・形式判別はしない——配信元は登録済み world＝
信頼済みコーパスであり、深い形式判別は脅威モデル過剰という裁定）。

`ExtRequestMiddleware`（`sherpa.api` が `app.add_middleware()` で装着する生 ASGI ミドルウェア・
`/ext/v1/*` にのみ関与）が X-Request-Id の解決と応答ヘッダ付与、`contextvars.ContextVar`＋
`logging.Filter` によるアプリログへの request_id 束縛、および認証成功後の1リクエスト=1行監査
（`request.state.audit_pending` を読んで実際に観測した応答ステータスで書く・DB書込は専用
writer スレッド経由・hash-chain は DB 側で直列化されるため並列化はしない）を一元的に行う。
詳細は `ExtRequestMiddleware` の docstring 参照。下流
（agentic search の実行イベント等）への request_id 伝播は
docs/proposals/2026-08-24-部品API設計.md §8 のとおり PART-4 側のスコープ（ここでは行わない）。
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
    Security,
    UploadFile,
)
from fastapi.responses import StreamingResponse
from fastapi.security import APIKeyHeader
from pydantic import BaseModel, Field, field_validator
from starlette.concurrency import run_in_threadpool

from sherpa import corpus_docs, ratelimit, research_service, safe_open, scope_infer, search_service, store, worlds
from sherpa import scope as scope_mod
from sherpa.fd_response import FdFileResponse, FdOwner, content_disposition
from sherpa.ingest import office_md
from sherpa.ingest.analyzers import registry as _analyzer_registry

_log = logging.getLogger("sherpa")

router = APIRouter(prefix="/ext/v1", tags=["外部連携API"])

_KEY_PREFIX = "sk-ext-"

_CONVERT_MAX_BYTES = int(os.environ.get("SHERPA_EXT_CONVERT_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MB
_ZIP_MAX_UNCOMPRESSED = 500 * 1024 * 1024   # zip爆弾: 展開合計上限 500MB
_ZIP_MAX_RATIO = 200                        # zip爆弾: 圧縮率上限
_ZIP_MAX_MEMBERS = 10_000                   # zip爆弾: メンバ数上限（EOCD の bounded 検査にも使う）
_ZIP_EXTS = {".docx", ".xlsx", ".pptx"}     # OOXML＝zip コンテナ
# convert が受理する拡張子（拡張子から method を決定的に導出）。
_ALLOWED_EXT = office_md.CONVERTIBLE_EXT | office_md.PDF_EXT   # {.docx,.xlsx,.pptx} | {.pdf}

# 監査の分類語彙（web/audit.html のフィルタ契約と一致させる・定数で固定する）。
_OUTCOME_SUCCESS = "success"
_OUTCOME_DENY = "deny"
_OUTCOME_ERROR = "error"
_SEVERITY_INFO = "info"
_SEVERITY_WARNING = "warning"


# ==== X-Request-Id（応答ヘッダの共通契約・OpenAPI にも宣言する）====

_REQUEST_ID_HEADER = "X-Request-Id"
_REQUEST_ID_MAX_LEN = 200
# fullmatch 専用（^/$ アンカーは使わない）: `$` は「文字列末尾」だけでなく「末尾の改行の直前」にも
# マッチするため、`re.match(r"^...$")` は末尾に LF が付いた値（ヘッダ注入の典型形）を誤って通す。
# `fullmatch()` は文字列全体を対象にするためこの抜け穴が無い。
_REQUEST_ID_RE = re.compile(r"[A-Za-z0-9._-]+")
_REQUEST_ID_OPENAPI_HEADER = {
    "X-Request-Id": {"schema": {"type": "string"}, "description": "リクエスト追跡ID"}}
# 422 は自動バリデーション（FastAPI 標準の HTTPValidationError 形）とハンドラ内のドメインエラー
# （`{"detail": "文字列"}`）の両方があり得る。`responses=` で 422 を上書きすると FastAPI の
# 既定 content（HTTPValidationError への $ref）が消えるため、X-Request-Id ヘッダを足す時は
# この content も明示的に道連れにする（自動バリデーションの形を仕様書から消さない）。
_VALIDATION_ERROR_CONTENT = {
    "application/json": {"schema": {"$ref": "#/components/schemas/HTTPValidationError"}}}


def _validation_error_response(extra_description: str) -> dict:
    return {"description": f"入力値が不正です（自動バリデーション、または {extra_description}）",
            "content": dict(_VALIDATION_ERROR_CONTENT), "headers": dict(_REQUEST_ID_OPENAPI_HEADER)}


# 入力側（受け入れる X-Request-Id）を OpenAPI に明示するための共通 Header() 宣言。実際の解決/検証は
# `ExtRequestMiddleware` が routing より前に行う（ここでの受け取りは仕様書に載せるための宣言のみ）。
# 長さ制約はここには付けない: FastAPI 自身の Header() 検証は `ExtRequestMiddleware` の寛容な
# フォールバック（不正/長すぎる値は黙って採番）とは独立に動くため、ここで `max_length` を付けると
# 不正な X-Request-Id を送っただけで本来通したいリクエスト全体が 422 で弾かれてしまう
# （実測済みの回帰・ミドルウェア側が唯一の検証者であるべき）。
_XRequestIdIn = Header(
    default=None, alias="X-Request-Id",
    description="呼び出し元が指定するリクエスト追跡ID（省略時は採番される・不正な値は無視して採番）")

# アプリログへ request_id を束縛するための ContextVar。`ExtRequestMiddleware` が要求ごとに
# set/reset する。監査 DB とは別系統＝障害調査でログと監査行を同じ ID で突き合わせるためのもの。
_request_id_ctx: contextvars.ContextVar[str | None] = contextvars.ContextVar("ext_request_id", default=None)


class _RequestIdLogFilter(logging.Filter):
    """`_request_id_ctx` の現在値を `record.request_id` として付与する（**属性が既に在れば
    上書きしない**＝冪等・`extra={"request_id": ...}` を明示的に渡す呼び出しと衝突しない）。

    **handler へ付与する**（logger へは付与しない）: `Logger.addFilter()` はそのロガー自身へ
    直接出されたレコードにしか効かず、子ロガー（`logging.getLogger(__name__)` で作る
    `"sherpa.ingest.office_md"` 等）には継承されない（フィルタはロガー階層を伝播せず、
    ハンドラだけが親へ伝播する仕様——`Logger.callHandlers()` が祖先の handler を辿って
    `Handler.handle()` を呼び、handler 自身の filter はそこで評価される）。`Handler.addFilter()`
    はその handler に実際に届いた全レコードに効くため、logger 名を問わず一律に効く。

    `logging.setLogRecordFactory()` によるグローバル差し替えは採用していない: プロセス全体・
    永続的な副作用を持ち、lifespan 終了時の復元が無く、再import/reload で wrapper が
    多重化しうる上、他モジュールが `extra={"request_id": ...}` を渡すと factory が既に
    同名属性を作っているため `KeyError`（属性重複）になりうる問題がある。
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if not hasattr(record, "request_id"):
            record.request_id = _request_id_ctx.get() or "-"
        return True


_request_id_log_filter = _RequestIdLogFilter()


@contextlib.contextmanager
def _logging_module_lock():
    """`logging` モジュール内部の直列化 lock（`getLogger()`/`addHandler()` 等が使うのと同じ
    もの）を、Python バージョン間の API 差異を吸収した context manager として貸す。

    Python 3.13 で `logging._acquireLock()`/`_releaseLock()`（関数形式）が撤去され、
    `logging._lock`（`RLock` 本体）を直接 `acquire()`/`release()`（または `with` 文）する
    必要がある。`getattr(logging, "_acquireLock", None)` で feature-detect し、**呼び出し時**
    （import 時ではない）にどちらの分岐を使うか決める——`import logging` した時点でこの
    判定自体が例外で落ちないようにするため、モジュール属性の有無を見るだけに留める。
    """
    acquire = getattr(logging, "_acquireLock", None)
    release = getattr(logging, "_releaseLock", None)
    if acquire is not None and release is not None:
        acquire()
        try:
            yield
        finally:
            release()
    else:
        # Python 3.13+: _acquireLock()/_releaseLock() 撤去。logging._lock（RLock 本体）を
        # 直接使う（`with` 文で acquire/release する）。
        with logging._lock:
            yield


def _attach_request_id_filter() -> None:
    """`_request_id_log_filter` を既知の受信点（handler 単位）へ冪等に付与する: root logger の
    **その時点の** handlers ＋ `"sherpa"` 自身と配下（`"sherpa.xxx"`）で**作成済みの**全 logger が
    持つ handlers ＋ `logging.lastResort`。

    root 自身には自前のハンドラ/フォーマッタを設定しない（デプロイ環境や ASGI サーバー・それも
    無ければ Python の `logging.lastResort` フォールバック handler に委ねる）ため、root の
    handlers を対象にするのが基本。**LOG-2（2026-09-03）以降の例外**: `sherpa/log_setup.py` が
    `"sherpa"` 自身（run ログの WARNING+ 用）と `"sherpa.convert.libreoffice"`/`"sherpa.ingest.
    convert"`/`"sherpa.embed"`（サブシステム専用ファイル用）へ自前の handler を付ける——これらは
    次段落の `"sherpa"`/`"sherpa.*"` 走査で拾われるため、本関数の呼び出し順（`configure_logging()`
    より後）を守れば取りこぼさない。加えて、`"sherpa"` 配下のどこかが独自の handler を
    直接付けて `propagate=False` にした named logger（root まで伝播しない）があっても取りこぼ
    さないよう、`logging.Logger.manager.loggerDict` から `"sherpa"`／`"sherpa.*"` という名前で
    **作成済みの** logger（`PlaceHolder`＝未作成の祖先ノードは除外）を毎回列挙し、それぞれの
    handlers も対象に含める。同じ handler オブジェクトが複数の logger から共有されうるため、
    集めてから重複排除して1回ずつ付与する（`_RequestIdLogFilter` の docstring どおり、
    付与対象は常に handler——logger 自身へ付けても子 logger の record には効かない）。

    import 時（モジュール読み込み時点）に加えて `sherpa.lifespan.lifespan()` の起動処理でも
    呼ぶ（冪等なので二重呼び出しは無害）——import はアプリ起動の最初期に走るため、ASGI
    サーバー（uvicorn 等）がロギング設定を終えて root へ自身の handler を追加するのが import
    より後になる場合、import 時点の1回だけでは新しい handler を取りこぼす。lifespan の起動
    処理はロギング設定が完了した後に走る前提のため、そこで再度呼べば取りこぼしを拾える。
    ただしそれでも**lifespan 起動処理より後**に動的追加される handler までは追随できない
    （このコードベースが自前でハンドラのライフサイクルを管理していない以上の保証はできない）。

    `manager.loggerDict` は `logging.getLogger()` が随時追加するライブな dict——ロックせずに
    直接 iterate すると、並行する `getLogger()` 呼び出し（`Logger.manager.getLogger()` が新規
    logger をこの dict へ挿入する）と競合し `RuntimeError: dictionary changed size during
    iteration` になりうる。`logging` 自身が `addHandler()`/`getLogger()` 等で使う内部 lock
    （`_logging_module_lock()`）を借りて、root の handlers・loggerDict のスナップショットだけを
    取る（ロック保持は最小限・以降の絞り込み/収集/付与はロック外で行う）。
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
    """X-Request-Id ヘッダ値を検証のうえ採用する（不正/無指定なら自前で採番）。"""
    if raw and len(raw) <= _REQUEST_ID_MAX_LEN and _REQUEST_ID_RE.fullmatch(raw):
        return raw
    return secrets.token_hex(16)


# path → (action, resource_type)。6つの X-API-Key ゲート付きエンドポイントのみ対象
# （自動422のように handler 本体が一度も実行されない終了経路のフォールバック監査・§finding malformed body 用）。
_ACTION_BY_PATH = {
    "/ext/v1/convert": ("ext_api.convert", "ext_convert"),
    "/ext/v1/search": ("ext_api.search", "ext_search"),
    "/ext/v1/capabilities": ("ext_api.capabilities", "ext_capabilities"),
    "/ext/v1/doc": ("ext_api.doc", "ext_doc"),
    "/ext/v1/research": ("ext_api.research", "ext_research"),
    "/ext/v1/openapi.json": ("ext_api.openapi", "ext_openapi"),
}

_HTTP_OUTCOME_REASON = {
    401: "unauthorized", 403: "forbidden", 404: "not_found", 413: "payload_too_large",
    415: "unsupported_media_type", 422: "validation_error", 429: "rate_limited",
    503: "unavailable", 504: "timeout",
}
_DENIED_STATUS = frozenset({401, 403, 429})   # 認可/レート制限に起因＝business_outcome="denied"

_audit_write_failures = 0   # 監査書込失敗の累積カウンタ（プロセス内・health/metrics 連携は将来）


def _init_audit_pending(action: str, resource_type: str, actor: str = "ext:unknown") -> dict:
    """監査下書き（`request.state.audit_pending` へ置く辞書）を1つ作る。単一の初期化点。"""
    return {"actor": actor, "action": action, "resource_type": resource_type,
            "resource_id": None, "detail": {}, "reason": None, "severity": None,
            "outcome": None, "business_outcome": "ok"}


def start_audit(request: Request, actor: str, action: str, resource_type: str,
                resource_id=None) -> dict:
    """管理系ルート（Cookie 認証・`sherpa/routers/system_extras.py`）が使う監査下書きの初期化。

    ext-key 系（`require_api_key`）と同じ `request.state.audit_pending` 機構に乗せることで、
    `ExtRequestMiddleware` が実応答ステータスで1行だけ書く一元経路へ統合する
    （キー発行/一覧/失効/回復の4ルートも同じ request-level 監査にする）。
    """
    pending = _init_audit_pending(action, resource_type, actor)
    pending["resource_id"] = resource_id
    request.state.audit_pending = pending
    return pending


_AUDIT_DB_CONNECT_TIMEOUT_S = float(os.environ.get("SHERPA_EXT_AUDIT_DB_CONNECT_TIMEOUT_S", "5"))
_AUDIT_DB_LOCK_TIMEOUT_MS = int(os.environ.get("SHERPA_EXT_AUDIT_DB_LOCK_TIMEOUT_MS", "3000"))
_AUDIT_DB_STATEMENT_TIMEOUT_MS = int(os.environ.get("SHERPA_EXT_AUDIT_DB_STATEMENT_TIMEOUT_MS", "5000"))


def _audit_db_connect():
    """監査書込み専用の接続（接続・advisory lock 待ち・statement 実行に上限時間を設ける）。

    `store.audit()`（他の全呼び出し元と共有・timeout 無し）は使わず、ここだけ専用の bounded
    接続で `store._audit_insert()` を直接呼ぶ。単一 writer スレッドが1件の DB 不調
    （ネットワーク分断・長時間ロック待ち等）で無期限にブロックすると、それ以降の全リクエストの
    監査書込み（`await asyncio.wrap_future(fut)`）が連鎖して無期限に停止してしまうため。
    """
    import psycopg
    from psycopg.rows import dict_row

    from sherpa.store.db import _dsn
    return psycopg.connect(
        _dsn(), row_factory=dict_row, connect_timeout=_AUDIT_DB_CONNECT_TIMEOUT_S,
        options=f"-c lock_timeout={_AUDIT_DB_LOCK_TIMEOUT_MS} -c statement_timeout={_AUDIT_DB_STATEMENT_TIMEOUT_MS}")


def _write_pending_audit(pending: dict | None, status_code: int, duration_ms: float,
                         method: str, path: str, request_id: str) -> None:
    """`request.state.audit_pending` と、実際に観測した応答ステータスから監査行を1つだけ書く
    （`_AuditWriter` の writer スレッド上でのみ・単一の書き込み点）。

    - `result_count` は未設定なら 0 を既定にする（欠落させない）。
    - HTTP 失敗（status>=400）で handler が business_outcome を明示していなければ、401/403/429 は
      "denied"、それ以外は "failed" に統一する（`detail` JSONB 内の業務結果・DB 列 `outcome` とは
      別物＝両方 "denied" を名乗っても衝突しない）。
    - DB 列 `outcome`/`severity` は `web/audit.html` のフィルタ契約語彙（`_OUTCOME_*`/`_SEVERITY_*`）
      に固定する。DB 専用列は追加しない（`detail` JSONB 内に収める＝audit_log の hash-chain
      対象列を増やさない）。
    - 監査書込失敗（DB 接続・advisory lock・statement の上限超過を含む）は**ここでは握り潰さず
      re-raise する**。呼び出し元の `_AuditWriter._run()` が捕まえて対応する `Future` へ例外として
      渡し、それを await する `_write_pending_audit_async` 側が ERROR ログ＋カウンタを記録して
      処理を続ける（リクエスト自体は失敗させない＝fail-closed ではなく fail-loud）。
    - **`store._ensure()`（schema 未初期化なら `init_schema()` を実行する自己修復）はここでは
      呼ばない**——`init_schema()` は advisory lock 待ち・DDL 実行に上限時間が無く、専用接続の
      timeout を丸ごと迂回してしまう（schema 準備は `lifespan` 起動時／readiness の責務・
      `store.schema_ready()`）。ここで schema が未準備なら、`_audit_db_connect()` の bounded
      接続の上で `_audit_insert()` 自体が失敗するだけ（期限付き失敗＝上記の re-raise 経路で
      正しく扱われる）。
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
    from sherpa import store as _facade   # 実行時解決（monkeypatch シーム維持・store.audit() と同じ流儀）
    with _audit_db_connect() as c:
        _facade._audit_insert(c, pending["actor"], pending["action"], pending["resource_type"],
                              pending["resource_id"], detail, outcome=outcome, reason=reason,
                              severity=pending.get("severity") or _SEVERITY_INFO, request_id=request_id)


_AUDIT_QUEUE_MAXSIZE = int(os.environ.get("SHERPA_EXT_AUDIT_QUEUE_MAXSIZE", "1000"))
_AUDIT_QUEUE_PUT_TIMEOUT_S = 5.0   # 飽和時にどれだけブロックして待つか（それでも空かなければ諦める）
_AUDIT_QUEUE_DRAIN_TIMEOUT_S = 10.0


_WRITER_RUNNING = "running"
_WRITER_STOPPING = "stopping"
_WRITER_STOPPED = "stopped"


class _AuditWriter:
    """監査 DB 書込み専用の**単一 writer スレッド＋bounded queue**。

    `audit_log` の hash-chain 書込みは DB 側で直列化されるため、複数 worker で並列化しても
    実効的な並列性は得られない。ここでは lifespan が起動/停止を管理するプロセス内シングルトンとし、
    queue が飽和したら少し待って（既定5秒）それでも空かなければ ERROR ログを出して**その1行だけ**
    諦める（他リクエストを巻き込まない・リクエスト自体は失敗させない＝fail-closed ではなく fail-loud）。

    `submit()` は writer スレッド未起動なら自前で `_lazy_start()` する——`lifespan` を実行しない
    埋め込み（`TestClient(app)` を `with` 無しで使う既存テスト流儀・lifespan イベントを起動しない
    ASGI ホスト等）でも監査書込みが黙って失われないための保険。ただし **一度でも明示的に `stop()`
    された後は lazy start しない**（`_explicitly_stopped` フラグ）——graceful shutdown 直後に
    遅延 submit が writer を蘇らせてしまうと、`stop()` が「止まった」ことの意味が失われる。
    本番の起動/終了は `sherpa/lifespan.py` の明示的な `start()`/`stop()` が担う。

    **2つの lock を持つ**:
    - `_lock`: `_state`/`_thread`/`_sentinel_put` の読み書きと `submit()` の「受付可否確認＋
      queue 投入」を不可分にする。ほとんどの区間は短時間だが、`submit()` の投入部分は
      `_lock` を保持したまま `self._q.put(..., timeout=_AUDIT_QUEUE_PUT_TIMEOUT_S)` を呼ぶ
      ため、queue 飽和時は最大 `_AUDIT_QUEUE_PUT_TIMEOUT_S` 秒までブロックしうる（非ブロッキング
      ではない——投入判定とキュー投入を同じ `_lock` 区間で不可分に行う必要があるため許容している
      トレードオフ）。
    - `_transition_lock`: `start()`/`_lazy_start()`/`stop()` の**ライフサイクル操作全体**
      （`_stopped_event.wait()`・sentinel 投入・`thread.join()` を含むブロッキング区間ごと）を
      直列化する。これが無いと: (a) `stop()` が `_state=STOPPING` にして `_lock` を解放した直後、
      生存中の旧 thread を見た `start()` が `_state` を `RUNNING` へ戻してしまい、以後の
      `submit()` が `stop()` の sentinel 投入と無関係に real item を投入できてしまう
      （sentinel の前後関係が保証されず、その item の Future が永久に解決されない）。
      (b) 並行する複数の `stop()` がそれぞれ sentinel を投入し、2本目の sentinel が次回起動時まで
      queue に残留して即座に writer を終了させる。`_transition_lock` を外枠として必ず先に取ることで、
      この2つの競合を構造的に無くす。**lock 順序は常に `_transition_lock` → `_lock`**（逆順で
      取らない・`submit()` の lazy start も `_lock` を保持したまま起動処理を呼ばず、一旦手放して
      から `_lazy_start()`（`_transition_lock` を正しい順で取る）を通す）。

    **世代管理（`_stopped_event`／`_sentinel_put`）**: `join(timeout=...)` がタイムアウトした
    （スレッドがまだ生存中）場合、`self._thread` を `None` にしない——lazy start の判定
    （`self._thread is None` で「未起動」とみなす）が誤って新しい writer スレッドを追加起動し、
    同じ queue を2スレッドが取り合う「二重 writer」を招くため。そのため次に `start()`/
    `_lazy_start()` が呼ばれたとき、旧世代のスレッドが本当に終わっているかを `_stopped_event`
    （`_run()` 自身がスレッド終了時に `finally` で set する——`stop()` の join 成否とは独立に、
    スレッド本体が実際に終わった瞬間を捉える）で確認し、set されるまで（既定 `drain_timeout` 秒）
    待ってから新スレッドを起こす。待ってもなお set されなければ起動を諦める（拒否）——同じ queue
    を新旧スレッドで取り合わせない。`_sentinel_put` は「この世代で sentinel を投入済みか」を
    記録し、`join` タイムアウト後に `stop()` が再試行されても sentinel を二重投入しない
    （二重投入すると、1つ目を消費して終了した旧スレッドの後を継ぐ新スレッドが2つ目を即座に
    消費して即終了し、それ以降の item が一切処理されなくなる＝残留 sentinel 事故）。
    `_start_locked()`（新スレッド起動の直前）で `_stopped_event.clear()`／`_sentinel_put = False`
    に必ずリセットする。

    **lost wake-up 対策**: `_run()` の finally は「`_restart_requested` を見て `_state` を
    STOPPED へ確定する」処理と「`_stopped_event.set()`」を**同じ `_lock` 保持区間**で行う
    （先に Event だけ set すると、別スレッドの `_start_locked()` の `wait()` がその直後に
    目覚めて先に新世代を起動してしまい、この finally が後から古い判定で state を上書きする
    隙ができる）。あわせて `self._thread is my_thread`（自分がまだ現行世代か）も確認し、
    既に新しい世代に置き換えられていたら state も Event も一切触らない（そうしないと、
    古いスレッドの finally が新世代の `_stopped_event` を誤って set し、新世代が生きている
    最中にさらに別スレッドが起動される二重 writer 事故になりうる）。`_start_locked()` 側も、
    `wait()` がタイムアウトした直後にもう一度 `_lock` を取って `_stopped_event` を確認する
    （タイムアウトとほぼ同時に旧世代の finally が完了している取りこぼしを防ぐ）。

    書込み完了通知は `concurrent.futures.Future` で行う。呼び出し元（`asyncio.wrap_future()` で
    await する側）が既にタイムアウト等で Future をキャンセル済みのことがあり、その状態へ
    `set_result`/`set_exception` すると `InvalidStateError` になる——これを writer ループ内で
    捕まえずに伝播させると、1件の Future 競合で `_run()` の while ループごと終了し（`threading.Thread`
    は未処理例外でスレッドを静かに終わらせるだけでプロセスは落ちない）、以後の全 item が永久に
    処理されなくなる。`_resolve_future()` で必ず捕まえる。

    `self._thread` をクリアする際は `self._thread is thread`（この `stop()` 呼び出しが捕まえた
    thread オブジェクトと現在の参照が**同一**）の場合だけ行う——`_transition_lock` により通常は
    起き得ないが、防御的に保持する（別の thread に置き換わっていたら、その新しい thread の管理は
    今回の `stop()` の責務ではない）。

    スレッドは `daemon=True`（`stop()` を一度も呼ばない異常系——interpreter 強制終了・lifespan を
    経由しない埋め込み——でプロセス終了を妨げないため。`atexit` フックは CPython の thread-shutdown
    が `atexit.register()` より先に非daemon スレッドの join を試みるため無力だった＝実測して確認済み。
    `stop()` を明示的に呼ぶ正常系の graceful shutdown はこの daemon 指定と無関係に機能する）。
    """

    def __init__(self, maxsize: int = _AUDIT_QUEUE_MAXSIZE):
        self._q: queue.Queue = queue.Queue(maxsize=maxsize)
        self._thread: threading.Thread | None = None
        self._state = _WRITER_STOPPED
        self._lock = threading.Lock()
        self._transition_lock = threading.Lock()
        # lazy start（クラス docstring 参照）は「一度も明示的に stop() されていない」インスタンス
        # に限る——`submit()` の `state==STOPPED` 判定だけで自動再起動すると、明示的な `stop()`
        # （graceful shutdown）直後の遅延 submit がプロセス終了間際に writer を蘇らせてしまう
        # （stop() 後は stay stopped が正しい契約）。
        self._explicitly_stopped = False
        self._sentinel_put = False   # 現世代で shutdown sentinel を投入済みか（stop() の再試行が二重投入しない）
        self._stopped_event = threading.Event()
        self._stopped_event.set()   # 初期状態＝スレッド無し＝「（この世代は）停止済み」
        # `_start_locked()` が旧世代の終了待ちでタイムアウトし起動を諦めたときに立てる
        # （クラス docstring「世代管理」・`_run()` の finally 参照）。
        self._restart_requested = False

    def start(self) -> bool:
        """明示的な起動（`sherpa/lifespan.py` 等）。`_explicitly_stopped` を無条件で解除してから
        起動を試みる（lazy start 専用の `_lazy_start()` とはここが異なる——`submit()` からの遅延
        起動は明示的な `stop()` の意図を上書きしてはいけないが、こちらは呼び出し自体が明示的な
        「起動してほしい」という意思表示のため）。

        戻り値: 実際に稼働状態（RUNNING）を確立できたら True。旧世代のスレッドがまだ停止し
        きっていない等の理由で新スレッドを起こせなかった場合は False——呼び出し元（lifespan
        起動処理）はこれを見て ERROR ログを出す（起動処理自体は止めない・fail-open）。False
        でも自己回復する: `_restart_requested` が立ち、旧世代が実際に終わった時点で `_run()`
        の finally が状態を STOPPED へ確定し、以後の `submit()`/`start()` の再試行で正しく
        再起動できる（『以後 submit が永久 None になる』事故を防ぐ）。
        """
        with self._transition_lock:
            with self._lock:
                self._explicitly_stopped = False
            return self._start_locked()

    def _lazy_start(self) -> bool:
        """`submit()` の遅延起動専用。`_transition_lock` 取得**後**に `_explicitly_stopped` を
        再判定してから起動する——取得前の判定（`submit()` 側）とここでの実行の間に、別スレッドの
        明示的な `stop()` が割り込んで `_explicitly_stopped=True` を立てることがある。取得前の
        古い判定のまま起動してしまうと、`stop()` 直後の遅延 submit が writer を蘇らせてしまう。
        """
        with self._transition_lock:
            with self._lock:
                if self._explicitly_stopped:
                    return False
            return self._start_locked()

    def _start_locked(self) -> bool:
        """`_transition_lock` 保持中に呼ぶこと（`start()`/`_lazy_start()` 専用の内部実装）。

        既に稼働中（`_state==RUNNING` かつ thread が生存中）なら何もせず True を返す。
        そうでなければ、旧世代のスレッドが本当に終わっている（`_stopped_event` が set 済み）
        ことを確認してから新スレッドを起こす——`join` タイムアウト直後は旧スレッドがまだ
        queue を読み出しているかもしれず、そこへ新スレッドを追加起動すると同じ queue を
        2スレッドが取り合う。待っても set されなければ起動を諦める（ERROR ログ・`_state`/
        `_thread` は変更しない＝既存の拒否的な状態を維持する）——ただし `_restart_requested`
        を立てておく。旧世代がそのうち実際に終わったとき、`_run()` の finally がこのフラグを
        見て `_state` を STOPPED へ確定するため、次回の `submit()`/`start()` は待たされずに
        （`_stopped_event` は既に set 済みなので）新世代を起動できる。
        """
        with self._lock:
            if self._state == _WRITER_RUNNING and self._thread is not None and self._thread.is_alive():
                return True   # 既に稼働中
        if not self._stopped_event.wait(timeout=_AUDIT_QUEUE_DRAIN_TIMEOUT_S):
            # wait() がタイムアウトしても、ちょうど同じタイミングで旧世代の `_run()` の finally が
            # 完了しているかもしれない（Event の set は finally が `_lock` 保持中に行うため、
            # ここで `_lock` を取って最終確認すれば lost wake-up にならない）。
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
                    if item is None:   # 停止シグナル（`stop()` が投入する）
                        break
                    pending, status_code, duration_ms, method, path, request_id, fut = item
                    try:
                        _write_pending_audit(pending, status_code, duration_ms, method, path, request_id)
                    except Exception as e:
                        # _write_pending_audit は握り潰さず re-raise する契約（その docstring 参照）——
                        # ここが実際に例外を捕まえる唯一の場所。writer loop 自体は落とさず、
                        # Future 経由で呼び出し元（_write_pending_audit_async）へ伝える。
                        self._resolve_future(fut, exc=e)
                    else:
                        self._resolve_future(fut, exc=None)
                finally:
                    self._q.task_done()
        finally:
            # 状態確定と Event の set は**同じ `_lock` 保持区間**で行う（lost wake-up 対策）:
            # 先に Event だけ set すると、その直後（この finally がまだ state を直していない
            # うちに）別スレッドの `_start_locked()` が `_stopped_event.wait()` から即座に
            # 目覚めて先に新世代を起動してしまい、この finally が後から古い判定で state を
            # 上書きする隙ができる。両方を1つの `with self._lock:` に収めることで、
            # 「Event が set 済み＝state も確定済み」という不変条件を保証する。
            #
            # `self._thread is my_thread`（世代の identity 確認）も併せて見る——自分（この
            # スレッド）が既に新しい世代に置き換えられている（`self._thread` が別オブジェクトに
            # なっている）場合は、自分の判定で state を触らない・**Event も set しない**
            # （新世代はまだ生きているのに、古いスレッドの finally が新世代の `_stopped_event`
            # を誤って set してしまうと、新世代が生きている最中にさらに別のスレッドが
            # 起動されてしまう二重 writer 事故になる）。
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
            pass   # 呼び出し側が既に諦めている（Future キャンセル済み等）＝結果を届ける相手がいない

    def submit(self, pending, status_code, duration_ms, method, path, request_id) -> Future | None:
        """キューへ投入する（同期・呼び出し側が別スレッドへ逃がすこと）。

        書込み完了時に解決される `concurrent.futures.Future` を返す——呼び出し元
        （`_write_pending_audit_async`）はこれを `asyncio.wrap_future()` で await し、
        **このリクエスト自身の**監査書込みが実際に完了するまで待てる（応答が完了する時点で
        監査行が読めることをテストが前提にしている・queue への投入だけで「完了」とみなすと
        他リクエストの処理より監査書込みが遅れて見える窓ができる）。await は event loop を
        塞がない＝他の並行リクエストの処理は妨げない。

        受付可否の確認と投入は同じ `_lock` 区間で不可分に行う（クラス docstring 参照）。
        lazy start が必要な場合は一旦 `_lock` を手放し、`_lazy_start()`（`_transition_lock` を
        正しい順で取り、`_explicitly_stopped` を再判定する）を経由してから改めて確認する
        （lock 順序逆転によるデッドロックを避けるため）。飽和時は `_AUDIT_QUEUE_PUT_TIMEOUT_S`
        秒だけブロックして待ち、それでも空かなければ諦めて None を返す（呼び出し側が ERROR
        ログ＋カウンタを記録する）。
        """
        with self._lock:
            needs_lazy_start = self._state == _WRITER_STOPPED and not self._explicitly_stopped
        if needs_lazy_start:
            self._lazy_start()   # stop() 後は再起動しない（_explicitly_stopped が担保）
        with self._lock:
            if self._state != _WRITER_RUNNING:
                return None   # STOPPING/明示的に STOPPED 済みは新規受付しない
            fut: Future = Future()
            try:
                self._q.put((pending, status_code, duration_ms, method, path, request_id, fut),
                           timeout=_AUDIT_QUEUE_PUT_TIMEOUT_S)
            except queue.Full:
                return None
            return fut

    def stop(self, drain_timeout: float = _AUDIT_QUEUE_DRAIN_TIMEOUT_S) -> None:
        """新規受付を止め、既存キューを writer スレッドに回収させてから終了する（lifespan shutdown）。

        `_transition_lock` を保持したまま sentinel 投入／`thread.join()` まで行う——並行する
        別の `stop()` 呼び出しはこの完了を待ってから見るため、sentinel は一度しか投入されない
        （クラス docstring 参照）。前回の呼び出しが `join` タイムアウトで戻っていた場合の**再試行**
        （同じ世代・thread がまだ生存中）でも、`_sentinel_put` が既に立っていれば投入し直さない
        （二重投入すると残留 sentinel 事故になる・クラス docstring「世代管理」参照）。
        """
        with self._transition_lock:
            with self._lock:
                if self._state == _WRITER_STOPPED:
                    self._explicitly_stopped = True   # 既に停止済みでも「明示的に止めた」ことは記録する
                    return
                self._state = _WRITER_STOPPING   # 以後 submit() は拒否される（この時点で確定）
                self._explicitly_stopped = True   # submit() の lazy start を以後禁止する
                thread = self._thread
                already_put = self._sentinel_put
            if thread is None:
                with self._lock:
                    self._state = _WRITER_STOPPED
                return
            if not already_put:
                try:
                    self._q.put(None, timeout=drain_timeout)   # 既存 item の後ろに確実に入る
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
                    return   # state は STOPPING のまま＝以後の submit() も引き続き拒否され続ける（安全側）
                if self._thread is thread:   # 同一スレッドの場合だけ参照をクリアする（クラス docstring 参照）
                    self._thread = None
                self._state = _WRITER_STOPPED


_audit_writer = _AuditWriter()   # lifespan（sherpa/lifespan.py）が start()/stop() を呼ぶ
# 保険（`ThreadPoolExecutor` 自身の atexit フックと同じ発想）: writer スレッドは daemon=True の
# ためプロセス終了自体は妨げないが、lifespan を一度も実行しない埋め込み（`with` 無しの
# `TestClient`・単発スクリプト等）で `stop()` が一度も呼ばれないと、queue に投入済みの未処理
# item が drain されないまま（daemon スレッド強制終了で）失われうる。`stop()` は未起動/既停止
# なら安全に no-op なので、二重登録（lifespan 経由の正常な graceful shutdown 後）でも問題ない。
atexit.register(_audit_writer.stop)


async def _write_pending_audit_async(pending: dict | None, status_code: int, duration_ms: float,
                                     method: str, path: str, request_id: str) -> None:
    """`_write_pending_audit` を専用 writer スレッドの queue へ投入し、**その書込みが完了するまで
    await する**（同期 DB 書込で event loop は塞がない。ただしこの1リクエスト自身の応答
    完了は監査行が実際に書かれるまで待つ——既存の全テストがそれを前提にしている・queue への
    投入だけを「完了」とみなすと後続リクエストほど監査行が遅れて見える窓ができる）。
    queue 投入自体（飽和時は最大5秒ブロック）も event loop を塞がないよう、デフォルト executor
    へ逃がす。書込み完了の待受けは `concurrent.futures.Future`→`asyncio.wrap_future()` で
    ブリッジする（他の並行リクエストの event loop 処理は妨げない）。

    queue 飽和（`fut is None`）だけでなく、書込み自体の失敗（DB 接続/lock/statement の上限超過を
    含む・`_write_pending_audit` が re-raise したもの）も ERROR ログ＋カウンタで記録して**続行する**
    （fail-closed ではなく fail-loud＝監査書込の障害でリクエスト自体を失敗させない）。
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


# ==== APIキー検証（`require_api_key` と ExtRequestMiddleware のフォールバックが共用する単一の真実源）====

def _generate_key() -> str:
    return _KEY_PREFIX + secrets.token_urlsafe(32)


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False, scheme_name="ApiKeyAuth")


def _key_audit_detail(row: dict) -> dict:
    """キー行から監査 `detail` の共通部分を作る（`label` は常に・`owner_uid` は自己発行キー
    （非 NULL）のときだけ）。認証の成功・失敗（401/429）・フォールバックのいずれでも、行を
    特定できた時点でこれを使い、呼び出し元＝どのユーザーの自己発行キーかを監査から追える
    ようにする（actor 自体は `ext:{key_id}` のまま＝同一ユーザーの複数キーを区別できるよう
    キー単位の集計を壊さない）。"""
    d = {"label": row["label"]}
    if row.get("owner_uid") is not None:
        d["owner_uid"] = row["owner_uid"]
    return d


def _verify_key_sync(raw_key: str | None) -> dict:
    """X-API-Key の検証本体（同期・DB アクセスを含む）。`require_api_key`（通常フロー）と
    `ExtRequestMiddleware` のフォールバック監査（malformed body 等で `require_api_key` 自体が
    実行されなかった終了経路）の両方から呼ぶ単一の真実源（検証ロジックの重複を避ける）。

    有効期限（`expires_at`）・日次クォータ（`daily_quota`）・自己発行キー（`owner_uid`）の
    所有者状態/機能トグルを追加でチェックする。いずれも既存キー（NULL）は対象外＝後方互換。

    返値: 成功時 `{"ok": True, "row": <api_keys 行>}`。
    失敗時 `{"ok": False, "status": 401|429, "reason": str, "actor": "ext:<id>"|"ext:unknown",
             "resource_id": int|None, "retry_after": int|None, "detail": dict|None}`。
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
        # 自己発行キーは Cookie セッションと同じ2つの前提を毎回確認する:
        # (1) 機能トグルが ON であること——トグル OFF への一括失効が何らかの理由で失敗しても、
        #     認証時点でも同じ結論に倒す（fail-safe な二重の締め出し）。
        # (2) 所有者が実在し `active` であること——Cookie セッション（`session_user`）は毎回
        #     `users.status='active'` を確認する契約であり、自己発行キーだけがアカウント停止/
        #     削除を迂回できてしまわないようにする。
        if not bool(store.get_system_settings().get("user_api_keys_allowed")):
            return {"ok": False, "status": 401, "reason": "user_keys_disabled",
                    "actor": f"ext:{row['id']}", "resource_id": row["id"],
                    "retry_after": None, "detail": _key_audit_detail(row)}
        if row.get("owner_status") != "active":
            return {"ok": False, "status": 401, "reason": "owner_inactive",
                    "actor": f"ext:{row['id']}", "resource_id": row["id"],
                    "retry_after": None, "detail": _key_audit_detail(row)}
    try:
        store.touch_api_key(row["id"])                   # best-effort
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
    """`ExtRequestMiddleware` が置いた request_id を読む（未装着経路への保険として自前解決も可）。"""
    rid = getattr(request.state, "request_id", None)
    return rid if rid else _resolve_request_id(request.headers.get(_REQUEST_ID_HEADER))


def require_api_key(request: Request, key: str | None = Security(_api_key_header)) -> dict:
    """X-API-Key 検証（`_verify_key_sync` へ委譲）。失敗は一律 401（キー不存在/失効の区別を外に
    出さない・rate limit は 429）。返値
    {"key_id": int, "label": str, "allowed_worlds": list[str]|None, "request_id": str}。

    `request.state.audit_pending` をここで初期化し、成功/失敗いずれの分岐でも actor/reason/detail
    を積む（自動422のように handler 本体が実行されない終了経路は `ExtRequestMiddleware` 側の
    フォールバックが拾う）。実際の DB 書き込みは `ExtRequestMiddleware` が行う。
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

    `request.state.audit_pending` を `ext_api.auth_failed`（reason=world_not_allowed）へ書き換える
    （`_write_pending_audit` が一元的に書くため、ここで別途 `store.audit` は呼ばない）。
    `pending["detail"]` は **update()（マージ）** する——`pending.update(detail={...})` のように
    "detail" キーそのものを置き換えると、handler がここより前に積んだ detail（query/prefix 等）が
    消えてしまう。
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
    """`with _AuditScope(request, action, resource_type) as audit:` の中で `audit.resource_id`／
    `audit.detail`／`audit.business_outcome` を埋める。**DB へは書かない**——
    `request.state.audit_pending` を更新するだけで、実際の監査行は `ExtRequestMiddleware` が
    応答の実際のステータスコードを観測してから1回だけ書く（handler 内で組み立てた「成功」の
    見立てと、最終的にクライアントへ届く応答が食い違うケース——`response_model` のシリアライズ
    失敗等——でも「見た目だけ成功」の行を残さないため）。
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
        return False   # 例外は常に再送出（監査は ExtRequestMiddleware の役割・ここでは書かない）


# ==== ExtRequestMiddleware（X-Request-Id・アプリログ束縛・request-level 監査の一元窓口）====

_INTERNAL_ERROR_BODY = json.dumps({"detail": "内部エラーが発生しました"}, ensure_ascii=False).encode("utf-8")


class ExtRequestMiddleware:
    """`/ext/v1/*` にだけ関与する**生の ASGI ミドルウェア**（`BaseHTTPMiddleware` は使わない）。

    理由: `BaseHTTPMiddleware` は downstream の応答を一旦タスクとして分離・再構成するため、
    `/ext/v1/doc` のストリーム配信や、未処理例外が発生したときの応答生成と相性が悪い
    （「完成した応答」しか触れない＝未処理例外時に自前で応答を組み立てられない）。生 ASGI なら
    `send` を直接ラップして、実際に送信される `http.response.start` メッセージにヘッダを
    差し込める。非 ext パスは `scope["path"]` だけを見て内側 app へ即座に委譲し、
    Request/Response オブジェクトすら作らない（オーバーヘッド無し）。

    ここで行うこと（一箇所に集約）:
    1. X-Request-Id の解決（ヘッダ検証つき・**大文字小文字を無視して既存指定を検出**）→
       `scope["state"]`／ContextVar へ束縛 → 応答ヘッダへ**常に1値**で付与（自動422・
       `HTTPException`・`StreamingResponse`・**未処理例外**のいずれでも付く）。
       `http.response.start` は実際に `await send()` が成功して初めて「開始済み」を確定する
       （送信自体が失敗した場合にヘッダ挿入の機会があったと誤認しないため）。
    2. アプリログへ1行（method/path/status/duration/request_id・`_request_id_log_filter` 経由）。
    3. 監査: `request.state.audit_pending`（`require_api_key`／`start_audit` が用意）があれば、
       実際に観測した応答ステータスコードで `_write_pending_audit_async` を呼ぶ（専用 writer
       スレッド・event loop を塞がない）。無ければ（自動422等で malformed body のため
       `require_api_key` 自体が一度も実行されなかった経路——**認証前キャンセルも含む**）、
       X-API-Key ルート（`_ACTION_BY_PATH` に列挙した各ルート）に限りフォールバックで identity を解決して最小限の監査行を書く
       （`_fallback_audit_pending`）。
    4. 応答開始**後**の例外/キャンセル（早期切断等）は「配信失敗」として `outcome=error`・
       `business_outcome=failed`・reason 付きで監査し、そのまま再送出する（ヘッダは今さら
       追加できないため応答は作り直さない・status が未確定/2xx のまま「成功」と誤記録しない）。
       応答開始**前**の未処理例外は自前で 500 応答を組み立てて返す（再送出しない・クライアントには
       綺麗な 500 を返す）。`asyncio.CancelledError`（認証前含む）は常に再送出するが、
       **fallback identity 解決・監査書込・ContextVar reset を単一の cleanup コルーチンへまとめて
       `asyncio.shield()` で保護**してから再送出する（どれか一つだけが二重キャンセルで欠落する
       事故を避ける）。
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
        send_exc: list[BaseException] = []   # 最初の配信例外だけ保持（自己生成500の再送も同じ経路）

        async def send_wrapper(message):
            """自己生成 500 応答も含め、**全ての send をここに通す唯一の経路**にする。
            送信失敗（`http.response.start` 自体の失敗を含む）はここで最初の1つだけ記録し、
            そのまま re-raise する——呼び出し元（`self.app(...)` 内部・下の自己生成500送信）の
            どちらから呼ばれても同じ追跡になるため、「start 送信失敗→自己生成500の再送も失敗」
            という経路でも例外を握り潰さず、status=0 のまま success 扱いになることがない。
            """
            if message["type"] == "http.response.start":
                # 既存の X-Request-Id（大文字小文字を問わず）を除いてから正準値を1つだけ付ける。
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
                # start は実際の送信が成功して初めて確定する（送信失敗時に「開始済み」と誤認しない）。
                status_holder["code"] = message["status"]
                status_holder["started"] = True

        # 元の例外を保持し、cleanup 完了後に明示的に再送出する（暗黙の try/finally 再送出に
        # 頼らない・二重キャンセルで cleanup 中の例外に上書きされて元のキャンセルを失わないため）。
        # `delivery_exc` は最終的に再送出する「配信失敗」の代表例外——
        # `send_wrapper` が記録した `send_exc`（自己生成500の再送失敗も含む）を最優先で使う。
        cancelled_exc: BaseException | None = None
        delivery_exc: BaseException | None = None
        try:
            await self.app(scope, receive, send_wrapper)
        except asyncio.CancelledError as e:
            cancelled_exc = e
        except Exception as e:
            if status_holder["started"]:
                # 応答開始後の失敗＝配信失敗（早期切断等）。ヘッダは今さら追加できない＝再送出のみ。
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
                    # 自己生成500の送信自体も失敗＝ send_wrapper が send_exc へ記録済み。
                    # ここでは握って下の一元判定（send_exc の有無）に委ねる（元の例外 e は
                    # 応答開始前のバグであり、配信失敗の代表例外としては send_exc を優先する）。
                    pass
        if send_exc and delivery_exc is None:
            # self.app(...) の呼び出し自体は例外を投げずに戻った（＝ Starlette 側が send 失敗を
            # 飲み込んで正常終了したように見えるケース）が、send_wrapper は失敗を観測している。
            # status=0 のまま success 扱いにしないための最終防波堤。
            delivery_exc = send_exc[0]

        cancelled = cancelled_exc is not None
        duration_ms = (time.monotonic() - state["t0"]) * 1000

        async def _cleanup() -> None:
            """fallback identity 解決・監査書込を1つにまとめる（`asyncio.shield()` で保護）。

            **ContextVar の reset はここに含めない**: `asyncio.shield()`/`ensure_future()` は
            対象コルーチンを Task 化して実行するが、Task は呼び出し元とは別の（コピーされた）
            `contextvars.Context` で走る。`_request_id_ctx.set()` が返す Token はそれを発行した
            Context でしか `reset()` できない（別 Context で呼ぶと `ValueError`・実測して発覚）
            ため、reset は shield 完了後に呼び出し元（元の Context）側の `finally` で行う——
            reset 自体は同期処理（await を挟まない）でキャンセルに割り込まれ得ないため、
            shield で保護する対象は非同期 I/O（fallback 解決・監査書込）だけで十分。
            """
            pending = state.get("audit_pending")
            if pending is None:
                # 認証前キャンセル／malformed body 等で require_api_key 自体が実行されなかった
                # 経路も含め、`_ACTION_BY_PATH` に列挙した X-API-Key ルートならここで identity を解決する。
                pending = await _fallback_audit_pending(scope)
            if cancelled and pending is not None:
                # status_holder["code"] は None/0 のままになりうる（0<400 で「成功」と誤判定
                # されないよう、outcome/business_outcome を明示する）。
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
    """`audit_pending` が一度も作られなかった終了経路（典型例: 不正な JSON ボディで body parse
    自体が失敗する自動422＝`require_api_key` が一度も実行されない）向けのフォールバック監査。

    `_ACTION_BY_PATH` に列挙した X-API-Key ルートに限り、ヘッダから鍵を読んで identity の解決を試みる（`_verify_key_sync`
    を共用。DB アクセスはデフォルト executor 経由——監査 writer 専用の bounded queue とは分離する・
    identity lookup を監査書込みの queue 飽和/バックプレッシャーに巻き込まない）。
    admin 系（Cookie 認証）はここでは検証できないためフォールバックしない（スコープ外・報告に明記）。
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
        # 成功時は `_verify_key_sync` が "actor"/"resource_id" を返さない（キー行そのものを返す
        # だけ・`require_api_key` の通常フローが row から actor 文字列を組み立てる設計と対称にする）。
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

_ZIP_READ_CHUNK = 1024 * 1024                   # zip爆弾: メンバ実測時の読み取り単位


def _zip_bomb_reason(p: Path, compressed_size: int) -> str | None:
    """OOXML zip の安全検査。危険なら理由文字列、安全なら None。壊れ zip は None（to_markdown の None 経路に任せる）。

    `ZipInfo.file_size`（中央ディレクトリの自己申告値）だけを見ると、圧縮側で file_size を偽装した
    高圧縮メンバで全検査をバイパスできる（申告は小さいが実解凍は巨大）。そのため各メンバを実際に
    ストリーム展開し、実測バイト数で上限を判定する（申告値は使わない＝解凍前に危険を確実に遮断する）。
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

    stateless: KB・台帳・ES・Neo4j には一切書き込まない。アーム/マージ機構は呼ばない
    （`office_md.to_markdown` による決定的変換のみ）。LLM を呼ばないため OpenAI へのファイル
    永続化も発生しない。ファイルは一時領域にのみ書き、必ず削除する。world を持たないため
    world スコープの enforcement は対象外（スコープ対象自体が無い＝常に許可）。

    一時ファイル書込・zip 爆弾検査・`office_md.to_markdown`（CPU 数秒級）は同期のまま
    `run_in_threadpool` へ退避する（単一 worker の event loop を塞がない）。外部契約
    （応答形状・エラー分類）は不変。
    """
    del x_request_id   # 実際の解決は ExtRequestMiddleware（ここは OpenAPI 契約の宣言のみ）
    filename = file.filename or ""
    ext = Path(filename).suffix.lower()
    with _AuditScope(request, "ext_api.convert", "ext_convert") as audit:
        audit.detail.update({"filename": filename, "ext": ext})
        if ext not in _ALLOWED_EXT:
            raise HTTPException(422, "この形式は変換できません（対応: .docx/.xlsx/.pptx/.pdf）")

        # チャンク読み＋サイズ上限（api.py の workspace_file_upload と同一パターン・OOM 回避）。
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

        method = "pdf_text" if ext == ".pdf" else "ooxml"        # 拡張子から決定的に導出

        def _convert() -> dict:
            """一時ファイル書込・zip 爆弾検査・`to_markdown` をまとめて
            threadpool 内で実行する（event loop 上では動かさない）。ロジック・順序は不変。
            """
            fd, tmp_name = tempfile.mkstemp(suffix=ext)
            tmp = Path(tmp_name)
            try:
                # 書き込み自体が例外を送出しても tmp のパスは既に確定しているため finally で確実に削除できる。
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
    engines: list[Literal["keyword", "vector", "graph"]] | None = None   # None→keyword+vector
    k: int = Field(default=10, ge=1, le=50)
    scope_paths: list[str] = Field(default_factory=list)                 # フォルダ prefix
    # 探す対象（調べ方ブロック §3.4）。既定 both＝フィルタなし。keyword/vector にのみ適用
    # （graph は言及エッジ（DOCUMENTS via=mention）が Document とコードを木を跨いで繋ぐため非適用・§3.5）。
    layer: Literal["docs", "code", "both"] = "both"
    weights: dict[Literal["keyword", "vector", "graph"], float] | None = None
    # graph エンジンの影響たどりの深さ（PART-1 で公開）。keyword/vector は消費しない（無視）。
    # 既定は `impact_service.IMPACT_MAX_DEPTH`（`SHERPA_IMPACT_MAX_DEPTH`）に揃える。
    # 上限（le）は元の契約値12を後退させない＝`max(12, IMPACT_MAX_DEPTH)`（env で広げたときだけ
    # 上限も広がる・env 未設定/12未満でも外部 API の契約は12のまま・後退させない）。
    # （Neo4j 側の安全弁＝`_run_read_capped` の30秒タイムアウトを食い潰さない範囲は運用側の責務）。
    depth: int = Field(default=search_service.IMPACT_MAX_DEPTH, ge=1,
                       le=max(12, search_service.IMPACT_MAX_DEPTH))

    @field_validator("weights")
    @classmethod
    def _w_range(cls, v):
        if v is not None and any(not (0 < x <= 10) for x in v.values()):
            raise ValueError("weights は 0 < w <= 10")
        return v


class ExtGraphPath(BaseModel):
    nodes: list[str]
    edges: list[dict]


class ExtHit(BaseModel):
    doc_id: str | None
    path: str | None
    line: int | None = None
    snippet: str
    score: float
    sources: dict[str, int]
    paths: list[ExtGraphPath] | None = None


class ExtDegraded(BaseModel):
    engine: Literal["keyword", "vector", "graph"]
    reason: str


class ExtSearchRes(BaseModel):
    world: str
    query: str
    hits: list[ExtHit]
    engines_used: list[str]
    degraded: list[ExtDegraded]


def _resolve_world_or_error(world: str, *, connect_timeout: float | None = None,
                            statement_timeout_ms: int | None = None) -> Path:
    """外部 API 専用の strict 解決。registry 不達／登録 root 不達は 503、未登録/未実在は 404。

    `connect_timeout`/`statement_timeout_ms`（両方省略可・既定 None＝無期限＝既存呼び出し元は
    無変更）: `worlds.resolve_external_world()` へそのまま転送する（`/ext/v1/research` が残り
    時間ベースで渡す）。
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


@router.post("/search", response_model=ExtSearchRes, responses=_SEARCH_RESPONSES)
def ext_search(req: ExtSearchReq, request: Request, key: dict = Depends(require_api_key),
               x_request_id: str | None = _XRequestIdIn):
    """RAG検索（エンジン分離＋RRF融合）。共有 KB のみ・個人 workspace は対象外（契約）。"""
    del x_request_id
    with _AuditScope(request, "ext_api.search", "ext_search") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200],
                             "engines": req.engines or list(search_service.DEFAULT_ENGINES),
                             "k": req.k, "depth": req.depth, "layer": req.layer})
        # scope 確認・world 解決・scope_paths 検証のいずれよりも前に正規化して積む＝403（scope
        # 外）・404/503（world 解決失敗）・422（scope_paths 不明）・503（走査失敗）のどの経路で
        # 失敗しても、監査には「何を要求されたか」（world・prefix）が残る。
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        _enforce_world_scope(request, key, req.world)   # scope 外は世界の存在有無を明かさず先に 403
        root = _resolve_world_or_error(req.world)        # strict 解決は1回だけ・以降これを使い回す
        try:
            # `valid_scope_paths(strict=True)` は known_scope_prefixes 経由でフォルダ木を走査
            # するため OSError を re-raise しうる（scope.py docstring 参照）——search() 本体と
            # 同じ try/except に含めて 503 にする。
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True):
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
            res = search_service.search(req.world, req.query, engines=req.engines, k=req.k,
                                        scope_paths=req.scope_paths, weights=req.weights,
                                        depth=req.depth, root=root, strict=True, layer=req.layer)
        except OSError as e:
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        audit.detail["result_count"] = len(res["hits"])
        audit.detail["degraded"] = [d["reason"] for d in res["degraded"]]
        return {"world": req.world, "query": req.query, **res}


# ==== POST /ext/v1/research（PART-4: AI 下調べ検索）====
#
# チャットを介さない部品として、既存のチャット内 agentic search（`sherpa/agentic_search.py`・
# `sherpa/providers/base.py` の Evidence Packet 組み立て）を `sherpa/research_service.py` 経由で
# そのまま呼ぶ（重複実装しない）。個人 workspace は対象外（agentic_search のツールは元々共有 KB
# のみを触る＝チャットの qa/troubleshoot と同じ範囲）。

class ExtResearchReq(BaseModel):
    world: str = Field(min_length=1, max_length=100)
    query: str = Field(min_length=1, max_length=1000)
    scope_paths: list[str] = Field(default_factory=list)                 # フォルダ prefix
    # 許容値は管理者カタログ（model_catalog・用途 subsearch）の allowed 内のみ（外は 400）。
    # 省略時は `provider`（下記）で決まる provider の既定モデル。
    model: str | None = Field(default=None, max_length=128)
    # PART-4a: 使う AI を明示指定（省略時は管理者設定「外部連携」タブの既定・未設定なら ollama＝
    # コスパ踏襲）。未知の値は pydantic 自身が 422 にする（`research_service.RESEARCH_PROVIDERS`
    # と同じ2択・Literal で OpenAPI スキーマにも反映させる）。
    provider: Literal["ollama", "openai"] | None = None
    # 反復上限（省略時は既定値＝agentic_search.MAX_TURNS）。上限12は search の depth 上限と同じ
    # 考え方（既存の agentic ループの安全弁 `agentic_search.MAX_TURNS`＝既定12を超えて要求させない・
    # `research_service._MAX_ITERATIONS_CEILING` と揃える）。
    max_iterations: int | None = Field(default=None, ge=1, le=12)
    max_results: int = Field(default=20, ge=1, le=50)                    # Evidence 件数の上限
    # リクエスト全体のデッドライン（開始時刻基準・省略時は既定値）。個々の LLM 呼び出しの上限は
    # 別途 `research_service._MAX_PER_CALL_TIMEOUT_S` で内部的に抑える（この値をそのまま複数ターン
    # 分掛け算した時間は待たせない）。
    timeout_s: int | None = Field(default=None, ge=5, le=180)


def _normalize_evidence_spans(evidence: list) -> None:
    """`evidence[].source_span` を `ExtEvidenceItem.source_span`（`list[int] | None`）の契約に
    正規化する（in-place）。

    行番号を持たない ES/RAG ヒット（`agentic_search.py` の es_search 分岐・チャンク由来で行番号が
    無い）は内部で `span=[None, None]` になる——これがそのまま Evidence Packet の `source_span`
    へ転記されると、`list[int]` は要素として `None` を許さないため Pydantic 検証で 500 になる
    （内部表現の欠落値をそのまま外部応答モデルへ流し込んでいた）。要素に `None` を1つでも含む
    span は「行番号情報なし」として `None` へ畳む（部分的に欠けた `[5, None]` のような形は現状の
    生成経路では起きないため、全体を丸ごと None にする単純な正規化で十分）。
    """
    for ev in evidence:
        span = ev.get("source_span")
        if isinstance(span, list) and any(x is None for x in span):
            ev["source_span"] = None


class ExtEvidenceItem(BaseModel):
    evidence_id: str
    source_type: str
    source_path: str | None = None
    source_span: list[int] | None = None
    verification_method: str | None = None
    used: bool
    matched_doc_ids: list[str] | None = None
    list_meta: dict | None = None
    card_meta: dict | None = None


class ExtEvidencePacket(BaseModel):
    """EXT-2 Evidence Packet（`sherpa/citations.py::build_evidence_packet` と同形）。"""
    task_id: str
    investigation_status: str
    summary: str = ""
    claims: list = Field(default_factory=list)
    evidence: list[ExtEvidenceItem] = Field(default_factory=list)
    remaining_gaps: list[str] = Field(default_factory=list)
    conflicts: list = Field(default_factory=list)
    candidates_seen: int = 0
    candidates_inspected: int = 0
    evidence_selected: int = 0
    stop_reason: str = ""
    next_action: str = ""


class ExtResearchRes(BaseModel):
    world: str
    query: str
    answer: str
    evidence_packet: ExtEvidencePacket
    model_used: str
    provider_used: Literal["openai", "ollama"]
    # 実行した思考/ツール手順の可視ステップ数（探索の反復回数）。課金相当の実 LLM 呼び出し回数
    # （ツールターン＋再合成＋根拠帰属の合計）は一致しない別の値＝ llm_calls を見る。
    iterations: int
    llm_calls: int


_RESEARCH_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    400: {"description": "model が許可リスト外です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    403: {"description": "このキーはこの world へのアクセスを許可されていません（world スコープ外）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    404: {"description": "資料フォルダ（world）が見つかりません", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("不明な範囲（scope_paths）が指定された場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "AIプロバイダに接続できない、または資料フォルダの参照先を確認できません"
                        "（一時的な障害・フォールバックはしません）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    504: {"description": "調査がリクエスト全体のデッドライン（timeout_s）内に完了しませんでした",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.post("/research", response_model=ExtResearchRes, responses=_RESEARCH_RESPONSES)
def ext_research(req: ExtResearchReq, request: Request, key: dict = Depends(require_api_key),
                 x_request_id: str | None = _XRequestIdIn):
    """AI 下調べ検索（agentic search・PART-4）。共有 KB のみ・個人 workspace は対象外（契約）。

    X-Request-Id は Evidence Packet の `task_id`（`ext-research:{request_id}`）へ伝播する
    （§8.1「下流の実行イベントへの伝播」——本エンドポイントは exec_event を発行しないため、
    「1呼び出し→内部処理」の追跡は Evidence Packet の task_id と監査行の request_id で担う。
    `research_service.research_task_id` docstring 参照）。

    world の認可解決（world_lock_shared・root の再解決・pin_world_root）は
    `research_service.run_research` 自身が rebind との TOCTOU を避けるために行う——
    ここでの `_resolve_world_or_error` は 404/422 を早く返すための preflight（軽い存在確認）に
    留め、その解決結果はそのまま `run_research` へは渡さない（`research_service.py` docstring
    「world の解決と固定」参照）。

    **リクエスト全体の絶対期限はこのハンドラの入口で一度だけ確定し、preflight（world 解決・
    scope_paths 走査）と `run_research` の両方でこの同じ時計を共有する**——`run_research` へは
    `timeout_s` へ変換せず、この絶対期限（`deadline`）そのものを渡す（`run_research(
    absolute_deadline=...)`）。一度 `timeout_s`（残り秒数）へ変換してから `run_research` が
    改めて絶対期限を作り直すと、整数秒への切り上げと変換〜呼び出しの間の僅かな時間が積み重なり、
    元の期限を最大約1秒超えてから 200 を返しうる。preflight（world 解決・scope_paths
    走査）の各ステップも同じ `deadline` を見て、それ自体が長引いて既に超過していれば、その時点で
    判明したはずの 404/422/503 より 504 を優先する（resolver 自身が直接送出する 404/503・
    `scope_infer.safe_files` の木走査打ち切りを含む・`run_research` 内部と同じ「デッドライン
    優先」の扱い）。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.research", "ext_research") as audit:
        audit.resource_id = req.world
        audit.detail.update({"world": req.world, "query": req.query[:200], "model": req.model,
                            "provider": req.provider})
        sp = scope_mod.normalize_scope_paths(req.scope_paths)
        audit.detail["prefix"] = sp
        timeout_s = req.timeout_s if req.timeout_s is not None else research_service._default_timeout_s()
        deadline = time.monotonic() + timeout_s

        def _remaining() -> float:
            return deadline - time.monotonic()

        def _deadline_exceeded() -> HTTPException:
            return HTTPException(
                504, f"調査が制限時間（{timeout_s}秒）内に完了しませんでした（world/scope 確認中）")

        _enforce_world_scope(request, key, req.world)   # scope 外は世界の存在有無を明かさず先に 403
        try:
            # 残り時間ベースで registry 読み取りに connect_timeout/statement_timeout を掛ける
            # （`store.get_world`/`worlds.resolve_external_world` docstring 参照）。
            root = _resolve_world_or_error(
                req.world, connect_timeout=_remaining(),
                statement_timeout_ms=max(1, int(_remaining() * 1000)))   # preflight のみ
        except HTTPException as e:
            # resolver 自身が 404/503 を直接送出する（`_resolve_world_or_error` 参照・そちら自身も
            # `_log_masked_exception` を通す）——それ自体は期限を見ないため、ここで捕捉して
            # 「resolver が長引いた末の失敗」なら 404/503 より 504 を優先する（デッドライン優先の
            # 契約を resolver 経路にも揃える）。再分類する場合も、その判断自体を診断ログへ残す。
            if _remaining() <= 0:
                from .ingest.graph_extract import _log_masked_exception
                _log_masked_exception(
                    _log, "ext_api: world resolver 失敗をデッドライン優先で504へ再分類", e)
                raise _deadline_exceeded() from None
            raise
        if _remaining() <= 0:
            raise _deadline_exceeded()
        try:
            if not scope_mod.valid_scope_paths(req.world, req.scope_paths, root=root, strict=True,
                                               deadline=deadline):
                if _remaining() <= 0:
                    raise _deadline_exceeded()
                raise HTTPException(422, "不明な範囲（scope_paths）が指定されました")
        except scope_infer.ScopeWalkDeadlineExceeded as e:
            # 木走査自体がデッドラインを超えて中断した（`scope_infer.safe_files` の `deadline`
            # 引数・1ディレクトリごとに確認）——422/503 ではなく 504 にする。
            from .ingest.graph_extract import _log_masked_exception
            _log_masked_exception(_log, "ext_api: scope 走査がデッドラインを超えて中断", e)
            raise _deadline_exceeded() from None
        except OSError as e:
            from .ingest.graph_extract import _log_masked_exception
            if _remaining() <= 0:
                _log_masked_exception(
                    _log, "ext_api: scope 走査中の OSError をデッドライン優先で504へ再分類", e)
                raise _deadline_exceeded() from e
            _log_masked_exception(_log, "ext_api: scope 走査中の OSError", e)
            raise HTTPException(
                503, "資料フォルダの走査中にエラーが発生しました（一時的な障害の可能性があります）") from e
        if _remaining() <= 0:
            raise _deadline_exceeded()
        try:
            result = research_service.run_research(
                world=req.world, query=req.query, scope_paths=req.scope_paths, model=req.model,
                provider=req.provider,
                max_iterations=req.max_iterations, max_results=req.max_results,
                timeout_s=timeout_s, key_id=key["key_id"],
                request_id=_current_request_id(request),
                # ハンドラ入口で確定した絶対期限そのものを渡す（`timeout_s` へ変換してから
                # `run_research` が改めて絶対期限を作り直すと、整数秒への切り上げ＋呼び出しに
                # かかる僅かな時間が積み重なり、元の期限を最大約1秒超えてから 200 を返しうる）。
                # `timeout_s` 自体はメッセージ表示用に元の値のまま渡す。
                absolute_deadline=deadline)
        except research_service.ModelNotAllowed as e:
            raise HTTPException(400, str(e)) from None
        except research_service.InvalidScope as e:
            raise HTTPException(422, str(e)) from None
        except research_service.ResearchTimeout as e:
            # 失敗までに解決/計測できた分は監査へ残す（成功時と同じキー名）。
            audit.detail.update({k: v for k, v in {
                "model_used": e.model_used, "provider_used": e.provider_used,
                "llm_calls": e.llm_calls}.items() if v is not None})
            raise HTTPException(504, str(e)) from None
        except research_service.ProviderUnavailable as e:
            # `from None`: `e`（ここでは常にマスク済みの固定的な message のみを持つ）を
            # HTTPException の __cause__ として保持しない——応答開始後のクライアント切断等で
            # この HTTPException の traceback が ASGI ミドルウェアの delivery-failure ログへ
            # 出力される経路があり、traceback formatter は __cause__ チェーンを無条件に辿って
            # 表示するため、内部例外を繋いだままにする理由が無い分は最初から切っておく
            # （多層防御・research_service.py 側の同種の是正と対）。
            audit.detail.update({k: v for k, v in {
                "model_used": e.model_used, "provider_used": e.provider_used,
                "llm_calls": e.llm_calls}.items() if v is not None})
            # `str(e)` は常にマスク済みの固定文言（秘密・生の例外文字列を含まない）のため、
            # 監査で「何が理由の 503 だったか」を追跡できるようそのまま残す。
            audit.detail["reason"] = str(e)
            raise HTTPException(503, str(e)) from None
        # §8.3（コスト記録）: model_used/iterations/llm_calls は監査にも残す。§8.4（根拠の
        # トレーサビリティ）: 実際に使った ev-* の全集合を監査へ記録する——`result["used_ev_ids"]`
        # は max_results での切り詰め前に確定した集合のため、切り詰められて Packet から消えた
        # 使用済み Evidence も監査からは漏れない（`research_service._truncate_preferring_used`
        # 参照）。
        audit.detail.update({
            "model_used": result["model_used"], "provider_used": result["provider_used"],
            "iterations": result["iterations"], "llm_calls": result["llm_calls"],
            "result_count": len(result["evidence_packet"]["evidence"]),
            "ev_ids": result["used_ev_ids"]})
        # `response_model=ExtResearchRes` の検証（`ExtEvidenceItem.source_span: list[int] | None`）
        # へ渡す前に正規化する——`_normalize_evidence_spans` docstring 参照。
        _normalize_evidence_spans(result["evidence_packet"]["evidence"])
        return result


# ==== GET /ext/v1/capabilities（discovery）====

class ExtWorldInfo(BaseModel):
    id: str
    document_count: int | None = None
    last_updated: str | None = None


class ExtCapabilitiesRes(BaseModel):
    worlds: list[ExtWorldInfo]
    features: list[str]


# このキーで使える機能一覧（現状は全キー共通・world スコープのみキーごとに絞られる）。
# convert は world を持たないため常に利用可（world スコープの enforcement 対象外）。
_FEATURES = ("convert", "search:keyword", "search:vector", "search:graph", "doc", "research")

_CAPABILITIES_RESPONSES = {
    200: {"headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    401: {"description": "APIキーが無効/未指定です", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    422: _validation_error_response("入力パラメータが不正な場合"),
    429: {"description": "レート制限を超過しました", "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
    503: {"description": "world 一覧を確認できませんでした（一時的な障害の可能性があります）",
          "headers": dict(_REQUEST_ID_OPENAPI_HEADER)},
}


@router.get("/capabilities", response_model=ExtCapabilitiesRes, responses=_CAPABILITIES_RESPONSES)
def ext_capabilities(request: Request, key: dict = Depends(require_api_key),
                     x_request_id: str | None = _XRequestIdIn):
    """discovery: world 一覧（id・原本の文書件数・最終確定同期時刻）＋このキーで使える機能一覧。

    `key["allowed_worlds"]` が非 None ならそのスコープ内の world だけを返す（スコープ外 world
    の存在を漏らさない）。**registry 行は `store.list_worlds_db()` で1回だけ取得し、以降その
    同一スナップショットから ID・root・最終同期時刻・文書件数のすべてを導出する**（2回目の
    生 DB 呼び出しを行わない＝そこで起きる未捕捉例外による 500 を無くす）。fixtures/dev
    直下の未登録候補は `worlds.discover_fs_world_ids_strict()`（ファイルシステム列挙のみ・DB
    非依存）で列挙する。

    `document_count` は**取り込み成功確定時に記録された事前集計値**（`worlds.last_doc_count`・
    `worker._run_locked` の成功パスでのみ更新）を返す。**ここではファイルツリーを走査しない**
    （ホットパスでの走査を廃止）。未確定（一度も成功同期していない）なら `null`
    （「不明」を正直に返す・0 に潰さない）。`last_updated` も同様に取り込みが**成功確定**した
    時刻のみ（進行中/失敗直後の pre-invalidate 書き込みで更新される時刻は使わない）。
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
        for wid in ids:
            row = registry_rows.get(wid)   # 同一スナップショットのみ参照（再照会しない・row 消失時の
            # dev フォールバックはしない＝row が None なら「未登録」として fixtures/dev のみ試す）。
            try:
                res = worlds.resolve_external_world(wid, registry_row=row)
            except worlds.ExternalResolverError as e:
                if row is not None:
                    # 登録済み world の root に到達できない＝一覧から静かに外すと「実在しない」と
                    # 区別が付かなくなる。fixtures/dev のみの未登録候補（row is None・
                    # FS 列挙の best-effort な性質上のレース等）は従来どおり静かに外す。
                    raise HTTPException(
                        503, "world の実在を確認できませんでした（一時的な障害の可能性があります）") from e
                continue
            if res.status != "ok":
                continue
            doc_count = None
            last_updated = None
            if row and row.get("last_synced_at") and row.get("last_sig"):
                # last_sig が空文字＝取り込み開始時の pre-invalidate 書き込みのまま（進行中/未確定）。
                # 非空＝取り込み成功確定後の署名。確定済みのときだけ「確定値」として報告する。
                last_updated = str(row["last_synced_at"])
                doc_count = row.get("last_doc_count")
            out.append({"id": wid, "document_count": doc_count, "last_updated": last_updated})
        audit.detail["result_count"] = len(out)
        return {"worlds": out, "features": list(_FEATURES)}


# ==== GET /ext/v1/doc（原本取得）====

_DOC_MAX_BYTES = int(os.environ.get("SHERPA_EXT_DOC_MAX_BYTES", str(50 * 1024 * 1024)))  # 50MiB
_OLE2_MAGIC = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"   # 旧バイナリ Office（OLE2/CFB）共通シグネチャ
# ソース原文（コード）分はアナライザ登録簿が単一の真実源（§2.4）。
_UTF8_DECLARE_EXT = {".md", ".markdown", ".txt"} | _analyzer_registry.registered_extensions()
_UTF8_VALIDATE_CAP = 65536   # charset=utf-8 の宣言判定はファイル全体がこの上限以下の時だけ行う

# 拡張子→固定 Content-Type。legacy Office（.doc/.xls/.ppt）は含めない——nosniff 済みのため MIME
# 混同の実害は無く、CFB の中身（真の形式）は判別しない裁定＝application/octet-stream 固定
# （`_DOC_CONTENT_TYPE.get(ext, "application/octet-stream")` の既定にそのまま落ちる）。
# ソース原文（コード）はすべて text/plain（拡張子集合はアナライザ登録簿が単一の真実源・§2.4）。
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
                       "〔.doc/.xls/.ppt〕は application/octet-stream 固定＝形式判別しない裁定）",
        # 実際に返しうる全 Content-Type を列挙する（`_DOC_CONTENT_TYPE` が単一の真実源）。
        # legacy Office・辞書に無い拡張子は既定の application/octet-stream に落ちる。
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
#
# legacy-Office/OOXML 検証方針（配信元は登録済み world＝信頼済みコーパス・深い形式判別は
# 脅威モデル過剰）:
# - legacy Office（.doc/.xls/.ppt）は CFB ヘッダの健全性のみ確認する（stream 列挙・形式判別・
#   入れ子解析はしない）。Content-Type は application/octet-stream 固定（nosniff 済みのため
#   MIME 混同の余地自体が無い）。
# - OOXML は EOCD（末尾の central directory 終端レコード）＋central directory 自体を
#   bounded に検証し（メンバ数上限・ZIP64・multi-disk・境界整合を含む）、メンバー名も
#   その検証済みの走査から直接得る——`zipfile.ZipFile` へ central directory を解析させる
#   工程は無い（巨大/悪意ある central directory を他 parser に一切食わせない・検証した
#   central directory と実際に見る central directory が別レコードになる二重解析も無い）。

_IMAGE_MAGIC_BY_EXT = {
    ".png": (b"\x89PNG\r\n\x1a\n",),
    ".jpg": (b"\xff\xd8\xff",),
    ".jpeg": (b"\xff\xd8\xff",),
    ".gif": (b"GIF87a", b"GIF89a"),
    ".bmp": (b"BM",),
    # TIFF: classic（version 42）と BigTIFF（version 43・4GB 超対応）の両方を受理する。
    ".tif": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
    ".tiff": (b"II*\x00", b"MM\x00*", b"II+\x00", b"MM\x00+"),
}

_OOXML_MAIN_PART = {
    ".docx": "word/document.xml", ".xlsx": "xl/workbook.xml", ".pptx": "ppt/presentation.xml"}

_ZIP_EOCD_SIG = b"PK\x05\x06"
_ZIP_EOCD_SIZE = 22
_ZIP_EOCD_MAX_COMMENT = 65535   # ZIP スペック上の comment 長の上限（2 バイトフィールド）
_ZIP64_SENTINEL = 0xFFFF        # EOCD の 16bit entry 数フィールドがこの値＝ZIP64（別レコード）を示す
_ZIP64_SENTINEL32 = 0xFFFFFFFF  # EOCD の 32bit cd_size/cd_offset がこの値＝同上
_ZIP_CD_ENTRY_SIG = b"PK\x01\x02"       # central directory file header の署名
_ZIP_CD_ENTRY_FIXED_SIZE = 46           # 可変長フィールド（filename/extra/comment）より前の固定部

# 複数 EOCD 候補を右から左へ試す際の**合算**上限。1候補あたりの上限（`_ZIP_MAX_MEMBERS`
# 等）だけでは、候補ごとには合法に見える偽 EOCD を comment 内に何個も並べて central directory
# 走査を何度も繰り返させる DoS を防げない——候補数・全候補合算の entry 数・pread バイト数の
# いずれかを超えたら、それ以上候補を試さずアーカイブ全体を拒否する。
_ZIP_MAX_EOCD_CANDIDATES = 8
_ZIP_MAX_TOTAL_ENTRIES_WALKED = 20_000
_ZIP_MAX_TOTAL_BYTES_READ = 8 * 1024 * 1024   # 8MiB


class _ZipScanBudget:
    """複数の EOCD 候補を試す際の合算走査量を数える（壁時計ではなく実測カウンタ）。

    候補ごとに central directory 走査をリセットせず、**全候補合算**で候補数・entry 数・
    pread バイト数を追跡する。いずれかの上限を超えたら `exceeded=True` になり、以後の
    `note_*()` は全て False を返す——呼び出し元（`_zip_bounded_check_names`）はこれを見て
    それ以上候補を試さずアーカイブ全体を拒否する（個々の候補が構造的に不正なだけの場合は
    次候補へ進む、という通常の分岐とは区別する）。
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
    """`tail`（ファイル末尾の bounded read）の中の `PK\x05\x06` 出現を**右から左へ**順に
    `(idx, eocd)` として yield する。**申告された comment_len が実際に EOF まで正確に一致する**
    （＝ EOCD としてそもそも形が成立しうる）候補だけを yield し、それ以外は無視してさらに左を
    探す——正当な ZIP のファイル本体・comment の中に偶然 `PK\x05\x06` という4バイト列が
    含まれることはありうる（EOCD としての形が成立しない限り無害）。

    ここでの絞り込みは「形が正しいか」（comment 長が辻褄が合うか）だけで、central directory
    との整合（`cd_offset+cd_size` 境界・実 entry 数の一致）までは検証しない——それは呼び出し元
    （`_zip_bounded_check_names`）が候補ごとに**同一の parser**で最後まで検証し、最初に完全
    通過したものだけを採用する（rightmost だけを機械的に信用して即座に受理/拒否を決めない：
    コメント内に偶然 EOCD 署名を含むだけの正当な ZIP を誤って全体拒否しない）。この関数自体は
    候補数を絞らない（列挙するだけ）——合算コストの上限は `_ZipScanBudget` が呼び出し元で見る。
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


_ZIP_CD_DISK_NUMBER_OFFSET = 34         # disk number where file starts（2バイト）
_ZIP_CD_COMPRESSED_SIZE_OFFSET = 20     # compressed size（4バイト）
_ZIP_CD_UNCOMPRESSED_SIZE_OFFSET = 24   # uncompressed size（4バイト）
_ZIP_CD_LOCAL_HEADER_OFFSET_OFFSET = 42  # local header offset（4バイト）
_ZIP64_EXTRA_TAG = 0x0001               # extra field 内の ZIP64 拡張情報サブレコードの tag


def _cd_entry_extra_has_zip64(extra: bytes) -> bool:
    """central directory entry の "extra" フィールド（`(tag:2, size:2, data:size)` のサブレコード
    列）に ZIP64 拡張情報（tag `0x0001`）が含まれるか。サブレコード列がきれいに終端しない
    （壊れている）場合も保守的に True を返す（安全側＝拒否する）。
    """
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
    """central directory（`cd_offset` から `cd_size` バイト）を1件ずつ**上限付き exact-read**で
    逐次走査し、実際の entry 数と各 entry のファイル名（生バイト列）を返す（46バイト固定
    header ＋可変長 filename/extra フィールドだけを都度読む・central directory 全体を一度に
    `pread` しない）。

    ここで集めたファイル名は、この後 `zipfile.ZipFile` に central directory を再解析させず
    メンバー名を確定させるために使う（`_zip_bounded_check` が検証したのと別の central
    directory を `zipfile.ZipFile` が独自に解析してしまう二重解析を避ける）。

    `budget`（`_zip_bounded_check_names` が複数候補にまたがって共有する）: `note_entry()` は
    各 entry の**ループ先頭**（`pread` する前）で呼ぶ——合算 entry 数上限に既に達している
    entry は、その header の `pread` すら一切行わない（末尾で呼ぶと、上限を超えた時点の
    entry の `pread` が既に発生してしまう）。`pread` するごとにも `note_read()` を呼ぶ。
    いずれかで合算上限を超えたら即座に None を返す（この関数単体では「この候補の走査を
    打ち切った」のか「合算上限に達した」のか区別しない——呼び出し元が `budget.exceeded` を
    見て、後者ならそれ以上他の候補も試さない）。

    以下のいずれかに該当すれば None（拒否）:
    - signature 不一致・可変長フィールドの合計が `cd_size` と不整合・`_ZIP_MAX_MEMBERS` 超過
      （`zipfile.ZipFile` も EOCD の自己申告件数ではなく `cd_size` 分を実際に走査して `ZipInfo`
      を作るため、ここでも同じ基準で実件数を確定させる）。
    - entry の `disk number start != 0`（multi-disk 拒否と整合）。
    - `compressed_size`/`uncompressed_size`/`local_header_offset` のいずれかが `0xFFFFFFFF`
      （ZIP64 sentinel＝実値は extra フィールド側・標準 CD だけでは境界を保証できない）。
    - extra フィールドに ZIP64 拡張情報（tag `0x0001`）が含まれる。
    - 合算走査量が `budget` の上限を超えた（`_ZIP_MAX_TOTAL_ENTRIES_WALKED`/`_ZIP_MAX_TOTAL_BYTES_READ`）。
    """
    if cd_size == 0:
        return 0, frozenset()
    pos = 0
    count = 0
    names: set[bytes] = set()
    while pos < cd_size:
        if not budget.note_entry():
            return None   # 合算 entry 数上限に既に達している＝この entry の pread は一切行わない
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
            return None   # multi-disk archive は拒否
        if _ZIP64_SENTINEL32 in (compressed_size, uncompressed_size, local_header_offset):
            return None   # ZIP64 sentinel（実値は extra フィールド）
        if cd_size - pos - _ZIP_CD_ENTRY_FIXED_SIZE < n + m + k:
            return None   # 可変長フィールドの合計が cd_size をはみ出す
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
    """EOCD の候補を右から左へ順に試し、**同一 parser** で EOCD の全フィールド・central
    directory 自体（`_ZIP_MAX_MEMBERS` 超・ZIP64・multi-disk・`cd_offset+cd_size` の境界・
    実 entry 数の一致まで）を完全に検証できた**最初の候補**を採用し、そのメンバー名の集合を
    返す（どの候補も完全通過しなければ None＝アーカイブ全体を拒否）。

    rightmost の候補が「comment 長は辻褄が合うが central directory とは整合しない」場合
    （正当な ZIP のファイル本体・comment に偶然 `PK\x05\x06` という4バイト列が含まれるだけ、
    等）に、それだけで全体拒否せず、次の候補（さらに左）を試す——ただし「候補として認める
    かどうか」（`_iter_eocd_candidates`）と「その候補を採用するかどうか」（ここ）を同じ
    parser・同じ検証基準で行うため、rightmost 以外を採用しても `zipfile.ZipFile` 等の別
    parser と食い違う余地はない（このモジュールは以後 central directory を再解析しない・
    メンバー名もこの検証済みの走査から直接得る）。

    `zipfile.ZipFile` は EOCD の自己申告 `total_entries` を信用せず、`cd_size` 分を実際に走査して
    全 `ZipInfo` を生成する。EOCD の件数フィールドだけを検査しても、central directory 自体に
    大量の entry を詰めて件数フィールドだけ小さく偽装されれば素通りしてしまう——そのため
    `_zip_count_central_directory_entries` で central directory を同じやり方で走査し、実際の
    entry 数を確定させてから EOCD の自己申告値と突き合わせる。

    複数候補を試すこと自体が新たな DoS 面にならないよう、`_ZipScanBudget` で**全候補合算**の
    候補数・entry 数・pread バイト数を追跡する——1候補あたりの上限（`_ZIP_MAX_MEMBERS` 等）
    だけでは、候補ごとに合法に見える偽 EOCD を comment 内に何個も並べて central directory
    走査を何度も繰り返させられる。合算上限を超えたら、それ以上候補を試さず即座にアーカイブ
    全体を拒否する（`budget.exceeded`）。

    `budget.note_candidate()` は central directory walker（disk I/O を伴う高コストな検査）を
    呼ぶ**直前**、すなわち EOCD フィールドの定数時間チェック（disk 番号・ZIP64 sentinel・
    `cd_offset+cd_size` 境界）を通過した候補にだけ課金する——安価にその場で弾ける偽候補
    （struct のフィールド不整合だけで即 `continue` する）まで候補数の枠を消費すると、その
    偽候補を8個以上並べるだけで正当な本物の EOCD（さらに左）まで試す前に候補数上限に達し、
    正当な ZIP を誤って拒否してしまう。
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
            continue   # multi-disk は不採用（single-disk なら disk 番号は 0・件数は一致するはず）
        if (total_entries == _ZIP64_SENTINEL or cd_size == _ZIP64_SENTINEL32
                or cd_offset == _ZIP64_SENTINEL32):
            continue   # ZIP64 sentinel（実値は別レコード）＝標準 EOCD だけでは境界を保証できない
        if total_entries > _ZIP_MAX_MEMBERS:
            continue
        if cd_offset + cd_size != eocd_abs_offset:
            continue   # central directory は EOCD の直前で終わっているはず（prepended data 等は不採用）
        if not budget.note_candidate():
            return None   # 候補数の合算上限を超過＝これ以上候補を試さずアーカイブ全体を拒否
        walked = _zip_count_central_directory_entries(fd, cd_offset, cd_size, budget)
        if walked is None:
            if budget.exceeded:
                return None   # entry 数/バイト数の合算上限を超過＝これ以上候補を試さない
            continue
        actual_count, names = walked
        if actual_count == total_entries:
            return names
    return None


def _zip_bounded_check(fd: int, size: int) -> bool:
    """`_zip_bounded_check_names` の合否のみを返す薄いラッパー（呼び出し側がメンバー名を
    使わない場合の簡便な入口）。"""
    return _zip_bounded_check_names(fd, size) is not None


def _ooxml_magic_ok(fd: int, ext: str, size: int) -> bool:
    """OOXML（.docx/.xlsx/.pptx）: EOCD と central directory の bounded 検査を通過し、かつ
    その検証済み central directory 走査で得たメンバー名が `[Content_Types].xml` と形式固有
    main part を含む。

    `zipfile.ZipFile` へ central directory を独自に再解析させない（`_zip_bounded_check_names`
    が確定させた central directory と別レコードを解析させてしまう二重解析＝検証バイパスの
    経路を避けるため）。fd の読み取り位置は `os.pread`（オフセット指定）のみで動かさないため
    seek 復帰は不要。
    """
    main_part = _OOXML_MAIN_PART.get(ext)
    if main_part is None:
        return False
    names = _zip_bounded_check_names(fd, size)
    if names is None:
        return False
    return b"[Content_Types].xml" in names and main_part.encode("ascii") in names


def _legacy_office_header_ok(data: bytes) -> bool:
    """.doc/.xls/.ppt: CFB（[MS-CFB]）ヘッダの署名＋version/byte order/sector shift の健全性
    だけを見る（legacy-Office 検証方針・stream 列挙／形式判別／入れ子解析はしない）。

    OLE2 でない場合は pre-OLE2 の旧形式（Excel 4.0 以前の生 BIFF・超旧版 Word 等）とみなし
    拒否しない——単一の信頼できる共通シグネチャが無く、doctype（拡張子）で既にゲート済み。
    """
    if data[:8] != _OLE2_MAGIC:
        return True
    if len(data) < 512:
        return False   # 512 バイトヘッダ未満＝壊れている
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
        return sector_shift == 9        # 512 バイトセクタ
    if major == 4:
        return sector_shift == 12       # 4096 バイトセクタ
    return False


def _doc_magic_ok(fd: int, ext: str, header: bytes, size: int) -> bool:
    """拡張子ごとに形式固有の検証を行う（バイナリ形式のみ・text 系はマジック無しのため対象外）。"""
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
    """厳密な UTF-8 検証（incremental decoder・全文）。呼び出し側は「ファイル全体が上限
    （64KiB）以下のときだけ」この関数を使う——打ち切った途中経過を渡すと、末尾の不完全な
    多バイト列を不正判定してしまう（打ち切り時は「検証できていない」ことを正直に charset
    非宣言にする方針）。
    """
    import codecs
    decoder = codecs.getincrementaldecoder("utf-8")(errors="strict")
    try:
        decoder.decode(data, final=True)
    except UnicodeDecodeError:
        return False
    return True


def _content_type_for(ext: str, fd: int, size: int) -> str:
    """text 系（charset=utf-8 を宣言する種別）はファイル全体が上限以下の場合のみ実データを
    検証し、妥当でなければ charset 宣言を外す（実際のエンコーディングを確認せず utf-8 だと
    偽らない）。上限超過（打ち切り）は検証していない＝charset を宣言しない。
    """
    base = _DOC_CONTENT_TYPE.get(ext, "application/octet-stream")
    if ext in _UTF8_DECLARE_EXT and size <= _UTF8_VALIDATE_CAP:
        sample = os.pread(fd, size, 0)
        if _looks_utf8(sample):
            return f"{base}; charset=utf-8"
    return base


def _doc_path_segments(path: str) -> tuple | None:
    """`path`（query）→ 検証済み POSIX セグメント列（`..`/絶対/空要素/バックスラッシュ/NUL は None）。"""
    if path.startswith("/") or "\\" in path or "\x00" in path:
        return None
    parts = tuple(path.split("/"))
    if not parts or ".." in parts or "" in parts:
        return None
    return parts


@router.get("/doc", response_class=StreamingResponse, responses=_DOC_RESPONSES)
def ext_doc(request: Request, world: str = Query(..., min_length=1, max_length=100),
           path: str = Query(..., min_length=1, max_length=4096),
           key: dict = Depends(require_api_key), x_request_id: str | None = _XRequestIdIn):
    """根拠の原本DL。

    検証（symlink 拒否・world root 封じ込め・doctype 対応種別・マジック整合）から実配信までを
    **同一 fd**で行う：`sherpa.safe_open.open_file_nofollow_walk` で `/` から中間ディレクトリを
    1段ずつ `O_NOFOLLOW` で辿って最終ファイルを open → その fd を `fstat`（通常ファイル・
    サイズ）→ マジック検証 → 配信は同じ fd（`sherpa.fd_response.FdOwner`/`FdFileResponse` が
    所有権を引き継ぐ・documents ルータの `/documents/download` と共有）から読むだけ、で一切
    パスを再解決しない。検証と配信が別操作（パスで再 open）だと、その間隔（TOCTOU）で world 外
    ファイルへの symlink 差し替えやサイズ上限の迂回を許してしまう。
    """
    del x_request_id
    with _AuditScope(request, "ext_api.doc", "ext_doc") as audit:
        audit.resource_id = world
        audit.detail.update({"world": world, "path": path})
        _enforce_world_scope(request, key, world)   # scope 外は世界の存在有無を明かさず先に 403
        ext = Path(path).suffix.lower()
        if corpus_docs.status_document_doctype(path, world) is None:
            raise HTTPException(404, "対応していない種別、または文書が見つかりません")
        parts = _doc_path_segments(path)
        if parts is None:
            raise HTTPException(404, "文書が見つかりません（パス不一致／未実在）")
        root = _resolve_world_or_error(world)
        try:
            fd = safe_open.open_file_nofollow_walk(root, parts)
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
        except Exception:   # HTTPException も含め、検証失敗時は配信前なので fd をここで閉じる
            os.close(fd)
            raise
        audit.detail["size_bytes"] = size_bytes
        headers = {
            # Content-Type はここで確定させ、media_type=None で渡す。starlette.Response は
            # media_type が "text/" 始まりで charset 未指定だと自動で `; charset=utf-8` を
            # 付け足す（`Response.init_headers`）——`_content_type_for` が「charset を宣言しない」
            # と判断した場合までこれで上書きされてしまうため、確定済みヘッダをそのまま使わせる。
            "Content-Type": media_type,
            "Content-Disposition": content_disposition(Path(path).name),
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "private, no-store",
        }
        return FdFileResponse(FdOwner(fd), size_bytes, st.st_mtime, media_type=None, headers=headers)


# ==== GET /ext/v1/openapi.json ====

def _ext_openapi_subset(app) -> dict:
    """`app.openapi()` から `/ext/v1` 配下（admin・利用者キー管理を除く）のパスと、そこから
    到達可能な `components.schemas` だけを抜いた OpenAPI 文書。Dify カスタムツールに直接
    インポート可能——X-API-Key 認証の5ルート（convert/search/capabilities/doc/research）のみを含める。

    `/ext/v1/keys*`（利用者本人による自己発行/一覧/失効/回復）は Cookie セッション認証
    （`_current_user`）であり X-API-Key ではない＝Dify 等の外部呼び出し元がこのキーで叩ける
    対象ではないため、admin 系と同様に除外する。
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
