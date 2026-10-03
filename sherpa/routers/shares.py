"""会話共有エンドポイント: `GET /users/suggest`・`POST /conversations/{cid}/shares`・`GET /share/conversations/{token}`・`POST /conversation-shares/{share_id}/revoke`・`POST /conversations/{wid}/fork`（引き継いで質問）・`POST /conversation-shares/{share_id}/refresh`（スナップショット更新）・`GET /conversations/{cid}/shares`（共有一覧）。
`sherpa.api` を import しない。
設計: docs/design/chat.md「共有」
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, Field

from sherpa import auth, store
from sherpa.store.conversations import SHARE_DEFAULT_EXPIRY_DAYS
from sherpa.deps import _COOKIE, _client_ip_hash, _current_user, _synthetic_admin
from sherpa.schemas import (
    ConversationForkResponse,
    ConversationShareExtendResponse,
    ConversationShareRefreshResponse,
    ShareCreateResponse,
    ShareListItem,
    UsersSuggestResponse,
)

_log = logging.getLogger("sherpa")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
router = APIRouter()


class ShareCreateReq(BaseModel):
    invitee_user_ids: list[str]
    expires_at: datetime | None = None  # 省略は作成から既定日数後。明示的な null（無期限）は 422。
    sanitize: bool = False  # 個人 workspace 参照会話を、個人部分を伏せた snapshot として共有する。


class ShareRevokeReq(BaseModel):
    pass


class ShareExtendReq(BaseModel):
    days: int = Field(SHARE_DEFAULT_EXPIRY_DAYS, ge=1, le=SHARE_DEFAULT_EXPIRY_DAYS)  # 今から最大 30 日。


@router.get("/users/suggest", tags=["会話共有"], response_model=UsersSuggestResponse)
def users_suggest(request: Request, q: str = Query("")):
    """共有ダイアログの入力補完: uid/表示名の部分一致で active ユーザーを返す（無効化ユーザー・自分自身は除く）。ログイン必須・上限10件。返す列は uid/display_name のみ。"""
    u = _current_user(request)
    q = (q or "").strip()
    if not q:
        return {"users": []}
    return {"users": store.suggest_users(q, u["uid"], limit=10)}


@router.post("/conversations/{cid}/shares", tags=["会話共有"], response_model=ShareCreateResponse)
def conversation_share_create(cid: int, req: ShareCreateReq, request: Request):
    """共有リンクを発行する（所有者のみ・個人 workspace を含む会話は拒否）。一度だけ表示する URL を返す。"""
    u = _current_user(request)
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]

    # 所有者確認。
    if not store.owns_conversation(u["uid"], cid):
        try:
            store.audit(u["uid"], "share.created", "share", None,
                        outcome="deny", reason="not_owner", severity="warning",
                        ip_hash=ip_hash, user_agent=ua,
                        detail={"conversation_id": cid})
        except Exception:
            pass
        raise HTTPException(403, "自分の会話のみ共有できます")

    # 共有は必ず期限を持つ（無期限は不可）。省略は既定日数、明示的な null だけ拒否する。
    if "expires_at" in req.model_fields_set and req.expires_at is None:
        raise HTTPException(422, "共有には有効期限が必要です（無期限にはできません）")

    # 個人 workspace ガード: sanitize=true なら個人部分を伏せた snapshot を共有する。
    share_target_cid = cid
    sanitized = False
    conv_row = store.get_conversation_for_read(u["uid"], cid)
    if req.sanitize:
        # sanitize=true は flag に関係なく必ず snapshot を共有する（ライブ元の個人内容を共有しない）。snapshot は per-turn 個人フラグで Q/A を伏字化する。
        snap = store.create_sanitized_snapshot(u["uid"], cid)
        if not snap:
            raise HTTPException(404, "会話が見つかりません")
        share_target_cid = snap
        sanitized = True
        try:
            store.audit(u["uid"], "share.sanitized_snapshot", "conversation", f"conv:{snap}",
                        detail={"source_conversation_id": cid, "snapshot_id": snap},
                        outcome="success", severity="info", ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
    # 多層防御: 会話フラグ contains_personal_workspace または個人ターン（messages.personal）が 1 件でもあれば通常共有を拒否する。
    elif conv_row and (conv_row["conversation"].get("contains_personal_workspace")
                       or store.conversation_has_personal_message(cid)):
        try:
            store.audit(u["uid"], "share.created", "share", None,
                        outcome="deny", reason="contains_personal_workspace", severity="warning",
                        ip_hash=ip_hash, user_agent=ua,
                        detail={"conversation_id": cid})
        except Exception:
            pass
        raise HTTPException(
            409, "個人 workspace を参照した会話は共有できません（sanitize=true で個人部分を除いて共有できます）")

    # 招待先 uid 検証。
    if not req.invitee_user_ids:
        raise HTTPException(422, "招待先ユーザーを1人以上指定してください")
    for iuid in req.invitee_user_ids:
        inv = store.get_user(iuid)
        if not inv or inv["status"] != "active":
            try:
                store.audit(u["uid"], "share.created", "share", None,
                            outcome="deny", reason="invitee_invalid", severity="warning",
                            ip_hash=ip_hash, user_agent=ua,
                            detail={"conversation_id": cid, "invalid_uid": iuid})
            except Exception:
                pass
            raise HTTPException(422, f"招待先ユーザー '{iuid}' が見つかりません（または無効）")

    token = auth.new_token()
    th = auth.token_hash(token)
    expires = req.expires_at
    if expires is None:
        expires = datetime.now(timezone.utc) + timedelta(days=SHARE_DEFAULT_EXPIRY_DAYS)
    elif expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    sid = store.create_share(share_target_cid, u["uid"], th, expires, req.invitee_user_ids)

    try:
        store.audit(u["uid"], "share.created", "share", f"share:{sid}",
                    detail={"conversation_id": share_target_cid, "source_conversation_id": cid,
                            "sanitized": sanitized, "owner_uid": u["uid"],
                            "invitee_uids": req.invitee_user_ids,
                            "expires_at": expires.isoformat()},
                    outcome="success", severity="info",
                    ip_hash=ip_hash, user_agent=ua)
    except Exception:
        # fail-closed: 共有は作成済みだが監査に失敗したら取り消して拒否する。
        _log.critical("audit write failed for share.created – revoking share %s", sid)
        store.revoke_share(sid, u["uid"])
        raise HTTPException(500, "共有処理中にエラーが発生しました")

    share_url = f"/share/conversations/{token}"
    return {"ok": True, "share_id": sid, "url": share_url,
            "note": "この URL は一度だけ表示されます。招待者に共有してください。"}


@router.get("/share/conversations/{token}", tags=["会話共有"])
def share_click(token: str, request: Request):
    """共有 token のクリック処理。未ログインは /ui/login.html へ、有効なら受領ラッパーを作って chat へ redirect する。"""
    u: dict | None = None
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    next_url = f"/share/conversations/{token}"

    if not auth.auth_disabled():
        raw = request.cookies.get(_COOKIE)
        if not raw:
            return RedirectResponse(f"/ui/login.html?next={next_url}", status_code=302)
        u = store.session_user(auth.token_hash(raw))
        if not u:
            return RedirectResponse(f"/ui/login.html?next={next_url}", status_code=302)
    else:
        u = _synthetic_admin()

    th = auth.token_hash(token)
    share = store.resolve_share_by_token(th)
    if not share or not share.get("active"):
        reason = "invalid_token" if not share else ("revoked" if share.get("revoked_at") else "expired")
        try:
            store.audit(u["uid"], "share.denied", "share", None,
                        outcome="deny", reason=reason, severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
        raise HTTPException(403, "この共有は開けません（無効・期限切れ・取消済みのいずれか）")

    if not store.is_invited(share["id"], u["uid"]):
        try:
            store.audit(u["uid"], "share.denied", "share", f"share:{share['id']}",
                        outcome="deny", reason="not_invited", severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
        raise HTTPException(403, "この共有は開けません（招待されていません）")

    # 受領ラッパー作成（冪等）。共有元が同時に物理削除された場合は ValueError として拒否する。
    try:
        # 受領と監査は同一トランザクション（監査失敗ならラッパーも作られない）。
        wid = store.accept_share(share["id"], u["uid"],
                                 audit={"ip_hash": ip_hash, "user_agent": ua})
    except store.ShareUnavailableError as e:
        reason = e.args[0] if e.args else "revoked"
        try:
            store.audit(u["uid"], "share.denied", "share", f"share:{share['id']}",
                        outcome="deny", reason=reason, severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
        raise HTTPException(403, "この共有は開けません（無効・期限切れ・取消済みのいずれか）")
    except ValueError:
        try:
            store.audit(u["uid"], "share.denied", "share", f"share:{share['id']}",
                        outcome="deny", reason="source_gone", severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
        raise HTTPException(403, "この共有は開けません（共有元が削除されました）")
    except Exception:
        _log.critical("share.accepted failed (share %s) – fail-closed", share["id"])
        raise HTTPException(500, "共有処理中にエラーが発生しました")

    return RedirectResponse(f"/ui/chat.html?conversation_id={wid}", status_code=302)


@router.post("/conversation-shares/{share_id}/revoke", tags=["会話共有"])
def conversation_share_revoke(share_id: int, request: Request):
    """共有を取り消す（所有者のみ）。"""
    u = _current_user(request)
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    try:
        # 取消と監査は同一トランザクション（監査失敗なら取消も戻る）。
        ok = store.revoke_share(share_id, u["uid"], audit={"ip_hash": ip_hash, "user_agent": ua})
    except Exception:
        _log.critical("share.revoked failed (fail-closed)")
        raise HTTPException(500, "取消処理中にエラーが発生しました")
    if not ok:
        try:
            store.audit(u["uid"], "share.revoked", "share", f"share:{share_id}",
                        outcome="deny", reason="not_owner_or_already_revoked", severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
        raise HTTPException(403, "取消できません（所有者以外 or 既に取消済み）")
    return {"ok": True, "share_id": share_id}


@router.post("/conversation-shares/{share_id}/extend", tags=["会話共有"],
             response_model=ConversationShareExtendResponse)
def conversation_share_extend(share_id: int, request: Request, req: ShareExtendReq | None = None):
    """共有の期限を今から `days` 日（1〜30・省略30）へ延ばす（所有者のみ・何度でも）。
    期限切れでも未取消なら延長できる。取消済み・所有者以外・存在しない共有はいずれも 404。期限の更新と監査の記録は同じトランザクションで行い、監査を記録できなければ期限も変えない（500）。
    """
    u = _current_user(request)
    days = (req or ShareExtendReq()).days
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    try:
        expires = store.extend_share(share_id, u["uid"], days, audit={"ip_hash": ip_hash, "user_agent": ua})
    except Exception:
        _log.critical("share.extended failed (fail-closed)")
        raise HTTPException(500, "延長処理中にエラーが発生しました")
    if expires is None:
        raise HTTPException(404, "共有が見つかりません")
    return {"ok": True, "share_id": share_id, "expires_at": expires}


# フォーク（「この会話を引き継いで質問」）。

_FORK_DENY_MESSAGES = {
    "not_received_share": "自分の受領した共有のみ引き継げます",
    "share_unavailable": "この共有は開けません（無効・期限切れ・取消済み・招待外のいずれか）",
    "personal_blocked": "この共有は開けません（元会話が個人ファイルを参照しています）",
}


@router.post("/conversations/{wid}/fork", tags=["会話共有"], response_model=ConversationForkResponse)
def conversation_fork(wid: int, request: Request):
    """受領共有ラッパー `wid` を自分の会話として複製し、続けて質問できるようにする。"""
    u = _current_user(request)
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    try:
        # 複製と監査を同一トランザクションで書く（`store.fork_received_share`）。監査失敗は例外として伝播し、下の汎用 except で fail-closed（複製ごと rollback 済み）。
        new_cid = store.fork_received_share(u["uid"], wid, ip_hash=ip_hash, user_agent=ua)
    except LookupError:
        raise HTTPException(404, "会話が見つかりません")
    except store.ForkNotAllowedError as e:
        reason = e.args[0] if e.args else "share_unavailable"
        try:
            store.audit(u["uid"], "share.forked", "share", None,
                        outcome="deny", reason=reason, severity="warning",
                        ip_hash=ip_hash, user_agent=ua, detail={"wrapper_conversation_id": wid})
        except Exception:
            pass
        raise HTTPException(403, _FORK_DENY_MESSAGES.get(reason, _FORK_DENY_MESSAGES["share_unavailable"]))
    except Exception:
        _log.critical("audit write failed for share.forked (fail-closed) – wrapper %s", wid)
        raise HTTPException(500, "フォーク処理中にエラーが発生しました")
    return {"ok": True, "conversation_id": new_cid}


# 再共有（「スナップショットを更新」）。

@router.post("/conversation-shares/{share_id}/refresh", tags=["会話共有"],
             response_model=ConversationShareRefreshResponse)
def conversation_share_refresh(share_id: int, request: Request):
    """サニタイズ共有のスナップショットを最新の内容へ取り直す（所有者のみ）。通常共有（元会話をライブ参照）は 409。"""
    u = _current_user(request)
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    try:
        # 更新と監査は同一トランザクション（監査失敗なら更新も戻る）。
        result = store.refresh_sanitized_share(u["uid"], share_id,
                                               audit={"ip_hash": ip_hash, "user_agent": ua})
    except LookupError:
        raise HTTPException(404, "共有が見つかりません")
    except PermissionError:
        raise HTTPException(403, "所有者のみ更新できます")
    except store.ShareNotSanitizedError:
        raise HTTPException(409, "この共有は常に最新の内容を表示します")
    except Exception:
        _log.critical("share.refreshed failed (share %s) – fail-closed", share_id)
        raise HTTPException(500, "更新処理中にエラーが発生しました")

    return {"ok": True, "share_id": share_id, "refreshed_at": result["refreshed_at"]}


@router.get("/conversations/{cid}/shares", tags=["会話共有"], response_model=list[ShareListItem])
def conversation_shares_list(cid: int, request: Request):
    """元会話 `cid` を対象にした共有一覧（所有者のみ）。サニタイズ有無・招待者・期限・取消・最終更新時刻を返す。"""
    u = _current_user(request)
    if not store.owns_conversation(u["uid"], cid):
        raise HTTPException(403, "自分の会話のみ確認できます")
    return store.list_shares_for_conversation(u["uid"], cid)
