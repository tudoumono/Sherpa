"""会話共有エンドポイント（フェーズ3スライス3・純移動）。

`GET /users/suggest`・`POST /conversations/{cid}/shares`・`GET /share/conversations/{token}`・
`POST /conversation-shares/{share_id}/revoke` を api.py から抽出する。
ロジックは変更しない（コード移動のみ）。ルート表 golden の定義順を保つため、api.py 側は
削除したブロックの元位置に `app.include_router(shares.router)` を1回だけ置く。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from pydantic import BaseModel

from sherpa import auth, store
from sherpa.deps import _COOKIE, _client_ip_hash, _current_user, _synthetic_admin
from sherpa.schemas import ShareCreateResponse, UsersSuggestResponse

_log = logging.getLogger("sherpa")

# router に tags を持たせない: 各エンドポイントの `tags=["会話共有"]` と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
router = APIRouter()


class ShareCreateReq(BaseModel):
    invitee_user_ids: list[str]
    expires_at: datetime | None = None   # None＝無期限（2026-07-02-共有の無期限と永続化.md）
    sanitize: bool = False        # 個人 workspace 参照会話を、個人部分を伏せた snapshot として共有する（§Phase2）


class ShareRevokeReq(BaseModel):
    pass


# ===== 共有エンドポイント =====

@router.get("/users/suggest", tags=["会話共有"], response_model=UsersSuggestResponse)
def users_suggest(request: Request, q: str = Query("")):
    """共有ダイアログの入力補完（バッチ2・5番・2026-07-03）: uid/表示名の部分一致で
    active ユーザーを返す（無効化ユーザー・自分自身は除外）。ログイン必須・上限10件。
    返す列は uid/display_name のみ（email・role 等は渡さない）。
    """
    u = _current_user(request)
    q = (q or "").strip()
    if not q:
        return {"users": []}
    return {"users": store.suggest_users(q, u["uid"], limit=10)}


@router.post("/conversations/{cid}/shares", tags=["会話共有"], response_model=ShareCreateResponse)
def conversation_share_create(cid: int, req: ShareCreateReq, request: Request):
    """共有リンク発行（所有者のみ・個人 workspace 含む会話は拒否）。一度だけ表示する URL を返す。"""
    u = _current_user(request)
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]

    # 所有者確認
    if not store.owns_conversation(u["uid"], cid):
        try:
            store.audit(u["uid"], "share.created", "share", None,
                        outcome="deny", reason="not_owner", severity="warning",
                        ip_hash=ip_hash, user_agent=ua,
                        detail={"conversation_id": cid})
        except Exception:
            pass
        raise HTTPException(403, "自分の会話のみ共有できます")

    # 個人 workspace ガード（§Phase2: sanitize=true なら個人部分を伏せた snapshot を共有）
    share_target_cid = cid
    sanitized = False
    conv_row = store.get_conversation_for_read(u["uid"], cid)
    if req.sanitize:
        # sanitize=true は **flag に関係なく必ず snapshot を共有**する（in-flight で flag 未設定でも
        #   ライブ元の個人内容を共有しない・RV HIGH）。snapshot は per-turn 個人フラグで Q/A を伏字化。
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
    # 多層防御: 会話フラグ contains_personal_workspace **または** 個人ターン(messages.personal)が
    #   1件でもあれば通常共有を拒否（フラグとメッセージがズレても漏らさない・Codex RV の任意提案）。
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

    # 招待先 uid 検証
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
    expires = req.expires_at   # None＝無期限
    if expires is not None and expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    sid = store.create_share(share_target_cid, u["uid"], th, expires, req.invitee_user_ids)

    try:
        store.audit(u["uid"], "share.created", "share", f"share:{sid}",
                    detail={"conversation_id": share_target_cid, "source_conversation_id": cid,
                            "sanitized": sanitized, "owner_uid": u["uid"],
                            "invitee_uids": req.invitee_user_ids,
                            "expires_at": expires.isoformat() if expires is not None else None},
                    outcome="success", severity="info",
                    ip_hash=ip_hash, user_agent=ua)
    except Exception:
        # fail-closed: 共有は作成済みだが監査失敗→取消して拒否。
        _log.critical("audit write failed for share.created – revoking share %s", sid)
        store.revoke_share(sid, u["uid"])
        raise HTTPException(500, "共有処理中にエラーが発生しました")

    share_url = f"/share/conversations/{token}"
    return {"ok": True, "share_id": sid, "url": share_url,
            "note": "この URL は一度だけ表示されます。招待者に共有してください。"}


@router.get("/share/conversations/{token}", tags=["会話共有"])
def share_click(token: str, request: Request):
    """共有 token のクリック処理。未ログイン→/ui/login.html へ。有効なら受領ラッパー作成→chat へ redirect。"""
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

    # 受領ラッパー作成（冪等）。共有元が同時に物理削除された極小レース（RV HIGH 対応・store.accept_share
    # の FOR UPDATE ロック導入後の残余ケース）は ValueError として拒否する。
    try:
        wid = store.accept_share(share["id"], u["uid"])
    except ValueError:
        try:
            store.audit(u["uid"], "share.denied", "share", f"share:{share['id']}",
                        outcome="deny", reason="source_gone", severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
        raise HTTPException(403, "この共有は開けません（共有元が削除されました）")

    try:
        store.audit(u["uid"], "share.accepted", "share", f"share:{share['id']}",
                    detail={"wrapper_conversation_id": wid,
                            "source_conversation_id": share["conversation_id"]},
                    outcome="success", ip_hash=ip_hash, user_agent=ua)
    except Exception:
        _log.critical("audit write failed for share.accepted (share %s) – fail-closed", share["id"])
        raise HTTPException(500, "共有処理中にエラーが発生しました")

    return RedirectResponse(f"/ui/chat.html?conversation_id={wid}", status_code=302)


@router.post("/conversation-shares/{share_id}/revoke", tags=["会話共有"])
def conversation_share_revoke(share_id: int, request: Request):
    """共有を取消（所有者のみ）。"""
    u = _current_user(request)
    ip_hash = _client_ip_hash(request)
    ua = request.headers.get("user-agent", "")[:512]
    ok = store.revoke_share(share_id, u["uid"])
    if not ok:
        try:
            store.audit(u["uid"], "share.revoked", "share", f"share:{share_id}",
                        outcome="deny", reason="not_owner_or_already_revoked", severity="warning",
                        ip_hash=ip_hash, user_agent=ua)
        except Exception:
            pass
        raise HTTPException(403, "取消できません（所有者以外 or 既に取消済み）")
    try:
        store.audit(u["uid"], "share.revoked", "share", f"share:{share_id}",
                    outcome="success", ip_hash=ip_hash, user_agent=ua)
    except Exception:
        _log.critical("audit write failed for share.revoked (fail-closed)")
        raise HTTPException(500, "取消処理中にエラーが発生しました")
    return {"ok": True, "share_id": share_id}
