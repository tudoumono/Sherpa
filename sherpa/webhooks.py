"""Webhook 通知。取り込み run の完了・失敗（sync/refresh/rebind/rerun/delete）を、`api_keys.webhook_url` を登録したキー宛てに署名付き POST で通知する。

配送は「先に保存してから送る」: 通知は宛先の鍵ごとに配送待ちの表（`store.outbox`）へ保存して返り、送り手（`outbox_sender`）が
1回ずつ送って、送れたら完了にする。失敗は 2/8/30 秒空けて再送し（合計4回）、それでも届かなければ理由を残して諦める
（`webhook.delivered`／`webhook.failed` を監査記録）。取り込みワーカーが終わっても残りは API のプロセスの送り手が引き継ぐ。
配送待ちが上限なら保存せず `webhook.dropped` を監査記録する。宛先は送信のたびに鍵から引き直す。
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
import time
import urllib.request
import uuid
from datetime import datetime, timezone
from urllib.parse import urlparse

from . import llm, outbox_sender
from .store import outbox

_log = logging.getLogger(__name__)

_TIMEOUT_SEC = 5
# 失敗後に続くリトライ間隔（秒）。要素数3＝失敗時リトライ3回（合計4回試行）
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


def _audit_detail(row: dict) -> dict:
    """配送の監査 detail（secret／フル URL は含めない）。宛先は鍵の現在の登録から引く。"""
    payload = row["payload"]
    dest = _destination(row["key_id"])
    detail = {"host_port": _host_port_for_audit(dest[0]) if dest else "（未解決）",
              "world": payload.get("world"), "run_id": payload.get("run_id"),
              "event": payload.get("event"), "attempts": row["attempts"]}
    if "job_id" in payload:
        detail["job_id"] = payload["job_id"]
    return detail


def _destination(key_id: int) -> tuple[str, str] | None:
    from . import store  # 遅延 import（循環回避）
    dest = store.get_api_key_webhook(key_id)
    if not dest or not dest.get("webhook_url") or not dest.get("webhook_secret"):
        return None
    return dest["webhook_url"], dest["webhook_secret"]


def _deliver_row(row: dict) -> None:
    """配送待ちの1行を1回送る。宛先は送信のたびに鍵から引き直し、許可リストで確かめる。

    宛先が無い・不許可は再送しても直らないので `GiveUp`。それ以外の失敗は例外のまま伝え、送り手が再送を予約する。成功は `webhook.delivered` を監査記録する。
    """
    from . import store  # 遅延 import（循環回避）
    payload = row["payload"]
    dest = _destination(row["key_id"])
    if dest is None:
        raise outbox_sender.GiveUp("destination_removed")
    url, secret = dest
    try:
        assert_webhook_url_allowed(url)
    except WebhookUrlInvalid:
        raise outbox_sender.GiveUp("destination_not_allowed") from None
    body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
    request_id = uuid.uuid5(uuid.NAMESPACE_URL, f"sherpa-outbox:{row['id']}").hex  # 再送でも同じ ID
    _send_once(url, secret, body, request_id, payload.get("event", ""))
    try:
        store.audit("system", "webhook.delivered", "webhook", str(row["key_id"]),
                    detail=_audit_detail(row), outcome="success")
    except Exception:
        _log.warning("Webhook 送信成功の監査記録に失敗しました（best-effort）", exc_info=True)


def _record_gave_up(row: dict, reason: str) -> None:
    from . import store  # 遅延 import（循環回避）
    _log.warning("Webhook を諦めました: key_id=%s reason=%s", row["key_id"], reason)
    try:
        store.audit("system", "webhook.failed", "webhook", str(row["key_id"]),
                    detail=_audit_detail(row), outcome="failure", severity="warning", reason=reason)
    except Exception:
        _log.warning("Webhook 送信失敗の監査記録に失敗しました（best-effort）", exc_info=True)


KIND = outbox_sender.Kind(deliver=_deliver_row, delays=_RETRY_DELAYS_SEC, on_gave_up=_record_gave_up)


def drain(timeout: float = 60.0) -> int:
    """手元で今送れる Webhook の配送待ちを送る（1 件で終わるプロセス＝取り込みワーカーが終了前に呼ぶ）。今送れる行が無くなったら、`timeout` 秒より前でもすぐ終える。
    再送待ちの行・他の送り手が貸出中の行は待たず、API のプロセスの送り手に任せる。残っている件数を返す。
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline and outbox_sender.run_due({"webhook": KIND}, deadline=deadline):
        pass
    return outbox.pending_count(("webhook",))


def _enqueue(key_id: int, payload: dict) -> None:
    """1キー分の通知を配送待ちの表へ保存する。上限（`outbox.MAX_PENDING_PER_KIND`）なら保存せず `webhook.dropped` を監査記録する。"""
    try:
        outbox.add("webhook", payload, key_id=key_id)
        return
    except outbox.OutboxFull:
        pass
    except Exception:
        _log.warning("Webhook の配送待ちへの保存に失敗しました（key_id=%s world=%s）",
                     key_id, payload.get("world"), exc_info=True)
        return
    record_dropped(key_id, payload)


def record_dropped(key_id: int, payload: dict) -> None:
    """配送待ちが上限で保存できなかった通知を `webhook.dropped` として監査記録する。"""
    _log.warning("Webhook の配送待ちが上限のため1件保存できませんでした: key_id=%s world=%s",
                key_id, payload.get("world"))
    try:
        from . import store  # 遅延 import（循環回避）
        dest = _destination(key_id)
        store.audit("system", "webhook.dropped", "webhook", str(key_id),
                    detail={"host_port": _host_port_for_audit(dest[0]) if dest else "（未解決）",
                           "world": payload.get("world"), "run_id": payload.get("run_id"),
                           "event": payload.get("event"),
                           **({"job_id": payload["job_id"]} if "job_id" in payload else {})},
                    outcome="failure", severity="warning", reason="outbox_full")
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
        _enqueue(key["id"], payload)


def codex_job_payload(job_id: str, status: str, finished_at) -> dict:
    """Codex ジョブの終了通知の本文（専用の4項目のみ・回答本文は載せない）。保存は `store.codex_jobs` が終端化と同じトランザクションで行う。
    宛先は送信時に鍵から読み、外されていれば `_deliver_row` が `destination_removed` で諦めて監査する。
    """
    return {"event": f"codex_job.{status}", "job_id": job_id, "status": status,
            "finished_at": str(finished_at) if finished_at is not None else None}
