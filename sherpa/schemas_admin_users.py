"""ユーザー管理 API（sherpa/routers/admin_users.py）の作成・一括追加・変更の応答スキーマ。`sherpa.schemas` から再公開する。"""
from __future__ import annotations

from pydantic import BaseModel


class UserCreateRow(BaseModel):
    """ユーザー作成の応答（`last_login_at` を持たない・`UserRow` と別形）。"""
    uid: str
    email: str | None
    display_name: str | None
    role: str
    status: str
    must_change_password: bool


class AdminUserCreateResponse(BaseModel):
    ok: bool
    user: UserCreateRow
    audit_recorded: bool = True


class AdminUserImportResponse(BaseModel):
    created: int
    uids: list[str]
    audit_recorded: bool = True


class AdminUserPatchResponse(BaseModel):
    ok: bool
    uid: str
    audit_recorded: bool = True
