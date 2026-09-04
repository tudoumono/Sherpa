"""管理者:ユーザー管理エンドポイント（フェーズ3スライス2・純移動）。

`GET/POST /admin/users`・`PATCH /admin/users/{uid}` を api.py から抽出する。
ロジックは変更しない（コード移動のみ）。ルート表 golden の定義順を保つため、api.py 側は
削除したブロックの元位置に `app.include_router(admin_users.router)` を1回だけ置く。

このモジュールは `sherpa.api` を import しない（循環回避）。
"""
from __future__ import annotations

import logging
import re
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

from sherpa import auth, store
from sherpa.deps import _current_user, _require_admin, ensure_workspace
from sherpa.deps import _validate_new_password
from sherpa.schemas import AdminUserCreateResponse, AdminUserPatchResponse, AdminUsersListResponse

_log = logging.getLogger("sherpa")

# uid は slug 制約（パストラバーサル防止・workspace パス注入防止）。api.py の同名定数と同一定義。
_UID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# router に tags を持たせない: 各エンドポイントの `tags=["管理者:ユーザー管理"]` と結合されて
# 二重化してしまう（ルート表 golden 不一致の原因）ため、tags 指定は各デコレータ側のみに残す
# （system.py:42-44 と同じパターン）。
router = APIRouter()


class UserCreateReq(BaseModel):
    uid: str
    display_name: str | None = None
    role: str = "user"
    password: str          # 初期パスワード（平文で受け取り hash して保存）
    email: str | None = None


class UserPatchReq(BaseModel):
    status: str | None = None       # active / disabled
    role: str | None = None
    password: str | None = None     # パスワード再設定（平文）
    # キー省略（JSON null 含む）＝変更なし。空文字は「クリア」の明示的な値として upsert_user に渡り、
    # 表示名を空にする（COALESCE は NULL のみ既存維持・空文字は上書き）。
    display_name: str | None = None


# ===== 管理者: ユーザー管理 =====

@router.get("/admin/users", tags=["管理者:ユーザー管理"], response_model=AdminUsersListResponse)
def admin_users_list(request: Request):
    """ユーザー一覧（管理者のみ）。"""
    _require_admin(_current_user(request))
    return {"users": store.list_users()}


@router.post("/admin/users", tags=["管理者:ユーザー管理"], response_model=AdminUserCreateResponse)
def admin_user_create(req: UserCreateReq, request: Request):
    """ユーザー作成（管理者のみ）。uid は slug 形式のみ。"""
    actor = _current_user(request)
    _require_admin(actor)
    uid = (req.uid or "").strip()
    if not uid or not _UID_PATTERN.match(uid):
        raise HTTPException(422, "uid は英数字＋._- のみ（先頭は英数字）")
    if req.role not in ("user", "admin"):
        raise HTTPException(422, "role は user / admin のみ")
    if not req.password:
        raise HTTPException(422, "初期パスワードは必須です")
    problem = _validate_new_password(uid, "", req.password, req.password)
    if problem:
        raise HTTPException(422, problem)
    ph = auth.hash_password(req.password)
    try:
        row = store.create_user(uid, email=req.email, display_name=req.display_name,
                                password_hash=ph, role=req.role, status="active")
    except Exception as e:
        try:
            store.audit(actor["uid"], "user.created", "user", f"user:{uid}",
                        outcome="error", reason="db_error",
                        after_state={"uid": uid, "role": req.role},
                        severity="critical")
        except Exception:
            pass
        raise HTTPException(409, f"ユーザー作成に失敗しました: {e}")
    if row is None:
        # RV「バッチ2」4番（2026-07-03）: 既存 uid（無効化済み含む）への「作成」は upsert で
        # 黙って上書きしていた（パスワード/権限が置き換わる事故）。作成専用の store.create_user は
        # ON CONFLICT DO NOTHING＝None を返す＝作成せず明示的に 409 で拒否する。
        try:
            store.audit(actor["uid"], "user.create_rejected", "user", f"user:{uid}",
                        outcome="deny", reason="uid_exists", severity="warning")
        except Exception:
            pass
        raise HTTPException(409, "このユーザーIDは既に存在します")
    # 個人 workspace ディレクトリを冪等作成（無効化でも消さない・実装計画 §1）。
    try:
        ensure_workspace(uid)
    except Exception as ws_err:
        # workspace 作成失敗はログだけ（ユーザー作成自体は成功扱い・初回利用時に再度 ensure）。
        _log.warning("workspace provisioning failed for uid=%s: %s", uid, ws_err)
    try:
        store.audit(actor["uid"], "user.created", "user", f"user:{uid}",
                    detail={"password_set": True, "created_via": "admin_ui"},
                    outcome="success", severity="critical",
                    after_state={"uid": uid, "email": req.email,
                                 "display_name": req.display_name,
                                 "role": req.role, "status": "active"})
    except Exception:
        _log.critical("audit write failed for user.created")
        # fail-closed: ユーザーは作成済みだが監査失敗→整合性保持のため HTTP 500 は返さず続行
        # （監査が書けなかった事実は critical log で記録）
    return {"ok": True, "user": row}


@router.patch("/admin/users/{uid}", tags=["管理者:ユーザー管理"], response_model=AdminUserPatchResponse)
def admin_user_patch(uid: str, req: UserPatchReq, request: Request):
    """ユーザーの無効化・role変更・表示名修正・パスワード再設定（管理者のみ）。

    実際に値が変わったフィールドだけを更新・監査する（キー省略・JSON null・現在値と同じ値の
    再送はいずれも「変更なし」＝upsert/監査の対象外。変更が0件なら 422）。1回の PATCH で
    複数フィールドが実際に変わった場合も、監査は既存の action 名ごとに行を分けて出す
    （1行に集約すると他の action の検索結果から実操作が欠落するため）。同一 PATCH 内の
    複数行は request_id で対応付ける。
    """
    actor = _current_user(request)
    _require_admin(actor)
    if not _UID_PATTERN.match(uid):
        raise HTTPException(422, "不正な uid")
    target = store.get_user(uid)
    if not target:
        raise HTTPException(404, "ユーザーが見つかりません")

    if req.status is not None and req.status not in ("active", "disabled"):
        raise HTTPException(422, "status は active / disabled のみ")
    # self-disable チェック（実際に状態が変わるかどうかに関わらず、要求そのものを拒否する）。
    if req.status == "disabled" and uid == actor["uid"]:
        try:
            store.audit(actor["uid"], "user.disabled", "user", f"user:{uid}",
                        outcome="deny", reason="self_disable", severity="warning")
        except Exception:
            pass
        raise HTTPException(403, "自分自身を無効化できません")
    if req.role is not None and req.role not in ("user", "admin"):
        raise HTTPException(422, "role は user / admin のみ")

    ph = None
    if req.password is not None:
        problem = _validate_new_password(uid, "", req.password, req.password)
        if problem:
            raise HTTPException(422, problem)
        ph = auth.hash_password(req.password)

    # 実差分だけを抽出する（各要素が upsert 対象フィールド1件・対応する監査行1件を表す）。
    changes: list[dict] = []
    if req.status is not None and req.status != target["status"]:
        changes.append({
            "field": "status", "value": req.status,
            "action": "user.disabled" if req.status == "disabled" else "user.created",
            "before": {"status": target["status"]}, "after": {"status": req.status},
            "severity": "info",
        })
    if req.role is not None and req.role != target["role"]:
        changes.append({
            "field": "role", "value": req.role, "action": "user.role_changed",
            "before": {"role": target["role"]}, "after": {"role": req.role},
            "severity": "critical" if req.role == "admin" else "info",
        })
    if req.display_name is not None and req.display_name != target["display_name"]:
        changes.append({
            "field": "display_name", "value": req.display_name, "action": "user.display_name_changed",
            "before": {"display_name": target["display_name"]}, "after": {"display_name": req.display_name},
            "severity": "info",
        })
    if req.password is not None:
        changes.append({
            "field": "password_hash", "value": ph, "action": "user.password_reset",
            "before": None, "after": None, "detail": {"password_changed": True},
            "severity": "info",
        })

    if not changes:
        raise HTTPException(422, "変更フィールドがありません")

    # upsert_user はデフォルト role="user"/status="active" を持つため、
    # 未変更の role/status は現在値を必ず渡す（降格/再有効化の意図せぬ副作用を防ぐ）。
    safe_updates = {c["field"]: c["value"] for c in changes
                    if c["field"] in ("email", "display_name", "password_hash", "role", "status")}
    if "role" not in safe_updates:
        safe_updates["role"] = target["role"]
    if "status" not in safe_updates:
        safe_updates["status"] = target["status"]
    if "password_hash" in safe_updates:
        # 管理者による再設定＝新パスワードは管理者も知っている状態のため、本人の初回ログインで
        # 変更を強制する（作成時の初期パスワードと同じ扱い・None なら既存フラグ維持）。
        safe_updates["must_change_password"] = True
    store.upsert_user(uid, **safe_updates)

    # 監査行群は「全部書けるか、1件も残らないか」（部分確定を防ぐ）。主変更（upsert_user）は
    # 既に確定済みで、監査バッチの失敗で取り消さない（best-effort＝主変更は監査の成否に関わらず
    # 成功のまま・応答も 200 のまま）。バッチ全体を1つの接続/トランザクションに載せ（`_audit_insert`
    # は呼び出し側のトランザクションを受ける契約・sherpa/store/audit.py 冒頭の不変条件参照）、
    # 途中の1件が失敗したら丸ごとロールバックする。例外はバッチ外で1回だけ捕捉し、request_id・
    # action 一覧・例外情報を critical ログへ残す（一部の action だけ監査に残る事故を防ぐ）。
    request_id = uuid.uuid4().hex   # 同一 PATCH 内の複数監査行を対応付ける相関 ID。
    try:
        with store._connect() as c:
            for ch in changes:
                store._audit_insert(c, actor["uid"], ch["action"], "user", f"user:{uid}",
                                    detail=ch.get("detail"),
                                    outcome="success", severity=ch["severity"], request_id=request_id,
                                    before_state=ch["before"], after_state=ch["after"])
    except Exception:
        _log.critical("audit batch write failed for request_id=%s actions=%s",
                      request_id, [ch["action"] for ch in changes], exc_info=True)
    return {"ok": True, "uid": uid}
