"""APIキーの生成・ハッシュ・検証（`require_api_key` と ExtRequestMiddleware のフォールバックが共用する単一の実装）と world スコープの強制。
設計: docs/design/external-api.md「共通の約束」
"""
from __future__ import annotations

import hashlib
import hmac
import secrets

from fastapi import HTTPException, Request, Security
from fastapi.security import APIKeyHeader

from sherpa import ratelimit, store
from sherpa.ext.audit import _ACTION_BY_PATH, _OUTCOME_DENY, _SEVERITY_WARNING, _init_audit_pending
from sherpa.ext.request_id import _REQUEST_ID_HEADER, _resolve_request_id

_KEY_PREFIX = "sk-ext-"


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
