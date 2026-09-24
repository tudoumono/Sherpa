"""Webhook 通知（PART-6・docs/proposals/2026-09-05-Webhook通知.md）。

取り込み run の terminal 化（sync/refresh/rebind/rerun/delete の完了・失敗）を、
`api_keys.webhook_url` を登録したキー宛てに署名付き POST で通知する（ポーリング排除）。

配送保証は軽量型（W1・裁定 2026-09-05）: 即時送信＋失敗時リトライ3回（2/8/30秒バックオフ）＋
監査記録。実送信は**単一の daemon worker スレッド＋有界キュー**（RV是正#4・`_QUEUE_MAXSIZE`）
が直列に行う——`notify_run_terminal` はキューへ積むだけで即座に返る（`ext_api._AuditWriter` と
同じ「単一 writer＋bounded queue」型だが、lifespan 連携までは持たない軽量版＝プロセス終了で
未処理分は消えてよい契約はそのまま）。キューが溢れたらその1件だけ捨てて `webhook.dropped` を
監査記録する（他の配送・取り込み自体は継続＝fail-loud だが個別行の犠牲で全体を守る）。
永続キューは持たない——プロセス終了で未送信の通知は消える（受信側は既存の
`GET /worlds/{wid}/status` ポーリングで補完できる＝劣化しても現状に戻るだけ）。

署名（W4）: `X-Sherpa-Signature: sha256=<hex(HMAC-SHA256(body_bytes, webhook_secret))>`。
`webhook_secret` は登録時に生成し平文保管する（署名生成に平文が必須＝API キーのハッシュ保管
方式は構造的に使えない。閉域 LAN・DB は管理境界内として受容）。

宛先ポリシー（W3・RV是正#1）: `llm._canonical_host_port` は「`base + path` 単純連結」契約
（`ollama_url()`）を守るため path（空/"/" 以外）・query・fragment 付き URL を解釈不能として
拒否する——Webhook の宛先は利用者の受信エンドポイントそのもので path/query を伴うのが通常
のため、この関数は流用せず `_webhook_host_port()` を別途新設する（host:port 抽出のみを行い
path/query はそのまま許す）。userinfo 拒否・scheme 既定ポート補完・末尾ドット除去は
`_canonical_host_port` と同じ規律。**loopback を含め既定は全拒否**——
`system_settings.webhook_allowlist`（host:port の配列・`ollama_allowlist` と同じ形）に明示
登録された host:port のみを許可する（Ollama の loopback 常時許可とは意図的に違える: Webhook は
自己発行キー利用者〔一般ユーザー〕が宛先を選べるため、loopback を暗黙許可すると認証なしの
内蔵サービス〔例: ES:9200〕を SSRF 経由で叩けてしまう）。DB 不達は fail-closed（allowlist
空扱い＝何も許可しない・`llm._allowlisted_hosts` と同じ規律）。
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
# 即時送信の後に続くリトライ間隔（秒）。要素数=3＝W1「失敗時リトライ3回」（合計4回試行）。
_RETRY_DELAYS_SEC = (2, 8, 30)


class WebhookUrlInvalid(ValueError):
    """Webhook 宛先 URL が不正・または宛先ポリシー（admin allowlist・RV是正#1で loopback も対象）
    を満たさない。"""


def _webhook_host_port(url: str) -> tuple[str, int] | None:
    """`url` を `(host, port)` に正規化する（解釈不能・不正なら None）。

    `llm._canonical_host_port` の Webhook 版——path/query/fragment を**許す**点だけが異なる
    （モジュール docstring 参照）。scheme は http/https のみ許可・userinfo（`user:pass@`）は
    禁止・ポート省略時は scheme の既定ポート（http=80・https=443）を補う・末尾ドットは除去する
    ——ここまでは `_canonical_host_port` と同じ規則（allowlist との突合が単純な文字列一致で
    済むよう揃える）。
    """
    try:
        p = urlparse(url or "")
    except ValueError:                       # 例: 不正な IPv6 リテラル
        return None
    if p.scheme not in ("http", "https"):
        return None
    # RV是正#8: 空文字の userinfo（`http://@host/`）は `p.username == ""`（falsy）で `or` 判定を
    # すり抜ける——`is not None` で判定し、`user:` のような片方だけの空文字も含めて確実に拒否する。
    if p.username is not None or p.password is not None:
        return None
    host = (p.hostname or "").rstrip(".")
    if not host:
        return None
    try:
        port = p.port
    except ValueError:                       # 例: ポートが数値でない/範囲外
        return None
    if port is not None:
        return host, port
    return host, 80 if p.scheme == "http" else 443


def _allowlisted_hosts(system_settings: dict | None = None) -> set[tuple[str, int]]:
    """許可された接続先（`system_settings.webhook_allowlist`）。RV是正#1: loopback もこの集合に
    含まれていなければ許可されない（`llm._allowlisted_hosts()` は非 loopback 専用だが、こちらは
    唯一の許可判定源）。DB 不達は空集合＝fail-closed。
    """
    allowed: set[tuple[str, int]] = set()
    try:
        if system_settings is not None:
            entries = system_settings.get("webhook_allowlist") or []
        else:
            from . import store              # 遅延 import（循環回避）
            entries = store.get_system_settings().get("webhook_allowlist") or []
    except Exception:
        entries = []
    for entry in entries:
        hp = llm._canonical_host_port(f"http://{entry}")   # allowlist の各エントリ自体は host:port のみ（path 無し）
        if hp is not None:
            allowed.add(hp)
    return allowed


def assert_webhook_url_allowed(url: str, *, system_settings: dict | None = None) -> None:
    """`url`（Webhook 宛先）が接続許可ポリシーを満たすか検証する（I/O なし・登録時／送信直前
    〔リトライ毎の再評価含む・RV是正#6〕の両方で呼ぶ）。RV是正#1: 既定許可なし——loopback も
    例外にせず、`_allowlisted_hosts()` に host:port が正規化一致するものだけ許可する（自己発行
    キー利用者が認証なしの内蔵サービスを宛先登録できてしまう穴を塞ぐ・モジュール docstring
    参照）。不正 URL／不許可の宛先は `WebhookUrlInvalid` を送出する。
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
    """1回分の送信（2xx 以外・接続エラー等は例外として呼び出し元へ伝播＝リトライ対象）。
    `llm.urlopen_no_redirect` を流用（redirect 非追跡・共有 opener・R2a と同じ安全側の既定）。

    RV是正#3: 応答本体は読まない（`urlopen` は 2xx 以外を `HTTPError` として送出する契約＝
    `with` を抜けた時点で 2xx 確定・本文を読む必要がない）。相手が無制限に本文を返すエンドポイント
    でも、ここでメモリを消費しない——`with` を抜ける際のクローズだけで済ませる。
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
    """1キー分の配送（即時送信＋失敗時リトライ3回・W1）。単一 daemon worker（`_worker_loop`）の
    中で他キーの配送と直列に実行される想定（RV是正#4・呼び出し元は `notify_run_terminal` が
    積んだキューを worker が消費する）。監査は最終結果（成功／全滅）のみ1行記録する
    （試行ごとには記録しない・detail に secret／フル URL は含めない）。

    RV是正#6: 宛先ポリシー（`assert_webhook_url_allowed`）は**試行ごと**に再評価する（登録時
    チェックとは別に、リトライ待機の間に admin が allowlist を変更した場合を即座に反映する）。
    不許可は恒久的な失敗＝そこで打ち切る（残りの待機・試行はしない）。
    """
    from . import store              # 遅延 import（循環回避）

    host_port = _host_port_for_audit(url)
    detail_base = {"host_port": host_port, "world": payload.get("world"),
                   "run_id": payload.get("run_id"), "event": payload.get("event")}
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_id = uuid.uuid4().hex
    attempts = 0
    last_error: Exception | None = None
    delays = (0,) + _RETRY_DELAYS_SEC   # 先頭 0 ＝即時（sleep しない）
    for delay in delays:
        if delay:
            time.sleep(delay)
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


# RV是正#4: 「イベント×キーごとに Thread を無制限生成」をやめ、単一 daemon worker が有界キューを
# 直列消費する型へ（`ext_api._AuditWriter` と同じ「単一 writer＋bounded queue」だが、lifespan
# start/stop 連携までは持たない軽量版——プロセス終了で未処理分が消えてよい契約は変わらないため）。
# 上限256＝1 world の同時 terminal 化がこれを超えて詰まることは通常考えにくい規模（監査での
# 可視化と同時に、無制限生成による OOM/FD 枯渇を防ぐことを優先する）。
_QUEUE_MAXSIZE = 256
_queue: queue.Queue = queue.Queue(maxsize=_QUEUE_MAXSIZE)
_worker_thread: threading.Thread | None = None
_worker_lock = threading.Lock()


def _process_queue_item(item: tuple) -> None:
    """キューから取り出した1件を処理する（`_worker_loop` の本体・単体テストが実スレッド/実
    キューなしで直接呼べるよう分離）。`_deliver` 自身は例外を握るが、想定外の例外で worker
    自体が落ちて以後の配送が止まらないよう、ここでも最外周として捕捉する。"""
    try:
        _deliver(*item)
    except Exception:
        _log.warning("Webhook 配送処理で未捕捉の例外が発生しました（worker は継続します）",
                     exc_info=True)


def _worker_loop() -> None:
    """単一 daemon worker 本体。キューから1件ずつ取り出し `_process_queue_item` を直列実行し
    続ける（プロセス生存中は戻らない想定・daemon thread なのでプロセス終了時に強制終了して
    問題ない）。"""
    while True:
        item = _queue.get()
        try:
            _process_queue_item(item)
        finally:
            _queue.task_done()


def _ensure_worker_started() -> None:
    """worker が未起動なら起こす（lazy start・複数回呼んでも1本しか起動しない）。"""
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
    """1キー分の配送をキューへ積む（`notify_run_terminal` から呼ぶ）。キューが飽和していれば
    このキュー投入だけを待たず即座に諦め、`webhook.dropped` を監査記録する（RV是正#4・
    他キーの配送・取り込み自体は継続する＝1件の犠牲で全体を守る）。
    """
    _ensure_worker_started()
    try:
        _queue.put_nowait((key_id, url, secret, payload))
        return
    except queue.Full:
        pass
    _log.warning("Webhook 配送キューが飽和したため1件破棄しました: key_id=%s world=%s",
                key_id, payload.get("world"))
    try:
        from . import store          # 遅延 import（循環回避）
        store.audit("system", "webhook.dropped", "webhook", str(key_id),
                    detail={"host_port": _host_port_for_audit(url), "world": payload.get("world"),
                           "run_id": payload.get("run_id"), "event": payload.get("event")},
                    outcome="failure", severity="warning", reason="queue_full")
    except Exception:
        _log.warning("Webhook 破棄の監査記録に失敗しました（best-effort）", exc_info=True)


def notify_run_terminal(world: str, run_id: int | None, op: str, status: str, *,
                        doc_count: int | None = None) -> None:
    """取り込み run の terminal 化を、`world` を許可する Webhook 登録済みキー全部へ通知する
    （イベント仕様は `docs/proposals/2026-09-05-Webhook通知.md` 参照）。

    best-effort・呼び出し元（`ingest.worker._record`／`worlds._finalize_pending_run`／
    `routers/worlds._run_delete_background`／`background.py` の最外周セーフティネット）は
    例外を気にせず呼べる（内部で全て捕捉し、取り込み自体の成否へは一切昇格させない）。実送信は
    単一 daemon worker が有界キューを直列消費する（RV是正#4）——ここでは対象キーの列挙と
    キューへの投入だけを行う。

    `status` は `ingest_runs.status`（'extracting' はここへ渡らない前提＝terminal 化のみが
    呼ぶ）: auto_published/auto_published_with_flags→`ingest.completed`・failed→`ingest.failed`。
    `op` は sync/refresh/rebind/rerun/delete のいずれか（呼び出し元が決める・情報用途のみ）。
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
