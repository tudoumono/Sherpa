"""外部 API の router と、各エンドポイントが共有する world 解決・出典モデル。
"""
from __future__ import annotations

import logging
from pathlib import Path

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from sherpa import worlds

_log = logging.getLogger("sherpa")

router = APIRouter(prefix="/ext/v1", tags=["外部連携API"])


def _resolve_world_or_error(world: str, *, connect_timeout: float | None = None,
                            statement_timeout_ms: int | None = None) -> Path:
    """外部 API 専用の strict 解決。registry 不達／登録 root 不達は 503、未登録/未実在は 404。
    `connect_timeout`/`statement_timeout_ms`（省略可）は `worlds.resolve_external_world()` へ転送する。
    """
    try:
        res = worlds.resolve_external_world(world, connect_timeout=connect_timeout,
                                            statement_timeout_ms=statement_timeout_ms)
    except worlds.ExternalResolverError as e:
        from sherpa.ingest.graph_extract import _log_masked_exception
        _log_masked_exception(_log, "ext_api: world resolver 到達不可", e)
        raise HTTPException(
            503, "資料フォルダの参照先を確認できませんでした（一時的な障害の可能性があります）") from e
    if res.status != "ok":
        raise HTTPException(404, "資料フォルダ（world）が見つかりません")
    return res.path


# ==== 出典（sources）共通モデル（`C-EXT-ANSWER-01`／Codex ジョブ結果と共有）====

class ExtSourceItem(BaseModel):
    """出典（`sources`）の共通スキーマ。

    `doc_id`: world 内の相対パス（`GET /ext/v1/doc?world=<world>&path=<doc_id>` の `path` へそのまま渡せる）。
    `locator`（省略可）: 出典の箇所（行の範囲等）。確かめられた箇所を持たない出典は省略する。
    """
    doc_id: str
    locator: str | None = None
    note: str | None = None  # 利用者の範囲の外にある、管理者が「範囲の外でも読む」優先にしている資料への注記（無ければ省く）
