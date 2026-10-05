"""認証エンドポイント（`POST /auth/login`・`GET /auth/me`・`POST /auth/logout`・`POST /auth/change-password`）。
api.py 側は `auth_routes.auth_router` を別名で import する（`from sherpa import auth` の束縛を上書きしないため）。`_ensure_initial_admin` は `sherpa/deps.py` にある。
`sherpa.api` を import しない。auth/ratelimit/store はモジュール import にする（tests/api/test_ratelimit.py が `ratelimit.time.time`/`ratelimit.EXT_API_RATE_LIMIT_PER_MINUTE` をモジュール属性で差し替える）。
設計: docs/design/users.md「ログインとセッション」
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Request, Response
from pydantic import BaseModel

from sherpa import auth, ratelimit, store
from sherpa.deps import _COOKIE, _client_ip_hash, _current_user, _ensure_initial_admin, _validate_new_password
from sherpa.schemas import AuthLoginResponse, AuthMeResponse, OkResponse

_log = logging.getLogger("sherpa")

# セッション有効期間（日）。
_SESSION_DAYS = 7

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
auth_router = APIRouter()


class LoginReq(BaseModel):
    username: str  # uid（ユーザー名）。
    password: str


class PasswordChangeReq(BaseModel):
    current_password: str
    new_password: str
    confirm_password: str


def _set_session_cookie(response: Response, token: str, secure: bool) -> None:
    """Cookie をセットする（HttpOnly / SameSite=Lax / Secure は条件付き）。"""
    response.set_cookie(
        _COOKIE, token,
        httponly=True, samesite="lax", path="/",
        secure=secure,
        max_age=_SESSION_DAYS * 86400,
    )


def _clear_session_cookie(response: Response) -> None:
    response.delete_cookie(_COOKIE, path="/", httponly=True, samesite="lax")


def _is_secure(request: Request | None = None) -> bool:
    """セッション cookie に Secure を付けるか。
    - `SHERPA_COOKIE_SECURE=1`: 常に付ける（HTTPS 終端が別ホストでアプリには http で届く構成向け）。
    - `SHERPA_COOKIE_SECURE=0`: 付けない。
    - 未指定（auto）: このログイン要求が HTTPS で来たときだけ付ける（uvicorn は 127.0.0.1 からの X-Forwarded-Proto を信用する）。
    auth 無効（互換モード）は付けない。
    """
    if auth.auth_disabled():
        return False
    forced = os.environ.get("SHERPA_COOKIE_SECURE", "").strip().lower()
    if forced in ("1", "true", "yes", "on"):
        return True
    if forced in ("0", "false", "no", "off"):
        return False
    if request is None:  # 判断材料が無い呼び出しは安全側（SHERPA_ENV の production 判定）。
        env = os.environ.get("SHERPA_ENV", "").lower()
        return env not in ("dev", "development", "")
    return request.url.scheme == "https"


@auth_router.post("/auth/login", tags=["認証"], response_model=AuthLoginResponse)
def auth_login(req: LoginReq, request: Request, response: Response):
    """ユーザー名＋パスワードでログインし、成功で cookie を発行する。失敗は汎用 401。"""
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]

    uid = (req.username or "").strip()

    # 初期 admin bootstrap（未設定でも既定初期パスワードでログインできる）。
    _ensure_initial_admin(ip_hash=ip_hash, user_agent=ua)

    # ログイン失敗バックオフ（パスワード照合より前に判定）。同一 uid（正規化しない）の連続失敗が閾値に達したら一定時間その uid への試行を拒否する。
    remaining = ratelimit.check_login_lockout(uid)
    if remaining is not None:
        try:
            store.audit(None, "auth.login_failed", "user", f"user:{uid}",
                        detail={"attempted_uid": uid, "rate_limited": True},
                        outcome="deny", reason="rate_limited",
                        severity="warning", ip_hash=ip_hash, user_agent=ua)
        except Exception:
            _log.critical("audit write failed for auth.login_failed (rate_limited)")
        raise HTTPException(429, "試行回数が多すぎます。しばらく待ってから再度お試しください",
                            headers={"Retry-After": str(int(remaining) + 1)})

    # 通常ログイン照合。
    db_user = store.get_user_by_uid(uid)

    def _fail_login(reason: str):
        ratelimit.record_login_failure(uid)
        try:
            store.audit(None, "auth.login_failed", "user", f"user:{uid}",
                        detail={"attempted_uid": uid},
                        outcome="deny", reason=reason,
                        severity="warning", ip_hash=ip_hash, user_agent=ua)
        except Exception:
            _log.critical("audit write failed for auth.login_failed")
        raise HTTPException(401, "ユーザー名またはパスワードが正しくありません")

    # 定数時間化: uid 不存在・password_hash なし・無効化済みの場合もダミーハッシュに対して `verify_password()` を実行してから失敗させ、時間差からアカウントの存在を推測されないようにする。
    if not db_user or not db_user.get("password_hash") or db_user["status"] != "active":
        auth.verify_password(req.password, auth._DUMMY_PASSWORD_HASH)
        if not db_user:
            _fail_login("user_not_found")
        if not db_user.get("password_hash"):
            _fail_login("user_not_found")
        _fail_login("user_disabled")
    if not auth.verify_password(req.password, db_user.get("password_hash")):
        _fail_login("bad_credentials")

    # ロックアウトの二重チェック: 照合前のチェックと今回の照合の間に別リクエストの失敗でロックアウトが成立しうるため、セッション発行の直前にもう一度確認し、ロック中なら 429 にする。
    remaining = ratelimit.check_login_lockout(uid)
    if remaining is not None:
        try:
            store.audit(None, "auth.login_failed", "user", f"user:{uid}",
                        detail={"attempted_uid": uid, "rate_limited": True},
                        outcome="deny", reason="rate_limited",
                        severity="warning", ip_hash=ip_hash, user_agent=ua)
        except Exception:
            _log.critical("audit write failed for auth.login_failed (rate_limited)")
        raise HTTPException(429, "試行回数が多すぎます。しばらく待ってから再度お試しください",
                            headers={"Retry-After": str(int(remaining) + 1)})

    # セッション発行（fail-closed: 監査に失敗したら session を出さない）。
    ratelimit.record_login_success(uid)
    token = auth.new_token()
    th = auth.token_hash(token)
    expires = datetime.now(timezone.utc) + timedelta(days=_SESSION_DAYS)
    store.create_session(uid, th, expires)
    store.set_last_login(uid)

    try:
        store.audit(uid, "auth.login", "user", f"user:{uid}",
                    detail={"login_method": "password"},
                    outcome="success", severity="info",
                    ip_hash=ip_hash, user_agent=ua)
    except Exception:
        # 監査失敗なら session も出さない（fail-closed）。
        try:
            store.revoke_session(th)
        except Exception:
            pass
        _log.critical("audit write failed for auth.login – session revoked")
        raise HTTPException(500, "認証処理中にエラーが発生しました")

    _set_session_cookie(response, token, _is_secure(request))
    must_change = bool(db_user.get("must_change_password"))
    return {"ok": True, "uid": uid, "must_change_password": must_change,
            "next": "/ui/change-password.html" if must_change else None}


@auth_router.get("/auth/me", tags=["認証"], response_model=AuthMeResponse)
def auth_me(request: Request):
    """現在のログインユーザー情報を返す。
    `auth_disabled` は互換モード（`SHERPA_AUTH_DISABLED=1`）かどうか。合成 admin と実ログイン admin は uid/role が同一のため、トップバーのログアウト/パスワード変更導線の出し分けに使う。
    """
    u = _current_user(request, allow_password_change=True)
    return {"uid": u["uid"], "email": u.get("email"), "display_name": u.get("display_name"),
            "role": u["role"], "must_change_password": bool(u.get("must_change_password")),
            "auth_disabled": auth.auth_disabled()}


@auth_router.post("/auth/logout", tags=["認証"], response_model=OkResponse)
def auth_logout(request: Request, response: Response):
    """セッションを失効させ cookie をクリアする。"""
    if auth.auth_disabled():
        _clear_session_cookie(response)
        return {"ok": True}
    token = request.cookies.get(_COOKIE)
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    if token:
        u = _current_user(request, allow_password_change=True)
        th = auth.token_hash(token)
        store.revoke_session(th)
        try:
            store.audit(u["uid"], "auth.logout", "user", f"user:{u['uid']}",
                        detail={"revoked": True},
                        outcome="success", ip_hash=ip_hash, user_agent=ua)
        except Exception:
            _log.warning("audit write failed for auth.logout (best-effort)")
    _clear_session_cookie(response)
    return {"ok": True}


@auth_router.post("/auth/change-password", tags=["認証"])
def auth_change_password(req: PasswordChangeReq, request: Request):
    """現在ユーザーのパスワードを変更する。初回ログイン時はこの完了まで他機能を使えない。"""
    u = _current_user(request, allow_password_change=True)
    uid = u["uid"]
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    db_user = store.get_user_by_uid(uid)
    if not db_user:
        raise HTTPException(401, "セッションが無効です（ログインし直してください）")

    def _fail(reason: str, detail: str):
        try:
            store.audit(uid, "auth.password_change_failed", "user", f"user:{uid}",
                        detail={"reason": reason},
                        outcome="deny", reason=reason, severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            _log.warning("audit write failed for auth.password_change_failed")
        raise HTTPException(422 if reason != "bad_current_password" else 401, detail)

    if not auth.verify_password(req.current_password, db_user.get("password_hash")):
        _fail("bad_current_password", "現在のパスワードが正しくありません")
    problem = _validate_new_password(uid, req.current_password, req.new_password, req.confirm_password)
    if problem:
        _fail("weak_new_password", problem)

    was_initial = bool(db_user.get("must_change_password"))
    store.upsert_user(
        uid,
        password_hash=auth.hash_password(req.new_password),
        role=db_user["role"],
        status=db_user["status"],
        must_change_password=False,
    )
    try:
        store.audit(uid, "auth.initial_password_changed" if was_initial else "auth.password_changed",
                    "user", f"user:{uid}",
                    detail={"initial": was_initial},
                    outcome="success", severity="critical" if was_initial else "info",
                    ip_hash=ip_hash, user_agent=ua)
    except Exception:
        _log.critical("audit write failed for password change")
        raise HTTPException(500, "パスワード変更の監査ログ記録に失敗しました")
    return {"ok": True, "uid": uid, "must_change_password": False}
