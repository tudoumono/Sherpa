"""管理者向けユーザー管理エンドポイント（`GET/POST /admin/users`・`PATCH /admin/users/{uid}`）。
`sherpa.api` を import しない。
設計: docs/design/users.md「役割と管理者の権限」
"""
from __future__ import annotations

import csv
import io
import logging
import re
import uuid

from fastapi import APIRouter, File, HTTPException, Request, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from sherpa import auth, store
from sherpa.deps import _current_user, _require_admin, ensure_workspace
from sherpa.deps import _validate_new_password
from sherpa.schemas import (AdminUserCreateResponse, AdminUserImportResponse, AdminUserPatchResponse,
                            AdminUsersListResponse)

_log = logging.getLogger("sherpa")

# uid は slug 制約（パストラバーサルと workspace パス注入を防ぐ）。api.py の同名定数と同一定義。
_UID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")

# router に tags を持たせない（各デコレータの tags と二重になりルート表 golden が一致しなくなる）。
router = APIRouter()


class UserCreateReq(BaseModel):
    uid: str
    display_name: str | None = None
    role: str = "user"
    password: str  # 初期パスワード（平文で受け取り hash して保存）。
    email: str | None = None


class UserPatchReq(BaseModel):
    status: str | None = None       # active / disabled
    role: str | None = None
    password: str | None = None  # パスワード再設定（平文）。
    # キー省略（JSON null 含む）は変更なし。空文字はクリアの明示値として渡り、表示名を空にする。
    display_name: str | None = None


@router.get("/admin/users", tags=["管理者:ユーザー管理"], response_model=AdminUsersListResponse)
def admin_users_list(request: Request):
    """ユーザー一覧を返す（管理者のみ）。"""
    _require_admin(_current_user(request))
    return {"users": store.list_users()}


@router.post("/admin/users", tags=["管理者:ユーザー管理"], response_model=AdminUserCreateResponse)
def admin_user_create(req: UserCreateReq, request: Request):
    """ユーザーを作成する（管理者のみ）。uid は slug 形式のみ。既存 uid（無効化済み含む）は 409 で拒否する。"""
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
        # 作成専用の `store.create_user` は既存 uid なら None を返す（ON CONFLICT DO NOTHING）ため、上書きせず 409 で拒否する。
        try:
            store.audit(actor["uid"], "user.create_rejected", "user", f"user:{uid}",
                        outcome="deny", reason="uid_exists", severity="warning")
        except Exception:
            pass
        raise HTTPException(409, "このユーザーIDは既に存在します")
    # 個人 workspace ディレクトリを冪等に作成する（無効化でも消さない）。
    try:
        ensure_workspace(uid)
    except Exception as ws_err:
        # workspace 作成の失敗はログだけにする（ユーザー作成自体は成功扱い・初回利用時に再度 ensure）。
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
        # ユーザーは作成済みのため、監査に失敗しても 500 にはせず続行し、critical log に残す。
    return {"ok": True, "user": row}


_IMPORT_MAX_ROWS = 200
_IMPORT_MAX_BYTES = 1024 * 1024
_IMPORT_COLUMNS = ("uid", "display_name", "email", "role", "password")


@router.post("/admin/users/import", tags=["管理者:ユーザー管理"], response_model=AdminUserImportResponse)
def admin_users_import(request: Request, file: UploadFile = File(...)):
    """CSV（uid,display_name,email,role,password）からユーザーを一括追加する（管理者のみ）。
    ① 全行を検査し、誤りが 1 行でもあれば 1 人も追加せず 422 `{"errors": [{line, uid, reason}]}` を返す
    ② 誤りが無ければ 1 トランザクションで全員を作成（全員 must_change_password=True）
    ③ 監査は 1 人ずつ `user.created`（detail.created_via=csv_import）。パスワードの値は応答・ログ・監査に出さない。
    """
    actor = _current_user(request)
    _require_admin(actor)
    raw = file.file.read(_IMPORT_MAX_BYTES + 1)
    if len(raw) > _IMPORT_MAX_BYTES:
        raise HTTPException(422, "ファイルが大きすぎます（1 MiB まで）")
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:
        raise HTTPException(422, "文字コードは UTF-8 にしてください")
    reader = csv.DictReader(io.StringIO(text), strict=True)
    try:
        fieldnames = reader.fieldnames
    except csv.Error:
        raise HTTPException(422, "CSV の形式が正しくありません（引用符の閉じ忘れなど）")
    # 見出しは 5 列ちょうど（重複・余分な列があると後ろの列の値が使われるため拒否する）
    if sorted(fieldnames or []) != sorted(_IMPORT_COLUMNS):
        return JSONResponse(status_code=422, content={"errors": [
            {"line": 1, "uid": "", "reason": "見出しは " + ",".join(_IMPORT_COLUMNS) + " の 5 列にしてください（重複・余分な列は不可）"}]})
    rows = []
    try:
        for r in reader:
            rows.append((reader.line_num, r))
            if len(rows) > _IMPORT_MAX_ROWS:
                raise HTTPException(422, f"行数が多すぎます（{_IMPORT_MAX_ROWS} 行まで）")
    except csv.Error:
        raise HTTPException(422, f"CSV の形式が正しくありません（{reader.line_num} 行目付近・引用符の閉じ忘れなど）")
    if not rows:
        raise HTTPException(422, "取り込む行がありません")

    errors: list[dict] = []
    seen: set[str] = set()
    seen_email: set[str] = set()
    todo: list[dict] = []
    for line, r in rows:
        uid = (r.get("uid") or "").strip()
        email = (r.get("email") or "").strip() or None
        role = (r.get("role") or "").strip()
        password = r.get("password") or ""
        reason = None
        if r.get(None):
            reason = "列の数が見出しと合いません"
        elif not uid or not _UID_PATTERN.match(uid):
            reason = "uid は英数字＋._- のみ（先頭は英数字）で入力してください"
        elif uid in seen:
            reason = "CSV の中で uid が重複しています"
        elif store.get_user(uid) is not None:
            reason = "このユーザーIDは既に存在します"
        elif email and email in seen_email:
            reason = "CSV の中でメールアドレスが重複しています"
        elif email and store.get_user_by_email(email) is not None:
            reason = "このメールアドレスは既に使われています"
        elif role not in ("user", "admin"):
            reason = "role は user / admin のみ"
        elif not password:
            reason = "初期パスワードは必須です"
        else:
            reason = _validate_new_password(uid, "", password, password)
        seen.add(uid)
        if email:
            seen_email.add(email)
        if reason:
            errors.append({"line": line, "uid": uid, "reason": reason})
            continue
        todo.append({"uid": uid, "email": email,
                     "display_name": (r.get("display_name") or "").strip() or None,
                     "role": role, "password": password})
    if errors:
        return JSONResponse(status_code=422, content={"errors": errors})

    for t in todo:
        t["password_hash"] = auth.hash_password(t.pop("password"))
    try:
        store.create_users_bulk(todo)
    except Exception as e:
        _log.warning("user csv import failed: %s", type(e).__name__)
        raise HTTPException(409, "ユーザーの一括追加に失敗しました（1 人も追加されていません）。もう一度お試しください")
    for t in todo:
        try:
            ensure_workspace(t["uid"])
        except Exception as ws_err:
            _log.warning("workspace provisioning failed for uid=%s: %s", t["uid"], ws_err)
        try:
            store.audit(actor["uid"], "user.created", "user", f"user:{t['uid']}",
                        detail={"password_set": True, "created_via": "csv_import"},
                        outcome="success", severity="critical",
                        after_state={"uid": t["uid"], "email": t["email"],
                                     "display_name": t["display_name"],
                                     "role": t["role"], "status": "active"})
        except Exception:
            _log.critical("audit write failed for user.created")
    return {"created": len(todo), "uids": [t["uid"] for t in todo]}


@router.patch("/admin/users/{uid}", tags=["管理者:ユーザー管理"], response_model=AdminUserPatchResponse)
def admin_user_patch(uid: str, req: UserPatchReq, request: Request):
    """ユーザーの無効化・role 変更・表示名修正・パスワード再設定を行う（管理者のみ）。
    実際に値が変わったフィールドだけを更新する（キー省略・JSON null・現在値と同じ値は変更なし・変更が 0 件なら 422）。
    監査は変わったフィールドの種類ごとに 1 行ずつ記録し、同じ PATCH の行には共通の request_id を付ける。
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
    # self-disable チェック（実際に状態が変わるかに関わらず要求を拒否する）。
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

    # 実差分だけを抽出する（各要素が upsert 対象フィールド 1 件・対応する監査行 1 件）。
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

    # upsert_user は既定で role="user"/status="active" を持つため、未変更の role/status は現在値を渡す。
    safe_updates = {c["field"]: c["value"] for c in changes
                    if c["field"] in ("email", "display_name", "password_hash", "role", "status")}
    if "role" not in safe_updates:
        safe_updates["role"] = target["role"]
    if "status" not in safe_updates:
        safe_updates["status"] = target["status"]
    if "password_hash" in safe_updates:
        # 管理者による再設定は新パスワードを管理者も知っているため、本人の初回ログインで変更を強制する（None なら既存フラグを維持）。
        safe_updates["must_change_password"] = True
    store.upsert_user(uid, **safe_updates)

    # 監査行群は「全部書けるか、1 件も残らないか」にする。主変更（upsert_user）は確定済みで、監査バッチの失敗では取り消さず 200 のまま返す。バッチ全体を 1 つの接続/トランザクションに載せ（`_audit_insert` は呼び出し側のトランザクションを受ける）、失敗したら丸ごとロールバックして、request_id・action 一覧・例外情報を critical ログへ 1 回だけ残す。
    request_id = uuid.uuid4().hex  # 同一 PATCH 内の複数監査行を対応付ける相関 ID。
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
