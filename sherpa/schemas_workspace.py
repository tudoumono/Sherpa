"""個人ワークスペース API（sherpa/routers/workspace.py）の応答スキーマ。`sherpa.schemas` から再公開する。"""
from __future__ import annotations

from pydantic import BaseModel


class WorkspaceFileRow(BaseModel):
    """個人 workspace のファイル一覧（GET /workspace/files）。`created_at`/`expires_at` は文字列。"""
    id: int
    rel_path: str
    size_bytes: int
    created_at: str
    expires_at: str | None


class WorkspaceFilesListResponse(BaseModel):
    files: list[WorkspaceFileRow]


class WorkspaceFileUploadResponse(BaseModel):
    ok: bool
    id: int
    rel_path: str
    size_bytes: int
    sha256: str


class WorkspaceFileDeleteResponse(BaseModel):
    ok: bool
    id: int
    rel_path: str
    physical_deleted: bool = True


class WorkspaceSearchHit(BaseModel):
    rel_path: str
    line: int
    text: str
    match: str


class WorkspaceSearchResponse(BaseModel):
    query: str
    source: str
    hits: list[WorkspaceSearchHit]
    unsearched: list[str] = []
