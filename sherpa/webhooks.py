"""Webhook 通知（PART-6・docs/proposals/2026-09-05-Webhook通知.md）。

取り込み run の terminal 化（sync/refresh/rebind/rerun/delete の完了・失敗）を、
`api_keys.webhook_url` を登録したキー宛てに署名付き POST で通知する（ポーリング排除）。

配送保証は軽量型（W1・裁定 2026-09-05）: 即時送信＋失敗時リトライ3回（2/8/30秒バックオフ・
プロセス内 daemon thread）＋監査記録。永続キューは持たない——プロセス終了で未送信の通知は
消える（受信側は既存の `GET /worlds/{wid}/status` ポーリングで補完できる＝劣化しても現状に
戻るだけ）。

署名（W4）: `X-Sherpa-Signature: sha256=<hex(HMAC-SHA256(body_bytes, webhook_secret))>`。
`webhook_secret` は登録時に生成し平文保管する（署名生成に平文が必須＝API キーのハッシュ保管
方式は構造的に使えない。閉域 LAN・DB は管理境界内として受容）。

宛先ポリシー（W3）: `llm._canonical_host_port` は「`base + path` 単純連結」契約
（`ollama_url()`）を守るため path（空/"/" 以外）・query・fragment 付き URL を解釈不能として
拒否する——Webhook の宛先は利用者の受信エンドポイントそのもので path/query を伴うのが通常
のため、この関数は流用せず `_webhook_host_port()` を別途新設する（host:port 抽出のみを行い
path/query はそのまま許す）。userinfo 拒否・scheme 既定ポート補完・末尾ドット除去は
`_canonical_host_port` と同じ規律。loopback は常時許可・それ以外は
`system_settings.webhook_allowlist`（host:port の配列・`ollama_allowlist` と同じ形）所属の
みを許可する。DB 不達は fail-closed（allowlist 空扱い＝loopback 以外は拒否・
`llm._allowlisted_hosts` と同じ規律）。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging
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
    """Webhook 宛先 URL が不正・または宛先ポリシー（loopback／admin allowlist）を満たさない。"""


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
    if p.username or p.password:
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
    """非 loopback 接続先の許可リスト（`system_settings.webhook_allowlist`）。
    `llm._allowlisted_hosts()` と同型（唯一の真実源は admin 保存値・DB 不達は空集合＝fail-closed）。
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
    """`url`（Webhook 宛先）が接続許可ポリシーを満たすか検証する（I/O なし・登録時＝送信直前の
    両方で呼ぶ）。既定許可＝loopback のみ。非 loopback は `_allowlisted_hosts()` に host:port が
    正規化一致するものだけ許可。不正 URL／不許可の宛先は `WebhookUrlInvalid` を送出する。
    """
    hp = _webhook_host_port(url)
    if hp is None:
        raise WebhookUrlInvalid("不正な Webhook URL です（http/https の URL を指定してください）")
    host, port = hp
    if llm.is_loopback_host(host):
        return
    if (host, port) not in _allowlisted_hosts(system_settings):
        raise WebhookUrlInvalid(f"許可されていない Webhook 宛先です: {host}:{port}（admin allowlist 未登録）")


def _sign(secret: str, body: bytes) -> str:
    """`X-Sherpa-Signature` の値（`sha256=<hex(HMAC-SHA256(body, secret))>`）。"""
    digest = hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


def _send_once(url: str, secret: str, body: bytes, request_id: str, event: str) -> None:
    """1回分の送信（2xx 以外・接続エラー等は例外として呼び出し元へ伝播＝リトライ対象）。
    `llm.urlopen_no_redirect` を流用（redirect 非追跡・共有 opener・R2a と同じ安全側の既定）。
    """
    headers = {
        "Content-Type": "application/json",
        "X-Sherpa-Event": event,
        "X-Request-Id": request_id,
        "X-Sherpa-Signature": _sign(secret, body),
    }
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with llm.urlopen_no_redirect(req, timeout=_TIMEOUT_SEC) as r:
        r.read()   # 応答本体は使わない（読み切ってコネクションを解放する）


def _host_port_for_audit(url: str) -> str:
    """監査 detail に残す「安全な宛先表現」（host:port のみ・path/query/secret は含めない）。"""
    hp = _webhook_host_port(url)
    return llm.format_host_port(hp[0], hp[1]) if hp is not None else "（解析できません）"


def _deliver(key_id: int, url: str, secret: str, payload: dict) -> None:
    """1キー分の配送（即時送信＋失敗時リトライ3回・W1）。daemon thread の中で実行される想定
    （呼び出し元 `notify_run_terminal` が thread を起こす）。監査は最終結果（成功／全滅）のみ
    1行記録する（試行ごとには記録しない・detail に secret／フル URL は含めない）。
    """
    from . import store              # 遅延 import（循環回避）

    host_port = _host_port_for_audit(url)
    detail_base = {"host_port": host_port, "world": payload.get("world"),
                   "run_id": payload.get("run_id"), "event": payload.get("event")}
    try:
        assert_webhook_url_allowed(url)
    except WebhookUrlInvalid as e:
        # 送信直前の再確認で不許可（登録後に admin が allowlist を外した等）＝リトライしても
        # 変わらないため1回で打ち切る。
        try:
            store.audit("system", "webhook.failed", "webhook", str(key_id),
                        detail={**detail_base, "attempts": 0}, outcome="failure",
                        severity="warning", reason=e.__class__.__name__)
        except Exception:
            _log.warning("Webhook 送信不許可の監査記録に失敗しました（best-effort）", exc_info=True)
        return
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_id = uuid.uuid4().hex
    attempts = 0
    last_error: Exception | None = None
    delays = (0,) + _RETRY_DELAYS_SEC   # 先頭 0 ＝即時（sleep しない）
    for delay in delays:
        if delay:
            time.sleep(delay)
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


def notify_run_terminal(world: str, run_id: int | None, op: str, status: str, *,
                        doc_count: int | None = None) -> None:
    """取り込み run の terminal 化を、`world` を許可する Webhook 登録済みキー全部へ通知する
    （イベント仕様は `docs/proposals/2026-09-05-Webhook通知.md` 参照）。

    best-effort・呼び出し元（`ingest.worker._record`／`worlds._finalize_pending_run`／
    `routers/worlds._run_delete_background`）は例外を気にせず呼べる（内部で全て捕捉し、
    取り込み自体の成否へは一切昇格させない）。実送信はキーごとに1本の daemon thread（W1 の
    軽量配送）——ここでは対象キーの列挙とスケジューリングだけを行う。

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
        threading.Thread(
            target=_deliver, args=(key["id"], key["webhook_url"], key["webhook_secret"], payload),
            daemon=True, name=f"sherpa-webhook-{key['id']}",
        ).start()
