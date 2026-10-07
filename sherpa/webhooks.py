"""Webhook 通知。取り込み run の完了・失敗（sync/refresh/rebind/rerun/delete）を、`api_keys.webhook_url` を登録したキー宛てに署名付き POST で通知する。

配送は軽量型: 即時送信＋失敗時リトライ3回（2/8/30秒バックオフ）＋監査記録。実送信は単一の daemon worker スレッドが有界キュー
（`_QUEUE_MAXSIZE`）を直列に消費し、`notify_run_terminal` はキューへ積むだけで返る。キューが溢れたらその1件だけ捨てて
`webhook.dropped` を監査記録する。永続キューは持たない（プロセス終了で未送信分は消える）。
署名: `X-Sherpa-Signature: sha256=<hex(HMAC-SHA256(body_bytes, webhook_secret))>`。`webhook_secret` は登録時に生成し平文保管する。
宛先ポリシー: path/query を許す宛先のため `_webhook_host_port()` で host:port を抽出する（userinfo 拒否・scheme 既定ポート補完・
末尾ドット除去は `llm._canonical_host_port` と同じ）。loopback を含め既定は全拒否で、`system_settings.webhook_allowlist` に
明示登録された host:port のみ許可する（DB 不達は allowlist 空扱い＝fail-closed）。
設計: docs/design/external-api.md「終了通知（Webhook）」
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
import queue
import threading
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import llm

_log = logging.getLogger(__name__)

_TIMEOUT_SEC = 5
# 即時送信の後に続くリトライ間隔（秒）。要素数3＝失敗時リトライ3回（合計4回試行）
_RETRY_DELAYS_SEC = (2, 8, 30)


class WebhookUrlInvalid(ValueError):
    """Webhook 宛先 URL が不正、または宛先ポリシー（admin allowlist）を満たさない。"""


def _webhook_host_port(url: str) -> tuple[str, int] | None:
    """`url` を `(host, port)` に正規化する（解釈不能・不正なら None）。

    `llm._canonical_host_port` の Webhook 版で、path/query/fragment を許す点だけが異なる。http/https のみ・userinfo 禁止・
    ポート省略時は scheme の既定ポートを補う・末尾ドットは除去する。
    """
    try:
        p = urlparse(url or "")
    except ValueError:
        return None
    if p.scheme not in ("http", "https"):
        return None
    # 空文字の userinfo（`http://@host/`）も拒否するため `is not None` で判定する
    if p.username is not None or p.password is not None:
        return None
    host = (p.hostname or "").rstrip(".")
    if not host:
        return None
    try:
        port = p.port
    except ValueError:
        return None
    if port is not None:
        return host, port
    return host, 80 if p.scheme == "http" else 443


def _allowlisted_hosts(system_settings: dict | None = None) -> set[tuple[str, int]]:
    """許可された接続先（`system_settings.webhook_allowlist`）。loopback もこの集合に含まれていなければ許可されない。DB 不達は空集合＝fail-closed。"""
    allowed: set[tuple[str, int]] = set()
    try:
        if system_settings is not None:
            entries = system_settings.get("webhook_allowlist") or []
        else:
            from . import store  # 遅延 import（循環回避）
            entries = store.get_system_settings().get("webhook_allowlist") or []
    except Exception:
        entries = []
    for entry in entries:
        hp = llm._canonical_host_port(f"http://{entry}")  # allowlist の各エントリは host:port のみ
        if hp is not None:
            allowed.add(hp)
    return allowed


def assert_webhook_url_allowed(url: str, *, system_settings: dict | None = None) -> None:
    """`url` が接続許可ポリシーを満たすか検証する（I/O なし。登録時と送信直前の両方で呼ぶ）。

    既定許可なし。`_allowlisted_hosts()` に host:port が一致するものだけ許可する。不正 URL／不許可の宛先は `WebhookUrlInvalid`。
    """
    hp = _webhook_host_port(url)
    if hp is None:
        raise WebhookUrlInvalid("不正な Webhook URL です（http/https の URL を指定してください）")
    host, port = hp
    if (host, port) not in _allowlisted_hosts(system_settings):
        raise WebhookUrlInvalid(f"許可されていない Webhook 宛先です: {host}:{port}（admin allowlist 未登録）")


def _sign(secret: str, body: bytes) -> str:
    """`X-Sherpa-Signature` の値（`sha256=<hex(HMAC-SHA256(body, secret))>`）。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _send_once(url: str, secret: str, body: bytes, request_id: str, event: str) -> None:
    """1回分の送信（2xx 以外・接続エラー等は例外として伝播＝リトライ対象）。

    `llm.urlopen_no_redirect` を使う（redirect 非追跡）。応答本体は読まない。
    """
    headers = {
        "Content-Type": "application/json",
        "X-Sherpa-Event": event,
        "X-Request-Id": request_id,
        "X-Sherpa-Signature": _sign(secret, body),
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with llm.urlopen_no_redirect(req, timeout=_TIMEOUT_SEC):
        pass


def _host_port_for_audit(url: str) -> str:
    """監査 detail に残す「安全な宛先表現」（host:port のみ・path/query/secret は含めない）。"""
    hp = _webhook_host_port(url)
    return llm.format_host_port(hp[0], hp[1]) if hp is not None else "（解析できません）"


def _deliver(key_id: int, url: str, secret: str, payload: dict) -> None:
    """1キー分の配送（即時送信＋失敗時リトライ3回）。単一 daemon worker の中で他キーの配送と直列に実行される。

    監査は最終結果（成功／全滅）のみ1行記録する（detail に secret／フル URL は含めない）。
    宛先ポリシー（`assert_webhook_url_allowed`）は試行ごとに再評価し、不許可は恒久的な失敗として打ち切る。
    """
    from . import store  # 遅延 import（循環回避）

    # Codex ジョブの終了通知は各試行の直前に鍵の現在の宛先を引き直す（引けなければ配送を打ち切る）
    resolve_each_attempt = str(payload.get("event", "")).startswith("codex_job.")
    host_port = _host_port_for_audit(url) if url else "（未解決）"
    detail_base = {"host_port": host_port, "world": payload.get("world"),
                   "run_id": payload.get("run_id"), "event": payload.get("event")}
    if "job_id" in payload:
        detail_base["job_id"] = payload["job_id"]
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_id = uuid.uuid4().hex
    attempts = 0
    last_error: Exception | None = None
    delays = (0,) + _RETRY_DELAYS_SEC  # 先頭 0 ＝即時
    for delay in delays:
        if delay:
            time.sleep(delay)
        if resolve_each_attempt:
            dest = store.get_api_key_webhook(key_id)
            if not dest:
                _log.info("Codex ジョブの終了通知を打ち切ります（通知先が無効になりました）: job_id=%s",
                          payload.get("job_id"))
                return
            url, secret = dest["webhook_url"], dest["webhook_secret"]
        try:
            assert_webhook_url_allowed(url)
        except WebhookUrlInvalid as e:
            last_error = e
            break
        attempts += 1
        try:
            _send_once(url, secret, body, request_id, payload.get("event", ""))
            try:
                store.audit("system", "webhook.delivered", "webhook", str(key_id),
                            detail={**detail_base, "attempts": attempts}, outcome="success")
            except Exception:
                _log.warning("Webhook 送信成功の監査記録に失敗しました（best-effort）", exc_info=True)
            return
        except Exception as e:
            last_error = e
            continue
    try:
        store.audit("system", "webhook.failed", "webhook", str(key_id),
                    detail={**detail_base, "attempts": attempts}, outcome="failure",
                    severity="warning",
                    reason=last_error.__class__.__name__ if last_error else "unknown")
    except Exception:
        _log.warning("Webhook 送信失敗の監査記録に失敗しました（best-effort）", exc_info=True)


# 単一 daemon worker が有界キューを直列消費する。上限は 256（無制限の Thread 生成による OOM/FD 枯渇を防ぐ）
_QUEUE_MAXSIZE = 256
_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _process_queue_item(item: tuple) -> None:
    """キューから取り出した1件を処理する（`_worker_loop` の本体）。想定外の例外で worker が落ちないよう最外周で捕捉する。"""
    try:
        _deliver(*item)
    except Exception:
        _log.warning("Webhook 配送処理で未捕捉の例外が発生しました（worker は継続します）",
                     exc_info=True)


def _worker_loop() -> None:
    """単一 daemon worker 本体。キューから1件ずつ取り出し `_process_queue_item` を直列実行し続ける。"""
    while True:
        item = _queue.get()
        try:
            _process_queue_item(item)
        finally:
            _queue.task_done()


def _ensure_worker_started() -> None:
    """worker が未起動なら起こす（lazy start・1本しか起動しない）。"""
    global _worker_thread
    if _worker_thread is not None and _worker_thread.is_alive():
        return
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return
        _worker_thread = threading.Thread(target=_worker_loop, daemon=True,
                                          name="sherpa-webhook-worker")
        _worker_thread.start()


def _enqueue(key_id: int, url: str, secret: str, payload: dict) -> None:
    """1キー分の配送をキューへ積む。キューが飽和していれば即座に諦め、`webhook.dropped` を監査記録する。"""
    _ensure_worker_started()
    try:
        _queue.put_nowait((key_id, url, secret, payload))
        return
    except queue.Full:
        pass
    _log.warning("Webhook 配送キューが飽和したため1件破棄しました: key_id=%s world=%s",
                key_id, payload.get("world"))
    try:
        from . import store  # 遅延 import（循環回避）
        store.audit("system", "webhook.dropped", "webhook", str(key_id),
                    detail={"host_port": _host_port_for_audit(url), "world": payload.get("world"),
                           "run_id": payload.get("run_id"), "event": payload.get("event"),
                           **({"job_id": payload["job_id"]} if "job_id" in payload else {})},
                    outcome="failure", severity="warning", reason="queue_full")
    except Exception:
        _log.warning("Webhook 破棄の監査記録に失敗しました（best-effort）", exc_info=True)


def notify_run_terminal(world: str, run_id: int | None, op: str, status: str, *,
                        doc_count: int | None = None) -> None:
    """取り込み run の terminal 化を、`world` を許可する Webhook 登録済みキー全部へ通知する。

    best-effort（内部で全て捕捉し、取り込み自体の成否へ影響させない）。対象キーの列挙とキューへの投入だけを行う。
    `status` は `ingest_runs.status`（terminal のみ）: auto_published/auto_published_with_flags→`ingest.completed`・failed→`ingest.failed`。
    `op` は sync/refresh/rebind/rerun/reconvert/delete（情報用途のみ）。
    """
    try:
        from . import store
        event = ("ingest.completed" if status in ("auto_published", "auto_published_with_flags")
                 else "ingest.failed")
        keys = store.list_webhook_keys_for_world(world)
    except Exception:
        _log.warning("Webhook 対象キーの列挙に失敗しました（通知は送られません・world=%s）",
                     world, exc_info=True)
        return
    if not keys:
        return
    at = datetime.now(timezone.utc).isoformat()
    payload_base = {"event": event, "world": world, "run_id": run_id, "op": op, "status": status,
                    "doc_count": doc_count, "at": at}
    for key in keys:
        payload = dict(payload_base)
        _enqueue(key["id"], key["webhook_url"], key["webhook_secret"], payload)


def notify_codex_job_terminal(job_id: str, key_id: int, status: str, finished_at) -> None:
    """Codex ジョブの終了（completed/failed/cancelled）を、受付時に `webhook: true` を指定した鍵の通知先へ1回積む。

    署名・再送・監査は取り込み通知と同じ仕組み（`_enqueue`→`_deliver`）。本文は専用の4項目のみ（回答本文は載せない）。
    宛先は送信時に鍵から読み、外されていれば送らずログだけ残す。例外は呼び出し元へ伝播させない。
    """
    try:
        from . import store
        dest = store.get_api_key_webhook(key_id)
        if not dest or not dest.get("webhook_url") or not dest.get("webhook_secret"):
            _log.info("Codex ジョブの終了通知を送りません（通知先が未登録）: job_id=%s", job_id)
            return
        payload = {"event": f"codex_job.{status}", "job_id": job_id, "status": status,
                   "finished_at": str(finished_at) if finished_at is not None else None}
        _enqueue(key_id, dest["webhook_url"], "", payload)  # 宛先・secret は送信直前に引き直す
    except Exception:
        _log.warning("Codex ジョブの終了通知の準備に失敗しました（job_id=%s）", job_id, exc_info=True)
