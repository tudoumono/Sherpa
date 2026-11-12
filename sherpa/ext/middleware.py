"""`/ext/v1/*` だけに関与する生の ASGI ミドルウェア（X-Request-Id・アプリログ束縛・request-level 監査の一元窓口）。
設計: docs/design/external-api.md「共通の約束」
"""
from __future__ import annotations

import asyncio
import json
import logging
import time

from sherpa.ext.audit import _ACTION_BY_PATH, _OUTCOME_ERROR, _init_audit_pending, _write_pending_audit_async
from sherpa.ext.auth import _key_audit_detail, _verify_key_sync
from sherpa.ext.request_id import _request_id_ctx, _resolve_request_id

_log = logging.getLogger("sherpa")

_INTERNAL_ERROR_BODY = json.dumps({"detail": "内部エラーが発生しました"}, ensure_ascii=False).encode("utf-8")


def _request_id_of(scope) -> str:
    """要求ヘッダの X-Request-Id（大文字小文字を無視して最初の1つ）を検証して採用する。不正/無指定なら採番する。"""
    raw = None
    for k, v in (scope.get("headers") or []):
        if k.lower() == b"x-request-id":
            raw = v.decode("latin-1")
            break
    return _resolve_request_id(raw)


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
        req_id = _request_id_of(scope)
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
